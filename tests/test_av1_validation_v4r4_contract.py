from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR
import json

import pytest

from mediaforce.core.evidence import stable_json_hash
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_FULL_VIDEO_POLICY,
    AV1_V4_R3_SOURCE_MANIFEST_FACTS,
    av1_v4_r3_derive_target_bytes,
    build_av1_v4_r3_all_closure_payloads,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    build_av1_v4r3_manifest_payload,
)
from mediaforce.tuning.av1_validation_v4r4_contract import (
    AV1V4R4ContractError,
    AV1_V4R4_MANIFEST_ID,
    AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
    AV1_V4R4_ORDINAL_COUNT,
    AV1_V4R4_POLICY_VALUES,
    AV1_V4R4_POLICY_VALUES_SHA256,
    assert_av1_v4r4_contract_payload,
    assert_av1_v4r4_runtime_policy,
    av1_v4r4_identity_domain,
    av1_v4r4_ordinal_layout,
    build_av1_v4r4_contract_payload,
    deserialize_av1_v4r4_contract_payload,
    serialize_av1_v4r4_contract_payload,
)


def test_contract_builds_and_round_trips_canonically() -> None:
    payload = build_av1_v4r4_contract_payload()

    assert payload["manifest_id"] == AV1_V4R4_MANIFEST_ID
    assert payload["payload_sha256"] == AV1_V4R4_MANIFEST_PAYLOAD_SHA256
    assert (
        deserialize_av1_v4r4_contract_payload(
            serialize_av1_v4r4_contract_payload(payload)
        )
        == payload
    )


def test_policy_values_digest_matches_revision_three_values_only() -> None:
    assert dict(AV1_V4R4_POLICY_VALUES) == dict(AV1_V4_R3_FULL_VIDEO_POLICY)
    assert AV1_V4R4_POLICY_VALUES_SHA256 == (
        f"sha256:{stable_json_hash(dict(AV1_V4_R3_FULL_VIDEO_POLICY))}"
    )


def test_identity_envelope_and_warm_start_ids_do_not_reuse_revision_three() -> None:
    payload = build_av1_v4r4_contract_payload()
    revision_three = build_av1_v4_r3_all_closure_payloads()
    revision_three_closure_ids = {
        str(closure["closure_id"]) for closure in revision_three
    }
    revision_four_warm_ids = {
        str(value)
        for ordinal in payload["ordinal_layout"]
        if ordinal["warm_start"] is not None
        for value in ordinal["warm_start"].values()
        if isinstance(value, str) and value.startswith("av1v4r4")
    }

    assert payload["manifest_id"].startswith("av1vmanifest4r4_")
    assert all("4r3" not in value for value in revision_four_warm_ids)
    assert revision_four_warm_ids.isdisjoint(revision_three_closure_ids)
    assert "revision:4" in av1_v4r4_identity_domain("ordinal-outcome")


def test_ordinal_layout_matches_frozen_revision_three_execution_order() -> None:
    revision_three = build_av1_v4_r3_all_closure_payloads()
    expected = [
        (closure["ordinal"], closure["asset_id"], closure["configuration"])
        for closure in revision_three
    ]
    actual = [
        (ordinal["ordinal"], ordinal["asset_id"], ordinal["configuration"])
        for ordinal in av1_v4r4_ordinal_layout()
    ]

    assert len(actual) == AV1_V4R4_ORDINAL_COUNT == 8
    assert actual == expected


def test_source_target_and_cap_bytes_match_frozen_revision_three_facts() -> None:
    payload = build_av1_v4r4_contract_payload()

    for source in payload["source_layout"]:
        asset_id = source["asset_id"]
        revision_three = AV1_V4_R3_SOURCE_MANIFEST_FACTS[asset_id]
        expected_cap = int(
            (
                Decimal(int(revision_three["media_bytes"])) * Decimal(80) / Decimal(100)
            ).quantize(Decimal("1"), rounding=ROUND_FLOOR)
        )
        assert source["duration_seconds"] == revision_three["duration_seconds"]
        assert source["media_bytes"] == revision_three["media_bytes"]
        assert source["target_size_bytes"] == av1_v4_r3_derive_target_bytes(asset_id)
        assert source["source_cap_total_bytes"] == expected_cap


def test_contract_cannot_confer_authority_or_expose_private_text() -> None:
    payload = build_av1_v4r4_contract_payload()
    assert all(
        payload[field] is False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
    )

    authorized = deepcopy(payload)
    authorized["dogfood_authorized"] = True
    with pytest.raises(AV1V4R4ContractError, match="cannot confer authority"):
        assert_av1_v4r4_contract_payload(authorized)

    private = deepcopy(payload)
    private["governance_issue"] = "/Users/operator/private"
    with pytest.raises(AV1V4R4ContractError, match="machine-local"):
        assert_av1_v4r4_contract_payload(private)


def test_contract_rejects_revision_three_identity_and_noncanonical_bytes() -> None:
    payload = build_av1_v4r4_contract_payload()
    payload["manifest_id"] = "av1vmanifest4r3_" + ("0" * 32)
    with pytest.raises(AV1V4R4ContractError, match="manifest ID"):
        assert_av1_v4r4_contract_payload(payload)

    canonical = serialize_av1_v4r4_contract_payload(build_av1_v4r4_contract_payload())
    noncanonical = json.dumps(json.loads(canonical), indent=2).encode() + b"\n"
    with pytest.raises(AV1V4R4ContractError, match="not canonical"):
        deserialize_av1_v4r4_contract_payload(noncanonical)

    with pytest.raises(AV1V4R4ContractError, match="shape"):
        assert_av1_v4r4_contract_payload(build_av1_v4r3_manifest_payload())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("metric_name", "xpsnr"),
        ("metric_target", 84.5),
        ("minimum_metric_score", 79.5),
        ("relax_step", 1.0),
        ("sample_projection_tolerance_percent", 11),
        ("final_output_tolerance_percent", 6),
        ("source_cap_percent", 81),
    ],
)
def test_runtime_policy_rejects_every_frozen_value_mismatch(
    field: str,
    value: object,
) -> None:
    bindings: dict[str, object] = {
        "metric_name": "vmaf",
        "metric_target": 85.0,
        "minimum_metric_score": 80.0,
        "relax_step": 0.5,
        "sample_projection_tolerance_percent": 10,
        "final_output_tolerance_percent": 5,
        "source_cap_percent": 80,
    }
    bindings[field] = value

    with pytest.raises(AV1V4R4ContractError, match="does not match"):
        assert_av1_v4r4_runtime_policy(**bindings)
