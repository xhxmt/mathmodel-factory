#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────
# solver_submit.sh — Multi-solver async job submitter
#
# Replaces stata_submit.sh for the modeling-factory workflow. Wraps
# python / julia / matlab / Rscript / gurobi_cl invocations behind a
# uniform jobid-based interface so prompts can submit work without
# blocking and poll for completion.
#
# CLI:
#   solver_submit.sh --type <python|julia|matlab|R|gurobi> <script>
#       [--max-time <seconds>]
#       [--args "arg1 arg2 ..."]
#   solver_submit.sh --status <jobid>
#   solver_submit.sh --wait <jobid>
#   solver_submit.sh --dry-run --type <type> <script> [...]
#
# Job state files live at run_state/solver_jobs/<jobid>.meta with
# key=value lines:
#   pid=12345
#   type=python
#   script=/abs/path/to/script.py
#   workdir=/abs/path
#   stdout_log=/abs/path/to/script.log
#   stderr_log=/abs/path/to/logs/script_stderr.log
#   exit_code_file=/abs/path/to/.solver_jobs/<jobid>.exit
#   max_time=300
#   started=1716060000
#
# Status transitions:
#   pid alive                              → RUNNING
#   exit file missing, pid gone            → EXITED (unknown)
#   exit file present, exit code 0         → COMPLETED
#   exit file present, exit code 124       → TIMEOUT (from `timeout`)
#   exit file present, exit code != 0      → FAILED
#
# Soft-failure markers for matlab/gurobi (e.g. exit 0 with "Error using"
# in matlab, or "Infeasible" in gurobi) are intentionally NOT classified
# as FAILED — they reflect modeling-level outcomes the caller must
# interpret. Use --status to get the raw code, then inspect the log.
# ─────────────────────────────────────────────────────────────────────

FACTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JOB_DIR="$FACTORY/run_state/solver_jobs"
# Capture the operator-level quarantine before loading any project-owned
# `.env.cloud`; a project must never be able to disable a global safety stop.
GLOBAL_CLOUD_QUARANTINE="${CLOUD_SOLVER_QUARANTINED:-true}"
mkdir -p "$JOB_DIR"

find_project_cloud_env() {
    local dir="$1"
    while [[ -n "$dir" && "$dir" != "/" ]]; do
        if [[ -f "$dir/.env.cloud" ]]; then
            echo "$dir/.env.cloud"
            return 0
        fi
        if [[ "$dir" == "$FACTORY" ]]; then
            break
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

load_project_cloud_env() {
    local workdir="$1"
    local env_file
    env_file="$(find_project_cloud_env "$workdir" 2>/dev/null || true)"
    if [[ -n "$env_file" ]]; then
        local env_size
        env_size="$(stat -c '%s' "$env_file" 2>/dev/null || echo 0)"
        if [[ ! "$env_size" =~ ^[0-9]+$ ]] || (( env_size > 4096 )); then
            echo "[solver_submit] Cloud config exceeds 4 KiB; forcing local solver" >&2
            USE_CLOUD_SOLVER=false
            export USE_CLOUD_SOLVER
            return 0
        fi
        local line key value invalid=false
        declare -A parsed_cloud_env=()
        while IFS= read -r line || [[ -n "$line" ]]; do
            line="${line%$'\r'}"
            [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
            if [[ "$line" != *=* ]]; then
                invalid=true
                continue
            fi
            key="${line%%=*}"
            value="${line#*=}"
            case "$key" in
                USE_CLOUD_SOLVER)
                    [[ "$value" == "true" || "$value" == "false" ]] || invalid=true
                    ;;
                CLOUD_THRESHOLD_TIME)
                    [[ "$value" =~ ^[1-9][0-9]{0,3}$ ]] && (( value <= 3600 )) || invalid=true
                    ;;
                CLOUD_SOLVER_TYPES)
                    [[ "$value" == "python" ]] || invalid=true
                    ;;
                *)
                    echo "[solver_submit] Ignoring unsupported cloud config key" >&2
                    continue
                    ;;
            esac
            parsed_cloud_env["$key"]="$value"
        done < "$env_file"

        if [[ "$invalid" == "true" ]]; then
            echo "[solver_submit] Invalid cloud config; forcing local solver: $env_file" >&2
            USE_CLOUD_SOLVER=false
            export USE_CLOUD_SOLVER
            return 0
        fi
        for key in "${!parsed_cloud_env[@]}"; do
            printf -v "$key" '%s' "${parsed_cloud_env[$key]}"
            export "$key"
        done
        echo "[solver_submit] Loaded cloud config: $env_file" >&2
    fi
}

