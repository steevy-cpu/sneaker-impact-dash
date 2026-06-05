/**
 * airtable_sync.js — Airtable Sync (outbox) page
 *
 * Shows the durable send-when-available queue: box data + brand summaries that
 * are saved locally and retried until their shipment exists in Airtable. Lets
 * the operator flush (retry all pending) on demand.
 */

function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function statusBadge(s) {
    if (s === "synced") return `<span class="badge badge-reuse">Synced</span>`;
    return `<span class="badge badge-review">Pending</span>`;
}

function reasonText(it) {
    if (it.status === "synced") return it.airtable_record_id ? esc(it.airtable_record_id) : "—";
    if (it.last_error === "no_row") return "waiting — shipment not in Airtable yet";
    if (it.last_error) return esc(it.last_error);
    return "not tried yet";
}

async function load() {
    const c = document.getElementById("as-container");
    const statusEl = document.getElementById("as-status");
    const filter = document.getElementById("as-filter").value;
    let d;
    try {
        d = await api.getAirtableOutbox(filter ? { status: filter } : {});
    } catch (err) {
        showError(c, "Could not load the outbox: " + err.message);
        return;
    }

    const pend = d.counts.pending || 0, sync = d.counts.synced || 0;
    const writeChip = d.sync_enabled
        ? `<span class="ld-chip ld-chip--total">writes ON</span>`
        : `<span class="ld-chip" style="background:var(--danger-soft,#fde8e8);color:var(--danger,#c0392b)">writes OFF</span>`;
    statusEl.innerHTML = `${writeChip}
        <span class="ld-chip">⏳ ${pend} pending</span>
        <span class="ld-chip">✅ ${sync} synced</span>`;

    if (!d.items.length) {
        showEmpty(c, filter ? `No ${filter} items.` :
            "Nothing queued yet. Captures with a barcode are saved here and sent to Airtable when their shipment exists.");
        return;
    }
    const rows = d.items.map(it => `
        <tr>
            <td class="td-id"><span class="td-link">${esc(it.table_photo_id)}</span></td>
            <td class="font-mono text-sm">${esc(it.match_barcode)}</td>
            <td class="text-sm">${[
                it.good != null ? "✅" + it.good : "",
                it.eol != null ? "💀" + it.eol : "",
                it.casuals ? "👟" + it.casuals : "",
                it.weight != null ? "⚖️" + it.weight : "",
            ].filter(Boolean).join("  ") || "—"}</td>
            <td class="text-sm">${esc(it.brand_summary || "—")}</td>
            <td>${statusBadge(it.status)}</td>
            <td class="text-sm text-muted">${reasonText(it)}</td>
            <td class="text-sm text-muted">${it.attempts || 0}</td>
        </tr>`).join("");
    c.innerHTML = `<div class="table-wrap"><table>
        <thead><tr><th>Capture</th><th>Barcode</th><th>Box</th><th>Brands</th><th>Status</th><th>Detail</th><th>Tries</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
}

document.getElementById("refresh-btn").addEventListener("click", load);
document.getElementById("as-filter").addEventListener("change", load);
document.getElementById("as-flush").addEventListener("click", async (e) => {
    e.target.disabled = true;
    const old = e.target.textContent;
    e.target.textContent = "Flushing…";
    try {
        const r = await api.flushAirtableOutbox();
        if (r.flushed) {
            showToast(`Flushed: ${r.synced} synced, ${r.pending} still pending`,
                      r.synced ? "success" : "info", 3000);
        } else {
            showToast(r.note || "Writes are disabled", "error", 3000);
        }
        load();
    } catch (err) {
        showToast("Flush failed: " + err.message, "error", 3000);
    } finally {
        e.target.disabled = false;
        e.target.textContent = old;
    }
});

load();
