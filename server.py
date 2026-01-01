import cv2
import numpy as np
import face_recognition
from flask import Flask, jsonify, render_template, request, Response
import sqlite3
import os
import time
import threading
import requests

# ===== Config =====
DB = "attendance.db"
UPLOADS = "student_images"
ESP32_STREAM = "http://192.168.1.81:81/stream"  # Change to your ESP32-CAM IP
os.makedirs(UPLOADS, exist_ok=True)

app = Flask(__name__)

# ===== Global for MJPEG streaming =====
current_frame = None
frame_lock = threading.Lock()


# ===== Database =====
def init_db():
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS students (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        student_id TEXT UNIQUE,
                        name TEXT,
                        encoding BLOB
                       )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS attendance (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        student_id TEXT,
                        timestamp TEXT
                       )''')
        c.commit()


def save_student_encoding(student_id, name, encoding):
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("INSERT OR REPLACE INTO students (student_id, name, encoding) VALUES (?, ?, ?)",
                    (student_id, name, encoding.tobytes()))
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


def log_attendance(student_id):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("INSERT INTO attendance (student_id, timestamp) VALUES (?, ?)", (student_id, ts))
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
            time.sleep(0.05)  # small delay if failed
    cap.release()


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
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stream")
def stream():
    return Response(generate_mjpeg(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route("/capture_enroll", methods=["GET"])
def capture_enroll():
    name = request.args.get("name")
    if not name:
        return jsonify({"error": "Name required"}), 400
    student_id = name.lower().replace(" ", "_")

    frame = get_latest_frame()
    if frame is None:
        return jsonify({"error": "No frame available"}), 500

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    encs = face_recognition.face_encodings(rgb)
    if not encs:
        return jsonify({"error": "No face detected"}), 400

    save_student_encoding(student_id, name, encs[0].astype(np.float64))
    path = os.path.join(UPLOADS, f"{student_id}.jpg")
    cv2.imwrite(path, frame)

    return jsonify({"status": "enrolled", "student_id": student_id})


@app.route("/capture_attendance", methods=["GET"])
def capture_attendance():
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
        log_attendance(student_id)
        return jsonify({"status": "match", "student_id": student_id, "name": known_names[best_idx]})
    return jsonify({"status": "unknown"})


@app.route("/attendance", methods=["GET"])
def attendance_list():
    with sqlite3.connect(DB) as c:
        cur = c.cursor()
        cur.execute("SELECT student_id, timestamp FROM attendance ORDER BY timestamp DESC LIMIT 50")
        rows = cur.fetchall()
    return jsonify(rows)


# ===== Main =====
if __name__ == "__main__":
    init_db()
    # Start frame grabbing thread
    t = threading.Thread(target=grab_frames, daemon=True)
    t.start()
    # Run Flask
    app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False)
