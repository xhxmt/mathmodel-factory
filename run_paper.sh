#!/bin/bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────
# run_paper.sh — Step-level paper runner
#
# Runs each of the 16 paper-skill steps in a FRESH Claude session,
# eliminating context-window exhaustion as a failure mode. Infers
# actual progress from file state (not just checkpoint.md), auto-
# retries failed steps, and writes heartbeat files for the watchdog.
#
# Usage:
#   ./run_paper.sh <project_dir>              Run / resume a paper
#   ./run_paper.sh --infer-step <dir>         Print inferred step number
#   ./run_paper.sh --status <dir>             One-line status summary
# ─────────────────────────────────────────────────────────────────────

# Honor FACTORY passed via env (set by the snapshot re-exec below) so
# the per-job snapshot copy still resolves to the real factory root for
# $FACTORY/prompts/*, $FACTORY/scripts/*, etc.  Fall back to deriving
# from BASH_SOURCE for direct invocations.
FACTORY="${FACTORY:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
SKILL="$FACTORY/STEPS.md"
MAX_RETRIES=5
REGISTRY="$FACTORY/run_state/process_registry"
KILL_MARKER=".killed"
REVIEW_STATE_FILE=".review_state.json"

# Source $FACTORY/.env if present so subprocesses (the google-antigravity
# SDK in scripts/agy_run.py, the MinerU client, etc.) see secrets like
# GEMINI_API_KEY / MINERU_TOKEN.  The file is gitignored (see .gitignore
# .env exclusion) so secrets never leak into the repo.  Format is plain
# KEY=value lines — same shape as .env.example.
if [[ -f "$FACTORY/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "$FACTORY/.env"
    set +a
fi

export PATH="$HOME/local/node/bin:$PATH"

# ── File-state step inference ────────────────────────────────────────
#
# Determines the last completed step by checking which output files
# exist. This is the authoritative source of progress — checkpoint.md
# may lag behind if the orchestrator died before updating it.

_lines() { wc -l < "$1" 2>/dev/null || echo 0; }
_chars() { wc -c < "$1" 2>/dev/null || echo 0; }
_mtime() { stat -c %Y "$1" 2>/dev/null || echo 0; }
_is_newer_by() {
    local newer="$1" older="$2" min_delta="${3:-1}"
    local newer_ts older_ts
    newer_ts=$(_mtime "$newer")
    older_ts=$(_mtime "$older")
    (( newer_ts > 0 && older_ts > 0 && newer_ts - older_ts >= min_delta ))
}

_kill_process_tree() {
    local root="${1:-}" sig="${2:-TERM}" kid
    [[ -n "$root" ]] || return 0
    for kid in $(pgrep -P "$root" 2>/dev/null || true); do
        _kill_process_tree "$kid" "$sig"
    done
    kill -s "$sig" "$root" 2>/dev/null || true
}

project_setup_complete() {
    local P="$1"
    local cp_step=""
    [[ -f "$P/checkpoint.md" ]] || return 1
    # Modeling-mode setup: produces a structured problem/ directory.
    # Social-mode setup: produces project_brief.md at the project root.
    if [[ -f "$P/problem/problem_brief.md" ]]; then
        :
    elif [[ -f "$P/project_brief.md" ]]; then
        :
    else
        return 1
    fi
    cp_step=$(grep "Last completed step" "$P/checkpoint.md" 2>/dev/null \
        | grep -oP -- '-?\d+' | head -1 || true)
    [[ "$cp_step" == "0" ]]
}

# Is this project a math-modeling project (Modeling Factory) rather than the
# original social-science Paper Factory?  Two signals:
#   1. The seeded research-question is an absolute path to a PDF / MD (i.e. a
#      competition problem file passed to `launch_agents.sh new`).
#   2. The project directory already contains problem/source.md (e.g. from a
#      previous setup pass or a manual seed).
is_modeling_input() {
    local P="$1" Q="$2"
    [[ -f "$P/problem/source.md" ]] && return 0
    case "$Q" in
        /*.pdf|/*.PDF|/*.md|/*.MD)
            [[ -f "$Q" ]] && return 0 ;;
    esac
    return 1
}

# After Step 0 (problem parsing) has run, the project always has a problem/
# directory.  This is the canonical signal that the project follows the
# math-modeling pipeline (Steps 1-16 modeling variants) rather than the
# legacy social-science pipeline.  Used by infer_step's modeling-mode
# short-circuit and by any future dispatcher branching.
is_modeling_project() { [[ -d "$1/problem" ]]; }

# HMML-lite hard gate.  Every method_library/<...>.md path cited in the given
# files MUST be a method registered in method_library/index.json (and the
# registry itself must be structurally valid — method_retrieve.py validates
# that on every run).  Echoes a report to stdout and returns non-zero on any
# unregistered/invalid citation.  Tolerates a missing retriever or index
# (returns 0) so social-mode projects and partial checkouts are unaffected.
check_method_citations() {
    local P="$1"; shift
    local script="$FACTORY/scripts/method_retrieve.py"
    local index="$FACTORY/method_library/index.json"
    [[ -f "$script" && -f "$index" ]] || return 0
    local -a args=() f
    for f in "$@"; do
        [[ -f "$f" ]] && args+=( --check-citations "$f" )
    done
    (( ${#args[@]} )) || return 0
    python3 "$script" "${args[@]}" 2>&1
}

analysis_ready_dirs() {
    local P="$1"
    local dir
    for dir in "$P/data/final" "$P/analysis/final" "$P/analysis/unified"; do
        [[ -d "$dir" ]] && printf '%s\n' "$dir"
    done
}

project_has_analysis_ready_data() {
    local P="$1"
    local dir
    while IFS= read -r dir; do
        [[ -z "$dir" ]] && continue
        # Analysis outputs often live in one level of subdirectories under
        # data/final (for example yearly shards or chunked .dta exports).
        # Search recursively and stop at the first qualifying file.
        if find "$dir" -type f \
            \( -name '*.dta' -o -name '*.parquet' -o -name '*.csv' -o -name '*.rds' -o -name '*.feather' \) \
            -size +8k -print -quit 2>/dev/null | grep -q .; then
            return 0
        fi
    done < <(analysis_ready_dirs "$P")
    return 1
}

step2_spec_path() {
    local P="$1" idx="$2"
    echo "$P/m${idx}_spec.md"
}

step2_critique_path() {
    local P="$1" idx="$2"
    echo "$P/m${idx}_critique.md"
}

step2_demo_result_path() {
    local P="$1" idx="$2"
    echo "$P/m${idx}_demo_result.json"
}

step2_stream_prefix() {
    local idx="$1"
    echo "m${idx}"
}

# Parse viable_streams.md for ## Stream m<N>: headers and return the
# active idx list as a space-separated string (e.g., "1 2 3"). When the
# file is missing or unparseable, return empty — callers must handle this
# (Step 2 cannot run without Step 1 having produced the stream roster).
step2_active_stream_ids() {
    local P="$1"
    local f="$P/viable_streams.md"
    [[ -f "$f" ]] || return 0
    grep -oE '^## Stream m[0-9]+:' "$f" 2>/dev/null \
        | grep -oE '[0-9]+' \
        | sort -un \
        | tr '\n' ' ' \
        | sed 's/ $//'
}

step2_critique_verdict() {
    local P="$1" idx="$2"
    awk -F': *' '/^VERDICT:/{print $2; exit}' \
        "$(step2_critique_path "$P" "$idx")" 2>/dev/null | tr -d '\r'
}

# A stream's spec+demo artifacts are "ready" when the spec is substantive
# (≥ 30 lines per the Step 2 contract) and the demo result JSON exists.
# The runner then feeds these to the critic. Demo JSON validity (jobid
# really in run_state/solver_jobs/, status==OPTIMAL, etc.) is the
# critic's job, not the runner's — runner only checks existence here.
step2_stream_ready() {
    local P="$1" idx="$2"
    local spec demo
    spec="$(step2_spec_path "$P" "$idx")"
    demo="$(step2_demo_result_path "$P" "$idx")"
    [[ -f "$spec" ]] || return 1
    (( $(_lines "$spec") >= 30 )) || return 1
    [[ -s "$demo" ]] || return 1
    return 0
}

step2_stream_validated() {
    local P="$1" idx="$2"
    step2_stream_ready "$P" "$idx" || return 1
    [[ "$(step2_critique_verdict "$P" "$idx")" == "VALIDATED" ]]
}

step2_stream_abandoned() {
    local P="$1" idx="$2"
    [[ -f "$(step2_critique_path "$P" "$idx")" ]] || return 1
    [[ "$(step2_critique_verdict "$P" "$idx")" == "ABANDONED" ]]
}

# Step 2 is "done" for the project when at least 2 streams are
# VALIDATED. Remaining streams may be ABANDONED — those are kept on
# disk to inform Step 3's trade-off discussion but don't count toward
# the threshold. If Step 1's viable_streams.md produced fewer than 2
# streams in the first place, Step 2 can never pass and the project
# would have already been KILLed at the viability gate.
step2_has_enough_validated_streams() {
    local P="$1"
    local idx count=0
    for idx in $(step2_active_stream_ids "$P"); do
        if step2_stream_validated "$P" "$idx"; then
            count=$((count + 1))
        fi
    done
    (( count >= 2 ))
}

review_cycle_info() {
    local P="$1"
    local state_file="$P/$REVIEW_STATE_FILE"
    if [[ ! -f "$state_file" ]]; then
        echo "0 0"
        return
    fi
    python3 - "$state_file" <<'PY'
import json
import sys

step = 0
ts = 0
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    step = int(data.get("resume_step") or 0)
    ts = int(float(data.get("requested_at_epoch") or 0))
except Exception:
    pass
print("%s %s" % (step, ts))
PY
}

infer_step() {
    local P="$1" base="$2"
    local paper="$P/${base}_paper.tex"
    local reopen_marker="$P/.step11_reopen_to_step10"
    local review_resume_step=0
    local review_requested_at=0

    read -r review_resume_step review_requested_at < <(review_cycle_info "$P")

    _review_step_is_fresh() {
        local step_num="$1"
        shift
        if (( review_resume_step <= 0 || review_requested_at <= 0 || step_num < review_resume_step )); then
            return 0
        fi
        local f mt
        for f in "$@"; do
            [[ -e "$f" ]] || continue
            mt=$(_mtime "$f")
            if (( mt > review_requested_at )); then
                return 0
            fi
        done
        return 1
    }

    # Killed projects stop before Step 1 completes.
    [[ -f "$P/$KILL_MARKER" ]] && echo 0 && return

    # 16: final PDF and submission bundle delivered to papers/
    [[ -f "$FACTORY/papers/${base}_paper.pdf" && -f "$FACTORY/papers/${base}_submission.zip" ]] && echo 16 && return

    # Modeling-mode step inference — short-circuits when the project has a
    # problem/ directory (set by Step 0 problem parsing).  Modeling artifacts
    # for Steps 5-16 are checked descending so the latest completed step is
    # reported even when older artifacts remain on disk.
    if is_modeling_project "$P"; then
        local gate2_marker="$P/.gate2_reopen_to_revision"

        # 15: citation_audit.md + derobotification.md + final paper.tex
        # (abstract replaced, document closes cleanly).  Step 15 also touches
        # tables/ but cleaned tables are too project-specific to gate on.
        if [[ -f "$P/citation_audit.md" && -f "$P/derobotification.md" && -f "$paper" ]] \
            && (( $(_lines "$P/citation_audit.md") >= 10 )) \
            && (( $(_lines "$P/derobotification.md") >= 10 )) \
            && grep -q '\\end{document}' "$paper" 2>/dev/null \
            && ! grep -q "ABSTRACT_PLACEHOLDER" "$paper" 2>/dev/null \
            && _review_step_is_fresh 15 "$P/derobotification.md" "$paper"; then
            echo 15
            return
        fi

        # 14: abstract_draft.md exists AND paper.tex no longer contains
        # ABSTRACT_PLACEHOLDER (i.e. the abstract has been spliced in).
        if [[ -f "$P/abstract_draft.md" && -f "$paper" ]] \
            && (( $(_lines "$P/abstract_draft.md") >= 20 )) \
            && grep -q '\\end{document}' "$paper" 2>/dev/null \
            && ! grep -q "ABSTRACT_PLACEHOLDER" "$paper" 2>/dev/null \
            && _review_step_is_fresh 14 "$P/abstract_draft.md" "$paper"; then
            echo 14
            return
        fi

        # 13: judge_evaluation.md with VERDICT: line (Gate 2).
        if [[ -f "$P/judge_evaluation.md" ]] \
            && (( $(_lines "$P/judge_evaluation.md") >= 30 )) \
            && grep -q "^VERDICT:" "$P/judge_evaluation.md" 2>/dev/null \
            && _review_step_is_fresh 13 "$P/judge_evaluation.md"; then
            echo 13
            return
        fi

        # 12: revision_summary.md + revised paper.tex + pre-revision archive.
        # When a Gate-2 reopen is active, all three must be newer than the
        # reopen marker so the rewind actually takes effect; mirrors the
        # legacy social-science Step 10 reopen logic at lines 337-344.
        if [[ -f "$P/revision_summary.md" && -f "$paper" && -d "$P/paper/archive/pre_step12" ]] \
            && (( $(_lines "$P/revision_summary.md") >= 10 )); then
            if [[ -f "$gate2_marker" ]]; then
                if _is_newer_by "$paper" "$gate2_marker" 60 \
                   && _is_newer_by "$P/revision_summary.md" "$gate2_marker" 1 \
                   && _review_step_is_fresh 12 "$P/revision_summary.md" "$paper"; then
                    echo 12
                    return
                fi
            else
                if _review_step_is_fresh 12 "$P/revision_summary.md" "$paper"; then
                    echo 12
                    return
                fi
            fi
        fi

        # 11: review_comments.md (constructive review).
        if [[ -f "$P/review_comments.md" ]] \
            && (( $(_lines "$P/review_comments.md") >= 30 )) \
            && _review_step_is_fresh 11 "$P/review_comments.md"; then
            echo 11
            return
        fi

        # 10: code_review.md (Gate 1 — numerical & code consistency).
        if [[ -f "$P/code_review.md" ]] \
            && (( $(_lines "$P/code_review.md") >= 20 )) \
            && _review_step_is_fresh 10 "$P/code_review.md"; then
            echo 10
            return
        fi

        # 9: paper.tex draft with the canonical CUMCM sections + ABSTRACT
        # placeholder still in place (Step 14 removes it).  Minimum length
        # is conservative; a real CUMCM draft easily clears 300 lines.
        if [[ -f "$paper" ]] \
            && (( $(_lines "$paper") > 200 )) \
            && grep -q '\\begin{document}' "$paper" 2>/dev/null \
            && grep -q '\\end{document}' "$paper" 2>/dev/null \
            && grep -q 'ABSTRACT_PLACEHOLDER' "$paper" 2>/dev/null \
            && _review_step_is_fresh 9 "$paper"; then
            echo 9
            return
        fi

        # 8: visualization_log.md + at least one polished figure newer than
        # the sensitivity step (8 polishes 5/6 figures into final form).
        if [[ -f "$P/visualization_log.md" ]] \
            && (( $(_lines "$P/visualization_log.md") >= 20 )) \
            && find "$P/figures" -maxdepth 1 -type f \( -name '*.pdf' -o -name '*.png' \) -print -quit 2>/dev/null | grep -q . \
            && _review_step_is_fresh 8 "$P/visualization_log.md"; then
            echo 8
            return
        fi

        # 7: evaluation.md (model strengths/weaknesses + comparison).
        if [[ -f "$P/evaluation.md" ]] \
            && (( $(_lines "$P/evaluation.md") >= 30 )) \
            && _review_step_is_fresh 7 "$P/evaluation.md"; then
            echo 7
            return
        fi

        # 6: sensitivity_report.md + at least one figures/sensitivity_*.{pdf,png}.
        if [[ -f "$P/sensitivity_report.md" ]] \
            && (( $(_lines "$P/sensitivity_report.md") >= 20 )) \
            && find "$P/figures" -maxdepth 1 -type f \( -name 'sensitivity_*.pdf' -o -name 'sensitivity_*.png' \) -print -quit 2>/dev/null | grep -q . \
            && _review_step_is_fresh 6 "$P/sensitivity_report.md"; then
            echo 6
            return
        fi

        # 5: solve_log.md (per-run table for all sub-problems) + at least one
        # values.json file under results/.  STEPS.md:83 requires
        # results/<subproblem>/values.json per sub-problem, but enumerating
        # sub-problem names from problem_brief.md is brittle — we accept any
        # values.json anywhere under results/ as the "solver actually ran"
        # signal.
        if [[ -f "$P/solve_log.md" ]] \
            && (( $(_lines "$P/solve_log.md") >= 20 )) \
            && find "$P/results" -type f -name 'values.json' -print -quit 2>/dev/null | grep -q . \
            && _review_step_is_fresh 5 "$P/solve_log.md"; then
            echo 5
            return
        fi

        # 4: model.md + symbol_table.md + assumption_ledger.md (Step 4
        # contract).  Checked before step 3 because chosen_method.md /
        # method_decision.md remain on disk past step 4 — descending by
        # step number ensures the latest completed step is reported.
        if [[ -f "$P/model.md" && -f "$P/symbol_table.md" && \
              -f "$P/assumption_ledger.md" ]] \
            && (( $(_lines "$P/model.md") >= 100 )) \
            && (( $(_lines "$P/symbol_table.md") >= 10 )) \
            && (( $(_lines "$P/assumption_ledger.md") >= 10 )); then
            echo 4
            return
        fi

        # 3: method_decision.md + chosen_method.md with PRIMARY: marker.
        # Checked before step 2 because the m{N}_critique.md files (which
        # step 2 detection reads) remain on disk past step 3 — descending
        # by step number ensures we report the latest completed step.
        if [[ -f "$P/method_decision.md" && -f "$P/chosen_method.md" ]] \
            && (( $(_lines "$P/method_decision.md") >= 30 )) \
            && (( $(_lines "$P/chosen_method.md") >= 10 )) \
            && grep -q "^PRIMARY:" "$P/chosen_method.md" 2>/dev/null; then
            echo 3
            return
        fi

        # 2: ≥ 2 active streams reached VERDICT: VALIDATED (Step 2 contract).
        # Checked before step 1 so a completed run isn't mistaken for "still
        # at step 1" because step 1 artifacts are always present after step 1.
        if [[ -f "$P/viable_streams.md" ]]; then
            local _s2_active _s2_validated=0 _s2_idx
            _s2_active="$(step2_active_stream_ids "$P")"
            for _s2_idx in $_s2_active; do
                step2_stream_validated "$P" "$_s2_idx" && _s2_validated=$((_s2_validated + 1))
            done
            if (( _s2_validated >= 2 )); then
                echo 2
                return
            fi
        fi

        # 1: research_brief + viable_streams + viability_gate (verdict line),
        # and viable_streams.md must cite only registered methods (HMML-lite
        # hard gate). An unregistered citation keeps the project at step 0 so
        # the runner retries Step 1 rather than building on a phantom method.
        if [[ -f "$P/research_brief.md" && -f "$P/viable_streams.md" && \
              -f "$P/viability_gate.md" ]] \
            && (( $(_lines "$P/research_brief.md") >= 30 )) \
            && (( $(_lines "$P/viable_streams.md") >= 20 )) \
            && (( $(_lines "$P/viability_gate.md") >= 10 )) \
            && check_method_citations "$P" "$P/viable_streams.md" >/dev/null 2>&1; then
            echo 1
            return
        fi
    fi

    local has_analysis_data=0
    project_has_analysis_ready_data "$P" && has_analysis_data=1

    if (( has_analysis_data )); then
        # Reopen gate: Step 11 requested one return to Step 10.
        # While the marker exists, force the workflow back through Step 10.
        if [[ -f "$reopen_marker" ]]; then
            if [[ -f "$P/final_review.md" ]] && \
               (( $(_lines "$P/final_review.md") >= 20 )) && \
               _is_newer_by "$P/final_review.md" "$reopen_marker" 1 && \
               _review_step_is_fresh 11 "$P/final_review.md"; then
                echo 11 && return
            fi
            if [[ -d "$P/do/archive/pre_step10" && -f "$P/revision_summary.md" && \
                  -f "$P/findings_brief.md" && -f "$paper" ]]; then
                if (( $(_lines "$P/revision_summary.md") >= 10 )) && \
                   _is_newer_by "$paper" "$reopen_marker" 60 && \
                   _is_newer_by "$P/findings_brief.md" "$reopen_marker" 60 && \
                   _is_newer_by "$P/revision_summary.md" "$reopen_marker" 1 && \
                   _review_step_is_fresh 10 "$P/revision_summary.md" "$paper" "$P/findings_brief.md" "$P/do/archive/pre_step10"; then
                    echo 10 && return
                fi
            fi
            echo 9 && return
        fi

        # 15: de-robotification (explicit summary + edited paper after abstract)
        if [[ -f "$P/derobotification.md" && -f "$P/abstract_draft.md" && -f "$paper" ]]; then
            if (( $(_lines "$P/derobotification.md") >= 5 )) && \
               _is_newer_by "$paper" "$P/abstract_draft.md" 60 && \
               ! grep -q "ABSTRACT PLACEHOLDER" "$paper" 2>/dev/null && \
               _review_step_is_fresh 15 "$P/derobotification.md" "$paper" "$P/abstract_draft.md"; then
                echo 15 && return
            fi
        fi

        # 14: abstract drafted and inserted into the paper
        if [[ -f "$P/abstract_draft.md" && -f "$paper" ]]; then
            if (( $(_chars "$P/abstract_draft.md") >= 300 )) && \
               ! grep -q "ABSTRACT PLACEHOLDER" "$paper" 2>/dev/null && \
               _review_step_is_fresh 14 "$P/abstract_draft.md" "$paper"; then
                echo 14 && return
            fi
        fi

        # 13–8: unique output files per step
        [[ -f "$P/table_formatting.md" ]]   && _review_step_is_fresh 13 "$P/table_formatting.md" && echo 13 && return
        [[ -f "$P/citation_audit.md" ]]     && _review_step_is_fresh 12 "$P/citation_audit.md" && echo 12 && return
        [[ -f "$P/final_review.md" ]] \
            && (( $(_lines "$P/final_review.md") >= 20 )) \
            && _review_step_is_fresh 11 "$P/final_review.md" \
            && echo 11 && return

        if [[ -d "$P/do/archive/pre_step10" && -f "$P/review_comments.md" && \
              -f "$P/revision_summary.md" && -f "$P/findings_brief.md" && -f "$paper" ]]; then
            if (( $(_lines "$P/revision_summary.md") >= 10 )) && \
               _is_newer_by "$paper" "$P/review_comments.md" 60 && \
               _is_newer_by "$P/findings_brief.md" "$P/review_comments.md" 60 && \
               _review_step_is_fresh 10 "$P/revision_summary.md" "$paper" "$P/findings_brief.md" "$P/do/archive/pre_step10"; then
                echo 10 && return
            fi
        fi

        [[ -f "$P/review_comments.md" ]] \
            && (( $(_lines "$P/review_comments.md") >= 20 )) \
            && _review_step_is_fresh 9 "$P/review_comments.md" \
            && echo  9 && return
        [[ -f "$P/code_review.md" ]] \
            && (( $(_lines "$P/code_review.md") >= 20 )) \
            && _review_step_is_fresh 8 "$P/code_review.md" \
            && echo  8 && return

        # 7: paper tex exists and is substantive
        if [[ -f "$paper" ]]; then
            (( $(_lines "$paper") > 200 )) \
                && grep -q '\\begin{document}' "$paper" 2>/dev/null \
                && grep -q '\\end{document}' "$paper" 2>/dev/null \
                && grep -q "ABSTRACT PLACEHOLDER" "$paper" 2>/dev/null \
                && _review_step_is_fresh 7 "$paper" \
                && echo 7 && return
        fi

        # 6: methods audit appended to findings_brief
        [[ -f "$P/findings_brief.md" ]] \
            && grep -qi "methods audit" "$P/findings_brief.md" 2>/dev/null \
            && _review_step_is_fresh 6 "$P/findings_brief.md" \
            && echo 6 && return

        # 5: data audit appended to findings_brief
        [[ -f "$P/findings_brief.md" ]] \
            && grep -qi "data audit" "$P/findings_brief.md" 2>/dev/null \
            && _review_step_is_fresh 5 "$P/findings_brief.md" \
            && echo 5 && return

        # 4: argument architecture decided
        [[ -f "$P/argument_decision.md" ]] \
            && (( $(_lines "$P/argument_decision.md") > 20 )) \
            && _review_step_is_fresh 4 "$P/argument_decision.md" \
            && echo 4 && return

        # 3: selected findings package documented and promoted
        [[ -f "$P/findings_decision.md" && -f "$P/findings_brief.md" ]] \
            && (( $(_lines "$P/findings_decision.md") > 20 )) \
            && (( $(_lines "$P/findings_brief.md") > 40 )) \
            && _review_step_is_fresh 3 "$P/findings_decision.md" "$P/findings_brief.md" \
            && echo 3 && return

        # 2: at least 2 validated modeling streams (per viable_streams.md)
        local -a step2_files=()
        local step2_idx
        for step2_idx in $(step2_active_stream_ids "$P"); do
            step2_files+=("$P/m${step2_idx}_spec.md" "$P/m${step2_idx}_critique.md")
        done
        if (( ${#step2_files[@]} > 0 )); then
            step2_has_enough_validated_streams "$P" \
                && _review_step_is_fresh 2 "${step2_files[@]}" \
                && echo 2 && return
        fi

        # 1: research + data wrangle + data context + key variables + descriptive map
        [[ -f "$P/codex_research.md" && -f "$P/data_wrangle.md" && \
           -f "$P/data_context.md" && -f "$P/key_variables.md" && -f "$P/descriptive_map.md" ]] \
            && (( $(_lines "$P/data_wrangle.md") > 20 )) \
            && (( $(_lines "$P/data_context.md") > 20 )) \
            && (( $(_lines "$P/key_variables.md") > 20 )) \
            && (( $(_lines "$P/descriptive_map.md") > 40 )) \
            && _review_step_is_fresh 1 "$P/codex_research.md" "$P/data_wrangle.md" "$P/data_context.md" "$P/key_variables.md" "$P/descriptive_map.md" \
            && echo 1 && return
    fi

    # 0: project set up — either modeling-mode (problem/problem_brief.md)
    # or social-mode (project_brief.md) artifacts present.
    [[ -f "$P/problem/problem_brief.md" || -f "$P/project_brief.md" ]] && echo 0 && return

    # -1: nothing yet
    echo -1
}

# ── Handle utility flags ─────────────────────────────────────────────

if [[ "${1:-}" == "--infer-step" ]]; then
    D="${2:?Usage: run_paper.sh --infer-step <project_dir>}"
    D="$(cd "$D" && pwd)"
    infer_step "$D" "$(basename "$D")"
    exit 0
fi

if [[ "${1:-}" == "--status" ]]; then
    D="${2:?Usage: run_paper.sh --status <project_dir>}"
    D="$(cd "$D" && pwd)"
    B="$(basename "$D")"
    S=$(infer_step "$D" "$B")
    CP=$(grep "Last completed step" "$D/checkpoint.md" 2>/dev/null \
        | grep -oP -- '-?\d+' | head -1 || echo -1)
    HB="none"
    if [[ -f "$D/.heartbeat" ]]; then
        HB=$(cat "$D/.heartbeat")
    fi
    KILLED="no"
    [[ -f "$D/$KILL_MARKER" ]] && KILLED="yes"
    echo "project=$B inferred=$S checkpoint=$CP heartbeat=$HB killed=$KILLED"
    exit 0
fi

# ── Main entry ────────────────────────────────────────────────────────

PROJECT="${1:?Usage: run_paper.sh <project_dir>}"
if [[ ! -d "$PROJECT" ]]; then
    echo "ERROR: directory does not exist: $PROJECT"
    exit 1
fi
PROJECT="$(cd "$PROJECT" && pwd)"
BASE="$(basename "$PROJECT")"
mkdir -p "$PROJECT/logs"

# Run from a per-job snapshot so edits to run_paper.sh do not affect
# already-running projects mid-execution.
if [[ -z "${RUN_PAPER_SNAPSHOT:-}" ]]; then
    SNAPSHOT_DIR="$PROJECT/logs/runner_snapshots"
    SNAPSHOT_PATH="$SNAPSHOT_DIR/run_paper_$(date +%Y%m%d_%H%M%S)_$$.sh"
    mkdir -p "$SNAPSHOT_DIR"
    if cp "$FACTORY/run_paper.sh" "$SNAPSHOT_PATH"; then
        chmod +x "$SNAPSHOT_PATH" 2>/dev/null || true
        exec env RUN_PAPER_SNAPSHOT=1 FACTORY="$FACTORY" "$SNAPSHOT_PATH" "$PROJECT"
    fi
fi

trap 'echo "$(date '\''+%Y-%m-%d %H:%M:%S'\'') [$BASE] ERR at line $LINENO (exit $?)" >> "$PROJECT/logs/runner.log"' ERR

project_is_killed() {
    local proj_dir="${1:-$PROJECT}"
    [[ -f "$proj_dir/$KILL_MARKER" ]]
}

remove_registry_entry() {
    local proj="$1"
    mkdir -p "$(dirname "$REGISTRY")"
    touch "$REGISTRY"
    local tmp="${REGISTRY}.tmp.$$"
    grep -v "^${proj} " "$REGISTRY" > "$tmp" 2>/dev/null || true
    mv "$tmp" "$REGISTRY"
}

step1_viability_verdict() {
    awk -F': *' '/^VERDICT:/{print $2; exit}' "$PROJECT/viability_gate.md" 2>/dev/null \
        | tr -d '\r'
}

mark_project_killed() {
    local timestamp
    timestamp=$(date +%s)
    touch "$PROJECT/$KILL_MARKER"
    echo "KILLED:1 $timestamp" > "$PROJECT/.heartbeat"
    sed -i "s/\*\*Last completed step\*\*: .*/\*\*Last completed step\*\*: 0/" \
        "$PROJECT/checkpoint.md" 2>/dev/null || true
    sed -i "s/\*\*Timestamp\*\*: .*/\*\*Timestamp\*\*: $(date '+%Y-%m-%d %H:%M')/" \
        "$PROJECT/checkpoint.md" 2>/dev/null || true
    if grep -q "Termination status" "$PROJECT/checkpoint.md" 2>/dev/null; then
        sed -i "s/\*\*Termination status\*\*: .*/\*\*Termination status\*\*: KILLED at Step 1 viability gate/" \
            "$PROJECT/checkpoint.md" 2>/dev/null || true
    else
        cat >> "$PROJECT/checkpoint.md" <<EOF
- **Termination status**: KILLED at Step 1 viability gate
EOF
    fi
    if grep -q "Termination memo" "$PROJECT/checkpoint.md" 2>/dev/null; then
        sed -i "s/\*\*Termination memo\*\*: .*/\*\*Termination memo\*\*: kill_memo.md/" \
            "$PROJECT/checkpoint.md" 2>/dev/null || true
    else
        cat >> "$PROJECT/checkpoint.md" <<EOF
- **Termination memo**: kill_memo.md
EOF
    fi
    remove_registry_entry "$BASE"
}

if [[ -f "$PROJECT/.paused" ]]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$BASE] Project is paused. Exiting without running." \
        | tee -a "$PROJECT/logs/runner.log"
    exit 0
fi

if project_is_killed "$PROJECT"; then
    remove_registry_entry "$BASE"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$BASE] Project is killed. See kill_memo.md. Exiting without running." \
        | tee -a "$PROJECT/logs/runner.log"
    exit 0
