from __future__ import annotations

import unittest

from app.graph_provider import AliasIndexResolver, GraphFeatureProvider


class GraphProviderTests(unittest.TestCase):
    def test_neptune_rows_preserve_edge_provenance(self) -> None:
        class Client:
            def execute_query(self, **kwargs):
                self.request = kwargs
                return {"payload": {"results": [{
                    "source_id": "skill:a", "target_id": "skill:b", "edge_id": "edge:1",
                    "relation_type": "RELATED_TO", "weight": 0.8, "confidence": 0.94,
                    "support_jobs": 30, "support_companies": 7,
                    "evidence": '[{"job_id":"1"}]',
                    "rules_version": "statistical-related-to-v1",
                    "corpus_hash": "abc123",
                    "provenance": '{"source":"full_corpus_cooccurrence"}',
                }]}}

        client = Client()
        result = GraphFeatureProvider("graph-1", client=client, graph_version="v1").expand("a", ["skill:a"])
        self.assertEqual(result.backend, "neptune_analytics")
        self.assertEqual(result.relations["skill:a"]["skill:b"]["edge_id"], "edge:1")
        relation = result.relations["skill:a"]["skill:b"]
        self.assertEqual(relation["evidence"], [{"job_id": "1"}])
        self.assertEqual(relation["rules_version"], "statistical-related-to-v1")
        self.assertEqual(relation["corpus_hash"], "abc123")
        self.assertEqual(
            relation["provenance"], {"source": "full_corpus_cooccurrence"}
        )
        self.assertEqual(client.request["parameters"], {"skill_ids": ["skill:a"]})
        self.assertIn("[edge:RELATED_TO]", client.request["queryString"])

    def test_malformed_neptune_response_degrades_without_relations(self) -> None:
        class Client:
            def execute_query(self, **kwargs):
                return {"payload": {"unexpected": True}}

        result = GraphFeatureProvider("graph-1", client=Client()).expand("a", ["skill:a"])
        self.assertEqual(result.relations, {})
        self.assertEqual(result.degraded_components, ["neptune"])

    def test_alias_index_uses_exact_surface_and_blocked_phrases(self) -> None:
        resolver = AliasIndexResolver("http://localhost:9200", "aliases")
        resolver._signed_request = lambda *args, **kwargs: {
            "hits": {
                "hits": [
                    {"_source": {"normalized_surface": "java", "canonical_ids": ["skill.java"]}},
                    {"_source": {"normalized_surface": "javascript", "canonical_ids": ["skill.javascript"]}},
                    {"_source": {"normalized_surface": "銷售", "canonical_ids": ["skill.sales"], "blocked_phrases": ["銷售面積"]}},
                ]
            }
        }
        self.assertEqual(resolver.resolve("JavaScript"), ("skill.javascript",))
        self.assertEqual(resolver.resolve("銷售面積"), ())

    def test_alias_failure_still_queries_neptune_with_fallback_ids(self) -> None:
        class AliasFailure:
            def resolve(self, query):
                raise RuntimeError("missing index")

        class Client:
            def execute_query(self, **kwargs):
                self.parameters = kwargs["parameters"]
                return {"payload": {"results": []}}

        client = Client()
        provider = GraphFeatureProvider(
            "graph-1", client=client, alias_resolver=AliasFailure()
        )
        result = provider.expand("Python", ["skill.python"])
        self.assertEqual(client.parameters, {"skill_ids": ["skill.python"]})
        self.assertEqual(result.degraded_components, ["skill_alias_index"])


if __name__ == "__main__":
    unittest.main()
