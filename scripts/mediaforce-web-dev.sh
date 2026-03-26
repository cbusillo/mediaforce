#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${HOME}/Library/Application Support/media-harness"
PID_FILE="${STATE_DIR}/mediaforce-web.pid"
LEGACY_PID_FILE="${STATE_DIR}/media-harness-web.pid"
LOG_FILE="${STATE_DIR}/mediaforce-web.log"

load_env() {
	if [[ -f "${ROOT_DIR}/.env" ]]; then
		set -a
		# shellcheck disable=SC1091
		source "${ROOT_DIR}/.env"
		set +a
	fi
	HOST="${MEDIAFORCE_WEB_HOST:-${MEDIA_HARNESS_WEB_HOST:-127.0.0.1}}"
	PORT="${MEDIAFORCE_WEB_PORT:-${MEDIA_HARNESS_WEB_PORT:-8777}}"
}

current_pid_file() {
	if [[ -f "${PID_FILE}" ]]; then
		printf '%s\n' "${PID_FILE}"
		return 0
	fi
	if [[ -f "${LEGACY_PID_FILE}" ]]; then
		printf '%s\n' "${LEGACY_PID_FILE}"
		return 0
	fi
	return 1
}

web_binary() {
	local preferred="${ROOT_DIR}/.venv/bin/mediaforce-web"
	local legacy="${ROOT_DIR}/.venv/bin/media-harness-web"
	if [[ -x "${preferred}" ]]; then
		printf '%s\n' "${preferred}"
		return 0
	fi
	printf '%s\n' "${legacy}"
}

is_running() {
	local pid_file
	if ! pid_file="$(current_pid_file)"; then
		return 1
	fi
	local pid
	pid="$(<"${pid_file}")"
	[[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null
}

running_pid() {
	local pid_file
	pid_file="$(current_pid_file)"
	cat "${pid_file}"
}

start_server() {
	load_env
	mkdir -p "${STATE_DIR}"
	if is_running; then
		echo "mediaforce-web already running on ${HOST}:${PORT} (pid $(running_pid))"
		return 0
	fi
	rm -f "${PID_FILE}" "${LEGACY_PID_FILE}"
	(
		cd "${ROOT_DIR}"
		nohup "$(web_binary)" >>"${LOG_FILE}" 2>&1 &
		echo $! >"${PID_FILE}"
	)
	sleep 1
	echo "started mediaforce-web on ${HOST}:${PORT} (pid $(running_pid))"
}

stop_server() {
	if ! is_running; then
		rm -f "${PID_FILE}" "${LEGACY_PID_FILE}"
		echo "mediaforce-web is not running"
		return 0
	fi
	local pid
	pid="$(running_pid)"
	kill "${pid}"
	rm -f "${PID_FILE}" "${LEGACY_PID_FILE}"
	echo "stopped mediaforce-web (pid ${pid})"
}

status_server() {
	load_env
	if is_running; then
		echo "mediaforce-web running on ${HOST}:${PORT} (pid $(running_pid))"
		return 0
	fi
	echo "mediaforce-web is stopped"
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
