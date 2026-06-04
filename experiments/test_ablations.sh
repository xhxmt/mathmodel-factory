#!/usr/bin/env bash
# experiments/test_ablations.sh — switch-effectiveness tests for the four
# ablation toggles. Fast (seconds), invokes NO agent, writes only to a mktemp
# dir. run_paper.sh has no main() guard and runs top-level logic on source, so
# we extract individual function bodies by name and eval them in isolation.
#
# Usage: ./experiments/test_ablations.sh
# Exit 0 iff all checks pass.
set -uo pipefail

FACTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$FACTORY/run_paper.sh"
PROMPTS="$FACTORY/prompts"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
note() { printf '\n== %s ==\n' "$1"; }

# Truthy helper (mirrors run_paper.sh); needed by every extracted function.
_ablate_on() { case "${1,,}" in 1|true|yes|on) return 0;; *) return 1;; esac; }

# Mirror run_paper.sh startup: all four toggles are always bound (default 0), so
# extracted functions referencing them don't trip `set -u` when a test sets only
# one via a command-prefix. Per-invocation prefixes still override these.
export ABLATE_NO_CONSULTATION=0 ABLATE_NO_METHOD_LIB=0 \
       ABLATE_NO_JUDGE=0 ABLATE_NO_INNOVATION_PROTECT=0

# extract_fn <name> <file> : print the function body via brace counting.
extract_fn() {
    awk -v fn="$1" '
        $0 ~ "^"fn"\\(\\) *\\{" {capture=1; depth=0}
        capture {
            print
            n=gsub(/{/,"{"); m=gsub(/}/,"}"); depth+=n-m
            if (depth<=0 && /}/) exit
        }' "$2"
}

# eval_fn <name> : extract from RUNNER, bash -n the snippet, then source it.
eval_fn() {
    local name="$1" snip="$TMP/$1.fn.sh"
    extract_fn "$name" "$RUNNER" > "$snip"
    [ -s "$snip" ] || { bad "extract $name (empty)"; return 1; }
    bash -n "$snip" 2>/dev/null || { bad "extract $name (syntax)"; return 1; }
    # shellcheck disable=SC1090
    . "$snip"
}

