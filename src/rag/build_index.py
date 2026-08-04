"""
RAG index build entry point.

Rebuilds the FAISS index over data/knowledge/*.json and writes it to
outputs/rag_index/. Run this whenever knowledge-base content changes.
Retriever also rebuilds automatically on first use if the index is
missing or stale (see src/rag/retriever.py), so this script is mainly
for explicit/CI rebuilds and for confirming the index matches the
current knowledge base.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retriever import Retriever  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the RAG FAISS index from data/knowledge/*.json")
    parser.add_argument("--knowledge_dir", default=None)
    parser.add_argument("--index_dir", default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    retriever = Retriever(knowledge_dir=args.knowledge_dir, index_dir=args.index_dir)
    retriever.build()

    by_domain: dict[str, int] = {}
    for chunk in retriever._chunks:
        by_domain[chunk.domain] = by_domain.get(chunk.domain, 0) + 1

    print(f"Indexed {len(retriever._chunks)} chunks from {retriever.knowledge_dir}")
    for domain, count in sorted(by_domain.items()):
        print(f"  {domain}: {count}")
    print(f"Wrote index to {retriever.index_dir}")
