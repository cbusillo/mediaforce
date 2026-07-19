import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Sequence

from sqlalchemy import bindparam, case, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import evidence_queue_state, library_item_evidence_state, library_items
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.core.utils import timestamp
from mediaforce.library.background_work import background_work_is_paused
from mediaforce.library.evidence_state import EVIDENCE_KINDS, EVIDENCE_STATE_ANALYSIS_REQUIRED, \
    EVIDENCE_STATE_CLASSIFICATION_REQUIRED, EvidenceKind
from mediaforce.library.media_scopes import MediaScope, resolve_media_scope, scope_rel_path_filter

DEFAULT_EVIDENCE_QUEUE_NAME = "evidence"
DEFAULT_EVIDENCE_BATCH_LIMIT = 25

EVIDENCE_QUEUE_ACTIVE_STATUSES = ("queued", "running", "paused", "cancel_requested")
EVIDENCE_WORK_CLAIMABLE_STATUSES = ("queued", "retry_wait", "waiting_source")
EVIDENCE_WORK_ACTIVE_STATUSES = (*EVIDENCE_WORK_CLAIMABLE_STATUSES, "running")
EVIDENCE_WORK_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
EvidenceHeartbeatStatus = Literal["active", "cancel_requested", "claim_lost"]


class EvidenceQueueConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EvidenceWorkClaim:
    batch_id: str
    library_item_id: int
    evidence_kind: EvidenceKind
    evidence_state: str
    evidence_reason: str | None
    source_path: str
    rel_path: str
    media_root: str
    expected_source_fingerprint: str | None
    uses_content_fingerprint: bool
    duration_seconds: float | None
    canonical_summary_json: str | None
    attempt_count: int
    worker_id: str


def ensure_evidence_queue_state(
        connection: DBClient,
        *,
        queue_name: str = DEFAULT_EVIDENCE_QUEUE_NAME,
        updated_at: str | None = None,
) -> None:
    now_iso = updated_at or timestamp()
    statement = sqlite_insert(evidence_queue_state).values(
        queue_name=queue_name,
        batch_id=None,
        status="idle",
        scope_json=None,
        evidence_kinds_json="[]",
        is_paused=0,
        cancel_requested=0,
        item_count=0,
        completed_count=0,
        failed_count=0,
        cancelled_count=0,
        created_at=None,
        started_at=None,
        finished_at=None,
        updated_at=now_iso,
    )
    connection.execute(
        statement.on_conflict_do_nothing(index_elements=[evidence_queue_state.c.queue_name])
    )


def load_evidence_queue_state(
        connection: DBClient,
        *,
        queue_name: str = DEFAULT_EVIDENCE_QUEUE_NAME,
) -> dict[str, Any]:
    row = connection.execute(
        select(evidence_queue_state).where(evidence_queue_state.c.queue_name == queue_name)
    ).mappings().fetchone()
    if row is None:
        return {
            "queue_name": queue_name,
            "batch_id": None,
            "status": "idle",
            "scope": None,
            "evidence_kinds": [],
            "is_paused": False,
            "cancel_requested": False,
            "item_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "cancelled_count": 0,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
            "updated_at": None,
        }
    return _hydrate_queue_state(row)


