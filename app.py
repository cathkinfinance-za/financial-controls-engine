import os
import json
import psycopg2
import vercel_blob
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
import google.generativeai as genai

app = Flask(__name__)

def get_connection():
    """Establish connection to Neon PostgreSQL database."""
    return psycopg2.connect(os.getenv("DATABASE_URL"))

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

        # --- MULTI-FILE VERCEL BLOB UPLOAD LOGIC ---
        uploaded_files = request.files.getlist('attach_quotes') or request.files.getlist('quote_attachment')
        uploaded_urls = []

        for quote_file in uploaded_files:
            if quote_file and quote_file.filename:
                try:
                    blob_response = vercel_blob.put(
                        f"quotes/{quote_file.filename}", 
                        quote_file.read(), 
                        options={"access": "public"}
                    )
                    url = blob_response.get('url')
                    if url:
                        uploaded_urls.append(url)
                except Exception as upload_err:
                    print(f"Blob Upload Error: {upload_err}")

        # Store multiple URLs as a comma-separated string
        quote_url = ",".join(uploaded_urls) if uploaded_urls else None

        try:
            estimated_cost = float(request.form.get("estimated_cost") or 0.0)
        except ValueError:
            estimated_cost = 0.0

        try:
            quotes_provided = int(request.form.get("quotes_provided") or 0)
        except ValueError:
            quotes_provided = 0

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
                                quote_filepath = COALESCE(%s, quote_filepath)
                            WHERE po_number = %s;
                        """, (
                            new_po, description, po_date, is_budgeted, gl_code, gl_code_id, 
                            expense_type, estimated_cost, recommended_vendor, justification_notes, 
                            submission_status, system_status, quotes_provided, actioned_date, 
                            actioned_by, approval_notes, ai_recommendation_summary, quote_url, original_po
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
                                quote_filepath = COALESCE(EXCLUDED.quote_filepath, po_log.quote_filepath);
                        """, (
                            new_po, description, po_date, is_budgeted, gl_code, gl_code_id, 
                            expense_type, estimated_cost, recommended_vendor, justification_notes, 
                            submission_status, system_status, quotes_provided, actioned_date, 
                            actioned_by, approval_notes, ai_recommendation_summary, quote_url
                        ))
                    
                    conn.commit()

                return redirect(url_for('po_form', po_number=new_po))

            except Exception as e:
                conn.rollback()
                message = f"Error saving purchase order: {e}"

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

                if selected_po and selected_po.get('quote_filepath'):
                    selected_po['quote_urls'] = [
                        u.strip() for u in selected_po['quote_filepath'].split(',') if u.strip()
                    ]
                elif selected_po:
                    selected_po['quote_urls'] = []

                if selected_po and selected_po.get('gl_code'):
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

def analyze_po_with_gemini(gemini_files, form_data):
    model = genai.GenerativeModel('gemini-1.5-pro')
    
    prompt = f"""
    Please review the attached purchase order documents and extract vendor details, 
    line items, and total pricing. Verify if the figures match across all attached documents.
    
    Form Context:
    - Vendor Name: {form_data.get('vendor_name')}
    - Department: {form_data.get('department')}
    """

    contents = [*gemini_files, prompt]
    response = model.generate_content(contents)
    
    for file_obj in gemini_files:
        try:
            genai.delete_file(file_obj.name)
        except Exception as e:
            print(f"Gemini File Deletion Error: {e}")

    return response.text

@app.route('/submit-po', methods=['POST'])
def submit_po():
    uploaded_files = request.files.getlist('attachments')
    saved_urls = []
    gemini_file_objects = []

    for file in uploaded_files:
        if file and file.filename != '':
            try:
                # Read stream directly for Vercel Blob without local disk write
                file_bytes = file.read()
                blob_response = vercel_blob.put(
                    f"quotes/{secure_filename(file.filename)}", 
                    file_bytes, 
                    options={"access": "public"}
                )
                url = blob_response.get('url')
                if url:
                    saved_urls.append(url)

                # Pass bytes to Gemini File API using in-memory byte streams
                uploaded_gemini_file = genai.upload_file(
                    file_bytes, 
                    mime_type=file.mimetype or "application/pdf"
                )
                gemini_file_objects.append(uploaded_gemini_file)
            except Exception as err:
                print(f"Error processing attachment {file.filename}: {err}")

    analysis_result = None
    if gemini_file_objects:
        analysis_result = analyze_po_with_gemini(gemini_file_objects, request.form)

    return render_template('po_detail.html', attachments=saved_urls, result=analysis_result)

@app.route("/simple")
def simple_redirect():
    """Redirect legacy /simple route to master form route."""
    po_number = request.args.get("po_number")
    if po_number:
        return redirect(url_for("po_form", po_number=po_number))
    return redirect(url_for("po_form"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)