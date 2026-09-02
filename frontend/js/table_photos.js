/**
 * table_photos.js — Table Photos page
 *
 * Lists every captured table photo with its box data + processing status, and a
 * detail modal showing the original photo, shipment match, and detected pairs.
 * Auto-refreshes while any photo is still pending/processing (visibility-aware).
 */

const TP = { pollTimer: null, pairs: [], lbIndex: 0 };

function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function statusBadge(status) {
    const map = {
        pending:    ["badge-review", "Pending"],
        processing: ["badge-review", "Processing…"],
        completed:  ["badge-reuse",  "Completed"],
        failed:     ["badge-failed", "Failed"],
    };
    const [cls, label] = map[status] || ["badge-muted", status || "—"];
    return `<span class="badge ${cls}">${label}</span>`;
}

function boxSummary(t) {
    const bits = [];
    if (t.total_good_sneakers) bits.push(`✅ ${t.total_good_sneakers}`);
    if (t.total_end_of_life)   bits.push(`💀 ${t.total_end_of_life}`);
    if (t.casuals)             bits.push(`👟 ${t.casuals}`);
    if (t.singles)             bits.push(`🥿 ${t.singles}`);
    if (t.weight_of_box)       bits.push(`⚖️ ${t.weight_of_box} lb`);
    return bits.join("  ") || "—";
}

// Operator note, trimmed to keep the row height stable. The full text is in the
// title tooltip and, unabridged, in the detail modal.
const NOTE_PREVIEW_CHARS = 48;

function notePreview(t) {
    const note = (t.notes || "").trim();
    if (!note) return `<span class="td-muted">—</span>`;
    const short = note.length > NOTE_PREVIEW_CHARS
        ? note.slice(0, NOTE_PREVIEW_CHARS).trimEnd() + "…"
        : note;
    return `<span class="tp-note" title="${esc(note)}">📝 ${esc(short)}</span>`;
}

/* ---- List ------------------------------------------------------------ */

