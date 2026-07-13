from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any, Literal, Mapping

MovieRole = Literal["feature", "extra", "uncertain"]
MovieScopeMode = Literal["single_file", "title_folder"]

_EXTRA_DIRECTORIES = {
    "behindthescenes": "Behind the scenes",
    "bonus": "Bonus",
    "bonusfeatures": "Bonus features",
    "deletedscenes": "Deleted scenes",
    "extras": "Extras",
    "featurettes": "Featurettes",
    "interviews": "Interviews",
    "samples": "Samples",
    "shorts": "Shorts",
    "trailers": "Trailers",
}
_EXTRA_NAME_PATTERNS = (
    (re.compile(r"(?:^|[-_.\s])behind[-_.\s]*the[-_.\s]*scenes?$", re.IGNORECASE), "Behind the scenes"),
    (re.compile(r"(?:^|[-_.\s])bonus(?:[-_.\s]*features?)?$", re.IGNORECASE), "Bonus features"),
    (re.compile(r"(?:^|[-_.\s])deleted[-_.\s]*scenes?$", re.IGNORECASE), "Deleted scenes"),
    (re.compile(r"(?:^|[-_.\s])featurettes?$", re.IGNORECASE), "Featurettes"),
    (re.compile(r"(?:^|[-_.\s])interviews?$", re.IGNORECASE), "Interviews"),
    (re.compile(r"(?:^|[-_.\s])samples?$", re.IGNORECASE), "Samples"),
    (re.compile(r"(?:^|[-_.\s])shorts?$", re.IGNORECASE), "Shorts"),
    (re.compile(r"(?:^|[-_.\s])trailers?$", re.IGNORECASE), "Trailers"),
)
_EDITION_PATTERNS = (
    (re.compile(r"\bdirector'?s\s+cut\b", re.IGNORECASE), "Director's Cut"),
    (re.compile(r"\btheatrical(?:\s+cut)?\b", re.IGNORECASE), "Theatrical Cut"),
    (re.compile(r"\bextended(?:\s+cut|\s+edition)?\b", re.IGNORECASE), "Extended Edition"),
    (re.compile(r"\bfinal\s+cut\b", re.IGNORECASE), "Final Cut"),
    (re.compile(r"\bunrated\b", re.IGNORECASE), "Unrated Edition"),
    (re.compile(r"\bspecial\s+edition\b", re.IGNORECASE), "Special Edition"),
    (re.compile(r"\bremaster(?:ed)?\b", re.IGNORECASE), "Remastered"),
)


@dataclass(frozen=True, slots=True)
class MovieMembership:
    rel_path: str
    root: str
    title_prefix: str
    title: str
    scope_mode: MovieScopeMode
    role: MovieRole
    label: str
    edition_label: str | None = None
    extra_category: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "prefix": self.rel_path,
            "root": self.root,
            "title_prefix": self.title_prefix,
            "title": self.title,
            "scope_mode": self.scope_mode,
            "role": self.role,
            "label": self.label,
            "edition_label": self.edition_label,
            "extra_category": self.extra_category,
        }


def classify_movie_path(rel_path: str, *, root: str) -> MovieMembership | None:
    normalized = str(rel_path or "").strip().strip("/")
    parts = PurePosixPath(normalized).parts
    if len(parts) < 2 or parts[0] != root:
        return None
    file_name = parts[-1]
    file_label = PurePosixPath(file_name).stem or file_name
    if len(parts) == 2:
        return MovieMembership(
            rel_path=normalized,
            root=root,
            title_prefix=normalized,
            title=file_label,
            scope_mode="single_file",
            role="feature",
            label=file_label,
            edition_label=infer_edition_label(file_label),
        )

    title = parts[1]
    title_prefix = "/".join(parts[:2])
    relative_parts = parts[2:]
    for directory in relative_parts[:-1]:
        category = _EXTRA_DIRECTORIES.get(_normalized_token(directory))
        if category:
            return MovieMembership(
                rel_path=normalized,
                root=root,
                title_prefix=title_prefix,
                title=title,
                scope_mode="title_folder",
                role="extra",
                label=file_label,
                extra_category=category,
            )
    extra_category = infer_extra_category(file_label)
    if extra_category is not None:
        return MovieMembership(
            rel_path=normalized,
            root=root,
            title_prefix=title_prefix,
            title=title,
            scope_mode="title_folder",
            role="extra",
            label=file_label,
            extra_category=extra_category,
        )
    if len(relative_parts) == 1:
        return MovieMembership(
            rel_path=normalized,
            root=root,
            title_prefix=title_prefix,
            title=title,
            scope_mode="title_folder",
            role="feature",
            label=file_label,
            edition_label=infer_edition_label(file_label),
        )
    return MovieMembership(
        rel_path=normalized,
        root=root,
        title_prefix=title_prefix,
        title=title,
        scope_mode="title_folder",
        role="uncertain",
        label=file_label,
    )


def movie_item_included(
        membership: MovieMembership,
        policy: Mapping[str, Any],
        *,
        explicit_exact: bool,
) -> tuple[bool, str | None]:
    if explicit_exact or membership.role == "feature":
        return True, None
    if membership.role == "extra" and str(policy.get("extras") or "exclude") == "include":
        return True, None
    if membership.role == "extra":
        return False, "Extras are excluded from title-wide processing by this movie library policy."
    return False, "This nested movie file needs an explicit exact-file action before processing."


def infer_edition_label(value: str) -> str | None:
    tagged = re.search(r"\{edition[-_: ]+([^}]+)\}", value, re.IGNORECASE)
    if tagged is not None:
        label = tagged.group(1).strip(" -_:")
        if label:
            return label
    for pattern, label in _EDITION_PATTERNS:
        if pattern.search(value):
            return label
    return None


def infer_extra_category(value: str) -> str | None:
    for pattern, category in _EXTRA_NAME_PATTERNS:
        if pattern.search(value):
            return category
    return None


def _normalized_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())
