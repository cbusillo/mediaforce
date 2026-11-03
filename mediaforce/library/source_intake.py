import json
import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import exists, func, or_, select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, DBRow, open_db
from mediaforce.core.db_tables import (
    item_events,
    library_item_evidence_state,
    library_items,
    source_admissions,
    source_generations,
    source_measurements,
    source_transitions,
    source_versions,
    staged_artifacts,
)
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.process_control import ManagedProcessController
from mediaforce.core.utils import timestamp
from mediaforce.library.background_work import background_work_is_paused
from mediaforce.library.evidence_state import EVIDENCE_KINDS, sync_library_item_evidence_state
from mediaforce.library.item_presence import LIBRARY_ITEM_PRESENT
from mediaforce.library.media_scopes import MediaScopeConflict
from mediaforce.library.probe import probe_evidence_source
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
    observed_measurement_matches,
    pending_source_operation,
)


LOGGER = logging.getLogger(__name__)


def encoder_rule_hash() -> str:
    return stable_json_hash(
        {"profiles": HISTORICAL_OUTPUT_PROFILES["encoders"], "normalization": "alphanumeric-without-lib-prefix-v1"}
    )


def matches_historical_encoder(value: str) -> bool:
    normalized = "".join(character for character in value.casefold() if character.isalnum())
    return any(
        "".join(character for character in encoder.removeprefix("lib") if character.isalnum()) in normalized
        for encoder in HISTORICAL_OUTPUT_PROFILES["encoders"]
    )


def _history_conditions(item_id: Any) -> list[Any]:
    return [
        (staged_artifacts.c.library_item_id == item_id) & staged_artifacts.c.encode_completed_at.is_not(None),
        source_transitions.c.library_item_id == item_id,
        (item_events.c.library_item_id == item_id)
        & item_events.c.event_type.in_(
            ("encoding_completed", "promotion_completed", "published_copy_replaced", "originals_removed_confirmed")
        ),
    ]


def _history_present(connection: DBClient, item_id: int) -> bool:
    return any(connection.execute(select(exists().where(condition))).scalar() for condition in _history_conditions(item_id))


