import os
import io
import sqlite3
import hashlib
import datetime
from functools import wraps
from flask import (
    Flask, request, redirect, url_for, render_template,
    send_from_directory, jsonify, session, abort, flash
)
from werkzeug.utils import secure_filename
import numpy as np
import cv2
import face_recognition
import threading
import time

# ------------------ Config ------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
KNOWN_DIR = os.path.join(DATA_DIR, "known_faces")
CAPTURES_DIR = os.path.join(DATA_DIR, "captures")
DB_PATH = os.path.join(DATA_DIR, "encodings.db")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(KNOWN_DIR, exist_ok=True)
os.makedirs(CAPTURES_DIR, exist_ok=True)

API_KEY = os.getenv("FACE_API_KEY", "cybarry")  # must match ESP32-CAM
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")  # change in prod

# Recognition thresholds
TOLERANCE = float(os.getenv("FR_TOLERANCE", "0.45"))
TOP_MATCH_ONLY = True      # if True, use best match by distance
SAVE_CAPTURE = True        # save every request frame

# Flask
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change_me_secret")

# ------------------ DB Helpers ------------------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False,)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS encodings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        encoding BLOB NOT NULL,
        image_path TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT,
        image_path TEXT,
        ts TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS rfid_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid TEXT UNIQUE,
        username TEXT
    )""")

    # Create indexes for better performance
    c.execute("CREATE INDEX IF NOT EXISTS idx_encodings_user_id ON encodings(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)")
    conn.commit()
    conn.close()

init_db()

# ------------------ Auth for Admin UI ------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username", "")
        pw = request.form.get("password", "")
        if user == ADMIN_USER and pw == ADMIN_PASS:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ------------------ Load Encodings (RAM cache) ------------------
def load_all_encodings():
    conn = db()
    c = conn.cursor()
    c.execute("""
      SELECT users.id, users.name, encodings.encoding
      FROM encodings JOIN users ON encodings.user_id = users.id
    """)
    ids, names, encs = [], [], []
    for row in c.fetchall():
        ids.append(row["id"])
        names.append(row["name"])
        # Create a new array instead of using the buffer directly
        enc_array = np.frombuffer(row["encoding"], dtype=np.float64).copy()
        encs.append(enc_array)
    conn.close()
    
    if encs:
        try:
            encs = np.vstack(encs)
        except ValueError:
            # Handle case where encodings have different shapes
            print("Warning: Encodings have inconsistent shapes, resetting cache")
            return [], [], np.empty((0, 128), dtype=np.float64)
    else:
        encs = np.empty((0, 128), dtype=np.float64)
        
    return ids, names, encs

KNOWN_IDS, KNOWN_NAMES, KNOWN_ENCS = load_all_encodings()

def refresh_cache():
    global KNOWN_IDS, KNOWN_NAMES, KNOWN_ENCS
    # Clear the existing arrays first to free memory
    if hasattr(KNOWN_ENCS, 'shape') and KNOWN_ENCS.shape[0] > 0:
        del KNOWN_IDS
        del KNOWN_NAMES
        del KNOWN_ENCS
    KNOWN_IDS, KNOWN_NAMES, KNOWN_ENCS = load_all_encodings()
    print(f"Cache refreshed: {len(KNOWN_NAMES)} encodings loaded")

# ------------------ Utilities ------------------
def save_log(user_id, name, status, reason=None, image_path=None):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO logs (user_id, name, status, reason, image_path, ts) VALUES (?,?,?,?,?,?)",
              (user_id, name, status, reason, image_path,
               datetime.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()

def allow_from_esp():
    key = request.headers.get("X-API-Key", "")
    return key == API_KEY

def save_capture_image(raw_bytes):
    # filename: YYYYmmdd_HHMMSS_<hash>.jpg
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    h = hashlib.sha1(raw_bytes).hexdigest()[:8]
    filename = f"{ts}_{h}.jpg"
    path = os.path.join(CAPTURES_DIR, filename)
    with open(path, "wb") as f:
        f.write(raw_bytes)
    return filename, path

def compute_encodings_from_image(image_bgr):
    try:
        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        boxes = face_recognition.face_locations(rgb, model="hog")
        encs = face_recognition.face_encodings(rgb, boxes)
        
        # Explicitly free memory
        del rgb
        return boxes, encs
    except Exception as e:
        print(f"Error in compute_encodings_from_image: {e}")
        return [], []

# ------------------ API for ESP32-CAM ------------------
@app.route("/api/health", methods=["GET"])
def health():
    if not allow_from_esp():
        return jsonify({"status": "unauthorized"}), 401
    return jsonify({"status": "ok", "known": len(KNOWN_NAMES)})

@app.route("/api/rfid", methods=["POST"])
def api_rfid():
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"status": "denied", "reason": "invalid_api_key"}), 403

    data = request.get_json()
    uid = data.get("uid")

    # lookup in DB table `rfid_cards`
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username FROM rfid_cards WHERE uid=?", (uid,))
    row = c.fetchone()
    conn.close()

    if row:
        user = row[0]
        log_access(user, "granted_rfid")
        return jsonify({"status": "granted", "user": user})
    else:
        log_access("unknown", "denied_rfid")
        return jsonify({"status": "denied", "reason": "card_not_found"})


@app.route("/api/recognize", methods=["POST"])
def api_recognize():
    try:
        if not allow_from_esp():
            save_log(None, "Unknown", "denied", "invalid_api_key", None)
            return jsonify({"status": "denied", "reason": "invalid_api_key"}), 401

        raw = request.get_data()
        if not raw:
            save_log(None, "Unknown", "denied", "empty_payload", None)
            return jsonify({"status": "denied", "reason": "empty_payload"}), 400

        # Save capture (for auditing/debug)
        image_file = None
        if SAVE_CAPTURE:
            try:
                image_name, image_path = save_capture_image(raw)
                image_file = image_name
            except Exception as e:
                print(f"Error saving capture: {e}")
                image_file = None

        npimg = np.frombuffer(raw, np.uint8)
        frame = cv2.imdecode(npimg, cv2.IMREAD_COLOR)
        if frame is None:
            save_log(None, "Unknown", "denied", "decode_failed", image_file)
            return jsonify({"status": "denied", "reason": "decode_failed"})

        boxes, encs = compute_encodings_from_image(frame)
        if len(encs) == 0:
            save_log(None, "Unknown", "denied", "no_face", image_file)
            return jsonify({"status": "denied", "reason": "no_face"})

        if KNOWN_ENCS.shape[0] == 0:
            save_log(None, "Unknown", "denied", "db_empty", image_file)
            return jsonify({"status": "denied", "reason": "db_empty"})

        # Compare to known encodings - find the best match across all faces
        best_user = None
        best_dist = 999.0
        best_user_id = None
        
        for enc in encs:
            distances = face_recognition.face_distance(KNOWN_ENCS, enc)
            idx = np.argmin(distances)
            dist = float(distances[idx])
            
            if dist < best_dist:
                best_dist = dist
                best_user = KNOWN_NAMES[idx]
                best_user_id = KNOWN_IDS[idx]

        if best_dist <= TOLERANCE:
            save_log(best_user_id, best_user, "granted", f"dist={best_dist:.3f}", image_file)
            return jsonify({"status": "granted", "user": best_user, "dist": best_dist})
        else:
            save_log(None, "Unknown", "denied", f"no_match_min={best_dist:.3f}", image_file)
            return jsonify({"status": "denied", "reason": "no_match", "min_dist": best_dist})
            
    except Exception as e:
        print(f"Critical error in recognition: {e}")
        # Don't save log to avoid recursive errors
        return jsonify({"status": "denied", "reason": "server_error"}), 500
    
# ------------------ Admin UI ------------------
@app.route("/")
@login_required
def dashboard():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) AS n FROM users")
    users_count = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) AS n FROM encodings")
    enc_count = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) AS n FROM logs")
    logs_count = c.fetchone()["n"]
    c.execute("SELECT name, status, reason, image_path, ts FROM logs ORDER BY id DESC LIMIT 20")
    recent = c.fetchall()
    conn.close()
    return render_template("dashboard.html",
                           users_count=users_count,
                           enc_count=enc_count,
                           logs_count=logs_count,
                           recent=recent)

@app.route("/users")
@login_required
def users():
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT users.id, users.name, COUNT(encodings.id) as encoding_count 
        FROM users 
        LEFT JOIN encodings ON users.id = encodings.user_id 
        GROUP BY users.id 
        ORDER BY users.name ASC
    """)
    users = c.fetchall()
    conn.close()
    return render_template("users.html", users=users)

