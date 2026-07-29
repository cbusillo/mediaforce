import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select, update

from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import library_items
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.core.utils import content_version_fingerprint, file_fingerprint
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND, reclassify_cadence_summary
from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_EVIDENCE_KIND, reclassify_media_fingerprint_summary
from mediaforce.library.evidence_queue import EvidenceWorkClaim, claim_next_evidence_work, \
    current_library_item_source_fingerprint, evidence_queue_summary, heartbeat_evidence_work, \
    load_evidence_queue_state, load_evidence_work_claim, mark_evidence_attempt_started, recover_evidence_queue, \
    transition_evidence_work
from mediaforce.library.evidence_state import EVIDENCE_STATE_ANALYSIS_REQUIRED, \
    EVIDENCE_STATE_CLASSIFICATION_REQUIRED, EVIDENCE_STATE_CURRENT, parse_evidence_summary, \
    sync_library_item_evidence_state
from mediaforce.library.probe import probe_evidence

EVIDENCE_JOB_LEASE_SECONDS = 45
EVIDENCE_JOB_HEARTBEAT_SECONDS = 5.0
EVIDENCE_JOB_RETRY_BASE_DELAY_SECONDS = 60
EVIDENCE_JOB_RETRY_MAX_DELAY_SECONDS = 15 * 60
EVIDENCE_JOB_MAX_ATTEMPTS = 3
EVIDENCE_SOURCE_RETRY_DELAY_SECONDS = 30
SourceAvailability = Literal["available", "root_unavailable", "source_unavailable"]


@dataclass(slots=True)
class EvidenceWorkerDeps:
    load_config: Callable[[Path], MediaforceConfig]
    analyze_evidence: Callable[..., dict[str, object]]
    logger: Any
    lease_seconds: int = EVIDENCE_JOB_LEASE_SECONDS
    heartbeat_seconds: float = EVIDENCE_JOB_HEARTBEAT_SECONDS
    retry_base_delay_seconds: int = EVIDENCE_JOB_RETRY_BASE_DELAY_SECONDS
    retry_max_delay_seconds: int = EVIDENCE_JOB_RETRY_MAX_DELAY_SECONDS
    max_attempts: int = EVIDENCE_JOB_MAX_ATTEMPTS
    source_retry_delay_seconds: int = EVIDENCE_SOURCE_RETRY_DELAY_SECONDS


def default_evidence_worker_deps() -> EvidenceWorkerDeps:
    return EvidenceWorkerDeps(
        load_config=load_config,
        analyze_evidence=probe_evidence,
        logger=logging.getLogger(__name__),
    )


def process_evidence_queue_once(
        *,
        config_path: Path,
        config: MediaforceConfig | None = None,
        deps: EvidenceWorkerDeps | None = None,
) -> bool:
    runtime_deps = deps or default_evidence_worker_deps()
    current_config = config or runtime_deps.load_config(config_path)
    worker_id = _evidence_worker_id()
    with open_db(current_config.paths.db_path) as connection:
        recover_evidence_queue(connection, reclaim_all_running=False)
        claim = claim_next_evidence_work(
            connection,
            worker_id=worker_id,
            lease_seconds=runtime_deps.lease_seconds,
        )
    if claim is None:
        return False
    _run_evidence_claim(
        config=current_config,
        claim=claim,
        deps=runtime_deps,
    )
    return True


def run_evidence_queue_until_blocked(
        *,
        config_path: Path,
        config: MediaforceConfig | None = None,
        deps: EvidenceWorkerDeps | None = None,
        max_work_items: int = 1,
        max_seconds: float | None = None,
) -> dict[str, Any]:
    if max_work_items <= 0:
        raise ValueError("Evidence worker item budget must be greater than zero.")
    if max_seconds is not None and max_seconds <= 0:
        raise ValueError("Evidence worker time budget must be greater than zero.")
    runtime_deps = deps or default_evidence_worker_deps()
    current_config = config or runtime_deps.load_config(config_path)
    processed = 0
    started_at = time.monotonic()
    stop_reason = "blocked"
    while processed < max_work_items:
        if max_seconds is not None and time.monotonic() - started_at >= max_seconds:
            stop_reason = "time_budget"
            break
        if not process_evidence_queue_once(
            config_path=config_path,
            config=current_config,
            deps=runtime_deps,
        ):
            break
        processed += 1
        if processed >= max_work_items:
            stop_reason = "item_budget"
    with open_db(current_config.paths.db_path) as connection:
        summary = evidence_queue_summary(connection)
    summary["processed_count"] = processed
    summary["stop_reason"] = stop_reason
    return summary


