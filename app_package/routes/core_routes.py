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

@core_bp.route('/audit-log')
def view_audit_log():
    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM workflow_control_log ORDER BY timestamp DESC LIMIT 100;")
        logs = cursor.fetchall()
    conn.close()
    return render_template('audit_log.html', logs=logs)
