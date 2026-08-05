_PREREGISTRATION_MINIMUM_EXTERNAL_DESCRIPTOR_RESERVE = 128


class _RepositoryAuthorityMonitor:
    _IN_MODIFY = 0x00000002
    _IN_ATTRIB = 0x00000004
    _IN_CLOSE_WRITE = 0x00000008
    _IN_MOVED_FROM = 0x00000040
    _IN_MOVED_TO = 0x00000080
    _IN_CREATE = 0x00000100
    _IN_DELETE = 0x00000200
    _IN_DELETE_SELF = 0x00000400
    _IN_MOVE_SELF = 0x00000800

    def __init__(
            self,
            *,
            root: str,
            authority_directories: tuple[
                tuple[str, tuple[int, int], bool],
                ...,
            ],
            metadata_roots: tuple[str, ...],
            metadata_excluded_roots: tuple[str, ...],
            metadata_files: tuple[str, ...],
            failure_type: type[BaseException],
            changed_message: str,
            unavailable_message: str,
    ) -> None:
        self._os = __import__("os")
        self._stat = __import__("stat")
        self._sys = __import__("sys")
        self._root = self._os.path.realpath(root)
        self._authority_directories = authority_directories
        self._metadata_roots = tuple(dict.fromkeys(metadata_roots))
        self._metadata_excluded_roots = tuple(
            dict.fromkeys(
                self._os.path.realpath(path)
                for path in metadata_excluded_roots
            )
        )
        self._metadata_files = tuple(dict.fromkeys(metadata_files))
        self._failure_type = failure_type
        self._changed_message = changed_message
        self._unavailable_message = unavailable_message
        self._failure_message: str | None = None
        self._descriptors: list[int] = []
        self._watched_paths: set[str] = set()
        self._path_states: dict[str, tuple[int, ...]] = {}
        self._missing_paths: set[str] = set()
        self._bound_python_sources: dict[
            str,
            tuple[str, bytes | None, bool, bool],
        ] = {}
        self._bound_snapshot_directory: str | None = None
        self._darwin_watcher = None
        self._inotify_descriptor = -1
        self._inotify_add_watch = None
        self._nofile_resource = None
        self._nofile_baseline_soft_limit: int | None = None
        self._original_nofile_soft_limit: int | None = None
        self._raised_nofile_soft_limit: int | None = None
        try:
            self._metadata_states = self._collect_metadata_states()
            descriptor_count = sum(
                self._is_watchable_state(state)
                for state in self._metadata_states.values()
            ) + len(authority_directories)
            self._ensure_descriptor_capacity(descriptor_count)
            self._start_monitoring()
            for path, expected_identity, require_safe_permissions in (
                    authority_directories
            ):
                self._add_authority_directory(
                    path,
                    expected_identity,
                    require_safe_permissions=require_safe_permissions,
                )
            for path, expected_state in self._metadata_states.items():
                self._add_existing_path(path, expected_state)
            if self._collect_metadata_states() != self._metadata_states:
                self._fail_changed()
            self.assert_quiet()
        except BaseException:
            self.close()
            raise

    def _fail(
            self,
            message: str,
            cause: BaseException | None = None,
    ) -> None:
        if self._failure_message is None:
            self._failure_message = message
        error = self._failure_type(self._failure_message)
        if cause is None:
            raise error
        raise error from cause

    def _raise_sticky_failure(self) -> None:
        if self._failure_message is not None:
            raise self._failure_type(self._failure_message)

    def _fail_changed(self, cause: BaseException | None = None) -> None:
        self._fail(self._changed_message, cause)

    def _fail_unavailable(self, cause: BaseException | None = None) -> None:
        self._fail(self._unavailable_message, cause)

    @staticmethod
    def _state(info: object) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def _is_watchable_state(self, state: tuple[int, ...]) -> bool:
        return self._stat.S_ISDIR(state[2]) or self._stat.S_ISREG(state[2])

    def _lstat_state(self, path: str) -> tuple[int, ...]:
        return self._state(self._os.stat(path, follow_symlinks=False))

    def _collect_tree_states(
            self,
            root: str,
            *,
            excluded_roots: tuple[str, ...] = (),
            reject_symlinks: bool = False,
    ) -> dict[str, tuple[int, ...]]:
        states: dict[str, tuple[int, ...]] = {}
        pending = [root]
        while pending:
            current = pending.pop()
            if current in excluded_roots:
                continue
            try:
                state = self._lstat_state(current)
                if reject_symlinks and self._stat.S_ISLNK(state[2]):
                    self._fail_changed()
                states[current] = state
                if not self._stat.S_ISDIR(state[2]):
                    continue
                with self._os.scandir(current) as entries:
                    children = sorted(
                        (entry.path for entry in entries),
                        reverse=True,
                    )
            except OSError as exc:
                self._fail_unavailable(exc)
            pending.extend(children)
        return states

    def _collect_metadata_states(self) -> dict[str, tuple[int, ...]]:
        states: dict[str, tuple[int, ...]] = {}
        for root in self._metadata_roots:
            excluded_roots = tuple(
                path
                for path in self._metadata_excluded_roots
                if path != root
                and self._os.path.commonpath((root, path)) == root
            )
            states.update(self._collect_tree_states(
                root,
                excluded_roots=excluded_roots,
                reject_symlinks=True,
            ))
        for path in self._metadata_files:
            try:
                states[path] = self._lstat_state(path)
            except OSError as exc:
                self._fail_unavailable(exc)
        return dict(sorted(states.items()))

    def _ensure_descriptor_capacity(self, additional: int) -> None:
        self._raise_sticky_failure()
        try:
            resource = __import__("resource")
            soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
            if (
                self._nofile_baseline_soft_limit is None
                or (
                    self._nofile_baseline_soft_limit == resource.RLIM_INFINITY
                    and soft_limit != resource.RLIM_INFINITY
                )
            ):
                self._nofile_baseline_soft_limit = soft_limit
            baseline_soft_limit = self._nofile_baseline_soft_limit
            if soft_limit == resource.RLIM_INFINITY:
                return
            new_watcher_descriptor_count = int(
                self._darwin_watcher is None
                and self._inotify_descriptor < 0
            )
            additional_process_descriptors = (
                additional + new_watcher_descriptor_count
            )
            projected_monitor_descriptors = (
                len(self._descriptors) + additional + 1
            )
            minimum_limit = (
                self._open_descriptor_count()
                + additional_process_descriptors
                + _PREREGISTRATION_MINIMUM_EXTERNAL_DESCRIPTOR_RESERVE
            )
            desired_limit = max(
                baseline_soft_limit + projected_monitor_descriptors,
                minimum_limit,
            )
            target_limit = desired_limit
            if hard_limit != resource.RLIM_INFINITY:
                if hard_limit < minimum_limit:
                    self._fail_unavailable()
                target_limit = min(desired_limit, hard_limit)
            if soft_limit == resource.RLIM_INFINITY or soft_limit >= target_limit:
                return
            if (
                self._raised_nofile_soft_limit is not None
                and soft_limit != self._raised_nofile_soft_limit
            ):
                self._original_nofile_soft_limit = None
                self._raised_nofile_soft_limit = None
                self._nofile_resource = None
            if self._original_nofile_soft_limit is None:
                self._original_nofile_soft_limit = soft_limit
            resource.setrlimit(
                resource.RLIMIT_NOFILE,
                (target_limit, hard_limit),
            )
            raised_soft_limit, _raised_hard_limit = resource.getrlimit(
                resource.RLIMIT_NOFILE
            )
            self._nofile_resource = resource
            self._raised_nofile_soft_limit = raised_soft_limit
            if (
                raised_soft_limit != resource.RLIM_INFINITY
                and raised_soft_limit < target_limit
            ):
                self._fail_unavailable()
        except (OSError, ValueError) as exc:
            self._fail_unavailable(exc)

    def _open_descriptor_count(self) -> int:
        descriptor_root = (
            "/dev/fd"
            if self._sys.platform == "darwin"
            else "/proc/self/fd"
        )
        try:
            return len(self._os.listdir(descriptor_root))
        except OSError as exc:
            self._fail_unavailable(exc)

    def _start_monitoring(self) -> None:
        if self._sys.platform == "darwin":
            try:
                self._darwin_watcher = __import__("select").kqueue()
            except (AttributeError, OSError) as exc:
                self._fail_unavailable(exc)
            return
        if (
            not self._sys.platform.startswith("linux")
            or not self._os.path.isdir("/proc/self/fd")
        ):
            self._fail_unavailable()
        ctypes = __import__("ctypes")
        libc = ctypes.CDLL(None, use_errno=True)
        inotify_init1 = getattr(libc, "inotify_init1", None)
        inotify_add_watch = getattr(libc, "inotify_add_watch", None)
        if inotify_init1 is None or inotify_add_watch is None:
            self._fail_unavailable()
        inotify_init1.argtypes = (ctypes.c_int,)
        inotify_init1.restype = ctypes.c_int
        inotify_add_watch.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        )
        inotify_add_watch.restype = ctypes.c_int
        descriptor = inotify_init1(
            self._os.O_NONBLOCK | getattr(self._os, "O_CLOEXEC", 0)
        )
        if descriptor < 0:
            error_number = ctypes.get_errno()
            self._fail_unavailable(
                OSError(error_number, self._os.strerror(error_number))
            )
        self._inotify_descriptor = descriptor
        self._inotify_add_watch = inotify_add_watch

    def _watch_descriptor(self, descriptor: int) -> None:
        descriptor_info = self._os.fstat(descriptor)
        if self._darwin_watcher is not None:
            select_module = __import__("select")
            flags = (
                select_module.KQ_NOTE_WRITE
                | getattr(select_module, "KQ_NOTE_EXTEND", 0)
                | select_module.KQ_NOTE_DELETE
                | select_module.KQ_NOTE_RENAME
                | select_module.KQ_NOTE_LINK
                | select_module.KQ_NOTE_REVOKE
            )
            if self._stat.S_ISDIR(descriptor_info.st_mode):
                flags |= select_module.KQ_NOTE_ATTRIB
            try:
                self._darwin_watcher.control(
                    [
                        select_module.kevent(
                            descriptor,
                            filter=select_module.KQ_FILTER_VNODE,
                            flags=(
                                select_module.KQ_EV_ADD
                                | select_module.KQ_EV_CLEAR
                            ),
                            fflags=flags,
                        )
                    ],
                    0,
                    0,
                )
            except OSError as exc:
                self._fail_unavailable(exc)
            return
        if self._inotify_descriptor < 0 or self._inotify_add_watch is None:
            self._fail_unavailable()
        mask = (
            self._IN_MODIFY
            | self._IN_CLOSE_WRITE
            | self._IN_DELETE_SELF
            | self._IN_MOVE_SELF
        )
        if self._stat.S_ISDIR(descriptor_info.st_mode):
            mask |= (
                self._IN_ATTRIB
                | self._IN_MOVED_FROM
                | self._IN_MOVED_TO
                | self._IN_CREATE
                | self._IN_DELETE
            )
        watch = self._inotify_add_watch(
            self._inotify_descriptor,
            self._os.fsencode(f"/proc/self/fd/{descriptor}"),
            mask,
        )
        if watch < 0:
            ctypes = __import__("ctypes")
            error_number = ctypes.get_errno()
            self._fail_unavailable(
                OSError(error_number, self._os.strerror(error_number))
            )

    def _open_descriptor(self, path: str, state: tuple[int, ...]) -> int:
        flags = getattr(
            self._os,
            "O_EVTONLY",
            self._os.O_RDONLY,
        ) | getattr(self._os, "O_CLOEXEC", 0)
        if self._stat.S_ISDIR(state[2]):
            flags |= getattr(self._os, "O_DIRECTORY", 0)
        if hasattr(self._os, "O_NOFOLLOW"):
            flags |= self._os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = self._os.open(path, flags)
            descriptor_info = self._os.fstat(descriptor)
            current_state = self._lstat_state(path)
        except OSError as exc:
            if descriptor >= 0:
                self._os.close(descriptor)
            errno_module = __import__("errno")
            if exc.errno in {
                errno_module.EMFILE,
                errno_module.ENFILE,
                errno_module.ENOMEM,
            }:
                self._fail_unavailable(exc)
            self._fail_changed(exc)
        if (
            (descriptor_info.st_dev, descriptor_info.st_ino)
            != (state[0], state[1])
            or current_state != state
            or self._stat.S_IFMT(descriptor_info.st_mode)
            != self._stat.S_IFMT(state[2])
        ):
            self._os.close(descriptor)
            self._fail_changed()
        return descriptor

    def _add_existing_path(
            self,
            path: str,
            expected_state: tuple[int, ...],
    ) -> None:
        if path in self._path_states:
            if self._path_states[path] != expected_state:
                self._fail_changed()
            return
        self._path_states[path] = expected_state
        if not self._is_watchable_state(expected_state):
            return
        descriptor = self._open_descriptor(path, expected_state)
        try:
            self._watch_descriptor(descriptor)
        except BaseException:
            self._os.close(descriptor)
            raise
        self._descriptors.append(descriptor)
        self._watched_paths.add(path)

    def _add_authority_directory(
            self,
            path: str,
            expected_identity: tuple[int, int],
            *,
            require_safe_permissions: bool,
    ) -> None:
        try:
            state = self._lstat_state(path)
        except OSError as exc:
            self._fail_changed(exc)
        if (
            not self._stat.S_ISDIR(state[2])
            or (state[0], state[1]) != expected_identity
            or (
                require_safe_permissions
                and (
                    self._os.stat(path, follow_symlinks=False).st_uid
                    != self._os.getuid()
                    or state[2] & 0o022
                )
            )
        ):
            self._fail_changed()
        self._add_existing_path(path, state)

    def _add_parent_directories(self, path: str) -> None:
        parent = self._os.path.dirname(path)
        try:
            relative_parent = self._os.path.relpath(parent, self._root)
        except ValueError as exc:
            self._fail_changed(exc)
        if relative_parent == self._os.pardir or relative_parent.startswith(
                f"{self._os.pardir}{self._os.sep}"
        ):
            self._fail_changed()
        current = self._root
        if relative_parent != self._os.curdir:
            for component in relative_parent.split(self._os.sep):
                current = self._os.path.join(current, component)
                try:
                    state = self._lstat_state(current)
                except OSError as exc:
                    self._fail_changed(exc)
                if not self._stat.S_ISDIR(state[2]):
                    self._fail_changed()
                if current not in self._path_states:
                    self._ensure_descriptor_capacity(1)
                    self._add_existing_path(current, state)

    def add_worktree_paths(self, relative_paths: tuple[str, ...]) -> None:
        self._raise_sticky_failure()
        unique_paths = tuple(dict.fromkeys(relative_paths))
        self._ensure_descriptor_capacity(len(unique_paths))
        for relative_path in unique_paths:
            if (
                not relative_path
                or self._os.path.isabs(relative_path)
                or any(
                    component in {"", self._os.curdir, self._os.pardir}
                    for component in relative_path.split(self._os.sep)
                )
            ):
                self._fail_changed()
            path = self._os.path.normpath(
                self._os.path.join(self._root, relative_path)
            )
            try:
                if self._os.path.commonpath((self._root, path)) != self._root:
                    self._fail_changed()
            except ValueError as exc:
                self._fail_changed(exc)
            self._add_parent_directories(path)
            try:
                state = self._lstat_state(path)
            except FileNotFoundError:
                self._missing_paths.add(path)
                continue
            except OSError as exc:
                self._fail_changed(exc)
            self._add_existing_path(path, state)
        self.assert_quiet()

    def add_worktree_tree(self, tree_root: str) -> None:
        self._raise_sticky_failure()
        normalized_root = self._os.path.realpath(tree_root)
        try:
            if self._os.path.commonpath((self._root, normalized_root)) != self._root:
                self._fail_changed()
        except ValueError as exc:
            self._fail_changed(exc)
        if not self._os.path.isdir(normalized_root):
            return
        expected_states = self._collect_tree_states(normalized_root)
        self._ensure_descriptor_capacity(
            sum(
                self._is_watchable_state(state)
                for state in expected_states.values()
            )
        )
        for path, expected_state in sorted(
                expected_states.items(),
                key=lambda entry: (entry[0].count(self._os.sep), entry[0]),
        ):
            self._add_existing_path(path, expected_state)
        if self._collect_tree_states(normalized_root) != expected_states:
            self._fail_changed()
        self.assert_quiet()

    def bind_bound_snapshot_directory(self, snapshot_directory: str) -> None:
        self._raise_sticky_failure()
        if self._bound_snapshot_directory is not None:
            self._fail_changed()
        normalized_directory = self._os.path.realpath(snapshot_directory)
        try:
            if self._os.path.commonpath((self._root, normalized_directory)) == self._root:
                self._fail_changed()
        except ValueError as exc:
            self._fail_changed(exc)
        try:
            root_state = self._lstat_state(normalized_directory)
        except OSError as exc:
            self._fail_changed(exc)
        if (
            not self._stat.S_ISDIR(root_state[2])
            or self._stat.S_ISLNK(root_state[2])
            or self._os.stat(
                normalized_directory,
                follow_symlinks=False,
            ).st_uid != self._os.getuid()
            or root_state[2] & 0o077
        ):
            self._fail_changed()
        self._bound_snapshot_directory = normalized_directory
        try:
            expected_states = self._collect_tree_states(
                normalized_directory,
                reject_symlinks=True,
            )
            self._ensure_descriptor_capacity(
                sum(
                    self._is_watchable_state(state)
                    for state in expected_states.values()
                )
            )
            for path, expected_state in sorted(
                    expected_states.items(),
                    key=lambda entry: (entry[0].count(self._os.sep), entry[0]),
            ):
                self._add_existing_path(path, expected_state)
            if self._collect_tree_states(
                    normalized_directory,
                    reject_symlinks=True,
            ) != expected_states:
                self._fail_changed()
            self.assert_quiet()
        except BaseException:
            self.close()
            raise

    def bound_snapshot_directory(self) -> str:
        self.assert_quiet()
        if self._bound_snapshot_directory is None:
            self._fail_changed()
        return self._bound_snapshot_directory

    def _events_changed(self) -> bool:
        self._raise_sticky_failure()
        if self._darwin_watcher is not None:
            try:
                return bool(self._darwin_watcher.control(None, 64, 0))
            except OSError as exc:
                self._fail_unavailable(exc)
        if self._inotify_descriptor < 0:
            self._fail_unavailable()
        changed = False
        while True:
            try:
                data = self._os.read(self._inotify_descriptor, 64 * 1024)
            except BlockingIOError:
                break
            except OSError as exc:
                self._fail_unavailable(exc)
            if not data:
                self._fail_unavailable()
            changed = True
        return changed

    def assert_quiet(self) -> None:
        self._raise_sticky_failure()
        changed = self._events_changed()
        try:
            try:
                current_metadata_states = self._collect_metadata_states()
            except self._failure_type:
                changed = True
            else:
                if current_metadata_states != self._metadata_states:
                    changed = True
            for path, expected_state in self._path_states.items():
                if self._lstat_state(path) != expected_state:
                    changed = True
            for path in self._missing_paths:
                try:
                    self._os.stat(path, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                changed = True
        except OSError:
            changed = True
        if self._events_changed():
            changed = True
        if changed:
            self._fail_changed()

    def bind_python_sources(
            self,
            sources: dict[str, tuple[str, bytes | None, bool, bool]],
    ) -> None:
        self.assert_quiet()
        if self._bound_python_sources:
            self._fail_changed()
        self._bound_python_sources = dict(sources)
        self.assert_quiet()

    def bound_python_sources(
            self,
    ) -> dict[str, tuple[str, bytes | None, bool, bool]]:
        self.assert_quiet()
        if not self._bound_python_sources:
            self._fail_changed()
        return dict(self._bound_python_sources)

    def _restore_descriptor_capacity(self) -> None:
        resource = self._nofile_resource
        original_soft_limit = self._original_nofile_soft_limit
        raised_soft_limit = self._raised_nofile_soft_limit
        self._nofile_resource = None
        self._nofile_baseline_soft_limit = None
        self._original_nofile_soft_limit = None
        self._raised_nofile_soft_limit = None
        if (
            resource is None
            or original_soft_limit is None
            or raised_soft_limit is None
        ):
            return
        try:
            current_soft_limit, current_hard_limit = resource.getrlimit(
                resource.RLIMIT_NOFILE
            )
            if current_soft_limit != raised_soft_limit:
                return
            resource.setrlimit(
                resource.RLIMIT_NOFILE,
                (original_soft_limit, current_hard_limit),
            )
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        snapshot_directory = getattr(self, "_bound_snapshot_directory", None)
        self._bound_snapshot_directory = None
        if self._darwin_watcher is not None:
            try:
                self._darwin_watcher.close()
            except OSError:
                pass
            self._darwin_watcher = None
        if self._inotify_descriptor >= 0:
            try:
                self._os.close(self._inotify_descriptor)
            except OSError:
                pass
            self._inotify_descriptor = -1
        for descriptor in self._descriptors:
            try:
                self._os.close(descriptor)
            except OSError:
                pass
        self._descriptors.clear()
        self._watched_paths.clear()
        self._restore_descriptor_capacity()
        if snapshot_directory is not None:
            shutil_module = __import__("shutil")
            try:
                if self._os.path.islink(snapshot_directory):
                    self._os.unlink(snapshot_directory)
                else:
                    shutil_module.rmtree(snapshot_directory)
            except OSError:
                pass

    def __del__(self) -> None:
        self.close()


def _raise_repository_authority_error(
        failure_type: type[BaseException],
        message: str,
        cause: BaseException | None = None,
) -> None:
    error = failure_type(message)
    if cause is None:
        raise error
    raise error from cause


def _bounded_authority_file_bytes(
        path: str,
        *,
        maximum_bytes: int,
        failure_type: type[BaseException],
        message: str,
) -> bytes | None:
    os_module = __import__("os")
    stat_module = __import__("stat")
    try:
        path_info = os_module.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _raise_repository_authority_error(failure_type, message, exc)
    if (
        stat_module.S_ISLNK(path_info.st_mode)
        or not stat_module.S_ISREG(path_info.st_mode)
        or path_info.st_size > maximum_bytes
    ):
        _raise_repository_authority_error(failure_type, message)
    flags = os_module.O_RDONLY | getattr(os_module, "O_CLOEXEC", 0)
    if not hasattr(os_module, "O_NOFOLLOW"):
        _raise_repository_authority_error(failure_type, message)
    flags |= os_module.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os_module.open(path, flags)
        descriptor_info = os_module.fstat(descriptor)
        if (
            not stat_module.S_ISREG(descriptor_info.st_mode)
            or descriptor_info.st_size > maximum_bytes
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
        ):
            _raise_repository_authority_error(failure_type, message)
        payload = bytearray()
        while len(payload) <= maximum_bytes:
            chunk = os_module.read(descriptor, min(64 * 1024, maximum_bytes + 1))
            if not chunk:
                break
            payload.extend(chunk)
        final_descriptor_info = os_module.fstat(descriptor)
        final_path_info = os_module.stat(path, follow_symlinks=False)
        if (
            len(payload) > maximum_bytes
            or _RepositoryAuthorityMonitor._state(descriptor_info)
            != _RepositoryAuthorityMonitor._state(final_descriptor_info)
            or _RepositoryAuthorityMonitor._state(path_info)
            != _RepositoryAuthorityMonitor._state(final_path_info)
        ):
            _raise_repository_authority_error(failure_type, message)
        return bytes(payload)
    except OSError as exc:
        _raise_repository_authority_error(failure_type, message, exc)
    finally:
        if descriptor >= 0:
            os_module.close(descriptor)


_BOUND_MEDIAFORCE_SNAPSHOT_AUXILIARY_PATHS = (
    b"config/defaults.toml",
    b"hatch_build.py",
    b"pyproject.toml",
    b"scripts/verify_av1_cold_start_preregistration.py",
    b"uv.lock",
)


def _assert_execution_authority_roots_are_not_symlinked(
        root: str,
        *,
        failure_type: type[BaseException],
        message: str,
) -> None:
    os_module = __import__("os")
    stat_module = __import__("stat")
    root_bytes = os_module.fsencode(root)
    for relative_path in (
            b"mediaforce",
            b"scripts",
            b"config/defaults.toml",
    ):
        components = _validate_git_path_bytes(
            relative_path,
            failure_type=failure_type,
            message=message,
        )
        current = root_bytes
        missing = False
        for component in components:
            current = os_module.path.join(current, component)
            try:
                info = os_module.stat(current, follow_symlinks=False)
            except FileNotFoundError:
                missing = True
                break
            except OSError as exc:
                _raise_repository_authority_error(failure_type, message, exc)
            if stat_module.S_ISLNK(info.st_mode):
                _raise_repository_authority_error(failure_type, message)
        if missing:
            continue
        pending = [current]
        while pending:
            candidate = pending.pop()
            try:
                info = os_module.stat(candidate, follow_symlinks=False)
            except OSError as exc:
                _raise_repository_authority_error(failure_type, message, exc)
            if stat_module.S_ISLNK(info.st_mode):
                _raise_repository_authority_error(failure_type, message)
            if not stat_module.S_ISDIR(info.st_mode):
                continue
            try:
                with os_module.scandir(candidate) as entries:
                    pending.extend(entry.path for entry in entries)
            except OSError as exc:
                _raise_repository_authority_error(failure_type, message, exc)


def _materialize_bound_mediaforce_snapshot(
        repository_root: str,
        captured_entries: dict[bytes, bytes],
        *,
        failure_type: type[BaseException],
        message: str,
) -> str:
    os_module = __import__("os")
    shutil_module = __import__("shutil")
    stat_module = __import__("stat")
    tempfile_module = __import__("tempfile")
    if not hasattr(os_module, "O_NOFOLLOW"):
        _raise_repository_authority_error(failure_type, message)
    try:
        snapshot_directory = tempfile_module.mkdtemp(
            prefix="mediaforce-av1-bound-",
            dir="/tmp",
        )
    except OSError as exc:
        _raise_repository_authority_error(failure_type, message, exc)
    try:
        snapshot_directory = os_module.path.realpath(snapshot_directory)
        try:
            if (
                os_module.path.commonpath((
                    os_module.path.realpath(repository_root),
                    snapshot_directory,
                ))
                == os_module.path.realpath(repository_root)
            ):
                _raise_repository_authority_error(failure_type, message)
        except ValueError as exc:
            _raise_repository_authority_error(failure_type, message, exc)
        os_module.chmod(snapshot_directory, 0o700)
        snapshot_info = os_module.stat(snapshot_directory, follow_symlinks=False)
        if (
            not stat_module.S_ISDIR(snapshot_info.st_mode)
            or stat_module.S_ISLNK(snapshot_info.st_mode)
            or snapshot_info.st_uid != os_module.getuid()
            or snapshot_info.st_mode & 0o077
        ):
            _raise_repository_authority_error(failure_type, message)

        def ensure_directory(components: tuple[str, ...]) -> str:
            directory = snapshot_directory
            for component in components:
                directory = os_module.path.join(directory, component)
                try:
                    os_module.mkdir(directory, 0o700)
                except FileExistsError:
                    pass
                except OSError as exc:
                    _raise_repository_authority_error(failure_type, message, exc)
                try:
                    directory_info = os_module.stat(
                        directory,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    _raise_repository_authority_error(failure_type, message, exc)
                if (
                    not stat_module.S_ISDIR(directory_info.st_mode)
                    or stat_module.S_ISLNK(directory_info.st_mode)
                    or directory_info.st_uid != os_module.getuid()
                    or directory_info.st_mode & 0o077
                ):
                    _raise_repository_authority_error(failure_type, message)
            return directory

        for path, payload in sorted(captured_entries.items()):
            components = _validate_git_path_bytes(
                path,
                failure_type=failure_type,
                message=message,
            )
            if not (
                path.startswith(b"mediaforce/")
                or path in _BOUND_MEDIAFORCE_SNAPSHOT_AUXILIARY_PATHS
            ):
                _raise_repository_authority_error(failure_type, message)
            decoded_components = tuple(
                os_module.fsdecode(component)
                for component in components
            )
            parent_directory = ensure_directory(decoded_components[:-1])
            target_path = os_module.path.join(
                parent_directory,
                decoded_components[-1],
            )
            descriptor = -1
            try:
                descriptor = os_module.open(
                    target_path,
                    (
                        os_module.O_WRONLY
                        | os_module.O_CREAT
                        | os_module.O_EXCL
                        | os_module.O_NOFOLLOW
                        | getattr(os_module, "O_CLOEXEC", 0)
                    ),
                    0o600,
                )
                written = 0
                while written < len(payload):
                    count = os_module.write(descriptor, payload[written:])
                    if count <= 0:
                        _raise_repository_authority_error(failure_type, message)
                    written += count
                os_module.fsync(descriptor)
                descriptor_info = os_module.fstat(descriptor)
                path_info = os_module.stat(target_path, follow_symlinks=False)
                if (
                    written != len(payload)
                    or not stat_module.S_ISREG(descriptor_info.st_mode)
                    or descriptor_info.st_uid != os_module.getuid()
                    or descriptor_info.st_mode & 0o077
                    or (descriptor_info.st_dev, descriptor_info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)
                ):
                    _raise_repository_authority_error(failure_type, message)
            except OSError as exc:
                _raise_repository_authority_error(failure_type, message, exc)
            finally:
                if descriptor >= 0:
                    os_module.close(descriptor)
        return snapshot_directory
    except BaseException:
        try:
            shutil_module.rmtree(snapshot_directory)
        except OSError:
            pass
        raise


def _git_config_has_external_authority_directive(payload: bytes) -> bool:
    current_section = b""
    for raw_line in payload.splitlines():
        line = raw_line.lstrip(b" \t\xef\xbb\xbf")
        if not line or line.startswith((b"#", b";")):
            continue
        if line.startswith(b"["):
            closing = line.find(b"]")
            if closing < 0:
                continue
            section = line[1:closing].strip().lower()
            current_section = section.split(None, 1)[0].split(b'"', 1)[0]
            if (
                current_section in {b"include", b"includeif"}
                or current_section.startswith((b"include.", b"includeif."))
            ):
                return True
            continue
        key = line.split(b"=", 1)[0].split(None, 1)[0].strip().lower()
        if key == b"include.path" or (
            key.startswith(b"includeif.") and key.endswith(b".path")
        ):
            return True
        if (
            (current_section == b"extensions" and key == b"partialclone")
            or (
                (
                    current_section == b"remote"
                    or current_section.startswith(b"remote.")
                )
                and key in {b"promisor", b"partialclonefilter"}
            )
            or key == b"extensions.partialclone"
            or (
                key.startswith(b"remote.")
                and key.endswith((b".promisor", b".partialclonefilter"))
            )
        ):
            return True
    return False


def _assert_git_metadata_authority_is_closed(
        git_directory: str,
        common_directory: str,
        *,
        failure_type: type[BaseException],
        message: str,
) -> None:
    os_module = __import__("os")
    stat_module = __import__("stat")
    excluded_root = os_module.path.join(common_directory, "worktrees")
    roots = tuple(dict.fromkeys((git_directory, common_directory)))
    pending = list(roots)
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            info = os_module.stat(current, follow_symlinks=False)
        except OSError as exc:
            _raise_repository_authority_error(failure_type, message, exc)
        if stat_module.S_ISLNK(info.st_mode):
            _raise_repository_authority_error(failure_type, message)
        if not stat_module.S_ISDIR(info.st_mode):
            continue
        if current == excluded_root:
            continue
        try:
            with os_module.scandir(current) as entries:
                pending.extend(entry.path for entry in entries)
        except OSError as exc:
            _raise_repository_authority_error(failure_type, message, exc)

    for config_path in dict.fromkeys((
        os_module.path.join(common_directory, "config"),
        os_module.path.join(common_directory, "config.worktree"),
        os_module.path.join(git_directory, "config.worktree"),
    )):
        payload = _bounded_authority_file_bytes(
            config_path,
            maximum_bytes=1024 * 1024,
            failure_type=failure_type,
            message=message,
        )
        if payload is not None and _git_config_has_external_authority_directive(
                payload
        ):
            _raise_repository_authority_error(failure_type, message)

    alternates = _bounded_authority_file_bytes(
        os_module.path.join(common_directory, "objects", "info", "alternates"),
        maximum_bytes=1024 * 1024,
        failure_type=failure_type,
        message=message,
    )
    if alternates is not None and alternates.strip():
        _raise_repository_authority_error(failure_type, message)


def _validate_git_path_bytes(
        path: bytes,
        *,
        failure_type: type[BaseException],
        message: str,
) -> tuple[bytes, ...]:
    components = tuple(path.split(b"/"))
    if (
        not path
        or path.startswith(b"/")
        or b"\0" in path
        or any(component in {b"", b".", b".."} for component in components)
    ):
        _raise_repository_authority_error(failure_type, message)
    return components


def _valid_git_object_id_bytes(object_id: bytes) -> bool:
    return len(object_id) in {40, 64} and all(
        byte in b"0123456789abcdef" for byte in object_id
    )


def _parse_git_index_snapshot(
        output: bytes,
        *,
        failure_type: type[BaseException],
        message: str,
) -> tuple[tuple[bytes, bytes, bytes], ...]:
    entries: list[tuple[bytes, bytes, bytes]] = []
    seen_paths: set[bytes] = set()
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path = record.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ", 2)
        except ValueError as exc:
            _raise_repository_authority_error(failure_type, message, exc)
        _validate_git_path_bytes(
            path,
            failure_type=failure_type,
            message=message,
        )
        if (
            stage != b"0"
            or mode not in {b"100644", b"100755", b"120000", b"160000"}
            or not _valid_git_object_id_bytes(object_id)
            or path in seen_paths
        ):
            _raise_repository_authority_error(failure_type, message)
        seen_paths.add(path)
        entries.append((path, mode, object_id))
    return tuple(entries)


def _parse_git_tree_snapshot(
        output: bytes,
        *,
        failure_type: type[BaseException],
        message: str,
) -> tuple[tuple[bytes, bytes, bytes], ...]:
    entries: list[tuple[bytes, bytes, bytes]] = []
    seen_paths: set[bytes] = set()
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split(b" ", 2)
        except ValueError as exc:
            _raise_repository_authority_error(failure_type, message, exc)
        _validate_git_path_bytes(
            path,
            failure_type=failure_type,
            message=message,
        )
        expected_type = b"commit" if mode == b"160000" else b"blob"
        if (
            mode not in {b"100644", b"100755", b"120000", b"160000"}
            or object_type != expected_type
            or not _valid_git_object_id_bytes(object_id)
            or path in seen_paths
        ):
            _raise_repository_authority_error(failure_type, message)
        seen_paths.add(path)
        entries.append((path, mode, object_id))
    return tuple(entries)


def _assert_ordinary_git_index_state(
        output: bytes,
        expected_paths: tuple[bytes, ...],
        *,
        failure_type: type[BaseException],
        message: str,
) -> None:
    paths: list[bytes] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        if len(record) < 3 or not record.startswith(b"H "):
            _raise_repository_authority_error(failure_type, message)
        path = record[2:]
        _validate_git_path_bytes(
            path,
            failure_type=failure_type,
            message=message,
        )
        paths.append(path)
    if tuple(paths) != expected_paths:
        _raise_repository_authority_error(failure_type, message)


def _git_blob_hasher(object_id: bytes) -> object:
    hashlib_module = __import__("hashlib")
    algorithm = "sha1" if len(object_id) == 40 else "sha256"
    try:
        return hashlib_module.new(algorithm, usedforsecurity=False)
    except TypeError:
        return hashlib_module.new(algorithm)


def _verify_git_worktree_entry(
        root: str,
        path: bytes,
        mode: bytes,
        object_id: bytes,
        *,
        capture_bytes: bool,
        reject_symlink_components: bool = False,
        failure_type: type[BaseException],
        message: str,
) -> bytes | None:
    os_module = __import__("os")
    stat_module = __import__("stat")
    components = _validate_git_path_bytes(
        path,
        failure_type=failure_type,
        message=message,
    )
    if (
        not hasattr(os_module, "O_DIRECTORY")
        or not hasattr(os_module, "O_NOFOLLOW")
        or os_module.open not in os_module.supports_dir_fd
        or os_module.stat not in os_module.supports_dir_fd
    ):
        _raise_repository_authority_error(failure_type, message)
    root_path = os_module.fsencode(root)
    directory_flags = (
        os_module.O_RDONLY
        | os_module.O_DIRECTORY
        | os_module.O_NOFOLLOW
        | getattr(os_module, "O_CLOEXEC", 0)
    )
    descriptor = -1
    directory_descriptors: list[int] = []
    try:
        root_path_info = os_module.stat(root_path, follow_symlinks=False)
        if (
            not stat_module.S_ISDIR(root_path_info.st_mode)
            or stat_module.S_ISLNK(root_path_info.st_mode)
        ):
            _raise_repository_authority_error(failure_type, message)
        current_directory = os_module.open(root_path, directory_flags)
        directory_descriptors.append(current_directory)
        root_descriptor_info = os_module.fstat(current_directory)
        if (
            not stat_module.S_ISDIR(root_descriptor_info.st_mode)
            or (root_descriptor_info.st_dev, root_descriptor_info.st_ino)
            != (root_path_info.st_dev, root_path_info.st_ino)
        ):
            _raise_repository_authority_error(failure_type, message)
        for component in components[:-1]:
            component_info = os_module.stat(
                component,
                dir_fd=current_directory,
                follow_symlinks=False,
            )
            if (
                not stat_module.S_ISDIR(component_info.st_mode)
                or stat_module.S_ISLNK(component_info.st_mode)
            ):
                _raise_repository_authority_error(failure_type, message)
            next_directory = os_module.open(
                component,
                directory_flags,
                dir_fd=current_directory,
            )
            directory_descriptors.append(next_directory)
            next_directory_info = os_module.fstat(next_directory)
            if (
                not stat_module.S_ISDIR(next_directory_info.st_mode)
                or (next_directory_info.st_dev, next_directory_info.st_ino)
                != (component_info.st_dev, component_info.st_ino)
            ):
                _raise_repository_authority_error(failure_type, message)
            current_directory = next_directory
        initial_path_info = os_module.stat(
            components[-1],
            dir_fd=current_directory,
            follow_symlinks=False,
        )
        if mode == b"160000":
            _raise_repository_authority_error(failure_type, message)
        if mode == b"120000":
            if (
                reject_symlink_components
                or not stat_module.S_ISLNK(initial_path_info.st_mode)
            ):
                _raise_repository_authority_error(failure_type, message)
            payload = os_module.readlink(
                components[-1],
                dir_fd=current_directory,
            )
            final_path_info = os_module.stat(
                components[-1],
                dir_fd=current_directory,
                follow_symlinks=False,
            )
            if not isinstance(payload, bytes) or (
                _RepositoryAuthorityMonitor._state(initial_path_info)
                != _RepositoryAuthorityMonitor._state(final_path_info)
            ):
                _raise_repository_authority_error(failure_type, message)
            hasher = _git_blob_hasher(object_id)
            hasher.update(f"blob {len(payload)}\0".encode("ascii"))
            hasher.update(payload)
            if hasher.hexdigest().encode("ascii") != object_id:
                _raise_repository_authority_error(failure_type, message)
            return payload if capture_bytes else None
        if (
            mode not in {b"100644", b"100755"}
            or not stat_module.S_ISREG(initial_path_info.st_mode)
        ):
            _raise_repository_authority_error(failure_type, message)
        worktree_mode = (
            b"100755"
            if initial_path_info.st_mode & stat_module.S_IXUSR
            else b"100644"
        )
        if worktree_mode != mode:
            _raise_repository_authority_error(failure_type, message)
        flags = (
            os_module.O_RDONLY
            | os_module.O_NOFOLLOW
            | getattr(os_module, "O_CLOEXEC", 0)
            | getattr(os_module, "O_NONBLOCK", 0)
        )
        descriptor = os_module.open(
            components[-1],
            flags,
            dir_fd=current_directory,
        )
        initial_descriptor_info = os_module.fstat(descriptor)
        if (
            not stat_module.S_ISREG(initial_descriptor_info.st_mode)
            or (initial_descriptor_info.st_dev, initial_descriptor_info.st_ino)
            != (initial_path_info.st_dev, initial_path_info.st_ino)
            or initial_descriptor_info.st_size != initial_path_info.st_size
        ):
            _raise_repository_authority_error(failure_type, message)
        hasher = _git_blob_hasher(object_id)
        hasher.update(
            f"blob {initial_descriptor_info.st_size}\0".encode("ascii")
        )
        captured = bytearray() if capture_bytes else None
        bytes_read = 0
        while True:
            chunk = os_module.read(descriptor, 64 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            bytes_read += len(chunk)
            if captured is not None:
                captured.extend(chunk)
        final_descriptor_info = os_module.fstat(descriptor)
        final_path_info = os_module.stat(
            components[-1],
            dir_fd=current_directory,
            follow_symlinks=False,
        )
        if (
            bytes_read != initial_descriptor_info.st_size
            or _RepositoryAuthorityMonitor._state(initial_descriptor_info)
            != _RepositoryAuthorityMonitor._state(final_descriptor_info)
            or _RepositoryAuthorityMonitor._state(initial_path_info)
            != _RepositoryAuthorityMonitor._state(final_path_info)
            or hasher.hexdigest().encode("ascii") != object_id
        ):
            _raise_repository_authority_error(failure_type, message)
        return bytes(captured) if captured is not None else None
    except OSError as exc:
        _raise_repository_authority_error(failure_type, message, exc)
    finally:
        if descriptor >= 0:
            os_module.close(descriptor)
        for directory_descriptor in reversed(directory_descriptors):
            os_module.close(directory_descriptor)


def _assert_exact_git_worktree_snapshot(
        root: str,
        index_output: bytes,
        tree_output: bytes,
        verbose_output: bytes,
        *,
        capture_mediaforce_python: bool,
        capture_mediaforce_resources: bool = False,
        reject_symlink_components: bool = False,
        failure_type: type[BaseException],
        message: str,
) -> dict[bytes, bytes]:
    index_entries = _parse_git_index_snapshot(
        index_output,
        failure_type=failure_type,
        message=message,
    )
    tree_entries = _parse_git_tree_snapshot(
        tree_output,
        failure_type=failure_type,
        message=message,
    )
    if index_entries != tree_entries:
        _raise_repository_authority_error(failure_type, message)
    expected_paths = tuple(path for path, _mode, _object_id in index_entries)
    _assert_ordinary_git_index_state(
        verbose_output,
        expected_paths,
        failure_type=failure_type,
        message=message,
    )
    captured_entries: dict[bytes, bytes] = {}
    for path, mode, object_id in index_entries:
        capture_bytes = (
            capture_mediaforce_python
            and path.startswith(b"mediaforce/")
            and path.endswith(b".py")
        ) or (
            capture_mediaforce_resources
            and (
                path.startswith(b"mediaforce/")
                or path in _BOUND_MEDIAFORCE_SNAPSHOT_AUXILIARY_PATHS
            )
        )
        payload = _verify_git_worktree_entry(
            root,
            path,
            mode,
            object_id,
            capture_bytes=capture_bytes,
            reject_symlink_components=reject_symlink_components,
            failure_type=failure_type,
            message=message,
        )
        if capture_bytes:
            if payload is None:
                _raise_repository_authority_error(failure_type, message)
            captured_entries[path] = payload
    return captured_entries


def _build_bound_mediaforce_sources(
        root: str,
        captured_sources: dict[bytes, bytes],
        *,
        failure_type: type[BaseException],
        message: str,
) -> dict[str, tuple[str, bytes | None, bool, bool]]:
    os_module = __import__("os")
    sources: dict[str, tuple[str, bytes | None, bool, bool]] = {}
    package_directories: dict[str, str] = {}
    for path, payload in captured_sources.items():
        components = _validate_git_path_bytes(
            path,
            failure_type=failure_type,
            message=message,
        )
        if components[0] != b"mediaforce":
            if path not in _BOUND_MEDIAFORCE_SNAPSHOT_AUXILIARY_PATHS:
                _raise_repository_authority_error(failure_type, message)
            continue
        if not components[-1].endswith(b".py"):
            continue
        decoded_components = tuple(os_module.fsdecode(component) for component in components)
        for component in decoded_components[:-1]:
            if "." in component:
                _raise_repository_authority_error(failure_type, message)
        filename = os_module.path.join(root, *decoded_components)
        is_package = components[-1] == b"__init__.py"
        module_components = (
            decoded_components[:-1]
            if is_package
            else (*decoded_components[:-1], decoded_components[-1][:-3])
        )
        if not module_components or any(
            not component or "." in component
            for component in module_components
        ):
            _raise_repository_authority_error(failure_type, message)
        module_name = ".".join(module_components)
        if module_name in sources:
            _raise_repository_authority_error(failure_type, message)
        sources[module_name] = (filename, payload, is_package, False)
        for depth in range(1, len(decoded_components)):
            package_name = ".".join(decoded_components[:depth])
            package_directories[package_name] = os_module.path.join(
                root,
                *decoded_components[:depth],
            )
    for package_name, directory in package_directories.items():
        if package_name not in sources:
            sources[package_name] = (directory, None, True, True)
    root_source = sources.get("mediaforce")
    if root_source is None or root_source[1] is None or not root_source[2]:
        _raise_repository_authority_error(failure_type, message)
    return sources


class _BoundMediaforceLoader:
    def __init__(
            self,
            fullname: str,
            record: tuple[str, bytes | None, bool, bool],
            sources: dict[str, tuple[str, bytes | None, bool, bool]],
    ) -> None:
        self._fullname = fullname
        self._location, self._source, self._is_package, self._is_namespace = record
        self._sources = sources
        mediaforce_location = sources["mediaforce"][0]
        self._snapshot_root = __import__("os").path.dirname(
            __import__("os").path.dirname(mediaforce_location)
        )
        self.path = (
            self._location
            if self._source is not None
            else __import__("os").path.join(self._location, "__init__.py")
        )

    def create_module(self, _spec: object) -> None:
        return None

    def exec_module(self, module: object) -> None:
        _assert_preregistration_bootstrap_authority()
        if self._source is None:
            if self._is_package:
                module.__dict__["__path__"] = [self._location]
            return
        try:
            module.__dict__["__file__"] = self._location
            if self._is_package:
                module.__dict__["__path__"] = [
                    __import__("os").path.dirname(self._location)
                ]
            code = compile(
                self._source,
                self._location,
                "exec",
                dont_inherit=True,
            )
            exec(code, module.__dict__)
        finally:
            _assert_preregistration_bootstrap_authority()

    def get_filename(self, fullname: str) -> str:
        if fullname != self._fullname or self._source is None:
            raise ImportError(fullname)
        return self._location

    def get_source(self, fullname: str) -> str | None:
        if fullname != self._fullname:
            raise ImportError(fullname)
        if self._source is None:
            return None
        importlib_util = __import__("importlib.util", fromlist=("decode_source",))
        return importlib_util.decode_source(self._source)

    def is_package(self, fullname: str) -> bool:
        if fullname != self._fullname:
            raise ImportError(fullname)
        return self._is_package

    def get_data(self, path: str) -> bytes:
        os_module = __import__("os")
        normalized = os_module.path.normpath(path)
        for location, source, _is_package, _is_namespace in self._sources.values():
            if source is not None and os_module.path.normpath(location) == normalized:
                return source
        try:
            if os_module.path.commonpath((self._snapshot_root, normalized)) != (
                    self._snapshot_root
            ):
                raise OSError("bound mediaforce resource escaped its snapshot")
        except ValueError as exc:
            raise OSError("bound mediaforce resource escaped its snapshot") from exc
        with open(normalized, "rb") as handle:
            return handle.read()

    def get_resource_reader(self, fullname: str) -> object | None:
        if fullname != self._fullname or not self._is_package:
            return None
        readers = __import__("importlib.resources.readers", fromlist=("FileReader",))
        return readers.FileReader(self)


class _BoundMediaforceFinder:
    def __init__(
            self,
            sources: dict[str, tuple[str, bytes | None, bool, bool]],
    ) -> None:
        self._sources = sources
        self._importlib_util = __import__("importlib.util", fromlist=("spec_from_loader",))
        self._importlib_machinery = __import__(
            "importlib.machinery",
            fromlist=("ModuleSpec",),
        )

    def find_spec(
            self,
            fullname: str,
            _path: object = None,
            _target: object = None,
    ) -> object | None:
        if fullname != "mediaforce" and not fullname.startswith("mediaforce."):
            return None
        record = self._sources.get(fullname)
        if record is None:
            raise ModuleNotFoundError(
                f"{fullname!r} is outside the bound mediaforce source snapshot",
                name=fullname,
            )
        loader = _BoundMediaforceLoader(fullname, record, self._sources)
        location, _source, is_package, is_namespace = record
        if is_namespace:
            spec = self._importlib_machinery.ModuleSpec(
                fullname,
                loader,
                is_package=True,
            )
            spec.submodule_search_locations = [location]
            return spec
        spec = self._importlib_util.spec_from_loader(
            fullname,
            loader,
            origin=location,
            is_package=is_package,
        )
        if spec is None:
            raise ImportError(f"could not bind {fullname!r}")
        spec.has_location = True
        if is_package:
            spec.submodule_search_locations = [__import__("os").path.dirname(location)]
        return spec


_PREREGISTRATION_BOOTSTRAP_AUTHORITY_MONITOR: (
    _RepositoryAuthorityMonitor | None
) = None
_PREREGISTRATION_BOOTSTRAP_ACTIVE = False
_PREREGISTRATION_BOOTSTRAP_FAILURE: BaseException | None = None


def _record_preregistration_bootstrap_failure(exc: BaseException) -> None:
    global _PREREGISTRATION_BOOTSTRAP_FAILURE
    if (
        _PREREGISTRATION_BOOTSTRAP_ACTIVE
        and _PREREGISTRATION_BOOTSTRAP_FAILURE is None
    ):
        _PREREGISTRATION_BOOTSTRAP_FAILURE = exc


def _assert_preregistration_bootstrap_authority() -> None:
    if not _PREREGISTRATION_BOOTSTRAP_ACTIVE:
        return
    failure = _PREREGISTRATION_BOOTSTRAP_FAILURE
    if failure is not None:
        raise RuntimeError(
            "AV1 preregistration bootstrap authority previously failed"
        ) from failure
    monitor = _PREREGISTRATION_BOOTSTRAP_AUTHORITY_MONITOR
    if monitor is None:
        error = RuntimeError(
            "AV1 preregistration bootstrap authority is unavailable"
        )
        _record_preregistration_bootstrap_failure(error)
        raise error
    try:
        monitor.assert_quiet()
    except BaseException as exc:
        _record_preregistration_bootstrap_failure(exc)
        raise RuntimeError(
            "AV1 preregistration runner detected changed repository authority"
        ) from exc


def _call_preregistration_publication_guard(
        callback: object = None,
) -> None:
    _assert_preregistration_bootstrap_authority()
    if callback is not None:
        callback()
    _assert_preregistration_bootstrap_authority()


def _install_bound_mediaforce_importer(
        monitor: _RepositoryAuthorityMonitor,
) -> None:
    sources = monitor.bound_python_sources()
    finder = _BoundMediaforceFinder(sources)
    sys_module = __import__("sys")
    if any(
        name == "mediaforce" or name.startswith("mediaforce.")
        for name in sys_module.modules
    ):
        raise RuntimeError(
            "AV1 preregistration runner refuses preloaded mediaforce modules"
        )
    sys_module.meta_path.insert(0, finder)
    try:
        __import__("importlib").import_module("mediaforce")
    finally:
        _assert_preregistration_bootstrap_authority()


def _finalize_preregistration_bootstrap_authority() -> None:
    global _PREREGISTRATION_BOOTSTRAP_AUTHORITY_MONITOR
    monitor = _PREREGISTRATION_BOOTSTRAP_AUTHORITY_MONITOR
    if monitor is None:
        if _PREREGISTRATION_BOOTSTRAP_FAILURE is not None:
            raise RuntimeError(
                "AV1 preregistration bootstrap authority previously failed"
            ) from _PREREGISTRATION_BOOTSTRAP_FAILURE
        return
    _PREREGISTRATION_BOOTSTRAP_AUTHORITY_MONITOR = None
    try:
        if _PREREGISTRATION_BOOTSTRAP_FAILURE is not None:
            raise RuntimeError(
                "AV1 preregistration bootstrap authority previously failed"
            ) from _PREREGISTRATION_BOOTSTRAP_FAILURE
        monitor.assert_quiet()
    except BaseException as exc:
        _record_preregistration_bootstrap_failure(exc)
        raise
    finally:
        monitor.close()


def _fail_closed_preregistration_bootstrap_exit() -> None:
    try:
        _finalize_preregistration_bootstrap_authority()
    except BaseException:
        bootstrap_os = __import__("os")
        try:
            bootstrap_os.write(
                2,
                b"AV1 preregistration runner detected changed repository authority\n",
            )
        finally:
            bootstrap_os._exit(1)


def _canonical_system_git_binary(platform: str) -> str:
    if platform == "darwin":
        return "/Library/Developer/CommandLineTools/usr/bin/git"
    return "/usr/bin/git"


def _preregistration_executable_rejection_reasons(
        *,
        owner_uid: int,
        mode: int,
        current_uid: int,
) -> tuple[str, ...]:
    bootstrap_stat = __import__("stat")
    reasons: list[str] = []
    if not bootstrap_stat.S_ISREG(mode):
        reasons.append("executable_type")
    if owner_uid not in {0, current_uid}:
        reasons.append("executable_owner")
    if mode & bootstrap_stat.S_IWOTH:
        reasons.append("executable_world_write")
    if mode & (bootstrap_stat.S_ISUID | bootstrap_stat.S_ISGID):
        reasons.append("executable_set_id")
    if mode & bootstrap_stat.S_IWGRP:
        reasons.append("executable_group_write")
    return tuple(reasons)


def _preregistration_executable_is_trusted(
        *,
        owner_uid: int,
        mode: int,
        current_uid: int,
) -> bool:
    return not _preregistration_executable_rejection_reasons(
        owner_uid=owner_uid,
        mode=mode,
        current_uid=current_uid,
    )


def _system_git_binary_is_trusted(path: str) -> bool:
    bootstrap_os = __import__("os")
    bootstrap_stat = __import__("stat")
    descriptor = -1
    try:
        flags = bootstrap_os.O_RDONLY | getattr(bootstrap_os, "O_CLOEXEC", 0)
        if hasattr(bootstrap_os, "O_NOFOLLOW"):
            flags |= bootstrap_os.O_NOFOLLOW
        descriptor = bootstrap_os.open(path, flags)
        descriptor_info = bootstrap_os.fstat(descriptor)
        path_info = bootstrap_os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            bootstrap_os.close(descriptor)
    return (
        bootstrap_stat.S_ISREG(descriptor_info.st_mode)
        and descriptor_info.st_uid == 0
        and not descriptor_info.st_mode & (
            0o022 | bootstrap_stat.S_ISUID | bootstrap_stat.S_ISGID
        )
        and (descriptor_info.st_dev, descriptor_info.st_ino)
        == (path_info.st_dev, path_info.st_ino)
    )


def _assert_preregistration_import_tree_clean(
        repository_root: object = None,
        *,
        retain_authority_monitor: bool = False,
) -> object | None:
    bootstrap_sys = __import__("sys")
    bootstrap_os = __import__("os")
    bootstrap_stat = __import__("stat")
    if repository_root is None:
        bootstrap_sys.dont_write_bytecode = True
        if not bootstrap_sys.flags.isolated or not bootstrap_sys.flags.no_site:
            raise RuntimeError(
                "AV1 preregistration runner must start with Python -I -S"
            )
        root = bootstrap_os.path.realpath(
            bootstrap_os.path.join(
                bootstrap_os.path.dirname(__file__),
                bootstrap_os.pardir,
            )
        )
        expected_script = bootstrap_os.path.join(
            root,
            "scripts",
            "verify_av1_cold_start_preregistration.py",
        )
        if bootstrap_os.path.realpath(__file__) != expected_script:
            raise RuntimeError(
                "AV1 preregistration runner must execute from its canonical repository path"
            )
    else:
        root = bootstrap_os.path.realpath(bootstrap_os.fspath(repository_root))

    def fail_closed(exc: BaseException) -> None:
        raise RuntimeError(
            "AV1 preregistration runner could not inspect repository imports"
        ) from exc

    def read_git_metadata_file(path: str, label: str) -> str:
        flags = bootstrap_os.O_RDONLY | getattr(bootstrap_os, "O_CLOEXEC", 0)
        if hasattr(bootstrap_os, "O_NOFOLLOW"):
            flags |= bootstrap_os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = bootstrap_os.open(path, flags)
            descriptor_info = bootstrap_os.fstat(descriptor)
            path_info = bootstrap_os.stat(path, follow_symlinks=False)
            if (
                not bootstrap_os.path.isfile(path)
                or descriptor_info.st_uid != bootstrap_os.getuid()
                or descriptor_info.st_nlink != 1
                or descriptor_info.st_size > 4096
                or descriptor_info.st_mode & 0o022
                or (descriptor_info.st_dev, descriptor_info.st_ino)
                != (path_info.st_dev, path_info.st_ino)
            ):
                raise RuntimeError(
                    f"AV1 preregistration runner refuses unsafe {label}"
                )
            raw = bytearray()
            while len(raw) <= 4096:
                chunk = bootstrap_os.read(descriptor, 4096)
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > 4096:
                raise RuntimeError(
                    f"AV1 preregistration runner refuses oversized {label}"
                )
            return bytes(raw).decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                f"AV1 preregistration runner refuses invalid {label}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"AV1 preregistration runner could not inspect {label}"
            ) from exc
        finally:
            if descriptor >= 0:
                bootstrap_os.close(descriptor)

    def resolved_git_metadata() -> tuple[str, tuple[int, int], str, tuple[int, int]]:
        metadata_path = bootstrap_os.path.join(root, ".git")
        try:
            metadata_info = bootstrap_os.stat(
                metadata_path,
                follow_symlinks=False,
            )
        except OSError as exc:
            fail_closed(exc)
        if bootstrap_stat.S_ISDIR(metadata_info.st_mode):
            git_directory = bootstrap_os.path.realpath(metadata_path)
        elif bootstrap_stat.S_ISREG(metadata_info.st_mode):
            pointer = read_git_metadata_file(metadata_path, "Git metadata pointer")
            prefix = "gitdir:"
            if not pointer.startswith(prefix):
                raise RuntimeError(
                    "AV1 preregistration runner refuses invalid Git metadata pointer"
                )
            candidate = pointer[len(prefix):].strip()
            if not candidate:
                raise RuntimeError(
                    "AV1 preregistration runner refuses invalid Git metadata pointer"
                )
            if not bootstrap_os.path.isabs(candidate):
                candidate = bootstrap_os.path.join(root, candidate)
            git_directory = bootstrap_os.path.realpath(candidate)
            backlink = read_git_metadata_file(
                bootstrap_os.path.join(git_directory, "gitdir"),
                "Git worktree backlink",
            )
            if not bootstrap_os.path.isabs(backlink):
                backlink = bootstrap_os.path.join(git_directory, backlink)
            if bootstrap_os.path.realpath(backlink) != bootstrap_os.path.realpath(
                    metadata_path
            ):
                raise RuntimeError(
                    "AV1 preregistration runner refuses foreign Git metadata"
                )
        else:
            raise RuntimeError(
                "AV1 preregistration runner refuses unsafe Git metadata"
            )
        commondir_path = bootstrap_os.path.join(git_directory, "commondir")
        if bootstrap_os.path.exists(commondir_path):
            common_value = read_git_metadata_file(
                commondir_path,
                "Git common-directory pointer",
            )
            if not common_value:
                raise RuntimeError(
                    "AV1 preregistration runner refuses invalid Git common directory"
                )
            if not bootstrap_os.path.isabs(common_value):
                common_value = bootstrap_os.path.join(
                    git_directory,
                    common_value,
                )
            common_directory = bootstrap_os.path.realpath(common_value)
            expected_worktrees_parent = bootstrap_os.path.join(
                common_directory,
                "worktrees",
            )
            if bootstrap_os.path.dirname(git_directory) != expected_worktrees_parent:
                raise RuntimeError(
                    "AV1 preregistration runner refuses foreign Git worktree metadata"
                )
        else:
            common_directory = git_directory

        def directory_identity(path: str, label: str) -> tuple[int, int]:
            flags = bootstrap_os.O_RDONLY | getattr(bootstrap_os, "O_DIRECTORY", 0)
            if hasattr(bootstrap_os, "O_NOFOLLOW"):
                flags |= bootstrap_os.O_NOFOLLOW
            descriptor = bootstrap_os.open(path, flags)
            try:
                descriptor_info = bootstrap_os.fstat(descriptor)
                path_info = bootstrap_os.stat(path, follow_symlinks=False)
                if (
                    not bootstrap_os.path.isdir(path)
                    or descriptor_info.st_uid != bootstrap_os.getuid()
                    or descriptor_info.st_mode & 0o022
                    or (descriptor_info.st_dev, descriptor_info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)
                ):
                    raise RuntimeError(
                        f"AV1 preregistration runner refuses unsafe {label}"
                    )
                return descriptor_info.st_dev, descriptor_info.st_ino
            finally:
                bootstrap_os.close(descriptor)

        return (
            git_directory,
            directory_identity(git_directory, "Git directory"),
            common_directory,
            directory_identity(common_directory, "Git common directory"),
        )

    for relative_root in ("mediaforce", "scripts"):
        import_root = bootstrap_os.path.join(root, relative_root)
        if not bootstrap_os.path.isdir(import_root):
            continue
        for _current_root, directory_names, filenames in bootstrap_os.walk(
                import_root,
                followlinks=False,
                onerror=fail_closed,
        ):
            if "__pycache__" in directory_names:
                raise RuntimeError(
                    "AV1 preregistration runner refuses repository bytecode caches"
                )
            if any(
                filename.endswith((".pyc", ".pyo"))
                for filename in filenames
            ):
                raise RuntimeError(
                    "AV1 preregistration runner refuses repository bytecode artifacts"
                )

    (
        git_directory,
        git_directory_identity,
        git_common_directory,
        git_common_directory_identity,
    ) = resolved_git_metadata()
    root_info = bootstrap_os.stat(root, follow_symlinks=False)
    if (
        not bootstrap_stat.S_ISDIR(root_info.st_mode)
        or bootstrap_stat.S_ISLNK(root_info.st_mode)
    ):
        raise RuntimeError(
            "AV1 preregistration runner refuses unsafe repository root"
        )
    root_identity = (root_info.st_dev, root_info.st_ino)
    execution_symlink_message = (
        "AV1 preregistration runner refuses symlinked execution authority"
    )
    _assert_execution_authority_roots_are_not_symlinked(
        root,
        failure_type=RuntimeError,
        message=execution_symlink_message,
    )
    metadata_path = bootstrap_os.path.join(root, ".git")
    metadata_info = bootstrap_os.stat(metadata_path, follow_symlinks=False)
    metadata_files = (
        (metadata_path,)
        if bootstrap_stat.S_ISREG(metadata_info.st_mode)
        else ()
    )
    closed_authority_message = (
        "AV1 preregistration runner refuses external or symlinked Git authority"
    )
    _assert_git_metadata_authority_is_closed(
        git_directory,
        git_common_directory,
        failure_type=RuntimeError,
        message=closed_authority_message,
    )
    authority_monitor = _RepositoryAuthorityMonitor(
        root=root,
        authority_directories=(
            (root, root_identity, False),
            (git_directory, git_directory_identity, True),
            (git_common_directory, git_common_directory_identity, True),
        ),
        metadata_roots=(git_directory, git_common_directory),
        metadata_excluded_roots=(
            bootstrap_os.path.join(git_common_directory, "worktrees"),
        ),
        metadata_files=metadata_files,
        failure_type=RuntimeError,
        changed_message=(
            "AV1 preregistration runner detected changed Git metadata or import authority"
        ),
        unavailable_message=(
            "AV1 preregistration Git authority monitoring is unavailable"
        ),
    )
    _assert_git_metadata_authority_is_closed(
        git_directory,
        git_common_directory,
        failure_type=RuntimeError,
        message=closed_authority_message,
    )
    authority_monitor.assert_quiet()
    for relative_root in ("mediaforce", "scripts"):
        authority_monitor.add_worktree_tree(
            bootstrap_os.path.join(root, relative_root)
        )
    source_default_authority_paths = tuple(
        relative_path
        for relative_path in (
            "config/defaults.toml",
            "hatch_build.py",
            "pyproject.toml",
        )
        if bootstrap_os.path.lexists(
            bootstrap_os.path.join(root, relative_path)
        )
    )
    if source_default_authority_paths:
        authority_monitor.add_worktree_paths(source_default_authority_paths)
    git_binary = _canonical_system_git_binary(bootstrap_sys.platform)
    if not _system_git_binary_is_trusted(git_binary):
        raise RuntimeError(
            "AV1 preregistration runner requires trusted direct system Git"
        )

    def git_output(*arguments: str) -> tuple[int, bytes]:
        authority_monitor.assert_quiet()
        bootstrap_time = __import__("time")
        read_descriptor, write_descriptor = bootstrap_os.pipe()
        try:
            process_id = bootstrap_os.fork()
        except BaseException:
            bootstrap_os.close(read_descriptor)
            bootstrap_os.close(write_descriptor)
            authority_monitor.assert_quiet()
            raise
        if process_id == 0:
            try:
                bootstrap_os.close(read_descriptor)
                bootstrap_os.dup2(write_descriptor, 1)
                null_descriptor = bootstrap_os.open(
                    bootstrap_os.devnull,
                    bootstrap_os.O_RDWR,
                )
                bootstrap_os.dup2(null_descriptor, 0)
                bootstrap_os.dup2(null_descriptor, 2)
                bootstrap_os.close(write_descriptor)
                if null_descriptor > 2:
                    bootstrap_os.close(null_descriptor)
                bootstrap_os.chdir(root)
                bootstrap_os.execve(
                    git_binary,
                    (
                        git_binary,
                        "-c",
                        "core.attributesFile=/dev/null",
                        "-c",
                        "core.excludesFile=/dev/null",
                        "-c",
                        "core.fsmonitor=false",
                        "-c",
                        "core.hooksPath=/dev/null",
                        *arguments,
                    ),
                    {
                        "GIT_ATTR_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": "/dev/null",
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_DIR": git_directory,
                        "GIT_COMMON_DIR": git_common_directory,
                        "GIT_NO_LAZY_FETCH": "1",
                        "GIT_NO_REPLACE_OBJECTS": "1",
                        "GIT_OPTIONAL_LOCKS": "0",
                        "GIT_WORK_TREE": root,
                        "HOME": root,
                        "LANG": "C",
                        "LC_ALL": "C",
                        "PATH": "/usr/bin:/bin",
                    },
                )
            except BaseException:
                bootstrap_os._exit(127)
        bootstrap_os.close(write_descriptor)
        output = bytearray()
        deadline = bootstrap_time.monotonic() + 15.0
        process_status = None
        try:
            bootstrap_os.set_blocking(read_descriptor, False)
            while True:
                try:
                    chunk = bootstrap_os.read(read_descriptor, 64 * 1024)
                except BlockingIOError:
                    chunk = None
                if chunk:
                    output.extend(chunk)
                    if len(output) > 1024 * 1024:
                        bootstrap_os.kill(process_id, 9)
                        _waited_process_id, process_status = (
                            bootstrap_os.waitpid(process_id, 0)
                        )
                        raise RuntimeError(
                            "AV1 preregistration repository inspection is oversized"
                        )
                waited_process_id, status = bootstrap_os.waitpid(
                    process_id,
                    bootstrap_os.WNOHANG,
                )
                if waited_process_id == process_id:
                    process_status = status
                    bootstrap_os.set_blocking(read_descriptor, True)
                    while True:
                        chunk = bootstrap_os.read(read_descriptor, 64 * 1024)
                        if not chunk:
                            break
                        output.extend(chunk)
                        if len(output) > 1024 * 1024:
                            raise RuntimeError(
                                "AV1 preregistration repository inspection is oversized"
                            )
                    break
                if bootstrap_time.monotonic() >= deadline:
                    bootstrap_os.kill(process_id, 9)
                    _waited_process_id, process_status = bootstrap_os.waitpid(
                        process_id,
                        0,
                    )
                    raise RuntimeError(
                        "AV1 preregistration repository inspection timed out"
                    )
                bootstrap_time.sleep(0.01)
        except BaseException:
            if process_status is None:
                try:
                    bootstrap_os.kill(process_id, 9)
                except ProcessLookupError:
                    pass
                try:
                    bootstrap_os.waitpid(process_id, 0)
                except ChildProcessError:
                    pass
            raise
        finally:
            bootstrap_os.close(read_descriptor)
            authority_monitor.assert_quiet()
        if process_status is None:
            raise RuntimeError(
                "AV1 preregistration repository inspection did not finish"
            )
        return bootstrap_os.waitstatus_to_exitcode(process_status), bytes(output)

    pathspec = (
        "--",
        "mediaforce",
        "scripts",
        "config/defaults.toml",
        "hatch_build.py",
        "pyproject.toml",
        "uv.lock",
    )
    for ignored in (False, True):
        arguments = ["ls-files", "-z", "--others"]
        if ignored:
            arguments.append("--ignored")
        arguments.extend(("--exclude-standard", *pathspec))
        return_code, output = git_output(*arguments)
        if return_code != 0:
            raise RuntimeError(
                "AV1 preregistration runner could not inspect repository imports"
            )
        if output:
            raise RuntimeError(
                "AV1 preregistration runner refuses untracked or ignored import state"
            )
    def repository_snapshot() -> tuple[bytes, bytes, bytes]:
        outputs: list[bytes] = []
        for arguments in (
            ("ls-files", "--cached", "--stage", "-z", *pathspec),
            ("ls-tree", "-r", "-z", "--full-tree", "HEAD", *pathspec),
            ("ls-files", "--cached", "-v", "-z", *pathspec),
        ):
            return_code, output = git_output(*arguments)
            if return_code != 0:
                raise RuntimeError(
                    "AV1 preregistration runner could not inspect repository imports"
                )
            outputs.append(output)
        return outputs[0], outputs[1], outputs[2]

    modified_import_message = (
        "AV1 preregistration runner refuses modified import state"
    )
    initial_snapshot = repository_snapshot()
    _assert_exact_git_worktree_snapshot(
        root,
        *initial_snapshot,
        capture_mediaforce_python=False,
        reject_symlink_components=True,
        failure_type=RuntimeError,
        message=modified_import_message,
    )
    authority_monitor.assert_quiet()
    verified_snapshot = repository_snapshot()
    if verified_snapshot != initial_snapshot:
        raise RuntimeError(
            "AV1 preregistration runner detected changed Git metadata or import authority"
        )
    captured_entries = _assert_exact_git_worktree_snapshot(
        root,
        *verified_snapshot,
        capture_mediaforce_python=True,
        capture_mediaforce_resources=True,
        reject_symlink_components=True,
        failure_type=RuntimeError,
        message=modified_import_message,
    )
    snapshot_directory = _materialize_bound_mediaforce_snapshot(
        root,
        captured_entries,
        failure_type=RuntimeError,
        message=modified_import_message,
    )
    try:
        authority_monitor.bind_bound_snapshot_directory(snapshot_directory)
        authority_monitor.bind_python_sources(
            _build_bound_mediaforce_sources(
                snapshot_directory,
                captured_entries,
                failure_type=RuntimeError,
                message=modified_import_message,
            )
        )
    except BaseException:
        authority_monitor.close()
        raise

    if repository_root is None:
        version_directory = (
            f"python{bootstrap_sys.version_info.major}."
            f"{bootstrap_sys.version_info.minor}"
        )
        stdlib_setting = getattr(bootstrap_sys, "_stdlib_dir", None)
        if not isinstance(stdlib_setting, str) or not stdlib_setting:
            raise RuntimeError(
                "AV1 preregistration runner cannot identify the standard library"
            )
        stdlib_directory = bootstrap_os.path.realpath(stdlib_setting)
        trusted_paths = [
            bootstrap_os.path.join(
                bootstrap_sys.base_prefix,
                "lib",
                f"python{bootstrap_sys.version_info.major}"
                f"{bootstrap_sys.version_info.minor}.zip",
            ),
            stdlib_directory,
            bootstrap_os.path.join(stdlib_directory, "lib-dynload"),
        ]
        expected_virtual_environment = bootstrap_os.path.realpath(
            bootstrap_os.path.join(root, ".venv")
        )
        declared_virtual_environment = bootstrap_os.environ.get("VIRTUAL_ENV")
        executable_path = bootstrap_os.path.abspath(bootstrap_sys.executable)
        expected_executable_directory = bootstrap_os.path.join(
            expected_virtual_environment,
            "bin",
        )
        allowed_executable_names = {
            "python",
            "python3",
            f"python{bootstrap_sys.version_info.major}",
            (
                f"python{bootstrap_sys.version_info.major}."
                f"{bootstrap_sys.version_info.minor}"
            ),
        }
        executable_realpath = bootstrap_os.path.realpath(executable_path)
        canonical_executable_path = bootstrap_os.path.join(
            expected_executable_directory,
            "python",
        )
        canonical_executable_realpath = bootstrap_os.path.realpath(
            canonical_executable_path
        )
        base_executable = getattr(bootstrap_sys, "_base_executable", None)
        base_executable_realpath = (
            bootstrap_os.path.realpath(base_executable)
            if isinstance(base_executable, str) and base_executable
            else None
        )
        executable_rejection_reasons: list[str] = []
        if (
            bootstrap_os.path.dirname(executable_path)
            != expected_executable_directory
        ):
            executable_rejection_reasons.append("launcher_directory")
        if (
            bootstrap_os.path.basename(executable_path)
            not in allowed_executable_names
        ):
            executable_rejection_reasons.append("launcher_name")
        if base_executable_realpath != executable_realpath:
            executable_rejection_reasons.append("base_executable_path")
        executable_descriptor = -1
        try:
            if not executable_rejection_reasons:
                flags = bootstrap_os.O_RDONLY | getattr(
                    bootstrap_os,
                    "O_CLOEXEC",
                    0,
                )
                if hasattr(bootstrap_os, "O_NOFOLLOW"):
                    flags |= bootstrap_os.O_NOFOLLOW
                executable_descriptor = bootstrap_os.open(
                    executable_realpath,
                    flags,
                )
                descriptor_info = bootstrap_os.fstat(executable_descriptor)
                path_info = bootstrap_os.stat(
                    executable_realpath,
                    follow_symlinks=False,
                )
                base_info = bootstrap_os.stat(
                    base_executable_realpath,
                    follow_symlinks=False,
                )
                canonical_info = bootstrap_os.stat(
                    canonical_executable_realpath,
                    follow_symlinks=False,
                )
                if not bootstrap_os.path.isfile(executable_realpath):
                    executable_rejection_reasons.append("executable_file")
                executable_rejection_reasons.extend(
                    _preregistration_executable_rejection_reasons(
                        owner_uid=descriptor_info.st_uid,
                        mode=descriptor_info.st_mode,
                        current_uid=bootstrap_os.getuid(),
                    )
                )
                descriptor_identity = (
                    descriptor_info.st_dev,
                    descriptor_info.st_ino,
                )
                if descriptor_identity != (path_info.st_dev, path_info.st_ino):
                    executable_rejection_reasons.append(
                        "executable_path_identity"
                    )
                if descriptor_identity != (base_info.st_dev, base_info.st_ino):
                    executable_rejection_reasons.append(
                        "base_executable_inode"
                    )
                if descriptor_identity != (
                    canonical_info.st_dev,
                    canonical_info.st_ino,
                ):
                    executable_rejection_reasons.append(
                        "canonical_launcher_inode"
                    )
        except OSError:
            executable_rejection_reasons.append("executable_inspection")
        finally:
            if executable_descriptor >= 0:
                bootstrap_os.close(executable_descriptor)
        if (
            declared_virtual_environment is not None
            and bootstrap_os.path.realpath(declared_virtual_environment)
            != expected_virtual_environment
        ):
            executable_rejection_reasons.append("declared_virtual_environment")
        if executable_rejection_reasons:
            raise RuntimeError(
                "AV1 preregistration runner requires the canonical repository "
                "virtual environment (failed checks: "
                f"{','.join(dict.fromkeys(executable_rejection_reasons))})"
            )
        trusted_paths.append(
            bootstrap_os.path.join(
                expected_virtual_environment,
                "lib",
                version_directory,
                "site-packages",
            )
        )
        normalized_paths = []
        for candidate in trusted_paths:
            normalized = bootstrap_os.path.realpath(candidate)
            if normalized not in normalized_paths and (
                bootstrap_os.path.exists(normalized)
                or normalized.endswith(".zip")
            ):
                normalized_paths.append(normalized)
        bootstrap_sys.path[:] = normalized_paths
        bootstrap_sys.path_importer_cache.clear()
        if any(
            name == "mediaforce" or name.startswith("mediaforce.")
            for name in bootstrap_sys.modules
        ):
            raise RuntimeError(
                "AV1 preregistration runner refuses preloaded mediaforce modules"
            )
    authority_monitor.assert_quiet()
    if retain_authority_monitor:
        return authority_monitor
    authority_monitor.close()
    return None


if __name__ == "__main__":
    _PREREGISTRATION_BOOTSTRAP_ACTIVE = True
    _PREREGISTRATION_BOOTSTRAP_AUTHORITY_MONITOR = (
        _assert_preregistration_import_tree_clean(
            retain_authority_monitor=True,
        )
    )
    if _PREREGISTRATION_BOOTSTRAP_AUTHORITY_MONITOR is None:
        raise RuntimeError(
            "AV1 preregistration runner could not retain repository authority"
        )
    __import__("atexit").register(
        _fail_closed_preregistration_bootstrap_exit
    )
    _install_bound_mediaforce_importer(
        _PREREGISTRATION_BOOTSTRAP_AUTHORITY_MONITOR
    )


import argparse
from contextlib import contextmanager
import ctypes
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable, cast, Iterator, Sequence, TypeAlias
import uuid

from mediaforce.core.config import (
    DEFAULT_CONFIG_PATH,
    MediaforceConfig,
    load_config,
    migrate_config_state,
)
from mediaforce.core.db import open_readonly_db
from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.core.process_control import (
    ManagedProcessController,
    ProcessCancelledError,
    ProcessDeadlineEnforcementError,
    run_command,
)
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_cold_start_evaluation import (
    AV1ColdStartValidationError,
    AV1ColdStartValidationManifestV1,
    assert_preregistered_av1_cold_start_validation_manifest,
    build_av1_cold_start_validation_report,
    format_av1_cold_start_validation_report,
    load_av1_cold_start_validation_evidence_set,
    load_av1_cold_start_validation_manifest,
)
from mediaforce.tuning.av1_validation_v2 import (
    AV1ValidationManifestV2,
    AV1ValidationV2Error,
    assert_preregistered_av1_validation_manifest_v2,
    assert_preregistered_av1_validation_v2_eligibility,
    build_av1_validation_v2_derivation_authorization,
    load_av1_validation_manifest_v2,
    load_av1_validation_v2_eligibility,
)
from mediaforce.tuning.av1_validation_v3 import (
    AV1ValidationProtocolV3,
    AV1ValidationV3Error,
    assert_preregistered_av1_validation_protocol_v3,
    load_av1_validation_protocol_v3,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationError,
    AV1ValidationV3QualificationPlan,
    assert_av1_validation_v3_qualification_plan_active,
    load_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_coverage import (
    AV1ValidationV3Tier1CoverageError,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_authorization import (
    AV1ValidationV3Tier1CompatAuthorizationError,
    AV1ValidationV3Tier1CompatRequest,
    assert_av1_validation_v3_tier1_compat_request_active,
    build_av1_validation_v3_tier1_compat_grant,
    build_av1_validation_v3_tier1_compat_request,
    load_av1_validation_v3_tier1_compat_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_operation import (
    AV1ValidationV3Tier1CompatOperationError,
    build_av1_validation_v3_tier1_compat_claim,
    run_av1_validation_v3_tier1_compat_probe,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_probe import (
    AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SHA256,
    AV1ValidationV3Tier1CompatProbeError,
    load_av1_validation_v3_tier1_compat_probe_matrix,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_publication import (
    load_published_av1_validation_v3_tier1_compat_grant,
    publish_av1_validation_v3_tier1_compat_claim,
    publish_av1_validation_v3_tier1_compat_grant,
    publish_av1_validation_v3_tier1_compat_request,
    publish_av1_validation_v3_tier1_compat_result,
)
from mediaforce.tuning.av1_validation_v3_tier1_diagnostics import (
    AV1ValidationV3Tier1DiagnosticsError,
    build_av1_validation_v3_tier1_run_diagnostics,
)
from mediaforce.tuning.av1_validation_v3_tier1_execution_publication import (
    load_published_av1_validation_v3_tier1_execution_grant,
    publish_av1_validation_v3_tier1_coverage_receipt,
    publish_av1_validation_v3_tier1_execution_claim,
    publish_av1_validation_v3_tier1_execution_grant,
)
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1ValidationV3Tier1ExecutorError,
    load_av1_validation_v3_tier1_fixture_matrix,
)
from mediaforce.tuning.av1_validation_v3_tier1_grant import (
    AV1ValidationV3Tier1GrantError,
    build_av1_validation_v3_tier1_execution_grant,
)
from mediaforce.tuning.av1_validation_v3_tier1_operation import (
    AV1ValidationV3Tier1OperationError,
    build_av1_validation_v3_tier1_execution_claim,
    run_av1_validation_v3_tier1_synthetic_qualification,
)
from mediaforce.tuning.av1_validation_v3_tier1_publication import (
    AV1ValidationV3Tier1PublicationError,
    ensure_av1_validation_v3_qualification_key,
    publish_av1_validation_v3_tier1_config_snapshot,
    publish_av1_validation_v3_tier1_preparation,
)
from mediaforce.tuning.av1_validation_v3_tier1_config_snapshot import (
    AV1ValidationV3Tier1ConfigSnapshotError,
    assert_av1_validation_v3_tier1_config_snapshot_matches,
    av1_validation_v3_tier1_config_snapshot_sha256,
    build_av1_validation_v3_tier1_config_snapshot,
    validate_av1_validation_v3_tier1_config_snapshot,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    AV1ValidationV3Tier1AuthorizationRequest,
    AV1ValidationV3Tier1RequestError,
    assert_av1_validation_v3_tier1_request_active,
    load_av1_validation_v3_tier1_authorization_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_runtime import (
    AV1ValidationV3Tier1RuntimeError,
    AV1ValidationV3Tier1ToolchainBinding,
    build_av1_validation_v3_tier1_toolchain_binding,
)
from mediaforce.tuning.av1_validation_derivation import (
    AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY,
    AV1_VALIDATION_DERIVATION_REASON_CODES,
    AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_ALLOWLIST,
    AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA,
    AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA_VERSION,
    AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BLOB_BYTES,
    AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BUNDLE_BYTES,
    AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_RESPONSE_BYTES,
    AV1_VALIDATION_DERIVATION_REVIEW_RESPONSE_SCHEMA_NAME,
    AV1_VALIDATION_DERIVATION_CHECKPOINTED_REVIEW_RUN_SCHEMA,
    AV1ValidationDerivationCandidateProposal,
    AV1ValidationDerivationError,
    AV1ValidationDerivationPlan,
    AV1ValidationDerivationSourceCommitment,
    AV1ValidationDerivationReviewClaim,
    AV1ValidationDerivationReviewDecision,
    AV1ValidationDerivationReviewEnvelope,
    assert_av1_validation_derivation_authorization_active,
    assert_av1_validation_derivation_observed_attempts_accepted,
    assert_av1_validation_derivation_repository_identity,
    AV1ValidationDerivationReviewLane,
    av1_validation_derivation_attempt_recovery_action,
    av1_validation_derivation_terminal_intent_published_after,
    av1_validation_derivation_review_analysis_sha256,
    av1_validation_derivation_review_developer_text,
    av1_validation_derivation_candidate_evaluation_public_summary,
    av1_validation_derivation_plan_public_summary,
    av1_validation_derivation_statistics_contract_sha256,
    assert_av1_validation_derivation_source_commitments,
    build_av1_validation_derivation_plan,
    build_av1_validation_derivation_source_commitments,
    build_av1_validation_derivation_review_claim,
    build_av1_validation_derivation_review_attestation,
    build_av1_validation_derivation_review_envelope,
    build_av1_validation_derivation_review_recovery_evidence,
    build_av1_validation_derivation_review_request,
    build_av1_validation_derivation_review_response_schema,
    evaluate_av1_validation_derivation_candidate,
    load_av1_validation_derivation_assignment_claims,
    load_av1_validation_derivation_attempt_publication_state,
    load_av1_validation_derivation_attempts,
    load_av1_validation_derivation_candidate_proposal,
    load_av1_validation_derivation_plan,
    load_av1_validation_derivation_review_claims,
    load_av1_validation_derivation_review_envelope,
    load_av1_validation_derivation_review_response_checkpoint,
    load_av1_validation_derivation_terminal_intents,
    load_av1_validation_derivation_terminal_records,
    load_av1_validation_derivation_verdict_claims,
    load_av1_validation_derivation_verdict_intent,
    retain_av1_validation_derivation_publication_directories,
    parse_av1_validation_derivation_review_response,
    validate_av1_validation_derivation_artifact_root_binding,
    write_av1_validation_derivation_candidate_proposal,
    write_av1_validation_derivation_plan,
    write_av1_validation_derivation_review_claim,
    write_av1_validation_derivation_review_envelope,
    write_av1_validation_derivation_review_response_checkpoint,
)
from mediaforce.web.runtime.av1_validation_derivation import (
    av1_validation_derivation_artifact_root,
    av1_validation_derivation_execution_environment_sha256,
    av1_validation_derivation_runtime_context_sha256,
    assert_av1_validation_derivation_source_snapshot_capacity,
    assert_av1_validation_derivation_execution_environment,
    finalize_av1_validation_derivation_candidate_lock,
    load_current_av1_validation_derivation_observations,
    record_av1_validation_derivation_visual_verdict,
    run_av1_validation_derivation_assignment,
)
from mediaforce.web.runtime import (
    av1_validation_derivation as av1_validation_derivation_runtime,
)
from mediaforce.web.runtime_lock import (
    MediaforceRuntimeBusyError,
    MediaforceRuntimeLockOwnershipError,
    exclusive_mediaforce_runtime_lock,
    reserve_mediaforce_database_identity,
)
from mediaforce.tuning.av1_validation_partition import (
    AV1ValidationPartitionError,
    AV1ValidationPrivatePartition,
    assert_private_artifact_path,
    av1_validation_partition_key_id,
    av1_validation_partition_public_summary,
    build_av1_validation_private_partition,
    ensure_av1_validation_partition_key,
    load_av1_validation_partition_key,
    load_av1_validation_private_partition,
    validate_av1_validation_partition_current_inputs,
    validate_av1_validation_private_partition,
    write_av1_validation_private_partition,
)
from mediaforce.tuning.av1_validation_partition_inventory import (
    AV1ValidationPartitionSourceSHA256Session,
    av1_validation_partition_source_sha256_resolver,
    load_av1_validation_partition_inventory,
)
ValidationManifest: TypeAlias = (
    AV1ColdStartValidationManifestV1
    | AV1ValidationManifestV2
    | AV1ValidationProtocolV3
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL_PREREGISTRATION_RUNNER = (
    REPOSITORY_ROOT / "scripts" / "verify_av1_cold_start_preregistration.py"
)
_AGENT_REVIEW_MAX_SECONDS = 1800
_AGENT_REVIEW_SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_AGENT_REVIEW_GENERIC_IDENTITY = "mediaforce-review"
_REVIEW_GIT_COMMAND_PREFIX = (
    _canonical_system_git_binary(sys.platform),
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "core.excludesFile=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
)
_MACH_O_MAGICS = frozenset({
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _assert_canonical_preregistration_runner() -> None:
    try:
        executing_path = Path(__file__).resolve(strict=True)
        canonical_path = _CANONICAL_PREREGISTRATION_RUNNER.resolve(strict=True)
        executing_info = executing_path.stat()
        canonical_info = canonical_path.stat()
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation preregistration runner identity is unavailable"
        ) from exc
    if (
        executing_path != canonical_path
        or not stat.S_ISREG(executing_info.st_mode)
        or (executing_info.st_dev, executing_info.st_ino)
        != (canonical_info.st_dev, canonical_info.st_ino)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation preregistration runner is not the canonical repository file"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an AV1 cold-start preregistration or aggregate its redacted evidence",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    validate = actions.add_parser(
        "validate", help="Validate one canonical preregistration manifest"
    )
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--json", action="store_true", dest="json_output")

    eligibility = actions.add_parser(
        "validate-eligibility",
        help="Validate the pinned machine-local v2 aggregate eligibility attestation",
    )
    eligibility.add_argument("attestation", type=Path)
    eligibility.add_argument("--json", action="store_true", dest="json_output")

    create_key = actions.add_parser(
        "create-partition-key",
        help="Create one owner-only machine-local key for the private v2 source partition",
    )
    create_key.add_argument("key", type=Path)
    create_key.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    create_key.add_argument("--json", action="store_true", dest="json_output")

    build_partition = actions.add_parser(
        "build-partition",
        help="Build the private v2 source partition without authorizing execution",
    )
    build_partition.add_argument("manifest", type=Path)
    build_partition.add_argument("eligibility", type=Path)
    build_partition.add_argument("--key", type=Path, required=True)
    build_partition.add_argument("--expected-token-key-id", required=True)
    build_partition.add_argument("--output", type=Path, required=True)
    build_partition.add_argument("--selected-at", required=True)
    build_partition.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    build_partition.add_argument("--json", action="store_true", dest="json_output")

    validate_partition = actions.add_parser(
        "validate-partition",
        help="Validate the exact private v2 source partition against current locked inputs",
    )
    validate_partition.add_argument("manifest", type=Path)
    validate_partition.add_argument("eligibility", type=Path)
    validate_partition.add_argument("partition", type=Path)
    validate_partition.add_argument("--key", type=Path, required=True)
    validate_partition.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    validate_partition.add_argument("--json", action="store_true", dest="json_output")

    create_derivation_plan = actions.add_parser(
        "create-derivation-plan",
        help="Create the immutable owner-only v2 derivation authorization and 24-slot worklist",
    )
    create_derivation_plan.add_argument("manifest", type=Path)
    create_derivation_plan.add_argument("eligibility", type=Path)
    create_derivation_plan.add_argument("partition", type=Path)
    create_derivation_plan.add_argument("--key", type=Path, required=True)
    create_derivation_plan.add_argument("--valid-until", required=True)
    create_derivation_plan.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    create_derivation_plan.add_argument("--json", action="store_true", dest="json_output")

    validate_derivation_plan = actions.add_parser(
        "validate-derivation-plan",
        help="Validate the private v2 derivation plan against current locked inputs",
    )
    validate_derivation_plan.add_argument("manifest", type=Path)
    validate_derivation_plan.add_argument("eligibility", type=Path)
    validate_derivation_plan.add_argument("partition", type=Path)
    validate_derivation_plan.add_argument("plan", type=Path)
    validate_derivation_plan.add_argument("--key", type=Path, required=True)
    validate_derivation_plan.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    validate_derivation_plan.add_argument("--json", action="store_true", dest="json_output")

    run_derivation = actions.add_parser(
        "run-derivation-assignment",
        help="Run one exact authorized derivation assignment through unchanged measured full search",
    )
    run_derivation.add_argument("manifest", type=Path)
    run_derivation.add_argument("partition", type=Path)
    run_derivation.add_argument("plan", type=Path)
    run_derivation.add_argument("assignment_id")
    run_derivation.add_argument("--key", type=Path, required=True)
    run_derivation.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_derivation.add_argument("--json", action="store_true", dest="json_output")

    derivation_status = actions.add_parser(
        "derivation-status",
        help="Report privacy-safe derivation attempt and terminal counts",
    )
    derivation_status.add_argument("plan", type=Path)
    derivation_status.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    derivation_status.add_argument("--json", action="store_true", dest="json_output")

    record_verdict = actions.add_parser(
        "record-derivation-verdict",
        help="Append one human visual verdict and freeze its terminal derivation record",
    )
    record_verdict.add_argument("manifest", type=Path)
    record_verdict.add_argument("partition", type=Path)
    record_verdict.add_argument("plan", type=Path)
    record_verdict.add_argument("assignment_id")
    record_verdict.add_argument("--key", type=Path, required=True)
    record_verdict.add_argument("--verdict", choices=("approved", "rejected"), required=True)
    record_verdict.add_argument("--concern-tag", action="append", default=[])
    record_verdict.add_argument("--evidence-id", action="append", default=[])
    record_verdict.add_argument("--moment-index", action="append", type=int, default=[])
    record_verdict.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    record_verdict.add_argument("--json", action="store_true", dest="json_output")

    build_proposal = actions.add_parser(
        "build-derivation-proposal",
        help="Build one deterministic unapproved candidate proposal or exact no-go",
    )
    build_proposal.add_argument("manifest", type=Path)
    build_proposal.add_argument("partition", type=Path)
    build_proposal.add_argument("plan", type=Path)
    build_proposal.add_argument("cell_plan_id")
    build_proposal.add_argument("--key", type=Path, required=True)
    build_proposal.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    build_proposal.add_argument("--json", action="store_true", dest="json_output")

    record_review = actions.add_parser(
        "record-derivation-review",
        help="Record one immutable proposal-bound independent review lane",
    )
    record_review.add_argument("plan", type=Path)
    record_review.add_argument("cell_plan_id")
    record_review.add_argument("--lane", choices=(
        "architecture",
        "statistical_model_contract",
        "privacy_security",
        "experimental_design",
        "adversarial",
    ), required=True)
    record_review.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    record_review.add_argument("--json", action="store_true", dest="json_output")

    finalize_lock = actions.add_parser(
        "finalize-derivation-lock",
        help="Finalize one owner-only candidate lock after all five clean review lanes",
    )
    finalize_lock.add_argument("manifest", type=Path)
    finalize_lock.add_argument("partition", type=Path)
    finalize_lock.add_argument("plan", type=Path)
    finalize_lock.add_argument("cell_plan_id")
    finalize_lock.add_argument("--key", type=Path, required=True)
    finalize_lock.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    finalize_lock.add_argument("--json", action="store_true", dest="json_output")

    report = actions.add_parser(
        "report", help="Build a deterministic aggregate acceptance report"
    )
    report.add_argument("manifest", type=Path)
    report.add_argument("evidence", type=Path)
    report.add_argument("--as-of", required=True)
    report.add_argument(
        "--runtime-state", choices=("paused", "available"), default="paused"
    )
    report.add_argument("--json", action="store_true", dest="json_output")

    write_tier1_config = actions.add_parser(
        "write-tier1-config-snapshot",
        help="Write one owner-only effective-config snapshot for AV1 v3 Tier 1",
    )
    write_tier1_config.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
    )

    create_qualification_key = actions.add_parser(
        "create-qualification-key",
        help="Create one owner-only AV1 v3 qualification key without execution authority",
    )
    create_qualification_key.add_argument(
        "--output-root",
        type=Path,
        required=True,
    )
    create_qualification_key.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    write_tier1_config.add_argument("--output-root", type=Path, required=True)
    write_tier1_config.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )

    publish_tier1 = actions.add_parser(
        "publish-tier1-preparation",
        help="Publish AV1 v3 Tier 1 Gate A0 preparation artifacts (non-executing)",
    )
    publish_tier1.add_argument("protocol", type=Path)
    publish_tier1.add_argument("--qualification-key-id", required=True)
    publish_tier1.add_argument("--repository-commit", required=True)
    publish_tier1.add_argument("--repository-tree", required=True)
    publish_tier1.add_argument("--config-artifact", type=Path, required=True)
    publish_tier1.add_argument("--ffmpeg", type=Path, required=True)
    publish_tier1.add_argument("--ffprobe", type=Path, required=True)
    publish_tier1.add_argument("--output-root", type=Path, required=True)
    publish_tier1.add_argument("--frozen-at", required=True)
    publish_tier1.add_argument("--plan-valid-until", required=True)
    publish_tier1.add_argument("--requested-at", required=True)
    publish_tier1.add_argument("--request-valid-until", required=True)
    publish_tier1.add_argument("--json", action="store_true", dest="json_output")

    authorize_tier1 = actions.add_parser(
        "authorize-tier1-execution",
        help="Publish one owner-only AV1 v3 Tier 1 synthetic execution grant",
    )
    authorize_tier1.add_argument("protocol", type=Path)
    authorize_tier1.add_argument("--plan-artifact", type=Path, required=True)
    authorize_tier1.add_argument("--request-artifact", type=Path, required=True)
    authorize_tier1.add_argument("--config-artifact", type=Path, required=True)
    authorize_tier1.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    authorize_tier1.add_argument("--ffmpeg", type=Path, required=True)
    authorize_tier1.add_argument("--ffprobe", type=Path, required=True)
    authorize_tier1.add_argument("--owner-principal", required=True)
    authorize_tier1.add_argument("--authorized-at", required=True)
    authorize_tier1.add_argument("--valid-until", required=True)
    authorize_tier1.add_argument("--output-root", type=Path, required=True)
    authorize_tier1.add_argument("--json", action="store_true", dest="json_output")

    run_tier1 = actions.add_parser(
        "run-tier1-synthetic-qualification",
        help="Claim and run exactly the four frozen AV1 v3 Tier 1 synthetic fixtures",
    )
    run_tier1.add_argument("protocol", type=Path)
    run_tier1.add_argument("--plan-artifact", type=Path, required=True)
    run_tier1.add_argument("--request-artifact", type=Path, required=True)
    run_tier1.add_argument("--config-artifact", type=Path, required=True)
    run_tier1.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_tier1.add_argument(
        "--matrix",
        type=Path,
        default=REPOSITORY_ROOT / "docs/validation/av1-tier1-synthetic-fixture-matrix-v2.json",
    )
    run_tier1.add_argument("--ffmpeg", type=Path, required=True)
    run_tier1.add_argument("--ffprobe", type=Path, required=True)
    run_tier1.add_argument("--grant-root", type=Path, required=True)
    run_tier1.add_argument("--execution-output-root", type=Path, required=True)
    run_tier1.add_argument("--fixture-output-root", type=Path, required=True)
    run_tier1.add_argument("--json", action="store_true", dest="json_output")

    publish_compat = actions.add_parser(
        "publish-tier1-compat-request",
        help="Publish one non-executing AV1 v3 Tier 1 compatibility-probe request",
    )
    publish_compat.add_argument("protocol", type=Path)
    publish_compat.add_argument("--plan-artifact", type=Path, required=True)
    publish_compat.add_argument("--config-artifact", type=Path, required=True)
    publish_compat.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    publish_compat.add_argument(
        "--probe-matrix",
        type=Path,
        default=REPOSITORY_ROOT / "docs/validation/av1-tier1-compat-probe-matrix-v1.json",
    )
    publish_compat.add_argument("--ffmpeg", type=Path, required=True)
    publish_compat.add_argument("--ffprobe", type=Path, required=True)
    publish_compat.add_argument("--requested-at", required=True)
    publish_compat.add_argument("--request-valid-until", required=True)
    publish_compat.add_argument("--output-root", type=Path, required=True)
    publish_compat.add_argument("--json", action="store_true", dest="json_output")

    authorize_compat = actions.add_parser(
        "authorize-tier1-compat-probe",
        help="Publish one owner-only AV1 v3 Tier 1 compatibility-probe grant",
    )
    authorize_compat.add_argument("protocol", type=Path)
    authorize_compat.add_argument("--plan-artifact", type=Path, required=True)
    authorize_compat.add_argument("--request-artifact", type=Path, required=True)
    authorize_compat.add_argument("--config-artifact", type=Path, required=True)
    authorize_compat.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    authorize_compat.add_argument(
        "--probe-matrix",
        type=Path,
        default=REPOSITORY_ROOT / "docs/validation/av1-tier1-compat-probe-matrix-v1.json",
    )
    authorize_compat.add_argument("--ffmpeg", type=Path, required=True)
    authorize_compat.add_argument("--ffprobe", type=Path, required=True)
    authorize_compat.add_argument("--owner-principal", required=True)
    authorize_compat.add_argument("--authorized-at", required=True)
    authorize_compat.add_argument("--valid-until", required=True)
    authorize_compat.add_argument("--output-root", type=Path, required=True)
    authorize_compat.add_argument("--json", action="store_true", dest="json_output")

    run_compat = actions.add_parser(
        "run-tier1-compat-probe",
        help="Run one claimed non-evidentiary AV1 v3 Tier 1 compatibility probe",
    )
    run_compat.add_argument("protocol", type=Path)
    run_compat.add_argument("--plan-artifact", type=Path, required=True)
    run_compat.add_argument("--request-artifact", type=Path, required=True)
    run_compat.add_argument("--config-artifact", type=Path, required=True)
    run_compat.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_compat.add_argument(
        "--probe-matrix",
        type=Path,
        default=REPOSITORY_ROOT / "docs/validation/av1-tier1-compat-probe-matrix-v1.json",
    )
    run_compat.add_argument("--ffmpeg", type=Path, required=True)
    run_compat.add_argument("--ffprobe", type=Path, required=True)
    run_compat.add_argument("--grant-root", type=Path, required=True)
    run_compat.add_argument("--execution-output-root", type=Path, required=True)
    run_compat.add_argument("--variant-output-root", type=Path, required=True)
    run_compat.add_argument("--json", action="store_true", dest="json_output")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _assert_preregistration_bootstrap_authority()
    _assert_canonical_preregistration_runner()
    args = build_parser().parse_args(argv)
    try:
        if args.action == "create-partition-key":
            assert_private_artifact_path(args.key, repository_root=REPOSITORY_ROOT)
            config = load_config(args.config)
            try:
                _assert_preregistration_bootstrap_authority()
                with exclusive_mediaforce_runtime_lock(
                    config,
                    owner_payload={"purpose": "av1-partition-key-create"},
                ):
                    _assert_preregistration_bootstrap_authority()
                    migrate_config_state(config)
                    _assert_preregistration_bootstrap_authority()
                    reserve_mediaforce_database_identity(config)
                    _assert_preregistration_bootstrap_authority()
                    token_key_id, created = ensure_av1_validation_partition_key(
                        args.key
                    )
                    _assert_preregistration_bootstrap_authority()
            except MediaforceRuntimeBusyError as exc:
                raise AV1ValidationPartitionError(
                    "AV1 partition key creation requires the Mediaforce runtime to be paused"
                ) from exc
            payload = {
                "created": created,
                "token_key_id": token_key_id,
                "runtime_execution_authorized": False,
                "derivation_execution_authorized": False,
                "holdout_execution_authorized": False,
            }
            _print_partition_payload(payload, json_output=args.json_output)
            return 0

        if args.action in {"build-partition", "validate-partition"}:
            return _run_partition_action(args)

        if args.action == "validate-eligibility":
            assert_private_artifact_path(
                args.attestation,
                repository_root=REPOSITORY_ROOT,
            )
            eligibility = load_av1_validation_v2_eligibility(args.attestation)
            assert_preregistered_av1_validation_v2_eligibility(eligibility)
            payload = {
                "eligibility_valid": True,
                "runtime_execution_authorized": False,
                "derivation_execution_authorized": False,
                "holdout_execution_authorized": False,
            }
            _print_partition_payload(payload, json_output=args.json_output)
            return 0

        if args.action in {
            "create-derivation-plan",
            "validate-derivation-plan",
            "run-derivation-assignment",
            "derivation-status",
            "record-derivation-verdict",
            "build-derivation-proposal",
            "record-derivation-review",
            "finalize-derivation-lock",
        }:
            return _run_derivation_action(args)

        if args.action == "publish-tier1-preparation":
            return _run_publish_tier1_preparation(args)

        if args.action == "write-tier1-config-snapshot":
            return _run_write_tier1_config_snapshot(args)

        if args.action == "create-qualification-key":
            return _run_create_qualification_key(args)

        if args.action == "authorize-tier1-execution":
            return _run_authorize_tier1_execution(args)

        if args.action == "run-tier1-synthetic-qualification":
            return _run_tier1_synthetic_qualification(args)

        if args.action == "publish-tier1-compat-request":
            return _run_publish_tier1_compat_request(args)

        if args.action == "authorize-tier1-compat-probe":
            return _run_authorize_tier1_compat_probe(args)

        if args.action == "run-tier1-compat-probe":
            return _run_tier1_compat_probe(args)

        manifest = _load_manifest(args.manifest)
        if isinstance(manifest, AV1ValidationProtocolV3):
            assert_preregistered_av1_validation_protocol_v3(manifest)
        elif isinstance(manifest, AV1ValidationManifestV2):
            assert_preregistered_av1_validation_manifest_v2(manifest)
        else:
            assert_preregistered_av1_cold_start_validation_manifest(manifest)
        if args.action == "validate":
            payload = _validation_payload(manifest)
            if args.json_output:
                rendered_payload = json.dumps(payload, indent=2, sort_keys=True)
            elif isinstance(manifest, AV1ValidationProtocolV3):
                rendered_payload = (
                    f"protocol={manifest.protocol_id} state={payload['state']} "
                    f"candidates={len(manifest.candidate_cells)} "
                    f"tier2_strata={len(manifest.tier2_strata)} "
                    "runtime_execution_authorized=false "
                    "qualification_execution_authorized=false"
                )
            else:
                rendered_payload = (
                    f"manifest={manifest.manifest_id} state={payload['state']} "
                    f"plans={len(manifest.cell_plans)} cases={len(manifest.cases)} "
                    "runtime_execution_authorized=false "
                    f"holdout_execution_authorized={str(payload['holdout_execution_authorized']).lower()}"
                )
            _assert_preregistration_bootstrap_authority()
            print(rendered_payload)
            return 0

        if isinstance(manifest, AV1ValidationProtocolV3):
            raise AV1ValidationV3Error(
                "AV1 v3 reports remain blocked until complete independently reviewed holdout evidence exists"
            )
        if isinstance(manifest, AV1ValidationManifestV2):
            raise AV1ValidationV2Error(
                "AV1 v2 holdout reports remain blocked until a separate execution authorization exists"
            )
        evidence_set = load_av1_cold_start_validation_evidence_set(args.evidence)
        report = build_av1_cold_start_validation_report(
            manifest,
            evidence_set,
            as_of=args.as_of,
            runtime_state=args.runtime_state,
        )
        if args.json_output:
            rendered_report = json.dumps(report.to_payload(), indent=2, sort_keys=True)
        else:
            rendered_report = format_av1_cold_start_validation_report(report)
        _assert_preregistration_bootstrap_authority()
        print(rendered_report)
        return 0 if report.supports_publication_review else 2
    except OSError:
        print(
            "AV1 cold-start validation failed: private or repository input is unreadable",
            file=sys.stderr,
        )
        return 1
    except (
        AV1ColdStartValidationError,
        AV1ValidationDerivationError,
        AV1ValidationPartitionError,
        AV1ValidationV2Error,
        AV1ValidationV3Error,
        AV1ValidationV3Tier1PublicationError,
        json.JSONDecodeError,
    ) as exc:
        print(f"AV1 cold-start validation failed: {exc}", file=sys.stderr)
        return 1
    except (TypeError, ValueError):
        print(
            "AV1 cold-start validation failed: private or repository input is invalid",
            file=sys.stderr,
        )
        return 1


def _run_publish_tier1_preparation(args: argparse.Namespace) -> int:
    _assert_preregistration_bootstrap_authority()
    _assert_canonical_preregistration_runner()
    try:
        protocol = load_av1_validation_protocol_v3(args.protocol)
        assert_preregistered_av1_validation_protocol_v3(protocol)
        _assert_preregistration_bootstrap_authority()
        try:
            repository_commit, repository_tree = _live_repository_identity()
        except AV1ValidationDerivationError:
            return _print_tier1_publication_failure(
                "repository_identity_unavailable",
                json_output=args.json_output,
            )
        _assert_preregistration_bootstrap_authority()
        if (
            repository_commit != args.repository_commit
            or repository_tree != args.repository_tree
        ):
            return _print_tier1_publication_failure(
                "repository_identity_mismatch",
                json_output=args.json_output,
            )
        config_bytes = args.config_artifact.read_bytes()
        validate_av1_validation_v3_tier1_config_snapshot(config_bytes)
        result = publish_av1_validation_v3_tier1_preparation(
            protocol=protocol,
            qualification_key_id=args.qualification_key_id,
            repository_commit=repository_commit,
            repository_tree=repository_tree,
            config_bytes=config_bytes,
            ffmpeg_path=args.ffmpeg,
            ffprobe_path=args.ffprobe,
            output_root=args.output_root,
            repository_root=REPOSITORY_ROOT,
            frozen_at=args.frozen_at,
            plan_valid_until=args.plan_valid_until,
            requested_at=args.requested_at,
            request_valid_until=args.request_valid_until,
        )
    except OSError:
        return _print_tier1_publication_failure(
            "input_unreadable",
            json_output=args.json_output,
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        return _print_tier1_publication_failure(
            exc.reason_code,
            json_output=args.json_output,
        )
    except (AV1ValidationV3Error, TypeError, ValueError):
        return _print_tier1_publication_failure(
            "input_invalid",
            json_output=args.json_output,
        )
    _assert_preregistration_bootstrap_authority()
    _print_partition_payload(result.to_summary(), json_output=args.json_output)
    return 0


def _run_authorize_tier1_execution(args: argparse.Namespace) -> int:
    _assert_preregistration_bootstrap_authority()
    _assert_canonical_preregistration_runner()
    try:
        (
            protocol,
            plan,
            request,
            _,
            _,
            _,
        ) = _load_tier1_execution_inputs(args, as_of=args.authorized_at)
        grant = build_av1_validation_v3_tier1_execution_grant(
            protocol=protocol,
            plan=plan,
            request=request,
            owner_principal=args.owner_principal,
            authorized_at=args.authorized_at,
            valid_until=args.valid_until,
        )
        result = publish_av1_validation_v3_tier1_execution_grant(
            grant=grant,
            output_root=args.output_root,
            repository_root=REPOSITORY_ROOT,
        )
    except OSError:
        return _print_tier1_publication_failure(
            "input_unreadable",
            json_output=args.json_output,
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        return _print_tier1_publication_failure(
            exc.reason_code,
            json_output=args.json_output,
        )
    except (
        AV1ValidationDerivationError,
        AV1ValidationV3Error,
        AV1ValidationV3QualificationError,
        AV1ValidationV3Tier1ConfigSnapshotError,
        AV1ValidationV3Tier1GrantError,
        AV1ValidationV3Tier1RequestError,
        AV1ValidationV3Tier1RuntimeError,
        TypeError,
        ValueError,
    ):
        return _print_tier1_publication_failure(
            "input_invalid",
            json_output=args.json_output,
        )
    _assert_preregistration_bootstrap_authority()
    _print_partition_payload(result.to_summary(), json_output=args.json_output)
    return 0


def _run_tier1_synthetic_qualification(args: argparse.Namespace) -> int:
    _assert_preregistration_bootstrap_authority()
    _assert_canonical_preregistration_runner()
    claim_created = False
    claim_exists = False
    claimed_at = _now_iso()
    try:
        (
            protocol,
            plan,
            request,
            config,
            config_snapshot_bytes,
            toolchain,
        ) = _load_tier1_execution_inputs(args, as_of=claimed_at)
        matrix_payload = load_av1_validation_v3_tier1_fixture_matrix(
            args.matrix
        )
        grant = load_published_av1_validation_v3_tier1_execution_grant(
            output_root=args.grant_root,
            repository_root=REPOSITORY_ROOT,
            request_id=request.request_id,
        )
        claim = build_av1_validation_v3_tier1_execution_claim(
            protocol=protocol,
            plan=plan,
            request=request,
            grant=grant,
            claimed_at=claimed_at,
        )

        def claim_execution() -> str:
            nonlocal claim_created, claim_exists
            try:
                claim_result = publish_av1_validation_v3_tier1_execution_claim(
                    claim=claim,
                    output_root=args.execution_output_root,
                    repository_root=REPOSITORY_ROOT,
                )
            except AV1ValidationV3Tier1PublicationError as exc:
                if exc.reason_code in {
                    "execution_already_claimed",
                    "publication_cleanup_failed",
                }:
                    claim_exists = True
                raise
            claim_exists = True
            claim_created = claim_result.created
            if not claim_result.created:
                raise AV1ValidationV3Tier1OperationError(
                    "AV1 v3 Tier 1 execution was already claimed"
                )
            return claim.claim_id

        operation = run_av1_validation_v3_tier1_synthetic_qualification(
            config,
            protocol=protocol,
            plan=plan,
            request=request,
            grant=grant,
            config_snapshot_bytes=config_snapshot_bytes,
            matrix=matrix_payload,
            output_directory=args.fixture_output_root,
            repository_root=REPOSITORY_ROOT,
            toolchain=toolchain,
            claim=claim,
            claim_execution=claim_execution,
        )
        diagnostics = build_av1_validation_v3_tier1_run_diagnostics(
            attestation=operation.attestation,
            outcomes=operation.outcomes,
            diagnostics=operation.diagnostics,
        )
        receipt = publish_av1_validation_v3_tier1_coverage_receipt(
            attestation=operation.attestation,
            diagnostics=diagnostics,
            output_root=args.execution_output_root,
            repository_root=REPOSITORY_ROOT,
        )
    except OSError:
        return _print_tier1_execution_failure(
            "execution_failed" if claim_created else "input_unreadable",
            claimed=claim_exists or claim_created,
            json_output=args.json_output,
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        return _print_tier1_execution_failure(
            exc.reason_code,
            claimed=claim_exists,
            json_output=args.json_output,
        )
    except (
        AV1ValidationDerivationError,
        AV1ValidationV3Error,
        AV1ValidationV3QualificationError,
        AV1ValidationV3Tier1ConfigSnapshotError,
        AV1ValidationV3Tier1CoverageError,
        AV1ValidationV3Tier1DiagnosticsError,
        AV1ValidationV3Tier1ExecutorError,
        AV1ValidationV3Tier1GrantError,
        AV1ValidationV3Tier1OperationError,
        AV1ValidationV3Tier1RequestError,
        MediaforceRuntimeBusyError,
        MediaforceRuntimeLockOwnershipError,
        TypeError,
        ValueError,
    ):
        return _print_tier1_execution_failure(
            (
                "execution_already_claimed"
                if claim_exists and not claim_created
                else "execution_failed"
                if claim_created
                else "input_invalid"
            ),
            claimed=claim_exists,
            json_output=args.json_output,
        )
    _assert_preregistration_bootstrap_authority()
    payload = {
        **operation.to_public_summary(),
        **receipt.to_summary(),
        "claim_id": claim.claim_id,
        "grant_id": grant.grant_id,
        "single_execution_claimed": True,
        "retry_authorized": False,
    }
    _print_partition_payload(payload, json_output=args.json_output)
    return 0 if operation.passed else 2


def _run_publish_tier1_compat_request(args: argparse.Namespace) -> int:
    _assert_preregistration_bootstrap_authority()
    _assert_canonical_preregistration_runner()
    try:
        (
            protocol,
            plan,
            _,
            _,
            _,
        ) = _load_tier1_compat_base_inputs(args, as_of=args.requested_at)
        request = build_av1_validation_v3_tier1_compat_request(
            protocol=protocol,
            plan=plan,
            probe_matrix_sha256=AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SHA256,
            requested_at=args.requested_at,
            valid_until=args.request_valid_until,
        )
        result = publish_av1_validation_v3_tier1_compat_request(
            request=request,
            output_root=args.output_root,
            repository_root=REPOSITORY_ROOT,
        )
    except OSError:
        return _print_tier1_compat_failure(
            "input_unreadable",
            claimed=False,
            json_output=args.json_output,
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        return _print_tier1_compat_failure(
            exc.reason_code,
            claimed=False,
            json_output=args.json_output,
        )
    except (
        AV1ValidationDerivationError,
        AV1ValidationV3Error,
        AV1ValidationV3QualificationError,
        AV1ValidationV3Tier1CompatAuthorizationError,
        AV1ValidationV3Tier1CompatOperationError,
        AV1ValidationV3Tier1CompatProbeError,
        AV1ValidationV3Tier1ConfigSnapshotError,
        AV1ValidationV3Tier1RuntimeError,
        TypeError,
        ValueError,
    ):
        return _print_tier1_compat_failure(
            "input_invalid",
            claimed=False,
            json_output=args.json_output,
        )
    _assert_preregistration_bootstrap_authority()
    _print_partition_payload(
        result.to_summary(artifact_kind="compat_request"),
        json_output=args.json_output,
    )
    return 0


def _run_authorize_tier1_compat_probe(args: argparse.Namespace) -> int:
    _assert_preregistration_bootstrap_authority()
    _assert_canonical_preregistration_runner()
    try:
        (
            protocol,
            plan,
            request,
            _,
            _,
            _,
        ) = _load_tier1_compat_execution_inputs(args, as_of=args.authorized_at)
        grant = build_av1_validation_v3_tier1_compat_grant(
            protocol=protocol,
            plan=plan,
            request=request,
            owner_principal=args.owner_principal,
            authorized_at=args.authorized_at,
            valid_until=args.valid_until,
        )
        result = publish_av1_validation_v3_tier1_compat_grant(
            grant=grant,
            output_root=args.output_root,
            repository_root=REPOSITORY_ROOT,
        )
    except OSError:
        return _print_tier1_compat_failure(
            "input_unreadable",
            claimed=False,
            json_output=args.json_output,
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        return _print_tier1_compat_failure(
            exc.reason_code,
            claimed=False,
            json_output=args.json_output,
        )
    except (
        AV1ValidationDerivationError,
        AV1ValidationV3Error,
        AV1ValidationV3QualificationError,
        AV1ValidationV3Tier1CompatAuthorizationError,
        AV1ValidationV3Tier1CompatOperationError,
        AV1ValidationV3Tier1CompatProbeError,
        AV1ValidationV3Tier1ConfigSnapshotError,
        AV1ValidationV3Tier1RuntimeError,
        TypeError,
        ValueError,
    ):
        return _print_tier1_compat_failure(
            "input_invalid",
            claimed=False,
            json_output=args.json_output,
        )
    _assert_preregistration_bootstrap_authority()
    _print_partition_payload(
        result.to_summary(artifact_kind="compat_grant"),
        json_output=args.json_output,
    )
    return 0


def _run_tier1_compat_probe(args: argparse.Namespace) -> int:
    _assert_preregistration_bootstrap_authority()
    _assert_canonical_preregistration_runner()
    claim_created = False
    claim_exists = False
    claimed_at = _now_iso()
    try:
        (
            protocol,
            plan,
            request,
            config,
            config_snapshot_bytes,
            toolchain,
        ) = _load_tier1_compat_execution_inputs(args, as_of=claimed_at)
        matrix_payload = load_av1_validation_v3_tier1_compat_probe_matrix(
            args.probe_matrix
        )
        grant = load_published_av1_validation_v3_tier1_compat_grant(
            output_root=args.grant_root,
            repository_root=REPOSITORY_ROOT,
            request_id=request.request_id,
        )
        claim = build_av1_validation_v3_tier1_compat_claim(
            protocol=protocol,
            plan=plan,
            request=request,
            grant=grant,
            claimed_at=claimed_at,
        )

        def claim_execution() -> str:
            nonlocal claim_created, claim_exists
            try:
                claim_result = publish_av1_validation_v3_tier1_compat_claim(
                    claim=claim,
                    output_root=args.execution_output_root,
                    repository_root=REPOSITORY_ROOT,
                )
            except AV1ValidationV3Tier1PublicationError as exc:
                if exc.reason_code in {
                    "execution_already_claimed",
                    "publication_cleanup_failed",
                }:
                    claim_exists = True
                raise
            claim_exists = True
            claim_created = claim_result.created
            if not claim_result.created:
                raise AV1ValidationV3Tier1CompatOperationError(
                    "AV1 v3 Tier 1 compatibility-probe execution was already claimed"
                )
            return claim.claim_id

        operation = run_av1_validation_v3_tier1_compat_probe(
            config,
            protocol=protocol,
            plan=plan,
            request=request,
            grant=grant,
            claim=claim,
            config_snapshot_bytes=config_snapshot_bytes,
            matrix=matrix_payload,
            output_directory=args.variant_output_root,
            repository_root=REPOSITORY_ROOT,
            toolchain=toolchain,
            claim_execution=claim_execution,
        )
        result_publication = publish_av1_validation_v3_tier1_compat_result(
            result=operation,
            output_root=args.execution_output_root,
            repository_root=REPOSITORY_ROOT,
        )
    except OSError:
        return _print_tier1_compat_failure(
            "execution_failed" if claim_created else "input_unreadable",
            claimed=claim_exists or claim_created,
            json_output=args.json_output,
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        return _print_tier1_compat_failure(
            exc.reason_code,
            claimed=claim_exists,
            json_output=args.json_output,
        )
    except (
        AV1ValidationDerivationError,
        AV1ValidationV3Error,
        AV1ValidationV3QualificationError,
        AV1ValidationV3Tier1CompatAuthorizationError,
        AV1ValidationV3Tier1CompatOperationError,
        AV1ValidationV3Tier1CompatProbeError,
        AV1ValidationV3Tier1ConfigSnapshotError,
        AV1ValidationV3Tier1RuntimeError,
        MediaforceRuntimeBusyError,
        MediaforceRuntimeLockOwnershipError,
        TypeError,
        ValueError,
    ):
        return _print_tier1_compat_failure(
            (
                "execution_already_claimed"
                if claim_exists and not claim_created
                else "execution_failed"
                if claim_created
                else "input_invalid"
            ),
            claimed=claim_exists,
            json_output=args.json_output,
        )
    _assert_preregistration_bootstrap_authority()
    payload = {
        **operation.to_public_summary(),
        **result_publication.to_summary(artifact_kind="compat_result"),
        "claim_id": claim.claim_id,
        "grant_id": grant.grant_id,
        "single_execution_claimed": True,
        "retry_authorized": False,
    }
    _print_partition_payload(payload, json_output=args.json_output)
    return 0


def _load_tier1_compat_base_inputs(
    args: argparse.Namespace,
    *,
    as_of: str,
) -> tuple[
    AV1ValidationProtocolV3,
    AV1ValidationV3QualificationPlan,
    MediaforceConfig,
    bytes,
    AV1ValidationV3Tier1ToolchainBinding,
]:
    repository_commit, repository_tree = _live_repository_identity()
    protocol = load_av1_validation_protocol_v3(args.protocol)
    assert_preregistered_av1_validation_protocol_v3(protocol)
    plan = load_av1_validation_v3_qualification_plan(args.plan_artifact)
    config_snapshot_bytes = args.config_artifact.read_bytes()
    validate_av1_validation_v3_tier1_config_snapshot(config_snapshot_bytes)
    config = load_config(args.config)
    assert_av1_validation_v3_tier1_config_snapshot_matches(
        config,
        config_snapshot_bytes,
    )
    toolchain = build_av1_validation_v3_tier1_toolchain_binding(
        ffmpeg_path=args.ffmpeg,
        ffprobe_path=args.ffprobe,
    )
    load_av1_validation_v3_tier1_compat_probe_matrix(args.probe_matrix)
    if (
        plan.repository_commit != repository_commit
        or plan.repository_tree != repository_tree
        or plan.config_sha256
        != av1_validation_v3_tier1_config_snapshot_sha256(config_snapshot_bytes)
        or plan.toolchain_sha256 != toolchain.payload_sha256
    ):
        raise AV1ValidationV3Tier1CompatOperationError(
            "AV1 v3 Tier 1 compatibility-probe inputs drifted after preparation"
        )
    assert_av1_validation_v3_qualification_plan_active(
        protocol,
        plan,
        as_of=as_of,
    )
    return protocol, plan, config, config_snapshot_bytes, toolchain


def _load_tier1_compat_execution_inputs(
    args: argparse.Namespace,
    *,
    as_of: str,
) -> tuple[
    AV1ValidationProtocolV3,
    AV1ValidationV3QualificationPlan,
    AV1ValidationV3Tier1CompatRequest,
    MediaforceConfig,
    bytes,
    AV1ValidationV3Tier1ToolchainBinding,
]:
    protocol, plan, config, config_snapshot_bytes, toolchain = (
        _load_tier1_compat_base_inputs(args, as_of=as_of)
    )
    request = load_av1_validation_v3_tier1_compat_request(args.request_artifact)
    if (
        request.repository_commit != plan.repository_commit
        or request.repository_tree != plan.repository_tree
        or request.config_sha256 != plan.config_sha256
        or request.toolchain_sha256 != plan.toolchain_sha256
        or request.probe_matrix_sha256
        != AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SHA256
    ):
        raise AV1ValidationV3Tier1CompatOperationError(
            "AV1 v3 Tier 1 compatibility-probe request inputs drifted after publication"
        )
    assert_av1_validation_v3_tier1_compat_request_active(
        protocol,
        plan,
        request,
        as_of=as_of,
    )
    return (
        protocol,
        plan,
        request,
        config,
        config_snapshot_bytes,
        toolchain,
    )


def _load_tier1_execution_inputs(
    args: argparse.Namespace,
    *,
    as_of: str,
) -> tuple[
    AV1ValidationProtocolV3,
    AV1ValidationV3QualificationPlan,
    AV1ValidationV3Tier1AuthorizationRequest,
    MediaforceConfig,
    bytes,
    AV1ValidationV3Tier1ToolchainBinding,
]:
    repository_commit, repository_tree = _live_repository_identity()
    protocol = load_av1_validation_protocol_v3(args.protocol)
    assert_preregistered_av1_validation_protocol_v3(protocol)
    plan = load_av1_validation_v3_qualification_plan(args.plan_artifact)
    request = load_av1_validation_v3_tier1_authorization_request(
        args.request_artifact
    )
    config_snapshot_bytes = args.config_artifact.read_bytes()
    validate_av1_validation_v3_tier1_config_snapshot(config_snapshot_bytes)
    config = load_config(args.config)
    assert_av1_validation_v3_tier1_config_snapshot_matches(
        config,
        config_snapshot_bytes,
    )
    toolchain = build_av1_validation_v3_tier1_toolchain_binding(
        ffmpeg_path=args.ffmpeg,
        ffprobe_path=args.ffprobe,
    )
    if (
        plan.repository_commit != repository_commit
        or plan.repository_tree != repository_tree
        or request.repository_commit != repository_commit
        or request.repository_tree != repository_tree
        or plan.config_sha256
        != av1_validation_v3_tier1_config_snapshot_sha256(
            config_snapshot_bytes
        )
        or request.config_sha256 != plan.config_sha256
        or plan.toolchain_sha256 != toolchain.payload_sha256
        or request.toolchain_sha256 != toolchain.payload_sha256
    ):
        raise AV1ValidationV3Tier1OperationError(
            "AV1 v3 Tier 1 execution inputs drifted after preparation"
        )
    assert_av1_validation_v3_qualification_plan_active(
        protocol,
        plan,
        as_of=as_of,
    )
    assert_av1_validation_v3_tier1_request_active(
        protocol,
        plan,
        request,
        as_of=as_of,
    )
    return (
        protocol,
        plan,
        request,
        config,
        config_snapshot_bytes,
        toolchain,
    )


def _run_write_tier1_config_snapshot(args: argparse.Namespace) -> int:
    _assert_preregistration_bootstrap_authority()
    _assert_canonical_preregistration_runner()
    try:
        config = load_config(args.config)
        snapshot_bytes = build_av1_validation_v3_tier1_config_snapshot(config)
        result = publish_av1_validation_v3_tier1_config_snapshot(
            snapshot_bytes=snapshot_bytes,
            output_root=args.output_root,
            repository_root=REPOSITORY_ROOT,
        )
    except OSError:
        return _print_tier1_publication_failure(
            "input_unreadable",
            json_output=args.json_output,
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        return _print_tier1_publication_failure(
            exc.reason_code,
            json_output=args.json_output,
        )
    except (AV1ValidationV3Tier1ConfigSnapshotError, TypeError, ValueError):
        return _print_tier1_publication_failure(
            "input_invalid",
            json_output=args.json_output,
        )
    _assert_preregistration_bootstrap_authority()
    _print_partition_payload(result.to_summary(), json_output=args.json_output)
    return 0


def _run_create_qualification_key(args: argparse.Namespace) -> int:
    _assert_preregistration_bootstrap_authority()
    _assert_canonical_preregistration_runner()
    try:
        result = ensure_av1_validation_v3_qualification_key(
            output_root=args.output_root,
            repository_root=REPOSITORY_ROOT,
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        return _print_tier1_publication_failure(
            exc.reason_code,
            json_output=args.json_output,
        )
    except (OSError, TypeError, ValueError):
        return _print_tier1_publication_failure(
            "input_invalid",
            json_output=args.json_output,
        )
    _assert_preregistration_bootstrap_authority()
    _print_partition_payload(result.to_summary(), json_output=args.json_output)
    return 0


def _print_tier1_publication_failure(
    reason_code: str,
    *,
    json_output: bool,
) -> int:
    payload = {
        "published": False,
        "failure_reason": reason_code,
        "gate": "A0",
        "tier": "tier1",
        "private_inventory_read_authorized": False,
        "media_read_authorized": False,
        "tier2_execution_authorized": False,
        "runtime_execution_authorized": False,
        "qualification_execution_authorized": False,
        "key_creation_authorized": False,
        "evidence_creation_authorized": False,
        "evidence_eligible": False,
        "empirical_authority_conferred": False,
        "derivation_authorized": False,
        "holdout_authorized": False,
        "publication_authorized": False,
        "public_bundle_activation_allowed": False,
        "activation_authorized": False,
        "execution_requires_separate_owner_authorization": True,
    }
    assert_av1_cold_start_public_payload_safe(payload)
    _assert_preregistration_bootstrap_authority()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"AV1 v3 Tier 1 preparation publication failed: {reason_code}",
            file=sys.stderr,
        )
    return 1


def _print_tier1_execution_failure(
    reason_code: str,
    *,
    claimed: bool,
    json_output: bool,
) -> int:
    payload = {
        "executed": False,
        "failure_reason": reason_code,
        "gate": "A0",
        "tier": "tier1",
        "single_execution_claimed": claimed,
        "retry_authorized": False,
        "private_inventory_read_authorized": False,
        "media_read_authorized": False,
        "tier2_execution_authorized": False,
        "evidence_creation_authorized": False,
        "evidence_eligible": False,
        "empirical_authority_conferred": False,
        "derivation_authorized": False,
        "holdout_authorized": False,
        "publication_authorized": False,
        "activation_authorized": False,
    }
    assert_av1_cold_start_public_payload_safe(payload)
    _assert_preregistration_bootstrap_authority()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"AV1 v3 Tier 1 synthetic execution failed: {reason_code}",
            file=sys.stderr,
        )
    return 1


def _print_tier1_compat_failure(
    reason_code: str,
    *,
    claimed: bool,
    json_output: bool,
) -> int:
    payload = {
        "executed": False,
        "published": False,
        "failure_reason": reason_code,
        "gate": "A0",
        "tier": "none",
        "diagnostic_only": True,
        "single_execution_claimed": claimed,
        "retry_authorized": False,
        "compat_probe_execution_authorized": False,
        "private_inventory_read_authorized": False,
        "media_read_authorized": False,
        "tier1_execution_authorized": False,
        "tier2_execution_authorized": False,
        "runtime_execution_authorized": False,
        "qualification_execution_authorized": False,
        "evidence_creation_authorized": False,
        "evidence_eligible": False,
        "empirical_authority_conferred": False,
        "derivation_authorized": False,
        "holdout_authorized": False,
        "publication_authorized": False,
        "activation_authorized": False,
    }
    assert_av1_cold_start_public_payload_safe(payload)
    _assert_preregistration_bootstrap_authority()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"AV1 v3 Tier 1 compatibility probe failed: {reason_code}",
            file=sys.stderr,
        )
    return 1


def _run_partition_action(args: argparse.Namespace) -> int:
    _assert_preregistration_bootstrap_authority()
    manifest = load_av1_validation_manifest_v2(args.manifest)
    assert_preregistered_av1_validation_manifest_v2(manifest)
    assert_private_artifact_path(args.eligibility, repository_root=REPOSITORY_ROOT)
    assert_private_artifact_path(args.key, repository_root=REPOSITORY_ROOT)
    eligibility = load_av1_validation_v2_eligibility(args.eligibility)
    assert_preregistered_av1_validation_v2_eligibility(eligibility)
    token_key = load_av1_validation_partition_key(args.key)
    if args.action == "build-partition":
        assert_private_artifact_path(args.output, repository_root=REPOSITORY_ROOT)
        config = load_config(args.config)
        try:
            _assert_preregistration_bootstrap_authority()
            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={"purpose": "av1-partition-build"},
            ):
                _assert_preregistration_bootstrap_authority()
                migrate_config_state(config)
                _assert_preregistration_bootstrap_authority()
                reserve_mediaforce_database_identity(config)
                _assert_preregistration_bootstrap_authority()
                with open_readonly_db(config.paths.db_path) as connection:
                    inventory = load_av1_validation_partition_inventory(
                        connection,
                        config=config,
                    )
                    with av1_validation_partition_source_sha256_resolver(
                        connection,
                        config=config,
                        verify_evidence=True,
                    ) as source_sha256_resolver:
                        partition = build_av1_validation_private_partition(
                            manifest=manifest,
                            eligibility_attestation_id=eligibility.attestation_id,
                            eligibility_payload_sha256=eligibility.payload_sha256,
                            sources=inventory.sources,
                            expectations=inventory.expectations,
                            token_key=token_key,
                            expected_token_key_id=args.expected_token_key_id,
                            selected_at=args.selected_at,
                        )
                        build_av1_validation_derivation_source_commitments(
                            partition=partition,
                            assignments=tuple(
                                assignment
                                for assignment in partition.assignments
                                if assignment.role == "derivation"
                            ),
                            resolver=source_sha256_resolver,
                        )
                        source_sha256_resolver.verify()
                        def before_partition_publish() -> None:
                            _call_preregistration_publication_guard(
                                source_sha256_resolver.assert_quiet
                            )

                        write_av1_validation_private_partition(
                            args.output,
                            partition,
                            before_publish=before_partition_publish,
                        )
        except MediaforceRuntimeBusyError as exc:
            raise AV1ValidationPartitionError(
                "AV1 partition build requires the Mediaforce runtime to be paused"
            ) from exc
    else:
        assert_private_artifact_path(args.partition, repository_root=REPOSITORY_ROOT)
        partition = load_av1_validation_private_partition(args.partition)
        if av1_validation_partition_key_id(token_key) != partition.token_key_id:
            raise AV1ValidationPartitionError(
                "AV1 partition key does not match the frozen token-key ID"
            )
        validate_av1_validation_private_partition(
            partition,
            manifest=manifest,
            token_key=token_key,
        )
        config = load_config(args.config)
        with open_readonly_db(config.paths.db_path) as connection:
            inventory = load_av1_validation_partition_inventory(
                connection,
                config=config,
            )
            validate_av1_validation_partition_current_inputs(
                partition,
                manifest=manifest,
                sources=inventory.sources,
                expectations=inventory.expectations,
                token_key=token_key,
            )
    _print_partition_payload(
        av1_validation_partition_public_summary(partition),
        json_output=args.json_output,
    )
    return 0


def _run_derivation_action(
        args: argparse.Namespace,
) -> int:
    _assert_preregistration_bootstrap_authority()
    _assert_canonical_preregistration_runner()
    direct_write_action = args.action in {
        "create-derivation-plan",
        "build-derivation-proposal",
        "record-derivation-review",
    }
    if direct_write_action:
        config = load_config(args.config)
        try:
            _assert_preregistration_bootstrap_authority()
            with exclusive_mediaforce_runtime_lock(
                config,
                owner_payload={
                    "purpose": "av1-derivation-tooling",
                    "action": str(args.action),
                },
            ):
                _assert_preregistration_bootstrap_authority()
                migrate_config_state(config)
                _assert_preregistration_bootstrap_authority()
                reserve_mediaforce_database_identity(config)
                _assert_preregistration_bootstrap_authority()
                return _run_derivation_action_body(
                    args,
                    locked_config=config,
                )
        except MediaforceRuntimeBusyError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation tooling requires the Mediaforce runtime to be paused"
            ) from exc
    return _run_derivation_action_body(args, locked_config=None)


def _run_derivation_action_body(
        args: argparse.Namespace,
        *,
        locked_config: MediaforceConfig | None,
) -> int:
    _assert_preregistration_bootstrap_authority()
    direct_write_action = args.action in {
        "create-derivation-plan",
        "build-derivation-proposal",
        "record-derivation-review",
    }
    if direct_write_action and locked_config is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation tooling lost its locked runtime context"
        )
    if args.action in {"create-derivation-plan", "validate-derivation-plan"}:
        return _run_derivation_plan_action(
            args,
            config=locked_config or load_config(args.config),
        )

    if args.action == "run-derivation-assignment":
        for path in (args.partition, args.plan, args.key):
            assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
        manifest = load_av1_validation_manifest_v2(args.manifest)
        partition = load_av1_validation_private_partition(args.partition)
        plan, artifact_root = _load_recovery_capable_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
        )
        token_key = load_av1_validation_partition_key(args.key)
        process_controller = ManagedProcessController()

        attempt = run_av1_validation_derivation_assignment(
            config_path=args.config,
            manifest=manifest,
            partition=partition,
            token_key=token_key,
            plan=plan,
            assignment_id=args.assignment_id,
            attempts_directory=artifact_root / "attempts",
            terminal_records_directory=artifact_root / "terminal-records",
            repository_identity_resolver=_live_repository_identity,
            process_controller=process_controller,
        )
        _print_partition_payload(
            {
                "attempt_id": attempt.attempt_id,
                "attempt_sha256": attempt.payload_sha256,
                "status": attempt.status,
                "reason_code": attempt.reason_code,
                "review_required": attempt.status == "review_pending",
                "holdout_execution_authorized": False,
                "public_bundle_activation_allowed": False,
            },
            json_output=args.json_output,
        )
        return 0 if attempt.status == "review_pending" else 2

    if args.action == "derivation-status":
        plan, artifact_root = _load_canonical_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
        )
        attempts_directory = artifact_root / "attempts"
        records_directory = artifact_root / "terminal-records"
        attempts = (
            load_av1_validation_derivation_attempts(
                attempts_directory,
                review_pending_published_before=plan.authorization.valid_until,
                allow_unaccepted_review_pending=True,
            )
            if attempts_directory.exists()
            else ()
        )
        records = (
            load_av1_validation_derivation_terminal_records(
                records_directory,
                observed_published_before=plan.authorization.valid_until,
            )
            if records_directory.exists()
            else ()
        )
        assignment_claims = load_av1_validation_derivation_assignment_claims(
            attempts_directory,
            plan=plan,
        )
        terminal_intents_directory = artifact_root / "terminal-intents"
        terminal_intents = (
            load_av1_validation_derivation_terminal_intents(
                terminal_intents_directory,
                observed_published_before=plan.authorization.valid_until,
                allow_late_observed=True,
            )
            if terminal_intents_directory.exists()
            else ()
        )
        verdict_claims = load_av1_validation_derivation_verdict_claims(
            artifact_root / "verdict-claims",
            plan=plan,
            attempts=attempts,
        )
        attempts_by_assignment = {
            attempt.assignment_id: attempt
            for attempt in attempts
        }
        attempt_assignment_ids = set(attempts_by_assignment)
        terminal_assignment_ids = {
            record.assignment_id
            for record in records
        }
        unaccepted_attempt_count = sum(
            av1_validation_derivation_attempt_recovery_action(
                attempt,
                publication_state,
                terminal_intents=terminal_intents,
                terminal_records=records,
            )
            != "none"
            for attempt in attempts
            if attempt.status == "review_pending"
            for publication_state in (
                load_av1_validation_derivation_attempt_publication_state(
                    attempts_directory,
                    attempt,
                    published_before=plan.authorization.valid_until,
                ),
            )
        )
        unresolved_assignment_claim_count = sum(
            str(claim["assignment_id"]) not in attempt_assignment_ids
            for claim in assignment_claims
        )
        unresolved_terminal_intent_count = sum(
            intent.assignment_id not in terminal_assignment_ids
            for intent in terminal_intents
        )
        late_observed_terminal_intent_count = sum(
            av1_validation_derivation_terminal_intent_published_after(
                terminal_intents_directory,
                intent,
                published_before=plan.authorization.valid_until,
            )
            for intent in terminal_intents
        )
        unresolved_verdict_claim_count = 0
        unresolved_verdict_intent_count = 0
        for claim in verdict_claims:
            assignment_id = str(claim["assignment_id"])
            if assignment_id in terminal_assignment_ids:
                continue
            attempt = attempts_by_assignment[assignment_id]
            verdict_intent = load_av1_validation_derivation_verdict_intent(
                artifact_root / "verdict-intents",
                plan=plan,
                attempt=attempt,
            )
            if verdict_intent is None:
                unresolved_verdict_claim_count += 1
            else:
                unresolved_verdict_intent_count += 1
        review_claim_count, unresolved_review_claim_count = (
            _derivation_review_recovery_counts(
                artifact_root=artifact_root,
                plan=plan,
            )
        )
        recovery_required = any((
            unaccepted_attempt_count,
            unresolved_assignment_claim_count,
            unresolved_terminal_intent_count,
            late_observed_terminal_intent_count,
            unresolved_verdict_claim_count,
            unresolved_verdict_intent_count,
            unresolved_review_claim_count,
        ))
        attempt_counts = {
            status: sum(attempt.status == status for attempt in attempts)
            for status in ("review_pending", "failed", "excluded", "stopped")
        }
        record_counts = {
            status: sum(record.status == status for record in records)
            for status in ("observed", "failed", "excluded", "stopped")
        }
        attempt_reason_code_counts = {
            reason_code: count
            for reason_code in sorted(AV1_VALIDATION_DERIVATION_REASON_CODES)
            if (
                count := sum(
                    attempt.reason_code == reason_code
                    for attempt in attempts
                )
            )
        }
        terminal_reason_code_counts = {
            reason_code: count
            for reason_code in sorted(AV1_VALIDATION_DERIVATION_REASON_CODES)
            if (
                count := sum(
                    record.reason_code == reason_code
                    for record in records
                )
            )
        }
        _print_partition_payload(
            {
                "plan_id": plan.plan_id,
                "registered_assignment_count": len(plan.assignments),
                "attempt_count": len(attempts),
                "terminal_record_count": len(records),
                "assignment_claim_count": len(assignment_claims),
                "terminal_intent_count": len(terminal_intents),
                "verdict_claim_count": len(verdict_claims),
                "review_claim_count": review_claim_count,
                "unaccepted_attempt_count": unaccepted_attempt_count,
                "unresolved_assignment_claim_count": (
                    unresolved_assignment_claim_count
                ),
                "unresolved_terminal_intent_count": (
                    unresolved_terminal_intent_count
                ),
                "late_observed_terminal_intent_count": (
                    late_observed_terminal_intent_count
                ),
                "unresolved_verdict_claim_count": (
                    unresolved_verdict_claim_count
                ),
                "unresolved_verdict_intent_count": (
                    unresolved_verdict_intent_count
                ),
                "unresolved_review_claim_count": (
                    unresolved_review_claim_count
                ),
                "recovery_required": recovery_required,
                **{f"attempt_{key}_count": value for key, value in attempt_counts.items()},
                **{f"terminal_{key}_count": value for key, value in record_counts.items()},
                "attempt_reason_code_counts": attempt_reason_code_counts,
                "terminal_reason_code_counts": terminal_reason_code_counts,
                "holdout_execution_authorized": False,
                "public_bundle_activation_allowed": False,
            },
            json_output=args.json_output,
        )
        return 2 if recovery_required else 0

    if args.action == "record-derivation-verdict":
        for path in (args.partition, args.plan, args.key):
            assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
        manifest = load_av1_validation_manifest_v2(args.manifest)
        assert_preregistered_av1_validation_manifest_v2(manifest)
        partition = load_av1_validation_private_partition(args.partition)
        token_key = load_av1_validation_partition_key(args.key)
        plan, artifact_root = _load_canonical_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
        )
        attempts = load_av1_validation_derivation_attempts(
            artifact_root / "attempts",
            review_pending_published_before=plan.authorization.valid_until,
        )
        attempt = next(
            (item for item in attempts if item.assignment_id == args.assignment_id),
            None,
        )
        if attempt is None:
            raise AV1ValidationDerivationError(
                "AV1 derivation assignment has no immutable attempt"
            )
        terminal = record_av1_validation_derivation_visual_verdict(
            config_path=args.config,
            manifest=manifest,
            plan=plan,
            partition=partition,
            token_key=token_key,
            attempt=attempt,
            terminal_records_directory=artifact_root / "terminal-records",
            verdict=args.verdict,
            concern_tags=args.concern_tag,
            evidence_ids=args.evidence_id,
            moment_indexes=args.moment_index,
            repository_identity_resolver=_live_repository_identity,
        )
        _print_partition_payload(
            {
                "terminal_record_sha256": terminal.payload_sha256,
                "status": terminal.status,
                "holdout_execution_authorized": False,
                "public_bundle_activation_allowed": False,
            },
            json_output=args.json_output,
        )
        return 0 if terminal.status == "observed" else 2

    if args.action == "build-derivation-proposal":
        return _run_derivation_proposal_action(
            args,
            config=cast(MediaforceConfig, locked_config),
            repository_identity_resolver=_live_repository_identity,
        )

    if args.action == "record-derivation-review":
        plan, artifact_root = _load_canonical_derivation_plan(
            plan_path=args.plan,
            config_path=args.config,
            config=locked_config,
        )
        proposal = load_av1_validation_derivation_candidate_proposal(
            artifact_root,
            plan=plan,
            cell_plan_id=args.cell_plan_id,
        )
        with retain_av1_validation_derivation_publication_directories((
            (
                artifact_root / "review-claims" / proposal.proposal_id,
                "review_claims",
                proposal.proposal_id,
                proposal.payload_sha256,
            ),
            (
                artifact_root / "reviews" / proposal.proposal_id,
                "reviews",
                proposal.proposal_id,
                proposal.payload_sha256,
            ),
            (
                artifact_root / "review-responses" / proposal.proposal_id,
                "review_responses",
                proposal.proposal_id,
                proposal.payload_sha256,
            ),
        )) as publication_guard:
            def guarded_publication() -> None:
                _call_preregistration_publication_guard(publication_guard)

            existing_review = _load_existing_derivation_review(
                artifact_root=artifact_root,
                plan=plan,
                proposal=proposal,
                lane=args.lane,
            )
            checkpoint_recovery = False
            if existing_review is not None and existing_review[1] is None:
                checkpoint_recovery = (
                    load_av1_validation_derivation_review_response_checkpoint(
                        artifact_root,
                        plan=plan,
                        proposal=proposal,
                        claim=existing_review[0],
                    )
                    is not None
                )
            requires_live_review_environment = (
                existing_review is None
                or (
                    existing_review[1] is None
                    and not checkpoint_recovery
                )
            )
            if requires_live_review_environment:
                assert_av1_validation_derivation_execution_environment(plan)
            repository_commit, repository_tree = _repository_review_identity(
                process_controller=ManagedProcessController(),
            )
            assert_av1_validation_derivation_repository_identity(
                plan,
                repository_commit=repository_commit,
                repository_tree=repository_tree,
            )
            if existing_review is None or existing_review[1] is None:
                existing_claim = (
                    existing_review[0]
                    if existing_review is not None
                    else None
                )
                (
                    claim,
                    review_evidence,
                    decision,
                    reviewed_at,
                ) = _run_code_llm_review(
                    artifact_root=artifact_root,
                    plan=plan,
                    proposal=proposal,
                    lane=args.lane,
                    claim=existing_claim,
                    before_publish=guarded_publication,
                )
                review_evidence_sha256 = (
                    f"sha256:{hashlib.sha256(review_evidence).hexdigest()}"
                )
                review = build_av1_validation_derivation_review_attestation(
                    proposal=proposal,
                    claim=claim,
                    review_evidence_sha256=review_evidence_sha256,
                    decision=decision,
                    reviewed_at=reviewed_at,
                )
                envelope = build_av1_validation_derivation_review_envelope(
                    review=review,
                    evidence=review_evidence,
                )
            else:
                claim, envelope = existing_review
                assert envelope is not None
                review = envelope.review
            if requires_live_review_environment:
                assert_av1_validation_derivation_execution_environment(plan)
            write_av1_validation_derivation_review_envelope(
                artifact_root,
                plan=plan,
                proposal=proposal,
                claim=claim,
                envelope=envelope,
                before_publish=guarded_publication,
            )
            guarded_publication()
        _print_partition_payload(
            {
                "proposal_id": proposal.proposal_id,
                "review_lane": review.lane,
                "decision": review.decision,
                "review_sha256": review.payload_sha256,
                "candidate_lock_created": False,
                "holdout_execution_authorized": False,
            },
            json_output=args.json_output,
        )
        return 0 if review.decision == "approved" else 2

    if args.action == "finalize-derivation-lock":
        for path in (
            args.plan,
            args.partition,
            args.key,
        ):
            assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
        manifest = load_av1_validation_manifest_v2(args.manifest)
        assert_preregistered_av1_validation_manifest_v2(manifest)
        partition = load_av1_validation_private_partition(args.partition)
        token_key = load_av1_validation_partition_key(args.key)
        validate_av1_validation_private_partition(
            partition,
            manifest=manifest,
            token_key=token_key,
        )
        lock_envelope = finalize_av1_validation_derivation_candidate_lock(
            config_path=args.config,
            manifest=manifest,
            partition=partition,
            token_key=token_key,
            plan_path=args.plan,
            cell_plan_id=args.cell_plan_id,
            repository_identity_resolver=_live_repository_identity,
            now_iso=_now_iso,
        )
        candidate_lock = lock_envelope.candidate_lock
        _print_partition_payload(
            {
                "candidate_lock_id": candidate_lock.candidate_lock_id,
                "candidate_lock_sha256": candidate_lock.payload_sha256,
                "candidate_lock_envelope_sha256": lock_envelope.payload_sha256,
                "candidate_lock_reviewed": True,
                "holdout_execution_authorized": False,
                "public_bundle_activation_allowed": False,
            },
            json_output=args.json_output,
        )
        return 0

    raise AV1ValidationDerivationError("AV1 derivation action is unsupported")


def _run_derivation_plan_action(
        args: argparse.Namespace,
        *,
        config: MediaforceConfig,
) -> int:
    with _load_current_derivation_inputs(
        args,
        config=config,
    ) as (
        manifest,
        partition,
        _token_key,
        source_sha256_session,
        source_commitments,
    ):
        runtime_context_sha256 = (
            av1_validation_derivation_runtime_context_sha256(config)
        )
        quality_metrics = {
            assignment.quality_metric
            for assignment in partition.assignments
            if assignment.role == "derivation"
        }
        if len(quality_metrics) != 1:
            raise AV1ValidationDerivationError(
                "AV1 derivation partition quality metric is not uniform"
            )
        execution_environment_sha256 = (
            av1_validation_derivation_execution_environment_sha256(
                quality_metric=next(iter(quality_metrics)),
            )
        )
        statistics_contract_sha256 = (
            av1_validation_derivation_statistics_contract_sha256(manifest)
        )
        (
            _review_runner_path,
            review_runner_canonical_path_sha256,
            review_runner_binary_sha256,
        ) = _review_runner_identity()
        repository_process_controller = ManagedProcessController()
        repository_commit, repository_tree = _repository_review_identity(
            process_controller=repository_process_controller,
        )
        if args.action == "create-derivation-plan":
            artifact_root = (
                config.paths.web_state_dir
                / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
                / partition.partition_id
            ).expanduser().resolve()
            assert_private_artifact_path(
                artifact_root,
                repository_root=REPOSITORY_ROOT,
            )
            existing_plan_path = artifact_root / "plan.json"
            if existing_plan_path.exists() or existing_plan_path.is_symlink():
                plan = load_av1_validation_derivation_plan(existing_plan_path)
                if plan.authorization.valid_until != args.valid_until:
                    raise AV1ValidationDerivationError(
                        "AV1 derivation plan retry changed the authorization window"
                    )
            else:
                root_binding_path = artifact_root / ".binding"
                if root_binding_path.exists() or root_binding_path.is_symlink():
                    raise AV1ValidationDerivationError(
                        "AV1 derivation root binding exists without its immutable plan"
                    )
                authorization = build_av1_validation_v2_derivation_authorization(
                    manifest=manifest,
                    selection_lock_sha256=partition.selection_lock_sha256,
                    derivation_partition_sha256=partition.derivation_partition_sha256,
                    authorized_at=_now_iso(),
                    valid_until=args.valid_until,
                )
                plan = build_av1_validation_derivation_plan(
                    manifest=manifest,
                    partition=partition,
                    authorization=authorization,
                    runtime_context_sha256=runtime_context_sha256,
                    execution_environment_sha256=execution_environment_sha256,
                    statistics_contract_sha256=statistics_contract_sha256,
                    review_runner_canonical_path_sha256=(
                        review_runner_canonical_path_sha256
                    ),
                    review_runner_binary_sha256=review_runner_binary_sha256,
                    repository_commit=repository_commit,
                    repository_tree=repository_tree,
                    source_commitments=source_commitments,
                )
                artifact_root = _derivation_artifact_root_for_plan(
                    config=config,
                    plan=plan,
                )
        else:
            plan, _artifact_root = _load_canonical_derivation_plan(
                plan_path=args.plan,
                config_path=args.config,
                config=config,
            )
        if (
            plan.execution_environment_sha256
            != execution_environment_sha256
            or plan.statistics_contract_sha256
            != statistics_contract_sha256
            or plan.review_runner_canonical_path_sha256
            != review_runner_canonical_path_sha256
            or plan.review_runner_binary_sha256
            != review_runner_binary_sha256
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation plan execution contract drifted"
            )
        assert_av1_validation_derivation_repository_identity(
            plan,
            repository_commit=repository_commit,
            repository_tree=repository_tree,
        )
        rebuilt = build_av1_validation_derivation_plan(
            manifest=manifest,
            partition=partition,
            authorization=plan.authorization,
            runtime_context_sha256=runtime_context_sha256,
            execution_environment_sha256=execution_environment_sha256,
            statistics_contract_sha256=statistics_contract_sha256,
            review_runner_canonical_path_sha256=(
                review_runner_canonical_path_sha256
            ),
            review_runner_binary_sha256=review_runner_binary_sha256,
            repository_commit=repository_commit,
            repository_tree=repository_tree,
            source_commitments=source_commitments,
        )
        if rebuilt != plan:
            raise AV1ValidationDerivationError(
                "AV1 derivation plan does not match current locked inputs"
            )
        if args.action == "create-derivation-plan":
            def _assert_plan_repository_identity() -> None:
                current_identity = _repository_review_identity(
                    process_controller=repository_process_controller,
                )
                if current_identity != (repository_commit, repository_tree):
                    raise AV1ValidationDerivationError(
                        "AV1 derivation plan repository identity changed before publication"
                    )

            def _before_plan_publish() -> None:
                _assert_preregistration_bootstrap_authority()
                assert_av1_validation_derivation_authorization_active(
                    plan,
                    at=_now_iso(),
                )
                _assert_plan_repository_identity()
                source_sha256_session.assert_quiet()
                assert_av1_validation_derivation_source_snapshot_capacity(
                    artifact_root,
                    plan=plan,
                )
                source_sha256_session.assert_quiet()
                _assert_plan_repository_identity()
                assert_av1_validation_derivation_authorization_active(
                    plan,
                    at=_now_iso(),
                )
                _assert_preregistration_bootstrap_authority()

            def _after_plan_publish() -> None:
                _assert_preregistration_bootstrap_authority()
                source_sha256_session.assert_quiet()
                _assert_plan_repository_identity()
                _assert_preregistration_bootstrap_authority()

            write_av1_validation_derivation_plan(
                artifact_root,
                plan,
                before_publish=_before_plan_publish,
                after_publish=_after_plan_publish,
                before_bind=_before_plan_publish,
            )
        _print_partition_payload(
            av1_validation_derivation_plan_public_summary(plan),
            json_output=args.json_output,
        )
        return 0


def _run_derivation_proposal_action(
        args: argparse.Namespace,
        *,
        config: MediaforceConfig,
        repository_identity_resolver: Callable[[], tuple[str, str]],
) -> int:
    for path in (
        args.partition,
        args.key,
        args.plan,
    ):
        assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
    manifest = load_av1_validation_manifest_v2(args.manifest)
    assert_preregistered_av1_validation_manifest_v2(manifest)
    plan, artifact_root = _load_canonical_derivation_plan(
        plan_path=args.plan,
        config_path=args.config,
        config=config,
    )

    def assert_live_repository_identity() -> None:
        repository_commit, repository_tree = repository_identity_resolver()
        assert_av1_validation_derivation_repository_identity(
            plan,
            repository_commit=repository_commit,
            repository_tree=repository_tree,
        )

    assert_live_repository_identity()
    with _load_derivation_partition_for_evaluation(
        manifest=manifest,
        partition_path=args.partition,
        key_path=args.key,
        config_path=args.config,
        config=config,
        plan=plan,
    ) as (partition, source_sha256_session):
        assert_av1_validation_derivation_execution_environment(plan)
        attempts = load_av1_validation_derivation_attempts(
            artifact_root / "attempts",
            review_pending_published_before=plan.authorization.valid_until,
            require_durable=True,
        )
        records = load_av1_validation_derivation_terminal_records(
            artifact_root / "terminal-records",
            observed_published_before=plan.authorization.valid_until,
        )
        assert_av1_validation_derivation_observed_attempts_accepted(
            artifact_root,
            plan,
            attempts,
            records,
        )
        current_observations = load_current_av1_validation_derivation_observations(
            config_path=args.config,
            config=config,
            records=records,
        )
        proposal_path = (
            artifact_root / "proposals" / f"{args.cell_plan_id}.json"
        )
        proposal_exists = proposal_path.exists() or proposal_path.is_symlink()
        if proposal_exists:
            persisted_proposal = load_av1_validation_derivation_candidate_proposal(
                artifact_root,
                plan=plan,
                cell_plan_id=args.cell_plan_id,
            )
            proposed_at = persisted_proposal.proposed_at
        else:
            proposed_at = _now_iso()
        evaluation = evaluate_av1_validation_derivation_candidate(
            manifest=manifest,
            plan=plan,
            partition=partition,
            cell_plan_id=args.cell_plan_id,
            attempts=attempts,
            records=records,
            current_observations=current_observations,
            proposed_at=proposed_at,
        )
        summary = av1_validation_derivation_candidate_evaluation_public_summary(
            evaluation
        )
        _print_partition_payload(summary, json_output=args.json_output)
        if evaluation.proposal is None:
            return 2
        assert_av1_validation_derivation_execution_environment(plan)
        def _before_proposal_publish() -> None:
            _assert_preregistration_bootstrap_authority()
            source_sha256_session.assert_quiet()
            assert_av1_validation_derivation_authorization_active(
                plan,
                at=_now_iso(),
            )
            assert_live_repository_identity()
            _assert_preregistration_bootstrap_authority()

        write_av1_validation_derivation_candidate_proposal(
            artifact_root,
            plan=plan,
            proposal=evaluation.proposal,
            before_publish=(None if proposal_exists else _before_proposal_publish),
        )
        assert_live_repository_identity()
        return 0


@contextmanager
def _load_current_derivation_inputs(
        args: argparse.Namespace,
        *,
        config: MediaforceConfig | None = None,
) -> Iterator[tuple[
    AV1ValidationManifestV2,
    AV1ValidationPrivatePartition,
    bytes,
    AV1ValidationPartitionSourceSHA256Session,
    tuple[AV1ValidationDerivationSourceCommitment, ...],
]]:
    for path in (args.eligibility, args.partition, args.key):
        assert_private_artifact_path(path, repository_root=REPOSITORY_ROOT)
    manifest = load_av1_validation_manifest_v2(args.manifest)
    assert_preregistered_av1_validation_manifest_v2(manifest)
    eligibility = load_av1_validation_v2_eligibility(args.eligibility)
    assert_preregistered_av1_validation_v2_eligibility(eligibility)
    partition = load_av1_validation_private_partition(args.partition)
    token_key = load_av1_validation_partition_key(args.key)
    validate_av1_validation_private_partition(
        partition,
        manifest=manifest,
        token_key=token_key,
    )
    current_config = config or load_config(args.config)
    with open_readonly_db(current_config.paths.db_path) as connection:
        inventory = load_av1_validation_partition_inventory(
            connection,
            config=current_config,
        )
        with av1_validation_partition_source_sha256_resolver(
            connection,
            config=current_config,
        ) as source_sha256_resolver:
            validate_av1_validation_partition_current_inputs(
                partition,
                manifest=manifest,
                sources=inventory.sources,
                expectations=inventory.expectations,
                token_key=token_key,
            )
            source_commitments = (
                build_av1_validation_derivation_source_commitments(
                    partition=partition,
                    assignments=tuple(
                        assignment
                        for assignment in partition.assignments
                        if assignment.role == "derivation"
                    ),
                    resolver=source_sha256_resolver,
                )
            )
            source_sha256_resolver.verify()
            yield (
                manifest,
                partition,
                token_key,
                source_sha256_resolver,
                source_commitments,
            )


@contextmanager
def _load_derivation_partition_for_evaluation(
        *,
        manifest: AV1ValidationManifestV2,
        partition_path: Path,
        key_path: Path,
        config_path: Path,
        config: MediaforceConfig | None = None,
        plan: AV1ValidationDerivationPlan,
) -> Iterator[tuple[
    AV1ValidationPrivatePartition,
    AV1ValidationPartitionSourceSHA256Session,
]]:
    partition = load_av1_validation_private_partition(partition_path)
    token_key = load_av1_validation_partition_key(key_path)
    validate_av1_validation_private_partition(
        partition,
        manifest=manifest,
        token_key=token_key,
    )
    current_config = config or load_config(config_path)
    with open_readonly_db(current_config.paths.db_path) as connection:
        inventory = load_av1_validation_partition_inventory(
            connection,
            config=current_config,
        )
        with av1_validation_partition_source_sha256_resolver(
            connection,
            config=current_config,
        ) as source_sha256_resolver:
            validate_av1_validation_partition_current_inputs(
                partition,
                manifest=manifest,
                sources=inventory.sources,
                expectations=inventory.expectations,
                token_key=token_key,
            )
            assert_av1_validation_derivation_source_commitments(
                plan,
                resolver=source_sha256_resolver,
            )
            source_sha256_resolver.verify()
            yield partition, source_sha256_resolver


def _derivation_artifact_root_for_plan(
        *,
        config: MediaforceConfig,
        plan: AV1ValidationDerivationPlan,
) -> Path:
    root = av1_validation_derivation_artifact_root(config, plan)
    assert_private_artifact_path(root, repository_root=REPOSITORY_ROOT)
    return root


def _load_canonical_derivation_plan(
        *,
        plan_path: Path,
        config_path: Path,
        config: MediaforceConfig | None = None,
) -> tuple[AV1ValidationDerivationPlan, Path]:
    plan, bound_artifact_root = _load_bound_derivation_plan(plan_path)
    artifact_root = _derivation_artifact_root_for_plan(
        config=config or load_config(config_path),
        plan=plan,
    )
    if bound_artifact_root != artifact_root.expanduser().resolve():
        raise AV1ValidationDerivationError(
            "AV1 derivation plan must use the partition-global canonical private state root"
        )
    return plan, artifact_root


def _load_recovery_capable_derivation_plan(
        *,
        plan_path: Path,
        config_path: Path,
) -> tuple[AV1ValidationDerivationPlan, Path]:
    plan, artifact_root = _load_bound_derivation_plan(plan_path)
    config = load_config(config_path)
    expected_state_root = (
        config.paths.web_state_dir
        / AV1_VALIDATION_DERIVATION_ARTIFACT_DIRECTORY
        / plan.partition_id
    ).expanduser().resolve()
    if artifact_root != expected_state_root:
        raise AV1ValidationDerivationError(
            "AV1 derivation recovery requires the canonical runtime state root"
        )
    return plan, artifact_root


def _load_bound_derivation_plan(
        plan_path: Path,
) -> tuple[AV1ValidationDerivationPlan, Path]:
    assert_private_artifact_path(plan_path, repository_root=REPOSITORY_ROOT)
    plan = load_av1_validation_derivation_plan(plan_path)
    artifact_root = plan_path.expanduser().resolve().parent
    if plan_path.expanduser().resolve() != artifact_root / "plan.json":
        raise AV1ValidationDerivationError(
            "AV1 derivation plan must use the frozen artifact-root plan path"
        )
    validate_av1_validation_derivation_artifact_root_binding(
        artifact_root,
        plan,
    )
    return plan, artifact_root


def _review_runner_identity() -> tuple[Path, str, str]:
    code_binary = shutil.which("code")
    if code_binary is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation review requires the Every Code executable"
        )
    try:
        resolved_binary = Path(code_binary).expanduser().resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code executable is unavailable"
        ) from exc
    if resolved_binary != _trusted_code_ancestor_path():
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code executable is not the active trusted runner"
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved_binary, flags)
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code executable is unavailable"
        ) from exc
    try:
        descriptor_info = os.fstat(descriptor)
        path_info = resolved_binary.lstat()
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
            or not os.access(resolved_binary, os.X_OK)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation Every Code executable identity is invalid"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            binary_bytes = handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _assert_native_review_runner(binary_bytes)
    canonical_path_sha256 = (
        f"sha256:{hashlib.sha256(str(resolved_binary).encode('utf-8')).hexdigest()}"
    )
    binary_sha256 = f"sha256:{hashlib.sha256(binary_bytes).hexdigest()}"
    return resolved_binary, canonical_path_sha256, binary_sha256


def _authorized_review_runner_identity(
        plan: AV1ValidationDerivationPlan,
) -> tuple[Path, str, str]:
    identity = _review_runner_identity()
    if (
        identity[1] != plan.review_runner_canonical_path_sha256
        or identity[2] != plan.review_runner_binary_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code executable drifted from the plan"
        )
    return identity


def _is_raw_git_object_id(value: str) -> bool:
    return (
        len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


def _review_git_command(*arguments: str) -> list[str]:
    if not _system_git_binary_is_trusted(_REVIEW_GIT_COMMAND_PREFIX[0]):
        raise AV1ValidationDerivationError(
            "AV1 derivation review requires trusted direct system Git"
        )
    return [*_REVIEW_GIT_COMMAND_PREFIX, *arguments]


def _repository_review_identity(
        *,
        process_controller: ManagedProcessController,
        repository_root: Path | None = None,
) -> tuple[str, str]:
    _assert_preregistration_bootstrap_authority()
    root = (REPOSITORY_ROOT if repository_root is None else repository_root).resolve()
    git_environment, git_authority = _review_git_context(repository_root=root)
    git_monitor = _ReviewGitAuthorityMonitor(git_authority)

    def resolve_identity() -> tuple[str, str]:
        identity = _run_review_git_process(
            _review_git_command("rev-parse", "HEAD", "HEAD^{tree}"),
            process_controller=process_controller,
            repository_root=root,
            git_environment=git_environment,
            git_authority=git_authority,
            git_monitor=git_monitor,
        )
        object_ids = tuple(identity.stdout.splitlines())
        if (
            identity.returncode != 0
            or len(object_ids) != 2
            or any(not _is_raw_git_object_id(object_id) for object_id in object_ids)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review repository identity is unavailable"
            )
        return object_ids[0], object_ids[1]

    def assert_clean_state(
            object_ids: tuple[str, str],
            index_snapshot: tuple[bytes, bytes],
    ) -> bytes:
        tree_state = _run_review_git_process(
            _review_git_command(
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                object_ids[1],
            ),
            process_controller=process_controller,
            repository_root=root,
            git_environment=git_environment,
            git_authority=git_authority,
            git_monitor=git_monitor,
            text=False,
        )
        if tree_state.returncode != 0 or not isinstance(tree_state.stdout, bytes):
            raise AV1ValidationDerivationError(
                "AV1 derivation review repository state is unavailable"
            )
        _assert_exact_git_worktree_snapshot(
            str(root),
            index_snapshot[0],
            tree_state.stdout,
            index_snapshot[1],
            capture_mediaforce_python=False,
            failure_type=AV1ValidationDerivationError,
            message=(
                "AV1 derivation review repository has uncommitted tracked changes"
            ),
        )
        repository_state = _run_review_git_process(
            _review_git_command(
                "ls-files",
                "-z",
                "--others",
                "--exclude-standard",
            ),
            process_controller=process_controller,
            repository_root=root,
            git_environment=git_environment,
            git_authority=git_authority,
            git_monitor=git_monitor,
            text=False,
        )
        if (
            repository_state.returncode != 0
            or not isinstance(repository_state.stdout, bytes)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review repository state is unavailable"
            )
        if repository_state.stdout:
            raise AV1ValidationDerivationError(
                "AV1 derivation review repository is not clean"
            )
        ignored_implementation_state = _run_review_git_process(
            _review_git_command(
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--",
                "mediaforce",
                "scripts/verify_av1_cold_start_preregistration.py",
            ),
            process_controller=process_controller,
            repository_root=root,
            git_environment=git_environment,
            git_authority=git_authority,
            git_monitor=git_monitor,
            text=False,
        )
        if (
            ignored_implementation_state.returncode != 0
            or not isinstance(ignored_implementation_state.stdout, bytes)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation ignored implementation state is unavailable"
            )
        if ignored_implementation_state.stdout:
            raise AV1ValidationDerivationError(
                "AV1 derivation repository has ignored implementation artifacts"
            )
        return tree_state.stdout

    try:
        _assert_review_git_authority(root, git_authority)
        git_monitor.assert_quiet()
        index_snapshot = _prime_review_worktree_monitor(
            repository_root=root,
            process_controller=process_controller,
            git_environment=git_environment,
            git_authority=git_authority,
            git_monitor=git_monitor,
        )
        object_ids = resolve_identity()
        tree_snapshot = assert_clean_state(object_ids, index_snapshot)
        verified_object_ids = resolve_identity()
        if verified_object_ids != object_ids:
            raise AV1ValidationDerivationError(
                "AV1 derivation review repository identity changed during verification"
            )
        verified_index_snapshot = _prime_review_worktree_monitor(
            repository_root=root,
            process_controller=process_controller,
            git_environment=git_environment,
            git_authority=git_authority,
            git_monitor=git_monitor,
        )
        if verified_index_snapshot != index_snapshot:
            raise AV1ValidationDerivationError(
                "AV1 derivation review repository identity changed during verification"
            )
        verified_tree_snapshot = assert_clean_state(
            verified_object_ids,
            verified_index_snapshot,
        )
        if verified_tree_snapshot != tree_snapshot:
            raise AV1ValidationDerivationError(
                "AV1 derivation review repository identity changed during verification"
            )
        git_monitor.assert_quiet()
        _assert_review_git_authority(root, git_authority)
        git_monitor.assert_quiet()
        _assert_preregistration_bootstrap_authority()
        return verified_object_ids
    finally:
        try:
            git_monitor.assert_quiet()
            _assert_review_git_authority(root, git_authority)
            git_monitor.assert_quiet()
        finally:
            git_monitor.close()


def _live_repository_identity() -> tuple[str, str]:
    _assert_preregistration_bootstrap_authority()
    identity = _repository_review_identity(
        process_controller=ManagedProcessController(),
    )
    _assert_preregistration_bootstrap_authority()
    return identity


def _read_review_git_metadata_file(path: Path, label: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        descriptor_info = os.fstat(descriptor)
        path_info = path.lstat()
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or descriptor_info.st_uid != os.getuid()
            or descriptor_info.st_nlink != 1
            or descriptor_info.st_size > 4096
            or stat.S_IMODE(descriptor_info.st_mode) & 0o022
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise AV1ValidationDerivationError(
                f"AV1 derivation review refuses unsafe {label}"
            )
        raw = bytearray()
        while len(raw) <= 4096:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > 4096:
            raise AV1ValidationDerivationError(
                f"AV1 derivation review refuses oversized {label}"
            )
        return bytes(raw).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise AV1ValidationDerivationError(
            f"AV1 derivation review refuses invalid {label}"
        ) from exc
    except OSError as exc:
        raise AV1ValidationDerivationError(
            f"AV1 derivation review could not inspect {label}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _review_git_authority(repository_root: Path) -> tuple[Path, Path]:
    root = repository_root.expanduser().resolve()
    metadata_path = root / ".git"
    try:
        metadata_info = metadata_path.lstat()
    except OSError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git metadata is unavailable"
        ) from exc
    if stat.S_ISDIR(metadata_info.st_mode):
        git_directory = metadata_path.resolve(strict=True)
    elif stat.S_ISREG(metadata_info.st_mode):
        pointer = _read_review_git_metadata_file(
            metadata_path,
            "Git metadata pointer",
        )
        prefix = "gitdir:"
        if not pointer.startswith(prefix) or not pointer[len(prefix):].strip():
            raise AV1ValidationDerivationError(
                "AV1 derivation review Git metadata pointer is invalid"
            )
        candidate = Path(pointer[len(prefix):].strip())
        if not candidate.is_absolute():
            candidate = root / candidate
        git_directory = candidate.resolve(strict=True)
        backlink = Path(_read_review_git_metadata_file(
            git_directory / "gitdir",
            "Git worktree backlink",
        ))
        if not backlink.is_absolute():
            backlink = git_directory / backlink
        if backlink.resolve(strict=True) != metadata_path.resolve(strict=True):
            raise AV1ValidationDerivationError(
                "AV1 derivation review Git metadata is foreign"
            )
    else:
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git metadata is unsafe"
        )
    commondir_path = git_directory / "commondir"
    if commondir_path.exists():
        common_value = _read_review_git_metadata_file(
            commondir_path,
            "Git common-directory pointer",
        )
        if not common_value:
            raise AV1ValidationDerivationError(
                "AV1 derivation review Git common directory is invalid"
            )
        common_directory = Path(common_value)
        if not common_directory.is_absolute():
            common_directory = git_directory / common_directory
        common_directory = common_directory.resolve(strict=True)
        if git_directory.parent != common_directory / "worktrees":
            raise AV1ValidationDerivationError(
                "AV1 derivation review Git worktree metadata is foreign"
            )
    else:
        common_directory = git_directory
    for path, label in (
        (git_directory, "Git directory"),
        (common_directory, "Git common directory"),
    ):
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise AV1ValidationDerivationError(
                f"AV1 derivation review refuses unsafe {label}"
            )
    _assert_git_metadata_authority_is_closed(
        str(git_directory),
        str(common_directory),
        failure_type=AV1ValidationDerivationError,
        message=(
            "AV1 derivation review refuses external or symlinked Git authority"
        ),
    )
    return git_directory, common_directory


_ReviewGitAuthoritySnapshot: TypeAlias = tuple[
    tuple[Path, tuple[int, int]],
    tuple[Path, tuple[int, int]],
    tuple[Path, tuple[int, int]],
]


class _ReviewGitAuthorityMonitor(_RepositoryAuthorityMonitor):
    def __init__(self, authority: _ReviewGitAuthoritySnapshot) -> None:
        root_entry, git_entry, common_entry = authority
        metadata_path = root_entry[0] / ".git"
        try:
            metadata_info = metadata_path.lstat()
        except OSError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review Git authority is unavailable"
            ) from exc
        metadata_files = (
            (str(metadata_path),)
            if stat.S_ISREG(metadata_info.st_mode)
            else ()
        )
        super().__init__(
            root=str(root_entry[0]),
            authority_directories=tuple(
                (
                    str(path),
                    expected_identity,
                    path != root_entry[0],
                )
                for path, expected_identity in authority
            ),
            metadata_roots=(str(git_entry[0]), str(common_entry[0])),
            metadata_excluded_roots=(
                str(common_entry[0] / "worktrees"),
            ),
            metadata_files=metadata_files,
            failure_type=AV1ValidationDerivationError,
            changed_message="AV1 derivation review Git authority changed",
            unavailable_message=(
                "AV1 derivation review Git authority monitoring is unavailable"
            ),
        )
        _assert_git_metadata_authority_is_closed(
            str(git_entry[0]),
            str(common_entry[0]),
            failure_type=AV1ValidationDerivationError,
            message=(
                "AV1 derivation review refuses external or symlinked Git authority"
            ),
        )
        self.assert_quiet()


def _review_git_authority_snapshot(
        repository_root: Path,
) -> _ReviewGitAuthoritySnapshot:
    root = repository_root.expanduser().resolve()
    git_directory, common_directory = _review_git_authority(root)
    entries: list[tuple[Path, tuple[int, int]]] = []
    for path in (root, git_directory, common_directory):
        try:
            info = path.lstat()
        except OSError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review Git authority is unavailable"
            ) from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise AV1ValidationDerivationError(
                "AV1 derivation review Git authority is unsafe"
            )
        entries.append((path, (info.st_dev, info.st_ino)))
    return cast(_ReviewGitAuthoritySnapshot, tuple(entries))


def _assert_review_git_authority(
        repository_root: Path,
        authority: _ReviewGitAuthoritySnapshot,
) -> None:
    root_entry, git_entry, common_entry = authority
    current_root = repository_root.expanduser().resolve()
    current_git_directory, current_common_directory = _review_git_authority(
        repository_root
    )
    if (
        current_root != root_entry[0]
        or current_git_directory != git_entry[0]
        or current_common_directory != common_entry[0]
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git authority changed"
        )
    for path, expected_identity in authority:
        try:
            info = path.lstat()
        except OSError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review Git authority changed"
            ) from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or (info.st_dev, info.st_ino) != expected_identity
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review Git authority changed"
            )


