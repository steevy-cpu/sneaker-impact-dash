/**
 * shoe_detail.js — Shoe Detail page (Phase 2 + Phase 3 override form)
 *
 * URL param:  ?id=SHOE-YYYYMMDD-NNNN
 *
 * Renders:
 *   - 5 shoe images in a grid
 *   - AI result + validation status cards
 *   - Full metadata table
 *   - Human review override form
 */

const shoeId = new URLSearchParams(window.location.search).get("id");

/* ----------------------------------------------------------------
   Load + re-load
   ---------------------------------------------------------------- */

async function loadShoeDetail() {
    const container = document.getElementById("detail-root");

    if (!shoeId) {
        showError(container,
            'No shoe ID provided. <a href="live_feed.html" class="td-link"><svg class="icon icon-inline" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg> Back to Live Feed</a>');
        return;
    }

    showLoading(container);

    try {
        const shoe = await api.getShoe(shoeId);
        renderDetail(shoe, container);
        document.title = `${shoe.id} — Sneaker Impact`;
        setupOverrideForm(shoe);
    } catch (err) {
        showError(container,
            `Could not load shoe ${shoeId}: ${err.message}. ` +
            `<a href="live_feed.html" class="td-link"><svg class="icon icon-inline" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg> Back to Live Feed</a>`);
    }
}

/* ================================================================
   RENDER HELPERS
   ================================================================ */

// Fixed view names map directly to database columns (shoe.img_top etc.)
const IMAGE_VIEWS = [
    { key: "img_top",         label: "Top View" },
    { key: "img_left",        label: "Left View" },
    { key: "img_right",       label: "Right View" },
    { key: "img_angle_left",  label: "Angle Left" },
    { key: "img_angle_right", label: "Angle Right" },
];

function renderImages(shoe) {
    return IMAGE_VIEWS.map(v => {
        let content;
        if (shoe[v.key]) {
            content = `<img src="${shoe[v.key]}" alt="${v.label}" loading="lazy">`;
        } else if (shoe.validation_status === "PARTIAL") {
            content = `<div class="img-cell-missing-view">
                <span class="img-cell-missing-icon"><svg class="icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/></svg></span>
                Missing view
            </div>`;
        } else {
            content = `<div class="img-cell-placeholder">No image</div>`;
        }
        return `
            <div class="img-cell">
                ${content}
                <div class="img-cell-label">${v.label}</div>
            </div>`;
    }).join("");
}

function renderImageSection(shoe) {
    if (shoe.validation_status === "FAILED_CAPTURE") {
        const detail = shoe.validation_errors || "No capture data available";
        return `
            <div class="img-failed-state">
                <span class="img-failed-icon"><svg class="icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg></span>
                <div>
                    <div class="img-failed-title">Images Unavailable — Failed Capture</div>
                    <div class="img-failed-detail">${detail}</div>
                </div>
            </div>`;
    }
    return `<div class="image-grid">${renderImages(shoe)}</div>`;
}

function renderResultCards(shoe) {
    const predClass  = shoe.ai_prediction ? shoe.ai_prediction.toLowerCase() : "none";
    const validClass = { VALID:"valid", FAILED_CAPTURE:"failed", PARTIAL:"partial" }[shoe.validation_status] || "none";
    const validLabel = { VALID:"Valid", FAILED_CAPTURE:"Failed Capture", PARTIAL:"Partial" }[shoe.validation_status] || shoe.validation_status;

    return `
        <div class="result-row">
            <div class="result-card ${predClass}">
                <div class="result-card-label">AI Prediction</div>
                <div class="result-card-value">${shoe.ai_prediction || "—"}</div>
                <div class="result-card-sub">
                    Confidence: ${formatConfidence(shoe.ai_confidence)}
                    &nbsp;·&nbsp; Model: ${shoe.model_version || "—"}
                </div>
            </div>
            <div class="result-card ${validClass}">
                <div class="result-card-label">Validation Status</div>
                <div class="result-card-value" style="font-size:20px;">${validLabel}</div>
                <div class="result-card-sub">${shoe.validation_errors || "No capture errors"}</div>
            </div>
        </div>`;
}

