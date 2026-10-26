import json
import logging
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import exists, select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import (
    item_events,
    library_items,
    source_admissions,
    source_measurements,
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
    append_measurement,
    append_version,
    ensure_active_source_version,
    json_text,
    load_source_evidence_context,
    normalize_codec,
    observe_file,
    pending_source_operation,
)

LOGGER = logging.getLogger(__name__)
INTAKE_RETRY_SECONDS = 900
INTAKE_HASH_BUDGET_SECONDS = 900
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


def intake_candidate(connection: DBClient, config: MediaforceConfig) -> dict[str, Any] | None:
    if background_work_is_paused(connection):
        return None
    recent = (datetime.now(UTC) - timedelta(seconds=INTAKE_RETRY_SECONDS)).isoformat(timespec="seconds")
    complete = [
        exists().where(
            source_measurements.c.version_id == library_items.c.active_source_version_id,
            source_measurements.c.kind == kind,
            source_measurements.c.binding == "observed",
        )
        for kind in EVIDENCE_KINDS
    ]
    assessed = exists().where(
        source_admissions.c.version_id == library_items.c.active_source_version_id,
        ((source_admissions.c.tier == "probable") & complete[0] & complete[1]) | (source_admissions.c.recorded_at > recent),
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
    return next((dict(row) for row in rows if not _history_present(connection, int(row["id"]))), None)


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


def process_source_intake_once(
    config: MediaforceConfig,
    *,
    analyze: Callable[..., dict[str, object]],
    target: Callable[[dict[str, Any], ManagedProcessController], Any],
) -> bool:
    with open_db(config.paths.db_path) as connection:
        item = intake_candidate(connection, config)
    if item is None:
        return False
    controller = ManagedProcessController()
    path = Path(item["source_path"])
    try:
        analysis_target = target(item, controller)
        if analysis_target is None:
            raise RuntimeError("No encode computer can measure this source yet")
        deadline = time.monotonic() + INTAKE_HASH_BUDGET_SECONDS
        last_pause_check = 0.0

        def check_active() -> None:
            nonlocal last_pause_check
            controller.throw_if_cancelled()
            now = time.monotonic()
            if now > deadline:
                raise RuntimeError("Intake I/O budget is exhausted")
            if now - last_pause_check < 1:
                return
            last_pause_check = now
            with open_db(config.paths.db_path) as database:
                if background_work_is_paused(database):
                    raise ProcessCancelledError("Background intake was paused")

        before = observe_file(path, full_digest=True, check_cancelled=check_active)
        probe = _probe_payload(path, process_controller=controller)
        after = observe_file(path)
        if any(before[key] != after[key] for key in ("stat", "size_bytes", "sample_fingerprint")):
            raise RuntimeError("Source changed during intake")
        with open_db(config.paths.db_path) as connection:
            checks = assess_readable_source(connection, before, probe)
            version = commit_readable_source(connection, item, before, probe)
        if checks.get("reason"):
            return True
        runner = analysis_target.command_runner
        kwargs = {"command_runner": runner} if runner is not None else {}
        for kind in EVIDENCE_KINDS:
            try:
                check_active()
                local_before = observe_file(path)
                remote_before = runner.observe_source(analysis_target.source_path, process_controller=controller) if runner else None
                if remote_before and any(remote_before.get(key) != before[key] for key in ("size_bytes", "sample_fingerprint")):
                    raise RuntimeError("Encode computer sees different source bytes")
                summary = analyze(analysis_target.source_path, kind, process_controller=controller, **kwargs)
                local_after = observe_file(path)
                remote_after = runner.observe_source(analysis_target.source_path, process_controller=controller) if runner else None
                if local_before != local_after or local_after != after or remote_before != remote_after:
                    raise RuntimeError("Source changed during intake measurement")
                with open_db(config.paths.db_path) as connection:
                    current = connection.execute(select(library_items).where(library_items.c.id == item["id"])).mappings().one()
                    if current["active_source_version_id"] != version or pending_source_operation(connection, int(item["id"])):
                        raise RuntimeError("Source changed before intake measurement commit")
                    append_measurement(
                        connection,
                        version_id=version,
                        kind=kind,
                        binding="observed",
                        summary=summary,
                        provenance={
                            "basis": "readable-source",
                            "before": local_before,
                            "after": local_after,
                            "remote_before": remote_before,
                            "remote_after": remote_after,
                            "codec_rule_hash": checks["codec_rule_hash"],
                        },
                        measured_at=timestamp(),
                        recorded_at=timestamp(),
                    )
            except ProcessCancelledError:
                raise
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                LOGGER.exception("Intake measurement failed for item %s (%s)", item["id"], kind)
    except ProcessCancelledError:
        return False
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        LOGGER.exception("Source intake deferred for item %s", item["id"])
        with open_db(config.paths.db_path) as database:
            current = database.execute(select(library_items).where(library_items.c.id == item["id"])).mappings().one()
            if current["source_revision"] == item["source_revision"] and not pending_source_operation(database, int(item["id"])):
                active = ensure_active_source_version(database, current)
                generation = database.execute(
                    select(source_versions.c.generation_id).where(source_versions.c.version_id == active)
                ).scalar_one()
                prior = database.execute(
                    select(source_admissions.c.admission_id)
                    .where(source_admissions.c.version_id == active)
                    .order_by(source_admissions.c.recorded_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                database.execute(
                    source_admissions.insert().values(
                        admission_id=uuid4().hex,
                        generation_id=generation,
                        version_id=active,
                        tier="unknown",
                        basis="readable-source",
                        supersedes_id=prior,
                        checks_json=json_text({"reason": "intake_retry_pending"}),
                        recorded_at=timestamp(),
                    )
                )
    finally:
        controller.reset()
    return True
