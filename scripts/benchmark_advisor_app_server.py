import argparse
import importlib
import json
import os
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from unittest.mock import patch

from mediaforce import advisor
from mediaforce.advising.evals import (
    AdvisorEvalCase,
    public_advisor_response,
    recommended_eval_cases,
    score_advisor_eval_case,
)
from mediaforce.advising.privacy import redact_sensitive_text
from mediaforce.advising.routing import AdvisorTask, default_advisor_routing
from mediaforce.advising.runtime import (
    codex_lab_developer_instruction,
    run_codex_lab_process,
    run_codex_lab_attempt,
)


DEFAULT_CASE_ID = "terra-seed-size-first"
DEFAULT_APP_SERVER_URL = "ws://127.0.0.1:4766"


@dataclass(slots=True)
class StructuredRequestSpec:
    developer: str
    message: str
    schema: dict[str, Any]
    max_seconds: int
    task: AdvisorTask
    prompt_version: str
    evidence_references: list[str]


@dataclass(slots=True, frozen=True)
class ArtifactSnapshot:
    session_file_count: int
    session_bytes: int
    session_index_signature: tuple[int, int] | None
    history_signature: tuple[int, int] | None


@dataclass(slots=True)
class ArtifactDelta:
    session_file_delta: int
    session_byte_delta: int
    session_index_changed: bool
    history_changed: bool

    @property
    def persisted_session_changed(self) -> bool:
        return any(
            (
                self.session_file_delta,
                self.session_byte_delta,
                self.session_index_changed,
                self.history_changed,
            )
        )


@dataclass(slots=True)
class TransportRun:
    status: str
    latency_ms: int
    payload: dict[str, Any] | None = None
    usage: dict[str, int] = field(default_factory=dict)
    diagnostic: str = ""
    event_counts: dict[str, int] = field(default_factory=dict)
    mcp_status_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    server_requests: list[str] = field(default_factory=list)
    tool_items: list[dict[str, Any]] = field(default_factory=list)
    validation_events: list[dict[str, Any]] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    thread_ephemeral: bool | None = None
    thread_path_present: bool | None = None
    artifact_delta: ArtifactDelta | None = None


@dataclass(slots=True)
class BenchmarkResult:
    transport: str
    status: str
    latency_ms: int
    usage: dict[str, int]
    passed: bool
    checks: list[dict[str, Any]]
    response: dict[str, Any]
    diagnostic: str
    event_counts: dict[str, int]
    mcp_status_counts: dict[str, int]
    warnings: list[str]
    server_requests: list[str]
    tool_items: list[dict[str, Any]]
    validation_events: list[dict[str, Any]]
    timings_ms: dict[str, int]
    thread_ephemeral: bool | None
    thread_path_present: bool | None
    artifact_measurement: str
    artifact_delta: ArtifactDelta | None


class AppServerProtocolError(RuntimeError):
    pass


class _CapturedRequest(RuntimeError):
    def __init__(self, spec: StructuredRequestSpec) -> None:
        super().__init__("captured structured advisor request")
        self.spec = spec


