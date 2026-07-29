from __future__ import annotations

import json
import unittest

from app.lambda_handler import handler


def event(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    stage: str | None = None,
) -> dict:
    request_context = {"http": {"method": method, "path": path}}
    if stage is not None:
        request_context["stage"] = stage
    return {
        "requestContext": request_context,
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


class LambdaHandlerTests(unittest.TestCase):
    def test_health(self) -> None:
        result = handler(event("GET", "/health"), None)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(json.loads(result["body"])["status"], "ok")

    def test_search_contract(self) -> None:
        result = handler(
            event("POST", "/api/v1/jobs/search", {"query": "行政助理", "top_k": 10}),
            None,
        )
        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual([row["rank"] for row in body["result"]], list(range(1, 11)))

    def test_invalid_query(self) -> None:
        result = handler(event("POST", "/api/v1/jobs/search", {"query": ""}), None)
        self.assertEqual(result["statusCode"], 400)

    def test_unknown_filter_codes_degrade_without_server_error(self) -> None:
        result = handler(
            event(
                "POST",
                "/api/v1/jobs/search",
                {
                    "query": "行政助理",
                    "location_code": ["unknown"],
                    "duty_code": ["unknown"],
                },
            ),
            None,
        )
        self.assertEqual(result["statusCode"], 200)
        self.assertGreaterEqual(len(json.loads(result["body"])["result"]), 10)

    def test_named_api_gateway_stage_is_removed_from_routes(self) -> None:
        health = handler(event("GET", "/prod/health", stage="prod"), None)
        self.assertEqual(json.loads(health["body"])["status"], "ok")
        search = handler(
            event(
                "POST",
                "/prod/api/v1/jobs/search",
                {"query": "行政助理", "top_k": 10},
                stage="prod",
            ),
            None,
        )
        self.assertEqual(search["statusCode"], 200)
        self.assertEqual(len(json.loads(search["body"])["result"]), 10)

    def test_named_stage_static_assets_resolve(self) -> None:
        result = handler(
            event("GET", "/prod/styles.css", stage="prod"),
            None,
        )
        self.assertEqual(result["statusCode"], 200)
        self.assertTrue(result["headers"]["content-type"].startswith("text/css"))


if __name__ == "__main__":
    unittest.main()
