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

# vendor_comparison_engine_postgres.py

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

            # 3. Fetch project weightings (non-pricing criteria)
            cursor.execute("SELECT * FROM project_weightings WHERE project_id = %s;", (project_id,))
            weightings = cursor.fetchall()

            # 4. Fetch dynamic system prompt template from system_prompts table
            cursor.execute("""
                SELECT prompt_template, selected_model 
                FROM system_prompts 
                WHERE process = %s AND is_active = true
                LIMIT 1;
            """, ('project evaluation',))
            prompt_row = cursor.fetchone()

        if not prompt_row:
            raise ValueError("No active system prompt found in system_prompts where process = 'project evaluation'.")

        prompt_template = prompt_row['prompt_template'] if isinstance(prompt_row, dict) else prompt_row[0]
        selected_model = (prompt_row['selected_model'] if isinstance(prompt_row, dict) else prompt_row[1]) or 'gemini-3.5-flash'

        # Format assessment criteria array into formatted JSON text for insertion into prompt
        criteria_list = [
            {
                "id": w["id"],
                "criterion_name": w["criterion_name"],
                "description": w.get("description", ""),
                "weight_percent": float(w.get("weight_percent", 0.0))
            }
            for w in weightings
        ]
        assessment_criteria_str = json.dumps(criteria_list, indent=2)
        ai_adjustments_str = str(project.get("ai_prompt_adjustments") or "None")

        # -------------------------------------------------------------------------
        # STEP 1: GEMINI EVALUATION & POPULATION OF BOTH PRICING & NON-PRICING TABLES
        # -------------------------------------------------------------------------
        for v in vendors:
            v_id = v["id"]
            v_name = v.get("vendor_name", "Unknown Vendor")

            content_part = build_vendor_payload_part(v)
            if not content_part:
                continue

            # Populate placeholder parameters into the fetched prompt template
            formatted_prompt = prompt_template.format(
                vendor_name=v_name,
                ai_prompt_adjustments=ai_adjustments_str,
                assessment_criteria=assessment_criteria_str
            )

            try:
                res = ai_client.models.generate_content(
                    model=selected_model,
                    contents=[content_part, formatted_prompt],
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                data = json.loads(res.text or "{}")

                pricing_items = data.get("pricing_line_items", [])
                non_pricing_evals = data.get("non_pricing_evaluations", [])

                with conn.cursor() as cursor:
                    # Clear previous entries for vendor to prevent duplication
                    cursor.execute("DELETE FROM options_line_items_pricing WHERE procurement_option_id = %s;", (v_id,))
                    cursor.execute("DELETE FROM options_line_items_non_pricing WHERE procurement_option_id = %s;", (v_id,))

                    # Populating options_line_items_pricing
                    for p_item in pricing_items:
                        cursor.execute("""
                            INSERT INTO options_line_items_pricing 
                            (procurement_option_id, cost_component_name, cost_type_category, amount)
                            VALUES (%s, %s, %s, %s);
                        """, (
                            v_id,
                            p_item.get("cost_component_name", "Uncategorized Item"),
                            p_item.get("cost_type_category", "One-Off Cost"),
                            float(p_item.get("amount", 0.0))
                        ))

                    # Populating options_line_items_non_pricing
                    for np_item in non_pricing_evals:
                        weighting_id = np_item.get("weighting_id")
                        if weighting_id:
                            score = float(np_item.get("score", 0.0))
                            justification = np_item.get("justification", "")
                            line_item_id = f"np_{weighting_id}_{v_id}"

                            cursor.execute("""
                                INSERT INTO options_line_items_non_pricing 
                                (line_item_id, procurement_option_id, weighting_id, score, justification, weighted_score_contribution)
                                VALUES (%s, %s, %s, %s, %s, %s);
                            """, (
                                line_item_id,
                                v_id,
                                weighting_id,
                                score,
                                justification,
                                0.0
                            ))

                conn.commit()

            except Exception as e:
                print(f"Error executing Gemini evaluation for vendor '{v_name}' (ID: {v_id}): {e}")

        # -------------------------------------------------------------------------
        # STEP 2: CALCULATE 5-YEAR TOTALS PER VENDOR
        # -------------------------------------------------------------------------
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
        price_weight = float(project.get('price_weighting') or 0.50)

        # -------------------------------------------------------------------------
        # STEP 3: SCORE NON-PRICING AND CALCULATE COMBINED WEIGHTS
        # -------------------------------------------------------------------------
        winning_score = -1.0
        winner_name = ""

        with conn.cursor() as cursor:
            for v in vendors:
                v_cost = vendor_totals[v['id']]
                p_score = round(10.0 * (lowest_bid / v_cost), 2) if v_cost > 0 else 0.0
                weighted_p_score = p_score * price_weight

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
                    
                    w_factor = weight_pct / 100.0 if weight_pct > 1 else weight_pct
                    contrib = round(raw_score * w_factor, 2)
                    total_np_score += contrib

                    cursor.execute("""
                        UPDATE options_line_items_non_pricing
                        SET weighted_score_contribution = %s
                        WHERE id = %s;
                    """, (contrib, item['line_item_id']))

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

        # -------------------------------------------------------------------------
        # STEP 4: OSINT DUE DILIGENCE & PROJECT MATRIX FLAGS
        # -------------------------------------------------------------------------
        run_due_diligence_osint(conn, project_id, vendors)

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


def generate_executive_recommendation_html(conn, project_id: int):
    """
    Fetches evaluation results and generates a standalone Executive Recommendation HTML document.
    """
    with conn.cursor() as cursor:
        # 1. Fetch project details
        cursor.execute("SELECT * FROM projects WHERE id = %s;", (project_id,))
        project = cursor.fetchone()
        if not project:
            raise ValueError(f"Project ID {project_id} not found.")

        # 2. Fetch ranked procurement options
        cursor.execute("""
            SELECT * FROM procurement_options 
            WHERE project_id = %s 
            ORDER BY final_weighted_score_output DESC NULLS LAST;
        """, (project_id,))
        vendors = cursor.fetchall()

        # 3. Fetch non-pricing criteria breakdown
        cursor.execute("""
            SELECT np.procurement_option_id, pw.criterion_name, np.score, np.justification, np.weighted_score_contribution
            FROM options_line_items_non_pricing np
            JOIN project_weightings pw ON np.weighting_id = pw.id
            WHERE pw.project_id = %s;
        """, (project_id,))
        non_pricing_scores = cursor.fetchall()

    winner = vendors[0] if vendors else None
    winner_name = winner.get("vendor_name", "N/A") if winner else "N/A"
    winning_score = winner.get("final_weighted_score_output", 0.0) if winner else 0.0
    lowest_bid = float(project.get("lowest_project_bid_floor") or 0.0)

    # Construct HTML Table Rows for Vendors
    vendor_rows_html = ""
    for rank, v in enumerate(vendors, start=1):
        vendor_rows_html += f"""
        <tr>
            <td><strong>#{rank} {v.get('vendor_name')}</strong></td>
            <td>R{float(v.get('projected_5yr_total') or 0.0):,.2f}</td>
            <td>{float(v.get('price_score') or 0.0):.2f}</td>
            <td>{float(v.get('total_non_pricing_score') or 0.0):.2f}</td>
            <td><strong>{float(v.get('final_weighted_score_output') or 0.0):.2f}</strong></td>
            <td><span class="badge {v.get('public_dd_status', '').lower()}">{v.get('public_dd_status', 'N/A')}</span></td>
        </tr>
        """

    # Assemble complete standalone HTML
    recommendation_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Executive Recommendation - {project.get('name', 'Project')}</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 40px; background-color: #f8fafc; color: #1e293b; }}
        .container {{ max-width: 1000px; margin: 0 auto; background: #ffffff; padding: 40px; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }}
        h1 {{ color: #0f172a; border-bottom: 2px solid #e2e8f0; padding-bottom: 12px; margin-top: 0; }}
        .winner-card {{ background: linear-gradient(135deg, #ecfdf5 0%, #d1fae5 100%); border: 1px solid #a7f3d0; padding: 24px; border-radius: 8px; margin: 24px 0; }}
        .winner-title {{ color: #065f46; font-size: 1.25rem; font-weight: bold; margin-bottom: 8px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 24px; }}
        th, td {{ border: 1px solid #cbd5e1; padding: 12px; text-align: left; }}
        th {{ background-color: #f1f5f9; font-weight: 600; color: #334155; }}
        .badge {{ padding: 4px 8px; border-radius: 4px; font-weight: 600; font-size: 0.85rem; }}
        .badge.passed {{ background-color: #dcfce7; color: #166534; }}
        .badge.caution {{ background-color: #fef9c3; color: #854d0e; }}
        .badge.high {{ background-color: #fee2e2; color: #991b1b; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Executive Procurement Recommendation</h1>
        <p><strong>Project:</strong> {project.get('name')}</p>
        
        <div class="winner-card">
            <div class="winner-title">Top Recommended Vendor: {winner_name}</div>
            <p><strong>Final Weighted Score:</strong> {winning_score:.2f} / 10.00</p>
            <p><strong>Lowest 5-Year Cost Floor:</strong> R{lowest_bid:,.2f}</p>
        </div>

        <h3>Vendor Comparison Summary</h3>
        <table>
            <thead>
                <tr>
                    <th>Vendor</th>
                    <th>5-Year Cost</th>
                    <th>Price Score</th>
                    <th>Non-Pricing Score</th>
                    <th>Final Weighted Score</th>
                    <th>Due Diligence</th>
                </tr>
            </thead>
            <tbody>
                {vendor_rows_html}
            </tbody>
        </table>
    </div>
</body>
</html>"""

    # 4. Save HTML output directly to database
    with conn.cursor() as cursor:
        cursor.execute("""
            UPDATE projects 
            SET executive_recommendation_html = %s 
            WHERE id = %s;
        """, (recommendation_html, project_id))
    conn.commit()

    return recommendation_html

if __name__ == "__main__":
    p_id = (int(sys.argv[1]),) if len(sys.argv) > 1 else (1,)
    execute_phase2(p_id)