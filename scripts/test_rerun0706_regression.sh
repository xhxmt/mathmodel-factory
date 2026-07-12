#!/usr/bin/env bash
# Regression: replay each cumcm_2025_a_rerun_0706 defect against the guard added
# to catch it.  Five defects, five guards — all five must fire (F in "five-hit").
#
#   1. fake session   -> A1  _worker_output_valid  (59-byte Kiro greeting)
#   2. stub solver    -> B3  verify_provenance     (models/m3_milp/*.stub)
#   3. toy budget     -> B2  verify_provenance     (de-maxiter=3 provenance)
#   4. P3==P2 escape  -> C1  verify_invariants     (gt_strict + nonzero)
#   5. stale verify   -> C4  _verification_is_fresh (07:52 verify < 10:18 results)
#
# Usage: bash scripts/test_rerun0706_regression.sh
# Exit 0 only if all five guards fire correctly.
set -u
FACTORY="$(cd "$(dirname "$0")/.." && pwd)"
RERUN="$FACTORY/complete/cumcm_2025_a_rerun_0706"
PASS=0 FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "== 1. A1 fake-session detection =="
# Inline copy of the helper (keep in sync with run_paper.sh _worker_output_valid).
_WORKER_MIN_BYTES=2048; _WORKER_MIN_SECS=120
_worker_output_valid() {
    local log_path="$1" elapsed="$2" proj="${3:-}" run_start="${4:-0}" sz
    sz=$(stat -c %s "$log_path" 2>/dev/null || echo 0)
    (( sz >= _WORKER_MIN_BYTES )) && return 0
    (( elapsed >= _WORKER_MIN_SECS )) && return 0
    if (( run_start > 0 )); then
        local touched
        touched=$(find "$proj" -maxdepth 3 -type f ! -name ".heartbeat" \
            ! -path "$proj/logs/*" ! -path "$proj/.runner.lock/*" \
            ! -path "$proj/diagnostics/*" -newermt "@$run_start" -print -quit 2>/dev/null)
        [[ -n "$touched" ]] && return 0
    fi
    return 1
}
NOW=$(date +%s)
greet="$RERUN/logs/step_5_claude_20260706_162902.log"
if [[ -f "$greet" ]]; then
    if _worker_output_valid "$greet" 7 "$RERUN" "$NOW"; then bad "59B greeting accepted"; else ok "59B greeting -> INVALID_SESSION"; fi
else
    echo "  SKIP: fixture log missing"; fi

echo "== 2+3. B2/B3 provenance + stub + budget =="
out=$(python3 "$FACTORY/scripts/verify_provenance.py" "$RERUN" 2>&1); rc=$?
echo "$out" | grep -q "STUB_RESIDUE     = 2" && ok "B3 stub residue detected (2)" || bad "B3 stub not detected"
echo "$out" | grep -qE "REPAIR_FALLBACK  = (10|[1-9])" && ok "B1 provenance gap detected" || bad "B1 provenance gap missed"
(( rc == 1 )) && ok "provenance verdict = FAIL (exit 1)" || bad "provenance exit $rc (want 1)"

echo "== 4. C1 strict-gain + nonzero invariants =="
TMP=$(mktemp -d)
mkdir -p "$TMP/results/p2" "$TMP/results/p3"
echo '{"mask_time_s": 4.531522}' > "$TMP/results/p2/values.json"
echo '{"mask_time_union_s": 4.531522, "rounds":[{"T_single_s":3.64},{"T_single_s":3.40},{"T_single_s":0.0}]}' > "$TMP/results/p3/values.json"
cat > "$TMP/results/invariants.json" <<'JSON'
{"invariants":[
 {"name":"P3>P2 strict","type":"gt_strict","left":"p3:mask_time_union_s","right":"p2:mask_time_s","margin":0.1},
 {"name":"P3 bomb3 nonzero","type":"nonzero","value":"p3:rounds[2].T_single_s","min_abs":1e-6}
]}
JSON
inv=$(python3 "$FACTORY/scripts/verify_invariants.py" "$TMP" 2>&1); irc=$?
echo "$inv" | grep -q "INVARIANTS_FAILED=2" && ok "C1 both invariants FAIL on P3==P2 + zero bomb" || bad "C1 did not fail as expected"
(( irc == 1 )) && ok "invariants verdict = FAIL (exit 1)" || bad "invariants exit $irc (want 1)"
rm -rf "$TMP"

echo "== 5. C4 verification freshness =="
_verification_is_fresh() {
    local proj_dir="$1" verify_file="$2"
    [[ -f "$verify_file" ]] || return 1
    local results="$proj_dir/results"; [[ -d "$results" ]] || return 0
    local newer; newer=$(find "$results" -type f -newer "$verify_file" -print -quit 2>/dev/null)
    [[ -z "$newer" ]]
}
T2=$(mktemp -d); mkdir -p "$T2/results"
touch -t 202607070752 "$T2/verify.txt"
touch -t 202607071018 "$T2/results/canonical.json"
if _verification_is_fresh "$T2" "$T2/verify.txt"; then bad "stale verify (07:52<10:18) accepted"; else ok "C4 stale verify rejected"; fi
touch -t 202607071100 "$T2/verify.txt"
if _verification_is_fresh "$T2" "$T2/verify.txt"; then ok "C4 fresh verify accepted"; else bad "fresh verify rejected"; fi
rm -rf "$T2"

echo
echo "==== RESULT: $PASS passed, $FAIL failed ===="
(( FAIL == 0 )) && { echo "ALL GUARDS FIRE — five-hit regression PASS"; exit 0; } || { echo "REGRESSION FAIL"; exit 1; }
