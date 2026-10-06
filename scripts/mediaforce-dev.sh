#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
STATE_DIR="${HOME}/Library/Application Support/mediaforce"
CHECKOUT_ID="$(printf '%s' "${ROOT_DIR}" | shasum -a 256 | awk '{print $1}')"
DEV_STATE_DIR="${STATE_DIR}/development/${CHECKOUT_ID}"
BACKEND_PID_FILE="${DEV_STATE_DIR}/mediaforce-web.pid"
BACKEND_LOG_FILE="${STATE_DIR}/mediaforce-web.log"
BACKEND_LOCK_FILE="${STATE_DIR}/mediaforce-web.lock"
BACKEND_LAUNCH_AGENT="com.mediaforce.web"
FRONTEND_PID_FILE="${DEV_STATE_DIR}/mediaforce-frontend.pid"
FRONTEND_LOG_FILE="${STATE_DIR}/mediaforce-frontend.log"

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
	BACKEND_HOST="${MEDIAFORCE_WEB_HOST:-127.0.0.1}"
	BACKEND_PORT="${MEDIAFORCE_WEB_PORT:-8777}"
	BACKEND_RELOAD="${MEDIAFORCE_WEB_RELOAD:-false}"
	FRONTEND_HOST="${MEDIAFORCE_FRONTEND_DEV_HOST:-127.0.0.1}"
	FRONTEND_PORT="${MEDIAFORCE_FRONTEND_DEV_PORT:-4173}"
}

web_binary() {
	local preferred="${ROOT_DIR}/.venv/bin/mediaforce-web"
	printf '%s\n' "${preferred}"
}

pid_command() {
	local pid="${1:-}"
	ps -p "${pid}" -o command= 2>/dev/null || true
}

pid_parent() {
	local pid="${1:-}"
	ps -p "${pid}" -o ppid= 2>/dev/null | awk '{print $1}'
}

pid_is_alive() {
	local pid="${1:-}"
	[[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null
}

pid_from_file() {
	local pid_file="${1:-}"
	if [[ -f "${pid_file}" ]]; then
		trim "$(<"${pid_file}")"
	fi
}

port_listener_pids() {
	local port="${1:-}"
	lsof -nP -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

command_matches_backend_binary() {
	local command="${1:-}"
	local managed_binary
	managed_binary="$(web_binary)"
	local interpreter="${command%%" ${managed_binary}"*}"
	local arguments="${command#"${interpreter} "}"
	if [[ "${command}" == "${managed_binary}" || "${command}" == "${managed_binary} "* ]]; then
		return 0
	fi
	[[ "${interpreter##*/}" =~ ^python([0-9]+(\.[0-9]+)*)?$ &&
		( "${interpreter}" != *" "* || -x "${interpreter}" ) &&
		( "${arguments}" == "${managed_binary}" || "${arguments}" == "${managed_binary} "* ) ]]
}

pid_matches_mediaforce_backend() {
	local pid="${1:-}"
	local depth=0
	while [[ -n "${pid}" && "${pid}" != "0" && ${depth} -lt 8 ]]; do
		local command
		command="$(pid_command "${pid}")"
		if command_matches_backend_binary "${command}"; then
			return 0
		fi
		pid="$(trim "$(pid_parent "${pid}")")"
		depth=$((depth + 1))
	done
	return 1
}

mediaforce_backend_root_pid() {
	local pid="${1:-}"
	local root="${pid}"
	local depth=0
	while [[ -n "${pid}" && "${pid}" != "0" && ${depth} -lt 8 ]]; do
		local parent command
		parent="$(trim "$(pid_parent "${pid}")")"
		[[ -n "${parent}" && "${parent}" != "0" ]] || break
		command="$(pid_command "${parent}")"
		if command_matches_backend_binary "${command}"; then
			root="${parent}"
			pid="${parent}"
			depth=$((depth + 1))
			continue
		fi
		break
	done
	printf '%s\n' "${root}"
}

pid_matches_mediaforce_frontend() {
	local pid="${1:-}"
	local depth=0
	while [[ -n "${pid}" && "${pid}" != "0" && ${depth} -lt 8 ]]; do
		local command cwd
		command="$(pid_command "${pid}")"
		cwd="$(lsof -a -p "${pid}" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' || true)"
		case "${command}" in
		"npm --prefix frontend run dev" | "npm --prefix frontend run dev "*)
			[[ "${cwd}" == "${ROOT_DIR}" ]] && return 0 ;;
		"npm --prefix ${ROOT_DIR}/frontend run dev" | "npm --prefix ${ROOT_DIR}/frontend run dev "* | \
		*"/npm --prefix ${ROOT_DIR}/frontend run dev" | *"/npm --prefix ${ROOT_DIR}/frontend run dev "*)
			[[ "${cwd}" == "${ROOT_DIR}" || "${cwd}" == "${ROOT_DIR}/frontend" ]] && return 0 ;;
		"npm run dev" | "npm run dev "* | "vite" | "vite "* | \
		*" ${ROOT_DIR}/frontend/node_modules/.bin/vite" | *" ${ROOT_DIR}/frontend/node_modules/.bin/vite "*)
			[[ "${cwd}" == "${ROOT_DIR}/frontend" ]] && return 0 ;;
		esac
		pid="$(trim "$(pid_parent "${pid}")")"
		depth=$((depth + 1))
	done
	return 1
}

