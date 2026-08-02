#!/usr/bin/env bash
#
# Merge the trained LoRA adapter into the base model and (optionally)
# quantize it to GGUF for Ollama / llama.cpp.
#
# Usage:
#   scripts/export_model.sh                          # merge only -> outputs/merged_model/
#   scripts/export_model.sh --export-gguf             # merge + GGUF (Q4_K_M, Q8_0) -> outputs/gguf/
#   scripts/export_model.sh --adapter_path outputs/checkpoint-final --export-gguf
#
# Any extra arguments are passed straight through to
# src/export/merge_and_convert.py (see --help there for the full list).
#
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EXPORT_SCRIPT="$REPO_ROOT/src/export/merge_and_convert.py"

# Make sure this script itself stays executable for future invocations.
chmod +x "${BASH_SOURCE[0]}" 2>/dev/null || true

echo "============================================================"
echo "QWEN 2.5 VOICE ASSISTANT — MODEL EXPORT"
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
echo "Python     : $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false

echo "------------------------------------------------------------"
cd "$REPO_ROOT"

# merge_and_convert.py already prints per-file sizes and usage instructions;
# this wrapper just runs it and reports its own view of what landed on disk.
"$PYTHON_BIN" "$EXPORT_SCRIPT" "$@"
STATUS=$?

echo ""
echo "============================================================"
echo "OUTPUT DIRECTORY LISTING"
echo "============================================================"
if [ -d "$REPO_ROOT/outputs/merged_model" ]; then
    echo ""
    echo "-- outputs/merged_model --"
    du -h "$REPO_ROOT/outputs/merged_model"/* 2>/dev/null | sort -h
fi
if [ -d "$REPO_ROOT/outputs/gguf" ]; then
    echo ""
    echo "-- outputs/gguf --"
    du -h "$REPO_ROOT/outputs/gguf"/* 2>/dev/null | sort -h
fi

exit $STATUS
