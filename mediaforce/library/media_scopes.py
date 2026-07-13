from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from sqlalchemy import and_, exists, literal, or_, select, true, union_all

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items

ScopeDomain = Literal["tv", "movie", "other"]
ScopeKind = Literal[
    "library_root",
    "tv_series",
    "tv_season",
    "movie_title",
    "movie_file",
    "media_folder",
    "media_file",
]
ScopeMatch = Literal["exact_item", "descendants"]
ScopeGroup = tuple[str, str, str, str]


class MediaScopeConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MediaScope:
    prefix: str
    root: str
    domain: ScopeDomain
    kind: ScopeKind
    match: ScopeMatch
    title: str
    subtitle: str
    scope_label: str
    parent_prefix: str | None = None
    parent_title: str | None = None

    def includes(self, rel_path: str) -> bool:
        return path_matches_scope(rel_path, self)

    def group_tuple(self) -> ScopeGroup:
        return self.prefix, self.title, self.subtitle, self.scope_label

    def to_payload(self) -> dict[str, object]:
        parent = None
        if self.parent_prefix is not None:
            parent = {
                "prefix": self.parent_prefix,
                "title": self.parent_title or self.parent_prefix,
            }
        return {
            "schema_version": 1,
            "prefix": self.prefix,
            "root": self.root,
            "domain": self.domain,
            "kind": self.kind,
            "match": self.match,
            "title": self.title,
            "subtitle": self.subtitle,
            "scope_label": self.scope_label,
            "parent": parent,
        }


def normalize_scope_prefix(prefix: str) -> str:
    return str(prefix or "").strip().strip("/")


def media_group_scope_for_rel_path(rel_path: str) -> MediaScope | None:
    normalized = normalize_scope_prefix(rel_path)
    parts = PurePosixPath(normalized).parts
    if len(parts) < 2:
        return None
    if parts[0].lower() == "tv" and len(parts) >= 4:
        return media_scope_from_prefix("/".join(parts[:3]), match="descendants")
    if parts[0].lower() == "tv" and len(parts) == 3:
        return media_scope_from_prefix(normalized, match="exact_item")
    if len(parts) == 2:
        return media_scope_from_prefix(normalized, match="exact_item")
    return media_scope_from_prefix("/".join(parts[:2]), match="descendants")


def tv_series_scope_for_rel_path(rel_path: str) -> MediaScope | None:
    normalized = normalize_scope_prefix(rel_path)
    parts = PurePosixPath(normalized).parts
    if len(parts) < 3 or parts[0].lower() != "tv":
        return None
    return media_scope_from_prefix("/".join(parts[:2]), match="descendants")


def media_scope_from_prefix(prefix: str, *, match: ScopeMatch) -> MediaScope:
    normalized = normalize_scope_prefix(prefix)
    parts = PurePosixPath(normalized).parts
    root = parts[0] if parts else ""
    domain = _scope_domain(root)
    root_title = _root_title(root)
    title = root_title if root else "Library"
    subtitle = "Library"
    scope_label = "Library"
    kind: ScopeKind = "library_root"
    parent_prefix: str | None = None
    parent_title: str | None = None

    if match == "exact_item":
        item_name = parts[-1] if parts else normalized
        title = PurePosixPath(item_name).stem or item_name
        subtitle = root_title if root else "Library"
        scope_label = "Title" if domain == "movie" and len(parts) == 2 else "File"
        kind = "movie_file" if domain == "movie" and len(parts) == 2 else "media_file"
        if len(parts) > 1:
            parent_prefix = "/".join(parts[:-1])
            parent_title = parts[-2].title() if len(parts) == 2 else parts[-2]
    elif domain == "tv" and len(parts) == 3:
        title = f"{parts[1]} · {parts[2]}"
        subtitle = parts[1]
        scope_label = "Season"
        kind = "tv_season"
        parent_prefix = "/".join(parts[:2])
        parent_title = parts[1]
    elif domain == "tv" and len(parts) > 3:
        title = parts[-1]
        subtitle = parts[-2]
        scope_label = "Folder"
        kind = "media_folder"
        parent_prefix = "/".join(parts[:-1])
        parent_title = parts[-2]
    elif domain == "tv" and len(parts) >= 2:
        normalized = "/".join(parts[:2])
        title = parts[1]
        subtitle = "TV series"
        scope_label = "Series"
        kind = "tv_series"
        parent_prefix = root
        parent_title = root_title
    elif len(parts) >= 2:
        title = parts[-1]
        subtitle = root_title
        scope_label = "Title" if domain == "movie" and len(parts) == 2 else "Folder"
        kind = "movie_title" if domain == "movie" and len(parts) == 2 else "media_folder"
        parent_prefix = "/".join(parts[:-1])
        parent_title = parts[-2].title() if len(parts) == 2 else parts[-2]

    return MediaScope(
        prefix=normalized,
        root=root,
        domain=domain,
        kind=kind,
        match=match,
        title=title,
        subtitle=subtitle,
        scope_label=scope_label,
        parent_prefix=parent_prefix,
        parent_title=parent_title,
    )


