/**
 * labeling.js — Client-side shoe image labeling tool.
 *
 * Workflow (everything stays in the browser, nothing is uploaded):
 *   1. User loads a folder / set of image files.
 *   2. They step through each image tagging decision, color, and brand.
 *   3. On finish, JSZip builds a ZIP with every file renamed
 *      `shoe_{decision}_{color}_{brand}_{n}.{ext}` and the browser downloads it.
 *
 * The counter {n} resets per unique decision+color+brand combination, so two
 * blue Nike reuse shoes become _1 and _2.
 */

// ---------------------------------------------------------------------------
// Predefined options
// ---------------------------------------------------------------------------

const DECISIONS = [
    { key: "reuse",   label: "Reuse",   cls: "decision-reuse" },
    { key: "recycle", label: "Recycle", cls: "decision-recycle" },
];

// Display name → swatch color. `slug` is what goes in the filename.
const COLORS = [
    { name: "White",      slug: "white",      hex: "#ffffff" },
    { name: "Black",      slug: "black",      hex: "#1a1a1a" },
    { name: "Gray",       slug: "gray",       hex: "#9ca3af" },
    { name: "Blue",       slug: "blue",       hex: "#2563eb" },
    { name: "Red",        slug: "red",        hex: "#dc2626" },
    { name: "Green",      slug: "green",      hex: "#16a34a" },
    { name: "Yellow",     slug: "yellow",     hex: "#facc15" },
    { name: "Orange",     slug: "orange",     hex: "#f97316" },
    { name: "Pink",       slug: "pink",       hex: "#ec4899" },
    { name: "Purple",     slug: "purple",     hex: "#9333ea" },
    { name: "Brown",      slug: "brown",      hex: "#92400e" },
    { name: "Beige",      slug: "beige",      hex: "#e7d3b3" },
    { name: "Multicolor", slug: "multicolor", hex: "linear-gradient(135deg,#ef4444,#f59e0b,#10b981,#3b82f6,#a855f7)" },
];

// Predefined brands — mirrors backend/services/simulation.py. Custom brands the
// user types are appended at runtime so they become reusable chips too.
const BRANDS = [
    "Nike", "Adidas", "Puma", "New Balance", "ASICS",
    "Reebok", "Converse", "Vans", "Under Armour", "Skechers",
];

const IMAGE_EXT = /\.(jpe?g|png|webp|avif|gif|bmp|tiff?)$/i;

/**
 * True for real image files we should label. Rejects:
 *  - macOS AppleDouble sidecars ("._foo.jpg") and other dot-hidden files
 *  - OS junk (.DS_Store, Thumbs.db) and the __MACOSX folder
 * The basename is checked because folder uploads give paths like "dir/._x.jpg".
 */
function isUsableImage(f) {
    const base = (f.name || "").split("/").pop();
    const path = f.webkitRelativePath || f.name || "";
    if (base.startsWith(".")) return false;          // ._sidecar, .DS_Store, hidden
    if (/(^|\/)__MACOSX(\/|$)/.test(path)) return false;
    if (/^Thumbs\.db$/i.test(base)) return false;
    return IMAGE_EXT.test(base) || (f.type || "").startsWith("image/");
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
    items: [],          // { file, url, name, decision, color, brand }
    index: 0,
    brands: [...BRANDS],
    lastZip: null,      // { blob, filename } for re-download
};

// DOM handles
const el = {};

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
    // cache elements
    [
        "upload-stage", "label-stage", "done-stage",
        "dropzone", "file-input", "file-input-files",
        "pick-folder-btn", "pick-files-btn",
        "progress-fill", "progress-count", "restart-btn",
        "viewer-img", "viewer-pos", "viewer-name", "prev-btn", "next-btn",
        "decision-group", "color-group", "brand-group",
        "custom-brand-wrap", "custom-brand-input", "custom-brand-add",
        "name-preview", "apply-next-btn", "finish-btn",
        "done-sub", "redownload-btn", "label-more-btn",
    ].forEach(id => { el[camel(id)] = document.getElementById(id); });

    wireUpload();
    wireControls();
    buildStaticControls();
});

