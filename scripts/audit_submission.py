#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports" / "submission-audit.json"


def load_object(relative: str) -> dict[str, Any]:
    value = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{relative} must contain a JSON object")
    return value


def exists(relative: str) -> bool:
    return (ROOT / relative).is_file()


def main() -> None:
    manifest = load_object("release-manifest.json")
    verifier = load_object("reports/verify-release.json")
    ablation = load_object("reports/ltr-ablation-test.json")
    video = (
        load_object("reports/demo-video.json")
        if exists("reports/demo-video.json")
        else {}
    )
    external = manifest.get("external_deliverables", {})
    requirements = {
        "R1_local_live_demo": exists("web/index.html") and verifier.get("passed") is True,
        "R1a_public_cloud_demo_url": bool(external.get("aws_url")),
        "R1b_five_minute_video_artifact": (
            video.get("passed") is True
            and video.get("metadata", {}).get("release") == manifest.get("release")
        ),
        "R1c_public_demo_video_url": bool(external.get("demo_video_url")),
        "R2_genai_method_and_failure_modes": (
            exists("pipeline/bedrock_extract.py")
            and exists("pipeline/graph_validation.py")
            and exists("docs/genai-safety.md")
        ),
        "R2a_full_train_only_bedrock_graph_executed": False,
        "R3_data_application_explained": exists("docs/data-card.md"),
        "R4_system_graph_schema_and_trace": (
            exists("docs/graph-schema.md")
            and verifier.get("groups", {}).get("G2_graph_cutoff", {}).get("passed")
            is True
        ),
        "R5_aws_architecture": exists("docs/aws-architecture.md"),
        "R5a_actual_aws_deployment": bool(external.get("aws_url")),
        "R6_public_github": bool(external.get("github_url")),
        "R6a_reproducible_source_and_ablation": (
            exists("scripts/run_ltr_ablation.sh")
            and exists("requirements-ltr.lock")
            and exists("release-manifest.json")
        ),
        "E1_quantifiable_ndcg_improvement": (
            float(ablation.get("relative_lift", {}).get("ndcg@10", 0.0)) > 0.0
        ),
        "E2_recommended_five_percent_lift": (
            ablation.get("release_gates", {}).get(
                "ndcg_relative_lift_at_least_5pct"
            )
            is True
        ),
        "E3_hit1_and_hit10_reported": (
            "hit@1" in ablation.get("skill_graph", {})
            and "hit@10" in ablation.get("skill_graph", {})
        ),
        "E4_position_bias_status_reported": (
            ablation.get("metadata", {}).get("position_bias_correction")
            == "XGBoost Unbiased LambdaMART"
        ),
        "E5_api_contract_verified": (
            verifier.get("groups", {}).get("G1_api_contract", {}).get("passed")
            is True
        ),
        "B1_business_case_and_ab_design": (
            exists("docs/business-case.md")
            and verifier.get("groups", {}).get("G9_business_impact", {}).get(
                "passed"
            )
            is True
        ),
        "K1_kiro_activity_evidence": exists("docs/kiro-evidence.md"),
    }
    mandatory_external = [
        "R1a_public_cloud_demo_url",
        "R1c_public_demo_video_url",
        "R5a_actual_aws_deployment",
        "R6_public_github",
    ]
    blockers = [
        name for name in mandatory_external if not requirements[name]
    ]
    if not requirements["R2a_full_train_only_bedrock_graph_executed"]:
        blockers.append("R2a_full_train_only_bedrock_graph_executed")
    report = {
        "metadata": {
            "schema": "skillweave-submission-audit-v1",
            "release": manifest.get("release"),
            "source": "binding brief + workshop contract + current release evidence",
        },
        "local_release_evidence_passed": verifier.get("passed") is True,
        "submission_ready": not blockers,
        "requirements": requirements,
        "blockers": blockers,
        "recommended_not_mandatory_gap": (
            None
            if requirements["E2_recommended_five_percent_lift"]
            else "Locked NDCG@10 lift is positive but below the recommended 5%."
        ),
        "interpretation": (
            "Local evidence can be release-ready while the binding public "
            "deployment, hosted video URL, GitHub, and executed Bedrock graph "
            "remain incomplete."
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest.setdefault("sha256", {})[
        "reports/submission-audit.json"
    ] = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    (ROOT / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
