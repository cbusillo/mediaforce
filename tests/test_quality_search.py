import subprocess
import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError, ScheduleWindowClosedError
from mediaforce.encoding.quality import (
    QualitySearchResult,
    QualitySearchWarmStart,
    QualityTempSetupError,
    REMOTE_QUALITY_TIMEOUT_SECONDS,
    SampleEncodeError,
    SampleEncodeResult,
    _probe_libvmaf,
    _quality_execution_mode,
    _run_quality_command,
    quality_toolchain_identity,
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


if __name__ == "__main__":
    unittest.main()
