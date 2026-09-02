/**
 * airtable_sync.js — Airtable Sync (outbox) page
 *
 * Shows the durable send-when-available queue: box data + brand summaries that
 * are saved locally and retried until their shipment exists in Airtable. Lets
 * the operator flush (retry all pending) on demand.
 */

let FEDEX_ON = false;

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
    FEDEX_ON = !!d.fedex_enabled;
    const writeChip = d.sync_enabled
        ? `<span class="ld-chip ld-chip--total">writes ON</span>`
        : `<span class="ld-chip" style="background:var(--danger-soft,#fde8e8);color:var(--danger,#c0392b)">writes OFF</span>`;
    const fedexChip = FEDEX_ON ? `<span class="ld-chip">📦 FedEx lookup ON</span>` : "";
    statusEl.innerHTML = `${writeChip}${fedexChip}
        <span class="ld-chip">⏳ ${pend} pending</span>
        <span class="ld-chip">✅ ${sync} synced</span>`;

    if (!d.items.length) {
        showEmpty(c, filter ? `No ${filter} items.` :
            "Nothing queued yet. Captures with a barcode are saved here and sent to Airtable when their shipment exists.");
        return;
    }
    // Offer a FedEx lookup only where it helps: enabled + shipment-not-in-Airtable.
    const canLookup = (it) => FEDEX_ON && it.status !== "synced" && it.last_error === "no_row";
    const rows = d.items.map(it => `
        <tr>
            <td class="td-id"><span class="td-link">${esc(it.table_photo_id)}</span></td>
            <td class="font-mono text-sm">${esc(it.match_barcode)}</td>
            <td class="text-sm">${[
                it.good != null ? "✅" + it.good : "",
                it.eol != null ? "💀" + it.eol : "",
                it.casuals ? "👟" + it.casuals : "",
                it.singles ? "🥿" + it.singles : "",
                it.weight != null ? "⚖️" + it.weight : "",
            ].filter(Boolean).join("  ") || "—"}</td>
            <td class="text-sm">${esc(it.brand_summary || "—")}</td>
            <td>${statusBadge(it.status)}</td>
            <td class="text-sm text-muted">${reasonText(it)}</td>
            <td class="text-sm">${canLookup(it)
                ? `<button class="btn-secondary fedex-btn" data-tp="${esc(it.table_photo_id)}" style="padding:3px 9px;">📦 Check FedEx</button>
                   <span class="fedex-out text-xs text-muted" data-out="${esc(it.table_photo_id)}"></span>`
                : ""}
                ${it.status !== "synced"
                ? `<button class="btn-secondary del-btn" data-tp="${esc(it.table_photo_id)}" title="Give up on this row — remove it from the queue (the capture itself is kept)" style="padding:3px 9px;">🗑</button>`
                : ""}</td>
            <td class="text-sm text-muted">${it.attempts || 0}</td>
        </tr>`).join("");
    c.innerHTML = `<div class="table-wrap"><table>
        <thead><tr><th>Capture</th><th>Barcode</th><th>Box</th><th>Brands</th><th>Status</th><th>Detail</th><th>FedEx</th><th>Tries</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;

    c.querySelectorAll(".fedex-btn").forEach(btn => btn.addEventListener("click", onFedexLookup));
    c.querySelectorAll(".del-btn").forEach(btn => btn.addEventListener("click", onDelete));
}

async function onDelete(e) {
    const btn = e.currentTarget;
    const tp = btn.dataset.tp;
    if (!confirm(`Remove ${tp} from the sync queue?\n\nIts box data will never be sent to Airtable. The capture and its pairs are kept.`))
        return;
    btn.disabled = true;
    try {
        await api.deleteAirtableOutbox(tp);
        showToast(`${tp} removed from the queue`, "success", 2500);
        load();
    } catch (err) {
        showToast("Delete failed: " + err.message, "error", 3000);
        btn.disabled = false;
    }
}

async function onFedexLookup(e) {
    const btn = e.currentTarget;
    const tp = btn.dataset.tp;
    const out = document.querySelector(`.fedex-out[data-out="${CSS.escape(tp)}"]`);
    btn.disabled = true; const label = btn.textContent; btn.textContent = "Checking…";
    try {
        const r = await api.fedexLookup(tp);
        if (r && r.found) {
            const bits = [
                r.delivered ? "✅ delivered" : (r.status || "in transit"),
                r.delivery_date ? new Date(r.delivery_date).toLocaleDateString() : "",
                r.weight || "",
                r.shipper ? "from " + r.shipper : "",
            ].filter(Boolean).join(" · ");
            out.innerHTML = ` <span style="color:var(--success,#27ae60)">${esc(bits)} — real shipment, not in Airtable yet</span>`;
        } else {
            out.innerHTML = ` <span style="color:var(--danger,#c0392b)">⚠️ ${esc((r && r.error) || "not found")}</span>`;
        }
    } catch (err) {
        out.innerHTML = ` <span style="color:var(--danger,#c0392b)">lookup failed: ${esc(err.message)}</span>`;
    } finally {
        btn.disabled = false; btn.textContent = label;
    }
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
