import os
from flask import Blueprint, render_template, request, redirect, url_for, current_app
from datetime import date
from app_package.db import get_db_connection
from werkzeug.utils import secure_filename

meetings_bp = Blueprint('meetings', __name__)

@meetings_bp.route('/minutes', methods=['GET', 'POST'])
def finance_minutes():
    conn = get_db_connection()
    meeting_type = request.args.get('type') or request.form.get('meeting_type') or 'Finance'

    if request.method == 'POST':
        meeting_date = request.form.get('meeting_date')
        chairperson = request.form.get('chairperson')
        attendees = request.form.get('attendees')
        apologies = request.form.get('apologies')
        notes_summary = request.form.get('notes_summary')

        # --- Handle File Upload ---
        attachment_filename = None
        if 'attachment' in request.files:
            file = request.files['attachment']
            if file and file.filename.strip() != '':
                filename = secure_filename(file.filename)
                
                # Ensure upload folder exists
                os.makedirs(current_app.config['UPLOAD_FOLDER'], exist_ok=True)
                
                # Save file to disk
                file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
                file.save(file_path)
                attachment_filename = filename

        with conn.cursor() as cursor:
            # Insert meeting including the attachment filename
            cursor.execute("""
                INSERT INTO meeting_minutes 
                (meeting_type, meeting_date, chairperson, attendees, apologies, notes_summary, attachment_filename)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
            """, (meeting_type, meeting_date, chairperson, attendees, apologies, notes_summary, attachment_filename))
            
            row = cursor.fetchone()
            meeting_id = row['id'] if isinstance(row, dict) else row[0]

            # --- Handle Action Items (with your date fix) ---
            descriptions = request.form.getlist('action_description[]')
            responsibles = request.form.getlist('responsible_person[]')
            targets = request.form.getlist('target_date[]')

            for i in range(len(descriptions)):
                desc = descriptions[i]
                if desc and desc.strip():
                    resp = responsibles[i] if i < len(responsibles) else ''
                    raw_target = targets[i] if i < len(targets) else None
                    target = raw_target if (raw_target and raw_target.strip()) else (meeting_date or str(date.today()))

                    cursor.execute("""
                        INSERT INTO meeting_action_items (meeting_id, action_description, responsible_person, target_date, status)
                        VALUES (%s, %s, %s, %s, 'Pending');
                    """, (meeting_id, desc, resp, target))

            conn.commit()
        conn.close()
        return redirect(url_for('meetings.finance_minutes', type=meeting_type))

    # GET request: Fetch meetings filtered by the selected meeting_type
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT id, meeting_type, meeting_date, chairperson, attendees, apologies, notes_summary, attachment_filename 
            FROM meeting_minutes 
            WHERE meeting_type = %s
            ORDER BY meeting_date DESC;
        """, (meeting_type,))
        meetings = cursor.fetchall()

        
        cursor.execute("""
            SELECT a.id, a.meeting_id, a.action_description, a.responsible_person, 
                   a.target_date, a.status, a.action_notes, m.meeting_date AS created_meeting_date, m.meeting_type
            FROM meeting_action_items a
            JOIN meeting_minutes m ON a.meeting_id = m.id
            WHERE m.meeting_type = %s
            ORDER BY a.target_date ASC;
        """, (meeting_type,))
        all_actions = cursor.fetchall()

        for m in meetings:
            m_date = m['meeting_date'] if isinstance(m, dict) else m[2]

            relevant_actions = []
            for act in all_actions:
                act_status = act['status'] if isinstance(act, dict) else act[5]
                act_created_date = act['created_meeting_date'] if isinstance(act, dict) else act[7]

                if act_status != 'Completed':
                    relevant_actions.append(act)
                elif act_status == 'Completed' and act_created_date >= m_date:
                    relevant_actions.append(act)

            m['action_items'] = relevant_actions

    conn.close()
    return render_template('minutes.html', meetings=meetings, current_type=meeting_type)


@meetings_bp.route('/update_meeting_minutes', methods=['POST'])
def update_meeting_minutes():
    meeting_id = request.form.get('meeting_id')
    meeting_type = request.form.get('meeting_type', 'Finance')
    meeting_date = request.form.get('meeting_date')
    chairperson = request.form.get('chairperson')
    attendees = request.form.get('attendees')
    apologies = request.form.get('apologies')
    notes_summary = request.form.get('notes_summary')

    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("""
            UPDATE meeting_minutes
            SET meeting_type = %s,
                meeting_date = %s,
                chairperson = %s,
                attendees = %s,
                apologies = %s,
                notes_summary = %s
            WHERE id = %s;
        """, (meeting_type, meeting_date, chairperson, attendees, apologies, notes_summary, meeting_id))

        delete_action_ids = request.form.getlist('delete_action_id[]')
        for act_id in delete_action_ids:
            if act_id:
                cursor.execute("DELETE FROM meeting_action_items WHERE id = %s;", (act_id,))

        action_ids = request.form.getlist('existing_action_id[]')
        descriptions = request.form.getlist('existing_action_description[]')
        responsibles = request.form.getlist('existing_responsible_person[]')
        targets = request.form.getlist('existing_target_date[]')

        for i in range(len(descriptions)):
            act_id = action_ids[i] if (i < len(action_ids) and action_ids[i]) else None
            desc = descriptions[i]
            resp = responsibles[i]
            
            raw_target = targets[i] if i < len(targets) else None
            target = raw_target if (raw_target and raw_target.strip()) else (meeting_date or str(date.today()))

            if act_id:
                cursor.execute("""
                    UPDATE meeting_action_items
                    SET action_description = %s,
                        responsible_person = %s,
                        target_date = %s
                    WHERE id = %s;
                """, (desc, resp, target, act_id))
            else:
                cursor.execute("""
                    INSERT INTO meeting_action_items (meeting_id, action_description, responsible_person, target_date, status)
                    VALUES (%s, %s, %s, %s, 'Pending');
                """, (meeting_id, desc, resp, target))

        conn.commit()
    conn.close()

    return redirect(url_for('meetings.finance_minutes', type=meeting_type))


@meetings_bp.route('/delete_meeting', methods=['POST'])
def delete_meeting():
    meeting_id = request.form.get('meeting_id')
    meeting_type = request.form.get('meeting_type', 'Finance')

    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM meeting_action_items WHERE meeting_id = %s;", (meeting_id,))
        cursor.execute("DELETE FROM meeting_minutes WHERE id = %s;", (meeting_id,))
        conn.commit()
    conn.close()

    return redirect(url_for('meetings.finance_minutes', type=meeting_type))


@meetings_bp.route('/update_action_item', methods=['POST'])
def update_action_item():
    action_id = request.form.get('action_id')
    action_description = request.form.get('action_description')
    responsible_person = request.form.get('responsible_person')
    
    # Sanitize target_date: convert empty strings to None (SQL NULL)
    raw_target_date = request.form.get('target_date')
    target_date_val = raw_target_date if (raw_target_date and raw_target_date.strip()) else None

    status = request.form.get('status')
    action_notes = request.form.get('action_notes')

    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("""
            UPDATE meeting_action_items
            SET action_description = %s,
                responsible_person = %s,
                target_date = %s,
                status = %s,
                action_notes = %s
            WHERE id = %s;
        """, (action_description, responsible_person, target_date_val, status, action_notes, action_id))
        conn.commit()
    conn.close()

    return redirect(url_for('meetings.finance_minutes'))


@meetings_bp.route('/delete_action_item', methods=['POST'])
def delete_action_item():
    action_id = request.form.get('action_id')
    if action_id:
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM meeting_action_items WHERE id = %s;", (action_id,))
            conn.commit()
        except Exception as e:
            conn.rollback()
        finally:
            conn.close()
    return redirect(url_for('meetings.finance_minutes'))
