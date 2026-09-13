from flask import Blueprint, request, jsonify, flash

# Define the blueprint
approved_vendors_bp = Blueprint('approved_vendors', __name__)

# Import your database connection function from your main module or db module
from app import get_db_connection


# 1. API: Get list of all approved vendors
@approved_vendors_bp.route('/api/approved_vendors', methods=['GET'])
def get_approved_vendors():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, vendor_name, supplier_code, gl_code, 
                   monthly_threshold_amount, last_approval_date, 
                   expiry_date, approval_reference, is_active, notes
            FROM public.approved_vendors
            ORDER BY vendor_name ASC;
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({'status': 'success', 'data': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# 2. API: Create a new approved vendor whitelist entry
@approved_vendors_bp.route('/api/approved_vendors/add', methods=['POST'])
def add_approved_vendor():
    try:
        data = request.form if request.form else request.get_json()
        
        vendor_name = data.get('vendor_name')
        supplier_code = data.get('supplier_code', '').strip().upper()
        gl_code = data.get('gl_code')
        monthly_threshold_amount = float(data.get('monthly_threshold_amount', 0.0))
        last_approval_date = data.get('last_approval_date')
        expiry_date = data.get('expiry_date') or None
        approval_reference = data.get('approval_reference')
        notes = data.get('notes')

        if not vendor_name or not supplier_code or not last_approval_date:
            return jsonify({'status': 'error', 'message': 'Vendor name, supplier code, and approval date are required.'}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO public.approved_vendors (
                vendor_name, supplier_code, gl_code, monthly_threshold_amount,
                last_approval_date, expiry_date, approval_reference, is_active, notes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
            RETURNING id;
        """, (vendor_name, supplier_code, gl_code, monthly_threshold_amount, 
              last_approval_date, expiry_date, approval_reference, notes))
        
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        conn.close()

        flash(f"Vendor '{vendor_name}' successfully white-listed.", "success")
        return jsonify({'status': 'success', 'id': new_id, 'message': 'Vendor added successfully.'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# 3. API: Toggle Active/Suspended Status
@approved_vendors_bp.route('/api/approved_vendors/<int:vendor_id>/toggle', methods=['POST'])
def toggle_vendor_status(vendor_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE public.approved_vendors 
            SET is_active = NOT is_active 
            WHERE id = %s 
            RETURNING is_active, vendor_name;
        """, (vendor_id,))
        result = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

        if result:
            status_str = "Activated" if result['is_active'] else "Suspended"
            return jsonify({'status': 'success', 'message': f"Vendor '{result['vendor_name']}' is now {status_str}."})
        return jsonify({'status': 'error', 'message': 'Vendor not found.'}), 404
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@approved_vendors_bp.route('/approved_vendors_ui')
def approved_vendors_ui():
    """Renders the dedicated Whitelist Management Dashboard page."""
    return render_template('approved_vendors.html')