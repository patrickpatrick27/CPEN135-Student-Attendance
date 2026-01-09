import cv2
import numpy as np
import face_recognition
from flask import Flask, jsonify, render_template, request, Response, session, redirect, url_for
import sqlite3
import os
import time
import threading
import requests
import urllib.parse
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta

# ================= CONFIGURATION =================
ESP32_IP = "192.168.68.108"  # <--- MAKE SURE THIS MATCHES YOUR LCD
ESP32_STREAM = f"http://{ESP32_IP}:81/stream"

DB = "attendance.db"
UPLOADS = "student_images"
os.makedirs(UPLOADS, exist_ok=True)
SECRET_KEY = "your_secret_key_here"
LATE_THRESHOLD = 15

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ===== Global for MJPEG streaming =====
current_frame = None
frame_lock = threading.Lock()

# ================= DATABASE FUNCTIONS =================
def init_db():
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS professors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE,
                        password_hash TEXT)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS classes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        professor_id INTEGER,
                        name TEXT,
                        day TEXT,
                        start_time TEXT,
                        end_time TEXT)''')
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
                        encoding BLOB)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS class_students (
                        class_id INTEGER,
                        student_number TEXT,
                        FOREIGN KEY(class_id) REFERENCES classes(id),
                        FOREIGN KEY(student_number) REFERENCES students(student_number))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS attendance (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        class_id INTEGER,
                        student_number TEXT,
                        timestamp TEXT,
                        status TEXT)''')
        c.commit()
        
        # Migrations
        cur.execute("PRAGMA table_info(attendance)")
        if 'status' not in [col[1] for col in cur.fetchall()]:
            cur.execute("ALTER TABLE attendance ADD COLUMN status TEXT")
        cur.execute("PRAGMA table_info(students)")
        cols = [col[1] for col in cur.fetchall()]
        if 'program' not in cols: cur.execute("ALTER TABLE students ADD COLUMN program TEXT")
        if 'suffix' not in cols: cur.execute("ALTER TABLE students ADD COLUMN suffix TEXT")
        if 'middle_name' not in cols: cur.execute("ALTER TABLE students ADD COLUMN middle_name TEXT")

def register_professor(username, password):
    password_hash = generate_password_hash(password)
    with sqlite3.connect(DB) as c:
        try:
            c.cursor().execute("INSERT INTO professors (username, password_hash) VALUES (?, ?)", (username, password_hash))
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def add_class(pid, name, day, start, end):
    with sqlite3.connect(DB) as c:
        c.cursor().execute("INSERT INTO classes (professor_id, name, day, start_time, end_time) VALUES (?, ?, ?, ?, ?)", (pid, name, day, start, end))
        c.commit()

def edit_class(cid, pid, name, day, start, end):
    with sqlite3.connect(DB) as c:
        c.cursor().execute("UPDATE classes SET name=?, day=?, start_time=?, end_time=? WHERE id=? AND professor_id=?", (name, day, start, end, cid, pid))
        c.commit()

def delete_class(cid, pid):
    with sqlite3.connect(DB) as c:
        c.cursor().execute("DELETE FROM classes WHERE id=? AND professor_id=?", (cid, pid))
        c.cursor().execute("DELETE FROM class_students WHERE class_id=?", (cid,))
        c.cursor().execute("DELETE FROM attendance WHERE class_id=?", (cid,))
        c.commit()

