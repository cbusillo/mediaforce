import os
import stat
from dataclasses import dataclass
from pathlib import Path

STORAGE_WAIT_REASON = "Waiting for shared storage. Mediaforce will try again."


@dataclass(frozen=True, slots=True)
class MediaPathProbe:
    info: os.stat_result | None
    error: OSError | None = None

    @property
    def unavailable(self) -> bool:
        return self.error is not None

    def exists_or_raise(self) -> bool:
        if self.error is not None:
            raise self.error
        return self.info is not None

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
