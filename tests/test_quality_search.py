import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import ANY, Mock, patch

from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError, ScheduleWindowClosedError
from mediaforce.core.schedule_deadline import SCHEDULE_CLOSE_DEADLINE_KEY
from mediaforce.encoding.quality import (
    _cleanup_scoped_quality_temp_dir,
    CONTAINMENT_UNPROVEN_FAILURE_KIND,
    QualitySearchResult,
    QualitySearchWarmStart,
    QualityTempSetupError,
    REMOTE_QUALITY_CONTAINED_MARKER,
    REMOTE_QUALITY_CONTAINMENT_SCRIPT,
    REMOTE_QUALITY_CONTAINMENT_TIMEOUT_SECONDS,
    REMOTE_QUALITY_TEMP_FILES_LEFT_NOTE,
    REMOTE_QUALITY_TIMEOUT_FAILURE_KIND,
    REMOTE_QUALITY_TIMEOUT_SECONDS,
    RemoteQualityTimeoutError,
    SampleEncodeError,
    SampleEncodeResult,
    _probe_libvmaf,
    _quality_execution_mode,
    _run_quality_command,
    quality_error_message,
    quality_toolchain_identity,
    run_crf_search,
    run_sample_encode,
)
from mediaforce.encoding.quality_search import search_quality


class QualityToolchainIdentityTests(unittest.TestCase):
    def test_quality_execution_routes_explicit_self_ssh_locally(self) -> None:
        self.assertEqual(
            _quality_execution_mode({"mode": "ssh", "host": "cbusillo@localhost"}),
            "local",
        )

    def test_localhost_ssh_ab_av1_command_uses_trusted_local_transport(self) -> None:
        process_controller = ManagedProcessController()
        host = {"mode": "ssh", "host": "cbusillo@localhost"}
        command = ["ab-av1", "--version"]
        completed = subprocess.CompletedProcess(command, 0, "ab-av1 0.11.3\n", "")

        with (
            patch("mediaforce.encoding.quality.run_command") as local_command,
            patch(
                "mediaforce.encoding.quality.run_trusted_local_orchestrator_command",
                return_value=completed,
            ) as trusted_command,
            patch("mediaforce.encoding.quality.run_remote_command") as remote_command,
        ):
            result = _run_quality_command(
                command,
                process_controller=process_controller,
                host=host,
            )

        self.assertIs(result, completed)
        local_command.assert_not_called()
        trusted_command.assert_called_once_with(
            command,
            process_controller=process_controller,
            env=ANY,
        )
        remote_command.assert_not_called()

    def test_nonlocal_ssh_quality_command_uses_remote_transport(self) -> None:
        process_controller = ManagedProcessController()
        host = {"mode": "ssh", "host": "encoder@example.invalid"}
        command = ["ab-av1", "--version"]
        completed = subprocess.CompletedProcess(command, 0, "ab-av1 0.11.3\n", "")

        with (
            patch("mediaforce.encoding.quality.run_command") as local_command,
            patch("mediaforce.encoding.quality.run_trusted_local_orchestrator_command") as trusted_command,
            patch("mediaforce.encoding.quality.run_remote_command", return_value=completed) as remote_command,
        ):
            result = _run_quality_command(
                command,
                process_controller=process_controller,
                host=host,
            )

        self.assertIs(result, completed)
        local_command.assert_not_called()
        trusted_command.assert_not_called()
        remote_command.assert_called_once_with(
            host,
            command,
            REMOTE_QUALITY_TIMEOUT_SECONDS,
            process_controller=process_controller,
        )

    def test_libvmaf_probe_avoids_a_shell_fork_under_managed_containment(self) -> None:
        process_controller = ManagedProcessController()
        completed = subprocess.CompletedProcess(
            args=["ffmpeg", "-hide_banner", "-filters"],
            returncode=0,
            stdout=" T.. libvmaf\n",
            stderr="",
        )

        with patch("mediaforce.encoding.quality.run_command", return_value=completed) as run_command:
            available = _probe_libvmaf(process_controller=process_controller)

        self.assertTrue(available)
        run_command.assert_called_once_with(
            ["ffmpeg", "-hide_banner", "-filters"],
            check=True,
            capture_output=True,
            text=True,
            env=ANY,
            process_controller=process_controller,
        )

    def test_identity_is_stable_and_uses_version_output(self) -> None:
        results = [
            subprocess.CompletedProcess(
                args=["ab-av1", "--version"],
                returncode=0,
                stdout="ab-av1 0.11.3\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["ffmpeg", "-version"],
                returncode=0,
                stdout="ffmpeg version 8.0\nbuilt with Apple clang\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["ffmpeg", "-f", "lavfi"],
                returncode=0,
                stdout="",
                stderr="Svt[info]: SVT [version]: SVT-AV1 Encoder Lib v4.2.0\n",
            ),
            subprocess.CompletedProcess(
                args=["ffmpeg", "-h", "filter=libvmaf"],
                returncode=0,
                stdout="Filter libvmaf\nmodel option\n",
                stderr="",
            ),
        ]
        with patch("mediaforce.encoding.quality._run_quality_command", side_effect=results):
            identity = quality_toolchain_identity(quality_metric="VMAF")

        self.assertEqual(identity["status"], "available")
        self.assertEqual(identity["quality_tool_version"], "ab-av1 0.11.3")
        self.assertEqual(identity["encoder_version"], "SVT-AV1 Encoder Lib v4.2.0")
        self.assertEqual(identity["encoder_runtime_version"], "ffmpeg version 8.0")
        self.assertTrue(str(identity["encoder_runtime_signature_id"]).startswith("erti1_"))
        self.assertTrue(str(identity["metric_runtime_signature_id"]).startswith("qmri1_"))
        self.assertTrue(str(identity["signature_id"]).startswith("qti1_"))

    def test_failed_version_command_returns_unavailable_identity(self) -> None:
        with patch(
                "mediaforce.encoding.quality._run_quality_command",
                side_effect=[
                    subprocess.CompletedProcess(["ab-av1"], 0, "ab-av1 0.11.3\n", ""),
                    subprocess.CompletedProcess(["ffmpeg"], 1, "", "failed"),
                ],
        ):
            identity = quality_toolchain_identity(quality_metric="VMAF")

        self.assertEqual(identity, {
            "schema_version": 1,
            "status": "unavailable",
            "reason": "version_command_failed",
        })

    def test_cancellation_and_schedule_close_are_not_downgraded_to_unavailable(self) -> None:
        for error in (ProcessCancelledError("cancelled"), ScheduleWindowClosedError("closed")):
            with self.subTest(error=type(error).__name__):
                with patch("mediaforce.encoding.quality._run_quality_command", side_effect=error):
                    with self.assertRaises(type(error)):
                        quality_toolchain_identity(quality_metric="VMAF")


