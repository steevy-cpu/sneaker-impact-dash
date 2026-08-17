"""
brands.py — canonical brand names + count merging, shared across every surface
that reports brands (Tableau stats/search/CSV, Airtable brand summary).

Brand strings from the model/cloud arrive with inconsistent casing (ASICS vs
Asics) and a few aliases (On Running vs On). Rather than rewrite stored
predictions — lossy and risky — we canonicalize at the AGGREGATION layer: wherever
brands are counted for display/report/sync, case- and alias-variants merge into a
single canonical label. Deterministic and fully reversible: change the map, not
the data. Nothing downstream keys off the raw label except display, so this can't
break matching, sync, or review.
"""

# Pinned canonical spellings, keyed by normalized (lower, single-spaced) name.
# Needed only where the automatic "most common spelling" pick would be wrong
# (official all-caps brands) or where two different strings are one brand (alias).
CANONICAL_BRANDS = {
    "asics": "ASICS",
    "oofos": "OOFOS",
    "anta": "ANTA",
    "ecco": "ECCO",
    "peak": "PEAK",
    "nobull": "NOBULL",
    "akk": "AKK",
    "on": "On",
    "on running": "On",          # alias -> On
    "hoka": "Hoka",
    "hoka one one": "Hoka",       # alias -> Hoka
    "l.a. gear": "L.A. Gear",
    "la gear": "L.A. Gear",       # alias -> L.A. Gear
}

_UNKNOWN = {None, "", "unknown"}

# Insole makers, by normalized key. Insoles ride along in shoe boxes and land on
# the capture table, where the cloud happily brands them ("Currex RunPro") — but
# they are NOT shoes: their counts are entered manually at capture (the
# "Insoles in box" field) and belong in the Insoles_* Airtable columns. In SHOE
# capture mode anything identified as one of these is excluded from Brand
# Summary, label_data export, and the seen-shoe cache. The insole capture mode
# has its own pipeline and never consults this set.
# Only brands that are insole-ONLY here: Dr. Scholl's and Spenco also make real
# footwear (Time Off Sneaker, Walking Shoe, sandals) and must keep counting as
# shoes.
INSOLE_BRANDS = {
    "currex", "curex", "currexsole",
    "superfeet", "super feet", "superfleet",
    "powerstep", "power step",
    "sof sole", "sofsole",
}


def is_insole_brand(name):
    """True when a brand string names an insole maker (case/alias-insensitive)."""
    return norm_key(name) in INSOLE_BRANDS


def norm_key(name):
    """Normalized match key: trimmed, whitespace-collapsed, lowercased."""
    return " ".join((name or "").strip().split()).lower()


def canonical_brand(name):
    """Canonical display label for one brand string (case/alias-merged).
    Unknown/empty -> None. Unmapped brands are returned trimmed as-is."""
    key = norm_key(name)
    if key in _UNKNOWN:
        return None
    if key in CANONICAL_BRANDS:
        return CANONICAL_BRANDS[key]
    return " ".join((name or "").strip().split())


def merge_brand_counts(rows):
    """Merge (label, count) rows into canonical brands, skipping unknown/empty.
    Case-variants AND aliases collapse: the group key is the pinned canonical when
    mapped (so 'on' and 'on running' both land on 'On'), else the case-folded key
    (so unmapped spellings still merge). Unmapped groups display their most common
    original spelling. Sorted desc."""
    agg = {}                       # group key -> [total, {spelling: count}, canon]
    for label, cnt in rows:
        key = norm_key(label)
        if key in _UNKNOWN:
            continue
        canon = CANONICAL_BRANDS.get(key)      # pinned canonical, or None
        gkey = canon or key                    # aliases share canon; else case key
        e = agg.setdefault(gkey, [0, {}, canon])
        e[0] += cnt
        e[1][label] = e[1].get(label, 0) + cnt
    out = []
    for gkey, (tot, spellings, canon) in agg.items():
        label = canon or max(spellings, key=spellings.get)
        out.append((label, tot))
    out.sort(key=lambda z: z[1], reverse=True)
    return out


def variants_for(name, known_makes):
    """Given a queried brand and the raw make spellings present in the DB, return
    the raw spellings that canonicalize to the same brand (for a SQL IN clause)."""
    target = norm_key(canonical_brand(name) or name)
    return [m for m in known_makes
            if norm_key(canonical_brand(m) or m) == target]


def brand_prefixes(make):
    """All brand-name strings worth stripping from a model of this make: the raw
    make, its canonical form, and every alias that maps to that canonical (so
    'Hoka One One Clifton' loses the full 'Hoka One One', not just 'Hoka'). Used
    for brand-aware model cleanup — we only strip a model's OWN brand."""
    out = set()
    m = (make or "").strip()
    if m:
        out.add(m)
    canon = canonical_brand(make)
    if canon:
        out.add(canon)
        out.update(k for k, v in CANONICAL_BRANDS.items() if v == canon)
    return {p for p in out if p}
