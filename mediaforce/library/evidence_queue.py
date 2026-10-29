import json
import os
import socket
import uuid
from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Sequence

from sqlalchemy import and_, bindparam, case, false, func, or_, select, true, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, DBRow
from mediaforce.core.db_tables import evidence_queue_state, library_item_evidence_state, library_items, source_admissions, source_measurements, source_versions
from mediaforce.core.evidence import stable_source_id
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.core.utils import timestamp
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND
from mediaforce.encoding.plain_failure import plain_failure_summary
from mediaforce.library.background_work import background_work_is_paused
from mediaforce.library.evidence_acquisition import DEFAULT_UNCERTAINTY_FRONTIER_LIMIT, \
    FingerprintAcquisitionPlan, load_fingerprint_acquisition_items, plan_fingerprint_acquisition
from mediaforce.library.evidence_hosts import ControllerDigestWindow, EVIDENCE_HOST_WAIT_REASON
from mediaforce.library.evidence_state import EVIDENCE_KINDS, EVIDENCE_STATE_ANALYSIS_REQUIRED, \
    EVIDENCE_STATE_CLASSIFICATION_REQUIRED, EVIDENCE_STATE_CURRENT, EvidenceKind
from mediaforce.library.item_presence import LIBRARY_ITEM_PRESENT
from mediaforce.library.media_scopes import MediaScope, resolve_media_scope, scope_rel_path_filter
from mediaforce.library.motion_keep_state import kept_original_clause

DEFAULT_EVIDENCE_QUEUE_NAME = "evidence"
DEFAULT_EVIDENCE_BATCH_LIMIT = 25
MAX_CONCURRENT_EVIDENCE_FILES = 3

EVIDENCE_WORK_PRIORITY_DECISION = 0
EVIDENCE_WORK_PRIORITY_RECLASSIFICATION = 10
EVIDENCE_WORK_PRIORITY_OPERATOR = 50
EVIDENCE_WORK_PRIORITY_REPRESENTATIVE = 70
EVIDENCE_WORK_REASON_ORIGINAL_SOURCE = "original_source_measurement"
EVIDENCE_BACKGROUND_RETRY_SECONDS = 15 * 60

SOURCE_ROOT_WAIT_REASON = "Source root is unavailable; evidence work will resume without consuming an attempt."

EVIDENCE_QUEUE_ACTIVE_STATUSES = ("queued", "running", "paused", "cancel_requested")
EVIDENCE_WORK_CLAIMABLE_STATUSES = ("queued", "retry_wait", "waiting_source", "waiting_host", "waiting_window")
EVIDENCE_WORK_ACTIVE_STATUSES = (*EVIDENCE_WORK_CLAIMABLE_STATUSES, "running")
EVIDENCE_WORK_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
EvidenceHeartbeatStatus = Literal["active", "cancel_requested", "claim_lost"]


class EvidenceQueueConflict(RuntimeError):
    pass


def _original_decision_takeover_fields(row: DBRow, reason: str) -> dict[str, Any]:
    if (row.get("work_reason") == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE and reason != EVIDENCE_WORK_REASON_ORIGINAL_SOURCE
            and row.get("work_status") in {"waiting_window", "retry_wait"}):
        return {"work_status": "queued", "retry_not_before": None, "last_error": None}
    return {}


@dataclass(frozen=True, slots=True)
class EvidenceWorkClaim:
    batch_id: str
    library_item_id: int
    evidence_kind: EvidenceKind
    evidence_state: str
    evidence_reason: str | None
    work_priority: int
    work_reason: str | None
    source_path: str
    rel_path: str
    media_root: str
    expected_source_fingerprint: str | None
    uses_content_fingerprint: bool
    duration_seconds: float | None
    canonical_summary_json: str | None
    attempt_count: int
    worker_id: str
    source_revision: int = 0
    source_size_bytes: int | None = None


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


def evidence_queue_allows_preparation(connection: DBClient, state: dict[str, Any]) -> bool:
    if str(state.get("status") or "") not in EVIDENCE_QUEUE_ACTIVE_STATUSES:
        return True
    reasons = set(connection.execute(select(library_item_evidence_state.c.work_reason).where(
        library_item_evidence_state.c.work_batch_id == state["batch_id"],
        library_item_evidence_state.c.work_status.in_(EVIDENCE_WORK_ACTIVE_STATUSES))).scalars())
    return reasons == {EVIDENCE_WORK_REASON_ORIGINAL_SOURCE} and not state["cancel_requested"]


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
        active_background = False
        if str(current_state.get("status") or "") in EVIDENCE_QUEUE_ACTIVE_STATUSES:
            active_background = evidence_queue_allows_preparation(connection, current_state)
        if str(current_state.get("status") or "") in EVIDENCE_QUEUE_ACTIVE_STATUSES and not active_background:
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
        candidates, fingerprint_plan = _evidence_work_candidates(
            connection,
            config,
            scope,
            selected_kinds,
            limit=normalized_limit,
        )
        if active_background:
            if not candidates:
                connection.commit()
                return {**evidence_queue_summary(connection), "prepared_count": 0}
            for candidate in candidates:
                queue_decision_evidence_work(connection, config, scope_prefix,
                    library_item_ids=[candidate["library_item_id"]], evidence_kind=candidate["evidence_kind"],
                    work_reason=candidate["work_reason"], priority=candidate["work_priority"], manage_transaction=False, restart_terminal=True)
            scope_payload = scope.to_payload()
            scope_payload["work_limit"] = normalized_limit
            if fingerprint_plan is not None:
                scope_payload["fingerprint_acquisition"] = _fingerprint_plan_summary(fingerprint_plan)
            connection.execute(update(evidence_queue_state).where(evidence_queue_state.c.queue_name == DEFAULT_EVIDENCE_QUEUE_NAME).values(
                is_paused=1, status="paused", scope_json=json.dumps(scope_payload), updated_at=now_iso))
            connection.commit()
            return {**evidence_queue_summary(connection), "prepared_count": len(candidates)}
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
        if fingerprint_plan is not None:
            scope_payload["fingerprint_acquisition"] = _fingerprint_plan_summary(fingerprint_plan)
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
    return {**evidence_queue_summary(connection), "prepared_count": len(candidates)}