def _review_git_environment_for_authority(
        authority: _ReviewGitAuthoritySnapshot,
) -> dict[str, str]:
    root_entry, git_entry, common_entry = authority
    return {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_DIR": str(git_entry[0]),
        "GIT_COMMON_DIR": str(common_entry[0]),
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_WORK_TREE": str(root_entry[0]),
        "LANG": "C",
        "LC_ALL": "C",
        "PAGER": "cat",
        "PATH": _AGENT_REVIEW_SAFE_PATH,
    }


def _review_git_context(
        *,
        repository_root: Path,
) -> tuple[dict[str, str], _ReviewGitAuthoritySnapshot]:
    authority = _review_git_authority_snapshot(repository_root)
    environment = _review_git_environment_for_authority(authority)
    _assert_review_git_authority(repository_root, authority)
    return environment, authority


@contextmanager
def _review_git_transaction(
        *,
        repository_root: Path,
) -> Iterator[
    tuple[
        dict[str, str],
        _ReviewGitAuthoritySnapshot,
        _ReviewGitAuthorityMonitor,
    ]
]:
    git_environment, git_authority = _review_git_context(
        repository_root=repository_root
    )
    monitor = _ReviewGitAuthorityMonitor(git_authority)
    try:
        _assert_review_git_authority(repository_root, git_authority)
        monitor.assert_quiet()
        try:
            yield git_environment, git_authority, monitor
        except BaseException as exc:
            for assertion in (
                lambda: _assert_review_git_authority(
                    repository_root,
                    git_authority,
                ),
                monitor.assert_quiet,
            ):
                try:
                    assertion()
                except AV1ValidationDerivationError as authority_error:
                    exc.add_note(
                        "AV1 derivation review Git transaction authority also changed: "
                        f"{authority_error}"
                    )
            raise
        monitor.assert_quiet()
        _assert_review_git_authority(repository_root, git_authority)
        monitor.assert_quiet()
    finally:
        monitor.close()