def start_evidence_work(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        *,
        evidence_kinds: Sequence[str] | None = None,
        limit: int = DEFAULT_EVIDENCE_BATCH_LIMIT,
        updated_at: str | None = None,
) -> dict[str, Any]:
    scope_prefix = str(prefix or "").strip().strip("/")
    if not scope_prefix:
        raise ValueError("Evidence work requires an explicit item, folder, or root scope.")
    selected_kinds = _normalize_evidence_kinds(evidence_kinds)
    normalized_limit = int(limit)
    if normalized_limit <= 0:
        raise ValueError("Evidence work limit must be greater than zero.")
    now_iso = updated_at or timestamp()

    connection.commit()
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        ensure_evidence_queue_state(connection, updated_at=now_iso)
        if background_work_is_paused(connection):
            raise EvidenceQueueConflict(
                "Background catalog and analysis work is paused. Resume background work before preparing evidence."
            )
        current_state = load_evidence_queue_state(connection)
        if str(current_state.get("status") or "") in EVIDENCE_QUEUE_ACTIVE_STATUSES:
            raise EvidenceQueueConflict(
                "Evidence work is already active; pause, cancel, or finish it before starting another scope."
            )
        scope = resolve_media_scope(
            connection,
            scope_prefix,
            library_types=config.library_type_map,
        )
        if scope.root not in config.source_root_map:
            raise EvidenceQueueConflict(
                f"Library {scope.root!r} is not enabled for production evidence work."
            )
        candidates = _evidence_work_candidates(
            connection,
            scope,
            selected_kinds,
            limit=normalized_limit,
        )
        batch_id = uuid.uuid4().hex
        _queue_candidate_rows(
            connection,
            batch_id=batch_id,
            candidates=candidates,
            updated_at=now_iso,
        )
        item_count = len(candidates)
        queue_status = "paused" if item_count else "completed"
        finished_at = None if item_count else now_iso
        scope_payload = scope.to_payload()
        scope_payload["work_limit"] = normalized_limit
        _save_evidence_queue_state(
            connection,
            {
                "queue_name": DEFAULT_EVIDENCE_QUEUE_NAME,
                "batch_id": batch_id,
                "status": queue_status,
                "scope": scope_payload,
                "evidence_kinds": list(selected_kinds),
                "is_paused": bool(item_count),
                "cancel_requested": False,
                "item_count": item_count,
                "completed_count": 0,
                "failed_count": 0,
                "cancelled_count": 0,
                "created_at": now_iso,
                "started_at": None,
                "finished_at": finished_at,
                "updated_at": now_iso,
            },
        )
    except BaseException:
        connection.rollback()
        raise
    return evidence_queue_summary(connection)


def evidence_queue_summary(connection: DBClient) -> dict[str, Any]:
    queue_state = load_evidence_queue_state(connection)
    batch_id = str(queue_state.get("batch_id") or "")
    counts: dict[str, int] = {}
    running: dict[str, Any] | None = None
    if batch_id:
        counts = {
            str(row["work_status"]): int(row["item_count"] or 0)
            for row in connection.execute(
                select(
                    library_item_evidence_state.c.work_status,
                    func.count().label("item_count"),
                )
                .where(library_item_evidence_state.c.work_batch_id == batch_id)
                .group_by(library_item_evidence_state.c.work_status)
            ).mappings()
            if row["work_status"]
        }
        running_row = connection.execute(
            select(
                library_item_evidence_state.c.library_item_id,
                library_item_evidence_state.c.evidence_kind,
                library_item_evidence_state.c.attempt_count,
                library_item_evidence_state.c.last_attempt_at,
                library_item_evidence_state.c.heartbeat_at,
                library_item_evidence_state.c.process_pid,
                library_items.c.rel_path,
            )
            .select_from(
                library_item_evidence_state.join(
                    library_items,
                    library_items.c.id == library_item_evidence_state.c.library_item_id,
                )
            )
            .where(
                library_item_evidence_state.c.work_batch_id == batch_id,
                library_item_evidence_state.c.work_status == "running",
            )
            .limit(1)
        ).mappings().fetchone()
        if running_row is not None:
            running = dict(running_row)
    queue_state["counts"] = counts
    queue_state["running"] = running
    queue_state["remaining_count"] = sum(
        counts.get(status, 0)
        for status in EVIDENCE_WORK_ACTIVE_STATUSES
    )
    return queue_state


def pause_evidence_queue(connection: DBClient, *, updated_at: str | None = None) -> dict[str, Any]:
    return _set_queue_control(connection, is_paused=True, updated_at=updated_at)


def resume_evidence_queue(connection: DBClient, *, updated_at: str | None = None) -> dict[str, Any]:
    return _set_queue_control(connection, is_paused=False, updated_at=updated_at)


def cancel_evidence_queue(connection: DBClient, *, updated_at: str | None = None) -> dict[str, Any]:
    now_iso = updated_at or timestamp()
    connection.commit()
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        state = load_evidence_queue_state(connection)
        batch_id = str(state.get("batch_id") or "")
        if not batch_id or str(state.get("status") or "") not in EVIDENCE_QUEUE_ACTIVE_STATUSES:
            connection.rollback()
            return evidence_queue_summary(connection)
        connection.execute(
            update(library_item_evidence_state)
            .where(
                library_item_evidence_state.c.work_batch_id == batch_id,
                library_item_evidence_state.c.work_status.in_(EVIDENCE_WORK_CLAIMABLE_STATUSES),
            )
            .values(
                work_status="cancelled",
                retry_not_before=None,
                leased_at=None,
                lease_expires_at=None,
                heartbeat_at=None,
                worker_id=None,
                process_pid=None,
                updated_at=now_iso,
            )
        )
        connection.execute(
            update(evidence_queue_state)
            .where(evidence_queue_state.c.queue_name == DEFAULT_EVIDENCE_QUEUE_NAME)
            .values(
                status="cancel_requested",
                is_paused=0,
                cancel_requested=1,
                updated_at=now_iso,
            )
        )
        _refresh_evidence_queue_state(connection, batch_id=batch_id, updated_at=now_iso)
    except BaseException:
        connection.rollback()
        raise
    return evidence_queue_summary(connection)


