import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from mediaforce.advisor import request_operator_note_parse
from mediaforce.advising.evals import recommended_eval_cases
from mediaforce.advising.privacy import advisor_evidence_references, sanitize_advisor_payload
from mediaforce.advising.routing import (
    AdvisorModelPricing,
    AdvisorTask,
    advisor_routing_for_models,
    advisor_routing_from_config,
)
from mediaforce.advising.runtime import (
    StructuredLLMFailure,
    _codex_lab_output,
    run_codex_lab_process,
    run_structured_llm_request,
)
from mediaforce.advising.telemetry import append_advisor_telemetry, estimated_cost_usd
from mediaforce.core.config import ConfigPaths, MediaforceConfig


def _jsonl(final_text: str, *, item_type: str = "agent_message", completed: bool = True) -> str:
    events: list[dict[str, Any]] = [
        {"type": "thread.started", "thread_id": "test-thread"},
        {
            "type": "item.completed",
            "item": {"id": "test-item", "type": item_type, "text": final_text},
        },
    ]
    if completed:
        events.append(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 30},
            }
        )
    return "\n".join(json.dumps(event, separators=(",", ":")) for event in events)


def _load_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


class AdvisorRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_config_resolves_task_routes_telemetry_and_optional_pricing(self) -> None:
        config = MediaforceConfig(
            raw={
                "advisor": {
                    "command": "custom-codex-lab",
                    "auth_profile": "mediaforce",
                    "telemetry_max_records": 250,
                    "routes": {
                        "operator_note_parse": {"models": ["test-luna", "test-terra", "test-terra"]},
                    },
                    "model_pricing": {
                        "test-luna": {
                            "input_usd_per_million": 1.0,
                            "cached_input_usd_per_million": 0.25,
                            "output_usd_per_million": 4.0,
                        }
                    },
                }
            },
            paths=self._paths(),
        )

        routing = advisor_routing_from_config(config)

        self.assertEqual(routing.command, "custom-codex-lab")
        self.assertEqual(routing.auth_profile, "mediaforce")
        self.assertEqual(routing.telemetry_max_records, 250)
        self.assertEqual(
            routing.route_for(AdvisorTask.OPERATOR_NOTE_PARSE).models,
            ("test-luna", "test-terra"),
        )
        self.assertEqual(
            routing.route_for(AdvisorTask.SEED_POLICY).models,
            ("gpt-5.6-terra", "gpt-5.6-sol"),
        )
        self.assertEqual(routing.telemetry_path, self.root / "web" / "advisor-routing.jsonl")
        self.assertEqual(routing.pricing_for("test-luna").output_usd_per_million, 4.0)

    def test_low_risk_fallback_records_reason_without_retaining_prompt(self) -> None:
        telemetry_path = self.root / "telemetry.jsonl"
        routing = advisor_routing_for_models(
            {AdvisorTask.OPERATOR_NOTE_PARSE: ("test-luna", "test-terra")},
            telemetry_path=telemetry_path,
        )
        commands: list[list[str]] = []
        inputs: list[str] = []
        valid_response = {
            "summary": "No executable tuning request was present.",
            "intent_type": "other",
            "request_type": "none",
            "operator_confirmed": False,
            "measured_size_followup": False,
            "evidence_authority": "none",
            "metric": None,
            "metric_target": None,
            "size_budget_value": None,
            "size_budget_unit": None,
            "scale_height": None,
            "black_bar_handling": None,
            "crop": None,
            "reasoning_note": "The note only supplied private context.",
        }

        def fake_run(cmd: list[str], **kwargs: Any) -> object:
            commands.append(cmd)
            inputs.append(str(kwargs.get("input") or ""))
            model = cmd[cmd.index("--model") + 1]
            stdout = _jsonl("not-json" if model == "test-luna" else json.dumps(valid_response))
            return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

        from unittest.mock import patch

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_operator_note_parse(
                project_root=self.root,
                payload={
                    "operator_note": "Look at /Users/alice/Videos/private.mkv and ask alice@example.com.",
                    "goal": "Classify only the supplied note.",
                },
                routing=routing,
            )

        self.assertIsNotNone(response)
        self.assertEqual([cmd[cmd.index("--model") + 1] for cmd in commands], ["test-luna", "test-terra"])
        self.assertTrue(all("gpt-5.6-sol" not in cmd for cmd in commands))
        self.assertTrue(all("/Users/alice" not in prompt for prompt in inputs))
        self.assertTrue(all("alice@example.com" not in prompt for prompt in inputs))
        self.assertTrue(all("[redacted-path]" in prompt for prompt in inputs))
        records = self._telemetry(telemetry_path)
        self.assertEqual([record["status"] for record in records], ["invalid_structured_output", "success"])
        self.assertIsNone(records[0]["fallback_reason"])
        self.assertEqual(records[1]["fallback_reason"], "invalid_structured_output")
        self.assertEqual(records[1]["evidence_references"], ["goal"])
        retained = telemetry_path.read_text()
        self.assertNotIn("private.mkv", retained)
        self.assertNotIn("alice@example.com", retained)
        self.assertNotIn("Look at", retained)

    def test_missing_image_is_nonretryable_and_path_is_redacted(self) -> None:
        telemetry_path = self.root / "telemetry.jsonl"
        routing = advisor_routing_for_models(
            {AdvisorTask.REVIEW_ARTIFACT_CRITIQUE: ("test-terra", "test-sol")},
            telemetry_path=telemetry_path,
        )
        run_calls = 0

        def fake_run(*_args: Any, **_kwargs: Any) -> object:
            nonlocal run_calls
            run_calls += 1
            raise AssertionError("subprocess should not start for a missing image")

        result = run_structured_llm_request(
            project_root=self.root,
            developer="Return JSON.",
            message="Review the supplied artifact.",
            schema={"type": "object"},
            max_seconds=10,
            task=AdvisorTask.REVIEW_ARTIFACT_CRITIQUE,
            prompt_version="test-v1",
            routing=routing,
            evidence_references=["multimodal_review_pack.image_count"],
            subprocess_run=fake_run,
            try_load_json=_load_json,
            images=[str(self.root / "private" / "missing.png")],
        )

        self.assertIsInstance(result, StructuredLLMFailure)
        self.assertEqual(run_calls, 0)
        records = self._telemetry(telemetry_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "missing_image")
        self.assertNotIn(str(self.root), telemetry_path.read_text())

    def test_unavailable_command_stops_without_trying_another_model(self) -> None:
        telemetry_path = self.root / "telemetry.jsonl"
        routing = advisor_routing_for_models(
            {AdvisorTask.SEED_POLICY: ("test-terra", "test-sol")},
            telemetry_path=telemetry_path,
        )
        run_calls = 0

        def fake_run(*_args: Any, **_kwargs: Any) -> object:
            nonlocal run_calls
            run_calls += 1
            raise FileNotFoundError("/Users/alice/bin/codex-lab")

        result = run_structured_llm_request(
            project_root=self.root,
            developer="Return JSON.",
            message="Draft one policy.",
            schema={"type": "object"},
            max_seconds=10,
            task=AdvisorTask.SEED_POLICY,
            prompt_version="test-v1",
            routing=routing,
            evidence_references=["base_policy"],
            subprocess_run=fake_run,
            try_load_json=_load_json,
        )

        self.assertIsInstance(result, StructuredLLMFailure)
        self.assertEqual(run_calls, 1)
        records = self._telemetry(telemetry_path)
        self.assertEqual([record["status"] for record in records], ["command_unavailable"])
        self.assertNotIn("/Users/alice", telemetry_path.read_text())

    def test_provider_failures_exhaust_only_the_configured_models(self) -> None:
        telemetry_path = self.root / "telemetry.jsonl"
        routing = advisor_routing_for_models(
            {AdvisorTask.SEED_POLICY: ("test-terra", "test-sol")},
            telemetry_path=telemetry_path,
        )
        models: list[str] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> object:
            models.append(cmd[cmd.index("--model") + 1])
            return type(
                "Result",
                (),
                {"returncode": 1, "stdout": "", "stderr": "requested model is unavailable"},
            )()

        result = run_structured_llm_request(
            project_root=self.root,
            developer="Return JSON.",
            message="Draft one policy.",
            schema={"type": "object"},
            max_seconds=10,
            task=AdvisorTask.SEED_POLICY,
            prompt_version="test-v1",
            routing=routing,
            evidence_references=["base_policy"],
            subprocess_run=fake_run,
            try_load_json=_load_json,
        )

        self.assertIsInstance(result, StructuredLLMFailure)
        self.assertEqual(models, ["test-terra", "test-sol"])
        records = self._telemetry(telemetry_path)
        self.assertEqual([record["status"] for record in records], ["provider_error", "provider_error"])
        self.assertEqual(records[1]["fallback_reason"], "provider_error")

    def test_image_command_uses_isolated_generic_copy(self) -> None:
        source_image = self.root / "private-title.png"
        source_image.write_bytes(b"not-a-real-png-but-copyable")
        telemetry_path = self.root / "telemetry.jsonl"
        routing = advisor_routing_for_models(
            {AdvisorTask.REVIEW_ARTIFACT_CRITIQUE: ("test-terra",)},
            telemetry_path=telemetry_path,
        )
        captured_image: Path | None = None

        def fake_run(cmd: list[str], **_kwargs: Any) -> object:
            nonlocal captured_image
            captured_image = Path(cmd[cmd.index("--image") + 1])
            self.assertTrue(captured_image.is_file())
            self.assertEqual(captured_image.name, "review-image-1.png")
            self.assertNotIn(str(source_image), cmd)
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": _jsonl(json.dumps({"ok": True})), "stderr": ""},
            )()

        result = run_structured_llm_request(
            project_root=self.root,
            developer="Return JSON.",
            message="Review the supplied artifact.",
            schema={"type": "object"},
            max_seconds=10,
            task=AdvisorTask.REVIEW_ARTIFACT_CRITIQUE,
            prompt_version="test-v1",
            routing=routing,
            evidence_references=["multimodal_review_pack.image_count"],
            subprocess_run=fake_run,
            try_load_json=_load_json,
            images=[str(source_image)],
        )

        self.assertEqual(result, {"ok": True})
        self.assertIsNotNone(captured_image)
        self.assertFalse(captured_image.exists())
        self.assertEqual(self._telemetry(telemetry_path)[0]["image_count"], 1)

    def test_jsonl_parser_rejects_raw_or_incomplete_output_and_tool_use(self) -> None:
        self.assertEqual(_codex_lab_output('{"ok":true}'), ("", {}, False, False))
        text, usage, tool_used, completed = _codex_lab_output(_jsonl("{}", completed=False))
        self.assertEqual(text, "{}")
        self.assertEqual(usage, {})
        self.assertFalse(tool_used)
        self.assertFalse(completed)
        _text, _usage, tool_used, completed = _codex_lab_output(
            _jsonl("", item_type="command_execution")
        )
        self.assertTrue(tool_used)
        self.assertTrue(completed)

    def test_process_timeout_terminates_spawned_process_group(self) -> None:
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            run_codex_lab_process(
                ["sh", "-c", "sleep 30 & echo $!; wait"],
                input="",
                capture_output=True,
                text=True,
                timeout=0.1,
                cwd=self.root,
            )

        self.assertLess(time.monotonic() - started, 3.0)
        child_pid = int(str(raised.exception.output).strip().splitlines()[0])
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail(f"timed-out child process {child_pid} was still running")

    def test_payload_sanitization_and_evidence_references_do_not_expose_private_values(self) -> None:
        payload = {
            "operator_note": "Inspect file:///Users/alice/private.mkv and email alice@example.com.",
            "source_path": "/Users/alice/private.mkv",
            "sample_item": {
                "rel_path": "tv/private/episode.mkv",
                "duration_seconds": 2700,
                "source_size_bytes": 2_000_000_000,
            },
            "multimodal_review_pack": {"images": ["/Users/alice/frame.png"]},
        }

        sanitized = sanitize_advisor_payload(payload)
        references = advisor_evidence_references(payload)

        serialized = json.dumps(sanitized, sort_keys=True)
        self.assertNotIn("/Users/alice", serialized)
        self.assertNotIn("alice@example.com", serialized)
        self.assertNotIn("rel_path", serialized)
        self.assertNotIn("source_path", serialized)
        self.assertIn("[redacted-path]", serialized)
        self.assertIn("[redacted-email]", serialized)
        self.assertEqual(
            references,
            [
                "sample_item.duration_seconds",
                "sample_item.source_size_bytes",
                "multimodal_review_pack.image_count",
            ],
        )

    def test_telemetry_is_bounded_redacted_and_can_estimate_configured_cost(self) -> None:
        path = self.root / "telemetry.jsonl"
        for index in range(3):
            append_advisor_telemetry(
                path,
                {
                    "index": index,
                    "diagnostic": f"/Users/alice/private-{index}.mkv",
                    "operator_note": "private operator text",
                    "prompt": "private prompt",
                },
                max_records=2,
            )

        records = self._telemetry(path)
        self.assertEqual([record["index"] for record in records], [1, 2])
        self.assertNotIn("/Users/alice", path.read_text())
        self.assertNotIn("private operator text", path.read_text())
        self.assertNotIn("private prompt", path.read_text())
        self.assertEqual(
            estimated_cost_usd(
                {"input_tokens": 1_000_000, "cached_input_tokens": 500_000, "output_tokens": 250_000},
                AdvisorModelPricing(
                    input_usd_per_million=2.0,
                    cached_input_usd_per_million=0.5,
                    output_usd_per_million=8.0,
                ),
            ),
            3.25,
        )

    def test_eval_corpus_covers_every_routing_tier_without_machine_local_media(self) -> None:
        cases = recommended_eval_cases()
        models = {case.model for case in cases if case.model is not None}
        tasks = {case.task for case in cases}
        serialized = json.dumps([case.payload for case in cases], sort_keys=True)

        self.assertEqual(models, {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"})
        self.assertTrue(
            all(
                case.fallback_models == ("gpt-5.6-terra",)
                for case in cases
                if case.model == "gpt-5.6-luna"
            )
        )
        self.assertTrue(
            all(
                case.fallback_models == ("gpt-5.6-sol",)
                for case in cases
                if case.model == "gpt-5.6-terra"
            )
        )
        self.assertTrue(
            all(not case.fallback_models for case in cases if case.model == "gpt-5.6-sol")
        )
        self.assertTrue(
            {
                AdvisorTask.OPERATOR_NOTE_PARSE,
                AdvisorTask.SEED_POLICY,
                AdvisorTask.NOTE_TUNING,
                AdvisorTask.REVIEW_ARTIFACT_CRITIQUE,
                "run_verdict",
            }.issubset(tasks)
        )
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("/Volumes/", serialized)
        self.assertNotIn("source_path", serialized)
        self.assertNotIn("rel_path", serialized)

    def _paths(self) -> ConfigPaths:
        return ConfigPaths(
            project_root=self.root,
            config_path=self.root / "config.toml",
            db_path=self.root / "library.sqlite3",
            run_manifest_dir=self.root / "runs",
            web_state_dir=self.root / "web",
            review_dir=self.root / "review",
            runtime_settings_path=self.root / "runtime.json",
        )

    @staticmethod
    def _telemetry(path: Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()
