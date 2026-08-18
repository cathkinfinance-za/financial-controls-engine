import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import json
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from google import genai
from google.genai import types
import csv
import datetime
import re
import time
from ddgs import DDGS

# ==========================================
# 1. DATABASE & CONFIGURATION HELPERS
# ==========================================
def get_db_connection():
    """Connects to the Neon PostgreSQL database using environment variables."""
    return psycopg2.connect(
        os.getenv("DATABASE_URL"),
        cursor_factory=RealDictCursor
    )

def write_control_log(po_number, action_type, user_email, notes=""):
    """Maintains an audit ledger for background compliance transactions."""
    log_file = r"C:\Users\Anita\OneDrive\Cathkin Estates\Process and Governance\workflow_control_log.csv"
    file_exists = os.path.exists(log_file)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        with open(log_file, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "PO Number", "Action Type", "Actor/Operator", "System Notes"])
            writer.writerow([timestamp, po_number, action_type, user_email, notes])
            print(f" Control Measure Signed: Row appended to audit ledger for 📝 {po_number}.")
    except Exception as e:
        print(f" Control Log Error: Failed to write background transaction token: {e}")

# ==========================================
# 2. EMAIL ENGINE FUNCTION
# ==========================================
def send_approval_email(to_emails, cc_emails, subject, body, attachment_paths=None):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    
    try:
        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg['From'] = sender_email
        msg['To'] = ", ".join(to_emails)
        if cc_emails:
            msg['Cc'] = ", ".join(cc_emails)
        
        msg.attach(MIMEText(body, 'plain'))
        
        if attachment_paths:
            for file_path in attachment_paths:
                if os.path.exists(file_path):
                    file_name = os.path.basename(file_path)
                    with open(file_path, "rb") as attachment:
                        part = MIMEBase("application", "octet-stream")
                        part.set_payload(attachment.read())
                        encoders.encode_base64(part)
                        part.add_header("Content-Disposition", f"attachment; filename= {file_name}")
                        msg.attach(part)
        
        all_recipients = list(set(to_emails + (cc_emails if cc_emails else [])))
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, all_recipients, msg.as_string())
        return True
    except Exception as e:
        print(f" ❌ Error sending email for {subject}: {e}")
        return False

