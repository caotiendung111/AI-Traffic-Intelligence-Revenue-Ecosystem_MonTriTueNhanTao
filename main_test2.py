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

# --- CONFIGURATION (ADVANCED AI SCANNER) ---
PLATE_MODEL_PATH = 'Train_Final_PBL4/weights/best_openvino_model'
VEHICLE_MODEL_PATH = 'yolov8n.pt' 
VIDEO_PATH = 'test_bot_test2.mp4' 
LOG_FILE = 'trafic_data_export_testbot2.csv' 
DB_FILE = 'traffic_test2.db'      
CONF_THRESHOLD = 0.25            
FRAME_SKIP = 2       
STABILITY_THRESHOLD = 2 
OUTPUT_PATH = 'output_test2.mp4'

# Layout
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 720
SIDEBAR_WIDTH = 380
VIDEO_DISPLAY_WIDTH = WINDOW_WIDTH - SIDEBAR_WIDTH

# --- AI REINFORCEMENT & LEARNING MANAGER ---
class AILearningManager:
    def __init__(self):
        self.confirmed_plates = set() # Master list of learned plates
        self.ocr_correction_map = {
            'GOK': '60A', 'GON': '60A', 'G0N': '60A', 'G0K': '60A',
            '5IF': '51F', 'SI6': '51G', 'S16': '51G', '51I': '51F',
            'O': '0', 'I': '1', 'S': '5', 'B': '8', 'D': '0'
        }

    def apply_reinforcement(self, raw_text):
        """Learns and corrects mistakes based on patterns and structure"""
        if not raw_text or len(raw_text) < 4: return None
        
        # 1. Structural Correction (Hard Snapping common OCR errors)
        text = raw_text.upper()
        for error, fix in self.ocr_correction_map.items():
            if len(error) == 3 and text.startswith(error):
                text = fix + text[3:]
        
        # 2. Character-Position Intelligence (Deep Learning Logic)
        # VN plates: 12-A-34567 or 12-A-345.67
        chars = list(text)
        for i in range(len(chars)):
            # Positions 0, 1 should be numbers
            if i in [0, 1] and chars[i] in ['I', 'S', 'O', 'B', 'D']:
                chars[i] = self.ocr_correction_map.get(chars[i], chars[i])
            # Position 2 should be a letter (usually)
            if i == 2 and chars[i] == '0': chars[i] = 'A'
            if i == 2 and chars[i] == '1': chars[i] = 'I'
            # Positions 3 onwards should be numbers
            if i >= 3 and chars[i] in ['I', 'S', 'O', 'B', 'D', 'G']:
                chars[i] = self.ocr_correction_map.get(chars[i], chars[i])
        
        corrected = "".join(chars)
        
        # 3. Knowledge Base Matching (Fuzzy Reinforcement)
        for confirmed in self.confirmed_plates:
            if SequenceMatcher(None, corrected, confirmed).ratio() > 0.88:
                return confirmed # Snap to already learned best version
        
        return corrected

    def format_final(self, text):
        """Final professional formatting"""
        if not text: return None
        t = re.sub(r'[^A-Z0-9]', '', text.upper())
        if len(t) >= 3:
            return t[:2] + t[2] + "." + t[3:]
        return t

# --- GLOBALS ---
db = None 
ai_learner = AILearningManager()
lock = threading.Lock()
stop_event = threading.Event()
frame_queue = Queue(maxsize=10)
output_queue = Queue(maxsize=15) 
last_plate_crop = None
last_plate_text = "N/A"
last_v_type = "N/A"

# --- HELPERS ---

def export_db_to_csv():
    try:
        conn = sqlite3.connect(DB_FILE)
        df = pd.read_sql_query("SELECT timestamp, plate_text, vehicle_type FROM detections", conn)
        df.to_csv(LOG_FILE, index=False, encoding='utf-8-sig')
        conn.close()
    except: pass

