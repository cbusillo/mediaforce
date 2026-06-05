import json
from typing import Any

from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import literal_column
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import true
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.db import DBClient
from mediaforce.core.db import DBRow
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.db_tables import encode_queue_state
from mediaforce.core.type_defs import int_value

DEFAULT_QUEUE_NAME = "heavy"
DEFAULT_SCHEDULER_POLICY = {
    "mode": "anytime",
    "start_hour": 22,
    "end_hour": 8,
    "timezone": "local",
}
DISPLAY_ENCODE_JOB_KINDS = ("single", "folder")
RUNNABLE_ENCODE_JOB_KINDS = ("single", "shard")
ACTIVE_ENCODE_JOB_STATUSES = ("queued", "retry_backoff", "running")
QUEUED_ENCODE_JOB_STATUSES = ("queued", "retry_backoff")
RECENT_ENCODE_JOB_STATUSES = ("completed", "failed", "stopped", "needs_attention")

_PERSISTED_ENCODE_HOST_KEYS = (
    "allowed_libraries",
    "capabilities",
    "failure_streak",
    "ffmpeg_path",
    "host",
    "key",
    "label",
    "media_access",
    "mode",
    "platform",
    "priority",
    "repo_path",
    "schedule_profile",
    "source_roots",
    "staging_root",
    "start_command",
    "start_timeout_seconds",
    "stop_command",
    "utc_offset_minutes",
    "videotoolbox_available",
)
_ENCODE_JOB_HOST_REPAIR_LENGTH_THRESHOLD = 65_536


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


def persisted_encode_host_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    host_payload = payload if isinstance(payload, dict) else {}
    persisted: dict[str, Any] = {}
    for key in _PERSISTED_ENCODE_HOST_KEYS:
        value = host_payload.get(key)
        if key in {"allowed_libraries", "capabilities"}:
            if isinstance(value, list):
                persisted[key] = [str(item) for item in value if isinstance(item, str)]
            continue
        if key == "source_roots":
            if isinstance(value, dict):
                persisted[key] = {
                    str(root_key): str(root_value)
                    for root_key, root_value in value.items()
                    if isinstance(root_key, str) and root_value is not None
                }
            continue
        if key in {"priority", "start_timeout_seconds", "utc_offset_minutes"}:
            if value in {None, ""}:
                continue
            try:
                persisted[key] = int(value)
            except (TypeError, ValueError):
                continue
            continue
        if key == "failure_streak":
            if value in {None, ""}:
                continue
            try:
                persisted[key] = max(1, int(value))
            except (TypeError, ValueError):
                continue
            continue
        if key == "videotoolbox_available":
            if value is None:
                continue
            persisted[key] = bool(value)
            continue
        if value is None:
            continue
        text = str(value).strip()
        if text:
            persisted[key] = text
    return persisted


def repair_persisted_encode_job_hosts(connection: DBClient) -> int:
    rows = connection.execute(
        select(encode_jobs.c.job_id, encode_jobs.c.host_json, encode_jobs.c.last_host_json)
    ).mappings().fetchall()
    repaired = 0
    for row in rows:
        job_id = str(row["job_id"])
        host_payload, host_payload_malformed = _loads_host_payload_for_repair(row["host_json"])
        last_host_payload, last_host_payload_malformed = _loads_host_payload_for_repair(row["last_host_json"])
        sanitized_host = persisted_encode_host_payload(host_payload)
        sanitized_last_host = persisted_encode_host_payload(last_host_payload)
        if not str(sanitized_host.get("key") or sanitized_host.get("host") or "").strip():
            sanitized_host = {
                **sanitized_last_host,
                **sanitized_host,
            }
        if (
            not host_payload_malformed
            and not last_host_payload_malformed
            and host_payload == sanitized_host
            and last_host_payload == sanitized_last_host
        ):
            continue
        connection.execute(
            update(encode_jobs)
            .where(encode_jobs.c.job_id == job_id)
            .values(
                host_json=json.dumps(sanitized_host, sort_keys=True),
                last_host_json=json.dumps(sanitized_last_host, sort_keys=True),
            )
        )
        repaired += 1
    return repaired


