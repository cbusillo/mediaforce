from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_SOURCE_IDS,
    av1_validation_v4_source_stream_selection_payload,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4_preparation_claim import (
    AV1ValidationV4PreparationClaimError,
    assert_av1_validation_v4_preparation_claim,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    AV1ValidationV4PreparationGrantError,
    assert_av1_validation_v4_preparation_grant,
)


AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_SCHEMA = (
    "mediaforce.av1_cold_start_v4_preparation_measurement"
)
AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_SCHEMA_VERSION = 1
AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_CONTRACT_VERSION = "av1v4pmeas1"
AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_STAGES = (
    "validate_inputs",
    "claim_grant",
    "create_key",
    "measure_config",
    "probe_toolchain",
    "derive_path_identities",
    "derive_runtime_compatibility",
    "derive_invocations",
    "build_record",
    "publish",
)

_MEASUREMENT_ID_RE = re.compile(r"av1vprepmeas4_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_METHOD = {
    "binary_digest_algorithm": "sha256_whole_file",
    "builder_subprocess_count": 0,
    "config_digest_algorithm": "sha256_canonical_file_bytes",
    "invocation_digest_source": "merged_manifest_observed_streams",
    "media_bytes_read_count": 0,
    "media_paths_opened_count": 0,
    "media_processing_subprocess_count": 0,
    "network_access_performed": False,
    "repository_identity_source": "caller_supplied_verified_against_grant",
    "tool_version_probe_subprocess_count_max": 3,
    "tool_version_probe_argv": {
        "ab_av1": ["--version"],
        "ffmpeg": ["-version"],
        "ffprobe": ["-version"],
    },
    "tool_version_probe_timeout_seconds_max": 10,
    "tool_version_string_normalization": "first_line_stripped",
}
_COMMON_KEYS = frozenset({
    "claim_id",
    "claim_payload_sha256",
    "completed_at",
    "contract_version",
    "experiment_id",
    "manifest_id",
    "manifest_payload_sha256",
    "manifest_revision",
    "measurement_id",
    "method",
    "payload_sha256",
    "preparation_grant_id",
    "preparation_grant_payload_sha256",
    "probes",
    "protocol_version",
    "schema",
    "schema_version",
    "stages_completed",
    "started_at",
    "state",
    "tool_version_probe_subprocess_count",
}) | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
_PREPARATION_ID_RE = re.compile(r"av1vprep4_[0-9a-f]{32}\Z")
_PREPARATION_GRANT_ID_RE = re.compile(r"av1vprepgrant4_[0-9a-f]{32}\Z")
_PREPARATION_CLAIM_ID_RE = re.compile(r"av1vprepclaim4_[0-9a-f]{32}\Z")
_PATH_PRIVACY_KEY_ID_RE = re.compile(r"av1vpathkey4_[0-9a-f]{32}\Z")
_RUNTIME_COMPATIBILITY_ID_RE = re.compile(r"av1vruntime4_[0-9a-f]{32}\Z")
_ROLLBACK_ARTIFACTS = frozenset({
    "effective_config_snapshot",
    "path_privacy_key",
    "preparation_record",
})


class AV1ValidationV4PreparationMeasurementError(ValueError):
    pass


def build_av1_validation_v4_preparation_success_measurement(
    *,
    claim: Mapping[str, Any],
    preparation_grant: Mapping[str, Any],
    preparation: Mapping[str, Any],
    started_at: str,
    completed_at: str,
    probes: Sequence[Mapping[str, Any]],
    key_custody: Mapping[str, Any],
    source_stream_constraints: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    record = object_dict(preparation)
    claim_payload = object_dict(claim)
    grant = object_dict(preparation_grant)
    if (
        record.get("preparation_claim_id") != claim_payload.get("claim_id")
        or record.get("preparation_claim_payload_sha256")
        != claim_payload.get("payload_sha256")
        or record.get("preparation_grant_id") != grant.get("grant_id")
        or record.get("preparation_grant_payload_sha256")
        != grant.get("payload_sha256")
        or isinstance(record.get("media_bytes_read_count"), bool)
        or not isinstance(record.get("media_bytes_read_count"), int)
        or record.get("media_bytes_read_count") != 0
    ):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement record binding is invalid"
        )
    payload = _common_payload(
        state="measured_success",
        claim=claim,
        preparation_grant=preparation_grant,
        started_at=started_at,
        completed_at=completed_at,
        probes=probes,
        stages_completed=AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_STAGES,
    )
    payload.update({
        "preparation_id": record.get("preparation_id"),
        "preparation_payload_sha256": record.get("payload_sha256"),
        "path_privacy_key_id": record.get("path_privacy_key_id"),
        "runtime_compatibility_id": record.get("runtime_compatibility_id"),
        "guided_warm_start_count": len(
            object_dict(record.get("guided_warm_start_identities"))
        ),
        "invocation_count": len(object_list(record.get("invocations"))),
        "key_custody": object_dict(key_custody),
        "source_stream_constraints": {
            str(asset_id): object_dict(constraint)
            for asset_id, constraint in source_stream_constraints.items()
        },
        "rollback": None,
        "failure": None,
    })
    return _bind_and_validate(payload)


