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

echo
echo "Pre-commit CLI smoke passed."