@dataclass(slots=True)
class AppServerEvents:
    event_counts: Counter[str] = field(default_factory=Counter)
    mcp_status_counts: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    server_requests: list[str] = field(default_factory=list)
    tool_items: list[dict[str, Any]] = field(default_factory=list)
    validation_events: list[dict[str, Any]] = field(default_factory=list)
    agent_messages: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    turn_status: str | None = None
    turn_error: Any = None
    artifact_home: Path | None = None
    artifact_before: ArtifactSnapshot | None = None

    def handle_notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        if not method:
            return
        self.event_counts[method] += 1
        params = (
            message.get("params") if isinstance(message.get("params"), dict) else {}
        )
        if method == "mcpServer/startupStatus/updated":
            status = params.get("status")
            self.mcp_status_counts[
                status if isinstance(status, str) and status else "unknown"
            ] += 1
        elif method == "warning":
            warning = next(
                (
                    value
                    for value in (
                        params.get("message"),
                        params.get("warning"),
                        params.get("text"),
                    )
                    if isinstance(value, str) and value
                ),
                None,
            )
            if warning is not None:
                self.warnings.append(redact_sensitive_text(warning, limit=600))
        elif method == "item/completed":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            item_type = str(item.get("type") or "unknown")
            if item_type == "agentMessage":
                text = item.get("text")
                if isinstance(text, str):
                    self.agent_messages.append(text)
            elif item_type not in {"userMessage", "reasoning"}:
                self.tool_items.append(
                    {"type": item_type, "status": item.get("status")}
                )
        elif method == "thread/tokenUsage/updated":
            token_usage = (
                params.get("tokenUsage")
                if isinstance(params.get("tokenUsage"), dict)
                else {}
            )
            total = (
                token_usage.get("total")
                if isinstance(token_usage.get("total"), dict)
                else {}
            )
            self.usage = usage_from_camel_case(total)
        elif method == "validation/completed":
            self.validation_events.append(
                {
                    "status": params.get("status"),
                    "valid": params.get("valid"),
                    "has_error": bool(params.get("error")),
                }
            )
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            self.turn_status = str(turn.get("status") or "unknown")
            self.turn_error = turn.get("error")


