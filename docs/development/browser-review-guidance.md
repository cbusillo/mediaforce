# Browser Review Guidance

Use this contract when launching browser-review subagents for UI exploration or
critique.

For repeatable route and fixture coverage, start from
`docs/development/browser-qa-matrix.md`. Browser-review subagents should add
visual and interaction judgment on top of that automated matrix, not replace it.

## Launch Contract

- For the first browser-review subagent in a session, run a tiny smoke pass
  before the full critique: open the page, wait for a known selector, capture a
  screenshot, and confirm the page title/URL.
- Launch browser-review subagents with `write: true` and explicitly mention the
  `browser-ui-review` skill in the task prompt.
- The prompt should require this exact order:
  1. open the live page in a browser
  2. wait for an app-specific ready selector
  3. interact enough to inspect the real UI state
  4. capture at least one screenshot artifact under `scratch/ui-checks/`
  5. report findings from the browser-visible result
- The prompt should also require the result to begin with a one-line browser
  status: `Browser review succeeded` or `BROWSER BLOCKED`.
- If `BROWSER BLOCKED` occurs, fix the browser-launch problem and rerun the
  review. Do not present the blocked subagent's code-informed notes as the
  requested review.
- For reviews of home, queue, review, or folder-workspace screens, require the
  critique to call out any SaaS-dashboard drift against
  `docs/style/workstation-ui.md`.
