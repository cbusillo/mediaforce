import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import library_items, source_admissions, source_generations, source_versions
from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import float_value
from mediaforce.core.utils import file_stat_signature, timestamp
from mediaforce.library.probe import probe_evidence_source
from mediaforce.library.source_evidence import append_version, ensure_active_source_version, json_text, load_source_evidence_context, observe_file, pending_source_operation
from mediaforce.library.source_intake import assess_readable_source
from mediaforce.ops.runtime_install import runtime_directory
from mediaforce.ops.login_item import _atomic_write


def confirmation_directory() -> Path:
    return runtime_directory(Path.home()).parent / "mac-app" / "original-approvals"


def _identifier(value: str) -> str:
    if UUID(value).hex != value:
        raise ValueError("Invalid original approval identifier")
    return value


def _preview_path(config: MediaforceConfig, preview_id: str) -> Path:
    return config.paths.web_state_dir / "original-previews" / f"{_identifier(preview_id)}.json"


def _checks(connection: DBClient, identity: Mapping[str, Any], override: str | None) -> dict[str, Any]:
    checks = assess_readable_source(connection, identity, probe_evidence_source(Path(identity["path"]), process_controller=None), codec_override=override)
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


def _preview_item(connection: DBClient, item_id: int, path: Path | None, override: str | None) -> dict[str, Any]:
    item = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one_or_none()
    if item is None:
        raise ValueError("The named library item is unavailable")
    if item["missing_since"] or pending_source_operation(connection, item_id):
        raise ValueError("This file is missing or has an unfinished change")
    identity = observe_file((path or Path(item["source_path"])).expanduser().resolve(), full_digest=True)
    checks = _checks(connection, identity, override)
    active = connection.execute(select(source_versions).where(
        source_versions.c.version_id == item["active_source_version_id"],
    )).mappings().one_or_none()
    attached = str(Path(identity["path"]).resolve()) == str(Path(item["source_path"]).resolve())
    if attached and (identity["size_bytes"] != item["size_bytes"]
                     or identity["sample_fingerprint"] != item["content_version_fingerprint"]
                     or identity["stat"][3] != item["mtime_ns"]):
        raise ValueError("Scan this file again before preparing its approval")
    generation = (active["generation_id"] if attached and active and active["role"] != "output"
                  and active["sha256"] in (None, identity["sha256"]) else uuid4().hex)
    prior = connection.execute(select(source_admissions).where(
        source_admissions.c.generation_id == generation,
        source_admissions.c.admission_id.not_in(select(source_admissions.c.supersedes_id).where(source_admissions.c.supersedes_id.is_not(None))),
    )).mappings().all()
    if len(prior) > 1:
        raise ValueError("This original has conflicting approvals")
    return {"item_id": item_id, "rel_path": item["rel_path"], "source_revision": item["source_revision"],
            "active_version_id": item["active_source_version_id"], "source_path": item["source_path"],
            "generation_id": generation, "version_id": uuid4().hex, "identity": identity, "checks": checks,
            "attached": attached, "supersedes_id": prior[0]["admission_id"] if prior else None}


def build_original_preview(config: MediaforceConfig, item_ids: Sequence[int], *, assertion: str,
                           originals: Mapping[int, Path] | None = None, overrides: Mapping[int, str] | None = None) -> dict[str, Any]:
    if (not assertion.strip() or not item_ids or len(set(item_ids)) != len(item_ids)
            or any(type(item) is not int or item <= 0 for item in item_ids)):
        raise ValueError("Name each file once and give its independent acquisition reason")
    originals, overrides = originals or {}, overrides or {}
    if set(originals) - set(item_ids) or set(overrides) - set(item_ids) or any(not value.strip() for value in overrides.values()):
        raise ValueError("Every exception must name one of these files and its reason")
    with open_db(config.paths.db_path) as connection:
        for item_id in item_ids:
            item = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one_or_none()
            if item is None or item["missing_since"]:
                raise ValueError("The named library item is unavailable")
            ensure_active_source_version(connection, item)
        connection.commit()
        items = [_preview_item(connection, item, originals.get(item), overrides.get(item)) for item in item_ids]
    preview_id = uuid4().hex
    body = {"preview_id": preview_id, "challenge": uuid4().hex, "owner_uid": os.getuid(),
            "assertion": assertion.strip(), "items": items}
    envelope = {"preview_id": preview_id, "challenge": body["challenge"], "manifest_hash": stable_json_hash(body),
                "canonical_json": json_text(body)}
    path = _preview_path(config, preview_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, canonical_json_bytes(envelope))
    return envelope


def _read_preview(config: MediaforceConfig, preview_id: str) -> dict[str, Any]:
    try:
        preview = json.loads(_preview_path(config, preview_id).read_text())
        body = json.loads(preview["canonical_json"])
        if (body["preview_id"] != preview_id or preview["preview_id"] != preview_id
                or stable_json_hash(body) != preview["manifest_hash"] or json_text(body) != preview["canonical_json"]
                or body["challenge"] != preview["challenge"] or body["owner_uid"] != os.getuid()):
            raise ValueError("The original approval preview changed")
        return preview
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


