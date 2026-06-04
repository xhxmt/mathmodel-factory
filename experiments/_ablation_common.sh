#!/usr/bin/env bash
# experiments/_ablation_common.sh — shared logic for the four ablation launchers.
#
# Sourced, not executed. Provides arg parsing and the create -> run -> evaluate
# flow so each ablation_*.sh launcher stays a few lines. See experiments/README.md.
#
# An ablation launcher sets two variables then calls al_main "$@":
#   ABLATION_VAR=ABLATE_NO_JUDGE     # the env var run_paper.sh reads
#   ABLATION_TAG=no_judge            # short tag used in project base names
#
# Default behavior runs the REAL multi-hour pipeline; use --dry-run to just print
# the commands (safe in tests / CI — creates nothing, calls no agent).

set -uo pipefail

FACTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Map a problem selector to an absolute input path. A|B|C|D -> benchmark PDF;
# anything else is treated as a literal absolute path to a .pdf/.md.
al_problem_path() {
    case "$1" in
        A|B|C|D) echo "$FACTORY/benchmark/cumcm_2024/$1题/$1题.pdf" ;;
        *)       echo "$1" ;;
    esac
}

al_usage() {
    cat >&2 <<EOF
Usage: $0 [--problem A|B|C|D|/abs/path.pdf] [--reps N] [--samples K]
          [--baseline] [--existing <ongoing_dir>] [--dry-run]

  --problem    benchmark problem (A/B/C/D) or absolute path (default: B)
  --reps N     repeats per variant (default: 3)
  --samples K  judge sampling rounds for run_evaluation.sh (default: 3)
  --baseline   also run a no-ablation control (<problem>_baseline_repK)
  --existing   reuse an existing ongoing/<base> project (skip creation)
  --dry-run    print the commands only; create/run nothing

This launcher toggles: $ABLATION_VAR (tag: $ABLATION_TAG)
EOF
    exit 2
}

# Parse common args into AL_* globals.
al_parse_args() {
    AL_PROBLEM="B"; AL_REPS=3; AL_SAMPLES=3
    AL_BASELINE=0; AL_EXISTING=""; AL_DRYRUN=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --problem)  AL_PROBLEM="${2:?--problem needs a value}"; shift 2 ;;
            --reps)     AL_REPS="${2:?--reps needs a value}"; shift 2 ;;
            --samples)  AL_SAMPLES="${2:?--samples needs a value}"; shift 2 ;;
            --baseline) AL_BASELINE=1; shift ;;
            --existing) AL_EXISTING="${2:?--existing needs a value}"; shift 2 ;;
            --dry-run)  AL_DRYRUN=1; shift ;;
            -h|--help)  al_usage ;;
            *) echo "Unknown arg: $1" >&2; al_usage ;;
        esac
    done
    case "$AL_REPS" in (*[!0-9]*|"") echo "ERROR: --reps must be a positive integer" >&2; exit 2 ;; esac
    case "$AL_SAMPLES" in (*[!0-9]*|"") echo "ERROR: --samples must be a positive integer" >&2; exit 2 ;; esac
    [ "$AL_REPS" -ge 1 ] || { echo "ERROR: --reps >= 1" >&2; exit 2; }
}

# Create a fresh project from the problem input (no auto-start).
# al_make_project <base> <problem_path>
al_make_project() {
    local base="$1" problem="$2"
    if [ "$AL_DRYRUN" -eq 1 ]; then
        echo "  [dry-run] ./launch_agents.sh new --no-start $base \"$problem\""
        return 0
    fi
    [ -f "$problem" ] || { echo "ERROR: problem input not found: $problem" >&2; return 1; }
    "$FACTORY/launch_agents.sh" new --no-start "$base" "$problem"
}

# Run one variant end to end: set the ablation env var, run the pipeline, evaluate.
# al_run <base> <env_assignment>  (env_assignment like "ABLATE_NO_JUDGE=1", or "" for baseline)
al_run() {
    local base="$1" env_assign="$2"
    local proj="$FACTORY/ongoing/$base"
    if [ "$AL_DRYRUN" -eq 1 ]; then
        echo "  [dry-run] ${env_assign:+$env_assign }./run_paper.sh ongoing/$base"
        echo "  [dry-run] ./evaluation/run_evaluation.sh ongoing/$base $base --samples $AL_SAMPLES --json"
        return 0
    fi
    echo ">>> running $base (${env_assign:-no ablation})"
    # shellcheck disable=SC2086 -- env_assign is a single KEY=VAL token or empty
    env $env_assign "$FACTORY/run_paper.sh" "$proj"
    echo ">>> evaluating $base"
    "$FACTORY/evaluation/run_evaluation.sh" "$proj" "$base" --samples "$AL_SAMPLES" --json
}

# Entry point: launchers call `al_main "$@"`.
al_main() {
    [ -n "${ABLATION_VAR:-}" ] && [ -n "${ABLATION_TAG:-}" ] || {
        echo "INTERNAL ERROR: launcher must set ABLATION_VAR and ABLATION_TAG" >&2; exit 3; }
    al_parse_args "$@"
    local problem_path; problem_path="$(al_problem_path "$AL_PROBLEM")"
    local problem_base="${AL_PROBLEM,,}"
    case "$AL_PROBLEM" in A|B|C|D) problem_base="cumcm2024${AL_PROBLEM,,}" ;; esac

    echo "=== Ablation: $ABLATION_TAG ($ABLATION_VAR=1) | problem=$AL_PROBLEM reps=$AL_REPS samples=$AL_SAMPLES ${AL_DRYRUN:+dry-run} ==="

    local k base
    for k in $(seq 1 "$AL_REPS"); do
        if [ -n "$AL_EXISTING" ]; then
            base="$(basename "$AL_EXISTING")"
        else
            base="${problem_base}_${ABLATION_TAG}_rep${k}"
            al_make_project "$base" "$problem_path" || exit 1
        fi
        al_run "$base" "${ABLATION_VAR}=1"
        [ -n "$AL_EXISTING" ] && break   # existing project: single run only
    done

    if [ "$AL_BASELINE" -eq 1 ] && [ -z "$AL_EXISTING" ]; then
        echo "=== Baseline control (no ablation) ==="
        for k in $(seq 1 "$AL_REPS"); do
            base="${problem_base}_baseline_rep${k}"
            al_make_project "$base" "$problem_path" || exit 1
            al_run "$base" ""
        done
    fi

    echo "=== done. Compare with: experiments/compare_ablations.py --baseline <base> --variant <base> ==="
}
