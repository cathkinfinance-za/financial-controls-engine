import os
import sys
import json
import time
import datetime
import psycopg2
from google.genai import types
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

def call_gemini_api(model: str, contents: list) -> dict:
    """
    Helper function to invoke Gemini model and return parsed JSON output.
    """
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )
    
    try:
        parsed_content = json.loads(response.text)
    except json.JSONDecodeError:
        parsed_content = {}
        
    return {"parsed_content": parsed_content, "raw_text": response.text}

def clean_schema(schema):
    if isinstance(schema, dict):
        schema.pop("additionalProperties", None)
        for key, value in schema.items():
            clean_schema(value)
    elif isinstance(schema, list):
        for item in schema:
            clean_schema(item)
    return schema

def execute_phase1(conn, project_id):
    """
    Phase 1: Assessment Criteria Formulation
    Retrieves system prompt & adjustments, runs Gemini to define project weights,
    and updates 'projects' and 'project_weightings'.
    """

    with conn.cursor() as cursor:
        # 1. Fetch active system prompt
        cursor.execute("""
            SELECT prompt_template, selected_model 
            FROM system_prompts 
            WHERE process = 'project matrix drafter' AND is_active = true
            LIMIT 1;
        """)
        prompt_row = cursor.fetchone()
        if not prompt_row or not prompt_row.get('prompt_template'):
            raise ValueError("Active system prompt for 'project matrix drafter' not found.")
        
        base_prompt = prompt_row['prompt_template']
        selected_model = prompt_row.get('selected_model') or 'gemini-1.5-pro'

        # 2. Fetch project metadata & prompt adjustments
        cursor.execute("""
            SELECT project_reference, project_description, project_objective, phase1_prompt_adjustments 
            FROM projects 
            WHERE id = %s;
        """, (project_id,))
        project = cursor.fetchone()
        if not project:
            raise ValueError(f"Project with ID {project_id} does not exist.")

        # 3. Format dynamic prompt
        formatted_prompt = base_prompt.format(
            project_reference=project.get('project_reference', ''),
            project_description=project.get('project_description', ''),
            project_objective=project.get('project_objective', ''),
            phase1_prompt_adjustments=project.get('phase1_prompt_adjustments') or 'None'
        )

        # 4. Invoke AI Model
        ai_response = call_gemini_api(model=selected_model, contents=[formatted_prompt])
        parsed_data = ai_response.get('parsed_content', {})

        # Key normalization for price weighting
        price_weighting = parsed_data.get('price_weight_percent')
        if price_weighting is None:
            price_weighting = parsed_data.get('price_weighting', 0)

        criteria_list = parsed_data.get('criteria', [])

        # 5. Database Writes
        cursor.execute("""
            UPDATE projects 
            SET price_weighting = %s 
            WHERE id = %s;
        """, (price_weighting, project_id))

        # Clear existing weightings before inserting new ones
        cursor.execute("DELETE FROM project_weightings WHERE project_id = %s;", (project_id,))
        
        for item in criteria_list:
            criteria_name = item.get('criteria_name') or item.get('component_name')
            
            # Handle weight_percent key extraction safely
            weight_val = item.get('weight_percent')
            if weight_val is None:
                weight_val = item.get('weighting', 0.0)

            if criteria_name and str(criteria_name).strip():
                cursor.execute("""
                    INSERT INTO project_weightings (project_id, criteria_name, weight_percent)
                    VALUES (%s, %s, %s);
                """, (project_id, str(criteria_name).strip(), weight_val))

    conn.commit()
    return {"status": "success", "project_id": project_id}


