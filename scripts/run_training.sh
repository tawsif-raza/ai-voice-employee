#!/usr/bin/env bash
#
# Sets up the environment and runs src/training/train.py.
#
# Usage:
#   scripts/run_training.sh              # full run, per src/training/config.py
#   scripts/run_training.sh --dry-run    # pipeline smoke test (10 steps, small output dir)
#
# Env vars:
#   REQUIRE_GPU=true   Exit instead of falling back to CPU when no GPU is found.
#
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TRAIN_DIR="$REPO_ROOT/src/training"
LOG_DIR="$REPO_ROOT/outputs"
LOG_FILE="$LOG_DIR/training.log"

# Make sure this script itself stays executable for future `./scripts/run_training.sh` invocations.
chmod +x "${BASH_SOURCE[0]}" 2>/dev/null || true

mkdir -p "$LOG_DIR"

echo "============================================================"
echo "QWEN 2.5 VOICE ASSISTANT — TRAINING RUNNER"
echo "============================================================"

# ── Parse arguments ───────────────────────────────────────────────────────────
DRY_RUN=false
EXTRA_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

# ── Virtual environment ──────────────────────────────────────────────────────
# We locate the venv's python binary directly rather than `source .venv/*/activate`.
# The activate script's OS-detection depends on `uname`, which isn't guaranteed
# to be on PATH in every shell this script runs under; when it's missing, the
# activate script silently falls back to an untranslated Windows path (e.g.
# "D:\...\Scripts") and splices it into the colon-delimited PATH, corrupting it
# (the drive-letter colon splits into a bogus extra PATH entry). Resolving the
# interpreter path ourselves sidesteps that entirely.
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

# ── GPU / CUDA detection ──────────────────────────────────────────────────────
echo ""
echo "Checking GPU availability..."

GPU_FOUND=false

if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    GPU_FOUND=true

    GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)"
    DRIVER_VERSION="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)"
    VRAM_TOTAL="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader | head -n1)"
    VRAM_FREE="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader | head -n1)"
    CUDA_VERSION="$(nvidia-smi | grep -oE 'CUDA Version: [0-9]+\.[0-9]+' | head -n1 | cut -d' ' -f3)"

    echo "GPU model  : $GPU_NAME"
    echo "Driver     : $DRIVER_VERSION"
    echo "CUDA       : ${CUDA_VERSION:-unknown}"
    echo "VRAM total : $VRAM_TOTAL"
    echo "VRAM free  : $VRAM_FREE"

    export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
elif "$PYTHON_BIN" -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
    GPU_FOUND=true
    echo "GPU        : CUDA device detected via torch (nvidia-smi unavailable)"
    "$PYTHON_BIN" -c "
import torch
print(f'GPU model  : {torch.cuda.get_device_name(0)}')
print(f'CUDA       : {torch.version.cuda}')
total = torch.cuda.get_device_properties(0).total_memory / 1e9
print(f'VRAM total : {total:.1f} GB')
"
fi

if [ "$GPU_FOUND" = false ]; then
    echo "WARNING: no GPU detected." >&2
    if [ "${REQUIRE_GPU:-false}" = true ]; then
        echo "ERROR: REQUIRE_GPU=true and no GPU is available — aborting." >&2
        exit 1
    fi
    echo "         Falling back to CPU. Training will be slow — use --dry-run to smoke-test first." >&2
fi

# ── Build the training command ────────────────────────────────────────────────
TRAIN_ARGS=("${EXTRA_ARGS[@]}")

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "Mode       : DRY RUN (--max_steps 10, isolated output dir)"
    TRAIN_ARGS+=(--max_steps 10 --output_dir "$REPO_ROOT/outputs/dry-run")
else
    echo ""
    echo "Mode       : FULL RUN (per src/training/config.py)"
fi

# ── Run training ──────────────────────────────────────────────────────────────
# Run from the repo root so the data paths in config.py, which are relative to
# the repo root (e.g. data/processed/train_final.json), resolve correctly.
# Python itself prepends train.py's own directory to sys.path, so its bare
# `import config` resolves regardless of cwd.
echo ""
echo "Launching train.py ${TRAIN_ARGS[*]}"
echo "Log file   : $LOG_FILE"
echo "------------------------------------------------------------"
cd "$REPO_ROOT"

set -o pipefail
"$PYTHON_BIN" "$TRAIN_DIR/train.py" "${TRAIN_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
