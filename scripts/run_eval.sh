#!/usr/bin/env bash
#
# Run the fixed 20-case benchmark against the voice assistant and report
# response-length, handoff precision/recall, and latency/throughput
# metrics. See src/eval/evaluate.py --help for CLI overrides.
#
# Usage:
#   scripts/run_eval.sh
#   scripts/run_eval.sh --adapter_path outputs/checkpoint-final
#
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EVAL_SCRIPT="$REPO_ROOT/src/eval/evaluate.py"

chmod +x "${BASH_SOURCE[0]}" 2>/dev/null || true

echo "============================================================"
echo "QWEN 2.5 VOICE ASSISTANT — EVALUATION RUNNER"
echo "============================================================"

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
export TOKENIZERS_PARALLELISM=false

echo "Python     : $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
echo "------------------------------------------------------------"
cd "$REPO_ROOT"

"$PYTHON_BIN" "$EVAL_SCRIPT" "$@"
