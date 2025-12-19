from typing import Optional, Sequence, Any

from sqlalchemy import func, case, literal
from sqlmodel import Session, select as _select  # type: ignore[reportMissingImports]

from mediaforce.db.models import MediaItem

select: Any = _select


class QueueRepository:
    """Encapsulate queue listings, keeping callers on Session."""

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
        path_col: Any = MediaItem.path
        size_col_model: Any = MediaItem.size_bytes
        tier_col_model: Any = MediaItem.detected_tier
        priority_col_model: Any = MediaItem.priority_score
        savings_col_model: Any = MediaItem.potential_savings_bytes
        scanned_at_col: Any = MediaItem.scanned_at
        updated_at_col: Any = MediaItem.updated_at
        mtime_col: Any = MediaItem.mtime

        filters = [
            MediaItem.status == "pending",
            path_col.like(like_root),
        ]

        if show_filter:
            filters.append(func.lower(path_col).like(f"%{show_filter.lower()}%"))
        if tier_filter:
            filters.append(tier_col_model == tier_filter)
        if size_min is not None:
            filters.append(size_col_model >= size_min)
        if size_max is not None:
            filters.append(size_col_model <= size_max)

        # Extract show name (first segment after library root)
        rel_path = func.substr(path_col, len(library_root) + 2)
        first_slash_pos = func.instr(rel_path, "/")
        show_name: Any = case(
            (first_slash_pos > 0, func.substr(rel_path, 1, first_slash_pos - 1)),
            else_=rel_path,
        ).label("show_name")

        file_count_col = func.count().label("file_count")
        size_col = func.sum(size_col_model).label("total_size_bytes")
        savings_col = func.sum(savings_col_model).label("total_savings_bytes")
        avg_priority_col = func.avg(priority_col_model).label("avg_priority")
        max_priority_col = func.max(priority_col_model).label("max_priority")
        latest_scan_col = func.max(
            func.coalesce(
                scanned_at_col,
                updated_at_col,
                func.datetime(mtime_col, "unixepoch"),
            )
        ).label("latest_scan")
        reduction_col: Any = case(
            (func.sum(size_col_model) > 0,
             func.sum(savings_col_model) * 1.0 / func.sum(size_col_model)),
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

        grouped = base_query.subquery()
        total_stmt = select(
            func.count().label("total_shows"),
            func.sum(grouped.c.file_count).label("total_files"),
            func.sum(grouped.c.total_savings_bytes).label("total_savings"),
        ).select_from(grouped)

        total_row = self.session.exec(total_stmt).mappings().fetchone()  # type: ignore[call-overload]
        total = total_row["total_shows"] if total_row and total_row["total_shows"] else 0
        total_files = total_row["total_files"] if total_row and total_row["total_files"] else 0
        total_savings = total_row["total_savings"] if total_row and total_row["total_savings"] else 0

        rows = self.session.exec(  # type: ignore[call-overload]
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
        path_col: Any = MediaItem.path
        size_col_model: Any = MediaItem.size_bytes
        savings_col_model: Any = MediaItem.potential_savings_bytes
        priority_col_model: Any = MediaItem.priority_score

        rel_path = func.substr(path_col, len(library_root) + len(show_name) + 3)
        first_slash_pos = func.instr(rel_path, "/")
        season_name: Any = case(
            (first_slash_pos > 0, func.substr(rel_path, 1, first_slash_pos - 1)),
            else_=literal("Files"),
        ).label("season_name")

        stmt = (
            select(
                season_name,
                func.count().label("file_count"),
                func.sum(size_col_model).label("total_size_bytes"),
                func.sum(savings_col_model).label("total_savings_bytes"),
                func.max(priority_col_model).label("max_priority"),
            )
            .where(MediaItem.status == "pending", path_col.like(like_pattern))
            .group_by(season_name)
            .order_by(func.max(priority_col_model).desc())
        )
        rows = self.session.exec(stmt).mappings().fetchall()  # type: ignore[call-overload]
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
        path_col: Any = MediaItem.path
        priority_col_model: Any = MediaItem.priority_score
        stmt = (
            select(MediaItem)
            .where(MediaItem.status == "pending", path_col.like(like_pattern))
            .order_by(priority_col_model.desc())
        )
        rows = self.session.exec(stmt).all()
        return rows
