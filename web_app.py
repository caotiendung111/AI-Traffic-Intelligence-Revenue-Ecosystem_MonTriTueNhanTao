"""TrafficAI dashboard backend."""

import csv
import io
import os
import re
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, List

import jwt
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, create_engine, desc
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from config import settings

engine = create_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
VALID_STATUSES = {"UNPAID", "PAID", "BLACKLISTED"}
CONFIG_KEYS = {"system_name", "price_per_vehicle", "token_expire_h"}


# --- REALTIME AI ENGINE STATE & THREAD ---
ai_engine_running = False
ai_engine_thread = None
latest_frame_lock = threading.Lock()
latest_processed_frame = None

_reader_lock = threading.Lock()
_easyocr_reader = None

_models_lock = threading.Lock()
_plate_model = None
_vehicle_model = None
_optimal_devices = None

def get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr
        with _reader_lock:
            if _easyocr_reader is None:
                _easyocr_reader = easyocr.Reader(['vi', 'en'], gpu=False)
    return _easyocr_reader

def get_ai_models():
    global _plate_model, _vehicle_model, _optimal_devices
    if _plate_model is None or _vehicle_model is None:
        with _models_lock:
            if _plate_model is None or _vehicle_model is None:
                from ultralytics import YOLO
                import openvino
                
                PLATE_MODEL_PATH = 'Train_Final_PBL4/weights/best_openvino_model'
                has_ov_vehicle = False
                if os.path.exists('yolov8n_openvino_model'):
                    VEHICLE_MODEL_PATH = 'yolov8n_openvino_model'
                    has_ov_vehicle = True
                else:
                    VEHICLE_MODEL_PATH = 'yolov8n.pt'
                    
                target_device_plate = 'cpu'
                target_device_vehicle = 'cpu'
                try:
                    core = openvino.Core()
                    if "GPU" in core.available_devices:
                        target_device_plate = "intel:gpu"
                        if has_ov_vehicle:
                            target_device_vehicle = "intel:gpu"
                except Exception:
                    pass
                
                print(f"[AI ENGINE] Loading models - Plate: {target_device_plate}, Vehicle: {target_device_vehicle}")
                _plate_model = YOLO(PLATE_MODEL_PATH, task='detect')
                _vehicle_model = YOLO(VEHICLE_MODEL_PATH, task='detect')
                _optimal_devices = {
                    "plate": target_device_plate,
                    "vehicle": target_device_vehicle
                }
    return _plate_model, _vehicle_model, _optimal_devices

