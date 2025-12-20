from mediaforce.config.paths import (
    canonicalize_mount_prefix_for_current_host,
    find_library_for_path,
    iter_libraries_for_current_host,
)
from mediaforce.config.settings import AppSettings, LibrarySettings


def test_iter_libraries_for_current_host_selects_mac_paths(monkeypatch, tmp_path):
    from mediaforce.config import paths as paths_mod

    monkeypatch.setattr(paths_mod.platform, "system", lambda: "Darwin")
    settings = AppSettings(
        libraries=[
            LibrarySettings(
                id="tv",
                name="TV",
                media_type="tv",
                mac_path=str(tmp_path / "tv"),
                linux_path="/mnt/media/tv",
            )
        ]
    )
    libs = iter_libraries_for_current_host(settings)
    assert libs[0][0].id == "tv"
    assert str(libs[0][1]).endswith("/tv")


def test_iter_libraries_for_current_host_selects_linux_paths(monkeypatch, tmp_path):
    from mediaforce.config import paths as paths_mod

    monkeypatch.setattr(paths_mod.platform, "system", lambda: "Linux")
    settings = AppSettings(
        libraries=[
            LibrarySettings(
                id="tv",
                name="TV",
                media_type="tv",
                mac_path=str(tmp_path / "tv"),
                linux_path="/mnt/media/tv",
            )
        ]
    )
    libs = iter_libraries_for_current_host(settings)
    assert str(libs[0][1]) == "/mnt/media/tv"


def test_find_library_for_path_matches_configured_root(monkeypatch, tmp_path):
    from mediaforce.config import paths as paths_mod

    monkeypatch.setattr(paths_mod.platform, "system", lambda: "Darwin")
    root = tmp_path / "tv"
    target = root / "ShowA" / "Season 1" / "E01.mkv"

    settings = AppSettings(
        libraries=[
            LibrarySettings(
                id="tv",
                name="TV",
                media_type="tv",
                mac_path=str(root),
                linux_path="/mnt/media/tv",
            )
        ]
    )
    lib, resolved = find_library_for_path(target, settings)
    assert lib is not None
    assert lib.id == "tv"
    assert resolved is not None
    assert str(resolved) == str(root)


def test_canonicalize_mount_prefix_for_current_host_linux(monkeypatch):
    from mediaforce.config import paths as paths_mod

    monkeypatch.setattr(paths_mod.platform, "system", lambda: "Linux")
    out = canonicalize_mount_prefix_for_current_host(paths_mod.pathlib.Path("/Volumes/media/tv"))
    assert str(out) == "/mnt/media/tv"


def test_canonicalize_mount_prefix_for_current_host_mac(monkeypatch):
    from mediaforce.config import paths as paths_mod

    monkeypatch.setattr(paths_mod.platform, "system", lambda: "Darwin")
    out = canonicalize_mount_prefix_for_current_host(paths_mod.pathlib.Path("/mnt/media/tv"))
    assert str(out) == "/Volumes/media/tv"
