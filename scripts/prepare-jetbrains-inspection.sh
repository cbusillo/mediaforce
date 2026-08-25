#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

(
	cd "$repo_root"
	uv sync --locked --python 3.13
)

frontend_root="$repo_root/frontend"
dependency_stamp="$frontend_root/node_modules/.mediaforce-dependencies.sha256"
dependency_digest="$(
	node -e '
const crypto = require("node:crypto");
const fs = require("node:fs");
const digest = crypto.createHash("sha256");
for (const path of process.argv.slice(1)) digest.update(fs.readFileSync(path));
process.stdout.write(digest.digest("hex"));
' "$frontend_root/package.json" "$frontend_root/package-lock.json"
)"

if [[ ! -f "$dependency_stamp" ]] || [[ "$(<"$dependency_stamp")" != "$dependency_digest" ]]; then
	npm ci --prefix "$frontend_root"
	printf '%s\n' "$dependency_digest" >"$dependency_stamp"
elif [[ ! -d "$frontend_root/.svelte-kit" ]]; then
	npm --prefix "$frontend_root" run prepare
fi

mkdir -p "$repo_root/.idea/inspectionProfiles"
canonical_profile="$repo_root/config/jetbrains/Mediaforce.xml"
generated_profile="$repo_root/.idea/inspectionProfiles/Mediaforce.xml"
if ! cmp -s "$canonical_profile" "$generated_profile"; then
	cp "$canonical_profile" "$generated_profile"
fi

mkdir -p "$frontend_root/.idea/inspectionProfiles"
generated_frontend_profile="$frontend_root/.idea/inspectionProfiles/Mediaforce.xml"
if ! cmp -s "$canonical_profile" "$generated_frontend_profile"; then
	cp "$canonical_profile" "$generated_frontend_profile"
fi
