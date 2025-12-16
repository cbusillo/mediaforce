#!/usr/bin/env bash
set -euo pipefail

bundle_path="${1:-}"
if [[ -z "$bundle_path" ]]; then
  echo "Usage: scripts/deploy_bundle.sh <bundle.tgz>" >&2
  exit 2
fi

# Create a deploy bundle that does not clobber runtime environment.
# In particular: never ship .env so hosts keep their local token/url/name.

tar -czf "$bundle_path" \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='dist' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='.code' \
  --exclude='.pytest_cache' \
  --exclude='.mypy_cache' \
  --exclude='.env' \
  .

echo "Wrote bundle: $bundle_path" >&2

