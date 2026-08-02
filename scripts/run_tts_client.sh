#!/usr/bin/env bash
#
# Interactive (or single-shot) voice chat: streams the assistant's reply
# from the inference API and speaks it via ElevenLabs TTS as each
# sentence completes.
#
# Requires the inference API to be running (scripts/run_pipeline.sh --serve)
# and $ELEVENLABS_API_KEY to be set.
#
# Usage:
#   scripts/run_tts_client.sh                          # interactive chat
#   scripts/run_tts_client.sh --message "What's my order status?"
#   scripts/run_tts_client.sh --voice-id <id> --player elevenlabs
#
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLIENT_SCRIPT="$REPO_ROOT/src/voice/client_tts.py"

# Make sure this script itself stays executable for future invocations.
chmod +x "${BASH_SOURCE[0]}" 2>/dev/null || true

# ── Virtual environment ──────────────────────────────────────────────────────
# Resolve the venv's python binary directly instead of `source .venv/*/activate`
# — see scripts/run_training.sh for why (uname-dependent PATH corruption).
if [ -x "$REPO_ROOT/.venv/Scripts/python.exe" ]; then
    PYTHON_BIN="$REPO_ROOT/.venv/Scripts/python.exe"
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
else
    echo "WARNING: no .venv found at $REPO_ROOT/.venv — using system Python." >&2
    PYTHON_BIN="$(command -v python)"
fi

export VIRTUAL_ENV="$REPO_ROOT/.venv"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

if [ -z "${ELEVENLABS_API_KEY:-}" ]; then
    echo "WARNING: ELEVENLABS_API_KEY is not set — client_tts.py will exit immediately." >&2
fi

cd "$REPO_ROOT"
"$PYTHON_BIN" "$CLIENT_SCRIPT" "$@"
