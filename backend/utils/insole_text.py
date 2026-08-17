"""
insole_text.py — parse the operator's free-text insole counts.

Combined boxes (shoes + insoles together) don't go through the insole engine;
the operator types what they counted into the capture form's "Insoles in box"
field, e.g. "2 currex pairs, 1 superfeet single". This module turns that text
into per-brand pairs/singles counts for the Insoles_currex / Insoles_superfeet
Airtable columns.

The grammar is deliberately forgiving — it accepts brand aliases and typos
(curex, superfleet, sf, cx), number-before or number-after brand, unit words in
any of pair/pairs/pr/p and single/singles/sgl/s, word numbers (one..twelve),
and clauses in any order. It is also deliberately STRICT about failure: any
number it can't attach to a brand, or brand without a number, makes the whole
parse fail (returns None) — the capture UI shows a live preview of the parsed
interpretation and blocks the send on failure, so a misread can never reach
Airtable silently.

KEEP IN SYNC with the JS mirror in frontend/js/capture.js (parseInsoleText).
Shared test vectors: backend/utils/insole_text_vectors.json — run
test_insole_text.py after touching either implementation.
"""
import re

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

# Brand aliases -> canonical token. Tolerant of the typos people actually make
# (curex/correx, superfleet/super feet) plus the short forms cx / sf.
_BRAND_SUBS = [
    (re.compile(r"\bc[ou]r+e?\s*r?e?xe?s?\b", re.I), " CURREX "),   # incl. "cur rex"
    (re.compile(r"\bcx\b", re.I), " CURREX "),
    (re.compile(r"\bsuper\s*fl?ee?ts?\b", re.I), " SUPERFEET "),
    (re.compile(r"\bsf\b", re.I), " SUPERFEET "),
]

# Token stream: a number with an optional attached unit, a canonical brand,
# a standalone unit word ("3 currex singles" — unit trails the brand), or any
# other word (checked against the noise whitelist below).
_TOKEN = re.compile(
    r"(?P<num>\d+)\s*(?P<unit>pairs?|prs?|p(?![a-z])|singles?|sgls?|s(?![a-z]))?"
    r"|(?P<brand>CURREX|SUPERFEET)"
    r"|(?P<lone_unit>\b(?:pairs?|prs?|singles?|sgls?)\b)"
    r"|(?P<word>[a-z]+)"
)

# Connector/filler words that may appear around the counts without meaning.
# Any OTHER word (an unknown brand like "spenco", a stray "nike") fails the
# whole parse — better a blocked send than numbers counted under the wrong
# brand.
_NOISE_WORDS = {"and", "or", "plus", "of", "in", "the", "a", "an", "box",
                "insole", "insoles", "with", "total", "x", "for", "table",
                "each", "only"}


def _unit_is_single(unit):
    return bool(unit) and unit.lower().startswith("s")


