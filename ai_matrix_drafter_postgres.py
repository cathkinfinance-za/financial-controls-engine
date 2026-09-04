import os
import sys
import json
import time
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List
from typing import Dict, Any

GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

ai_client = genai.Client(api_key=GEMINI_KEY)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def log_to_db(conn, project_id, process_name, message):
    print(f"[{process_name}] {message}")
    with conn.cursor() as cursor:
        cursor.execute("""
            INSERT INTO workflow_control_log (po_number, action_type, actor_email, system_notes)
            VALUES ((SELECT project_reference FROM projects WHERE id = %s), %s, 'System AI Engine', %s);
        """, (project_id, process_name, message))
        cursor.execute("""
            UPDATE projects SET latest_ai_status = %s WHERE id = %s;
        """, (f"[{process_name}] {message}", project_id))
    conn.commit()

class LineItem(BaseModel):
    cost_component_name: str = Field(description="Name or line description of charge item.")
    cost_type_category: str = Field(description="'One-Off Cost' or 'Annual Cost'.")
    amount: float = Field(description="Raw numeric amount.")
    
class PricingExtraction(BaseModel):
    line_items: List[LineItem]
    quote_total: float = Field(description="Final grand total stated on quote.")

class CriterionDetail(BaseModel):
    component_name: str = Field(description="Name of the technical/qualitative evaluation criteria.")
    weight_percent: float = Field(description="Percentage weight of this criteria.")
    vendor_scores: Dict[str, float] = Field(description="Dictionary mapping vendor name to numeric score out of 10.0.")
    vendor_justifications: Dict[str, str] = Field(description="Dictionary mapping vendor name to a brief qualitative justification explaining the assigned score.")

class Phase1Output(BaseModel):
    price_weight_percent: int
    precheck_analysis: str
    criteria: List[CriterionDetail]
    line_items: List[Any]  # Or your specific pricing line-item format if handled together

def clean_schema(schema):
    if isinstance(schema, dict):
        schema.pop("additionalProperties", None)
        for key, value in schema.items():
            clean_schema(value)
    elif isinstance(schema, list):
        for item in schema:
            clean_schema(item)
    return schema

