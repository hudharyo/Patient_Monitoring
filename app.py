from flask import Flask, request, jsonify, render_template, Response, session
from datetime import datetime
import json
import sqlite3
import os
import base64
import re

app = Flask(__name__)
app.secret_key = 'your_secret_key_here_change_in_production'

# SQLite database configuration
DATABASE = "patient_data.db"

# Store the latest received data
received_data = {}
# List of per-client queues for Server-Sent Events (SSE)
data_clients = []
# Store current monitoring session info (in-memory)
current_session = {}
# ---------------------------------------------------------------------------
# Overview / Purpose
# This small Flask application receives patient monitoring payloads (JSON),
# persists them into a local SQLite database, and serves a set of lightweight
# web pages to start/stop monitoring sessions, view live data, and inspect
# historical sessions.
#
# Key concepts:
# - Incoming data: POSTed JSON to `/data`. Payloads may be plain JSON or a
#   Base64-encoded JSON string (the helper `decode_data` handles both).
# - Persistence: Every incoming record is stored in the central `patient_data`
#   table (legacy/backup). If a session is active, the record is also written
#   to a per-session table created dynamically (one table per session).
# - Sessions: Starting a session (`/start-session`) creates a per-session table
#   and inserts a metadata row into `sessions`. Stopping a session (`/stop-session`)
#   clears the in-memory `current_session` so incoming data no longer targets
#   that per-session table.
# - Live updates: The `/stream-data` endpoint provides Server-Sent Events (SSE)
#   to clients. Connected clients receive real-time payloads from `notify_clients`.
#
# Important endpoints (brief):
# - POST /data                : Receive monitoring payloads (JSON or Base64)
# - GET  /stream-data         : SSE stream for live data
# - POST /start-session       : Start a session for a patient (creates per-table)
# - POST /stop-session        : Stop the current session (clears current_session)
# - GET  /get-current-session : Return in-memory current session metadata
# - GET  /get-session-data    : Return rows for a given session id
# - DELETE /delete-session    : Delete session and its per-session table
# - DELETE /delete-patient    : Delete patient and all their session tables
#
# Database (SQLite) layout:
# - patients(id, patient_name, created_at)
# - sessions(id, patient_id, patient_name, table_name, timestamp, created_at)
# - patient_data(id, data, timestamp, created_at)   # central store for all
# - per-session tables: created on start, named <safe_patient_name>_<ts>
#
# Security / Production notes:
# - `app.secret_key` must be changed and managed securely when deploying.
# - This code uses SQLite and in-memory state (`current_session`) which is
#   suitable for single-process usage. For multi-worker production setups you
#   should use a central store (e.g., Redis) for session state and a more
#   robust DB server if concurrent writes/reads are expected.
# ---------------------------------------------------------------------------

def decode_data(data):
    """
    Decode received data. Supports Base64 decoding.
    If data is a dict with 'encoded' key, decode that field.
    If data is a string, attempt to decode it as Base64.
    """
    try:
        # If data is a dictionary with an 'encoded' field
        if isinstance(data, dict) and 'encoded' in data:
            encoded_str = data['encoded']
            decoded_str = base64.b64decode(encoded_str).decode('utf-8')
            decoded_data = json.loads(decoded_str)
            # Merge decoded data back into the original dict
            return {**data, **decoded_data}
        
        # If data is a string, try to decode as Base64
        elif isinstance(data, str):
            decoded_str = base64.b64decode(data).decode('utf-8')
            return json.loads(decoded_str)
        
        # If data is already decoded (dict/list), return as is
        else:
            return data
    except Exception as e:
        print(f"Error decoding data: {e}")
        # Return original data if decoding fails
        return data

