from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from app_package.db import get_db_connection

core_bp = Blueprint('core', __name__)

@core_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        conn = get_db_connection()
        cur = conn.cursor()

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

        if user and user['password_hash'] == password:
            session['user_id'] = user['id']
            return redirect(url_for('core.dashboard_home'))
        
        flash('Invalid email or password', 'danger')
        
    return render_template('login.html')

@core_bp.route("/")
def dashboard_home():
    return render_template("dashboard.html")

@core_bp.route('/guide')
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

@core_bp.route('/legal_framework')
def legal_framework():
    return render_template('legal_framework.html')

@core_bp.route('/prompts')
def render_prompts_page():
    return render_template('prompts.html')

@core_bp.route('/api/prompts', methods=['GET'])
def api_get_prompts():
    conn = get_db_connection()
    cur = conn.cursor()
    
    process_name = request.args.get('process')
    
    try:
        if process_name:
            # Fetch a specific prompt configuration from system_prompts
            cur.execute(
                """
                SELECT process, prompt_key, prompt_template, description, is_active, selected_model 
                FROM system_prompts WHERE process = %s
                """,
                (process_name,)
            )
            row = cur.fetchone()
            if row:
                return jsonify({
                    "process": row['process'],
                    "prompt_key": row['prompt_key'],
                    "prompt_template": row['prompt_template'],
                    "description": row['description'],
                    "is_active": row['is_active'],
                    "selected_model": row['selected_model']
                })
            return jsonify({"error": "Prompt not found"}), 404
        else:
            # Return list of all available processes for the dropdown
            cur.execute("SELECT process FROM system_prompts ORDER BY process ASC;")
            rows = cur.fetchall()
            processes = [row['process'] for row in rows]
            return jsonify(processes)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

@core_bp.route('/api/prompts', methods=['POST'])
def api_save_prompt():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON payload"}), 400
        
    process = data.get('process')
    prompt_template = data.get('prompt_template')
    is_active = data.get('is_active')
    selected_model = data.get('selected_model')

    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Update system_prompts and refresh updated_at timestamp
        cur.execute(
            """
            UPDATE system_prompts 
            SET prompt_template = %s, is_active = %s, selected_model = %s, updated_at = CURRENT_TIMESTAMP 
            WHERE process = %s
            """,
            (prompt_template, is_active, selected_model, process)
        )
        conn.commit()
        return jsonify({"success": True, "message": "Settings saved successfully"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

@core_bp.route('/audit-log')
def view_audit_log():
    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM workflow_control_log ORDER BY timestamp DESC LIMIT 100;")
        logs = cursor.fetchall()
    conn.close()
    return render_template('audit_log.html', logs=logs)

@core_bp.route('/api/approved_vendors', methods=['GET'])
def api_get_approved_vendors():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, vendor_name, supplier_code, gl_code, 
                   monthly_threshold_amount, last_approval_date, 
                   expiry_date, approval_reference, is_active, 
                   notes, match_pattern, created_at 
            FROM approved_vendors 
            ORDER BY vendor_name ASC;
        """)
        rows = cur.fetchall()
        
        vendors = []
        for r in rows:
            vendors.append({
                "id": r[0],
                "vendor_name": r[1],
                "supplier_code": r[2],
                "gl_code": r[3],
                "monthly_threshold_amount": float(r[4]) if r[4] is not None else 0.0,
                "last_approval_date": str(r[5]) if r[5] else None,
                "expiry_date": str(r[6]) if r[6] else None,
                "approval_reference": r[7],
                "is_active": r[8],
                "notes": r[9],
                "match_pattern": r[10],
                "created_at": str(r[11]) if r[11] else None
            })
            
        return jsonify({"status": "success", "data": vendors})
    except Exception as e:
        print(f"Error fetching approved vendors: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        cur.close()
        conn.close()
