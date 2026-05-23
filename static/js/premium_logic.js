const State = {
    user: null,
    ws: null,
    activeTab: "dashboard",
    page: 1,
    loading: false,
    hasMore: true,
    search: "",
    status: "",
};

document.addEventListener("DOMContentLoaded", () => {
    lucide.createIcons();
    setupInfiniteScroll();
    setupActionDelegation();
    updateClock();
    setInterval(updateClock, 1000);

    const saved = localStorage.getItem("trafficai_user");
    if (saved) {
        State.user = JSON.parse(saved);
        document.getElementById("login-screen").style.display = "none";
        initSystem();
    }
});

document.addEventListener("keydown", event => {
    if (event.key === "Escape") closeModal();
});

async function doLogin(event) {
    event?.preventDefault();
    const username = document.getElementById("login-user").value.trim();
    const password = document.getElementById("login-pass").value;
    if (!username || !password) {
        showToast("Vui long nhap username va password", "warning");
        return;
    }

    const res = await fetch("/api/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
        headers: { "Content-Type": "application/json" },
    });

    if (!res.ok) {
        showToast("Dang nhap that bai", "danger");
        return;
    }

    State.user = await res.json();
    localStorage.setItem("trafficai_user", JSON.stringify(State.user));
    document.getElementById("login-screen").style.display = "none";
    initSystem();
}

function initSystem() {
    setupWS();
    setupDragAndDrop();
    loadVideos();
    checkCurrentAIStatus();
    refreshAll();
}

function logout() {
    localStorage.removeItem("trafficai_user");
    location.reload();
}

function authHeaders() {
    return {
        "Content-Type": "application/json",
        ...(State.user?.token ? { Authorization: `Bearer ${State.user.token}` } : {}),
    };
}

async function apiFetch(url, opts = {}) {
    try {
        const res = await fetch(url, { ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) } });
        if (res.status === 401) {
            showToast("Phien dang nhap da het han", "warning");
            logout();
            return null;
        }
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            showToast(body.detail || "API request failed", "danger");
        }
        return res;
    } catch (err) {
        showToast("Khong ket noi duoc backend", "danger");
        return null;
    }
}

function refreshAll() {
    State.page = 1;
    State.hasMore = true;
    syncStats();
    if (State.activeTab === "records") loadRecords();
    else if (State.activeTab === "reports") loadReports();
    else syncVehicles(true);
}

async function syncStats() {
    const res = await apiFetch("/api/stats");
    if (!res?.ok) return;
    const stats = await res.json();
    setText("st-total", stats.total);
    setText("st-paid", stats.paid);
    setText("st-unpaid", stats.unpaid);
    setText("st-rev", stats.revenue);
    setText("st-black", stats.blacklisted);
}

async function syncVehicles(clear = true) {
    if (State.loading) return;
    State.loading = true;
    const url = `/api/vehicles?page=${State.page}&limit=50&search=${encodeURIComponent(State.search)}&status=${encodeURIComponent(State.status)}`;
    const res = await apiFetch(url);
    State.loading = false;
    if (!res?.ok) return;

    const data = await res.json();
    State.hasMore = data.page < data.pages;
    renderVehicles(data.data, clear);
}

