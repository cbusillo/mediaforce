from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
from unittest.mock import patch

import pytest

from mediaforce.ops.login_item import LoginItemError, LoginItemPaths, install_login_item, run_login_item_command, verify_bundled_startup
from mediaforce.web.app import _load_project_env_file
from scripts.verify_package_contents import PackageContentsError, verify_package_archives
from scripts.bundle_server import verify_native_target


@pytest.fixture
def bundle_paths(tmp_path: Path) -> LoginItemPaths:
    bundle = tmp_path / "Mediaforce.app"
    program = bundle / "Contents/MacOS/mediaforce-server"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    (program.parent / "Mediaforce").write_bytes(b"signed-app-fixture")
    support = tmp_path / "support"
    config = tmp_path / "config/custom.toml"
    config.parent.mkdir()
    config.write_text(
        f'[state]\ndb_path = "{support}/library.sqlite3"\nrun_manifest_dir = "{support}/runs"\n'
        f'web_state_dir = "{support}/web"\n[media]\nsource_roots = {{}}\n'
    )
    return LoginItemPaths(bundle, tmp_path / "home", config)


def test_bundle_service_uses_saved_config_and_writable_directory(bundle_paths: LoginItemPaths) -> None:
    with patch("mediaforce.ops.login_item._require_supported_platform"), patch(
        "mediaforce.ops.login_item._run_command", return_value=subprocess.CompletedProcess([], 0)
    ):
        assert install_login_item(bundle_paths) == "installed"
        assert install_login_item(bundle_paths) == "unchanged"
    payload = plistlib.loads(bundle_paths.plist.read_bytes())
    arguments = payload["ProgramArguments"]
    assert Path(arguments[0]).is_relative_to(bundle_paths.project_root)
    assert Path(arguments[arguments.index("--config") + 1]) == bundle_paths.config_path
    assert Path(payload["WorkingDirectory"]) == bundle_paths.working_directory


def test_bundle_install_cannot_replace_existing_runtime(bundle_paths: LoginItemPaths) -> None:
    bundle_paths.plist.parent.mkdir(parents=True)
    previous = plistlib.dumps({"ProgramArguments": ["/previous/server"]})
    bundle_paths.plist.write_bytes(previous)
    with patch("mediaforce.ops.login_item._require_supported_platform"), patch(
        "mediaforce.ops.login_item._run_command", return_value=subprocess.CompletedProcess([], 0)
    ), pytest.raises(LoginItemError):
        install_login_item(bundle_paths)
    assert bundle_paths.plist.read_bytes() == previous


def test_bundle_signature_failure_preserves_service(bundle_paths: LoginItemPaths) -> None:
    with patch("mediaforce.ops.login_item._require_supported_platform"), patch(
        "mediaforce.ops.login_item._run_command", return_value=subprocess.CompletedProcess([], 1, stderr="invalid signature")
    ), pytest.raises(LoginItemError, match="invalid signature"):
        install_login_item(bundle_paths)
    assert not bundle_paths.plist.exists()


def test_explicit_credentials_supply_environment_defaults(tmp_path: Path) -> None:
    credentials = tmp_path / "saved.env"
    credentials.write_text("MEDIAFORCE_TEST_VALUE=from-saved-path\n")
    with patch.dict(os.environ, {}, clear=True):
        _load_project_env_file(credentials)
        assert os.environ["MEDIAFORCE_TEST_VALUE"] == "from-saved-path"


@pytest.mark.parametrize("explicit", [False, True])
def test_bundled_web_uses_external_credentials_for_the_custom_database(bundle_paths: LoginItemPaths, explicit: bool) -> None:
    from mediaforce.web import app as web_app

    support = bundle_paths.working_directory
    support.mkdir()
    (support / ".env").write_text("MEDIAFORCE_TEST_VALUE=beside-database\n")
    credentials = support / "saved-credentials.env"
    credentials.write_text("MEDIAFORCE_TEST_VALUE=explicit-source\n")
    arguments = ["--config", str(bundle_paths.config_path)]
    if explicit:
        arguments.extend(["--credentials", str(credentials)])
    with patch.dict(os.environ, {}, clear=True), patch(
        "mediaforce.ops.runtime_install.bundled_app_path", return_value=bundle_paths.project_root
    ), patch("mediaforce.ops.login_item.verify_bundled_startup"), patch.object(
        web_app, "DEFAULT_CONFIG_PATH", bundle_paths.home / "legacy/config/defaults.toml"
    ), patch.object(web_app, "create_app", return_value=object()), patch.object(web_app.uvicorn, "run"):
        web_app.main(arguments)
        assert os.environ["MEDIAFORCE_TEST_VALUE"] == ("explicit-source" if explicit else "beside-database")


