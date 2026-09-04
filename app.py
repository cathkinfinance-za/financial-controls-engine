import os
import io
import json
from datetime import date
from datetime import datetime
import psycopg2
import psycopg2.extras
from psycopg2.extras import RealDictCursor
import vercel_blob
import requests
import time
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, g
from werkzeug.utils import secure_filename
from ai_matrix_drafter_postgres import execute_phase1
from vendor_comparison_engine_postgres import execute_phase2
from flask import request
from collections import defaultdict
from flask import send_file, abort
from flask import render_template, request, session, redirect, url_for, flash
from werkzeug.security import check_password_hash
from flask import session

try:
    from google import genai
    #genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    GEMINI_AVAILABLE = True
except ModuleNotFoundError:
    genai = None
    GEMINI_AVAILABLE = False

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "cathkin-estates-secret-key")
DATABASE_URL = os.getenv("DATABASE_URL")


def get_db_connection():
    """Establishes and returns a connection to the Neon PostgreSQL database."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL environment variable is missing.")
    return psycopg2.connect(db_url, cursor_factory=RealDictCursor)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        conn = get_db_connection()
        cur = conn.cursor()

        # Querying exact columns from the approvers table
        cur.execute(
                """
                SELECT id, email, password_hash, role, "Name", "Surname" 
                FROM approvers 
                WHERE email = %s
                """,
                (email,)
            )
        user = cur.fetchone()
        cur.close()
        conn.close()

        print(f"DEBUG -> Found user: {user}")
        if user:
            print(f"DEBUG -> Stored hash starts with: {user['password_hash'][:15] if user['password_hash'] else 'None'}")
        
        # Verify the password against the database hash
        if user and user['password_hash'] == password:
            session['user_id'] = user['id']
            return redirect(url_for('dashboard_home'))
        
        flash('Invalid email or password', 'danger')
        
    return render_template('login.html')

@app.before_request
def require_login():
    # List endpoints that do not require authentication
    allowed_endpoints = ['login', 'static']
    
    if request.endpoint not in allowed_endpoints and 'user_id' not in session:
        return redirect(url_for('login', next=request.url))


# Helper to fetch all projects for the sidebar
def get_all_projects_summary(cursor):
    cursor.execute("SELECT id, project_reference, name FROM projects ORDER BY id DESC;")
    return cursor.fetchall()


def trigger_github_workflow():
    github_token = os.environ.get("GITHUB_TOKEN")
    repo = "cathkinfinance-za/financial-controls-engine" 

    if not github_token:
        print("GITHUB_TOKEN missing; skipping instant dispatch.")
        return

    url = f"https://api.github.com/repos/{repo}/dispatches"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {"event_type": "trigger-po-email"}

    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 204:
            print("Successfully triggered GitHub approval workflow.")
        else:
            print(f"Failed to trigger GitHub Action: {response.status_code}")
    except Exception as e:
        print(f"Error triggering GitHub Action: {e}")

# --- DASHBOARD & GENERAL ROUTES ---
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
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, matrix_category, description, min_value, max_value, 
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

@app.route('/legal_framework')
def legal_framework():
    return render_template('legal_framework.html')


def fetch_budget_summary(gl_code):
    if not gl_code:
        return {}

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
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
            return dict(row) if row else {}
    finally:
        conn.close()

# --- PURCHASE ORDERS ---
@app.route("/api/budget-details")
def budget_details():
    gl_code = request.args.get("gl_code")
    return jsonify(fetch_budget_summary(gl_code))


@app.route("/po_form", methods=["GET", "POST"])
@app.route("/simple", methods=["GET", "POST"])
def po_form():
    conn = get_db_connection()
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
        actioned_date = request.form.get("actioned_date") or None
        actioned_by = request.form.get("actioned_by", "").strip()
        approval_notes = request.form.get("approval_notes", "").strip()
        ai_recommendation_summary = request.form.get("ai_recommendation_summary", "").strip()
        approval_requirement = request.form.get('approval_requirement')
        system_status = request.form.get("system_status", "").strip()
        clear_ai_flag = request.form.get("clear_ai_flag", "0")
              
        
        if clear_ai_flag == '1':
            ai_recommendation_summary = None
        else:
            ai_recommendation_summary = request.form.get('ai_recommendation_summary')

        # Update database record
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE po_log
                    SET 
                        ai_recommendation_summary = %s
                    WHERE po_number = %s
                """, (ai_recommendation_summary, new_po))
                conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"Error updating purchase order: {e}")

        try:
            estimated_cost = float(request.form.get("estimated_cost") or 0.0)
        except ValueError:
            estimated_cost = 0.0

        existing_filepath_str = request.form.get("existing_quote_filepath", "").strip()
        deleted_files_raw = request.form.get("deleted_files", "").strip()
        
        if not existing_filepath_str and (original_po or new_po):
            try:
                with conn.cursor() as cur:
                    lookup_target = original_po if original_po else new_po
                    cur.execute("SELECT quote_filepath FROM po_log WHERE po_number = %s;", (lookup_target,))
                    existing_row = cur.fetchone()
                    if existing_row and existing_row.get("quote_filepath"):
                        existing_filepath_str = existing_row["quote_filepath"]
            except Exception as e:
                print(f"Error fetching existing PO filepath: {e}")

        existing_urls = [u.strip() for u in existing_filepath_str.split(",") if u.strip()]

        # Filter out any files marked for deletion from the frontend UI
        if deleted_files_raw:
            deleted_list = [df.strip() for df in deleted_files_raw.split(",") if df.strip()]
            existing_urls = [
                url for url in existing_urls 
                if not any(df in url for df in deleted_list)
            ]

        raw_files = request.files.getlist('attach_quotes') or request.files.getlist('quote_attachment')
        new_urls = []
        gemini_file_payloads = []

        for quote_file in raw_files:
            if quote_file and quote_file.filename:
                try:
                    file_bytes = quote_file.read()
                    safe_filename = secure_filename(quote_file.filename)
                    
                    blob_response = vercel_blob.put(
                        f"quotes/{safe_filename}", 
                        file_bytes, 
                        options={
                            "access": "public",
                            "token": os.getenv("PUBLIC_BLOB_READ_WRITE_TOKEN"),
                            "addRandomSuffix": True
                        }
                    )
                    
                    url = blob_response.get('url') if isinstance(blob_response, dict) else getattr(blob_response, 'url', None)
                    if url:
                        new_urls.append(url)

                    gemini_file_payloads.append((
                        file_bytes, 
                        safe_filename, 
                        quote_file.mimetype
                    ))
                except Exception as upload_err:
                    print(f"File Processing Error ({quote_file.filename}): {upload_err}")

        all_urls = existing_urls + new_urls
        combined_quote_filepath = ",".join(all_urls) if all_urls else None
        quotes_provided = len(all_urls)

        print(f"DEBUG -> Existing String: {existing_filepath_str}")
        print(f"DEBUG -> New URLs: {new_urls}")
        print(f"DEBUG -> Combined Filepath to Save: {combined_quote_filepath}") 

        # Run AI Analysis if explicitly selected or if new files were uploaded
        if submission_status == "Run AI Analysis" or gemini_file_payloads:
            if not gemini_file_payloads and combined_quote_filepath:
                for file_url in combined_quote_filepath.split(','):
                    clean_url = file_url.strip()
                    if clean_url:
                        try:
                            resp = requests.get(clean_url, timeout=10)
                            if resp.status_code == 200:
                                filename = clean_url.split('/')[-1].split('?')[0]
                                content_type = resp.headers.get('Content-Type', 'application/pdf')
                                gemini_file_payloads.append((resp.content, filename, content_type))
                        except Exception as fetch_err:
                            print(f"Error fetching existing attachment for AI analysis: {fetch_err}")

            if gemini_file_payloads:
                ai_summary = analyze_po_with_gemini(gemini_file_payloads, request.form)
                if ai_summary:
                    ai_recommendation_summary = ai_summary
                    

        if new_po:
            try:
                with conn.cursor() as cur:
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
                                quote_filepath = %s,
                                chair_requested_at = CASE WHEN %s IN ('Submit for Finance Review','Submit for Approval', 'Sent') THEN NOW() ELSE chair_requested_at END,
                                chair_escalated = CASE WHEN %s IN ('Submit for Finance Review','Submit for Approval', 'Sent') THEN FALSE ELSE chair_escalated END
                            WHERE po_number = %s;
                        """, (
                            new_po, description, po_date, is_budgeted, gl_code, gl_code_id, 
                            expense_type, estimated_cost, recommended_vendor, justification_notes, 
                            submission_status, system_status, quotes_provided, actioned_date, 
                            actioned_by, approval_notes, ai_recommendation_summary, 
                            combined_quote_filepath, submission_status, submission_status, original_po
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

                    # Run AI Analysis if explicitly selected or if new files were uploaded
                    if submission_status == "Run AI Analysis" or gemini_file_payloads:
                        # If no new files were uploaded during this submit, fetch existing attached files from Blob storage
                        if not gemini_file_payloads and combined_quote_filepath:
                            for file_url in combined_quote_filepath.split(','):
                                clean_url = file_url.strip()
                                if clean_url:
                                    try:
                                        resp = requests.get(clean_url, timeout=10)
                                        if resp.status_code == 200:
                                            filename = clean_url.split('/')[-1].split('?')[0]
                                            content_type = resp.headers.get('Content-Type', 'application/pdf')
                                            gemini_file_payloads.append((resp.content, filename, content_type))
                                    except Exception as fetch_err:
                                        print(f"Error fetching existing attachment for AI analysis: {fetch_err}")

                        # Trigger Gemini analysis if files are present
                        if gemini_file_payloads:
                            ai_summary = analyze_po_with_gemini(gemini_file_payloads, request.form)
                            if ai_summary:
                                ai_recommendation_summary = ai_summary
                                # Persist the newly generated AI summary to the database
                                cur.execute("""
                                    UPDATE po_log 
                                    SET ai_recommendation_summary = %s 
                                    WHERE po_number = %s;
                                """, (ai_recommendation_summary, new_po))
                                conn.commit()

                    # Trigger GitHub Action dispatch if required
                    if submission_status in ["Run AI Analysis", "Submit for Finance Review", "Submit for Approval", "Sent"]:
                        trigger_github_workflow()

                return redirect(url_for('po_form', po_number=new_po))

            except Exception as e:
                conn.rollback()
                message = f"Error saving purchase order: {e}"

    # Check URL args first, then fall back to form data if it's a POST request
    selected_po_num = request.args.get("po_number") or request.form.get("po_number")
    audit_logs = []

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT gl_code, description FROM master_budget ORDER BY gl_code ASC;")
            gl_records = cur.fetchall()

            cur.execute("SELECT po_number, description FROM po_log ORDER BY created_at DESC;")
            saved_pos = cur.fetchall()
            
            if selected_po_num:
                cur.execute("SELECT * FROM po_log WHERE po_number = %s;", (selected_po_num,))
                selected_po = cur.fetchone()

                if selected_po:
                    raw_filepath = selected_po.get('quote_filepath') or ''
                    selected_po['quote_urls'] = [
                        u.strip() for u in raw_filepath.split(',') if u.strip()
                    ]

                   # Fetch audit trail history for this PO
                    try:
                        target_po = str(selected_po.get('po_number', '')).strip()
                        print(f"DEBUG -> Querying audit logs for trimmed PO number: '{target_po}'")
                        
                        cur.execute(
                            """
                            SELECT timestamp AS action_timestamp, actor_email, action_type, system_notes AS notes 
                            FROM workflow_control_log 
                            WHERE TRIM(po_number) ILIKE TRIM(%s) 
                            ORDER BY timestamp DESC
                            """,
                            (target_po,)
                        )
                        audit_logs = cur.fetchall()
                        print(f"DEBUG -> Successfully fetched {len(audit_logs)} audit log entries.")
                    except Exception as audit_err:
                        print(f"Error fetching audit logs: {audit_err}")
                        audit_logs = []

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
        audit_logs=audit_logs,
        message=message
    )

def analyze_po_with_gemini(uploaded_files_data, form_data, financial_data=None, custom_prompt=None):
    """Sends uploaded files and financial context to Gemini using the database system prompt or fallback template."""
    if not GEMINI_AVAILABLE or not os.getenv("GEMINI_API_KEY"):
        return "Gemini AI SDK is not installed or GEMINI_API_KEY is missing."

    # 1. Fetch active prompt and model configuration from system_prompts table
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT prompt_template, selected_model, is_active 
                FROM system_prompts 
                WHERE LOWER(process) = LOWER('Quote evaluation');
            """)
            prompt_row = cur.fetchone()
    except Exception as e:
        print(f"Database error fetching quote evaluation prompt: {e}", flush=True)
        prompt_row = None
    finally:
        if conn:
            conn.close()

    # 2. Extract configuration or use defaults/fallback
    is_active = prompt_row.get('is_active', True) if prompt_row else True
    if not is_active:
        return "AI Quote Evaluation is currently inactive in system prompts."

    prompt_template = (prompt_row.get('prompt_template') if prompt_row else None)
    model_name = (prompt_row.get('selected_model') if prompt_row else None) or "gemini-3.5-flash"

    api_key = os.getenv("GEMINI_API_KEY")
    
    try:
        client = genai.Client(api_key=api_key)
    except Exception as e:
        return f"Client initialization failed: {str(e)}"

    gemini_file_objects = []
    
    # Fallback or auto-fetch financial data using the helper function if not provided
    if not financial_data:
        gl_code = form_data.get('gl_code')
        financial_data = fetch_budget_summary(gl_code) if gl_code else {}

    # Extract variance and estimated cost
    variance = float(financial_data.get('variance', 0.0) or 0.0)
    estimated_cost = float(form_data.get('estimated_cost', 0.0) or 0.0)

    # Calculate remaining YTD budget as variance minus estimated cost
    remaining_budget = variance - estimated_cost
    
    try:
        # 3. Upload files using the files client API
        for file_bytes, filename, mime_type in uploaded_files_data:
            bio = io.BytesIO(file_bytes)
            bio.name = filename
            
            uploaded_gemini_file = client.files.upload(
                file=bio, 
                config={"mime_type": mime_type or "application/pdf"}
            )
            gemini_file_objects.append(uploaded_gemini_file)

        # 4. Format prompt template with comprehensive form and financial context variables
        prompt_context = {
            "po_num": form_data.get('po_number', 'N/A'),
            "desc_val": form_data.get('description', 'N/A'),
            "expense_type": form_data.get('expense_type', 'N/A'),
            "gl_code_val": form_data.get('gl_code', 'N/A'),
            "cost": float(form_data.get('estimated_cost', 0.0) or 0.0),
            "total_annual_budget": float(financial_data.get('total_annual_budget', 0.0) or 0.0),
            "ytd_actual": float(financial_data.get('ytd_actual', 0.0) or 0.0),
            "budget_ytd": float(financial_data.get('budget_ytd', 0.0) or 0.0),
            "remaining_budget": remaining_budget,
            "user_recommended_vendor": form_data.get('recommended_vendor', 'N/A'),
            "user_justification": form_data.get('justification_notes', 'N/A'),
            "coi_details": form_data.get('coi_details', 'None declared'),
            "approval_requirement": form_data.get('approval_requirement'),
            "system_status": form_data.get('system_status')
        }
        
        formatted_prompt = prompt_template.format(**prompt_context)

        # 5. Execute generation using the selected model
        print(f"Attempting analysis with model: {model_name}...", flush=True)
        
        response = None
        for attempt in range(3):  # Retry up to 3 times for temporary 503 high demand
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[*gemini_file_objects, formatted_prompt]
                )
                if response and response.text:
                    return response.text.strip()
            except Exception as err:
                if "503" in str(err) or "UNAVAILABLE" in str(err):
                    wait_time = (attempt + 1) * 2
                    print(f"503 High Demand on {model_name}. Retrying in {wait_time}s...", flush=True)
                    time.sleep(wait_time)
                else:
                    print(f"Gemini error on {model_name}: {err}", flush=True)
                    break

        return "AI Analysis temporarily unavailable due to high Google API demand. Please resubmit shortly."

    except Exception as err:
        print(f"Gemini Processing Error: {err}", flush=True)
        return f"AI Analysis failed: {str(err)}"
        
    finally:
        # Cleanup uploaded files from Google servers
        for file_obj in gemini_file_objects:
            try:
                client.files.delete(name=file_obj.name)
            except Exception as e:
                print(f"Gemini File Cleanup Error: {e}", flush=True)