def init_db():
    """Initialize the SQLite database and create tables if they don't exist"""
    # If the DB file doesn't exist, create it and required tables
    if not os.path.exists(DATABASE):
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()

        # Create a simple patients table to store unique patient names
        cursor.execute('''
            CREATE TABLE patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_name TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create sessions table which records session metadata and the name of the
        # per-session data table where that session's raw measurements are stored
        cursor.execute('''
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL,
                patient_name TEXT NOT NULL,
                table_name TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (patient_id) REFERENCES patients(id)
            )
        ''')

        # Legacy/general data table used to store all incoming records regardless
        # of active session (keeps a central copy of incoming data)
        cursor.execute('''
            CREATE TABLE patient_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        conn.close()
        print(f"Database '{DATABASE}' created successfully.")
    else:
        # If DB exists ensure the minimum required tables are present. This helps
        # on upgrades/migrations where the DB file exists but tables may be missing.
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='patients'")
        if not cursor.fetchone():
            cursor.execute('''
                CREATE TABLE patients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_name TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
        if not cursor.fetchone():
            cursor.execute('''
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id INTEGER NOT NULL,
                    patient_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (patient_id) REFERENCES patients(id)
                )
            ''')

        conn.commit()
        conn.close()

def save_to_db(data):
    """Save data to the current session table and legacy patient_data table"""
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        data_json = json.dumps(data)
        timestamp = datetime.now().isoformat()
        # Always store a copy in the central `patient_data` table (legacy/backup)
        cursor.execute('''
            INSERT INTO patient_data (data, timestamp)
            VALUES (?, ?)
        ''', (data_json, timestamp))

        # If a monitoring session is active, also write to that session's table.
        # Session-specific tables are created dynamically by `create_session_table`.
        if current_session and 'table_name' in current_session:
            table_name = current_session['table_name']
            try:
                cursor.execute(f'''
                    INSERT INTO "{table_name}" (data, timestamp)
                    VALUES (?, ?)
                ''', (data_json, timestamp))
            except sqlite3.OperationalError:
                # If the per-session table is missing for any reason, log a warning
                # but keep the application running (data is still stored in legacy table).
                print(f"Warning: Could not insert into {table_name}")
        
        conn.commit()
        conn.close()
        print(f"Data saved to database: {data}")
    except Exception as e:
        print(f"Error saving data to database: {e}")

def create_session_table(patient_name, timestamp_str):
    """Create a new table for patient session data with patient name and timestamp"""
    try:
        # Sanitize table name (replace spaces and special chars with underscores)
        safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', patient_name.strip())
        # Remove leading numbers and ensure it doesn't exceed limits
        safe_name = re.sub(r'^[0-9]+', '', safe_name) or 'patient'
        
        # Create table name with timestamp
        timestamp_str = timestamp_str.replace(':', '-').replace('.', '-')
        table_name = f"{safe_name}_{timestamp_str}"
        
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()

        # Create a per-session table to store raw measurement payloads for this
        # monitoring session. Using a separate table per session makes it easier
        # to export or delete data for a single session.
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS "{table_name}" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        conn.close()
        print(f"Session table created: {table_name}")
        return table_name
    except Exception as e:
        print(f"Error creating session table: {e}")
        return None

def get_patient_id(patient_name):
    """Get or create patient ID"""
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # Check if patient exists
        cursor.execute('SELECT id FROM patients WHERE patient_name = ?', (patient_name,))
        result = cursor.fetchone()
        
        if result:
            patient_id = result[0]
        else:
            # Create new patient
            cursor.execute('INSERT INTO patients (patient_name) VALUES (?)', (patient_name,))
            patient_id = cursor.lastrowid
        
        conn.commit()
        conn.close()
        return patient_id
    except Exception as e:
        print(f"Error managing patient: {e}")
        return None

def get_all_data():
    """Retrieve all data from the database"""
    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM patient_data ORDER BY timestamp DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"Error retrieving data from database: {e}")
        return []

# Initialize database when app starts
init_db()

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/data", methods=["GET"])
def show_data():
    return render_template("data.html")

@app.route("/history", methods=["GET"])
def show_history():
    return render_template("history.html")

@app.route("/session-detail", methods=["GET"])
def show_session_detail():
    return render_template("session-detail.html")

@app.route("/data", methods=["POST"])
def receive_data():
    global received_data
    data = request.json
    
    # Decode the received data before processing
    decoded_data = decode_data(data)
    
    received_data = decoded_data
    received_data['_timestamp'] = datetime.now().isoformat()
    print("Decoded Data:", decoded_data)
    
    # Save decoded data to SQLite database
    save_to_db(decoded_data)
    
    # Notify all connected clients
    notify_clients(decoded_data)
    
    return jsonify({"status": "ok"})

@app.route("/get-data", methods=["GET"])
def get_data():
    return jsonify(received_data)

@app.route("/stop-session", methods=["POST"])
def stop_session():
    """Stop the current monitoring session"""
    global current_session
    try:
        # Clearing `current_session` stops writes to the per-session table
        current_session = {}
        return jsonify({"status": "ok", "message": "Session stopped"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/start-session", methods=["POST"])
def start_session():
    """Start a new monitoring session for a patient"""
    global current_session
    try:
        data = request.json
        patient_name = data.get('patient_name', '').strip()
        
        if not patient_name:
            return jsonify({"message": "Patient name is required"}), 400
        
        # Get or create patient (ensures we have a numeric `patient_id`)
        patient_id = get_patient_id(patient_name)
        
        # Create timestamps: a full ISO timestamp for display and a compact
        # timestamp used in the session table name
        timestamp = datetime.now().isoformat()
        timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Create a new per-session table for storing this session's records
        table_name = create_session_table(patient_name, timestamp_str)
        
        if not table_name:
            return jsonify({"message": "Failed to create session table"}), 500
        
        # Save session metadata to the sessions table so we can query available
        # sessions later and find the corresponding per-session table name
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO sessions (patient_id, patient_name, table_name, timestamp)
            VALUES (?, ?, ?, ?)
        ''', (patient_id, patient_name, table_name, timestamp))
        session_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # Store current session info in-memory so incoming data gets written to
        # the correct per-session table while the session is active
        current_session = {
            'session_id': session_id,
            'patient_id': patient_id,
            'patient_name': patient_name,
            'table_name': table_name,
            'timestamp': timestamp
        }
        
        return jsonify({
            "session_id": session_id,
            "patient_name": patient_name,
            "table_name": table_name,
            "timestamp": timestamp,
            "message": "Session started successfully"
        }), 200
    
    except Exception as e:
        print(f"Error starting session: {e}")
        return jsonify({"message": f"Error: {str(e)}"}), 500

