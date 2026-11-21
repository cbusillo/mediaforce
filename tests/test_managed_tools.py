import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from mediaforce import remote
from mediaforce.hosts import managed_tools as tools
from mediaforce.hosts import tool_versions
from mediaforce.hosts import worker_tools as worker


VERSIONS = {"ffmpeg": "9.0", "ffprobe": "9.0", "ab-av1": "0.11", "svt-av1": "4.3", "opus": "1.6", "libvmaf": "3.2"}


def test_library_drift_blocks_even_when_both_program_versions_match() -> None:
    issue = tools.version_issue(VERSIONS, {**VERSIONS, "libvmaf": "3.0"})
    assert "3.0" in issue and "3.2" in issue
    assert not tools.version_issue(VERSIONS, VERSIONS)


def test_update_waits_for_the_current_file() -> None:
    execute = Mock()
    result = tools.update_when_idle(busy=lambda: True, execute=execute)
    assert not result.ok and "file" in result.message
    execute.assert_not_called()


def test_failed_verification_restores_previous_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old, candidate = tmp_path / "previous", tmp_path / "candidate"
    old.mkdir()
    candidate.mkdir()
    current = tmp_path / "current"
    current.symlink_to(old)
    seen = []

    def previous_versions(_programs: list[str]) -> dict[str, str]:
        seen.append(current.resolve())
        return VERSIONS

    monkeypatch.setattr(tool_versions, "running_versions", previous_versions)

    def verify() -> None:
        seen.append(current.resolve())
        if current.resolve() == candidate:
            raise RuntimeError("sound check failed")

    with pytest.raises(RuntimeError, match="previous"):
        worker.activate(current, candidate, VERSIONS, verify)
    assert current.resolve() == old and seen == [candidate, old]


def test_pinned_ffmpeg_refused_before_homebrew_installs(tmp_path: Path) -> None:
    run = Mock(return_value="ffmpeg-full\n")
    with pytest.raises(RuntimeError, match="pinned"):
        worker.install_mac(tmp_path, VERSIONS, {}, {"ffmpeg"}, run)
    assert [call.args[0] for call in run.call_args_list] == [["brew", "list", "--pinned"]]


def test_linked_package_release_identifies_a_patch_with_an_older_runtime_version(tmp_path: Path) -> None:
    from mediaforce.hosts.tool_versions import library_release

    binary = tmp_path / "lib" / "libvmaf.3.dylib"
    binary.parent.mkdir()
    binary.touch()
    package = {"packages": [{"name": "libvmaf", "versionInfo": "3.2.1"}]}
    (tmp_path / "sbom.spdx.json").write_text(json.dumps(package), encoding="utf-8")
    assert library_release(binary, "libvmaf", "3.2.0") == "3.2.1"


def test_interrupted_switch_refuses_work_even_with_matching_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_versions, "TOOLS_DIRECTORY", tmp_path)
    monkeypatch.setattr(tool_versions, "running_versions", lambda _: VERSIONS)
    (tmp_path / "pending").write_text("null", encoding="utf-8")
    assert json.loads(tool_versions.probe_record())["pending"]


def test_candidate_cannot_take_work_until_the_new_tools_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    previous = {**VERSIONS, "ab-av1": "0.10"}
    existing = tmp_path / "installed"
    existing.mkdir()
    for name in tool_versions.PROGRAMS:
        (existing / name).touch()
    root = tmp_path / "managed"
    current = root / "current"
    monkeypatch.setattr(tool_versions, "TOOLS_DIRECTORY", root)
    monkeypatch.setattr(worker.shutil, "which", lambda program: str(existing / program))
    monkeypatch.setattr(worker, "ensure_idle", lambda: None)
    monkeypatch.setattr(worker.sys, "platform", "linux")

    def install(prefix: Path, _sources: dict, needed: set[str]) -> None:
        assert needed == {"ab-av1"}
        (prefix / "bin/ab-av1").touch()

    def inspect(programs: list[str]) -> dict[str, str]:
        return VERSIONS if current.is_symlink() or Path(programs[0]).is_absolute() else previous

    def check(_quality: list[str]) -> None:
        assert (root / "pending").is_file()
        if current.is_symlink():
            raise RuntimeError("test sound could not be decoded")

    monkeypatch.setattr(worker, "install_linux", install)
    monkeypatch.setattr(tool_versions, "running_versions", inspect)
    monkeypatch.setattr(worker, "self_test", check)
    with pytest.raises(RuntimeError, match="Restored the previous media tools.*sound"):
        worker.update(VERSIONS, {}, [])
    assert not current.is_symlink() and not (root / "pending").exists()
    assert all((existing / name).is_file() for name in tool_versions.PROGRAMS)


def test_fresh_readiness_holds_an_otherwise_capable_computer_after_library_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Mock()
    config.explicit_source_root_map_for_host.return_value = {}
    host = {"host": "test@version-drift.invalid", "media_access": "stream", "capabilities": ["encode_queue"]}
    versions = VERSIONS.copy()

    def probe(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        reply = ["tool|ffmpeg|1", "tool|ffmpeg_libsvtav1|1", "tool|ffmpeg_videotoolbox|0",
                 "tool|ffmpeg_libvmaf|1", "tool|ffmpeg_xpsnr|1", "tool|ab_av1|1", "meta|platform|linux",
                 "meta|managed_tools|" + json.dumps({"versions": versions})]
        return subprocess.CompletedProcess([], 0, stdout="\n".join(reply), stderr="")

    monkeypatch.setattr(tools, "controller_versions", lambda: VERSIONS)
    monkeypatch.setattr(remote, "_run_remote_status_probe", probe)
    monkeypatch.setattr(remote, "_learn_remote_wake_mac", lambda *_: None)
    assert remote.probe_host_status(config, host).available
    versions["libvmaf"] = "3.0"
    status = remote.probe_host_status(config, host)
    assert not status.available and status.can_update_tools
    assert status.detail and "libvmaf" in status.detail