# --- SYSTEM PROMPTS & AUDIT ---
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
                
                if not process_name:
                    cursor.execute("SELECT DISTINCT process FROM system_prompts ORDER BY process ASC;")
                    rows = cursor.fetchall()
                    processes = [row['process'] for row in rows]
                    return jsonify(processes), 200

                cursor.execute(
                    "SELECT process, prompt_template, description, is_active, selected_model FROM system_prompts WHERE LOWER(process) = LOWER(%s);", 
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

                # 1. Prioritize model selected in frontend payload
                model_name = data.get('selected_model')

                # 2. Fall back to existing database value if payload didn't provide one
                if not model_name:
                    cursor.execute(
                        "SELECT selected_model FROM system_prompts WHERE LOWER(process) = LOWER(%s);", 
                        (process_name,)
                    )
                    row = cursor.fetchone()
                    model_name = (row.get('selected_model') if row else None) or 'gemini-3.6-flash'

                # 3. Update database with new prompt configuration
                cursor.execute("""
                    UPDATE system_prompts 
                    SET prompt_template = %s, 
                        is_active = %s,
                        selected_model = %s,
                        updated_at = CURRENT_TIMESTAMP 
                    WHERE LOWER(process) = LOWER(%s);
                """, (prompt_template, is_active, model_name, process_name))
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


# --- PROJECTS & PROCUREMENT ---
@app.route("/projects", methods=["GET"])
@app.route("/projects/", methods=["GET"])
@app.route("/projects/<int:project_id>", methods=["GET"])
def projects_page(project_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()

    all_projects = get_all_projects_summary(cursor)

    # Always fetch master budget GL records for the dropdown
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
        total_cost = vendor.get('total_cost', 0.0)
        quantity = vendor.get('option_quantity', 1.0) or 1.0  # Prevent division by zero
        vendor['total_effective_rate'] = total_cost / quantity
        vendor['projected_5yr_total'] = vendor.get('total_effective_rate') or vendor.get('quote_total') or 0.0

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
        all_projects=all_projects,
        gl_records=gl_records
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
    return redirect(url_for("projects_page", project_id=new_id))


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

@app.route('/view-quote/<int:option_id>')
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

@app.route("/draft-matrix/<int:project_id>", methods=["POST"])
def draft_matrix(project_id):
    try:
        raw_weight = float(request.form.get("price_weighting", 30))
    except (ValueError, TypeError):
        raw_weight = 30.0

    price_weighting = raw_weight / 100.0 if raw_weight > 1.0 else raw_weight
    prompt_adjustments = request.form.get("ai_prompt_adjustments", "")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE projects 
        SET ai_prompt_adjustments = %s, price_weighting = %s, latest_ai_status = 'Processing AI Matrix...' 
        WHERE id = %s;
    """, (prompt_adjustments, price_weighting, project_id))
    conn.commit()
    cursor.close()
    conn.close()

    try:
        execute_phase1(project_id)
        flash("AI Matrix Draft completed successfully.")
    except Exception as e:
        flash(f"Error during AI drafting: {str(e)}")

    return redirect(url_for("projects_page", project_id=project_id))


@app.route("/recalculate-matrix/<int:project_id>", methods=["POST"])
def recalculate_matrix(project_id):
    try:
        raw_weight = float(request.form.get("price_weighting", 30))
    except (ValueError, TypeError):
        raw_weight = 30.0

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

    try:
        execute_phase2(project_id)
        flash("Matrix recalculated with updated weightings.")
    except Exception as e:
        flash(f"Error recalculating matrix: {str(e)}")

    return redirect(url_for("projects_page", project_id=project_id))


@app.route("/update-project/<int:project_id>", methods=["POST"])
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
    
    raw_weight = float(request.form.get("price_weighting", 30))
    price_weighting = raw_weight / 100.0 if raw_weight > 1.0 else raw_weight

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            UPDATE projects 
            SET project_reference = %s,
                name = %s,
                project_description = %s,
                project_objective = %s,
                ai_prompt_adjustments = %s,
                phase1_prompt_adjustments = %s,
                gl_code = %s,
                gl_title = %s,
                gl_sub = %s,
                price_weighting = %s,
                executive_sourcing_recommendation = %s
            WHERE id = %s;
        """, (
            project_reference, name, project_description, project_objective, 
            ai_prompt_adjustments, phase1_prompt_adjustments, gl_code, gl_title, gl_sub, 
            price_weighting, executive_sourcing_recommendation, project_id
        ))

        for key, value in request.form.items():
            if key.startswith("pricing_name_"):
                item_id = key.replace("pricing_name_", "")
                cursor.execute("""
                    UPDATE options_line_items_pricing 
                    SET cost_component_name = %s 
                    WHERE id = %s;
                """, (value, item_id))

            elif key.startswith("pricing_amount_") and not key.startswith("pricing_amount_new_"):
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

            elif key.startswith("weighting_"):
                criteria_id = key.replace("weighting_", "")
                weight_val = float(value) if value else 0.0
                cursor.execute("""
                    UPDATE project_weightings 
                    SET weighting_percent = %s 
                    WHERE id = %s;
                """, (weight_val, criteria_id))

            elif key.startswith("score_"):
                # Parses format: score_{criteria_id}_{vendor_id}
                parts = key.split("_")
                if len(parts) == 3:
                    criteria_id = parts[1]
                    vendor_id = parts[2]
                    score_val = float(value) if value else 0.0

                    # Fetch current weighting percentage from project_weightings to calculate contribution
                    cursor.execute("SELECT weighting_percent FROM project_weightings WHERE id = %s;", (criteria_id,))
                    res = cursor.fetchone()
                    weight = float(res[0]) if res and res[0] is not None else 0.0
                    weighted_contrib = score_val * (weight / 100.0)

                    # Check if row exists in options_line_items_non_pricing
                    cursor.execute("""
                        SELECT id FROM options_line_items_non_pricing 
                        WHERE weighting_id = %s AND procurement_option_id = %s;
                    """, (criteria_id, vendor_id))
                    row = cursor.fetchone()

                    if row:
                        cursor.execute("""
                            UPDATE options_line_items_non_pricing 
                            SET score = %s, weighted_score_contribution = %s 
                            WHERE weighting_id = %s AND procurement_option_id = %s;
                        """, (score_val, weighted_contrib, criteria_id, vendor_id))
                    else:
                        line_item_id = f"np_{criteria_id}_{vendor_id}"
                        cursor.execute("""
                            INSERT INTO options_line_items_non_pricing 
                            (line_item_id, procurement_option_id, weighting_id, score, weighted_score_contribution)
                            VALUES (%s, %s, %s, %s, %s);
                        """, (line_item_id, vendor_id, criteria_id, score_val, weighted_contrib))

            elif key.startswith("option_quantity_"):
                opt_id = key.replace("option_quantity_", "")
                qty_val = float(value) if value else 0.0
                cursor.execute("""
                    UPDATE procurement_options 
                    SET total_quantity = %s 
                    WHERE id = %s;
                """, (qty_val, opt_id))

            elif key.startswith("option_unit_"):
                opt_id = key.replace("option_unit_", "")
                cursor.execute("""
                    UPDATE procurement_options 
                    SET unit_of_measure = %s 
                    WHERE id = %s;
                """, (value, opt_id))

        conn.commit()
        flash("Project definitions and matrix line items updated successfully.")
    except Exception as e:
        conn.rollback()
        flash(f"Error updating project: {str(e)}")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for("projects_page", project_id=project_id))


