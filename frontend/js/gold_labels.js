/**
 * gold_labels.js — browse the human-verified gold set.
 *
 * Shows only pairs a labeler CONFIRMED (review_status=COMPLETED + final_make),
 * newest first, with the human label and a confirmed/corrected badge. One
 * request per page (GET /api/pairs/gold), images lazy-loaded, grid replaced per
 * page so the DOM never holds more than one page — stays fast as the set grows.
 */

const GL = { page: 1, pageSize: 60, total: 0, make: "", filledFilter: false };

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
    GL.page = data.page; GL.total = data.total;
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

$("gl-make").addEventListener("change", (e) => { GL.make = e.target.value; load(1); });
$("gl-refresh").addEventListener("click", () => { GL.filledFilter = false; load(GL.page); });
$("gl-zoom").addEventListener("click", () => $("gl-zoom").classList.remove("open"));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("gl-zoom").classList.remove("open"); });

load(1);
