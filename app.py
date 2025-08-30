# app.py
import os
import io
import sqlite3
import hashlib
import datetime
from functools import wraps
from flask import (
    Flask, request, redirect, url_for, render_template,
    send_from_directory, jsonify, session, flash, Response, stream_with_context
)
from werkzeug.utils import secure_filename
import numpy as np
import cv2
import face_recognition
import threading
import time
import requests

# ------------------ Config ------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
KNOWN_DIR = os.path.join(DATA_DIR, "known_faces")
CAPTURES_DIR = os.path.join(DATA_DIR, "captures")
DB_PATH = os.path.join(DATA_DIR, "encodings.db")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(KNOWN_DIR, exist_ok=True)
os.makedirs(CAPTURES_DIR, exist_ok=True)

API_KEY = os.getenv("FACE_API_KEY", "cybarry")  # must match ESP32 nodes
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")  # change in prod

# set this to your ESP32-CAM MJPEG stream address
ESP32CAM_URL = os.getenv("ESP32CAM_URL", "http://192.168.0.143:81/stream")

# Recognition thresholds
TOLERANCE = float(os.getenv("FR_TOLERANCE", "0.45"))
SAVE_CAPTURE = True        # save every request frame

# Flask
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change_me_secret")

# ------------------ DB Helpers ------------------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
        uid TEXT UNIQUE NOT NULL,
        user_id INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_encodings_user_id ON encodings(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_rfid_uid ON rfid_cards(uid)")
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

# ------------------ RAM Cache for encodings ------------------
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
        enc_array = np.frombuffer(row["encoding"], dtype=np.float64).copy()
        encs.append(enc_array)
    conn.close()
    if encs:
        try:
            encs = np.vstack(encs)
        except ValueError:
            print("Warning: Encodings inconsistent, resetting cache")
            return [], [], np.empty((0, 128), dtype=np.float64)
    else:
        encs = np.empty((0, 128), dtype=np.float64)
    return ids, names, encs

KNOWN_IDS, KNOWN_NAMES, KNOWN_ENCS = load_all_encodings()

def refresh_cache():
    global KNOWN_IDS, KNOWN_NAMES, KNOWN_ENCS
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

def log_access(name_or_uid, status, reason=None, image_path=None, user_id=None):
    save_log(user_id, str(name_or_uid), status, reason, image_path)

def allow_from_esp():
    key = request.headers.get("X-API-Key", "")
    return key == API_KEY

def save_capture_image(raw_bytes):
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
        del rgb
        return boxes, encs
    except Exception as e:
        print(f"Error in compute_encodings_from_image: {e}")
        return [], []

# ------------------ RFID last-scan cache (for enrollment-by-swipe) ---------------
LAST_RFID = {"uid": None, "ts": 0}
LAST_RFID_LOCK = threading.Lock()

def set_last_rfid(uid):
    with LAST_RFID_LOCK:
        LAST_RFID["uid"] = uid
        LAST_RFID["ts"] = time.time()

def get_last_rfid():
    with LAST_RFID_LOCK:
        return LAST_RFID.copy()

def clear_last_rfid():
    with LAST_RFID_LOCK:
        LAST_RFID["uid"] = None
        LAST_RFID["ts"] = 0

# ------------------ Camera proxy & snapshot endpoints ------------------

@app.route("/camera/stream")
def camera_stream():
    """
    Proxy the ESP32-CAM MJPEG stream to the browser.
    If ESP32CAM_URL is unreachable this returns a small multipart error chunk.
    """
    # Use a small timeout but stream continuously
    try:
        upstream = requests.get(ESP32CAM_URL, stream=True, timeout=(3, 10))
    except Exception as e:
        app.logger.error(f"Could not open upstream camera stream: {e}")
        # return a tiny multipart response with plain text
        def errgen():
            yield b"--frame\r\nContent-Type: text/plain\r\n\r\nCamera not reachable\r\n"
        return Response(stream_with_context(errgen()), mimetype="multipart/x-mixed-replace; boundary=frame")

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=1024):
                if chunk:
                    yield chunk
        except GeneratorExit:
            app.logger.info("Client disconnected from camera stream")
        except Exception as e:
            app.logger.error(f"Error while streaming camera: {e}")
            # Emit a short error chunk
            yield b"--frame\r\nContent-Type: text/plain\r\n\r\nStream error\r\n"

    return Response(stream_with_context(generate()), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/camera/snapshot", methods=["GET"])
def camera_snapshot():
    """
    Grab a single JPEG from the ESP32 MJPEG stream by scanning for JPEG SOI/EOI bytes.
    Returns image/jpeg or JSON error.
    """
    try:
        # Stream a small portion and extract first JPEG
        r = requests.get(ESP32CAM_URL, stream=True, timeout=(3, 10))
    except Exception as e:
        app.logger.error(f"Snapshot upstream connect error: {e}")
        return jsonify({"status": "error", "reason": "upstream_connect", "details": str(e)}), 502

    # Read until we find a full JPEG (start 0xFFD8, end 0xFFD9)
    buf = b""
    start_found = False
    deadline = time.time() + 8.0  # don't hang
    try:
        for chunk in r.iter_content(chunk_size=1024):
            if not chunk:
                continue
            buf += chunk
            if not start_found:
                if b"\xff\xd8" in buf:
                    start_found = True
                    # cut everything before first SOI
                    idx = buf.index(b"\xff\xd8")
                    buf = buf[idx:]
            if start_found:
                if b"\xff\xd9" in buf:
                    idx2 = buf.index(b"\xff\xd9") + 2
                    jpeg = buf[:idx2]
                    # Optionally save the snapshot to captures
                    try:
                        filename, path = save_capture_image(jpeg)
                    except Exception:
                        filename = None
                    return Response(jpeg, mimetype="image/jpeg", headers={
                        "Content-Disposition": f"inline; filename={filename or 'snapshot.jpg'}"
                    })
            if time.time() > deadline:
                break
    except Exception as e:
        app.logger.error(f"Snapshot iteration error: {e}")

    return jsonify({"status": "error", "reason": "no_jpeg_found"}), 504

# ------------------ API for ESP32 nodes (health/recognize/rfid) ------------------
@app.route("/api/health", methods=["GET"])
def health():
    if not allow_from_esp():
        return jsonify({"status": "unauthorized"}), 401
    return jsonify({"status": "ok", "known": len(KNOWN_NAMES)})


@app.route("/api/rfid", methods=["POST"])
def api_rfid():
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"status": "denied", "reason": "invalid_api_key"}), 403

    data = request.get_json(silent=True) or {}
    uid = (data.get("uid") or "").upper().strip()
    if not uid:
        return jsonify({"status": "denied", "reason": "empty_uid"}), 400

    set_last_rfid(uid)
    conn = db()
    c = conn.cursor()
    # check existence
    c.execute("SELECT uid, user_id FROM rfid_cards WHERE uid = ?", (uid,))
    row = c.fetchone()
    if row:
        # find assigned user
        c.execute("""SELECT users.id, users.name
                     FROM rfid_cards LEFT JOIN users ON users.id = rfid_cards.user_id
                     WHERE rfid_cards.uid = ?""", (uid,))
        r2 = c.fetchone()
        if r2 and r2["id"] is not None:
            user_id, user_name = r2["id"], r2["name"]
            # log using consistent status "granted" and reason "rfid"
            save_log(user_id, user_name, "granted", "rfid", None)
            conn.close()
            return jsonify({"status": "granted", "user": user_name})
        else:
            save_log(None, uid, "denied", "card_not_assigned", None)
            conn.close()
            return jsonify({"status": "denied", "reason": "card_not_assigned"})
    else:
        save_log(None, uid, "denied", "card_not_found", None)
        conn.close()
        return jsonify({"status": "denied", "reason": "card_not_found"})



    

    
    
