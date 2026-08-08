"""Pure revision-3 rights rollover for AV1 protocol v4.

The rollover embeds one validated revision-2 rights attestation as inert
provenance, preserves its terms and source claims exactly, and records an
independent owner reaffirmation against the approved revision-3 manifest.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE,
    AV1ValidationV4RightsError,
    assert_av1_validation_v4_rights_attestation,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_FULL_VIDEO_POLICY,
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_OUTPUT_CONTAINER,
    AV1_V4_R3_PROTOCOL_VERSION,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    AV1_V4R3_MANIFEST_APPROVED_AT,
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
    AV1_V4R3_MANIFEST_VALID_UNTIL,
)


AV1_V4R3_RIGHTS_SCHEMA = "mediaforce.av1_cold_start_v4r3_rights_attestation"
AV1_V4R3_RIGHTS_SCHEMA_VERSION = 1
AV1_V4R3_RIGHTS_CONTRACT_VERSION = "acsv4r3rights1"
AV1_V4R3_RIGHTS_STATE = "owner_attested"
AV1_V4R3_RIGHTS_REAFFIRMED_AT = "2026-08-08T02:22:20Z"
AV1_V4R3_RIGHTS_IDENTITY_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:3:rights:identity:v1"
)

_ATTESTATION_ID_RE = re.compile(r"av1vrights4r3_[0-9a-f]{32}\Z")
_PAYLOAD_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_PAYLOAD_DEPTH = 32
_MAX_PAYLOAD_NODES = 10_000
_PRIOR_WRAPPER_KEYS = frozenset({"artifact", "confers_revision_3_authority"})
_ALLOWED_TOP_LEVEL_KEYS = (
    frozenset(
        {
            "attestation_id",
            "attested_at",
            "contract_version",
            "derivative_profile_acknowledgement",
            "discovery_public_sha256",
            "experiment_id",
            "manifest_id",
            "manifest_payload_sha256",
            "manifest_revision",
            "owner_attested",
            "owner_principal",
            "payload_sha256",
            "prior_revision_attestation",
            "protocol_version",
            "schema",
            "schema_version",
            "source_claims",
            "state",
            "terms",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)


class AV1V4R3RightsError(ValueError):
    """Raised when a revision-3 rights rollover is invalid."""


def build_av1_v4r3_rights_attestation(
    *,
    prior_revision_attestation: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an owner-reaffirmed r3 record from validated r2 provenance."""

    prior = object_dict(prior_revision_attestation)
    _assert_no_private_text(prior)
    _assert_prior_revision_attestation(prior)
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_RIGHTS_SCHEMA,
        "schema_version": AV1_V4R3_RIGHTS_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_RIGHTS_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_V4R3_RIGHTS_STATE,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "owner_attested": True,
        "owner_principal": prior["owner_principal"],
        "attested_at": AV1_V4R3_RIGHTS_REAFFIRMED_AT,
        "prior_revision_attestation": {
            "artifact": prior,
            "confers_revision_3_authority": False,
        },
        "terms": prior["terms"],
        "source_claims": prior["source_claims"],
        "derivative_profile_acknowledgement": _derivative_profile_acknowledgement(),
        **{field: False for field in sorted(AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS)},
    }
    bound = _bind_identity(payload)
    assert_av1_v4r3_rights_attestation(bound)
    return bound


def assert_av1_v4r3_rights_attestation(payload: Mapping[str, Any]) -> None:
    """Validate one owner-attested revision-3 rights rollover."""

    materialized = object_dict(payload)
    if not materialized:
        raise AV1V4R3RightsError("AV1 v4 r3 rights payload is invalid")
    unknown = set(materialized) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise AV1V4R3RightsError(
            f"AV1 v4 r3 rights payload contains unknown fields: {sorted(unknown)}"
        )
    _assert_no_private_text(materialized)
    _assert_base_binding(materialized)
    _assert_false_authorities(materialized)
    prior = _assert_prior_wrapper(materialized)
    _assert_owner_reaffirmation(materialized, prior)
    _assert_identity(materialized)


