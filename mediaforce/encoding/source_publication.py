import json
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, source_generations, source_moves, source_transitions, source_versions
from mediaforce.library.source_evidence import append_version, ensure_active_source_version, json_text, observe_file, \
    pending_source_operation, version_locations


def _matches(path: Path, identity: Mapping[str, Any]) -> bool:
    try:
        observed = observe_file(path)
    except (OSError, RuntimeError):
        return False
    return (observed["size_bytes"] == identity.get("size_bytes")
            and observed["sample_fingerprint"] == identity.get("sample_fingerprint"))


def prepare_source_publish(connection: DBClient, item_id: int, *, source_path: Path, staging_path: Path,
                           archive_path: Path, destination_path: Path, backup_path: Path | None) -> str:
    """Persist all intended moves before touching media. No media read holds a write transaction."""
    connection.commit()
    if pending_source_operation(connection, item_id):
        raise RuntimeError("Source has an unresolved publication; reconcile it before publishing")
    item = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()
    active = item.get("active_source_version_id")
    previous = connection.execute(select(source_versions).where(source_versions.c.version_id == active)).mappings().one_or_none()
    try:
        source_identity = observe_file(source_path, full_digest=previous is None or not previous["sha256"])
    except FileNotFoundError:
        source_identity = None
    if source_identity is not None and (source_identity["size_bytes"] != item["size_bytes"]
            or (item["content_version_fingerprint"] and source_identity["sample_fingerprint"] != item["content_version_fingerprint"])):
        raise RuntimeError("Publish input changed since the catalog snapshot")
    outputs = connection.execute(select(source_versions).where(source_versions.c.role == "output")).mappings().all()
    output_identity = observe_file(staging_path)
    output = next((row for row in outputs if row["sha256"] and row["size_bytes"] == output_identity["size_bytes"]
                   and row["sample_fingerprint"] == output_identity["sample_fingerprint"]
                   and json.loads(row["identity_json"]).get("path") == str(staging_path)), None)
    if output is None:
        output_identity = observe_file(staging_path, full_digest=True)
    else:
        output_identity["sha256"] = output["sha256"]
    archive_identity = observe_file(archive_path) if backup_path and source_identity is not None else None
    archive_version = None
    if archive_identity is not None:
        candidates = connection.execute(select(source_versions).where(
            source_versions.c.size_bytes == archive_identity["size_bytes"],
            source_versions.c.sample_fingerprint == archive_identity["sample_fingerprint"],
        )).mappings().all()
        locations = version_locations(connection, candidates)
        archive_version = next((row for row in candidates if locations[row["version_id"]] == str(archive_path)), None)
    before = ensure_active_source_version(connection, item)
    current = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()
    version = connection.execute(select(source_versions).where(source_versions.c.version_id == before)).mappings().one()
    generation = version["generation_id"]
    now = datetime.now(UTC).isoformat()
    if source_identity is not None and not version["sha256"]:
        before = append_version(connection, generation_id=generation, role=version["role"], identity=source_identity,
                                profile=json.loads(version["profile_json"]), recorded_at=now)
        connection.execute(update(library_items).where(library_items.c.id == item_id).values(
            active_source_version_id=before, source_revision=library_items.c.source_revision + 1,
        ))
        current = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()
    elif source_identity is not None:
        source_identity["sha256"] = version["sha256"]
    after = (output["version_id"] if output is not None and output["generation_id"] == generation else
             append_version(connection, generation_id=generation, role="output", identity=output_identity,
                            profile=json.loads(output["profile_json"]) if output else {}, recorded_at=now))
    operation = uuid4().hex
    connection.execute(source_transitions.insert().values(
        operation_id=operation, library_item_id=item_id, before_version_id=before, after_version_id=after,
        expected_revision=current["source_revision"], kind="publish", outcome="prepared",
        evidence_json=json_text({"strength": "observed", "limitation": "stat and samples across journaled moves",
                                 "source_available": source_identity is not None}),
        recorded_at=now,
    ))
    moves: list[tuple[str, Path, Path, dict[str, Any]]] = []
    if backup_path is not None and archive_identity is not None:
        if archive_version is None:
            detached_generation = uuid4().hex
            connection.execute(source_generations.insert().values(generation_id=detached_generation, created_at=now))
            archive_id = append_version(connection, generation_id=detached_generation, role="unknown", identity=archive_identity,
                                        profile={}, recorded_at=now)
        else:
            archive_id = archive_version["version_id"]
        moves.append((archive_id, archive_path, backup_path, archive_identity))
    if source_identity is not None:
        moves.append((before, source_path, archive_path, source_identity))
    moves.append((after, staging_path, destination_path, output_identity))
    record_prepared_moves(connection, operation, moves)
    return operation


