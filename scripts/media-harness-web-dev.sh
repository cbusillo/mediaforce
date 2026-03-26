#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${HOME}/Library/Application Support/media-harness"
PID_FILE="${STATE_DIR}/media-harness-web.pid"
LOG_FILE="${STATE_DIR}/media-harness-web.log"

load_env() {
	if [[ -f "${ROOT_DIR}/.env" ]]; then
		set -a
		# shellcheck disable=SC1091
		source "${ROOT_DIR}/.env"
		set +a
	fi
	HOST="${MEDIA_HARNESS_WEB_HOST:-127.0.0.1}"
	PORT="${MEDIA_HARNESS_WEB_PORT:-8765}"
}

is_running() {
	if [[ ! -f "${PID_FILE}" ]]; then
		return 1
	fi
	local pid
	pid="$(<"${PID_FILE}")"
	[[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null
}

start_server() {
	load_env
	mkdir -p "${STATE_DIR}"
	if is_running; then
		echo "media-harness-web already running on ${HOST}:${PORT} (pid $(<"${PID_FILE}"))"
		return 0
	fi
	rm -f "${PID_FILE}"
	(
		cd "${ROOT_DIR}"
		nohup "${ROOT_DIR}/.venv/bin/media-harness-web" >>"${LOG_FILE}" 2>&1 &
		echo $! >"${PID_FILE}"
	)
	sleep 1
	echo "started media-harness-web on ${HOST}:${PORT} (pid $(<"${PID_FILE}"))"
}

stop_server() {
	if ! is_running; then
		rm -f "${PID_FILE}"
		echo "media-harness-web is not running"
		return 0
	fi
	local pid
	pid="$(<"${PID_FILE}")"
	kill "${pid}"
	rm -f "${PID_FILE}"
	echo "stopped media-harness-web (pid ${pid})"
}

status_server() {
	load_env
	if is_running; then
		echo "media-harness-web running on ${HOST}:${PORT} (pid $(<"${PID_FILE}"))"
		return 0
	fi
	echo "media-harness-web is stopped"
	return 1
}

smoke_test() {
	load_env
	local base_url
	base_url="http://127.0.0.1:${PORT}"
	curl -fsS "${base_url}/" >/dev/null
	curl -fsS "${base_url}/api/dashboard" >/dev/null
	curl -fsS "${base_url}/api/settings" >/dev/null
	curl -fsS "${base_url}/api/hosts" >/dev/null
	echo "smoke passed for ${base_url}"
}

case "${1:-status}" in
start)
	start_server
	;;
stop)
	stop_server
	;;
restart)
	stop_server || true
	start_server
	;;
status)
	status_server
	;;
smoke)
	smoke_test
	;;
*)
	echo "usage: $(basename "$0") {start|stop|restart|status|smoke}" >&2
	exit 1
	;;
esac
