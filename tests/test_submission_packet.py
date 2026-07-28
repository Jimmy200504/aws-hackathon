import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_submission_packet
from scripts.build_submission_packet import build_packet


class SubmissionPacketTests(unittest.TestCase):
    def write(self, root: Path, relative: str, value: dict) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def fixture(self, root: Path, *, urls: bool) -> None:
        external = {
            "aws_url": "https://aws.invalid/demo" if urls else None,
            "github_url": "https://github.invalid/release" if urls else None,
            "demo_video_url": "https://video.invalid/demo" if urls else None,
        }
        self.write(
            root,
            "release-manifest.json",
            {
                "release": "release-test",
                "external_deliverables": external,
                "kiro": {
                    "cli_version": "1.0",
                    "session_id": "session",
                    "evidence": "docs/evidence.md",
                },
            },
        )
        self.write(
            root,
            "reports/ltr-ablation-test.json",
            {
                "metadata": {"queries": 1993},
                "baseline_no_graph": {"ndcg@10": 0.4},
                "skill_graph": {"ndcg@10": 0.405},
                "relative_lift": {
                    "ndcg@10": 0.0134,
                    "mrr": 0.0172,
                    "hit@1": 0.027,
                    "hit@10": -0.0012,
                },
                "paired_bootstrap_ndcg": {
                    "ci95_low": 0.002,
                    "ci95_high": 0.009,
                },
            },
        )
        self.write(
            root,
            "reports/graph-coverage.json",
            {
                "coverage": {
                    "relevant_row_graph_coverage_rate": 0.4053
                },
                "post_hoc_subgroups": {
                    "confidence_gate_active": {
                        "queries": 285,
                        "ndcg_at_10_relative_lift": 0.1141,
                    }
                },
            },
        )
        self.write(
            root,
            "reports/business-impact.json",
            {
                "scale_translation": {
                    "rounded_incremental_top1_relevance_events": 40050
                }
            },
        )
        self.write(
            root,
            "reports/demo-video.json",
            {
                "artifact": {
                    "duration_seconds": 300.046,
                    "sha256": "a" * 64,
                }
            },
        )
        self.write(
            root,
            "reports/verify-release.json",
            {"summary": {"passed": 60, "failed": 0, "warnings": 3}},
        )

    def test_packet_preserves_claim_limits_and_pending_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, urls=False)
            packet = build_packet(root)
            self.assertIn("相對 +1.34%", packet)
            self.assertIn("5% gate **未通過**", packet)
            self.assertIn("不宣稱營收", packet)
            self.assertEqual(
                packet.count("PENDING — 不可填 placeholder"),
                3,
            )

    def test_packet_uses_registered_urls_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, urls=True)
            packet = build_packet(root)
            for url in (
                "https://aws.invalid/demo",
                "https://github.invalid/release",
                "https://video.invalid/demo",
            ):
                self.assertIn(url, packet)
            self.assertNotIn("**PENDING", packet)

    def test_writer_refreshes_packet_hash_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, urls=False)
            output = root / "docs/submission-packet.md"
            with (
                patch.object(build_submission_packet, "ROOT", root),
                patch.object(build_submission_packet, "OUTPUT", output),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                build_submission_packet.main()
            manifest = json.loads(
                (root / "release-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["sha256"]["docs/submission-packet.md"],
                hashlib.sha256(output.read_bytes()).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
