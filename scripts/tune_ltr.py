#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np
import xgboost as xgb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.train_ltr import BASE_FEATURES, BEHAVIOR_GRAPH_FEATURES
from scripts.benchmark import aggregate, metrics


DEFAULT_TRAIN = ROOT / "artifacts" / "ltr" / "train.jsonl"
DEFAULT_VALIDATION = ROOT / "artifacts" / "ltr" / "validation.jsonl"
DEFAULT_OUTPUT = ROOT / "reports" / "ltr-tuning-validation.json"
DEFAULT_MODEL = ROOT / "artifacts" / "models" / "ltr-quality-candidate.ubj"

RETRIEVAL_FEATURES = [
    "retrieval_rank",
    "retrieval_reciprocal_rank",
    "retrieval_log_rank",
    "retrieval_top1",
    "retrieval_top3",
    "retrieval_top10",
]

FULL_GRAPH_FEATURES = [
    "seed_graph_raw",
    "technical_graph_raw",
    "seed_occupation_graph_raw",
    "duty_occupation_graph_raw",
    "seed_direct_match_count",
    "llm_skill_match_count",
    "seed_occupation_match_count",
    "duty_occupation_match_count",
    "seed_related_path_count",
    "related_path_count",
    "seed_job_skill_count",
    "technical_job_skill_count",
    "seed_occupation_job_skill_count",
    "duty_occupation_job_skill_count",
    "seed_graph_cold_start",
    "cold_start",
]

BEHAVIOR_FEATURES = [
    "behavior_query_seen",
    "behavior_query_job_seen",
    "behavior_query_job_positive_rate",
    "behavior_query_job_grade_rate",
    "behavior_query_job_exposures",
    "behavior_query_skill_seen_count",
    "behavior_query_skill_positive_rate",
    "behavior_query_skill_grade_rate",
    "behavior_query_skill_max_positive_rate",
    "behavior_job_global_seen",
    "behavior_job_global_exposures_log",
    "behavior_job_global_positive_rate",
    "behavior_job_global_grade_rate",
    "behavior_job_global_positive_rate_smoothed",
    "behavior_job_global_grade_rate_smoothed",
    "behavior_company_global_seen",
    "behavior_company_global_exposures_log",
    "behavior_company_global_positive_rate",
    "behavior_company_global_grade_rate",
    "behavior_company_global_positive_rate_smoothed",
    "behavior_company_global_grade_rate_smoothed",
]

FEATURE_SETS = {
    "current": list(dict.fromkeys(BEHAVIOR_GRAPH_FEATURES)),
    "base_retrieval": list(dict.fromkeys([*BASE_FEATURES, *RETRIEVAL_FEATURES])),
    "current_retrieval": list(
        dict.fromkeys([*BEHAVIOR_GRAPH_FEATURES, *RETRIEVAL_FEATURES])
    ),
    "current_retrieval_smooth": list(
        dict.fromkeys(
            [
                *BEHAVIOR_GRAPH_FEATURES,
                "retrieval_rank",
                "retrieval_reciprocal_rank",
                "retrieval_log_rank",
            ]
        )
    ),
    "current_retrieval_minimal": list(
        dict.fromkeys(
            [*BEHAVIOR_GRAPH_FEATURES, "retrieval_reciprocal_rank"]
        )
    ),
    "current_retrieval_minimal_no_global": list(
        dict.fromkeys(
            [
                *[
                    name
                    for name in BEHAVIOR_GRAPH_FEATURES
                    if not name.startswith("behavior_job_global")
                    and not name.startswith("behavior_company_global")
                ],
                "retrieval_reciprocal_rank",
            ]
        )
    ),
    "current_retrieval_minimal_no_smoothing": list(
        dict.fromkeys(
            [
                *[
                    name
                    for name in BEHAVIOR_GRAPH_FEATURES
                    if not name.endswith("_smoothed")
                ],
                "retrieval_reciprocal_rank",
            ]
        )
    ),
    "current_retrieval_company_only": list(
        dict.fromkeys(
            [
                *[
                    name
                    for name in BEHAVIOR_GRAPH_FEATURES
                    if not name.startswith("behavior_job_global")
                ],
                "retrieval_reciprocal_rank",
            ]
        )
    ),
    "current_retrieval_smoothed_globals": list(
        dict.fromkeys(
            [
                *[
                    name
                    for name in BEHAVIOR_GRAPH_FEATURES
                    if (
                        not name.startswith("behavior_job_global")
                        and not name.startswith("behavior_company_global")
                    )
                    or name.endswith("_seen")
                    or name.endswith("_exposures_log")
                    or name.endswith("_smoothed")
                ],
                "retrieval_reciprocal_rank",
            ]
        )
    ),
    "current_retrieval_buckets": list(
        dict.fromkeys(
            [
                *BEHAVIOR_GRAPH_FEATURES,
                "retrieval_reciprocal_rank",
                "retrieval_top1",
                "retrieval_top3",
                "retrieval_top10",
            ]
        )
    ),
    "full": list(
        dict.fromkeys([*BASE_FEATURES, *FULL_GRAPH_FEATURES, *BEHAVIOR_FEATURES])
    ),
    "full_retrieval": list(
        dict.fromkeys(
            [
                *BASE_FEATURES,
                *RETRIEVAL_FEATURES,
                *FULL_GRAPH_FEATURES,
                *BEHAVIOR_FEATURES,
            ]
        )
    ),
}

