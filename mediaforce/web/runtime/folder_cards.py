import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items

FolderGroup = tuple[str, str, str, str]
FolderBadge = dict[str, str | None]


@dataclass(slots=True)
class FolderCard:
    prefix: str
    title: str
    subtitle: str
    scope_label: str
    item_count: int
    pending_count: int
    total_size_bytes: int
    estimated_savings_bytes: int
    average_age_days: float
    sort_score: float
    statuses: dict[str, int]
    video_codecs: dict[str, int]
    review_badge_label: str | None = None
    review_badge_tone: str | None = None
    details_loading: bool = False


_FOLDER_CARD_CACHE_LOCK = threading.Lock()
_FOLDER_CARD_CACHE_KEY: tuple[str, int, int] | None = None
_FOLDER_CARD_CACHE_VALUE: list[FolderCard] = []


def cached_folder_cards(
        config: MediaforceConfig,
        connection: DBClient,
        *,
        minimum_recommended_savings_bytes: int,
        folder_group: Callable[[str], FolderGroup | None],
        age_days: Callable[[str], float],
        estimate_savings_bytes: Callable[..., int],
        review_badge_for_prefix: Callable[[str], FolderBadge],
) -> list[FolderCard]:
    global _FOLDER_CARD_CACHE_KEY, _FOLDER_CARD_CACHE_VALUE

    cache_key = folder_card_cache_key(config)
    with _FOLDER_CARD_CACHE_LOCK:
        if _FOLDER_CARD_CACHE_KEY == cache_key:
            return list(_FOLDER_CARD_CACHE_VALUE)
    cards = list_folder_cards(
        connection,
        minimum_recommended_savings_bytes=minimum_recommended_savings_bytes,
        folder_group=folder_group,
        age_days=age_days,
        estimate_savings_bytes=estimate_savings_bytes,
        review_badge_for_prefix=review_badge_for_prefix,
    )
    with _FOLDER_CARD_CACHE_LOCK:
        _FOLDER_CARD_CACHE_KEY = cache_key
        _FOLDER_CARD_CACHE_VALUE = list(cards)
    return cards


def reset_folder_card_cache() -> None:
    global _FOLDER_CARD_CACHE_KEY, _FOLDER_CARD_CACHE_VALUE

    with _FOLDER_CARD_CACHE_LOCK:
        _FOLDER_CARD_CACHE_KEY = None
        _FOLDER_CARD_CACHE_VALUE = []


def preview_folder_cards(
        connection: DBClient,
        *,
        minimum_recommended_savings_bytes: int,
        folder_group: Callable[[str], FolderGroup | None],
        estimate_savings_bytes: Callable[..., int],
        review_badge_for_prefix: Callable[[str], FolderBadge],
) -> list[FolderCard]:
    rows = connection.execute(
        select(
            library_items.c.rel_path,
            library_items.c.size_bytes,
            library_items.c.status,
            library_items.c.video_codec,
            library_items.c.audio_summary_json,
        )
        .where(library_items.c.status != "missing")
        .order_by(library_items.c.rel_path)
    ).mappings().fetchall()
    grouped: dict[str, FolderCard] = {}
    for row in rows:
        rel_path = str(row["rel_path"])
        group = folder_group(rel_path)
        if group is None:
            continue
        prefix, title, subtitle, scope_label = group
        card = grouped.get(prefix)
        if card is None:
            card = FolderCard(
                prefix=prefix,
                title=title,
                subtitle=subtitle,
                scope_label=scope_label,
                item_count=0,
                pending_count=0,
                total_size_bytes=0,
                estimated_savings_bytes=0,
                average_age_days=0.0,
                sort_score=0.0,
                statuses={},
                video_codecs={},
                details_loading=True,
            )
            grouped[prefix] = card
        card.item_count += 1
        size_bytes = int(row["size_bytes"])
        card.total_size_bytes += size_bytes
        status = str(row["status"] or "unknown")
        codec = str(row["video_codec"] or "unknown")
        if status != "promoted":
            card.pending_count += 1
            card.estimated_savings_bytes += estimate_savings_bytes(
                size_bytes=size_bytes,
                video_codec=codec,
                audio_summary_json=str(row["audio_summary_json"] or "[]"),
            )
        card.statuses[status] = card.statuses.get(status, 0) + 1
        card.video_codecs[codec] = card.video_codecs.get(codec, 0) + 1
    cards = [
        card
        for card in grouped.values()
        if card.pending_count > 0 and card.estimated_savings_bytes >= minimum_recommended_savings_bytes
    ]
    _apply_folder_review_badges(cards, review_badge_for_prefix)
    return sorted(cards, key=lambda item: (item.estimated_savings_bytes, item.total_size_bytes), reverse=True)


