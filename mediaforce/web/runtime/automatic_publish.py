import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import insert, or_, select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import item_events, library_items, staged_artifacts
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.core.utils import file_fingerprint, timestamp
from mediaforce.encoding.encode_queue import load_active_encode_jobs_for_prefix
from mediaforce.encoding.delivery_lock import delivery_lock
from mediaforce.encoding.staging import PromotionRestoreError, PromotionWaiting
from mediaforce.execution import promote_one_item, validate_one_item
from mediaforce.library.staged_integrity import staged_integrity_report
from mediaforce.web.runtime.folder_actions import current_production_approval_matches, production_action_blocker


LOGGER = logging.getLogger(__name__)
LoadCalibrationState = Callable[[MediaforceConfig, str], dict[str, Any] | None]


def publish_checked_files_once(config: MediaforceConfig, *, load_calibration_state: LoadCalibrationState) -> None:
    """Check and publish each finished production file; temporary holds retry next pass."""
    with open_db(config.paths.db_path) as connection:
        item_ids = list(
            connection.execute(
                select(staged_artifacts.c.library_item_id)
                .join(library_items, library_items.c.id == staged_artifacts.c.library_item_id)
                .where(staged_artifacts.c.promoted_at.is_(None))
                .where(library_items.c.status.in_(("encoded", "validated")))
                .where(
                    or_(staged_artifacts.c.encode_origin.is_(None), staged_artifacts.c.encode_origin != "calibration")
                )
                .order_by(staged_artifacts.c.library_item_id)
            ).scalars()
        )
    for item_id in item_ids:
        with delivery_lock(config.paths.db_path, item_id, blocking=False) as acquired:
            if not acquired:
                continue
            with open_db(config.paths.db_path) as connection:
                _try_publish_file(connection, config, item_id, load_calibration_state)


def _try_publish_file(
    connection: DBClient, config: MediaforceConfig, item_id: int, load_calibration_state: LoadCalibrationState
) -> None:
    try:
        _publish_file(connection, config, item_id, load_calibration_state)
    except PromotionRestoreError:
        LOGGER.exception("Automatic publish could not restore item %s", item_id)
        connection.rollback()
        _record_wait(
            connection,
            item_id,
            "unsafe",
            "Publishing could not put the files back. Check this file before trying again.",
        )
    except PromotionWaiting as exc:
        connection.rollback()
        _record_wait(connection, item_id, "waiting", str(exc))
    except FileExistsError:
        connection.rollback()
        _record_wait(connection, item_id, "waiting", "A different file is already at its place in the library.")
    except Exception:
        LOGGER.exception("Automatic check or publish failed for item %s", item_id)
        connection.rollback()
        _record_wait(
            connection, item_id, "waiting", "Mediaforce could not check or publish this file. It will try again."
        )