function camel(id) { return id.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }

// ---------------------------------------------------------------------------
// Upload stage
// ---------------------------------------------------------------------------

function wireUpload() {
    el.pickFolderBtn.addEventListener("click", () => el.fileInput.click());
    el.pickFilesBtn.addEventListener("click", () => el.fileInputFiles.click());
    el.fileInput.addEventListener("change", e => loadFiles(e.target.files));
    el.fileInputFiles.addEventListener("change", e => loadFiles(e.target.files));

    const dz = el.dropzone;
    ["dragenter", "dragover"].forEach(ev =>
        dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add("dragover"); }));
    ["dragleave", "drop"].forEach(ev =>
        dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove("dragover"); }));
    dz.addEventListener("drop", async e => {
        const files = await collectDroppedFiles(e.dataTransfer);
        loadFiles(files);
    });
}

/** Pull files out of a drop, descending into a dropped folder when supported. */
async function collectDroppedFiles(dt) {
    const entries = dt.items ? [...dt.items].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()) : [];
    if (entries.some(Boolean)) {
        const out = [];
        await Promise.all(entries.filter(Boolean).map(entry => walkEntry(entry, out)));
        return out;
    }
    return [...dt.files]; // fallback: flat file list
}

function walkEntry(entry, out) {
    return new Promise(resolve => {
        if (entry.isFile) {
            entry.file(f => { out.push(f); resolve(); }, () => resolve());
        } else if (entry.isDirectory) {
            const reader = entry.createReader();
            const readBatch = () => reader.readEntries(async batch => {
                if (!batch.length) return resolve();
                await Promise.all(batch.map(e => walkEntry(e, out)));
                readBatch(); // directories may need multiple reads
            }, () => resolve());
            readBatch();
        } else {
            resolve();
        }
    });
}

function loadFiles(fileList) {
    const images = [...fileList].filter(isUsableImage);
    if (!images.length) {
        alert("No image files found. Please choose a folder or files containing images.");
        return;
    }
    // Natural sort by filename so the order is predictable.
    images.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }));

    revokeUrls();
    state.items = images.map(file => ({
        file,
        url: URL.createObjectURL(file),
        name: file.name,
        decision: null,
        color: null,
        brand: null,
    }));
    state.index = 0;
    state.lastZip = null;

    showStage("label");
    render();
}

// ---------------------------------------------------------------------------
// Static controls (built once)
// ---------------------------------------------------------------------------

function buildStaticControls() {
    // Decision buttons
    el.decisionGroup.innerHTML = DECISIONS.map(d =>
        `<button type="button" class="decision-btn ${d.cls}" data-decision="${d.key}">${d.label}</button>`
    ).join("");
    el.decisionGroup.querySelectorAll("button").forEach(btn =>
        btn.addEventListener("click", () => setField("decision", btn.dataset.decision)));

    // Color swatches
    el.colorGroup.innerHTML = COLORS.map(c =>
        `<button type="button" class="lbl-swatch" data-color="${c.slug}" title="${c.name}">
           <span class="lbl-swatch-dot" style="background:${c.hex}"></span>
           <span class="lbl-swatch-name">${c.name}</span>
         </button>`
    ).join("");
    el.colorGroup.querySelectorAll("button").forEach(btn =>
        btn.addEventListener("click", () => setField("color", btn.dataset.color)));

    renderBrandChips();
}