@app.route("/users/add", methods=["POST"])
@login_required
def users_add():
    name = request.form.get("name", "").strip()
    files = request.files.getlist("files[]")

    if not name or not files:
        flash("Name and at least one image are required", "error")
        return redirect(url_for("users"))

    safe_name = secure_filename(name)
    user_dir = os.path.join(KNOWN_DIR, safe_name)
    os.makedirs(user_dir, exist_ok=True)

    # Insert user if not exists
    conn = db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (name) VALUES (?)", (safe_name,))
        user_id = c.lastrowid
    except sqlite3.IntegrityError:
        c.execute("SELECT id FROM users WHERE name=?", (safe_name,))
        user_id = c.fetchone()["id"]
    
    encoding_count = 0
    processed_files = 0
    
    for file in files:
        if file and file.filename:
            processed_files += 1
            # Save original image
            filename = secure_filename(file.filename)
            img_path = os.path.join(user_dir, filename)
            
            try:
                file.save(img_path)

                # Compute encoding(s) with error handling
                image_bgr = cv2.imread(img_path)
                if image_bgr is None:
                    os.remove(img_path)
                    continue

                # Use try-except to handle face recognition errors
                try:
                    boxes, encs = compute_encodings_from_image(image_bgr)
                except Exception as e:
                    print(f"Error processing face in {filename}: {e}")
                    os.remove(img_path)
                    continue
                    
                if len(encs) == 0:
                    os.remove(img_path)
                    continue

                # Save all encodings from this image
                for enc in encs:
                    enc_bytes = enc.astype(np.float64).tobytes()
                    c.execute("INSERT INTO encodings (user_id, encoding, image_path) VALUES (?,?,?)", 
                             (user_id, enc_bytes, filename))
                    encoding_count += 1
                    
            except Exception as e:
                print(f"Error processing file {filename}: {e}")
                # Clean up failed file
                if os.path.exists(img_path):
                    os.remove(img_path)

    conn.commit()
    conn.close()

    # Refresh in-RAM cache with error handling
    try:
        refresh_cache()
    except Exception as e:
        print(f"Error refreshing cache: {e}")
        flash("User added but cache refresh failed. System may need restart.", "warning")
    
    # Show feedback message
    if encoding_count > 0:
        flash(f"Added {encoding_count} encodings for user '{name}' from {processed_files} files", "success")
    else:
        flash("No valid faces found in the uploaded images", "warning")
        
    return redirect(url_for("users"))



