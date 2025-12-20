from datetime import datetime, timedelta
from typing import Any, Optional, cast

from sqlalchemy import func
from sqlmodel import Session, select as _select  # type: ignore[reportMissingImports]

from mediaforce.db.models import EncodeProgress, MediaItem, WorkerRegistry

select: Any = _select


class ProgressRepository:
    def __init__(self, session: Session):
        self.session = session

    def cleanup_stale_progress(self, *, stale_seconds: int = 10 * 60) -> int:
        cutoff = (datetime.now() - timedelta(seconds=int(stale_seconds))).isoformat()
        progress_cols = cast(Any, EncodeProgress.__table__.c)
        updated_at: Any = progress_cols.updated_at
        started_at: Any = progress_cols.started_at

        rows = self.session.exec(
            select(EncodeProgress).where(
                (updated_at.is_not(None) & (updated_at < cutoff))
                | (updated_at.is_(None) & (started_at < cutoff))
            )
        ).all()

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

        worker_cols = cast(Any, WorkerRegistry.__table__.c)
        registry_rows: list[WorkerRegistry] = self.session.exec(
            select(WorkerRegistry).order_by(worker_cols.machine)
        ).all()
        for row in registry_rows:
            last_seen_dt: Optional[datetime]
            try:
                last_seen_dt = datetime.fromisoformat(str(row.last_seen))
            except (TypeError, ValueError):
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
                "status_message": getattr(row, "status_message", None),
                "updated_at": row.last_seen,
                "role": row.role,
                "state": "waiting" if is_online else "offline",
            }

        progress_cols = cast(Any, EncodeProgress.__table__.c)
        machine: Any = progress_cols.machine
        updated_at: Any = progress_cols.updated_at
        percent_complete: Any = progress_cols.percent_complete
        tier: Any = progress_cols.tier
        source_path: Any = progress_cols.source_path

        cutoff = (now - timedelta(seconds=int(stale_seconds))).isoformat()
        rows = self.session.exec(
            select(
                machine.label("machine"),
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

        for (
            machine_val,
            active_val,
            updated_at_val,
            percent_complete_val,
            tier_val,
            sample_path_val,
        ) in rows:
            machine_name = str(machine_val or "")
            if not machine_name:
                continue
            base = workers.get(machine_name) or {"machine": machine_name, "role": "encoder"}
            base.update(
                {
                    "active": active_val or 0,
                    "percent_complete": percent_complete_val or 0,
                    "tier": tier_val,
                    "sample_path": sample_path_val or base.get("sample_path"),
                    "updated_at": updated_at_val or base.get("updated_at"),
                }
            )
            if (active_val or 0) > 0:
                base["state"] = "encoding"
            workers[machine_name] = base

        # If a worker claimed a job but hasn't started progress tracking yet,
        # show it as "starting" so the UI reflects reality.
        media_cols = cast(Any, MediaItem.__table__.c)
        claimed_by: Any = media_cols.claimed_by
        claimed_at: Any = media_cols.claimed_at
        path: Any = media_cols.path
        status: Any = media_cols.status

        starting_rows = self.session.exec(
            select(
                claimed_by.label("machine"),
                func.count().label("active"),
                func.max(claimed_at).label("updated_at"),
                func.max(path).label("sample_path"),
            )
            .where(status == "encoding", claimed_by.is_not(None))
            .group_by(claimed_by)
        ).all()

        for machine_val, active_val, updated_at_val, sample_path_val in starting_rows:
            machine_name = str(machine_val or "")
            if not machine_name:
                continue
            base = workers.get(machine_name) or {"machine": machine_name, "role": "encoder"}
            if str(base.get("state") or "") == "encoding":
                continue
            base.update(
                {
                    "active": active_val or base.get("active") or 0,
                    "percent_complete": base.get("percent_complete") or 0,
                    "sample_path": sample_path_val or base.get("sample_path"),
                    "updated_at": updated_at_val or base.get("updated_at"),
                }
            )
            base["state"] = "starting"
            workers[machine_name] = base

        return sorted(workers.values(), key=lambda w: str(w.get("machine") or "").lower())

    def list_active(
        self,
        *,
        library_root: Optional[str] = None,
        stale_seconds: int = 10 * 60,
    ) -> list[tuple[EncodeProgress, Optional[int], Optional[str]]]:
        progress_cols = cast(Any, EncodeProgress.__table__.c)
        media_cols = cast(Any, MediaItem.__table__.c)

        started_at: Any = progress_cols.started_at

        self.cleanup_stale_progress(stale_seconds=stale_seconds)
        cutoff = (datetime.now() - timedelta(seconds=int(stale_seconds))).isoformat()
        updated_at: Any = progress_cols.updated_at

        stmt = (
            select(EncodeProgress, media_cols.size_bytes, media_cols.video_codec)
            .select_from(EncodeProgress)
            .join(MediaItem, progress_cols.source_id == media_cols.id, isouter=True)
            .where((updated_at.is_not(None) & (updated_at >= cutoff)) | updated_at.is_(None))
            .order_by(started_at.desc())
        )
        if library_root:
            stmt = stmt.where(progress_cols.source_path.like(f"{library_root}/%"))
        return self.session.exec(stmt).all()
