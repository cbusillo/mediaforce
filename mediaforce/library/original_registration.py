import json
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, DBRow, open_db
from mediaforce.core.db_tables import item_events, library_item_evidence_state, library_items, source_admissions, source_generations, source_versions
from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.process_control import ManagedProcessController
from mediaforce.core.type_defs import float_value
from mediaforce.core.utils import file_stat_signature, timestamp
from mediaforce.library import probe, source_evidence
from mediaforce.library.source_evidence import append_version, ensure_active_source_version, json_text, load_source_evidence_bulk, load_source_evidence_context, pending_source_operation
from mediaforce.library.source_intake import assess_readable_source, frozen_observed_measurements, _matches_scanned_identity
from mediaforce.ops.runtime_install import runtime_directory


def confirmation_directory() -> Path:
    return runtime_directory(Path.home()).parent / "mac-app" / "original-approvals"


def _identifier(value: str) -> str:
    if UUID(value).hex != value:
        raise ValueError("Invalid original approval identifier")
    return value


def _preview_path(config: MediaforceConfig, preview_id: str) -> Path:
    return config.paths.web_state_dir / "original-previews" / f"{_identifier(preview_id)}.json"


def _checks(connection: DBClient, identity: Mapping[str, Any], override: str | None, controller: ManagedProcessController | None = None) -> dict[str, Any]:
    checks = assess_readable_source(connection, identity, probe.probe_evidence_source(Path(identity["path"]), process_controller=controller), codec_override=override)
    if checks.get("reason"):
        reason = checks["reason"]
        if reason in {"mediaforce_stamp", "known_output_match"}:
            raise ValueError("Mediaforce has already compressed this file")
        if reason in {"historical_output_codec_or_unknown", "historical_output_encoder"}:
            raise ValueError("This file's format needs a separate original-file exception or more file information")
        raise ValueError("Mediaforce cannot yet rule out an earlier compressed copy of this file")
    if override and not checks.get("exception_matches"):
        raise ValueError("This file has no codec or encoder match needing an exception")
    return checks


def _reuse_generation(active: Mapping[str, Any] | DBRow, identity: Mapping[str, Any]) -> bool:
    return active["role"] != "output" and active["sha256"] in (None, identity["sha256"])


def _reuse_version(active: Mapping[str, Any] | DBRow, identity: Mapping[str, Any]) -> bool:
    return active["role"] == "input" and json.loads(active["identity_json"]) == identity


def _current_admissions(connection: DBClient, generation: str) -> list[DBRow]:
    return list(connection.execute(select(source_admissions).where(
        source_admissions.c.generation_id == generation,
        source_admissions.c.admission_id.not_in(select(source_admissions.c.supersedes_id).where(source_admissions.c.supersedes_id.is_not(None))),
    )).mappings())


def _preview_item(connection: DBClient, item_id: int, override: str | None) -> dict[str, Any]:
    item = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one_or_none()
    if item is None:
        raise ValueError("The named library item is unavailable")
    if not item["active_source_version_id"]:
        raise ValueError("This file changed while preparing its list. Prepare it again.")
    if item["missing_since"] or pending_source_operation(connection, item_id):
        raise ValueError("This file is missing or has an unfinished change")
    identity = source_evidence.observe_file(Path(item["source_path"]).expanduser().resolve(), full_digest=True)
    checks = _checks(connection, identity, override)
    active = connection.execute(select(source_versions).where(
        source_versions.c.version_id == item["active_source_version_id"],
    )).mappings().one_or_none()
    if active is None:
        raise ValueError("This file changed while preparing its list. Prepare it again.")
    if not _matches_scanned_identity(item, identity):
        raise ValueError("Scan this file again before preparing its approval")
    generation = active["generation_id"] if _reuse_generation(active, identity) else uuid4().hex
    prior = _current_admissions(connection, generation)
    if len(prior) > 1:
        raise ValueError("This original has conflicting approvals")
    measurements = frozen_observed_measurements(connection, active, identity, prior[0] if prior else None) if active else []
    return {"item_id": item_id, "rel_path": item["rel_path"], "source_revision": item["source_revision"],
            "active_version_id": item["active_source_version_id"], "source_path": item["source_path"],
            "generation_id": generation,
            "version_id": active["version_id"] if _reuse_version(active, identity) else uuid4().hex,
            "identity": identity, "checks": checks, "measurement_ids": measurements, "supersedes_id": prior[0]["admission_id"] if prior else None}