def load_encode_job(connection: DBClient, job_id: str) -> dict[str, Any] | None:
    row = connection.execute(_encode_job_select().where(encode_jobs.c.job_id == job_id)).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def load_latest_encode_job(connection: DBClient, prefix: str) -> dict[str, Any] | None:
    row = connection.execute(
        _encode_job_select()
        .where(encode_jobs.c.prefix == prefix)
        .where(encode_jobs.c.job_kind.in_(DISPLAY_ENCODE_JOB_KINDS))
        .order_by(encode_jobs.c.created_at.desc(), _rowid_column().desc())
        .limit(1)
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def load_active_encode_job_for_prefix(connection: DBClient, prefix: str) -> dict[str, Any] | None:
    normalized_prefix = _normalize_prefix(prefix)
    row = connection.execute(
        _encode_job_select()
        .where(_prefix_overlap_filter(normalized_prefix))
        .where(encode_jobs.c.job_kind.in_(DISPLAY_ENCODE_JOB_KINDS))
        .where(encode_jobs.c.status.in_(ACTIVE_ENCODE_JOB_STATUSES))
        .order_by(encode_jobs.c.created_at.desc(), _rowid_column().desc())
        .limit(1)
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def _normalize_prefix(prefix: str) -> str:
    return prefix.strip().strip("/")


def _prefix_overlap_filter(prefix: str) -> Any:
    if not prefix:
        return true()
    return or_(
        encode_jobs.c.prefix == prefix,
        encode_jobs.c.prefix.like(f"{prefix}/%"),
        literal(prefix).like(encode_jobs.c.prefix + "/%"),
    )


def clear_terminal_encode_jobs_for_prefix(connection: DBClient, prefix: str) -> None:
    connection.execute(
        delete(encode_jobs)
        .where(encode_jobs.c.prefix == prefix)
        .where(or_(encode_jobs.c.job_kind != "shard", encode_jobs.c.status != "completed"))
        .where(
            or_(
                encode_jobs.c.status.in_(RECENT_ENCODE_JOB_STATUSES),
                encode_jobs.c.job_kind == "shard",
            )
        )
    )


def load_latest_terminal_encode_job_for_prefix(connection: DBClient, prefix: str) -> dict[str, Any] | None:
    row = connection.execute(
        _encode_job_select()
        .where(encode_jobs.c.prefix == prefix)
        .where(encode_jobs.c.status.in_(RECENT_ENCODE_JOB_STATUSES))
        .where(or_(encode_jobs.c.job_kind != "shard", encode_jobs.c.status != "completed"))
        .order_by(encode_jobs.c.created_at.desc(), _rowid_column().desc())
        .limit(1)
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def load_active_encode_job(connection: DBClient) -> dict[str, Any] | None:
    row = connection.execute(
        _encode_job_select()
        .where(encode_jobs.c.status == "running")
        .where(encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS))
        .order_by(encode_jobs.c.started_at.desc(), _rowid_column().desc())
        .limit(1)
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def load_next_queued_job(connection: DBClient) -> dict[str, Any] | None:
    row = connection.execute(
        _encode_job_select()
        .where(encode_jobs.c.status == "queued")
        .where(encode_jobs.c.job_kind.in_(RUNNABLE_ENCODE_JOB_KINDS))
        .order_by(encode_jobs.c.created_at.asc(), _rowid_column().asc())
        .limit(1)
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def queue_position(connection: DBClient, job_id: str) -> tuple[int, int] | None:
    row = connection.execute(
        select(encode_jobs.c.created_at, _rowid_column())
        .where(encode_jobs.c.job_id == job_id)
        .where(encode_jobs.c.job_kind.in_(DISPLAY_ENCODE_JOB_KINDS))
        .where(encode_jobs.c.status.in_(QUEUED_ENCODE_JOB_STATUSES))
    ).mappings().fetchone()
    if row is None:
        return None
    created_at = str(row["created_at"])
    rowid = int(row["rowid"])
    position = int(connection.execute(
        select(func.count())
        .select_from(encode_jobs)
        .where(encode_jobs.c.job_kind.in_(DISPLAY_ENCODE_JOB_KINDS))
        .where(encode_jobs.c.status.in_(QUEUED_ENCODE_JOB_STATUSES))
        .where((encode_jobs.c.created_at < created_at) | ((encode_jobs.c.created_at == created_at) & (_rowid_column() <= rowid)))
    ).scalar_one())
    total = int(connection.execute(
        select(func.count())
        .select_from(encode_jobs)
        .where(encode_jobs.c.job_kind.in_(DISPLAY_ENCODE_JOB_KINDS))
        .where(encode_jobs.c.status.in_(QUEUED_ENCODE_JOB_STATUSES))
    ).scalar_one())
    return position, total


def list_encode_jobs(
        connection: DBClient,
        *,
        statuses: tuple[str, ...],
        limit: int = 8,
        job_kinds: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    statement = _encode_job_select().where(encode_jobs.c.status.in_(statuses))
    if job_kinds is not None:
        statement = statement.where(encode_jobs.c.job_kind.in_(job_kinds))
    rows = connection.execute(
        statement.order_by(encode_jobs.c.created_at.asc(), _rowid_column().asc()).limit(limit)
    ).mappings().fetchall()
    return [_hydrate_job(row) for row in rows]


def list_child_encode_jobs(connection: DBClient, parent_job_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        _encode_job_select()
        .where(encode_jobs.c.parent_job_id == parent_job_id)
        .order_by(encode_jobs.c.created_at.asc(), _rowid_column().asc())
    ).mappings().fetchall()
    return [_hydrate_job(row) for row in rows]


def summarize_encode_queue(connection: DBClient) -> dict[str, Any]:
    queued = list_encode_jobs(connection, statuses=QUEUED_ENCODE_JOB_STATUSES, job_kinds=DISPLAY_ENCODE_JOB_KINDS)
    running = list_encode_jobs(connection, statuses=("running",), limit=2, job_kinds=DISPLAY_ENCODE_JOB_KINDS)
    recent = list_encode_jobs(connection, statuses=RECENT_ENCODE_JOB_STATUSES, limit=6, job_kinds=DISPLAY_ENCODE_JOB_KINDS)
    counts = {
        "queued": _count_jobs(connection, statuses=QUEUED_ENCODE_JOB_STATUSES, job_kinds=DISPLAY_ENCODE_JOB_KINDS),
        "running": _count_jobs(connection, statuses=("running",), job_kinds=DISPLAY_ENCODE_JOB_KINDS),
        "retry_backoff": _count_jobs(connection, statuses=("retry_backoff",), job_kinds=DISPLAY_ENCODE_JOB_KINDS),
        "needs_attention": _count_jobs(connection, statuses=("needs_attention",), job_kinds=DISPLAY_ENCODE_JOB_KINDS),
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


def _count_jobs(connection: DBClient, *, statuses: tuple[str, ...], job_kinds: tuple[str, ...] | None = None) -> int:
    statement = select(func.count()).select_from(encode_jobs).where(encode_jobs.c.status.in_(statuses))
    if job_kinds is not None:
        statement = statement.where(encode_jobs.c.job_kind.in_(job_kinds))
    return int(connection.execute(statement).scalar_one())


def _encode_job_select() -> Any:
    return select(*encode_jobs.c, _rowid_column())


def _rowid_column() -> Any:
    return literal_column("rowid").label("rowid")


def _serialize_encode_job(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": payload["job_id"],
        "prefix": payload["prefix"],
        "job_kind": str(payload.get("job_kind") or "single"),
        "parent_job_id": payload.get("parent_job_id"),
        "status": payload["status"],
        "manifest_path": payload["manifest_path"],
        "manifest_indexes_json": (
            json.dumps(payload.get("manifest_indexes") or [], separators=(",", ":"))
            if payload.get("manifest_indexes") is not None
            else None
        ),
        "item_count": int_value(payload.get("item_count")),
        "saved_profile_path": payload.get("saved_profile_path"),
        "host_json": json.dumps(persisted_encode_host_payload(payload.get("host")), sort_keys=True),
        "last_host_json": json.dumps(persisted_encode_host_payload(payload.get("last_host")), sort_keys=True),
        "notes": str(payload.get("notes") or ""),
        "process_pid": payload.get("process_pid"),
        "error": payload.get("error"),
        "bypass_schedule": int(bool(payload.get("bypass_schedule"))),
        "attempt_count": int_value(payload.get("attempt_count")),
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
        "job_kind": str(row["job_kind"] or "single"),
        "parent_job_id": row["parent_job_id"],
        "status": str(row["status"]),
        "manifest_path": str(row["manifest_path"]),
        "manifest_indexes": json.loads(str(row["manifest_indexes_json"] or "[]")) if row["manifest_indexes_json"] else None,
        "item_count": int(row["item_count"] or 0),
        "saved_profile_path": row["saved_profile_path"],
        "host": _loads_host_payload(row["host_json"]),
        "last_host": _loads_host_payload(row["last_host_json"]),
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


def _loads_host_payload(raw_payload: Any) -> dict[str, Any]:
    payload, _ = _loads_host_payload_for_repair(raw_payload)
    return payload


def _loads_host_payload_for_repair(raw_payload: Any) -> tuple[dict[str, Any], bool]:
    if isinstance(raw_payload, str) and not raw_payload.strip():
        return {}, True
    try:
        payload = json.loads(str(raw_payload or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}, True
    if not isinstance(payload, dict):
        return {}, True
    return payload, False
