#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release-manifest.json"
OUTPUT = ROOT / "reports" / "aws-production-smoke.json"
INDEX_VERSION = "demo-2026.06.05-v1"
QUERIES = (
    "AWS Docker Kubernetes",
    "React 前端工程師",
    "後端工程師 Node.js",
    "資料工程師 Python",
    "行政助理",
)


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def endpoint(base_url: str, relative: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", relative.lstrip("/"))


def request(
    base_url: str,
    relative: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    body = None
    headers = {"user-agent": "SkillWeave-production-smoke/1"}
    method = "GET"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json"
        method = "POST"
    started = time.perf_counter()
    for attempt in range(3):
        try:
            with urlopen(
                Request(
                    endpoint(base_url, relative),
                    data=body,
                    headers=headers,
                    method=method,
                ),
                timeout=timeout,
            ) as response:
                raw = response.read()
                return {
                    "status": response.status,
                    "content_type": response.headers.get_content_type(),
                    "body": raw,
                    "latency_ms": round(
                        (time.perf_counter() - started) * 1000, 2
                    ),
                }
        except (URLError, ConnectionError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(0.25 * (attempt + 1))
    raise AssertionError("unreachable")


def json_body(result: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(result["body"].decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("response must contain a JSON object")
    return value


def search_contract(body: dict[str, Any], expected: int = 10) -> bool:
    rows = body.get("result", [])
    if not isinstance(rows, list) or len(rows) != expected:
        return False
    ranks = [row.get("rank") for row in rows]
    job_ids = [row.get("job_id") for row in rows]
    return (
        ranks == list(range(1, expected + 1))
        and all(isinstance(job_id, str) and job_id for job_id in job_ids)
        and len(job_ids) == len(set(job_ids))
    )


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("cannot calculate percentile of an empty sample")
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[index], 2)


def run_load_request(base_url: str, index: int) -> dict[str, Any]:
    try:
        result = request(
            base_url,
            "api/v1/jobs/search",
            {
                "query": QUERIES[index % len(QUERIES)],
                "top_k": 10,
                "use_graph": index % 2 == 0,
            },
        )
        body = json_body(result)
        return {
            "status": result["status"],
            "latency_ms": result["latency_ms"],
            "top_10": search_contract(body),
            "index_version": body.get("meta", {}).get("index_version"),
            "ranking_model": body.get("meta", {}).get("ranking_model"),
            "candidate_source": body.get("meta", {}).get("candidate_source"),
            "degraded_components": body.get("meta", {}).get(
                "degraded_components"
            ),
            "error": None,
        }
    except Exception as exc:
        return {
            "status": None,
            "latency_ms": None,
            "top_10": False,
            "index_version": None,
            "ranking_model": None,
            "candidate_source": None,
            "degraded_components": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_smoke(
    base_url: str,
    requests: int,
    concurrency: int,
    *,
    require_full_corpus: bool = False,
) -> dict[str, Any]:
    root = request(base_url, "")
    css = request(base_url, "styles.css")
    javascript = request(base_url, "app.js")
    health_result = request(base_url, "health")
    meta_result = request(base_url, "api/v1/meta")
    graph_on_result = request(
        base_url,
        "api/v1/jobs/search",
        {"query": "AWS Docker Kubernetes", "top_k": 10, "use_graph": True},
    )
    graph_off_result = request(
        base_url,
        "api/v1/jobs/search",
        {"query": "AWS Docker Kubernetes", "top_k": 10, "use_graph": False},
    )
    trace_result = request(
        base_url,
        "api/v1/graph/trace",
        {"query": "React 前端工程師", "top_k": 10},
    )

    health = json_body(health_result)
    meta = json_body(meta_result)
    graph_on = json_body(graph_on_result)
    graph_off = json_body(graph_off_result)
    trace = json_body(trace_result)
    graph_on_rows = graph_on.get("result", [])
    graph_off_rows = graph_off.get("result", [])
    paths = [
        path
        for row in trace.get("trace", [])
        for path in row.get("paths", [])
    ]

    checks = {
        "public_ui": (
            root["status"] == 200
            and root["content_type"] == "text/html"
            and b"SkillWeave" in root["body"]
        ),
        "relative_css_asset": (
            css["status"] == 200
            and css["content_type"] == "text/css"
            and len(css["body"]) > 100
        ),
        "relative_javascript_asset": (
            javascript["status"] == 200
            and javascript["content_type"] in {
                "application/javascript",
                "text/javascript",
            }
            and b"api/v1/jobs/search" in javascript["body"]
        ),
        "health_contract": (
            health_result["status"] == 200
            and health.get("status") == "ok"
            and health.get("index_version") == INDEX_VERSION
            and health.get("jobs") == 12_000
        ),
        "metadata_contract": (
            meta_result["status"] == 200
            and meta.get("metadata", {}).get("index_version") == INDEX_VERSION
            and meta.get("job_count") == 12_000
        ),
        "full_corpus_scope_when_required": (
            not require_full_corpus
            or meta.get("search_scope") == "full_corpus_opensearch"
        ),
        "full_corpus_count_when_required": (
            not require_full_corpus
            or meta.get("search_corpus_job_count") == 1_218_635
        ),
        "full_corpus_candidate_source_when_required": (
            not require_full_corpus
            or (
                graph_on.get("meta", {}).get("candidate_source")
                == "opensearch_full_corpus"
                and graph_off.get("meta", {}).get("candidate_source")
                == "opensearch_full_corpus"
                and not graph_on.get("meta", {}).get("degraded_components")
                and not graph_off.get("meta", {}).get("degraded_components")
            )
        ),
        "quality_ltr_deployed": (
            graph_on.get("meta", {}).get("ranking_model")
            == "ltr-quality-final.ubj"
            and graph_off.get("meta", {}).get("ranking_model")
            == "ltr-quality-final.ubj"
        ),
        "real_bedrock_pilot_deployed": (
            meta.get("metadata", {})
            .get("bedrock_pilot", {})
            .get("records_accepted")
            == 180
            and meta.get("metadata", {})
            .get("bedrock_pilot", {})
            .get("model_id")
            == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        ),
        "graph_on_contract": (
            graph_on_result["status"] == 200 and search_contract(graph_on)
        ),
        "graph_off_contract": (
            graph_off_result["status"] == 200 and search_contract(graph_off)
        ),
        "graph_toggle_changes_ranking": (
            [row.get("job_id") for row in graph_on_rows]
            != [row.get("job_id") for row in graph_off_rows]
            and any(
                float(row.get("features", {}).get("graph", 0.0)) > 0.0
                for row in graph_on_rows
            )
            and all(
                float(row.get("features", {}).get("graph", 0.0)) == 0.0
                for row in graph_off_rows
            )
        ),
        "graph_trace_provenance": (
            trace_result["status"] == 200
            and bool(paths)
            and all(
                path.get("path")
                and path.get("edges")
                and str(path.get("evidence", "")).strip()
                for path in paths
            )
        ),
        "trace_source_provenance": (
            any(
                isinstance(path.get("provenance"), dict)
                and path["provenance"].get("source")
                == "amazon_bedrock_structured_extraction"
                for path in paths
            )
            or (
                require_full_corpus
                and any(
                    path.get("provenance")
                    == "deterministic_alias_full_corpus_v1"
                    for path in paths
                )
            )
        ),
    }

    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(run_load_request, base_url, index)
            for index in range(requests)
        ]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    latencies = [
        float(result["latency_ms"])
        for result in results
        if result["latency_ms"] is not None
    ]
    http_200 = sum(result["status"] == 200 for result in results)
    top_10 = sum(result["top_10"] is True for result in results)
    matching_index = sum(
        result["index_version"] == INDEX_VERSION for result in results
    )
    matching_model = sum(
        result["ranking_model"] == "ltr-quality-final.ubj"
        for result in results
    )
    full_corpus_responses = sum(
        result["candidate_source"] == "opensearch_full_corpus"
        and not result["degraded_components"]
        for result in results
    )
    errors = [result["error"] for result in results if result["error"]]
    load = {
        "requests": requests,
        "concurrency": concurrency,
        "http_200": http_200,
        "top_10_responses": top_10,
        "matching_index_version": matching_index,
        "matching_ranking_model": matching_model,
        "full_corpus_responses": full_corpus_responses,
        "elapsed_ms": elapsed_ms,
        "throughput_requests_per_second": round(
            requests / (elapsed_ms / 1000), 2
        ),
        "latency_ms": {
            "min": round(min(latencies), 2) if latencies else None,
            "p50": round(statistics.median(latencies), 2) if latencies else None,
            "p95": percentile(latencies, 0.95) if latencies else None,
            "p99": percentile(latencies, 0.99) if latencies else None,
            "max": round(max(latencies), 2) if latencies else None,
        },
        "errors": errors,
    }
    checks.update(
        {
            "load_all_http_200": http_200 == requests,
            "load_all_top_10": top_10 == requests,
            "load_index_version": matching_index == requests,
            "load_quality_ltr_model": matching_model == requests,
            "load_full_corpus_when_required": (
                not require_full_corpus or full_corpus_responses == requests
            ),
            "load_p95_below_timeout": (
                bool(latencies) and percentile(latencies, 0.95) < 10_000.0
            ),
        }
    )
    return {
        "metadata": {
            "schema": "skillweave-aws-production-smoke-v1",
            "verified_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "base_url": base_url.rstrip("/") + "/",
            "index_version": INDEX_VERSION,
            "client": "stdlib urllib; public HTTPS; no AWS session",
            "require_full_corpus": require_full_corpus,
        },
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            "job_count": health.get("jobs"),
            "skill_count": meta.get("skill_count"),
            "search_scope": meta.get("search_scope"),
            "search_corpus_job_count": meta.get("search_corpus_job_count"),
            "candidate_source": graph_on.get("meta", {}).get(
                "candidate_source"
            ),
            "degraded_components": graph_on.get("meta", {}).get(
                "degraded_components"
            ),
            "graph_on_job_ids": [
                row.get("job_id") for row in graph_on_rows
            ],
            "graph_off_job_ids": [
                row.get("job_id") for row in graph_off_rows
            ],
            "graph_trace_path_count": len(paths),
            "bedrock_trace_path_count": sum(
                isinstance(path.get("provenance"), dict)
                and path["provenance"].get("source")
                == "amazon_bedrock_structured_extraction"
                for path in paths
            ),
            "deterministic_full_corpus_trace_path_count": sum(
                path.get("provenance")
                == "deterministic_alias_full_corpus_v1"
                for path in paths
            ),
        },
        "load": load,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the public SkillWeave AWS deployment"
    )
    parser.add_argument("--url")
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--require-full-corpus",
        action="store_true",
        help="Fail if any request uses the embedded 12,000-job fallback",
    )
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must be positive")
    if args.concurrency > 10:
        parser.error("--concurrency must not exceed the compact demo limit of 10")

    manifest = load_object(MANIFEST)
    registered_url = manifest.get("external_deliverables", {}).get("aws_url")
    base_url = args.url or registered_url
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        parser.error("a public HTTPS --url or registered aws_url is required")
    if registered_url and base_url.rstrip("/") != str(registered_url).rstrip("/"):
        parser.error("--url must match the release manifest aws_url")

    report = run_smoke(
        base_url,
        args.requests,
        args.concurrency,
        require_full_corpus=args.require_full_corpus,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        relative = args.output.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        relative = None
    if relative:
        manifest.setdefault("sha256", {})[relative] = hashlib.sha256(
            args.output.read_bytes()
        ).hexdigest()
        MANIFEST.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
