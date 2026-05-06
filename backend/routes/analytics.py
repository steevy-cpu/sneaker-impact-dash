"""
analytics.py — Aggregation endpoints for the Analytics and Dashboard Home pages.

  GET /api/analytics/daily-summary  →  Dashboard Home stat cards
  GET /api/analytics/trends         →  Analytics page charts

All metrics are computed with SQL conditional aggregation (CASE WHEN).
Days with no data are zero-filled so charts have a continuous x-axis.
"""
import sqlite3
from datetime import date as date_type, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query

from backend.database import get_db

router = APIRouter(prefix="/api/analytics", tags=["Analytics"])


@router.get("/daily-summary", summary="Totals and averages for a single day")
def daily_summary(
    date: Optional[str] = Query(
        None,
        description="Date in YYYY-MM-DD format. Defaults to today."
    ),
    conn: sqlite3.Connection = Depends(get_db),
):
    """
    Returns aggregated stats for all shoes inspected on the given date.

    Used by the Dashboard Home page to populate the stat cards.
    Defaults to today when no date is provided.
    """
    target_date = date or date_type.today().isoformat()

    row = conn.execute(
        """
        SELECT
            COUNT(*)                                                          AS total,
            SUM(CASE WHEN ai_prediction = 'REUSE'   THEN 1 ELSE 0 END)       AS reuse_count,
            SUM(CASE WHEN ai_prediction = 'RECYCLE' THEN 1 ELSE 0 END)       AS recycle_count,
            SUM(CASE WHEN ai_prediction = 'REVIEW'  THEN 1 ELSE 0 END)       AS review_count,
            -- CASE excludes NULLs so failed captures don't drag the average down
            AVG(CASE WHEN ai_confidence IS NOT NULL THEN ai_confidence END)   AS avg_confidence,
            SUM(CASE WHEN validation_status != 'VALID' THEN 1 ELSE 0 END)    AS failed_captures,
            SUM(CASE WHEN review_status = 'PENDING'    THEN 1 ELSE 0 END)    AS pending_review_count
        FROM shoes
        WHERE date(timestamp) = ?
        """,
        (target_date,),
    ).fetchone()

    return {
        "date":                 target_date,
        "total":                row["total"] or 0,
        "reuse_count":          row["reuse_count"] or 0,
        "recycle_count":        row["recycle_count"] or 0,
        "review_count":         row["review_count"] or 0,
        "avg_confidence":       round(row["avg_confidence"] or 0.0, 4),
        "failed_captures":      row["failed_captures"] or 0,
        "pending_review_count": row["pending_review_count"] or 0,
    }


@router.get("/trends", summary="Per-day trend data for the analytics charts")
def trends(
    days: int = Query(7, ge=1, le=90, description="Number of days back (including today)"),
    conn: sqlite3.Connection = Depends(get_db),
):
    """
    Returns one entry per day for the last N days.
    Days with no inspections are filled with zeros so charts have a
    continuous x-axis.
    """
    today = date_type.today()
    date_range = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]

    rows = conn.execute(
        """
        SELECT
            date(timestamp)                                                        AS day,
            COUNT(*)                                                               AS total,
            SUM(CASE WHEN ai_prediction = 'REUSE'   THEN 1 ELSE 0 END)            AS reuse_count,
            SUM(CASE WHEN ai_prediction = 'RECYCLE' THEN 1 ELSE 0 END)            AS recycle_count,
            SUM(CASE WHEN ai_prediction = 'REVIEW'  THEN 1 ELSE 0 END)            AS review_count,
            AVG(CASE WHEN ai_confidence IS NOT NULL THEN ai_confidence END)        AS avg_confidence,
            SUM(CASE WHEN validation_status != 'VALID' THEN 1 ELSE 0 END)         AS failed_captures,
            SUM(CASE WHEN human_override = 1          THEN 1 ELSE 0 END)          AS human_override_count,
            SUM(CASE WHEN review_status  = 'COMPLETED' THEN 1 ELSE 0 END)         AS human_reviewed_count,
            -- ai_human_agreed: reviewed shoes where the human confirmed the AI's prediction
            -- This is NOT AI accuracy — it only covers the subset that received human review
            SUM(
                CASE WHEN review_status = 'COMPLETED'
                          AND final_decision = ai_prediction THEN 1 ELSE 0 END
            )                                                                      AS ai_human_agreed
        FROM shoes
        WHERE date(timestamp) >= ?
        GROUP BY day
        ORDER BY day
        """,
        (date_range[0],),
    ).fetchall()

    # Index rows by date so we can detect missing days
    by_day = {r["day"]: r for r in rows}

    # Zero-fill: iterate the full date range and substitute zeros for missing days.
    # This keeps chart x-axes continuous even during weekends or low-volume periods.
    result = []
    for d in date_range:
        r = by_day.get(d)
        result.append({
            "date":                 d,
            "total":                r["total"]                if r else 0,
            "reuse_count":          r["reuse_count"]          if r else 0,
            "recycle_count":        r["recycle_count"]        if r else 0,
            "review_count":         r["review_count"]         if r else 0,
            "avg_confidence":       round(r["avg_confidence"] or 0.0, 4) if r else 0.0,
            "failed_captures":      r["failed_captures"]      if r else 0,
            "human_override_count": r["human_override_count"] if r else 0,
            "human_reviewed_count": r["human_reviewed_count"] if r else 0,
            "ai_human_agreed":      r["ai_human_agreed"]      if r else 0,
        })

    return {"days": days, "data": result}


