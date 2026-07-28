#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.evaluate_ltr import (
    load_model,
    paired_bootstrap,
    read_grouped_rows,
    score_model,
)
from pipeline.train_ltr import BASE_FEATURES
from scripts.benchmark import aggregate


def nonzero(row: dict[str, Any], names: list[str]) -> bool:
    return any(float(row["features"].get(name, 0.0)) != 0.0 for name in names)


def subgroup(
    indices: list[int],
    baseline_rows: list[dict[str, float]],
    graph_rows: list[dict[str, float]],
) -> dict[str, Any]:
    baseline = aggregate([baseline_rows[index] for index in indices])
    graph = aggregate([graph_rows[index] for index in indices])
    deltas = [
        graph_rows[index]["ndcg@10"] - baseline_rows[index]["ndcg@10"]
        for index in indices
    ]
    return {
        "queries": len(indices),
        "baseline_no_graph": baseline,
        "skill_graph": graph,
        "ndcg_at_10_relative_lift": (
            graph["ndcg@10"] / baseline["ndcg@10"] - 1
            if baseline["ndcg@10"]
            else None
        ),
        "paired_bootstrap_ndcg": paired_bootstrap(
            [baseline_rows[index] for index in indices],
            [graph_rows[index] for index in indices],
            repeats=3000,
        ),
        "query_outcomes": {
            "improved": sum(delta > 1e-12 for delta in deltas),
            "regressed": sum(delta < -1e-12 for delta in deltas),
            "unchanged": sum(abs(delta) <= 1e-12 for delta in deltas),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report aggregate graph coverage on the locked confirmation pairs"
    )
    parser.add_argument(
        "--pairs",
        type=Path,
        default=ROOT / "artifacts" / "ltr" / "test.jsonl",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=ROOT / "artifacts" / "models" / "ltr-graph-final.ubj",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=ROOT / "artifacts" / "benchmark-index.json",
    )
    parser.add_argument(
        "--ablation",
        type=Path,
        default=ROOT / "reports" / "ltr-ablation-test.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports" / "graph-coverage.json",
    )
    args = parser.parse_args()

    model, manifest = load_model(args.model)
    groups = read_grouped_rows(args.pairs)
    baseline_rows = score_model(
        model,
        manifest,
        groups,
        allowed_features=set(BASE_FEATURES),
    )
    graph_rows = score_model(
        model,
        manifest,
        groups,
        confidence_gate="behavior_job_edge",
    )
    graph_features = [
        name for name in manifest["features"] if name not in set(BASE_FEATURES)
    ]

    gate_active: list[int] = []
    graph_present: list[int] = []
    relevant_graph_covered: list[int] = []
    candidate_rows = graph_candidate_rows = 0
    relevant_rows = graph_relevant_rows = 0
    for index, group in enumerate(groups):
        if any(
            float(row["features"].get("behavior_query_job_seen", 0.0)) > 0.0
            for row in group
        ):
            gate_active.append(index)
        if any(nonzero(row, graph_features) for row in group):
            graph_present.append(index)
        positives = [row for row in group if int(row["label"]) > 0]
        if any(nonzero(row, graph_features) for row in positives):
            relevant_graph_covered.append(index)
        candidate_rows += len(group)
        graph_candidate_rows += sum(nonzero(row, graph_features) for row in group)
        relevant_rows += len(positives)
        graph_relevant_rows += sum(nonzero(row, graph_features) for row in positives)

    index_artifact = json.loads(args.index.read_text(encoding="utf-8"))
    authoritative = json.loads(args.ablation.read_text(encoding="utf-8"))
    if authoritative["metadata"]["queries"] != len(groups):
        raise ValueError("coverage pairs do not match authoritative query count")

    importances = {
        name: float(value)
        for name, value in zip(manifest["features"], model.feature_importances_)
    }
    graph_importances = {
        name: importances[name]
        for name in graph_features
        if importances[name] > 0.0
    }
    report = {
        "metadata": {
            "schema": "skillweave-graph-coverage-v1",
            "split": "test",
            "queries": len(groups),
            "random_seed": 1111,
            "confidence_gate": "behavior_job_edge",
            "analysis_status": "post_hoc_descriptive",
            "privacy": "aggregate counts only; no query, user, or job identifiers",
        },
        "authoritative_overall": {
            "report": "reports/ltr-ablation-test.json",
            "ndcg_at_10_relative_lift": authoritative["relative_lift"]["ndcg@10"],
            "paired_ci95": [
                authoritative["paired_bootstrap_ndcg"]["ci95_low"],
                authoritative["paired_bootstrap_ndcg"]["ci95_high"],
            ],
            "five_percent_gate_passed": authoritative["release_gates"][
                "ndcg_relative_lift_at_least_5pct"
            ],
        },
        "graph_inventory": index_artifact["metadata"]["stats"],
        "coverage": {
            "queries_total": len(groups),
            "queries_with_any_graph_feature": len(graph_present),
            "queries_with_any_graph_feature_rate": len(graph_present) / len(groups),
            "queries_with_relevant_graph_covered": len(relevant_graph_covered),
            "queries_with_relevant_graph_covered_rate": (
                len(relevant_graph_covered) / len(groups)
            ),
            "confidence_gate_active_queries": len(gate_active),
            "confidence_gate_active_rate": len(gate_active) / len(groups),
            "candidate_rows_total": candidate_rows,
            "candidate_rows_with_graph_feature": graph_candidate_rows,
            "candidate_row_graph_coverage_rate": (
                graph_candidate_rows / candidate_rows
            ),
            "relevant_rows_total": relevant_rows,
            "relevant_rows_with_graph_feature": graph_relevant_rows,
            "relevant_row_graph_coverage_rate": graph_relevant_rows / relevant_rows,
        },
        "post_hoc_subgroups": {
            "confidence_gate_active": subgroup(
                gate_active, baseline_rows, graph_rows
            ),
            "relevant_graph_covered": subgroup(
                relevant_graph_covered, baseline_rows, graph_rows
            ),
        },
        "model_reliance": {
            "measure": "XGBoost normalized gain; descriptive, not causal",
            "graph_feature_gain_share": sum(graph_importances.values()),
            "nonzero_graph_features": dict(
                sorted(
                    graph_importances.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ),
        },
        "interpretation_guardrail": (
            "Subgroup results explain dilution and may guide coverage work. "
            "They are post-hoc diagnostics and do not replace the locked overall "
            "confirmation or make the unmet overall 5% gate pass."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
