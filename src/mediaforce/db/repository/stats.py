from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func
from sqlmodel import Session, select as _select  # type: ignore[reportMissingImports]

from mediaforce.db.models import EncodeResult, MediaItem

select: Any = _select


@dataclass(slots=True)
class DailyEncodeStats:
    day: date
    encodes: int
    source_bytes: int
    output_bytes: int
    saved_bytes: int
    avg_reduction: Optional[float]
    avg_speed: Optional[float]


@dataclass(slots=True)
class TierEncodeStats:
    tier: str
    encodes: int
    saved_bytes: int
    avg_reduction: Optional[float]


@dataclass(slots=True)
class EncodeTotals:
    encodes: int
    source_bytes: int
    output_bytes: int
    saved_bytes: int
    avg_reduction: Optional[float]
    avg_speed: Optional[float]


class StatsRepository:
    def __init__(self, session: Session):
        self.session = session

    def totals(self, *, since: Optional[datetime] = None) -> EncodeTotals:
        output_size: Any = EncodeResult.output_size_bytes
        size_bytes: Any = MediaItem.size_bytes
        completed_at: Any = EncodeResult.completed_at

        where = [
            output_size.is_not(None),
            output_size > 0,
            size_bytes.is_not(None),
            size_bytes > 0,
        ]
        if since is not None:
            where.append(completed_at.is_not(None))
            where.append(completed_at >= since.isoformat())

        size_den = func.nullif(size_bytes, 0)
        reduction_expr = (size_bytes - output_size) * 1.0 / size_den

        stmt: Any = (
            select(
                func.count(EncodeResult.id),
                func.coalesce(func.sum(size_bytes), 0),
                func.coalesce(func.sum(output_size), 0),
                func.avg(reduction_expr),
                func.avg(EncodeResult.encode_speed),
            )
            .join(MediaItem, EncodeResult.source_id == MediaItem.id)
            .where(*where)
        )

        row = self.session.exec(stmt).one()

        encodes = int(row[0] or 0)
        source_bytes = int(row[1] or 0)
        output_bytes = int(row[2] or 0)
        saved_bytes = source_bytes - output_bytes
        avg_reduction = float(row[3]) if row[3] is not None else None
        avg_speed = float(row[4]) if row[4] is not None else None

        return EncodeTotals(
            encodes=encodes,
            source_bytes=source_bytes,
            output_bytes=output_bytes,
            saved_bytes=saved_bytes,
            avg_reduction=avg_reduction,
            avg_speed=avg_speed,
        )

    def daily(self, *, days: int) -> list[DailyEncodeStats]:
        end_day = date.today()
        start_day = end_day - timedelta(days=days - 1)
        since = datetime.combine(start_day, datetime.min.time())

        output_size: Any = EncodeResult.output_size_bytes
        size_bytes: Any = MediaItem.size_bytes
        completed_at: Any = EncodeResult.completed_at

        day_key = func.substr(completed_at, 1, 10)
        size_den = func.nullif(size_bytes, 0)
        reduction_expr = (size_bytes - output_size) * 1.0 / size_den

        stmt: Any = (
            select(
                day_key.label("day"),
                func.count(EncodeResult.id).label("encodes"),
                func.coalesce(func.sum(size_bytes), 0).label("source_bytes"),
                func.coalesce(func.sum(output_size), 0).label("output_bytes"),
                func.avg(reduction_expr).label("avg_reduction"),
                func.avg(EncodeResult.encode_speed).label("avg_speed"),
            )
            .join(MediaItem, EncodeResult.source_id == MediaItem.id)
            .where(
                completed_at.is_not(None),
                completed_at >= since.isoformat(),
                output_size.is_not(None),
                output_size > 0,
                size_bytes.is_not(None),
                size_bytes > 0,
            )
            .group_by(day_key)
            .order_by(day_key)
        )

        rows = self.session.exec(stmt).all()

        daily_by_key: dict[str, DailyEncodeStats] = {}
        for row in rows:
            day = date.fromisoformat(str(row[0]))
            encodes = int(row[1] or 0)
            source_bytes = int(row[2] or 0)
            output_bytes = int(row[3] or 0)
            saved_bytes = source_bytes - output_bytes
            avg_reduction = float(row[4]) if row[4] is not None else None
            avg_speed = float(row[5]) if row[5] is not None else None
            daily_by_key[day.isoformat()] = DailyEncodeStats(
                day=day,
                encodes=encodes,
                source_bytes=source_bytes,
                output_bytes=output_bytes,
                saved_bytes=saved_bytes,
                avg_reduction=avg_reduction,
                avg_speed=avg_speed,
            )

        results: list[DailyEncodeStats] = []
        cursor = start_day
        while cursor <= end_day:
            key = cursor.isoformat()
            if key in daily_by_key:
                results.append(daily_by_key[key])
            else:
                results.append(
                    DailyEncodeStats(
                        day=cursor,
                        encodes=0,
                        source_bytes=0,
                        output_bytes=0,
                        saved_bytes=0,
                        avg_reduction=None,
                        avg_speed=None,
                    )
                )
            cursor += timedelta(days=1)
        return results

    def reduction_by_tier(self, *, since: Optional[datetime] = None) -> list[TierEncodeStats]:
        output_size: Any = EncodeResult.output_size_bytes
        size_bytes: Any = MediaItem.size_bytes
        completed_at: Any = EncodeResult.completed_at
        tier: Any = EncodeResult.tier

        where = [
            tier.is_not(None),
            output_size.is_not(None),
            output_size > 0,
            size_bytes.is_not(None),
            size_bytes > 0,
        ]
        if since is not None:
            where.append(completed_at.is_not(None))
            where.append(completed_at >= since.isoformat())

        size_den = func.nullif(size_bytes, 0)
        reduction_expr = (size_bytes - output_size) * 1.0 / size_den
        saved_expr: Any = func.coalesce(func.sum(size_bytes - output_size), 0)

        stmt: Any = (
            select(
                tier,
                func.count(EncodeResult.id).label("encodes"),
                saved_expr.label("saved_bytes"),
                func.avg(reduction_expr).label("avg_reduction"),
            )
            .join(MediaItem, EncodeResult.source_id == MediaItem.id)
            .where(*where)
            .group_by(tier)
            .order_by(tier)
        )

        rows = self.session.exec(stmt).all()

        results: list[TierEncodeStats] = []
        for row in rows:
            tier = str(row[0] or "unknown")
            encodes = int(row[1] or 0)
            saved_bytes = int(row[2] or 0)
            avg_reduction = float(row[3]) if row[3] is not None else None
            results.append(
                TierEncodeStats(
                    tier=tier,
                    encodes=encodes,
                    saved_bytes=saved_bytes,
                    avg_reduction=avg_reduction,
                )
            )
        return results