class AppServerClient:
    def __init__(self, websocket: Any) -> None:
        self._websocket = websocket
        self._next_id = 1
        self.events = AppServerEvents()

    def request(
        self, method: str, params: dict[str, Any], *, deadline: float
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"method": method, "id": request_id, "params": params})
        while True:
            message = self._receive(deadline=deadline)
            if message.get("id") == request_id and "method" not in message:
                if message.get("error") is not None:
                    raise AppServerProtocolError(
                        f"{method} failed: {redact_sensitive_text(json.dumps(message['error']), limit=600)}"
                    )
                result = message.get("result")
                if not isinstance(result, dict):
                    raise AppServerProtocolError(
                        f"{method} returned a non-object result."
                    )
                return result
            self._handle_message(message)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def wait_for_turn(self, turn_id: str, *, deadline: float) -> None:
        while True:
            message = self._receive(deadline=deadline)
            self._handle_message(message)
            if message.get("method") != "turn/completed":
                continue
            params = (
                message.get("params") if isinstance(message.get("params"), dict) else {}
            )
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            if turn.get("id") == turn_id:
                return

    def interrupt(self, thread_id: str, turn_id: str) -> None:
        self._send(
            {
                "method": "turn/interrupt",
                "id": self._next_id,
                "params": {"threadId": thread_id, "turnId": turn_id},
            }
        )
        self._next_id += 1

    def _send(self, payload: dict[str, Any]) -> None:
        self._websocket.send(json.dumps(payload, separators=(",", ":")))

    def _receive(self, *, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for an app-server message.")
        payload = self._websocket.recv(timeout=remaining)
        if isinstance(payload, bytes):
            encoded_message = payload.decode()
        elif isinstance(payload, str):
            encoded_message = payload
        else:
            raise AppServerProtocolError(
                "App-server returned a non-text WebSocket message."
            )
        message = json.loads(encoded_message)
        if not isinstance(message, dict):
            raise AppServerProtocolError(
                "App-server returned a non-object JSON-RPC message."
            )
        return message

    def _handle_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        if message.get("id") is not None:
            self.events.server_requests.append(method)
            response = _server_request_response(method, message.get("params"))
            self._send({"id": message["id"], **response})
            return
        self.events.handle_notification(message)


def _server_request_response(method: str, params: object) -> dict[str, Any]:
    request_params = params if isinstance(params, dict) else {}
    if method == "item/permissions/requestApproval":
        return {"result": {"permissions": {}, "scope": "turn"}}
    if "requestApproval" in method:
        return {"result": {"decision": "decline"}}
    if method == "mcpServer/elicitation/request":
        return {
            "result": {"action": "decline", "content": None, "_meta": None}
        }
    if method == "item/tool/requestUserInput":
        questions = request_params.get("questions")
        if not isinstance(questions, list):
            questions = []
        answers = {
            question["id"]: {"answers": []}
            for question in questions
            if isinstance(question, dict)
            and isinstance(question.get("id"), str)
        }
        return {"result": {"answers": answers}}
    if method == "item/tool/call":
        return {
            "result": {
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": "Benchmark client does not execute dynamic tools.",
                    }
                ],
                "success": False,
            }
        }
    if method == "currentTime/read":
        return {"result": {"currentTimeAt": int(time.time())}}
    if method == "attestation/generate":
        return {
            "error": {
                "code": -32000,
                "message": "Benchmark client does not provide attestation.",
            }
        }
    if method == "account/chatgptAuthTokens/refresh":
        return {
            "error": {
                "code": -32000,
                "message": "Benchmark client cannot refresh externally managed auth tokens.",
            }
        }
    return {
        "error": {
            "code": -32601,
            "message": "Benchmark client does not support this request.",
        }
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare Mediaforce's current Codex Lab exec adapter with an existing app-server daemon."
    )
    parser.add_argument("--case", default=DEFAULT_CASE_ID)
    parser.add_argument("--model")
    parser.add_argument("--command", default="codex-lab")
    parser.add_argument(
        "--app-server-url",
        default=os.environ.get("CODEX_APP_SERVER_URL", DEFAULT_APP_SERVER_URL),
    )
    parser.add_argument(
        "--transport", choices=("both", "exec", "app-server"), default="both"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    payload = run_benchmark(
        project_root=Path.cwd(),
        case_id=args.case,
        model_override=args.model,
        command=args.command,
        app_server_url=args.app_server_url,
        transport=args.transport,
    )
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        output_path = args.output.expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if payload["summary"]["all_passed"] else 1


def run_benchmark(
    *,
    project_root: Path,
    case_id: str = DEFAULT_CASE_ID,
    model_override: str | None = None,
    command: str = "codex-lab",
    app_server_url: str = DEFAULT_APP_SERVER_URL,
    transport: str = "both",
    codex_home: Path | None = None,
    exec_runner: Callable[
        [StructuredRequestSpec, str, str, str | None, Path, Path], TransportRun
    ]
    | None = None,
    app_server_runner: Callable[[StructuredRequestSpec, str, str, Path], TransportRun]
    | None = None,
) -> dict[str, Any]:
    if transport not in {"both", "exec", "app-server"}:
        raise ValueError(f"Unsupported benchmark transport: {transport}")
    case = seed_eval_case(case_id)
    model = model_override or case.model
    if model is None:
        raise ValueError(f"Evaluation case {case_id!r} does not define a model.")
    spec = capture_seed_request(project_root=project_root, case=case)
    routing = default_advisor_routing()
    resolved_codex_home = (
        codex_home
        or Path(os.environ.get("CODEX_LAB_HOME", "~/.codex-lab")).expanduser()
    )
    results: list[BenchmarkResult] = []

    if transport in {"both", "app-server"}:
        results.append(
            _measure_transport(
                transport="app-server",
                project_root=project_root,
                case=case,
                measure_artifacts=True,
                runner=lambda: (app_server_runner or run_app_server_transport)(
                    spec,
                    model,
                    app_server_url,
                    project_root,
                ),
            )
        )
    if transport in {"both", "exec"}:
        results.append(
            _measure_transport(
                transport="exec",
                project_root=project_root,
                case=case,
                measure_artifacts=transport != "both",
                runner=lambda: (exec_runner or _run_exec_transport)(
                    spec,
                    model,
                    command,
                    routing.auth_profile,
                    project_root,
                    resolved_codex_home,
                ),
            )
        )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "case_id": case.case_id,
        "task": spec.task.value,
        "prompt_version": spec.prompt_version,
        "model": model,
        "transport_order": [result.transport for result in results],
        "transports": [asdict(result) for result in results],
        "comparison": _comparison(results),
        "summary": {
            "transport_count": len(results),
            "passed": sum(1 for result in results if result.passed),
            "all_passed": bool(results) and all(result.passed for result in results),
        },
    }


