#!/usr/bin/env bash
set -euo pipefail

FACTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEGACY="$FACTORY/legacy/shell/launch_agents_legacy.sh"

engine_project() {
    [[ -f "$FACTORY/ongoing/$1/.factory/state.db" ]]
}

case "${1:-}" in
    new)
        shift
        no_start=0
        consult=0
        while [[ "${1:-}" == --* ]]; do
            case "$1" in
                --no-start) no_start=1 ;;
                --consult) consult=1 ;;
                *) echo "ERROR: unknown flag for new: $1" >&2; exit 2 ;;
            esac
            shift
        done
        base="${1:?Usage: $0 new [--no-start] [--consult] <base_name> <question>}"
        shift
        question="${*:?research question is required}"
        args=(python3 -m factory_core.cli create "$base" "$question")
        (( consult )) && args+=(--consult)
        (( no_start )) || args+=(--start)
        exec "${args[@]}"
        ;;
    pause|kill)
        action="$1"
        shift
        [[ $# -gt 0 ]] || { echo "Usage: $0 $action <project> [...]" >&2; exit 2; }
        for project in "$@"; do
            if engine_project "$project"; then
                python3 -m factory_core.cli action "$action" "$FACTORY/ongoing/$project"
            else
                "$LEGACY" "$action" "$project"
            fi
        done
        ;;
    resume)
        shift
        [[ $# -gt 0 ]] || { echo "Usage: $0 resume <project> [...]" >&2; exit 2; }
        for project in "$@"; do
            if engine_project "$project"; then
                python3 -m factory_core.cli action resume "$FACTORY/ongoing/$project"
            else
                "$LEGACY" resume "$project"
            fi
        done
        ;;
    run)
        project="${2:?Usage: $0 run <project>}"
        exec "$FACTORY/run_paper.sh" "$FACTORY/ongoing/$project"
        ;;
    status|trace|attach|consult|"")
        exec "$LEGACY" "$@"
        ;;
    *)
        # Historical "launch one or more projects" form.
        exec "$LEGACY" "$@"
        ;;
esac
