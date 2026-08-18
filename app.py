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

def evaluate_system_status(form_data, files_count=0):
    sub_status = form_data.get("submission_status", "Draft")
    is_budgeted = form_data.get("is_budgeted", "")
    gl_code = form_data.get("gl_code", "")
    expense_type = form_data.get("expense_type", "")
    est_cost = float(form_data.get("estimated_cost", 0) or 0)
    
    total_budget = float(form_data.get("total_annual_budget", 0) or 0)
    ytd_actual = float(form_data.get("ytd_actual", 0) or 0)
    
    if sub_status == "Approved":
        return "✅ APPROVED: Compliance Process Complete"
    if sub_status == "Rejected":
        return "🛑 REJECTED: Final Authorisation Declined"
    if is_budgeted == "Yes" and not gl_code:
        return "⚠️ ACTION REQUIRED: Select a valid GL description"
    if not expense_type:
        return "⚠️ ACTION REQUIRED: Select Expense Type If Not Approved"
    if files_count == 0 and est_cost > 0:
        return "⚠️ ACTION REQUIRED: Enter number of quotes provided"
        
    # Quote threshold checks
    if est_cost <= 10000 and files_count < 1:
        return "❌ BLOCKED: Insufficient Quotes Provided for this Threshold"
    if 10001 <= est_cost <= 50000 and files_count < 2:
        return "❌ BLOCKED: Insufficient Quotes Provided for this Threshold"
    if est_cost > 50000 and files_count < 3:
        return "❌ BLOCKED: Insufficient Quotes Provided for this Threshold"
    if expense_type == "Capital" and files_count < 3:
        return "❌ BLOCKED: Insufficient Quotes Provided for this Threshold"

    remaining_budget = total_budget - ytd_actual
    buffer_pool = total_budget / 6

    if is_budgeted == "Yes" and total_budget > 0 and est_cost > remaining_budget:
        return "❌ OVER-EXPENDITURE: Transaction Exceeds True Remaining Annual Budget"
    if is_budgeted == "Yes" and total_budget > 0 and (remaining_budget - est_cost) < buffer_pool:
        return "⚠️ LIQUIDITY ALERT: Post-purchase runway drops below required 2-Month Buffer Pool"
        
    if expense_type == "Capital":
        return "🏦 Capital Expenditure: Reserve Allocation (Requires Full Board & AGM Ratification)"
    if expense_type == "Maintenance": # Service provider equivalent
        return "📝 Service Provider Contract: SLA Appointment (Requires Full Board Dual Signatures)"
        
    if is_budgeted == "Yes":
        if est_cost <= 10000:
            return "🟢 Routine Operational: Within Budget (No Approval Required)"
        elif est_cost <= 50000:
            return "🟡 Portfolio Operational: Within Budget (Requires Chairman Sign-off)"
        else:
            return "🟠 High-Value Operational: Within Budget (Requires 2 Board Members)"
            
    if is_budgeted == "No" and expense_type == "Emergency":
        if est_cost <= 30000:
            return "⚡ Emergency Expenditure: Unbudgeted (Requires Ops & Board Chair Ratification)"
        else:
            return "❌ EMERGENCY CRITICAL: Exceeds R30,000 Limit (Requires Urgent Board Resolution)"
            
    if is_budgeted == "No" and expense_type == "Operational": # Extraordinary mapping
        if est_cost <= 30000:
            return "🔵 Extraordinary / Unbudgeted: (Requires Full Board Written Motivation)"
        else:
            return "❌ UNBUDGETED BLOCKED: Exceeds R30,000 Limit (Requires AGM Ratification)"

    return "Draft - In Progress"

@app.route("/", methods=["GET", "POST"])
def purchase_order_form():
    conn = get_connection()
    message = None
    
    if request.method == "POST":
        # Handle manual project or purchase order creation if submitted from form
        proj_ref = request.form.get("project_reference")
        proj_name = request.form.get("project_name")
        price_weighting = request.form.get("price_weighting", 50.0)
        bid_floor = request.form.get("lowest_project_bid_floor", 0.0)
        
        if proj_ref:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO projects (project_reference, name, price_weighting, lowest_project_bid_floor)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (project_reference) DO UPDATE SET
                            name = EXCLUDED.name,
                            price_weighting = EXCLUDED.price_weighting,
                            lowest_project_bid_floor = EXCLUDED.lowest_project_bid_floor;
                    """, (proj_ref, proj_name, price_weighting, bid_floor))
                    conn.commit()
                message = f"Project '{proj_ref}' saved successfully!"
            except Exception as e:
                message = f"Error saving project: {e}"

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. Fetch synced GL codes and descriptions from Neon master_budget table
            cur.execute("SELECT gl_code, description FROM master_budget ORDER BY gl_code ASC;")
            gl_records = cur.fetchall()

            # 2. Fetch evaluated scores view for default or target project
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

    # Renders your po_form.html template, passing the dynamic GL records and evaluated rows
    return render_template(
        "po_form.html", 
        gl_records=gl_records, 
        evaluated_rows=evaluated_rows, 
        message=message
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)