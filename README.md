# 🚦 Integrated AI Traffic Intelligence & Automated Revenue Management Ecosystem
### *Môn Trí Tuệ Nhân Tạo — Hệ Thống Quản Lý Giao Thông Thông Minh*

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![OpenVINO](https://img.shields.io/badge/OpenVINO-AI_Engine-0071C5?style=for-the-badge&logo=intel&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-Database-003B57?style=for-the-badge&logo=sqlite&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)

> **Hệ sinh thái toàn diện** tích hợp Trí tuệ Nhân tạo để nhận diện biển số xe tự động, giám sát giao thông thời gian thực và quản lý doanh thu tự động dành cho các cơ quan quản lý giao thông.

</div>

---

## 📌 Tổng quan dự án

Dự án xây dựng một **nền tảng quản lý giao thông thế hệ mới** hoàn chỉnh gồm hai lớp chính:

1. **Lớp AI (Nhận diện):** Sử dụng YOLOv8 kết hợp OpenVINO để nhận diện phương tiện và đọc biển số xe Việt Nam với độ chính xác cao trong thời gian thực.
2. **Lớp Web (Quản lý):** Dashboard doanh nghiệp (Enterprise) được xây dựng bằng FastAPI, cho phép quản lý phương tiện, thu phí, phân tích dữ liệu và kiểm soát bảo mật.

---

## ✨ Tính năng nổi bật

| Tính năng | Mô tả |
|-----------|-------|
| 🎯 **ANPR Thông minh** | Nhận diện biển số Việt Nam 1 dòng & 2 dòng bằng AI |
| ⚡ **Xử lý thời gian thực** | Pipeline OpenVINO tối ưu cho CPU, không cần GPU |
| 📊 **Dashboard Enterprise** | Giao diện Glassmorphism cao cấp, PWA-ready |
| 💰 **Quản lý Doanh thu** | Tự động tính phí, theo dõi trạng thái thanh toán |
| 🔒 **Bảo mật nhiều lớp** | JWT Authentication, RBAC, Audit Log đầy đủ |
| 🚫 **Blacklist System** | Tự động cảnh báo phương tiện trong danh sách đen |
| 📈 **Phân tích & Báo cáo** | Thống kê theo ngày, xuất CSV, lịch sử phát hiện |
| 🔌 **WebSocket Live** | Cập nhật dữ liệu tức thì không cần refresh trang |

---

## 🏗️ Kiến trúc hệ thống

```
AI-Traffic-Intelligence-Revenue-Ecosystem/
│
├── main.py                  # Pipeline AI chính (YOLO + OpenVINO + OCR)
├── web_app.py               # FastAPI backend (API + WebSocket + Auth)
├── database_manager.py      # Lớp quản lý cơ sở dữ liệu SQLite
├── config.py                # Cấu hình hệ thống (Pydantic Settings)
├── requirements.txt         # Danh sách thư viện
│
├── static/
│   ├── css/                 # Giao diện Premium Glassmorphism
│   ├── js/                  # Frontend logic
│   ├── manifest.json        # PWA Manifest
│   └── sw.js                # Service Worker
│
└── templates/
    └── index.html           # Template Dashboard chính
```

---

## 🤖 Stack Công nghệ

**Backend & AI:**
- `FastAPI` — Web framework hiệu năng cao (ASGI)
- `YOLOv8` (Ultralytics) — Phát hiện phương tiện
- `OpenVINO` — Tối ưu hóa suy luận AI trên CPU Intel
- `EasyOCR / Tesseract` — Đọc ký tự biển số
- `SQLAlchemy` + `SQLite` — ORM & Cơ sở dữ liệu
- `APScheduler` — Lập lịch tác vụ tự động
- `JWT` + `passlib` — Xác thực & bảo mật

**Frontend:**
- Vanilla JS + CSS Glassmorphism
- WebSocket (cập nhật realtime)
- Progressive Web App (PWA)

---

## 🚀 Hướng dẫn cài đặt

### 1. Clone repository
```bash
git clone https://github.com/caotiendung111/AI-Traffic-Intelligence-Revenue-Ecosystem_MonTriTueNhanTao.git
cd AI-Traffic-Intelligence-Revenue-Ecosystem_MonTriTueNhanTao
```

### 2. Cài đặt thư viện
```bash
pip install -r requirements.txt
```

### 3. Tải model AI (thủ công)
> Do giới hạn kích thước file GitHub, model cần tải riêng:
- **YOLOv8n:** Tải tại [Ultralytics Releases](https://github.com/ultralytics/assets/releases) → đặt vào thư mục gốc với tên `yolov8n.pt`
- **OpenVINO model (biển số):** Đặt vào thư mục `PBL4_Project/`

### 4. Chạy hệ thống

**Chạy Pipeline AI (nhận diện video):**
```bash
python main.py
```

**Chạy Web Dashboard:**
```bash
python web_app.py
# Truy cập: http://localhost:8001
# Tài khoản mặc định: admin / 123456
```

---

## 📸 Giao diện hệ thống

> Dashboard Enterprise với giao diện Hyper-Precision Glass — Glassmorphism cao cấp, Dark Mode chuyên nghiệp.

---

## 👥 Nhóm phát triển

| Thành viên | Vai trò |
|------------|---------|
| **Cao Tiến Dũng** | Trưởng nhóm · AI Pipeline · Backend |

📚 **Môn học:** Trí Tuệ Nhân Tạo  
🏫 **Trường:** *(Điền tên trường của bạn)*  
📅 **Năm học:** 2025 - 2026

---

## 📄 Giấy phép

Dự án được phát hành theo giấy phép [MIT License](LICENSE).

---

<div align="center">
  <sub>Built with ❤️ using FastAPI + YOLOv8 + OpenVINO</sub>
</div>
