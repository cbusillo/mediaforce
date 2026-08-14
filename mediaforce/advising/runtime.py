import json
import os
import signal
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from mediaforce.advising.failures import ASSISTANT_RETRYABLE_FAILURE_CODES
from mediaforce.advising.privacy import redact_sensitive_text
from mediaforce.advising.routing import AdvisorRouting, AdvisorTask, default_advisor_routing
from mediaforce.advising.telemetry import append_advisor_telemetry, estimated_cost_usd


_FALLBACK_STATUSES = ASSISTANT_RETRYABLE_FAILURE_CODES
_CODEX_LAB_SKILL_TRUNCATION_NOTICE_PREFIX = "Skill descriptions were shortened to fit the skills context budget."


@dataclass(slots=True)
class StructuredLLMFailure:
    attempts: list[str]
    statuses: list[str] = field(default_factory=list)

    @property
    def raw(self) -> str:
        return "\n".join(self.attempts)


@dataclass(slots=True)
class _CodexLabAttempt:
    status: str
    text: str
    payload: dict[str, Any] | None
    diagnostic: str
    latency_ms: int
    usage: dict[str, int]


def run_codex_lab_process(
        cmd: list[str],
        *,
        input: str,
        capture_output: bool,
        text: bool,
        timeout: float,
        cwd: Path,
) -> subprocess.CompletedProcess[str]:
    if not capture_output or not text:
        raise ValueError("Codex Lab execution requires captured text output.")
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = _terminate_process_group(process)
        raise subprocess.TimeoutExpired(
            cmd=cmd,
            timeout=timeout,
            output=stdout,
            stderr=stderr,
        ) from exc
    except BaseException:
        _terminate_process_group(process)
        raise
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _terminate_process_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.communicate()


def _trim_diagnostic(value: Any, *, limit: int = 1200) -> str:
    text = value.decode(errors="replace") if isinstance(value, bytes) else str(value or "")
    return redact_sensitive_text(text.strip(), limit=limit)


def _attempt_diagnostic(attempt: int, message: str, detail: Any = None) -> str:
    detail_text = _trim_diagnostic(detail)
    if detail_text:
        return f"attempt {attempt}: {message}: {detail_text}"
    return f"attempt {attempt}: {message}"


def run_structured_llm_request(
        *,
        project_root: Path,
        developer: str,
        message: str,
        schema: dict[str, Any],
        max_seconds: int,
        task: AdvisorTask,
        prompt_version: str,
        routing: AdvisorRouting | None,
        evidence_references: list[str],
        subprocess_run: Callable[..., Any],
        try_load_json: Callable[[str], Any],
        images: list[str] | None = None,
) -> dict[str, Any] | StructuredLLMFailure | None:
    resolved_routing = routing or default_advisor_routing()
    request_id = uuid.uuid4().hex
    failures: list[str] = []
    statuses: list[str] = []
    fallback_reason: str | None = None
    resolved_images = [str(Path(path).expanduser().resolve()) for path in images or []]
    route = resolved_routing.route_for(task)
    for route_index, model in enumerate(route.models):
        attempt = _run_codex_lab_attempt(
            project_root=project_root,
            developer=developer,
            message=message,
            images=resolved_images,
            schema=schema,
            max_seconds=max_seconds,
            command=resolved_routing.command,
            auth_profile=resolved_routing.auth_profile,
            model=model,
            subprocess_run=subprocess_run,
            try_load_json=try_load_json,
        )
        _record_attempt(
            routing=resolved_routing,
            request_id=request_id,
            task=task,
            prompt_version=prompt_version,
            model=model,
            route_index=route_index,
            fallback_reason=fallback_reason,
            attempt=attempt,
            input_characters=len(developer) + len(message),
            image_count=len(resolved_images),
            evidence_references=evidence_references,
        )
        if attempt.status == "success" and attempt.payload is not None:
            return attempt.payload
        failures.append(_attempt_diagnostic(route_index + 1, attempt.status, attempt.diagnostic))
        statuses.append(attempt.status)
        fallback_reason = attempt.status
        if attempt.status not in _FALLBACK_STATUSES:
            break
    return StructuredLLMFailure(failures, statuses=statuses) if failures else None


