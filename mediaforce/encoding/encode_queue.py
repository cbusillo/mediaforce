import json
from typing import Any

from sqlalchemy import func
from sqlalchemy import literal_column
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.db import DBClient
from mediaforce.core.db import DBRow
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.db_tables import encode_queue_state

DEFAULT_QUEUE_NAME = "heavy"
DEFAULT_SCHEDULER_POLICY = {
    "mode": "anytime",
    "start_hour": 22,
    "end_hour": 8,
    "timezone": "local",
}
QUEUED_ENCODE_JOB_STATUSES = ("queued", "retry_backoff")
RECENT_ENCODE_JOB_STATUSES = ("completed", "failed", "stopped", "needs_attention")


def ensure_queue_state(connection: DBClient, *, queue_name: str = DEFAULT_QUEUE_NAME, updated_at: str) -> None:
    statement = sqlite_insert(encode_queue_state).values(
        queue_name=queue_name,
        is_paused=0,
        stop_requested=0,
        active_job_id=None,
        updated_at=updated_at,
    )
    connection.execute(statement.on_conflict_do_nothing(index_elements=[encode_queue_state.c.queue_name]))


def load_queue_state(connection: DBClient, *, queue_name: str = DEFAULT_QUEUE_NAME) -> dict[str, Any]:
    row = connection.execute(
        select(
            encode_queue_state.c.queue_name,
            encode_queue_state.c.is_paused,
            encode_queue_state.c.stop_requested,
            encode_queue_state.c.active_job_id,
            encode_queue_state.c.updated_at,
        ).where(encode_queue_state.c.queue_name == queue_name)
    ).mappings().fetchone()
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


def save_queue_state(connection: DBClient, payload: dict[str, Any]) -> None:
    values = {
        "queue_name": payload["queue_name"],
        "is_paused": int(bool(payload.get("is_paused"))),
        "stop_requested": int(bool(payload.get("stop_requested"))),
        "active_job_id": payload.get("active_job_id"),
        "updated_at": payload["updated_at"],
    }
    statement = sqlite_insert(encode_queue_state).values(**values)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[encode_queue_state.c.queue_name],
            set_={
                "is_paused": statement.excluded.is_paused,
                "stop_requested": statement.excluded.stop_requested,
                "active_job_id": statement.excluded.active_job_id,
                "updated_at": statement.excluded.updated_at,
            },
        )
    )


def save_encode_job(connection: DBClient, payload: dict[str, Any]) -> None:
    values = _serialize_encode_job(payload)
    statement = sqlite_insert(encode_jobs).values(**values)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[encode_jobs.c.job_id],
            set_={column: getattr(statement.excluded, column) for column in values if column != "job_id"},
        )
    )