def list_folder_cards(
        connection: DBClient,
        *,
        minimum_recommended_savings_bytes: int,
        folder_group: Callable[[str], FolderGroup | None],
        age_days: Callable[[str], float],
        estimate_savings_bytes: Callable[..., int],
        review_badge_for_prefix: Callable[[str], FolderBadge],
) -> list[FolderCard]:
    rows = connection.execute(
        select(
            library_items.c.rel_path,
            library_items.c.source_path,
            library_items.c.size_bytes,
            library_items.c.status,
            library_items.c.video_codec,
            library_items.c.audio_summary_json,
        )
        .where(library_items.c.status != "missing")
        .order_by(library_items.c.rel_path)
    ).mappings().fetchall()
    grouped: dict[str, FolderCard] = {}
    for row in rows:
        rel_path = str(row["rel_path"])
        group = folder_group(rel_path)
        if group is None:
            continue
        prefix, title, subtitle, scope_label = group
        card = grouped.get(prefix)
        if card is None:
            card = FolderCard(
                prefix=prefix,
                title=title,
                subtitle=subtitle,
                scope_label=scope_label,
                item_count=0,
                pending_count=0,
                total_size_bytes=0,
                estimated_savings_bytes=0,
                average_age_days=0.0,
                sort_score=0.0,
                statuses={},
                video_codecs={},
            )
            grouped[prefix] = card
        card.item_count += 1
        size_bytes = int(row["size_bytes"])
        card.total_size_bytes += size_bytes
        path_age_days = age_days(str(row["source_path"]))
        card.average_age_days += path_age_days
        status = str(row["status"] or "unknown")
        codec = str(row["video_codec"] or "unknown")
        if status != "promoted":
            estimated_savings = estimate_savings_bytes(
                size_bytes=size_bytes,
                video_codec=codec,
                audio_summary_json=str(row["audio_summary_json"] or "[]"),
            )
            age_multiplier = _age_multiplier(path_age_days)
            card.pending_count += 1
            card.estimated_savings_bytes += estimated_savings
            card.sort_score += (estimated_savings / (1024 ** 3)) * age_multiplier
        card.statuses[status] = card.statuses.get(status, 0) + 1
        card.video_codecs[codec] = card.video_codecs.get(codec, 0) + 1
    cards = list(grouped.values())
    for card in cards:
        card.average_age_days = round(card.average_age_days / max(card.item_count, 1), 1)
    cards = [
        card
        for card in cards
        if card.pending_count > 0 and card.estimated_savings_bytes >= minimum_recommended_savings_bytes
    ]
    _apply_folder_review_badges(cards, review_badge_for_prefix)
    return sorted(
        cards,
        key=lambda item: (item.sort_score, item.estimated_savings_bytes, item.total_size_bytes),
        reverse=True,
    )


def folder_card_cache_key(config: MediaforceConfig) -> tuple[str, int, int]:
    try:
        db_mtime_ns = config.paths.db_path.stat().st_mtime_ns
    except OSError:
        db_mtime_ns = 0
    return str(config.paths.db_path), db_mtime_ns, _web_state_latest_mtime_ns(config)


def _path_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return -1


def _web_state_latest_mtime_ns(config: MediaforceConfig) -> int:
    latest = _path_mtime_ns(config.paths.web_state_dir)
    try:
        for candidate in config.paths.web_state_dir.glob("*.json"):
            latest = max(latest, candidate.stat().st_mtime_ns)
    except FileNotFoundError:
        return latest
    return latest


def _apply_folder_review_badges(cards: list[FolderCard], review_badge_for_prefix: Callable[[str], FolderBadge]) -> None:
    for card in cards:
        badge = review_badge_for_prefix(card.prefix)
        card.review_badge_label = badge["label"]
        card.review_badge_tone = badge["tone"]


def _age_multiplier(age_days: float) -> float:
    if age_days >= 3650:
        return 1.35
    if age_days >= 1825:
        return 1.25
    if age_days >= 730:
        return 1.15
    return 1.0