fi

# Per-step timeout in seconds.
# Generous — the step-level approach already gives us the main benefit.
step_timeout() {
    case "$1" in
        1)  echo 14400 ;;   # 4h  — two background agents + monitoring
        2)  echo 28800 ;;   # 8h  — 6 parallel findings/critic streams with revisions
        3)  echo 7200  ;;   # 2h  — findings-package decider
        4)  echo 14400 ;;   # 4h  — full model construction (single agent)
        5)  echo 14400 ;;   # 4h  — full solve (parallel solver_submit jobs; bulk of compute)
        6)  echo 10800 ;;   # 3h  — sensitivity + robustness (modeling) / methods audit (legacy)
        7)  echo 7200  ;;   # 2h  — model evaluation (modeling) / paper writing (legacy)
        8)  echo 10800 ;;   # 3h  — visualization polish (modeling) / code review (legacy)
        9)  echo 14400 ;;   # 4h  — paper draft (modeling) / constructive review (legacy)
        10) echo 10800 ;;   # 3h  — Gate 1 numerical check (modeling) / revision (legacy)
        11) echo 7200  ;;   # 2h  — constructive review (modeling) / final review (legacy)
        12) echo 14400 ;;   # 4h  — revision after Gate 2 reopen (modeling) / citation audit (legacy)
        13) echo 10800 ;;   # 3h  — Gate 2 judge simulation (modeling) / table formatting (legacy)
        14) echo 7200  ;;   # 2h  — abstract
        15) echo 10800 ;;   # 3h  — citation + table + de-robotification polish bundle (modeling)
        16) echo 3600  ;;   # 1h  — delivery + submission bundle
        *)  echo 10800 ;;
    esac
}