class TrafficAIEngine(threading.Thread):
    def __init__(self, source_path: str):
        super().__init__(daemon=True)
        self.source_path = source_path
        self.stop_event = threading.Event()
        self.fps = 0
        self.processed_frames = 0
        self.status = "Initializing"

    def run(self):
        try:
            from ultralytics import YOLO
            import time
            import datetime
            from difflib import SequenceMatcher
            import cv2
            
            self.status = "Loading AI models..."
            plate_model, vehicle_model, devices = get_ai_models()
            target_device_plate = devices["plate"]
            target_device_vehicle = devices["vehicle"]
            reader = get_easyocr_reader()
            
            self.status = "Opening source..."
            cap = cv2.VideoCapture(self.source_path)
            if not cap.isOpened():
                self.status = f"Failed to open source"
                return
                
            self.status = "Active"
            tracked_objects = {}
            last_seen_cleanup = time.time()
            
            knowledge_base = set()
            if os.path.exists('ground_truth.csv'):
                try:
                    with open('ground_truth.csv', 'r', encoding='utf-8') as f:
                         for line in f:
                             plate = line.strip().upper().replace(".", "").replace("-", "")
                             if plate: knowledge_base.add(plate)
                except Exception as e:
                    print(f"Error loading GT: {e}")
                    
            def get_best_match(text):
                if not text: return None, 0
                clean_text = text.upper().replace(" ", "").replace(".", "").replace("-", "")
                if len(clean_text) < 5: return None, 0
                best_match = None
                best_ratio = 0
                for knowledge in knowledge_base:
                    ratio = SequenceMatcher(None, clean_text, knowledge).ratio()
                    if ratio >= 0.75 and ratio > best_ratio:
                        best_ratio = ratio
                        best_match = knowledge
                return best_match, best_ratio

            def finalize_formatting(text):
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
                matched, ratio = get_best_match(text)
                if matched:
                    return finalize_formatting(matched), True
                clean = re.sub(r'[^A-Z0-9]', '', text.upper())
                if len(clean) >= 7 and re.match(r'^\d{2}[A-Z]', clean):
                    return finalize_formatting(clean), False
                return None, False

            frame_count = 0
            start_time = time.time()
            
            global latest_processed_frame
            
            while cap.isOpened() and not self.stop_event.is_set():
                ret, frame = cap.read()
                if not ret:
                    if isinstance(self.source_path, str) and not self.source_path.startswith("rtsp") and not self.source_path.startswith("http"):
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    else:
                        break
                        
                frame_count += 1
                self.processed_frames = frame_count
                
                elapsed = time.time() - start_time
                if elapsed > 1.0:
                    self.fps = round(frame_count / elapsed, 1)
                    start_time = time.time()
                    frame_count = 0
                    
                if frame_count % 2 != 0:
                    continue
                    
                v_results = vehicle_model.track(frame, conf=0.25, persist=True, verbose=False, imgsz=640, classes=[2, 3, 5, 7], device=target_device_vehicle)
                p_results = plate_model.predict(frame, conf=0.35, verbose=False, imgsz=640, device=target_device_plate)
                
                current_detections = []
                if v_results[0].boxes.id is not None:
                    v_boxes = v_results[0].boxes.xyxy.cpu().numpy().astype(int)
                    v_ids = v_results[0].boxes.id.cpu().numpy().astype(int)
                    v_cls = v_results[0].boxes.cls.cpu().numpy().astype(int)
                    v_names = v_results[0].names
                    
                    for box, track_id, cls_id in zip(v_boxes, v_ids, v_cls):
                        v_type = v_names[cls_id].upper()
                        if track_id not in tracked_objects:
                            tracked_objects[track_id] = {
                                "v_type": v_type, "plate": "SCANNING", "locked": False, "candidates": {}, "last_seen": time.time()
                            }
                        
                        obj = tracked_objects[track_id]
                        obj["last_seen"] = time.time()
                        
                        for p_box in p_results[0].boxes.xyxy.cpu().numpy().astype(int):
                            px1, py1, px2, py2 = p_box
                            if px1 > box[0]-20 and py1 > box[1]-20 and px2 < box[2]+20 and py2 < box[3]+20:
                                if not obj["locked"]:
                                    plate_crop = frame[py1:py2, px1:px2]
                                    if plate_crop.size > 0:
                                        enhanced_plate = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2GRAY)
                                        enhanced_plate = cv2.resize(enhanced_plate, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                                        ocr_results = reader.readtext(enhanced_plate)
                                        raw_text = "".join([res[1] for res in ocr_results if res[2] > 0.15])
                                        text, is_trusted = format_plate_vietnam(raw_text)
                                        
                                        if text:
                                            obj["candidates"][text] = obj["candidates"].get(text, 0) + 1
                                            best = max(obj["candidates"], key=obj["candidates"].get)
                                            obj["plate"] = best
                                            
                                            db_session = SessionLocal()
                                            try:
                                                is_logged = db_session.query(Detection).filter(Detection.plate_text == best, Detection.deleted_at == None).first() is not None
                                                threshold = 1 if is_trusted else 2
                                                if obj["candidates"][best] >= threshold and not is_logged:
                                                    obj["locked"] = True
                                                    
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
                                                        print(f"Error saving crop: {ex}")
                                                        
                                                    new_det = Detection(
                                                        timestamp=datetime.datetime.now(),
                                                        plate_text=best,
                                                        vehicle_type=obj["v_type"],
                                                        confidence=1.0 if is_trusted else 0.85,
                                                        payment_status="UNPAID",
                                                        package_type="STANDARD",
                                                        crop_path=crop_path,
                                                        lane="Lane 1"
                                                    )
                                                    db_session.add(new_det)
                                                    db_session.commit()
                                                    
                                                    # Loopback notify to local endpoint
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
                                                                "http://localhost:8001/api/internal/notify",
                                                                data=req_data,
                                                                headers={'Content-Type': 'application/json'}
                                                            )
                                                            with urllib.request.urlopen(req, timeout=1.0) as response:
                                                                response.read()
                                                        except Exception:
                                                            pass
                                                    threading.Thread(target=notify_app, args=(best, obj["v_type"], crop_path), daemon=True).start()
                                                    print(f"[BOT LOGGED] {best} | {obj['v_type']}")
                                            except Exception as e:
                                                print(f"DB Error inside thread: {e}")
                                            finally:
                                                db_session.close()
                                                
                        current_detections.append({
                            "box": box, "plate": obj["plate"], "locked": obj["locked"]
                        })
                        
                for det in current_detections:
                    bx1, by1, bx2, by2 = det["box"]
                    color = (0, 255, 0) if det["locked"] else (0, 165, 255)
                    cv2.rectangle(frame, (bx1, by1), (bx2, by2), color, 2)
                    cv2.putText(frame, f"{det['plate']}", (bx1, by1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)
                    
                cv2.putText(frame, f"TrafficAI BOT Camera - FPS: {self.fps}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
                
                frame_resized = cv2.resize(frame, (1280, 720))
                _, jpeg = cv2.imencode('.jpg', frame_resized)
                
                with latest_frame_lock:
                    latest_processed_frame = jpeg.tobytes()
                    
                now_time = time.monotonic()
                if now_time - last_seen_cleanup > 5:
                    to_del = [tid for tid, o in tracked_objects.items() if now_time - o["last_seen"] > 3]
                    for tid in to_del: del tracked_objects[tid]
                    last_seen_cleanup = now_time
                    
            cap.release()
            self.status = "Finished"
        except Exception as err:
            self.status = f"Error: {err}"
            print(f"Error inside AI Thread: {err}")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class PackageUpdate(BaseModel):
    package: str = Field("SUPREME", min_length=1, max_length=32)


class BlacklistUpdate(BaseModel):
    reason: str = Field("Manual review", max_length=255)


class ConfigUpdate(BaseModel):
    value: Any


class TrafficShield:
    """Small in-memory guard for local deployments and classroom demos."""

    def __init__(self):
        self.lock = Lock()
        self.buckets = defaultdict(deque)
        self.bans: dict[str, float] = {}
        self.ws_by_ip = defaultdict(int)
        self.ws_total = 0
        self.allowed_requests = 0
        self.blocked_requests = 0
        self.body_rejected = 0
        self.websocket_rejected = 0

    def _cleanup_bucket(self, values: deque, now: float, window_seconds: int):
        cutoff = now - window_seconds
        while values and values[0] <= cutoff:
            values.popleft()

    def _limit_for_path(self, path: str) -> tuple[str, int, int]:
        if path == "/api/login":
            return "login", settings.LOGIN_RATE_LIMIT_REQUESTS, settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS
        return "global", settings.RATE_LIMIT_REQUESTS, settings.RATE_LIMIT_WINDOW_SECONDS

    def check_http(self, ip: str, path: str, method: str, content_length: str | None) -> tuple[bool, int, str, int]:
        if not settings.RATE_LIMIT_ENABLED or method == "OPTIONS":
            return True, 200, "", 0

        max_body = max(0, settings.MAX_REQUEST_BODY_BYTES)
        if content_length and max_body:
            try:
                if int(content_length) > max_body:
                    with self.lock:
                        self.blocked_requests += 1
                        self.body_rejected += 1
                    return False, 413, "Request body is too large", 0
            except ValueError:
                with self.lock:
                    self.blocked_requests += 1
                    self.body_rejected += 1
                return False, 400, "Invalid Content-Length header", 0

        now = time.monotonic()
        with self.lock:
            ban_until = self.bans.get(ip)
            if ban_until:
                if ban_until > now:
                    self.blocked_requests += 1
                    return False, 429, "IP is temporarily rate limited", max(1, int(ban_until - now))
                del self.bans[ip]

            bucket_name, limit, window = self._limit_for_path(path)
            key = (ip, bucket_name)
            values = self.buckets[key]
            self._cleanup_bucket(values, now, window)
            if len(values) >= max(1, limit):
                ban_seconds = max(0, settings.RATE_LIMIT_BAN_SECONDS)
                if ban_seconds:
                    self.bans[ip] = now + ban_seconds
                self.blocked_requests += 1
                retry_after = ban_seconds or max(1, int(window - (now - values[0])))
                return False, 429, "Too many requests", retry_after

            values.append(now)
            self.allowed_requests += 1
            return True, 200, "", 0

    def connect_ws(self, ip: str) -> tuple[bool, str]:
        if not settings.RATE_LIMIT_ENABLED:
            return True, ""

        now = time.monotonic()
        with self.lock:
            ban_until = self.bans.get(ip)
            if ban_until:
                if ban_until > now:
                    self.websocket_rejected += 1
                    return False, "IP is temporarily rate limited"
                del self.bans[ip]

            if self.ws_total >= max(1, settings.MAX_WS_TOTAL):
                self.websocket_rejected += 1
                return False, "WebSocket capacity reached"
            if self.ws_by_ip[ip] >= max(1, settings.MAX_WS_PER_IP):
                self.websocket_rejected += 1
                return False, "Too many WebSocket connections from this IP"

            self.ws_total += 1
            self.ws_by_ip[ip] += 1
            return True, ""

    def disconnect_ws(self, ip: str):
        with self.lock:
            if self.ws_by_ip[ip] > 0:
                self.ws_by_ip[ip] -= 1
                self.ws_total = max(0, self.ws_total - 1)
            if self.ws_by_ip[ip] == 0:
                self.ws_by_ip.pop(ip, None)

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self.lock:
            expired = [ip for ip, until in self.bans.items() if until <= now]
            for ip in expired:
                del self.bans[ip]
            return {
                "enabled": settings.RATE_LIMIT_ENABLED,
                "global_limit": {
                    "requests": settings.RATE_LIMIT_REQUESTS,
                    "window_seconds": settings.RATE_LIMIT_WINDOW_SECONDS,
                },
                "login_limit": {
                    "requests": settings.LOGIN_RATE_LIMIT_REQUESTS,
                    "window_seconds": settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS,
                },
                "ban_seconds": settings.RATE_LIMIT_BAN_SECONDS,
                "max_request_body_bytes": settings.MAX_REQUEST_BODY_BYTES,
                "active_bans": len(self.bans),
                "active_websockets": self.ws_total,
                "active_websockets_by_ip": dict(self.ws_by_ip),
                "allowed_requests": self.allowed_requests,
                "blocked_requests": self.blocked_requests,
                "body_rejected": self.body_rejected,
                "websocket_rejected": self.websocket_rejected,
            }


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String, default="operator")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    ip = Column(String)
    user_agent = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)


