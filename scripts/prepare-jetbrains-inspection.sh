#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
skills_home="${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/skills"
python_prepare="$skills_home/jetbrains-inspection/scripts/prepare-python-project.py"

if [[ ! -f "$python_prepare" ]]; then
	echo "JetBrains inspection preparation helper not found: $python_prepare" >&2
	exit 1
fi

for extra_module in "$repo_root/.idea/mediaforce@"*.iml; do
	if [[ -e "$extra_module" ]]; then
		echo "Review duplicate IDE module before preparation: $extra_module" >&2
		exit 1
	fi
done

uv run --no-project "$python_prepare" \
	--repo "$repo_root" \
	--python 3.13 \
	--module-name mediaforce \
	--test-root tests \
	--sync

module_file="$repo_root/.idea/mediaforce.iml"
node "$repo_root/scripts/prepare-jetbrains-state.mjs" normalize "$module_file"

frontend_root="$repo_root/frontend"
dependency_stamp="$frontend_root/node_modules/.mediaforce-dependencies.sha256"
dependency_digest="$(
	node "$repo_root/scripts/prepare-jetbrains-state.mjs" digest "$frontend_root/package.json" "$frontend_root/package-lock.json"
)"
if [[ -z "$dependency_digest" ]]; then
	echo "Dependency digest preparation returned no digest" >&2
	exit 1
fi

if [[ ! -f "$dependency_stamp" ]] || [[ ! -f "$frontend_root/node_modules/.package-lock.json" ]] || [[ "$(<"$dependency_stamp")" != "$dependency_digest" ]]; then
	rm -f "$dependency_stamp"
	npm --prefix "$frontend_root" ci
	(cd "$frontend_root" && ./node_modules/.bin/svelte-kit sync)
	printf '%s\n' "$dependency_digest" >"$dependency_stamp"
elif [[ ! -d "$frontend_root/.svelte-kit" ]]; then
	(cd "$frontend_root" && ./node_modules/.bin/svelte-kit sync)
fi
if [[ ! -d "$frontend_root/.svelte-kit" ]]; then
	echo "Svelte preparation did not create .svelte-kit" >&2
	exit 1
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
