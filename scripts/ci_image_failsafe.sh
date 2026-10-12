#!/usr/bin/env bash
# Production-image fail-safe checks (docs/MASTER_PROJECT_PLAN.md H4).
#
# Starts the image's real entrypoint with missing or forbidden production
# configuration and asserts that every case refuses to start: non-zero exit,
# within the timeout (a process still running at the timeout means it is
# serving with a broken configuration -- that is a failure), and with the
# expected error in its output.
#
# Usage (CI):   scripts/ci_image_failsafe.sh ai-voice-agent:ci
# The container runner can be overridden for local dry runs of this script's
# logic, e.g. CONTAINER_RUNNER="./fake-docker-run" -- it is invoked as
#   $CONTAINER_RUNNER -e KEY=VALUE ... <image>
set -uo pipefail

IMAGE="${1:?usage: ci_image_failsafe.sh <image>}"
RUNNER="${CONTAINER_RUNNER:-docker run --rm --network host}"
TIMEOUT_SECONDS="${FAILSAFE_TIMEOUT_SECONDS:-60}"

OIDC=(-e OIDC_ISSUER_URL=https://issuer.example.test/ -e OIDC_AUDIENCE=ai-voice-agent
      -e OIDC_JWKS_URL=https://issuer.example.test/.well-known/jwks.json)
PROD=(-e APP_ENV=production -e AUTH_MODE=production "${OIDC[@]}")

failures=0

expect_refusal() {
    local name="$1" pattern="$2"
    shift 2
    local output status
    output="$(timeout "$TIMEOUT_SECONDS" $RUNNER "$@" "$IMAGE" 2>&1)"
    status=$?
    if [[ $status -eq 124 ]]; then
        echo "FAIL  $name -- still running after ${TIMEOUT_SECONDS}s (served with broken config)"
        failures=$((failures + 1))
    elif [[ $status -eq 0 ]]; then
        echo "FAIL  $name -- exited 0"
        failures=$((failures + 1))
    elif ! grep -qE "$pattern" <<<"$output"; then
        echo "FAIL  $name -- exit $status but output lacks /$pattern/"
        echo "$output" | tail -n 15 | sed 's/^/        /'
        failures=$((failures + 1))
    else
        echo "PASS  $name (exit $status)"
    fi
}

expect_refusal "no configuration at all (APP_ENV unset = production)" \
    "SecurityPostureError.*APP_ENV=production|AUTH_MODE='dev'"
expect_refusal "dev auth outside dev" "SecurityPostureError" \
    -e APP_ENV=production -e AUTH_MODE=dev -e DEV_AUTH_ENABLED=true
expect_refusal "unrecognised APP_ENV" "not recognised" \
    -e APP_ENV=prod -e AUTH_MODE=production "${OIDC[@]}"
expect_refusal "production auth without OIDC settings" "AuthConfigurationError" \
    -e APP_ENV=production -e AUTH_MODE=production
expect_refusal "mock voice services outside dev" "VOICE_MOCK_SERVICES" \
    -e APP_ENV=staging -e AUTH_MODE=production "${OIDC[@]}" -e VOICE_MOCK_SERVICES=true
expect_refusal "telephony mock PIN outside dev" "TELEPHONY_MOCK_PIN" \
    "${PROD[@]}" -e TELEPHONY_MOCK_PIN=1234
# LLM keys (fake) are supplied so startup gets past provider selection and
# genuinely fails on the database, not on the missing local-model stack.
expect_refusal "production persistence with unreachable database" "DatabaseUnavailableError" \
    "${PROD[@]}" -e PERSISTENCE_MODE=production \
    -e DATABASE_URL=postgresql://nobody:nothing@127.0.0.1:1/none \
    -e GEMINI_API_KEY=AIzaFAILSAFE_NOT_REAL -e GROQ_API_KEY=gsk_FAILSAFE_NOT_REAL
expect_refusal "no LLM credentials (local model stack is not in this image)" "ModuleNotFoundError|ImportError" \
    "${PROD[@]}"

if [[ $failures -gt 0 ]]; then
    echo "RESULT: $failures fail-safe check(s) FAILED"
    exit 1
fi
echo "RESULT: ALL PASS"
