import subprocess
from pathlib import Path
from typing import Callable

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import object_dict
from mediaforce.hosts.config import _host_capabilities, _host_priority, _parse_utc_offset_minutes, \
    host_media_access_for_host, host_targets_current_machine, stream_host_has_remote_source_roots
from mediaforce.hosts.status_helpers import _classify_ssh_failure, _find_local_tool, _host_capability_issues, \
    _host_setup_supported, _local_platform_name, _local_utc_offset_minutes, _parse_remote_status_output, \
    _remote_setup_needs_password, _remote_status_script, _status_from_paths, _status_platform
from mediaforce.hosts.types import HostStatus


def _run_remote_status_probe(
        host: dict[str, object],
        script: str,
        *,
        timeout: int,
        run_remote_ssh: Callable[..., subprocess.CompletedProcess[str]],
        should_retry_remote_status_exception: Callable[[Exception], bool],
        should_retry_remote_status_failure: Callable[[str], bool],
        sleep: Callable[[float], None],
        retry_delay_seconds: float,
) -> subprocess.CompletedProcess[str]:
    last_exception: Exception | None = None
    for attempt in range(2):
        try:
            result = run_remote_ssh(
                host,
                "sh",
                "-s",
                input_text=script,
                timeout=timeout,
                wake_before_connect=False,
            )
        except Exception as exc:
            last_exception = exc
            if attempt == 0 and should_retry_remote_status_exception(exc):
                sleep(retry_delay_seconds)
                continue
            raise

        if result.returncode == 0:
            return result
        detail = result.stderr.strip() or result.stdout.strip() or ""
        if attempt == 0 and should_retry_remote_status_failure(detail):
            sleep(retry_delay_seconds)
            continue
        return result

    if last_exception is not None:
        raise last_exception
    raise RuntimeError("Remote status probe did not produce a result.")


def _remote_host_status(
        config: MediaforceConfig,
        host: dict[str, object],
        *,
        current_machine_host_status: Callable[..., HostStatus],
        run_remote_status_probe: Callable[..., subprocess.CompletedProcess[str]],
        learn_remote_wake_mac: Callable[[MediaforceConfig, dict[str, object], str], None],
) -> HostStatus:
    label = str(host.get("label") or host.get("host") or "remote")
    ssh_host = str(host.get("host") or "")
    repo_path = str(host.get("repo_path") or "") or None
    if not ssh_host:
        return HostStatus(
            key=label,
            label=label,
            mode="ssh",
            priority=_host_priority(host),
            capabilities=_host_capabilities(host),
            available=False,
            message="Missing SSH host configuration",
            missing_paths=[],
            repo_path=repo_path,
            issues=["Add an SSH host to enable remote checks."],
        )

    if host_targets_current_machine({"host": ssh_host}):
        return current_machine_host_status(
            config,
            host,
            ssh_host=ssh_host,
            label=label,
            repo_path=repo_path,
        )

    media_access = host_media_access_for_host(host)
    merged_source_roots = config.source_root_map_for_host(host)
    explicit_source_roots = object_dict(host.get("source_roots"))
    supports_remote_stream_quality = stream_host_has_remote_source_roots(host)
    paths = []
    require_paths = media_access != "stream"
    if require_paths:
        paths = [str(path) for path in merged_source_roots.values()] + [
            str(config.staging_root_for_host(host))]
    elif media_access == "stream":
        paths = [
            str(Path(str(path).strip()).expanduser())
            for path in explicit_source_roots.values()
            if str(path).strip()
        ]
        require_paths = bool(paths)
    repo_check_path = repo_path if repo_path is not None else ""
    script = _remote_status_script(paths=paths, repo_path=repo_check_path)
    try:
        result = run_remote_status_probe(host, script, timeout=8)
    except Exception as exc:
        return HostStatus(
            key=ssh_host,
            label=label,
            mode="ssh",
            priority=_host_priority(host),
            capabilities=_host_capabilities(host),
            available=False,
            message="SSH unavailable",
            missing_paths=[],
            repo_path=repo_path,
            issues=["Mediaforce could not reach this host over SSH."],
            detail=str(exc),
        )

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "SSH host unavailable"
        classification = _classify_ssh_failure(detail)
        return HostStatus(
            key=ssh_host,
            label=label,
            mode="ssh",
            priority=_host_priority(host),
            capabilities=_host_capabilities(host),
            available=False,
            message=classification["message"],
            missing_paths=[],
            repo_path=repo_path,
            issues=classification["issues"],
            detail=detail if classification.get("show_detail", True) else None,
            setup_supported=classification["setup_supported"],
            setup_requires_password=classification["setup_requires_password"],
            trust_reset_supported=classification.get("trust_reset_supported", False),
        )

    try:
        payload = _parse_remote_status_output(result.stdout)
    except ValueError:
        return HostStatus(
            key=ssh_host,
            label=label,
            mode="ssh",
            priority=_host_priority(host),
            capabilities=_host_capabilities(host),
            available=False,
            message="Remote check returned unreadable output",
            missing_paths=[],
            repo_path=repo_path,
            issues=["The remote host responded, but the capability payload was invalid."],
            detail=result.stdout.strip() or None,
            setup_supported=True,
        )

    mounted_paths = {
        str(path_text): bool(exists)
        for path_text, exists in object_dict(payload.get("paths")).items()
    }
    tools = object_dict(payload.get("tools"))
    tool_paths = object_dict(payload.get("tool_paths"))
    platform_name = _status_platform(payload.get("platform"), tools=tools)
    learn_remote_wake_mac(config, host, ssh_host)
    capability_issues = _host_capability_issues(
        host,
        tools=tools,
        platform_name=platform_name,
        repo_path=repo_path,
        repo_path_exists=bool(payload.get("repo_path_exists", False)),
        supports_remote_stream_quality=supports_remote_stream_quality,
    )
    return _status_from_paths(
        key=ssh_host,
        label=label,
        mode="ssh",
        priority=_host_priority(host),
        capabilities=_host_capabilities(host),
        mounted_paths=mounted_paths,
        repo_path=repo_path,
        ffmpeg_path=str(tool_paths.get("ffmpeg") or "") or None,
        platform=platform_name,
        videotoolbox_available=bool(tools.get("ffmpeg_videotoolbox")),
        utc_offset_minutes=_parse_utc_offset_minutes(payload.get("utc_offset")),
        issues=capability_issues,
        setup_supported=_host_setup_supported(platform_name),
        setup_requires_password=_remote_setup_needs_password(capability_issues),
        require_paths=require_paths,
    )


