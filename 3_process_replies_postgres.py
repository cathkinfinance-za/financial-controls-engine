import os
import re
import imaplib
import email
import email.utils
from email.header import decode_header
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from google import genai
import json


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
                actor_operator VARCHAR(255),
                system_notes TEXT
            );
        """)
        conn.commit()

def write_control_log(conn, po_number, action_type, actor_email, notes, timestamp=None):
    with conn.cursor() as cursor:
        cursor.execute("""
            INSERT INTO workflow_control_log (po_number, action_type, actor_email, system_notes, timestamp)
            VALUES (%s, %s, %s, %s, %s);
        """, (po_number, action_type, actor_email, notes, timestamp or datetime.datetime.now()))
        conn.commit()
        log(f"Control log recorded in DB for PO '{po_number}'.")
    except Exception as e:
        log(f"Failed to write control log: {e}", "WARNING")

def extract_po_id(subject, body):
    text_to_search = f"{subject or ''} {body or ''}"
    match = re.search(r"PO-ID:\s*(\d+)", text_to_search, re.IGNORECASE)
    return int(match.group(1)) if match else None

def get_system_prompt_from_db(conn, process_name="Email Reply Classification"):
    """
    Fetches the prompt template, selected model, and active status from the database table system_prompts.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT prompt_template, selected_model, is_active 
                FROM system_prompts 
                WHERE process = %s;
                """,
                (process_name,)
            )
            row = cur.fetchone()
            if row:
                return {
                    "template": row['prompt_template'],
                    "model": row['selected_model'] or 'gemini-3.5-flash',
                    "is_active": row['is_active']
                }
    except Exception as e:
        log(f"Error fetching prompt from system_prompts: {e}", "WARNING")
    
    # Fallback default if table row is missing
    return {
        "template": "Sender: {sender_email}\nSubject: {subject}\nBody: {body}\nClassify as APPROVE, REJECT, RECOMMEND_APPROVE, RECOMMEND_REJECT, or NEUTRAL as JSON.",
        "model": "gemini-3.5-flash",
        "is_active": True
    }


def classify_reply_with_gemini(conn, sender_email, subject, body):
    """
    Uses the modern google-genai SDK and database prompt template, 
    safely replacing placeholders without crashing on JSON curly braces.
    """
    prompt_config = get_system_prompt_from_db(conn, process_name="Email Reply Classification")
    
    if not prompt_config["is_active"]:
        log("Email Reply Classification prompt is disabled in system_prompts. Defaulting to NEUTRAL.", "WARNING")
        return {"classification": "NEUTRAL", "summary_notes": "Prompt disabled in system configuration."}

    template = prompt_config["template"]
    model_name = prompt_config["model"] or "gemini-3.5-flash"

    # Safely replace placeholders using string replacement 
    # (This prevents Python from crashing on the JSON curly braces in your template)
    prompt = template.replace("{sender_email}", sender_email) \
                     .replace("{subject}", subject or "") \
                     .replace("{body}", body or "")

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log("GEMINI_API_KEY environment variable is missing.", "ERROR")
        return {"classification": "NEUTRAL", "summary_notes": "API key missing."}

    try:
        # Explicitly pass the API key to the modern google-genai client
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
        )
        
        cleaned_text = response.text.replace("```json", "").replace("```", "").strip()
        result = json.loads(cleaned_text)
        return result
    except Exception as e:
        log(f"Gemini SDK classification error: {e}", "WARNING")
        return {"classification": "NEUTRAL", "summary_notes": "Failed to parse via Gemini fallback."}
        
def fetch_approval_replies(conn):
    imap_server = os.getenv("IMAP_SERVER", "imap.gmail.com")
    email_account = os.getenv("SENDER_EMAIL")
    email_password = os.getenv("SENDER_PASSWORD")

    if not email_account or not email_password:
        log("SENDER_EMAIL or SENDER_PASSWORD missing.", "ERROR")
        return [], [], [], []

    log(f"Connecting to IMAP inbox at {imap_server}...")
    try:
        mail = imaplib.IMAP4_SSL(imap_server, 993)
        mail.login(email_account, email_password)
        mail.select("inbox")

        status, messages = mail.search(None, 'UNSEEN')
        if status != "OK" or not messages or messages == [b'']:
            log("No new unread approval emails found in inbox.")
            mail.logout()
            return [], [], [], []

        raw_data = messages[0]
        email_ids = raw_data.split() if isinstance(raw_data, bytes) else str(raw_data).split()
        latest_email_ids = email_ids[-20:]
        log(f"Scanning latest {len(latest_email_ids)} unread workflow email(s)...")

        recommendations_approval = []
        recommendations_rejection = []
        final_approvals = []
        final_rejections = []

        for e_id in latest_email_ids:
            res, msg_data = mail.fetch(e_id, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple) and len(response_part) > 1:
                    raw_bytes = response_part[1]
                    if not isinstance(raw_bytes, bytes):
                        continue

                    msg = email.message_from_bytes(raw_bytes)
                    
                    # --- 1. EXTRACT NATIVE EMAIL TIMESTAMP ---
                    date_header = msg.get('Date')
                    if date_header:
                        parsed_date_tuple = email.utils.parsedate_tz(date_header)
                        if parsed_date_tuple:
                            email_timestamp = datetime.datetime.fromtimestamp(email.utils.mktime_tz(parsed_date_tuple))
                        else:
                            email_timestamp = datetime.datetime.now()
                    else:
                        email_timestamp = datetime.datetime.now()

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

                    # Strip email threading and splitters
                    top_reply = body.strip()
                    reply_splitters = ["-----Original Message-----", "From:", "On ", "Am ", "Le ", "wrote:"]
                    for splitter in reply_splitters:
                        if splitter in top_reply:
                            top_reply = top_reply.split(splitter)[0]

                    lines = top_reply.splitlines()
                    clean_lines = []
                    for line in lines:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        if stripped.startswith('>'):
                            continue
                        clean_lines.append(stripped)

                    clean_reply_notes = "\n".join(clean_lines).strip()
                    
                    sender = str(msg.get('From', '')).lower()
                    sender_match = re.search(r'<([^>]+)>', sender)
                    sender_clean = sender_match.group(1).strip() if sender_match else sender.strip()

                   # Classify intent using the database prompt & Gemini
                    ai_decision = classify_reply_with_gemini(conn, sender_clean, decoded_subject, clean_reply_notes)
                    intent = ai_decision.get("classification")
                    voter_email = ai_decision.get("voter_email", sender_clean).strip().lower()
                    parsed_notes = ai_decision.get("summary_notes", clean_reply_notes)

                    payload_meta = {
                        "po_id": po_id, 
                        "sender": sender_clean, 
                        "voter": voter_email or sender_clean, 
                        "notes": parsed_notes,
                        "timestamp": email_timestamp  # Passed along for correct audit logging
                    }

                    # --- ROLE-BASED ROUTING & INTERCEPTION ---
                    with conn.cursor() as perm_cursor:
                        auth_check = verify_approver_authority(perm_cursor, payload_meta["voter"], 'Approval Authority')
                        finance_check = verify_approver_authority(perm_cursor, payload_meta["voter"], 'Finance Review')

                    if not auth_check["authorized"] and finance_check["authorized"]:
                        # 1. FINANCE REVIEWER: Force any approval/rejection intent into Finance Recommendations
                        if intent in ["APPROVE", "RECOMMEND_APPROVE"]:
                            recommendations_approval.append(payload_meta)
                            log(f"ℹ️ Reassigned approval from Finance Reviewer {payload_meta['voter']} to FINANCE RECOMMENDATION.")
                        elif intent in ["REJECT", "RECOMMEND_REJECT"]:
                            recommendations_rejection.append(payload_meta)
                            log(f"ℹ️ Reassigned rejection from Finance Reviewer {payload_meta['voter']} to FINANCE REJECTION.")
                        else:
                            write_control_log(conn, f"PO-ID-{po_id}", "Informational Reply", sender_clean, parsed_notes, email_timestamp)
                            log(f"ℹ️ Email from Finance Reviewer {sender_clean} classified as NEUTRAL.")

                    elif auth_check["authorized"]:
                        # 2. BOARD DIRECTOR / APPROVAL AUTHORITY: Route to final votes
                        if intent in ["APPROVE", "RECOMMEND_APPROVE"]:
                            final_approvals.append(payload_meta)
                        elif intent in ["REJECT", "RECOMMEND_REJECT"]:
                            final_rejections.append(payload_meta)
                        else:
                            write_control_log(conn, f"PO-ID-{po_id}", "Informational Reply", sender_clean, parsed_notes, email_timestamp)
                            log(f"ℹ️ Email from Director {payload_meta['voter']} classified as NEUTRAL.")
                    else:
                        # 3. UNAUTHORIZED SENDER
                        write_control_log(conn, f"PO-ID-{po_id}", "Unauthorized Action Blocked", sender_clean, "Sender lacks recognized approval or review authority.", email_timestamp)
                        log(f"⚠️ Unauthorized sender blocked: {sender_clean}", "WARNING")

                    mail.store(e_id, '+FLAGS', '\\Seen')

        mail.logout()
        return recommendations_approval, recommendations_rejection, final_approvals, final_rejections

    except Exception as e:
        log(f"IMAP Processing Error: {e}", "ERROR")
        return [], [], [], []

def verify_approver_authority(cursor, sender_email, required_permission):
    cursor.execute(
        """
        SELECT approval_permission 
        FROM approvers 
        WHERE (LOWER(email) = LOWER(%s) OR LOWER(temp_email) = LOWER(%s)) 
          AND active = 'YES';
        """,
        (sender_email, sender_email)
    )
    row = cursor.fetchone()
    if not row:
        return {"authorized": False, "is_chair": False, "permission": None}
    
    user_permission = row.get('approval_permission') or ''
    is_chair = 'Chair' in user_permission
    
    if required_permission == 'Approval Authority':
        authorized = ('Approval Authority' in user_permission or is_chair)
    elif required_permission == 'Finance Review':
        authorized = ('Finance Review' in user_permission or 'Approval Authority' in user_permission or is_chair)
    else:
        authorized = False
        
    return {"authorized": authorized, "is_chair": is_chair, "permission": user_permission}

def process_replies():
    log("Starting Inbox Reply Processor...")
    try:
        conn = get_db_connection()
        ensure_audit_log_table(conn)
        
        recommendations_approval, recommendations_rejection, final_approvals, final_rejections = fetch_approval_replies(conn)
        
        if not any([recommendations_approval, recommendations_rejection, final_approvals, final_rejections]):
            log("No actionable email replies detected. Processing complete.")
            conn.close()
            return

        all_po_ids = list(set(
            [item["po_id"] for item in recommendations_approval] + 
            [item["po_id"] for item in recommendations_rejection] + 
            [item["po_id"] for item in final_approvals] + 
            [item["po_id"] for item in final_rejections]
        ))
        log(f"Found replies for PO IDs: {all_po_ids}")

        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM po_log_with_status WHERE id = ANY(%s);",
                (all_po_ids,)
            )
            records = cursor.fetchall()
            po_id_map = {row['id']: row for row in records if row.get('id')}

            # 1. PROCESS FINANCE REVIEW RECOMMENDATIONS
            for meta in recommendations_approval:
                po_id = int(meta["po_id"])
                record = po_id_map.get(po_id)
                if record:
                    cursor.execute("""
                        UPDATE po_log 
                        SET submission_status = CASE 
                                WHEN submission_status = 'Finance Review' THEN 'Finance Recommended'
                                ELSE submission_status 
                            END
                        WHERE id = %s;
                    """, (record['id'],))
                    conn.commit()
                    notes = meta.get("notes") or "Finance Committee recommended for approval."
                    voter = meta.get("voter") or meta.get("sender")
                    email_ts = meta.get("timestamp")
                    
                    write_control_log(conn, record['po_number'], "Finance Review - Approval", voter, notes, email_ts)
                    log(f"✅ PO '{record['po_number']}' recorded as Finance Review - Approval by {voter}.")

            for meta in recommendations_rejection:
                po_id = int(meta["po_id"])
                record = po_id_map.get(po_id)
                if record:
                    cursor.execute("""
                        UPDATE po_log 
                        SET submission_status = CASE 
                                WHEN submission_status = 'Finance Review' THEN 'Finance Rejected'
                                ELSE submission_status 
                            END
                        WHERE id = %s;
                    """, (record['id'],))
                    conn.commit()
                    notes = meta.get("notes") or "Finance Committee recommended for rejection."
                    voter = meta.get("voter") or meta.get("sender")
                    email_ts = meta.get("timestamp")
                    
                    write_control_log(conn, record['po_number'], "Finance Review - Rejection", voter, notes, email_ts)
                    log(f"❌ PO '{record['po_number']}' recorded as Finance Review - Rejection by {voter}.")

            # 2. PROCESS FINAL APPROVALS
            for meta in final_approvals:
                po_id = int(meta["po_id"])
                record = po_id_map.get(po_id)
                if not record:
                    continue

                po_number = record['po_number']
                actual_voter = meta.get("voter") or meta.get("sender")
                forwarded_by = meta.get("sender")
                email_ts = meta.get("timestamp")

                auth_info = verify_approver_authority(cursor, actual_voter, 'Approval Authority')
                if not auth_info["authorized"]:
                    log(f"⚠️ Unauthorized action attempt by {actual_voter} for PO {po_number}", "WARNING")
                    write_control_log(conn, po_number, "Unauthorized Action Blocked", forwarded_by, f"Voter {actual_voter} lacks approval authority.", email_ts)
                    continue

                # Log the inbound approval first so it's included in vote tallying
                write_control_log(conn, po_number, "Inbound Approval", actual_voter, meta.get("notes") or "Approved via email reply.", email_ts)

                # Fetch all valid active votes for this PO to compute detailed breakdown
                cursor.execute("""
                    SELECT actor_email, action_type 
                    FROM workflow_control_log 
                    WHERE po_number = %s AND action_type IN ('Inbound Approval', 'Board Approval', 'Inbound Rejection');
                """, (po_number,))
                all_votes = cursor.fetchall()

                # Deduplicate to get each approver's latest action
                latest_votes = {}
                for vote in all_votes:
                    latest_votes[vote['actor_email'].lower()] = vote['action_type']

                approved_emails = [email for email, action in latest_votes.items() if action in ('Inbound Approval', 'Board Approval')]
                rejected_emails = [email for email, action in latest_votes.items() if action == 'Inbound Rejection']

                # Check chair approval requirements
                cursor.execute("""
                    SELECT count(*) as chair_count 
                    FROM approvers 
                    WHERE LOWER(email) = ANY(%s) 
                      AND approval_permission LIKE '%%Chair%%';
                """, (approved_emails,))
                chair_approved = (cursor.fetchone()['chair_count'] > 0)

                required_count = record.get('required_approval_count', 1)
                requires_chair = record.get('requires_chair_approval', False)
                current_approvals = len(approved_emails)
                current_rejections = len(rejected_emails)
                chair_status_text = "Approved" if chair_approved else "Pending"

                # Formulate detailed governance status banner string
                if current_rejections > 0 and current_approvals < required_count:
                    new_status = 'Rejected'
                    detailed_status = f"REJECTED (Approvals: {current_approvals}/{required_count}, Rejections: {current_rejections}; Chair: {chair_status_text})"
                elif current_approvals >= required_count and (not requires_chair or chair_approved):
                    new_status = 'Approved'
                    detailed_status = f"APPROVED (Approvals: {current_approvals}/{required_count}, Rejections: {current_rejections}; Chair: {chair_status_text})"
                else:
                    new_status = 'Partially Approved'
                    detailed_status = f"PENDING APPROVAL (Approvals: {current_approvals}/{required_count}, Rejections: {current_rejections}; Chair: {chair_status_text})"

                # Update PO status & calculated approval status string in database
                cursor.execute("""
                    UPDATE po_log 
                    SET submission_status = %s, calculated_approval_status = %s
                    WHERE id = %s;
                """, (new_status, detailed_status, record['id']))
                conn.commit()

                log(f"✅ PO '{po_number}' updated to {new_status.upper()} [{detailed_status}] (Vote logged by {actual_voter}).")

            # 3. PROCESS REJECTIONS
            for meta in final_rejections:
                po_id = int(meta["po_id"])
                record = po_id_map.get(po_id)
                if record:
                    po_number = record['po_number']
                    actual_voter = meta.get("voter") or meta.get("sender")
                    forwarded_by = meta.get("sender")
                    email_ts = meta.get("timestamp")

                    auth_info = verify_approver_authority(cursor, actual_voter, 'Approval Authority')
                    if not auth_info["authorized"]:
                        log(f"⚠️ Unauthorized rejection attempt by {actual_voter} for PO {po_number}", "WARNING")
                        write_control_log(conn, po_number, "Unauthorized Action Blocked", forwarded_by, f"Voter {actual_voter} lacks approval authority.", email_ts)
                        continue

                    # Log the inbound rejection
                    write_control_log(conn, po_number, "Inbound Rejection", actual_voter, meta.get("notes") or "Marked as rejected via email reply.", email_ts)

                    # Re-tally votes
                    cursor.execute("""
                        SELECT actor_email, action_type 
                        FROM workflow_control_log 
                        WHERE po_number = %s AND action_type IN ('Inbound Approval', 'Board Approval', 'Inbound Rejection');
                    """, (po_number,))
                    all_votes = cursor.fetchall()

                    latest_votes = {}
                    for vote in all_votes:
                        latest_votes[vote['actor_email'].lower()] = vote['action_type']

                    approved_emails = [email for email, action in latest_votes.items() if action in ('Inbound Approval', 'Board Approval')]
                    rejected_emails = [email for email, action in latest_votes.items() if action == 'Inbound Rejection']

                    cursor.execute("""
                        SELECT count(*) as chair_count 
                        FROM approvers 
                        WHERE LOWER(email) = ANY(%s) 
                          AND approval_permission LIKE '%%Chair%%';
                    """, (approved_emails,))
                    chair_approved = (cursor.fetchone()['chair_count'] > 0)

                    required_count = record.get('required_approval_count', 1)
                    requires_chair = record.get('requires_chair_approval', False)
                    current_approvals = len(approved_emails)
                    current_rejections = len(rejected_emails)
                    chair_status_text = "Approved" if chair_approved else "Pending"

                    detailed_status = f"REJECTED (Approvals: {current_approvals}/{required_count}, Rejections: {current_rejections}; Chair: {chair_status_text})"

                    cursor.execute("""
                        UPDATE po_log 
                        SET submission_status = 'Rejected', calculated_approval_status = %s
                        WHERE id = %s;
                    """, (detailed_status, record['id']))
                    conn.commit()
                    
                    log(f"❌ PO ID '{record['id']}' updated to REJECTED [{detailed_status}] (Vote logged by {actual_voter}).")

        conn.close()
    except Exception as db_err:
        log(f"Database sync error: {db_err}", "CRITICAL")

if __name__ == "__main__":
    process_replies()
