/**
 * quick_label.js — multi-worker gold labeling.
 *
 * Two views:
 *   PICKER  — choose a table photo to label. Each worker (identified by name)
 *             CLAIMS a whole table; claimed tables vanish from everyone else's
 *             list, so two people never label the same batch. A heartbeat keeps
 *             the claim alive; it auto-expires if the tab dies (server lease).
 *   LABEL   — keyboard-confirm every PENDING pair in the claimed table (brand +
 *             color chips, model optional). Saving sets review_status=COMPLETED
 *             via reviewPair → that pair becomes a human-verified GOLD label.
 *             When the table is emptied it's released and you return to PICKER.
 *
 * Controlled-vocabulary chips (never free text) keep the gold taxonomy canonical.
 * label_action (confirmed vs corrected) is recorded as a free AI-accuracy meter.
 */

const BRANDS = ["Nike", "Brooks", "Hoka", "New Balance", "ASICS", "Saucony",
                "Adidas", "Altra", "On", "Mizuno", "Reebok", "Under Armour",
                "Puma", "Merrell", "Other"];
const BRAND_ALIAS = { asics: "ASICS", "new balance": "New Balance", newbalance: "New Balance",
    "under armour": "Under Armour", underarmour: "Under Armour", "hoka one one": "Hoka",
    "on running": "On", onrunning: "On" };
const COLORS = [["black","#111827"],["white","#e5e7eb"],["gray","#9ca3af"],
    ["brown","#92400e"],["red","#ef4444"],["orange","#f97316"],["yellow","#eab308"],
    ["green","#22c55e"],["blue","#3b82f6"],["purple","#a855f7"],["pink","#ec4899"],
    ["unknown","#cbd5e1"]];

const QL = { worker: null, table: null, queue: [], cur: null, labeled: 0,
             session: 0, brand: null, color: null, hb: null };

