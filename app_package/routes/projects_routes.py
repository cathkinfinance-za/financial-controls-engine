import os
import io
import psycopg2
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from app_package.db import get_db_connection

# Import phase execution engines
from ai_matrix_drafter_postgres import execute_phase1
from vendor_comparison_engine_postgres import execute_phase2, generate_executive_recommendation_html

try:
    from google import genai
    GEMINI_AVAILABLE = True
except ModuleNotFoundError:
    genai = None
    GEMINI_AVAILABLE = False

projects_bp = Blueprint('projects', __name__)

def get_all_projects_summary(cursor):
    cursor.execute("SELECT id, project_reference, name FROM projects ORDER BY id DESC;")
    return cursor.fetchall()


@projects_bp.route("/projects", methods=["GET"])
@projects_bp.route("/projects/", methods=["GET"])
@projects_bp.route("/projects/<int:project_id>", methods=["GET"])
def projects_page(project_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()

    all_projects = get_all_projects_summary(cursor)

    cursor.execute("SELECT gl_code, description FROM master_budget ORDER BY gl_code")
    gl_records = cursor.fetchall()

    if not project_id:
        cursor.close()
        conn.close()
        return render_template(
            "projects.html", 
            project=None, 
            vendors=[], 
            criteria_list=[], 
            scores_map={}, 
            all_projects=all_projects,
            gl_records=gl_records
        )
    
    cursor.execute("SELECT * FROM projects WHERE id = %s;", (project_id,))
    project = cursor.fetchone()

    cursor.execute("SELECT * FROM procurement_options WHERE project_id = %s;", (project_id,))
    vendors = cursor.fetchall()

    cursor.execute("SELECT * FROM project_weightings WHERE project_id = %s ORDER BY id ASC;", (project_id,))
    criteria_list = cursor.fetchall()

    for vendor in vendors:
        cursor.execute("SELECT * FROM options_line_items_pricing WHERE procurement_option_id = %s ORDER BY id ASC;", (vendor["id"],))
        vendor["pricing_items"] = cursor.fetchall()
        total_cost = float(vendor.get('total_cost') or 0.0)
        quantity = float(vendor.get('total_quantity') or 1.0)
        if quantity <= 0:
            quantity = 1.0
        vendor['total_effective_rate'] = total_cost / quantity
        vendor['projected_5yr_total'] = vendor.get('total_effective_rate') or vendor.get('quote_total') or 0.0

    vendor_rates = {}

    for vendor in vendors:
        qty = float(vendor.get('total_quantity') or 1.0)
        if qty <= 0:
            qty = 1.0

        one_off = sum(float(item['amount']) for item in vendor['pricing_items'] if item['cost_type_category'] == 'One-Off Cost')
        annual = sum(float(item['amount']) for item in vendor['pricing_items'] if item['cost_type_category'] == 'Annual Cost')
        
        projected_5yr_cost = one_off + (annual * 5)
        effective_unit_rate = projected_5yr_cost / qty
        
        vendor['projected_5yr_cost'] = projected_5yr_cost
        vendor['total_effective_rate'] = effective_unit_rate
        vendor_rates[vendor['id']] = effective_unit_rate

    min_rate = min([r for r in vendor_rates.values() if r > 0], default=0)

    for vendor in vendors:
        rate = vendor_rates[vendor['id']]
        if rate > 0 and min_rate > 0:
            price_score = (min_rate / rate) * 10.0
        else:
            price_score = 0.0
            
        vendor['price_score'] = round(price_score, 2)
        
        cursor.execute("""
            UPDATE procurement_options 
            SET price_score = %s, total_effective_rate = %s, projected_5yr_cost = %s 
            WHERE id = %s;
        """, (vendor['price_score'], vendor['total_effective_rate'], vendor['projected_5yr_cost'], vendor['id']))

    conn.commit()

    cursor.execute("""
        SELECT line_item_id, procurement_option_id, weighting_id, score, justification
        FROM options_line_items_non_pricing 
        WHERE procurement_option_id IN (SELECT id FROM procurement_options WHERE project_id = %s);
    """, (project_id,))
    scores_raw = cursor.fetchall()
    
    scores_map = {}
    for row in scores_raw:
        w_id = row["weighting_id"]
        v_id = row["procurement_option_id"]
        if w_id not in scores_map:
            scores_map[w_id] = {}
        scores_map[w_id][v_id] = row

    cursor.close()
    conn.close()

    return render_template(
        "projects.html", 
        project=project, 
        vendors=vendors, 
        criteria_list=criteria_list, 
        scores_map=scores_map,
        all_projects=all_projects,
        gl_records=gl_records
    )


@projects_bp.route("/create-project", methods=["POST"])
def create_project():
    conn = get_db_connection()
    cursor = conn.cursor()

    raw_weight = float(request.form.get("price_weighting", 30))
    price_weighting = raw_weight / 100.0 if raw_weight > 1.0 else raw_weight

    cursor.execute("""
        INSERT INTO projects (
            project_reference, name, project_description, project_objective, 
            ai_prompt_adjustments, phase1_prompt_adjustments, gl_code, gl_title, gl_sub, price_weighting, 
            executive_sourcing_recommendation
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id;
    """, (
        request.form.get("project_reference", ""),
        request.form.get("name", ""),
        request.form.get("project_description", ""),
        request.form.get("project_objective", ""),
        request.form.get("ai_prompt_adjustments", ""),
        request.form.get("phase1_prompt_adjustments", ""),
        request.form.get("gl_code", ""),
        request.form.get("gl_title", ""),
        request.form.get("gl_sub", ""),
        price_weighting,
        request.form.get("executive_sourcing_recommendation", "")
    ))

    new_id = cursor.fetchone()["id"]
    conn.commit()
    cursor.close()
    conn.close()

    flash("New project created successfully.")
    return redirect(url_for("projects.projects_page", project_id=new_id))


@projects_bp.route("/upload-quote", methods=["POST"])
def upload_quote():
    raw_project_id = request.form.get("project_id", "1").strip()
    vendor_name = request.form.get("vendor_name", "").strip()
    file = request.files.get("quote_file")

    try:
        project_id = int(raw_project_id) if raw_project_id and raw_project_id != "None" else 1
    except ValueError:
        project_id = 1

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM projects WHERE id = %s;", (project_id,))
            if not cursor.fetchone():
                cursor.execute("""
                    INSERT INTO projects (id, project_reference, name, project_description) 
                    VALUES (%s, %s, %s, %s);
                """, (project_id, f"2026_{project_id:02d}", f"New Project #{project_id}", "Initial Evaluation"))
                conn.commit()

            if file and file.filename != '':
                file_bytes = file.read()
                filename = file.filename

                cursor.execute("""
                    INSERT INTO procurement_options (project_id, vendor_name, quote_filename, quote_file_bytes)
                    VALUES (%s, %s, %s, %s);
                """, (project_id, vendor_name, filename, psycopg2.Binary(file_bytes)))
                
                conn.commit()
                flash(f"Quote for '{vendor_name}' uploaded successfully.", "success")
            else:
                flash("No valid file selected for upload.", "warning")

    except Exception as e:
        conn.rollback()
        flash(f"Error uploading quote: {str(e)}", "danger")
    finally:
        conn.close()

    return redirect(url_for("projects.projects_page", project_id=project_id))


@projects_bp.route('/view-quote/<int:option_id>')
def view_quote(option_id):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT quote_filename, quote_file_bytes 
                FROM procurement_options 
                WHERE id = %s;
            """, (option_id,))
            record = cursor.fetchone()
            
            if not record or not record.get('quote_file_bytes'):
                return "Quote not found", 404
                
            filename = record.get('quote_filename') or f"quote_{option_id}.pdf"
            file_bytes = bytes(record['quote_file_bytes'])
            
            return send_file(
                io.BytesIO(file_bytes),
                mimetype='application/pdf',
                as_attachment=False,
                download_name=filename
            )


@projects_bp.route("/execute-phase1/<int:project_id>", methods=["POST"])
def handle_phase1(project_id):
    phase1_adjustments = request.form.get("phase1_prompt_adjustments", "")
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            if phase1_adjustments:
                cursor.execute("""
                    UPDATE projects 
                    SET phase1_prompt_adjustments = %s
                    WHERE id = %s;
                """, (phase1_adjustments, project_id))
            
            cursor.execute("""
                UPDATE projects 
                SET latest_ai_status = 'Formulating Criteria (Phase 1)...' 
                WHERE id = %s;
            """, (project_id,))

        conn.commit()
        execute_phase1(conn, project_id)
        
        with conn.cursor() as cursor:
            cursor.execute("UPDATE projects SET latest_ai_status = 'Phase 1 Complete' WHERE id = %s;", (project_id,))
        conn.commit()

        flash("Phase 1: Assessment Criteria Formulated Successfully!")
    except Exception as e:
        conn.rollback()
        flash(f"Error during Phase 1 processing: {str(e)}")
    finally:
        conn.close()

    return redirect(url_for("projects.projects_page", project_id=project_id))


@projects_bp.route("/execute-phase2b/<int:project_id>", methods=["POST"])
def execute_phase2b(project_id):
    ai_adjustments = request.form.get("ai_prompt_adjustments", "")
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE projects 
                SET ai_prompt_adjustments = %s, latest_ai_status = 'Evaluating Vendors (Phase 2)...' 
                WHERE id = %s;
            """, (ai_adjustments, project_id))
        conn.commit()

        execute_phase2(conn, project_id)

        with conn.cursor() as cursor:
            cursor.execute("UPDATE projects SET latest_ai_status = 'Phase 2 Complete' WHERE id = %s;", (project_id,))
        conn.commit()

        flash("Phase 2b: Vendor Pricing & Evaluation Completed Successfully!", "success")

    except Exception as e:
        if conn and not conn.closed:
            conn.rollback()
        flash(f"Error during Phase 2 evaluation: {str(e)}", "danger")

    finally:
        if conn and not conn.closed:
            conn.close()

    return redirect(url_for("projects.projects_page", project_id=project_id))