def _measure_transport(
    *,
    transport: str,
    project_root: Path,
    case: AdvisorEvalCase,
    measure_artifacts: bool,
    runner: Callable[[], TransportRun],
) -> BenchmarkResult:
    run = runner()
    artifact_delta = run.artifact_delta if measure_artifacts else None
    response = normalize_seed_response(
        case=case, project_root=project_root, payload=run.payload
    )
    telemetry = [
        {"valid": run.status == "success", "status": run.status, "usage": run.usage}
    ]
    checks = score_advisor_eval_case(case, response, telemetry)
    checks.append(
        _check(
            "transport:success",
            run.status == "success",
            run.diagnostic or run.status,
        )
    )
    if measure_artifacts:
        checks.append(
            _check(
                "artifacts:measured",
                artifact_delta is not None,
                "The transport's Codex home was unavailable for artifact measurement",
            )
        )
    if measure_artifacts and artifact_delta is not None:
        checks.append(
            _check(
                "artifacts:no_persisted_session",
                not artifact_delta.persisted_session_changed,
                "Codex Lab session or history artifacts changed during the request",
            )
        )
    if transport == "app-server":
        checks.extend(
            (
                _check(
                    "tools:not_used",
                    not run.tool_items,
                    f"received {len(run.tool_items)} tool items",
                ),
                _check(
                    "thread:ephemeral",
                    run.thread_ephemeral is True,
                    f"received {_optional_bool_text(run.thread_ephemeral)}",
                ),
                _check(
                    "thread:no_path",
                    run.thread_path_present is False,
                    f"path presence was {_optional_bool_text(run.thread_path_present)}",
                ),
            )
        )
    return BenchmarkResult(
        transport=transport,
        status=run.status,
        latency_ms=run.latency_ms,
        usage=run.usage,
        passed=all(check["passed"] for check in checks),
        checks=checks,
        response=response,
        diagnostic=run.diagnostic,
        event_counts=run.event_counts,
        mcp_status_counts=run.mcp_status_counts,
        warnings=run.warnings,
        server_requests=run.server_requests,
        tool_items=run.tool_items,
        validation_events=run.validation_events,
        timings_ms=run.timings_ms,
        thread_ephemeral=run.thread_ephemeral,
        thread_path_present=run.thread_path_present,
        artifact_measurement=(
            "isolated_transport" if measure_artifacts else "skipped_shared_home"
        ),
        artifact_delta=artifact_delta,
    )


def _run_exec_transport(
    spec: StructuredRequestSpec,
    model: str,
    command: str,
    auth_profile: str | None,
    project_root: Path,
    codex_home: Path,
) -> TransportRun:
    artifact_before = _artifact_snapshot(codex_home)
    attempt = run_codex_lab_attempt(
        project_root=project_root,
        developer=spec.developer,
        message=spec.message,
        images=[],
        schema=spec.schema,
        max_seconds=spec.max_seconds,
        command=command,
        auth_profile=auth_profile,
        model=model,
        subprocess_run=run_codex_lab_process,
        try_load_json=_try_load_json,
    )
    return TransportRun(
        status=attempt.status,
        latency_ms=attempt.latency_ms,
        payload=attempt.payload,
        usage=attempt.usage,
        diagnostic=attempt.diagnostic,
        artifact_delta=artifact_delta_between(
            artifact_before, _artifact_snapshot(codex_home)
        ),
    )


