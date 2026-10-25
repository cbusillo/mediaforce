"""Put a replaced file's original back from its backup, at the owner's request.

The compressed copy leaves the library for its staging place and the original returns to where it was,
through the same journaled moves publishing uses, so an interruption is reconciled the same way. Once the
catalog records the original, the compressed copy and its stage are removed, as when a rescan finds a
replaced file changed. The file is then kept original for the source that came back, so nothing
compresses it again until the owner undoes that choice.
"""

import logging
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, motion_keep_decisions, source_transitions, \
    source_versions, staged_artifacts
from mediaforce.library.source_evidence import append_version, ensure_active_source_version, json_text, observe_file, \
    pending_source_operation
from mediaforce.encoding.source_publication import apply_source_moves, commit_source_publish, reconcile_source_publish, \
    record_prepared_moves

LOGGER = logging.getLogger(__name__)
PUT_BACK_REASON = "put_back"
PUT_BACK_EVENT = "original_put_back"


class PutBackRefused(RuntimeError):
    """Nothing moved; ``code`` says why in a form the app words."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PutBackRestoreError(RuntimeError):
    """A failed put-back could not return its files, so the original or its copy may be out of place."""


def reconcile_unfinished_put_backs(connection: DBClient, library_item_id: int | None = None) -> int:
    """Return the files of put-backs stopped partway to where the catalog says they are; count those that cannot be."""
    connection.commit()
    statement = select(source_transitions.c.operation_id).where(
        source_transitions.c.kind == "put-back",
        source_transitions.c.outcome.in_(("prepared", "unresolved")),
    )
    if library_item_id is not None:
        statement = statement.where(source_transitions.c.library_item_id == library_item_id)
    return sum(not reconcile_source_publish(connection, operation)
               for operation in connection.execute(statement).scalars().all())


def _original_version(connection: DBClient, item_id: int, active: str, archive_identity: dict[str, Any]) -> str:
    """The version publishing replaced, or a new one in the same generation when it was published before the journal.

    A recorded original that differs from the backup on disk is refused: that backup is not this file's original.
    """
    replaced = connection.execute(select(source_transitions.c.before_version_id).where(
        source_transitions.c.library_item_id == item_id,
        source_transitions.c.kind == "publish",
        source_transitions.c.outcome == "committed",
        source_transitions.c.after_version_id == active,
    ).order_by(source_transitions.c.recorded_at.desc())).scalars().first()
    if replaced:
        version = connection.execute(select(source_versions).where(source_versions.c.version_id == replaced)).mappings().one()
        if (version["size_bytes"] != archive_identity["size_bytes"]
                or version["sample_fingerprint"] != archive_identity["sample_fingerprint"]):
            raise PutBackRefused("backup_changed", "Its original backup changed since it was made, so it was not put back.")
        return str(replaced)
    generation = connection.execute(select(source_versions.c.generation_id).where(
        source_versions.c.version_id == active,
    )).scalar_one()
    return append_version(connection, generation_id=generation, role="input", identity=archive_identity, profile={},
                          recorded_at=str(archive_identity.get("observed_at") or ""))


def _prepare(connection: DBClient, item: Mapping[str, Any], *, library_path: Path, out_path: Path,
             archive_path: Path, original_path: Path, now: str) -> str:
    """Persist both intended moves before touching media, as publishing does."""
    connection.commit()
    item_id = int(item["id"])
    if pending_source_operation(connection, item_id):
        raise PutBackRefused("busy", "Another change to this file has not finished yet.")
    library_identity = observe_file(library_path)
    if (library_identity["size_bytes"] != item["size_bytes"]
            or (item["content_version_fingerprint"]
                and library_identity["sample_fingerprint"] != item["content_version_fingerprint"])):
        raise PutBackRefused("file_changed", "The file in the library changed since Mediaforce last looked at it.")
    archive_identity = observe_file(archive_path)
    active = ensure_active_source_version(connection, item)
    current = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()
    original = _original_version(connection, item_id, active, {**archive_identity, "observed_at": now})
    operation = uuid4().hex
    connection.execute(source_transitions.insert().values(
        operation_id=operation, library_item_id=item_id, before_version_id=active, after_version_id=original,
        expected_revision=current["source_revision"], kind="put-back", outcome="prepared",
        evidence_json=json_text({"strength": "observed", "limitation": "stat and samples across journaled moves"}),
        recorded_at=now,
    ))
    moves = [(active, library_path, out_path, library_identity), (original, archive_path, original_path, archive_identity)]
    record_prepared_moves(connection, operation, moves)
    return operation


def put_original_back(
        connection: DBClient,
        library_item_id: int,
        *,
        probe_media: Callable[[Path], Any],
        file_fingerprint: Callable[[Path, Any, float | None], str],
        content_fingerprint: Callable[[Path, Any], str],
        timestamp: Callable[[], str],
        record_event: Callable[[DBClient, int, str, dict[str, Any]], None],
) -> Path:
    """Return the original to the library and keep it original; refuse plainly when that cannot be done safely.

    An earlier put-back stopped partway is first returned to where the catalog says its files are, so a retry
    starts from a known place.
    """
    if reconcile_unfinished_put_backs(connection, library_item_id):
        raise PutBackRestoreError("An earlier put-back left files Mediaforce cannot place")
    item = connection.execute(select(library_items).where(library_items.c.id == library_item_id)).mappings().one_or_none()
    stage = connection.execute(select(staged_artifacts).where(
        staged_artifacts.c.library_item_id == library_item_id,
    )).mappings().one_or_none()
    if item is None or stage is None or stage["promoted_at"] is None:
        raise PutBackRefused("not_replaced", "This file was never replaced, so its original is already in the library.")
    library_path = Path(str(stage["promoted_path"]))
    archive_path = Path(str(stage["archived_source_path"] or ""))
    original_path = Path(str(stage["source_path"]))
    out_path = Path(str(stage["staging_path"]))
    if not stage["archived_source_path"] or not archive_path.is_file():
        raise PutBackRefused("backup_gone", "Its original backup was deleted, so there is nothing to put back.")
    if Path(str(item["source_path"])) != library_path or not library_path.is_file():
        raise PutBackRefused("file_changed", "The file in the library is not the one Mediaforce put there.")
    if original_path != library_path and original_path.exists():
        raise PutBackRefused("occupied", "Another file is already where the original goes.")
    if out_path.exists():
        raise PutBackRefused("occupied", "Another file is already where the compressed copy goes.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    original_path.parent.mkdir(parents=True, exist_ok=True)

    now = timestamp()
    try:
        operation = _prepare(connection, item, library_path=library_path, out_path=out_path,
                             archive_path=archive_path, original_path=original_path, now=now)
    except PutBackRefused:
        connection.rollback()
        raise
    try:
        apply_source_moves(connection, operation)
        stat = original_path.stat()
        probe = probe_media(original_path)
        content = content_fingerprint(original_path, stat)
        commit_source_publish(connection, operation)
        connection.execute(update(library_items).where(library_items.c.id == library_item_id).values(
            source_path=str(original_path),
            rel_path=str(PurePosixPath(str(item["rel_path"])).with_name(original_path.name)),
            file_name=original_path.name,
            container=original_path.suffix.lower(),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            fingerprint=file_fingerprint(original_path, stat, probe.duration_seconds),
            duration_seconds=probe.duration_seconds,
            video_codec=probe.video_codec,
            video_bitrate=probe.video_bitrate,
            width=probe.width,
            height=probe.height,
            pix_fmt=probe.pix_fmt,
            audio_track_count=probe.audio_track_count,
            subtitle_track_count=probe.subtitle_track_count,
            english_audio_count=probe.english_audio_count,
            english_subtitle_count=probe.english_subtitle_count,
            default_audio_language=probe.default_audio_language,
            default_subtitle_language=probe.default_subtitle_language,
            audio_summary_json=probe.audio_summary_json,
            subtitle_summary_json=probe.subtitle_summary_json,
            attachment_summary_json=probe.attachment_summary_json,
            content_version_changed_at=now,
            content_version_fingerprint=content,
            status="discovered",
            missing_since=None,
            updated_at=now,
            last_seen_at=now,
        ))
        connection.execute(delete(staged_artifacts).where(staged_artifacts.c.library_item_id == library_item_id))
        keep = {"source_fingerprint": content, "reason_code": PUT_BACK_REASON, "prefix": str(item["parent_dir"]),
                "kept_at": now}
        connection.execute(
            sqlite_insert(motion_keep_decisions)
            .values(library_item_id=library_item_id, **keep)
            .on_conflict_do_update(index_elements=[motion_keep_decisions.c.library_item_id], set_=keep)
        )
        record_event(connection, library_item_id, PUT_BACK_EVENT, {
            "original_path": str(original_path),
            "archive_path": str(archive_path),
            "removed_compressed_path": str(out_path),
            "bytes_saved_before": stage["bytes_saved"],
        })
        connection.commit()
    except BaseException as put_back_error:
        connection.rollback()
        try:
            if not reconcile_source_publish(connection, operation):
                raise RuntimeError("Recorded file locations could not be reconciled")
        except BaseException as restore_error:
            raise PutBackRestoreError(
                f"Putting the original back failed and the files could not be returned: {restore_error}"
            ) from put_back_error
        raise
    try:
        out_path.unlink(missing_ok=True)
    except OSError as error:
        LOGGER.warning("Put back %s, but could not remove its compressed copy %s: %s", original_path, out_path, error)
    return original_path

