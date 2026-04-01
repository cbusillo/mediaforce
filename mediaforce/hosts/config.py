import socket

from mediaforce.hosts.types import DEFAULT_HOST_MEDIA_ACCESS, HostStatus, REMOTE_SHELL_PATH


def normalize_host_media_access(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "stream":
        return "stream"
    return DEFAULT_HOST_MEDIA_ACCESS


def host_media_access_for_host(host: dict[str, object] | None) -> str:
    return normalize_host_media_access((host or {}).get("media_access"))


def ssh_target_for_host(host: dict[str, object]) -> str:
    return str(host.get("host") or host.get("key") or "").strip()


def _host_priority(host: dict[str, object]) -> int:
    try:
        return int(str(host.get("priority") or "0"))
    except (TypeError, ValueError):
        return 0


def _host_capabilities(host: dict[str, object]) -> list[str]:
    raw = host.get("capabilities")
    if isinstance(raw, str):
        values = [value.strip() for value in raw.split(",") if value.strip()]
    elif isinstance(raw, list):
        values = [str(value).strip() for value in raw if str(value).strip()]
    else:
        values = []
    normalized = sorted({value.lower().replace(" ", "_") for value in values})
    return normalized or ["encode_queue"]


def _host_supports_capability(host: dict[str, object], capability: str) -> bool:
    return capability.strip().lower() in _host_capabilities(host)


def remote_shell_path_export_line() -> str:
    return f'export PATH="{REMOTE_SHELL_PATH}:$PATH"'


def _parse_utc_offset_minutes(value: object) -> int | None:
    text = str(value or "").strip()
    if len(text) != 5 or text[0] not in {"+", "-"} or not text[1:].isdigit():
        return None
    sign = 1 if text[0] == "+" else -1
    hours = int(text[1:3])
    minutes = int(text[3:5])
    return sign * ((hours * 60) + minutes)


def host_status_targets_current_machine(status: HostStatus) -> bool:
    return _host_lookup_targets_current_machine(_ssh_lookup_host(status.key))


def host_targets_current_machine(host: dict[str, object]) -> bool:
    return _host_lookup_targets_current_machine(_ssh_lookup_host(ssh_target_for_host(host)))


def execution_mode_for_host(host: dict[str, object] | None) -> str:
    host_payload = host or {}
    mode = str(host_payload.get("mode") or "local").strip().lower() or "local"
    if mode == "ssh" and host_targets_current_machine(host_payload):
        return "local"
    return mode


def _ssh_lookup_host(ssh_host: str) -> str:
    host_part = ssh_host.rsplit("@", 1)[-1].strip()
    if host_part.startswith("[") and "]" in host_part:
        return host_part[1: host_part.index("]")]
    if host_part.count(":") == 1:
        name, port = host_part.rsplit(":", 1)
        if port.isdigit():
            return name
    return host_part


def _host_lookup_targets_current_machine(lookup_host: str) -> bool:
    normalized = lookup_host.strip().lower()
    if not normalized:
        return False
    aliases = {
        "localhost",
        "127.0.0.1",
        "::1",
        socket.gethostname().strip().lower(),
        socket.getfqdn().strip().lower(),
    }
    return normalized in aliases


__all__ = [
    "_host_capabilities",
    "_host_lookup_targets_current_machine",
    "_host_priority",
    "_host_supports_capability",
    "_parse_utc_offset_minutes",
    "_ssh_lookup_host",
    "execution_mode_for_host",
    "host_media_access_for_host",
    "host_status_targets_current_machine",
    "host_targets_current_machine",
    "normalize_host_media_access",
    "remote_shell_path_export_line",
    "ssh_target_for_host",
]
