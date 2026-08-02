"""Hybrid BM25 + kNN retrieval behaviour and its honest disclosure."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.embeddings import BedrockEmbeddingClient, EMBEDDING_DIM
from app.retrieval import OpenSearchRetriever, hybrid_retrieval_meta
from scripts.index_full_opensearch import embedding_bulk_payload


class StubEmbeddingClient:
    """Deterministic stand-in for Bedrock Titan embeddings."""

    def __init__(self, vector=None, raises: bool = False, delay: float = 0.0) -> None:
        self.model_id = "amazon.titan-embed-text-v2:0"
        self.vector = vector
        self.raises = raises
        self.delay = delay
        self.calls = 0

    def embed(self, text: str):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.raises:
            raise RuntimeError("bedrock unavailable")
        return self.vector


def hit(doc_id: str, score: float = 1.0) -> dict:
    return {"_id": doc_id, "_score": score, "_source": {"id": doc_id, "title": doc_id}}


class HybridRetrievalTests(unittest.TestCase):
    def build(self, embedding_client=None, knn_hits=None, bm25_hits=None, deadline=None):
        retriever = OpenSearchRetriever(
            "https://example.us-east-1.es.amazonaws.com",
            "skillweave-jobs-v1",
            embedding_client=embedding_client,
            hybrid_deadline_seconds=deadline,
        )
        retriever._search_bm25 = lambda *a, **k: list(  # type: ignore[method-assign]
            bm25_hits if bm25_hits is not None else [hit("a"), hit("b")]
        )
        retriever._search_knn = lambda *a, **k: list(  # type: ignore[method-assign]
            knn_hits if knn_hits is not None else []
        )
        return retriever

    def test_bm25_only_when_no_embedding_client(self) -> None:
        retriever = self.build()
        candidates = retriever.retrieve("後端工程師", limit=10)
        self.assertEqual([job["id"] for job in candidates], ["a", "b"])
        self.assertFalse(retriever.hybrid_enabled)
        self.assertEqual(
            retriever.last_retrieval_telemetry()["mode"],
            OpenSearchRetriever.BM25_ONLY,
        )

    def test_knn_appends_novel_candidates_without_reordering_bm25(self) -> None:
        retriever = self.build(
            embedding_client=StubEmbeddingClient(vector=[0.1] * 8),
            knn_hits=[hit("b"), hit("z")],
        )
        candidates = retriever.retrieve("後端工程師", limit=10)
        # BM25 order is preserved and only the unseen kNN document is appended.
        self.assertEqual([job["id"] for job in candidates], ["a", "b", "z"])
        telemetry = retriever.last_retrieval_telemetry()
        self.assertEqual(telemetry["mode"], OpenSearchRetriever.HYBRID_BM25_KNN)
        self.assertEqual(telemetry["knn_candidates"], 2)
        self.assertFalse(telemetry["knn_degraded"])

    def test_embedding_failure_degrades_to_bm25_and_is_disclosed(self) -> None:
        retriever = self.build(embedding_client=StubEmbeddingClient(raises=True))
        candidates = retriever.retrieve("後端工程師", limit=10)
        self.assertEqual([job["id"] for job in candidates], ["a", "b"])
        telemetry = retriever.last_retrieval_telemetry()
        self.assertTrue(telemetry["knn_degraded"])
        self.assertFalse(telemetry["knn_deadline_skipped"])
        self.assertEqual(telemetry["mode"], OpenSearchRetriever.BM25_ONLY)

    def test_missing_query_vector_is_degraded_not_silent(self) -> None:
        retriever = self.build(embedding_client=StubEmbeddingClient(vector=None))
        retriever.retrieve("後端工程師", limit=10)
        self.assertTrue(retriever.last_retrieval_telemetry()["knn_degraded"])

    def test_empty_vector_index_reports_bm25_only(self) -> None:
        """An enabled vector leg that returns nothing must not claim hybrid."""
        retriever = self.build(
            embedding_client=StubEmbeddingClient(vector=[0.1] * 8), knn_hits=[]
        )
        retriever.retrieve("後端工程師", limit=10)
        self.assertEqual(
            retriever.last_retrieval_telemetry()["mode"],
            OpenSearchRetriever.BM25_ONLY,
        )

    def test_slow_vector_leg_is_abandoned_at_the_deadline(self) -> None:
        """The latency budget belongs to the user, not to Bedrock."""
        retriever = self.build(
            embedding_client=StubEmbeddingClient(vector=[0.1] * 8, delay=1.0),
            knn_hits=[hit("z")],
            deadline=0.1,
        )
        started = time.monotonic()
        candidates = retriever.retrieve("後端工程師", limit=10)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.9)
        self.assertEqual([job["id"] for job in candidates], ["a", "b"])
        telemetry = retriever.last_retrieval_telemetry()
        self.assertTrue(telemetry["knn_deadline_skipped"])
        # A latency policy decision is not a component failure.
        self.assertFalse(telemetry["knn_degraded"])
        self.assertEqual(telemetry["mode"], OpenSearchRetriever.BM25_ONLY)

    def test_vector_leg_within_deadline_still_contributes(self) -> None:
        retriever = self.build(
            embedding_client=StubEmbeddingClient(vector=[0.1] * 8, delay=0.05),
            knn_hits=[hit("z")],
            deadline=1.0,
        )
        candidates = retriever.retrieve("後端工程師", limit=10)
        self.assertEqual([job["id"] for job in candidates], ["a", "b", "z"])
        self.assertFalse(retriever.last_retrieval_telemetry()["knn_degraded"])


class EmbeddingCacheTests(unittest.TestCase):
    class FakeBedrock:
        def __init__(self) -> None:
            self.invocations = 0

        def invoke_model(self, **kwargs):
            self.invocations += 1
            import io
            import json as _json

            body = _json.dumps({"embedding": [0.01] * EMBEDDING_DIM}).encode()
            return {"body": io.BytesIO(body)}

    def test_repeat_query_does_not_re_invoke_bedrock(self) -> None:
        fake = self.FakeBedrock()
        client = BedrockEmbeddingClient(
            "amazon.titan-embed-text-v2:0", client=fake, cache_size=8
        )
        first = client.embed("後端工程師")
        second = client.embed("後端工程師")
        self.assertEqual(first, second)
        self.assertEqual(fake.invocations, 1)
        self.assertEqual(client.cache_hits, 1)

    def test_cache_is_bounded_so_backfill_cannot_exhaust_memory(self) -> None:
        fake = self.FakeBedrock()
        client = BedrockEmbeddingClient(
            "amazon.titan-embed-text-v2:0", client=fake, cache_size=2
        )
        for index in range(5):
            client.embed(f"職務 {index}")
        self.assertLessEqual(len(client._cache), 2)
        self.assertEqual(fake.invocations, 5)

    def test_cache_can_be_disabled(self) -> None:
        fake = self.FakeBedrock()
        client = BedrockEmbeddingClient(
            "amazon.titan-embed-text-v2:0", client=fake, cache_size=0
        )
        client.embed("後端工程師")
        client.embed("後端工程師")
        self.assertEqual(fake.invocations, 2)


class HybridDisclosureTests(unittest.TestCase):
    def test_meta_is_disabled_without_retriever(self) -> None:
        meta = hybrid_retrieval_meta(None)
        self.assertFalse(meta["enabled"])
        self.assertIsNone(meta["vector_document_count"])
        self.assertIsNone(meta["fusion_method"])

    def test_meta_reports_model_and_vector_coverage(self) -> None:
        retriever = OpenSearchRetriever(
            "https://example.us-east-1.es.amazonaws.com",
            "skillweave-jobs-v1",
            embedding_client=StubEmbeddingClient(vector=[0.1] * 8),
        )
        retriever._signed_request = lambda *a, **k: {"count": 4321}  # type: ignore[method-assign]
        meta = hybrid_retrieval_meta(retriever)
        self.assertTrue(meta["enabled"])
        self.assertEqual(meta["vector_document_count"], 4321)
        self.assertEqual(meta["embedding_model_id"], "amazon.titan-embed-text-v2:0")
        self.assertEqual(
            meta["fusion_method"], OpenSearchRetriever.FUSION_METHOD
        )

    def test_vector_coverage_probe_failure_returns_none(self) -> None:
        retriever = OpenSearchRetriever(
            "https://example.us-east-1.es.amazonaws.com",
            "skillweave-jobs-v1",
            embedding_client=StubEmbeddingClient(vector=[0.1] * 8),
        )

        def boom(*args, **kwargs):
            raise RuntimeError("count unavailable")

        retriever._signed_request = boom  # type: ignore[method-assign]
        self.assertIsNone(retriever.vector_document_count())


class EmbeddingBackfillPayloadTests(unittest.TestCase):
    def test_backfill_only_writes_the_embedding_field(self) -> None:
        payload = embedding_bulk_payload(
            "skillweave-jobs-v1", [("132144448", [0.5, 0.25])]
        ).decode("utf-8")
        lines = payload.strip().split("\n")
        self.assertEqual(len(lines), 2)
        self.assertIn('"update"', lines[0])
        self.assertIn('"_id":"132144448"', lines[0])
        # A partial update must not resend unrelated fields.
        self.assertEqual(lines[1], '{"doc":{"embedding":[0.5,0.25]}}')

    def test_payload_ends_with_newline(self) -> None:
        payload = embedding_bulk_payload("idx", [("1", [0.1])])
        self.assertTrue(payload.endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
