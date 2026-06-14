#!/usr/bin/env bash
# ablation_monitor.sh — 监视消融实验进程，长时间无输出时自动诊断并重启
# Usage: ./scripts/ablation_monitor.sh [--stale-mins N]  (default: 15 min)

set -uo pipefail
FACTORY="$(cd "$(dirname "$0")/.." && pwd)"
cd "$FACTORY"

STALE_MINS="${1:-15}"
STALE_SECS=$(( STALE_MINS * 60 ))
POLL=60   # check every 60s

declare -A ABLATION_VARS=(
  [cumcm2024b_no_judge_rep1]="ABLATE_NO_JUDGE=1"
  [cumcm2024b_no_consult_rep1]="ABLATE_NO_CONSULTATION=1"
  [cumcm2024b_no_methodlib_rep1]="ABLATE_NO_METHOD_LIB=1"
  [cumcm2024b_no_innov_rep1]="ABLATE_NO_INNOVATION_PROTECT=1"
)

log() { echo "[$(date '+%H:%M:%S')] $*"; }

runner_pid() {
    local proj="$1"
    # check registered pid first, then lock dir
    local pidfile="$FACTORY/run_state/${proj}.pid"
    [[ -f "$pidfile" ]] && cat "$pidfile" && return
    local lock="$FACTORY/ongoing/$proj/.runner.lock"
    [[ -f "$lock/pid" ]] && cat "$lock/pid"
}

is_alive() { local pid="$1"; [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; }

runner_log() { echo "$FACTORY/ongoing/$1/logs/runner.log"; }

last_output_age() {
    local f; f="$(runner_log "$1")"
    [[ -f "$f" ]] || { echo 99999; return; }
    echo $(( $(date +%s) - $(stat -c %Y "$f") ))
}

diagnose() {
    local proj="$1"
    log "  >> DIAGNOSING $proj"
    local rlog; rlog="$(runner_log "$proj")"
    log "  last runner.log lines:"
    tail -5 "$rlog" 2>/dev/null | sed 's/^/     /'
    # check heartbeat
    local hb="$FACTORY/ongoing/$proj/.heartbeat"
    [[ -f "$hb" ]] && log "  heartbeat: $(cat "$hb")" || log "  heartbeat: missing"
    # check lock
    local lock="$FACTORY/ongoing/$proj/.runner.lock"
    if [[ -d "$lock" ]]; then
        local lpid; lpid=$(cat "$lock/pid" 2>/dev/null || echo "no-pid")
        log "  lock exists (pid=$lpid)"
        is_alive "$lpid" && log "  lock holder ALIVE" || log "  lock holder DEAD — stale lock"
    fi
    # check FATAL / STUCK in log
    local fatal; fatal=$(grep "FATAL\|STUCK" "$rlog" 2>/dev/null | tail -3)
    [[ -n "$fatal" ]] && log "  FATAL/STUCK: $fatal"
}

start_project() {
    local proj="$1" env_var="${ABLATION_VARS[$proj]}"
    # clear stale lock
    rm -rf "$FACTORY/ongoing/$proj/.runner.lock"
    local out="$FACTORY/logs/${proj}_mon_$(date +%Y%m%d_%H%M%S).out"
    nohup env $env_var "$FACTORY/run_paper.sh" "$FACTORY/ongoing/$proj" >> "$out" 2>&1 &
    local pid=$!
    echo "$pid" > "$FACTORY/run_state/${proj}.pid"
    log "  restarted $proj -> pid $pid ($env_var)"
}

is_complete() {
    local proj="$1"
    [[ -d "$FACTORY/complete/$proj" ]] && return 0
    local inferred; inferred=$(grep -oP 'file-state: step \K[0-9]+' "$FACTORY/ongoing/$proj/logs/runner.log" 2>/dev/null | tail -1)
    [[ "$inferred" == "16" ]] && return 0
    return 1
}

log "=== ablation_monitor started (stale=${STALE_MINS}min, poll=${POLL}s) ==="
mkdir -p "$FACTORY/run_state"

while true; do
    all_done=1
    for proj in "${!ABLATION_VARS[@]}"; do
        [[ -d "$FACTORY/ongoing/$proj" ]] || continue
        is_complete "$proj" && log "$proj: complete ✓" && continue
        all_done=0

        pid=$(runner_pid "$proj" || true)
        age=$(last_output_age "$proj")

        if is_alive "$pid"; then
            log "$proj: running (pid=$pid, runner.log ${age}s ago)"
            if (( age > STALE_SECS )); then
                log "  STALE (>${STALE_MINS}min no output)"
                diagnose "$proj"
                log "  killing stale runner..."
                kill -TERM "$pid" 2>/dev/null; sleep 3; kill -KILL "$pid" 2>/dev/null || true
                start_project "$proj"
            fi
        else
            log "$proj: STOPPED (runner.log ${age}s ago)"
            diagnose "$proj"
            # only block restart if the LAST line of the log is a FATAL (not historical)
            local_fatal=$(tail -3 "$FACTORY/ongoing/$proj/logs/runner.log" 2>/dev/null | grep "FATAL.*after 5 attempts")
            if [[ -n "$local_fatal" ]]; then
                log "  FATAL (max attempts) — NOT auto-restarting: $local_fatal"
            else
                start_project "$proj"
            fi
        fi
    done

    if [[ "$all_done" == "1" ]]; then
        log "All ablation projects complete. Monitor exiting."
        break
    fi

    sleep "$POLL"
done
