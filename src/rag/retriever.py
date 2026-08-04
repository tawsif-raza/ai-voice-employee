"""
FAISS-backed retriever over the knowledge base (src/rag/knowledge_base.py).

Builds an exact inner-product index (IndexFlatIP) over L2-normalized
sentence embeddings — inner product on normalized vectors equals cosine
similarity. Exact search is intentional: at this corpus size (currently
~80 chunks, expected to stay in the hundreds), approximate indexes
(IVF/HNSW) add tuning complexity for no measurable speed benefit.

Persists to <index_dir>/{index.faiss, chunks.json} and rebuilds
automatically if the knowledge-base JSON files are newer than the saved
index — the same self-healing pattern HandoffDetector uses for its YAML
config (src/inference/handoff_detector.py).
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from embeddings import DEFAULT_MODEL_NAME, embed_query, embed_texts  # noqa: E402
from knowledge_base import Chunk, DEFAULT_KNOWLEDGE_DIR, knowledge_dir_mtime, load_knowledge_base  # noqa: E402

DEFAULT_INDEX_DIR = Path(__file__).resolve().parents[2] / "outputs" / "rag_index"


@dataclass(frozen=True)
class RetrievedChunk:
    id: str
    domain: str
    title: str
    content: str
    score: float

    def to_dict(self) -> dict:
        return {"id": self.id, "domain": self.domain, "title": self.title, "score": round(self.score, 4)}


class Retriever:
    def __init__(
        self,
        knowledge_dir: Optional[str] = None,
        index_dir: Optional[str] = None,
        embedding_model: str = DEFAULT_MODEL_NAME,
    ):
        self.knowledge_dir = Path(knowledge_dir) if knowledge_dir else DEFAULT_KNOWLEDGE_DIR
        self.index_dir = Path(index_dir) if index_dir else DEFAULT_INDEX_DIR
        self.embedding_model = embedding_model
        self._chunks: list[Chunk] = []
        self._index = None
        self._load_or_build()

    # ── Index lifecycle ─────────────────────────────────────────────────────

    def _index_paths(self) -> tuple[Path, Path]:
        return self.index_dir / "index.faiss", self.index_dir / "chunks.json"

    def _load_or_build(self) -> None:
        index_path, chunks_path = self._index_paths()
        if index_path.exists() and chunks_path.exists():
            if index_path.stat().st_mtime >= knowledge_dir_mtime(self.knowledge_dir):
                try:
                    self._load(index_path, chunks_path)
                    return
                except (OSError, ValueError, json.JSONDecodeError):
                    pass  # fall through to a fresh build
        self.build()

    def _load(self, index_path: Path, chunks_path: Path) -> None:
        import faiss

        self._index = faiss.read_index(str(index_path))
        with open(chunks_path, "r", encoding="utf-8") as f:
            records = json.load(f)
        self._chunks = [
            Chunk(id=r["id"], domain=r["domain"], title=r["title"], content=r["content"], tags=tuple(r.get("tags", [])))
            for r in records
        ]

    def build(self) -> None:
        """(Re)build the index from the knowledge base and persist it to disk."""
        import faiss

        self._chunks = load_knowledge_base(self.knowledge_dir)
        if not self._chunks:
            raise ValueError(f"No knowledge chunks found under {self.knowledge_dir}")

        vectors = embed_texts([c.embedding_text() for c in self._chunks], model_name=self.embedding_model)
        index = faiss.IndexFlatIP(vectors.shape[1])
        index.add(vectors)
        self._index = index

        self.index_dir.mkdir(parents=True, exist_ok=True)
        index_path, chunks_path = self._index_paths()
        faiss.write_index(index, str(index_path))
        with open(chunks_path, "w", encoding="utf-8") as f:
            json.dump([c.to_dict() for c in self._chunks], f, indent=2)

    # ── Retrieval ────────────────────────────────────────────────────────────

    def retrieve(self, query: str, top_k: int = 3, domain: Optional[str] = None) -> list[RetrievedChunk]:
        if not query or not query.strip() or self._index is None or self._index.ntotal == 0:
            return []

        query_vector = embed_query(query, model_name=self.embedding_model).reshape(1, -1)
        # Over-fetch when filtering by domain so top_k results after
        # filtering still has a fair chance of being full.
        fetch_k = min(top_k * 5 if domain else top_k, self._index.ntotal)
        scores, indices = self._index.search(query_vector, fetch_k)

        results: list[RetrievedChunk] = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            chunk = self._chunks[idx]
            if domain and chunk.domain != domain:
                continue
            results.append(RetrievedChunk(id=chunk.id, domain=chunk.domain, title=chunk.title, content=chunk.content, score=float(score)))
            if len(results) >= top_k:
                break
        return results
