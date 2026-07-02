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
# Step 6/13 定制化重试限制(消融实验显示这两步容易进入重试循环)
MAX_RETRIES_STEP6=3  # 敏感性分析: 降低重试次数,快速失败并诊断
MAX_RETRIES_STEP13=2 # 评委打分: 最多 2 次尝试,避免无效循环
REGISTRY="$FACTORY/run_state/process_registry"
KILL_MARKER=".killed"
REVIEW_STATE_FILE=".review_state.json"

# Source $FACTORY/.env if present for non-secret runtime settings such as
# GCP_PROJECT_ID / GCP_REGION. Sensitive values are loaded from Secret Manager
# by load_runtime_secrets below so stale local .env entries cannot win.
if [[ -f "$FACTORY/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "$FACTORY/.env"
    set +a
fi

load_runtime_secrets() {
    if [[ -f "$FACTORY/scripts/load_secrets.sh" ]]; then
        # shellcheck disable=SC1091
        source "$FACTORY/scripts/load_secrets.sh"
    else
        echo "ERROR: missing $FACTORY/scripts/load_secrets.sh" >&2
        return 1
    fi
}

export PATH="$HOME/local/node/bin:$PATH"

if [[ -f "$FACTORY/scripts/runner_diagnostics.sh" ]]; then
    # shellcheck disable=SC1091
source "$FACTORY/scripts/runner_diagnostics.sh"
source "$FACTORY/scripts/runner_state.sh"
fi

# ── Ablation toggles (experiments/) ──────────────────────────────────
#
# Each toggle disables one pipeline mechanism so the experiments/ harness
# can measure its marginal contribution to paper quality (docs/refactor_plan
# §5.4). All are OFF unless set to a truthy value (1/true/yes/on). They are
# read from the environment (launchers `export` them) and survive the snapshot
# re-exec below because it uses `env` without -i. See experiments/README.md.
_ablate_on() { case "${1,,}" in 1|true|yes|on) return 0;; *) return 1;; esac; }
ABLATE_NO_CONSULTATION="${ABLATE_NO_CONSULTATION:-0}"
ABLATE_NO_METHOD_LIB="${ABLATE_NO_METHOD_LIB:-0}"
ABLATE_NO_JUDGE="${ABLATE_NO_JUDGE:-0}"
ABLATE_NO_INNOVATION_PROTECT="${ABLATE_NO_INNOVATION_PROTECT:-0}"


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

gate2_verdict_for_path() {
    local proj_dir="$1"
    awk -F': *' '/^VERDICT:/{print $2; exit}' "$proj_dir/judge_evaluation.md" 2>/dev/null \
        | tr -d '\r'
}

gate2_passed_for_path() {
    [[ "$(gate2_verdict_for_path "$1")" == "PASS" ]]
}

step8_5_verdict_for_path() {
    local proj_dir="$1"
    if [[ -f "$FACTORY/scripts/step8_5_gate.py" ]]; then
        python3 "$FACTORY/scripts/step8_5_gate.py" "$proj_dir" --verdict 2>/dev/null || true
    else
        awk -F': *' '/^VERDICT:/{print $2; exit}' "$proj_dir/entry_gate.md" 2>/dev/null \
            | tr -d '\r'
    fi
}

step8_5_passed_for_path() {
    [[ "$(step8_5_verdict_for_path "$1")" == "PASS" ]]
}

delivery_quality_gate() {
    local proj_dir="$1"
    [[ -x "$FACTORY/scripts/evaluate_modeling_project.py" ]] || return 1
    "$FACTORY/scripts/evaluate_modeling_project.py" "$proj_dir" --json
}

number_gate_passed() {
    local proj_dir="$1" base_name="$2"
    [[ -f "$proj_dir/numbers_manifest.json" ]] || return 1
    python3 "$FACTORY/scripts/verify_numbers.py" --verify "$proj_dir" "$base_name" \
        > "$proj_dir/number_verification.latest.stdout" \
        2> "$proj_dir/number_verification.latest.stderr"
}

