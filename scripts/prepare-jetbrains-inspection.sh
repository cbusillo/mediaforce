#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
skills_home="${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/skills"
python_prepare="$skills_home/jetbrains-inspection/scripts/prepare-python-project.py"

if [[ ! -f "$python_prepare" ]]; then
	echo "JetBrains inspection preparation helper not found: $python_prepare" >&2
	exit 1
fi

uv run "$python_prepare" \
	--repo "$repo_root" \
	--python 3.13 \
	--module-name mediaforce \
	--test-root tests \
	--sync

rm -f "$repo_root/.idea/mediaforce@"*.iml
module_file="$repo_root/.idea/mediaforce.iml"
node - "$module_file" <<'NODE'
const fs = require('node:fs');

const moduleFile = process.argv[2];
const original = fs.readFileSync(moduleFile, 'utf8');
let normalized = original.replace(
	'<content url="file://$MODULE_DIR$">',
	'<content url="file://$MODULE_DIR$/..">'
);
for (const relativePath of ['.mypy_cache', '.system', '.local', '.code', '.venv', 'tests']) {
	normalized = normalized.replaceAll(
		`url="file://$MODULE_DIR$/${relativePath}"`,
		`url="file://$MODULE_DIR$/../${relativePath}"`
	);
}
const frontendExclusion = '      <excludeFolder url="file://$MODULE_DIR$/../frontend" />';
if (!normalized.includes(frontendExclusion)) {
	normalized = normalized.replace('    </content>', `${frontendExclusion}\n    </content>`);
}
if (normalized !== original) fs.writeFileSync(moduleFile, normalized);
NODE

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