def _review_git_environment(*, repository_root: Path) -> dict[str, str]:
    environment, _authority = _review_git_context(
        repository_root=repository_root
    )
    return environment


def _review_runner_environment(*, working_directory: Path) -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": pwd.getpwuid(os.getuid()).pw_dir,
        "LANG": "C",
        "LC_ALL": "C",
        "LOGNAME": _AGENT_REVIEW_GENERIC_IDENTITY,
        "PATH": _AGENT_REVIEW_SAFE_PATH,
        "PWD": str(working_directory),
        "SHELL": "/bin/zsh",
        "TMPDIR": "/tmp",
        "USER": _AGENT_REVIEW_GENERIC_IDENTITY,
        "ZDOTDIR": "/var/empty",
    }


def _run_review_git(
        arguments: list[str],
        *,
        repository_root: Path,
        process_controller: ManagedProcessController,
        git_environment: dict[str, str],
        git_authority: _ReviewGitAuthoritySnapshot,
        git_monitor: _ReviewGitAuthorityMonitor,
        binary: bool = False,
) -> str | bytes:
    if git_environment != _review_git_environment_for_authority(git_authority):
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git environment drifted"
        )
    _assert_review_git_authority(repository_root, git_authority)
    git_monitor.assert_quiet()
    try:
        completed = _run_review_git_process(
            _review_git_command(*arguments),
            process_controller=process_controller,
            repository_root=repository_root,
            git_environment=git_environment,
            git_authority=git_authority,
            git_monitor=git_monitor,
            text=not binary,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git data is unavailable"
        ) from exc
    if completed.returncode != 0:
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git data is unavailable"
        )
    output = completed.stdout
    if binary:
        if not isinstance(output, bytes):
            raise AV1ValidationDerivationError(
                "AV1 derivation review Git blob data is invalid"
            )
        return output
    if not isinstance(output, str):
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git text data is invalid"
        )
    return output


