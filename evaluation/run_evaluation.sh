#!/usr/bin/env bash
# evaluation/run_evaluation.sh — external, reproducible, cross-comparable quality
# evaluation of a finished Modeling Factory project. See evaluation/README.md.
#
# Pipeline:  precheck gate -> compile -> numeric-traceability signal ->
#            assemble judge input -> claude -p judge x K -> aggregate (median+spread)
#
# NOTE: intentionally NOT `set -e`. scripts/verify_numbers.py exits 1 whenever
# any paper number is unmatched (verify_numbers.py:217) — that is a signal we
# want to capture, not a fatal error. Critical steps are guarded explicitly.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$REPO_ROOT/scripts"
RESULTS_DIR="$REPO_ROOT/evaluation/results"
PROMPT_FILE="$REPO_ROOT/evaluation/llm_judge_prompt.txt"

# API keys (DEEPSEEK_API_KEY / GEMINI_API_KEY) live in .env; source it so the
# shared caller scripts/llm_judge_call.py can see them. Existing env wins.
if [ -f "$REPO_ROOT/.env" ]; then
  set -a; . "$REPO_ROOT/.env"; set +a
fi

# Evidence files fed to the judge alongside the paper .tex (each guarded by -f).
EVIDENCE_FILES=(model.md solve_log.md sensitivity_report.md evaluation.md symbol_table.md assumption_ledger.md)

usage() {
  cat >&2 <<EOF
Usage: $0 <project_dir> [base_name] [--samples K] [--force] [--json]

  <project_dir>   complete/<base> or ongoing/<base> (relative to repo root or cwd)
  [base_name]     defaults to basename of <project_dir>
  --samples K     judge sampling rounds, median is reported (default 3)
  --force         run the judge even if the structural precheck fails
  --json          also print the aggregate JSON to stdout

Env overrides: CLAUDE_BIN, CLAUDE_MODEL (default deepseek-chat), CLAUDE_EFFORT, JUDGE_TIMEOUT (sec, default 360)
EOF
  exit 2
}

# ---- arg parse ----
PROJECT=""; BASE=""; SAMPLES=3; FORCE=0; JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --samples) SAMPLES="${2:?--samples needs a value}"; shift 2 ;;
    --samples=*) SAMPLES="${1#*=}"; shift ;;
    --force) FORCE=1; shift ;;
    --json) JSON=1; shift ;;
    -h|--help) usage ;;
    -*) echo "Unknown option: $1" >&2; usage ;;
    *) if [ -z "$PROJECT" ]; then PROJECT="$1"
       elif [ -z "$BASE" ]; then BASE="$1"
       else echo "Unexpected arg: $1" >&2; usage; fi
       shift ;;
  esac
done
[ -n "$PROJECT" ] || usage
case "$SAMPLES" in (*[!0-9]*|"") echo "ERROR: --samples must be a positive integer" >&2; exit 2 ;; esac
[ "$SAMPLES" -ge 1 ] || { echo "ERROR: --samples must be >= 1" >&2; exit 2; }

# resolve project dir (relative to cwd or repo root), then absolutize
[ -d "$PROJECT" ] || { [ -d "$REPO_ROOT/$PROJECT" ] && PROJECT="$REPO_ROOT/$PROJECT"; }
[ -d "$PROJECT" ] || { echo "ERROR: project dir not found: $PROJECT" >&2; exit 2; }
PROJECT="$(cd "$PROJECT" && pwd)"
[ -n "$BASE" ] || BASE="$(basename "$PROJECT")"

CLAUDE_BIN="${CLAUDE_BIN:-claude}"

# Judge model. Default deepseek-chat: the perturbation study (phase 5.3b) showed
# it dramatically outperforms haiku[1m] at detecting degraded papers (total-score
# deduction rate 73% vs 0%), and unlike opus[1m] on the anyrouter.top router it
# does not stall on this heavy scoring workload. Backend is picked by name prefix
# in scripts/llm_judge_call.py (deepseek* / gemini* / else=claude). Override with
# CLAUDE_MODEL, e.g. CLAUDE_MODEL=opus (direct Anthropic key) or =haiku[1m].
CLAUDE_MODEL="${CLAUDE_MODEL:-deepseek-chat}"

