#!/usr/bin/env bash
set -euo pipefail
umask 077

# Independent Phase 7+8 exact structured verification entry.  It does not
# mutate or relax the frozen Phase 3-6 bootstrap contract in bootstrap.sh.
readonly BOOTSTRAP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly BOOTSTRAP_NAME="phase78-bootstrap"

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
  "no Python interpreter found; set PYTHON_BIN to a Python >=3.11 environment with project dev+web dependencies"
readonly PYTHON_EXECUTABLE

if [[ -n "${PHASE78_BOOTSTRAP_TMPDIR:-}" ]]; then
  BOOTSTRAP_TEMP_BASE="$PHASE78_BOOTSTRAP_TMPDIR"
else
  [[ -n "${HOME:-}" ]] || fail \
    "HOME is unset; set PHASE78_BOOTSTRAP_TMPDIR to an absolute external directory"
  BOOTSTRAP_TEMP_BASE="${XDG_CACHE_HOME:-$HOME/.cache}/paper_factory/phase78-bootstrap"
fi
[[ "$BOOTSTRAP_TEMP_BASE" == /* ]] || fail \
  "PHASE78_BOOTSTRAP_TMPDIR must be an absolute path"
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
readonly BOOTSTRAP_RESULT_DIR="$BOOTSTRAP_RUN_DIR/structured"
mkdir -p -- "$BOOTSTRAP_LOG_DIR" "$BOOTSTRAP_RESULT_DIR" \
  "$BOOTSTRAP_RUN_DIR/pytest" "$BOOTSTRAP_RUN_DIR/pycache" \
  "$BOOTSTRAP_RUN_DIR/runtime-tmp" "$BOOTSTRAP_RUN_DIR/xdg-cache"

export PYTHONDONTWRITEBYTECODE=1
export PYTHONHASHSEED=0
export PYTHONNOUSERSITE=1
export PYTHONPATH="$BOOTSTRAP_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPYCACHEPREFIX="$BOOTSTRAP_RUN_DIR/pycache"
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export TMPDIR="$BOOTSTRAP_RUN_DIR/runtime-tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
export XDG_CACHE_HOME="$BOOTSTRAP_RUN_DIR/xdg-cache"

RUN_ID="$("$PYTHON_EXECUTABLE" - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
)" || fail "cannot generate bootstrap run identity"
[[ "$RUN_ID" =~ ^[0-9a-f]{32}$ ]] || fail "invalid generated run identity"
readonly RUN_ID

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
  printf '[%s] run_id: %s\n' "$BOOTSTRAP_NAME" "$RUN_ID"
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
printf '[%s] policy: no dependency installation; no network command; external caches only\n' \
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
        "selected Python lacks required Phase 7+8 dev/runtime dependencies; "
        "bootstrap will not install them. Missing: " + "; ".join(missing)
    )
print(f"python_version={sys.version.split()[0]}")
print("dependencies=PASS")
try:
    import PIL
except ImportError:
    print("png_validation=stdlib-png-ihdr-v1")
else:
    print(f"png_validation=Pillow/{getattr(PIL, '__version__', 'unknown')}")
PY

for command_name in pdfinfo pdftoppm pdftotext; do
  command -v "$command_name" >/dev/null 2>&1 || fail \
    "required local PDF tool is unavailable: $command_name"
done

run_logged python-compile "$PYTHON_EXECUTABLE" -m compileall -q \
  factory_core scripts tests web/backend
run_logged bash-syntax bash -n bootstrap_phase78.sh

if command -v git >/dev/null 2>&1 && \
  [[ "$(git -C "$BOOTSTRAP_ROOT" rev-parse --show-toplevel 2>/dev/null || true)" == "$BOOTSTRAP_ROOT" ]]; then
  run_logged git-diff-check git diff --check
else
  printf '[%s] INFO: extracted-tree mode; git-diff-check is not applicable\n' \
    "$BOOTSTRAP_NAME" | tee "$BOOTSTRAP_LOG_DIR/extracted-tree.log"
fi

run_logged test-count-contract "$PYTHON_EXECUTABLE" -m \
  scripts.phase78_test_contract describe

run_group() {
  local group="$1"
  local stem="pytest-phase78-$group"
  run_logged "$stem" "$PYTHON_EXECUTABLE" -m \
    scripts.phase78_test_contract run-group \
    --group "$group" \
    --run-id "$RUN_ID" \
    --results-dir "$BOOTSTRAP_RESULT_DIR" \
    --basetemp "$BOOTSTRAP_RUN_DIR/pytest/$group"
}

run_group unit
run_group runtime
run_group adapters
run_group pdf-cas
run_group e2e

run_logged pytest-summary "$PYTHON_EXECUTABLE" -m \
  scripts.phase78_test_contract verify \
  --results-dir "$BOOTSTRAP_RESULT_DIR" \
  --run-id "$RUN_ID"

printf '[%s] external logs: %s\n' "$BOOTSTRAP_NAME" "$BOOTSTRAP_LOG_DIR"
