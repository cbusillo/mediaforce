"""Decide whether a merge-train pull request run can defer to its push run.

Launchplane pushes each merge-train candidate to a ``launchplane/train/``
branch, which starts the full CI workflow for that exact commit, and then
opens a pull request for the same commit, which would start it again.

Only the newest check run of each name counts, in GitHub's merge box and in
the train alike, so a skipped pull-request check would hide a push check that
is still running or has failed. The pull-request run may therefore skip only
after the push run for that exact commit has passed.

This script reads the repository's workflow runs for the pull request's head
commit (the JSON returned by ``GET /repos/{repo}/actions/runs?head_sha=...``)
on standard input and prints one word:

- ``passed``: this workflow's push run for the same commit and branch, in a
  same-repository merge-train pull request, completed successfully.
- ``running``: that push run exists but has not finished yet; check again.
- ``none``: anything else, including a failed or cancelled push run, so the
  full suite runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Literal

TRAIN_BRANCH_PREFIX = "launchplane/train/"

PushRunState = Literal["passed", "running", "none"]


def push_run_state(
    runs_payload: dict[str, Any],
    *,
    repository: str,
    head_repository: str,
    head_ref: str,
    head_sha: str,
    workflow_path: str,
) -> PushRunState:
    """Return the state of the push run of this workflow for this pull request head."""
    if not head_ref.startswith(TRAIN_BRANCH_PREFIX):
        return "none"
    if not repository or head_repository != repository:
        return "none"
    if not head_sha:
        return "none"
    runs = runs_payload.get("workflow_runs")
    if not isinstance(runs, list):
        return "none"
    state: PushRunState = "none"
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_repository = run.get("head_repository")
        if not (
            run.get("event") == "push"
            and run.get("head_sha") == head_sha
            and run.get("head_branch") == head_ref
            and run.get("path") == workflow_path
            and isinstance(run_repository, dict)
            and run_repository.get("full_name") == repository
        ):
            continue
        if run.get("status") != "completed":
            state = "running"
        elif run.get("conclusion") != "success":
            # A failed run keeps the full suite; it must not be hidden.
            return "none"
        elif state == "none":
            state = "passed"
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head-repository", required=True)
    parser.add_argument("--head-ref", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--workflow-path", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}
    state: PushRunState = "none"
    if isinstance(payload, dict):
        state = push_run_state(
            payload,
            repository=args.repository,
            head_repository=args.head_repository,
            head_ref=args.head_ref,
            head_sha=args.head_sha,
            workflow_path=args.workflow_path,
        )
    print(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
