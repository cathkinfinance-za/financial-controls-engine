import os
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL")

def get_connection():
    """Establish connection to Neon PostgreSQL database."""
    return psycopg2.connect(DATABASE_URL)

# ==========================================
# 1. CLEANUP ENGINE (Prevents Duplication)
# ==========================================

def clear_project_cascade(project_ref: str):
    """
    Deletes an existing project and its associated options/line items.
    Relies on ON DELETE CASCADE constraints defined in schema.
    """
    query = "DELETE FROM projects WHERE project_reference = %s;"
    
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (project_ref,))
            conn.commit()
            print(f"Cleared existing records for project reference: {project_ref}")


# ==========================================
# 2. DATA INGESTION & UPSERTS
# ==========================================

def upsert_project(project_data: dict):
    """
    Inserts or updates a master project record.
    """
    query = """
        INSERT INTO projects (
            project_reference, 
            project_name, 
            price_weighting, 
            lowest_project_bid_floor
        )
        VALUES (%(project_reference)s, %(project_name)s, %(price_weighting)s, %(lowest_project_bid_floor)s)
        ON CONFLICT (project_reference) DO UPDATE SET
            project_name = EXCLUDED.project_name,
            price_weighting = EXCLUDED.price_weighting,
            lowest_project_bid_floor = EXCLUDED.lowest_project_bid_floor;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, project_data)
            conn.commit()

def bulk_insert_procurement_options(options_list: list):
    """
    Inserts vendor options linked to a project.
    """
    query = """
        INSERT INTO procurement_options (
            project_id, 
            vendor_name, 
            projected_5yr_total
        )
        VALUES (%(project_id)s, %(vendor_name)s, %(projected_5yr_total)s)
        RETURNING id, vendor_name;
    """
    inserted_ids = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            for opt in options_list:
                cur.execute(query, opt)
                row = cur.fetchone()
                inserted_ids[row[1]] = row[0]  # Map vendor_name -> generated integer id
            conn.commit()
    return inserted_ids

def bulk_insert_non_pricing_items(items_list: list):
    """
    Inserts evaluation line items for non-pricing criteria.
    """
    query = """
        INSERT INTO options_line_items_non_pricing (
            line_item_id, 
            component, 
            score, 
            weighted_score_contribution
        )
        VALUES (%(line_item_id)s, %(component)s, %(score)s, %(weighted_score_contribution)s);
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(query, items_list)
            conn.commit()


# ==========================================
# 3. EVALUATION & SCORING READS
# ==========================================

def get_evaluated_scores_for_project(project_ref: str) -> list:
    """
    Queries the live `procurement_options_evaluated` view for a given project reference.
    Returns ranked options with calculated price scores, non-pricing scores, and final weighted totals.
    """
    query = """
        SELECT 
            procurement_option_id,
            project_reference,
            vendor_name,
            projected_5yr_total,
            price_weighting,
            lowest_project_bid_floor,
            calculated_price_score,
            calculated_non_pricing_score,
            calculated_final_score
        FROM procurement_options_evaluated
        WHERE project_reference = %s
        ORDER BY calculated_final_score DESC;
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (project_ref,))
            results = cur.fetchall()
            return [dict(row) for row in results]