mediaforce_frontend_root_pid() {
	local pid="${1:-}"
	local parent depth=0
	while [[ ${depth} -lt 8 ]]; do
		parent="$(trim "$(pid_parent "${pid}")")"
		[[ -n "${parent}" && "${parent}" != "0" ]] || break
		pid_matches_mediaforce_frontend "${parent}" || break
		pid="${parent}"
		depth=$((depth + 1))
	done
	printf '%s\n' "${pid}"
}

managed_listener_pids() {
	local port="${1:-}"
	local matcher="${2:-}"
	local pid
	for pid in $(port_listener_pids "${port}"); do
		if "${matcher}" "${pid}"; then
			printf '%s\n' "${pid}"
		fi
	done
}

foreign_listener_pids() {
	local port="${1:-}"
	local matcher="${2:-}"
	local pid
	for pid in $(port_listener_pids "${port}"); do
		if ! "${matcher}" "${pid}"; then
			printf '%s\n' "${pid}"
		fi
	done
}

kill_pid_tree() {
	local root_pid="${1:-}"
	local component="${2:-}"
	uv run --no-sync --project "${ROOT_DIR}" python -m mediaforce.ops.dev_processes \
		"${root_pid}" "${ROOT_DIR}/scripts/mediaforce-dev.sh" "${component}"
}

wait_for_no_managed_listener() {
	local port="${1:-}"
	local matcher="${2:-}"
	local attempt=0
	while [[ ${attempt} -lt 20 ]]; do
		if [[ -z "$(managed_listener_pids "${port}" "${matcher}")" ]]; then
			return 0
		fi
		attempt=$((attempt + 1))
		sleep 0.25
	done
	return 1
}

backend_running_pid() {
	local pid
	pid="$(pid_from_file "${BACKEND_PID_FILE}")"
	if pid_is_alive "${pid}" && pid_matches_mediaforce_backend "${pid}"; then
		printf '%s\n' "${pid}"
		return 0
	fi
	pid="$(backend_lock_pid)"
	if pid_is_alive "${pid}" && pid_matches_mediaforce_backend "${pid}"; then
		printf '%s\n' "${pid}"
	fi
}

backend_lock_pid() {
	if [[ -f "${BACKEND_LOCK_FILE}" ]]; then
		python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text()).get("pid", ""))' "${BACKEND_LOCK_FILE}" 2>/dev/null || true
	fi
}

