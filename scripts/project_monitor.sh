#!/usr/bin/env bash
# Read-only monitor for a Modeling Factory project.
# Usage:
#   scripts/project_monitor.sh <project> [interval_seconds]
#   scripts/project_monitor.sh --once <project>

set -euo pipefail

FACTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="loop"

if [[ "${1:-}" == "--once" ]]; then
    MODE="once"
    shift
fi

PROJECT="${1:?Usage: scripts/project_monitor.sh [--once] <project> [interval_seconds]}"
INTERVAL="${2:-900}"

PROJECT_DIR="$FACTORY/ongoing/$PROJECT"
if [[ ! -d "$PROJECT_DIR" && -d "$FACTORY/complete/$PROJECT" ]]; then
    PROJECT_DIR="$FACTORY/complete/$PROJECT"
fi

LOG="$FACTORY/run_state/${PROJECT}_monitor.log"
PIDFILE="$FACTORY/run_state/${PROJECT}_monitor.pid"

mkdir -p "$FACTORY/run_state"

runner_pid() {
    if [[ -f "$PROJECT_DIR/.runner.pid" ]]; then
        tr -d '[:space:]' < "$PROJECT_DIR/.runner.pid"
        return
    fi
    if [[ -f "$PROJECT_DIR/.runner.lock.info" ]]; then
        sed -n 's/^pid=//p' "$PROJECT_DIR/.runner.lock.info" | tail -1
    fi
}

latest_file() {
    find "$PROJECT_DIR" -maxdepth 4 -type f \
        -printf '%T@ %TY-%Tm-%Td %TH:%TM:%TS %s %p\n' 2>/dev/null \
        | sort -nr \
        | head -1 \
        | cut -d' ' -f2-
}

latest_log() {
    find "$PROJECT_DIR/logs" -maxdepth 1 -type f \
        -printf '%T@ %TY-%Tm-%Td %TH:%TM:%TS %s %f\n' 2>/dev/null \
        | sort -nr \
        | head -1 \
        | cut -d' ' -f2-
}

count_files() {
    local path="$1"
    local pattern="$2"
    find "$path" -type f -name "$pattern" 2>/dev/null | wc -l
}

status_json_summary() {
    local status_file="$PROJECT_DIR/diagnostics/status.json"
    [[ -f "$status_file" ]] || return 0
    python3 -c 'import json, sys
p = sys.argv[1]
d = json.load(open(p))
print("status_json state={state} current_step={step} action={action} reason={reason}".format(
    state=d.get("state", ""),
    step=d.get("current_step", ""),
    action=d.get("current_action", ""),
    reason=d.get("reason_code", ""),
))' "$status_file" 2>/dev/null || true
}

monitor_once() {
    {
        echo "===== MONITOR $(date -u '+%Y-%m-%d %H:%M:%S UTC') ====="

        if [[ ! -d "$PROJECT_DIR" ]]; then
            echo "project_dir_missing=$PROJECT_DIR"
            echo
            return
        fi

        local inferred
        inferred="$("$FACTORY/run_paper.sh" --infer-step "$PROJECT_DIR" 2>/dev/null || echo "unknown")"
        echo "project=$PROJECT"
        echo "project_dir=$PROJECT_DIR"
        echo "infer_step=$inferred"

        if [[ -f "$PROJECT_DIR/checkpoint.md" ]]; then
            grep -m1 'Last completed step' "$PROJECT_DIR/checkpoint.md" 2>/dev/null || true
        fi

        if [[ -f "$PROJECT_DIR/.heartbeat" ]]; then
            local hb_mtime hb_age
            hb_mtime="$(stat -c %Y "$PROJECT_DIR/.heartbeat" 2>/dev/null || echo 0)"
            hb_age=$(( $(date +%s) - hb_mtime ))
            echo "heartbeat=$(cat "$PROJECT_DIR/.heartbeat" 2>/dev/null) age_sec=$hb_age"
        else
            echo "heartbeat=missing"
        fi

        local pid
        pid="$(runner_pid || true)"
        if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
            echo "runner_pid=$pid alive=yes"
            ps -o pid,ppid,etime,stat,pcpu,pmem,comm -p "$pid" 2>/dev/null || true
        else
            echo "runner_pid=${pid:-missing} alive=no"
        fi

        status_json_summary

        echo "latest_file=$(latest_file || true)"
        echo "latest_log=$(latest_log || true)"
        echo "values_json_count=$(count_files "$PROJECT_DIR/results" "values.json")"
        echo "solver_job_meta_count=$(count_files "$PROJECT_DIR/run_state/solver_jobs" "*.meta")"
        [[ -f "$PROJECT_DIR/solve_log.md" ]] && echo "solve_log=present" || echo "solve_log=missing"
        [[ -f "$PROJECT_DIR/results/canonical_results.json" ]] && echo "canonical_results=present" || echo "canonical_results=missing"
        [[ -f "$PROJECT_DIR/results/invariants.json" ]] && echo "invariants=present" || echo "invariants=missing"

        if [[ -f "$PROJECT_DIR/logs/runner.log" ]]; then
            echo "--- runner.log tail ---"
            tail -12 "$PROJECT_DIR/logs/runner.log" 2>/dev/null || true
        fi

        echo
    } >> "$LOG"
}

if [[ "$MODE" == "once" ]]; then
    monitor_once
    exit 0
fi

if [[ -f "$PIDFILE" ]]; then
    old_pid="$(tr -d '[:space:]' < "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "${old_pid:-}" ]] && kill -0 "$old_pid" 2>/dev/null; then
        echo "Monitor already running for $PROJECT: pid $old_pid"
        exit 0
    fi
fi

echo "$$" > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

while true; do
    monitor_once

    if [[ -d "$FACTORY/complete/$PROJECT" ]]; then
        {
            echo "===== MONITOR $(date -u '+%Y-%m-%d %H:%M:%S UTC') ====="
            echo "project completed; monitor exiting"
            echo
        } >> "$LOG"
        break
    fi

    sleep "$INTERVAL"
done
