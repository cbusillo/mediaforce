from __future__ import annotations

from pathlib import Path
import time

import pytest

from mediaforce.hosts import status_runtime


def _hanging_command(tmp_path: Path) -> str:
    command = tmp_path / "never-returns"
    command.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
    command.chmod(0o755)
    return str(command)


def test_directory_whose_listing_never_returns_is_unreadable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(status_runtime, "_LOCAL_LIST_COMMAND", _hanging_command(tmp_path))
    monkeypatch.setattr(status_runtime, "_LOCAL_CHECK_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()

    assert status_runtime._local_path_readable(tmp_path) is False
    assert time.monotonic() - started < 10


def test_directory_whose_write_never_returns_is_unwritable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(status_runtime, "_LOCAL_SHELL", _hanging_command(tmp_path))
    monkeypatch.setattr(status_runtime, "_LOCAL_CHECK_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()

    assert status_runtime._local_path_writable(tmp_path) is False
    assert time.monotonic() - started < 10


def test_directory_passes_both_checks_and_leaves_nothing_behind(tmp_path: Path) -> None:
    assert status_runtime._local_path_readable(tmp_path) is True
    assert status_runtime._local_path_writable(tmp_path) is True
    assert list(tmp_path.iterdir()) == []


def test_missing_path_passes_neither_check(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    assert status_runtime._local_path_readable(missing) is False
    assert status_runtime._local_path_writable(missing) is False
