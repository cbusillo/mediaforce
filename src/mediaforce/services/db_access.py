from __future__ import annotations

from typing import Any, Iterable


def fetch_review_items(conn) -> list[dict[str, Any]]:
    """Return review rows with optional profile eval info."""
    cursor = conn.execute(
        """
        SELECT e.id, m.path, m.size_bytes, m.detected_tier, e.output_path,
               e.output_size_bytes, e.vmaf, e.is_outlier,
               pe.status as eval_status, pe.median_vmaf as eval_median,
               pe.min_vmaf as eval_min, pe.weighted_vmaf as eval_weighted,
               pe.id as eval_id, pe.note as eval_note,
               pe.threshold_min as eval_thresh_min, pe.threshold_median as eval_thresh_med,
               pe.threshold_max as eval_thresh_max,
               rc.status as retrain_status
        FROM encode_results e
        JOIN media_inventory m ON e.source_id = m.id
        LEFT JOIN profile_evaluations pe ON pe.id = e.profile_eval_id
        LEFT JOIN retraining_candidates rc ON rc.evaluation_id = e.profile_eval_id AND rc.status != 'done'
        WHERE m.status = 'encoded'
          AND e.output_path IS NOT NULL
          AND e.output_size_bytes > 0
        ORDER BY e.completed_at DESC
        """
    )
    rows = cursor.fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        results.append({
            "id": row["id"],
            "path": row["path"],
            "size_bytes": row["size_bytes"],
            "detected_tier": row["detected_tier"],
            "output_path": row["output_path"],
            "output_size_bytes": row["output_size_bytes"],
            "vmaf": row["vmaf"],
            "is_outlier": row["is_outlier"],
            "eval_status": row["eval_status"],
            "eval_median": row["eval_median"],
            "eval_min": row["eval_min"],
            "eval_weighted": row["eval_weighted"],
            "eval_id": row["eval_id"],
            "eval_note": row["eval_note"],
            "eval_thresh_min": row["eval_thresh_min"],
            "eval_thresh_med": row["eval_thresh_med"],
            "eval_thresh_max": row["eval_thresh_max"],
            "retrain_status": row["retrain_status"],
        })
    return results


def fetch_queue_totals(conn) -> dict[str, Any]:
    cursor = conn.execute(
        """
        SELECT COUNT(*) as total, SUM(potential_savings_bytes) as total_savings,
               SUM(size_bytes) as total_size
        FROM media_inventory
        WHERE status='pending'
        """
    )
    row = cursor.fetchone()
    return {
        "total": row["total"] if row else 0,
        "total_savings": row["total_savings"] if row else 0,
        "total_size": row["total_size"] if row else 0,
    }


def fetch_queue_listing(conn, limit: int) -> Iterable[Any]:
    return conn.execute(
        """
        SELECT id, path, size_bytes, detected_tier, priority_score, bitrate_kbps,
               potential_savings_bytes, manual_priority
        FROM media_inventory
        WHERE status = 'pending'
        ORDER BY priority_score DESC
        LIMIT ?
        """,
        (limit,)
    ).fetchall()


def fetch_stats(conn) -> dict[str, Any]:
    cursor = conn.execute(
        """
        SELECT SUM(m.size_bytes - e.output_size_bytes) as saved_bytes, COUNT(*) as encodes
        FROM encode_results e
        JOIN media_inventory m ON e.source_id = m.id
        WHERE e.output_size_bytes IS NOT NULL AND e.output_size_bytes > 0
        """
    )
    row = cursor.fetchone()
    saved = row["saved_bytes"] or 0 if row else 0
    encodes = row["encodes"] or 0 if row else 0

    active = conn.execute(
        "SELECT COUNT(*) as active FROM encode_progress WHERE percent_complete < 100"
    ).fetchone()
    active_count = active["active"] if active else 0

    return {
        "saved_bytes": saved,
        "encodes": encodes,
        "active_encodes": active_count,
    }
