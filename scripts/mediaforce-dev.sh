#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ -f "${ROOT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${ROOT_DIR}/.env"
    set +a
fi
if [[ ! -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    echo "Prepare this checkout with uv sync --locked before running development commands." >&2
    exit 1
fi
cd "${ROOT_DIR}"
exec "${ROOT_DIR}/.venv/bin/python" "${ROOT_DIR}/mediaforce/ops/dev_processes.py" "${1:-status}" "${2:-all}"