def resolve_media_scope(connection: DBClient, prefix: str) -> MediaScope:
    return resolve_media_scopes(connection, [prefix])[0]


def resolve_media_scopes(connection: DBClient, prefixes: list[str]) -> list[MediaScope]:
    normalized_prefixes = [normalize_scope_prefix(prefix) for prefix in prefixes]
    if not normalized_prefixes:
        return []
    unique_prefixes = list(dict.fromkeys(normalized_prefixes))
    nonempty_prefixes = [prefix for prefix in unique_prefixes if prefix]
    exact_statuses: dict[str, list[str]] = {prefix: [] for prefix in nonempty_prefixes}
    for prefix_batch in _chunks(nonempty_prefixes, 200):
        exact_rows = connection.execute(
            select(library_items.c.rel_path, library_items.c.status)
            .where(library_items.c.rel_path.in_(prefix_batch))
        ).fetchall()
        for rel_path, status in exact_rows:
            exact_statuses[str(rel_path)].append(str(status or ""))
    active_descendant_prefixes: set[str] = set()
    exact_prefixes = [prefix for prefix, statuses in exact_statuses.items() if statuses]
    for prefix_batch in _chunks(exact_prefixes, 200):
        prefix_queries = [select(literal(prefix).label("prefix")) for prefix in prefix_batch]
        prefix_table = union_all(*prefix_queries).cte("scope_prefixes")
        active_descendant_prefixes.update(
            str(prefix)
            for prefix in connection.execute(
                select(prefix_table.c.prefix).where(
                    exists(
                        select(library_items.c.id).where(
                            library_items.c.status != "missing",
                            scope_descendant_filter(library_items.c.rel_path, prefix_table.c.prefix),
                        )
                    )
                )
            ).scalars().all()
        )
    scopes_by_prefix: dict[str, MediaScope] = {}
    for prefix in unique_prefixes:
        exact_rows = exact_statuses.get(prefix, [])
        active_exact_count = sum(status != "missing" for status in exact_rows)
        active_descendant_match = prefix in active_descendant_prefixes
        if active_exact_count > 1:
            raise MediaScopeConflict(
                f"Media scope {prefix!r} is ambiguous because multiple active items share that path."
            )
        if active_exact_count and active_descendant_match:
            raise MediaScopeConflict(
                f"Media scope {prefix!r} is ambiguous because it is both an item and a folder prefix."
            )
        exact_match = bool(active_exact_count or (exact_rows and not active_descendant_match))
        scopes_by_prefix[prefix] = media_scope_from_prefix(
            prefix,
            match="exact_item" if exact_match else "descendants",
        )
    return [scopes_by_prefix[prefix] for prefix in normalized_prefixes]


