from __future__ import annotations

import ast
from pathlib import Path
import unittest

from mediaforce.core.evidence import stable_json_hash
from mediaforce.tuning import av1_validation_v4r3_execution_preflight as module
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
)
from mediaforce.tuning.av1_validation_v4r3_execution_preflight import (
    AV1V4R3ExecutionPreflightError,
    AV1_V4R3_EXECUTION_PREFLIGHT_APPROVAL_FIELD,
    AV1_V4R3_EXECUTION_PREFLIGHT_READINESS_STATE,
    AV1_V4R3_EXECUTION_PREFLIGHT_SCOPE,
    assert_av1_v4r3_ordinal_plan_preflight_pair,
    assert_av1_v4r3_execution_preflight,
    av1_v4r3_execution_preflight_id,
    build_av1_v4r3_execution_preflight,
    deserialize_av1_v4r3_execution_preflight,
    serialize_av1_v4r3_execution_preflight,
)
from mediaforce.tuning.av1_validation_v4r3_manifest_identity import (
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_VALID_UNTIL,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1_V4R3_OW_AGGREGATE_SECONDS_MAX,
    assert_av1_v4r3_ordinal_window_plan,
    av1_v4r3_ordinal_window_plan_seam_sha256,
    build_av1_v4r3_ordinal_window_plan_draft,
    finalize_av1_v4r3_ordinal_window_plan,
)
from mediaforce.tuning.av1_validation_v4r3_qualification_request import (
    av1_v4r3_qualification_request_valid_until,
    build_av1_v4r3_qualification_request,
)
from tests.test_av1_validation_v4r3_preparation_custody import _r3_freeze


_PLAN_OPENS = "2026-08-08T04:00:00Z"
_PLAN_CLOSES = "2026-08-09T10:40:00Z"
_RUN_REGISTRY_ID = "av1v4r3runreg_" + "a" * 64


