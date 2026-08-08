"""Pure terminal preparation measurement for AV1 protocol-v4 revision 3."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import json
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_PROTOCOL_VERSION,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_bundle import (
    av1_v4r3_effective_config_hmac_id,
    assert_av1_v4r3_preparation_bundle,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_config import (
    assert_av1_v4r3_effective_config_snapshot,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody import (
    assert_av1_v4r3_path_privacy_key_custody,
    assert_av1_v4r3_preparation_claim,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_grant import (
    assert_av1_v4r3_preparation_grant,
)


AV1_V4R3_PREPARATION_MEASUREMENT_SCHEMA = (
    "mediaforce.av1_cold_start_v4r3_preparation_measurement"
)
AV1_V4R3_PREPARATION_MEASUREMENT_SCHEMA_VERSION = 1
AV1_V4R3_PREPARATION_MEASUREMENT_CONTRACT_VERSION = "av1v4r3prepmeas1"
AV1_V4R3_PREPARATION_STAGES = (
    "validate_custody_chain",
    "start_terminal_attempt",
    "read_path_privacy_key",
    "build_effective_config",
    "measure_repository",
    "hash_toolchain",
    "probe_toolchain",
    "derive_private_identities",
    "build_prepared_bundle",
    "publish_terminal_measurement",
)
AV1_V4R3_PREPARATION_METHOD = {
    "builder_subprocess_count": 0,
    "database_access_performed": False,
    "media_bytes_read_count": 0,
    "media_paths_opened_count": 0,
    "media_paths_statted_count": 0,
    "media_processing_subprocess_count": 0,
    "network_access_performed": False,
    "tool_binary_digest_algorithm": "sha256_whole_file",
    "tool_version_probe_argv": {
        "ab_av1": ["--version"],
        "ffmpeg": ["-version"],
        "ffprobe": ["-version"],
    },
    "tool_version_probe_timeout_seconds_max": 10,
    "tool_version_string_normalization": "first_line_stripped",
}

_MEASUREMENT_ID_RE = re.compile(r"av1v4r3prepmeas_[0-9a-f]{32}\Z")
_CONFIG_HMAC_ID_RE = re.compile(r"av1v4r3confighmac_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ERROR_CLASS_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,127}\Z")
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_COMMON_KEYS = (
    frozenset(
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "manifest_id",
            "manifest_payload_sha256",
            "measurement_id",
            "payload_sha256",
            "state",
            "preparation_grant_id",
            "preparation_grant_payload_sha256",
            "claim_id",
            "claim_payload_sha256",
            "custody_id",
            "custody_payload_sha256",
            "started_at",
            "completed_at",
            "stages_completed",
            "probes",
            "method",
            "resume_allowed",
            "substitution_allowed",
            "backfill_allowed",
            "selection_or_partition_use_allowed",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)


class AV1V4R3PreparationMeasurementError(ValueError):
    """Raised when a revision-3 terminal measurement is invalid."""


def build_av1_v4r3_preparation_success_measurement(
    *,
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    key_custody: Mapping[str, Any],
    effective_config: Mapping[str, Any],
    preparation_bundle: Mapping[str, Any],
    path_privacy_key: bytes,
    started_at: str,
    completed_at: str,
    probes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    config = object_dict(effective_config)
    bundle = object_dict(preparation_bundle)
    assert_av1_v4r3_effective_config_snapshot(config)
    assert_av1_v4r3_preparation_bundle(bundle)
    config_hmac_id = av1_v4r3_effective_config_hmac_id(path_privacy_key, config)
    if bundle.get("effective_config_hmac_id") != config_hmac_id:
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 success measurement bundle binding is invalid"
        )
    payload = {
        **_common_payload(
            state="terminal_success",
            preparation_grant=preparation_grant,
            preparation_claim=preparation_claim,
            key_custody=key_custody,
            started_at=started_at,
            completed_at=completed_at,
            stages_completed=AV1_V4R3_PREPARATION_STAGES,
            probes=probes,
        ),
        "effective_config_hmac_id": config_hmac_id,
        "preparation_bundle_id": bundle["bundle_id"],
        "preparation_bundle_payload_sha256": bundle["payload_sha256"],
        "failure": None,
    }
    return _validated_bound(payload)


def build_av1_v4r3_preparation_failure_measurement(
    *,
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    key_custody: Mapping[str, Any],
    started_at: str,
    completed_at: str,
    stages_completed: Sequence[str],
    probes: Sequence[Mapping[str, Any]],
    failure_stage: str,
    error_class: str,
    error_code: str,
    message_sha256: str,
) -> dict[str, Any]:
    payload = {
        **_common_payload(
            state="terminal_failure",
            preparation_grant=preparation_grant,
            preparation_claim=preparation_claim,
            key_custody=key_custody,
            started_at=started_at,
            completed_at=completed_at,
            stages_completed=stages_completed,
            probes=probes,
        ),
        "effective_config_hmac_id": None,
        "preparation_bundle_id": None,
        "preparation_bundle_payload_sha256": None,
        "failure": {
            "stage": failure_stage,
            "error_class": error_class,
            "error_code": error_code,
            "message_sha256": message_sha256,
        },
    }
    return _validated_bound(payload)


def assert_av1_v4r3_preparation_measurement(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    allowed = _COMMON_KEYS | {
        "effective_config_hmac_id",
        "preparation_bundle_id",
        "preparation_bundle_payload_sha256",
        "failure",
    }
    if set(materialized) != allowed:
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation measurement shape is invalid"
        )
    expected = {
        "schema": AV1_V4R3_PREPARATION_MEASUREMENT_SCHEMA,
        "schema_version": AV1_V4R3_PREPARATION_MEASUREMENT_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_PREPARATION_MEASUREMENT_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "method": AV1_V4R3_PREPARATION_METHOD,
        "resume_allowed": False,
        "substitution_allowed": False,
        "backfill_allowed": False,
        "selection_or_partition_use_allowed": False,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation measurement binding is invalid"
        )
    _assert_no_private_text(materialized)
    _assert_timestamps(materialized)
    _assert_stages(materialized)
    _assert_probes(materialized.get("probes"))
    _assert_terminal_state(materialized)
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R3PreparationMeasurementError(
                f"AV1 v4 r3 preparation measurement cannot authorize {field}"
            )
    for field in (
        "manifest_payload_sha256",
        "preparation_grant_payload_sha256",
        "claim_payload_sha256",
        "custody_payload_sha256",
        "payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(materialized.get(field) or "")):
            raise AV1V4R3PreparationMeasurementError(
                f"AV1 v4 r3 preparation measurement {field} is invalid"
            )
    measurement_id = str(materialized.get("measurement_id") or "")
    if not _MEASUREMENT_ID_RE.fullmatch(
        measurement_id
    ) or measurement_id != _measurement_id(materialized):
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation measurement ID is invalid"
        )
    without_sha = {
        key: value for key, value in materialized.items() if key != "payload_sha256"
    }
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation measurement digest does not match"
        )


def serialize_av1_v4r3_preparation_measurement(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_preparation_measurement(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_preparation_measurement(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation measurement bytes are unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation measurement bytes are not canonical"
        )
    assert_av1_v4r3_preparation_measurement(payload)
    return payload


def _common_payload(
    *,
    state: str,
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    key_custody: Mapping[str, Any],
    started_at: str,
    completed_at: str,
    stages_completed: Sequence[str],
    probes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    grant = object_dict(preparation_grant)
    claim = object_dict(preparation_claim)
    custody = object_dict(key_custody)
    assert_av1_v4r3_preparation_grant(grant)
    assert_av1_v4r3_preparation_claim(claim)
    assert_av1_v4r3_path_privacy_key_custody(custody)
    if (
        claim.get("preparation_grant_id") != grant.get("grant_id")
        or claim.get("preparation_grant_payload_sha256") != grant.get("payload_sha256")
        or custody.get("claim_id") != claim.get("claim_id")
        or custody.get("claim_payload_sha256") != claim.get("payload_sha256")
    ):
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation measurement custody chain is invalid"
        )
    return {
        "schema": AV1_V4R3_PREPARATION_MEASUREMENT_SCHEMA,
        "schema_version": AV1_V4R3_PREPARATION_MEASUREMENT_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_PREPARATION_MEASUREMENT_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "state": state,
        "preparation_grant_id": grant["grant_id"],
        "preparation_grant_payload_sha256": grant["payload_sha256"],
        "claim_id": claim["claim_id"],
        "claim_payload_sha256": claim["payload_sha256"],
        "custody_id": custody["custody_id"],
        "custody_payload_sha256": custody["payload_sha256"],
        "started_at": started_at,
        "completed_at": completed_at,
        "stages_completed": list(stages_completed),
        "probes": [dict(object_dict(probe)) for probe in probes],
        "method": AV1_V4R3_PREPARATION_METHOD,
        "resume_allowed": False,
        "substitution_allowed": False,
        "backfill_allowed": False,
        "selection_or_partition_use_allowed": False,
        **{field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS},
    }


def _assert_terminal_state(payload: Mapping[str, Any]) -> None:
    state = payload.get("state")
    failure = payload.get("failure")
    binding_fields = (
        "effective_config_hmac_id",
        "preparation_bundle_id",
        "preparation_bundle_payload_sha256",
    )
    if state == "terminal_success":
        if failure is not None or any(
            not payload.get(field) for field in binding_fields
        ):
            raise AV1V4R3PreparationMeasurementError(
                "AV1 v4 r3 success measurement binding is invalid"
            )
        if len(object_list(payload.get("probes"))) != 3:
            raise AV1V4R3PreparationMeasurementError(
                "AV1 v4 r3 success measurement requires all three tool probes"
            )
        if not _CONFIG_HMAC_ID_RE.fullmatch(
            str(payload.get("effective_config_hmac_id") or "")
        ):
            raise AV1V4R3PreparationMeasurementError(
                "AV1 v4 r3 success measurement config binding is invalid"
            )
        for field in ("preparation_bundle_payload_sha256",):
            if not _SHA256_RE.fullmatch(str(payload.get(field) or "")):
                raise AV1V4R3PreparationMeasurementError(
                    "AV1 v4 r3 success measurement digest is invalid"
                )
        if object_list(payload.get("stages_completed")) != list(
            AV1_V4R3_PREPARATION_STAGES
        ):
            raise AV1V4R3PreparationMeasurementError(
                "AV1 v4 r3 success measurement is incomplete"
            )
        return
    if state != "terminal_failure" or any(
        payload.get(field) is not None for field in binding_fields
    ):
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 failure measurement binding is invalid"
        )
    failure_payload = object_dict(failure)
    if set(failure_payload) != {
        "stage",
        "error_class",
        "error_code",
        "message_sha256",
    }:
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 failure measurement shape is invalid"
        )
    if (
        failure_payload.get("stage") not in AV1_V4R3_PREPARATION_STAGES
        or not _ERROR_CLASS_RE.fullmatch(str(failure_payload.get("error_class") or ""))
        or not _ERROR_CODE_RE.fullmatch(str(failure_payload.get("error_code") or ""))
        or not _SHA256_RE.fullmatch(str(failure_payload.get("message_sha256") or ""))
    ):
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 failure measurement detail is invalid"
        )


def _assert_stages(payload: Mapping[str, Any]) -> None:
    stages = [str(stage) for stage in object_list(payload.get("stages_completed"))]
    if stages != list(AV1_V4R3_PREPARATION_STAGES[: len(stages)]):
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation measurement stages are invalid"
        )
    failure = payload.get("failure")
    if payload.get("state") == "terminal_failure" and isinstance(failure, Mapping):
        failure_stage = failure.get("stage")
        if failure_stage in stages:
            raise AV1V4R3PreparationMeasurementError(
                "AV1 v4 r3 failure stage cannot be marked completed"
            )
        if len(stages) >= len(AV1_V4R3_PREPARATION_STAGES):
            raise AV1V4R3PreparationMeasurementError(
                "AV1 v4 r3 failure measurement cannot complete every stage"
            )
        if failure_stage != AV1_V4R3_PREPARATION_STAGES[len(stages)]:
            raise AV1V4R3PreparationMeasurementError(
                "AV1 v4 r3 failure stage is not the next stage"
            )


def _assert_probes(value: Any) -> None:
    probes = object_list(value)
    if len(probes) > 3:
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation probe count is invalid"
        )
    expected_names = list(AV1_V4R3_PREPARATION_METHOD["tool_version_probe_argv"])
    for index, item in enumerate(probes):
        probe = object_dict(item)
        if set(probe) != {"tool", "argv", "version", "binary_sha256"}:
            raise AV1V4R3PreparationMeasurementError(
                "AV1 v4 r3 preparation probe shape is invalid"
            )
        tool = probe.get("tool")
        version = probe.get("version")
        if (
            index >= len(expected_names)
            or tool != expected_names[index]
            or probe.get("argv")
            != AV1_V4R3_PREPARATION_METHOD["tool_version_probe_argv"][tool]
            or not _SHA256_RE.fullmatch(str(probe.get("binary_sha256") or ""))
            or not isinstance(version, str)
            or not version
            or "\n" in version
            or "\r" in version
        ):
            raise AV1V4R3PreparationMeasurementError(
                "AV1 v4 r3 preparation probe binding is invalid"
            )


def _assert_timestamps(payload: Mapping[str, Any]) -> None:
    started = _parse_timestamp(payload.get("started_at"))
    completed = _parse_timestamp(payload.get("completed_at"))
    if completed < started:
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation measurement timestamps are out of order"
        )


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation timestamp is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation timestamp is invalid"
        ) from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation timestamp must use canonical UTC seconds"
        )
    return normalized


def _assert_no_private_text(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_no_private_text(str(key))
            _assert_no_private_text(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_private_text(child)
    elif isinstance(value, str) and av1_validation_v4_contains_private_text(value):
        raise AV1V4R3PreparationMeasurementError(
            "AV1 v4 r3 preparation measurement exposes machine-local text"
        )


def _measurement_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"measurement_id", "payload_sha256"}
    }
    return f"av1v4r3prepmeas_{stable_json_hash(semantic)[:32]}"


def _validated_bound(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["measurement_id"] = _measurement_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    materialized = json.loads(canonical_json_bytes(bound))
    assert_av1_v4r3_preparation_measurement(materialized)
    return materialized
