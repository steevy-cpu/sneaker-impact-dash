"use strict";
/* insole_parse.js — read insole counts out of operator text (live preview).
 *
 * JS MIRROR of backend/utils/insole_text.py (the authoritative server-side
 * implementation) — KEEP THE TWO IN SYNC. Two entry points:
 *
 *   parseInsoleText(text)     STRICT: for a dedicated count field — unknown
 *                             words or unattached numbers fail the whole parse
 *                             (null). Vectors: insole_text_vectors.json.
 *   extractInsoleCounts(text) LENIENT: for the free-form NOTES field — prose
 *                             is ignored, only confident brand+count bindings
 *                             are kept, never fails a capture. Vectors:
 *                             insole_extract_vectors.json.
 *
 * Both run the parity test through node; pure regex + one linear pass over a
 * short string: microseconds per keystroke, no network, nothing that can slow
 * the capture page down.
 */
(function () {
    const WORD_NUMBERS = {
        one: 1, two: 2, three: 3, four: 4, five: 5, six: 6,
        seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12,
    };
    const BRAND_SUBS = [
        [/\bc[ou]r+e?\s*r?e?xe?s?\b/g, " CURREX "],    // incl. "cur rex"
        [/\bcx\b/g, " CURREX "],
        [/\bsuper\s*fl?ee?ts?\b/g, " SUPERFEET "],
        [/\bsf\b/g, " SUPERFEET "],
    ];
    const TOKEN = /(\d+)\s*(pairs?|prs?|p(?![a-z])|singles?|sgls?|s(?![a-z]))?|(CURREX|SUPERFEET)|\b(pairs?|prs?|singles?|sgls?)\b|([a-z]+)/g;
    const NOISE = new Set(["and", "or", "plus", "of", "in", "the", "a", "an",
                           "box", "insole", "insoles", "with", "total", "x",
                           "for", "table", "each", "only"]);
    // How many unrelated words a unit-tagged count may cross to reach a brand.
    const EXTRACT_GAP = 3;

    function unitIsSingle(u) { return !!u && u.toLowerCase().startsWith("s"); }

    /* Shared normalize + tokenize. Noise words are dropped entirely; any other
     * unknown word becomes a ["junk", word, null] token. */
    function tokenize(text) {
        let t = text.toLowerCase();
        t = t.replace(
            new RegExp("\\b(" + Object.keys(WORD_NUMBERS).join("|") + ")\\b", "g"),
            (m) => String(WORD_NUMBERS[m]));
        for (const [pat, repl] of BRAND_SUBS) t = t.replace(pat, repl);
        const tokens = [];
        let m;
        TOKEN.lastIndex = 0;
        while ((m = TOKEN.exec(t)) !== null) {
            if (m[3]) tokens.push(["brand", m[3].toLowerCase(), null]);
            else if (m[1]) tokens.push(["num", parseInt(m[1], 10), m[2] || null]);
            else if (m[4]) tokens.push(["unit", m[4], null]);
            else if (!NOISE.has(m[5])) tokens.push(["junk", m[5], null]);
        }
        return tokens;
    }

    function parseInsoleText(text) {
        if (!text || !text.trim()) return null;
        const tokens = tokenize(text);
        if (tokens.some((t) => t[0] === "junk")) return null;  // never guess

        const counts = {};                 // brand -> [pairs, singles]
        const fed = new Set();             // brands that got a number
        let pending = [];                  // numbers waiting for a brand
        let current = null;                // most recent brand
        let last = null;                   // [brand, n, wasDefaultUnit]

        function assign(brand, n, unit) {
            if (!counts[brand]) counts[brand] = [0, 0];
            counts[brand][unitIsSingle(unit) ? 1 : 0] += n;   // no unit -> pairs
            fed.add(brand);
            last = [brand, n, unit === null];
        }

        for (let i = 0; i < tokens.length; i++) {
            const [kind, val, unit] = tokens[i];
            if (kind === "brand") {
                if (!counts[val]) counts[val] = [0, 0];
                for (const [n, u] of pending) assign(val, n, u);
                pending = [];
                current = val;
            } else if (kind === "num") {
                // A number just before a brand belongs to THAT brand — unless
                // that brand has its own number after it (then it stays with
                // the current brand). Mirrors the Python lookahead exactly.
                const nxt = tokens[i + 1] || null;
                if (nxt && nxt[0] === "brand") {
                    const after = tokens[i + 2] || null;
                    const brandHasOwn = !!(after && after[0] === "num");
                    if (!brandHasOwn || current === null) {
                        pending.push([val, unit]);
                        continue;
                    }
                }
                if (current === null) pending.push([val, unit]);
                else assign(current, val, unit);
            } else {                       // lone unit word retypes the last
                if (last && last[2]) {     // default-typed number
                    const [brand, n] = last;
                    if (unitIsSingle(val)) {
                        counts[brand][0] -= n;
                        counts[brand][1] += n;
                    }
                    last = [brand, n, false];
                }
            }
        }

        if (pending.length) return null;               // numbers with no brand
        const brands = Object.keys(counts);
        if (!brands.length) return null;               // no brand mentioned
        for (const b of brands) if (!fed.has(b)) return null;  // brand w/o number
        return counts;
    }

    /* LENIENT extraction for the notes field — see the Python docstring for
     * the binding rules (bare numbers must be adjacent; unit-tagged counts may
     * cross up to EXTRACT_GAP words of prose; junk is ignored, never fatal). */
    function extractInsoleCounts(text) {
        if (!text || !text.trim()) return null;
        const tokens = tokenize(text);
        const counts = {};
        const fed = new Set();
        let pending = [];                  // [n, unit, junkGap]
        let current = null;
        let currentGap = 0;
        let last = null;

        function assign(brand, n, unit) {
            if (!counts[brand]) counts[brand] = [0, 0];
            counts[brand][unitIsSingle(unit) ? 1 : 0] += n;
            fed.add(brand);
            last = [brand, n, unit === null];
        }
        function nextNonJunk(from) {
            for (let j = from; j < tokens.length; j++) {
                if (tokens[j][0] !== "junk") return tokens[j];
            }
            return null;
        }

        for (let i = 0; i < tokens.length; i++) {
            const [kind, val, unit] = tokens[i];
            if (kind === "junk") {
                // Bare pending numbers belonged to this word ("3 boxes");
                // unit-tagged ones survive a few words of prose.
                pending = pending.filter((p) => p[1] !== null && p[2] < EXTRACT_GAP);
                for (const p of pending) p[2] += 1;
                currentGap += 1;
                if (currentGap > EXTRACT_GAP) current = null;
                last = null;
            } else if (kind === "brand") {
                for (const [n, u] of pending) assign(val, n, u);
                pending = [];
                current = val;
                currentGap = 0;
            } else if (kind === "num") {
                // A bare number immediately followed by an unknown word belongs
                // to that word ("3 nike", "3 boxes"), never to a brand.
                if (unit === null && i + 1 < tokens.length
                        && tokens[i + 1][0] === "junk") {
                    continue;
                }
                const prevJunk = i > 0 && tokens[i - 1][0] === "junk";
                // Number just before a brand belongs to THAT brand ("wet box.
                // 2 currex pairs") — unless the brand has its own number right
                // after it ("refund 12, currex 2 pairs": the 12 is not ours).
                let j = i + 1;
                while (j < tokens.length && tokens[j][0] === "junk") j += 1;
                if (j < tokens.length && tokens[j][0] === "brand") {
                    const after = nextNonJunk(j + 1);
                    if (!(after && after[0] === "num")) {
                        pending.push([val, unit, 0]);
                        continue;
                    }
                    // The brand ahead counts itself; this number is the current
                    // brand's or nobody's.
                    if (current !== null && (unit || currentGap === 0)
                            && !(unit === null && prevJunk)) {
                        assign(current, val, unit);
                    }
                    continue;
                }
                // No brand ahead: a bare number right after prose was that
                // prose's number ("refund 12"), not a count.
                if (unit === null && prevJunk) continue;
                if (current !== null && (unit || currentGap === 0)) {
                    assign(current, val, unit);
                } else {
                    pending.push([val, unit, 0]);
                }
            } else {                       // lone unit word retypes the last
                if (last && last[2]) {
                    const [brand, n] = last;
                    if (unitIsSingle(val)) {
                        counts[brand][0] -= n;
                        counts[brand][1] += n;
                    }
                    last = [brand, n, false];
                }
            }
        }

        for (const b of Object.keys(counts)) if (!fed.has(b)) delete counts[b];
        return Object.keys(counts).length ? counts : null;
    }

    if (typeof module !== "undefined" && module.exports) {
        module.exports = { parseInsoleText, extractInsoleCounts };  // node parity
    } else {
        window.parseInsoleText = parseInsoleText;      // browser (capture page)
        window.extractInsoleCounts = extractInsoleCounts;
    }
})();
