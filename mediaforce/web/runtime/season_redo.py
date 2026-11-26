import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, DBRow, open_db
from mediaforce.core.db_tables import (
    encode_jobs,
    library_items,
    season_redos,
    staged_artifacts,
)
from mediaforce.core.media_storage import probe_media_path
from mediaforce.core.utils import timestamp
from mediaforce.encoding.encode_queue import (
    QUEUED_ENCODE_JOB_STATUSES,
    load_active_encode_jobs_for_prefix,
)
from mediaforce.library.media_scopes import (
    scope_descendant_filter,
    scope_rel_path_filter,
)
from mediaforce.library.motion_keep_state import kept_original_clause
from mediaforce.library.planner import apply_manifest_policy
from mediaforce.library.remake_intents import remake_intent
from mediaforce.library.season_redo import finish_redo_file
from mediaforce.library.source_evidence import archived_version
from mediaforce.web.runtime.left_out_files import LeftOutFile
from mediaforce.web.runtime.production_approval import (
    EffectiveApproval,
    LoadCalibrationState,
    effective_production_approval,
    production_approval_contract,
    production_approval_matches,
)


def cartoon_redo_approval(config: MediaforceConfig, prefix: str) -> EffectiveApproval | None:
    from mediaforce.web.runtime.kind_approvals import (
        CARTOON,
        CARTOON_TARGET_MB,
        load_kind_approval,
        matches_cartoon_size,
    )

    calibration = load_kind_approval(config, CARTOON)
    if not calibration:
        return None
    contract = production_approval_contract(calibration)
    approval = EffectiveApproval(prefix, calibration, contract, kind=CARTOON)
    if approval.current and matches_cartoon_size(contract, CARTOON_TARGET_MB):
        return approval
    return None


def run_approval(connection: DBClient, config: MediaforceConfig, item: Mapping[str, Any], load: LoadCalibrationState) -> EffectiveApproval | None:
    rel_path = str(item.get("rel_path") or "")
    prefix = item.get("redo_season") or connection.execute(select(season_redos.c.prefix).where(
        season_redos.c.state == "running", season_redos.c.prefix == "/".join(Path(rel_path).parts[:3]),
    )).scalar_one_or_none()
    if prefix:
        return cartoon_redo_approval(config, str(prefix))
    return effective_production_approval(config, rel_path, load)


def approved_redo_item(config: MediaforceConfig, prefix: str, stage: DBRow) -> dict[str, Any] | None:
    from mediaforce.web.runtime.manifest_reads import read_manifest

    manifest = read_manifest(Path(stage["manifest_path"] or "")) or {}
    items = manifest.get("items", [])
    index = stage["item_index"]
    if not isinstance(index, int) or not 0 <= index < len(items):
        return None
    item = items[index]
    if (
        item.get("redo_season") not in {None, prefix}
        or item.get("library_item_id") != stage["library_item_id"]
        or not production_approval_matches(cartoon_redo_approval(config, prefix), manifest, item)
    ):
        return None
    return {**item, "redo_season": prefix}


def stop_redo_season(config: MediaforceConfig, prefix: str) -> dict[str, Any]:
    with open_db(config.paths.db_path) as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        state = connection.execute(select(season_redos.c.state).where(season_redos.c.prefix == prefix)).scalar_one_or_none()
        if state not in {"marked", "stopped"}:
            return {"ok": False, "message": "This season has no pending redo to stop for."}
        now = timestamp()
        connection.execute(
            update(encode_jobs)
            .where(
                scope_rel_path_filter(encode_jobs.c.prefix, prefix),
                (
                    encode_jobs.c.status.in_(QUEUED_ENCODE_JOB_STATUSES)
                    | ((encode_jobs.c.job_kind == "folder") & (encode_jobs.c.status == "running"))
                ),
            )
            .values(status="stopped", terminal_reason="season_stopped", finished_at=now, updated_at=now)
        )
        connection.execute(update(season_redos).where(season_redos.c.prefix == prefix).values(state="stopped"))
    return {"ok": True}


