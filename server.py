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
ESP32_STREAM = "http://192.168.18.59:81/stream"  # Change to your ESP32-CAM IP
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
        # Classes table
        cur.execute('''CREATE TABLE IF NOT EXISTS classes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        professor_id INTEGER,
                        name TEXT,
                        day TEXT,
                        start_time TEXT,
                        end_time TEXT
                       )''')
        # Students table (middle_initial -> middle_name)
        cur.execute('''CREATE TABLE IF NOT EXISTS students (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        student_number TEXT UNIQUE,
                        last_name TEXT,
                        first_name TEXT,
                        middle_name TEXT,
                        year TEXT,
                        program TEXT,
                        section TEXT,
                        suffix TEXT,
                        name TEXT,
                        encoding BLOB
                       )''')
        # Class students
        cur.execute('''CREATE TABLE IF NOT EXISTS class_students (
                        class_id INTEGER,
                        student_number TEXT,
                        FOREIGN KEY(class_id) REFERENCES classes(id),
                        FOREIGN KEY(student_number) REFERENCES students(student_number)
                       )''')
        # Attendance table
        cur.execute('''CREATE TABLE IF NOT EXISTS attendance (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        class_id INTEGER,
                        student_number TEXT,
                        timestamp TEXT,
                        status TEXT
                       )''')
        c.commit()

        # Migrations
        cur.execute("PRAGMA table_info(attendance)")
        columns = [col[1] for col in cur.fetchall()]
        if 'status' not in columns:
            cur.execute("ALTER TABLE attendance ADD COLUMN status TEXT")

        cur.execute("PRAGMA table_info(students)")
        columns = [col[1] for col in cur.fetchall()]
        if 'program' not in columns:
            cur.execute("ALTER TABLE students ADD COLUMN program TEXT")
        if 'suffix' not in columns:
            cur.execute("ALTER TABLE students ADD COLUMN suffix TEXT")
        if 'middle_name' not in columns:
            cur.execute("ALTER TABLE students ADD COLUMN middle_name TEXT")
        if 'middle_initial' in columns:
            cur.execute("ALTER TABLE students RENAME COLUMN middle_initial TO middle_name")
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
            return None

def add_class(professor_id, name, day, start_time, end_time):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute('''INSERT INTO classes (professor_id, name, day, start_time, end_time)
                       VALUES (?, ?, ?, ?, ?)''',
                    (professor_id, name, day, start_time, end_time))
        c.commit()
        return cur.lastrowid

def edit_class(class_id, professor_id, name, day, start_time, end_time):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute('''UPDATE classes SET name = ?, day = ?, start_time = ?, end_time = ? 
                       WHERE id = ? AND professor_id = ?''',
                    (name, day, start_time, end_time, class_id, professor_id))
        c.commit()

def delete_class(class_id, professor_id):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("DELETE FROM classes WHERE id = ? AND professor_id = ?", (class_id, professor_id))
        cur.execute("DELETE FROM class_students WHERE class_id = ?", (class_id,))
        cur.execute("DELETE FROM attendance WHERE class_id = ?", (class_id,))
        c.commit()

