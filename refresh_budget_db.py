import os
import re
import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import psycopg2

def clean_and_convert_number(text_value):
    """Converts clean accounting string metrics into valid calculation floats."""
    if not text_value or str(text_value).strip() == "" or str(text_value).strip() == "-":
        return 0.0
    cleaned = re.sub(r'[\s\xa0R\(\)]', '', str(text_value))
    if '(' in str(text_value) and ')' in str(text_value):
        cleaned = "-" + re.sub(r'[\(\)]', '', cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def header_to_column(header_text):
    """Maps header text like 'Jun 2026' to database column names like 'jun_2026'."""
    text = header_text.strip().lower()
    match = re.search(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s*(\d{4})', text)
    if match:
        return f"{match.group(1)}_{match.group(2)}"
    if text == 'ytd':
        return 'ytd'
    if 'budget ytd' in text:
        return 'budget_ytd'
    if text == 'variance':
        return 'variance'
    if 'total budget' in text:
        return 'total_budget'
    return None

def fetch_and_sync_weconnectu_budget():
    URL = os.getenv("WECONNECTU_URL")
    DATABASE_URL = os.getenv("DATABASE_URL")

    if not URL or not DATABASE_URL:
        print("❌ ERROR: WECONNECTU_URL or DATABASE_URL environment variable is missing.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        print("Connecting to WeconnectU report server...")
        page.goto(URL, timeout=60000, wait_until="networkidle")
        
        print("Waiting 10 seconds for layout matrix grids to finalize calculations...")
        page.wait_for_selector("text=/\\d{3,4}\\/\\d{3}/", timeout=15000)
        
        html_content = page.content()
        browser.close()
        
    print("Parsing dynamic data containers and extracting currency arrays...")
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Dynamically extract ordered metric column names from report headers
    metric_columns = []
    header_rows = soup.find_all(['tr', 'thead'])
    for elem in header_rows:
        texts = [td.text.strip() for td in elem.find_all(['th', 'td'])]
        potential_cols = [header_to_column(t) for t in texts if header_to_column(t)]
        if len(potential_cols) >= 3:
            metric_columns = potential_cols
            break

    if not metric_columns:
        ths = soup.find_all('th')
        metric_columns = [header_to_column(th.text) for th in ths if header_to_column(th.text)]

    print(f"🔍 [LOG] Detected metric columns from report headers: {metric_columns}")

    financial_data = []
    gl_pattern = re.compile(r'(\d{3,4}/\d{3})')

    for tr in soup.find_all('tr'):
        cells = [td.text.strip() for td in tr.find_all(['td', 'th'])]
        if not cells:
            continue
        
        full_row_text = " ".join(cells)
        gl_match = gl_pattern.search(full_row_text)
        
        if gl_match:
            gl_code = gl_match.group(1)
            
            description = "Operational Portfolio Allocation"
            for cell in cells:
                if gl_code in cell:
                    extracted_desc = cell.replace(gl_code, '').strip()
                    if extracted_desc:
                        description = extracted_desc
                    break

            clean_metrics = []
            for cell in cells:
                cleaned_cell = re.sub(r'[\s\xa0R]', '', cell)
                if re.match(r'^\(?\-?\d+(?:[\.,]\d+)?\)?$', cleaned_cell):
                    clean_metrics.append(cell)
            
            vals = [clean_and_convert_number(m) for m in clean_metrics]

            row_data = {
                "gl_code": gl_code,
                "description": description
            }

            for i, col_name in enumerate(metric_columns):
                if i < len(vals):
                    row_data[col_name] = vals[i]

            financial_data.append(row_data)

    df = pd.DataFrame(financial_data)
    if not df.empty:
        df = df.drop_duplicates(subset=["gl_code"], keep="first")
    else:
        print("⚠️ Warning: Standard parsing found 0 rows.")
        return

    detected_db_columns = set(metric_columns)

    print("Connecting to Neon PostgreSQL for Budget Synchronization...")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        
        # 1. Fetch current columns directly from the database schema
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'master_budget';
            """)
            existing_columns = {row[0] for row in cur.fetchall()}
            print(f"📋 [LOG] Current DB columns before alteration: {sorted(list(existing_columns))}")

        # 2. Add missing columns with explicit autocommit & transaction commit
        for col in detected_db_columns:
            if col and col not in existing_columns and col not in ['ytd', 'budget_ytd', 'variance', 'total_budget']:
                print(f"⚙️ [LOG] Executing DDL: ALTER TABLE master_budget ADD COLUMN {col}...")
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(f"ALTER TABLE master_budget ADD COLUMN IF NOT EXISTS {col} NUMERIC(12, 2) DEFAULT '0.00';")
                conn.autocommit = False
                print(f"✅ [LOG] Column {col} added successfully.")

        # 3. Perform Upsert
        with conn.cursor() as cur:
            update_cols = [c for c in detected_db_columns if c]
            base_cols = ['gl_code', 'description'] + update_cols
            
            placeholders = ", ".join([f"%({col})s" for col in base_cols])
            columns_str = ", ".join(base_cols)
            
            update_set_clauses = ["description = EXCLUDED.description"]
            for col in update_cols:
                update_set_clauses.append(f"{col} = EXCLUDED.{col}")
            
            update_str = ", ".join(update_set_clauses)
            
            upsert_query = f"""
                INSERT INTO master_budget ({columns_str})
                VALUES ({placeholders})
                ON CONFLICT (gl_code) DO UPDATE SET
                    {update_str};
            """

            print(f"📝 [LOG] Generated UPSERT Query Structure:\n{upsert_query.strip()}\n")
            print(f"Syncing {len(df)} financial records with PostgreSQL...")
            
            for _, row in df.iterrows():
                row_dict = row.to_dict()
                for col in base_cols:
                    if col not in row_dict or pd.isna(row_dict[col]):
                        row_dict[col] = 0.0 if col not in ['gl_code', 'description'] else ""
                cur.execute(upsert_query, row_dict)
            conn.commit()

        # 4. Final verification of database schema
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'master_budget';
            """)
            final_columns = [row[0] for row in cur.fetchall()]
            print(f"📊 [LOG] Final DB columns after script run: {final_columns}")
            
        conn.close()
        print("🟢 Success! Parsed and pushed to Neon PostgreSQL master_budget.")
        
    except Exception as e:
        print(f"🔴 Database sync failed. Technical Error: {e}")

if __name__ == "__main__":
    fetch_and_sync_weconnectu_budget()
