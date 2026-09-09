#!/usr/bin/env bash
set -euo pipefail

# Self-contained Phase 3-6 verification entry for a normal supported checkout.
# It never installs dependencies or intentionally contacts the network. All
# Python cache and pytest temporary state is redirected outside the source tree.

readonly BOOTSTRAP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly BOOTSTRAP_NAME="phase46-bootstrap"

fail() {
  printf '[%s] ERROR: %s\n' "$BOOTSTRAP_NAME" "$*" >&2
  exit 2
}

resolve_python() {
  local requested="${PYTHON_BIN:-}"
  if [[ -n "$requested" ]]; then
    command -v -- "$requested" 2>/dev/null || return 1
    return 0
  fi
  if [[ -x "$BOOTSTRAP_ROOT/.venv/bin/python" ]]; then
    printf '%s\n' "$BOOTSTRAP_ROOT/.venv/bin/python"
    return 0
  fi
  command -v python3 2>/dev/null
}

PYTHON_EXECUTABLE="$(resolve_python)" || fail \
  "no Python interpreter found; set PYTHON_BIN to a Python >=3.11 environment with the project dev+web dependencies"
readonly PYTHON_EXECUTABLE

if [[ -n "${PHASE46_BOOTSTRAP_TMPDIR:-}" ]]; then
  BOOTSTRAP_TEMP_BASE="$PHASE46_BOOTSTRAP_TMPDIR"
else
  [[ -n "${HOME:-}" ]] || fail \
    "HOME is unset; set PHASE46_BOOTSTRAP_TMPDIR to an absolute external directory"
  BOOTSTRAP_TEMP_BASE="${XDG_CACHE_HOME:-$HOME/.cache}/paper_factory/phase46-bootstrap"
