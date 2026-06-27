#!/usr/bin/env bash

runner_diag_python() {
    python3 "${FACTORY:?FACTORY not set}/scripts/project_diagnostics.py" "$@"
}

runner_mark_running() {
    local project="$1" step="$2" action="$3"
    runner_diag_python write-status "$project" \
        --state running \
        --step "$step" \
        --action "$action" \
        --display-status running
}

runner_mark_consultation() {
    local project="$1" step="$2" gate="$3"
    runner_diag_python write-status "$project" \
        --state waiting \
        --step "$step" \
        --action consultation_wait \
        --reason-code CONSULTATION_PENDING \
        --reason-summary "Runner paused for human consultation" \
        --display-status awaiting_consultation \
        --consultation-gate "$gate"
}

export -f runner_diag_python runner_mark_running runner_mark_consultation
