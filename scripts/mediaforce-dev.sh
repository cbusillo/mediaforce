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
	ps -ww -p "${pid}" -o command= 2>/dev/null || true
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
	[[ "${interpreter##*/}" =~ ^[Pp]ython([0-9]+(\.[0-9]+)*t?)?$ &&
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

pid_is_frontend_launcher() {
	local pid="${1:-}" cwd
	cwd="$(lsof -a -p "${pid}" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' || true)"
	if [[ -z "${cwd}" ]]; then
		pid_is_alive "${pid}" || return 1
		echo "frontend: working directory ownership unknown for pid ${pid}" >&2
		return 2
	fi
	(
		cd "${ROOT_DIR}"
		uv run --no-sync --project "${ROOT_DIR}" python "${ROOT_DIR}/mediaforce/ops/dev_frontend.py" \
			"${pid}" "${ROOT_DIR}" "${cwd}"
	)
}

pid_matches_mediaforce_frontend() {
	local pid="${1:-}"
	local depth=0
	while [[ -n "${pid}" && "${pid}" != "0" && "${pid}" != "1" && ${depth} -lt 8 ]]; do
		local status=0
		pid_is_frontend_launcher "${pid}" || status=$?
		[[ ${status} -eq 0 ]] && return 0
		[[ ${status} -eq 1 ]] || return 2
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
		[[ -n "${parent}" && "${parent}" != "0" && "${parent}" != "1" ]] || break
		local status=0
		pid_matches_mediaforce_frontend "${parent}" || status=$?
		if [[ ${status} -eq 2 ]]; then
			echo "frontend: parent ownership unknown; preserving ancestors and stopping proven subtree pid ${pid}" >&2
			break
		fi
		[[ ${status} -eq 0 ]] || break
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
		local status=0
		"${matcher}" "${pid}" || status=$?
		if [[ ${status} -eq 0 ]]; then
			printf '%s\n' "${pid}"
		elif [[ ${status} -ne 1 ]]; then
			return 2
		fi
	done
}

managed_listener_root_pids() {
	local port="${1:-}"
	local matcher="${2:-}"
	local root_resolver="${3:-}"
	local pid
	# Resolve and deduplicate every root before stopping any listener's tree.
	local listeners
	listeners="$(managed_listener_pids "${port}" "${matcher}")" || return 2
	for pid in ${listeners}; do
		"${root_resolver}" "${pid}"
	done | sort -u
}

foreign_listener_pids() {
	local port="${1:-}"
	local matcher="${2:-}"
	local pid
	for pid in $(port_listener_pids "${port}"); do
		local status=0
		"${matcher}" "${pid}" || status=$?
		if [[ ${status} -eq 1 ]]; then
			printf '%s\n' "${pid}"
		elif [[ ${status} -ne 0 ]]; then
			return 2
		fi
	done
}

kill_pid_tree() {
	local root_pid="${1:-}"
	local component="${2:-}"
	(
		cd "${ROOT_DIR}"
		uv run --no-sync --project "${ROOT_DIR}" python -m mediaforce.ops.dev_processes \
			"${root_pid}" "${ROOT_DIR}/scripts/mediaforce-dev.sh" "${component}"
	)
}

wait_for_no_managed_listener() {
	local port="${1:-}"
	local matcher="${2:-}"
	local attempt=0
	while [[ ${attempt} -lt 20 ]]; do
		local listeners
		listeners="$(managed_listener_pids "${port}" "${matcher}")" || return 2
		if [[ -z "${listeners}" ]]; then
			return 0
		fi
		attempt=$((attempt + 1))
		sleep 0.25
	done
	return 1
}

confirm_frontend_listener_clearance() {
	local status=0
	wait_for_no_managed_listener "${FRONTEND_PORT}" pid_matches_mediaforce_frontend || status=$?
	case "${status}" in
	0) return 0 ;;
	1) echo "frontend: cleanup finished but a managed listener remains on port ${FRONTEND_PORT}; PID bookkeeping retained; retry stop" >&2 ;;
	*) echo "frontend: cleanup finished but listener ownership unknown; PID bookkeeping retained; resolve the reader error and retry stop" >&2 ;;
	esac
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
	local line
	BACKEND_AGENT_DIRECTORY=""
	BACKEND_AGENT_PROGRAM=""
	BACKEND_AGENT_PID=""
	BACKEND_AGENT_INFO="$(launchctl print "gui/$(id -u)/${BACKEND_LAUNCH_AGENT}" 2>/dev/null)" || return 1
	while IFS= read -r line; do
		line="$(trim "${line}")"
		case "${line}" in
		"working directory = "*) BACKEND_AGENT_DIRECTORY="${line#working directory = }" ;;
		"program = "*) BACKEND_AGENT_PROGRAM="${line#program = }" ;;
		"pid = "*) BACKEND_AGENT_PID="${line#pid = }" ;;
		esac
	done <<<"${BACKEND_AGENT_INFO}"
	[[ "${BACKEND_AGENT_DIRECTORY}" == "${ROOT_DIR}" && "${BACKEND_AGENT_PROGRAM}" == "$(web_binary)" ]]
}

