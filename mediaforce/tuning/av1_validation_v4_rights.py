from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_SOURCE_IDS,
    AV1_VALIDATION_V4_VALID_UNTIL,
    AV1_VALIDATION_V4_REVISED_AT,
)


AV1_VALIDATION_V4_RIGHTS_SCHEMA = (
    "mediaforce.av1_cold_start_v4_rights_attestation"
)
AV1_VALIDATION_V4_RIGHTS_SCHEMA_VERSION = 1
AV1_VALIDATION_V4_RIGHTS_TEMPLATE_STATE = "template_unattested"
AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE = "owner_attested"

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ATTESTATION_ID_RE = re.compile(r"av1vrights4_[0-9a-f]{32}\Z")
_FORBIDDEN_PATH_PREFIXES = ("/Users/", "/Volumes/", "/opt/homebrew/")
_FORBIDDEN_CAPTURE_KEY_FRAGMENTS = (
    "archive_body",
    "capture",
    "page_bytes",
    "raw_html",
    "raw_terms",
    "snapshot",
)
_MAX_TERMS_SUMMARY_LENGTH = 400
_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "attestation_id",
    "attested_at",
    "discovery_public_sha256",
    "experiment_id",
    "manifest_id",
    "manifest_payload_sha256",
    "owner_attested",
    "owner_principal",
    "payload_sha256",
    "protocol_version",
    "schema",
    "schema_version",
    "source_claims",
    "state",
    "terms",
}) | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS

_RIGHTS_TERMS: dict[str, dict[str, int | str]] = {
    "cc-by-3.0-legalcode.html": {
        "bytes": 51_333,
        "sha256": "sha256:1575ec5fc503cb90e64fc44656704698266747597fe45ca56b0f38d7dfbec782",
    },
    "cosmos-license.html": {
        "bytes": 16_423,
        "sha256": "sha256:43a269c7d5da66da5f405e29579aaa804b537bff2c8c0b94cde164e2349e18d1",
    },
    "cosmos-object-license-response.xml": {
        "bytes": 332,
        "sha256": "sha256:136d8b638f45a0a134aa0d033e64c20dd843b1f2bbc066e1f9aaf17238afa7f3",
    },
    "nasa-earth-views-asset.json": {
        "bytes": 5_103,
        "sha256": "sha256:c177189905e827fd4ce2aec4c87ea1b0f827735fc3839a341a82459cd7b11567",
    },
    "nasa-earth-views-metadata.json": {
        "bytes": 28_922,
        "sha256": "sha256:749c95db8a639b9a873421da650dd5526bcfe4b24191341721334722ac412e70",
    },
    "nasa-media-guidelines.html": {
        "bytes": 313_117,
        "sha256": "sha256:252aeeb5e3183d68044cadd774506b3f1a8e8f709e1310ca41718bb4d16a9e30",
    },
    "netflix-techblog-readme.txt": {
        "bytes": 7_883,
        "sha256": "sha256:b997f0019e436499c35045bdcdc3df3ddf4a14a24a5a6695a1830c48aff3c9e6",
    },
    "sintel-sharing.html": {
        "bytes": 8_463,
        "sha256": "sha256:67f30f30c55022c6f7de66beb997f04117b24f7aca928a912286a53edaa4b627",
    },
    "tears-sharing.html": {
        "bytes": 11_331,
        "sha256": "sha256:c3defc0521e16af0a3afb6a96d084517620adf6434038e6a3a2ba1cf50f86947",
    },
}

_SOURCE_TERMS = {
    AV1_VALIDATION_V4_SOURCE_IDS[0]: (
        "sintel-sharing.html",
        "cc-by-3.0-legalcode.html",
    ),
    AV1_VALIDATION_V4_SOURCE_IDS[1]: (
        "cosmos-license.html",
        "cosmos-object-license-response.xml",
        "netflix-techblog-readme.txt",
        "cc-by-3.0-legalcode.html",
    ),
    AV1_VALIDATION_V4_SOURCE_IDS[2]: (
        "tears-sharing.html",
        "cc-by-3.0-legalcode.html",
    ),
    AV1_VALIDATION_V4_SOURCE_IDS[3]: (
        "nasa-media-guidelines.html",
        "nasa-earth-views-metadata.json",
        "nasa-earth-views-asset.json",
    ),
}


class AV1ValidationV4RightsError(ValueError):
    pass


def build_av1_validation_v4_rights_template() -> dict[str, Any]:
    payload = _base_payload(
        state=AV1_VALIDATION_V4_RIGHTS_TEMPLATE_STATE,
        owner_attested=False,
        owner_principal=None,
        attested_at=None,
        source_claims={asset_id: None for asset_id in AV1_VALIDATION_V4_SOURCE_IDS},
    )
    return _bind_identity(payload)