# Monitoring cadence for long-running agent steps. Five-minute polling made
# the pipeline feel artificially slow because step completion was only noticed
# on the next poll. One minute keeps overhead low while cutting that latency.
MONITOR_SLEEP=60

# ── Lock: prevent two runners on the same project ────────────────────

LOCKDIR="$PROJECT/.runner.lock"
LOCKINFO="$PROJECT/.runner.lock.info"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    # Check if the lock is stale by examining heartbeat freshness.
    # The heartbeat records the last completed step and its timestamp.
    # If the heartbeat is older than the expected timeout for the next
    # step (+ 30min buffer), the owning runner is dead.
    if [[ -d "$LOCKDIR" ]]; then
        stale=false
        stale_reason=""
        owner_pid=""
        if [[ -f "$LOCKINFO" ]]; then
            owner_pid=$(awk -F= '$1=="pid"{print $2}' "$LOCKINFO" 2>/dev/null | head -1)
        fi

        if [[ -n "$owner_pid" && "$owner_pid" =~ ^[0-9]+$ ]] && ! kill -0 "$owner_pid" 2>/dev/null; then
            stale=true
            stale_reason="owner pid $owner_pid is not running"
        fi

        HB_FILE="$PROJECT/.heartbeat"
        if ! $stale && [[ -f "$HB_FILE" ]]; then
            read -r hb_step hb_ts < "$HB_FILE" 2>/dev/null || true
            hb_step="${hb_step#STUCK:}"   # strip prefix if stuck
            hb_step="${hb_step#ACTIVE:}"  # strip prefix if active
            if [[ -n "$hb_ts" && "$hb_ts" =~ ^[0-9]+$ ]]; then
                next_step=$(( hb_step + 1 ))
                max_wait=$(( $(step_timeout "$next_step") + 1800 ))
                hb_age=$(( $(date +%s) - hb_ts ))
                if (( hb_age > max_wait )); then
                    stale=true
                    stale_reason="heartbeat age ${hb_age}s exceeds ${max_wait}s"
                fi
            else
                # Can't parse heartbeat — fall back to lock dir age
                lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCKDIR") ))
                if (( lock_age > 14400 )); then
                    stale=true
                    stale_reason="lock age ${lock_age}s exceeds 14400s"
                fi
            fi
        elif ! $stale; then
            # No heartbeat file — fall back to lock dir age (4h)
            lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCKDIR") ))
            if (( lock_age > 14400 )); then
                stale=true
                stale_reason="lock age ${lock_age}s exceeds 14400s"
            fi
        fi

        if $stale; then
            [[ -z "$stale_reason" ]] && stale_reason="unknown"
            echo "$(date '+%Y-%m-%d %H:%M:%S') [${BASE}] Stale lock detected ($stale_reason) — reclaiming." \
                | tee -a "$PROJECT/logs/runner.log"
            rm -f "$LOCKINFO" 2>/dev/null || true
            if ! rmdir "$LOCKDIR" 2>/dev/null; then
                echo "$(date '+%Y-%m-%d %H:%M:%S') [${BASE}] Failed to remove stale lock directory. Exiting." \
                    | tee -a "$PROJECT/logs/runner.log"
                exit 0
            fi
            if ! mkdir "$LOCKDIR" 2>/dev/null; then
                echo "$(date '+%Y-%m-%d %H:%M:%S') [${BASE}] Failed to reacquire lock after reclaim. Exiting." \
                    | tee -a "$PROJECT/logs/runner.log"
                exit 0
            fi
        else
            echo "$(date '+%Y-%m-%d %H:%M:%S') [${BASE}] Another runner is active (lock exists). Exiting." \
                | tee -a "$PROJECT/logs/runner.log"
            exit 0
        fi
    fi
fi
{
    echo "jobid=${SLURM_JOB_ID:-}"
    echo "pid=$$"
    echo "host=$(hostname -s 2>/dev/null || hostname)"
    echo "started=$(date +%s)"
} > "$LOCKINFO" 2>/dev/null || true
trap 'rm -f "$LOCKINFO"; rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# ── Helpers ───────────────────────────────────────────────────────────

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$BASE] $*" | tee -a "$PROJECT/logs/runner.log"
}

checkpoint_step() {
    grep "Last completed step" "$PROJECT/checkpoint.md" 2>/dev/null \
        | grep -oP -- '-?\d+' | head -1 || echo -1
}

