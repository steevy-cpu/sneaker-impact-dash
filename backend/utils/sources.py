"""Prediction-source family normalization.

`pairs.prediction_source` is a raw string and the seen-shoe cache embeds its
similarity in it ('cache:cloud:0.9926'), so grouping stats by the raw value
fragments one tier into hundreds of micro-groups. Collapse to a FAMILY before
any per-tier statistics (the random-audit accuracy meter groups on this).

NOTE: tableau.py has an older inline prefix-grouping that predates the cache
tier (it buckets 'cache:*' as unattributed) — prefer this helper for new code.
"""


def source_family(prediction_source) -> str:
    """'cache' | 'gemini+lens' | 'gemini' | 'openai' | 'local' | 'other'."""
    s = (prediction_source or "").strip().lower()
    if s.startswith("cache"):
        return "cache"
    if "+lens" in s:
        return "gemini+lens"
    if s.startswith("cloud:gemini"):
        return "gemini"
    if s.startswith("cloud:openai"):
        return "openai"
    if s == "local":
        return "local"
    return "other"