def parse_insole_text(text):
    """Parse free text -> {"currex": [pairs, singles], "superfeet": [...]}
    with only the brands the operator mentioned. Returns None when the text
    can't be fully understood (never guess). Empty/whitespace text -> None
    (callers treat "no text" separately)."""
    if not text or not text.strip():
        return None
    tokens = _tokenize(text)
    if any(k == "junk" for k, _v, _u in tokens):
        return None                               # unknown word -> never guess

    counts = {}                                   # brand -> [pairs, singles]
    fed = set()                                   # brands that got a number
    pending = []                                  # numbers waiting for a brand
    current = None                                # most recent brand
    last = None                                   # (brand, n, was_default_unit)

    def assign(brand, n, unit):
        nonlocal last
        counts.setdefault(brand, [0, 0])
        idx = 1 if _unit_is_single(unit) else 0   # no unit -> pairs (the norm)
        counts[brand][idx] += n
        fed.add(brand)
        last = (brand, n, unit is None)

    for i, (kind, val, unit) in enumerate(tokens):
        if kind == "brand":
            counts.setdefault(val, [0, 0])
            for n, u in pending:
                assign(val, n, u)
            pending = []
            current = val
        elif kind == "num":
            # A number just before a brand belongs to THAT brand ("3 currex,
            # 2 superfeet") — unless that brand has its own number after it
            # ("currex 2 pairs, 1 single, superfeet 3": the 1 stays currex's).
            nxt = next(((k, v) for k, v, _u in tokens[i + 1:]), None)
            if nxt and nxt[0] == "brand":
                after = next(((k, v) for k, v, _u in tokens[i + 2:]), None)
                brand_has_own = bool(after and after[0] == "num")
                if not brand_has_own or current is None:
                    pending.append((val, unit))
                    continue
            if current is None:
                pending.append((val, unit))
            else:
                assign(current, val, unit)
        else:  # lone unit word retypes the last default-typed number:
            # "3 currex singles" -> the 3 was counted as pairs; move it.
            if last and last[2]:
                brand, n, _ = last
                if _unit_is_single(val):
                    counts[brand][0] -= n
                    counts[brand][1] += n
                last = (brand, n, False)

    if pending:                                   # numbers with no brand
        return None
    if not counts:                                # no brand mentioned
        return None
    if any(b not in fed for b in counts):         # brand with no number
        return None
    return counts


def _tokenize(text):
    """Normalize + tokenize shared by both parsers. Returns a token list of
    ("brand"|"num"|"unit"|"junk", value, unit); connector noise words are
    dropped entirely, any other unknown word becomes "junk"."""
    t = text.lower()
    t = re.sub(r"\b(" + "|".join(_WORD_NUMBERS) + r")\b",
               lambda m: str(_WORD_NUMBERS[m.group(1)]), t)
    for pat, repl in _BRAND_SUBS:
        t = pat.sub(repl, t)
    tokens = []
    for m in _TOKEN.finditer(t):
        if m.group("brand"):
            tokens.append(("brand", m.group("brand").lower(), None))
        elif m.group("num"):
            tokens.append(("num", int(m.group("num")), m.group("unit")))
        elif m.group("lone_unit"):
            tokens.append(("unit", m.group("lone_unit"), None))
        elif m.group("word") not in _NOISE_WORDS:
            tokens.append(("junk", m.group("word"), None))
    return tokens


# How many unrelated words a unit-tagged count may cross before/after a brand
# mention ("25 pair accessories cur rex" -> 1 word of junk between count and
# brand). Small on purpose: past this the association is a guess.
_EXTRACT_GAP = 3


