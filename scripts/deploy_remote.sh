#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  scripts/deploy_remote.sh <user@host> <remote_dir> <bundle.tgz> [--systemd <unit>] [--launchd <label>]

Notes:
  - The deploy bundle should be created via scripts/deploy_bundle.sh.
  - This script never overwrites <remote_dir>/.env.
  - For macOS launchd, provide --launchd <label> (e.g. com.mediaforce.worker).
  - For Linux systemd, provide --systemd <unit> (e.g. mediaforce-worker).
EOF
}

host="${1:-}"
remote_dir="${2:-}"
bundle="${3:-}"
shift 3 || true

if [[ -z "$host" || -z "$remote_dir" || -z "$bundle" ]]; then
  usage
  exit 2
fi
if [[ ! -f "$bundle" ]]; then
  echo "Bundle not found: $bundle" >&2
  exit 2
fi

systemd_unit=""
launchd_label=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --systemd)
      systemd_unit="${2:-}"; shift 2 ;;
    --launchd)
      launchd_label="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2
      ;;
  esac
done

tmp_remote="/tmp/mediaforce-deploy.tgz"

echo "Uploading bundle to ${host}..." >&2
scp "$bundle" "$host:$tmp_remote" >/dev/null

echo "Deploying into ${remote_dir}..." >&2

# Use stdin to avoid ssh argument quoting issues.
ssh "$host" \
  "REMOTE_DIR=$(printf %q "$remote_dir") SYSTEMD_UNIT=$(printf %q "$systemd_unit") LAUNCHD_LABEL=$(printf %q "$launchd_label") TMP_REMOTE=$(printf %q "$tmp_remote") bash -s" <<'REMOTE'
set -euo pipefail

eval "REMOTE_DIR=${REMOTE_DIR}"
eval "SYSTEMD_UNIT=${SYSTEMD_UNIT}"
eval "LAUNCHD_LABEL=${LAUNCHD_LABEL}"
eval "TMP_REMOTE=${TMP_REMOTE}"

mkdir -p "${REMOTE_DIR}"
cd "${REMOTE_DIR}"
tar -xzf "${TMP_REMOTE}"

# Ensure local env stays local (bundle intentionally excludes it).
if [[ ! -f "${REMOTE_DIR}/.env" ]]; then
  {
    echo "# Local dev env (gitignored)"
    echo "MEDIAFORCE_API_TOKEN="
    echo "MEDIAFORCE_API_URL="
  } > "${REMOTE_DIR}/.env"
fi

if command -v uv >/dev/null 2>&1; then
  uv sync
elif [[ -x "${HOME}/.local/bin/uv" ]]; then
  "${HOME}/.local/bin/uv" sync
elif [[ -x "/root/.local/bin/uv" ]]; then
  /root/.local/bin/uv sync
else
  echo 'uv not found on remote; skipping uv sync' >&2
fi

if [[ -n "${SYSTEMD_UNIT}" ]]; then
  systemctl restart "${SYSTEMD_UNIT}"
  systemctl is-active "${SYSTEMD_UNIT}" >/dev/null
fi

if [[ -n "${LAUNCHD_LABEL}" ]]; then
  launchctl kickstart -k "gui/$(id -u)/${LAUNCHD_LABEL}" || true
fi
REMOTE

echo "Done." >&2