def _run_review_git_process(
        command: list[str],
        *,
        repository_root: Path,
        process_controller: ManagedProcessController,
        git_environment: dict[str, str],
        git_authority: _ReviewGitAuthoritySnapshot,
        git_monitor: _ReviewGitAuthorityMonitor,
        text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    _assert_review_git_authority(repository_root, git_authority)
    git_monitor.assert_quiet()
    try:
        completed = run_command(
            command,
            process_controller=process_controller,
            cwd=repository_root,
            env=git_environment,
            text=text,
            timeout=15,
            check=False,
        )
    except BaseException as exc:
        for assertion in (
            lambda: _assert_review_git_authority(
                repository_root,
                git_authority,
            ),
            git_monitor.assert_quiet,
        ):
            try:
                assertion()
            except AV1ValidationDerivationError as authority_error:
                exc.add_note(
                    "AV1 derivation review Git authority also changed: "
                    f"{authority_error}"
                )
        raise
    git_monitor.assert_quiet()
    _assert_review_git_authority(repository_root, git_authority)
    git_monitor.assert_quiet()
    return completed


def _prime_review_worktree_monitor(
        *,
        repository_root: Path,
        process_controller: ManagedProcessController,
        git_environment: dict[str, str],
        git_authority: _ReviewGitAuthoritySnapshot,
        git_monitor: _ReviewGitAuthorityMonitor,
) -> tuple[bytes, bytes]:
    stage_command = _review_git_command(
        "ls-files",
        "--cached",
        "--stage",
        "-z",
    )
    verbose_command = _review_git_command(
        "ls-files",
        "--cached",
        "-v",
        "-z",
    )
    initial_stage = _run_review_git_process(
        stage_command,
        process_controller=process_controller,
        repository_root=repository_root,
        git_environment=git_environment,
        git_authority=git_authority,
        git_monitor=git_monitor,
        text=False,
    )
    if initial_stage.returncode != 0 or not isinstance(initial_stage.stdout, bytes):
        raise AV1ValidationDerivationError(
            "AV1 derivation review repository index state is unavailable"
        )
    index_entries = _parse_git_index_snapshot(
        initial_stage.stdout,
        failure_type=AV1ValidationDerivationError,
        message="AV1 derivation review repository has unsafe index state",
    )
    git_monitor.add_worktree_paths(
        tuple(os.fsdecode(path) for path, _mode, _object_id in index_entries)
    )
    initial_verbose = _run_review_git_process(
        verbose_command,
        process_controller=process_controller,
        repository_root=repository_root,
        git_environment=git_environment,
        git_authority=git_authority,
        git_monitor=git_monitor,
        text=False,
    )
    if (
        initial_verbose.returncode != 0
        or not isinstance(initial_verbose.stdout, bytes)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review repository index state is unavailable"
        )
    _assert_ordinary_git_index_state(
        initial_verbose.stdout,
        tuple(path for path, _mode, _object_id in index_entries),
        failure_type=AV1ValidationDerivationError,
        message="AV1 derivation review repository has unsafe index state",
    )
    verified_stage = _run_review_git_process(
        stage_command,
        process_controller=process_controller,
        repository_root=repository_root,
        git_environment=git_environment,
        git_authority=git_authority,
        git_monitor=git_monitor,
        text=False,
    )
    verified_verbose = _run_review_git_process(
        verbose_command,
        process_controller=process_controller,
        repository_root=repository_root,
        git_environment=git_environment,
        git_authority=git_authority,
        git_monitor=git_monitor,
        text=False,
    )
    if (
        verified_stage.returncode != 0
        or not isinstance(verified_stage.stdout, bytes)
        or verified_verbose.returncode != 0
        or not isinstance(verified_verbose.stdout, bytes)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review repository index state is unavailable"
        )
    if (
        verified_stage.stdout != initial_stage.stdout
        or verified_verbose.stdout != initial_verbose.stdout
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git authority changed"
        )
    return verified_stage.stdout, verified_verbose.stdout


def _review_git_commit_tree(
        commit: str,
        *,
        repository_root: Path,
        process_controller: ManagedProcessController,
        git_environment: dict[str, str],
        git_authority: _ReviewGitAuthoritySnapshot,
        git_monitor: _ReviewGitAuthorityMonitor,
) -> str:
    payload = _run_review_git(
        ["cat-file", "commit", commit],
        repository_root=repository_root,
        process_controller=process_controller,
        git_environment=git_environment,
        git_authority=git_authority,
        git_monitor=git_monitor,
        binary=True,
    )
    if not isinstance(payload, bytes):
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git commit data is invalid"
        )
    headers, separator, _message = payload.partition(b"\n\n")
    tree_entries = [
        header.removeprefix(b"tree ")
        for header in headers.splitlines()
        if header.startswith(b"tree ")
    ]
    if not separator or len(tree_entries) != 1:
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git commit data is invalid"
        )
    try:
        tree = tree_entries[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git commit data is invalid"
        ) from exc
    if not _is_raw_git_object_id(tree):
        raise AV1ValidationDerivationError(
            "AV1 derivation review Git commit data is invalid"
        )
    return tree