@router.get("/alerts", summary="Operational alerts based on current data thresholds")
def alerts(conn: sqlite3.Connection = Depends(get_db)):
    """
    Returns a list of alerts derived from today's and all-time data.
    Used by Dashboard Home and System Health to surface issues at a glance.

    Alert levels:
      "warning" — needs attention (threshold exceeded)
      "info"    — worth noting (approaching threshold)
      "ok"      — all clear (no issues detected)
    """
    today     = date_type.today().isoformat()
    yesterday = (date_type.today() - timedelta(days=1)).isoformat()

    # Today's stats
    row = conn.execute(
        """
        SELECT
            COUNT(*)                                                              AS total,
            SUM(CASE WHEN review_status  = 'PENDING'      THEN 1 ELSE 0 END)    AS today_pending,
            SUM(CASE WHEN validation_status != 'VALID'    THEN 1 ELSE 0 END)    AS failed,
            AVG(CASE WHEN ai_confidence IS NOT NULL THEN ai_confidence END)      AS avg_conf,
            SUM(CASE WHEN ai_prediction = 'REVIEW'        THEN 1 ELSE 0 END)    AS review_flagged
        FROM shoes WHERE date(timestamp) = ?
        """,
        (today,),
    ).fetchone()

    # All-time pending (review queue may accumulate across days)
    all_pending = conn.execute(
        "SELECT COUNT(*) FROM shoes WHERE review_status = 'PENDING'"
    ).fetchone()[0]

    # Yesterday's review-flagged count for queue-growth detection
    yesterday_flagged = conn.execute(
        "SELECT SUM(CASE WHEN ai_prediction = 'REVIEW' THEN 1 ELSE 0 END)"
        " FROM shoes WHERE date(timestamp) = ?",
        (yesterday,),
    ).fetchone()[0] or 0

    total         = row["total"]          or 0
    today_pending = row["today_pending"]  or 0
    failed        = row["failed"]         or 0
    avg_conf      = row["avg_conf"]       or 0.0
    review_flagged = row["review_flagged"] or 0

    result: list[dict] = []

    # ── Alert: pending review queue ────────────────────────────────
    if all_pending >= 10:
        result.append({
            "level":   "warning",
            "type":    "pending_review",
            "message": f"{all_pending} shoes are waiting for human review — review queue is high",
            "value":   all_pending,
        })
    elif all_pending >= 3:
        result.append({
            "level":   "info",
            "type":    "pending_review",
            "message": f"{all_pending} shoes pending review",
            "value":   all_pending,
        })

    # ── Alert: high failed captures today ──────────────────────────
    if total > 0:
        fail_pct = failed / total
        if fail_pct >= 0.15:
            result.append({
                "level":   "warning",
                "type":    "failed_captures",
                "message": (
                    f"High capture failure rate today: {failed} of {total} "
                    f"shoes ({fail_pct:.0%}) — check camera setup"
                ),
                "value":   failed,
            })
        elif fail_pct >= 0.10:
            result.append({
                "level":   "info",
                "type":    "failed_captures",
                "message": f"Elevated capture failures today: {failed} shoes ({fail_pct:.0%})",
                "value":   failed,
            })

    # ── Alert: low average confidence ──────────────────────────────
    if avg_conf > 0:
        if avg_conf < 0.65:
            result.append({
                "level":   "warning",
                "type":    "low_confidence",
                "message": (
                    f"Average AI confidence is low today ({avg_conf:.0%}) "
                    "— model may need retraining"
                ),
                "value":   round(avg_conf, 4),
            })
        elif avg_conf < 0.75:
            result.append({
                "level":   "info",
                "type":    "low_confidence",
                "message": f"Average AI confidence is moderate today ({avg_conf:.0%})",
                "value":   round(avg_conf, 4),
            })

    # ── Alert: review queue growing ────────────────────────────────
    if review_flagged > 0 and yesterday_flagged > 0 and review_flagged > yesterday_flagged * 1.5:
        result.append({
            "level":   "info",
            "type":    "queue_growing",
            "message": (
                f"Review volume is up today ({review_flagged} flagged vs "
                f"{yesterday_flagged} yesterday)"
            ),
            "value":   review_flagged,
        })

    return {"alerts": result, "all_pending": all_pending}


