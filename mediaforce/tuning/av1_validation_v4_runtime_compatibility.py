from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_RUNTIME_COMPATIBILITY_SCOPE,
    av1_validation_v4_contains_private_text,
)


AV1_VALIDATION_V4_RUNTIME_COMPATIBILITY_DOMAIN = (
    "mediaforce:av1:protocol:4:revision:2:runtime-compatibility:v1"
)
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TOOL_NAMES = frozenset({"ab_av1", "ffmpeg", "ffprobe"})


class AV1ValidationV4RuntimeCompatibilityError(ValueError):
    pass


def av1_validation_v4_runtime_compatibility_payload(
    *,
    effective_config_sha256: str,
    toolchain: Mapping[str, Mapping[str, str]],
    operating_system: str,
    operating_system_version: str,
    architecture: str,
    python_version: str,
) -> dict[str, Any]:
    if not _SHA256_RE.fullmatch(effective_config_sha256):
        raise AV1ValidationV4RuntimeCompatibilityError(
            "AV1 v4 runtime effective config digest is invalid"
        )
    normalized_toolchain = {
        str(name): object_dict(identity)
        for name, identity in toolchain.items()
    }
    if set(normalized_toolchain) != _TOOL_NAMES:
        raise AV1ValidationV4RuntimeCompatibilityError(
            "AV1 v4 runtime toolchain set is invalid"
        )
    for name, identity in normalized_toolchain.items():
        if set(identity) != {"binary_sha256", "version"}:
            raise AV1ValidationV4RuntimeCompatibilityError(
                f"AV1 v4 runtime {name} identity is invalid"
            )
        if not _SHA256_RE.fullmatch(str(identity.get("binary_sha256") or "")):
            raise AV1ValidationV4RuntimeCompatibilityError(
                f"AV1 v4 runtime {name} binary digest is invalid"
            )
        _validate_public_string(identity.get("version"), f"{name} version")
    platform_payload = {
        "operating_system": operating_system,
        "operating_system_version": operating_system_version,
        "architecture": architecture,
        "python_version": python_version,
    }
    for label, value in platform_payload.items():
        _validate_public_string(value, label)
    return {
        "domain": AV1_VALIDATION_V4_RUNTIME_COMPATIBILITY_DOMAIN,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "scope": AV1_VALIDATION_V4_RUNTIME_COMPATIBILITY_SCOPE,
        "effective_config_sha256": effective_config_sha256,
        "toolchain": normalized_toolchain,
        "platform": platform_payload,
    }


def av1_validation_v4_runtime_compatibility_id(
    *,
    effective_config_sha256: str,
    toolchain: Mapping[str, Mapping[str, str]],
    operating_system: str,
    operating_system_version: str,
    architecture: str,
    python_version: str,
) -> str:
    payload = av1_validation_v4_runtime_compatibility_payload(
        effective_config_sha256=effective_config_sha256,
        toolchain=toolchain,
        operating_system=operating_system,
        operating_system_version=operating_system_version,
        architecture=architecture,
        python_version=python_version,
    )
    return f"av1vruntime4_{stable_json_hash(payload)[:32]}"


def av1_validation_v4_runtime_compatibility_id_from_payload(
    payload: Mapping[str, Any],
) -> str:
    materialized = object_dict(payload)
    if set(materialized) != {
        "domain",
        "effective_config_sha256",
        "manifest_revision",
        "platform",
        "protocol_version",
        "scope",
        "toolchain",
    }:
        raise AV1ValidationV4RuntimeCompatibilityError(
            "AV1 v4 runtime compatibility payload shape is invalid"
        )
    platform_payload = object_dict(materialized.get("platform"))
    expected = av1_validation_v4_runtime_compatibility_payload(
        effective_config_sha256=str(materialized.get("effective_config_sha256") or ""),
        toolchain={
            str(name): object_dict(identity)
            for name, identity in object_dict(materialized.get("toolchain")).items()
        },
        operating_system=str(platform_payload.get("operating_system") or ""),
        operating_system_version=str(
            platform_payload.get("operating_system_version") or ""
        ),
        architecture=str(platform_payload.get("architecture") or ""),
        python_version=str(platform_payload.get("python_version") or ""),
    )
    if materialized != expected:
        raise AV1ValidationV4RuntimeCompatibilityError(
            "AV1 v4 runtime compatibility payload binding is invalid"
        )
    return f"av1vruntime4_{stable_json_hash(materialized)[:32]}"


def _validate_public_string(value: Any, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 512
        or av1_validation_v4_contains_private_text(value)
    ):
        raise AV1ValidationV4RuntimeCompatibilityError(
            f"AV1 v4 runtime {label} is invalid"
        )