function renderBrandChips() {
    const chips = state.brands.map(b =>
        `<button type="button" class="lbl-chip" data-brand="${escapeAttr(b)}">${escapeHtml(b)}</button>`
    ).join("");
    el.brandGroup.innerHTML = chips +
        `<button type="button" class="lbl-chip lbl-chip-add" id="brand-add-chip">＋ Other</button>`;

    el.brandGroup.querySelectorAll("[data-brand]").forEach(btn =>
        btn.addEventListener("click", () => setField("brand", btn.dataset.brand)));
    document.getElementById("brand-add-chip").addEventListener("click", () => {
        el.customBrandWrap.hidden = !el.customBrandWrap.hidden;
        if (!el.customBrandWrap.hidden) el.customBrandInput.focus();
    });
}

// ---------------------------------------------------------------------------
// Controls wiring (navigation, custom brand, finish)
// ---------------------------------------------------------------------------

function wireControls() {
    el.prevBtn.addEventListener("click", () => go(state.index - 1));
    el.nextBtn.addEventListener("click", () => go(state.index + 1));
    el.applyNextBtn.addEventListener("click", advance);
    el.finishBtn.addEventListener("click", finish);
    el.restartBtn.addEventListener("click", restart);
    el.labelMoreBtn.addEventListener("click", restart);
    el.redownloadBtn.addEventListener("click", () => {
        if (state.lastZip) triggerDownload(state.lastZip.blob, state.lastZip.filename);
    });

    el.customBrandAdd.addEventListener("click", addCustomBrand);
    el.customBrandInput.addEventListener("keydown", e => {
        if (e.key === "Enter") { e.preventDefault(); addCustomBrand(); }
    });

    document.addEventListener("keydown", e => {
        if (el.labelStage.hidden) return;
        const typing = document.activeElement === el.customBrandInput;
        if (typing) return;
        if (e.key === "ArrowLeft")  { go(state.index - 1); }
        if (e.key === "ArrowRight") { go(state.index + 1); }
        if (e.key === "Enter" && !el.applyNextBtn.disabled && !el.applyNextBtn.hidden) advance();
    });
}

function addCustomBrand() {
    const raw = el.customBrandInput.value.trim();
    if (!raw) return;
    // Reuse an existing brand if it matches case-insensitively.
    const existing = state.brands.find(b => b.toLowerCase() === raw.toLowerCase());
    const brand = existing || raw;
    if (!existing) state.brands.push(brand);
    el.customBrandInput.value = "";
    el.customBrandWrap.hidden = true;
    renderBrandChips();
    setField("brand", brand);
}

// ---------------------------------------------------------------------------
// Per-item state + rendering
// ---------------------------------------------------------------------------

function current() { return state.items[state.index]; }

function setField(field, value) {
    current()[field] = value;
    renderControls();
}

function go(idx) {
    if (idx < 0 || idx >= state.items.length) return;
    state.index = idx;
    render();
}

/** Move to the next unlabeled item, or reveal Finish if all are done. */
function advance() {
    const next = state.items.findIndex((it, i) => i > state.index && !isLabeled(it));
    if (next !== -1) { go(next); return; }
    const anyUnlabeled = state.items.findIndex(it => !isLabeled(it));
    if (anyUnlabeled !== -1) { go(anyUnlabeled); return; }
    finish();
}

function isLabeled(it) { return it.decision && it.color && it.brand; }

function render() {
    const it = current();
    el.viewerImg.src = it.url;
    el.viewerImg.alt = it.name;
    el.viewerPos.textContent = `${state.index + 1} / ${state.items.length}`;
    el.viewerName.textContent = it.name;
    el.viewerName.title = it.name;
    el.prevBtn.disabled = state.index === 0;
    el.nextBtn.disabled = state.index === state.items.length - 1;
    renderControls();
}

