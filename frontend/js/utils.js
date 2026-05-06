/**
 * utils.js — Shared helpers used by all page scripts.
 *
 * Every function that generates HTML returns a string.
 * Functions that modify the DOM accept a container element.
 */

/* ----------------------------------------------------------------
   Date / number formatting
   ---------------------------------------------------------------- */

/** Format an ISO datetime string to "May 4, 2:32 PM". */
function formatDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return d.toLocaleString("en-US", {
        month: "short",
        day:   "numeric",
        hour:  "2-digit",
        minute: "2-digit",
    });
}

/** Format a confidence score (0.0–1.0) to "92.3%". */
function formatConfidence(score) {
    if (score == null) return "—";
    return (score * 100).toFixed(1) + "%";
}

/* ----------------------------------------------------------------
   Badge HTML generators
   ---------------------------------------------------------------- */

/** Returns HTML for an AI prediction badge (REUSE / RECYCLE / REVIEW). */
function predictionBadgeHTML(prediction) {
    if (!prediction) return '<span class="badge badge-null">—</span>';
    const cls = prediction.toLowerCase();   // "reuse" | "recycle" | "review"
    return `<span class="badge badge-${cls}">${prediction}</span>`;
}

/** Returns HTML for a validation status badge. */
function validationBadgeHTML(status, errors) {
    const classMap = {
        VALID:          "badge-valid",
        FAILED_CAPTURE: "badge-failed",
        PARTIAL:        "badge-partial",
    };
    const labelMap = {
        VALID:          "Valid",
        FAILED_CAPTURE: "Failed Capture",
        PARTIAL:        "Partial",
    };
    const cls   = classMap[status]  || "badge-muted";
    const label = labelMap[status]  || status;
    // `title` adds the raw error message as a hover tooltip on the badge
    const title = errors ? ` title="${errors}"` : "";
    return `<span class="badge ${cls}"${title}>${label}</span>`;
}

/** Returns HTML for a review status badge. */
function reviewStatusBadgeHTML(status) {
    const map = {
        NOT_REQUIRED: ["badge-muted",   "Not Required"],
        PENDING:      ["badge-review",  "Pending Review"],
        COMPLETED:    ["badge-reuse",   "Completed"],
    };
    const [cls, label] = map[status] || ["badge-muted", status];
    return `<span class="badge ${cls}">${label}</span>`;
}

/* ----------------------------------------------------------------
   Thumbnail strip — 5 small images side by side
   ---------------------------------------------------------------- */

const THUMB_VIEWS = [
    { key: "img_top",         label: "Top" },
    { key: "img_left",        label: "Left" },
    { key: "img_right",       label: "Right" },
    { key: "img_angle_left",  label: "A.Left" },
    { key: "img_angle_right", label: "A.Right" },
];

/**
 * Returns an HTML thumbnail strip for a shoe record.
 *
 * Three states:
 *   FAILED_CAPTURE — no images at all; show a red error banner
 *   PARTIAL        — some views captured; missing ones show a dash placeholder
 *   VALID (or other) — all images present; render thumbnails with hover zoom
 */
function thumbStripHTML(shoe) {
    // Failed capture — no usable images at all
    if (shoe.validation_status === "FAILED_CAPTURE") {
        return `<div class="thumb-failed">
            <span class="thumb-failed-icon"><svg class="icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg></span>
            Images unavailable&#160;&mdash;&#160;failed capture
        </div>`;
    }

    const thumbs = THUMB_VIEWS.map(v => {
        if (shoe[v.key]) {
            return `<img src="${shoe[v.key]}" class="thumb" alt="${v.label}" title="${v.label}" loading="lazy">`;
        }
        // Partial capture — some views are missing
        if (shoe.validation_status === "PARTIAL") {
            return `<div class="thumb-placeholder" title="${v.label} — missing view" style="font-style:italic">—</div>`;
        }
        return `<div class="thumb-placeholder" title="${v.label}">N/A</div>`;
    });
    return `<div class="thumb-strip">${thumbs.join("")}</div>`;
}

/* ----------------------------------------------------------------
   DOM helpers
   ---------------------------------------------------------------- */

/** Replace container content with a loading indicator. */
function showLoading(container) {
    container.innerHTML = '<div class="loading-state">Loading...</div>';
}

/** Replace container content with an error message. */
function showError(container, message) {
    container.innerHTML = `<div class="error-state">${message}</div>`;
}

/** Replace container content with an empty-state message. */
function showEmpty(container, message) {
    container.innerHTML = `<div class="empty-state">${message}</div>`;
}
