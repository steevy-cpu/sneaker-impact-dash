/**
 * quick_label.js — fast keyboard-driven brand+color confirmation.
 *
 * Pulls PENDING pairs from /api/pairs/gold-queue (value-ordered: mid-confidence
 * first, brand-diversified) and shows ONE at a time. The worker confirms or
 * corrects brand (chips/keys) + color, then Enter saves via the existing
 * reviewPair endpoint with review_status=COMPLETED — turning each into a
 * human-verified GOLD label. We record label_action (confirmed vs corrected)
 * so we also get a free measurement of the AI's real accuracy.
 *
 * Controlled vocabulary = chips, never free text for brand/color, so the gold
 * set stays canonical by construction (no ASICS/Asics drift).
 */

// Canonical brands (top of the dataset) + Other→free-text. First 9 get number keys.
const BRANDS = ["Nike", "Brooks", "Hoka", "New Balance", "ASICS", "Saucony",
                "Adidas", "Altra", "On", "Mizuno", "Reebok", "Under Armour",
                "Puma", "Merrell", "Other"];
// Alias → canonical, mirrors the backend build_dataset.py normalization.
const BRAND_ALIAS = { asics: "ASICS", "new balance": "New Balance", newbalance: "New Balance",
    "under armour": "Under Armour", underarmour: "Under Armour", "hoka one one": "Hoka",
    "on running": "On", onrunning: "On" };
const COLORS = [["black","#111827"],["white","#e5e7eb"],["gray","#9ca3af"],
    ["brown","#92400e"],["red","#ef4444"],["orange","#f97316"],["yellow","#eab308"],
    ["green","#22c55e"],["blue","#3b82f6"],["purple","#a855f7"],["pink","#ec4899"],
    ["unknown","#cbd5e1"]];

const QL = { queue: [], cur: null, session: 0, pending: 0,
             brand: null, color: null };

function canonBrand(s) {
    const k = (s || "").trim().toLowerCase();
    if (!k || k === "unknown") return null;
    if (BRAND_ALIAS[k]) return BRAND_ALIAS[k];
    const hit = BRANDS.find(b => b.toLowerCase() === k);
    return hit || null;          // unknown spelling → no chip preselected (→ Other)
}

const $ = (id) => document.getElementById(id);
const conf = (c) => c == null ? "—" : Math.round(c * 100) + "%";

/* ---- chips ----------------------------------------------------------- */
function buildChips() {
    $("ql-brand-chips").innerHTML = BRANDS.map((b, i) =>
        `<div class="ql-chip" data-brand="${b}">${i < 9 ? `<span class="key">${i+1}</span>` : ""}${b}</div>`
    ).join("");
    $("ql-color-chips").innerHTML = COLORS.map(([c, hex]) =>
        `<div class="ql-chip ql-color-chip" data-color="${c}" title="${c}" style="background:${hex}"></div>`
    ).join("");
    $("ql-brand-chips").querySelectorAll(".ql-chip").forEach(el =>
        el.addEventListener("click", () => selectBrand(el.dataset.brand)));
    $("ql-color-chips").querySelectorAll(".ql-chip").forEach(el =>
        el.addEventListener("click", () => selectColor(el.dataset.color)));
}

function selectBrand(b) {
    QL.brand = b;
    $("ql-brand-chips").querySelectorAll(".ql-chip").forEach(el =>
        el.classList.toggle("sel", el.dataset.brand === b));
    $("ql-other-make").style.display = (b === "Other") ? "block" : "none";
    if (b === "Other") $("ql-other-make").focus();
    updateActionHint();
}
function selectColor(c) {
    QL.color = c;
    $("ql-color-chips").querySelectorAll(".ql-chip").forEach(el =>
        el.classList.toggle("sel", el.dataset.color === c));
    updateActionHint();
}

/* ---- the confirmed-vs-corrected indicator ---------------------------- */
function computeAction() {
    const p = QL.cur;
    const aiBrand = canonBrand(p.make);
    const aiColor = (p.detected_color || "unknown").toLowerCase();
    const brandSame = QL.brand !== "Other" && QL.brand === aiBrand;
    const colorSame = QL.color === aiColor;
    return (brandSame && colorSame) ? "confirmed" : "corrected";
}
function updateActionHint() {
    if (!QL.cur) return;
    const a = computeAction();
    $("ql-action-hint").innerHTML = a === "confirmed"
        ? "will save as <b>confirmed</b> (AI was right)"
        : "<span class='ql-corrected'>corrected</span> (you changed the AI guess)";
}

