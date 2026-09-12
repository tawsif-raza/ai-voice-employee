"""
RAG knowledge base loading.

Loads the flat chunk documents under data/knowledge/*.json into a single
list of Chunk records for src/rag/retriever.py to index. Each JSON file's
stem becomes the chunk domain (faqs, policies, medicine, appointments) —
add a new domain by dropping in another *.json file with the same
{id, title, content, tags} shape, no code changes needed.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "data" / "knowledge"


@dataclass(frozen=True)
class Chunk:
    id: str
    domain: str
    title: str
    content: str
    tags: tuple[str, ...] = ()

    def embedding_text(self) -> str:
        """Text handed to the embedding model — the title gives short queries a lexical anchor alongside the content."""
        return f"{self.title}. {self.content}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "domain": self.domain,
            "title": self.title,
            "content": self.content,
            "tags": list(self.tags),
        }


def load_knowledge_base(knowledge_dir: Optional[Path] = None) -> list[Chunk]:
    """Load every *.json file in knowledge_dir into a flat list of Chunks."""
    directory = Path(knowledge_dir) if knowledge_dir else DEFAULT_KNOWLEDGE_DIR
    chunks: list[Chunk] = []
    seen_ids: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        domain = path.stem
        with open(path, "r", encoding="utf-8") as f:
            records = json.load(f)
        for record in records:
            if record["id"] in seen_ids:
                raise ValueError(f"Duplicate knowledge chunk id '{record['id']}' in {path}")
            seen_ids.add(record["id"])
            chunks.append(
                Chunk(
                    id=record["id"],
                    domain=domain,
                    title=record["title"],
                    content=record["content"],
                    tags=tuple(record.get("tags", [])),
                )
            )
    return chunks


def knowledge_dir_mtime(knowledge_dir: Optional[Path] = None) -> float:
    """Latest mtime across all knowledge JSON files — used to detect a stale index."""
    directory = Path(knowledge_dir) if knowledge_dir else DEFAULT_KNOWLEDGE_DIR
    files = list(directory.glob("*.json"))
    if not files:
        return 0.0
    return max(f.stat().st_mtime for f in files)
