#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat <<'USAGE'
Usage: scripts/build-mac-app.sh test
       scripts/build-mac-app.sh build OUTPUT_DIR
       scripts/build-mac-app.sh bundle OUTPUT_DIR CACHE_DIR

test   builds the Mac app and runs its unit tests.
build  builds a Release copy into OUTPUT_DIR/Mediaforce.app, signed with the team's Developer ID
       so macOS lets it send notifications.
USAGE
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
derived_data="$repo_root/.build/mac-app"
project="$repo_root/macos/Mediaforce.xcodeproj"
# macOS gives an ad-hoc signed app no notification permission, so the installed copy is team-signed.
team_id="$(uv run --directory "$repo_root" python -c 'from mediaforce.ops.login_item import MACOS_SIGNING_TEAM; print(MACOS_SIGNING_TEAM)')"

generate_project() {
	command -v xcodegen >/dev/null || { echo "xcodegen is required (brew install xcodegen)" >&2; exit 1; }
	xcodegen generate --quiet --spec "$repo_root/macos/project.yml"
}

case "${1:-}" in
test)
	generate_project
	xcodebuild -project "$project" -scheme Mediaforce -destination 'platform=macOS' \
		-derivedDataPath "$derived_data" test
	;;
build|bundle)
	output="${2:?build needs an output directory}"
	generate_project
	xcodebuild -project "$project" -scheme Mediaforce -configuration Release -destination 'platform=macOS' \
		-derivedDataPath "$derived_data" build \
		CODE_SIGN_IDENTITY="Developer ID Application" DEVELOPMENT_TEAM="$team_id"
	mkdir -p "$output"
	rm -rf "$output/Mediaforce.app"
	ditto "$derived_data/Build/Products/Release/Mediaforce.app" "$output/Mediaforce.app"
	if [[ "$1" == bundle ]]; then
		uv run --directory "$repo_root" python -m scripts.bundle_server "$output/Mediaforce.app" --cache "${3:?bundle needs a cache directory}"
	fi
	codesign --verify --strict "$output/Mediaforce.app"
	signature="$(codesign -dv "$output/Mediaforce.app" 2>&1)"
	[[ "$signature" == *$'\n'"TeamIdentifier=$team_id"$'\n'* ]] ||
		{ echo "Mediaforce.app is not signed by team $team_id" >&2; exit 1; }
	;;
*)
	usage >&2
	exit 2
	;;
esac
