from typing import Any


def _ffmpeg_command_with_progress(command: list[str], *, encode_progress_args: list[str]) -> list[str]:
    if command[:3] == [command[0], *encode_progress_args[:2]]:
        return command
    return [command[0], *encode_progress_args, *command[1:]]


def _update_ffmpeg_progress_state(
        progress_state: dict[str, str],
        line: str,
        *,
        elapsed_seconds: float,
) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    progress_state[key] = value
    if key != "progress":
        return None
    return {
        "fps": _progress_float(progress_state.get("fps")),
        "speed": _progress_speed(progress_state.get("speed")),
        "out_time_seconds": _progress_out_time_seconds(progress_state),
        "elapsed_seconds": max(elapsed_seconds, 0.0),
        "progress_state": value,
    }


def _progress_float(value: str | None) -> float | None:
    try:
        return float(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _progress_speed(value: str | None) -> float | None:
    text = str(value or "").strip().lower().rstrip("x")
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _progress_out_time_seconds(progress_state: dict[str, str]) -> float | None:
    raw_microseconds = progress_state.get("out_time_us") or progress_state.get("out_time_ms")
    if raw_microseconds:
        try:
            return float(raw_microseconds) / 1_000_000.0
        except (TypeError, ValueError):
            return None
    text = str(progress_state.get("out_time") or "").strip()
    if not text:
        return None
    try:
        hours, minutes, seconds = text.split(":", 2)
        return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)
    except (TypeError, ValueError):
        return None
