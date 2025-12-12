from __future__ import annotations

import argparse
import pathlib

from sqlmodel import SQLModel, Session, create_engine

import mediaforce.core as core
from mediaforce.db import EncodeResult, MediaItem
from mediaforce.services.promote import ProbeSummary


def test_cmd_promote_rolls_back_on_db_commit_failure(tmp_path: pathlib.Path, monkeypatch):
    media_root = tmp_path / "media"
    media_root.mkdir()
    library_path = media_root / "tv"
    source_dir = library_path / "Show" / "Season 1"
    source_dir.mkdir(parents=True)

    source_file = source_dir / "Episode.mkv"
    source_file.write_bytes(b"source" * 1024)
    sidecar = source_dir / "Episode.srt"
    sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n")

    transcode_root = tmp_path / "transcode"
    encoded_dir = transcode_root / "tv" / "Show" / "Season 1"
    encoded_dir.mkdir(parents=True)
    encoded_file = encoded_dir / "Episode.AV1.mp4"
    encoded_file.write_bytes(b"encoded" * (1024 * 1024 // 6 + 10))

    def fake_probe(path: pathlib.Path):
        if path == source_file:
            return ProbeSummary(
                path=path,
                duration_seconds=120.0,
                video_codec="h264",
                width=1920,
                height=1080,
                audio_streams=1,
            )
        return ProbeSummary(
            path=path,
            duration_seconds=120.0,
            video_codec="av1",
            width=1920,
            height=1080,
            audio_streams=1,
        )

    import mediaforce.services.promote as promote

    monkeypatch.setattr(promote, "probe_with_ffprobe", fake_probe)

    monkeypatch.setattr(core, "get_media_roots", lambda: [str(media_root)])

    db_path = tmp_path / "inventory.db"
    db_path.touch()
    monkeypatch.setattr(core, "get_db_path", lambda _=None: db_path)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    item = MediaItem(path=str(source_file), status="pending")
    session.add(item)
    session.commit()
    session.refresh(item)
    enc = EncodeResult(source_id=item.id, source_path=str(source_file))
    session.add(enc)
    session.commit()
    item_id = item.id
    enc_id = enc.id

    def fail_commit() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", fail_commit)
    monkeypatch.setattr(core, "init_db", lambda _: session)

    args = argparse.Namespace(
        path=str(library_path),
        transcode_root=str(transcode_root),
        dry_run=False,
        delete_original=True,
    )

    result = core.cmd_promote(args)

    # DB commit failed, so promote must rollback filesystem changes.
    assert result == 1
    assert source_file.exists()
    assert sidecar.exists()
    assert encoded_file.exists()

    promoted_path = source_dir / "Episode.AV1.mp4"
    assert not promoted_path.exists()

    with Session(engine) as check_session:
        db_item = check_session.get(MediaItem, item_id)
        assert db_item is not None
        assert db_item.status == "pending"
        assert db_item.path == str(source_file)

        db_enc = check_session.get(EncodeResult, enc_id)
        assert db_enc is not None
        assert not db_enc.promoted
        assert db_enc.promoted_at is None
