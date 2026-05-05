#!/usr/bin/env bash
set -euo pipefail

FACTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATA_WRAPPER="$FACTORY/stata_wrapper.sh"
JOB_DIR="$FACTORY/run_state/stata_jobs"
mkdir -p "$JOB_DIR"

usage() {
    cat <<'EOF'
Usage:
  stata_submit.sh do/filename.do
  stata_submit.sh --time 04:00:00 do/file.do
  stata_submit.sh --status <jobid>
  stata_submit.sh --wait <jobid>
  stata_submit.sh --dry-run do/file.do
EOF
}

job_meta() {
    echo "$JOB_DIR/$1.meta"
}

job_field() {
    local jobid="$1" key="$2"
    awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, "", $0); print $0; exit}' "$(job_meta "$jobid")" 2>/dev/null || true
}

job_status() {
    local jobid="$1"
    local meta
    meta="$(job_meta "$jobid")"
    [[ -f "$meta" ]] || { echo "UNKNOWN"; return 1; }

    local pid log_file moved_log
    pid="$(job_field "$jobid" pid)"
    log_file="$(job_field "$jobid" log_file)"
    moved_log="$(job_field "$jobid" moved_log)"

    if [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        echo "RUNNING"
        return 0
    fi

    if [[ -n "$moved_log" && -f "$moved_log" ]]; then
        log_file="$moved_log"
    fi

    if [[ -n "$log_file" && -f "$log_file" ]]; then
        if grep -Eq '^r\([0-9]+\);' "$log_file"; then
            echo "FAILED"
        elif grep -q 'end of do-file' "$log_file"; then
            echo "COMPLETED"
        else
            echo "EXITED"
        fi
    else
        echo "UNKNOWN"
    fi
}

TIME_OVERRIDE=""
DRY_RUN=false
STATUS_JOB=""
WAIT_JOB=""
DOFILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --time)    TIME_OVERRIDE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        --status)  STATUS_JOB="$2"; shift 2 ;;
        --wait)    WAIT_JOB="$2"; shift 2 ;;
        -b)        shift ;;
        do)        shift ;;
        -*)        echo "Unknown option: $1" >&2; exit 1 ;;
        *)         DOFILE="$1"; shift ;;
    esac
done

if [[ -n "$STATUS_JOB" ]]; then
    job_status "$STATUS_JOB"
    exit 0
fi

if [[ -n "$WAIT_JOB" ]]; then
    while true; do
        state="$(job_status "$WAIT_JOB")"
        echo "$state"
        [[ "$state" == "RUNNING" ]] || break
        sleep 5
    done
    [[ "$state" != "FAILED" ]]
    exit $?
fi

if [[ -z "$DOFILE" ]]; then
    usage >&2
    exit 1
fi

if [[ ! "$DOFILE" = /* ]]; then
    DOFILE="$(pwd)/$DOFILE"
fi

if [[ ! -f "$DOFILE" ]]; then
    echo "ERROR: do-file not found: $DOFILE" >&2
    exit 1
fi

WORKDIR="$(cd "$(dirname "$DOFILE")" && pwd)"
if [[ "$(basename "$WORKDIR")" == "do" ]]; then
    WORKDIR="$(dirname "$WORKDIR")"
fi

DOFILE_BASE="$(basename "$DOFILE" .do)"
STDERR_LOG="$WORKDIR/logs/${DOFILE_BASE}_stderr.log"
PRIMARY_LOG="$WORKDIR/${DOFILE_BASE}.log"
MOVED_LOG="$WORKDIR/logs/${DOFILE_BASE}.log"
JOBID="local_$(date +%Y%m%d%H%M%S)_$$"
META_FILE="$(job_meta "$JOBID")"

CMD=( "$STATA_WRAPPER" -b do "$DOFILE" )

if $DRY_RUN; then
    printf 'TIME_OVERRIDE=%s\n' "${TIME_OVERRIDE:-none}"
    printf 'WORKDIR=%s\n' "$WORKDIR"
    printf 'COMMAND=%q ' "${CMD[@]}"
    printf '\n'
    exit 0
fi

mkdir -p "$WORKDIR/logs"
(
    cd "$WORKDIR"
    nohup "${CMD[@]}" >/dev/null 2>"$STDERR_LOG" < /dev/null &
    pid=$!
    {
        echo "pid=$pid"
        echo "dofile=$DOFILE"
        echo "workdir=$WORKDIR"
        echo "log_file=$PRIMARY_LOG"
        echo "moved_log=$MOVED_LOG"
        echo "stderr_log=$STDERR_LOG"
        echo "requested_time=${TIME_OVERRIDE:-}"
        echo "started=$(date +%s)"
    } > "$META_FILE"
)

echo "$JOBID"
