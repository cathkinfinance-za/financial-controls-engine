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
def purchase_order_form():
    conn = get_connection()
    message = None
    
    if request.method == "POST":
        po_number = request.form.get("po_number")
        description = request.form.get("description")
        po_date = request.form.get("po_date") or None
        is_budgeted = request.form.get("is_budgeted")
        gl_code = request.form.get("gl_code")
        expense_type = request.form.get("expense_type")
        estimated_cost = request.form.get("estimated_cost") or 0.0
        recommended_vendor = request.form.get("recommended_vendor")
        justification_notes = request.form.get("justification_notes")
        submission_status = request.form.get("submission_status")
        system_status = request.form.get("system_status")

        if po_number:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO po_log (
                            po_number, description, po_date, is_budgeted, gl_code, 
                            expense_type, estimated_cost, recommended_vendor, 
                            justification_notes, submission_status, system_status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (po_number) DO UPDATE SET
                            description = EXCLUDED.description,
                            po_date = EXCLUDED.po_date,
                            is_budgeted = EXCLUDED.is_budgeted,
                            gl_code = EXCLUDED.gl_code,
                            expense_type = EXCLUDED.expense_type,
                            estimated_cost = EXCLUDED.estimated_cost,
                            recommended_vendor = EXCLUDED.recommended_vendor,
                            justification_notes = EXCLUDED.justification_notes,
                            submission_status = EXCLUDED.submission_status,
                            system_status = EXCLUDED.system_status;
                    """, (
                        po_number, description, po_date, is_budgeted, gl_code,
                        expense_type, estimated_cost, recommended_vendor,
                        justification_notes, submission_status, system_status
                    ))
                    conn.commit()
                message = f"Purchase Order '{po_number}' saved successfully!"
            except Exception as e:
                print(f"DATABASE ERROR: {e}")  # This will show up in your local terminal / Vercel logs
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
    finally:
        conn.close()

    return render_template(
        "po_form.html", 
        gl_records=gl_records, 
        evaluated_rows=evaluated_rows, 
        message=message
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)