def _run_evidence_claim(
        *,
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        deps: EvidenceWorkerDeps,
) -> None:
    with open_db(config.paths.db_path) as connection:
        active_claim = _reload_owned_claim(connection, claim)
    if active_claim is None:
        return
    claim = active_claim
    if claim.evidence_state == EVIDENCE_STATE_CURRENT:
        _finish_claim(
            config,
            claim,
            work_status="completed",
            reset_attempt_state=True,
        )
        return
    if claim.evidence_state == EVIDENCE_STATE_CLASSIFICATION_REQUIRED:
        _run_classification_claim(config, claim)
        return

    source_path = Path(claim.source_path)
    source_availability, actual_fingerprint = _source_fingerprint(config, claim, source_path)
    if source_availability == "root_unavailable":
        _defer_unavailable_source(config, claim, deps, restore_attempt=False)
        return
    if source_availability == "source_unavailable":
        _finish_claim(
            config,
            claim,
            work_status="failed",
            last_error="Source file is unavailable; run a scan before starting new evidence work.",
        )
        return
    if actual_fingerprint != claim.expected_source_fingerprint:
        _finish_claim(
            config,
            claim,
            work_status="failed",
            last_error="Source changed since the catalog snapshot; run a scan before retrying evidence work.",
        )
        return

    with open_db(config.paths.db_path) as connection:
        active_claim = mark_evidence_attempt_started(connection, claim)
    if active_claim is None:
        return
    claim = active_claim

    process_controller = ManagedProcessController()
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_evidence_heartbeat_loop,
        kwargs={
            "config": config,
            "claim": claim,
            "process_controller": process_controller,
            "stop_event": heartbeat_stop,
            "deps": deps,
        },
        daemon=True,
        name=f"evidence-heartbeat-{claim.library_item_id}-{claim.evidence_kind}",
    )
    heartbeat_thread.start()
    try:
        process_controller.throw_if_cancelled()
        summary = deps.analyze_evidence(
            source_path,
            claim.evidence_kind,
            process_controller=process_controller,
        )
        process_controller.throw_if_cancelled()
    except ProcessCancelledError:
        _finish_claim(
            config,
            claim,
            work_status="cancelled",
            last_error="Evidence work was cancelled.",
            restore_attempt=True,
        )
        return
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        source_availability, _current_fingerprint = _source_fingerprint(config, claim, source_path)
        if source_availability == "root_unavailable":
            _defer_unavailable_source(config, claim, deps, restore_attempt=True)
        elif source_availability == "source_unavailable":
            _finish_claim(
                config,
                claim,
                work_status="failed",
                last_error="Source file became unavailable during evidence analysis; the result was discarded.",
                restore_attempt=True,
            )
        else:
            _retry_or_fail(config, claim, deps, exc)
        return
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)
        process_controller.reset()

    final_availability, final_fingerprint = _source_fingerprint(config, claim, source_path)
    if final_availability == "root_unavailable":
        _defer_unavailable_source(config, claim, deps, restore_attempt=True)
        return
    if final_availability == "source_unavailable":
        _finish_claim(
            config,
            claim,
            work_status="failed",
            last_error="Source file became unavailable during evidence analysis; the result was discarded.",
            restore_attempt=True,
        )
        return
    if final_fingerprint != actual_fingerprint:
        _finish_claim(
            config,
            claim,
            work_status="failed",
            last_error="Source changed during evidence analysis; the result was discarded.",
            restore_attempt=True,
        )
        return
    _commit_evidence_summary(
        config,
        claim,
        summary,
        actual_fingerprint=final_fingerprint,
        classification_only=False,
    )


