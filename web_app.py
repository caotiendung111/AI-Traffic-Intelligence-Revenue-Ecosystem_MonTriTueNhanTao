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
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
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
    ]
    for sql in migrations:
        try:
            cur.execute(sql)
        except sqlite3.OperationalError:
            pass
    cur.execute("UPDATE detections SET payment_status = 'UNPAID' WHERE payment_status IS NULL OR payment_status = ''")
    cur.execute("UPDATE detections SET package_type = 'STANDARD' WHERE package_type IS NULL OR package_type = '' OR package_type = 'NONE'")
    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate_legacy_schema()
    Base.metadata.create_all(bind=engine)

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


app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


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
