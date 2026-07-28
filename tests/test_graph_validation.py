from __future__ import annotations

import unittest

from pipeline.graph_validation import validate_extraction


class GraphValidationTests(unittest.TestCase):
    def test_accepts_grounded_mention(self) -> None:
        result = validate_extraction(
            {"職務內容": "熟悉 React.js 前端開發", "職務名稱": "前端工程師"},
            "2026-06-05 10:00:00",
            "2026-06-05 23:59:59",
            {
                "mentions": [
                    {
                        "surface": "React.js",
                        "canonical_skill": "React",
                        "type": "framework",
                        "level": "required",
                        "evidence": "React.js",
                        "evidence_field": "職務內容",
                        "confidence": 0.97,
                    }
                ],
                "relations": [],
            },
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.accepted_mentions[0]["canonical_skill"], "React")

    def test_rejects_hallucinated_evidence(self) -> None:
        result = validate_extraction(
            {"職務內容": "熟悉 Linux"},
            "2026-06-05 10:00:00",
            "2026-06-05 23:59:59",
            {
                "mentions": [
                    {
                        "canonical_skill": "Kubernetes",
                        "type": "platform",
                        "level": "required",
                        "evidence": "Kubernetes",
                        "evidence_field": "職務內容",
                        "confidence": 0.99,
                    }
                ]
            },
        )
        self.assertFalse(result.valid)
        self.assertIn("exact source substring", result.rejected[0]["message"])

    def test_future_job_is_fatal(self) -> None:
        result = validate_extraction(
            {"職務內容": "Python"},
            "2026-06-06 00:00:00",
            "2026-06-05 23:59:59",
            {"mentions": []},
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.rejected[0]["code"], "future_source")


if __name__ == "__main__":
    unittest.main()
