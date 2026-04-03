import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable


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
) -> dict[str, Any] | None:
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
    try:
        result = subprocess_run(cmd, capture_output=True, text=True, timeout=max_seconds + 15, cwd=project_root)
    except (OSError, subprocess.SubprocessError):
        return None
    raw = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0 or not raw:
        return None
    parsed = try_load_json(raw)
    if parsed is None:
        parsed = try_load_first_json_object(raw)
    return parsed if isinstance(parsed, dict) else None


def run_multimodal_tune_request(
        *,
        project_root: Path,
        developer: str,
        message: str,
        images: list[str],
        max_seconds: int,
        advisor_model: str,
        memory_disabled_code_args: Callable[[], list[str]],
        subprocess_run: Callable[..., Any],
        try_load_json: Callable[[str], Any],
        try_load_first_json_object: Callable[[str], Any],
) -> dict[str, Any] | None:
    with tempfile.NamedTemporaryFile(prefix="mediaforce-tune-", suffix=".txt", delete=False) as handle:
        output_path = Path(handle.name)
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
        "--demo",
        developer,
        "-C",
        str(project_root),
    ]
    for image_path in images:
        cmd.extend(["--image", image_path])
    cmd.append(message)
    try:
        result = subprocess_run(cmd, capture_output=True, text=True, timeout=max_seconds + 30)
    except (OSError, subprocess.SubprocessError):
        output_path.unlink(missing_ok=True)
        return None
    try:
        raw = output_path.read_text().strip()
    except OSError:
        raw = ""
    finally:
        output_path.unlink(missing_ok=True)
    if result.returncode != 0 and not raw:
        raw = result.stderr.strip() or result.stdout.strip()
    if not raw:
        return None
    parsed = try_load_json(raw)
    if parsed is None:
        parsed = try_load_first_json_object(raw)
    return parsed if isinstance(parsed, dict) else None
