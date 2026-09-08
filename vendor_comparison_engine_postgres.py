import os
import sys
import json
import re
import psycopg2
from psycopg2.extras import RealDictCursor
from google import genai
from google.genai import types
from duckduckgo_search import DDGS


GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

ai_client = genai.Client(api_key=GEMINI_KEY)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def build_vendor_payload_part(vendor: dict) -> types.Part | None:
    """
    Selects HTML analysis text over PDF raw bytes if available.
    """
    analysis_html = vendor.get("analysis_sheet_html")
    quote_bytes = vendor.get("quote_file_bytes")

    if analysis_html and str(analysis_html).strip():
        # Pass HTML analysis document as text content
        return types.Part.from_bytes(
            data=str(analysis_html).encode("utf-8"),
            mime_type="text/html",
        )
    elif quote_bytes:
        # Fallback to binary PDF quote
        raw_data = bytes(quote_bytes) if not isinstance(quote_bytes, bytes) else quote_bytes
        return types.Part.from_bytes(
            data=raw_data,
            mime_type="application/pdf",
        )
    
    return None

def process_vendor_quote_pricing(project_id: int, vendor_options: list[dict], db_connection=None):
    """
    Phase 2b Evaluation Engine with Conditional Vendor Content Payload.
    """
    contents = []

    for vendor in vendor_options:
        content_part = build_vendor_payload_part(vendor)
        if not content_part:
            continue

        # Construct vendor block for Gemini Evaluation
        vendor_info_text = f"Vendor ID: {vendor.get('id')}\nVendor Name: {vendor.get('vendor_name', 'Unknown')}\n"
        
        contents.append(vendor_info_text)
        contents.append(content_part)

    if not contents:
        raise ValueError("No valid PDF or HTML analysis documents found for Phase 2b evaluation.")

    # Proceed with model evaluation call
    res = ai_client.models.generate_content(
        model='gemini-3.5-flash-lite',
        contents=contents
    )
    return res.text or ""