stop_backend_launch_agent() {
	local shutdown_file="${DEV_STATE_DIR}/backend-shutdown.pids"
	local target observed_pids service_pids="" pid attempt=0 pending bootout_status=0
	target="gui/$(id -u)/${BACKEND_LAUNCH_AGENT}"
	if ! backend_launch_agent_loaded; then
		if [[ "${BACKEND_AGENT_PROGRAM}" == "$(web_binary)" ]]; then
			echo "backend: login item uses this checkout's binary with a different working directory; refusing to manage it" >&2
			return 1
		fi
		[[ -f "${shutdown_file}" ]] || return 0
		service_pids="$(<"${shutdown_file}")"
	else
		observed_pids="${BACKEND_AGENT_PID}"
		if [[ -f "${shutdown_file}" ]]; then
			service_pids="$(<"${shutdown_file}")"$'\n'
		fi
		while IFS= read -r pid; do
			[[ -n "${pid}" ]] || continue
			service_pids+="${pid}"$'\n'"$(mediaforce_backend_root_pid "${pid}")"$'\n'
		done <<<"${observed_pids}"
		mkdir -p "${DEV_STATE_DIR}" || return 1
		printf '%s' "${service_pids}" >"${shutdown_file}" || return 1
		launchctl bootout "${target}" 2>/dev/null || bootout_status=$?
		case "${bootout_status}" in
		0 | 3 | 36 | 113) ;;
		*)
			echo "backend: could not unload login item ${BACKEND_LAUNCH_AGENT}; refusing to continue" >&2
			return 1
			;;
		esac
	fi
	while [[ ${attempt} -lt 20 ]]; do
		pending=false
		if backend_launch_agent_loaded; then
			pending=true
		fi
		while IFS= read -r pid; do
			if pid_is_alive "${pid}" && pid_matches_mediaforce_backend "${pid}"; then
				pending=true
			fi
		done <<<"${service_pids}"
		if [[ "${pending}" == false ]]; then
			rm -f "${shutdown_file}" || return 1
			echo "backend: unloaded launch agent ${BACKEND_LAUNCH_AGENT} and completed shutdown"
			return 0
		fi
		attempt=$((attempt + 1))
		sleep 0.25
	done
	echo "backend: login item shutdown did not finish within 20 checks; refusing to continue" >&2
	return 1
}

backend_has_listener_for_pid() {
	local pid="${1:-}" listener root
	root="$(mediaforce_backend_root_pid "${pid}")"
	for listener in $(managed_listener_pids "${BACKEND_PORT}" pid_matches_mediaforce_backend); do
		if [[ "$(mediaforce_backend_root_pid "${listener}")" == "${root}" ]]; then
			return 0
		fi
	done
	return 1
}

wait_for_backend_listener() {
	local pid="${1:-}" attempt=0
	while [[ ${attempt} -lt 20 ]]; do
		if pid_is_alive "${pid}" && pid_matches_mediaforce_backend "${pid}" && backend_has_listener_for_pid "${pid}"; then
			return 0
		fi
		attempt=$((attempt + 1))
		sleep 0.25
	done
	return 1
}

frontend_running_pid() {
	local pid
	pid="$(pid_from_file "${FRONTEND_PID_FILE}")"
	if pid_is_alive "${pid}"; then
		local status=0
		pid_matches_mediaforce_frontend "${pid}" || status=$?
		[[ ${status} -ne 2 ]] || return 2
		if [[ ${status} -eq 0 ]]; then
			printf '%s\n' "${pid}"
		fi
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
	stop_backend_launch_agent || return 1
	local running_pid
	running_pid="$(backend_running_pid)"
	if [[ -n "${running_pid}" ]]; then
		if ! wait_for_backend_listener "${running_pid}"; then
			echo "backend: pid ${running_pid} has not started listening on port ${BACKEND_PORT}; refusing to start another backend; see ${BACKEND_LOG_FILE}" >&2
			return 1
		fi
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
	running_pid="$(pid_from_file "${BACKEND_PID_FILE}")"
	if ! wait_for_backend_listener "${running_pid}"; then
		echo "backend: failed to start; see ${BACKEND_LOG_FILE}" >&2
		return 1
	fi
	echo "backend: started http://${BACKEND_HOST}:${BACKEND_PORT} pid ${running_pid}"
}