def record_prepared_moves(connection: DBClient, operation: str,
                          moves: list[tuple[str, Path, Path, dict[str, Any]]]) -> None:
    """Record an operation's moves in order, before any file is touched."""
    for ordinal, (version_id, old, new, identity) in enumerate(moves):
        connection.execute(source_moves.insert().values(
            operation_id=operation, ordinal=ordinal, version_id=version_id, from_path=str(old), to_path=str(new),
            phase="prepared", checks_json=json_text(identity),
        ))
    connection.commit()


def apply_source_moves(connection: DBClient, operation: str) -> None:
    moves = connection.execute(select(source_moves).where(source_moves.c.operation_id == operation)
                               .order_by(source_moves.c.ordinal)).mappings().all()
    for move in moves:
        old, new = Path(move["from_path"]), Path(move["to_path"])
        expected = json.loads(move["checks_json"])
        if not _matches(old, expected) or new.exists():
            raise RuntimeError("Publish move no longer matches its prepared identities")
        shutil.move(str(old), str(new))
        observed = observe_file(new)
        if observed["sample_fingerprint"] != expected["sample_fingerprint"] or observed["size_bytes"] != expected["size_bytes"]:
            raise RuntimeError("Publish destination does not match its prepared identity")
        connection.execute(update(source_moves).where(
            source_moves.c.operation_id == operation, source_moves.c.ordinal == move["ordinal"],
        ).values(phase="moved", checks_json=json_text({**expected, "after_move": observed})))
        connection.commit()


def commit_source_publish(connection: DBClient, operation: str) -> None:
    row = connection.execute(select(source_transitions).where(source_transitions.c.operation_id == operation)).mappings().one()
    for move in connection.execute(select(source_moves).where(source_moves.c.operation_id == operation)).mappings():
        checks = json.loads(move["checks_json"])
        if move["phase"] != "moved" or observe_file(Path(move["to_path"])) != checks.get("after_move"):
            raise RuntimeError("Publish file changed before catalog commit")
    result = connection.execute(update(library_items).where(
        library_items.c.id == row["library_item_id"], library_items.c.active_source_version_id == row["before_version_id"],
        library_items.c.source_revision == row["expected_revision"],
    ).values(active_source_version_id=row["after_version_id"], source_revision=library_items.c.source_revision + 1))
    if result.rowcount != 1 or row["outcome"] != "prepared":
        raise RuntimeError("Publish source revision changed")
    connection.execute(update(source_transitions).where(source_transitions.c.operation_id == operation).values(outcome="committed"))


