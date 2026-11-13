#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat <<'USAGE'
Usage: scripts/build-mac-app.sh test
       scripts/build-mac-app.sh build OUTPUT_DIR
       scripts/build-mac-app.sh bundle OUTPUT_DIR CACHE_DIR
       scripts/build-mac-app.sh proof OUTPUT_DIR CACHE_DIR BUILD FEED_URL PUBLIC_KEY

test   builds the Mac app and runs its unit tests.
build  builds a Release copy into OUTPUT_DIR/Mediaforce.app, signed with the team's Developer ID
       so macOS lets it send notifications.
proof  builds a bundled copy for update proofs with its own app ID, service label and port, so it
       never addresses the live service, plus the given build number, feed and Sparkle key.
USAGE
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
derived_data="$repo_root/.build/mac-app"
project="$repo_root/macos/Mediaforce.xcodeproj"
# macOS gives an ad-hoc signed app no notification permission, so the installed copy is team-signed.

team_id() {
	uv run --directory "$repo_root" python -c 'from mediaforce.ops.login_item import MACOS_SIGNING_TEAM; print(MACOS_SIGNING_TEAM)'
}

generate_project() {
	command -v xcodegen >/dev/null || { echo "xcodegen is required (brew install xcodegen)" >&2; exit 1; }
	xcodegen generate --quiet --spec "$repo_root/macos/project.yml"
}

case "${1:-}" in
test)
	generate_project
	# An unsigned build is a new program to macOS every time, so it asks again to read the drive the build is on
	# and the test runner hangs until someone answers. With the team's certificate one answer lasts.
	identity=()
	team_id="$(team_id)"
	if security find-identity -v -p codesigning 2>/dev/null | grep -q "Developer ID Application: .*($team_id)"; then
		identity=(CODE_SIGN_IDENTITY="Developer ID Application" DEVELOPMENT_TEAM="$team_id")
	fi
	xcodebuild -project "$project" -scheme Mediaforce -destination 'platform=macOS' \
		-derivedDataPath "$derived_data" test ${identity[@]+"${identity[@]}"}
	;;
build|bundle|proof)
	output="${2:?build needs an output directory}"
	identity=()
	if [[ "$1" == proof ]]; then
		identity=(PRODUCT_BUNDLE_IDENTIFIER=com.shinycomputers.mediaforce.proof MEDIAFORCE_SERVICE_LABEL=com.mediaforce.web.proof
			MEDIAFORCE_SERVER_PORT=8787 "CURRENT_PROJECT_VERSION=${4:?proof needs a build number}"
			"MEDIAFORCE_UPDATE_FEED=${5:?proof needs a feed URL}" "MEDIAFORCE_UPDATE_KEY=${6:?proof needs a public key}")
	fi
	team_id="$(team_id)"
	generate_project
	xcodebuild -project "$project" -scheme Mediaforce -configuration Release -destination 'platform=macOS' \
		-derivedDataPath "$derived_data" build \
		CODE_SIGN_IDENTITY="Developer ID Application" DEVELOPMENT_TEAM="$team_id" ${identity[@]+"${identity[@]}"}
	mkdir -p "$output"
	rm -rf "$output/Mediaforce.app"
	ditto "$derived_data/Build/Products/Release/Mediaforce.app" "$output/Mediaforce.app"
	if [[ "$1" != build ]]; then
		uv run --directory "$repo_root" python -m scripts.bundle_server "$output/Mediaforce.app" \
			--cache "${3:?bundle needs a cache directory}"
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