def build_av1_validation_v4_preparation_failure_measurement(
    *,
    claim: Mapping[str, Any],
    preparation_grant: Mapping[str, Any],
    started_at: str,
    completed_at: str,
    probes: Sequence[Mapping[str, Any]],
    stages_completed: Sequence[str],
    failure_stage: str,
    reason_code: str,
    error_class: str,
    message_sha256: str,
    rollback: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _common_payload(
        state="measured_failed",
        claim=claim,
        preparation_grant=preparation_grant,
        started_at=started_at,
        completed_at=completed_at,
        probes=probes,
        stages_completed=stages_completed,
    )
    payload.update({
        "preparation_id": None,
        "preparation_payload_sha256": None,
        "path_privacy_key_id": object_dict(rollback).get("path_privacy_key_id"),
        "runtime_compatibility_id": None,
        "guided_warm_start_count": None,
        "invocation_count": None,
        "key_custody": None,
        "source_stream_constraints": None,
        "rollback": object_dict(rollback),
        "failure": {
            "stage": failure_stage,
            "reason_code": reason_code,
            "error_class": error_class,
            "message_sha256": message_sha256,
        },
    })
    return _bind_and_validate(payload)


def assert_av1_validation_v4_preparation_measurement(
    payload: Mapping[str, Any],
) -> None:
    materialized = object_dict(payload)
    state = materialized.get("state")
    allowed = _COMMON_KEYS | {
        "failure",
        "guided_warm_start_count",
        "invocation_count",
        "key_custody",
        "path_privacy_key_id",
        "preparation_id",
        "preparation_payload_sha256",
        "rollback",
        "runtime_compatibility_id",
        "source_stream_constraints",
    }
    if not materialized or set(materialized) - allowed:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement shape is invalid"
        )
    _assert_no_private_text(materialized)
    expected = {
        "schema": AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "method": _METHOD,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1ValidationV4PreparationMeasurementError(
                f"AV1 v4 preparation measurement cannot authorize {field}"
            )
    started_at = _parse_timestamp(materialized.get("started_at"))
    completed_at = _parse_timestamp(materialized.get("completed_at"))
    if completed_at < started_at:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement timestamps are out of order"
        )
    if not _PREPARATION_GRANT_ID_RE.fullmatch(
        str(materialized.get("preparation_grant_id") or "")
    ) or not _PREPARATION_CLAIM_ID_RE.fullmatch(
        str(materialized.get("claim_id") or "")
    ):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement upstream identity is invalid"
        )
    for field in (
        "preparation_grant_payload_sha256",
        "claim_payload_sha256",
        "payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(materialized.get(field) or "")):
            raise AV1ValidationV4PreparationMeasurementError(
                f"AV1 v4 preparation measurement {field} is invalid"
            )
    stages = [str(value) for value in object_list(materialized.get("stages_completed"))]
    if stages != list(AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_STAGES[: len(stages)]):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement stages are invalid"
        )
    _assert_probes(materialized.get("probes"))
    probe_count = materialized.get("tool_version_probe_subprocess_count")
    if (
        isinstance(probe_count, bool)
        or not isinstance(probe_count, int)
        or probe_count != len(object_list(materialized.get("probes")))
        or not 0 <= probe_count <= 3
    ):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation subprocess accounting is invalid"
        )
    if state == "measured_success":
        _assert_success(materialized)
    elif state == "measured_failed":
        _assert_failure(materialized)
    else:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement state is invalid"
        )
    _assert_identity(materialized)


def serialize_av1_validation_v4_preparation_measurement(
    payload: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_v4_preparation_measurement(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def load_av1_validation_v4_preparation_measurement(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement is unreadable"
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement bytes are not canonical"
        )
    assert_av1_validation_v4_preparation_measurement(payload)
    return payload


def av1_validation_v4_preparation_measurement_id(
    payload: Mapping[str, Any],
) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"measurement_id", "payload_sha256"}
    }
    return f"av1vprepmeas4_{stable_json_hash(semantic)[:32]}"


