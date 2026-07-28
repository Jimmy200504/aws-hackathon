from __future__ import annotations

import json
import unittest

from pipeline.bedrock_extract import extract_with_bedrock


class FakeBedrock:
    def __init__(self) -> None:
        self.request = None

    def converse(self, **kwargs):
        self.request = kwargs
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "mentions": [
                                        {
                                            "surface": "Python",
                                            "canonical_skill": "Python",
                                            "type": "language",
                                            "level": "required",
                                            "evidence": "Python",
                                            "evidence_field": "職務內容",
                                            "confidence": 0.99,
                                        }
                                    ],
                                    "relations": [],
                                }
                            )
                        }
                    ]
                }
            }
        }


class BedrockExtractTests(unittest.TestCase):
    def test_uses_strict_structured_output(self) -> None:
        client = FakeBedrock()
        result = extract_with_bedrock(
            client,
            "model-id",
            "system",
            {"job_id": "1", "職務內容": "Python"},
        )
        self.assertEqual(result["mentions"][0]["canonical_skill"], "Python")
        output = client.request["outputConfig"]["textFormat"]
        self.assertEqual(output["type"], "json_schema")
        schema = json.loads(output["structure"]["jsonSchema"]["schema"])
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