def run_app_server_transport(
    spec: StructuredRequestSpec,
    model: str,
    app_server_url: str,
    project_root: Path,
    connect_factory: Callable[..., Any] | None = None,
) -> TransportRun:
    del project_root
    started = time.monotonic()
    deadline = started + spec.max_seconds + 30
    client: AppServerClient | None = None
    timings: dict[str, int] = {}
    thread_ephemeral: bool | None = None
    thread_path_present: bool | None = None
    transport_errors: tuple[type[Exception], ...] = (
        AppServerProtocolError,
        OSError,
        ValueError,
    )
    resolved_connect = connect_factory
    if resolved_connect is None:
        try:
            client_module = importlib.import_module("websockets.sync.client")
            exceptions_module = importlib.import_module("websockets.exceptions")
            resolved_connect = getattr(client_module, "connect")
            websocket_exception = getattr(exceptions_module, "WebSocketException")
        except (ImportError, AttributeError):
            return TransportRun(
                status="command_unavailable",
                latency_ms=_elapsed_ms(started),
                diagnostic=(
                    "Run this benchmark with `uv run --with websockets python "
                    "scripts/benchmark_advisor_app_server.py`."
                ),
            )
        if isinstance(websocket_exception, type) and issubclass(
            websocket_exception, Exception
        ):
            transport_errors += (websocket_exception,)
    cleanup_errors = transport_errors + (TimeoutError,)
    try:
        with resolved_connect(
            app_server_url,
            open_timeout=min(10, spec.max_seconds),
            close_timeout=2,
            max_size=16 * 1024 * 1024,
        ) as websocket:
            client = AppServerClient(websocket)
            timings["connected"] = _elapsed_ms(started)
            initialize = client.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-backend",
                        "title": "Mediaforce Advisor Benchmark",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                deadline=deadline,
            )
            codex_home_value = initialize.get("codexHome")
            if not isinstance(codex_home_value, str) or not codex_home_value:
                raise AppServerProtocolError("initialize returned no codexHome.")
            artifact_home = Path(codex_home_value).expanduser()
            if not artifact_home.is_dir():
                raise AppServerProtocolError(
                    "initialize returned a Codex home that is not locally accessible."
                )
            client.events.artifact_home = artifact_home
            client.events.artifact_before = _artifact_snapshot(artifact_home)
            client.notify("initialized", {})
            timings["initialized"] = _elapsed_ms(started)
            with tempfile.TemporaryDirectory(
                prefix="mediaforce-app-server-benchmark-"
            ) as temp_dir:
                thread_result = client.request(
                    "thread/start",
                    {
                        "model": model,
                        "cwd": temp_dir,
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                        "baseInstructions": (
                            "You are a bounded structured-response engine. Do not use tools, inspect files, access "
                            "the network, or perform side effects. Return only the JSON object required by the "
                            "supplied schema."
                        ),
                        "developerInstructions": codex_lab_developer_instruction(
                            spec.developer
                        ),
                        "ephemeral": True,
                        "environments": [],
                        "runtimeWorkspaceRoots": [],
                        "dynamicTools": [],
                    },
                    deadline=deadline,
                )
                thread = (
                    thread_result.get("thread")
                    if isinstance(thread_result.get("thread"), dict)
                    else {}
                )
                thread_id = str(thread.get("id") or "")
                if not thread_id:
                    raise AppServerProtocolError("thread/start returned no thread id.")
                thread_ephemeral = (
                    thread.get("ephemeral")
                    if isinstance(thread.get("ephemeral"), bool)
                    else None
                )
                thread_path_present = thread.get("path") is not None
                timings["thread_started"] = _elapsed_ms(started)
                turn_result = client.request(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": spec.message.strip()}],
                        "model": model,
                        "environments": [],
                        "outputSchema": spec.schema,
                    },
                    deadline=deadline,
                )
                turn = (
                    turn_result.get("turn")
                    if isinstance(turn_result.get("turn"), dict)
                    else {}
                )
                turn_id = str(turn.get("id") or "")
                if not turn_id:
                    raise AppServerProtocolError("turn/start returned no turn id.")
                timings["turn_accepted"] = _elapsed_ms(started)
                try:
                    client.wait_for_turn(turn_id, deadline=deadline)
                except TimeoutError:
                    try:
                        client.interrupt(thread_id, turn_id)
                    except transport_errors:
                        pass
                    raise
                timings["turn_completed"] = _elapsed_ms(started)
                try:
                    client.request(
                        "thread/unsubscribe",
                        {"threadId": thread_id},
                        deadline=min(deadline, time.monotonic() + 2),
                    )
                except cleanup_errors:
                    pass
            events = client.events
            if events.turn_status != "completed":
                turn_status = events.turn_status or "unknown"
                return _app_server_result(
                    started,
                    events,
                    status="provider_error",
                    diagnostic=f"App-server turn ended with status {turn_status}.",
                    timings=timings,
                    thread_ephemeral=thread_ephemeral,
                    thread_path_present=thread_path_present,
                )
            if events.tool_items:
                return _app_server_result(
                    started,
                    events,
                    status="tool_use_rejected",
                    diagnostic="The app-server turn attempted to use an agent tool.",
                    timings=timings,
                    thread_ephemeral=thread_ephemeral,
                    thread_path_present=thread_path_present,
                )
            final_text = events.agent_messages[-1] if events.agent_messages else ""
            parsed = _try_load_json(final_text)
            if not isinstance(parsed, dict):
                return _app_server_result(
                    started,
                    events,
                    status="invalid_structured_output",
                    diagnostic=f"App-server returned {len(final_text)} bytes that did not parse as the requested object.",
                    timings=timings,
                    thread_ephemeral=thread_ephemeral,
                    thread_path_present=thread_path_present,
                )
            return _app_server_result(
                started,
                events,
                status="success",
                payload=parsed,
                timings=timings,
                thread_ephemeral=thread_ephemeral,
                thread_path_present=thread_path_present,
            )
    except TimeoutError:
        return _app_server_result(
            started,
            client.events if client is not None else AppServerEvents(),
            status="timeout",
            diagnostic=f"App-server timed out after {spec.max_seconds + 30}s.",
            timings=timings,
            thread_ephemeral=thread_ephemeral,
            thread_path_present=thread_path_present,
        )
    except transport_errors as exc:
        return _app_server_result(
            started,
            client.events if client is not None else AppServerEvents(),
            status="transport_error",
            diagnostic=redact_sensitive_text(str(exc), limit=800),
            timings=timings,
            thread_ephemeral=thread_ephemeral,
            thread_path_present=thread_path_present,
        )


