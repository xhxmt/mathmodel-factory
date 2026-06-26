#!/usr/bin/env bash
# state_manager.sh — Unified state management for Paper Factory projects
#
# ⚠️  PARTIALLY INTEGRATED (as of 2026-06-20).
#     run_paper.sh now SOURCES this file and DUAL-WRITES .state.json at key state
#     transitions (state_init at startup; progress.last_completed_step on every
#     _set_checkpoint_step call). HOWEVER .state.json is still only a MIRROR — the
#     authoritative project state remains file-state inference (infer_step) plus
#     the dispersed markers: checkpoint.md, .heartbeat, .runner.lock, .paused,
#     .killed, .review_state.json. No reader (infer_step / launch_agents /
#     dashboard) consumes .state.json yet. Therefore:
#       - DO NOT yet treat .state.json as the source of truth.
#     Migrating readers onto it (so the markers can retire) is the remaining
#     higher-risk work. launch_agents.sh does NOT source this file.
#
# Provides a single-file-of-truth (.state.json) to replace dispersed state
# across checkpoint.md, marker files, heartbeat, lock files, etc.
#
# Usage:
#   source scripts/state_manager.sh
#   state_init <project_dir>
#   state_read <project_dir> <key>
#   state_update <project_dir> <key> <value>
#   state_sync_from_legacy <project_dir>

set -euo pipefail

STATE_FILE=".state.json"
STATE_VERSION="1.0"

# ═══════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════

_state_file_path() {
    echo "$1/$STATE_FILE"
}

_ensure_jq() {
    if ! command -v jq &>/dev/null; then
        echo "ERROR: jq is required for state management" >&2
        echo "Install with: sudo apt-get install jq  # or brew install jq" >&2
        return 1
    fi
}

_file_mtime() {
    stat -c %Y "$1" 2>/dev/null || echo 0
}

_now() {
    date +%s
}

# ═══════════════════════════════════════════════════════════════════════
# Core Functions
# ═══════════════════════════════════════════════════════════════════════

# Initialize a new .state.json file with default structure
state_init() {
    local proj_dir="$1"
    local state_file
    state_file=$(_state_file_path "$proj_dir")

    _ensure_jq || return 1

    # Don't overwrite existing state
    if [[ -f "$state_file" ]]; then
        echo "State file already exists: $state_file" >&2
        return 0
    fi

    local base_name
    base_name=$(basename "$proj_dir")

    local now
    now=$(_now)

    # Detect mode (modeling vs social-science)
    local mode="unknown"
    if [[ -d "$proj_dir/problem" ]]; then
        mode="modeling"
    elif [[ -f "$proj_dir/project_brief.md" ]]; then
        mode="social_science"
    fi

    # Create initial structure
    cat > "$state_file" <<EOF
{
  "version": "$STATE_VERSION",
  "project": {
    "base_name": "$base_name",
    "mode": "$mode",
    "created_at": $now,
    "updated_at": $now
  },
  "progress": {
    "current_step": -1,
    "last_completed_step": -1,
    "checkpoint_source": "init",
    "step_timeline": {}
  },
  "runner": {
    "status": "IDLE",
    "pid": null,
    "heartbeat": 0,
    "lock_acquired_at": 0,
    "activity_check": {
      "last_log_line": 0,
      "hung_threshold": 0
    }
  },
  "markers": {
    "paused": false,
    "killed": false,
    "gate2_reopen_pending": false
  },
  "consultation": {
    "enabled": false,
    "pending_gate": null,
    "request_created_at": 0,
    "answer_consumed": false
  },
  "review": {
    "cycle_active": false,
    "resume_step": null,
    "requested_at": null
  },
  "solver_jobs": {
    "active_count": 0,
    "completed_count": 0,
    "failed_count": 0,
    "latest_job_id": null
  }
}
EOF

    echo "Initialized state: $state_file"
}

# Read a field from .state.json
# Usage: state_read <project_dir> <key>
# Example: state_read "$P" "progress.current_step"
state_read() {
    local proj_dir="$1"
    local key="$2"
    local state_file
    state_file=$(_state_file_path "$proj_dir")

    _ensure_jq || return 1

    if [[ ! -f "$state_file" ]]; then
        echo "null"
        return 0
    fi

    jq -r ".$key // null" "$state_file" 2>/dev/null || echo "null"
}

# Update a field in .state.json atomically
# Usage: state_update <project_dir> <key> <value>
# Example: state_update "$P" "progress.last_completed_step" 5
state_update() {
    local proj_dir="$1"
    local key="$2"
    local value="$3"
    local state_file
    state_file=$(_state_file_path "$proj_dir")

    _ensure_jq || return 1

    # Initialize if doesn't exist
    if [[ ! -f "$state_file" ]]; then
        state_init "$proj_dir" || return 1
    fi

    local tmp_file="${state_file}.tmp.$$"
    local now
    now=$(_now)

    # Prepare value for jq (handle strings vs numbers vs booleans)
    local jq_value
    case "$value" in
        true|false|null)
            jq_value="$value"
            ;;
        ''|*[!0-9]*)
            # String (including empty)
            jq_value=$(jq -n --arg v "$value" '$v')
            ;;
        *)
            # Number
            jq_value="$value"
            ;;
    esac

    # Atomic update: read -> modify -> write to temp -> rename
    jq --argjson val "$jq_value" \
       --argjson ts "$now" \
       ".$key = \$val | .project.updated_at = \$ts" \
       "$state_file" > "$tmp_file" 2>/dev/null || {
        rm -f "$tmp_file"
        echo "ERROR: Failed to update $key in $state_file" >&2
        return 1
    }

    mv "$tmp_file" "$state_file"
}