def claim_next_evidence_work(
        connection: DBClient,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
) -> EvidenceWorkClaim | None:
    current = now or datetime.now(UTC)
    now_iso = current.isoformat(timespec="seconds")
    lease_expires_at = (current + timedelta(seconds=max(1, lease_seconds))).isoformat(timespec="seconds")
    connection.commit()
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        queue_state = load_evidence_queue_state(connection)
        batch_id = str(queue_state.get("batch_id") or "")
        if (
                not batch_id
                or background_work_is_paused(connection)
                or bool(queue_state.get("is_paused"))
                or bool(queue_state.get("cancel_requested"))
                or str(queue_state.get("status") or "") not in EVIDENCE_QUEUE_ACTIVE_STATUSES
        ):
            connection.rollback()
            return None
        running_count = int(
            connection.execute(
                select(func.count())
                .select_from(library_item_evidence_state)
                .where(
                    library_item_evidence_state.c.work_batch_id == batch_id,
                    library_item_evidence_state.c.work_status == "running",
                )
            ).scalar_one()
        )
        if running_count:
            connection.rollback()
            return None
        candidate = connection.execute(
            select(
                library_item_evidence_state.c.library_item_id,
                library_item_evidence_state.c.evidence_kind,
            )
            .where(
                library_item_evidence_state.c.work_batch_id == batch_id,
                library_item_evidence_state.c.work_status.in_(EVIDENCE_WORK_CLAIMABLE_STATUSES),
                or_(
                    library_item_evidence_state.c.retry_not_before.is_(None),
                    library_item_evidence_state.c.retry_not_before <= now_iso,
                ),
            )
            .order_by(
                case(
                    (library_item_evidence_state.c.state == EVIDENCE_STATE_CLASSIFICATION_REQUIRED, 0),
                    else_=1,
                ),
                library_item_evidence_state.c.library_item_id,
                library_item_evidence_state.c.evidence_kind,
            )
            .limit(1)
        ).mappings().fetchone()
        if candidate is None:
            _refresh_evidence_queue_state(connection, batch_id=batch_id, updated_at=now_iso)
            connection.commit()
            return None
        update_result = connection.execute(
            update(library_item_evidence_state)
            .where(
                library_item_evidence_state.c.library_item_id == int(candidate["library_item_id"]),
                library_item_evidence_state.c.evidence_kind == str(candidate["evidence_kind"]),
                library_item_evidence_state.c.work_batch_id == batch_id,
                library_item_evidence_state.c.work_status.in_(EVIDENCE_WORK_CLAIMABLE_STATUSES),
            )
            .values(
                work_status="running",
                leased_at=now_iso,
                lease_expires_at=lease_expires_at,
                heartbeat_at=now_iso,
                worker_id=worker_id,
                process_pid=None,
                retry_not_before=None,
                updated_at=now_iso,
            )
        )
        if _rowcount(update_result.rowcount) != 1:
            connection.rollback()
            return None
        connection.execute(
            update(evidence_queue_state)
            .where(evidence_queue_state.c.queue_name == DEFAULT_EVIDENCE_QUEUE_NAME)
            .values(
                status="running",
                started_at=func.coalesce(evidence_queue_state.c.started_at, now_iso),
                updated_at=now_iso,
            )
        )
        claim = load_evidence_work_claim(
            connection,
            library_item_id=int(candidate["library_item_id"]),
            evidence_kind=str(candidate["evidence_kind"]),
        )
    except BaseException:
        connection.rollback()
        raise
    return claim


