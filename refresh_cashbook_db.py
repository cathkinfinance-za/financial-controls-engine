import os
import re
import psycopg2
import pandas as pd
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

def clean_amount(text_value):
    """Converts accounting formatted text into floats."""
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
        
        print("Waiting for #payment-cashbook container...")
        page.wait_for_selector("#payment-cashbook", timeout=30000)

        # Scroll into view so all pay-info divs render
        page.eval_on_selector("#payment-cashbook", "el => el.scrollIntoView()")
        page.wait_for_timeout(3000)

        html_content = page.content()
        browser.close()

    soup = BeautifulSoup(html_content, 'html.parser')
    
    cashbook_container = soup.find(id="payment-cashbook")
    if not cashbook_container:
        print("❌ Error: #payment-cashbook element not found in DOM.")
        return

    # Target parent section to ensure we catch box-info wrapper
    parent_section = cashbook_container.parent if cashbook_container.parent else cashbook_container

    allocations_data = []
    gl_pattern = re.compile(r'(\d{3,4}/\d{3})')
    supplier_pattern = re.compile(r'^(?:SUPPLIER|DEBTOR)\s+([A-Z0-9]+):', re.IGNORECASE)

    # Search for all div elements with class "pay-info"
    rows = parent_section.find_all('div', class_='pay-info')

    for row in rows:
        # Extract immediate child divs (Date, Transaction, Amount, Allocation)
        cols = [div.text.strip() for div in row.find_all('div', recursive=False)]
        
        if len(cols) < 3:
            continue

        trans_date = parse_date(cols[0])
        if not trans_date:
            continue  # Filters out the header row "Date | Transaction | Amount | Allocation"

        transaction_text = cols[1]
        amount = clean_amount(cols[2])
        allocation_raw = cols[3] if len(cols) > 3 else ""

        # Skip non-transaction opening balances
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
        print("⚠️ Warning: 0 cashbook rows parsed from div.pay-info.")
        return

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
        print("🟢 Success! Cashbook allocations synced to PostgreSQL.")
    except Exception as e:
        print(f"🔴 Database sync failed: {e}")

if __name__ == "__main__":
    fetch_and_sync_cashbook_allocations()
