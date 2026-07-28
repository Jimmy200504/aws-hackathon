#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import SkillWeaveRanker


INDEX = ROOT / "artifacts" / "demo-index.json"
QRELS = ROOT / "artifacts" / "temporal-eval.json"
REPORT = ROOT / "reports" / "ablation.json"


def metrics(ranked_ids: list[str], qrels: dict[str, int], k: int = 10) -> dict[str, float]:
    gains = [int(qrels.get(job_id, 0)) for job_id in ranked_ids[:k]]
    dcg = sum((2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(gains))
    ideal = sorted((int(value) for value in qrels.values()), reverse=True)[:k]
    idcg = sum((2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(ideal))
    relevant_ranks = [index + 1 for index, grade in enumerate(gains) if grade > 0]
    return {
        "ndcg@10": dcg / idcg if idcg else 0.0,
        "mrr": 1.0 / relevant_ranks[0] if relevant_ranks else 0.0,
        "hit@1": float(bool(gains and gains[0] > 0)),
        "hit@10": float(bool(relevant_ranks)),
        "precision@10": sum(grade > 0 for grade in gains) / 10.0,
    }


def aggregate(rows: list[dict[str, float]]) -> dict[str, float]:
    if not rows:
        return {name: 0.0 for name in ["ndcg@10", "mrr", "hit@1", "hit@10", "precision@10"]}
    return {name: mean(row[name] for row in rows) for name in rows[0]}


def bootstrap_delta(
    baseline: list[dict[str, float]],
    graph: list[dict[str, float]],
    metric: str,
    seed: int,
    repeats: int = 2000,
) -> dict[str, float]:
    if not baseline:
        return {"mean": 0.0, "ci95_low": 0.0, "ci95_high": 0.0}
    rng = random.Random(seed)
    deltas = [g[metric] - b[metric] for b, g in zip(baseline, graph)]
    samples = []
    for _ in range(repeats):
        samples.append(mean(deltas[rng.randrange(len(deltas))] for _ in deltas))
    samples.sort()
    return {
        "mean": mean(deltas),
        "ci95_low": samples[int(0.025 * (len(samples) - 1))],
        "ci95_high": samples[int(0.975 * (len(samples) - 1))],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run graph/no-graph temporal ablation")
    parser.add_argument("--index", type=Path, default=INDEX)
    parser.add_argument("--qrels", type=Path, default=QRELS)
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--split", choices=["validation", "test"], default="test")
    parser.add_argument("--graph-novelty-threshold", type=float, default=1.0)
    args = parser.parse_args()
    ranker = SkillWeaveRanker(
        args.index, graph_novelty_threshold=args.graph_novelty_threshold
    )
    evaluation = json.loads(args.qrels.read_text(encoding="utf-8"))
    cases = evaluation["splits"][args.split]
    baseline_rows: list[dict[str, float]] = []
    graph_rows: list[dict[str, float]] = []
    per_query: list[dict] = []
    for case in cases:
        candidates = set(case["candidates"])
        common = {
            "query": case["query"],
            "location_code": case["location_code"],
            "duty_code": case["duty_code"],
            "top_k": 20,
            "candidate_ids": candidates,
        }
        baseline_result = ranker.search(include_graph=False, **common)["results"]
        graph_result = ranker.search(include_graph=True, **common)["results"]
        baseline_metric = metrics([row["job_id"] for row in baseline_result], case["qrels"])
        graph_metric = metrics([row["job_id"] for row in graph_result], case["qrels"])
        baseline_rows.append(baseline_metric)
        graph_rows.append(graph_metric)
        per_query.append(
            {
                "query_id": case["query_id"],
                "baseline_ndcg@10": baseline_metric["ndcg@10"],
                "graph_ndcg@10": graph_metric["ndcg@10"],
                "delta": graph_metric["ndcg@10"] - baseline_metric["ndcg@10"],
            }
        )
    baseline = aggregate(baseline_rows)
    graph = aggregate(graph_rows)
    lift = {
        name: ((graph[name] / baseline[name] - 1.0) if baseline[name] else None)
        for name in baseline
    }
    report = {
        "metadata": {
            "split": args.split,
            "queries": len(cases),
            "index_version": ranker.metadata["index_version"],
            "qrels_schema": evaluation["metadata"]["schema"],
            "random_seed": 1111,
            "graph_novelty_threshold": args.graph_novelty_threshold,
            "position_bias_correction": False,
            "position_bias_note": "This script evaluates a deterministic reranker. IPS is applied by the production LTR trainer, not by evaluation metrics.",
            "status": "valid" if len(cases) >= 100 else "insufficient_qrels",
        },
        "baseline_no_graph": baseline,
        "skill_graph": graph,
        "relative_lift": lift,
        "paired_bootstrap_ndcg": bootstrap_delta(
            baseline_rows, graph_rows, "ndcg@10", seed=1111
        ),
        "per_query": per_query,
    }
    ndcg_lift = report["relative_lift"]["ndcg@10"]
    report["release_gates"] = {
        "minimum_queries_100": len(cases) >= 100,
        "ndcg_relative_lift_at_least_5pct": bool(
            ndcg_lift is not None and ndcg_lift >= 0.05
        ),
        "paired_ci_excludes_zero": report["paired_bootstrap_ndcg"]["ci95_low"] > 0,
    }
    report["release_gates"]["theme_gate_passed"] = (
        report["release_gates"]["minimum_queries_100"]
        and report["release_gates"]["ndcg_relative_lift_at_least_5pct"]
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "per_query"}, ensure_ascii=False, indent=2))
    print(f"Wrote {args.report}")
    if not cases:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
