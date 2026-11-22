#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat <<'USAGE'
Usage: scripts/build-mac-app.sh test
       scripts/build-mac-app.sh build OUTPUT_DIR
       scripts/build-mac-app.sh bundle OUTPUT_DIR CACHE_DIR
       scripts/build-mac-app.sh proof OUTPUT_DIR CACHE_DIR BUILD FEED_URL PUBLIC_KEY
       scripts/build-mac-app.sh release OUTPUT_DIR CACHE_DIR BUILD
       scripts/build-mac-app.sh notarize APP ARCHIVE
       scripts/build-mac-app.sh smoke APP

test   builds the Mac app and runs its unit tests.
build  builds a Release copy into OUTPUT_DIR/Mediaforce.app, signed with the team's Developer ID
       so macOS lets it send notifications.
proof  builds a bundled copy for update proofs with its own app ID, service label and port, so it
       never addresses the live service, plus the given build number, feed and Sparkle key.
release  builds the bundled copy a release ships: the given build number, and the feed and Sparkle key from
       pyproject.toml.
Every build shows pyproject.toml's [project] version.
notarize  has Apple notarize APP, staples it, checks Gatekeeper accepts it and writes the stapled ARCHIVE (zip).
       It reads an App Store Connect API key: NOTARY_KEY (the .p8 file), NOTARY_KEY_ID and NOTARY_ISSUER.
smoke  starts APP's server on its own port with a throwaway config and checks it answers as APP's build.
USAGE
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
derived_data="$repo_root/.build/mac-app"
project="$repo_root/macos/Mediaforce.xcodeproj"
# macOS gives an ad-hoc signed app no notification permission, so the installed copy is team-signed.

team_id() {
	uv run --directory "$repo_root" python -c 'from mediaforce.ops.login_item import MACOS_SIGNING_TEAM; print(MACOS_SIGNING_TEAM)'
}

project_version() {
	local line section="" pattern='^version = "([0-9]+\.[0-9]+\.[0-9]+)"$'
	while IFS= read -r line; do
		[[ "$line" == "["*"]" ]] && section="$line"
		if [[ "$section" == "[project]" && "$line" =~ $pattern ]]; then
			echo "${BASH_REMATCH[1]}"
			return
		fi
	done <"$repo_root/pyproject.toml"
	echo "pyproject.toml's [project] version is not MAJOR.MINOR.PATCH" >&2
	return 1
}

generate_project() {
	command -v xcodegen >/dev/null || { echo "xcodegen is required (brew install xcodegen)" >&2; exit 1; }
	xcodegen generate --quiet --spec "$repo_root/macos/project.yml"
}

case "${1:-}" in
test | build | bundle | proof | release)
	version="MARKETING_VERSION=$(project_version)"
	;;
esac

case "${1:-}" in
test)
	generate_project
	# An unsigned build is a new program to macOS every time, so it asks again to read the drive the build is on
	# and the test runner hangs until someone answers. With the team's certificate one answer lasts.
	# The team is only looked up when a certificate is here, so CI needs no Python setup to run the tests.
	# The test host gets its own identity. With the installed app's bundle identifier, the test harness asked
	# Launch Services for that identifier and opened the installed app; the service label and port keep a test host
	# away from the running service too.
	identity=(MEDIAFORCE_BUNDLE_IDENTIFIER=com.shinycomputers.mediaforce.testhost
		MEDIAFORCE_SERVICE_LABEL=com.mediaforce.web.testhost MEDIAFORCE_SERVER_PORT=8797)
	if security find-identity -v -p codesigning 2>/dev/null | grep -q "Developer ID Application: "; then
		team_id="$(team_id)"
		if security find-identity -v -p codesigning | grep -q "Developer ID Application: .*($team_id)"; then
			identity+=(CODE_SIGN_IDENTITY="Developer ID Application" DEVELOPMENT_TEAM="$team_id")
		fi
	fi
	xcodebuild -project "$project" -scheme Mediaforce -destination 'platform=macOS' \
		-derivedDataPath "$derived_data" test "$version" ${identity[@]+"${identity[@]}"}
	;;
