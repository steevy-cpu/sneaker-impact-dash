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
"""
import re

# Fold to a match key: lowercase, then every run of non-alphanumerics -> single
# space, trimmed. Collapses case + punctuation (-, %, +) + repeated/edge spacing.
_NONALNUM = re.compile(r"[^a-z0-9]+")
_SKIP_KEYS = {"", "unknown"}


def model_key(name):
    """Punctuation/case-insensitive match key for a model string."""
    if name is None:
        return ""
    return _NONALNUM.sub(" ", name.lower()).strip()


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