async function loadList() {
    const c = document.getElementById("tp-container");
    let data;
    try {
        data = await api.getTablePhotos({ page_size: 100 });
    } catch (err) {
        showError(c, "Could not load table photos: " + err.message);
        return;
    }
    if (!data.items.length) {
        showEmpty(c, "No table photos captured yet. Use the Capture page to add one.");
        stopPolling();
        return;
    }
    const rows = data.items.map(t => `
        <tr class="tp-row" data-id="${esc(t.id)}">
            <td class="td-id"><span class="td-link">${esc(t.id)}</span></td>
            <td class="td-muted">${formatDate(t.created_at)}</td>
            <td class="font-mono text-sm">${esc(t.barcode || "—")}</td>
            <td class="text-sm">${boxSummary(t)}</td>
            <td>${statusBadge(t.status)}</td>
            <td class="text-sm">${t.status === "completed" ? t.num_pairs : "—"}</td>
            <td>${t.shipment_info && t.shipment_info.partner ? "📦 " + esc(t.shipment_info.partner) : "—"}</td>
            <td class="text-sm">${notePreview(t)}</td>
        </tr>`).join("");
    c.innerHTML = `<div class="table-wrap"><table>
        <thead><tr><th>ID</th><th>Time</th><th>Barcode</th><th>Box</th><th>Status</th><th>Pairs</th><th>Shipment</th><th>Note</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
    c.querySelectorAll(".tp-row").forEach(r => r.addEventListener("click", () => openDetail(r.dataset.id)));

    // Keep refreshing while anything is in-flight.
    const inFlight = data.items.some(t => t.status === "pending" || t.status === "processing");
    if (inFlight) startPolling(); else stopPolling();
}

function startPolling() {
    if (TP.pollTimer) return;
    TP.pollTimer = setInterval(() => { if (!document.hidden) loadList(); }, 3000);
}
function stopPolling() {
    if (TP.pollTimer) { clearInterval(TP.pollTimer); TP.pollTimer = null; }
}

/* ---- Detail modal ---------------------------------------------------- */

async function openDetail(id) {
    const modal = document.getElementById("tp-modal");
    const body = document.getElementById("tp-modal-body");
    document.getElementById("tp-modal-title").textContent = id;
    body.innerHTML = `<div class="loading-state">Loading…</div>`;
    modal.classList.add("open");
    document.getElementById("tp-reprocess").dataset.id = id;
    document.getElementById("tp-delete").dataset.id = id;
    document.getElementById("tp-reidentify").dataset.id = id;

    let t;
    try { t = await api.getTablePhoto(id); }
    catch (err) { body.innerHTML = `<div class="error-state">${esc(err.message)}</div>`; return; }

    const ship = t.shipment_info
        ? `<div class="tp-kv"><span>Shipment</span><b>${esc(t.shipment_info.partner || "matched")}${t.shipment_info.weight != null ? " · " + esc(t.shipment_info.weight) + " lb" : ""}</b></div>`
        : "";
    const err = t.error_message ? `<div class="form-error" style="display:block">${esc(t.error_message)}</div>` : "";
    // Show the thumbnail in the modal (the original can be several MB);
    // clicking it opens the full-resolution photo in a new tab.
    const photo = t.image_path
        ? `<a href="${esc(t.image_path)}" target="_blank" rel="noopener" style="display:block" title="Click to open full resolution">
               <img src="${esc(t.thumb_path || t.image_path)}" class="tp-photo" alt="table photo">
           </a>`
        : `<div class="tp-photo tp-photo--none">metadata only — no image</div>`;

    // Build the lightbox gallery from pairs that have a crop image, in order.
    TP.pairs = (t.pairs || []).filter(p => p.image_path);
    let lbIdx = 0;
    const pairs = (t.pairs || []).map(p => {
        const thumb = p.image_path
            ? `<img src="${esc(p.image_path)}" alt="pair crop" class="tp-pair-thumb" data-lb-index="${lbIdx++}" title="Click to view full size">`
            : `<div class="tp-pair-noimg">—</div>`;
        return `
        <div class="tp-pair">
            ${thumb}
            <div class="tp-pair-info">
                <div>${esc(p.detected_color || "—")} · <b>${esc(p.final_make || p.make || "—")}</b> / ${esc(p.final_model || p.model || "—")}</div>
                <div class="text-xs text-muted">${p.pair_score != null ? "🔗 " + Math.round(p.pair_score * 100) + "% match · " : "single · "}${reviewStatusBadgeHTML(p.review_status)}</div>
            </div>
            <button class="tp-pair-del btn-danger" type="button" data-pair-id="${esc(p.id)}"
                    title="Delete this detection (e.g. YOLO flagged something that isn't a shoe)"
                    style="margin-left:auto;flex:0 0 auto;align-self:center;padding:4px 10px;white-space:nowrap;">✕ Delete</button>
        </div>`;
    }).join("") || `<div class="empty-state">No pairs ${t.status === "completed" ? "detected" : "yet"}.</div>`;

    // Count from the actual records so singles are never lumped into "pairs"
    // (correct even for tables processed before the num_pairs fix).
    const nTruePairs = (t.pairs || []).filter(p => p.pair_score != null).length;
    const nSingles = (t.pairs || []).filter(p => p.pair_score == null).length;

    // Full note (the list only shows a preview). Rendered as its own block so a
    // long remark wraps instead of stretching the meta grid.
    const note = (t.notes || "").trim();
    const noteBlock = note
        ? `<div class="tp-note-block"><span class="tp-note-label">📝 Operator note</span>${esc(note)}</div>`
        : "";

    body.innerHTML = `
        ${photo}
        <div class="tp-meta">
            <div class="tp-kv"><span>Status</span><b>${statusBadge(t.status)}</b></div>
            <div class="tp-kv"><span>Barcode</span><b class="font-mono">${esc(t.barcode || "—")}</b></div>
            <div class="tp-kv"><span>Box</span><b>${boxSummary(t)}</b></div>
            <div class="tp-kv"><span>Pairs</span><b>${nTruePairs}${nSingles ? ` <span class="text-xs text-muted" style="font-weight:400;">+ ${nSingles} single${nSingles > 1 ? "s" : ""}</span>` : ""}</b></div>
            ${ship}
        </div>
        ${noteBlock}
        ${err}
        <h3 class="tp-pairs-title">Detected pairs ${TP.pairs.length ? `<span class="text-xs text-muted" style="font-weight:400;">— click a crop to view it full size</span>` : ""}</h3>
        <div class="tp-pairs">${pairs}</div>`;
}

/* ---- Pair-crop lightbox ---------------------------------------------- */

function renderLightbox() {
    const p = TP.pairs[TP.lbIndex];
    if (!p) return;
    document.getElementById("lb-img").src = p.image_path;
    const make  = p.final_make  || p.make  || "—";
    const model = p.final_model || p.model || "—";
    const match = p.pair_score != null
        ? `🔗 ${Math.round(p.pair_score * 100)}% pair match`
        : "single shoe";
    document.getElementById("lb-caption").innerHTML =
        `<div>${esc(p.detected_color || "—")} · <b>${esc(make)}</b> / ${esc(model)}</div>` +
        `<div class="lb-count">${esc(match)} · ${TP.lbIndex + 1} of ${TP.pairs.length}</div>`;
    // Hide nav arrows when there's only one crop.
    const multi = TP.pairs.length > 1;
    document.getElementById("lb-prev").style.display = multi ? "" : "none";
    document.getElementById("lb-next").style.display = multi ? "" : "none";
}

function openLightbox(index) {
    if (!TP.pairs.length) return;
    TP.lbIndex = index;
    renderLightbox();
    document.getElementById("lb-overlay").classList.add("open");
}

function closeLightbox() {
    document.getElementById("lb-overlay").classList.remove("open");
}

function stepLightbox(delta) {
    const n = TP.pairs.length;
    if (!n) return;
    TP.lbIndex = (TP.lbIndex + delta + n) % n;   // wrap around
    renderLightbox();
}

/* ---- Init ------------------------------------------------------------ */

document.getElementById("refresh-btn").addEventListener("click", loadList);
document.getElementById("tp-close").addEventListener("click", () => document.getElementById("tp-modal").classList.remove("open"));

// Lightbox: open on crop click (delegated, since pairs re-render each open), nav + keyboard.
document.getElementById("tp-modal-body").addEventListener("click", async (e) => {
    const thumb = e.target.closest(".tp-pair-thumb");
    if (thumb) { openLightbox(Number(thumb.dataset.lbIndex)); return; }

    // Delete one detected pair (e.g. a YOLO false-positive that isn't a shoe).
    const del = e.target.closest(".tp-pair-del");
    if (del) {
        const pid = del.dataset.pairId;
        if (!confirm(`Delete detection ${pid}?\n\nUse this when YOLO flagged something that isn't a shoe. It permanently removes the pair and its crop, decrements the pair count, and updates the Airtable brand summary. This cannot be undone.`)) return;
        del.disabled = true; del.textContent = "Deleting…";
        const photoId = document.getElementById("tp-delete").dataset.id;   // current modal's photo
        try {
            await api.deletePair(pid);
            showToast("Deleted " + pid, "info", 1800);
            await openDetail(photoId);   // re-fetch: pairs, count, and crop gallery rebuilt from server
            loadList();                  // refresh the list so its pair count stays in sync
        } catch (err) {
            showToast(err.message || "Delete failed", "error", 3000);
            del.disabled = false; del.textContent = "✕ Delete";
        }
    }
});
document.getElementById("lb-close").addEventListener("click", closeLightbox);
document.getElementById("lb-prev").addEventListener("click", () => stepLightbox(-1));
document.getElementById("lb-next").addEventListener("click", () => stepLightbox(1));
document.getElementById("lb-overlay").addEventListener("click", (e) => { if (e.target.id === "lb-overlay") closeLightbox(); });
document.addEventListener("keydown", (e) => {
    if (!document.getElementById("lb-overlay").classList.contains("open")) return;
    if (e.key === "Escape")     closeLightbox();
    else if (e.key === "ArrowLeft")  stepLightbox(-1);
    else if (e.key === "ArrowRight") stepLightbox(1);
});
document.getElementById("tp-modal").addEventListener("click", (e) => { if (e.target.id === "tp-modal") e.currentTarget.classList.remove("open"); });
document.getElementById("tp-reprocess").addEventListener("click", async (e) => {
    const id = e.target.dataset.id;
    e.target.disabled = true;
    try {
        await api.reprocessTablePhoto(id);
        showToast("Re-queued " + id, "info", 1800);
        document.getElementById("tp-modal").classList.remove("open");
        loadList();
    } catch (err) {
        showToast("Reprocess failed: " + err.message, "error", 2800);
    } finally { e.target.disabled = false; }
});
document.getElementById("tp-delete").addEventListener("click", async (e) => {
    const id = e.target.dataset.id;
    if (!confirm(`Delete table photo ${id}?\n\nThis permanently removes the photo, all its detected pairs, and its Airtable sync queue entry. This cannot be undone.`)) return;
    e.target.disabled = true;
    try {
        const res = await api.deleteTablePhoto(id);
        showToast(`Deleted ${id} (${res.pairs_removed} pair${res.pairs_removed === 1 ? "" : "s"})`, "info", 2200);
        document.getElementById("tp-modal").classList.remove("open");
        loadList();
    } catch (err) {
        showToast("Delete failed: " + err.message, "error", 2800);
    } finally { e.target.disabled = false; }
});
// ---- Re-identify (cloud backfill of unknown brand/model) ----------------
async function startReidentify(promise, btn, label) {
    btn.disabled = true; btn.textContent = "Starting…";
    try {
        const r = await promise;
        if (r.started) {
            showToast(`Re-identifying ${r.count} photo${r.count === 1 ? "" : "s"} via the cloud…`, "info", 3000);
            pollReidentify();
        } else {
            showToast(r.error || "Could not start", "error", 3000);
        }
    } catch (err) {
        showToast("Re-identify failed: " + err.message, "error", 3000);
    } finally { btn.disabled = false; btn.textContent = label; }
}

