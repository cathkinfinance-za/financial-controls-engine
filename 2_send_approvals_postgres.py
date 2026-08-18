import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import csv
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from google import genai
from google.genai import types

def get_db_connection():
    return psycopg2.connect(
        os.getenv("DATABASE_URL"),
        cursor_factory=RealDictCursor
    )

def write_control_log(po_number, action_type, user_email, notes=""):
    log_file = os.getenv("AUDIT_LOG_PATH", "workflow_control_log.csv")
    file_exists = os.path.exists(log_file)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        with open(log_file, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "PO Number", "Action Type", "Actor/Operator", "System Notes"])
            writer.writerow([timestamp, po_number, action_type, user_email, notes])
            print(f"Control Measure Signed: Row appended for {po_number}.")
    except Exception as e:
        print(f"Control Log Warning: {e}")

def get_gemini_analysis(po_num, desc_val, expense_type, cost, remaining_budget):
    """Uses Gemini API to generate live audit analysis."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "Automated compliance analysis processed via PostgreSQL workflow engine."

    try:
        client = genai.Client(api_key=api_key)
        prompt = f"""
Perform a concise compliance and budget risk evaluation for Purchase Order {po_num}:
- Description: {desc_val}
- Expense Type: {expense_type}
- Estimated Cost: R{cost:,.2f}
- Remaining GL Budget: R{remaining_budget:,.2f}

Provide 2-3 bullet points analyzing spend risk, budget compliance, and procurement approval recommendations.
"""
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return "Automated compliance analysis processed via PostgreSQL workflow engine."

def send_approval_email(to_emails, cc_emails, subject, body, attachment_paths=None):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    
    # Filter out None/empty items
    to_emails = [e for e in to_emails if e]
    cc_emails = [e for e in cc_emails if e]
    
    if not to_emails:
        print(f"❌ Error: No valid recipient email addresses for {subject}.")
        return False

    try:
        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg['From'] = sender_email
        msg['To'] = ", ".join(to_emails)
        if cc_emails:
            msg['Cc'] = ", ".join(cc_emails)
        
        msg.attach(MIMEText(body, 'plain'))
        
        all_recipients = list(set(to_emails + cc_emails))
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, all_recipients, msg.as_string())
        return True
    except Exception as e:
        print(f"❌ Error sending email for {subject}: {e}")
        return False

def process_postgres_approvals():
    print("Polling Neon PostgreSQL database rows...")
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT p.*, m.gl_code as master_gl_code, m.total_budget as total_annual_budget, m.ytd as ytd_actual, m.variance 
                FROM po_log p
                LEFT JOIN master_budget m ON p.gl_code_id = m.id
                WHERE LOWER(p.submission_status) LIKE 'submit%';
            """)
            records = cursor.fetchall()
            
            cursor.execute("SELECT role, email FROM approvers;")
            approver_records = cursor.fetchall()
            approver_emails = {row['role'].strip(): row['email'].strip() for row in approver_records}

            for record in records:
                po_num = str(record.get('po_number', '')).strip()
                if not po_num:
                    continue
                
                system_status = str(record.get('system_status', '')).strip()
                if "blocked" in system_status.lower() or "action required" in system_status.lower():
                    print(f"⏩ Skipping {po_num}: Blocked status.")
                    continue
                
                cost = float(record.get('estimated_cost', 0.0))
                desc_val = str(record.get('description', 'Operational Procurement')).strip()
                expense_type = str(record.get('expense_type', '')).strip()
                gl_code_val = str(record.get('master_gl_code', 'N/A')).strip()
                
                total_budget = float(record.get('total_annual_budget', 0.0))
                ytd_actual = float(record.get('ytd_actual', 0.0))
                remaining_budget = float(record.get('variance', 0.0))
                
                # Dynamic Gemini Intelligence Summary
                ai_analysis_text = get_gemini_analysis(po_num, desc_val, expense_type, cost, remaining_budget)
                
                # Routing
                to_list = [approver_emails.get("Estate Manager")]
                cc_list = [approver_emails.get("Managing Agent (GEMS)")]
                
                email_subject = f"[{po_num}] Approval Required: {desc_val}"
                email_body = f"""Hello,

An expenditure item has been submitted for approval.

Item Details:
- PO Number: {po_num}
- Description: {desc_val}
- Expense Type: {expense_type}
- GL Account: {gl_code_val}
- Estimated Cost: R{cost:,.2f}
- Total Annual Budget: R{total_budget:,.2f}
- YTD Actual Spend: R{ytd_actual:,.2f}
- Remaining YTD Budget: R{remaining_budget:,.2f}

====================================================
🤖 AUTOMATED INTELLIGENCE AUDIT SUMMARY & RECOMMENDATION
====================================================
{ai_analysis_text}
====================================================

Instruction:
Please review the purchase order details and reply directly to this message typing either "APPROVED" or "REJECTED".

Regards,
Automated Compliance Engine"""

                if send_approval_email(to_list, cc_list, email_subject, email_body):
                    with conn.cursor() as update_cursor:
                        update_cursor.execute(
                            "UPDATE po_log SET submission_status = %s WHERE id = %s;",
                            ("Sent", record['id'])
                        )
                        conn.commit()
                    write_control_log(po_num, "Outbound Dispatch", os.getenv("SENDER_EMAIL"), "Emailed approvers.")
                    print(f"🚀 Delivery successful for {po_num}.")
    finally:
        conn.close()

if __name__ == "__main__":
    process_postgres_approvals()