def build_original_preview(config: MediaforceConfig, item_ids: Sequence[int], *, assertion: str,
                           overrides: Mapping[int, str] | None = None) -> dict[str, Any]:
    if (not assertion.strip() or not item_ids or len(set(item_ids)) != len(item_ids)
            or any(type(item) is not int or item <= 0 for item in item_ids)):
        raise ValueError("Name each file once and give its independent acquisition reason")
    overrides = overrides or {}
    if set(overrides) - set(item_ids) or any(not value.strip() for value in overrides.values()):
        raise ValueError("Every exception must name one of these files and its reason")
    with open_db(config.paths.db_path) as connection:
        for item_id in item_ids:
            item = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one_or_none()
            if item is None or item["missing_since"]:
                raise ValueError("The named library item is unavailable")
            ensure_active_source_version(connection, item)
        connection.commit()
        items = [_preview_item(connection, item, overrides.get(item)) for item in item_ids]
    preview_id = uuid4().hex
    body = {"preview_id": preview_id, "challenge": uuid4().hex, "owner_uid": os.getuid(),
            "assertion": assertion.strip(), "items": items}
    envelope = {"preview_id": preview_id, "challenge": body["challenge"], "manifest_hash": stable_json_hash(body),
                "canonical_json": json_text(body)}
    path = _preview_path(config, preview_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical_json_bytes(envelope))
    return envelope


def _read_preview(config: MediaforceConfig, preview_id: str) -> dict[str, Any]:
    try:
        preview = json.loads(_preview_path(config, preview_id).read_text())
        body = json.loads(preview["canonical_json"])
        items = body["items"]
        fields = {"item_id", "rel_path", "source_revision", "active_version_id", "source_path", "generation_id", "version_id", "identity", "checks", "measurement_ids", "supersedes_id"}
        if (not isinstance(items, list) or not items or not isinstance(preview["challenge"], str) or not preview["challenge"]
                or any(not isinstance(item, dict) or not fields.issubset(item)
                       or type(item["item_id"]) is not int or item["item_id"] <= 0
                       or not item["active_version_id"] or not isinstance(item["identity"], dict)
                       or not isinstance(item["checks"], dict) or not isinstance(item["measurement_ids"], list) for item in items)
                or len({item["item_id"] for item in items}) != len(items)):
            raise ValueError("The original-file list is incomplete. Prepare it again.")
        if (body["preview_id"] != preview_id or preview["preview_id"] != preview_id
                or stable_json_hash(body) != preview["manifest_hash"] or json_text(body) != preview["canonical_json"]
                or body["challenge"] != preview["challenge"] or body["owner_uid"] != os.getuid()):
            raise ValueError("The original approval preview changed")
        return {key: preview[key] for key in ("preview_id", "challenge", "manifest_hash", "canonical_json")}
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("The original approval preview is unavailable") from error


def _confirmation(preview_id: str) -> dict[str, Any]:
    try:
        receipt = json.loads((confirmation_directory() / f"{_identifier(preview_id)}.json").read_text())
        if (receipt["preview_id"] != preview_id or receipt["owner_uid"] != os.getuid()
                or receipt["authentication"] != "deviceOwnerAuthentication" or not receipt["confirmed_at"]):
            raise ValueError("Confirm this exact file list in the Mac app")
        body = json.loads(receipt["canonical_json"])
        if (stable_json_hash(body) != receipt["manifest_hash"] or json_text(body) != receipt["canonical_json"]
                or body["preview_id"] != preview_id or body["challenge"] != receipt["challenge"]
                or body["owner_uid"] != receipt["owner_uid"]):
            raise ValueError("The Mac approval does not match its file list")
        return receipt
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Confirm this exact file list in the Mac app") from error


