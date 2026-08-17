import os
import re
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor

# ---------------------------------------------------------
# DATABASE CONFIGURATION
# ---------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

def get_connection():
    return psycopg2.connect(DATABASE_URL)

# ---------------------------------------------------------
# HELPER DATA CLEANING FUNCTIONS
# ---------------------------------------------------------
def clean_numeric(val):
    """Parses currency strings, commas, or empty values to float."""
    if pd.isna(val) or val == '' or val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    # Remove currency symbols, commas, spaces
    cleaned = re.sub(r'[^\d.-]', '', str(val))
    return float(cleaned) if cleaned else 0.0

def clean_percentage(val):
    """Converts percentage string (e.g. '50%', '6.00%') to float decimal or integer (0-100 or 0-1)."""
    if pd.isna(val) or val == '' or val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = re.sub(r'[^\d.-]', '', str(val))
    return float(cleaned) if cleaned else 0.0

# ---------------------------------------------------------
# CORE SYNC PIPELINE
# ---------------------------------------------------------
def run_sync_pipeline(
    projects_csv="Projects-Grid view.csv",
    options_csv="Procurement Options-Grid view.csv",
    non_pricing_csv="Option Line Items - Non-Pricing-Grid view.csv"
):
    print("🚀 Starting Automated Data Ingestion Pipeline...")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # -----------------------------------------------------
            # STEP 1: READ AND PARSE SOURCE CSVS
            # -----------------------------------------------------
            df_projects = pd.read_csv(projects_csv) if os.path.exists(projects_csv) else None
            df_options = pd.read_csv(options_csv) if os.path.exists(options_csv) else None
            df_non_pricing = pd.read_csv(non_pricing_csv) if os.path.exists(non_pricing_csv) else None

            # -----------------------------------------------------
            # STEP 2: CLEAR EXISTING DATA (Prevents Duplication)
            # -----------------------------------------------------
            print("🧹 Clearing existing procurement and project tables...")
            cur.execute("TRUNCATE TABLE options_line_items_non_pricing, procurement_options, projects RESTART IDENTITY CASCADE;")

            # -----------------------------------------------------
            # STEP 3: SYNC PROJECTS
            # -----------------------------------------------------
            if df_projects is not None and not df_projects.empty:
                print(f"📦 Syncing {len(df_projects)} Projects...")
                project_query = """
                    INSERT INTO projects (
                        project_reference, 
                        project_name, 
                        price_weighting, 
                        lowest_project_bid_floor
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (project_reference) DO UPDATE SET
                        project_name = EXCLUDED.project_name,
                        price_weighting = EXCLUDED.price_weighting,
                        lowest_project_bid_floor = EXCLUDED.lowest_project_bid_floor;
                """
                for _, row in df_projects.iterrows():
                    proj_ref = str(row.get('Project Reference', row.get('Project', ''))).strip()
                    proj_name = str(row.get('Project Name', row.get('Name', ''))).strip()
                    price_w = clean_percentage(row.get('Price Weighting %', row.get('Price Weighting', 50)))
                    bid_floor = clean_numeric(row.get('Lowest Bid Floor', row.get('Lowest Bid Lookup', 0)))

                    if proj_ref:
                        cur.execute(project_query, (proj_ref, proj_name, price_w, bid_floor))

            # -----------------------------------------------------
            # STEP 4: SYNC PROCUREMENT OPTIONS
            # -----------------------------------------------------
            if df_options is not None and not df_options.empty:
                print(f"⚙️ Syncing {len(df_options)} Procurement Options...")
                options_query = """
                    INSERT INTO procurement_options (
                        project_id, 
                        vendor_name, 
                        projected_5yr_total
                    )
                    VALUES (%s, %s, %s);
                """
                for _, row in df_options.iterrows():
                    proj_ref = str(row.get('Project', row.get('Project Reference', ''))).strip()
                    vendor = str(row.get('Vendor Name', '')).strip()
                    total_5yr = clean_numeric(row.get('Projected 5-Year Total', 0))

                    if proj_ref and vendor:
                        cur.execute(options_query, (proj_ref, vendor, total_5yr))

            # -----------------------------------------------------
            # STEP 5: SYNC NON-PRICING LINE ITEMS
            # -----------------------------------------------------
            if df_non_pricing is not None and not df_non_pricing.empty:
                print(f"📊 Syncing {len(df_non_pricing)} Non-Pricing Line Items...")
                non_pricing_query = """
                    INSERT INTO options_line_items_non_pricing (
                        line_item_id, 
                        component, 
                        score, 
                        weighted_score_contribution
                    )
                    VALUES (%s, %s, %s, %s);
                """
                for _, row in df_non_pricing.iterrows():
                    item_id = str(row.get('Line_Item_Id', '')).strip()
                    comp = str(row.get('Component', '')).strip()
                    score = clean_numeric(row.get('Score', 0))
                    contrib = clean_numeric(row.get('Weighted Score Contribution', 0))

                    if item_id:
                        cur.execute(non_pricing_query, (item_id, comp, score, contrib))

            conn.commit()
            print("✅ Data Ingestion Pipeline completed successfully!")

    except Exception as e:
        conn.rollback()
        print(f"❌ Error during sync execution: {e}")
        raise e
    finally:
        conn.close()

if __name__ == "__main__":
    run_sync_pipeline()