function renderVehicles(data, clear) {
    const container = document.getElementById("vehicle-rows");
    if (clear) container.innerHTML = "";
    if (!data.length && clear) {
        container.innerHTML = emptyRow("Chua co du lieu nhan dien", 7);
        return;
    }

    container.insertAdjacentHTML("beforeend", data.map(row => {
        const isBlacklisted = row.status === "BLACKLISTED";
        const imgHtml = row.crop_path 
            ? `<img src="/${row.crop_path}" class="table-thumbnail" onclick="viewPhoto('/${row.crop_path}', '${escapeAttr(row.plate)}')" alt="Plate">` 
            : `<div class="no-photo-badge"><i data-lucide="image-off"></i></div>`;
        return `
            <tr id="row-${row.id}" class="fade-in">
                <td>${imgHtml}</td>
                <td><span class="plate-badge">${escapeHtml(row.plate || "N/A")}</span></td>
                <td>${escapeHtml(row.type || "UNKNOWN")}</td>
                <td>${formatTime(row.timestamp)}</td>
                <td><span class="status-tag ${(row.status || "").toLowerCase()}">${escapeHtml(row.status || "N/A")}</span></td>
                <td>${escapeHtml(row.package || "STANDARD")}</td>
                <td>
                    <div class="action-group">
                        <button class="btn-action" data-action="upgrade" data-plate="${escapeAttr(row.plate)}" title="Upgrade"><i data-lucide="zap"></i></button>
                        <button class="btn-action" data-action="cancel" data-plate="${escapeAttr(row.plate)}" title="Cancel package"><i data-lucide="rotate-ccw"></i></button>
                        <button class="btn-action ${isBlacklisted ? "" : "danger"}" data-action="${isBlacklisted ? "clear-blacklist" : "blacklist"}" data-plate="${escapeAttr(row.plate)}" title="${isBlacklisted ? "Clear blacklist" : "Blacklist"}"><i data-lucide="${isBlacklisted ? "shield-check" : "shield-alert"}"></i></button>
                        <button class="btn-action" data-action="history" data-plate="${escapeAttr(row.plate)}" title="History"><i data-lucide="history"></i></button>
                        <button class="btn-action danger" data-action="delete" data-plate="${escapeAttr(row.plate)}" title="Delete"><i data-lucide="trash"></i></button>
                    </div>
                </td>
            </tr>
        `;
    }).join(""));
    lucide.createIcons();
}

async function loadRecords() {
    const url = `/api/vehicles?page=1&limit=100&search=${encodeURIComponent(State.search)}&status=${encodeURIComponent(State.status)}`;
    const res = await apiFetch(url);
    if (!res?.ok) return;
    const payload = await res.json();
    setText("record-count", `${payload.total} ban ghi`);
    const container = document.getElementById("record-rows");
    container.innerHTML = payload.data.length ? payload.data.map(row => {
        const imgHtml = row.crop_path 
            ? `<img src="/${row.crop_path}" class="table-thumbnail" onclick="viewPhoto('/${row.crop_path}', '${escapeAttr(row.plate)}')" alt="Plate">` 
            : `<div class="no-photo-badge"><i data-lucide="image-off"></i></div>`;
        return `
            <tr>
                <td>${imgHtml}</td>
                <td><span class="plate-badge">${escapeHtml(row.plate || "N/A")}</span></td>
                <td>${escapeHtml(row.type || "UNKNOWN")}</td>
                <td>${formatDateTime(row.timestamp)}</td>
                <td><span class="status-tag ${(row.status || "").toLowerCase()}">${escapeHtml(row.status || "N/A")}</span></td>
                <td>${escapeHtml(row.package || "STANDARD")}</td>
                <td>${row.confidence ? `${Math.round(row.confidence * 100)}%` : "N/A"}</td>
                <td><button class="btn-action" data-action="history" data-plate="${escapeAttr(row.plate)}" title="History"><i data-lucide="history"></i></button></td>
            </tr>
        `;
    }).join("") : emptyRow("Khong co ban ghi phu hop", 8);
    lucide.createIcons();
}

async function actionUpgrade(plate) {
    const res = await apiFetch(`/api/vehicle/${encodeURIComponent(plate)}/upgrade`, {
        method: "POST",
        body: JSON.stringify({ package: "SUPREME" }),
    });
    if (res?.ok) {
        showToast(`${plate} da chuyen sang PAID`, "success");
        refreshAll();
    }
}

async function actionCancel(plate) {
    if (!confirm(`Chuyen ${plate} ve UNPAID?`)) return;
    const res = await apiFetch(`/api/vehicle/${encodeURIComponent(plate)}/cancel`, { method: "POST" });
    if (res?.ok) {
        showToast(`${plate} da ve UNPAID`, "warning");
        refreshAll();
    }
}

async function actionBlacklist(plate) {
    const reason = prompt(`Ly do dua ${plate} vao danh sach den:`, "Manual review");
    if (reason === null) return;
    const res = await apiFetch(`/api/vehicle/${encodeURIComponent(plate)}/blacklist`, {
        method: "POST",
        body: JSON.stringify({ reason }),
    });
    if (res?.ok) {
        showToast(`${plate} da vao danh sach den`, "danger");
        refreshAll();
    }
}

