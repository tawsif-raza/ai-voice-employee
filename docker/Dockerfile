# AI Voice Employee — training/eval/export/serve image.
#
# Base includes torch prebuilt against CUDA 12.1 — requirements.txt
# deliberately does not list torch, so `pip install -r requirements.txt`
# below cannot clobber it with a mismatched wheel.
FROM pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime

WORKDIR /app

# git: some HF downloads / llama.cpp conversion tooling shell out to it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY configs/ configs/
COPY src/ src/
COPY scripts/ scripts/
# Phase 26: alembic.ini + alembic/ were never copied into the image, so
# `alembic upgrade head` (docs/DATABASE.md's own documented migration
# step, and docs/ROLLBACK_PROCEDURE.md's `alembic downgrade -1`) could
# not actually run inside a built container -- undetected until this
# phase's first real image build + deploy. Minimal, additive fix.
COPY alembic.ini .
COPY alembic/ alembic/
# data/ is intentionally not copied — mount it as a volume (docker-compose.yml)
# so large datasets don't bloat the image and stay editable without a rebuild.

RUN chmod +x scripts/*.sh

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    TOKENIZERS_PARALLELISM=false \
    HF_HOME=/app/.cache/huggingface \
    PORT=8000

EXPOSE 8000

# Overridden per-service by docker-compose.yml (trainer vs. api).
CMD ["scripts/run_pipeline.sh", "--serve"]

