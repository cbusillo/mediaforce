import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import object_dict
from mediaforce.hosts.config import _host_capabilities, _host_priority, _parse_utc_offset_minutes, \
    host_media_access_for_host, host_targets_current_machine, stream_host_has_remote_source_roots
from mediaforce.hosts.mount_runtime import finder_mount_roots_for_paths
from mediaforce.hosts.status_helpers import _classify_ssh_failure, _find_local_tool, _host_capability_issues, \
    _host_setup_supported, _local_platform_name, _local_schedule_timezone, _local_utc_offset_minutes, \
    _parse_remote_status_output, _remote_setup_needs_password, _remote_status_script, _status_from_paths, \
    _status_platform
from mediaforce.hosts.types import HostStatus

REMOTE_TOOL_CACHE_TTL_SECONDS = 24 * 60 * 60
_REMOTE_TOOL_CACHE: dict[str, tuple[float, dict[str, object]]] = {}


def _cached_remote_tool_payload(ssh_host: str, *, now: float | None = None) -> dict[str, object] | None:
    if now is None:
        now = time.monotonic()
    cached = _REMOTE_TOOL_CACHE.get(ssh_host)
    if cached is None:
        return None
    cached_at, payload = cached
    if now - cached_at > REMOTE_TOOL_CACHE_TTL_SECONDS:
        _REMOTE_TOOL_CACHE.pop(ssh_host, None)
        return None
    return dict(payload)


def _store_remote_tool_payload(ssh_host: str, payload: dict[str, object], *, now: float | None = None) -> None:
    if now is None:
        now = time.monotonic()
    tools = dict(object_dict(payload.get("tools")))
    expensive_keys = {"ffmpeg_videotoolbox", "ffmpeg_libvmaf", "ffmpeg_xpsnr", "ffmpeg_libsvtav1"}
    if not expensive_keys.issubset(tools):
        return
    _REMOTE_TOOL_CACHE[ssh_host] = (
        now,
        {
            "tools": tools,
            "tool_paths": dict(object_dict(payload.get("tool_paths"))),
            "tool_meta": dict(object_dict(payload.get("tool_meta"))),
            "platform": str(payload.get("platform") or "unknown"),
        },
    )


def _cached_remote_tools_compatible(payload: dict[str, object], cached: dict[str, object] | None) -> bool:
    if not cached:
        return False
    current_meta = object_dict(payload.get("tool_meta"))
    cached_meta = object_dict(cached.get("tool_meta"))
    cached_signature = str(cached_meta.get("ffmpeg_signature") or "")
    current_signature = str(current_meta.get("ffmpeg_signature") or "")
    if cached_signature and current_signature and cached_signature != current_signature:
        return False
    if cached_signature and not current_signature:
        return False
    current_paths = object_dict(payload.get("tool_paths"))
    cached_paths = object_dict(cached.get("tool_paths"))
    cached_ffmpeg_path = str(cached_paths.get("ffmpeg") or "")
    current_ffmpeg_path = str(current_paths.get("ffmpeg") or "")
    if cached_ffmpeg_path and current_ffmpeg_path and cached_ffmpeg_path != current_ffmpeg_path:
        return False
    return True