async function actionClearBlacklist(plate) {
    if (!confirm(`Go ${plate} khoi danh sach den?`)) return;
    const res = await apiFetch(`/api/vehicle/${encodeURIComponent(plate)}/clear-blacklist`, { method: "POST" });
    if (res?.ok) {
        showToast(`${plate} da duoc go blacklist`, "success");
        refreshAll();
    }
}

async function actionDelete(plate) {
    if (!confirm(`Dua ${plate} vao Trash?`)) return;
    const res = await apiFetch(`/api/vehicle/${encodeURIComponent(plate)}`, { method: "DELETE" });
    if (res?.ok) {
        showToast(`${plate} da duoc xoa mem`, "danger");
        refreshAll();
    }
}

async function restoreVehicle(plate) {
    const res = await apiFetch(`/api/vehicle/${encodeURIComponent(plate)}/restore`, { method: "POST" });
    if (res?.ok) {
        showToast(`${plate} da duoc khoi phuc`, "success");
        loadTrash();
        refreshAll();
    }
}

async function loadVehicleHistory(plate) {
    const res = await apiFetch(`/api/vehicle/${encodeURIComponent(plate)}/history`);
    if (!res?.ok) return;
    const data = await res.json();
    const latest = data.detections?.[0] || {};
    const changes = data.changes?.length ? data.changes.map(row => `
        <tr>
            <td>${formatDateTime(row.changed_at)}</td>
            <td>${escapeHtml(row.old_status || "N/A")}</td>
            <td>${escapeHtml(row.new_status || "N/A")}</td>
            <td>${escapeHtml(row.changed_by || "SYSTEM")}</td>
        </tr>
    `).join("") : emptyRow("Chua co thay doi trang thai", 4);

    openModal(`History - ${escapeHtml(data.plate)}`, `
        <div class="detail-list">
            <div class="detail-item"><span>Bien so</span><strong>${escapeHtml(data.plate)}</strong></div>
            <div class="detail-item"><span>Trang thai hien tai</span><strong>${escapeHtml(latest.status || "N/A")}</strong></div>
            <div class="detail-item"><span>Loai xe</span><strong>${escapeHtml(latest.type || "UNKNOWN")}</strong></div>
            <div class="detail-item"><span>Goi</span><strong>${escapeHtml(latest.package || "STANDARD")}</strong></div>
            <div class="detail-item"><span>Ly do blacklist</span><strong>${escapeHtml(latest.blacklist_reason || "N/A")}</strong></div>
        </div>
        <table>
            <thead><tr><th>Thoi gian</th><th>Cu</th><th>Moi</th><th>Nguoi doi</th></tr></thead>
            <tbody>${changes}</tbody>
        </table>
    `);
}

async function loadConfig() {
    const res = await apiFetch("/api/config");
    if (!res?.ok) return;
    const data = await res.json();
    data.forEach(item => {
        const el = document.getElementById(`cfg-${item.key}`);
        if (el) el.value = item.value;
    });
}

async function saveConfig() {
    const keys = ["system_name", "price_per_vehicle", "token_expire_h"];
    for (const key of keys) {
        const value = document.getElementById(`cfg-${key}`).value;
        const res = await apiFetch(`/api/config/${key}`, { method: "PUT", body: JSON.stringify({ value }) });
        if (!res?.ok) return;
    }
    showToast("Da luu cau hinh", "success");
    refreshAll();
}

