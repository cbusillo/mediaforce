import json
from pathlib import Path

from sqlalchemy import delete, select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.utils import content_version_fingerprint
from typing import Any
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, season_redos
from mediaforce.library.media_scopes import scope_descendant_filter


def redo_prefixes(connection: DBClient) -> set[str]:
    return set(connection.execute(select(season_redos.c.prefix)).scalars())


def redo_item_ids(connection: DBClient) -> set[int]:
    return set(
        connection.execute(
            select(library_items.c.id).join(season_redos, scope_descendant_filter(library_items.c.rel_path, season_redos.c.prefix))
        ).scalars()
    )


def archive_season_paths(connection: DBClient, root: Path) -> set[Path]:
    return {(root / prefix).resolve() for prefix in redo_prefixes(connection)}


def finish_redo_file(connection: DBClient, prefix: str, item_id: int) -> None:
    row = connection.execute(
        select(season_redos.c.remaining_json).where(season_redos.c.prefix == prefix, season_redos.c.state == "running")
    ).scalar_one_or_none()
    if row is None:
        return
    remaining = json.loads(row)
    remaining.pop(str(item_id), None)
    if remaining:
        connection.execute(update(season_redos).where(season_redos.c.prefix == prefix).values(remaining_json=json.dumps(remaining)))
    else:
        connection.execute(delete(season_redos).where(season_redos.c.prefix == prefix))


def verify_redo_input(config: MediaforceConfig, item: dict[str, Any], *, input_path: Path | None = None) -> None:
    root = config.archive_root.resolve()
    backup = (root / item["redo_backup"]).resolve()
    path = input_path or backup
    if not backup.is_relative_to(root) or content_version_fingerprint(path, path.stat()) != item["redo_original_fingerprint"]:
        raise ValueError("The original backup changed after it was saved.")
