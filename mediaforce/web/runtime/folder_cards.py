import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, staged_artifacts

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
    known_saved_bytes: int
    projected_reclaim_bytes: int
    average_age_days: float
    sort_score: float
    statuses: dict[str, int]
    video_codecs: dict[str, int]
    review_badge_label: str | None = None
    review_badge_tone: str | None = None
    details_loading: bool = False


@dataclass(slots=True)
class SavingsHistory:
    total_source_bytes: int = 0
    total_bytes_saved: int = 0
    sample_count: int = 0
    known_codec_sample_count: int = 0
    codec_counts: dict[str, int] = field(default_factory=dict)

    def record(self, *, source_size_bytes: int, bytes_saved: int, codec_key: str | None = None) -> None:
        if source_size_bytes <= 0:
            return
        self.total_source_bytes += source_size_bytes
        self.total_bytes_saved += bytes_saved
        self.sample_count += 1
        if codec_key:
            self.known_codec_sample_count += 1
            self.codec_counts[codec_key] = self.codec_counts.get(codec_key, 0) + 1

    @property
    def savings_ratio(self) -> float:
        if self.total_source_bytes <= 0:
            return 0.0
        return max(0.0, min(self.total_bytes_saved / self.total_source_bytes, 0.95))

    @property
    def dominant_codec_key(self) -> str | None:
        if not self.codec_counts:
            return None
        dominant_codec, dominant_count = max(self.codec_counts.items(), key=lambda item: item[1])
        if sum(1 for count in self.codec_counts.values() if count == dominant_count) > 1:
            return None
        return dominant_codec

    @property
    def dominant_codec_share(self) -> float:
        dominant_codec = self.dominant_codec_key
        if dominant_codec is None or self.sample_count <= 0:
            return 0.0
        return self.codec_counts[dominant_codec] / self.sample_count


_FOLDER_CARD_CACHE_LOCK = threading.Lock()
_FOLDER_CARD_CACHE_KEY: tuple[str, int, int] | None = None
_FOLDER_CARD_CACHE_VALUE: list[FolderCard] = []
_FOLDER_CARD_CACHE_GENERATION = 0


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
    global _FOLDER_CARD_CACHE_KEY, _FOLDER_CARD_CACHE_VALUE, _FOLDER_CARD_CACHE_GENERATION

    while True:
        cache_key = folder_card_cache_key(config)
        with _FOLDER_CARD_CACHE_LOCK:
            cache_generation = _FOLDER_CARD_CACHE_GENERATION
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
            current_cache_key = folder_card_cache_key(config)
            if cache_generation != _FOLDER_CARD_CACHE_GENERATION or cache_key != current_cache_key:
                continue
            if _FOLDER_CARD_CACHE_KEY == cache_key:
                return list(_FOLDER_CARD_CACHE_VALUE)
            _FOLDER_CARD_CACHE_KEY = cache_key
            _FOLDER_CARD_CACHE_VALUE = list(cards)
            return cards


