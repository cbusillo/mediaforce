from __future__ import annotations

from typing import Optional, Sequence, Any

from sqlalchemy import text, func, case, select
from sqlmodel import Session

from mediaforce.db.models import MediaItem


class QueueRepository:
    """Encapsulate queue listings with raw SQL, keeping callers on Session."""

    def __init__(self, session: Session):
        self.session = session

    def list_shows(
        self,
        library_root: str,
        show_filter: Optional[str],
        tier_filter: Optional[str],
        size_min: Optional[int],
        size_max: Optional[int],
        per_page: int,
        page: int,
        sort: str,
        direction: str,
    ) -> tuple[list[dict], int, int, int]:
        like_root = f"{library_root}/%"
        filters = [
            MediaItem.status == "pending",
            MediaItem.path.like(like_root),
        ]

        if show_filter:
            filters.append(func.lower(MediaItem.path).like(f"%{show_filter.lower()}%"))
        if tier_filter:
            filters.append(MediaItem.detected_tier == tier_filter)
        if size_min is not None:
            filters.append(MediaItem.size_bytes >= size_min)
        if size_max is not None:
            filters.append(MediaItem.size_bytes <= size_max)

        # Extract show name (first segment after library root)
        rel_path = func.substr(MediaItem.path, len(library_root) + 2)
        first_slash_pos = func.instr(rel_path, "/")
        show_name = case(
            (first_slash_pos > 0, func.substr(rel_path, 1, first_slash_pos - 1)),
            else_=rel_path,
        ).label("show_name")

        file_count_col = func.count().label("file_count")
        size_col = func.sum(MediaItem.size_bytes).label("total_size_bytes")
        savings_col = func.sum(MediaItem.potential_savings_bytes).label("total_savings_bytes")
        avg_priority_col = func.avg(MediaItem.priority_score).label("avg_priority")
        max_priority_col = func.max(MediaItem.priority_score).label("max_priority")
        latest_scan_col = func.max(
            func.coalesce(
                MediaItem.scanned_at,
                MediaItem.updated_at,
                func.datetime(MediaItem.mtime, "unixepoch"),
            )
        ).label("latest_scan")
        reduction_col = case(
            (func.sum(MediaItem.size_bytes) > 0,
             func.sum(MediaItem.potential_savings_bytes) * 1.0 / func.sum(MediaItem.size_bytes)),
            else_=0,
        ).label("reduction_ratio")

        show_name_ci = show_name.collate("NOCASE")

        base_query = select(
            show_name,
            file_count_col,
            size_col,
            savings_col,
            avg_priority_col,
            max_priority_col,
            latest_scan_col,
            reduction_col,
        ).where(*filters).group_by(show_name)

        sort_map: dict[str, Any] = {
            "name": show_name_ci,
            "files": file_count_col,
            "size": size_col,
            "savings": savings_col,
            "priority": max_priority_col,
            "date": latest_scan_col,
            "reduction": reduction_col,
        }
        sort_expr = sort_map.get(sort, max_priority_col)
        sort_expr = sort_expr.asc() if direction.lower() == "asc" else sort_expr.desc()

        total_stmt = select(
            func.count().label("total_shows"),
            func.sum(text("file_count")).label("total_files"),
            func.sum(text("total_savings_bytes")).label("total_savings"),
        ).select_from(base_query.subquery())

        total_row = self.session.exec(total_stmt).mappings().fetchone()
        total = total_row["total_shows"] if total_row and total_row["total_shows"] else 0
        total_files = total_row["total_files"] if total_row and total_row["total_files"] else 0
        total_savings = total_row["total_savings"] if total_row and total_row["total_savings"] else 0

        rows = self.session.exec(
            base_query.order_by(sort_expr).limit(per_page).offset((page - 1) * per_page)
        ).mappings().fetchall()
        shows: list[dict] = []
        for row in rows:
            shows.append(
                {
                    "show_name": row["show_name"],
                    "file_count": row["file_count"],
                    "total_size": row["total_size_bytes"] or 0,
                    "total_savings": row["total_savings_bytes"] or 0,
                    "avg_priority": row["avg_priority"] or 0,
                    "max_priority": row["max_priority"] or 0,
                    "latest_scan": row["latest_scan"],
                    "reduction_pct": (row["reduction_ratio"] or 0) * 100,
                }
            )

        return shows, total, total_files, total_savings

    def list_seasons(self, library_root: str, show_name: str) -> list[dict]:
        like_pattern = f"{library_root}/{show_name}/%"
        rel_path = func.substr(MediaItem.path, len(library_root) + len(show_name) + 3)
        first_slash_pos = func.instr(rel_path, "/")
        season_name = case(
            (first_slash_pos > 0, func.substr(rel_path, 1, first_slash_pos - 1)),
            else_=text("'Files'"),
        ).label("season_name")

        stmt = (
            select(
                season_name,
                func.count().label("file_count"),
                func.sum(MediaItem.size_bytes).label("total_size_bytes"),
                func.sum(MediaItem.potential_savings_bytes).label("total_savings_bytes"),
                func.max(MediaItem.priority_score).label("max_priority"),
            )
            .where(MediaItem.status == "pending", MediaItem.path.like(like_pattern))
            .group_by(season_name)
            .order_by(func.max(MediaItem.priority_score).desc())
        )
        rows = self.session.exec(stmt).mappings().fetchall()
        seasons: list[dict] = []
        for row in rows:
            seasons.append(
                {
                    "season_name": row["season_name"],
                    "file_count": row["file_count"],
                    "total_size": row["total_size_bytes"] or 0,
                    "total_savings": row["total_savings_bytes"] or 0,
                    "max_priority": row["max_priority"] or 0,
                }
            )
        return seasons

    def list_episodes(self, library_root: str, show_name: str, season_name: str) -> Sequence[MediaItem]:
        like_pattern = f"{library_root}/{show_name}/{season_name}/%"
        stmt = (
            select(MediaItem)
            .where(MediaItem.status == "pending", MediaItem.path.like(like_pattern))
            .order_by(MediaItem.priority_score.desc())
        )
        rows = self.session.exec(stmt).all()
        return rows
"""Queue listing repository (raw SQL encapsulated)."""
# mypy: ignore-errors
# pyright: reportMissingImports=false, reportGeneralTypeIssues=false
