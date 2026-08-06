from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3 import AV1ValidationProtocolV3
from mediaforce.tuning.av1_validation_v3_tier2_inventory import (
    AV1ValidationV3Tier2Inventory,
    assert_av1_validation_v3_tier2_inventory_read_authorized,
    load_av1_validation_v3_tier2_inventory,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_authorization import (
    AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS,
    AV1ValidationV3Tier2InventoryReadContext,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_publication import (
    AV1ValidationV3Tier2InventoryReadClaimPublicationResult,
    publish_av1_validation_v3_tier2_inventory_read_claim,
)


class AV1ValidationV3Tier2InventoryOperationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2InventoryReadOperationResult:
    claim_publication: AV1ValidationV3Tier2InventoryReadClaimPublicationResult
    inventory: AV1ValidationV3Tier2Inventory

    def __post_init__(self) -> None:
        assert_av1_cold_start_public_payload_safe(self.to_public_summary())

    def to_public_summary(self) -> dict[str, Any]:
        summary = {
            "artifact_kind": "tier2_inventory_read_summary",
            "gate": "A0",
            "tier": "tier2",
            "request_id": self.claim_publication.claim.request_id,
            "grant_id": self.claim_publication.claim.grant_id,
            "claim_id": self.claim_publication.claim.claim_id,
            "read_claim_published": True,
            "read_claim_created": self.claim_publication.created,
            "private_inventory_read_authorized": True,
            "single_read_claimed": True,
            **dict.fromkeys(
                AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS,
                False,
            ),
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


def run_av1_validation_v3_tier2_inventory_read(
    connection: DBClient,
    *,
    config: MediaforceConfig,
    protocol: AV1ValidationProtocolV3,
    read_context: AV1ValidationV3Tier2InventoryReadContext,
    config_snapshot_bytes: bytes,
    output_root: Path,
    repository_root: Path,
    clock: Callable[[], str],
    adapter: Callable[
        ..., AV1ValidationV3Tier2Inventory
    ] = load_av1_validation_v3_tier2_inventory,
) -> AV1ValidationV3Tier2InventoryReadOperationResult:
    read_at = clock()
    assert_av1_validation_v3_tier2_inventory_read_authorized(
        config=config,
        protocol=protocol,
        read_context=read_context,
        config_snapshot_bytes=config_snapshot_bytes,
        as_of=read_at,
    )
    claim_publication = publish_av1_validation_v3_tier2_inventory_read_claim(
        claim=read_context.claim,
        output_root=output_root,
        repository_root=repository_root,
    )
    if not claim_publication.created:
        raise AV1ValidationV3Tier2InventoryOperationError(
            "AV1 v3 Tier 2 inventory read claim replay is not permitted"
        )
    inventory = adapter(
        connection,
        config=config,
        protocol=protocol,
        read_context=read_context,
        config_snapshot_bytes=config_snapshot_bytes,
        clock=lambda: read_at,
    )
    if not isinstance(inventory, AV1ValidationV3Tier2Inventory):
        raise AV1ValidationV3Tier2InventoryOperationError(
            "AV1 v3 Tier 2 inventory adapter returned an invalid result"
        )
    return AV1ValidationV3Tier2InventoryReadOperationResult(
        claim_publication=claim_publication,
        inventory=inventory,
    )