class AV1V4R3ExecutionPreflightContractTests(unittest.TestCase):
    def test_pair_round_trip_is_non_authorizing_and_bijective(self) -> None:
        draft = _plan_draft()
        preflight = _preflight(draft=draft)
        plan = finalize_av1_v4r3_ordinal_window_plan(
            draft=draft,
            r3_preflight_id=str(preflight["preflight_id"]),
        )
        data = serialize_av1_v4r3_execution_preflight(preflight)

        self.assertEqual(deserialize_av1_v4r3_execution_preflight(data), preflight)
        self.assertEqual(plan["r3_preflight_id"], preflight["preflight_id"])
        self.assertEqual(
            preflight["ordinal_window_plan_seam_sha256"],
            av1_v4r3_ordinal_window_plan_seam_sha256(plan),
        )
        assert_av1_v4r3_ordinal_plan_preflight_pair(
            plan=plan,
            preflight=preflight,
        )
        self.assertEqual(
            preflight["execution_readiness_state"],
            AV1_V4R3_EXECUTION_PREFLIGHT_READINESS_STATE,
        )
        self.assertNotIn("execution_ready", preflight)
        self.assertEqual(preflight["blockers"], [])
        self.assertTrue(preflight[AV1_V4R3_EXECUTION_PREFLIGHT_APPROVAL_FIELD])
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            self.assertIs(preflight[field], False)

    def test_seam_is_stable_and_preflight_id_includes_created_at(self) -> None:
        draft = _plan_draft()
        reordered = dict(reversed(list(draft.items())))
        self.assertEqual(
            av1_v4r3_ordinal_window_plan_seam_sha256(draft),
            av1_v4r3_ordinal_window_plan_seam_sha256(reordered),
        )

        changed = dict(draft)
        changed["run_registry_id"] = "av1v4r3runreg_" + "b" * 64
        self.assertNotEqual(
            av1_v4r3_ordinal_window_plan_seam_sha256(draft),
            av1_v4r3_ordinal_window_plan_seam_sha256(changed),
        )

        preflight = _preflight(draft=draft)
        changed_preflight = dict(preflight)
        changed_preflight["created_at"] = "2026-08-08T04:00:01Z"
        self.assertNotEqual(
            av1_v4r3_execution_preflight_id(preflight),
            av1_v4r3_execution_preflight_id(changed_preflight),
        )

    def test_ordinal_readiness_and_unresolved_bindings_are_honest(self) -> None:
        preflight = _preflight()
        readiness = preflight["ordinal_readiness"]
        self.assertEqual(len(readiness), 8)
        self.assertEqual([item["ordinal"] for item in readiness], list(range(1, 9)))
        self.assertTrue(
            all(item["media_read_performed"] is False for item in readiness)
        )

        unresolved = preflight["unresolved_execution_bindings"]
        self.assertEqual(
            unresolved["resolution_stage"],
            "runner_admission_under_owner_execution_grant",
        )
        self.assertEqual(len(unresolved["assets"]), 4)
        for asset in unresolved["assets"].values():
            self.assertIsNone(asset["production_stream_plan_id"])
            self.assertIsNone(asset["stream_budget_ledger_id"])

    def test_rejects_rebound_authority_scope_and_readiness_tampering(self) -> None:
        preflight = _preflight()
        mutations = (
            ("state", "execution_authorized"),
            ("plan_span_seconds", float(preflight["plan_span_seconds"])),
            (AV1_V4R3_EXECUTION_PREFLIGHT_APPROVAL_FIELD, 1),
            ("selection_or_partition_use_allowed", 0),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                mutated = dict(preflight)
                mutated[field] = value
                with self.assertRaises(AV1V4R3ExecutionPreflightError):
                    assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            with self.subTest(authority=field):
                mutated = dict(preflight)
                mutated[field] = True
                with self.assertRaises(AV1V4R3ExecutionPreflightError):
                    assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

        mutated = dict(preflight)
        mutated["preflight_scope"] = dict(preflight["preflight_scope"])
        mutated["preflight_scope"]["retry_authorized"] = True
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

        mutated = dict(preflight)
        mutated["readiness"] = dict(preflight["readiness"])
        mutated["readiness"]["media_bytes_read"] = True
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

        mutated = dict(preflight)
        mutated["blockers"] = ["unexpected"]
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

        missing = dict(preflight)
        missing.pop("runtime_id")
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_execution_preflight(_rebind_preflight(missing))

        extra = dict(preflight)
        extra["smuggled"] = False
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_execution_preflight(_rebind_preflight(extra))

    def test_rejects_rebound_repository_invocation_and_readiness_tampering(
        self,
    ) -> None:
        preflight = _preflight()

        mutated = dict(preflight)
        mutated["preflight_repository"] = {"commit": "f" * 40, "tree": "e" * 40}
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

        mutated = dict(preflight)
        mutated["r3_invocation_digests"] = [
            dict(item) for item in preflight["r3_invocation_digests"]
        ]
        mutated["r3_invocation_digests"][1]["invocation_sha256"] = mutated[
            "r3_invocation_digests"
        ][0]["invocation_sha256"]
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

        mutated = dict(preflight)
        mutated["r3_closure_ids"] = list(reversed(preflight["r3_closure_ids"]))
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

        mutated = dict(preflight)
        mutated["ordinal_readiness"] = [
            dict(item) for item in preflight["ordinal_readiness"]
        ]
        mutated["ordinal_readiness"][0]["ledger_closure_id"] = (
            "av1v4r3ledger_" + "f" * 32
        )
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

        mutated = dict(preflight)
        mutated["ordinal_window_plan_seam_sha256"] = "sha256:" + "f" * 64
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

        mutated = dict(preflight)
        mutated["unresolved_execution_bindings"] = {
            **preflight["unresolved_execution_bindings"],
            "must_verify_returned_ledger_id": False,
        }
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

    def test_builder_rejects_timeline_and_cross_artifact_mismatch(self) -> None:
        request = _qualification_request()
        draft = _plan_draft(request=request)

        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            build_av1_v4r3_execution_preflight(
                qualification_request=request,
                ordinal_window_plan_draft=draft,
                owner_principal="owner:test",
                preflight_repository_commit="1" * 40,
                preflight_repository_tree="2" * 40,
                created_at="2026-08-08T04:00:01Z",
            )

        mismatched = dict(draft)
        mismatched["r3_request_id"] = "av1v4r3req_" + "f" * 32
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            build_av1_v4r3_execution_preflight(
                qualification_request=request,
                ordinal_window_plan_draft=mismatched,
                owner_principal="owner:test",
                preflight_repository_commit="1" * 40,
                preflight_repository_tree="2" * 40,
                created_at=_PLAN_OPENS,
            )

        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            build_av1_v4r3_execution_preflight(
                qualification_request=request,
                ordinal_window_plan_draft=draft,
                owner_principal="owner:test",
                preflight_repository_commit="f" * 40,
                preflight_repository_tree="e" * 40,
                created_at=_PLAN_OPENS,
            )

    def test_pair_rejects_wrong_preflight_reference(self) -> None:
        draft = _plan_draft()
        preflight = _preflight(draft=draft)
        mismatched_plan = finalize_av1_v4r3_ordinal_window_plan(
            draft=draft,
            r3_preflight_id="av1v4r3preflight_" + "f" * 32,
        )
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_ordinal_plan_preflight_pair(
                plan=mismatched_plan,
                preflight=preflight,
            )

    def test_rejects_noncanonical_timestamp_rebinding(self) -> None:
        preflight = _preflight()
        for created_at in (
            "2026-08-08T04:00:00.000000Z",
            "2026-08-08T04:00:00+00:00",
            "2026-08-08 04:00:00Z",
        ):
            with self.subTest(created_at=created_at):
                mutated = dict(preflight)
                mutated["created_at"] = created_at
                with self.assertRaises(AV1V4R3ExecutionPreflightError):
                    assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

    def test_rejects_noncanonical_nonfinite_and_private_payloads(self) -> None:
        preflight = _preflight()
        data = serialize_av1_v4r3_execution_preflight(preflight)
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            deserialize_av1_v4r3_execution_preflight(data.rstrip(b"\n"))
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                with self.assertRaises(AV1V4R3ExecutionPreflightError):
                    deserialize_av1_v4r3_execution_preflight(
                        b'{"payload_sha256":' + constant + b"}\n"
                    )

        mutated = dict(preflight)
        mutated["owner_principal"] = "/private/owner"
        with self.assertRaises(AV1V4R3ExecutionPreflightError):
            assert_av1_v4r3_execution_preflight(_rebind_preflight(mutated))

    def test_scope_is_immutable_and_module_is_pure(self) -> None:
        with self.assertRaises(TypeError):
            AV1_V4R3_EXECUTION_PREFLIGHT_SCOPE["retry_authorized"] = True

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
                "fcntl",
                "os",
                "pathlib",
                "socket",
                "subprocess",
                "mediaforce.execution",
                "mediaforce.core.db",
                "mediaforce.web",
            }.isdisjoint(imports)
        )
        self.assertFalse(any(name.endswith("_registry") for name in imports))
        self.assertFalse(any(name.endswith("_operation") for name in imports))


