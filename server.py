import cv2
import numpy as np
import face_recognition
from flask import Flask, jsonify, render_template, request, Response, session, redirect, url_for, make_response
import sqlite3
import os
import time
import threading
import requests
import urllib.parse
import csv
from io import StringIO
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta

# ================= CONFIGURATION =================
ESP32_IP = "10.98.88.138" 
ESP32_STREAM = f"http://{ESP32_IP}:81/stream"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "attendance.db")
UPLOADS = os.path.join(BASE_DIR, "student_images")

os.makedirs(UPLOADS, exist_ok=True)
SECRET_KEY = "your_secret_key_here"
LATE_THRESHOLD = 15

app = Flask(__name__)
app.secret_key = SECRET_KEY

current_frame = None
frame_lock = threading.Lock()

# ================= DATABASE FUNCTIONS =================
def init_db():
    print(f"Connecting to database at: {DB}")
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS professors (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password_hash TEXT)''')
        
        cur.execute('''CREATE TABLE IF NOT EXISTS classes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, 
                        professor_id INTEGER, 
                        name TEXT, 
                        day TEXT, 
                        start_time TEXT, 
                        end_time TEXT, 
                        start_date TEXT, 
                        end_date TEXT,
                        program TEXT,
                        year TEXT,
                        section TEXT)''')
                        
        cur.execute('''CREATE TABLE IF NOT EXISTS students (id INTEGER PRIMARY KEY AUTOINCREMENT, student_number TEXT UNIQUE, last_name TEXT, first_name TEXT, middle_name TEXT, year TEXT, program TEXT, section TEXT, suffix TEXT, name TEXT, encoding BLOB)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS class_students (class_id INTEGER, student_number TEXT, FOREIGN KEY(class_id) REFERENCES classes(id), FOREIGN KEY(student_number) REFERENCES students(student_number))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS attendance (id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER, student_number TEXT, timestamp TEXT, status TEXT)''')
        c.commit()
        
        # Migrations
        cur.execute("PRAGMA table_info(classes)")
        cols = [col[1] for col in cur.fetchall()]
        if 'start_date' not in cols: cur.execute("ALTER TABLE classes ADD COLUMN start_date TEXT")
        if 'end_date' not in cols: cur.execute("ALTER TABLE classes ADD COLUMN end_date TEXT")
        if 'program' not in cols: cur.execute("ALTER TABLE classes ADD COLUMN program TEXT")
        if 'year' not in cols: cur.execute("ALTER TABLE classes ADD COLUMN year TEXT")
        if 'section' not in cols: cur.execute("ALTER TABLE classes ADD COLUMN section TEXT")

def register_professor(username, password):
    password_hash = generate_password_hash(password)
    with sqlite3.connect(DB) as c:
        try:
            c.cursor().execute("INSERT INTO professors (username, password_hash) VALUES (?, ?)", (username, password_hash))
            c.commit()
            return True
        except sqlite3.IntegrityError: return False

def verify_professor_credentials(username, password):
    with sqlite3.connect(DB) as c:
        row = c.cursor().execute("SELECT password_hash FROM professors WHERE username=?", (username,)).fetchone()
        if row and check_password_hash(row[0], password):
            return True
    return False

def add_class(pid, name, day, start, end, s_date, e_date, program, year, section):
    with sqlite3.connect(DB) as c:
        c.cursor().execute('''INSERT INTO classes (professor_id, name, day, start_time, end_time, start_date, end_date, program, year, section) 
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (pid, name, day, start, end, s_date, e_date, program, year, section))
        c.commit()