symbol_gate_passed() {
    local proj_dir="$1" base_name="$2"
    local out="$proj_dir/symbol_verification.latest.txt"
    if python3 "$FACTORY/scripts/verify_symbols.py" "$proj_dir" "$base_name" > "$out" 2>&1; then
        return 0
    fi

    local used undefined coverage
    used=$(awk -F= '/SYMBOLS_USED/{gsub(/[^0-9]/, "", $2); print $2; exit}' "$out" 2>/dev/null || true)
    undefined=$(awk -F= '/UNDEFINED_SYMBOLS/{gsub(/[^0-9]/, "", $2); print $2; exit}' "$out" 2>/dev/null || true)
    [[ -n "$used" && -n "$undefined" && "$used" -gt 0 ]] || return 1
    coverage=$(awk -v used="$used" -v undefined="$undefined" 'BEGIN { printf "%.3f", 1 - undefined / used }')
    awk -v c="$coverage" 'BEGIN { exit !(c >= 0.5) }' || return 1
    grep -qiE '未登记符号|undefined_symbols|UNDEFINED_SYMBOLS|白名单|补登记|symbol' "$proj_dir/code_review.md" 2>/dev/null
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
    if _ablate_on "$ABLATE_NO_METHOD_LIB"; then
        echo "ABLATION: method-library citation gate disabled (ABLATE_NO_METHOD_LIB)"
        return 0
    fi
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
    grep -oP '^## Stream m[0-9]+[：:]' "$f" 2>/dev/null \
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

# True if a step's output is "fresh" relative to an active review/reopen cycle.
# Extracted from infer_step (was a nested function redefined on every call). It
# reads `review_resume_step` / `review_requested_at` from its CALLER's locals via
# bash dynamic scope, so it must only be called from within infer_step (verified
# sole caller). Semantics unchanged: with no active review cycle (resume_step<=0 /
# requested_at<=0) or for steps below the resume point, output counts as fresh;
# otherwise at least one given file must be newer than the reopen-request epoch.
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

# Modeling-mode step inference, extracted from infer_step (body moved verbatim).
# Echoes the matched step number to stdout, or nothing if no modeling step matches.
# Called only from infer_step via $(...); the subshell inherits infer_step's
# review_resume_step / review_requested_at locals, which _review_step_is_fresh reads
# through bash dynamic scope.
_infer_step_modeling() {
    local P="$1" base="$2" paper="$3"
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
            && step8_5_passed_for_path "$P" \
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

        # 4: model.md + symbol_table.md + assumption_ledger.md +
        # modeling_scope_gate.md (Step 4
        # contract).  Checked before step 3 because chosen_method.md /
        # method_decision.md remain on disk past step 4 — descending by
        # step number ensures the latest completed step is reported.
        if [[ -f "$P/model.md" && -f "$P/symbol_table.md" && \
              -f "$P/assumption_ledger.md" && -f "$P/modeling_scope_gate.md" ]] \
            && (( $(_lines "$P/model.md") >= 100 )) \
            && (( $(_lines "$P/symbol_table.md") >= 10 )) \
            && (( $(_lines "$P/assumption_ledger.md") >= 10 )) \
            && grep -q '^VERDICT: PASS' "$P/modeling_scope_gate.md" 2>/dev/null; then
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
}

infer_step() {
    local P="$1" base="$2"
    local paper="$P/${base}_paper.tex"
    local reopen_marker="$P/.step11_reopen_to_step10"
    local review_resume_step=0
    local review_requested_at=0

    # review_resume_step / review_requested_at feed the module-level
    # _review_step_is_fresh (defined above) via bash dynamic scope — they must
    # stay local to infer_step.
    read -r review_resume_step review_requested_at < <(review_cycle_info "$P")

    # Killed projects stop before Step 1 completes.
    [[ -f "$P/$KILL_MARKER" ]] && echo 0 && return

    # 16: final PDF and submission bundle delivered to papers/, with the final
    # quality gates still passing. A stale PDF/zip pair is not enough.
    if [[ -f "$FACTORY/papers/${base}_paper.pdf" && -f "$FACTORY/papers/${base}_submission.zip" ]] \
        && gate2_passed_for_path "$P" \
        && step8_5_passed_for_path "$P"; then
        echo 16
        return
    fi

    # Modeling-mode step inference — short-circuits when the project has a
    # problem/ directory (set by Step 0 problem parsing).  Modeling artifacts
    # for Steps 5-16 are checked descending so the latest completed step is
    # reported even when older artifacts remain on disk.
    if is_modeling_project "$P"; then
        local _m; _m="$(_infer_step_modeling "$P" "$base" "$paper")"
        [[ -n "$_m" ]] && echo "$_m" && return
    fi

    # Legacy social-science step inference — kept INLINE (not extracted like the
    # modeling branch above) because it has no characterization-test coverage;
    # extracting it without a safety net is deferred. Active domain is modeling
    # (see CLAUDE.md); this path serves old social-science projects only.
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

load_runtime_secrets

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

# Integrate state_manager (P1-1): source once, then dual-write .state.json at key
# state transitions. NOTE: .state.json is currently a MIRROR — readers (infer_step,
# launch_agents, dashboard) are NOT yet migrated to consume it; that migration is a
# separate, higher-risk follow-up. Sourcing is safe: zero symbol clashes with
# run_paper.sh, both use `set -euo pipefail`, and state_manager.sh has no top-level
# side effects. Dual-writes are best-effort (no-op via `declare -F` guards if jq is
# missing or the source failed).
if [[ -f "$FACTORY/scripts/state_manager.sh" ]]; then
    source "$FACTORY/scripts/state_manager.sh"
    state_init "$PROJECT" 2>/dev/null || true
fi

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

# Atomically rewrite checkpoint.md's "Last completed step" (and "Timestamp")
# in a single temp-file+rename, instead of two separate in-place `sed -i` calls
# that leave a brief window where step is updated but timestamp is not. No-op if
# checkpoint.md is missing. checkpoint.md is display-only (file-state is
# authoritative), so a missed timestamp is harmless; a torn file is not.
_set_checkpoint_step() {
    local step="$1"
    local cp="$PROJECT/checkpoint.md"
    [[ -f "$cp" ]] || return 0
    local ts tmp
    ts="$(date '+%Y-%m-%d %H:%M')"
    tmp="$(mktemp "${cp}.XXXXXX")" || return 0
    if sed -e "s/\*\*Last completed step\*\*: .*/\*\*Last completed step\*\*: ${step}/" \
           -e "s/\*\*Timestamp\*\*: .*/\*\*Timestamp\*\*: ${ts}/" \
           "$cp" > "$tmp" 2>/dev/null; then
        mv -f "$tmp" "$cp"
    else
        rm -f "$tmp"
    fi
    # Dual-write the step to the .state.json mirror (best-effort; readers not yet
    # migrated). No-op when state_manager wasn't sourced (e.g. jq missing).
    if declare -F state_update >/dev/null 2>&1; then
        state_update "$PROJECT" "progress.last_completed_step" "$step" 2>/dev/null || true
    fi
}

mark_project_killed() {
    local timestamp
    timestamp=$(date +%s)
    touch "$PROJECT/$KILL_MARKER"
    echo "KILLED:1 $timestamp" > "$PROJECT/.heartbeat"
    _set_checkpoint_step 0
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
            hb_step="${hb_step#STUCK:}"              # strip prefix if stuck
            hb_step="${hb_step#ACTIVE:}"             # strip prefix if active
            hb_step="${hb_step#AWAITING_STEP8_5:}"   # strip prefix if awaiting 8.5 gate
            # Guard the arithmetic: an unknown prefix (or a future marker)
            # would otherwise throw a bash syntax error in $(( hb_step + 1 )).
            # Non-numeric hb_step falls through to the lock-age fallback below.
            if [[ -n "$hb_ts" && "$hb_ts" =~ ^[0-9]+$ && "$hb_step" =~ ^[0-9]+$ ]]; then
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
            diag_event "$PROJECT" "${next_step:-0}" lock_stale_reclaimed LOCK_STALE_RECLAIMED \
                "Stale lock reclaimed: $stale_reason" "logs/runner.log"
            diag_status "$PROJECT" waiting "${next_step:-0}" lock_recovery LOCK_STALE_RECLAIMED \
                "检测到陈旧锁并已回收" "open_runner_log,refresh_status" "file:logs/runner.log"
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

_set_checkpoint_step() {
    local step="$1"
    sed -i "s/\*\*Last completed step\*\*: .*/\*\*Last completed step\*\*: $step/" \
        "$PROJECT/checkpoint.md" 2>/dev/null || true
    sed -i "s/\*\*Timestamp\*\*: .*/\*\*Timestamp\*\*: $(date '+%Y-%m-%d %H:%M')/" \
        "$PROJECT/checkpoint.md" 2>/dev/null || true
}

get_question() {
    grep "Research question" "$PROJECT/checkpoint.md" 2>/dev/null \
        | sed 's/.*\*\*: //'
}

# ── Semantic verification for step outputs ──────────────────────────
#
# Verifies that step outputs are not just present but semantically valid.
# Goes beyond line-count thresholds to check for structural markers that
# indicate real completion (VERDICT lines, proper escaping, minimum content).
#
# Returns 0 if the step output passes semantic checks, 1 otherwise.
verify_step_output() {
    local step="$1" P="$PROJECT"

    case "$step" in
        1)
            # Step 1: viable_streams.md and viability_gate.md must exist with VERDICT
            [[ -f "$P/viable_streams.md" ]] || return 1
            [[ -f "$P/viability_gate.md" ]] || return 1
            grep -q "^VERDICT:" "$P/viability_gate.md" || return 1
            # At least one stream should be listed
            grep -q "^## Stream m[0-9]" "$P/viable_streams.md" || return 1
            ;;
        2)
            # Step 2: at least one validated stream
            local validated=0
            for f in "$P"/m*_critique.md; do
                [[ -f "$f" ]] || continue
                if grep -q "^VERDICT: VALIDATED" "$f"; then
                    validated=1
                    break
                fi
            done
            (( validated )) || return 1
            ;;
        3)
            # Step 3: method_decision.md and chosen_method.md must exist
            [[ -f "$P/method_decision.md" ]] || return 1
            [[ -f "$P/chosen_method.md" ]] || return 1
            # Chosen method should reference a stream
            grep -q "m[0-9]" "$P/chosen_method.md" || return 1
            ;;
        4)
            # Step 4: model.md, symbol_table.md, assumption_ledger.md, modeling_scope_gate.md
            [[ -f "$P/model.md" ]] || return 1
            [[ -f "$P/symbol_table.md" ]] || return 1
            [[ -f "$P/assumption_ledger.md" ]] || return 1
            [[ -f "$P/modeling_scope_gate.md" ]] || return 1
            grep -q '^VERDICT: PASS' "$P/modeling_scope_gate.md" || return 1
            # Symbol table should have at least some entries
            (( $(_lines "$P/symbol_table.md") >= 10 )) || return 1
            ;;
        9)
            # Step 9: paper.tex must have ABSTRACT_PLACEHOLDER properly escaped
            [[ -f "$P/paper/paper.tex" ]] || return 1
            # Check for proper detokenize or at least presence of placeholder
            if grep -q 'detokenize.*ABSTRACT.*PLACEHOLDER' "$P/paper/paper.tex"; then
                : # Good: properly escaped
            elif grep -q 'ABSTRACT.*PLACEHOLDER' "$P/paper/paper.tex"; then
                : # Acceptable: placeholder present (might work)
            else
                return 1  # No placeholder found
            fi
            ;;
        10)
            # Step 10: Gate 1 numerical check
            [[ -f "$P/code_review.md" ]] || return 1
            (( $(_lines "$P/code_review.md") >= 20 )) || return 1
            number_gate_passed "$P" "$BASE" || return 1
            symbol_gate_passed "$P" "$BASE" || return 1
            ;;
        13)
            # Step 13: Gate 2 judge evaluation must have VERDICT
            [[ -f "$P/judge_evaluation.md" ]] || return 1
            grep -q "^VERDICT:" "$P/judge_evaluation.md" || return 1
            ;;
        14)
            # Step 14: Abstract should replace placeholder
            [[ -f "$P/paper/paper.tex" ]] || return 1
            # Placeholder should be gone or filled
            if grep -q 'ABSTRACT.*PLACEHOLDER' "$P/paper/paper.tex"; then
                # If placeholder still exists, it's not done
                return 1
            fi
            ;;
        16)
            # Step 16: Final PDF must exist
            [[ -f "$P/${BASE}_paper.pdf" ]] || return 1
            # PDF should be substantial (> 50KB)
            local pdf_size
            pdf_size=$(stat -c %s "$P/${BASE}_paper.pdf" 2>/dev/null || echo 0)
            (( pdf_size > 51200 )) || return 1
            gate2_passed_for_path "$P" || return 1
            step8_5_passed_for_path "$P" || return 1
            delivery_quality_gate "$P" > "$P/delivery_quality_gate.json" 2> "$P/delivery_quality_gate.stderr.log" || return 1
            ;;
        *)
            # For other steps, rely on file-state inference
            return 0
            ;;
    esac

    return 0
}

# ── Error classification for intelligent retry ──────────────────────
#
# Classifies errors from step logs to distinguish transient (retry-worthy)
# from permanent (stop immediately) failures.
classify_step_error() {
    local log_file="$1"
    [[ -f "$log_file" ]] || { echo "UNKNOWN"; return; }

    # Check for retryable transient errors
    if grep -qiE "rate.?limit|429|quota.?exceeded|too.?many.?requests" "$log_file"; then
        echo "TRANSIENT_RATE_LIMIT"
        return
    fi

    if grep -qiE "timeout|timed.?out|connection.?timeout|504|502|503" "$log_file"; then
        echo "TRANSIENT_TIMEOUT"
        return
    fi

    if grep -qiE "temporary|temporarily.?unavailable|service.?unavailable" "$log_file"; then
        echo "TRANSIENT_UNAVAILABLE"
        return
    fi

    # Check for permanent errors
    if grep -qiE "authentication.?failed|invalid.?api.?key|401|403|permission.?denied" "$log_file"; then
        echo "PERMANENT_AUTH"
        return
    fi

    if grep -qiE "not.?found|404|file.?not.?found|no.?such.?file" "$log_file"; then
        echo "PERMANENT_NOT_FOUND"
        return
    fi

    if grep -qiE "invalid.?request|bad.?request|400|malformed" "$log_file"; then
        echo "PERMANENT_INVALID"
        return
    fi

    # Check for resource errors
    if grep -qiE "out.?of.?memory|oom.?killed|memory.?error|cannot.?allocate" "$log_file"; then
        echo "RESOURCE_MEMORY"
        return
    fi

    if grep -qiE "disk.?full|no.?space.?left|quota.?exceeded" "$log_file"; then
        echo "RESOURCE_DISK"
        return
    fi

    # Default: unknown (retry with caution)
    echo "UNKNOWN"
}