def queue_decision_evidence_work(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        *,
        library_item_ids: Sequence[int],
        evidence_kind: str,
        work_reason: str,
        updated_at: str | None = None,
        manage_transaction: bool = True,
        start_new_batch: bool = False,
        require_measurement: bool = False,
        priority: int = EVIDENCE_WORK_PRIORITY_DECISION,
        restart_terminal: bool = False,
) -> dict[str, Any]:
    """Queue decision-priority evidence for the selected items.

    A new batch starts paused for the operator unless start_new_batch is set, as it is for checks that
    an approved production run is waiting on.
    """
    scope_prefix = str(prefix or "").strip().strip("/")
    if not scope_prefix:
        raise ValueError("Decision evidence work requires an explicit item or folder scope.")
    normalized_ids = sorted({int(item_id) for item_id in library_item_ids})
    if not normalized_ids:
        return {
            "evidence_kind": _normalize_evidence_kinds([evidence_kind])[0],
            "required_count": 0,
            "prepared_count": 0,
            "work": evidence_queue_summary(connection),
        }
    selected_kind = _normalize_evidence_kinds([evidence_kind])[0]
    normalized_reason = str(work_reason or "decision_required").strip() or "decision_required"
    now_iso = updated_at or timestamp()

    if manage_transaction:
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        ensure_evidence_queue_state(connection, updated_at=now_iso)
        if background_work_is_paused(connection):
            raise EvidenceQueueConflict(
                "Background catalog and analysis work is paused. Resume background work before requesting safety evidence."
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
        rows = connection.execute(
            select(
                library_item_evidence_state.c.library_item_id,
                library_item_evidence_state.c.evidence_kind,
                library_item_evidence_state.c.state,
                library_item_evidence_state.c.work_batch_id,
                library_item_evidence_state.c.work_status,
                library_item_evidence_state.c.work_priority,
                library_item_evidence_state.c.work_reason,
                library_item_evidence_state.c.work_source_fingerprint,
                library_item_evidence_state.c.retry_not_before,
                library_item_evidence_state.c.attempt_count,
                library_item_evidence_state.c.last_attempt_at,
                library_item_evidence_state.c.last_error,
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
                library_item_evidence_state.c.library_item_id.in_(normalized_ids),
                library_item_evidence_state.c.evidence_kind == selected_kind,
                library_item_evidence_state.c.state.in_((
                    EVIDENCE_STATE_ANALYSIS_REQUIRED,
                    EVIDENCE_STATE_CLASSIFICATION_REQUIRED,
                    *([EVIDENCE_STATE_CURRENT] if require_measurement else []),
                )),
                LIBRARY_ITEM_PRESENT,
                scope_rel_path_filter(library_items.c.rel_path, scope),
            )
            .order_by(library_item_evidence_state.c.library_item_id)
        ).mappings().fetchall()
        if not rows:
            if manage_transaction:
                connection.commit()
            return {
                "evidence_kind": selected_kind,
                "required_count": 0,
                "prepared_count": 0,
                "work": evidence_queue_summary(connection),
            }

        queue_state = load_evidence_queue_state(connection)
        queue_status = str(queue_state.get("status") or "idle")
        if queue_status in EVIDENCE_QUEUE_ACTIVE_STATUSES and (bool(queue_state.get("cancel_requested")) or queue_status == "cancel_requested"):
            raise EvidenceQueueConflict(
                "Evidence work is stopping. Wait for cancellation to finish before requesting safety evidence."
            )
        active_batch = queue_status in EVIDENCE_QUEUE_ACTIVE_STATUSES and bool(queue_state.get("batch_id"))
        batch_id = str(queue_state.get("batch_id") or "") if active_batch else uuid.uuid4().hex
        if any(
                str(row.get("work_status") or "") == "running"
                and str(row.get("work_batch_id") or "") != batch_id
                for row in rows
        ):
            raise EvidenceQueueConflict(
                "The required evidence item is already owned by another active batch."
            )
        prepared_count = 0
        terminal_failure_count = 0
        for row in rows:
            row_batch_id = str(row.get("work_batch_id") or "")
            row_status = str(row.get("work_status") or "")
            current_priority = int_value(row["work_priority"]) if row.get("work_priority") is not None else EVIDENCE_WORK_PRIORITY_OPERATOR
            if row_batch_id == batch_id and row_status in EVIDENCE_WORK_ACTIVE_STATUSES:
                next_reason = normalized_reason if priority < current_priority or not row.get("work_reason") else str(row["work_reason"])
                connection.execute(
                    update(library_item_evidence_state)
                    .where(
                        library_item_evidence_state.c.library_item_id == int(row["library_item_id"]),
                        library_item_evidence_state.c.evidence_kind == selected_kind,
                    )
                    .values(
                        work_priority=min(current_priority, priority),
                        work_reason=next_reason,
                        updated_at=now_iso,
                        **_original_decision_takeover_fields(row, next_reason),
                    )
                )
                prepared_count += 1
                continue
            current_source_fingerprint = _item_source_fingerprint(row)
            preserve_retry_state = bool(
                not (restart_terminal and row_status in EVIDENCE_WORK_TERMINAL_STATUSES)
                and (require_measurement or str(row.get("state") or "") == EVIDENCE_STATE_ANALYSIS_REQUIRED)
                and current_source_fingerprint
                and str(row.get("work_source_fingerprint") or "") == current_source_fingerprint
                and row_status in {"retry_wait", "waiting_source", "waiting_host", "waiting_window", "failed"}
            )
            if preserve_retry_state and row_status == "failed":
                connection.execute(
                    update(library_item_evidence_state)
                    .where(
                        library_item_evidence_state.c.library_item_id == int(row["library_item_id"]),
                        library_item_evidence_state.c.evidence_kind == selected_kind,
                    )
                    .values(
                        work_priority=min(current_priority, priority),
                        work_reason=normalized_reason if priority < current_priority or not row.get("work_reason") else row["work_reason"],
                        updated_at=now_iso,
                    )
                )
                terminal_failure_count += 1
                continue
            next_status = row_status if preserve_retry_state else "queued"
            next_reason = (str(row["work_reason"]) if preserve_retry_state and current_priority <= priority and row.get("work_reason")
                           else normalized_reason)
            takeover = _original_decision_takeover_fields(row, next_reason)
            connection.execute(
                update(library_item_evidence_state)
                .where(
                    library_item_evidence_state.c.library_item_id == int(row["library_item_id"]),
                    library_item_evidence_state.c.evidence_kind == selected_kind,
                )
                .values(
                    work_batch_id=batch_id,
                    work_status=takeover.get("work_status", next_status),
                    work_priority=min(current_priority, priority) if preserve_retry_state else priority,
                    work_reason=next_reason,
                    work_source_fingerprint=current_source_fingerprint,
                    leased_at=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    worker_id=None,
                    process_pid=None,
                    retry_not_before=takeover.get("retry_not_before", row.get("retry_not_before") if preserve_retry_state else None),
                    attempt_count=int_value(row.get("attempt_count")) if preserve_retry_state else 0,
                    last_attempt_at=row.get("last_attempt_at") if preserve_retry_state else None,
                    last_error=takeover.get("last_error", row.get("last_error") if preserve_retry_state else None),
                    updated_at=now_iso,
                )
            )
            prepared_count += 1

        if not active_batch and prepared_count == 0:
            if manage_transaction:
                connection.commit()
            return {
                "evidence_kind": selected_kind,
                "required_count": len(rows),
                "prepared_count": 0,
                "terminal_failure_count": terminal_failure_count,
                "work": evidence_queue_summary(connection),
            }

        _release_controller_digest_waits(connection, batch_id, now_iso)
        if active_batch:
            evidence_kinds = list(dict.fromkeys([
                *object_list(queue_state.get("evidence_kinds")),
                selected_kind,
            ]))
            item_count = int(
                connection.execute(
                    select(func.count())
                    .select_from(library_item_evidence_state)
                    .where(library_item_evidence_state.c.work_batch_id == batch_id)
                ).scalar_one()
            )
            priority_rows = connection.execute(
                select(
                    library_item_evidence_state.c.work_reason,
                    func.count().label("item_count"),
                )
                .where(
                    library_item_evidence_state.c.work_batch_id == batch_id,
                    library_item_evidence_state.c.work_priority == EVIDENCE_WORK_PRIORITY_DECISION,
                )
                .group_by(library_item_evidence_state.c.work_reason)
            ).mappings().fetchall()
            scope_payload = object_dict(queue_state.get("scope"))
            scope_payload["priority_request_count"] = sum(
                int(row["item_count"] or 0)
                for row in priority_rows
            )
            scope_payload["priority_reasons"] = sorted(
                str(row["work_reason"])
                for row in priority_rows
                if row["work_reason"]
            )
            connection.execute(
                update(evidence_queue_state)
                .where(evidence_queue_state.c.queue_name == DEFAULT_EVIDENCE_QUEUE_NAME)
                .values(
                    scope_json=json.dumps(scope_payload, separators=(",", ":"), sort_keys=True),
                    evidence_kinds_json=json.dumps(evidence_kinds, separators=(",", ":")),
                    item_count=item_count,
                    finished_at=None,
                    updated_at=now_iso,
                )
            )
            _refresh_evidence_queue_state(connection, batch_id=batch_id, updated_at=now_iso)
        else:
            scope_payload = scope.to_payload()
            scope_payload.update({
                "work_limit": prepared_count,
                "trigger": "decision",
                "work_reason": normalized_reason,
            })
            _save_evidence_queue_state(
                connection,
                {
                    "queue_name": DEFAULT_EVIDENCE_QUEUE_NAME,
                    "batch_id": batch_id,
                    "status": "queued" if start_new_batch else "paused",
                    "scope": scope_payload,
                    "evidence_kinds": [selected_kind],
                    "is_paused": not start_new_batch,
                    "cancel_requested": False,
                    "item_count": prepared_count,
                    "completed_count": 0,
                    "failed_count": 0,
                    "cancelled_count": 0,
                    "created_at": now_iso,
                    "started_at": None,
                    "finished_at": None,
                    "updated_at": now_iso,
                },
            )
            _refresh_evidence_queue_state(connection, batch_id=batch_id, updated_at=now_iso)
        if manage_transaction:
            connection.commit()
    except BaseException:
        if manage_transaction:
            connection.rollback()
        raise
    return {
        "evidence_kind": selected_kind,
        "required_count": len(rows),
        "prepared_count": prepared_count,
        "terminal_failure_count": terminal_failure_count,
        "work": evidence_queue_summary(connection),
    }


def _claimable_batch_id(connection: DBClient) -> str | None:
    """The current batch when it may hand out work: active, unpaused, not cancelling, background running."""
    queue_state = load_evidence_queue_state(connection)
    batch_id = str(queue_state.get("batch_id") or "")
    if (
            not batch_id
            or background_work_is_paused(connection)
            or bool(queue_state.get("is_paused"))
            or bool(queue_state.get("cancel_requested"))
            or str(queue_state.get("status") or "") not in EVIDENCE_QUEUE_ACTIVE_STATUSES
    ):
        return None
    return batch_id


def _evidence_due_now(now_iso: str) -> Any:
    return or_(library_item_evidence_state.c.retry_not_before.is_(None), library_item_evidence_state.c.retry_not_before <= now_iso)


def original_intake_hold(
    connection: DBClient, *, batch_id: str, priority: int, excluded_media_roots: Collection[str] = (),
    allowed_running_item: int | None = None,
    max_running_files: int = 1,
) -> Literal["cancelled", "paused", "higher_priority"] | None:
    state = load_evidence_queue_state(connection)
    if (state["batch_id"] != batch_id or state["cancel_requested"]
            or state["status"] not in EVIDENCE_QUEUE_ACTIVE_STATUSES):
        return "cancelled"
    if background_work_is_paused(connection) or state["is_paused"]:
        return "paused"
    urgent = next_evidence_work_query(batch_id, timestamp(), excluded_media_roots=excluded_media_roots,
                                     allowed_running_item=allowed_running_item).where(
        library_item_evidence_state.c.work_priority < priority,
    )
    if max_running_files > 1:
        running = select(
            library_item_evidence_state.c.library_item_id,
            func.min(library_item_evidence_state.c.work_priority).label("priority"),
        ).where(
            library_item_evidence_state.c.work_batch_id == batch_id,
            library_item_evidence_state.c.work_status == "running",
        ).group_by(library_item_evidence_state.c.library_item_id).subquery()
        victim = select(running, func.count().over().label("file_count")).order_by(
            running.c.priority.desc(), running.c.library_item_id.desc(),
        ).limit(1).subquery()
        urgent = urgent.outerjoin(victim, true()).where(or_(
            library_item_evidence_state.c.library_item_id == allowed_running_item,
            and_(victim.c.file_count >= max_running_files, victim.c.library_item_id == allowed_running_item,
                 victim.c.priority > library_item_evidence_state.c.work_priority),
        ))
    return "higher_priority" if connection.execute(urgent).first() is not None else None


def runnable_evidence_work_count(connection: DBClient) -> int:
    """Claimable items in the current batch, or zero when anything holds the queue back."""
    batch_id = _claimable_batch_id(connection)
    if not batch_id:
        return 0
    return int(
        connection.execute(
            select(func.count())
            .select_from(library_item_evidence_state)
            .where(
                library_item_evidence_state.c.work_batch_id == batch_id,
                library_item_evidence_state.c.work_status.in_(EVIDENCE_WORK_CLAIMABLE_STATUSES),
                _evidence_due_now(timestamp()),
            )
        ).scalar_one()
    )


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
                library_item_evidence_state.c.work_priority,
                library_item_evidence_state.c.work_reason,
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
                retry_not_before=case((library_item_evidence_state.c.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
                    (datetime.fromisoformat(now_iso) + timedelta(seconds=EVIDENCE_BACKGROUND_RETRY_SECONDS)).isoformat(timespec="seconds")), else_=None),
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


def next_evidence_work_query(batch_id: str, now_iso: str, *, excluded_media_roots: Collection[str] = (),
                             allowed_running_item: int | None = None) -> Any:
    query = (
        select(library_item_evidence_state.c.library_item_id, library_item_evidence_state.c.evidence_kind)
        .where(
            library_item_evidence_state.c.work_batch_id == batch_id,
            library_item_evidence_state.c.work_status.in_(EVIDENCE_WORK_CLAIMABLE_STATUSES),
            _evidence_due_now(now_iso),
        )
        .order_by(
            library_item_evidence_state.c.work_priority,
            case((library_item_evidence_state.c.work_status == "queued", 0), else_=1),
            case((library_item_evidence_state.c.state == EVIDENCE_STATE_CLASSIFICATION_REQUIRED, 0), else_=1),
            library_item_evidence_state.c.library_item_id,
            library_item_evidence_state.c.evidence_kind,
        )
        .limit(1)
    )

    blocked_items = select(library_items.c.id).where(library_items.c.media_root.in_(excluded_media_roots))
    source_waiting_items = select(library_items.c.id).where(
        library_items.c.media_root.in_(_waiting_roots_query(batch_id, now_iso, "waiting_source"))
    )
    running = library_item_evidence_state.alias("running_items")
    running_items = select(running.c.library_item_id).where(running.c.work_status == "running")
    if allowed_running_item is not None:
        running_items = running_items.where(running.c.library_item_id != allowed_running_item)
    query = query.where(library_item_evidence_state.c.library_item_id.not_in(running_items))
    return query.where(
        or_(library_item_evidence_state.c.library_item_id.not_in(blocked_items), ~_evidence_requires_computer()),
        or_(~_evidence_requires_source(), library_item_evidence_state.c.library_item_id.not_in(source_waiting_items)),
    )


def _original_digest_required() -> Any:
    cached = select(library_items.c.id).select_from(library_items.join(
        source_versions, source_versions.c.version_id == library_items.c.active_source_version_id,
    )).where(
        source_versions.c.sha256.is_not(None), source_versions.c.sha256 != "",
        source_versions.c.size_bytes == library_items.c.size_bytes,
        source_versions.c.sample_fingerprint == library_items.c.content_version_fingerprint,
        func.json_extract(source_versions.c.identity_json, "$.path") == library_items.c.source_path,
        func.json_extract(source_versions.c.identity_json, "$.stat[3]") == library_items.c.mtime_ns,
    )
    return and_(
        func.coalesce(library_item_evidence_state.c.work_reason, "") == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
        library_item_evidence_state.c.library_item_id.not_in(cached),
    )


def _release_controller_digest_waits(
        connection: DBClient, batch_id: str, updated_at: str, *, window_open: bool = False, waiting_reason: str | None = None,
) -> None:
    connection.execute(update(library_item_evidence_state).where(
        library_item_evidence_state.c.work_batch_id == batch_id,
        or_(library_item_evidence_state.c.work_status == "waiting_window",
            and_(true() if window_open else false(), library_item_evidence_state.c.work_status == "retry_wait",
                 library_item_evidence_state.c.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
                 library_item_evidence_state.c.last_error == waiting_reason)),
        true() if window_open else ~_original_digest_required(),
    ).values(work_status="queued", retry_not_before=None, last_error=None, updated_at=updated_at))


def refresh_controller_digest_waits(connection: DBClient, window: ControllerDigestWindow) -> None:
    """Use the queue's existing due-time mechanism for this computer's window, without leasing waiting files."""
    state = load_evidence_queue_state(connection)
    batch = str(state.get("batch_id") or "")
    if not batch or state["status"] not in EVIDENCE_QUEUE_ACTIVE_STATUSES:
        return
    now = datetime.now(UTC)
    now_iso = now.isoformat(timespec="seconds")
    owned = library_item_evidence_state.c.work_batch_id == batch
    needs_digest = _original_digest_required()
    _release_controller_digest_waits(connection, batch, now_iso, window_open=window.is_open, waiting_reason=window.waiting_reason)
    if window.is_open:
        return
    opens_at = window.next_opens_at
    retry_at = opens_at if opens_at is not None and opens_at > now else now + timedelta(seconds=EVIDENCE_BACKGROUND_RETRY_SECONDS)
    connection.execute(update(library_item_evidence_state).where(
        owned, library_item_evidence_state.c.work_status.in_(("queued", "retry_wait", "waiting_window")),
        _evidence_due_now(now_iso), needs_digest,
    ).values(work_status="waiting_window", retry_not_before=retry_at.isoformat(timespec="seconds"),
             last_error=window.waiting_reason, updated_at=now_iso))


def original_check_wait_reasons(connection: DBClient) -> list[str]:
    batch = str(load_evidence_queue_state(connection).get("batch_id") or "")
    reasons = connection.execute(select(library_item_evidence_state.c.last_error).where(
        library_item_evidence_state.c.work_batch_id == batch,
        library_item_evidence_state.c.work_status.in_(("waiting_window", "retry_wait")),
        library_item_evidence_state.c.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
        library_item_evidence_state.c.last_error.is_not(None),
    ).distinct()).scalars()
    summaries: set[str] = set()
    for reason in reasons:
        summary = plain_failure_summary(reason)
        if summary is not None:
            summaries.add(summary)
    return sorted(summaries)


def _evidence_requires_computer() -> Any:
    original = func.coalesce(library_item_evidence_state.c.work_reason, "") == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE
    return and_(
        or_(library_item_evidence_state.c.state != EVIDENCE_STATE_CURRENT,
            and_(original, ~_observed_original_receipt_exists())),
        or_(library_item_evidence_state.c.state != EVIDENCE_STATE_CLASSIFICATION_REQUIRED, original),
    )


def _observed_original_receipt_exists() -> Any:
    items = library_items.alias("receipt_items")
    measurements = source_measurements.alias("observed_receipts")
    admissions = source_admissions.alias("receipt_admissions")
    frozen = func.json_each(admissions.c.checks_json, "$.measurement_ids").table_valued("value", joins_implicitly=True).alias("frozen_receipts")
    direct = select(measurements.c.measurement_id).select_from(items.join(
        measurements, measurements.c.version_id == items.c.active_source_version_id,
    )).where(items.c.id == library_item_evidence_state.c.library_item_id,
             measurements.c.kind == library_item_evidence_state.c.evidence_kind,
             measurements.c.binding == "observed").exists()
    qualified = select(measurements.c.measurement_id).select_from(
        items.join(admissions, admissions.c.version_id == items.c.active_source_version_id)
        .join(frozen, true()).join(measurements, measurements.c.measurement_id == frozen.c.value)
    ).where(items.c.id == library_item_evidence_state.c.library_item_id,
            measurements.c.kind == library_item_evidence_state.c.evidence_kind,
            measurements.c.binding == "observed").exists()
    return or_(direct, qualified)


def _evidence_requires_source() -> Any:
    return or_(
        func.coalesce(library_item_evidence_state.c.work_reason, "") == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
        library_item_evidence_state.c.state.not_in((EVIDENCE_STATE_CURRENT, EVIDENCE_STATE_CLASSIFICATION_REQUIRED)),
    )


def _waiting_roots_query(batch_id: str, now_iso: str, work_status: str) -> Any:
    waits = library_item_evidence_state.alias("resource_waits")
    items = library_items.alias("waiting_items")
    return select(items.c.media_root).select_from(items.join(waits, waits.c.library_item_id == items.c.id)).where(
        waits.c.work_batch_id == batch_id,
        waits.c.work_status == work_status,
        waits.c.retry_not_before > now_iso,
    ).distinct()


def waiting_evidence_host_roots(connection: DBClient, *, batch_id: str | None = None) -> frozenset[str]:
    batch = batch_id if batch_id is not None else str(load_evidence_queue_state(connection).get("batch_id") or "")
    return frozenset(connection.execute(_waiting_roots_query(batch, timestamp(), "waiting_host")).scalars())


def _mark_unavailable_evidence_work(connection: DBClient, batch_id: str, now_iso: str, roots: Collection[str]) -> None:
    source_roots = frozenset(connection.execute(_waiting_roots_query(batch_id, now_iso, "waiting_source")).scalars())
    for blocked, status, required, reason in (
        (roots, "waiting_host", _evidence_requires_computer(), EVIDENCE_HOST_WAIT_REASON),
        (source_roots, "waiting_source", _evidence_requires_source(), SOURCE_ROOT_WAIT_REASON),
    ):
        if not blocked:
            continue
        blocked_items = select(library_items.c.id).where(library_items.c.media_root.in_(blocked))
        connection.execute(update(library_item_evidence_state).where(
            library_item_evidence_state.c.work_batch_id == batch_id,
            library_item_evidence_state.c.work_status == "queued",
            library_item_evidence_state.c.library_item_id.in_(blocked_items),
            required,
        ).values(work_status=status, last_error=reason, updated_at=now_iso))


def claim_next_evidence_work(
        connection: DBClient,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
        excluded_media_roots: Collection[str] = (),
        max_running_files: int = 1,
) -> EvidenceWorkClaim | None:
    if not 1 <= max_running_files <= MAX_CONCURRENT_EVIDENCE_FILES:
        raise ValueError(f"Evidence concurrency must be between one and {MAX_CONCURRENT_EVIDENCE_FILES} files.")
    current = now or datetime.now(UTC)
    now_iso = current.isoformat(timespec="seconds")
    lease_expires_at = (current + timedelta(seconds=max(1, lease_seconds))).isoformat(timespec="seconds")
    connection.commit()
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        batch_id = _claimable_batch_id(connection)
        if not batch_id:
            connection.rollback()
            return None
        running_count = int(
            connection.execute(
                select(func.count(library_item_evidence_state.c.library_item_id.distinct()))
                .select_from(library_item_evidence_state)
                .where(
                    library_item_evidence_state.c.work_batch_id == batch_id,
                    library_item_evidence_state.c.work_status == "running",
                )
            ).scalar_one()
        )
        if running_count >= max_running_files:
            connection.rollback()
            return None
        _mark_unavailable_evidence_work(connection, batch_id, now_iso, excluded_media_roots)
        # A check queued before its file was kept original would only measure what no one will encode.
        _release_kept_motion_work(connection, batch_id=batch_id, updated_at=now_iso)
        candidate = connection.execute(next_evidence_work_query(
            batch_id, now_iso, excluded_media_roots=excluded_media_roots,
        )).mappings().fetchone()
        if candidate is None:
            _refresh_evidence_queue_state(connection, batch_id=batch_id, updated_at=now_iso)
            connection.commit()
            return None
        if not _lease_evidence_row(connection, int(candidate["library_item_id"]), str(candidate["evidence_kind"]),
                                   batch_id, worker_id, now_iso, lease_expires_at):
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


def _lease_evidence_row(connection: DBClient, item_id: int, kind: str, batch_id: str,
                        worker_id: str, now_iso: str, lease_expires_at: str) -> bool:
    update_result = connection.execute(
        update(library_item_evidence_state)
        .where(
            library_item_evidence_state.c.library_item_id == item_id,
            library_item_evidence_state.c.evidence_kind == kind,
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
    return _rowcount(update_result.rowcount) == 1


def extend_evidence_claim(
        connection: DBClient,
        claim: EvidenceWorkClaim,
        *,
        lease_seconds: int,
        excluded_media_roots: Collection[str] = (),
) -> EvidenceWorkClaim | None:
    now = datetime.now(UTC)
    now_iso = now.isoformat(timespec="seconds")
    connection.commit()
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        owned = connection.execute(select(library_item_evidence_state.c.library_item_id).where(
            *_claim_owner_filter(claim), library_item_evidence_state.c.lease_expires_at > now_iso,
        )).first()
        if owned is None or _claimable_batch_id(connection) != claim.batch_id:
            connection.rollback()
            return None
        _mark_unavailable_evidence_work(connection, claim.batch_id, now_iso, excluded_media_roots)
        _release_kept_motion_work(connection, batch_id=claim.batch_id, updated_at=now_iso)
        candidate = connection.execute(next_evidence_work_query(
            claim.batch_id, now_iso, excluded_media_roots=excluded_media_roots,
            allowed_running_item=claim.library_item_id,
        )).mappings().first()
        if candidate is None or int(candidate["library_item_id"]) != claim.library_item_id:
            connection.commit()
            return None
        kind = str(candidate["evidence_kind"])
        row = connection.execute(select(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == claim.library_item_id,
            library_item_evidence_state.c.evidence_kind == kind,
        )).mappings().one()
        original = row["work_reason"] == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE
        if (row["work_source_fingerprint"] != claim.expected_source_fingerprint
                or original != (claim.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE)
                or (not original and row["state"] != EVIDENCE_STATE_ANALYSIS_REQUIRED)):
            connection.commit()
            return None
        if not _lease_evidence_row(connection, claim.library_item_id, kind, claim.batch_id, claim.worker_id,
                                  now_iso, (now + timedelta(seconds=max(1, lease_seconds))).isoformat(timespec="seconds")):
            connection.rollback()
            return None
        companion = load_evidence_work_claim(connection, library_item_id=claim.library_item_id, evidence_kind=kind)
        connection.commit()
        return companion
    except BaseException:
        connection.rollback()
        raise


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
            library_item_evidence_state.c.work_priority,
            library_item_evidence_state.c.work_reason,
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
            library_items.c.source_revision,
            library_items.c.size_bytes,
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
        work_priority=int(row["work_priority"] if row["work_priority"] is not None else EVIDENCE_WORK_PRIORITY_OPERATOR),
        work_reason=str(row["work_reason"]) if row["work_reason"] is not None else None,
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
        source_revision=int(row["source_revision"]),
        source_size_bytes=int(row["size_bytes"]),
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
    if work_status == "cancelled" and claim.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE:
        values["retry_not_before"] = (datetime.fromisoformat(now_iso) + timedelta(seconds=EVIDENCE_BACKGROUND_RETRY_SECONDS)).isoformat(timespec="seconds")
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
        abandoned = [
            worker_id for worker_id in connection.execute(
                select(library_item_evidence_state.c.worker_id).where(*conditions).distinct()
            ).scalars() if _local_worker_exited(worker_id)
        ]
        conditions.append(
            or_(
                library_item_evidence_state.c.lease_expires_at.is_(None),
                library_item_evidence_state.c.lease_expires_at <= now_iso,
                library_item_evidence_state.c.worker_id.in_(abandoned),
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


def _local_worker_exited(worker_id: str | None) -> bool:
    parts = (worker_id or "").split(":", 2)
    if len(parts) != 3 or parts[0] != socket.gethostname() or not parts[1].isdigit() or not parts[2]:
        return False
    pid = int(parts[1])
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):
        return False
    return False


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
        config: MediaforceConfig,
        scope: MediaScope,
        evidence_kinds: Sequence[EvidenceKind],
        *,
        limit: int,
) -> tuple[list[dict[str, Any]], FingerprintAcquisitionPlan | None]:
    query = (
        select(
            library_item_evidence_state.c.library_item_id,
            library_item_evidence_state.c.evidence_kind,
            library_item_evidence_state.c.state,
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
            LIBRARY_ITEM_PRESENT,
            scope_rel_path_filter(library_items.c.rel_path, scope),
            # A file the owner kept original for its current source needs no motion-pattern check.
            or_(
                library_item_evidence_state.c.evidence_kind != CADENCE_EVIDENCE_KIND,
                ~kept_original_clause(library_items.c.id),
            ),
        )
        .order_by(
            library_item_evidence_state.c.library_item_id,
            library_item_evidence_state.c.evidence_kind,
        )
    )
    rows = connection.execute(query).mappings().fetchall()
    classification_rows = [
        row
        for row in rows
        if str(row["state"]) == EVIDENCE_STATE_CLASSIFICATION_REQUIRED
    ]
    cadence_rows = [
        row
        for row in rows
        if (
            str(row["state"]) == EVIDENCE_STATE_ANALYSIS_REQUIRED
            and str(row["evidence_kind"]) == EVIDENCE_KINDS[0]
        )
    ]
    fingerprint_plan: FingerprintAcquisitionPlan | None = None
    fingerprint_rows: list[Any] = []
    fingerprint_analysis_required = any(
        str(row["state"]) == EVIDENCE_STATE_ANALYSIS_REQUIRED
        and str(row["evidence_kind"]) == EVIDENCE_KINDS[1]
        for row in rows
    )
    if EVIDENCE_KINDS[1] in evidence_kinds and fingerprint_analysis_required:
        _candidate_scope, representative_items, measured_source_ids = load_fingerprint_acquisition_items(
            connection,
            config,
            scope.prefix,
        )
        if representative_items:
            item_id_by_source_id = {
                stable_source_id(item): int(item["library_item_id"])
                for item in representative_items
            }
            fingerprint_plan = plan_fingerprint_acquisition(
                representative_items,
                prefix=scope.prefix,
                policy=(
                    config.resolve_policy(scope.prefix)
                    if all(
                        isinstance(config.raw.get(section), dict)
                        for section in ("video", "audio", "subtitle", "planning")
                    )
                    else None
                ),
                max_uncertainty_frontier=min(DEFAULT_UNCERTAINTY_FRONTIER_LIMIT, max(1, limit)),
                measured_source_ids=measured_source_ids,
            )
            planned_item_ids = {
                item_id_by_source_id[source_id]
                for source_id in fingerprint_plan.candidate_ids
                if source_id in item_id_by_source_id
            }
            fingerprint_rows_by_item = {
                int(row["library_item_id"]): row
                for row in rows
                if (
                    str(row["state"]) == EVIDENCE_STATE_ANALYSIS_REQUIRED
                    and str(row["evidence_kind"]) == EVIDENCE_KINDS[1]
                    and int(row["library_item_id"]) in planned_item_ids
                )
            }
            fingerprint_rows = [
                fingerprint_rows_by_item[item_id_by_source_id[source_id]]
                for source_id in fingerprint_plan.candidate_ids
                if item_id_by_source_id.get(source_id) in fingerprint_rows_by_item
            ]
    candidates: list[dict[str, Any]] = []
    for row in classification_rows:
        candidates.append(_work_candidate(
            row,
            work_priority=EVIDENCE_WORK_PRIORITY_RECLASSIFICATION,
            work_reason="policy_reclassification",
        ))
    for row in cadence_rows:
        candidates.append(_work_candidate(
            row,
            work_priority=EVIDENCE_WORK_PRIORITY_OPERATOR,
            work_reason="operator_scope",
        ))
    fingerprint_reason = (
        "representative_uncertainty"
        if fingerprint_plan is not None and fingerprint_plan.phase == "uncertainty_frontier"
        else "representative_technical_coverage"
    )
    for row in fingerprint_rows:
        candidates.append(_work_candidate(
            row,
            work_priority=EVIDENCE_WORK_PRIORITY_REPRESENTATIVE,
            work_reason=fingerprint_reason,
        ))
    return candidates[:limit], fingerprint_plan


def _work_candidate(
        row: Any,
        *,
        work_priority: int,
        work_reason: str,
) -> dict[str, Any]:
    return {
        "library_item_id": int(row["library_item_id"]),
        "evidence_kind": str(row["evidence_kind"]),
        "work_priority": work_priority,
        "work_reason": work_reason,
        "work_source_fingerprint": _item_source_fingerprint(row),
    }


def _fingerprint_plan_summary(plan: FingerprintAcquisitionPlan) -> dict[str, Any]:
    return {
        "phase": plan.phase,
        "candidate_count": len(plan.candidate_ids),
        "analysis_budget": plan.analysis_budget,
        "remaining_analysis_budget": plan.remaining_analysis_budget,
        "stop_reason": plan.stop_reason,
        "intentionally_unqueued_count": plan.intentionally_unqueued_count,
        "coverage": asdict(plan.coverage),
        "estimated_analysis_cost": asdict(plan.estimated_analysis_cost),
    }


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
            work_priority=bindparam("candidate_priority"),
            work_reason=bindparam("candidate_reason"),
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
                "candidate_priority": int(candidate.get("work_priority", EVIDENCE_WORK_PRIORITY_OPERATOR)),
                "candidate_reason": str(candidate.get("work_reason") or "operator_scope"),
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


def release_kept_motion_work(connection: DBClient, *, updated_at: str | None = None) -> int:
    """Take unstarted motion-pattern checks for files kept original out of the current batch."""
    batch_id = str(load_evidence_queue_state(connection).get("batch_id") or "")
    if not batch_id:
        return 0
    return _release_kept_motion_work(connection, batch_id=batch_id, updated_at=updated_at or timestamp())


def _release_kept_motion_work(connection: DBClient, *, batch_id: str, updated_at: str) -> int:
    kept_ids = [
        int(item_id)
        for item_id in connection.execute(
            select(library_item_evidence_state.c.library_item_id)
            .select_from(
                library_item_evidence_state.join(
                    library_items, library_items.c.id == library_item_evidence_state.c.library_item_id,
                )
            )
            .where(
                library_item_evidence_state.c.work_batch_id == batch_id,
                library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
                library_item_evidence_state.c.work_status.in_(EVIDENCE_WORK_CLAIMABLE_STATUSES),
                kept_original_clause(library_items.c.id),
            )
        ).scalars()
    ]
    if not kept_ids:
        return 0
    connection.execute(
        update(library_item_evidence_state)
        .where(
            library_item_evidence_state.c.library_item_id.in_(kept_ids),
            library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
            library_item_evidence_state.c.work_batch_id == batch_id,
        )
        .values(
            work_batch_id=None,
            work_status=None,
            work_reason=None,
            work_source_fingerprint=None,
            retry_not_before=None,
            updated_at=updated_at,
        )
    )
    connection.execute(
        update(evidence_queue_state)
        .where(
            evidence_queue_state.c.queue_name == DEFAULT_EVIDENCE_QUEUE_NAME,
            evidence_queue_state.c.batch_id == batch_id,
        )
        .values(item_count=func.max(0, evidence_queue_state.c.item_count - len(kept_ids)))
    )
    _refresh_evidence_queue_state(connection, batch_id=batch_id, updated_at=updated_at)
    return len(kept_ids)


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
