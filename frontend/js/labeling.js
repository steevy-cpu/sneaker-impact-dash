/**
 * labeling.js — Client-side shoe image labeling tool.
 *
 * Workflow (everything stays in the browser, nothing is uploaded):
 *   1. User loads a folder / set of image files.
 *   2. They step through each image tagging it Reuse or Recycle.
 *   3. On finish, JSZip builds a ZIP with every file renamed
 *      `shoe_{decision}_{n}.{ext}` and the browser downloads it.
 *
 * The counter {n} resets per decision, so the first two reuse shoes become
 * shoe_reuse_1 and shoe_reuse_2.
 */

// ---------------------------------------------------------------------------
// Predefined options
// ---------------------------------------------------------------------------

const DECISIONS = [
    { key: "reuse",   label: "Reuse",   cls: "decision-reuse" },
    { key: "recycle", label: "Recycle", cls: "decision-recycle" },
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
    items: [],          // { file, url, name, decision }
    index: 0,
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
        "decision-group",
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
}

// ---------------------------------------------------------------------------
// Controls wiring (navigation, finish)
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

    document.addEventListener("keydown", e => {
        if (el.labelStage.hidden) return;
        if (e.key === "ArrowLeft")  { go(state.index - 1); }
        if (e.key === "ArrowRight") { go(state.index + 1); }
        if (e.key === "Enter" && !el.applyNextBtn.disabled && !el.applyNextBtn.hidden) advance();
    });
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

function isLabeled(it) { return !!it.decision; }

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

    // Name preview (counter shown as N — resolved per decision on download)
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
    return `shoe_${it.decision}_N${ext}`;
}

// ---------------------------------------------------------------------------
// Finish: build renamed ZIP and download
// ---------------------------------------------------------------------------

async function finish() {
    const unlabeled = state.items.filter(it => !isLabeled(it)).length;
    if (unlabeled > 0) {
        alert(`${unlabeled} image(s) still need a Reuse or Recycle label.`);
        return;
    }

    el.finishBtn.disabled = true;
    el.finishBtn.textContent = "Building ZIP…";

    try {
        const zip = new JSZip();
        const counters = new Map(); // decision → count so far
        const usedNames = new Set();

        for (const it of state.items) {
            const n = (counters.get(it.decision) || 0) + 1;
            counters.set(it.decision, n);

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

/** `shoe_{decision}{sep}{n}{ext}` */
function buildName(it, sep, n) {
    const ext = extOf(it.name);
    return `shoe_${it.decision}${sep}${n}${ext}`;
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

/** Lowercased extension including the dot, or "" if none. */
function extOf(filename) {
    const m = filename.match(/\.[a-z0-9]+$/i);
    return m ? m[0].toLowerCase() : "";
}
