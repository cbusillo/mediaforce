"""Pure canonical path validation shared by AV1 protocol-v4 revision 3."""

from __future__ import annotations


class AV1V4R3CanonicalPathError(ValueError):
    """Raised when a revision-3 path is not canonical absolute POSIX text."""


def canonical_av1_v4r3_absolute_posix_path(value: str) -> str:
    """Return one already-canonical absolute POSIX path without filesystem I/O."""

    if not isinstance(value, str):
        raise AV1V4R3CanonicalPathError("AV1 v4 r3 path must be text")
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or not value.startswith("/")
        or value == "/"
        or "//" in value
        or value.endswith("/")
        or "/../" in value
        or value.endswith("/..")
        or "/./" in value
        or value.endswith("/.")
    ):
        raise AV1V4R3CanonicalPathError(
            "AV1 v4 r3 path must be a canonical absolute POSIX path"
        )
    return value
