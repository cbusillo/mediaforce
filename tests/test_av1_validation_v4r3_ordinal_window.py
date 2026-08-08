from __future__ import annotations

import ast
import os
import stat
import textwrap
import threading
import unittest
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
from unittest.mock import patch

from mediaforce.tuning import av1_validation_v4r3_ordinal_window as pure_module
from mediaforce.tuning.av1_validation_v4 import AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_PROTOCOL_VERSION,
    build_av1_v4_r3_all_closure_payloads,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_VALID_UNTIL,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1V4R3OrdinalWindowError,
    AV1_V4R3_OW_AGGREGATE_SECONDS_MAX,
    AV1_V4R3_OW_ORDINAL_COUNT,
    AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX,
    AV1_V4R3_OW_SETUP_SECONDS,
    AV1_V4R3_OW_TEARDOWN_SECONDS,
    AV1_V4R3_OW_TRAVERSAL_SECONDS_MAX,
    AV1_V4R3_OW_WINDOW_SECONDS_MAX,
    assert_av1_v4r3_ordinal_window_plan,
    av1_v4r3_ordinal_window_plan_seam_sha256,
    build_av1_v4r3_ordinal_window_plan_draft,
    deserialize_av1_v4r3_ordinal_window_plan,
    finalize_av1_v4r3_ordinal_window_plan,
    serialize_av1_v4r3_ordinal_window_claim,
    serialize_av1_v4r3_ordinal_window_plan,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window_registry import (
    AV1V4R3OrdinalWindowRegistryBinding,
    AV1V4R3OrdinalWindowRegistryError,
    assert_av1_v4r3_ordinal_window_file_custody,
    assert_av1_v4r3_ordinal_window_registry,
    av1_v4r3_ordinal_window_registry_hmac_id,
    derive_av1_v4r3_ordinal_window_high_water,
    load_av1_v4r3_ordinal_window_registry_terminal,
    publish_av1_v4r3_ordinal_window_claim,
    publish_av1_v4r3_ordinal_window_grant,
    publish_av1_v4r3_ordinal_window_outcome,
    publish_av1_v4r3_ordinal_window_plan,
    publish_av1_v4r3_ordinal_window_started,
    publish_av1_v4r3_ordinal_window_terminal,
)


_OPENS = "2026-08-08T04:00:00Z"
_CLOSES = "2026-08-09T10:40:00Z"
_REQUEST_UNTIL = "2026-08-10T03:30:00Z"
_FAKE_HEX32 = "a" * 32
_FAKE_HEX40 = "b" * 40
_KEY = b"k" * 32
_REPOSITORY = {"commit": _FAKE_HEX40, "tree": _FAKE_HEX40}
_TOOLCHAIN_ID = f"av1v4r3toolchain_{_FAKE_HEX32}"


def _ts(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 8, 8, hour, minute, second, tzinfo=UTC)


def _clock(value: datetime) -> Callable[[], datetime]:
    return lambda: value


def _make_registry(tmp_path: Path) -> Path:
    registry = tmp_path / "registry"
    registry.mkdir(mode=0o700)
    return registry


def _binding(registry: Path) -> AV1V4R3OrdinalWindowRegistryBinding:
    return AV1V4R3OrdinalWindowRegistryBinding(
        registry=registry,
        run_registry_id=av1_v4r3_ordinal_window_registry_hmac_id(
            registry,
            key=_KEY,
        ),
    )


def _closure_ids() -> list[str]:
    return [
        str(closure["closure_id"]) for closure in build_av1_v4_r3_all_closure_payloads()
    ]


def _invocation_digests() -> list[dict[str, object]]:
    return [
        {
            "ordinal": index,
            "closure_id": closure_id,
            "invocation_sha256": f"sha256:{index:064x}",
            "r3_repository": dict(_REPOSITORY),
            "r3_toolchain_id": _TOOLCHAIN_ID,
        }
        for index, closure_id in enumerate(_closure_ids(), start=1)
    ]