@projects_bp.route("/recalculate-matrix/<int:project_id>", methods=["POST"])
def recalculate_matrix(project_id):
    try:
        raw_weight = float(request.form.get("price_weighting", 30))
    except (ValueError, TypeError):
        raw_weight = 30.0

    price_weighting = raw_weight / 100.0 if raw_weight > 1.0 else raw_weight
    conn = get_db_connection()
    
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE projects 
                SET price_weighting = %s, recalculate_matrix = TRUE 
                WHERE id = %s;
            """, (price_weighting, project_id))
        conn.commit()

        execute_phase2(conn, project_id)
        flash("Matrix recalculated with updated weightings.", "success")
    except Exception as e:
        if conn and not conn.closed:
            conn.rollback()
        flash(f"Error recalculating matrix: {str(e)}", "danger")

    finally:
        if conn and not conn.closed:
            conn.close()

    return redirect(url_for("projects.projects_page", project_id=project_id))


@projects_bp.route("/update-project/<int:project_id>", methods=["POST"])
def update_project(project_id):
    project_reference = request.form.get("project_reference", "")
    name = request.form.get("name", "")
    project_description = request.form.get("project_description", "")
    project_objective = request.form.get("project_objective", "")
    ai_prompt_adjustments = request.form.get("ai_prompt_adjustments", "")
    phase1_prompt_adjustments = request.form.get("phase1_prompt_adjustments", "")
    gl_code = request.form.get("gl_code", "")
    gl_title = request.form.get("gl_title", "")
    gl_sub = request.form.get("gl_sub", "")
    executive_sourcing_recommendation = request.form.get("executive_sourcing_recommendation", "")
    
    try:
        raw_weight = float(request.form.get("price_weighting", 30))
    except (ValueError, TypeError):
        raw_weight = 30.0

    price_weighting = raw_weight / 100.0 if raw_weight > 1.0 else raw_weight

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            UPDATE projects 
            SET project_reference = %s, name = %s, project_description = %s, project_objective = %s,
                ai_prompt_adjustments = %s, phase1_prompt_adjustments = %s, gl_code = %s, gl_title = %s,
                gl_sub = %s, price_weighting = %s, executive_sourcing_recommendation = %s
            WHERE id = %s;
        """, (
            project_reference, name, project_description, project_objective, 
            ai_prompt_adjustments, phase1_prompt_adjustments, gl_code, gl_title, gl_sub, 
            price_weighting, executive_sourcing_recommendation, project_id
        ))

        deleted_criteria_ids = request.form.getlist('deleted_criteria_ids')
        deleted_item_ids = request.form.getlist('deleted_item_ids')

        if deleted_criteria_ids:
            cursor.execute("DELETE FROM project_weightings WHERE id = ANY(%s::int[]);", (deleted_criteria_ids,))

        if deleted_item_ids:
            cursor.execute("DELETE FROM options_line_items_pricing WHERE id = ANY(%s::int[]);", (deleted_item_ids,))

        conn.commit()

        cursor.execute("SELECT id, total_quantity FROM procurement_options WHERE project_id = %s;", (project_id,))
        project_vendors = cursor.fetchall()

        for key, value in request.form.items():
            if any(key.endswith(f"_{d_id}") for d_id in deleted_item_ids):
                continue

            if key.startswith("pricing_name_") and not key.startswith("pricing_name_new_"):
                item_id = key.split("pricing_name_")[1]
                if item_id.isdigit():
                    cursor.execute("UPDATE options_line_items_pricing SET cost_component_name = %s WHERE id = %s;", (value, int(item_id)))

            elif key.startswith("pricing_amount_") and not key.startswith("pricing_amount_new_"):
                item_id = key.split("pricing_amount_")[1]
                if item_id.isdigit():
                    amount_val = float(value) if value else 0.0
                    cursor.execute("UPDATE options_line_items_pricing SET amount = %s WHERE id = %s;", (amount_val, int(item_id)))

            elif key.startswith("criterion_name_"):
                criteria_id = key.replace("criterion_name_", "")
                if criteria_id.isdigit():
                    cursor.execute("UPDATE project_weightings SET criterion_name = %s WHERE id = %s;", (value, int(criteria_id)))

            elif key.startswith("criteria_weight_"):
                criteria_id = key.replace("criteria_weight_", "")
                if criteria_id.isdigit():
                    weight_val = float(value) if value else 0.0
                    cursor.execute("UPDATE project_weightings SET weight_percent = %s WHERE id = %s;", (weight_val, int(criteria_id)))

        conn.commit()
        flash("Project definitions and weightings updated successfully.")

    except Exception as e:
        conn.rollback()
        flash(f"Error updating project: {str(e)}")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for("projects.projects_page", project_id=project_id))


