from __future__ import annotations

import ast
from pathlib import Path
import unittest

from mediaforce.core.evidence import stable_json_hash
from mediaforce.tuning import av1_validation_v4r3_execution_grant as module
from mediaforce.tuning.av1_validation_v4 import AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
from mediaforce.tuning.av1_validation_v4r3_execution_grant import (
    AV1V4R3ExecutionGrantError,
    AV1_V4R3_EXECUTION_GRANT_AUTHORIZED_FIELDS,
    assert_av1_v4r3_execution_grant,
    assert_av1_v4r3_execution_grant_active,
    assert_av1_v4r3_execution_grant_chain,
    assert_av1_v4r3_execution_grant_for_ordinal,
    build_av1_v4r3_execution_grant,
    deserialize_av1_v4r3_execution_grant,
    serialize_av1_v4r3_execution_grant,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    build_av1_v4r3_ordinal_window_grant,
    finalize_av1_v4r3_ordinal_window_plan,
)
from tests.test_av1_validation_v4r3_execution_preflight import (
    _plan_draft,
    _preflight,
)


class AV1V4R3ExecutionGrantTests(unittest.TestCase):
    def test_round_trip_authorizes_only_one_ordinal_execution_envelope(self) -> None:
        _plan, _preflight_payload, _sequencing, grant = _execution_chain()
        self.assertEqual(
            deserialize_av1_v4r3_execution_grant(
                serialize_av1_v4r3_execution_grant(grant)
            ),
            grant,
        )
        self.assertEqual(grant["ordinal"], 1)
        self.assertEqual(grant["grant_scope"]["ordinal_scope"], "exactly_one")
        for field in AV1_V4R3_EXECUTION_GRANT_AUTHORIZED_FIELDS:
            self.assertIs(grant[field], True)
        for field in (
            AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
            - AV1_V4R3_EXECUTION_GRANT_AUTHORIZED_FIELDS
        ):
            self.assertIs(grant[field], False)
        assert_av1_v4r3_execution_grant_active(grant, as_of="2026-08-08T04:07:00Z")
        assert_av1_v4r3_execution_grant_for_ordinal(grant, ordinal=1)
        assert_av1_v4r3_execution_grant_chain(
            plan=_plan,
            preflight=_preflight_payload,
            sequencing_grant=_sequencing,
            execution_grant=grant,
        )
        with self.assertRaises(AV1V4R3ExecutionGrantError):
            assert_av1_v4r3_execution_grant_for_ordinal(grant, ordinal=2)

    def test_rejects_authority_shape_identity_and_window_tampering(self) -> None:
        _plan, _preflight_payload, _sequencing, grant = _execution_chain()
        for field, value in (
            ("ordinal", 1.0),
            ("schema_version", True),
            ("protocol_version", 4.0),
            ("manifest_revision", 3.0),
            (
                "grant_scope",
                {**grant["grant_scope"], "single_use_authorized": 1},
            ),
            ("dogfood_authorized", True),
            ("media_read_authorized", False),
            ("owner_principal", "/Users/private"),
        ):
            with self.subTest(field=field):
                mutated = dict(grant)
                mutated[field] = value
                with self.assertRaises(AV1V4R3ExecutionGrantError):
                    assert_av1_v4r3_execution_grant(_rebind(mutated))
        with self.assertRaises(AV1V4R3ExecutionGrantError):
            assert_av1_v4r3_execution_grant_active(grant, as_of=grant["valid_until"])

        unknown = dict(grant)
        unknown["unexpected"] = False
        missing = dict(grant)
        missing.pop("runtime_id")
        for label, malformed in (("unknown", unknown), ("missing", missing)):
            with (
                self.subTest(label=label),
                self.assertRaises(AV1V4R3ExecutionGrantError),
            ):
                assert_av1_v4r3_execution_grant(malformed)

        plan, preflight_payload, sequencing, _grant = _execution_chain()
        sibling = dict(sequencing)
        sibling["plan_id"] = "av1vordplan4r3_" + "f" * 32
        sibling["plan_payload_sha256"] = "sha256:" + "e" * 64
        with self.assertRaises(AV1V4R3ExecutionGrantError):
            build_av1_v4r3_execution_grant(
                plan=plan,
                preflight=preflight_payload,
                sequencing_grant=_rebind_sequencing(sibling),
                owner_principal="owner:test",
                authorized_at="2026-08-08T04:06:00Z",
                valid_until="2026-08-08T07:40:00Z",
            )

    def test_chain_rejects_rebound_predecessor_substitution(self) -> None:
        plan, preflight, sequencing, grant = _execution_chain()
        for field, value in (
            ("invocation_sha256", "sha256:" + "f" * 64),
            (
                "reviewed_repository",
                {"commit": "e" * 40, "tree": "d" * 40},
            ),
            ("preflight_payload_sha256", "sha256:" + "c" * 64),
        ):
            with self.subTest(field=field):
                mutated = dict(grant)
                mutated[field] = value
                rebound = _rebind(mutated)
                assert_av1_v4r3_execution_grant(rebound)
                with self.assertRaises(AV1V4R3ExecutionGrantError):
                    assert_av1_v4r3_execution_grant_chain(
                        plan=plan,
                        preflight=preflight,
                        sequencing_grant=sequencing,
                        execution_grant=rebound,
                    )

    def test_rejects_noncanonical_bytes_and_has_no_io_imports(self) -> None:
        grant = _execution_chain()[3]
        with self.assertRaises(AV1V4R3ExecutionGrantError):
            deserialize_av1_v4r3_execution_grant(
                serialize_av1_v4r3_execution_grant(grant).rstrip(b"\n")
            )
        with self.assertRaises(AV1V4R3ExecutionGrantError):
            deserialize_av1_v4r3_execution_grant(b'{"value":NaN}\n')
        tree = ast.parse(Path(module.__file__).read_text())
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertTrue(
            {
                "os",
                "pathlib",
                "subprocess",
                "requests",
                "mediaforce.core.db",
            }.isdisjoint(imports)
        )


