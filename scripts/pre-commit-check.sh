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

run_staged_frontend_lint() {
	echo
	echo "==> Frontend lint (staged files)"

	local path
	local -a prettier_files=()
	local -a eslint_files=()
	while IFS= read -r path; do
		[[ -n "${path}" ]] || continue
		case "${path}" in
		frontend/*)
			prettier_files+=("${path}")
			case "${path}" in
			*.js | *.cjs | *.mjs | *.ts | *.svelte)
				eslint_files+=("${path}")
				;;
			esac
			;;
		esac
	done < <(git diff --cached --name-only --diff-filter=ACMR)

	if ((${#prettier_files[@]} == 0)); then
		echo "No staged frontend files to lint."
		return 0
	fi

	npm --prefix frontend exec -- prettier --check --ignore-unknown "${prettier_files[@]}"
	if ((${#eslint_files[@]} > 0)); then
		npm --prefix frontend exec -- eslint "${eslint_files[@]}"
	fi
}

cd "${ROOT_DIR}"

run_step "Backend tests" uv run --with pytest pytest
run_step "CLI smoke" uv run mediaforce --help
run_step "Frontend checks" npm --prefix frontend run check
run_staged_frontend_lint
run_step "Frontend unit tests" npm --prefix frontend test
run_step "Frontend build" npm --prefix frontend run build

echo
echo "Pre-commit acceptance checks passed."
