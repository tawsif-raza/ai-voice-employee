"""
Unit tests for the FAISS-backed retriever (src/rag/retriever.py).

Builds a tiny temporary knowledge base so these don't depend on (or get
broken by) the real data/knowledge/*.json content. Needs the
sentence-transformers embedding model available locally (downloaded once
by src/rag/build_index.py or the first Retriever construction) -- these
tests are slower than the fully offline handoff-detector suite because of
that one-time model load.

Run with:
    python -m unittest tests.test_retriever -v
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "rag"))

from retriever import Retriever  # noqa: E402

FAKE_KNOWLEDGE = {
    "faqs": [
        {
            "id": "faq_hours",
            "title": "Store hours",
            "content": "We are open Monday to Saturday, 9am to 8pm.",
            "tags": ["hours"],
        },
        {
            "id": "faq_returns",
            "title": "Returning an item",
            "content": "Unopened items can be returned within 30 days with a receipt.",
            "tags": ["returns"],
        },
    ],
    "medicine": [
        {
            "id": "med_ibuprofen",
            "title": "Ibuprofen",
            "content": "Ibuprofen is an over-the-counter pain reliever and anti-inflammatory.",
            "tags": ["pain"],
        },
        {
            "id": "med_vitamin_c",
            "title": "Vitamin C",
            "content": "Vitamin C is a common over-the-counter immune-support supplement.",
            "tags": ["vitamin"],
        },
    ],
    "appointments": [
        {
            "id": "appt_book",
            "title": "Booking an appointment",
            "content": "Book an appointment through the app by choosing a date and time.",
            "tags": ["booking"],
        },
    ],
}


class TestRetriever(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = Path(tempfile.mkdtemp(prefix="rag_test_"))
        cls.knowledge_dir = cls.tmp_dir / "knowledge"
        cls.index_dir = cls.tmp_dir / "index"
        cls.knowledge_dir.mkdir(parents=True)

        for domain, chunks in FAKE_KNOWLEDGE.items():
            with open(cls.knowledge_dir / f"{domain}.json", "w", encoding="utf-8") as f:
                json.dump(chunks, f)

        cls.retriever = Retriever(knowledge_dir=cls.knowledge_dir, index_dir=cls.index_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def test_index_has_all_chunks(self):
        self.assertEqual(self.retriever._index.ntotal, 5)

    def test_retrieves_correct_chunk_for_matching_query(self):
        results = self.retriever.retrieve("What time do you open?", top_k=1)
        self.assertEqual(results[0].id, "faq_hours")

    def test_retrieves_correct_chunk_medicine_domain(self):
        results = self.retriever.retrieve("What is ibuprofen used for?", top_k=1)
        self.assertEqual(results[0].id, "med_ibuprofen")

    def test_unrelated_query_scores_low(self):
        results = self.retriever.retrieve("What is the airspeed velocity of an unladen swallow?", top_k=1)
        self.assertLess(results[0].score, 0.3)

    def test_domain_filter(self):
        results = self.retriever.retrieve("tell me about products", top_k=5, domain="medicine")
        self.assertTrue(all(r.domain == "medicine" for r in results))
        self.assertLessEqual(len(results), 2)

    def test_empty_query_returns_empty(self):
        self.assertEqual(self.retriever.retrieve(""), [])
        self.assertEqual(self.retriever.retrieve("   "), [])

    def test_top_k_respected(self):
        results = self.retriever.retrieve("appointment or return or medicine", top_k=2)
        self.assertLessEqual(len(results), 2)

    def test_index_persisted_to_disk(self):
        index_path = self.index_dir / "index.faiss"
        chunks_path = self.index_dir / "chunks.json"
        self.assertTrue(index_path.exists())
        self.assertTrue(chunks_path.exists())

    def test_reload_from_persisted_index(self):
        # A fresh Retriever pointed at the same dirs should load (not
        # rebuild) and return identical results without re-embedding.
        reloaded = Retriever(knowledge_dir=self.knowledge_dir, index_dir=self.index_dir)
        results = reloaded.retrieve("What time do you open?", top_k=1)
        self.assertEqual(results[0].id, "faq_hours")


class TestKnowledgeBaseLoading(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="kb_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_duplicate_id_raises(self):
        from knowledge_base import load_knowledge_base

        (self.tmp_dir / "a.json").write_text(json.dumps([{"id": "x", "title": "A", "content": "a"}]), encoding="utf-8")
        (self.tmp_dir / "b.json").write_text(json.dumps([{"id": "x", "title": "B", "content": "b"}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_knowledge_base(self.tmp_dir)

    def test_domain_from_filename(self):
        from knowledge_base import load_knowledge_base

        (self.tmp_dir / "widgets.json").write_text(
            json.dumps([{"id": "w1", "title": "Widget", "content": "A widget."}]), encoding="utf-8"
        )
        chunks = load_knowledge_base(self.tmp_dir)
        self.assertEqual(chunks[0].domain, "widgets")


if __name__ == "__main__":
    unittest.main()
