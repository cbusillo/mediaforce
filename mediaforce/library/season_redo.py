import json
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, season_redos, staged_artifacts
from mediaforce.core.utils import content_version_fingerprint
from mediaforce.hosts.config import execution_mode_for_host, host_media_access_for_host
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
    paths = set()
    for prefix, pending in connection.execute(select(season_redos.c.prefix, season_redos.c.remaining_json)):
        paths.add((root / prefix).resolve())
        for prior in json.loads(pending or "{}").values():
            if original := (prior or {}).get("stage", {}).get("archived_source_path"):
                paths.add(Path(original).resolve())
    return paths


def finish_redo_file(connection: DBClient, prefix: str, item_id: int, *, keep_current: bool = False) -> None:
    row = connection.execute(
        select(season_redos.c.remaining_json).where(season_redos.c.prefix == prefix, season_redos.c.state == "running")
    ).scalar_one_or_none()
    if row is None:
        return
    remaining = json.loads(row)
    if str(item_id) not in remaining:
        return
    prior = remaining.pop(str(item_id))
    if keep_current and prior:
        current = connection.execute(select(library_items.c.fingerprint).where(library_items.c.id == item_id)).scalar_one()
        if current == prior["fingerprint"]:
            connection.execute(staged_artifacts.insert().values(**prior["stage"]))
            connection.execute(update(library_items).where(library_items.c.id == item_id).values(status="promoted"))
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


def backup_encode_host(host: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    if (
        execution_mode_for_host(host) == "ssh"
        and host_media_access_for_host(host) != "stream"
        and any(item.get("redo_backup") for item in items)
    ):
        return {**host, "media_access": "stream", "scratch_root": None, "source_roots": {}}
    return host
