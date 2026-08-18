import os
import io
import json
import psycopg2
import vercel_blob
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for, jsonify
from werkzeug.utils import secure_filename

try:
    import google.generativeai as genai
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    GEMINI_AVAILABLE = True
except ModuleNotFoundError:
    genai = None
    GEMINI_AVAILABLE = False

app = Flask(__name__)

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
        model = genai.GenerativeModel('gemini-1.5-flash')
        
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

        # Fetch existing record if updating to preserve old attachment URLs
        existing_filepath_str = ""
        if original_po or new_po:
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
                    
                    # 1. Save to Vercel Blob
                    blob_response = vercel_blob.put(
                        f"quotes/{safe_filename}", 
                        file_bytes, 
                        options={"access": "public"}
                    )
                    url = blob_response.get('url')
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)