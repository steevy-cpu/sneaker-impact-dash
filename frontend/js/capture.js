/**
 * capture.js — Capture Station (P2)
 *
 * Mirrors the PyQt desktop client's operator flow on the web:
 *   1. Live camera preview (getUserMedia) of the whole table, file-upload fallback.
 *   2. A prominent green "Capture" button (Space/Enter shortcut, debounced) freezes
 *      a still frame and opens the box-data popup.
 *   3. Box-data popup — the same 4 fields as the desktop BoxDataDialog
 *      (Weight / Good / End-of-Life / Casuals) + barcode, same validation
 *      ("at least ONE field > 0"), same Cancel / Confirm & Capture buttons.
 *   4. Confirm → POST /api/capture (photo + metadata) → "queued" toast → ready.
 */
(function () {
    "use strict";

    const OPERATOR_ID = localStorage.getItem("operator_id") || "OP-WEB";
    const CAPTURE_DEBOUNCE_MS = 900;   // prevent double-submit (desktop uses ~0.5s)

    // DOM refs
    const stage     = document.getElementById("capture-stage");
    const video     = document.getElementById("capture-video");
    const frozen     = document.getElementById("capture-frozen");
    const frozenBadge = document.getElementById("frozen-badge");
    const stageMsg   = document.getElementById("stage-msg");
    const canvas     = document.getElementById("capture-canvas");
    const cameraLabel = document.getElementById("camera-label");

    const barcodeInput  = document.getElementById("barcode-input");
    const barcodeStatus = document.getElementById("barcode-status");
    const shipmentPreview = document.getElementById("shipment-preview");
    const captureBtn = document.getElementById("capture-btn");
    const uploadBtn  = document.getElementById("upload-btn");
    const fileInput  = document.getElementById("file-input");

    const modal      = document.getElementById("boxdata-modal");
    const bdWeight   = document.getElementById("bd-weight");
    const bdGood     = document.getElementById("bd-good");
    const bdEol      = document.getElementById("bd-eol");
    const bdCasuals  = document.getElementById("bd-casuals");
    const bdBarcode  = document.getElementById("bd-barcode");
    const bdError    = document.getElementById("bd-error");
    const bdCancel   = document.getElementById("bd-cancel");
    const bdConfirm  = document.getElementById("bd-confirm");

    let stream = null;
    let capturedBlob = null;       // the still frame / uploaded file awaiting confirm
    let lastCaptureAt = 0;

    /* ---- Camera --------------------------------------------------------- */

    async function startCamera() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            return cameraUnavailable("This browser can't access the camera. Use “Upload image…”.");
        }
        try {
            stream = await navigator.mediaDevices.getUserMedia({
                video: { width: { ideal: 1920 }, height: { ideal: 1080 } },
                audio: false,
            });
            video.srcObject = stream;
            stageMsg.style.display = "none";
            // Show the active camera's label if the browser exposes it.
            const track = stream.getVideoTracks()[0];
            if (track && track.label) cameraLabel.textContent = track.label;
        } catch (err) {
            cameraUnavailable("Camera unavailable (" + (err.name || "error") +
                              "). Use “Upload image…” instead.");
        }
    }

    function cameraUnavailable(msg) {
        stageMsg.textContent = msg;
        stageMsg.style.display = "flex";
        cameraLabel.textContent = "No camera";
    }

    /* ---- Capture (freeze a frame) --------------------------------------- */

    function captureFromVideo() {
        if (!stream || !video.videoWidth) return false;
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
        frozen.src = canvas.toDataURL("image/jpeg", 0.9);
        canvas.toBlob(b => { capturedBlob = b; }, "image/jpeg", 0.9);
        return true;
    }

    function freezeAndPrompt() {
        // Debounce rapid Space/Enter / double clicks.
        const now = Date.now();
        if (now - lastCaptureAt < CAPTURE_DEBOUNCE_MS) return;
        lastCaptureAt = now;
        if (isModalOpen()) return;

        if (!captureFromVideo()) {
            showToast("NO CAMERA — use “Upload image…”", "error");
            return;
        }
        stage.classList.add("frozen");
        frozenBadge.style.display = "";
        showToast("CAPTURE STARTED", "info", 1400);
        openModal();
    }

    function useUploadedFile(file) {
        if (!file || !file.type.startsWith("image/")) {
            showToast("Please choose an image file", "error");
            return;
        }
        capturedBlob = file;
        frozen.src = URL.createObjectURL(file);
        stage.classList.add("frozen");
        frozenBadge.style.display = "";
        openModal();
    }

    function resetStage() {
        capturedBlob = null;
        stage.classList.remove("frozen");
        frozenBadge.style.display = "none";
        frozen.removeAttribute("src");
        barcodeInput.value = "";
        barcodeStatus.textContent = "Ready for barcode scanning…";
        fileInput.value = "";
    }

    /* ---- Box-data modal ------------------------------------------------- */

    function isModalOpen() { return modal.classList.contains("open"); }

    function openModal() {
        bdError.style.display = "none";
        bdWeight.value = "0"; bdGood.value = "0"; bdEol.value = "0"; bdCasuals.value = "0";
        bdBarcode.value = barcodeInput.value.trim();
        modal.classList.add("open");
        setTimeout(() => { bdWeight.focus(); bdWeight.select(); }, 30);
    }

    function closeModal() { modal.classList.remove("open"); }

    function readBoxData() {
        return {
            weight:  parseFloat(bdWeight.value) || 0,
            good:    parseInt(bdGood.value, 10) || 0,
            eol:     parseInt(bdEol.value, 10) || 0,
            casuals: parseInt(bdCasuals.value, 10) || 0,
        };
    }

    function validate(d) {
        if (d.weight <= 0 && d.good <= 0 && d.eol <= 0 && d.casuals <= 0) {
            bdError.textContent = "⚠️ At least ONE field must be greater than 0";
            bdError.style.display = "";
            return false;
        }
        bdError.style.display = "none";
        return true;
    }

    async function confirmCapture() {
        const d = readBoxData();
        if (!validate(d)) return;
        if (!capturedBlob) { showToast("No photo captured", "error"); return; }

        const fd = new FormData();
        fd.append("image", capturedBlob, "table.jpg");
        fd.append("barcode", bdBarcode.value.trim());
        fd.append("weight_of_box", String(d.weight));
        fd.append("total_good_sneakers", String(d.good));
        fd.append("total_end_of_life", String(d.eol));
        fd.append("casuals", String(d.casuals));
        fd.append("operator_id", OPERATOR_ID);

        bdConfirm.disabled = true;
        bdConfirm.textContent = "Uploading…";
        try {
            const res = await api.captureTablePhoto(fd);
            closeModal();
            resetStage();
            showToast("✅ QUEUED — " + res.id, "success", 2600);
        } catch (err) {
            showToast(err.message || "Capture failed", "error", 3200);
        } finally {
            bdConfirm.disabled = false;
            bdConfirm.textContent = "✅ Confirm & Capture";
        }
    }

    function cancelCapture() {
        closeModal();
        resetStage();   // discard the frozen photo, resume live preview
    }

    /* ---- Barcode (USB scanner / manual) --------------------------------- */
    // USB scanners type fast and end with Enter. We accept Enter as "scanned"
    // and also reflect manual edits in the status line.

    let lastLookup = "";

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
                shipmentPreview.style.display = "none";   // lookup not set up — stay quiet
            } else {
                shipmentPreview.className = "shipment-preview shipment-preview--none";
                shipmentPreview.textContent = "No shipment match for this barcode";
            }
        } catch (err) {
            shipmentPreview.style.display = "none";        // fail-safe: never nag
        }
    }

    barcodeInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            const code = barcodeInput.value.trim();
            if (code.length >= 4) {
                barcodeStatus.textContent = "Scanned: " + code;
                showToast("BARCODE SCANNED: " + code, "barcode", 1800);
                lookupShipment(code);
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

    /* ---- Wiring --------------------------------------------------------- */

    captureBtn.addEventListener("click", freezeAndPrompt);
    uploadBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", (e) => useUploadedFile(e.target.files[0]));

    bdCancel.addEventListener("click", cancelCapture);
    bdConfirm.addEventListener("click", confirmCapture);

    // Global Space/Enter triggers capture — but never while typing in a field
    // or while the modal is open (the modal has its own key handling).
    document.addEventListener("keydown", (e) => {
        if (isModalOpen()) return;
        const tag = (document.activeElement && document.activeElement.tagName) || "";
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "BUTTON") return;
        if (e.code === "Space" || e.key === "Enter") {
            e.preventDefault();
            freezeAndPrompt();
        }
    });

    // Modal keyboard: Enter = confirm, Esc = cancel.
    modal.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { e.preventDefault(); cancelCapture(); }
        else if (e.key === "Enter") { e.preventDefault(); confirmCapture(); }
    });
    // Click the dim backdrop to cancel.
    modal.addEventListener("click", (e) => { if (e.target === modal) cancelCapture(); });

    // Stop the camera when leaving the page.
    window.addEventListener("beforeunload", () => {
        if (stream) stream.getTracks().forEach(t => t.stop());
    });

    startCamera();
})();
