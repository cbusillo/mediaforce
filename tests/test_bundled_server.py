from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
from unittest.mock import patch

import pytest

from mediaforce.ops.login_item import LoginItemError, LoginItemPaths, install_login_item
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
    assert payload["ProgramArguments"] == [str(bundle_paths.program), "--no-reload", "--config", str(bundle_paths.config_path)]
    assert Path(payload["WorkingDirectory"]) == bundle_paths.working_directory
    assert not bundle_paths.working_directory.is_relative_to(bundle_paths.project_root)
    assert not (bundle_paths.project_root / ".env").exists()


def test_bundle_install_cannot_replace_existing_runtime(bundle_paths: LoginItemPaths) -> None:
    bundle_paths.plist.parent.mkdir(parents=True)
    previous = plistlib.dumps({"ProgramArguments": ["/previous/server"]})
    bundle_paths.plist.write_bytes(previous)
    with patch("mediaforce.ops.login_item._require_supported_platform"), patch(
        "mediaforce.ops.login_item._run_command", return_value=subprocess.CompletedProcess([], 0)
    ), pytest.raises(LoginItemError, match="safe update transaction"):
        install_login_item(bundle_paths)
    assert bundle_paths.plist.read_bytes() == previous


def test_bundle_signature_failure_preserves_service(bundle_paths: LoginItemPaths) -> None:
    with patch("mediaforce.ops.login_item._require_supported_platform"), patch(
        "mediaforce.ops.login_item._run_command", return_value=subprocess.CompletedProcess([], 1, stderr="invalid signature")
    ), pytest.raises(LoginItemError, match="invalid signature"):
        install_login_item(bundle_paths)
    assert not bundle_paths.plist.exists()


def test_explicit_credentials_are_read_without_clone_lookup(tmp_path: Path) -> None:
    credentials = tmp_path / "saved.env"
    credentials.write_text("MEDIAFORCE_TEST_VALUE=from-saved-path\n")
    with patch.dict(os.environ, {"MEDIAFORCE_ENV_FILE": str(credentials)}, clear=True):
        _load_project_env_file()
        assert os.environ["MEDIAFORCE_TEST_VALUE"] == "from-saved-path"
    assert credentials.read_text() == "MEDIAFORCE_TEST_VALUE=from-saved-path\n"


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
            return commands
        return f"/Users/example/build/Mediaforce:\n\t{library} (compatibility version 1.0.0)\n"

    with patch("scripts.bundle_server.run", side_effect=inspect):
        verify_native_target(Path("Mediaforce"), {"arm64", "x86_64"}, "15.0")
        library = "/Volumes/build/libprivate.dylib"
        with pytest.raises(ValueError, match="not relocatable"):
            verify_native_target(Path("Mediaforce"), {"arm64"}, "15.0")
