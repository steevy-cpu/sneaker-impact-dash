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

    let stream = null;
    let uploadedBlob = null;     // set only by the upload fallback
    let lastCaptureAt = 0;
    let busy = false;
    let learning = false;
    let lastLookup = "";

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
        capWeight.value = "0"; capGood.value = "0"; capEol.value = "0"; capCasuals.value = "0";
        barcodeInput.value = "";
        barcodeStatus.textContent = "Ready for barcode scanning…";
        shipmentPreview.style.display = "none"; lastLookup = "";
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
        try {
            const res = await api.captureTablePhoto(fd);
            showToast("✅ QUEUED — " + res.id, "success", 2600);
            resetFields();
            barcodeInput.focus();
        } catch (err) {
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
    barcodeInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            const code = barcodeInput.value.trim();
            if (code.length >= 4) {
                barcodeStatus.textContent = "Scanned: " + code;
                showToast("BARCODE SCANNED: " + code, "barcode", 1500);
                lookupShipment(code);
                capGood.focus();           // after a scan, move on to the counts
            } else {
                barcodeStatus.textContent = "Barcode too short (min 4 chars)";
            }
        }
    });
    barcodeInput.addEventListener("input", () => {
        const code = barcodeInput.value.trim();
        barcodeStatus.textContent = code ? "Barcode: " + code : "Ready for barcode scanning…";
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

    /* ---- Wiring --------------------------------------------------------- */

    captureBtn.addEventListener("click", fullSend);
    uploadBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", (e) => useUploadedFile(e.target.files[0]));

    window.addEventListener("beforeunload", () => { if (stream) stream.getTracks().forEach(t => t.stop()); });

    refreshHint();
    startCamera();
})();
