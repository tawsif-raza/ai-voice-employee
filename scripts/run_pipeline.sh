#!/usr/bin/env bash
#
# Master pipeline entrypoint. Orchestrates the existing per-phase scripts
# rather than reimplementing them, so each stage still works standalone.
#
# Usage:
#   scripts/run_pipeline.sh --all          # train -> eval -> merge -> serve (foreground)
#   scripts/run_pipeline.sh --train-only   # train -> eval -> merge, then exit
#   scripts/run_pipeline.sh --serve        # just start the inference API
#
# Any arguments after the mode flag are forwarded to
# src/export/merge_and_convert.py, e.g.:
#   scripts/run_pipeline.sh --train-only --export-gguf
#
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

chmod +x "${BASH_SOURCE[0]}" 2>/dev/null || true

usage() {
    echo "Usage: $0 [--all | --train-only | --serve] [extra merge_and_convert.py args]"
}

MODE=""
case "${1:-}" in
    --all)        MODE="all" ;;
    --train-only) MODE="train-only" ;;
    --serve)      MODE="serve" ;;
    -h|--help)    usage; exit 0 ;;
    "")           echo "ERROR: no mode given." >&2; usage; exit 1 ;;
    *)            echo "ERROR: unknown flag '$1'." >&2; usage; exit 1 ;;
esac
shift
MERGE_ARGS=("$@")

echo "============================================================"
echo "QWEN 2.5 VOICE ASSISTANT — PIPELINE ($MODE)"
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
cd "$REPO_ROOT"

# ── Stages ────────────────────────────────────────────────────────────────────

run_training_stage() {
    echo ""
    echo "── [1/3] Training ──────────────────────────────────────────"
    "$SCRIPT_DIR/run_training.sh"
}

run_eval_stage() {
    echo ""
    echo "── [2/3] Evaluation ────────────────────────────────────────"
    if [ -f "$REPO_ROOT/src/eval/evaluate.py" ]; then
        "$PYTHON_BIN" "$REPO_ROOT/src/eval/evaluate.py"
    else
        echo "SKIP: src/eval/evaluate.py not found — evaluation stage not built yet." >&2
    fi
}

run_merge_stage() {
    echo ""
    echo "── [3/3] Merge / Export ────────────────────────────────────"
    "$PYTHON_BIN" "$REPO_ROOT/src/export/merge_and_convert.py" "${MERGE_ARGS[@]}"
}

run_serve_stage() {
    echo ""
    echo "── Serving ──────────────────────────────────────────────────"
    echo "Starting inference API on port ${PORT:-8000}..."
    # exec replaces this shell so the server becomes PID 1 and receives
    # signals directly (matters for `docker stop` / SIGTERM handling).
    exec "$PYTHON_BIN" "$REPO_ROOT/src/api/server.py"
}

# ── Run ───────────────────────────────────────────────────────────────────────

case "$MODE" in
    all)
        run_training_stage
        run_eval_stage
        run_merge_stage
        run_serve_stage
        ;;
    train-only)
        run_training_stage
        run_eval_stage
        run_merge_stage
        echo ""
        echo "Pipeline complete (train -> eval -> merge)."
        ;;
    serve)
        run_serve_stage
        ;;
esac
