# Acceptance Gate

We do not stop at "it works." We stop when we fully like the result.

## Required bar

- We should 100% love the code we are about to commit
- Remove code smells, bad practices, stale naming, and obvious design issues
- Follow the [local/CI test selection](../style/testing.md) before commits or ending a session
- For primary user surfaces, read `docs/style/workstation-ui.md`
- Use the `qualityGate` section in `.github/github.json` as the
  canonical source for repo validation commands

## Minimum checklist

Local checks apply before committing. CI checks apply before ready or merge.

- Focused local tests and complete affected safety modules pass
- Full backend and Mac app tests pass in CI
- CLI smoke passes
- `bash scripts/pre-commit-check.sh` runs CLI smoke; select local tests separately
- Package-sensitive changes pass `uv build` and
  `scripts/verify_package_contents.py`
- Docs are updated when behavior, workflow, operations, or user-facing
  expectations change
- The final code is something we would be happy to own long term

## Inspection authority

- The required Python lane and its `prepare.python` settings are declared in
  `.github/github.json`, with `config/jetbrains/Mediaforce.xml` as the tracked
  profile. The shared [jetbrains-inspection skill](https://github.com/cbusillo/codex-skills/blob/main/skills/jetbrains-inspection/SKILL.md)
  owns native preparation, execution, coverage, and cleanup.
- Changes to the shared inspection profile's approved baseline require another
  clean/defect qualification.
