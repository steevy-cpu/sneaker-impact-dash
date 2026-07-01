/**
 * system_health.js — System Health page logic
 *
 * Loads GET /api/health, renders mode banner, stat grid, and camera
 * placeholder cards. Auto-refreshes every 30 seconds.
 */

const CAMERAS = [
    { name: "Top",         key: "cam_top" },
    { name: "Left Side",   key: "cam_left" },
    { name: "Right Side",  key: "cam_right" },
    { name: "Angle Left",  key: "cam_angle_left" },
    { name: "Angle Right", key: "cam_angle_right" },
];

// ── Mode banner ───────────────────────────────────────────────────────────
function renderBanner(health) {
    const el    = document.getElementById("mode-banner");
    const badge = document.getElementById("mode-badge");
    const isActual = health.mode === "actual";

    if (isActual) {
        badge.textContent = "ACTUAL MODE";
        badge.className   = "mode-badge actual";
        el.innerHTML = `
            <div class="health-mode-banner actual">
                <span class="health-mode-icon"><svg class="icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/></svg></span>
                Running in <strong>ACTUAL mode</strong> — connected to live inspection station.
            </div>`;
    } else {
        el.innerHTML = `
            <div class="health-mode-banner simulation">
                <span class="health-mode-icon"><svg class="icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg></span>
                Running in <strong>SIMULATION mode</strong> — data is generated for testing only.
            </div>`;
    }
}

// ── Stat grid ─────────────────────────────────────────────────────────────
function dotHtml(cls) {
    return `<span class="status-dot ${cls}"></span>`;
}

function renderGrid(health) {
    const el = document.getElementById("health-grid");

    const dbClass = health.db_connected ? "ok" : "err";
    const dbLabel = health.db_connected ? "Connected" : "Error";

    const pendingClass = health.pending_review_count > 20 ? "warn"
                       : health.pending_review_count > 0  ? ""
                       : "ok";

    const failedClass = health.validation_error_count > 10 ? "warn" : "";

    const lastCapture = health.last_capture_time
        ? new Date(health.last_capture_time).toLocaleString()
        : "—";

    const storageMB = typeof health.storage_usage_mb === "number"
        ? health.storage_usage_mb.toFixed(1) + " MB"
        : "—";

    el.innerHTML = `
        <div class="health-stat ${dbClass}">
            <div class="health-stat-value">${dotHtml(dbClass)}${dbLabel}</div>
            <div class="health-stat-label">Database Status</div>
        </div>
        <div class="health-stat">
            <div class="health-stat-value">${health.shoe_count.toLocaleString()}</div>
            <div class="health-stat-label">Total Shoes</div>
        </div>
        <div class="health-stat">
            <div class="health-stat-value">${health.batch_count.toLocaleString()}</div>
            <div class="health-stat-label">Total Batches</div>
        </div>
        <div class="health-stat ${pendingClass}">
            <div class="health-stat-value">${health.pending_review_count}</div>
            <div class="health-stat-label">Pending Reviews</div>
        </div>
        <div class="health-stat ${failedClass}">
            <div class="health-stat-value">${health.validation_error_count}</div>
            <div class="health-stat-label">Validation Errors</div>
        </div>
        <div class="health-stat">
            <div class="health-stat-value" style="font-size:16px;">${lastCapture}</div>
            <div class="health-stat-label">Last Capture</div>
        </div>
        <div class="health-stat">
            <div class="health-stat-value" style="font-size:18px;">${health.model_version || "—"}</div>
            <div class="health-stat-label">Model Version</div>
        </div>
        <div class="health-stat">
            <div class="health-stat-value">${storageMB}</div>
            <div class="health-stat-label">Storage Used</div>
        </div>
        <div class="health-stat ok">
            <div class="health-stat-value">${dotHtml("ok")}OK</div>
            <div class="health-stat-label">API Status</div>
        </div>
    `;
}

// ── Camera placeholders ───────────────────────────────────────────────────
// In simulation mode these cards are informational only — no real cameras are connected.
// In actual mode, extend this function to call a camera-status API endpoint and
// update each card with real-time feed state (online/offline/error).
function renderCameras(mode) {
    const el      = document.getElementById("camera-grid");
    const isActual = mode === "actual";

    el.innerHTML = CAMERAS.map(cam => {
        const statusText = isActual ? "Status unknown" : "Simulation mode";
        const dotCls     = isActual ? "unknown" : "warn";
        return `
            <div class="camera-card">
                <div class="camera-card-icon"><svg class="icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/></svg></div>
                <div class="camera-card-name">${cam.name}</div>
                <div class="camera-card-status">
                    <span class="status-dot ${dotCls}"></span>${statusText}
                </div>
            </div>`;
    }).join("");
}

// ── Subtitle ──────────────────────────────────────────────────────────────
function updateSubtitle() {
    document.getElementById("health-subtitle").textContent =
        "Last updated: " + new Date().toLocaleTimeString();
}

// ── Operational alerts ─────────────────────────────────────────────────────
const HEALTH_ALERT_ICONS = {
    warning: '<svg class="icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
    info:    '<svg class="icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
    ok:      '<svg class="icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/></svg>',
};

async function loadHealthAlerts() {
    const section = document.getElementById("health-alerts");
    if (!section) return;
    try {
        const data = await api.getAlerts();
        if (!data.alerts.length) {
            section.innerHTML = `
                <div class="alert-item ok">
                    <span class="alert-icon">${HEALTH_ALERT_ICONS.ok}</span>
                    <span>All systems normal — no alerts at this time</span>
                </div>`;
            return;
        }
        section.innerHTML = data.alerts.map(a => `
            <div class="alert-item ${a.level}">
                <span class="alert-icon">${HEALTH_ALERT_ICONS[a.level] || ""}</span>
                <span>${a.message}</span>
            </div>
        `).join("");
    } catch (_) { /* non-critical */ }
}

async function loadDataQuality() {
    const list = document.getElementById("dq-list");
    const sum = document.getElementById("dq-summary");
    if (!list) return;
    try {
        const d = await api.getDataQuality();
        const c = d.counts || {};
        sum.textContent = `${c.fail || 0} fail · ${c.warn || 0} warn · ${c.ok || 0} ok`;
        sum.style.color = d.status === "fail" ? "#dc2626" : d.status === "warn" ? "#d97706" : "#16a34a";
        list.innerHTML = d.checks.map(ck => `
            <div class="dq-item">
                <span class="dq-dot ${ck.status}"></span>
                <div class="dq-main">
                    <span class="dq-label">${ck.label}</span><span class="dq-val ${ck.status}">${ck.value}</span>
                    <div class="dq-detail">${ck.detail}</div>
                    ${ck.fix ? `<div class="dq-fix">→ ${ck.fix}</div>` : ""}
                </div>
            </div>`).join("");
    } catch (_) {
        sum.textContent = "unavailable";
    }
}

// ── Main load ─────────────────────────────────────────────────────────────
async function loadHealth() {
    try {
        const health = await api.getHealth();
        renderBanner(health);
        renderGrid(health);
        renderCameras(health.mode);
        initSimControls(health.mode);
        updateSubtitle();
        loadHealthAlerts();
        loadDataQuality();
    } catch (err) {
        document.getElementById("health-grid").innerHTML =
            `<div class="error-state" style="grid-column:1/-1">
                Failed to load health data: ${err.message}
             </div>`;
    }
}

// ── Bootstrap + auto-refresh every 30 s ──────────────────────────────────
loadHealth();
setInterval(loadHealth, 30_000);
