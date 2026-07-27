from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from typing import Callable, Iterable
import zipfile

from mediaforce.tuning.av1_cold_start import (
    AV1ColdStartContractError,
    assert_av1_cold_start_public_payload_safe,
)


PUBLIC_PRIOR_PATH = PurePosixPath("mediaforce/tuning/data/av1_cold_start_priors_v1.json")
PACKAGED_DEFAULTS_PATH = PurePosixPath("mediaforce/package_defaults/defaults.toml")
FORBIDDEN_COMPONENTS = frozenset({
    ".idea",
    ".env",
    ".DS_Store",
    "__pycache__",
    "review-media",
    "scratch",
    "state",
})
FORBIDDEN_SUFFIXES = (
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
FORBIDDEN_SECRET_SUFFIXES = (".key", ".p12", ".pem", ".pfx")
FORBIDDEN_RUNTIME_PACKAGE_PATHS = frozenset({
    PurePosixPath("config/defaults.toml"),
    PurePosixPath("config/folder-defaults.toml"),
})
MAX_SCANNABLE_MEMBER_BYTES = 8_000_000
EXAMPLE_HOME_PATHS = frozenset({
    b"/home/runner",
    b"/Users/example",
    b"/Users/me",
    b"/Users/operator",
})
PRIVATE_MACHINE_PATH_RE = re.compile(
    rb"(?:/Users/(?!operator(?:/|\b)|example(?:/|\b)|me(?:/|\b))"
    rb"[A-Za-z0-9._-]+/|"
    rb"/home/(?!runner(?:/|\b)|operator(?:/|\b)|example(?:/|\b))"
    rb"[A-Za-z0-9._-]+/|"
    rb"/private/var/[A-Za-z0-9._-]+/|"
    rb"[A-Za-z]:\\Users\\(?!operator\\|example\\)[^\\\s]+\\)",
    re.IGNORECASE,
)
TEMP_MACHINE_PATH_RE = re.compile(
    rb"/tmp/(?!mediaforce(?:-|/)|\.com\.apple\.dt\.CommandLineTools\b)"
    rb"[A-Za-z0-9._-]+(?:/[^\s\"']*)?",
)
SDIST_ROOT_RE = re.compile(r"mediaforce-\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*)?\Z")


class PackageContentsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _ArchiveMember:
    path: PurePosixPath
    size: int
    read: Callable[[], bytes]


@dataclass(frozen=True, slots=True)
class _ArchiveView:
    members: tuple[_ArchiveMember, ...]
    close: Callable[[], None]


def verify_package_archives(
        archives: Iterable[Path],
        *,
        source_artifact: Path,
        source_defaults: Path | None = None,
) -> list[dict[str, object]]:
    source_bytes = source_artifact.read_bytes()
    default_bytes = source_defaults.read_bytes() if source_defaults is not None else None
    summaries = []
    for archive in archives:
        view = _archive_members(archive)
        try:
            _reject_forbidden_members(archive, (member.path for member in view.members))
            matches = [member for member in view.members if _is_public_prior(member.path)]
            if len(matches) != 1:
                raise PackageContentsError(
                    f"{archive.name} must contain exactly one public AV1 prior artifact; "
                    f"found {[str(member.path) for member in matches]}"
                )
            artifact_bytes = matches[0].read()
            if artifact_bytes != source_bytes:
                raise PackageContentsError(
                    f"{archive.name} AV1 prior bytes differ from the checked-in artifact"
                )
            try:
                assert_av1_cold_start_public_payload_safe(
                    json.loads(artifact_bytes.decode("utf-8"))
                )
            except (UnicodeDecodeError, json.JSONDecodeError, AV1ColdStartContractError) as exc:
                raise PackageContentsError(
                    f"{archive.name} AV1 prior violates the public privacy contract"
                ) from exc
            default_matches: list[_ArchiveMember] = []
            if default_bytes is not None:
                default_matches = [
                    member
                    for member in view.members
                    if _without_sdist_root(member.path) == PACKAGED_DEFAULTS_PATH
                ]
                if len(default_matches) != 1 or default_matches[0].read() != default_bytes:
                    raise PackageContentsError(
                        f"{archive.name} must contain one byte-identical install-safe default config"
                    )
            _scan_archive_text(archive, view.members)
            summaries.append(
                {
                    "archive": archive.name,
                    "member_count": len(view.members),
                    "public_prior": str(matches[0].path),
                    "packaged_defaults": (
                        str(default_matches[0].path) if default_bytes is not None else None
                    ),
                }
            )
        finally:
            view.close()
    return summaries


def _archive_members(
        archive: Path,
) -> _ArchiveView:
    if archive.suffix == ".whl":
        wheel = zipfile.ZipFile(archive)
        members = tuple(
            _ArchiveMember(
                path=PurePosixPath(info.filename),
                size=info.file_size,
                read=lambda info=info: wheel.read(info),
            )
            for info in wheel.infolist()
            if not info.is_dir()
        )
        return _ArchiveView(members=members, close=wheel.close)
    if archive.name.endswith(".tar.gz"):
        source = tarfile.open(archive, "r:gz")

        def read_member(member: tarfile.TarInfo) -> bytes:
            extracted = source.extractfile(member)
            if extracted is None:
                raise PackageContentsError(f"Unable to read {member.name} from {archive.name}")
            return extracted.read()
        members = tuple(
            _ArchiveMember(
                path=PurePosixPath(member.name),
                size=member.size,
                read=lambda member=member: read_member(member),
            )
            for member in source.getmembers()
            if member.isfile()
        )
        return _ArchiveView(members=members, close=source.close)
    raise PackageContentsError(f"Unsupported package archive {archive}")


def _reject_forbidden_members(archive: Path, members: Iterable[PurePosixPath]) -> None:
    rejected = []
    for member in members:
        parts = set(member.parts)
        member_name = member.name.casefold()
        if (
                member.is_absolute()
                or ".." in parts
                or "\\" in str(member)
                or parts.intersection(FORBIDDEN_COMPONENTS)
                or any(part.casefold().startswith(".env") for part in member.parts)
                or member_name.endswith(FORBIDDEN_SUFFIXES)
                or member_name.endswith(FORBIDDEN_SECRET_SUFFIXES)
                or _without_sdist_root(member) in FORBIDDEN_RUNTIME_PACKAGE_PATHS
                or (_has_public_prior_suffix(member) and not _is_public_prior(member))
        ):
            rejected.append(str(member))
    if rejected:
        raise PackageContentsError(f"{archive.name} contains forbidden private/runtime members: {rejected}")


def _without_sdist_root(member: PurePosixPath) -> PurePosixPath:
    if len(member.parts) > 1 and SDIST_ROOT_RE.fullmatch(member.parts[0]):
        return PurePosixPath(*member.parts[1:])
    return member


def _is_public_prior(member: PurePosixPath) -> bool:
    return _without_sdist_root(member) == PUBLIC_PRIOR_PATH


def _has_public_prior_suffix(member: PurePosixPath) -> bool:
    expected_parts = PUBLIC_PRIOR_PATH.parts
    return member.parts[-len(expected_parts):] == expected_parts


def _scan_archive_text(archive: Path, members: Iterable[_ArchiveMember]) -> None:
    private_path = str(Path.home()).encode("utf-8")
    for member in members:
        if not _scannable_member(member.path):
            continue
        if member.size > MAX_SCANNABLE_MEMBER_BYTES:
            raise PackageContentsError(
                f"{archive.name} contains oversized scannable member {member.path}"
            )
        payload = member.read()
        if private_path and private_path not in EXAMPLE_HOME_PATHS and private_path in payload:
            raise PackageContentsError(f"{archive.name} contains the current machine's home path")
        if PRIVATE_MACHINE_PATH_RE.search(payload):
            raise PackageContentsError(
                f"{archive.name} contains a machine-specific absolute path in {member.path}"
            )
        if TEMP_MACHINE_PATH_RE.search(payload):
            raise PackageContentsError(
                f"{archive.name} contains a machine-specific temporary path in {member.path}"
            )


def _scannable_member(member: PurePosixPath) -> bool:
    if member.name in {"METADATA", "PKG-INFO", "RECORD", "WHEEL"}:
        return True
    return member.suffix.casefold() in {
        ".cfg",
        ".css",
        ".html",
        ".js",
        ".json",
        ".lock",
        ".map",
        ".md",
        ".py",
        ".svelte",
        ".toml",
        ".ts",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Mediaforce wheel/sdist privacy and AV1 prior contents."
    )
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument(
        "--source-artifact",
        type=Path,
        default=Path("mediaforce/tuning/data/av1_cold_start_priors_v1.json"),
    )
    parser.add_argument(
        "--source-defaults",
        type=Path,
        default=Path("mediaforce/package_defaults/defaults.toml"),
    )
    args = parser.parse_args()
    summaries = verify_package_archives(
        args.archives,
        source_artifact=args.source_artifact,
        source_defaults=args.source_defaults,
    )
    print(json.dumps({"status": "ok", "archives": summaries}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
