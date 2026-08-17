/**
 * theme.js — Light / dark mode toggle
 *
 * Loaded in <head> so the correct theme is applied before first paint,
 * preventing a flash of the wrong theme on reload.
 *
 * Saves preference to localStorage key "si_theme".
 * Applies via data-theme="dark" attribute on <html>.
 */

(function () {
    const KEY  = "si_theme";
    const root = document.documentElement;

    function applyTheme(theme) {
        root.setAttribute("data-theme", theme);
        localStorage.setItem(KEY, theme);
        // Update toggle icon if already in DOM
        var icon = document.getElementById("theme-icon");
        if (icon) icon.innerHTML = theme === "dark" ? "<svg class=\"icon\" width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\"><circle cx=\"12\" cy=\"12\" r=\"4\"/><path d=\"M12 2v2\"/><path d=\"M12 20v2\"/><path d=\"m4.93 4.93 1.41 1.41\"/><path d=\"m17.66 17.66 1.41 1.41\"/><path d=\"M2 12h2\"/><path d=\"M20 12h2\"/><path d=\"m6.34 17.66-1.41 1.41\"/><path d=\"m19.07 4.93-1.41 1.41\"/></svg>" : "<svg class=\"icon\" width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\"><path d=\"M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z\"/></svg>";
    }

    // Apply saved preference immediately — runs before CSS is painted
    applyTheme(localStorage.getItem(KEY) || "light");

    // Wire the toggle button once the DOM is ready
    document.addEventListener("DOMContentLoaded", function () {
        // Sync icon text (may have been missed above if DOM wasn't ready)
        var icon = document.getElementById("theme-icon");
        if (icon) {
            icon.innerHTML =
                root.getAttribute("data-theme") === "dark" ? "<svg class=\"icon\" width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\"><circle cx=\"12\" cy=\"12\" r=\"4\"/><path d=\"M12 2v2\"/><path d=\"M12 20v2\"/><path d=\"m4.93 4.93 1.41 1.41\"/><path d=\"m17.66 17.66 1.41 1.41\"/><path d=\"M2 12h2\"/><path d=\"M20 12h2\"/><path d=\"m6.34 17.66-1.41 1.41\"/><path d=\"m19.07 4.93-1.41 1.41\"/></svg>" : "<svg class=\"icon\" width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\"><path d=\"M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z\"/></svg>";
        }

        var btn = document.getElementById("theme-toggle");
        if (btn) {
            btn.addEventListener("click", function () {
                var current = root.getAttribute("data-theme") || "light";
                applyTheme(current === "dark" ? "light" : "dark");
            });
        }

        // Mode badge: reflect the REAL backend mode (APP_MODE) instead of the
        // hardcoded "SIMULATION MODE" placeholder. One place, every page.
        // The same response carries the IT-gate state (it_gate / it_authed),
        // which shows or hides the gated nav links — one fetch for both.
        var badge = document.getElementById("mode-badge");
        fetch("/api/health").then(function (r) { return r.json(); }).then(function (d) {
            if (badge && d && d.mode) {
                var mode = String(d.mode);
                badge.textContent = mode.toUpperCase() + " MODE";
                badge.className = "mode-badge " + (mode === "simulation" ? "simulation" : "actual");
            }
            if (d && d.it_authed) {
                document.body.classList.add("it-authed");
                // Only offer logout when the gate is actually on (it_authed is
                // true for everyone while IT_PASSWORD is unset).
                var toggle = document.getElementById("theme-toggle");
                if (d.it_gate && toggle && !document.getElementById("it-logout")) {
                    var out = document.createElement("button");
                    out.id = "it-logout";
                    out.className = "it-logout";
                    out.title = "You are in IT mode on this browser";
                    out.textContent = "IT · Log out";
                    out.addEventListener("click", function () {
                        fetch("/api/it/logout", { method: "POST" }).finally(function () {
                            location.href = "/frontend/tableau.html";
                        });
                    });
                    toggle.parentNode.insertBefore(out, toggle);
                }
            }
        }).catch(function () { /* leave the static badge if health is unreachable */ });
    });
}());
