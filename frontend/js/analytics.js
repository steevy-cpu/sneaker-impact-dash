/**
 * analytics.js — Analytics page logic
 *
 * Fetches trend data from /api/analytics/trends, fills summary cards,
 * and renders three Chart.js charts. Period selector buttons rebuild
 * everything for the chosen window.
 */

let chartDaily      = null;
let chartConfidence = null;
let chartOverrides  = null;
let chartBrands     = null;
let activeDays      = 14;

// ── Colour palette ────────────────────────────────────────────────────────
const C = {
    reuse:    { fill: "rgba(22,163,74,0.15)",   line: "#16a34a" },
    recycle:  { fill: "rgba(255,127,36,0.18)",  line: "#ff7f24" },
    review:   { fill: "rgba(245,158,11,0.15)",  line: "#f59e0b" },
    conf:     { fill: "rgba(8,166,194,0.12)",   line: "#08a6c2" },
    override: { fill: "rgba(168,85,247,0.12)",  line: "#9333ea" },
    failed:   { fill: "rgba(220,38,38,0.12)",   line: "#dc2626" },
};

// ── Chart defaults ────────────────────────────────────────────────────────
Chart.defaults.font.family = "Inter, system-ui, sans-serif";
Chart.defaults.font.size   = 12;
Chart.defaults.color       = "#64748b";

