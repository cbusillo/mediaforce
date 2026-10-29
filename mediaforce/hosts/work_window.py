from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.process_control import ScheduleWindowClosedError
from mediaforce.hosts.config import host_targets_current_machine, ssh_target_for_host

if TYPE_CHECKING:
    from mediaforce.web.runtime.encode_scheduler import EncodeScheduleTransition, SchedulePolicyDeps

CONTROLLER_WORK_WINDOW_WAIT = "controller_work_window"


def controller_work_schedule(
        config: MediaforceConfig,
        deps: SchedulePolicyDeps | None = None,
        *,
        now: datetime | None = None,
) -> EncodeScheduleTransition:
    from mediaforce.web.runtime.encode_scheduler import (
        EncodeScheduleTransition, encode_queue_schedule_profiles, evaluate_encode_schedule_transition,
        schedule_policy_deps, schedule_profile_policy_for_host,
    )

    if deps is None:
        deps = schedule_policy_deps()
    controller_hosts = [
        host for host in config.remote_hosts
        if host.get("mode") == "local" or _host_targets_controller(ssh_target_for_host(host))
    ]
    if not controller_hosts:
        return EncodeScheduleTransition(is_open=True)
    profiles = encode_queue_schedule_profiles(config, deps)
    transitions = [
        evaluate_encode_schedule_transition(
            schedule_profile_policy_for_host(config, host, deps, profiles=profiles),
            deps,
            now=now,
            host_payload=host,
        ) for host in controller_hosts
    ]
    if len(transitions) == 1:
        return transitions[0]
    if any(not transition.is_open for transition in transitions):
        return EncodeScheduleTransition(is_open=False)
    deadlines = [transition.current_closes_at for transition in transitions if transition.current_closes_at is not None]
    return EncodeScheduleTransition(is_open=True, current_closes_at=min(deadlines) if deadlines else None)


def controller_work_allowed(config: MediaforceConfig) -> bool:
    return controller_work_schedule(config).is_open


def require_controller_work_window(config: MediaforceConfig) -> None:
    if not controller_work_allowed(config):
        raise ScheduleWindowClosedError("Waiting for this computer's work window.")


@lru_cache(maxsize=128)
def _host_targets_controller(target: str) -> bool:
    return host_targets_current_machine({"host": target})
