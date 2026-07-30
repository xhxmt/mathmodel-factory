#!/usr/bin/env bash
# GCP Solver Client - calls Cloud Run solver API from local environment
# Usage: gcp_solver_client.sh --type python --max-time 600 --script path/to/script.py

set -euo pipefail

# Configuration
PROJECT_ID="${GCP_PROJECT_ID:-level-night-476302-k0}"
REGION="${GCP_REGION:-europe-west4}"
SERVICE_NAME="${GCP_SOLVER_SERVICE:-solver-api}"
BUCKET="${GCP_SOLVER_BUCKET:-${PROJECT_ID}-solver-jobs}"
export CLOUD_SOLVER_IMPERSONATE_SERVICE_ACCOUNT="${CLOUD_SOLVER_IMPERSONATE_SERVICE_ACCOUNT:-solver-invoker@${PROJECT_ID}.iam.gserviceaccount.com}"

resolve_binary() {
    local env_value="$1"
    local name="$2"
    local home_sdk="$HOME/google-cloud-sdk/bin/$name"
    local tfisher_sdk="/home/tfisher/google-cloud-sdk/bin/$name"

    if [[ -n "$env_value" ]]; then
        echo "$env_value"
        return 0
    fi
    if command -v "$name" >/dev/null 2>&1; then
        command -v "$name"
        return 0
    fi
    if [[ -x "$home_sdk" ]]; then
        echo "$home_sdk"
        return 0
    fi
    if [[ -x "$tfisher_sdk" ]]; then
        echo "$tfisher_sdk"
        return 0
    fi
    echo "Error: $name not found" >&2
    exit 1
}

AUTH_HELPER="${CLOUD_SOLVER_AUTH_HELPER:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cloud_solver_auth.py}"
CAPABILITY_MANIFEST="${CLOUD_SOLVER_CAPABILITIES_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/cloud/runtime_capabilities.json}"

# Get Cloud Run service URL
get_service_url() {
    "$GCLOUD_BIN_RESOLVED" run services describe "$SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --format="value(status.url)" 2>/dev/null || {
        echo "Error: Cloud Run service '$SERVICE_NAME' not found in region '$REGION'" >&2
        exit 1
    }
}

# Parse arguments
SOLVER_TYPE=""
MAX_TIME=1800
SCRIPT_PATH=""
JOB_ID=""
WORKING_FILES=()
ENV_VARS=()

while [[ $# -gt 0 ]]; do
    case $1 in
        --type)
            SOLVER_TYPE="$2"
            shift 2
            ;;
        --max-time)
            MAX_TIME="$2"
            shift 2
            ;;
        --script)
            SCRIPT_PATH="$2"
            shift 2
            ;;
        --job-id)
            JOB_ID="$2"
            shift 2
            ;;
        --working-file)
            WORKING_FILES+=("$2")
            shift 2
            ;;
        --env)
            ENV_VARS+=("$2")
            shift 2
            ;;
        *)
            SCRIPT_PATH="$1"
            shift
            ;;
    esac
done

# Validate required arguments
if [[ -z "$SOLVER_TYPE" ]]; then
    echo "Error: --type required (cloud currently supports python)" >&2
    exit 1
fi

if [[ ! -f "$CAPABILITY_MANIFEST" ]] || \
   ! jq -e --arg solver_type "$SOLVER_TYPE" \
       '.runtimes[$solver_type].enabled == true' "$CAPABILITY_MANIFEST" >/dev/null 2>&1; then
    echo "Error: runtime is not available in the Cloud Solver image: $SOLVER_TYPE" >&2
    exit 1
fi

if [[ -z "$SCRIPT_PATH" || ! -f "$SCRIPT_PATH" ]]; then
    echo "Error: script file not found: $SCRIPT_PATH" >&2
    exit 1
fi

if [[ ! -f "$AUTH_HELPER" ]]; then
    echo "Error: shared Cloud Run authentication helper not found" >&2
    exit 1
fi

