import json
from collections.abc import Callable
from pathlib import Path
import re
import logging
import shutil
import subprocess
from dataclasses import dataclass
import threading
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from mediaforce.core.binaries import ffmpeg_binary, ffprobe_binary, remote_low_priority_shell
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import calibration_jobs, encode_jobs
from mediaforce.encoding.duration_estimate import host_identity_tokens
from mediaforce.hosts import tool_versions
from mediaforce.hosts.config import remote_shell_path_export_line
from mediaforce.hosts.transport import keep_awake_while_running
from mediaforce.hosts.types import HostSetupResult, HostStatus, VMAF_SELF_TEST_ARGS


def controller_versions() -> dict[str, str]:
    try:
        return tool_versions.running_versions(
            [ffmpeg_binary(), ffprobe_binary(), shutil.which("ab-av1") or "/opt/homebrew/bin/ab-av1"]
        )
    except subprocess.TimeoutExpired:
        return {}


def version_issue(target: dict[str, str], actual: dict[str, str]) -> str:
    differences = []
    for name in (*tool_versions.PROGRAMS, *tool_versions.LIBRARIES):
        wanted = target.get(name)
        if not wanted:
            return f"This Mac could not check its own {name} version."
        if wanted != actual.get(name):
            differences.append(f"{name} {actual.get(name) or '(missing)'} → {wanted}")
    return "Media tools need an update. " + ", ".join(differences) if differences else ""


def worker_probe_script() -> str:
    source = Path(__file__).with_name("tool_versions.py").read_text(encoding="utf-8")
    return f"""printf 'meta|managed_tools|%s\\n' "$(python3 - <<'MEDIAFORCE_PY'
{source}
MEDIAFORCE_PY
)" """


def controller_sources(target: dict[str, str]) -> dict[str, dict[str, str]]:
    prefix = Path(ffmpeg_binary()).resolve().parents[3]
    sources = {}
    for name in ("ffmpeg", *tool_versions.LIBRARIES, "ab-av1"):
        formula = "ffmpeg-full" if name == "ffmpeg" else name
        keg = (prefix.parent / "opt" / formula).resolve(strict=True)
        source_record = keg / "sbom.spdx.json"
        if not source_record.is_file():
            raise tool_versions.ToolUpdateError(f"Reinstall {formula} with Homebrew on this Mac to restore its source record, then choose Retry connection.")
        package = json.loads(source_record.read_text(encoding="utf-8"))["packages"][0]
        checksum = next((item["checksumValue"] for item in package["checksums"] if item["algorithm"] == "SHA256"), "")
        if (
            package["name"] != formula
            or package["versionInfo"] != target[name]
            or not re.fullmatch("[0-9a-f]{64}", checksum)
            or not package["downloadLocation"].startswith("https://")
        ):
            raise tool_versions.ToolUpdateError(f"This Mac could not confirm the source for {name} {target[name]}.")
        revision = json.loads((keg / "INSTALL_RECEIPT.json").read_text(encoding="utf-8"))["source"].get("tap_git_head")
        sources[name] = {
            "url": package["downloadLocation"],
            "sha256": checksum,
            "revision": revision if revision and re.fullmatch("[0-9a-f]{40}", revision) else "HEAD",
            "recipe": (keg / ".brew" / f"{formula}.rb").read_text(encoding="utf-8"),
        }
    return sources


def update_when_idle(*, busy: Callable[[], bool], execute: Callable[[], HostSetupResult]) -> HostSetupResult:
    if busy():
        return HostSetupResult(False, "Waiting for the current file to finish before updating media tools.")
    return execute()


