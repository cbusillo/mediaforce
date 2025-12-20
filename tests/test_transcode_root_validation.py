import pathlib
import platform

from mediaforce.config.settings import AppSettings, LibrarySettings
from mediaforce.web.app import _validate_transcode_root


def _host_paths() -> tuple[str, str, str]:
    is_mac = platform.system() == "Darwin"
    media_root = "/Volumes/media" if is_mac else "/mnt/media"
    alt_media_root = "/mnt/media" if is_mac else "/Volumes/media"
    library_root = f"{media_root}/tv"
    alt_library_root = f"{alt_media_root}/tv"
    return media_root, library_root, alt_library_root


def test_validate_allows_library_root_transcode():
    media_root, library_root, alt_library_root = _host_paths()
    settings = AppSettings(
        libraries=[
            LibrarySettings(
                id="tv",
                name="TV",
                media_type="tv",
                mac_path=f"{media_root}/tv" if media_root.startswith("/Volumes/") else alt_library_root,
                linux_path=f"{media_root}/tv" if media_root.startswith("/mnt/") else alt_library_root,
            )
        ]
    )

    ok, reason = _validate_transcode_root(pathlib.Path(f"{library_root}/transcode"), settings=settings)
    assert ok, reason


def test_validate_rejects_library_root_itself():
    _, library_root, _ = _host_paths()
    settings = AppSettings(
        libraries=[
            LibrarySettings(
                id="tv",
                name="TV",
                media_type="tv",
                mac_path=library_root,
                linux_path=library_root,
            )
        ]
    )

    ok, reason = _validate_transcode_root(pathlib.Path(library_root), settings=settings)
    assert ok is False
    assert "dedicated" in reason.lower()


def test_validate_allows_media_root_transcode():
    media_root, _, _ = _host_paths()
    ok, reason = _validate_transcode_root(pathlib.Path(f"{media_root}/transcode"))
    assert ok, reason


def test_validate_accepts_other_host_mount_scheme():
    media_root, library_root, alt_library_root = _host_paths()
    settings = AppSettings(
        libraries=[
            LibrarySettings(
                id="tv",
                name="TV",
                media_type="tv",
                mac_path=f"{media_root}/tv" if media_root.startswith("/Volumes/") else alt_library_root,
                linux_path=f"{media_root}/tv" if media_root.startswith("/mnt/") else alt_library_root,
            )
        ]
    )

    ok, reason = _validate_transcode_root(pathlib.Path(f"{alt_library_root}/transcode"), settings=settings)
    assert ok, reason
