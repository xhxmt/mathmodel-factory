#!/usr/bin/env bash
set -euo pipefail

# Compatibility launcher. Authoritative orchestration for migrated projects is
# implemented in factory_core; the frozen shell runner is selected only for
# projects that have not been explicitly migrated or were explicitly rolled
# back.
CODE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTORY="${FACTORY:-$CODE_ROOT}"
export FACTORY
export PYTHONPATH="$CODE_ROOT${PYTHONPATH:+:$PYTHONPATH}"

exec python3 -m factory_core.cli compat "$@"
