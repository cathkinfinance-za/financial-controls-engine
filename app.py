import os
import io
import json
import psycopg2
import psycopg2.extras
import vercel_blob
from ai_matrix_drafter_postgres import execute_phase1
from vendor_comparison_engine_postgres import execute_phase2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from werkzeug.utils import secure_filename
from ai_matrix_drafter_postgres import execute_phase1
from vendor_comparison_engine_postgres import execute_phase2


try:
    import google.generativeai as genai
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    GEMINI_AVAILABLE = True
except ModuleNotFoundError:
    genai = None
    GEMINI_AVAILABLE = False

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "cathkin-estates-secret-key")
DATABASE_URL = os.getenv("DATABASE_URL")


# --- 1. NEW DASHBOARD HOME PAGE ROUTE ---
@app.route("/")
def dashboard_home():
    """Renders the central management dashboard as the home page."""
    return render_template("dashboard.html")

@app.route('/guide')
def render_guide_page():
    conn = None
    matrix_rows = []
    try:
        conn = get_db_connection()
        # Pass RealDictCursor here so rows match dictionary keys expected by guide.html
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("""
                SELECT id, matrix_category, min_value, max_value, 
                       reviewer_roles, quotes_required, approver_roles, 
                       applicable_controls, compliance_audit_requirement 
                FROM approval_matrix 
                ORDER BY id ASC;
            """)
            matrix_rows = cursor.fetchall()
    except Exception as e:
        print(f"Database error on /guide: {e}")
    finally:
        if conn:
            conn.close()
        
    return render_template('guide.html', matrix_rows=matrix_rows)

def get_connection():
    """Establish connection to Neon PostgreSQL database."""
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def analyze_po_with_gemini(uploaded_files_data, form_data):
    """
    Sends uploaded file streams to Gemini Flash for extraction & compliance checking.
    uploaded_files_data: list of tuples -> (file_bytes, filename, mime_type)
    """
    if not GEMINI_AVAILABLE or not os.getenv("GEMINI_API_KEY"):
        return "Gemini AI SDK is not installed or GEMINI_API_KEY is missing."

    gemini_file_objects = []
    try:
        model = genai.GenerativeModel('gemini-3.5-flash')
        
        for file_bytes, filename, mime_type in uploaded_files_data:
            # Use io.BytesIO so genai receives a readable stream
            bio = io.BytesIO(file_bytes)
            bio.name = filename
            
            uploaded_gemini_file = genai.upload_file(
                bio, 
                mime_type=mime_type or "application/pdf"
            )
            gemini_file_objects.append(uploaded_gemini_file)

        prompt = f"""
        Analyze the attached purchase order documents and quote attachments for vendor compliance.
        
        Form Details:
        - PO Number: {form_data.get('po_number')}
        - Description: {form_data.get('description')}
        - Estimated Cost: R{form_data.get('estimated_cost')}
        - Suggested Vendor: {form_data.get('recommended_vendor')}
        - Justification: {form_data.get('justification_notes')}
        
        Tasks:
        1. Extract and summarize key line items, total amounts, and vendor details from attached documents.
        2. Highlight any discrepancies between quotes and the submitted form data.
        3. Provide a 2-3 sentence executive recommendation.
        """

        response = model.generate_content([*gemini_file_objects, prompt])
        return response.text.strip() if response and response.text else "AI analysis produced no text."

    except Exception as err:
        print(f"Gemini Processing Error: {err}")
        return f"AI Analysis failed: {str(err)}"
        
    finally:
        # Clean up files uploaded to Gemini File API
        for file_obj in gemini_file_objects:
            try:
                genai.delete_file(file_obj.name)
            except Exception as e:
                print(f"Gemini File Cleanup Error: {e}")

