#!/usr/bin/env python3
"""Normalize offline evaluation queries with Amazon Bedrock, once per distinct string.

The online normalizer is tuned for latency and silently degrades to a
deterministic fallback. That behaviour is correct for serving and wrong for
measurement: a quiet fallback would make an unmeasured LLM look like a measured
null result. This batch therefore uses generous timeouts, enforces the event's
1 request-per-second ceiling, records provenance per row, and refuses to declare
success when the degraded rate is high.

Cache is append-only JSONL keyed by the raw query, so an expired Workshop
Studio credential can be replaced and the run resumed without re-billing work
that already succeeded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.query_normalizer import (
    SYSTEM_PROMPT,
    BedrockQueryNormalizer,
    deterministic_fallback,
)

DEFAULT_QRELS = ROOT / "artifacts" / "llm-exp-smoke" / "temporal-eval.json"
DEFAULT_CACHE = ROOT / "artifacts" / "llm-exp-smoke" / "query-normalization.jsonl"
DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-6"
# The event rule caps Bedrock at one request per second. A small margin absorbs
# clock granularity so a burst can never cross the ceiling.
MIN_INTERVAL_SECONDS = 1.05


def batch_normalizer(model_id: str, region: str) -> BedrockQueryNormalizer:
    """Same prompt and schema as production, timeouts sized for batch work."""
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            connect_timeout=10.0,
            read_timeout=60.0,
            retries={"max_attempts": 5, "mode": "adaptive"},
        ),
    )
    return BedrockQueryNormalizer(model_id, region=region, client=client)


def distinct_queries(qrels_path: Path, splits: list[str]) -> list[str]:
    payload = json.loads(qrels_path.read_text(encoding="utf-8"))
    seen: dict[str, None] = {}
    for split in splits:
        for case in payload["splits"].get(split, []):
            query = case["query"]
            if query and query.strip():
                seen.setdefault(query, None)
    return list(seen)


def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    cache: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "query" in row:
                cache[row["query"]] = row
    return cache


def pending(queries: list[str], cache: dict[str, dict[str, Any]]) -> Iterator[str]:
    for query in queries:
        row = cache.get(query)
        # Retry rows that only recorded a degraded fallback: those cost nothing
        # to redo and leaving them in place would silently bias the measurement.
        if row is None or row.get("degraded"):
            yield query


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qrels", type=Path, default=DEFAULT_QRELS)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--model-id", default=DEFAULT_MODEL)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--splits", default="test", help="comma separated: train,validation,test"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="stop after N calls (0 = no limit)"
    )
    parser.add_argument(
        "--max-degraded-rate",
        type=float,
        default=0.05,
        help="exit non-zero when more calls than this fell back",
    )
    args = parser.parse_args()

    splits = [s.strip() for s in args.splits.split(",") if s.strip()]
    queries = distinct_queries(args.qrels, splits)
    cache = load_cache(args.cache)
    todo = list(pending(queries, cache))
    if args.limit > 0:
        todo = todo[: args.limit]

    print(
        json.dumps(
            {
                "splits": splits,
                "distinct_queries": len(queries),
                "already_cached": len(queries) - len(list(pending(queries, cache))),
                "to_call": len(todo),
                "model_id": args.model_id,
                "region": args.region,
                "min_interval_seconds": MIN_INTERVAL_SECONDS,
                "estimated_minutes": round(len(todo) * MIN_INTERVAL_SECONDS / 60, 1),
                "prompt_sha256": hashlib.sha256(
                    SYSTEM_PROMPT.encode("utf-8")
                ).hexdigest()[:16],
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    if not todo:
        print("nothing to do", flush=True)
        return

    normalizer = batch_normalizer(args.model_id, args.region)
    args.cache.parent.mkdir(parents=True, exist_ok=True)
    latencies: list[float] = []
    degraded = 0
    changed = 0
    last_call = 0.0
    started = time.monotonic()

    with args.cache.open("a", encoding="utf-8") as handle:
        for index, query in enumerate(todo, 1):
            wait = MIN_INTERVAL_SECONDS - (time.monotonic() - last_call)
            if wait > 0:
                time.sleep(wait)
            last_call = time.monotonic()
            call_started = time.perf_counter()
            result = normalizer.normalize(query)
            latency_ms = round((time.perf_counter() - call_started) * 1000, 1)
            latencies.append(latency_ms)
            degraded += bool(result.degraded)
            baseline = deterministic_fallback(query)
            changed += result.query != baseline
            handle.write(
                json.dumps(
                    {
                        "query": query,
                        "normalized": result.query,
                        "deterministic": baseline,
                        "source": result.source,
                        "model_id": result.model_id,
                        "degraded": bool(result.degraded),
                        "latency_ms": latency_ms,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            handle.flush()
            if index % 25 == 0 or index == len(todo):
                elapsed = time.monotonic() - started
                print(
                    "  %d/%d  degraded=%d  changed=%d  %.1f min elapsed"
                    % (index, len(todo), degraded, changed, elapsed / 60),
                    flush=True,
                )

    rate = degraded / max(1, len(todo))
    summary = {
        "calls": len(todo),
        "degraded": degraded,
        "degraded_rate": round(rate, 4),
        "changed_vs_deterministic": changed,
        "changed_rate": round(changed / max(1, len(todo)), 4),
        "latency_ms_p50": round(statistics.median(latencies), 1) if latencies else None,
        "latency_ms_p95": (
            round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1)
            if latencies
            else None
        ),
        "observed_rps": round(len(todo) / max(0.001, time.monotonic() - started), 4),
        "cache": str(args.cache),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["observed_rps"] > 1.0:
        raise SystemExit("rate ceiling violated: observed %.3f RPS" % summary["observed_rps"])
    if rate > args.max_degraded_rate:
        raise SystemExit(
            "degraded rate %.1f%% exceeds --max-degraded-rate; the measurement "
            "would describe the fallback, not Bedrock" % (100 * rate)
        )


if __name__ == "__main__":
    main()
