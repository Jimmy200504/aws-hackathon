import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import audit_submission


class SubmissionAuditStateTests(unittest.TestCase):
    def write_json(self, root: Path, relative: str, value: dict) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_external_completion_removes_stale_blockers_and_refreshes_hash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_json(
                root,
                "release-manifest.json",
                {
                    "release": "release-test",
                    "sha256": {},
                    "external_deliverables": {
                        "aws_url": "https://demo.test.invalid",
                        "github_url": "https://github.test.invalid/release",
                        "demo_video_url": "https://video.test.invalid/demo",
                    },
                },
            )
            self.write_json(
                root,
                "reports/verify-release.json",
                {
                    "passed": True,
                    "groups": {
                        "G1_api_contract": {"passed": True},
                        "G2_graph_cutoff": {"passed": True},
                        "G9_business_impact": {"passed": True},
                    },
                },
            )
            self.write_json(
                root,
                "reports/ltr-ablation-test.json",
                {
                    "relative_lift": {"ndcg@10": 0.01},
                    "release_gates": {
                        "ndcg_relative_lift_at_least_5pct": False
                    },
                    "metadata": {
                        "position_bias_correction": (
                            "XGBoost Unbiased LambdaMART"
                        )
                    },
                    "skill_graph": {"hit@1": 0.2, "hit@10": 0.8},
                },
            )
            self.write_json(
                root,
                "reports/demo-video.json",
                {
                    "passed": True,
                    "metadata": {"release": "release-test"},
                },
            )
            for relative in (
                "web/index.html",
                "pipeline/deterministic_extract.py",
                "pipeline/skill_graph.py",
                "docs/genai-safety.md",
                "docs/data-card.md",
                "docs/graph-schema.md",
                "docs/aws-architecture.md",
                "scripts/run_ltr_ablation.sh",
                "requirements-ltr.lock",
                "docs/business-case.md",
                "docs/kiro-evidence.md",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")

            output = root / "reports/submission-audit.json"
            with (
                patch.object(audit_submission, "ROOT", root),
                patch.object(audit_submission, "OUTPUT", output),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                audit_submission.main()

            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                report["blockers"],
                ["R2a_full_deterministic_graph_release_gates_passed"],
            )
            manifest = json.loads(
                (root / "release-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["sha256"]["reports/submission-audit.json"],
                hashlib.sha256(output.read_bytes()).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
