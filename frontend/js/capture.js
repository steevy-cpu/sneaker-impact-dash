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
    const capError = document.getElementById("cap-error");
    const captureBtn = document.getElementById("capture-btn");
    const uploadBtn = document.getElementById("upload-btn");
    const fileInput = document.getElementById("file-input");
    const learnBtn = document.getElementById("learn-btn");
    const dupModal = document.getElementById("dup-modal");
    const dupModalBody = document.getElementById("dup-modal-body");
    const dupCancelBtn = document.getElementById("dup-cancel");
    const dupOverwriteBtn = document.getElementById("dup-overwrite");
    const buzzVolume = document.getElementById("buzz-volume");
    const buzzVolumeLabel = document.getElementById("buzz-volume-label");
    const buzzTestBtn = document.getElementById("buzz-test");

    let stream = null;
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
            stream = await navigator.mediaDevices.getUserMedia({
                video: { width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false,
            });
            video.srcObject = stream;
            stageMsg.style.display = "none";
            const track = stream.getVideoTracks()[0];
            if (track && track.label) cameraLabel.textContent = track.label;
        } catch (err) {
            cameraUnavailable("Camera unavailable (" + (err.name || "error") + "). Use “Upload image…”.");
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
        };
    }
    function validate(d) {
        if (d.weight <= 0 && d.good <= 0 && d.eol <= 0 && d.casuals <= 0) {
            capError.textContent = "⚠️ Enter box data first — at least ONE field must be greater than 0";
            capError.style.display = "";
            return false;
        }
        capError.style.display = "none";
        return true;
    }
    function resetFields() {
        capWeight.value = ""; capGood.value = ""; capEol.value = ""; capCasuals.value = "";
        barcodeInput.value = "";
        barcodeStatus.textContent = "Ready for barcode scanning…";
        shipmentPreview.style.display = "none"; lastLookup = ""; overwriteOf = null;
        uploadedBlob = null; fileInput.value = "";
        stage.classList.remove("frozen"); frozenBadge.style.display = "none"; frozen.removeAttribute("src");
    }

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
        if (!validate(d)) { showToast("Enter box data first", "error", 1800); return; }
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
        fd.append("total_good_sneakers", String(d.good));
        fd.append("total_end_of_life", String(d.eol));
        fd.append("casuals", String(d.casuals));
        fd.append("operator_id", OPERATOR_ID);
        if (overwriteOf) fd.append("overwrite_of", overwriteOf);
        try {
            const res = await api.captureTablePhoto(fd);
            showToast("✅ QUEUED — " + res.id, "success", 2600);
            resetFields();
            barcodeInput.focus();
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

    let dupHandlers = null;     // {onOverwrite, onCancel} for the open modal

    function showDupModal(existing, matchCount, handlers) {
        dupHandlers = handlers;
        const bits = [];
        if (existing.weight_of_box != null) bits.push(existing.weight_of_box + " lbs");
        if (existing.total_good_sneakers) bits.push(existing.total_good_sneakers + " good");
        if (existing.total_end_of_life) bits.push(existing.total_end_of_life + " EOL");
        if (existing.casuals) bits.push(existing.casuals + " casuals");
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
                    showToast("Will OVERWRITE " + r.matches[0].id + " on send", "info", 2600);
                    capWeight.focus();
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
            if (code.length >= 4) {
                barcodeStatus.textContent = "Scanned: " + code;
                showToast("BARCODE SCANNED: " + code, "barcode", 1500);
                lookupShipment(code);
                checkDuplicate(code);
                capWeight.focus();         // after a scan, jump straight to Weight; Enter/Tab walks the rest
            } else {
                barcodeStatus.textContent = "Barcode too short (min 4 chars)";
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
        if (dupModalOpen) return;   // no trigger/learning while the duplicate modal is up
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
        if (dupModalOpen) return;   // modal buttons are plain left-clicks; no trigger can fire
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
    // worker down Weight → Good → End of Life → Casuals without reaching for
    // the mouse. Enter past the last field just blurs (it does NOT auto-send —
    // sending is the deliberate USB-button / Capture & Send press).
    const BOX_ORDER = [capWeight, capGood, capEol, capCasuals];
    BOX_ORDER.forEach((el, i) => {
        el.addEventListener("keydown", (e) => {
            if (e.key !== "Enter" || dupModalOpen) return;   // Tab is handled natively by the browser
            e.preventDefault();
            const next = BOX_ORDER[i + 1];
            if (next) next.focus();
            else el.blur();
        });
    });

    /* ---- Wiring --------------------------------------------------------- */

    captureBtn.addEventListener("click", fullSend);
    uploadBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", (e) => useUploadedFile(e.target.files[0]));

    window.addEventListener("beforeunload", () => { if (stream) stream.getTracks().forEach(t => t.stop()); });

    refreshHint();
    refreshVolumeUI();
    startCamera();
})();
