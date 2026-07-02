#!/usr/bin/env bash
# Load secrets from GCP Secret Manager
# Usage: source scripts/load_secrets.sh

set -euo pipefail

echo "Loading secrets from GCP Secret Manager..." >&2

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTORY="$(cd "$SCRIPT_DIR/.." && pwd)"

fail() {
    echo "ERROR: $*" >&2
    return 1
}

read_env_key() {
    local file="$1"
    local key="$2"
    local line value
    [[ -f "$file" ]] || return 1
    line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
    [[ -n "$line" ]] || return 1
    value="${line#*=}"
    value="${value%$'\r'}"
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
        value="${value#\"}"
        value="${value%\"}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
        value="${value#\'}"
        value="${value%\'}"
    fi
    printf '%s' "$value"
}

load_non_secret_if_missing() {
    local var_name="$1"
    local value="${!var_name:-}"
    local file
    if [[ -n "$value" ]]; then
        export "$var_name=$value"
        return 0
    fi
    for file in "$FACTORY/.env" "$FACTORY/web/.env"; do
        value="$(read_env_key "$file" "$var_name" || true)"
        if [[ -n "$value" ]]; then
            export "$var_name=$value"
            return 0
        fi
    done
}

resolve_gcloud() {
    if [[ -n "${GCLOUD_BIN:-}" ]]; then
        [[ -x "$GCLOUD_BIN" ]] || fail "GCLOUD_BIN is set but not executable: $GCLOUD_BIN"
        printf '%s' "$GCLOUD_BIN"
        return 0
    fi
    if command -v gcloud >/dev/null 2>&1; then
        command -v gcloud
        return 0
    fi
    if [[ -x "$HOME/google-cloud-sdk/bin/gcloud" ]]; then
        printf '%s' "$HOME/google-cloud-sdk/bin/gcloud"
        return 0
    fi
    if [[ -x "/home/tfisher/google-cloud-sdk/bin/gcloud" ]]; then
        printf '%s' "/home/tfisher/google-cloud-sdk/bin/gcloud"
        return 0
    fi
    fail "gcloud CLI not found; install Google Cloud SDK or set GCLOUD_BIN"
}

load_secret_required() {
    local var_name="$1"
    local secret_name="$2"
    local loaded
    if ! loaded="$("$GCLOUD_BIN_RESOLVED" secrets versions access latest --secret="$secret_name" --project="$GCP_PROJECT_ID" 2>&1)"; then
        echo "$loaded" >&2
        fail "failed to load $var_name from Secret Manager secret '$secret_name' in project '$GCP_PROJECT_ID'"
    fi
    [[ -n "$loaded" ]] || fail "Secret Manager secret '$secret_name' returned an empty value for $var_name"
    export "$var_name=$loaded"
}

load_non_secret_if_missing GCP_PROJECT_ID
[[ -n "${GCP_PROJECT_ID:-}" ]] || fail "GCP_PROJECT_ID is required to load secrets from Secret Manager"

GCLOUD_BIN_RESOLVED="$(resolve_gcloud)"

# Sensitive values are always sourced from Secret Manager, overriding any stale
# local environment value so .env cannot silently become the source of truth.
load_secret_required MINERU_TOKEN mineru-token
load_secret_required GEMINI_API_KEY gemini-api-key
load_secret_required DEEPSEEK_API_KEY deepseek-api-key
load_secret_required JWT_SECRET dashboard-jwt-secret
load_secret_required ADMIN_PASSWORD dashboard-admin-password

echo "Secrets loaded successfully" >&2
