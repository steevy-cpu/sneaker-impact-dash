/**
 * live_feed.js — Live Feed page logic
 *
 * Shows the newest shoe inspection records in a table.
 * Auto-refreshes every 10 seconds when the user is on page 1.
 */

const PAGE_SIZE = 20;
let currentPage = 1;
let totalRecords = 0;
let refreshTimer = null;

/* ----------------------------------------------------------------
   Load and render
   ---------------------------------------------------------------- */

async function loadFeed(page) {
    const container = document.getElementById("feed-container");
    if (page === 1 && !container.querySelector("table")) {
        showLoading(container);
    }

    try {
        const data = await api.getShoes({ page, page_size: PAGE_SIZE });
        totalRecords = data.total;
        currentPage  = page;

        renderTable(data.items, container);
        updatePagination();
        updateTimestamp();
    } catch (err) {
        showError(container, "Could not load feed: " + err.message);
    }
}

function renderTable(items, container) {
    if (!items.length) {
        showEmpty(container, "No shoe inspections recorded yet.");
        return;
    }

    const rows = items.map(shoe => `
        <tr>
            <td class="td-id">
                <a href="shoe_detail.html?id=${shoe.id}" class="td-link">${shoe.id}</a>
            </td>
            <td class="td-muted text-sm font-mono">${shoe.batch_id}</td>
            <td class="td-muted">${formatDate(shoe.timestamp)}</td>
            <td>${thumbStripHTML(shoe)}</td>
            <td>${validationBadgeHTML(shoe.validation_status, shoe.validation_errors)}</td>
            <td>${predictionBadgeHTML(shoe.ai_prediction)}</td>
            <td class="font-mono text-sm">${formatConfidence(shoe.ai_confidence)}</td>
            <td>${reviewStatusBadgeHTML(shoe.review_status)}</td>
        </tr>
    `).join("");

    container.innerHTML = `
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Shoe ID</th>
                        <th>Batch</th>
                        <th>Time</th>
                        <th>Images</th>
                        <th>Validation</th>
                        <th>AI Result</th>
                        <th>Confidence</th>
                        <th>Review</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
    `;
}

/* ----------------------------------------------------------------
   Pagination controls
   ---------------------------------------------------------------- */

function updatePagination() {
    const totalPages = Math.max(1, Math.ceil(totalRecords / PAGE_SIZE));

    const infoEl = document.getElementById("page-info");
    if (infoEl) {
        infoEl.textContent = `Page ${currentPage} of ${totalPages}  (${totalRecords} total)`;
    }

    const prevBtn = document.getElementById("btn-prev");
    const nextBtn = document.getElementById("btn-next");
    if (prevBtn) prevBtn.disabled = currentPage <= 1;
    if (nextBtn) nextBtn.disabled = currentPage >= totalPages;
}

document.getElementById("btn-prev").addEventListener("click", () => {
    if (currentPage > 1) loadFeed(currentPage - 1);
});

document.getElementById("btn-next").addEventListener("click", () => {
    const totalPages = Math.ceil(totalRecords / PAGE_SIZE);
    if (currentPage < totalPages) loadFeed(currentPage + 1);
});

/* ----------------------------------------------------------------
   Refresh
   ---------------------------------------------------------------- */

function updateTimestamp() {
    const el = document.getElementById("last-updated");
    if (el) el.textContent = "Updated " + new Date().toLocaleTimeString();
}

function startAutoRefresh() {
    if (refreshTimer) clearInterval(refreshTimer);
    // Only auto-refresh on page 1 (the newest records).
    // Refreshing while the user is browsing older pages would jump them back to the top.
    refreshTimer = setInterval(() => {
        if (currentPage === 1) loadFeed(1);
    }, 10000);
}

/* ----------------------------------------------------------------
   Init
   ---------------------------------------------------------------- */

loadFeed(1);
startAutoRefresh();
