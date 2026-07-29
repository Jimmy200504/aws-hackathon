#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


BASE_FEATURES = [
    "lexical",
    "exact_title",
    "title_phrase",
    "category_phrase",
    "description_phrase",
    "query_unit_overlap",
    "title_unit_overlap",
    "location",
    "duty",
    "behavior",
    "freshness",
    "post_cutoff_jd",
]
SEED_GRAPH_FEATURES = [
    *BASE_FEATURES,
    "seed_graph_raw",
    "seed_direct_match_count",
    "seed_related_path_count",
    "seed_job_skill_count",
    "cold_start",
]
SEED_GRAPH_DIAGNOSTIC_FEATURES = [
    *SEED_GRAPH_FEATURES,
    "technical_graph_raw",
    "seed_occupation_graph_raw",
    "llm_skill_match_count",
    "seed_occupation_match_count",
    "technical_job_skill_count",
    "seed_occupation_job_skill_count",
    "seed_graph_cold_start",
]
GRAPH_FEATURES = [
    *SEED_GRAPH_FEATURES,
    "duty_occupation_graph_raw",
    "duty_occupation_match_count",
    "duty_occupation_job_skill_count",
    "cold_start",
]
BEHAVIOR_GRAPH_FEATURES = [
    *BASE_FEATURES,
    "seed_graph_raw",
    "seed_direct_match_count",
    "seed_related_path_count",
    "seed_job_skill_count",
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
RETRIEVAL_FEATURES = [
    "retrieval_rank",
    "retrieval_reciprocal_rank",
    "retrieval_log_rank",
    "retrieval_top1",
    "retrieval_top3",
    "retrieval_top10",
]
QUALITY_FEATURES = [
    *BEHAVIOR_GRAPH_FEATURES,
    *RETRIEVAL_FEATURES,
]
QUALITY_MINIMAL_FEATURES = [
    *BEHAVIOR_GRAPH_FEATURES,
    "retrieval_reciprocal_rank",
]
QUALITY_COMPANY_FEATURES = [
    *[
        name
        for name in BEHAVIOR_GRAPH_FEATURES
        if not name.startswith("behavior_job_global")
    ],
    "retrieval_reciprocal_rank",
]


def load_groups(path: Path, features: list[str]):
    import numpy as np

    x: list[list[float]] = []
    y: list[int] = []
    groups: list[int] = []
    current_query = None
    current_size = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            query_id = row["query_id"]
            if current_query is not None and query_id != current_query:
                groups.append(current_size)
                current_size = 0
            current_query = query_id
            current_size += 1
            x.append([float(row["features"].get(name, 0.0)) for name in features])
            y.append(int(row["label"]))
    if current_size:
        groups.append(current_size)
    return np.asarray(x, dtype=np.float32), np.asarray(y, dtype=np.int32), groups


def main() -> None:
    parser = argparse.ArgumentParser(description="Train unbiased LambdaMART ranker")
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument(
        "--train-extra",
        type=Path,
        action="append",
        default=[],
        help="Additional grouped JSONL used only after model selection",
    )
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--feature-set",
        choices=[
            "baseline",
            "seed_graph",
            "seed_graph_diagnostic",
            "graph",
            "behavior_graph",
            "quality",
            "quality_minimal",
            "quality_company",
        ],
        required=True,
    )
    parser.add_argument("--n-estimators", type=int, default=700)
    parser.add_argument("--max-depth", type=int, default=7)
    parser.add_argument("--min-child-weight", type=float, default=8)
    parser.add_argument("--learning-rate", type=float, default=0.045)
    parser.add_argument("--early-stopping-rounds", type=int, default=60)
    args = parser.parse_args()
    try:
        import xgboost as xgb
    except ImportError as exc:
        raise SystemExit(
            "Run this script in the pinned SageMaker XGBoost 3.0-5 framework container"
        ) from exc
    features = {
        "baseline": BASE_FEATURES,
        "seed_graph": SEED_GRAPH_FEATURES,
        "seed_graph_diagnostic": SEED_GRAPH_DIAGNOSTIC_FEATURES,
        "graph": GRAPH_FEATURES,
        "behavior_graph": BEHAVIOR_GRAPH_FEATURES,
        "quality": QUALITY_FEATURES,
        "quality_minimal": QUALITY_MINIMAL_FEATURES,
        "quality_company": QUALITY_COMPANY_FEATURES,
    }[args.feature_set]
    train_parts = [
        load_groups(path, features)
        for path in [args.train, *args.train_extra]
    ]
    import numpy as np

    x_train = np.concatenate([part[0] for part in train_parts])
    y_train = np.concatenate([part[1] for part in train_parts])
    group_train = [
        group
        for part in train_parts
        for group in part[2]
    ]
    x_valid, y_valid, group_valid = load_groups(args.validation, features)
    model = xgb.XGBRanker(
        objective="rank:ndcg",
        eval_metric="ndcg@10",
        tree_method="hist",
        n_estimators=args.n_estimators,
        learning_rate=args.learning_rate,
        max_depth=args.max_depth,
        min_child_weight=args.min_child_weight,
        subsample=0.8,
        colsample_bytree=0.82,
        reg_alpha=0.2,
        reg_lambda=2.0,
        lambdarank_unbiased=True,
        lambdarank_pair_method="topk",
        lambdarank_num_pair_per_sample=12,
        random_state=1111,
        n_jobs=-1,
        early_stopping_rounds=(
            args.early_stopping_rounds if args.early_stopping_rounds > 0 else None
        ),
    )
    model.fit(
        x_train,
        y_train,
        group=group_train,
        eval_set=[(x_valid, y_valid)],
        eval_group=[group_valid],
        verbose=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(args.output)
    manifest = {
        "model": str(args.output.name),
        "objective": "rank:ndcg",
        "eval_metric": "ndcg@10",
        "lambdarank_unbiased": True,
        "feature_set": args.feature_set,
        "features": features,
        "random_seed": 1111,
        "n_estimators": args.n_estimators,
        "max_depth": args.max_depth,
        "min_child_weight": args.min_child_weight,
        "learning_rate": args.learning_rate,
        "train_sources": [str(args.train), *map(str, args.train_extra)],
        "best_iteration": getattr(model, "best_iteration", args.n_estimators - 1),
        "xgboost_version": xgb.__version__,
    }
    args.output.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
