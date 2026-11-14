import os
import re
import imaplib
import email
import psycopg2
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

def extract_report_metadata(tracking_url):
    """Follows the tracking link, resolves the target WeConnectU URL, and extracts the report month."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(tracking_url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_selector("text=/\\d{4}-\\d{2}-\\d{2}\\s+to\\s+\\d{4}-\\d{2}-\\d{2}/", timeout=20000)
        resolved_url = page.url
        body_text = page.inner_text("body")
        browser.close()

    match = re.search(r'(\d{4}-\d{2})-\d{2}\s+to\s+\d{4}-\d{2}-\d{2}', body_text)
    if not match:
        raise ValueError("Could not locate 'YYYY-MM-DD to YYYY-MM-DD' period header on report.")
    
    report_month = f"{match.group(1)}-01"  # Converts e.g. '2026-08' to '2026-08-01'
    return resolved_url, report_month

def fetch_and_store_weconnectu_url(report_type="Community Report - Cathkin Estate"):
    """
    Scans Gmail for all Community Report emails, extracts their resolved URLs and 
    reporting months, and upserts them into Neon PostgreSQL.
    """
    gmail_user = os.getenv("SENDER_EMAIL")
    gmail_pass = os.getenv("SENDER_PASSWORD")
    database_url = os.getenv("DATABASE_URL")

    if not gmail_user or not gmail_pass:
        raise ValueError("SENDER_EMAIL or SENDER_PASSWORD environment variable is missing.")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is missing.")

    print(f"Connecting to Gmail inbox as {gmail_user}...")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(gmail_user, gmail_pass)
    mail.select("inbox")

    search_query = f'(SUBJECT "{report_type}")'
    status, messages = mail.search(None, search_query)
    if status != "OK" or not messages[0]:
        mail.logout()
        raise RuntimeError(f"No emails found with subject matching '{report_type}' in Inbox.")

    conn = psycopg2.connect(database_url)
    upsert_query = """
        INSERT INTO public.weconnectu_report_logs (report_type, report_month, report_url, fetched_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (report_month) DO UPDATE SET
            report_url = EXCLUDED.report_url,
            fetched_at = CURRENT_TIMESTAMP;
    """

    email_ids = messages[0].split()
    latest_resolved_url = None

    for e_id in email_ids:
        _, msg_data = mail.fetch(e_id, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])

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
            continue

        soup = BeautifulSoup(html_body, "html.parser")
        tracking_url = None
        for a_tag in soup.find_all("a", href=True):
            if "community report online" in a_tag.get_text(strip=True).lower():
                tracking_url = a_tag["href"]
                break

        if not tracking_url:
            continue

        try:
            resolved_url, report_month = extract_report_metadata(tracking_url)
            with conn.cursor() as cur:
                cur.execute(upsert_query, (report_type, report_month, resolved_url))
            conn.commit()
            print(f"✅ Logged report for month {report_month}")
            latest_resolved_url = resolved_url
        except Exception as e:
            print(f"⚠️ Warning: Failed to process email ID {e_id}: {e}")

    conn.close()
    mail.logout()
    return latest_resolved_url

def get_latest_weconnectu_url(database_url=None):
    """Queries Neon PostgreSQL for the URL belonging to the most recent report_month."""
    db_url = database_url or os.getenv("DATABASE_URL")
    conn = psycopg2.connect(db_url)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT report_url, report_month
            FROM public.weconnectu_report_logs
            WHERE report_month IS NOT NULL
            ORDER BY report_month DESC, fetched_at DESC
            LIMIT 1;
        """)
        row = cur.fetchone()
    conn.close()

    if not row:
        raise RuntimeError("No logged WeConnectU report URLs found in weconnectu_report_logs.")
    
    print(f"📅 Using latest report for period: {row[1]}")
    return row[0]

if __name__ == "__main__":
    print("Running WeConnectU report logger & date extractor...")
    try:
        url = fetch_and_store_weconnectu_url()
        print(f"Successfully processed reports. Latest URL: {url}")
    except Exception as e:
        print(f"Error: {e}")
