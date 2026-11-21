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


def storage_access_issue(error: OSError | None) -> str | None:
    if isinstance(error, PermissionError):
        return "Mediaforce needs permission to access the media folder."
    if error is not None and error.errno == errno.ELOOP:
        return "A link in the media folder points back to itself. Check the folder path."
    return None


@dataclass(frozen=True, slots=True)
class MediaPathProbe:
    info: os.stat_result | None
    error: OSError | None = None

    @property
    def unavailable(self) -> bool:
        return self.error is not None

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
