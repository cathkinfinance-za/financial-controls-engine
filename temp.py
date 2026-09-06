NEW

import os
import sys
import json
import time
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from google import genai
from google.genai import types

GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

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

def call_gemini_api(model: str, contents: list) -> dict:
    """
    Helper function to invoke Gemini model and return robustly parsed JSON output.
    """
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )
    
    raw_text = response.text or ""
    
    # Strip markdown code fencing if returned by API
    clean_text = raw_text.strip()
    if clean_text.startswith("```json"):
        clean_text = clean_text[7:]
    if clean_text.startswith("```"):
        clean_text = clean_text[3:]
    if clean_text.endswith("```"):
        clean_text = clean_text[:-3]
    clean_text = clean_text.strip()

    try:
        parsed_content = json.loads(clean_text)
    except json.JSONDecodeError:
        parsed_content = {}
        
    return {"parsed_content": parsed_content, "raw_text": raw_text}

def execute_phase1(conn, project_id):
    """
    Phase 1: Assessment Criteria Formulation
    Retrieves system prompt & adjustments, runs Gemini to define project weights,
    and updates 'projects' and 'project_weightings'.
    """
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT prompt_template, selected_model 
            FROM system_prompts 
            WHERE LOWER(process) = LOWER('project matrix drafter') AND is_active = true
            LIMIT 1;
        """)
        prompt_row = cursor.fetchone()
        if not prompt_row or not prompt_row.get('prompt_template'):
            raise ValueError("Active system prompt for 'project matrix drafter' not found.")
        
        base_prompt = prompt_row['prompt_template']
        selected_model = prompt_row.get('selected_model') or 'gemini-2.5-flash'

        cursor.execute("""
            SELECT project_reference, project_description, project_objective, phase1_prompt_adjustments 
            FROM projects 
            WHERE id = %s;
        """, (project_id,))
        project = cursor.fetchone()
        if not project:
            raise ValueError(f"Project with ID {project_id} does not exist.")

        formatted_prompt = base_prompt.format(
            project_reference=project.get('project_reference', ''),
            project_description=project.get('project_description', ''),
            project_objective=project.get('project_objective', ''),
            phase1_prompt_adjustments=project.get('phase1_prompt_adjustments') or 'None'
        )

        ai_response = call_gemini_api(model=selected_model, contents=[formatted_prompt])
        parsed_data = ai_response.get('parsed_content', {})

        # Flexible key extraction for price weighting across diverse prompt formats
        price_weighting = (
            parsed_data.get('price_weight_percent') 
            or parsed_data.get('price_weighting') 
            or parsed_data.get('price_weight') 
            or 30
        )

        try:
            price_weighting = float(price_weighting)
            if price_weighting > 1.0:
                price_weighting = price_weighting / 100.0
        except (ValueError, TypeError):
            price_weighting = 0.30

        criteria_list = parsed_data.get('criteria', [])

        cursor.execute("""
            UPDATE projects 
            SET price_weighting = %s 
            WHERE id = %s;
        """, (price_weighting, project_id))

        cursor.execute("DELETE FROM project_weightings WHERE project_id = %s;", (project_id,))
        
        for item in criteria_list:
            criteria_name = item.get('criteria_name') or item.get('component_name')
            
            weight_val = item.get('weight_percent')
            if weight_val is None:
                weight_val = item.get('weighting', 0.0)

            if criteria_name and str(criteria_name).strip():
                cursor.execute("""
                    INSERT INTO project_weightings (project_id, criteria_name, weight_percent)
                    VALUES (%s, %s, %s);
                """, (project_id, str(criteria_name).strip(), float(weight_val)))

    conn.commit()
    return {"status": "success", "project_id": project_id}


def process_vendor_quote_pricing(conn, vendor_record, project_id):
    """
    Phase 2: Vendor Quote & Evaluation Processing
    """
    vendor_id = vendor_record['id']
    quote_bytes = vendor_record.get('quote_file_bytes')

    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT prompt_template, selected_model 
            FROM system_prompts 
            WHERE LOWER(process) = LOWER('project evaluation') AND is_active = true
            LIMIT 1;
        """)
        prompt_row = cursor.fetchone()
        if not prompt_row or not prompt_row.get('prompt_template'):
            raise ValueError("Active system prompt for 'project evaluation' not found.")
        
        base_prompt = prompt_row['prompt_template']
        selected_model = prompt_row.get('selected_model') or 'gemini-2.5-flash'

        cursor.execute("""
            SELECT ai_prompt_adjustments 
            FROM projects 
            WHERE id = %s;
        """, (project_id,))
        project = cursor.fetchone()
        
        cursor.execute("""
            SELECT id, criteria_name, weight_percent 
            FROM project_weightings 
            WHERE project_id = %s;
        """, (project_id,))
        weightings = cursor.fetchall()

        gemini_contents = []
        if quote_bytes:
            doc_part = types.Part.from_bytes(data=bytes(quote_bytes), mime_type="application/pdf")
            gemini_contents.append(doc_part)

        formatted_prompt = base_prompt.format(
            vendor_name=vendor_record.get('vendor_name', ''),
            ai_prompt_adjustments=project.get('ai_prompt_adjustments') or 'None',
            assessment_criteria=json.dumps(weightings, default=str)
        )
        gemini_contents.append(formatted_prompt)

        ai_response = call_gemini_api(model=selected_model, contents=gemini_contents)
        parsed_data = ai_response.get('parsed_content', {})

        cursor.execute("""
            DELETE FROM options_line_items_non_pricing 
            WHERE procurement_option_id = %s;
        """, (vendor_id,))
        
        for np_item in parsed_data.get('non_pricing_evaluations', []):
            weighting_id = np_item.get('weighting_id')
            score = float(np_item.get('score', 0.0))
            line_item_id = f"np_{weighting_id}_{vendor_id}"
            
            cursor.execute("""
                INSERT INTO options_line_items_non_pricing 
                (line_item_id, procurement_option_id, weighting_id, score, weighted_score_contribution)
                VALUES (%s, %s, %s, %s, %s);
            """, (line_item_id, vendor_id, weighting_id, score, 0.0))

    conn.commit()
    return {"status": "success", "vendor_id": vendor_id}