verify_step() {
    local new
    new=$(infer_step "$PROJECT" "$BASE")
    (( new >= $1 ))
}

# Gate 2 (Step 13 in modeling mode; was Step 11 in the legacy social-science
# pipeline).  Reads VERDICT line from judge_evaluation.md and, if it requests
# a reopen, drops a marker so the runner rewinds to Step 11 (which then re-runs
# the Step 12 revision) once. The .gate2_reopened_once file prevents an
# infinite reopen loop.
gate2_reopen_marker() {
    echo "$PROJECT/.gate2_reopen_to_revision"
}

gate2_reopened_once_file() {
    echo "$PROJECT/.gate2_reopened_once"
}

# Ablation stub for Step 13 (ABLATE_NO_JUDGE): write a minimal judge_evaluation.md
# that satisfies the Step-13 output contract (first line VERDICT:, >= 30 lines,
# written fresh) WITHOUT running the real judge or its reopen logic. VERDICT: PASS
# makes the main-loop gate2 branch advance straight to Step 14, and the structural
# precheck (scripts/evaluate_modeling_project.py) accepts it.
_write_judge_stub() {
    cat > "$PROJECT/judge_evaluation.md" <<EOF
VERDICT: PASS

# Step 13 Gate 2 — Judge Simulation (ABLATED) — \`$BASE\`

修订轮次: 1
整体得分: N/A (judge ablated via ABLATE_NO_JUDGE)

## 说明

本运行启用了 ABLATE_NO_JUDGE 消融开关: 跳过真实评委模拟与 reopen 逻辑。
本文件仅用于满足 Step 13 的结构契约 (VERDICT 行 + >= 30 行 + 新鲜度),
使流水线推进到 Step 14。它**不代表任何真实质量评判**。

真实质量评分由 experiments/ 外部的 evaluation/run_evaluation.sh 统一负责,
这正是本消融实验要测量的对照: "去掉 in-loop 评委后, 论文质量掉多少"。

## 6 维度评分 (消融, 留空)

下游 evaluate_modeling_project.py 只检查 VERDICT: PASS 与文件存在性, 不解析下表。

| 维度 | 权重 | 得分 | 评级 |
|---|---|---|---|
| 模型合理性 | 20% | - | - |
| 求解正确性 | 20% | - | - |
| 创新性 | 20% | - | - |
| 写作清晰度 | 15% | - | - |
| 结果说服力 | 15% | - | - |
| 灵敏度分析 | 10% | - | - |

## 备注

- ablation = ABLATE_NO_JUDGE
- 不触发 REOPEN_REVISION_TEXT / REOPEN_REVISION_MODEL
- 不执行 Gate-2 reopen → Step 12 第二轮修订 (该效应已并入本消融的对照差异)
EOF
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

block_gate2_after_reopen() {
    local verdict="$1"
    log "   Step 13 verdict $verdict after prior reopen — blocking delivery"
    echo "BLOCKED_STEP13:$verdict $(date +%s)" > "$PROJECT/.heartbeat"
    touch "$PROJECT/.gate2_blocked"
    diag_event "$PROJECT" 13 gate_blocked GATE2_REOPEN_UNRESOLVED \
        "Gate 2 still requests reopen after the allowed revision cycle" "judge_evaluation.md"
    diag_status "$PROJECT" blocked 13 gate2_unresolved GATE2_REOPEN_UNRESOLVED \
        "Gate 2 未通过，项目不能进入摘要和交付" \
        "open_judge_evaluation,open_revision_summary,refresh_status" \
        "file:judge_evaluation.md,file:revision_summary.md"
}

# ── Determine starting point ─────────────────────────────────────────

INFERRED=$(infer_step "$PROJECT" "$BASE")
FROM_CP=$(checkpoint_step)
STEP=$INFERRED

QUESTION=$(get_question)

log "========================================"
log "Runner starting"
log "  file-state: step $INFERRED | checkpoint: step $FROM_CP | resuming from: step $STEP"
diag_status "$PROJECT" running "$STEP" bootstrap runner_start "" "" ""
runner_mark_running "$PROJECT" "$STEP" bootstrap
{
    _active_ablations=""
    _ablate_on "$ABLATE_NO_CONSULTATION"       && _active_ablations+=" no-consultation"
    _ablate_on "$ABLATE_NO_METHOD_LIB"         && _active_ablations+=" no-method-lib"
    _ablate_on "$ABLATE_NO_JUDGE"              && _active_ablations+=" no-judge"
    _ablate_on "$ABLATE_NO_INNOVATION_PROTECT" && _active_ablations+=" no-innovation-protect"
    [[ -n "$_active_ablations" ]] && log "  ABLATIONS ACTIVE:$_active_ablations"
}

if [[ -z "$QUESTION" ]]; then
    log "ERROR: no research question in checkpoint.md"
    exit 1
fi
log "  question: ${QUESTION:0:120}..."

# File-state is authoritative; correct checkpoint drift in either direction.
if (( INFERRED != FROM_CP )); then
    log "  checkpoint disagrees with file-state ($FROM_CP vs $INFERRED) — correcting"
    _set_checkpoint_step "$INFERRED"
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
        _set_checkpoint_step 0
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

        if ! $alive; then
            diag_status "$proj_dir" waiting "$step" activity_monitor NO_LOG_PROGRESS \
                "日志暂未增长，runner 仍在等待新活动信号" \
                "open_runner_log,refresh_status" "file:logs/runner.log"
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
    # Only when this project opted into the consultation window AND the dynamic
    # gate is active: tell the agent how to ask a human for help instead of
    # guessing on a hard, load-bearing decision. Omitted entirely otherwise so
    # unattended runs see a byte-identical preamble.
    if consult_gate_active dynamic; then
        cat <<EOF
如果在本步遇到难以独立判断、且会显著影响后续的关键决策（例如建模路线分叉、关键假设取舍、与题意冲突的数据解释），不要臆断：把该决策清楚地写入项目目录下的 \`consultation/REQUEST.md\`（首行 \`CONSULT: <一句话问题>\`，其后补充背景、你已尝试的选项与各自利弊），并就此停手、不要产出本步的最终交付物。人工会借助前沿模型回填结论，流水线随后带着回填重跑本步。仅在真正卡住时使用；常规不确定性请按既有纪律自行处理。

EOF
    fi
}

render_prompt() {
    local template="$1"
    local extra="${2:-}"
    # Escape & in QUESTION so sed doesn't treat it as "insert match"
    local q_escaped="${QUESTION//&/\\&}"
    # Build sed args as an array. (Historically $extra was spliced in unquoted,
    # which would word-split any space-containing arg — no caller uses it, but
    # the array form is correct and lets the ablations below add space-bearing
    # Chinese patterns safely.)
    local -a sed_args=(
        -e "s|__PROJECT_PATH__|$PROJECT|g"
        -e "s|__RESEARCH_QUESTION__|$q_escaped|g"
        -e "s|__BASE_NAME__|$BASE|g"
        -e "s|__FACTORY__|$FACTORY|g"
    )
    # Ablation: external web consultation. Only Step 1 carries the web-search
    # instruction; drop just the web clause, leaving the rest of the line.
    if _ablate_on "$ABLATE_NO_CONSULTATION" && [[ "$template" == step1_research_viability.txt ]]; then
        sed_args+=( -e 's#；用 web 检索拿到主文献##' )
    fi
    # Ablation: innovation protection. Delete lines where a PROTECTED label
    # co-occurs with a non-downgrade enforcement verb, freeing downstream
    # review/revision to alter those assumptions. The PROTECTED label-creation
    # table in step4 is left intact (no enforcement verb on those rows).
    if _ablate_on "$ABLATE_NO_INNOVATION_PROTECT"; then
        case "$template" in
            step4_model_construction.txt|step6_sensitivity.txt|step7_model_eval.txt|\
            step10_gate1_numerical.txt|step11_constructive_review.txt|\
            step12_revision.txt|step13_gate2_judge.txt|step14_abstract.txt)
                sed_args+=( -e '/PROTECTED/{/不可降级\|不得删除\|删 PROTECTED\|永不动\|绝不动\|永远不动/d}' )
                ;;
        esac
    fi
    [[ -n "$extra" ]] && sed_args+=( $extra )  # back-compat slot (currently unused)
    common_prompt_preamble
    sed "${sed_args[@]}" "$PROMPTS/$template"
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

# ── Human consultation window (opt-in) ───────────────────────────────
#
# Lets a human inject GPT Pro / Gemini Deep Think conclusions into the
# otherwise-autonomous pipeline at chosen points (pre-flight seed, before
# Step 4 modeling, or any step where an agent asks for help).
#
# Design is dictated by the activity monitor: a blocking wait would both hold
# the lock and look like a hang (the known hang-bug family).  So a consultation
# pause writes a request, notifies, and EXITS CLEANLY (exit 0) — the EXIT trap
# releases the lock, exactly like `.paused`.  The human pastes the answer into
# human_review.md (read at highest priority by every agent via the prompt
# preamble), flips STATUS to READY, and `resume`s; on the next run the gate
# sees READY and proceeds.
#
# OFF by default: gates are no-ops unless the project opts in via
# `consultation/enabled` (or CONSULT_ENABLE=1).  This keeps unattended
# benchmark/ablation runs byte-identical and stops them from stalling.
CONSULT_DIR="$PROJECT/consultation"
CONSULT_AWAIT_MARKER="$PROJECT/.awaiting_consultation"
HUMAN_REVIEW="$PROJECT/human_review.md"

consult_enabled() {
    [[ "${CONSULT_ENABLE:-0}" == 1 ]] && return 0
    [[ -f "$CONSULT_DIR/enabled" ]]
}

# A gate is active if consultation is enabled AND (the enabled file lists no
# gates → all gates on, OR it lists this gate).  Tokens are space/comma/newline
# separated, e.g.  preflight step4  or  preflight,dynamic.
consult_gate_active() {
    local gate="$1"
    consult_enabled || return 1
    local f="$CONSULT_DIR/enabled"
    [[ -f "$f" ]] || return 0            # CONSULT_ENABLE=1, no file → all gates
    local body
    body=$(tr ',\n\t' '   ' < "$f" 2>/dev/null || true)
    [[ -z "${body// /}" ]] && return 0   # empty file → all gates
    grep -qiw "$gate" <<<"$body"
}

# Has the human marked this gate READY in human_review.md?
# Matches a heading like:  ## CONSULT <gate> (Step N) — STATUS: READY
consult_ready() {
    local gate="$1"
    [[ -f "$HUMAN_REVIEW" ]] || return 1
    grep -qiE "^##[[:space:]]+CONSULT[[:space:]]+${gate}([[:space:](].*)?STATUS:[[:space:]]*READY" \
        "$HUMAN_REVIEW"
}

consult_section_exists() {
    local gate="$1"
    [[ -f "$HUMAN_REVIEW" ]] || return 1
    grep -qiE "^##[[:space:]]+CONSULT[[:space:]]+${gate}\b" "$HUMAN_REVIEW"
}

# Best-effort Telegram push.  DISABLED by default — set CONSULT_TELEGRAM=1 to
# enable.  Reads token/chat from env or the claude-to-im config; silently skips
# if unavailable.  The terminal log (via log()) always happens regardless.
notify_consult_telegram() {
    local text="$1"
    [[ "${CONSULT_TELEGRAM:-0}" == 1 ]] || return 0
    local tok="${CTI_TG_BOT_TOKEN:-}" chat="${CTI_TG_CHAT_ID:-}"
    if [[ -z "$tok" || -z "$chat" ]]; then
        local cfg="$HOME/.config/claude-to-im/config.env"
        if [[ -f "$cfg" ]]; then
            tok=$(grep -E '^CTI_TG_BOT_TOKEN=' "$cfg" 2>/dev/null | head -1 | cut -d= -f2- || true)
            chat=$(grep -E '^CTI_TG_CHAT_ID=' "$cfg" 2>/dev/null | head -1 | cut -d= -f2- || true)
        fi
    fi
    [[ -n "$tok" && -n "$chat" ]] || return 0
    curl -s --max-time 15 "https://api.telegram.org/bot${tok}/sendMessage" \
        --data-urlencode "chat_id=${chat}" \
        --data-urlencode "text=${text}" >/dev/null 2>&1 || true
}

# Append a fill-in section to human_review.md for the human to complete.
_consult_seed_section() {
    local gate="$1" step="$2" title="$3"
    consult_section_exists "$gate" && return 0
    touch "$HUMAN_REVIEW"
    cat >> "$HUMAN_REVIEW" <<EOF

## CONSULT ${gate} (Step ${step}) — STATUS: AWAITING
<!--
咨询点：${title}
请用 GPT Pro / Gemini Deep Think 处理 consultation/${gate}_request.md 里的问题，
把结论粘到下面「你的回填」标题之下，然后把上面这行的 STATUS: AWAITING 改成 STATUS: READY，
再运行：  ./launch_agents.sh resume ${BASE}
流水线会带着你的回填重新接管。STATUS 未改成 READY 之前不会继续。
-->

### 你的回填（${gate}）：

EOF
}

# Remove a `## CONSULT <gate> …` section (heading through the line before the
# next `## ` heading or EOF) from human_review.md.  Used to reset the repeatable
# `dynamic` gate after its answer has been consumed, so a later dynamic request
# re-pauses instead of seeing a stale STATUS: READY.
_consult_drop_section() {
    local gate="$1"
    [[ -f "$HUMAN_REVIEW" ]] || return 0
    awk -v g="$gate" '
        tolower($0) ~ ("^##[ \t]+consult[ \t]+" tolower(g) "([ \t(]|$)") { drop=1; next }
        drop==1 && /^##[ \t]/ { drop=0 }
        drop==1 { next }
        { print }
    ' "$HUMAN_REVIEW" > "$HUMAN_REVIEW.tmp" 2>/dev/null \
        && mv "$HUMAN_REVIEW.tmp" "$HUMAN_REVIEW" || rm -f "$HUMAN_REVIEW.tmp"
}

# Core gate.  Returns 0 to proceed; otherwise writes a request, notifies, and
# exits the runner cleanly so the human can fill in the answer and resume.
#   $1 gate id   $2 step   $3 human-readable title   $4 optional context file
maybe_consult() {
    local gate="$1" step="$2" title="$3" ctx_file="${4:-}"
    consult_gate_active "$gate" || return 0

    if consult_ready "$gate"; then
        log "   CONSULT[$gate]: human marked READY — proceeding (answer in human_review.md)"
        rm -f "$CONSULT_AWAIT_MARKER" 2>/dev/null || true
        return 0
    fi

    mkdir -p "$CONSULT_DIR"
    local req="$CONSULT_DIR/${gate}_request.md"
    if [[ ! -f "$req" ]]; then
        {
            echo "# 咨询请求：${title}"
            echo
            echo "**元数据**"
            echo "- gate: ${gate}"
            echo "- step: ${step}"
            echo "- project: ${BASE}"
            echo "- created: $(date '+%Y-%m-%d %H:%M:%S')"
            echo

            # ── 项目背景上下文 ──
            echo "## 📋 项目背景"
            echo
            if [[ -f "$PROJECT/problem/problem_brief.md" ]]; then
                echo "### 问题概要"
                echo '```'
                head -30 "$PROJECT/problem/problem_brief.md" 2>/dev/null || echo "(无法读取)"
                echo '```'
                echo
            fi

            local current_step_name=""
            case "$step" in
                0) current_step_name="问题解析与候选方法识别" ;;
                1) current_step_name="背景研究与方法可行性" ;;
                2) current_step_name="并行建模提案与demo验证" ;;
                3) current_step_name="方法选择" ;;
                4) current_step_name="完整模型构建" ;;
                5) current_step_name="模型求解" ;;
                6-15) current_step_name="论文写作与审核（Step $step）" ;;
            esac
            echo "**当前所在步骤**: Step ${step} — ${current_step_name}"
            echo

            # ── 决策影响分析 ──
            echo "## 🎯 决策影响"
            echo
            case "$gate" in
                preflight)
                    echo "这是**启动前咨询**，你的决策将影响："
                    echo "- Step 1 的背景研究方向（哪些领域、哪些前沿方法值得深入）"
                    echo "- Step 2 的建模提案范围（探索哪些技术路线）"
                    echo "- 整个项目的技术栈选择（优化器、求解器、工具链）"
                    echo
                    echo "**建议关注点**："
                    echo "- 问题的本质是什么？（优化/预测/分类/仿真/博弈…）"
                    echo "- 哪些前沿方法/论文值得参考？"
                    echo "- 有无特殊约束需要在建模初期就考虑？"
                    ;;
                step4)
                    echo "这是**建模定型前咨询**，你的决策将影响："
                    echo "- Step 4 最终的模型公式化"
                    echo "- Step 5 的求解方案（算法、求解器选择）"
                    echo "- Step 6-7 的敏感性分析和鲁棒性验证策略"
                    echo
                    echo "**建议关注点**："
                    echo "- 模型假设是否合理？有无遗漏的关键约束？"
                    echo "- 是否有更优雅/高效的数学表达？"
                    echo "- 求解难度评估：凸优化？NP-hard？可近似？"
                    ;;
                dynamic)
                    echo "这是**agent 主动求助**，说明遇到了难以自动决策的问题。"
                    echo "你的决策将直接影响当前 Step ${step} 能否顺利推进。"
                    echo
                    ;;
            esac
            echo

            # ── 关键文件引用 ──
            echo "## 📁 关键文件引用"
            echo
            echo "以下是项目中已完成的关键文件，供你参考决策："
            echo

            local has_any_file=0
            [[ -f "$PROJECT/problem/problem_brief.md" ]] && {
                echo "- \`problem/problem_brief.md\` — 问题解析"
                has_any_file=1
            }
            [[ -f "$PROJECT/problem/constraints.md" ]] && {
                echo "- \`problem/constraints.md\` — 约束条件分析"
                has_any_file=1
            }
            [[ -f "$PROJECT/problem/data_inventory.md" ]] && {
                echo "- \`problem/data_inventory.md\` — 数据清单"
                has_any_file=1
            }
            [[ -f "$PROJECT/viable_streams.md" ]] && {
                echo "- \`viable_streams.md\` — 可行方法流（Step 1 产出）"
                has_any_file=1
            }
            for idx in {1..5}; do
                [[ -f "$PROJECT/m${idx}_spec.md" ]] && {
                    echo "- \`m${idx}_spec.md\` — 建模提案 ${idx}"
                    has_any_file=1
                }
                [[ -f "$PROJECT/m${idx}_critique.md" ]] && {
                    echo "- \`m${idx}_critique.md\` — 提案 ${idx} 评审"
                    has_any_file=1
                }
            done
            [[ -f "$PROJECT/method_decision.md" ]] && {
                echo "- \`method_decision.md\` — 方法决策记录（Step 3 产出）"
                has_any_file=1
            }
            [[ -f "$PROJECT/model.md" ]] && {
                echo "- \`model.md\` — 完整模型描述"
                has_any_file=1
            }
            [[ -f "$PROJECT/symbol_table.md" ]] && {
                echo "- \`symbol_table.md\` — 符号表"
                has_any_file=1
            }
            [[ -f "$PROJECT/assumption_ledger.md" ]] && {
                echo "- \`assumption_ledger.md\` — 假设清单"
                has_any_file=1
            }

            if (( has_any_file == 0 )); then
                echo "（项目刚启动，暂无已完成文件）"
            fi
            echo

            # ── 核心咨询内容 ──
            echo "## 🤔 需要你（借助 GPT Pro / Gemini Deep Think）决定的事"
            echo
            if [[ -n "$ctx_file" && -f "$ctx_file" ]]; then
                cat "$ctx_file"
            else
                echo "${title}"
            fi
            echo

            # ── 回答指引 ──
            echo "## 💡 回答建议"
            echo
            echo "请提供："
            echo "1. **明确的决策建议**（选哪个方案 / 采用什么策略）"
            echo "2. **充分的理由**（为什么这样选？有何优势？）"
            echo "3. **潜在风险提示**（这个选择可能遇到的坑）"
            echo "4. **实施要点**（如果有具体的技术细节、参数建议、论文引用等）"
            echo
            echo "你可以用 GPT Pro 的深度思考模式、或 Gemini 的 Deep Think 功能，来进行多角度分析。"
            echo

            # ── 回填方式 ──
            echo "## 🔄 回填方式"
            echo
            echo "1. 把你的决策结论写进 \`human_review.md\` 的「## CONSULT ${gate} … STATUS: …」段落下；"
            echo "2. 把该段落的 \`STATUS: AWAITING\` 改成 \`STATUS: READY\`；"
            echo "3. 运行：\`./launch_agents.sh resume ${BASE}\`"
            echo
            echo "流水线会读取你的回填内容，并在后续步骤中优先参考。"
        } > "$req"
    fi

    _consult_seed_section "$gate" "$step" "$title"

    echo "GATE:${gate} STEP:${step} TS:$(date +%s)" > "$CONSULT_AWAIT_MARKER"
    echo "CONSULT:${step} $(date +%s)" > "$PROJECT/.heartbeat"
    diag_event "$PROJECT" "$step" consultation_requested CONSULTATION_PENDING \
        "Runner paused for human consultation" "consultation/${gate}_request.md"
    diag_status "$PROJECT" waiting "$step" consultation_wait CONSULTATION_PENDING \
        "等待人工咨询回填" \
        "open_consultation_request,open_human_review,refresh_status" \
        "file:consultation/${gate}_request.md,file:human_review.md"
    runner_mark_consultation "$PROJECT" "$step" "$gate"

    local msg="🛑 [paper_factory] ${BASE} 在 Step ${step} 暂停，等待人工咨询：${title}
