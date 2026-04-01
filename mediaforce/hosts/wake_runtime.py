import socket
import time
from typing import Any, Callable

from mediaforce.core.config import MediaforceConfig
from mediaforce.hosts.types import DEFAULT_WAKE_WAIT_SECONDS, HostSetupResult
from mediaforce.hosts.wake_helpers import _mac_from_arp, _normalize_mac_address, _persist_remote_wake_mac, \
    _resolve_host_to_ip, _resolved_ssh_network_host, _tcp_port_is_open, _wake_broadcast_destinations


def _ensure_remote_awake_for_ssh(
        host: dict[str, object],
        *,
        wake_wait_seconds: int = DEFAULT_WAKE_WAIT_SECONDS,
        wake_remote_host_if_configured: Callable[[dict[str, Any]], HostSetupResult],
) -> None:
    mac_address = str(host.get("wake_mac") or host.get("wol_mac") or "").strip()
    if not mac_address:
        return
    ssh_host = str(host.get("host") or "").strip()
    network_host = _resolved_ssh_network_host(ssh_host)
    if not network_host:
        return
    ip_address = _resolve_host_to_ip(network_host)
    if not ip_address:
        return
    if _tcp_port_is_open(ip_address, 22):
        return
    wake_remote_host_if_configured(host)
    deadline = time.monotonic() + wake_wait_seconds
    while time.monotonic() < deadline:
        if _tcp_port_is_open(ip_address, 22):
            return
        time.sleep(2)


def _wake_remote_host_if_configured(host: dict[str, Any]) -> HostSetupResult:
    mac_address = str(host.get("wake_mac") or host.get("wol_mac") or "").strip()
    if not mac_address:
        return HostSetupResult(ok=True, message="No Wake-on-LAN step was needed.")

    normalized = _normalize_mac_address(mac_address)
    if normalized is None:
        return HostSetupResult(
            ok=False,
            message="Wake-on-LAN is configured with an invalid MAC address.",
            detail="Use a 6-byte MAC such as aa:bb:cc:dd:ee:ff.",
        )

    packet = bytes.fromhex(normalized) * 16
    packet = b"\xff" * 6 + packet
    destinations = _wake_broadcast_destinations(host)
    sent_to: list[tuple[str, int]] = []
    last_error: OSError | None = None
    for address, port in destinations:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(packet, (address, port))
            sent_to.append((address, port))
        except OSError as exc:
            last_error = exc
            continue
    if not sent_to:
        return HostSetupResult(
            ok=False,
            message="Sending the Wake-on-LAN packet failed.",
            detail=str(last_error) if last_error is not None else "No valid broadcast destination was available.",
        )

    time.sleep(8)
    return HostSetupResult(
        ok=True,
        message="Wake-on-LAN packet sent.",
        performed_steps=[
            f"Sent a Wake-on-LAN packet to {mac_address} via {', '.join(f'{addr}:{port}' for addr, port in sent_to)}."],
    )


def _learn_remote_wake_mac(config: MediaforceConfig, host: dict[str, object], ssh_host: str) -> None:
    if str(host.get("wake_mac") or host.get("wol_mac") or "").strip():
        return
    network_host = _resolved_ssh_network_host(ssh_host)
    if not network_host:
        return
    ip_address = _resolve_host_to_ip(network_host)
    if not ip_address:
        return
    mac_address = _mac_from_arp(ip_address)
    if not mac_address:
        return
    _persist_remote_wake_mac(config, host, mac_address)
