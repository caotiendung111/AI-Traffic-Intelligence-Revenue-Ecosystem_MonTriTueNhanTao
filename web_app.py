"""
AI Traffic Monitoring System — v6.3.0 "The Masterpiece"
Integrated: Full v6.1 Features + v6.2 Aesthetic & Cancel Logic + Enhanced Security
"""

import os
import json
import csv
import io
import jwt
import time
import uuid
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Annotated, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks, Depends, status, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, desc, func, Boolean, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from apscheduler.schedulers.background import BackgroundScheduler

from config import settings

# ─────────────────────────────────────────────
# DATABASE MODELS (RESTORED & EXPANDED)
# ─────────────────────────────────────────────
engine = create_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id              = Column(Integer, primary_key=True, index=True)
    username        = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role            = Column(String, default="operator")
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

class UserSession(Base):
    __tablename__ = "user_sessions"
    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"))
    ip          = Column(String)
    user_agent  = Column(String)
    created_at  = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)
    is_active   = Column(Boolean, default=True)

class Detection(Base):
    __tablename__ = "detections"
    id              = Column(Integer, primary_key=True, index=True)
    timestamp       = Column(DateTime, default=datetime.utcnow)
    plate_text      = Column(String, index=True)
    vehicle_type    = Column(String)
    confidence      = Column(Float)
    payment_status  = Column(String, default="UNPAID")
    package_type    = Column(String, default="STANDARD")
    blacklist_reason = Column(String, nullable=True)
    lane            = Column(String, nullable=True)
    speed_kmh       = Column(Float, nullable=True)
    deleted_at      = Column(DateTime, nullable=True)

class DetectionHistory(Base):
    __tablename__ = "detection_history"
    id           = Column(Integer, primary_key=True, index=True)
    detection_id = Column(Integer, ForeignKey("detections.id"))
    plate_text   = Column(String)
    old_status   = Column(String)
    new_status   = Column(String)
    changed_by   = Column(String)
    changed_at   = Column(DateTime, default=datetime.utcnow)

class SystemConfig(Base):
    __tablename__ = "system_config"
    key        = Column(String, primary_key=True, unique=True)
    value      = Column(String)
    updated_by = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow)

class DailySummary(Base):
    __tablename__ = "daily_summaries"
    id          = Column(Integer, primary_key=True, index=True)
    date        = Column(String, unique=True)
    total       = Column(Integer)
    paid        = Column(Integer)
    blacklisted = Column(Integer)
    revenue     = Column(Integer)

class UserPreference(Base):
    __tablename__ = "user_preferences"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"))
    pref_key   = Column(String)
    pref_value = Column(String)

class Alert(Base):
    __tablename__ = "alerts"
    id        = Column(Integer, primary_key=True, index=True)
    level     = Column(String)
    message   = Column(String)
    plate     = Column(String, nullable=True)
    resolved  = Column(Boolean, default=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

class AuditLog(Base):
    __tablename__ = "audit_log"
    id        = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    username  = Column(String)
    action    = Column(String)
    detail    = Column(String)
    ip        = Column(String)

# ─────────────────────────────────────────────
# CORE LOGIC & SECURITY
# ─────────────────────────────────────────────
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")
scheduler = BackgroundScheduler()

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def get_config(db: Session, key: str, default: str = "") -> str:
    cfg = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    return cfg.value if cfg else default

def log_action(db: Session, user: str, action: str, detail: str = "", ip: str = ""):
    log = AuditLog(username=user, action=action, detail=detail, ip=ip)
    db.add(log); db.commit()

# ─────────────────────────────────────────────
# LIFESPAN (RESTORED FULL LOGIC)
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    conn = sqlite3.connect("traffic_monitoring.db")
    cur  = conn.cursor()
    try: cur.execute("ALTER TABLE detections ADD COLUMN deleted_at DATETIME")
    except: pass
    conn.commit(); conn.close()
    
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    # Seed Config
    defaults = {
        "price_per_vehicle": str(settings.PRICE_PER_VEHICLE),
        "token_expire_h": str(settings.TOKEN_EXPIRE_H),
        "system_name": settings.SYSTEM_NAME
    }
    for k, v in defaults.items():
        if not db.query(SystemConfig).filter(SystemConfig.key == k).first():
            db.add(SystemConfig(key=k, value=v, updated_by="SYSTEM"))
    if not db.query(User).filter(User.username == "admin").first():
        db.add(User(username="admin", hashed_password=pwd_context.hash("123456"), role="admin"))
    db.commit(); db.close()
    
    # Start Scheduler
    if not scheduler.running:
        scheduler.start()
    
    yield
    # Shutdown
    scheduler.shutdown()

app = FastAPI(title=settings.SYSTEM_NAME, version="6.3.0", lifespan=lifespan)

# ─────────────────────────────────────────────
# MIDDLEWARES (RESTORED)
# ─────────────────────────────────────────────
@app.middleware("http")
async def request_logging_and_id_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    start_time = time.time()
    response = await call_next(request)
    process_time = (time.time() - start_time) * 1000
    print(f"ID: {request_id} | {request.method} {request.url.path} | Status: {response.status_code} | {process_time:.2f}ms")
    response.headers["X-Request-ID"] = request_id
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
# AUTH HELPERS
# ─────────────────────────────────────────────
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(hours=8)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except:
        raise HTTPException(status_code=401, detail="Invalid token")

def require_role(roles: List[str]):
    def role_checker(user: dict = Depends(get_current_user)):
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="Permission denied")
        return user
    return role_checker

# ─────────────────────────────────────────────
# API ENDPOINTS (FULL RESTORED + NEW)
# ─────────────────────────────────────────────

