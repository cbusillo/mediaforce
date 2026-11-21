import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path

STORAGE_WAIT_REASON = "Waiting for shared storage. Mediaforce will try again."


def stalled_media_read(error: BaseException) -> bool:
    return isinstance(error, OSError) and error.filename is not None and error.errno in {
        errno.ETIMEDOUT, errno.ENOTCONN, errno.ESTALE, errno.EIO, errno.EHOSTDOWN,
    }


@dataclass(frozen=True, slots=True)
class StorageProblem:
    reason: str
    needs_action: bool

    @property
    def code(self) -> str:
        return "storage_access" if self.needs_action else "storage_unavailable"


def storage_problem(error: OSError, *, retry_reason: str = STORAGE_WAIT_REASON) -> StorageProblem:
    if isinstance(error, PermissionError):
        return StorageProblem("Mediaforce needs permission to access the media folder.", True)
    if error.errno == errno.ELOOP:
        return StorageProblem("A link in the media folder points back to itself. Check the folder path.", True)
    return StorageProblem(retry_reason, False)


@dataclass(frozen=True, slots=True)
class MediaPathProbe:
    info: os.stat_result | None
    error: OSError | None = None

    @property
    def unavailable(self) -> bool:
        return self.error is not None

    @property
    def problem(self) -> StorageProblem | None:
        return storage_problem(self.error) if self.error is not None else None

    def checked_info(self) -> os.stat_result | None:
        if self.error is not None:
            raise self.error
        return self.info

    def exists_or_raise(self) -> bool:
        return self.checked_info() is not None

    @property
    def is_dir(self) -> bool:
        return self.info is not None and stat.S_ISDIR(self.info.st_mode)

    @property
    def is_file(self) -> bool:
        return self.info is not None and stat.S_ISREG(self.info.st_mode)


def probe_media_path(path: Path) -> MediaPathProbe:
    try:
        return MediaPathProbe(path.stat())
    except (FileNotFoundError, NotADirectoryError):
        return MediaPathProbe(None)
    except OSError as exc:
        return MediaPathProbe(None, error=exc)