def _qualification_request() -> dict[str, object]:
    requested_at = "2026-08-08T03:30:00Z"
    return build_av1_v4r3_qualification_request(
        owner_freeze=_r3_freeze(),
        owner_principal="owner:test",
        requested_at=requested_at,
        valid_until=av1_v4r3_qualification_request_valid_until(requested_at),
        requesting_repository_commit="1" * 40,
        requesting_repository_tree="2" * 40,
    )


def _plan_draft(*, request: dict[str, object] | None = None) -> dict[str, object]:
    request_payload = request or _qualification_request()
    return build_av1_v4r3_ordinal_window_plan_draft(
        r3_manifest_id=AV1_V4R3_MANIFEST_ID,
        r3_manifest_valid_until=AV1_V4R3_MANIFEST_VALID_UNTIL,
        r3_request_id=str(request_payload["request_id"]),
        r3_request_valid_until=str(request_payload["valid_until"]),
        r3_repository=request_payload["reviewed_repository"],
        r3_toolchain_id=str(request_payload["toolchain_id"]),
        r3_closure_ids=list(request_payload["closure_ids"]),
        r3_invocation_digests=list(request_payload["invocation_digests"]),
        plan_opens_at=_PLAN_OPENS,
        plan_closes_at=_PLAN_CLOSES,
        run_registry_id=_RUN_REGISTRY_ID,
    )


def _preflight(*, draft: dict[str, object] | None = None) -> dict[str, object]:
    return build_av1_v4r3_execution_preflight(
        qualification_request=_qualification_request(),
        ordinal_window_plan_draft=draft or _plan_draft(),
        owner_principal="owner:test",
        preflight_repository_commit="1" * 40,
        preflight_repository_tree="2" * 40,
        created_at=_PLAN_OPENS,
    )


def _rebind_preflight(payload: dict[str, object]) -> dict[str, object]:
    rebound = dict(payload)
    rebound["preflight_id"] = av1_v4r3_execution_preflight_id(rebound)
    without_sha = {
        key: value for key, value in rebound.items() if key != "payload_sha256"
    }
    rebound["payload_sha256"] = "sha256:" + stable_json_hash(without_sha)
    return rebound


if __name__ == "__main__":
    unittest.main()
