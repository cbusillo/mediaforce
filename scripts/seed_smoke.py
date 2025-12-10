"""Seed minimal data for UI smoke tests (review/completed/compare).

Creates tiny placeholder media/encoded files in /tmp/mediaforce and inserts
MediaItem + EncodeResult rows into ~/.config/mediaforce/mediaforce.db.
Safe to re-run; will append additional rows.
"""

from datetime import datetime
from pathlib import Path

from sqlmodel import Session, create_engine

from mediaforce.db.models import EncodeResult, MediaItem


def main() -> None:
    base = Path("/tmp/mediaforce")
    base.mkdir(parents=True, exist_ok=True)
    for name in ["source1.mkv", "encoded1.mp4", "source2.mkv", "encoded2.mp4"]:
        p = base / name
        if not p.exists():
            p.write_bytes(b"0000")

    db = Path.home() / ".config" / "mediaforce" / "mediaforce.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    now = datetime.now().isoformat()

    with Session(engine) as session:
        m1 = MediaItem(
            path=str(base / "source1.mkv"),
            status="encoded",
            size_bytes=10_000_000,
            detected_tier="good",
            bitrate_kbps=8000,
            duration_sec=1200,
            scanned_at=now,
        )
        session.add(m1)
        session.flush()

        e1 = EncodeResult(
            source_id=m1.id,
            source_path=m1.path,
            output_path=str(base / "encoded1.mp4"),
            output_size_bytes=5_000_000,
            output_bitrate_kbps=4000,
            tier="good",
            crf=26,
            preset=5,
            vmaf=95.0,
            ssim=0.98,
            completed_at=now,
            compression_ratio=0.5,
        )
        session.add(e1)

        m2 = MediaItem(
            path=str(base / "source2.mkv"),
            status="completed",
            size_bytes=12_000_000,
            detected_tier="pristine",
            bitrate_kbps=9000,
            duration_sec=1500,
            scanned_at=now,
        )
        session.add(m2)
        session.flush()

        e2 = EncodeResult(
            source_id=m2.id,
            source_path=m2.path,
            output_path=str(base / "encoded2.mp4"),
            output_size_bytes=6_000_000,
            output_bitrate_kbps=4500,
            tier="pristine",
            crf=24,
            preset=5,
            vmaf=92.0,
            ssim=0.97,
            completed_at=now,
            promoted_at=now,
            compression_ratio=0.5,
        )
        session.add(e2)

        session.commit()

    print("Seeded sample rows.")


if __name__ == "__main__":
    main()
