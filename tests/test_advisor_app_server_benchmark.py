import json
from pathlib import Path

from mediaforce.advising.routing import AdvisorTask
from scripts.benchmark_advisor_app_server import (
    AppServerClient,
    AppServerEvents,
    ArtifactDelta,
    ArtifactSnapshot,
    TransportRun,
    artifact_delta_between,
    capture_seed_request,
    normalize_seed_response,
    run_benchmark,
    run_app_server_transport,
    seed_eval_case,
    usage_from_camel_case,
    _server_request_response,
)


def test_capture_seed_request_reuses_existing_case_contract(tmp_path: Path) -> None:
    case = seed_eval_case("terra-seed-size-first")

    spec = capture_seed_request(project_root=tmp_path, case=case)

    assert spec.task == AdvisorTask.SEED_POLICY
    assert spec.max_seconds == 75
    assert spec.schema["additionalProperties"] is False
    assert "300 MB" in spec.message


def test_normalize_seed_response_applies_existing_policy_logic(tmp_path: Path) -> None:
    case = seed_eval_case("terra-seed-size-first")
    payload = _valid_seed_payload()

    response = normalize_seed_response(
        case=case, project_root=tmp_path, payload=payload
    )

    assert response["ok"] is True
    assert response["request_disposition"] == "honored"
    assert response["proposed_policy"]["video"]["target_size_bytes"] == 300_000_000


def test_app_server_events_capture_usage_without_exposing_mcp_names() -> None:
    events = AppServerEvents()

    events.handle_notification(
        {
            "method": "mcpServer/startupStatus/updated",
            "params": {"name": "private-server-name", "status": "ready"},
        }
    )
    events.handle_notification(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "tokenUsage": {
                    "total": {
                        "inputTokens": 120,
                        "cachedInputTokens": 20,
                        "outputTokens": 30,
                        "reasoningOutputTokens": 10,
                    }
                }
            },
        }
    )

    assert events.mcp_status_counts == {"ready": 1}
    assert events.usage == {
        "input_tokens": 120,
        "cached_input_tokens": 20,
        "output_tokens": 30,
        "reasoning_output_tokens": 10,
    }
    assert "private-server-name" not in json.dumps(events.mcp_status_counts)


def test_wait_for_turn_ignores_completion_for_another_turn() -> None:
    websocket = _FakeWebSocket(
        [
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "other", "status": "completed"}},
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "expected", "status": "completed"}},
            },
        ]
    )
    client = AppServerClient(websocket)

    client.wait_for_turn("expected", deadline=float("inf"))

    assert client.events.event_counts["turn/completed"] == 2
    assert websocket.received_count == 2


def test_wait_for_turn_accepts_completion_received_during_start_request() -> None:
    websocket = _FakeWebSocket(
        [
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "expected", "status": "completed"}},
            },
            {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "id": "other",
                        "status": "failed",
                        "error": {"message": "unrelated"},
                    }
                },
            },
            {"id": 1, "result": {"turn": {"id": "expected"}}},
        ]
    )
    client = AppServerClient(websocket)

    result = client.request("turn/start", {}, deadline=float("inf"))
    client.wait_for_turn("expected", deadline=float("inf"))

    assert result == {"turn": {"id": "expected"}}
    assert client.events.turn_status == "completed"
    assert client.events.turn_error is None
    assert websocket.received_count == 3


def test_server_request_responses_use_valid_safe_protocol_payloads() -> None:
    assert _server_request_response(
        "item/permissions/requestApproval", {}
    ) == {"result": {"permissions": {}, "scope": "turn"}}
    assert _server_request_response(
        "item/commandExecution/requestApproval", {}
    ) == {"result": {"decision": "decline"}}
    assert _server_request_response(
        "mcpServer/elicitation/request", {}
    ) == {"result": {"action": "decline", "content": None, "_meta": None}}
    assert _server_request_response(
        "item/tool/requestUserInput",
        {"questions": [{"id": "choice"}, {"id": 3}, "invalid"]},
    ) == {"result": {"answers": {"choice": {"answers": []}}}}
    assert _server_request_response("item/tool/call", {})["result"]["success"] is False
    assert isinstance(
        _server_request_response("currentTime/read", {})["result"]["currentTimeAt"],
        int,
    )
    assert _server_request_response("attestation/generate", {})["error"]["code"] == -32000
    assert (
        _server_request_response("account/chatgptAuthTokens/refresh", {})["error"][
            "code"
        ]
        == -32000
    )
    assert _server_request_response("unknown/request", {})["error"]["code"] == -32601


