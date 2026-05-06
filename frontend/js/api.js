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
};