def _publish_file(
    connection: DBClient, config: MediaforceConfig, item_id: int, load_calibration_state: LoadCalibrationState
) -> None:
    stage = (
        connection.execute(select(staged_artifacts).where(staged_artifacts.c.library_item_id == item_id))
        .mappings()
        .first()
    )
    source = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().first()
    if stage is None or source is None or stage["promoted_at"] is not None or stage["encode_origin"] == "calibration":
        return
    if source["status"] not in {"encoded", "validated"}:
        return
    if stage["encode_origin"] not in {"queue", "cli"}:
        _record_wait(
            connection,
            item_id,
            "waiting",
            "This file's production origin is unknown. Check and publish it manually if it is a full replacement.",
        )
        return
    validation = object_dict(json.loads(stage["validation_json"] or "{}"))
    if object_dict(validation.get("automatic_publish")).get("state") == "unsafe":
        return
    rel_path = str(source["rel_path"])
    blocker = production_action_blocker(config, rel_path)
    if blocker is not None:
        _record_wait(connection, item_id, "waiting", str(blocker["message"]))
        return
    # Folder parents coordinate children; only actual file jobs can still write this output.
    for job in load_active_encode_jobs_for_prefix(connection, rel_path):
        if job.get("job_kind") == "folder":
            continue
        active_manifest = object_dict(json.loads(Path(str(job["manifest_path"])).read_text()))
        active_items = object_list(active_manifest.get("items"))
        indexes = object_list(job.get("manifest_indexes")) or list(range(len(active_items)))
        if not active_items:
            raise RuntimeError("An active file job's membership is unavailable")
        if any(int(object_dict(active_items[index]).get("library_item_id") or 0) == item_id for index in indexes):
            _record_wait(
                connection, item_id, "waiting", "This file is still being made. It will be checked when it finishes."
            )
            return
    manifest = object_dict(json.loads(Path(str(stage["manifest_path"])).read_text()))
    item = object_dict(object_list(manifest.get("items"))[int(stage["item_index"])])
    if int(item.get("library_item_id") or 0) != item_id or str(item.get("source_path")) != str(source["source_path"]):
        _record_wait(
            connection,
            item_id,
            "waiting",
            "The saved work no longer matches this file. Make it again with the current settings.",
        )
        return
    prefixes = [rel_path, *(parent.as_posix() for parent in Path(rel_path).parents if parent.as_posix() != ".")]
    calibration = next(
        (state for prefix in prefixes if (state := load_calibration_state(config, prefix)) is not None), None
    )
    if not current_production_approval_matches(calibration, manifest, item):
        _record_wait(
            connection,
            item_id,
            "waiting",
            "This file needs a current matching sample approval before it can be published.",
        )
        return
    path = Path(str(source["source_path"]))
    fingerprint = file_fingerprint(path, path.stat(), source["duration_seconds"])
    if (
        fingerprint != item.get("source_fingerprint")
        or fingerprint != source["fingerprint"]
        or fingerprint != stage["source_fingerprint"]
    ):
        _record_wait(
            connection,
            item_id,
            "waiting",
            "The original changed after this work was planned. Check it before making a replacement.",
        )
        return
    report = staged_integrity_report(connection, config, rel_path, discover=False)
    record = next((record for record in report.records if record.item_id == item_id), None)
    if report.database_truncated or record is None:
        raise RuntimeError("This file's integrity record is unavailable")
    if record.disposition not in {"unvalidated", "promotable"}:
        _record_wait(connection, item_id, "waiting", record.detail)
        return
    connection.commit()
    if record.disposition == "unvalidated":
        result = validate_one_item(connection, config, item)
        if not result["passed"]:
            _record_wait(
                connection,
                item_id,
                "waiting",
                "This file did not pass its checks. Inspect the check results before replacing it.",
            )
            return
    # Validation may repair a container. Check the resulting evidence before installing it.
    report = staged_integrity_report(connection, config, rel_path, discover=False)
    if not any(record.item_id == item_id and record.disposition == "promotable" for record in report.records):
        _record_wait(connection, item_id, "waiting", "The checked file changed. Check it again before replacing it.")
        return
    connection.commit()
    calibration = next(
        (state for prefix in prefixes if (state := load_calibration_state(config, prefix)) is not None), None
    )
    if not current_production_approval_matches(calibration, manifest, item):
        _record_wait(
            connection,
            item_id,
            "waiting",
            "The approval changed during checking. Review the current sample before publishing this file.",
        )
        return
    if file_fingerprint(path, path.stat(), source["duration_seconds"]) != fingerprint:
        _record_wait(
            connection,
            item_id,
            "waiting",
            "The original changed during checking. Check it before making a replacement.",
        )
        return
    promote_one_item(connection, config, item, force=False)


def _record_wait(connection: DBClient, item_id: int, state: str, reason: str) -> None:
    row = connection.execute(
        select(staged_artifacts.c.validation_json).where(staged_artifacts.c.library_item_id == item_id)
    ).first()
    if row is None:
        return
    try:
        validation = object_dict(json.loads(row[0] or "{}"))
    except (ValueError, TypeError):
        validation = {"passed": False, "unreadable_validation_json": row[0]}
        reason = "Its saved check results could not be read. Check this file again before replacing it."
    delivery = {"state": state, "reason": reason}
    if validation.get("automatic_publish") == delivery:
        connection.rollback()
        return
    validation["automatic_publish"] = delivery
    now = timestamp()
    connection.execute(
        update(staged_artifacts)
        .where(staged_artifacts.c.library_item_id == item_id)
        .values(validation_json=json.dumps(validation), updated_at=now)
    )
    connection.execute(
        insert(item_events).values(
            library_item_id=item_id,
            event_type="automatic_publish_waiting",
            details_json=json.dumps(delivery),
            created_at=now,
        )
    )
    connection.commit()
