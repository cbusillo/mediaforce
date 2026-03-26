from __future__ import annotations

import json
import sqlite3
from typing import Any


DEFAULT_QUEUE_NAME = "heavy"
DEFAULT_SCHEDULER_POLICY = {
    "mode": "anytime",
    "start_hour": 22,
    "end_hour": 8,
    "timezone": "local",
}
QUEUED_ENCODE_JOB_STATUSES = ("queued", "retry_backoff")
RECENT_ENCODE_JOB_STATUSES = ("completed", "failed", "stopped", "needs_attention")


def ensure_queue_state(connection: sqlite3.Connection, *, queue_name: str = DEFAULT_QUEUE_NAME, updated_at: str) -> None:
    connection.execute(
        """
        INSERT INTO encode_queue_state(queue_name, is_paused, stop_requested, active_job_id, updated_at)
        VALUES (?, 0, 0, NULL, ?)
        ON CONFLICT(queue_name) DO NOTHING
        """,
        (queue_name, updated_at),
    )


def load_queue_state(connection: sqlite3.Connection, *, queue_name: str = DEFAULT_QUEUE_NAME) -> dict[str, Any]:
    row = connection.execute(
        "SELECT queue_name, is_paused, stop_requested, active_job_id, updated_at FROM encode_queue_state WHERE queue_name = ?",
        (queue_name,),
    ).fetchone()
    if row is None:
        return {
            "queue_name": queue_name,
            "is_paused": False,
            "stop_requested": False,
            "active_job_id": None,
            "updated_at": None,
            "scheduler": dict(DEFAULT_SCHEDULER_POLICY),
        }
    return {
        "queue_name": str(row["queue_name"]),
        "is_paused": bool(row["is_paused"]),
        "stop_requested": bool(row["stop_requested"]),
        "active_job_id": row["active_job_id"],
        "updated_at": row["updated_at"],
        "scheduler": dict(DEFAULT_SCHEDULER_POLICY),
    }