@app.route("/get-recent-patients", methods=["GET"])
def get_recent_patients():
    """Get list of recent patients"""
    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get last 5 unique patients ordered by most recent
        cursor.execute('''
            SELECT DISTINCT patient_name, MAX(created_at) as created_at 
            FROM sessions
            GROUP BY patient_name
            ORDER BY created_at DESC
            LIMIT 5
        ''')
        rows = cursor.fetchall()
        conn.close()
        
        patients = [dict(row) for row in rows]
        return jsonify({"patients": patients}), 200
    
    except Exception as e:
        print(f"Error getting recent patients: {e}")
        return jsonify({"patients": []}), 200

@app.route("/get-current-session", methods=["GET"])
def get_current_session():
    """Get current monitoring session info"""
    # Return the in-memory `current_session` (empty if no session active)
    return jsonify(current_session if current_session else {}), 200

@app.route("/get-all-patients", methods=["GET"])
def get_all_patients():
    """Get all patients with their info"""
    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM patients ORDER BY created_at DESC')
        rows = cursor.fetchall()
        conn.close()
        
        patients = [dict(row) for row in rows]
        return jsonify({"patients": patients}), 200
    except Exception as e:
        print(f"Error getting all patients: {e}")
        return jsonify({"patients": []}), 200

@app.route("/get-patient-sessions", methods=["GET"])
def get_patient_sessions():
    """Get all sessions for a patient"""
    try:
        patient_id = request.args.get('patient_id')
        if not patient_id:
            return jsonify({"patient": None, "sessions": []}), 400
        
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get patient info
        cursor.execute('SELECT * FROM patients WHERE id = ?', (patient_id,))
        patient_row = cursor.fetchone()
        patient = dict(patient_row) if patient_row else None
        
        # Get sessions
        cursor.execute('SELECT * FROM sessions WHERE patient_id = ? ORDER BY timestamp DESC', (patient_id,))
        session_rows = cursor.fetchall()
        sessions = [dict(row) for row in session_rows]
        
        conn.close()
        
        return jsonify({"patient": patient, "sessions": sessions}), 200
    except Exception as e:
        print(f"Error getting patient sessions: {e}")
        return jsonify({"patient": None, "sessions": []}), 500

@app.route("/get-session-info", methods=["GET"])
def get_session_info():
    """Get session information"""
    try:
        session_id = request.args.get('session_id')
        if not session_id:
            return jsonify({"error": "No session ID provided"}), 400
        
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM sessions WHERE id = ?', (session_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return jsonify(dict(row)), 200
        else:
            return jsonify({"error": "Session not found"}), 404
    except Exception as e:
        print(f"Error getting session info: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/get-session-record-count", methods=["GET"])