@app.route('/generate-recommendation/<int:project_id>', methods=['POST'])
def generate_recommendation(project_id):
    if not GEMINI_AVAILABLE:
        flash('Gemini API library is not installed or configured.')
        return redirect(url_for('projects_page', project_id=project_id))

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM projects WHERE id = %s;", (project_id,))
            project = cursor.fetchone()

            if not project:
                flash('Project not found.')
                return redirect(url_for('projects_page', project_id=project_id))

            cursor.execute("SELECT * FROM procurement_options WHERE project_id = %s;", (project_id,))
            vendors = cursor.fetchall()

            if not vendors:
                flash('Cannot generate recommendation without vendor options.')
                return redirect(url_for('projects_page', project_id=project_id))

            cursor.execute("""
                SELECT prompt_template, selected_model 
                FROM system_prompts 
                WHERE process = 'executive_recommendation' AND is_active = TRUE;
            """)
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
            raw_score = winner.get('final_weighted_score_output') or winner.get('weighted_score') or 0.0
            winning_score = f"{float(raw_score):.1f}"

            cheapest_vendor = cheapest.get('vendor_name', 'N/A') if cheapest else "N/A"
            raw_cost = cheapest.get('projected_5yr_total') or cheapest.get('total_cost') or 0
            min_cost = f"{int(round(float(raw_cost))):,}"

            formatted_task_prompt = prompt_template.format(
                winner_name=winner_name,
                winning_score=winning_score,
                cheapest_vendor=cheapest_vendor,
                min_cost=min_cost
            )

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

            model_name = (rec_row.get('selected_model') if rec_row else None) or 'gemini-3.6-flash'
            model = genai.GenerativeModel(model_name)
            
            response = model.generate_content(full_prompt)
            generated_text = response.text if response and response.text else "No content generated."

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


