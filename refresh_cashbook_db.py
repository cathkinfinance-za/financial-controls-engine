import os
import re
import psycopg2
import pandas as pd
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

def clean_amount(text_value):
    """Converts string metrics to float, handling negative accounting brackets."""
    if not text_value or str(text_value).strip() in ["", "-"]:
        return 0.0
    cleaned = re.sub(r'[\s\xa0R]', '', str(text_value))
    if '(' in cleaned and ')' in cleaned:
        cleaned = "-" + re.sub(r'[\(\)]', '', cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def parse_date(date_text):
    """Parses date string YYYY-MM-DD."""
    try:
        return datetime.strptime(date_text.strip(), '%Y-%m-%d').date()
    except ValueError:
        return None

def fetch_and_sync_cashbook_allocations():
    URL = os.getenv("WECONNECTU_URL")
    DATABASE_URL = os.getenv("DATABASE_URL")

    if not URL or not DATABASE_URL:
        print("❌ ERROR: WECONNECTU_URL or DATABASE_URL environment variable is missing.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        print("Connecting to WeconnectU report server...")
        page.goto(URL, timeout=90000, wait_until="networkidle")
        
        print("Navigating and scrolling to Cashbook Allocations section...")
        # Scroll down incrementally to trigger any lazy-loaded tables
        for _ in range(5):
            page.evaluate("window.scrollBy(0, 1000)")
            page.wait_for_timeout(1000)

        # Attempt to click cashbook tab if it exists
        try:
            cashbook_tab = page.locator("text=Cashbook Allocations").first
            if cashbook_tab.is_visible():
                cashbook_tab.scroll_into_view_if_needed()
                cashbook_tab.click()
                page.wait_for_timeout(3000)
        except Exception:
            pass

        # Give dynamic JavaScript time to fill tables
        page.wait_for_timeout(5000)

        html_content = page.content()
        browser.close()

    soup = BeautifulSoup(html_content, 'html.parser')
    
    allocations_data = []
    gl_pattern = re.compile(r'(\d{3,4}/\d{3})')
    supplier_pattern = re.compile(r'^(?:SUPPLIER|DEBTOR)\s+([A-Z0-9]+):', re.IGNORECASE)

    # Strategy 1: Search standard HTML <tr> tags
    for tr in soup.find_all('tr'):
        cells = [td.text.strip() for td in tr.find_all(['td', 'th'])]
        if len(cells) < 3:
            continue
            
        trans_date = parse_date(cells[0])
        if not trans_date:
            continue  # Must start with valid YYYY-MM-DD date

        transaction_text = cells[1]
        amount = clean_amount(cells[2])
        allocation_raw = cells[3] if len(cells) > 3 else ""

        if transaction_text.lower() == "opening balance":
            continue

        transaction_type = "INCOME" if amount >= 0 else "EXPENSE"
        gl_match = gl_pattern.search(allocation_raw)
        supplier_match = supplier_pattern.search(allocation_raw)

        allocations_data.append({
            "transaction_date": trans_date,
            "transaction_text": transaction_text,
            "amount": amount,
            "transaction_type": transaction_type,
            "allocation_raw": allocation_raw,
            "gl_code": gl_match.group(1) if gl_match else None,
            "supplier_code": supplier_match.group(1) if supplier_match else None
        })

    # Strategy 2: Flexbox/Grid fallback if 0 standard <tr> rows were matched
    if not allocations_data:
        print("⚠️ Standard table rows not found. Running div grid fallbacks...")
        # Search for date pattern YYYY-MM-DD in text elements
        date_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}$')
        
        for elem in soup.find_all(text=date_pattern):
            parent = elem.find_parent(['div', 'li', 'tr'])
            if parent:
                texts = [t.strip() for t in parent.stripped_strings if t.strip()]
                if len(texts) >= 3:
                    trans_date = parse_date(texts[0])
                    if trans_date:
                        transaction_text = texts[1]
                        amount = clean_amount(texts[2])
                        allocation_raw = texts[3] if len(texts) > 3 else ""
                        
                        if transaction_text.lower() == "opening balance":
                            continue

                        transaction_type = "INCOME" if amount >= 0 else "EXPENSE"
                        gl_match = gl_pattern.search(allocation_raw)
                        supplier_match = supplier_pattern.search(allocation_raw)

                        allocations_data.append({
                            "transaction_date": trans_date,
                            "transaction_text": transaction_text,
                            "amount": amount,
                            "transaction_type": transaction_type,
                            "allocation_raw": allocation_raw,
                            "gl_code": gl_match.group(1) if gl_match else None,
                            "supplier_code": supplier_match.group(1) if supplier_match else None
                        })

    df = pd.DataFrame(allocations_data)
    if df.empty:
        print("⚠️ Warning: 0 cashbook rows parsed. Please verify report target page load.")
        return

    # Deduplicate extracted entries
    df = df.drop_duplicates(subset=["transaction_date", "transaction_text", "amount"])
    print(f"Parsed {len(df)} cashbook allocation entries ({len(df[df['transaction_type'] == 'INCOME'])} Income, {len(df[df['transaction_type'] == 'EXPENSE'])} Expense).")

    upsert_query = """
        INSERT INTO public.cashbook_allocations (
            transaction_date, transaction_text, amount, transaction_type, allocation_raw, gl_code, supplier_code
        )
        VALUES (
            %(transaction_date)s, %(transaction_text)s, %(amount)s, %(transaction_type)s, %(allocation_raw)s, %(gl_code)s, %(supplier_code)s
        )
        ON CONFLICT (transaction_date, transaction_text, amount) DO UPDATE SET
            transaction_type = EXCLUDED.transaction_type,
            allocation_raw = EXCLUDED.allocation_raw,
            gl_code = EXCLUDED.gl_code,
            supplier_code = EXCLUDED.supplier_code;
    """

    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            for _, row in df.iterrows():
                cur.execute(upsert_query, row.to_dict())
            conn.commit()
        conn.close()
        print("🟢 Success! Income & Expense cashbook allocations synced to PostgreSQL.")
    except Exception as e:
        print(f"🔴 Database sync failed: {e}")

if __name__ == "__main__":
    fetch_and_sync_cashbook_allocations()