def load_evidence_work_claim(
        connection: DBClient,
        *,
        library_item_id: int,
        evidence_kind: str,
) -> EvidenceWorkClaim | None:
    normalized_kind = _normalize_evidence_kinds([evidence_kind])[0]
    row = connection.execute(
        select(
            library_item_evidence_state.c.work_batch_id,
            library_item_evidence_state.c.library_item_id,
            library_item_evidence_state.c.evidence_kind,
            library_item_evidence_state.c.state,
            library_item_evidence_state.c.reason,
            library_item_evidence_state.c.work_source_fingerprint,
            library_item_evidence_state.c.attempt_count,
            library_item_evidence_state.c.worker_id,
            library_items.c.source_path,
            library_items.c.rel_path,
            library_items.c.media_root,
            library_items.c.content_version_fingerprint,
            library_items.c.duration_seconds,
            library_items.c.cadence_summary_json,
            library_items.c.media_fingerprint_json,
        )
        .select_from(
            library_item_evidence_state.join(
                library_items,
                library_items.c.id == library_item_evidence_state.c.library_item_id,
            )
        )
        .where(
            library_item_evidence_state.c.library_item_id == library_item_id,
            library_item_evidence_state.c.evidence_kind == normalized_kind,
            library_item_evidence_state.c.work_status == "running",
        )
    ).mappings().fetchone()
    if row is None:
        return None
    if normalized_kind == EVIDENCE_KINDS[0]:
        summary_json = row["cadence_summary_json"]
    else:
        summary_json = row["media_fingerprint_json"]
    return EvidenceWorkClaim(
        batch_id=str(row["work_batch_id"]),
        library_item_id=int(row["library_item_id"]),
        evidence_kind=normalized_kind,
        evidence_state=str(row["state"]),
        evidence_reason=str(row["reason"]) if row["reason"] is not None else None,
        source_path=str(row["source_path"]),
        rel_path=str(row["rel_path"]),
        media_root=str(row["media_root"]),
        expected_source_fingerprint=(
            str(row["work_source_fingerprint"])
            if row["work_source_fingerprint"] is not None
            else None
        ),
        uses_content_fingerprint=row["content_version_fingerprint"] is not None,
        duration_seconds=(
            float(row["duration_seconds"])
            if row["duration_seconds"] is not None
            else None
        ),
        canonical_summary_json=str(summary_json) if summary_json is not None else None,
        attempt_count=int(row["attempt_count"] or 0),
        worker_id=str(row["worker_id"]),
    )


def mark_evidence_attempt_started(
        connection: DBClient,
        claim: EvidenceWorkClaim,
        *,
        process_pid: int | None = None,
        updated_at: str | None = None,
) -> EvidenceWorkClaim | None:
    now_iso = updated_at or timestamp()
    result = connection.execute(
        update(library_item_evidence_state)
        .where(*_claim_owner_filter(claim))
        .values(
            attempt_count=library_item_evidence_state.c.attempt_count + 1,
            last_attempt_at=now_iso,
            last_error=None,
            process_pid=process_pid,
            updated_at=now_iso,
        )
    )
    if _rowcount(result.rowcount) != 1:
        return None
    active_claim = load_evidence_work_claim(
        connection,
        library_item_id=claim.library_item_id,
        evidence_kind=claim.evidence_kind,
    )
    if active_claim is None or active_claim.worker_id != claim.worker_id:
        return None
    return active_claim


def heartbeat_evidence_work(
        connection: DBClient,
        claim: EvidenceWorkClaim,
        *,
        lease_seconds: int,
        process_pid: int | None,
        now: datetime | None = None,
) -> EvidenceHeartbeatStatus:
    current = now or datetime.now(UTC)
    now_iso = current.isoformat(timespec="seconds")
    lease_expires_at = (current + timedelta(seconds=max(1, lease_seconds))).isoformat(timespec="seconds")
    result = connection.execute(
        update(library_item_evidence_state)
        .where(*_claim_owner_filter(claim))
        .values(
            heartbeat_at=now_iso,
            lease_expires_at=lease_expires_at,
            process_pid=process_pid,
            updated_at=now_iso,
        )
    )
    if _rowcount(result.rowcount) != 1:
        return "claim_lost"
    cancel_requested = connection.execute(
        select(evidence_queue_state.c.cancel_requested)
        .where(
            evidence_queue_state.c.queue_name == DEFAULT_EVIDENCE_QUEUE_NAME,
            evidence_queue_state.c.batch_id == claim.batch_id,
        )
    ).scalar_one_or_none()
    return "cancel_requested" if bool(cancel_requested) else "active"