@app.route("/api/budget-details")
def budget_details():
    """API endpoint for live JS fetching of budget summary metrics on GL Code change."""
    gl_code = request.args.get("gl_code")
    if not gl_code:
        return jsonify({})

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT 
                    COALESCE(ytd, 0.00) AS ytd_actual, 
                    COALESCE(total_budget, 0.00) AS total_annual_budget, 
                    COALESCE(budget_ytd, 0.00) AS budget_ytd, 
                    LEAST(
                        ROUND(COALESCE(total_budget, 0.00) / 6.0, 2),
                        GREATEST(0.00, COALESCE(total_budget, 0.00) - COALESCE(ytd, 0.00))
                    ) AS buffer_pool, 
                    COALESCE(variance, 0.00) AS variance 
                FROM master_budget 
                WHERE gl_code = %s;
            """, (gl_code,))
            row = cur.fetchone()
            return jsonify(row if row else {})
    finally:
        conn.close()

@app.route("/", methods=["GET", "POST"])
@app.route("/po_form", methods=["GET", "POST"])
@app.route("/simple", methods=["GET", "POST"])

def po_form():
    conn = get_connection()
    message = None
    selected_po = None
    financial_summary = None

    if request.method == "POST":
        original_po = request.form.get("original_po_number", "").strip()
        new_po = request.form.get("po_number", "").strip()
        description = request.form.get("description", "").strip()
        po_date = request.form.get("po_date") or None
        is_budgeted = request.form.get("is_budgeted")
        gl_code = request.form.get("gl_code", "").strip() or None
        expense_type = request.form.get("expense_type")
        recommended_vendor = request.form.get("recommended_vendor", "").strip()
        justification_notes = request.form.get("justification_notes", "").strip()
        submission_status = request.form.get("submission_status")

        # Approvals Fields
        actioned_date = request.form.get("actioned_date") or None
        actioned_by = request.form.get("actioned_by", "").strip()
        approval_notes = request.form.get("approval_notes", "").strip()
        ai_recommendation_summary = request.form.get("ai_recommendation_summary", "").strip()
        system_status = request.form.get("system_status", "").strip()

        try:
            estimated_cost = float(request.form.get("estimated_cost") or 0.0)
        except ValueError:
            estimated_cost = 0.0

        # 1. Check hidden form input first, then fall back to DB lookup to preserve existing URLs
        existing_filepath_str = request.form.get("existing_quote_filepath", "").strip()

        if not existing_filepath_str and (original_po or new_po):
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    lookup_target = original_po if original_po else new_po
                    cur.execute("SELECT quote_filepath FROM po_log WHERE po_number = %s;", (lookup_target,))
                    existing_row = cur.fetchone()
                    if existing_row and existing_row.get("quote_filepath"):
                        existing_filepath_str = existing_row["quote_filepath"]
            except Exception as e:
                print(f"Error fetching existing PO filepath: {e}")

        existing_urls = [u.strip() for u in existing_filepath_str.split(",") if u.strip()]

        # --- MULTI-FILE VERCEL BLOB UPLOAD & GEMINI PREPARATION ---
        raw_files = request.files.getlist('attach_quotes') or request.files.getlist('quote_attachment')
        new_urls = []
        gemini_file_payloads = []

        for quote_file in raw_files:
            if quote_file and quote_file.filename:
                try:
                    file_bytes = quote_file.read()
                    safe_filename = secure_filename(quote_file.filename)
                    
                    # 1. Save to Vercel Blob using "private" access
                    blob_response = vercel_blob.put(
                        f"quotes/{safe_filename}", 
                        file_bytes, 
                        options={
                            "access": "public",
                            "token": os.getenv("PUBLIC_BLOB_READ_WRITE_TOKEN")
                        }
                    )
                    
                    # Extract URL from response (handles dict or object response)
                    url = blob_response.get('url') if isinstance(blob_response, dict) else getattr(blob_response, 'url', None)
                    if url:
                        new_urls.append(url)

                    # 2. Collect bytes for Gemini
                    gemini_file_payloads.append((
                        file_bytes, 
                        safe_filename, 
                        quote_file.mimetype
                    ))
                except Exception as upload_err:
                    print(f"File Processing Error ({quote_file.filename}): {upload_err}")

        # Combine existing and newly uploaded attachment URLs
        all_urls = existing_urls + new_urls
        combined_quote_filepath = ",".join(all_urls) if all_urls else None
        
        # Dynamically set quotes_provided to match total attached files count
        quotes_provided = len(all_urls)

        # Run Gemini Analysis on newly uploaded attachments
        if gemini_file_payloads:
            ai_summary = analyze_po_with_gemini(gemini_file_payloads, request.form)
            if ai_summary:
                ai_recommendation_summary = ai_summary

        if new_po:
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    gl_code_id = None
                    if gl_code:
                        cur.execute("SELECT id FROM master_budget WHERE gl_code = %s;", (gl_code,))
                        gl_row = cur.fetchone()
                        if gl_row:
                            gl_code_id = gl_row['id']

                    if original_po:
                        cur.execute("""
                            UPDATE po_log 
                            SET po_number = %s, description = %s, po_date = %s, is_budgeted = %s,
                                gl_code = %s, gl_code_id = %s, expense_type = %s, estimated_cost = %s,
                                recommended_vendor = %s, justification_notes = %s, submission_status = %s,
                                system_status = %s, quotes_provided = %s, actioned_date = %s,
                                actioned_by = %s, approval_notes = %s, ai_recommendation_summary = %s,
                                quote_filepath = %s
                            WHERE po_number = %s;
                        """, (
                            new_po, description, po_date, is_budgeted, gl_code, gl_code_id, 
                            expense_type, estimated_cost, recommended_vendor, justification_notes, 
                            submission_status, system_status, quotes_provided, actioned_date, 
                            actioned_by, approval_notes, ai_recommendation_summary, 
                            combined_quote_filepath, original_po
                        ))
                    else:
                        cur.execute("""
                            INSERT INTO po_log (
                                po_number, description, po_date, is_budgeted, gl_code, gl_code_id, 
                                expense_type, estimated_cost, recommended_vendor, justification_notes, 
                                submission_status, system_status, quotes_provided, actioned_date, 
                                actioned_by, approval_notes, ai_recommendation_summary, quote_filepath
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (po_number) DO UPDATE SET
                                description = EXCLUDED.description,
                                po_date = EXCLUDED.po_date,
                                is_budgeted = EXCLUDED.is_budgeted,
                                gl_code = EXCLUDED.gl_code,
                                gl_code_id = EXCLUDED.gl_code_id,
                                expense_type = EXCLUDED.expense_type,
                                estimated_cost = EXCLUDED.estimated_cost,
                                recommended_vendor = EXCLUDED.recommended_vendor,
                                justification_notes = EXCLUDED.justification_notes,
                                submission_status = EXCLUDED.submission_status,
                                system_status = EXCLUDED.system_status,
                                quotes_provided = EXCLUDED.quotes_provided,
                                actioned_date = EXCLUDED.actioned_date,
                                actioned_by = EXCLUDED.actioned_by,
                                approval_notes = EXCLUDED.approval_notes,
                                ai_recommendation_summary = EXCLUDED.ai_recommendation_summary,
                                quote_filepath = EXCLUDED.quote_filepath;
                        """, (
                            new_po, description, po_date, is_budgeted, gl_code, gl_code_id, 
                            expense_type, estimated_cost, recommended_vendor, justification_notes, 
                            submission_status, system_status, quotes_provided, actioned_date, 
                            actioned_by, approval_notes, ai_recommendation_summary, combined_quote_filepath
                        ))
                    
                    conn.commit()

                return redirect(url_for('po_form', po_number=new_po))

            except Exception as e:
                conn.rollback()
                message = f"Error saving purchase order: {e}"

    # --- GET REQUEST / DATA RENDERING ---
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT gl_code, description FROM master_budget ORDER BY gl_code ASC;")
            gl_records = cur.fetchall()

            cur.execute("SELECT po_number, description FROM po_log ORDER BY created_at DESC;")
            saved_pos = cur.fetchall()

            selected_po_num = request.args.get("po_number")
            if selected_po_num:
                cur.execute("SELECT * FROM po_log WHERE po_number = %s;", (selected_po_num,))
                selected_po = cur.fetchone()

                if selected_po:
                    raw_filepath = selected_po.get('quote_filepath') or ''
                    selected_po['quote_urls'] = [
                        u.strip() for u in raw_filepath.split(',') if u.strip()
                    ]

                    if selected_po.get('gl_code'):
                        try:
                            cur.execute("""
                                SELECT 
                                    COALESCE(ytd, 0.00) AS ytd_actual, 
                                    COALESCE(total_budget, 0.00) AS total_annual_budget, 
                                    COALESCE(budget_ytd, 0.00) AS budget_ytd, 
                                    LEAST(
                                        ROUND(COALESCE(total_budget, 0.00) / 6.0, 2),
                                        GREATEST(0.00, COALESCE(total_budget, 0.00) - COALESCE(ytd, 0.00))
                                    ) AS buffer_pool, 
                                    COALESCE(variance, 0.00) AS variance 
                                FROM master_budget WHERE gl_code = %s;
                            """, (selected_po['gl_code'],))
                            financial_summary = cur.fetchone()
                        except Exception:
                            financial_summary = None

    finally:
        conn.close()

    return render_template(
        "po_form.html", 
        gl_records=gl_records,
        saved_pos=saved_pos,
        selected_po=selected_po,
        financial_summary=financial_summary,
        message=message
    )


def get_db_connection():
    """Establishes and returns a connection to the Neon PostgreSQL database."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL environment variable is missing.")
    
    conn = psycopg2.connect(
        db_url,
        cursor_factory=RealDictCursor
    )
    return conn

