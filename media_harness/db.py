from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SQLITE_BUSY_TIMEOUT_MS = 30_000


SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_runs (
    scan_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    roots_json TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'unknown',
    prefixes_json TEXT,
    file_count INTEGER NOT NULL DEFAULT 0,
    reprobed_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS library_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL UNIQUE,
    rel_path TEXT NOT NULL,
    media_root TEXT NOT NULL,
    parent_dir TEXT NOT NULL,
    file_name TEXT NOT NULL,
    container TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    duration_seconds REAL,
    video_codec TEXT,
    video_bitrate INTEGER,
    width INTEGER,
    height INTEGER,
    pix_fmt TEXT,
    audio_track_count INTEGER NOT NULL DEFAULT 0,
    subtitle_track_count INTEGER NOT NULL DEFAULT 0,
    english_audio_count INTEGER NOT NULL DEFAULT 0,
    english_subtitle_count INTEGER NOT NULL DEFAULT 0,
    default_audio_language TEXT,
    default_subtitle_language TEXT,
    audio_summary_json TEXT NOT NULL,
    subtitle_summary_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'discovered',
    priority_score REAL NOT NULL DEFAULT 0,
    recommendation TEXT,
    recommendation_reason TEXT,
    last_scan_id TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_library_items_rel_path
    ON library_items(rel_path);

CREATE INDEX IF NOT EXISTS idx_library_items_status_score
    ON library_items(status, priority_score DESC);

CREATE TABLE IF NOT EXISTS run_manifests (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    output_path TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    item_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS calibration_jobs (
    job_id TEXT PRIMARY KEY,
    prefix TEXT NOT NULL,
    status TEXT NOT NULL,
    lane TEXT NOT NULL,
    action TEXT NOT NULL,
    host_json TEXT NOT NULL,
    notes TEXT,
    policy_json TEXT NOT NULL,
    sample_item_json TEXT NOT NULL,
    seed_source TEXT,
    seed_summary TEXT,
    seed_prompt_version TEXT,
    seed_raw_response TEXT,
    seed_proposed_policy_json TEXT,
    seed_applied_policy_json TEXT,
    result_json TEXT,
    error TEXT,
    owner_pid INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calibration_jobs_prefix_created
    ON calibration_jobs(prefix, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_calibration_jobs_lane_status_created
    ON calibration_jobs(lane, status, created_at ASC);

CREATE TABLE IF NOT EXISTS encode_queue_state (
    queue_name TEXT PRIMARY KEY,
    is_paused INTEGER NOT NULL DEFAULT 0,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    active_job_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS encode_jobs (
    job_id TEXT PRIMARY KEY,
    prefix TEXT NOT NULL,
    status TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    saved_profile_path TEXT,
    host_json TEXT NOT NULL,
    last_host_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    process_pid INTEGER,
    error TEXT,
    bypass_schedule INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    leased_at TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    worker_id TEXT,
    retry_not_before TEXT,
    waiting_reason TEXT,
    terminal_reason TEXT,
    last_failure_kind TEXT,
    last_failure_at TEXT,
    host_cooldown_until TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_encode_jobs_status_created
    ON encode_jobs(status, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_encode_jobs_prefix_created
    ON encode_jobs(prefix, created_at DESC);

CREATE TABLE IF NOT EXISTS tuning_sessions (
    session_id TEXT PRIMARY KEY,
    prefix TEXT NOT NULL,
    note TEXT NOT NULL,
    summary TEXT,
    diagnosis TEXT,
    confidence TEXT,
    evidence_checked_json TEXT NOT NULL DEFAULT '[]',
    suggested_follow_up TEXT,
    prompt_version TEXT,
    proposed_policy_json TEXT,
    applied_policy_json TEXT,
    toolbelt_json TEXT NOT NULL DEFAULT '{}',
    self_check_json TEXT,
    raw_response TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tuning_sessions_prefix_created
    ON tuning_sessions(prefix, created_at DESC);

CREATE TABLE IF NOT EXISTS learning_artifacts (
    artifact_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    prefix TEXT NOT NULL,
    title TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    summary TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES tuning_sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_learning_artifacts_prefix_updated
    ON learning_artifacts(prefix, updated_at DESC);

CREATE TABLE IF NOT EXISTS staged_artifacts (
    library_item_id INTEGER PRIMARY KEY,
    manifest_run_id TEXT,
    manifest_path TEXT,
    item_index INTEGER,
    source_fingerprint TEXT,
    staging_path TEXT NOT NULL,
    staging_size_bytes INTEGER,
    staging_mtime_ns INTEGER,
    staging_fingerprint TEXT,
    chosen_crf REAL,
    quality_metric TEXT,
    quality_target REAL,
    quality_score REAL,
    encode_command_json TEXT,
    audio_summary_json TEXT,
    subtitle_summary_json TEXT,
    validation_json TEXT,
    staged_at TEXT,
    validated_at TEXT,
    promoted_at TEXT,
    promoted_path TEXT,
    archived_source_path TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(library_item_id) REFERENCES library_items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS item_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_item_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    FOREIGN KEY(library_item_id) REFERENCES library_items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_item_events_item_time
    ON item_events(library_item_id, created_at DESC);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("PRAGMA foreign_keys=ON;")
    connection.executescript(SCHEMA)
    _ensure_scan_run_columns(connection)
    _ensure_encode_queue_columns(connection)
    return connection


def _ensure_scan_run_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(scan_runs)").fetchall()
    }
    if "scope" not in existing:
        connection.execute("ALTER TABLE scan_runs ADD COLUMN scope TEXT NOT NULL DEFAULT 'unknown'")
        connection.execute("UPDATE scan_runs SET scope = 'unknown' WHERE scope IS NULL OR scope = 'full'")
    if "prefixes_json" not in existing:
        connection.execute("ALTER TABLE scan_runs ADD COLUMN prefixes_json TEXT")


def _ensure_encode_queue_columns(connection: sqlite3.Connection) -> None:
    encode_job_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(encode_jobs)").fetchall()
    }
    column_defaults = {
        "bypass_schedule": "INTEGER NOT NULL DEFAULT 0",
        "last_host_json": "TEXT NOT NULL DEFAULT '{}'",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "leased_at": "TEXT",
        "lease_expires_at": "TEXT",
        "heartbeat_at": "TEXT",
        "worker_id": "TEXT",
        "retry_not_before": "TEXT",
        "waiting_reason": "TEXT",
        "terminal_reason": "TEXT",
        "last_failure_kind": "TEXT",
        "last_failure_at": "TEXT",
        "host_cooldown_until": "TEXT",
    }
    for column_name, column_definition in column_defaults.items():
        if column_name in encode_job_columns:
            continue
        connection.execute(
            f"ALTER TABLE encode_jobs ADD COLUMN {column_name} {column_definition}"
        )


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()
