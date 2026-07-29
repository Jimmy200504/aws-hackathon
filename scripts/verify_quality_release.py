#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTS = [
    ROOT / "reports" / "ltr-quality-confirmation.json",
    ROOT / "reports" / "ltr-quality-replication.json",
]
EXPECTED_BUCKETS = [(2400, 3400), (3400, 4400)]


def main() -> None:
    checks: dict[str, bool] = {}
    model_signatures: list[tuple] = []
    intervals: list[tuple[int, int]] = []
    for index, (path, expected_bucket) in enumerate(
        zip(REPORTS, EXPECTED_BUCKETS), 1
    ):
        report = json.loads(path.read_text(encoding="utf-8"))
        metadata = report["metadata"]
        fixture = metadata["evaluation_fixture"]
        model = metadata["graph_model"]
        prefix = f"confirmation_{index}"
        checks[f"{prefix}_minimum_queries"] = metadata["queries"] >= 1000
        checks[f"{prefix}_ndcg_lift_ge_5pct"] = (
            report["relative_lift"]["ndcg@10"] >= 0.05
        )
        checks[f"{prefix}_ci_positive"] = (
            report["paired_bootstrap_ndcg"]["ci95_low"] > 0
        )
        checks[f"{prefix}_expected_bucket"] = (
            fixture["test_sample_bucket_start"],
            fixture["test_sample_bucket_end_exclusive"],
        ) == expected_bucket
        checks[f"{prefix}_same_model_ablation"] = (
            metadata["ablation_design"]
            == "same trained model; graph feature family zeroed at inference"
        )
        model_signatures.append(
            (
                model["model"],
                model["feature_set"],
                tuple(model["features"]),
                model["n_estimators"],
                model["max_depth"],
                model["min_child_weight"],
                model["learning_rate"],
            )
        )
        intervals.append(expected_bucket)
    checks["same_frozen_model"] = len(set(model_signatures)) == 1
    checks["confirmation_buckets_disjoint"] = (
        intervals[0][1] <= intervals[1][0]
        or intervals[1][1] <= intervals[0][0]
    )
    result = {"passed": all(checks.values()), "checks": checks}
    output = ROOT / "reports" / "verify-quality-release.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
