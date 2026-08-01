from __future__ import annotations

import json
import unittest

from app.query_normalizer import BedrockQueryNormalizer


class FakeBedrock:
    def __init__(self, normalized_query: str = "Node.js 後端工程師") -> None:
        self.normalized_query = normalized_query
        self.request = None

    def converse(self, **kwargs):
        self.request = kwargs
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {"normalized_query": self.normalized_query},
                                ensure_ascii=False,
                            )
                        }
                    ]
                }
            }
        }


class FailingBedrock:
    def converse(self, **kwargs):
        raise TimeoutError("simulated timeout")


class BedrockQueryNormalizerTests(unittest.TestCase):
    def test_uses_bedrock_structured_output(self) -> None:
        client = FakeBedrock()
        normalizer = BedrockQueryNormalizer("model-id", client=client)

        result = normalizer.normalize("node js backend engineer")

        self.assertEqual(result.query, "Node.js 後端工程師")
        self.assertEqual(result.source, "amazon_bedrock")
        self.assertFalse(result.degraded)
        self.assertEqual(client.request["modelId"], "model-id")
        output = client.request["outputConfig"]["textFormat"]
        self.assertEqual(output["type"], "json_schema")
        schema = json.loads(output["structure"]["jsonSchema"]["schema"])
        self.assertFalse(schema["additionalProperties"])

    def test_failure_falls_back_without_logging_query_content(self) -> None:
        normalizer = BedrockQueryNormalizer("model-id", client=FailingBedrock())

        with self.assertLogs("app.query_normalizer", level="WARNING") as logs:
            result = normalizer.normalize("  Ｎｏｄｅ   js secret@example.com  ")

        self.assertEqual(result.query, "Node js secret@example.com")
        self.assertEqual(result.source, "deterministic_fallback")
        self.assertTrue(result.degraded)
        self.assertEqual(
            result.merge_degraded_components(["opensearch", "opensearch"]),
            ["opensearch", "bedrock_query_normalizer"],
        )
        self.assertNotIn("secret@example.com", " ".join(logs.output))

    def test_unconfigured_local_mode_does_not_construct_a_client(self) -> None:
        result = BedrockQueryNormalizer(None).normalize("  React   前端  ")
        self.assertEqual(result.query, "React 前端")
        self.assertEqual(result.source, "deterministic")
        self.assertFalse(result.degraded)
        self.assertEqual(
            result.metadata(),
            {
                "source": "deterministic",
                "model_id": None,
                "normalized_query": "React 前端",
            },
        )

    def test_rejects_oversized_query_before_calling_bedrock(self) -> None:
        client = FakeBedrock()
        normalizer = BedrockQueryNormalizer("model-id", client=client)
        with self.assertRaisesRegex(ValueError, "at most 500"):
            normalizer.normalize("x" * 501)
        self.assertIsNone(client.request)


if __name__ == "__main__":
    unittest.main()
