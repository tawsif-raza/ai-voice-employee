"""
Lazy-loaded sentence-embedding backend for src/rag/retriever.py.

Wraps sentence-transformers so the model is only imported/loaded once per
process, and only when retrieval is actually used (not paid for by
callers that construct VoiceAssistantInference with RAG disabled).
"""

from typing import Optional

import numpy as np

DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

_model_cache: dict = {}


def _get_model(model_name: str):
    if model_name not in _model_cache:
        from sentence_transformers import SentenceTransformer

        _model_cache[model_name] = SentenceTransformer(model_name)
    return _model_cache[model_name]


def embed_texts(texts: list[str], model_name: str = DEFAULT_MODEL_NAME) -> np.ndarray:
    """Embed a batch of texts, L2-normalized so FAISS inner product == cosine similarity."""
    model = _get_model(model_name)
    vectors = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False)
    return vectors.astype("float32")


def embed_query(text: str, model_name: str = DEFAULT_MODEL_NAME) -> np.ndarray:
    return embed_texts([text], model_name=model_name)[0]
