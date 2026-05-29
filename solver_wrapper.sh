#!/usr/bin/env bash
# solver_wrapper.sh — internal helper for solver_submit.sh.
#
# Runs a command, captures its exit code to a known file, then exits
# with that same code. Used so solver_submit.sh can spawn the work via
# a single `nohup wrapper &` (giving $! a stable pid that survives the
# launcher's exit) while still recording the exit code for --status.
#
# Usage: solver_wrapper.sh <exit_code_file> <cmd> [args...]

set -uo pipefail

EXIT_FILE="${1:?usage: solver_wrapper.sh <exit_code_file> <cmd> [args...]}"
shift

"$@"
rc=$?
echo "$rc" > "$EXIT_FILE"
exit "$rc"