def _execution_chain() -> tuple[
    dict[str, object], dict[str, object], dict[str, object], dict[str, object]
]:
    draft = _plan_draft()
    preflight = _preflight(draft=draft)
    plan = finalize_av1_v4r3_ordinal_window_plan(
        draft=draft,
        r3_preflight_id=str(preflight["preflight_id"]),
    )
    sequencing = build_av1_v4r3_ordinal_window_grant(
        plan=plan,
        ordinal=1,
        authorized_at="2026-08-08T04:05:00Z",
        valid_until="2026-08-08T07:50:00Z",
    )
    grant = build_av1_v4r3_execution_grant(
        plan=plan,
        preflight=preflight,
        sequencing_grant=sequencing,
        owner_principal="owner:test",
        authorized_at="2026-08-08T04:06:00Z",
        valid_until="2026-08-08T07:40:00Z",
    )
    return plan, preflight, sequencing, grant


def _rebind(payload: dict[str, object]) -> dict[str, object]:
    rebound = dict(payload)
    rebound["grant_id"] = module.av1_v4r3_execution_grant_id(rebound)
    without_sha = {
        key: value for key, value in rebound.items() if key != "payload_sha256"
    }
    rebound["payload_sha256"] = "sha256:" + stable_json_hash(without_sha)
    return rebound


def _rebind_sequencing(payload: dict[str, object]) -> dict[str, object]:
    from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
        av1_v4r3_ordinal_window_grant_id,
    )

    rebound = dict(payload)
    rebound["grant_id"] = av1_v4r3_ordinal_window_grant_id(rebound)
    without_sha = {
        key: value for key, value in rebound.items() if key != "payload_sha256"
    }
    rebound["payload_sha256"] = "sha256:" + stable_json_hash(without_sha)
    return rebound


if __name__ == "__main__":
    unittest.main()
