#!/usr/bin/env bash

# Shared, fail-closed health contract for the production API service.
# HTTP success is accepted only after the listener is proved to belong to the
# systemd unit and the unit remains stable for the configured observation window.

SERVICE_NAME="${SERVICE_NAME:-paper-factory-api.service}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:${BACKEND_PORT}/}"
BACKEND_READY_ATTEMPTS="${BACKEND_READY_ATTEMPTS:-30}"
BACKEND_STABILITY_SECONDS="${BACKEND_STABILITY_SECONDS:-5}"
PROC_ROOT="${PROC_ROOT:-/proc}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SS_BIN="${SS_BIN:-ss}"
CURL_BIN="${CURL_BIN:-curl}"

backend_service_value() {
    "$SYSTEMCTL_BIN" show "$SERVICE_NAME" --property="$1" --value
}

backend_listener_pids() {
    "$SS_BIN" -H -ltnp "sport = :$BACKEND_PORT" 2>/dev/null \
        | grep -oE 'pid=[0-9]+' \
        | cut -d= -f2 \
        | sort -nu
}

backend_pid_in_cgroup() {
    local pid="$1"
    local control_group="$2"
    local membership="$PROC_ROOT/$pid/cgroup"
    [ -r "$membership" ] || return 1
    awk -F: -v expected="$control_group" '
        $3 == expected || index($3, expected "/") == 1 { found = 1 }
        END { exit found ? 0 : 1 }
    ' "$membership"
}

backend_service_snapshot() {
    local active substate main_pid restarts control_group listener_pids pid
    active="$(backend_service_value ActiveState)" || return 1
    substate="$(backend_service_value SubState)" || return 1
    main_pid="$(backend_service_value MainPID)" || return 1
    restarts="$(backend_service_value NRestarts)" || return 1
    control_group="$(backend_service_value ControlGroup)" || return 1

    if [ "$active" != "active" ] || [ "$substate" != "running" ]; then
        echo "backend unit is not active/running: ${active}/${substate}" >&2
        return 1
    fi
    if ! [[ "$main_pid" =~ ^[1-9][0-9]*$ ]] || [ ! -d "$PROC_ROOT/$main_pid" ]; then
        echo "backend unit has no live MainPID: $main_pid" >&2
        return 1
    fi
    if ! [[ "$restarts" =~ ^[0-9]+$ ]] || [[ "$control_group" != /* ]]; then
        echo "backend unit metadata is invalid" >&2
        return 1
    fi
    if ! backend_pid_in_cgroup "$main_pid" "$control_group"; then
        echo "MainPID $main_pid is outside unit cgroup $control_group" >&2
        return 1
    fi

    listener_pids="$(backend_listener_pids)"
    if [ -z "$listener_pids" ]; then
        echo "no inspectable listener owns TCP port $BACKEND_PORT" >&2
        return 1
    fi
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        if ! backend_pid_in_cgroup "$pid" "$control_group"; then
            echo "listener PID $pid is outside unit cgroup $control_group" >&2
            return 1
        fi
    done <<< "$listener_pids"

    printf '%s|%s|%s|%s\n' "$main_pid" "$restarts" "$control_group" \
        "$(tr '\n' ',' <<< "$listener_pids" | sed 's/,$//')"
}

wait_for_owned_backend() {
    local attempt snapshot
    for attempt in $(seq 1 "$BACKEND_READY_ATTEMPTS"); do
        if snapshot="$(backend_service_snapshot)" \
            && "$CURL_BIN" -fsS "$BACKEND_URL" >/dev/null; then
            printf '%s\n' "$snapshot"
            return 0
        fi
        sleep 1
    done
    echo "backend did not become unit-owned and HTTP-ready after $BACKEND_READY_ATTEMPTS attempts" >&2
    return 1
}

verify_backend_service_stable() {
    local first second first_pid first_restarts first_group _first_listeners
    local second_pid second_restarts second_group _second_listeners
    first="$(wait_for_owned_backend)" || return 1
    IFS='|' read -r first_pid first_restarts first_group _first_listeners <<< "$first"

    sleep "$BACKEND_STABILITY_SECONDS"
    second="$(backend_service_snapshot)" || return 1
    IFS='|' read -r second_pid second_restarts second_group _second_listeners <<< "$second"
    if [ "$first_pid" != "$second_pid" ]; then
        echo "backend MainPID changed during stability window: $first_pid -> $second_pid" >&2
        return 1
    fi
    if [ "$first_restarts" != "$second_restarts" ]; then
        echo "backend NRestarts changed during stability window: $first_restarts -> $second_restarts" >&2
        return 1
    fi
    if [ "$first_group" != "$second_group" ]; then
        echo "backend ControlGroup changed during stability window" >&2
        return 1
    fi
    if ! "$CURL_BIN" -fsS "$BACKEND_URL" >/dev/null; then
        echo "backend HTTP probe failed after stability window" >&2
        return 1
    fi
    printf '%s\n' "$second"
}

if [ "${1:-}" = "--verify" ]; then
    verify_backend_service_stable
fi