get_question() {
    grep "Research question" "$PROJECT/checkpoint.md" 2>/dev/null \
        | sed 's/.*\*\*: //'
}

verify_step() {
    local new
    new=$(infer_step "$PROJECT" "$BASE")
    (( new >= $1 ))
}

# Gate 2 (Step 13 in modeling mode; was Step 11 in the legacy social-science
# pipeline).  Reads VERDICT line from judge_evaluation.md and, if it requests
# a reopen, drops a marker so the runner rewinds to Step 12 (revision) once.
# The .gate2_reopened_once file prevents an infinite reopen loop.
gate2_reopen_marker() {
    echo "$PROJECT/.gate2_reopen_to_revision"
}

gate2_reopened_once_file() {
    echo "$PROJECT/.gate2_reopened_once"
}

gate2_verdict() {
    awk -F': *' '/^VERDICT:/{print $2; exit}' "$PROJECT/judge_evaluation.md" 2>/dev/null \
        | tr -d '\r'
}

gate2_requests_reopen() {
    local verdict
    verdict=$(gate2_verdict)
    [[ "$verdict" == "REOPEN_REVISION_TEXT" || "$verdict" == "REOPEN_REVISION_MODEL" ]]
}

# ── Determine starting point ─────────────────────────────────────────

INFERRED=$(infer_step "$PROJECT" "$BASE")
FROM_CP=$(checkpoint_step)
STEP=$INFERRED

QUESTION=$(get_question)

log "========================================"
log "Runner starting"
log "  file-state: step $INFERRED | checkpoint: step $FROM_CP | resuming from: step $STEP"

if [[ -z "$QUESTION" ]]; then
    log "ERROR: no research question in checkpoint.md"
    exit 1
fi
log "  question: ${QUESTION:0:120}..."

# File-state is authoritative; correct checkpoint drift in either direction.
if (( INFERRED != FROM_CP )); then
    log "  checkpoint disagrees with file-state ($FROM_CP vs $INFERRED) — correcting"
    sed -i "s/\*\*Last completed step\*\*: .*/\*\*Last completed step\*\*: $INFERRED/" \
        "$PROJECT/checkpoint.md"
    sed -i "s/\*\*Timestamp\*\*: .*/\*\*Timestamp\*\*: $(date '+%Y-%m-%d %H:%M')/" \
        "$PROJECT/checkpoint.md"
fi

# Write heartbeat immediately so the lock staleness check always has a
# timestamp. Without this, a runner that dies before completing any step
# leaves no heartbeat, and the lock fallback uses a generous 4h age check
# on the lock directory — too slow to recover.
echo "$STEP $(date +%s)" > "$PROJECT/.heartbeat"

if (( STEP >= 16 )); then
    log "Paper already complete (step $STEP). Nothing to do."
    exit 0
fi

# ── Setup step for new projects (step -1 -> 0) ───────────────────────

if (( STEP < 0 )); then
    log "New project — running setup (prerequisites)"
    SETUP_LOG="$PROJECT/logs/step_setup_$(date +%Y%m%d_%H%M%S).log"

    if is_modeling_input "$PROJECT" "$QUESTION"; then
        log "Modeling-mode setup — using prompts/step0_problem_parsing.txt"
        # Inline render: render_prompt() is defined later in the file (after
        # this setup block) so we substitute placeholders directly here.
        # The step0 prompt is self-contained — it explicitly overrides
        # analysis_guide.md with modeling_guide.md — so we deliberately skip
        # the social-science common_prompt_preamble.
        _q_escaped="${QUESTION//&/\\&}"
        SETUP_PROMPT=$(sed \
            -e "s|__PROJECT_PATH__|$PROJECT|g" \
            -e "s|__RESEARCH_QUESTION__|$_q_escaped|g" \
            -e "s|__BASE_NAME__|$BASE|g" \
            -e "s|__FACTORY__|$FACTORY|g" \
            "$FACTORY/prompts/step0_problem_parsing.txt")
    else
        log "Social-mode setup — using legacy inline prompt"
        SETUP_PROMPT="Read the paper skill instructions at $SKILL.

Set up the project at $PROJECT for the following research question:
$QUESTION

The base name is \"$BASE\". The project directory and template files
(style/paper.sty, bib/bibliography.bst, style/model_papers_style.json,
analysis_guide.md) are already in place.

Execute ONLY the PREREQUISITES section: locate the .dta data files on
the filesystem, write project_brief.md with data locations and research
question, and update checkpoint.md to Last completed step: 0.

Do NOT proceed to Step 1 — stop after setup is complete."

        SETUP_PROMPT="$SETUP_PROMPT

Do not inspect, reference, reuse, or mention completed projects unless the human researcher explicitly points you to one. Work only from the current project directory, the source data, and shared infrastructure."
    fi

    set +e
    (
        cd "$PROJECT" && timeout --kill-after=120 3600 \
            codex exec \
              --model gpt-5.5 \
              -c 'model_reasoning_effort="xhigh"' \
              --dangerously-bypass-approvals-and-sandbox \
              -C "$PROJECT" \
              --skip-git-repo-check \
              "$SETUP_PROMPT"
    ) > "$SETUP_LOG" 2>&1 &
    SETUP_PID=$!
    SETUP_EC=0
    SETUP_SHORT_CIRCUITED=0

    while kill -0 "$SETUP_PID" 2>/dev/null; do
        if project_setup_complete "$PROJECT"; then
            SETUP_SHORT_CIRCUITED=1
            log "Setup artifacts detected — stopping setup session and advancing"
            _kill_process_tree "$SETUP_PID" TERM
            sleep 2
            _kill_process_tree "$SETUP_PID" KILL
            break
        fi
        echo "-1 $(date +%s)" > "$PROJECT/.heartbeat"
        sleep 5
    done

    wait "$SETUP_PID" 2>/dev/null
    SETUP_EC=$?
    set -e

    # The modeling step-0 prompt forbids the agent from writing checkpoint.md
    # (the runner owns it — see prompts/step0_problem_parsing.txt). Once the
    # structured problem/ brief exists, record setup completion here so the
    # gate below and infer_step() agree on step 0. Gated on the modeling brief
    # so social-mode setup (whose agent writes its own checkpoint) is untouched.
    if [[ -f "$PROJECT/problem/problem_brief.md" ]]; then
        sed -i "s/\*\*Last completed step\*\*: .*/\*\*Last completed step\*\*: 0/" \
            "$PROJECT/checkpoint.md" 2>/dev/null || true
    fi

    if project_setup_complete "$PROJECT"; then
        # HMML-lite hard gate: Step 0's candidate_methods.md may cite only
        # methods registered in method_library/index.json. A hallucinated or
        # unregistered method path is a correctness violation — stop for human
        # review rather than letting a phantom method propagate into Steps 1-5.
        # (No setup retry loop exists here, so this is FATAL by design.)
        if [[ -f "$PROJECT/problem/candidate_methods.md" ]]; then
            if ! cm_report=$(check_method_citations "$PROJECT" "$PROJECT/problem/candidate_methods.md"); then
                log "FATAL: Step 0 candidate_methods.md cites unregistered method(s):"
                while IFS= read -r cm_line; do [[ -n "$cm_line" ]] && log "   $cm_line"; done <<<"$cm_report"
                log "   Fix the path(s) to registered method_library/ methods, or register the method in"
                log "   method_library/index.json (+ .md doc), then resume. Unregistered suggestions"
                log "   belong in candidate_methods.md WITHOUT the method_library/ prefix."
                echo "STUCK:0 $(date +%s)" > "$PROJECT/.heartbeat"
                exit 1
            fi
        fi
        log "Setup complete"
        STEP=0
        echo "0 $(date +%s)" > "$PROJECT/.heartbeat"
    else
        log "FATAL: setup failed (exit $SETUP_EC) — setup artifacts incomplete. See $SETUP_LOG"
        exit 1
    fi
fi

# ── Activity monitor ──────────────────────────────────────────────────
#
# Runs in the background during each step. Checks whether the step log
# file is still growing every ACTIVITY_INTERVAL seconds. If it is,
# writes an ACTIVE heartbeat so the watchdog knows the agent is alive.
# This gives intra-step visibility: a hung agent is detected in minutes,
# not hours.

ACTIVITY_INTERVAL=300   # check every 5 minutes
CLAUDE_PROJECTS="$HOME/.claude/projects"
CODEX_SESSIONS="$HOME/.codex/sessions"

_start_activity_monitor() {
    local log_file="$1" step="$2" hb_file="$3"
    local last_size=0
    local last_trace_mtime=0
    # Derive the project name (used to find trace files)
    local proj_name
    proj_name=$(basename "$(dirname "$hb_file")")
    local proj_encoded="${proj_name//_/-}"
    local proj_dir
    proj_dir=$(dirname "$hb_file")

    while true; do
        sleep "$ACTIVITY_INTERVAL"

        local alive=false

        # Signal 1: ANY log file in the project's logs/ dir is growing
        local total_log_size=0
        for lf in "$proj_dir"/logs/step_${step}_*.log; do
            [ -f "$lf" ] || continue
            local s
            s=$(stat -c %s "$lf" 2>/dev/null || echo 0)
            total_log_size=$((total_log_size + s))
        done
        if (( total_log_size > last_size )); then
            alive=true
            last_size=$total_log_size
        fi

        # Signal 2: Claude trace files updated recently
        if ! $alive; then
            local newest_trace=0
            for tdir in "$CLAUDE_PROJECTS"/*"$proj_encoded"*; do
                [ -d "$tdir" ] || continue
                for tf in "$tdir"/*.jsonl "$tdir"/subagents/*.jsonl; do
                    [ -f "$tf" ] || continue
                    local mt
                    mt=$(stat -c %Y "$tf" 2>/dev/null || echo 0)
                    (( mt > newest_trace )) && newest_trace=$mt
                done
            done
            if (( newest_trace > last_trace_mtime )); then
                alive=true
                last_trace_mtime=$newest_trace
            fi
        fi

        # Signal 3: Codex session traces updated recently
        #   Use find instead of ** glob (globstar is off by default)
        if ! $alive && [ -d "$CODEX_SESSIONS" ]; then
            local now
            now=$(date +%s)
            while IFS= read -r sf; do
                [ -f "$sf" ] || continue
                local mt
                mt=$(stat -c %Y "$sf" 2>/dev/null || echo 0)
                (( now - mt > 3600 )) && continue
                if head -1 "$sf" 2>/dev/null | grep -q "$proj_name"; then
                    if (( mt > last_trace_mtime )); then
                        alive=true
                        last_trace_mtime=$mt
                        break
                    fi
                fi
            done < <(find "$CODEX_SESSIONS" -name "*.jsonl" -mmin -60 2>/dev/null)
        fi

        # Signal 4: any file in the project dir modified recently
        if ! $alive; then
            local newest_file
            newest_file=$(find "$proj_dir" -maxdepth 1 -type f -newer "$hb_file" 2>/dev/null | head -1)
            if [[ -n "$newest_file" ]]; then
                alive=true
            fi
        fi

        if $alive; then
            echo "ACTIVE:$step $(date +%s)" > "$hb_file"
        fi
    done
}

# ── Prompt rendering ─────────────────────────────────────────────────
#
# Prompt templates live in factory/prompts/. Placeholders:
#   __PROJECT_PATH__     → $PROJECT
#   __RESEARCH_QUESTION__→ $QUESTION
#   __BASE_NAME__        → $BASE

PROMPTS="$FACTORY/prompts"

agent_key_from_prompt_file() {
    local prompt_file="$1"
    local base="${prompt_file##*/}"
    echo "${base%.txt}"
}

prepend_agent_key() {
    local agent_key="$1"
    local prompt="$2"
    if [[ -z "$agent_key" ]]; then
        printf '%s' "$prompt"
    else
        printf 'AGENT_KEY: %s\n\n%s' "$agent_key" "$prompt"
    fi
}

