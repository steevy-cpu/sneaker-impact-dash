/**
 * dashboard.js — Dashboard Home page logic
 */

async function loadDailySummary() {
    const errorTarget = document.getElementById("stats-grid");
    try {
        const s = await api.getDailySummary();

        setStatValue("stat-total",      s.total);
        setStatValue("stat-reuse",      s.reuse_count);
        setStatValue("stat-recycle",    s.recycle_count);
        setStatValue("stat-review",     s.review_count);
        setStatValue("stat-confidence", formatConfidence(s.avg_confidence));
        setStatValue("stat-failed",     s.failed_captures);

        // Microcopy hints — sub-text beneath each stat value
        // Uses data from the daily-summary endpoint (same API call, different fields)
        if (s.total > 0) {
            const reuseRate = Math.round((s.reuse_count / s.total) * 100);
            setStatSub("stat-reuse-sub",    `${reuseRate}% of today's total`);
            const recycleRate = Math.round((s.recycle_count / s.total) * 100);
            setStatSub("stat-recycle-sub",  `${recycleRate}% of today's total`);
        }
        if (s.pending_review_count > 0) {
            setStatSub("stat-pending-sub",
                `${s.pending_review_count} awaiting review`);
        } else if (s.review_count > 0) {
            setStatSub("stat-pending-sub", "All reviewed");
        }
        if (s.failed_captures > 0) {
            setStatSub("stat-failed-sub",
                s.failed_captures === 1 ? "1 capture failed" : `${s.failed_captures} captures failed`);
        } else {
            setStatSub("stat-failed-sub", "No failures today");
        }
        if (s.avg_confidence > 0) {
            const quality = s.avg_confidence >= 0.9 ? "Excellent" :
                            s.avg_confidence >= 0.75 ? "Good" :
                            s.avg_confidence >= 0.6  ? "Moderate" : "Low";
            setStatSub("stat-confidence-sub", quality + " signal quality");
        }

        // Show today's date in the page subtitle (formatted long-form for readability)
        const dateEl = document.getElementById("page-subtitle");
        if (dateEl) {
            dateEl.textContent = new Date().toLocaleDateString("en-US", {
                weekday: "long", year: "numeric", month: "long", day: "numeric",
            });
        }
    } catch (err) {
        showError(errorTarget, "Could not load daily summary: " + err.message);
    }
}

function setStatValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value != null ? value : "—";
}

function setStatSub(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text || "";
}

async function loadRecentShoes() {
    const container = document.getElementById("recent-container");
    showLoading(container);
    try {
        const data = await api.getShoes({ page_size: 5 });

        if (!data.items.length) {
            showEmpty(container, "No shoe inspections recorded yet.");
            return;
        }

        const rows = data.items.map(shoe => `
            <tr>
                <td class="td-id">
                    <a href="shoe_detail.html?id=${shoe.id}" class="td-link">${shoe.id}</a>
                </td>
                <td class="td-muted">${formatDate(shoe.timestamp)}</td>
                <td>${thumbStripHTML(shoe)}</td>
                <td>${predictionBadgeHTML(shoe.ai_prediction)}</td>
                <td class="font-mono">${formatConfidence(shoe.ai_confidence)}</td>
                <td>${reviewStatusBadgeHTML(shoe.review_status)}</td>
            </tr>
        `).join("");

        container.innerHTML = `
            <table>
                <thead>
                    <tr>
                        <th>Shoe ID</th>
                        <th>Time</th>
                        <th>Images</th>
                        <th>AI Result</th>
                        <th>Confidence</th>
                        <th>Review Status</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    } catch (err) {
        showError(container, "Could not load recent shoes: " + err.message);
    }
}

async function loadModeBadge() {
    try {
        const health = await api.getHealth();
        const badge = document.getElementById("mode-badge");
        if (badge) {
            badge.textContent = health.mode.toUpperCase() + " MODE";
            badge.className = "mode-badge " + health.mode;
        }
    } catch (_) { /* non-critical */ }
}

// ── Operational alerts ────────────────────────────────────────────────────

const ALERT_ICONS = {
    warning: '<svg class="icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
    info:    '<svg class="icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
    ok:      '<svg class="icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/></svg>',
};

async function loadAlerts() {
    const section = document.getElementById("alerts-section");
    if (!section) return;
    try {
        const data = await api.getAlerts();
        if (!data.alerts.length) {
            section.innerHTML = "";
            return;
        }
        section.innerHTML = data.alerts.map(a => `
            <div class="alert-item ${a.level}">
                <span class="alert-icon">${ALERT_ICONS[a.level] || ""}</span>
                <span>${a.message}</span>
            </div>
        `).join("");
    } catch (_) { /* non-critical — silently skip if alerts endpoint unavailable */ }
}

// ── Activity feed ─────────────────────────────────────────────────────────

function _timeAgo(isoTimestamp) {
    const secs = Math.floor((Date.now() - new Date(isoTimestamp).getTime()) / 1000);
    if (secs < 60)  return secs + "s ago";
    const mins = Math.floor(secs / 60);
    if (mins < 60)  return mins + "m ago";
    const hrs  = Math.floor(mins / 60);
    if (hrs  < 24)  return hrs  + "h ago";
    return Math.floor(hrs / 24) + "d ago";
}

async function loadActivity() {
    const container = document.getElementById("activity-container");
    if (!container) return;
    try {
        const data = await api.getActivity();
        if (!data.events.length) {
            container.innerHTML = '<div class="loading-state" style="font-size:13px;padding:16px 0;">No activity yet.</div>';
            return;
        }
        container.innerHTML = `<div class="activity-list">${
            data.events.map(e => `
                <div class="activity-item">
                    <span class="activity-dot ${e.level}"></span>
                    <span class="activity-msg">${e.message}</span>
                    <span class="activity-time">${_timeAgo(e.timestamp)}</span>
                </div>
            `).join("")
        }</div>`;
    } catch (_) { /* non-critical — silently skip */ }
}

loadDailySummary();
loadRecentShoes();
loadModeBadge();
loadAlerts();
loadActivity();

// Auto-refresh every 15 s when Live Ops is active in another tab
if (localStorage.getItem("sim_live_ops") === "1") {
    setInterval(() => {
        loadDailySummary();
        loadRecentShoes();
        loadAlerts();
        loadActivity();
    }, 15_000);
}
