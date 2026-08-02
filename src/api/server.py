"""
Phase 9: Inference API
Goal: Serve the exported voice assistant over HTTP so a Text-to-Speech
      layer (or any other client) can consume plain-text responses
      without embedding the model-loading code itself.
"""

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import uvicorn

# Reuse VoiceAssistantInference from src/inference/predict.py instead of
# duplicating it. This codebase avoids package-relative imports (no
# __init__.py anywhere), so the sibling directory is added to sys.path
# explicitly rather than importing across src/ subpackages.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "inference"))
from predict import VoiceAssistantInference  # noqa: E402

# Where to load the model from. Prefer an already-merged export (produced by
# src/export/merge_and_convert.py) — no adapter attach step needed. Falls
# back to base model + auto-resolved LoRA adapter if no merged model exists
# yet (e.g. before Phase 8 export has been run).
MERGED_MODEL_DIR = os.environ.get("MERGED_MODEL_DIR", "outputs/merged_model")
BASE_MODEL_NAME = os.environ.get("BASE_MODEL_NAME", "Qwen/Qwen2.5-0.5B-Instruct")

_assistant: Optional[VoiceAssistantInference] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _assistant
    merged_dir = Path(MERGED_MODEL_DIR)
    if merged_dir.exists():
        print(f"Loading merged model from {merged_dir}...")
        _assistant = VoiceAssistantInference(
            base_model_name=str(merged_dir), auto_resolve_adapter=False
        )
    else:
        print(
            f"No merged model at {merged_dir} — loading base model "
            "with auto-resolved LoRA adapter instead."
        )
        _assistant = VoiceAssistantInference(base_model_name=BASE_MODEL_NAME)
    yield


app = FastAPI(title="AI Voice Employee — Inference API", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = Field(default_factory=list)
    # When True, /generate returns newline-delimited JSON (NDJSON) chunks
    # instead of blocking for the full reply — see generate() below.
    stream: bool = False


class ChatResponse(BaseModel):
    response: str
    is_handoff: bool
    latency_ms: float


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model_loaded": _assistant is not None}


@app.post("/generate", response_model=ChatResponse)
def generate(req: ChatRequest):
    """
    Generate a response to req.message.

    req.stream == False (default): blocks until generation finishes and
    returns a single ChatResponse JSON body.

    req.stream == True: returns NDJSON — one {"token": "..."} line per
    generated chunk, followed by a final
    {"done": true, "response": ..., "is_handoff": ..., "latency_ms": ...}
    line. Lets a client (e.g. src/voice/client_tts.py) start speaking the
    first sentence before the rest of the reply has finished generating.
    """
    if _assistant is None:
        raise HTTPException(status_code=503, detail="Model not loaded yet.")

    if not req.stream:
        result = _assistant.generate_response(req.message, history=req.history)
        return ChatResponse(**result)

    def ndjson():
        for item in _assistant.generate_response_stream(req.message, history=req.history):
            if isinstance(item, str):
                yield json.dumps({"token": item}) + "\n"
            else:
                yield json.dumps({"done": True, **item}) + "\n"

    return StreamingResponse(ndjson(), media_type="application/x-ndjson")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
