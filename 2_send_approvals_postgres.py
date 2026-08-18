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

def log(msg, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}")

def check_environment():
    log("Checking environment configurations...")
    db_url = os.getenv("DATABASE_URL")
    sender = os.getenv("SENDER_EMAIL")
    gemini_key = os.getenv("GEMINI_API_KEY")

    if not db_url:
        log("DATABASE_URL is NOT set in environment variables!", "ERROR")
    else:
        log("DATABASE_URL detected.")

    if not sender:
        log("SENDER_EMAIL is NOT set!", "WARNING")
    else:
        log(f"SENDER_EMAIL configured as: {sender}")

    if not gemini_key:
        log("GEMINI_API_KEY is NOT set. Script will use fallback static text.", "WARNING")
    else:
        log("GEMINI_API_KEY detected.")

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
            log(f"Control audit log written for PO {po_number}.")
    except Exception as e:
        log(f"Control Log Error: {e}", "WARNING")

def get_gemini_analysis(po_num, desc_val, expense_type, cost, remaining_budget):
    """Uses Gemini API to generate live audit analysis."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log(f"PO {po_num}: Skipping Gemini API call (GEMINI_API_KEY not found).", "WARNING")
        return "Automated compliance analysis processed via PostgreSQL workflow engine."

    try:
        log(f"PO {po_num}: Contacting Gemini API for AI analysis...")
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
            model="gemini-2.5-flash",
            contents=prompt,
        )
        log(f"PO {po_num}: Successfully generated Gemini analysis.")
        return response.text.strip()
    except Exception as e:
        log(f"PO {po_num}: Gemini API Exception: {e}", "ERROR")
        return "Automated compliance analysis processed via PostgreSQL workflow engine."

def send_approval_email(to_emails, cc_emails, subject, body, attachment_paths=None):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    
    to_emails = [e for e in to_emails if e]
    cc_emails = [e for e in cc_emails if e]
    
    if not to_emails:
        log(f"Cannot send email '{subject}': No valid recipient email addresses.", "ERROR")
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
        log(f"Attempting SMTP connection to {smtp_server}:{smtp_port}...")
        
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, all_recipients, msg.as_string())
            
        log(f"Email sent successfully to: {', '.join(all_recipients)}")
        return True
    except Exception as e:
        log(f"SMTP Error for '{subject}': {e}", "ERROR")
        return False

def process_postgres_approvals():
    check_environment()
    log("Connecting to PostgreSQL database...")
    
    try:
        conn = get_db_connection()
        log("Database connection established.")
    except Exception as e:
        log(f"Failed to connect to PostgreSQL: {e}", "CRITICAL")
        return

    try:
        with conn.cursor() as cursor:
            log("Querying 'po_log' table for records with status 'Submit for Approval'...")
            cursor.execute("""
                SELECT p.*, m.gl_code as master_gl_code, m.total_budget as total_annual_budget, m.ytd as ytd_actual, m.variance 
                FROM po_log p
                LEFT JOIN master_budget m ON p.gl_code_id = m.id
                WHERE LOWER(p.submission_status) LIKE 'submit%';
            """)
            records = cursor.fetchall()
            log(f"Query executed. Found {len(records)} pending record(s) to process.")

            if not records:
                log("No purchase orders currently pending approval. Processing complete.")
                return

            log("Fetching approver matrix mapping...")
            cursor.execute("SELECT role, email FROM approvers;")
            approver_records = cursor.fetchall()
            approver_emails = {row['role'].strip(): row['email'].strip() for row in approver_records}
            log(f"Loaded approver roles: {list(approver_emails.keys())}")

            for idx, record in enumerate(records, start=1):
                po_num = str(record.get('po_number', '')).strip()
                log(f"--- Processing Record {idx}/{len(records)}: PO #{po_num} ---")

                if not po_num:
                    log("Skipping record: Missing PO number.", "WARNING")
                    continue
                
                system_status = str(record.get('system_status', '')).strip()
                if "blocked" in system_status.lower() or "action required" in system_status.lower():
                    log(f"⏩ Skipping PO {po_num}: Blocked by system status ('{system_status}').", "WARNING")
                    continue
                
                cost = float(record.get('estimated_cost', 0.0))
                desc_val = str(record.get('description', 'Operational Procurement')).strip()
                expense_type = str(record.get('expense_type', '')).strip()
                gl_code_val = str(record.get('master_gl_code', 'N/A')).strip()
                
                total_budget = float(record.get('total_annual_budget', 0.0))
                ytd_actual = float(record.get('ytd_actual', 0.0))
                remaining_budget = float(record.get('variance', 0.0))
                
                log(f"PO {po_num} Summary: Cost=R{cost:,.2f}, Type='{expense_type}', GL='{gl_code_val}'")

                # Generate live Gemini analysis
                ai_analysis_text = get_gemini_analysis(po_num, desc_val, expense_type, cost, remaining_budget)
                
                # Routing
                to_list = [approver_emails.get("Estate Manager")]
                cc_list = [approver_emails.get("Managing Agent (GEMS)")]
                
                log(f"Routing PO {po_num} -> To: {to_list}, CC: {cc_list}")

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
                    log(f"Updating database status for PO {po_num} to 'Sent'...")
                    with conn.cursor() as update_cursor:
                        update_cursor.execute(
                            """
                            UPDATE po_log 
                            SET submission_status = %s,
                                ai_recommendation_summary = %s 
                            WHERE id = %s;
                            """,
                            ("Sent", ai_analysis_text, record['id'])
                        )
                        conn.commit()
                    log(f"Database successfully updated for PO {po_num}.")
                    write_control_log(po_num, "Outbound Dispatch", os.getenv("SENDER_EMAIL"), "Emailed approvers.")
                else:
                    log(f"Failed to dispatch approval email for PO {po_num}. Skipping database update.", "ERROR")

    except Exception as db_err:
        log(f"Database error during polling execution: {db_err}", "CRITICAL")
    finally:
        conn.close()
        log("PostgreSQL connection closed. Processing run complete.")

if __name__ == "__main__":
    process_postgres_approvals()