GCLOUD_BIN_RESOLVED="$(resolve_binary "${GCLOUD_BIN:-}" gcloud)"
GSUTIL_BIN_RESOLVED="$(resolve_binary "${GSUTIL_BIN:-}" gsutil)"
PYTHON_BIN_RESOLVED="$(resolve_binary "${PYTHON_BIN:-}" python3)"

# Generate job ID if not provided
if [[ -z "$JOB_ID" ]]; then
    JOB_ID="job-$(date +%Y%m%d-%H%M%S)-$$"
fi

# Get service URL
SERVICE_URL=$(get_service_url)
echo "Using Cloud Run service: $SERVICE_URL" >&2

get_identity_token() {
    "$PYTHON_BIN_RESOLVED" "$AUTH_HELPER" token --audience "$SERVICE_URL"
}

SCRIPT_NAME=$(basename "$SCRIPT_PATH")
REQUEST_TMP_DIR=$(mktemp -d)
chmod 700 "$REQUEST_TMP_DIR"
trap 'rm -rf -- "$REQUEST_TMP_DIR"' EXIT
WORKING_FILES_PATH="$REQUEST_TMP_DIR/working_files.json"
ENV_VARS_PATH="$REQUEST_TMP_DIR/env_vars.json"
REQUEST_JSON_PATH="$REQUEST_TMP_DIR/request.json"
printf '{}\n' > "$WORKING_FILES_PATH"
printf '{}\n' > "$ENV_VARS_PATH"

# Build working files through files so large inputs never enter argv.
for file in "${WORKING_FILES[@]}"; do
    if [[ -f "$file" ]]; then
        filename=$(basename "$file")
        next_path="$REQUEST_TMP_DIR/working_files.next.json"
        jq --arg k "$filename" --rawfile v "$file" '. + {($k): $v}' \
            "$WORKING_FILES_PATH" > "$next_path"
        mv "$next_path" "$WORKING_FILES_PATH"
    fi
done

# Build env vars without carrying the assembled document in argv.
for env_pair in "${ENV_VARS[@]}"; do
    key="${env_pair%%=*}"
    value="${env_pair#*=}"
    value_path="$REQUEST_TMP_DIR/env_value"
    printf '%s' "$value" > "$value_path"
    next_path="$REQUEST_TMP_DIR/env_vars.next.json"
    jq --arg k "$key" --rawfile v "$value_path" '. + {($k): $v}' \
        "$ENV_VARS_PATH" > "$next_path"
    mv "$next_path" "$ENV_VARS_PATH"
done
unset value

# Build the request on disk so neither jq nor curl receives the body as argv.
jq -n \
    --arg job_id "$JOB_ID" \
    --arg solver_type "$SOLVER_TYPE" \
    --rawfile script_content "$SCRIPT_PATH" \
    --arg script_name "$SCRIPT_NAME" \
    --argjson max_time "$MAX_TIME" \
    --slurpfile working_files "$WORKING_FILES_PATH" \
    --slurpfile env_vars "$ENV_VARS_PATH" \
    '{
        job_id: $job_id,
        solver_type: $solver_type,
        script_content: $script_content,
        script_name: $script_name,
        max_time: $max_time,
        working_files: $working_files[0],
        env_vars: $env_vars[0]
    }' > "$REQUEST_JSON_PATH"

# Submit job
echo "Submitting job $JOB_ID to Cloud Run..." >&2
ID_TOKEN="$(get_identity_token)"
SUBMIT_RESPONSE="$(
    curl -sS -X POST \
        -H "Content-Type: application/json" \
        -H @/dev/fd/3 \
        --data-binary "@$REQUEST_JSON_PATH" \
        "${SERVICE_URL}/solve/${SOLVER_TYPE}" \
        3< <(printf 'Authorization: Bearer %s\n' "$ID_TOKEN")
)"
unset ID_TOKEN

# Check if submission succeeded
if echo "$SUBMIT_RESPONSE" | jq -e '.job_id' > /dev/null 2>&1; then
    echo "Job submitted successfully: $JOB_ID" >&2