def preprocess_plate(plate_img):
    # Advanced preprocessing: Upscale + Grayscale + Adaptive Threshold
    resized = cv2.resize(plate_img, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    # Use Adaptive Threshold to handle lighting variations
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
    return thresh

# --- WORKERS ---

class VideoWorker(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
    def run(self):
        cap = cv2.VideoCapture(self.path)
        frame_idx = 0
        while cap.isOpened() and not stop_event.is_set():
            ret, frame = cap.read()
            if not ret: break
            while frame_queue.qsize() > 5 and not stop_event.is_set(): time.sleep(0.01)
            frame_queue.put((frame_idx, frame))
            frame_idx += 1
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
                            "v_type": v_type, "plate": "LEARNING...", "locked": False, 
                            "candidates": {}, "last_seen": time.time()
                        }
                    
                    obj = self.tracked_objects[track_id]
                    obj["last_seen"] = time.time()

                    for p_box in p_results[0].boxes.xyxy.cpu().numpy().astype(int):
                        px1, py1, px2, py2 = p_box
                        # Associate plate with vehicle if it's inside or very close to vehicle box
                        if px1 > box[0]-25 and py1 > box[1]-25 and px2 < box[2]+25 and py2 < box[3]+25:
                            obj["current_plate_box"] = p_box # Store for drawing
                            if not obj["locked"]:
                                # EXPAND CROP (Padding) to catch both lines
                                pad_h, pad_w = int((py2-py1)*0.3), int((px2-px1)*0.1)
                                e_y1, e_y2 = max(0, py1-pad_h), min(frame.shape[0], py2+int(pad_h/2))
                                e_x1, e_x2 = max(0, px1-pad_w), min(frame.shape[1], px2+pad_w)
                                
                                plate_crop = frame[e_y1:e_y2, e_x1:e_x2]
                                if plate_crop.size > 0:
                                    # OCR with 2-line awareness
                                    ocr_results = self.reader.readtext(preprocess_plate(plate_crop))
                                    # Sort results by Y coordinate first
                                    ocr_results.sort(key=lambda x: x[0][0][1])
                                    raw_ocr = "".join([res[1] for res in ocr_results if res[2] > 0.15])
                                    text = ai_learner.apply_reinforcement(raw_ocr)
                                    
                                    if text:
                                        obj["candidates"][text] = obj["candidates"].get(text, 0) + 1
                                        best_raw = max(obj["candidates"], key=obj["candidates"].get)
                                        obj["plate"] = ai_learner.format_final(best_raw)
                                        
                                        if not db.is_plate_logged(obj["plate"]):
                                            threshold = 1 if ai_learner.apply_reinforcement(best_raw) in ai_learner.confirmed_plates else STABILITY_THRESHOLD
                                            if obj["candidates"][best_raw] >= threshold:
                                                final_plate = obj["plate"]
                                                obj["locked"] = True
                                                ai_learner.confirmed_plates.add(best_raw)
                                                with lock:
                                                    last_plate_text = final_plate
                                                    last_plate_crop = plate_crop.copy()
                                                    last_v_type = obj["v_type"]
                                                    db.log_detection(final_plate, obj["v_type"], 0.0, "AI_LEARN", 1.0)
                                                    export_db_to_csv()
                                                    print(f"[AI LEARNED] {final_plate} | {obj['v_type']}")

                    current_detections.append({
                        "id": track_id, "box": box, "type": obj["v_type"], 
                        "plate": obj["plate"], "locked": obj["locked"],
                        "plate_box": obj.get("current_plate_box") # Pass plate box to main
                    })
            
            # Clear current_plate_box for next frame
            for tid in self.tracked_objects: self.tracked_objects[tid]["current_plate_box"] = None
            
            to_delete = [tid for tid, o in self.tracked_objects.items() if time.time() - o["last_seen"] > 2]
            for tid in to_delete: del self.tracked_objects[tid]
            output_queue.put((frame, current_detections))

def main():
    global db
    # try:
    #     if os.path.exists(DB_FILE): os.remove(DB_FILE)
    #     if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
    # except: pass

    db = DatabaseManager(DB_FILE)
    v_thread = VideoWorker(VIDEO_PATH)
    p_thread = ProcessorWorker(PLATE_MODEL_PATH, VEHICLE_MODEL_PATH, v_thread)
    v_thread.start(); p_thread.start()
    
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out_writer = cv2.VideoWriter(OUTPUT_PATH, fourcc, 20.0, (WINDOW_WIDTH, WINDOW_HEIGHT))
    
    try:
        while not stop_event.is_set():
            try: frame, detections = output_queue.get(timeout=0.5)
            except Empty:
                if not p_thread.is_alive(): break
                continue
                
            with lock:
                history = db.get_recent_history(10)
                curr_preview_crop = last_plate_crop.copy() if last_plate_crop is not None else None
                curr_preview_text = last_plate_text
                curr_v_type = last_v_type
                total_count = db.get_total_count()
            
            for det in detections:
                # 1. Draw Vehicle Box (Thin)
                vx1, vy1, vx2, vy2 = det["box"]
                v_color = (150, 150, 150) # Grey for vehicle
                cv2.rectangle(frame, (vx1, vy1), (vx2, vy2), v_color, 1)
                cv2.putText(frame, f"{det['type']}", (vx1, vy1-5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, v_color, 1)

                # 2. Draw Plate Box (Thick)
                if det["plate_box"] is not None:
                    px1, py1, px2, py2 = det["plate_box"]
                    p_color = (0, 255, 0) if det["locked"] else (0, 165, 255)
                    cv2.rectangle(frame, (px1, py1), (px2, py2), p_color, 2)
                    # Label on plate
                    cv2.putText(frame, f"{det['plate']}", (px1, py1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, p_color, 2)
            
            sidebar = np.zeros((WINDOW_HEIGHT, SIDEBAR_WIDTH, 3), dtype=np.uint8)
            for i in range(WINDOW_HEIGHT): sidebar[i, :] = (int(10 + (i/WINDOW_HEIGHT)*30),)*3
            
            cv2.putText(sidebar, "AI REINFORCEMENT SCANNER", (30, 60), cv2.FONT_HERSHEY_DUPLEX, 0.7, (255, 255, 255), 2)
            cv2.putText(sidebar, f"Identified: {total_count}", (40, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.putText(sidebar, "SMART LEARNED LOG", (40, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
            
            for i, row in enumerate(history):
                plate, vtype, _, tstamp = row
                cv2.putText(sidebar, f"{plate} | {vtype}", (40, 210 + i*30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            
            if curr_preview_crop is not None:
                h_crop, w_crop = curr_preview_crop.shape[:2]
                cw = SIDEBAR_WIDTH - 60; ch = min(180, int(h_crop * (cw / w_crop)))
                try:
                    crop_disp = cv2.resize(curr_preview_crop, (cw, ch))
                    sidebar[500:500+ch, 30:30+cw] = crop_disp
                    cv2.putText(sidebar, f"LEARNED: {curr_preview_text}", (40, 500+ch+35), cv2.FONT_HERSHEY_DUPLEX, 0.7, (0, 255, 255), 1)
                except: pass

            combined = np.hstack((cv2.resize(frame, (VIDEO_DISPLAY_WIDTH, WINDOW_HEIGHT)), sidebar))
            out_writer.write(combined)
            cv2.imshow("AI Reinforced Traffic Monitoring", combined)
            if cv2.waitKey(1) & 0xFF == ord('q'): break
    except KeyboardInterrupt: pass

    stop_event.set(); out_writer.release(); cv2.destroyAllWindows()
    print("AI Scan Complete.")

if __name__ == "__main__": main()
