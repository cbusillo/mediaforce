from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


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


def log_event(level: int, event: str, *, logger: Optional[logging.Logger] = None, **fields: Any) -> None:
    """Log a structured event with optional key/value fields."""

    target = logger or logging.getLogger(DEFAULT_COMPONENT)
    target.log(level, event, extra={"event": event, "fields": fields})


def log_info(event: str, **fields: Any) -> None:
    log_event(logging.INFO, event, **fields)


def log_warn(event: str, **fields: Any) -> None:
    log_event(logging.WARNING, event, **fields)


def log_error(event: str, **fields: Any) -> None:
    log_event(logging.ERROR, event, **fields)