def test_app_server_timeout_interrupts_before_connection_closes(tmp_path: Path) -> None:
    case = seed_eval_case("terra-seed-size-first")
    spec = capture_seed_request(project_root=tmp_path, case=case)
    websocket = _ScriptedAppServerWebSocket(
        codex_home=tmp_path / "daemon-home",
        timeout_when_idle=True,
    )

    result = run_app_server_transport(
        spec,
        "gpt-5.6-terra",
        "ws://127.0.0.1:4766",
        tmp_path,
        connect_factory=lambda *_args, **_kwargs: websocket,
    )

    assert result.status == "timeout"
    assert "turn/interrupt" in websocket.sent_methods
    assert websocket.interrupt_sent_while_open is True
    assert websocket.closed is True


def test_app_server_snapshots_the_reported_codex_home(tmp_path: Path) -> None:
    case = seed_eval_case("terra-seed-size-first")
    spec = capture_seed_request(project_root=tmp_path, case=case)
    websocket = _ScriptedAppServerWebSocket(
        codex_home=tmp_path / "daemon-home",
        notifications=[
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "text": json.dumps(_valid_seed_payload()),
                    }
                },
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "completed"}},
            },
        ],
        create_session_artifact=True,
    )

    result = run_app_server_transport(
        spec,
        "gpt-5.6-terra",
        "ws://127.0.0.1:4766",
        tmp_path,
        connect_factory=lambda *_args, **_kwargs: websocket,
    )

    assert result.status == "success"
    assert result.artifact_delta is not None
    assert result.artifact_delta.session_file_delta == 1


def test_app_server_accepts_snake_case_agent_message(tmp_path: Path) -> None:
    result = _run_scripted_app_server(
        tmp_path,
        [
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(_valid_seed_payload()),
                    }
                },
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "completed"}},
            },
        ],
    )

    assert result.status == "success"
    assert result.tool_items == []


def test_app_server_reports_failed_turn_as_provider_error(tmp_path: Path) -> None:
    result = _run_scripted_app_server(
        tmp_path,
        [
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "failed"}},
            }
        ],
    )

    assert result.status == "provider_error"


def test_app_server_rejects_tool_use(tmp_path: Path) -> None:
    result = _run_scripted_app_server(
        tmp_path,
        [
            {
                "method": "item/completed",
                "params": {"item": {"type": "commandExecution", "status": "completed"}},
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "completed"}},
            },
        ],
    )

    assert result.status == "tool_use_rejected"
    assert result.tool_items == [{"type": "commandExecution", "status": "completed"}]


def test_app_server_rejects_invalid_structured_output(tmp_path: Path) -> None:
    result = _run_scripted_app_server(
        tmp_path,
        [
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": "not-json"}},
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "completed"}},
            },
        ],
    )

    assert result.status == "invalid_structured_output"


def test_artifact_delta_reports_persisted_session_changes() -> None:
    before = ArtifactSnapshot(2, 100, (1, 20), (1, 30))
    after = ArtifactSnapshot(3, 140, (2, 25), (1, 30))

    delta = artifact_delta_between(before, after)

    assert delta.session_file_delta == 1
    assert delta.session_byte_delta == 40
    assert delta.session_index_changed is True
    assert delta.history_changed is False
    assert delta.persisted_session_changed is True


def test_run_benchmark_compares_injected_transports_without_live_models(
    tmp_path: Path,
) -> None:
    raw_payload = _valid_seed_payload()
    call_order: list[str] = []

    def exec_runner(*_args: object) -> TransportRun:
        call_order.append("exec")
        return TransportRun(
            status="success",
            latency_ms=10_000,
            payload=raw_payload,
            usage={
                "input_tokens": 2_000,
                "cached_input_tokens": 0,
                "output_tokens": 200,
            },
            artifact_delta=_unchanged_artifact_delta(),
        )

    def app_server_runner(*_args: object) -> TransportRun:
        call_order.append("app-server")
        return TransportRun(
            status="success",
            latency_ms=4_000,
            payload=raw_payload,
            usage={
                "input_tokens": 1_000,
                "cached_input_tokens": 0,
                "output_tokens": 200,
            },
            thread_ephemeral=True,
            thread_path_present=False,
            artifact_delta=_unchanged_artifact_delta(),
        )

    report = run_benchmark(
        project_root=tmp_path,
        codex_home=tmp_path / ".codex-lab",
        exec_runner=exec_runner,
        app_server_runner=app_server_runner,
    )

    assert report["summary"] == {"transport_count": 2, "passed": 2, "all_passed": True}
    assert call_order == ["app-server", "exec"]
    assert report["transport_order"] == ["app-server", "exec"]
    app_server_result, exec_result = report["transports"]
    assert app_server_result["artifact_measurement"] == "isolated_transport"
    assert app_server_result["artifact_delta"] is not None
    assert exec_result["artifact_measurement"] == "skipped_shared_home"
    assert exec_result["artifact_delta"] is None
    assert report["comparison"]["latency_reduction_percent"] == 60.0
    assert report["comparison"]["input_token_reduction_percent"] == 50.0