@app.route('/prompts')
def render_prompts_page():
    return render_template('prompts.html')

@app.route('/api/prompts', methods=['GET', 'POST'])
def handle_prompts_api():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            if request.method == 'GET':
                process_name = request.args.get('process')
                
                # If no process parameter passed, return a list of all process names
                if not process_name:
                    cursor.execute("SELECT DISTINCT process FROM system_prompts ORDER BY process ASC;")
                    rows = cursor.fetchall()
                    processes = [row['process'] for row in rows]
                    return jsonify(processes), 200

                # Fetch specific prompt details
                cursor.execute(
                    "SELECT process, prompt_template, description, is_active FROM system_prompts WHERE LOWER(process) = LOWER(%s);", 
                    (process_name,)
                )
                row = cursor.fetchone()
                if row:
                    return jsonify(dict(row)), 200
                return jsonify({'error': 'Prompt process not found.'}), 404

            elif request.method == 'POST':
                data = request.json or {}
                process_name = data.get('process')
                prompt_template = data.get('prompt_template')
                is_active = data.get('is_active', True)

                if not process_name or prompt_template is None:
                    return jsonify({'error': 'Missing required fields.'}), 400

                cursor.execute("""
                    UPDATE system_prompts 
                    SET prompt_template = %s, 
                        is_active = %s,
                        updated_at = CURRENT_TIMESTAMP 
                    WHERE LOWER(process) = LOWER(%s);
                """, (prompt_template, is_active, process_name))
                conn.commit()

                return jsonify({'status': 'success', 'message': 'Prompt updated successfully.'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/audit-log')
def view_audit_log():
    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM workflow_control_log ORDER BY timestamp DESC LIMIT 100;")
        logs = cursor.fetchall()
    conn.close()
    return render_template('audit_log.html', logs=logs)

# Helper to fetch all projects for the sidebar
def get_all_projects_summary(cursor):
    cursor.execute("SELECT id, project_reference, name FROM projects ORDER BY id DESC;")
    return cursor.fetchall()


# 1. Base route: Opens blank project form
@app.route("/projects/", methods=["GET"])
@app.route("/projects", methods=["GET"])
def new_project_page():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    all_projects = get_all_projects_summary(cursor)
    
    cursor.close()
    conn.close()
    
    # Renders template with project=None (blank form)
    return render_template("projects.html", project=None, vendors=[], criteria_list=[], scores_map={}, all_projects=all_projects)

# Route to list or access projects dashboard
@app.route("/projects")
@app.route("/projects/<int:project_id>", methods=["GET"])
def projects_page(project_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Sidebar data
    all_projects = get_all_projects_summary(cursor)

    # Active Project
    cursor.execute("SELECT * FROM projects WHERE id = %s;", (project_id,))
    project = cursor.fetchone()

    # Active Vendors
    cursor.execute("SELECT * FROM procurement_options WHERE project_id = %s;", (project_id,))
    vendors = cursor.fetchall()

    # Criteria list
    cursor.execute("SELECT * FROM project_weightings WHERE project_id = %s ORDER BY id ASC;", (project_id,))
    criteria_list = cursor.fetchall()

    # Pricing items per vendor
    for vendor in vendors:
        cursor.execute("SELECT * FROM options_line_items_pricing WHERE procurement_option_id = %s ORDER BY id ASC;", (vendor["id"],))
        vendor["pricing_items"] = cursor.fetchall()

    # Qualitative scores map
    cursor.execute("""
        SELECT line_item_id, procurement_option_id, weighting_id, score 
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
        all_projects=all_projects
    )

@app.route("/create-project", methods=["POST"])
def create_project():
    conn = get_db_connection()
    cursor = conn.cursor()

    raw_weight = float(request.form.get("price_weighting", 30))
    price_weighting = raw_weight / 100.0 if raw_weight > 1.0 else raw_weight

    cursor.execute("""
        INSERT INTO projects (
            project_reference, name, project_description, project_objective, 
            ai_prompt_adjustments, gl_code, gl_title, gl_sub, price_weighting, 
            executive_sourcing_recommendation
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id;
    """, (
        request.form.get("project_reference", ""),
        request.form.get("name", ""),
        request.form.get("project_description", ""),
        request.form.get("project_objective", ""),
        request.form.get("ai_prompt_adjustments", ""),
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
    return redirect(url_for("projects_page", project_id=new_id))

@app.route('/projects/<int:project_id>')
def view_project(project_id):
    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM projects WHERE id = %s;", (project_id,))
        project = cursor.fetchone()

        cursor.execute("SELECT * FROM procurement_options WHERE project_id = %s;", (project_id,))
        vendors = cursor.fetchall()

        # 1. Fetch unified project criteria weightings
        cursor.execute("""
            SELECT * FROM project_weightings 
            WHERE project_id = %s 
            ORDER BY id ASC;
        """, (project_id,))
        criteria_list = cursor.fetchall()

        # 2. Fetch pricing line items per vendor
        for vendor in vendors:
            cursor.execute("""
                SELECT * FROM options_line_items_pricing 
                WHERE procurement_option_id = %s 
                ORDER BY id ASC;
            """, (vendor["id"],))
            vendor["pricing_items"] = cursor.fetchall()

        # 3. Map non-pricing scores by (weighting_id, vendor_id)
        cursor.execute("""
            SELECT line_item_id, procurement_option_id, weighting_id, score 
            FROM options_line_items_non_pricing 
            WHERE procurement_option_id IN (
                SELECT id FROM procurement_options WHERE project_id = %s
            );
        """, (project_id,))
        scores_raw = cursor.fetchall()
        
        # Build score map: scores_map[weighting_id][vendor_id] = {id, score}
        scores_map = {}
        for row in scores_raw:
            w_id = row["weighting_id"]
            v_id = row["procurement_option_id"]
            if w_id not in scores_map:
                scores_map[w_id] = {}
            scores_map[w_id][v_id] = row

    conn.close()
    return render_template(
        "projects.html", 
        project=project, 
        vendors=vendors, 
        criteria_list=criteria_list, 
        scores_map=scores_map
    )
# --- 2. QUOTE UPLOAD ROUTE ---
@app.route("/upload-quote", methods=["POST"])
def upload_quote():
    raw_project_id = request.form.get("project_id", "1").strip()
    vendor_name = request.form.get("vendor_name", "").strip()
    file = request.files.get("quote_file")

    try:
        project_id = int(raw_project_id) if raw_project_id and raw_project_id != "None" else 1
    except ValueError:
        project_id = 1

    conn = get_db_connection()
    cursor = conn.cursor()

    # Verify or create parent project
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
        flash(f"Quote for '{vendor_name}' uploaded successfully.")

    cursor.close()
    conn.close()

    return redirect(url_for("projects_page", project_id=project_id))

# --- 3. DRAFT MATRIX VIA AI (PHASE 1) ROUTE ---
@app.route("/draft-matrix/<int:project_id>", methods=["POST"])
def draft_matrix(project_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Save prompt adjustments or metadata changes prior to AI run
    prompt_adjustments = request.form.get("ai_prompt_adjustments", "")
    cursor.execute("""
        UPDATE projects 
        SET ai_prompt_adjustments = %s, latest_ai_status = 'Processing AI Matrix...' 
        WHERE id = %s;
    """, (prompt_adjustments, project_id))
    conn.commit()
    cursor.close()
    conn.close()

    # Execute Phase 1 Multi-Modal Gemini Extraction
    try:
        execute_phase1(project_id)
        flash("AI Matrix Draft completed successfully.")
    except Exception as e:
        flash(f"Error during AI drafting: {str(e)}")

    return redirect(url_for("projects_page", project_id=project_id))

@app.route("/recalculate-matrix/<int:project_id>", methods=["POST"])
def recalculate_matrix(project_id):
    # Safely parse and convert the weight input
    try:
        raw_weight = float(request.form.get("price_weighting", 30))
    except (ValueError, TypeError):
        raw_weight = 30.0

    # Normalize whole percentage (e.g., 50.0) to decimal (0.50)
    price_weighting = raw_weight / 100.0 if raw_weight > 1.0 else raw_weight
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE projects 
        SET price_weighting = %s, recalculate_matrix = TRUE 
        WHERE id = %s;
    """, (price_weighting, project_id))
    conn.commit()
    cursor.close()
    conn.close()

    # Execute Phase 2 Scoring Engine
    try:
        execute_phase2(project_id)
        flash("Matrix recalculated with updated weightings.")
    except Exception as e:
        flash(f"Error recalculating matrix: {str(e)}")

    return redirect(url_for("projects_page", project_id=project_id))

# --- 5. UPDATE PROJECT DEFINITIONS ROUTE ---
@app.route("/update-project/<int:project_id>", methods=["POST"])
def update_project(project_id):
    project_reference = request.form.get("project_reference", "")
    name = request.form.get("name", "")
    project_description = request.form.get("project_description", "")
    project_objective = request.form.get("project_objective", "")
    ai_prompt_adjustments = request.form.get("ai_prompt_adjustments", "")
    gl_code = request.form.get("gl_code", "")
    gl_title = request.form.get("gl_title", "")
    gl_sub = request.form.get("gl_sub", "")
    executive_sourcing_recommendation = request.form.get("executive_sourcing_recommendation", "")
    
    raw_weight = float(request.form.get("price_weighting", 30))
    price_weighting = raw_weight / 100.0 if raw_weight > 1.0 else raw_weight

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 1. Update Project Definitions
        cursor.execute("""
            UPDATE projects 
            SET project_reference = %s,
                name = %s,
                project_description = %s,
                project_objective = %s,
                ai_prompt_adjustments = %s,
                gl_code = %s,
                gl_title = %s,
                gl_sub = %s,
                price_weighting = %s,
                executive_sourcing_recommendation = %s
            WHERE id = %s;
        """, (
            project_reference, name, project_description, project_objective, 
            ai_prompt_adjustments, gl_code, gl_title, gl_sub, 
            price_weighting, executive_sourcing_recommendation, project_id
        ))

        # 2. Dynamic Update of Pricing and Non-Pricing Line Items
        for key, value in request.form.items():
            if key.startswith("pricing_name_"):
                item_id = key.replace("pricing_name_", "")
                cursor.execute("""
                    UPDATE options_line_items_pricing 
                    SET cost_component_name = %s 
                    WHERE id = %s;
                """, (value, item_id))

            elif key.startswith("pricing_amount_"):
                item_id = key.replace("pricing_amount_", "")
                amount_val = float(value) if value else 0.0
                cursor.execute("""
                    UPDATE options_line_items_pricing 
                    SET amount = %s 
                    WHERE id = %s;
                """, (amount_val, item_id))

            elif key.startswith("pricing_category_"):
                item_id = key.replace("pricing_category_", "")
                cursor.execute("""
                    UPDATE options_line_items_pricing 
                    SET cost_type_category = %s 
                    WHERE id = %s;
                """, (value, item_id))

            elif key.startswith("non_pricing_score_"):
                item_id = key.replace("non_pricing_score_", "")
                score_val = float(value) if value else 0.0
                cursor.execute("""
                    UPDATE options_line_items_non_pricing 
                    SET score = %s 
                    WHERE id = %s;
                """, (score_val, item_id))

        conn.commit()
        flash("Project definitions and matrix line items updated successfully.")
    except Exception as e:
        conn.rollback()
        flash(f"Error updating project: {str(e)}")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for("projects_page", project_id=project_id))   


@app.route('/delete-vendor/<int:vendor_id>', methods=['POST'])
def delete_vendor(vendor_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Get project_id before deleting
            cursor.execute("SELECT project_id, vendor_name FROM procurement_options WHERE id = %s;", (vendor_id,))
            vendor = cursor.fetchone()
            
            if not vendor:
                flash("Vendor option not found.")
                return redirect(url_for('new_project_page'))

            project_id = vendor['project_id']
            vendor_name = vendor['vendor_name']

            # Delete child line items
            cursor.execute("DELETE FROM options_line_items_pricing WHERE procurement_option_id = %s;", (vendor_id,))
            cursor.execute("DELETE FROM options_line_items_non_pricing WHERE procurement_option_id = %s;", (vendor_id,))

            # Delete vendor parent row
            cursor.execute("DELETE FROM procurement_options WHERE id = %s;", (vendor_id,))
            
            conn.commit()
            flash(f'Option "{vendor_name}" was successfully removed.')
    except Exception as e:
        conn.rollback()
        flash(f'Error deleting vendor: {str(e)}')
    finally:
        conn.close()

    return redirect(url_for('projects_page', project_id=project_id))

@app.route('/generate-recommendation/<int:project_id>', methods=['POST'])
def generate_recommendation(project_id):
    if not GEMINI_AVAILABLE:
        flash('Gemini API library is not installed or configured.')
        return redirect(url_for('projects_page', project_id=project_id))

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # 1. Fetch Project Details
            cursor.execute("SELECT * FROM projects WHERE id = %s;", (project_id,))
            project = cursor.fetchone()

            if not project:
                flash('Project not found.')
                return redirect(url_for('projects_page', project_id=project_id))

            # 2. Fetch Vendors (Procurement Options)
            cursor.execute("SELECT * FROM procurement_options WHERE project_id = %s;", (project_id,))
            vendors = cursor.fetchall()

            if not vendors:
                flash('Cannot generate recommendation without vendor options.')
                return redirect(url_for('projects_page', project_id=project_id))

            # 3. Fetch System Prompts
            cursor.execute("SELECT prompt_template FROM system_prompts WHERE process = 'executive_recommendation' AND is_active = TRUE;")
            rec_row = cursor.fetchone()
            
            cursor.execute("SELECT prompt_template FROM system_prompts WHERE process = 'Company assessment' AND is_active = TRUE;")
            company_row = cursor.fetchone()

            prompt_template = rec_row['prompt_template'] if rec_row else ""
            company_assessment_context = company_row['prompt_template'] if company_row else ""

            # 4. Identify winner and cheapest option
            # Fallback keys check for common field names in database schema
            sorted_by_score = sorted(vendors, key=lambda v: v.get('final_weighted_score_output') or v.get('weighted_score') or 0, reverse=True)
            sorted_by_cost = sorted(vendors, key=lambda v: v.get('projected_5yr_total') or v.get('total_cost') or float('inf'))

            winner = sorted_by_score[0] if sorted_by_score else None
            cheapest = sorted_by_cost[0] if sorted_by_cost else None

            winner_name = winner.get('vendor_name', 'N/A') if winner else "N/A"
            raw_score = winner.get('final_weighted_score_output') or winner.get('weighted_score') or 0.0
            winning_score = f"{float(raw_score):.1f}"

            cheapest_vendor = cheapest.get('vendor_name', 'N/A') if cheapest else "N/A"
            raw_cost = cheapest.get('projected_5yr_total') or cheapest.get('total_cost') or 0
            min_cost = f"{int(round(float(raw_cost))):,}"

            # 5. Inject variables into task template
            formatted_task_prompt = prompt_template.format(
                winner_name=winner_name,
                winning_score=winning_score,
                cheapest_vendor=cheapest_vendor,
                min_cost=min_cost
            )

            # 6. Construct full prompt payload
            full_prompt = f"""
=== COMPANY & SYSTEM DIRECTIVES ===
{company_assessment_context}

=== PROJECT DETAILS ===
Project Name: {project.get('name', '')}
Project Reference: {project.get('project_reference', '')}
Project Description: {project.get('project_description', '')}
Project Objectives: {project.get('project_objective', '')}

=== USER SPECIFIC INSTRUCTIONS ===
{project.get('ai_prompt_adjustments') or 'None provided.'}

=== EVALUATION DATA & INSTRUCTIONS ===
{formatted_task_prompt}
"""

            # 7. Call Gemini API
            model = genai.GenerativeModel('gemini-3.5-flash')
            response = model.generate_content(full_prompt)
            generated_text = response.text if response and response.text else "No content generated."

            # 8. Save output back to Neon PostgreSQL
            cursor.execute("""
                UPDATE projects 
                SET executive_sourcing_recommendation = %s, 
                    latest_ai_status = 'Recommendation Generated' 
                WHERE id = %s;
            """, (generated_text, project_id))
            
            conn.commit()
            flash('Executive sourcing recommendation successfully generated!')

    except Exception as e:
        conn.rollback()
        flash(f'Error generating recommendation: {str(e)}')
    finally:
        conn.close()

    return redirect(url_for('projects_page', project_id=project_id))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)