def _run_classification_claim(
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
) -> None:
    summary = parse_evidence_summary(claim.canonical_summary_json)
    if summary is None:
        _finish_claim(
            config,
            claim,
            work_status="failed",
            last_error="Stored evidence could not be reclassified because its canonical JSON is malformed.",
        )
        return
    if claim.evidence_kind == CADENCE_EVIDENCE_KIND:
        reclassified = reclassify_cadence_summary(summary)
    elif claim.evidence_kind == MEDIA_FINGERPRINT_EVIDENCE_KIND:
        reclassified = reclassify_media_fingerprint_summary(summary)
    else:
        raise ValueError(f"Unsupported evidence kind: {claim.evidence_kind}")
    _commit_evidence_summary(
        config,
        claim,
        reclassified,
        actual_fingerprint=claim.expected_source_fingerprint,
        classification_only=True,
    )


def _commit_evidence_summary(
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        summary: dict[str, object],
        *,
        actual_fingerprint: str | None,
        classification_only: bool,
) -> None:
    summary_json = json.dumps(summary, separators=(",", ":"), sort_keys=True)
    with open_db(config.paths.db_path) as connection:
        active_claim = _reload_owned_claim(connection, claim)
        if active_claim is None:
            return
        queue_state = load_evidence_queue_state(connection)
        if (
                str(queue_state.get("batch_id") or "") != claim.batch_id
                or bool(queue_state.get("cancel_requested"))
        ):
            transition_evidence_work(
                connection,
                claim,
                work_status="cancelled",
                last_error="Evidence work was cancelled before its result committed.",
                restore_attempt=not classification_only,
            )
            return
        current_source_fingerprint = current_library_item_source_fingerprint(
            connection,
            claim.library_item_id,
        )
        if (
                actual_fingerprint is None
                or actual_fingerprint != claim.expected_source_fingerprint
                or current_source_fingerprint != claim.expected_source_fingerprint
        ):
            transition_evidence_work(
                connection,
                claim,
                work_status="failed",
                last_error="Source changed before evidence commit; the result was discarded.",
                restore_attempt=not classification_only,
            )
            return
        column = (
            library_items.c.cadence_summary_json
            if claim.evidence_kind == CADENCE_EVIDENCE_KIND
            else library_items.c.media_fingerprint_json
        )
        connection.execute(
            update(library_items)
            .where(library_items.c.id == claim.library_item_id)
            .values({column.name: summary_json})
        )
        item = connection.execute(
            select(library_items).where(library_items.c.id == claim.library_item_id)
        ).mappings().one()
        projection = sync_library_item_evidence_state(
            connection,
            item,
            claim.evidence_kind,
            preserve_source_identity=False,
            preserve_policy_identity=False,
        )
        if projection.state == EVIDENCE_STATE_CURRENT:
            transition_evidence_work(
                connection,
                claim,
                work_status="completed",
                reset_attempt_state=True,
            )
        elif classification_only and projection.state == EVIDENCE_STATE_ANALYSIS_REQUIRED:
            if (
                    claim.evidence_kind == MEDIA_FINGERPRINT_EVIDENCE_KIND
                    and claim.work_priority != 0
            ):
                transition_evidence_work(
                    connection,
                    claim,
                    work_status="completed",
                    reset_attempt_state=True,
                )
            else:
                transition_evidence_work(
                    connection,
                    claim,
                    work_status="queued",
                )
        else:
            transition_evidence_work(
                connection,
                claim,
                work_status="failed",
                last_error=(
                    f"Evidence analysis completed with non-current result: "
                    f"{projection.reason or projection.state}."
                ),
            )


