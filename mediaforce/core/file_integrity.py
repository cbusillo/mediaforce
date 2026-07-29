from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import select as select_module
import shutil
import stat
import tempfile
from typing import Any


MACOS_FILE_INTEGRITY_CONTRACT_VERSION = "macos-kqueue-path-chain-v1"

_REQUIRED_KQUEUE_NAMES = (
    "kqueue",
    "kevent",
    "KQ_FILTER_VNODE",
    "KQ_EV_ADD",
    "KQ_EV_CLEAR",
    "KQ_NOTE_WRITE",
    "KQ_NOTE_DELETE",
    "KQ_NOTE_RENAME",
    "KQ_NOTE_LINK",
    "KQ_NOTE_REVOKE",
    "KQ_NOTE_ATTRIB",
    "KQ_NOTE_EXTEND",
)


class FileIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _PathBinding:
    path: Path
    descriptor: int
    device: int
    inode: int


class MacOSFileIntegrityGuard:
    def __init__(
            self,
            *,
            path: Path,
            descriptor: int,
            require_single_link: bool,
    ) -> None:
        assert_macos_file_integrity_capability()
        self._path = path.expanduser().resolve(strict=True)
        self._descriptor = descriptor
        self._require_single_link = require_single_link
        self._directory_bindings: list[_PathBinding] = []
        self._watcher: Any | None = None
        self._violated = False
        try:
            self._open_directory_bindings()
            self._watcher = select_module.kqueue()
            self._register_watchers()
            self.assert_quiet()
        except BaseException:
            self.close()
            raise

    @property
    def path(self) -> Path:
        return self._path

    def assert_quiet(self, *, timeout_seconds: float = 0.0) -> None:
        watcher = self._watcher
        if watcher is None:
            self._violated = True
        else:
            try:
                if watcher.control(None, 32, timeout_seconds):
                    self._violated = True
            except OSError:
                self._violated = True
        if not self._current_bindings_match():
            self._violated = True
        if self._violated:
            raise FileIntegrityError("guarded file or path changed")

    def close(self) -> None:
        if self._watcher is not None:
            self._watcher.close()
            self._watcher = None
        for binding in reversed(self._directory_bindings):
            os.close(binding.descriptor)
        self._directory_bindings.clear()

    def _open_directory_bindings(self) -> None:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        for directory in self._path.parents:
            descriptor = os.open(directory, directory_flags)
            descriptor_info = os.fstat(descriptor)
            try:
                path_info = directory.lstat()
            except BaseException:
                os.close(descriptor)
                raise
            if (
                not stat.S_ISDIR(descriptor_info.st_mode)
                or not stat.S_ISDIR(path_info.st_mode)
                or (descriptor_info.st_dev, descriptor_info.st_ino)
                != (path_info.st_dev, path_info.st_ino)
            ):
                os.close(descriptor)
                raise FileIntegrityError("guarded path contains an unstable directory")
            self._directory_bindings.append(_PathBinding(
                path=directory,
                descriptor=descriptor,
                device=int(descriptor_info.st_dev),
                inode=int(descriptor_info.st_ino),
            ))

    def _register_watchers(self) -> None:
        watcher = self._watcher
        if watcher is None:
            raise FileIntegrityError("guarded file watcher is unavailable")
        file_flags = (
            select_module.KQ_NOTE_WRITE
            | select_module.KQ_NOTE_DELETE
            | select_module.KQ_NOTE_RENAME
            | select_module.KQ_NOTE_LINK
            | select_module.KQ_NOTE_REVOKE
            | select_module.KQ_NOTE_ATTRIB
            | select_module.KQ_NOTE_EXTEND
        )
        directory_flags = (
            select_module.KQ_NOTE_DELETE
            | select_module.KQ_NOTE_RENAME
            | select_module.KQ_NOTE_REVOKE
            | select_module.KQ_NOTE_ATTRIB
        )
        events = [
            select_module.kevent(
                self._descriptor,
                filter=select_module.KQ_FILTER_VNODE,
                flags=select_module.KQ_EV_ADD | select_module.KQ_EV_CLEAR,
                fflags=file_flags,
            )
        ]
        events.extend(
            select_module.kevent(
                binding.descriptor,
                filter=select_module.KQ_FILTER_VNODE,
                flags=select_module.KQ_EV_ADD | select_module.KQ_EV_CLEAR,
                fflags=directory_flags,
            )
            for binding in self._directory_bindings
        )
        watcher.control(events, 0, 0)

    def _current_bindings_match(self) -> bool:
        try:
            descriptor_info = os.fstat(self._descriptor)
            path_info = self._path.lstat()
        except OSError:
            return False
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
            or (
                self._require_single_link
                and (descriptor_info.st_nlink != 1 or path_info.st_nlink != 1)
            )
        ):
            return False
        for binding in self._directory_bindings:
            try:
                descriptor_info = os.fstat(binding.descriptor)
                path_info = binding.path.lstat()
            except OSError:
                return False
            if (
                not stat.S_ISDIR(descriptor_info.st_mode)
                or not stat.S_ISDIR(path_info.st_mode)
                or (descriptor_info.st_dev, descriptor_info.st_ino)
                != (binding.device, binding.inode)
                or (path_info.st_dev, path_info.st_ino)
                != (binding.device, binding.inode)
            ):
                return False
        return True


def assert_macos_file_integrity_capability() -> None:
    if (
        platform.system() != "Darwin"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or any(not hasattr(select_module, name) for name in _REQUIRED_KQUEUE_NAMES)
    ):
        raise FileIntegrityError("macOS file-integrity monitoring is unavailable")


def probe_macos_file_integrity(root: Path) -> None:
    assert_macos_file_integrity_capability()
    probe_directory = Path(tempfile.mkdtemp(prefix=".mediaforce-integrity-", dir=root))
    probe_path = probe_directory / "probe"
    descriptor = -1
    guard: MacOSFileIntegrityGuard | None = None
    try:
        write_descriptor = os.open(
            probe_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(write_descriptor, b"clean")
            os.fsync(write_descriptor)
        finally:
            os.close(write_descriptor)
        descriptor = os.open(probe_path, os.O_RDONLY | os.O_NOFOLLOW)
        guard = MacOSFileIntegrityGuard(
            path=probe_path,
            descriptor=descriptor,
            require_single_link=True,
        )
        guard.assert_quiet()
        mutation_descriptor = os.open(probe_path, os.O_WRONLY | os.O_NOFOLLOW)
        try:
            os.pwrite(mutation_descriptor, b"X", 0)
            os.fsync(mutation_descriptor)
        finally:
            os.close(mutation_descriptor)
        try:
            guard.assert_quiet(timeout_seconds=0.1)
        except FileIntegrityError:
            return
        raise FileIntegrityError("macOS file-integrity probe missed a mutation")
    finally:
        if guard is not None:
            guard.close()
        if descriptor >= 0:
            os.close(descriptor)
        shutil.rmtree(probe_directory, ignore_errors=True)