def save_queue_state(connection: sqlite3.Connection, payload: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO encode_queue_state(queue_name, is_paused, stop_requested, active_job_id, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(queue_name) DO UPDATE SET
            is_paused = excluded.is_paused,
            stop_requested = excluded.stop_requested,
            active_job_id = excluded.active_job_id,
            updated_at = excluded.updated_at
        """,
        (
            payload["queue_name"],
            int(bool(payload.get("is_paused"))),
            int(bool(payload.get("stop_requested"))),
            payload.get("active_job_id"),
            payload["updated_at"],
        ),
    )


def save_encode_job(connection: sqlite3.Connection, payload: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO encode_jobs(
            job_id, prefix, status, manifest_path, item_count, saved_profile_path,
            host_json, last_host_json, notes, process_pid, error, bypass_schedule,
            attempt_count, leased_at, lease_expires_at, heartbeat_at, worker_id,
            retry_not_before, waiting_reason, terminal_reason, last_failure_kind,
            last_failure_at, host_cooldown_until, created_at, started_at, finished_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            prefix = excluded.prefix,
            status = excluded.status,
            manifest_path = excluded.manifest_path,
            item_count = excluded.item_count,
            saved_profile_path = excluded.saved_profile_path,
            host_json = excluded.host_json,
            last_host_json = excluded.last_host_json,
            notes = excluded.notes,
            process_pid = excluded.process_pid,
            error = excluded.error,
            bypass_schedule = excluded.bypass_schedule,
            attempt_count = excluded.attempt_count,
            leased_at = excluded.leased_at,
            lease_expires_at = excluded.lease_expires_at,
            heartbeat_at = excluded.heartbeat_at,
            worker_id = excluded.worker_id,
            retry_not_before = excluded.retry_not_before,
            waiting_reason = excluded.waiting_reason,
            terminal_reason = excluded.terminal_reason,
            last_failure_kind = excluded.last_failure_kind,
            last_failure_at = excluded.last_failure_at,
            host_cooldown_until = excluded.host_cooldown_until,
            created_at = excluded.created_at,
            started_at = excluded.started_at,
            finished_at = excluded.finished_at,
            updated_at = excluded.updated_at
        """,
        (
            payload["job_id"],
            payload["prefix"],
            payload["status"],
            payload["manifest_path"],
            int(payload.get("item_count") or 0),
            payload.get("saved_profile_path"),
            json.dumps(payload.get("host") or {}, sort_keys=True),
            json.dumps(payload.get("last_host") or {}, sort_keys=True),
            str(payload.get("notes") or ""),
            payload.get("process_pid"),
            payload.get("error"),
            int(bool(payload.get("bypass_schedule"))),
            int(payload.get("attempt_count") or 0),
            payload.get("leased_at"),
            payload.get("lease_expires_at"),
            payload.get("heartbeat_at"),
            payload.get("worker_id"),
            payload.get("retry_not_before"),
            payload.get("waiting_reason"),
            payload.get("terminal_reason"),
            payload.get("last_failure_kind"),
            payload.get("last_failure_at"),
            payload.get("host_cooldown_until"),
            payload["created_at"],
            payload.get("started_at"),
            payload.get("finished_at"),
            payload["updated_at"],
        ),
    )


def load_encode_job(connection: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM encode_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _hydrate_job(row) if row is not None else None


def load_latest_encode_job(connection: sqlite3.Connection, prefix: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM encode_jobs WHERE prefix = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (prefix,),
    ).fetchone()
    return _hydrate_job(row) if row is not None else None


def load_active_encode_job(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM encode_jobs WHERE status = 'running' ORDER BY started_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return _hydrate_job(row) if row is not None else None


def load_next_queued_job(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM encode_jobs WHERE status = 'queued' ORDER BY created_at ASC, rowid ASC LIMIT 1"
    ).fetchone()
    return _hydrate_job(row) if row is not None else None


def queue_position(connection: sqlite3.Connection, job_id: str) -> tuple[int, int] | None:
    row = connection.execute(
        "SELECT created_at, rowid FROM encode_jobs WHERE job_id = ? AND status IN ('queued', 'retry_backoff')",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    created_at = str(row["created_at"])
    rowid = int(row["rowid"])
    position = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM encode_jobs
            WHERE status IN ('queued', 'retry_backoff')
              AND (created_at < ? OR (created_at = ? AND rowid <= ?))
            """,
            (created_at, created_at, rowid),
        ).fetchone()[0]
    )
    total = int(
        connection.execute(
            "SELECT COUNT(*) FROM encode_jobs WHERE status IN ('queued', 'retry_backoff')"
        ).fetchone()[0]
    )
    return position, total


def list_encode_jobs(connection: sqlite3.Connection, *, statuses: tuple[str, ...], limit: int = 8) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in statuses)
    rows = connection.execute(
        f"SELECT * FROM encode_jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC, rowid ASC LIMIT ?",
        (*statuses, limit),
    ).fetchall()
    return [_hydrate_job(row) for row in rows]


def summarize_encode_queue(connection: sqlite3.Connection) -> dict[str, Any]:
    queued = list_encode_jobs(connection, statuses=QUEUED_ENCODE_JOB_STATUSES, limit=8)
    running = list_encode_jobs(connection, statuses=("running",), limit=2)
    recent = list_encode_jobs(connection, statuses=RECENT_ENCODE_JOB_STATUSES, limit=6)
    counts = {
        "queued": int(
            connection.execute(
                "SELECT COUNT(*) FROM encode_jobs WHERE status IN ('queued', 'retry_backoff')"
            ).fetchone()[0]
        ),
        "running": int(connection.execute("SELECT COUNT(*) FROM encode_jobs WHERE status = 'running'").fetchone()[0]),
        "retry_backoff": int(
            connection.execute("SELECT COUNT(*) FROM encode_jobs WHERE status = 'retry_backoff'").fetchone()[0]
        ),
        "needs_attention": int(
            connection.execute("SELECT COUNT(*) FROM encode_jobs WHERE status = 'needs_attention'").fetchone()[0]
        ),
    }
    state = load_queue_state(connection)
    return {
        "state": state,
        "queued": queued,
        "running": running,
        "recent": recent,
        "queued_count": counts["queued"],
        "running_count": counts["running"],
        "retry_backoff_count": counts["retry_backoff"],
        "needs_attention_count": counts["needs_attention"],
    }


def _hydrate_job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": str(row["job_id"]),
        "prefix": str(row["prefix"]),
        "status": str(row["status"]),
        "manifest_path": str(row["manifest_path"]),
        "item_count": int(row["item_count"] or 0),
        "saved_profile_path": row["saved_profile_path"],
        "host": json.loads(str(row["host_json"] or "{}")),
        "last_host": json.loads(str(row["last_host_json"] or "{}")),
        "notes": str(row["notes"] or ""),
        "process_pid": row["process_pid"],
        "error": row["error"],
        "bypass_schedule": bool(row["bypass_schedule"]),
        "attempt_count": int(row["attempt_count"] or 0),
        "leased_at": row["leased_at"],
        "lease_expires_at": row["lease_expires_at"],
        "heartbeat_at": row["heartbeat_at"],
        "worker_id": row["worker_id"],
        "retry_not_before": row["retry_not_before"],
        "waiting_reason": row["waiting_reason"],
        "terminal_reason": row["terminal_reason"],
        "last_failure_kind": row["last_failure_kind"],
        "last_failure_at": row["last_failure_at"],
        "host_cooldown_until": row["host_cooldown_until"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "updated_at": row["updated_at"],
    }