class Detection(Base):
    __tablename__ = "detections"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    plate_text = Column(String, index=True)
    vehicle_type = Column(String)
    confidence = Column(Float)
    payment_status = Column(String, default="UNPAID")
    package_type = Column(String, default="STANDARD")
    blacklist_reason = Column(String, nullable=True)
    lane = Column(String, nullable=True)
    speed_kmh = Column(Float, nullable=True)
    deleted_at = Column(DateTime, nullable=True)
    crop_path = Column(String, nullable=True)
    direction = Column(String, nullable=True)


class DetectionHistory(Base):
    __tablename__ = "detection_history"
    id = Column(Integer, primary_key=True, index=True)
    detection_id = Column(Integer, ForeignKey("detections.id"))
    plate_text = Column(String)
    old_status = Column(String)
    new_status = Column(String)
    changed_by = Column(String)
    changed_at = Column(DateTime, default=datetime.utcnow)


class SystemConfig(Base):
    __tablename__ = "system_config"
    key = Column(String, primary_key=True, unique=True)
    value = Column(String)
    updated_by = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow)


class DailySummary(Base):
    __tablename__ = "daily_summaries"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, unique=True)
    total = Column(Integer)
    paid = Column(Integer)
    blacklisted = Column(Integer)
    revenue = Column(Integer)


class UserPreference(Base):
    __tablename__ = "user_preferences"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    pref_key = Column(String)
    pref_value = Column(String)


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, index=True)
    level = Column(String)
    message = Column(String)
    plate = Column(String, nullable=True)
    resolved = Column(Boolean, default=False)
    timestamp = Column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    username = Column(String)
    action = Column(String)
    detail = Column(String)
    ip = Column(String)


