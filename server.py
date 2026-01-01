import cv2
import numpy as np
import face_recognition
from flask import Flask, jsonify, render_template, request, Response, session, redirect, url_for
import sqlite3
import os
import time
import threading
import requests
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta

# ===== Config =====
DB = "attendance.db"
UPLOADS = "student_images"
ESP32_STREAM = "http://192.168.1.81:81/stream"  # Change to your ESP32-CAM IP
os.makedirs(UPLOADS, exist_ok=True)
SECRET_KEY = "your_secret_key_here"  # Change this to a secure random key
LATE_THRESHOLD = 15  # Constant late threshold in minutes

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ===== Global for MJPEG streaming =====
current_frame = None
frame_lock = threading.Lock()

# ===== Database =====
def init_db():
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        # Professors table
        cur.execute('''CREATE TABLE IF NOT EXISTS professors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE,
                        password_hash TEXT
                       )''')
        # Classes table (schedules, removed late_threshold)
        cur.execute('''CREATE TABLE IF NOT EXISTS classes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        professor_id INTEGER,
                        name TEXT,
                        day TEXT,  -- e.g., 'Monday'
                        start_time TEXT,  -- e.g., '09:00'
                        end_time TEXT  -- e.g., '10:30'
                       )''')
        # Students table
        cur.execute('''CREATE TABLE IF NOT EXISTS students (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        student_id TEXT UNIQUE,
                        name TEXT,
                        encoding BLOB
                       )''')
        # Class students (enrollments)
        cur.execute('''CREATE TABLE IF NOT EXISTS class_students (
                        class_id INTEGER,
                        student_id TEXT,
                        FOREIGN KEY(class_id) REFERENCES classes(id),
                        FOREIGN KEY(student_id) REFERENCES students(student_id)
                       )''')
        # Attendance table
        cur.execute('''CREATE TABLE IF NOT EXISTS attendance (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        class_id INTEGER,
                        student_id TEXT,
                        timestamp TEXT,
                        status TEXT  -- 'on_time', 'late', 'absent'
                       )''')
        c.commit()

def register_professor(username, password):
    password_hash = generate_password_hash(password)
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        try:
            cur.execute("INSERT INTO professors (username, password_hash) VALUES (?, ?)",
                        (username, password_hash))
            c.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # Username exists

def add_class(professor_id, name, day, start_time, end_time):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute('''INSERT INTO classes (professor_id, name, day, start_time, end_time)
                       VALUES (?, ?, ?, ?, ?)''',
                    (professor_id, name, day, start_time, end_time))
        c.commit()
        return cur.lastrowid

def save_student_encoding(student_id, name, encoding):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("INSERT OR REPLACE INTO students (student_id, name, encoding) VALUES (?, ?, ?)",
                    (student_id, name, encoding.tobytes()))
        c.commit()

def enroll_student_in_class(class_id, student_id):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("INSERT OR IGNORE INTO class_students (class_id, student_id) VALUES (?, ?)",
                    (class_id, student_id))
        c.commit()

def get_all_encodings():
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("SELECT student_id, name, encoding FROM students")
        rows = cur.fetchall()
    known_ids, known_names, known_encs = [], [], []
    for sid, name, enc_blob in rows:
        if enc_blob:
            enc = np.frombuffer(enc_blob, dtype=np.float64)
            known_ids.append(sid)
            known_names.append(name)
            known_encs.append(enc)
    return known_ids, known_names, known_encs

def get_all_professor_classes(professor_id):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("SELECT id, name, day, start_time, end_time FROM classes WHERE professor_id = ?",
                    (professor_id,))
        rows = cur.fetchall()
    return rows

def get_class_details(class_id, professor_id):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("SELECT name, start_time, end_time FROM classes WHERE id = ? AND professor_id = ?",
                    (class_id, professor_id))
        return cur.fetchone()

def get_class_students(class_id):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute('''SELECT s.student_id, s.name FROM students s
                       JOIN class_students cs ON s.student_id = cs.student_id
                       WHERE cs.class_id = ?''', (class_id,))
        rows = cur.fetchall()
    return {row[0]: row[1] for row in rows}

def get_today_attendance(class_id):
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("SELECT student_id, status FROM attendance WHERE class_id = ? AND timestamp LIKE ?",
                    (class_id, f"{today}%"))
        rows = cur.fetchall()
    return {row[0]: row[1] for row in rows}

def get_student_statuses(class_id):
    students = get_class_students(class_id)
    attendance = get_today_attendance(class_id)
    statuses = {}
    for student_id in students:
        status = attendance.get(student_id, 'absent')
        statuses[student_id] = {
            'name': students[student_id],
            'status': status
        }
    return statuses

def compute_status(class_start, timestamp):
    start_dt = datetime.strptime(class_start, "%H:%M")
    ts_dt = datetime.strptime(timestamp.split(" ")[1], "%H:%M:%S")
    if ts_dt <= start_dt + timedelta(minutes=LATE_THRESHOLD):
        return "on_time"
    else:
        return "late"

def log_attendance(class_id, student_id, status):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        # Check if already logged today to avoid duplicates
        today = ts.split(" ")[0]
        cur.execute("SELECT * FROM attendance WHERE class_id = ? AND student_id = ? AND timestamp LIKE ?",
                    (class_id, student_id, f"{today}%"))
        if cur.fetchone():
            return  # Already logged
        cur.execute("INSERT INTO attendance (class_id, student_id, timestamp, status) VALUES (?, ?, ?, ?)",
                    (class_id, student_id, ts, status))
        c.commit()