请阅读 ${req}
填 human_review.md 的 §CONSULT ${gate}（STATUS 改 READY），然后：./launch_agents.sh resume ${BASE}"
    log "   CONSULT[$gate]: pausing for human input — see $req"
    log "   等待人工咨询：$title"
    notify_consult_telegram "$msg"

    remove_registry_entry "$BASE"
    log "   Runner exiting cleanly for consultation. Resume with: ./launch_agents.sh resume $BASE"
    exit 0
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
# Shared hang check for run_codex / run_agy (this block was byte-identical in
# both functions). Returns 1 when the process is NOT (yet) considered hung —
# either stale_since hasn't exceeded hang_timeout, or it has but the process
# still has direct children (treated as waiting on a long job; stale_since is
# deliberately NOT reset, so a second quiet window will fire the kill). Returns
# 0 after killing+reaping a process judged hung (stale + no children).
# NOTE: this uses a simple `pgrep -P` child count, NOT the full descendant-tree
# walk run_codex_parallel uses. Per that function's comment, the
# subshell→timeout→codex tree means a simple count almost always finds the
# `timeout` child, so this heuristic rarely fires a kill. Behaviour is preserved
# verbatim here; reconciling the two kill heuristics is a separate behavioural
# change, intentionally NOT part of this DRY extraction.
_maybe_kill_hung() {
    local pid="$1" stale_since="$2" hang_timeout="$3" label="$4"
    local now
    now=$(date +%s)
    (( now - stale_since > hang_timeout )) || return 1
    local children
    children=$(pgrep -P "$pid" 2>/dev/null | wc -l)
    if (( children > 0 )); then
        log "   $label stale ${hang_timeout}s but has $children child processes — not hung"
        return 1
    fi
    log "   $label stale for ${hang_timeout}s — killing (hung)"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    return 0
}

