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


if __name__ == "__main__":
    unittest.main()
