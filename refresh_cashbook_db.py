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

    if "#payment-cashbook" not in URL:
        URL += "#payment-cashbook"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        print("Connecting to WeconnectU report server...")
        page.goto(URL, timeout=60000, wait_until="networkidle")
        
        try:
            page.wait_for_selector("text=Cashbook Allocations", timeout=15000)
        except Exception:
            pass

        html_content = page.content()
        browser.close()

    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Search for heading or container
    heading = soup.find(lambda tag: tag.name in ['h1', 'h2', 'h3', 'h4', 'div', 'span'] and 'Cashbook Allocations' in tag.text)
    table = heading.find_next('table') if heading else None
    
    if not table:
        for tbl in soup.find_all('table'):
            if "Allocation" in tbl.text and "Transaction" in tbl.text:
                table = tbl
                break

    if not table:
        print("❌ Error: Could not locate Cashbook Allocations table.")
        return

    rows = table.find_all('tr')
    allocations_data = []

    # Patterns for GL codes (e.g. 1000/010, 9900/001) or Supplier/Debtor codes
    gl_pattern = re.compile(r'^(\d{3,4}/\d{3})')
    supplier_pattern = re.compile(r'^(?:SUPPLIER|DEBTOR)\s+([A-Z0-9]+):', re.IGNORECASE)

    for tr in rows:
        cells = [td.text.strip() for td in tr.find_all(['td', 'th'])]
        
        if len(cells) < 3:
            continue
            
        trans_date = parse_date(cells[0])
        if not trans_date:
            continue  # Skip header or opening balance rows without valid dates

        transaction_text = cells[1]
        amount = clean_amount(cells[2])
        allocation_raw = cells[3] if len(cells) > 3 else ""

        # Ignore non-transaction opening balance rows
        if transaction_text.lower() == "opening balance":
            continue

        # Determine transaction type
        transaction_type = "INCOME" if amount >= 0 else "EXPENSE"

        # Extract GL or Supplier/Debtor identifiers
        gl_match = gl_pattern.search(allocation_raw)
        supplier_match = supplier_pattern.search(allocation_raw)

        gl_code = gl_match.group(1) if gl_match else None
        supplier_code = supplier_match.group(1) if supplier_match else None

        allocations_data.append({
            "transaction_date": trans_date,
            "transaction_text": transaction_text,
            "amount": amount,
            "transaction_type": transaction_type,
            "allocation_raw": allocation_raw,
            "gl_code": gl_code,
            "supplier_code": supplier_code
        })

    df = pd.DataFrame(allocations_data)
    if df.empty:
        print("⚠️ Warning: 0 cashbook rows parsed.")
        return

    print(f"Parsed {len(df)} cashbook allocation entries ({len(df[df['transaction_type'] == 'INCOME'])} Income, {len(df[df['transaction_type'] == 'EXPENSE'])} Expense).")

    # Upsert query
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