start_frontend() {
	load_env
	mkdir -p "${DEV_STATE_DIR}"
	local running_pid
	running_pid="$(frontend_running_pid)" || { frontend_discovery_unknown; return 1; }
	if [[ -n "${running_pid}" ]]; then
		echo "frontend: running http://${FRONTEND_HOST}:${FRONTEND_PORT} pid ${running_pid}"
		return 0
	fi
	local managed_pids foreign_pids
	managed_pids="$(managed_listener_pids "${FRONTEND_PORT}" pid_matches_mediaforce_frontend)" || { frontend_discovery_unknown; return 1; }
	if [[ -n "${managed_pids}" ]]; then
		echo "frontend: running http://${FRONTEND_HOST}:${FRONTEND_PORT} listener $(printf '%s' "${managed_pids}" | paste -sd ',' -)"
		return 0
	fi
	foreign_pids="$(foreign_listener_pids "${FRONTEND_PORT}" pid_matches_mediaforce_frontend)" || { frontend_discovery_unknown; return 1; }
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
	running_pid="$(frontend_running_pid)" || {
		echo "frontend: launched pid $(pid_from_file "${FRONTEND_PID_FILE}") but could not confirm ownership" >&2
		frontend_discovery_unknown
		return 1
	}
	if [[ -z "${running_pid}" ]]; then
		echo "frontend: failed to start; see ${FRONTEND_LOG_FILE}" >&2
		return 1
	fi
	echo "frontend: started http://${FRONTEND_HOST}:${FRONTEND_PORT} pid ${running_pid}"
}

stop_backend() {
	load_env
	stop_backend_launch_agent || return 1
	local pid managed_roots
	pid="$(backend_running_pid)"
	if [[ -n "${pid}" ]]; then
		pid="$(mediaforce_backend_root_pid "${pid}")"
		kill_pid_tree "${pid}" backend || return 1
		wait_for_no_managed_listener "${BACKEND_PORT}" pid_matches_mediaforce_backend || true
		rm -f "${BACKEND_PID_FILE}"
		echo "backend: stopped pid ${pid}"
		return 0
	fi
	managed_roots="$(managed_listener_root_pids "${BACKEND_PORT}" pid_matches_mediaforce_backend mediaforce_backend_root_pid)"
	if [[ -n "${managed_roots}" ]]; then
		while IFS= read -r root_pid; do
			[[ -n "${root_pid}" ]] || continue
			kill_pid_tree "${root_pid}" backend || return 1
		done <<<"${managed_roots}"
		wait_for_no_managed_listener "${BACKEND_PORT}" pid_matches_mediaforce_backend || true
		rm -f "${BACKEND_PID_FILE}"
		echo "backend: stopped tree $(printf '%s' "${managed_roots}" | paste -sd ',' -)"
		return 0
	fi
	rm -f "${BACKEND_PID_FILE}"
	echo "backend: stopped"
}

stop_frontend() {
	load_env
	local pid managed_roots
	pid="$(frontend_running_pid)" || { frontend_discovery_unknown; return 1; }
	if [[ -n "${pid}" ]]; then
		pid="$(mediaforce_frontend_root_pid "${pid}")"
		kill_pid_tree "${pid}" frontend || return 1
		confirm_frontend_listener_clearance || return 1
		rm -f "${FRONTEND_PID_FILE}"
		echo "frontend: stopped pid ${pid}"
		return 0
	fi
	managed_roots="$(managed_listener_root_pids "${FRONTEND_PORT}" pid_matches_mediaforce_frontend mediaforce_frontend_root_pid)" || { frontend_discovery_unknown; return 1; }
	if [[ -n "${managed_roots}" ]]; then
		while IFS= read -r root_pid; do
			[[ -n "${root_pid}" ]] || continue
			kill_pid_tree "${root_pid}" frontend || return 1
		done <<<"${managed_roots}"
		confirm_frontend_listener_clearance || return 1
		rm -f "${FRONTEND_PID_FILE}"
		echo "frontend: stopped tree $(printf '%s' "${managed_roots}" | paste -sd ',' -)"
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
	pid="$(frontend_running_pid)" || { frontend_discovery_unknown; return 2; }
	if [[ -n "${pid}" ]]; then
		echo "frontend: running http://${FRONTEND_HOST}:${FRONTEND_PORT} pid ${pid}"
		return 0
	fi
	listener_pids="$(managed_listener_pids "${FRONTEND_PORT}" pid_matches_mediaforce_frontend)" || { frontend_discovery_unknown; return 2; }
	if [[ -n "${listener_pids}" ]]; then
		echo "frontend: running http://${FRONTEND_HOST}:${FRONTEND_PORT} listener $(printf '%s' "${listener_pids}" | paste -sd ',' -)"
		return 0
	fi
	echo "frontend: stopped http://${FRONTEND_HOST}:${FRONTEND_PORT}"
	return 1
}

frontend_discovery_unknown() {
	echo "frontend: discovery ownership unknown; processes and PID bookkeeping retained; resolve the reader error, check uv and the checkout's prepared Python environment (uv sync --locked), then retry" >&2
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
		start_backend || return 1
		start_frontend
		;;
	restart:backend)
		stop_backend || return 1
		start_backend
		;;
	restart:frontend)
		stop_frontend && start_frontend
		;;
	status:all)
		status_backend || true
		local status=0
		status_frontend || status=$?
		[[ ${status} -ne 2 ]]
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
