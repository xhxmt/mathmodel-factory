#!/usr/bin/env bash
set -euo pipefail

FACTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 🔒 Load secrets from GCP Secret Manager
if [[ -f "$FACTORY/scripts/load_secrets.sh" ]]; then
    source "$FACTORY/scripts/load_secrets.sh" 2>/dev/null || echo "Warning: Failed to load secrets from Secret Manager" >&2
fi
REGISTRY="$FACTORY/run_state/process_registry"
PAPER_STY="$FACTORY/resources/style/paper.sty"
BIB_BST="$FACTORY/resources/bib/bibliography.bst"
STYLE_JSON="$FACTORY/resources/style/model_papers_style.json"
ANALYSIS_GUIDE="$FACTORY/analysis_guide.md"
MODELING_GUIDE="$FACTORY/modeling_guide.md"
PAUSE_MARKER=".paused"
KILL_MARKER=".killed"

mkdir -p "$FACTORY/run_state" "$FACTORY/logs" "$FACTORY/ongoing" "$FACTORY/complete" "$FACTORY/papers"

project_dir() {
    echo "$FACTORY/ongoing/$1"
}

pause_file() {
    echo "$(project_dir "$1")/$PAUSE_MARKER"
}

kill_file() {
    echo "$(project_dir "$1")/$KILL_MARKER"
}

pid_file() {
    echo "$(project_dir "$1")/.runner.pid"
}

