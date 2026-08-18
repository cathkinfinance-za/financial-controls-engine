import os
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import jsonify
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)

def get_connection():
    """Establish connection to Neon PostgreSQL database."""
    return psycopg2.connect(os.getenv("DATABASE_URL"))

@app.route('/api/budget-details')
def budget_details():
    gl_code = request.args.get('gl_code')
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT ytd, budget_ytd, variance, total_budget 
                FROM master_budget 
                WHERE gl_code = %s;
            """, (gl_code,))
            row = cur.fetchone()
            if row:
                return jsonify(row)
    finally:
        conn.close()
    return jsonify({})

@app.route('/api/evaluate-status', methods=['POST'])
def evaluate_status():
    data = request.json or {}
    
    sub_status = data.get("submission_status", "")
    is_budgeted = data.get("is_budgeted", "")
    gl_code = data.get("gl_code", "")
    expense_type = data.get("expense_type", "")
    quotes_provided = int(data.get("quotes_provided", 0) or 0)
    est_cost = float(data.get("estimated_cost", 0) or 0)
    
    total_budget = float(data.get("total_annual_budget", 0) or 0)
    ytd_actual = float(data.get("ytd_actual", 0) or 0)
    
    # Exact rules translated from your formula text file
    if sub_status == "Approved":
        status = "✅ APPROVED: Compliance Process Complete"
    elif sub_status == "Rejected":
        status = "🛑 REJECTED: Final Authorisation Declined"
    elif is_budgeted == "Yes" and not gl_code:
        status = "⚠️ ACTION REQUIRED: Select a valid GL description"
    elif not expense_type:
        status = "⚠️ ACTION REQUIRED: Select Expense Type If Not Approved"
    elif quotes_provided == 0 and est_cost > 0:
        status = "⚠️ ACTION REQUIRED: Enter number of quotes provided"
    elif (
        (est_cost <= 10000 and quotes_provided < 1) or
        (10001 <= est_cost <= 50000 and quotes_provided < 2) or
        (est_cost > 50000 and quotes_provided < 3) or
        (expense_type == "Capital" and quotes_provided < 3)
    ):
        status = "❌ BLOCKED: Insufficient Quotes Provided for this Threshold"
    else:
        remaining_budget = total_budget - ytd_actual
        buffer_pool = total_budget / 6
        
        if is_budgeted == "Yes" and total_budget > 0 and est_cost > remaining_budget:
            status = "❌ OVER-EXPENDITURE: Transaction Exceeds True Remaining Annual Budget"
        elif is_budgeted == "Yes" and total_budget > 0 and (remaining_budget - est_cost) < buffer_pool:
            status = "⚠️ LIQUIDITY ALERT: Post-purchase runway drops below required 2-Month Buffer Pool"
        elif expense_type == "Capital":
            status = "🏦 Capital Expenditure: Reserve Allocation (Requires Full Board & AGM Ratification)"
        elif expense_type == "Maintenance":
            status = "📝 Service Provider Contract: SLA Appointment (Requires Full Board Dual Signatures)"
        elif is_budgeted == "Yes":
            if est_cost <= 10000:
                status = "🟢 Routine Operational: Within Budget (No Approval Required)"
            elif est_cost <= 50000:
                status = "🟡 Portfolio Operational: Within Budget (Requires Chairman Sign-off)"
            else:
                status = "🟠 High-Value Operational: Within Budget (Requires 2 Board Members)"
        elif is_budgeted == "No" and expense_type == "Emergency":
            if est_cost <= 30000:
                status = "⚡ Emergency Expenditure: Unbudgeted (Requires Ops & Board Chair Ratification)"
            else:
                status = "❌ EMERGENCY CRITICAL: Exceeds R30,000 Limit (Requires Urgent Board Resolution)"
        elif is_budgeted == "No" and expense_type == "Operational":
            if est_cost <= 30000:
                status = "🔵 Extraordinary / Unbudgeted: (Requires Full Board Written Motivation)"
            else:
                status = "❌ UNBUDGETED BLOCKED: Exceeds R30,000 Limit (Requires AGM Ratification)"
        else:
            status = "Draft - In Progress"

    return jsonify({"system_status": status})

@app.route("/", methods=["GET", "POST"])
@app.route("/", methods=["GET", "POST"])
def purchase_order_form():
    conn = get_connection()
    message = None
    selected_po = None
    
    if request.method == "POST":
        original_po_number = request.args.get("po_number") or request.form.get("original_po_number")
        po_number = request.form.get("po_number")
        description = request.form.get("description")
        po_date = request.form.get("po_date") or None
        is_budgeted = request.form.get("is_budgeted")
        gl_code = request.form.get("gl_code")
        expense_type = request.form.get("expense_type")
        
        # Safely convert inputs to float or default to 0.0
        try:
            estimated_cost = float(request.form.get("estimated_cost") or 0.0)
        except ValueError:
            estimated_cost = 0.0

        recommended_vendor = request.form.get("recommended_vendor")
        justification_notes = request.form.get("justification_notes")
        submission_status = request.form.get("submission_status")
        system_status = request.form.get("system_status")

        target_key = original_po_number if original_po_number else po_number

        if target_key:
            try:
                with conn.cursor() as cur:
                    gl_code_id = None
                    if gl_code:
                        cur.execute("SELECT id FROM master_budget WHERE gl_code = %s;", (gl_code,))
                        gl_row = cur.fetchone()
                        gl_code_id = gl_row['id'] if gl_row else None

                    cur.execute("""
                        INSERT INTO po_log (
                            po_number, description, po_date, is_budgeted, gl_code, gl_code_id,
                            expense_type, estimated_cost, recommended_vendor, 
                            justification_notes, submission_status, system_status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                            system_status = COALESCE(NULLIF(EXCLUDED.system_status, ''), po_log.system_status);
                    """, (
                        po_number, description, po_date, is_budgeted, gl_code, gl_code_id,
                        expense_type, estimated_cost, recommended_vendor,
                        justification_notes, submission_status, system_status
                    ))
                    conn.commit()
                message = f"Purchase Order '{po_number}' saved successfully!"
            except Exception as e:
                conn.rollback()
                print(f"DATABASE ERROR: {e}")
                # Set user friendly message instead of re-raising uncaught exception
                message = f"Error saving purchase order: {e}"

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT gl_code, description FROM master_budget ORDER BY gl_code ASC;")
            gl_records = cur.fetchall()

            target_ref = request.args.get("project_ref", "2026_02")
            cur.execute("""
                SELECT vendor_name, projected_5yr_total, calculated_price_score, 
                       calculated_non_pricing_score, calculated_final_score
                FROM procurement_options_evaluated
                WHERE project_reference = %s
                ORDER BY calculated_final_score DESC;
            """, (target_ref,))
            evaluated_rows = cur.fetchall()

            # Fetch all saved POs for the sidebar list
            cur.execute("""
                SELECT po_number, description, po_date, estimated_cost, 
                       recommended_vendor, system_status, submission_status, created_at
                FROM po_log
                ORDER BY created_at DESC;
            """)
            saved_pos = cur.fetchall()

            # If a sidebar item was clicked, fetch that specific PO's full details
            selected_po_num = request.args.get("po_number")
            if selected_po_num:
                cur.execute("SELECT * FROM po_log WHERE po_number = %s;", (selected_po_num,))
                selected_po = cur.fetchone()
                
    finally:
        conn.close()

    return render_template(
        "po_form.html", 
        gl_records=gl_records, 
        evaluated_rows=evaluated_rows, 
        saved_pos=saved_pos,
        selected_po=selected_po,
        message=message
    )


@app.route("/simple", methods=["GET", "POST"])
def simple_po_form():
    conn = get_connection()
    message = None
    selected_po = None

    if request.method == "POST":
        original_po = request.form.get("original_po_number", "").strip()
        new_po = request.form.get("po_number", "").strip()
        description = request.form.get("description", "").strip()
        po_date = request.form.get("po_date") or None
        is_budgeted = request.form.get("is_budgeted")
        gl_code = request.form.get("gl_code")
        expense_type = request.form.get("expense_type")
        
        try:
            estimated_cost = float(request.form.get("estimated_cost") or 0.0)
        except ValueError:
            estimated_cost = 0.0

        if new_po:
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    # Look up primary key for the selected GL account
                    gl_code_id = None
                    if gl_code:
                        cur.execute("SELECT id FROM master_budget WHERE gl_code = %s;", (gl_code,))
                        gl_row = cur.fetchone()
                        gl_code_id = gl_row['id'] if gl_row else None

                    if original_po:
                        # Update existing row
                        cur.execute("""
                            UPDATE po_log 
                            SET po_number = %s,
                                description = %s,
                                po_date = %s,
                                is_budgeted = %s,
                                gl_code = %s,
                                gl_code_id = %s,
                                expense_type = %s,
                                estimated_cost = %s
                            WHERE po_number = %s;
                        """, (
                            new_po, description, po_date, is_budgeted, 
                            gl_code, gl_code_id, expense_type, estimated_cost, original_po
                        ))
                    else:
                        # Insert new row
                        cur.execute("""
                            INSERT INTO po_log (
                                po_number, description, po_date, is_budgeted, 
                                gl_code, gl_code_id, expense_type, estimated_cost
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (po_number) DO UPDATE SET
                                description = EXCLUDED.description,
                                po_date = EXCLUDED.po_date,
                                is_budgeted = EXCLUDED.is_budgeted,
                                gl_code = EXCLUDED.gl_code,
                                gl_code_id = EXCLUDED.gl_code_id,
                                expense_type = EXCLUDED.expense_type,
                                estimated_cost = EXCLUDED.estimated_cost;
                        """, (
                            new_po, description, po_date, is_budgeted, 
                            gl_code, gl_code_id, expense_type, estimated_cost
                        ))
                    
                    conn.commit()
                
                return redirect(url_for('simple_po_form', po_number=new_po))

            except Exception as e:
                conn.rollback()
                print(f"DATABASE ERROR: {e}")
                message = f"Error saving purchase order: {e}"

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Fetch GL accounts for dropdown list
            cur.execute("SELECT gl_code, description FROM master_budget ORDER BY gl_code ASC;")
            gl_records = cur.fetchall()

            # Sidebar records query
            cur.execute("SELECT po_number, description FROM po_log ORDER BY created_at DESC;")
            saved_pos = cur.fetchall()

            # Active record fetch
            selected_po_num = request.args.get("po_number")
            if selected_po_num:
                cur.execute("SELECT * FROM po_log WHERE po_number = %s;", (selected_po_num,))
                selected_po = cur.fetchone()

    finally:
        conn.close()

    return render_template(
        "po_form_simple.html", 
        gl_records=gl_records,
        saved_pos=saved_pos,
        selected_po=selected_po,
        message=message
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)