// ── Short date label (e.g. "May 3") ──────────────────────────────────────
function shortDate(iso) {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

// ── Destroy then recreate a chart ─────────────────────────────────────────
function makeChart(ref, canvasId, config) {
    if (ref) ref.destroy();
    const ctx = document.getElementById(canvasId).getContext("2d");
    return new Chart(ctx, config);
}

// ── Shared axis options ───────────────────────────────────────────────────
function xAxis(labels) {
    return {
        type: "category",
        labels,
        ticks: { maxTicksLimit: 10 },
        grid:  { color: "rgba(0,0,0,0.05)" },
    };
}

function yAxis(title, min = 0) {
    return {
        beginAtZero: true,
        min,
        title: { display: !!title, text: title, color: "#94a3b8", font: { size: 11 } },
        grid:  { color: "rgba(0,0,0,0.05)" },
    };
}

// ── Build summary totals from trend data ──────────────────────────────────
function buildSummary(data) {
    let total = 0, confSum = 0, confDays = 0,
        overrides = 0, reviewed = 0, agreed = 0;

    for (const d of data) {
        total    += d.total;
        overrides += d.human_override_count;
        reviewed  += d.human_reviewed_count;
        agreed    += d.ai_human_agreed;
        if (d.avg_confidence > 0) { confSum += d.avg_confidence; confDays++; }
    }

    document.getElementById("sum-total").textContent     = total.toLocaleString();
    document.getElementById("sum-confidence").textContent =
        confDays ? (confSum / confDays * 100).toFixed(1) + "%" : "—";
    // Override rate = overrides / reviewed (not overrides / total).
    // Unreviewed shoes are excluded so the metric only reflects deliberate human decisions.
    document.getElementById("sum-override-rate").textContent =
        reviewed ? (overrides / reviewed * 100).toFixed(1) + "%" : "0%";
    document.getElementById("sum-agreement").textContent =
        reviewed ? (agreed / reviewed * 100).toFixed(1) + "%" : "—";
}

// ── Chart insight text ─────────────────────────────────────────────────────
function computeInsights(data) {
    const n = data.length;

    // Confidence trend — compare first half vs second half
    let confInsight = null;
    const withConf = data.filter(d => d.avg_confidence > 0);
    if (withConf.length >= 2) {
        const recent = withConf.slice(-3);
        const earlier = withConf.slice(0, Math.max(1, withConf.length - 3));
        const recentAvg  = recent.reduce((s, d) => s + d.avg_confidence, 0) / recent.length;
        const earlierAvg = earlier.reduce((s, d) => s + d.avg_confidence, 0) / earlier.length;
        const delta = recentAvg - earlierAvg;
        // ±0.02 = 2 percentage-point shift; below that, treat as noise
        if (delta > 0.02)       confInsight = { dot: "ok",      text: `Confidence trending upward over the last ${Math.min(3, recent.length)} days` };
        else if (delta < -0.02) confInsight = { dot: "warn",    text: `Confidence trending downward — check camera alignment` };
        else                    confInsight = { dot: "neutral",  text: "Confidence stable across this period" };
    }

    // Override / review trend
    let overrideInsight = null;
    const totalReviewed  = data.reduce((s, d) => s + d.human_reviewed_count, 0);
    const totalOverrides = data.reduce((s, d) => s + d.human_override_count, 0);
    if (totalReviewed > 0) {
        const rate = (totalOverrides / totalReviewed) * 100;
        // >30% = workers frequently disagree with AI (model may need retraining)
        // 10–30% = expected range in normal operation
        // <10% = AI is performing well
        if (rate > 30)       overrideInsight = { dot: "warn",    text: `Override rate is ${rate.toFixed(0)}% — workers are frequently disagreeing with AI` };
        else if (rate > 10)  overrideInsight = { dot: "neutral", text: `${rate.toFixed(0)}% override rate — normal range` };
        else                 overrideInsight = { dot: "ok",      text: `Low override rate (${rate.toFixed(0)}%) — AI predictions are accurate` };
    }

    // Daily volume
    let volumeInsight = null;
    if (n >= 2) {
        const lastDay = data[n-1].total;
        const prevDay = data[n-2].total;
        if (prevDay > 0) {
            const pct = Math.round(((lastDay - prevDay) / prevDay) * 100);
            if (pct > 10)        volumeInsight = { dot: "ok",      text: `Volume up ${pct}% vs previous day` };
            else if (pct < -10)  volumeInsight = { dot: "warn",    text: `Volume down ${Math.abs(pct)}% vs previous day` };
            else                 volumeInsight = { dot: "neutral",  text: "Volume consistent with previous day" };
        }
    }

    return { confInsight, overrideInsight, volumeInsight };
}

function insightHTML(insight) {
    if (!insight) return "";
    return `<div class="chart-insight">
        <span class="chart-insight-dot ${insight.dot}"></span>
        ${insight.text}
    </div>`;
}

// ── Chart 1: stacked bar — reuse / recycle / review ───────────────────────
function buildDailyChart(data, insights) {
    const labels    = data.map(d => shortDate(d.date));
    const isSingle  = data.length <= 1;
    const insightEl = document.getElementById("insight-daily");
    if (insightEl) insightEl.innerHTML = insightHTML(insights.volumeInsight);
    chartDaily = makeChart(chartDaily, "chart-daily", {
        type: "bar",
        data: {
            labels,
            datasets: [
                {
                    label:           "Reuse",
                    data:            data.map(d => d.reuse_count),
                    backgroundColor: C.reuse.line,
                    borderRadius:    3,
                },
                {
                    label:           "Recycle",
                    data:            data.map(d => d.recycle_count),
                    backgroundColor: C.recycle.line,
                    borderRadius:    3,
                },
                {
                    label:           "Review",
                    data:            data.map(d => d.review_count),
                    backgroundColor: C.review.line,
                    borderRadius:    3,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { position: "bottom" }, tooltip: { mode: "index" } },
            scales:  { x: xAxis(labels), y: { ...yAxis("Shoes"), stacked: true } },
        },
    });
}

// ── Chart 2: line — avg confidence ───────────────────────────────────────
function buildConfidenceChart(data, insights) {
    const labels   = data.map(d => shortDate(d.date));
    const isSingle = data.length <= 1;
    const insightEl = document.getElementById("insight-confidence");
    if (insightEl) insightEl.innerHTML = insightHTML(insights.confInsight);
    // Map 0 → null so Chart.js skips days with no confidence data (e.g. days with only
    // failed captures). spanGaps: true draws a line across the gap.
    const pct      = data.map(d => d.avg_confidence > 0
        ? parseFloat((d.avg_confidence * 100).toFixed(1)) : null);

    chartConfidence = makeChart(chartConfidence, "chart-confidence", {
        type: "line",
        data: {
            labels,
            datasets: [{
                label:            "Avg Confidence %",
                data:             pct,
                borderColor:      C.conf.line,
                backgroundColor:  C.conf.fill,
                fill:             true,
                tension:          0.35,
                pointRadius:      isSingle ? 6 : 3,
                spanGaps:         true,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales:  { x: xAxis(labels), y: { ...yAxis("Confidence %", 0), max: 100 } },
        },
    });
}

// ── Chart 3: line — override rate + failed captures ───────────────────────
function buildOverrideChart(data, insights) {
    const labels   = data.map(d => shortDate(d.date));
    const isSingle = data.length <= 1;
    const insightEl = document.getElementById("insight-overrides");
    if (insightEl) insightEl.innerHTML = insightHTML(insights.overrideInsight);

    chartOverrides = makeChart(chartOverrides, "chart-overrides", {
        type: "line",
        data: {
            labels,
            datasets: [
                {
                    label:           "Human Overrides",
                    data:            data.map(d => d.human_override_count),
                    borderColor:     C.override.line,
                    backgroundColor: C.override.fill,
                    fill:            true,
                    tension:         0.35,
                    pointRadius:     isSingle ? 6 : 3,
                },
                {
                    label:           "Failed Captures",
                    data:            data.map(d => d.failed_captures),
                    borderColor:     C.failed.line,
                    backgroundColor: C.failed.fill,
                    fill:            true,
                    tension:         0.35,
                    pointRadius:     isSingle ? 6 : 3,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { position: "bottom" }, tooltip: { mode: "index" } },
            scales:  { x: xAxis(labels), y: yAxis("Count") },
        },
    });
}

// ── Global insights card ──────────────────────────────────────────────────
// Renders 3–5 plain-English observations above the charts.
// Complements the per-chart insight bars with a higher-level summary.
function buildGlobalInsights(data) {
    const el = document.getElementById("insights-list");
    if (!el) return;

    const items = [];

    // 1. Dominant prediction outcome
    const totals = data.reduce((acc, d) => {
        acc.reuse   += d.reuse_count;
        acc.recycle += d.recycle_count;
        acc.review  += d.review_count;
        acc.total   += d.total;
        return acc;
    }, { reuse: 0, recycle: 0, review: 0, total: 0 });

    if (totals.total > 0) {
        const dominant = totals.reuse >= totals.recycle && totals.reuse >= totals.review
            ? { label: "REUSE",   pct: totals.reuse   / totals.total }
            : totals.recycle >= totals.review
            ? { label: "RECYCLE", pct: totals.recycle / totals.total }
            : { label: "REVIEW",  pct: totals.review  / totals.total };

        const dot = dominant.label === "REUSE" ? "ok" : dominant.label === "RECYCLE" ? "neutral" : "warn";
        items.push({ dot, title: "Dominant outcome", text: `${dominant.label} is the most common AI result at ${(dominant.pct * 100).toFixed(0)}% of ${totals.total.toLocaleString()} inspections` });
    }

    // 2. Average confidence level
    const confDays = data.filter(d => d.avg_confidence > 0);
    if (confDays.length > 0) {
        const avgConf = confDays.reduce((s, d) => s + d.avg_confidence, 0) / confDays.length;
        const dot  = avgConf >= 0.85 ? "ok" : avgConf >= 0.70 ? "neutral" : "warn";
        const qual = avgConf >= 0.85 ? "High" : avgConf >= 0.70 ? "Moderate" : "Low";
        items.push({ dot, title: "AI confidence", text: `${qual} average confidence (${(avgConf * 100).toFixed(1)}%) across this period` });
    }

    // 3. Failed capture rate
    const totalFailed = data.reduce((s, d) => s + d.failed_captures, 0);
    const totalAll    = data.reduce((s, d) => s + d.total, 0);
    if (totalAll > 0) {
        const failPct = totalFailed / totalAll;
        const dot  = failPct < 0.05 ? "ok" : failPct < 0.15 ? "neutral" : "warn";
        const qual = failPct < 0.05 ? "Low" : failPct < 0.15 ? "Elevated" : "High";
        items.push({ dot, title: "Capture failures", text: `${qual} failure rate — ${totalFailed} failed capture${totalFailed !== 1 ? "s" : ""} (${(failPct * 100).toFixed(1)}% of total)` });
    }

    // 4. Override rate observation (if any reviewed shoes exist)
    const totalReviewed  = data.reduce((s, d) => s + d.human_reviewed_count, 0);
    const totalOverrides = data.reduce((s, d) => s + d.human_override_count, 0);
    if (totalReviewed > 0) {
        const rate = (totalOverrides / totalReviewed) * 100;
        const dot  = rate < 10 ? "ok" : rate < 30 ? "neutral" : "warn";
        items.push({ dot, title: "Human overrides", text: `Workers overrode the AI in ${rate.toFixed(0)}% of reviewed cases (${totalOverrides} of ${totalReviewed} reviewed)` });
    }

    // 5. Today vs yesterday volume trend
    const n = data.length;
    if (n >= 2) {
        const today = data[n - 1].total;
        const prev  = data[n - 2].total;
        if (prev > 0) {
            const pct = Math.round(((today - prev) / prev) * 100);
            const dot = pct > 10 ? "ok" : pct < -10 ? "warn" : "neutral";
            const dir = pct > 0 ? `up ${pct}%` : pct < 0 ? `down ${Math.abs(pct)}%` : "unchanged";
            items.push({ dot, title: "Volume trend", text: `Today's inspection volume is ${dir} compared to the previous day` });
        } else if (today > 0) {
            items.push({ dot: "neutral", title: "Volume trend", text: `${today} inspection${today !== 1 ? "s" : ""} recorded today` });
        }
    }

    el.innerHTML = items.map(item => `
        <div class="insight-item">
            <span class="insight-dot ${item.dot}"></span>
            <span class="insight-text">
                <strong>${item.title}</strong>
                <span>${item.text}</span>
            </span>
        </div>
    `).join("");
}

// ── Main load function ────────────────────────────────────────────────────
async function loadAnalytics(days) {
    activeDays = days;

    // Show loading state in summary cards
    ["sum-total", "sum-confidence", "sum-override-rate", "sum-agreement"]
        .forEach(id => { document.getElementById(id).textContent = "…"; });

    try {
        const result = await api.getTrends(days);
        const data   = result.data;

        const insights = computeInsights(data);
        buildSummary(data);
        buildGlobalInsights(data);
        buildDailyChart(data, insights);
        buildConfidenceChart(data, insights);
        buildOverrideChart(data, insights);
    } catch (err) {
        ["sum-total", "sum-confidence", "sum-override-rate", "sum-agreement"]
            .forEach(id => { document.getElementById(id).textContent = "—"; });
        console.error("Analytics error:", err);
    }
}

// ── Period selector wiring ────────────────────────────────────────────────
document.getElementById("period-selector").addEventListener("click", e => {
    const btn = e.target.closest(".period-btn");
    if (!btn) return;

    document.querySelectorAll(".period-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    loadAnalytics(parseInt(btn.dataset.days, 10));
});

// ── Brand distribution chart + insights ──────────────────────────────────

const BRAND_PALETTE = [
    "#16a34a","#08a6c2","#f59e0b","#9333ea","#ef4444",
    "#0ea5e9","#f97316","#84cc16","#ec4899","#64748b",
];

async function loadBrandAnalytics() {
    try {
        const data = await api.getBrandDistribution();
        if (!data.brands.length) return;
        _buildBrandInsights(data.brands);
        _buildBrandChart(data.brands);
    } catch (_) { /* non-critical */ }
}

function _buildBrandInsights(brands) {
    const el = document.getElementById("brand-insights-list");
    if (!el) return;

    const top   = brands[0];
    const total = brands.reduce((s, b) => s + b.count, 0);
    const pct   = total > 0 ? Math.round(top.count / total * 100) : 0;

    const highRev = [...brands].sort((a, b) => b.review_rate - a.review_rate)[0];

    const items = [
        { dot: "ok",   title: "Most inspected brand",
          text: `${top.brand} — ${top.count.toLocaleString()} inspections (${pct}% of total)` },
        { dot: "warn", title: "Highest review rate",
          text: `${highRev.brand} — ${(highRev.review_rate * 100).toFixed(0)}% of its inspections flagged for review` },
        { dot: "neutral", title: "Most common color",
          text: "White — 100% of inspections (simulation uses uniform placeholder images)" },
    ];

    el.innerHTML = items.map(item => `
        <div class="insight-item">
            <span class="insight-dot ${item.dot}"></span>
            <span class="insight-text">
                <strong>${item.title}</strong>
                <span>${item.text}</span>
            </span>
        </div>
    `).join("");
}

function _buildBrandChart(brands) {
    const canvas = document.getElementById("chart-brands");
    if (!canvas) return;

    if (chartBrands) chartBrands.destroy();
    chartBrands = new Chart(canvas.getContext("2d"), {
        type: "doughnut",
        data: {
            labels: brands.map(b => b.brand),
            datasets: [{
                data:            brands.map(b => b.count),
                backgroundColor: brands.map((_, i) => BRAND_PALETTE[i % BRAND_PALETTE.length]),
                borderWidth:     2,
                borderColor:     "rgba(255,255,255,0.8)",
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: "right", labels: { font: { size: 12 } } },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            const b     = brands[ctx.dataIndex];
                            const total = brands.reduce((s, x) => s + x.count, 0);
                            return ` ${b.brand}: ${b.count} (${Math.round(b.count / total * 100)}%)`;
                        },
                    },
                },
            },
        },
    });
}

// ── Bootstrap ────────────────────────────────────────────────────────────
loadAnalytics(activeDays);
loadBrandAnalytics();
