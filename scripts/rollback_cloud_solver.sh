#!/usr/bin/env bash
# Deploy a specific immutable Solver API digest as a new private revision.

set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-level-night-476302-k0}"
REGION="${GCP_REGION:-europe-west4}"
SERVICE_NAME="${GCP_SOLVER_SERVICE:-solver-api}"
REPOSITORY="${GCP_SOLVER_REPOSITORY:-solver-images}"
DIGEST=""
EXECUTE=false

usage() {
    echo "Usage: $0 --digest sha256:<64-hex> [--project ID] [--region REGION] [--service NAME] [--execute]" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --digest) DIGEST="${2:-}"; shift 2 ;;
        --project) PROJECT_ID="${2:-}"; shift 2 ;;
        --region) REGION="${2:-}"; shift 2 ;;
        --service) SERVICE_NAME="${2:-}"; shift 2 ;;
        --repository) REPOSITORY="${2:-}"; shift 2 ;;
        --execute) EXECUTE=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

if [[ ! "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "Error: --digest must be sha256 followed by exactly 64 lowercase hex characters" >&2
    exit 2
fi
for value in "$PROJECT_ID" "$REGION" "$SERVICE_NAME" "$REPOSITORY"; do
    if [[ ! "$value" =~ ^[a-z][a-z0-9-]{1,62}$ ]]; then
        echo "Error: project, region, service, and repository names must use safe lowercase identifiers" >&2
        exit 2
    fi
done

GCLOUD_BIN_RESOLVED="${GCLOUD_BIN:-}"
if [[ -z "$GCLOUD_BIN_RESOLVED" ]]; then
    GCLOUD_BIN_RESOLVED="$(command -v gcloud || true)"
fi
if [[ -z "$GCLOUD_BIN_RESOLVED" && -x "$HOME/google-cloud-sdk/bin/gcloud" ]]; then
    GCLOUD_BIN_RESOLVED="$HOME/google-cloud-sdk/bin/gcloud"
fi
if [[ -z "$GCLOUD_BIN_RESOLVED" && -x "/home/tfisher/google-cloud-sdk/bin/gcloud" ]]; then
    GCLOUD_BIN_RESOLVED="/home/tfisher/google-cloud-sdk/bin/gcloud"
fi
if [[ -z "$GCLOUD_BIN_RESOLVED" || ! -x "$GCLOUD_BIN_RESOLVED" ]]; then
    echo "Error: gcloud CLI is not available" >&2
    exit 1
fi

IMAGE_REF="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/solver-api@${DIGEST}"
ROLLBACK_COMMAND=(
    "$GCLOUD_BIN_RESOLVED" run deploy "$SERVICE_NAME"
    "--project=$PROJECT_ID"
    "--region=$REGION"
    "--platform=managed"
    "--image=$IMAGE_REF"
    --no-allow-unauthenticated
    --update-env-vars=SOLVER_EXECUTION_ENABLED=false
    --update-labels=rollback=true
)

if [[ "$EXECUTE" != "true" ]]; then
    echo "Dry run; add --execute to deploy this immutable digest:" >&2
    printf '%q ' "${ROLLBACK_COMMAND[@]}"
    printf '\n'
    exit 0
fi

"${ROLLBACK_COMMAND[@]}"
"$GCLOUD_BIN_RESOLVED" run services describe "$SERVICE_NAME" \
    "--project=$PROJECT_ID" \
    "--region=$REGION" \
    --format='value(status.latestReadyRevisionName)'