# Usage: run_codex <prompt_file> <timeout_secs> [hang_timeout_secs]
# Returns: 0=success, 1=timeout/error, 2=hung(killed)
run_codex() {
    local prompt_file="$1" timeout="$2" hang_timeout="${3:-3600}"
    # Optional model override (model registry / per-step dispatch).  Defaults
    # preserve the historical hardcoded behavior for every existing caller.
    local cx_model="${4:-gpt-5.5}" cx_effort="${5:-xhigh}"
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

    log "   Codex: $prompt_file (model=$cx_model effort=$cx_effort, timeout ${timeout}s)"

    ( cd "$PROJECT" && timeout --kill-after=120 "$timeout" \
        codex exec \
          --model "$cx_model" \
          -c "model_reasoning_effort=\"$cx_effort\"" \
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

        if _maybe_kill_hung "$codex_pid" "$stale_since" "$hang_timeout" "Codex trace"; then
            return 2
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
    # via ListModels 2026-05-25.  A 4th positional arg (from the model
    # registry / per-step dispatch) takes precedence over AGY_MODEL.
    local agy_model="${4:-${AGY_MODEL:-gemini-3.1-pro-preview}}"

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

        if _maybe_kill_hung "$agy_pid" "$stale_since" "$hang_timeout" "Agy log"; then
            return 2
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
    local prompt_src="$1" timeout="$2" is_literal="${3:-}" cl_model="${4:-}"
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
    log "   Claude worker: ${prompt_src:0:60}${cl_model:+ (model=$cl_model)}"

    # Optional --model override (model registry / per-step dispatch).  Empty =>
    # the claude CLI's own default model, identical to historical behavior.
    local cl_model_flag=()
    [[ -n "$cl_model" ]] && cl_model_flag=(--model "$cl_model")

    set +e
    ( cd "$PROJECT" && stdbuf -oL -eL timeout --kill-after=120 "$timeout" \
        claude -p "$prompt" --dangerously-skip-permissions --effort max "${cl_model_flag[@]}" \
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

# ── Model registry & per-step model dispatch ─────────────────────────
#
# Optional, opt-in layer that lets the web dashboard pick which model runs
# each step.  Mirrors the get_user_note()/web/notes.json precedent:
#
#   web/model_registry.json  — global catalog of selectable models.  Each entry:
#       { "id", "label", "backend": claude|codex|agy|openai|gemini,
#         "model", "effort", "base_url", "key_env", "enabled" }
#   web/model_config.json    — per-step assignment, project overrides default:
#       { "_default": { "step_13": {"primary":"<id>","fallback":"<id>"} },
#         "<base>":   { "step_5":  {"primary":"<id>"} } }
#
# When a step has NO assignment, dispatch_step calls the step's built-in
# hardcoded combinator unchanged, so unattended benchmark / ablation runs stay
# byte-identical (no files => no behavior change).
MODEL_REGISTRY_FILE="$FACTORY/web/model_registry.json"
MODEL_CONFIG_FILE="$FACTORY/web/model_config.json"

# Echo "primary_id|fallback_id" for a step ("" if no assignment).  Project
# ($BASE) assignment wins over the "_default" preset.
get_step_model_ids() {
    local step="$1"
    [[ -f "$MODEL_CONFIG_FILE" ]] || return 0
    python3 - "$MODEL_CONFIG_FILE" "$BASE" "$step" <<'PY' 2>/dev/null || true
import json, sys
cfg_file, base, step = sys.argv[1], sys.argv[2], sys.argv[3]
key = "step_%s" % step
try:
    with open(cfg_file) as f:
        cfg = json.load(f)
except Exception:
    sys.exit(0)
def get(scope):
    e = (cfg.get(scope) or {}).get(key)
    if isinstance(e, dict):
        return e
    if isinstance(e, str) and e:
        return {"primary": e}
    return None
entry = get(base) or get("_default")
if not entry:
    sys.exit(0)
prim = (entry.get("primary") or "").strip()
fb = (entry.get("fallback") or "").strip()
if not prim:
    sys.exit(0)
print("%s|%s" % (prim, fb))
PY
}

# Echo "backend<US>model<US>effort<US>base_url<US>key_env" (US = \x1f) for a
# registry id, or return 1 if the id is missing / disabled.  A non-whitespace
# separator is required: `read` with IFS=tab collapses empty middle fields.
get_model_entry() {
    local id="$1"
    [[ -n "$id" && -f "$MODEL_REGISTRY_FILE" ]] || return 1
    python3 - "$MODEL_REGISTRY_FILE" "$id" <<'PY' 2>/dev/null || return 1
import json, sys
reg_file, mid = sys.argv[1], sys.argv[2]
try:
    with open(reg_file) as f:
        reg = json.load(f)
except Exception:
    sys.exit(1)
models = reg.get("models", []) if isinstance(reg, dict) else reg
for m in models:
    if m.get("id") == mid:
        if m.get("enabled") is False:
            sys.exit(1)
        print("\x1f".join([
            m.get("backend", ""), m.get("model", ""), m.get("effort", ""),
            m.get("base_url", ""), m.get("key_env", ""),
        ]))
        sys.exit(0)
sys.exit(1)
PY
}

# Step -> the single markdown artifact a non-agentic API backend should write.
# Only the judge / review / evaluation steps have a clean single-file output;
# other steps need an agentic backend and return "" (API backend is skipped).
api_step_output_file() {
    case "$1" in
        7)  echo "evaluation.md" ;;
        11) echo "review_comments.md" ;;
        13) echo "judge_evaluation.md" ;;
        *)  echo "" ;;
    esac
}

