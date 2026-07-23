import os
import subprocess
from datetime import UTC, datetime, tzinfo
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def valid_timezone_name(value: object) -> str | None:
    text = str(value or "").strip().removeprefix(":")
    if not text:
        return None
    try:
        ZoneInfo(text)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return text


@lru_cache(maxsize=1)
def system_timezone_name() -> str | None:
    environment_timezone = valid_timezone_name(os.environ.get("TZ"))
    if environment_timezone:
        return environment_timezone

    try:
        localtime_path = str(Path("/etc/localtime").resolve())
    except OSError:
        localtime_path = ""
    for marker in ("/zoneinfo.default/", "/zoneinfo/"):
        if marker not in localtime_path:
            continue
        timezone_name = valid_timezone_name(localtime_path.split(marker, 1)[1])
        if timezone_name:
            return timezone_name

    try:
        timezone_file_value = Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        timezone_file_value = ""
    timezone_name = valid_timezone_name(timezone_file_value)
    if timezone_name:
        return timezone_name

    for command in (
        ["timedatectl", "show", "--property=Timezone", "--value"],
        ["/usr/sbin/systemsetup", "-gettimezone"],
    ):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        command_value = result.stdout.strip().removeprefix("Time Zone:").strip()
        timezone_name = valid_timezone_name(command_value)
        if timezone_name:
            return timezone_name
    return None


def system_timezone() -> tzinfo:
    timezone_name = system_timezone_name()
    if timezone_name:
        return ZoneInfo(timezone_name)
    return datetime.now(UTC).astimezone().tzinfo or UTC
