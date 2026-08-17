import os
import psycopg2
from psycopg2.extras import RealDictCursor
from google import genai
from google.genai import types

# ---------------------------------------------------------
# CONFIGURATION & CONNECTIONS
# ---------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# Initialize standard Google GenAI SDK client
client = genai.Client(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------
# 1. FETCH EVALUATED OPTIONS FROM POSTGRES VIEW
# ---------------------------------------------------------
def get_project_evaluation_data(project_ref: str) -> dict:
    """
    Retrieves project details and dynamically evaluated vendor options.
    """
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Fetch project master metadata
            cur.execute(
                "SELECT project_reference, name AS project_name, price_weighting, lowest_project_bid_floor "
                "FROM projects WHERE project_reference = %s;", 
                (project_ref,)
            )
            project = cur.fetchone()
            
            if not project:
                raise ValueError(f"Project with reference '{project_ref}' not found.")

            # Fetch evaluated vendor options from live Postgres view
            cur.execute(
                "SELECT procurement_option_id, vendor_name, projected_5yr_total, "
                "calculated_price_score, calculated_non_pricing_score, calculated_final_score "
                "FROM procurement_options_evaluated "
                "WHERE project_reference = %s "
                "ORDER BY calculated_final_score DESC;", 
                (project_ref,)
            )
            options = cur.fetchall()

            return {
                "project": dict(project),
                "options": [dict(opt) for opt in options]
            }
    finally:
        conn.close()


# ---------------------------------------------------------
# 2. GENERATE AI SOURCING RECOMMENDATION
# ---------------------------------------------------------
def generate_sourcing_recommendation(eval_data: dict) -> str:
    """
    Constructs a prompt using evaluated scores and prompts Gemini to produce an executive synthesis.
    """
    project = eval_data["project"]
    options = eval_data["options"]

    # Build clear vendor comparison text block for prompt input
    vendor_lines = []
    for opt in options:
        line = (
            f"- Vendor: {opt['vendor_name']} | 5-Year Cost: R{opt['projected_5yr_total']:,.2f} | "
            f"Price Score: {opt['calculated_price_score']} | "
            f"Non-Pricing Score: {opt['calculated_non_pricing_score']} | "
            f"TOTAL WEIGHTED SCORE: {opt['calculated_final_score']}"
        )
        vendor_lines.append(line)
    
    vendor_summary_text = "\n".join(vendor_lines)

    prompt = f"""
You are an executive procurement advisor reviewing vendor bids for the following project:

PROJECT DETAILS:
- Reference: {project['project_reference']}
- Name: {project['project_name']}
- Price Weighting: {project['price_weighting']}%
- Lowest Bid Floor: R{project['lowest_project_bid_floor']:,.2f}

EVALUATED VENDOR OPTIONS (Ranked by Total Weighted Score):
{vendor_summary_text}

TASK:
Provide a concise, formal Executive Sourcing Recommendation (approx 150-250 words) structured as follows:
1. Recommended Vendor & Justification (balance of cost vs technical non-pricing score)
2. Risk Analysis / Trade-Offs (what trade-offs exist between top choices)
3. Actionable Next Steps for Board / Finance Committee Approval
"""

    print(f"🤖 Requesting AI analysis for {project['project_reference']} via Gemini...")

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2, # Low temperature for consistent corporate output
        )
    )

    return response.text


# ---------------------------------------------------------
# 3. SAVE RECOMMENDATION BACK TO POSTGRES
# ---------------------------------------------------------
def save_recommendation_to_db(project_ref: str, recommendation: str):
    """
    Updates the projects table with the generated executive sourcing recommendation.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE projects "
                "SET executive_sourcing_recommendation = %s "
                "WHERE project_reference = %s;",
                (recommendation, project_ref)
            )
            conn.commit()
            print(f"✅ Sourcing recommendation stored successfully for project: {project_ref}")
    finally:
        conn.close()


# ---------------------------------------------------------
# EXECUTION WORKFLOW
# ---------------------------------------------------------
def run_ai_sourcing_pipeline(project_ref: str):
    data = get_project_evaluation_data(project_ref)
    
    if not data["options"]:
        print(f"⚠️ No evaluated options found for project reference: {project_ref}")
        return

    recommendation = generate_sourcing_recommendation(data)
    save_recommendation_to_db(project_ref, recommendation)
    
    print("\n--- EXECUTIVE RECOMMENDATION PREVIEW ---")
    print(recommendation)


if __name__ == "__main__":
    # Test with project reference
    target_project = "2026_02" 
    run_ai_sourcing_pipeline(target_project)