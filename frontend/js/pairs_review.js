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

async function loadPending() {
    const container = document.getElementById("pairs-container");
    const banner    = document.getElementById("queue-banner");
    showLoading(container);
    try {
        const data = await api.getPairs({ review_status: "PENDING", page_size: 200 });
        data.items.sort((a, b) => new Date(a.created_at) - new Date(b.created_at)); // FIFO
        renderBanner(banner, data.total);
        renderCards(data.items, container);
    } catch (err) {
        showError(container, "Could not load pairs: " + err.message);
        banner.innerHTML = "";
    }
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
                <span class="badge badge-review">Pending</span>
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
            removeCard(card);
            showToast("✅ Saved — " + id, "success", 1800);
        } catch (err) {
            showToast(err.message || "Save failed", "error", 3000);
            approve.disabled = false; approve.textContent = "✅ Approve & Save";
        }
    });

    // "Skip" just hides it this session (leaves it PENDING in the DB).
    skip.addEventListener("click", () => removeCard(card));

    // "Delete" permanently removes the pair (DB row + crop file).
    del.addEventListener("click", async () => {
        if (!confirm(`Delete pair ${id}?\n\nThis permanently removes the pair and its crop. This cannot be undone.`)) return;
        del.disabled = true; del.textContent = "Deleting…";
        try {
            await api.deletePair(id);
            removeCard(card);
            showToast("Deleted " + id, "info", 1800);
        } catch (err) {
            showToast(err.message || "Delete failed", "error", 3000);
            del.disabled = false; del.textContent = "Delete";
        }
    });
}

function removeCard(card) {
    card.remove();
    const remaining = document.querySelectorAll(".pair-card").length;
    const banner = document.getElementById("queue-banner");
    renderBanner(banner, remaining);
    if (remaining === 0) {
        document.getElementById("pairs-container").className = "";
        showEmpty(document.getElementById("pairs-container"), "No pairs are currently pending review.");
    }
}

loadPending();
