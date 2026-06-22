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

    /** All Tableau visualizations in one cached payload. */
    getTableauStats() {
        return apiFetch("/api/tableau/stats");
    },

    /** Next batch of PENDING pairs ordered for gold-label value (Quick Label). */
    getGoldQueue(limit = 20) {
        return apiFetch(`/api/pairs/gold-queue?limit=${limit}`);
    },

    /* ---- Multi-worker table claiming (Quick Label) ---- */
    /** Tables with pending pairs, claim-aware (mine + free, others hidden). */
    getLabelTables(worker) {
        return apiFetch(`/api/labeling/tables?worker=${encodeURIComponent(worker)}`);
    },
    claimTable(tpId, worker) {
        return apiFetch(`/api/labeling/claim/${encodeURIComponent(tpId)}?worker=${encodeURIComponent(worker)}`, { method: "POST" });
    },
    heartbeatTable(tpId, worker) {
        return apiFetch(`/api/labeling/heartbeat/${encodeURIComponent(tpId)}?worker=${encodeURIComponent(worker)}`, { method: "POST" });
    },
    releaseTable(tpId, worker) {
        return apiFetch(`/api/labeling/release/${encodeURIComponent(tpId)}?worker=${encodeURIComponent(worker)}`, { method: "POST" });
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

    /** Delete a table photo + its pairs, crops, and Airtable outbox row. */
    deleteTablePhoto(id) {
        return apiFetch(`/api/table-photos/${encodeURIComponent(id)}`, { method: "DELETE" });
    },

    /** Re-identify unknown pairs via the cloud (one photo or all), re-sync Airtable. */
    reidentifyPhoto(id) {
        return apiFetch(`/api/reidentify/photo/${encodeURIComponent(id)}`, { method: "POST" });
    },
    reidentifyAll() {
        return apiFetch(`/api/reidentify/all`, { method: "POST" });
    },
    reidentifyStatus() {
        return apiFetch(`/api/reidentify/status`);
    },

    /** List pairs (filters: table_photo_id, review_status, page, page_size). */
    getPairs(params = {}) {
        const qs = new URLSearchParams(params).toString();
        return apiFetch(`/api/pairs${qs ? "?" + qs : ""}`);
    },

    /** Browse the curated label_data training set (filters: make, color, page, page_size). */
    getLabelData(params = {}) {
        const qs = new URLSearchParams(params).toString();
        return apiFetch(`/api/label-data${qs ? "?" + qs : ""}`);
    },

    /** Delete one label_data crop (+ its JSON sidecar) by filename. */
    deleteLabelData(filename) {
        return apiFetch(`/api/label-data/${encodeURIComponent(filename)}`, { method: "DELETE" });
    },

    /** Airtable outbox: queued captures + counts (filter by status pending|synced). */
    getAirtableOutbox(params = {}) {
        const qs = new URLSearchParams(params).toString();
        return apiFetch(`/api/airtable-outbox${qs ? "?" + qs : ""}`);
    },

    /** Retry all pending outbox rows now. */
    flushAirtableOutbox() {
        return apiFetch(`/api/airtable-outbox/flush`, { method: "POST" });
    },

    /** FedEx Track lookup for a queued box's tracking number (diagnostic). */
    fedexLookup(tpId) {
        return apiFetch(`/api/airtable-outbox/${encodeURIComponent(tpId)}/fedex`);
    },

    /** Get a single pair. */
    getPair(id) {
        return apiFetch(`/api/pairs/${encodeURIComponent(id)}`);
    },

    /** Resolve a scanned barcode to shipment/order info (Airtable now). */
    getShipment(barcode) {
        return apiFetch(`/api/shipment/${encodeURIComponent(barcode)}`);
    },

    /* ---- Config tab (station config + v4l2 camera control) -------------- */

    getStationConfig()       { return apiFetch("/api/config/station"); },
    saveStationConfig(cfg)   { return apiFetch("/api/config/station", { method: "PUT", body: JSON.stringify(cfg) }); },
    getIntegrations()        { return apiFetch("/api/config/integrations"); },
    getCameraDevices()       { return apiFetch("/api/camera/devices"); },
    getCameraControls(dev)   { return apiFetch(`/api/camera/controls?device=${encodeURIComponent(dev)}`); },
    getCameraResolutions(dev){ return apiFetch(`/api/camera/resolutions?device=${encodeURIComponent(dev)}`); },
    setCameraControl(device, name, value) {
        return apiFetch("/api/camera/control", { method: "POST", body: JSON.stringify({ device, name, value }) });
    },
    resetCamera(device) {
        return apiFetch("/api/camera/reset", { method: "POST", body: JSON.stringify({ device }) });
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

    /** Delete a single pair (+ its crop file). */
    deletePair(id) {
        return apiFetch(`/api/pairs/${encodeURIComponent(id)}`, { method: "DELETE" });
    },
};
