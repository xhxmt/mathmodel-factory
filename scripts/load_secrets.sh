#!/usr/bin/env bash
# Load secrets from GCP Secret Manager
# Usage: source scripts/load_secrets.sh

set -euo pipefail

echo "Loading secrets from GCP Secret Manager..." >&2

load_secret_if_missing() {
    local var_name="$1"
    local secret_name="$2"
    local current="${!var_name:-}"
    if [[ -n "$current" ]]; then
        export "$var_name=$current"
        return 0
    fi
    local loaded
    loaded="$(gcloud secrets versions access latest --secret="$secret_name" 2>/dev/null || echo "")"
    if [[ -n "$loaded" ]]; then
        export "$var_name=$loaded"
    fi
}

# Load main factory secrets
load_secret_if_missing MINERU_TOKEN mineru-token
load_secret_if_missing GEMINI_API_KEY gemini-api-key
load_secret_if_missing DEEPSEEK_API_KEY deepseek-api-key

# Load web dashboard secrets
load_secret_if_missing JWT_SECRET dashboard-jwt-secret
load_secret_if_missing ADMIN_PASSWORD dashboard-admin-password

# Verify critical secrets are loaded
if [[ -z "${MINERU_TOKEN:-}" ]]; then
    echo "Warning: MINERU_TOKEN not loaded from Secret Manager" >&2
fi

if [[ -z "${JWT_SECRET:-}" ]]; then
    echo "Warning: JWT_SECRET not loaded from Secret Manager" >&2
fi

echo "Secrets loaded successfully" >&2