def process_vendor_quote_pricing(conn, vendor_record, project_id):
    """
    Phase 2: Vendor Quote & Evaluation Processing
    Evaluates individual vendor quotes against criteria (non-pricing) and extracts itemized pricing.
    """
    vendor_id = vendor_record['id']
    quote_bytes = vendor_record.get('quote_file_bytes')

    with conn.cursor() as cursor:
        # 1. Fetch system prompt for Phase 2
        cursor.execute("""
            SELECT prompt_template, selected_model 
            FROM system_prompts 
            WHERE process = 'project evaluation' AND is_active = true
            LIMIT 1;
        """)
        prompt_row = cursor.fetchone()
        if not prompt_row or not prompt_row.get('prompt_template'):
            raise ValueError("Active system prompt for 'project evaluation' not found.")
        
        base_prompt = prompt_row['prompt_template']
        selected_model = prompt_row.get('selected_model') or 'gemini-1.5-pro'

        # 2. Fetch project adjustments and defined weightings from Phase 1
        cursor.execute("""
            SELECT ai_prompt_adjustments 
            FROM projects 
            WHERE id = %s;
        """, (project_id,))
        project = cursor.fetchone()
        
        cursor.execute("""
            SELECT id, component_name, weight_percent 
            FROM project_weightings 
            WHERE project_id = %s;
        """, (project_id,))
        weightings = cursor.fetchall()

        # 3. Construct Payload with PDF Attachment
        gemini_contents = []
        if quote_bytes:
            doc_part = types.Part.from_bytes(data=bytes(quote_bytes), mime_type="application/pdf")
            gemini_contents.append(doc_part)

        formatted_prompt = base_prompt.format(
            vendor_name=vendor_record.get('vendor_name', ''),
            ai_prompt_adjustments=project.get('ai_prompt_adjustments') or 'None',
            assessment_criteria=weightings
        )
        gemini_contents.append(formatted_prompt)

        # 4. Invoke AI Model
        ai_response = call_gemini_api(model=selected_model, contents=gemini_contents)
        parsed_data = ai_response.get('parsed_content', {})

        # 5. Database Writes: Non-Pricing Items
        cursor.execute("""
            DELETE FROM options_line_items_non_pricing 
            WHERE vendor_option_id = %s;
        """, (vendor_id,))
        
        for np_item in parsed_data.get('non_pricing_evaluations', []):
            cursor.execute("""
                INSERT INTO options_line_items_non_pricing 
                (vendor_option_id, weighting_id, score, justification)
                VALUES (%s, %s, %s, %s);
            """, (
                vendor_id, 
                np_item.get('weighting_id'), 
                np_item.get('score'), 
                np_item.get('justification', '')
            ))

        # 6. Database Writes: Pricing Line Items
        cursor.execute("""
            DELETE FROM options_line_items_pricing 
            WHERE vendor_option_id = %s;
        """, (vendor_id,))
        
        for p_item in parsed_data.get('pricing_line_items', []):
            cursor.execute("""
                INSERT INTO options_line_items_pricing 
                (vendor_option_id, description, cost_type, raw_amount, quantity, normalized_amount)
                VALUES (%s, %s, %s, %s, %s, %s);
            """, (
                vendor_id,
                p_item.get('description'),
                p_item.get('cost_type', 'One-Off'),
                p_item.get('raw_amount'),
                p_item.get('quantity', 1),
                p_item.get('normalized_amount')
            ))

    conn.commit()
    return {"status": "success", "vendor_id": vendor_id}

def reset_project_matrix(cursor, project_id):
    """
    Deletes all existing pricing and non-pricing line items for a project
    so the AI Drafter can rewrite everything from scratch.
    """
    cursor.execute("""
        DELETE FROM options_line_items_pricing 
        WHERE procurement_option_id IN (
            SELECT id FROM procurement_options WHERE project_id = %s
        );
    """, (project_id,))

    cursor.execute("""
        DELETE FROM options_line_items_non_pricing 
        WHERE procurement_option_id IN (
            SELECT id FROM procurement_options WHERE project_id = %s
        );
    """, (project_id,))

if __name__ == "__main__":
    p_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    execute_phase1(p_id)