def transition_evidence_work(
        connection: DBClient,
        claim: EvidenceWorkClaim,
        *,
        work_status: str,
        last_error: str | None = None,
        retry_not_before: str | None = None,
        restore_attempt: bool = False,
        reset_attempt_state: bool = False,
        updated_at: str | None = None,
) -> bool:
    if work_status not in (*EVIDENCE_WORK_ACTIVE_STATUSES, *EVIDENCE_WORK_TERMINAL_STATUSES):
        raise ValueError(f"Unsupported evidence work status: {work_status}")
    now_iso = updated_at or timestamp()
    values: dict[str, Any] = {
        "work_status": work_status,
        "last_error": last_error,
        "retry_not_before": retry_not_before,
        "leased_at": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "worker_id": None,
        "process_pid": None,
        "updated_at": now_iso,
    }
    if restore_attempt:
        values["attempt_count"] = func.max(library_item_evidence_state.c.attempt_count - 1, 0)
    elif reset_attempt_state:
        values.update(
            {
                "attempt_count": 0,
                "retry_not_before": None,
                "last_attempt_at": None,
                "last_error": None,
            }
        )
    result = connection.execute(
        update(library_item_evidence_state)
        .where(*_claim_owner_filter(claim))
        .values(**values)
    )
    if _rowcount(result.rowcount) != 1:
        return False
    _refresh_evidence_queue_state(connection, batch_id=claim.batch_id, updated_at=now_iso)
    return True


def recover_evidence_queue(
        connection: DBClient,
        *,
        reclaim_all_running: bool = False,
        now: datetime | None = None,
) -> int:
    current = now or datetime.now(UTC)
    now_iso = current.isoformat(timespec="seconds")
    queue_state = load_evidence_queue_state(connection)
    batch_id = str(queue_state.get("batch_id") or "")
    if not batch_id:
        return 0
    conditions = [
        library_item_evidence_state.c.work_batch_id == batch_id,
        library_item_evidence_state.c.work_status == "running",
    ]
    if not reclaim_all_running:
        conditions.append(
            or_(
                library_item_evidence_state.c.lease_expires_at.is_(None),
                library_item_evidence_state.c.lease_expires_at <= now_iso,
            )
        )
    target_status = "cancelled" if bool(queue_state.get("cancel_requested")) else "queued"
    result = connection.execute(
        update(library_item_evidence_state)
        .where(*conditions)
        .values(
            work_status=target_status,
            leased_at=None,
            lease_expires_at=None,
            heartbeat_at=None,
            worker_id=None,
            process_pid=None,
            retry_not_before=None,
            last_error=(
                "Evidence work was cancelled during worker recovery."
                if target_status == "cancelled"
                else "Evidence work was interrupted and reclaimed after worker restart."
            ),
            updated_at=now_iso,
        )
    )
    recovered_count = _rowcount(result.rowcount)
    _refresh_evidence_queue_state(connection, batch_id=batch_id, updated_at=now_iso)
    return recovered_count


def current_library_item_source_fingerprint(
        connection: DBClient,
        library_item_id: int,
) -> str | None:
    row = connection.execute(
        select(
            library_items.c.content_version_fingerprint,
            library_items.c.fingerprint,
        ).where(library_items.c.id == library_item_id)
    ).mappings().fetchone()
    if row is None:
        return None
    return _item_source_fingerprint(row)


def _set_queue_control(
        connection: DBClient,
        *,
        is_paused: bool,
        updated_at: str | None,
) -> dict[str, Any]:
    now_iso = updated_at or timestamp()
    connection.commit()
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        state = load_evidence_queue_state(connection)
        batch_id = str(state.get("batch_id") or "")
        if not batch_id or str(state.get("status") or "") not in EVIDENCE_QUEUE_ACTIVE_STATUSES:
            connection.rollback()
            return evidence_queue_summary(connection)
        if bool(state.get("cancel_requested")):
            connection.rollback()
            return evidence_queue_summary(connection)
        if not is_paused and background_work_is_paused(connection):
            raise EvidenceQueueConflict(
                "Background catalog and analysis work is paused. Resume background work before analysis."
            )
        connection.execute(
            update(evidence_queue_state)
            .where(evidence_queue_state.c.queue_name == DEFAULT_EVIDENCE_QUEUE_NAME)
            .values(
                is_paused=int(is_paused),
                updated_at=now_iso,
            )
        )
        _refresh_evidence_queue_state(connection, batch_id=batch_id, updated_at=now_iso)
    except BaseException:
        connection.rollback()
        raise
    return evidence_queue_summary(connection)


