"""
models.py — model-name normalization for reporting aggregates.

Model strings from the cloud/model vary by case, punctuation, and spacing for the
SAME shoe: "Gel-Kayano" / "GEL-Kayano" / "GEL-KAYANO", "Ultraboost" / "UltraBoost",
"Aurora-BL" / "Aurora BL", "Chuck Taylor All-Star" / "Chuck Taylor All Star". Like
the brand merge ([[brands]]), we normalize at the AGGREGATION layer — wherever
models are counted for display/report — rather than rewriting stored predictions.
Deterministic, reversible, and nothing downstream keys off the raw label.

Version numbers survive: "Gel-Kayano" and "Gel-Kayano 28" have different keys and
stay separate (they're genuinely different products), so this only collapses
spelling noise, not model granularity.

Two hard-won key rules (2026-08-17): spacing is NEVER semantic in shoe model
names ("Ultra Boost"/"Ultraboost", "860v14"/"860 v14", "Rocket X 2"/"Rocket X2"
are the same shoe), so the key drops it entirely — but '+' and '%' ARE semantic
("Metaspeed Sky+" and "Metaspeed Sky" are different products), so they stay in
the key.
"""
import re

# Fold to a match key: lowercase, keep letters/digits and the product-line
# significant '+'/'%', drop everything else (hyphens, dots, ALL spacing).
_KEY_DROP = re.compile(r"[^a-z0-9+%]+")
_SKIP_KEYS = {"", "unknown"}


def model_key(name):
    """Spacing/punctuation/case-insensitive match key for a model string.
    '+' and '%' survive (they distinguish real products)."""
    if name is None:
        return ""
    return _KEY_DROP.sub("", name.lower())


def strip_brand_prefix(model, prefixes):
    """Remove a leading brand name (any of `prefixes`) from a model string.
    Case-insensitive, word-boundary (must be followed by space or hyphen), longest
    match wins. Returns '' when the model is ONLY the brand ('Hoka One One' -> '',
    i.e. brand-as-model with no real model). Non-matching input is returned as-is."""
    if not model:
        return ""
    m = model.strip()
    ml = m.lower()
    best = 0
    for p in prefixes:
        p = (p or "").strip().lower()
        if not p:
            continue
        if ml == p:                            # model is just the brand -> no model
            return ""
        if (ml.startswith(p + " ") or ml.startswith(p + "-")) and len(p) > best:
            best = len(p)
    if not best:
        return m
    return m[best:].lstrip(" -").strip()       # len matches (ASCII, case-only diff)


def clean_model(model, make):
    """Brand-aware model cleanup: strip the model's OWN brand prefix.
    'Hoka One One Clifton'(make Hoka) -> 'Clifton'; 'New Balance 990v5' -> '990v5';
    'Hoka One One' -> '' (brand-as-model). Imported lazily to avoid any import
    cycle with brands."""
    from backend.utils.brands import brand_prefixes
    return strip_brand_prefix(model, brand_prefixes(make))


# Human-curated spelling fixes: model_key -> canonical spelling. Wins over the
# auto-generated dominant-spelling map (canonical_models_gen.py) when the
# dataset's most common spelling is not the one we want to standardize on.
CANONICAL_MODEL_OVERRIDES = {}


def canonical_model(model, make):
    """Write-time model normalization: strip the model's own brand prefix, then
    swap in the canonical spelling for its key (curated override first, else the
    dataset-dominant spelling). Unknown keys return the cleaned input unchanged,
    so never-seen models pass through untouched."""
    cleaned = clean_model(model, make)
    key = model_key(cleaned)
    if key in _SKIP_KEYS:
        return cleaned
    from backend.utils.canonical_models_gen import CANONICAL_MODELS
    return CANONICAL_MODEL_OVERRIDES.get(key) or CANONICAL_MODELS.get(key, cleaned)


def normalize_labels(make, model):
    """Write-time cleanup for a (brand, model) prediction before it is stored
    ([[brand-model-dedup]] Phase B): canonical brand spelling + brand-stripped,
    canonically-spelled model. None / '' / 'unknown' pass through unchanged so
    every caller's unknown-handling semantics survive. Pure string ops — safe on
    the worker hot path."""
    from backend.utils.brands import canonical_brand, norm_key
    n_make = make
    if make is not None and norm_key(make) not in ("", "unknown"):
        n_make = canonical_brand(make)
    n_model = model
    if model is not None and model_key(model) not in _SKIP_KEYS:
        n_model = canonical_model(model, n_make)
    return n_make, n_model


def merge_model_counts(rows):
    """Merge (label, count) rows by normalized model, skipping unknown/empty.
    Keeps the most common original spelling as the display label. Sorted desc."""
    agg = {}                                   # key -> [total, {spelling: count}]
    for label, cnt in rows:
        key = model_key(label)
        if key in _SKIP_KEYS:
            continue
        e = agg.setdefault(key, [0, {}])
        e[0] += cnt
        e[1][label] = e[1].get(label, 0) + cnt
    out = [(max(sp, key=sp.get), tot) for tot, sp in agg.values()]
    out.sort(key=lambda z: z[1], reverse=True)
    return out