class QualitySearchWarmStartTests(unittest.TestCase):
    def test_quality_hint_uses_one_sample_with_the_existing_strict_constraints(self) -> None:
        run_search = Mock()
        run_sample = Mock(
            return_value=self._sample(score=85.1, predicted_encode_percent=50.0)
        )

        result = self._search(
            run_search,
            run_sample=run_sample,
            warm_start=QualitySearchWarmStart(30.0, 30, "qms1_test", "qsc1_test"),
        )

        self.assertEqual(result.crf, 30.0)
        run_search.assert_not_called()
        run_sample.assert_called_once()
        call = run_sample.call_args
        self.assertEqual(call.kwargs["preferred_metric"], "vmaf")
        self.assertEqual(call.kwargs["crf"], 30.0)
        self.assertEqual(call.kwargs["svt_params"], ["tune=0"])
        trace = result.quality_search_trace or {}
        self.assertEqual(trace["candidate_count"], 1)
        self.assertEqual(trace["search_max_crf"], 38)
        self.assertEqual(trace["warm_start"]["status"], "accepted")

    def test_quality_hint_miss_discards_probe_before_unchanged_fallback(self) -> None:
        direct_search = Mock(return_value=self._baseline_result())
        direct = self._search(direct_search)
        warmed_search = Mock(return_value=self._baseline_result())
        warm_sample = Mock(return_value=self._sample(score=83.0, predicted_encode_percent=40.0))

        warmed = self._search(
            warmed_search,
            run_sample=warm_sample,
            warm_start=QualitySearchWarmStart(32.0, 32, "qms1_test", "qsc1_test"),
        )

        self.assertEqual(warmed.crf, direct.crf)
        warm_sample.assert_called_once()
        warmed_search.assert_called_once()
        self.assertEqual(warmed_search.call_args.kwargs, direct_search.call_args.kwargs)
        trace = warmed.quality_search_trace or {}
        self.assertEqual(trace["warm_start"]["status"], "rejected_fallback")
        self.assertEqual(trace["warm_start"]["fallback_reason"], "quality_target_miss")
        self.assertEqual(trace["warm_start"]["baseline_candidate_count"], 1)
        self.assertEqual(trace["candidate_count"], 2)
        self.assertEqual(len(trace["attempts"]), 1)

    def test_quality_hint_over_size_cap_falls_back_to_unchanged_search(self) -> None:
        baseline_result = QualitySearchResult(
            crf=28.0,
            metric="VMAF",
            target=85.0,
            score=85.2,
            stdout="crf 28 VMAF 85.2 predicted video stream size 600 MiB (60%)",
        )
        run_search = Mock(return_value=baseline_result)

        result = self._search(
            run_search,
            run_sample=Mock(return_value=self._sample(score=86.0, predicted_encode_percent=81.0)),
            warm_start=QualitySearchWarmStart(30.0, 30, "qms1_test", "qsc1_test"),
        )

        run_search.assert_called_once()
        trace = result.quality_search_trace or {}
        self.assertEqual(trace["warm_start"]["status"], "rejected_fallback")
        self.assertEqual(trace["warm_start"]["fallback_reason"], "size_cap_miss")

    def test_invalid_quality_hint_is_not_measured(self) -> None:
        run_search = Mock(
            return_value=QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=85.2,
                stdout="crf 28 VMAF 85.2 predicted video stream size 600 MiB (60%)",
            )
        )

        run_sample = Mock()
        result = self._search(
            run_search,
            run_sample=run_sample,
            warm_start=QualitySearchWarmStart(50.0, 50, "qms1_test", "qsc1_test"),
        )

        run_sample.assert_not_called()
        run_search.assert_called_once()
        self.assertEqual(run_search.call_args.kwargs["min_crf"], 18)
        self.assertEqual(run_search.call_args.kwargs["max_crf"], 38)
        trace = result.quality_search_trace or {}
        self.assertEqual(trace["warm_start"]["status"], "guard_rejected")
        self.assertFalse(trace["warm_start"]["attempted"])

    def test_stale_quality_signature_is_rejected_before_measurement(self) -> None:
        run_search = Mock(
            return_value=QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=85.2,
                stdout="crf 28 VMAF 85.2 predicted video stream size 600 MiB (60%)",
            )
        )
        run_sample = Mock()

        result = self._search(
            run_search,
            run_sample=run_sample,
            warm_start=QualitySearchWarmStart(30.0, 30, "qms1_stale", "qsc1_test"),
        )

        run_sample.assert_not_called()
        run_search.assert_called_once()
        trace = result.quality_search_trace or {}
        self.assertEqual(trace["warm_start"]["status"], "guard_rejected")
        self.assertEqual(trace["warm_start"]["fallback_reason"], "invalid_hint_contract")

    def test_failed_launched_probe_counts_as_candidate_work_before_fallback(self) -> None:
        run_search = Mock(
            return_value=QualitySearchResult(
                crf=28.0,
                metric="VMAF",
                target=85.0,
                score=85.2,
                stdout="crf 28 VMAF 85.2 predicted video stream size 600 MiB (60%)",
            )
        )

        result = self._search(
            run_search,
            run_sample=Mock(side_effect=SampleEncodeError("probe failed")),
            warm_start=QualitySearchWarmStart(30.0, 30, "qms1_test", "qsc1_test"),
        )

        trace = result.quality_search_trace or {}
        self.assertEqual(trace["candidate_count"], 2)
        self.assertEqual(trace["warm_start"]["candidate_count"], 1)
        self.assertEqual(trace["warm_start"]["status"], "probe_error_fallback")

    def test_setup_failure_does_not_count_unlaunched_candidate_work(self) -> None:
        result = self._search(
            Mock(return_value=self._baseline_result()),
            run_sample=Mock(side_effect=QualityTempSetupError("temp unavailable")),
            warm_start=QualitySearchWarmStart(30.0, 30, "qms1_test", "qsc1_test"),
        )

        trace = result.quality_search_trace or {}
        self.assertEqual(trace["candidate_count"], 1)
        self.assertEqual(trace["warm_start"]["candidate_count"], 0)
        self.assertEqual(trace["warm_start"]["status"], "probe_error_fallback")

    def _search(
            self,
            run_search: Mock,
            *,
            run_sample: Mock | None = None,
            warm_start: QualitySearchWarmStart | None = None,
    ) -> QualitySearchResult:
        return search_quality(
            Path("/tmp/input.mkv"),
            self._policy(),
            source_codec="h264",
            width=1920,
            height=1080,
            host_media_access_for_host=Mock(return_value="mounted"),
            select_quality_metric=Mock(return_value=("vmaf", 95.0)),
            build_svt_params=Mock(return_value=["tune=0"]),
            effective_video_preset=Mock(return_value=4),
            run_crf_search=run_search,
            run_sample_encode=run_sample,
            warm_start=warm_start,
            expected_search_signature_id="qms1_test",
        )

    @staticmethod
    def _sample(*, score: float, predicted_encode_percent: float) -> SampleEncodeResult:
        return SampleEncodeResult(
            metric="VMAF",
            score=score,
            predicted_encode_percent=predicted_encode_percent,
            predicted_encode_seconds=12.0,
            predicted_encode_size_bytes=500_000_000,
            stdout="sample-encode",
        )

    @staticmethod
    def _baseline_result() -> QualitySearchResult:
        return QualitySearchResult(
            crf=28.0,
            metric="VMAF",
            target=85.0,
            score=85.2,
            stdout="crf 28 VMAF 85.2 predicted video stream size 600 MiB (60%)",
        )

    @staticmethod
    def _policy() -> dict[str, object]:
        return {
            "encoder": "libsvtav1",
            "quality_metric": "vmaf",
            "target_vmaf": 85.0,
            "min_target_vmaf": 80.0,
            "target_relax_step_vmaf": 0.5,
            "pixel_format": "yuv420p10le",
            "sample_every": "8m",
            "sample_duration": "20s",
            "min_crf": 18,
            "max_crf": 38,
            "max_encoded_percent": 80,
        }


