from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import platform
import secrets
import select as select_module
import stat
import sys
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


def fsync_durable_file(descriptor: int) -> None:
    os.fsync(descriptor)
    if sys.platform == "darwin":
        full_fsync = getattr(fcntl, "F_FULLFSYNC", None)
        if full_fsync is None:
            raise OSError(errno.ENOTSUP, "full artifact fsync is unavailable")
        fcntl.fcntl(descriptor, full_fsync)


def rename_exclusive(
        *,
        source_directory_descriptor: int,
        source_name: str,
        destination_directory_descriptor: int,
        destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename_function = getattr(libc, "renameatx_np", None)
        rename_flags = 0x00000004
    elif sys.platform.startswith("linux"):
        rename_function = getattr(libc, "renameat2", None)
        rename_flags = 0x00000001
    else:
        rename_function = None
        rename_flags = 0
    if rename_function is None:
        raise OSError(errno.ENOTSUP, "exclusive atomic rename is unavailable")
    rename_function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename_function.restype = ctypes.c_int
    result = rename_function(
        source_directory_descriptor,
        os.fsencode(source_name),
        destination_directory_descriptor,
        os.fsencode(destination_name),
        rename_flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


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
        _reject_existing_final_symlink(path)
        self._path = stable_absolute_path(path)
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
        directory = Path(self._path.anchor)
        descriptor = os.open(directory, directory_flags)
        try:
            self._append_directory_binding(directory, descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        for component in self._path.parent.parts[1:]:
            parent_descriptor = descriptor
            directory /= component
            descriptor = os.open(
                component,
                directory_flags,
                dir_fd=parent_descriptor,
            )
            try:
                self._append_directory_binding(directory, descriptor)
            except BaseException:
                os.close(descriptor)
                raise

    def _append_directory_binding(self, path: Path, descriptor: int) -> None:
        descriptor_info = os.fstat(descriptor)
        path_info = path.lstat()
        if (
            not stat.S_ISDIR(descriptor_info.st_mode)
            or not stat.S_ISDIR(path_info.st_mode)
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise FileIntegrityError("guarded path contains an unstable directory")
        self._directory_bindings.append(_PathBinding(
            path=path,
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


def stable_absolute_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    if absolute == Path(absolute.anchor):
        return absolute
    return Path(os.path.realpath(absolute.parent)) / absolute.name


def open_stable_directory(
        path: Path,
        *,
        require_owner_only: bool = False,
) -> tuple[Path, int]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise FileIntegrityError("stable directory binding is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = -1
    try:
        _reject_existing_final_symlink(path)
        normalized = stable_absolute_path(path)
        descriptor = os.open(normalized.anchor, flags)
        for component in normalized.parts[1:]:
            next_descriptor = os.open(
                component,
                flags,
                dir_fd=descriptor,
            )
            try:
                _validate_directory_entry(
                    parent_descriptor=descriptor,
                    entry_name=component,
                    descriptor=next_descriptor,
                )
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        descriptor_info = os.fstat(descriptor)
        if (
                require_owner_only
                and (
                    descriptor_info.st_uid != os.getuid()
                    or stat.S_IMODE(descriptor_info.st_mode) & 0o077
                )
        ):
            raise FileIntegrityError("directory must be owner-only")
        return normalized, descriptor
    except BaseException as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(exc, FileIntegrityError):
            raise
        raise FileIntegrityError("stable directory binding failed") from exc


def ensure_owner_only_directory(path: Path) -> tuple[Path, int]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise FileIntegrityError("stable directory creation is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = -1
    try:
        _reject_existing_final_symlink(path, allow_missing=True)
        normalized = stable_absolute_path(path)
        descriptor = os.open(normalized.anchor, flags)
        for index, component in enumerate(normalized.parts[1:], start=1):
            created = False
            try:
                next_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                created = True
                next_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=descriptor,
                )
            try:
                descriptor_info = _validate_directory_entry(
                    parent_descriptor=descriptor,
                    entry_name=component,
                    descriptor=next_descriptor,
                )
                if (
                        (created or index == len(normalized.parts) - 1)
                        and (
                            descriptor_info.st_uid != os.getuid()
                            or stat.S_IMODE(descriptor_info.st_mode) & 0o077
                        )
                ):
                    raise FileIntegrityError("directory must be owner-only")
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        descriptor_info = os.fstat(descriptor)
        if (
                descriptor_info.st_uid != os.getuid()
                or stat.S_IMODE(descriptor_info.st_mode) & 0o077
        ):
            raise FileIntegrityError("directory must be owner-only")
        return normalized, descriptor
    except BaseException as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(exc, FileIntegrityError):
            raise
        raise FileIntegrityError("owner-only directory creation failed") from exc


def _validate_directory_entry(
        *,
        parent_descriptor: int,
        entry_name: str,
        descriptor: int,
) -> os.stat_result:
    descriptor_info = os.fstat(descriptor)
    entry_info = os.stat(
        entry_name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if (
            not stat.S_ISDIR(descriptor_info.st_mode)
            or not stat.S_ISDIR(entry_info.st_mode)
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (entry_info.st_dev, entry_info.st_ino)
    ):
        raise FileIntegrityError("directory path is unstable")
    return descriptor_info


def _reject_existing_final_symlink(
        path: Path,
        *,
        allow_missing: bool = False,
) -> None:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    try:
        info = absolute.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise FileIntegrityError("path identity is unavailable")
    except OSError as exc:
        raise FileIntegrityError("path identity is unavailable") from exc
    if stat.S_ISLNK(info.st_mode):
        raise FileIntegrityError("path must not be a symbolic link")


def probe_macos_file_integrity(root: Path) -> None:
    assert_macos_file_integrity_capability()
    root_path, root_descriptor = open_stable_directory(
        root,
        require_owner_only=True,
    )
    probe_directory_name = f".mediaforce-integrity-{secrets.token_hex(8)}"
    probe_directory = root_path / probe_directory_name
    probe_path = probe_directory / "probe"
    probe_directory_descriptor = -1
    descriptor = -1
    guard: MacOSFileIntegrityGuard | None = None
    try:
        os.mkdir(probe_directory_name, mode=0o700, dir_fd=root_descriptor)
        os.fsync(root_descriptor)
        probe_directory_descriptor = os.open(
            probe_directory_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_descriptor,
        )
        descriptor = os.open(
            "probe",
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400,
            dir_fd=probe_directory_descriptor,
        )
        os.write(descriptor, b"clean")
        os.fsync(descriptor)
        guard = MacOSFileIntegrityGuard(
            path=probe_path,
            descriptor=descriptor,
            require_single_link=True,
        )
        guard.assert_quiet()
        os.pwrite(descriptor, b"X", 0)
        os.fsync(descriptor)
        try:
            guard.assert_quiet(timeout_seconds=0.1)
        except FileIntegrityError:
            return
        raise FileIntegrityError("macOS file-integrity probe missed a mutation")
    except FileIntegrityError:
        raise
    except OSError as exc:
        raise FileIntegrityError("macOS file-integrity probe failed") from exc
    finally:
        if guard is not None:
            guard.close()
        if descriptor >= 0:
            os.close(descriptor)
        if probe_directory_descriptor >= 0:
            os.close(probe_directory_descriptor)
        os.close(root_descriptor)
