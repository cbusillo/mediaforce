from __future__ import annotations

from pathlib import Path
import time

import pytest

from mediaforce.hosts import status_runtime


def _install_list_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script_body: str) -> None:
    command = tmp_path / "fake-list"
    command.write_text(f"#!/bin/sh\n{script_body}\n")
    command.chmod(0o755)
    monkeypatch.setattr(status_runtime, "_LOCAL_LIST_COMMAND", str(command))
    monkeypatch.setattr(status_runtime, "_LOCAL_LIST_TIMEOUT_SECONDS", 0.2)


def test_directory_whose_listing_never_returns_reads_as_unreadable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_list_command(monkeypatch, tmp_path, "exec /bin/sleep 30")
    started = time.monotonic()

    assert status_runtime._local_path_readable(tmp_path) is False
    assert time.monotonic() - started < 10


def test_unreadable_directory_is_never_opened_for_writing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_list_command(monkeypatch, tmp_path, "exit 1")

    def fail_if_write_checked(_path: Path) -> bool:
        raise AssertionError("write check ran against a directory that failed its read check")

    monkeypatch.setattr(status_runtime, "_local_path_writable", fail_if_write_checked)

    assert status_runtime._local_path_access(tmp_path, check_write=True) == {
        "exists": True,
        "read": False,
        "write": False,
    }


def test_listable_directory_is_readable(tmp_path: Path) -> None:
    assert status_runtime._local_path_readable(tmp_path) is True


def test_missing_path_is_not_readable(tmp_path: Path) -> None:
    assert status_runtime._local_path_readable(tmp_path / "missing") is False
