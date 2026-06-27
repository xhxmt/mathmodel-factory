#!/usr/bin/env bash
set -euo pipefail

FACTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 🔒 Load secrets from GCP Secret Manager
if [[ -f "$FACTORY/scripts/load_secrets.sh" ]]; then
    source "$FACTORY/scripts/load_secrets.sh" 2>/dev/null || echo "Warning: Failed to load secrets from Secret Manager" >&2
fi
PAPER_STY="$FACTORY/resources/style/paper.sty"
BIB_BST="$FACTORY/resources/bib/bibliography.bst"
STYLE_JSON="$FACTORY/resources/style/model_papers_style.json"
ANALYSIS_GUIDE="$FACTORY/analysis_guide.md"
MODELING_GUIDE="$FACTORY/modeling_guide.md"

mkdir -p "$FACTORY/run_state" "$FACTORY/logs" "$FACTORY/ongoing" "$FACTORY/complete" "$FACTORY/papers"

project_dir() {
    echo "$FACTORY/ongoing/$1"
}

submit_project() {
    local proj="$1"
    local proj_dir
    proj_dir="$(project_dir "$proj")"

    if [[ ! -f "$proj_dir/checkpoint.md" ]]; then
        echo "ERROR: No checkpoint.md in $proj_dir"
        return 1
    fi

    mkdir -p "$FACTORY/logs" "$proj_dir/logs"

    local out
    out="$FACTORY/logs/${proj}_$(date +%Y%m%d_%H%M%S).out"
    (
        cd "$FACTORY"
        nohup "$FACTORY/run_paper.sh" "$proj_dir" >> "$out" 2>&1 &
        echo $! > "$proj_dir/.runner.pid"
    )
    local pid
    pid="$(cat "$proj_dir/.runner.pid")"
    echo "  $proj -> pid $pid"
}

usage() {
    cat <<EOF
Usage:
  ./launch_agents.sh new [--no-start] [--consult] <base_name> "question"
  ./launch_agents.sh <project> [project2] ...
  ./launch_agents.sh resume <project> [project2] ...
  ./launch_agents.sh pause <project> [project2] ...
  ./launch_agents.sh kill <project> [project2] ...
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
    exec python3 "$FACTORY/scripts/project_ctl.py" status --factory-root "$FACTORY"
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
        proj_dir="$(project_dir "$proj")"
        [[ -f "$proj_dir/checkpoint.md" ]] || { echo "ERROR: No checkpoint.md in $proj_dir"; exit 1; }
        python3 "$FACTORY/scripts/project_ctl.py" pause "$proj_dir" "$proj" --factory-root "$FACTORY" >/dev/null
        echo "  $proj paused"
    done
    echo ""
    exit 0
fi

if [[ "${1:-}" == "kill" ]]; then
    shift
    [[ $# -gt 0 ]] || { echo "Usage: $0 kill <project> [project2 ...]"; exit 1; }
    echo ""
    echo "Killing $# project(s)..."
    for proj in "$@"; do
        proj_dir="$(project_dir "$proj")"
        [[ -f "$proj_dir/checkpoint.md" ]] || { echo "ERROR: No checkpoint.md in $proj_dir"; exit 1; }
        python3 "$FACTORY/scripts/project_ctl.py" kill "$proj_dir" --factory-root "$FACTORY" >/dev/null
        echo "  $proj killed"
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
    proj_dir="$(project_dir "$proj")"
    [[ -f "$proj_dir/checkpoint.md" ]] || { echo "ERROR: No checkpoint.md in $proj_dir"; exit 1; }
    python3 "$FACTORY/scripts/project_ctl.py" resume "$proj_dir" "$proj" --factory-root "$FACTORY" >/dev/null
done
echo ""
echo "Commands:"
echo "  $0 status"
echo "  $0 attach <project>"
echo "  tail -f $FACTORY/ongoing/<project>/logs/runner.log"
