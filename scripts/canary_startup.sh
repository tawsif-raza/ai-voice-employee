#!/usr/bin/env bash
# ============================================================================
# Canary Startup & Verification Script (scripts/canary_startup.sh)
# ============================================================================
# Usage:
#   ./scripts/canary_startup.sh [PORT] [HOST]
# ============================================================================

set -euo pipefail

PORT="${1:-${PORT:-8000}}"
HOST="${2:-localhost}"
BASE_URL="http://${HOST}:${PORT}"

echo "========================================================================"
echo "         AI Voice Agent — Canary Deployment Verification"
echo "========================================================================"
echo "Target Base URL: ${BASE_URL}"
echo "Current Persistence Mode: ${PERSISTENCE_MODE:-dev}"
echo "Current Auth Mode: ${AUTH_MODE:-dev}"
echo "Current LLM Provider: ${LLM_PROVIDER:-fallback}"
echo "------------------------------------------------------------------------"

# 1. Environment Configuration Check
echo -n "[1/4] Checking required environment configuration... "
if [[ "${AUTH_MODE:-dev}" == "production" || "${AUTH_MODE:-dev}" == "oidc" ]]; then
    if [[ -z "${OIDC_ISSUER_URL:-}" || -z "${OIDC_AUDIENCE:-}" || -z "${OIDC_JWKS_URL:-}" ]]; then
        echo "FAILED!"
        echo "Error: AUTH_MODE=production requires OIDC_ISSUER_URL, OIDC_AUDIENCE, and OIDC_JWKS_URL" >&2
        exit 1
    fi
fi

if [[ "${PERSISTENCE_MODE:-dev}" == "production" ]]; then
    if [[ -z "${DATABASE_URL:-}" ]]; then
        echo "FAILED!"
        echo "Error: PERSISTENCE_MODE=production requires DATABASE_URL" >&2
        exit 1
    fi
fi
echo "OK"

# 2. Liveness Check (/health)
echo -n "[2/4] Verifying liveness endpoint (/health)... "
HEALTH_RESP=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/health" || echo "000")
if [[ "${HEALTH_RESP}" != "200" ]]; then
    echo "FAILED (HTTP ${HEALTH_RESP})" >&2
    exit 1
fi
echo "OK (HTTP 200)"

# 3. Voice Provider Health Check (/health/voice)
echo -n "[3/4] Verifying voice provider status (/health/voice)... "
VOICE_RESP=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/health/voice" || echo "000")
if [[ "${VOICE_RESP}" != "200" ]]; then
    echo "FAILED (HTTP ${VOICE_RESP})" >&2
    exit 1
fi
echo "OK (HTTP 200)"

# 4. Readiness Check (/ready)
echo -n "[4/4] Verifying readiness endpoint (/ready)... "
READY_RESP=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/ready" || echo "000")
if [[ "${READY_RESP}" != "200" ]]; then
    echo "FAILED (HTTP ${READY_RESP})" >&2
    exit 1
fi
echo "OK (HTTP 200)"

echo "------------------------------------------------------------------------"
echo "Canary verification SUCCEEDED: Service is healthy, ready, and operational."
echo "========================================================================"
exit 0