# Step -> project files inlined as context for a non-agentic API backend
# (it cannot open files itself).  Missing files are skipped by api_agent_run.py.
api_step_context_files() {
    local step="$1"
    local common="problem/problem_brief.md model.md symbol_table.md assumption_ledger.md"
    case "$step" in
        7)  echo "$common solve_log.md sensitivity_report.md" ;;
        11) echo "$common solve_log.md sensitivity_report.md evaluation.md ${BASE}_paper.tex paper/paper.tex" ;;
        13) echo "$common solve_log.md sensitivity_report.md evaluation.md review_comments.md revision_summary.md ${BASE}_paper.tex paper/paper.tex" ;;
        *)  echo "$common" ;;
    esac
}

# Run a non-agentic API model for the current step (NEXT) via api_agent_run.py.
# Args: <prompt_file> <timeout> <model> <backend> <base_url> <key_env>
run_api_model() {
    local prompt_file="$1" timeout="$2" model="$3" backend="$4" base_url="$5" key_env="$6"
    local out_rel
    out_rel="$(api_step_output_file "$NEXT")"
    if [[ -z "$out_rel" ]]; then
        log "   API model: step $NEXT has no single-file output — API backend not applicable"
        return 1
    fi

    local rendered note
    rendered=$(render_prompt "$prompt_file")
    note=$(get_user_note "$NEXT")
    [[ -n "$note" ]] && rendered="$rendered

NOTE FROM THE RESEARCHER: $note"

    local stamp; stamp=$(date +%Y%m%d_%H%M%S)
    local prompt_tmp="$PROJECT/logs/step_${NEXT}_api_${model//\//_}_${stamp}.prompt.txt"
    local api_log="$PROJECT/logs/step_${NEXT}_api_${model//\//_}_${stamp}.log"
    printf '%s' "$rendered" > "$prompt_tmp"

    local ctx_args=() f
    for f in $(api_step_context_files "$NEXT"); do
        ctx_args+=(--context-file "$f")
    done

    local api_args=(--model "$model" --backend "$backend")
    [[ -n "$base_url" ]] && api_args+=(--base-url "$base_url")
    [[ -n "$key_env" ]] && api_args+=(--key-env "$key_env")

    local inner=$(( timeout - 30 )); (( inner < 60 )) && inner=$timeout
    log "   API model: $model [$backend] -> $out_rel (timeout ${timeout}s)"
    ( cd "$PROJECT" && timeout --kill-after=60 "$timeout" \
        python3 "$FACTORY/scripts/api_agent_run.py" \
            "${api_args[@]}" \
            --prompt-file "$prompt_tmp" \
            --project "$PROJECT" \
            --output-file "$out_rel" \
            "${ctx_args[@]}" \
            --timeout "$inner" \
    ) > "$api_log" 2>&1
    local ec=$?
    if (( ec == 0 )); then
        log "   API model $model wrote $out_rel"
        return 0
    fi
    log "   API model $model failed (exit $ec) — see ${api_log##*/}"
    return 1
}

# Run ONE registry model id for the current step's prompt, routing to the
# right backend primitive.  Returns 0 on success.
# Args: <model_id> <prompt_file> <timeout> <hang_timeout>
run_backend() {
    local mid="$1" prompt_file="$2" timeout="$3" hang="${4:-3600}"
    local entry backend model effort base_url key_env
    if ! entry="$(get_model_entry "$mid")"; then
        log "   model '$mid' not found in registry (or disabled)"
        return 1
    fi
    IFS=$'\x1f' read -r backend model effort base_url key_env <<<"$entry"
    case "$backend" in
        claude)
            run_claude_worker "$prompt_file" "$timeout" "" "$model" ;;
        codex)
            run_codex "$prompt_file" "$timeout" "$hang" "${model:-gpt-5.5}" "${effort:-xhigh}" ;;
        agy)
            run_agy "$prompt_file" "$timeout" "$hang" "$model" ;;
        openai|deepseek|gemini)
            run_api_model "$prompt_file" "$timeout" "$model" "$backend" "$base_url" "$key_env" ;;
        *)
            log "   unknown backend '$backend' for model '$mid'"; return 1 ;;
    esac
}

# Generic per-step dispatch.  If the step (NEXT) has a model assignment, run the
# configured primary then fallback; otherwise call the step's built-in default
# combinator with identical args (no behavior change).  default_fn must accept
# (prompt_file, timeout, hang_timeout).
# Args: <prompt_file> <timeout> <hang_timeout> <default_fn>
dispatch_step() {
    local prompt_file="$1" timeout="$2" hang="$3" default_fn="$4"
    local step_key="${5:-$NEXT}"
    local ids primary fallback
    ids="$(get_step_model_ids "$step_key")"
    if [[ -z "$ids" ]]; then
        "$default_fn" "$prompt_file" "$timeout" "$hang"
        return $?
    fi
    primary="${ids%%|*}"
    fallback="${ids#*|}"
    [[ "$fallback" == "$ids" ]] && fallback=""
    log "   Step $NEXT: model override — primary='$primary' fallback='${fallback:-<none>}'"

    if run_backend "$primary" "$prompt_file" "$timeout" "$hang"; then
        return 0
    fi
    if verify_step "$NEXT"; then
        log "   Step $NEXT: primary exited nonzero but artifacts verify — done"
        return 0
    fi
    if [[ -n "$fallback" && "$fallback" != "$primary" ]]; then
        log "   Step $NEXT: primary failed — trying configured fallback '$fallback'"
        if run_backend "$fallback" "$prompt_file" "$timeout" "$hang"; then
            return 0
        fi
        if verify_step "$NEXT"; then return 0; fi
    fi
    log "   Step $NEXT: configured model(s) failed — falling back to built-in default"
    "$default_fn" "$prompt_file" "$timeout" "$hang"
}

