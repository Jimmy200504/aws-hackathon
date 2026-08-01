#!/usr/bin/env python3
"""Precompute structured intents for the head of the query distribution.

A Lambda invocation serves a single request, so the online batch coalescer has
no sibling query to group with and the container is frozen between calls — the
LRU never accumulates. Shipping the head of the distribution as a lookup is the
only way structured normalization is reachable on the serverless path.

The top 2,000 distinct queries cover ~61% of the search log, and generating
them costs a few minutes against the Bedrock requests-per-minute quota because
each request carries a whole batch.
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


TOP_QUERIES = ROOT / "config" / "top-queries.json"
OUTPUT = ROOT / "config" / "query-intents.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-queries", type=Path, default=TOP_QUERIES)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--max-batch", type=int, default=10)
    parser.add_argument(
        "--requests-per-minute",
        type=float,
        default=45.0,
        help="Stay under the Bedrock quota (50 RPM for Claude Haiku 4.5)",
    )
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Keep intents already present in the output file",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)

    if not args.top_queries.is_file():
        raise SystemExit(
            f"{args.top_queries} is missing; run scripts/build_top_queries.py first"
        )
    queries = json.loads(args.top_queries.read_text(encoding="utf-8"))["queries"]
    queries = queries[: max(1, args.limit)]

    normalizer = BedrockQueryNormalizer(
        args.model_id, max_batch=args.max_batch, cache_size=len(queries) * 2 + 1024
    )
    if not normalizer.enabled:
        raise SystemExit(
            "--model-id (or BEDROCK_QUERY_MODEL_ID) is required, and "
            "config/query-intent-vocab.json must exist"
        )
    existing: dict[str, dict] = {}
    if args.merge and args.output.is_file():
        existing = json.loads(args.output.read_text(encoding="utf-8")).get("intents", {})
        normalizer.load_intents(args.output)

    started = time.perf_counter()
    normalizer.prewarm(
        queries,
        requests_per_minute=args.requests_per_minute,
        max_workers=args.max_workers,
        background=False,
    )
    elapsed = time.perf_counter() - started

    intents = dict(existing)
    resolved = 0
    for query in queries:
        normalization = normalizer.normalize(query)
        # Only publish readings Bedrock actually produced; a deterministic
        # fallback is already what the online path computes for free.
        if normalization.source.startswith("amazon_bedrock"):
            intents[query] = normalization.intent.as_dict()
            resolved += 1

    payload = {
        "schema": "skillweave-query-intents-v1",
        "model_id": normalizer.model_id,
        "stats": {
            "requested": len(queries),
            "resolved": resolved,
            "published": len(intents),
            "bedrock_requests": normalizer.batch_stats["prewarm_requests"],
            "elapsed_seconds": round(elapsed, 1),
        },
        "intents": intents,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(json.dumps(payload["stats"], ensure_ascii=False, indent=2))
    print(f"Wrote {args.output} ({args.output.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
