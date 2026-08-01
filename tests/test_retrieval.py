from __future__ import annotations

import unittest

from app.retrieval import OpenSearchRetriever


class StubOpenSearchRetriever(OpenSearchRetriever):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:9200", "skillweave-jobs-v1")
        self.request = None

    def _signed_request(self, method, path, body):
        self.request = (method, path, body)
        return {
            "hits": {
                "hits": [
                    {
                        "_id": "full-job-1",
                        "_score": 12.5,
                        "_source": {
                            "title": "Node.js 後端工程師",
                            "description": "開發 API",
                            "categories": ["軟體工程"],
                            "city": "台北市",
                            "industry": "資訊軟體",
                            "graph_eligible": True,
                            "skills": ["skill.nodejs"],
                        },
                    }
                ]
            }
        }


class OpenSearchRetrieverTests(unittest.TestCase):
    def test_local_endpoint_does_not_require_aws_signing(self) -> None:
        retriever = OpenSearchRetriever("http://localhost:9200", "jobs")
        self.assertFalse(retriever.sign_requests)

    def test_retrieve_builds_full_text_and_condition_query(self) -> None:
        retriever = StubOpenSearchRetriever()
        rows = retriever.retrieve(
            "後端工程師 Node.js",
            limit=200,
            location_names=["台北市"],
            duty_names=["軟體工程"],
        )
        self.assertEqual(rows[0]["id"], "full-job-1")
        self.assertEqual(rows[0]["_retrieval_score"], 12.5)
        method, path, body = retriever.request
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/skillweave-jobs-v1/_search")
        self.assertEqual(body["size"], 200)
        should = body["query"]["bool"]["should"]
        self.assertTrue(any("multi_match" in clause for clause in should))
        self.assertTrue(
            any(
                clause.get("term", {}).get("city.keyword", {}).get("value")
                == "台北市"
                for clause in should
            )
        )

    def test_rejects_non_tls_remote_endpoint(self) -> None:
        with self.assertRaises(ValueError):
            OpenSearchRetriever("http://example.com", "jobs")

    def test_remote_intent_adds_is_remote_should_clause(self) -> None:
        retriever = StubOpenSearchRetriever()
        retriever.retrieve(
            "遠端客服",
            limit=200,
            wants_remote=True,
        )
        _, _, body = retriever.request
        should = body["query"]["bool"]["should"]
        self.assertTrue(
            any(
                clause.get("term", {}).get("is_remote", {}).get("value") is True
                for clause in should
            )
        )

    def test_no_remote_intent_omits_is_remote_clause(self) -> None:
        retriever = StubOpenSearchRetriever()
        retriever.retrieve("後端工程師", limit=200, wants_remote=False)
        _, _, body = retriever.request
        should = body["query"]["bool"]["should"]
        self.assertFalse(
            any("is_remote" in clause.get("term", {}) for clause in should)
        )

    def test_salary_intent_adds_type_gated_range_clause(self) -> None:
        retriever = StubOpenSearchRetriever()
        retriever.retrieve(
            "時薪210",
            limit=200,
            salary_intent={
                "salary_type": "hourly",
                "target": 210.0,
                "comparator": "at_least",
            },
        )
        _, _, body = retriever.request
        should = body["query"]["bool"]["should"]
        salary_clauses = [
            clause
            for clause in should
            if "bool" in clause
            and any(
                f.get("term", {}).get("salary_type") == "hourly"
                for f in clause["bool"].get("filter", [])
            )
        ]
        self.assertEqual(len(salary_clauses), 1)
        inner_should = salary_clauses[0]["bool"]["should"]
        # Covers the case where salary_max is set (must reach the target)...
        self.assertTrue(
            any(
                any(
                    f.get("range", {}).get("salary_max", {}).get("gte") == 210.0
                    for f in clause.get("bool", {}).get("filter", [])
                )
                for clause in inner_should
            )
        )
        # ...and the case where only salary_min is set (no upper bound).
        self.assertTrue(
            any(
                any(
                    f.get("range", {}).get("salary_min", {}).get("gte") == 210.0
                    for f in clause.get("bool", {}).get("filter", [])
                )
                for clause in inner_should
            )
        )

    def test_no_salary_intent_omits_salary_clause(self) -> None:
        retriever = StubOpenSearchRetriever()
        retriever.retrieve("後端工程師", limit=200, salary_intent=None)
        _, _, body = retriever.request
        should = body["query"]["bool"]["should"]
        self.assertFalse(
            any(
                any(
                    f.get("term", {}).get("salary_type")
                    for f in clause.get("bool", {}).get("filter", [])
                )
                for clause in should
                if "bool" in clause
            )
        )

    def test_source_fields_include_salary_and_remote_fields(self) -> None:
        retriever = StubOpenSearchRetriever()
        retriever.retrieve("後端工程師", limit=200)
        _, _, body = retriever.request
        self.assertIn("salary_min", body["_source"])
        self.assertIn("salary_max", body["_source"])
        self.assertIn("salary_type", body["_source"])
        self.assertIn("is_remote", body["_source"])


if __name__ == "__main__":
    unittest.main()