def save_student(sn, ln, fn, mn, yr, prog, sec, suf, encoding=None):
    name = f"{fn} {mn} {ln} {suf}".strip()
    enc_blob = encoding.tobytes() if encoding is not None else None
    with sqlite3.connect(DB) as c:
        c.cursor().execute('''INSERT OR REPLACE INTO students 
                       (student_number, last_name, first_name, middle_name, year, program, section, suffix, name, encoding) 
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (sn, ln, fn, mn, yr, prog.upper(), sec, suf, name, enc_blob))
        c.commit()

def edit_student(sn, ln, fn, mn, yr, prog, sec, suf):
    name = f"{fn} {mn} {ln} {suf}".strip()
    with sqlite3.connect(DB) as c:
        c.cursor().execute('''UPDATE students SET last_name=?, first_name=?, middle_name=?, year=?, program=?, section=?, suffix=?, name=? 
                       WHERE student_number=?''',
                    (ln, fn, mn, yr, prog.upper(), sec, suf, name, sn))
        c.commit()

def get_all_encodings():
    with sqlite3.connect(DB) as c:
        rows = c.cursor().execute("SELECT student_number, name, encoding FROM students WHERE encoding IS NOT NULL").fetchall()
    ids, names, encs = [], [], []
    for sid, name, blob in rows:
        if blob:
            ids.append(sid)
            names.append(name)
            encs.append(np.frombuffer(blob, dtype=np.float64))
    return ids, names, encs

def get_all_professor_classes(pid):
    with sqlite3.connect(DB) as c:
        return c.cursor().execute("SELECT id, name, day, start_time, end_time FROM classes WHERE professor_id=?", (pid,)).fetchall()

def get_classes_by_day(pid, day):
    with sqlite3.connect(DB) as c:
        return c.cursor().execute("SELECT id, name, start_time, end_time FROM classes WHERE professor_id=? AND day=?", (pid, day)).fetchall()

def get_class_details(cid, pid):
    with sqlite3.connect(DB) as c:
        return c.cursor().execute("SELECT name, start_time, end_time, day FROM classes WHERE id=? AND professor_id=?", (cid, pid)).fetchone()

def get_all_students(year=None, program=None, section=None, name=None, student_number=None, offset=0, limit=None):
    with sqlite3.connect(DB) as c:
        query = "SELECT student_number, first_name, last_name, year, program, section, middle_name, suffix, (encoding IS NOT NULL) FROM students"
        params, conds = [], []
        if year: conds.append("year=?"), params.append(year)
        if program: conds.append("program=?"), params.append(program.upper())
        if section: conds.append("section=?"), params.append(section)
        if name: conds.append("name LIKE ?"), params.append(f"%{name}%")
        if student_number: conds.append("student_number LIKE ?"), params.append(f"%{student_number}%")
        if conds: query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY last_name, first_name"
        if limit is not None: query += " LIMIT ? OFFSET ?"; params += [limit, offset]
        return c.cursor().execute(query, params).fetchall()

def add_students_to_class(cid, sns):
    with sqlite3.connect(DB) as c:
        for sn in sns: c.cursor().execute("INSERT OR IGNORE INTO class_students (class_id, student_number) VALUES (?, ?)", (cid, sn))
        c.commit()

def remove_student_from_class(cid, sn):
    with sqlite3.connect(DB) as c:
        c.cursor().execute("DELETE FROM class_students WHERE class_id=? AND student_number=?", (cid, sn))
        c.cursor().execute("DELETE FROM attendance WHERE class_id=? AND student_number=?", (cid, sn))
        c.commit()

def get_class_students(cid):
    with sqlite3.connect(DB) as c:
        return c.cursor().execute('''SELECT s.student_number, s.name, s.section FROM students s
                       JOIN class_students cs ON s.student_number = cs.student_number
                       WHERE cs.class_id=? ORDER BY s.last_name, s.first_name''', (cid,)).fetchall()

def get_today_attendance(cid):
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB) as c:
        rows = c.cursor().execute("SELECT student_number, status FROM attendance WHERE class_id=? AND timestamp LIKE ?", (cid, f"{today}%")).fetchall()
    return {r[0]: r[1] for r in rows}

def get_student_statuses(cid):
    students = get_class_students(cid)
    attendance = get_today_attendance(cid)
    return {row[0]: {'name': row[1], 'section': row[2], 'status': attendance.get(row[0], 'absent')} for row in students}

def compute_status(start, ts):
    s_dt = datetime.strptime(start, "%H:%M")
    t_dt = datetime.strptime(ts.split(" ")[1], "%H:%M:%S")
    return "on_time" if t_dt <= s_dt + timedelta(minutes=LATE_THRESHOLD) else "late"

def log_attendance(cid, sn, status):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB) as c:
        today = ts.split(" ")[0]
        if not c.cursor().execute("SELECT * FROM attendance WHERE class_id=? AND student_number=? AND timestamp LIKE ?", (cid, sn, f"{today}%")).fetchone():
            c.cursor().execute("INSERT INTO attendance (class_id, student_number, timestamp, status) VALUES (?, ?, ?, ?)", (cid, sn, ts, status))
            c.commit()

# ================= ROBUST FRAME GRABBER (Requests Mode) =================
def grab_frames():
    global current_frame
    bytes_data = b''
    
    while True:
        try:
            print(f"Connecting to ESP32: {ESP32_STREAM}")
            stream = requests.get(ESP32_STREAM, stream=True, timeout=5)
            
            if stream.status_code == 200:
                print("Stream Connected! Receiving data...")
                for chunk in stream.iter_content(chunk_size=1024):
                    bytes_data += chunk
                    a = bytes_data.find(b'\xff\xd8')
                    b = bytes_data.find(b'\xff\xd9')
                    if a != -1 and b != -1:
                        jpg = bytes_data[a:b+2]
                        bytes_data = bytes_data[b+2:]
                        img = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if img is not None:
                            with frame_lock:
                                current_frame = img
            else:
                print(f"Stream Status Code: {stream.status_code}")
                time.sleep(2)
        except Exception as e:
            print(f"Waiting for ESP32... Error: {e}")
            time.sleep(2)

def get_latest_frame():
    with frame_lock:
        return current_frame.copy() if current_frame is not None else None

def generate_mjpeg():
    while True:
        frame = get_latest_frame()
        if frame is None:
            time.sleep(0.05)
            continue
        ret, jpeg = cv2.imencode('.jpg', frame)
        if ret: yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')

# ================= FLASK ROUTES =================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if 'register' in request.form: return redirect(url_for("register"))
        u, p = request.form.get("username"), request.form.get("password")
        with sqlite3.connect(DB) as c:
            row = c.cursor().execute("SELECT id, password_hash FROM professors WHERE username=?", (u,)).fetchone()
        if row and check_password_hash(row[1], p):
            session["professor_id"], session["username"] = row[0], u
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        if register_professor(request.form.get("username"), request.form.get("password")): return redirect(url_for("login"))
        return render_template("register.html", error="Username exists")
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.pop("professor_id", None)
    return redirect(url_for("login"))

@app.route("/")
def index():
    return redirect(url_for("dashboard")) if "professor_id" in session else redirect(url_for("login"))

@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if "professor_id" not in session: return redirect(url_for("login"))
    if request.method == "POST":
        d = request.json
        add_class(session["professor_id"], d.get("name"), d.get("day"), d.get("start_time"), d.get("end_time"))
        return jsonify({"status": "added"})
    
    cid = request.args.get("class_id", type=int)
    classes = get_all_professor_classes(session["professor_id"])
    
    s_date = request.args.get("date")
    s_day = request.args.get("day")
    if s_date: 
        try: s_day = datetime.strptime(s_date, "%Y-%m-%d").strftime("%A")
        except: pass
        
    day_classes = {s_day: get_classes_by_day(session["professor_id"], s_day)} if s_day else {}
    
    sel_class, stats = None, None
    if cid:
        det = get_class_details(cid, session["professor_id"])
        if det:
            sel_class = {'id': cid, 'name': det[0], 'start_time': det[1], 'end_time': det[2], 'day': det[3]}
            stats = get_student_statuses(cid)
            
    return render_template("dashboard.html", all_classes=classes, classes_by_day=day_classes, selected_class=sel_class, students_statuses=stats, username=session.get("username"), selected_day=s_day, selected_date=s_date)

@app.route("/edit_class", methods=["POST"])
def edit_class_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    edit_class(d.get("class_id"), session["professor_id"], d.get("name"), d.get("day"), d.get("start_time"), d.get("end_time"))
    return jsonify({"status": "edited"})

@app.route("/delete_class", methods=["POST"])
def delete_class_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    with sqlite3.connect(DB) as c:
        row = c.cursor().execute("SELECT password_hash FROM professors WHERE id=? AND username=?", (session["professor_id"], d.get("username"))).fetchone()
        if row and check_password_hash(row[0], d.get("password")):
            delete_class(d.get("class_id"), session["professor_id"])
            return jsonify({"status": "deleted"})
        return jsonify({"error": "Invalid credentials"}), 401

@app.route("/remove_student_from_class", methods=["POST"])
def remove_student_from_class_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    with sqlite3.connect(DB) as c:
        row = c.cursor().execute("SELECT password_hash FROM professors WHERE id=? AND username=?", (session["professor_id"], d.get("username"))).fetchone()
        if row and check_password_hash(row[0], d.get("password")):
            remove_student_from_class(d.get("class_id"), d.get("student_number"))
            return jsonify({"status": "removed"})
        return jsonify({"error": "Invalid credentials"}), 401

@app.route("/enroll_global", methods=["POST"])
def enroll_global():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    try:
        save_student(d.get("student_number"), d.get("last_name"), d.get("first_name"), d.get("middle_name"), d.get("year"), d.get("program"), d.get("section"), d.get("suffix"))
        return jsonify({"status": "enrolled"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/edit_student", methods=["POST"])
def edit_student_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    edit_student(d.get("student_number"), d.get("last_name"), d.get("first_name"), d.get("middle_name"), d.get("year"), d.get("program"), d.get("section"), d.get("suffix"))
    return jsonify({"status": "edited"})

@app.route("/capture_global", methods=["GET"])
def capture_global():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    sn = request.args.get("student_number")
    frame = get_latest_frame()
    if frame is None: return jsonify({"error": "No frame"}), 500
    encs = face_recognition.face_encodings(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    if not encs: return jsonify({"error": "No face detected"}), 400
    
    with sqlite3.connect(DB) as c:
        c.cursor().execute("UPDATE students SET encoding=? WHERE student_number=?", (encs[0].tobytes(), sn))
        c.commit()
    cv2.imwrite(os.path.join(UPLOADS, f"{sn}.jpg"), frame)
    return jsonify({"status": "captured", "student_number": sn})

@app.route("/get_students", methods=["GET"])
def get_students():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    rows = get_all_students(request.args.get("year"), request.args.get("program"), request.args.get("section"), request.args.get("name"), request.args.get("student_number"), request.args.get("offset", 0, type=int), request.args.get("limit", 5, type=int))
    total = len(get_all_students(request.args.get("year"), request.args.get("program"), request.args.get("section"), request.args.get("name"), request.args.get("student_number")))
    return jsonify({"students": rows, "total": total})

@app.route("/add_students_to_class", methods=["POST"])
def add_students_to_class_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    add_students_to_class(request.json.get("class_id"), request.json.get("student_numbers", []))
    return jsonify({"status": "added"})

@app.route("/stream")
def stream():
    if "professor_id" not in session: return "Unauthorized", 403
    return Response(generate_mjpeg(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/attendance", methods=["GET"])
def attendance_list():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    with sqlite3.connect(DB) as c:
        return jsonify(c.cursor().execute("SELECT student_number, timestamp, status FROM attendance WHERE class_id=? ORDER BY timestamp DESC LIMIT 50", (request.args.get("class_id"),)).fetchall())

@app.route("/capture_attendance", methods=["GET"])
def capture_attendance():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    cid = request.args.get("class_id", type=int)
    if not cid: return jsonify({"error": "Class ID required"}), 400
    
    frame = get_latest_frame()
    if frame is None: return jsonify({"error": "No frame"}), 500

    encs = face_recognition.face_encodings(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    
    # 1. NO FACE
    if not encs:
        try: requests.get(f"http://{ESP32_IP}/update_lcd?message=No%20Face%20Detected", timeout=0.5)
        except: pass
        return jsonify({"status": "no_face"})

    query = encs[0]
    ids, names, known_encs = get_all_encodings()
    
    # 2. NO STUDENTS ENROLLED
    if not known_encs: return jsonify({"status": "no_known_faces"})

    dists = face_recognition.face_distance(known_encs, query)
    best_idx = int(np.argmin(dists))
    
    # 3. MATCH FOUND
    if dists[best_idx] < 0.5:
        sn = ids[best_idx]
        
        # Check Class Enrollment
        with sqlite3.connect(DB) as c:
            if not c.cursor().execute("SELECT * FROM class_students WHERE class_id=? AND student_number=?", (cid, sn)).fetchone():
                try: requests.get(f"http://{ESP32_IP}/update_lcd?message=Not%20In%20Class", timeout=0.5)
                except: pass
                return jsonify({"status": "not_in_class"})
            
            # Get Class Time
            start_time = c.cursor().execute("SELECT start_time FROM classes WHERE id=?", (cid,)).fetchone()[0]
            
            # --------------------------------------------------------
            # FETCH LAST NAME AND FIRST NAME FOR LCD FORMATTING
            # --------------------------------------------------------
            row = c.cursor().execute("SELECT last_name, first_name FROM students WHERE student_number=?", (sn,)).fetchone()
            if row:
                lname = row[0]
                fname = row[1]
                # Get first word of first name
                fname_short = fname.split()[0] if fname else ""
                lcd_name = f"{lname}, {fname_short}"
            else:
                lcd_name = "Unknown"
            # --------------------------------------------------------

        status = compute_status(start_time, time.strftime("%Y-%m-%d %H:%M:%S"))
        log_attendance(cid, sn, status)

        # UPDATE LCD with Shortened Name
        try:
            msg = f"{urllib.parse.quote(lcd_name)}|{urllib.parse.quote(status.upper())}"
            requests.get(f"http://{ESP32_IP}/update_lcd?message={msg}", timeout=1)
        except: pass

        return jsonify({"status": "match", "student_number": sn, "name": lcd_name, "attendance_status": status})
    
    else:
        # 4. UNKNOWN FACE
        try: requests.get(f"http://{ESP32_IP}/update_lcd?message=Unknown%20Face|Access%20Denied", timeout=0.5)
        except: pass
        return jsonify({"status": "unknown"})

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=grab_frames, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False)