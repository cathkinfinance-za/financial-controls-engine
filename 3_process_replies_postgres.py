import os
import re
import imaplib
import email
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

                    # Updated log to show both the voter and the email sender if they differ
                    if voter_email != sender_clean:
                        log(f"Gemini Intent for voter {voter_email} (via {sender_clean}): {intent} -> {parsed_notes}")
                    else:
                        log(f"Gemini Intent for {sender_clean}: {intent} -> {parsed_notes}")

                    payload_meta = {
                        "po_id": po_id, 
                        "sender": sender_clean, 
                        "voter": voter_email, 
                        "notes": parsed_notes
                    }
                    
                    if intent == "RECOMMEND_REJECT":
                        recommendations_rejection.append(payload_meta)
                    elif intent == "RECOMMEND_APPROVE":
                        recommendations_approval.append(payload_meta)
                    elif intent == "REJECT":
                        final_rejections.append(payload_meta)
                    elif intent == "APPROVE":
                        final_approvals.append(payload_meta)
                    else:
                        write_control_log(conn, f"PO-ID-{po_id}", "Informational Reply", sender_clean, parsed_notes)
                        log(f"ℹ️ Email from {sender_clean} classified as NEUTRAL. Ignored as vote.")

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
                    write_control_log(conn, record['po_number'], "Finance Review", voter, notes)
                    log(f"✅ PO '{record['po_number']}' recorded as FINANCE REVIEW recommendation by {voter}.")

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
                    write_control_log(conn, record['po_number'], "Finance Review", voter, notes)
                    log(f"❌ PO '{record['po_number']}' recorded as FINANCE REVIEW rejection by {voter}.")

            # 2. PROCESS FINAL APPROVALS
            for meta in final_approvals:
                po_id = int(meta["po_id"])
                record = po_id_map.get(po_id)
                if not record:
                    continue

                po_number = record['po_number']
                
                # (We removed the 'if record.get('submission_status') == 'Rejected': continue' block here)
                
                actual_voter = meta.get("voter") or meta.get("sender")
                forwarded_by = meta.get("sender")

                auth_info = verify_approver_authority(cursor, actual_voter, 'Approval Authority')
                if not auth_info["authorized"]:
                    log(f"⚠️ Unauthorized action attempt by {actual_voter} for PO {po_number}", "WARNING")
                    write_control_log(conn, po_number, "Unauthorized Action Blocked", forwarded_by, f"Voter {actual_voter} lacks approval authority.")
                    continue

                # Fetch all valid approval logs for this PO to count active quorum
                cursor.execute("""
                    SELECT actor_email 
                    FROM workflow_control_log 
                    WHERE po_number = %s AND action_type IN ('Inbound Approval', 'Board Approval');
                """, (po_number,))
                prior_logs = cursor.fetchall()

                approved_emails = set(row['actor_email'].lower() for row in prior_logs if row.get('actor_email'))
                approved_emails.add(actual_voter.lower())

                # Check chair approval requirements
                cursor.execute("""
                    SELECT count(*) as chair_count 
                    FROM approvers 
                    WHERE LOWER(email) = ANY(%s) 
                      AND approval_permission LIKE '%%Chair%%';
                """, (list(approved_emails),))
                chair_approved = (cursor.fetchone()['chair_count'] > 0)

                required_count = record.get('required_approval_count', 1)
                requires_chair = record.get('requires_chair_approval', False)
                current_count = len(approved_emails)

                # Dynamically calculate new status based on current valid approvals
                if current_count >= required_count and (not requires_chair or chair_approved):
                    new_status = 'Approved'
                    log_action = 'Inbound Approval'
                    notes = meta.get("notes") or f"Fully approved ({current_count}/{required_count} sign-offs received)."
                else:
                    new_status = 'Partially Approved'
                    log_action = 'Inbound Partial Approval'
                    notes = meta.get("notes") or f"Partially approved ({current_count}/{required_count})."

                # Update status back to Approved or Partially Approved, overwriting previous rejections if quorum is met
                cursor.execute("""
                    UPDATE po_log 
                    SET submission_status = %s
                    WHERE id = %s;
                """, (new_status, record['id']))
                conn.commit()

                write_control_log(conn, po_number, log_action, actual_voter, notes)
                log(f"✅ PO '{po_number}' successfully updated to {new_status.upper()} by {actual_voter}.")

           # 3. PROCESS REJECTIONS
            for meta in final_rejections:
                po_id = int(meta["po_id"])
                record = po_id_map.get(po_id)
                if record:
                    actual_voter = meta.get("voter") or meta.get("sender")
                    
                    # Set status to Rejected
                    cursor.execute("""
                        UPDATE po_log 
                        SET submission_status = 'Rejected'
                        WHERE id = %s;
                    """, (record['id'],))
                    conn.commit()
                    
                    notes = meta.get("notes") or "Marked as rejected via email reply."
                    write_control_log(conn, record['po_number'], "Inbound Rejection", actual_voter, notes)
                    log(f"❌ PO ID '{record['id']}' updated to REJECTED by {actual_voter}.")

        conn.close()
    except Exception as db_err:
        log(f"Database sync error: {db_err}", "CRITICAL")

if __name__ == "__main__":
    process_replies()