pwd_context = CryptContext(schemes=["argon2", "pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")
scheduler = BackgroundScheduler()
traffic_shield = TrafficShield()


def get_client_ip_from_request(request: Request) -> str:
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip() or "unknown"
    return request.client.host if request.client else "unknown"


def get_client_ip_from_websocket(websocket: WebSocket) -> str:
    if settings.TRUST_PROXY_HEADERS:
        forwarded = websocket.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
        real_ip = websocket.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip() or "unknown"
    return websocket.client.host if websocket.client else "unknown"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def parse_dt(value):
    if value is None or isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value).replace("Z", ""), fmt)
        except ValueError:
            continue
    return None


def iso_dt(value):
    parsed = parse_dt(value)
    return parsed.isoformat() if parsed else ""


def get_config_value(db: Session, key: str, default: str = "") -> str:
    cfg = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    return cfg.value if cfg else default


def normalize_plate(plate: str) -> str:
    value = (plate or "").strip().upper()
    if not value:
        raise HTTPException(status_code=400, detail="Plate is required")
    return value


def normalize_status(status: str) -> str:
    value = (status or "").strip().upper()
    if value and value not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unsupported status: {value}")
    return value


def normalize_package(package: str) -> str:
    value = (package or "STANDARD").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,32}", value):
        raise HTTPException(status_code=400, detail="Package must contain only letters, numbers, '-' or '_'")
    return value


def safe_int(value: Any, default: int, min_value: int = 0, max_value: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < min_value:
        return default
    if max_value is not None and parsed > max_value:
        return default
    return parsed


def get_price_per_vehicle(db: Session) -> int:
    return safe_int(get_config_value(db, "price_per_vehicle", settings.PRICE_PER_VEHICLE), settings.PRICE_PER_VEHICLE, 0)


def get_token_expire_hours(db: Session) -> int:
    return safe_int(get_config_value(db, "token_expire_h", settings.TOKEN_EXPIRE_H), settings.TOKEN_EXPIRE_H, 1, 168)


def detection_payload(row: Detection) -> dict:
    return {
        "id": row.id,
        "plate": row.plate_text,
        "type": row.vehicle_type,
        "timestamp": iso_dt(row.timestamp),
        "status": row.payment_status,
        "package": row.package_type,
        "confidence": row.confidence,
        "speed_kmh": row.speed_kmh,
        "lane": row.lane,
        "blacklist_reason": row.blacklist_reason,
        "deleted_at": iso_dt(row.deleted_at),
        "crop_path": row.crop_path or "",
    }


def log_action(db: Session, user: str, action: str, detail: str = "", ip: str = ""):
    db.add(AuditLog(username=user, action=action, detail=detail, ip=ip))


def migrate_legacy_schema():
    conn = sqlite3.connect("traffic_monitoring.db")
    cur = conn.cursor()
    migrations = [
        "ALTER TABLE detections ADD COLUMN payment_status TEXT DEFAULT 'UNPAID'",
        "ALTER TABLE detections ADD COLUMN package_type TEXT DEFAULT 'STANDARD'",
        "ALTER TABLE detections ADD COLUMN blacklist_reason TEXT",
        "ALTER TABLE detections ADD COLUMN lane TEXT",
        "ALTER TABLE detections ADD COLUMN speed_kmh REAL",
        "ALTER TABLE detections ADD COLUMN deleted_at DATETIME",
        "ALTER TABLE detections ADD COLUMN crop_path TEXT",
        "ALTER TABLE detections ADD COLUMN direction TEXT",
    ]
    for sql in migrations:
        try:
            cur.execute(sql)
        except sqlite3.OperationalError:
            pass
    try:
        cur.execute("UPDATE detections SET payment_status = 'UNPAID' WHERE payment_status IS NULL OR payment_status = ''")
        cur.execute("UPDATE detections SET package_type = 'STANDARD' WHERE package_type IS NULL OR package_type = '' OR package_type = 'NONE'")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    migrate_legacy_schema()

    db = SessionLocal()
    defaults = {
        "price_per_vehicle": str(settings.PRICE_PER_VEHICLE),
        "token_expire_h": str(settings.TOKEN_EXPIRE_H),
        "system_name": settings.SYSTEM_NAME,
    }
    for key, value in defaults.items():
        if not db.query(SystemConfig).filter(SystemConfig.key == key).first():
            db.add(SystemConfig(key=key, value=value, updated_by="SYSTEM"))
    if not db.query(User).filter(User.username == "admin").first():
        db.add(User(username="admin", hashed_password=pwd_context.hash("123456"), role="admin"))
    db.commit()
    db.close()

    if not scheduler.running:
        scheduler.start()

    # Pre-load AI models and reader in the background during server boot so start is instant!
    def pre_load():
        try:
            get_ai_models()
            get_easyocr_reader()
            print("[AI ENGINE] Models pre-loaded successfully in background!")
        except Exception as e:
            print(f"[AI ENGINE] Error pre-loading models in background: {e}")
    threading.Thread(target=pre_load, daemon=True).start()

    yield
    if scheduler.running:
        scheduler.shutdown()


app = FastAPI(title=settings.SYSTEM_NAME, version=settings.VERSION, lifespan=lifespan)


@app.middleware("http")
async def request_logging_and_id_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    start_time = time.time()
    client_ip = get_client_ip_from_request(request)
    allowed, status_code, detail, retry_after = traffic_shield.check_http(
        client_ip,
        request.url.path,
        request.method,
        request.headers.get("content-length"),
    )
    if not allowed:
        response = JSONResponse(
            status_code=status_code,
            content={"detail": detail, "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )
        if retry_after:
            response.headers["Retry-After"] = str(retry_after)
        response.headers["X-TrafficShield"] = "blocked"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        print(f"ID: {request_id} | {request.method} {request.url.path} | IP: {client_ip} | Status: {status_code} | BLOCKED")
        return response

    response = await call_next(request)
    process_time = (time.time() - start_time) * 1000
    print(f"ID: {request_id} | {request.method} {request.url.path} | IP: {client_ip} | Status: {response.status_code} | {process_time:.2f}ms")
    response.headers["X-Request-ID"] = request_id
    response.headers["X-TrafficShield"] = "pass"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def create_access_token(data: dict, expire_hours: int):
    to_encode = data.copy()
    to_encode.update({"exp": datetime.utcnow() + timedelta(hours=expire_hours)})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc


def require_role(roles: List[str]):
    def role_checker(user: dict = Depends(get_current_user)):
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="Permission denied")
        return user
    return role_checker


@app.get("/api/health")
def health_check():
    return {"status": "healthy", "version": settings.VERSION, "engine": "TrafficAI"}


@app.get("/api/system/status")
def get_system_status(db: Session = Depends(get_db), _: dict = Depends(require_role(["admin"]))):
    db_path = settings.DATABASE_URL.replace("sqlite:///", "")
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    return {
        "version": settings.VERSION,
        "database": db_path,
        "database_size_bytes": db_size,
        "detections": db.query(Detection).count(),
        "active_sessions": db.query(UserSession).filter(UserSession.is_active == True).count(),
        "scheduler_running": scheduler.running,
        "allowed_origins": settings.ALLOWED_ORIGINS,
        "traffic_shield": traffic_shield.snapshot(),
    }


@app.get("/api/security/status")
def get_security_status(_: dict = Depends(require_role(["admin"]))):
    return traffic_shield.snapshot()


@app.post("/api/login")
async def login(request: Request, data: LoginRequest, db: Session = Depends(get_db)):
    username = data.username.strip()
    password = data.password
    client_ip = get_client_ip_from_request(request)
    user = db.query(User).filter(User.username == username, User.is_active == True).first()
    if not user or not pwd_context.verify(password, user.hashed_password):
        log_action(db, username or "unknown", "LOGIN_FAIL", ip=client_ip)
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user.username, "role": user.role, "uid": user.id}, get_token_expire_hours(db))
    db.add(UserSession(user_id=user.id, ip=client_ip, user_agent=request.headers.get("user-agent")))
    log_action(db, user.username, "LOGIN_OK", ip=client_ip)
    db.commit()
    return {"token": token, "username": user.username, "role": user.role}


