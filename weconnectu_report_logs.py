import os
import imaplib
import email
from bs4 import BeautifulSoup
import psycopg2

def fetch_and_store_weconnectu_url(report_type="Community Report - Cathkin Estate"):
    """
    Connects to Gmail via IMAP, extracts the latest report URL, 
    saves it to the PostgreSQL database for record-keeping, and returns the URL.
    """
    gmail_user = os.getenv("GMAIL_USER")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD")
    database_url = os.getenv("DATABASE_URL")
    
    if not gmail_user or not gmail_pass:
        raise ValueError("GMAIL_USER or GMAIL_APP_PASSWORD environment variable is missing.")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is missing.")

    print("Connecting to Gmail to fetch the latest WeConnectU report link...")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(gmail_user, gmail_pass)
    mail.select("inbox")

    # Search for emails with the matching subject
    search_query = f'(SUBJECT "{report_type}")'
    status, messages = mail.search(None, search_query)
    if status != "OK" or not messages[0]:
        mail.logout()
        raise RuntimeError(f"No emails found with subject matching '{report_type}' in Inbox.")

    # Get the most recent email ID
    latest_email_id = messages[0].split()[-1]
    _, msg_data = mail.fetch(latest_email_id, "(RFC822)")
    
    raw_email = msg_data[0][1]
    msg = email.message_from_bytes(raw_email)
    mail.logout()

    # Extract HTML body from the email
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html_body = part.get_payload(decode=True).decode(errors="ignore")
                break
    else:
        if msg.get_content_type() == "text/html":
            html_body = msg.get_payload(decode=True).decode(errors="ignore")

    if not html_body:
        raise RuntimeError("Could not find HTML content in the latest report email.")

    # Parse the email HTML to find the 'Click here to view the Community Report online' link
    soup = BeautifulSoup(html_body, "html.parser")
    report_url = None
    for a_tag in soup.find_all("a", href=True):
        link_text = a_tag.get_text(strip=True).lower()
        if "community report online" in link_text:
            report_url = a_tag["href"]
            break

    if not report_url:
        raise RuntimeError("Found the email, but could not locate the 'Community Report online' link.")

    print(f"✅ [LOG] Extracted WeConnectU URL: {report_url}")

    # Save/Log the URL into Neon PostgreSQL
    upsert_query = """
        INSERT INTO public.weconnectu_report_logs (report_type, report_url, fetched_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (report_url) 
        DO UPDATE SET fetched_at = CURRENT_TIMESTAMP;
    """
    
    try:
        conn = psycopg2.connect(database_url)
        with conn.cursor() as cur:
            cur.execute(upsert_query, (report_type, report_url))
            conn.commit()
        conn.close()
        print("🟢 Success! Report URL logged to PostgreSQL database.")
    except Exception as e:
        print(f"⚠️ Warning: Failed to record URL to database: {e}")

    return report_url
