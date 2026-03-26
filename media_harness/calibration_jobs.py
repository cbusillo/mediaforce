from __future__ import annotations

import json
import sqlite3
from typing import Any


ACTIVE_JOB_STATUSES = {"queued", "running", "pending_review"}


def load_latest_job(connection: sqlite3.Connection, prefix: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM calibration_jobs WHERE prefix = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (prefix,),
    ).fetchone()
    return _hydrate_job(row) if row is not None else None


def load_job(connection: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM calibration_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _hydrate_job(row) if row is not None else None


def load_active_job(connection: sqlite3.Connection, prefix: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT *
        FROM calibration_jobs
        WHERE prefix = ? AND status IN ('queued', 'running', 'pending_review')
        ORDER BY created_at DESC, rowid DESC
        LIMIT 1
        """,
        (prefix,),
    ).fetchone()
    return _hydrate_job(row) if row is not None else None


def list_queued_jobs(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM calibration_jobs WHERE status = 'queued' ORDER BY created_at ASC, rowid ASC"
    ).fetchall()
    return [_hydrate_job(row) for row in rows]


def claim_next_queued_calibration_job(
    connection: sqlite3.Connection,
    *,
    lane: str,
    owner_pid: int,
    started_at: str,
    excluded_prefixes: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    query = [
        "lane = ?",
        "status = 'queued'",
    ]
    params: list[Any] = [lane]
    if excluded_prefixes:
        placeholders = ", ".join("?" for _ in excluded_prefixes)
        query.append(f"prefix NOT IN ({placeholders})")
        params.extend(excluded_prefixes)
    where_clause = " AND ".join(query)
    row = connection.execute(
        f"""
        WITH candidate AS (
            SELECT job_id
            FROM calibration_jobs
            WHERE {where_clause}
            ORDER BY created_at ASC, rowid ASC
            LIMIT 1
        )
        UPDATE calibration_jobs
        SET status = 'running',
            owner_pid = ?,
            started_at = COALESCE(started_at, ?),
            finished_at = NULL,
            error = NULL,
            updated_at = ?
        WHERE job_id = (SELECT job_id FROM candidate)
          AND status = 'queued'
        RETURNING *
        """,
        tuple(params + [owner_pid, started_at, started_at]),
    ).fetchone()
    return _hydrate_job(row) if row is not None else None


def list_queue_summary(connection: sqlite3.Connection, *, limit_per_lane: int = 6) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT *
        FROM calibration_jobs
        WHERE status IN ('queued', 'running', 'pending_review')
        ORDER BY created_at ASC, rowid ASC
        """
    ).fetchall()
    summary: dict[str, Any] = {
        "sample": {"running": [], "queued": [], "pending_review": [], "running_count": 0, "queued_count": 0, "pending_review_count": 0},
        "full": {"running": [], "queued": [], "pending_review": [], "running_count": 0, "queued_count": 0, "pending_review_count": 0},
        "active_count": 0,
    }
    for row in rows:
        payload = _hydrate_job(row)
        lane = str(payload.get("lane") or "sample")
        lane_summary = summary[lane]
        status = str(payload.get("status") or "queued")
        key = f"{status}_count"
        if key in lane_summary:
            lane_summary[key] += 1
        if status in {"queued", "running", "pending_review"}:
            summary["active_count"] += 1
        if status in lane_summary and len(lane_summary[status]) < limit_per_lane:
            lane_summary[status].append(payload)
    return summary


def queue_position(connection: sqlite3.Connection, job_id: str) -> tuple[int, int] | None:
    row = connection.execute(
        "SELECT lane, created_at, rowid FROM calibration_jobs WHERE job_id = ? AND status = 'queued'",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    lane = str(row["lane"])
    created_at = str(row["created_at"])
    rowid = int(row["rowid"])
    position = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM calibration_jobs
            WHERE lane = ?
              AND status = 'queued'
              AND (created_at < ? OR (created_at = ? AND rowid <= ?))
            """,
            (lane, created_at, created_at, rowid),
        ).fetchone()[0]
    )
    total = int(
        connection.execute(
            "SELECT COUNT(*) FROM calibration_jobs WHERE lane = ? AND status = 'queued'",
            (lane,),
        ).fetchone()[0]
    )
    return position, total


def save_job(connection: sqlite3.Connection, payload: dict[str, Any]) -> None:
    values = _serialize_job(payload)
    columns = ", ".join(values.keys())
    placeholders = ", ".join("?" for _ in values)
    updates = ", ".join(f"{column} = excluded.{column}" for column in values if column != "job_id")
    connection.execute(
        f"""
        INSERT INTO calibration_jobs ({columns})
        VALUES ({placeholders})
        ON CONFLICT(job_id) DO UPDATE SET {updates}
        """,
        tuple(values.values()),
    )


def _serialize_job(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": str(payload["job_id"]),
        "prefix": str(payload["prefix"]),
        "status": str(payload.get("status") or "queued"),
        "lane": str(payload.get("lane") or payload.get("mode") or "sample"),
        "action": str(payload.get("action") or "baseline"),
        "host_json": json.dumps(payload.get("host") or {}, sort_keys=True),
        "notes": str(payload.get("notes") or ""),
        "policy_json": json.dumps(payload.get("policy") or {}, sort_keys=True),
        "sample_item_json": json.dumps(payload.get("sample_item") or {}, sort_keys=True),
        "seed_source": payload.get("seed_source"),
        "seed_summary": payload.get("seed_summary"),
        "seed_prompt_version": payload.get("seed_prompt_version"),
        "seed_raw_response": payload.get("seed_raw_response"),
        "seed_proposed_policy_json": _dumps_optional(payload.get("seed_proposed_policy")),
        "seed_applied_policy_json": _dumps_optional(payload.get("seed_applied_policy")),
        "result_json": _dumps_optional(payload.get("result")),
        "error": payload.get("error"),
        "owner_pid": int(payload["owner_pid"]) if payload.get("owner_pid") is not None else None,
        "created_at": str(payload["created_at"]),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "updated_at": str(payload.get("updated_at") or payload.get("finished_at") or payload.get("started_at") or payload["created_at"]),
    }


def _hydrate_job(row: sqlite3.Row) -> dict[str, Any]:
    payload = {
        "job_id": str(row["job_id"]),
        "prefix": str(row["prefix"]),
        "status": str(row["status"]),
        "lane": str(row["lane"]),
        "mode": str(row["lane"]),
        "action": str(row["action"]),
        "host": _loads_optional(row["host_json"], default={}),
        "notes": str(row["notes"] or ""),
        "policy": _loads_optional(row["policy_json"], default={}),
        "sample_item": _loads_optional(row["sample_item_json"], default={}),
        "seed_source": row["seed_source"],
        "seed_summary": row["seed_summary"],
        "seed_prompt_version": row["seed_prompt_version"],
        "seed_raw_response": row["seed_raw_response"],
        "seed_proposed_policy": _loads_optional(row["seed_proposed_policy_json"], default=None),
        "seed_applied_policy": _loads_optional(row["seed_applied_policy_json"], default=None),
        "result": _loads_optional(row["result_json"], default=None),
        "error": row["error"],
        "owner_pid": row["owner_pid"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "updated_at": row["updated_at"],
    }
    if payload["seed_source"] == "ai":
        payload["seed_applied"] = True
    return payload


def _loads_optional(raw: Any, *, default: Any) -> Any:
    if raw in {None, ""}:
        return default
    try:
        return json.loads(str(raw))
    except json.JSONDecodeError:
        return default


def _dumps_optional(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True)