class RemoteQualityTimeoutTests(unittest.TestCase):
    HOST = {
        "mode": "ssh", "host": "encoder@example.invalid", "key": "remote-a", "label": "Remote A",
        SCHEDULE_CLOSE_DEADLINE_KEY: "2026-09-30T23:00:00+00:00",
    }
    PATH_EXPORT = "export PATH=/opt/homebrew/bin:$PATH"
    CONTAINED = subprocess.CompletedProcess(["ssh"], 0, f"{REMOTE_QUALITY_CONTAINED_MARKER}\n", "")

    def _timeout(self, cmd: list[str]) -> subprocess.TimeoutExpired:
        script = f"{self.PATH_EXPORT}\n{' '.join(cmd)}"
        return subprocess.TimeoutExpired(
            ["ssh", "-o", "BatchMode=yes", "encoder@example.invalid", "sh", "-lc", script],
            REMOTE_QUALITY_TIMEOUT_SECONDS,
            output="crf 30 VMAF 94.1\n",
            stderr=f"{self.PATH_EXPORT}\nEncoding sample 3/5\rEncoding sample 4/5\n",
        )

    def _run_with_quality_failure(
            self,
            run: str,
            failure: Exception | None,
            *,
            containment: subprocess.CompletedProcess[str] | Exception = CONTAINED,
            cleanup: subprocess.CompletedProcess[str] | None = None,
            quality_temp_dir: Path | None = Path("/remote/quality-temp"),
    ) -> tuple[Exception, list[tuple[dict[str, object], list[str], int]]]:
        """Run one quality phase over SSH whose ab-av1 step raises ``failure``, or times out when it is None.

        ``containment`` is what the stop step on that computer returns or raises.
        """
        calls: list[tuple[dict[str, object], list[str], int]] = []

        def fake_remote(host: dict[str, object], cmd: list[str], timeout: int, **_kwargs: object) -> object:
            calls.append((host, cmd, timeout))
            if cmd[0] == "ab-av1":
                raise failure or self._timeout(cmd)
            if cmd[:2] == ["sh", "-c"]:
                if isinstance(containment, Exception):
                    raise containment
                return containment
            if cmd[0] == "rm" and cleanup is not None:
                return cleanup
            return subprocess.CompletedProcess(cmd, 0, "", "")

        common = {
            "preferred_metric": "xpsnr",
            "preset": 4,
            "pixel_format": "yuv420p10le",
            "sample_every": "12m",
            "sample_duration": "20s",
            "svt_params": [],
            # A fresh copy: the temp-folder cleanup drops the schedule deadline from the host it is given.
            "host": dict(self.HOST),
            "quality_temp_dir": quality_temp_dir,
        }
        with patch("mediaforce.encoding.quality.run_remote_command", side_effect=fake_remote):
            try:
                if run == "crf_search":
                    run_crf_search(
                        Path("/remote/input.mkv"), metric_target=41.0, min_crf=20, max_crf=35,
                        max_encoded_percent=70, thorough=False, **common,
                    )
                else:
                    run_sample_encode(Path("/remote/input.mkv"), crf=28.0, **common)
            except Exception as exc:  # noqa: BLE001 - the test inspects whichever error the run raised.
                return exc, calls
        self.fail("the quality run should have failed")

    def _assert_plain(self, exc: RemoteQualityTimeoutError) -> None:
        message = quality_error_message(exc)
        self.assertIn(self.HOST["label"], message)
        for raw in ("ssh", "export PATH", "ab-av1", "timed out after", str(REMOTE_QUALITY_TIMEOUT_SECONDS)):
            self.assertNotIn(raw, message)

    def test_timed_out_run_that_is_stopped_on_that_computer_is_contained_and_cleaned_up(self) -> None:
        for phase in ("crf_search", "sample_encode"):
            with self.subTest(phase=phase):
                exc, calls = self._run_with_quality_failure(phase, None)

                assert isinstance(exc, RemoteQualityTimeoutError)
                self.assertTrue(exc.remote_process_contained)
                self.assertEqual(exc.failure_kind, REMOTE_QUALITY_TIMEOUT_FAILURE_KIND)
                self.assertEqual(exc.phase, phase)
                self.assertEqual(exc.timeout_seconds, REMOTE_QUALITY_TIMEOUT_SECONDS)
                self.assertEqual(exc.host_key, self.HOST["key"])
                self._assert_plain(exc)
                self.assertNotIn("not confirmed", str(exc))
                assert exc.output_tail is not None
                self.assertIn("Encoding sample 4/5", exc.output_tail)
                self.assertNotIn("export PATH", exc.output_tail)
                temp_dir = next(cmd[2] for _, cmd, _ in calls if cmd[0] == "mkdir")
                containment_host, containment_cmd, containment_timeout = next(
                    call for call in calls if call[1][:2] == ["sh", "-c"]
                )
                self.assertEqual(containment_cmd, ["sh", "-c", REMOTE_QUALITY_CONTAINMENT_SCRIPT, "sh", temp_dir])
                self.assertEqual(containment_timeout, REMOTE_QUALITY_CONTAINMENT_TIMEOUT_SECONDS)
                self.assertNotIn(SCHEDULE_CLOSE_DEADLINE_KEY, containment_host)
                self.assertEqual(calls[-1][1], ["rm", "-rf", temp_dir])

    def test_temp_folder_left_after_a_stopped_run_is_told_in_plain_words(self) -> None:
        cleanup = subprocess.CompletedProcess(
            ["ssh"], 1, "", "rm: /remote/quality-temp/.mediaforce-ab-av1-x: Permission denied\nssh: exit status 1",
        )

        exc, _ = self._run_with_quality_failure("crf_search", None, cleanup=cleanup)

        assert isinstance(exc, RemoteQualityTimeoutError)
        self.assertTrue(exc.remote_process_contained)
        message = quality_error_message(exc)
        self.assertTrue(message.endswith(REMOTE_QUALITY_TEMP_FILES_LEFT_NOTE), message)
        for raw in ("/remote/quality-temp", "Permission denied", "ssh", "rm:"):
            self.assertNotIn(raw, message)
        self.assertIn("Permission denied", exc.temp_cleanup_error or "")

    def test_timed_out_run_not_shown_stopped_is_unproven_and_keeps_its_temp_folder(self) -> None:
        cases = {
            "stop step timed out": subprocess.TimeoutExpired(["ssh"], REMOTE_QUALITY_CONTAINMENT_TIMEOUT_SECONDS),
            "stop step could not connect": RuntimeError("ssh: connect to host: Connection refused"),
            "processes survived": subprocess.CompletedProcess(["ssh"], 1, "", ""),
            "no confirmation printed": subprocess.CompletedProcess(["ssh"], 0, "", ""),
        }
        for label, containment in cases.items():
            for phase in ("crf_search", "sample_encode"):
                with self.subTest(label, phase=phase):
                    exc, calls = self._run_with_quality_failure(phase, None, containment=containment)

                    assert isinstance(exc, RemoteQualityTimeoutError)
                    self.assertFalse(exc.remote_process_contained)
                    self.assertEqual(exc.failure_kind, CONTAINMENT_UNPROVEN_FAILURE_KIND)
                    self._assert_plain(exc)
                    self.assertIn("not confirmed stopped", str(exc))
                    # The run on that computer may still be using its temp folder, so it is not removed.
                    self.assertNotIn("rm", [cmd[0] for _, cmd, _ in calls])

    def test_timed_out_run_without_its_own_temp_folder_cannot_be_shown_stopped(self) -> None:
        exc, calls = self._run_with_quality_failure("crf_search", None, quality_temp_dir=None)

        assert isinstance(exc, RemoteQualityTimeoutError)
        self.assertFalse(exc.remote_process_contained)
        self.assertEqual(exc.failure_kind, CONTAINMENT_UNPROVEN_FAILURE_KIND)
        self.assertEqual([cmd[0] for _, cmd, _ in calls], ["ab-av1"])

    def test_stop_step_reaches_that_computer_with_the_temp_folder_as_one_argument(self) -> None:
        temp_root = Path("/remote/it's a \"quality\" $(temp) `run`")
        scripts: list[str] = []

        def fake_ssh(_host: dict[str, object], *remote_args: str, **_kwargs: object) -> object:
            scripts.append(remote_args[-1])
            if "ab-av1 sample-encode" in remote_args[-1]:
                raise subprocess.TimeoutExpired(["ssh"], REMOTE_QUALITY_TIMEOUT_SECONDS)
            return self.CONTAINED

        with patch("mediaforce.remote._run_remote_ssh", side_effect=fake_ssh):
            exc = self._run_sample_encode_through_transport(temp_root)

        assert isinstance(exc, RemoteQualityTimeoutError)
        self.assertTrue(exc.remote_process_contained)
        containment = next(shlex.split(script.split("\n", 1)[1]) for script in scripts if REMOTE_QUALITY_CONTAINED_MARKER in script)
        self.assertEqual(containment[:4], ["sh", "-c", REMOTE_QUALITY_CONTAINMENT_SCRIPT, "sh"])
        self.assertEqual(Path(containment[4]).parent, temp_root)
        self.assertEqual(len(containment), 5)

    def _run_sample_encode_through_transport(self, temp_root: Path) -> Exception:
        try:
            run_sample_encode(
                Path("/remote/input.mkv"), crf=28.0, preferred_metric="xpsnr", preset=4,
                pixel_format="yuv420p10le", sample_every="12m", sample_duration="20s", svt_params=[],
                host={key: value for key, value in self.HOST.items() if key != SCHEDULE_CLOSE_DEADLINE_KEY},
                quality_temp_dir=temp_root,
            )
        except Exception as exc:  # noqa: BLE001 - the test inspects whichever error the run raised.
            return exc
        self.fail("the quality run should have failed")

    def test_other_remote_quality_failures_still_remove_the_temp_folder(self) -> None:
        for phase in ("crf_search", "sample_encode"):
            with self.subTest(phase=phase):
                exc, calls = self._run_with_quality_failure(phase, RuntimeError("connection lost"))

                self.assertNotIsInstance(exc, RemoteQualityTimeoutError)
                temp_dir = next(cmd[2] for _, cmd, _ in calls if cmd[0] == "mkdir")
                self.assertEqual(calls[-1][1], ["rm", "-rf", temp_dir])
                self.assertNotIn(["sh", "-c"], [cmd[:2] for _, cmd, _ in calls])

    def test_removing_the_remote_temp_folder_keeps_the_callers_schedule_deadline(self) -> None:
        host = dict(self.HOST)
        with patch(
                "mediaforce.encoding.quality.run_remote_command",
                return_value=subprocess.CompletedProcess(["ssh"], 0, "", ""),
        ) as run_remote:
            cleanup_error = _cleanup_scoped_quality_temp_dir(Path("/remote/quality/.mediaforce-ab-av1-x"), host=host)

        self.assertIsNone(cleanup_error)
        self.assertNotIn(SCHEDULE_CLOSE_DEADLINE_KEY, run_remote.call_args.args[0])
        self.assertEqual(host, self.HOST)