def load_encode_job(connection: DBClient, job_id: str) -> dict[str, Any] | None:
    row = connection.execute(_encode_job_select().where(encode_jobs.c.job_id == job_id)).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def load_latest_encode_job(connection: DBClient, prefix: str) -> dict[str, Any] | None:
    row = connection.execute(
        _encode_job_select()
        .where(encode_jobs.c.prefix == prefix)
        .order_by(encode_jobs.c.created_at.desc(), _rowid_column().desc())
        .limit(1)
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def load_active_encode_job(connection: DBClient) -> dict[str, Any] | None:
    row = connection.execute(
        _encode_job_select()
        .where(encode_jobs.c.status == "running")
        .order_by(encode_jobs.c.started_at.desc(), _rowid_column().desc())
        .limit(1)
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def load_next_queued_job(connection: DBClient) -> dict[str, Any] | None:
    row = connection.execute(
        _encode_job_select()
        .where(encode_jobs.c.status == "queued")
        .order_by(encode_jobs.c.created_at.asc(), _rowid_column().asc())
        .limit(1)
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def queue_position(connection: DBClient, job_id: str) -> tuple[int, int] | None:
    row = connection.execute(
        select(encode_jobs.c.created_at, _rowid_column())
        .where(encode_jobs.c.job_id == job_id)
        .where(encode_jobs.c.status.in_(QUEUED_ENCODE_JOB_STATUSES))
    ).mappings().fetchone()
    if row is None:
        return None
    created_at = str(row["created_at"])
    rowid = int(row["rowid"])
    position = int(
        connection.execute(
            select(func.count())
            .select_from(encode_jobs)
            .where(encode_jobs.c.status.in_(QUEUED_ENCODE_JOB_STATUSES))
            .where((encode_jobs.c.created_at < created_at) | ((encode_jobs.c.created_at == created_at) & (_rowid_column() <= rowid)))
        ).fetchone()[0]
    )
    total = int(
        connection.execute(
            select(func.count())
            .select_from(encode_jobs)
            .where(encode_jobs.c.status.in_(QUEUED_ENCODE_JOB_STATUSES))
        ).fetchone()[0]
    )
    return position, total


def list_encode_jobs(connection: DBClient, *, statuses: tuple[str, ...], limit: int = 8) -> list[dict[str, Any]]:
    rows = connection.execute(
        _encode_job_select()
        .where(encode_jobs.c.status.in_(statuses))
        .order_by(encode_jobs.c.created_at.asc(), _rowid_column().asc())
        .limit(limit)
    ).mappings().fetchall()
    return [_hydrate_job(row) for row in rows]


def summarize_encode_queue(connection: DBClient) -> dict[str, Any]:
    queued = list_encode_jobs(connection, statuses=QUEUED_ENCODE_JOB_STATUSES)
    running = list_encode_jobs(connection, statuses=("running",), limit=2)
    recent = list_encode_jobs(connection, statuses=RECENT_ENCODE_JOB_STATUSES, limit=6)
    counts = {
        "queued": _count_jobs(connection, statuses=QUEUED_ENCODE_JOB_STATUSES),
        "running": _count_jobs(connection, statuses=("running",)),
        "retry_backoff": _count_jobs(connection, statuses=("retry_backoff",)),
        "needs_attention": _count_jobs(connection, statuses=("needs_attention",)),
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


def _count_jobs(connection: DBClient, *, statuses: tuple[str, ...]) -> int:
    return int(
        connection.execute(
            select(func.count()).select_from(encode_jobs).where(encode_jobs.c.status.in_(statuses))
        ).fetchone()[0]
    )


def _encode_job_select() -> Any:
    return select(*encode_jobs.c, _rowid_column())


def _rowid_column() -> Any:
    return literal_column("rowid").label("rowid")


def _serialize_encode_job(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": payload["job_id"],
        "prefix": payload["prefix"],
        "status": payload["status"],
        "manifest_path": payload["manifest_path"],
        "item_count": int(payload.get("item_count") or 0),
        "saved_profile_path": payload.get("saved_profile_path"),
        "host_json": json.dumps(payload.get("host") or {}, sort_keys=True),
        "last_host_json": json.dumps(payload.get("last_host") or {}, sort_keys=True),
        "notes": str(payload.get("notes") or ""),
        "process_pid": payload.get("process_pid"),
        "error": payload.get("error"),
        "bypass_schedule": int(bool(payload.get("bypass_schedule"))),
        "attempt_count": int(payload.get("attempt_count") or 0),
        "leased_at": payload.get("leased_at"),
        "lease_expires_at": payload.get("lease_expires_at"),
        "heartbeat_at": payload.get("heartbeat_at"),
        "worker_id": payload.get("worker_id"),
        "retry_not_before": payload.get("retry_not_before"),
        "waiting_reason": payload.get("waiting_reason"),
        "terminal_reason": payload.get("terminal_reason"),
        "last_failure_kind": payload.get("last_failure_kind"),
        "last_failure_at": payload.get("last_failure_at"),
        "host_cooldown_until": payload.get("host_cooldown_until"),
        "progress_json": json.dumps(payload.get("progress") or {}, sort_keys=True) if payload.get("progress") is not None else None,
        "created_at": payload["created_at"],
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "updated_at": payload["updated_at"],
    }


def _hydrate_job(row: DBRow) -> dict[str, Any]:
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
        "progress": json.loads(str(row["progress_json"] or "{}")) if row["progress_json"] else None,
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "updated_at": row["updated_at"],
    }