const $ = (id) => document.getElementById(id);
const conf = (c) => c == null ? "—" : Math.round(c * 100) + "%";
function esc(s){ return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

function canonBrand(s) {
    const k = (s || "").trim().toLowerCase();
    if (!k || k === "unknown") return null;
    if (BRAND_ALIAS[k]) return BRAND_ALIAS[k];
    return BRANDS.find(b => b.toLowerCase() === k) || null;
}

/* ===== worker identity ============================================== */
function showNamePrompt() {
    $("ql-name-overlay").classList.add("open");
    $("ql-name-input").value = QL.worker || "";
    $("ql-name-input").focus();
}
function commitName() {
    const v = $("ql-name-input").value.trim();
    if (!v) { $("ql-name-input").focus(); return; }
    QL.worker = v;
    localStorage.setItem("ql_worker", v);
    $("ql-worker-name").textContent = v;
    $("ql-name-overlay").classList.remove("open");
    showPicker();
}

/* ===== VIEW: picker ================================================= */
async function showPicker() {
    if (QL.table) { try { await api.releaseTable(QL.table, QL.worker); } catch(e){} }
    stopHeartbeat();
    QL.table = null; QL.cur = null; QL.queue = [];
    $("ql-labelview").style.display = "none";
    $("ql-picker").style.display = "block";
    await loadTables();
}

async function loadTables() {
    const c = $("ql-tables");
    try {
        const data = await api.getLabelTables(QL.worker);
        if (!data.tables.length) { c.innerHTML = `<div class="empty-state">🎉 No tables left with pending pairs. All caught up!</div>`; return; }
        c.innerHTML = data.tables.map(t => {
            const thumb = t.thumb_path
                ? `<img class="ql-table-thumb" src="${esc(t.thumb_path)}" alt="table" loading="lazy">`
                : `<div class="ql-table-thumb ql-table-thumb--none">no image</div>`;
            const done = (t.total || 0) - t.pending;
            return `<div class="ql-table-card" data-id="${esc(t.id)}">
                ${thumb}
                <div class="ql-table-body">
                    <div class="id">${esc(t.id)}</div>
                    <div class="meta">
                        <span class="ql-pill ${t.mine ? "ql-resume" : ""}">${t.mine ? "▶ Resume" : t.pending + " to label"}</span>
                        <span>${done}/${t.total || "?"} done${t.barcode ? " · 📦" : ""}</span>
                    </div>
                </div></div>`;
        }).join("");
        c.querySelectorAll(".ql-table-card").forEach(el =>
            el.addEventListener("click", () => pickTable(el.dataset.id)));
    } catch (err) {
        c.innerHTML = `<div class="error-state">Could not load tables: ${esc(err.message)}</div>`;
    }
}

async function pickTable(tpId) {
    try {
        await api.claimTable(tpId, QL.worker);          // 409 if someone grabbed it
    } catch (err) {
        showToast(err.message || "Could not claim table", "error", 2600);
        return loadTables();                            // refresh — it's likely taken
    }
    enterTable(tpId);
}

/* ===== VIEW: labeling a table ======================================= */
async function enterTable(tpId) {
    QL.table = tpId; QL.cur = null; QL.queue = []; QL.labeled = 0;
    $("ql-picker").style.display = "none";
    $("ql-labelview").style.display = "block";
    $("ql-table-id").textContent = tpId;
    startHeartbeat();
    try {
        const data = await api.getPairs({ table_photo_id: tpId, review_status: "PENDING", page_size: 500 });
        // Highest-value first within the table: mid-confidence (~0.6) leads.
        QL.queue = (data.items || []).sort((a, b) =>
            Math.abs((a.make_confidence ?? 0.5) - 0.6) - Math.abs((b.make_confidence ?? 0.5) - 0.6));
        QL.total = QL.queue.length;
        next();
    } catch (err) { showError(err.message); }
}

function tableProgress() {
    const left = QL.queue.length + (QL.cur ? 1 : 0);
    $("ql-pending").textContent = left;
    $("ql-table-progress").textContent = `${QL.labeled} done · ${left} left`;
}

function next() {
    if (!QL.queue.length) return tableComplete();
    QL.cur = QL.queue.shift();
    $("ql-stage").style.display = "grid";
    $("ql-empty").style.display = "none";
    showCurrent();
    tableProgress();
}

async function tableComplete() {
    QL.cur = null;
    $("ql-stage").style.display = "none";
    const e = $("ql-empty"); e.style.display = "block";
    e.innerHTML = `✅ <b>${esc(QL.table)}</b> fully labeled — ${QL.labeled} pairs. Returning to tables…`;
    try { await api.releaseTable(QL.table, QL.worker); } catch(err){}
    stopHeartbeat();
    setTimeout(showPicker, 1200);
}

/* ===== card render + chips ========================================= */
function buildChips() {
    $("ql-brand-chips").innerHTML = BRANDS.map((b, i) =>
        `<div class="ql-chip" data-brand="${b}">${i < 9 ? `<span class="key">${i+1}</span>` : ""}${b}</div>`).join("");
    $("ql-color-chips").innerHTML = COLORS.map(([c, hex]) =>
        `<div class="ql-chip ql-color-chip" data-color="${c}" title="${c}" style="background:${hex}"></div>`).join("");
    $("ql-brand-chips").querySelectorAll(".ql-chip").forEach(el =>
        el.addEventListener("click", () => selectBrand(el.dataset.brand)));
    $("ql-color-chips").querySelectorAll(".ql-chip").forEach(el =>
        el.addEventListener("click", () => selectColor(el.dataset.color)));
}
function selectBrand(b) {
    QL.brand = b;
    $("ql-brand-chips").querySelectorAll(".ql-chip").forEach(el => el.classList.toggle("sel", el.dataset.brand === b));
    $("ql-other-make").style.display = (b === "Other") ? "block" : "none";
    if (b === "Other") $("ql-other-make").focus();
    updateActionHint();
}
function selectColor(c) {
    QL.color = c;
    $("ql-color-chips").querySelectorAll(".ql-chip").forEach(el => el.classList.toggle("sel", el.dataset.color === c));
    updateActionHint();
}
function computeAction() {
    const p = QL.cur;
    const brandSame = QL.brand !== "Other" && QL.brand === canonBrand(p.make);
    const colorSame = QL.color === (p.detected_color || "unknown").toLowerCase();
    return (brandSame && colorSame) ? "confirmed" : "corrected";
}
function updateActionHint() {
    if (!QL.cur) return;
    $("ql-action-hint").innerHTML = computeAction() === "confirmed"
        ? "will save as <b>confirmed</b> (AI was right)"
        : "<span class='ql-corrected'>corrected</span> (you changed the AI guess)";
}
function showCurrent() {
    const p = QL.cur; if (!p) return;
    $("ql-img").src = p.image_path || "";
    $("ql-imgmeta").innerHTML =
        `<b>${esc(p.id)}</b><br>AI: <b>${esc(p.make || "—")}</b> (${conf(p.make_confidence)}) · ` +
        `${esc(p.model || "—")} (${conf(p.model_confidence)}) · ${esc(p.detected_color || "—")} (${conf(p.color_confidence)})` +
        (p.prediction_source ? `<br><span class="text-muted">${esc(p.prediction_source)}</span>` : "");
    $("ql-ai-make").textContent = p.make ? "AI: " + p.make : "";
    $("ql-ai-color").textContent = p.detected_color ? "AI: " + p.detected_color : "";
    selectBrand(canonBrand(p.make) || "Other");
    $("ql-other-make").value = (canonBrand(p.make) === null && p.make) ? p.make : "";
    selectColor(COLORS.some(c => c[0] === (p.detected_color || "").toLowerCase()) ? p.detected_color.toLowerCase() : "unknown");
    $("ql-model").value = p.model && p.model !== "unknown" ? p.model : "";
}

/* ===== actions ===================================================== */
async function approve() {
    const p = QL.cur; if (!p) return;
    let make = QL.brand;
    if (make === "Other") { make = $("ql-other-make").value.trim(); if (!make) { $("ql-other-make").focus(); return; } }
    const btn = $("ql-approve"); btn.disabled = true;
    try {
        await api.reviewPair(p.id, { final_make: make, final_color: QL.color,
            final_model: $("ql-model").value.trim() || null,
            label_action: computeAction(), review_status: "COMPLETED" });
        QL.labeled += 1; QL.session += 1;
        $("ql-session").textContent = QL.session;
        const goal = Math.max(1, parseInt($("ql-goal").value, 10) || 50);
        $("ql-bar-fill").style.width = Math.min(100, 100 * QL.session / goal) + "%";
        next();
    } catch (err) {
        showToast(err.message || "Save failed", "error", 2500);
    } finally { btn.disabled = false; }
}
async function del() {
    const p = QL.cur; if (!p) return;
    const btn = $("ql-del"); btn.disabled = true;
    try { await api.deletePair(p.id); showToast("Deleted " + p.id, "info", 800); next(); }
    catch (err) { showToast(err.message || "Delete failed", "error", 2500); }
    finally { btn.disabled = false; }
}
function skip() {                       // defer: send to the back of THIS table's queue
    if (QL.cur) QL.queue.push(QL.cur);
    next();
}
function showError(msg) { const e = $("ql-error"); e.style.display = "block"; e.textContent = "Error: " + msg; }

/* ===== heartbeat (keep my claim alive) ============================= */
function startHeartbeat() {
    stopHeartbeat();
    QL.hb = setInterval(() => { if (QL.table) api.heartbeatTable(QL.table, QL.worker).catch(()=>{}); }, 45000);
}
function stopHeartbeat() { if (QL.hb) { clearInterval(QL.hb); QL.hb = null; } }

/* ===== zoom ======================================================== */
function toggleZoom() {
    const o = $("ql-zoom");
    if (o.classList.contains("open")) return o.classList.remove("open");
    $("ql-zoom-img").src = QL.cur ? (QL.cur.image_path || "") : "";
    o.classList.add("open");
}

/* ===== wiring ====================================================== */
buildChips();
$("ql-name-go").addEventListener("click", commitName);
$("ql-name-input").addEventListener("keydown", (e) => { if (e.key === "Enter") commitName(); });
$("ql-change-worker").addEventListener("click", showNamePrompt);
$("ql-refresh-tables").addEventListener("click", () => { if (!QL.table) loadTables(); });
$("ql-back").addEventListener("click", showPicker);
$("ql-approve").addEventListener("click", approve);
$("ql-skip").addEventListener("click", skip);
$("ql-del").addEventListener("click", del);
$("ql-img").addEventListener("click", toggleZoom);
$("ql-zoom").addEventListener("click", () => $("ql-zoom").classList.remove("open"));
$("ql-other-make").addEventListener("input", updateActionHint);

document.addEventListener("keydown", (e) => {
    if ($("ql-name-overlay").classList.contains("open")) return;   // typing name
    if (!QL.table) return;                                         // only in label view
    const typing = e.target.tagName === "INPUT" && e.target.type === "text";
    if (e.key === "Enter") { e.preventDefault(); approve(); return; }
    if (e.key === "Escape") { $("ql-zoom").classList.remove("open"); return; }
    if (typing) return;
    if (e.key >= "1" && e.key <= "9") { const b = BRANDS[parseInt(e.key,10)-1]; if (b) selectBrand(b); }
    else if (e.key.toLowerCase() === "s") skip();
    else if (e.key.toLowerCase() === "d") del();
    else if (e.key.toLowerCase() === "z") toggleZoom();
});

// Best-effort release if the tab closes mid-table (lease expiry is the backstop).
window.addEventListener("beforeunload", () => {
    if (QL.table && navigator.sendBeacon) {
        navigator.sendBeacon(`/api/labeling/release/${encodeURIComponent(QL.table)}?worker=${encodeURIComponent(QL.worker)}`);
    }
});

/* ===== boot ======================================================== */
QL.worker = localStorage.getItem("ql_worker");
if (QL.worker) { $("ql-worker-name").textContent = QL.worker; showPicker(); }
else { showNamePrompt(); }
