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
    financial_data = []
    
    containers = soup.find_all(['tr', 'div', 'p'])
    gl_pattern = re.compile(r'(\d{3,4}/\d{3})')
    
    # Improved regex to capture digits with optional spaces, commas, decimals, or negative brackets
    currency_pattern = re.compile(r'\(?\d{1,3}(?:[,\s]?\d{3})*(?:\.\d+)?\)?')

    for element in containers:
        text_line = element.text.strip()
        if not text_line:
            continue
            
        gl_match = gl_pattern.search(text_line)
        if gl_match:
            gl_code = gl_match.group(1)
            remainder = text_line.replace(gl_code, '', 1).strip()
            
            all_numbers = currency_pattern.findall(remainder)
            
            # Keep all valid captured numbers (removed the strict '.' requirement that dropped whole numbers)
            clean_metrics = [num.strip() for num in all_numbers if num.strip() != '']
            
            description = remainder
            for num in clean_metrics:
                description = description.replace(num, '', 1)
            description = re.sub(r'\s+', ' ', description).strip()
            
            if not description:
                description = "Operational Portfolio Allocation"

            # Safely parse mapping the last 4 fixed summary columns from the back
            if len(clean_metrics) >= 4:
                total_budget = clean_and_convert_number(clean_metrics[-1])
                variance = clean_and_convert_number(clean_metrics[-2])
                budget_ytd = clean_and_convert_number(clean_metrics[-3])
                ytd = clean_and_convert_number(clean_metrics[-4])
                
                # Months are everything before the last 4 metrics
                months = clean_metrics[:-4]
                mar_2026 = clean_and_convert_number(months[0]) if len(months) > 0 else 0.0
                apr_2026 = clean_and_convert_number(months[1]) if len(months) > 1 else 0.0
                may_2026 = clean_and_convert_number(months[2]) if len(months) > 2 else 0.0
                jun_2026 = clean_and_convert_number(months[3]) if len(months) > 3 else 0.0
                jul_2026 = clean_and_convert_number(months[4]) if len(months) > 4 else 0.0

                financial_data.append({
                    "gl_code": gl_code,
                    "description": description,
                    "mar_2026": mar_2026,
                    "apr_2026": apr_2026,
                    "may_2026": may_2026,
                    "jun_2026": jun_2026,
                    "jul_2026": jul_2026,
                    "ytd": ytd,
                    "budget_ytd": budget_ytd,
                    "variance": variance,
                    "total_budget": total_budget
                })

    df = pd.DataFrame(financial_data)
    if not df.empty:
        df = df.drop_duplicates(subset=["gl_code"], keep="first")
    else:
        print("⚠️ Warning: Standard parsing found 0 rows.")

    print("Connecting to Neon PostgreSQL for Budget Synchronization...")
    upsert_query = """
        INSERT INTO master_budget (
            gl_code, description, mar_2026, apr_2026, may_2026, 
            jun_2026, jul_2026, ytd, budget_ytd, variance, total_budget
        )
        VALUES (
            %(gl_code)s, %(description)s, %(mar_2026)s, %(apr_2026)s, %(may_2026)s, 
            %(jun_2026)s, %(jul_2026)s, %(ytd)s, %(budget_ytd)s, %(variance)s, %(total_budget)s
        )
        ON CONFLICT (gl_code) DO UPDATE SET
            description = EXCLUDED.description,
            mar_2026 = EXCLUDED.mar_2026,
            apr_2026 = EXCLUDED.apr_2026,
            may_2026 = EXCLUDED.may_2026,
            jun_2026 = EXCLUDED.jun_2026,
            jul_2026 = EXCLUDED.jul_2026,
            ytd = EXCLUDED.ytd,
            budget_ytd = EXCLUDED.budget_ytd,
            variance = EXCLUDED.variance,
            total_budget = EXCLUDED.total_budget;
    """

    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            print(f"Syncing {len(df)} financial records with PostgreSQL...")
            for _, row in df.iterrows():
                cur.execute(upsert_query, row.to_dict())
            conn.commit()
        conn.close()
        print("🟢 Success! Parsed and pushed to Neon PostgreSQL master_budget.")
        
    except Exception as e:
        print(f"🔴 Database sync failed. Technical Error: {e}")

if __name__ == "__main__":
    fetch_and_sync_weconnectu_budget()