def run_multimodal_tune_request(
        *,
        project_root: Path,
        developer: str,
        message: str,
        images: list[str],
        schema: dict[str, Any],
        max_seconds: int,
        task: AdvisorTask,
        prompt_version: str,
        routing: AdvisorRouting | None,
        evidence_references: list[str],
        subprocess_run: Callable[..., Any],
        try_load_json: Callable[[str], Any],
) -> dict[str, Any] | StructuredLLMFailure | None:
    return run_structured_llm_request(
        project_root=project_root,
        developer=developer,
        message=message,
        schema=schema,
        max_seconds=max_seconds,
        task=task,
        prompt_version=prompt_version,
        routing=routing,
        evidence_references=evidence_references,
        subprocess_run=subprocess_run,
        try_load_json=try_load_json,
        images=images,
    )


def _run_codex_lab_attempt(
        *,
        project_root: Path,
        developer: str,
        message: str,
        images: list[str],
        schema: dict[str, Any] | None,
        max_seconds: int,
        command: str,
        auth_profile: str | None,
        model: str,
        subprocess_run: Callable[..., Any],
        try_load_json: Callable[[str], Any],
) -> _CodexLabAttempt:
    del project_root
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mediaforce-advisor-") as temp_dir:
        work_dir = Path(temp_dir)
        cmd = [
            command,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--json",
            "--model",
            model,
            "-C",
            str(work_dir),
        ]
        if auth_profile:
            cmd.extend(["--auth-profile", auth_profile])
        if schema is not None:
            schema_path = work_dir / "response-schema.json"
            schema_path.write_text(json.dumps(schema, separators=(",", ":"), sort_keys=True))
            cmd.extend(["--output-schema", str(schema_path)])
        for image_index, image_path in enumerate(images, start=1):
            source_image = Path(image_path)
            if not source_image.is_file():
                return _attempt_result(
                    started,
                    status="missing_image",
                    diagnostic=f"review image was unavailable: {image_path}",
                )
            suffix = source_image.suffix.lower()
            if suffix not in {".jpeg", ".jpg", ".png", ".webp"}:
                return _attempt_result(
                    started,
                    status="unsupported_image",
                    diagnostic=f"review image format was unsupported: {suffix or 'unknown'}",
                )
            copied_image = work_dir / f"review-image-{image_index}{suffix}"
            shutil.copyfile(source_image, copied_image)
            cmd.extend(["--image", str(copied_image)])
        cmd.extend(["--color", "never"])
        cmd.extend(["-c", f"developer_instructions={json.dumps(_codex_lab_developer_instruction(developer))}"])
        cmd.append("-")
        try:
            result = subprocess_run(
                cmd,
                input=message.strip(),
                capture_output=True,
                text=True,
                timeout=max_seconds + 30,
                cwd=work_dir,
            )
        except subprocess.TimeoutExpired as exc:
            return _attempt_result(
                started,
                status="timeout",
                diagnostic=f"Codex Lab timed out after {exc.timeout}s.",
            )
        except FileNotFoundError:
            return _attempt_result(
                started,
                status="command_unavailable",
                diagnostic="The configured Codex Lab command was unavailable.",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _attempt_result(
                started,
                status="transport_error",
                diagnostic=f"Codex Lab transport failed with {exc.__class__.__name__}.",
            )
    stdout = str(result.stdout or "").strip()
    if result.returncode != 0:
        return _attempt_result(
            started,
            status="provider_error",
            diagnostic=f"Codex Lab returned exit code {result.returncode}.",
        )
    final_text, usage, tool_used, completed = _codex_lab_output(stdout)
    if not completed:
        return _attempt_result(
            started,
            status="incomplete_turn",
            diagnostic="Codex Lab ended without a terminal turn.completed event.",
            usage=usage,
        )
    if tool_used:
        return _attempt_result(
            started,
            status="tool_use_rejected",
            diagnostic="The model attempted to use an agent tool in a side-channel request.",
            usage=usage,
        )
    if not final_text:
        return _attempt_result(
            started,
            status="empty_response",
            diagnostic="Codex Lab returned no final agent message.",
            usage=usage,
        )
    if schema is None:
        return _attempt_result(started, status="success", text=final_text, usage=usage)
    parsed = try_load_json(final_text)
    if not isinstance(parsed, dict):
        return _attempt_result(
            started,
            status="invalid_structured_output",
            diagnostic=f"Codex Lab returned {len(final_text)} bytes that did not parse as the requested object.",
            usage=usage,
        )
    return _attempt_result(started, status="success", text=final_text, payload=parsed, usage=usage)


def _attempt_result(
        started: float,
        *,
        status: str,
        text: str = "",
        payload: dict[str, Any] | None = None,
        diagnostic: Any = "",
        usage: Mapping[str, Any] | None = None,
) -> _CodexLabAttempt:
    return _CodexLabAttempt(
        status=status,
        text=text,
        payload=payload,
        diagnostic=_trim_diagnostic(diagnostic),
        latency_ms=max(0, int(round((time.monotonic() - started) * 1000))),
        usage={
            key: max(0, int(value))
            for key, value in (usage or {}).items()
            if key in {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}
            and isinstance(value, int | float)
        },
    )


def _codex_lab_developer_instruction(developer: str) -> str:
    return (
        "Do not use tools, inspect files, or infer facts outside the supplied user content. "
        f"{developer.strip()}"
    )


def _codex_lab_output(raw: str) -> tuple[str, dict[str, int], bool, bool]:
    final_text = ""
    usage: dict[str, int] = {}
    parsed_event = False
    tool_used = False
    completed = False
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            continue
        parsed_event = True
        if event["type"] == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "agent_message":
                final_text = str(item.get("text") or "").strip()
            elif item_type == "error":
                message = str(item.get("message") or "")
                if not message.startswith(_CODEX_LAB_SKILL_TRUNCATION_NOTICE_PREFIX):
                    tool_used = True
            elif item_type != "reasoning":
                tool_used = True
        elif event["type"] == "turn.completed" and isinstance(event.get("usage"), dict):
            completed = True
            usage = {
                key: max(0, int(value))
                for key, value in event["usage"].items()
                if key in {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}
                and isinstance(value, int | float)
            }
    if not parsed_event:
        return "", {}, False, False
    return final_text, usage, tool_used, completed


def _record_attempt(
        *,
        routing: AdvisorRouting,
        request_id: str,
        task: AdvisorTask,
        prompt_version: str,
        model: str,
        route_index: int,
        fallback_reason: str | None,
        attempt: _CodexLabAttempt,
        input_characters: int,
        image_count: int,
        evidence_references: list[str],
) -> None:
    try:
        append_advisor_telemetry(
            routing.telemetry_path,
            {
                "schema_version": 1,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "request_id": request_id,
                "task": task.value,
                "prompt_version": prompt_version,
                "model": model,
                "route_index": route_index,
                "fallback_reason": fallback_reason,
                "status": attempt.status,
                "valid": attempt.status == "success",
                "latency_ms": attempt.latency_ms,
                "usage": attempt.usage,
                "estimated_cost_usd": estimated_cost_usd(attempt.usage, routing.pricing_for(model)),
                "input_characters": max(0, input_characters),
                "image_count": max(0, image_count),
                "evidence_references": sorted(set(evidence_references))[:64],
            },
            max_records=routing.telemetry_max_records,
        )
    except OSError:
        return