# Wrapper so single-Claude-worker steps (1, 3) can be a dispatch default_fn:
# run_claude_worker's 3rd arg is is_literal, NOT hang, so it must be elided.
_default_claude_worker() { run_claude_worker "$1" "$2"; }

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
        dispatch_step step1_research_viability.txt 14400 3600 _default_claude_worker || true
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

    # ── Step 2 资源配额优化 ──────────────────────────────────────────
    # 根据问题复杂度动态裁剪流数量，避免简单问题过度并行
    local quota_result
    if quota_result=$(python3 "$FACTORY/scripts/step2_resource_quota.py" "$PROJECT" 2>/dev/null); then
        local recommended
        recommended=$(echo "$quota_result" | python3 -c "import sys,json; print(json.load(sys.stdin)['recommended_streams'])" 2>/dev/null || echo "")
        if [[ -n "$recommended" && "$recommended" =~ ^[0-9]+$ ]]; then
            local original_count=${#active_ids[@]}
            if (( recommended < original_count )); then
                log "   Step 2: quota advisor recommends $recommended streams (original: $original_count)"
                # 保留前N个流（Step 1按优先级排序）
                active_ids=("${active_ids[@]:0:$recommended}")
            fi
        fi
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

        # ── Step 2 早停检测 ──────────────────────────────────────────
        # 在proposal阶段，每个监控周期检查demo是否快速失败，提前终止
        for idx in "${active_ids[@]}"; do
            local phase="${stream_phases[$idx]}"
            [[ "$phase" != "proposal" ]] && continue
            local pid="${stream_pids[$idx]}"
            [[ -z "$pid" ]] && continue
            kill -0 "$pid" 2>/dev/null || continue  # 已经退出的跳过

            # 调用早停检测器
            local early_result
            if early_result=$(python3 "$FACTORY/scripts/step2_early_stop.py" "$PROJECT" "$idx" 2>/dev/null); then
                local should_stop
                should_stop=$(echo "$early_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('should_stop', False))" 2>/dev/null || echo "False")
                if [[ "$should_stop" == "True" ]]; then
                    local reason
                    reason=$(echo "$early_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason', 'unknown'))" 2>/dev/null || echo "unknown")
                    log "   Step 2: stream $idx early-stop triggered (reason: $reason) — killing proposal agent"
                    _kill_process_tree "$pid" TERM
                    sleep 2
                    kill -0 "$pid" 2>/dev/null && _kill_process_tree "$pid" KILL
                    stream_pids[$idx]=""
                    stream_phases[$idx]="done"
                    # 在critique文件中记录早停
                    local critique_path
                    critique_path=$(step2_critique_path "$PROJECT" "$idx")
                    echo -e "\n## Early Stop\n\nVERDICT: ABANDONED\n\nReason: Demo solve failed early with $reason. Stream terminated to save resources.\n" >> "$critique_path"
                fi
            fi
        done

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
    dispatch_step step3_method_selection.txt 7200 1800 _default_claude_worker || true

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
    # Modeling-mode Step 4: full model construction.  Changed to Claude primary
    # (Codex fallback) to avoid Agy. Reads chosen_method.md to identify PRIMARY
    # (and optional AUXILIARY), expands m{primary}_spec.md into the canonical
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

    log "   Step 4: full model construction (Claude → Codex fallback, Agy disabled)"
    dispatch_step step4_model_construction.txt 14400 3600 run_claude_then_codex || true

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
    dispatch_step step5_full_solve.txt 14400 3600 run_codex_then_claude
}
run_step_6() {
    # Step 6 预检查: 确保 Step 5 产物完整,避免进入重试循环后才发现基础数据缺失
    log "   Running Step 6 coverage precheck"
    if [[ -x "$FACTORY/scripts/step6_coverage_precheck.py" ]]; then
        if "$FACTORY/scripts/step6_coverage_precheck.py" "$PROJECT" >> "$PROJECT/logs/runner.log" 2>&1; then
            log "   Step 6 precheck PASS"
        else
            local exit_code=$?
            if (( exit_code == 2 )); then
                log "   Step 6 precheck BLOCKED — missing Step 5 outputs, aborting"
                return 1
            else
                log "   Step 6 precheck WARNING — continuing with caution"
            fi
        fi
    else
        log "   WARNING: step6_coverage_precheck.py not found, skipping precheck"
    fi

    dispatch_step step6_sensitivity.txt 10800 3600 run_codex_then_claude
}

run_step_7()  { dispatch_step step7_model_eval.txt 7200 1800 run_claude_then_codex; }
run_step_8()  { dispatch_step step8_visualization.txt 10800 3600 run_claude_then_codex; }

# Step 8.5 is implemented as a pre-Step-9 editorial gate.
# We do not renumber the integer main loop. Instead, Step 9 refuses to draft
# the paper until reviewer_entry_map.md + anchor_figure_plan.md + entry_gate.md
# exist and entry_gate.md says VERDICT: PASS.
step8_5_verdict() {
    python3 "$FACTORY/scripts/step8_5_gate.py" "$PROJECT" --verdict 2>/dev/null || true
}

step8_5_passed() {
    [[ "$(step8_5_verdict)" == "PASS" ]]
}

run_step_8_5() {
    dispatch_step step8_5_reviewer_entry.txt 7200 1800 run_claude_then_codex 8_5
}

run_step_9() {
    if ! step8_5_passed; then
        log "   Step 9 preflight: running Step 8.5 Reviewer Entry Design"
        run_step_8_5 || return $?
        local verdict
        verdict="$(step8_5_verdict)"
        if [[ "$verdict" != "PASS" ]]; then
            log "   Step 8.5 verdict ${verdict:-<missing>} — stop before paper draft"
            diag_event "$PROJECT" 8 gate_blocked AWAITING_STEP8_5 \
                "Step 8.5 verdict is not PASS" "entry_gate.md"
            diag_status "$PROJECT" waiting 8 step8_5_gate_review AWAITING_STEP8_5 \
                "Step 8.5 未通过，等待补足 reviewer entry 材料" \
                "open_entry_gate,open_reviewer_entry_artifacts,refresh_status" \
                "file:entry_gate.md,file:reviewer_entry_map.md,file:anchor_figure_plan.md"
            return 42
        fi
    fi
    dispatch_step step9_paper_draft.txt 14400 3600 run_claude_then_codex
}

run_step_10() { dispatch_step step10_gate1_numerical.txt 10800 3600 run_codex_then_claude; }

run_step_11() { dispatch_step step11_constructive_review.txt 7200 1800 run_codex_then_claude; }

run_step_12() { dispatch_step step12_revision.txt 14400 3600 run_claude_then_codex; }
run_step_13() {
    if _ablate_on "$ABLATE_NO_JUDGE"; then
        log "   ABLATION: skipping Gate-2 judge (ABLATE_NO_JUDGE); writing PASS stub"
        _write_judge_stub
        return 0
    fi

    # Step 13 缓存检查: 如果论文内容未变,直接复用缓存的评分结果
    log "   Checking Step 13 judge evaluation cache"
    if [[ -x "$FACTORY/scripts/step13_judge_cache.py" ]]; then
        if "$FACTORY/scripts/step13_judge_cache.py" "$PROJECT" --check >> "$PROJECT/logs/runner.log" 2>&1; then
            log "   Step 13 cache HIT — reusing cached evaluation"
            # 缓存命中(exit 0),judge_evaluation.md 应该已存在,直接验证即可
            if [[ -f "$PROJECT/judge_evaluation.md" ]]; then
                return 0
            else
                log "   WARNING: cache hit but judge_evaluation.md missing, re-evaluating"
            fi
        else
            local cache_exit=$?
            if (( cache_exit == 1 )); then
                log "   Step 13 cache PARTIAL HIT — evaluating changed sections"
            else
                log "   Step 13 cache MISS — full evaluation needed"
            fi
        fi
    fi

    dispatch_step step13_gate2_judge.txt 10800 3600 run_codex_then_claude

    # 评分完成后保存到缓存
    if [[ -f "$PROJECT/judge_evaluation.md" ]] && [[ -x "$FACTORY/scripts/step13_judge_cache.py" ]]; then
        local verdict=""
        local score=0
        verdict=$(awk -F': *' '/^VERDICT:/{print $2; exit}' "$PROJECT/judge_evaluation.md" 2>/dev/null | tr -d ' ')
        score=$(grep -oP '整体得分[：:]\s*\K[\d.]+' "$PROJECT/judge_evaluation.md" 2>/dev/null | head -1 || echo "0")

        if [[ -n "$verdict" ]] && [[ -n "$score" ]]; then
            local reopen_cycle=1
            [[ -f "$(gate2_reopened_once_file)" ]] && reopen_cycle=2

            "$FACTORY/scripts/step13_judge_cache.py" "$PROJECT" --save \
                --verdict "$verdict" --score "$score" --reopen-cycle "$reopen_cycle" \
                >> "$PROJECT/logs/runner.log" 2>&1 || true
            log "   Step 13 evaluation cached (verdict=$verdict, score=$score)"
        fi
    fi
}

run_step_14() { dispatch_step step14_abstract.txt 7200 1800 run_claude_then_codex; }
run_step_15() { dispatch_step step15_polish.txt 10800 3600 run_codex_then_claude; }

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
    log "   Running final delivery quality gate"
    if delivery_quality_gate "$PROJECT" > "$PROJECT/delivery_quality_gate.json" 2> "$PROJECT/delivery_quality_gate.stderr.log"; then
        log "   Final delivery quality gate PASS"
    else
        log "   Final delivery quality gate FAIL — project remains incomplete"
        return 1
    fi
    # Update checkpoint
    _set_checkpoint_step 16
}

