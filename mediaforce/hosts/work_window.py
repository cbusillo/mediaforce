from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.process_control import ScheduleWindowClosedError

if TYPE_CHECKING:
    from mediaforce.library.evidence_hosts import ControllerDigestWindow

CONTROLLER_WORK_WINDOW_WAIT = "controller_work_window"


def controller_work_schedule(config: MediaforceConfig, *, now: datetime | None = None) -> ControllerDigestWindow:
    from mediaforce.library.evidence_hosts import controller_digest_window

    return controller_digest_window(config, lambda current: _controller_schedule_rows(current, now=now))


def _controller_schedule_rows(config: MediaforceConfig, *, now: datetime | None) -> list[dict[str, Any]]:
    from mediaforce.web.runtime.encode_scheduler import (
        encode_queue_schedule_profiles, evaluate_encode_schedule_transition,
        schedule_policy_deps, schedule_profile_policy_for_host,
    )

    deps = schedule_policy_deps()
    profiles = encode_queue_schedule_profiles(config, deps)
    rows = []
    for host in config.remote_hosts:
        if not isinstance(host, dict):
            continue
        policy = schedule_profile_policy_for_host(config, host, deps, profiles=profiles)
        transition = evaluate_encode_schedule_transition(policy, deps, now=now, host_payload=host)
        rows.append({"key": host.get("host"), "label": host.get("label"),
                     "finish_current_file": policy.get("finish_current_file") is True, **transition.to_payload()})
    return rows


def controller_work_allowed(config: MediaforceConfig) -> bool:
    return controller_work_schedule(config).is_open


def require_controller_work_window(config: MediaforceConfig, *, window: ControllerDigestWindow | None = None) -> None:
    current = window if window is not None else controller_work_schedule(config)
    if not current.is_open:
        raise ScheduleWindowClosedError("Waiting for this computer's work window.")
