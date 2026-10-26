import json
import logging
import subprocess
import time
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.exc import SQLAlchemyError

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import (
    evidence_queue_state,
    item_events,
    library_items,
    source_admissions,
    source_transitions,
    source_versions,
    staged_artifacts,
)
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.core.utils import timestamp
from mediaforce.library.background_work import background_work_is_paused
from mediaforce.library.evidence_state import EVIDENCE_KINDS
from mediaforce.library.item_presence import LIBRARY_ITEM_PRESENT
from mediaforce.library.probe import _probe_payload
from mediaforce.library.source_evidence import (
    HISTORICAL_OUTPUT_PROFILES,
    SourceEvidenceContext,
    append_version,
    ensure_active_source_version,
    json_text,
    load_source_evidence,
    load_source_evidence_context,
    normalize_codec,
    observe_file,
    pending_source_operation,
)

LOGGER = logging.getLogger(__name__)
INTAKE_RETRY_SECONDS = 900
INTAKE_HASH_BUDGET_SECONDS = 900
INTAKE_MAX_RETRY_SECONDS = 24 * 60 * 60
HISTORICAL_ENCODER_ALIASES = ("libsvtav1", "svt-av1", "svt_av1", "svtav1", "av1_videotoolbox")


def _history_present(connection: DBClient, item_id: int) -> bool:
    return any(
        connection.execute(select(exists().where(condition))).scalar()
        for condition in (
            (staged_artifacts.c.library_item_id == item_id) & staged_artifacts.c.encode_completed_at.is_not(None),
            (source_transitions.c.library_item_id == item_id),
            (item_events.c.library_item_id == item_id)
            & item_events.c.event_type.in_(
                ("encoding_completed", "promotion_completed", "source_replaced", "originals_removed_confirmed")
            ),
        )
    )


def intake_allowed(connection: DBClient) -> bool:
    queue = connection.execute(select(evidence_queue_state)).mappings().first()
    return not background_work_is_paused(connection) and not (queue and (queue["is_paused"] or queue["cancel_requested"]))