def extract_insole_counts(text):
    """LENIENT extraction for the free-form NOTES field (vs parse_insole_text's
    strict all-or-nothing for a dedicated field). Notes contain arbitrary
    operator prose, so unknown words are ignored instead of failing — but the
    binding rules stay conservative to avoid counting unrelated numbers:

      * a number WITH a unit ("25 pair") may cross up to 3 unrelated words to
        reach a brand, in either direction;
      * a BARE number binds only when adjacent to the brand ("currex: 10",
        "10 currex") — one unrelated word in between ("3 boxes currex") kills
        it, since the word it modified was probably not insoles;
      * a brand mentioned without any usable count is skipped, not an error.

    Returns {"currex": [pairs, singles], ...} for brands with counts, or None
    when nothing reliable was found. Never raises, never blocks a capture."""
    if not text or not text.strip():
        return None
    counts = {}
    fed = set()
    pending = []                                  # [n, unit, junk_gap]
    current = None                                # brand counts bind back to
    current_gap = 0                               # junk since current was live
    last = None                                   # (brand, n, was_default_unit)

    def assign(brand, n, unit):
        nonlocal last
        counts.setdefault(brand, [0, 0])
        counts[brand][1 if _unit_is_single(unit) else 0] += n
        fed.add(brand)
        last = (brand, n, unit is None)

    tokens = _tokenize(text)
    for i, (kind, val, unit) in enumerate(tokens):
        if kind == "junk":
            # Bare pending numbers belonged to this word ("3 boxes"), not to a
            # brand; unit-tagged ones survive a few words of prose.
            pending = [p for p in pending
                       if p[1] is not None and p[2] < _EXTRACT_GAP]
            for p in pending:
                p[2] += 1
            current_gap += 1
            if current_gap > _EXTRACT_GAP:
                current = None
            last = None
        elif kind == "brand":
            for n, u, _g in pending:
                assign(val, n, u)
            pending = []
            current, current_gap = val, 0
        elif kind == "num":
            # A bare number immediately followed by an unknown word belongs to
            # that word ("3 nike", "3 boxes"), never to an insole brand.
            if (unit is None and i + 1 < len(tokens)
                    and tokens[i + 1][0] == "junk"):
                continue
            prev_junk = i > 0 and tokens[i - 1][0] == "junk"
            # Same forward-binding lookahead as the strict parser: a number
            # just before a brand belongs to THAT brand ("wet box. 2 currex
            # pairs") — unless the brand has its own number right after it
            # ("refund 12, currex 2 pairs": the 12 is not ours).
            j = i + 1
            while j < len(tokens) and tokens[j][0] == "junk":
                j += 1
            if j < len(tokens) and tokens[j][0] == "brand":
                after = next(((k, v) for k, v, _u in tokens[j + 1:]
                              if k != "junk"), None)
                if not (after and after[0] == "num"):
                    pending.append([val, unit, 0])
                    continue
                # The brand ahead counts itself; this number is the current
                # brand's ("currex 2 pairs, 1 single, superfeet 3") or nobody's.
                if current is not None and (unit or current_gap == 0) \
                        and not (unit is None and prev_junk):
                    assign(current, val, unit)
                continue
            # No brand ahead: a bare number right after prose was that prose's
            # number ("refund 12"), not a count.
            if unit is None and prev_junk:
                continue
            if current is not None and (unit or current_gap == 0):
                assign(current, val, unit)
            else:
                pending.append([val, unit, 0])
        else:  # lone unit word retypes the last default-typed number
            if last and last[2]:
                brand, n, _ = last
                if _unit_is_single(val):
                    counts[brand][0] -= n
                    counts[brand][1] += n
                last = (brand, n, False)

    counts = {b: c for b, c in counts.items() if b in fed}
    return counts or None


def canonical_insole_text(counts):
    """Counts dict -> a canonical text ("currex 25 pairs 2 singles,
    superfeet 3 pairs") that round-trips through the STRICT parser — it's what
    gets stored in table_photos.insoles_text when counts were extracted from
    notes, so downstream re-parsing (tableau) stays exact."""
    parts = []
    for brand in ("currex", "superfeet"):
        c = counts.get(brand)
        if not c:
            continue
        bits = []
        if c[0] or not c[1]:
            bits.append(f"{c[0]} pair{'' if c[0] == 1 else 's'}")
        if c[1]:
            bits.append(f"{c[1]} single{'' if c[1] == 1 else 's'}")
        parts.append(f"{brand} " + " ".join(bits))
    return ", ".join(parts)


def format_insole_counts(n_pairs, n_singles):
    """The Airtable cell text: '3 pairs, 2 singles' (singular-aware)."""
    return (f"{n_pairs} pair{'' if n_pairs == 1 else 's'}, "
            f"{n_singles} single{'' if n_singles == 1 else 's'}")


def summaries_from_parse(parsed):
    """Parsed counts -> {'insoles_currex': text|None, 'insoles_superfeet': ...}.
    Only mentioned brands get a text — a manual entry must never zero a column
    it didn't talk about."""
    if not parsed:
        return {"insoles_currex": None, "insoles_superfeet": None}
    out = {}
    for brand, key in (("currex", "insoles_currex"),
                       ("superfeet", "insoles_superfeet")):
        c = parsed.get(brand)
        out[key] = format_insole_counts(c[0], c[1]) if c else None
    return out
