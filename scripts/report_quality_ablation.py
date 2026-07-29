#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.evaluate_ltr import (
    ABLATION_BASE_FEATURES,
    load_model,
    paired_bootstrap,
    read_grouped_rows,
    score_model,
)
from scripts.benchmark import aggregate


DEFAULT_MODEL = ROOT / "artifacts" / "models" / "ltr-quality-final.ubj"
DEFAULT_PAIRS = (
    Path("/private/tmp/skillweave-quality-final-confirmation")
    / "ltr"
    / "test.jsonl"
)
DEFAULT_OUTPUT = ROOT / "reports" / "ltr-quality-component-ablation.json"

STATIC_SKILL = {
    "seed_graph_raw",
    "seed_direct_match_count",
    "seed_related_path_count",
    "seed_job_skill_count",
}
QUERY_JOB = {
    "behavior_query_job_seen",
    "behavior_query_job_positive_rate",
    "behavior_query_job_grade_rate",
    "behavior_query_job_exposures",
}
QUERY_SKILL = {
    "behavior_query_skill_seen_count",
    "behavior_query_skill_positive_rate",
    "behavior_query_skill_grade_rate",
    "behavior_query_skill_max_positive_rate",
}
GLOBAL_JOB = {
    "behavior_job_global_seen",
    "behavior_job_global_exposures_log",
    "behavior_job_global_positive_rate",
    "behavior_job_global_grade_rate",
    "behavior_job_global_positive_rate_smoothed",
    "behavior_job_global_grade_rate_smoothed",
}
GLOBAL_COMPANY = {
    "behavior_company_global_seen",
    "behavior_company_global_exposures_log",
    "behavior_company_global_positive_rate",
    "behavior_company_global_grade_rate",
    "behavior_company_global_positive_rate_smoothed",
    "behavior_company_global_grade_rate_smoothed",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report cumulative feature-family ablations for locked quality LTR"
    )
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--pairs", type=Path, default=DEFAULT_PAIRS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    model, manifest = load_model(args.model)
    groups = read_grouped_rows(args.pairs)
    stages = [
        ("retrieval_text", set(ABLATION_BASE_FEATURES)),
        ("plus_static_skill", set(ABLATION_BASE_FEATURES) | STATIC_SKILL),
        (
            "plus_query_skill_graph",
            set(ABLATION_BASE_FEATURES) | STATIC_SKILL | QUERY_SKILL,
        ),
        (
            "plus_query_job_graph",
            set(ABLATION_BASE_FEATURES)
            | STATIC_SKILL
            | QUERY_SKILL
            | QUERY_JOB,
        ),
        (
            "plus_global_job_prior",
            set(ABLATION_BASE_FEATURES)
            | STATIC_SKILL
            | QUERY_SKILL
            | QUERY_JOB
            | GLOBAL_JOB,
        ),
        (
            "full_with_company_prior",
            set(ABLATION_BASE_FEATURES)
            | STATIC_SKILL
            | QUERY_SKILL
            | QUERY_JOB
            | GLOBAL_JOB
            | GLOBAL_COMPANY,
        ),
    ]
    baseline_rows = score_model(
        model, manifest, groups, allowed_features=stages[0][1]
    )
    baseline = aggregate(baseline_rows)
    results = {}
    for name, allowed in stages:
        rows = score_model(model, manifest, groups, allowed_features=allowed)
        values = aggregate(rows)
        results[name] = {
            "allowed_features": sorted(allowed),
            "metrics": values,
            "relative_to_retrieval_text": {
                metric: values[metric] / baseline[metric] - 1
                if baseline[metric]
                else None
                for metric in baseline
            },
            "paired_ndcg_vs_retrieval_text": paired_bootstrap(
                baseline_rows, rows
            ),
        }
    report = {
        "metadata": {
            "schema": "skillweave-quality-component-ablation-v1",
            "queries": len(groups),
            "analysis_status": "locked-confirmation post-hoc component attribution",
            "caveat": (
                "Cumulative same-model zeroing is order-dependent; the authoritative "
                "release comparison is retrieval_text versus full_with_company_prior."
            ),
            "model": manifest,
        },
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                name: value["metrics"]
                for name, value in results.items()
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
