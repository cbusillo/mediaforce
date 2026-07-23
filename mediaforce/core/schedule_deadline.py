import shlex
import signal
import subprocess
from datetime import UTC, datetime
from typing import Any, Mapping


SCHEDULE_CLOSE_DEADLINE_KEY = "schedule_close_deadline_at"
SCHEDULE_DEADLINE_EXIT_CODE = 124
SCHEDULE_DEADLINE_MARKER = "__MEDIAFORCE_SCHEDULE_DEADLINE__"
SCHEDULE_DEADLINE_FALLBACK_RETURN_CODES = {
    -signal.SIGKILL,
    -signal.SIGTERM,
    128 + signal.SIGKILL,
    128 + signal.SIGTERM,
    255,
}


class ScheduleDeadlineConfigurationError(RuntimeError):
    pass


def parse_schedule_close_deadline(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid schedule close deadline: {text}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Schedule close deadline must include a timezone: {text}")
    return parsed.astimezone(UTC)


def schedule_close_deadline_for_host(host: Mapping[str, Any] | None) -> datetime | None:
    if not isinstance(host, Mapping):
        return None
    return parse_schedule_close_deadline(host.get(SCHEDULE_CLOSE_DEADLINE_KEY))


def guard_command_for_schedule_deadline(
        command: list[str],
        host: Mapping[str, Any] | None,
        *,
        process_controller: Any,
) -> list[str]:
    deadline = managed_schedule_close_deadline(host, process_controller)
    if deadline is None:
        return command
    return ["sh", "-c", guard_shell_script_for_schedule_deadline(shlex.join(command), deadline)]


def managed_schedule_close_deadline(
        host: Mapping[str, Any] | None,
        process_controller: Any,
) -> datetime | None:
    deadline = schedule_close_deadline_for_host(host)
    if deadline is not None and process_controller is None:
        raise ScheduleDeadlineConfigurationError(
            "Schedule deadline enforcement requires a managed process controller."
        )
    return deadline


def guard_shell_script_for_schedule_deadline(script: str, deadline: datetime) -> str:
    deadline_epoch = int(deadline.astimezone(UTC).timestamp())
    indented_script = "\n".join(f"    {line}" for line in script.splitlines())
    return f"""mediaforce_deadline={deadline_epoch}
mediaforce_deadline_marker="${{TMPDIR:-/tmp}}/.mediaforce-deadline-$$-{deadline_epoch}"
mediaforce_now="$(date -u +%s)" || exit 125
if [ "$mediaforce_now" -ge "$mediaforce_deadline" ]; then
    printf '%s\\n' '{SCHEDULE_DEADLINE_MARKER}' >&2
    exit {SCHEDULE_DEADLINE_EXIT_CODE}
fi
rm -f "$mediaforce_deadline_marker"
mediaforce_expire_deadline() {{
    : > "$mediaforce_deadline_marker"
    kill -TERM 0 2>/dev/null || true
    sleep 2
    kill -KILL 0 2>/dev/null || true
}}
(
    trap '' TERM HUP INT
    while :; do
        mediaforce_now="$(date -u +%s)" || {{
            mediaforce_expire_deadline
            exit 125
        }}
        if [ "$mediaforce_now" -ge "$mediaforce_deadline" ]; then
            break
        fi
        sleep 0.1
    done
    mediaforce_expire_deadline
) </dev/null >/dev/null 2>&1 &
mediaforce_watchdog_pid=$!
trap 'if [ ! -f "$mediaforce_deadline_marker" ]; then kill -KILL "$mediaforce_watchdog_pid" 2>/dev/null || true; wait "$mediaforce_watchdog_pid" 2>/dev/null || true; fi; rm -f "$mediaforce_deadline_marker"' 0
trap ':' TERM
(
{indented_script}
)
mediaforce_status=$?
mediaforce_now="$(date -u +%s)" || : > "$mediaforce_deadline_marker"
if [ -n "$mediaforce_now" ] && [ "$mediaforce_now" -ge "$mediaforce_deadline" ]; then
    : > "$mediaforce_deadline_marker"
fi
if [ -f "$mediaforce_deadline_marker" ]; then
    printf '%s\\n' '{SCHEDULE_DEADLINE_MARKER}' >&2
    exit {SCHEDULE_DEADLINE_EXIT_CODE}
fi
exit "$mediaforce_status"
"""


def process_result_reached_schedule_deadline(
        result: subprocess.CompletedProcess[Any],
        host: Mapping[str, Any] | None,
        *,
        now: datetime | None = None,
) -> bool:
    stderr = result.stderr
    if isinstance(stderr, bytes):
        stderr_text = stderr.decode("utf-8", errors="replace")
    else:
        stderr_text = str(stderr or "")
    if SCHEDULE_DEADLINE_MARKER in stderr_text:
        return True
    deadline = schedule_close_deadline_for_host(host)
    if deadline is None or result.returncode not in SCHEDULE_DEADLINE_FALLBACK_RETURN_CODES:
        return False
    current = now.astimezone(UTC) if now is not None else datetime.now(tz=UTC)
    return current >= deadline