should_use_cloud() {
    local type="$1" max_time="$2"
    local use_cloud="${USE_CLOUD_SOLVER:-false}"
    local quarantined="$GLOBAL_CLOUD_QUARANTINE"
    local threshold="${CLOUD_THRESHOLD_TIME:-300}"
    local solver_types="${CLOUD_SOLVER_TYPES:-python}"
    local capability_manifest="${CLOUD_SOLVER_CAPABILITIES_FILE:-$FACTORY/cloud/runtime_capabilities.json}"
    local fallback_marker="${CLOUD_SOLVER_FALLBACK_MARKER:-$FACTORY/run_state/cloud_solver_fallback.marker}"

    [[ "$quarantined" != "true" ]] || return 1
    [[ "$use_cloud" == "true" ]] || return 1
    [[ ! -f "$fallback_marker" ]] || return 1
    [[ ",$solver_types," == *",$type,"* ]] || return 1
    command -v jq >/dev/null 2>&1 || return 1
    [[ -f "$capability_manifest" ]] || return 1
    jq -e --arg solver_type "$type" \
        '.runtimes[$solver_type].enabled == true' "$capability_manifest" >/dev/null 2>&1 || return 1
    [[ "$max_time" =~ ^[0-9]+$ ]] || return 1
    (( max_time >= threshold ))
}

route_to_cloud() {
    local type="$1" max_time="$2" script="$3"
    local cloud_client="${CLOUD_SOLVER_CLIENT:-$FACTORY/scripts/gcp_solver_client.sh}"
    local stdout_log="$WORKDIR/${SCRIPT_STEM}.log"
    local stderr_log="$WORKDIR/logs/${SCRIPT_STEM}_stderr.log"
    local jobid="cloud_${type}_$(date +%Y%m%d%H%M%S)_$$"
    local exit_file="$JOB_DIR/${jobid}.exit"
    local meta_file
    meta_file="$(job_meta "$jobid")"
    local solver_wrapper="$FACTORY/solver_wrapper.sh"

    echo "[solver_submit] Routing to Cloud Run (max_time=${max_time}s)" >&2
    mkdir -p "$WORKDIR/logs"
    (
        cd "$WORKDIR"
        setsid "$solver_wrapper" "$exit_file" "$cloud_client" --type "$type" --max-time "$max_time" "$script" \
            > "$stdout_log" 2> "$stderr_log" < /dev/null &
        bg_pid=$!
        {
            echo "pid=$bg_pid"
            echo "type=$type"
            echo "backend=cloud_run"
            echo "script=$script"
            echo "workdir=$WORKDIR"
            echo "stdout_log=$stdout_log"
            echo "stderr_log=$stderr_log"
            echo "exit_code_file=$exit_file"
            echo "max_time=$max_time"
            echo "started=$(date +%s)"
        } > "$meta_file"
        disown "$bg_pid" 2>/dev/null || true
    )
    echo "$jobid"
    exit 0
}

usage() {
    cat <<'EOF'
Usage:
  solver_submit.sh --type <type> <script> [--max-time <sec>] [--args "..."]
  solver_submit.sh --status <jobid> [--json]
  solver_submit.sh --wait <jobid>
  solver_submit.sh --dry-run --type <type> <script> [...]

Supported --type values:
  python   → python3 <script> [args...]
  julia    → julia <script> [args...]
  matlab   → matlab -batch "run('<script>'); exit"  (args ignored)
  R        → Rscript <script> [args...]
  gurobi   → gurobi_cl <script> [args...]  (script is usually .lp/.mps)

Options:
  --max-time <sec>   Wall-clock limit. Job is killed with TERM then KILL.
  --args "..."       Extra argv passed to the script (where supported).
  --dry-run          Print what would run, don't launch.
  --json             With --status, print allowlisted job evidence as JSON.

Returns:
  On submit: prints a jobid on stdout (local_<TYPE>_<TS>_<PID> or cloud_<TYPE>_<TS>_<PID>).
  On --status: prints one of RUNNING / COMPLETED / FAILED / TIMEOUT / EXITED / UNKNOWN.
  On --wait: polls every 5s and prints state changes; exit 0 if final state COMPLETED,
             else nonzero.
EOF
}

job_meta() {
    echo "$JOB_DIR/$1.meta"
}

job_field() {
    local jobid="$1" key="$2"
    awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, "", $0); print $0; exit}' \
        "$(job_meta "$jobid")" 2>/dev/null || true
}