def serialize_av1_v4r3_rights_attestation(payload: Mapping[str, Any]) -> bytes:
    """Return canonical JSON bytes for a validated r3 rights record."""

    assert_av1_v4r3_rights_attestation(payload)
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_rights_attestation(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_rights_attestation(data: bytes) -> dict[str, Any]:
    """Load canonical r3 rights bytes without filesystem access."""

    if not isinstance(data, bytes):
        raise AV1V4R3RightsError("AV1 v4 r3 rights input must be bytes")
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AV1V4R3RightsError("AV1 v4 r3 rights JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise AV1V4R3RightsError("AV1 v4 r3 rights payload must be a JSON object")
    assert_av1_v4r3_rights_attestation(payload)
    try:
        canonical = canonical_json_bytes(payload) + b"\n"
    except (RecursionError, TypeError, ValueError) as exc:
        raise AV1V4R3RightsError("AV1 v4 r3 rights JSON is invalid") from exc
    if data != canonical:
        raise AV1V4R3RightsError("AV1 v4 r3 rights bytes are not canonical")
    return payload


def av1_v4r3_rights_attestation_id(payload: Mapping[str, Any]) -> str:
    """Return the revision-scoped identity for one rights payload."""

    _assert_no_private_text(payload)
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"attestation_id", "payload_sha256"}
    }
    digest = stable_json_hash(
        {
            "domain": AV1_V4R3_RIGHTS_IDENTITY_DOMAIN,
            "payload": semantic,
        }
    )
    return f"av1vrights4r3_{digest[:32]}"


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["attestation_id"] = av1_v4r3_rights_attestation_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _derivative_profile_acknowledgement() -> dict[str, Any]:
    return {
        "owner_reaffirmed": True,
        "decision_scope": "non_executing_preparation_planning",
        "output_video_codec": "av1",
        "encoder": AV1_V4_R3_FULL_VIDEO_POLICY["encoder"],
        "output_container": AV1_V4_R3_OUTPUT_CONTAINER,
        "video_only": True,
        "max_height": AV1_V4_R3_FULL_VIDEO_POLICY["max_height"],
        "target_size_mode": AV1_V4_R3_FULL_VIDEO_POLICY["size_goal_mode"],
        "reference_target_size_bytes": AV1_V4_R3_FULL_VIDEO_POLICY["target_size_bytes"],
        "nasa_acknowledgement_required_on_future_output_or_publication": True,
        "endorsement_prohibited": True,
        "redistribution_disposition": "none",
        "terms_documents_re_reviewed": False,
    }