def assess_readable_source(
    connection: DBClient, identity: Mapping[str, Any], probe: Mapping[str, Any], *, context: SourceEvidenceContext | None = None,
    load_context: Callable[[], SourceEvidenceContext] | None = None,
) -> dict[str, Any]:
    streams = probe.get("streams")
    format_info = probe.get("format")
    checks: dict[str, Any] = {
        "basis": "readable-source",
        "codec_rule_hash": stable_json_hash(HISTORICAL_OUTPUT_PROFILES),
        "encoder_rule_hash": encoder_rule_hash(),
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
    if any(matches_historical_encoder(value) for key, value in tags if "encoder" in key):
        return {**checks, "reason": "historical_output_encoder"}
    if not identity.get("sha256"):
        return {**checks, "reason": "digest_unavailable"}
    context = context or (load_context() if load_context is not None else load_source_evidence_context(connection))
    if context.rule_conflicts(codec):
        return {**checks, "reason": "historical_output_conflict"}
    output_check = context.output_check({**identity, "video_codec": codec})
    return {
        **checks,
        "output_check": output_check,
        "output_index_hash": context.output_index_hash,
        "reason": None if output_check == "clear" else f"known_output_{output_check}",
    }


def _matches_scanned_identity(item: Mapping[str, Any] | DBRow, identity: Mapping[str, Any]) -> bool:
    return (item["size_bytes"] == identity["size_bytes"]
            and item["content_version_fingerprint"] == identity["sample_fingerprint"]
            and item["mtime_ns"] == identity["stat"][3])


def commit_readable_source(
    connection: DBClient, item: Mapping[str, Any], identity: Mapping[str, Any], probe: Mapping[str, Any],
    *, before_commit: Callable[[DBClient], None] | None = None,
) -> str:
    connection.commit()
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    if before_commit is not None:
        before_commit(connection)
    current = connection.execute(select(library_items).where(library_items.c.id == item["id"])).mappings().one()
    if (
        current["source_revision"] != item["source_revision"]
        or pending_source_operation(connection, int(item["id"]))
        or not _matches_scanned_identity(current, identity)
        or _history_present(connection, int(item["id"]))
    ):
        raise RuntimeError("The file changed since its library scan; scan it again before checking the original")
    active = ensure_active_source_version(connection, current)
    previous = connection.execute(select(source_versions).where(source_versions.c.version_id == active)).mappings().one()
    if previous["role"] == "output":
        raise RuntimeError("A known Mediaforce output cannot be an original")
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
    checks = assess_readable_source(connection, identity, probe)
    same_bytes = previous["sha256"] == identity.get("sha256")
    if (
        prior
        and prior["version_id"] == active
        and same_bytes
        and json.loads(previous["identity_json"]) == identity
        and {key: value for key, value in json.loads(prior["checks_json"]).items() if key != "measurement_ids"} == checks
    ):
        return active
    generation = previous["generation_id"]
    if previous["sha256"] and not same_bytes:
        generation = uuid4().hex
        connection.execute(source_generations.insert().values(generation_id=generation, created_at=timestamp()))
        prior = None
    version = (
        active
        if previous["role"] == "input" and same_bytes and json.loads(previous["identity_json"]) == identity
        else append_version(
            connection,
            generation_id=generation,
            role="input",
            identity=identity,
            profile={**json.loads(previous["profile_json"]), "video_codec": checks.get("video_codec")},
            recorded_at=timestamp(),
        )
    )
    receipts = (
        connection.execute(
            select(source_measurements)
            .join(source_versions)
            .where(
                source_versions.c.generation_id == generation,
                source_versions.c.role != "output",
                or_(source_versions.c.sha256.is_(None), source_versions.c.sha256 == identity.get("sha256")),
            )
        )
        .mappings()
        .all()
    )
    frozen = {
        row["measurement_id"]
        for row in receipts
        if (not previous["sha256"] or same_bytes) and observed_measurement_matches(row, identity, require_stat=True)
    }
    if prior and same_bytes:
        frozen.update(json.loads(prior["checks_json"]).get("measurement_ids", []))
        frozen.update(row["measurement_id"] for row in receipts if observed_measurement_matches(row, identity))
    checks["measurement_ids"] = sorted(frozen)
    connection.execute(
        update(library_items)
        .where(library_items.c.id == item["id"])
        .values(active_source_version_id=version, source_revision=library_items.c.source_revision + 1)
    )
    connection.execute(
        source_admissions.insert().values(
            admission_id=uuid4().hex,
            generation_id=generation,
            version_id=version,
            tier="unknown" if checks.get("reason") else "probable",
            basis="readable-source",
            checks_json=json_text(checks),
            supersedes_id=prior["admission_id"] if prior else None,
            recorded_at=timestamp(),
        )
    )
    return version


def assess_source_input(
    config: MediaforceConfig, item_id: int, controller: ManagedProcessController, check_cancelled: Callable[[], None],
    *, before_commit: Callable[[DBClient], None] | None = None,
    digest_guard: Callable[[], AbstractContextManager[None]] = nullcontext,
) -> None:
    with open_db(config.paths.db_path) as connection:
        item = cast(
            dict[str, Any], dict(connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one())
        )
        if _history_present(connection, item_id):
            return
        active = (
            connection.execute(select(source_versions).where(source_versions.c.version_id == item["active_source_version_id"]))
            .mappings()
            .one_or_none()
        )
        admission = (
            connection.execute(
                select(source_admissions).where(
                    source_admissions.c.version_id == item["active_source_version_id"],
                    source_admissions.c.admission_id.not_in(
                        select(source_admissions.c.supersedes_id).where(source_admissions.c.supersedes_id.is_not(None))
                    ),
                )
            )
            .mappings()
            .one_or_none()
        )
    path = Path(item["source_path"])
    root = config.source_root_map.get(item["media_root"])
    if root is None or not root.is_dir():
        raise RuntimeError("The original's library is unavailable")
    before = observe_file(path)
    if not _matches_scanned_identity(item, before):
        raise RuntimeError("The file changed since its library scan; scan it again before checking the original")
    saved = json.loads(active["identity_json"]) if active else {}
    if (
        admission
        and all(saved.get(key) == before[key] for key in ("path", "stat", "size_bytes", "sample_fingerprint"))
        and json.loads(admission["checks_json"]).get("codec_rule_hash") == stable_json_hash(HISTORICAL_OUTPUT_PROFILES)
        and json.loads(admission["checks_json"]).get("encoder_rule_hash") == encoder_rule_hash()
    ):
        return
    if saved.get("sha256") and all(saved.get(key) == before[key] for key in ("path", "stat", "size_bytes", "sample_fingerprint")):
        before["sha256"] = saved["sha256"]
    else:
        with digest_guard():
            before = observe_file(path, full_digest=True, check_cancelled=check_cancelled)
    if not saved.get("sha256") or saved.get("stat") != before["stat"]:
        with open_db(config.paths.db_path) as connection:
            connection.commit()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            if before_commit is not None:
                before_commit(connection)
            current = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()
            if (current["source_revision"] != item["source_revision"] or pending_source_operation(connection, item_id)
                    or not _matches_scanned_identity(current, before)):
                raise RuntimeError("The file changed before its content check could be recorded")
            active_id = ensure_active_source_version(connection, current)
            version = connection.execute(select(source_versions).where(source_versions.c.version_id == active_id)).mappings().one()
            if version["role"] == "output":
                raise RuntimeError("The current file is a known Mediaforce output")
            generation = version["generation_id"]
            if version["sha256"] and version["sha256"] != before["sha256"]:
                generation = uuid4().hex
                connection.execute(source_generations.insert().values(generation_id=generation, created_at=timestamp()))
            observed = append_version(
                connection,
                generation_id=generation,
                role="unknown",
                identity=before,
                profile=json.loads(version["profile_json"]),
                recorded_at=timestamp(),
            )
            connection.execute(
                update(library_items)
                .where(library_items.c.id == item_id)
                .values(active_source_version_id=observed, source_revision=library_items.c.source_revision + 1)
            )
            item = cast(
                dict[str, Any], dict(connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one())
            )
    probe = probe_evidence_source(path, process_controller=controller)
    after = observe_file(path)
    if any(before[key] != after[key] for key in ("stat", "size_bytes", "sample_fingerprint")):
        raise RuntimeError("The file changed while checking its original; the check will retry")
    check_cancelled()
    with open_db(config.paths.db_path) as connection:
        commit_readable_source(connection, item, before, probe, before_commit=before_commit)


def queue_original_intake(connection: DBClient, config: MediaforceConfig, *, limit: int = 25) -> int:
    from mediaforce.library.evidence_queue import (
        EVIDENCE_WORK_PRIORITY_REPRESENTATIVE,
        EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
        EVIDENCE_QUEUE_ACTIVE_STATUSES,
        EVIDENCE_WORK_ACTIVE_STATUSES,
        load_evidence_queue_state,
        queue_decision_evidence_work,
    )

    state = load_evidence_queue_state(connection)
    if background_work_is_paused(connection) or (
        state["status"] in EVIDENCE_QUEUE_ACTIVE_STATUSES and (state["is_paused"] or state["cancel_requested"])
    ):
        return 0
    assessed = exists().where(
        source_admissions.c.version_id == library_items.c.active_source_version_id,
        func.json_extract(source_admissions.c.checks_json, "$.codec_rule_hash") == stable_json_hash(HISTORICAL_OUTPUT_PROFILES),
        func.json_extract(source_admissions.c.checks_json, "$.encoder_rule_hash") == encoder_rule_hash(),
    )
    due_terminal = exists().where(
        library_item_evidence_state.c.library_item_id == library_items.c.id,
        library_item_evidence_state.c.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
        library_item_evidence_state.c.work_status == "cancelled",
        library_item_evidence_state.c.retry_not_before <= timestamp(),
    )
    already_queued = exists().where(
        library_item_evidence_state.c.library_item_id == library_items.c.id,
        library_item_evidence_state.c.evidence_kind == "media_fingerprint",
        library_item_evidence_state.c.work_source_fingerprint
        == func.coalesce(library_items.c.content_version_fingerprint, library_items.c.fingerprint),
        or_(
            library_item_evidence_state.c.work_status.in_(EVIDENCE_WORK_ACTIVE_STATUSES),
            library_item_evidence_state.c.work_status == "failed",
            (library_item_evidence_state.c.work_status == "cancelled")
            & (library_item_evidence_state.c.retry_not_before > timestamp()),
        ),
    )
    rows = (
        connection.execute(
            select(library_items)
            .where(
                LIBRARY_ITEM_PRESENT,
                library_items.c.media_root.in_(tuple(config.source_root_map)),
                library_items.c.video_codec.in_(HISTORICAL_OUTPUT_PROFILES["known_input_codecs"]),
                or_((~assessed) & (~already_queued), due_terminal),
                ~or_(*(exists().where(condition) for condition in _history_conditions(library_items.c.id))),
            )
            .order_by(library_items.c.id)
            .limit(limit)
        )
        .mappings()
        .all()
    )
    prepared = 0
    context = load_source_evidence_context(connection)
    for raw_item in rows:
        item = cast(dict[str, Any], dict(raw_item))
        try:
            with connection.begin_nested():
                evidence = load_source_evidence(connection, int(item["id"]), context=context)
                due_kinds = list(
                    connection.execute(
                        select(library_item_evidence_state.c.evidence_kind).where(
                            library_item_evidence_state.c.library_item_id == item["id"],
                            library_item_evidence_state.c.work_reason == EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
                            library_item_evidence_state.c.work_status == "cancelled",
                            library_item_evidence_state.c.retry_not_before <= timestamp(),
                        )
                    ).scalars()
                )
                if (
                    due_kinds
                    and evidence.availability != "available"
                    and connection.execute(
                        select(source_admissions.c.admission_id).where(
                            source_admissions.c.version_id == item["active_source_version_id"]
                        )
                    ).first()
                ):
                    connection.execute(
                        update(library_item_evidence_state)
                        .where(
                            library_item_evidence_state.c.library_item_id == item["id"],
                            library_item_evidence_state.c.evidence_kind.in_(due_kinds),
                        )
                        .values(
                            work_status="failed",
                            retry_not_before=None,
                            last_error="An eligible original could not be confirmed; refresh the library before preparing another check",
                        )
                    )
                    continue
                kinds = due_kinds or (
                    [kind for kind in EVIDENCE_KINDS if evidence.traits[kind].tier == "unknown"]
                    if evidence.availability == "available"
                    else ["media_fingerprint"]
                )
                for kind in kinds:
                    existing = (
                        connection.execute(
                            select(library_item_evidence_state).where(
                                library_item_evidence_state.c.library_item_id == item["id"],
                                library_item_evidence_state.c.evidence_kind == kind,
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if existing and (
                        existing["work_status"] == "failed"
                        or (
                            existing["work_status"] == "cancelled"
                            and existing["retry_not_before"]
                            and existing["retry_not_before"] > timestamp()
                        )
                    ):
                        continue
                    sync_library_item_evidence_state(connection, item, kind)
                    queued = queue_decision_evidence_work(
                        connection,
                        config,
                        item["rel_path"],
                        library_item_ids=[item["id"]],
                        evidence_kind=kind,
                        work_reason=EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
                        require_measurement=True,
                        priority=EVIDENCE_WORK_PRIORITY_REPRESENTATIVE,
                        start_new_batch=True,
                        manage_transaction=False,
                        restart_terminal=True,
                    )
                    prepared += queued["prepared_count"]
        except MediaScopeConflict:
            LOGGER.exception("Original discovery could not resolve item %s", item["id"])
            sync_library_item_evidence_state(connection, item, "media_fingerprint")
            connection.execute(
                update(library_item_evidence_state)
                .where(
                    library_item_evidence_state.c.library_item_id == item["id"],
                    library_item_evidence_state.c.evidence_kind == "media_fingerprint",
                )
                .values(
                    work_status="failed",
                    work_reason=EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
                    work_source_fingerprint=item["content_version_fingerprint"] or item["fingerprint"],
                    retry_not_before=None,
                    last_error="The library has conflicting paths for this file; refresh the library before retrying",
                    updated_at=timestamp(),
                )
            )
    return prepared