def _app_server_result(
    started: float,
    events: AppServerEvents,
    *,
    status: str,
    timings: dict[str, int],
    thread_ephemeral: bool | None,
    thread_path_present: bool | None,
    payload: dict[str, Any] | None = None,
    diagnostic: str = "",
) -> TransportRun:
    return TransportRun(
        status=status,
        latency_ms=_elapsed_ms(started),
        payload=payload,
        usage=events.usage,
        diagnostic=diagnostic,
        event_counts=dict(events.event_counts),
        mcp_status_counts=dict(events.mcp_status_counts),
        warnings=events.warnings,
        server_requests=events.server_requests,
        tool_items=events.tool_items,
        validation_events=events.validation_events,
        timings_ms=timings,
        thread_ephemeral=thread_ephemeral,
        thread_path_present=thread_path_present,
        artifact_delta=_app_server_artifact_delta(events),
    )


def capture_seed_request(
    *, project_root: Path, case: AdvisorEvalCase
) -> StructuredRequestSpec:
    def capture(**kwargs: Any) -> Any:
        raise _CapturedRequest(
            StructuredRequestSpec(
                developer=str(kwargs["developer"]),
                message=str(kwargs["message"]),
                schema=dict(kwargs["schema"]),
                max_seconds=int(kwargs["max_seconds"]),
                task=kwargs["task"],
                prompt_version=str(kwargs["prompt_version"]),
                evidence_references=list(kwargs.get("evidence_references") or []),
            )
        )

    try:
        with patch(
            "mediaforce.advisor._run_structured_llm_request", side_effect=capture
        ):
            advisor.request_seed_policy(project_root=project_root, payload=case.payload)
    except _CapturedRequest as exc:
        return exc.spec
    raise RuntimeError(
        "Seed policy request did not invoke the structured advisor adapter."
    )


