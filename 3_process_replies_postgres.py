import os
import re
import json
import imaplib
import email
from email.header import decode_header
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

# ==========================================
# 1. LOGGING & DATABASE HELPERS
# ==========================================
def log(msg, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}")

def get_db_connection():
    return psycopg2.connect(
        os.getenv("DATABASE_URL"),
        cursor_factory=RealDictCursor
    )

def ensure_audit_log_table(conn):
    """Ensures the workflow_control_log table exists in PostgreSQL."""
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
    """Writes an immutable control log entry directly into PostgreSQL."""
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO workflow_control_log (po_number, action_type, actor_email, system_notes)
                VALUES (%s, %s, %s, %s);
            """, (po_number, action_type, user_email, notes))
            conn.commit()
            log(f"Control log recorded in PostgreSQL for PO {po_number}.")
    except Exception as e:
        log(f"Failed to write control log for PO {po_number}: {e}", "WARNING")

# ==========================================
# 2. INBOX PROCESSING ENGINE
# ==========================================
def fetch_approval_replies():
    """Connects via IMAP and extracts APPROVED/REJECTED responses."""
    imap_server = os.getenv("IMAP_SERVER", "imap.gmail.com")
    email_account = os.getenv("SENDER_EMAIL")
    email_password = os.getenv("SENDER_PASSWORD")

    if not email_account or not email_password:
        log("SENDER_EMAIL or SENDER_PASSWORD missing. Aborting inbox check.", "ERROR")
        return {}, {}

    log(f"Connecting to IMAP inbox at {imap_server}...")
    try:
        mail = imaplib.IMAP4_SSL(imap_server, 993)
        mail.login(email_account, email_password)
        mail.select("inbox")

        status, messages = mail.search(None, 'UNSEEN SUBJECT "Approval Required"')
        if status != "OK" or not messages or messages == [b'']:
            log("No unseen approval workflow emails found.")
            mail.logout()
            return {}, {}

        raw_data = messages[0]
        email_ids = raw_data.split() if isinstance(raw_data, bytes) else str(raw_data).split()
        log(f"Found {len(email_ids)} unread workflow email(s) to process.")

        approvals = {}
        rejections = {}

        for e_id in email_ids:
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

                    # Extract PO number from [PO-XXX] subject format
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

                    # Parse top message reply only
                    top_reply = body.strip()
                    reply_splitters = ["-----Original Message-----", "From:", "On ", "Am ", "Le ", "wrote:"]
                    for splitter in reply_splitters:
                        if splitter in top_reply:
                            top_reply = top_reply.split(splitter)[0]

                    body_upper = top_reply.upper()
                    sender = str(msg.get('From', '')).lower()
                    sender_match = re.search(r'<([^>]+)>', sender)
                    sender_clean = sender_match.group(1).strip() if sender_match else sender.strip()

                    if "REJECTED" in body_upper or "REJECT" in body_upper:
                        rejections[po_number] = {"sender": sender_clean}
                        mail.store(e_id, '+FLAGS', '\\Seen')
                    elif "APPROVED" in body_upper or "APPROVE" in body_upper:
                        approvals[po_number] = {"sender": sender_clean}
                        mail.store(e_id, '+FLAGS', '\\Seen')

        mail.logout()
        return approvals, rejections

    except Exception as e:
        log(f"IMAP Processing Error: {e}", "ERROR")
        return {}, {}

# ==========================================
# 3. POSTGRESQL STATE SYNCHRONIZER
# ==========================================
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
        log(f"Matching {len(all_po_numbers)} PO number(s) in PostgreSQL database...")

        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM po_log WHERE po_number = ANY(%s);",
                (all_po_numbers,)
            )
            records = cursor.fetchall()
            po_map = {row['po_number'].strip(): row for row in records if row.get('po_number')}

            current_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # ------------------------------------
            # 1. PROCESS REJECTIONS
            # ------------------------------------
            for po_num, meta in rejections.items():
                record = po_map.get(po_num)
                if not record:
                    log(f"PO {po_num} found in rejection email, but record does not exist in DB.", "WARNING")
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

                write_control_log(conn, po_num, "Inbound Rejection", rejector_email, "Marked as rejected via email reply.")
                log(f"❌ PO {po_num} marked as REJECTED by {rejector_email}.")

            # ------------------------------------
            # 2. PROCESS APPROVALS
            # ------------------------------------
            for po_num, meta in approvals.items():
                record = po_map.get(po_num)
                if not record:
                    log(f"PO {po_num} found in approval email, but record does not exist in DB.", "WARNING")
                    continue

                approver_email = meta.get("sender", "Unknown Approver")
                current_status = str(record.get('submission_status') or '').strip().lower()
                system_status = str(record.get('system_status') or '').strip()

                # Rule Check: Dual Sign-off required if category requires Chairman / Second sign-off
                requires_dual_signoff = "requires chairman sign-off" in system_status.lower() or "high-value" in system_status.lower()

                if requires_dual_signoff:
                    if current_status in ['sent', 'submitted', 'awaiting approval', 'pending']:
                        # First Vote
                        cursor.execute("""
                            UPDATE po_log 
                            SET submission_status = 'Approved (1/2)',
                                actioned_by = %s,
                                actioned_date = %s
                            WHERE id = %s;
                        """, (approver_email, current_timestamp, record['id']))
                        conn.commit()

                        write_control_log(conn, po_num, "Inbound Partial Approval", approver_email, "First sign-off captured.")
                        log(f"🔒 PO {po_num} updated to 'Approved (1/2)' by {approver_email}. Awaiting 2nd sign-off.")

                    elif '1/2' in current_status:
                        first_voter = str(record.get('actioned_by') or '').strip()

                        # Prevent double-voting by the same individual
                        if approver_email.lower() in first_voter.lower():
                            log(f"⚠️ Double-voting blocked for PO {po_num} by {approver_email}.", "WARNING")
                            continue

                        # Second Vote
                        combined_voters = f"{first_voter} | {approver_email}"
                        cursor.execute("""
                            UPDATE po_log 
                            SET submission_status = 'Approved',
                                actioned_by = %s,
                                actioned_date = %s
                            WHERE id = %s;
                        """, (combined_voters, current_timestamp, record['id']))
                        conn.commit()

                        write_control_log(conn, po_num, "Inbound Final Approval", approver_email, "Dual authorization complete.")
                        log(f"✅ PO {po_num} fully APPROVED (2/2). Combined sign-offs: {combined_voters}.")

                else:
                    # Single Sign-off Standard Logic
                    if current_status in ['sent', 'submitted', 'awaiting approval', 'pending']:
                        cursor.execute("""
                            UPDATE po_log 
                            SET submission_status = 'Approved',
                                actioned_by = %s,
                                actioned_date = %s
                            WHERE id = %s;
                        """, (approver_email, current_timestamp, record['id']))
                        conn.commit()

                        write_control_log(conn, po_num, "Inbound Approval", approver_email, "Single sign-off complete.")
                        log(f"✅ PO {po_num} fully APPROVED by {approver_email}.")
                    else:
                        log(f"⏩ Skipping PO {po_num}: Current status is already '{current_status}'.")

    except Exception as db_err:
        log(f"Database sync error: {db_err}", "CRITICAL")
    finally:
        if 'conn' in locals() and conn:
            conn.close()
            log("PostgreSQL connection closed.")

if __name__ == "__main__":
    process_replies()