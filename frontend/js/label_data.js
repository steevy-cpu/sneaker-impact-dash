/**
 * label_data.js — Label Data page
 *
 * Browses the curated training set (LABEL_DATA_DIR): every auto-approved pair
 * crop with its color/make/model + confidence + source photo. Gallery grid with
 * make/color filters + a stats bar; click a card for the full metadata.
 */

const LD = { stats: null };

function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function pct(v) {
    return typeof v === "number" ? Math.round(v * 100) + "%" : "—";
}

/* ---- Stats + filters ------------------------------------------------- */

function renderStats(stats) {
    const el = document.getElementById("ld-stats");
    if (!stats || !stats.total) { el.innerHTML = ""; return; }
    const makes = Object.entries(stats.by_make)
        .map(([m, n]) => `<span class="ld-chip">${esc(m)} <b>${n}</b></span>`).join("");
    el.innerHTML = `<span class="ld-chip ld-chip--total">${stats.total} total</span>${makes}`;
}

function fillFilter(sel, counts, label) {
    const cur = sel.value;
    const opts = [`<option value="">${label}</option>`].concat(
        Object.entries(counts).map(([k, n]) => `<option value="${esc(k)}">${esc(k)} (${n})</option>`));
    sel.innerHTML = opts.join("");
    sel.value = cur;   // preserve selection across refreshes
}

/* ---- List ------------------------------------------------------------ */

async function loadList() {
    const c = document.getElementById("ld-container");
    const make = document.getElementById("ld-make").value;
    const color = document.getElementById("ld-color").value;
    let data;
    try {
        data = await api.getLabelData({ make, color, page_size: 200 });
    } catch (err) {
        showError(c, "Could not load label data: " + err.message);
        return;
    }
    // Stats + filter options reflect the FULL set (returned regardless of filter).
    LD.stats = data.stats;
    renderStats(data.stats);
    fillFilter(document.getElementById("ld-make"), data.stats.by_make, "All makes");
    fillFilter(document.getElementById("ld-color"), data.stats.by_color, "All colors");

    if (!data.items.length) {
        const why = (make || color)
            ? "No entries match this filter."
            : "No label data yet. Pairs land here automatically once they're auto-approved (high make + model confidence) or you confirm them in Pairs Review.";
        showEmpty(c, why);
        return;
    }
    c.innerHTML = `<div class="ld-grid">${data.items.map(cardHTML).join("")}</div>`;
    c.querySelectorAll(".ld-card").forEach(card =>
        card.addEventListener("click", () => openDetail(card.dataset.idx, data.items)));
}

function cardHTML(e, idx) {
    return `
        <div class="ld-card" data-idx="${idx}">
            <div class="ld-card-img">
                <img src="${esc(e.image_url)}" alt="${esc(e.make || "pair")}" loading="lazy"
                     onerror="this.parentElement.classList.add('ld-img-missing')">
            </div>
            <div class="ld-card-body">
                <div class="ld-card-title"><b>${esc(e.make || "—")}</b> ${esc(e.model || "")}</div>
                <div class="ld-card-meta">
                    <span class="ld-dot" title="color"></span>${esc(e.detected_color || "—")}
                    <span class="ld-card-conf">make ${pct(e.make_confidence)} · model ${pct(e.model_confidence)}</span>
                </div>
            </div>
        </div>`;
}

/* ---- Detail modal ---------------------------------------------------- */

function openDetail(idx, items) {
    const e = items[idx];
    if (!e) return;
    document.getElementById("ld-modal-title").textContent = e.filename;
    document.getElementById("ld-delete").dataset.filename = e.filename;
    document.getElementById("ld-modal-body").innerHTML = `
        <img src="${esc(e.image_url)}" class="ld-detail-img" alt="${esc(e.make || "pair")}">
        <div class="tp-meta">
            <div class="tp-kv"><span>Make</span><b>${esc(e.make || "—")} <span class="text-muted">(${pct(e.make_confidence)})</span></b></div>
            <div class="tp-kv"><span>Model</span><b>${esc(e.model || "—")} <span class="text-muted">(${pct(e.model_confidence)})</span></b></div>
            <div class="tp-kv"><span>Color</span><b>${esc(e.detected_color || "—")}</b></div>
            <div class="tp-kv"><span>Source photo</span><b class="font-mono">${esc(e.source_photo || "—")}</b></div>
            <div class="tp-kv"><span>Source pair</span><b class="font-mono">${esc(e.source_pair || "—")}</b></div>
            <div class="tp-kv"><span>Exported</span><b>${e.timestamp ? esc(formatDate(e.timestamp)) : "—"} · ${esc(e.exported_by || "—")}</b></div>
        </div>`;
    document.getElementById("ld-modal").classList.add("open");
}

/* ---- Init ------------------------------------------------------------ */

document.getElementById("refresh-btn").addEventListener("click", loadList);
document.getElementById("ld-make").addEventListener("change", loadList);
document.getElementById("ld-color").addEventListener("change", loadList);
document.getElementById("ld-close").addEventListener("click", () => document.getElementById("ld-modal").classList.remove("open"));
document.getElementById("ld-modal").addEventListener("click", (e) => { if (e.target.id === "ld-modal") e.currentTarget.classList.remove("open"); });

document.getElementById("ld-delete").addEventListener("click", async (e) => {
    const fn = e.target.dataset.filename;
    if (!fn) return;
    if (!confirm(`Delete ${fn} from Label Data?\n\nThis removes the crop and its label permanently.`)) return;
    e.target.disabled = true;
    try {
        await api.deleteLabelData(fn);
        showToast("Deleted " + fn, "info", 1800);
        document.getElementById("ld-modal").classList.remove("open");
        loadList();
    } catch (err) {
        showToast("Delete failed: " + err.message, "error", 2800);
    } finally {
        e.target.disabled = false;
    }
});

loadList();
