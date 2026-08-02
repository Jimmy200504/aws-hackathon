from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from scripts.verify_app_deployment import (
    asset_mismatches,
    deployment_url,
    validate_backend,
    verify_assets,
)


class DeploymentUrlTests(unittest.TestCase):
    def test_preserves_api_gateway_stage_for_assets_and_routes(self) -> None:
        base = "https://example.execute-api.us-east-1.amazonaws.com/prod/"
        self.assertEqual(
            deployment_url(base, "app.js", "check 1"),
            f"{base}app.js?deployment_check=check%201",
        )
        self.assertEqual(deployment_url(base, "health"), f"{base}health")
        self.assertEqual(deployment_url(base, "index.html"), base)


class AssetValidationTests(unittest.TestCase):
    def test_reports_only_files_that_do_not_match(self) -> None:
        expected = {"index.html": b"new html", "app.js": b"new js"}
        observed = {"index.html": b"new html", "app.js": b"old js"}
        mismatches = asset_mismatches(expected, observed)
        self.assertEqual(list(mismatches), ["app.js"])
        self.assertNotEqual(
            mismatches["app.js"]["expected"], mismatches["app.js"]["observed"]
        )

    @patch("scripts.verify_app_deployment.request_bytes")
    def test_requires_consecutive_exact_samples_after_old_lambda_drains(
        self, request: MagicMock
    ) -> None:
        expected = {
            "index.html": b"new html",
            "app.js": b"new js",
            "styles.css": b"new css",
        }
        request.side_effect = [
            b"new html",
            b"old js",
            b"new css",
            b"new html",
            b"new js",
            b"new css",
            b"new html",
            b"new js",
            b"new css",
        ]
        hashes = verify_assets(
            "https://example.test/prod/",
            expected,
            stable_samples=2,
            max_attempts=3,
            retry_seconds=0,
            timeout=1,
        )
        self.assertEqual(set(hashes), set(expected))
        self.assertEqual(request.call_count, 9)


class BackendValidationTests(unittest.TestCase):
    def test_accepts_full_corpus_neptune_search(self) -> None:
        summary = validate_backend(
            {
                "status": "ok",
                "full_corpus_retrieval": True,
                "graph_backend": "neptune_analytics",
            },
            {
                "result": [{"rank": 1, "job_id": "1"}],
                "meta": {
                    "candidate_source": "opensearch_full_corpus",
                    "graph_backend": "neptune_analytics",
                    "graph_version": "graph-v1",
                    "degraded_components": [],
                    "query_normalization": {
                        "source": "amazon_bedrock_cached",
                        "normalized_query": "AWS",
                    },
                },
            },
            require_full_corpus=True,
            require_neptune=True,
            expected_graph_version="graph-v1",
        )
        self.assertEqual(summary["candidate_source"], "opensearch_full_corpus")
        self.assertEqual(summary["normalization_source"], "amazon_bedrock_cached")

    def test_rejects_frontend_only_or_degraded_backend(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError, "full_corpus|no result|query_normalization"
        ):
            validate_backend(
                {"status": "ok", "full_corpus_retrieval": False},
                {"result": [], "meta": {"degraded_components": ["opensearch"]}},
                require_full_corpus=True,
                require_neptune=False,
                expected_graph_version=None,
            )


if __name__ == "__main__":
    unittest.main()
