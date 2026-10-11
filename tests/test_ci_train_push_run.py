from __future__ import annotations

import io
import json
from typing import Any

import pytest

from scripts import ci_train_push_run
from scripts.ci_train_push_run import push_run_state

REPOSITORY = "cbusillo/mediaforce"
TRAIN_REF = "launchplane/train/cbusillo/mediaforce/main/merge-train-batch-example"
HEAD_SHA = "2a10dc7e31320e42684eedc64804e5f9f5b943b1"
WORKFLOW_PATH = ".github/workflows/ci.yml"


def _run(**overrides: Any) -> dict[str, Any]:
    run: dict[str, Any] = {
        "event": "push",
        "head_sha": HEAD_SHA,
        "head_branch": TRAIN_REF,
        "path": WORKFLOW_PATH,
        "head_repository": {"full_name": REPOSITORY},
        "status": "completed",
        "conclusion": "success",
    }
    run.update(overrides)
    return run


def _state(runs: list[dict[str, Any]], **overrides: str) -> str:
    arguments = {
        "repository": REPOSITORY,
        "head_repository": REPOSITORY,
        "head_ref": TRAIN_REF,
        "head_sha": HEAD_SHA,
        "workflow_path": WORKFLOW_PATH,
    }
    arguments.update(overrides)
    return push_run_state({"workflow_runs": runs}, **arguments)


def test_passed_push_run_for_the_same_train_commit_covers_the_pull_request() -> None:
    assert _state([_run()]) == "passed"


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
def test_unfinished_push_run_means_wait_rather_than_skip(status: str) -> None:
    # A skipped pull-request check would be newer than the push check and hide
    # it, so skipping before the push run passes would land untested work.
    assert _state([_run(status=status, conclusion=None)]) == "running"


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required", None])
def test_push_run_that_did_not_pass_runs_the_full_suite(conclusion: str | None) -> None:
    assert _state([_run(conclusion=conclusion)]) == "none"


def test_any_failed_matching_push_run_wins_over_a_passed_one() -> None:
    assert _state([_run(), _run(conclusion="failure")]) == "none"
    assert _state([_run(conclusion="failure"), _run()]) == "none"


def test_unfinished_matching_push_run_keeps_waiting_beside_a_passed_one() -> None:
    assert _state([_run(), _run(status="in_progress", conclusion=None)]) == "running"


@pytest.mark.parametrize(
    "run_overrides",
    [
        {"head_sha": "001e4e41aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        {"head_branch": "launchplane/train/cbusillo/mediaforce/main/other-batch"},
        {"event": "pull_request"},
        {"path": ".github/workflows/codeql.yml"},
        {"head_repository": {"full_name": "someone/mediaforce"}},
        {"head_repository": None},
    ],
)
def test_a_run_that_is_not_this_workflows_push_of_this_commit_does_not_cover(
    run_overrides: dict[str, Any],
) -> None:
    assert _state([_run(**run_overrides)]) == "none"


def test_no_push_run_yet_runs_the_full_suite() -> None:
    assert _state([]) == "none"


def test_ordinary_pull_request_branches_never_skip() -> None:
    assert _state([_run(head_branch="work/862-dedupe")], head_ref="work/862-dedupe") == "none"


def test_fork_pull_request_named_like_a_train_branch_never_skips() -> None:
    assert _state([_run()], head_repository="someone/mediaforce") == "none"


def test_missing_head_commit_never_skips() -> None:
    assert _state([_run(head_sha="")], head_sha="") == "none"


@pytest.mark.parametrize("payload", ["", "not json", "[]", '{"workflow_runs": null}'])
def test_unreadable_api_response_runs_the_full_suite(
    payload: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert ci_train_push_run.main(_cli_arguments()) == 0
    assert capsys.readouterr().out.strip() == "none"


def test_command_line_prints_passed_for_a_covering_push_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"workflow_runs": [_run()]})))
    assert ci_train_push_run.main(_cli_arguments()) == 0
    assert capsys.readouterr().out.strip() == "passed"


def _cli_arguments() -> list[str]:
    return [
        "--repository",
        REPOSITORY,
        "--head-repository",
        REPOSITORY,
        "--head-ref",
        TRAIN_REF,
        "--head-sha",
        HEAD_SHA,
        "--workflow-path",
        WORKFLOW_PATH,
    ]
