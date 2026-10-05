/**
 * ai_database.js — AI Database page
 *
 * Searchable browser over every pair the AI has identified (the `pairs` table,
 * ~100k rows): free-text search across brand / model / color, a brand filter,
 * and a paged card grid. Reuses the Label Data page's grid styling (ld-*) so
 * it looks native. Read-only; talks to /api/ai-database/* only.
 */
const DB = { page: 1, pageSize: 48, total: 0, facets: null, timer: null };

function dbEsc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function dbPct(v) {
    return typeof v === "number" ? Math.round(v * 100) + "%" : "—";
}
async function dbGet(path) {
    const res = await fetch(path);
    if (res.status === 401) throw new Error("IT login required (open /it)");
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
}

/* ---- Facets: brand dropdown + totals ---------------------------------- */
async function loadFacets() {
    try {
        DB.facets = await dbGet("/api/ai-database/facets");
    } catch (err) {
        return;   // stats/filter are optional; the search still works without them
    }
    const sel = document.getElementById("db-make");
    const cur = sel.value;
    sel.innerHTML = ['<option value="">All brands</option>'].concat(
        DB.facets.makes.map(m => `<option value="${dbEsc(m.make)}">${dbEsc(m.make)} (${m.count})</option>`)).join("");
    sel.value = cur;
    renderStats();
}
function renderStats() {
    const el = document.getElementById("db-stats");
    const t = DB.facets && DB.facets.totals;
    const shown = DB.total ? `<span class="ld-chip"><b>${DB.total}</b> matching</span>` : "";
    el.innerHTML = t
        ? `<span class="ld-chip ld-chip--total">${t.pairs} pairs</span>
           <span class="ld-chip"><b>${t.makes}</b> brands</span>
           <span class="ld-chip"><b>${t.models}</b> models</span>${shown}`
        : shown;
}

/* ---- List ------------------------------------------------------------ */
async function loadList(page) {
    if (page == null) page = DB.page;
    const c = document.getElementById("db-container");
    const q = document.getElementById("db-search").value.trim();
    const make = document.getElementById("db-make").value;
    const params = new URLSearchParams({ page, page_size: DB.pageSize });
    if (q) params.set("q", q);
    if (make) params.set("make", make);
    let data;
    try {
        data = await dbGet("/api/ai-database/search?" + params.toString());
    } catch (err) {
        showError(c, "Could not load the AI database: " + err.message);
        return;
    }
    DB.page = data.page;
    DB.total = data.total;
    renderStats();
    if (!data.items.length) {
        if (data.page > 1) { return loadList(1); }
        showEmpty(c, (q || make) ? "No pairs match this search." : "No pairs in the database yet.");
        renderPagination();
        return;
    }
    c.innerHTML = `<div class="ld-grid">${data.items.map(cardHTML).join("")}</div>`;
    c.querySelectorAll(".ld-card").forEach(card =>
        card.addEventListener("click", () => openDetail(card.dataset.id)));
    renderPagination();
}
function renderPagination() {
    const el = document.getElementById("db-pagination");
    const totalPages = Math.max(1, Math.ceil(DB.total / DB.pageSize));
    if (DB.total <= DB.pageSize) { el.style.display = "none"; el.innerHTML = ""; return; }
    el.style.display = "flex";
    el.innerHTML =
        `<button class="page-btn" id="db-prev" ${DB.page <= 1 ? "disabled" : ""}>Prev</button>
         <span class="page-info">Page ${DB.page} of ${totalPages} · ${DB.total} pairs</span>
         <button class="page-btn" id="db-next" ${DB.page >= totalPages ? "disabled" : ""}>Next</button>`;
    document.getElementById("db-prev").addEventListener("click", () => loadList(DB.page - 1));
    document.getElementById("db-next").addEventListener("click", () => loadList(DB.page + 1));
}
function cardHTML(e) {
    const model = e.model && e.model.toLowerCase() !== "unknown" ? e.model : "";
    const make = e.make && e.make.toLowerCase() !== "unknown" ? e.make : "—";
    return `
        <div class="ld-card" data-id="${dbEsc(e.id)}" title="${dbEsc(e.id)} · ${dbEsc(e.table_photo_id)} — click for the JSON record" style="cursor:pointer;">
            <div class="ld-card-img">
                <img src="${dbEsc(e.image_path)}" alt="${dbEsc(make)}" loading="lazy"
                     onerror="this.parentElement.classList.add('ld-img-missing')">
            </div>
            <div class="ld-card-body">
                <div class="ld-card-title"><b>${dbEsc(make)}</b> ${dbEsc(model)}${e.verified ? ' <span title="human-verified">✓</span>' : ""}</div>
                <div class="ld-card-meta">
                    <span class="ld-dot" title="color"></span>${dbEsc(e.color || "—")}
                    <span class="ld-card-conf">make ${dbPct(e.make_confidence)} · model ${dbPct(e.model_confidence)}</span>
                </div>
            </div>
        </div>`;
}

