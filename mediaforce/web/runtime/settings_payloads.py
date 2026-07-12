from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.library.metadata_sync import metadata_configuration_status
from mediaforce.web.settings_runtime import HOST_CAPABILITY_OPTIONS, index_schedule_profile_rows, \
    index_settings_library_rows, index_settings_remote_rows, schedule_profile_options, settings_archive_root, \
    settings_library_rows_for_config, settings_remote_rows_for_config, settings_schedule_profile_rows_for_config, \
    settings_transcode_root_value, settings_video_defaults_for_config


def settings_page_payload(
        config: MediaforceConfig,
        *,
        encode_queue_scheduler_policy: Any,
        normalize_encode_queue_scheduler: Any,
        archive_cleanup_summary_fn: Any,
        include_archive_cleanup: bool = True,
        error: str | None = None,
        saved: bool = False,
        host_notice: str | None = None,
        host_notice_kind: str | None = None,
        libraries: list[dict[str, Any]] | None = None,
        remote_hosts: list[dict[str, Any]] | None = None,
        transcode_root: str | None = None,
        video_defaults: dict[str, Any] | None = None,
        encode_queue_scheduler: dict[str, Any] | None = None,
        schedule_profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved_transcode_root = settings_transcode_root_value(config) if transcode_root is None else transcode_root
    resolved_encode_queue_scheduler = (
        normalize_encode_queue_scheduler(encode_queue_scheduler)
        if encode_queue_scheduler is not None
        else encode_queue_scheduler_policy(config)
    )
    resolved_schedule_profiles = (
        index_schedule_profile_rows(schedule_profiles)
        if schedule_profiles is not None
        else settings_schedule_profile_rows_for_config(config)
    )
    return {
        "error": error,
        "saved": saved,
        "host_notice": host_notice,
        "host_notice_kind": host_notice_kind,
        "libraries": index_settings_library_rows(libraries) if libraries is not None else settings_library_rows_for_config(config),
        "remote_hosts": index_settings_remote_rows(remote_hosts) if remote_hosts is not None else settings_remote_rows_for_config(config),
        "transcode_root": resolved_transcode_root,
        "video_defaults": dict(video_defaults) if video_defaults is not None else settings_video_defaults_for_config(config),
        "encode_queue_scheduler": resolved_encode_queue_scheduler,
        "schedule_profiles": resolved_schedule_profiles,
        "schedule_profile_options": schedule_profile_options(schedule_profiles=resolved_schedule_profiles),
        "host_capability_options": list(HOST_CAPABILITY_OPTIONS),
        "archive_root": settings_archive_root(resolved_transcode_root),
        "archive_cleanup": archive_cleanup_summary_fn(config) if include_archive_cleanup else None,
        "metadata": metadata_configuration_status(config),
        "runtime_settings_path": str(config.paths.runtime_settings_path),
        "repo_config_path": str(config.paths.config_path),
    }