fi
[[ "$BOOTSTRAP_TEMP_BASE" == /* ]] || fail \
  "PHASE46_BOOTSTRAP_TMPDIR must be an absolute path"
[[ ! -L "$BOOTSTRAP_TEMP_BASE" ]] || fail \
  "temporary base must not be a symlink: $BOOTSTRAP_TEMP_BASE"
mkdir -p -- "$BOOTSTRAP_TEMP_BASE" || fail \
  "cannot create temporary base: $BOOTSTRAP_TEMP_BASE"
readonly BOOTSTRAP_TEMP_BASE="$(cd -- "$BOOTSTRAP_TEMP_BASE" && pwd -P)"
case "$BOOTSTRAP_TEMP_BASE/" in
  "$BOOTSTRAP_ROOT/"*)
    fail "temporary base must be outside the source tree: $BOOTSTRAP_TEMP_BASE"
    ;;
esac

BOOTSTRAP_RUN_DIR="$(mktemp -d \
  "$BOOTSTRAP_TEMP_BASE/run-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")" || fail \
  "cannot create a unique run directory below $BOOTSTRAP_TEMP_BASE"
readonly BOOTSTRAP_RUN_DIR
readonly BOOTSTRAP_LOG_DIR="$BOOTSTRAP_RUN_DIR/logs"
mkdir -p -- "$BOOTSTRAP_LOG_DIR" "$BOOTSTRAP_RUN_DIR/pytest" \
  "$BOOTSTRAP_RUN_DIR/structured" \
  "$BOOTSTRAP_RUN_DIR/pycache" "$BOOTSTRAP_RUN_DIR/runtime-tmp" \
  "$BOOTSTRAP_RUN_DIR/xdg-cache"
readonly BOOTSTRAP_RESULT_DIR="$BOOTSTRAP_RUN_DIR/structured"

export PYTHONDONTWRITEBYTECODE=1
export PYTHONHASHSEED=0
export PYTHONPATH="$BOOTSTRAP_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPYCACHEPREFIX="$BOOTSTRAP_RUN_DIR/pycache"
export TMPDIR="$BOOTSTRAP_RUN_DIR/runtime-tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
export XDG_CACHE_HOME="$BOOTSTRAP_RUN_DIR/xdg-cache"

CURRENT_STEP="startup"
BOOTSTRAP_STARTED="$(date +%s)"

finish() {
  local status=$?
  local finished elapsed
  finished="$(date +%s)"
  elapsed=$((finished - BOOTSTRAP_STARTED))
  if ((status == 0)); then
    printf '[%s] PASS: all gates completed in %ss\n' "$BOOTSTRAP_NAME" "$elapsed"
  else
    printf '[%s] FAIL: step=%s exit=%s elapsed=%ss\n' \
      "$BOOTSTRAP_NAME" "$CURRENT_STEP" "$status" "$elapsed" >&2
  fi
  printf '[%s] external evidence directory: %s\n' \
    "$BOOTSTRAP_NAME" "$BOOTSTRAP_RUN_DIR"
}
trap finish EXIT

run_logged() {
  local name="$1"
  shift
  CURRENT_STEP="$name"
  printf '[%s] RUN: %s\n' "$BOOTSTRAP_NAME" "$name"
  "$@" 2>&1 | tee "$BOOTSTRAP_LOG_DIR/$name.log"
  local command_status=${PIPESTATUS[0]}
  printf '[%s] EXIT: %s status=%s\n' \
    "$BOOTSTRAP_NAME" "$name" "$command_status"
  return "$command_status"
}

cd -- "$BOOTSTRAP_ROOT"
printf '[%s] source root: %s\n' "$BOOTSTRAP_NAME" "$BOOTSTRAP_ROOT"
printf '[%s] Python: %s\n' "$BOOTSTRAP_NAME" "$PYTHON_EXECUTABLE"
printf '[%s] policy: no dependency installation; no network command; no /tmp dependency\n' \
  "$BOOTSTRAP_NAME"

run_logged preflight "$PYTHON_EXECUTABLE" - <<'PY'
import importlib
import sys

if sys.version_info < (3, 11):
    raise SystemExit(
        f"Python >=3.11 is required; selected interpreter is {sys.version.split()[0]}"
    )

required = ("bcrypt", "fastapi", "httpx", "jwt", "pydantic", "pytest")
missing = []
for module in required:
    try:
        importlib.import_module(module)
    except ImportError as error:
        missing.append(f"{module}: {error}")
if missing:
    raise SystemExit(
        "selected Python lacks required dev+web dependencies; bootstrap will not "
        "install them. Set PYTHON_BIN to a prepared environment. Missing: "
        + "; ".join(missing)
    )
print(f"python_version={sys.version.split()[0]}")
print("dependencies=PASS")
PY

command -v node >/dev/null 2>&1 || fail \
  "node is required by the Phase 6 projection regression tests"

GIT_WORKTREE_MODE=false
if command -v git >/dev/null 2>&1; then
  candidate_git_root="$(git -C "$BOOTSTRAP_ROOT" rev-parse --show-toplevel 2>/dev/null || true)"
  if [[ -n "$candidate_git_root" ]] && \
    [[ "$(cd -- "$candidate_git_root" && pwd -P)" == "$BOOTSTRAP_ROOT" ]]; then
    GIT_WORKTREE_MODE=true
  fi
fi
readonly GIT_WORKTREE_MODE
printf '[%s] source validation mode: %s\n' \
  "$BOOTSTRAP_NAME" "$([[ "$GIT_WORKTREE_MODE" == true ]] && printf git-worktree || printf extracted-tree)"

run_logged python-compile "$PYTHON_EXECUTABLE" -m compileall -q \
  factory_core scripts tests web/backend

CURRENT_STEP="bash-syntax"
printf '[%s] RUN: bash-syntax\n' "$BOOTSTRAP_NAME"
bash_syntax_count=0
if [[ "$GIT_WORKTREE_MODE" == true ]]; then
  while IFS= read -r -d '' shell_file; do
    bash -n -- "$shell_file" 2>&1 | tee -a "$BOOTSTRAP_LOG_DIR/bash-syntax.log"
    bash_syntax_count=$((bash_syntax_count + 1))
  done < <(git ls-files --cached --others --exclude-standard -z -- '*.sh')
else
  while IFS= read -r -d '' shell_file; do
    bash -n -- "$shell_file" 2>&1 | tee -a "$BOOTSTRAP_LOG_DIR/bash-syntax.log"
    bash_syntax_count=$((bash_syntax_count + 1))
  done < <(
    find "$BOOTSTRAP_ROOT" -xdev \
      \( -path "$BOOTSTRAP_ROOT/.git" \
         -o -path "$BOOTSTRAP_ROOT/.venv" \
         -o -path "$BOOTSTRAP_ROOT/node_modules" \
         -o -path "$BOOTSTRAP_ROOT/web/frontend/node_modules" \
         -o -path "$BOOTSTRAP_ROOT/web/frontend/dist" \
         -o -path "$BOOTSTRAP_ROOT/audit_artifacts" \
         -o -name __pycache__ -o -name .pytest_cache \) -prune \
      -o -type f -name '*.sh' -print0 | sort -z
  )
fi
printf 'bash_syntax_ok files=%s\n' "$bash_syntax_count" | \
  tee -a "$BOOTSTRAP_LOG_DIR/bash-syntax.log"
printf '[%s] EXIT: bash-syntax status=0\n' "$BOOTSTRAP_NAME"

if [[ "$GIT_WORKTREE_MODE" == true ]]; then
  run_logged git-diff-check git diff --check
else
  run_logged extracted-source-check "$PYTHON_EXECUTABLE" - "$BOOTSTRAP_ROOT" <<'PY'
from pathlib import Path
import hashlib
import os
import stat
import sys

root = Path(sys.argv[1])
source_suffixes = {
    ".bash", ".cjs", ".css", ".html", ".js", ".json", ".mjs", ".py",
    ".pyi", ".sh", ".sql", ".toml", ".ts", ".tsx", ".vue", ".yaml",
    ".yml",
}
source_basenames = {"Dockerfile", "Makefile"}
# `git diff --check` does not reject whitespace that is unchanged from HEAD.
# This one exact historical source blob contains three such lines.  The digest
# makes the exception fail closed: any byte change restores the strict scan.
baseline_trailing_whitespace = {
    "web/frontend/update_styles.py":
        "b0806b8c85a7a79dab0c6e906d1ce7c94b6b7966cde95c79da3a177927f1654f",
}
pruned_names = {
    ".git", ".pytest_cache", ".venv", "__pycache__", "audit_artifacts",
    "dist", "node_modules",
}
failures = []
scanned = 0
baseline_exemptions = 0
for directory, names, files in os.walk(root, topdown=True, followlinks=False):
    names[:] = sorted(name for name in names if name not in pruned_names)
    for filename in sorted(files):
        path = Path(directory, filename)
        if path.suffix.lower() not in source_suffixes and filename not in source_basenames:
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            continue
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            failures.append(f"{path.relative_to(root)}: non-UTF-8 source: {error}")
            continue
        scanned += 1
        relative = path.relative_to(root).as_posix()
        allow_baseline_trailing = (
            baseline_trailing_whitespace.get(relative) == hashlib.sha256(raw).hexdigest()
        )
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.rstrip(" \t") != line:
                if allow_baseline_trailing:
                    baseline_exemptions += 1
                else:
                    failures.append(f"{relative}:{line_number}: trailing whitespace")
            if (
                line.startswith("<<<<<<< ")
                or line == "======="
                or line.startswith(">>>>>>> ")
            ):
                failures.append(
                    f"{path.relative_to(root)}:{line_number}: conflict marker"
                )
if failures:
    print("\n".join(failures))
    raise SystemExit(f"extracted source check found {len(failures)} problem(s)")
print(f"extracted_source_check=PASS files={scanned}")
print(f"exact_baseline_trailing_whitespace_exemptions={baseline_exemptions}")
PY
fi

run_logged default-off "$PYTHON_EXECUTABLE" - <<'PY'
import os

from factory_core.phase4_shadow_runtime import run_phase4_full_shadow
from factory_core.phase5_shadow_supervisor import run_phase5_full_shadow
from factory_core.phase6_snapshot_grants import run_phase6_snapshot_grants_shadow
from web.backend.config import load_settings

phase4 = run_phase4_full_shadow()
phase5 = run_phase5_full_shadow()
phase6 = run_phase6_snapshot_grants_shadow()
for name, run in (("phase4", phase4), ("phase5", phase5), ("phase6", phase6)):
    assert run.enabled is False, name
    assert run.authoritative is False, name
    assert run.dispatch_performed is False, name

prior_enabled = os.environ.pop("PHASE6_SNAPSHOT_ENABLED", None)
prior_path = os.environ.pop("PHASE6_SNAPSHOT_DB_FILE", None)
try:
    settings = load_settings()
    assert settings.phase6_snapshot_enabled is False
    assert settings.phase6_snapshot_db_file is None
finally:
    if prior_enabled is not None:
        os.environ["PHASE6_SNAPSHOT_ENABLED"] = prior_enabled
    if prior_path is not None:
        os.environ["PHASE6_SNAPSHOT_DB_FILE"] = prior_path

print("phase4_default_off=PASS")
print("phase5_default_off=PASS")
print("phase6_core_default_off=PASS")
print("phase6_web_default_off=PASS")
PY

run_pytest_group() {
  local stem="$1" group="$2" basetemp="$3"
  shift 3
  run_logged "$stem" "$PYTHON_EXECUTABLE" -m pytest -q \
    -p no:cacheprovider -p scripts.bootstrap_pytest_outcomes \
    --basetemp="$basetemp" \
    --junitxml="$BOOTSTRAP_RESULT_DIR/$stem.junit.xml" \
    --phase46-bootstrap-group="$group" \
    --phase46-bootstrap-outcomes="$BOOTSTRAP_RESULT_DIR/$stem.outcomes.json" \
    "$@"
}

run_logged test-count-contract "$PYTHON_EXECUTABLE" -m \
  scripts.bootstrap_test_contract describe

run_pytest_group pytest-phase3 phase3 "$BOOTSTRAP_RUN_DIR/pytest/phase3" \
  tests/test_phase3_artifact_foundation.py \
  tests/test_phase3_artifact_registry_integration.py \
  tests/test_phase3_artifact_registry_shadow.py \
  tests/test_phase3_authority_read.py \
  tests/test_phase3_authority_writer.py \
  tests/test_phase3_packaging_isolation.py \
  tests/test_phase3_pro_authorization_regressions.py \
  tests/test_phase3_pro_major_regressions.py

run_pytest_group pytest-phase45 phase45 "$BOOTSTRAP_RUN_DIR/pytest/phase45" \
  tests/test_phase45_fd_ownership_atomicity.py \
  tests/test_phase45_f1_historical_closure.py \
  tests/test_phase4_shadow_runtime.py \
  tests/test_phase4_durable_operation.py \
  tests/test_phase4_durable_operation_integration.py \
  tests/test_phase5_shadow_supervisor.py \
  tests/test_phase5_pause_policy.py \
  tests/test_phase5_pause_policy_integration.py \
  tests/test_phase4_5_shadow_integration.py

run_pytest_group pytest-phase6-core phase6-core \
  "$BOOTSTRAP_RUN_DIR/pytest/phase6-core" \
  tests/test_phase6_project_snapshot_ui.py \
  tests/test_phase6_snapshot_grants.py

run_pytest_group pytest-phase6-web phase6-web \
  "$BOOTSTRAP_RUN_DIR/pytest/phase6-web" \
  tests/test_phase6_web_integration.py \
  tests/test_phase6_backend_security_regressions.py \
  tests/test_phase8_shadow_isolation.py

run_pytest_group pytest-payload-policy payload-policy \
  "$BOOTSTRAP_RUN_DIR/pytest/payload-policy" \
  tests/test_payload_secret_scan.py

run_logged pytest-summary "$PYTHON_EXECUTABLE" -m \
  scripts.bootstrap_test_contract verify \
  --results-dir "$BOOTSTRAP_RESULT_DIR"

printf '[%s] external logs: %s\n' "$BOOTSTRAP_NAME" "$BOOTSTRAP_LOG_DIR"