def _build_av1_validation_derivation_review_bundle(
        *,
        claim: AV1ValidationDerivationReviewClaim,
        process_controller: ManagedProcessController,
        repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    root = repository_root.resolve()
    with _review_git_transaction(repository_root=root) as (
        git_environment,
        git_authority,
        git_monitor,
    ):
        _prime_review_worktree_monitor(
            repository_root=root,
            process_controller=process_controller,
            git_environment=git_environment,
            git_authority=git_authority,
            git_monitor=git_monitor,
        )
        return _build_av1_validation_derivation_review_bundle_transaction(
            claim=claim,
            process_controller=process_controller,
            repository_root=root,
            git_environment=git_environment,
            git_authority=git_authority,
            git_monitor=git_monitor,
        )


def _build_av1_validation_derivation_review_bundle_transaction(
        *,
        claim: AV1ValidationDerivationReviewClaim,
        process_controller: ManagedProcessController,
        repository_root: Path,
        git_environment: dict[str, str],
        git_authority: _ReviewGitAuthoritySnapshot,
        git_monitor: _ReviewGitAuthorityMonitor,
) -> dict[str, object]:
    resolved_tree = _review_git_commit_tree(
        claim.repository_commit,
        repository_root=repository_root,
        process_controller=process_controller,
        git_environment=git_environment,
        git_authority=git_authority,
        git_monitor=git_monitor,
    )
    if resolved_tree != claim.repository_tree:
        raise AV1ValidationDerivationError(
            "AV1 derivation review bundle repository identity drifted"
        )
    allowed_paths = AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_ALLOWLIST.get(claim.lane)
    if allowed_paths is None:
        raise AV1ValidationDerivationError("AV1 derivation review bundle lane is invalid")
    files: list[dict[str, object]] = []
    total_size = 0
    for path in allowed_paths:
        listing = _run_review_git(
            [
                "ls-tree",
                "-z",
                "--full-tree",
                resolved_tree,
                "--",
                path,
            ],
            repository_root=repository_root,
            process_controller=process_controller,
            git_environment=git_environment,
            git_authority=git_authority,
            git_monitor=git_monitor,
            binary=True,
        )
        records = [record for record in listing.split(b"\0") if record]
        if len(records) != 1 or b"\t" not in records[0]:
            raise AV1ValidationDerivationError(
                "AV1 derivation review bundle allowlisted path is unavailable"
            )
        metadata, encoded_path = records[0].split(b"\t", 1)
        try:
            mode, object_type, blob_id_bytes = metadata.split(b" ", 2)
            listed_path = encoded_path.decode("utf-8")
            blob_id = blob_id_bytes.decode("ascii")
        except (UnicodeDecodeError, ValueError) as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review bundle tree entry is invalid"
            ) from exc
        if (
            listed_path != path
            or mode not in {b"100644", b"100755"}
            or object_type != b"blob"
            or not _is_raw_git_object_id(blob_id)
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review bundle tree entry is not an allowlisted blob"
            )
        raw_size = _run_review_git(
            ["cat-file", "-s", blob_id],
            repository_root=repository_root,
            process_controller=process_controller,
            git_environment=git_environment,
            git_authority=git_authority,
            git_monitor=git_monitor,
        )
        try:
            blob_size = int(raw_size.strip())
        except ValueError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review bundle blob size is invalid"
            ) from exc
        if (
            blob_size < 0
            or blob_size > AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BLOB_BYTES
            or total_size + blob_size
            > AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_BUNDLE_BYTES
        ):
            raise AV1ValidationDerivationError("AV1 derivation review bundle is oversized")
        blob = _run_review_git(
            ["cat-file", "blob", blob_id],
            repository_root=repository_root,
            process_controller=process_controller,
            git_environment=git_environment,
            git_authority=git_authority,
            git_monitor=git_monitor,
            binary=True,
        )
        if len(blob) != blob_size:
            raise AV1ValidationDerivationError(
                "AV1 derivation review bundle blob size drifted"
            )
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review bundle blob is not UTF-8"
            ) from exc
        files.append({
            "path": path,
            "blob_id": blob_id,
            "blob_size_bytes": blob_size,
            "blob_sha256": f"sha256:{hashlib.sha256(blob).hexdigest()}",
            "text": text,
        })
        total_size += blob_size
    semantic_payload = {
        "schema": AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA_VERSION,
        "lane": claim.lane,
        "repository_commit": claim.repository_commit,
        "repository_tree": claim.repository_tree,
        "files": files,
    }
    return {
        **semantic_payload,
        "payload_sha256": (
            f"sha256:{hashlib.sha256(canonical_json_bytes(semantic_payload)).hexdigest()}"
        ),
    }


