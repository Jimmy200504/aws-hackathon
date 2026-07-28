#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_OUTPUT = ROOT / "reports" / "verify-release.json"
GRAPH_CUTOFF = "2026-06-05 23:59:59.999"


@dataclass(frozen=True)
class Check:
    id: str
    status: str
    message: str


class ReleaseVerifier:
    """Machine-check the public release without opening sensitive raw data."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.groups: dict[str, list[Check]] = {}

    def add(
        self,
        group: str,
        check_id: str,
        condition: bool,
        success: str,
        failure: str,
        *,
        warning: bool = False,
    ) -> None:
        status = "PASS" if condition else "WARN" if warning else "FAIL"
        self.groups.setdefault(group, []).append(
            Check(check_id, status, success if condition else failure)
        )

    def load_json(self, relative: str) -> dict[str, Any]:
        value = json.loads((self.root / relative).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{relative} must contain a JSON object")
        return value

    @staticmethod
    def lambda_event(method: str, path: str, body: dict | None = None) -> dict:
        return {
            "requestContext": {"http": {"method": method, "path": path}},
            "body": json.dumps(body, ensure_ascii=False) if body is not None else None,
            "isBase64Encoded": False,
        }

    @staticmethod
    def response_body(response: dict) -> dict[str, Any]:
        raw = response["body"]
        if response.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Lambda response body must be a JSON object")
        return value

    @staticmethod
    def valid_graph_path(item: dict[str, Any]) -> bool:
        nodes = item.get("path")
        edges = item.get("edges")
        evidence = item.get("evidence")
        return (
            isinstance(nodes, list)
            and len(nodes) >= 3
            and isinstance(edges, list)
            and len(edges) == len(nodes) - 1
            and str(nodes[0]).startswith("Query:")
            and str(nodes[-1]).startswith("Job:")
            and isinstance(evidence, str)
            and bool(evidence.strip())
        )

    def check_api(self) -> None:
        from app.lambda_handler import RANKER, handler

        group = "G1_api_contract"
        health = handler(self.lambda_event("GET", "/health"), None)
        health_body = self.response_body(health)
        self.add(
            group,
            "G1.1",
            health["statusCode"] == 200 and health_body.get("status") == "ok",
            "Health endpoint returned 200/ok.",
            "Health endpoint did not return 200/ok.",
        )

        search = handler(
            self.lambda_event(
                "POST",
                "/api/v1/jobs/search",
                {"query": "後端工程師", "top_k": 5},
            ),
            None,
        )
        search_body = self.response_body(search)
        rows = search_body.get("result", [])
        ranks = [row.get("rank") for row in rows]
        job_ids = [row.get("job_id") for row in rows]
        self.add(
            group,
            "G1.2",
            (
                search["statusCode"] == 200
                and bool(rows)
                and ranks == list(range(1, len(rows) + 1))
                and len(job_ids) == len(set(job_ids))
            ),
            "Query-only search returned unique jobs with contiguous ranks.",
            "Query-only search violated the result/rank contract.",
        )

        legacy = handler(
            self.lambda_event(
                "POST", "/api/v1/jobs/search", {"ks": "行政助理", "top_k": 5}
            ),
            None,
        )
        self.add(
            group,
            "G1.3",
            legacy["statusCode"] == 200,
            "Legacy ks/c0/d0 aliases are accepted.",
            "Legacy request aliases were rejected.",
        )

        empty = handler(
            self.lambda_event("POST", "/api/v1/jobs/search", {"query": ""}), None
        )
        self.add(
            group,
            "G1.4",
            empty["statusCode"] == 400,
            "Empty query returns a client error.",
            "Empty query did not return HTTP 400.",
        )

        trace = handler(
            self.lambda_event(
                "POST",
                "/api/v1/graph/trace",
                {"query": "React", "top_k": 5},
            ),
            None,
        )
        trace_body = self.response_body(trace)
        self.add(
            group,
            "G1.5",
            trace["statusCode"] == 200 and isinstance(trace_body.get("trace"), list),
            "Graph trace endpoint returned a trace list.",
            "Graph trace endpoint violated its contract.",
        )

        unknown = handler(
            self.lambda_event(
                "POST",
                "/api/v1/jobs/search",
                {
                    "query": "工程師",
                    "location_code": ["unknown_xyz"],
                    "duty_code": ["unknown_xyz"],
                    "top_k": 5,
                },
            ),
            None,
        )
        self.add(
            group,
            "G1.6",
            unknown["statusCode"] == 200,
            "Unknown filter codes degrade safely.",
            "Unknown filter codes caused a request failure.",
        )

        graph_on = handler(
            self.lambda_event(
                "POST",
                "/api/v1/jobs/search",
                {
                    "query": "AWS Docker Kubernetes",
                    "top_k": 10,
                    "use_graph": True,
                },
            ),
            None,
        )
        graph_off = handler(
            self.lambda_event(
                "POST",
                "/api/v1/jobs/search",
                {
                    "query": "AWS Docker Kubernetes",
                    "top_k": 10,
                    "use_graph": False,
                },
            ),
            None,
        )
        graph_on_rows = self.response_body(graph_on).get("result", [])
        graph_off_rows = self.response_body(graph_off).get("result", [])
        self.add(
            group,
            "G1.7",
            (
                [row.get("job_id") for row in graph_on_rows]
                != [row.get("job_id") for row in graph_off_rows]
                and any(
                    float(row.get("features", {}).get("graph", 0.0)) > 0.0
                    for row in graph_on_rows
                )
            ),
            "Live graph toggle changes ranking with non-zero graph contribution.",
            "Live graph toggle does not affect the compact ranking.",
        )

        graph_group = "G2_graph_cutoff"
        manifest = self.load_json("release-manifest.json")
        self.add(
            graph_group,
            "G2.1",
            manifest.get("graph_cutoff") == GRAPH_CUTOFF,
            "Release graph cutoff matches the registered cutoff.",
            "Release graph cutoff changed unexpectedly.",
        )
        cold_jobs = [job for job in RANKER.jobs if not job.get("graph_eligible", False)]
        self.add(
            graph_group,
            "G2.2",
            bool(cold_jobs) and all(not job.get("skills") for job in cold_jobs),
            "Every cold-start demo job has an empty skill edge list.",
            "A cold-start demo job contains graph skills.",
        )
        paths = [
            path
            for row in trace_body.get("trace", [])
            for path in row.get("paths", [])
        ]
        self.add(
            graph_group,
            "G2.3",
            bool(paths) and all(self.valid_graph_path(path) for path in paths),
            "Runtime graph paths contain nodes, edges, and evidence.",
            "Runtime graph trace is absent or lacks provenance evidence.",
        )

    def check_model(self) -> None:
        group = "G3_model_manifest"
        try:
            model = self.load_json("artifacts/models/ltr-graph-final.manifest.json")
            loaded = True
        except (OSError, ValueError, json.JSONDecodeError):
            model = {}
            loaded = False
        self.add(
            group,
            "G3.1",
            loaded,
            "Final model manifest is valid JSON.",
            "Final model manifest is missing or invalid.",
        )
        expectations = [
            ("G3.2", model.get("lambdarank_unbiased") is True, "Unbiased LambdaMART is enabled."),
            ("G3.3", model.get("objective") == "rank:ndcg", "Ranking objective is rank:ndcg."),
            (
                "G3.4",
                model.get("n_estimators") == 20 and model.get("max_depth") == 2,
                "Registered 20-tree/depth-2 capacity is intact.",
            ),
            ("G3.5", model.get("random_seed") == 1111, "Model seed is 1111."),
        ]
        for check_id, condition, message in expectations:
            self.add(
                group,
                check_id,
                condition,
                message,
                f"{message[:-1]} does not match the release contract.",
            )

    def check_ablation(self, report: dict[str, Any] | None = None) -> None:
        group = "G4_ablation_report"
        report = report or self.load_json("reports/ltr-ablation-test.json")
        metadata = report.get("metadata", {})
        gates = report.get("release_gates", {})
        bootstrap = report.get("paired_bootstrap_ndcg", {})
        lift = report.get("relative_lift", {})
        conditions = [
            ("G4.1", metadata.get("schema") == "skillweave-ltr-ablation-v1", "Ablation schema is registered.", "Ablation schema is unexpected."),
            ("G4.2", int(metadata.get("queries", 0)) >= 1900, "Confirmation contains at least 1,900 queries.", "Confirmation query count is too small."),
            ("G4.3", metadata.get("confidence_gate") == "behavior_job_edge", "Registered confidence gate is active.", "Confirmation confidence gate changed."),
            ("G4.4", float(bootstrap.get("ci95_low", -1.0)) > 0.0, "Paired NDCG CI is entirely positive.", "Paired NDCG CI does not exclude zero."),
            ("G4.5", gates.get("ndcg_relative_lift_at_least_5pct") is False, "The unmet 5% gate remains honestly false.", "The 5% gate was flipped to an unsupported success."),
            ("G4.6", gates.get("paired_ci_excludes_zero") is True, "Report records a positive paired CI.", "Report does not record a positive paired CI."),
            ("G4.7", float(lift.get("ndcg@10", -1.0)) > 0.0, "Graph-on NDCG lift is positive.", "Graph-on NDCG lift is not positive."),
        ]
        for check_id, condition, success, failure in conditions:
            self.add(group, check_id, condition, success, failure)

    def check_failed_holdout(self, report: dict[str, Any] | None = None) -> None:
        group = "G5_failed_holdout"
        report = report or self.load_json("reports/ltr-ablation-holdout-1-failed.json")
        metadata = report.get("metadata", {})
        gates = report.get("release_gates", {})
        lift = report.get("relative_lift", {})
        conditions = [
            ("G5.1", isinstance(report, dict) and bool(report), "Failed holdout report is preserved.", "Failed holdout report is missing."),
            ("G5.2", gates.get("ndcg_relative_lift_at_least_5pct") is False, "Failed holdout did not pass the 5% gate.", "Failed holdout was rewritten as a 5% success."),
            ("G5.3", gates.get("paired_ci_excludes_zero") is False, "Failed holdout CI remains non-significant.", "Failed holdout CI was rewritten as significant."),
            ("G5.4", float(lift.get("ndcg@10", 1.0)) < 0.0, "Failed holdout retains negative NDCG lift.", "Failed holdout no longer records negative NDCG lift."),
            ("G5.5", int(metadata.get("queries", 0)) >= 100, "Failed holdout contains at least 100 queries.", "Failed holdout query count is too small."),
        ]
        for check_id, condition, success, failure in conditions:
            self.add(group, check_id, condition, success, failure)

    def check_load_smoke(self, report: dict[str, Any] | None = None) -> None:
        group = "G6_load_smoke"
        report = report or self.load_json("reports/load-smoke.json")
        requests = int(report.get("requests", 0))
        conditions = [
            ("G6.1", requests > 0 and int(report.get("http_200", -1)) == requests, "Every load-smoke request returned HTTP 200.", "Load smoke contains failed HTTP responses."),
            ("G6.2", float(report.get("p95_ms", 999999)) < 3000.0, "Compact demo p95 is below 3,000 ms.", "Compact demo p95 exceeds 3,000 ms."),
            ("G6.3", int(report.get("top_10_responses", -1)) == requests, "Every load-smoke response returned Top 10.", "A load-smoke response did not return Top 10."),
        ]
        for check_id, condition, success, failure in conditions:
            self.add(group, check_id, condition, success, failure)

    def check_graph_coverage(self, report: dict[str, Any] | None = None) -> None:
        group = "G8_graph_coverage"
        report = report or self.load_json("reports/graph-coverage.json")
        metadata = report.get("metadata", {})
        overall = report.get("authoritative_overall", {})
        coverage = report.get("coverage", {})
        active = report.get("post_hoc_subgroups", {}).get(
            "confidence_gate_active", {}
        )
        bootstrap = active.get("paired_bootstrap_ndcg", {})
        serialized = json.dumps(report, ensure_ascii=False)
        conditions = [
            (
                "G8.1",
                metadata.get("schema") == "skillweave-graph-coverage-v1"
                and metadata.get("analysis_status") == "post_hoc_descriptive",
                "Coverage report is explicitly registered as post-hoc.",
                "Coverage report is missing its post-hoc status.",
            ),
            (
                "G8.2",
                int(metadata.get("queries", 0)) == 1993
                and int(coverage.get("queries_total", 0)) == 1993,
                "Coverage report uses all 1,993 locked confirmation queries.",
                "Coverage report does not match the locked confirmation population.",
            ),
            (
                "G8.3",
                overall.get("five_percent_gate_passed") is False
                and abs(
                    float(overall.get("ndcg_at_10_relative_lift", 0.0))
                    - 0.013447251090391354
                )
                < 1e-12,
                "Coverage report preserves the authoritative overall result.",
                "Coverage report rewrites the authoritative overall result.",
            ),
            (
                "G8.4",
                int(active.get("queries", 0)) >= 100
                and float(active.get("ndcg_at_10_relative_lift", 0.0)) >= 0.05
                and float(bootstrap.get("ci95_low", -1.0)) > 0.0,
                "Gate-active subgroup has positive, >5% descriptive NDCG lift.",
                "Gate-active subgroup evidence is absent or unsupported.",
            ),
            (
                "G8.5",
                int(coverage.get("queries_with_any_graph_feature", 0))
                <= int(coverage.get("queries_total", -1))
                and int(coverage.get("relevant_rows_with_graph_feature", 0))
                <= int(coverage.get("relevant_rows_total", -1)),
                "Coverage counts respect their population bounds.",
                "Coverage counts exceed their population bounds.",
            ),
            (
                "G8.6",
                all(
                    token not in serialized
                    for token in ('"query_id"', '"job_id"', '"talentNo"', '"user_id"')
                ),
                "Coverage artifact contains aggregate evidence only.",
                "Coverage artifact contains row-level identifiers.",
            ),
        ]
        for check_id, condition, success, failure in conditions:
            self.add(group, check_id, condition, success, failure)

    def check_business_impact(self, report: dict[str, Any] | None = None) -> None:
        group = "G9_business_impact"
        report = report or self.load_json("reports/business-impact.json")
        metadata = report.get("metadata", {})
        observed = report.get("observed_offline_metrics", {})
        scale = report.get("scale_translation", {})
        financial = report.get("financial_claim", {})
        searches = int(scale.get("weekly_searches", 0))
        absolute_lift = float(observed.get("absolute_hit_at_1_lift", 0.0))
        conditions = [
            (
                "G9.1",
                metadata.get("schema") == "skillweave-business-impact-v1"
                and metadata.get("analysis_status")
                == "offline_scale_translation_not_causal",
                "Business report is registered as a non-causal scale translation.",
                "Business report is missing its non-causal analysis status.",
            ),
            (
                "G9.2",
                int(metadata.get("source_searches", 0)) == 6_139_952
                and int(metadata.get("source_ablation_queries", 0)) == 1993,
                "Business report uses the documented data and confirmation populations.",
                "Business report populations differ from release evidence.",
            ),
            (
                "G9.3",
                abs(
                    float(observed.get("skill_graph_hit_at_1", 0.0))
                    - float(observed.get("baseline_hit_at_1", 0.0))
                    - absolute_lift
                )
                < 1e-12
                and int(scale.get("rounded_incremental_top1_relevance_events", -1))
                == round(searches * absolute_lift),
                "Top-1 scale translation is arithmetically consistent.",
                "Top-1 scale translation is inconsistent.",
            ),
            (
                "G9.4",
                financial.get("currency_value_per_incremental_top1") is None
                and financial.get("estimated_revenue") is None,
                "Unsupported monetary value and revenue remain null.",
                "Business report contains an unsupported monetary claim.",
            ),
            (
                "G9.5",
                "not causal" in str(report.get("guardrail", "")).lower()
                and "revenue" in str(report.get("guardrail", "")).lower(),
                "Business report carries an explicit causal/revenue guardrail.",
                "Business report lacks its causal/revenue guardrail.",
            ),
        ]
        for check_id, condition, success, failure in conditions:
            self.add(group, check_id, condition, success, failure)

    def check_sam_local_smoke(self, report: dict[str, Any] | None = None) -> None:
        group = "G10_sam_local_smoke"
        report = report or self.load_json("reports/sam-local-smoke.json")
        metadata = report.get("metadata", {})
        checks = report.get("checks", {})
        observed = report.get("observed", {})
        required = {
            "sam_validate_lint",
            "sam_build",
            "sam_local_invoke",
            "http_200",
            "top_10",
            "contiguous_ranks",
            "unique_job_ids",
            "index_version",
            "graph_provenance",
        }
        conditions = [
            (
                "G10.1",
                metadata.get("schema") == "skillweave-sam-local-smoke-v1"
                and metadata.get("runtime") == "python3.13"
                and metadata.get("architecture") == "arm64",
                "SAM smoke used the deployment runtime and architecture.",
                "SAM smoke runtime or architecture differs from the template.",
            ),
            (
                "G10.2",
                report.get("passed") is True
                and required.issubset(checks)
                and all(checks.get(name) is True for name in required),
                "SAM validate, build, invoke, contract, and provenance checks passed.",
                "SAM local smoke is incomplete or contains a failed check.",
            ),
            (
                "G10.3",
                int(observed.get("result_count", 0)) == 10
                and int(observed.get("graph_path_count", 0)) > 0,
                "Packaged Lambda returned Top 10 with graph provenance.",
                "Packaged Lambda lacks Top 10 or graph provenance.",
            ),
            (
                "G10.4",
                0.0 < float(observed.get("duration_ms", 0.0)) < 10_000.0,
                "Local Lambda invocation completed below the 10-second timeout.",
                "Local Lambda invocation exceeded the configured timeout.",
            ),
        ]
        for check_id, condition, success, failure in conditions:
            self.add(group, check_id, condition, success, failure)

    def check_submission_audit(self, report: dict[str, Any] | None = None) -> None:
        group = "G11_submission_audit"
        report = report or self.load_json("reports/submission-audit.json")
        metadata = report.get("metadata", {})
        requirements = report.get("requirements", {})
        blockers = set(report.get("blockers", []))
        required_blockers = {
            "R1a_public_cloud_demo_url",
            "R1c_public_demo_video_url",
            "R5a_actual_aws_deployment",
            "R6_public_github",
            "R2a_full_train_only_bedrock_graph_executed",
        }
        local_required = {
            "R1_local_live_demo",
            "R1b_five_minute_video_artifact",
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
        }
        conditions = [
            (
                "G11.1",
                metadata.get("schema") == "skillweave-submission-audit-v1",
                "Submission audit schema is registered.",
                "Submission audit schema is unexpected.",
            ),
            (
                "G11.2",
                report.get("local_release_evidence_passed") is True
                and all(requirements.get(name) is True for name in local_required),
                "All locally achievable binding evidence is recorded complete.",
                "A locally achievable binding requirement is incomplete.",
            ),
            (
                "G11.3",
                report.get("submission_ready") is False
                and required_blockers.issubset(blockers),
                "Submission audit preserves all unresolved binding blockers.",
                "Submission audit overclaims final readiness.",
            ),
            (
                "G11.4",
                requirements.get("E2_recommended_five_percent_lift") is False,
                "Recommended 5% gap remains honestly false.",
                "Submission audit fabricates the recommended 5% result.",
            ),
        ]
        for check_id, condition, success, failure in conditions:
            self.add(group, check_id, condition, success, failure)

    def check_demo_video(self, report: dict[str, Any] | None = None) -> None:
        group = "G12_demo_video"
        report = report or self.load_json("reports/demo-video.json")
        manifest = self.load_json("release-manifest.json")
        metadata = report.get("metadata", {})
        checks = report.get("checks", {})
        artifact = report.get("artifact", {})
        relative = str(artifact.get("path", ""))
        expected = str(artifact.get("sha256", ""))
        path = self.root / relative if relative else self.root / "__missing__"
        registered = manifest.get("sha256", {}).get(relative)
        conditions = [
            (
                "G12.1",
                metadata.get("schema") == "skillweave-demo-video-v1"
                and metadata.get("language") == "zh-TW"
                and metadata.get("release") == manifest.get("release"),
                "Demo video schema, language, and release are registered.",
                "Demo video metadata differs from the release contract.",
            ),
            (
                "G12.2",
                report.get("passed") is True
                and len(checks) >= 6
                and all(value is True for value in checks.values()),
                "Five-minute Full HD video, audio, scenes, and subtitles passed.",
                "Demo video media checks are incomplete or failed.",
            ),
            (
                "G12.3",
                299.0 <= float(artifact.get("duration_seconds", 0.0)) <= 301.0
                and relative == "dist/skillweave-demo-5min.mp4"
                and len(expected) == 64
                and registered == expected,
                "Demo video duration, path, and immutable hash are registered.",
                "Demo video duration, path, or manifest hash is invalid.",
            ),
            (
                "G12.4",
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                (
                    "Local demo video hash matches the report."
                    if path.is_file()
                    else "Absent rebuildable video is covered by its source and report hash."
                ),
                "Local demo video hash differs from the registered artifact.",
            ),
            (
                "G12.5",
                bool(
                    manifest.get("external_deliverables", {}).get(
                        "demo_video_url"
                    )
                )
                or report.get("external_url_registered") is False,
                "Local video is complete without claiming an unregistered public URL.",
                "Video report fabricates external registration.",
            ),
            (
                "G12.6",
                all(
                    (self.root / relative).is_file()
                    for relative in (
                        "video/scenes.json",
                        "video/pitch-deck.html",
                        "scripts/render_demo_video.py",
                    )
                ),
                "Tracked deck, scene contract, and renderer make the video reproducible.",
                "A required demo-video source file is missing.",
            ),
        ]
        for check_id, condition, success, failure in conditions:
            self.add(group, check_id, condition, success, failure)

    def check_manifest(self) -> None:
        group = "G7_manifest_hashes"
        manifest = self.load_json("release-manifest.json")
        confirmation = manifest.get("confirmation", {})
        self.add(
            group,
            "G7.1",
            int(confirmation.get("queries", 0)) >= 1900,
            "Manifest registers the large confirmation split.",
            "Manifest confirmation query count is too small.",
        )
        self.add(
            group,
            "G7.2",
            confirmation.get("aspirational_five_percent_gate_passed") is False,
            "Manifest keeps the 5% gate false.",
            "Manifest makes an unsupported 5% claim.",
        )

        hashes = manifest.get("sha256", {})
        failures: list[str] = []
        skips: list[str] = []
        for relative, expected in sorted(hashes.items()):
            path = self.root / relative
            if not path.is_file():
                if relative.startswith("dist/"):
                    skips.append(relative)
                    continue
                failures.append(f"{relative}: missing")
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                failures.append(f"{relative}: mismatch")
        self.add(
            group,
            "G7.3",
            not failures,
            f"Verified {len(hashes) - len(skips)} artifact hashes"
            + (f"; skipped absent rebuildable {', '.join(skips)}." if skips else "."),
            "Artifact hash failures: " + ", ".join(failures),
        )

        required = [
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
        ]
        missing = [item for item in required if not (self.root / item).is_file()]
        self.add(
            group,
            "G7.4",
            not missing,
            "All required API/graph/AWS/submission files exist.",
            "Missing required files: " + ", ".join(missing),
        )

        from app.lambda_handler import RANKER

        self.add(
            group,
            "G7.5",
            RANKER.metadata.get("index_version")
            == manifest.get("demo_index_version"),
            "Runtime demo index version matches the release manifest.",
            "Runtime demo index version differs from the release manifest.",
        )

        external = manifest.get("external_deliverables", {})
        for index, key in enumerate(("aws_url", "github_url", "demo_video_url"), start=6):
            value = external.get(key)
            if not value:
                self.add(
                    group,
                    f"G7.{index}",
                    False,
                    f"External deliverable {key} is registered.",
                    f"External deliverable {key} is still null.",
                    warning=True,
                )
                continue
            from scripts.update_release_urls import validate_public_https_url

            try:
                validate_public_https_url(str(value))
                valid = True
            except argparse.ArgumentTypeError:
                valid = False
            self.add(
                group,
                f"G7.{index}",
                valid,
                f"External deliverable {key} is a public HTTPS URL.",
                f"External deliverable {key} is not a valid public HTTPS URL.",
            )

    def run(self) -> dict[str, Any]:
        checks: list[Callable[[], None]] = [
            self.check_api,
            self.check_model,
            self.check_ablation,
            self.check_failed_holdout,
            self.check_load_smoke,
            self.check_graph_coverage,
            self.check_business_impact,
            self.check_sam_local_smoke,
            self.check_demo_video,
            self.check_submission_audit,
            self.check_manifest,
        ]
        for check in checks:
            try:
                check()
            except Exception as exc:
                self.groups.setdefault(f"fatal_{check.__name__}", []).append(
                    Check(
                        f"FATAL.{check.__name__}",
                        "FAIL",
                        f"{type(exc).__name__}: {exc}",
                    )
                )
        all_checks = [item for items in self.groups.values() for item in items]
        passed = sum(item.status == "PASS" for item in all_checks)
        failed = sum(item.status == "FAIL" for item in all_checks)
        warnings = sum(item.status == "WARN" for item in all_checks)
        try:
            release = self.load_json("release-manifest.json").get("release")
        except Exception:
            release = None
        return {
            "verifier_version": "1",
            "verified_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "release": release,
            "passed": failed == 0,
            "summary": {
                "total": len(all_checks),
                "passed": passed,
                "failed": failed,
                "warnings": warnings,
            },
            "groups": {
                name: {
                    "passed": all(item.status != "FAIL" for item in items),
                    "checks": [asdict(item) for item in items],
                }
                for name, items in self.groups.items()
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify SkillWeave release evidence")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        report = ReleaseVerifier().run()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    except Exception as exc:
        fatal = {
            "verifier_version": "1",
            "passed": False,
            "fatal_error": f"{type(exc).__name__}: {exc}",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(fatal, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(fatal, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