def _common_payload(
    *,
    state: str,
    claim: Mapping[str, Any],
    preparation_grant: Mapping[str, Any],
    started_at: str,
    completed_at: str,
    probes: Sequence[Mapping[str, Any]],
    stages_completed: Sequence[str],
) -> dict[str, Any]:
    grant = object_dict(preparation_grant)
    claim_payload = object_dict(claim)
    try:
        assert_av1_validation_v4_preparation_grant(grant)
        assert_av1_validation_v4_preparation_claim(claim_payload)
    except (
        AV1ValidationV4PreparationGrantError,
        AV1ValidationV4PreparationClaimError,
    ) as exc:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement inputs are invalid"
        ) from exc
    if (
        claim_payload.get("preparation_grant_id") != grant.get("grant_id")
        or claim_payload.get("preparation_grant_payload_sha256")
        != grant.get("payload_sha256")
    ):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement claim binding is invalid"
        )
    authorized_at = _parse_timestamp(grant.get("authorized_at"))
    valid_until = _parse_timestamp(grant.get("valid_until"))
    started = _parse_timestamp(started_at)
    claimed = _parse_timestamp(claim_payload.get("claimed_at"))
    completed = _parse_timestamp(completed_at)
    if not authorized_at <= started <= claimed <= completed:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement timeline is invalid"
        )
    if state == "measured_success" and completed >= valid_until:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 successful preparation completed outside its grant window"
        )
    payload: dict[str, Any] = {
        "schema": AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": state,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "preparation_grant_id": grant.get("grant_id"),
        "preparation_grant_payload_sha256": grant.get("payload_sha256"),
        "claim_id": claim_payload.get("claim_id"),
        "claim_payload_sha256": claim_payload.get("payload_sha256"),
        "started_at": started_at,
        "completed_at": completed_at,
        "stages_completed": list(stages_completed),
        "method": _METHOD,
        "probes": [object_dict(probe) for probe in probes],
        "tool_version_probe_subprocess_count": len(probes),
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    return payload


def _assert_probes(value: Any) -> None:
    probes = [object_dict(item) for item in object_list(value)]
    if len(probes) > 3:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation probe count is invalid"
        )
    expected_argv = _METHOD["tool_version_probe_argv"]
    seen: set[str] = set()
    for probe in probes:
        if set(probe) != {
            "argv",
            "binary_sha256",
            "duration_ms",
            "exit_code",
            "stdout_first_line",
            "tool",
            "truncated",
        }:
            raise AV1ValidationV4PreparationMeasurementError(
                "AV1 v4 preparation probe shape is invalid"
            )
        tool = str(probe.get("tool") or "")
        if tool in seen or probe.get("argv") != expected_argv.get(tool):
            raise AV1ValidationV4PreparationMeasurementError(
                "AV1 v4 preparation probe argv is invalid"
            )
        seen.add(tool)
        if (
            probe.get("exit_code") != 0
            or not isinstance(probe.get("duration_ms"), int)
            or probe["duration_ms"] < 0
            or probe.get("truncated") is not False
            or not _SHA256_RE.fullmatch(str(probe.get("binary_sha256") or ""))
            or not isinstance(probe.get("stdout_first_line"), str)
            or not str(probe["stdout_first_line"]).strip()
        ):
            raise AV1ValidationV4PreparationMeasurementError(
                "AV1 v4 preparation probe result is invalid"
            )


def _assert_success(payload: Mapping[str, Any]) -> None:
    if object_list(payload.get("stages_completed")) != list(
        AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_STAGES
    ) or len(object_list(payload.get("probes"))) != 3:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 successful measurement is incomplete"
        )
    if payload.get("guided_warm_start_count") != 4 or payload.get("invocation_count") != 8:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 successful measurement cardinality is invalid"
        )
    custody = object_dict(payload.get("key_custody"))
    if custody != {
        "measured": {
            "bytes": 32,
            "hard_link_count": 1,
            "mode": "0600",
            "regular_file": True,
        },
        "method": {
            "created_exclusive": True,
            "no_follow": True,
        },
        "policy": {
            "material_recorded": False,
            "persistence": "machine_local_only",
        },
    }:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 key custody is invalid"
        )
    constraints = object_dict(payload.get("source_stream_constraints"))
    if constraints != av1_validation_v4_source_stream_selection_payload():
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 source stream constraints are invalid"
        )
    if payload.get("failure") is not None or payload.get("rollback") is not None:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 successful measurement carries failure state"
        )
    if not _PREPARATION_ID_RE.fullmatch(str(payload.get("preparation_id") or "")):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 successful measurement preparation ID is invalid"
        )
    if not _SHA256_RE.fullmatch(
        str(payload.get("preparation_payload_sha256") or "")
    ):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 successful measurement preparation digest is invalid"
        )
    if not _PATH_PRIVACY_KEY_ID_RE.fullmatch(
        str(payload.get("path_privacy_key_id") or "")
    ) or not _RUNTIME_COMPATIBILITY_ID_RE.fullmatch(
        str(payload.get("runtime_compatibility_id") or "")
    ):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 successful measurement derived identity is invalid"
        )