async function loadReports() {
    const days = document.getElementById("report-days")?.value || "14";
    const res = await apiFetch(`/api/reports/overview?days=${encodeURIComponent(days)}`);
    if (!res?.ok) return;
    const payload = await res.json();

    const reportRows = document.getElementById("report-rows");
    reportRows.innerHTML = payload.daily.length ? payload.daily.slice().reverse().map(row => `
        <tr>
            <td>${escapeHtml(row.date)}</td>
            <td>${row.total}</td>
            <td>${row.paid}</td>
            <td>${row.unpaid}</td>
            <td>${row.blacklisted}</td>
            <td>${formatCurrency(row.revenue)}</td>
        </tr>
    `).join("") : emptyRow("Chua co bao cao", 6);

    const typeRows = document.getElementById("type-rows");
    const entries = Object.entries(payload.vehicle_types || {});
    typeRows.innerHTML = entries.length ? entries.map(([type, count]) => `
        <tr><td>${escapeHtml(type)}</td><td>${count}</td></tr>
    `).join("") : emptyRow("Chua co du lieu loai xe", 2);

    // FEATURE 4: Update ApexCharts dynamically
    updateCharts(payload);
}

async function exportVehicles() {
    const url = `/api/export/vehicles?search=${encodeURIComponent(State.search)}&status=${encodeURIComponent(State.status)}`;
    const res = await fetch(url, { headers: authHeaders() });
    if (res.status === 401) {
        showToast("Phien dang nhap da het han", "warning");
        logout();
        return;
    }
    if (!res.ok) {
        showToast("Khong xuat duoc CSV", "danger");
        return;
    }
    const blob = await res.blob();
    const filename = parseDownloadName(res.headers.get("Content-Disposition")) || "trafficai_vehicles.csv";
    const href = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = href;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(href);
}

function switchTab(id, el) {
    State.activeTab = id;
    document.querySelectorAll(".section").forEach(section => section.classList.remove("active"));
    document.querySelectorAll(".nav-item").forEach(item => item.classList.remove("active"));
    document.getElementById(`tab-${id}`).classList.add("active");
    el?.classList.add("active");

    if (id === "settings") loadConfig();
    else if (id === "audit") loadAudit();
    else if (id === "trash") loadTrash();
    else if (id === "records") loadRecords();
    else if (id === "reports") loadReports();
    else refreshAll();
}

async function loadAudit() {
    const res = await apiFetch("/api/audit");
    if (!res?.ok) return;
    const data = await res.json();
    const container = document.getElementById("audit-rows");
    container.innerHTML = data.length ? data.map(row => `
        <tr>
            <td>${formatDateTime(row.timestamp)}</td>
            <td>${escapeHtml(row.username || "")}</td>
            <td><b>${escapeHtml(row.action || "")}</b></td>
            <td>${escapeHtml(row.detail || "")}</td>
            <td>${escapeHtml(row.ip || "")}</td>
        </tr>
    `).join("") : emptyRow("Chua co audit log", 5);
}

async function loadTrash() {
    const res = await apiFetch("/api/trash");
    if (!res?.ok) return;
    const data = await res.json();
    const container = document.getElementById("trash-rows");
    container.innerHTML = data.length ? data.map(row => {
        const imgHtml = row.crop_path 
            ? `<img src="/${row.crop_path}" class="table-thumbnail" onclick="viewPhoto('/${row.crop_path}', '${escapeAttr(row.plate_text)}')" alt="Plate">` 
            : `<div class="no-photo-badge"><i data-lucide="image-off"></i></div>`;
        return `
            <tr>
                <td>${imgHtml}</td>
                <td><span class="plate-badge">${escapeHtml(row.plate_text || "N/A")}</span></td>
                <td>${escapeHtml(row.vehicle_type || "UNKNOWN")}</td>
                <td>${formatDateTime(row.deleted_at)}</td>
                <td><span class="status-tag ${(row.payment_status || "").toLowerCase()}">${escapeHtml(row.payment_status || "")}</span></td>
                <td><button class="btn-action" data-action="restore" data-plate="${escapeAttr(row.plate_text)}" title="Restore"><i data-lucide="refresh-cw"></i></button></td>
            </tr>
        `;
    }).join("") : emptyRow("Trash dang trong", 6);
    lucide.createIcons();
}

function handleSearch(value) {
    State.search = value;
    State.page = 1;
    if (State.activeTab === "records") loadRecords();
    else if (State.activeTab === "reports") loadReports();
    else syncVehicles(true);
}

function handleStatus(value) {
    State.status = value;
    State.page = 1;
    if (State.activeTab === "records") loadRecords();
    else syncVehicles(true);
}