def _make_plan(binding: AV1V4R3OrdinalWindowRegistryBinding) -> dict[str, object]:
    return pure_module.build_av1_v4r3_ordinal_window_plan(
        r3_manifest_id=AV1_V4R3_MANIFEST_ID,
        r3_manifest_valid_until=AV1_V4R3_MANIFEST_VALID_UNTIL,
        r3_request_id=f"av1v4r3req_{_FAKE_HEX32}",
        r3_request_valid_until=_REQUEST_UNTIL,
        r3_preflight_id=f"av1v4r3preflight_{_FAKE_HEX32}",
        r3_repository=_REPOSITORY,
        r3_toolchain_id=_TOOLCHAIN_ID,
        r3_closure_ids=_closure_ids(),
        r3_invocation_digests=_invocation_digests(),
        plan_opens_at=_OPENS,
        plan_closes_at=_CLOSES,
        run_registry_id=binding.run_registry_id,
    )


def _publish_grant_claim_start_outcome(
    binding: AV1V4R3OrdinalWindowRegistryBinding,
    plan: dict[str, object],
    ordinal: int,
    *,
    success: bool = True,
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    authorized = _ts(7) + timedelta(minutes=ordinal * 10)
    grant = publish_av1_v4r3_ordinal_window_grant(
        binding=binding,
        plan=plan,
        ordinal=ordinal,
        clock=_clock(authorized),
        valid_until=(
            authorized + timedelta(seconds=AV1_V4R3_OW_WINDOW_SECONDS_MAX)
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    claim = publish_av1_v4r3_ordinal_window_claim(
        binding=binding,
        plan=plan,
        grant=grant,
        clock=_clock(authorized + timedelta(minutes=5)),
    )
    started = publish_av1_v4r3_ordinal_window_started(
        binding=binding,
        plan=plan,
        grant=grant,
        claim=claim,
        clock=_clock(authorized + timedelta(minutes=6)),
    )
    outcome = publish_av1_v4r3_ordinal_window_outcome(
        binding=binding,
        plan=plan,
        started=started,
        clock=_clock(authorized + timedelta(minutes=7)),
        success=success,
    )
    return grant, claim, started, outcome


def _claim_worker(
    registry_text: str, run_registry_id: str, plan: dict[str, object]
) -> str:
    binding = AV1V4R3OrdinalWindowRegistryBinding(
        registry=Path(registry_text),
        run_registry_id=run_registry_id,
    )
    grant = publish_av1_v4r3_ordinal_window_grant(
        binding=binding,
        plan=plan,
        ordinal=1,
        clock=_clock(_ts(7)),
        valid_until="2026-08-08T10:50:00Z",
    )
    try:
        publish_av1_v4r3_ordinal_window_claim(
            binding=binding,
            plan=plan,
            grant=grant,
            clock=_clock(_ts(7, 6)),
        )
    except AV1V4R3OrdinalWindowRegistryError:
        return "rejected"
    return "claimed"


class TimingAndSchemaTests(unittest.TestCase):
    def test_plan_draft_finalize_and_seam_are_deterministic(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            draft = build_av1_v4r3_ordinal_window_plan_draft(
                r3_manifest_id=AV1_V4R3_MANIFEST_ID,
                r3_manifest_valid_until=AV1_V4R3_MANIFEST_VALID_UNTIL,
                r3_request_id=f"av1v4r3req_{_FAKE_HEX32}",
                r3_request_valid_until=_REQUEST_UNTIL,
                r3_repository=_REPOSITORY,
                r3_toolchain_id=_TOOLCHAIN_ID,
                r3_closure_ids=_closure_ids(),
                r3_invocation_digests=_invocation_digests(),
                plan_opens_at=_OPENS,
                plan_closes_at=_CLOSES,
                run_registry_id=binding.run_registry_id,
            )
            plan = finalize_av1_v4r3_ordinal_window_plan(
                draft=draft,
                r3_preflight_id=f"av1v4r3preflight_{_FAKE_HEX32}",
            )

        assert_av1_v4r3_ordinal_window_plan(plan)
        self.assertEqual(
            av1_v4r3_ordinal_window_plan_seam_sha256(draft),
            av1_v4r3_ordinal_window_plan_seam_sha256(plan),
        )

    def test_plan_hardening_rejects_manifest_span_and_shape_drift(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            kwargs = {
                "r3_manifest_id": AV1_V4R3_MANIFEST_ID,
                "r3_manifest_valid_until": AV1_V4R3_MANIFEST_VALID_UNTIL,
                "r3_request_id": f"av1v4r3req_{_FAKE_HEX32}",
                "r3_request_valid_until": _REQUEST_UNTIL,
                "r3_repository": _REPOSITORY,
                "r3_toolchain_id": _TOOLCHAIN_ID,
                "r3_closure_ids": _closure_ids(),
                "r3_invocation_digests": _invocation_digests(),
                "plan_opens_at": _OPENS,
                "plan_closes_at": _CLOSES,
                "run_registry_id": binding.run_registry_id,
            }
            with self.assertRaises(AV1V4R3OrdinalWindowError):
                build_av1_v4r3_ordinal_window_plan_draft(
                    **{**kwargs, "r3_manifest_id": f"av1vmanifest4r3_{_FAKE_HEX32}"}
                )
            with self.assertRaises(AV1V4R3OrdinalWindowError):
                build_av1_v4r3_ordinal_window_plan_draft(
                    **{
                        **kwargs,
                        "plan_closes_at": "2026-08-09T10:39:59Z",
                    }
                )
            with self.assertRaises(AV1V4R3OrdinalWindowError):
                build_av1_v4r3_ordinal_window_plan_draft(
                    **{
                        **kwargs,
                        "plan_opens_at": "2026-08-08T01:26:53Z",
                        "plan_closes_at": "2026-08-09T08:06:53Z",
                    }
                )

            plan = _make_plan(binding)
            missing = dict(plan)
            missing.pop("run_registry_id")
            with self.assertRaises(AV1V4R3OrdinalWindowError):
                assert_av1_v4r3_ordinal_window_plan(missing)

    def test_window_and_aggregate_constants_are_preserved(self) -> None:
        self.assertEqual(AV1_V4R3_OW_SETUP_SECONDS, 300)
        self.assertEqual(AV1_V4R3_OW_TRAVERSAL_SECONDS_MAX, 13_200)
        self.assertEqual(AV1_V4R3_OW_TEARDOWN_SECONDS, 300)
        self.assertEqual(AV1_V4R3_OW_WINDOW_SECONDS_MAX, 13_800)
        self.assertEqual(AV1_V4R3_OW_AGGREGATE_SECONDS_MAX, 110_400)
        self.assertLessEqual(
            AV1_V4R3_OW_AGGREGATE_SECONDS_MAX,
            AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX,
        )

    def test_plan_publishes_opaque_registry_id_not_path(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
        self.assertIn("run_registry_id", plan)
        self.assertNotIn("run_registry", plan)
        self.assertNotIn("/", str(plan["run_registry_id"]))
        assert_av1_v4r3_ordinal_window_plan(plan)

    def test_exact_ordered_revision_3_closure_ids_required(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            self.assertEqual(plan["r3_closure_ids"], _closure_ids())
            reversed_ids = list(reversed(_closure_ids()))
            with self.assertRaises(AV1V4R3OrdinalWindowError):
                pure_module.build_av1_v4r3_ordinal_window_plan(
                    r3_manifest_id=AV1_V4R3_MANIFEST_ID,
                    r3_manifest_valid_until=AV1_V4R3_MANIFEST_VALID_UNTIL,
                    r3_request_id=f"av1v4r3req_{_FAKE_HEX32}",
                    r3_request_valid_until=_REQUEST_UNTIL,
                    r3_preflight_id=f"av1v4r3preflight_{_FAKE_HEX32}",
                    r3_repository=_REPOSITORY,
                    r3_toolchain_id=_TOOLCHAIN_ID,
                    r3_closure_ids=reversed_ids,
                    r3_invocation_digests=_invocation_digests(),
                    plan_opens_at=_OPENS,
                    plan_closes_at=_CLOSES,
                    run_registry_id=binding.run_registry_id,
                )

    def test_invocation_digest_cross_binding_required(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            digests = _invocation_digests()
            digests[0] = {
                **digests[0],
                "r3_toolchain_id": f"av1v4r3toolchain_{'d' * 32}",
            }
            with self.assertRaises(AV1V4R3OrdinalWindowError):
                pure_module.build_av1_v4r3_ordinal_window_plan(
                    r3_manifest_id=AV1_V4R3_MANIFEST_ID,
                    r3_manifest_valid_until=AV1_V4R3_MANIFEST_VALID_UNTIL,
                    r3_request_id=f"av1v4r3req_{_FAKE_HEX32}",
                    r3_request_valid_until=_REQUEST_UNTIL,
                    r3_preflight_id=f"av1v4r3preflight_{_FAKE_HEX32}",
                    r3_repository=_REPOSITORY,
                    r3_toolchain_id=_TOOLCHAIN_ID,
                    r3_closure_ids=_closure_ids(),
                    r3_invocation_digests=digests,
                    plan_opens_at=_OPENS,
                    plan_closes_at=_CLOSES,
                    run_registry_id=binding.run_registry_id,
                )

    def test_all_artifacts_keep_all_authority_fields_false(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            grant, claim, started, outcome = _publish_grant_claim_start_outcome(
                binding, plan, 1
            )
            terminal = publish_av1_v4r3_ordinal_window_terminal(
                binding=binding,
                plan=plan,
                clock=_clock(_ts(8)),
            )
        for artifact in (plan, grant, claim, started, outcome, terminal):
            for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                self.assertIs(artifact[field], False)

    def test_pure_module_has_no_filesystem_or_load_helpers(self) -> None:
        source = Path(pure_module.__file__).read_text()
        tree = ast.parse(source)
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertNotIn("pathlib", {node.module for node in imports})
        self.assertNotIn("os", {node.module for node in imports})
        self.assertFalse(
            any(
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                for node in ast.walk(tree)
            )
        )
        self.assertNotIn("def load_av1_v4r3_ordinal_window", source)

    def test_canonical_deserialize_accepts_bytes_only(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            data = serialize_av1_v4r3_ordinal_window_plan(plan)
            self.assertEqual(deserialize_av1_v4r3_ordinal_window_plan(data), plan)
            with self.assertRaises(AV1V4R3OrdinalWindowError):
                deserialize_av1_v4r3_ordinal_window_plan(data.rstrip(b"\n"))


class RegistryCustodyAndTimingTests(unittest.TestCase):
    def test_registry_requires_absolute_owner_only_0700_directory(self) -> None:
        with TemporaryDirectory() as raw:
            registry = _make_registry(Path(raw))
            assert_av1_v4r3_ordinal_window_registry(registry)
            registry.chmod(0o755)
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                assert_av1_v4r3_ordinal_window_registry(registry)

    def test_custody_rejects_symlink_and_hardlink_files(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            registry = _make_registry(root)
            binding = _binding(registry)
            plan = _make_plan(binding)
            publish_av1_v4r3_ordinal_window_plan(binding=binding, plan=plan)
            outside = root / "outside.json"
            outside.write_text("{}\n")
            os.chmod(outside, 0o600)
            os.unlink(registry / "plan.json")
            os.symlink(outside, registry / "plan.json")
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                assert_av1_v4r3_ordinal_window_file_custody(registry, "plan.json")
            os.unlink(registry / "plan.json")
            os.link(outside, registry / "plan.json")
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                assert_av1_v4r3_ordinal_window_file_custody(registry, "plan.json")

    def test_claim_uses_registry_grant_not_caller_substitution(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            grant = publish_av1_v4r3_ordinal_window_grant(
                binding=binding,
                plan=plan,
                ordinal=1,
                clock=_clock(_ts(7)),
                valid_until="2026-08-08T10:50:00Z",
            )
            forged = {**grant, "payload_sha256": f"sha256:{'f' * 64}"}
            with self.assertRaises(AV1V4R3OrdinalWindowError):
                publish_av1_v4r3_ordinal_window_claim(
                    binding=binding,
                    plan=plan,
                    grant=forged,
                    clock=_clock(_ts(7, 6)),
                )

    def test_byte_chain_substitution_is_terminal_fail_closed(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            grant = publish_av1_v4r3_ordinal_window_grant(
                binding=binding,
                plan=plan,
                ordinal=1,
                clock=_clock(_ts(7)),
                valid_until="2026-08-08T10:50:00Z",
            )
            claim = publish_av1_v4r3_ordinal_window_claim(
                binding=binding,
                plan=plan,
                grant=grant,
                clock=_clock(_ts(7, 6)),
            )
            started = publish_av1_v4r3_ordinal_window_started(
                binding=binding,
                plan=plan,
                grant=grant,
                claim=claim,
                clock=_clock(_ts(7, 7)),
            )
            replacement = pure_module.build_av1_v4r3_ordinal_window_claim(
                plan=plan,
                grant=grant,
                claimed_at="2026-08-08T07:06:01Z",
            )
            (binding.registry / "ordinal_01.claim.json").write_bytes(
                serialize_av1_v4r3_ordinal_window_claim(replacement)
            )
            os.chmod(binding.registry / "ordinal_01.claim.json", 0o600)
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                publish_av1_v4r3_ordinal_window_outcome(
                    binding=binding,
                    plan=plan,
                    started=started,
                    clock=_clock(_ts(7, 8)),
                    success=True,
                )
            terminal = load_av1_v4r3_ordinal_window_registry_terminal(binding.registry)
            self.assertIsNotNone(terminal)
            self.assertEqual(terminal["terminal_at"], "2026-08-08T07:08:00Z")

    def test_interrupted_atomic_publish_leaves_no_final_or_temp_artifact(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            with patch(
                "mediaforce.tuning.av1_validation_v4r3_ordinal_window_registry.os.link",
                side_effect=OSError("simulated interrupted publication"),
            ):
                with self.assertRaises(OSError):
                    publish_av1_v4r3_ordinal_window_plan(binding=binding, plan=plan)
            self.assertFalse((binding.registry / "plan.json").exists())
            self.assertFalse(list(binding.registry.glob(".*.tmp")))

    def test_stale_prelink_temp_is_removed_before_registry_access(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            stale = binding.registry / ".ordinal_01.grant.json.123.0123456789abcdef.tmp"
            stale.write_bytes(b"partial")
            stale.chmod(0o600)
            publish_av1_v4r3_ordinal_window_plan(binding=binding, plan=plan)
            self.assertFalse(stale.exists())

    def test_stale_postlink_temp_is_reconciled_before_registry_access(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            publish_av1_v4r3_ordinal_window_plan(binding=binding, plan=plan)
            final = binding.registry / "plan.json"
            stale = binding.registry / ".plan.json.123.0123456789abcdef.tmp"
            os.link(final, stale)
            self.assertEqual(final.stat().st_nlink, 2)
            publish_av1_v4r3_ordinal_window_plan(binding=binding, plan=plan)
            self.assertFalse(stale.exists())
            self.assertEqual(final.stat().st_nlink, 1)

    def test_clock_backdating_cannot_reopen_expired_window(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            grant = publish_av1_v4r3_ordinal_window_grant(
                binding=binding,
                plan=plan,
                ordinal=1,
                clock=_clock(_ts(7)),
                valid_until="2026-08-08T10:50:00Z",
            )
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                publish_av1_v4r3_ordinal_window_claim(
                    binding=binding,
                    plan=plan,
                    grant=grant,
                    clock=_clock(_ts(10, 46)),
                )
            self.assertIsNotNone(
                load_av1_v4r3_ordinal_window_registry_terminal(binding.registry)
            )
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                publish_av1_v4r3_ordinal_window_claim(
                    binding=binding,
                    plan=plan,
                    grant=grant,
                    clock=_clock(_ts(7, 6)),
                )

    def test_outcome_clock_rewind_publishes_absorbing_terminal(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            grant, claim, started, _ = _publish_grant_claim_start_outcome(
                binding, plan, 1
            )
            self.assertEqual(grant["ordinal"], claim["ordinal"])
            self.assertIsNotNone(started)
            self.assertEqual(
                derive_av1_v4r3_ordinal_window_high_water(
                    binding.registry, clock=_clock(_ts(8))
                ),
                1,
            )

        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            grant = publish_av1_v4r3_ordinal_window_grant(
                binding=binding,
                plan=plan,
                ordinal=1,
                clock=_clock(_ts(7)),
                valid_until="2026-08-08T10:50:00Z",
            )
            claim = publish_av1_v4r3_ordinal_window_claim(
                binding=binding,
                plan=plan,
                grant=grant,
                clock=_clock(_ts(7, 6)),
            )
            started = publish_av1_v4r3_ordinal_window_started(
                binding=binding,
                plan=plan,
                grant=grant,
                claim=claim,
                clock=_clock(_ts(7, 7)),
            )
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                publish_av1_v4r3_ordinal_window_outcome(
                    binding=binding,
                    plan=plan,
                    started=started,
                    clock=_clock(_ts(7, 6, 59)),
                    success=True,
                )
            self.assertIsNotNone(
                load_av1_v4r3_ordinal_window_registry_terminal(binding.registry)
            )

    def test_failure_outcome_and_crash_started_state_are_absorbing(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            _publish_grant_claim_start_outcome(binding, plan, 1, success=False)
            terminal = load_av1_v4r3_ordinal_window_registry_terminal(binding.registry)
            self.assertIsNotNone(terminal)
            self.assertFalse(terminal["success_all_ordinals"])
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                derive_av1_v4r3_ordinal_window_high_water(
                    binding.registry,
                    clock=_clock(_ts(8)),
                )
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                publish_av1_v4r3_ordinal_window_grant(
                    binding=binding,
                    plan=plan,
                    ordinal=2,
                    clock=_clock(_ts(8)),
                    valid_until="2026-08-08T11:50:00Z",
                )

        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            grant = publish_av1_v4r3_ordinal_window_grant(
                binding=binding,
                plan=plan,
                ordinal=1,
                clock=_clock(_ts(7)),
                valid_until="2026-08-08T10:50:00Z",
            )
            claim = publish_av1_v4r3_ordinal_window_claim(
                binding=binding,
                plan=plan,
                grant=grant,
                clock=_clock(_ts(7, 6)),
            )
            publish_av1_v4r3_ordinal_window_started(
                binding=binding,
                plan=plan,
                grant=grant,
                claim=claim,
                clock=_clock(_ts(7, 7)),
            )
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                derive_av1_v4r3_ordinal_window_high_water(
                    binding.registry, clock=_clock(_ts(8))
                )
            self.assertIsNotNone(
                load_av1_v4r3_ordinal_window_registry_terminal(binding.registry)
            )

    def test_successful_ordinal_eight_publishes_success_terminal(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            for ordinal in range(1, 9):
                _publish_grant_claim_start_outcome(binding, plan, ordinal)
            terminal = load_av1_v4r3_ordinal_window_registry_terminal(binding.registry)
            self.assertIsNotNone(terminal)
            self.assertTrue(terminal["success_all_ordinals"])
            self.assertEqual(terminal["ordinal_count_successful"], 8)

    def test_no_hwm_sentinel_files_are_created(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            _publish_grant_claim_start_outcome(binding, plan, 1)
            self.assertFalse(list(binding.registry.glob("*.hwm")))
            self.assertEqual(
                derive_av1_v4r3_ordinal_window_high_water(
                    binding.registry, clock=_clock(_ts(8))
                ),
                1,
            )


class ConcurrencyTests(unittest.TestCase):
    def test_same_process_claim_concurrency_allows_exactly_one_claim(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            grant = publish_av1_v4r3_ordinal_window_grant(
                binding=binding,
                plan=plan,
                ordinal=1,
                clock=_clock(_ts(7)),
                valid_until="2026-08-08T10:50:00Z",
            )
            results: list[str] = []
            barrier = threading.Barrier(8)

            def worker() -> None:
                barrier.wait()
                try:
                    publish_av1_v4r3_ordinal_window_claim(
                        binding=binding,
                        plan=plan,
                        grant=grant,
                        clock=_clock(_ts(7, 6)),
                    )
                except AV1V4R3OrdinalWindowRegistryError:
                    results.append("rejected")
                else:
                    results.append("claimed")

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(results.count("claimed"), 1)
            self.assertEqual(results.count("rejected"), 7)

    def test_cross_process_claim_concurrency_allows_exactly_one_claim(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            ctx = get_context("spawn")
            with ctx.Pool(4) as pool:
                results = pool.starmap(
                    _claim_worker,
                    [
                        (str(binding.registry), binding.run_registry_id, plan)
                        for _ in range(4)
                    ],
                )
            self.assertEqual(results.count("claimed"), 1)
            self.assertEqual(results.count("rejected"), 3)


class NonRegressionTests(unittest.TestCase):
    def test_protocol_and_manifest_revision_are_r3_without_reinterpreting_r2(
        self,
    ) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
        self.assertEqual(plan["protocol_version"], AV1_V4_R3_PROTOCOL_VERSION)
        self.assertEqual(plan["manifest_revision"], AV1_V4_R3_MANIFEST_REVISION)
        self.assertNotIn("revision_2", str(plan))

    def test_private_path_strings_are_rejected_in_public_artifacts(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            tampered = {**plan, "r3_preflight_id": "/Users/cbusillo/private"}
            with self.assertRaises(AV1V4R3OrdinalWindowError):
                assert_av1_v4r3_ordinal_window_plan(tampered)


class StaticImplementationTests(unittest.TestCase):
    def test_registry_uses_directory_fd_and_flock_not_pathlib_file_io(self) -> None:
        source = Path(
            "mediaforce/tuning/av1_validation_v4r3_ordinal_window_registry.py"
        ).read_text()
        self.assertIn("fcntl.flock(dir_fd, fcntl.LOCK_EX)", source)
        self.assertIn("dir_fd=self.dir_fd", source)
        self.assertIn("os.O_DIRECTORY", source)
        self.assertIn("os.O_NOFOLLOW", source)
        tree = ast.parse(source)
        forbidden_attrs = {"read_bytes", "write_bytes"}
        self.assertFalse(
            any(
                isinstance(node, ast.Attribute) and node.attr in forbidden_attrs
                for node in ast.walk(tree)
            )
        )

    def test_registry_has_no_empty_hwm_sentinels(self) -> None:
        source = Path(
            "mediaforce/tuning/av1_validation_v4r3_ordinal_window_registry.py"
        ).read_text()
        self.assertNotIn("hwm", source.lower())

    def test_no_cli_runner_media_or_network_surface_added(self) -> None:
        source = Path(
            "mediaforce/tuning/av1_validation_v4r3_ordinal_window_registry.py"
        ).read_text()
        for token in (
            "requests",
            "urllib",
            "subprocess",
            "ffmpeg",
            "click",
            "argparse",
        ):
            self.assertNotIn(token, source)


class DirFdRegressionTests(unittest.TestCase):
    def test_transition_body_uses_one_locked_registry_context(self) -> None:
        source = textwrap.dedent(
            Path(
                "mediaforce/tuning/av1_validation_v4r3_ordinal_window_registry.py"
            ).read_text()
        )
        tree = ast.parse(source)
        transition_names = {
            "publish_av1_v4r3_ordinal_window_claim",
            "publish_av1_v4r3_ordinal_window_started",
            "publish_av1_v4r3_ordinal_window_outcome",
            "publish_av1_v4r3_ordinal_window_terminal",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in transition_names:
                with_count = sum(
                    isinstance(child, ast.With) for child in ast.walk(node)
                )
                self.assertEqual(with_count, 1, node.name)
                self.assertNotIn("os.open", ast.unparse(node))
                self.assertNotIn("Path(", ast.unparse(node))

    def test_registry_record_files_are_0600(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(_make_registry(Path(raw)))
            plan = _make_plan(binding)
            publish_av1_v4r3_ordinal_window_plan(binding=binding, plan=plan)
            mode = stat.S_IMODE((binding.registry / "plan.json").stat().st_mode)
            self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