def _merge_cached_remote_tools(payload: dict[str, object], cached: dict[str, object] | None) -> dict[str, object]:
    if not cached:
        return payload
    if not _cached_remote_tools_compatible(payload, cached):
        return payload
    tools = dict(object_dict(payload.get("tools")))
    if not bool(tools.get("ffmpeg")):
        return payload
    merged_tools = dict(object_dict(cached.get("tools")))
    merged_tools.update(tools)
    for key in ("ffmpeg_videotoolbox", "ffmpeg_libvmaf", "ffmpeg_xpsnr", "ffmpeg_libsvtav1"):
        if key in object_dict(cached.get("tools")) and key not in tools:
            merged_tools[key] = object_dict(cached.get("tools"))[key]
    merged_tool_paths = dict(object_dict(cached.get("tool_paths")))
    merged_tool_paths.update(object_dict(payload.get("tool_paths")))
    merged = dict(payload)
    merged["tools"] = merged_tools
    merged["tool_paths"] = merged_tool_paths
    merged_tool_meta = dict(object_dict(cached.get("tool_meta")))
    merged_tool_meta.update(object_dict(payload.get("tool_meta")))
    merged["tool_meta"] = merged_tool_meta
    if str(merged.get("platform") or "unknown") == "unknown" and cached.get("platform"):
        merged["platform"] = cached["platform"]
    return merged


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
            probe_available=False,
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
    explicit_source_roots = config.explicit_source_root_map_for_host(host)
    supports_remote_stream_quality = stream_host_has_remote_source_roots(host)
    paths = []
    require_paths = media_access != "stream"
    if require_paths:
        staging_check_path = str(config.staging_root_for_host(host))
        paths = [str(path) for path in merged_source_roots.values()] + [
            staging_check_path]
        writable_paths = [staging_check_path]
    elif media_access == "stream":
        paths = [
            str(Path(str(path).strip()).expanduser())
            for path in explicit_source_roots.values()
            if str(path).strip()
        ]
        require_paths = bool(paths)
        writable_paths = []
    else:
        writable_paths = []
    repo_check_path = repo_path if repo_path is not None else ""
    cached_tools = _cached_remote_tool_payload(ssh_host)
    script = _remote_status_script(
        paths=paths,
        repo_path=repo_check_path,
        include_expensive_tools=cached_tools is None,
        mount_paths=[str(path) for path in finder_mount_roots_for_paths(paths)],
        writable_paths=writable_paths,
    )
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
            probe_available=False,
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
            probe_available=False,
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
            probe_available=False,
            repo_path=repo_path,
            issues=["The remote host responded, but the capability payload was invalid."],
            detail=result.stdout.strip() or None,
            setup_supported=True,
        )
    if cached_tools is None:
        _store_remote_tool_payload(ssh_host, payload)
    elif _cached_remote_tools_compatible(payload, cached_tools):
        payload = _merge_cached_remote_tools(payload, cached_tools)
    else:
        _REMOTE_TOOL_CACHE.pop(ssh_host, None)
        full_script = _remote_status_script(
            paths=paths,
            repo_path=repo_check_path,
            include_expensive_tools=True,
            mount_paths=[str(path) for path in finder_mount_roots_for_paths(paths)],
            writable_paths=writable_paths,
        )
        try:
            result = run_remote_status_probe(host, full_script, timeout=8)
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
                probe_available=False,
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
                probe_available=False,
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
                probe_available=False,
                repo_path=repo_path,
                issues=["The remote host responded, but the capability payload was invalid."],
                detail=result.stdout.strip() or None,
                setup_supported=True,
            )
        _store_remote_tool_payload(ssh_host, payload)

    mounted_paths = {
        str(path_text): bool(exists)
        for path_text, exists in object_dict(payload.get("paths")).items()
    }
    source_path_map = explicit_source_roots if media_access == "stream" else merged_source_roots
    source_paths = [str(path) for path in source_path_map.values()]
    staging_path = str(config.staging_root_for_host(host)) if require_paths and media_access != "stream" else None
    tools = object_dict(payload.get("tools"))
    tool_paths = object_dict(payload.get("tool_paths"))
    platform_name = _status_platform(payload.get("platform"), tools=tools)
    missing_mounts = [
        str(path_text)
        for path_text, mounted in object_dict(payload.get("mounts")).items()
        if platform_name == "macos" and not bool(mounted)
    ]
    learn_remote_wake_mac(config, host, ssh_host)
    capability_issues = _host_capability_issues(
        host,
        tools=tools,
        platform_name=platform_name,
        repo_path=repo_path,
        repo_path_exists=bool(payload.get("repo_path_exists", False)),
        path_access={
            str(path): object_dict(access)
            for path, access in object_dict(payload.get("path_access")).items()
        },
        source_paths=source_paths,
        staging_path=staging_path,
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
        schedule_timezone=str(payload.get("schedule_timezone") or "") or None,
        utc_offset_minutes=_parse_utc_offset_minutes(payload.get("utc_offset")),
        issues=capability_issues,
        missing_mounts=missing_mounts,
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
    explicit_source_roots = config.explicit_source_root_map_for_host(host)
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
    platform_name = _local_platform_name()
    mounted_paths = {
        str(path): path.exists()
        for path in status_paths
    }
    missing_mounts = [
        str(path)
        for path in finder_mount_roots_for_paths(status_paths)
        if platform_name == "macos" and not _local_mount_present(path)
    ]
    source_path_map = explicit_source_roots if media_access == "stream" else merged_source_roots
    source_paths = [str(path) for path in source_path_map.values()]
    staging_path = str(config.staging_root_for_host(host)) if require_paths and media_access != "stream" else None
    path_access = {
        str(path): {
            "exists": path.exists(),
            "read": _local_path_readable(path),
            "write": _local_path_writable(path) if staging_path and str(path) == staging_path else False,
        }
        for path in status_paths
    }
    tools = local_tool_status_snapshot()
    ffmpeg_bin = _find_local_tool("ffmpeg", fallback_paths=["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"])
    capability_issues = _host_capability_issues(
        host,
        tools=tools,
        platform_name=platform_name,
        repo_path=repo_path,
        repo_path_exists=not repo_path or Path(repo_path).expanduser().exists(),
        path_access=path_access,
        source_paths=source_paths,
        staging_path=staging_path,
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
        schedule_timezone=_local_schedule_timezone(),
        utc_offset_minutes=_local_utc_offset_minutes(),
        issues=capability_issues,
        missing_mounts=missing_mounts,
        setup_supported=_host_setup_supported(platform_name),
        require_paths=require_paths,
    )


def _local_path_readable(path: Path) -> bool:
    if path.is_dir():
        try:
            next(path.iterdir(), None)
        except StopIteration:
            return True
        except OSError:
            return False
        return True
    return path.exists() and path.is_file()


def _local_path_writable(path: Path) -> bool:
    if not path.is_dir():
        return False
    probe_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".mediaforce-write-test.", dir=path, delete=False) as probe:
            probe_path = probe.name
    except OSError:
        return False
    if probe_path:
        try:
            Path(probe_path).unlink()
        except OSError:
            pass
    return True


def _local_mount_present(path: Path) -> bool:
    try:
        return path.is_mount()
    except OSError:
        return False