function setupWS() {
    if (State.ws) State.ws.close();
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    State.ws = new WebSocket(`${scheme}://${location.host}/ws`);
    State.ws.onmessage = (event) => {
        try {
            const payload = JSON.parse(event.data);
            if (payload && payload.event === "new_detection") {
                const info = payload.data || {};
                playChime();
                showToast(`Đã phát hiện xe mới: ${info.plate || "N/A"} (${info.type || "UNKNOWN"})`, "success");
            }
        } catch (e) {
            // Fallback for regular updates
        }

        syncStats();
        if (State.activeTab === "dashboard") syncVehicles(true);
        if (State.activeTab === "records") loadRecords();
        if (State.activeTab === "reports") loadReports();
    };
    State.ws.onclose = () => setTimeout(setupWS, 3000);
}

function setupInfiniteScroll() {
    const area = document.querySelector(".scroll-area");
    if (!area) return;
    area.addEventListener("scroll", () => {
        const nearBottom = area.scrollTop + area.clientHeight >= area.scrollHeight - 80;
        if (nearBottom && !State.loading && State.hasMore && State.activeTab === "dashboard") {
            State.page += 1;
            syncVehicles(false);
        }
    });
}

function setupActionDelegation() {
    document.addEventListener("click", event => {
        const button = event.target.closest("[data-action]");
        if (!button) return;
        const plate = button.dataset.plate || "";
        const action = button.dataset.action;
        if (action === "upgrade") actionUpgrade(plate);
        else if (action === "cancel") actionCancel(plate);
        else if (action === "blacklist") actionBlacklist(plate);
        else if (action === "clear-blacklist") actionClearBlacklist(plate);
        else if (action === "history") loadVehicleHistory(plate);
        else if (action === "delete") actionDelete(plate);
        else if (action === "restore") restoreVehicle(plate);
    });
}

function updateClock() {
    setText("clock", new Date().toLocaleTimeString("vi-VN"));
}

function openModal(title, html) {
    setText("modal-title", title);
    document.getElementById("modal-body").innerHTML = html;
    document.getElementById("modal").hidden = false;
    lucide.createIcons();
}

function closeModal() {
    const modal = document.getElementById("modal");
    if (modal) modal.hidden = true;
}

function showToast(message, type = "info") {
    const container = document.getElementById("toast-container");
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 3600);
}

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

function formatTime(value) {
    const date = value ? new Date(value) : null;
    return date && !Number.isNaN(date.valueOf()) ? date.toLocaleTimeString("vi-VN") : "N/A";
}

function formatDateTime(value) {
    const date = value ? new Date(value) : null;
    return date && !Number.isNaN(date.valueOf()) ? date.toLocaleString("vi-VN") : "N/A";
}

function formatCurrency(value) {
    return `${Number(value || 0).toLocaleString("vi-VN")} VND`;
}

function emptyRow(message, colspan) {
    return `<tr><td colspan="${colspan}" class="empty-cell">${message}</td></tr>`;
}

function parseDownloadName(disposition) {
    if (!disposition) return "";
    const match = disposition.match(/filename="?([^"]+)"?/i);
    return match ? match[1] : "";
}

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
    }[char]));
}

