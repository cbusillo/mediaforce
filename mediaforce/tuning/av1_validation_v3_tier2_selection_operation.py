from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3 import (
    AV1ValidationProtocolV3,
    av1_validation_v3_qualification_key_id,
)
from mediaforce.tuning.av1_validation_v3_tier1_publication import (
    AV1ValidationV3Tier1PublicationError,
    load_av1_validation_v3_qualification_key,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory import (
    AV1ValidationV3Tier2Inventory,
    assert_av1_validation_v3_tier2_inventory_read_authorized,
    load_av1_validation_v3_tier2_inventory,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_authorization import (
    AV1ValidationV3Tier2InventoryReadContext,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_operation import (
    AV1ValidationV3Tier2InventoryReadOperationResult,
    run_av1_validation_v3_tier2_inventory_read,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_publication import (
    AV1ValidationV3Tier2InventoryReadClaimPublicationResult,
)
from mediaforce.tuning.av1_validation_v3_tier2_selection import (
    build_av1_validation_v3_tier2_selection_record,
)
from mediaforce.tuning.av1_validation_v3_tier2_selection_authorization import (
    AV1_VALIDATION_V3_TIER2_SELECTION_FALSE_AUTHORITY_FIELDS,
    AV1ValidationV3Tier2SelectionContext,
    assert_av1_validation_v3_tier2_selection_context,
)
from mediaforce.tuning.av1_validation_v3_tier2_selection_publication import (
    AV1ValidationV3Tier2SelectionClaimPublicationResult,
    AV1ValidationV3Tier2SelectionRecordPublicationResult,
    publish_av1_validation_v3_tier2_selection_claim,
    publish_av1_validation_v3_tier2_selection_record,
)


class AV1ValidationV3Tier2SelectionOperationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2SelectionOperationResult:
    outer_claim_publication: AV1ValidationV3Tier2SelectionClaimPublicationResult
    inner_claim_publication: AV1ValidationV3Tier2InventoryReadClaimPublicationResult
    selection_record_publication: AV1ValidationV3Tier2SelectionRecordPublicationResult

    def __post_init__(self) -> None:
        assert_av1_cold_start_public_payload_safe(self.to_public_summary())

    def to_public_summary(self) -> dict[str, Any]:
        summary = {
            "artifact_kind": "tier2_selection_summary",
            "gate": "A0",
            "tier": "tier2",
            "outer_selection_claim_created": self.outer_claim_publication.created,
            "inner_inventory_claim_created": self.inner_claim_publication.created,
            "selection_record_published": True,
            "selection_record_created": self.selection_record_publication.created,
            **dict.fromkeys(AV1_VALIDATION_V3_TIER2_SELECTION_FALSE_AUTHORITY_FIELDS, False),
            "tier2_selection_execution_authorized": False,
            "qualification_key_read_authorized": False,
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


def run_av1_validation_v3_tier2_selection(
    connection: DBClient,
    *,
    config: MediaforceConfig,
    protocol: AV1ValidationProtocolV3,
    selection_context: AV1ValidationV3Tier2SelectionContext,
    inventory_context: AV1ValidationV3Tier2InventoryReadContext,
    config_snapshot_bytes: bytes,
    output_root: Path,
    repository_root: Path,
    key_root: Path,
    clock: Callable[[], str],
    inventory_adapter: Callable[
        ..., AV1ValidationV3Tier2Inventory
    ] = load_av1_validation_v3_tier2_inventory,
) -> AV1ValidationV3Tier2SelectionOperationResult:
    selected_at = clock()

    assert_av1_validation_v3_tier2_selection_context(
        protocol, selection_context, inventory_context, as_of=selected_at
    )

    assert_av1_validation_v3_tier2_inventory_read_authorized(
        config=config,
        protocol=protocol,
        read_context=inventory_context,
        config_snapshot_bytes=config_snapshot_bytes,
        as_of=selected_at,
    )

    outer_claim_pub = publish_av1_validation_v3_tier2_selection_claim(
        claim=selection_context.claim,
        output_root=output_root,
        repository_root=repository_root,
    )
    if not outer_claim_pub.created:
        raise AV1ValidationV3Tier2SelectionOperationError(
            "AV1 v3 Tier 2 selection claim replay is not permitted"
        )

    inner_result = run_av1_validation_v3_tier2_inventory_read(
        connection,
        config=config,
        protocol=protocol,
        read_context=inventory_context,
        config_snapshot_bytes=config_snapshot_bytes,
        output_root=output_root,
        repository_root=repository_root,
        clock=lambda: selected_at,
        adapter=inventory_adapter,
    )

    try:
        qualification_key = load_av1_validation_v3_qualification_key(
            output_root=key_root,
            repository_root=repository_root,
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        raise AV1ValidationV3Tier2SelectionOperationError(
            "AV1 v3 Tier 2 qualification key is unavailable"
        ) from exc

    key_id = av1_validation_v3_qualification_key_id(qualification_key)
    if key_id != selection_context.plan.qualification_key_id:
        del qualification_key
        raise AV1ValidationV3Tier2SelectionOperationError(
            "AV1 v3 Tier 2 qualification key does not match plan commitment"
        )

    try:
        record = build_av1_validation_v3_tier2_selection_record(
            protocol=protocol,
            plan=selection_context.plan,
            sources=inner_result.inventory.candidate_sources,
            qualification_key=qualification_key,
            selected_at=selected_at,
        )
    finally:
        del qualification_key

    record_pub = publish_av1_validation_v3_tier2_selection_record(
        record=record,
        claim=selection_context.claim,
        output_root=output_root,
        repository_root=repository_root,
    )
    if not record_pub.created:
        raise AV1ValidationV3Tier2SelectionOperationError(
            "AV1 v3 Tier 2 selection record replay is not permitted"
        )

    return AV1ValidationV3Tier2SelectionOperationResult(
        outer_claim_publication=outer_claim_pub,
        inner_claim_publication=inner_result.claim_publication,
        selection_record_publication=record_pub,
    )
