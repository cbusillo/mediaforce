from datetime import datetime
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine
from sqlalchemy.engine import Engine


def now_iso() -> str:
    return datetime.now().isoformat()


class AppSetting(SQLModel, table=True):  # type: ignore[misc,call-arg]
    id: int = Field(default=1, primary_key=True)
    global_max_height: Optional[int] = None
    max_concurrency: int = 1
    offpeak_enabled: bool = False
    offpeak_start: Optional[str] = "00:00"
    offpeak_end: Optional[str] = "05:00"
    transcode_root: Optional[str] = None


class Library(SQLModel, table=True):  # type: ignore[misc,call-arg]
    id: str = Field(primary_key=True)
    name: str
    media_type: str
    mac_path: str
    linux_path: str
    watch: bool = True
    max_height: Optional[int] = None
    weight: float = 1.0


class MediaItem(SQLModel, table=True):  # type: ignore[misc,call-arg]
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

    cooldown_until: Optional[str] = None

    scanned_at: Optional[str] = None
    updated_at: Optional[str] = None


class EncodeResult(SQLModel, table=True):  # type: ignore[misc,call-arg]
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
    promoted: bool = False
    promoted_at: Optional[str] = None
    promoted_path: Optional[str] = None
    source_backup_path: Optional[str] = None
    promote_manifest_json: Optional[str] = None
    is_outlier: bool = False
    outlier_reasons: Optional[str] = None
    review_status: str = "approved"
    reviewed_at: Optional[str] = None
    profile_eval_id: Optional[int] = Field(default=None, foreign_key="profile_evaluations.id")


class ProfileSettingsSource(SQLModel, table=True):  # type: ignore[misc,call-arg]
    __tablename__ = "profile_settings_sources"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    source_type: str = Field(default="remote")  # remote|local
    url: Optional[str] = None
    etag: Optional[str] = None
    checksum: Optional[str] = None
    payload: str  # JSON payload
    fetched_at: Optional[str] = None
    applied_at: Optional[str] = None
    is_active: bool = True


class ProfileEvaluation(SQLModel, table=True):  # type: ignore[misc,call-arg]
    __tablename__ = "profile_evaluations"

    id: Optional[int] = Field(default=None, primary_key=True)
    media_id: int = Field(foreign_key="media_inventory.id")
    encode_result_id: Optional[int] = Field(default=None, foreign_key="encode_results.id")
    settings_source_id: Optional[int] = Field(default=None, foreign_key="profile_settings_sources.id")
    selected_profile: str
    sample_strategy: str = "3x8s_motion"
    sample_count: int = 3
    sample_length: float = 8.0
    weighted_vmaf: Optional[float] = None
    median_vmaf: Optional[float] = None
    min_vmaf: Optional[float] = None
    max_vmaf: Optional[float] = None
    threshold_min: Optional[float] = None
    threshold_median: Optional[float] = None
    threshold_max: Optional[float] = None
    decision: str = "keep"  # keep|bump|fail|flagged
    status: str = "pending"  # pending|running|done|failed|flagged
    note: Optional[str] = None
    reason_json: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: Optional[str] = None


class VmafSample(SQLModel, table=True):  # type: ignore[misc,call-arg]
    __tablename__ = "vmaf_samples"

    id: Optional[int] = Field(default=None, primary_key=True)
    evaluation_id: int = Field(foreign_key="profile_evaluations.id")
    sample_kind: str = "auto"
    start_sec: float
    duration_sec: float
    vmaf: float
    weight: Optional[float] = None
    log_path: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


class ProfileChoiceFeedback(SQLModel, table=True):  # type: ignore[misc,call-arg]
    __tablename__ = "profile_choice_feedback"

    id: Optional[int] = Field(default=None, primary_key=True)
    evaluation_id: int = Field(foreign_key="profile_evaluations.id")
    decision: str  # good|bad
    reason_text: Optional[str] = None
    status: str = "queued"  # queued|processing|done
    flagged_at: str = Field(default_factory=now_iso)
    promoted_at: Optional[str] = None


class RetrainingCandidate(SQLModel, table=True):  # type: ignore[misc,call-arg]
    __tablename__ = "retraining_candidates"

    id: Optional[int] = Field(default=None, primary_key=True)
    evaluation_id: int = Field(foreign_key="profile_evaluations.id")
    media_id: int = Field(foreign_key="media_inventory.id")
    encode_result_id: Optional[int] = Field(default=None, foreign_key="encode_results.id")
    feedback_id: Optional[int] = Field(default=None, foreign_key="profile_choice_feedback.id")

    reason_text: Optional[str] = None
    status: str = "pending"  # pending|processing|done|dismissed
    created_at: str = Field(default_factory=now_iso)
    processed_at: Optional[str] = None
    notes: Optional[str] = None


class EncodeProgress(SQLModel, table=True):  # type: ignore[misc,call-arg]
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


class WorkerRegistry(SQLModel, table=True):  # type: ignore[misc,call-arg]
    __tablename__ = "worker_registry"

    machine: str = Field(primary_key=True)
    role: str = "encoder"  # encoder|watcher
    last_seen: str = Field(default_factory=now_iso)
    sample_path: Optional[str] = None
    status_message: Optional[str] = None


class WorkerControl(SQLModel, table=True):  # type: ignore[misc,call-arg]
    __tablename__ = "worker_control"

    key: str = Field(primary_key=True)
    mode: str = "run"  # run|drain|stop
    updated_at: str = Field(default_factory=now_iso)


class WorkerCommand(SQLModel, table=True):  # type: ignore[misc,call-arg]
    __tablename__ = "worker_commands"

    key: str = Field(primary_key=True)
    stop_now: bool = False
    requested_at: Optional[str] = None
    updated_at: str = Field(default_factory=now_iso)


class ShowOverride(SQLModel, table=True):  # type: ignore[misc,call-arg]
    __tablename__ = "show_overrides"

    show_name: str = Field(primary_key=True)
    default_tier: Optional[str] = None
    notes: Optional[str] = None
    max_height: Optional[int] = None
    updated_at: Optional[str] = None

def ensure_schema(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)

    # SQLite: SQLModel's create_all won't add columns; backfill schema changes here.
    with engine.begin() as conn:
        def _table_columns(table: str) -> set[str]:
            rows = list(conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall())
            columns: set[str] = set()
            for row in rows:
                name = None
                mapping = getattr(row, "_mapping", None)
                if mapping is not None:
                    name = getattr(mapping, "get", lambda _key, _default=None: None)("name")
                if name is None and isinstance(row, (tuple, list)) and len(row) > 1:
                    name = row[1]
                if isinstance(name, str) and name:
                    columns.add(name)
            return columns

        inventory_cols = _table_columns("media_inventory")
        if "cooldown_until" not in inventory_cols:
            conn.exec_driver_sql("ALTER TABLE media_inventory ADD COLUMN cooldown_until TEXT")

        appsetting_cols = _table_columns("appsetting")
        if "transcode_root" not in appsetting_cols:
            conn.exec_driver_sql("ALTER TABLE appsetting ADD COLUMN transcode_root TEXT")

        worker_cols = _table_columns("worker_registry")
        if "status_message" not in worker_cols:
            conn.exec_driver_sql("ALTER TABLE worker_registry ADD COLUMN status_message TEXT")


def init_engine(db_path: str):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    ensure_schema(engine)
    return engine


def new_session(engine) -> Session:
    return Session(engine)