# ==========================================
# 3. POSTGRESQL PROCESSING & COMPLIANCE LOGIC
# ==========================================
def process_postgres_approvals():
    print("Polling Neon PostgreSQL database rows for active 'Submit for Approval' entries...")
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Fetch records pending approval from your po_log table joined with budget data
            cursor.execute("""
                SELECT p.*, m.gl_code as master_gl_code, m.total_annual_budget, m.ytd_actual, m.variance 
                FROM po_log p
                LEFT JOIN master_budget m ON p.gl_code_id = m.id
                WHERE LOWER(p.submission_status) = 'submit for approval';
            """)
            records = cursor.fetchall()
            
            # Fetch approvers mapping matrix
            cursor.execute("SELECT role, email FROM approvers;")
            approver_records = cursor.fetchall()
            approver_emails = {row['role'].strip(): row['email'].strip() for row in approver_records}

            for record in records:
                po_num = str(record.get('po_number', '')).strip()
                if not po_num:
                    continue
                
                system_status = str(record.get('system_status', '')).strip()
                if "blocked" in system_status.lower() or "action required" in system_status.lower():
                    print(f"⏩ Skipping workflow for {po_num}: PO block triggered by systemic status.")
                    continue
                
                cost = float(record.get('estimated_cost', 0.0))
                desc_val = str(record.get('description', 'Operational Portfolio Procurement')).strip()
                expense_type = str(record.get('expense_type', '')).strip()
                coi_status = str(record.get('conflict_of_interest', 'NO')).strip().upper()
                coi_details = str(record.get('conflict_details', 'No specific details provided.')).strip()
                has_conflict = (coi_status == "YES")
                gl_code_val = str(record.get('master_gl_code', 'N/A')).strip()
                
                total_budget = float(record.get('total_annual_budget', 0.0))
                ytd_actual = float(record.get('ytd_actual', 0.0))
                remaining_budget = float(record.get('variance', 0.0))
                two_m_buffer = float(record.get('buffer_pool_2m', 0.0))
                
                # Sourcing requirements matrix
                if cost <= 10000:
                    sourcing_requirement = "Single written quote from an approved, accredited estate vendor."
                elif 10001 <= cost <= 50000:
                    sourcing_requirement = "Minimum of two independent written quotes must be evaluated."
                else:
                    sourcing_requirement = "Minimum of three independent written quotes accompanied by a standardised vendor comparison sheet."
                
                if expense_type.lower() == "capital":
                    sourcing_requirement = "Minimum of three independent written quotes accompanied by a standardised vendor comparison sheet submitted to the Finance Committee."
                
                # Routing & Governance Mapping
                if has_conflict:
                    to_list = [approver_emails.get("Full Board of Directors")]
                    cc_list = [approver_emails.get("Finance Committee Chair"), approver_emails.get("Managing Agent (GEMS)")]
                    audit_requirement = "⚠️ CONFLICT OF INTEREST DECLARED: Mandatory Board / Governance Committee ratification required prior to commitment."
                    email_subject = f"[{po_num}] ⚠️ GOVERNANCE REVIEW (COI Declared): {desc_val}"
                    action_prompt = 'A Conflict of Interest has been declared for this transaction. Please review the conflict notes below and reply "APPROVED" or "REJECTED".'
                else:
                    if "contract" in expense_type.lower():
                        to_list = [approver_emails.get("Full Board of Directors")]
                        cc_list = [approver_emails.get("Finance Committee Chair")]
                        audit_requirement = "Requires dual signature of authorized Directors on the formal contract."
                    elif "capital" in expense_type.lower():
                        to_list = [approver_emails.get("Full Board of Directors")]
                        cc_list = [approver_emails.get("Managing Agent (GEMS)"), approver_emails.get("Finance Committee Chair")]
                        audit_requirement = "Must align explicitly with statutory 10-Year Maintenance Plan and require AGM ratification."
                    else:
                        to_list = [approver_emails.get("Estate Manager")]
                        cc_list = [approver_emails.get("Managing Agent (GEMS)")]
                        audit_requirement = "Month-end general ledger reconciliation against budgeted line items."
                    
                    email_subject = f"[{po_num}] Approval Required: {desc_val}"
                    action_prompt = 'Please review the attached documents and reply directly to this message typing either "APPROVED" or "REJECTED".'

                # Placeholder for AI analysis execution (using Gemini + DuckDuckGo similar to your old structure)
                ai_analysis_text = "Automated compliance analysis processed via PostgreSQL workflow engine."

                coi_banner = ""
                if has_conflict:
                    coi_banner = f"""
====================================================
⚠️ CONFLICT OF INTEREST DECLARATION
====================================================
Status: DECLARED (YES)
Details: {coi_details}
"""

                email_body = f"""Hello,
An expenditure item has been processed through the automated compliance engine framework.

Item Details:
- PO Number: {po_num}
- Description: {desc_val}
- Expense Type: {expense_type}
{coi_banner}
Live General Ledger Budget Context:
- GL Account: {gl_code_val}
- Estimated Cost: R{cost:,.2f}
- Total Annual Budget: R{total_budget:,.2f}
- YTD Actual Spend: R{ytd_actual:,.2f}
- Remaining YTD Budget: R{remaining_budget:,.2f}
- 2-Month Run-rate Buffer: R{two_m_buffer:,.2f}

====================================================
🤖 AUTOMATED INTELLIGENCE AUDIT SUMMARY & RECOMMENDATION
====================================================
{ai_analysis_text}
====================================================

Procurement Sourcing Check:
{sourcing_requirement}

Mandatory Audit Condition:
{audit_requirement}

Instruction:
{action_prompt}

Regards,
Automated Compliance Engine"""

                print(f"📨 Dispatching email notification for PO {po_num}...")
                if send_approval_email(to_list, cc_list, email_subject, email_body):
                    # Update status in PostgreSQL to 'Sent'
                    with conn.cursor() as update_cursor:
                        update_cursor.execute(
                            "UPDATE po_log SET submission_status = %s WHERE id = %s;",
                            ("Sent", record['id'])
                        )
                        conn.commit()
                    
                    write_control_log(
                        po_number=po_num, 
                        action_type="Outbound Dispatch", 
                        user_email=os.getenv("SENDER_EMAIL", "system@cathkin.local"), 
                        notes=f"Emailed to approvers: {', '.join(to_list)}."
                    )
                    print(f"🚀 Delivery successful for {po_num}. Status updated to 'Sent'.")
        
    except Exception as db_err:
        print(f"❌ Database polling error: {db_err}")
    finally:
        conn.close()

if __name__ == "__main__":
    print("👀 Running PostgreSQL workflow processing loop...")
    process_postgres_approvals()