function renderMetaTable(shoe) {
    const dash = '<span class="text-light">—</span>';
    const rows = [
        ["Final Decision",   predictionBadgeHTML(shoe.final_decision)],
        ["Review Status",    reviewStatusBadgeHTML(shoe.review_status)],
        ["Human Override",   shoe.human_override
            ? '<span class="badge badge-review">Yes</span>'
            : '<span class="badge badge-muted">No</span>'],
        ["Override Reason",  shoe.override_reason || dash],
        ["Brand",            shoe.shoe_brand || dash],
        ["Color",            shoe.shoe_color || dash],
        ["Operator ID",      `<span class="font-mono">${shoe.operator_id}</span>`],
        ["Batch ID",         `<span class="font-mono">${shoe.batch_id}</span>`],
        ["Timestamp",        `<span class="font-mono">${shoe.timestamp}</span>`],
        ["Model Version",    shoe.model_version ? `<span class="font-mono">${shoe.model_version}</span>` : dash],
        ["Notes",            shoe.notes || dash],
    ];
    const rowHTML = rows.map(([k, v]) =>
        `<tr><td class="meta-key">${k}</td><td class="meta-val">${v}</td></tr>`
    ).join("");

    return `
        <div class="card">
            <div class="card-header"><span class="card-title">Inspection Details</span></div>
            <table class="meta-table">${rowHTML}</table>
        </div>`;
}

/* ----------------------------------------------------------------
   Override form HTML
   ---------------------------------------------------------------- */

function renderOverrideForm(shoe) {
    const isCompleted = shoe.review_status === "COMPLETED";
    const statusBadge = reviewStatusBadgeHTML(shoe.review_status);

    // Context box: what the AI decided (or why there's no prediction)
    let contextContent;
    if (shoe.ai_prediction) {
        contextContent = `
            AI predicted ${predictionBadgeHTML(shoe.ai_prediction)}
            with <strong>${formatConfidence(shoe.ai_confidence)}</strong> confidence.
            ${isCompleted
                ? ' &nbsp;<span class="badge badge-reuse" style="margin-left:4px">Decision recorded</span>'
                : " Select your decision below."}`;
    } else {
        contextContent = `No AI prediction — capture ${shoe.validation_status.replace("_", " ").toLowerCase()}.
            You can still assign a manual decision.`;
    }

    const REASONS = [
        "Hole visible",
        "Sole separation",
        "Interior damage",
        "False AI prediction",
        "Worker judgment",
        "Other",
    ];
    const reasonOptions = REASONS.map(r =>
        `<option value="${r}"${shoe.override_reason === r ? " selected" : ""}>${r}</option>`
    ).join("");

    const currentDecision = shoe.final_decision || shoe.ai_prediction || "";

    return `
        <div class="override-card" id="override-card">
            <div class="override-card-header">
                <span class="override-card-title">Human Review</span>
                ${statusBadge}
            </div>
            <div class="override-card-body">

                <div class="ai-context-box${isCompleted ? " completed" : ""}">
                    ${contextContent}
                </div>

                <div class="form-group">
                    <label class="form-label">Your Decision</label>
                    <div class="decision-buttons">
                        <button class="decision-btn decision-reuse${currentDecision === "REUSE" ? " selected" : ""}"
                                data-value="REUSE">REUSE</button>
                        <button class="decision-btn decision-recycle${currentDecision === "RECYCLE" ? " selected" : ""}"
                                data-value="RECYCLE">RECYCLE</button>
                        <button class="decision-btn decision-review${currentDecision === "REVIEW" ? " selected" : ""}"
                                data-value="REVIEW">NEEDS REVIEW</button>
                    </div>
                </div>

                <div class="form-group" id="reason-group" style="display:none">
                    <label class="form-label">
                        Override Reason <span class="required-star">*</span>
                    </label>
                    <select class="form-select" id="override-reason">
                        <option value="">— Select a reason —</option>
                        ${reasonOptions}
                    </select>
                </div>

                <div class="form-group">
                    <label class="form-label">
                        Notes <span class="form-optional">(optional)</span>
                    </label>
                    <textarea class="form-textarea" id="override-notes"
                        placeholder="Any additional observations...">${shoe.notes || ""}</textarea>
                </div>

                <div class="form-actions">
                    <button class="submit-btn" id="submit-btn">
                        ${isCompleted ? "Update Decision" : "Submit Decision"}
                    </button>
                </div>

                <div class="form-feedback" id="form-feedback"></div>
            </div>
        </div>`;
}