job_status() {
    local jobid="$1"
    local meta
    meta="$(job_meta "$jobid")"
    [[ -f "$meta" ]] || { echo "UNKNOWN"; return 1; }

    local pid exit_file
    pid="$(job_field "$jobid" pid)"
    exit_file="$(job_field "$jobid" exit_code_file)"

    if [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        echo "RUNNING"
        return 0
    fi

    if [[ -z "$exit_file" || ! -f "$exit_file" ]]; then
        echo "EXITED"
        return 0
    fi

    local code
    code="$(cat "$exit_file" 2>/dev/null || true)"
    case "$code" in
        0)   echo "COMPLETED" ;;
        124) echo "TIMEOUT"   ;;
        '')  echo "EXITED"    ;;
        *)   echo "FAILED"    ;;
    esac
}

job_evidence_json() {
    local jobid="$1"
    local meta
    meta="$(job_meta "$jobid")"
    [[ -f "$meta" ]] || { echo "UNKNOWN"; return 1; }

    local status backend runtime script workdir max_time started
    status="$(job_status "$jobid")"
    backend="$(job_field "$jobid" backend)"
    if [[ -z "$backend" ]]; then
        case "$jobid" in
            cloud_*) backend="cloud_run" ;;
            *)       backend="local" ;;
        esac
    fi
    runtime="$(job_field "$jobid" type)"
    script="$(job_field "$jobid" script)"
    workdir="$(job_field "$jobid" workdir)"
    max_time="$(job_field "$jobid" max_time)"
    started="$(job_field "$jobid" started)"

    python3 - "$jobid" "$backend" "$runtime" "$script" "$workdir" \
        "$status" "$max_time" "$started" <<'PY'
import json
import sys


def integer_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


job_id, backend, runtime, script, workdir, status, max_time, started = sys.argv[1:]
print(
    json.dumps(
        {
            "schema": "solver-job-evidence-v1",
            "job_id": job_id,
            "backend": backend,
            "runtime": runtime,
            "script": script,
            "workdir": workdir,
            "status": status,
            "max_time_seconds": integer_or_none(max_time),
            "requested_at": integer_or_none(started),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
)
PY
}

build_command() {
    # Print the command argv (one token per line) for the given solver type.
    local type="$1" script="$2"
    shift 2
    case "$type" in
        python)
            printf '%s\n' python3 "$script" "$@"
            ;;
        julia)
            printf '%s\n' julia "$script" "$@"
            ;;
        R|r|rscript|Rscript)
            printf '%s\n' Rscript "$script" "$@"
            ;;
        matlab)
            # MATLAB R2019a+ supports -batch; older may need -nodisplay -r.
            # We use -batch here; users with older MATLAB can wrap manually.
            local stem
            stem="$(basename "$script")"
            stem="${stem%.m}"
            printf '%s\n' matlab -batch "cd('$(dirname "$script")'); ${stem}"
            ;;
        gurobi)
            printf '%s\n' gurobi_cl "$@" "$script"
            ;;
        *)
            echo "ERROR: unknown --type: $type" >&2
            return 1
            ;;
    esac
}

# ── Argument parsing ────────────────────────────────────────────────────

TYPE=""
SCRIPT=""
MAX_TIME=""
EXTRA_ARGS=""
DRY_RUN=false
STATUS_JOB=""
WAIT_JOB=""
JSON_OUTPUT=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --type)     TYPE="$2"; shift 2 ;;
        --max-time) MAX_TIME="$2"; shift 2 ;;
        --args)     EXTRA_ARGS="$2"; shift 2 ;;
        --dry-run)  DRY_RUN=true; shift ;;
        --status)   STATUS_JOB="$2"; shift 2 ;;
        --wait)     WAIT_JOB="$2"; shift 2 ;;
        --json)     JSON_OUTPUT=true; shift ;;
        -h|--help)  usage; exit 0 ;;
        -*)         echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
        *)          SCRIPT="$1"; shift ;;
    esac
done

if [[ -n "$STATUS_JOB" ]]; then
    if $JSON_OUTPUT; then
        job_evidence_json "$STATUS_JOB"
    else
        job_status "$STATUS_JOB"
    fi
    exit 0
fi

if [[ -n "$WAIT_JOB" ]]; then
    state=""
    prev=""
    while true; do
        state="$(job_status "$WAIT_JOB")"
        if [[ "$state" != "$prev" ]]; then
            echo "$state"
            prev="$state"
        fi
        [[ "$state" == "RUNNING" ]] || break
        sleep 5
    done
    [[ "$state" == "COMPLETED" ]]
    exit $?