def owner_confirmation_matches(admission: Mapping[str, Any] | DBRow, original: Mapping[str, Any] | DBRow | None = None,
                               *, confirmations: dict[str, Any] | None = None) -> bool:
    confirmations = confirmations if confirmations is not None else {}
    reference = str(admission["confirmation_ref"])
    if not reference.startswith("mac-app:"):
        return False
    if reference not in confirmations:
        try:
            receipt = _confirmation(reference.removeprefix("mac-app:"))
            confirmations[reference] = {"hash": receipt["manifest_hash"], "items": {
                (item["version_id"], item["generation_id"]): item for item in json.loads(receipt["canonical_json"])["items"]}}
        except (ValueError, KeyError, TypeError):
            confirmations[reference] = None
    saved = confirmations[reference]
    if saved is None:
        return False
    try:
        item = saved["items"][(admission["version_id"], admission["generation_id"])]
        checks = json.loads(admission["checks_json"])
        return (item["measurement_ids"] == checks.get("measurement_ids", []) and item["checks"] == checks["source_checks"]
                and saved["hash"] == checks["preview_hash"]
                and (original is None or all(original[key] == item["identity"][key] for key in ("sha256", "size_bytes"))))
    except (ValueError, KeyError, TypeError):
        return False


def _stored_previews(config: MediaforceConfig) -> list[dict[str, Any]]:
    previews = []
    for path in sorted((config.paths.web_state_dir / "original-previews").glob("*.json")):
        try:
            previews.append(_read_preview(config, path.stem))
        except ValueError:
            continue
    return previews


def _member_failure(connection: DBClient, preview_id: str, item: Mapping[str, Any]) -> str | None:
    return connection.execute(select(func.json_extract(item_events.c.details_json, "$.reason")).where(
        item_events.c.library_item_id == item["item_id"], item_events.c.event_type == "original_confirmation_failed",
        func.json_extract(item_events.c.details_json, "$.preview_id") == preview_id,
        func.json_extract(item_events.c.details_json, "$.version_id") == item["version_id"],
        func.json_extract(item_events.c.details_json, "$.source_revision") == item["source_revision"],
    ).order_by(item_events.c.id.desc())).scalars().first()


def _record_member_failure(connection: DBClient, preview: Mapping[str, Any], item: Mapping[str, Any], reason: str) -> None:
    if _member_failure(connection, preview["preview_id"], item) or not connection.execute(
            select(library_items.c.id).where(library_items.c.id == item["item_id"])).first():
        return
    connection.execute(item_events.insert().values(
        library_item_id=item["item_id"], created_at=timestamp(), event_type="original_confirmation_failed",
        details_json=json_text({"preview_id": preview["preview_id"], "version_id": item["version_id"],
            "source_revision": item["source_revision"], "reason": reason}),
    ))


def list_original_previews(config: MediaforceConfig, *, unconfirmed_only: bool = False) -> list[dict[str, Any]]:
    pending = []
    previews = _stored_previews(config)
    with open_db(config.paths.db_path) as connection:
        evidence = load_source_evidence_bulk(connection, [item["item_id"] for preview in previews
                                                        for item in json.loads(preview["canonical_json"])["items"]])
        for preview in previews:
            items = json.loads(preview["canonical_json"])["items"]
            owners = dict(connection.execute(select(library_items.c.id, source_admissions.c.confirmation_ref).join(
                source_versions, source_versions.c.version_id == library_items.c.active_source_version_id,
            ).join(source_admissions, source_admissions.c.generation_id == source_versions.c.generation_id).where(
                library_items.c.id.in_([item["item_id"] for item in items]), source_admissions.c.basis == "owner",
                source_admissions.c.admission_id.not_in(select(source_admissions.c.supersedes_id).where(source_admissions.c.supersedes_id.is_not(None))),
            )).all())
            problems = []
            for item in items:
                current = evidence[item["item_id"]]
                owner = owners.get(item["item_id"])
                if owner and owner != f"mac-app:{preview['preview_id']}" and current.availability == "available":
                    continue
                reason = _member_failure(connection, preview["preview_id"], item)
                if (not reason and owner == f"mac-app:{preview['preview_id']}"
                        and current.original_version_id == item["version_id"] and current.availability != "available"):
                    reason = "The saved Mac approval or its original-file checks are unavailable. Restore the saved approval or prepare an updated file list."
                if reason:
                    problems.append(f"{item['rel_path']}: {reason}")
            if problems:
                preview["problem"] = "This file list needs preparing again. " + " ".join(problems)
            elif all(_member_applied(connection, preview, item) for item in items):
                continue
            else:
                try:
                    _check_preview_catalog(connection, items)
                except ValueError:
                    continue
                if unconfirmed_only:
                    try:
                        _confirmed_preview(config, preview["preview_id"], preview["manifest_hash"])
                        continue
                    except ValueError:
                        pass
            try:
                if (confirmation_directory() / f"{preview['preview_id']}.dismissed").read_text() == preview["manifest_hash"]:
                    continue
            except OSError:
                pass
            pending.append(preview)
    return pending