/* ----------------------------------------------------------------
   Override form interactivity
   ---------------------------------------------------------------- */

function setupOverrideForm(shoe) {
    const buttons    = document.querySelectorAll(".decision-btn");
    const reasonGrp  = document.getElementById("reason-group");
    const submitBtn  = document.getElementById("submit-btn");
    const feedback   = document.getElementById("form-feedback");

    if (!submitBtn) return;  // form not present (no AI result and no form rendered)

    // Track which decision button is currently selected
    let selectedDecision = shoe.final_decision || shoe.ai_prediction || null;

    // The override reason field is only required when the human chooses a DIFFERENT
    // decision than the AI. If there is no AI prediction (failed capture), the reason
    // field is never shown because there is nothing to override.
    function syncReasonVisibility() {
        const needsReason = shoe.ai_prediction
            && selectedDecision
            && selectedDecision !== shoe.ai_prediction;
        if (reasonGrp) reasonGrp.style.display = needsReason ? "block" : "none";
    }
    syncReasonVisibility();

    // Button click: update selection state
    buttons.forEach(btn => {
        btn.addEventListener("click", () => {
            buttons.forEach(b => b.classList.remove("selected"));
            btn.classList.add("selected");
            selectedDecision = btn.dataset.value;
            syncReasonVisibility();
            feedback.innerHTML = "";  // clear any previous feedback
        });
    });

    // Submit
    submitBtn.addEventListener("click", async () => {
        feedback.innerHTML = "";

        if (!selectedDecision) {
            showFormError(feedback, "Please select a decision before submitting.");
            return;
        }

        const reason = document.getElementById("override-reason")?.value || "";
        const notes  = document.getElementById("override-notes")?.value  || "";

        // An override means the human chose differently from the AI.
        // If there is no AI prediction (failed capture), any decision is just a manual entry, not an override.
        const isOverride = shoe.ai_prediction && selectedDecision !== shoe.ai_prediction;
        if (isOverride && !reason) {
            showFormError(feedback, "Please select a reason — it is required when changing the AI decision.");
            return;
        }

        submitBtn.disabled    = true;
        submitBtn.textContent = "Saving...";

        try {
            await api.updateDecision(shoeId, {
                final_decision:  selectedDecision,
                override_reason: reason || null,
                notes:           notes  || null,
            });

            showFormSuccess(feedback,
                `Decision recorded: ${selectedDecision}${isOverride ? " (override)" : ""}`);

            // Pause 1.8 s before reloading so the success message is readable,
            // then reload the full record to reflect the new review_status and final_decision.
            setTimeout(loadShoeDetail, 1800);

        } catch (err) {
            submitBtn.disabled    = false;
            submitBtn.textContent = "Submit Decision";
            showFormError(feedback, err.message);
        }
    });
}

/* ----------------------------------------------------------------
   Inline form feedback helpers (scoped to override form)
   ---------------------------------------------------------------- */

function showFormSuccess(el, msg) {
    el.innerHTML = `<div class="form-success">&#10003; ${msg}</div>`;
}

function showFormError(el, msg) {
    el.innerHTML = `<div class="form-error">${msg}</div>`;
}

/* ----------------------------------------------------------------
   Full detail render (combines all sections)
   ---------------------------------------------------------------- */

function renderDetail(shoe, container) {
    container.innerHTML = `
        <a href="live_feed.html" class="back-link"><svg class="icon icon-inline" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg> Back to Live Feed</a>

        <div class="page-header">
            <h1 class="page-title">${shoe.id}</h1>
            <p class="page-subtitle">
                Batch: ${shoe.batch_id}
                &nbsp;·&nbsp; Operator: ${shoe.operator_id}
                &nbsp;·&nbsp; ${formatDate(shoe.timestamp)}
            </p>
        </div>

        ${renderImageSection(shoe)}

        ${renderResultCards(shoe)}

        ${renderMetaTable(shoe)}

        ${renderOverrideForm(shoe)}
    `;
}

/* ----------------------------------------------------------------
   Init
   ---------------------------------------------------------------- */

loadShoeDetail();