# --- MINUTES ---
from datetime import date

@app.route('/minutes', methods=['GET', 'POST'])
def finance_minutes():
    conn = get_db_connection()
    
    if request.method == 'POST':
        meeting_date = request.form.get('meeting_date')
        chairperson = request.form.get('chairperson')
        attendees = request.form.get('attendees')
        apologies = request.form.get('apologies')
        notes_summary = request.form.get('notes_summary')

        with conn.cursor() as cursor:
            # Insert the new meeting record and return its new ID
            cursor.execute("""
                INSERT INTO meeting_minutes (meeting_date, chairperson, attendees, apologies, notes_summary)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
            """, (meeting_date, chairperson, attendees, apologies, notes_summary))
            
            row = cursor.fetchone()
            meeting_id = row['id'] if isinstance(row, dict) else row[0]

            # Save initial action items submitted with the form
            descriptions = request.form.getlist('action_description[]')
            responsibles = request.form.getlist('responsible_person[]')
            targets = request.form.getlist('target_date[]')

            for i in range(len(descriptions)):
                desc = descriptions[i]
                if desc and desc.strip():  # Only insert if there's text
                    resp = responsibles[i] if i < len(responsibles) else ''
                    raw_target = targets[i] if i < len(targets) else None
                    target = raw_target if (raw_target and raw_target.strip()) else (meeting_date or str(date.today()))

                    cursor.execute("""
                        INSERT INTO meeting_action_items (meeting_id, action_description, responsible_person, target_date, status)
                        VALUES (%s, %s, %s, %s, 'Pending');
                    """, (meeting_id, desc, resp, target))

            conn.commit()
        conn.close()
        return redirect(url_for('finance_minutes'))

    # GET request: Load existing meetings and their action items
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT id, meeting_date, chairperson, attendees, apologies, notes_summary 
            FROM meeting_minutes 
            ORDER BY meeting_date DESC;
        """)
        meetings = cursor.fetchall()
        
        for m in meetings:
            cursor.execute("""
                SELECT id, action_description, responsible_person, target_date, status, action_notes 
                FROM meeting_action_items 
                WHERE meeting_id = %s ORDER BY id ASC;
            """, (m['id'],))
            m['action_items'] = cursor.fetchall()

    conn.close()
    return render_template('minutes.html', meetings=meetings)

@app.route('/delete_meeting', methods=['POST'])
def delete_meeting():
    meeting_id = request.form.get('meeting_id')

    conn = get_db_connection()
    with conn.cursor() as cursor:
        # Delete associated action items first to maintain referential integrity
        cursor.execute("DELETE FROM meeting_action_items WHERE meeting_id = %s;", (meeting_id,))
        
        # Delete the meeting record itself
        cursor.execute("DELETE FROM meeting_minutes WHERE id = %s;", (meeting_id,))
        
        conn.commit()
    conn.close()

    return redirect(url_for('finance_minutes'))

@app.route('/update_action_item', methods=['POST'])
def update_action_item():
    action_id = request.form.get('action_id')
    action_description = request.form.get('action_description')
    responsible_person = request.form.get('responsible_person')
    target_date_val = request.form.get('target_date')
    status = request.form.get('status')
    action_notes = request.form.get('action_notes')

    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("""
            UPDATE meeting_action_items
            SET action_description = %s,
                responsible_person = %s,
                target_date = %s,
                status = %s,
                action_notes = %s
            WHERE id = %s;
        """, (action_description, responsible_person, target_date_val, status, action_notes, action_id))
        conn.commit()
    conn.close()

    return redirect(url_for('finance_minutes'))


@app.route('/update_meeting_minutes', methods=['POST'])
def update_meeting_minutes():
    meeting_id = request.form.get('meeting_id')
    meeting_date = request.form.get('meeting_date')
    chairperson = request.form.get('chairperson')
    attendees = request.form.get('attendees')
    apologies = request.form.get('apologies')
    notes_summary = request.form.get('notes_summary')

    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("""
            UPDATE meeting_minutes
            SET meeting_date = %s,
                chairperson = %s,
                attendees = %s,
                apologies = %s,
                notes_summary = %s
            WHERE id = %s;
        """, (meeting_date, chairperson, attendees, apologies, notes_summary, meeting_id))

        delete_action_ids = request.form.getlist('delete_action_id[]')
        for act_id in delete_action_ids:
            if act_id:
                cursor.execute("DELETE FROM meeting_action_items WHERE id = %s;", (act_id,))

        action_ids = request.form.getlist('existing_action_id[]')
        descriptions = request.form.getlist('existing_action_description[]')
        responsibles = request.form.getlist('existing_responsible_person[]')
        targets = request.form.getlist('existing_target_date[]')

        for i in range(len(descriptions)):
            act_id = action_ids[i] if (i < len(action_ids) and action_ids[i]) else None
            desc = descriptions[i]
            resp = responsibles[i]
            
            raw_target = targets[i] if i < len(targets) else None
            target = raw_target if (raw_target and raw_target.strip()) else (meeting_date or str(date.today()))

            if act_id:
                cursor.execute("""
                    UPDATE meeting_action_items
                    SET action_description = %s,
                        responsible_person = %s,
                        target_date = %s
                    WHERE id = %s;
                """, (desc, resp, target, act_id))
            else:
                cursor.execute("""
                    INSERT INTO meeting_action_items (meeting_id, action_description, responsible_person, target_date, status)
                    VALUES (%s, %s, %s, %s, 'Pending');
                """, (meeting_id, desc, resp, target))

        conn.commit()
    conn.close()

    return redirect(url_for('finance_minutes'))


@app.route('/update_authority_matrix', methods=['POST'])
def update_authority_matrix():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # 1. Handle Deletions
            delete_ids = request.form.getlist('delete_matrix_id[]')
            for del_id in delete_ids:
                if del_id:
                    cursor.execute("DELETE FROM approval_matrix WHERE id = %s;", (del_id,))

            # 2. Handle Updates to Existing Rows
            row_ids = request.form.getlist('existing_matrix_id[]')
            categories = request.form.getlist('existing_matrix_category[]')
            descriptions = request.form.getlist('existing_description[]')
            min_values = request.form.getlist('existing_min_value[]')
            max_values = request.form.getlist('existing_max_value[]')
            reviewer_roles = request.form.getlist('existing_reviewer_roles[]')
            quotes_req = request.form.getlist('existing_quotes_required[]')
            approver_roles = request.form.getlist('existing_approver_roles[]')
            applicable_controls = request.form.getlist('existing_applicable_controls[]')
            audit_reqs = request.form.getlist('existing_compliance_audit_requirement[]')

            for i in range(len(row_ids)):
                r_id = row_ids[i]
                min_val = float(min_values[i]) if (i < len(min_values) and min_values[i].strip()) else 0.0
                max_val = float(max_values[i]) if (i < len(max_values) and max_values[i].strip()) else None
                quotes = int(quotes_req[i]) if (i < len(quotes_req) and quotes_req[i].strip()) else None
                
                cat = categories[i] if i < len(categories) else ''
                desc = descriptions[i] if i < len(descriptions) else None
                rev = reviewer_roles[i] if i < len(reviewer_roles) else None
                app_roles = approver_roles[i] if i < len(approver_roles) else None
                controls = applicable_controls[i] if i < len(applicable_controls) else None
                audit = audit_reqs[i] if i < len(audit_reqs) else None

                cursor.execute("""
                    UPDATE approval_matrix
                    SET matrix_category = %s,
                        description = %s,
                        min_value = %s,
                        max_value = %s,
                        reviewer_roles = %s,
                        quotes_required = %s,
                        approver_roles = %s,
                        applicable_controls = %s,
                        compliance_audit_requirement = %s
                    WHERE id = %s;
                """, (
                    cat, desc, min_val, max_val,
                    rev, quotes, app_roles,
                    controls, audit, r_id
                ))

            # 3. Handle Insertion of Newly Added Tier Rows
            new_categories = request.form.getlist('new_matrix_category[]')
            new_descriptions = request.form.getlist('new_description[]')
            new_min_values = request.form.getlist('new_min_value[]')
            new_max_values = request.form.getlist('new_max_value[]')
            new_reviewer_roles = request.form.getlist('new_reviewer_roles[]')
            new_quotes_req = request.form.getlist('new_quotes_required[]')
            new_approver_roles = request.form.getlist('new_approver_roles[]')
            new_applicable_controls = request.form.getlist('new_applicable_controls[]')
            new_audit_reqs = request.form.getlist('new_compliance_audit_requirement[]')

            for i in range(len(new_categories)):
                if not new_categories[i].strip():
                    continue

                min_val = float(new_min_values[i]) if (i < len(new_min_values) and new_min_values[i].strip()) else 0.0
                max_val = float(new_max_values[i]) if (i < len(new_max_values) and new_max_values[i].strip()) else None
                quotes = int(new_quotes_req[i]) if (i < len(new_quotes_req) and new_quotes_req[i].strip()) else None

                cat = new_categories[i]
                desc = new_descriptions[i] if i < len(new_descriptions) else None
                rev = new_reviewer_roles[i] if i < len(new_reviewer_roles) else None
                app_roles = new_approver_roles[i] if i < len(new_approver_roles) else None
                controls = new_applicable_controls[i] if i < len(new_applicable_controls) else None
                audit = new_audit_reqs[i] if i < len(new_audit_reqs) else None

                cursor.execute("""
                    INSERT INTO approval_matrix (
                        matrix_category, description, min_value, max_value,
                        reviewer_roles, quotes_required, approver_roles,
                        applicable_controls, compliance_audit_requirement
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    cat, desc, min_val, max_val,
                    rev, quotes, app_roles,
                    controls, audit
                ))

            conn.commit()
            flash("Approval & Authority Matrix updated successfully.")
    except Exception as e:
        conn.rollback()
        flash(f"Error updating matrix: {str(e)}")
    finally:
        conn.close()

    return redirect(url_for('render_guide_page'))


