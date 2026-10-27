import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from functools import partial
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import library_items
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError, ProcessDeadlineEnforcementError
from mediaforce.core.utils import content_version_fingerprint, file_fingerprint
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND, reclassify_cadence_summary
from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_EVIDENCE_KIND, fingerprint_video_decode_timed_out, reclassify_media_fingerprint_summary
from mediaforce.encoding.remote_media import RemoteMediaCommands, RemoteMediaConnections, RemoteMediaHostUnavailableError
from mediaforce.library.evidence_hosts import EVIDENCE_HOST_WAIT_REASON, collect_evidence_host_rows, \
    evidence_source_path_on_host, remote_evidence_hosts, select_evidence_host, unmeasurable_evidence_roots
from mediaforce.library.evidence_queue import DEFAULT_EVIDENCE_BATCH_LIMIT, SOURCE_ROOT_WAIT_REASON, EVIDENCE_WORK_REASON_ORIGINAL_SOURCE, EvidenceWorkClaim, claim_next_evidence_work, \
    current_library_item_source_fingerprint, evidence_queue_summary, extend_evidence_claim, heartbeat_evidence_work, \
    load_evidence_queue_state, load_evidence_work_claim, mark_evidence_attempt_started, original_intake_hold, recover_evidence_queue, \
    transition_evidence_work, waiting_evidence_host_roots
from mediaforce.library.evidence_state import EVIDENCE_KINDS, EVIDENCE_STATE_ANALYSIS_REQUIRED, \
    EVIDENCE_STATE_CLASSIFICATION_REQUIRED, EVIDENCE_STATE_CURRENT, parse_evidence_summary, \
    sync_library_item_evidence_state
from mediaforce.library.probe import probe_evidence, probe_evidence_source
from mediaforce.library.source_evidence import load_source_evidence, observe_file, pending_source_operation, record_current_measurement
from mediaforce.library.source_prefetch import SourceAdmissionPrefetcher

EVIDENCE_JOB_LEASE_SECONDS = 45
EVIDENCE_JOB_HEARTBEAT_SECONDS = 5.0
EVIDENCE_JOB_RETRY_BASE_DELAY_SECONDS = 60
EVIDENCE_JOB_RETRY_MAX_DELAY_SECONDS = 15 * 60
EVIDENCE_JOB_MAX_ATTEMPTS = 3
EVIDENCE_SOURCE_RETRY_DELAY_SECONDS = 30
SourceAvailability = Literal["available", "root_unavailable", "source_unavailable"]
REMOTE_SOURCE_MISMATCH_ERROR = (
    "The encode computer does not see the same copy of this file; run a scan before retrying evidence work."
)


class EvidenceWorkDeferred(ProcessCancelledError):
    pass


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
    evidence_host_rows: Callable[[MediaforceConfig], list[dict[str, Any]]] = collect_evidence_host_rows
    shutdown_event: threading.Event | None = None
    prepare_next_original: Callable[[MediaforceConfig, EvidenceWorkClaim], None] | None = None
    remote_commands: Callable[[dict[str, object]], RemoteMediaCommands] | None = None
    read_probe: Callable[..., dict[str, object]] = probe_evidence_source


@dataclass(frozen=True, slots=True)
class _AnalysisTarget:
    source_path: Path
    command_runner: RemoteMediaCommands | None


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
        excluded_media_roots: Collection[str] | None = None,
        max_work_items: int | None = 1,
) -> int:
    if max_work_items is not None and max_work_items <= 0:
        raise ValueError("Evidence worker item budget must be greater than zero.")
    runtime_deps = deps or default_evidence_worker_deps()
    current_config = config or runtime_deps.load_config(config_path)
    roots = excluded_media_roots if excluded_media_roots is not None else _unmeasurable_roots(current_config, runtime_deps)
    worker_id = _evidence_worker_id()
    with open_db(current_config.paths.db_path) as connection:
        recover_evidence_queue(connection, reclaim_all_running=False)
        claim = claim_next_evidence_work(
            connection,
            worker_id=worker_id,
            lease_seconds=runtime_deps.lease_seconds,
            excluded_media_roots=roots,
        )
    if claim is None:
        return 0
    return _run_evidence_claim(
        config=current_config,
        claim=claim,
        deps=runtime_deps,
        excluded_media_roots=roots,
        max_work_items=max_work_items,
    )


