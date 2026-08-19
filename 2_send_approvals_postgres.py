import os
import json
import smtplib
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import csv
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from duckduckgo_search import DDGS
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

def fetch_file_bytes_and_mime(location):
    """Fetches binary data from a local filepath or remote Vercel Blob URL."""
    try:
        location_lower = location.lower().strip()
        if location_lower.endswith(('thumbs.db', '.ds_store', '.txt', '.csv', '.xlsm', '.xlsx')):
            return None, None

        mime_type = "application/pdf" if location_lower.endswith('.pdf') else (
            "image/jpeg" if location_lower.endswith(('.jpg', '.jpeg')) else (
            "image/png" if location_lower.endswith('.png') else (
            "image/webp" if location_lower.endswith('.webp') else None
        )))

        if not mime_type:
            mime_type = "application/pdf"

        if location.startswith("http://") or location.startswith("https://"):
            req = urllib.request.Request(location, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as resp:
                data = resp.read()
                return data, mime_type
        elif os.path.exists(location):
            with open(location, "rb") as f:
                return f.read(), mime_type
    except Exception as err:
        log(f"Unable to read file/URL ({location}): {err}", "WARNING")
    
    return None, None


def get_prompt_by_process(cursor, process_name):
    """Fetches prompt template only if is_active is True."""
    if not cursor:
        raise ValueError(f"Database cursor required for '{process_name}'.")
    
    cursor.execute("SELECT prompt_template, is_active FROM system_prompts WHERE LOWER(process) = LOWER(%s);", (process_name,))
    row = cursor.fetchone()
    
    if row:
        if not row.get("is_active"):
            log(f"Prompt for '{process_name}' is currently DISABLED. Skipping execution.")
            return None
        return row.get("prompt_template")
    else:
        raise ValueError(f"No prompt template found in DB for process: '{process_name}'")


def get_gemini_analysis(record, cursor):
    """Executes AI analysis phases conditionally based on prompt active status."""
    api_key = os.getenv("GEMINI_API_KEY")
    po_num = str(record.get('po_number', '')).strip()
    
    if not api_key:
        log(f"PO {po_num}: Skipping Gemini API call (GEMINI_API_KEY not found).", "WARNING")
        return "Automated compliance analysis processed via PostgreSQL workflow engine."

    # 1. Initialize ALL variables with default fallbacks at the top
    desc_val = str(record.get('description') or 'Operational Procurement').strip()
    cost = float(record.get('estimated_cost') or 0.0)
    gl_code_val = str(record.get('gl_code') or record.get('master_gl_code') or 'N/A').strip()
    user_recommended_vendor = str(record.get('recommended_vendor') or 'N/A').strip()
    user_justification = str(record.get('justification_notes') or 'N/A').strip()
    coi_status = str(record.get('conflict_of_interest') or 'No').strip()
    coi_details = str(record.get('conflict_details') or 'None').strip()

    extracted_vendor_name = user_recommended_vendor
    cipc_num = "N/A"
    vat_num = "N/A"
    quoted_amount = "N/A"
    includes_vat = "Unspecified"
    search_context = "No direct public OSINT web results returned."

    try:
        client = genai.Client(api_key=api_key)
        
        raw_filepaths = record.get('quote_filepath') or ''
        quote_attachments = [u.strip() for u in raw_filepaths.split(',') if u.strip()]

        # ----------------------------------------------------
        # PHASE 1: QUOTE EVALUATION (If Active)
        # ----------------------------------------------------
        quote_eval_template = get_prompt_by_process(cursor, 'Quote evaluation')
        
        if quote_eval_template:
            parse_prompt_text = quote_eval_template.format(
                po_num=po_num,
                desc_val=desc_val,
                cost=cost,
                gl_code_val=gl_code_val,
                user_recommended_vendor=user_recommended_vendor,
                user_justification=user_justification,
                coi_status=coi_status,
                coi_details=coi_details,
                extracted_vendor_name=extracted_vendor_name,
                cipc_num=cipc_num,
                vat_num=vat_num,
                quoted_amount=quoted_amount,
                includes_vat=includes_vat,
                search_context=search_context
            )
            parse_contents = [parse_prompt_text]

            for file_loc in quote_attachments:
                file_data, mime_type = fetch_file_bytes_and_mime(file_loc)
                if file_data and mime_type:
                    parse_contents.append(types.Part.from_bytes(data=file_data, mime_type=mime_type))

            log(f"PO {po_num}: 📑 Parsing quote metadata...")
            parse_response = client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=parse_contents,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )

            try:
                vendor_meta = json.loads(parse_response.text or "{}")
                extracted_vendor_name = vendor_meta.get("legal_name") or user_recommended_vendor
                cipc_num = vendor_meta.get("cipc_number") or "N/A"
                vat_num = vendor_meta.get("vat_number") or "N/A"
                quoted_amount = vendor_meta.get("quoted_amount") or "N/A"
                includes_vat = vendor_meta.get("includes_vat") or "Unspecified"
            except Exception as meta_err:
                log(f"PO {po_num}: Failed to parse JSON metadata response: {meta_err}", "WARNING")

        # ----------------------------------------------------
        # PHASE 2: OSINT WEB SEARCH (DuckDuckGo)
        # ----------------------------------------------------
        log(f"PO {po_num}: 🌐 Fetching public OSINT records for '{extracted_vendor_name}' via DuckDuckGo...")
        try:
            with DDGS() as ddgs:
                query = f"{extracted_vendor_name} {cipc_num} South Africa compliance risk"
                results = list(ddgs.text(query, max_results=5))
                if results:
                    search_context = ""
                    for r in results:
                        search_context += f"- Title: {r.get('title')}\n  Snippet: {r.get('body')}\n"
        except Exception as search_err:
            log(f"PO {po_num}: DuckDuckGo search lookup failed for '{extracted_vendor_name}': {search_err}", "WARNING")

        # ----------------------------------------------------
        # PHASE 3: COMPANY ASSESSMENT / AUDIT SYNTHESIS (If Active)
        # ----------------------------------------------------
        company_assess_template = get_prompt_by_process(cursor, 'Company assessment')
        
        if not company_assess_template:
            log(f"PO {po_num}: 'Company assessment' is toggled OFF. Skipping final synthesis.")
            return "AI Company Assessment step disabled by administrative toggle."

        synthesis_prompt = company_assess_template.format(
            po_num=po_num,
            desc_val=desc_val,
            cost=cost,
            gl_code_val=gl_code_val,
            user_recommended_vendor=user_recommended_vendor,
            user_justification=user_justification,
            coi_status=coi_status,
            coi_details=coi_details,
            extracted_vendor_name=extracted_vendor_name,
            cipc_num=cipc_num,
            vat_num=vat_num,
            quoted_amount=quoted_amount,
            includes_vat=includes_vat,
            search_context=search_context
        )

        log(f"PO {po_num}: Generating Gemini audit synthesis...")
        synthesis_response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=synthesis_prompt
        )
        
        return synthesis_response.text.strip()

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
                
                cost = float(record.get('estimated_cost') or 0.0)
                desc_val = str(record.get('description') or 'Operational Procurement').strip()
                expense_type = str(record.get('expense_type') or '').strip()
                gl_code_val = str(record.get('gl_code') or record.get('master_gl_code') or 'N/A').strip()
                
                total_budget = float(record.get('total_annual_budget') or 0.0)
                ytd_actual = float(record.get('ytd_actual') or 0.0)
                remaining_budget = float(record.get('variance') or 0.0)
                
                log(f"PO {po_num} Summary: Cost=R{cost:,.2f}, Type='{expense_type}', GL='{gl_code_val}'")

                # Run multi-phase Gemini analysis
                ai_analysis_text = get_gemini_analysis(record, cursor)
                
                # Routing
                to_list = [approver_emails.get("Estate Manager")]
                cc_list = [approver_emails.get("Managing Agent (GEMS)")]
                
                log(f"Routing PO {po_num} -> To: {to_list}, CC: {cc_list}")

                # Format attachment links section for the email
                raw_filepaths = record.get('quote_filepath') or ''
                quote_urls = [u.strip() for u in raw_filepaths.split(',') if u.strip()]

                if quote_urls:
                    formatted_links = "\n".join([f"  • Document {i+1}: {url}" for i, url in enumerate(quote_urls)])
                    attachments_section = f"\nAttached Quote Documents:\n{formatted_links}\n"
                else:
                    attachments_section = "\nAttached Quote Documents:\n  No documents attached.\n"


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
{attachments_section}

====================================================
🤖 AUTOMATED INTELLIGENCE AUDIT SUMMARY & RECOMMENDATION
====================================================
{ai_analysis_text}
====================================================

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