/* ---- render one pair ------------------------------------------------- */
function showCurrent() {
    const p = QL.cur;
    if (!p) return;
    $("ql-img").src = p.image_path || "";
    $("ql-imgmeta").innerHTML =
        `<b>${esc(p.id)}</b> · from ${esc(p.table_photo_id)}<br>` +
        `AI: <b>${esc(p.make || "—")}</b> (${conf(p.make_confidence)}) · ` +
        `${esc(p.model || "—")} (${conf(p.model_confidence)}) · ` +
        `${esc(p.detected_color || "—")} (${conf(p.color_confidence)})` +
        (p.prediction_source ? `<br><span class="text-muted">${esc(p.prediction_source)}</span>` : "");
    $("ql-ai-make").textContent = p.make ? "AI: " + p.make : "";
    $("ql-ai-color").textContent = p.detected_color ? "AI: " + p.detected_color : "";
    // Pre-select the AI guesses (the fast path = just press Enter when right).
    selectBrand(canonBrand(p.make) || "Other");
    if (canonBrand(p.make) === null && p.make) $("ql-other-make").value = p.make;
    else $("ql-other-make").value = "";
    selectColor(COLORS.some(c => c[0] === (p.detected_color || "").toLowerCase())
        ? p.detected_color.toLowerCase() : "unknown");
    $("ql-model").value = p.model && p.model !== "unknown" ? p.model : "";
}

function esc(s) { return String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

/* ---- queue ----------------------------------------------------------- */
async function fillQueue() {
    const data = await api.getGoldQueue(20);
    QL.queue = data.items || [];
    QL.pending = data.total_pending || 0;
    $("ql-pending").textContent = QL.pending.toLocaleString();
}

async function next() {
    if (!QL.queue.length) {
        try { await fillQueue(); }
        catch (err) { return showError(err.message); }
    }
    if (!QL.queue.length) return showDone();
    QL.cur = QL.queue.shift();
    $("ql-stage").style.display = "grid";
    $("ql-empty").style.display = "none";
    showCurrent();
}

function showDone() {
    $("ql-stage").style.display = "none";
    const e = $("ql-empty");
    e.style.display = "block";
    e.textContent = QL.pending === 0
        ? "🎉 All pending pairs have been labeled. Nothing left in the queue."
        : "Queue empty for now — refresh to pull more.";
}
function showError(msg) {
    const e = $("ql-error"); e.style.display = "block";
    e.textContent = "Error: " + msg;
}

function bumpProgress() {
    QL.session += 1;
    QL.pending = Math.max(0, QL.pending - 1);
    $("ql-session").textContent = QL.session;
    $("ql-pending").textContent = QL.pending.toLocaleString();
    const goal = Math.max(1, parseInt($("ql-goal").value, 10) || 50);
    $("ql-bar-fill").style.width = Math.min(100, 100 * QL.session / goal) + "%";
}

/* ---- actions --------------------------------------------------------- */
async function approve() {
    const p = QL.cur; if (!p) return;
    let make = QL.brand;
    if (make === "Other") {
        make = $("ql-other-make").value.trim();
        if (!make) { $("ql-other-make").focus(); return; }   // need a brand
    }
    const btn = $("ql-approve"); btn.disabled = true;
    try {
        await api.reviewPair(p.id, {
            final_make: make,
            final_color: QL.color,
            final_model: $("ql-model").value.trim() || null,
            label_action: computeAction(),
            review_status: "COMPLETED",
        });
        bumpProgress();
        showToast("✅ " + make + (computeAction() === "corrected" ? " (corrected)" : ""), "success", 900);
        await next();
    } catch (err) {
        showToast(err.message || "Save failed", "error", 2500);
    } finally { btn.disabled = false; }
}

async function del() {
    const p = QL.cur; if (!p) return;
    const btn = $("ql-del"); btn.disabled = true;
    try {
        await api.deletePair(p.id);
        QL.pending = Math.max(0, QL.pending - 1);
        $("ql-pending").textContent = QL.pending.toLocaleString();
        showToast("Deleted " + p.id, "info", 900);
        await next();
    } catch (err) {
        showToast(err.message || "Delete failed", "error", 2500);
    } finally { btn.disabled = false; }
}

function skip() { next(); }   // leaves it PENDING; just moves on

/* ---- zoom ------------------------------------------------------------ */
function toggleZoom() {
    const o = $("ql-zoom");
    if (o.classList.contains("open")) { o.classList.remove("open"); return; }
    $("ql-zoom-img").src = QL.cur ? (QL.cur.image_path || "") : "";
    o.classList.add("open");
}

/* ---- wiring ---------------------------------------------------------- */
buildChips();
$("ql-approve").addEventListener("click", approve);
$("ql-skip").addEventListener("click", skip);
$("ql-del").addEventListener("click", del);
$("ql-img").addEventListener("click", toggleZoom);
$("ql-zoom").addEventListener("click", () => $("ql-zoom").classList.remove("open"));
$("ql-other-make").addEventListener("input", updateActionHint);

document.addEventListener("keydown", (e) => {
    // Don't hijack typing in the Other/model text inputs (except Enter/Esc).
    const typing = e.target.tagName === "INPUT" && e.target.type === "text";
    if (e.key === "Enter") { e.preventDefault(); approve(); return; }
    if (e.key === "Escape") { $("ql-zoom").classList.remove("open"); return; }
    if (typing) return;
    if (e.key >= "1" && e.key <= "9") {
        const b = BRANDS[parseInt(e.key, 10) - 1];
        if (b) selectBrand(b);
    } else if (e.key.toLowerCase() === "s") { skip(); }
    else if (e.key.toLowerCase() === "d") { del(); }
    else if (e.key.toLowerCase() === "z") { toggleZoom(); }
});

next();
