import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypedDict

from mediaforce.config.logging import log_event


class NotificationPayload(TypedDict):
    ts: str
    event: str
    summary: str
    data: dict[str, Any]
    text: str
    content: str


class NotificationResult(TypedDict):
    sent: int
    failed: int


HttpPost = Callable[[str, bytes, dict[str, str], float], None]


@dataclass(frozen=True)
class NotificationConfig:
    urls: tuple[str, ...]
    timeout_seconds: float = 3.0

    @property
    def enabled(self) -> bool:
        return bool(self.urls)


def _default_http_post(url: str, body: bytes, headers: dict[str, str], timeout_seconds: float) -> None:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_seconds):
        return


def load_notification_config() -> NotificationConfig:
    raw_single = os.getenv("MEDIAFORCE_NOTIFY_WEBHOOK_URL", "").strip()
    raw_list = os.getenv("MEDIAFORCE_NOTIFY_WEBHOOK_URLS", "").strip()

    urls: list[str] = []
    if raw_single:
        urls.append(raw_single)
    if raw_list:
        urls.extend([u.strip() for u in raw_list.split(",") if u.strip()])

    try:
        timeout = float(os.getenv("MEDIAFORCE_NOTIFY_TIMEOUT_SECONDS", "3").strip())
        if timeout <= 0:
            timeout = 3.0
    except Exception:
        timeout = 3.0

    seen: set[str] = set()
    uniq: list[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)

    return NotificationConfig(urls=tuple(uniq), timeout_seconds=timeout)


def build_notification_payload(
    *,
    event: str,
    summary: str,
    data: dict[str, Any],
) -> NotificationPayload:
    ts = datetime.now(timezone.utc).isoformat()
    payload: NotificationPayload = {
        "ts": ts,
        "event": event,
        "summary": summary,
        "data": data,
        "text": summary,
        "content": summary,
    }
    return payload


def send_webhook(
    *,
    url: str,
    payload: NotificationPayload,
    timeout_seconds: float,
    http_post: HttpPost = _default_http_post,
    logger: Optional[logging.Logger] = None,
) -> bool:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "mediaforce-webhook/1.0",
    }

    try:
        http_post(url, body, headers, timeout_seconds)
        log_event(logging.INFO, "notify_webhook_ok", logger=logger, url=url, notify_event=str(payload.get("event")))
        return True
    except urllib.error.HTTPError as exc:
        log_event(
            logging.WARNING,
            "notify_webhook_http_error",
            logger=logger,
            url=url,
            status=getattr(exc, "code", None),
            error=str(exc),
        )
        return False
    except Exception as exc:
        log_event(logging.WARNING, "notify_webhook_failed", logger=logger, url=url, error=str(exc))
        return False


def send_notifications(
    *,
    event: str,
    summary: str,
    data: dict[str, Any],
    config: Optional[NotificationConfig] = None,
    http_post: HttpPost = _default_http_post,
    logger: Optional[logging.Logger] = None,
) -> NotificationResult:
    cfg = config or load_notification_config()
    if not cfg.enabled:
        return {"sent": 0, "failed": 0}

    payload = build_notification_payload(event=event, summary=summary, data=data)
    sent = 0
    failed = 0
    for url in cfg.urls:
        ok = send_webhook(url=url, payload=payload, timeout_seconds=cfg.timeout_seconds, http_post=http_post, logger=logger)
        if ok:
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed}