build|bundle|proof|release)
	output="${2:?build needs an output directory}"
	identity=()
	if [[ "$1" == release ]]; then
		release="$(uv run --directory "$repo_root" python -m scripts.release_app identity)"
		value() { sed -n "s/^$1=//p" <<<"$release"; }
		[[ -n "$(value public_key)" ]] || { echo "pyproject.toml names no Sparkle public key for releases" >&2; exit 1; }
		build="${4:?release needs a build number}"
		[[ "$build" =~ ^[1-9][0-9]*$ ]] || { echo "the build number must be a positive integer, not $build" >&2; exit 1; }
		[[ "$version" == "MARKETING_VERSION=$(value version)" ]] ||
			{ echo "$version disagrees with the release identity's version $(value version)" >&2; exit 1; }
		identity=("CURRENT_PROJECT_VERSION=$build" "MEDIAFORCE_UPDATE_FEED=$(value feed)" "MEDIAFORCE_UPDATE_KEY=$(value public_key)")
	elif [[ "$1" == proof ]]; then
		identity=(PRODUCT_BUNDLE_IDENTIFIER=com.shinycomputers.mediaforce.proof MEDIAFORCE_SERVICE_LABEL=com.mediaforce.web.proof
			MEDIAFORCE_SERVER_PORT=8787 "CURRENT_PROJECT_VERSION=${4:?proof needs a build number}"
			"MEDIAFORCE_UPDATE_FEED=${5:?proof needs a feed URL}" "MEDIAFORCE_UPDATE_KEY=${6:?proof needs a public key}")
	fi
	team_id="$(team_id)"
	generate_project
	xcodebuild -project "$project" -scheme Mediaforce -configuration Release -destination 'platform=macOS' \
		-derivedDataPath "$derived_data" build \
		CODE_SIGN_IDENTITY="Developer ID Application" DEVELOPMENT_TEAM="$team_id" "$version" ${identity[@]+"${identity[@]}"}
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
notarize)
	app="${2:?notarize needs the app}"
	archive="${3:?notarize needs the archive to write}"
	submission="$(mktemp -d)/Mediaforce.zip"
	ditto -c -k --keepParent "$app" "$submission"
	notary=(--key "${NOTARY_KEY:?}" --key-id "${NOTARY_KEY_ID:?}" --issuer "${NOTARY_ISSUER:?}")
	result="$(xcrun notarytool submit "$submission" "${notary[@]}" --wait --timeout 1h --output-format json)"
	echo "$result"
	id="$(plutil -extract id raw - <<<"$result")"
	# Apple's log lists warnings even for an accepted submission; keep it in the run's output.
	xcrun notarytool log "$id" "${notary[@]}"
	[[ "$(plutil -extract status raw - <<<"$result")" == Accepted ]] || { echo "Apple did not accept $app" >&2; exit 1; }
	xcrun stapler staple "$app"
	xcrun stapler validate "$app"
	spctl --assess --type execute --verbose=4 "$app"
	rm -f "$archive"
	ditto -c -k --sequesterRsrc --keepParent "$app" "$archive"
	;;
smoke)
	app="${2:?smoke needs the app}"
	support="$(mktemp -d)"
	port="$(plutil -extract MediaforceServerPort raw "$app/Contents/Info.plist")"
	! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null || { echo "port $port is in use; smoke a proof build here" >&2; exit 1; }
	printf '[state]\ndb_path = "%s/library.sqlite3"\nrun_manifest_dir = "%s/runs"\nweb_state_dir = "%s/web"\n[media]\nsource_roots = {}\nstaging_root = "%s/staging"\narchive_root = "%s/archive"\n' \
		"$support" "$support" "$support" "$support" "$support" > "$support/config.toml"
	"$app/Contents/MacOS/mediaforce-server" --config "$support/config.toml" --host 127.0.0.1 --no-reload \
		> "$support/server.log" 2>&1 &
	server=$!
	stop_server() {
		local status=$?
		kill "$server" 2>/dev/null || true
		wait "$server" 2>/dev/null || true
		exit "$status"
	}
	trap stop_server EXIT
	expected="$(plutil -extract CFBundleVersion raw "$app/Contents/Info.plist")"
	build=""
	for _ in $(seq 1 120); do
		if answer="$(curl -fsS -m 3 "http://127.0.0.1:$port/api/release/work" 2>/dev/null)"; then
			build="$(plutil -extract server.build raw - <<<"$answer" 2>/dev/null)" || true
			break
		fi
		kill -0 "$server" 2>/dev/null || break
		sleep 1
	done
	[[ "$build" == "$expected" ]] || { cat "$support/server.log" >&2; echo "the server did not answer as build $expected" >&2; exit 1; }
	echo "the bundled server answers as build $build"
	;;
*)
	usage >&2
	exit 2
	;;
esac
