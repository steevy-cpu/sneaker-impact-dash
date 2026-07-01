/**
 * tableau.js — Tableau visualization page.
 *
 * Makes ONE request to /api/tableau/stats (cached server-side) and renders every
 * chart from that single payload — no per-chart calls, no polling. Chart.js is
 * defer-loaded, so we build on DOMContentLoaded (fires after deferred scripts),
 * guaranteeing `Chart` is defined.
 */

// Brand-neutral categorical palette for bars/donuts.
const PAL = ["#6366f1", "#06b6d4", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
             "#ec4899", "#14b8a6", "#f97316", "#3b82f6", "#84cc16", "#a855f7"];
// Real-ish swatches for the color-distribution donut.
const COLOR_HEX = {
    black:"#111827", white:"#e5e7eb", gray:"#9ca3af", grey:"#9ca3af",
    brown:"#92400e", red:"#ef4444", orange:"#f97316", yellow:"#eab308",
    green:"#22c55e", blue:"#3b82f6", purple:"#a855f7", pink:"#ec4899",
    unknown:"#cbd5e1", multi:"#64748b",
};
const charts = [];
const n = (v) => (v == null ? "0" : Number(v).toLocaleString());
const RANGE_LABEL = { today: "today", week: "this week", "30d": "in the last 30 days",
                      "90d": "in the last 90 days", all: "since launch" };
let currentRange = "all";

function setText(id, v) { const e = document.getElementById(id); if (e) e.textContent = v; }
function insight(id, html) { const e = document.getElementById(id); if (e) e.innerHTML = html; }

function mk(canvasId, config) {
    const el = document.getElementById(canvasId);
    if (!el) return;
    config.options = Object.assign({
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 600 },
    }, config.options || {});
    charts.push(new Chart(el.getContext("2d"), config));
}

function hbar(canvasId, labels, data, colorFn) {
    mk(canvasId, {
        type: "bar",
        data: { labels, datasets: [{
            data, borderWidth: 0, borderRadius: 5,
            backgroundColor: labels.map((l, i) => colorFn ? colorFn(l, i) : PAL[i % PAL.length]),
        }] },
        options: {
            indexAxis: "y",
            plugins: { legend: { display: false } },
            scales: { x: { beginAtZero: true, grid: { display: false } },
                      y: { grid: { display: false } } },
        },
    });
}

function vbar(canvasId, labels, data, color) {
    mk(canvasId, {
        type: "bar",
        data: { labels, datasets: [{ data, backgroundColor: color || "#6366f1",
            borderWidth: 0, borderRadius: 5 }] },
        options: { plugins: { legend: { display: false } },
            scales: { y: { beginAtZero: true, grid: { color: "rgba(148,163,184,.15)" } },
                      x: { grid: { display: false } } } },
    });
}

function donut(canvasId, labels, data, colors) {
    mk(canvasId, {
        type: "doughnut",
        data: { labels, datasets: [{ data, backgroundColor: colors,
            borderWidth: 2, borderColor: "rgba(255,255,255,.6)" }] },
        options: { cutout: "58%",
            plugins: { legend: { position: "right",
                labels: { boxWidth: 12, font: { size: 11 }, padding: 8 } } } },
    });
}

function pct(part, whole) { return whole ? Math.round(100 * part / whole) : 0; }

