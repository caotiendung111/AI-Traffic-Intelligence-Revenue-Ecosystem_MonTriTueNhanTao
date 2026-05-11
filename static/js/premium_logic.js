/* ─────────────────────────────────────────────
   AI Traffic Monitor — v6.3.0  |  premium_logic.js
   Full Feature Restoration & Professional Logic
───────────────────────────────────────────── */

const State = {
    user:        null,
    ws:          null,
    activeTab:   "dashboard",
    page:        1,
    loading:     false,
    hasMore:     true,
    search:      "",
    status:      "",
};

// ── App Lifecycle ──────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    lucide.createIcons();
    setupInfiniteScroll();
});

async function doLogin() {
    const u = document.getElementById("login-user").value.trim();
    const p = document.getElementById("login-pass").value;
    const res = await fetch("/api/login", {
        method:  "POST",
        body:    JSON.stringify({ username: u, password: p }),
        headers: { "Content-Type": "application/json" },
    });
    if (res.ok) {
        State.user = await res.json();
        document.getElementById("login-screen").style.display = "none";
        initSystem();
    } else {
        showToast("Invalid credentials", "danger");
    }
}

function initSystem() {
    setupWS();
    refreshAll();
    setInterval(updateClock, 1000);
}

function refreshAll() {
    State.page = 1;
    State.hasMore = true;
    syncStats();
    syncVehicles(true);
}

// ── Networking ─────────────────────────────
function authHeaders() {
    return {
        "Content-Type": "application/json",
        ...(State.user?.token ? { "Authorization": `Bearer ${State.user.token}` } : {}),
    };
}

async function apiFetch(url, opts = {}) {
    const res = await fetch(url, {
        ...opts,
        headers: { ...authHeaders(), ...(opts.headers || {}) },
    });
    if (res.status === 401) { location.reload(); return null; }
    return res;
}

// ── Data Sync ──────────────────────────────
async function syncStats() {
    const res = await apiFetch("/api/stats");
    if (res) {
        const s = await res.json();
        setText("st-total", s.total);
        setText("st-paid", s.paid);
        setText("st-rev", s.revenue);
        setText("st-black", s.blacklisted);
    }
}

async function syncVehicles(clear = true) {
    if (State.loading) return;
    State.loading = true;
    const url = `/api/vehicles?page=${State.page}&limit=50&search=${encodeURIComponent(State.search)}&status=${State.status}`;
    const res = await apiFetch(url);
    if (!res) return;
    const d = await res.json();
    State.loading = false;
    State.hasMore = d.page < d.pages;
    renderVehicles(d.data, clear);
}

function renderVehicles(data, clear) {
    const container = document.getElementById("vehicle-rows");
    if (clear) container.innerHTML = "";
    
    container.insertAdjacentHTML('beforeend', data.map(x => `
        <tr id="row-${x.id}" class="fade-in">
            <td><span class="plate-badge">${x.plate}</span></td>
            <td>${x.type}</td>
            <td>${x.timestamp.split("T")[1].split(".")[0]}</td>
            <td><span class="status-tag ${x.status.toLowerCase()}">${x.status}</span></td>
            <td>${x.package}</td>
            <td>
                <div style="display:flex; gap:0.5rem">
                    <button class="btn-action" onclick="actionUpgrade('${x.plate}')" title="Upgrade"><i data-lucide="zap"></i></button>
                    <button class="btn-action" onclick="actionCancel('${x.plate}')" title="Cancel Package"><i data-lucide="rotate-ccw"></i></button>
                    <button class="btn-action danger" onclick="actionDelete('${x.plate}')" title="Delete"><i data-lucide="trash"></i></button>
                </div>
            </td>
        </tr>
    `).join(""));
    lucide.createIcons();
}

// ── Vehicle Actions ────────────────────────
async function actionUpgrade(plate) {
    const res = await apiFetch(`/api/vehicle/${plate}/upgrade`, {
        method: "POST",
        body: JSON.stringify({ package: "SUPREME" })
    });
    if (res?.ok) { showToast(`${plate} Upgraded!`, "success"); refreshAll(); }
}