common_prompt_preamble() {
    cat <<EOF
Before doing any substantive work, read the project style guide in the current project directory: prefer \`modeling_guide.md\` (math-modeling mode) if present, otherwise read \`analysis_guide.md\` (legacy social-science mode). It is the canonical guide for local job execution (solver_submit.sh / stata_submit.sh), figure style, project file layout, code conventions, error recovery, and table formatting. For tasks that do not touch those areas directly, skim it and apply the relevant parts.

If \`human_review.md\` exists in the current project directory, read it before doing substantive work. Treat it as the newest human reviewer guidance for the current review cycle. If it conflicts with older review materials, prefer \`human_review.md\`. Older downstream artifacts may still be on disk for context after a rewind or revision request; do not treat them as authoritative unless you deliberately reuse or regenerate them in the current step.

Do not inspect, read, cite, summarize, reuse, or mention completed projects unless the human researcher explicitly instructs you to do so for this project. Work from the current project directory, the source data, the active prompts, and shared infrastructure only.

EOF
}

render_prompt() {
    local template="$1"
    local extra="${2:-}"
    # Escape & in QUESTION so sed doesn't treat it as "insert match"
    local q_escaped="${QUESTION//&/\\&}"
    common_prompt_preamble
    sed -e "s|__PROJECT_PATH__|$PROJECT|g" \
        -e "s|__RESEARCH_QUESTION__|$q_escaped|g" \
        -e "s|__BASE_NAME__|$BASE|g" \
        -e "s|__FACTORY__|$FACTORY|g" \
        $extra \
        "$PROMPTS/$template"
}

get_user_note() {
    local step="$1"
    local note=""
    local notes_file="$FACTORY/web/notes.json"
    if [[ -f "$notes_file" ]]; then
        note=$(python3 -c "
import json
try:
    with open('$notes_file') as f:
        notes = json.load(f)
    n = notes.get('$BASE', {}).get('step_$step', '')
    if n: print(n)
except: pass
" 2>/dev/null || true)
    fi
    echo "$note"
}

# ── Execution primitives ────────────────────────────────────────────
#
# These replace the old "launch Claude as orchestrator-monitor" pattern.
# Claude is invoked only as a worker or fallback, never as a monitor.

# Kill lingering child processes from this step.
cleanup_children() {
    local my_pids="$$"
    local _apid=$$
    while (( _apid > 1 )); do
        _apid=$(ps -o ppid= -p "$_apid" 2>/dev/null | tr -d ' ') || break
        [[ -z "$_apid" ]] && break
        my_pids="$my_pids $_apid"
    done
    while read -r pid; do
        echo " $my_pids " | grep -q " $pid " && continue
        kill -TERM "$pid" 2>/dev/null || true
    done < <(pgrep -f "$PROJECT" 2>/dev/null || true)
    sleep 2

}

# Find Codex trace file belonging to this project launched after $2 epoch.
find_codex_trace() {
    local project_path="$1" after="${2:-0}"
    local latest="" latest_ts=0
    for f in "$CODEX_SESSIONS"/*/*/*/*.jsonl "$CODEX_SESSIONS"/*/*/*.jsonl "$CODEX_SESSIONS"/*.jsonl; do
        [ -f "$f" ] || continue
        local mt
        mt=$(stat -c %Y "$f" 2>/dev/null || echo 0)
        (( mt <= after )) && continue
        (( mt <= latest_ts )) && continue
        if head -1 "$f" 2>/dev/null | grep -q "$project_path"; then
            latest="$f"
            latest_ts=$mt
        fi
    done
    echo "$latest"
}

# Run a single Codex agent with shell-level hang detection.
# Usage: run_codex <prompt_file> <timeout_secs> [hang_timeout_secs]
# Returns: 0=success, 1=timeout/error, 2=hung(killed)
run_codex() {
    local prompt_file="$1" timeout="$2" hang_timeout="${3:-3600}"
    local rendered
    rendered=$(render_prompt "$prompt_file")
    local note
    note=$(get_user_note "$NEXT")
    [[ -n "$note" ]] && rendered="$rendered

NOTE FROM THE RESEARCHER: $note"
    rendered=$(prepend_agent_key "$(agent_key_from_prompt_file "$prompt_file")" "$rendered")

    local codex_log="$PROJECT/logs/step_${NEXT}_codex_${prompt_file%.txt}_$(date +%Y%m%d_%H%M%S).log"
    local before_ts
    before_ts=$(date +%s)

    log "   Codex: $prompt_file (timeout ${timeout}s)"

    ( cd "$PROJECT" && timeout --kill-after=120 "$timeout" \
        codex exec \
          --model gpt-5.5 \
          -c 'model_reasoning_effort="xhigh"' \
          --dangerously-bypass-approvals-and-sandbox \
          -C "$PROJECT" \
          --skip-git-repo-check \
          "$rendered" \
    ) > "$codex_log" 2>&1 &
    local codex_pid=$!

    # Wait for trace file to appear (up to 60s)
    sleep 15
    local trace_file
    trace_file=$(find_codex_trace "$PROJECT" "$before_ts")
    local last_size=0 stale_since
    stale_since=$(date +%s)

    # Monitor: check trace freshness on a short cadence so completed work
    # is noticed promptly.
    while kill -0 "$codex_pid" 2>/dev/null; do
        sleep "$MONITOR_SLEEP"
        echo "ACTIVE:$NEXT $(date +%s)" > "$PROJECT/.heartbeat"

        # Refresh trace discovery if we haven't found it yet
        if [[ -z "$trace_file" ]] || [[ ! -f "$trace_file" ]]; then
            trace_file=$(find_codex_trace "$PROJECT" "$before_ts")
        fi

        if [[ -n "$trace_file" ]] && [[ -f "$trace_file" ]]; then
            local cur_size
            cur_size=$(stat -c %s "$trace_file" 2>/dev/null || echo 0)
            if (( cur_size > last_size )); then
                last_size=$cur_size
                stale_since=$(date +%s)
            fi
        fi

        local now
        now=$(date +%s)
        if (( now - stale_since > hang_timeout )); then
            # Before killing, check if Codex has active child processes
            # (e.g., stata, python). If children are running, the agent
            # is waiting on a long job, not hung.
            local children
            children=$(pgrep -P "$codex_pid" 2>/dev/null | wc -l)
            if (( children > 0 )); then
                log "   Codex trace stale ${hang_timeout}s but has $children child processes — not hung"
                # Don't reset stale_since: if children also go quiet
                # for another hang_timeout period, we'll kill then
            else
                log "   Codex trace stale for ${hang_timeout}s — killing (hung)"
                kill "$codex_pid" 2>/dev/null || true
                wait "$codex_pid" 2>/dev/null || true
                return 2
            fi
        fi
    done

    wait "$codex_pid"
    local ec=$?
    case "$ec" in
        0)   log "   Codex $prompt_file exited OK" ;;
        124) log "   Codex $prompt_file TIMEOUT" ; return 1 ;;
        *)   log "   Codex $prompt_file exit code $ec" ; return 1 ;;
    esac
    return 0
}

# Run a single agy (Antigravity CLI / Gemini) worker with shell-level hang
# detection.
# Usage: run_agy <prompt_file> <timeout_secs> [hang_timeout_secs]
# Returns: 0=success, 1=timeout/error, 2=hung(killed)
#
# Phase 3.6: this used to shell out to the `agy` CLI binary; it now calls the
# `google-antigravity` Python SDK via scripts/agy_run.py.  See that file for
# the SDK contract (LocalAgentConfig + CapabilitiesConfig + async chat).
#
# agy still differs from codex in three ways the rest of this file cares about:
#  1. Auth is via GEMINI_API_KEY env var ONLY — the SDK does NOT accept the
#     OAuth token at ~/.gemini/antigravity-cli/antigravity-oauth-token that
#     the legacy `agy` CLI used.  run_paper.sh sources $FACTORY/.env on
#     startup, so put `GEMINI_API_KEY=...` there (get a key from
#     https://aistudio.google.com/apikey).  If the var is missing,
#     scripts/agy_run.py exits 2 and run_agy logs a "pip install" / "set
#     GEMINI_API_KEY" hint before falling through to the Claude fallback.
#  2. There is no per-session trace file we can poll (codex writes to
#     ~/.codex/sessions/.../*.jsonl) — so hang detection watches the
#     redirected stdout log file's mtime instead.  The same long-job
#     whitelist (real children: python/julia/matlab/...) applies.
#  3. The SDK is async; we forward our outer timeout to scripts/agy_run.py
#     via --timeout-secs (the inner asyncio.wait_for cancellation fires
#     slightly before the outer bash `timeout` SIGTERM, giving us a clean
#     Python-level traceback instead of a hard kill).
#
# Prompt size: rendered prompts (step 4 / step 7 pull in many files, can hit
# ~30KB) are passed via a tempfile rather than argv, so we're not constrained
# by Linux ARG_MAX.
run_agy() {
    local prompt_file="$1" timeout="$2" hang_timeout="${3:-3600}"
    local rendered
    rendered=$(render_prompt "$prompt_file")
    local note
    note=$(get_user_note "$NEXT")
    [[ -n "$note" ]] && rendered="$rendered

NOTE FROM THE RESEARCHER: $note"
    rendered=$(prepend_agent_key "$(agent_key_from_prompt_file "$prompt_file")" "$rendered")

    local agy_log="$PROJECT/logs/step_${NEXT}_agy_${prompt_file%.txt}_$(date +%Y%m%d_%H%M%S).log"
    local agy_prompt_tmp="$PROJECT/logs/step_${NEXT}_agy_${prompt_file%.txt}_$(date +%Y%m%d_%H%M%S).prompt.txt"
    printf '%s' "$rendered" > "$agy_prompt_tmp"

    log "   Agy: $prompt_file (timeout ${timeout}s, SDK via scripts/agy_run.py)"

    # Pad the inner asyncio timeout slightly under the outer bash 'timeout'
    # wrapper so the outer wrapper is the authoritative kill, but never below
    # 60s.  scripts/agy_run.py exits 124 on inner timeout, matching the unix
    # `timeout` convention so the post-run case statement below stays simple.
    local agy_inner=$(( timeout - 30 ))
    (( agy_inner < 60 )) && agy_inner=$timeout

    # Model: default to gemini-3.1-pro-preview (1M ctx, current top model on
    # the Antigravity SDK as of 2026-05).  Override with AGY_MODEL env var if
    # the SDK defaults shift or the preview name gets promoted.  Note: the
    # name "gemini-3.1-pro" (no -preview) is NOT valid on v1beta — confirmed
    # via ListModels 2026-05-25.
    local agy_model="${AGY_MODEL:-gemini-3.1-pro-preview}"

    ( cd "$PROJECT" && timeout --kill-after=120 "$timeout" \
        "$FACTORY/.venv/bin/python3" "$FACTORY/scripts/agy_run.py" \
            --prompt-file "$agy_prompt_tmp" \
            --timeout-secs "$agy_inner" \
            --workspace "$PROJECT" \
            --workspace "$FACTORY" \
            --model "$agy_model" \
    ) > "$agy_log" 2>&1 &
    local agy_pid=$!

    sleep 5
    local last_mtime
    last_mtime=$(_mtime "$agy_log")
    local stale_since
    stale_since=$(date +%s)

    while kill -0 "$agy_pid" 2>/dev/null; do
        sleep "$MONITOR_SLEEP"
        echo "ACTIVE:$NEXT $(date +%s)" > "$PROJECT/.heartbeat"

        local mt
        mt=$(_mtime "$agy_log")
        if (( mt > last_mtime )); then
            last_mtime=$mt
            stale_since=$(date +%s)
        fi

        local now
        now=$(date +%s)
        if (( now - stale_since > hang_timeout )); then
            local children
            children=$(pgrep -P "$agy_pid" 2>/dev/null | wc -l)
            if (( children > 0 )); then
                log "   Agy log stale ${hang_timeout}s but has $children child processes — not hung"
            else
                log "   Agy log stale for ${hang_timeout}s — killing (hung)"
                kill "$agy_pid" 2>/dev/null || true
                wait "$agy_pid" 2>/dev/null || true
                return 2
            fi
        fi
    done

    wait "$agy_pid"
    local ec=$?
    case "$ec" in
        0)   log "   Agy $prompt_file exited OK" ;;
        2)   log "   Agy $prompt_file: SDK not installed (run: pip install --user google-antigravity)" ; return 1 ;;
        124) log "   Agy $prompt_file TIMEOUT" ; return 1 ;;
        *)   log "   Agy $prompt_file exit code $ec" ; return 1 ;;
    esac
    return 0
}

# Backup wrappers: try the named worker only after a sibling has failed.
run_agy_backup() {
    local prompt_file="$1" timeout="$2" hang_timeout="${3:-3600}"
    log "   Sibling failed for $prompt_file — trying Agy backup"
    run_agy "$prompt_file" "$timeout" "$hang_timeout"
}

# Agy primary, Codex fallback.  Matches run_codex_then_claude's pattern:
# if Agy exits cleanly OR its artifacts already verify, skip the fallback.
run_agy_then_codex() {
    local prompt_file="$1" timeout="$2" hang_timeout="${3:-3600}"
    if run_agy "$prompt_file" "$timeout" "$hang_timeout"; then
        return 0
    fi
    if verify_step "$NEXT"; then
        log "   Agy exited nonzero but step $NEXT artifacts verify — skipping Codex backup"
        return 0
    fi
    run_codex "$prompt_file" "$timeout" "$hang_timeout"
}

# Agy primary, Claude fallback (claude_fallback runs Claude's own tools).
run_agy_then_claude() {
    local prompt_file="$1" timeout="$2" hang_timeout="${3:-3600}"
    if run_agy "$prompt_file" "$timeout" "$hang_timeout"; then
        return 0
    fi
    if verify_step "$NEXT"; then
        log "   Agy exited nonzero but step $NEXT artifacts verify — skipping Claude fallback"
        return 0
    fi
    run_claude_fallback "$NEXT"
}
# Usage: run_claude_worker <prompt_file_or_literal> <timeout_secs> [is_literal]
# When is_literal is "literal", first arg is treated as the prompt text directly.
run_claude_worker() {
    local prompt_src="$1" timeout="$2" is_literal="${3:-}"
    local prompt
    if [[ "$is_literal" == "literal" ]]; then
        prompt="$(common_prompt_preamble)
$prompt_src"
    else
        prompt=$(render_prompt "$prompt_src")
    fi
    local note
    note=$(get_user_note "$NEXT")
    [[ -n "$note" ]] && prompt="$prompt

NOTE FROM THE RESEARCHER: $note"
    if [[ "$is_literal" != "literal" ]]; then
        prompt=$(prepend_agent_key "$(agent_key_from_prompt_file "$prompt_src")" "$prompt")
    fi

    local claude_log="$PROJECT/logs/step_${NEXT}_claude_$(date +%Y%m%d_%H%M%S).log"
    log "   Claude worker: ${prompt_src:0:60}"

    set +e
    ( cd "$PROJECT" && stdbuf -oL -eL timeout --kill-after=120 "$timeout" \
        claude -p "$prompt" --dangerously-skip-permissions --effort max \
    ) > "$claude_log" 2>&1
    local ec=$?
    set -e

    case "$ec" in
        0)   log "   Claude exited OK" ;;
        124) log "   Claude TIMEOUT after ${timeout}s" ;;
        *)   log "   Claude exit code $ec" ;;
    esac
    return $ec
}

# Fallback: Codex failed, retry the step with Claude.
# Claude reads STEPS.md and does the work itself (no Codex launch).
run_claude_fallback() {
    local step="$1"
    log "   FALLBACK: retrying step $step with Claude (Codex failed)"
    local prompt=""

    case "$step" in
        2)
            prompt="Read the current Step 2 contracts at:
- $PROMPTS/step2_modeling_proposal.txt
- $PROMPTS/step2_modeling_critic.txt

Execute ONLY Step 2 for the project at $PROJECT.
Base name: \"$BASE\". Research question:
$QUESTION

Read $PROJECT/viable_streams.md to discover the active stream ids (## Stream m<N>: headers). For each stream, run the proposal agent to produce m<N>_spec.md + m<N>_demo_result.json + models/m<N>_<short>/ code, then run the critic to produce m<N>_critique.md with a verdict line (VALIDATED / REVISE / ABANDONED). Loop proposal↔critic up to 4 rounds per stream. The step passes when at least 2 streams end with VERDICT: VALIDATED; remaining streams may be ABANDONED and their artifacts must be kept.

IMPORTANT: A previous Codex agent attempt for this step FAILED. Do NOT launch any Codex agents. Do the work yourself directly using your own tools (Bash, Read, Write, Edit, Glob, Grep, Agent subagents, etc.). Follow the current Step 2 prompt contracts, but execute the work yourself instead of delegating to Codex.

Execute Step 2 completely, then stop."
            ;;
        3)
            prompt="Read the current Step 3 contract at:
- $PROMPTS/step3_decider.txt

Execute ONLY Step 3 for the project at $PROJECT.
Base name: \"$BASE\". Research question:
$QUESTION

IMPORTANT: A previous Codex agent attempt for this step FAILED. Do NOT launch any Codex agents. Do the work yourself directly using your own tools. Follow the current Step 3 contract, pick one validated findings package, write findings_decision.md and findings_brief.md, then stop."
            ;;
        4)
            prompt="Read the current Step 4 contract at:
- $PROMPTS/step4_model_construction.txt

Execute ONLY Step 4 for the project at $PROJECT.
Base name: \"$BASE\". Research question:
$QUESTION

Read $PROJECT/chosen_method.md to identify the PRIMARY stream m{N} and optional AUXILIARY m{M}. Promote m{N}_spec.md into model.md (≥ 100 lines), produce symbol_table.md and assumption_ledger.md, and populate models/m{N}_<short>/ with runnable 01_data/02_model/03_solve/04_postprocess scripts + at least 3 sanity tests under tests/. If AUXILIARY is not NONE, create models/m{M}_<short>/ as a scaffold only (README + .stub files, no executable code).

IMPORTANT: A previous Agy agent attempt for this step FAILED. Do NOT launch any Agy or Codex agents. Do the work yourself directly using your own tools (Bash, Read, Write, Edit, Glob, Grep, Agent subagents). Follow the current Step 4 prompt contract exactly, then stop."
            ;;
        *)
            prompt="Read the paper skill instructions at $SKILL.

Execute ONLY Step $step for the project at $PROJECT.
Base name: \"$BASE\". Research question:
$QUESTION

IMPORTANT: A previous Codex agent attempt for this step FAILED. Do NOT
launch any Codex agents - do the work yourself directly using your own
tools (Bash, Read, Write, Edit, Glob, Grep, Agent subagents, etc.).
Follow the step instructions but execute the work yourself instead of
delegating to Codex.

Execute Step $step completely, then stop."
            ;;
    esac

    run_claude_worker "$prompt" "$(step_timeout "$step")" "literal"
}

run_codex_backup() {
    local prompt_file="$1" timeout="$2" hang_timeout="${3:-3600}"
    log "   Claude failed for $prompt_file — trying Codex backup"
    run_codex "$prompt_file" "$timeout" "$hang_timeout"
}

run_claude_then_codex() {
    local prompt_file="$1" timeout="$2" hang_timeout="${3:-3600}"
    if run_claude_worker "$prompt_file" "$timeout"; then
        return 0
    fi
    if verify_step "$NEXT"; then
        log "   Claude exited nonzero but step $NEXT artifacts verify — skipping Codex backup"
        return 0
    fi
    run_codex_backup "$prompt_file" "$timeout" "$hang_timeout"
}

run_codex_then_claude() {
    local prompt_file="$1" timeout="$2" hang_timeout="${3:-3600}"
    if run_codex "$prompt_file" "$timeout" "$hang_timeout"; then
        return 0
    fi
    if verify_step "$NEXT"; then
        log "   Codex exited nonzero but step $NEXT artifacts verify — skipping Claude fallback"
        return 0
    fi
    run_claude_fallback "$NEXT"
}

# Run multiple Codex agents in parallel, monitor all.
# Usage: run_codex_parallel <timeout> <hang_timeout> prompt1.txt prompt2.txt ...
# Returns: number of failures
run_codex_parallel() {
    local timeout="$1" hang_timeout="$2"
    shift 2
    local prompt_files=("$@")
    local pids=() traces=() logs=() labels=()
    local before_ts
    before_ts=$(date +%s)

    local note
    note=$(get_user_note "$NEXT")

    for pf in "${prompt_files[@]}"; do
        local rendered
        rendered=$(render_prompt "$pf")
        [[ -n "$note" ]] && rendered="$rendered

NOTE FROM THE RESEARCHER: $note"
        rendered=$(prepend_agent_key "$(agent_key_from_prompt_file "$pf")" "$rendered")

        local codex_log="$PROJECT/logs/step_${NEXT}_codex_${pf%.txt}_$(date +%Y%m%d_%H%M%S).log"
        log "   Launching Codex: $pf"

        ( cd "$PROJECT" && timeout --kill-after=120 "$timeout" \
            codex exec \
              --model gpt-5.5 \
              -c 'model_reasoning_effort="xhigh"' \
              --dangerously-bypass-approvals-and-sandbox \
              -C "$PROJECT" \
              --skip-git-repo-check \
              "$rendered" \
        ) > "$codex_log" 2>&1 &
        pids+=($!)
        logs+=("$codex_log")
        labels+=("$pf")
        traces+=("")  # will be discovered
    done

    # Monitor all agents on a short cadence, kill any that hang.
    local last_sizes=()
    local stale_since=()
    for i in "${!pids[@]}"; do
        last_sizes+=( 0 )
        stale_since+=( "$(date +%s)" )
    done

    while true; do
        # Check if any agents are still running
        local any_alive=false
        for i in "${!pids[@]}"; do
            kill -0 "${pids[$i]}" 2>/dev/null && any_alive=true
        done
        $any_alive || break

        sleep "$MONITOR_SLEEP"
        echo "ACTIVE:$NEXT $(date +%s)" > "$PROJECT/.heartbeat"

        local now
        now=$(date +%s)

        for i in "${!pids[@]}"; do
            kill -0 "${pids[$i]}" 2>/dev/null || continue  # already done

            # Discover trace if not yet found
            if [[ -z "${traces[$i]}" ]]; then
                # Look for traces that appeared after launch
                for f in "$CODEX_SESSIONS"/*/*/*/*.jsonl "$CODEX_SESSIONS"/*/*/*.jsonl "$CODEX_SESSIONS"/*.jsonl; do
                    [ -f "$f" ] || continue
                    local mt
                    mt=$(stat -c %Y "$f" 2>/dev/null || echo 0)
                    (( mt <= before_ts )) && continue
                    if head -1 "$f" 2>/dev/null | grep -q "$PROJECT"; then
                        # Make sure this trace isn't already assigned
                        local already=false
                        for j in "${!traces[@]}"; do
                            [[ "${traces[$j]}" == "$f" ]] && already=true
                        done
                        $already || { traces[$i]="$f"; break; }
                    fi
                done
            fi

            # Check trace freshness
            if [[ -n "${traces[$i]}" ]] && [[ -f "${traces[$i]}" ]]; then
                local cur_size
                cur_size=$(stat -c %s "${traces[$i]}" 2>/dev/null || echo 0)
                if (( cur_size > last_sizes[$i] )); then
                    last_sizes[$i]=$cur_size
                    stale_since[$i]=$now
                fi
            fi

            # Kill if hung — but only if no real work (stata) is running.
            # The process tree is: subshell -> timeout -> codex -> children.
            # pgrep -P on the subshell always finds "timeout", so a simple
            # child-count check never triggers the kill. Instead, walk the
            # full descendant tree and look for stata specifically.
            if (( now - stale_since[$i] > hang_timeout )); then
                local has_work=false
                local desc_pids
                desc_pids=$(pgrep -P "${pids[$i]}" 2>/dev/null || true)
                # Walk the full tree (children, grandchildren, etc.)
                # Look for srun (stata via stata_submit.sh) or stata itself,
                # plus modeling-factory solvers (matlab/gurobi/cplex/scip/ipopt/octave).
                local frontier="$desc_pids"
                while [[ -n "$frontier" ]]; do
                    local next_frontier=""
                    for dpid in $frontier; do
                        local pname
                        pname=$(ps -p "$dpid" -o comm= 2>/dev/null || true)
                        if [[ "$pname" == srun || "$pname" == stata* || "$pname" == python* \
                              || "$pname" == Rscript || "$pname" == R || "$pname" == julia \
                              || "$pname" == matlab* || "$pname" == MATLAB* \
                              || "$pname" == gurobi_cl || "$pname" == cplex* \
                              || "$pname" == scip* || "$pname" == ipopt* \
                              || "$pname" == octave* ]]; then
                            has_work=true
                        fi
                        local grandkids
                        grandkids=$(pgrep -P "$dpid" 2>/dev/null || true)
                        next_frontier="$next_frontier $grandkids"
                    done
                    frontier=$(echo "$next_frontier" | xargs)
                done

                if $has_work; then
                    log "   Codex ${labels[$i]} trace stale but a solver child (stata/python/matlab/gurobi/...) is running — not hung"
                else
                    log "   Codex ${labels[$i]} hung (stale ${hang_timeout}s, no work processes) — killing"
                    kill "${pids[$i]}" 2>/dev/null || true
                fi
            fi
        done
    done

    # Reap all and count failures
    local failures=0
    for i in "${!pids[@]}"; do
        wait "${pids[$i]}" 2>/dev/null
        local ec=$?
        if (( ec != 0 )); then
            log "   Codex ${labels[$i]} failed (exit $ec)"
            failures=$((failures + 1))
        else
            log "   Codex ${labels[$i]} completed OK"
        fi
    done
    return $failures
}

# ── Step dispatch functions ──────────────────────────────────────────
#
# Each step function knows exactly what agents to launch and what
# files to verify. No Claude orchestrator — shell handles monitoring.

run_step_1() {
    # Modeling-mode Step 1: research_brief + viable_streams + viability_gate
    # via a single Claude worker on prompts/step1_research_viability.txt.
    #
    # Per project convention (Phase 3): tasks are routed to Claude as the
    # primary worker; codex primitives stay in the file as a future hook
    # but are not invoked here.  The social-science multi-phase 1A-1E
    # implementation lives in STEPS_original.md and pre-86f21de git
    # history if it ever needs to be revived.
    local rb="$PROJECT/research_brief.md"
    local vs="$PROJECT/viable_streams.md"
    local vg="$PROJECT/viability_gate.md"

    if [[ -f "$rb" && -f "$vs" && -f "$vg" ]] \
       && (( $(_lines "$rb") >= 30 )) \
       && (( $(_lines "$vs") >= 20 )) \
       && (( $(_lines "$vg") >= 10 )); then
        log "   Step 1: artifacts present — skipping worker"
    else
        log "   Step 1: research + method preselection + viability gate (Claude)"
        run_claude_worker step1_research_viability.txt 14400 || true
    fi

    # KILL gate — short-circuit if verdict is KILL.  Reuses the existing
    # step1_viability_verdict() and mark_project_killed() helpers.
    local gate_verdict
    gate_verdict=$(step1_viability_verdict)
    if [[ "$gate_verdict" == "KILL" ]]; then
        if [[ ! -f "$PROJECT/kill_memo.md" ]] || \
           (( $(_lines "$PROJECT/kill_memo.md") < 5 )); then
            log "   kill_memo.md missing/short — retrying Step 1 once"
            run_claude_worker step1_research_viability.txt 14400 || true
            gate_verdict=$(step1_viability_verdict)
        fi
        if [[ "$gate_verdict" == "KILL" && -f "$PROJECT/kill_memo.md" ]] \
           && (( $(_lines "$PROJECT/kill_memo.md") >= 5 )); then
            log "   Step 1 verdict: KILL — pruning intermediates and stopping"
            if [[ -f "$FACTORY/scripts/cleanup_project_artifacts.py" ]]; then
                python3 "$FACTORY/scripts/cleanup_project_artifacts.py" "$PROJECT" \
                    >> "$PROJECT/logs/runner.log" 2>&1 || true
            fi
            mark_project_killed
            return 0
        fi
    fi

    # Pass-path verification
    if [[ "$gate_verdict" != "PASS" ]]; then
        log "   Step 1: viability_gate.md missing or verdict not PASS/KILL"
        return 1
    fi
    if [[ ! -f "$rb" ]] || (( $(_lines "$rb") < 30 )); then
        log "   Step 1: research_brief.md missing or too short"
        return 1
    fi
    if [[ ! -f "$vs" ]] || (( $(_lines "$vs") < 20 )); then
        log "   Step 1: viable_streams.md missing or too short"
        return 1
    fi
    # HMML-lite hard gate: viable_streams.md may cite only registered methods.
    # infer_step() blocks advancement on this; log the offenders so the retry
    # reason is visible rather than the generic "output files missing".
    local _cit_report
    if ! _cit_report=$(check_method_citations "$PROJECT" "$vs"); then
        log "   Step 1: viable_streams.md cites unregistered method(s) — must exist in method_library/index.json:"
        while IFS= read -r _cl; do [[ -n "$_cl" ]] && log "     $_cl"; done <<<"$_cit_report"
        return 1
    fi
    return 0
}


run_step_2() {
    local max_rounds=4
    local proposal_timeout=18000
    local critic_timeout=7200
    local idx

    local -a active_ids
    local active_str
    active_str=$(step2_active_stream_ids "$PROJECT")
    if [[ -z "$active_str" ]]; then
        log "   Step 2: viable_streams.md missing or empty — cannot launch streams"
        return 1
    fi
    read -ra active_ids <<<"$active_str"
    if (( ${#active_ids[@]} < 2 )); then
        log "   Step 2: only ${#active_ids[@]} stream(s) in viable_streams.md (need ≥ 2)"
        return 1
    fi
    log "   Step 2: active streams = ${active_ids[*]} (N=${#active_ids[@]})"

    # Model assignment: last stream goes to Claude for diversification,
    # rest go to Codex. Critic is always Codex (stable structured judge).
    local -A stream_models
    local last_idx="${active_ids[${#active_ids[@]}-1]}"
    for idx in "${active_ids[@]}"; do
        if [[ "$idx" == "$last_idx" ]]; then
            stream_models[$idx]="claude"
        else
            stream_models[$idx]="codex"
        fi
    done

    local -A stream_phases stream_pids stream_rounds stream_critic_attempts

    local note note_block=""
    note=$(get_user_note "$NEXT")
    [[ -n "$note" ]] && note_block="

NOTE FROM THE RESEARCHER: $note"

    launch_proposal_stream() {
        local stream_idx="$1"
        local model="${stream_models[$stream_idx]}"
        local prefix prompt log_path

        prefix="$(step2_stream_prefix "$stream_idx")"
        stream_rounds[$stream_idx]=$(( ${stream_rounds[$stream_idx]:-0} + 1 ))
        stream_critic_attempts[$stream_idx]=0

        prompt=$(render_prompt step2_modeling_proposal.txt)
        prompt="${prompt//__STREAM_ID__/$stream_idx}"
        prompt="${prompt//__STREAM_PREFIX__/$prefix}"
        prompt="$prompt$note_block"
        prompt=$(prepend_agent_key "step2_modeling_proposal_${stream_idx}" "$prompt")

        log_path="$PROJECT/logs/step_${NEXT}_${prefix}_${model}_proposal_r${stream_rounds[$stream_idx]}_$(date +%Y%m%d_%H%M%S).log"
        log "   Step 2: launching proposal stream $stream_idx ($model, round ${stream_rounds[$stream_idx]})"

        if [[ "$model" == "codex" ]]; then
            (
                cd "$PROJECT" && timeout --kill-after=120 "$proposal_timeout" \
                    codex exec \
                      --model gpt-5.5 \
                      -c 'model_reasoning_effort="xhigh"' \
                      --dangerously-bypass-approvals-and-sandbox \
                      -C "$PROJECT" \
                      --skip-git-repo-check \
                      "$prompt"
            ) > "$log_path" 2>&1 &
        else
            (
                cd "$PROJECT" && stdbuf -oL -eL timeout --kill-after=120 "$proposal_timeout" \
                    claude -p "$prompt" --dangerously-skip-permissions --effort max
            ) > "$log_path" 2>&1 &
        fi

        stream_pids[$stream_idx]=$!
        stream_phases[$stream_idx]="proposal"
    }

    launch_critic_stream() {
        local stream_idx="$1"
        local prefix prompt log_path

        prefix="$(step2_stream_prefix "$stream_idx")"
        stream_critic_attempts[$stream_idx]=$(( ${stream_critic_attempts[$stream_idx]:-0} + 1 ))

        prompt=$(render_prompt step2_modeling_critic.txt)
        prompt="${prompt//__STREAM_ID__/$stream_idx}"
        prompt="${prompt//__STREAM_PREFIX__/$prefix}"
        prompt="$prompt$note_block"
        prompt=$(prepend_agent_key "step2_modeling_critic_${stream_idx}" "$prompt")

        log_path="$PROJECT/logs/step_${NEXT}_${prefix}_critic_a${stream_critic_attempts[$stream_idx]}_$(date +%Y%m%d_%H%M%S).log"
        log "   Step 2: launching critic for stream $stream_idx"

        (
            cd "$PROJECT" && timeout --kill-after=120 "$critic_timeout" \
                codex exec \
                  --model gpt-5.5 \
                  -c 'model_reasoning_effort="xhigh"' \
                  --dangerously-bypass-approvals-and-sandbox \
                  -C "$PROJECT" \
                  --skip-git-repo-check \
                  "$prompt"
        ) > "$log_path" 2>&1 &

        stream_pids[$stream_idx]=$!
        stream_phases[$stream_idx]="critic"
    }

    for idx in "${active_ids[@]}"; do
        local verdict
        verdict=$(step2_critique_verdict "$PROJECT" "$idx")
        stream_rounds[$idx]=0
        stream_critic_attempts[$idx]=0
        stream_pids[$idx]=""

        if step2_stream_validated "$PROJECT" "$idx"; then
            stream_phases[$idx]="done"
            log "   Step 2: stream $idx already validated — skipping"
        elif step2_stream_abandoned "$PROJECT" "$idx"; then
            stream_phases[$idx]="done"
            log "   Step 2: stream $idx already abandoned — keeping artifacts, not relaunching"
        elif step2_stream_ready "$PROJECT" "$idx" && [[ -z "$verdict" ]]; then
            launch_critic_stream "$idx"
        else
            [[ -f "$(step2_critique_path "$PROJECT" "$idx")" ]] && stream_rounds[$idx]=1
            launch_proposal_stream "$idx"
        fi
    done

    while true; do
        local all_done=true
        for idx in "${active_ids[@]}"; do
            if [[ "${stream_phases[$idx]}" != "done" ]]; then
                all_done=false
                break
            fi
        done
        $all_done && break

        sleep "$MONITOR_SLEEP"
        echo "ACTIVE:$NEXT $(date +%s)" > "$PROJECT/.heartbeat"

        for idx in "${active_ids[@]}"; do
            local phase pid ec verdict
            phase="${stream_phases[$idx]}"
            pid="${stream_pids[$idx]}"
            [[ "$phase" == "done" || -z "$pid" ]] && continue
            kill -0 "$pid" 2>/dev/null && continue

            wait "$pid" 2>/dev/null
            ec=$?
            stream_pids[$idx]=""

            if [[ "$phase" == "proposal" ]]; then
                if (( ec == 0 )) && step2_stream_ready "$PROJECT" "$idx"; then
                    launch_critic_stream "$idx"
                elif (( stream_rounds[$idx] < max_rounds )); then
                    log "   Step 2: proposal stream $idx incomplete (exit $ec) — retrying"
                    launch_proposal_stream "$idx"
                else
                    log "   Step 2: proposal stream $idx exhausted retries"
                    stream_phases[$idx]="done"
                fi
            elif [[ "$phase" == "critic" ]]; then
                verdict=$(step2_critique_verdict "$PROJECT" "$idx")
                if (( ec == 0 )) && [[ "$verdict" == "VALIDATED" ]]; then
                    stream_phases[$idx]="done"
                    log "   Step 2: stream $idx VALIDATED"
                elif (( ec == 0 )) && [[ "$verdict" == "ABANDONED" ]]; then
                    stream_phases[$idx]="done"
                    log "   Step 2: stream $idx ABANDONED (kept for Step 3 reference)"
                elif (( ec == 0 )) && [[ "$verdict" == "REVISE" ]]; then
                    if (( stream_rounds[$idx] < max_rounds )); then
                        log "   Step 2: critic requested revision for stream $idx"
                        launch_proposal_stream "$idx"
                    else
                        log "   Step 2: stream $idx hit max revision rounds — treating as ABANDONED"
                        stream_phases[$idx]="done"
                    fi
                elif (( stream_critic_attempts[$idx] < 2 )); then
                    log "   Step 2: critic output for stream $idx unusable (exit $ec, verdict '${verdict:-missing}') — retrying critic"
                    launch_critic_stream "$idx"
                else
                    log "   Step 2: critic for stream $idx exhausted retries"
                    stream_phases[$idx]="done"
                fi
            fi
        done
    done

    # Step 2 passes if ≥ 2 streams ended up VALIDATED. Streams that
    # ABANDONED or exhausted retries are recorded but don't count.
    if step2_has_enough_validated_streams "$PROJECT"; then
        local validated=0 abandoned=0
        for idx in "${active_ids[@]}"; do
            if step2_stream_validated "$PROJECT" "$idx"; then
                validated=$((validated + 1))
            elif step2_stream_abandoned "$PROJECT" "$idx"; then
                abandoned=$((abandoned + 1))
            fi
        done
        log "   Step 2: complete — $validated validated, $abandoned abandoned, $((${#active_ids[@]} - validated - abandoned)) other"
        return 0
    fi

    log "   Step 2: insufficient validated streams (need ≥ 2)"
    return 1
}

run_step_3() {
    # Modeling-mode Step 3: method selection.  Single Claude worker reads all
    # VALIDATED m{N} streams, picks PRIMARY + optional AUXILIARY, honors the
    # `## Step 3 decision:` human-override section in human_review.md if
    # present.  Mirrors run_step_1's primary-Claude convention.  The original
    # social-science decider lives at prompts/step3_decider.txt and in
    # STEPS_original.md / pre-86f21de git history.
    local md="$PROJECT/method_decision.md"
    local cm="$PROJECT/chosen_method.md"

    if [[ -f "$md" && -f "$cm" ]] \
       && (( $(_lines "$md") >= 30 )) \
       && (( $(_lines "$cm") >= 10 )) \
       && grep -q "^PRIMARY:" "$cm" 2>/dev/null; then
        log "   Step 3: artifacts present — skipping worker"
        return 0
    fi

    log "   Step 3: method selection (Claude)"
    run_claude_worker step3_method_selection.txt 7200 || true

    if [[ ! -f "$md" ]] || (( $(_lines "$md") < 30 )); then
        log "   Step 3: method_decision.md missing or < 30 lines"
        return 1
    fi
    if [[ ! -f "$cm" ]] || (( $(_lines "$cm") < 10 )); then
        log "   Step 3: chosen_method.md missing or < 10 lines"
        return 1
    fi
    if ! grep -q "^PRIMARY:" "$cm" 2>/dev/null; then
        log "   Step 3: chosen_method.md missing PRIMARY: marker on line 1"
        return 1
    fi
    return 0
}