backend_launch_agent_loaded() {
	local info line working_directory="" program=""
	info="$(launchctl print "gui/$(id -u)/${BACKEND_LAUNCH_AGENT}" 2>/dev/null)" || return 1
	while IFS= read -r line; do
		line="$(trim "${line}")"
		case "${line}" in
		"working directory = "*) working_directory="${line#working directory = }" ;;
		"program = "*) program="${line#program = }" ;;
		esac
	done <<<"${info}"
	[[ "${working_directory}" == "${ROOT_DIR}" && "${program}" == "$(web_binary)" ]]
}

stop_backend_launch_agent() {
	if backend_launch_agent_loaded; then
		launchctl bootout "gui/$(id -u)/${BACKEND_LAUNCH_AGENT}" 2>/dev/null || true
		sleep 0.5
		echo "backend: unloaded launch agent ${BACKEND_LAUNCH_AGENT}"
	fi
}

frontend_running_pid() {
	local pid
	pid="$(pid_from_file "${FRONTEND_PID_FILE}")"
	if pid_is_alive "${pid}" && pid_matches_mediaforce_frontend "${pid}"; then
		printf '%s\n' "${pid}"
	fi
}

reload_arg() {
	case "$(printf '%s' "${BACKEND_RELOAD}" | tr '[:upper:]' '[:lower:]')" in
	1 | true | yes | on) printf '%s\n' "--reload" ;;
	*) printf '%s\n' "--no-reload" ;;
	esac
}

start_backend() {
	load_env
	mkdir -p "${DEV_STATE_DIR}"
	stop_backend_launch_agent
	local running_pid
	running_pid="$(backend_running_pid)"
	if [[ -n "${running_pid}" ]]; then
		echo "backend: running http://${BACKEND_HOST}:${BACKEND_PORT} pid ${running_pid}"
		return 0
	fi
	local managed_pids foreign_pids
	managed_pids="$(managed_listener_pids "${BACKEND_PORT}" pid_matches_mediaforce_backend)"
	if [[ -n "${managed_pids}" ]]; then
		echo "backend: running http://${BACKEND_HOST}:${BACKEND_PORT} listener $(printf '%s' "${managed_pids}" | paste -sd ',' -)"
		return 0
	fi
	foreign_pids="$(foreign_listener_pids "${BACKEND_PORT}" pid_matches_mediaforce_backend)"
	if [[ -n "${foreign_pids}" ]]; then
		echo "backend: port ${BACKEND_PORT} is used by a process outside this checkout; refusing to start" >&2
		return 1
	fi
	rm -f "${BACKEND_PID_FILE}"
	local command=("$(web_binary)" --host "${BACKEND_HOST}" --port "${BACKEND_PORT}" "$(reload_arg)")
	if [[ -n "${MEDIAFORCE_CONFIG_PATH:-}" ]]; then
		command+=(--config "${MEDIAFORCE_CONFIG_PATH}")
	fi
	(
		cd "${ROOT_DIR}"
		nohup "${command[@]}" >>"${BACKEND_LOG_FILE}" 2>&1 &
		echo $! >"${BACKEND_PID_FILE}"
	)
	sleep 1
	running_pid="$(backend_running_pid)"
	if [[ -z "${running_pid}" ]]; then
		echo "backend: failed to start; see ${BACKEND_LOG_FILE}" >&2
		return 1
	fi
	echo "backend: started http://${BACKEND_HOST}:${BACKEND_PORT} pid ${running_pid}"
}

