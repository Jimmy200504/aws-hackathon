from __future__ import annotations

import unittest

from pipeline.graph_release import evaluate_release_gates


class GraphReleaseGateTests(unittest.TestCase):
    def test_all_quality_and_ranking_gates_must_pass(self) -> None:
        result = evaluate_release_gates(
            quality={"silent_loss": 0, "referential_integrity": True},
            gold={
                "mention_precision": 0.95, "mention_recall": 0.85,
                "alias_auto_merge_precision": 0.995, "published_relation_precision": 0.90,
            },
            graph_off={"ndcg@10": 0.4, "mrr": 0.4},
            graph_on={"ndcg@10": 0.41, "mrr": 0.4},
            api_smoke_passed=True, degraded_fallback_passed=True, search_p95_ms=799,
            production_manifest={
                "processed": 1218635, "cutoff_eligible": 967377,
                "model_id": None, "llm_requests": 0, "embedding_requests": 0,
                "default_scope": "evaluation-cutoff", "candidate_nodes_published": 0,
            },
        )
        self.assertTrue(result.passed)

    def test_ranking_regression_blocks_release(self) -> None:
        result = evaluate_release_gates(
            quality={"silent_loss": 0, "referential_integrity": True},
            gold={
                "mention_precision": 1, "mention_recall": 1,
                "alias_auto_merge_precision": 1, "published_relation_precision": 1,
            },
            graph_off={"ndcg@10": 0.4}, graph_on={"ndcg@10": 0.39},
            api_smoke_passed=True, degraded_fallback_passed=True, search_p95_ms=100,
        )
        self.assertFalse(result.passed)
        self.assertIn("ranking_non_regression", result.failures)

    def test_offline_model_request_blocks_production_release(self) -> None:
        result = evaluate_release_gates(
            quality={"silent_loss": 0, "referential_integrity": True},
            gold={"mention_precision": 1, "mention_recall": 1, "exact_alias_precision": 1, "published_relation_precision": 1},
            graph_off={"ndcg@10": 0.4}, graph_on={"ndcg@10": 0.41},
            api_smoke_passed=True, degraded_fallback_passed=True, search_p95_ms=100,
            production_manifest={
                "processed": 1218635, "cutoff_eligible": 967377,
                "model_id": "forbidden", "llm_requests": 1, "embedding_requests": 0,
                "default_scope": "evaluation-cutoff", "candidate_nodes_published": 0,
            },
        )
        self.assertFalse(result.passed)
        self.assertIn("offline_zero_llm", result.failures)


if __name__ == "__main__":
    unittest.main()
