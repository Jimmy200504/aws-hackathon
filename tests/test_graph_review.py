from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_graph_review_packet import carry_forward_reviews, deterministic_job_sample
from scripts.score_graph_review import score_review_packet


class GraphReviewTests(unittest.TestCase):
    def test_carry_forward_requires_identical_locked_evidence(self) -> None:
        current = [
            {"id": "same", "evidence": "A", "decision": "", "reviewer": ""},
            {"id": "changed", "evidence": "B2", "decision": "", "reviewer": ""},
        ]
        previous = [
            {"id": "same", "evidence": "A", "decision": "1", "reviewer": "JK"},
            {"id": "changed", "evidence": "B1", "decision": "1", "reviewer": "JK"},
        ]
        carried = carry_forward_reviews(
            current,
            previous,
            key="id",
            locked_fields=("id", "evidence"),
            review_fields=("decision", "reviewer"),
            required_review_fields=("decision", "reviewer"),
        )
        self.assertEqual(carried, 1)
        self.assertEqual(current[0]["reviewer"], "JK")
        self.assertEqual(current[1]["reviewer"], "")

    def test_job_sample_is_stable_and_stratified(self) -> None:
        rows = [
            {"job_id": f"m-{index}", "mentions": [{"node_id": "skill.x"}]}
            for index in range(8)
        ] + [
            {"job_id": f"n-{index}", "mentions": []}
            for index in range(8)
        ]
        first = deterministic_job_sample(rows, per_stratum=3, seed="locked")
        second = deterministic_job_sample(reversed(rows), per_stratum=3, seed="locked")
        self.assertEqual([row["job_id"] for row in first], [row["job_id"] for row in second])
        self.assertEqual(
            {name: sum(row["sample_stratum"] == name for row in first) for name in ("mentioned", "no_mentions")},
            {"mentioned": 3, "no_mentions": 3},
        )

    def test_score_is_fail_closed_and_computes_locked_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text(json.dumps({
                "status": "awaiting_human_review",
                "job_review_rows": 2,
                "relation_review_rows": 2,
                "manifest_hash": "packet-hash",
            }), encoding="utf-8")
            job_fields = [
                "published_mention_count", "published_mentions_json", "valid_published_mentions", "missed_reviewed_mentions",
                "incorrect_alias_matches", "reviewer",
            ]
            with (root / "job-review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=job_fields)
                writer.writeheader()
                writer.writerow({
                    "published_mention_count": 2, "published_mentions_json": "[{},{}]", "valid_published_mentions": 2,
                    "missed_reviewed_mentions": 0, "incorrect_alias_matches": 0, "reviewer": "r1",
                })
                writer.writerow({
                    "published_mention_count": "", "published_mentions_json": "[]", "valid_published_mentions": 0,
                    "missed_reviewed_mentions": 1, "incorrect_alias_matches": 0, "reviewer": "r1",
                })
            with (root / "relation-review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["is_valid", "reviewer"])
                writer.writeheader()
                writer.writerow({"is_valid": 1, "reviewer": "r2"})
                writer.writerow({"is_valid": 0, "reviewer": "r2"})
            result = score_review_packet(root)
            self.assertEqual(result["mention_precision"], 1.0)
            self.assertEqual(result["mention_recall"], 0.66666667)
            self.assertEqual(result["exact_alias_precision"], 1.0)
            self.assertEqual(result["published_relation_precision"], 0.5)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["quality_gate_passed"])
            self.assertFalse(result["serving_approved"])

            with (root / "relation-review.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["reviewer"] = ""
            with (root / "relation-review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["is_valid", "reviewer"])
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "reviewer is required"):
                score_review_packet(root)


if __name__ == "__main__":
    unittest.main()