def _assert_base_binding(payload: Mapping[str, Any]) -> None:
    expected = {
        "schema": AV1_V4R3_RIGHTS_SCHEMA,
        "schema_version": AV1_V4R3_RIGHTS_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_RIGHTS_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_V4R3_RIGHTS_STATE,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "derivative_profile_acknowledgement": _derivative_profile_acknowledgement(),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise AV1V4R3RightsError("AV1 v4 r3 rights binding is invalid")


def _assert_false_authorities(payload: Mapping[str, Any]) -> None:
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if payload.get(field) is not False:
            raise AV1V4R3RightsError(f"AV1 v4 r3 rights cannot authorize {field}")


def _assert_prior_revision_attestation(prior: Mapping[str, Any]) -> None:
    try:
        assert_av1_validation_v4_rights_attestation(prior)
    except AV1ValidationV4RightsError as exc:
        raise AV1V4R3RightsError(
            "AV1 v4 r3 rights prior attestation is invalid"
        ) from exc
    if prior.get("state") != AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE:
        raise AV1V4R3RightsError(
            "AV1 v4 r3 rights requires an owner-attested prior record"
        )


def _assert_prior_wrapper(payload: Mapping[str, Any]) -> dict[str, Any]:
    wrapper = object_dict(payload.get("prior_revision_attestation"))
    if set(wrapper) != _PRIOR_WRAPPER_KEYS:
        raise AV1V4R3RightsError(
            "AV1 v4 r3 rights prior attestation wrapper is invalid"
        )
    if wrapper.get("confers_revision_3_authority") is not False:
        raise AV1V4R3RightsError(
            "AV1 v4 r3 rights prior attestation cannot confer authority"
        )
    prior = object_dict(wrapper.get("artifact"))
    _assert_prior_revision_attestation(prior)
    if canonical_json_bytes(payload.get("terms")) != canonical_json_bytes(
        prior.get("terms")
    ) or canonical_json_bytes(payload.get("source_claims")) != canonical_json_bytes(
        prior.get("source_claims")
    ):
        raise AV1V4R3RightsError(
            "AV1 v4 r3 rights terms and claims must match prior provenance"
        )
    return prior


def _assert_owner_reaffirmation(
    payload: Mapping[str, Any], prior: Mapping[str, Any]
) -> None:
    if payload.get("owner_attested") is not True:
        raise AV1V4R3RightsError(
            "AV1 v4 r3 rights requires independent owner attestation"
        )
    owner_principal = payload.get("owner_principal")
    if (
        not isinstance(owner_principal, str)
        or not owner_principal.strip()
        or len(owner_principal) > 128
        or owner_principal != prior.get("owner_principal")
    ):
        raise AV1V4R3RightsError("AV1 v4 r3 rights owner principal is invalid")
    if payload.get("attested_at") != AV1_V4R3_RIGHTS_REAFFIRMED_AT:
        raise AV1V4R3RightsError(
            "AV1 v4 r3 rights reaffirmation timestamp is not frozen"
        )
    reaffirmed_at = _parse_timestamp(AV1_V4R3_RIGHTS_REAFFIRMED_AT)
    manifest_approved_at = _parse_timestamp(AV1_V4R3_MANIFEST_APPROVED_AT)
    manifest_valid_until = _parse_timestamp(AV1_V4R3_MANIFEST_VALID_UNTIL)
    prior_attested_at = _parse_timestamp(prior.get("attested_at"))
    if not (
        prior_attested_at
        <= manifest_approved_at
        <= reaffirmed_at
        < manifest_valid_until
    ):
        raise AV1V4R3RightsError(
            "AV1 v4 r3 rights reaffirmation timestamps are out of order"
        )


def _assert_identity(payload: Mapping[str, Any]) -> None:
    attestation_id = str(payload.get("attestation_id") or "")
    if not _ATTESTATION_ID_RE.fullmatch(attestation_id):
        raise AV1V4R3RightsError("AV1 v4 r3 rights attestation ID is invalid")
    if attestation_id != av1_v4r3_rights_attestation_id(payload):
        raise AV1V4R3RightsError(
            "AV1 v4 r3 rights attestation ID does not match its payload"
        )
    payload_without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    expected_sha256 = f"sha256:{stable_json_hash(payload_without_sha)}"
    if payload.get(
        "payload_sha256"
    ) != expected_sha256 or not _PAYLOAD_SHA256_RE.fullmatch(
        str(payload.get("payload_sha256") or "")
    ):
        raise AV1V4R3RightsError("AV1 v4 r3 rights payload SHA-256 does not match")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R3RightsError("AV1 v4 r3 rights timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1V4R3RightsError("AV1 v4 r3 rights timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.microsecond != 0:
        raise AV1V4R3RightsError("AV1 v4 r3 rights timestamp is invalid")
    return parsed.astimezone(UTC)


def _assert_no_private_text(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > _MAX_PAYLOAD_DEPTH or nodes > _MAX_PAYLOAD_NODES:
            raise AV1V4R3RightsError(
                "AV1 v4 r3 rights payload structure is too deep or large"
            )
        if isinstance(current, str):
            if av1_validation_v4_contains_private_text(current):
                raise AV1V4R3RightsError(
                    "AV1 v4 r3 rights payload contains private path-like text"
                )
            continue
        if isinstance(current, float) and not math.isfinite(current):
            raise AV1V4R3RightsError(
                "AV1 v4 r3 rights payload contains a non-finite number"
            )
        if isinstance(current, Mapping):
            for key, nested in current.items():
                stack.append((str(key), depth + 1))
                stack.append((nested, depth + 1))
            continue
        if isinstance(current, Sequence) and not isinstance(
            current, (bytes, bytearray)
        ):
            stack.extend((nested, depth + 1) for nested in current)
