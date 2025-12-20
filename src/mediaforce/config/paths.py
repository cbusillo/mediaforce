import pathlib
import platform
from typing import Optional

from mediaforce.config.settings import AppSettings, LibrarySettings, load_app_settings


MEDIA_ROOTS_MAC = ["/Volumes/media", "/Volumes/extras"]
MEDIA_ROOTS_LINUX = ["/mnt/media", "/mnt/extras"]


def get_media_roots() -> list[str]:
    if platform.system() == "Darwin":
        return MEDIA_ROOTS_MAC
    return MEDIA_ROOTS_LINUX


def default_transcode_root() -> str:
    roots = get_media_roots()
    if not roots:
        return "transcode"
    return str(pathlib.Path(roots[0]) / "transcode")


def normalize_path(path: pathlib.Path) -> pathlib.Path:
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


def canonicalize_mount_prefix_for_current_host(path: pathlib.Path) -> pathlib.Path:
    """Swap /Volumes/... <-> /mnt/... prefixes based on current host OS.

    Unlike `normalize_path`, this does not require the target path to exist. This is
    useful when creating directories (e.g., transcode roots) where the other-host
    mount style would otherwise cause writes under a non-existent prefix.
    """

    raw = str(path)
    is_mac = platform.system() == "Darwin"

    if is_mac:
        for linux_root in MEDIA_ROOTS_LINUX:
            mac_root = linux_root.replace("/mnt/", "/Volumes/")
            if raw == linux_root or raw.startswith(linux_root.rstrip("/") + "/"):
                return pathlib.Path(raw.replace(linux_root, mac_root, 1))
        return path

    for mac_root in MEDIA_ROOTS_MAC:
        linux_root = mac_root.replace("/Volumes/", "/mnt/")
        if raw == mac_root or raw.startswith(mac_root.rstrip("/") + "/"):
            return pathlib.Path(raw.replace(mac_root, linux_root, 1))
    return path


def iter_libraries_for_current_host(
    settings: Optional[AppSettings] = None,
) -> list[tuple[LibrarySettings, pathlib.Path]]:
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


def get_db_path(_: Optional[pathlib.Path] = None) -> pathlib.Path:
    from mediaforce.config.settings import INVENTORY_DB
    return INVENTORY_DB


def resolve_target_height_for_path(path: pathlib.Path, settings: AppSettings) -> tuple[Optional[int], str]:
    path_str = str(path)
    for lib in settings.libraries:
        roots = [lib.mac_path, lib.linux_path]
        for root in roots:
            if not root:
                continue
            try:
                if path.is_relative_to(pathlib.Path(root)):
                    if lib.max_height is not None:
                        return lib.max_height, f"library:{lib.id}"
                    return settings.global_max_height, "global"
            except Exception:
                if path_str.startswith(root.rstrip("/") + "/") or path_str == root:
                    if lib.max_height is not None:
                        return lib.max_height, f"library:{lib.id}"
                    return settings.global_max_height, "global"

    if settings.global_max_height is not None:
        return settings.global_max_height, "global"

    return None, "none"


def get_transcode_output_path(source_path: pathlib.Path, transcode_root: pathlib.Path) -> Optional[pathlib.Path]:
    source_str = str(source_path)
    rel_path = None
    for root in get_media_roots():
        if source_str.startswith(root):
            rel_path = source_path.relative_to(root)
            break

    stem = source_path.stem

    if rel_path:
        output_dir = transcode_root / rel_path.parent
        output_path = output_dir / f"{stem}.AV1.mp4"
        if output_path.exists():
            return output_path

    flat_output = transcode_root / f"{stem}.AV1.mp4"
    if flat_output.exists():
        return flat_output

    stem_stripped = stem
    for marker in [".x264", ".x265", ".h264", ".h265", ".HEVC", ".AVC", ".H.264", ".H.265"]:
        stem_stripped = stem_stripped.replace(marker, "")

    if stem_stripped != stem:
        if rel_path:
            output_dir = transcode_root / rel_path.parent
            output_path = output_dir / f"{stem_stripped}.AV1.mp4"
            if output_path.exists():
                return output_path

        flat_output = transcode_root / f"{stem_stripped}.AV1.mp4"
        if flat_output.exists():
            return flat_output

    return None
