import streamlit as st
import psycopg2
import os

# Connect to Neon
def get_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

st.title("Cathkin Procurement: Manual Data Entry")

# --- Form for New Project ---
with st.form("project_form"):
    st.subheader("1. Add or Update Project")
    proj_ref = st.text_input("Project Reference (e.g. 2026_02)")
    proj_name = st.text_input("Project Name")
    price_weighting = st.number_input("Price Weighting (%)", value=50.0)
    bid_floor = st.number_input("Lowest Bid Floor (R)", value=0.0)
    
    submitted = st.form_submit_button("Save Project")
    if submitted and proj_ref:
        conn = get_connection()
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
            conn.close()
        st.success(f"Project '{proj_ref}' saved successfully!")

# --- Display Evaluated Results View ---
st.subheader("2. Live Evaluated Scores")
target_ref = st.text_input("Enter Project Reference to View Scores", value="2026_02")

if st.button("Fetch Rankings"):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT vendor_name, projected_5yr_total, calculated_price_score, 
                   calculated_non_pricing_score, calculated_final_score
            FROM procurement_options_evaluated
            WHERE project_reference = %s
            ORDER BY calculated_final_score DESC;
        """, (target_ref,))
        rows = cur.fetchall()
        if rows:
            st.table(rows)
        else:
            st.info("No evaluated options found for this project reference.")
    finally:
        conn.close()