run_step_4() {
    # Modeling-mode Step 4: full model construction.  Single Agy worker
    # (Claude fallback) reads chosen_method.md to identify PRIMARY (and
    # optional AUXILIARY), expands m{primary}_spec.md into the canonical
    # cross-step trio (model.md / symbol_table.md / assumption_ledger.md)
    # plus runnable code under models/m{primary}_<short>/.  AUXILIARY gets
    # scaffold-only (README + stub + entry comment); full impl deferred to
    # Step 5/6.  The original social-science multi-phase (extensions /
    # architects / decider+auditor / executor) is preserved in
    # prompts/step4_{ext*,architect*,decider,auditor,executor}.txt and
    # STEPS_original.md / pre-Phase-3.5 git history.
    local mdl="$PROJECT/model.md"
    local sym="$PROJECT/symbol_table.md"
    local ledger="$PROJECT/assumption_ledger.md"

    if [[ -f "$mdl" && -f "$sym" && -f "$ledger" ]] \
       && (( $(_lines "$mdl") >= 100 )) \
       && (( $(_lines "$sym") >= 10 )) \
       && (( $(_lines "$ledger") >= 10 )); then
        log "   Step 4: artifacts present — skipping worker"
        return 0
    fi

    log "   Step 4: full model construction (Agy → Claude fallback)"
    run_agy_then_claude step4_model_construction.txt 14400 3600 || true

    if [[ ! -f "$mdl" ]] || (( $(_lines "$mdl") < 100 )); then
        log "   Step 4: model.md missing or < 100 lines"
        return 1
    fi
    if [[ ! -f "$sym" ]] || (( $(_lines "$sym") < 10 )); then
        log "   Step 4: symbol_table.md missing or < 10 lines"
        return 1
    fi
    if [[ ! -f "$ledger" ]] || (( $(_lines "$ledger") < 10 )); then
        log "   Step 4: assumption_ledger.md missing or < 10 lines"
        return 1
    fi
    return 0
}