def reset_folder_card_cache() -> None:
    global _FOLDER_CARD_CACHE_KEY, _FOLDER_CARD_CACHE_VALUE, _FOLDER_CARD_CACHE_GENERATION

    with _FOLDER_CARD_CACHE_LOCK:
        _FOLDER_CARD_CACHE_GENERATION += 1
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
    folder_history, codec_history = _load_savings_history(connection, folder_group=folder_group)
    rows = connection.execute(
        select(
            library_items.c.rel_path,
            library_items.c.size_bytes,
            library_items.c.status,
            library_items.c.video_codec,
            library_items.c.audio_summary_json,
            staged_artifacts.c.bytes_saved,
        )
        .select_from(
            library_items.outerjoin(
                staged_artifacts,
                staged_artifacts.c.library_item_id == library_items.c.id,
            )
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
                known_saved_bytes=0,
                projected_reclaim_bytes=0,
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
        known_saved_bytes = _pending_folder_item_savings_bytes(status=status, bytes_saved=row["bytes_saved"])
        if known_saved_bytes is not None:
            card.known_saved_bytes += known_saved_bytes
        if status != "promoted":
            card.pending_count += 1
            if known_saved_bytes is None:
                card.estimated_savings_bytes += _estimated_pending_savings_bytes(
                    size_bytes=size_bytes,
                    video_codec=codec,
                    audio_summary_json=str(row["audio_summary_json"] or "[]"),
                    estimate_savings_bytes=estimate_savings_bytes,
                    folder_history=folder_history.get(prefix),
                    codec_history=codec_history.get(_canonical_video_codec(codec)),
                )
        card.statuses[status] = card.statuses.get(status, 0) + 1
        card.video_codecs[codec] = card.video_codecs.get(codec, 0) + 1
    for card in grouped.values():
        card.projected_reclaim_bytes = card.known_saved_bytes + card.estimated_savings_bytes
    cards = [
        card
        for card in grouped.values()
        if card.pending_count > 0 and card.projected_reclaim_bytes >= minimum_recommended_savings_bytes
    ]
    _apply_folder_review_badges(cards, review_badge_for_prefix)
    return sorted(cards, key=lambda item: (item.projected_reclaim_bytes, item.total_size_bytes), reverse=True)


def list_folder_cards(
        connection: DBClient,
        *,
        minimum_recommended_savings_bytes: int,
        folder_group: Callable[[str], FolderGroup | None],
        age_days: Callable[[str], float],
        estimate_savings_bytes: Callable[..., int],
        review_badge_for_prefix: Callable[[str], FolderBadge],
) -> list[FolderCard]:
    folder_history, codec_history = _load_savings_history(connection, folder_group=folder_group)
    rows = connection.execute(
        select(
            library_items.c.rel_path,
            library_items.c.source_path,
            library_items.c.size_bytes,
            library_items.c.status,
            library_items.c.video_codec,
            library_items.c.audio_summary_json,
            staged_artifacts.c.bytes_saved,
        )
        .select_from(
            library_items.outerjoin(
                staged_artifacts,
                staged_artifacts.c.library_item_id == library_items.c.id,
            )
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
                known_saved_bytes=0,
                projected_reclaim_bytes=0,
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
        known_saved_bytes = _pending_folder_item_savings_bytes(status=status, bytes_saved=row["bytes_saved"])
        age_multiplier = _age_multiplier(path_age_days)
        if known_saved_bytes is not None:
            card.known_saved_bytes += known_saved_bytes
            card.sort_score += (known_saved_bytes / (1024 ** 3)) * age_multiplier
        if status != "promoted":
            card.pending_count += 1
            if known_saved_bytes is None:
                estimated_savings = _estimated_pending_savings_bytes(
                    size_bytes=size_bytes,
                    video_codec=codec,
                    audio_summary_json=str(row["audio_summary_json"] or "[]"),
                    estimate_savings_bytes=estimate_savings_bytes,
                    folder_history=folder_history.get(prefix),
                    codec_history=codec_history.get(_canonical_video_codec(codec)),
                )
                card.estimated_savings_bytes += estimated_savings
                card.sort_score += (estimated_savings / (1024 ** 3)) * age_multiplier
        card.statuses[status] = card.statuses.get(status, 0) + 1
        card.video_codecs[codec] = card.video_codecs.get(codec, 0) + 1
    cards = list(grouped.values())
    for card in cards:
        card.average_age_days = round(card.average_age_days / max(card.item_count, 1), 1)
        card.projected_reclaim_bytes = card.known_saved_bytes + card.estimated_savings_bytes
    cards = [
        card
        for card in cards
        if card.pending_count > 0 and card.projected_reclaim_bytes >= minimum_recommended_savings_bytes
    ]
    _apply_folder_review_badges(cards, review_badge_for_prefix)
    return sorted(
        cards,
        key=lambda item: (item.sort_score, item.projected_reclaim_bytes, item.total_size_bytes),
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


def _load_savings_history(
        connection: DBClient,
        *,
        folder_group: Callable[[str], FolderGroup | None],
) -> tuple[dict[str, SavingsHistory], dict[str, SavingsHistory]]:
    rows = connection.execute(
        select(
            library_items.c.rel_path,
            library_items.c.status,
            staged_artifacts.c.source_rel_path,
            staged_artifacts.c.source_video_codec,
            staged_artifacts.c.source_size_bytes,
            staged_artifacts.c.bytes_saved,
        )
        .select_from(
            staged_artifacts.join(
                library_items,
                staged_artifacts.c.library_item_id == library_items.c.id,
            )
        )
        .where(library_items.c.status.in_(("encoded", "validated", "promoted")))
        .where(staged_artifacts.c.source_size_bytes.is_not(None))
        .where(staged_artifacts.c.bytes_saved.is_not(None))
    ).mappings().fetchall()

    folder_history: dict[str, SavingsHistory] = {}
    codec_history: dict[str, SavingsHistory] = {}
    for row in rows:
        source_size_bytes = int(row["source_size_bytes"] or 0)
        if source_size_bytes <= 0:
            continue
        bytes_saved = int(row["bytes_saved"] or 0)
        codec = _history_video_codec_key(row["source_video_codec"])
        rel_path = str(row["source_rel_path"] or row["rel_path"] or "")
        group = folder_group(rel_path)
        if group is not None and codec is not None:
            history = folder_history.setdefault(group[0], SavingsHistory())
            history.record(source_size_bytes=source_size_bytes, bytes_saved=bytes_saved, codec_key=codec)
        if codec is not None:
            history = codec_history.setdefault(codec, SavingsHistory())
            history.record(source_size_bytes=source_size_bytes, bytes_saved=bytes_saved, codec_key=codec)
    return folder_history, codec_history


def _estimated_pending_savings_bytes(
        *,
        size_bytes: int,
        video_codec: str,
        audio_summary_json: str,
        estimate_savings_bytes: Callable[..., int],
        folder_history: SavingsHistory | None,
        codec_history: SavingsHistory | None,
) -> int:
    if size_bytes <= 0:
        return 0
    pending_codec = _canonical_video_codec(video_codec)
    if _folder_history_matches_pending_codec(folder_history, pending_codec):
        return int(size_bytes * folder_history.savings_ratio)
    if codec_history is not None and codec_history.sample_count > 0:
        return int(size_bytes * codec_history.savings_ratio)
    return estimate_savings_bytes(
        size_bytes=size_bytes,
        video_codec=video_codec,
        audio_summary_json=audio_summary_json,
    )


def _pending_folder_item_savings_bytes(*, status: str, bytes_saved: object) -> int | None:
    if status not in {"encoded", "validated"}:
        return None
    if bytes_saved is None:
        return None
    return int(bytes_saved)


def _folder_history_matches_pending_codec(history: SavingsHistory | None, pending_codec: str) -> bool:
    if history is None or history.sample_count < 2 or history.known_codec_sample_count < 2:
        return False
    dominant_codec = history.dominant_codec_key
    if dominant_codec is None:
        return False
    if pending_codec != "unknown" and dominant_codec != pending_codec:
        return False
    return history.dominant_codec_share > 0.5


def _canonical_video_codec(codec: str) -> str:
    normalized = codec.strip().lower()
    if not normalized:
        return "unknown"
    return {
        "h265": "hevc",
        "hvc1": "hevc",
        "hev1": "hevc",
        "avc1": "h264",
        "avc3": "h264",
    }.get(normalized, normalized)


def _history_video_codec_key(codec: object) -> str | None:
    canonical = _canonical_video_codec(str(codec or ""))
    if canonical == "unknown":
        return None
    return canonical


def _age_multiplier(age_days: float) -> float:
    if age_days >= 3650:
        return 1.35
    if age_days >= 1825:
        return 1.25
    if age_days >= 730:
        return 1.15
    return 1.0
