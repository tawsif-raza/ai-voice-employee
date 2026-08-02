#!/usr/bin/env bash
#
# Interactive terminal chat session against the voice assistant.
# Streams tokens as they're generated and shows [HANDOFF TRIGGERED]
# when the model's response signals a human handoff.
#
# Usage:
#   scripts/chat.sh                              # auto-resolve latest checkpoint
#   scripts/chat.sh --adapter_path outputs/checkpoint-final
#   scripts/chat.sh --no-merge --temperature 0.5
#
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PREDICT_SCRIPT="$REPO_ROOT/src/inference/predict.py"

# Make sure this script itself stays executable for future `./scripts/chat.sh` invocations.
chmod +x "${BASH_SOURCE[0]}" 2>/dev/null || true

echo "============================================================"
echo "QWEN 2.5 VOICE ASSISTANT — CHAT"
echo "============================================================"

# ── Virtual environment ──────────────────────────────────────────────────────
# Resolve the venv's python binary directly instead of `source .venv/*/activate`.
# The activate script's OS-detection depends on `uname`, which isn't
# guaranteed to be on PATH in every shell this runs under; when it's missing,
# activate silently falls back to an untranslated Windows path and corrupts
# PATH (see scripts/run_training.sh for the full explanation).
if [ -x "$REPO_ROOT/.venv/Scripts/python.exe" ]; then
    PYTHON_BIN="$REPO_ROOT/.venv/Scripts/python.exe"
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
else
    echo "WARNING: no .venv found at $REPO_ROOT/.venv — using system Python." >&2
    PYTHON_BIN="$(command -v python)"
fi

export VIRTUAL_ENV="$REPO_ROOT/.venv"
echo "Python     : $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

# ── Environment variables ────────────────────────────────────────────────────
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false

echo ""
echo "Checking GPU availability..."
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    echo "GPU        : $(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)"
    export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
elif "$PYTHON_BIN" -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
    echo "GPU        : CUDA device detected via torch"
else
    echo "GPU        : none detected — running on CPU (responses will be slower)."
fi

echo "------------------------------------------------------------"
cd "$REPO_ROOT"
exec "$PYTHON_BIN" "$PREDICT_SCRIPT" "$@"