def test_app_verifier_checks_defaults_and_rejects_private_files(tmp_path: Path) -> None:
    app = tmp_path / "Mediaforce.app"
    server = app / "Contents/Resources/server/app"
    defaults = server / "mediaforce/package_defaults/defaults.toml"
    defaults.parent.mkdir(parents=True)
    defaults.write_text("[media.source_roots]\n")
    verify_package_archives([app], source_defaults=defaults)
    private = server / ".env"
    private.write_text("TOKEN=private\n")
    with pytest.raises(PackageContentsError, match="forbidden"):
        verify_package_archives([app], source_defaults=defaults)
    private.unlink()
    (server / "outside").symlink_to(tmp_path)
    with pytest.raises(PackageContentsError, match="escapes"):
        verify_package_archives([app], source_defaults=defaults)


def test_native_qualification_parses_dependencies_and_os_versions() -> None:
    commands = "cmd LC_BUILD_VERSION\n minos 15.0.0\n tool LD\n version 27037.1\n"
    library = "/System/Library/Frameworks/Foundation.framework/Foundation"

    def inspect(*arguments: str | Path) -> str:
        if arguments[0] == "lipo":
            return "arm64 x86_64"
        if arguments[1] == "-l":
            return commands + f"cmd LC_LOAD_DYLIB\n cmdsize 80\n name {library} (offset 24)\ncmd LC_RPATH\n cmdsize 40\n path /usr/lib/swift (offset 12)\n"
        raise AssertionError(arguments)

    with patch("scripts.bundle_server.run", side_effect=inspect):
        verify_native_target(Path("Mediaforce"), {"arm64", "x86_64"}, "15.0")
        library = "/Volumes/build/libprivate.dylib"
        with pytest.raises(ValueError):
            verify_native_target(Path("Mediaforce"), {"arm64"}, "15.0")


def test_app_vendor_examples_do_not_hide_current_home_leaks(tmp_path: Path) -> None:
    app = tmp_path / "Mediaforce.app"
    vendor = app / "Contents/Resources/server/arm64/vendor.py"
    vendor.parent.mkdir(parents=True)
    with patch.object(Path, "home", return_value=Path("/home/private-owner")):
        vendor.write_text('example = "/Users/someone/demo"\n')
        verify_package_archives([app])
        vendor.write_text('generated = "/home/private-owner/private"\n')
        with pytest.raises(PackageContentsError):
            verify_package_archives([app])
        vendor.write_text("safe vendor file")
        binary = app / "Contents/MacOS/Mediaforce"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"\x00/home/private-owner/private\x00")
        with pytest.raises(PackageContentsError):
            verify_package_archives([app])


def test_moved_bundle_can_repair_only_the_same_saved_app(bundle_paths: LoginItemPaths) -> None:
    with patch("mediaforce.ops.login_item._require_supported_platform"), patch(
        "mediaforce.ops.login_item._run_command", return_value=subprocess.CompletedProcess([], 0)
    ):
        install_login_item(bundle_paths)
        moved = bundle_paths.project_root.with_name("Moved.app")
        bundle_paths.project_root.rename(moved)
        assert run_login_item_command("install", project_root=moved, home=bundle_paths.home) == 0
        installed = plistlib.loads(bundle_paths.plist.read_bytes())
        assert Path(installed["ProgramArguments"][0]).is_relative_to(moved)
        saved = bundle_paths.plist.read_bytes()
        (moved / "Contents/MacOS/Mediaforce").write_bytes(b"different-build")
        assert run_login_item_command("install", project_root=moved, home=bundle_paths.home) == 1
        assert bundle_paths.plist.read_bytes() == saved


def test_conflicting_config_is_refused_and_missing_config_does_not_block_disable(bundle_paths: LoginItemPaths) -> None:
    config_path = bundle_paths.config_path
    assert config_path is not None
    with patch("mediaforce.ops.login_item._require_supported_platform"), patch(
        "mediaforce.ops.login_item._run_command", return_value=subprocess.CompletedProcess([], 0)
    ), patch("mediaforce.ops.login_item.disable_login_item") as disable:
        install_login_item(bundle_paths)
        saved = bundle_paths.plist.read_bytes()
        assert run_login_item_command("install", project_root=bundle_paths.project_root, home=bundle_paths.home,
                                      config_path=config_path.with_name("other.toml")) == 1
        assert bundle_paths.plist.read_bytes() == saved
        config_path.unlink()
        assert run_login_item_command("disable", project_root=bundle_paths.project_root, home=bundle_paths.home) == 0
        disable.assert_called_once()


def test_replaced_bundle_refuses_startup_before_database_access(bundle_paths: LoginItemPaths) -> None:
    with patch("mediaforce.ops.login_item._require_supported_platform"), patch(
        "mediaforce.ops.login_item._run_command", return_value=subprocess.CompletedProcess([], 0)
    ):
        install_login_item(bundle_paths)
        verify_bundled_startup(bundle_paths.project_root, home=bundle_paths.home)
        (bundle_paths.program.parent / "Mediaforce").write_bytes(b"replaced-build")
        with pytest.raises(LoginItemError):
            verify_bundled_startup(bundle_paths.project_root, home=bundle_paths.home)