@projects_bp.route('/generate-recommendation/<int:project_id>', methods=['POST'])
def generate_recommendation(project_id):
    if not GEMINI_AVAILABLE:
        flash('Gemini API library is not installed or configured.')
        return redirect(url_for('projects.projects_page', project_id=project_id))

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM projects WHERE id = %s;", (project_id,))
            project = cursor.fetchone()

            if not project:
                flash('Project not found.')
                return redirect(url_for('projects.projects_page', project_id=project_id))

            cursor.execute("SELECT * FROM procurement_options WHERE project_id = %s;", (project_id,))
            vendors = cursor.fetchall()

            if not vendors:
                flash('Cannot generate recommendation without vendor options.')
                return redirect(url_for('projects.projects_page', project_id=project_id))

            cursor.execute("SELECT prompt_template, selected_model FROM system_prompts WHERE process = 'executive_recommendation' AND is_active = TRUE;")
            rec_row = cursor.fetchone()
            
            cursor.execute("SELECT prompt_template FROM system_prompts WHERE process = 'Company assessment' AND is_active = TRUE;")
            company_row = cursor.fetchone()

            prompt_template = rec_row['prompt_template'] if rec_row else ""
            company_assessment_context = company_row['prompt_template'] if company_row else ""

            sorted_by_score = sorted(vendors, key=lambda v: v.get('final_weighted_score_output') or v.get('weighted_score') or 0, reverse=True)
            sorted_by_cost = sorted(vendors, key=lambda v: v.get('projected_5yr_total') or v.get('total_cost') or float('inf'))

            winner = sorted_by_score[0] if sorted_by_score else None
            cheapest = sorted_by_cost[0] if sorted_by_cost else None

            winner_name = winner.get('vendor_name', 'N/A') if winner else "N/A"
            winning_score = f"{float(winner.get('final_weighted_score_output') or 0.0):.1f}"
            cheapest_vendor = cheapest.get('vendor_name', 'N/A') if cheapest else "N/A"
            min_cost = f"{int(round(float(cheapest.get('projected_5yr_total') or 0))):,}"

            context_vars = {
                "{winner_name}": winner_name,
                "{winning_score}": winning_score,
                "{cheapest_vendor}": cheapest_vendor,
                "{min_cost}": min_cost
            }

            formatted_task_prompt = prompt_template
            for placeholder, val in context_vars.items():
                formatted_task_prompt = formatted_task_prompt.replace(placeholder, str(val))

            full_prompt = f"""
=== COMPANY & SYSTEM DIRECTIVES ===
{company_assessment_context}

=== PROJECT DETAILS ===
Project Name: {project.get('name', '')}
Project Reference: {project.get('project_reference', '')}

=== EVALUATION DATA & INSTRUCTIONS ===
{formatted_task_prompt}
"""

            model_name = (rec_row.get('selected_model') if rec_row else None) or 'gemini-2.5-flash'
            client = genai.Client()
            response = client.models.generate_content(model=model_name, contents=[full_prompt])
            generated_text = response.text if response and response.text else "No content generated."

            cursor.execute("""
                UPDATE projects 
                SET executive_sourcing_recommendation = %s, latest_ai_status = 'Recommendation Generated' 
                WHERE id = %s;
            """, (generated_text, project_id))

            generate_executive_recommendation_html(conn, project_id)
            conn.commit()
            flash('Executive sourcing recommendation successfully generated!')

    except Exception as e:
        conn.rollback()
        flash(f'Error generating recommendation: {str(e)}')
    finally:
        conn.close()

    return redirect(url_for('projects.projects_page', project_id=project_id))


@projects_bp.route("/project/<int:project_id>/executive-recommendation")
def view_executive_recommendation(project_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT executive_recommendation_html FROM projects WHERE id = %s;", (project_id,))
            row = cursor.fetchone()

        html_content = row.get("executive_recommendation_html") if isinstance(row, dict) else (row[0] if row else None)
        if not html_content:
            flash("Executive recommendation HTML has not been generated yet.", "warning")
            return redirect(url_for("projects.projects_page", project_id=project_id))

        return html_content, 200, {'Content-Type': 'text/html; charset=utf-8'}
    finally:
        if conn and not conn.closed:
            conn.close()
