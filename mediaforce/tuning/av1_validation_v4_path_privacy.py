from __future__ import annotations

import hashlib
import hmac
from pathlib import PurePosixPath
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_INSTANCE_PATH_HMAC_DOMAIN,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PATH_PRIVACY_KEY_ID_DOMAIN,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_SOURCE_IDS,
    AV1_VALIDATION_V4_SOURCE_PATH_HMAC_DOMAIN,
)


AV1_VALIDATION_V4_PATH_PRIVACY_KEY_BYTES_MIN = 32
AV1_VALIDATION_V4_INSTANCE_PATH_ROLES = frozenset({
    "runtime_lock",
    "source_root",
    "state_root",
    "temp_root",
})


class AV1ValidationV4PathPrivacyError(ValueError):
    pass


def av1_validation_v4_path_privacy_key_id(key: bytes) -> str:
    return "av1vpathkey4_" + _hmac_hex(
        key,
        {
            "domain": AV1_VALIDATION_V4_PATH_PRIVACY_KEY_ID_DOMAIN,
            "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
            "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        },
    )[:32]


def av1_validation_v4_instance_path_hmac_id(
    key: bytes,
    *,
    role: str,
    normalized_path: str,
) -> str:
    if role not in AV1_VALIDATION_V4_INSTANCE_PATH_ROLES:
        raise AV1ValidationV4PathPrivacyError(
            "AV1 v4 instance path role is invalid"
        )
    path = _canonical_absolute_posix_path(normalized_path)
    return "av1vpath4_" + _hmac_hex(
        key,
        {
            "domain": AV1_VALIDATION_V4_INSTANCE_PATH_HMAC_DOMAIN,
            "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
            "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
            "role": role,
            "normalized_path": path,
        },
    )[:32]


def av1_validation_v4_source_path_hmac_id(
    key: bytes,
    *,
    asset_id: str,
    normalized_path: str,
) -> str:
    if asset_id not in AV1_VALIDATION_V4_SOURCE_IDS:
        raise AV1ValidationV4PathPrivacyError(
            "AV1 v4 source path asset ID is invalid"
        )
    path = _canonical_absolute_posix_path(normalized_path)
    return "av1vsource4_" + _hmac_hex(
        key,
        {
            "domain": AV1_VALIDATION_V4_SOURCE_PATH_HMAC_DOMAIN,
            "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
            "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
            "asset_id": asset_id,
            "normalized_path": path,
        },
    )[:32]


def _hmac_hex(key: bytes, payload: dict[str, Any]) -> str:
    _validate_key(key)
    return hmac.new(
        key,
        canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < AV1_VALIDATION_V4_PATH_PRIVACY_KEY_BYTES_MIN:
        raise AV1ValidationV4PathPrivacyError(
            "AV1 v4 path privacy key must contain at least 32 bytes"
        )


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
        raise AV1ValidationV4PathPrivacyError(
            "AV1 v4 path must be a canonical absolute POSIX path"
        )
    path = PurePosixPath(value)
    if ".." in path.parts or path.as_posix() != value:
        raise AV1ValidationV4PathPrivacyError(
            "AV1 v4 path must be a canonical absolute POSIX path"
        )
    return value