def process_vendor_quote_pricing(conn, vendor_record, project_id):
    v_id = vendor_record['id']
    v_name = vendor_record['vendor_name']
    file_bytes = vendor_record.get('quote_file_bytes')
    filename = vendor_record.get('quote_filename') or "quote.pdf"

    if not file_bytes:
        log_to_db(conn, project_id, "Pricing Extractor", f"⚠️ No PDF binary found for '{v_name}'. Skipping.")
        return

    # Fetch active prompt template and selected model from system_prompts
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT prompt_template, selected_model FROM system_prompts 
            WHERE process = 'project matrix drafter' AND is_active = true;
        """)
        prompt_record = cursor.fetchone()

    # Fetch valid units
    with conn.cursor() as cursor:
        cursor.execute("SELECT unit_code, unit_name FROM units_of_measure;")
        valid_units = cursor.fetchall()
        uom_list_str = ", ".join([f"'{u['unit_code']}' ({u['unit_name']})" for u in valid_units])

    if not prompt_record:
        log_to_db(conn, project_id, "Pricing Extractor", "❌ Aborted: Active 'project matrix drafter' prompt not found in system_prompts.")
        return

    prompt_template = prompt_record['prompt_template']
    model_name = prompt_record['selected_model'] or 'gemini-3.5-flash-lite'

    # Fetch project scopes (JSONB) for normalization baselines
    with conn.cursor() as cursor:
        cursor.execute("SELECT project_scopes FROM projects WHERE id = %s;", (project_id,))
        proj_res = cursor.fetchone()
        project_scopes = proj_res.get('project_scopes', {}) if proj_res else {}

    log_to_db(conn, project_id, "Pricing Extractor", f"🧠 Gemini parsing quote items & quantities for {v_name} using {model_name}...")
    
    doc_part = types.Part.from_bytes(data=bytes(file_bytes), mime_type="application/pdf")

    ai_response = ai_client.models.generate_content(
        model=model_name,
        contents=[doc_part, prompt_template],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=PricingExtraction,
            temperature=0.1
        )
    )

    extracted_data = json.loads(ai_response.text)
    items = extracted_data.get("line_items", [])
    quote_total = extracted_data.get("quote_total")

    processed_items = []
    total_option_qty = 0.0
    primary_uom = ""

    for item in items:
        qty = float(item.get("quantity", 1.0) or 1.0)
        uom = item.get("unit_of_measure", "").strip().lower()
        raw_amount = float(item.get("amount", 0.0))
        
        unit_rate = round(raw_amount / qty, 2) if qty > 0 else raw_amount
        final_amount = raw_amount

        if uom in project_scopes and qty > 0:
            target_baseline_qty = float(project_scopes[uom])
            if qty != target_baseline_qty:
                final_amount = round(unit_rate * target_baseline_qty, 2)
                qty = target_baseline_qty  # update quantity to the normalized baseline

        # Track total quantity and primary unit of measure at the option level
        total_option_qty += qty
        if not primary_uom and uom:
            primary_uom = uom

        processed_items.append({
            "cost_component_name": item["cost_component_name"],
            "cost_type_category": item["cost_type_category"],
            "amount": final_amount
        })

    if quote_total and quote_total > 0:
        sum_items = round(sum(i["amount"] for i in processed_items), 2)
        diff = round(quote_total - sum_items, 2)
        if abs(diff) > 0.01:
            processed_items.append({
                "cost_component_name": "VAT / Alignment Adjustment",
                "cost_type_category": "One-Off Cost",
                "amount": diff
            })

    with conn.cursor() as cursor:
        # 1. Update procurement_options with total quantity and unit of measure
        cursor.execute("""
            UPDATE procurement_options 
            SET total_quantity = %s, unit_of_measure = %s
            WHERE id = %s;
        """, (total_option_qty, primary_uom, v_id))

        # 2. Insert line items without quantity, unit_of_measure, or calculated_unit_rate
        cursor.execute("DELETE FROM options_line_items_pricing WHERE procurement_option_id = %s;", (v_id,))
        for idx, item in enumerate(processed_items):
            line_item_id = f"PRICE_{v_id}_{idx}_{int(time.time())}"
            cursor.execute("""
                INSERT INTO options_line_items_pricing 
                (line_item_id, procurement_option_id, cost_component_name, cost_type_category, amount)
                VALUES (%s, %s, %s, %s, %s);
            """, (line_item_id, v_id, item["cost_component_name"], item["cost_type_category"], item["amount"]))
            
    conn.commit()
    log_to_db(conn, project_id, "Pricing Extractor", f"✅ Option-level quantities and normalized pricing inserted for {v_name}.")

def execute_phase1(project_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM projects WHERE id = %s;", (project_id,))
            project = cursor.fetchone()
            if not project:
                return

            cursor.execute("SELECT * FROM procurement_options WHERE project_id = %s;", (project_id,))
            vendors = cursor.fetchall()

        if not vendors:
            log_to_db(conn, project_id, "AI Matrix Drafter", "❌ Aborted: No procurement options linked to project.")
            return

        gemini_contents = []

        vendor_names = []
        vendor_map = {}

        for v in vendors:
            v_name = v['vendor_name']
            vendor_names.append(v_name)
            vendor_map[v_name] = v['id']
            gemini_contents.append(f"=== VENDOR OPTION: {v_name} ===")
            if v.get('quote_file_bytes'):
                doc_part = types.Part.from_bytes(data=bytes(v['quote_file_bytes']), mime_type="application/pdf")
                gemini_contents.append(doc_part)

        schema_example = ", ".join([f'"{v}": 7.5' for v in vendor_names])
        
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT prompt_template, selected_model FROM system_prompts 
                WHERE process = 'project matrix drafter' AND is_active = true;
            """)
            prompt_record = cursor.fetchone()

        if not prompt_record:
            log_to_db(conn, project_id, "AI Matrix Drafter", "❌ Aborted: Active 'project matrix drafter' prompt not found in database.")
            return

        template = prompt_record['prompt_template']
        model_name = prompt_record['selected_model'] or 'gemini-3.5-flash'

        # Format template placeholders dynamically
        formatted_prompt = template.format(
            schema_example=schema_example,
            project_reference=project.get('project_reference', ''),
            project_description=project.get('project_description', ''),
            project_objective=project.get('project_objective', ''),
            phase1_prompt_adjustments=project.get('phase1_prompt_adjustments', '')
        )
        
        gemini_contents.append(formatted_prompt)

        raw_schema = Phase1Output.model_json_schema()
        safe_schema = clean_schema(raw_schema)

        log_to_db(conn, project_id, "AI Matrix Drafter", "🧠 Running qualitative evaluation via Gemini...")
        ai_response = ai_client.models.generate_content(
            model=model_name,
            contents=gemini_contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=safe_schema
            )
        )
        
        matrix_data = json.loads(ai_response.text)
        price_weight_pct = int(matrix_data.get("price_weight_percent", 50)) / 100.0

        with conn.cursor() as cursor:
            # Update project price weighting & analysis gap summary
            cursor.execute("""
                UPDATE projects 
                SET price_weighting = %s, analysis = %s 
                WHERE id = %s;
            """, (price_weight_pct, matrix_data.get("precheck_analysis"), project_id))

            # Clear previous criteria and non-pricing items
            cursor.execute("""
                DELETE FROM options_line_items_non_pricing 
                WHERE procurement_option_id IN (SELECT id FROM procurement_options WHERE project_id = %s);
            """, (project_id,))
            cursor.execute("DELETE FROM project_weightings WHERE project_id = %s;", (project_id,))

            # Insert AI-generated criteria weightings & scores
            for item in matrix_data.get("criteria", []):
                cursor.execute("""
                    INSERT INTO project_weightings (project_id, criteria_name, weighting_percent)
                    VALUES (%s, %s, %s) RETURNING id;
                """, (project_id, item["component_name"], item["weight_percent"]))
                weighting_id = cursor.fetchone()['id']

                scores_map = item.get("vendor_scores", {})
                justifications_map = item.get("vendor_justifications", {})

                for v_name in vendor_names:
                    v_id = vendor_map.get(v_name)
                    v_score = float(scores_map.get(v_name, 5.0))
                    v_justification = justifications_map.get(v_name, "")
                    line_item_id = f"NON_PRICE_{v_id}_{weighting_id}"
                    cursor.execute("""
                        INSERT INTO options_line_items_non_pricing 
                        (line_item_id, procurement_option_id, weighting_id, score, justification)
                        VALUES (%s, %s, %s, %s, %s);
                    """, (line_item_id, v_id, weighting_id, v_score, v_justification))
        conn.commit()

        # Parse line-item pricing for each vendor
        for v in vendors:
            process_vendor_quote_pricing(conn, v, project_id)

        # Mark draft complete for user review
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE projects 
                SET draft_matrix_via_ai = 'Complete' 
                WHERE id = %s;
            """, (project_id,))
        conn.commit()
        
        log_to_db(conn, project_id, "AI Matrix Drafter", "✅ Phase 1 complete. Review criteria/weights in UI before recalculating.")

    finally:
        conn.close()

if __name__ == "__main__":
    p_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    execute_phase1(p_id)