@router.get("/activity", summary="Recent inspection activity as human-readable events")
def activity(conn: sqlite3.Connection = Depends(get_db)):
    """
    Returns the last 20 shoe inspection records as plain-English event messages.
    Used by the Dashboard activity feed to make the system feel alive.
    """
    rows = conn.execute(
        """SELECT id, ai_prediction, ai_confidence, validation_status,
                  shoe_brand, review_status, timestamp
           FROM shoes ORDER BY timestamp DESC LIMIT 20"""
    ).fetchall()

    events = []
    for row in rows:
        brand = row["shoe_brand"] or "Unknown"
        pred  = row["ai_prediction"]
        conf  = row["ai_confidence"]
        vs    = row["validation_status"]

        conf_str = f" ({conf:.0%})" if conf else ""

        if vs == "FAILED_CAPTURE":
            msg, level = f"Failed capture detected — {brand}", "warn"
        elif pred == "REVIEW":
            msg, level = f"{brand} flagged for human review{conf_str}", "warn"
        elif pred == "REUSE" and conf and conf >= 0.90:
            msg, level = f"High-confidence {brand} classified for reuse ({conf:.0%})", "ok"
        elif pred == "RECYCLE":
            msg, level = f"{brand} classified for recycling{conf_str}", "neutral"
        elif pred == "REUSE":
            msg, level = f"New {brand} inspection — REUSE{conf_str}", "ok"
        else:
            msg, level = f"New {brand} inspection added", "neutral"

        events.append({
            "id":        row["id"],
            "message":   msg,
            "level":     level,
            "timestamp": row["timestamp"],
        })

    return {"events": events}


@router.get("/brand-distribution", summary="Inspection counts and review rates by shoe brand")
def brand_distribution(conn: sqlite3.Connection = Depends(get_db)):
    """
    Returns per-brand aggregated stats.
    Used by the Analytics page brand insights and doughnut chart.
    """
    rows = conn.execute(
        """
        SELECT shoe_brand,
               COUNT(*)                                                           AS count,
               SUM(CASE WHEN ai_prediction = 'REVIEW' THEN 1 ELSE 0 END)        AS review_count,
               AVG(CASE WHEN ai_confidence IS NOT NULL THEN ai_confidence END)   AS avg_confidence
        FROM shoes
        WHERE shoe_brand IS NOT NULL
        GROUP BY shoe_brand
        ORDER BY count DESC
        """
    ).fetchall()

    brands = []
    for r in rows:
        cnt = r["count"]
        rev = r["review_count"] or 0
        brands.append({
            "brand":          r["shoe_brand"],
            "count":          cnt,
            "review_count":   rev,
            "avg_confidence": round(r["avg_confidence"] or 0.0, 4),
            "review_rate":    round(rev / cnt, 4) if cnt else 0.0,
        })

    return {"brands": brands}
