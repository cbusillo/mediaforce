"""Seed minimal data for UI smoke tests (review/completed/compare).

Creates tiny placeholder media/encoded files in /tmp/mediaforce and inserts
MediaItem + EncodeResult rows into ~/.config/mediaforce/mediaforce.db.
Safe to re-run; will append additional rows.
"""

from datetime import datetime
from pathlib import Path
import shutil
import subprocess

from sqlmodel import Session, create_engine
from sqlalchemy import text

from mediaforce.db.models import EncodeResult, MediaItem


def ensure_sample(base: Path) -> tuple[Path, Path]:
    """Create two tiny MP4s locally so hover/preview works offline."""

    base.mkdir(parents=True, exist_ok=True)
    sample = base / "sample.mp4"
    encoded = base / "sample-encoded.mp4"
    thumbnail = base / "sample.jpg"

    if not sample.exists():
        print("Generating 5s sample.mp4 with ffmpeg…")
        # 5‑second 320x180 color bars + tone; small enough for quick serve.
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "smptebars=size=320x180:rate=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=44100",
                "-t",
                "5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "veryfast",
                "-c:a",
                "aac",
                "-shortest",
                sample,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if not encoded.exists():
        shutil.copy(sample, encoded)

    if not thumbnail.exists():
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                sample,
                "-vframes",
                "1",
                "-q:v",
                "2",
                thumbnail,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    return sample, encoded


def main() -> None:
    base = Path("/tmp/mediaforce")
    sample, encoded = ensure_sample(base)

    db = Path.home() / ".config" / "mediaforce" / "mediaforce.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    now = datetime.now().isoformat()

    with Session(engine) as session:
        # Clear previous sample rows so the script is idempotent.
        # Remove prior seeded rows for idempotency
        session.exec(
            text(
                "DELETE FROM encode_results WHERE source_path IN (:sample, :encoded) "
                "OR output_path IN (:sample, :encoded)"
            ).bindparams(sample=str(sample), encoded=str(encoded))
        )
        session.exec(
            text("DELETE FROM media_inventory WHERE path IN (:sample, :encoded)").bindparams(
                sample=str(sample), encoded=str(encoded)
            )
        )
        session.commit()

        # Pending encode (review/compare)
        m1 = MediaItem(
            path=str(sample),
            status="encoded",
            size_bytes=sample.stat().st_size,
            detected_tier="good",
            bitrate_kbps=3500,
            duration_sec=10,
            scanned_at=now,
        )
        session.add(m1)
        session.flush()

        e1 = EncodeResult(
            source_id=m1.id,
            source_path=m1.path,
            output_path=str(encoded),
            output_size_bytes=encoded.stat().st_size,
            output_bitrate_kbps=1800,
            tier="good",
            crf=26,
            preset=5,
            vmaf=95.0,
            ssim=0.98,
            completed_at=now,
            compression_ratio=0.5,
        )
        session.add(e1)

        # Completed encode
        m2 = MediaItem(
            path=str(encoded),
            status="completed",
            size_bytes=encoded.stat().st_size,
            detected_tier="pristine",
            bitrate_kbps=3500,
            duration_sec=10,
            scanned_at=now,
        )
        session.add(m2)
        session.flush()

        e2 = EncodeResult(
            source_id=m2.id,
            source_path=m2.path,
            output_path=str(encoded),
            output_size_bytes=encoded.stat().st_size,
            output_bitrate_kbps=1800,
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

    print("Seeded sample rows with playable media.")


if __name__ == "__main__":
    main()
