#!/usr/bin/env bash
# evaluation/run_evaluation.sh — external, reproducible, cross-comparable quality
# evaluation of a finished Modeling Factory project. See evaluation/README.md.
#
# Pipeline: precheck -> compile -> build isolated packets -> run three independent
#           judge calls per sample -> correctness-veto aggregation -> median/spread
#
# NOTE: intentionally NOT `set -e`. scripts/verify_numbers.py exits 1 whenever
# any paper number is unmatched (verify_numbers.py:217) — that is a signal we
# want to capture, not a fatal error. Critical steps are guarded explicitly.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$REPO_ROOT/scripts"
RESULTS_DIR="${EVALUATION_RESULTS_DIR:-$REPO_ROOT/evaluation/results}"
JUDGE_PROMPTS="$REPO_ROOT/prompts/judges"
CALIBRATION_REPORT="${EVALUATION_CALIBRATION_REPORT:-$REPO_ROOT/evaluation/proxy_calibration_report.json}"
case "$CALIBRATION_REPORT" in
  /*) : ;;
  *) CALIBRATION_REPORT="$REPO_ROOT/$CALIBRATION_REPORT" ;;
esac

# API keys (DEEPSEEK_API_KEY / GEMINI_API_KEY) live in .env; source it so the
# shared caller scripts/llm_judge_call.py can see them. Existing env wins.
if [ -f "$REPO_ROOT/.env" ]; then
  set -a; . "$REPO_ROOT/.env"; set +a
fi

usage() {
  cat >&2 <<EOF
Usage: $0 <project_dir> [base_name] [--samples K] [--force] [--json]

  <project_dir>   complete/<base> or ongoing/<base> (relative to repo root or cwd)
  [base_name]     defaults to basename of <project_dir>
  --samples K     judge sampling rounds, median is reported (default 3)
  --force         retained for CLI compatibility; structural precheck is evidence only
  --json          also print the aggregate JSON to stdout

Env overrides: CLAUDE_BIN, CLAUDE_MODEL (default deepseek-chat), CLAUDE_EFFORT, JUDGE_TIMEOUT (sec, default 360), EVALUATION_CALIBRATION_REPORT
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
RUN_PARENT="$RESULTS_DIR/runs/$BASE"
mkdir -p "$RUN_PARENT"
STAGING_DIR="$(mktemp -d "$RUN_PARENT/.staging.XXXXXX")"
TMP_DIR=""
cleanup() {
  [ -z "${TMP_DIR:-}" ] || rm -rf "$TMP_DIR"
  [ -z "${STAGING_DIR:-}" ] || rm -rf "$STAGING_DIR"
}
trap cleanup EXIT

echo ">>> Evaluating $BASE  (project: $PROJECT, samples: $SAMPLES)"

# ---- 1. structural precheck gate (cheap; don't burn judge tokens on junk) ----
echo ">>> [1/5] precheck: evaluate_modeling_project.py"
PRECHECK_JSON="$STAGING_DIR/precheck.json"
PRECHECK_STDERR="$STAGING_DIR/precheck.stderr.log"
if python3 "$SCRIPTS/evaluate_modeling_project.py" "$PROJECT" --json >"$PRECHECK_JSON" 2>"$PRECHECK_STDERR"; then
  PRECHECK_PASSED=true;  echo "    precheck PASS"
else
  PRECHECK_PASSED=false; echo "    precheck FAIL:"
  python3 "$SCRIPTS/evaluate_modeling_project.py" "$PROJECT" 2>/dev/null | grep -E '^\[FAIL\]' | sed 's/^/      /' || true
  echo "    continuing: internal Gate 2/delivery state is structural evidence, not external-scoring eligibility."
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

# ---- 4. build isolated, hash-addressed judge packets ----
echo ">>> [4/5] building isolated judge packets"
python3 "$SCRIPTS/judge_packet.py" "$PROJECT" --base "$BASE" >/dev/null || {
  echo "ERROR: failed to build judge packets" >&2
  exit 4
}

# Packet byte integrity remains an immediate infrastructure gate. Role-level
# evidence completeness is different: the manifest declares it and the
# aggregator deterministically converts an incomplete role to INDETERMINATE,
# preserving a diagnostic result instead of exiting before publication.
if ! PROJECT="$PROJECT" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

project = Path(os.environ["PROJECT"])
manifests = {
    role: json.loads((project / "judge_packets" / role / "manifest.json").read_text())
    for role in ("paper", "math", "execution")
}

contexts_match = all(
    hashlib.sha256((project / "judge_packets" / role / "context.txt").read_bytes()).hexdigest()
    == manifests[role]["context"]["sha256"]
    for role in manifests
)
if not contexts_match:
    raise SystemExit("packet integrity FAIL: packet/context hash mismatch")
incomplete = [
    role for role, manifest in manifests.items()
    if manifest.get("completeness", {}).get("eligible") is not True
]
if incomplete:
    print(
        "    packet completeness INCOMPLETE: " + ", ".join(incomplete)
        + " (affected roles will be forced to INDETERMINATE)"
    )
PY
then
  echo "ERROR: judge packet integrity failed" >&2
  exit 4
fi

# Freeze the exact evaluator configuration. Raw outputs live in this unique,
# immutable run directory; the legacy flat JSON is updated only as a latest pointer.
CONFIG_JSON="$STAGING_DIR/configuration.json"
CONFIG_FINGERPRINT="$(PROJECT="$PROJECT" BASE="$BASE" MODEL="$CLAUDE_MODEL" SAMPLES="$SAMPLES" \
  UNMATCHED="$UNMATCHED" PRECHECK_PASSED="$PRECHECK_PASSED" CONFIG_JSON="$CONFIG_JSON" \
  REPO_ROOT="$REPO_ROOT" CALIBRATION_REPORT="$CALIBRATION_REPORT" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

root = Path(os.environ["REPO_ROOT"])
project = Path(os.environ["PROJECT"])
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def optional_file_record(path):
    exists = path.is_file()
    return {
        "path": str(path),
        "exists": exists,
        "sha256": sha(path) if exists else None,
    }

record = {
    "version": 2,
    "base": os.environ["BASE"],
    "model": os.environ["MODEL"],
    "samples": int(os.environ["SAMPLES"]),
    "unmatched_numbers": os.environ["UNMATCHED"],
    "precheck_passed": os.environ["PRECHECK_PASSED"] == "true",
    "system_prompt_version": "paper-evaluation-untrusted-data-v1",
    "implementation": {
        "run_evaluation_sha256": sha(root / "evaluation/run_evaluation.sh"),
        "judge_packet_sha256": sha(root / "scripts/judge_packet.py"),
        "llm_judge_call_sha256": sha(root / "scripts/llm_judge_call.py"),
        "aggregate_judges_sha256": sha(root / "scripts/aggregate_judges.py"),
        "parse_judge_score_sha256": sha(root / "scripts/parse_judge_score.py"),
        "enrich_evaluation_result_sha256": sha(root / "scripts/enrich_evaluation_result.py"),
        "evaluate_modeling_project_sha256": sha(root / "scripts/evaluate_modeling_project.py"),
    },
    "prompts": {
        name: sha(root / "prompts/judges" / name)
        for name in ("math_auditor.txt", "execution_auditor.txt", "paper_reviewer.txt")
    },
    "packets": {
        role: json.loads((project / "judge_packets" / role / "manifest.json").read_text())["packet_fingerprint"]
        for role in ("math", "execution", "paper")
    },
    "calibration_report": optional_file_record(Path(os.environ["CALIBRATION_REPORT"])),
}
canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
record["configuration_fingerprint"] = hashlib.sha256(canonical.encode()).hexdigest()
path = Path(os.environ["CONFIG_JSON"])
tmp = path.with_name("." + path.name + ".tmp")
tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
tmp.replace(path)
print(record["configuration_fingerprint"])
PY
)"
RUN_STAMP="$(date -u +%Y%m%dT%H%M%S)"
RUN_ID="${RUN_STAMP}-${CONFIG_FINGERPRINT:0:12}-$$"
RUN_DIR="$RUN_PARENT/$RUN_ID"
[ ! -e "$RUN_DIR" ] || { echo "ERROR: result run already exists: $RUN_DIR" >&2; exit 4; }
mv "$STAGING_DIR" "$RUN_DIR"
STAGING_DIR=""
PRECHECK_JSON="$RUN_DIR/precheck.json"
PRECHECK_STDERR="$RUN_DIR/precheck.stderr.log"
CONFIG_JSON="$RUN_DIR/configuration.json"
TMP_DIR="$(mktemp -d)"

assemble_role_input() {
  role="$1"
  prompt="$JUDGE_PROMPTS/${role}_auditor.txt"
  [ "$role" = paper ] && prompt="$JUDGE_PROMPTS/paper_reviewer.txt"
  {
    sed -e "s|__PROJECT_PATH__|ISOLATED_PACKET|g" -e "s|__BASE_NAME__|$BASE|g" "$prompt"
    if [ "$role" = math ] || [ "$role" = execution ]; then
      printf '\n=== MACHINE-GENERATED NUMERIC TRACEABILITY SIGNAL ===\n'
      printf 'UNMATCHED_NUMBERS=%s\n' "$UNMATCHED"
      printf 'Treat NA as unavailable evidence. A nonzero value requires explicit audit attention; it is not by itself proof of a fatal flaw.\n'
    fi
    printf '\n=== PACKET MANIFEST START ===\n'
    cat "$PROJECT/judge_packets/$role/manifest.json"
    printf '\n=== PACKET MANIFEST END ===\n'
    printf '\n=== UNTRUSTED ISOLATED PACKET START ===\n'
    cat "$PROJECT/judge_packets/$role/context.txt"
    printf '\n=== UNTRUSTED ISOLATED PACKET END ===\n'
    printf 'Return only the requested role output. Do not add an outer code fence.\n'
  } > "$TMP_DIR/${role}.prompt.txt"
}

for role in math execution paper; do
  assemble_role_input "$role"
done

# ---- 5. run judge K times (stdin = full prompt; avoids ARG_MAX limits) ----
echo ">>> [5/5] judging x$SAMPLES"
judge_call() {  # prompt on stdin -> scorecard on stdout
  # Routes through scripts/llm_judge_call.py, which picks the backend by model
  # name prefix: deepseek* (DeepSeek API), gemini* (Google API), else `claude -p`.
  # For the claude backend it drops ambient MCP servers and explicitly disables
  # built-in tools, keeping the stdin packet evaluation single-shot/non-agentic.
  # CLAUDE_EFFORT is passed through. Model defaults to deepseek-chat (see above).
  (cd "$TMP_DIR" && python3 "$SCRIPTS/llm_judge_call.py" --model "$CLAUDE_MODEL" \
    --timeout "${JUDGE_TIMEOUT:-360}")
}
RUN_FILES=()
for k in $(seq 1 "$SAMPLES"); do
  ROLE_DIR="$TMP_DIR/run${k}"
  mkdir -p "$ROLE_DIR"
  for role in math execution paper; do
    ROLE_OUT="$ROLE_DIR/${role}.md"
    ERRLOG="$RUN_DIR/eval_run${k}_${role}.stderr.log"
    judge_call < "$TMP_DIR/${role}.prompt.txt" > "$ROLE_OUT" 2>"$ERRLOG"
    rc=$?
    if [ "$rc" -eq 0 ] && [ -s "$ROLE_OUT" ]; then
      echo "    run $k $role -> $(head -1 "$ROLE_OUT")"
    else
      rm -f "$ROLE_OUT"
      echo "    run $k $role -> ERROR rc=$rc (recorded as INDETERMINATE)"
    fi
  done
  OUT="$RUN_DIR/eval_run${k}.md"
  python3 "$SCRIPTS/aggregate_judges.py" \
    --math "$ROLE_DIR/math.md" \
    --execution "$ROLE_DIR/execution.md" \
    --paper "$ROLE_DIR/paper.md" \
    --math-manifest "$PROJECT/judge_packets/math/manifest.json" \
    --execution-manifest "$PROJECT/judge_packets/execution/manifest.json" \
    --paper-manifest "$PROJECT/judge_packets/paper/manifest.json" \
    --output "$OUT" \
    --json "$RUN_DIR/eval_run${k}_roles.json" \
    --base "$BASE" >/dev/null
  echo "    run $k aggregate -> $(head -1 "$OUT")"
  RUN_FILES+=("$OUT")
done

# ---- aggregate median + spread, enrich JSON, print one-line summary ----
AGG_JSON="$RUN_DIR/eval.json"
AGG_TMP="$RUN_DIR/.eval.json.tmp"
python3 "$SCRIPTS/parse_judge_score.py" --aggregate --base "$BASE" "${RUN_FILES[@]}" > "$AGG_TMP"
PARSE_RC=$?
if AGG_TMP="$AGG_TMP" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["AGG_TMP"])
data = json.loads(path.read_text())
if not isinstance(data, dict):
    raise SystemExit(1)
PY
then
  mv "$AGG_TMP" "$AGG_JSON"
else
  rm -f "$AGG_TMP"
  echo "ERROR: parser did not produce valid diagnostic JSON (rc=$PARSE_RC)" >&2
  exit 5
fi
if [ "$PARSE_RC" -ne 0 ]; then
  echo "    aggregate parser rc=$PARSE_RC; preserving diagnostic JSON for enrichment/publication"
fi

# Add evaluator identity before calibration enrichment. This prevents a result
# with an absent/ambiguous model identity from inheriting another judge's
# calibration readiness.
AGG_JSON="$AGG_JSON" CONFIG_JSON="$CONFIG_JSON" MODEL="$CLAUDE_MODEL" \
  PROJECT="$PROJECT" python3 - <<'PY'
import json
import os
from pathlib import Path

aggregate_path = Path(os.environ["AGG_JSON"])
config = json.loads(Path(os.environ["CONFIG_JSON"]).read_text())
aggregate = json.loads(aggregate_path.read_text())
project = Path(os.environ["PROJECT"])
packet_completeness = {
    role: json.loads(
        (project / "judge_packets" / role / "manifest.json").read_text()
    )["completeness"]
    for role in ("math", "execution", "paper")
}
aggregate["packet_completeness"] = packet_completeness
aggregate["scoring_eligible"] = all(
    item.get("eligible") is True for item in packet_completeness.values()
)
aggregate["model"] = os.environ["MODEL"]
aggregate["judge_config"] = {
    "model": os.environ["MODEL"],
    "configuration_fingerprint": config["configuration_fingerprint"],
    "system_prompt_version": config["system_prompt_version"],
    "samples": config["samples"],
}
aggregate["calibration_schema_version"] = 3
tmp = aggregate_path.with_name("." + aggregate_path.name + ".identity.tmp")
tmp.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n")
tmp.replace(aggregate_path)
PY

INLOOP="$(python3 "$SCRIPTS/parse_judge_score.py" "$PROJECT/judge_evaluation.md" --base "$BASE" 2>/dev/null \
          | python3 -c 'import json,sys; print(json.load(sys.stdin).get("total"))' 2>/dev/null)" || true
[ -n "$INLOOP" ] || INLOOP="NA"

ENRICH_TMP="$RUN_DIR/.eval.enrich.tmp.json"
cp "$AGG_JSON" "$ENRICH_TMP"
python3 "$SCRIPTS/enrich_evaluation_result.py" "$ENRICH_TMP" \
  --precheck "$PRECHECK_JSON" \
  --unmatched "$UNMATCHED" \
  --inloop "$INLOOP" \
  --calibration-report "$CALIBRATION_REPORT" >/dev/null || {
    rm -f "$ENRICH_TMP"
    echo "ERROR: failed to enrich aggregate result" >&2
    exit 5
  }
mv "$ENRICH_TMP" "$AGG_JSON"

# Bind the aggregate to the immutable run and exact configuration, then update
# the historical flat path as an atomic latest pointer. Existing legacy flat
# JSON is preserved inside the first immutable run that supersedes it.
AGG_JSON="$AGG_JSON" CONFIG_JSON="$CONFIG_JSON" RUN_ID="$RUN_ID" RUN_DIR="$RUN_DIR" \
  RESULTS_DIR="$RESULTS_DIR" BASE="$BASE" python3 - <<'PY'
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

aggregate_path = Path(os.environ["AGG_JSON"])
config = json.loads(Path(os.environ["CONFIG_JSON"]).read_text())
aggregate = json.loads(aggregate_path.read_text())
aggregate["evaluation_run"] = {
    "run_id": os.environ["RUN_ID"],
    "immutable_result": str(aggregate_path),
    "configuration_fingerprint": config["configuration_fingerprint"],
    "created_at": datetime.now(timezone.utc).isoformat(),
}
tmp = aggregate_path.with_name("." + aggregate_path.name + ".final.tmp")
tmp.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n")
tmp.replace(aggregate_path)

latest = Path(os.environ["RESULTS_DIR"]) / f"{os.environ['BASE']}_eval.json"
if latest.exists() and not latest.is_symlink():
    legacy = Path(os.environ["RUN_DIR"]) / "legacy_previous_flat_eval.json"
    if not legacy.exists():
        shutil.copy2(latest, legacy)
link_tmp = latest.with_name(f".{latest.name}.{os.getpid()}.tmp")
link_tmp.unlink(missing_ok=True)
link_tmp.symlink_to(os.path.relpath(aggregate_path, latest.parent))
os.replace(link_tmp, latest)
PY

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
echo "config fingerprint: $CONFIG_FINGERPRINT"
echo "=================================================="

[ "$JSON" -eq 1 ] && cat "$AGG_JSON"
chmod -R a-w "$RUN_DIR"
FINAL_RC="$(AGG_JSON="$AGG_JSON" python3 - <<'PY'
import json
import os

data = json.load(open(os.environ["AGG_JSON"]))
print(0 if data.get("comparison_ready") is True else 1)
PY
)"
exit "$FINAL_RC"
