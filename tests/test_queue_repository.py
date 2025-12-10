from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from mediaforce.db.models import MediaItem
from mediaforce.db.repository.queue import QueueRepository


def _make_engine():
    # In-memory SQLite is sufficient for repository SQL.
    return create_engine("sqlite://", connect_args={"check_same_thread": False})


def _seed(session: Session, library_root: str):
    items = [
        MediaItem(
            path=f"{library_root}/ShowA/Season 1/E01.mkv",
            status="pending",
            size_bytes=1_000_000,
            potential_savings_bytes=500_000,
            priority_score=10,
            detected_tier="mediocre",
        ),
        MediaItem(
            path=f"{library_root}/ShowA/Season 1/E02.mkv",
            status="pending",
            size_bytes=2_000_000,
            potential_savings_bytes=1_000_000,
            priority_score=20,
            detected_tier="mediocre",
        ),
        MediaItem(
            path=f"{library_root}/ShowB/Season 1/E01.mkv",
            status="pending",
            size_bytes=500_000,
            potential_savings_bytes=100_000,
            priority_score=5,
            detected_tier="good",
        ),
        # Non-pending should be ignored
        MediaItem(
            path=f"{library_root}/ShowC/Season 1/E01.mkv",
            status="encoded",
            size_bytes=5_000_000,
            potential_savings_bytes=2_000_000,
            priority_score=50,
            detected_tier="good",
        ),
    ]
    session.add_all(items)
    session.commit()


def test_list_shows_filters_and_pagination():
    library_root = "/library"
    engine = _make_engine()
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        _seed(session, library_root)
        repo = QueueRepository(session)

        shows, total, total_files, total_savings = repo.list_shows(
            library_root=library_root,
            show_filter=None,
            tier_filter=None,
            size_min=None,
            size_max=None,
            per_page=2,
            page=1,
            sort="priority",
            direction="desc",
        )

        assert total == 2  # ShowA and ShowB
        assert total_files == 3
        assert total_savings == 1_600_000
        assert len(shows) == 2
        assert {s["show_name"] for s in shows} == {"ShowA", "ShowB"}

        # Apply show filter
        shows_filtered, total_filt, total_files_filt, savings_filt = repo.list_shows(
            library_root=library_root,
            show_filter="ShowA",
            tier_filter=None,
            size_min=None,
            size_max=None,
            per_page=10,
            page=1,
            sort="priority",
            direction="desc",
        )
        assert total_filt == 1
        assert total_files_filt == 2
        assert savings_filt == 1_500_000
        assert shows_filtered[0]["show_name"] == "ShowA"

        # Apply size filter to drop ShowB
        shows_size, total_size, total_files_size, _ = repo.list_shows(
            library_root=library_root,
            show_filter=None,
            tier_filter=None,
            size_min=800_000,
            size_max=None,
            per_page=10,
            page=1,
            sort="priority",
            direction="desc",
        )
        assert total_size == 1
        assert total_files_size == 2
        assert shows_size[0]["show_name"] == "ShowA"