def _check_preview_catalog(connection: DBClient, items: Sequence[Mapping[str, Any]]) -> None:
    for item in items:
        current = connection.execute(select(library_items).where(library_items.c.id == item["item_id"])).mappings().one_or_none()
        if (current is None or current["missing_since"] or current["source_revision"] != item["source_revision"]
                or current["active_source_version_id"] != item["active_version_id"] or current["source_path"] != item["source_path"]
                or current["rel_path"] != item["rel_path"] or not _matches_scanned_identity(current, item["identity"])
                or pending_source_operation(connection, item["item_id"])):
            raise ValueError("This file changed. Prepare an updated file list.")


def request_original_apply(config: MediaforceConfig, preview_id: str, manifest_hash: str) -> dict[str, Any]:
    from mediaforce.library.evidence_queue import (EVIDENCE_WORK_ACTIVE_STATUSES, EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
        queue_decision_evidence_work)
    from mediaforce.library.evidence_state import sync_library_item_evidence_state

    preview, body = _confirmed_preview(config, preview_id, manifest_hash)
    pending = False
    with open_db(config.paths.db_path) as connection:
        for item in body["items"]:
            connection.commit()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            if _member_applied(connection, preview, item) or _member_failure(connection, preview_id, item):
                continue
            try:
                _check_preview_catalog(connection, [item])
            except ValueError as error:
                _record_member_failure(connection, preview, item, str(error))
                continue
            current = cast(dict[str, Any], dict(connection.execute(select(library_items).where(library_items.c.id == item["item_id"])).mappings().one()))
            sync_library_item_evidence_state(connection, current, "media_fingerprint")
            work = connection.execute(select(library_item_evidence_state).where(
                library_item_evidence_state.c.library_item_id == item["item_id"],
                library_item_evidence_state.c.evidence_kind == "media_fingerprint",
            )).mappings().one_or_none()
            pending = True
            if not work or work["work_status"] not in EVIDENCE_WORK_ACTIVE_STATUSES:
                queue_decision_evidence_work(connection, config, item["rel_path"], library_item_ids=[item["item_id"]],
                    evidence_kind="media_fingerprint", work_reason=EVIDENCE_WORK_REASON_ORIGINAL_SOURCE,
                    require_measurement=True, start_new_batch=True, restart_terminal=True, manage_transaction=False)
    return {"ok": True, "pending": pending}


def queue_confirmed_originals(config: MediaforceConfig) -> None:
    from mediaforce.library.evidence_queue import EvidenceQueueConflict

    for preview in _stored_previews(config):
        try:
            request_original_apply(config, preview["preview_id"], preview["manifest_hash"])
        except (ValueError, EvidenceQueueConflict):
            continue


def fail_confirmed_originals(connection: DBClient, config: MediaforceConfig, item_id: int, reason: str) -> None:
    for preview in _stored_previews(config):
        for item in json.loads(preview["canonical_json"])["items"]:
            if item["item_id"] != item_id:
                continue
            if _member_applied(connection, preview, item):
                continue
            try:
                _confirmed_preview(config, preview["preview_id"], preview["manifest_hash"])
            except ValueError:
                continue
            _record_member_failure(connection, preview, item, reason)


