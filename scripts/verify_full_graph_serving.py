#!/usr/bin/env python3
"""Verify complete local and AWS latest-graph serving state."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import SkillWeaveRanker
from scripts.index_full_opensearch import SignedOpenSearchClient


RUN_ID = "deterministic-v1-rules-v2-full"
GRAPH_VERSION = "deterministic-v1-rules-v2-latest"
EXPECTED_JOBS = 1_218_635
EXPECTED_NODES = 1_219_372
EXPECTED_EDGES = 5_249_573
POST_CUTOFF_JOB_ID = "113042386"
POST_CUTOFF_QUERY = "I186 Node.js後端工程師 可遠端工作"
BUCKET = "skillweave-provisioned-search-snapshotbucket-rigq6j18z9ma"
POINTER_KEY = "graph-artifacts/serving/production-manifest.json"
LATEST_PREFIX = f"graph-artifacts/{RUN_ID}/latest/neptune/"
MANIFEST = (
    ROOT
    / "artifacts/skill-graph-full-v2/release/runs"
    / RUN_ID
    / "latest/manifest.json"
)
QUALITY = MANIFEST.with_name("quality-report.json")
DEMO = ROOT / "artifacts/demo-index.json"
OUTPUT = ROOT / "reports/full-graph-serving.json"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def decode_payload(response: dict[str, Any]) -> dict[str, Any]:
    payload = response["payload"]
    if hasattr(payload, "read"):
        payload = payload.read()
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    value = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(value, dict):
        raise ValueError("Neptune query payload must be an object")
    return value


def public_search(url: str) -> dict[str, Any]:
    body = json.dumps(
        {"query": POST_CUTOFF_QUERY, "use_graph": True, "top_k": 10},
        ensure_ascii=False,
    ).encode("utf-8")
    with urlopen(
        Request(
            url.rstrip("/") + "/api/v1/jobs/search",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        ),
        timeout=30,
    ) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("public search response must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify local and AWS full/latest Skill Graph serving"
    )
    parser.add_argument(
        "--url",
        default="https://m97uj2vc55.execute-api.us-east-1.amazonaws.com/prod/",
    )
    parser.add_argument(
        "--function-name",
        default="skillweave-demo-SearchFunction-FADugcmevmjk",
    )
    parser.add_argument("--alias", default="live")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    manifest = load_json(MANIFEST)
    quality = load_json(QUALITY)
    demo = load_json(DEMO)
    demo_jobs = demo["jobs"]
    local_ranker = SkillWeaveRanker(DEMO)
    local_rows = local_ranker.search(POST_CUTOFF_QUERY, top_k=20)["results"]
    local_post_cutoff = next(
        (row for row in local_rows if row["job_id"] == POST_CUTOFF_JOB_ID), None
    )

    import boto3

    lambda_client = boto3.client("lambda", region_name=args.region)
    neptune = boto3.client("neptune-graph", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)
    alias = lambda_client.get_alias(
        FunctionName=args.function_name, Name=args.alias
    )
    live_version = str(alias["FunctionVersion"])
    function = lambda_client.get_function_configuration(
        FunctionName=args.function_name, Qualifier=live_version
    )
    environment = function.get("Environment", {}).get("Variables", {})
    graph_id = str(environment.get("NEPTUNE_GRAPH_ID", ""))
    graph_version = str(environment.get("GRAPH_VERSION", ""))
    endpoint = str(environment.get("OPENSEARCH_ENDPOINT", ""))
    index = str(environment.get("OPENSEARCH_INDEX", ""))

    pointer = json.loads(
        s3.get_object(Bucket=BUCKET, Key=POINTER_KEY)["Body"].read()
    )
    import_task = neptune.get_import_task(
        taskIdentifier=str(pointer["neptune_import_task"])
    )
    count_payload = decode_payload(
        neptune.execute_query(
            graphIdentifier=graph_id,
            queryString="MATCH (n:Job) RETURN count(n) AS jobs",
            language="OPEN_CYPHER",
        )
    )
    edge_payload = decode_payload(
        neptune.execute_query(
            graphIdentifier=graph_id,
            queryString=(
                "MATCH (j) WHERE id(j) = 'job:113042386' "
                "MATCH (j)-[r]->(s) RETURN type(r) AS relation, "
                "id(s) AS target_id LIMIT 10"
            ),
            language="OPEN_CYPHER",
        )
    )
    remote_jobs = int(count_payload["results"][0]["jobs"])

    opensearch = SignedOpenSearchClient(endpoint, args.region, 60)

    def count(query: dict[str, Any]) -> int:
        response = opensearch.request(
            "POST",
            f"/{index}/_count",
            json.dumps({"query": query}, separators=(",", ":")).encode(),
        )
        return int(response["count"])

    eligible_jobs = count({"term": {"graph_eligible": True}})
    ineligible_jobs = count({"term": {"graph_eligible": False}})
    public = public_search(args.url)
    public_row = next(
        (
            row
            for row in public.get("result", [])
            if row.get("job_id") == POST_CUTOFF_JOB_ID
        ),
        None,
    )
    public_meta = public.get("meta", {})

    files = {
        item["path"]: item
        for item in manifest.get("files", [])
        if item.get("path") in {"neptune/nodes.csv", "neptune/edges.csv"}
    }
    s3_nodes = s3.head_object(Bucket=BUCKET, Key=LATEST_PREFIX + "nodes.csv")
    s3_edges = s3.head_object(Bucket=BUCKET, Key=LATEST_PREFIX + "edges.csv")
    manifest_sha256 = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()

    checks = {
        "local_latest_manifest": manifest.get("scope") == "latest"
        and manifest.get("graph_version") == GRAPH_VERSION
        and manifest.get("accepted") == EXPECTED_JOBS,
        "local_graph_integrity": quality.get("referential_integrity") is True
        and quality.get("nodes") == EXPECTED_NODES
        and quality.get("edges") == EXPECTED_EDGES
        and quality.get("silent_loss") == 0,
        "local_demo_all_eligible": len(demo_jobs) == 12_000
        and all(job.get("graph_eligible") is True for job in demo_jobs)
        and any(job.get("post_cutoff_jd") for job in demo_jobs),
        "local_post_cutoff_trace": local_post_cutoff is not None
        and local_post_cutoff.get("graph_eligible") is True
        and local_post_cutoff.get("features", {}).get("cold_start") == 0.0
        and bool(local_post_cutoff.get("graph_trace")),
        "s3_artifact_sizes": s3_nodes["ContentLength"]
        == files["neptune/nodes.csv"]["bytes"]
        and s3_edges["ContentLength"] == files["neptune/edges.csv"]["bytes"],
        "serving_pointer_latest": pointer.get("scope") == "latest"
        and pointer.get("graph_id") == graph_id
        and pointer.get("graph_version") == GRAPH_VERSION
        and pointer.get("manifest_sha256") == manifest_sha256,
        "neptune_import_succeeded": import_task.get("status") == "SUCCEEDED"
        and import_task.get("importTaskDetails", {}).get("errorCount") == 0,
        "neptune_all_job_nodes": remote_jobs == EXPECTED_JOBS,
        "neptune_post_cutoff_edges": bool(edge_payload.get("results")),
        "opensearch_all_eligible": eligible_jobs == EXPECTED_JOBS
        and ineligible_jobs == 0,
        "lambda_latest_graph": graph_id == "g-ndf9sijo15"
        and graph_version == GRAPH_VERSION,
        "public_api_latest_graph": public_meta.get("graph_backend")
        == "neptune_analytics"
        and public_meta.get("graph_version") == GRAPH_VERSION
        and not public_meta.get("degraded_components"),
        "public_post_cutoff_trace": public_row is not None
        and public_row.get("graph_eligible") is True
        and public_row.get("features", {}).get("post_cutoff_jd") == 1.0
        and public_row.get("features", {}).get("cold_start") == 0.0
        and bool(public_row.get("graph_trace")),
    }
    report = {
        "metadata": {
            "schema": "skillweave-full-graph-serving-verification-v1",
            "verified_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "dataset_version": "1111-2026-06-01_2026-06-07",
        },
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            "local_jobs": manifest.get("accepted"),
            "local_nodes": quality.get("nodes"),
            "local_edges": quality.get("edges"),
            "local_demo_jobs": len(demo_jobs),
            "remote_graph_id": graph_id,
            "remote_graph_version": graph_version,
            "lambda_live_version": live_version,
            "remote_job_nodes": remote_jobs,
            "remote_post_cutoff_edge_count": len(edge_payload.get("results", [])),
            "opensearch_eligible_jobs": eligible_jobs,
            "opensearch_ineligible_jobs": ineligible_jobs,
            "public_post_cutoff_job_id": (
                public_row.get("job_id") if public_row else None
            ),
            "public_post_cutoff_trace_count": (
                len(public_row.get("graph_trace", [])) if public_row else 0
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