def edit_class(cid, pid, name, day, start, end, s_date, e_date, program, year, section):
    with sqlite3.connect(DB) as c:
        c.cursor().execute("UPDATE classes SET name=?, day=?, start_time=?, end_time=?, start_date=?, end_date=?, program=?, year=?, section=? WHERE id=? AND professor_id=?", (name, day, start, end, s_date, e_date, program, year, section, cid, pid))
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
        c.cursor().execute('''INSERT OR REPLACE INTO students (student_number, last_name, first_name, middle_name, year, program, section, suffix, name, encoding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (sn, ln, fn, mn, yr, prog.upper(), sec, suf, name, enc_blob))
        c.commit()

def edit_student(sn, ln, fn, mn, yr, prog, sec, suf):
    name = f"{fn} {mn} {ln} {suf}".strip()
    with sqlite3.connect(DB) as c:
        c.cursor().execute('''UPDATE students SET last_name=?, first_name=?, middle_name=?, year=?, program=?, section=?, suffix=?, name=? WHERE student_number=?''', (ln, fn, mn, yr, prog.upper(), sec, suf, name, sn))
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
        return c.cursor().execute("SELECT id, name, day, start_time, end_time, section, program, year, start_date, end_date FROM classes WHERE professor_id=?", (pid,)).fetchall()

def get_classes_by_day(pid, day):
    with sqlite3.connect(DB) as c:
        return c.cursor().execute("SELECT id, name, start_time, end_time, section FROM classes WHERE professor_id=? AND day=?", (pid, day)).fetchall()

def get_class_details(cid, pid):
    with sqlite3.connect(DB) as c:
        return c.cursor().execute("SELECT name, start_time, end_time, day, program, year, section, start_date, end_date FROM classes WHERE id=? AND professor_id=?", (cid, pid)).fetchone()

def count_all_students(year=None, program=None, section=None, search_type=None, search_val=None, is_irregular=False):
    with sqlite3.connect(DB) as c:
        query = "SELECT COUNT(*) FROM students"
        params, conds = [], []
        if year: conds.append("year=?"), params.append(year)
        if program: conds.append("program=?"), params.append(program.upper())
        if section: conds.append("section LIKE ?"), params.append(f"%{section}%")
        if is_irregular: conds.append("section LIKE '%IRREG%'")
        if search_val:
            if search_type == 'first_name': conds.append("first_name LIKE ?"), params.append(f"%{search_val}%")
            elif search_type == 'last_name': conds.append("last_name LIKE ?"), params.append(f"%{search_val}%")
            elif search_type == 'student_number': conds.append("student_number LIKE ?"), params.append(f"%{search_val}%")
        if conds: query += " WHERE " + " AND ".join(conds)
        return c.cursor().execute(query, params).fetchone()[0]

def get_all_students(year=None, program=None, section=None, search_type=None, search_val=None, is_irregular=False, offset=0, limit=None):
    with sqlite3.connect(DB) as c:
        query = "SELECT student_number, first_name, last_name, year, program, section, middle_name, suffix, (encoding IS NOT NULL) FROM students"
        params, conds = [], []
        if year: conds.append("year=?"), params.append(year)
        if program: conds.append("program=?"), params.append(program.upper())
        if section: conds.append("section LIKE ?"), params.append(f"%{section}%")
        if is_irregular: conds.append("section LIKE '%IRREG%'")
        if search_val:
            if search_type == 'first_name': conds.append("first_name LIKE ?"), params.append(f"%{search_val}%")
            elif search_type == 'last_name': conds.append("last_name LIKE ?"), params.append(f"%{search_val}%")
            elif search_type == 'student_number': conds.append("student_number LIKE ?"), params.append(f"%{search_val}%")
        if conds: query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY last_name, first_name"
        if limit is not None: query += " LIMIT ? OFFSET ?"; params += [limit, offset]
        return c.cursor().execute(query, params).fetchall()

def add_students_to_class(cid, sns):
    with sqlite3.connect(DB) as c:
        for sn in sns: c.cursor().execute("INSERT OR IGNORE INTO class_students (class_id, student_number) VALUES (?, ?)", (cid, sn))
        c.commit()

def import_section_students(cid):
    with sqlite3.connect(DB) as c:
        cls = c.cursor().execute("SELECT program, year, section FROM classes WHERE id=?", (cid,)).fetchone()
        if not cls: return 0
        prog, yr, sec = cls
        students = c.cursor().execute("SELECT student_number FROM students WHERE program=? AND year=? AND section=?", (prog, yr, sec)).fetchall()
        count = 0
        for s in students:
            c.cursor().execute("INSERT OR IGNORE INTO class_students (class_id, student_number) VALUES (?, ?)", (cid, s[0]))
            count += 1
        c.commit()
        return count

def remove_student_from_class(cid, sn):
    with sqlite3.connect(DB) as c:
        c.cursor().execute("DELETE FROM class_students WHERE class_id=? AND student_number=?", (cid, sn))
        c.cursor().execute("DELETE FROM attendance WHERE class_id=? AND student_number=?", (cid, sn))
        c.commit()

def get_class_students_with_details(cid):
    with sqlite3.connect(DB) as c:
        return c.cursor().execute('''
            SELECT s.student_number, s.last_name, s.first_name, s.middle_name, s.section 
            FROM students s
            JOIN class_students cs ON s.student_number = cs.student_number
            WHERE cs.class_id=? 
            ORDER BY s.last_name, s.first_name''', (cid,)).fetchall()

def log_attendance(cid, sn, status, specific_date=None):
    if specific_date:
        ts = f"{specific_date} {datetime.now().strftime('%H:%M:%S')}"
    else:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        
    today = ts.split(" ")[0]
    
    with sqlite3.connect(DB) as c:
        existing = c.cursor().execute("SELECT * FROM attendance WHERE class_id=? AND student_number=? AND timestamp LIKE ?", (cid, sn, f"{today}%")).fetchone()
        if existing:
            c.cursor().execute("UPDATE attendance SET timestamp=?, status=? WHERE class_id=? AND student_number=? AND timestamp LIKE ?", (ts, status, cid, sn, f"{today}%"))
            c.commit()
        else:
            c.cursor().execute("INSERT INTO attendance (class_id, student_number, timestamp, status) VALUES (?, ?, ?, ?)", (cid, sn, ts, status))
            c.commit()

def get_attendance_by_date(cid, target_date):
    if not target_date: target_date = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB) as c:
        rows = c.cursor().execute("SELECT student_number, status FROM attendance WHERE class_id=? AND timestamp LIKE ?", (cid, f"{target_date}%")).fetchall()
    return {r[0]: r[1] for r in rows}

def get_student_statuses(cid, target_date=None):
    students = get_class_students_with_details(cid)
    attendance = get_attendance_by_date(cid, target_date)
    results = {}
    for row in students:
        sn, ln, fn, mn, section = row
        mi = f" {mn[0]}." if mn and len(mn) > 0 else ""
        formatted_name = f"{ln}, {fn}{mi}"
        status = attendance.get(sn, 'absent')
        results[sn] = {'name': formatted_name, 'section': section, 'status': status}
    return results

def compute_status(start, ts):
    s_dt = datetime.strptime(start, "%H:%M")
    t_dt = datetime.strptime(ts.split(" ")[1], "%H:%M:%S")
    return "on_time" if t_dt <= s_dt + timedelta(minutes=LATE_THRESHOLD) else "late"

# --- HELPER: Generate all dates for a class ---
def get_class_dates(day_name, start_date_str, end_date_str):
    """Generates all dates for a class between start and end date (or today)."""
    dates = []
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        limit = min(end, today)
        
        target_weekday = time.strptime(day_name, "%A").tm_wday
        
        curr = start
        while curr <= limit:
            if curr.weekday() == target_weekday:
                dates.append(curr.strftime("%Y-%m-%d"))
            curr += timedelta(days=1)
    except Exception as e:
        print(f"Error generating dates: {e}")
    return dates

def find_nearest_class_date(day_name, start_date_str, end_date_str):
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        target_weekday = time.strptime(day_name, "%A").tm_wday
        delta = timedelta(days=1)
        candidates = []
        curr = start
        while curr <= end:
            if curr.weekday() == target_weekday:
                candidates.append(curr)
            curr += delta
        if not candidates:
            return None
        closest = min(candidates, key=lambda d: abs((d - today).days))
        return closest.strftime("%Y-%m-%d")
    except:
        return None

def grab_frames():
    global current_frame
    bytes_data = b''
    while True:
        try:
            stream = requests.get(ESP32_STREAM, stream=True, timeout=5)
            if stream.status_code == 200:
                for chunk in stream.iter_content(chunk_size=1024):
                    bytes_data += chunk
                    a = bytes_data.find(b'\xff\xd8'); b = bytes_data.find(b'\xff\xd9')
                    if a != -1 and b != -1:
                        jpg = bytes_data[a:b+2]; bytes_data = bytes_data[b+2:]
                        img = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if img is not None:
                            with frame_lock: current_frame = img
            else: time.sleep(2)
        except: time.sleep(2)

def get_latest_frame():
    with frame_lock: return current_frame.copy() if current_frame is not None else None

def generate_mjpeg():
    while True:
        frame = get_latest_frame()
        if frame is None: time.sleep(0.05); continue
        ret, jpeg = cv2.imencode('.jpg', frame)
        if ret: yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')

# ================= ROUTES =================
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
def logout(): session.pop("professor_id", None); return redirect(url_for("login"))

@app.route("/")
def index(): return redirect(url_for("dashboard")) if "professor_id" in session else redirect(url_for("login"))

@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if "professor_id" not in session: return redirect(url_for("login"))
    if request.method == "POST":
        d = request.json
        add_class(session["professor_id"], d.get("name"), d.get("day"), d.get("start_time"), d.get("end_time"), 
                  d.get("start_date"), d.get("end_date"), d.get("program"), d.get("year"), d.get("section"))
        return jsonify({"status": "added"})
    
    cid = request.args.get("class_id", type=int)
    classes = get_all_professor_classes(session["professor_id"])
    s_date = request.args.get("date", None)
    s_day = datetime.strptime(s_date, "%Y-%m-%d").strftime("%A") if s_date else None
    day_classes = {s_day: get_classes_by_day(session["professor_id"], s_day)} if s_day else {}
    
    sel_class, stats = None, None
    present, late, absent = 0, 0, 0
    error_msg = None
    today = datetime.now().strftime("%Y-%m-%d")
    if cid:
        det = get_class_details(cid, session["professor_id"])
        if det:
            sel_class = {'id': cid, 'name': det[0], 'start_time': det[1], 'end_time': det[2], 'day': det[3], 'program': det[4], 'year': det[5], 'section': det[6], 'start_date': det[7], 'end_date': det[8]}
            if not s_date:
                s_date = find_nearest_class_date(sel_class['day'], sel_class['start_date'], sel_class['end_date'])
            if s_date:
                try:
                    s_datetime = datetime.strptime(s_date, "%Y-%m-%d")
                    start_dt = datetime.strptime(sel_class['start_date'], "%Y-%m-%d")
                    end_dt = datetime.strptime(sel_class['end_date'], "%Y-%m-%d")
                    if start_dt <= s_datetime <= end_dt and s_datetime.strftime("%A") == sel_class['day']:
                        stats = get_student_statuses(cid, s_date)
                        present = sum(1 for v in stats.values() if v['status'] == 'on_time')
                        late = sum(1 for v in stats.values() if v['status'] == 'late')
                        absent = sum(1 for v in stats.values() if v['status'] == 'absent')
                    else:
                        error_msg = "Invalid date for this class. No attendance tracked."
                except ValueError:
                    error_msg = "Invalid date format."
            
    return render_template("dashboard.html", all_classes=classes, classes_by_day=day_classes, selected_class=sel_class, students_statuses=stats, username=session.get("username"), selected_day=s_day, selected_date=s_date, present=present, late=late, absent=absent, today=today, error_msg=error_msg)

@app.route("/edit_class", methods=["POST"])
def edit_class_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    edit_class(d.get("class_id"), session["professor_id"], d.get("name"), d.get("day"), d.get("start_time"), d.get("end_time"), d.get("start_date"), d.get("end_date"), d.get("program"), d.get("year"), d.get("section"))
    return jsonify({"status": "edited"})

@app.route("/delete_class", methods=["POST"])
def delete_class_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    if d.get("username") != session["username"] or not verify_professor_credentials(d.get("username"), d.get("password")):
        return jsonify({"error": "Invalid credentials"}), 401
    delete_class(d.get("class_id"), session["professor_id"])
    return jsonify({"status": "deleted"})

@app.route("/remove_student_from_class", methods=["POST"])
def remove_student_from_class_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    if d.get("username") != session["username"] or not verify_professor_credentials(d.get("username"), d.get("password")):
        return jsonify({"error": "Invalid credentials"}), 401
    remove_student_from_class(d.get("class_id"), d.get("student_number"))
    return jsonify({"status": "removed"})

@app.route("/manual_attendance", methods=["POST"])
def manual_attendance_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    if d.get("username") != session["username"] or not verify_professor_credentials(d.get("username"), d.get("password")):
        return jsonify({"error": "Invalid credentials"}), 401
    cid = d.get("class_id")
    sn = d.get("student_number")
    date = d.get("date") or datetime.now().strftime("%Y-%m-%d")
    if not cid or not sn or not date:
        return jsonify({"error": "Missing parameters"}), 400
    with sqlite3.connect(DB) as c:
        if not c.cursor().execute("SELECT * FROM class_students WHERE class_id=? AND student_number=?", (cid, sn)).fetchone():
            return jsonify({"error": "Student not in class"}), 400
    today = datetime.now().strftime("%Y-%m-%d")
    if date == today:
        with sqlite3.connect(DB) as c:
            start_time = c.cursor().execute("SELECT start_time FROM classes WHERE id=?", (cid,)).fetchone()[0]
        status = compute_status(start_time, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    else:
        status = "on_time"
    log_attendance(cid, sn, status, date)
    return jsonify({"status": "marked"})

@app.route("/clear_attendance", methods=["POST"])
def clear_attendance_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    if d.get("username") != session["username"] or not verify_professor_credentials(d.get("username"), d.get("password")):
        return jsonify({"error": "Invalid credentials"}), 401
    cid = d.get("class_id")
    sn = d.get("student_number")
    date = d.get("date") or datetime.now().strftime("%Y-%m-%d")
    if not cid or not sn or not date:
        return jsonify({"error": "Missing parameters"}), 400
    with sqlite3.connect(DB) as c:
        c.cursor().execute("DELETE FROM attendance WHERE class_id=? AND student_number=? AND timestamp LIKE ?", (cid, sn, f"{date}%"))
        c.commit()
    return jsonify({"status": "cleared"})

@app.route("/enroll_global", methods=["POST"])
def enroll_global():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    try:
        save_student(d.get("student_number"), d.get("last_name"), d.get("first_name"), d.get("middle_name"), d.get("year"), d.get("program"), d.get("section"), d.get("suffix"))
        return jsonify({"status": "enrolled", "student_number": d.get("student_number")})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/edit_student", methods=["POST"])
def edit_student_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    d = request.json
    edit_student(d.get("student_number"), d.get("last_name"), d.get("first_name"), d.get("middle_name"), d.get("year"), d.get("program"), d.get("section"), d.get("suffix"))
    return jsonify({"status": "edited"})

@app.route("/upload_face", methods=["POST"])
def upload_face():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    sn = request.form.get("student_number")
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "No selected file"}), 400
    if file and file.filename.lower().endswith(('.jpg', '.jpeg')):
        img = cv2.imdecode(np.frombuffer(file.read(), np.uint8), cv2.IMREAD_COLOR)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        encs = face_recognition.face_encodings(rgb)
        if len(encs) != 1: return jsonify({"error": "Exactly one face must be detected"}), 400
        with sqlite3.connect(DB) as c:
            c.cursor().execute("UPDATE students SET encoding=? WHERE student_number=?", (encs[0].tobytes(), sn))
            c.commit()
        cv2.imwrite(os.path.join(UPLOADS, f"{sn}.jpg"), img)
        return jsonify({"status": "uploaded"})
    return jsonify({"error": "Invalid file"}), 400

@app.route("/get_students", methods=["GET"])
def get_students():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    y, p, s, n, sn = request.args.get("year"), request.args.get("program"), request.args.get("section"), request.args.get("name"), request.args.get("student_number")
    off, lim = request.args.get("offset", 0, type=int), request.args.get("limit", 10, type=int)
    search_type = request.args.get("search_type")
    search_val = request.args.get("search_val")
    is_irregular = request.args.get("is_irregular") == 'true'
    rows = get_all_students(y, p, s, search_type, search_val, is_irregular, off, lim)
    total = count_all_students(y, p, s, search_type, search_val, is_irregular)
    return jsonify({"students": rows, "total": total})

@app.route("/add_students_to_class", methods=["POST"])
def add_students_to_class_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    add_students_to_class(request.json.get("class_id"), request.json.get("student_numbers", []))
    return jsonify({"status": "added"})

@app.route("/import_section_students", methods=["POST"])
def import_section_students_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    cid = request.json.get("class_id")
    count = import_section_students(cid)
    return jsonify({"status": "imported", "count": count})

@app.route("/stream")
def stream():
    if "professor_id" not in session: return "Unauthorized", 403
    return Response(generate_mjpeg(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/attendance", methods=["GET"])
def attendance_list():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    with sqlite3.connect(DB) as c:
        return jsonify(c.cursor().execute("SELECT s.name, a.timestamp, a.status FROM attendance a JOIN students s ON a.student_number = s.student_number WHERE class_id=? ORDER BY a.timestamp DESC LIMIT 50", (request.args.get("class_id"),)).fetchall())

@app.route("/capture_attendance", methods=["GET"])
def capture_attendance():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    cid = request.args.get("class_id", type=int)
    if not cid: return jsonify({"error": "Class ID required"}), 400
    frame = get_latest_frame()
    if frame is None: return jsonify({"error": "No frame"}), 500

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    encs = face_recognition.face_encodings(rgb)
    if not encs:
        try: requests.get(f"http://{ESP32_IP}/update_lcd?message=No%20Face%20Detected", timeout=0.5)
        except: pass
        return jsonify({"status": "no_face"})

    query = encs[0]
    ids, names, known_encs = get_all_encodings()
    if not known_encs: return jsonify({"status": "no_known_faces"})

    dists = face_recognition.face_distance(known_encs, query)
    best_idx = int(np.argmin(dists))
    min_dist = dists[best_idx]

    if min_dist < 0.65:
        sn = ids[best_idx]
        with sqlite3.connect(DB) as c:
            if not c.cursor().execute("SELECT * FROM class_students WHERE class_id=? AND student_number=?", (cid, sn)).fetchone():
                try: requests.get(f"http://{ESP32_IP}/update_lcd?message=Not%20In%20Class", timeout=0.5)
                except: pass
                return jsonify({"status": "not_in_class"})
            start_time = c.cursor().execute("SELECT start_time FROM classes WHERE id=?", (cid,)).fetchone()[0]
            row = c.cursor().execute("SELECT last_name, first_name FROM students WHERE student_number=?", (sn,)).fetchone()
            class_row = c.cursor().execute("SELECT name, section FROM classes WHERE id=?", (cid,)).fetchone()
            if row:
                lname = row[0] if row[0] else ""
                fname = row[1] if row[1] else ""
                fname_short = fname.split()[0] if fname else ""
                lcd_name = f"{lname}, {fname_short}"
            else:
                lcd_name = "Unknown"
            if class_row:
                cls_name = class_row[0] if class_row[0] else ""
                cls_section = class_row[1] if class_row[1] else ""
                lcd_class = f"{cls_name} {cls_section}"
            else:
                lcd_class = "Unknown"

        status = compute_status(start_time, time.strftime("%Y-%m-%d %H:%M:%S"))
        if status == "on_time":
            lcd_status = "On Time"
        elif status == "late":
            lcd_status = "Late"
        else:
            lcd_status = "Absent"
        log_attendance(cid, sn, status)
        try:
            msg = f"{urllib.parse.quote(lcd_name)}|{urllib.parse.quote(lcd_class)}|{urllib.parse.quote(lcd_status)}"
            requests.get(f"http://{ESP32_IP}/update_lcd?message={msg}", timeout=1)
        except: pass
        return jsonify({"status": "match", "student_number": sn, "name": lcd_name, "attendance_status": status})
    else:
        try: requests.get(f"http://{ESP32_IP}/update_lcd?message=Unknown%20Face|Access%20Denied", timeout=0.5)
        except: pass
        return jsonify({"status": "unknown"})

@app.route("/capture_global", methods=["GET"])
def capture_global():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    sn = request.args.get("student_number")
    if not sn: return jsonify({"error": "Student number required"}), 400
    frame = get_latest_frame()
    if frame is None: return jsonify({"error": "No frame"}), 500
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    encs = face_recognition.face_encodings(rgb)
    if len(encs) != 1: return jsonify({"error": "Exactly one face must be detected"}), 400
    enc = encs[0]
    with sqlite3.connect(DB) as c:
        c.cursor().execute("UPDATE students SET encoding=? WHERE student_number=?", (enc.tobytes(), sn))
        c.commit()
    cv2.imwrite(os.path.join(UPLOADS, f"{sn}.jpg"), frame)
    return jsonify({"status": "updated"})

# ================= NEW EXPORT FUNCTIONS =================

@app.route("/export_session")
def export_session():
    if "professor_id" not in session: return "Unauthorized", 403
    cid = request.args.get("class_id")
    t_date = request.args.get("date")
    
    si = StringIO()
    cw = csv.writer(si)
    
    with sqlite3.connect(DB) as c:
        cls = c.cursor().execute("SELECT name, section, program, year FROM classes WHERE id=?", (cid,)).fetchone()
        c_name = cls[0] if cls else "Unknown Class"
        
        cw.writerow([f"Class: {c_name}"])
        cw.writerow([f"Date: {t_date}"])
        cw.writerow([])
        cw.writerow(["Student Number", "Last Name", "First Name", "Middle Name", "Status", "Time In"])
        
        students = get_class_students_with_details(cid)
        # Fetch detailed time for this date
        att_rows = c.cursor().execute("SELECT student_number, status, timestamp FROM attendance WHERE class_id=? AND timestamp LIKE ?", (cid, f"{t_date}%")).fetchall()
        att_map = {r[0]: {'status': r[1], 'time': r[2].split(" ")[1]} for r in att_rows}
        
        for row in students:
            sn, ln, fn, mn, _ = row
            record = att_map.get(sn, {'status': 'ABSENT', 'time': '-'})
            cw.writerow([sn, ln, fn, mn, record['status'].upper(), record['time']])
            
    out = make_response(si.getvalue())
    out.headers["Content-Disposition"] = f"attachment; filename=Attendance_{t_date}_{c_name}.csv"
    out.headers["Content-type"] = "text/csv"
    return out

@app.route("/export_course")
def export_course():
    """Exports a matrix of all attendance data for a specific class (Dates vs Students)."""
    if "professor_id" not in session: return "Unauthorized", 403
    cid = request.args.get("class_id")
    si = StringIO()
    cw = csv.writer(si)
    
    with sqlite3.connect(DB) as c:
        # Get Class Info
        cls = c.cursor().execute("SELECT name, day, start_date, end_date, start_time, end_time, section, program, year FROM classes WHERE id=?", (cid,)).fetchone()
        if not cls: return "Class not found", 404
        
        c_name, c_day, c_start_date, c_end_date, c_start_time, c_end_time, c_sec, c_prog, c_yr = cls
        
        # Header Info
        cw.writerow([f"Course: {c_name}"])
        cw.writerow([f"Section: {c_yr} {c_prog} {c_sec}"])
        cw.writerow([f"Schedule: {c_day} {c_start_time}-{c_end_time}"])
        cw.writerow([])
        
        # Generate all class dates from start date until today
        dates = get_class_dates(c_day, c_start_date, c_end_date)
        
        # Headers: Student Info + Dates + Summary
        headers = ["Student No.", "Name"] + dates + ["Present", "Late", "Absent", "Rate (%)"]
        cw.writerow(headers)
        
        # Get Students
        students = get_class_students_with_details(cid)
        
        # Get All Attendance for this class
        att_rows = c.cursor().execute("SELECT student_number, timestamp, status FROM attendance WHERE class_id=?", (cid,)).fetchall()
        
        # Map: student_no -> date -> status
        att_map = {}
        for sn, ts, status in att_rows:
            date_str = ts.split(" ")[0] # Extract YYYY-MM-DD
            if sn not in att_map: att_map[sn] = {}
            att_map[sn][date_str] = status
            
        # Build Rows
        for stud in students:
            sn, ln, fn, mn, _ = stud
            mi = f" {mn[0]}." if mn and len(mn) > 0 else ""
            full_name = f"{ln}, {fn}{mi}"
            
            row = [sn, full_name]
            p_count, l_count, a_count = 0, 0, 0
            
            for d in dates:
                status = att_map.get(sn, {}).get(d, None)
                if status == 'on_time':
                    row.append("P")
                    p_count += 1
                elif status == 'late':
                    row.append("L")
                    l_count += 1
                else:
                    # Absent if no record found for a past date
                    row.append("A")
                    a_count += 1
            
            total_days = len(dates)
            rate = ((p_count + l_count) / total_days * 100) if total_days > 0 else 0.0
            
            row.append(p_count)
            row.append(l_count)
            row.append(a_count)
            row.append(f"{rate:.2f}")
            
            cw.writerow(row)
            
    out = make_response(si.getvalue())
    out.headers["Content-Disposition"] = f"attachment; filename=Summary_{c_name}_{datetime.now().strftime('%Y%m%d')}.csv"
    out.headers["Content-type"] = "text/csv"
    return out

@app.route("/export_all_students")
def export_all_students():
    """Exports the master list of all students in the database."""
    if "professor_id" not in session: return "Unauthorized", 403
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["Student Number", "Last Name", "First Name", "Middle Name", "Suffix", "Program", "Year", "Section", "Has Face Data"])
    
    with sqlite3.connect(DB) as c:
        rows = c.cursor().execute("SELECT student_number, last_name, first_name, middle_name, suffix, program, year, section, (encoding IS NOT NULL) FROM students ORDER BY last_name, first_name").fetchall()
        for r in rows:
            cw.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], "Yes" if r[8] else "No"])
            
    out = make_response(si.getvalue())
    out.headers["Content-Disposition"] = f"attachment; filename=Master_Student_List_{datetime.now().strftime('%Y%m%d')}.csv"
    out.headers["Content-type"] = "text/csv"
    return out

# ================= FIXED: EXPORT ALL ATTENDANCE MATRIX =================
@app.route("/export_all_attendance")
def export_all_attendance():
    """
    Exports ALL classes in a single CSV file using the Stacked Matrix format.
    Format:
    Class A Block (Headers, Dates, Students)
    [Blank Rows]
    Class B Block (Headers, Dates, Students)
    """
    if "professor_id" not in session: return "Unauthorized", 403
    si = StringIO()
    cw = csv.writer(si)

    pid = session["professor_id"]
    
    with sqlite3.connect(DB) as c:
        # Get all classes for the professor
        classes = c.cursor().execute("SELECT id, name, day, start_date, end_date, start_time, end_time, section, program, year FROM classes WHERE professor_id=?", (pid,)).fetchall()
        
        for cls in classes:
            cid, c_name, c_day, c_start_date, c_end_date, c_start_time, c_end_time, c_sec, c_prog, c_yr = cls
            
            # --- BLOCK HEADER ---
            cw.writerow([f"Course: {c_name}"])
            cw.writerow([f"Section: {c_yr} {c_prog} {c_sec}"])
            cw.writerow([f"Schedule: {c_day} {c_start_time}-{c_end_time}"])
            cw.writerow([]) # Empty row for spacing
            
            # --- CALCULATE COLUMNS (DATES) ---
            dates = get_class_dates(c_day, c_start_date, c_end_date)
            
            # --- TABLE HEADER ---
            # Columns: Student No | Name | [Date 1] | [Date 2] | ... | Present | Late | Absent | Rate (%)
            headers = ["Student No.", "Name"] + dates + ["Present", "Late", "Absent", "Rate (%)"]
            cw.writerow(headers)
            
            # --- FETCH DATA ---
            students = get_class_students_with_details(cid)
            att_rows = c.cursor().execute("SELECT student_number, timestamp, status FROM attendance WHERE class_id=?", (cid,)).fetchall()
            
            # Map: student_no -> date -> status
            att_map = {}
            for sn, ts, status in att_rows:
                date_str = ts.split(" ")[0] # Extract YYYY-MM-DD
                if sn not in att_map: att_map[sn] = {}
                att_map[sn][date_str] = status
            
            # --- BUILD STUDENT ROWS ---
            for stud in students:
                sn, ln, fn, mn, _ = stud
                mi = f" {mn[0]}." if mn and len(mn) > 0 else ""
                full_name = f"{ln}, {fn}{mi}"
                
                row = [sn, full_name]
                p_count, l_count, a_count = 0, 0, 0
                
                # Loop through every valid class date for this semester
                for d in dates:
                    status = att_map.get(sn, {}).get(d, None)
                    if status == 'on_time':
                        row.append("P")
                        p_count += 1
                    elif status == 'late':
                        row.append("L")
                        l_count += 1
                    else:
                        row.append("A")
                        a_count += 1
                
                total_days = len(dates)
                rate = ((p_count + l_count) / total_days * 100) if total_days > 0 else 0.0
                
                # Add Summary Columns
                row.append(p_count)
                row.append(l_count)
                row.append(a_count)
                row.append(f"{rate:.2f}")
                
                cw.writerow(row)
            
            # --- SEPARATOR BETWEEN CLASSES ---
            cw.writerow([])
            cw.writerow([])
            cw.writerow([])

    out = make_response(si.getvalue())
    out.headers["Content-Disposition"] = f"attachment; filename=All_Attendance_Matrix_{datetime.now().strftime('%Y%m%d')}.csv"
    out.headers["Content-type"] = "text/csv"
    return out

@app.route("/get_student_statuses", methods=["GET"])
def get_student_statuses_route():
    if "professor_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    cid = request.args.get("class_id", type=int)
    date = request.args.get("date")
    if not cid or not date:
        return jsonify({"error": "Missing parameters"}), 400
    statuses = get_student_statuses(cid, date)
    present = sum(1 for v in statuses.values() if v['status'] == 'on_time')
    late = sum(1 for v in statuses.values() if v['status'] == 'late')
    absent = sum(1 for v in statuses.values() if v['status'] == 'absent')
    return jsonify({"statuses": statuses, "present": present, "late": late, "absent": absent})

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=grab_frames, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False)