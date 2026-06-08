/**
 * table_photos.js — Table Photos page
 *
 * Lists every captured table photo with its box data + processing status, and a
 * detail modal showing the original photo, shipment match, and detected pairs.
 * Auto-refreshes while any photo is still pending/processing (visibility-aware).
 */

const TP = { pollTimer: null };

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
    if (t.weight_of_box)       bits.push(`⚖️ ${t.weight_of_box} lb`);
    return bits.join("  ") || "—";
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
        </tr>`).join("");
    c.innerHTML = `<div class="table-wrap"><table>
        <thead><tr><th>ID</th><th>Time</th><th>Barcode</th><th>Box</th><th>Status</th><th>Pairs</th><th>Shipment</th></tr></thead>
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

    let t;
    try { t = await api.getTablePhoto(id); }
    catch (err) { body.innerHTML = `<div class="error-state">${esc(err.message)}</div>`; return; }

    const ship = t.shipment_info
        ? `<div class="tp-kv"><span>Shipment</span><b>${esc(t.shipment_info.partner || "matched")}${t.shipment_info.weight != null ? " · " + esc(t.shipment_info.weight) + " lb" : ""}</b></div>`
        : "";
    const err = t.error_message ? `<div class="form-error" style="display:block">${esc(t.error_message)}</div>` : "";
    const photo = t.image_path
        ? `<img src="${esc(t.image_path)}" class="tp-photo" alt="table photo">`
        : `<div class="tp-photo tp-photo--none">metadata only — no image</div>`;

    const pairs = (t.pairs || []).map(p => `
        <div class="tp-pair">
            ${p.image_path ? `<img src="${esc(p.image_path)}" alt="pair">` : `<div class="tp-pair-noimg">—</div>`}
            <div class="tp-pair-info">
                <div>${esc(p.detected_color || "—")} · <b>${esc(p.final_make || p.make || "—")}</b> / ${esc(p.final_model || p.model || "—")}</div>
                <div class="text-xs text-muted">${reviewStatusBadgeHTML(p.review_status)}</div>
            </div>
        </div>`).join("") || `<div class="empty-state">No pairs ${t.status === "completed" ? "detected" : "yet"}.</div>`;

    body.innerHTML = `
        ${photo}
        <div class="tp-meta">
            <div class="tp-kv"><span>Status</span><b>${statusBadge(t.status)}</b></div>
            <div class="tp-kv"><span>Barcode</span><b class="font-mono">${esc(t.barcode || "—")}</b></div>
            <div class="tp-kv"><span>Box</span><b>${boxSummary(t)}</b></div>
            <div class="tp-kv"><span>Pairs</span><b>${t.num_pairs}</b></div>
            ${ship}
        </div>
        ${err}
        <h3 class="tp-pairs-title">Detected pairs</h3>
        <div class="tp-pairs">${pairs}</div>`;
}

/* ---- Init ------------------------------------------------------------ */

document.getElementById("refresh-btn").addEventListener("click", loadList);
document.getElementById("tp-close").addEventListener("click", () => document.getElementById("tp-modal").classList.remove("open"));
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
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadList(); });

loadList();
