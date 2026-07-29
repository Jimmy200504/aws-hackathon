#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def build_report(
    assumptions: dict[str, Any],
    ablation: dict[str, Any],
) -> dict[str, Any]:
    searches = int(assumptions["searches_in_window"])
    baseline = ablation["baseline_no_graph"]
    graph = ablation["skill_graph"]
    baseline_hit1 = float(baseline["hit@1"])
    graph_hit1 = float(graph["hit@1"])
    absolute_hit1_lift = graph_hit1 - baseline_hit1
    return {
        "metadata": {
            "schema": "skillweave-business-impact-v1",
            "analysis_status": "offline_scale_translation_not_causal",
            "measurement_window": assumptions["measurement_window"],
            "source_searches": searches,
            "source_ablation_queries": ablation["metadata"]["queries"],
        },
        "observed_offline_metrics": {
            "baseline_hit_at_1": baseline_hit1,
            "skill_graph_hit_at_1": graph_hit1,
            "absolute_hit_at_1_lift": absolute_hit1_lift,
            "relative_hit_at_1_lift": (
                graph_hit1 / baseline_hit1 - 1 if baseline_hit1 else None
            ),
            "baseline_mrr": float(baseline["mrr"]),
            "skill_graph_mrr": float(graph["mrr"]),
        },
        "scale_translation": {
            "weekly_searches": searches,
            "baseline_top1_relevance_events": searches * baseline_hit1,
            "skill_graph_top1_relevance_events": searches * graph_hit1,
            "incremental_top1_relevance_events": searches * absolute_hit1_lift,
            "rounded_incremental_top1_relevance_events": round(
                searches * absolute_hit1_lift
            ),
        },
        "financial_claim": {
            "currency_value_per_incremental_top1": assumptions.get(
                "currency_value_per_incremental_top1"
            ),
            "estimated_revenue": None,
            "reason": (
                "No causal conversion or monetary-value experiment exists; "
                "revenue is intentionally not estimated."
            ),
        },
        "guardrail": assumptions["guardrail"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Translate locked offline ranking lift to a bounded scale proxy"
    )
    parser.add_argument(
        "--assumptions",
        type=Path,
        default=ROOT / "config" / "business-assumptions.json",
    )
    parser.add_argument(
        "--ablation",
        type=Path,
        default=ROOT / "reports" / "ltr-quality-confirmation.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports" / "business-impact.json",
    )
    args = parser.parse_args()
    report = build_report(
        load_object(args.assumptions),
        load_object(args.ablation),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