BASELINE_FEATURES = set([*BASE_FEATURES, *RETRIEVAL_FEATURES])


@dataclass(frozen=True)
class TrialConfig:
    n_estimators: int
    max_depth: int
    learning_rate: float
    min_child_weight: float

    @property
    def name(self) -> str:
        return (
            f"trees{self.n_estimators}-depth{self.max_depth}"
            f"-lr{self.learning_rate:g}-child{self.min_child_weight:g}"
        )


TRIAL_CONFIGS = [
    TrialConfig(20, 2, 0.10, 8),
    TrialConfig(50, 2, 0.05, 8),
    TrialConfig(100, 2, 0.03, 8),
    TrialConfig(40, 3, 0.05, 8),
    TrialConfig(80, 3, 0.03, 8),
    TrialConfig(40, 4, 0.05, 12),
]


def read_groups(path: Path) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current_id: str | None = None
    current: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            query_id = row["query_id"]
            if current_id is not None and query_id != current_id:
                groups.append(current)
                current = []
            current_id = query_id
            current.append(row)
    if current:
        groups.append(current)
    return groups


def feature_value(row: dict[str, Any], name: str) -> float:
    rank = max(1, int(row["exposure_rank"]))
    if name == "retrieval_rank":
        return float(rank)
    if name == "retrieval_reciprocal_rank":
        return 1.0 / rank
    if name == "retrieval_log_rank":
        return math.log1p(rank)
    if name == "retrieval_top1":
        return float(rank == 1)
    if name == "retrieval_top3":
        return float(rank <= 3)
    if name == "retrieval_top10":
        return float(rank <= 10)
    return float(row["features"].get(name, 0.0))


def matrix(
    groups: list[list[dict[str, Any]]],
    features: list[str],
) -> tuple[np.ndarray, np.ndarray, list[int]]:
    rows = [row for group in groups for row in group]
    x = np.asarray(
        [[feature_value(row, name) for name in features] for row in rows],
        dtype=np.float32,
    )
    y = np.asarray([int(row["label"]) for row in rows], dtype=np.int32)
    return x, y, [len(group) for group in groups]


def gate_active(group: list[dict[str, Any]], gate: str) -> bool:
    if gate == "off":
        return False
    if gate == "none":
        return True
    if gate == "behavior_job_edge":
        return any(
            feature_value(row, "behavior_query_job_seen") > 0 for row in group
        )
    if gate == "behavior_or_skill_edge":
        return any(
            feature_value(row, "behavior_query_job_seen") > 0
            or feature_value(row, "behavior_query_skill_seen_count") > 0
            for row in group
        )
    if gate == "behavior_or_direct":
        return any(
            feature_value(row, "behavior_query_job_seen") > 0
            or feature_value(row, "seed_direct_match_count") > 0
            for row in group
        )
    if gate == "direct":
        return any(feature_value(row, "seed_direct_match_count") > 0 for row in group)
    if gate == "seed_active":
        return any(feature_value(row, "seed_graph_raw") > 0 for row in group)
    raise ValueError(f"unknown gate: {gate}")


def score_groups(
    model: xgb.XGBRanker,
    groups: list[list[dict[str, Any]]],
    features: list[str],
    gate: str,
) -> tuple[dict[str, float], int, list[dict[str, float]]]:
    rows: list[dict[str, float]] = []
    active = 0
    base_indices = [index for index, name in enumerate(features) if name in BASELINE_FEATURES]
    for group in groups:
        x = np.asarray(
            [[feature_value(row, name) for name in features] for row in group],
            dtype=np.float32,
        )
        if not gate_active(group, gate):
            graph_x = np.zeros_like(x)
            graph_x[:, base_indices] = x[:, base_indices]
            x = graph_x
        else:
            active += 1
        predictions = model.predict(x)
        ranked = [
            row["job_id"]
            for _, row in sorted(
                zip(predictions, group),
                key=lambda item: (-float(item[0]), item[1]["job_id"]),
            )
        ]
        qrels = {row["job_id"]: int(row["label"]) for row in group}
        rows.append(metrics(ranked, qrels))
    return aggregate(rows), active, rows


