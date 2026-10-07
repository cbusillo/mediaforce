import ctypes
import errno
from pathlib import Path
import struct
import sys
from typing import Any
from unittest.mock import Mock

import pytest

from mediaforce.ops import dev_frontend


@pytest.mark.parametrize("arguments,owned", [
    (["node", "--max-old-space-size=128", "VITE", "dev"], True),
    (["node", "--max-old-space-size", "128", "VITE"], True),
    (["/removed/node runtime/node", "--inspect-brk=0", "VITE"], True),
    (["/removed/node runtime/node", "--max-old-space-size=128", "VITE"], True),
    (["node", "--inspect=0", "--max-old-space-size=128", "--", "VITE"], True),
    (["node", "--max-old-space-size=128", "shared-wrapper.js", "VITE"], False),
    (["node", "shared-wrapper.js /removed/node runtime/node", "VITE"], False),
    (["node", "--max-old-space-size=128 shared-wrapper.js", "VITE"], False),
    (["node", "--max-old-space-size", "shared-wrapper.js", "VITE"], False),
    (["node", "--max-old-space-size=no", "VITE"], False),
    (["node", "--require", "VITE"], False),
    (["node", "--eval", "VITE"], False),
    (["node", "--unknown", "VITE"], False),
    (["node", "--max-old-space-size=128"], False),
    (["python3.13", "VITE"], True),
    (["python3.13", "wrapper.py", "VITE"], False),
    (["npm", "run", "dev"], True),
    (["node", "/bin/npm-cli.js", "run", "dev"], True),
    (["npm run dev --host 127.0.0.1", "", ""], True),
    (["npm run dev", "shared-wrapper.js"], False),
    (["npm run development"], False),
    (["node VITE"], False),
    ([], False),
])
def test_launcher_uses_native_argument_boundaries(arguments: list[str], owned: bool) -> None:
    checkout = "/workspace/Python Projects/mediaforce"
    arguments = [part.replace("VITE", checkout + "/frontend/node_modules/.bin/vite") for part in arguments]
    assert dev_frontend.matches_frontend(arguments, checkout, checkout + "/frontend") == owned
    assert not dev_frontend.matches_frontend(arguments, checkout, checkout + "-sibling/frontend")


def test_identical_ps_text_does_not_make_wrapper_a_launcher() -> None:
    checkout = "/checkout with spaces"
    vite = checkout + "/frontend/node_modules/.bin/vite"
    owned = ["/removed/node wrapper/node", vite]
    foreign = ["/removed/node", "wrapper/node", vite]
    assert " ".join(owned) == " ".join(foreign)
    assert dev_frontend.matches_frontend(owned, checkout, checkout + "/frontend")
    assert not dev_frontend.matches_frontend(foreign, checkout, checkout + "/frontend")


@pytest.mark.parametrize("prefix,cwd,owned", [
    ("frontend", "/checkout with spaces", True),
    ("frontend", "/checkout with spaces/frontend", False),
    ("/checkout with spaces/frontend", "/checkout with spaces", True),
    ("/checkout with spaces/frontend", "/checkout with spaces/frontend", True),
    ("/checkout with spaces-sibling/frontend", "/checkout with spaces", False),
])
def test_npm_prefix_arguments_are_exact(prefix: str, cwd: str, owned: bool) -> None:
    assert dev_frontend.matches_frontend(["node", "/bin/npm", "--prefix", prefix, "run", "dev"],
                                        "/checkout with spaces", cwd) == owned


def darwin_buffer(arguments: list[bytes]) -> bytes:
    return struct.pack("=i", len(arguments)) + b"/resolved/node\0\0\0" + b"\0".join(arguments) + b"\0ENV=private\0"


def test_darwin_reader_preserves_spaces_empty_args_and_excludes_environment() -> None:
    arguments = [b"/removed/node runtime/node", b"--inspect-brk=0", b"/checkout with spaces/vite", b"", b"\xff"]
    assert dev_frontend.decode_darwin_arguments(darwin_buffer(arguments)) == [
        "/removed/node runtime/node", "--inspect-brk=0", "/checkout with spaces/vite", "", "\udcff",
    ]


@pytest.mark.parametrize("data", [b"", struct.pack("=i", 0), struct.pack("=i", -1),
                                  struct.pack("=i", 100), struct.pack("=i", 2) + b"/node\0node\0vite"])
def test_darwin_reader_rejects_incomplete_arguments(data: bytes) -> None:
    with pytest.raises(ValueError):
        dev_frontend.decode_darwin_arguments(data)