def apply_confirmed_originals(config: MediaforceConfig, item_id: int, controller: ManagedProcessController,
                             check_cancelled: Callable[[], None], digest_guard: Callable[[], AbstractContextManager[None]]) -> None:
    for preview in _stored_previews(config):
        items = [item for item in json.loads(preview["canonical_json"])["items"] if item["item_id"] == item_id]
        if not items:
            continue
        try:
            _confirmed_preview(config, preview["preview_id"], preview["manifest_hash"])
        except ValueError:
            continue
        with open_db(config.paths.db_path) as connection:
            if _member_applied(connection, preview, items[0]) or _member_failure(connection, preview["preview_id"], items[0]):
                continue
        try:
            with digest_guard():
                apply_original_preview(config, preview["preview_id"], preview["manifest_hash"], item_id=item_id,
                                       controller=controller, check_cancelled=check_cancelled)
        except ValueError as error:
            with open_db(config.paths.db_path) as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                _record_member_failure(connection, preview, items[0], str(error))


def _apply_item(connection: DBClient, item: Mapping[str, Any], preview: Mapping[str, Any], now: str,
                checked_probe: Mapping[str, Any]) -> str:
    generation = item["generation_id"]
    if connection.execute(select(source_generations).where(source_generations.c.generation_id == generation)).first() is None:
        connection.execute(source_generations.insert().values(generation_id=generation, created_at=now))
    video = next(stream for stream in checked_probe["streams"] if stream.get("codec_type") == "video")
    profile = {"video_codec": item["checks"]["video_codec"], "width": video.get("width"), "height": video.get("height"),
               "pix_fmt": video.get("pix_fmt"), "duration_seconds": float_value(checked_probe["format"].get("duration")) or None}
    version = item["version_id"]
    if connection.execute(select(source_versions.c.version_id).where(source_versions.c.version_id == version)).first() is None:
        append_version(connection, generation_id=generation, role="input", identity=item["identity"],
                       profile=profile, recorded_at=now, version_id=version)
    admission = stable_json_hash({"preview": preview["preview_id"], "item": item["item_id"]})
    connection.execute(source_admissions.insert().values(
        admission_id=admission, generation_id=generation, version_id=version, tier="proven", basis="owner",
        checks_json=json_text({"source_checks": item["checks"], "preview_hash": preview["manifest_hash"],
                               "measurement_ids": item["measurement_ids"]}),
        confirmation_ref=f"mac-app:{preview['preview_id']}", supersedes_id=item["supersedes_id"], recorded_at=now,
    ))
    changed = connection.execute(update(library_items).where(
        library_items.c.id == item["item_id"], library_items.c.source_revision == item["source_revision"],
    ).values(active_source_version_id=version, source_revision=library_items.c.source_revision + 1))
    if changed.rowcount != 1:
        raise ValueError("This file changed before its approval could be saved")
    return admission