@app.route('/delete_action_item', methods=['POST'])
def delete_action_item():
    action_id = request.form.get('action_id')
    if action_id:
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM meeting_action_items WHERE id = %s;", (action_id,))
            conn.commit()
            flash("Action item deleted successfully.")
        except Exception as e:
            conn.rollback()
            flash(f"Error deleting action item: {str(e)}")
        finally:
            conn.close()
    return redirect(url_for('finance_minutes'))


ALLOWED_MONTHS = {
    'mar_2026': 'March 2026',
    'apr_2026': 'April 2026',
    'may_2026': 'May 2026',
    'jun_2026': 'June 2026',
    'jul_2026': 'July 2026',
    'aug_2026': 'August 2026'
}

@app.route('/expenditure_expose')
def expenditure_expose():
    selected_month = request.args.get('month', 'aug_2026')
    if selected_month not in ALLOWED_MONTHS:
        selected_month = 'aug_2026'

    all_items = []
    cached_analysis = None

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. Check for cached AI analysis first
        cur.execute("""
            SELECT analysis_text 
            FROM public.expenditure_ai_cache 
            WHERE month_code = %s;
        """, (selected_month,))
        cache_row = cur.fetchone()
        if cache_row:
            cached_analysis = cache_row['analysis_text']

        # 2. Fetch line items excluding rolled-up parent items ending in /000
        query = f"""
            SELECT gl_code, description, 
                   COALESCE({selected_month}, 0) AS selected_month_actual,
                   COALESCE(ytd, 0) AS ytd, 
                   COALESCE(budget_ytd, 0) AS budget_ytd, 
                   COALESCE(variance, 0) AS variance, 
                   COALESCE(total_budget, 0) AS total_budget
            FROM public.master_budget
            WHERE gl_code NOT LIKE '%/000'
            ORDER BY gl_code ASC;
        """
        
        cur.execute(query)
        all_items = cur.fetchall()


        # 3. Fetch Cashbook Compliance Audit Register & Group by Verdict Category
        cur.execute("""
            SELECT 
                transaction_date,
                transaction_text,
                amount,
                gl_code,
                supplier_code,
                authority_tier,
                min_quotes_required,
                po_number,
                quotes_attached,
                compliance_verdict
            FROM vw_cashbook_compliance_audit
            ORDER BY transaction_date DESC;
        """)
        raw_audit_rows = cur.fetchall()

        # Group audit items by category and compute aggregate Rand totals
        grouped_audit_raw = defaultdict(list)
        category_totals = defaultdict(float)

        for row in raw_audit_rows:
            verdict = row['compliance_verdict']
            grouped_audit_raw[verdict].append(row)
            category_totals[verdict] += float(abs(row['amount']))

        # Define priority order (Critical first, Exempt/Payroll last)
        VERDICT_ORDER = {
            'NON-COMPLIANT: Missing Approved PO': 1,
            'FLAGGED: Unbudgeted / Suspense Allocation': 2,
            'FLAGGED: Anti-Splitting Suspected': 3,
            'COMPLIANT: Petty/Minor Spend (No PO Required)': 4,
            'COMPLIANT': 5,
            'COMPLIANT: Exempt (Payroll Spend)': 6
        }

        # Sort dictionary keys based on priority rank
        sorted_categories = sorted(
            grouped_audit_raw.keys(),
            key=lambda cat: VERDICT_ORDER.get(cat, 99)
        )

        # Build ordered dictionary for Jinja template
        grouped_audit = {cat: grouped_audit_raw[cat] for cat in sorted_categories}

        cur.close()
        conn.close()
    except Exception as e:
        flash(f"Database error: {str(e)}", "danger")

    # Categorize items by series prefix
    income_items = [item for item in all_items if str(item['gl_code']).startswith('1')]
    expenditure_items = [item for item in all_items if str(item['gl_code']).startswith('2')]

    over_budget_expenditure = []
    for item in expenditure_items:
        ytd = float(item['ytd'])
        budget_ytd = float(item['budget_ytd'])
        
        if ytd > budget_ytd:
            item_dict = dict(item)
            # Round variance to 0 decimal places
            item_dict['variance'] = round(ytd - budget_ytd)
            # Round percentage to 0 decimal places
            item_dict['ytd_pct'] = round((ytd / budget_ytd * 100.0)) if budget_ytd > 0 else 0
            over_budget_expenditure.append(item_dict)

    # Sort descending from highest numerical variance to lowest
    over_budget_expenditure.sort(key=lambda x: x['variance'], reverse=True)

    # Calculate positive income shortfall magnitude and sort descending
    under_budget_income = []
    for item in income_items:
        ytd = float(item['ytd'])
        budget_ytd = float(item['budget_ytd'])
        
        if ytd < budget_ytd:
            item_dict = dict(item)
            # Round variance shortfall to 0 decimal places
            item_dict['variance'] = round(budget_ytd - ytd)
            # Round percentage to 0 decimal places
            item_dict['ytd_pct'] = round((ytd / budget_ytd * 100.0)) if budget_ytd > 0 else 0
            under_budget_income.append(item_dict)

    # Sort from largest income shortfall to smallest
    under_budget_income.sort(key=lambda x: x['variance'], reverse=True)


    # Macro totals
    total_ytd_income = sum(float(item['ytd']) for item in income_items)
    total_ytd_income_budget = sum(float(item['budget_ytd']) for item in income_items)
    
    total_ytd_expenditure = sum(float(item['ytd']) for item in expenditure_items)
    total_ytd_expenditure_budget = sum(float(item['budget_ytd']) for item in expenditure_items)

    total_selected_month_spend = sum(float(item['selected_month_actual']) for item in expenditure_items)
    total_expenditure_overrun = sum(float(item['variance']) for item in over_budget_expenditure)
    total_expenditure_variance = total_ytd_expenditure - total_ytd_expenditure_budget

    month_label = ALLOWED_MONTHS[selected_month]

    # 3. Handle AI Analysis generation or use cached version
    if cached_analysis:
        ai_analysis = cached_analysis
    elif GEMINI_AVAILABLE and all_items:
        conn_ai = None
        try:
            conn_ai = get_db_connection()
            cur_ai = conn_ai.cursor()

            # Query template and selected model from database
            cur_ai.execute("""
                SELECT prompt_template, selected_model 
                FROM public.system_prompts 
                WHERE LOWER(process) = LOWER('expenditure_expose') AND is_active = TRUE;
            """)
            prompt_row = cur_ai.fetchone()

            if not prompt_row or not prompt_row.get('prompt_template'):
                ai_analysis = "AI analysis is currently disabled or template is missing in system_prompts."
            else:
                income_summary = "\n".join([
                    f"- GL {i['gl_code']} ({i['description']}): YTD Actual R{float(i['ytd']):,.2f} vs Budget R{float(i['budget_ytd']):,.2f}"
                    for i in income_items
                ])
                
                expenditure_summary = "\n".join([
                    f"- GL {e['gl_code']} ({e['description']}): YTD Actual R{float(e['ytd']):,.2f} vs Budget R{float(e['budget_ytd']):,.2f} (Variance: R{float(e['variance']):,.2f})"
                    for e in expenditure_items
                ])

                prompt_template = prompt_row['prompt_template']

                # Format template with runtime values
                formatted_prompt = prompt_template.format(
                    month_label=month_label,
                    total_ytd_income=total_ytd_income,
                    total_ytd_income_budget=total_ytd_income_budget,
                    income_summary=income_summary,
                    total_ytd_expenditure=total_ytd_expenditure,
                    total_ytd_expenditure_budget=total_ytd_expenditure_budget,
                    total_expenditure_overrun=total_expenditure_overrun,
                    expenditure_summary=expenditure_summary
                )

                # Dynamically instantiate the model chosen in the frontend
                model_name = prompt_row.get('selected_model') or 'gemini-3.6-flash'
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(formatted_prompt)
                ai_analysis = response.text

                # Save generated response to cache database
                cur_ai.execute("""
                    INSERT INTO public.expenditure_ai_cache (month_code, analysis_text)
                    VALUES (%s, %s)
                    ON CONFLICT (month_code) 
                    DO UPDATE SET analysis_text = EXCLUDED.analysis_text, created_at = CURRENT_TIMESTAMP;
                """, (selected_month, ai_analysis))
                conn_ai.commit()

        except Exception as e:
            ai_analysis = f"AI Analysis temporarily unavailable: {str(e)}"
        finally:
            if conn_ai:
                conn_ai.close()
    else:
        ai_analysis = "AI analysis is currently disabled or unavailable."
    
    return render_template(
        'expenditure_expose.html',
        income_items=income_items,
        expenditure_items=expenditure_items,
        over_budget_expenditure=over_budget_expenditure,
        under_budget_income=under_budget_income,
        total_ytd_income=total_ytd_income,
        total_ytd_income_budget=total_ytd_income_budget,
        total_ytd_expenditure=total_ytd_expenditure,
        total_ytd_expenditure_budget=total_ytd_expenditure_budget,
        total_selected_month_spend=total_selected_month_spend,
        total_expenditure_overrun=total_expenditure_overrun,
        total_expenditure_variance=total_expenditure_variance,
        selected_month=selected_month,
        allowed_months=ALLOWED_MONTHS,
        ai_analysis=ai_analysis,
        is_cached=bool(cached_analysis),
        grouped_audit=grouped_audit,
        category_totals=category_totals
    )


