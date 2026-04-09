#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${HOME}/Library/Application Support/mediaforce"
PID_FILE="${STATE_DIR}/mediaforce-web.pid"
LOG_FILE="${STATE_DIR}/mediaforce-web.log"

trim() {
	local value="${1:-}"
	printf '%s\n' "${value}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

load_env() {
	if [[ -f "${ROOT_DIR}/.env" ]]; then
		set -a
		# shellcheck disable=SC1091
		source "${ROOT_DIR}/.env"
		set +a
	fi
	HOST="${MEDIAFORCE_WEB_HOST:-127.0.0.1}"
	PORT="${MEDIAFORCE_WEB_PORT:-8777}"
}

current_pid_file() {
	if [[ -f "${PID_FILE}" ]]; then
		printf '%s\n' "${PID_FILE}"
		return 0
	fi
	return 1
}

web_binary() {
	local preferred="${ROOT_DIR}/.venv/bin/mediaforce-web"
	if [[ -x "${preferred}" ]]; then
		printf '%s\n' "${preferred}"
		return 0
	fi
	printf '%s\n' "${preferred}"
}

is_running() {
	local pid_file
	if ! pid_file="$(current_pid_file)"; then
		return 1
	fi
	local pid
	pid="$(<"${pid_file}")"
	[[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null && pid_matches_mediaforce_session "${pid}"
}

running_pid() {
	local pid_file
	pid_file="$(current_pid_file)"
	cat "${pid_file}"
}

pid_command() {
	local pid="${1:-}"
	ps -p "${pid}" -o command= 2>/dev/null || true
}

pid_parent() {
	local pid="${1:-}"
	ps -p "${pid}" -o ppid= 2>/dev/null | awk '{print $1}'
}

port_listener_pids() {
	load_env
	lsof -nP -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

pid_matches_mediaforce_session() {
	local pid="${1:-}"
	local managed_binary
	local depth=0
	managed_binary="$(web_binary)"
	while [[ -n "${pid}" && "${pid}" != "0" && ${depth} -lt 8 ]]; do
		local command
		command="$(pid_command "${pid}")"
		if [[ -n "${command}" ]]; then
			if [[ "${command}" == *"${managed_binary}"* ]]; then
				return 0
			fi
			if [[ "${command}" == *"uv run mediaforce-web"* && "${command}" == *"${ROOT_DIR}"* ]]; then
				return 0
			fi
		fi
		pid="$(trim "$(pid_parent "${pid}")")"
		depth=$((depth + 1))
	done
	return 1
}

managed_listener_pids() {
	local pid
	for pid in $(port_listener_pids); do
		if pid_matches_mediaforce_session "${pid}"; then
			printf '%s\n' "${pid}"
		fi
	done
}

foreign_listener_pids() {
	local pid
	for pid in $(port_listener_pids); do
		if ! pid_matches_mediaforce_session "${pid}"; then
			printf '%s\n' "${pid}"
		fi
	done
}

collect_descendant_pids() {
	local root_pid="${1:-}"
	local child
	for child in $(ps -axo pid=,ppid= | awk -v ppid="${root_pid}" '$2 == ppid {print $1}'); do
		printf '%s\n' "${child}"
		collect_descendant_pids "${child}"
	done
}

kill_pid_tree() {
	local root_pid="${1:-}"
	local descendants
	descendants="$(collect_descendant_pids "${root_pid}")"
	if [[ -n "${descendants}" ]]; then
		while IFS= read -r child_pid; do
			[[ -n "${child_pid}" ]] || continue
			kill "${child_pid}" 2>/dev/null || true
		done <<<"${descendants}"
	fi
	kill "${root_pid}" 2>/dev/null || true
}

cleanup_stale_sessions() {
	local reason="${1:-stale session}"
	local managed_pids
	managed_pids="$(managed_listener_pids)"
	if [[ -z "${managed_pids}" ]]; then
		return 0
	fi
	echo "cleaning ${reason} on ${HOST}:${PORT}"
	while IFS= read -r pid; do
		[[ -n "${pid}" ]] || continue
		kill_pid_tree "${pid}"
	done <<<"${managed_pids}"
	sleep 1
	return 0
}

start_server() {
	load_env
	mkdir -p "${STATE_DIR}"
	local managed_pids
	local foreign_pids
	if is_running; then
		echo "mediaforce-web already running on ${HOST}:${PORT} (pid $(running_pid))"
		return 0
	fi
	managed_pids="$(managed_listener_pids)"
	if [[ -n "${managed_pids}" ]]; then
		echo "mediaforce-web already running on ${HOST}:${PORT} (listener $(printf '%s' "${managed_pids}" | paste -sd ',' -))"
		return 0
	fi
	foreign_pids="$(foreign_listener_pids)"
	if [[ -n "${foreign_pids}" ]]; then
		echo "port ${PORT} is already in use by a non-mediaforce process; refusing to kill it" >&2
		return 1
	fi
	cleanup_stale_sessions "old mediaforce-web session"
	rm -f "${PID_FILE}"
	(
		cd "${ROOT_DIR}"
		nohup "$(web_binary)" >>"${LOG_FILE}" 2>&1 &
		echo $! >"${PID_FILE}"
	)
	sleep 1
	echo "started mediaforce-web on ${HOST}:${PORT} (pid $(running_pid))"
}

stop_server() {
	load_env
	local pid=""
	if is_running; then
		pid="$(running_pid)"
		kill_pid_tree "${pid}"
	fi
	cleanup_stale_sessions "old mediaforce-web session"
	rm -f "${PID_FILE}"
	if [[ -n "${pid}" ]]; then
		echo "stopped mediaforce-web (pid ${pid})"
		return 0
	fi
	if [[ -n "$(managed_listener_pids)" ]]; then
		echo "mediaforce-web listener cleanup may still be in progress" >&2
		return 1
	fi
	echo "mediaforce-web is not running"
}

status_server() {
	load_env
	if is_running; then
		echo "mediaforce-web running on ${HOST}:${PORT} (pid $(running_pid))"
		return 0
	fi
	local listener_pids
	listener_pids="$(managed_listener_pids)"
	if [[ -n "${listener_pids}" ]]; then
		echo "mediaforce-web running on ${HOST}:${PORT} (listener $(printf '%s' "${listener_pids}" | paste -sd ',' -))"
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
