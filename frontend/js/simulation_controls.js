/**
 * simulation_controls.js — Simulation control panel (System Health page).
 *
 * Uses apiFetch() directly so it works regardless of which api.js is cached.
 * initSimControls(mode) is called by loadHealth() with the fetched mode string.
 */

// ── Live Ops state ────────────────────────────────────────────────────────
let _liveTimer   = null;  // setTimeout handle for next generation
let _tickTimer   = null;  // setInterval handle for "X seconds ago" display
let _lastGenTime = null;  // Date.now() of last successful auto-generation

// ── Init ──────────────────────────────────────────────────────────────────

function initSimControls(mode) {
    const panel = document.getElementById("sim-controls-panel");
    if (!panel) return;
    panel.style.display = (mode === "actual") ? "none" : "";

    const toggle = document.getElementById("live-ops-toggle");
    if (!toggle) return;

    // Restore live ops state persisted across page navigation
    const wasLive = localStorage.getItem("sim_live_ops") === "1";
    toggle.checked = wasLive;
    _updateLiveBadge(wasLive);
    if (wasLive) _startLiveOps();

    toggle.addEventListener("change", () => {
        if (toggle.checked) {
            localStorage.setItem("sim_live_ops", "1");
            _startLiveOps();
        } else {
            localStorage.removeItem("sim_live_ops");
            _stopLiveOps();
        }
    });
}

// ── Manual generate ───────────────────────────────────────────────────────

async function generateInspections(count, btn) {
    const condition    = document.getElementById("sim-condition")?.value || "normal_mix";
    const originalText = btn.textContent.trim();
    btn.disabled    = true;
    btn.textContent = "Generating…";

    try {
        const result = await apiFetch("/api/simulation/generate", {
            method: "POST",
            body:   JSON.stringify({ count, condition }),
        });
        showSimFeedback(
            `Generated ${result.generated} new inspection${result.generated !== 1 ? "s" : ""}. ` +
            `Total records: ${result.total.toLocaleString()}`,
            "ok"
        );
        refreshPageAfterChange();
    } catch (err) {
        showSimFeedback("Error: " + err.message, "err");
    } finally {
        btn.disabled    = false;
        btn.textContent = originalText;
    }
}

// ── Reset ─────────────────────────────────────────────────────────────────

async function resetSimulation(btn) {
    if (!confirm(
        "Reset all simulation data?\n\n" +
        "This deletes every shoe and batch record, then re-seeds 50 fresh " +
        "records across 3 batches. This cannot be undone."
    )) return;

    const originalText = btn.textContent.trim();
    btn.disabled    = true;
    btn.textContent = "Resetting…";

    try {
        const result = await apiFetch("/api/simulation/reset", { method: "POST" });
        showSimFeedback(
            `Reset complete — ${result.seeded} records re-seeded across 3 batches.`,
            "ok"
        );
        refreshPageAfterChange();
    } catch (err) {
        showSimFeedback("Error: " + err.message, "err");
    } finally {
        btn.disabled    = false;
        btn.textContent = originalText;
    }
}

// ── Live Operations ───────────────────────────────────────────────────────

function _startLiveOps() {
    _stopLiveOps();
    _lastGenTime = Date.now();
    _scheduleNextGen();
    _startTick();
    _updateLiveBadge(true);
}

function _stopLiveOps() {
    clearTimeout(_liveTimer);
    clearInterval(_tickTimer);
    _liveTimer   = null;
    _tickTimer   = null;
    _lastGenTime = null;
    _updateLiveBadge(false);
}

function _scheduleNextGen() {
    // Randomise delay 10–20 s for a natural, non-mechanical feel
    const delay = (10 + Math.random() * 10) * 1000;
    _liveTimer = setTimeout(async () => {
        if (localStorage.getItem("sim_live_ops") !== "1") return;

        const condition = document.getElementById("sim-condition")?.value || "normal_mix";
        try {
            await apiFetch("/api/simulation/generate", {
                method: "POST",
                body:   JSON.stringify({ count: 1, condition }),
            });
            _lastGenTime = Date.now();
            refreshPageAfterChange();
        } catch (_) { /* continue even on transient error */ }

        if (localStorage.getItem("sim_live_ops") === "1") _scheduleNextGen();
    }, delay);
}

function _startTick() {
    _tickTimer = setInterval(() => {
        if (!_lastGenTime) return;
        const secs = Math.floor((Date.now() - _lastGenTime) / 1000);
        const el = document.getElementById("live-ops-last");
        if (el) el.textContent = secs < 3 ? "just now" : `${secs}s ago`;
    }, 1000);
}

function _updateLiveBadge(active) {
    const badge    = document.getElementById("live-ops-badge");
    const lastWrap = document.getElementById("live-ops-last-wrap");
    if (badge)    badge.style.display    = active ? "" : "none";
    if (lastWrap) lastWrap.style.display = active ? "" : "none";
}

// ── Page refresh helper ───────────────────────────────────────────────────

function refreshPageAfterChange() {
    if (typeof loadHealth       === "function") loadHealth();
    if (typeof loadHealthAlerts === "function") loadHealthAlerts();
    if (typeof loadDailySummary === "function") loadDailySummary();
    if (typeof loadRecentShoes  === "function") loadRecentShoes();
    if (typeof loadAlerts       === "function") loadAlerts();
    if (typeof loadActivity     === "function") loadActivity();
}

// ── Feedback toast ────────────────────────────────────────────────────────

function showSimFeedback(message, type) {
    const el = document.getElementById("sim-feedback");
    if (!el) return;
    el.className     = "sim-feedback sim-feedback-" + type;
    el.textContent   = message;
    el.style.display = "";
    clearTimeout(el._timer);
    el._timer = setTimeout(() => { el.style.display = "none"; }, 5000);
}
