#!/usr/bin/env bash
# Load secrets from GCP Secret Manager
# Usage: source scripts/load_secrets.sh

set -euo pipefail

echo "Loading secrets from GCP Secret Manager..." >&2

# Load main factory secrets
export MINERU_TOKEN=$(gcloud secrets versions access latest --secret=mineru-token 2>/dev/null || echo "")
export GEMINI_API_KEY=$(gcloud secrets versions access latest --secret=gemini-api-key 2>/dev/null || echo "")
export DEEPSEEK_API_KEY=$(gcloud secrets versions access latest --secret=deepseek-api-key 2>/dev/null || echo "")

# Load web dashboard secrets
export JWT_SECRET=$(gcloud secrets versions access latest --secret=dashboard-jwt-secret 2>/dev/null || echo "")
export ADMIN_PASSWORD=$(gcloud secrets versions access latest --secret=dashboard-admin-password 2>/dev/null || echo "")

# Verify critical secrets are loaded
if [[ -z "$MINERU_TOKEN" ]]; then
    echo "Warning: MINERU_TOKEN not loaded from Secret Manager" >&2
fi

if [[ -z "$JWT_SECRET" ]]; then
    echo "Warning: JWT_SECRET not loaded from Secret Manager" >&2
fi

echo "Secrets loaded successfully" >&2
