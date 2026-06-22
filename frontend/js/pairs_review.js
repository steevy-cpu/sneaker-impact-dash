/**
 * pairs_review.js — Pairs Review page
 *
 * Lists pairs with review_status = PENDING (the ones the AI wasn't confident
 * about; high-confidence pairs are auto-approved by the worker). For each, the
 * operator can correct the make/model and approve → PATCH /api/pairs/{id}/review.
 */

function esc(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function conf(c) { return c == null ? "—" : (c * 100).toFixed(0) + "%"; }

// Pagination + the TRUE pending total (so the banner never shows the
// visible-card count). pageSize stays 200 = same density as before; the grid
// is REPLACED each page, so the DOM never holds more than one page of cards.
const PR = { page: 1, pageSize: 200, total: 0 };

async function loadPage(page) {
    if (page == null) page = PR.page;
    const container = document.getElementById("pairs-container");
    const banner    = document.getElementById("queue-banner");
    showLoading(container);
    try {
        const data = await api.getPairs({
            review_status: "PENDING", page, page_size: PR.pageSize });
        // Empty page past the first (e.g. the tail got reviewed away) → snap to 1.
        if (!data.items.length && data.page > 1) { return loadPage(1); }
        PR.page  = data.page;
        PR.total = data.total;                 // real count of ALL pending pairs
        data.items.sort((a, b) => new Date(a.created_at) - new Date(b.created_at)); // FIFO within page
        renderBanner(banner, PR.total);
        renderCards(data.items, container);
        renderPagination();
        container.scrollIntoView({ block: "start" });
    } catch (err) {
        showError(container, "Could not load pairs: " + err.message);
        banner.innerHTML = "";
    }
}

function renderPagination() {
    const el = document.getElementById("pairs-pagination");
    const totalPages = Math.max(1, Math.ceil(PR.total / PR.pageSize));
    if (PR.total <= PR.pageSize) { el.style.display = "none"; el.innerHTML = ""; return; }
    el.style.display = "flex";
    el.innerHTML =
        `<button class="page-btn" id="pr-prev" ${PR.page <= 1 ? "disabled" : ""}>Prev</button>
         <span class="page-info">Page ${PR.page} of ${totalPages} · ${PR.total} pending</span>
         <button class="page-btn" id="pr-next" ${PR.page >= totalPages ? "disabled" : ""}>Next</button>`;
    document.getElementById("pr-prev").addEventListener("click", () => loadPage(PR.page - 1));
    document.getElementById("pr-next").addEventListener("click", () => loadPage(PR.page + 1));
}

function renderBanner(banner, count) {
    banner.className = "queue-banner" + (count === 0 ? " empty" : "");
    banner.innerHTML = count === 0
        ? `<div class="queue-count">0</div>
           <div class="queue-banner-text"><strong>All caught up!</strong><br>
           <span class="text-muted text-sm">No pairs are pending review.</span></div>`
        : `<div class="queue-count">${count}</div>
           <div class="queue-banner-text">
           <strong>${count === 1 ? "1 pair needs" : count + " pairs need"} review</strong><br>
           <span class="text-muted text-sm">Confirm the make &amp; model, or correct it, then approve.</span></div>`;
}

function cardHTML(p) {
    const img = p.image_path
        ? `<img src="${esc(p.image_path)}" class="pair-card-img" alt="pair crop" loading="lazy">`
        : `<div class="pair-card-img pair-card-img--none">no image</div>`;
    const sources = (p.model_sources && p.model_sources.length)
        ? `<div class="pair-meta-row"><span class="pair-meta-label">Source</span>
             <span class="pair-meta-val text-sm">${esc(p.model_sources[0])}</span></div>`
        : "";
    return `
    <div class="pair-card" data-id="${esc(p.id)}">
        ${img}
        <div class="pair-card-body">
            <div class="pair-card-head">
                <span class="pair-card-id">${esc(p.id)}</span>
                ${p.prediction_source && p.prediction_source.startsWith("cloud")
                    ? `<span class="badge badge-reuse" title="${esc(p.prediction_source)}">☁️ Cloud AI</span>`
                    : `<span class="badge badge-review">Pending</span>`}
            </div>
            <div class="pair-meta-row"><span class="pair-meta-label">From</span>
                <span class="pair-meta-val text-sm">${esc(p.table_photo_id)}</span></div>
            <div class="pair-meta-row"><span class="pair-meta-label">Pair match</span>
                <span class="pair-meta-val">${p.pair_score != null
                    ? "🔗 " + conf(p.pair_score) + " visual similarity"
                    : "<span class='text-muted'>single shoe (unmatched)</span>"}</span></div>
            <div class="pair-meta-row"><span class="pair-meta-label">Color</span>
                <span class="pair-meta-val">${esc(p.detected_color || "—")}
                    <span class="text-muted text-xs">(${conf(p.color_confidence)})</span></span></div>
            <div class="pair-meta-row"><span class="pair-meta-label">AI make</span>
                <span class="pair-meta-val">${esc(p.make || "—")}
                    <span class="text-muted text-xs">(${conf(p.make_confidence)})</span></span></div>
            <div class="pair-meta-row"><span class="pair-meta-label">AI model</span>
                <span class="pair-meta-val">${esc(p.model || "—")}
                    <span class="text-muted text-xs">(${conf(p.model_confidence)})</span></span></div>
            ${sources}
            <div class="pair-edit">
                <label class="form-label">Final make</label>
                <input class="form-input pair-make" type="text" value="${esc(p.make || "")}" placeholder="brand">
                <label class="form-label">Final model</label>
                <input class="form-input pair-model" type="text" value="${esc(p.model || "")}" placeholder="model / silhouette">
            </div>
            <div class="pair-actions">
                <button class="btn-danger pair-delete" type="button">Delete</button>
                <button class="btn-secondary pair-skip" type="button">Skip</button>
                <button class="btn-success pair-approve" type="button">✅ Approve &amp; Save</button>
            </div>
        </div>
    </div>`;
}

function renderCards(items, container) {
    if (!items.length) {
        showEmpty(container, "No pairs are currently pending review.");
        return;
    }
    container.className = "pairs-grid";
    container.innerHTML = items.map(cardHTML).join("");
    container.querySelectorAll(".pair-card").forEach(wireCard);
}

function wireCard(card) {
    const id      = card.dataset.id;
    const approve = card.querySelector(".pair-approve");
    const skip    = card.querySelector(".pair-skip");
    const del     = card.querySelector(".pair-delete");

    approve.addEventListener("click", async () => {
        const final_make  = card.querySelector(".pair-make").value.trim();
        const final_model = card.querySelector(".pair-model").value.trim();
        approve.disabled = true; approve.textContent = "Saving…";
        try {
            await api.reviewPair(id, {
                final_make:  final_make || null,
                final_model: final_model || null,
                review_status: "COMPLETED",
            });
            removeCard(card, { leftPending: true });   // approved → no longer pending
            showToast("✅ Saved — " + id, "success", 1800);
        } catch (err) {
            showToast(err.message || "Save failed", "error", 3000);
            approve.disabled = false; approve.textContent = "✅ Approve & Save";
        }
    });

    // "Skip" just hides it this session (leaves it PENDING in the DB → total unchanged).
    skip.addEventListener("click", () => removeCard(card, { leftPending: false }));

    // "Delete" permanently removes the pair (DB row + crop file).
    del.addEventListener("click", async () => {
        if (!confirm(`Delete pair ${id}?\n\nThis permanently removes the pair and its crop. This cannot be undone.`)) return;
        del.disabled = true; del.textContent = "Deleting…";
        try {
            await api.deletePair(id);
            removeCard(card, { leftPending: true });    // removed from DB → no longer pending
            showToast("Deleted " + id, "info", 1800);
        } catch (err) {
            showToast(err.message || "Delete failed", "error", 3000);
            del.disabled = false; del.textContent = "Delete";
        }
    });
}

// opts.leftPending: true when the pair LEFT the pending set (approved/deleted)
// → decrement the real total; skip keeps it pending so the total is unchanged.
function removeCard(card, opts) {
    opts = opts || {};
    card.remove();
    if (opts.leftPending) PR.total = Math.max(0, PR.total - 1);
    renderBanner(document.getElementById("queue-banner"), PR.total);
    renderPagination();
    if (document.querySelectorAll(".pair-card").length === 0) {
        const container = document.getElementById("pairs-container");
        container.className = "";
        if (PR.total === 0) {
            showEmpty(container, "No pairs are currently pending review.");
        } else if (opts.leftPending) {
            loadPage(PR.page);   // batch reviewed → pull the next page of pending work
        } else {
            showEmpty(container, "All pairs on this page were skipped. Use Prev / Next to keep going.");
        }
    }
}

loadPage(1);
