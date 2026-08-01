#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.train_ltr import (
    BASE_FEATURES,
    LLM_GRAPH_FEATURES,
    RETRIEVAL_FEATURES,
    load_groups,
)
from scripts.benchmark import aggregate, metrics

ABLATION_BASE_FEATURES = set([*BASE_FEATURES, *RETRIEVAL_FEATURES])
LLM_ABLATION_FEATURES = set(LLM_GRAPH_FEATURES)


def baseline_allowed_features(scope: str, manifest: dict) -> set[str]:
    """Features the control arm may read.

    graph_off keeps only text/condition/retrieval evidence. llm_off keeps every
    reviewed-graph and behavior feature and removes just the Bedrock-derived
    family, which isolates the generative-AI contribution.
    """
    if scope == "graph_off":
        return set(ABLATION_BASE_FEATURES)
    return set(manifest["features"]) - LLM_ABLATION_FEATURES


def load_model(path: Path):
    import xgboost as xgb

    model = xgb.XGBRanker()
    model.load_model(path)
    manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    return model, manifest


def read_grouped_rows(path: Path) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current_id = None
    current: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if current_id is not None and row["query_id"] != current_id:
                groups.append(current)
                current = []
            current_id = row["query_id"]
            current.append(row)
    if current:
        groups.append(current)
    return groups


def score_model(
    model,
    manifest: dict,
    groups: list[list[dict]],
    allowed_features: set[str] | None = None,
    confidence_gate: str = "none",
) -> list[dict[str, float]]:
    import numpy as np

    output = []
    features = manifest["features"]
    for group in groups:
        group_allowed_features = allowed_features
        if confidence_gate == "behavior_job_edge" and not any(
            row["features"].get("behavior_query_job_seen", 0.0) > 0
            for row in group
        ):
            group_allowed_features = ABLATION_BASE_FEATURES
        elif confidence_gate == "behavior_or_skill_edge" and not any(
            row["features"].get("behavior_query_job_seen", 0.0) > 0
            or row["features"].get("behavior_query_skill_seen_count", 0.0) > 0
            for row in group
        ):
            group_allowed_features = ABLATION_BASE_FEATURES
        elif confidence_gate == "behavior_or_direct" and not any(
            row["features"].get("behavior_query_job_seen", 0.0) > 0
            or row["features"].get("seed_direct_match_count", 0.0) > 0
            for row in group
        ):
            group_allowed_features = ABLATION_BASE_FEATURES
        elif confidence_gate == "direct" and not any(
            row["features"].get("seed_direct_match_count", 0.0) > 0
            for row in group
        ):
            group_allowed_features = ABLATION_BASE_FEATURES
        elif confidence_gate == "seed_active" and not any(
            row["features"].get("seed_graph_raw", 0.0) > 0 for row in group
        ):
            group_allowed_features = ABLATION_BASE_FEATURES
        x = np.asarray(
            [
                [
                    (
                        float(row["features"].get(name, 0.0))
                        if group_allowed_features is None
                        or name in group_allowed_features
                        else 0.0
                    )
                    for name in features
                ]
                for row in group
            ],
            dtype=np.float32,
        )
        predictions = model.predict(x)
        ranked = [
            row["job_id"]
            for _, row in sorted(
                zip(predictions, group),
                key=lambda item: (-float(item[0]), item[1]["job_id"]),
            )
        ]
        qrels = {row["job_id"]: int(row["label"]) for row in group}
        output.append(metrics(ranked, qrels))
    return output


def paired_bootstrap(
    baseline: list[dict[str, float]],
    graph: list[dict[str, float]],
    repeats: int = 3000,
) -> dict[str, float]:
    rng = random.Random(1111)
    deltas = [
        graph_row["ndcg@10"] - base_row["ndcg@10"]
        for base_row, graph_row in zip(baseline, graph)
    ]
    samples = sorted(
        mean(deltas[rng.randrange(len(deltas))] for _ in deltas)
        for _ in range(repeats)
    )
    return {
        "mean": mean(deltas),
        "ci95_low": samples[int(0.025 * (repeats - 1))],
        "ci95_high": samples[int(0.975 * (repeats - 1))],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate baseline and graph LTR models")
    parser.add_argument("--baseline-model", type=Path)
    parser.add_argument("--graph-model", type=Path, required=True)
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument(
        "--qrels",
        type=Path,
        help="Optional temporal-eval JSON used to bind split provenance",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=["validation", "test"], required=True)
    parser.add_argument(
        "--confidence-gate",
        choices=[
            "none",
            "behavior_job_edge",
            "behavior_or_skill_edge",
            "behavior_or_direct",
            "direct",
            "seed_active",
        ],
        default="none",
    )
    parser.add_argument(
        "--baseline-scope",
        choices=["graph_off", "llm_off"],
        default="graph_off",
        help="Which feature families the control arm loses",
    )
    args = parser.parse_args()
    graph_model, graph_manifest = load_model(args.graph_model)
    groups = read_grouped_rows(args.pairs)
    if args.baseline_model:
        baseline_model, baseline_manifest = load_model(args.baseline_model)
        ablation_design = "independently trained no-graph and graph models"
        baseline_rows = score_model(baseline_model, baseline_manifest, groups)
    else:
        baseline_model, baseline_manifest = graph_model, graph_manifest
        ablation_design = (
            "same trained model; graph feature family zeroed at inference"
            if args.baseline_scope == "graph_off"
            else "same trained model; only Bedrock-derived LLM graph family "
            "zeroed at inference"
        )
        baseline_rows = score_model(
            baseline_model,
            baseline_manifest,
            groups,
            allowed_features=baseline_allowed_features(
                args.baseline_scope, baseline_manifest
            ),
        )
    graph_rows = score_model(
        graph_model,
        graph_manifest,
        groups,
        confidence_gate=args.confidence_gate,
    )
    baseline = aggregate(baseline_rows)
    graph = aggregate(graph_rows)
    lift = {
        name: graph[name] / baseline[name] - 1 if baseline[name] else None
        for name in baseline
    }
    bootstrap = paired_bootstrap(baseline_rows, graph_rows)
    report = {
        "metadata": {
            "schema": "skillweave-ltr-ablation-v1",
            "split": args.split,
            "queries": len(groups),
            "random_seed": 1111,
            "position_bias_correction": "XGBoost Unbiased LambdaMART",
            "ablation_design": ablation_design,
            "baseline_scope": args.baseline_scope,
            "confidence_gate": args.confidence_gate,
            "baseline_model": baseline_manifest,
            "graph_model": graph_manifest,
            "evaluation_fixture": (
                json.loads(args.qrels.read_text(encoding="utf-8"))["metadata"]
                if args.qrels is not None
                else None
            ),
        },
        "baseline_no_graph": baseline,
        "skill_graph": graph,
        "relative_lift": lift,
        "paired_bootstrap_ndcg": bootstrap,
        "release_gates": {
            "minimum_queries_100": len(groups) >= 100,
            "ndcg_relative_lift_at_least_5pct": bool(
                lift["ndcg@10"] is not None and lift["ndcg@10"] >= 0.05
            ),
            "paired_ci_excludes_zero": bootstrap["ci95_low"] > 0,
        },
    }
    report["release_gates"]["theme_gate_passed"] = (
        report["release_gates"]["minimum_queries_100"]
        and report["release_gates"]["ndcg_relative_lift_at_least_5pct"]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