def owner_confirmation_matches(admission: Mapping[str, Any], original: Mapping[str, Any] | None = None) -> bool:
    try:
        reference = str(admission["confirmation_ref"])
        if not reference.startswith("mac-app:"):
            return False
        receipt = _confirmation(reference.removeprefix("mac-app:"))
        checks = json.loads(admission["checks_json"])
        return any(item["version_id"] == admission["version_id"] and item["generation_id"] == admission["generation_id"]
                   and item["checks"] == checks["source_checks"] and receipt["manifest_hash"] == checks["preview_hash"]
                   and (original is None or all(original[key] == item["identity"][key] for key in ("sha256", "size_bytes")))
                   for item in json.loads(receipt["canonical_json"])["items"])
    except (ValueError, KeyError, TypeError):
        return False


def list_original_previews(config: MediaforceConfig) -> list[dict[str, Any]]:
    paths = (config.paths.web_state_dir / "original-previews").glob("*.json")
    with open_db(config.paths.db_path) as connection:
        applied = set(connection.execute(select(source_admissions.c.confirmation_ref)).scalars())
    pending = []
    for path in sorted(paths):
        if f"mac-app:{path.stem}" in applied:
            continue
        try:
            preview = _read_preview(config, path.stem)
        except ValueError:
            preview = {"preview_id": path.stem, "manifest_hash": "", "canonical_json": "", "challenge": ""}
        try:
            dismissed = (confirmation_directory() / f"{path.stem}.dismissed").read_text() == preview["manifest_hash"]
        except OSError:
            dismissed = False
        if not dismissed:
            pending.append(preview)
    return pending


def _apply_item(connection: DBClient, item: Mapping[str, Any], preview: Mapping[str, Any], now: str) -> str:
    generation = item["generation_id"]
    if connection.execute(select(source_generations).where(source_generations.c.generation_id == generation)).first() is None:
        connection.execute(source_generations.insert().values(generation_id=generation, created_at=now))
    probe = item["checks"]["raw_probe"]
    video = next(stream for stream in probe["streams"] if stream.get("codec_type") == "video")
    profile = {"video_codec": item["checks"]["video_codec"], "width": video.get("width"), "height": video.get("height"),
               "pix_fmt": video.get("pix_fmt"), "duration_seconds": float_value(probe["format"].get("duration")) or None}
    version = append_version(connection, generation_id=generation, role="input", identity=item["identity"],
                             profile=profile, recorded_at=now, version_id=item["version_id"])
    admission = stable_json_hash({"preview": preview["preview_id"], "item": item["item_id"]})
    connection.execute(source_admissions.insert().values(
        admission_id=admission, generation_id=generation, version_id=version, tier="proven", basis="owner",
        checks_json=json_text({"source_checks": item["checks"], "preview_hash": preview["manifest_hash"]}),
        confirmation_ref=f"mac-app:{preview['preview_id']}", supersedes_id=item["supersedes_id"], recorded_at=now,
    ))
    if item["attached"]:
        changed = connection.execute(update(library_items).where(
            library_items.c.id == item["item_id"], library_items.c.source_revision == item["source_revision"],
        ).values(active_source_version_id=version, source_revision=library_items.c.source_revision + 1))
        if changed.rowcount != 1:
            raise ValueError("This file changed before its approval could be saved")
    return admission


def apply_original_preview(config: MediaforceConfig, preview_id: str, manifest_hash: str,
                           *, before_commit: Callable[[DBClient], None] | None = None) -> dict[str, Any]:
    preview = _read_preview(config, preview_id)
    receipt = _confirmation(preview_id)
    if (manifest_hash != preview["manifest_hash"] or any(receipt[key] != preview[key]
            for key in ("preview_id", "manifest_hash", "canonical_json", "challenge"))):
        raise ValueError("The approval does not match this exact preview")
    body = json.loads(preview["canonical_json"])
    ids = [stable_json_hash({"preview": preview_id, "item": item["item_id"]}) for item in body["items"]]
    result = {"ok": True, "admission_ids": ids}
    with open_db(config.paths.db_path) as connection:
        existing = set(connection.execute(select(source_admissions.c.admission_id).where(
            source_admissions.c.confirmation_ref == f"mac-app:{preview_id}",
        )).scalars())
        if existing == set(ids):
            return result
        if existing:
            raise ValueError("This approval has an incomplete result")
        for item in body["items"]:
            observation = observe_file(Path(item["identity"]["path"]), full_digest=True)
            if observation != item["identity"] or _checks(connection, observation, item["checks"].get("exception_reason")) != item["checks"]:
                raise ValueError("This file or its original checks changed; prepare a new preview")
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        if before_commit is not None:
            before_commit(connection)
        for item in body["items"]:
            current = connection.execute(select(library_items).where(library_items.c.id == item["item_id"])).mappings().one_or_none()
            if (current is None or current["missing_since"] or current["source_revision"] != item["source_revision"]
                    or current["active_source_version_id"] != item["active_version_id"] or current["source_path"] != item["source_path"]
                    or pending_source_operation(connection, item["item_id"])
                    or list(file_stat_signature(Path(item["identity"]["path"]).stat())) != item["identity"]["stat"]
                    or load_source_evidence_context(connection).output_index_hash != item["checks"]["output_index_hash"]):
                raise ValueError("This file or a known output changed; prepare a new preview")
        for item in body["items"]:
            _apply_item(connection, item, preview, timestamp())
    return result