def test_exec_only_run_keeps_independent_artifact_measurement(tmp_path: Path) -> None:
    def exec_runner(*_args: object) -> TransportRun:
        return TransportRun(
            status="success",
            latency_ms=10_000,
            payload=_valid_seed_payload(),
            usage={"input_tokens": 2_000},
            artifact_delta=_unchanged_artifact_delta(),
        )

    report = run_benchmark(
        project_root=tmp_path,
        transport="exec",
        codex_home=tmp_path / ".codex-lab",
        exec_runner=exec_runner,
    )

    result = report["transports"][0]
    assert result["artifact_measurement"] == "isolated_transport"
    assert result["artifact_delta"] is not None


def test_usage_conversion_ignores_non_numeric_values() -> None:
    assert usage_from_camel_case(
        {
            "inputTokens": 10,
            "cachedInputTokens": "unknown",
            "outputTokens": 2.8,
            "reasoningOutputTokens": None,
        }
    ) == {"input_tokens": 10, "output_tokens": 2}


def _valid_seed_payload() -> dict[str, object]:
    return {
        "request_response": "Prepared the requested measured size-first draft.",
        "request_disposition": "honored",
        "summary": "Prepared a conservative 300 MB first test.",
        "diagnosis": "The explicit request can be measured safely.",
        "confidence": "medium",
        "evidence_checked": ["operator_note", "requested_experiment"],
        "suggested_follow_up": "Run sampled calibration before queueing.",
        "feasibility_note": None,
        "policy": {"video": {"target_size_bytes": 300_000_000}},
    }


def _run_scripted_app_server(
    tmp_path: Path,
    notifications: list[dict[str, object]],
) -> TransportRun:
    case = seed_eval_case("terra-seed-size-first")
    spec = capture_seed_request(project_root=tmp_path, case=case)
    websocket = _ScriptedAppServerWebSocket(
        codex_home=tmp_path / "daemon-home",
        notifications=notifications,
    )
    return run_app_server_transport(
        spec,
        "gpt-5.6-terra",
        "ws://127.0.0.1:4766",
        tmp_path,
        connect_factory=lambda *_args, **_kwargs: websocket,
    )


class _FakeWebSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self._messages = [json.dumps(message) for message in messages]
        self.received_count = 0

    def send(self, _payload: str) -> None:
        pass

    def recv(self, *, timeout: float) -> str:
        del timeout
        self.received_count += 1
        if not self._messages:
            raise TimeoutError("synthetic timeout")
        return self._messages.pop(0)


class _ScriptedAppServerWebSocket:
    def __init__(
        self,
        *,
        codex_home: Path,
        notifications: list[dict[str, object]] | None = None,
        timeout_when_idle: bool = False,
        create_session_artifact: bool = False,
    ) -> None:
        codex_home.mkdir(parents=True, exist_ok=True)
        self._codex_home = codex_home
        self._responses: list[str] = []
        self._notifications = notifications or []
        self._timeout_when_idle = timeout_when_idle
        self._create_session_artifact = create_session_artifact
        self.sent_methods: list[str] = []
        self.interrupt_sent_while_open = False
        self.closed = False

    def __enter__(self) -> "_ScriptedAppServerWebSocket":
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def send(self, payload: str) -> None:
        message = json.loads(payload)
        method = message.get("method")
        if isinstance(method, str):
            self.sent_methods.append(method)
        if method == "initialize":
            self._responses.append(
                json.dumps(
                    {
                        "id": message["id"],
                        "result": {"codexHome": str(self._codex_home)},
                    }
                )
            )
        elif method == "thread/start":
            if self._create_session_artifact:
                sessions = self._codex_home / "sessions"
                sessions.mkdir(exist_ok=True)
                (sessions / "app-server-created.jsonl").write_text("artifact\n")
            self._responses.append(
                json.dumps(
                    {
                        "id": message["id"],
                        "result": {
                            "thread": {
                                "id": "thread-1",
                                "ephemeral": True,
                                "path": None,
                            }
                        },
                    }
                )
            )
        elif method == "turn/start":
            self._responses.append(
                json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-1"}}})
            )
            self._responses.extend(
                json.dumps(notification) for notification in self._notifications
            )
        elif method == "turn/interrupt":
            self.interrupt_sent_while_open = not self.closed
        elif method == "thread/unsubscribe":
            self._responses.append(
                json.dumps({"id": message["id"], "result": {"status": "unsubscribed"}})
            )

    def recv(self, *, timeout: float) -> str:
        del timeout
        if self._responses:
            return self._responses.pop(0)
        if self._timeout_when_idle:
            raise TimeoutError("synthetic timeout")
        raise AssertionError("scripted app server ran out of messages")


def _unchanged_artifact_delta() -> ArtifactDelta:
    return ArtifactDelta(
        session_file_delta=0,
        session_byte_delta=0,
        session_index_changed=False,
        history_changed=False,
    )