@app.route("/users/delete/<int:user_id>", methods=["POST"])
@login_required
def users_delete(user_id):
    # Remove user, encodings, and optionally images
    conn = db()
    c = conn.cursor()
    c.execute("SELECT name FROM users WHERE id=?", (user_id,))
    row = c.fetchone()
    if row:
        name = row["name"]
        c.execute("DELETE FROM encodings WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        # Remove folder with originals (optional)
        user_dir = os.path.join(KNOWN_DIR, name)
        try:
            if os.path.isdir(user_dir):
                for f in os.listdir(user_dir):
                    os.remove(os.path.join(user_dir, f))
                os.rmdir(user_dir)
        except Exception:
            pass
    conn.close()
    refresh_cache()
    return redirect(url_for("users"))

@app.route("/logs")
@login_required
def logs():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT name, status, reason, image_path, ts FROM logs ORDER BY id DESC LIMIT 200")
    rows = c.fetchall()
    conn.close()
    return render_template("logs.html", rows=rows)

@app.route("/captures/<path:filename>")
@login_required
def captures(filename):
    return send_from_directory(CAPTURES_DIR, filename, as_attachment=False)

def periodic_cache_refresh():
    """Refresh cache every 5 minutes to prevent memory issues"""
    while True:
        time.sleep(300)  # 5 minutes
        try:
            refresh_cache()
            print("Periodic cache refresh completed")
        except Exception as e:
            print(f"Periodic cache refresh failed: {e}")

# Start the background thread (add this after app definition)
if __name__ == "__main__":
    # Start background thread for cache refresh
    refresh_thread = threading.Thread(target=periodic_cache_refresh, daemon=True)
    refresh_thread.start()
    
    app.run(host="0.0.0.0", port=5000, debug=True)

#if __name__ == "__main__":
    # Dev server; for production use gunicorn + systemd
 #   app.run(host="0.0.0.0", port=5000, debug=True)
