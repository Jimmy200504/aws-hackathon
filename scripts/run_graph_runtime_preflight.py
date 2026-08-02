#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.lambda_handler as lambda_handler
from app.graph_provider import GraphFeatureProvider
from app.query_normalizer import QueryNormalization


DEFAULT_MANIFEST = (
    ROOT
    / "artifacts/skill-graph-full-v2/release/runs/"
    "deterministic-v1-rules-v2-full/evaluation-cutoff/manifest.json"
)
DEFAULT_OUTPUT = ROOT / "reports/deterministic-graph-runtime-preflight.json"


class DeterministicNormalizer:
    enabled = False

    def normalize(self, query: str) -> QueryNormalization:
        return QueryNormalization(query, "deterministic_fallback", None)


class FailingNeptuneClient:
    def execute_query(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("intentional runtime preflight failure")


def event(query: str) -> dict[str, Any]:
    return {
        "requestContext": {
            "http": {"method": "POST", "path": "/api/v1/jobs/search"}
        },
        "body": json.dumps(
            {"query": query, "top_k": 10, "use_graph": True},
            ensure_ascii=False,
        ),
        "isBase64Encoded": False,
    }


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[index], 2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run local API and Neptune-failure contract preflight"
    )
    parser.add_argument("--graph-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--requests", type=int, default=30)
    args = parser.parse_args()
    if args.requests < 20:
        parser.error("--requests must be at least 20")

    manifest = json.loads(args.graph_manifest.read_text(encoding="utf-8"))
    graph_version = str(manifest["graph_version"])
    provider = GraphFeatureProvider(
        "failure-injected",
        graph_version=graph_version,
        client=FailingNeptuneClient(),
    )
    original_provider = lambda_handler.GRAPH_PROVIDER
    original_normalizer = lambda_handler.QUERY_NORMALIZER
    lambda_handler.GRAPH_PROVIDER = provider
    lambda_handler.QUERY_NORMALIZER = DeterministicNormalizer()
    queries = (
        "AWS Docker Kubernetes",
        "React 前端工程師",
        "後端工程師 Node.js",
        "資料工程師 Python",
        "行政助理",
    )
    try:
        for index in range(3):
            lambda_handler.handler(event(queries[index]), None)
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for index in range(args.requests):
            started = time.perf_counter()
            response = lambda_handler.handler(
                event(queries[index % len(queries)]), None
            )
            latencies.append((time.perf_counter() - started) * 1000)
            body = json.loads(response["body"])
            rows.append({"status": response["statusCode"], "body": body})
    finally:
        lambda_handler.GRAPH_PROVIDER = original_provider
        lambda_handler.QUERY_NORMALIZER = original_normalizer

    checks = {
        "serving_scope_supported": manifest.get("scope")
        in {"evaluation-cutoff", "latest"},
        "complete_corpus_when_latest": manifest.get("scope") != "latest"
        or manifest.get("accepted") == 1_218_635,
        "offline_zero_llm": manifest.get("llm_requests") == 0
        and manifest.get("embedding_requests") == 0,
        "all_http_200": all(row["status"] == 200 for row in rows),
        "all_top_10": all(len(row["body"].get("result", [])) == 10 for row in rows),
        "neptune_backend_disclosed": all(
            row["body"].get("meta", {}).get("graph_backend")
            == "neptune_analytics"
            for row in rows
        ),
        "v2_graph_version_preserved": all(
            row["body"].get("meta", {}).get("graph_version") == graph_version
            for row in rows
        ),
        "neptune_failure_disclosed": all(
            "neptune"
            in row["body"].get("meta", {}).get("degraded_components", [])
            for row in rows
        ),
        "no_internal_error": all(
            "error" not in row["body"] for row in rows
        ),
        "local_warm_p95_below_800ms": percentile(latencies, 0.95) < 800,
    }
    report = {
        "metadata": {
            "schema": "skillweave-graph-runtime-preflight-v1",
            "analysis_status": (
                "local_contract_preflight_not_managed_service_release_gate"
            ),
            "verified_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "graph_version": graph_version,
            "declared_graph_manifest_hash": manifest.get("manifest_hash"),
            "failure_injection": "Neptune execute_query raises RuntimeError",
        },
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            "requests": args.requests,
            "p50_ms": round(statistics.median(latencies), 2),
            "p95_ms": percentile(latencies, 0.95),
            "max_ms": round(max(latencies), 2),
            "candidate_source": sorted(
                {
                    row["body"].get("meta", {}).get("candidate_source")
                    for row in rows
                }
            ),
            "degraded_components": sorted(
                {
                    component
                    for row in rows
                    for component in row["body"]
                    .get("meta", {})
                    .get("degraded_components", [])
                }
            ),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
