from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.tuning.av1_validation_v3 import load_av1_validation_protocol_v3
from mediaforce.tuning.av1_validation_v3_qualification import (
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_authorization import (
    build_av1_validation_v3_tier1_compat_grant,
    build_av1_validation_v3_tier1_compat_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_probe import (
    AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SHA256,
    AV1ValidationV3Tier1CompatExecutionContext,
    build_av1_validation_v3_tier1_compat_probe_plans,
    load_av1_validation_v3_tier1_compat_probe_matrix,
)
from mediaforce.tuning.av1_validation_v3_tier1_residual_authorization import (
    build_av1_validation_v3_tier1_residual_grant,
    build_av1_validation_v3_tier1_residual_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_residual_probe import (
    AV1_VALIDATION_V3_TIER1_RESIDUAL_MATRIX_SHA256,
    AV1ValidationV3Tier1ResidualExecutionContext,
    build_av1_validation_v3_tier1_residual_probe_plans,
    load_av1_validation_v3_tier1_residual_probe_matrix,
)
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
    AV1ValidationV3Tier1ExecutionContext,
    load_av1_validation_v3_tier1_fixture_matrix,
)
from mediaforce.tuning.av1_validation_v3_tier1_grant import (
    build_av1_validation_v3_tier1_execution_grant,
)
from mediaforce.tuning.av1_validation_v3_tier1_config_snapshot import (
    av1_validation_v3_tier1_config_snapshot_sha256,
    build_av1_validation_v3_tier1_config_snapshot,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    build_av1_validation_v3_tier1_authorization_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_runtime import (
    AV1ValidationV3Tier1PausedRuntimeExecutor,
    AV1ValidationV3Tier1RuntimeError,
    AV1ValidationV3Tier1RuntimeLimits,
    build_av1_validation_v3_tier1_toolchain_binding,
    paused_av1_validation_v3_tier1_compat_runtime,
    paused_av1_validation_v3_tier1_residual_runtime,
    paused_av1_validation_v3_tier1_runtime,
)
from mediaforce.web.runtime_lock import (
    assert_mediaforce_runtime_lock_held,
    exclusive_mediaforce_runtime_lock,
)


MATRIX_PATH = Path("docs/validation/av1-tier1-synthetic-fixture-matrix-v3.json")
COMPAT_MATRIX_PATH = Path("docs/validation/av1-tier1-compat-probe-matrix-v1.json")
RESIDUAL_MATRIX_PATH = Path("docs/validation/av1-tier1-residual-probe-matrix-v1.json")
PROTOCOL_PATH = Path("docs/validation/av1-cold-start-preregistration-v3.json")


class AV1ValidationV3Tier1RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_directory = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_directory.cleanup)
        self.runtime_root = Path(runtime_directory.name)
        self.output_root = self.runtime_root / "outputs"
        self.output_root.mkdir()
        self.output_root = self.output_root.resolve()
        self.config = MediaforceConfig(
            raw={
                "config": {"include_files": []},
                "video": {"codec": "av1"},
            },
            paths=ConfigPaths(
                project_root=self.runtime_root,
                config_path=self.runtime_root / "config.toml",
                db_path=self.runtime_root / "mediaforce.sqlite3",
                run_manifest_dir=self.runtime_root / "manifests",
                web_state_dir=self.runtime_root / "state",
                review_dir=self.runtime_root / "review",
                runtime_settings_path=self.runtime_root / "runtime-settings.json",
                runtime_reservation_dir=self.runtime_root / "runtime-reservations",
            ),
        )
        self.config_snapshot_bytes = build_av1_validation_v3_tier1_config_snapshot(
            self.config
        )
        self.toolchain = build_av1_validation_v3_tier1_toolchain_binding(
            ffmpeg_path=Path(sys.executable),
            ffprobe_path=Path(sys.executable),
        )
        self.matrix = load_av1_validation_v3_tier1_fixture_matrix(MATRIX_PATH)
        self.context = _execution_context(
            self.toolchain.payload_sha256,
            av1_validation_v3_tier1_config_snapshot_sha256(
                self.config_snapshot_bytes
            ),
        )
        self.compat_matrix = load_av1_validation_v3_tier1_compat_probe_matrix(
            COMPAT_MATRIX_PATH
        )
        self.compat_context = _compat_execution_context(
            self.toolchain.payload_sha256,
            av1_validation_v3_tier1_config_snapshot_sha256(
                self.config_snapshot_bytes
            ),
        )
        self.residual_matrix = load_av1_validation_v3_tier1_residual_probe_matrix(
            RESIDUAL_MATRIX_PATH
        )
        self.residual_context = _residual_execution_context(
            self.toolchain.payload_sha256,
            av1_validation_v3_tier1_config_snapshot_sha256(
                self.config_snapshot_bytes
            ),
        )

    def test_toolchain_binding_is_canonical_and_detects_changes(self) -> None:
        self.assertEqual(
            tuple(program.name for program in self.toolchain.programs),
            ("ffmpeg", "ffprobe"),
        )
        self.assertTrue(self.toolchain.payload_sha256.startswith("sha256:"))
        changed = replace(
            self.toolchain.programs[0],
            modified_ns=self.toolchain.programs[0].modified_ns + 1,
        )
        with self.assertRaisesRegex(AV1ValidationV3Tier1RuntimeError, "binary changed"):
            with exclusive_mediaforce_runtime_lock(
                self.config,
                owner_payload={"purpose": "tier1-runtime-test"},
            ) as lease:
                executor = AV1ValidationV3Tier1PausedRuntimeExecutor(
                    lease=lease,
                    output_root=self.output_root,
                    toolchain=replace(
                        self.toolchain,
                        programs=(changed, self.toolchain.programs[1]),
                    ),
                    limits=AV1ValidationV3Tier1RuntimeLimits(),
                    buffered_commands=frozenset({("ffmpeg", "-c", "pass")}),
                    streaming_commands=frozenset(),
                )
                executor.run(("ffmpeg", "-c", "pass"))

    def test_runtime_session_acquires_new_pause_lease_and_rejects_arbitrary_commands(self) -> None:
        session = None
        with paused_av1_validation_v3_tier1_runtime(
            self.config,
            config_snapshot_bytes=self.config_snapshot_bytes,
            context=self.context,
            matrix=self.matrix,
            output_directory=self.output_root,
            repository_root=Path.cwd(),
            toolchain=self.toolchain,
        ) as active_session:
            session = active_session
            assert_mediaforce_runtime_lock_held().assert_active()
            with self.assertRaisesRegex(AV1ValidationV3Tier1RuntimeError, "frozen fixture matrix"):
                active_session.executor.run(("ffprobe", "-c", "print('not frozen')"))
        self.assertIsNotNone(session)
        self.assertTrue(session.cleanup_passed)

    def test_compat_runtime_allows_only_frozen_buffered_commands(self) -> None:
        plans = build_av1_validation_v3_tier1_compat_probe_plans(
            self.compat_matrix,
            output_directory=self.output_root,
            repository_root=Path.cwd(),
        )
        expected_commands = frozenset(
            command
            for plan in plans
            for command in (
                plan.generate_args,
                *(surface.probe_args for surface in plan.surfaces),
            )
        )
        session = None
        with paused_av1_validation_v3_tier1_compat_runtime(
            self.config,
            config_snapshot_bytes=self.config_snapshot_bytes,
            context=self.compat_context,
            matrix=self.compat_matrix,
            output_directory=self.output_root,
            repository_root=Path.cwd(),
            toolchain=self.toolchain,
        ) as active_session:
            session = active_session
            assert_mediaforce_runtime_lock_held().assert_active()
            self.assertEqual(active_session.executor._buffered_commands, expected_commands)
            self.assertEqual(active_session.executor._streaming_commands, frozenset())
            with self.assertRaisesRegex(
                AV1ValidationV3Tier1RuntimeError,
                "frozen fixture matrix",
            ):
                active_session.executor.run(("ffprobe", "-c", "print('not frozen')"))
        self.assertIsNotNone(session)
        self.assertTrue(session.cleanup_passed)

    def test_residual_runtime_allows_exact_buffered_and_streaming_commands(self) -> None:
        plans = build_av1_validation_v3_tier1_residual_probe_plans(
            self.residual_matrix,
            output_directory=self.output_root,
            repository_root=Path.cwd(),
        )
        expected_buffered = frozenset(
            command
            for plan in plans
            for command in (
                plan.generate_args,
                *(surface.command_args for surface in plan.surfaces if not surface.streaming),
            )
        )
        expected_streaming = frozenset(
            surface.command_args
            for plan in plans
            for surface in plan.surfaces
            if surface.streaming
        )
        session = None
        with paused_av1_validation_v3_tier1_residual_runtime(
            self.config,
            config_snapshot_bytes=self.config_snapshot_bytes,
            context=self.residual_context,
            matrix=self.residual_matrix,
            output_directory=self.output_root,
            repository_root=Path.cwd(),
            toolchain=self.toolchain,
        ) as active_session:
            session = active_session
            assert_mediaforce_runtime_lock_held().assert_active()
            self.assertEqual(active_session.executor._buffered_commands, expected_buffered)
            self.assertEqual(active_session.executor._streaming_commands, expected_streaming)
            self.assertEqual(len(expected_buffered), 12)
            self.assertEqual(len(expected_streaming), 4)
            with self.assertRaisesRegex(
                AV1ValidationV3Tier1RuntimeError,
                "frozen fixture matrix",
            ):
                active_session.executor.run(("ffprobe", "-c", "print('not frozen')"))
        self.assertIsNotNone(session)
        self.assertTrue(session.cleanup_passed)

    def test_runtime_session_rejects_reentrant_lock_and_toolchain_mismatch(self) -> None:
        with exclusive_mediaforce_runtime_lock(
            self.config,
            owner_payload={"purpose": "outer-test"},
        ):
            with self.assertRaisesRegex(AV1ValidationV3Tier1RuntimeError, "newly acquired"):
                with paused_av1_validation_v3_tier1_runtime(
                    self.config,
                    config_snapshot_bytes=self.config_snapshot_bytes,
                    context=self.context,
                    matrix=self.matrix,
                    output_directory=self.output_root,
                    repository_root=Path.cwd(),
                    toolchain=self.toolchain,
                ):
                    pass
        mismatched_context = _execution_context(
            f"sha256:{'f' * 64}",
            av1_validation_v3_tier1_config_snapshot_sha256(
                self.config_snapshot_bytes
            ),
        )
        with self.assertRaisesRegex(AV1ValidationV3Tier1RuntimeError, "not bound"):
            with paused_av1_validation_v3_tier1_runtime(
                self.config,
                config_snapshot_bytes=self.config_snapshot_bytes,
                context=mismatched_context,
                matrix=self.matrix,
                output_directory=self.output_root,
                repository_root=Path.cwd(),
                toolchain=self.toolchain,
            ):
                pass

    def test_runtime_session_rejects_inactive_grant_before_lock_acquisition(self) -> None:
        inactive_context = replace(self.context, as_of="2026-08-04T19:00:00Z")
        with self.assertRaisesRegex(AV1ValidationV3Tier1RuntimeError, "grant is not active"):
            with paused_av1_validation_v3_tier1_runtime(
                self.config,
                config_snapshot_bytes=self.config_snapshot_bytes,
                context=inactive_context,
                matrix=self.matrix,
                output_directory=self.output_root,
                repository_root=Path.cwd(),
                toolchain=self.toolchain,
            ):
                pass

    def test_runtime_rejects_live_config_drift_and_unbound_snapshot(self) -> None:
        changed_config = replace(
            self.config,
            raw={
                **self.config.raw,
                "video": {"codec": "changed"},
            },
        )
        with self.assertRaisesRegex(
            AV1ValidationV3Tier1RuntimeError,
            "effective config changed",
        ):
            with paused_av1_validation_v3_tier1_runtime(
                changed_config,
                config_snapshot_bytes=self.config_snapshot_bytes,
                context=self.context,
                matrix=self.matrix,
                output_directory=self.output_root,
                repository_root=Path.cwd(),
                toolchain=self.toolchain,
            ):
                pass

        unbound_context = _execution_context(
            self.toolchain.payload_sha256,
            f"sha256:{'f' * 64}",
        )
        with self.assertRaisesRegex(
            AV1ValidationV3Tier1RuntimeError,
            "config snapshot is not bound",
        ):
            with paused_av1_validation_v3_tier1_runtime(
                self.config,
                config_snapshot_bytes=self.config_snapshot_bytes,
                context=unbound_context,
                matrix=self.matrix,
                output_directory=self.output_root,
                repository_root=Path.cwd(),
                toolchain=self.toolchain,
            ):
                pass

    def test_buffered_run_captures_output_and_sanitizes_environment(self) -> None:
        script = (
            "import json,os;"
            "print(json.dumps({'cwd':os.getcwd(),'secret':os.getenv('MEDIAFORCE_SECRET')}))"
        )
        command = ("ffprobe", "-c", script)
        with self._executor(buffered_commands={command}) as executor:
            previous = os.environ.get("MEDIAFORCE_SECRET")
            os.environ["MEDIAFORCE_SECRET"] = "must-not-leak"
            try:
                result = executor.run(command)
            finally:
                if previous is None:
                    os.environ.pop("MEDIAFORCE_SECRET", None)
                else:
                    os.environ["MEDIAFORCE_SECRET"] = previous
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload, {"cwd": str(self.output_root), "secret": None})
        self.assertEqual(executor.diagnostics[0].outcome, "completed")

    def test_buffered_run_enforces_stdout_limit_and_keeps_stderr_tail(self) -> None:
        script = "import sys;sys.stdout.write('x'*1024);sys.stderr.write('a'*64+'TAIL')"
        command = ("ffprobe", "-c", script)
        limits = AV1ValidationV3Tier1RuntimeLimits(
            max_stdout_bytes=32,
            max_stderr_bytes=16,
            max_stream_bytes=1024,
            chunk_bytes=64,
        )
        with self._executor(buffered_commands={command}, limits=limits) as executor:
            result = executor.run(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout), 32)
        self.assertEqual(executor.diagnostics[0].outcome, "stdout_limit")

    def test_buffered_run_bounds_stderr_and_preserves_its_tail(self) -> None:
        command = ("ffprobe", "-c", "import sys;sys.stderr.write('a'*64+'TAIL')")
        limits = AV1ValidationV3Tier1RuntimeLimits(
            max_stdout_bytes=1024,
            max_stderr_bytes=16,
            max_stream_bytes=1024,
            chunk_bytes=64,
        )
        with self._executor(buffered_commands={command}, limits=limits) as executor:
            result = executor.run(command)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stderr.startswith("[stderr truncated; tail follows]"))
        self.assertTrue(result.stderr.endswith("TAIL"))
        self.assertTrue(executor.diagnostics[0].stderr_truncated)

    def test_buffered_run_times_out_and_reaps_process(self) -> None:
        command = ("ffprobe", "-c", "import time;time.sleep(5)")
        limits = AV1ValidationV3Tier1RuntimeLimits(
            timeout_seconds=0.1,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
            max_stream_bytes=1024,
            chunk_bytes=64,
        )
        with self._executor(buffered_commands={command}, limits=limits) as executor:
            result = executor.run(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(executor.diagnostics[0].outcome, "timed_out")

    def test_buffered_run_times_out_when_child_closes_pipes_but_keeps_running(self) -> None:
        command = (
            "ffprobe",
            "-c",
            "import os,time;os.close(1);os.close(2);time.sleep(5)",
        )
        limits = AV1ValidationV3Tier1RuntimeLimits(
            timeout_seconds=0.1,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
            max_stream_bytes=1024,
            chunk_bytes=64,
        )
        with self._executor(buffered_commands={command}, limits=limits) as executor:
            result = executor.run(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(executor.diagnostics[0].outcome, "timed_out")

    def test_timeout_kills_the_child_process_group(self) -> None:
        pid_path = self.output_root / "grandchild.pid"
        heartbeat_path = self.output_root / "grandchild-heartbeat"
        grandchild_script = (
            "import sys,time;from pathlib import Path;path=Path(sys.argv[1]);"
            "exec(\"while True:\\n path.write_text(str(time.monotonic()))\\n time.sleep(0.02)\")"
        )
        script = (
            "import subprocess,sys,time;from pathlib import Path;"
            f"child=subprocess.Popen([sys.executable,'-c',{grandchild_script!r},sys.argv[2]]);"
            "Path(sys.argv[1]).write_text(str(child.pid));time.sleep(5)"
        )
        command = ("ffprobe", "-c", script, str(pid_path), str(heartbeat_path))
        limits = AV1ValidationV3Tier1RuntimeLimits(
            timeout_seconds=0.3,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
            max_stream_bytes=1024,
            chunk_bytes=64,
        )
        with self._executor(buffered_commands={command}, limits=limits) as executor:
            result = executor.run(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(pid_path.is_file())
        deadline = time.monotonic() + 1
        while not heartbeat_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(heartbeat_path.is_file())
        heartbeat = heartbeat_path.read_text()
        time.sleep(0.15)
        self.assertEqual(heartbeat_path.read_text(), heartbeat)

    def test_spawn_failure_returns_bounded_diagnostic_result(self) -> None:
        command = ("ffprobe", "-c", "pass")
        with self._executor(buffered_commands={command}) as executor:
            with patch(
                "mediaforce.tuning.av1_validation_v3_tier1_runtime.subprocess.Popen",
                side_effect=OSError("spawn failed"),
            ):
                result = executor.run(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("spawn failed", result.stderr)
        self.assertEqual(executor.diagnostics[0].outcome, "spawn_failed")

    def test_streaming_hash_counts_and_hashes_the_same_bytes(self) -> None:
        payload = b"streamed-content" * 1024
        script = f"import sys;sys.stdout.buffer.write({payload!r})"
        command = ("ffmpeg", "-c", script, "-")
        limits = AV1ValidationV3Tier1RuntimeLimits(
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
            max_stream_bytes=len(payload) + 1,
            chunk_bytes=257,
        )
        with self._executor(streaming_commands={command}, limits=limits) as executor:
            result = executor.run_streaming_sha256(command)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.byte_count, len(payload))
        self.assertEqual(
            result.content_sha256,
            f"sha256:{hashlib.sha256(payload).hexdigest()}",
        )

    def test_streaming_hash_enforces_byte_limit(self) -> None:
        command = ("ffmpeg", "-c", "import sys;sys.stdout.buffer.write(b'x'*4096)", "-")
        limits = AV1ValidationV3Tier1RuntimeLimits(
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
            max_stream_bytes=128,
            chunk_bytes=64,
        )
        with self._executor(streaming_commands={command}, limits=limits) as executor:
            result = executor.run_streaming_sha256(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertGreater(result.byte_count, 128)
        self.assertEqual(executor.diagnostics[0].outcome, "stream_limit")

    def test_command_paths_must_remain_inside_output_root(self) -> None:
        command = ("ffprobe", "/private/tmp/not-allowed")
        with self._executor(buffered_commands={command}) as executor:
            with self.assertRaisesRegex(AV1ValidationV3Tier1RuntimeError, "outside"):
                executor.run(command)

    def test_cleanup_removes_only_tracked_regular_outputs(self) -> None:
        output = self.output_root / "fixture.nut"
        script = "from pathlib import Path;import sys;Path(sys.argv[1]).write_bytes(b'fixture')"
        command = ("ffmpeg", "-c", script, str(output))
        with self._executor(buffered_commands={command}) as executor:
            result = executor.run(command)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(output.is_file())
            self.assertEqual(executor.cleanup(), ())
            self.assertFalse(output.exists())

    def test_cleanup_refuses_symlink_substitution(self) -> None:
        output = self.output_root / "fixture.nut"
        outside = self.runtime_root / "outside.nut"
        outside.write_bytes(b"outside")
        script = "from pathlib import Path;import sys;Path(sys.argv[1]).write_bytes(b'fixture')"
        command = ("ffmpeg", "-c", script, str(output))
        with self._executor(buffered_commands={command}) as executor:
            self.assertEqual(executor.run(command).returncode, 0)
            output.unlink()
            output.symlink_to(outside)
            self.assertEqual(executor.cleanup(), ("fixture.nut",))
        self.assertTrue(output.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_cleanup_refuses_regular_file_substitution(self) -> None:
        output = self.output_root / "fixture.nut"
        script = "from pathlib import Path;import sys;Path(sys.argv[1]).write_bytes(b'fixture')"
        command = ("ffmpeg", "-c", script, str(output))
        with self._executor(buffered_commands={command}) as executor:
            self.assertEqual(executor.run(command).returncode, 0)
            output.unlink()
            output.write_bytes(b"replacement")
            self.assertEqual(executor.cleanup(), ("fixture.nut",))
        self.assertEqual(output.read_bytes(), b"replacement")

    def test_runtime_rejects_output_root_that_contains_repository(self) -> None:
        with self.assertRaisesRegex(AV1ValidationV3Tier1RuntimeError, "outside"):
            with paused_av1_validation_v3_tier1_runtime(
                self.config,
                config_snapshot_bytes=self.config_snapshot_bytes,
                context=self.context,
                matrix=self.matrix,
                output_directory=Path("/"),
                repository_root=Path.cwd(),
                toolchain=self.toolchain,
            ):
                pass

    def test_runtime_module_has_no_private_media_or_evidence_imports(self) -> None:
        source = Path("mediaforce/tuning/av1_validation_v3_tier1_runtime.py").read_text()
        tree = ast.parse(source)
        imported = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        for forbidden in (
            "av1_validation_partition",
            "av1_validation_derivation",
            "av1_validation_harness",
            "av1_validation_v2",
            "mediaforce.core.db",
        ):
            self.assertFalse(any(forbidden in module for module in imported))

    def _executor(
        self,
        *,
        buffered_commands: set[tuple[str, ...]] | None = None,
        streaming_commands: set[tuple[str, ...]] | None = None,
        limits: AV1ValidationV3Tier1RuntimeLimits | None = None,
    ) -> _ExecutorContext:
        lock = exclusive_mediaforce_runtime_lock(
            self.config,
            owner_payload={"purpose": "tier1-runtime-executor-test"},
        )
        lease = lock.__enter__()
        executor = AV1ValidationV3Tier1PausedRuntimeExecutor(
            lease=lease,
            output_root=self.output_root,
            toolchain=self.toolchain,
            limits=limits or AV1ValidationV3Tier1RuntimeLimits(),
            buffered_commands=frozenset(buffered_commands or set()),
            streaming_commands=frozenset(streaming_commands or set()),
        )
        return _ExecutorContext(executor, lock)


class _ExecutorContext:
    def __init__(
        self,
        executor: AV1ValidationV3Tier1PausedRuntimeExecutor,
        lock: object,
    ) -> None:
        self.executor = executor
        self.lock = lock

    def __enter__(self) -> AV1ValidationV3Tier1PausedRuntimeExecutor:
        return self.executor

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.lock.__exit__(exc_type, exc, traceback)


def _execution_context(
    toolchain_sha256: str,
    config_sha256: str,
) -> AV1ValidationV3Tier1ExecutionContext:
    protocol = load_av1_validation_protocol_v3(PROTOCOL_PATH)
    plan = build_av1_validation_v3_qualification_plan(
        protocol=protocol,
        qualification_key_id=f"av1vqkey3_{'a' * 32}",
        eligibility_predicate_sha256=f"sha256:{'b' * 64}",
        repository_commit="1" * 40,
        repository_tree="2" * 40,
        config_sha256=config_sha256,
        toolchain_sha256=toolchain_sha256,
        fixture_matrix_sha256=AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
        frozen_at="2026-08-03T12:00:00Z",
        valid_until="2026-08-05T12:00:00Z",
    )
    request = build_av1_validation_v3_tier1_authorization_request(
        protocol=protocol,
        plan=plan,
        requested_at="2026-08-03T13:00:00Z",
        valid_until="2026-08-04T20:00:00Z",
    )
    grant = build_av1_validation_v3_tier1_execution_grant(
        protocol=protocol,
        plan=plan,
        request=request,
        owner_principal="owner-1234abcd",
        authorized_at="2026-08-03T14:00:00Z",
        valid_until="2026-08-04T19:00:00Z",
    )
    return AV1ValidationV3Tier1ExecutionContext(
        protocol=protocol,
        qualification_plan=plan,
        request=request,
        grant=grant,
        as_of="2026-08-04T18:00:00Z",
    )


def _compat_execution_context(
    toolchain_sha256: str,
    config_sha256: str,
) -> AV1ValidationV3Tier1CompatExecutionContext:
    protocol = load_av1_validation_protocol_v3(PROTOCOL_PATH)
    plan = build_av1_validation_v3_qualification_plan(
        protocol=protocol,
        qualification_key_id=f"av1vqkey3_{'a' * 32}",
        eligibility_predicate_sha256=f"sha256:{'b' * 64}",
        repository_commit="1" * 40,
        repository_tree="2" * 40,
        config_sha256=config_sha256,
        toolchain_sha256=toolchain_sha256,
        fixture_matrix_sha256=AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
        frozen_at="2026-08-03T12:00:00Z",
        valid_until="2026-08-05T12:00:00Z",
    )
    request = build_av1_validation_v3_tier1_compat_request(
        protocol=protocol,
        plan=plan,
        probe_matrix_sha256=AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SHA256,
        requested_at="2026-08-03T13:00:00Z",
        valid_until="2026-08-04T20:00:00Z",
    )
    grant = build_av1_validation_v3_tier1_compat_grant(
        protocol=protocol,
        plan=plan,
        request=request,
        owner_principal="owner-1234abcd",
        authorized_at="2026-08-03T14:00:00Z",
        valid_until="2026-08-04T19:00:00Z",
    )
    return AV1ValidationV3Tier1CompatExecutionContext(
        protocol=protocol,
        qualification_plan=plan,
        request=request,
        grant=grant,
        as_of="2026-08-04T18:00:00Z",
    )


def _residual_execution_context(
    toolchain_sha256: str,
    config_sha256: str,
) -> AV1ValidationV3Tier1ResidualExecutionContext:
    protocol = load_av1_validation_protocol_v3(PROTOCOL_PATH)
    plan = build_av1_validation_v3_qualification_plan(
        protocol=protocol,
        qualification_key_id=f"av1vqkey3_{'a' * 32}",
        eligibility_predicate_sha256=f"sha256:{'b' * 64}",
        repository_commit="1" * 40,
        repository_tree="2" * 40,
        config_sha256=config_sha256,
        toolchain_sha256=toolchain_sha256,
        fixture_matrix_sha256=AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
        frozen_at="2026-08-03T12:00:00Z",
        valid_until="2026-08-05T12:00:00Z",
    )
    request = build_av1_validation_v3_tier1_residual_request(
        protocol=protocol,
        plan=plan,
        probe_matrix_sha256=AV1_VALIDATION_V3_TIER1_RESIDUAL_MATRIX_SHA256,
        requested_at="2026-08-03T13:00:00Z",
        valid_until="2026-08-04T20:00:00Z",
    )
    grant = build_av1_validation_v3_tier1_residual_grant(
        protocol=protocol,
        plan=plan,
        request=request,
        owner_principal="owner-1234abcd",
        authorized_at="2026-08-03T14:00:00Z",
        valid_until="2026-08-04T19:00:00Z",
    )
    return AV1ValidationV3Tier1ResidualExecutionContext(
        protocol=protocol,
        qualification_plan=plan,
        request=request,
        grant=grant,
        as_of="2026-08-04T18:00:00Z",
    )
