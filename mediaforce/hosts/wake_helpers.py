import socket
import subprocess
from typing import Any

from mediaforce.core.config import MediaforceConfig, update_runtime_settings
from mediaforce.hosts.config import _ssh_lookup_host


def _wake_broadcast_destinations(host: dict[str, Any]) -> list[tuple[str, int]]:
    destinations: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for address in _local_broadcast_addresses(host):
        for port in (9, 7):
            destination = (address, port)
            if destination in seen:
                continue
            destinations.append(destination)
            seen.add(destination)
    for port in (9, 7):
        destination = ("255.255.255.255", port)
        if destination in seen:
            continue
        destinations.append(destination)
        seen.add(destination)
    return destinations


def _local_broadcast_addresses(host: dict[str, Any]) -> list[str]:
    addresses: list[str] = []
    resolved_host = _resolved_ssh_network_host(str(host.get("host") or "").strip())
    if resolved_host:
        ip_address = _resolve_host_to_ip(resolved_host)
        if ip_address:
            routed_interface = _interface_for_ip(ip_address)
            if routed_interface:
                addresses.extend(_broadcast_addresses_for_interface(routed_interface))
    try:
        result = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return list(dict.fromkeys(addresses))
    current_interface = ""
    for raw_line in result.stdout.splitlines():
        line = raw_line.rstrip()
        if line and not line.startswith("\t") and ":" in line:
            current_interface = line.split(":", 1)[0]
            continue
        if not current_interface:
            continue
        broadcast = _broadcast_address_from_line(line)
        if broadcast is not None:
            addresses.append(broadcast)
    return list(dict.fromkeys(addresses))


def _interface_for_ip(ip_address: str) -> str | None:
    try:
        result = subprocess.run(["route", "-n", "get", ip_address], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("interface:"):
            continue
        return stripped.split(":", 1)[1].strip() or None
    return None


def _broadcast_addresses_for_interface(interface: str) -> list[str]:
    try:
        result = subprocess.run(["ifconfig", interface], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return _broadcast_addresses_from_lines(result.stdout.splitlines())


def _tcp_port_is_open(ip_address: str, port: int) -> bool:
    try:
        with socket.create_connection((ip_address, port), timeout=1.5):
            return True
    except OSError:
        return False


def _normalize_mac_address(value: str) -> str | None:
    cleaned = "".join(ch for ch in value if ch.isalnum())
    if len(cleaned) != 12 or any(ch not in "0123456789abcdefABCDEF" for ch in cleaned):
        return None
    return cleaned.lower()


def _resolved_ssh_network_host(ssh_host: str) -> str | None:
    fallback = _ssh_lookup_host(ssh_host)
    try:
        result = subprocess.run(["ssh", "-G", ssh_host], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return fallback or None
    if result.returncode != 0:
        return fallback or None
    for line in result.stdout.splitlines():
        if not line.startswith("hostname "):
            continue
        hostname = line.split(maxsplit=1)[1].strip()
        return hostname or fallback or None
    return fallback or None


def _resolve_host_to_ip(hostname: str) -> str | None:
    try:
        return socket.gethostbyname(hostname)
    except OSError:
        return hostname if _looks_like_ipv4_address(hostname) else None


def _looks_like_ipv4_address(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    return all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def _mac_from_arp(ip_address: str) -> str | None:
    try:
        result = subprocess.run(["arp", "-n", ip_address], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    found = _mac_from_arp_output(result.stdout, result.stderr)
    if found is not None:
        return found
    try:
        subprocess.run(["ping", "-c", "1", ip_address], capture_output=True, text=True, timeout=5)
        result = subprocess.run(["arp", "-n", ip_address], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return _mac_from_arp_output(result.stdout, result.stderr)


def _broadcast_addresses_from_lines(lines: list[str]) -> list[str]:
    addresses: list[str] = []
    for line in lines:
        broadcast = _broadcast_address_from_line(line)
        if broadcast is not None:
            addresses.append(broadcast)
    return addresses


def _broadcast_address_from_line(line: str) -> str | None:
    if "broadcast" not in line or "inet " not in line:
        return None
    parts = line.split()
    for index, part in enumerate(parts[:-1]):
        if part == "broadcast":
            return parts[index + 1]
    return None


def _mac_from_arp_output(stdout: str, stderr: str) -> str | None:
    output = f"{stdout}\n{stderr}"
    for token in output.replace("(", " ").replace(")", " ").split():
        normalized = _normalize_mac_address(token)
        if normalized is not None:
            return ":".join(normalized[index: index + 2] for index in range(0, 12, 2))
    return None


def _persist_remote_wake_mac(config: MediaforceConfig, host: dict[str, object], mac_address: str) -> None:
    host["wake_mac"] = mac_address
    try:
        target_host = str(host.get("host") or "")
        target_label = str(host.get("label") or "")

        def _apply(runtime_settings: dict[str, Any]) -> dict[str, Any]:
            existing_runtime_hosts = runtime_settings.get("remote_hosts")
            source_hosts = existing_runtime_hosts if isinstance(existing_runtime_hosts, list) else config.remote_hosts
            updated_hosts: list[dict[str, Any]] = []
            for entry in source_hosts:
                if not isinstance(entry, dict):
                    continue
                updated_entry = dict(entry)
                if str(updated_entry.get("host") or "") == target_host and str(
                        updated_entry.get("label") or "") == target_label:
                    updated_entry["wake_mac"] = mac_address
                updated_hosts.append(updated_entry)
            runtime_settings["remote_hosts"] = updated_hosts
            return runtime_settings

        update_runtime_settings(config.paths.runtime_settings_path, _apply)
    except (OSError, ValueError):
        return
