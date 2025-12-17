#!/usr/bin/env bash
set -euo pipefail

bundle_path="${1:-}"
if [[ -z "$bundle_path" ]]; then
	echo "Usage: scripts/deploy_bundle.sh <bundle.tgz>" >&2
	exit 2
fi

# Create a deploy bundle that does not clobber runtime environment.
# In particular: never ship .env so hosts keep their local token/url/name.
#
bundle_cmd=(
	--exclude='.git'
	--exclude='node_modules'
	--exclude='dist'
	--exclude='__pycache__'
	--exclude='.venv'
	--exclude='.code'
	--exclude='.pytest_cache'
	--exclude='.mypy_cache'
	--exclude='.env'
	.
)

# Avoid including macOS extended attributes in the bundle (Linux tar will warn).
# Prefer bsdtar flags when available.
if command -v bsdtar >/dev/null 2>&1; then
	if bsdtar -czf "$bundle_path" --no-xattrs --no-mac-metadata "${bundle_cmd[@]}" 2>/dev/null; then
		echo "Wrote bundle: $bundle_path" >&2
		exit 0
	fi
fi

export COPYFILE_DISABLE=1
export COPY_EXTENDED_ATTRIBUTES_DISABLE=1
tar -czf "$bundle_path" "${bundle_cmd[@]}"

echo "Wrote bundle: $bundle_path" >&2
