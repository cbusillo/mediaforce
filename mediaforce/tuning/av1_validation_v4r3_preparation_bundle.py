"""Pure prepared-unfrozen bundle contract for AV1 protocol-v4 revision 3."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
import json
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_PROTOCOL_VERSION,
    av1_v4_r3_quality_temp_hmac_id,
    av1_v4_r3_quality_temp_key_id,
    build_av1_v4_r3_all_closure_payloads,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
)
from mediaforce.tuning.av1_validation_v4r3_path_privacy import (
    AV1_V4R3_INSTANCE_PATH_ROLES,
    av1_v4r3_instance_path_hmac_id,
    av1_v4r3_path_privacy_key_id,
    av1_v4r3_source_path_hmac_id,
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


AV1_V4R3_PREPARATION_BUNDLE_SCHEMA = "mediaforce.av1_cold_start_v4r3_preparation_bundle"
AV1_V4R3_PREPARATION_BUNDLE_SCHEMA_VERSION = 1
AV1_V4R3_PREPARATION_BUNDLE_CONTRACT_VERSION = "av1v4r3prepbundle1"
AV1_V4R3_PREPARATION_BUNDLE_STATE = "prepared_unfrozen"
AV1_V4R3_TOOL_NAMES = ("ab_av1", "ffmpeg", "ffprobe")
AV1_V4R3_PREPARATION_INVOCATION_COUNT = 8
AV1_V4R3_EFFECTIVE_CONFIG_HMAC_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:effective-config:binding:v1"
)

_BUNDLE_ID_RE = re.compile(r"av1v4r3prepbundle_[0-9a-f]{32}\Z")
_TOOLCHAIN_ID_RE = re.compile(r"av1v4r3toolchain_[0-9a-f]{32}\Z")
_RUNTIME_ID_RE = re.compile(r"av1v4r3runtime_[0-9a-f]{32}\Z")
_RIGHTS_ID_RE = re.compile(r"av1vrights4r3_[0-9a-f]{32}\Z")
_GRANT_ID_RE = re.compile(r"av1vprepgrant4r3_[0-9a-f]{32}\Z")
_CLAIM_ID_RE = re.compile(r"av1v4r3prepclaim_[0-9a-f]{32}\Z")
_CUSTODY_ID_RE = re.compile(r"av1v4r3keycustody_[0-9a-f]{32}\Z")
_PATH_KEY_ID_RE = re.compile(r"av1vpathkey4r3_[0-9a-f]{32}\Z")
_QUALITY_KEY_ID_RE = re.compile(r"av1vqtkey4r3_[0-9a-f]{32}\Z")
_CONFIG_HMAC_ID_RE = re.compile(r"av1v4r3confighmac_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_ALLOWED_KEYS = (
    frozenset(
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
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
            "quality_temp_key_id",
            "effective_config_hmac_id",
            "toolchain",
            "toolchain_id",
            "runtime",
            "runtime_id",
            "source_path_hmac_ids",
            "dedicated_instance_hmac_ids",
            "quality_temp_hmac_ids",
            "closure_ids",
            "invocation_digests",
            "unresolved_execution_bindings",
            "selection_or_partition_use_allowed",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)


class AV1V4R3PreparationBundleError(ValueError):
    """Raised when a revision-3 prepared bundle is invalid."""


def build_av1_v4r3_preparation_bundle(
    *,
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    key_custody: Mapping[str, Any],
    effective_config: Mapping[str, Any],
    path_privacy_key: bytes,
    toolchain: Mapping[str, Mapping[str, str]],
    runtime: Mapping[str, str],
) -> dict[str, Any]:
    """Build all private identities and frozen invocation digests without I/O."""

    grant = object_dict(preparation_grant)
    claim = object_dict(preparation_claim)
    custody = object_dict(key_custody)
    config = object_dict(effective_config)
    assert_av1_v4r3_preparation_grant(grant)
    assert_av1_v4r3_preparation_claim(claim)
    assert_av1_v4r3_path_privacy_key_custody(custody)
    assert_av1_v4r3_effective_config_snapshot(config)
    _assert_upstream_chain(grant, claim, custody, config, path_privacy_key)
    normalized_toolchain = _normalized_toolchain(toolchain)
    normalized_runtime = _normalized_runtime(runtime)
    toolchain_id = _prefixed_id("av1v4r3toolchain_", normalized_toolchain)
    runtime_id = _prefixed_id("av1v4r3runtime_", normalized_runtime)
    source_ids = {
        asset_id: av1_v4r3_source_path_hmac_id(
            path_privacy_key,
            asset_id=asset_id,
            normalized_path=str(object_dict(config["source_paths"])[asset_id]),
        )
        for asset_id in AV1_VALIDATION_V4_SOURCE_IDS
    }
    instance_ids = {
        role: av1_v4r3_instance_path_hmac_id(
            path_privacy_key,
            role=role,
            normalized_path=str(object_dict(config["dedicated_instance_paths"])[role]),
        )
        for role in sorted(AV1_V4R3_INSTANCE_PATH_ROLES)
    }
    quality_temp_key_id = av1_v4_r3_quality_temp_key_id(path_privacy_key)
    effective_config_hmac_id = av1_v4r3_effective_config_hmac_id(
        path_privacy_key, config
    )
    quality_temp_ids = {
        asset_id: av1_v4_r3_quality_temp_hmac_id(
            path_privacy_key,
            asset_id=asset_id,
            normalized_path=str(object_dict(config["quality_temp_paths"])[asset_id]),
        )
        for asset_id in AV1_VALIDATION_V4_SOURCE_IDS
    }
    repository = dict(object_dict(config["repository"]))
    closures = build_av1_v4_r3_all_closure_payloads()
    invocation_digests = [
        {
            "ordinal": ordinal,
            "closure_id": closure["closure_id"],
            "invocation_sha256": "sha256:"
            + stable_json_hash(
                {
                    "closure": closure,
                    "source_path_hmac_id": source_ids[str(closure["asset_id"])],
                    "dedicated_instance_hmac_ids": instance_ids,
                    "quality_temp_hmac_id": quality_temp_ids[str(closure["asset_id"])],
                    "quality_temp_key_id": quality_temp_key_id,
                    "production_stream_plan_id": None,
                    "stream_budget_ledger_id": None,
                    "effective_config_hmac_id": effective_config_hmac_id,
                    "repository": repository,
                    "toolchain_id": toolchain_id,
                    "runtime_id": runtime_id,
                }
            ),
            "r3_repository": repository,
            "r3_toolchain_id": toolchain_id,
        }
        for ordinal, closure in enumerate(closures, start=1)
    ]
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_PREPARATION_BUNDLE_SCHEMA,
        "schema_version": AV1_V4R3_PREPARATION_BUNDLE_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_PREPARATION_BUNDLE_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "state": AV1_V4R3_PREPARATION_BUNDLE_STATE,
        "repository": repository,
        "rights_attestation_id": claim["rights_attestation_id"],
        "rights_attestation_payload_sha256": claim["rights_attestation_payload_sha256"],
        "preparation_grant_id": grant["grant_id"],
        "preparation_grant_payload_sha256": grant["payload_sha256"],
        "claim_id": claim["claim_id"],
        "claim_payload_sha256": claim["payload_sha256"],
        "custody_id": custody["custody_id"],
        "custody_payload_sha256": custody["payload_sha256"],
        "path_privacy_key_id": custody["key_id"],
        "quality_temp_key_id": quality_temp_key_id,
        "effective_config_hmac_id": effective_config_hmac_id,
        "toolchain": normalized_toolchain,
        "toolchain_id": toolchain_id,
        "runtime": normalized_runtime,
        "runtime_id": runtime_id,
        "source_path_hmac_ids": source_ids,
        "dedicated_instance_hmac_ids": instance_ids,
        "quality_temp_hmac_ids": quality_temp_ids,
        "closure_ids": [closure["closure_id"] for closure in closures],
        "invocation_digests": invocation_digests,
        "unresolved_execution_bindings": {
            "production_stream_plan_id": None,
            "stream_budget_ledger_id": None,
            "assigned_stage": "execution_preflight",
        },
        "selection_or_partition_use_allowed": False,
        **{field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS},
    }
    bound = _bind_identity(payload)
    assert_av1_v4r3_preparation_bundle(bound)
    return bound


def assert_av1_v4r3_preparation_bundle(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _ALLOWED_KEYS:
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation bundle shape is invalid"
        )
    expected = {
        "schema": AV1_V4R3_PREPARATION_BUNDLE_SCHEMA,
        "schema_version": AV1_V4R3_PREPARATION_BUNDLE_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_PREPARATION_BUNDLE_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "state": AV1_V4R3_PREPARATION_BUNDLE_STATE,
        "selection_or_partition_use_allowed": False,
        "unresolved_execution_bindings": {
            "production_stream_plan_id": None,
            "stream_budget_ledger_id": None,
            "assigned_stage": "execution_preflight",
        },
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation bundle binding is invalid"
        )
    _assert_no_private_text(materialized)
    _assert_repository(materialized.get("repository"))
    _assert_sha_fields(materialized)
    _assert_bound_ids(materialized)
    _assert_identity_mapping(
        materialized.get("source_path_hmac_ids"),
        set(AV1_VALIDATION_V4_SOURCE_IDS),
        "av1vsource4r3_",
    )
    _assert_identity_mapping(
        materialized.get("dedicated_instance_hmac_ids"),
        set(AV1_V4R3_INSTANCE_PATH_ROLES),
        "av1vpath4r3_",
    )
    _assert_identity_mapping(
        materialized.get("quality_temp_hmac_ids"),
        set(AV1_VALIDATION_V4_SOURCE_IDS),
        "av1vqtemp4r3_",
    )
    _assert_toolchain(materialized.get("toolchain"), materialized.get("toolchain_id"))
    _assert_runtime(materialized.get("runtime"), materialized.get("runtime_id"))
    _assert_invocations(materialized)
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R3PreparationBundleError(
                f"AV1 v4 r3 preparation bundle cannot authorize {field}"
            )
    bundle_id = str(materialized.get("bundle_id") or "")
    if not _BUNDLE_ID_RE.fullmatch(bundle_id) or bundle_id != _bundle_id(materialized):
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation bundle ID is invalid"
        )
    without_sha = {
        key: value for key, value in materialized.items() if key != "payload_sha256"
    }
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation bundle digest does not match"
        )


def serialize_av1_v4r3_preparation_bundle(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_preparation_bundle(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_preparation_bundle(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation bundle bytes are unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation bundle bytes are not canonical"
        )
    assert_av1_v4r3_preparation_bundle(payload)
    return payload


def av1_v4r3_effective_config_hmac_id(
    key: bytes,
    effective_config: Mapping[str, Any],
) -> str:
    """Bind a private config without publishing a path-guessing oracle."""

    config = object_dict(effective_config)
    assert_av1_v4r3_effective_config_snapshot(config)
    av1_v4r3_path_privacy_key_id(key)
    digest = hmac.new(
        key,
        canonical_json_bytes(
            {
                "domain": AV1_V4R3_EFFECTIVE_CONFIG_HMAC_DOMAIN,
                "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
                "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
                "effective_config": config,
            }
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"av1v4r3confighmac_{digest[:32]}"


def _assert_upstream_chain(
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    custody: Mapping[str, Any],
    config: Mapping[str, Any],
    key: bytes,
) -> None:
    repository = object_dict(grant.get("repository"))
    if (
        claim.get("preparation_grant_id") != grant.get("grant_id")
        or claim.get("preparation_grant_payload_sha256") != grant.get("payload_sha256")
        or custody.get("claim_id") != claim.get("claim_id")
        or custody.get("claim_payload_sha256") != claim.get("payload_sha256")
        or object_dict(claim.get("repository")) != repository
        or object_dict(custody.get("repository")) != repository
        or object_dict(config.get("repository")) != repository
        or custody.get("key_id") != av1_v4r3_path_privacy_key_id(key)
    ):
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation bundle upstream chain is invalid"
        )


def _normalized_toolchain(
    value: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    toolchain = object_dict(value)
    if set(toolchain) != set(AV1_V4R3_TOOL_NAMES):
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation toolchain set is invalid"
        )
    normalized: dict[str, dict[str, str]] = {}
    for name in AV1_V4R3_TOOL_NAMES:
        tool = object_dict(toolchain[name])
        if set(tool) != {"binary_sha256", "version"}:
            raise AV1V4R3PreparationBundleError(
                "AV1 v4 r3 preparation tool identity shape is invalid"
            )
        digest = tool.get("binary_sha256")
        version = tool.get("version")
        if (
            not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
            or not isinstance(version, str)
            or not version
            or "\n" in version
            or "\r" in version
            or av1_validation_v4_contains_private_text(version)
        ):
            raise AV1V4R3PreparationBundleError(
                "AV1 v4 r3 preparation tool identity is invalid"
            )
        normalized[name] = {"binary_sha256": digest, "version": version}
    return normalized


def _normalized_runtime(value: Mapping[str, str]) -> dict[str, str]:
    runtime = object_dict(value)
    expected = {
        "python_implementation",
        "python_version",
        "platform_system",
        "platform_machine",
    }
    if set(runtime) != expected or any(
        not isinstance(item, str)
        or not item
        or "\n" in item
        or "\r" in item
        or av1_validation_v4_contains_private_text(item)
        for item in runtime.values()
    ):
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation runtime identity is invalid"
        )
    return {key: str(runtime[key]) for key in sorted(expected)}


def _assert_toolchain(value: Any, toolchain_id: Any) -> None:
    normalized = _normalized_toolchain(object_dict(value))
    if (
        not isinstance(toolchain_id, str)
        or not _TOOLCHAIN_ID_RE.fullmatch(toolchain_id)
        or toolchain_id != _prefixed_id("av1v4r3toolchain_", normalized)
    ):
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation toolchain ID is invalid"
        )


def _assert_runtime(value: Any, runtime_id: Any) -> None:
    normalized = _normalized_runtime(object_dict(value))
    if (
        not isinstance(runtime_id, str)
        or not _RUNTIME_ID_RE.fullmatch(runtime_id)
        or runtime_id != _prefixed_id("av1v4r3runtime_", normalized)
    ):
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation runtime ID is invalid"
        )


def _assert_invocations(payload: Mapping[str, Any]) -> None:
    closures = build_av1_v4_r3_all_closure_payloads()
    if len(closures) != AV1_V4R3_PREPARATION_INVOCATION_COUNT:
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation requires exactly eight closures"
        )
    closure_ids = [closure["closure_id"] for closure in closures]
    if object_list(payload.get("closure_ids")) != closure_ids:
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation closure order is invalid"
        )
    invocations = object_list(payload.get("invocation_digests"))
    if len(invocations) != len(closures):
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation invocation count is invalid"
        )
    repository = dict(object_dict(payload.get("repository")))
    source_ids = object_dict(payload.get("source_path_hmac_ids"))
    instance_ids = object_dict(payload.get("dedicated_instance_hmac_ids"))
    quality_temp_ids = object_dict(payload.get("quality_temp_hmac_ids"))
    seen: set[str] = set()
    for ordinal, item in enumerate(invocations, start=1):
        invocation = object_dict(item)
        closure = closures[ordinal - 1]
        expected_digest = "sha256:" + stable_json_hash(
            {
                "closure": closure,
                "source_path_hmac_id": source_ids[str(closure["asset_id"])],
                "dedicated_instance_hmac_ids": instance_ids,
                "quality_temp_hmac_id": quality_temp_ids[str(closure["asset_id"])],
                "quality_temp_key_id": payload.get("quality_temp_key_id"),
                "production_stream_plan_id": None,
                "stream_budget_ledger_id": None,
                "effective_config_hmac_id": payload.get("effective_config_hmac_id"),
                "repository": repository,
                "toolchain_id": payload.get("toolchain_id"),
                "runtime_id": payload.get("runtime_id"),
            }
        )
        if set(invocation) != {
            "ordinal",
            "closure_id",
            "invocation_sha256",
            "r3_repository",
            "r3_toolchain_id",
        }:
            raise AV1V4R3PreparationBundleError(
                "AV1 v4 r3 preparation invocation shape is invalid"
            )
        digest = invocation.get("invocation_sha256")
        if (
            invocation.get("ordinal") != ordinal
            or invocation.get("closure_id") != closure_ids[ordinal - 1]
            or invocation.get("r3_repository") != repository
            or invocation.get("r3_toolchain_id") != payload.get("toolchain_id")
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
            or digest != expected_digest
            or digest in seen
        ):
            raise AV1V4R3PreparationBundleError(
                "AV1 v4 r3 preparation invocation binding is invalid"
            )
        seen.add(digest)


def _assert_identity_mapping(value: Any, expected_keys: set[str], prefix: str) -> None:
    identities = object_dict(value)
    if set(identities) != expected_keys or any(
        not isinstance(identity, str)
        or not re.fullmatch(rf"{re.escape(prefix)}[0-9a-f]{{32}}", identity)
        for identity in identities.values()
    ):
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation private identity set is invalid"
        )


def _assert_bound_ids(payload: Mapping[str, Any]) -> None:
    patterns = {
        "rights_attestation_id": _RIGHTS_ID_RE,
        "preparation_grant_id": _GRANT_ID_RE,
        "claim_id": _CLAIM_ID_RE,
        "custody_id": _CUSTODY_ID_RE,
        "path_privacy_key_id": _PATH_KEY_ID_RE,
        "quality_temp_key_id": _QUALITY_KEY_ID_RE,
        "effective_config_hmac_id": _CONFIG_HMAC_ID_RE,
    }
    if any(
        not pattern.fullmatch(str(payload.get(field) or ""))
        for field, pattern in patterns.items()
    ):
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation upstream identity is invalid"
        )


def _assert_repository(value: Any) -> None:
    repository = object_dict(value)
    if set(repository) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation repository is invalid"
        )


def _assert_sha_fields(payload: Mapping[str, Any]) -> None:
    for field in (
        "manifest_payload_sha256",
        "rights_attestation_payload_sha256",
        "preparation_grant_payload_sha256",
        "claim_payload_sha256",
        "custody_payload_sha256",
        "payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(payload.get(field) or "")):
            raise AV1V4R3PreparationBundleError(
                f"AV1 v4 r3 preparation {field} is invalid"
            )


def _assert_no_private_text(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_no_private_text(str(key))
            _assert_no_private_text(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_private_text(child)
    elif isinstance(value, str) and av1_validation_v4_contains_private_text(value):
        raise AV1V4R3PreparationBundleError(
            "AV1 v4 r3 preparation bundle exposes machine-local text"
        )


def _prefixed_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}{stable_json_hash(payload)[:32]}"


def _bundle_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"bundle_id", "payload_sha256"}
    }
    return _prefixed_id("av1v4r3prepbundle_", semantic)


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["bundle_id"] = _bundle_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


if len(build_av1_v4_r3_all_closure_payloads()) != AV1_V4R3_PREPARATION_INVOCATION_COUNT:
    raise RuntimeError("AV1 v4 r3 preparation requires exactly eight closures")
