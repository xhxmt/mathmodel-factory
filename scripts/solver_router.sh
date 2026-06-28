#!/usr/bin/env bash
# Solver Router - decides whether to run solver locally or on Cloud Run
# Usage: solver_router.sh --type python --max-time 600 script.py

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTORY="$(cd "$SCRIPT_DIR/.." && pwd)"

# Configuration
USE_CLOUD="${USE_CLOUD_SOLVER:-false}"
CLOUD_THRESHOLD_TIME="${CLOUD_THRESHOLD_TIME:-300}"  # Use cloud for jobs > 5 min
CLOUD_SOLVER_TYPES="${CLOUD_SOLVER_TYPES:-python,julia,matlab,R}"

# Parse arguments to extract max-time
MAX_TIME=1800
SOLVER_TYPE=""
prev_arg=""

for arg in "$@"; do
    if [[ "$prev_arg" == "--max-time" ]]; then
        MAX_TIME="$arg"
    fi
    if [[ "$prev_arg" == "--type" ]]; then
        SOLVER_TYPE="$arg"
    fi
    prev_arg="$arg"
done

# Decision logic
use_cloud=false

# Check if cloud solver is globally enabled
if [[ "$USE_CLOUD" == "true" ]]; then
    # Check for fallback marker (set by cloud_solver_monitor.py when health checks fail)
    FALLBACK_MARKER="$FACTORY/run_state/cloud_solver_fallback.marker"
    if [[ -f "$FALLBACK_MARKER" ]]; then
        echo "[solver_router] Cloud Solver fallback active - routing to local" >&2
        use_cloud=false
    else
        # Check if this solver type is supported on cloud
        if [[ ",$CLOUD_SOLVER_TYPES," == *",$SOLVER_TYPE,"* ]]; then
            # Check if job is long enough to warrant cloud execution
            if (( MAX_TIME >= CLOUD_THRESHOLD_TIME )); then
                use_cloud=true
            fi
        fi
    fi
fi

if [[ "$use_cloud" == "true" ]]; then
    echo "[solver_router] Routing to Cloud Run (max_time=${MAX_TIME}s)" >&2
    CLOUD_CLIENT="${CLOUD_SOLVER_CLIENT:-$SCRIPT_DIR/gcp_solver_client.sh}"
    exec "$CLOUD_CLIENT" "$@"
else
    echo "[solver_router] Routing to local solver" >&2
    exec "$FACTORY/solver_submit.sh" "$@"
fi
