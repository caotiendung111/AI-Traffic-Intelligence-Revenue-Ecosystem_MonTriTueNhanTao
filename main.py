import cv2
import torch
from ultralytics import YOLO
import easyocr
import datetime
import time
import numpy as np
import os
import re
import warnings
import csv
from difflib import SequenceMatcher
import threading
from queue import Queue, Empty
from database_manager import DatabaseManager
import pandas as pd
import sqlite3

# Suppress harmless warnings
warnings.filterwarnings("ignore", category=UserWarning)

# --- CONFIGURATION ---
PLATE_MODEL_PATH = 'Train_Final_PBL4/weights/best_openvino_model'
VEHICLE_MODEL_PATH = 'yolov8n.pt' 
VIDEO_PATH = 'test_bot_cutted.mp4'
CONF_THRESHOLD = 0.35 
LOG_FILE = 'traffic_data_export.csv' 
GROUND_TRUTH_FILE = 'ground_truth.csv'
MATCH_THRESHOLD = 0.75 
FRAME_SKIP = 2       
STABILITY_THRESHOLD = 2 
OUTPUT_PATH = 'output_traffic_monitoring.avi'

# Layout
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 720
SIDEBAR_WIDTH = 380
VIDEO_DISPLAY_WIDTH = WINDOW_WIDTH - SIDEBAR_WIDTH

# --- LEARNING MANAGER ---
class LearningManager:
    def __init__(self, gt_path):
        self.gt_path = gt_path
        self.knowledge_base = set() 
        self.load_knowledge()

    def load_knowledge(self):
        if not os.path.exists(self.gt_path): return
        try:
            with open(self.gt_path, 'r', encoding='utf-8') as f:
                for line in f:
                    plate = line.strip().upper().replace(".", "").replace("-", "")
                    if plate: self.knowledge_base.add(plate)
            print(f"[LEARNING] Loaded {len(self.knowledge_base)} target plates.")
        except Exception as e: print(f"[LEARNING] Error: {e}")

    def get_best_match(self, text):
        if not text: return None, 0
        clean_text = text.upper().replace(" ", "").replace(".", "").replace("-", "")
        if len(clean_text) < 5: return None, 0

        best_match = None
        best_ratio = 0
        for knowledge in self.knowledge_base:
            ratio = SequenceMatcher(None, clean_text, knowledge).ratio()
            if ratio >= 0.75 and ratio > best_ratio:
                best_ratio = ratio
                best_match = knowledge
        return best_match, best_ratio

# --- GLOBALS ---
db = None 
learner = LearningManager(GROUND_TRUTH_FILE)
lock = threading.Lock()
stop_event = threading.Event()
frame_queue = Queue(maxsize=10)
output_queue = Queue(maxsize=10) 
last_plate_crop = None
last_plate_text = "N/A"
last_v_type = "N/A"

# --- HELPERS ---

def finalize_formatting(text):
    """Smart formatting for both 8 and 9 digit Vietnamese plates: XXA.XXXXX"""
    if not text: return None
    t = text.upper().replace(".", "").replace("-", "").replace(" ", "")
    if len(t) >= 3:
        res = t[:2] + t[2]
        if len(t) > 3:
            res += "." + t[3:]
        return res
    return t

def format_plate_vietnam(text):
    if not text: return None, False
    
    # 1. Try to snap to Ground Truth
    matched, ratio = learner.get_best_match(text)
    if matched:
        return finalize_formatting(matched), True
    
    # 2. Strict Filter for unknown plates (Reject noise like 270.82)
    clean = re.sub(r'[^A-Z0-9]', '', text.upper())
    if len(clean) >= 7 and re.match(r'^\d{2}[A-Z]', clean):
        return finalize_formatting(clean), False
            
    return None, False

def export_db_to_csv():
    try:
        conn = sqlite3.connect('traffic_monitoring.db')
        df = pd.read_sql_query("SELECT timestamp, plate_text, vehicle_type FROM detections ORDER BY timestamp ASC", conn)
        df.to_csv(LOG_FILE, index=False, encoding='utf-8-sig')
        conn.close()
    except: pass