def _evidence_work_candidates(
        connection: DBClient,
        scope: MediaScope,
        evidence_kinds: Sequence[EvidenceKind],
        *,
        limit: int,
) -> list[dict[str, Any]]:
    query = (
        select(
            library_item_evidence_state.c.library_item_id,
            library_item_evidence_state.c.evidence_kind,
            library_items.c.content_version_fingerprint,
            library_items.c.fingerprint,
        )
        .select_from(
            library_item_evidence_state.join(
                library_items,
                library_items.c.id == library_item_evidence_state.c.library_item_id,
            )
        )
        .where(
            library_item_evidence_state.c.state.in_((
                EVIDENCE_STATE_ANALYSIS_REQUIRED,
                EVIDENCE_STATE_CLASSIFICATION_REQUIRED,
            )),
            library_item_evidence_state.c.evidence_kind.in_(tuple(evidence_kinds)),
            library_items.c.status != "missing",
            scope_rel_path_filter(library_items.c.rel_path, scope),
        )
        .order_by(
            library_item_evidence_state.c.library_item_id,
            library_item_evidence_state.c.evidence_kind,
        )
        .limit(limit)
    )
    rows = connection.execute(query).mappings().fetchall()
    return [
        {
            "library_item_id": int(row["library_item_id"]),
            "evidence_kind": str(row["evidence_kind"]),
            "work_source_fingerprint": _item_source_fingerprint(row),
        }
        for row in rows
    ]


def _queue_candidate_rows(
        connection: DBClient,
        *,
        batch_id: str,
        candidates: Sequence[dict[str, Any]],
        updated_at: str,
) -> None:
    if not candidates:
        return
    statement = (
        update(library_item_evidence_state)
        .where(
            library_item_evidence_state.c.library_item_id == bindparam("candidate_item_id"),
            library_item_evidence_state.c.evidence_kind == bindparam("candidate_evidence_kind"),
        )
        .values(
            work_batch_id=batch_id,
            work_status="queued",
            work_source_fingerprint=bindparam("candidate_source_fingerprint"),
            leased_at=None,
            lease_expires_at=None,
            heartbeat_at=None,
            worker_id=None,
            process_pid=None,
            retry_not_before=None,
            attempt_count=0,
            last_attempt_at=None,
            last_error=None,
            updated_at=updated_at,
        )
    )
    connection.execute(
        statement,
        [
            {
                "candidate_item_id": candidate["library_item_id"],
                "candidate_evidence_kind": candidate["evidence_kind"],
                "candidate_source_fingerprint": candidate["work_source_fingerprint"],
            }
            for candidate in candidates
        ],
    )


def _save_evidence_queue_state(connection: DBClient, payload: dict[str, Any]) -> None:
    values = {
        "queue_name": str(payload.get("queue_name") or DEFAULT_EVIDENCE_QUEUE_NAME),
        "batch_id": payload.get("batch_id"),
        "status": str(payload.get("status") or "idle"),
        "scope_json": (
            json.dumps(payload.get("scope"), separators=(",", ":"), sort_keys=True)
            if payload.get("scope") is not None
            else None
        ),
        "evidence_kinds_json": json.dumps(payload.get("evidence_kinds") or [], separators=(",", ":")),
        "is_paused": int(bool(payload.get("is_paused"))),
        "cancel_requested": int(bool(payload.get("cancel_requested"))),
        "item_count": int_value(payload.get("item_count")),
        "completed_count": int_value(payload.get("completed_count")),
        "failed_count": int_value(payload.get("failed_count")),
        "cancelled_count": int_value(payload.get("cancelled_count")),
        "created_at": payload.get("created_at"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "updated_at": payload.get("updated_at") or timestamp(),
    }
    statement = sqlite_insert(evidence_queue_state).values(**values)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[evidence_queue_state.c.queue_name],
            set_={
                key: getattr(statement.excluded, key)
                for key in values
                if key != "queue_name"
            },
        )
    )


