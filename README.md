# TrafficAI - AI Car Monitoring

He thong nhan dien bien so va quan ly luot xe cho do an AI_CAR/PBL4.

## Tinh nang chinh

- Nhan dien xe va bien so tu video bang YOLOv8, OpenVINO va EasyOCR.
- Ghi lich su nhan dien vao SQLite.
- Dashboard FastAPI de xem thong ke, tim kiem, loc trang thai va quan ly thanh toan.
- Soft-delete, Trash Bin va khoi phuc ban ghi.
- Blacklist/Clear blacklist kem ly do va audit log.
- Xem lich su tung bien so: cac lan nhan dien va cac lan doi trang thai.
- Reports tab: thong ke theo ngay va thong ke loai xe.
- Export CSV theo bo loc hien tai.
- Audit log cho dang nhap, cap nhat goi, blacklist, xoa/khoi phuc va cau hinh.
- TrafficShield: rate limit theo IP, login limit rieng, chan tam IP, gioi han body request va WebSocket.
- PWA co cache noi bo cho giao dien chinh.
- Tu migrate schema cu khi khoi dong, khong can xoa database cu.

## Cau truc quan trong

```text
main.py                         Pipeline AI xu ly video
database_manager.py             Lop ghi/lay du lieu SQLite cho pipeline
web_app.py                      FastAPI backend va dashboard
config.py                       Cau hinh he thong
templates/index.html            Giao dien dashboard
static/css/premium_styles.css   Style dashboard
static/js/premium_logic.js      Logic frontend
Train_Final_PBL4/weights/       Model bien so da train
traffic_monitoring.db           Database SQLite
requirements-ai.txt             Dependency rieng cho pipeline AI
```

> Pham vi AI_CAR khong bao gom cac thu muc Fireguard/Freshman.

## Cai dat

```bash
pip install -r requirements.txt
```

Neu chay pipeline AI, cai them cac thu vien AI can thiet neu may chua co:

```bash
pip install -r requirements-ai.txt
```

## Chay dashboard

```bash
python web_app.py
```

Mo trinh duyet tai:

```text
http://localhost:8001
```

Tai khoan mac dinh:

```text
admin / 123456
```

## Chay pipeline nhan dien

```bash
python main.py
```

Mac dinh pipeline doc video `test_bot_cutted.mp4`, ghi ket qua vao `traffic_monitoring.db` va xuat file `output_traffic_monitoring.avi`.

## Cau hinh

Co the sua truc tiep trong `config.py` hoac dat bien moi truong:

- `SECRET_KEY`
- `TOKEN_EXPIRE_H`
- `DATABASE_URL`
- `PRICE_PER_VEHICLE`
- `SYSTEM_NAME`
- `ALLOWED_ORIGINS`
- `RATE_LIMIT_ENABLED`
- `RATE_LIMIT_REQUESTS`
- `RATE_LIMIT_WINDOW_SECONDS`
- `LOGIN_RATE_LIMIT_REQUESTS`
- `LOGIN_RATE_LIMIT_WINDOW_SECONDS`
- `RATE_LIMIT_BAN_SECONDS`
- `MAX_REQUEST_BODY_BYTES`
- `MAX_WS_PER_IP`
- `MAX_WS_TOTAL`
- `TRUST_PROXY_HEADERS`
- `UVICORN_LIMIT_CONCURRENCY`
- `UVICORN_TIMEOUT_KEEP_ALIVE`

Dashboard cung co tab `System Config` de sua mot so cau hinh runtime.

## API quan trong

- `POST /api/login`
- `GET /api/stats`
- `GET /api/vehicles`
- `GET /api/vehicle/{plate}/history`
- `POST /api/vehicle/{plate}/upgrade`
- `POST /api/vehicle/{plate}/cancel`
- `POST /api/vehicle/{plate}/blacklist`
- `POST /api/vehicle/{plate}/clear-blacklist`
- `DELETE /api/vehicle/{plate}`
- `POST /api/vehicle/{plate}/restore`
- `GET /api/reports/overview`
- `GET /api/export/vehicles`
- `GET /api/system/status`
- `GET /api/security/status`

## TrafficShield

Backend co lop bao ve muc ung dung de giam request rac:

- Gioi han request theo IP cho toan bo API.
- Gioi han rieng endpoint dang nhap de giam brute-force.
- Tu dong chan tam IP khi vuot nguong.
- Tu choi request co `Content-Length` qua lon.
- Gioi han so ket noi WebSocket tren moi IP va tong he thong.
- Gioi han concurrency va keep-alive khi chay bang `python web_app.py`.

Luu y: lop nay giup on dinh ung dung khi bi spam nhe/vua. Neu public Internet va bi DDoS that su, nen dat them Nginx/Cloudflare/firewall truoc FastAPI.

## Kiem tra nhanh

Sau khi chay `python web_app.py`, co the kiem tra:

```bash
curl http://localhost:8001/api/health
```

Neu dung dashboard, dang nhap bang `admin / 123456`, mo tab `Reports`, `Records`, `Trash`, `System Config` de kiem tra cac tinh nang quan tri.