# ===== Background Frame Grabber =====
def grab_frames():
    global current_frame
    cap = cv2.VideoCapture(ESP32_STREAM)
    if not cap.isOpened():
        print("Failed to open ESP32 stream!")
        return
    while True:
        ret, frame = cap.read()
        if ret:
            with frame_lock:
                current_frame = frame
        else:
            time.sleep(0.05)

def get_latest_frame():
    with frame_lock:
        if current_frame is None:
            return None
        return current_frame.copy()

# ===== MJPEG Stream Generator =====
def generate_mjpeg():
    while True:
        frame = get_latest_frame()
        if frame is None:
            time.sleep(0.05)
            continue
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        jpg_bytes = jpeg.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpg_bytes + b'\r\n')

# ===== Routes =====
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if 'register' in request.form:
            return redirect(url_for("register"))
        username = request.form.get("username")
        password = request.form.get("password")
        with sqlite3.connect(DB) as c:
            cur = c.cursor()
            cur.execute("SELECT id, password_hash FROM professors WHERE username = ?", (username,))
            row = cur.fetchone()
        if row and check_password_hash(row[1], password):
            session["professor_id"] = row[0]
            session["username"] = username
            return redirect(url_for("dashboard"))
        else:
            return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if register_professor(username, password):
            return redirect(url_for("login"))
        else:
            return render_template("register.html", error="Username already exists")
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.pop("professor_id", None)
    session.pop("username", None)
    return redirect(url_for("login"))

@app.route("/")
def index():
    if "professor_id" not in session:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))

@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if "professor_id" not in session:
        return redirect(url_for("login"))
    class_id = request.args.get("class_id", type=int)
    if request.method == "POST":
        name = request.form.get("name")
        day = request.form.get("day")
        start_time = request.form.get("start_time")
        end_time = request.form.get("end_time")
        add_class(session["professor_id"], name, day, start_time, end_time)
        return redirect(url_for("dashboard"))
    all_classes = get_all_professor_classes(session["professor_id"])
    selected_class = None
    students_statuses = None
    if class_id:
        class_details = get_class_details(class_id, session["professor_id"])
        if class_details:
            selected_class = {'id': class_id, 'name': class_details[0], 'start_time': class_details[1], 'end_time': class_details[2]}
            students_statuses = get_student_statuses(class_id)
    return render_template("dashboard.html", all_classes=all_classes, selected_class=selected_class, students_statuses=students_statuses, username=session.get("username"))

@app.route("/stream")
def stream():
    if "professor_id" not in session:
        return "Unauthorized", 403
    return Response(generate_mjpeg(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/capture_enroll", methods=["GET"])
def capture_enroll():
    if "professor_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403
    class_id = request.args.get("class_id", type=int)
    name = request.args.get("name")
    if not class_id or not name:
        return jsonify({"error": "Class ID and name required"}), 400
    student_id = name.lower().replace(" ", "_")

    frame = get_latest_frame()
    if frame is None:
        return jsonify({"error": "No frame available"}), 500

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    encs = face_recognition.face_encodings(rgb)
    if not encs:
        return jsonify({"error": "No face detected"}), 400

    save_student_encoding(student_id, name, encs[0].astype(np.float64))
    enroll_student_in_class(class_id, student_id)
    path = os.path.join(UPLOADS, f"{student_id}.jpg")
    cv2.imwrite(path, frame)

    return jsonify({"status": "enrolled", "student_id": student_id})

@app.route("/capture_attendance", methods=["GET"])
def capture_attendance():
    if "professor_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403
    class_id = request.args.get("class_id", type=int)
    if not class_id:
        return jsonify({"error": "Class ID required"}), 400
    frame = get_latest_frame()
    if frame is None:
        return jsonify({"error": "No frame available"}), 500

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    encs = face_recognition.face_encodings(rgb)
    if not encs:
        return jsonify({"status": "no_face"})

    query = encs[0]
    known_ids, known_names, known_encs = get_all_encodings()
    if not known_encs:
        return jsonify({"status": "no_known_faces"})

    distances = face_recognition.face_distance(known_encs, query)
    best_idx = int(np.argmin(distances))
    if distances[best_idx] < 0.5:
        student_id = known_ids[best_idx]
        # Check if student in class
        with sqlite3.connect(DB) as c:
            cur = c.cursor()
            cur.execute("SELECT * FROM class_students WHERE class_id = ? AND student_id = ?", (class_id, student_id))
            if not cur.fetchone():
                return jsonify({"status": "not_in_class"})
        # Get class start
        with sqlite3.connect(DB) as c:
            cur = c.cursor()
            cur.execute("SELECT start_time FROM classes WHERE id = ?", (class_id,))
            class_info = cur.fetchone()
        status = compute_status(class_info[0], time.strftime("%Y-%m-%d %H:%M:%S"))
        log_attendance(class_id, student_id, status)
        return jsonify({"status": "match", "student_id": student_id, "name": known_names[best_idx], "attendance_status": status})
    return jsonify({"status": "unknown"})

@app.route("/attendance", methods=["GET"])
def attendance_list():
    if "professor_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403
    class_id = request.args.get("class_id", type=int)
    if not class_id:
        return jsonify({"error": "Class ID required"}), 400
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("SELECT student_id, timestamp, status FROM attendance WHERE class_id = ? ORDER BY timestamp DESC LIMIT 50",
                    (class_id,))
        rows = cur.fetchall()
    return jsonify(rows)

# ===== Main =====
if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=grab_frames, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False)