@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    live = Detection.deleted_at == None
    total = db.query(Detection).filter(live).count()
    paid = db.query(Detection).filter(Detection.payment_status == "PAID", live).count()
    blacklisted = db.query(Detection).filter(Detection.payment_status == "BLACKLISTED", live).count()
    unpaid = max(total - paid - blacklisted, 0)
    price = get_price_per_vehicle(db)
    revenue_raw = paid * price
    return {
        "total": total,
        "paid": paid,
        "blacklisted": blacklisted,
        "unpaid": unpaid,
        "revenue": f"{revenue_raw:,} VND",
        "revenue_raw": revenue_raw,
    }


@app.get("/api/vehicles")
def get_vehicles(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    search: str = "",
    status: str = "",
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    status = normalize_status(status)
    query = db.query(Detection).filter(Detection.deleted_at == None)
    if search:
        query = query.filter(Detection.plate_text.contains(search.strip().upper()))
    if status:
        query = query.filter(Detection.payment_status == status)

    total = query.count()
    rows = query.order_by(desc(Detection.timestamp), desc(Detection.id)).offset((page - 1) * limit).limit(limit).all()
    return {
        "data": [detection_payload(row) for row in rows],
        "total": total,
        "page": page,
        "pages": (total + limit - 1) // limit,
    }


@app.get("/api/vehicle/{plate}/history")
def get_vehicle_history(plate: str, db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    plate = normalize_plate(plate)
    detections = (
        db.query(Detection)
        .filter(Detection.plate_text == plate)
        .order_by(desc(Detection.timestamp), desc(Detection.id))
        .limit(50)
        .all()
    )
    changes = (
        db.query(DetectionHistory)
        .filter(DetectionHistory.plate_text == plate)
        .order_by(desc(DetectionHistory.changed_at), desc(DetectionHistory.id))
        .limit(50)
        .all()
    )
    if not detections and not changes:
        raise HTTPException(status_code=404, detail="Vehicle history not found")
    return {
        "plate": plate,
        "detections": [detection_payload(row) for row in detections],
        "changes": [
            {
                "old_status": row.old_status,
                "new_status": row.new_status,
                "changed_by": row.changed_by,
                "changed_at": iso_dt(row.changed_at),
            }
            for row in changes
        ],
    }


@app.get("/api/export/vehicles")
def export_vehicles(
    search: str = "",
    status: str = "",
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    status = normalize_status(status)
    query = db.query(Detection).filter(Detection.deleted_at == None)
    if search:
        query = query.filter(Detection.plate_text.contains(search.strip().upper()))
    if status:
        query = query.filter(Detection.payment_status == status)
    rows = query.order_by(desc(Detection.timestamp), desc(Detection.id)).limit(10000).all()

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["id", "timestamp", "plate", "vehicle_type", "status", "package", "confidence", "speed_kmh", "lane", "blacklist_reason"])
    for row in rows:
        writer.writerow([
            row.id,
            iso_dt(row.timestamp),
            row.plate_text,
            row.vehicle_type,
            row.payment_status,
            row.package_type,
            row.confidence,
            row.speed_kmh,
            row.lane,
            row.blacklist_reason,
        ])

    filename = f"trafficai_vehicles_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/reports/overview")
def get_report_overview(
    days: int = Query(14, ge=1, le=90),
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    price = get_price_per_vehicle(db)
    start_date = (datetime.utcnow() - timedelta(days=days - 1)).date()
    daily = {
        (start_date + timedelta(days=offset)).isoformat(): {
            "date": (start_date + timedelta(days=offset)).isoformat(),
            "total": 0,
            "paid": 0,
            "unpaid": 0,
            "blacklisted": 0,
            "revenue": 0,
        }
        for offset in range(days)
    }
    status_counts = {status: 0 for status in VALID_STATUSES}
    vehicle_types: dict[str, int] = {}

    rows = db.query(Detection).filter(Detection.deleted_at == None).all()
    for row in rows:
        status = row.payment_status if row.payment_status in VALID_STATUSES else "UNPAID"
        status_counts[status] += 1
        vehicle_type = row.vehicle_type or "UNKNOWN"
        vehicle_types[vehicle_type] = vehicle_types.get(vehicle_type, 0) + 1

        timestamp = parse_dt(row.timestamp)
        if not timestamp:
            continue
        key = timestamp.date().isoformat()
        if key not in daily:
            continue
        daily[key]["total"] += 1
        daily[key][status.lower()] += 1
        if status == "PAID":
            daily[key]["revenue"] += price

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "days": days,
        "daily": list(daily.values()),
        "status_counts": status_counts,
        "vehicle_types": dict(sorted(vehicle_types.items(), key=lambda item: item[1], reverse=True)),
    }


@app.post("/api/vehicle/{plate}/upgrade")
def upgrade_vehicle(plate: str, data: PackageUpdate, db: Session = Depends(get_db), current: dict = Depends(get_current_user)):
    plate = normalize_plate(plate)
    package = normalize_package(data.package)
    rows = db.query(Detection).filter(Detection.plate_text == plate, Detection.deleted_at == None).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    old_status = rows[0].payment_status
    for row in rows:
        row.payment_status = "PAID"
        row.package_type = package
        row.blacklist_reason = None
    db.add(DetectionHistory(detection_id=rows[0].id, plate_text=plate, old_status=old_status, new_status="PAID", changed_by=current["sub"]))
    log_action(db, current["sub"], "UPGRADE", detail=f"{plate} -> {package}")
    db.commit()
    return {"status": "success"}


@app.post("/api/vehicle/{plate}/cancel")
def cancel_package(plate: str, db: Session = Depends(get_db), current: dict = Depends(get_current_user)):
    plate = normalize_plate(plate)
    rows = db.query(Detection).filter(Detection.plate_text == plate, Detection.deleted_at == None).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    old_status = rows[0].payment_status
    for row in rows:
        row.payment_status = "UNPAID"
        row.package_type = "STANDARD"
        row.blacklist_reason = None
    db.add(DetectionHistory(detection_id=rows[0].id, plate_text=plate, old_status=old_status, new_status="UNPAID", changed_by=current["sub"]))
    log_action(db, current["sub"], "CANCEL_PACKAGE", detail=plate)
    db.commit()
    return {"status": "success"}


@app.post("/api/vehicle/{plate}/blacklist")
def blacklist_vehicle(
    plate: str,
    data: BlacklistUpdate,
    db: Session = Depends(get_db),
    current: dict = Depends(require_role(["admin", "operator"])),
):
    plate = normalize_plate(plate)
    reason = (data.reason or "Manual review").strip()[:255]
    rows = db.query(Detection).filter(Detection.plate_text == plate, Detection.deleted_at == None).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    old_status = rows[0].payment_status
    for row in rows:
        row.payment_status = "BLACKLISTED"
        row.blacklist_reason = reason
    db.add(DetectionHistory(detection_id=rows[0].id, plate_text=plate, old_status=old_status, new_status="BLACKLISTED", changed_by=current["sub"]))
    db.add(Alert(level="HIGH", message=f"Blacklisted vehicle: {plate}", plate=plate))
    log_action(db, current["sub"], "BLACKLIST", detail=f"{plate}: {reason}")
    db.commit()
    return {"status": "success"}


@app.post("/api/vehicle/{plate}/clear-blacklist")
def clear_blacklist_vehicle(
    plate: str,
    db: Session = Depends(get_db),
    current: dict = Depends(require_role(["admin", "operator"])),
):
    plate = normalize_plate(plate)
    rows = db.query(Detection).filter(Detection.plate_text == plate, Detection.deleted_at == None).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    old_status = rows[0].payment_status
    for row in rows:
        if row.payment_status == "BLACKLISTED":
            row.payment_status = "UNPAID"
        row.blacklist_reason = None
    db.add(DetectionHistory(detection_id=rows[0].id, plate_text=plate, old_status=old_status, new_status=rows[0].payment_status, changed_by=current["sub"]))
    log_action(db, current["sub"], "CLEAR_BLACKLIST", detail=plate)
    db.commit()
    return {"status": "success"}


@app.delete("/api/vehicle/{plate}")
def delete_vehicle(plate: str, db: Session = Depends(get_db), current: dict = Depends(require_role(["admin", "operator"]))):
    plate = normalize_plate(plate)
    rows = db.query(Detection).filter(Detection.plate_text == plate, Detection.deleted_at == None).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    for row in rows:
        row.deleted_at = datetime.utcnow()
    log_action(db, current["sub"], "SOFT_DELETE", detail=plate)
    db.commit()
    return {"status": "success"}


@app.post("/api/vehicle/{plate}/restore")
def restore_vehicle(plate: str, db: Session = Depends(get_db), current: dict = Depends(require_role(["admin"]))):
    plate = normalize_plate(plate)
    rows = db.query(Detection).filter(Detection.plate_text == plate, Detection.deleted_at != None).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Vehicle not found in trash")

    for row in rows:
        row.deleted_at = None
    log_action(db, current["sub"], "RESTORE", detail=plate)
    db.commit()
    return {"status": "success"}


@app.get("/api/trash")
def get_trash(db: Session = Depends(get_db), _: dict = Depends(require_role(["admin"]))):
    rows = db.query(Detection).filter(Detection.deleted_at != None).order_by(desc(Detection.deleted_at), desc(Detection.id)).limit(100).all()
    return [
        {
            "id": row.id,
            "plate_text": row.plate_text,
            "vehicle_type": row.vehicle_type,
            "deleted_at": iso_dt(row.deleted_at),
            "payment_status": row.payment_status,
        }
        for row in rows
    ]


@app.get("/api/config")
def get_config_all(db: Session = Depends(get_db), _: dict = Depends(require_role(["admin"]))):
    rows = db.query(SystemConfig).order_by(SystemConfig.key).all()
    return [{"key": row.key, "value": row.value, "updated_by": row.updated_by, "updated_at": iso_dt(row.updated_at)} for row in rows]


@app.put("/api/config/{key}")
def update_config(key: str, data: ConfigUpdate, db: Session = Depends(get_db), current: dict = Depends(require_role(["admin"]))):
    if key not in CONFIG_KEYS:
        raise HTTPException(status_code=400, detail="This config key is not editable from the dashboard")

    value = data.value
    if key == "system_name":
        value = str(value).strip()
        if not value or len(value) > 80:
            raise HTTPException(status_code=400, detail="System name must be 1-80 characters")
    elif key == "price_per_vehicle":
        try:
            value = str(max(0, int(value)))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Price must be a positive integer") from exc
    elif key == "token_expire_h":
        try:
            hours = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Token expiry must be an integer") from exc
        if hours < 1 or hours > 168:
            raise HTTPException(status_code=400, detail="Token expiry must be between 1 and 168 hours")
        value = str(hours)

    config = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config key not found")
    config.value = str(value)
    config.updated_by = current["sub"]
    config.updated_at = datetime.utcnow()
    log_action(db, current["sub"], "UPDATE_CONFIG", detail=key)
    db.commit()
    return {"status": "success"}


@app.get("/api/audit")
def get_audit(db: Session = Depends(get_db), _: dict = Depends(require_role(["admin"]))):
    rows = db.query(AuditLog).order_by(desc(AuditLog.id)).limit(200).all()
    return [
        {
            "timestamp": iso_dt(row.timestamp),
            "username": row.username,
            "action": row.action,
            "detail": row.detail,
            "ip": row.ip,
        }
        for row in rows
    ]


class ConnectionManager:
    def __init__(self):
        self.active = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active:
            self.active.remove(websocket)

    async def broadcast(self, message: dict):
        dead = []
        for connection in self.active:
            try:
                await connection.send_json(message)
            except RuntimeError:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)


manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_ip = get_client_ip_from_websocket(websocket)
    allowed, reason = traffic_shield.connect_ws(client_ip)
    if not allowed:
        await websocket.close(code=1008, reason=reason)
        return

    connected = False
    try:
        await manager.connect(websocket)
        connected = True
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if connected:
            manager.disconnect(websocket)
        traffic_shield.disconnect_ws(client_ip)


@app.post("/api/internal/notify")
async def notify_new_detection(payload: dict):
    await manager.broadcast({
        "event": "new_detection",
        "data": payload
    })
    return {"status": "success"}


# --- ADVANCED AI CONTROL AND STREAMING ENDPOINTS ---

@app.post("/api/ai/upload")
async def upload_video(file: UploadFile = File(...), current: dict = Depends(require_role(["admin", "operator"]))):
    if not file.filename.endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Only MP4 videos are allowed")
    
    uploads_dir = os.path.join("static", "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    
    safe_name = f"{uuid.uuid4().hex}_{re.sub(r'[^a-zA-Z0-9_.-]', '_', file.filename)}"
    file_path = os.path.join(uploads_dir, safe_name)
    
    with open(file_path, "wb") as buffer:
        content = await file.read()
        buffer.write(content)
        
    return {"status": "success", "filename": safe_name, "path": f"static/uploads/{safe_name}"}


@app.get("/api/ai/videos")
def list_videos(current: dict = Depends(get_current_user)):
    uploads_dir = os.path.join("static", "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    files = []
    for f in os.listdir(uploads_dir):
        if f.endswith(".mp4"):
            files.append({
                "filename": f,
                "path": f"static/uploads/{f}"
            })
    return files


@app.post("/api/ai/start")
def start_ai(source: str = Query(...), current: dict = Depends(require_role(["admin", "operator"]))):
    global ai_engine_running, ai_engine_thread, latest_processed_frame
    
    if ai_engine_running:
        raise HTTPException(status_code=400, detail="AI engine is already running")
        
    # Resolve source path
    if source.isdigit():
        resolved_source = int(source)
    elif source.startswith("rtsp://") or source.startswith("http://") or source.startswith("https://"):
        resolved_source = source
    else:
        # Check in static/uploads or main folder
        uploads_path = os.path.join("static", "uploads", source)
        if os.path.exists(uploads_path):
            resolved_source = uploads_path
        elif os.path.exists(source):
            resolved_source = source
        else:
            raise HTTPException(status_code=404, detail=f"Source video not found: {source}")
            
    # --- INSTANT-START OPTIMIZATION ---
    # Draw a premium glowing loading panel to publish instantly so the UI has zero connection lag
    try:
        import cv2
        import numpy as np
        loading_img = np.zeros((720, 1280, 3), dtype=np.uint8)
        # Background dark matching premium neon HSL theme
        loading_img[:] = (10, 7, 5)
        # Draw dark panel border
        cv2.rectangle(loading_img, (40, 40), (1240, 680), (33, 27, 21), 2)
        # Glowing Teal/Emerald details
        cv2.putText(loading_img, "KBN TRAFFIC-AI ENGINE v6.5", (100, 240), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (191, 212, 45), 3) # Teal/Emerald
        cv2.putText(loading_img, "DANG KET NOI CAMERA BOT...", (100, 320), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        
        if isinstance(resolved_source, int):
            src_text = "Nguon: Camera may tinh (Cong 0)" if resolved_source == 0 else "Nguon: Camera dien thoai / Cam phu (Cong 1)"
        else:
            src_text = f"Nguon: {source}"
        cv2.putText(loading_img, src_text, (100, 400), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (184, 163, 148), 2)
        cv2.putText(loading_img, "Vui long doi giay, dang khoi chay YOLOv8 + OpenVINO...", (100, 480), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (94, 197, 34), 2) # Green
        
        _, jpeg = cv2.imencode('.jpg', loading_img)
        with latest_frame_lock:
            latest_processed_frame = jpeg.tobytes()
    except Exception as ex:
        print(f"Error drawing loading frame: {ex}")
    # ----------------------------------

    # Start the engine thread
    ai_engine_thread = TrafficAIEngine(resolved_source)
    ai_engine_running = True
    ai_engine_thread.start()
    
    return {"status": "success", "message": f"Started AI Engine on {source}"}


@app.post("/api/ai/stop")
def stop_ai(current: dict = Depends(require_role(["admin", "operator"]))):
    global ai_engine_running, ai_engine_thread, latest_processed_frame
    
    if not ai_engine_running or ai_engine_thread is None:
        return {"status": "success", "message": "AI engine was not running"}
        
    # Stop thread
    ai_engine_thread.stop_event.set()
    ai_engine_thread.join(timeout=2.0)
    
    ai_engine_running = False
    ai_engine_thread = None
    with latest_frame_lock:
        latest_processed_frame = None
        
    return {"status": "success", "message": "Stopped AI Engine"}


@app.get("/api/ai/status")
def get_ai_status(current: dict = Depends(get_current_user)):
    global ai_engine_running, ai_engine_thread
    
    if not ai_engine_running or ai_engine_thread is None:
        return {
            "running": False,
            "status": "Idle",
            "fps": 0,
            "processed_frames": 0
        }
        
    return {
        "running": True,
        "status": ai_engine_thread.status,
        "fps": ai_engine_thread.fps,
        "processed_frames": ai_engine_thread.processed_frames
    }


@app.get("/api/stream")
def video_stream():
    def frame_generator():
        global latest_processed_frame, ai_engine_running
        last_sent_frame = None
        while ai_engine_running:
            frame = None
            with latest_frame_lock:
                frame = latest_processed_frame
            if frame is not None and frame != last_sent_frame:
                last_sent_frame = frame
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            else:
                time.sleep(0.03)
        # Yield empty bytes when server stops or AI stops to close connection nicely
        yield b''
            
    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/api/reports/charts")
def get_reports_charts(
    days: int = Query(14, ge=1, le=90),
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    price = get_price_per_vehicle(db)
    start_date = (datetime.utcnow() - timedelta(days=days - 1)).date()
    daily = {
        (start_date + timedelta(days=offset)).isoformat(): {
            "date": (start_date + timedelta(days=offset)).isoformat(),
            "total": 0,
            "paid": 0,
            "unpaid": 0,
            "blacklisted": 0,
            "revenue": 0,
        }
        for offset in range(days)
    }
    
    vehicle_types = {"CAR": 0, "TRUCK": 0, "BUS": 0}
    
    rows = db.query(Detection).filter(Detection.deleted_at == None).all()
    for row in rows:
        status = row.payment_status if row.payment_status in VALID_STATUSES else "UNPAID"
        v_type = (row.vehicle_type or "CAR").upper()
        if v_type in vehicle_types:
            vehicle_types[v_type] += 1
        else:
            vehicle_types[v_type] = vehicle_types.get(v_type, 0) + 1
            
        timestamp = parse_dt(row.timestamp)
        if not timestamp:
            continue
        key = timestamp.date().isoformat()
        if key not in daily:
            continue
        daily[key]["total"] += 1
        daily[key][status.lower()] += 1
        if status == "PAID":
            daily[key]["revenue"] += price
            
    sorted_vehicle_types = dict(sorted(vehicle_types.items(), key=lambda item: item[1], reverse=True))
    
    return {
        "days": days,
        "daily": list(daily.values()),
        "vehicle_types": sorted_vehicle_types
    }


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", context={})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001,
        limit_concurrency=settings.UVICORN_LIMIT_CONCURRENCY,
        timeout_keep_alive=settings.UVICORN_TIMEOUT_KEEP_ALIVE,
        proxy_headers=settings.TRUST_PROXY_HEADERS,
    )
