from __future__ import annotations

from typing import Optional, Callable

from sqlmodel import Session, delete

from mediaforce.db import EncodeProgress, WorkerRegistry, now_iso
from mediaforce.db import now_iso as default_now_iso


def start_progress_tracking(
    session: Session,
    source_id: int,
    source_path: str,
    output_path: str,
    machine: str,
    tier: str,
    duration_sec: float,
    total_frames: Optional[int] = None,
) -> int:
    now_str = now_iso()
    session.exec(delete(EncodeProgress).where(EncodeProgress.machine == machine))
    session.merge(WorkerRegistry(machine=machine, role="encoder", last_seen=now_str, sample_path=source_path))
    progress = EncodeProgress(
        source_id=source_id,
        source_path=source_path,
        output_path=output_path,
        machine=machine,
        tier=tier,
        started_at=now_str,
        duration_sec=duration_sec,
        total_frames=total_frames,
        phase="encoding",
        updated_at=now_str,
    )
    session.add(progress)
    session.commit()
    session.refresh(progress)
    return int(progress.id)  # type: ignore[arg-type]


def update_progress(
    session: Session,
    progress_id: int,
    frame: int = 0,
    fps: float = 0,
    speed: float = 0,
    bitrate_kbps: Optional[float] = None,
    size_bytes: int = 0,
    time_encoded_sec: float = 0,
    duration_sec: Optional[float] = None,
    phase: Optional[str] = None,
    phase_detail: Optional[str] = None,
) -> None:
    now_str = now_iso()

    percent_complete = 0.0
    eta_seconds: Optional[int] = None
    if duration_sec and duration_sec > 0 and time_encoded_sec > 0:
        percent_complete = min(100.0, (time_encoded_sec / duration_sec) * 100)
        if speed and speed > 0:
            remaining_sec = duration_sec - time_encoded_sec
            eta_seconds = int(remaining_sec / speed)

    progress = session.get(EncodeProgress, progress_id)
    if not progress:
        return

    session.merge(
        WorkerRegistry(
            machine=progress.machine,
            role="encoder",
            last_seen=now_str,
            sample_path=progress.source_path,
        )
    )

    progress.frame = frame
    progress.fps = fps
    progress.speed = speed
    progress.bitrate_kbps = bitrate_kbps
    progress.size_bytes = size_bytes
    progress.time_encoded_sec = time_encoded_sec
    progress.percent_complete = percent_complete
    progress.eta_seconds = eta_seconds
    if phase:
        progress.phase = phase
    if phase_detail:
        progress.phase_detail = phase_detail
    progress.updated_at = now_str
    session.add(progress)
    session.commit()


def upsert_heartbeat(
    session: Session,
    *,
    machine: str,
    sample_path: Optional[str] = None,
    now_iso: Callable[[], str] = default_now_iso,
) -> None:
    now_str = now_iso()
    session.merge(
        WorkerRegistry(
            machine=machine,
            role="encoder",
            last_seen=now_str,
            sample_path=sample_path,
        )
    )
    session.commit()


def finish_progress_tracking(
    session: Session,
    progress_id: int,
    success: bool,
    error_msg: Optional[str] = None,
) -> None:
    progress = session.get(EncodeProgress, progress_id)
    if progress:
        session.delete(progress)
        session.commit()
