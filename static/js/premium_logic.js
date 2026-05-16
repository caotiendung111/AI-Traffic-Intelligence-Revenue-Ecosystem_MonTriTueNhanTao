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
        container.innerHTML = emptyRow("Chua co du lieu nhan dien", 6);
        return;
    }

    container.insertAdjacentHTML("beforeend", data.map(row => {
        const isBlacklisted = row.status === "BLACKLISTED";
        return `
            <tr id="row-${row.id}" class="fade-in">
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
    container.innerHTML = payload.data.length ? payload.data.map(row => `
        <tr>
            <td><span class="plate-badge">${escapeHtml(row.plate || "N/A")}</span></td>
            <td>${escapeHtml(row.type || "UNKNOWN")}</td>
            <td>${formatDateTime(row.timestamp)}</td>
            <td><span class="status-tag ${(row.status || "").toLowerCase()}">${escapeHtml(row.status || "N/A")}</span></td>
            <td>${escapeHtml(row.package || "STANDARD")}</td>
            <td>${row.confidence ? `${Math.round(row.confidence * 100)}%` : "N/A"}</td>
            <td><button class="btn-action" data-action="history" data-plate="${escapeAttr(row.plate)}" title="History"><i data-lucide="history"></i></button></td>
        </tr>
    `).join("") : emptyRow("Khong co ban ghi phu hop", 7);
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
    container.innerHTML = data.length ? data.map(row => `
        <tr>
            <td><span class="plate-badge">${escapeHtml(row.plate_text || "N/A")}</span></td>
            <td>${escapeHtml(row.vehicle_type || "UNKNOWN")}</td>
            <td>${formatDateTime(row.deleted_at)}</td>
            <td><span class="status-tag ${(row.payment_status || "").toLowerCase()}">${escapeHtml(row.payment_status || "")}</span></td>
            <td><button class="btn-action" data-action="restore" data-plate="${escapeAttr(row.plate_text)}" title="Restore"><i data-lucide="refresh-cw"></i></button></td>
        </tr>
    `).join("") : emptyRow("Trash dang trong", 5);
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
    State.ws.onmessage = () => {
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
