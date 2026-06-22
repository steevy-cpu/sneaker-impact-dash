/**
 * gold_labels.js — browse the human-verified gold set.
 *
 * Shows only pairs a labeler CONFIRMED (review_status=COMPLETED + final_make),
 * newest first, with the human label and a confirmed/corrected badge. One
 * request per page (GET /api/pairs/gold), images lazy-loaded, grid replaced per
 * page so the DOM never holds more than one page — stays fast as the set grows.
 */

const GL = { page: 1, pageSize: 60, total: 0, make: "", filledFilter: false,
             items: [], edit: null, eBrand: null, eColor: null };

// Controlled vocabulary (mirrors Quick Label / build_dataset) — editing stays
// canonical, so a fix can't reintroduce a dirty brand spelling.
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
function canonBrand(s) {
    const k = (s || "").trim().toLowerCase();
    if (!k || k === "unknown") return null;
    return BRAND_ALIAS[k] || BRANDS.find(b => b.toLowerCase() === k) || null;
}

function esc(s){ return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
const $ = (id) => document.getElementById(id);

async function load(page) {
    if (page == null) page = GL.page;
    const c = $("gl-container");
    c.innerHTML = `<div class="loading-state">Loading…</div>`;
    let data;
    try {
        data = await api.getGoldLabels({ page, page_size: GL.pageSize, make: GL.make });
    } catch (err) {
        c.innerHTML = `<div class="error-state">Could not load gold labels: ${esc(err.message)}</div>`;
        return;
    }
    GL.page = data.page; GL.total = data.total; GL.items = data.items;
    renderStats(data.stats);

    if (!data.items.length) {
        if (data.page > 1) return load(1);                 // page emptied → snap back
        c.innerHTML = `<div class="empty-state">${GL.make
            ? "No gold labels for this brand yet."
            : "No gold labels yet. Confirm pairs in <b>Quick Label</b> to build the human-verified set — they'll appear here."}</div>`;
        $("gl-pagination").style.display = "none";
        return;
    }
    c.innerHTML = `<div class="gl-grid">${data.items.map(cardHTML).join("")}</div>`;
    c.querySelectorAll(".gl-img").forEach(img => img.addEventListener("click", () => zoom(img.src)));
    c.querySelectorAll(".gl-edit-btn").forEach(b => b.addEventListener("click", () => openEditor(b.dataset.id)));
    renderPagination();
    c.scrollIntoView({ block: "start" });
}

function cardHTML(p) {
    const make  = p.final_make  || p.make  || "—";
    const model = p.final_model || p.model || "";
    const color = p.final_color || p.detected_color || "";
    const corrected = p.label_action === "corrected";
    const badge = corrected
        ? `<span class="gl-badge gl-corrected">✎ corrected</span>`
        : `<span class="gl-badge gl-confirmed">✓ confirmed</span>`;
    // When corrected, show what the AI had guessed (the teaching signal).
    const was = corrected && p.make && p.make.toLowerCase() !== make.toLowerCase()
        ? `<div class="gl-was">AI said: ${esc(p.make)}</div>` : "";
    const img = p.image_path
        ? `<img class="gl-img" src="${esc(p.image_path)}" alt="${esc(make)}" loading="lazy">`
        : `<div class="gl-img"></div>`;
    return `<div class="gl-card">
        ${img}
        <div class="gl-body">
            <div class="gl-make">${esc(make)}</div>
            <div class="gl-sub">${esc(model || "—")}${color ? " · " + esc(color) : ""}</div>
            ${badge}${was}
            <button class="gl-edit-btn" data-id="${esc(p.id)}" type="button">✎ Edit</button>
        </div></div>`;
}

function renderStats(s) {
    if (!s) return;
    $("gl-total").textContent = (s.total || 0).toLocaleString();
    $("gl-brands").textContent = (s.by_make || []).length;
    const pct = s.total ? Math.round(100 * (s.corrected || 0) / s.total) : 0;
    $("gl-corrected").textContent = (s.corrected || 0).toLocaleString() + (s.total ? ` · ${pct}%` : "");
    if (!GL.filledFilter && s.by_make && s.by_make.length) {
        const sel = $("gl-make");
        sel.innerHTML = `<option value="">All brands (${s.total})</option>` +
            s.by_make.map(m => `<option value="${esc(m.make)}">${esc(m.make)} (${m.count})</option>`).join("");
        sel.value = GL.make;
        GL.filledFilter = true;
    }
}

function renderPagination() {
    const el = $("gl-pagination");
    const pages = Math.max(1, Math.ceil(GL.total / GL.pageSize));
    if (GL.total <= GL.pageSize) { el.style.display = "none"; return; }
    el.style.display = "flex";
    el.innerHTML =
        `<button class="page-btn" id="gl-prev" ${GL.page <= 1 ? "disabled" : ""}>Prev</button>
         <span class="page-info">Page ${GL.page} of ${pages} · ${GL.total} labels</span>
         <button class="page-btn" id="gl-next" ${GL.page >= pages ? "disabled" : ""}>Next</button>`;
    $("gl-prev").addEventListener("click", () => load(GL.page - 1));
    $("gl-next").addEventListener("click", () => load(GL.page + 1));
}

function zoom(src) {
    $("gl-zoom-img").src = src;
    $("gl-zoom").classList.add("open");
}

/* ---- edit a gold label (fix an error) -------------------------------- */
function buildEditChips() {
    $("gl-edit-brands").innerHTML = BRANDS.map(b => `<div class="gl-chip" data-brand="${b}">${b}</div>`).join("");
    $("gl-edit-colors").innerHTML = COLORS.map(([c, hex]) =>
        `<div class="gl-chip gl-color-chip" data-color="${c}" title="${c}" style="background:${hex}"></div>`).join("");
    $("gl-edit-brands").querySelectorAll(".gl-chip").forEach(el =>
        el.addEventListener("click", () => selBrand(el.dataset.brand)));
    $("gl-edit-colors").querySelectorAll(".gl-chip").forEach(el =>
        el.addEventListener("click", () => selColor(el.dataset.color)));
}
function selBrand(b) {
    GL.eBrand = b;
    $("gl-edit-brands").querySelectorAll(".gl-chip").forEach(el => el.classList.toggle("sel", el.dataset.brand === b));
    $("gl-edit-other").style.display = (b === "Other") ? "block" : "none";
    if (b === "Other") $("gl-edit-other").focus();
}
function selColor(c) {
    GL.eColor = c;
    $("gl-edit-colors").querySelectorAll(".gl-chip").forEach(el => el.classList.toggle("sel", el.dataset.color === c));
}
function openEditor(id) {
    const p = GL.items.find(x => x.id === id);
    if (!p) return;
    GL.edit = p;
    $("gl-edit-id").textContent = id;
    $("gl-edit-img").src = p.image_path || "";
    const curMake = p.final_make || p.make || "";
    const cb = canonBrand(curMake);
    selBrand(cb || "Other");
    $("gl-edit-other").value = (cb === null && curMake) ? curMake : "";
    const curColor = (p.final_color || p.detected_color || "unknown").toLowerCase();
    selColor(COLORS.some(c => c[0] === curColor) ? curColor : "unknown");
    $("gl-edit-model").value = (p.final_model || p.model || "") === "unknown" ? "" : (p.final_model || p.model || "");
    $("gl-edit").classList.add("open");
}
function closeEditor() { $("gl-edit").classList.remove("open"); GL.edit = null; }

async function saveEdit() {
    const p = GL.edit; if (!p) return;
    let make = GL.eBrand;
    if (make === "Other") { make = $("gl-edit-other").value.trim(); if (!make) { $("gl-edit-other").focus(); return; } }
    // confirmed vs corrected = is the final label still the AI's original guess?
    const action = (make === canonBrand(p.make) && GL.eColor === (p.detected_color || "unknown").toLowerCase())
        ? "confirmed" : "corrected";
    const btn = $("gl-save"); btn.disabled = true;
    try {
        await api.reviewPair(p.id, { final_make: make, final_color: GL.eColor,
            final_model: $("gl-edit-model").value.trim() || null,
            label_action: action, review_status: "COMPLETED" });
        showToast("✅ Updated " + p.id, "success", 1500);
        closeEditor(); GL.filledFilter = false; load(GL.page);
    } catch (err) { showToast(err.message || "Save failed", "error", 2800); }
    finally { btn.disabled = false; }
}
async function sendToReview() {
    const p = GL.edit; if (!p) return;
    if (!confirm("Remove this from the gold set and send it back to the Quick Label queue?")) return;
    try {
        await api.reviewPair(p.id, { final_make: null, final_color: null, final_model: null,
            label_action: null, review_status: "PENDING" });
        showToast("↩ Sent back to review", "info", 1500);
        closeEditor(); GL.filledFilter = false; load(GL.page);
    } catch (err) { showToast(err.message || "Failed", "error", 2800); }
}
async function deleteGold() {
    const p = GL.edit; if (!p) return;
    if (!confirm(`Delete pair ${p.id} entirely?\n\nUse this if it isn't a shoe. Permanently removes the pair + crop.`)) return;
    try {
        await api.deletePair(p.id);
        showToast("Deleted " + p.id, "info", 1500);
        closeEditor(); GL.filledFilter = false; load(GL.page);
    } catch (err) { showToast(err.message || "Delete failed", "error", 2800); }
}

buildEditChips();
$("gl-make").addEventListener("change", (e) => { GL.make = e.target.value; load(1); });
$("gl-refresh").addEventListener("click", () => { GL.filledFilter = false; load(GL.page); });
$("gl-zoom").addEventListener("click", () => $("gl-zoom").classList.remove("open"));
$("gl-save").addEventListener("click", saveEdit);
$("gl-cancel").addEventListener("click", closeEditor);
$("gl-toreview").addEventListener("click", sendToReview);
$("gl-del2").addEventListener("click", deleteGold);
$("gl-edit").addEventListener("click", (e) => { if (e.target.id === "gl-edit") closeEditor(); });
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { $("gl-zoom").classList.remove("open"); closeEditor(); }
});

load(1);