def remaining_redo_files(connection: DBClient, prefix: str, redo: DBRow) -> dict[str, Any]:
    saved: dict[str, Any] = json.loads(redo["remaining_json"] or "{}")
    rows = connection.execute(
        select(library_items.c.id, library_items.c.fingerprint, staged_artifacts)
        .outerjoin(staged_artifacts, staged_artifacts.c.library_item_id == library_items.c.id)
        .where(
            scope_descendant_filter(library_items.c.rel_path, prefix),
            library_items.c.missing_since.is_(None),
            ~kept_original_clause(library_items.c.id),
        )
    ).mappings()
    remaining = {}
    for row in rows:
        key = str(row["id"])
        if redo["remaining_json"] is not None and key not in saved:
            continue
        prior = saved.get(key)
        if not (prior or {}).get("stage") and row["promoted_at"]:
            prior = {"stage": {col.name: row[col] for col in staged_artifacts.c}, "fingerprint": row["fingerprint"]}
        remaining[key] = prior
    return remaining


def archived_redo_items(
    connection: DBClient, config: MediaforceConfig, prefix: str, remaining: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[LeftOutFile]]:
    items, left_out = [], []
    for key, original_stage in remaining.items():
        if not (original_stage or {}).get("stage"):
            continue
        item_id = int(key)
        row = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()
        stage = original_stage["stage"]
        current = (
            connection.execute(select(staged_artifacts).where(staged_artifacts.c.library_item_id == item_id)).mappings().first()
        )
        if current and current["promoted_at"] is None and not remake_intent(current):
            left_out.append(LeftOutFile(item_id, row["rel_path"], "awaiting_checks", "This copy is already waiting for its checks."))
            continue
        try:
            old = json.loads(Path(stage["manifest_path"]).read_text(encoding="utf-8"))["items"][stage["item_index"]]
            backup = Path(stage["archived_source_path"] or "").resolve()
            relative_backup = backup.relative_to(config.archive_root.resolve())
            probe = probe_media_path(backup)
            if probe.checked_info() is None:
                raise ValueError("The original backup could not be found.")
            recorded = archived_version(connection, item_id, str(backup))
            sample = old.get("content_version_fingerprint") or (recorded["sample_fingerprint"] if recorded else None)
            if (
                old["library_item_id"] != item_id
                or not old["rel_path"].startswith(prefix + "/")
                or not probe.is_file
                or not sample
                or not row["rel_path"].startswith(prefix + "/")
                or row["fingerprint"] != original_stage["fingerprint"]
                or recorded
                and recorded["sample_fingerprint"] != sample
            ):
                raise ValueError("The original backup or the current library copy no longer matches the saved work.")
            item = apply_manifest_policy(dict(old), config)
            item.update(
                source_path=row["source_path"],
                rel_path=row["rel_path"],
                source_fingerprint=row["fingerprint"],
                redo_backup=relative_backup.as_posix(),
                redo_library_size=row["size_bytes"],
                redo_library_sample=row["content_version_fingerprint"],
                redo_library_revision=row["source_revision"],
                redo_original_path=old["source_path"],
                redo_original_fingerprint=sample,
                redo_cadence_fingerprint=old["source_fingerprint"],
            )
            item.pop("target_lineage", None)
            items.append(item)
        except (OSError, KeyError, IndexError, ValueError, TypeError):
            left_out.append(
                LeftOutFile(
                    item_id,
                    row["rel_path"],
                    "original_unavailable",
                    "Restore the original backup and its saved settings to retry, or keep the current episode.",
                )
            )
    return items, left_out


def keep_unavailable_redo(config: MediaforceConfig, prefix: str, item_id: int) -> dict[str, Any]:
    with open_db(config.paths.db_path) as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        saved = connection.execute(select(season_redos.c.remaining_json).where(season_redos.c.prefix == prefix)).scalar_one_or_none()
        prior = json.loads(str(saved or "{}")).get(str(item_id), {})
        if not isinstance(prior, dict) or not prior.get("reason"):
            return {"ok": False, "message": "This episode is no longer waiting for that choice."}
        rel_path = connection.execute(select(library_items.c.rel_path).where(library_items.c.id == item_id)).scalar_one_or_none()
        if not str(rel_path or "").startswith(prefix + "/"):
            return {"ok": False, "message": "This episode is no longer in this season."}
        if load_active_encode_jobs_for_prefix(connection, str(rel_path)):
            return {"ok": False, "message": "A file is still being worked on. Let it finish first."}
        current = connection.execute(
            select(staged_artifacts.c.promoted_at).where(staged_artifacts.c.library_item_id == item_id)
        ).scalar_one_or_none()
        if not current:
            return {"ok": False, "message": "A replacement is still being checked."}
        finish_redo_file(connection, prefix, item_id)
    return {"ok": True}