def run_due_diligence_osint(conn, project_id, vendors):
    for v in vendors:
        v_id = v['id']
        v_name = v['vendor_name']

        legal_name = v_name
        cipc_num = "N/A"
        vat_num = "N/A"

        doc_part = build_vendor_payload_part(v)
        if doc_part:
            parse_prompt = "Extract legal_name, cipc_number, vat_number from document as JSON."
            try:
                res = ai_client.models.generate_content(
                    model='gemini-3.5-flash-lite',
                    contents=[doc_part, parse_prompt],
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                meta = json.loads(res.text or "{}")
                legal_name = meta.get("legal_name") or v_name
                cipc_num = meta.get("cipc_number") or "N/A"
                vat_num = meta.get("vat_number") or "N/A"
            except Exception:
                pass

        # DuckDuckGo OSINT search
        search_context = ""
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(f"{legal_name} {cipc_num} South Africa risk compliance", max_results=4))
                for r in results:
                    search_context += f"- Title: {r.get('title')}\n  Snippet: {r.get('body')}\n"
        except Exception:
            search_context = "No direct web search records returned."

        dd_prompt = f"""
        Audit vendor '{legal_name}' (CIPC: {cipc_num}, VAT: {vat_num}) in South Africa.
        Context: {search_context}
        Evaluate operational footprint, statutory registrations, and compliance risks.
        End response with 'DD_STATUS: Passed', 'DD_STATUS: Caution', or 'DD_STATUS: High Risk'.
        """

        dd_res = ai_client.models.generate_content(model='gemini-3.5-flash-lite', contents=[dd_prompt])
        raw_text = (dd_res.text or "").strip()
        
        status_match = re.search(r'DD_STATUS:\s*(Passed|Caution|High Risk)', raw_text, re.IGNORECASE)
        v_status = status_match.group(1).title() if status_match else "Caution"
        findings = re.sub(r'DD_STATUS:.*', '', raw_text, flags=re.IGNORECASE).strip()

        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE procurement_options 
                SET public_dd_status = %s, public_search_findings = %s 
                WHERE id = %s;
            """, (v_status, findings, v_id))
        conn.commit()

def execute_phase2(conn, project_id=None):
    # Handle single positional argument calls: execute_phase2(project_id)
    if project_id is None:
        project_id = conn
        conn = None
        should_close_conn = True
    else:
        should_close_conn = False

    try:
        if conn is None:
            conn = get_db_connection()

        with conn.cursor() as cursor:
            # 1. Fetch target project record
            cursor.execute("SELECT * FROM projects WHERE id = %s;", (project_id,))
            project = cursor.fetchone()
            if not project:
                raise ValueError(f"Project with ID {project_id} not found.")

            # 2. Fetch associated vendors/options for this project
            cursor.execute("SELECT * FROM procurement_options WHERE project_id = %s;", (project_id,))
            vendors = cursor.fetchall()
            if not vendors:
                return {"status": "warning", "message": f"No procurement options found for project {project_id}."}

            # 3. Fetch project weightings
            cursor.execute("SELECT * FROM project_weightings WHERE project_id = %s;", (project_id,))
            weightings = cursor.fetchall()

            # -------------------------------------------------------------------------
            # STEP 2b PRE-PROCESSING: Parse quotes & populate line items per vendor
            # -------------------------------------------------------------------------
            for v in vendors:
                # Parse quote/analysis sheet data and populate pricing/non-pricing line items
                process_vendor_quote_pricing(project_id, vendors, conn)

        # 1. Update 5-Year Totals per Vendor
        vendor_totals = {}
        with conn.cursor() as cursor:
            for v in vendors:
                cursor.execute("""
                    SELECT amount, cost_type_category FROM options_line_items_pricing 
                    WHERE procurement_option_id = %s;
                """, (v['id'],))
                lines = cursor.fetchall()
                
                total_5yr = sum(
                    (float(l['amount']) * 5 if l['cost_type_category'] == 'Annual Cost' else float(l['amount']))
                    for l in lines
                )
                vendor_totals[v['id']] = total_5yr
                
                cursor.execute("""
                    UPDATE procurement_options SET projected_5yr_total = %s WHERE id = %s;
                """, (total_5yr, v['id']))
        conn.commit()

        lowest_bid = min(vendor_totals.values()) if vendor_totals else 0.0
        price_weight = float(project['price_weighting'] or 0.50)

        # 2. Score Non-Pricing and Combined Weights
        winning_score = -1.0
        winner_name = ""

        with conn.cursor() as cursor:
            for v in vendors:
                v_cost = vendor_totals[v['id']]
                p_score = round(10.0 * (lowest_bid / v_cost), 2) if v_cost > 0 else 0.0
                weighted_p_score = p_score * price_weight

                # Join to fetch id for updating, raw score, and criteria weight
                cursor.execute("""
                    SELECT 
                        np.id AS line_item_id,
                        np.score, 
                        pw.weight_percent 
                    FROM options_line_items_non_pricing np
                    JOIN project_weightings pw ON np.weighting_id = pw.id
                    WHERE np.procurement_option_id = %s;
                """, (v['id'],))
                np_items = cursor.fetchall()

                total_np_score = 0.0
                for item in np_items:
                    raw_score = float(item['score'] or 0.0)
                    weight_pct = float(item['weight_percent'] or 0.0)
                    
                    # Normalize percentage if stored as 0-100 instead of 0-1
                    w_factor = weight_pct / 100.0 if weight_pct > 1 else weight_pct
                    
                    # Calculate contribution for this specific non-pricing criterion
                    contrib = round(raw_score * w_factor, 2)
                    total_np_score += contrib

                    # Update weighted_score_contribution directly in options_line_items_non_pricing
                    cursor.execute("""
                        UPDATE options_line_items_non_pricing
                        SET weighted_score_contribution = %s
                        WHERE id = %s;
                    """, (contrib, item['line_item_id']))

                    print(f"Updated rows: {cursor.rowcount}")  # Should be 1, NOT 0

                final_weighted_score = round(weighted_p_score + total_np_score, 2)

                if final_weighted_score > winning_score:
                    winning_score = final_weighted_score
                    winner_name = v['vendor_name']

                cursor.execute("""
                    UPDATE procurement_options 
                    SET lowest_bid_lookup = %s,
                        price_score = %s,
                        total_non_pricing_score = %s,
                        final_weighted_score_output = %s
                    WHERE id = %s;
                """, (lowest_bid, p_score, total_np_score, final_weighted_score, v['id']))
        conn.commit()

        # 3. OSINT Due Diligence
        run_due_diligence_osint(conn, project_id, vendors)

       # 4. Update lowest bid floor and clear recalculate flag
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE projects 
                SET lowest_project_bid_floor = %s,
                    recalculate_matrix = FALSE
                WHERE id = %s;
            """, (lowest_bid, project_id))
        conn.commit()

    except Exception as e:
        if conn and not conn.closed:
            conn.rollback()
        raise e

    finally:
        if should_close_conn and conn and not conn.closed:
            conn.close()

if __name__ == "__main__":
    p_id = (int(sys.argv[1]),) if len(sys.argv) > 1 else (1,)
    execute_phase2(p_id)