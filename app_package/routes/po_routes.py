import os
import io
import time
import requests
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, flash
from werkzeug.utils import secure_filename
from app_package.db import get_db_connection

try:
    from google import genai
    GEMINI_AVAILABLE = True
except ModuleNotFoundError:
    genai = None
    GEMINI_AVAILABLE = False

po_bp = Blueprint('po', __name__)

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

@po_bp.route("/api/budget-details")
def budget_details():
    gl_code = request.args.get("gl_code")
    return jsonify(fetch_budget_summary(gl_code))

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

def analyze_po_with_gemini(uploaded_files_data, form_data, financial_data=None):
    if not GEMINI_AVAILABLE or not os.getenv("GEMINI_API_KEY"):
        return "Gemini AI SDK is not installed or GEMINI_API_KEY is missing."

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

    is_active = prompt_row.get('is_active', True) if prompt_row else True
    if not is_active:
        return "AI Quote Evaluation is currently inactive in system prompts."

    prompt_template = (prompt_row.get('prompt_template') if prompt_row else None)
    model_name = (prompt_row.get('selected_model') if prompt_row else None) or "gemini-3.5-flash"

    try:
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    except Exception as e:
        return f"Client initialization failed: {str(e)}"

    gemini_file_objects = []
    
    if not financial_data:
        gl_code = form_data.get('gl_code')
        financial_data = fetch_budget_summary(gl_code) if gl_code else {}

    variance = float(financial_data.get('variance', 0.0) or 0.0)
    estimated_cost = float(form_data.get('estimated_cost', 0.0) or 0.0)
    remaining_budget = variance - estimated_cost
    
    try:
        for file_bytes, filename, mime_type in uploaded_files_data:
            bio = io.BytesIO(file_bytes)
            bio.name = filename
            
            uploaded_gemini_file = client.files.upload(
                file=bio, 
                config={"mime_type": mime_type or "application/pdf"}
            )
            gemini_file_objects.append(uploaded_gemini_file)

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

        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[*gemini_file_objects, formatted_prompt]
                )
                if response and response.text:
                    return response.text.strip()
            except Exception as err:
                if "503" in str(err) or "UNAVAILABLE" in str(err):
                    time.sleep((attempt + 1) * 2)
                else:
                    break

        return "AI Analysis temporarily unavailable due to high Google API demand. Please resubmit shortly."

    except Exception as err:
        return f"AI Analysis failed: {str(err)}"
        
    finally:
        for file_obj in gemini_file_objects:
            try:
                client.files.delete(name=file_obj.name)
            except Exception:
                pass


@po_bp.route("/po_form", methods=["GET", "POST"])
@po_bp.route("/simple", methods=["GET", "POST"])
def po_form():
    import vercel_blob
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
        system_status = request.form.get("system_status", "").strip()
        clear_ai_flag = request.form.get("clear_ai_flag", "0")
              
        if clear_ai_flag == '1':
            ai_recommendation_summary = None

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
            except Exception:
                pass

        existing_urls = [u.strip() for u in existing_filepath_str.split(",") if u.strip()]

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

                    gemini_file_payloads.append((file_bytes, safe_filename, quote_file.mimetype))
                except Exception as upload_err:
                    print(f"File Processing Error: {upload_err}")

        all_urls = existing_urls + new_urls
        combined_quote_filepath = ",".join(all_urls) if all_urls else None
        quotes_provided = len(all_urls)

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
                        except Exception:
                            pass

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

                    if submission_status in ["Run AI Analysis", "Submit for Finance Review", "Submit for Approval", "Sent"]:
                        trigger_github_workflow()

                return redirect(url_for('po.po_form', po_number=new_po))

            except Exception as e:
                conn.rollback()
                message = f"Error saving purchase order: {e}"

    selected_po_num = request.args.get("po_number") or request.form.get("po_number")
    audit_logs = []

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT gl_code, description FROM master_budget ORDER BY gl_code ASC;")
            gl_records = cur.fetchall()

            cur.execute("SELECT po_number, description FROM po_log ORDER BY created_at DESC;")
            saved_pos = cur.fetchall()
            
            if selected_po_num:
                cur.execute("SELECT * FROM po_log_with_status WHERE po_number = %s;", (selected_po_num,))
                selected_po = cur.fetchone()

                if selected_po:
                    raw_filepath = selected_po.get('quote_filepath') or ''
                    selected_po['quote_urls'] = [u.strip() for u in raw_filepath.split(',') if u.strip()]

                    try:
                        target_po = str(selected_po.get('po_number', '')).strip()
                        cur.execute(
                            """
                            SELECT 
                                l.timestamp AS action_timestamp, 
                                l.actor_email, 
                                l.action_type, 
                                l.system_notes AS notes,
                                a.approval_permission,
                                NULLIF(TRIM(CONCAT_WS(' ', a."Name", a."Surname")), '') AS full_name
                            FROM workflow_control_log l
                            LEFT JOIN approvers a ON LOWER(TRIM(l.actor_email)) = LOWER(TRIM(a.email))
                            WHERE TRIM(l.po_number) ILIKE TRIM(%s) 
                            ORDER BY l.timestamp DESC;
                            """,
                            (target_po,)
                        )
                        audit_logs = cur.fetchall()
                    except Exception:
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


@po_bp.route("/api/cron/check-escalations", methods=["GET", "POST"])
def cron_check_escalations():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
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
            trigger_github_workflow()
            cursor.execute("UPDATE public.po_log SET chair_escalated = TRUE WHERE id = %s;", (po['id'],))
            cursor.execute(
                "INSERT INTO public.workflow_control_log (po_number, log_entry, timestamp) VALUES (%s, %s, NOW());",
                (po['po_number'], f"Automated 6-Hour Escalation dispatched. Requested at: {po['chair_requested_at']}")
            )
        conn.commit()
        return {"status": "success", "message": "Escalation check completed"}, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500
    finally:
        conn.close()
