"""Pure path-privacy identity contract for AV1 protocol-v4 revision 3."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.tuning.av1_validation_v4 import AV1_VALIDATION_V4_SOURCE_IDS
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_PROTOCOL_VERSION,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    AV1_V4R3_INSTANCE_PATH_HMAC_DOMAIN,
    AV1_V4R3_PATH_PRIVACY_KEY_ID_DOMAIN,
    AV1_V4R3_SOURCE_PATH_HMAC_DOMAIN,
    build_av1_v4r3_manifest_payload,
)
from mediaforce.tuning.av1_validation_v4r3_paths import (
    AV1V4R3CanonicalPathError,
    canonical_av1_v4r3_absolute_posix_path,
)


AV1_V4R3_PATH_PRIVACY_KEY_BYTES_MIN = 32
AV1_V4R3_PATH_PRIVACY_KEY_ID_PREFIX = "av1vpathkey4r3_"
AV1_V4R3_INSTANCE_PATH_ID_PREFIX = "av1vpath4r3_"
AV1_V4R3_SOURCE_PATH_ID_PREFIX = "av1vsource4r3_"
AV1_V4R3_INSTANCE_PATH_ROLES = frozenset(
    {
        "runtime_lock",
        "source_root",
        "state_root",
        "temp_root",
    }
)


class AV1V4R3PathPrivacyError(ValueError):
    """Raised when a revision-3 path-privacy derivation is invalid."""


def av1_v4r3_path_privacy_contract_payload() -> dict[str, Any]:
    """Return the exact revision-3 path-privacy contract frozen in the manifest."""

    return {
        "algorithm": "hmac_sha256_v1",
        "key_bytes_min": AV1_V4R3_PATH_PRIVACY_KEY_BYTES_MIN,
        "single_key_with_domain_separation": True,
        "normalized_path_format": "absolute_posix_canonical",
        "key_id_domain": AV1_V4R3_PATH_PRIVACY_KEY_ID_DOMAIN,
        "key_id_prefix": AV1_V4R3_PATH_PRIVACY_KEY_ID_PREFIX,
        "instance_path_domain": AV1_V4R3_INSTANCE_PATH_HMAC_DOMAIN,
        "instance_path_id_prefix": AV1_V4R3_INSTANCE_PATH_ID_PREFIX,
        "source_path_domain": AV1_V4R3_SOURCE_PATH_HMAC_DOMAIN,
        "source_path_id_prefix": AV1_V4R3_SOURCE_PATH_ID_PREFIX,
        "selection_or_partition_use_allowed": False,
    }


def assert_av1_v4r3_path_privacy_contract() -> None:
    """Fail closed if the implementation drifts from the approved manifest."""

    manifest_contract = build_av1_v4r3_manifest_payload().get("revision_3_path_privacy")
    if manifest_contract != av1_v4r3_path_privacy_contract_payload():
        raise AV1V4R3PathPrivacyError(
            "AV1 v4 r3 path-privacy contract does not match the approved manifest"
        )


def av1_v4r3_path_privacy_key_id(key: bytes) -> str:
    """Derive a public key-identity token without exposing key material."""

    return (
        AV1_V4R3_PATH_PRIVACY_KEY_ID_PREFIX
        + _hmac_hex(
            key,
            {
                "domain": AV1_V4R3_PATH_PRIVACY_KEY_ID_DOMAIN,
                "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
                "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
            },
        )[:32]
    )


def av1_v4r3_instance_path_hmac_id(
    key: bytes,
    *,
    role: str,
    normalized_path: str,
) -> str:
    """Derive a role-scoped identity for one machine-local instance path."""

    if role not in AV1_V4R3_INSTANCE_PATH_ROLES:
        raise AV1V4R3PathPrivacyError("AV1 v4 r3 instance path role is invalid")
    path = _canonical_path(normalized_path)
    return (
        AV1_V4R3_INSTANCE_PATH_ID_PREFIX
        + _hmac_hex(
            key,
            {
                "domain": AV1_V4R3_INSTANCE_PATH_HMAC_DOMAIN,
                "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
                "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
                "role": role,
                "normalized_path": path,
            },
        )[:32]
    )


def av1_v4r3_source_path_hmac_id(
    key: bytes,
    *,
    asset_id: str,
    normalized_path: str,
) -> str:
    """Derive an asset-scoped identity for one machine-local source path."""

    if asset_id not in AV1_VALIDATION_V4_SOURCE_IDS:
        raise AV1V4R3PathPrivacyError("AV1 v4 r3 source path asset ID is invalid")
    path = _canonical_path(normalized_path)
    return (
        AV1_V4R3_SOURCE_PATH_ID_PREFIX
        + _hmac_hex(
            key,
            {
                "domain": AV1_V4R3_SOURCE_PATH_HMAC_DOMAIN,
                "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
                "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
                "asset_id": asset_id,
                "normalized_path": path,
            },
        )[:32]
    )


def _hmac_hex(key: bytes, payload: dict[str, Any]) -> str:
    _validate_key(key)
    return hmac.new(key, canonical_json_bytes(payload), hashlib.sha256).hexdigest()


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < AV1_V4R3_PATH_PRIVACY_KEY_BYTES_MIN:
        raise AV1V4R3PathPrivacyError(
            "AV1 v4 r3 path-privacy key must contain at least 32 bytes"
        )


def _canonical_path(value: str) -> str:
    try:
        return canonical_av1_v4r3_absolute_posix_path(value)
    except AV1V4R3CanonicalPathError as exc:
        raise AV1V4R3PathPrivacyError(str(exc)) from exc


assert_av1_v4r3_path_privacy_contract()