def _member_applied(connection: DBClient, preview: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    admission = connection.execute(select(source_admissions).where(
        source_admissions.c.admission_id == _admission_id(preview["preview_id"], item["item_id"]),
    )).mappings().one_or_none()
    if admission is None:
        return False
    if (admission["version_id"] != item["version_id"] or admission["generation_id"] != item["generation_id"]
            or admission["confirmation_ref"] != f"mac-app:{preview['preview_id']}"):
        raise ValueError("This approval has a conflicting result")
    return True


def _stable_checks(checks: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in checks.items() if key not in {"output_index_hash", "raw_probe"}}


def _preview_member(body: Mapping[str, Any], item_id: int) -> dict[str, Any]:
    for item in body["items"]:
        if item["item_id"] == item_id:
            return cast(dict[str, Any], item)
    raise ValueError("This file is outside the confirmed list")


def _check_member_ancestry(connection: DBClient, item: Mapping[str, Any]) -> None:
    active = connection.execute(select(source_versions).where(source_versions.c.version_id == item["active_version_id"])).mappings().one_or_none()
    if active is None:
        raise ValueError("This file's history changed. Prepare an updated file list.")
    if _reuse_generation(active, item["identity"]):
        if item["generation_id"] != active["generation_id"]:
            raise ValueError("This file list refers to another original's history")
    elif connection.execute(select(source_generations.c.generation_id).where(source_generations.c.generation_id == item["generation_id"])).first():
        raise ValueError("This new original must have its own history")
    if _reuse_version(active, item["identity"]):
        if item["version_id"] != active["version_id"]:
            raise ValueError("This file list refers to a different original")
    elif connection.execute(select(source_versions.c.version_id).where(source_versions.c.version_id == item["version_id"])).first():
        raise ValueError("This file list reuses a different original's identity")
    prior = _current_admissions(connection, item["generation_id"])
    if len(prior) > 1 or item["supersedes_id"] != (prior[0]["admission_id"] if prior else None):
        raise ValueError("The original-file decision changed. Prepare an updated file list.")
    eligible = frozen_observed_measurements(connection, active, item["identity"], prior[0] if prior else None)
    if not set(item["measurement_ids"]).issubset(eligible):
        raise ValueError("This file list refers to measurements that do not belong to this original")


def apply_original_preview(config: MediaforceConfig, preview_id: str, manifest_hash: str, *, item_id: int,
                           before_commit: Callable[[DBClient], None] | None = None,
                           controller: ManagedProcessController | None = None, check_cancelled: Callable[[], None] = lambda: None) -> dict[str, Any]:
    preview, body = _confirmed_preview(config, preview_id, manifest_hash)
    item = _preview_member(body, item_id)
    result = {"ok": True, "admission_ids": [_admission_id(preview_id, item_id)]}
    with open_db(config.paths.db_path) as connection:
        if _member_applied(connection, preview, item):
            return result
        _check_preview_catalog(connection, [item])
        _check_member_ancestry(connection, item)
        frozen_checks = assess_readable_source(connection, item["identity"], item["checks"].get("raw_probe", {}),
                                               codec_override=item["checks"].get("exception_reason"))
        if frozen_checks.get("reason") or _stable_checks(frozen_checks) != _stable_checks(item["checks"]):
            raise ValueError("The saved original-file checks are incomplete or changed. Prepare an updated file list.")
        observation = source_evidence.observe_file(Path(item["identity"]["path"]), full_digest=True, check_cancelled=check_cancelled)
        checks = _checks(connection, observation, item["checks"].get("exception_reason"), controller)
        if observation != item["identity"] or _stable_checks(checks) != _stable_checks(item["checks"]):
            raise ValueError("This file or its original checks changed. Prepare an updated file list.")
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        check_cancelled()
        if _member_applied(connection, preview, item):
            return result
        if before_commit is not None:
            before_commit(connection)
        _check_preview_catalog(connection, [item])
        _check_member_ancestry(connection, item)
        context = load_source_evidence_context(connection)
        candidate = {**item["identity"], "video_codec": checks["video_codec"]}
        if (str(Path(item["source_path"]).resolve()) != item["identity"]["path"]
                or list(file_stat_signature(Path(item["identity"]["path"]).stat())) != item["identity"]["stat"]
                or context.rule_conflicts(checks["video_codec"]) or context.output_check(candidate) != "clear"):
            raise ValueError("This file or a matching compressed copy changed. Prepare an updated file list.")
        _apply_item(connection, item, preview, timestamp(), checks["raw_probe"])
    return result


def _admission_id(preview_id: str, item_id: int) -> str:
    return stable_json_hash({"preview": preview_id, "item": item_id})


def _confirmed_preview(config: MediaforceConfig, preview_id: str, manifest_hash: str) -> tuple[dict[str, Any], dict[str, Any]]:
    preview = _read_preview(config, preview_id)
    receipt = _confirmation(preview_id)
    if (manifest_hash != preview["manifest_hash"] or any(receipt[key] != preview[key]
            for key in ("preview_id", "manifest_hash", "canonical_json", "challenge"))):
        raise ValueError("The approval does not match this exact preview")
    return preview, json.loads(preview["canonical_json"])
