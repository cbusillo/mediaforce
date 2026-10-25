"""The owner's request to put one replaced file's original back, checked against its page and running work."""

from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import library_items
from mediaforce.core.utils import content_version_fingerprint, file_fingerprint, timestamp
from mediaforce.encoding.delivery_lock import archive_activity, delivery_lock
from mediaforce.encoding.put_back import PutBackRefused, PutBackRestoreError, put_original_back
from mediaforce.encoding.streams import _record_event
from mediaforce.library.media_scopes import path_matches_scope, resolve_media_scope
from mediaforce.library.probe import probe_media
from mediaforce.web.runtime.folder_actions import active_encode_library_item_ids


def put_original_back_action(config: MediaforceConfig, prefix: str, library_item_id: int) -> dict[str, Any]:
    """Put the named file's original back; a refusal changes nothing and says why."""
    try:
        with open_db(config.paths.db_path) as connection:
            scope = resolve_media_scope(connection, prefix, library_types=config.library_type_map)
            rel_path = connection.execute(
                select(library_items.c.rel_path).where(library_items.c.id == library_item_id)
            ).scalar_one_or_none()
            if rel_path is None or not path_matches_scope(str(rel_path), scope.prefix):
                raise PutBackRefused("not_here", "This file is not in this folder anymore.")
            active = active_encode_library_item_ids(connection, scope.prefix)
            if active is None or library_item_id in active:
                raise PutBackRefused("busy", "This file is being worked on right now. Try again when that finishes.")
            connection.commit()
            with delivery_lock(config.paths.db_path, library_item_id), \
                    archive_activity(config.paths.db_path) as available:
                if not available:
                    raise PutBackRefused("busy", "Original backups are being cleaned. Try again when that finishes.")
                original = put_original_back(
                    connection,
                    library_item_id,
                    probe_media=probe_media,
                    file_fingerprint=file_fingerprint,
                    content_fingerprint=content_version_fingerprint,
                    timestamp=timestamp,
                    record_event=_record_event,
                )
    except PutBackRefused as refused:
        return {"ok": False, "code": refused.code, "message": str(refused)}
    except PutBackRestoreError:
        return {
            "ok": False,
            "code": "unsafe",
            "message": "Putting the original back failed partway, and Mediaforce could not return the files. "
                       "Look in the library and the original backups folder before trying again.",
        }
    return {"ok": True, "rel_path": str(rel_path), "original_path": str(original)}
