from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select
from sqlalchemy import text


class AppSetting(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    global_max_height: Optional[int] = None
    max_concurrency: int = 1
    offpeak_enabled: bool = False
    offpeak_start: Optional[str] = "00:00"
    offpeak_end: Optional[str] = "05:00"


class Library(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str
    media_type: str
    mac_path: str
    linux_path: str
    watch: bool = True
    max_height: Optional[int] = None
    weight: float = 1.0


class MediaItem(SQLModel, table=True):
    __tablename__ = "media_inventory"

    id: Optional[int] = Field(default=None, primary_key=True)
    path: str = Field(unique=True, index=True)
    library_id: Optional[str] = Field(default=None, foreign_key="library.id")

    size_bytes: Optional[int] = None
    mtime: Optional[int] = None
    duration_sec: Optional[float] = None

    video_codec: Optional[str] = None
    video_profile: Optional[str] = None
    resolution: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    bitrate_kbps: Optional[int] = None
    bit_depth: Optional[int] = None
    frame_rate: Optional[str] = None
    is_interlaced: bool = False
    is_hdr: bool = False
    hdr_format: Optional[str] = None

    audio_tracks: Optional[str] = None
    subtitle_tracks: Optional[str] = None

    detected_tier: Optional[str] = None
    tier_reasoning: Optional[str] = None
    is_av1: bool = False
    is_opus: bool = False

    estimated_target_bitrate_kbps: Optional[int] = None
    potential_savings_bytes: Optional[int] = None
    priority_score: Optional[float] = None
    manual_priority: int = 0

    status: str = "pending"
    skip_reason: Optional[str] = None

    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None

    scanned_at: Optional[str] = None
    updated_at: Optional[str] = None


class EncodeResult(SQLModel, table=True):
    __tablename__ = "encode_results"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="media_inventory.id")
    source_path: str

    tier: Optional[str] = None
    crf: Optional[int] = None
    preset: Optional[int] = None
    denoise: Optional[str] = None
    film_grain: Optional[int] = None
    audio_codec: Optional[str] = None
    audio_bitrate_kbps: Optional[int] = None

    output_path: Optional[str] = None
    output_size_bytes: Optional[int] = None
    output_bitrate_kbps: Optional[int] = None
    compression_ratio: Optional[float] = None

    psnr: Optional[float] = None
    ssim: Optional[float] = None
    vmaf: Optional[float] = None
    vmaf_sample_sec: Optional[float] = None

    machine: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    encode_speed: Optional[float] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    is_outlier: bool = False
    outlier_reasons: Optional[str] = None
    review_status: str = "approved"
    reviewed_at: Optional[str] = None
    promoted_at: Optional[str] = None


class EncodeProgress(SQLModel, table=True):
    __tablename__ = "encode_progress"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="media_inventory.id")
    source_path: str
    output_path: str
    machine: str
    tier: str
    started_at: str
    frame: int = 0
    total_frames: Optional[int] = None
    fps: float = 0.0
    speed: float = 0.0
    bitrate_kbps: Optional[float] = None
    size_bytes: int = 0
    time_encoded_sec: float = 0.0
    duration_sec: Optional[float] = None
    percent_complete: float = 0.0
    eta_seconds: Optional[int] = None
    phase: str = "encoding"
    phase_detail: Optional[str] = None
    updated_at: Optional[str] = None


class ShowOverride(SQLModel, table=True):
    __tablename__ = "show_overrides"

    show_name: str = Field(primary_key=True)
    default_tier: Optional[str] = None
    notes: Optional[str] = None
    max_height: Optional[int] = None
    updated_at: Optional[str] = None


def now_iso() -> str:
    return datetime.now().isoformat()


def _maybe_migrate_legacy_tables(engine) -> None:
    """Rename legacy table names to current ones if needed."""
    renames = [
        ("mediaitem", "media_inventory"),
        ("encoderesult", "encode_results"),
        ("encodeprogress", "encode_progress"),
        ("showoverride", "show_overrides"),
    ]
    with engine.connect() as conn:
        existing = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        for old, new in renames:
            if old in existing and new not in existing:
                conn.execute(text(f'ALTER TABLE "{old}" RENAME TO "{new}"'))
        conn.commit()


def init_engine(db_path: str):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    _maybe_migrate_legacy_tables(engine)
    SQLModel.metadata.create_all(engine)
    return engine


def new_session(engine) -> Session:
    return Session(engine)
