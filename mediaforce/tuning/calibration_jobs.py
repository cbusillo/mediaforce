import json
from typing import Any
from typing import TypeVar

from sqlalchemy import func
from sqlalchemy import literal_column
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.db import DBClient
from mediaforce.core.db import DBRow
from mediaforce.core.db_tables import calibration_jobs
from mediaforce.core.type_defs import JSONValue

T = TypeVar("T")

ACTIVE_JOB_STATUSES = {"queued", "running", "pending_review"}


def load_latest_job(connection: DBClient, prefix: str) -> dict[str, Any] | None:
    row = connection.execute(
        _calibration_job_select()
        .where(calibration_jobs.c.prefix == prefix)
        .order_by(calibration_jobs.c.created_at.desc(), _rowid_column().desc())
        .limit(1)
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def load_latest_retryable_sample_job(connection: DBClient, prefix: str) -> dict[str, Any] | None:
    row = connection.execute(
        _calibration_job_select()
        .where(calibration_jobs.c.prefix == prefix)
        .where(calibration_jobs.c.lane == "sample")
        .where(calibration_jobs.c.status.in_(("failed", "stopped")))
        .where(calibration_jobs.c.action.in_(("baseline", "ai_tune")))
        .order_by(calibration_jobs.c.created_at.desc(), _rowid_column().desc())
        .limit(1)
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def load_latest_sample_job(connection: DBClient, prefix: str) -> dict[str, Any] | None:
    row = connection.execute(
        _calibration_job_select()
        .where(calibration_jobs.c.prefix == prefix)
        .where(calibration_jobs.c.lane == "sample")
        .order_by(calibration_jobs.c.created_at.desc(), _rowid_column().desc())
        .limit(1)
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def load_job(connection: DBClient, job_id: str) -> dict[str, Any] | None:
    row = connection.execute(_calibration_job_select().where(calibration_jobs.c.job_id == job_id)).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def load_active_job(connection: DBClient, prefix: str) -> dict[str, Any] | None:
    row = connection.execute(
        _calibration_job_select()
        .where(calibration_jobs.c.prefix == prefix)
        .where(calibration_jobs.c.status.in_(tuple(ACTIVE_JOB_STATUSES)))
        .order_by(calibration_jobs.c.created_at.desc(), _rowid_column().desc())
        .limit(1)
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def list_queued_jobs(connection: DBClient) -> list[dict[str, Any]]:
    rows = connection.execute(
        _calibration_job_select()
        .where(calibration_jobs.c.status == "queued")
        .order_by(calibration_jobs.c.created_at.asc(), _rowid_column().asc())
    ).mappings().fetchall()
    return [_hydrate_job(row) for row in rows]


def claim_next_queued_calibration_job(
        connection: DBClient,
        *,
        lane: str,
        owner_pid: int,
        started_at: str,
        excluded_prefixes: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    candidate_query = (
        select(calibration_jobs.c.job_id)
        .where(calibration_jobs.c.lane == lane)
        .where(calibration_jobs.c.status == "queued")
        .order_by(calibration_jobs.c.created_at.asc(), _rowid_column().asc())
        .limit(1)
    )
    if excluded_prefixes:
        candidate_query = candidate_query.where(calibration_jobs.c.prefix.not_in(excluded_prefixes))
    candidate_job_id = candidate_query.scalar_subquery()
    row = connection.execute(
        update(calibration_jobs)
        .where(calibration_jobs.c.job_id == candidate_job_id)
        .where(calibration_jobs.c.status == "queued")
        .values(
            status="running",
            owner_pid=owner_pid,
            started_at=func.coalesce(calibration_jobs.c.started_at, started_at),
            finished_at=None,
            error=None,
            updated_at=started_at,
        )
        .returning(*calibration_jobs.c, _rowid_column())
    ).mappings().fetchone()
    return _hydrate_job(row) if row is not None else None


def list_queue_summary(connection: DBClient, *, limit_per_lane: int = 6) -> dict[str, Any]:
    rows = connection.execute(
        _calibration_job_select()
        .where(calibration_jobs.c.status.in_(("queued", "running", "pending_review")))
        .order_by(calibration_jobs.c.created_at.asc(), _rowid_column().asc())
    ).mappings().fetchall()
    summary: dict[str, Any] = {
        "sample": {"running": [], "queued": [], "pending_review": [], "running_count": 0, "queued_count": 0,
                   "pending_review_count": 0},
        "full": {"running": [], "queued": [], "pending_review": [], "running_count": 0, "queued_count": 0,
                 "pending_review_count": 0},
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


def queue_position(connection: DBClient, job_id: str) -> tuple[int, int] | None:
    row = connection.execute(
        select(calibration_jobs.c.lane, calibration_jobs.c.created_at, _rowid_column())
        .where(calibration_jobs.c.job_id == job_id)
        .where(calibration_jobs.c.status == "queued")
    ).mappings().fetchone()
    if row is None:
        return None
    lane = str(row["lane"])
    created_at = str(row["created_at"])
    rowid = int(row["rowid"])
    position = int(
        connection.execute(
            select(func.count())
            .select_from(calibration_jobs)
            .where(calibration_jobs.c.lane == lane)
            .where(calibration_jobs.c.status == "queued")
            .where(
                (calibration_jobs.c.created_at < created_at)
                | ((calibration_jobs.c.created_at == created_at) & (_rowid_column() <= rowid))
            )
        ).scalar_one()
    )
    total = int(
        connection.execute(
            select(func.count())
            .select_from(calibration_jobs)
            .where(calibration_jobs.c.lane == lane)
            .where(calibration_jobs.c.status == "queued")
        ).scalar_one()
    )
    return position, total


def save_job(connection: DBClient, payload: dict[str, Any]) -> None:
    values = _serialize_job(payload)
    statement = sqlite_insert(calibration_jobs).values(**values)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[calibration_jobs.c.job_id],
            set_={column: getattr(statement.excluded, column) for column in values if column != "job_id"},
        )
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
        "updated_at": str(
            payload.get("updated_at") or payload.get("finished_at") or payload.get("started_at") or payload["created_at"]
        ),
    }


def _hydrate_job(row: DBRow) -> dict[str, Any]:
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


def _loads_optional(raw: JSONValue, *, default: T) -> JSONValue | T:
    if raw in {None, ""}:
        return default
    try:
        return json.loads(str(raw))
    except json.JSONDecodeError:
        return default


def _dumps_optional(value: JSONValue) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True)


def _calibration_job_select() -> Any:
    return select(*calibration_jobs.c, _rowid_column())


def _rowid_column() -> Any:
    return literal_column("rowid").label("rowid")