def build_av1_validation_v4_rights_attestation(
    *,
    owner_principal: str,
    attested_at: str,
    source_claims: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    payload = _base_payload(
        state=AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE,
        owner_attested=True,
        owner_principal=owner_principal,
        attested_at=attested_at,
        source_claims={
            str(asset_id): object_dict(claim)
            for asset_id, claim in source_claims.items()
        },
    )
    bound = _bind_identity(payload)
    assert_av1_validation_v4_rights_attestation(bound)
    return bound


def load_av1_validation_v4_rights_attestation(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights attestation is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights attestation must contain a JSON object"
        )
    if raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights attestation bytes are not canonical"
        )
    assert_av1_validation_v4_rights_attestation(payload)
    return payload


def serialize_av1_validation_v4_rights_attestation(
    payload: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_v4_rights_attestation(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def assert_av1_validation_v4_rights_attestation(
    payload: Mapping[str, Any],
) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1ValidationV4RightsError("AV1 v4 rights payload is invalid")
    unknown_fields = set(materialized) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_fields:
        raise AV1ValidationV4RightsError(
            f"AV1 v4 rights attestation contains unknown fields: {sorted(unknown_fields)}"
        )
    _assert_no_private_or_captured_content(materialized)
    _assert_base_binding(materialized)
    _assert_false_authorities(materialized)
    _assert_state(materialized)
    _assert_identity(materialized)


def av1_validation_v4_rights_attestation_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"attestation_id", "payload_sha256"}
    }
    return f"av1vrights4_{stable_json_hash(semantic)[:32]}"


def _base_payload(
    *,
    state: str,
    owner_attested: bool,
    owner_principal: str | None,
    attested_at: str | None,
    source_claims: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": AV1_VALIDATION_V4_RIGHTS_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_RIGHTS_SCHEMA_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": state,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "owner_attested": owner_attested,
        "owner_principal": owner_principal,
        "attested_at": attested_at,
        "terms": _RIGHTS_TERMS,
        "source_claims": dict(source_claims),
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    return json.loads(canonical_json_bytes(payload))


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["attestation_id"] = av1_validation_v4_rights_attestation_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _assert_base_binding(payload: Mapping[str, Any]) -> None:
    if (
        payload.get("schema") != AV1_VALIDATION_V4_RIGHTS_SCHEMA
        or payload.get("schema_version") != AV1_VALIDATION_V4_RIGHTS_SCHEMA_VERSION
        or payload.get("protocol_version") != AV1_VALIDATION_V4_PROTOCOL_VERSION
        or payload.get("experiment_id") != AV1_VALIDATION_V4_EXPERIMENT_ID
        or payload.get("manifest_id") != AV1_VALIDATION_V4_MANIFEST_ID
        or payload.get("manifest_payload_sha256") != AV1_VALIDATION_V4_PAYLOAD_SHA256
        or payload.get("discovery_public_sha256")
        != AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256
    ):
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights attestation binding is invalid"
        )
    if object_dict(payload.get("terms")) != _RIGHTS_TERMS:
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights terms digests are not frozen"
        )


def _assert_false_authorities(payload: Mapping[str, Any]) -> None:
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if payload.get(field) is not False:
            raise AV1ValidationV4RightsError(
                f"AV1 v4 rights attestation cannot authorize {field}"
            )


def _assert_state(payload: Mapping[str, Any]) -> None:
    state = payload.get("state")
    claims = object_dict(payload.get("source_claims"))
    if tuple(sorted(claims)) != tuple(sorted(AV1_VALIDATION_V4_SOURCE_IDS)):
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights source claim set is invalid"
        )
    if state == AV1_VALIDATION_V4_RIGHTS_TEMPLATE_STATE:
        if (
            payload.get("owner_attested") is not False
            or payload.get("owner_principal") is not None
            or payload.get("attested_at") is not None
            or any(value is not None for value in claims.values())
        ):
            raise AV1ValidationV4RightsError(
                "AV1 v4 rights template must remain unattested"
            )
        return
    if state != AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE:
        raise AV1ValidationV4RightsError("AV1 v4 rights state is invalid")
    if payload.get("owner_attested") is not True:
        raise AV1ValidationV4RightsError(
            "AV1 v4 completed rights record requires owner attestation"
        )
    owner_principal = payload.get("owner_principal")
    if (
        not isinstance(owner_principal, str)
        or not owner_principal.strip()
        or len(owner_principal) > 128
    ):
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights owner principal is invalid"
        )
    _assert_attested_at(payload.get("attested_at"))
    _assert_source_claims(claims)


def _assert_attested_at(value: Any) -> None:
    if not isinstance(value, str):
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights attestation timestamp is invalid"
        )
    try:
        attested_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights attestation timestamp is invalid"
        ) from exc
    if attested_at.tzinfo is None:
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights attestation timestamp must be timezone-aware"
        )
    active_at = datetime.fromisoformat(AV1_VALIDATION_V4_REVISED_AT.replace("Z", "+00:00"))
    valid_until = datetime.fromisoformat(AV1_VALIDATION_V4_VALID_UNTIL.replace("Z", "+00:00"))
    normalized = attested_at.astimezone(UTC)
    if normalized < active_at or normalized >= valid_until:
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights attestation timestamp is outside the manifest window"
        )