def run_evidence_queue_until_blocked(
        *,
        config_path: Path,
        config: MediaforceConfig | None = None,
        deps: EvidenceWorkerDeps | None = None,
        max_work_items: int | None = 1,
        max_seconds: float | None = None,
        stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    if max_work_items is not None and max_work_items <= 0:
        raise ValueError("Evidence worker item budget must be greater than zero.")
    if max_seconds is not None and max_seconds <= 0:
        raise ValueError("Evidence worker time budget must be greater than zero.")
    runtime_deps = deps or default_evidence_worker_deps()
    if stop_event is not None:
        runtime_deps = replace(runtime_deps, shutdown_event=stop_event)
    current_config = config or runtime_deps.load_config(config_path)
    processed = 0
    progress_since_discovery = 0
    started_at = time.monotonic()
    stop_reason = "blocked"
    stop_requested = stop_event.is_set if stop_event is not None else lambda: False
    with RemoteMediaConnections() as connections, SourceAdmissionPrefetcher(stop_event=stop_event, logger=runtime_deps.logger,
                                 deadline=started_at + max_seconds if max_seconds is not None else None) as prefetch:
        while max_work_items is None or processed < max_work_items:
            if stop_requested():
                stop_reason = "shutdown"
                break
            if max_seconds is not None and time.monotonic() - started_at >= max_seconds:
                stop_reason = "time_budget"
                break
            if config is None and processed:
                current_config = runtime_deps.load_config(config_path)
            roots = _unmeasurable_roots(current_config, runtime_deps)
            if stop_requested():
                stop_reason = "shutdown"
                break
            prepare_next = partial(prefetch.start, excluded_media_roots=roots)
            runtime_deps = replace(runtime_deps, remote_commands=runtime_deps.remote_commands or connections.runner, prepare_next_original=(
                prepare_next if max_work_items is None or processed + 1 < max_work_items else None
            ))
            completed = process_evidence_queue_once(config_path=config_path, config=current_config,
                deps=runtime_deps, excluded_media_roots=roots,
                max_work_items=None if max_work_items is None else max_work_items - processed)
            if not completed:
                if max_work_items is None and progress_since_discovery and not stop_requested():
                    progress_since_discovery = 0
                    if _discover_originals(current_config, runtime_deps):
                        continue
                break
            prefetch.wait()
            processed += completed
            progress_since_discovery += completed
            if max_work_items is None and progress_since_discovery >= DEFAULT_EVIDENCE_BATCH_LIMIT and not stop_requested():
                _discover_originals(current_config, runtime_deps)
                progress_since_discovery = 0
            if max_work_items is not None and processed >= max_work_items:
                stop_reason = "item_budget"
    with open_db(current_config.paths.db_path) as connection:
        summary = evidence_queue_summary(connection)
    summary["processed_count"] = processed
    summary["stop_reason"] = stop_reason
    return summary


def _unmeasurable_roots(config: MediaforceConfig, deps: EvidenceWorkerDeps) -> frozenset[str]:
    if not remote_evidence_hosts(config):
        return frozenset()
    roots = unmeasurable_evidence_roots(config, deps.evidence_host_rows(config))
    with open_db(config.paths.db_path) as connection:
        return roots | waiting_evidence_host_roots(connection)


def _discover_originals(config: MediaforceConfig, deps: EvidenceWorkerDeps) -> int:
    from mediaforce.library.source_intake import queue_original_intake

    try:
        with open_db(config.paths.db_path) as connection:
            state = load_evidence_queue_state(connection)
            if state["is_paused"] or state["cancel_requested"] or state["status"] in {"cancelled", "cancel_requested"}:
                return 0
            return queue_original_intake(connection, config)
    except (OSError, RuntimeError, ValueError, SQLAlchemyError):
        deps.logger.exception("Original discovery failed; queued analysis can still continue")
        return 0


def _run_evidence_claim(
        *,
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        deps: EvidenceWorkerDeps,
        excluded_media_roots: Collection[str] = (),
        max_work_items: int | None = 1,
) -> int:
    with open_db(config.paths.db_path) as connection:
        active_claim = _reload_owned_claim(connection, claim)
    if active_claim is None:
        return 1
    claim = active_claim
    original_work = claim.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE
    if claim.evidence_state == EVIDENCE_STATE_CURRENT and not original_work:
        _finish_claim(
            config,
            claim,
            work_status="completed",
            reset_attempt_state=True,
        )
        return 1
    if claim.evidence_state == EVIDENCE_STATE_CLASSIFICATION_REQUIRED and not original_work:
        _run_classification_claim(config, claim)
        return 1

    source_path = Path(claim.source_path)
    source_availability, actual_fingerprint = _source_fingerprint(config, claim, source_path)
    if source_availability == "root_unavailable":
        _defer_unavailable_source(config, claim, deps, restore_attempt=False)
        return 1
    if source_availability == "source_unavailable":
        _finish_claim(
            config,
            claim,
            work_status="failed",
            last_error="Source file is unavailable; run a scan before starting new evidence work.",
        )
        return 1
    if actual_fingerprint != claim.expected_source_fingerprint:
        _finish_claim(
            config,
            claim,
            work_status="failed",
            last_error="Source changed since the catalog snapshot; run a scan before retrying evidence work.",
        )
        return 1

    process_controller = ManagedProcessController()
    claims = [claim]
    started: set[str] = set()
    heartbeats = [_ClaimHeartbeat(config=config, claim=claim, process_controller=process_controller, deps=deps)]
    shared_probe_started = False

    def stop_heartbeats() -> None:
        for heartbeat in heartbeats:
            heartbeat.stop()

    try:
        if original_work:
            from mediaforce.library.source_intake import assess_source_input
            from mediaforce.library.evidence_queue import EVIDENCE_WORK_PRIORITY_REPRESENTATIVE, queue_decision_evidence_work

            with open_db(config.paths.db_path) as connection:
                active_claim = mark_evidence_attempt_started(connection, claim)
            if active_claim is None:
                return 1
            claim = active_claim
            claims[0] = claim
            started.add(claim.evidence_kind)
            last_guard_check = 0.0

            def guard_intake() -> None:
                nonlocal last_guard_check
                shutting_down = deps.shutdown_event is not None and deps.shutdown_event.is_set()
                now = time.monotonic()
                if now - last_guard_check < 1 and not shutting_down:
                    return
                last_guard_check = now
                with open_db(config.paths.db_path) as database:
                    hold = original_intake_hold(database, batch_id=claim.batch_id, priority=claim.work_priority, excluded_media_roots=excluded_media_roots)
                if hold == "cancelled":
                    raise ProcessCancelledError("Evidence work was cancelled.")
                if shutting_down:
                    raise EvidenceWorkDeferred("Original checks will resume after the app restarts")
                if hold == "paused":
                    raise EvidenceWorkDeferred("Original checks are paused")
                if hold == "higher_priority":
                    raise EvidenceWorkDeferred("Waiting for a file that needs a check sooner")

            with process_controller.activity_guard(guard_intake):
                assess_source_input(config, claim.library_item_id, process_controller, process_controller.throw_if_cancelled)
            with open_db(config.paths.db_path) as connection:
                evidence = load_source_evidence(connection, claim.library_item_id)
                item = connection.execute(select(library_items).where(library_items.c.id == claim.library_item_id)).mappings().one()
                if evidence.availability == "available":
                    for kind in EVIDENCE_KINDS:
                        if kind == claim.evidence_kind or evidence.traits[kind].tier == "probable":
                            continue
                        sync_library_item_evidence_state(connection, cast(dict[str, Any], dict(item)), kind)
                        queue_decision_evidence_work(connection, config, item["rel_path"], library_item_ids=[claim.library_item_id],
                            evidence_kind=kind, work_reason=EVIDENCE_WORK_REASON_ORIGINAL_SOURCE, require_measurement=True,
                            priority=EVIDENCE_WORK_PRIORITY_REPRESENTATIVE, start_new_batch=True, manage_transaction=False)
            if evidence.availability != "available" or (evidence.traits[claim.evidence_kind].tier == "probable" and claim.evidence_state == EVIDENCE_STATE_CURRENT):
                _finish_claim(config, claim, work_status="completed", reset_attempt_state=True)
                return 1
        analysis_target = _analysis_target(config, claim, source_path, deps, process_controller)
        _throw_if_preparation_cancelled(config, claim, process_controller)
        if analysis_target is None:
            raise RemoteMediaHostUnavailableError(EVIDENCE_HOST_WAIT_REASON)
        if max_work_items is None or max_work_items >= 2:
            with open_db(config.paths.db_path) as connection:
                companion = extend_evidence_claim(connection, claim, lease_seconds=deps.lease_seconds,
                                                  excluded_media_roots=excluded_media_roots)
                evidence = load_source_evidence(connection, claim.library_item_id) if companion is not None else None
            if companion is not None:
                if (companion.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE
                        and evidence is not None and (evidence.availability != "available"
                        or (evidence.traits[companion.evidence_kind].tier == "probable"
                            and companion.evidence_state == EVIDENCE_STATE_CURRENT))):
                    _finish_claim(config, companion, work_status="queued")
                else:
                    claims.append(companion)
                    heartbeats.append(_ClaimHeartbeat(config=config, claim=companion,
                                                      process_controller=process_controller, deps=deps))
        if claim.evidence_kind not in started:
            with open_db(config.paths.db_path) as connection:
                active = mark_evidence_attempt_started(connection, claim)
            if active is None:
                raise ProcessCancelledError("Evidence claim ownership was lost.")
            claim = active
            claims[0] = claim
            started.add(claim.evidence_kind)
        remote_before = (analysis_target.command_runner.observe_source(analysis_target.source_path, process_controller=process_controller)
                         if analysis_target.command_runner else None)
        measurement_before = observe_file(source_path)
        if remote_before is not None and any(measurement_before[key] != remote_before.get(key)
                                            for key in ("size_bytes", "sample_fingerprint")):
            stop_heartbeats()
            for owned in claims:
                _finish_claim(config, owned, work_status="failed", last_error=REMOTE_SOURCE_MISMATCH_ERROR)
            return len(claims)
        _throw_if_preparation_cancelled(config, claim, process_controller)
        kwargs: dict[str, Any] = {"process_controller": process_controller}
        if analysis_target.command_runner is not None:
            kwargs["command_runner"] = analysis_target.command_runner
        summaries: dict[str, dict[str, object]] = {}
        errors: dict[str, Exception] = {}
        shared_probe = None
        if original_work and deps.prepare_next_original is not None and (max_work_items is None or max_work_items > len(claims)):
            deps.prepare_next_original(config, claim)
        for owned in tuple(claims):
            if owned != claim:
                with open_db(config.paths.db_path) as connection:
                    active = _reload_owned_claim(connection, owned)
                    if active is None:
                        raise ProcessCancelledError("Evidence claim ownership was lost.")
                    claims[claims.index(owned)] = active
                    owned = active
                    hold = original_intake_hold(connection, batch_id=owned.batch_id, priority=owned.work_priority,
                                                excluded_media_roots=excluded_media_roots)
                if hold == "cancelled":
                    raise ProcessCancelledError("Evidence work was cancelled.")
                if hold in {"higher_priority", "paused"} or (deps.shutdown_event is not None and deps.shutdown_event.is_set()):
                    heartbeats[-1].stop()
                    _finish_claim(config, owned, work_status="queued")
                    claims.remove(owned)
                    continue
            if deps.shutdown_event is not None and deps.shutdown_event.is_set():
                raise EvidenceWorkDeferred("Original checks will resume after the app restarts")
            if owned.evidence_kind not in started:
                with open_db(config.paths.db_path) as connection:
                    active = mark_evidence_attempt_started(connection, owned)
                if active is None:
                    raise ProcessCancelledError("Evidence claim ownership was lost.")
                claims[claims.index(owned)] = active
                owned = active
                started.add(owned.evidence_kind)
            if len(claims) > 1 and shared_probe is None:
                shared_probe_started = True
                shared_probe = deps.read_probe(analysis_target.source_path, **kwargs)
            analysis_kwargs = dict(kwargs)
            if shared_probe is not None:
                analysis_kwargs["probe_payload"] = shared_probe
            if owned.evidence_kind == MEDIA_FINGERPRINT_EVIDENCE_KIND:
                if fingerprint_video_decode_timed_out(parse_evidence_summary(owned.canonical_summary_json) or {}):
                    analysis_kwargs["hardware_decoding"] = False
            try:
                summaries[owned.evidence_kind] = deps.analyze_evidence(
                    analysis_target.source_path, owned.evidence_kind, **analysis_kwargs)
            except (ProcessCancelledError, ProcessDeadlineEnforcementError, RemoteMediaHostUnavailableError):
                raise
            except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError, SQLAlchemyError) as exc:
                errors[owned.evidence_kind] = exc
            process_controller.throw_if_cancelled()
        measurement_after = observe_file(source_path)
        remote_after = (analysis_target.command_runner.observe_source(analysis_target.source_path, process_controller=process_controller)
                        if analysis_target.command_runner else None)
        if measurement_before != measurement_after:
            raise RuntimeError("Source changed during evidence measurement")
    except EvidenceWorkDeferred as exc:
        stop_heartbeats()
        for owned in claims:
            _defer_claim(config, owned, deps, work_status="retry_wait", reason=str(exc), restore_attempt=owned.evidence_kind in started)
        return len(claims)
    except ProcessCancelledError:
        stop_heartbeats()
        for owned in claims:
            _finish_claim(config, owned, work_status="cancelled", last_error="Evidence work was cancelled.",
                          restore_attempt=owned.evidence_kind in started)
        return len(claims)
    except RemoteMediaHostUnavailableError:
        stop_heartbeats()
        for owned in claims:
            _defer_for_encode_computer(config, owned, deps, restore_attempt=owned.evidence_kind in started)
        return len(claims)
    except ProcessDeadlineEnforcementError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError, SQLAlchemyError) as exc:
        stop_heartbeats()
        source_availability, _current_fingerprint = _source_fingerprint(config, claim, source_path)
        for owned in claims:
            if source_availability == "root_unavailable":
                _defer_unavailable_source(config, owned, deps, restore_attempt=owned.evidence_kind in started)
            elif source_availability == "source_unavailable":
                _finish_claim(config, owned, work_status="failed",
                    last_error="Source file became unavailable during evidence analysis; the result was discarded.",
                    restore_attempt=owned.evidence_kind in started)
            else:
                if owned.evidence_kind not in started and not shared_probe_started:
                    _defer_claim(config, owned, deps, work_status="retry_wait", reason=str(exc), restore_attempt=False)
                    continue
                if owned.evidence_kind not in started:
                    with open_db(config.paths.db_path) as connection:
                        active = mark_evidence_attempt_started(connection, owned)
                    if active is None:
                        continue
                    owned = active
                _retry_or_fail(config, owned, deps, exc)
        return len(claims)
    finally:
        stop_heartbeats()
        process_controller.reset()

    final_availability, final_fingerprint = _source_fingerprint(config, claim, source_path)
    for owned in claims:
        if final_availability == "root_unavailable":
            _defer_unavailable_source(config, owned, deps, restore_attempt=True)
        elif final_availability == "source_unavailable" or final_fingerprint != actual_fingerprint or remote_before != remote_after:
            _finish_claim(config, owned, work_status="failed",
                last_error="Source changed during evidence analysis; the result was discarded.", restore_attempt=True)
        elif owned.evidence_kind in errors:
            _retry_or_fail(config, owned, deps, errors[owned.evidence_kind])
        else:
            _commit_evidence_summary(config, owned, summaries[owned.evidence_kind], actual_fingerprint=final_fingerprint,
                classification_only=False, deps=deps,
                observation={"before": measurement_before, "after": measurement_after,
                    "worker_id": owned.worker_id, "lease_batch_id": owned.batch_id,
                    "source_revision": owned.source_revision, "remote_before": remote_before, "remote_after": remote_after})
    return len(claims)


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
        observation: dict[str, Any] | None = None,
        deps: EvidenceWorkerDeps | None = None,
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
        same_identity = (claim.uses_content_fingerprint and current_source_fingerprint == claim.expected_source_fingerprint
                         and claim.source_size_bytes is not None and active_claim.source_size_bytes == claim.source_size_bytes)
        if (pending_source_operation(connection, claim.library_item_id)
                or (active_claim.source_revision != claim.source_revision and not same_identity)):
            transition_evidence_work(connection, claim, work_status="failed",
                                     last_error="Source revision changed before evidence commit.", restore_attempt=True)
            return
        if (
                actual_fingerprint is None
                or actual_fingerprint != claim.expected_source_fingerprint
                or current_source_fingerprint != claim.expected_source_fingerprint
                or (claim.source_size_bytes is not None and active_claim.source_size_bytes != claim.source_size_bytes)
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
        if not classification_only:
            record_current_measurement(
                connection, item, kind=claim.evidence_kind, summary=summary,
                provenance=observation or {"reason": "measurement observation unavailable"},
                binding="observed" if observation else "unbound",
            )
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
            retry_not_before = None
            if deps is not None:
                if claim.attempt_count < deps.max_attempts:
                    retry_not_before = (datetime.now(UTC) + timedelta(seconds=_retry_delay_seconds(claim.attempt_count, deps))).isoformat(timespec="seconds")
            transition_evidence_work(
                connection,
                claim,
                work_status="retry_wait" if retry_not_before else "failed",
                retry_not_before=retry_not_before,
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
        if deps.shutdown_event is not None and deps.shutdown_event.is_set():
            process_controller.cancel(EvidenceWorkDeferred("Analysis will resume after the app restarts"))
            return
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


def _analysis_target(
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        source_path: Path,
        deps: EvidenceWorkerDeps,
        process_controller: ManagedProcessController,
) -> _AnalysisTarget | None:
    """Where the measurement runs: locally when no encode computer is set up, otherwise only on one.

    None means encode computers are set up but none can take the work now, so it waits instead of
    falling back to the controller.
    """
    if not remote_evidence_hosts(config):
        return _AnalysisTarget(source_path=source_path, command_runner=None)
    host = select_evidence_host(config, deps.evidence_host_rows(config), media_root=claim.media_root)
    if host is None:
        return None
    command_runner = (deps.remote_commands or RemoteMediaCommands)(host)
    command_runner.ffmpeg_version(process_controller)
    return _AnalysisTarget(
        source_path=evidence_source_path_on_host(
            config,
            host,
            source_path=claim.source_path,
            media_root=claim.media_root,
            rel_path=claim.rel_path,
        ),
        command_runner=command_runner,
    )


def _throw_if_preparation_cancelled(
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        process_controller: ManagedProcessController,
) -> None:
    """A cancel during host preparation wins over whatever the preparation found.

    The heartbeat may not have noticed the cancel yet, so the batch is read as well.
    """
    process_controller.throw_if_cancelled()
    with open_db(config.paths.db_path) as connection:
        queue_state = load_evidence_queue_state(connection)
    if (
            str(queue_state.get("batch_id") or "") != claim.batch_id
            or bool(queue_state.get("cancel_requested"))
    ):
        raise ProcessCancelledError("Evidence work was cancelled.")



class _ClaimHeartbeat:
    """Keeps the claim's lease alive and cancels its commands when the claim is lost or cancelled."""

    def __init__(
            self,
            *,
            config: MediaforceConfig,
            claim: EvidenceWorkClaim,
            process_controller: ManagedProcessController,
            deps: EvidenceWorkerDeps,
    ) -> None:
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=_evidence_heartbeat_loop,
            kwargs={
                "config": config,
                "claim": claim,
                "process_controller": process_controller,
                "stop_event": self._stop_event,
                "deps": deps,
            },
            name=f"evidence-heartbeat-{claim.library_item_id}-{claim.evidence_kind}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not threading.current_thread():
            self._thread.join()


def _defer_unavailable_source(
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        deps: EvidenceWorkerDeps,
        *,
        restore_attempt: bool,
) -> None:
    _defer_claim(
        config,
        claim,
        deps,
        work_status="waiting_source",
        reason=SOURCE_ROOT_WAIT_REASON,
        restore_attempt=restore_attempt,
    )


def _defer_for_encode_computer(
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        deps: EvidenceWorkerDeps,
        *,
        restore_attempt: bool,
) -> None:
    _defer_claim(
        config,
        claim,
        deps,
        work_status="waiting_host",
        reason=EVIDENCE_HOST_WAIT_REASON,
        restore_attempt=restore_attempt,
    )


def _defer_claim(
        config: MediaforceConfig,
        claim: EvidenceWorkClaim,
        deps: EvidenceWorkerDeps,
        *,
        work_status: str,
        reason: str,
        restore_attempt: bool,
) -> None:
    """Wait for something outside this file to come back, without using up one of its attempts."""
    retry_not_before = (
        datetime.now(UTC) + timedelta(seconds=deps.source_retry_delay_seconds)
    ).isoformat(timespec="seconds")
    _finish_claim(
        config,
        claim,
        work_status=work_status,
        last_error=reason,
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
    if active_claim is None or active_claim.worker_id != claim.worker_id or active_claim.batch_id != claim.batch_id:
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
