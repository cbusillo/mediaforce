from __future__ import annotations

import pathlib
import platform
from typing import Optional

from mediaforce.config.settings import AppSettings, LibrarySettings, load_app_settings


MEDIA_ROOTS_MAC = ["/Volumes/media", "/Volumes/extras"]
MEDIA_ROOTS_LINUX = ["/mnt/media", "/mnt/extras"]


def get_media_roots() -> list[str]:
    """Return coarse mount roots for the current platform."""

    if platform.system() == "Darwin":
        return MEDIA_ROOTS_MAC
    return MEDIA_ROOTS_LINUX


def normalize_path(path: pathlib.Path) -> pathlib.Path:
    """Normalize path between macOS (/Volumes/...) and Linux (/mnt/...) mount points."""

    if path.exists():
        return path

    path_str = str(path)

    for mac_root in MEDIA_ROOTS_MAC:
        if path_str.startswith(mac_root):
            linux_root = mac_root.replace("/Volumes/", "/mnt/")
            candidate = pathlib.Path(path_str.replace(mac_root, linux_root, 1))
            if candidate.exists():
                return candidate

    for linux_root in MEDIA_ROOTS_LINUX:
        if path_str.startswith(linux_root):
            mac_root = linux_root.replace("/mnt/", "/Volumes/")
            candidate = pathlib.Path(path_str.replace(linux_root, mac_root, 1))
            if candidate.exists():
                return candidate

    return path


def iter_libraries_for_current_host(
    settings: Optional[AppSettings] = None,
) -> list[tuple[LibrarySettings, pathlib.Path]]:
    """Return (library, resolved root path) for the current OS."""

    if settings is None:
        settings = load_app_settings()

    is_mac = platform.system() == "Darwin"
    result: list[tuple[LibrarySettings, pathlib.Path]] = []
    for lib in settings.libraries:
        root = lib.mac_path if is_mac else lib.linux_path
        if not root:
            continue
        result.append((lib, pathlib.Path(root)))
    return result


def get_library_root(path: pathlib.Path) -> pathlib.Path:
    """Infer the library root (tv, movies, etc.) for a given path."""

    path = path.resolve()
    for root in get_media_roots():
        root_path = pathlib.Path(root)
        if str(path).startswith(str(root_path)):
            rel = path.relative_to(root_path)
            parts = rel.parts
            if parts:
                return root_path / parts[0]

    if path.is_dir():
        return path
    return path.parent


def find_library_for_path(
    path: pathlib.Path,
    settings: Optional[AppSettings] = None,
) -> tuple[Optional[LibrarySettings], Optional[pathlib.Path]]:
    """Match a filesystem path against configured libraries."""

    resolved = path.resolve()
    if settings is None:
        settings = load_app_settings()

    for lib, root in iter_libraries_for_current_host(settings):
        try:
            resolved.relative_to(root)
            return lib, root
        except ValueError:
            continue
    return None, None

