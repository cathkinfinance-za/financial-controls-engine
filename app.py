import os
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)

def get_connection():
    """Establish connection to Neon PostgreSQL database."""
    return psycopg2.connect(os.getenv("DATABASE_URL"))

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