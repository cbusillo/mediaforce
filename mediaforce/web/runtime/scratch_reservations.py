"""Scratch promised to calibration jobs before their source/output bytes reach disk."""

import json
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import calibration_jobs
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.encoding.staged_host import host_scratch_root, required_scratch_bytes
from mediaforce.hosts.config import execution_mode_for_host, host_media_access_for_host


def host_identity_tokens(host: dict[str, Any]) -> set[str]:
    return {str(value).strip() for value in [host.get("key"), host.get("host"), *object_list(host.get("identity_tokens"))]
            if value and str(value).strip()}


def calibration_scratch_reservations(
        connection: DBClient, config: MediaforceConfig, *, exclude_job_id: str | None = None,
) -> dict[str, int | None]:
    query = select(calibration_jobs.c.host_json, calibration_jobs.c.sample_item_json).where(
        calibration_jobs.c.status.in_(("starting", "running")),
    )
    if exclude_job_id is not None:
        query = query.where(calibration_jobs.c.job_id != exclude_job_id)
    reservations: dict[str, int | None] = {}
    for row in connection.execute(query).mappings():
        host = object_dict(json.loads(row["host_json"]))
        tokens = host_identity_tokens(host)
        for configured in config.remote_hosts:
            if tokens & host_identity_tokens(configured) or str(host.get("key") or "") == configured.get("label"):
                host = {**configured, **host}
                break
        if (execution_mode_for_host(host) != "ssh" or host_media_access_for_host(host) != "stream"
                or host_scratch_root(host) is None):
            continue
        source_size = int_value(object_dict(json.loads(row["sample_item_json"])).get("source_size_bytes"))
        budget = required_scratch_bytes(source_size) if source_size > 0 else None
        for token in host_identity_tokens(host):
            previous = reservations.get(token, 0)
            reservations[token] = previous + budget if previous is not None and budget is not None else None
    return reservations
