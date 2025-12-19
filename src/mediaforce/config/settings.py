import pathlib
from dataclasses import dataclass, field
from typing import Optional

from sqlmodel import Session, select, delete

from mediaforce.db import AppSetting, Library, init_engine


CONFIG_DIR = pathlib.Path.home() / ".config" / "mediaforce"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = CONFIG_DIR / "mediaforce.db"
SETTINGS_DB = DB_PATH
SETTINGS_PATH = DB_PATH
INVENTORY_DB = DB_PATH

ENGINE = init_engine(str(DB_PATH))


@dataclass
class LibrarySettings:
    id: str
    name: str
    media_type: str  # e.g. "tv", "movies"
    mac_path: str
    linux_path: str
    watch: bool = True
    max_height: Optional[int] = None  # Downscale target height (e.g., 1080); never upscales
    weight: float = 1.0


@dataclass
class AppSettings:
    libraries: list[LibrarySettings] = field(default_factory=list)
    global_max_height: Optional[int] = None
    max_concurrency: int = 1
    offpeak_enabled: bool = False
    offpeak_start: str = "00:00"
    offpeak_end: str = "05:00"


def _default_app_settings() -> AppSettings:
    return AppSettings(
        global_max_height=1080,
        max_concurrency=1,
        libraries=[
            LibrarySettings(
                id="tv",
                name="TV",
                media_type="tv",
                mac_path="/Volumes/media/tv",
                linux_path="/mnt/media/tv",
                max_height=1080,
            ),
            LibrarySettings(
                id="movies",
                name="Movies",
                media_type="movies",
                mac_path="/Volumes/media/movies",
                linux_path="/mnt/media/movies",
                max_height=2160,
            ),
        ],
    )


def load_app_settings() -> AppSettings:
    if SETTINGS_DB.exists():
        try:
            with Session(ENGINE) as session:
                setting = session.get(AppSetting, 1)
                gmh = setting.global_max_height if setting else None
                libs = session.exec(select(Library)).all()
                if libs:
                    return AppSettings(
                        libraries=[
                            LibrarySettings(
                                id=lib.id,
                                name=lib.name,
                                media_type=lib.media_type,
                                mac_path=lib.mac_path,
                                linux_path=lib.linux_path,
                                watch=lib.watch,
                                max_height=lib.max_height,
                                weight=lib.weight,
                            )
                            for lib in libs
                        ],
                        global_max_height=gmh,
                        max_concurrency=setting.max_concurrency if setting else 1,
                        offpeak_enabled=setting.offpeak_enabled if setting else False,
                        offpeak_start=(setting.offpeak_start or "00:00") if setting else "00:00",
                        offpeak_end=(setting.offpeak_end or "05:00") if setting else "05:00",
                    )
        except Exception:
            pass

    return _default_app_settings()


def save_app_settings(settings: AppSettings) -> None:
    with Session(ENGINE) as session:
        setting = session.get(AppSetting, 1) or AppSetting(id=1)
        setting.global_max_height = settings.global_max_height
        setting.max_concurrency = settings.max_concurrency
        setting.offpeak_enabled = settings.offpeak_enabled
        setting.offpeak_start = settings.offpeak_start
        setting.offpeak_end = settings.offpeak_end
        session.add(setting)
        session.exec(delete(Library))
        for lib in settings.libraries:
            session.add(
                Library(
                    id=lib.id,
                    name=lib.name,
                    media_type=lib.media_type,
                    mac_path=lib.mac_path,
                    linux_path=lib.linux_path,
                    watch=lib.watch,
                    max_height=lib.max_height,
                    weight=lib.weight,
                )
            )
        session.commit()


def init_db(_: Optional[pathlib.Path] = None) -> Session:
    return Session(ENGINE)