def _assert_failure(payload: Mapping[str, Any]) -> None:
    failure = object_dict(payload.get("failure"))
    rollback = object_dict(payload.get("rollback"))
    if set(failure) != {"error_class", "message_sha256", "reason_code", "stage"}:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 failure measurement is invalid"
        )
    if failure.get("stage") not in AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_STAGES:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 failure stage is invalid"
        )
    if not _SHA256_RE.fullmatch(str(failure.get("message_sha256") or "")):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 failure message digest is invalid"
        )
    if (
        rollback.get("claim_retained") is not True
        or rollback.get("grant_consumed") is not True
        or not isinstance(rollback.get("preparation_record_retained"), bool)
        or set(rollback) != {
            "claim_retained",
            "grant_consumed",
            "path_privacy_key_id",
            "preparation_record_retained",
            "removed_artifacts",
            "retained_artifacts",
        }
    ):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 failure rollback state is invalid"
        )
    if payload.get("preparation_id") is not None:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 failure measurement cannot bind a preparation record"
        )
    for field in (
        "preparation_payload_sha256",
        "runtime_compatibility_id",
        "guided_warm_start_count",
        "invocation_count",
        "key_custody",
        "source_stream_constraints",
    ):
        if payload.get(field) is not None:
            raise AV1ValidationV4PreparationMeasurementError(
                f"AV1 v4 failure measurement {field} must be null"
            )
    removed = object_list(rollback.get("removed_artifacts"))
    retained = object_list(rollback.get("retained_artifacts"))
    if (
        any(not isinstance(value, str) for value in removed)
        or len(set(removed)) != len(removed)
        or not set(removed) <= _ROLLBACK_ARTIFACTS
        or any(not isinstance(value, str) for value in retained)
        or len(set(retained)) != len(retained)
        or not set(retained) <= _ROLLBACK_ARTIFACTS
        or set(removed) & set(retained)
        or rollback.get("preparation_record_retained")
        is not ("preparation_record" in retained)
    ):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 failure rollback artifact set is invalid"
        )
    key_id = rollback.get("path_privacy_key_id")
    if key_id is not None and not _PATH_PRIVACY_KEY_ID_RE.fullmatch(str(key_id)):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 failure rollback key identity is invalid"
        )
    completed_stages = object_list(payload.get("stages_completed"))
    expected_failure_index = len(completed_stages)
    if (
        expected_failure_index >= len(AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_STAGES)
        or failure.get("stage")
        != AV1_VALIDATION_V4_PREPARATION_MEASUREMENT_STAGES[expected_failure_index]
    ):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 failure stage does not follow completed stages"
        )


def _bind_and_validate(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["measurement_id"] = av1_validation_v4_preparation_measurement_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    materialized = json.loads(canonical_json_bytes(bound))
    assert_av1_validation_v4_preparation_measurement(materialized)
    return materialized


def _assert_identity(payload: Mapping[str, Any]) -> None:
    measurement_id = str(payload.get("measurement_id") or "")
    if not _MEASUREMENT_ID_RE.fullmatch(measurement_id):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement ID is invalid"
        )
    if measurement_id != av1_validation_v4_preparation_measurement_id(payload):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement ID does not match"
        )
    without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    if payload.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement digest does not match"
        )


def _assert_no_private_text(payload: Mapping[str, Any]) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and av1_validation_v4_contains_private_text(value):
            raise AV1ValidationV4PreparationMeasurementError(
                "AV1 v4 preparation measurement exposes machine-local text"
            )

    visit(payload)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement timestamp is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement timestamp is invalid"
        ) from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond != 0 or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1ValidationV4PreparationMeasurementError(
            "AV1 v4 preparation measurement timestamp must be canonical UTC seconds"
        )
    return normalized