# ── (a) syntax ───────────────────────────────────────────────────────────────
note "(a) syntax"
if bash -n "$RUNNER"; then ok "run_paper.sh bash -n"; else bad "run_paper.sh bash -n"; fi
syntax_all=1
for s in "$FACTORY"/experiments/*.sh; do
    bash -n "$s" || { bad "bash -n $(basename "$s")"; syntax_all=0; }
done
[ "$syntax_all" -eq 1 ] && ok "experiments/*.sh bash -n"
if python3 -m py_compile "$FACTORY/experiments/compare_ablations.py" 2>/dev/null; then
    ok "compare_ablations.py py_compile"; else bad "compare_ablations.py py_compile"; fi

# ── (b) method-library toggle ────────────────────────────────────────────────
note "(b) ABLATE_NO_METHOD_LIB flips the citation gate"
printf 'cites method_library/bogus/nope.md which is not registered\n' > "$TMP/viable_streams.md"
if eval_fn check_method_citations; then
    ABLATE_NO_METHOD_LIB=0 check_method_citations "$TMP" "$TMP/viable_streams.md" >/dev/null 2>&1
    off_rc=$?
    ABLATE_NO_METHOD_LIB=1 check_method_citations "$TMP" "$TMP/viable_streams.md" >/dev/null 2>&1
    on_rc=$?
    [ "$off_rc" -ne 0 ] && ok "OFF: bogus citation rejected (rc=$off_rc)" \
        || bad "OFF: expected rejection, got rc=$off_rc"
    [ "$on_rc" -eq 0 ] && ok "ON: gate disabled, citation passes (rc=0)" \
        || bad "ON: expected rc=0, got rc=$on_rc"
fi

# ── (c) render_prompt strips on, keeps off ──────────────────────────────────
note "(c) render_prompt prompt-text ablations"
common_prompt_preamble() { :; }   # stub
PROJECT="$TMP/proj"; BASE="t"; QUESTION="q"
if eval_fn render_prompt; then
    WEB='用 web 检索拿到主文献'
    on_web="$(ABLATE_NO_CONSULTATION=1 render_prompt step1_research_viability.txt)"
    off_web="$(ABLATE_NO_CONSULTATION=0 render_prompt step1_research_viability.txt)"
    grep -q "$WEB" <<<"$off_web" && ok "consult OFF: web clause present" \
        || bad "consult OFF: web clause missing (pattern drift?)"
    grep -q "$WEB" <<<"$on_web" && bad "consult ON: web clause NOT stripped" \
        || ok "consult ON: web clause stripped"
    # placeholder substitution still works after array rewrite
    grep -q '__PROJECT_PATH__' <<<"$off_web" && bad "placeholder __PROJECT_PATH__ not substituted" \
        || ok "placeholder substitution intact"

    on_prot="$(ABLATE_NO_INNOVATION_PROTECT=1 render_prompt step12_revision.txt)"
    off_prot="$(ABLATE_NO_INNOVATION_PROTECT=0 render_prompt step12_revision.txt)"
    off_n=$(grep -c 'PROTECTED' <<<"$off_prot")
    on_n=$(grep -c 'PROTECTED' <<<"$on_prot")
    [ "$off_n" -gt 0 ] && ok "innov OFF: PROTECTED lines present ($off_n)" \
        || bad "innov OFF: no PROTECTED lines (pattern drift?)"
    [ "$on_n" -lt "$off_n" ] && ok "innov ON: enforcement lines stripped ($off_n -> $on_n)" \
        || bad "innov ON: nothing stripped ($off_n -> $on_n)"
    # step4 label-creation table row must survive the strip
    on_s4="$(ABLATE_NO_INNOVATION_PROTECT=1 render_prompt step4_model_construction.txt)"
    grep -q '| A1 |' <<<"$on_s4" && ok "innov ON: step4 label table row A1 preserved" \
        || bad "innov ON: step4 label table row A1 wrongly removed"
fi

# ── (d) no-judge stub satisfies Step-13 contract ─────────────────────────────
note "(d) ABLATE_NO_JUDGE writes a PASS stub instead of judging"
log() { :; }                                   # stub runner logger
run_codex_then_claude() { echo "REAL_JUDGE_RAN"; return 9; }   # must NOT be called when ablated
PROJECT="$TMP/p13"; mkdir -p "$PROJECT"; BASE="t13"
if eval_fn _write_judge_stub && eval_fn run_step_13; then
    out="$(ABLATE_NO_JUDGE=1 run_step_13 2>/dev/null)"; rc=$?
    [ "$rc" -eq 0 ] && ok "ON: run_step_13 returns 0" || bad "ON: rc=$rc (expected 0)"
    grep -q REAL_JUDGE_RAN <<<"$out" && bad "ON: real judge was invoked" \
        || ok "ON: real judge NOT invoked"
    je="$PROJECT/judge_evaluation.md"
    if [ -f "$je" ]; then
        first="$(awk -F': *' '/^VERDICT:/{print $2; exit}' "$je" | tr -d '\r')"
        [ "$first" = "PASS" ] && ok "ON: first VERDICT line is PASS" \
            || bad "ON: VERDICT is '$first' (expected PASS)"
        n=$(wc -l < "$je")
        [ "$n" -ge 30 ] && ok "ON: stub >= 30 lines ($n)" || bad "ON: stub only $n lines (<30)"
    else
        bad "ON: judge_evaluation.md not written"
    fi
    # OFF path must call the real judge
    out_off="$(ABLATE_NO_JUDGE=0 run_step_13 2>/dev/null)"
    grep -q REAL_JUDGE_RAN <<<"$out_off" && ok "OFF: real judge invoked" \
        || bad "OFF: real judge NOT invoked"
fi

# ── (e) compare_ablations.py smoke (read-only, real eval JSONs if present) ───
note "(e) compare_ablations.py smoke"
RES="$FACTORY/evaluation/results"
if [ -f "$RES/test_cumcm2024a_eval.json" ] && [ -f "$RES/test_cumcm2024b_eval.json" ]; then
    if python3 "$FACTORY/experiments/compare_ablations.py" \
        --baseline test_cumcm2024b --variant test_cumcm2024a >"$TMP/cmp.out" 2>/dev/null; then
        grep -qi 'baseline' "$TMP/cmp.out" && ok "compare prints a delta table" \
            || bad "compare ran but output missing baseline row"
    else
        bad "compare_ablations.py exited non-zero"
    fi
else
    note "(e) skipped — no real eval JSONs in evaluation/results/"
fi

# ── summary ──────────────────────────────────────────────────────────────────
printf '\n========================================\n'
printf 'ablation toggle tests: %d passed, %d failed\n' "$PASS" "$FAIL"
printf '========================================\n'
[ "$FAIL" -eq 0 ]
