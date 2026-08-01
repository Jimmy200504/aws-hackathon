#!/usr/bin/env python3
"""Measure what batched structured normalization changes for real queries.

Runs a small sample of head/tail queries taken from the organizer search log
through the ranker twice — once with the deterministic reading only, once with
the batched Bedrock intent — and reports the retrieval effect plus the actual
request coalescing achieved against the live Bedrock quota.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.query_normalizer import BedrockQueryNormalizer  # noqa: E402
from app.ranker import SkillWeaveRanker  # noqa: E402


DEFAULT_INDEX = ROOT / "artifacts" / "demo-index.json"
DEFAULT_MODEL = ROOT / "artifacts" / "models" / "ltr-quality-final.trees.json"
OUTPUT = ROOT / "reports" / "query-normalization-validation.json"

# Sampled from userSearchLog_20260601_20260607.csv: the attribute-heavy head
# (現領 alone is 12.8% of all searches), brand and landmark queries, and the
# punctuation-noise tail that makes up half of the distinct queries.
SAMPLE_QUERIES = [
    "現領",
    "正職",
    "二度就業",
    "早班兼職",
    "工讀生",
    "親子",
    "萊爾富",
    "青埔",
    "診所",
    "學徒",
    "冷凍空調學徒+技術人員",
    "機台操作員(輪班人員)",
    "護理師+護士+藝文特區+誠徵",
    "104人力銀行////////",
    "包裝員/作業員",
]


def summarize(result: dict, elapsed_ms: float) -> dict:
    rows = result["results"]
    return {
        "results": len(rows),
        "latency_ms": round(elapsed_ms, 1),
        "resolved_skills": list(result["intent"].skills)[:5],
        "top_titles": [row["title"][:34] for row in rows[:3]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--ltr-model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--model-id", default=None, help="Bedrock model id")
    parser.add_argument("--max-batch", type=int, default=10)
    parser.add_argument("--max-wait-seconds", type=float, default=1.0)
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=60.0,
        help="Validation waits for the batch; production uses a short deadline",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--top-k", type=int, default=20)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    ranker = SkillWeaveRanker(args.index, ltr_model_path=args.ltr_model)
    baseline_normalizer = BedrockQueryNormalizer(None, vocabulary=None)
    structured_normalizer = BedrockQueryNormalizer(
        args.model_id,
        max_batch=args.max_batch,
        max_wait_seconds=args.max_wait_seconds,
        deadline_seconds=args.deadline_seconds,
    )
    if not structured_normalizer.enabled:
        raise SystemExit(
            "--model-id (or BEDROCK_QUERY_MODEL_ID) is required, and "
            "config/query-intent-vocab.json must exist"
        )

    started = time.perf_counter()
    structured = structured_normalizer.normalize_many(SAMPLE_QUERIES)
    batch_elapsed = time.perf_counter() - started

    cases = []
    for query, normalization in zip(SAMPLE_QUERIES, structured):
        baseline_normalization = baseline_normalizer.normalize(query)
        clock = time.perf_counter()
        baseline = ranker.search(
            query,
            top_k=args.top_k,
            normalized_query=baseline_normalization.query,
        )
        baseline_ms = (time.perf_counter() - clock) * 1000
        clock = time.perf_counter()
        treatment = ranker.search(
            query,
            top_k=args.top_k,
            normalized_query=normalization.query,
            structured_intent=normalization.intent,
        )
        treatment_ms = (time.perf_counter() - clock) * 1000
        cases.append(
            {
                "query": query,
                "normalized_query": normalization.query,
                "source": normalization.source,
                "intent": normalization.intent.as_dict(),
                "baseline": summarize(baseline, baseline_ms),
                "structured": summarize(treatment, treatment_ms),
            }
        )

    baseline_zero = sum(case["baseline"]["results"] == 0 for case in cases)
    structured_zero = sum(case["structured"]["results"] == 0 for case in cases)
    baseline_total = sum(case["baseline"]["results"] for case in cases)
    structured_total = sum(case["structured"]["results"] for case in cases)
    report = {
        "schema": "skillweave-query-normalization-validation-v1",
        "model_id": structured_normalizer.model_id,
        "index": str(args.index),
        "candidate_source": "embedded_demo_index",
        "queries": len(SAMPLE_QUERIES),
        "batching": {
            **structured_normalizer.batch_stats,
            "max_batch": args.max_batch,
            "max_wait_seconds": args.max_wait_seconds,
            "wall_clock_seconds": round(batch_elapsed, 2),
            "queries_per_bedrock_request": round(
                structured_normalizer.batch_stats["queries_dispatched"]
                / max(1, structured_normalizer.batch_stats["batches_dispatched"]),
                2,
            ),
        },
        "totals": {
            "baseline_zero_result_queries": baseline_zero,
            "structured_zero_result_queries": structured_zero,
            "baseline_returned_rows": baseline_total,
            "structured_returned_rows": structured_total,
        },
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["batching"], ensure_ascii=False, indent=2))
    print(json.dumps(report["totals"], ensure_ascii=False, indent=2))
    for case in cases:
        print(
            f"{case['query']!r:34} "
            f"{case['baseline']['results']:2d} → {case['structured']['results']:2d} rows | "
            f"{'、'.join(case['intent']['duty_categories'][:3]) or '-'}"
        )
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