def update_worker(
    host: dict[str, object],
    target: dict[str, str],
    run_remote_ssh: Callable[..., subprocess.CompletedProcess[str]],
    *,
    repair_video: bool = False,
) -> HostSetupResult:
    try:
        sources = controller_sources(target)
        directory = Path(__file__).parent
        arguments = [
            "python3",
            "-m",
            "mediaforce.hosts.worker_tools",
            json.dumps(target),
            json.dumps(sources),
            json.dumps(VMAF_SELF_TEST_ARGS),
            json.dumps(repair_video),
        ]
        lines = [
            remote_shell_path_export_line(),
            "set -eu",
            "scratch=$(mktemp -d)",
            """trap 'rm -rf "$scratch"' EXIT""",
            'cd "$scratch"',
            "mkdir -p mediaforce/hosts",
            "touch mediaforce/__init__.py mediaforce/hosts/__init__.py",
        ]
        for module in ("tool_versions.py", "worker_tools.py"):
            lines.extend(
                [
                    f"cat > mediaforce/hosts/{module} <<'MEDIAFORCE_PY'",
                    (directory / module).read_text(encoding="utf-8"),
                    "MEDIAFORCE_PY",
                ]
            )
        lines.append(remote_low_priority_shell(arguments))
        body = "\n".join(lines)
        result = run_remote_ssh(
            host, "sh", "-s", input_text=keep_awake_while_running(body), wake_before_connect=False, timeout=24 * 60 * 60
        )
    except tool_versions.ToolUpdateError as error:
        return HostSetupResult(False, str(error), failure_kind="update_failed")
    except (OSError, KeyError, ValueError, TypeError, IndexError, subprocess.SubprocessError):
        logging.getLogger(__name__).exception("Could not prepare or send the media tool update")
        return HostSetupResult(False, "This Mac could not prepare or send the media tool update.", failure_kind="update_failed")
    if result.returncode == 75:
        return HostSetupResult(False, "Waiting for the computer to finish its current work.")
    if result.returncode:
        logging.getLogger(__name__).warning("Worker tool update failed (%s): %s", result.returncode, result.stderr)
        reason = "The computer could not finish its media tool update."
        if result.returncode == 65:
            for line in result.stdout.splitlines():
                if line.startswith(tool_versions.ToolUpdateError.prefix):
                    reason = line.removeprefix(tool_versions.ToolUpdateError.prefix)
                    break
        return HostSetupResult(False, reason, failure_kind="update_failed")
    return HostSetupResult(True, "Media tools updated and checked.")


@dataclass
class _UpdateAttempt:
    target: dict[str, str]
    retry_at: float
    result: HostSetupResult


_UPDATE_LOCK = threading.RLock()
_UPDATE_RESULTS: dict[tuple[Path, str], _UpdateAttempt] = {}
_UPDATING = "Updating and checking media tools."


def host_is_busy(config: MediaforceConfig, host: dict[str, object]) -> bool:
    tokens = host_identity_tokens(host)
    with open_db(config.paths.db_path) as connection:
        for table in (calibration_jobs, encode_jobs):
            jobs = connection.execute(select(table.c.host_json).where(table.c.status.in_(("starting", "running")))).scalars()
            for value in jobs:
                assigned = json.loads(value or "{}")
                if tokens & host_identity_tokens(assigned):
                    return True
    return False


def update_result(config: MediaforceConfig, key: str, target: dict[str, str]) -> HostSetupResult | None:
    with _UPDATE_LOCK:
        record = _UPDATE_RESULTS.get((config.paths.db_path, key))
        if record and (record.result.failure_kind == "updating" or record.target == target):
            return record.result
    return None


def request_update(
    config: MediaforceConfig,
    host: dict[str, object],
    status: HostStatus,
    run_remote_ssh: Callable[..., subprocess.CompletedProcess[str]],
    *,
    requested: bool = False,
) -> HostSetupResult:
    key = (config.paths.db_path, status.key)
    target = status.tool_target

    def execute() -> HostSetupResult:
        return update_worker(host, target, run_remote_ssh, repair_video=status.repair_video_tools)

    def work() -> None:
        started = time.monotonic()
        try:
            result = update_when_idle(busy=lambda: host_is_busy(config, host), execute=execute)
        except (SQLAlchemyError, OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError):
            logging.getLogger(__name__).exception("Could not complete media tool setup for %s", status.key)
            result = HostSetupResult(False, "Mediaforce could not finish checking this computer.", failure_kind="update_failed")
        with _UPDATE_LOCK:
            finished = time.monotonic()
            delay = 30
            if result.failure_kind == "update_failed":
                repeated = previous and previous.target == target and previous.result.failure_kind == "update_failed"
                delay = max(3600 if repeated else 300, 2 * (finished - started))
            _UPDATE_RESULTS[key] = _UpdateAttempt(target, finished + delay, result)

    with _UPDATE_LOCK:
        previous = _UPDATE_RESULTS.get(key)
        if previous and (previous.result.failure_kind == "updating" or
                         (previous.target == target and not requested and time.monotonic() < previous.retry_at)):
            return previous.result
        updating = HostSetupResult(False, _UPDATING, failure_kind="updating")
        _UPDATE_RESULTS[key] = _UpdateAttempt(target, float("inf"), updating)
        threading.Thread(target=work, name="media-tool-update", daemon=True).start()
        return updating
