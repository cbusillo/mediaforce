import subprocess
import tempfile
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(slots=True)
class StructuredLLMFailure:
    attempts: list[str]

    @property
    def raw(self) -> str:
        return "\n".join(self.attempts)


def _trim_diagnostic(value: Any, *, limit: int = 1200) -> str:
    text = value.decode(errors="replace") if isinstance(value, bytes) else str(value or "")
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _attempt_diagnostic(attempt: int, message: str, detail: Any = None) -> str:
    detail_text = _trim_diagnostic(detail)
    if detail_text:
        return f"attempt {attempt}: {message}: {detail_text}"
    return f"attempt {attempt}: {message}"


def run_code_prompt(
        *,
        project_root: Path,
        prompt: str,
        max_seconds: int,
        advisor_model: str,
        memory_disabled_code_args: Callable[[], list[str]],
        advisor_response_factory: Callable[..., Any],
        subprocess_run: Callable[..., Any],
) -> Any:
    with tempfile.NamedTemporaryFile(prefix="mediaforce-advice-", suffix=".txt", delete=False) as handle:
        output_path = Path(handle.name)
    cmd = [
        "code",
        *memory_disabled_code_args(),
        "exec",
        "--model",
        advisor_model,
        "--sandbox",
        "danger-full-access",
        "--skip-git-repo-check",
        "--max-seconds",
        str(max_seconds),
        "--output-last-message",
        str(output_path),
        "-C",
        str(project_root),
        prompt,
    ]
    try:
        result = subprocess_run(cmd, capture_output=True, text=True, timeout=75)
    except FileNotFoundError as exc:
        return advisor_response_factory(ok=False, summary="`code` is required but not installed.", raw=str(exc))
    except (OSError, subprocess.SubprocessError) as exc:
        return advisor_response_factory(ok=False, summary="`code` failed to run.", raw=str(exc))

    try:
        raw = output_path.read_text().strip()
    finally:
        output_path.unlink(missing_ok=True)

    if result.returncode != 0 and not raw:
        message = result.stderr.strip() or result.stdout.strip() or "Unknown `code` failure"
        return advisor_response_factory(ok=False, summary="GPT recommendation failed.", raw=message)

    summary = raw.splitlines()[0] if raw else "No recommendation returned."
    return advisor_response_factory(ok=bool(raw), summary=summary, raw=raw or (result.stderr.strip() or result.stdout.strip()))


def run_structured_llm_request(
        *,
        project_root: Path,
        developer: str,
        message: str,
        schema: dict[str, Any],
        max_seconds: int,
        advisor_model: str,
        memory_disabled_code_args: Callable[[], list[str]],
        subprocess_run: Callable[..., Any],
        try_load_json: Callable[[str], Any],
        try_load_first_json_object: Callable[[str], Any],
) -> dict[str, Any] | StructuredLLMFailure | None:
    cmd = [
        "code",
        *memory_disabled_code_args(),
        "llm",
        "request",
        "--model",
        advisor_model,
        "--developer",
        developer,
        "--message",
        message,
        "--format-name",
        "mediaforce_tuning",
        "--schema-json",
        __import__("json").dumps(schema, sort_keys=True),
        "--format-strict",
    ]
    failures: list[str] = []
    for attempt in range(1, 3):
        try:
            result = subprocess_run(cmd, capture_output=True, text=True, timeout=max_seconds + 15, cwd=project_root)
        except subprocess.TimeoutExpired as exc:
            diagnostic = _trim_diagnostic(exc.stderr) or _trim_diagnostic(exc.stdout)
            failures.append(_attempt_diagnostic(attempt, f"timed out after {exc.timeout}s", diagnostic))
            continue
        except FileNotFoundError as exc:
            failures.append(_attempt_diagnostic(attempt, "`code` is required but was not found", exc))
            continue
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(_attempt_diagnostic(attempt, "`code llm request` failed to run", exc))
            continue
        raw = result.stdout.strip() or result.stderr.strip()
        if result.returncode != 0 or not raw:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            failures.append(_attempt_diagnostic(attempt, f"returned {result.returncode}", detail))
            continue
        parsed = try_load_json(raw)
        if parsed is None:
            parsed = try_load_first_json_object(raw)
        if isinstance(parsed, dict):
            return parsed
        failures.append(_attempt_diagnostic(attempt, "returned unparseable structured JSON", raw))
    return StructuredLLMFailure(failures) if failures else None


def run_multimodal_tune_request(
        *,
        project_root: Path,
        developer: str,
        message: str,
        images: list[str],
        schema: dict[str, Any],
        max_seconds: int,
        advisor_model: str,
        memory_disabled_code_args: Callable[[], list[str]],
        subprocess_run: Callable[..., Any],
        try_load_json: Callable[[str], Any],
        try_load_first_json_object: Callable[[str], Any],
) -> dict[str, Any] | None:
    with tempfile.NamedTemporaryFile(prefix="mediaforce-tune-", suffix=".txt", delete=False) as handle:
        output_path = Path(handle.name)
    with tempfile.NamedTemporaryFile(prefix="mediaforce-schema-", suffix=".json", delete=False, mode="w") as handle:
        json.dump(schema, handle, sort_keys=True)
        schema_path = Path(handle.name)
    cmd = [
        "code",
        *memory_disabled_code_args(),
        "exec",
        "--model",
        advisor_model,
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--max-seconds",
        str(max_seconds),
        "--output-last-message",
        str(output_path),
        "--output-schema",
        str(schema_path),
        "--demo",
        developer,
        "-C",
        str(project_root),
    ]
    for image_path in images:
        cmd.extend(["--image", image_path])
    cmd.append(message)
    for _attempt in range(2):
        try:
            result = subprocess_run(cmd, capture_output=True, text=True, timeout=max_seconds + 30)
        except (OSError, subprocess.SubprocessError):
            continue
        try:
            raw = output_path.read_text().strip()
        except OSError:
            raw = ""
        finally:
            output_path.unlink(missing_ok=True)
        if result.returncode != 0 and not raw:
            raw = result.stderr.strip() or result.stdout.strip()
        if not raw:
            continue
        parsed = try_load_json(raw)
        if parsed is None:
            parsed = try_load_first_json_object(raw)
        if isinstance(parsed, dict):
            schema_path.unlink(missing_ok=True)
            return parsed
    schema_path.unlink(missing_ok=True)
    return None
