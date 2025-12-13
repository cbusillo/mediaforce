from __future__ import annotations

from contextlib import contextmanager

from sqlmodel import SQLModel, Session, create_engine

from mediaforce.db import EncodeProgress, EncodeResult, MediaItem
from mediaforce.db.repository.encode import EncodeRepository
from mediaforce.db.repository.media import MediaRepository
from mediaforce.db.repository.progress import ProgressRepository


@contextmanager
def session_for_test(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
        session.commit()
    finally:
        session.close()


def test_media_repository_counts(tmp_path):
    with session_for_test(tmp_path) as session:
        session.add(MediaItem(path="/tmp/a.mkv", status="pending", detected_tier="good", scanned_at="2025-01-01T00:00:00"))
        session.add(MediaItem(path="/tmp/b.mkv", status="pending", detected_tier="poor", scanned_at="2025-01-02T00:00:00"))
        session.add(MediaItem(path="/tmp/c.mkv", status="encoded", detected_tier="good"))
        session.commit()

        repo = MediaRepository(session)
        counts = repo.count_by_status()
        assert counts["pending"] == 2
        assert counts["encoded"] == 1

        tiers = repo.pending_tier_counts()
        assert tiers["good"] == 1
        assert tiers["poor"] == 1


def test_media_repository_last_scan_ts(tmp_path):
    with session_for_test(tmp_path) as session:
        session.add(MediaItem(path="/tmp/a.mkv", status="pending", library_id="tv", scanned_at="2025-01-01T00:00:00"))
        session.add(MediaItem(path="/tmp/b.mkv", status="pending", library_id="movies", scanned_at="2025-01-03T00:00:00"))
        session.commit()

        repo = MediaRepository(session)
        assert repo.last_scan_ts(library_id="tv") == "2025-01-01T00:00:00"
        assert repo.last_scan_ts(library_id="movies") == "2025-01-03T00:00:00"


def test_encode_repository_space_saved_and_recent(tmp_path):
    with session_for_test(tmp_path) as session:
        item1 = MediaItem(path="/tmp/a.mkv", status="encoded", size_bytes=1000, detected_tier="good")
        item2 = MediaItem(path="/tmp/b.mkv", status="encoded", size_bytes=2000, detected_tier="poor")
        session.add(item1)
        session.add(item2)
        session.commit()
        session.refresh(item1)
        session.refresh(item2)
        assert item1.id is not None
        assert item2.id is not None

        session.add(
            EncodeResult(
                source_id=int(item1.id),
                source_path=item1.path,
                output_path="/tmp/a.AV1.mp4",
                output_size_bytes=500,
                completed_at="2025-01-02T00:00:00",
            )
        )
        session.add(
            EncodeResult(
                source_id=int(item2.id),
                source_path=item2.path,
                output_path="/tmp/b.AV1.mp4",
                output_size_bytes=1000,
                completed_at="2025-01-03T00:00:00",
            )
        )
        session.commit()

        repo = EncodeRepository(session)
        assert repo.space_saved_bytes() == 1500

        recent = repo.recent_completions(limit=10)
        assert len(recent) == 2
        # Newest first
        assert recent[0][0] == "/tmp/b.mkv"


def test_progress_repository_workers_and_active(tmp_path):
    with session_for_test(tmp_path) as session:
        item = MediaItem(path="/tmp/a.mkv", status="encoding", size_bytes=1000, video_codec="h264")
        session.add(item)
        session.commit()
        session.refresh(item)
        assert item.id is not None

        session.add(
            EncodeProgress(
                source_id=int(item.id),
                source_path=item.path,
                output_path="/tmp/a.AV1.mp4",
                machine="worker-1",
                tier="good",
                started_at="2025-01-01T00:00:00",
                percent_complete=12.5,
                updated_at="2025-01-01T00:01:00",
            )
        )
        session.commit()

        repo = ProgressRepository(session)
        workers = repo.list_workers()
        assert workers[0]["machine"] == "worker-1"
        assert workers[0]["active"] == 1

        active = repo.list_active()
        assert len(active) == 1
        prog, size_bytes, codec = active[0]
        assert prog.machine == "worker-1"
        assert size_bytes == 1000
        assert codec == "h264"

