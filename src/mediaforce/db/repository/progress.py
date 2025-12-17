from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select as _select  # type: ignore[reportMissingImports]

from mediaforce.db.models import EncodeProgress, MediaItem, WorkerRegistry

select: Any = _select


class ProgressRepository:
    def __init__(self, session: Session):
        self.session = session

    def cleanup_stale_progress(self, *, stale_seconds: int = 10 * 60) -> int:
        """Remove progress rows that have stopped updating.

        Workers update progress roughly every couple seconds while encoding.
        If we don't hear from them for a while, treat the row as stale so the
        UI doesn't show ghost encodes forever.
        """

        cutoff = (datetime.now() - timedelta(seconds=int(stale_seconds))).isoformat()
        updated_at: Any = EncodeProgress.updated_at
        started_at: Any = EncodeProgress.started_at

        rows = self.session.exec(select(EncodeProgress).where(
            (updated_at.is_not(None) & (updated_at < cutoff))  # type: ignore[operator]
            | (updated_at.is_(None) & (started_at < cutoff))  # type: ignore[operator]
        )).all()

        if not rows:
            return 0

        for row in rows:
            self.session.delete(row)
        self.session.commit()
        return len(rows)

    def list_workers(self, *, stale_seconds: int = 10 * 60, online_seconds: int = 90) -> list[dict]:
        workers: dict[str, dict] = {}

        self.cleanup_stale_progress(stale_seconds=stale_seconds)
        now = datetime.now()

        for row in self.session.exec(select(WorkerRegistry).order_by(WorkerRegistry.machine)).all():
            last_seen_dt: Optional[datetime]
            try:
                last_seen_dt = datetime.fromisoformat(str(row.last_seen))
            except Exception:
                last_seen_dt = None
            is_online = False
            if last_seen_dt is not None:
                is_online = (now - last_seen_dt).total_seconds() <= float(online_seconds)

            workers[str(row.machine)] = {
                "machine": row.machine,
                "active": 0,
                "percent_complete": 0,
                "tier": None,
                "sample_path": row.sample_path,
                "updated_at": row.last_seen,
                "role": row.role,
                "state": "waiting" if is_online else "offline",
            }

        machine: Any = EncodeProgress.machine
        updated_at: Any = EncodeProgress.updated_at
        percent_complete: Any = EncodeProgress.percent_complete
        tier: Any = EncodeProgress.tier
        source_path: Any = EncodeProgress.source_path

        cutoff = (now - timedelta(seconds=int(stale_seconds))).isoformat()
        rows = self.session.exec(
            select(
                machine,
                func.count().label("active"),
                func.max(updated_at).label("updated_at"),
                func.max(percent_complete).label("percent_complete"),
                func.max(tier).label("tier"),
                func.max(source_path).label("sample_path"),
            )
            .where((updated_at.is_not(None) & (updated_at >= cutoff)) | updated_at.is_(None))
            .group_by(machine)
            .order_by(machine.collate("NOCASE"))
        ).all()

        for row in rows:
            machine_name = str(row.machine)
            base = workers.get(machine_name) or {"machine": machine_name, "role": "encoder"}
            base.update({
                "active": row.active or 0,
                "percent_complete": row.percent_complete or 0,
                "tier": row.tier,
                "sample_path": row.sample_path or base.get("sample_path"),
                "updated_at": row.updated_at or base.get("updated_at"),
            })
            if (row.active or 0) > 0:
                base["state"] = "encoding"
            workers[machine_name] = base

        # If a worker claimed a job but hasn't started progress tracking yet,
        # show it as "starting" so the UI reflects reality.
        claimed_by: Any = MediaItem.claimed_by
        claimed_at: Any = MediaItem.claimed_at
        path: Any = MediaItem.path
        status: Any = MediaItem.status

        starting_rows = self.session.exec(
            select(
                claimed_by,
                func.count().label("active"),
                func.max(claimed_at).label("updated_at"),
                func.max(path).label("sample_path"),
            )
            .where(status == "encoding", claimed_by.is_not(None))
            .group_by(claimed_by)
        ).all()

        for row in starting_rows:
            machine_name = str(row.claimed_by or "")
            if not machine_name:
                continue
            base = workers.get(machine_name) or {"machine": machine_name, "role": "encoder"}
            if str(base.get("state") or "") == "encoding":
                continue
            base.update({
                "active": row.active or base.get("active") or 0,
                "percent_complete": base.get("percent_complete") or 0,
                "sample_path": row.sample_path or base.get("sample_path"),
                "updated_at": row.updated_at or base.get("updated_at"),
            })
            base["state"] = "starting"
            workers[machine_name] = base

        return sorted(workers.values(), key=lambda w: str(w.get("machine") or "").lower())

    def list_active(
        self,
        *,
        library_root: Optional[str] = None,
        stale_seconds: int = 10 * 60,
    ) -> list[tuple[EncodeProgress, Optional[int], Optional[str]]]:
        started_at: Any = EncodeProgress.started_at

        self.cleanup_stale_progress(stale_seconds=stale_seconds)
        cutoff = (datetime.now() - timedelta(seconds=int(stale_seconds))).isoformat()
        updated_at: Any = EncodeProgress.updated_at

        stmt = (
            select(EncodeProgress, MediaItem.size_bytes, MediaItem.video_codec)
            .select_from(EncodeProgress)
            .join(MediaItem, EncodeProgress.source_id == MediaItem.id, isouter=True)
            .where((updated_at.is_not(None) & (updated_at >= cutoff)) | updated_at.is_(None))
            .order_by(started_at.desc())
        )
        if library_root:
            stmt = stmt.where(EncodeProgress.source_path.like(f"{library_root}/%"))
        return self.session.exec(stmt).all()