run_step_5() {
    # Modeling-mode Step 5 — full solve across all sub-problems.
    # Codex primary (numerical / shell-heavy work), Claude fallback.
    # Hang detection budget 1h matches Step 4's; the solver itself runs
    # via solver_submit.sh and counts as "real work" for hang detection
    # (CLAUDE.md hang-detection rule).
    run_codex_then_claude step5_full_solve.txt 14400 3600
}
run_step_6()  { run_codex_then_claude step6_sensitivity.txt 10800 3600; }

run_step_7()  { run_claude_then_codex step7_model_eval.txt 7200 1800; }
run_step_8()  { run_claude_then_codex step8_visualization.txt 10800 3600; }
run_step_9()  { run_claude_then_codex step9_paper_draft.txt 14400 3600; }

run_step_10() { run_codex_then_claude step10_gate1_numerical.txt 10800 3600; }

run_step_11() { run_codex_then_claude step11_constructive_review.txt 7200 1800; }

run_step_12() { run_claude_then_codex step12_revision.txt 14400 3600; }
run_step_13() { run_codex_then_claude step13_gate2_judge.txt 10800 3600; }

run_step_14() { run_claude_then_codex step14_abstract.txt 7200 1800; }
run_step_15() { run_codex_then_claude step15_polish.txt 10800 3600; }