def test_darwin_sysctl_reads_the_returned_size(monkeypatch: pytest.MonkeyPatch) -> None:
    data = darwin_buffer([b"/removed/node runtime/node", b"vite"])
    libc = Mock()

    def capacity(_name: bytes, output: Any, _size: Any, _new: object, _length: int) -> int:
        ctypes.cast(output, ctypes.POINTER(ctypes.c_int))[0] = 1024
        return 0

    def arguments(mib: ctypes.Array, length: int, output: Any, size: Any, _new: object, _length: int) -> int:
        assert list(mib) == [1, 49, 4242]
        assert length == 3
        ctypes.memmove(output, data, len(data))
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = len(data)
        return 0

    libc.sysctlbyname.side_effect = capacity
    libc.sysctl.side_effect = arguments
    monkeypatch.setattr(dev_frontend.ctypes, "CDLL", Mock(return_value=libc))
    assert dev_frontend.darwin_arguments(4242) == ["/removed/node runtime/node", "vite"]


def test_darwin_sysctl_failure_is_not_a_ps_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    libc = Mock()
    def refused(*_args: object) -> int:
        ctypes.set_errno(errno.EACCES)
        return -1

    libc.sysctlbyname.side_effect = refused
    monkeypatch.setattr(dev_frontend.ctypes, "CDLL", Mock(return_value=libc))
    previous_errno = ctypes.get_errno()
    try:
        with pytest.raises(PermissionError):
            dev_frontend.darwin_arguments(4242)
    finally:
        ctypes.set_errno(previous_errno)


@pytest.mark.parametrize("data,expected", [
    (b"/removed/node runtime/node\0--max-old-space-size=128\0/checkout with spaces/vite\0\0",
     ["/removed/node runtime/node", "--max-old-space-size=128", "/checkout with spaces/vite", ""]),
    (b"npm run dev --host 127.0.0.1\0\0\0", ["npm run dev --host 127.0.0.1", "", ""]),
    (b"", []),
])
def test_linux_reader_preserves_native_boundaries(monkeypatch: pytest.MonkeyPatch, data: bytes, expected: list[str]) -> None:
    monkeypatch.setattr(dev_frontend.sys, "platform", "linux")

    def read(path: Path) -> bytes:
        assert str(path) == "/proc/4242/cmdline"
        return data

    monkeypatch.setattr(Path, "read_bytes", read)
    assert dev_frontend.process_arguments(4242) == expected


def test_linux_reader_rejects_unterminated_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dev_frontend.sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_bytes", lambda _path: b"node\0vite")
    with pytest.raises(ValueError):
        dev_frontend.process_arguments(4242)


@pytest.mark.parametrize("error,status", [(PermissionError(), 2), (ValueError(), 2), (ProcessLookupError(), 1)])
def test_argument_read_outcome_is_explicit(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception, status: int) -> None:
    monkeypatch.setattr(sys, "argv", ["dev_frontend.py", "4242", "/checkout", "/checkout/frontend"])
    monkeypatch.setattr(dev_frontend, "process_arguments", Mock(side_effect=error))
    monkeypatch.setattr(dev_frontend.os, "kill", Mock())
    assert dev_frontend.main() == status
    assert ("ownership unknown" in capsys.readouterr().err) == (status == 2)


@pytest.mark.parametrize("exited", [False, True])
def test_einval_requires_independent_exit_evidence(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exited: bool) -> None:
    monkeypatch.setattr(sys, "argv", ["dev_frontend.py", "4242", "/checkout", "/checkout/frontend"])
    monkeypatch.setattr(dev_frontend, "process_arguments", Mock(side_effect=OSError(errno.EINVAL, "native read failed")))
    alive = Mock(side_effect=ProcessLookupError() if exited else None)
    monkeypatch.setattr(dev_frontend.os, "kill", alive)
    assert dev_frontend.main() == (1 if exited else 2)
    alive.assert_called_once_with(4242, 0)
    assert ("ownership unknown" in capsys.readouterr().err) == (not exited)


def test_pid_one_is_invalid_without_native_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["dev_frontend.py", "1", "/checkout", "/checkout/frontend"])
    arguments, alive = Mock(), Mock()
    monkeypatch.setattr(dev_frontend, "process_arguments", arguments)
    monkeypatch.setattr(dev_frontend.os, "kill", alive)
    assert dev_frontend.main() == 2
    arguments.assert_not_called()
    alive.assert_not_called()