let reidTimer = null;
async function pollReidentify() {
    const el = document.getElementById("reid-status");
    try {
        const s = await api.reidentifyStatus();
        if (s.running) {
            el.textContent = `✨ re-identifying ${s.done}/${s.total} — ${s.pairs_fixed} pairs fixed`;
            if (!reidTimer) reidTimer = setInterval(pollReidentify, 4000);
        } else {
            if (reidTimer) { clearInterval(reidTimer); reidTimer = null; }
            if (s.finished_at && s.total) {
                el.textContent = `✅ done — ${s.pairs_fixed} pairs fixed across ${s.total} photo(s)`;
                loadList();
            } else { el.textContent = ""; }
        }
    } catch (e) { /* leave status as-is */ }
}

document.getElementById("reid-all-btn").addEventListener("click", (e) =>
    startReidentify(api.reidentifyAll(), e.currentTarget, "✨ Re-identify all unknowns"));
document.getElementById("tp-reidentify").addEventListener("click", (e) => {
    const id = e.currentTarget.dataset.id;
    document.getElementById("tp-modal").classList.remove("open");
    startReidentify(api.reidentifyPhoto(id), e.currentTarget, "✨ Re-identify unknowns");
});

document.addEventListener("visibilitychange", () => { if (!document.hidden) loadList(); });

loadList();
pollReidentify();   // reflect an in-progress run on load