def _write_review_request(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise AV1ValidationDerivationError(
                "AV1 derivation review request file could not be written"
            )
        remaining = remaining[written:]


@contextmanager
def _owner_only_review_request_file(request: bytes) -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="mediaforce-av1-review-request-", dir="/tmp"))
    path = directory / "request.json"
    descriptor = -1
    directory_identity: tuple[int, int] | None = None
    file_identity: tuple[int, int] | None = None
    try:
        os.chmod(directory, 0o700)
        directory_info = directory.lstat()
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or stat.S_IMODE(directory_info.st_mode) != 0o700
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review request directory is not owner-only"
            )
        directory_identity = (directory_info.st_dev, directory_info.st_ino)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        file_info = os.fstat(descriptor)
        file_identity = (file_info.st_dev, file_info.st_ino)
        _write_review_request(descriptor, request)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        file_info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_info.st_mode)
            or file_info.st_uid != os.getuid()
            or stat.S_IMODE(file_info.st_mode) != 0o400
            or file_info.st_nlink != 1
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review request file is not owner-only"
            )
        os.close(descriptor)
        descriptor = -1
        yield path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_identity is None:
            return
        try:
            directory_info = directory.lstat()
        except OSError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review request cleanup identity is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or stat.S_IMODE(directory_info.st_mode) != 0o700
            or (directory_info.st_dev, directory_info.st_ino) != directory_identity
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation review request cleanup identity changed"
            )
        try:
            if file_identity is not None:
                file_info = path.lstat()
                if (
                    not stat.S_ISREG(file_info.st_mode)
                    or stat.S_ISLNK(file_info.st_mode)
                    or (file_info.st_dev, file_info.st_ino) != file_identity
                    or file_info.st_uid != os.getuid()
                    or stat.S_IMODE(file_info.st_mode) != 0o400
                    or file_info.st_nlink != 1
                ):
                    raise AV1ValidationDerivationError(
                        "AV1 derivation review request cleanup identity changed"
                    )
                path.unlink()
            elif path.exists() or path.is_symlink():
                raise AV1ValidationDerivationError(
                    "AV1 derivation review request cleanup identity is unavailable"
                )
            directory.rmdir()
        except OSError as exc:
            raise AV1ValidationDerivationError(
                "AV1 derivation review request cleanup failed"
            ) from exc