def _evidence_heartbeat_loop(
        *,
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        process_controller: ManagedProcessController,
        stop_event: threading.Event,
        deps: EvidenceWorkerDeps,
) -> None:
    while not stop_event.wait(deps.heartbeat_seconds):
        try:
            with open_db(config.paths.db_path) as connection:
                heartbeat_status = heartbeat_evidence_work(
                    connection,
                    claim,
                    lease_seconds=deps.lease_seconds,
                    process_pid=process_controller.pid,
                )
        except Exception:
            deps.logger.exception("Evidence heartbeat failed; cancelling active media analysis")
            process_controller.cancel()
            return
        if heartbeat_status != "active":
            if heartbeat_status == "claim_lost":
                deps.logger.warning("Evidence claim ownership was lost; cancelling active media analysis")
            process_controller.cancel()
            return


def _retry_or_fail(
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        deps: EvidenceWorkerDeps,
        exc: Exception,
) -> None:
    error_message = _error_message(exc)
    if claim.attempt_count >= deps.max_attempts:
        _finish_claim(
            config,
            claim,
            work_status="failed",
            last_error=error_message,
        )
        return
    retry_delay = _retry_delay_seconds(claim.attempt_count, deps)
    retry_not_before = (
        datetime.now(UTC) + timedelta(seconds=retry_delay)
    ).isoformat(timespec="seconds")
    _finish_claim(
        config,
        claim,
        work_status="retry_wait",
        last_error=error_message,
        retry_not_before=retry_not_before,
    )


def _defer_unavailable_source(
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        deps: EvidenceWorkerDeps,
        *,
        restore_attempt: bool,
) -> None:
    retry_not_before = (
        datetime.now(UTC) + timedelta(seconds=deps.source_retry_delay_seconds)
    ).isoformat(timespec="seconds")
    _finish_claim(
        config,
        claim,
        work_status="waiting_source",
        last_error="Source root is unavailable; evidence work will resume without consuming an attempt.",
        retry_not_before=retry_not_before,
        restore_attempt=restore_attempt,
    )


def _finish_claim(
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        *,
        work_status: str,
        last_error: str | None = None,
        retry_not_before: str | None = None,
        restore_attempt: bool = False,
        reset_attempt_state: bool = False,
) -> None:
    with open_db(config.paths.db_path) as connection:
        transition_evidence_work(
            connection,
            claim,
            work_status=work_status,
            last_error=last_error,
            retry_not_before=retry_not_before,
            restore_attempt=restore_attempt,
            reset_attempt_state=reset_attempt_state,
        )


def _reload_owned_claim(connection: Any, claim: EvidenceWorkClaim) -> EvidenceWorkClaim | None:
    active_claim = load_evidence_work_claim(
        connection,
        library_item_id=claim.library_item_id,
        evidence_kind=claim.evidence_kind,
    )
    if active_claim is None or active_claim.worker_id != claim.worker_id:
        return None
    return active_claim


def _source_fingerprint(
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        source_path: Path,
) -> tuple[SourceAvailability, str | None]:
    root_path = config.source_root_map.get(claim.media_root)
    if root_path is None:
        return "root_unavailable", None
    try:
        if not root_path.is_dir():
            return "root_unavailable", None
        if not source_path.is_file():
            return "source_unavailable", None
        stat_result = source_path.stat()
        if claim.uses_content_fingerprint:
            return "available", content_version_fingerprint(source_path, stat_result)
        return "available", file_fingerprint(source_path, stat_result, claim.duration_seconds)
    except OSError:
        try:
            if not root_path.is_dir():
                return "root_unavailable", None
        except OSError:
            return "root_unavailable", None
        return "source_unavailable", None


def _retry_delay_seconds(attempt_count: int, deps: EvidenceWorkerDeps) -> int:
    exponent = max(attempt_count - 1, 0)
    delay = deps.retry_base_delay_seconds * (2 ** exponent)
    return min(delay, deps.retry_max_delay_seconds)


def _evidence_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.current_thread().name}"


def _error_message(exc: Exception) -> str:
    text = str(exc).strip()
    return text[:2000] if text else exc.__class__.__name__
