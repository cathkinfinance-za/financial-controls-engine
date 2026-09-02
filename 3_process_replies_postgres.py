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

def extract_po_id(subject, body):
    # Combine subject and body and match PO-ID: followed by digits flexibly
    text_to_search = f"{subject or ''} {body or ''}"
    match = re.search(r"PO-ID:\s*(\d+)", text_to_search, re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_incoming_reply(email_subject, email_body):
    text_to_search = f"{email_subject} {email_body}"
    # Match [PO-ID: 3] or PO-ID: 3
    match = re.search(r"PO-ID:\s*(\d+)", text_to_search, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def fetch_approval_replies():
    imap_server = os.getenv("IMAP_SERVER", "imap.gmail.com")
    email_account = os.getenv("SENDER_EMAIL")
    email_password = os.getenv("SENDER_PASSWORD")

    if not email_account or not email_password:
        log("SENDER_EMAIL or SENDER_PASSWORD missing.", "ERROR")
        return {}, {}, {}, {}

    log(f"Connecting to IMAP inbox at {imap_server}...")
    try:
        mail = imaplib.IMAP4_SSL(imap_server, 993)
        mail.login(email_account, email_password)
        mail.select("inbox")

        # Pull the latest emails and let your Python logic filter them:
        status, messages = mail.search(None, 'ALL')
        if status != "OK" or not messages or messages == [b'']:
            log("No matching approval emails found in inbox.")
            mail.logout()
            return {}, {}, {}, {}

        raw_data = messages[0]
        email_ids = raw_data.split() if isinstance(raw_data, bytes) else str(raw_data).split()
        
        latest_email_ids = email_ids[-20:]
        log(f"Scanning latest {len(latest_email_ids)} workflow email(s)...")

        # 1. Initialize all 4 response categories
        recommendations_approval = {}
        recommendations_rejection = {}
        final_approvals = {}
        final_rejections = {}

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
                    
                    po_id = extract_po_id(decoded_subject, body)
                    log(f"Inspecting Subject: '{decoded_subject}' | Extracted PO-ID: {po_id}")
                    if not po_id:
                        continue

                    top_reply = body.strip()
                    reply_splitters = ["-----Original Message-----", "From:", "On ", "Am ", "Le ", "wrote:"]
                    for splitter in reply_splitters:
                        if splitter in top_reply:
                            top_reply = top_reply.split(splitter)[0]

                    # 2. Define full_payload BEFORE performing keyword checks
                    full_payload = f"{decoded_subject} {top_reply}".upper()
                    sender = str(msg.get('From', '')).lower()
                    sender_match = re.search(r'<([^>]+)>', sender)
                    sender_clean = sender_match.group(1).strip() if sender_match else sender.strip()

                    # 3. Categorize reply
                    if "RECOMMEND FOR REJECTION" in full_payload or "RECOMMEND REJECT" in full_payload:
                        recommendations_rejection[po_id] = {"sender": sender_clean}
                    elif "RECOMMEND FOR APPROVAL" in full_payload or "RECOMMEND APPROVE" in full_payload:
                        recommendations_approval[po_id] = {"sender": sender_clean}
                    elif "REJECTED" in full_payload or "REJECT" in full_payload:
                        final_rejections[po_id] = {"sender": sender_clean}
                    elif "APPROVED" in full_payload or "APPROVE" in full_payload:
                        final_approvals[po_id] = {"sender": sender_clean}

        mail.logout()
        return recommendations_approval, recommendations_rejection, final_approvals, final_rejections

    except Exception as e:
        log(f"IMAP Processing Error: {e}", "ERROR")
        return {}, {}, {}, {}

def verify_approver_authority(cursor, sender_email, required_permission):
    cursor.execute(
        "SELECT approval_permission FROM approvers WHERE LOWER(email) = LOWER(%s);",
        (sender_email,)
    )
    row = cursor.fetchone()
    if not row:
        return False
    
    user_permission = row.get('approval_permission')
    if required_permission == 'Approval Authority' and user_permission == 'Approval Authority':
        return True
    if required_permission == 'Finance Review' and user_permission in ['Finance Review', 'Approval Authority']:
        return True
        
    return False

def process_replies():
    log("Starting Inbox Reply Processor...")
    recommendations_approval, recommendations_rejection, final_approvals, final_rejections = fetch_approval_replies()
    
    # 1. Guard against empty responses across all 4 categories
    if not any([recommendations_approval, recommendations_rejection, final_approvals, final_rejections]):
        log("No actionable email replies detected. Processing complete.")
        return

    try:
        conn = get_db_connection()
        ensure_audit_log_table(conn)
        
        # 2. Combine all unique PO numbers across all categories
        all_po_ids = list(set(
            list(recommendations_approval.keys()) + 
            list(recommendations_rejection.keys()) + 
            list(final_approvals.keys()) + 
            list(final_rejections.keys())
        ))
        log(f"Found replies for PO IDs: {all_po_ids}")

        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM po_log WHERE id = ANY(%s);",
                (all_po_ids,)
            )
            records = cursor.fetchall()
            po_map = {str(row['po_number']).strip().lower(): row for row in records if row.get('po_number')}
            po_id_map = {row['id']: row for row in records if row.get('id')}

            current_timestamp = datetime.datetime.now().strftime("%Y-%m-%d")

            # 3. PROCESS FINANCE REVIEW RECOMMENDATIONS
            for raw_po_id, meta in recommendations_approval.items():
                try:
                    po_id = int(raw_po_id)
                except (ValueError, TypeError):
                    continue

                record = po_id_map.get(po_id)
                if record and record.get('submission_status') == 'Finance Review':
                    cursor.execute("""
                        UPDATE po_log 
                        SET submission_status = 'Finance Recommended',
                            actioned_by = %s,
                            actioned_date = %s
                        WHERE id = %s;
                    """, (meta.get("sender"), current_timestamp, record['id']))
                    conn.commit()
                    write_control_log(conn, record['po_number'], "Finance Review Recommendation", meta.get("sender"), "Finance Committee recommended for approval.")
                    log(f"✅ PO ID '{record['id']}' (PO: '{record['po_number']}') updated to FINANCE RECOMMENDED.")

            for raw_po_id, meta in recommendations_rejection.items():
                try:
                    po_id = int(raw_po_id)
                except (ValueError, TypeError):
                    continue

                record = po_id_map.get(po_id)
                if record and record.get('submission_status') == 'Finance Review':
                    cursor.execute("""
                        UPDATE po_log 
                        SET submission_status = 'Finance Rejected',
                            actioned_by = %s,
                            actioned_date = %s
                        WHERE id = %s;
                    """, (meta.get("sender"), current_timestamp, record['id']))
                    conn.commit()

                    write_control_log(conn, record['po_number'], "Finance Review Rejection", meta.get("sender"), "Finance Committee recommended for rejection.")
                    log(f"❌ PO ID '{record['id']}' (PO: '{record['po_number']}') updated to FINANCE REJECTED.")

            # 4. PROCESS FINAL APPROVALS & REJECTIONS
            for raw_po_id, meta in final_approvals.items():
                try:
                    po_id = int(raw_po_id)
                except (ValueError, TypeError):
                    continue

                record = po_id_map.get(po_id)
                if record:
                    # ---> AUTHORIZATION CHECK <---
                    if verify_approver_authority(cursor, meta["sender"], 'Approval Authority'):
                        cursor.execute("""
                            UPDATE po_log 
                            SET submission_status = 'Approved',
                                actioned_by = %s,
                                actioned_date = %s
                            WHERE id = %s;
                        """, (meta.get("sender"), current_timestamp, record['id']))
                        conn.commit()
                        write_control_log(conn, record['po_number'], "Inbound Approval", meta.get("sender"), "Marked as approved via email reply.")
                        log(f"✅ PO '{record['po_number']}' updated to APPROVED.")
                    else:
                        log(f"⚠️ Unauthorized action attempt by {meta['sender']} for PO {record['po_number']}", "WARNING")
                        write_control_log(conn, record['po_number'], "Unauthorized Action Blocked", meta["sender"], "Sender lacks approval authority.")

            for raw_po_id, meta in final_rejections.items():
                try:
                    po_id = int(raw_po_id)
                except (ValueError, TypeError):
                    continue

                record = po_id.get(po_id)
                if record:
                    cursor.execute("""
                        UPDATE po_log 
                        SET submission_status = 'Rejected',
                            actioned_by = %s,
                            actioned_date = %s
                        WHERE id = %s;
                    """, (meta.get("sender"), current_timestamp, record['id']))
                    conn.commit()
                    write_control_log(conn, record['po_number'], "Inbound Rejection", meta.get("sender"), "Marked as rejected via email reply.")
                    log(f"❌ PO ID '{record['id']}' (PO: '{record['po_number']}') updated to REJECTED.")

    except Exception as db_err:
        log(f"Database sync error: {db_err}", "CRITICAL")
    finally:
        if 'conn' in locals() and conn:
            conn.close()
            log("PostgreSQL connection closed.")



if __name__ == "__main__":
    process_replies()