def _structured_review_response(
        stdout: str,
        *,
        proposal: AV1ValidationDerivationCandidateProposal,
        claim: AV1ValidationDerivationReviewClaim,
) -> tuple[dict[str, object], AV1ValidationDerivationReviewDecision]:
    return parse_av1_validation_derivation_review_response(
        stdout,
        proposal=proposal,
        claim=claim,
    )


def _assert_native_review_runner(binary_bytes: bytes) -> None:
    if binary_bytes[:4] not in _MACH_O_MAGICS:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code executable must be a native Mach-O binary"
        )


def _trusted_code_ancestor_path() -> Path:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        proc_pidpath = libproc.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
    except (AttributeError, OSError) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation active Every Code runner identity is unavailable"
        ) from exc
    process_id = os.getppid()
    for _ in range(16):
        buffer = ctypes.create_string_buffer(4096)
        if proc_pidpath(process_id, buffer, len(buffer)) > 0:
            candidate = Path(buffer.value.decode("utf-8")).resolve()
            if candidate.name == "code":
                return candidate
        parent = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-p", str(process_id)],
            text=True,
            capture_output=True,
            check=False,
            env={"PATH": _AGENT_REVIEW_SAFE_PATH},
        )
        try:
            process_id = int(parent.stdout.strip())
        except ValueError:
            break
        if process_id <= 1:
            break
    raise AV1ValidationDerivationError(
        "AV1 derivation must run from the active trusted Every Code session"
    )