# Bulk update multiple fields atomically
# Usage: state_update_multi <project_dir> <key1> <val1> <key2> <val2> ...
state_update_multi() {
    local proj_dir="$1"; shift
    local state_file
    state_file=$(_state_file_path "$proj_dir")

    _ensure_jq || return 1

    [[ -f "$state_file" ]] || state_init "$proj_dir" || return 1

    local tmp_file="${state_file}.tmp.$$"
    local now
    now=$(_now)

    # Build jq expression
    local jq_expr=". | .project.updated_at = $now"
    while (( $# >= 2 )); do
        local key="$1"
        local value="$2"
        shift 2

        local jq_value
        case "$value" in
            true|false|null) jq_value="$value" ;;
            ''|*[!0-9]*) jq_value=$(jq -n --arg v "$value" '$v') ;;
            *) jq_value="$value" ;;
        esac

        jq_expr="$jq_expr | .$key = $jq_value"
    done

    jq "$jq_expr" "$state_file" > "$tmp_file" 2>/dev/null || {
        rm -f "$tmp_file"
        echo "ERROR: Failed multi-update in $state_file" >&2
        return 1
    }

    mv "$tmp_file" "$state_file"
}

# Sync state from legacy file-based markers
# Reconstructs .state.json from checkpoint.md, marker files, etc.
state_sync_from_legacy() {
    local proj_dir="$1"
    local state_file
    state_file=$(_state_file_path "$proj_dir")

    _ensure_jq || return 1

    # Initialize if doesn't exist
    [[ -f "$state_file" ]] || state_init "$proj_dir" || return 1

    local now
    now=$(_now)

    # Read checkpoint.md for step info
    local cp_step=-1
    if [[ -f "$proj_dir/checkpoint.md" ]]; then
        cp_step=$(grep -oP "Last completed step:\s*\K-?\d+" "$proj_dir/checkpoint.md" 2>/dev/null | head -1 || echo "-1")
    fi

    # Detect runner status from marker files and PID
    local status="IDLE"
    local pid=""

    if [[ -f "$proj_dir/.paused" ]]; then
        status="PAUSED"
    elif [[ -f "$proj_dir/.killed" ]]; then
        status="KILLED"
    elif [[ -f "$proj_dir/.heartbeat" ]]; then
        local hb_content
        hb_content=$(cat "$proj_dir/.heartbeat" 2>/dev/null || echo "")
        case "$hb_content" in
            ACTIVE*) status="ACTIVE" ;;
            STUCK*) status="STUCK" ;;
            CONSULT*) status="CONSULT" ;;
            KILLED*) status="KILLED" ;;
            *) status="UNKNOWN" ;;
        esac
    fi

    # Read PID
    if [[ -f "$proj_dir/.runner.pid" ]]; then
        pid=$(cat "$proj_dir/.runner.pid" 2>/dev/null || echo "")
        # Validate PID is alive
        if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
            pid=""
            status="STOPPED"
        fi
    fi

    # Read heartbeat timestamp
    local hb_time=0
    if [[ -f "$proj_dir/.heartbeat" ]]; then
        hb_time=$(_file_mtime "$proj_dir/.heartbeat")
    fi

    # Check consultation state
    local consult_enabled=false
    local consult_gate=""
    if [[ -f "$proj_dir/consultation/enabled" ]] || [[ -d "$proj_dir/consultation" ]]; then
        consult_enabled=true
        # Check for pending gates
        for gate_req in "$proj_dir/consultation"/*_request.md; do
            [[ -f "$gate_req" ]] || continue
            local gate
            gate=$(basename "$gate_req" | sed 's/_request\.md$//')
            consult_gate="$gate"
            break
        done
    fi

    # Check review state
    local review_active=false
    local resume_step=null
    if [[ -f "$proj_dir/.review_state.json" ]]; then
        review_active=$(jq -r '.cycle_active // false' "$proj_dir/.review_state.json" 2>/dev/null || echo "false")
        resume_step=$(jq -r '.resume_step // null' "$proj_dir/.review_state.json" 2>/dev/null || echo "null")
    fi

    # Check gate2 reopen
    local gate2_reopen=false
    [[ -f "$proj_dir/.gate2_reopen_to_revision" ]] && gate2_reopen=true

    # Update all fields atomically
    local tmp_file="${state_file}.tmp.$$"
    jq --argjson cp_step "$cp_step" \
       --arg status "$status" \
       --arg pid "$pid" \
       --argjson hb_time "$hb_time" \
       --argjson consult_enabled "$consult_enabled" \
       --arg consult_gate "$consult_gate" \
       --argjson review_active "$review_active" \
       --argjson resume_step "$resume_step" \
       --argjson gate2_reopen "$gate2_reopen" \
       --argjson ts "$now" \
       '
       .progress.last_completed_step = $cp_step |
       .progress.current_step = ($cp_step + 1) |
       .progress.checkpoint_source = "legacy_sync" |
       .runner.status = $status |
       .runner.pid = (if $pid != "" then ($pid | tonumber) else null end) |
       .runner.heartbeat = $hb_time |
       .consultation.enabled = $consult_enabled |
       .consultation.pending_gate = (if $consult_gate != "" then $consult_gate else null end) |
       .review.cycle_active = $review_active |
       .review.resume_step = $resume_step |
       .markers.gate2_reopen_pending = $gate2_reopen |
       .project.updated_at = $ts
       ' "$state_file" > "$tmp_file" || {
        rm -f "$tmp_file"
        echo "ERROR: Failed to sync from legacy" >&2
        return 1
    }

    mv "$tmp_file" "$state_file"
    echo "Synced state from legacy files (step=$cp_step, status=$status)"
}

# Export functions for sourcing
export -f state_init state_read state_update state_update_multi state_sync_from_legacy
export -f _state_file_path _ensure_jq _file_mtime _now