else
    echo "Error submitting job:" >&2
    echo "$SUBMIT_RESPONSE" >&2
    exit 1
fi

# Poll for completion
echo "Polling job status..." >&2
while true; do
    ID_TOKEN="$(get_identity_token)"
    STATUS_RESPONSE="$(
        curl -sS \
            -H @/dev/fd/3 \
            "${SERVICE_URL}/jobs/${JOB_ID}/status" \
            3< <(printf 'Authorization: Bearer %s\n' "$ID_TOKEN")
    )"
    unset ID_TOKEN

    STATUS=$(echo "$STATUS_RESPONSE" | jq -r '.status')

    case "$STATUS" in
        completed|failed|timeout)
            echo "Job finished with status: $STATUS" >&2
            break
            ;;
        queued|running)
            echo -n "." >&2
            sleep 5
            ;;
        *)
            echo "Unknown status: $STATUS" >&2
            echo "$STATUS_RESPONSE" >&2
            exit 1
            ;;
    esac
done
echo "" >&2

# Download stdout
STDOUT_URL=$(echo "$STATUS_RESPONSE" | jq -r '.stdout_url')
if [[ -n "$STDOUT_URL" && "$STDOUT_URL" != "null" ]]; then
    STDOUT_LOCAL="${SCRIPT_PATH}.log"
    echo "Downloading stdout to $STDOUT_LOCAL..." >&2
    "$GSUTIL_BIN_RESOLVED" cp "$STDOUT_URL" "$STDOUT_LOCAL" 2>/dev/null || {
        echo "Warning: failed to download stdout" >&2
    }
fi

# Download stderr
STDERR_URL=$(echo "$STATUS_RESPONSE" | jq -r '.stderr_url')
if [[ -n "$STDERR_URL" && "$STDERR_URL" != "null" ]]; then
    STDERR_LOCAL="${SCRIPT_PATH}.err"
    echo "Downloading stderr to $STDERR_LOCAL..." >&2
    "$GSUTIL_BIN_RESOLVED" cp "$STDERR_URL" "$STDERR_LOCAL" 2>/dev/null || {
        echo "Warning: failed to download stderr" >&2
    }
fi

# Download result files
RESULT_FILES=$(echo "$STATUS_RESPONSE" | jq -r '.result_files[]?' 2>/dev/null || true)
if [[ -n "$RESULT_FILES" ]]; then
    RESULT_DOWNLOAD_DIR="${CLOUD_SOLVER_DOWNLOAD_DIR:-${SCRIPT_PATH}.cloud-results}"
    mkdir -p "$RESULT_DOWNLOAD_DIR"
    chmod 700 "$RESULT_DOWNLOAD_DIR"
    echo "Downloading result files to $RESULT_DOWNLOAD_DIR..." >&2
    for url in $RESULT_FILES; do
        relative_path="${url#*/outputs/}"
        if [[ "$relative_path" == "$url" || "$relative_path" == /* || \
              "$relative_path" == *".."* || \
              ! "$relative_path" =~ ^[A-Za-z0-9_][A-Za-z0-9_./-]*$ ]]; then
            echo "Warning: refusing unsafe result path" >&2
            continue
        fi
        local_target="$RESULT_DOWNLOAD_DIR/$relative_path"
        mkdir -p "$(dirname "$local_target")"
        "$GSUTIL_BIN_RESOLVED" cp "$url" "$local_target" 2>/dev/null || {
            echo "Warning: failed to download result artifact" >&2
        }
    done
fi

# Return final status
EXIT_CODE=$(echo "$STATUS_RESPONSE" | jq -r '.exit_code // 1')

if [[ "$STATUS" == "completed" ]]; then
    echo "Job completed successfully" >&2
    exit 0
else
    ERROR_MSG=$(echo "$STATUS_RESPONSE" | jq -r '.error_message // "Unknown error"')
    echo "Job failed: $ERROR_MSG" >&2
    exit "$EXIT_CODE"
fi
