"""Decide whether a merge-train pull request run can defer to its push run.

Launchplane pushes each merge-train candidate to a ``launchplane/train/``
branch, which starts the full CI workflow for that exact commit, and then
opens a pull request for the same commit, which would start it again. Both
runs report check runs on the same head commit, and the train reads every
check run on that commit, so the pull-request run adds no coverage. A failure
in the push run still blocks the candidate.

This script reads the repository's workflow runs for the pull request's head
commit (the JSON returned by ``GET /repos/{repo}/actions/runs?head_sha=...``)
on standard input and prints ``true`` only when the pull request is a
same-repository merge-train branch and this workflow already has a push run
for that same commit and branch. Anything else prints ``false``, so the full
suite runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

TRAIN_BRANCH_PREFIX = "launchplane/train/"


def push_run_covers(
    runs_payload: dict[str, Any],
    *,
    repository: str,
    head_repository: str,
    head_ref: str,
    head_sha: str,
    workflow_path: str,
) -> bool:
    """Return whether a push run of this workflow covers this pull request head."""
    if not head_ref.startswith(TRAIN_BRANCH_PREFIX):
        return False
    if not repository or head_repository != repository:
        return False
    if not head_sha:
        return False
    runs = runs_payload.get("workflow_runs")
    if not isinstance(runs, list):
        return False
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_repository = run.get("head_repository")
        if (
            run.get("event") == "push"
            and run.get("head_sha") == head_sha
            and run.get("head_branch") == head_ref
            and run.get("path") == workflow_path
            and isinstance(run_repository, dict)
            and run_repository.get("full_name") == repository
        ):
            return True
    return False


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
    covered = isinstance(payload, dict) and push_run_covers(
        payload,
        repository=args.repository,
        head_repository=args.head_repository,
        head_ref=args.head_ref,
        head_sha=args.head_sha,
        workflow_path=args.workflow_path,
    )
    print("true" if covered else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