def _current_machine_host_status(
        config: MediaforceConfig,
        host: dict[str, object],
        *,
        ssh_host: str,
        label: str,
        repo_path: str | None,
        local_tool_status_snapshot: Callable[[], dict[str, bool]],
) -> HostStatus:
    media_access = host_media_access_for_host(host)
    merged_source_roots = config.source_root_map_for_host(host)
    explicit_source_roots = object_dict(host.get("source_roots"))
    supports_remote_stream_quality = stream_host_has_remote_source_roots(host)
    require_paths = media_access != "stream"
    status_paths: list[Path]
    if require_paths:
        status_paths = [
            *merged_source_roots.values(),
            config.staging_root_for_host(host),
        ]
    else:
        status_paths = [Path(str(path).strip()).expanduser() for path in explicit_source_roots.values() if str(path).strip()]
        require_paths = bool(status_paths)
    mounted_paths = {
        str(path): path.exists()
        for path in status_paths
    }
    tools = local_tool_status_snapshot()
    ffmpeg_bin = _find_local_tool("ffmpeg", fallback_paths=["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"])
    platform_name = _local_platform_name()
    capability_issues = _host_capability_issues(
        host,
        tools=tools,
        platform_name=platform_name,
        repo_path=repo_path,
        repo_path_exists=not repo_path or Path(repo_path).expanduser().exists(),
        supports_remote_stream_quality=supports_remote_stream_quality,
    )
    return _status_from_paths(
        key=ssh_host,
        label=label,
        mode="ssh",
        priority=_host_priority(host),
        capabilities=_host_capabilities(host),
        mounted_paths=mounted_paths,
        repo_path=repo_path,
        ffmpeg_path=ffmpeg_bin,
        platform=platform_name,
        videotoolbox_available=bool(tools.get("ffmpeg_videotoolbox")),
        utc_offset_minutes=_local_utc_offset_minutes(),
        issues=capability_issues,
        setup_supported=_host_setup_supported(platform_name),
        require_paths=require_paths,
    )
