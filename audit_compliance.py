import os
import sys
import psycopg2
import pandas as pd

def run_compliance_audit():
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        print("❌ DATABASE_URL missing.")
        return

    try:
        conn = psycopg2.connect(DATABASE_URL)
        query = """
            SELECT 
                transaction_date, 
                transaction_text, 
                amount, 
                authority_tier, 
                compliance_verdict 
            FROM vw_cashbook_compliance_audit 
            WHERE compliance_verdict != 'COMPLIANT'
            ORDER BY transaction_date DESC;
        """
        
        # Execute query directly to bypass pandas SQL connection deprecation warnings
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
            colnames = [desc[0] for desc in cur.description]
            
        conn.close()
        
        df = pd.DataFrame(rows, columns=colnames)

        print(f"\n================ FINANCIAL COMPLIANCE AUDIT REPORT ================")
        print(f"Total Non-Compliant / Flagged Transactions: {len(df)}\n")

        if not df.empty:
            print(df.to_string(index=False))
            # Optional: Exit with code 1 so GitHub Actions / CI pipelines catch non-compliance
            # sys.exit(1)
        else:
            print("🟢 All expenses comply with Section 2 & Section 3 parameters!")

    except Exception as e:
        print(f"🔴 Audit execution failed: {e}")

if __name__ == "__main__":
    run_compliance_audit()