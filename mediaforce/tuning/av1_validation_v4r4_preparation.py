"""Revision-4 preparation contracts for the AV1 cold-start flow.

This module is deliberately path-free at the public artifact boundary. Private
machine-local paths may appear only inside the owner-only effective-config
snapshot that lives under custody in the preparation registry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r4_contract import (
    AV1V4R4ContractError,
    AV1_V4R4_CONFIGURATIONS,
    AV1_V4R4_EXPERIMENT_ID,
    AV1_V4R4_MANIFEST_APPROVED_AT,
    AV1_V4R4_MANIFEST_ID,
    AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
    AV1_V4R4_MANIFEST_REVISION,
    AV1_V4R4_POLICY_VALUES,
    AV1_V4R4_POLICY_VALUES_SHA256,
    AV1_V4R4_PROTOCOL_VERSION,
    AV1_V4R4_SOURCE_LAYOUT,
    av1_v4r4_identity_domain,
    av1_v4r4_ordinal_layout,
)
from mediaforce.tuning.av1_validation_v4r3_rights import (
    AV1V4R3RightsError,
    assert_av1_v4r3_rights_attestation,
)
from mediaforce.tuning.av1_validation_v4r4_invocation import (
    av1_v4r4_runner_invocation_sha256,
)


AV1_V4R4_RIGHTS_SCHEMA = "mediaforce.av1_cold_start_v4r4_rights_attestation"
AV1_V4R4_RIGHTS_SCHEMA_VERSION = 1
AV1_V4R4_RIGHTS_CONTRACT_VERSION = "av1v4r4rights1"
AV1_V4R4_RIGHTS_STATE = "owner_attested"
AV1_V4R4_RIGHTS_REAFFIRMED_AT = AV1_V4R4_MANIFEST_APPROVED_AT

AV1_V4R4_PREPARATION_REGISTRY_BINDING_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_preparation_registry_binding"
)
AV1_V4R4_PREPARATION_REGISTRY_BINDING_SCHEMA_VERSION = 1
AV1_V4R4_PREPARATION_REGISTRY_BINDING_CONTRACT_VERSION = "av1v4r4pregbind1"

AV1_V4R4_PREPARATION_GRANT_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_preparation_grant"
)
AV1_V4R4_PREPARATION_GRANT_SCHEMA_VERSION = 1
AV1_V4R4_PREPARATION_GRANT_CONTRACT_VERSION = "av1v4r4prepgrant1"
AV1_V4R4_PREPARATION_GRANT_STATE = "owner_authorized"
AV1_V4R4_PREPARATION_GRANT_AUTHORITY = (
    "av1_v4r4_preparation_non_media_custody"
)
AV1_V4R4_PREPARATION_GRANT_MAX_SECONDS = 86_400
AV1_V4R4_PREPARATION_CONSUMPTION_REGISTRY_TOKEN = (
    "av1v4r4prepregistry_334f8709c54f4e8b53c494e387caaf9d"
)

AV1_V4R4_PREPARATION_CLAIM_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_preparation_claim"
)
AV1_V4R4_PREPARATION_CLAIM_SCHEMA_VERSION = 1
AV1_V4R4_PREPARATION_CLAIM_CONTRACT_VERSION = "av1v4r4prepclaim1"
AV1_V4R4_PREPARATION_CLAIM_STATE = "grant_consumed"

AV1_V4R4_PATH_PRIVACY_KEY_CUSTODY_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_path_privacy_key_custody"
)
AV1_V4R4_PATH_PRIVACY_KEY_CUSTODY_SCHEMA_VERSION = 1
AV1_V4R4_PATH_PRIVACY_KEY_CUSTODY_CONTRACT_VERSION = "av1v4r4keycustody1"
AV1_V4R4_PATH_PRIVACY_KEY_CUSTODY_STATE = "key_created"

AV1_V4R4_EFFECTIVE_CONFIG_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_effective_config_snapshot"
)
AV1_V4R4_EFFECTIVE_CONFIG_SCHEMA_VERSION = 1
AV1_V4R4_EFFECTIVE_CONFIG_CONTRACT_VERSION = "av1v4r4config1"

AV1_V4R4_PREPARATION_BUNDLE_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_preparation_bundle"
)
AV1_V4R4_PREPARATION_BUNDLE_SCHEMA_VERSION = 1
AV1_V4R4_PREPARATION_BUNDLE_CONTRACT_VERSION = "av1v4r4prepbundle1"
AV1_V4R4_PREPARATION_BUNDLE_STATE = "prepared_unfrozen"

AV1_V4R4_PREPARATION_MEASUREMENT_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_preparation_measurement"
)
AV1_V4R4_PREPARATION_MEASUREMENT_SCHEMA_VERSION = 1
AV1_V4R4_PREPARATION_MEASUREMENT_CONTRACT_VERSION = "av1v4r4prepmeas1"
AV1_V4R4_PREPARATION_STAGES = (
    "validate_custody_chain",
    "reverify_source_digests",
    "build_effective_config",
    "measure_repository",
    "hash_toolchain",
    "probe_toolchain",
    "derive_private_identities",
    "build_prepared_bundle",
    "publish",
)
AV1_V4R4_PREPARATION_METHOD: Mapping[str, Any] = MappingProxyType(
    {
        "source_digest_algorithm": "sha256_whole_file",
        "source_digest_reverification_count": 4,
        "builder_subprocess_count": 0,
        "media_bytes_read_authorized_scope": "source_digest_reverification_only",
        "media_processing_subprocess_count": 0,
        "network_access_performed": False,
        "repository_identity_source": "caller_supplied_verified_against_live_clean_repo",
        "tool_version_probe_argv": {
            "ab_av1": ["--version"],
            "ffmpeg": ["-version"],
            "ffprobe": ["-version"],
        },
        "tool_version_probe_subprocess_count_max": 3,
        "tool_version_probe_timeout_seconds_max": 10,
        "tool_version_string_normalization": "first_line_stripped",
    }
)

AV1_V4R4_FREEZE_SCHEMA = "mediaforce.av1_cold_start_v4r4_owner_freeze"
AV1_V4R4_FREEZE_SCHEMA_VERSION = 1
AV1_V4R4_FREEZE_CONTRACT_VERSION = "av1v4r4freeze1"
AV1_V4R4_FREEZE_STATE = "owner_frozen_materialized"
AV1_V4R4_FREEZE_AUTHORITY = "av1_v4r4_owner_freeze"
AV1_V4R4_FREEZE_APPROVAL_FIELD = "manifest_revision_4_owner_freeze_approved"
AV1_V4R4_FREEZE_SCOPE: Mapping[str, object] = MappingProxyType(
    {
        "sequencing_grant_creation_authorized": True,
        "sequencing_claim_creation_authorized": False,
        "execution_grant_creation_authorized": False,
        "manifest_payload_mutation_authorized": False,
        "qualification_request_creation_authorized": False,
        "retry_authorized": False,
        "reviewed_artifact_set": (
            "rights_grant_claim_key_custody_prepared_bundle_terminal_measurement"
        ),
        "next_owner_decision": "execution_authority",
    }
)

AV1_V4R4_QUALIFICATION_REQUEST_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_qualification_request"
)
AV1_V4R4_QUALIFICATION_REQUEST_SCHEMA_VERSION = 1
AV1_V4R4_QUALIFICATION_REQUEST_CONTRACT_VERSION = "av1v4r4qreq1"
AV1_V4R4_QUALIFICATION_REQUEST_STATE = "owner_submitted_non_authorizing"
AV1_V4R4_QUALIFICATION_REQUEST_AUTHORITY = (
    "av1_v4_manifest_revision_4_qualification_request"
)
AV1_V4R4_QUALIFICATION_REQUEST_APPROVAL_FIELD = (
    "manifest_revision_4_qualification_request_approved"
)
AV1_V4R4_QUALIFICATION_REQUEST_MAX_SUBMISSION_LAG_SECONDS = 86_400
AV1_V4R4_QUALIFICATION_REQUEST_TTL_SECONDS = 172_800
AV1_V4R4_QUALIFICATION_REQUEST_SCOPE: Mapping[str, object] = MappingProxyType(
    {
        "sequencing_grant_creation_authorized": True,
        "sequencing_claim_creation_authorized": False,
        "execution_grant_creation_authorized": False,
        "execution_claim_creation_authorized": False,
        "retry_authorized": False,
        "resume_authorized": False,
        "substitution_authorized": False,
        "backfill_authorized": False,
        "manifest_payload_mutation_authorized": False,
        "reviewed_artifact_set": "owner_frozen_revision_4_preparation_cohort",
        "requested_activity": "owner_execution_authority_planning_only",
    }
)

AV1_V4R4_EXECUTION_PREFLIGHT_SCHEMA = (
    "mediaforce.av1_cold_start_v4r4_execution_preflight"
)
AV1_V4R4_EXECUTION_PREFLIGHT_SCHEMA_VERSION = 1
AV1_V4R4_EXECUTION_PREFLIGHT_CONTRACT_VERSION = "av1v4r4preflight1"
AV1_V4R4_EXECUTION_PREFLIGHT_STATE = "owner_execution_ready_non_authorizing"
AV1_V4R4_EXECUTION_PREFLIGHT_AUTHORITY = (
    "av1_v4_manifest_revision_4_execution_preflight"
)
AV1_V4R4_EXECUTION_PREFLIGHT_APPROVAL_FIELD = (
    "manifest_revision_4_execution_preflight_approved"
)
AV1_V4R4_EXECUTION_PREFLIGHT_READINESS_STATE = (
    "ready_pending_owner_execution_authority_decision"
)
AV1_V4R4_EXECUTION_PREFLIGHT_SCOPE: Mapping[str, object] = MappingProxyType(
    {
        "sequencing_grant_creation_authorized": True,
        "sequencing_claim_creation_authorized": False,
        "execution_grant_creation_authorized": False,
        "execution_claim_creation_authorized": False,
        "media_read_authorized": False,
        "runtime_execution_authorized": False,
        "retry_authorized": False,
        "resume_authorized": False,
        "substitution_authorized": False,
        "backfill_authorized": False,
        "manifest_payload_mutation_authorized": False,
        "reviewed_artifact_set": "owner_frozen_revision_4_preparation_cohort",
        "requested_activity": "execution_authority_decision_only",
        "next_owner_decision": "publish_execution_grant_or_abort",
    }
)

AV1_V4R4_INSTANCE_PATH_ROLES = frozenset(
    {"runtime_lock", "source_root", "state_root", "temp_root"}
)

AV1_V4R4_PREPARATION_SOURCE_SPECS: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "av1v4_animation_primary_sintel": MappingProxyType(
            {
                "content_class": "animation_content",
                "role": "primary",
                "media_sha256": (
                    "sha256:97f1dbc66231df42ad49bd8c29aa174b8f48933058e47e7157d4ba63d93a8efa"
                ),
                "width": 1920,
                "height": 818,
                "source_codec": "h264",
                "video_stream_index": 0,
                "excluded_stream_indexes": tuple(range(1, 12)),
                "target_size_bytes": 98_670_222,
                "source_cap_total_bytes": 944_072_472,
            }
        ),
        "av1v4_live_action_primary_tears_of_steel": MappingProxyType(
            {
                "content_class": "live_action_content",
                "role": "primary",
                "media_sha256": (
                    "sha256:bd2b5bc6c16d4085034f47ef7e4b3938afe86b4eec4ac3cf2685367d3b0b23b0"
                ),
                "width": 1920,
                "height": 800,
                "source_codec": "h264",
                "video_stream_index": 0,
                "excluded_stream_indexes": (1,),
                "target_size_bytes": 81_574_074,
                "source_cap_total_bytes": 591_101_064,
            }
        ),
        "av1v4_animation_confirmation_cosmos_laundromat": MappingProxyType(
            {
                "content_class": "animation_content",
                "role": "confirmation",
                "media_sha256": (
                    "sha256:96ace08a4d61878cfb7cc8b850b7f5aa7ba304de45017eaf832ccd3d52893130"
                ),
                "width": 2048,
                "height": 858,
                "source_codec": "h264",
                "video_stream_index": 0,
                "excluded_stream_indexes": (1,),
                "target_size_bytes": 81_118_815,
                "source_cap_total_bytes": 392_996_027,
            }
        ),
        "av1v4_live_action_confirmation_nasa_earth_views": MappingProxyType(
            {
                "content_class": "live_action_content",
                "role": "confirmation",
                "media_sha256": (
                    "sha256:184b9ebaf6624b219455b0d4a72967e05d3e6cebc034ac8036fcc0198a17a8ef"
                ),
                "width": 1280,
                "height": 720,
                "source_codec": "h264",
                "video_stream_index": 0,
                "excluded_stream_indexes": (1,),
                "target_size_bytes": 22_305_618,
                "source_cap_total_bytes": 187_312_060,
            }
        ),
    }
)

_OWNER_PRINCIPAL_RE = re.compile(r"[a-z][a-z0-9_.:@-]{2,127}\Z")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")

_RIGHTS_ID_RE = re.compile(r"av1vrights4r4_[0-9a-f]{32}\Z")
_PRIOR_RIGHTS_ID_RE = re.compile(r"av1vrights4r3_[0-9a-f]{32}\Z")
_REGISTRY_BINDING_ID_RE = re.compile(r"av1v4r4pregbind_[0-9a-f]{32}\Z")
_GRANT_ID_RE = re.compile(r"av1vprepgrant4r4_[0-9a-f]{32}\Z")
_CLAIM_ID_RE = re.compile(r"av1v4r4prepclaim_[0-9a-f]{32}\Z")
_CUSTODY_ID_RE = re.compile(r"av1v4r4keycustody_[0-9a-f]{32}\Z")
_KEY_ID_RE = re.compile(r"av1vpathkey4r4_[0-9a-f]{32}\Z")
_CONFIG_ID_RE = re.compile(r"av1v4r4config_[0-9a-f]{32}\Z")
_CONFIG_HMAC_ID_RE = re.compile(r"av1v4r4confighmac_[0-9a-f]{32}\Z")
_INVOCATION_CONFIG_HMAC_ID_RE = re.compile(r"av1v4r4invconfighmac_[0-9a-f]{32}\Z")
_BUNDLE_ID_RE = re.compile(r"av1v4r4prepbundle_[0-9a-f]{32}\Z")
_MEASUREMENT_ID_RE = re.compile(r"av1v4r4prepmeas_[0-9a-f]{32}\Z")
_FREEZE_ID_RE = re.compile(r"av1vfreeze4r4_[0-9a-f]{32}\Z")
_REQUEST_ID_RE = re.compile(r"av1v4r4req_[0-9a-f]{32}\Z")
_PREFLIGHT_ID_RE = re.compile(r"av1v4r4preflight_[0-9a-f]{32}\Z")
_CLOSURE_ID_RE = re.compile(r"av1vclosure4r4_[0-9a-f]{32}\Z")
_INVOCATION_SHA_ID_RE = _SHA256_RE
_TOOLCHAIN_ID_RE = re.compile(r"av1v4r4toolchain_[0-9a-f]{32}\Z")
_RUNTIME_ID_RE = re.compile(r"av1v4r4runtime_[0-9a-f]{32}\Z")
_PATH_HMAC_ID_RE = re.compile(r"av1vpath4r4_[0-9a-f]{32}\Z")
_SOURCE_HMAC_ID_RE = re.compile(r"av1vsource4r4_[0-9a-f]{32}\Z")
_QUALITY_TEMP_HMAC_ID_RE = re.compile(r"av1v4r4qtemp_[0-9a-f]{32}\Z")
_ORDINAL_REGISTRY_ID_RE = re.compile(r"av1v4r4ordreg_[0-9a-f]{64}\Z")
_ORDINAL_PLAN_ID_RE = re.compile(r"av1v4r4ordplan_[0-9a-f]{32}\Z")
_ORDINAL_GRANT_ID_RE = re.compile(r"av1v4r4ordgrant_[0-9a-f]{32}\Z")


class AV1V4R4PreparationError(ValueError):
    """Raised when a revision-4 preparation artifact is invalid."""


def av1_v4r4_path_privacy_key_id(key: bytes) -> str:
    _assert_key_bytes(key)
    return "av1vpathkey4r4_" + _hmac_hex(
        key,
        {
            "domain": av1_v4r4_identity_domain("path-privacy-key-id"),
            "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
            "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        },
    )[:32]


def av1_v4r4_instance_path_hmac_id(
    key: bytes,
    *,
    role: str,
    normalized_path: str,
) -> str:
    _assert_key_bytes(key)
    if role not in AV1_V4R4_INSTANCE_PATH_ROLES:
        raise AV1V4R4PreparationError("AV1 v4 r4 instance path role is invalid")
    path = _canonical_absolute_posix_path(normalized_path)
    return "av1vpath4r4_" + _hmac_hex(
        key,
        {
            "domain": av1_v4r4_identity_domain("instance-path-hmac"),
            "role": role,
            "normalized_path": path,
        },
    )[:32]


def av1_v4r4_source_path_hmac_id(
    key: bytes,
    *,
    asset_id: str,
    normalized_path: str,
) -> str:
    _assert_key_bytes(key)
    _source_spec(asset_id)
    path = _canonical_absolute_posix_path(normalized_path)
    return "av1vsource4r4_" + _hmac_hex(
        key,
        {
            "domain": av1_v4r4_identity_domain("source-path-hmac"),
            "asset_id": asset_id,
            "normalized_path": path,
        },
    )[:32]


def av1_v4r4_quality_temp_hmac_id(
    key: bytes,
    *,
    asset_id: str,
    normalized_path: str,
) -> str:
    _assert_key_bytes(key)
    _source_spec(asset_id)
    path = _canonical_absolute_posix_path(normalized_path)
    return "av1v4r4qtemp_" + _hmac_hex(
        key,
        {
            "domain": av1_v4r4_identity_domain("quality-temp-hmac"),
            "asset_id": asset_id,
            "normalized_path": path,
        },
    )[:32]


def av1_v4r4_effective_config_hmac_id(
    key: bytes,
    effective_config: Mapping[str, Any],
) -> str:
    _assert_key_bytes(key)
    assert_av1_v4r4_effective_config_snapshot(effective_config)
    return "av1v4r4confighmac_" + _hmac_hex(
        key,
        {
            "domain": av1_v4r4_identity_domain("effective-config-hmac"),
            "config": {
                item: value
                for item, value in object_dict(effective_config).items()
                if item not in {"config_id", "payload_sha256"}
            },
        },
    )[:32]


def av1_v4r4_invocation_config_hmac_id(
    key: bytes,
    *,
    ordinal: int,
    source_path: str,
    quality_temp_path: str,
    source_codec: str,
    width: int,
    height: int,
    invocation_sha256: str,
) -> str:
    _assert_key_bytes(key)
    _assert_sha(invocation_sha256, "invocation digest")
    if type(ordinal) is not int or not 1 <= ordinal <= len(av1_v4r4_ordinal_layout()):
        raise AV1V4R4PreparationError("AV1 v4 r4 invocation ordinal is invalid")
    source = _canonical_absolute_posix_path(source_path)
    quality_temp = _canonical_absolute_posix_path(quality_temp_path)
    spec = _source_spec(str(av1_v4r4_ordinal_layout()[ordinal - 1]["asset_id"]))
    if source_codec != spec["source_codec"] or width != spec["width"] or height != spec["height"]:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 invocation config stream facts are invalid"
        )
    return "av1v4r4invconfighmac_" + _hmac_hex(
        key,
        {
            "domain": av1_v4r4_identity_domain("invocation-config-hmac"),
            "ordinal": ordinal,
            "source_path": source,
            "quality_temp_path": quality_temp,
            "source_codec": source_codec,
            "width": width,
            "height": height,
            "invocation_sha256": invocation_sha256,
        },
    )[:32]


def build_av1_v4r4_rights_attestation(
    *,
    prior_revision_attestation: Mapping[str, Any],
) -> dict[str, Any]:
    prior = _normalized_prior_rights_attestation(prior_revision_attestation)
    payload = {
        **_common_payload(
            schema=AV1_V4R4_RIGHTS_SCHEMA,
            schema_version=AV1_V4R4_RIGHTS_SCHEMA_VERSION,
            contract_version=AV1_V4R4_RIGHTS_CONTRACT_VERSION,
        ),
        "state": AV1_V4R4_RIGHTS_STATE,
        "owner_attested": True,
        "owner_principal": prior["owner_principal"],
        "attested_at": AV1_V4R4_RIGHTS_REAFFIRMED_AT,
        "prior_revision_attestation": {
            "artifact": prior,
            "attestation_id": prior["attestation_id"],
            "payload_sha256": prior["payload_sha256"],
            "confers_revision_4_authority": False,
        },
        "terms": prior["terms"],
        "source_claims": prior["source_claims"],
        "derivative_profile_acknowledgement": {
            "owner_reaffirmed": True,
            "decision_scope": "execution_authority_preparation_only",
            "output_video_codec": "av1",
            "encoder": AV1_V4R4_POLICY_VALUES["encoder"],
            "output_container": "mkv",
            "video_only": True,
            "max_height": AV1_V4R4_POLICY_VALUES["max_height"],
            "reference_target_size_bytes": AV1_V4R4_POLICY_VALUES["target_size_bytes"],
            "nasa_acknowledgement_required_on_future_output_or_publication": True,
            "endorsement_prohibited": True,
            "redistribution_disposition": "none",
            "terms_documents_re_reviewed": False,
        },
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="attestation_id",
        id_prefix="av1vrights4r4_",
        domain=av1_v4r4_identity_domain("rights-attestation"),
    )
    assert_av1_v4r4_rights_attestation(bound)
    return bound


def assert_av1_v4r4_rights_attestation(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    expected = {
        "schema": AV1_V4R4_RIGHTS_SCHEMA,
        "schema_version": AV1_V4R4_RIGHTS_SCHEMA_VERSION,
        "contract_version": AV1_V4R4_RIGHTS_CONTRACT_VERSION,
        "state": AV1_V4R4_RIGHTS_STATE,
        "owner_attested": True,
        "attested_at": AV1_V4R4_RIGHTS_REAFFIRMED_AT,
    }
    _assert_fields(
        materialized,
        {
            *expected,
            "protocol_version",
            "manifest_revision",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "attestation_id",
            "payload_sha256",
            "owner_principal",
            "prior_revision_attestation",
            "terms",
            "source_claims",
            "derivative_profile_acknowledgement",
            *AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
        },
        "rights attestation",
    )
    _assert_common_binding(materialized, expected)
    _assert_owner_principal(materialized.get("owner_principal"), "rights owner")
    prior_wrapper = object_dict(materialized.get("prior_revision_attestation"))
    if set(prior_wrapper) != {
        "artifact",
        "attestation_id",
        "payload_sha256",
        "confers_revision_4_authority",
    }:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 rights prior attestation wrapper is invalid"
        )
    if prior_wrapper.get("confers_revision_4_authority") is not False:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 prior attestation cannot confer authority"
        )
    prior = _normalized_prior_rights_attestation(prior_wrapper.get("artifact"))
    if (
        prior_wrapper.get("attestation_id") != prior["attestation_id"]
        or prior_wrapper.get("payload_sha256") != prior["payload_sha256"]
    ):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 rights prior attestation binding is invalid"
        )
    if prior["owner_principal"] != materialized.get("owner_principal"):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 rights owner binding is invalid"
        )
    if materialized.get("terms") != prior["terms"] or materialized.get(
        "source_claims"
    ) != prior["source_claims"]:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 rights provenance binding is invalid"
        )
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(
        materialized,
        id_field="attestation_id",
        id_pattern=_RIGHTS_ID_RE,
        expected_id_prefix="av1vrights4r4_",
        domain=av1_v4r4_identity_domain("rights-attestation"),
    )


def build_av1_v4r4_preparation_registry_binding() -> dict[str, Any]:
    payload = {
        **_common_payload(
            schema=AV1_V4R4_PREPARATION_REGISTRY_BINDING_SCHEMA,
            schema_version=AV1_V4R4_PREPARATION_REGISTRY_BINDING_SCHEMA_VERSION,
            contract_version=AV1_V4R4_PREPARATION_REGISTRY_BINDING_CONTRACT_VERSION,
        ),
        "consumption_registry_token": AV1_V4R4_PREPARATION_CONSUMPTION_REGISTRY_TOKEN,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="registry_binding_id",
        id_prefix="av1v4r4pregbind_",
        domain=av1_v4r4_identity_domain("preparation-registry-binding"),
    )
    assert_av1_v4r4_preparation_registry_binding(bound)
    return bound


def assert_av1_v4r4_preparation_registry_binding(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    _assert_fields(
        materialized,
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "registry_binding_id",
            "payload_sha256",
            "consumption_registry_token",
            *AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
        },
        "registry binding",
    )
    _assert_common_binding(
        materialized,
        {
            "schema": AV1_V4R4_PREPARATION_REGISTRY_BINDING_SCHEMA,
            "schema_version": AV1_V4R4_PREPARATION_REGISTRY_BINDING_SCHEMA_VERSION,
            "contract_version": AV1_V4R4_PREPARATION_REGISTRY_BINDING_CONTRACT_VERSION,
        },
    )
    if (
        materialized.get("consumption_registry_token")
        != AV1_V4R4_PREPARATION_CONSUMPTION_REGISTRY_TOKEN
    ):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation registry token is invalid"
        )
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(
        materialized,
        id_field="registry_binding_id",
        id_pattern=_REGISTRY_BINDING_ID_RE,
        expected_id_prefix="av1v4r4pregbind_",
        domain=av1_v4r4_identity_domain("preparation-registry-binding"),
    )


def build_av1_v4r4_preparation_grant(
    *,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    repository_commit: str,
    repository_tree: str,
    authorized_at: str,
    valid_until: str,
) -> dict[str, Any]:
    rights = object_dict(rights_attestation)
    assert_av1_v4r4_rights_attestation(rights)
    if owner_principal != rights.get("owner_principal"):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation grant owner must match the rights attestation"
        )
    _assert_repository(
        {"commit": repository_commit, "tree": repository_tree},
        "grant repository",
    )
    _assert_window(
        authorized_at,
        valid_until,
        AV1_V4R4_PREPARATION_GRANT_MAX_SECONDS,
        "preparation grant",
    )
    payload = {
        **_common_payload(
            schema=AV1_V4R4_PREPARATION_GRANT_SCHEMA,
            schema_version=AV1_V4R4_PREPARATION_GRANT_SCHEMA_VERSION,
            contract_version=AV1_V4R4_PREPARATION_GRANT_CONTRACT_VERSION,
        ),
        "state": AV1_V4R4_PREPARATION_GRANT_STATE,
        "authority": AV1_V4R4_PREPARATION_GRANT_AUTHORITY,
        "rights_attestation_id": rights["attestation_id"],
        "rights_attestation_payload_sha256": rights["payload_sha256"],
        "rights_attested_at": rights["attested_at"],
        "owner_principal": owner_principal,
        "repository": {"commit": repository_commit, "tree": repository_tree},
        "consumption_registry_token": AV1_V4R4_PREPARATION_CONSUMPTION_REGISTRY_TOKEN,
        "authorized_at": authorized_at,
        "valid_until": valid_until,
        "operation_scope": {
            "ab_av1_version_probe_argv": ["--version"],
            "binary_hashing_authorized": True,
            "builder_subprocess_authorized": False,
            "config_hashing_authorized": True,
            "ffmpeg_version_probe_argv": ["-version"],
            "ffprobe_version_probe_argv": ["-version"],
            "grant_reuse_authorized": False,
            "invocation_digest_derivation_authorized": True,
            "key_bytes": 32,
            "key_file_create_exclusive_required": True,
            "key_file_mode": "0600",
            "key_material_disclosure_authorized": False,
            "key_persistence": "machine_local_only",
            "media_bytes_read_authorized": False,
            "media_processing_subprocess_authorized": False,
            "network_access_authorized": False,
            "ordinal_registry_binding_derivation_authorized": True,
            "path_identity_derivation_authorized": True,
            "path_privacy_key_creation_authorized": True,
            "prepared_bundle_creation_authorized": True,
            "qualification_request_creation_authorized": False,
            "repository_identity_measurement_authorized": True,
            "sequencing_claim_creation_authorized": False,
            "sequencing_grant_creation_authorized": False,
            "source_digest_reverification_authorized": True,
            "tool_version_probe_subprocess_authorized": True,
            "tool_version_probe_timeout_seconds_max": 10,
        },
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="grant_id",
        id_prefix="av1vprepgrant4r4_",
        domain=av1_v4r4_identity_domain("preparation-grant"),
    )
    assert_av1_v4r4_preparation_grant(bound)
    return bound


def assert_av1_v4r4_preparation_grant(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    _assert_fields(
        materialized,
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "grant_id",
            "payload_sha256",
            "state",
            "authority",
            "rights_attestation_id",
            "rights_attestation_payload_sha256",
            "rights_attested_at",
            "owner_principal",
            "repository",
            "consumption_registry_token",
            "authorized_at",
            "valid_until",
            "operation_scope",
            *AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
        },
        "preparation grant",
    )
    _assert_common_binding(
        materialized,
        {
            "schema": AV1_V4R4_PREPARATION_GRANT_SCHEMA,
            "schema_version": AV1_V4R4_PREPARATION_GRANT_SCHEMA_VERSION,
            "contract_version": AV1_V4R4_PREPARATION_GRANT_CONTRACT_VERSION,
            "state": AV1_V4R4_PREPARATION_GRANT_STATE,
            "authority": AV1_V4R4_PREPARATION_GRANT_AUTHORITY,
        },
    )
    if not _RIGHTS_ID_RE.fullmatch(str(materialized.get("rights_attestation_id") or "")):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation grant rights binding is invalid"
        )
    _assert_sha(materialized.get("rights_attestation_payload_sha256"), "rights attestation payload")
    _assert_owner_principal(materialized.get("owner_principal"), "preparation grant owner")
    _assert_repository(object_dict(materialized.get("repository")), "grant repository")
    if (
        materialized.get("consumption_registry_token")
        != AV1_V4R4_PREPARATION_CONSUMPTION_REGISTRY_TOKEN
    ):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation grant registry token is invalid"
        )
    _assert_window(
        str(materialized.get("authorized_at") or ""),
        str(materialized.get("valid_until") or ""),
        AV1_V4R4_PREPARATION_GRANT_MAX_SECONDS,
        "preparation grant",
    )
    scope = object_dict(materialized.get("operation_scope"))
    if (
        scope.get("ab_av1_version_probe_argv") != ["--version"]
        or scope.get("ffmpeg_version_probe_argv") != ["-version"]
        or scope.get("ffprobe_version_probe_argv") != ["-version"]
        or scope.get("key_bytes") != 32
        or scope.get("key_file_mode") != "0600"
        or scope.get("key_material_disclosure_authorized") is not False
        or scope.get("grant_reuse_authorized") is not False
        or scope.get("sequencing_grant_creation_authorized") is not False
        or scope.get("sequencing_claim_creation_authorized") is not False
    ):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation grant operation scope is invalid"
        )
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(
        materialized,
        id_field="grant_id",
        id_pattern=_GRANT_ID_RE,
        expected_id_prefix="av1vprepgrant4r4_",
        domain=av1_v4r4_identity_domain("preparation-grant"),
    )


def assert_av1_v4r4_preparation_grant_active(
    payload: Mapping[str, Any], *, as_of: str
) -> None:
    assert_av1_v4r4_preparation_grant(payload)
    observed = _parse_timestamp(as_of, "activity timestamp")
    authorized = _parse_timestamp(str(payload["authorized_at"]), "authorization")
    valid_until = _parse_timestamp(str(payload["valid_until"]), "expiration")
    if not authorized <= observed < valid_until:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation grant is not active"
        )


def build_av1_v4r4_preparation_claim(
    *,
    preparation_grant: Mapping[str, Any],
    rights_attestation: Mapping[str, Any],
    claimed_at: str,
) -> dict[str, Any]:
    grant = object_dict(preparation_grant)
    rights = object_dict(rights_attestation)
    assert_av1_v4r4_preparation_grant_active(grant, as_of=claimed_at)
    assert_av1_v4r4_rights_attestation(rights)
    if (
        grant["rights_attestation_id"] != rights["attestation_id"]
        or grant["rights_attestation_payload_sha256"] != rights["payload_sha256"]
        or grant["owner_principal"] != rights["owner_principal"]
    ):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation claim rights binding does not match"
        )
    payload = {
        **_common_payload(
            schema=AV1_V4R4_PREPARATION_CLAIM_SCHEMA,
            schema_version=AV1_V4R4_PREPARATION_CLAIM_SCHEMA_VERSION,
            contract_version=AV1_V4R4_PREPARATION_CLAIM_CONTRACT_VERSION,
        ),
        "state": AV1_V4R4_PREPARATION_CLAIM_STATE,
        "consumption_registry_token": grant["consumption_registry_token"],
        "preparation_grant_id": grant["grant_id"],
        "preparation_grant_payload_sha256": grant["payload_sha256"],
        "rights_attestation_id": rights["attestation_id"],
        "rights_attestation_payload_sha256": rights["payload_sha256"],
        "owner_principal": rights["owner_principal"],
        "repository": dict(object_dict(grant["repository"])),
        "claimed_at": claimed_at,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="claim_id",
        id_prefix="av1v4r4prepclaim_",
        domain=av1_v4r4_identity_domain("preparation-claim"),
    )
    assert_av1_v4r4_preparation_claim(bound)
    return bound


def assert_av1_v4r4_preparation_claim(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    _assert_fields(
        materialized,
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "claim_id",
            "payload_sha256",
            "state",
            "consumption_registry_token",
            "preparation_grant_id",
            "preparation_grant_payload_sha256",
            "rights_attestation_id",
            "rights_attestation_payload_sha256",
            "owner_principal",
            "repository",
            "claimed_at",
            *AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
        },
        "preparation claim",
    )
    _assert_common_binding(
        materialized,
        {
            "schema": AV1_V4R4_PREPARATION_CLAIM_SCHEMA,
            "schema_version": AV1_V4R4_PREPARATION_CLAIM_SCHEMA_VERSION,
            "contract_version": AV1_V4R4_PREPARATION_CLAIM_CONTRACT_VERSION,
            "state": AV1_V4R4_PREPARATION_CLAIM_STATE,
        },
    )
    if (
        materialized.get("consumption_registry_token")
        != AV1_V4R4_PREPARATION_CONSUMPTION_REGISTRY_TOKEN
        or not _GRANT_ID_RE.fullmatch(str(materialized.get("preparation_grant_id") or ""))
        or not _RIGHTS_ID_RE.fullmatch(str(materialized.get("rights_attestation_id") or ""))
    ):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation claim binding is invalid"
        )
    _assert_sha(materialized.get("preparation_grant_payload_sha256"), "preparation grant payload")
    _assert_sha(materialized.get("rights_attestation_payload_sha256"), "rights attestation payload")
    _assert_owner_principal(materialized.get("owner_principal"), "preparation claim owner")
    _assert_repository(object_dict(materialized.get("repository")), "claim repository")
    _parse_timestamp(str(materialized.get("claimed_at") or ""), "claimed_at")
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(
        materialized,
        id_field="claim_id",
        id_pattern=_CLAIM_ID_RE,
        expected_id_prefix="av1v4r4prepclaim_",
        domain=av1_v4r4_identity_domain("preparation-claim"),
    )


def build_av1_v4r4_path_privacy_key_custody(
    *,
    preparation_claim: Mapping[str, Any],
    key_id: str,
    created_at: str,
) -> dict[str, Any]:
    claim = object_dict(preparation_claim)
    assert_av1_v4r4_preparation_claim(claim)
    created = _parse_timestamp(created_at, "key custody created_at")
    if created < _parse_timestamp(str(claim["claimed_at"]), "claimed_at"):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 key custody created_at cannot precede claim"
        )
    if not _KEY_ID_RE.fullmatch(key_id):
        raise AV1V4R4PreparationError("AV1 v4 r4 path-privacy key ID is invalid")
    payload = {
        **_common_payload(
            schema=AV1_V4R4_PATH_PRIVACY_KEY_CUSTODY_SCHEMA,
            schema_version=AV1_V4R4_PATH_PRIVACY_KEY_CUSTODY_SCHEMA_VERSION,
            contract_version=AV1_V4R4_PATH_PRIVACY_KEY_CUSTODY_CONTRACT_VERSION,
        ),
        "state": AV1_V4R4_PATH_PRIVACY_KEY_CUSTODY_STATE,
        "claim_id": claim["claim_id"],
        "claim_payload_sha256": claim["payload_sha256"],
        "consumption_registry_token": claim["consumption_registry_token"],
        "created_at": created_at,
        "owner_principal": claim["owner_principal"],
        "claimed_at": claim["claimed_at"],
        "preparation_grant_id": claim["preparation_grant_id"],
        "preparation_grant_payload_sha256": claim["preparation_grant_payload_sha256"],
        "repository": dict(object_dict(claim["repository"])),
        "key_id": key_id,
        "key_bytes_length": 32,
        "key_file_create_exclusive": True,
        "key_file_mode": "0600",
        "key_material_serialized": False,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="custody_id",
        id_prefix="av1v4r4keycustody_",
        domain=av1_v4r4_identity_domain("path-privacy-key-custody"),
    )
    assert_av1_v4r4_path_privacy_key_custody(bound)
    return bound


def assert_av1_v4r4_path_privacy_key_custody(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    _assert_fields(
        materialized,
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "custody_id",
            "payload_sha256",
            "state",
            "claim_id",
            "claim_payload_sha256",
            "consumption_registry_token",
            "created_at",
            "owner_principal",
            "claimed_at",
            "preparation_grant_id",
            "preparation_grant_payload_sha256",
            "repository",
            "key_id",
            "key_bytes_length",
            "key_file_create_exclusive",
            "key_file_mode",
            "key_material_serialized",
            *AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
        },
        "key custody",
    )
    _assert_common_binding(
        materialized,
        {
            "schema": AV1_V4R4_PATH_PRIVACY_KEY_CUSTODY_SCHEMA,
            "schema_version": AV1_V4R4_PATH_PRIVACY_KEY_CUSTODY_SCHEMA_VERSION,
            "contract_version": AV1_V4R4_PATH_PRIVACY_KEY_CUSTODY_CONTRACT_VERSION,
            "state": AV1_V4R4_PATH_PRIVACY_KEY_CUSTODY_STATE,
            "key_bytes_length": 32,
            "key_file_create_exclusive": True,
            "key_file_mode": "0600",
            "key_material_serialized": False,
        },
    )
    if (
        not _CLAIM_ID_RE.fullmatch(str(materialized.get("claim_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("claim_payload_sha256") or ""))
        or materialized.get("consumption_registry_token")
        != AV1_V4R4_PREPARATION_CONSUMPTION_REGISTRY_TOKEN
        or not _GRANT_ID_RE.fullmatch(str(materialized.get("preparation_grant_id") or ""))
        or not _SHA256_RE.fullmatch(
            str(materialized.get("preparation_grant_payload_sha256") or "")
        )
        or not _KEY_ID_RE.fullmatch(str(materialized.get("key_id") or ""))
    ):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 key custody binding is invalid"
        )
    _assert_owner_principal(materialized.get("owner_principal"), "key custody owner")
    created = _parse_timestamp(str(materialized.get("created_at") or ""), "key custody created_at")
    claimed = _parse_timestamp(str(materialized.get("claimed_at") or ""), "key custody claimed_at")
    if created < claimed:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 key custody created_at cannot precede claim"
        )
    _assert_repository(object_dict(materialized.get("repository")), "key custody repository")
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(
        materialized,
        id_field="custody_id",
        id_pattern=_CUSTODY_ID_RE,
        expected_id_prefix="av1v4r4keycustody_",
        domain=av1_v4r4_identity_domain("path-privacy-key-custody"),
    )


def build_av1_v4r4_effective_config_snapshot(
    *,
    repository_commit: str,
    repository_tree: str,
    source_paths: Mapping[str, str],
    dedicated_instance_paths: Mapping[str, str],
    quality_temp_paths: Mapping[str, str],
) -> dict[str, Any]:
    payload = {
        **_common_payload(
            schema=AV1_V4R4_EFFECTIVE_CONFIG_SCHEMA,
            schema_version=AV1_V4R4_EFFECTIVE_CONFIG_SCHEMA_VERSION,
            contract_version=AV1_V4R4_EFFECTIVE_CONFIG_CONTRACT_VERSION,
        ),
        "repository": {"commit": repository_commit, "tree": repository_tree},
        "source_paths": {
            asset_id: str(source_paths[asset_id])
            for asset_id in _source_asset_ids()
        },
        "dedicated_instance_paths": {
            role: str(dedicated_instance_paths[role])
            for role in sorted(AV1_V4R4_INSTANCE_PATH_ROLES)
        },
        "quality_temp_paths": {
            asset_id: str(quality_temp_paths[asset_id])
            for asset_id in _source_asset_ids()
        },
        "configuration_modes": {
            "balanced_full_search_baseline": "baseline",
            "balanced_frozen_search_hint": "guided",
        },
        "policy_values_sha256": AV1_V4R4_POLICY_VALUES_SHA256,
        "frozen_source_stream_constraints": {
            asset_id: {
                "width": _source_spec(asset_id)["width"],
                "height": _source_spec(asset_id)["height"],
                "source_codec": _source_spec(asset_id)["source_codec"],
                "video_stream_index": _source_spec(asset_id)["video_stream_index"],
                "excluded_stream_indexes": list(
                    _source_spec(asset_id)["excluded_stream_indexes"]
                ),
            }
            for asset_id in _source_asset_ids()
        },
        "media_bytes_must_not_be_read_during_preparation": False,
        "source_digest_reverification_required_before_authority": True,
        "media_processing_subprocess_allowed_during_preparation": False,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="config_id",
        id_prefix="av1v4r4config_",
        domain=av1_v4r4_identity_domain("effective-config"),
    )
    assert_av1_v4r4_effective_config_snapshot(bound)
    return bound


def assert_av1_v4r4_effective_config_snapshot(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    _assert_fields(
        materialized,
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "config_id",
            "payload_sha256",
            "repository",
            "source_paths",
            "dedicated_instance_paths",
            "quality_temp_paths",
            "configuration_modes",
            "policy_values_sha256",
            "frozen_source_stream_constraints",
            "media_bytes_must_not_be_read_during_preparation",
            "source_digest_reverification_required_before_authority",
            "media_processing_subprocess_allowed_during_preparation",
            *AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
        },
        "effective config",
    )
    _assert_common_binding(
        materialized,
        {
            "schema": AV1_V4R4_EFFECTIVE_CONFIG_SCHEMA,
            "schema_version": AV1_V4R4_EFFECTIVE_CONFIG_SCHEMA_VERSION,
            "contract_version": AV1_V4R4_EFFECTIVE_CONFIG_CONTRACT_VERSION,
            "policy_values_sha256": AV1_V4R4_POLICY_VALUES_SHA256,
            "media_bytes_must_not_be_read_during_preparation": False,
            "source_digest_reverification_required_before_authority": True,
            "media_processing_subprocess_allowed_during_preparation": False,
        },
    )
    _assert_repository(object_dict(materialized.get("repository")), "effective config repository")
    _assert_absolute_path_mapping(
        object_dict(materialized.get("source_paths")),
        set(_source_asset_ids()),
        "source path",
    )
    _assert_absolute_path_mapping(
        object_dict(materialized.get("quality_temp_paths")),
        set(_source_asset_ids()),
        "quality temp path",
    )
    _assert_absolute_path_mapping(
        object_dict(materialized.get("dedicated_instance_paths")),
        set(AV1_V4R4_INSTANCE_PATH_ROLES),
        "instance path",
    )
    if materialized.get("configuration_modes") != {
        "balanced_full_search_baseline": "baseline",
        "balanced_frozen_search_hint": "guided",
    }:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 effective config modes are invalid"
        )
    constraints = object_dict(materialized.get("frozen_source_stream_constraints"))
    if set(constraints) != set(_source_asset_ids()):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 effective config source constraints are incomplete"
        )
    for asset_id in _source_asset_ids():
        expected = {
            "width": _source_spec(asset_id)["width"],
            "height": _source_spec(asset_id)["height"],
            "source_codec": _source_spec(asset_id)["source_codec"],
            "video_stream_index": _source_spec(asset_id)["video_stream_index"],
            "excluded_stream_indexes": list(_source_spec(asset_id)["excluded_stream_indexes"]),
        }
        if object_dict(constraints[asset_id]) != expected:
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 effective config source constraints are invalid"
            )
    _assert_false_authorities(materialized)
    _assert_identity(
        materialized,
        id_field="config_id",
        id_pattern=_CONFIG_ID_RE,
        expected_id_prefix="av1v4r4config_",
        domain=av1_v4r4_identity_domain("effective-config"),
    )


def build_av1_v4r4_closure_payloads() -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for ordinal_layout in av1_v4r4_ordinal_layout():
        asset_id = str(ordinal_layout["asset_id"])
        spec = _source_spec(asset_id)
        semantic = {
            **_common_payload(
                schema="mediaforce.av1_cold_start_v4r4_invocation_closure",
                schema_version=1,
                contract_version="av1v4r4closure1",
            ),
            "ordinal": ordinal_layout["ordinal"],
            "asset_id": asset_id,
            "content_class": ordinal_layout["content_class"],
            "role": ordinal_layout["role"],
            "configuration": ordinal_layout["configuration"],
            "media_sha256": spec["media_sha256"],
            "width": spec["width"],
            "height": spec["height"],
            "source_codec": spec["source_codec"],
            "video_stream_index": spec["video_stream_index"],
            "excluded_stream_indexes": list(spec["excluded_stream_indexes"]),
            "target_size_bytes": ordinal_layout["target_size_bytes"],
            "source_cap_total_bytes": ordinal_layout["source_cap_total_bytes"],
            "policy_values_sha256": AV1_V4R4_POLICY_VALUES_SHA256,
            "warm_start": (
                dict(object_dict(ordinal_layout["warm_start"]))
                if ordinal_layout["warm_start"] is not None
                else None
            ),
            **_false_authority_payload(),
        }
        payload = _bind_identity(
            semantic,
            id_field="closure_id",
            id_prefix="av1vclosure4r4_",
            domain=av1_v4r4_identity_domain("invocation-closure"),
        )
        payloads.append(payload)
    return payloads


def build_av1_v4r4_preparation_bundle(
    *,
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    key_custody: Mapping[str, Any],
    effective_config: Mapping[str, Any],
    path_privacy_key: bytes,
    toolchain: Mapping[str, Mapping[str, str]],
    runtime: Mapping[str, str],
    source_digests: Mapping[str, str],
) -> dict[str, Any]:
    grant = object_dict(preparation_grant)
    claim = object_dict(preparation_claim)
    custody = object_dict(key_custody)
    config = object_dict(effective_config)
    assert_av1_v4r4_preparation_grant(grant)
    assert_av1_v4r4_preparation_claim(claim)
    assert_av1_v4r4_path_privacy_key_custody(custody)
    assert_av1_v4r4_effective_config_snapshot(config)
    _assert_key_bytes(path_privacy_key)
    if custody["key_id"] != av1_v4r4_path_privacy_key_id(path_privacy_key):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation key custody does not match the key"
        )
    if (
        claim["preparation_grant_id"] != grant["grant_id"]
        or claim["preparation_grant_payload_sha256"] != grant["payload_sha256"]
        or custody["claim_id"] != claim["claim_id"]
        or custody["claim_payload_sha256"] != claim["payload_sha256"]
        or object_dict(grant["repository"]) != object_dict(claim["repository"])
        or object_dict(grant["repository"]) != object_dict(custody["repository"])
        or object_dict(grant["repository"]) != object_dict(config["repository"])
    ):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation bundle evidence chain is invalid"
        )
    if set(source_digests) != set(_source_asset_ids()):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation source digest map is incomplete"
        )
    source_path_hmac_ids = {
        asset_id: av1_v4r4_source_path_hmac_id(
            path_privacy_key,
            asset_id=asset_id,
            normalized_path=str(object_dict(config["source_paths"])[asset_id]),
        )
        for asset_id in _source_asset_ids()
    }
    dedicated_instance_hmac_ids = {
        role: av1_v4r4_instance_path_hmac_id(
            path_privacy_key,
            role=role,
            normalized_path=str(object_dict(config["dedicated_instance_paths"])[role]),
        )
        for role in sorted(AV1_V4R4_INSTANCE_PATH_ROLES)
    }
    quality_temp_hmac_ids = {
        asset_id: av1_v4r4_quality_temp_hmac_id(
            path_privacy_key,
            asset_id=asset_id,
            normalized_path=str(object_dict(config["quality_temp_paths"])[asset_id]),
        )
        for asset_id in _source_asset_ids()
    }
    effective_config_hmac_id = av1_v4r4_effective_config_hmac_id(path_privacy_key, config)
    normalized_toolchain = _normalize_toolchain(toolchain)
    normalized_runtime = _normalize_runtime(runtime)
    toolchain_id = _prefixed_id(
        "av1v4r4toolchain_",
        normalized_toolchain,
        av1_v4r4_identity_domain("toolchain-binding"),
    )
    runtime_id = _prefixed_id(
        "av1v4r4runtime_",
        normalized_runtime,
        av1_v4r4_identity_domain("runtime-binding"),
    )
    closures = build_av1_v4r4_closure_payloads()
    invocation_digests = []
    for closure in closures:
        asset_id = str(closure["asset_id"])
        ordinal = int(closure["ordinal"])
        spec = _source_spec(asset_id)
        invocation_sha256 = av1_v4r4_runner_invocation_sha256(
            ordinal=ordinal,
            source_path=Path(str(object_dict(config["source_paths"])[asset_id])),
            quality_temp_path=Path(str(object_dict(config["quality_temp_paths"])[asset_id])),
            source_codec=str(spec["source_codec"]),
            width=int(spec["width"]),
            height=int(spec["height"]),
        )
        invocation_digests.append(
            {
                "ordinal": ordinal,
                "asset_id": asset_id,
                "configuration": closure["configuration"],
                "closure_id": closure["closure_id"],
                "source_path_hmac_id": source_path_hmac_ids[asset_id],
                "quality_temp_hmac_id": quality_temp_hmac_ids[asset_id],
                "mode": "guided" if closure["warm_start"] is not None else "baseline",
                "source_codec": spec["source_codec"],
                "width": spec["width"],
                "height": spec["height"],
                "invocation_sha256": invocation_sha256,
                "invocation_config_hmac_id": av1_v4r4_invocation_config_hmac_id(
                    path_privacy_key,
                    ordinal=ordinal,
                    source_path=str(object_dict(config["source_paths"])[asset_id]),
                    quality_temp_path=str(object_dict(config["quality_temp_paths"])[asset_id]),
                    source_codec=str(spec["source_codec"]),
                    width=int(spec["width"]),
                    height=int(spec["height"]),
                    invocation_sha256=invocation_sha256,
                ),
            }
        )
    payload = {
        **_common_payload(
            schema=AV1_V4R4_PREPARATION_BUNDLE_SCHEMA,
            schema_version=AV1_V4R4_PREPARATION_BUNDLE_SCHEMA_VERSION,
            contract_version=AV1_V4R4_PREPARATION_BUNDLE_CONTRACT_VERSION,
        ),
        "state": AV1_V4R4_PREPARATION_BUNDLE_STATE,
        "repository": dict(object_dict(config["repository"])),
        "rights_attestation_id": claim["rights_attestation_id"],
        "rights_attestation_payload_sha256": claim["rights_attestation_payload_sha256"],
        "preparation_grant_id": grant["grant_id"],
        "preparation_grant_payload_sha256": grant["payload_sha256"],
        "claim_id": claim["claim_id"],
        "claim_payload_sha256": claim["payload_sha256"],
        "custody_id": custody["custody_id"],
        "custody_payload_sha256": custody["payload_sha256"],
        "path_privacy_key_id": custody["key_id"],
        "effective_config_hmac_id": effective_config_hmac_id,
        "toolchain": normalized_toolchain,
        "toolchain_id": toolchain_id,
        "runtime": normalized_runtime,
        "runtime_id": runtime_id,
        "source_path_hmac_ids": source_path_hmac_ids,
        "dedicated_instance_hmac_ids": dedicated_instance_hmac_ids,
        "quality_temp_hmac_ids": quality_temp_hmac_ids,
        "source_digest_sha256": {
            asset_id: source_digests[asset_id] for asset_id in _source_asset_ids()
        },
        "closure_ids": [item["closure_id"] for item in closures],
        "invocation_digests": invocation_digests,
        "unresolved_execution_bindings": {
            "ordinal_registry_id": None,
            "sequencing_grant_id": None,
            "sequencing_claim_id": None,
            "execution_grant_id": None,
            "execution_claim_id": None,
            "runner_admission_id": None,
            "assigned_stage": "execution_authority",
        },
        "selection_or_partition_use_allowed": False,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="bundle_id",
        id_prefix="av1v4r4prepbundle_",
        domain=av1_v4r4_identity_domain("preparation-bundle"),
    )
    assert_av1_v4r4_preparation_bundle(bound)
    return bound


def assert_av1_v4r4_preparation_bundle(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    _assert_fields(
        materialized,
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "bundle_id",
            "payload_sha256",
            "state",
            "repository",
            "rights_attestation_id",
            "rights_attestation_payload_sha256",
            "preparation_grant_id",
            "preparation_grant_payload_sha256",
            "claim_id",
            "claim_payload_sha256",
            "custody_id",
            "custody_payload_sha256",
            "path_privacy_key_id",
            "effective_config_hmac_id",
            "toolchain",
            "toolchain_id",
            "runtime",
            "runtime_id",
            "source_path_hmac_ids",
            "dedicated_instance_hmac_ids",
            "quality_temp_hmac_ids",
            "source_digest_sha256",
            "closure_ids",
            "invocation_digests",
            "unresolved_execution_bindings",
            "selection_or_partition_use_allowed",
            *AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
        },
        "preparation bundle",
    )
    _assert_common_binding(
        materialized,
        {
            "schema": AV1_V4R4_PREPARATION_BUNDLE_SCHEMA,
            "schema_version": AV1_V4R4_PREPARATION_BUNDLE_SCHEMA_VERSION,
            "contract_version": AV1_V4R4_PREPARATION_BUNDLE_CONTRACT_VERSION,
            "state": AV1_V4R4_PREPARATION_BUNDLE_STATE,
            "selection_or_partition_use_allowed": False,
        },
    )
    _assert_repository(object_dict(materialized.get("repository")), "bundle repository")
    for field, pattern in (
        ("rights_attestation_id", _RIGHTS_ID_RE),
        ("preparation_grant_id", _GRANT_ID_RE),
        ("claim_id", _CLAIM_ID_RE),
        ("custody_id", _CUSTODY_ID_RE),
        ("path_privacy_key_id", _KEY_ID_RE),
        ("effective_config_hmac_id", _CONFIG_HMAC_ID_RE),
        ("toolchain_id", _TOOLCHAIN_ID_RE),
        ("runtime_id", _RUNTIME_ID_RE),
    ):
        if not pattern.fullmatch(str(materialized.get(field) or "")):
            raise AV1V4R4PreparationError(
                f"AV1 v4 r4 preparation bundle {field} is invalid"
            )
    for field in (
        "rights_attestation_payload_sha256",
        "preparation_grant_payload_sha256",
        "claim_payload_sha256",
        "custody_payload_sha256",
    ):
        _assert_sha(materialized.get(field), field)
    _assert_toolchain(materialized.get("toolchain"))
    _assert_runtime(materialized.get("runtime"))
    _assert_path_hmac_mapping(
        object_dict(materialized.get("source_path_hmac_ids")),
        set(_source_asset_ids()),
        _SOURCE_HMAC_ID_RE,
        "source path hmac",
    )
    _assert_path_hmac_mapping(
        object_dict(materialized.get("quality_temp_hmac_ids")),
        set(_source_asset_ids()),
        _QUALITY_TEMP_HMAC_ID_RE,
        "quality temp hmac",
    )
    _assert_path_hmac_mapping(
        object_dict(materialized.get("dedicated_instance_hmac_ids")),
        set(AV1_V4R4_INSTANCE_PATH_ROLES),
        _PATH_HMAC_ID_RE,
        "instance path hmac",
    )
    digest_map = object_dict(materialized.get("source_digest_sha256"))
    if set(digest_map) != set(_source_asset_ids()):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation bundle source digest map is incomplete"
        )
    for asset_id in _source_asset_ids():
        digest = str(digest_map[asset_id])
        _assert_sha(digest, f"source digest {asset_id}")
        if digest != _source_spec(asset_id)["media_sha256"]:
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation bundle source digest is invalid"
            )
    closure_ids = [str(item) for item in object_list(materialized.get("closure_ids"))]
    if len(closure_ids) != len(av1_v4r4_ordinal_layout()) or any(
        not _CLOSURE_ID_RE.fullmatch(value) for value in closure_ids
    ) or len(set(closure_ids)) != len(closure_ids):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation bundle closure identities are invalid"
        )
    invocation_digests = [
        dict(object_dict(item)) for item in object_list(materialized.get("invocation_digests"))
    ]
    if len(invocation_digests) != len(av1_v4r4_ordinal_layout()):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation bundle requires exactly eight invocation digests"
        )
    for index, closure in enumerate(build_av1_v4r4_closure_payloads(), start=1):
        invocation = invocation_digests[index - 1]
        if (
            invocation.get("ordinal") != index
            or invocation.get("asset_id") != closure["asset_id"]
            or invocation.get("configuration") != closure["configuration"]
            or invocation.get("closure_id") != closure["closure_id"]
            or not _SOURCE_HMAC_ID_RE.fullmatch(
                str(invocation.get("source_path_hmac_id") or "")
            )
            or not _QUALITY_TEMP_HMAC_ID_RE.fullmatch(
                str(invocation.get("quality_temp_hmac_id") or "")
            )
            or invocation.get("mode")
            != ("guided" if closure["warm_start"] is not None else "baseline")
            or invocation.get("source_codec") != closure["source_codec"]
            or invocation.get("width") != closure["width"]
            or invocation.get("height") != closure["height"]
            or not _INVOCATION_SHA_ID_RE.fullmatch(
                str(invocation.get("invocation_sha256") or "")
            )
            or not _INVOCATION_CONFIG_HMAC_ID_RE.fullmatch(
                str(invocation.get("invocation_config_hmac_id") or "")
            )
        ):
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation bundle invocation binding is invalid"
            )
    if materialized.get("unresolved_execution_bindings") != {
        "ordinal_registry_id": None,
        "sequencing_grant_id": None,
        "sequencing_claim_id": None,
        "execution_grant_id": None,
        "execution_claim_id": None,
        "runner_admission_id": None,
        "assigned_stage": "execution_authority",
    }:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation bundle unresolved execution bindings are invalid"
        )
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(
        materialized,
        id_field="bundle_id",
        id_pattern=_BUNDLE_ID_RE,
        expected_id_prefix="av1v4r4prepbundle_",
        domain=av1_v4r4_identity_domain("preparation-bundle"),
    )


def assert_av1_v4r4_preparation_bundle_private_invocations(
    *,
    preparation_bundle: Mapping[str, Any],
    effective_config: Mapping[str, Any],
    path_privacy_key: bytes,
) -> None:
    """Verify pathful runner invocation digests using owner-only private inputs."""

    bundle = object_dict(preparation_bundle)
    config = object_dict(effective_config)
    assert_av1_v4r4_preparation_bundle(bundle)
    assert_av1_v4r4_effective_config_snapshot(config)
    _assert_key_bytes(path_privacy_key)
    if bundle["effective_config_hmac_id"] != av1_v4r4_effective_config_hmac_id(
        path_privacy_key,
        config,
    ):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation bundle private config binding is invalid"
        )
    source_paths = object_dict(config["source_paths"])
    quality_temp_paths = object_dict(config["quality_temp_paths"])
    source_hmac_ids = object_dict(bundle["source_path_hmac_ids"])
    quality_hmac_ids = object_dict(bundle["quality_temp_hmac_ids"])
    invocations = [
        dict(object_dict(item)) for item in object_list(bundle["invocation_digests"])
    ]
    for index, invocation in enumerate(invocations, start=1):
        asset_id = str(invocation["asset_id"])
        spec = _source_spec(asset_id)
        expected_source_hmac = av1_v4r4_source_path_hmac_id(
            path_privacy_key,
            asset_id=asset_id,
            normalized_path=str(source_paths[asset_id]),
        )
        expected_quality_hmac = av1_v4r4_quality_temp_hmac_id(
            path_privacy_key,
            asset_id=asset_id,
            normalized_path=str(quality_temp_paths[asset_id]),
        )
        expected_invocation = av1_v4r4_runner_invocation_sha256(
            ordinal=index,
            source_path=Path(str(source_paths[asset_id])),
            quality_temp_path=Path(str(quality_temp_paths[asset_id])),
            source_codec=str(spec["source_codec"]),
            width=int(spec["width"]),
            height=int(spec["height"]),
        )
        expected_config_hmac = av1_v4r4_invocation_config_hmac_id(
            path_privacy_key,
            ordinal=index,
            source_path=str(source_paths[asset_id]),
            quality_temp_path=str(quality_temp_paths[asset_id]),
            source_codec=str(spec["source_codec"]),
            width=int(spec["width"]),
            height=int(spec["height"]),
            invocation_sha256=expected_invocation,
        )
        if (
            source_hmac_ids[asset_id] != expected_source_hmac
            or quality_hmac_ids[asset_id] != expected_quality_hmac
            or invocation["source_path_hmac_id"] != expected_source_hmac
            or invocation["quality_temp_hmac_id"] != expected_quality_hmac
            or invocation["invocation_sha256"] != expected_invocation
            or invocation["invocation_config_hmac_id"] != expected_config_hmac
        ):
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation bundle private invocation binding is invalid"
            )


def build_av1_v4r4_preparation_success_measurement(
    *,
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    key_custody: Mapping[str, Any],
    preparation_bundle: Mapping[str, Any],
    started_at: str,
    completed_at: str,
    probes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    grant = object_dict(preparation_grant)
    claim = object_dict(preparation_claim)
    custody = object_dict(key_custody)
    bundle = object_dict(preparation_bundle)
    assert_av1_v4r4_preparation_grant(grant)
    assert_av1_v4r4_preparation_claim(claim)
    assert_av1_v4r4_path_privacy_key_custody(custody)
    assert_av1_v4r4_preparation_bundle(bundle)
    payload = {
        **_measurement_common(
            preparation_grant=grant,
            preparation_claim=claim,
            started_at=started_at,
            completed_at=completed_at,
            probes=probes,
            stages_completed=AV1_V4R4_PREPARATION_STAGES,
            state="terminal_success",
        ),
        "bundle_id": bundle["bundle_id"],
        "bundle_payload_sha256": bundle["payload_sha256"],
        "path_privacy_key_id": custody["key_id"],
        "key_custody": custody,
        "source_digest_sha256": dict(object_dict(bundle["source_digest_sha256"])),
        "invocation_count": len(object_list(bundle["invocation_digests"])),
        "closure_count": len(object_list(bundle["closure_ids"])),
        "rollback": None,
        "failure": None,
    }
    bound = _bind_identity(
        payload,
        id_field="measurement_id",
        id_prefix="av1v4r4prepmeas_",
        domain=av1_v4r4_identity_domain("preparation-measurement"),
    )
    assert_av1_v4r4_preparation_measurement(bound)
    return bound


def build_av1_v4r4_preparation_failure_measurement(
    *,
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    started_at: str,
    completed_at: str,
    probes: Sequence[Mapping[str, Any]],
    stages_completed: Sequence[str],
    failure_stage: str,
    reason_code: str,
    error_class: str,
    message_sha256: str,
    rollback: Mapping[str, Any],
    path_privacy_key_id: str | None,
) -> dict[str, Any]:
    grant = object_dict(preparation_grant)
    claim = object_dict(preparation_claim)
    assert_av1_v4r4_preparation_grant(grant)
    assert_av1_v4r4_preparation_claim(claim)
    _assert_sha(message_sha256, "failure message digest")
    payload = {
        **_measurement_common(
            preparation_grant=grant,
            preparation_claim=claim,
            started_at=started_at,
            completed_at=completed_at,
            probes=probes,
            stages_completed=tuple(stages_completed),
            state="terminal_failure",
        ),
        "bundle_id": None,
        "bundle_payload_sha256": None,
        "path_privacy_key_id": path_privacy_key_id,
        "key_custody": None,
        "source_digest_sha256": None,
        "invocation_count": None,
        "closure_count": None,
        "rollback": dict(object_dict(rollback)),
        "failure": {
            "stage": failure_stage,
            "reason_code": reason_code,
            "error_class": error_class,
            "message_sha256": message_sha256,
        },
    }
    bound = _bind_identity(
        payload,
        id_field="measurement_id",
        id_prefix="av1v4r4prepmeas_",
        domain=av1_v4r4_identity_domain("preparation-measurement"),
    )
    assert_av1_v4r4_preparation_measurement(bound)
    return bound


def assert_av1_v4r4_preparation_measurement(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    _assert_fields(
        materialized,
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "measurement_id",
            "payload_sha256",
            "state",
            "preparation_grant_id",
            "preparation_grant_payload_sha256",
            "claim_id",
            "claim_payload_sha256",
            "started_at",
            "completed_at",
            "stages_completed",
            "method",
            "probes",
            "tool_version_probe_subprocess_count",
            "bundle_id",
            "bundle_payload_sha256",
            "path_privacy_key_id",
            "key_custody",
            "source_digest_sha256",
            "invocation_count",
            "closure_count",
            "rollback",
            "failure",
            *AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
        },
        "preparation measurement",
    )
    state = materialized.get("state")
    if state not in {"terminal_success", "terminal_failure"}:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation measurement state is invalid"
        )
    _assert_common_binding(
        materialized,
        {
            "schema": AV1_V4R4_PREPARATION_MEASUREMENT_SCHEMA,
            "schema_version": AV1_V4R4_PREPARATION_MEASUREMENT_SCHEMA_VERSION,
            "contract_version": AV1_V4R4_PREPARATION_MEASUREMENT_CONTRACT_VERSION,
        },
    )
    if not _GRANT_ID_RE.fullmatch(str(materialized.get("preparation_grant_id") or "")):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation measurement grant binding is invalid"
        )
    _assert_sha(materialized.get("preparation_grant_payload_sha256"), "measurement grant payload")
    if not _CLAIM_ID_RE.fullmatch(str(materialized.get("claim_id") or "")):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation measurement claim binding is invalid"
        )
    _assert_sha(materialized.get("claim_payload_sha256"), "measurement claim payload")
    started = _parse_timestamp(str(materialized.get("started_at") or ""), "measurement started_at")
    completed = _parse_timestamp(str(materialized.get("completed_at") or ""), "measurement completed_at")
    if completed < started:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation measurement timeline is invalid"
        )
    stages_completed = tuple(str(item) for item in object_list(materialized.get("stages_completed")))
    if not _is_stage_prefix(stages_completed):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation measurement stages are invalid"
        )
    if object_dict(materialized.get("method")) != dict(AV1_V4R4_PREPARATION_METHOD):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation measurement method is invalid"
        )
    probes = [dict(object_dict(item)) for item in object_list(materialized.get("probes"))]
    if len(probes) > 3:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation measurement probe count is invalid"
        )
    for probe in probes:
        if (
            probe.get("tool") not in {"ab_av1", "ffmpeg", "ffprobe"}
            or probe.get("argv")
            != AV1_V4R4_PREPARATION_METHOD["tool_version_probe_argv"][probe["tool"]]
            or not isinstance(probe.get("version"), str)
            or not probe["version"]
            or not _SHA256_RE.fullmatch(str(probe.get("binary_sha256") or ""))
        ):
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation measurement probe is invalid"
            )
    if materialized.get("tool_version_probe_subprocess_count") != len(probes):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation measurement probe count does not match"
        )
    if state == "terminal_success":
        if stages_completed != AV1_V4R4_PREPARATION_STAGES:
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation measurement success stages are incomplete"
            )
        if (
            not _BUNDLE_ID_RE.fullmatch(str(materialized.get("bundle_id") or ""))
            or not _SHA256_RE.fullmatch(str(materialized.get("bundle_payload_sha256") or ""))
            or not _KEY_ID_RE.fullmatch(str(materialized.get("path_privacy_key_id") or ""))
            or materialized.get("rollback") is not None
            or materialized.get("failure") is not None
            or materialized.get("key_custody") is None
            or materialized.get("source_digest_sha256") is None
            or materialized.get("invocation_count") != len(av1_v4r4_ordinal_layout())
            or materialized.get("closure_count") != len(av1_v4r4_ordinal_layout())
        ):
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation measurement success binding is invalid"
            )
        assert_av1_v4r4_path_privacy_key_custody(object_dict(materialized["key_custody"]))
        digest_map = object_dict(materialized["source_digest_sha256"])
        if set(digest_map) != set(_source_asset_ids()):
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation measurement source digest map is invalid"
            )
        for asset_id in _source_asset_ids():
            digest = str(digest_map[asset_id])
            if digest != _source_spec(asset_id)["media_sha256"]:
                raise AV1V4R4PreparationError(
                    "AV1 v4 r4 preparation measurement source digest is invalid"
                )
    else:
        failure = object_dict(materialized.get("failure"))
        rollback = object_dict(materialized.get("rollback"))
        if set(failure) != {"stage", "reason_code", "error_class", "message_sha256"}:
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation measurement failure payload is invalid"
            )
        _assert_sha(failure.get("message_sha256"), "failure message digest")
        if stages_completed == AV1_V4R4_PREPARATION_STAGES:
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation measurement failure stages are invalid"
            )
        if failure.get("stage") != AV1_V4R4_PREPARATION_STAGES[len(stages_completed)]:
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation measurement failure stage is invalid"
            )
        if set(rollback) != {"removed_artifacts", "retained_artifacts"}:
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation measurement rollback payload is invalid"
            )
        if materialized.get("bundle_id") is not None or materialized.get("bundle_payload_sha256") is not None:
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation measurement failure cannot bind a bundle"
            )
        path_key = materialized.get("path_privacy_key_id")
        if path_key is not None and not _KEY_ID_RE.fullmatch(str(path_key)):
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 preparation measurement failure path key ID is invalid"
            )
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(
        materialized,
        id_field="measurement_id",
        id_pattern=_MEASUREMENT_ID_RE,
        expected_id_prefix="av1v4r4prepmeas_",
        domain=av1_v4r4_identity_domain("preparation-measurement"),
    )


def build_av1_v4r4_owner_freeze(
    *,
    rights_attestation: Mapping[str, Any],
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    key_custody: Mapping[str, Any],
    preparation_bundle: Mapping[str, Any],
    preparation_measurement: Mapping[str, Any],
    owner_principal: str,
    decided_at: str,
    materializer_repository_commit: str,
    materializer_repository_tree: str,
) -> dict[str, Any]:
    rights = object_dict(rights_attestation)
    grant = object_dict(preparation_grant)
    claim = object_dict(preparation_claim)
    custody = object_dict(key_custody)
    bundle = object_dict(preparation_bundle)
    measurement = object_dict(preparation_measurement)
    assert_av1_v4r4_rights_attestation(rights)
    assert_av1_v4r4_preparation_grant(grant)
    assert_av1_v4r4_preparation_claim(claim)
    assert_av1_v4r4_path_privacy_key_custody(custody)
    assert_av1_v4r4_preparation_bundle(bundle)
    assert_av1_v4r4_preparation_measurement(measurement)
    _assert_owner_principal(owner_principal, "freeze owner")
    if not (
        rights["owner_principal"]
        == grant["owner_principal"]
        == claim["owner_principal"]
        == custody["owner_principal"]
        == owner_principal
    ):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 owner freeze owner binding is invalid"
        )
    if (
        bundle["rights_attestation_id"] != rights["attestation_id"]
        or bundle["rights_attestation_payload_sha256"] != rights["payload_sha256"]
        or bundle["preparation_grant_id"] != grant["grant_id"]
        or bundle["preparation_grant_payload_sha256"] != grant["payload_sha256"]
        or bundle["claim_id"] != claim["claim_id"]
        or bundle["claim_payload_sha256"] != claim["payload_sha256"]
        or bundle["custody_id"] != custody["custody_id"]
        or bundle["custody_payload_sha256"] != custody["payload_sha256"]
        or bundle["path_privacy_key_id"] != custody["key_id"]
        or measurement["state"] != "terminal_success"
        or measurement["bundle_id"] != bundle["bundle_id"]
        or measurement["bundle_payload_sha256"] != bundle["payload_sha256"]
    ):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 owner freeze evidence chain is invalid"
        )
    decided = _parse_timestamp(decided_at, "freeze decided_at")
    if decided < _parse_timestamp(str(measurement["completed_at"]), "measurement completed_at"):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 owner freeze timeline is invalid"
        )
    materializer_repository = {
        "commit": materializer_repository_commit,
        "tree": materializer_repository_tree,
    }
    _assert_repository(materializer_repository, "freeze materializer repository")
    payload = {
        **_common_payload(
            schema=AV1_V4R4_FREEZE_SCHEMA,
            schema_version=AV1_V4R4_FREEZE_SCHEMA_VERSION,
            contract_version=AV1_V4R4_FREEZE_CONTRACT_VERSION,
        ),
        "manifest_valid_until": "2027-02-02T18:53:35Z",
        "state": AV1_V4R4_FREEZE_STATE,
        "authority": AV1_V4R4_FREEZE_AUTHORITY,
        "registry_key": f"{AV1_V4R4_MANIFEST_ID}:revision:4",
        "owner_principal": owner_principal,
        "decided_at": decided_at,
        "reviewed_repository": dict(object_dict(bundle["repository"])),
        "materializer_repository": materializer_repository,
        "rights_attestation_id": rights["attestation_id"],
        "rights_attestation_payload_sha256": rights["payload_sha256"],
        "rights_attested_at": rights["attested_at"],
        "preparation_grant_id": grant["grant_id"],
        "preparation_grant_payload_sha256": grant["payload_sha256"],
        "preparation_grant_authorized_at": grant["authorized_at"],
        "preparation_grant_valid_until": grant["valid_until"],
        "claim_id": claim["claim_id"],
        "claim_payload_sha256": claim["payload_sha256"],
        "claimed_at": claim["claimed_at"],
        "custody_id": custody["custody_id"],
        "custody_payload_sha256": custody["payload_sha256"],
        "path_privacy_key_id": custody["key_id"],
        "custody_created_at": custody["created_at"],
        "preparation_bundle_id": bundle["bundle_id"],
        "preparation_bundle_payload_sha256": bundle["payload_sha256"],
        "preparation_bundle_state": bundle["state"],
        "effective_config_hmac_id": bundle["effective_config_hmac_id"],
        "toolchain_id": bundle["toolchain_id"],
        "runtime_id": bundle["runtime_id"],
        "closure_ids": list(object_list(bundle["closure_ids"])),
        "invocation_digests": [
            dict(object_dict(item)) for item in object_list(bundle["invocation_digests"])
        ],
        "measurement_id": measurement["measurement_id"],
        "measurement_payload_sha256": measurement["payload_sha256"],
        "measurement_state": measurement["state"],
        "measurement_started_at": measurement["started_at"],
        "measurement_completed_at": measurement["completed_at"],
        AV1_V4R4_FREEZE_APPROVAL_FIELD: True,
        "freeze_scope": dict(AV1_V4R4_FREEZE_SCOPE),
        "selection_or_partition_use_allowed": False,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="freeze_id",
        id_prefix="av1vfreeze4r4_",
        domain=av1_v4r4_identity_domain("owner-freeze"),
    )
    assert_av1_v4r4_owner_freeze(bound)
    return bound


def assert_av1_v4r4_owner_freeze(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    _assert_fields(
        materialized,
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "manifest_valid_until",
            "freeze_id",
            "payload_sha256",
            "state",
            "authority",
            "registry_key",
            "owner_principal",
            "decided_at",
            "reviewed_repository",
            "materializer_repository",
            "rights_attestation_id",
            "rights_attestation_payload_sha256",
            "rights_attested_at",
            "preparation_grant_id",
            "preparation_grant_payload_sha256",
            "preparation_grant_authorized_at",
            "preparation_grant_valid_until",
            "claim_id",
            "claim_payload_sha256",
            "claimed_at",
            "custody_id",
            "custody_payload_sha256",
            "path_privacy_key_id",
            "custody_created_at",
            "preparation_bundle_id",
            "preparation_bundle_payload_sha256",
            "preparation_bundle_state",
            "effective_config_hmac_id",
            "toolchain_id",
            "runtime_id",
            "closure_ids",
            "invocation_digests",
            "measurement_id",
            "measurement_payload_sha256",
            "measurement_state",
            "measurement_started_at",
            "measurement_completed_at",
            AV1_V4R4_FREEZE_APPROVAL_FIELD,
            "freeze_scope",
            "selection_or_partition_use_allowed",
            *AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
        },
        "owner freeze",
    )
    _assert_common_binding(
        materialized,
        {
            "schema": AV1_V4R4_FREEZE_SCHEMA,
            "schema_version": AV1_V4R4_FREEZE_SCHEMA_VERSION,
            "contract_version": AV1_V4R4_FREEZE_CONTRACT_VERSION,
            "state": AV1_V4R4_FREEZE_STATE,
            "authority": AV1_V4R4_FREEZE_AUTHORITY,
            "manifest_valid_until": "2027-02-02T18:53:35Z",
            AV1_V4R4_FREEZE_APPROVAL_FIELD: True,
            "preparation_bundle_state": AV1_V4R4_PREPARATION_BUNDLE_STATE,
            "measurement_state": "terminal_success",
            "freeze_scope": dict(AV1_V4R4_FREEZE_SCOPE),
            "selection_or_partition_use_allowed": False,
        },
    )
    _assert_owner_principal(materialized.get("owner_principal"), "freeze owner")
    _parse_timestamp(str(materialized.get("decided_at") or ""), "freeze decided_at")
    _assert_repository(object_dict(materialized.get("reviewed_repository")), "freeze reviewed repository")
    _assert_repository(object_dict(materialized.get("materializer_repository")), "freeze materializer repository")
    for field, pattern in (
        ("rights_attestation_id", _RIGHTS_ID_RE),
        ("preparation_grant_id", _GRANT_ID_RE),
        ("claim_id", _CLAIM_ID_RE),
        ("custody_id", _CUSTODY_ID_RE),
        ("path_privacy_key_id", _KEY_ID_RE),
        ("preparation_bundle_id", _BUNDLE_ID_RE),
        ("effective_config_hmac_id", _CONFIG_HMAC_ID_RE),
        ("toolchain_id", _TOOLCHAIN_ID_RE),
        ("runtime_id", _RUNTIME_ID_RE),
        ("measurement_id", _MEASUREMENT_ID_RE),
    ):
        if not pattern.fullmatch(str(materialized.get(field) or "")):
            raise AV1V4R4PreparationError(
                f"AV1 v4 r4 owner freeze {field} is invalid"
            )
    for field in (
        "rights_attestation_payload_sha256",
        "preparation_grant_payload_sha256",
        "claim_payload_sha256",
        "custody_payload_sha256",
        "preparation_bundle_payload_sha256",
        "measurement_payload_sha256",
    ):
        _assert_sha(materialized.get(field), field)
    closure_ids = [str(item) for item in object_list(materialized.get("closure_ids"))]
    expected_closure_ids = [item["closure_id"] for item in build_av1_v4r4_closure_payloads()]
    if closure_ids != expected_closure_ids:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 owner freeze closure identities are invalid"
        )
    invocation_digests = [
        dict(object_dict(item)) for item in object_list(materialized.get("invocation_digests"))
    ]
    if len(invocation_digests) != len(av1_v4r4_ordinal_layout()):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 owner freeze invocation coverage is invalid"
        )
    for index, invocation in enumerate(invocation_digests, start=1):
        if invocation.get("ordinal") != index or not _INVOCATION_SHA_ID_RE.fullmatch(
            str(invocation.get("invocation_sha256") or "")
        ):
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 owner freeze invocation binding is invalid"
            )
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(
        materialized,
        id_field="freeze_id",
        id_pattern=_FREEZE_ID_RE,
        expected_id_prefix="av1vfreeze4r4_",
        domain=av1_v4r4_identity_domain("owner-freeze"),
    )


def av1_v4r4_qualification_request_valid_until(requested_at: str) -> str:
    requested = _parse_timestamp(requested_at, "qualification request requested_at")
    return _format_ts(requested + timedelta(seconds=AV1_V4R4_QUALIFICATION_REQUEST_TTL_SECONDS))


def build_av1_v4r4_qualification_request(
    *,
    owner_freeze: Mapping[str, Any],
    owner_principal: str,
    requested_at: str,
    valid_until: str,
    requesting_repository_commit: str,
    requesting_repository_tree: str,
) -> dict[str, Any]:
    freeze = object_dict(owner_freeze)
    assert_av1_v4r4_owner_freeze(freeze)
    _assert_owner_principal(owner_principal, "qualification request owner")
    if freeze.get("owner_principal") != owner_principal:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 qualification request owner does not match the freeze"
        )
    _assert_timeline_values(
        freeze_decided_at=str(freeze["decided_at"]),
        requested_at=requested_at,
        valid_until=valid_until,
    )
    requesting_repository = {
        "commit": requesting_repository_commit,
        "tree": requesting_repository_tree,
    }
    _assert_repository(requesting_repository, "requesting repository")
    payload = {
        **_common_payload(
            schema=AV1_V4R4_QUALIFICATION_REQUEST_SCHEMA,
            schema_version=AV1_V4R4_QUALIFICATION_REQUEST_SCHEMA_VERSION,
            contract_version=AV1_V4R4_QUALIFICATION_REQUEST_CONTRACT_VERSION,
        ),
        "manifest_valid_until": "2027-02-02T18:53:35Z",
        "state": AV1_V4R4_QUALIFICATION_REQUEST_STATE,
        "authority": AV1_V4R4_QUALIFICATION_REQUEST_AUTHORITY,
        "registry_key": f"{AV1_V4R4_MANIFEST_ID}:revision:4:qualification-request",
        "owner_principal": owner_principal,
        "requested_at": requested_at,
        "valid_until": valid_until,
        "request_ttl_seconds": AV1_V4R4_QUALIFICATION_REQUEST_TTL_SECONDS,
        "reviewed_repository": dict(object_dict(freeze["reviewed_repository"])),
        "requesting_repository": requesting_repository,
        "freeze_id": freeze["freeze_id"],
        "freeze_payload_sha256": freeze["payload_sha256"],
        "freeze_decided_at": freeze["decided_at"],
        "preparation_bundle_id": freeze["preparation_bundle_id"],
        "preparation_bundle_payload_sha256": freeze["preparation_bundle_payload_sha256"],
        "effective_config_hmac_id": freeze["effective_config_hmac_id"],
        "path_privacy_key_id": freeze["path_privacy_key_id"],
        "toolchain_id": freeze["toolchain_id"],
        "runtime_id": freeze["runtime_id"],
        "closure_ids": list(object_list(freeze["closure_ids"])),
        "invocation_digests": [
            dict(object_dict(item)) for item in object_list(freeze["invocation_digests"])
        ],
        "requested_ordinal_count": len(av1_v4r4_ordinal_layout()),
        AV1_V4R4_QUALIFICATION_REQUEST_APPROVAL_FIELD: True,
        "request_scope": dict(AV1_V4R4_QUALIFICATION_REQUEST_SCOPE),
        "selection_or_partition_use_allowed": False,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="request_id",
        id_prefix="av1v4r4req_",
        domain=av1_v4r4_identity_domain("qualification-request"),
    )
    assert_av1_v4r4_qualification_request(bound)
    return bound


def assert_av1_v4r4_qualification_request(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    _assert_fields(
        materialized,
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "manifest_valid_until",
            "request_id",
            "payload_sha256",
            "state",
            "authority",
            "registry_key",
            "owner_principal",
            "requested_at",
            "valid_until",
            "request_ttl_seconds",
            "reviewed_repository",
            "requesting_repository",
            "freeze_id",
            "freeze_payload_sha256",
            "freeze_decided_at",
            "preparation_bundle_id",
            "preparation_bundle_payload_sha256",
            "effective_config_hmac_id",
            "path_privacy_key_id",
            "toolchain_id",
            "runtime_id",
            "closure_ids",
            "invocation_digests",
            "requested_ordinal_count",
            AV1_V4R4_QUALIFICATION_REQUEST_APPROVAL_FIELD,
            "request_scope",
            "selection_or_partition_use_allowed",
            *AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
        },
        "qualification request",
    )
    _assert_common_binding(
        materialized,
        {
            "schema": AV1_V4R4_QUALIFICATION_REQUEST_SCHEMA,
            "schema_version": AV1_V4R4_QUALIFICATION_REQUEST_SCHEMA_VERSION,
            "contract_version": AV1_V4R4_QUALIFICATION_REQUEST_CONTRACT_VERSION,
            "manifest_valid_until": "2027-02-02T18:53:35Z",
            "state": AV1_V4R4_QUALIFICATION_REQUEST_STATE,
            "authority": AV1_V4R4_QUALIFICATION_REQUEST_AUTHORITY,
            "request_ttl_seconds": AV1_V4R4_QUALIFICATION_REQUEST_TTL_SECONDS,
            AV1_V4R4_QUALIFICATION_REQUEST_APPROVAL_FIELD: True,
            "request_scope": dict(AV1_V4R4_QUALIFICATION_REQUEST_SCOPE),
            "selection_or_partition_use_allowed": False,
            "requested_ordinal_count": len(av1_v4r4_ordinal_layout()),
        },
    )
    _assert_owner_principal(materialized.get("owner_principal"), "qualification request owner")
    _assert_repository(object_dict(materialized.get("reviewed_repository")), "qualification request reviewed repository")
    _assert_repository(object_dict(materialized.get("requesting_repository")), "qualification request requesting repository")
    if not _FREEZE_ID_RE.fullmatch(str(materialized.get("freeze_id") or "")):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 qualification request freeze ID is invalid"
        )
    for field, pattern in (
        ("preparation_bundle_id", _BUNDLE_ID_RE),
        ("effective_config_hmac_id", _CONFIG_HMAC_ID_RE),
        ("path_privacy_key_id", _KEY_ID_RE),
        ("toolchain_id", _TOOLCHAIN_ID_RE),
        ("runtime_id", _RUNTIME_ID_RE),
    ):
        if not pattern.fullmatch(str(materialized.get(field) or "")):
            raise AV1V4R4PreparationError(
                f"AV1 v4 r4 qualification request {field} is invalid"
            )
    _assert_sha(materialized.get("freeze_payload_sha256"), "freeze payload")
    _assert_sha(materialized.get("preparation_bundle_payload_sha256"), "preparation bundle payload")
    _assert_timeline_values(
        freeze_decided_at=str(materialized["freeze_decided_at"]),
        requested_at=str(materialized["requested_at"]),
        valid_until=str(materialized["valid_until"]),
    )
    expected_closures = [item["closure_id"] for item in build_av1_v4r4_closure_payloads()]
    if [str(item) for item in object_list(materialized.get("closure_ids"))] != expected_closures:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 qualification request closure coverage is invalid"
        )
    invocations = [
        dict(object_dict(item)) for item in object_list(materialized.get("invocation_digests"))
    ]
    if len(invocations) != len(av1_v4r4_ordinal_layout()):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 qualification request invocation count is invalid"
        )
    for index, invocation in enumerate(invocations, start=1):
        if invocation.get("ordinal") != index or not _INVOCATION_SHA_ID_RE.fullmatch(
            str(invocation.get("invocation_sha256") or "")
        ):
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 qualification request invocation binding is invalid"
            )
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(
        materialized,
        id_field="request_id",
        id_pattern=_REQUEST_ID_RE,
        expected_id_prefix="av1v4r4req_",
        domain=av1_v4r4_identity_domain("qualification-request"),
    )


def build_av1_v4r4_execution_preflight(
    *,
    qualification_request: Mapping[str, Any],
    owner_freeze: Mapping[str, Any],
    owner_principal: str,
    ordinal_registry_id: str,
    plan: Mapping[str, Any],
    preflight_repository_commit: str,
    preflight_repository_tree: str,
    created_at: str,
) -> dict[str, Any]:
    request = object_dict(qualification_request)
    freeze = object_dict(owner_freeze)
    plan_payload = object_dict(plan)
    assert_av1_v4r4_qualification_request(request)
    assert_av1_v4r4_owner_freeze(freeze)
    if not _ORDINAL_REGISTRY_ID_RE.fullmatch(ordinal_registry_id):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 execution preflight ordinal registry ID is invalid"
        )
    if not _ORDINAL_PLAN_ID_RE.fullmatch(str(plan_payload.get("plan_id") or "")):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 execution preflight plan binding is invalid"
        )
    _assert_sha(plan_payload.get("payload_sha256"), "ordinal plan payload")
    _assert_owner_principal(owner_principal, "execution preflight owner")
    if request.get("owner_principal") != owner_principal or freeze.get("owner_principal") != owner_principal:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 execution preflight owner binding is invalid"
        )
    preflight_repository = {
        "commit": preflight_repository_commit,
        "tree": preflight_repository_tree,
    }
    _assert_repository(preflight_repository, "execution preflight repository")
    created = _parse_timestamp(created_at, "execution preflight created_at")
    requested = _parse_timestamp(str(request["requested_at"]), "qualification requested_at")
    valid_until = _parse_timestamp(str(request["valid_until"]), "qualification valid_until")
    opens = _parse_timestamp(str(plan_payload["plan_opens_at"]), "plan_opens_at")
    closes = _parse_timestamp(str(plan_payload["plan_closes_at"]), "plan_closes_at")
    if not requested <= created < valid_until or not opens <= created < closes:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 execution preflight timing is invalid"
        )
    if request.get("freeze_id") != freeze.get("freeze_id") or request.get(
        "freeze_payload_sha256"
    ) != freeze.get("payload_sha256"):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 execution preflight request binding is invalid"
        )
    payload = {
        **_common_payload(
            schema=AV1_V4R4_EXECUTION_PREFLIGHT_SCHEMA,
            schema_version=AV1_V4R4_EXECUTION_PREFLIGHT_SCHEMA_VERSION,
            contract_version=AV1_V4R4_EXECUTION_PREFLIGHT_CONTRACT_VERSION,
        ),
        "manifest_valid_until": "2027-02-02T18:53:35Z",
        "state": AV1_V4R4_EXECUTION_PREFLIGHT_STATE,
        "authority": AV1_V4R4_EXECUTION_PREFLIGHT_AUTHORITY,
        "owner_principal": owner_principal,
        "created_at": created_at,
        "valid_until": str(request["valid_until"]),
        "qualification_request_id": request["request_id"],
        "qualification_request_payload_sha256": request["payload_sha256"],
        "freeze_id": freeze["freeze_id"],
        "freeze_payload_sha256": freeze["payload_sha256"],
        "ordinal_registry_id": ordinal_registry_id,
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "reviewed_repository": dict(object_dict(request["reviewed_repository"])),
        "requesting_repository": dict(object_dict(request["requesting_repository"])),
        "preflight_repository": preflight_repository,
        "plan_opens_at": plan_payload["plan_opens_at"],
        "plan_closes_at": plan_payload["plan_closes_at"],
        "requested_ordinal_count": len(av1_v4r4_ordinal_layout()),
        AV1_V4R4_EXECUTION_PREFLIGHT_APPROVAL_FIELD: True,
        "execution_readiness_state": AV1_V4R4_EXECUTION_PREFLIGHT_READINESS_STATE,
        "next_sequencing_ordinal": 1,
        "sequencing_grant_binding_stage": "post_preflight_publication",
        "preflight_scope": dict(AV1_V4R4_EXECUTION_PREFLIGHT_SCOPE),
        "selection_or_partition_use_allowed": False,
        **_false_authority_payload(),
    }
    bound = _bind_identity(
        payload,
        id_field="preflight_id",
        id_prefix="av1v4r4preflight_",
        domain=av1_v4r4_identity_domain("execution-preflight"),
    )
    assert_av1_v4r4_execution_preflight(bound)
    return bound


def assert_av1_v4r4_execution_preflight(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    _assert_fields(
        materialized,
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "manifest_valid_until",
            "preflight_id",
            "payload_sha256",
            "state",
            "authority",
            "owner_principal",
            "created_at",
            "valid_until",
            "qualification_request_id",
            "qualification_request_payload_sha256",
            "freeze_id",
            "freeze_payload_sha256",
            "ordinal_registry_id",
            "plan_id",
            "plan_payload_sha256",
            "reviewed_repository",
            "requesting_repository",
            "preflight_repository",
            "plan_opens_at",
            "plan_closes_at",
            "requested_ordinal_count",
            AV1_V4R4_EXECUTION_PREFLIGHT_APPROVAL_FIELD,
            "execution_readiness_state",
            "next_sequencing_ordinal",
            "sequencing_grant_binding_stage",
            "preflight_scope",
            "selection_or_partition_use_allowed",
            *AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
        },
        "execution preflight",
    )
    _assert_common_binding(
        materialized,
        {
            "schema": AV1_V4R4_EXECUTION_PREFLIGHT_SCHEMA,
            "schema_version": AV1_V4R4_EXECUTION_PREFLIGHT_SCHEMA_VERSION,
            "contract_version": AV1_V4R4_EXECUTION_PREFLIGHT_CONTRACT_VERSION,
            "manifest_valid_until": "2027-02-02T18:53:35Z",
            "state": AV1_V4R4_EXECUTION_PREFLIGHT_STATE,
            "authority": AV1_V4R4_EXECUTION_PREFLIGHT_AUTHORITY,
            AV1_V4R4_EXECUTION_PREFLIGHT_APPROVAL_FIELD: True,
            "execution_readiness_state": AV1_V4R4_EXECUTION_PREFLIGHT_READINESS_STATE,
            "preflight_scope": dict(AV1_V4R4_EXECUTION_PREFLIGHT_SCOPE),
            "selection_or_partition_use_allowed": False,
            "requested_ordinal_count": len(av1_v4r4_ordinal_layout()),
            "next_sequencing_ordinal": 1,
            "sequencing_grant_binding_stage": "post_preflight_publication",
        },
    )
    _assert_owner_principal(materialized.get("owner_principal"), "execution preflight owner")
    if not _REQUEST_ID_RE.fullmatch(str(materialized.get("qualification_request_id") or "")):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 execution preflight request ID is invalid"
        )
    _assert_sha(materialized.get("qualification_request_payload_sha256"), "qualification request payload")
    if not _FREEZE_ID_RE.fullmatch(str(materialized.get("freeze_id") or "")):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 execution preflight freeze ID is invalid"
        )
    _assert_sha(materialized.get("freeze_payload_sha256"), "freeze payload")
    if not _ORDINAL_REGISTRY_ID_RE.fullmatch(str(materialized.get("ordinal_registry_id") or "")):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 execution preflight ordinal registry ID is invalid"
        )
    if not _ORDINAL_PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or "")):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 execution preflight plan ID is invalid"
        )
    _assert_sha(materialized.get("plan_payload_sha256"), "ordinal plan payload")
    _assert_repository(object_dict(materialized.get("reviewed_repository")), "execution preflight reviewed repository")
    _assert_repository(object_dict(materialized.get("requesting_repository")), "execution preflight requesting repository")
    _assert_repository(object_dict(materialized.get("preflight_repository")), "execution preflight repository")
    created = _parse_timestamp(str(materialized.get("created_at") or ""), "execution preflight created_at")
    valid_until = _parse_timestamp(str(materialized.get("valid_until") or ""), "execution preflight valid_until")
    opens = _parse_timestamp(str(materialized.get("plan_opens_at") or ""), "execution preflight plan_opens_at")
    closes = _parse_timestamp(str(materialized.get("plan_closes_at") or ""), "execution preflight plan_closes_at")
    if not opens <= created < valid_until <= closes:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 execution preflight window is invalid"
        )
    _assert_false_authorities(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(
        materialized,
        id_field="preflight_id",
        id_pattern=_PREFLIGHT_ID_RE,
        expected_id_prefix="av1v4r4preflight_",
        domain=av1_v4r4_identity_domain("execution-preflight"),
    )


def serialize_av1_v4r4_rights_attestation(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_rights_attestation(payload)
    return _canonical_bytes(payload)


def serialize_av1_v4r4_preparation_registry_binding(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_preparation_registry_binding(payload)
    return _canonical_bytes(payload)


def serialize_av1_v4r4_preparation_grant(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_preparation_grant(payload)
    return _canonical_bytes(payload)


def serialize_av1_v4r4_preparation_claim(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_preparation_claim(payload)
    return _canonical_bytes(payload)


def serialize_av1_v4r4_path_privacy_key_custody(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_path_privacy_key_custody(payload)
    return _canonical_bytes(payload)


def serialize_av1_v4r4_effective_config_snapshot(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_effective_config_snapshot(payload)
    return _canonical_bytes(payload)


def serialize_av1_v4r4_preparation_bundle(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_preparation_bundle(payload)
    return _canonical_bytes(payload)


def serialize_av1_v4r4_preparation_measurement(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_preparation_measurement(payload)
    return _canonical_bytes(payload)


def serialize_av1_v4r4_owner_freeze(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_owner_freeze(payload)
    return _canonical_bytes(payload)


def serialize_av1_v4r4_qualification_request(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_qualification_request(payload)
    return _canonical_bytes(payload)


def serialize_av1_v4r4_execution_preflight(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_execution_preflight(payload)
    return _canonical_bytes(payload)


def deserialize_av1_v4r4_rights_attestation(data: bytes) -> dict[str, Any]:
    payload = _load_canonical_mapping(data, "rights attestation")
    assert_av1_v4r4_rights_attestation(payload)
    return payload


def deserialize_av1_v4r4_preparation_registry_binding(data: bytes) -> dict[str, Any]:
    payload = _load_canonical_mapping(data, "registry binding")
    assert_av1_v4r4_preparation_registry_binding(payload)
    return payload


def deserialize_av1_v4r4_preparation_grant(data: bytes) -> dict[str, Any]:
    payload = _load_canonical_mapping(data, "preparation grant")
    assert_av1_v4r4_preparation_grant(payload)
    return payload


def deserialize_av1_v4r4_preparation_claim(data: bytes) -> dict[str, Any]:
    payload = _load_canonical_mapping(data, "preparation claim")
    assert_av1_v4r4_preparation_claim(payload)
    return payload


def deserialize_av1_v4r4_path_privacy_key_custody(data: bytes) -> dict[str, Any]:
    payload = _load_canonical_mapping(data, "key custody")
    assert_av1_v4r4_path_privacy_key_custody(payload)
    return payload


def deserialize_av1_v4r4_effective_config_snapshot(data: bytes) -> dict[str, Any]:
    payload = _load_canonical_mapping(data, "effective config")
    assert_av1_v4r4_effective_config_snapshot(payload)
    return payload


def deserialize_av1_v4r4_preparation_bundle(data: bytes) -> dict[str, Any]:
    payload = _load_canonical_mapping(data, "preparation bundle")
    assert_av1_v4r4_preparation_bundle(payload)
    return payload


def deserialize_av1_v4r4_preparation_measurement(data: bytes) -> dict[str, Any]:
    payload = _load_canonical_mapping(data, "preparation measurement")
    assert_av1_v4r4_preparation_measurement(payload)
    return payload


def deserialize_av1_v4r4_owner_freeze(data: bytes) -> dict[str, Any]:
    payload = _load_canonical_mapping(data, "owner freeze")
    assert_av1_v4r4_owner_freeze(payload)
    return payload


def deserialize_av1_v4r4_qualification_request(data: bytes) -> dict[str, Any]:
    payload = _load_canonical_mapping(data, "qualification request")
    assert_av1_v4r4_qualification_request(payload)
    return payload


def deserialize_av1_v4r4_execution_preflight(data: bytes) -> dict[str, Any]:
    payload = _load_canonical_mapping(data, "execution preflight")
    assert_av1_v4r4_execution_preflight(payload)
    return payload


def _measurement_common(
    *,
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    started_at: str,
    completed_at: str,
    probes: Sequence[Mapping[str, Any]],
    stages_completed: Sequence[str],
    state: str,
) -> dict[str, Any]:
    _parse_timestamp(started_at, "measurement started_at")
    _parse_timestamp(completed_at, "measurement completed_at")
    return {
        **_common_payload(
            schema=AV1_V4R4_PREPARATION_MEASUREMENT_SCHEMA,
            schema_version=AV1_V4R4_PREPARATION_MEASUREMENT_SCHEMA_VERSION,
            contract_version=AV1_V4R4_PREPARATION_MEASUREMENT_CONTRACT_VERSION,
        ),
        "state": state,
        "preparation_grant_id": preparation_grant["grant_id"],
        "preparation_grant_payload_sha256": preparation_grant["payload_sha256"],
        "claim_id": preparation_claim["claim_id"],
        "claim_payload_sha256": preparation_claim["payload_sha256"],
        "started_at": started_at,
        "completed_at": completed_at,
        "stages_completed": list(stages_completed),
        "method": dict(AV1_V4R4_PREPARATION_METHOD),
        "probes": [dict(object_dict(item)) for item in probes],
        "tool_version_probe_subprocess_count": len(probes),
        **_false_authority_payload(),
    }


def _is_stage_prefix(stages_completed: Sequence[str]) -> bool:
    return tuple(stages_completed) == AV1_V4R4_PREPARATION_STAGES[: len(stages_completed)]


def _normalized_prior_rights_attestation(payload: Any) -> dict[str, Any]:
    materialized = object_dict(payload)
    try:
        assert_av1_v4r3_rights_attestation(materialized)
    except AV1V4R3RightsError as exc:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 prior rights attestation must be canonical v4r3 rights"
        ) from exc
    if not _PRIOR_RIGHTS_ID_RE.fullmatch(str(materialized.get("attestation_id") or "")):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 prior rights attestation ID is invalid"
        )
    _assert_sha(materialized.get("payload_sha256"), "prior rights payload")
    _assert_owner_principal(materialized.get("owner_principal"), "prior rights owner")
    _assert_no_private_text(materialized)
    return materialized


def _common_payload(
    *,
    schema: str,
    schema_version: int,
    contract_version: str,
) -> dict[str, Any]:
    return {
        "schema": schema,
        "schema_version": schema_version,
        "contract_version": contract_version,
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "experiment_id": AV1_V4R4_EXPERIMENT_ID,
        "manifest_id": AV1_V4R4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
    }


def _assert_common_binding(
    payload: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    base = {
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "experiment_id": AV1_V4R4_EXPERIMENT_ID,
        "manifest_id": AV1_V4R4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
        **dict(expected),
    }
    if any(payload.get(key) != value for key, value in base.items()):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation artifact binding is invalid"
        )


def _false_authority_payload() -> dict[str, bool]:
    return {field: False for field in sorted(AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS)}


def _assert_false_authorities(payload: Mapping[str, Any]) -> None:
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if payload.get(field) is not False:
            raise AV1V4R4PreparationError(
                f"AV1 v4 r4 artifact cannot authorize {field}"
            )


def _assert_fields(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(payload) != set(expected):
        raise AV1V4R4PreparationError(
            f"AV1 v4 r4 {label} shape is invalid"
        )


def _assert_identity(
    payload: Mapping[str, Any],
    *,
    id_field: str,
    id_pattern: re.Pattern[str],
    expected_id_prefix: str,
    domain: str,
) -> None:
    identifier = str(payload.get(id_field) or "")
    if not id_pattern.fullmatch(identifier):
        raise AV1V4R4PreparationError(
            f"AV1 v4 r4 {id_field} is invalid"
        )
    semantic = {
        key: value for key, value in payload.items() if key not in {id_field, "payload_sha256"}
    }
    expected_identifier = expected_id_prefix + stable_json_hash(
        {"domain": domain, "payload": semantic}
    )[:32]
    if identifier != expected_identifier:
        raise AV1V4R4PreparationError(
            f"AV1 v4 r4 {id_field} does not match its payload"
        )
    without_sha = {key: value for key, value in payload.items() if key != "payload_sha256"}
    expected_sha = f"sha256:{stable_json_hash(without_sha)}"
    if payload.get("payload_sha256") != expected_sha:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 payload digest does not match"
        )


def _bind_identity(
    payload: Mapping[str, Any],
    *,
    id_field: str,
    id_prefix: str,
    domain: str,
) -> dict[str, Any]:
    materialized = json.loads(canonical_json_bytes(payload))
    semantic = {key: value for key, value in materialized.items() if key not in {id_field, "payload_sha256"}}
    materialized[id_field] = id_prefix + stable_json_hash(
        {"domain": domain, "payload": semantic}
    )[:32]
    without_sha = {key: value for key, value in materialized.items() if key != "payload_sha256"}
    materialized["payload_sha256"] = f"sha256:{stable_json_hash(without_sha)}"
    return json.loads(canonical_json_bytes(materialized))


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    return canonical_json_bytes(materialized) + b"\n"


def _load_canonical_mapping(data: bytes, label: str) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise AV1V4R4PreparationError(
            f"AV1 v4 r4 {label} input must be bytes"
        )
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AV1V4R4PreparationError(
            f"AV1 v4 r4 {label} JSON is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise AV1V4R4PreparationError(
            f"AV1 v4 r4 {label} payload must be a JSON object"
        )
    canonical = canonical_json_bytes(payload) + b"\n"
    if data != canonical:
        raise AV1V4R4PreparationError(
            f"AV1 v4 r4 {label} bytes are not canonical"
        )
    return payload


def _source_asset_ids() -> tuple[str, ...]:
    return tuple(str(item["asset_id"]) for item in AV1_V4R4_SOURCE_LAYOUT)


def _source_spec(asset_id: str) -> Mapping[str, Any]:
    try:
        return AV1_V4R4_PREPARATION_SOURCE_SPECS[asset_id]
    except KeyError as exc:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 preparation source asset ID is invalid"
        ) from exc


def _normalize_toolchain(payload: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    materialized = object_dict(payload)
    if set(materialized) != {"ab_av1", "ffmpeg", "ffprobe"}:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 toolchain mapping is invalid"
        )
    normalized: dict[str, dict[str, str]] = {}
    for name in ("ab_av1", "ffmpeg", "ffprobe"):
        item = object_dict(materialized[name])
        if set(item) != {"binary_sha256", "version"}:
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 toolchain payload is invalid"
            )
        _assert_sha(item.get("binary_sha256"), f"toolchain {name} binary")
        version = str(item.get("version") or "")
        if not version or av1_validation_v4_contains_private_text(version):
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 toolchain version is invalid"
            )
        normalized[name] = {
            "binary_sha256": str(item["binary_sha256"]),
            "version": version,
        }
    return normalized


def _assert_toolchain(payload: Any) -> None:
    _normalize_toolchain(object_dict(payload))


def _normalize_runtime(payload: Mapping[str, str]) -> dict[str, str]:
    materialized = object_dict(payload)
    expected = {
        "python_implementation",
        "python_version",
        "platform_machine",
        "platform_system",
    }
    if set(materialized) != expected:
        raise AV1V4R4PreparationError("AV1 v4 r4 runtime payload is invalid")
    normalized: dict[str, str] = {}
    for key in sorted(expected):
        value = str(materialized.get(key) or "")
        if not value or av1_validation_v4_contains_private_text(value):
            raise AV1V4R4PreparationError(
                "AV1 v4 r4 runtime value is invalid"
            )
        normalized[key] = value
    return normalized


def _assert_runtime(payload: Any) -> None:
    _normalize_runtime(object_dict(payload))


def _assert_path_hmac_mapping(
    payload: Mapping[str, Any],
    expected_keys: set[str],
    pattern: re.Pattern[str],
    label: str,
) -> None:
    if set(payload) != expected_keys:
        raise AV1V4R4PreparationError(
            f"AV1 v4 r4 {label} map is invalid"
        )
    if any(not pattern.fullmatch(str(value or "")) for value in payload.values()):
        raise AV1V4R4PreparationError(
            f"AV1 v4 r4 {label} map is invalid"
        )


def _assert_sha(value: Any, label: str) -> None:
    if not _SHA256_RE.fullmatch(str(value or "")):
        raise AV1V4R4PreparationError(
            f"AV1 v4 r4 {label} is invalid"
        )


def _assert_owner_principal(value: Any, label: str) -> None:
    if not _OWNER_PRINCIPAL_RE.fullmatch(str(value or "")):
        raise AV1V4R4PreparationError(f"AV1 v4 r4 {label} is invalid")


def _assert_repository(payload: Mapping[str, Any], label: str) -> None:
    if set(payload) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(payload.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1V4R4PreparationError(f"AV1 v4 r4 {label} is invalid")


def _assert_window(
    authorized_at: str,
    valid_until: str,
    max_seconds: int,
    label: str,
) -> None:
    authorized = _parse_timestamp(authorized_at, f"{label} authorized_at")
    valid_until_ts = _parse_timestamp(valid_until, f"{label} valid_until")
    if valid_until_ts <= authorized or (valid_until_ts - authorized).total_seconds() > max_seconds:
        raise AV1V4R4PreparationError(f"AV1 v4 r4 {label} window is invalid")


def _assert_timeline_values(
    *,
    freeze_decided_at: str,
    requested_at: str,
    valid_until: str,
) -> None:
    freeze_ts = _parse_timestamp(freeze_decided_at, "freeze decided_at")
    requested_ts = _parse_timestamp(requested_at, "requested_at")
    valid_until_ts = _parse_timestamp(valid_until, "valid_until")
    if requested_ts < freeze_ts:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 qualification request submission is too early"
        )
    if (requested_ts - freeze_ts).total_seconds() > AV1_V4R4_QUALIFICATION_REQUEST_MAX_SUBMISSION_LAG_SECONDS:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 qualification request submission lag is too long"
        )
    expected_valid_until = requested_ts + timedelta(
        seconds=AV1_V4R4_QUALIFICATION_REQUEST_TTL_SECONDS,
    )
    if valid_until_ts != expected_valid_until:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 qualification request validity deadline is invalid"
        )


def _assert_no_private_text(payload: Any) -> None:
    serialized = canonical_json_bytes(payload).decode("utf-8", errors="ignore")
    if av1_validation_v4_contains_private_text(serialized):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 artifact exposes machine-local text"
        )


def _assert_absolute_path_mapping(
    payload: Mapping[str, Any],
    expected_keys: set[str],
    label: str,
) -> None:
    if set(payload) != expected_keys:
        raise AV1V4R4PreparationError(
            f"AV1 v4 r4 {label} mapping is invalid"
        )
    for value in payload.values():
        _canonical_absolute_posix_path(str(value))


def _assert_key_bytes(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != 32:
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 path-privacy key must contain exactly 32 bytes"
        )


def _hmac_hex(key: bytes, payload: Mapping[str, Any]) -> str:
    return hmac.new(key, canonical_json_bytes(payload), hashlib.sha256).hexdigest()


def _prefixed_id(prefix: str, payload: Mapping[str, Any], domain: str) -> str:
    return prefix + stable_json_hash({"domain": domain, "payload": payload})[:32]


def _canonical_absolute_posix_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or not value.startswith("/")
        or value == "/"
        or "//" in value
        or value.endswith("/")
    ):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 path must be a canonical absolute POSIX path"
        )
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        raise AV1V4R4PreparationError(
            "AV1 v4 r4 path must be a canonical absolute POSIX path"
        )
    return value


def _parse_timestamp(value: str, label: str) -> datetime:
    if not _TIMESTAMP_RE.fullmatch(str(value or "")):
        raise AV1V4R4PreparationError(f"AV1 v4 r4 {label} is invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise AV1V4R4PreparationError(f"AV1 v4 r4 {label} is invalid") from exc


def _format_ts(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None or value.microsecond != 0:
        raise AV1V4R4PreparationError("AV1 v4 r4 timestamp is invalid")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