start_frontend() {
	load_env
	mkdir -p "${DEV_STATE_DIR}"
	local running_pid
	running_pid="$(frontend_running_pid)"
	if [[ -n "${running_pid}" ]]; then
		echo "frontend: running http://${FRONTEND_HOST}:${FRONTEND_PORT} pid ${running_pid}"
		return 0
	fi
	local managed_pids foreign_pids
	managed_pids="$(managed_listener_pids "${FRONTEND_PORT}" pid_matches_mediaforce_frontend)"
	if [[ -n "${managed_pids}" ]]; then
		echo "frontend: running http://${FRONTEND_HOST}:${FRONTEND_PORT} listener $(printf '%s' "${managed_pids}" | paste -sd ',' -)"
		return 0
	fi
	foreign_pids="$(foreign_listener_pids "${FRONTEND_PORT}" pid_matches_mediaforce_frontend)"
	if [[ -n "${foreign_pids}" ]]; then
		echo "frontend: port ${FRONTEND_PORT} is used by a process outside this checkout; refusing to start" >&2
		return 1
	fi
	rm -f "${FRONTEND_PID_FILE}"
	(
		cd "${ROOT_DIR}/frontend"
		nohup npm --prefix "${ROOT_DIR}/frontend" run dev -- --host "${FRONTEND_HOST}" --port "${FRONTEND_PORT}" --strictPort >>"${FRONTEND_LOG_FILE}" 2>&1 &
		echo $! >"${FRONTEND_PID_FILE}"
	)
	sleep 1
	running_pid="$(frontend_running_pid)"
	if [[ -z "${running_pid}" ]]; then
		echo "frontend: failed to start; see ${FRONTEND_LOG_FILE}" >&2
		return 1
	fi
	echo "frontend: started http://${FRONTEND_HOST}:${FRONTEND_PORT} pid ${running_pid}"
}

stop_backend() {
	load_env
	stop_backend_launch_agent
	local pid managed_pids
	pid="$(backend_running_pid)"
	if [[ -n "${pid}" ]]; then
		pid="$(mediaforce_backend_root_pid "${pid}")"
		kill_pid_tree "${pid}" backend || return 1
		wait_for_no_managed_listener "${BACKEND_PORT}" pid_matches_mediaforce_backend || true
		rm -f "${BACKEND_PID_FILE}"
		echo "backend: stopped pid ${pid}"
		return 0
	fi
	managed_pids="$(managed_listener_pids "${BACKEND_PORT}" pid_matches_mediaforce_backend)"
	if [[ -n "${managed_pids}" ]]; then
		while IFS= read -r listener_pid; do
			[[ -n "${listener_pid}" ]] || continue
			listener_pid="$(mediaforce_backend_root_pid "${listener_pid}")"
			kill_pid_tree "${listener_pid}" backend || return 1
		done <<<"${managed_pids}"
		wait_for_no_managed_listener "${BACKEND_PORT}" pid_matches_mediaforce_backend || true
		rm -f "${BACKEND_PID_FILE}"
		echo "backend: stopped listener $(printf '%s' "${managed_pids}" | paste -sd ',' -)"
		return 0
	fi
	rm -f "${BACKEND_PID_FILE}"
	echo "backend: stopped"
}

stop_frontend() {
	load_env
	local pid managed_pids
	pid="$(frontend_running_pid)"
	if [[ -n "${pid}" ]]; then
		pid="$(mediaforce_frontend_root_pid "${pid}")"
		kill_pid_tree "${pid}" frontend || return 1
		wait_for_no_managed_listener "${FRONTEND_PORT}" pid_matches_mediaforce_frontend || true
		rm -f "${FRONTEND_PID_FILE}"
		echo "frontend: stopped pid ${pid}"
		return 0
	fi
	managed_pids="$(managed_listener_pids "${FRONTEND_PORT}" pid_matches_mediaforce_frontend)"
	if [[ -n "${managed_pids}" ]]; then
		while IFS= read -r listener_pid; do
			[[ -n "${listener_pid}" ]] || continue
			listener_pid="$(mediaforce_frontend_root_pid "${listener_pid}")"
			kill_pid_tree "${listener_pid}" frontend || return 1
		done <<<"${managed_pids}"
		wait_for_no_managed_listener "${FRONTEND_PORT}" pid_matches_mediaforce_frontend || true
		rm -f "${FRONTEND_PID_FILE}"
		echo "frontend: stopped listener $(printf '%s' "${managed_pids}" | paste -sd ',' -)"
		return 0
	fi
	rm -f "${FRONTEND_PID_FILE}"
	echo "frontend: stopped"
}