is_pid_live() {
    local pid="${1:-}"
    [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null
}

get_registered_pid() {
    local proj="$1"
    grep "^${proj} " "$REGISTRY" 2>/dev/null | awk '{print $2}' | tail -1 || true
}

update_registry_entry() {
    local proj="$1" pid="$2"
    mkdir -p "$(dirname "$REGISTRY")"
    touch "$REGISTRY"
    local tmp="${REGISTRY}.tmp.$$"
    grep -v "^${proj} " "$REGISTRY" > "$tmp" 2>/dev/null || true
    if [[ -n "$pid" ]]; then
        echo "$proj $pid" >> "$tmp"
    fi
    mv "$tmp" "$REGISTRY"
}

remove_registry_entry() {
    update_registry_entry "$1" ""
}

cleanup_project_lock() {
    local proj_dir="$1"
    rm -f "$proj_dir/.runner.lock.info" 2>/dev/null || true
    rm -f "$proj_dir/.runner.lock/info" 2>/dev/null || true
    rmdir "$proj_dir/.runner.lock" 2>/dev/null || true
}

submit_project() {
    local proj="$1"
    local proj_dir
    proj_dir="$(project_dir "$proj")"

    if [[ ! -f "$proj_dir/checkpoint.md" ]]; then
        echo "ERROR: No checkpoint.md in $proj_dir"
        return 1
    fi

    local current_pid
    current_pid="$(get_registered_pid "$proj")"
    if is_pid_live "$current_pid"; then
        echo "  $proj already running (pid $current_pid)"
        return 0
    fi

    mkdir -p "$FACTORY/logs" "$proj_dir/logs"

    local out
    out="$FACTORY/logs/${proj}_$(date +%Y%m%d_%H%M%S).out"
    (
        cd "$FACTORY"
        nohup "$FACTORY/run_paper.sh" "$proj_dir" >> "$out" 2>&1 &
        echo $! > "$(pid_file "$proj")"
    )

    local pid
    pid="$(cat "$(pid_file "$proj")")"
    update_registry_entry "$proj" "$pid"
    echo "  $proj -> pid $pid"
}

pause_project() {
    local proj="$1"
    local proj_dir
    proj_dir="$(project_dir "$proj")"

    if [[ ! -f "$proj_dir/checkpoint.md" ]]; then
        echo "ERROR: No checkpoint.md in $proj_dir"
        return 1
    fi

    touch "$(pause_file "$proj")"

    local pid=""
    if [[ -f "$(pid_file "$proj")" ]]; then
        pid="$(cat "$(pid_file "$proj")" 2>/dev/null || true)"
    fi
    [[ -z "$pid" ]] && pid="$(get_registered_pid "$proj")"

    if is_pid_live "$pid"; then
        kill "$pid" 2>/dev/null || true
        for _ in {1..10}; do
            if ! is_pid_live "$pid"; then
                break
            fi
            sleep 1
        done
        if is_pid_live "$pid"; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    fi

    remove_registry_entry "$proj"
    rm -f "$(pid_file "$proj")" 2>/dev/null || true
    cleanup_project_lock "$proj_dir"
    rm -f "$proj_dir/.heartbeat" 2>/dev/null || true
    echo "  $proj paused"
}

resume_project() {
    local proj="$1"
    local proj_dir
    proj_dir="$(project_dir "$proj")"

    if [[ ! -f "$proj_dir/checkpoint.md" ]]; then
        echo "ERROR: No checkpoint.md in $proj_dir"
        return 1
    fi

    if [[ -f "$(kill_file "$proj")" ]]; then
        echo "ERROR: $proj was killed by the viability gate and will not be resumed"
        return 1
    fi

    rm -f "$(pause_file "$proj")"
    cleanup_project_lock "$proj_dir"
    rm -f "$proj_dir/.heartbeat" 2>/dev/null || true
    submit_project "$proj"
}

usage() {
    cat <<EOF
Usage:
  ./launch_agents.sh new [--no-start] [--consult] <base_name> "question"
  ./launch_agents.sh <project> [project2] ...
  ./launch_agents.sh resume <project> [project2] ...
  ./launch_agents.sh pause <project> [project2] ...
  ./launch_agents.sh run <project>
  ./launch_agents.sh consult <project>
  ./launch_agents.sh attach <project>
  ./launch_agents.sh trace <project> [--lines N] [--follow]
  ./launch_agents.sh status
EOF
}

if [[ "${1:-}" == "trace" ]]; then
    shift
    exec python3 "$FACTORY/trace_viewer.py" "$@"
fi

if [[ "${1:-}" == "attach" ]]; then
    PROJ="${2:-}"
    if [[ -z "$PROJ" ]]; then
        echo "Usage: $0 attach <project>"
        exit 1
    fi
    LOG_FILE="$FACTORY/ongoing/$PROJ/logs/runner.log"
    if [[ ! -f "$LOG_FILE" ]]; then
        echo "No runner log found at $LOG_FILE"
        exit 1
    fi
    exec tail -f "$LOG_FILE"
fi

if [[ "${1:-}" == "status" ]]; then
    echo ""
    echo "=== Modeling Factory Status ==="

    _print_projects() {
        local section="$1" search_dir="$2"
        [[ -d "$search_dir" ]] || return
        local found=false
        for dir in "$search_dir"/*/; do
            [[ -d "$dir" ]] || continue
            local proj
            proj=$(basename "$dir")
            [[ -f "$dir/checkpoint.md" ]] || continue
            if ! $found; then
                echo ""
                echo "  $section"
                printf "  %-25s %-10s %-12s %-18s %s\n" \
                    "PROJECT" "INFERRED" "CHECKPOINT" "PROCESS" "TIMESTAMP"
                printf "  %-25s %-10s %-12s %-18s %s\n" \
                    "-------" "--------" "----------" "-------" "---------"
                found=true
            fi

            local inferred cp_step ts process pid
            inferred=$("$FACTORY/run_paper.sh" --infer-step "$dir" 2>/dev/null || echo "?")
            cp_step=$(grep "Last completed step" "$dir/checkpoint.md" 2>/dev/null \
                | grep -oP -- '-?\d+' | head -1 || echo "?")
            ts=$(grep "Timestamp" "$dir/checkpoint.md" 2>/dev/null \
                | sed 's/.*: //' || echo "")

            process="stopped"
            if [[ -f "$dir/$KILL_MARKER" ]]; then
                process="KILLED"
            elif [[ -f "$dir/.paused" ]]; then
                process="PAUSED"
            elif [[ -f "$dir/.awaiting_consultation" ]]; then
                local cstep
                cstep=$(grep -oP 'STEP:\K[0-9]+' "$dir/.awaiting_consultation" 2>/dev/null | head -1)
                process="CONSULT(${cstep:-?})"
            else
                pid=""
                [[ -f "$dir/.runner.pid" ]] && pid="$(cat "$dir/.runner.pid" 2>/dev/null || true)"
                [[ -z "$pid" ]] && pid="$(get_registered_pid "$proj")"
                if is_pid_live "$pid"; then
                    process="RUNNING($pid)"
                elif [[ -n "$pid" ]]; then
                    process="EXITED($pid)"
                fi
            fi

            printf "  %-25s %-10s %-12s %-18s %s\n" \
                "$proj" "$inferred" "$cp_step" "$process" "$ts"
        done
    }

    _print_projects "ONGOING" "$FACTORY/ongoing"
    _print_projects "COMPLETE" "$FACTORY/complete"
    echo ""
    exit 0
fi

if [[ "${1:-}" == "consult" ]]; then
    proj="${2:-}"
    [[ -n "$proj" ]] || { echo "Usage: $0 consult <project>"; exit 1; }
    d="$(project_dir "$proj")"
    [[ -d "$d" ]] || { echo "ERROR: no such project: $proj"; exit 1; }
    echo ""
    if [[ -f "$d/.awaiting_consultation" ]]; then
        echo "── $proj 正在等待人工咨询 ──"
        cat "$d/.awaiting_consultation"
        echo ""
    else
        echo "（$proj 当前没有挂起的咨询请求）"
    fi
    shopt -s nullglob
    for r in "$d"/consultation/*_request.md "$d"/consultation/REQUEST.md; do
        [[ -f "$r" ]] || continue
        echo "===================== $r ====================="
        cat "$r"
        echo ""
    done
    shopt -u nullglob
    echo "回填后运行： $0 resume $proj"
    echo ""
    exit 0
fi

if [[ "${1:-}" == "pause" ]]; then
    shift
    [[ $# -gt 0 ]] || { echo "Usage: $0 pause <project> [project2 ...]"; exit 1; }
    echo ""
    echo "Pausing $# project(s)..."
    for proj in "$@"; do
        pause_project "$proj"
    done
    echo ""
    exit 0
fi

if [[ "${1:-}" == "run" ]]; then
    proj="${2:-}"
    [[ -n "$proj" ]] || { echo "Usage: $0 run <project>"; exit 1; }
    exec "$FACTORY/run_paper.sh" "$FACTORY/ongoing/$proj"
fi

if [[ "${1:-}" == "new" ]]; then
    shift
    NO_START=0
    CONSULT=0
    while [[ "${1:-}" == --* ]]; do
        case "$1" in
            --no-start) NO_START=1; shift;;
            --consult)  CONSULT=1; shift;;
            *) echo "ERROR: unknown flag for 'new': $1"; exit 1;;
        esac
    done

    BASE_NAME="${1:?Usage: $0 new [--no-start] <base_name> \"Research question\"}"
    shift
    QUESTION="${*:?Usage: $0 new [--no-start] <base_name> \"Research question\"}"

    if [[ -d "$FACTORY/ongoing/$BASE_NAME" || -d "$FACTORY/complete/$BASE_NAME" ]]; then
        echo "ERROR: Project '$BASE_NAME' already exists."
        [[ -d "$FACTORY/ongoing/$BASE_NAME" ]] && echo "  (in ongoing/)"
        [[ -d "$FACTORY/complete/$BASE_NAME" ]] && echo "  (in complete/)"
        exit 1
    fi

    echo ""
    echo "Creating project: $BASE_NAME"
    echo "Question: $QUESTION"
    echo ""

    mkdir -p "$FACTORY/ongoing/$BASE_NAME"/{style,bib,figures,tables,do/archive,logs,replication,replication/temp,data/raw,data/intermediate,data/final,tmp,scripts,docs}
    cp "$PAPER_STY" "$FACTORY/ongoing/$BASE_NAME/style/"
    cp "$BIB_BST" "$FACTORY/ongoing/$BASE_NAME/bib/"
    cp "$STYLE_JSON" "$FACTORY/ongoing/$BASE_NAME/style/"
    cp "$ANALYSIS_GUIDE" "$FACTORY/ongoing/$BASE_NAME/"
    [[ -f "$MODELING_GUIDE" ]] && cp "$MODELING_GUIDE" "$FACTORY/ongoing/$BASE_NAME/"
    touch "$FACTORY/ongoing/$BASE_NAME/references.bib"

    cat > "$FACTORY/ongoing/$BASE_NAME/checkpoint.md" <<EOF
# Paper Skill Checkpoint

- **Base name**: $BASE_NAME
- **Project path**: $FACTORY/ongoing/$BASE_NAME
- **Research question**: $QUESTION
- **Last completed step**: -1
- **Timestamp**: $(date '+%Y-%m-%d %H:%M')
EOF

    if (( CONSULT )); then
        mkdir -p "$FACTORY/ongoing/$BASE_NAME/consultation"
        : > "$FACTORY/ongoing/$BASE_NAME/consultation/enabled"
        echo "Consultation window ENABLED (gates: preflight, step4, dynamic)."
        echo "  The pipeline will pause for your input at those points; fill"
        echo "  human_review.md (§CONSULT ...), set STATUS: READY, then resume."
    fi

    if (( NO_START )); then
        echo "Created project without starting the runner."
        echo "Commands:"
        echo "  $0 resume $BASE_NAME"
        echo "  $0 status"
        exit 0
    fi

    echo "Starting..."
    submit_project "$BASE_NAME"
    echo ""
    echo "Commands:"
    echo "  $0 status"
    echo "  $0 attach $BASE_NAME"
    echo "  tail -f $FACTORY/ongoing/$BASE_NAME/logs/runner.log"
    exit 0
fi

if [[ "${1:-}" == "resume" ]]; then
    shift
    [[ $# -gt 0 ]] || { echo "Usage: $0 resume <project> [project2 ...]"; exit 1; }
    PROJECTS=("$@")
else
    PROJECTS=("$@")
fi

if [[ ${#PROJECTS[@]} -eq 0 ]]; then
    for dir in "$FACTORY"/ongoing/*/; do
        [[ -d "$dir" ]] || continue
        proj=$(basename "$dir")
        [[ -f "$dir/checkpoint.md" ]] || continue
        [[ -f "$FACTORY/papers/${proj}_paper.pdf" ]] && continue
        [[ -f "$dir/$PAUSE_MARKER" ]] && continue
        [[ -f "$dir/$KILL_MARKER" ]] && continue
        PROJECTS+=("$proj")
    done
fi

if [[ ${#PROJECTS[@]} -eq 0 ]]; then
    usage
    exit 0
fi

echo ""
echo "Starting ${#PROJECTS[@]} project(s)..."
for proj in "${PROJECTS[@]}"; do
    resume_project "$proj"
done
echo ""
echo "Commands:"
echo "  $0 status"
echo "  $0 attach <project>"
echo "  tail -f $FACTORY/ongoing/<project>/logs/runner.log"
