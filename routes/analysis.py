import os
import re
import json
from flask import Blueprint, request, jsonify
from google import genai
from google.genai import types
from flask import render_template_string, abort

analysis_bp = Blueprint('analysis', __name__, url_prefix='/api/v1')

# Initialize Gemini Client
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY")) if os.getenv("GEMINI_API_KEY") else None


def get_system_prompt(process_name="multi_document_analysis"):
    """Fetch active prompt template and model name from system_prompts."""
    from app import get_db_connection  # Deferred import to prevent circular dependency
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT prompt_template, selected_model 
            FROM system_prompts 
            WHERE process = %s AND is_active = TRUE 
            LIMIT 1;
            """,
            (process_name,)
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"No active system prompt found in database for process: '{process_name}'")
        
        return row['prompt_template'], row['selected_model'] or 'gemini-2.5-flash'
    finally:
        cursor.close()
        conn.close()


@analysis_bp.route('/analyze-documents', methods=['POST'])
def analyze_documents():
    if not client:
        return jsonify({'error': 'Gemini API client is not configured'}), 500

    if 'files' not in request.files:
        return jsonify({'error': 'No files provided'}), 400

    uploaded_files = request.files.getlist('files')
    if not uploaded_files or uploaded_files[0].filename == '':
        return jsonify({'error': 'No selected files'}), 400

    # Retrieve form metadata
    option_id = request.form.get('option_id')
    project_id = request.form.get('project_id')
    vendor_name = request.form.get('vendor_name')

    try:
        # Fetch active system prompt and model selection from DB
        prompt_template, model_name = get_system_prompt('multi_document_analysis')

        # Build multipart contents for Gemini
        contents = []
        for file_obj in uploaded_files:
            file_bytes = file_obj.read()
            mime_type = file_obj.content_type or 'application/pdf'
            contents.append(
                types.Part.from_bytes(
                    data=file_bytes,
                    mime_type=mime_type
                )
            )

        contents.append(prompt_template)

        # Call Gemini API
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.1
            )
        )

        # Sanitize code fences if returned by Gemini
        raw_html = response.text.strip()
        cleaned_html = re.sub(r'^```html\s*|^```\s*|\s*```$', '', raw_html, flags=re.MULTILINE).strip()

        # Save HTML to procurement_options in PostgreSQL
        from app import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if option_id:
            cursor.execute(
                """
                UPDATE procurement_options 
                SET analysis_sheet_html = %s 
                WHERE id = %s;
                """,
                (cleaned_html, option_id)
            )
        elif project_id and vendor_name:
            cursor.execute(
                """
                UPDATE procurement_options 
                SET analysis_sheet_html = %s 
                WHERE project_id = %s AND vendor_name = %s;
                """,
                (cleaned_html, project_id, vendor_name)
            )
            
        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({
            "status": "success",
            "model_used": model_name,
            "html_output": cleaned_html
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@analysis_bp.route('/analyze-documents/<int:project_id>', methods=['POST'])
def analyze_project_documents(project_id):
    if not client:
        return jsonify({'error': 'Gemini API client is not configured'}), 500

    from app import get_db_connection
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Fetch options for this project that have quote PDF bytes
        cursor.execute(
            "SELECT id, vendor_name, quote_file_bytes FROM procurement_options WHERE project_id = %s;",
            (project_id,)
        )
        options = cursor.fetchall()

        if not options:
            return jsonify({'error': 'No procurement options/quotes found for this project'}), 400

        prompt_template, model_name = get_system_prompt('multi_document_analysis')

        for opt in options:
            if not opt['quote_file_bytes']:
                continue

            contents = [
                types.Part.from_bytes(
                    data=bytes(opt['quote_file_bytes']),
                    mime_type='application/pdf'
                ),
                prompt_template
            ]

            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(temperature=0.1)
            )

            raw_html = response.text.strip()
            cleaned_html = re.sub(r'^```html\s*|^```\s*|\s*```$', '', raw_html, flags=re.MULTILINE).strip()

            cursor.execute(
                "UPDATE procurement_options SET analysis_sheet_html = %s WHERE id = %s;",
                (cleaned_html, opt['id'])
            )

        conn.commit()
        return jsonify({"status": "success", "message": "Phase 2a document analysis complete"}), 200

    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@analysis_bp.route('/view-analysis/<int:option_id>', methods=['GET'])
def view_analysis(option_id):
    from app import get_db_connection
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            "SELECT vendor_name, analysis_sheet_html FROM procurement_options WHERE id = %s;", 
            (option_id,)
        )
        row = cursor.fetchone()
        
        if not row or not row['analysis_sheet_html']:
            return "Analysis sheet not found.", 404
            
        # Standalone HTML page wrapper with modern typography styling
        full_page = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{row['vendor_name']} - Detailed Analysis Sheet</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-slate-50 min-h-screen p-8 text-slate-800">
            <div class="max-w-5xl mx-auto bg-white p-8 rounded-xl shadow-md border border-slate-200">
                <div class="mb-6 pb-4 border-b border-slate-200 flex justify-between items-center">
                    <h1 class="text-2xl font-bold text-slate-900">{row['vendor_name']} Analysis Sheet</h1>
                    <button onclick="window.print()" class="px-3 py-1.5 text-sm bg-slate-100 hover:bg-slate-200 rounded text-slate-700 font-medium">Print / Export PDF</button>
                </div>
                <div class="analysis-sheet-content">
                    {row['analysis_sheet_html']}
                </div>
            </div>
        </body>
        </html>
        """
        return render_template_string(full_page)
        
    finally:
        cursor.close()
        conn.close()