def _load_existing_derivation_review(
        *,
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        lane: AV1ValidationDerivationReviewLane,
) -> tuple[
    AV1ValidationDerivationReviewClaim,
    AV1ValidationDerivationReviewEnvelope | None,
] | None:
    claim_path = (
        artifact_root
        / "review-claims"
        / proposal.proposal_id
        / f"{lane}.json"
    )
    envelope_path = (
        artifact_root
        / "reviews"
        / proposal.proposal_id
        / f"{lane}.json"
    )
    checkpoint_path = (
        artifact_root
        / "review-responses"
        / proposal.proposal_id
        / f"{lane}.json"
    )
    claim_exists = claim_path.exists() or claim_path.is_symlink()
    envelope_exists = envelope_path.exists() or envelope_path.is_symlink()
    checkpoint_exists = checkpoint_path.exists() or checkpoint_path.is_symlink()
    if not claim_exists:
        if envelope_exists or checkpoint_exists:
            raise AV1ValidationDerivationError(
                "AV1 derivation review state has no immutable lane claim"
            )
        return None
    claims = load_av1_validation_derivation_review_claims(
        artifact_root,
        plan=plan,
        proposal=proposal,
    )
    claim = next((item for item in claims if item.lane == lane), None)
    if claim is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation review claim directory lost its requested lane"
        )
    if not envelope_exists:
        return claim, None
    envelope = load_av1_validation_derivation_review_envelope(
        artifact_root,
        plan=plan,
        proposal=proposal,
        claim=claim,
    )
    return claim, envelope


def _derivation_review_recovery_counts(
        *,
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
) -> tuple[int, int]:
    review_claim_count = 0
    unresolved_review_claim_count = 0
    for cell_plan_id in sorted({
        assignment.cell_plan_id
        for assignment in plan.assignments
    }):
        proposal_path = (
            artifact_root
            / "proposals"
            / f"{cell_plan_id}.json"
        )
        if not (proposal_path.exists() or proposal_path.is_symlink()):
            continue
        proposal = load_av1_validation_derivation_candidate_proposal(
            artifact_root,
            plan=plan,
            cell_plan_id=cell_plan_id,
        )
        claim_directory = (
            artifact_root
            / "review-claims"
            / proposal.proposal_id
        )
        if not (claim_directory.exists() or claim_directory.is_symlink()):
            continue
        claims = load_av1_validation_derivation_review_claims(
            artifact_root,
            plan=plan,
            proposal=proposal,
        )
        review_claim_count += len(claims)
        for claim in claims:
            envelope_path = (
                artifact_root
                / "reviews"
                / proposal.proposal_id
                / f"{claim.lane}.json"
            )
            if not (envelope_path.exists() or envelope_path.is_symlink()):
                unresolved_review_claim_count += 1
                continue
            load_av1_validation_derivation_review_envelope(
                artifact_root,
                plan=plan,
                proposal=proposal,
                claim=claim,
            )
    return review_claim_count, unresolved_review_claim_count


def _assert_code_llm_request_contract(
        code_binary: Path,
        *,
        process_controller: ManagedProcessController,
) -> None:
    working_directory = Path("/tmp").resolve()
    try:
        completed = run_command(
            [str(code_binary), "llm", "request", "--help"],
            process_controller=process_controller,
            cwd=working_directory,
            env=_review_runner_environment(
                working_directory=working_directory,
            ),
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code LLM request contract is unavailable"
        ) from exc
    required_options = (
        "--developer",
        "--message-file",
        "--format-type",
        "--format-name",
        "--format-strict",
        "--schema-json",
    )
    if (
        completed.returncode != 0
        or any(option not in completed.stdout for option in required_options)
        or "--request-file" in completed.stdout
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code LLM request contract is incompatible"
        )


def _assert_no_tool_llm_review_command(command: Sequence[str]) -> None:
    required_options = {
        "--developer",
        "--format-type",
        "--format-name",
        "--format-strict",
        "--message-file",
        "--schema-json",
    }
    disallowed_arguments = {
        "exec",
        "--auto",
        "--json",
        "--message",
        "--request-file",
        "--sandbox",
        "--schema",
        "-s",
    }
    if (
        len(command) < 3
        or tuple(command[1:3]) != ("llm", "request")
        or not required_options.issubset(command)
        or any(argument in disallowed_arguments for argument in command[1:])
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review must use Every Code LLM request with no tools"
        )


def _build_code_llm_review_materials(
        *,
        proposal: AV1ValidationDerivationCandidateProposal,
        claim: AV1ValidationDerivationReviewClaim,
        process_controller: ManagedProcessController,
) -> tuple[
    dict[str, object],
    dict[str, object],
    str,
    str,
    dict[str, object],
    str,
]:
    safe_bundle = _build_av1_validation_derivation_review_bundle(
        claim=claim,
        process_controller=process_controller,
    )
    request = build_av1_validation_derivation_review_request(
        proposal=proposal,
        claim=claim,
        safe_bundle=safe_bundle,
    )
    developer_text = av1_validation_derivation_review_developer_text(claim.lane)
    response_schema = build_av1_validation_derivation_review_response_schema(
        proposal=proposal,
        claim=claim,
    )
    request_sha256 = (
        f"sha256:{hashlib.sha256(canonical_json_bytes(request)).hexdigest()}"
    )
    response_schema_text = canonical_json_bytes(response_schema).decode("utf-8")
    return (
        safe_bundle,
        request,
        request_sha256,
        developer_text,
        response_schema,
        response_schema_text,
    )


def _assert_existing_code_llm_review_claim_bindings(
        *,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        lane: AV1ValidationDerivationReviewLane,
        claim: AV1ValidationDerivationReviewClaim,
        repository_identity: tuple[str, str],
) -> None:
    if (
        claim.plan_id != plan.plan_id
        or claim.authorization_id != plan.authorization.authorization_id
        or claim.proposal_id != proposal.proposal_id
        or claim.proposal_payload_sha256 != proposal.payload_sha256
        or claim.lane != lane
        or (claim.repository_commit, claim.repository_tree)
        != repository_identity
        or claim.review_runner_canonical_path_sha256
        != plan.review_runner_canonical_path_sha256
        or claim.review_runner_binary_sha256
        != plan.review_runner_binary_sha256
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation interrupted review claim is no longer authorized"
        )


def _code_llm_review_result_from_checkpoint(
        *,
        proposal: AV1ValidationDerivationCandidateProposal,
        claim: AV1ValidationDerivationReviewClaim,
        checkpoint: dict[str, object],
        safe_bundle: dict[str, object],
        request: dict[str, object],
        request_sha256: str,
        developer_text: str,
        response_schema: dict[str, object],
) -> tuple[bytes, AV1ValidationDerivationReviewDecision, str]:
    if checkpoint.get("request_sha256") != request_sha256:
        raise AV1ValidationDerivationError(
            "AV1 derivation review response checkpoint request changed"
        )
    response_text = checkpoint.get("response")
    stderr_sha256 = checkpoint.get("stderr_sha256")
    reviewed_at = checkpoint.get("completed_at")
    checkpoint_payload_sha256 = checkpoint.get("payload_sha256")
    if (
        not isinstance(response_text, str)
        or not isinstance(stderr_sha256, str)
        or not isinstance(reviewed_at, str)
        or not isinstance(checkpoint_payload_sha256, str)
    ):
        raise AV1ValidationDerivationError(
            "AV1 derivation review response checkpoint is invalid"
        )
    try:
        model_response, decision = _structured_review_response(
            response_text,
            proposal=proposal,
            claim=claim,
        )
    except AV1ValidationDerivationError:
        evidence = build_av1_validation_derivation_review_recovery_evidence(
            proposal=proposal,
            claim=claim,
            reason_code="invalid_durable_response",
            recovered_at=reviewed_at,
            response_checkpoint_payload_sha256=checkpoint_payload_sha256,
        )
        _assert_preregistration_bootstrap_authority()
        return evidence, "rejected", reviewed_at
    evidence_payload: dict[str, object] = {
        "schema": AV1_VALIDATION_DERIVATION_CHECKPOINTED_REVIEW_RUN_SCHEMA,
        "schema_version": AV1_VALIDATION_DERIVATION_REVIEW_BUNDLE_SCHEMA_VERSION,
        "review_run_id": claim.review_run_id,
        "reviewer_token": f"agent:{claim.review_run_id}",
        "proposal_id": proposal.proposal_id,
        "proposal_payload_sha256": proposal.payload_sha256,
        "review_claim_id": claim.claim_id,
        "review_claim_payload_sha256": claim.payload_sha256,
        "lane": claim.lane,
        "decision": decision,
        "repository_commit": claim.repository_commit,
        "repository_tree": claim.repository_tree,
        "review_runner_canonical_path_sha256": (
            claim.review_runner_canonical_path_sha256
        ),
        "review_runner_binary_sha256": claim.review_runner_binary_sha256,
        "proposal": proposal.to_payload(),
        "review_claim": claim.to_payload(),
        "safe_bundle": safe_bundle,
        "request": request,
        "request_sha256": request_sha256,
        "developer_text": developer_text,
        "developer_text_sha256": (
            f"sha256:{hashlib.sha256(developer_text.encode('utf-8')).hexdigest()}"
        ),
        "response_schema": response_schema,
        "response_schema_sha256": (
            f"sha256:{hashlib.sha256(canonical_json_bytes(response_schema)).hexdigest()}"
        ),
        "returncode": 0,
        "stderr_sha256": stderr_sha256,
        "model_response": model_response,
        "model_response_sha256": (
            f"sha256:{hashlib.sha256(canonical_json_bytes(model_response)).hexdigest()}"
        ),
        "analysis_sha256": av1_validation_derivation_review_analysis_sha256(
            model_response
        ),
        "response_checkpoint_payload_sha256": checkpoint_payload_sha256,
        "completed_at": reviewed_at,
    }
    evidence = canonical_json_bytes(evidence_payload)
    _assert_preregistration_bootstrap_authority()
    return evidence, decision, reviewed_at


def _recover_checkpointed_code_llm_review(
        *,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        lane: AV1ValidationDerivationReviewLane,
        claim: AV1ValidationDerivationReviewClaim,
        checkpoint: dict[str, object],
        before_publish: Callable[[], None] | None,
) -> tuple[
    AV1ValidationDerivationReviewClaim,
    bytes,
    AV1ValidationDerivationReviewDecision,
    str,
]:
    _assert_preregistration_bootstrap_authority()
    process_controller = ManagedProcessController()
    repository_identity = _repository_review_identity(
        process_controller=process_controller,
    )
    _assert_existing_code_llm_review_claim_bindings(
        plan=plan,
        proposal=proposal,
        lane=lane,
        claim=claim,
        repository_identity=repository_identity,
    )
    _call_preregistration_publication_guard(before_publish)
    (
        safe_bundle,
        request,
        request_sha256,
        developer_text,
        response_schema,
        _response_schema_text,
    ) = _build_code_llm_review_materials(
        proposal=proposal,
        claim=claim,
        process_controller=process_controller,
    )
    evidence, decision, reviewed_at = _code_llm_review_result_from_checkpoint(
        proposal=proposal,
        claim=claim,
        checkpoint=checkpoint,
        safe_bundle=safe_bundle,
        request=request,
        request_sha256=request_sha256,
        developer_text=developer_text,
        response_schema=response_schema,
    )
    return claim, evidence, decision, reviewed_at


def _run_code_llm_review(
        *,
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        lane: AV1ValidationDerivationReviewLane,
        claim: AV1ValidationDerivationReviewClaim | None = None,
        before_publish: Callable[[], None] | None = None,
) -> tuple[
    AV1ValidationDerivationReviewClaim,
    bytes,
    AV1ValidationDerivationReviewDecision,
    str,
]:
    if claim is not None:
        checkpoint = load_av1_validation_derivation_review_response_checkpoint(
            artifact_root,
            plan=plan,
            proposal=proposal,
            claim=claim,
        )
        if checkpoint is not None:
            return _recover_checkpointed_code_llm_review(
                plan=plan,
                proposal=proposal,
                lane=lane,
                claim=claim,
                checkpoint=checkpoint,
                before_publish=before_publish,
            )
    try:
        deadline = datetime.fromisoformat(
            plan.authorization.valid_until.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation review authorization deadline is invalid"
        ) from exc
    if deadline.tzinfo is None:
        raise AV1ValidationDerivationError(
            "AV1 derivation review authorization deadline must include a UTC offset"
        )
    process_controller = ManagedProcessController()
    try:
        with process_controller.absolute_deadline(deadline.astimezone(UTC)):
            return _run_code_llm_review_before_deadline(
                artifact_root=artifact_root,
                plan=plan,
                proposal=proposal,
                lane=lane,
                claim=claim,
                process_controller=process_controller,
                before_publish=before_publish,
            )
    except (ProcessCancelledError, ProcessDeadlineEnforcementError) as exc:
        raise AV1ValidationDerivationError(
            "AV1 derivation Every Code review exceeded its authorization deadline"
        ) from exc


def _run_code_llm_review_before_deadline(
        *,
        artifact_root: Path,
        plan: AV1ValidationDerivationPlan,
        proposal: AV1ValidationDerivationCandidateProposal,
        lane: AV1ValidationDerivationReviewLane,
        claim: AV1ValidationDerivationReviewClaim | None,
        process_controller: ManagedProcessController,
        before_publish: Callable[[], None] | None = None,
) -> tuple[
    AV1ValidationDerivationReviewClaim,
    bytes,
    AV1ValidationDerivationReviewDecision,
    str,
]:
    _assert_preregistration_bootstrap_authority()

    def guarded_before_publish() -> None:
        _call_preregistration_publication_guard(before_publish)

    assert_av1_validation_derivation_execution_environment(
        plan,
        process_controller=process_controller,
    )
    before_repository_identity = _repository_review_identity(
        process_controller=process_controller,
    )
    claim_was_existing = claim is not None
    before_identity: tuple[Path, str, str] | None = None
    if claim is None:
        before_identity = _authorized_review_runner_identity(plan)
        _assert_code_llm_request_contract(
            before_identity[0],
            process_controller=process_controller,
        )
        review_run_id = str(uuid.uuid4())
        claim = build_av1_validation_derivation_review_claim(
            plan=plan,
            proposal=proposal,
            repository_commit=before_repository_identity[0],
            repository_tree=before_repository_identity[1],
            lane=lane,
            review_run_id=review_run_id,
            review_runner_canonical_path_sha256=before_identity[1],
            review_runner_binary_sha256=before_identity[2],
            claimed_at=_now_iso(),
        )
        write_av1_validation_derivation_review_claim(
            artifact_root,
            plan=plan,
            proposal=proposal,
            claim=claim,
            before_publish=guarded_before_publish,
        )
    else:
        _assert_existing_code_llm_review_claim_bindings(
            plan=plan,
            proposal=proposal,
            lane=lane,
            claim=claim,
            repository_identity=before_repository_identity,
        )
        guarded_before_publish()
    checkpoint = load_av1_validation_derivation_review_response_checkpoint(
        artifact_root,
        plan=plan,
        proposal=proposal,
        claim=claim,
    )
    if claim_was_existing and checkpoint is None:
        reviewed_at = _now_iso()
        evidence = build_av1_validation_derivation_review_recovery_evidence(
            proposal=proposal,
            claim=claim,
            reason_code="interrupted_before_durable_response",
            recovered_at=reviewed_at,
        )
        _assert_preregistration_bootstrap_authority()
        return claim, evidence, "rejected", reviewed_at
    (
        safe_bundle,
        request,
        request_sha256,
        developer_text,
        response_schema,
        response_schema_text,
    ) = _build_code_llm_review_materials(
        proposal=proposal,
        claim=claim,
        process_controller=process_controller,
    )
    request_bytes = canonical_json_bytes(request)
    if checkpoint is None:
        if before_identity is None:
            raise AV1ValidationDerivationError(
                "AV1 derivation review runner identity is unavailable for launch"
            )
        try:
            with _owner_only_review_request_file(request_bytes) as request_path:
                command = [
                    str(before_identity[0]),
                    "llm",
                    "request",
                    "--developer",
                    developer_text,
                    "--message-file",
                    str(request_path),
                    "--format-type",
                    "json_schema",
                    "--format-name",
                    AV1_VALIDATION_DERIVATION_REVIEW_RESPONSE_SCHEMA_NAME,
                    "--format-strict",
                    "--schema-json",
                    response_schema_text,
                ]
                _assert_no_tool_llm_review_command(command)
                _assert_preregistration_bootstrap_authority()
                try:
                    completed = run_command(
                        command,
                        process_controller=process_controller,
                        cwd=request_path.parent,
                        env=_review_runner_environment(
                            working_directory=request_path.parent,
                        ),
                        timeout=_AGENT_REVIEW_MAX_SECONDS + 30,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise AV1ValidationDerivationError(
                        "AV1 derivation Every Code structured review did not complete"
                    ) from exc
        finally:
            integrity_errors: list[BaseException] = []
            try:
                after_repository_identity = _repository_review_identity(
                    process_controller=process_controller,
                )
                if after_repository_identity != before_repository_identity:
                    raise AV1ValidationDerivationError(
                        "AV1 derivation review repository changed during review"
                    )
            except BaseException as exc:
                integrity_errors.append(exc)
            try:
                after_identity = _authorized_review_runner_identity(plan)
                if after_identity != before_identity:
                    raise AV1ValidationDerivationError(
                        "AV1 derivation Every Code executable changed during review"
                    )
            except BaseException as exc:
                integrity_errors.append(exc)
            if integrity_errors:
                primary_error = integrity_errors[0]
                for error in integrity_errors[1:]:
                    primary_error.add_note(
                        "AV1 review integrity check also failed: "
                        f"{type(error).__name__}: {error}"
                    )
                raise primary_error
        if completed.returncode != 0:
            raise AV1ValidationDerivationError(
                "AV1 derivation Every Code structured review failed"
            )
        if (
            len(completed.stdout.encode("utf-8"))
            > AV1_VALIDATION_DERIVATION_REVIEW_MAXIMUM_RESPONSE_BYTES
        ):
            raise AV1ValidationDerivationError(
                "AV1 derivation Every Code structured review response is oversized"
            )
        write_av1_validation_derivation_review_response_checkpoint(
            artifact_root,
            plan=plan,
            proposal=proposal,
            claim=claim,
            request_sha256=request_sha256,
            response=completed.stdout,
            stderr_sha256=(
                f"sha256:{hashlib.sha256(completed.stderr.encode('utf-8')).hexdigest()}"
            ),
            completed_at=_now_iso(),
            before_publish=guarded_before_publish,
        )
        checkpoint = (
            load_av1_validation_derivation_review_response_checkpoint(
                artifact_root,
                plan=plan,
                proposal=proposal,
                claim=claim,
            )
        )
        if checkpoint is None:
            raise AV1ValidationDerivationError(
                "AV1 derivation review response checkpoint was not published"
            )
    evidence, decision, reviewed_at = _code_llm_review_result_from_checkpoint(
        proposal=proposal,
        claim=claim,
        checkpoint=checkpoint,
        safe_bundle=safe_bundle,
        request=request,
        request_sha256=request_sha256,
        developer_text=developer_text,
        response_schema=response_schema,
    )
    return claim, evidence, decision, reviewed_at


def _print_partition_payload(payload: dict[str, object], *, json_output: bool) -> None:
    assert_av1_cold_start_public_payload_safe(payload)
    if json_output:
        rendered_payload = json.dumps(payload, indent=2, sort_keys=True)
        _assert_preregistration_bootstrap_authority()
        print(rendered_payload)
        return
    fields = [
        f"{key}={str(value).lower() if isinstance(value, bool) else value}"
        for key, value in payload.items()
    ]
    rendered_payload = " ".join(fields)
    _assert_preregistration_bootstrap_authority()
    print(rendered_payload)


def _load_manifest(path: Path) -> ValidationManifest:
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise AV1ColdStartValidationError(
            "AV1 validation manifest must be a JSON object"
        )
    if payload.get("protocol_version") == 3:
        return load_av1_validation_protocol_v3(path)
    schema_version = payload.get("schema_version")
    if schema_version == 1:
        return load_av1_cold_start_validation_manifest(path)
    if schema_version == 2:
        return load_av1_validation_manifest_v2(path)
    raise AV1ColdStartValidationError(
        "AV1 validation manifest schema version is unsupported"
    )


def _validation_payload(manifest: ValidationManifest) -> dict[str, object]:
    if isinstance(manifest, AV1ValidationProtocolV3):
        return {
            "protocol_id": manifest.protocol_id,
            "schema_version": 1,
            "protocol_version": 3,
            "state": "owner_approved_non_executing",
            "candidate_cell_count": len(manifest.candidate_cells),
            "tier2_stratum_count": len(manifest.tier2_strata),
            "runtime_execution_authorized": False,
            "qualification_execution_authorized": False,
            "private_inventory_read_authorized": False,
            "key_creation_authorized": False,
            "partition_construction_authorized": False,
            "derivation_execution_authorized": False,
            "holdout_execution_authorized": False,
        }
    if isinstance(manifest, AV1ValidationManifestV2):
        return {
            "manifest_id": manifest.manifest_id,
            "schema_version": 2,
            "state": "preregistered_derivation_only",
            "authority": "derivation_only",
            "cell_plan_count": len(manifest.cell_plans),
            "candidate_cell_count": sum(
                plan.mode == "publication_candidate" for plan in manifest.cell_plans
            ),
            "fallback_cell_count": sum(
                plan.mode == "fallback_conformance" for plan in manifest.cell_plans
            ),
            "excluded_cell_count": len(manifest.excluded_cells),
            "registered_case_count": len(manifest.cases),
            "runtime_execution_authorized": False,
            "holdout_execution_authorized": False,
        }
    return {
        "manifest_id": manifest.manifest_id,
        "schema_version": 1,
        "state": manifest.state,
        "cell_plan_count": len(manifest.cell_plans),
        "registered_case_count": len(manifest.cases),
        "runtime_execution_authorized": False,
        "holdout_execution_authorized": False,
    }


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        _finalize_preregistration_bootstrap_authority()