def save_student(student_number, last_name, first_name, middle_name, year, program, section, suffix, encoding=None):
    name = f"{first_name} {middle_name} {last_name} {suffix}".strip()
    print("Saving student:", student_number, name)  # Debug
    with sqlite3.connect(DB) as c:
        try:
            cur = c.cursor()
            enc_blob = encoding.tobytes() if encoding is not None else None
            cur.execute('''INSERT OR REPLACE INTO students 
                           (student_number, last_name, first_name, middle_name, year, program, section, suffix, name, encoding) 
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (student_number, last_name, first_name, middle_name, year, program.upper(), section, suffix, name, enc_blob))
            c.commit()
            print("Student saved successfully")  # Debug
        except Exception as e:
            print("Error saving student:", str(e))

def edit_student(student_number, last_name, first_name, middle_name, year, program, section, suffix):
    name = f"{first_name} {middle_name} {last_name} {suffix}".strip()
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute('''UPDATE students SET last_name = ?, first_name = ?, middle_name = ?, year = ?, program = ?, section = ?, suffix = ?, name = ? 
                       WHERE student_number = ?''',
                    (last_name, first_name, middle_name, year, program.upper(), section, suffix, name, student_number))
        c.commit()

def get_all_encodings():
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("SELECT student_number, name, encoding FROM students WHERE encoding IS NOT NULL")
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

def get_classes_by_day(professor_id, day):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("SELECT id, name, start_time, end_time FROM classes WHERE professor_id = ? AND day = ?",
                    (professor_id, day))
        rows = cur.fetchall()
    return rows

def get_class_details(class_id, professor_id):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("SELECT name, start_time, end_time, day FROM classes WHERE id = ? AND professor_id = ?",
                    (class_id, professor_id))
        return cur.fetchone()

def get_all_students(year=None, program=None, section=None, offset=0, limit=None):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        query = "SELECT student_number, first_name, last_name, year, program, section, middle_name, suffix FROM students"
        params = []
        conditions = []
        if year:
            conditions.append("year = ?")
            params.append(year)
        if program:
            conditions.append("program = ?")
            params.append(program.upper())
        if section:
            conditions.append("section = ?")
            params.append(section)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY last_name, first_name"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        cur.execute(query, params)
        rows = cur.fetchall()
        print("Fetched students:", rows)  # Debug
    return rows

def add_students_to_class(class_id, student_numbers):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        for sn in student_numbers:
            cur.execute("INSERT OR IGNORE INTO class_students (class_id, student_number) VALUES (?, ?)",
                        (class_id, sn))
        c.commit()

def remove_student_from_class(class_id, student_number):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("DELETE FROM class_students WHERE class_id = ? AND student_number = ?", (class_id, student_number))
        cur.execute("DELETE FROM attendance WHERE class_id = ? AND student_number = ?", (class_id, student_number))
        c.commit()

def get_class_students(class_id):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute('''SELECT s.student_number, s.name, s.section FROM students s
                       JOIN class_students cs ON s.student_number = cs.student_number
                       WHERE cs.class_id = ? ORDER BY s.last_name, s.first_name''', (class_id,))
        rows = cur.fetchall()
    return rows  # Return list for sorting already applied

def get_today_attendance(class_id):
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("SELECT student_number, status FROM attendance WHERE class_id = ? AND timestamp LIKE ?",
                    (class_id, f"{today}%"))
        rows = cur.fetchall()
    return {row[0]: row[1] for row in rows}

def get_student_statuses(class_id):
    students = get_class_students(class_id)
    attendance = get_today_attendance(class_id)
    statuses = {}
    for row in students:
        student_number, name, section = row
        status = attendance.get(student_number, 'absent')
        statuses[student_number] = {
            'name': name,
            'section': section,
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

def log_attendance(class_id, student_number, status):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        today = ts.split(" ")[0]
        cur.execute("SELECT * FROM attendance WHERE class_id = ? AND student_number = ? AND timestamp LIKE ?",
                    (class_id, student_number, f"{today}%"))
        if cur.fetchone():
            return
        cur.execute("INSERT INTO attendance (class_id, student_number, timestamp, status) VALUES (?, ?, ?, ?)",
                    (class_id, student_number, ts, status))
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
    if request.method == "POST":
        data = request.json
        name = data.get("name")
        day = data.get("day")
        start_time = data.get("start_time")
        end_time = data.get("end_time")
        add_class(session["professor_id"], name, day, start_time, end_time)
        return jsonify({"status": "added"})
    class_id = request.args.get("class_id", type=int)
    selected_date = request.args.get("date")
    selected_day = request.args.get("day")
    all_classes = get_all_professor_classes(session["professor_id"])
    classes_by_day = {}
    if selected_date:
        try:
            dt = datetime.strptime(selected_date, "%Y-%m-%d")
            selected_day = dt.strftime("%A")
        except ValueError:
            selected_day = None
    if selected_day:
        classes_by_day[selected_day] = get_classes_by_day(session["professor_id"], selected_day)
    selected_class = None
    students_statuses = None
    if class_id:
        class_details = get_class_details(class_id, session["professor_id"])
        if class_details:
            selected_class = {'id': class_id, 'name': class_details[0], 'start_time': class_details[1], 'end_time': class_details[2], 'day': class_details[3]}
            students_statuses = get_student_statuses(class_id)
    return render_template("dashboard.html", all_classes=all_classes, classes_by_day=classes_by_day, selected_class=selected_class, 
                           students_statuses=students_statuses, username=session.get("username"), selected_day=selected_day, selected_date=selected_date)

@app.route("/edit_class", methods=["POST"])
def edit_class_route():
    if "professor_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    class_id = data.get("class_id")
    name = data.get("name")
    day = data.get("day")
    start_time = data.get("start_time")
    end_time = data.get("end_time")
    if not class_id:
        return jsonify({"error": "Class ID required"}), 400
    edit_class(class_id, session["professor_id"], name, day, start_time, end_time)
    return jsonify({"status": "edited"})

@app.route("/delete_class", methods=["POST"])
def delete_class_route():
    if "professor_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    class_id = data.get("class_id")
    username = data.get("username")
    password = data.get("password")
    if not class_id:
        return jsonify({"error": "Class ID required"}), 400
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("SELECT password_hash FROM professors WHERE id = ? AND username = ?", (session["professor_id"], username))
        row = cur.fetchone()
        if row and check_password_hash(row[0], password):
            delete_class(class_id, session["professor_id"])
            return jsonify({"status": "deleted"})
        else:
            return jsonify({"error": "Invalid credentials"}), 401

@app.route("/remove_student_from_class", methods=["POST"])
def remove_student_from_class_route():
    if "professor_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    class_id = data.get("class_id")
    student_number = data.get("student_number")
    username = data.get("username")
    password = data.get("password")
    if not class_id or not student_number:
        return jsonify({"error": "Class ID and student number required"}), 400
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("SELECT password_hash FROM professors WHERE id = ? AND username = ?", (session["professor_id"], username))
        row = cur.fetchone()
        if row and check_password_hash(row[0], password):
            remove_student_from_class(class_id, student_number)
            return jsonify({"status": "removed"})
        else:
            return jsonify({"error": "Invalid credentials"}), 401

@app.route("/enroll_global", methods=["POST"])
def enroll_global():
    if "professor_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    print("Received data:", data)  # Debug
    student_number = data.get("student_number")
    last_name = data.get("last_name")
    first_name = data.get("first_name")
    middle_name = data.get("middle_name")
    year = data.get("year")
    program = data.get("program")
    section = data.get("section")
    suffix = data.get("suffix")
    save_student(student_number, last_name, first_name, middle_name, year, program, section, suffix)
    print("Student saved")  # Debug
    return jsonify({"status": "enrolled", "student_number": student_number})

@app.route("/edit_student", methods=["POST"])
def edit_student_route():
    if "professor_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    student_number = data.get("student_number")
    last_name = data.get("last_name")
    first_name = data.get("first_name")
    middle_name = data.get("middle_name")
    year = data.get("year")
    program = data.get("program")
    section = data.get("section")
    suffix = data.get("suffix")
    edit_student(student_number, last_name, first_name, middle_name, year, program, section, suffix)
    return jsonify({"status": "edited"})

@app.route("/capture_global", methods=["GET"])
def capture_global():
    if "professor_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403
    student_number = request.args.get("student_number")
    if not student_number:
        return jsonify({"error": "Student number required"}), 400

    frame = get_latest_frame()
    if frame is None:
        return jsonify({"error": "No frame available"}), 500

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    encs = face_recognition.face_encodings(rgb)
    if not encs:
        return jsonify({"error": "No face detected"}), 400

    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("UPDATE students SET encoding = ? WHERE student_number = ?",
                    (encs[0].tobytes(), student_number))
        c.commit()

    path = os.path.join(UPLOADS, f"{student_number}.jpg")
    cv2.imwrite(path, frame)

    return jsonify({"status": "captured", "student_number": student_number})

@app.route("/capture_enroll", methods=["GET"])
def capture_enroll():
    if "professor_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403
    class_id = request.args.get("class_id", type=int)
    student_number = request.args.get("student_number")
    if not class_id or not student_number:
        return jsonify({"error": "Class ID and student number required"}), 400

    frame = get_latest_frame()
    if frame is None:
        return jsonify({"error": "No frame available"}), 500

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    encs = face_recognition.face_encodings(rgb)
    if not encs:
        return jsonify({"error": "No face detected"}), 400

    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("UPDATE students SET encoding = ? WHERE student_number = ?",
                    (encs[0].tobytes(), student_number))
        c.commit()

    add_students_to_class(class_id, [student_number])
    path = os.path.join(UPLOADS, f"{student_number}.jpg")
    cv2.imwrite(path, frame)

    return jsonify({"status": "enrolled", "student_number": student_number})

@app.route("/get_students", methods=["GET"])
def get_students():
    if "professor_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403
    year = request.args.get("year")
    program = request.args.get("program")
    section = request.args.get("section")
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 5, type=int)
    students = get_all_students(year, program, section, offset, limit)
    total = len(get_all_students(year, program, section))  # For pagination
    return jsonify({"students": students, "total": total})

@app.route("/add_students_to_class", methods=["POST"])
def add_students_to_class_route():
    if "professor_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    class_id = data.get("class_id")
    student_numbers = data.get("student_numbers", [])
    if not class_id:
        return jsonify({"error": "Class ID required"}), 400
    add_students_to_class(class_id, student_numbers)
    return jsonify({"status": "added"})

@app.route("/stream")
def stream():
    if "professor_id" not in session:
        return "Unauthorized", 403
    return Response(generate_mjpeg(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

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
        student_number = known_ids[best_idx]
        with sqlite3.connect(DB) as c:
            cur = c.cursor()
            cur.execute("SELECT * FROM class_students WHERE class_id = ? AND student_number = ?", (class_id, student_number))
            if not cur.fetchone():
                return jsonify({"status": "not_in_class"})
        with sqlite3.connect(DB) as c:
            cur = c.cursor()
            cur.execute("SELECT start_time FROM classes WHERE id = ?", (class_id,))
            class_info = cur.fetchone()
        status = compute_status(class_info[0], time.strftime("%Y-%m-%d %H:%M:%S"))
        log_attendance(class_id, student_number, status)
        return jsonify({"status": "match", "student_number": student_number, "name": known_names[best_idx], "attendance_status": status})
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
        cur.execute("SELECT student_number, timestamp, status FROM attendance WHERE class_id = ? ORDER BY timestamp DESC LIMIT 50",
                    (class_id,))
        rows = cur.fetchall()
    return jsonify(rows)

# ===== Main =====
if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=grab_frames, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False)