def _refresh_evidence_queue_state(
        connection: DBClient,
        *,
        batch_id: str,
        updated_at: str,
) -> None:
    queue_state = load_evidence_queue_state(connection)
    if str(queue_state.get("batch_id") or "") != batch_id:
        return
    counts = {
        str(row["work_status"]): int(row["item_count"] or 0)
        for row in connection.execute(
            select(
                library_item_evidence_state.c.work_status,
                func.count().label("item_count"),
            )
            .where(library_item_evidence_state.c.work_batch_id == batch_id)
            .group_by(library_item_evidence_state.c.work_status)
        ).mappings()
        if row["work_status"]
    }
    running_count = counts.get("running", 0)
    claimable_count = sum(counts.get(status, 0) for status in EVIDENCE_WORK_CLAIMABLE_STATUSES)
    completed_count = counts.get("completed", 0)
    failed_count = counts.get("failed", 0)
    cancelled_count = counts.get("cancelled", 0)
    cancel_requested = bool(queue_state.get("cancel_requested"))
    is_paused = bool(queue_state.get("is_paused"))
    if cancel_requested:
        status = "cancel_requested" if running_count else "cancelled"
        finished_at = None if running_count else updated_at
    elif running_count:
        status = "paused" if is_paused else "running"
        finished_at = None
    elif claimable_count:
        status = "paused" if is_paused else "queued"
        finished_at = None
    elif failed_count:
        status = "completed_with_errors"
        finished_at = updated_at
    elif cancelled_count:
        status = "cancelled"
        finished_at = updated_at
    else:
        status = "completed"
        finished_at = updated_at
    connection.execute(
        update(evidence_queue_state)
        .where(
            evidence_queue_state.c.queue_name == DEFAULT_EVIDENCE_QUEUE_NAME,
            evidence_queue_state.c.batch_id == batch_id,
        )
        .values(
            status=status,
            is_paused=int(status == "paused"),
            completed_count=completed_count,
            failed_count=failed_count,
            cancelled_count=cancelled_count,
            finished_at=finished_at,
            updated_at=updated_at,
        )
    )


def _hydrate_queue_state(row: Any) -> dict[str, Any]:
    scope_payload = _loads_json_object(row["scope_json"])
    evidence_kinds = [
        str(kind)
        for kind in object_list(_loads_json_value(row["evidence_kinds_json"]))
        if str(kind) in EVIDENCE_KINDS
    ]
    return {
        "queue_name": str(row["queue_name"]),
        "batch_id": str(row["batch_id"]) if row["batch_id"] is not None else None,
        "status": str(row["status"]),
        "scope": scope_payload or None,
        "evidence_kinds": evidence_kinds,
        "is_paused": bool(row["is_paused"]),
        "cancel_requested": bool(row["cancel_requested"]),
        "item_count": int(row["item_count"] or 0),
        "completed_count": int(row["completed_count"] or 0),
        "failed_count": int(row["failed_count"] or 0),
        "cancelled_count": int(row["cancelled_count"] or 0),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "updated_at": row["updated_at"],
    }


def _normalize_evidence_kinds(evidence_kinds: Sequence[str] | None) -> tuple[EvidenceKind, ...]:
    requested = list(evidence_kinds) if evidence_kinds is not None else list(EVIDENCE_KINDS)
    normalized = tuple(kind for kind in EVIDENCE_KINDS if kind in requested)
    unknown = sorted({str(kind) for kind in requested if kind not in EVIDENCE_KINDS})
    if unknown:
        raise ValueError(f"Unsupported evidence kinds: {', '.join(unknown)}")
    if not normalized:
        raise ValueError("At least one evidence kind is required.")
    return normalized


def _item_source_fingerprint(item: Any) -> str | None:
    payload = object_dict(item)
    value = payload.get("content_version_fingerprint") or payload.get("fingerprint")
    text = str(value or "").strip()
    return text or None


def _claim_owner_filter(claim: EvidenceWorkClaim) -> tuple[Any, ...]:
    return (
        library_item_evidence_state.c.library_item_id == claim.library_item_id,
        library_item_evidence_state.c.evidence_kind == claim.evidence_kind,
        library_item_evidence_state.c.work_batch_id == claim.batch_id,
        library_item_evidence_state.c.work_status == "running",
        library_item_evidence_state.c.worker_id == claim.worker_id,
    )


def _rowcount(value: Any) -> int:
    if callable(value):
        return int(value())
    return int(value) if isinstance(value, int) else 0


def _loads_json_value(value: Any) -> Any:
    try:
        return json.loads(str(value or "null"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _loads_json_object(value: Any) -> dict[str, Any]:
    payload = _loads_json_value(value)
    return payload if isinstance(payload, dict) else {}
