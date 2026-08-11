from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.tuning import av1_validation_v4r4_execution_authority as authority
from mediaforce.tuning import av1_validation_v4r4_preparation_flow as preparation_flow
from mediaforce.tuning.av1_validation_v4 import AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
from mediaforce.tuning.av1_validation_v4_qualification_search import (
    av1_validation_v4_qualification_search_invocation_sha256,
)
from mediaforce.tuning.av1_validation_v4r4_contract import (
    AV1_V4R4_POLICY_VALUES_SHA256,
    av1_v4r4_identity_domain,
    av1_v4r4_ordinal_layout,
)
from mediaforce.tuning.av1_validation_v4r4_one_ordinal_runner import (
    _video_policy_for_ordinal,
    _warm_start_for_ordinal,
)
from mediaforce.tuning.av1_validation_v4r4_execution_authority import (
    AV1V4R4ExecutionAuthorityError,
    assert_av1_v4r4_execution_chain,
    assert_av1_v4r4_execution_claim,
    assert_av1_v4r4_execution_grant,
    av1_v4r4_execution_authority_payload_sha256,
    av1_v4r4_execution_claim_public_id,
    av1_v4r4_execution_grant_public_id,
    deserialize_av1_v4r4_execution_claim,
    deserialize_av1_v4r4_execution_grant,
)
from mediaforce.tuning.av1_validation_v4r4_preparation_flow import (
    prepare_av1_v4r4_preparation_custody_readiness,
)
from mediaforce.tuning.av1_validation_v4r4_preparation_registry import (
    AV1V4R4PreparationRegistryBinding,
)
from tests.test_av1_validation_v4r4_ordinal_registry import TickClock
from tests.test_av1_validation_v4r4_preparation_flow import (
    _patched_runtime,
    _request_payload,
    _stub_tools,
)


