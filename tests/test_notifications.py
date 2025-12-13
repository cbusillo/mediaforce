from __future__ import annotations

from mediaforce.services.notifications import (
    NotificationConfig,
    build_notification_payload,
    send_notifications,
)


def test_build_payload_includes_slack_and_discord_fields():
    payload = build_notification_payload(event="encode_completed", summary="hello", data={"a": 1})
    assert payload["event"] == "encode_completed"
    assert payload["summary"] == "hello"
    assert payload["text"] == "hello"
    assert payload["content"] == "hello"
    assert payload["data"]["a"] == 1


def test_send_notifications_calls_each_url():
    called: list[str] = []

    def fake_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
        assert body
        assert headers.get("Content-Type") == "application/json"
        assert timeout == 1.0
        called.append(url)

    cfg = NotificationConfig(urls=("https://example.com/a", "https://example.com/b"), timeout_seconds=1.0)
    result = send_notifications(
        event="encode_completed",
        summary="ok",
        data={"x": 1},
        config=cfg,
        http_post=fake_post,
    )
    assert result == {"sent": 2, "failed": 0}
    assert called == ["https://example.com/a", "https://example.com/b"]


def test_send_notifications_disabled_returns_zero():
    cfg = NotificationConfig(urls=(), timeout_seconds=1.0)
    result = send_notifications(event="x", summary="y", data={}, config=cfg)
    assert result == {"sent": 0, "failed": 0}

