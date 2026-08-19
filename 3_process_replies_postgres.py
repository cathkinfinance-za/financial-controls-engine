import os
import re
import imaplib
import email
from email.header import decode_header
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

def log(msg, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}")

def get_db_connection():
    return psycopg2.connect(
        os.getenv("DATABASE_URL"),
        cursor_factory=RealDictCursor
    )

def ensure_audit_log_table(conn):
    with conn.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS workflow_control_log (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                po_number VARCHAR(100),
                action_type VARCHAR(100),
                actor_email VARCHAR(255),
                system_notes TEXT
            );
        """)
        conn.commit()

def write_control_log(conn, po_number, action_type, user_email, notes=""):
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO workflow_control_log (po_number, action_type, actor_email, system_notes)
                VALUES (%s, %s, %s, %s);
            """, (po_number, action_type, user_email, notes))
            conn.commit()
            log(f"Control log recorded in DB for PO '{po_number}'.")
    except Exception as e:
        log(f"Failed to write control log: {e}", "WARNING")

def fetch_approval_replies():
    imap_server = os.getenv("IMAP_SERVER", "imap.gmail.com")
    email_account = os.getenv("SENDER_EMAIL")
    email_password = os.getenv("SENDER_PASSWORD")

    if not email_account or not email_password:
        log("SENDER_EMAIL or SENDER_PASSWORD missing.", "ERROR")
        return {}, {}

    log(f"Connecting to IMAP inbox at {imap_server}...")
    try:
        mail = imaplib.IMAP4_SSL(imap_server, 993)
        mail.login(email_account, email_password)
        mail.select("inbox")

        # Search for ALL recent messages with "Approval Required" in subject (handles read/unread/self-sent)
        status, messages = mail.search(None, 'SUBJECT "Approval Required"')
        if status != "OK" or not messages or messages == [b'']:
            log("No matching approval emails found in inbox.")
            mail.logout()
            return {}, {}

        raw_data = messages[0]
        email_ids = raw_data.split() if isinstance(raw_data, bytes) else str(raw_data).split()
        
        # Take the most recent 20 emails to keep runs fast
        latest_email_ids = email_ids[-20:]
        log(f"Scanning latest {len(latest_email_ids)} workflow email(s)...")

        approvals = {}
        rejections = {}

        for e_id in latest_email_ids:
            res, msg_data = mail.fetch(e_id, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple) and len(response_part) > 1:
                    raw_bytes = response_part[1]
                    if not isinstance(raw_bytes, bytes):
                        continue

                    msg = email.message_from_bytes(raw_bytes)
                    subject_header = msg.get('Subject', '')
                    
                    decoded_subject = ""
                    if subject_header:
                        for text, encoding in decode_header(subject_header):
                            if isinstance(text, bytes):
                                decoded_subject += text.decode(encoding if encoding else 'utf-8', errors='ignore')
                            else:
                                decoded_subject += str(text)

                    # Extract PO number from brackets [PO_NUM]
                    po_match = re.search(r'\[([A-Za-z0-9_-]+)\]', decoded_subject)
                    if not po_match:
                        continue

                    po_number = po_match.group(1).strip()

                    # Extract body payload
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                payload = part.get_payload(decode=True)
                                if isinstance(payload, bytes):
                                    body = payload.decode(errors='ignore')
                                break
                    else:
                        payload = msg.get_payload(decode=True)
                        if isinstance(payload, bytes):
                            body = payload.decode(errors='ignore')

                    top_reply = body.strip()
                    reply_splitters = ["-----Original Message-----", "From:", "On ", "Am ", "Le ", "wrote:"]
                    for splitter in reply_splitters:
                        if splitter in top_reply:
                            top_reply = top_reply.split(splitter)[0]

                    # Combine subject + body for keyword check
                    full_payload = f"{decoded_subject} {top_reply}".upper()
                    sender = str(msg.get('From', '')).lower()
                    sender_match = re.search(r'<([^>]+)>', sender)
                    sender_clean = sender_match.group(1).strip() if sender_match else sender.strip()

                    if "REJECTED" in full_payload or "REJECT" in full_payload:
                        rejections[po_number] = {"sender": sender_clean}
                    elif "APPROVED" in full_payload or "APPROVE" in full_payload:
                        approvals[po_number] = {"sender": sender_clean}

        mail.logout()
        return approvals, rejections

    except Exception as e:
        log(f"IMAP Processing Error: {e}", "ERROR")
        return {}, {}

def process_replies():
    log("Starting Inbox Reply Processor...")
    approvals, rejections = fetch_approval_replies()
    
    if not approvals and not rejections:
        log("No actionable email replies detected. Processing complete.")
        return

    try:
        conn = get_db_connection()
        ensure_audit_log_table(conn)
        
        all_po_numbers = list(set(list(rejections.keys()) + list(approvals.keys())))
        log(f"Found replies for PO numbers: {all_po_numbers}")

        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM po_log WHERE LOWER(po_number) = ANY(%s);",
                ([p.lower() for p in all_po_numbers],)
            )
            records = cursor.fetchall()
            po_map = {str(row['po_number']).strip().lower(): row for row in records if row.get('po_number')}

            current_timestamp = datetime.datetime.now().strftime("%Y-%m-%d")

            # 1. PROCESS REJECTIONS
            for po_num, meta in rejections.items():
                record = po_map.get(po_num.lower())
                if not record:
                    log(f"PO '{po_num}' not found in database records.", "WARNING")
                    continue

                rejector_email = meta.get("sender", "Approver (Via Email)")
                
                cursor.execute("""
                    UPDATE po_log 
                    SET submission_status = 'Rejected',
                        actioned_by = %s,
                        actioned_date = %s
                    WHERE id = %s;
                """, (rejector_email, current_timestamp, record['id']))
                conn.commit()

                write_control_log(conn, record['po_number'], "Inbound Rejection", rejector_email, "Marked as rejected via email reply.")
                log(f"❌ PO '{record['po_number']}' successfully updated to REJECTED in database!")

            # 2. PROCESS APPROVALS
            for po_num, meta in approvals.items():
                record = po_map.get(po_num.lower())
                if not record:
                    log(f"PO '{po_num}' not found in database records.", "WARNING")
                    continue

                approver_email = meta.get("sender", "Approver (Via Email)")
                
                cursor.execute("""
                    UPDATE po_log 
                    SET submission_status = 'Approved',
                        actioned_by = %s,
                        actioned_date = %s
                    WHERE id = %s;
                """, (approver_email, current_timestamp, record['id']))
                conn.commit()

                write_control_log(conn, record['po_number'], "Inbound Approval", approver_email, "Marked as approved via email reply.")
                log(f"✅ PO '{record['po_number']}' successfully updated to APPROVED in database!")

    except Exception as db_err:
        log(f"Database sync error: {db_err}", "CRITICAL")
    finally:
        if 'conn' in locals() and conn:
            conn.close()
            log("PostgreSQL connection closed.")

if __name__ == "__main__":
    process_replies()