def intake_candidate(connection: DBClient, config: MediaforceConfig) -> dict[str, Any] | None:
    if not intake_allowed(connection):
        return None
    recent = (datetime.now(UTC) - timedelta(seconds=INTAKE_RETRY_SECONDS)).isoformat(timespec="seconds")
    assessed = exists().where(
        source_admissions.c.version_id == library_items.c.active_source_version_id, source_admissions.c.recorded_at > recent
    )
    rows = connection.execute(
        select(library_items)
        .where(
            LIBRARY_ITEM_PRESENT,
            library_items.c.media_root.in_(tuple(config.source_root_map)),
            library_items.c.video_codec.in_(HISTORICAL_OUTPUT_PROFILES["known_input_codecs"]),
            ~assessed,
        )
        .order_by(library_items.c.id)
    ).mappings()
    for row in rows:
        if _history_present(connection, int(row["id"])):
            continue
        previous = (
            connection.execute(
                select(source_admissions)
                .where(source_admissions.c.version_id == row["active_source_version_id"])
                .order_by(source_admissions.c.recorded_at.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        checks = json.loads(previous["checks_json"]) if previous else {}
        if previous:
            delay = min(INTAKE_MAX_RETRY_SECONDS, INTAKE_RETRY_SECONDS * 2 ** min(max(0, checks.get("attempts", 1) - 1), 7))
            if datetime.now(UTC) < datetime.fromisoformat(previous["recorded_at"]) + timedelta(seconds=delay):
                continue
        if checks.get("reason") in {
            "mediaforce_stamp",
            "historical_output_codec_or_unknown",
            "historical_output_encoder",
            "known_output_match",
        }:
            continue
        return dict(row)
    return None


def assess_readable_source(
    connection: DBClient, identity: Mapping[str, Any], probe: Mapping[str, Any], *, context: SourceEvidenceContext | None = None
) -> dict[str, Any]:
    streams = probe.get("streams")
    format_info = probe.get("format")
    checks: dict[str, Any] = {
        "basis": "readable-source",
        "codec_rule_hash": stable_json_hash(HISTORICAL_OUTPUT_PROFILES),
        "encoder_rule_hash": stable_json_hash(HISTORICAL_ENCODER_ALIASES),
        "reason": "raw_probe_unavailable",
    }
    if not isinstance(streams, list) or not isinstance(format_info, dict) or not streams:
        return checks
    containers = [format_info, *streams]
    if any(not isinstance(container, dict) or not isinstance(container.get("tags", {}), dict) for container in containers):
        return checks
    tags = [
        (str(key).casefold(), str(value).casefold()) for container in containers for key, value in container.get("tags", {}).items()
    ]
    if any(key == "mediaforce_encoded_by" for key, _value in tags):
        return {**checks, "reason": "mediaforce_stamp"}
    codec = next((normalize_codec(stream.get("codec_name")) for stream in streams if stream.get("codec_type") == "video"), "")
    checks.update(video_codec=codec, stamp="absent", raw_probe=probe)
    if codec not in HISTORICAL_OUTPUT_PROFILES["known_input_codecs"]:
        return {**checks, "reason": "historical_output_codec_or_unknown"}
    if any(alias in value for key, value in tags if "encoder" in key for alias in HISTORICAL_ENCODER_ALIASES):
        return {**checks, "reason": "historical_output_encoder"}
    if not identity.get("sha256"):
        return {**checks, "reason": "digest_unavailable"}
    context = context or load_source_evidence_context(connection)
    if context.rule_conflicts(codec):
        return {**checks, "reason": "historical_output_conflict"}
    output_check = context.output_check({**identity, "video_codec": codec})
    return {
        **checks,
        "output_check": output_check,
        "output_index_hash": context.output_index_hash,
        "reason": None if output_check == "clear" else f"known_output_{output_check}",
    }


def commit_readable_source(
    connection: DBClient, item: Mapping[str, Any], identity: Mapping[str, Any], probe: Mapping[str, Any]
) -> str:
    connection.commit()
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    if not intake_allowed(connection):
        raise ProcessCancelledError("Source intake was paused")
    current = connection.execute(select(library_items).where(library_items.c.id == item["id"])).mappings().one()
    if (
        current["source_revision"] != item["source_revision"]
        or pending_source_operation(connection, int(item["id"]))
        or current["size_bytes"] != identity["size_bytes"]
        or current["content_version_fingerprint"] != identity["sample_fingerprint"]
        or current["mtime_ns"] != identity["stat"][3]
        or _history_present(connection, int(item["id"]))
    ):
        raise RuntimeError("Source changed before intake commit")
    checks = assess_readable_source(connection, identity, probe)
    active = ensure_active_source_version(connection, current)
    previous = connection.execute(select(source_versions).where(source_versions.c.version_id == active)).mappings().one()
    if previous["role"] == "output":
        raise RuntimeError("Known output cannot be admitted as an original")
    now = timestamp()
    version = (
        active
        if previous["sha256"] == identity.get("sha256") and previous["role"] == "input"
        else append_version(
            connection,
            generation_id=previous["generation_id"],
            role="input",
            identity=identity,
            profile={**json.loads(previous["profile_json"]), "video_codec": checks.get("video_codec")},
            recorded_at=now,
        )
    )
    connection.execute(
        update(library_items)
        .where(library_items.c.id == item["id"])
        .values(active_source_version_id=version, source_revision=library_items.c.source_revision + 1)
    )
    prior = (
        connection.execute(
            select(source_admissions).where(
                source_admissions.c.generation_id == previous["generation_id"],
                source_admissions.c.admission_id.not_in(
                    select(source_admissions.c.supersedes_id).where(source_admissions.c.supersedes_id.is_not(None))
                ),
            )
        )
        .mappings()
        .one_or_none()
    )
    checks["measurement_versions"] = [active, version]
    checks["attempts"] = (json.loads(prior["checks_json"]).get("attempts", 0) if prior else 0) + 1
    admission_id = uuid4().hex
    connection.execute(
        source_admissions.insert().values(
            admission_id=admission_id,
            generation_id=previous["generation_id"],
            version_id=version,
            tier="unknown" if checks.get("reason") else "probable",
            basis="readable-source",
            checks_json=json_text(checks),
            supersedes_id=prior["admission_id"] if prior else None,
            recorded_at=now,
        )
    )
    return version


def queue_source_measurements(connection: DBClient, config: MediaforceConfig, item: Mapping[str, Any]) -> None:
    from mediaforce.library.evidence_queue import (
        EVIDENCE_WORK_PRIORITY_REPRESENTATIVE,
        EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
        queue_decision_evidence_work,
    )
    from mediaforce.library.evidence_state import sync_library_item_evidence_state

    evidence = load_source_evidence(connection, int(item["id"]))
    for kind in EVIDENCE_KINDS:
        if evidence.traits[kind].tier == "probable":
            continue
        sync_library_item_evidence_state(connection, item, kind)
        queue_decision_evidence_work(
            connection,
            config,
            str(item["rel_path"]),
            library_item_ids=[int(item["id"])],
            evidence_kind=kind,
            work_reason=EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
            start_new_batch=True,
            require_measurement=True,
            priority=EVIDENCE_WORK_PRIORITY_REPRESENTATIVE,
            manage_transaction=False,
        )


def process_source_intake_once(
    config: MediaforceConfig,
    *,
    stop_event: threading.Event | None = None,
) -> bool:
    with open_db(config.paths.db_path) as connection:
        item = intake_candidate(connection, config)
    if item is None:
        return False
    controller = ManagedProcessController()
    path = Path(item["source_path"])
    monitor_stop = threading.Event()

    def monitor() -> None:
        from mediaforce.library.evidence_queue import runnable_evidence_work_count

        while not monitor_stop.wait(0.5):
            try:
                with open_db(config.paths.db_path) as database:
                    should_stop = not intake_allowed(database) or runnable_evidence_work_count(database) > 0
                if should_stop or (stop_event is not None and stop_event.is_set()):
                    controller.cancel()
                    return
            except (OSError, RuntimeError, SQLAlchemyError):
                LOGGER.exception("Intake cancellation check failed")
                controller.cancel()
                return

    monitor_thread = threading.Thread(target=monitor, name="intake-cancellation")
    monitor_thread.start()
    before = None
    try:
        deadline = time.monotonic() + INTAKE_HASH_BUDGET_SECONDS
        last_pause_check = 0.0

        def check_active() -> None:
            nonlocal last_pause_check
            controller.throw_if_cancelled()
            now = time.monotonic()
            if now - last_pause_check < 1:
                return
            last_pause_check = now
            with open_db(config.paths.db_path) as database:
                if not intake_allowed(database) or (stop_event is not None and stop_event.is_set()):
                    raise ProcessCancelledError("Background intake was paused")

        def check_hash_budget() -> None:
            check_active()
            if time.monotonic() > deadline:
                raise RuntimeError("Intake I/O budget is exhausted")

        before = observe_file(path)
        with open_db(config.paths.db_path) as database:
            previous = database.execute(
                select(source_versions.c.identity_json).where(source_versions.c.version_id == item["active_source_version_id"])
            ).scalar_one_or_none()
        saved = json.loads(previous) if previous else {}
        if saved.get("sha256") and all(
            saved.get(key) == before[key] for key in ("path", "stat", "size_bytes", "sample_fingerprint")
        ):
            before["sha256"] = saved["sha256"]
        else:
            before = observe_file(path, full_digest=True, check_cancelled=check_hash_budget)
        probe = _probe_payload(path, process_controller=controller)
        after = observe_file(path)
        if any(before[key] != after[key] for key in ("stat", "size_bytes", "sample_fingerprint")):
            raise RuntimeError("Source changed during intake")
        with open_db(config.paths.db_path) as connection:
            checks = assess_readable_source(connection, before, probe)
            version = commit_readable_source(connection, item, before, probe)
            if not checks.get("reason"):
                current = connection.execute(select(library_items).where(library_items.c.id == item["id"])).mappings().one()
                if current["active_source_version_id"] != version:
                    raise RuntimeError("Source changed before queuing its measurements")
                queue_source_measurements(connection, config, dict(current))
    except ProcessCancelledError:
        return False
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        LOGGER.exception("Source intake deferred for item %s", item["id"])
        with open_db(config.paths.db_path) as database:
            current = database.execute(select(library_items).where(library_items.c.id == item["id"])).mappings().one()
            if current["source_revision"] == item["source_revision"] and not pending_source_operation(database, int(item["id"])):
                if before is not None and before.get("sha256"):
                    commit_readable_source(database, item, before, {})
                    return True
                active = ensure_active_source_version(database, current)
                generation = database.execute(
                    select(source_versions.c.generation_id).where(source_versions.c.version_id == active)
                ).scalar_one()
                prior = (
                    database.execute(
                        select(source_admissions)
                        .where(source_admissions.c.version_id == active)
                        .order_by(source_admissions.c.recorded_at.desc())
                        .limit(1)
                    )
                    .mappings()
                    .one_or_none()
                )
                database.execute(
                    source_admissions.insert().values(
                        admission_id=uuid4().hex,
                        generation_id=generation,
                        version_id=active,
                        tier="unknown",
                        basis="readable-source",
                        supersedes_id=prior["admission_id"] if prior else None,
                        checks_json=json_text(
                            {
                                "reason": "intake_retry_pending",
                                "attempts": (json.loads(prior["checks_json"]).get("attempts", 0) if prior else 0) + 1,
                            }
                        ),
                        recorded_at=timestamp(),
                    )
                )
    finally:
        monitor_stop.set()
        monitor_thread.join()
        controller.reset()
    return True