def get_session_record_count():
    """Get record count for a session table"""
    try:
        table_name = request.args.get('table_name')
        if not table_name:
            return jsonify({"count": 0}), 400
        
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        try:
            # Query the dynamically named per-session table for a record count
            cursor.execute(f'SELECT COUNT(*) as count FROM "{table_name}"')
            result = cursor.fetchone()
            count = result[0] if result else 0
        except sqlite3.OperationalError:
            # If the table does not exist, treat count as zero
            count = 0
        
        conn.close()
        return jsonify({"count": count}), 200
    except Exception as e:
        print(f"Error getting record count: {e}")
        return jsonify({"count": 0}), 500

@app.route("/get-session-data", methods=["GET"])
def get_session_data():
    """Get all data for a specific session"""
    try:
        session_id = request.args.get('session_id')
        if not session_id:
            return jsonify({"data": []}), 400
        
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Look up the session record to find the per-session table name
        cursor.execute('SELECT table_name FROM sessions WHERE id = ?', (session_id,))
        session = cursor.fetchone()
        
        if not session:
            conn.close()
            return jsonify({"data": []}), 404
        
        table_name = session['table_name']
        
        # Fetch rows from the per-session table in chronological order
        try:
            cursor.execute(f'SELECT * FROM "{table_name}" ORDER BY timestamp ASC')
            rows = cursor.fetchall()
            data = [dict(row) for row in rows]
        except sqlite3.OperationalError:
            # If the table is missing or an error occurs, return an empty list
            data = []
        
        conn.close()
        return jsonify({"data": data}), 200
    except Exception as e:
        print(f"Error getting session data: {e}")
        return jsonify({"data": []}), 500

@app.route("/delete-patient", methods=["DELETE"])
def delete_patient():
    """Delete a patient and all their sessions and data"""
    try:
        patient_id = request.args.get('patient_id')
        if not patient_id:
            return jsonify({"message": "No patient ID provided"}), 400
        
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # Get all sessions for this patient
        cursor.execute('SELECT table_name FROM sessions WHERE patient_id = ?', (patient_id,))
        sessions = cursor.fetchall()
        
        # Drop each per-session table associated with this patient
        for session in sessions:
            table_name = session[0]
            try:
                cursor.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            except Exception as e:
                print(f"Error dropping table {table_name}: {e}")
        
        # Delete sessions
        cursor.execute('DELETE FROM sessions WHERE patient_id = ?', (patient_id,))
        
        # Delete patient
        cursor.execute('DELETE FROM patients WHERE id = ?', (patient_id,))
        
        conn.commit()
        conn.close()
        
        return jsonify({"message": "Patient deleted successfully"}), 200
    except Exception as e:
        print(f"Error deleting patient: {e}")
        return jsonify({"message": f"Error: {str(e)}"}), 500

@app.route("/delete-session", methods=["DELETE"])
def delete_session():
    """Delete a session and its data"""
    try:
        session_id = request.args.get('session_id')
        if not session_id:
            return jsonify({"message": "No session ID provided"}), 400
        
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # Get session to find table name
        cursor.execute('SELECT table_name FROM sessions WHERE id = ?', (session_id,))
        session = cursor.fetchone()
        
        if session:
            table_name = session[0]
            # Drop session table if present
            try:
                cursor.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            except Exception as e:
                print(f"Error dropping table {table_name}: {e}")
        
        # Delete session
        cursor.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
        
        conn.commit()
        conn.close()
        
        return jsonify({"message": "Session deleted successfully"}), 200
    except Exception as e:
        print(f"Error deleting session: {e}")
        return jsonify({"message": f"Error: {str(e)}"}), 500

@app.route("/stream-data", methods=["GET"])
def stream_data():
    def generate():
        # Send initial data if exists
        if received_data:
            yield f"data: {json.dumps(received_data)}\n\n"
        
        # Keep connection open and wait for new data
        client_queue = []
        data_clients.append(client_queue)
        
        try:
            while True:
                if client_queue:
                    data = client_queue.pop(0)
                    yield f"data: {json.dumps(data)}\n\n"
        finally:
            data_clients.remove(client_queue)
    
    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no"
    })

def notify_clients(data):
    for client_queue in data_clients:
        client_queue.append(data)

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",  # IMPORTANT
        port=5000,
        debug=True
    )