function renderControls() {
    const it = current();

    el.decisionGroup.querySelectorAll("[data-decision]").forEach(b =>
        b.classList.toggle("selected", b.dataset.decision === it.decision));
    el.colorGroup.querySelectorAll("[data-color]").forEach(b =>
        b.classList.toggle("selected", b.dataset.color === it.color));
    el.brandGroup.querySelectorAll("[data-brand]").forEach(b =>
        b.classList.toggle("selected", b.dataset.brand === it.brand));

    // Name preview (counter shown as N — resolved per combo on download)
    el.namePreview.textContent = isLabeled(it) ? previewName(it) : "—";

    // Progress
    const done = state.items.filter(isLabeled).length;
    el.progressCount.textContent = `${done} of ${state.items.length} labeled`;
    el.progressFill.style.width = `${(done / state.items.length) * 100}%`;

    // Action buttons: Finish once everything is labeled, otherwise Save & next.
    const allDone = done === state.items.length;
    el.finishBtn.hidden = !allDone;
    el.applyNextBtn.hidden = allDone;
    el.applyNextBtn.disabled = !isLabeled(it);
}

/** Filename preview for the current item (counter shown as a placeholder). */
function previewName(it) {
    const ext = extOf(it.name);
    return `shoe_${it.decision}_${it.color}_${slug(it.brand)}_N${ext}`;
}

// ---------------------------------------------------------------------------
// Finish: build renamed ZIP and download
// ---------------------------------------------------------------------------

async function finish() {
    const unlabeled = state.items.filter(it => !isLabeled(it)).length;
    if (unlabeled > 0) {
        alert(`${unlabeled} image(s) still need a decision, color, and brand.`);
        return;
    }

    el.finishBtn.disabled = true;
    el.finishBtn.textContent = "Building ZIP…";

    try {
        const zip = new JSZip();
        const counters = new Map(); // combo key → count so far
        const usedNames = new Set();

        for (const it of state.items) {
            const combo = `${it.decision}_${it.color}_${slug(it.brand)}`;
            const n = (counters.get(combo) || 0) + 1;
            counters.set(combo, n);

            let name = buildName(it, "_", String(n));
            // Guard against any residual collision (e.g. odd extensions).
            let dedupe = 1;
            const base = name.replace(IMAGE_EXT, "");
            const ext = extOf(it.name);
            while (usedNames.has(name.toLowerCase())) {
                name = `${base}_${++dedupe}${ext}`;
            }
            usedNames.add(name.toLowerCase());

            zip.file(name, it.file);
        }

        const blob = await zip.generateAsync({ type: "blob" });
        const filename = `labeled_shoes_${state.items.length}.zip`;
        state.lastZip = { blob, filename };
        triggerDownload(blob, filename);

        el.doneSub.textContent =
            `${state.items.length} image${state.items.length === 1 ? "" : "s"} renamed and packaged into ${filename}.`;
        showStage("done");
    } catch (err) {
        alert("Something went wrong building the ZIP: " + (err && err.message ? err.message : err));
    } finally {
        el.finishBtn.disabled = false;
        el.finishBtn.textContent = "Finish & download ZIP";
    }
}

/** `shoe_{decision}_{color}_{brand}{sep}{n}{ext}` */
function buildName(it, sep, n) {
    const ext = extOf(it.name);
    return `shoe_${it.decision}_${it.color}_${slug(it.brand)}${sep}${n}${ext}`;
}

function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------------------------------------------------------------------------
// Stage switching + reset
// ---------------------------------------------------------------------------

function showStage(name) {
    el.uploadStage.hidden = name !== "upload";
    el.labelStage.hidden  = name !== "label";
    el.doneStage.hidden   = name !== "done";
}

function restart() {
    revokeUrls();
    state.items = [];
    state.index = 0;
    el.fileInput.value = "";
    el.fileInputFiles.value = "";
    showStage("upload");
}

function revokeUrls() {
    state.items.forEach(it => { if (it.url) URL.revokeObjectURL(it.url); });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Filename-safe slug: lowercase, spaces/punctuation → hyphen. */
function slug(s) {
    return String(s)
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "") || "unknown";
}

/** Lowercased extension including the dot, or "" if none. */
function extOf(filename) {
    const m = filename.match(/\.[a-z0-9]+$/i);
    return m ? m[0].toLowerCase() : "";
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }
