/**
 * review_queue.js — Review Queue page
 *
 * Shows all shoes with review_status = PENDING, sorted oldest first.
 * Each row links to the Shoe Detail page where the worker can submit a decision.
 */

/* ----------------------------------------------------------------
   Load
   ---------------------------------------------------------------- */

async function loadQueue() {
    const container = document.getElementById("queue-container");
    const banner    = document.getElementById("queue-banner");
    showLoading(container);

    try {
        // Fetch all pending shoes (up to 200 — enough for any realistic queue)
        const data = await api.getShoes({ review_status: "PENDING", page_size: 200 });

        // Sort oldest first (FIFO queue) — the shoe that has been waiting longest
        // appears at the top so reviewers process in the order shoes were inspected.
        // The API returns newest-first by default, so we re-sort on the client.
        data.items.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));

        updateBanner(banner, data.total);
        renderTable(data.items, container);
    } catch (err) {
        showError(container, "Could not load review queue: " + err.message);
        if (banner) banner.style.display = "none";
    }
}

/* ----------------------------------------------------------------
   Banner (count at top of page)
   ---------------------------------------------------------------- */

function updateBanner(banner, count) {
    if (!banner) return;

    if (count === 0) {
        banner.classList.add("empty");
        banner.innerHTML = `
            <div class="queue-count">0</div>
            <div class="queue-banner-text">
                <strong>All caught up!</strong><br>
                <span class="text-muted text-sm">No shoes are pending review right now.</span>
            </div>`;
    } else {
        banner.innerHTML = `
            <div class="queue-count">${count}</div>
            <div class="queue-banner-text">
                <strong>${count === 1 ? "1 shoe needs" : count + " shoes need"} human review</strong><br>
                <span class="text-muted text-sm">
                    Sorted oldest first. Click <em>Review</em> to open the shoe detail and submit a decision.
                </span>
            </div>`;
    }
}

/* ----------------------------------------------------------------
   Table
   ---------------------------------------------------------------- */

function renderTable(items, container) {
    if (!items.length) {
        showEmpty(container, "No shoes are currently pending review.");
        return;
    }

    const rows = items.map(shoe => `
        <tr>
            <td class="td-id">
                <a href="shoe_detail.html?id=${shoe.id}" class="td-link">${shoe.id}</a>
            </td>
            <td class="td-muted">${formatDate(shoe.timestamp)}</td>
            <td>${thumbStripHTML(shoe)}</td>
            <td>${predictionBadgeHTML(shoe.ai_prediction)}</td>
            <td class="font-mono text-sm">${formatConfidence(shoe.ai_confidence)}</td>
            <td>${validationBadgeHTML(shoe.validation_status, shoe.validation_errors)}</td>
            <td>
                <a href="shoe_detail.html?id=${shoe.id}" class="btn-review-action">
                    Review <svg class="icon icon-inline" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>
                </a>
            </td>
        </tr>
    `).join("");

    container.innerHTML = `
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Shoe ID</th>
                        <th>Time</th>
                        <th>Images</th>
                        <th>AI Result</th>
                        <th>Confidence</th>
                        <th>Validation</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;
}

/* ----------------------------------------------------------------
   Init
   ---------------------------------------------------------------- */

loadQueue();