@app.get("/api/health")
def health_check():
    return {"status": "healthy", "version": "6.3.0", "engine": "Ultimate"}

@app.post("/api/login")
async def login(request: Request, data: dict, db: Session = Depends(get_db)):
    uname, pwd = data.get("username"), data.get("password")
    user = db.query(User).filter(User.username == uname).first()
    if not user or not pwd_context.verify(pwd, user.hashed_password):
        log_action(db, uname or "unknown", "LOGIN_FAIL", ip=request.client.host)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    token = create_access_token({"sub": user.username, "role": user.role, "uid": user.id})
    db.add(UserSession(user_id=user.id, ip=request.client.host, user_agent=request.headers.get("user-agent")))
    log_action(db, user.username, "LOGIN_OK", ip=request.client.host)
    db.commit()
    return {"token": token, "username": user.username, "role": user.role}

@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    total = db.query(Detection).filter(Detection.deleted_at == None).count()
    paid = db.query(Detection).filter(Detection.payment_status == "PAID", Detection.deleted_at == None).count()
    black = db.query(Detection).filter(Detection.payment_status == "BLACKLISTED", Detection.deleted_at == None).count()
    unpaid = total - paid - black
    
    price = int(get_config(db, "price_per_vehicle", "50000"))
    revenue_raw = paid * price
    
    return {
        "total": total, "paid": paid, "blacklisted": black, "unpaid": unpaid,
        "revenue": f"{revenue_raw:,} VNĐ",
        "revenue_raw": revenue_raw
    }

@app.get("/api/vehicles")
def get_vehicles(
    page: int = 1, limit: int = 50, search: str = "", status: str = "",
    db: Session = Depends(get_db), _: dict = Depends(get_current_user)
):
    query = db.query(Detection).filter(Detection.deleted_at == None)
    if search: query = query.filter(Detection.plate_text.contains(search))
    if status: query = query.filter(Detection.payment_status == status)
    
    total = query.count()
    rows = query.order_by(desc(Detection.timestamp)).offset((page - 1) * limit).limit(limit).all()
    
    return {
        "data": [
            {"id": r.id, "plate": r.plate_text, "type": r.vehicle_type, "timestamp": r.timestamp.isoformat(), 
             "status": r.payment_status, "package": r.package_type, "confidence": r.confidence} 
            for r in rows
        ],
        "total": total, "page": page, "pages": (total // limit) + (1 if total % limit > 0 else 0)
    }

@app.post("/api/vehicle/{plate}/upgrade")
def upgrade_vehicle(plate: str, data: dict, db: Session = Depends(get_db), current: dict = Depends(get_current_user)):
    pkg = data.get("package", "STANDARD")
    recs = db.query(Detection).filter(Detection.plate_text == plate, Detection.deleted_at == None).all()
    if not recs: raise HTTPException(status_code=404)
    old_s = recs[0].payment_status
    for r in recs:
        r.payment_status = "PAID"
        r.package_type = pkg
    db.add(DetectionHistory(detection_id=recs[0].id, plate_text=plate, old_status=old_s, new_status="PAID", changed_by=current["sub"]))
    db.commit()
    return {"status": "success"}

@app.post("/api/vehicle/{plate}/cancel")
def cancel_package(plate: str, db: Session = Depends(get_db), current: dict = Depends(get_current_user)):
    recs = db.query(Detection).filter(Detection.plate_text == plate, Detection.deleted_at == None).all()
    if not recs: raise HTTPException(status_code=404)
    old_s = recs[0].payment_status
    for r in recs:
        r.payment_status = "UNPAID"
        r.package_type = "STANDARD"
    db.add(DetectionHistory(detection_id=recs[0].id, plate_text=plate, old_status=old_s, new_status="UNPAID", changed_by=current["sub"]))
    db.commit()
    return {"status": "success"}

@app.delete("/api/vehicle/{plate}")
def delete_vehicle(plate: str, db: Session = Depends(get_db), current: dict = Depends(require_role(["admin", "operator"]))):
    recs = db.query(Detection).filter(Detection.plate_text == plate, Detection.deleted_at == None).all()
    if not recs: raise HTTPException(status_code=404)
    for r in recs: r.deleted_at = datetime.utcnow()
    log_action(db, current["sub"], "SOFT_DELETE", detail=plate)
    db.commit()
    return {"status": "success"}

@app.get("/api/trash")
def get_trash(db: Session = Depends(get_db), _: dict = Depends(require_role(["admin"]))):
    return db.query(Detection).filter(Detection.deleted_at != None).limit(100).all()

@app.get("/api/config")
def get_config_all(db: Session = Depends(get_db), _: dict = Depends(require_role(["admin"]))):
    return db.query(SystemConfig).all()

@app.put("/api/config/{key}")
def update_config(key: str, data: dict, db: Session = Depends(get_db), current: dict = Depends(require_role(["admin"]))):
    cfg = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if not cfg: raise HTTPException(status_code=404)
    cfg.value = str(data.get("value"))
    cfg.updated_by = current["sub"]
    cfg.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "success"}

@app.get("/api/audit")
def get_audit(db: Session = Depends(get_db), _: dict = Depends(require_role(["admin"]))):
    return db.query(AuditLog).order_by(desc(AuditLog.id)).limit(200).all()

# WebSocket Manager
class ConnectionManager:
    def __init__(self): self.active = []
    async def connect(self, ws: WebSocket): await ws.accept(); self.active.append(ws)
    def disconnect(self, ws: WebSocket): 
        if ws in self.active: self.active.remove(ws)
    async def broadcast(self, msg: dict):
        for c in self.active:
            try: await c.send_json(msg)
            except: pass

manager = ConnectionManager()
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: manager.disconnect(ws)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", context={})

startup_time = time.time()
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
