import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_release import ReleaseVerifier
from scripts.update_release_urls import validate_public_https_url
from scripts.report_business_impact import build_report


class ReleaseVerifierIntegrityTests(unittest.TestCase):
    def checks(self, verifier: ReleaseVerifier, group: str) -> dict[str, str]:
        return {check.id: check.status for check in verifier.groups[group]}

    def test_valid_ablation_passes_every_integrity_check(self) -> None:
        report = {
            "metadata": {
                "schema": "skillweave-ltr-ablation-v1",
                "queries": 1993,
                "confidence_gate": "behavior_job_edge",
            },
            "release_gates": {
                "ndcg_relative_lift_at_least_5pct": False,
                "paired_ci_excludes_zero": True,
            },
            "paired_bootstrap_ndcg": {"ci95_low": 0.002},
            "relative_lift": {"ndcg@10": 0.013},
        }
        verifier = ReleaseVerifier()
        verifier.check_ablation(report)
        self.assertTrue(
            all(status == "PASS" for status in self.checks(verifier, "G4_ablation_report").values())
        )

    def test_graph_path_requires_consistent_edges_and_provenance(self) -> None:
        valid = {
            "path": ["Query:React", "Skill:skill.react", "Job:123"],
            "edges": ["RESOLVES_TO", "REQUIRES"],
            "evidence": "職稱：React engineer",
        }
        self.assertTrue(ReleaseVerifier.valid_graph_path(valid))
        self.assertFalse(
            ReleaseVerifier.valid_graph_path(
                {**valid, "edges": ["RESOLVES_TO"]}
            )
        )
        self.assertFalse(ReleaseVerifier.valid_graph_path({**valid, "evidence": "  "}))

    def test_fabricated_five_percent_gate_fails(self) -> None:
        report = {
            "metadata": {
                "schema": "skillweave-ltr-ablation-v1",
                "queries": 1993,
                "confidence_gate": "behavior_job_edge",
            },
            "release_gates": {
                "ndcg_relative_lift_at_least_5pct": True,
                "paired_ci_excludes_zero": True,
            },
            "paired_bootstrap_ndcg": {"ci95_low": 0.002},
            "relative_lift": {"ndcg@10": 0.013},
        }
        verifier = ReleaseVerifier()
        verifier.check_ablation(report)
        self.assertEqual(
            self.checks(verifier, "G4_ablation_report")["G4.5"],
            "FAIL",
        )

    def test_failed_holdout_passes_only_when_failure_is_preserved(self) -> None:
        report = {
            "metadata": {"queries": 200},
            "release_gates": {
                "ndcg_relative_lift_at_least_5pct": False,
                "paired_ci_excludes_zero": False,
            },
            "relative_lift": {"ndcg@10": -0.01},
        }
        verifier = ReleaseVerifier()
        verifier.check_failed_holdout(report)
        self.assertTrue(
            all(status == "PASS" for status in self.checks(verifier, "G5_failed_holdout").values())
        )

    def test_rewritten_failed_holdout_is_detected(self) -> None:
        report = {
            "metadata": {"queries": 200},
            "release_gates": {
                "ndcg_relative_lift_at_least_5pct": False,
                "paired_ci_excludes_zero": True,
            },
            "relative_lift": {"ndcg@10": 0.01},
        }
        verifier = ReleaseVerifier()
        verifier.check_failed_holdout(report)
        checks = self.checks(verifier, "G5_failed_holdout")
        self.assertEqual(checks["G5.3"], "FAIL")
        self.assertEqual(checks["G5.4"], "FAIL")

    def test_partial_load_smoke_success_is_detected(self) -> None:
        verifier = ReleaseVerifier()
        verifier.check_load_smoke(
            {
                "requests": 50,
                "http_200": 49,
                "p95_ms": 100,
                "top_10_responses": 50,
            }
        )
        self.assertEqual(
            self.checks(verifier, "G6_load_smoke")["G6.1"],
            "FAIL",
        )

    def test_graph_coverage_cannot_rewrite_authoritative_gate(self) -> None:
        report = {
            "metadata": {
                "schema": "skillweave-graph-coverage-v1",
                "analysis_status": "post_hoc_descriptive",
                "queries": 1993,
            },
            "authoritative_overall": {
                "five_percent_gate_passed": True,
                "ndcg_at_10_relative_lift": 0.06,
            },
            "coverage": {
                "queries_total": 1993,
                "queries_with_any_graph_feature": 1000,
                "relevant_rows_total": 100,
                "relevant_rows_with_graph_feature": 50,
            },
            "post_hoc_subgroups": {
                "confidence_gate_active": {
                    "queries": 200,
                    "ndcg_at_10_relative_lift": 0.1,
                    "paired_bootstrap_ndcg": {"ci95_low": 0.01},
                }
            },
        }
        verifier = ReleaseVerifier()
        verifier.check_graph_coverage(report)
        self.assertEqual(
            self.checks(verifier, "G8_graph_coverage")["G8.3"],
            "FAIL",
        )

    def test_business_impact_is_bounded_and_has_no_revenue_claim(self) -> None:
        report = build_report(
            {
                "measurement_window": "2026-06-01/2026-06-07",
                "searches_in_window": 1000,
                "currency_value_per_incremental_top1": None,
                "guardrail": (
                    "The scale translation is not causal and is not revenue."
                ),
            },
            {
                "metadata": {"queries": 1993},
                "baseline_no_graph": {"hit@1": 0.20, "mrr": 0.30},
                "skill_graph": {"hit@1": 0.25, "mrr": 0.35},
            },
        )
        self.assertEqual(
            report["scale_translation"][
                "rounded_incremental_top1_relevance_events"
            ],
            50,
        )
        self.assertIsNone(report["financial_claim"]["estimated_revenue"])

    def test_business_verifier_rejects_monetary_fabrication(self) -> None:
        report = {
            "metadata": {
                "schema": "skillweave-business-impact-v1",
                "analysis_status": "offline_scale_translation_not_causal",
                "source_searches": 6_139_952,
                "source_ablation_queries": 1993,
            },
            "observed_offline_metrics": {
                "baseline_hit_at_1": 0.20,
                "skill_graph_hit_at_1": 0.21,
                "absolute_hit_at_1_lift": 0.01,
            },
            "scale_translation": {
                "weekly_searches": 6_139_952,
                "rounded_incremental_top1_relevance_events": 61_400,
            },
            "financial_claim": {
                "currency_value_per_incremental_top1": 10,
                "estimated_revenue": 614_000,
            },
            "guardrail": "This is not causal and is not revenue.",
        }
        verifier = ReleaseVerifier()
        verifier.check_business_impact(report)
        self.assertEqual(
            self.checks(verifier, "G9_business_impact")["G9.4"],
            "FAIL",
        )

    def test_sam_smoke_rejects_missing_provenance(self) -> None:
        report = {
            "metadata": {
                "schema": "skillweave-sam-local-smoke-v1",
                "runtime": "python3.13",
                "architecture": "arm64",
            },
            "passed": True,
            "checks": {
                "sam_validate_lint": True,
                "sam_build": True,
                "sam_local_invoke": True,
                "http_200": True,
                "top_10": True,
                "contiguous_ranks": True,
                "unique_job_ids": True,
                "index_version": True,
                "graph_provenance": False,
            },
            "observed": {
                "result_count": 10,
                "graph_path_count": 0,
                "duration_ms": 500,
            },
        }
        verifier = ReleaseVerifier()
        verifier.check_sam_local_smoke(report)
        checks = self.checks(verifier, "G10_sam_local_smoke")
        self.assertEqual(checks["G10.2"], "FAIL")
        self.assertEqual(checks["G10.3"], "FAIL")

    def test_submission_audit_rejects_false_readiness(self) -> None:
        local = {
            name: True
            for name in (
                "R1_local_live_demo",
                "R2_genai_method_and_failure_modes",
                "R3_data_application_explained",
                "R4_system_graph_schema_and_trace",
                "R5_aws_architecture",
                "R6a_reproducible_source_and_ablation",
                "E1_quantifiable_ndcg_improvement",
                "E3_hit1_and_hit10_reported",
                "E4_position_bias_status_reported",
                "E5_api_contract_verified",
                "B1_business_case_and_ab_design",
                "K1_kiro_activity_evidence",
            )
        }
        report = {
            "metadata": {"schema": "skillweave-submission-audit-v1"},
            "local_release_evidence_passed": True,
            "submission_ready": True,
            "requirements": {
                **local,
                "E2_recommended_five_percent_lift": False,
            },
            "blockers": [],
        }
        verifier = ReleaseVerifier()
        verifier.check_submission_audit(report)
        self.assertEqual(
            self.checks(verifier, "G11_submission_audit")["G11.3"],
            "FAIL",
        )

    def test_manifest_hash_mismatch_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "artifact.bin"
            artifact.write_bytes(b"actual")
            manifest = {
                "confirmation": {
                    "queries": 1993,
                    "aspirational_five_percent_gate_passed": False,
                },
                "sha256": {
                    "artifact.bin": hashlib.sha256(b"different").hexdigest()
                },
                "external_deliverables": {},
            }
            (root / "release-manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            for relative in (
                "docs/openapi.yaml",
                "docs/graph-schema.md",
                "docs/graph-coverage.md",
                "docs/business-case.md",
                "docs/judge-pitch.md",
                "docs/evidence-index.md",
                "docs/aws-architecture.md",
                "docs/genai-safety.md",
                "docs/kiro-evidence.md",
                "docs/deployment.md",
                "docs/submission-checklist.md",
                "infra/template.yaml",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")

            verifier = ReleaseVerifier(root)
            verifier.check_manifest()
            self.assertEqual(
                self.checks(verifier, "G7_manifest_hashes")["G7.3"],
                "FAIL",
            )

    def test_external_url_rejects_local_and_placeholder_hosts(self) -> None:
        self.assertEqual(
            validate_public_https_url("https://github.com/acme/skillweave"),
            "https://github.com/acme/skillweave",
        )
        for value in (
            "http://demo.example.org",
            "https://example.com",
            "https://localhost/demo",
            "https://placeholder.invalid/demo",
        ):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    validate_public_https_url(value)


if __name__ == "__main__":
    unittest.main()