# ── Main step loop ────────────────────────────────────────────────────

PROJECT_KILLED=0

# Pre-flight consultation: seed the run with human / frontier-model input
# (problem reading + candidate methods) before Step 1.  No-op unless the
# project opted in (consultation/enabled + the `preflight` gate active).
if (( STEP < 1 )); then
    maybe_consult preflight 0 "启动前 seed：题目解读与候选方法（贴 GPT Pro / Gemini Deep Think 的初步结论）"
fi

while (( STEP < 16 )); do
    NEXT=$((STEP + 1))
    RETRIES=0
    TIMEOUT=$(step_timeout "$NEXT")

    # 步骤特定的重试限制
    local STEP_MAX_RETRIES=$MAX_RETRIES
    case "$NEXT" in
        6)  STEP_MAX_RETRIES=$MAX_RETRIES_STEP6 ;;
        13) STEP_MAX_RETRIES=$MAX_RETRIES_STEP13 ;;
    esac

    # Pre-Step-4 consultation: confirm the modeling route before the full
    # model is constructed.  No-op unless opted in + the `step4` gate active.
    if (( NEXT == 4 )); then
        maybe_consult step4 4 "建模定型前：确认 model.md 的建模路线（贴前沿模型的建模方案）"
    fi

    while (( RETRIES < STEP_MAX_RETRIES )); do
        log "-- Step $NEXT | attempt $((RETRIES + 1))/$STEP_MAX_RETRIES | timeout ${TIMEOUT}s --"

        STEP_LOG="$PROJECT/logs/step_${NEXT}_$(date +%Y%m%d_%H%M%S).log"
        log "   log: $STEP_LOG"
        touch "$STEP_LOG"
        diag_event "$PROJECT" "$NEXT" step_started "" "Step $NEXT started" "$STEP_LOG"
        diag_status "$PROJECT" running "$NEXT" step_dispatch "" "" "" "file:$STEP_LOG"

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
        STEP_RC=$?
        set -e

        # Stop activity monitor
        kill "$MONITOR_PID" 2>/dev/null || true
        wait "$MONITOR_PID" 2>/dev/null || true

        # Kill lingering child processes
        cleanup_children

        if (( NEXT == 9 )) && (( STEP_RC == 42 )); then
            log "   Step 8.5 requires revision — leaving checkpoint at Step 8"
            _set_checkpoint_step 8
            echo "AWAITING_STEP8_5:8 $(date +%s)" > "$PROJECT/.heartbeat"
            # Exit cleanly, mirroring the consultation-gate pattern (CLAUDE.md:
            # awaiting states exit 0 so the activity monitor never treats them
            # as a hang/crash and launch_agents.sh status shows AWAIT_8.5).
            exit 0
        fi

        # Agent-initiated consultation (dynamic gate): an agent that hit a hard
        # judgment call wrote consultation/REQUEST.md and left the step
        # incomplete.  If the human has since answered (STATUS: READY), archive
        # the request and let the step re-run with the answer in human_review.md;
        # otherwise pause for input (maybe_consult exits cleanly).
        if consult_gate_active dynamic && [[ -f "$CONSULT_DIR/REQUEST.md" ]]; then
            if consult_ready dynamic; then
                log "   CONSULT[dynamic]: resolved — archiving request, re-running Step $NEXT"
                mv "$CONSULT_DIR/REQUEST.md" \
                   "$CONSULT_DIR/REQUEST.resolved.$(date +%s).md" 2>/dev/null || true
                rm -f "$CONSULT_AWAIT_MARKER" 2>/dev/null || true
                # Reset the repeatable dynamic section so a later request re-pauses.
                _consult_drop_section dynamic
            else
                maybe_consult dynamic "$NEXT" "Step $NEXT agent 主动求助" "$CONSULT_DIR/REQUEST.md"
            fi
        fi

        if project_is_killed "$PROJECT"; then
            PROJECT_KILLED=1
            log "   Project marked KILLED at the Step 1 viability gate"
            break
        fi

        # Verify step completed regardless of exit code.
        # First check file-state (infer_step), then semantic validation.
        if verify_step "$NEXT" && verify_step_output "$NEXT"; then
            if (( NEXT == 13 )); then
                verdict=""
                verdict=$(gate2_verdict)

                if [[ -f "$(gate2_reopen_marker)" ]]; then
                    rm -f "$(gate2_reopen_marker)"
                    log "   Gate 2 reopen cycle completed"
                    if [[ "$verdict" == "REOPEN_REVISION_TEXT" || "$verdict" == "REOPEN_REVISION_MODEL" ]]; then
                        block_gate2_after_reopen "$verdict"
                        exit 1
                    fi
                elif [[ "$verdict" == "REOPEN_REVISION_TEXT" || "$verdict" == "REOPEN_REVISION_MODEL" ]]; then
                    if [[ ! -f "$(gate2_reopened_once_file)" ]]; then
                        touch "$(gate2_reopened_once_file)"
                        touch "$(gate2_reopen_marker)"
                        log "   Step 13 verdict $verdict — reopening Step 12 once"
                        STEP=11
                        echo "$STEP $(date +%s)" > "$PROJECT/.heartbeat"
                        _set_checkpoint_step "$STEP"
                        break
                    else
                        block_gate2_after_reopen "$verdict"
                        exit 1
                    fi
                elif [[ -z "$verdict" ]]; then
                    log "   Step 13 judge_evaluation.md has no VERDICT line — blocking delivery"
                    echo "BLOCKED_STEP13:MISSING_VERDICT $(date +%s)" > "$PROJECT/.heartbeat"
                    exit 1
                elif [[ "$verdict" != "PASS" ]]; then
                    log "   Step 13 VERDICT '$verdict' not recognized — blocking delivery"
                    echo "BLOCKED_STEP13:UNKNOWN_VERDICT $(date +%s)" > "$PROJECT/.heartbeat"
                    exit 1
                fi
            fi

            diag_event "$PROJECT" "$NEXT" step_completed "" "Step $NEXT verified" "$STEP_LOG"
            diag_status "$PROJECT" running "$NEXT" step_complete "" "" "" "file:$STEP_LOG"
            if (( NEXT == 16 )); then
                diag_status "$PROJECT" completed 16 delivery_complete "" "项目已完成" "" "file:logs/runner.log"
            fi
            log "   Step $NEXT VERIFIED"
            STEP=$NEXT
            echo "$STEP $(date +%s)" > "$PROJECT/.heartbeat"
            # Update checkpoint.md from shell (don't rely on LLM)
            _set_checkpoint_step "$STEP"
            break
        else
            RETRIES=$((RETRIES + 1))

            # Classify error for intelligent retry
            local err_class
            err_class=$(classify_step_error "$STEP_LOG")

            log "   Step $NEXT NOT VERIFIED (expected output files missing or invalid)"
            log "   Error classification: $err_class"
            diag_event "$PROJECT" "$NEXT" verification_failed VERIFY_OUTPUT_FAILED \
                "Step $NEXT output missing or invalid" "$STEP_LOG"
            diag_status "$PROJECT" retrying "$NEXT" verification VERIFY_OUTPUT_FAILED \
                "Step $NEXT 产物校验未通过，等待重试" \
                "open_runner_log,refresh_status" "file:$STEP_LOG"

            # Permanent errors: stop immediately
            if [[ "$err_class" == PERMANENT_* ]]; then
                log "   PERMANENT error detected — stopping retries"
                RETRIES=$STEP_MAX_RETRIES
            fi

            # Step 6/13 快速失败诊断
            if (( NEXT == 6 || NEXT == 13 )) && (( RETRIES >= 1 )); then
                log "   Step $NEXT failed $((RETRIES)) times — entering diagnostic mode"
                if (( NEXT == 6 )); then
                    log "   Hint: Check solve_log.md §Step 6 接力 for sweep list"
                    log "   Hint: Run 'python3 scripts/step6_coverage_precheck.py $PROJECT' for details"
                elif (( NEXT == 13 )); then
                    log "   Hint: Check judge_evaluation.md structure and VERDICT line"
                    log "   Hint: Ensure paper PDF compiles successfully"
                fi
            fi

            if (( RETRIES < STEP_MAX_RETRIES )); then
                # Exponential backoff: 30s, 60s, 120s, 300s, 600s
                local delays=(30 60 120 300 600)
                local delay_idx=$((RETRIES - 1))
                local delay=${delays[$delay_idx]:-600}

                log "   Retrying in ${delay}s (attempt $((RETRIES + 1))/$STEP_MAX_RETRIES)..."
                sleep "$delay"
            fi
        fi
    done

    if (( PROJECT_KILLED )); then
        break
    fi

    if (( RETRIES >= STEP_MAX_RETRIES )); then
        log "FATAL: Step $NEXT failed after $STEP_MAX_RETRIES attempts. Stopping."
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