def _process_stopped(pid: int) -> bool:
    """Gone, or exited and waiting to be reaped: the same test the stop step uses."""
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return not state or state.startswith("Z")


def _wait_until(condition: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class RemoteQualityContainmentScriptTests(unittest.TestCase):
    """Run the stop step in a real shell against processes this test starts; it needs no SSH.

    Every fixture writes a ready file once it is set up and keeps its identifying argument in its
    own command line, and the test waits for all of them before stopping anything.
    """

    # Ignores the polite stop, so only the forced one ends it; it does not name the run's folder.
    STUBBORN_CHILD = (
        "import pathlib, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(300)"
    )
    # Names the run's folder, starts a stubborn child in its own session that does not name it, and
    # dies on the polite stop, so the child is re-parented before anything looks for it again.
    RUN = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {STUBBORN_CHILD!r}, sys.argv[2] + '.child'], "
        "start_new_session=True); "
        "ready = pathlib.Path(sys.argv[2]); partial = ready.with_suffix('.partial'); "
        "partial.write_text(str(child.pid)); partial.rename(ready); time.sleep(300)"
    )
    # Names its folder as the argument after the script and loops, so the shell cannot exec away the argument.
    SHELL_RUN = 'trap "" TERM; : > "$2"; while :; do sleep 1; done'
    SHELL_BYSTANDER = ': > "$2"; while :; do sleep 1; done'

    def setUp(self) -> None:
        self.folder = f"/remote/it's a \"quality\" $(temp)/.mediaforce-ab-av1-{uuid.uuid4().hex}"
        ready_dir = tempfile.TemporaryDirectory()
        self.addCleanup(ready_dir.cleanup)
        self.ready_dir = Path(ready_dir.name)
        self.started: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        for process in self.started:
            process.kill()
            process.wait(timeout=10)

    def _stop(self, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", "-c", REMOTE_QUALITY_CONTAINMENT_SCRIPT, "sh", self.folder],
            capture_output=True, text=True, start_new_session=True, env=env, timeout=60,
        )

    def _start(self, name: str, argv: list[str]) -> Path:
        ready = self.ready_dir / name
        self.started.append(subprocess.Popen([*argv, str(ready)], start_new_session=True, text=True))
        return ready

    def test_stops_the_run_and_its_descendants_and_leaves_other_folders_alone(self) -> None:
        run_ready = self._start("run", [sys.executable, "-c", self.RUN, self.folder])
        stubborn_ready = self._start("stubborn", ["sh", "-c", self.SHELL_RUN, "sh", self.folder])
        bystander_readies = [
            self._start(f"bystander-{index}", ["sh", "-c", self.SHELL_BYSTANDER, "sh", folder])
            for index, folder in enumerate((f"{self.folder}0", f"{self.folder}-x"))
        ]
        child_ready = Path(f"{run_ready}.child")
        readies = [run_ready, stubborn_ready, child_ready, *bystander_readies]
        self.assertTrue(_wait_until(lambda: all(ready.exists() for ready in readies)), "fixtures never became ready")
        child_pid = int(run_ready.read_text())
        run, stubborn, *bystanders = self.started

        result = self._stop()

        try:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(REMOTE_QUALITY_CONTAINED_MARKER, result.stdout.split())
            for process in (run, stubborn):
                self.assertTrue(_wait_until(lambda: _process_stopped(process.pid)))
            self.assertTrue(_wait_until(lambda: _process_stopped(child_pid)))
            for bystander in bystanders:
                self.assertIsNone(bystander.poll())
        finally:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_a_process_listing_that_fails_or_is_empty_is_never_proof(self) -> None:
        real_ps = shutil.which("ps")
        assert real_ps is not None
        cases = {
            "fails": "exit 1",
            "is empty": "exit 0",
            # The first listing works, so only the later check can catch it.
            "fails when checked": f'[ -e "$0.used" ] && exit 1; touch "$0.used"; exec {real_ps} "$@"',
            "is empty when checked": f'[ -e "$0.used" ] && exit 0; touch "$0.used"; exec {real_ps} "$@"',
        }
        for label, fake_ps in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tools:
                ps = Path(tools) / "ps"
                ps.write_text(f"#!/bin/sh\n{fake_ps}\n")
                ps.chmod(0o755)

                result = self._stop({**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"})

                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(REMOTE_QUALITY_CONTAINED_MARKER, result.stdout)


if __name__ == "__main__":
    unittest.main()
