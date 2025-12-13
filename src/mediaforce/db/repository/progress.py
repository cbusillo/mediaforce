from __future__ import annotations

from typing import Any
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select as _select  # type: ignore[reportMissingImports]

from mediaforce.db.models import EncodeProgress, MediaItem, WorkerRegistry

select: Any = _select


class ProgressRepository:
    def __init__(self, session: Session):
        self.session = session

    def list_workers(self) -> list[dict]:
        workers: dict[str, dict] = {}

        for row in self.session.exec(select(WorkerRegistry).order_by(WorkerRegistry.machine)).all():
            workers[str(row.machine)] = {
                "machine": row.machine,
                "active": 0,
                "percent_complete": 0,
                "tier": None,
                "sample_path": row.sample_path,
                "updated_at": row.last_seen,
                "role": row.role,
            }

        machine: Any = EncodeProgress.machine
        updated_at: Any = EncodeProgress.updated_at
        percent_complete: Any = EncodeProgress.percent_complete
        tier: Any = EncodeProgress.tier
        source_path: Any = EncodeProgress.source_path

        rows = self.session.exec(
            select(
                machine,
                func.count().label("active"),
                func.max(updated_at).label("updated_at"),
                func.max(percent_complete).label("percent_complete"),
                func.max(tier).label("tier"),
                func.max(source_path).label("sample_path"),
            )
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
            workers[machine_name] = base

        return sorted(workers.values(), key=lambda w: str(w.get("machine") or "").lower())

    def list_active(self) -> list[tuple[EncodeProgress, Optional[int], Optional[str]]]:
        started_at: Any = EncodeProgress.started_at
        return (
            self.session.exec(
                select(EncodeProgress, MediaItem.size_bytes, MediaItem.video_codec)
                .select_from(EncodeProgress)
                .join(MediaItem, EncodeProgress.source_id == MediaItem.id, isouter=True)
                .order_by(started_at.desc())
            ).all()
        )