function escapeAttr(value) {
    return escapeHtml(value).replace(/`/g, "&#96;");
}

function playChime() {
    try {
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const now = audioCtx.currentTime;
        const osc1 = audioCtx.createOscillator();
        const gain1 = audioCtx.createGain();
        osc1.type = 'sine';
        osc1.frequency.setValueAtTime(659.25, now); // E5
        gain1.gain.setValueAtTime(0.12, now);
        gain1.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
        osc1.connect(gain1);
        gain1.connect(audioCtx.destination);
        osc1.start(now);
        osc1.stop(now + 0.35);
        
        const osc2 = audioCtx.createOscillator();
        const gain2 = audioCtx.createGain();
        osc2.type = 'sine';
        osc2.frequency.setValueAtTime(880.00, now + 0.08); // A5
        gain2.gain.setValueAtTime(0.12, now + 0.08);
        gain2.gain.exponentialRampToValueAtTime(0.001, now + 0.55);
        osc2.connect(gain2);
        gain2.connect(audioCtx.destination);
        osc2.start(now + 0.08);
        osc2.stop(now + 0.55);
    } catch (e) {
        console.warn("Chime blocked by auto-play policies", e);
    }
}

function viewPhoto(path, plate) {
    openModal(`Snapshot - ${escapeHtml(plate)}`, `
        <div class="modal-photo-frame">
            <img src="${path}" alt="License Plate Crop">
        </div>
        <div class="detail-list">
            <div class="detail-item"><span>Biển kiểm soát</span><strong>${escapeHtml(plate)}</strong></div>
            <div class="detail-item"><span>Đường dẫn ảnh</span><strong style="font-family: var(--font-mono); font-size: 0.8rem;">${escapeHtml(path)}</strong></div>
        </div>
    `);
}


// --- LIVE AI MONITOR & CONTROLS ---

let aiStatusPollInterval = null;

function toggleSourceInput() {
    const sourceSelect = document.getElementById("video-source");
    const customUrlGroup = document.getElementById("custom-url-group");
    if (sourceSelect.value === "custom") {
        customUrlGroup.style.display = "flex";
    } else {
        customUrlGroup.style.display = "none";
    }
}

function triggerFileInput() {
    document.getElementById("file-input").click();
}

async function handleFileUpload(event) {
    const file = event.target.files[0];
    if (!file) return;
    uploadFile(file);
}

async function uploadFile(file) {
    if (!file.name.endsWith(".mp4")) {
        showToast("Chỉ hỗ trợ định dạng video MP4!", "warning");
        return;
    }

    const formData = new FormData();
    formData.append("file", file);

    const progressDiv = document.getElementById("upload-progress");
    const progressFill = document.getElementById("progress-fill");
    const progressText = document.getElementById("progress-text");

    progressDiv.style.display = "flex";
    progressFill.style.width = "0%";
    progressText.textContent = "0%";

    try {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/ai/upload", true);
        
        if (State.user?.token) {
            xhr.setRequestHeader("Authorization", `Bearer ${State.user.token}`);
        }

        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
                const percent = Math.round((e.loaded / e.total) * 100);
                progressFill.style.width = `${percent}%`;
                progressText.textContent = `${percent}%`;
            }
        };

        xhr.onload = async () => {
            progressDiv.style.display = "none";
            if (xhr.status === 200) {
                showToast("Tải video lên thành công!", "success");
                await loadVideos();
                const resData = JSON.parse(xhr.responseText);
                const sourceSelect = document.getElementById("video-source");
                sourceSelect.value = resData.filename;
                toggleSourceInput();
            } else {
                const errData = JSON.parse(xhr.responseText || "{}");
                showToast(errData.detail || "Tải video lên thất bại!", "danger");
            }
        };

        xhr.onerror = () => {
            progressDiv.style.display = "none";
            showToast("Có lỗi mạng xảy ra khi tải video!", "danger");
        };

        xhr.send(formData);
    } catch (err) {
        progressDiv.style.display = "none";
        showToast("Lỗi tải video lên!", "danger");
    }
}

function setupDragAndDrop() {
    const dropzone = document.getElementById("dropzone");
    if (!dropzone) return;

    ['dragenter', 'dragover'].forEach(eventName => {
        dropzone.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropzone.classList.add('dragover');
        }, false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        dropzone.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropzone.classList.remove('dragover');
        }, false);
    });

    dropzone.addEventListener('drop', (e) => {
        const dt = e.dataTransfer;
        const files = dt.files;
        if (files.length > 0) {
            uploadFile(files[0]);
        }
    }, false);
}

async function loadVideos() {
    const sourceSelect = document.getElementById("video-source");
    if (!sourceSelect) return;

    const res = await apiFetch("/api/ai/videos");
    if (!res?.ok) return;
    const videos = await res.json();

    sourceSelect.innerHTML = `
        <option value="test_bot_cutted.mp4">Video BOT Mẫu (Mặc định)</option>
        <option value="0">Camera máy tính (Cổng 0)</option>
        <option value="1">Camera điện thoại / Cam phụ (Cổng 1)</option>
        <option value="custom">-- Luồng RTSP / Custom URL --</option>
    `;

    videos.forEach(v => {
        const opt = document.createElement("option");
        opt.value = v.filename;
        opt.textContent = `Video Uploaded: ${v.filename.substring(33)}`;
        sourceSelect.appendChild(opt);
    });
}

async function startAI() {
    const sourceSelect = document.getElementById("video-source");
    let source = sourceSelect.value;
    if (source === "custom") {
        source = document.getElementById("custom-url").value.trim();
        if (!source) {
            showToast("Vui lòng nhập đường dẫn Custom URL hoặc RTSP!", "warning");
            return;
        }
    }

    const res = await apiFetch(`/api/ai/start?source=${encodeURIComponent(source)}`, { method: "POST" });
    if (!res?.ok) return;

    showToast("Đã khởi chạy luồng xử lý AI giám sát BOT!", "success");
    
    const liveFeed = document.getElementById("live-feed");
    const placeholder = document.getElementById("monitor-placeholder");
    const badge = document.getElementById("monitor-badge");
    const btnStart = document.getElementById("btn-start");
    const btnStop = document.getElementById("btn-stop");

    liveFeed.src = `/api/stream?t=${Date.now()}`;
    liveFeed.style.display = "block";
    placeholder.style.display = "none";

    badge.textContent = "ONLINE";
    badge.className = "monitor-badge online";

    btnStart.disabled = true;
    btnStop.disabled = false;

    if (aiStatusPollInterval) clearInterval(aiStatusPollInterval);
    aiStatusPollInterval = setInterval(pollAIStatus, 1000);
}

async function stopAI() {
    const res = await apiFetch("/api/ai/stop", { method: "POST" });
    if (!res?.ok) return;

    showToast("Đã tạm dừng luồng giám sát BOT!", "warning");
    setMonitorOfflineUI();
}

function setMonitorOfflineUI() {
    const liveFeed = document.getElementById("live-feed");
    const placeholder = document.getElementById("monitor-placeholder");
    const badge = document.getElementById("monitor-badge");
    const btnStart = document.getElementById("btn-start");
    const btnStop = document.getElementById("btn-stop");

    liveFeed.src = "";
    liveFeed.style.display = "none";
    placeholder.style.display = "block";

    badge.textContent = "OFFLINE";
    badge.className = "monitor-badge offline";

    btnStart.disabled = false;
    btnStop.disabled = true;

    document.getElementById("monitor-fps").innerHTML = `<i data-lucide="cpu"></i> FPS: 0`;
    document.getElementById("monitor-count").innerHTML = `<i data-lucide="car"></i> Đã xử lý: 0`;
    lucide.createIcons();

    if (aiStatusPollInterval) {
        clearInterval(aiStatusPollInterval);
        aiStatusPollInterval = null;
    }
}

async function pollAIStatus() {
    const res = await apiFetch("/api/ai/status");
    if (!res?.ok) return;
    const status = await res.json();

    if (status.running) {
        const badge = document.getElementById("monitor-badge");
        badge.textContent = status.status.toUpperCase();
        badge.className = "monitor-badge online";

        document.getElementById("monitor-fps").innerHTML = `<i data-lucide="cpu"></i> FPS: ${status.fps}`;
        document.getElementById("monitor-count").innerHTML = `<i data-lucide="car"></i> Đã xử lý: ${status.processed_frames}`;
        lucide.createIcons();
    } else {
        setMonitorOfflineUI();
        refreshAll();
    }
}

async function checkCurrentAIStatus() {
    const res = await apiFetch("/api/ai/status");
    if (!res?.ok) return;
    const status = await res.json();

    if (status.running) {
        const liveFeed = document.getElementById("live-feed");
        const placeholder = document.getElementById("monitor-placeholder");
        const badge = document.getElementById("monitor-badge");
        const btnStart = document.getElementById("btn-start");
        const btnStop = document.getElementById("btn-stop");

        liveFeed.src = "/api/stream";
        liveFeed.style.display = "block";
        placeholder.style.display = "none";

        badge.textContent = status.status.toUpperCase();
        badge.className = "monitor-badge online";

        btnStart.disabled = true;
        btnStop.disabled = false;

        if (aiStatusPollInterval) clearInterval(aiStatusPollInterval);
        aiStatusPollInterval = setInterval(pollAIStatus, 1000);
    }
}

// --- APEXCHARTS ANALYTICS INTEGRATION ---

let trafficChart = null;
let vehicleTypeChart = null;

function initCharts() {
    const trafficContainer = document.querySelector("#chart-traffic");
    const typesContainer = document.querySelector("#chart-types");
    if (!trafficContainer || !typesContainer) return;
    if (trafficChart && vehicleTypeChart) return;

    // 1. Traffic & Revenue Chart (Area Chart)
    const trafficOptions = {
        chart: {
            type: 'area',
            height: 320,
            background: 'transparent',
            toolbar: { show: false },
            foreColor: '#94a3b8'
        },
        colors: ['#2dd4bf', '#22c55e'],
        stroke: { curve: 'smooth', width: 3 },
        fill: {
            type: 'gradient',
            gradient: {
                shadeIntensity: 1,
                opacityFrom: 0.35,
                opacityTo: 0.05,
                stops: [0, 90, 100]
            }
        },
        dataLabels: { enabled: false },
        series: [
            { name: 'Tổng số xe', data: [] },
            { name: 'Doanh thu (k VND)', data: [] }
        ],
        xaxis: {
            categories: [],
            axisBorder: { show: false },
            axisTicks: { show: false }
        },
        yaxis: [
            {
                title: { text: 'Số lượt xe' },
                labels: {
                    formatter: val => Math.round(val)
                }
            },
            {
                opposite: true,
                title: { text: 'Doanh thu (k VND)' },
                labels: {
                    formatter: val => `${val.toLocaleString()}k`
                }
            }
        ],
        grid: {
            borderColor: 'rgba(255, 255, 255, 0.08)',
            strokeDashArray: 4
        },
        legend: {
            position: 'top',
            horizontalAlign: 'right'
        },
        theme: { mode: 'dark' }
    };

    trafficChart = new ApexCharts(trafficContainer, trafficOptions);
    trafficChart.render();

    // 2. Vehicle Categories Chart (Donut Chart)
    const typeOptions = {
        chart: {
            type: 'donut',
            height: 320,
            background: 'transparent',
            foreColor: '#94a3b8'
        },
        colors: ['#2dd4bf', '#f59e0b', '#3b82f6', '#ec4899', '#8b5cf6'],
        series: [],
        labels: [],
        plotOptions: {
            pie: {
                donut: {
                    size: '70%',
                    labels: {
                        show: true,
                        total: {
                            show: true,
                            label: 'Tổng xe',
                            color: '#f8fafc',
                            formatter: w => {
                                return w.globals.seriesTotals.reduce((a, b) => a + b, 0);
                            }
                        }
                    }
                }
            }
        },
        legend: {
            position: 'bottom'
        },
        dataLabels: { enabled: false },
        theme: { mode: 'dark' }
    };

    vehicleTypeChart = new ApexCharts(typesContainer, typeOptions);
    vehicleTypeChart.render();
}

function updateCharts(payload) {
    const trafficContainer = document.querySelector("#chart-traffic");
    const typesContainer = document.querySelector("#chart-types");
    if (!trafficContainer || !typesContainer) return;
    
    if (!trafficChart || !vehicleTypeChart) {
        initCharts();
    }

    const categories = payload.daily.map(row => {
        const parts = row.date.split('-');
        return parts.length >= 3 ? `${parts[2]}/${parts[1]}` : row.date;
    });
    const vehicleCounts = payload.daily.map(row => row.total);
    const revenueK = payload.daily.map(row => row.revenue / 1000);

    trafficChart.updateSeries([
        { name: 'Tổng số xe', data: vehicleCounts },
        { name: 'Doanh thu (k VND)', data: revenueK }
    ]);
    trafficChart.updateOptions({
        xaxis: { categories: categories }
    });

    const types = Object.keys(payload.vehicle_types || {});
    const counts = Object.values(payload.vehicle_types || {});

    vehicleTypeChart.updateSeries(counts);
    vehicleTypeChart.updateOptions({
        labels: types
    });
}