def scope_rel_path_filter(column: Any, scope: MediaScope | str) -> Any:
    legacy_prefix = isinstance(scope, str)
    resolved = _legacy_scope(scope)
    if not resolved.prefix:
        return true()
    if resolved.match == "exact_item":
        return column == resolved.prefix
    descendant_filter = scope_descendant_filter(column, resolved.prefix)
    if legacy_prefix:
        return or_(column == resolved.prefix, descendant_filter)
    return descendant_filter


def scope_descendant_filter(column: Any, prefix: Any) -> Any:
    if isinstance(prefix, str):
        normalized = normalize_scope_prefix(prefix)
        if not normalized:
            return true()
        lower_bound = f"{normalized}/"
        upper_bound = f"{normalized}0"
    else:
        lower_bound = prefix + "/"
        upper_bound = prefix + "0"
    binary_column = column.collate("BINARY")
    return and_(binary_column >= lower_bound, binary_column < upper_bound)


def scope_ancestor_prefixes(prefix: str) -> list[str]:
    normalized = normalize_scope_prefix(prefix)
    parts = PurePosixPath(normalized).parts
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def scope_prefix_overlap_filter(column: Any, scope: MediaScope) -> Any:
    if not scope.prefix:
        return true()
    filters = [column == scope.prefix]
    ancestors = scope_ancestor_prefixes(scope.prefix)
    if ancestors:
        filters.append(column.in_(ancestors))
    if scope.match == "descendants":
        filters.append(scope_descendant_filter(column, scope.prefix))
    return or_(*filters)


def path_matches_scope(rel_path: str, scope: MediaScope | str) -> bool:
    normalized_path = normalize_scope_prefix(rel_path)
    legacy_prefix = isinstance(scope, str)
    resolved = _legacy_scope(scope)
    if not resolved.prefix:
        return True
    if resolved.match == "exact_item":
        return normalized_path == resolved.prefix
    return (
        (legacy_prefix and normalized_path == resolved.prefix)
        or normalized_path.startswith(f"{resolved.prefix}/")
    )


def scopes_overlap(left: MediaScope | str, right: MediaScope | str) -> bool:
    left_scope = _legacy_scope(left)
    right_scope = _legacy_scope(right)
    if not left_scope.prefix or not right_scope.prefix:
        return True
    if left_scope.prefix == right_scope.prefix:
        return True
    if left_scope.match == "exact_item":
        return (
            right_scope.match == "descendants"
            and left_scope.prefix.startswith(f"{right_scope.prefix}/")
        )
    if right_scope.match == "exact_item":
        return right_scope.prefix.startswith(f"{left_scope.prefix}/")
    return (
        left_scope.prefix.startswith(f"{right_scope.prefix}/")
        or right_scope.prefix.startswith(f"{left_scope.prefix}/")
    )


def is_tv_season_prefix(prefix: str) -> bool:
    parts = PurePosixPath(normalize_scope_prefix(prefix)).parts
    return len(parts) == 3 and parts[0].lower() == "tv"


def is_tv_series_prefix(prefix: str) -> bool:
    parts = PurePosixPath(normalize_scope_prefix(prefix)).parts
    return len(parts) == 2 and parts[0].lower() == "tv"


def series_context_for_prefix(prefix: str) -> dict[str, str] | None:
    normalized = normalize_scope_prefix(prefix)
    if not is_tv_season_prefix(normalized):
        return None
    parts = PurePosixPath(normalized).parts
    return {"prefix": "/".join(parts[:2]), "title": parts[1]}


def _legacy_scope(scope: MediaScope | str) -> MediaScope:
    if isinstance(scope, MediaScope):
        return scope
    return media_scope_from_prefix(scope, match="descendants")


def _scope_domain(root: str) -> ScopeDomain:
    normalized = root.lower()
    if normalized == "tv":
        return "tv"
    if normalized == "movies":
        return "movie"
    return "other"


def _root_title(root: str) -> str:
    return "TV" if root.lower() == "tv" else root.title()


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]
