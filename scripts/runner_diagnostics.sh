#!/usr/bin/env bash

diag_python() {
    python3 "${FACTORY:?FACTORY not set}/scripts/project_diagnostics.py" "$@"
}

diag_warn() {
    printf 'runner_diagnostics.sh: %s failed\n' "$1" >&2
}

diag_status() {
    local project="$1" state="$2" step="$3" action="$4" reason_code="$5" reason_summary="$6"
    local action_csv="${7:-}"
    local evidence="${8:-}"
    local -a cmd=(write-status "$project" --state "$state" --step "$step" --action "$action")

    [[ -n "$reason_code" ]] && cmd+=(--reason-code "$reason_code")
    [[ -n "$reason_summary" ]] && cmd+=(--reason-summary "$reason_summary")

    if [[ -n "$action_csv" ]]; then
        local IFS=','
        for item in $action_csv; do
            [[ -n "$item" ]] && cmd+=(--suggested-action "$item")
        done
    fi

    if [[ -n "$evidence" ]]; then
        local IFS=','
        for item in $evidence; do
            [[ -n "$item" ]] && cmd+=(--evidence "$item")
        done
    fi

    if ! diag_python "${cmd[@]}" >/dev/null 2>&1; then
        diag_warn "write-status"
    fi
}

diag_event() {
    local project="$1" step="$2" event_type="$3" reason_code="$4" message="$5"
    local file_path="${6:-}"
    local -a cmd=(append-event "$project" --step "$step" --type "$event_type" --message "$message")

    [[ -n "$reason_code" ]] && cmd+=(--reason-code "$reason_code")
    [[ -n "$file_path" ]] && cmd+=(--file "$file_path")

    if ! diag_python "${cmd[@]}" >/dev/null 2>&1; then
        diag_warn "append-event"
    fi
}

export -f diag_python diag_warn diag_status diag_event
