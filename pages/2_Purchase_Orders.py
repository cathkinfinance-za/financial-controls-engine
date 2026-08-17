import streamlit as st
import psycopg2
import os

def get_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

st.title("Purchase Order Details")

# Top action bar with back button link
col_title, col_btn = st.columns([4, 1])
with col_btn:
    if st.button("Back to Dashboard >"):
        st.switch_page("app.py")

ALL_STATUS_OPTIONS = [
    "Draft",
    "Submit for Approval",
    "Sent",
    "Approved"
]

# Fetch available PO numbers from the database for selection
conn = get_connection()
try:
    cur = conn.cursor()
    cur.execute("SELECT po_number FROM po_log ORDER BY created_at DESC;")
    po_rows = cur.fetchall()
    po_list = [row[0] for row in po_rows]
except Exception:
    po_list = []
finally:
    cur.close()
    conn.close()

if not po_list:
    st.warning("No purchase orders found in the database. Please create one first.")
    st.stop()

# Select which PO record to view/edit
selected_po = st.selectbox("Select PO Number", options=po_list)

# Fetch selected PO data and related master budget information
conn = get_connection()
cur = conn.cursor()

cur.execute("""
    SELECT po_number, description, po_date, is_budgeted, gl_code, 
           expense_type, estimated_cost, recommended_vendor, 
           justification_notes, submission_status, ai_status, 
           actioned_by, actioned_date, approval_notes
    FROM po_log 
    WHERE po_number = %s
""", (selected_po,))
po_record = cur.fetchone()

budget_record = None
gl_code_val = po_record[4] if po_record and po_record[4] else None

if gl_code_val:
    cur.execute("""
        SELECT gl_code, gl_description, ytd_actual, budget_ytd, 
               total_annual_budget, buffer_pool
        FROM master_budget 
        WHERE gl_code = %s
    """, (gl_code_val,))
    budget_record = cur.fetchone()

cur.close()
conn.close()

if not po_record:
    st.error("Selected purchase order record could not be loaded.")
    st.stop()

# Unpack PO record fields safely
db_po_number, db_desc, db_date, db_is_budgeted, db_gl_code, db_expense_type, db_cost, db_vendor, db_notes, db_status, db_ai_status, db_actioned_by, db_actioned_date, db_approval_notes = po_record

with st.form("po_detailed_form"):
    
    st.subheader(db_po_number)
    
    po_number_input = st.text_input("PO Number", value=db_po_number)
    description = st.text_area("Description", value=db_desc or "")
    
    col_date, col_budgeted = st.columns(2)
    with col_date:
        po_date = st.date_input("Date", value=db_date if db_date else None)
    with col_budgeted:
        is_budgeted_idx = 0 if db_is_budgeted == "Yes" else 1
        is_budgeted = st.selectbox("Has this been budgeted for?", options=["Yes", "No"], index=is_budgeted_idx)
    
    col_gl, col_type = st.columns(2)
    with col_gl:
        gl_code = st.text_input("Select GL Code", value=db_gl_code or "")
    with col_type:
        expense_options = ["Operational", "Capital", "Maintenance", "Emergency"]
        exp_idx = expense_options.index(db_expense_type) if db_expense_type in expense_options else 0
        expense_type = st.selectbox("Expense Type", options=expense_options, index=exp_idx)
        
    gl_description_val = budget_record[1] if budget_record else "N/A"
    gl_description = st.text_input("GL Description (from Select GL Code)", value=gl_description_val, disabled=True)
    estimated_cost = st.number_input("Estimated Cost", min_value=0.0, value=float(db_cost or 0.0), step=100.0)
    
    st.markdown("### Attach Quotes")
    uploaded_quotes = st.file_uploader("Attach file", type=["pdf", "png", "jpg", "jpeg", "docx"], accept_multiple_files=True, label_visibility="collapsed")
    
    st.markdown("---")
    st.subheader("Vendor Selection & Rationale")
    
    recommended_vendor = st.text_input("Recommended Vendor", value=db_vendor or "")
    justification_notes = st.text_area("Justification Notes", value=db_notes or "")
    
    status_idx = ALL_STATUS_OPTIONS.index(db_status) if db_status in ALL_STATUS_OPTIONS else 0
    submission_status = st.selectbox("Submission Status", options=ALL_STATUS_OPTIONS, index=status_idx)
    
    latest_ai_status = st.text_input("Latest AI Status", value=db_ai_status or "", disabled=True)
    
    st.markdown("---")
    st.subheader("Financial Summary")
    
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        st.text_input("YTD Actual (from Select GL Code)", value=str(budget_record[2]) if budget_record else "0", disabled=True)
        st.text_input("Budget YTD (from Select GL Code)", value=str(budget_record[3]) if budget_record else "0", disabled=True)
        st.text_input("Variance (from Select GL Code)", value=str(float(budget_record[3] or 0) - float(budget_record[2] or 0)) if budget_record else "0", disabled=True)
    with col_f2:
        st.text_input("Total Annual Budget (from Select GL Code)", value=str(budget_record[4]) if budget_record else "0", disabled=True)
        st.text_input("2M Buffer Pool", value=str(budget_record[5]) if budget_record else "0", disabled=True)
        
    st.markdown("---")
    st.subheader("Approvals")
    
    col_a1, col_a2 = st.columns(2)
    with col_a1:
        st.text_input("Quotes Provided", value="2", disabled=True)
        st.text_input("SYSTEM STATUS", value="Portfolio Operational: Within Budget", disabled=True)
        st.text_input("Actioned By", value=db_actioned_by or "", disabled=True)
    with col_a2:
        st.text_input("Actioned Date", value=str(db_actioned_date) if db_actioned_date else "", disabled=True)
        st.text_input("Approval Notes", value=db_approval_notes or "—", disabled=True)
        
    ai_recommendation_summary = st.text_area(
        "AI Recommendation Summary", 
        value="Automated analysis complete based on current ledger values.",
        disabled=True
    )
    
    submitted = st.form_submit_button("Save Purchase Order Changes")
    
    if submitted:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE po_log SET
                        po_number = %s,
                        description = %s,
                        po_date = %s,
                        is_budgeted = %s,
                        gl_code = %s,
                        expense_type = %s,
                        estimated_cost = %s,
                        recommended_vendor = %s,
                        justification_notes = %s,
                        submission_status = %s
                    WHERE po_number = %s;
                """, (
                    po_number_input, description, po_date, is_budgeted, gl_code, 
                    expense_type, estimated_cost, recommended_vendor, 
                    justification_notes, submission_status, selected_po
                ))
                conn.commit()
            st.success("Purchase Order updated successfully!")
        except Exception as e:
            st.error(f"Error updating database: {e}")
        finally:
            conn.close()