def fit(
    train_groups: list[list[dict[str, Any]]],
    validation_groups: list[list[dict[str, Any]]],
    features: list[str],
    config: TrialConfig,
) -> xgb.XGBRanker:
    x_train, y_train, train_sizes = matrix(train_groups, features)
    x_validation, y_validation, validation_sizes = matrix(
        validation_groups, features
    )
    model = xgb.XGBRanker(
        objective="rank:ndcg",
        eval_metric="ndcg@10",
        tree_method="hist",
        n_estimators=config.n_estimators,
        learning_rate=config.learning_rate,
        max_depth=config.max_depth,
        min_child_weight=config.min_child_weight,
        subsample=0.8,
        colsample_bytree=0.82,
        reg_alpha=0.2,
        reg_lambda=2.0,
        lambdarank_unbiased=True,
        lambdarank_pair_method="topk",
        lambdarank_num_pair_per_sample=12,
        random_state=1111,
        n_jobs=-1,
    )
    model.fit(
        x_train,
        y_train,
        group=train_sizes,
        eval_set=[(x_validation, y_validation)],
        eval_group=[validation_sizes],
        verbose=False,
    )
    return model


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validation-only LTR feature, model, and confidence-gate search"
    )
    parser.add_argument("--train", type=Path, default=DEFAULT_TRAIN)
    parser.add_argument("--validation", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument(
        "--secondary-validation",
        type=Path,
        help="Optional already-open development holdout; never use a locked confirmation set",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--best-model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument(
        "--feature-sets",
        default=",".join(FEATURE_SETS),
        help="Comma-separated feature set names",
    )
    args = parser.parse_args()

    selected_sets = [name for name in args.feature_sets.split(",") if name]
    unknown = sorted(set(selected_sets) - FEATURE_SETS.keys())
    if unknown:
        raise SystemExit(f"unknown feature sets: {', '.join(unknown)}")

    train_groups = read_groups(args.train)
    validation_groups = read_groups(args.validation)
    secondary_groups = (
        read_groups(args.secondary_validation)
        if args.secondary_validation is not None
        else None
    )
    gates = [
        "off",
        "none",
        "behavior_job_edge",
        "behavior_or_skill_edge",
        "behavior_or_direct",
        "direct",
        "seed_active",
    ]
    trials: list[dict[str, Any]] = []
    best: tuple[float, xgb.XGBRanker, dict[str, Any]] | None = None

    for feature_set in selected_sets:
        features = FEATURE_SETS[feature_set]
        for config in TRIAL_CONFIGS:
            model = fit(train_groups, validation_groups, features, config)
            gate_results: dict[str, Any] = {}
            for gate in gates:
                result, active, per_query = score_groups(
                    model, validation_groups, features, gate
                )
                gate_results[gate] = {
                    "metrics": result,
                    "active_queries": active,
                    "mean_ndcg": mean(row["ndcg@10"] for row in per_query),
                }
                secondary_result = None
                secondary_active = None
                if secondary_groups is not None:
                    secondary_result, secondary_active, _ = score_groups(
                        model, secondary_groups, features, gate
                    )
                    gate_results[gate]["secondary_metrics"] = secondary_result
                    gate_results[gate]["secondary_active_queries"] = secondary_active
                selection_score = result["ndcg@10"]
                if secondary_result is not None:
                    selection_score = mean(
                        [selection_score, secondary_result["ndcg@10"]]
                    )
                gate_results[gate]["selection_score"] = selection_score
                candidate = {
                    "feature_set": feature_set,
                    "features": features,
                    "config": {
                        "n_estimators": config.n_estimators,
                        "max_depth": config.max_depth,
                        "learning_rate": config.learning_rate,
                        "min_child_weight": config.min_child_weight,
                    },
                    "gate": gate,
                    "validation": gate_results[gate],
                }
                if best is None or selection_score > best[0]:
                    best = (selection_score, model, candidate)
            trials.append(
                {
                    "feature_set": feature_set,
                    "config": config.name,
                    "gates": gate_results,
                }
            )
            print(
                json.dumps(
                    {
                        "feature_set": feature_set,
                        "config": config.name,
                        "best_gate": max(
                            gate_results,
                            key=lambda gate: gate_results[gate]["selection_score"],
                        ),
                        "best_selection_score": max(
                            value["selection_score"]
                            for value in gate_results.values()
                        ),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    assert best is not None
    _, best_model, selection = best
    args.best_model.parent.mkdir(parents=True, exist_ok=True)
    best_model.save_model(args.best_model)
    manifest = {
        "model": args.best_model.name,
        "objective": "rank:ndcg",
        "eval_metric": "ndcg@10",
        "lambdarank_unbiased": True,
        "selection_policy": "train and 2026-06-06 validation only; no test rows read",
        "random_seed": 1111,
        **selection,
        "xgboost_version": xgb.__version__,
    }
    args.best_model.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = {
        "metadata": {
            "schema": "skillweave-ltr-tuning-v1",
            "train_groups": len(train_groups),
            "validation_groups": len(validation_groups),
            "secondary_validation_groups": (
                len(secondary_groups) if secondary_groups is not None else 0
            ),
            "test_rows_read": False,
            "random_seed": 1111,
        },
        "best": selection,
        "trials": trials,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"best": selection}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