run_step_16() {
    log "   Recompiling PDF"
    if "$FACTORY/compile_paper.sh" "$PROJECT" "$BASE" >> "$PROJECT/logs/runner.log" 2>&1; then
        log "   compile_paper.sh finished"
    else
        log "   WARNING: compile_paper.sh failed — copying existing PDF if present"
    fi
    log "   Delivering final PDF"
    mkdir -p "$FACTORY/papers"
    cp "$PROJECT/${BASE}_paper.pdf" "$FACTORY/papers/" 2>/dev/null || true
    if [[ -x "$FACTORY/scripts/package_submission.py" ]]; then
        log "   Creating submission bundle"
        if "$FACTORY/scripts/package_submission.py" "$PROJECT" "$BASE" "$FACTORY/papers/${BASE}_submission.zip" >> "$PROJECT/logs/runner.log" 2>&1; then
            log "   Submission bundle OK"
        else
            log "   WARNING: submission bundle creation failed (exit $?)"
            return 1
        fi
    else
        log "   WARNING: package_submission.py not found or not executable"
        return 1
    fi
    if [[ -x "$FACTORY/scripts/cleanup_project_artifacts.py" ]]; then
        log "   Cleaning rebuildable intermediate data"
        if "$FACTORY/scripts/cleanup_project_artifacts.py" "$PROJECT" >> "$PROJECT/logs/runner.log" 2>&1; then
            log "   Intermediate data cleanup OK"
        else
            log "   WARNING: intermediate data cleanup failed (exit $?)"
        fi
    else
        log "   WARNING: cleanup_project_artifacts.py not found or not executable"
    fi
    # Update checkpoint
    sed -i "s/\*\*Last completed step\*\*: .*/\*\*Last completed step\*\*: 16/" \
        "$PROJECT/checkpoint.md" 2>/dev/null || true
}

# ── Main step loop ────────────────────────────────────────────────────

PROJECT_KILLED=0
while (( STEP < 16 )); do
    NEXT=$((STEP + 1))
    RETRIES=0
    TIMEOUT=$(step_timeout "$NEXT")

    while (( RETRIES < MAX_RETRIES )); do
        log "-- Step $NEXT | attempt $((RETRIES + 1))/$MAX_RETRIES | timeout ${TIMEOUT}s --"

        STEP_LOG="$PROJECT/logs/step_${NEXT}_$(date +%Y%m%d_%H%M%S).log"
        log "   log: $STEP_LOG"
        touch "$STEP_LOG"

        echo "ACTIVE:$NEXT $(date +%s)" > "$PROJECT/.heartbeat"

        # Start activity monitor in background
        _start_activity_monitor "$STEP_LOG" "$NEXT" "$PROJECT/.heartbeat" &
        MONITOR_PID=$!

        # Dispatch to step-specific function
        set +e
        case "$NEXT" in
            1)  run_step_1  ;;
            2)  run_step_2  ;;
            3)  run_step_3  ;;
            4)  run_step_4  ;;
            5)  run_step_5  ;;
            6)  run_step_6  ;;
            7)  run_step_7  ;;
            8)  run_step_8  ;;
            9)  run_step_9  ;;
            10) run_step_10 ;;
            11) run_step_11 ;;
            12) run_step_12 ;;
            13) run_step_13 ;;
            14) run_step_14 ;;
            15) run_step_15 ;;
            16) run_step_16 ;;
        esac
        set -e

        # Stop activity monitor
        kill "$MONITOR_PID" 2>/dev/null || true
        wait "$MONITOR_PID" 2>/dev/null || true

        # Kill lingering child processes
        cleanup_children

        if project_is_killed "$PROJECT"; then
            PROJECT_KILLED=1
            log "   Project marked KILLED at the Step 1 viability gate"
            break
        fi

        # Verify step completed regardless of exit code.
        if verify_step "$NEXT"; then
            if (( NEXT == 13 )); then
                verdict=""
                verdict=$(gate2_verdict)

                if [[ -f "$(gate2_reopen_marker)" ]]; then
                    rm -f "$(gate2_reopen_marker)"
                    log "   Gate 2 reopen cycle completed"
                    if [[ "$verdict" == "REOPEN_REVISION_TEXT" || "$verdict" == "REOPEN_REVISION_MODEL" ]]; then
                        log "   Step 13 verdict $verdict after prior reopen — proceeding to Step 14 per policy"
                    fi
                elif [[ "$verdict" == "REOPEN_REVISION_TEXT" || "$verdict" == "REOPEN_REVISION_MODEL" ]]; then
                    if [[ ! -f "$(gate2_reopened_once_file)" ]]; then
                        touch "$(gate2_reopened_once_file)"
                        touch "$(gate2_reopen_marker)"
                        log "   Step 13 verdict $verdict — reopening Step 12 once"
                        STEP=11
                        echo "$STEP $(date +%s)" > "$PROJECT/.heartbeat"
                        sed -i "s/\*\*Last completed step\*\*: .*/\*\*Last completed step\*\*: $STEP/" \
                            "$PROJECT/checkpoint.md" 2>/dev/null || true
                        sed -i "s/\*\*Timestamp\*\*: .*/\*\*Timestamp\*\*: $(date '+%Y-%m-%d %H:%M')/" \
                            "$PROJECT/checkpoint.md" 2>/dev/null || true
                        break
                    else
                        log "   Step 13 verdict $verdict after prior reopen — proceeding to Step 14 per policy"
                    fi
                elif [[ -z "$verdict" ]]; then
                    log "   Step 13 judge_evaluation.md has no VERDICT line — treating as pass"
                elif [[ "$verdict" != "PASS" ]]; then
                    log "   Step 13 VERDICT '$verdict' not recognized — treating as pass"
                fi
            fi

            log "   Step $NEXT VERIFIED"
            STEP=$NEXT
            echo "$STEP $(date +%s)" > "$PROJECT/.heartbeat"
            # Update checkpoint.md from shell (don't rely on LLM)
            sed -i "s/\*\*Last completed step\*\*: .*/\*\*Last completed step\*\*: $STEP/" \
                "$PROJECT/checkpoint.md" 2>/dev/null || true
            sed -i "s/\*\*Timestamp\*\*: .*/\*\*Timestamp\*\*: $(date '+%Y-%m-%d %H:%M')/" \
                "$PROJECT/checkpoint.md" 2>/dev/null || true
            break
        else
            RETRIES=$((RETRIES + 1))
            log "   Step $NEXT NOT VERIFIED (expected output files missing)"
            if (( RETRIES < MAX_RETRIES )); then
                log "   Retrying in 30s..."
                sleep 30
            fi
        fi
    done

    if (( PROJECT_KILLED )); then
        break
    fi

    if (( RETRIES >= MAX_RETRIES )); then
        log "FATAL: Step $NEXT failed after $MAX_RETRIES attempts. Stopping."
        echo "STUCK:$NEXT $(date +%s)" > "$PROJECT/.heartbeat"
        exit 1
    fi
done

if (( PROJECT_KILLED )); then
    log "Project terminated by the viability gate. See kill_memo.md."
    rm -f "$PROJECT/.step11_reopen_to_step10" "$PROJECT/.step11_reopened_once" \
          "$PROJECT/.gate2_reopen_to_revision" "$PROJECT/.gate2_reopened_once" 2>/dev/null || true
    log "========================================"
    exit 0
fi

log "All 16 steps complete. Paper delivered."

rm -f "$PROJECT/.step11_reopen_to_step10" "$PROJECT/.step11_reopened_once" \
      "$PROJECT/.gate2_reopen_to_revision" "$PROJECT/.gate2_reopened_once" 2>/dev/null || true

# Move completed project from ongoing/ to complete/
DEST="$FACTORY/complete/$BASE"
if [[ ! -d "$DEST" ]]; then
    mkdir -p "$FACTORY/complete"
    rmdir "$LOCKDIR" 2>/dev/null || true
    mv "$PROJECT" "$DEST"
    PROJECT="$DEST"
    LOCKDIR="$PROJECT/.runner.lock"
    LOCKINFO="$PROJECT/.runner.lock.info"
    [[ -L "$FACTORY/$BASE" ]] && rm -f "$FACTORY/$BASE"
    log "Moved project to $DEST"
else
    log "WARNING: $DEST already exists, skipping move"
fi

remove_registry_entry "$BASE"
log "========================================"
