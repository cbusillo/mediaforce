from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from sqlmodel import SQLModel, Session, create_engine

import mediaforce.core as core
from mediaforce.db import EncodeResult, MediaItem


def make_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_purge_backups_dry_run_does_not_delete(tmp_path, monkeypatch):
    session = make_session()

    promoted = tmp_path / "Episode.AV1.mp4"
    promoted.write_bytes(b"encoded")

    backup = tmp_path / ".Episode.mkv.mediaforce-orig-deadbeef01"
    backup.write_bytes(b"original")

    source = tmp_path / "Episode.mkv"
    promoted_at = (datetime.now() - timedelta(days=40)).isoformat()

    item = MediaItem(path=str(promoted), status="completed")
    session.add(item)
    session.commit()
    session.refresh(item)

    enc = EncodeResult(
        source_id=item.id,
        source_path=str(source),
        promoted=True,
        promoted_at=promoted_at,
        promoted_path=str(promoted),
        source_backup_path=str(backup),
        promote_manifest_json="{}",
    )
    session.add(enc)
    session.commit()

    db_path = tmp_path / "inventory.db"
    db_path.touch()
    monkeypatch.setattr(core, "get_db_path", lambda _=None: db_path)
    monkeypatch.setattr(core, "init_db", lambda _: session)

    args = argparse.Namespace(older_than_days=30, limit=0, apply=False)
    assert core.cmd_purge_backups(args) == 0
    assert backup.exists()


def test_purge_backups_apply_deletes(tmp_path, monkeypatch):
    session = make_session()

    promoted = tmp_path / "Episode.AV1.mp4"
    promoted.write_bytes(b"encoded")

    backup = tmp_path / ".Episode.mkv.mediaforce-orig-deadbeef02"
    backup.write_bytes(b"original")

    source = tmp_path / "Episode.mkv"
    promoted_at = (datetime.now() - timedelta(days=40)).isoformat()

    item = MediaItem(path=str(promoted), status="completed")
    session.add(item)
    session.commit()
    session.refresh(item)

    enc = EncodeResult(
        source_id=item.id,
        source_path=str(source),
        promoted=True,
        promoted_at=promoted_at,
        promoted_path=str(promoted),
        source_backup_path=str(backup),
        promote_manifest_json="{}",
    )
    session.add(enc)
    session.commit()

    db_path = tmp_path / "inventory.db"
    db_path.touch()
    monkeypatch.setattr(core, "get_db_path", lambda _=None: db_path)
    monkeypatch.setattr(core, "init_db", lambda _: session)

    args = argparse.Namespace(older_than_days=30, limit=0, apply=True)
    assert core.cmd_purge_backups(args) == 0
    assert not backup.exists()
    assert promoted.exists()


def test_purge_backups_skips_when_promoted_missing(tmp_path, monkeypatch):
    session = make_session()

    promoted = tmp_path / "Episode.AV1.mp4"
    backup = tmp_path / ".Episode.mkv.mediaforce-orig-deadbeef03"
    backup.write_bytes(b"original")

    source = tmp_path / "Episode.mkv"
    promoted_at = (datetime.now() - timedelta(days=40)).isoformat()

    item = MediaItem(path=str(promoted), status="completed")
    session.add(item)
    session.commit()
    session.refresh(item)

    enc = EncodeResult(
        source_id=item.id,
        source_path=str(source),
        promoted=True,
        promoted_at=promoted_at,
        promoted_path=str(promoted),
        source_backup_path=str(backup),
        promote_manifest_json="{}",
    )
    session.add(enc)
    session.commit()

    db_path = tmp_path / "inventory.db"
    db_path.touch()
    monkeypatch.setattr(core, "get_db_path", lambda _=None: db_path)
    monkeypatch.setattr(core, "init_db", lambda _: session)

    args = argparse.Namespace(older_than_days=30, limit=0, apply=True)
    assert core.cmd_purge_backups(args) == 0
    assert backup.exists()