def _assert_source_claims(claims: Mapping[str, Any]) -> None:
    for asset_id in AV1_VALIDATION_V4_SOURCE_IDS:
        claim = object_dict(claims.get(asset_id))
        if not claim:
            raise AV1ValidationV4RightsError(
                f"AV1 v4 rights claim {asset_id} is absent"
            )
        if set(claim) != _expected_claim_keys(asset_id):
            raise AV1ValidationV4RightsError(
                f"AV1 v4 rights claim {asset_id} fields are invalid"
            )
        summary = claim.get("terms_summary")
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > _MAX_TERMS_SUMMARY_LENGTH
        ):
            raise AV1ValidationV4RightsError(
                f"AV1 v4 rights claim {asset_id} summary is invalid"
            )
        expected_common = {
            "attribution_required": True,
            "redistribution_disposition": "none",
            "terms_reviewed": list(_SOURCE_TERMS[asset_id]),
        }
        for key, value in expected_common.items():
            if claim.get(key) != value:
                raise AV1ValidationV4RightsError(
                    f"AV1 v4 rights claim {asset_id} {key} is invalid"
                )
        _assert_source_specific_claim(asset_id, claim)


def _expected_claim_keys(asset_id: str) -> set[str]:
    fields = {
        "attribution_required",
        "license_basis",
        "redistribution_disposition",
        "terms_reviewed",
        "terms_summary",
    }
    if asset_id == AV1_VALIDATION_V4_SOURCE_IDS[1]:
        fields.update({
            "license_precedence",
            "netflix_mirror_is_technical_provenance_only",
        })
    elif asset_id == AV1_VALIDATION_V4_SOURCE_IDS[3]:
        fields.update({
            "allowed_stream_indexes",
            "endorsement_prohibited",
            "excluded_stream_indexes",
            "nasa_acknowledgement_required",
            "third_party_audio_ingredients_present",
            "video_stream_only",
        })
    return fields


def _assert_source_specific_claim(asset_id: str, claim: Mapping[str, Any]) -> None:
    if asset_id in {
        AV1_VALIDATION_V4_SOURCE_IDS[0],
        AV1_VALIDATION_V4_SOURCE_IDS[2],
    }:
        if claim.get("license_basis") != "CC-BY-3.0":
            raise AV1ValidationV4RightsError(
                f"AV1 v4 rights claim {asset_id} license basis is invalid"
            )
        return
    if asset_id == AV1_VALIDATION_V4_SOURCE_IDS[1]:
        if (
            claim.get("license_basis") != "CC-BY-3.0"
            or claim.get("license_precedence")
            != "official_blender_gooseberry_title_grant"
            or claim.get("netflix_mirror_is_technical_provenance_only") is not True
        ):
            raise AV1ValidationV4RightsError(
                "AV1 v4 Cosmos rights precedence is invalid"
            )
        return
    if (
        claim.get("license_basis") != "NASA-media-guidelines"
        or claim.get("video_stream_only") is not True
        or claim.get("allowed_stream_indexes") != [0]
        or claim.get("excluded_stream_indexes") != [1]
        or claim.get("third_party_audio_ingredients_present") is not True
        or claim.get("nasa_acknowledgement_required") is not True
        or claim.get("endorsement_prohibited") is not True
    ):
        raise AV1ValidationV4RightsError(
            "AV1 v4 NASA rights constraints are invalid"
        )


def _assert_identity(payload: Mapping[str, Any]) -> None:
    attestation_id = str(payload.get("attestation_id") or "")
    if not _ATTESTATION_ID_RE.fullmatch(attestation_id):
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights attestation ID is invalid"
        )
    if attestation_id != av1_validation_v4_rights_attestation_id(payload):
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights attestation ID does not match its payload"
        )
    payload_without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    expected_sha256 = f"sha256:{stable_json_hash(payload_without_sha)}"
    if payload.get("payload_sha256") != expected_sha256:
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights payload SHA-256 does not match"
        )
    if not _SHA256_RE.fullmatch(str(payload.get("payload_sha256") or "")):
        raise AV1ValidationV4RightsError(
            "AV1 v4 rights payload SHA-256 is invalid"
        )


def _assert_no_private_or_captured_content(payload: Mapping[str, Any]) -> None:
    def visit(value: Any, key: str | None = None) -> None:
        normalized_key = (key or "").lower()
        if any(fragment in normalized_key for fragment in _FORBIDDEN_CAPTURE_KEY_FRAGMENTS):
            raise AV1ValidationV4RightsError(
                "AV1 v4 rights payload cannot contain captured terms content"
            )
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and any(
            prefix in value for prefix in _FORBIDDEN_PATH_PREFIXES
        ):
            raise AV1ValidationV4RightsError(
                "AV1 v4 rights payload exposes a machine-local path"
            )

    visit(payload)
