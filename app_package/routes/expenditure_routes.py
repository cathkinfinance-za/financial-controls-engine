import os
from flask import Blueprint, render_template, request, redirect, url_for, flash
from collections import defaultdict
from app_package.db import get_db_connection

try:
    from google import genai
    GEMINI_AVAILABLE = True
except ModuleNotFoundError:
    genai = None
    GEMINI_AVAILABLE = False

expenditure_bp = Blueprint('expenditure', __name__)

ALLOWED_MONTHS = {
    'mar_2026': 'March 2026',
    'apr_2026': 'April 2026',
    'may_2026': 'May 2026',
    'jun_2026': 'June 2026',
    'jul_2026': 'July 2026',
    'aug_2026': 'August 2026',
    'sep_2026': 'September 2026'
}

@expenditure_bp.route('/expenditure_expose')
def expenditure_expose():
    selected_month = request.args.get('month', 'sep_2026')
    if selected_month not in ALLOWED_MONTHS:
        selected_month = 'sep_2026'

    all_items = []
    grouped_audit = {}
    category_totals = {}
    cached_analysis = None

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT analysis_text 
            FROM public.expenditure_ai_cache 
            WHERE month_code = %s;
        """, (selected_month,))
        cache_row = cur.fetchone()
        if cache_row:
            cached_analysis = cache_row['analysis_text']

        query = f"""
            SELECT gl_code, description, 
                   COALESCE({selected_month}, 0) AS selected_month_actual,
                   COALESCE(ytd, 0) AS ytd, 
                   COALESCE(budget_ytd, 0) AS budget_ytd, 
                   COALESCE(variance, 0) AS variance, 
                   COALESCE(total_budget, 0) AS total_budget
            FROM public.master_budget
            WHERE gl_code NOT LIKE '%%/000'
            ORDER BY gl_code ASC;
        """
        cur.execute(query)
        all_items = cur.fetchall()

        selected_month_clean = selected_month.lower().strip()
        month_str, year_str = selected_month_clean.split('_')

        MONTH_MAP = {
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
            'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
        }

        target_month = MONTH_MAP.get(month_str)
        target_year = int(year_str)

        cur.execute("""
            SELECT 
                transaction_date, transaction_text, amount, gl_code, supplier_code, 
                authority_tier, min_quotes_required, po_number, quotes_attached, compliance_verdict
            FROM vw_cashbook_compliance_audit
            WHERE EXTRACT(MONTH FROM transaction_date) = %s
            AND EXTRACT(YEAR FROM transaction_date) = %s
            ORDER BY transaction_date DESC;
        """, (target_month, target_year))

        raw_audit_rows = cur.fetchall()
        grouped_audit_raw = defaultdict(list)
        category_totals = defaultdict(float)

        for row in raw_audit_rows:
            verdict = row['compliance_verdict']
            grouped_audit_raw[verdict].append(row)
            category_totals[verdict] += float(abs(row['amount']))

        VERDICT_ORDER = {
            'NON-COMPLIANT: Missing Approved PO': 1,
            'NON-COMPLIANT: PO Not Approved': 2,
            'NON-COMPLIANT: Insufficient Quotes': 3,
            'FLAGGED: Unbudgeted / Suspense Allocation': 4,
            'FLAGGED: Anti-Splitting Suspected': 5,
            'FLAGGED: Approved Vendor Threshold Exceeded': 6,
            'FLAGGED: Approved Vendor Authorization Expired': 7,
            'COMPLIANT: Approved Regular Vendor': 8,
            'COMPLIANT: Petty/Minor Spend (No PO Required)': 9,
            'COMPLIANT': 10,
            'COMPLIANT: Exempt (Payroll Spend)': 11
        }

        sorted_categories = sorted(grouped_audit_raw.keys(), key=lambda cat: VERDICT_ORDER.get(cat, 99))
        grouped_audit = {cat: grouped_audit_raw[cat] for cat in sorted_categories}

        cur.close()
        conn.close()
    except Exception as e:
        flash(f"Database error: {str(e)}", "danger")

    income_items = [
        item for item in all_items 
        if str(item['gl_code']).startswith('1') or str(item['gl_code']).startswith('RFI')
    ]
    expenditure_items = [
        item for item in all_items 
        if str(item['gl_code']).startswith('2') or str(item['gl_code']).startswith('RFE') or str(item['gl_code']).startswith('RFI')
    ]

    over_budget_expenditure = []
    for item in expenditure_items:
        ytd = float(item['ytd'])
        budget_ytd = float(item['budget_ytd'])
        if ytd > budget_ytd:
            item_dict = dict(item)
            item_dict['variance'] = round(ytd - budget_ytd)
            item_dict['ytd_pct'] = round((ytd / budget_ytd * 100.0)) if budget_ytd > 0 else 0
            over_budget_expenditure.append(item_dict)
    over_budget_expenditure.sort(key=lambda x: x['variance'], reverse=True)

    under_budget_income = []
    for item in income_items:
        ytd = float(item['ytd'])
        budget_ytd = float(item['budget_ytd'])
        if ytd < budget_ytd:
            item_dict = dict(item)
            item_dict['variance'] = round(budget_ytd - ytd)
            item_dict['ytd_pct'] = round((ytd / budget_ytd * 100.0)) if budget_ytd > 0 else 0
            under_budget_income.append(item_dict)
    under_budget_income.sort(key=lambda x: x['variance'], reverse=True)

    total_ytd_income = sum(float(item['ytd']) for item in income_items)
    total_ytd_income_budget = sum(float(item['budget_ytd']) for item in income_items)
    total_ytd_expenditure = sum(float(item['ytd']) for item in expenditure_items)
    total_ytd_expenditure_budget = sum(float(item['budget_ytd']) for item in expenditure_items)
    total_selected_month_spend = sum(float(item['selected_month_actual']) for item in expenditure_items)
    total_expenditure_overrun = sum(float(item['variance']) for item in over_budget_expenditure)
    total_expenditure_variance = total_ytd_expenditure - total_ytd_expenditure_budget
    month_label = ALLOWED_MONTHS[selected_month]

    if cached_analysis:
        ai_analysis = cached_analysis
    elif GEMINI_AVAILABLE and all_items:
        conn_ai = None
        try:
            conn_ai = get_db_connection()
            cur_ai = conn_ai.cursor()
            cur_ai.execute("SELECT prompt_template, selected_model FROM public.system_prompts WHERE LOWER(process) = LOWER('expenditure_expose') AND is_active = TRUE;")
            prompt_row = cur_ai.fetchone()

            if not prompt_row or not prompt_row.get('prompt_template'):
                ai_analysis = "AI analysis is currently disabled or template is missing."
            else:
                income_summary = "\n".join([f"- GL {i['gl_code']} ({i['description']}): YTD Actual R{float(i['ytd']):,.2f} vs Budget R{float(i['budget_ytd']):,.2f}" for i in income_items])
                expenditure_summary = "\n".join([f"- GL {e['gl_code']} ({e['description']}): YTD Actual R{float(e['ytd']):,.2f} vs Budget R{float(e['budget_ytd']):,.2f}" for e in expenditure_items])

                formatted_prompt = prompt_row['prompt_template'].format(
                    month_label=month_label, total_ytd_income=total_ytd_income, total_ytd_income_budget=total_ytd_income_budget,
                    income_summary=income_summary, total_ytd_expenditure=total_ytd_expenditure, total_ytd_expenditure_budget=total_ytd_expenditure_budget,
                    total_expenditure_overrun=total_expenditure_overrun, expenditure_summary=expenditure_summary
                )

                model_name = prompt_row.get('selected_model') or 'gemini-3.6-flash'
                client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
                response = client.models.generate_content(model=model_name, contents=formatted_prompt)
                ai_analysis = response.text

                cur_ai.execute("""
                    INSERT INTO public.expenditure_ai_cache (month_code, analysis_text)
                    VALUES (%s, %s)
                    ON CONFLICT (month_code) DO UPDATE SET analysis_text = EXCLUDED.analysis_text, created_at = CURRENT_TIMESTAMP;
                """, (selected_month, ai_analysis))
                conn_ai.commit()
        except Exception as e:
            ai_analysis = f"AI Analysis temporarily unavailable: {str(e)}"
        finally:
            if conn_ai:
                conn_ai.close()
    else:
        ai_analysis = "AI analysis is currently disabled or unavailable."
    
    return render_template(
        'expenditure_expose.html',
        income_items=income_items, expenditure_items=expenditure_items,
        over_budget_expenditure=over_budget_expenditure, under_budget_income=under_budget_income,
        total_ytd_income=total_ytd_income, total_ytd_income_budget=total_ytd_income_budget,
        total_ytd_expenditure=total_ytd_expenditure, total_ytd_expenditure_budget=total_ytd_expenditure_budget,
        total_selected_month_spend=total_selected_month_spend, total_expenditure_overrun=total_expenditure_overrun,
        total_expenditure_variance=total_expenditure_variance, selected_month=selected_month,
        allowed_months=ALLOWED_MONTHS, ai_analysis=ai_analysis, is_cached=bool(cached_analysis),
        grouped_audit=grouped_audit, category_totals=category_totals
    )


@expenditure_bp.route('/expenditure_expose/refresh/<month>')
def refresh_expenditure_ai(month):
    if month in ALLOWED_MONTHS:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM public.expenditure_ai_cache WHERE month_code = %s;", (month,))
            conn.commit()
            cur.close()
            conn.close()
            flash(f"AI commentary cache cleared for {ALLOWED_MONTHS[month]}. Re-generating...", "info")
        except Exception as e:
            flash(f"Error clearing cache: {str(e)}", "danger")
    return redirect(url_for('expenditure.expenditure_expose', month=month))
