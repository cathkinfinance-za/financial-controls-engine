import os
import sys
import json
import re
import psycopg2
import markdown
from psycopg2.extras import RealDictCursor
from google import genai
from google.genai import types
from duckduckgo_search import DDGS
from flask import url_for


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
    Generates a standalone, self-contained HTML report for executive recommendation.
    """
    with conn.cursor() as cursor:
        # 1. Fetch Project Details
        cursor.execute("SELECT * FROM projects WHERE id = %s;", (project_id,))
        project = cursor.fetchone()
        if not project:
            return None

        # 2. Fetch Procurement Options / Vendors
        cursor.execute("SELECT * FROM procurement_options WHERE project_id = %s ORDER BY id ASC;", (project_id,))
        vendors = cursor.fetchall()

        # 3. Fetch Qualitative Criteria
        cursor.execute("SELECT * FROM project_weightings WHERE project_id = %s ORDER BY id ASC;", (project_id,))
        criteria_list = cursor.fetchall()

        # 4. Fetch Non-Pricing Scores & Justifications
        cursor.execute("""
            SELECT line_item_id, procurement_option_id, weighting_id, score, justification
            FROM options_line_items_non_pricing 
            WHERE procurement_option_id IN (SELECT id FROM procurement_options WHERE project_id = %s);
        """, (project_id,))
        scores_raw = cursor.fetchall()

        # 5. Fetch Pricing Line Items to Check for "Annual Cost" categories
        cursor.execute("""
            SELECT cost_type_category 
            FROM options_line_items_pricing 
            WHERE procurement_option_id IN (SELECT id FROM procurement_options WHERE project_id = %s);
        """, (project_id,))
        pricing_items = cursor.fetchall()

        # Check if any pricing item has the "Annual Cost" category
        has_annual_costs = any(item.get("cost_type_category") == "Annual Cost" for item in pricing_items)

        scores_map = {}
        for row in scores_raw:
            w_id = row["weighting_id"]
            v_id = row["procurement_option_id"]
            if w_id not in scores_map:
                scores_map[w_id] = {}
            scores_map[w_id][v_id] = row

    # Define dynamic labels based on presence of Annual Costs
    total_cost_label = "Projected 5-Year Total Cost" if has_annual_costs else "Total Cost"
    short_cost_label = "Projected Total" if has_annual_costs else "Total Cost"

    # Format Executive Recommendation Markdown to HTML
    rec_markdown = project.get("executive_sourcing_recommendation") or "No recommendation generated."
    rec_html_content = markdown.markdown(rec_markdown, extensions=['tables', 'fenced_code'])
    
    # Build Header Columns for Matrix Table
    vendor_headers_html = "".join([f'<th class="text-right">{v["vendor_name"]}</th>' for v in vendors])

    # Build Price Evaluation Row
    pw = float(project.get("price_weighting", 0.3) or 0.3)
    price_weight_pct = pw * 100.0 if pw <= 1.0 else pw

    price_cells_html = ""
    for v in vendors:
        score = float(v.get("price_score") or 0.0)
        rate = float(v.get("total_effective_rate") or 0.0)
        price_cells_html += (
            f'<td class="text-right">'
            f'<div class="score-value">{score:.2f}</div>'
            f'<div class="text-muted">ZAR {rate:,.2f} per unit</div>'
            f'</td>'
        )

    # Build Qualitative Criteria Rows
    qualitative_rows_html = ""
    total_criteria_weight = price_weight_pct

    for criteria in criteria_list:
        c_weight = float(criteria.get("weight_percent") or 0.0)
        total_criteria_weight += c_weight
        
        vendor_score_cells = ""
        for v in vendors:
            score_entry = scores_map.get(criteria["id"], {}).get(v["id"], {})
            score_val = score_entry.get("score")
            score_display = f"{float(score_val):.2f}" if score_val is not None else "0.00"
            justification = score_entry.get("justification", "")
            
            tooltip_html = ""
            if justification:
                tooltip_html = (
                    f'<span class="info-tooltip">ⓘ'
                    f'<span class="tooltip-text"><strong>AI Justification:</strong><br>{justification}</span>'
                    f'</span>'
                )

            vendor_score_cells += f'<td class="text-right"><span class="score-value">{score_display}</span>{tooltip_html}</td>'

        c_name = criteria.get("criteria_name", "")
        c_cat = criteria.get("category", "Qualitative")
        qualitative_rows_html += (
            f'<tr>'
            f'<td>{c_name}</td>'
            f'<td>{c_cat}</td>'
            f'<td class="text-right">{c_weight:.2f}%</td>'
            f'{vendor_score_cells}'
            f'</tr>'
        )

    # Matrix Summary Rows
    final_scores_cells = "".join([f'<td class="text-right highlight-score">{(v.get("final_weighted_score_output") or 0.0):.2f}</td>' for v in vendors])
    quantity_cells = "".join([f'<td class="text-right">{float(v.get("total_quantity") or 1.0):,.2f} {v.get("unit_of_measure") or "m2"}</td>' for v in vendors])
    unit_rate_cells = "".join([f'<td class="text-right">ZAR {(v.get("total_effective_rate") or 0.0):,.2f}</td>' for v in vendors])
    total_cost_cells = "".join([f'<td class="text-right">ZAR {(v.get("projected_5yr_cost") or v.get("projected_5yr_total") or 0.0):,.2f}</td>' for v in vendors])

    # Build Procurement Cards HTML
    procurement_cards_html = ""
    for v in vendors:
        # Safely parse cost numeric values
        cost = float(v.get("projected_5yr_cost") or v.get("projected_5yr_total") or 0.0)
        filename = v.get("quote_filename") or "Quote.pdf"
        
        # 1. Analysis Link Setup
        if v.get("analysis_sheet_html"):
            analysis_url = url_for('analysis.view_analysis', option_id=v['id'])
            analysis_link_html = f'''
            <a href="{analysis_url}" 
            target="_blank" 
            rel="noopener noreferrer"
            style="display: inline-flex; align-items: center; gap: 4px; font-size: 13px; font-weight: 600; color: #2563eb; text-decoration: none;">
                <span>View Analysis</span>
                <svg style="width: 14px; height: 14px;" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                </svg>
            </a>
            '''
        else:
            analysis_link_html = '<span style="font-size: 12px; color: #6b7280; font-style: italic;">No analysis sheet</span>'

        # 2. Quote PDF URL (Matches /view-quote/<vendor_id>)
        quote_file_url = f"/view-quote/{v['id']}"

        # 3. Card HTML Generation (PDF icon removed, text converted to working link)
        procurement_cards_html += f'''
        <div class="vendor-card" style="display: flex; align-items: center; justify-content: space-between; padding: 12px 16px;">
            <div>
                <strong style="font-size: 16px; display: block; margin-bottom: 6px;">{v["vendor_name"]}</strong>
                <div class="vendor-info" style="display: flex; gap: 12px; align-items: center;">
                    <span style="font-size: 13px;">Projected Total: <strong>ZAR {cost:,.2f}</strong></span>
                    <span>•</span>
                    {analysis_link_html}
                </div>
            </div>
            <div>
                <a href="{quote_file_url}" 
                target="_blank" 
                rel="noopener noreferrer" 
                style="font-size: 13px; color: #4b5563; text-decoration: underline; font-weight: 500;">
                    {filename}
                </a>
            </div>
        </div>
        '''

    if not procurement_cards_html:
        procurement_cards_html = '<div class="text-muted">No procurement options recorded.</div>'

    # CSS Stylesheet String
    css_styles = """
    :root { --bg-color: #f8f9fa; --border-color: #e9ecef; --text-main: #212529; --text-muted: #6c757d; --primary-blue: #0d6efd; --card-bg: #ffffff; } 
    body { font-family: system-ui, -apple-system, sans-serif; background-color: var(--bg-color); color: var(--text-main); margin: 0; padding: 40px 20px; } 
    .dashboard-container { max-width: 1100px; margin: 0 auto; background-color: var(--card-bg); border: 1px solid var(--border-color); border-radius: 8px; padding: 40px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); } 
    .section-header { font-size: 20px; font-weight: 700; margin-top: 36px; margin-bottom: 20px; border-bottom: 2px solid var(--border-color); padding-bottom: 8px; color: #1e293b; } 
    .form-row { display: grid; grid-template-columns: 220px 1fr; align-items: start; margin-bottom: 18px; } 
    .form-label { font-weight: 600; font-size: 14px; padding-top: 4px; } 
    .form-value { font-size: 14px; background: #fafafa; padding: 8px 12px; border-radius: 4px; border: 1px solid #f1f5f9; } 
    .vendor-card { background-color: #fdfdfd; border: 1px solid var(--border-color); border-radius: 6px; padding: 16px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; } 
    .vendor-info { display: grid; grid-template-columns: 180px 1fr; gap: 16px; margin-top: 4px; } 
    .pdf-icon { width: 32px; height: 38px; background-color: #dc3545; color: white; border-radius: 3px; display: flex; align-items: center; justify-content: center; font-size: 10px; font-weight: bold; } 
    .table-responsive { width: 100%; overflow-x: auto; margin: 12px 0 20px 0; } 
    table.matrix-table { width: 100%; border-collapse: collapse; } 
    table.matrix-table th, table.matrix-table td { border: 1px solid var(--border-color); padding: 12px 14px; font-size: 14px; } 
    table.matrix-table th { background-color: #f8f9fa; font-weight: 600; } 
    .text-right { text-align: right !important; } 
    .score-value { font-weight: bold; font-size: 15px; } 
    .highlight-score { font-size: 16px; color: var(--primary-blue); font-weight: bold; } 
    .info-tooltip { position: relative; display: inline-block; cursor: help; margin-left: 6px; color: var(--primary-blue); } 
    .info-tooltip .tooltip-text { visibility: hidden; width: 240px; background-color: #212529; color: #fff; border-radius: 4px; padding: 10px; position: absolute; z-index: 100; bottom: 125%; left: 50%; transform: translateX(-50%); opacity: 0; transition: opacity 0.2s; font-size: 11px; font-weight: normal; } 
    .info-tooltip:hover .tooltip-text { visibility: visible; opacity: 1; } 
    .recommendation-box { background-color: #f8fafc; border: 1px solid #cbd5e1; border-left: 5px solid var(--primary-blue); border-radius: 6px; padding: 24px; margin-top: 12px; line-height: 1.6; font-size: 15px; } 
    .text-muted { color: var(--text-muted); }
    """

    # Assemble HTML Output
    full_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Recommendation (AI Generated) - {project.get("name", "Project")}</title>
    <style>{css_styles}</style>
</head>
<body>
    <div class="dashboard-container">
        <h1>Recommendation (AI Generated)</h1>
        
        <div class="section-header">Project Definitions</div>
        <div class="form-row"><div class="form-label">Project Reference</div><div class="form-value">{project.get("project_reference") or "N/A"}</div></div>
        <div class="form-row"><div class="form-label">Name</div><div class="form-value">{project.get("name") or "N/A"}</div></div>
        <div class="form-row"><div class="form-label">Project Description</div><div class="form-value">{project.get("project_description") or "N/A"}</div></div>
        <div class="form-row"><div class="form-label">Project Objective</div><div class="form-value">{project.get("project_objective") or "N/A"}</div></div>
        <div class="form-row"><div class="form-label">Phase 1 AI Prompt Adjustments</div><div class="form-value">{project.get("phase1_prompt_adjustments") or "None"}</div></div>
        <div class="form-row"><div class="form-label">Phase 2 Prompt Adjustments</div><div class="form-value">{project.get("ai_prompt_adjustments") or "None"}</div></div>
        <div class="form-row"><div class="form-label">GL Code</div><div class="form-value">{project.get("gl_code") or "N/A"}</div></div>
        
        <div class="section-header">Procurement Options</div>
        {procurement_cards_html}

        <div class="section-header">Qualitative & Pricing Criteria Matrix</div>
        <div class="table-responsive">
            <table class="matrix-table">
                <thead>
                    <tr>
                        <th>Evaluation Criteria / Line Item</th>
                        <th>Category</th>
                        <th class="text-right">Weight (%) / Amount</th>
                        {vendor_headers_html}
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td>Commercial / Price Evaluation</td>
                        <td>Relative inverse pricing</td>
                        <td class="text-right">{price_weight_pct:.2f}%</td>
                        {price_cells_html}
                    </tr>
                    {qualitative_rows_html}
                    <tr>
                        <td colspan="2"><strong>Final Weighted Score (/10)</strong></td>
                        <td class="text-right"><strong>{total_criteria_weight:.2f}%</strong></td>
                        {final_scores_cells}
                    </tr>
                    <tr>
                        <td colspan="3"><strong>Option Quantity & Unit</strong></td>
                        {quantity_cells}
                    </tr>
                    <tr>
                        <td colspan="3"><strong>Total Effective Unit Rate</strong></td>
                        {unit_rate_cells}
                    </tr>
                    <tr>
                        <td colspan="3"><strong>{total_cost_label}</strong></td>
                        {total_cost_cells}
                    </tr>
                </tbody>
            </table>
        </div>

        <div class="section-header">Executive Sourcing Recommendation</div>
        <div class="recommendation-box">
            {rec_html_content}
        </div>
    </div>
</body>
</html>'''

    # Save to Database
    with conn.cursor() as cursor:
        cursor.execute("""
            UPDATE projects 
            SET executive_recommendation_html = %s 
            WHERE id = %s;
        """, (full_html, project_id))
    conn.commit()

    return full_html

if __name__ == "__main__":
    p_id = (int(sys.argv[1]),) if len(sys.argv) > 1 else (1,)
    execute_phase2(p_id)