# A claude-* model needs the CLI on PATH; API backends (deepseek/gemini) don't.
case "$CLAUDE_MODEL" in
  deepseek*|gemini*) : ;;
  *) command -v "$CLAUDE_BIN" >/dev/null 2>&1 || { echo "ERROR: '$CLAUDE_BIN' not on PATH" >&2; exit 3; } ;;
esac
mkdir -p "$RESULTS_DIR"

echo ">>> Evaluating $BASE  (project: $PROJECT, samples: $SAMPLES)"

# ---- 1. structural precheck gate (cheap; don't burn judge tokens on junk) ----
echo ">>> [1/5] precheck: evaluate_modeling_project.py"
PRECHECK_JSON="$RESULTS_DIR/${BASE}_precheck.json"
PRECHECK_STDERR="$RESULTS_DIR/${BASE}_precheck.stderr.log"
if python3 "$SCRIPTS/evaluate_modeling_project.py" "$PROJECT" --json >"$PRECHECK_JSON" 2>"$PRECHECK_STDERR"; then
  PRECHECK_PASSED=true;  echo "    precheck PASS"
else
  PRECHECK_PASSED=false; echo "    precheck FAIL:"
  python3 "$SCRIPTS/evaluate_modeling_project.py" "$PROJECT" 2>/dev/null | grep -E '^\[FAIL\]' | sed 's/^/      /' || true
  if [ "$FORCE" -ne 1 ]; then
    echo "    refusing to spend judge tokens on an incomplete project (use --force to override)." >&2
    exit 1
  fi
  echo "    --force given; continuing."
fi

# ---- 2. ensure fresh PDF (parity with in-loop Step 13; best-effort) ----
echo ">>> [2/5] compile_paper.sh (best-effort)"
if [ -f "$PROJECT/${BASE}_paper.tex" ]; then
  if "$REPO_ROOT/compile_paper.sh" "$PROJECT" "$BASE" >/dev/null 2>&1; then
    echo "    compiled ${BASE}_paper.pdf"
  else
    echo "    WARN: compile failed; judging from .tex text"
  fi
else
  echo "    WARN: ${BASE}_paper.tex not found"
fi

# ---- 3. numeric-traceability signal (verify_numbers exits 1 on any unmatched) ----
echo ">>> [3/5] verify_numbers.py"
VN_OUT="$(python3 "$SCRIPTS/verify_numbers.py" "$PROJECT" "$BASE" 2>/dev/null)" || true
UNMATCHED="$(printf '%s\n' "$VN_OUT" | sed -n 's/^UNMATCHED (no source found):[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)"
[ -n "$UNMATCHED" ] || UNMATCHED="NA"
echo "    UNMATCHED numbers: $UNMATCHED"

# ---- 4. assemble judge input (prompt header + paper + evidence, inline) ----
echo ">>> [4/5] assembling judge input"
INPUT_FILE="$(mktemp)"
trap 'rm -f "$INPUT_FILE"' EXIT
{
  sed "s/__BASE_NAME__/${BASE}/g" "$PROMPT_FILE"
  printf '\n═══════════════════════════════════════════════\n=== 输入材料开始 ===\n'
  printf 'UNMATCHED_NUMBERS = %s\n\n' "$UNMATCHED"
  printf -- '----- 论文 LaTeX 源 (%s_paper.tex) -----\n' "$BASE"
  cat "$PROJECT/${BASE}_paper.tex" 2>/dev/null || echo "(缺失)"
  for f in "${EVIDENCE_FILES[@]}"; do
    if [ -f "$PROJECT/$f" ]; then
      printf -- '\n----- 证据: %s -----\n' "$f"
      cat "$PROJECT/$f"
    fi
  done
  printf '\n=== 输入材料结束 — 现在直接输出评分卡 ===\n'
} > "$INPUT_FILE"