@app.route('/expenditure_expose/refresh/<month>')
def refresh_expenditure_ai(month):
    if month in ALLOWED_MONTHS:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM public.expenditure_ai_cache WHERE month_code = %s;", (month,))
            conn.commit()
            cur.close()
            conn.close()
            flash(f"AI commentary cache cleared for {ALLOWED_MONTHS[month]}. Re-generating...", "info")
        except Exception as e:
            flash(f"Error clearing cache: {str(e)}", "danger")
    return redirect(url_for('expenditure_expose', month=month))

def check_and_dispatch_chair_escalations(db_connection):
    cursor = db_connection.cursor()

    # 1. Query POs pending Chair action for more than 6 hours
    escalation_query = """
        SELECT id, po_number, description, estimated_cost, recommended_vendor, chair_requested_at
        FROM public.po_log
        WHERE submission_status IN ('Submit for Approval', 'Sent')
          AND chair_escalated = FALSE
          AND chair_requested_at <= NOW() - INTERVAL '6 hours';
    """
    cursor.execute(escalation_query)
    overdue_pos = cursor.fetchall()

    for po in overdue_pos:
        po_id = po['id']
        po_num = po['po_number']
        requested_at = po['chair_requested_at']

        # 2. Dispatch Email / Notification Logic
        trigger_github_workflow()

        # 3. Mark as escalated so duplicate notifications are suppressed
        cursor.execute(
            """
            UPDATE public.po_log
            SET chair_escalated = TRUE
            WHERE id = %s;
        """,
            (po_id,),
        )

        # 4. Record escalation in workflow_control_log
        cursor.execute(
            """
            INSERT INTO public.workflow_control_log (po_number, log_entry, timestamp)
            VALUES (%s, %s, NOW());
        """,
            (
                po_num,
                f"Automated 6-Hour Escalation dispatched to Board Members. Requested at: {requested_at}",
            ),
        )

    db_connection.commit()

def get_board_escalation_recipients(conn):
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT DISTINCT TRIM(email) 
            FROM public.approvers 
            WHERE approval_permission LIKE '%Approval Authority%' 
               OR approval_permission LIKE '%Chair%';
        """)
        return [row['email'] for row in cursor.fetchall() if row.get('email')]


@app.route("/api/cron/check-escalations", methods=["GET", "POST"])
def cron_check_escalations():
    # Enforce basic security header if running on Vercel
    # auth_header = request.headers.get("Authorization")
    # if auth_header != f"Bearer {os.environ.get('CRON_SECRET')}":
    #     return {"error": "Unauthorized"}, 401

    conn = get_db_connection()
    try:
        check_and_dispatch_chair_escalations(conn)
        return {"status": "success", "message": "Escalation check completed"}, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)