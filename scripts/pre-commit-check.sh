#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

run_step() {
	local label="${1}"
	shift
	echo
	echo "==> ${label}"
	"$@"
}

cd "${ROOT_DIR}"

run_step "CLI smoke" uv run mediaforce --help
run_step "Frontend checks" npm --prefix frontend run check
run_step "Frontend lint" npm --prefix frontend run lint
run_step "Frontend build" npm --prefix frontend run build

echo
echo "Pre-commit static checks and CLI smoke passed."
