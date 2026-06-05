/**
 * api.js — Shared API client
 *
 * All fetch calls go through apiFetch(), which handles errors uniformly.
 * Named functions in the `api` object make call sites readable.
 *
 * Since the frontend is served from the same origin as the API server,
 * all paths are relative to the server root (e.g. /api/shoes).
 */

async function apiFetch(path, options = {}) {
    const res = await fetch(path, {
        headers: { "Content-Type": "application/json", ...options.headers },
        ...options,
    });

    if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        // FastAPI validation errors return detail as an array of objects.
        // All other errors return detail as a plain string.
        let message;
        if (Array.isArray(body.detail)) {
            message = body.detail.map(e => e.msg || JSON.stringify(e)).join("; ");
        } else {
            message = body.detail || `Server error ${res.status}`;
        }
        throw new Error(message);
    }

    return res.json();
}

/**
 * Multipart upload (FormData) — does NOT set Content-Type so the browser adds
 * the correct multipart boundary. Same error shaping as apiFetch().
 */
async function apiUpload(path, formData) {
    const res = await fetch(path, { method: "POST", body: formData });
    if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        let message;
        if (Array.isArray(body.detail)) {
            message = body.detail.map(e => e.msg || JSON.stringify(e)).join("; ");
        } else {
            message = body.detail || `Server error ${res.status}`;
        }
        throw new Error(message);
    }
    return res.json();
}

const api = {
    /**
     * List shoes with optional filters and pagination.
     *
     * @param {Object} params - Optional query params:
     *   page, page_size, batch_id, ai_prediction,
     *   validation_status, review_status, human_override
     */
    getShoes(params = {}) {
        const qs = new URLSearchParams(params).toString();
        return apiFetch(`/api/shoes${qs ? "?" + qs : ""}`);
    },

    /** Get a single shoe record by ID. */
    getShoe(id) {
        return apiFetch(`/api/shoes/${encodeURIComponent(id)}`);
    },

    /** Create a new shoe inspection record. */
    createShoe(data) {
        return apiFetch("/api/shoes", { method: "POST", body: JSON.stringify(data) });
    },

    /** List all batches. */
    getBatches() {
        return apiFetch("/api/batches");
    },

    /** Get a single batch by ID. */
    getBatch(id) {
        return apiFetch(`/api/batches/${encodeURIComponent(id)}`);
    },

    /** Create a new batch. */
    createBatch(data) {
        return apiFetch("/api/batches", { method: "POST", body: JSON.stringify(data) });
    },

    /**
     * Submit a human review decision for a shoe.
     *
     * @param {string} shoeId
     * @param {{ final_decision: string, override_reason?: string, notes?: string }} data
     */
    updateDecision(shoeId, data) {
        return apiFetch(`/api/shoes/${encodeURIComponent(shoeId)}/decision`, {
            method: "PATCH",
            body:   JSON.stringify(data),
        });
    },

    /** System health and stats. */
    getHealth() {
        return apiFetch("/api/health");
    },

    /**
     * Daily summary stats for the dashboard home.
     *
     * @param {string} [date] - YYYY-MM-DD. Defaults to today on the server.
     */
    getDailySummary(date) {
        const qs = date ? `?date=${date}` : "";
        return apiFetch(`/api/analytics/daily-summary${qs}`);
    },

    /**
     * Per-day trend data for analytics charts.
     *
     * @param {number} [days=7] - How many days back to fetch (including today).
     */
    getTrends(days = 7) {
        return apiFetch(`/api/analytics/trends?days=${days}`);
    },

    /** Operational alerts derived from today's data. */
    getAlerts() {
        return apiFetch("/api/analytics/alerts");
    },

    /**
     * Generate fake shoe inspections (simulation mode only).
     *
     * @param {number} count       - How many to create (1–50).
     * @param {string} condition   - Batch condition profile key.
     */
    generateInspections(count, condition) {
        return apiFetch("/api/simulation/generate", {
            method: "POST",
            body:   JSON.stringify({ count, condition }),
        });
    },

    /** Wipe all simulation data and re-seed (simulation mode only). */
    resetSimulation() {
        return apiFetch("/api/simulation/reset", { method: "POST" });
    },

    /** Recent inspection events as human-readable messages (activity feed). */
    getActivity() {
        return apiFetch("/api/analytics/activity");
    },

    /** Per-brand inspection counts and review rates (analytics page). */
    getBrandDistribution() {
        return apiFetch("/api/analytics/brand-distribution");
    },

    /* ---- Table-photo capture flow (new operator workflow) ---------------- */

    /**
     * Upload one whole-table photo + box metadata (multipart).
     * @param {FormData} formData - image + barcode + box counts + operator_id
     */
    captureTablePhoto(formData) {
        return apiUpload("/api/capture", formData);
    },

    /** Fast-track box metadata without an image (mirrors ShoeSort /api/metadata). */
    createMetadata(data) {
        return apiFetch("/api/metadata", { method: "POST", body: JSON.stringify(data) });
    },

    /** List table photos (optional filters: page, page_size, status, batch_id). */
    getTablePhotos(params = {}) {
        const qs = new URLSearchParams(params).toString();
        return apiFetch(`/api/table-photos${qs ? "?" + qs : ""}`);
    },

    /** One table photo + its pairs. */
    getTablePhoto(id) {
        return apiFetch(`/api/table-photos/${encodeURIComponent(id)}`);
    },

    /** Lightweight processing status for polling. */
    getTablePhotoStatus(id) {
        return apiFetch(`/api/table-photos/${encodeURIComponent(id)}/status`);
    },

    /** Re-queue a table photo for background processing. */
    reprocessTablePhoto(id) {
        return apiFetch(`/api/table-photos/${encodeURIComponent(id)}/reprocess`, { method: "POST" });
    },

    /** List pairs (filters: table_photo_id, review_status, page, page_size). */
    getPairs(params = {}) {
        const qs = new URLSearchParams(params).toString();
        return apiFetch(`/api/pairs${qs ? "?" + qs : ""}`);
    },

    /** Get a single pair. */
    getPair(id) {
        return apiFetch(`/api/pairs/${encodeURIComponent(id)}`);
    },

    /** Resolve a scanned barcode to shipment/order info (Airtable now). */
    getShipment(barcode) {
        return apiFetch(`/api/shipment/${encodeURIComponent(barcode)}`);
    },

    /**
     * Human confirm/override a pair.
     * @param {string} id
     * @param {{ final_make?, final_model?, review_status?, notes? }} data
     */
    reviewPair(id, data) {
        return apiFetch(`/api/pairs/${encodeURIComponent(id)}/review`, {
            method: "PATCH",
            body:   JSON.stringify(data),
        });
    },
};
