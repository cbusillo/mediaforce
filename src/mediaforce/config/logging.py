from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional


# Default logger names live under the mediaforce namespace so child loggers can
# inherit handlers/formatters without additional setup.
DEFAULT_COMPONENT = "mediaforce"


@dataclass
class LogConfig:
    """Runtime logging configuration.

    Attributes
    ----------
    level: str
        Log level name (INFO, DEBUG, WARNING, ERROR).
    component: str
        Logger namespace; child loggers inherit handlers automatically.
    json_path: Optional[pathlib.Path]
        When set, append newline-delimited JSON events to this file in addition
        to stdout.
    """

    level: str = "INFO"
    component: str = DEFAULT_COMPONENT
    json_path: Optional[pathlib.Path] = None


class _JsonFormatter(logging.Formatter):
    """Emit structured JSON with consistent keys for both stdout and files."""

    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401 - tiny formatter
        payload: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "component": self.component,
        }

        event = getattr(record, "event", None)
        message = record.getMessage()

        if event:
            payload["event"] = event
        if message and (not event or message != event):
            payload["message"] = message

        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)

        return json.dumps(payload, ensure_ascii=False)


def configure_logging(config: LogConfig) -> logging.Logger:
    """Configure and return a logger for the given component.

    The logger writes structured JSON to stdout and, when provided, to a JSONL
    file. Repeated calls replace handlers to avoid duplicate output.
    """

    logger = logging.getLogger(config.component)
    logger.setLevel(config.level.upper())
    logger.propagate = False
    logger.handlers.clear()

    formatter = _JsonFormatter(component=config.component)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    if config.json_path:
        path = pathlib.Path(config.json_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def env_log_config(component: str = DEFAULT_COMPONENT) -> LogConfig:
    """Build LogConfig from environment variables.

    MEDIAFORCE_LOG_LEVEL controls the level (default INFO).
    MEDIAFORCE_LOG_FILE enables JSONL output to that path.
    """

    level = os.getenv("MEDIAFORCE_LOG_LEVEL", "INFO")
    json_file = os.getenv("MEDIAFORCE_LOG_FILE")
    return LogConfig(
        level=level,
        component=component,
        json_path=pathlib.Path(json_file).expanduser() if json_file else None,
    )


def _human_enabled(logger: logging.Logger) -> bool:
    if not sys.stderr.isatty():
        return False

    raw = os.getenv("MEDIAFORCE_HUMAN")
    if raw is not None and raw.strip().lower() in {"0", "false", "no", "off"}:
        return False

    # Avoid noisy human output for the web server by default.
    return logger.name.endswith(".cli")


def _emit_human_line(line: str) -> None:
    try:
        sys.stderr.write(line.rstrip() + "\n")
        sys.stderr.flush()
    except Exception:
        return



def _human_basename(value: Any) -> Optional[str]:
    if not value:
        return None
    try:
        return pathlib.Path(str(value)).name
    except Exception:
        return None


def _human_fmt_float(value: Any) -> Optional[str]:
    try:
        return f"{float(value):.2f}"
    except Exception:
        return None


HUMAN_EVENT_FORMATTERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "queue_empty": lambda f: f"Queue: empty (limit={f.get('limit')})",
    "queue_listing": lambda f: (
        f"Queue: {f.get('count')} items (limit={f.get('limit')})"
        + (
            f" · top={_human_basename((f.get('items') or [{}])[0].get('path'))}"
            if isinstance(f.get("items"), list) and f.get("items")
            else ""
        )
    ),
    "verify_result": lambda f: (
        f"Verify: grade={f.get('grade')} acceptable={f.get('acceptable')}"
        + (
            f" vmaf={_human_fmt_float(f.get('vmaf'))}"
            if f.get("vmaf") is not None
            else ""
        )
    ),
    "verify_batch_summary": lambda f: (
        f"Verify batch: verified={f.get('verified')} failed={f.get('failed')} skipped={f.get('skipped')}"
    ),
    "review_list_empty": lambda _f: "Review: empty",
    "review_list": lambda f: f"Review: {f.get('count')} items (all={f.get('all')})",
    "review_approved": lambda f: f"Review: approved id={f.get('id')} file={_human_basename(f.get('source'))}",
    "review_rejected": lambda f: f"Review: rejected id={f.get('id')} file={_human_basename(f.get('source'))}",
    "compare_ready": lambda f: f"Compare ready: {f.get('html') or f.get('output') or f.get('clip_dir')}",
    "compare_video_ready": lambda f: f"Compare video ready: {f.get('output')}",
    "compare_video_failed": lambda _f: "Compare error: video generation failed",
    "missing_output_reset": lambda f: f"Reset: missing output for {_human_basename(f.get('source'))}",
    "verify_source_missing": lambda f: f"Verify error: missing source {_human_basename(f.get('source'))}",
    "verify_encoded_missing": lambda f: f"Verify error: missing encoded {_human_basename(f.get('encoded'))}",
    "review_db_missing": lambda f: f"Review error: missing DB {f.get('db')}",
    "review_encode_not_found": lambda f: f"Review error: encode not found id={f.get('id')}",
    "verify_batch_path_missing": lambda f: f"Verify batch error: missing path {f.get('path')}",
    "verify_batch_transcode_root_missing": lambda f: (
        f"Verify batch error: missing transcode root {f.get('transcode_root')}"
    ),
    "compare_source_missing": lambda f: f"Compare error: missing source {_human_basename(f.get('source'))}",
    "compare_encoded_missing": lambda f: f"Compare error: missing encoded {_human_basename(f.get('encoded'))}",
    "compare_ffmpeg_missing": lambda _f: "Compare error: ffmpeg not found",
    "compare_probe_failed": lambda f: f"Compare error: probe failed {_human_basename(f.get('source'))}",
    "compare_extract_failed": lambda _f: "Compare error: extract failed",
}


def _format_human(event: str, fields: dict[str, Any]) -> Optional[str]:
    fn = HUMAN_EVENT_FORMATTERS.get(event)
    if not fn:
        return None

    try:
        return fn(fields)
    except Exception:
        return event


def log_event(level: int, event: str, *, logger: Optional[logging.Logger] = None, **fields: Any) -> None:
    """Log a structured event with optional key/value fields."""

    target = logger or logging.getLogger(DEFAULT_COMPONENT)
    target.log(level, event, extra={"event": event, "fields": fields})

    if _human_enabled(target):
        line = _format_human(event, fields)
        if line:
            _emit_human_line(line)


def log_info(event: str, **fields: Any) -> None:
    log_event(logging.INFO, event, **fields)


def log_warn(event: str, **fields: Any) -> None:
    log_event(logging.WARNING, event, **fields)


def log_error(event: str, **fields: Any) -> None:
    log_event(logging.ERROR, event, **fields)