function render(d) {
    // ---- Impact / ESG headline ----
    const im = d.impact || {};
    setText("im-shoes", n(im.shoes_diverted));
    setText("im-shoes-sub", `${n(im.reused)} reused · ${n(im.recycled)} recycled · ${n(im.casual)} casual`);
    setText("im-reused", n(im.reused));
    setText("im-reused-sub", `${im.reuse_rate || 0}% reuse rate`);
    setText("im-weight", n(im.weight_lbs) + " lb");
    setText("im-weight-sub", `across ${n(im.weighed_boxes)} weighed boxes`);
    setText("im-co2e", (im.co2e_tonnes >= 1 ? im.co2e_tonnes + " t" : n(im.co2e_kg) + " kg"));
    setText("im-co2e-sub", "≈ " + n(im.co2e_kg) + " kg CO₂e · estimated");

    // ---- KPIs ----
    const k = d.kpis;
    setText("kpi-photos", n(k.table_photos));
    setText("kpi-pairs", n(k.pairs));
    setText("kpi-shoes", n(k.shoes));
    setText("kpi-brands", n(k.brands));
    setText("kpi-models", n(k.models));
    setText("kpi-identified", k.identified_pct + "%");
    setText("kpi-sync", k.sync_rate + "%");

    // ---- Brands ----
    const bl = d.brands.map(b => b.label), bc = d.brands.map(b => b.count);
    const bTot = bc.reduce((a, b) => a + b, 0);
    hbar("c-brands", bl, bc);
    if (bl.length) insight("ins-brands",
        `<b>${bl[0]}</b> leads with <b>${n(bc[0])}</b> pairs (${pct(bc[0], bTot)}% of identified) · ${d.brands.length} brands shown`);

    // ---- Models ----
    const ml = d.models.map(m => m.label), mc = d.models.map(m => m.count);
    hbar("c-models", ml, mc, () => "#06b6d4");
    if (ml.length) insight("ins-models", `Most common silhouette: <b>${ml[0]}</b> (${n(mc[0])} pairs)`);

    // ---- Colors ----
    const cl = d.colors.map(c => c.label), cc = d.colors.map(c => c.count);
    const cTot = cc.reduce((a, b) => a + b, 0);
    donut("c-colors", cl, cc, cl.map(c => COLOR_HEX[(c || "").toLowerCase()] || "#cbd5e1"));
    const unk = d.colors.find(c => (c.label || "").toLowerCase() === "unknown");
    insight("ins-colors", `${cl.length} colors detected${unk ? ` · ${pct(unk.count, cTot)}% still uncolored` : ""}`);

    // ---- Box composition ----
    const bx = d.box;
    donut("c-box", ["Good", "End-of-Life", "Casual"], [bx.good, bx.eol, bx.casuals],
          ["#10b981", "#ef4444", "#f59e0b"]);
    const boxTot = bx.good + bx.eol + bx.casuals;
    insight("ins-box", `Good <b>${pct(bx.good, boxTot)}%</b> · EOL <b>${pct(bx.eol, boxTot)}%</b> · Casual <b>${pct(bx.casuals, boxTot)}%</b> · avg box ${bx.avg_weight} lb`);

    // ---- AI source ----
    const src = d.ai.source;
    const sl = Object.keys(src).filter(kk => src[kk] > 0), sv = sl.map(kk => src[kk]);
    donut("c-source", sl, sv, sl.map((kk, i) =>
        kk === "local" ? "#10b981" : kk.includes("Gemini") ? "#6366f1"
        : kk.includes("OpenAI") ? "#06b6d4" : "#cbd5e1"));
    const sTot = sv.reduce((a, b) => a + b, 0);
    const cloud = (src["cloud · Gemini"] || 0) + (src["cloud · OpenAI"] || 0);
    insight("ins-source", `<b>${pct(cloud, sTot)}%</b> resolved by the cloud, <b>${pct(src.local || 0, sTot)}%</b> locally`);

    // ---- Review ----
    const rv = d.ai.review;
    const auto = rv.NOT_REQUIRED || 0, pend = rv.PENDING || 0, done = rv.COMPLETED || 0;
    donut("c-review", ["Auto-approved", "Pending review", "Completed"], [auto, pend, done],
          ["#10b981", "#f59e0b", "#3b82f6"]);
    insight("ins-review", `<b>${pct(auto, auto + pend + done)}%</b> auto-approved · ${n(pend)} awaiting human review`);

    // ---- Confidence histograms ----
    const mkc = d.ai.make_conf, mdc = d.ai.model_conf, clc = d.ai.color_conf;
    vbar("c-makeconf", mkc.labels, mkc.counts, "#6366f1");
    const mkTop = mkc.counts[4] || 0, mkAll = mkc.counts.reduce((a, b) => a + b, 0);
    insight("ins-makeconf", `<b>${pct(mkTop, mkAll)}%</b> of brand calls are ≥90% confident`);
    vbar("c-modelconf", mdc.labels, mdc.counts, "#8b5cf6");
    const mdTop = mdc.counts[4] || 0, mdAll = mdc.counts.reduce((a, b) => a + b, 0);
    insight("ins-modelconf", `<b>${pct(mdTop, mdAll)}%</b> of model calls are ≥90% confident`);
    vbar("c-colorconf", clc.labels, clc.counts, "#14b8a6");
    insight("ins-colorconf", `Color confidence across ${n(clc.counts.reduce((a, b) => a + b, 0))} pairs`);

    // ---- Pairing ----
    const pr = d.pairing;
    donut("c-pairing", ["Tied pairs", "Singles"], [pr.pairs, pr.singles], ["#06b6d4", "#f59e0b"]);
    insight("ins-pairing", `<b>${pct(pr.pairs, pr.pairs + pr.singles)}%</b> matched into pairs · ${n(pr.singles)} singles`);
    vbar("c-pairscore", pr.score_hist.labels, pr.score_hist.counts, "#06b6d4");
    insight("ins-pairscore", `Average appearance-match score <b>${Math.round(pr.avg_score * 100)}%</b>`);

    // ---- Timeline ----
    const tl = d.timeline;
    const short = tl.days.map(s => s.slice(5));  // MM-DD
    mk("c-timeline", {
        type: "line",
        data: { labels: short, datasets: [
            { label: "Pairs", data: tl.pairs, borderColor: "#6366f1",
              backgroundColor: "rgba(99,102,241,.12)", fill: true, tension: .35,
              pointRadius: 0, borderWidth: 2, yAxisID: "y" },
            { label: "Photos", data: tl.photos, borderColor: "#06b6d4",
              backgroundColor: "rgba(6,182,212,.10)", fill: true, tension: .35,
              pointRadius: 0, borderWidth: 2, yAxisID: "y1" },
        ] },
        options: { interaction: { mode: "index", intersect: false },
            plugins: { legend: { position: "top", labels: { boxWidth: 12, font: { size: 11 } } } },
            scales: {
                y: { beginAtZero: true, position: "left", grid: { color: "rgba(148,163,184,.15)" },
                     title: { display: true, text: "pairs" } },
                y1: { beginAtZero: true, position: "right", grid: { display: false },
                      title: { display: true, text: "photos" } },
                x: { grid: { display: false }, ticks: { maxTicksLimit: 12 } } } },
    });
    const pT = tl.pairs.reduce((a, b) => a + b, 0), phT = tl.photos.reduce((a, b) => a + b, 0);
    insight("ins-timeline", `<b>${n(phT)}</b> photos and <b>${n(pT)}</b> pairs processed ${RANGE_LABEL[d.range] || "over the shown window"}`);

    // ---- Airtable sync ----
    const ob = d.airtable.status;
    const synced = ob.synced || 0, pending = ob.pending || 0;
    donut("c-sync", ["Synced", "Pending"], [synced, pending], ["#10b981", "#f59e0b"]);
    let syncIns = `<b>${d.airtable.sync_rate}%</b> synced to Airtable`;
    if (pending) syncIns += ` · ${n(pending)} pending`;
    if (d.airtable.errors && d.airtable.errors.length)
        syncIns += ` (${d.airtable.errors.map(e => `${e[0]}×${e[1]}`).join(", ")})`;
    insight("ins-sync", syncIns);

    setText("tb-foot", "Generated " + (d.generated_at || "").replace("T", " ") + " · auto-cached 60s");
}

// Chart.js reuses <canvas> elements, so every chart from the previous window
// must be torn down before we re-render, or it throws "Canvas already in use".
function destroyCharts() {
    charts.forEach((c) => { try { c.destroy(); } catch (_) {} });
    charts.length = 0;
}

async function load(range) {
    currentRange = range || currentRange;
    const err = document.getElementById("tb-error");
    err.style.display = "none";
    try {
        const data = await api.getTableauStats(currentRange);
        destroyCharts();
        render(data);
    } catch (e) {
        err.style.display = "block";
        err.textContent = "Could not load visualizations: " + e.message;
    }
}

function initRanges() {
    const bar = document.getElementById("tb-ranges");
    if (!bar) return;
    bar.addEventListener("click", (e) => {
        const btn = e.target.closest(".tb-range");
        if (!btn || btn.classList.contains("active")) return;
        bar.querySelectorAll(".tb-range").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        load(btn.dataset.range);
    });
}

document.addEventListener("DOMContentLoaded", () => { initRanges(); load("all"); });