fi

if [[ -z "$TYPE" || -z "$SCRIPT" ]]; then
    usage >&2
    exit 1
fi

# Normalize script to absolute path
if [[ ! "$SCRIPT" = /* ]]; then
    SCRIPT="$(pwd)/$SCRIPT"
fi
if [[ ! -f "$SCRIPT" ]]; then
    echo "ERROR: script not found: $SCRIPT" >&2
    exit 1
fi

WORKDIR="$(cd "$(dirname "$SCRIPT")" && pwd)"
SCRIPT_BASE="$(basename "$SCRIPT")"
SCRIPT_STEM="${SCRIPT_BASE%.*}"
EFFECTIVE_MAX_TIME="${MAX_TIME:-1800}"
load_project_cloud_env "$WORKDIR"

if should_use_cloud "$TYPE" "$EFFECTIVE_MAX_TIME"; then
    route_to_cloud "$TYPE" "$EFFECTIVE_MAX_TIME" "$SCRIPT"
fi

# Parse extra args (split on whitespace; agents passing complex argv
# should use --args "a b 'c d'" — quoting is left to shell expansion).
EXTRA_ARRAY=()
if [[ -n "$EXTRA_ARGS" ]]; then
    # shellcheck disable=SC2206
    EXTRA_ARRAY=( $EXTRA_ARGS )
fi

# Resolve the inner command.
# Note: process substitution masks build_command's exit code, so we
# validate emptiness afterwards to surface unknown-type errors.
mapfile -t INNER_CMD < <(build_command "$TYPE" "$SCRIPT" "${EXTRA_ARRAY[@]}")
if (( ${#INNER_CMD[@]} == 0 )); then
    echo "ERROR: failed to build command for --type $TYPE" >&2
    exit 1
fi

# Apply timeout wrapper if --max-time given
if [[ -n "$MAX_TIME" ]]; then
    if ! [[ "$MAX_TIME" =~ ^[0-9]+$ ]]; then
        echo "ERROR: --max-time must be a positive integer (seconds)" >&2
        exit 1
    fi
    WRAPPED_CMD=( timeout --kill-after=10 "$MAX_TIME" "${INNER_CMD[@]}" )
else
    WRAPPED_CMD=( "${INNER_CMD[@]}" )
fi

STDOUT_LOG="$WORKDIR/${SCRIPT_STEM}.log"
STDERR_LOG="$WORKDIR/logs/${SCRIPT_STEM}_stderr.log"
JOBID="local_${TYPE}_$(date +%Y%m%d%H%M%S)_$$"
EXIT_FILE="$JOB_DIR/${JOBID}.exit"
META_FILE="$(job_meta "$JOBID")"

if $DRY_RUN; then
    printf 'TYPE=%s\n' "$TYPE"
    printf 'SCRIPT=%s\n' "$SCRIPT"
    printf 'WORKDIR=%s\n' "$WORKDIR"
    printf 'MAX_TIME=%s\n' "${MAX_TIME:-none}"
    printf 'STDOUT_LOG=%s\n' "$STDOUT_LOG"
    printf 'STDERR_LOG=%s\n' "$STDERR_LOG"
    printf 'COMMAND='
    printf '%q ' "${WRAPPED_CMD[@]}"
    printf '\n'
    exit 0
fi

mkdir -p "$WORKDIR/logs"

# Launch via solver_wrapper.sh so $! is the pid of a process that
# survives this script's exit and is recoverable by --status. setsid
# puts the wrapper in its own session/process group so it survives if
# a parent agent does `kill -- -PGID` cleanup on its child group (codex
# exec_command does this between turns, which previously killed the
# wrapper before it could write EXIT_FILE).
SOLVER_WRAPPER="$FACTORY/solver_wrapper.sh"
(
    cd "$WORKDIR"
    setsid "$SOLVER_WRAPPER" "$EXIT_FILE" "${WRAPPED_CMD[@]}" \
        > "$STDOUT_LOG" 2> "$STDERR_LOG" < /dev/null &
    bg_pid=$!
    {
        echo "pid=$bg_pid"
        echo "type=$TYPE"
        echo "script=$SCRIPT"
        echo "workdir=$WORKDIR"
        echo "stdout_log=$STDOUT_LOG"
        echo "stderr_log=$STDERR_LOG"
        echo "exit_code_file=$EXIT_FILE"
        echo "max_time=${MAX_TIME:-}"
        echo "started=$(date +%s)"
    } > "$META_FILE"
    disown "$bg_pid" 2>/dev/null || true
)

echo "$JOBID"