def reconcile_source_publish(connection: DBClient, operation: str) -> bool:
    """Roll back only identifiable journaled moves; an ambiguous location remains unresolved."""
    row = connection.execute(select(source_transitions).where(source_transitions.c.operation_id == operation)).mappings().one()
    if row["outcome"] in {"committed", "rolled_back"}:
        return True
    moves = connection.execute(select(source_moves).where(source_moves.c.operation_id == operation)
                               .order_by(source_moves.c.ordinal.desc())).mappings().all()
    try:
        for move in moves:
            old, new = Path(move["from_path"]), Path(move["to_path"])
            identity = json.loads(move["checks_json"])
            at_old, at_new = _matches(old, identity), _matches(new, identity)
            if at_old == at_new:
                raise RuntimeError("The recorded file location is ambiguous or unavailable")
            if at_new:
                if old.exists():
                    raise RuntimeError("Cannot restore source over an occupied path")
                shutil.move(str(new), str(old))
                if not _matches(old, identity):
                    raise RuntimeError("Restored source identity is unavailable")
        connection.execute(update(source_moves).where(source_moves.c.operation_id == operation).values(phase="rolled_back"))
        connection.execute(update(source_transitions).where(source_transitions.c.operation_id == operation).values(outcome="rolled_back"))
        connection.execute(update(library_items).where(library_items.c.id == row["library_item_id"]).values(
            source_revision=library_items.c.source_revision + 1,
        ))
        connection.commit()
        return True
    except (OSError, RuntimeError):
        connection.rollback()
        connection.execute(update(source_transitions).where(source_transitions.c.operation_id == operation).values(outcome="unresolved"))
        connection.commit()
        return False


def pending_archive_paths(connection: DBClient) -> set[Path]:
    rows = connection.execute(select(source_moves.c.from_path, source_moves.c.to_path).join(source_transitions).where(
        source_transitions.c.outcome.in_(("prepared", "unresolved")),
    )).mappings()
    return {Path(path).resolve() for row in rows for path in (row["from_path"], row["to_path"]) if path}


def path_in_pending_move(connection: DBClient, path: str) -> bool:
    """Whether unfinished publishing or a put-back is moving a file to or from this path."""
    return connection.execute(select(source_moves.c.ordinal).join(source_transitions).where(
        source_transitions.c.outcome.in_(("prepared", "unresolved")),
        (source_moves.c.from_path == path) | (source_moves.c.to_path == path),
    ).limit(1)).first() is not None


def prepare_archive_removal(connection: DBClient, path: Path) -> list[str]:
    try:
        observed = observe_file(path)
    except FileNotFoundError:
        return []
    versions = connection.execute(select(source_versions).where(
        source_versions.c.size_bytes == observed["size_bytes"],
        source_versions.c.sample_fingerprint == observed["sample_fingerprint"],
    )).mappings().all()
    locations = version_locations(connection, versions)
    operations: list[str] = []
    for version in versions:
        if locations[version["version_id"]] != str(path):
            continue
        operation = uuid4().hex
        operations.append(operation)
        connection.execute(source_transitions.insert().values(
            operation_id=operation, before_version_id=version["version_id"], after_version_id=version["version_id"],
            expected_revision=0, kind="owner-cleanup", outcome="prepared", evidence_json=json_text(observed), recorded_at=datetime.now(UTC).isoformat(),
        ))
        connection.execute(source_moves.insert().values(
            operation_id=operation, ordinal=0, version_id=version["version_id"], from_path=str(path), to_path="",
            phase="prepared", checks_json=json_text(observed),
        ))
    connection.commit()
    return operations


def commit_archive_removal(connection: DBClient, operations: list[str]) -> None:
    for operation in operations:
        connection.execute(update(source_moves).where(source_moves.c.operation_id == operation).values(phase="removed"))
        connection.execute(update(source_transitions).where(source_transitions.c.operation_id == operation).values(outcome="committed"))
    connection.commit()


def reconcile_archive_removals(connection: DBClient) -> None:
    """Finish interrupted owner cleanup without attempting another deletion."""
    moves = connection.execute(select(source_moves).join(source_transitions).where(
        source_transitions.c.kind == "owner-cleanup",
        source_transitions.c.outcome.in_(("prepared", "unresolved")),
    )).mappings().all()
    for move in moves:
        try:
            Path(move["from_path"]).stat()
        except FileNotFoundError:
            outcome, phase = "committed", "removed"
        except OSError:
            continue
        else:
            outcome, phase = "rolled_back", "rolled_back"
        connection.execute(update(source_moves).where(
            source_moves.c.operation_id == move["operation_id"], source_moves.c.ordinal == move["ordinal"],
        ).values(phase=phase))
        connection.execute(update(source_transitions).where(
            source_transitions.c.operation_id == move["operation_id"],
        ).values(outcome=outcome))
    connection.commit()