status_backend() {
	load_env
	local pid listener_pids
	pid="$(backend_running_pid)"
	if [[ -n "${pid}" ]]; then
		echo "backend: running http://${BACKEND_HOST}:${BACKEND_PORT} pid ${pid}"
		return 0
	fi
	listener_pids="$(managed_listener_pids "${BACKEND_PORT}" pid_matches_mediaforce_backend)"
	if [[ -n "${listener_pids}" ]]; then
		echo "backend: running http://${BACKEND_HOST}:${BACKEND_PORT} listener $(printf '%s' "${listener_pids}" | paste -sd ',' -)"
		return 0
	fi
	echo "backend: stopped http://${BACKEND_HOST}:${BACKEND_PORT}"
	return 1
}

status_frontend() {
	load_env
	local pid listener_pids
	pid="$(frontend_running_pid)"
	if [[ -n "${pid}" ]]; then
		echo "frontend: running http://${FRONTEND_HOST}:${FRONTEND_PORT} pid ${pid}"
		return 0
	fi
	listener_pids="$(managed_listener_pids "${FRONTEND_PORT}" pid_matches_mediaforce_frontend)"
	if [[ -n "${listener_pids}" ]]; then
		echo "frontend: running http://${FRONTEND_HOST}:${FRONTEND_PORT} listener $(printf '%s' "${listener_pids}" | paste -sd ',' -)"
		return 0
	fi
	echo "frontend: stopped http://${FRONTEND_HOST}:${FRONTEND_PORT}"
	return 1
}

smoke_backend() {
	load_env
	local base_url="http://127.0.0.1:${BACKEND_PORT}"
	curl -fsS "${base_url}/" >/dev/null
	curl -fsS "${base_url}/api/dashboard" >/dev/null
	curl -fsS "${base_url}/api/settings" >/dev/null
	curl -fsS "${base_url}/api/hosts" >/dev/null
	echo "backend: smoke passed ${base_url}"
}

smoke_frontend() {
	load_env
	local base_url="http://127.0.0.1:${FRONTEND_PORT}"
	curl -fsS "${base_url}/" >/dev/null
	echo "frontend: smoke passed ${base_url}"
}

run_for_component() {
	local action="${1:-status}"
	local component="${2:-all}"
	case "${action}:${component}" in
	start:all) start_backend && start_frontend ;;
	start:backend) start_backend ;;
	start:frontend) start_frontend ;;
	stop:all)
		stop_frontend || return 1
		stop_backend || return 1
		;;
	stop:backend) stop_backend ;;
	stop:frontend) stop_frontend ;;
	restart:all)
		stop_frontend || return 1
		stop_backend || return 1
		start_backend && start_frontend
		;;
	restart:backend)
		stop_backend && start_backend
		;;
	restart:frontend)
		stop_frontend && start_frontend
		;;
	status:all)
		status_backend || true
		status_frontend || true
		;;
	status:backend) status_backend ;;
	status:frontend) status_frontend ;;
	smoke:all)
		smoke_backend
		smoke_frontend
		;;
	smoke:backend) smoke_backend ;;
	smoke:frontend) smoke_frontend ;;
	*)
		echo "usage: $(basename "$0") {start|stop|restart|status|smoke} [all|backend|frontend]" >&2
		exit 1
		;;
	esac
}

if [[ "${1:-}" == "check-owner" ]]; then
	case "${2:-}" in
	backend) pid_matches_mediaforce_backend "${3:-}" ;;
	frontend) pid_matches_mediaforce_frontend "${3:-}" ;;
	*) exit 1 ;;
	esac
else
	run_for_component "${1:-status}" "${2:-all}"
fi
