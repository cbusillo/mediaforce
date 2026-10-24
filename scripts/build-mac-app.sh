#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat <<'USAGE'
Usage: scripts/build-mac-app.sh test
       scripts/build-mac-app.sh build OUTPUT_DIR

test   builds the Mac app and runs its unit tests.
build  builds a Release copy, signed to run on this Mac, into OUTPUT_DIR/Mediaforce.app.
USAGE
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
derived_data="$repo_root/.build/mac-app"
project="$repo_root/macos/Mediaforce.xcodeproj"

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
build)
	output="${2:?build needs an output directory}"
	generate_project
	xcodebuild -project "$project" -scheme Mediaforce -configuration Release -destination 'platform=macOS' \
		-derivedDataPath "$derived_data" build
	mkdir -p "$output"
	rm -rf "$output/Mediaforce.app"
	ditto "$derived_data/Build/Products/Release/Mediaforce.app" "$output/Mediaforce.app"
	codesign --verify --strict "$output/Mediaforce.app"
	;;
*)
	usage >&2
	exit 2
	;;
esac
