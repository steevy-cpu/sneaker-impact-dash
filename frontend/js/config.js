/**
 * config.js — Station Config page
 *
 * Camera section talks to the server-side v4l2 control API (the camera is on
 * the station box). Controls apply live to the real device. Device + resolution
 * choices are saved to the shared server-side station config.
 */

const cfgState = { device: null, station: {} };

function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* ---- Camera ---------------------------------------------------------- */

async function loadDevices() {
    const sel = document.getElementById("cam-device");
    const controls = document.getElementById("cam-controls");
    let data;
    try {
        data = await api.getCameraDevices();
    } catch (err) {
        showError(controls, "Could not query cameras: " + err.message);
        return;
    }
    if (!data.available) {
        sel.innerHTML = `<option>v4l2-ctl not installed on server</option>`;
        sel.disabled = true;
        showEmpty(controls, "Camera control unavailable (v4l2-ctl missing on the station).");
        return;
    }
    if (!data.devices.length) {
        sel.innerHTML = `<option>No camera detected</option>`;
        sel.disabled = true;
        showEmpty(controls, "No camera is attached to the station box. Plug in the USB camera and click Refresh.");
        return;
    }
    sel.disabled = false;
    sel.innerHTML = data.devices
        .map(d => `<option value="${esc(d.path)}">${esc(d.name)} (${esc(d.path)})</option>`).join("");
    // Restore the saved device if still present.
    const saved = cfgState.station.cameraDevice;
    if (saved && data.devices.some(d => d.path === saved)) sel.value = saved;
    cfgState.device = sel.value;
    await Promise.all([loadControls(), loadResolutions()]);
}

async function loadControls() {
    const container = document.getElementById("cam-controls");
    showLoading(container);
    let data;
    try {
        data = await api.getCameraControls(cfgState.device);
    } catch (err) {
        showError(container, "Could not read controls: " + err.message);
        return;
    }
    if (!data.controls.length) {
        showEmpty(container, "This device exposes no adjustable controls.");
        return;
    }
    container.className = "cam-controls";
    container.innerHTML = data.controls.map(controlRowHTML).join("");
    data.controls.forEach(wireControl);
}

function controlRowHTML(c) {
    const label = c.name.replace(/_/g, " ");
    let input;
    if (c.type === "bool") {
        input = `<input type="checkbox" class="cam-input" data-name="${esc(c.name)}" data-type="bool" ${c.value ? "checked" : ""}>`;
    } else if (c.type === "menu") {
        input = `<select class="form-select cam-input" data-name="${esc(c.name)}" data-type="menu">` +
            (c.options || []).map(o => `<option value="${o.value}" ${o.value === c.value ? "selected" : ""}>${esc(o.label)}</option>`).join("") +
            `</select>`;
    } else {
        input = `<input type="range" class="cam-input cam-range" data-name="${esc(c.name)}" data-type="int"
                    min="${c.min}" max="${c.max}" step="${c.step || 1}" value="${c.value}">
                 <span class="cam-val" id="val-${esc(c.name)}">${c.value}</span>`;
    }
    return `<div class="cam-ctrl">
        <label class="cam-ctrl-label" title="${esc(c.name)}">${esc(label)}</label>
        <div class="cam-ctrl-input">${input}</div>
    </div>`;
}

function wireControl(c) {
    const el = document.querySelector(`.cam-input[data-name="${CSS.escape(c.name)}"]`);
    if (!el) return;
    const valSpan = document.getElementById("val-" + c.name);

    const apply = async (value) => {
        try {
            await api.setCameraControl(cfgState.device, c.name, value);
        } catch (err) {
            showToast(`${c.name}: ${err.message}`, "error", 2500);
        }
    };
    if (c.type === "bool") {
        el.addEventListener("change", () => apply(el.checked ? 1 : 0));
    } else if (c.type === "menu") {
        el.addEventListener("change", () => apply(parseInt(el.value, 10)));
    } else {
        el.addEventListener("input", () => { if (valSpan) valSpan.textContent = el.value; });
        el.addEventListener("change", () => apply(parseInt(el.value, 10)));  // on release
    }
}

async function loadResolutions() {
    const sel = document.getElementById("cam-resolution");
    let data;
    try {
        data = await api.getCameraResolutions(cfgState.device);
    } catch (err) {
        sel.innerHTML = `<option>—</option>`;
        return;
    }
    if (!data.resolutions.length) {
        sel.innerHTML = `<option value="">(unknown)</option>`;
        return;
    }
    sel.innerHTML = data.resolutions
        .map(r => `<option value="${r.width}x${r.height}">${r.width} × ${r.height}</option>`).join("");
    if (cfgState.station.cameraResolution) sel.value = cfgState.station.cameraResolution;
}

/* ---- Integrations ---------------------------------------------------- */

function dot(ok) { return `<span class="status-dot ${ok ? "on" : "off"}"></span>`; }

async function loadIntegrations() {
    const c = document.getElementById("integrations");
    try {
        const s = await api.getIntegrations();
        c.innerHTML = `
        <div class="cfg-status-row">${dot(s.airtable.lookup_configured)} <strong>Airtable lookup</strong>
            <span class="text-muted text-sm">${s.airtable.lookup_configured ? "configured" : "not configured (set AIRTABLE_API_KEY + AIRTABLE_BASE_ID)"}</span></div>
        <div class="cfg-status-row">${dot(s.airtable.sync_enabled)} <strong>Airtable write sync</strong>
            <span class="text-muted text-sm">${s.airtable.sync_enabled ? "active" : "dormant"} · table “${esc(s.airtable.shipments_table)}”</span></div>
        <div class="cfg-status-row">${dot(s.engine.enabled)} <strong>Engine worker</strong>
            <span class="text-muted text-sm">${s.engine.enabled ? "running" : "disabled"} · model ${esc(s.engine.ollama_model)} · seg ${esc(s.engine.segment_model)} · auto-approve ≥ ${s.engine.auto_approve}</span></div>`;
    } catch (err) {
        showError(c, "Could not load integrations: " + err.message);
    }
}

/* ---- Save + init ----------------------------------------------------- */

async function saveStation() {
    cfgState.station.cameraDevice = document.getElementById("cam-device").value || null;
    cfgState.station.cameraResolution = document.getElementById("cam-resolution").value || null;
    try {
        await api.saveStationConfig(cfgState.station);
        showToast("✅ Station settings saved", "success", 1800);
    } catch (err) {
        showToast("Save failed: " + err.message, "error", 3000);
    }
}

async function init() {
    try { cfgState.station = await api.getStationConfig(); } catch { cfgState.station = {}; }
    await loadDevices();
    loadIntegrations();

    document.getElementById("cam-device").addEventListener("change", async (e) => {
        cfgState.device = e.target.value;
        await Promise.all([loadControls(), loadResolutions()]);
    });
    document.getElementById("cam-refresh").addEventListener("click", loadDevices);
    document.getElementById("cam-reset").addEventListener("click", async () => {
        if (!cfgState.device) return;
        try { await api.resetCamera(cfgState.device); await loadControls(); showToast("Controls reset to defaults", "info", 1600); }
        catch (err) { showToast("Reset failed: " + err.message, "error", 2500); }
    });
    document.getElementById("cfg-save").addEventListener("click", saveStation);
}

init();