async function actionCancel(plate) {
    if (!confirm(`Revert ${plate} to UNPAID status?`)) return;
    const res = await apiFetch(`/api/vehicle/${plate}/cancel`, { method: "POST" });
    if (res?.ok) { showToast(`${plate} Reverted`, "warning"); refreshAll(); }
}

async function actionDelete(plate) {
    if (!confirm(`Soft-delete ${plate}? It will go to Trash.`)) return;
    const res = await apiFetch(`/api/vehicle/${plate}`, { method: "DELETE" });
    if (res?.ok) { showToast(`${plate} Deleted`, "danger"); refreshAll(); }
}

// ── Settings ───────────────────────────────
async function loadConfig() {
    const res = await apiFetch("/api/config");
    if (res) {
        const data = await res.json();
        data.forEach(c => {
            const el = document.getElementById(`cfg-${c.key}`);
            if (el) el.value = c.value;
        });
    }
}

async function saveConfig() {
    const keys = ["system_name", "price_per_vehicle", "token_expire_h"];
    for (const k of keys) {
        const val = document.getElementById(`cfg-${k}`).value;
        await apiFetch(`/api/config/${k}`, {
            method: "PUT",
            body: JSON.stringify({ value: val })
        });
    }
    showToast("System Config Synchronized", "success");
    refreshAll();
}

// ── Tab Logic ──────────────────────────────
function switchTab(id) {
    State.activeTab = id;
    document.querySelectorAll(".section").forEach(s => s.classList.remove("active"));
    document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
    
    document.getElementById(`tab-${id}`).classList.add("active");
    // Mark active nav by finding icon or text
    event.currentTarget.classList.add("active");

    if (id === "settings") loadConfig();
    else if (id === "audit") loadAudit();
    else if (id === "trash") loadTrash();
    else refreshAll();
}

async function loadAudit() {
    const res = await apiFetch("/api/audit");
    if (!res) return;
    const d = await res.json();
    const container = document.getElementById("audit-rows");
    container.innerHTML = d.map(x => `
        <tr>
            <td>${x.timestamp.split("T")[1].split(".")[0]}</td>
            <td>${x.username}</td>
            <td><b>${x.action}</b></td>
            <td>${x.detail}</td>
            <td>${x.ip}</td>
        </tr>
    `).join("");
}

async function loadTrash() {
    const res = await apiFetch("/api/trash");
    if (!res) return;
    const d = await res.json();
    const container = document.getElementById("trash-rows");
    container.innerHTML = d.map(x => `
        <tr>
            <td>${x.plate_text}</td>
            <td>${x.deleted_at?.split("T")[1]?.split(".")[0] || "N/A"}</td>
            <td><button class="btn-action" onclick="restoreVehicle('${x.plate_text}')"><i data-lucide="refresh-cw"></i></button></td>
        </tr>
    `).join("");
    lucide.createIcons();
}

// ── Helpers ────────────────────────────────
function handleSearch(val) {
    State.search = val;
    State.page = 1;
    syncVehicles(true);
}

function showToast(msg, type = "info") {
    const container = document.getElementById("toast-container");
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.textContent = msg;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

function setupWS() {
    State.ws = new WebSocket(`ws://${location.host}/ws`);
    State.ws.onmessage = e => { syncStats(); if (State.activeTab === "dashboard") syncVehicles(true); };
}

function updateClock() {
    const el = document.getElementById("clock");
    if (el) el.textContent = new Date().toLocaleTimeString();
}

function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}

function setupInfiniteScroll() {
    const area = document.querySelector(".scroll-area");
    if (area) area.onscroll = () => {
        if (area.scrollTop + area.clientHeight >= area.scrollHeight - 50 && !State.loading && State.hasMore && State.activeTab === "dashboard") {
            State.page++;
            syncVehicles(false);
        }
    };
}