@app.route("/api/rfid/last", methods=["GET"])
@login_required
def api_rfid_last():
    # Polled by the users page to auto-fill the UID field
    info = get_last_rfid()
    # consider it fresh if seen in the last 20s
    fresh = (time.time() - info["ts"] <= 20) and bool(info["uid"])
    return jsonify({"uid": info["uid"], "fresh": fresh, "age": time.time() - info["ts"]})

@app.route("/api/rfid/clear", methods=["POST"])
@login_required
def api_rfid_clear():
    clear_last_rfid()
    return jsonify({"ok": True})

# ------------------ Recognition (unchanged) ------------------
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

        best_user = None
        best_dist = 999.0
        best_user_id = None

        for enc in encs:
            distances = face_recognition.face_distance(KNOWN_ENCS, enc)
            idx = int(np.argmin(distances))
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
        return jsonify({"status": "denied", "reason": "server_error"}), 500

# ------------------ Admin UI (unchanged) ------------------
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

# ... (keep the rest of your user management / rfid pages as before)
# For brevity, I assume the rest of your endpoints (users, users_add, rfid delete, logs) remain unchanged.
# If you want I can paste the entire previous code below this line; the camera additions are complete.

@app.route("/users")
@login_required
def users():
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT users.id, users.name, 
               COUNT(DISTINCT encodings.id) as encoding_count,
               (SELECT COUNT(*) FROM rfid_cards WHERE rfid_cards.user_id = users.id) as rfid_count
        FROM users 
        LEFT JOIN encodings ON users.id = encodings.user_id 
        GROUP BY users.id 
        ORDER BY users.name ASC
    """)
    users = c.fetchall()
    c.execute("""
        SELECT rfid_cards.id, rfid_cards.uid, users.name as user_name
        FROM rfid_cards
        LEFT JOIN users ON rfid_cards.user_id = users.id
        ORDER BY rfid_cards.uid
    """)
    rfid_cards = c.fetchall()
    conn.close()
    return render_template("users.html", users=users, rfid_cards=rfid_cards)

@app.route("/users/add", methods=["POST"])
@login_required
def users_add():
    name = request.form.get("name", "").strip()
    files = request.files.getlist("files[]")
    enroll_face = request.form.get("enroll_face") is not None
    enroll_rfid = request.form.get("enroll_rfid") is not None
    rfid_uid = request.form.get("rfid_uid", "").strip().upper()

    if not name:
        flash("Name is required", "error")
        return redirect(url_for("users"))
    if enroll_face and not files:
        flash("Face images are required for face enrollment", "error")
        return redirect(url_for("users"))
    if enroll_rfid and not rfid_uid:
        flash("RFID UID is required for RFID enrollment", "error")
        return redirect(url_for("users"))

    safe_name = secure_filename(name)
    user_dir = os.path.join(KNOWN_DIR, safe_name)

    conn = db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (name) VALUES (?)", (safe_name,))
        user_id = c.lastrowid
    except sqlite3.IntegrityError:
        c.execute("SELECT id FROM users WHERE name=?", (safe_name,))
        user_id = c.fetchone()["id"]

    encoding_count = 0
    rfid_added = False

    if enroll_face:
        os.makedirs(user_dir, exist_ok=True)
        for file in files:
            if file and file.filename:
                filename = secure_filename(file.filename)
                img_path = os.path.join(user_dir, filename)
                try:
                    file.save(img_path)
                    image_bgr = cv2.imread(img_path)
                    if image_bgr is None:
                        os.remove(img_path)
                        continue
                    boxes, encs = compute_encodings_from_image(image_bgr)
                    if len(encs) == 0:
                        os.remove(img_path)
                        continue
                    for enc in encs:
                        enc_bytes = enc.astype(np.float64).tobytes()
                        c.execute("INSERT INTO encodings (user_id, encoding, image_path) VALUES (?,?,?)",
                                  (user_id, enc_bytes, filename))
                        encoding_count += 1
                except Exception as e:
                    print(f"Error processing file {filename}: {e}")
                    if os.path.exists(img_path):
                        os.remove(img_path)

    if enroll_rfid and rfid_uid:
        try:
            c.execute("DELETE FROM rfid_cards WHERE uid = ?", (rfid_uid,))
            c.execute("INSERT INTO rfid_cards (uid, user_id) VALUES (?, ?)", (rfid_uid, user_id))
            rfid_added = True
            # if this came from a fresh swipe, clear it so it's not reused by mistake
            if get_last_rfid().get("uid") == rfid_uid:
                clear_last_rfid()
        except sqlite3.IntegrityError:
            flash(f"RFID card {rfid_uid} could not be assigned", "error")

    conn.commit()
    conn.close()

    try:
        refresh_cache()
    except Exception as e:
        print(f"Error refreshing cache: {e}")
        flash("User added but cache refresh failed", "warning")

    parts = []
    if encoding_count > 0: parts.append(f"{encoding_count} face encodings")
    if rfid_added: parts.append(f"RFID {rfid_uid}")
    if parts:
        flash(f"User '{name}' created: " + ", ".join(parts), "success")
    else:
        flash("User created but no enrollment data added", "warning")

    return redirect(url_for("users"))

@app.route("/users/delete/<int:user_id>", methods=["POST"])
@login_required
def users_delete(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    flash("User deleted", "success")
    return redirect(url_for("users"))

@app.route("/rfid/delete/<int:rfid_id>", methods=["POST"])
@login_required
def rfid_delete(rfid_id):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM rfid_cards WHERE id = ?", (rfid_id,))
    conn.commit()
    conn.close()
    flash("RFID card deleted", "success")
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
    while True:
        time.sleep(300)
        try:
            refresh_cache()
            print("Periodic cache refresh completed")
        except Exception as e:
            print(f"Periodic cache refresh failed: {e}")

if __name__ == "__main__":
    print("🚀 Starting Access Control Server...")
    refresh_thread = threading.Thread(target=periodic_cache_refresh, daemon=True)
    refresh_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=True)