def preprocess_plate(plate_img):
    gray = cv2.cvtColor(plate_img, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    enhanced = clahe.apply(resized)
    return enhanced

# --- WORKERS ---

class VideoWorker(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
    def run(self):
        cap = cv2.VideoCapture(self.path)
        while cap.isOpened() and not stop_event.is_set():
            ret, frame = cap.read()
            if not ret: break
            while frame_queue.qsize() > 5 and not stop_event.is_set(): time.sleep(0.01)
            frame_queue.put((0, frame)) # index not strictly needed for this logic
        cap.release()
        print("Video Finished.")

class ProcessorWorker(threading.Thread):
    def __init__(self, plate_model_path, vehicle_model_path, video_thread):
        super().__init__(daemon=True)
        self.plate_model = YOLO(plate_model_path, task='detect')
        self.vehicle_model = YOLO(vehicle_model_path, task='detect')
        self.reader = easyocr.Reader(['vi', 'en'], gpu=False)
        self.tracked_objects = {} 
        self.video_thread = video_thread
        
    def run(self):
        global last_plate_crop, last_plate_text, last_v_type
        while not stop_event.is_set():
            try: _, frame = frame_queue.get(timeout=0.5)
            except Empty:
                if not self.video_thread.is_alive(): break
                continue

            v_results = self.vehicle_model.track(frame, conf=0.25, persist=True, verbose=False, imgsz=640, classes=[2, 3, 5, 7])
            p_results = self.plate_model.predict(frame, conf=CONF_THRESHOLD, verbose=False, imgsz=640)
            
            current_detections = []
            if v_results[0].boxes.id is not None:
                v_boxes = v_results[0].boxes.xyxy.cpu().numpy().astype(int)
                v_ids = v_results[0].boxes.id.cpu().numpy().astype(int)
                v_cls = v_results[0].boxes.cls.cpu().numpy().astype(int)
                v_names = v_results[0].names
                
                for box, track_id, cls_id in zip(v_boxes, v_ids, v_cls):
                    v_type = v_names[cls_id].upper()
                    if track_id not in self.tracked_objects:
                        self.tracked_objects[track_id] = {
                            "v_type": v_type, "plate": "SCANNING", "locked": False, "candidates": {}, "last_seen": time.time()
                        }
                    
                    obj = self.tracked_objects[track_id]
                    obj["last_seen"] = time.time()

                    for p_box in p_results[0].boxes.xyxy.cpu().numpy().astype(int):
                        px1, py1, px2, py2 = p_box
                        if px1 > box[0]-20 and py1 > box[1]-20 and px2 < box[2]+20 and py2 < box[3]+20:
                            if not obj["locked"]:
                                plate_crop = frame[py1:py2, px1:px2]
                                if plate_crop.size > 0:
                                    ocr_results = self.reader.readtext(preprocess_plate(plate_crop))
                                    raw_text = "".join([res[1] for res in ocr_results if res[2] > 0.15])
                                    text, is_trusted = format_plate_vietnam(raw_text)
                                    
                                    if text:
                                        obj["candidates"][text] = obj["candidates"].get(text, 0) + 1
                                        best = max(obj["candidates"], key=obj["candidates"].get)
                                        obj["plate"] = best
                                        
                                        if not db.is_plate_logged(best):
                                            threshold = 1 if is_trusted else STABILITY_THRESHOLD
                                            if obj["candidates"][best] >= threshold:
                                                obj["locked"] = True
                                                
                                                # Save crop image
                                                crop_path = None
                                                try:
                                                    crop_dir = os.path.join("static", "crops")
                                                    os.makedirs(crop_dir, exist_ok=True)
                                                    timestamp_slug = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                                                    crop_filename = f"{best.replace('.', '').replace('-', '')}_{timestamp_slug}.jpg"
                                                    crop_filepath = os.path.join(crop_dir, crop_filename)
                                                    cv2.imwrite(crop_filepath, plate_crop)
                                                    crop_path = f"static/crops/{crop_filename}"
                                                except Exception as ex:
                                                    print(f"[ERROR] Failed to save crop: {ex}")

                                                with lock:
                                                    last_plate_text = best
                                                    last_plate_crop = plate_crop.copy()
                                                    last_v_type = obj["v_type"]
                                                    db.log_detection(best, obj["v_type"], 0.0, "UNKNOWN", 1.0, crop_path=crop_path)
                                                    export_db_to_csv()
                                                    print(f"[LOGGED] {best} | {obj['v_type']}")
                                                    
                                                # Notify Uvicorn server local endpoint using standard urllib in a daemon thread
                                                def notify_app(plate, v_type, c_path):
                                                    import urllib.request
                                                    import json
                                                    try:
                                                        req_data = json.dumps({
                                                            "plate": plate,
                                                            "type": v_type,
                                                            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                                            "status": "UNPAID",
                                                            "package": "STANDARD",
                                                            "crop_path": c_path or ""
                                                        }).encode('utf-8')
                                                        req = urllib.request.Request(
                                                            "https://ai-traffic-dashboard-cmto.onrender.com/api/internal/notify",
                                                            data=req_data,
                                                            headers={'Content-Type': 'application/json'}
                                                        )
                                                        with urllib.request.urlopen(req, timeout=1.0) as response:
                                                            response.read()
                                                    except Exception:
                                                        pass
                                                        
                                                threading.Thread(target=notify_app, args=(best, obj["v_type"], crop_path), daemon=True).start()
                                            else:
                                                with lock:
                                                    last_plate_text = best
                                                    last_plate_crop = plate_crop.copy()
                                                    last_v_type = obj["v_type"]

                    current_detections.append({
                        "id": track_id, "box": box, "type": obj["v_type"], 
                        "plate": obj["plate"], "locked": obj["locked"]
                    })
            
            to_delete = [tid for tid, o in self.tracked_objects.items() if time.time() - o["last_seen"] > 2]
            for tid in to_delete: del self.tracked_objects[tid]
            output_queue.put((frame, current_detections))

def main():
    global db
    # try:
    #     if os.path.exists('traffic_monitoring.db'): os.remove('traffic_monitoring.db')
    #     if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
    # except: pass

    db = DatabaseManager()
    v_thread = VideoWorker(VIDEO_PATH)
    p_thread = ProcessorWorker(PLATE_MODEL_PATH, VEHICLE_MODEL_PATH, v_thread)
    v_thread.start(); p_thread.start()
    
    fourcc = cv2.VideoWriter_fourcc(*'XVID')
    out_writer = cv2.VideoWriter(OUTPUT_PATH, fourcc, 20.0, (WINDOW_WIDTH, WINDOW_HEIGHT))
    start_time = time.time(); frame_count = 0
    
    try:
        while not stop_event.is_set():
            try: frame, detections = output_queue.get(timeout=0.5)
            except Empty:
                if not p_thread.is_alive(): break
                continue
                
            frame_count += 1
            with lock:
                history = db.get_recent_history(5)
                curr_preview_crop = last_plate_crop.copy() if last_plate_crop is not None else None
                curr_preview_text = last_plate_text
                curr_v_type = last_v_type
                total_count = db.get_total_count()
            
            for det in detections:
                x1, y1, x2, y2 = det["box"]
                color = (0, 255, 0) if det["locked"] else (0, 165, 255)
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(frame, f"{det['plate']}", (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
            
            sidebar = np.zeros((WINDOW_HEIGHT, SIDEBAR_WIDTH, 3), dtype=np.uint8)
            for i in range(WINDOW_HEIGHT): sidebar[i, :] = (int(20 + (i/WINDOW_HEIGHT)*40),)*3
            
            cv2.putText(sidebar, "TRAFFIC MONITOR 2.0", (30, 60), cv2.FONT_HERSHEY_DUPLEX, 0.9, (255, 255, 255), 2)
            cv2.putText(sidebar, f"Total Vehicles: {total_count}", (40, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.putText(sidebar, "LIVE TRAFFIC FEED", (40, 230), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1)
            
            for i, row in enumerate(history):
                plate, vtype, _, tstamp = row
                cv2.putText(sidebar, f"{tstamp.split(' ')[1]} | {plate} | {vtype}", (40, 280 + i*35), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            
            if curr_preview_crop is not None:
                h_crop, w_crop = curr_preview_crop.shape[:2]
                cw = SIDEBAR_WIDTH - 60; ch = min(180, int(h_crop * (cw / w_crop)))
                try:
                    crop_disp = cv2.resize(curr_preview_crop, (cw, ch))
                    sidebar[510:510+ch, 30:30+cw] = crop_disp
                    cv2.putText(sidebar, f"PLATE: {curr_preview_text}", (40, 510+ch+35), cv2.FONT_HERSHEY_DUPLEX, 0.8, (0, 255, 255), 1)
                except: pass

            combined = np.hstack((cv2.resize(frame, (VIDEO_DISPLAY_WIDTH, WINDOW_HEIGHT)), sidebar))
            out_writer.write(combined)
            cv2.imshow("Professional Traffic Monitoring System", combined)
            if cv2.waitKey(1) & 0xFF == ord('q'): break
    except KeyboardInterrupt: pass

    stop_event.set(); out_writer.release(); cv2.destroyAllWindows()
    print("Cleanly Shut Down.")

if __name__ == "__main__": main()