/* ---- Detail modal: the pair's full JSON record ------------------------ */
let dbDetailJSON = "";
async function openDetail(pairId) {
    const modal = document.getElementById("db-modal");
    const body = document.getElementById("db-modal-body");
    document.getElementById("db-modal-title").textContent = pairId;
    body.innerHTML = '<div class="loading-state">Loading…</div>';
    modal.classList.add("open");
    let rec;
    try {
        rec = await dbGet("/api/pairs/" + encodeURIComponent(pairId));
    } catch (err) {
        showError(body, "Could not load this pair: " + err.message);
        return;
    }
    dbDetailJSON = JSON.stringify(rec, null, 2);
    body.innerHTML = `
        <div style="display:flex; gap:18px; align-items:flex-start; flex-wrap:wrap;">
            <img src="${dbEsc(rec.image_path)}" alt="${dbEsc(rec.make || "pair")}"
                 style="width:220px; max-width:100%; border-radius:var(--radius-md); border:1px solid var(--border); background:var(--surface);"
                 onerror="this.style.display='none'">
            <pre style="flex:1 1 380px; min-width:0; margin:0; max-height:60vh; overflow:auto; padding:12px 14px; border:1px solid var(--border); border-radius:var(--radius-md); background:var(--surface); font-family:var(--font-mono); font-size:var(--text-sm); line-height:1.45; white-space:pre-wrap; word-break:break-word;">${dbEsc(dbDetailJSON)}</pre>
        </div>`;
}
function closeDetail() { document.getElementById("db-modal").classList.remove("open"); }
document.getElementById("db-close").addEventListener("click", closeDetail);
document.getElementById("db-modal").addEventListener("click", (e) => { if (e.target.id === "db-modal") closeDetail(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDetail(); });
document.getElementById("db-copy").addEventListener("click", async () => {
    const btn = document.getElementById("db-copy");
    try { await navigator.clipboard.writeText(dbDetailJSON); btn.textContent = "Copied ✓"; }
    catch (err) { btn.textContent = "Copy failed"; }
    setTimeout(() => { btn.textContent = "Copy JSON"; }, 1500);
});

/* ---- Init ------------------------------------------------------------ */
// Typing searches after a short pause (no request per keystroke); filter and
// refresh reset to page 1 because the result set changes underneath.
document.getElementById("db-search").addEventListener("input", () => {
    clearTimeout(DB.timer);
    DB.timer = setTimeout(() => loadList(1), 350);
});
document.getElementById("db-search").addEventListener("keydown", (e) => { if (e.key === "Enter") loadList(1); });
document.getElementById("db-make").addEventListener("change", () => loadList(1));
document.getElementById("db-refresh").addEventListener("click", () => { loadFacets(); loadList(1); });
loadFacets();
loadList(1);
