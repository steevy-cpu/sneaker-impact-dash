/**
 * capture.js — Capture Station (full-send)
 *
 * Full-send flow: the operator enters box data (barcode + weight/good/eol/casuals)
 * in always-visible fields, then ONE press — the on-screen "Capture & Send"
 * button, the bound USB-C hardware button, or the upload fallback — photographs
 * the table and submits it straight to the processing queue (no popup).
 *
 * USB button: "Set USB button" learns whatever the hardware button emits (a key
 * or a non-primary mouse button) and stores it in localStorage; pressing it then
 * triggers a full-send. It works through this page so it reuses the already-open
 * getUserMedia camera stream (no second process grabbing the device).
 */
(function () {
    "use strict";

    const OPERATOR_ID = localStorage.getItem("operator_id") || "OP-WEB";
    const CAPTURE_DEBOUNCE_MS = 1200;
    // Hard requirement: every capture needs a scanned tracking barcode or its
    // counts can never match an Airtable shipment. FedEx = 12 digits (routing
    // scans collapse to those 12), UPS = 18 chars — 12 is the floor. The
    // server enforces the same rule (422), this is just the friendly gate.
    const MIN_BARCODE_LEN = 12;
    const TRIGGER_KEY = "capture_trigger";   // {kind:'key', code} | {kind:'mouse', button}

    const stage = document.getElementById("capture-stage");
    const video = document.getElementById("capture-video");
    const frozen = document.getElementById("capture-frozen");
    const frozenBadge = document.getElementById("frozen-badge");
    const stageMsg = document.getElementById("stage-msg");
    const canvas = document.getElementById("capture-canvas");
    const cameraLabel = document.getElementById("camera-label");

    const barcodeInput = document.getElementById("barcode-input");
    const barcodeStatus = document.getElementById("barcode-status");
    const shipmentPreview = document.getElementById("shipment-preview");
    const capWeight = document.getElementById("cap-weight");
    const capGood = document.getElementById("cap-good");
    const capEol = document.getElementById("cap-eol");
    const capCasuals = document.getElementById("cap-casuals");
    const capSingles = document.getElementById("cap-singles");
    const capNote = document.getElementById("cap-note");
    const capNoteCount = document.getElementById("cap-note-count");
    const capError = document.getElementById("cap-error");
    const captureBtn = document.getElementById("capture-btn");
    const uploadBtn = document.getElementById("upload-btn");
    const fileInput = document.getElementById("file-input");
    const learnBtn = document.getElementById("learn-btn");
    const dupModal = document.getElementById("dup-modal");
    const dupModalBody = document.getElementById("dup-modal-body");
    const dupCancelBtn = document.getElementById("dup-cancel");
    const dupOverwriteBtn = document.getElementById("dup-overwrite");
    const nobcModal = document.getElementById("nobc-modal");
    const nobcOkBtn = document.getElementById("nobc-ok");
    const todayCount = document.getElementById("today-count");
    const capTiming = document.getElementById("cap-timing");
    const buzzVolume = document.getElementById("buzz-volume");
    const buzzVolumeLabel = document.getElementById("buzz-volume-label");
    const buzzTestBtn = document.getElementById("buzz-test");
    const insoleRow = document.getElementById("insole-toggle-row");
    const insoleToggle = document.getElementById("insole-toggle");
    const insoleBadge = document.getElementById("insole-badge");
    const insoleModeLabel = document.getElementById("insole-mode-label");
    const insolePreview = document.getElementById("insole-parse-preview");

    let stream = null;
    let insoleMode = false;      // station-config-gated insole-only capture
    let uploadedBlob = null;     // set only by the upload fallback
    let lastCaptureAt = 0;
    let busy = false;
    let learning = false;
    let lastLookup = "";
    let overwriteOf = null;      // previous entry id to replace on send (duplicate label)
    let dupModalOpen = false;
    let audioCtx = null;         // lazy — created on the first buzz (a user gesture)

    /* ---- Camera --------------------------------------------------------- */

    async function startCamera() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            return cameraUnavailable("This browser can't access the camera. Use “Upload image…”.");
        }
        try {
            // 4K, not 1080p: the 48MP station camera offers 3840x2160 MJPEG at a
            // full 30 fps, and the snapshot canvas inherits the stream size — 4x
            // the pixels for detection/cloud ID at ~the same tiling cost (batched
            // tile inference absorbs the 15->60 tile jump). Full 8000x6000 exists
            // but only at 5 fps preview and ~22x the tiles — don't request it.
            //
            // `exact` first: the station's Firefox silently stays at 1080p on an
            // `ideal` 4K request (verified 2026-09-04 via v4l2 --get-fmt-video),
            // and only OverconstrainedError tells us it truly can't. The ideal
            // fallback keeps the station capturing no matter what.
            const attempts = [
                { video: { width: { exact: 3840 }, height: { exact: 2160 } }, audio: false },
                { video: { width: { ideal: 3840 }, height: { ideal: 2160 } }, audio: false },
            ];
            let lastErr = null;
            stream = null;
            for (const constraints of attempts) {
                try { stream = await navigator.mediaDevices.getUserMedia(constraints); break; }
                catch (err) { lastErr = err; }
            }
            if (!stream) throw lastErr || new Error("no stream");
            video.srcObject = stream;
            stageMsg.style.display = "none";
            const track = stream.getVideoTracks()[0];
            if (track && track.label) {
                // Show the NEGOTIATED resolution so a silent fallback is visible
                // on the page itself (and in any screenshot the floor sends us).
                const s = track.getSettings ? track.getSettings() : {};
                cameraLabel.textContent = track.label
                    + (s.width ? " — " + s.width + "x" + s.height : "");
            }
        } catch (err) {
            cameraUnavailable("Camera unavailable (" + ((err && err.name) || "error") + "). Use “Upload image…”.");
        }
    }
    function cameraUnavailable(msg) {
        stageMsg.textContent = msg; stageMsg.style.display = "flex"; cameraLabel.textContent = "No camera";
    }

    /* ---- Box data ------------------------------------------------------- */

    function readBox() {
        return {
            weight: parseFloat(capWeight.value) || 0,
            good: parseInt(capGood.value, 10) || 0,
            eol: parseInt(capEol.value, 10) || 0,
            casuals: parseInt(capCasuals.value, 10) || 0,
            singles: parseInt(capSingles.value, 10) || 0,
            // Optional — a note alone is NOT box data, so validate() ignores it.
            // Insole counts for combined boxes live IN the note ("25 pair
            // currex"); the server extracts them, the preview shows them.
            note: capNote.value.trim(),
        };
    }
    function validate(d) {
        // Barcode first, BOTH modes: without a tracking number the box can
        // never match its Airtable shipment, so the capture is refused with a
        // buzz + modal the worker can't miss.
        if (barcodeInput.value.trim().length < MIN_BARCODE_LEN) {
            capError.textContent = "⚠️ Scan or enter the box barcode first (min " + MIN_BARCODE_LEN + " chars)";
            capError.style.display = "";
            showNobcModal();
            return false;
        }
        // Insole mode: counts fields are hidden, the barcode was the only rule.
        if (insoleMode) {
            capError.style.display = "none";
            return true;
        }
        if (d.weight <= 0 && d.good <= 0 && d.eol <= 0 && d.casuals <= 0 && d.singles <= 0) {
            capError.textContent = "⚠️ Enter box data first — at least ONE field must be greater than 0";
            capError.style.display = "";
            return false;
        }
        capError.style.display = "none";
        return true;
    }
    function updateNoteCount() {
        capNoteCount.textContent = String(capNote.value.length);
    }
    capNote.addEventListener("input", updateNoteCount);

    /* ---- Insole counts detected in the NOTE (combined boxes) ------------- */
    // Lenient in-browser extraction (js/insole_parse.js mirror) on each
    // keystroke — microseconds on a short note, no network, can't slow the
    // page. Purely informational: shows what the server WILL record, shows
    // nothing when no counts are found, and never blocks the send.
    function updateInsolePreview() {
        if (!insolePreview) return;
        const txt = (insoleTextEnabled && !insoleMode) ? capNote.value.trim() : "";
        const found = txt ? window.extractInsoleCounts(txt) : null;
        if (found) {
            const bits = [];
            for (const [brand, label] of [["currex", "Currex"], ["superfeet", "Superfeet"]]) {
                const c = found[brand];
                if (c) bits.push(`${label}: ${c[0]} pair${c[0] === 1 ? "" : "s"}, ${c[1]} single${c[1] === 1 ? "" : "s"}`);
            }
            insolePreview.textContent = "🦶 Insoles detected — " + bits.join(" · ");
            insolePreview.className = "insole-parse-preview ok";
        } else {
            insolePreview.textContent = "";
            insolePreview.className = "insole-parse-preview";
        }
    }
    capNote.addEventListener("input", updateInsolePreview);

    function resetFields() {
        capWeight.value = ""; capGood.value = ""; capEol.value = ""; capCasuals.value = ""; capSingles.value = "";
        capNote.value = ""; updateNoteCount(); updateInsolePreview();
        barcodeInput.value = "";
        barcodeStatus.textContent = "Ready for barcode scanning…";
        shipmentPreview.style.display = "none"; lastLookup = ""; overwriteOf = null;
        uploadedBlob = null; fileInput.value = "";
        stage.classList.remove("frozen"); frozenBadge.style.display = "none"; frozen.removeAttribute("src");
    }

    /* ---- "Total boxes today" counter ------------------------------------ */
    // Purely informational for the floor: refreshed on load, after every send,
    // and once a minute (tab-visible only). Best-effort — a failed fetch just
    // leaves the last number, never touches the capture flow.
    async function refreshTodayCount() {
        try {
            const r = await fetch("/api/capture-stats/today");
            if (!r.ok) return;
            const d = await r.json();
            todayCount.textContent = d.tables_today;
            const t = d.timing;
            if (t && capTiming) {
                const fmt = v => (v == null ? "–" : v.toFixed(2) + " sec");
                const since = t.since
                    ? " (since " + new Date(t.since + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) + ")"
                    : "";
                // "Average" = active average: gaps over 60 s (pauses) are excluded
                // on the server; the raw all-time mean is in the tooltip.
                const avg = t.active_avg_sec != null ? t.active_avg_sec : t.avg_sec;
                const lastAt = t.last_at
                    ? " (" + new Date(t.last_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) + ")"
                    : "";
                capTiming.textContent = "Last capture: " + fmt(t.last_sec) + lastAt
                    + " · Average: " + fmt(avg) + since + " · Captures: " + t.captures;
                capTiming.title = "Average counts only gaps of " + Math.round(t.active_cutoff_sec || 60)
                    + " s or less (" + (t.active_excluded || 0) + " longer pause(s) excluded). Raw all-time mean: " + fmt(t.avg_sec);
            }
        } catch (e) { /* cosmetic only */ }
    }
    setInterval(() => { if (!document.hidden) refreshTodayCount(); }, 60000);

    /* ---- Grab a still --------------------------------------------------- */

    function grabVideoBlob() {
        return new Promise((resolve) => {
            if (!stream || !video.videoWidth) return resolve(null);
            canvas.width = video.videoWidth; canvas.height = video.videoHeight;
            canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
            frozen.src = canvas.toDataURL("image/jpeg", 0.9);
            canvas.toBlob(b => resolve(b), "image/jpeg", 0.92);
        });
    }

    /* ---- Full send (the whole capture in one action) -------------------- */

    async function fullSend() {
        const now = Date.now();
        if (busy || now - lastCaptureAt < CAPTURE_DEBOUNCE_MS) return;
        const d = readBox();
        if (!validate(d)) return;   // validate() shows its own feedback (inline error / barcode modal)
        const blob = uploadedBlob || await grabVideoBlob();
        if (!blob) { showToast("NO CAMERA — use “Upload image…”", "error"); return; }

        lastCaptureAt = now; busy = true;
        captureBtn.disabled = true;
        const label = captureBtn.textContent; captureBtn.textContent = "Sending…";
        stage.classList.add("frozen"); frozenBadge.style.display = "";

        const fd = new FormData();
        fd.append("image", blob, "table.jpg");
        fd.append("barcode", barcodeInput.value.trim());
        fd.append("weight_of_box", String(d.weight));
        fd.append("total_good_sneakers", String(insoleMode ? 0 : d.good));
        fd.append("total_end_of_life", String(insoleMode ? 0 : d.eol));
        fd.append("casuals", String(insoleMode ? 0 : d.casuals));
        fd.append("singles", String(insoleMode ? 0 : d.singles));
        fd.append("capture_mode", insoleMode ? "insoles" : "shoes");
        if (d.note) fd.append("notes", d.note);
        fd.append("operator_id", OPERATOR_ID);
        if (overwriteOf) fd.append("overwrite_of", overwriteOf);
        try {
            const res = await api.captureTablePhoto(fd);
            showToast("✅ QUEUED — " + res.id, "success", 2600);
            resetFields();
            barcodeInput.focus();
            refreshTodayCount();
        } catch (err) {
            // Duplicate-label backstop: the server refused the capture because
            // this barcode already exists (typed without Enter, scan-check
            // missed, or another station won a race). Same buzz + modal as a
            // scan-time hit; OVERWRITE re-sends with overwrite_of.
            if (err.status === 409 && err.detail && err.detail.code === "duplicate_barcode") {
                playBuzz();
                showDupModal(err.detail.existing, err.detail.match_count, {
                    onOverwrite: () => {
                        overwriteOf = err.detail.existing.id;
                        lastCaptureAt = 0;          // bypass the debounce for the resend
                        fullSend();
                    },
                    onCancel: () => { stage.classList.remove("frozen"); frozenBadge.style.display = "none"; },
                });
                return;
            }
            showToast(err.message || "Capture failed", "error", 3200);
            stage.classList.remove("frozen"); frozenBadge.style.display = "none";
        } finally {
            busy = false; captureBtn.disabled = false; captureBtn.textContent = label;
        }
    }

    function useUploadedFile(file) {
        if (!file || !file.type.startsWith("image/")) { showToast("Please choose an image file", "error"); return; }
        uploadedBlob = file;
        frozen.src = URL.createObjectURL(file);
        stage.classList.add("frozen"); frozenBadge.style.display = "";
        showToast("Image ready — fill box data, then Capture & Send", "info", 2400);
    }

    /* ---- Barcode (USB scanner / manual) --------------------------------- */

    async function lookupShipment(code) {
        if (!code || code.length < 4 || code === lastLookup) return;
        lastLookup = code;
        shipmentPreview.style.display = "";
        shipmentPreview.className = "shipment-preview shipment-preview--loading";
        shipmentPreview.textContent = "Looking up shipment…";
        try {
            const s = await api.getShipment(code);
            if (s.found) {
                const bits = [];
                if (s.partner) bits.push("📦 " + s.partner);
                if (s.weight != null) bits.push(s.weight + " lbs");
                if (s.status) bits.push(s.status);
                shipmentPreview.className = "shipment-preview shipment-preview--found";
                shipmentPreview.textContent = "Shipment: " + (bits.join(" · ") || "matched");
                // Insole mode: the operator doesn't weigh the box — reuse the
                // shipment's known weight. Their own typing always wins.
                if (insoleMode && s.weight != null && capWeight.value.trim() === "") {
                    capWeight.value = s.weight;
                    capWeight.style.transition = "background-color 0.2s";
                    capWeight.style.backgroundColor = "rgba(255, 200, 60, 0.25)";
                    setTimeout(() => { capWeight.style.backgroundColor = ""; }, 1500);
                }
            } else if (s.configured === false) {
                shipmentPreview.style.display = "none";
            } else {
                shipmentPreview.className = "shipment-preview shipment-preview--none";
                shipmentPreview.textContent = "No shipment match for this barcode";
            }
        } catch (err) { shipmentPreview.style.display = "none"; }
    }
    /* ---- Duplicate-label guard (buzz + overwrite/cancel modal) ----------- */

    // Alert volume: per-station (localStorage), 0-100%. 50% == the original
    // fixed gain of 0.25; 100% doubles it. 0% mutes the buzz (modal still shows).
    const VOLUME_KEY = "dup_buzz_volume";
    function getBuzzVolume() {
        const v = parseInt(localStorage.getItem(VOLUME_KEY), 10);
        return Number.isFinite(v) ? Math.min(100, Math.max(0, v)) : 50;
    }
    function refreshVolumeUI() {
        const v = getBuzzVolume();
        buzzVolume.value = v; buzzVolumeLabel.textContent = v + "%";
    }
    buzzVolume.addEventListener("input", () => {
        localStorage.setItem(VOLUME_KEY, buzzVolume.value);
        buzzVolumeLabel.textContent = buzzVolume.value + "%";
    });
    buzzVolume.addEventListener("change", () => playBuzz());  // hear the level you just set
    buzzTestBtn.addEventListener("click", () => playBuzz());

    // Double error-buzz via Web Audio — no asset file needed. Best-effort: the
    // scanner's Enter keydown is a user gesture, so the AudioContext may start.
    function playBuzz() {
        try {
            const gain = (getBuzzVolume() / 100) * 0.5;
            if (gain <= 0) return;                     // muted — the modal still shows
            audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
            if (audioCtx.state === "suspended") audioCtx.resume();
            const t0 = audioCtx.currentTime;
            [0, 0.22].forEach((off) => {
                const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
                osc.type = "square"; osc.frequency.value = 220;
                g.gain.setValueAtTime(gain, t0 + off);
                g.gain.exponentialRampToValueAtTime(0.001, t0 + off + 0.15);
                osc.connect(g); g.connect(audioCtx.destination);
                osc.start(t0 + off); osc.stop(t0 + off + 0.16);
            });
        } catch (e) { /* audio is best-effort — the modal still shows */ }
    }

    /* ---- Missing-barcode modal (hard requirement) ------------------------ */

    let nobcOpen = false;

    function showNobcModal() {
        playBuzz();
        nobcOpen = true;
        nobcModal.classList.add("open");
        nobcModal.focus();      // focus the overlay so a held Space can't press anything
    }
    function closeNobcModal() {
        nobcOpen = false;
        nobcModal.classList.remove("open");
        barcodeInput.focus();   // straight back to scanning
    }
    nobcOkBtn.addEventListener("click", closeNobcModal);
    // A scanner burst ends in Enter — just close and refocus so the NEXT scan
    // lands in the barcode field; Escape closes too.
    document.addEventListener("keydown", (e) => {
        if (!nobcOpen) return;
        if (e.key === "Enter" || e.key === "Escape") {
            e.preventDefault(); e.stopPropagation(); closeNobcModal();
        }
    }, true);

    let dupHandlers = null;     // {onOverwrite, onCancel} for the open modal

    function showDupModal(existing, matchCount, handlers) {
        dupHandlers = handlers;
        const bits = [];
        if (existing.weight_of_box != null) bits.push(existing.weight_of_box + " lbs");
        if (existing.total_good_sneakers) bits.push(existing.total_good_sneakers + " good");
        if (existing.total_end_of_life) bits.push(existing.total_end_of_life + " EOL");
        if (existing.casuals) bits.push(existing.casuals + " casuals");
        if (existing.singles) bits.push(existing.singles + " singles");
        if (existing.num_pairs) bits.push(existing.num_pairs + " pairs");
        dupModalBody.innerHTML =
            '<p style="margin:0 0 10px;">This label was already captured as ' +
            "<strong>" + existing.id + "</strong> (" + formatDate(existing.created_at) +
            (existing.status ? " · " + existing.status : "") + ").</p>" +
            (bits.length ? '<p style="margin:0 0 10px;">Previous box data: ' + bits.join(" · ") + "</p>" : "") +
            (matchCount > 1
                ? '<p style="margin:0 0 10px;">(+' + (matchCount - 1) + " older entr" +
                  (matchCount - 1 === 1 ? "y" : "ies") + " with this label)</p>" : "") +
            '<p style="margin:0; color:var(--text-muted); font-size:var(--text-sm);">' +
            "OVERWRITE deletes that entry (photo + pairs) and replaces it with this capture. " +
            "Cancel clears the barcode.</p>";
        dupModalOpen = true;
        dupModal.classList.add("open");
        dupModal.focus();       // focus the overlay, NOT a button — Space can't press anything
    }

    function closeDupModal() {
        dupModalOpen = false; dupHandlers = null;
        dupModal.classList.remove("open");
    }

    // Shared cancel path: forget the scan entirely and return to a clean field.
    function dupCancel() {
        const h = dupHandlers;
        closeDupModal();
        barcodeInput.value = ""; overwriteOf = null; lastLookup = "";
        shipmentPreview.style.display = "none";
        barcodeStatus.textContent = "Ready for barcode scanning…";
        barcodeInput.focus();
        if (h && h.onCancel) h.onCancel();
    }
    function dupOverwrite() {
        const h = dupHandlers;
        closeDupModal();
        if (h && h.onOverwrite) h.onOverwrite();
    }
    dupCancelBtn.addEventListener("click", dupCancel);
    dupOverwriteBtn.addEventListener("click", dupOverwrite);

    // Keyboard safety while the modal is open: a scanner re-scan ends in Enter,
    // so Enter is swallowed ENTIRELY (overwrite is click-only); Escape cancels.
    document.addEventListener("keydown", (e) => {
        if (!dupModalOpen) return;
        if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); return; }
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); dupCancel(); }
    }, true);

    // On OVERWRITE, load the previous entry's box data into any field the
    // worker hasn't already typed in — they only correct what changed instead
    // of retyping everything. Filled fields flash briefly so it's obvious
    // which values came from the old entry.
    function prefillFromPrevious(existing) {
        const map = [
            [capWeight, existing.weight_of_box],
            [capGood, existing.total_good_sneakers],
            [capEol, existing.total_end_of_life],
            [capCasuals, existing.casuals],
            [capSingles, existing.singles],
            [capNote, existing.notes],
        ];
        let filled = 0;
        for (const [input, value] of map) {
            if (input.value.trim() !== "") continue;   // worker's own typing wins
            if (value == null || value === "") continue;
            input.value = value;
            filled++;
            input.style.transition = "background-color 0.2s";
            input.style.backgroundColor = "rgba(255, 200, 60, 0.25)";
            setTimeout(() => { input.style.backgroundColor = ""; }, 1500);
        }
        capNoteCount.textContent = String(capNote.value.length);
        return filled;
    }

    // Scan-time duplicate check — fire-and-forget so it never delays the flow;
    // a failed check is harmless (the server's 409 backstop still protects).
    async function checkDuplicate(code) {
        try {
            const r = await api.checkBarcode(code);
            if (!r.duplicate) return;
            // Stale guard: the field changed (new scan/cleared) while we waited.
            if (barcodeInput.value.trim() !== code || dupModalOpen) return;
            playBuzz();
            showDupModal(r.matches[0], r.matches.length, {
                onOverwrite: () => {
                    overwriteOf = r.matches[0].id;
                    const filled = prefillFromPrevious(r.matches[0]);
                    showToast(filled
                        ? "Previous data loaded — change what's needed, then capture (overwrites " + r.matches[0].id + ")"
                        : "Will OVERWRITE " + r.matches[0].id + " on send",
                        "info", 3200);
                    capWeight.focus();
                    capWeight.select();   // typing replaces the loaded value outright
                },
                onCancel: () => {},
            });
        } catch (e) { /* never block scanning on a failed check */ }
    }

    barcodeInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            if (dupModalOpen) return;          // double-scan while the modal is up
            const code = barcodeInput.value.trim();
            if (code.length >= MIN_BARCODE_LEN) {
                barcodeStatus.textContent = "Scanned: " + code;
                showToast("BARCODE SCANNED: " + code, "barcode", 1500);
                lookupShipment(code);
                checkDuplicate(code);
                capWeight.focus();         // after a scan, jump straight to Weight; Enter/Tab walks the rest
            } else {
                barcodeStatus.textContent = "Barcode too short (min " + MIN_BARCODE_LEN + " chars — scan the FedEx/UPS label)";
            }
        }
    });
    barcodeInput.addEventListener("input", () => {
        const code = barcodeInput.value.trim();
        barcodeStatus.textContent = code ? "Barcode: " + code : "Ready for barcode scanning…";
        overwriteOf = null;                    // a pending overwrite dies with a changed code
        if (!code) { shipmentPreview.style.display = "none"; lastLookup = ""; }
    });
    barcodeInput.addEventListener("blur", () => lookupShipment(barcodeInput.value.trim()));

    /* ---- USB hardware button: learn + trigger --------------------------- */

    function getTrigger() {
        try { return JSON.parse(localStorage.getItem(TRIGGER_KEY) || "null"); } catch (e) { return null; }
    }
    function triggerLabel() {
        const t = getTrigger();
        if (!t) return "not set";
        return t.kind === "key" ? ("key " + t.code) : ("mouse btn " + t.button);
    }
    function refreshHint() { learnBtn.textContent = "🔘 USB button: " + triggerLabel(); }
    function setTrigger(t) { localStorage.setItem(TRIGGER_KEY, JSON.stringify(t)); refreshHint(); }

    function inField() {
        const tag = (document.activeElement && document.activeElement.tagName) || "";
        return tag === "INPUT" || tag === "TEXTAREA";
    }
    const MODIFIERS = ["ShiftLeft","ShiftRight","ControlLeft","ControlRight",
                       "AltLeft","AltRight","MetaLeft","MetaRight"];

    learnBtn.addEventListener("click", () => {
        learning = true;
        learnBtn.textContent = "Press the USB button now…";
        showToast("Press the USB-C button now to bind it", "info", 4000);
    });

    window.addEventListener("keydown", (e) => {
        if (dupModalOpen || nobcOpen) return;   // no trigger/learning while a modal is up
        if (learning) {
            if (MODIFIERS.includes(e.code)) return;      // ignore a held modifier
            e.preventDefault();
            setTrigger({ kind: "key", code: e.code });
            learning = false;
            showToast("✅ USB button bound to key: " + e.code, "success", 2800);
            return;
        }
        const t = getTrigger();
        if (!t || t.kind !== "key" || e.code !== t.code) return;
        // Don't hijack the barcode field's Enter (used to confirm a scan).
        if (e.code === "Enter" && document.activeElement === barcodeInput) return;
        // For ordinary printable keys, only fire when NOT typing in a field (so a
        // digit-bound button doesn't capture mid-count). Function/media keys fire
        // regardless of focus — ideal for a hands-free hardware button.
        if (e.key && e.key.length === 1 && inField()) return;
        e.preventDefault();
        fullSend();
    }, true);

    window.addEventListener("mousedown", (e) => {
        if (dupModalOpen || nobcOpen) return;   // modal buttons are plain left-clicks; no trigger can fire
        if (learning) {
            if (e.button === 0) {
                showToast("That's a normal left-click — the button likely sends a key. Try again.", "error", 3800);
                return;
            }
            e.preventDefault();
            setTrigger({ kind: "mouse", button: e.button });
            learning = false;
            showToast("✅ USB button bound to mouse button " + e.button, "success", 2800);
            return;
        }
        const t = getTrigger();
        if (t && t.kind === "mouse" && e.button === t.button) { e.preventDefault(); fullSend(); }
    }, true);

    window.addEventListener("contextmenu", (e) => {
        const t = getTrigger();
        if (learning || (t && t.kind === "mouse" && t.button === 2)) e.preventDefault();
    });

    /* ---- Enter/Tab advance through the box-data fields ------------------ */
    // After the barcode scan drops focus on Weight, Enter (or Tab) walks the
    // worker down Weight → Good → End of Life → Casuals → Singles without
    // reaching for the mouse. Enter past the last field just blurs (it does NOT
    // auto-send — sending is the deliberate USB-button / Capture & Send press).
    const BOX_ORDER = [capWeight, capGood, capEol, capCasuals, capSingles];
    BOX_ORDER.forEach((el, i) => {
        el.addEventListener("keydown", (e) => {
            if (e.key !== "Enter" || dupModalOpen) return;   // Tab is handled natively by the browser
            e.preventDefault();
            // skip fields hidden by insole mode (offsetParent is null)
            const next = BOX_ORDER.slice(i + 1).find(f => f.offsetParent !== null);
            if (next) next.focus();
            else el.blur();
        });
    });

    /* ---- Insole mode (feature-flagged via station config) ---------------- */
    // The toggle row stays hidden unless the shared station config sets
    // insole_mode — so nothing changes for the floor until it's switched on.
    // The toggle itself is session-sticky (NOT reset after a send): insole
    // boxes arrive in batches, and re-flipping per box invites mistakes.

    const SHOE_COUNT_FIELDS = [capGood, capEol, capCasuals, capSingles];

    let insoleTextEnabled = false;   // station-config flag: insole_text_field

    function applyInsoleMode() {
        insoleMode = insoleToggle.checked;
        SHOE_COUNT_FIELDS.forEach((el) => {
            const cell = el.closest(".box-field");
            if (cell) cell.style.display = insoleMode ? "none" : "";
            if (insoleMode) el.value = "";
        });
        insoleBadge.style.display = insoleMode ? "" : "none";
        insoleModeLabel.textContent = insoleMode ? "🦶 Insoles" : "👟 Shoes";
        // Notes-extraction preview is for insoles in a SHOE box; in insole-only
        // mode the engine counts, so the preview goes quiet.
        updateInsolePreview();
        capError.style.display = "none";
        barcodeInput.focus();
    }
    insoleToggle.addEventListener("change", applyInsoleMode);

    async function initInsoleFlag() {
        try {
            const cfg = await api.getStationConfig();
            if (cfg && cfg.insole_mode) insoleRow.style.display = "flex";
            if (cfg && cfg.insole_text_field) {
                insoleTextEnabled = true;   // enables the notes-extraction preview
                updateInsolePreview();
            }
        } catch (e) { /* flag off / config unreachable -> feature stays hidden */ }
    }

    /* ---- Live shoe-box guide (SAM3 preview overlay) --------------------- */
    // Server-gated (PREVIEW_SEGMENT_ENABLED): one probe at load; if off, none
    // of this runs and the overlay stays hidden. When on: every ~2.5s send a
    // downscaled frame to /api/capture-preview and draw the returned boxes.
    // Everything here is fail-safe and self-pacing -- a slow reply just delays
    // the next tick (no overlap) and an error clears the overlay. The guide
    // stays ON while tables process (shared SAM3 worker); ticks are just
    // slower then (~10s) because the engine shares the GPU.

    const previewOverlay = document.getElementById("preview-overlay");
    const previewBadge = document.getElementById("preview-badge");
    // Adaptive cadence (2026-09-21): FAST while someone is arranging shoes
    // (detections changed within the last PREVIEW_STATIC_MS), SLOW while a
    // table is processing (GPU belongs to the engine) or the scene has been
    // static -- so the GPU only runs hot in the moments the feedback matters.
    // The loop is self-pacing (next frame only after the previous reply), so
    // FAST effectively means "as fast as SAM3 answers", ~1 update/s.
    const PREVIEW_TICK_FAST_MS = 300;
    const PREVIEW_TICK_SLOW_MS = 2500;
    const PREVIEW_STATIC_MS = 30000;
    const PREVIEW_SEND_W = 1280;         // downscale width sent to the server
    let previewEnabled = false;
    let previewQueueBusy = false;        // server said a table is processing/pending
    let previewLastChangeAt = 0;         // last time the detections changed
    let previewLastSig = "";             // coarse signature of the last result

    function previewSignature(res) {
        // Coarse: 8px grid + status, so camera noise doesn't count as "change".
        const pl = res.placement;
        const items = pl && pl.objects
            ? pl.objects.map(o => o.placement_status[0] + (o.bbox[0] >> 3) + "," + (o.bbox[1] >> 3) + "," + (o.bbox[2] >> 3) + "," + (o.bbox[3] >> 3))
            : res.boxes.map(b => (b.x1 >> 3) + "," + (b.y1 >> 3) + "," + (b.x2 >> 3) + "," + (b.y2 >> 3));
        return items.sort().join("|");
    }
    function previewNextDelay() {
        const staticFor = Date.now() - previewLastChangeAt;
        return (previewQueueBusy || staticFor > PREVIEW_STATIC_MS)
            ? PREVIEW_TICK_SLOW_MS : PREVIEW_TICK_FAST_MS;
    }

    function previewSetBadge(text) {
        previewBadge.textContent = text;
        previewBadge.style.display = text ? "" : "none";
    }
    function previewClear() {
        const ctx = previewOverlay.getContext("2d");
        ctx.clearRect(0, 0, previewOverlay.width, previewOverlay.height);
    }

    function previewDraw(res) {
        // Map source-frame coords onto the displayed video, which is
        // object-fit:contain inside the stage -- compute the letterboxed rect.
        const vw = video.videoWidth, vh = video.videoHeight;
        if (!vw || !vh) return;
        const cw = previewOverlay.clientWidth, ch = previewOverlay.clientHeight;
        previewOverlay.width = cw; previewOverlay.height = ch;
        const scale = Math.min(cw / vw, ch / vh);
        const dw = vw * scale, dh = vh * scale;
        const ox = (cw - dw) / 2, oy = (ch - dh) / 2;
        const fx = dw / res.width, fy = dh / res.height;
        const ctx = previewOverlay.getContext("2d");
        ctx.clearRect(0, 0, cw, ch);
        const pl = res.placement;
        if (pl && pl.objects) {
            drawPlacement(ctx, pl, res, ox, oy, fx, fy);
        } else {
            ctx.lineWidth = 2;
            ctx.strokeStyle = "#ffd166";
            for (const b of res.boxes) {
                ctx.strokeRect(ox + b.x1 * fx, oy + b.y1 * fy,
                               (b.x2 - b.x1) * fx, (b.y2 - b.y1) * fy);
            }
        }
        // Shared-worker design: the guide stays on during processing, just
        // refreshing slower (~10s) while the engine shares the GPU -- say so.
        const ready = pl && pl.table ? "READY: " + pl.table.ready + " / " + pl.table.total
                                     : "👟 " + res.boxes.length + " shoes seen";
        previewSetBadge(ready + (pl && pl.stale ? " · holding" : "")
                        + (res.queue ? " · processing" : ""));
        updatePlacementPanel(pl);
    }

    /* ---- Live Placement Validation (overlay + readiness panel) ---------- */
    // The server's placement block is the single source of truth (see
    // backend/placement/). This layer ONLY draws it. It lives on the overlay
    // canvas -- the saved capture frame (captureFrame -> canvas.toBlob) is
    // taken from the raw <video>, so placement graphics never reach a scan.
    const PLACEMENT_STYLE = {
        ready:      { color: "#22c55e", label: "READY" },
        too_close:  { color: "#ef4444", label: "TOO CLOSE" },
        center_bar: { color: "#eab308", label: "CENTER BAR" },
    };
    const placementPanel = document.getElementById("placement-panel");
    window.placementState = null;     // latest server result, for a future scan gate

    function drawPlacement(ctx, pl, res, ox, oy, fx, fy) {
        // restricted zones (translucent) + station split line
        for (const z of pl.zones || []) {
            ctx.beginPath();
            z.polygon.forEach(([x, y], i) => {
                const px = ox + x * fx, py = oy + y * fy;
                if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
            });
            ctx.closePath();
            ctx.fillStyle = "rgba(234,179,8,0.18)";
            ctx.fill();
            ctx.strokeStyle = "rgba(234,179,8,0.7)";
            ctx.lineWidth = 1;
            ctx.stroke();
        }
        if (pl.station_split_x != null) {
            const sx = ox + pl.station_split_x * fx;
            ctx.setLineDash([6, 6]);
            ctx.strokeStyle = "rgba(255,255,255,0.35)";
            ctx.beginPath(); ctx.moveTo(sx, oy); ctx.lineTo(sx, oy + res.height * fy); ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = "rgba(255,255,255,0.7)";
            ctx.font = "bold 9px system-ui, sans-serif";
            ctx.fillText("A", sx - 12, oy + 11);
            ctx.fillText("B", sx + 5, oy + 11);
        }
        // objects
        ctx.font = "bold 8px system-ui, sans-serif";     // small: don't hide the shoes
        for (const o of pl.objects) {
            const st = PLACEMENT_STYLE[o.placement_status] || PLACEMENT_STYLE.ready;
            const [x1, y1, x2, y2] = o.bbox;
            const X = ox + x1 * fx, Y = oy + y1 * fy, W = (x2 - x1) * fx, H = (y2 - y1) * fy;
            ctx.lineWidth = o.placement_status === "ready" ? 1.5 : 2.5;
            ctx.strokeStyle = st.color;
            ctx.strokeRect(X, Y, W, H);
            if (o.placement_status !== "ready") {
                const label = st.label;
                const tw = ctx.measureText(label).width + 6;
                const ly = Y - 10 < oy ? Y + H : Y - 10;
                ctx.fillStyle = st.color;
                ctx.fillRect(X, ly, tw, 10);
                ctx.fillStyle = "#111";
                ctx.fillText(label, X + 3, ly + 8);
            }
        }
    }

    function updatePlacementPanel(pl) {
        window.placementState = pl || null;
        if (!placementPanel) return;
        if (!pl || !pl.stations) { placementPanel.style.display = "none"; return; }
        placementPanel.style.display = "";
        const cards = { A: pl.stations.A, B: pl.stations.B, table: pl.table };
        for (const card of placementPanel.querySelectorAll(".placement-card")) {
            const s = cards[card.dataset.station];
            if (!s) continue;
            card.querySelector(".placement-ready").textContent = "READY: " + s.ready + " / " + s.total;
            const v = card.querySelector(".placement-verdict");
            if (s.ready_to_scan) {
                v.textContent = "✓ READY TO SCAN";
                v.style.color = "#22c55e";
                card.style.borderColor = "rgba(34,197,94,0.6)";
            } else {
                v.textContent = "PLACEMENT ISSUES: " + s.issues;
                v.style.color = "#ef4444";
                card.style.borderColor = "rgba(239,68,68,0.6)";
            }
        }
    }

    async function previewTick() {
        if (document.hidden || !stream || busy || dupModalOpen || nobcOpen) return;
        if (stage.classList.contains("frozen")) { previewClear(); previewSetBadge(""); updatePlacementPanel(null); return; }
        if (!video.videoWidth) return;
        try {
            const w = Math.min(PREVIEW_SEND_W, video.videoWidth);
            const h = Math.round(video.videoHeight * (w / video.videoWidth));
            const c = document.createElement("canvas");
            c.width = w; c.height = h;
            c.getContext("2d").drawImage(video, 0, 0, w, h);
            const blob = await new Promise(r => c.toBlob(r, "image/jpeg", 0.7));
            if (!blob) return;
            const fd = new FormData();
            fd.append("frame", blob, "frame.jpg");
            const resp = await fetch("/api/capture-preview", { method: "POST", body: fd });
            if (!resp.ok) return;
            const res = await resp.json();
            if (!res.ok || !res.boxes) {
                // Worker warming up (first frame loads the model) or down.
                previewSetBadge("guide warming up…");
                return;
            }
            previewQueueBusy = !!res.queue;
            const sig = previewSignature(res);
            if (sig !== previewLastSig) { previewLastSig = sig; previewLastChangeAt = Date.now(); }
            previewDraw(res);
        } catch (e) { /* cosmetic feature -- never interfere with capture */ }
    }

    async function initPreviewGuide() {
        try {
            const r = await fetch("/api/capture-preview");
            if (!r.ok) return;
            const d = await r.json();
            if (!d.enabled) return;
            previewEnabled = true;
            previewOverlay.style.display = "";
            previewSetBadge("guide starting…");
            // Self-pacing loop: next tick is scheduled only after this one
            // fully finishes, so slow replies can never stack up requests.
            previewLastChangeAt = Date.now();     // start FAST: someone just opened the page
            (async function loop() {
                while (previewEnabled) {
                    await previewTick();
                    await new Promise(r => setTimeout(r, previewNextDelay()));
                }
            })();
        } catch (e) { /* server unreachable -> guide stays hidden */ }
    }

    /* ---- Wiring --------------------------------------------------------- */

    captureBtn.addEventListener("click", fullSend);
    uploadBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", (e) => useUploadedFile(e.target.files[0]));

    window.addEventListener("beforeunload", () => { if (stream) stream.getTracks().forEach(t => t.stop()); });

    refreshHint();
    refreshVolumeUI();
    refreshTodayCount();
    initInsoleFlag();
    initPreviewGuide();
    startCamera();
})();