def normalize_seed_response(
    *,
    case: AdvisorEvalCase,
    project_root: Path,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if payload is None:
        return {}
    with patch("mediaforce.advisor._run_structured_llm_request", return_value=payload):
        response = advisor.request_seed_policy(
            project_root=project_root, payload=case.payload
        )
    return public_advisor_response(response)


def seed_eval_case(case_id: str) -> AdvisorEvalCase:
    for case in recommended_eval_cases():
        if case.case_id != case_id:
            continue
        if case.task != AdvisorTask.SEED_POLICY:
            raise ValueError(f"Evaluation case {case_id!r} is not a seed_policy case.")
        return case
    raise ValueError(f"Unknown advisor evaluation case: {case_id}")


def _artifact_snapshot(codex_home: Path) -> ArtifactSnapshot:
    sessions = codex_home / "sessions"
    session_file_count = 0
    session_bytes = 0
    if sessions.is_dir():
        for path in sessions.rglob("*"):
            if not path.is_file():
                continue
            session_file_count += 1
            try:
                session_bytes += path.stat().st_size
            except FileNotFoundError:
                continue
    return ArtifactSnapshot(
        session_file_count=session_file_count,
        session_bytes=session_bytes,
        session_index_signature=_file_signature(codex_home / "session_index.jsonl"),
        history_signature=_file_signature(codex_home / "history.jsonl"),
    )


def artifact_delta_between(
    before: ArtifactSnapshot, after: ArtifactSnapshot
) -> ArtifactDelta:
    return ArtifactDelta(
        session_file_delta=after.session_file_count - before.session_file_count,
        session_byte_delta=after.session_bytes - before.session_bytes,
        session_index_changed=after.session_index_signature
        != before.session_index_signature,
        history_changed=after.history_signature != before.history_signature,
    )


def _app_server_artifact_delta(events: AppServerEvents) -> ArtifactDelta | None:
    if events.artifact_home is None or events.artifact_before is None:
        return None
    return artifact_delta_between(
        events.artifact_before,
        _artifact_snapshot(events.artifact_home),
    )


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return None
    return stat_result.st_mtime_ns, stat_result.st_size


def _comparison(results: list[BenchmarkResult]) -> dict[str, Any] | None:
    by_transport = {result.transport: result for result in results}
    current = by_transport.get("exec")
    app_server = by_transport.get("app-server")
    if current is None or app_server is None:
        return None
    return {
        "latency_delta_ms": app_server.latency_ms - current.latency_ms,
        "latency_reduction_percent": _reduction_percent(
            current.latency_ms, app_server.latency_ms
        ),
        "input_token_delta": app_server.usage.get("input_tokens", 0)
        - current.usage.get("input_tokens", 0),
        "input_token_reduction_percent": _reduction_percent(
            current.usage.get("input_tokens", 0),
            app_server.usage.get("input_tokens", 0),
        ),
    }


def _reduction_percent(baseline: int, candidate: int) -> float | None:
    if baseline <= 0:
        return None
    return round(((baseline - candidate) / baseline) * 100, 2)


def _try_load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def usage_from_camel_case(payload: dict[str, Any]) -> dict[str, int]:
    mapping = {
        "inputTokens": "input_tokens",
        "cachedInputTokens": "cached_input_tokens",
        "outputTokens": "output_tokens",
        "reasoningOutputTokens": "reasoning_output_tokens",
    }
    return {
        target: max(0, int(payload.get(source) or 0))
        for source, target in mapping.items()
        if isinstance(payload.get(source), int | float)
    }


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _optional_bool_text(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "true" if value else "false"


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": "" if passed else detail}


if __name__ == "__main__":
    raise SystemExit(main())