def test_execution_grant_and_claim_verify_owner_chain_and_round_trip(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    plan_binding, plan, clock = prepared.binding, prepared.plan, prepared.clock
    sequencing_grant = prepared.sequencing_grant
    sequencing_claim = _sequencing_claim(plan_binding, plan, sequencing_grant, clock)
    grant = _execution_grant(
        plan,
        sequencing_grant,
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
    )
    claim = _execution_claim(plan, sequencing_claim, grant)

    assert_av1_v4r4_execution_grant(grant)
    assert_av1_v4r4_execution_claim(claim)
    assert grant["execution_grant_id"] == av1_v4r4_execution_grant_public_id(grant)
    assert claim["execution_claim_id"] == av1_v4r4_execution_claim_public_id(claim)
    assert grant["payload_sha256"] == av1_v4r4_execution_authority_payload_sha256(grant)
    assert deserialize_av1_v4r4_execution_grant(_private_canonical_bytes(grant)) == grant
    assert deserialize_av1_v4r4_execution_claim(_private_canonical_bytes(claim)) == claim
    assert_av1_v4r4_execution_chain(
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
        plan=plan,
        sequencing_grant=sequencing_grant,
        sequencing_claim=sequencing_claim,
        execution_grant=grant,
        execution_claim=claim,
        now=clock.current,
    )


def test_execution_chain_rejects_inactive_preparation(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    sequencing_claim = _sequencing_claim(
        prepared.binding,
        prepared.plan,
        prepared.sequencing_grant,
        prepared.clock,
    )
    execution_grant = _execution_grant(
        prepared.plan,
        prepared.sequencing_grant,
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
    )
    execution_claim = _execution_claim(
        prepared.plan,
        sequencing_claim,
        execution_grant,
    )
    expired = datetime.fromisoformat(
        str(prepared.plan["plan_closes_at"]).replace("Z", "+00:00")
    )

    with pytest.raises(
        AV1V4R4ExecutionAuthorityError,
        match="preparation chain is inactive",
    ):
        assert_av1_v4r4_execution_chain(
            qualification_request=prepared.request,
            execution_preflight=prepared.preflight,
            plan=prepared.plan,
            sequencing_grant=prepared.sequencing_grant,
            sequencing_claim=sequencing_claim,
            execution_grant=execution_grant,
            execution_claim=execution_claim,
            now=expired,
        )


def test_execution_grant_authorizes_exactly_three_fields(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    plan, sequencing_grant = prepared.plan, prepared.sequencing_grant
    grant = _execution_grant(
        plan,
        sequencing_grant,
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
    )

    true_fields = {
        field for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS if grant[field] is True
    }
    assert true_fields == {
        "media_read_authorized",
        "qualification_execution_authorized",
        "runtime_execution_authorized",
    }
    mutated = _rebind_execution_grant({**grant, "retry_authorized": True})
    with pytest.raises(AV1V4R4ExecutionAuthorityError, match="authority fields"):
        assert_av1_v4r4_execution_grant(mutated)


def test_execution_authority_rejects_forged_policy_owner_window_and_binding(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    plan_binding, plan, clock = prepared.binding, prepared.plan, prepared.clock
    sequencing_grant = prepared.sequencing_grant
    sequencing_claim = _sequencing_claim(plan_binding, plan, sequencing_grant, clock)
    grant = _execution_grant(
        plan,
        sequencing_grant,
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
    )
    claim = _execution_claim(plan, sequencing_claim, grant)

    bad_policy = _rebind_execution_grant({**grant, "policy_values_sha256": "sha256:" + "0" * 64})
    with pytest.raises(AV1V4R4ExecutionAuthorityError, match="ordinal binding"):
        assert_av1_v4r4_execution_grant(bad_policy)

    bad_owner = _rebind_execution_grant({**grant, "owner_principal": "/Users/local"})
    with pytest.raises(AV1V4R4ExecutionAuthorityError, match="owner principal"):
        assert_av1_v4r4_execution_grant(bad_owner)

    bad_window = _rebind_execution_grant({**grant, "valid_until": grant["authorized_at"]})
    with pytest.raises(AV1V4R4ExecutionAuthorityError, match="window"):
        assert_av1_v4r4_execution_grant(bad_window)

    bad_claim = _rebind_execution_claim({**claim, "execution_grant_id": "av1v4r4execgrant_" + "0" * 32})
    with pytest.raises(AV1V4R4ExecutionAuthorityError, match="chain binding"):
        assert_av1_v4r4_execution_chain(
            qualification_request=prepared.request,
            execution_preflight=prepared.preflight,
            plan=plan,
            sequencing_grant=sequencing_grant,
            sequencing_claim=sequencing_claim,
            execution_grant=grant,
            execution_claim=bad_claim,
            now=clock.current,
        )


def test_execution_chain_rejects_future_claim_and_window_escape(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    plan_binding, plan, clock = prepared.binding, prepared.plan, prepared.clock
    sequencing_grant = prepared.sequencing_grant
    sequencing_claim = _sequencing_claim(plan_binding, plan, sequencing_grant, clock)
    grant = _execution_grant(
        plan,
        sequencing_grant,
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
    )
    claim = _execution_claim(plan, sequencing_claim, grant)

    future_claim = _rebind_execution_claim({**claim, "claimed_at": "2026-08-10T04:10:00Z"})
    with pytest.raises(AV1V4R4ExecutionAuthorityError, match="future"):
        assert_av1_v4r4_execution_chain(
            qualification_request=prepared.request,
            execution_preflight=prepared.preflight,
            plan=plan,
            sequencing_grant=sequencing_grant,
            sequencing_claim=sequencing_claim,
            execution_grant=grant,
            execution_claim=future_claim,
            now=datetime(2026, 8, 10, 4, 0, 30, tzinfo=UTC),
        )

    wide_grant = _rebind_execution_grant({**grant, "valid_until": "2026-08-11T00:00:01Z"})
    wide_claim = _rebind_execution_claim({**claim, "execution_grant_id": wide_grant["execution_grant_id"], "execution_grant_payload_sha256": wide_grant["payload_sha256"]})
    with pytest.raises(AV1V4R4ExecutionAuthorityError, match="inactive"):
        assert_av1_v4r4_execution_chain(
            qualification_request=prepared.request,
            execution_preflight=prepared.preflight,
            plan=plan,
            sequencing_grant=sequencing_grant,
            sequencing_claim=sequencing_claim,
            execution_grant=wide_grant,
            execution_claim=wide_claim,
            now=datetime(2026, 8, 10, 4, 0, 30, tzinfo=UTC),
        )


def test_execution_chain_requires_exact_owner_between_grant_and_claim(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    plan_binding, plan, clock = prepared.binding, prepared.plan, prepared.clock
    sequencing_grant = prepared.sequencing_grant
    sequencing_claim = _sequencing_claim(plan_binding, plan, sequencing_grant, clock)
    grant = _execution_grant(
        plan,
        sequencing_grant,
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
        owner="owner.mediaforce",
    )
    claim = _execution_claim(plan, sequencing_claim, grant)
    rebound_claim = _rebind_execution_claim({**claim, "owner_principal": "other.mediaforce"})

    with pytest.raises(AV1V4R4ExecutionAuthorityError, match="chain binding"):
        assert_av1_v4r4_execution_chain(
            qualification_request=prepared.request,
            execution_preflight=prepared.preflight,
            plan=plan,
            sequencing_grant=sequencing_grant,
            sequencing_claim=sequencing_claim,
            execution_grant=grant,
            execution_claim=rebound_claim,
            now=clock.current,
        )


def test_execution_chain_rejects_request_or_preflight_substitution(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path / "primary")
    substitute = _prepared_chain(tmp_path / "substitute")
    sequencing_claim = _sequencing_claim(
        prepared.binding,
        prepared.plan,
        prepared.sequencing_grant,
        prepared.clock,
    )
    grant = _execution_grant(
        prepared.plan,
        prepared.sequencing_grant,
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
    )
    claim = _execution_claim(prepared.plan, sequencing_claim, grant)
    with pytest.raises(AV1V4R4ExecutionAuthorityError, match="preparation"):
        assert_av1_v4r4_execution_chain(
            qualification_request=substitute.request,
            execution_preflight=prepared.preflight,
            plan=prepared.plan,
            sequencing_grant=prepared.sequencing_grant,
            sequencing_claim=sequencing_claim,
            execution_grant=grant,
            execution_claim=claim,
            now=prepared.clock.current,
        )
    with pytest.raises(AV1V4R4ExecutionAuthorityError, match="preparation"):
        assert_av1_v4r4_execution_chain(
            qualification_request=prepared.request,
            execution_preflight=substitute.preflight,
            plan=prepared.plan,
            sequencing_grant=prepared.sequencing_grant,
            sequencing_claim=sequencing_claim,
            execution_grant=grant,
            execution_claim=claim,
            now=prepared.clock.current,
        )


def test_execution_authority_exposes_no_mint_or_write_api() -> None:
    names = set(dir(authority))
    forbidden_prefixes = ("build_", "serialize_", "publish_", "materialize_")
    forbidden = {
        name
        for name in names
        if name.endswith(("execution_grant", "execution_claim"))
        and name.startswith(forbidden_prefixes)
    }
    assert forbidden == set()
    exported_functions = {
        name for name, value in inspect.getmembers(authority, inspect.isfunction)
    }
    assert forbidden.isdisjoint(exported_functions)


def _prepared_chain(tmp_path: Path) -> SimpleNamespace:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    preparation = tmp_path / "preparation"
    ordinal = tmp_path / "ordinal"
    request_payload = _request_payload(
        repo=repo,
        preparation=preparation,
        ordinal=ordinal,
        tools=_stub_tools(tmp_path),
    )
    with _patched_runtime(repo):
        result = prepare_av1_v4r4_preparation_custody_readiness(**request_payload)
    binding = preparation_flow._ordinal_binding(
        AV1V4R4PreparationRegistryBinding(
            registry=preparation,
            repository_root=repo,
        ),
        ordinal,
    )
    return SimpleNamespace(
        binding=binding,
        request=result.request,
        preflight=result.preflight,
        plan=result.plan,
        sequencing_grant=result.ordinal_grant,
        private_inputs=request_payload,
        clock=TickClock(datetime(2026, 8, 10, 4, 0, 1, tzinfo=UTC)),
    )


def _sequencing_grant(binding: Any, plan: Mapping[str, Any], clock: Any, *, ordinal: int = 1) -> dict[str, Any]:
    from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
        publish_av1_v4r4_ordinal_registry_grant,
    )

    return publish_av1_v4r4_ordinal_registry_grant(
        binding=binding,
        plan=plan,
        ordinal=ordinal,
        clock=clock,
        valid_until="2026-08-11T00:00:00Z",
    )


def _sequencing_claim(binding: Any, plan: Mapping[str, Any], grant: Mapping[str, Any], clock: Any) -> dict[str, Any]:
    from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
        load_av1_v4r4_ordinal_registry_preparation,
        publish_av1_v4r4_ordinal_registry_claim,
    )

    request, preflight = load_av1_v4r4_ordinal_registry_preparation(
        binding=binding,
        plan=plan,
    )
    execution_grant = _execution_grant(
        plan,
        grant,
        qualification_request=request,
        execution_preflight=preflight,
    )
    grant_file = binding.registry / f"v4r4-ordinal-{grant['ordinal']:02d}-execution-grant.json"
    if not grant_file.exists():
        grant_file.write_bytes(_private_canonical_bytes(execution_grant))
        grant_file.chmod(0o600)
    return publish_av1_v4r4_ordinal_registry_claim(
        binding=binding,
        plan=plan,
        grant=grant,
        clock=clock,
    )


def _execution_grant(
    plan: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    *,
    qualification_request: Mapping[str, Any] | None = None,
    execution_preflight: Mapping[str, Any] | None = None,
    owner: str = "owner.mediaforce",
    prepared_invocation_sha256: str | None = None,
) -> dict[str, Any]:
    layout = av1_v4r4_ordinal_layout()[sequencing_grant["ordinal"] - 1]
    ordinal = int(sequencing_grant["ordinal"])
    request = qualification_request or {
        "request_id": "av1v4r4req_" + "1" * 32,
        "payload_sha256": "sha256:" + "2" * 64,
    }
    preflight = execution_preflight or {
        "preflight_id": "av1v4r4preflight_" + "3" * 32,
        "payload_sha256": "sha256:" + "4" * 64,
    }
    payload = {
        "schema": authority.AV1_V4R4_EXECUTION_GRANT_SCHEMA,
        "schema_version": authority.AV1_V4R4_EXECUTION_GRANT_SCHEMA_VERSION,
        "contract_version": authority.AV1_V4R4_EXECUTION_GRANT_CONTRACT_VERSION,
        "protocol_version": 4,
        "manifest_revision": 4,
        "experiment_id": "av1_cold_start_v4",
        "manifest_id": "av1vmanifest4r4_7d3d62b272d048e7ac4aaa397eace2a0",
        "manifest_payload_sha256": "sha256:bb7c1d865a618a1b0ba4ccd5b63895d8c3ecc0c3384e0fe359cf0626cb959b67",
        "owner_principal": owner,
        "qualification_request_id": request["request_id"],
        "qualification_request_payload_sha256": request["payload_sha256"],
        "execution_preflight_id": preflight["preflight_id"],
        "execution_preflight_payload_sha256": preflight["payload_sha256"],
        "plan_id": plan["plan_id"],
        "plan_payload_sha256": plan["payload_sha256"],
        "sequencing_grant_id": sequencing_grant["grant_id"],
        "sequencing_grant_payload_sha256": sequencing_grant["payload_sha256"],
        "prepared_invocation_sha256": prepared_invocation_sha256
        or (
            next(
                item["invocation_sha256"]
                for item in qualification_request["invocation_digests"]
                if item["ordinal"] == ordinal
            )
            if qualification_request is not None
            else _prepared_invocation_sha256(ordinal)
        ),
        "ordinal": ordinal,
        "asset_id": layout["asset_id"],
        "content_class": layout["content_class"],
        "role": layout["role"],
        "configuration": layout["configuration"],
        "target_size_bytes": layout["target_size_bytes"],
        "source_cap_total_bytes": layout["source_cap_total_bytes"],
        "policy_values_sha256": AV1_V4R4_POLICY_VALUES_SHA256,
        "authorized_at": sequencing_grant["authorized_at"],
        "valid_until": sequencing_grant["valid_until"],
        **{
            field: field
            in {
                "media_read_authorized",
                "qualification_execution_authorized",
                "runtime_execution_authorized",
            }
            for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
        },
    }
    return _rebind_execution_grant(payload)


def _prepared_invocation_sha256(ordinal: int) -> str:
    warm_start = _warm_start_for_ordinal(ordinal)
    return av1_validation_v4_qualification_search_invocation_sha256(
        source_path=Path("/tmp/mediaforce-v4r4-source.mkv"),
        video_policy=_video_policy_for_ordinal(ordinal),
        mode="guided" if warm_start is not None else "baseline",
        warm_start=warm_start,
        extra_search_kwargs={
            "source_codec": "h264",
            "width": 1920,
            "height": 1080,
            "quality_temp_dir": Path("/tmp/mediaforce-v4r4-quality"),
        },
    )


def _execution_claim(
    plan: Mapping[str, Any],
    sequencing_claim: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema": authority.AV1_V4R4_EXECUTION_CLAIM_SCHEMA,
        "schema_version": authority.AV1_V4R4_EXECUTION_CLAIM_SCHEMA_VERSION,
        "contract_version": authority.AV1_V4R4_EXECUTION_CLAIM_CONTRACT_VERSION,
        "protocol_version": 4,
        "manifest_revision": 4,
        "experiment_id": "av1_cold_start_v4",
        "manifest_id": "av1vmanifest4r4_7d3d62b272d048e7ac4aaa397eace2a0",
        "manifest_payload_sha256": "sha256:bb7c1d865a618a1b0ba4ccd5b63895d8c3ecc0c3384e0fe359cf0626cb959b67",
        "owner_principal": execution_grant["owner_principal"],
        "plan_id": plan["plan_id"],
        "plan_payload_sha256": plan["payload_sha256"],
        "sequencing_claim_id": sequencing_claim["claim_id"],
        "sequencing_claim_payload_sha256": sequencing_claim["payload_sha256"],
        "execution_grant_id": execution_grant["execution_grant_id"],
        "execution_grant_payload_sha256": execution_grant["payload_sha256"],
        "ordinal": execution_grant["ordinal"],
        "claimed_at": sequencing_claim["claimed_at"],
        **{
            field: field
            in {
                "media_read_authorized",
                "qualification_execution_authorized",
                "runtime_execution_authorized",
            }
            for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
        },
    }
    return _rebind_execution_claim(payload)


def _rebind_execution_grant(payload: Mapping[str, Any]) -> dict[str, Any]:
    rebound = deepcopy(payload)
    rebound.pop("execution_grant_id", None)
    rebound.pop("payload_sha256", None)
    rebound["execution_grant_id"] = "av1v4r4execgrant_" + stable_json_hash(
        {"domain": av1_v4r4_identity_domain("execution-grant"), "payload": rebound}
    )[:32]
    rebound["payload_sha256"] = f"sha256:{stable_json_hash(rebound)}"
    return json.loads(canonical_json_bytes(rebound))


def _rebind_execution_claim(payload: Mapping[str, Any]) -> dict[str, Any]:
    rebound = deepcopy(payload)
    rebound.pop("execution_claim_id", None)
    rebound.pop("payload_sha256", None)
    rebound["execution_claim_id"] = "av1v4r4execclaim_" + stable_json_hash(
        {"domain": av1_v4r4_identity_domain("execution-claim"), "payload": rebound}
    )[:32]
    rebound["payload_sha256"] = f"sha256:{stable_json_hash(rebound)}"
    return json.loads(canonical_json_bytes(rebound))


def _private_canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(payload) + b"\n"