# ---- 5. run judge K times (stdin = full prompt; avoids ARG_MAX limits) ----
echo ">>> [5/5] judging x$SAMPLES"
judge_call() {  # prompt on stdin -> scorecard on stdout
  # Routes through scripts/llm_judge_call.py, which picks the backend by model
  # name prefix: deepseek* (DeepSeek API), gemini* (Google API), else `claude -p`.
  # For the claude backend it sets --strict-mcp-config (drops ambient MCP servers
  # like grok-search) and passes CLAUDE_EFFORT through. (NOTE: --disallowedTools
  # was tried to force no-tools but HANGS claude -p, so it is intentionally unused
  # — see evaluation/README.) Model defaults to deepseek-chat (see note above).
  python3 "$SCRIPTS/llm_judge_call.py" --model "$CLAUDE_MODEL" \
    --timeout "${JUDGE_TIMEOUT:-360}"
}
RUN_FILES=()
for k in $(seq 1 "$SAMPLES"); do
  OUT="$RESULTS_DIR/${BASE}_eval_run${k}.md"
  ERRLOG="$RESULTS_DIR/${BASE}_eval_run${k}.stderr.log"
  judge_call < "$INPUT_FILE" > "$OUT" 2>"$ERRLOG"
  rc=$?
  if [ "$rc" -eq 0 ] && [ -s "$OUT" ]; then
    if head -1 "$OUT" | grep -q '^VERDICT:'; then
      echo "    run $k -> $(head -1 "$OUT")"
    else
      echo "    run $k -> WARN: first line is not VERDICT: (malformed output)"
    fi
    RUN_FILES+=("$OUT")
  else
    body="$( [ -s "$OUT" ] && echo nonempty || echo empty )"
    echo "    run $k -> ERROR: judge failed (rc=$rc, stdout=$body). stderr tail:"
    tail -n 8 "$ERRLOG" 2>/dev/null | sed 's/^/        /' || true
  fi
done
[ "${#RUN_FILES[@]}" -gt 0 ] || { echo "ERROR: all $SAMPLES judge runs failed" >&2; exit 4; }

# ---- aggregate median + spread, enrich JSON, print one-line summary ----
AGG_JSON="$RESULTS_DIR/${BASE}_eval.json"
python3 "$SCRIPTS/parse_judge_score.py" --aggregate --base "$BASE" "${RUN_FILES[@]}" > "$AGG_JSON" || true

INLOOP="$(python3 "$SCRIPTS/parse_judge_score.py" "$PROJECT/judge_evaluation.md" --base "$BASE" 2>/dev/null \
          | python3 -c 'import json,sys; print(json.load(sys.stdin).get("total"))' 2>/dev/null)" || true
[ -n "$INLOOP" ] || INLOOP="NA"

python3 "$SCRIPTS/enrich_evaluation_result.py" "$AGG_JSON" \
  --precheck "$PRECHECK_JSON" \
  --unmatched "$UNMATCHED" \
  --inloop "$INLOOP" >/dev/null

SUMMARY="$(AGG_JSON="$AGG_JSON" PRECHECK_PASSED="$PRECHECK_PASSED" UNMATCHED="$UNMATCHED" \
           INLOOP="$INLOOP" BASE="$BASE" python3 - <<'PY'
import json, os
p = os.environ["AGG_JSON"]
d = json.load(open(p))
pc = "PASS" if d["precheck_passed"] else "FAIL"
# Report the recomputed median (dimension-sum, robust to total-score anchoring)
# as the headline external score; show the judge's anchored total in parens.
llm = d.get("llm_score", {})
ext = llm.get("median_recomputed")
ext = ext if ext is not None else llm.get("median_total")
lo, hi = llm.get("min_recomputed"), llm.get("max_recomputed")
clamp = " [clamped]" if d.get("any_clamped") else ""
print(f'{os.environ["BASE"]}: external={ext}/100 '
      f'(spread {lo}-{hi}, anchored-total={llm.get("median_total")}){clamp}, '
      f'in-loop={os.environ["INLOOP"]}, unmatched={os.environ["UNMATCHED"]}, precheck={pc}')
PY
)"

echo ""
echo "=================================================="
echo "$SUMMARY"
echo "json: $AGG_JSON"
echo "=================================================="

[ "$JSON" -eq 1 ] && cat "$AGG_JSON"
exit 0
