# Mediaforce Handoff

## Current situation

Mediaforce v2 now lives in this standalone repo. It was recently promoted from
`claude-local-machine/projects/media-encoding` and pushed to
`cbusillo/mediaforce` on `main`.

The repo is in a good baseline state:

- standalone repo promotion is complete
- targeted backend tests passed during promotion
- frontend `npm run check` passed during promotion
- the `mediaforce` CLI entrypoint resolves correctly

The most likely active product work is the frontend regression track after the
move from the older refreshing-page UI model to the SvelteKit SPA. That means
the highest-value next work is probably on the dashboard, folders, settings,
and any workflow gaps caused by the migration.

## Start here

- Read `AGENTS.md` for project-specific facts that are easy to miss.
- Use `TODO.md` as the current priority list.

## Suggested next move

Pick one operator-critical Svelte route, validate it in a real browser, compare
it against the expected workflow, and close the highest-impact regression you
find first.

