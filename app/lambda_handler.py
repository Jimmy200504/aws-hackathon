from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from app.graph_provider import GraphExpansion, GraphFeatureProvider
from app.query_normalizer import BedrockQueryNormalizer
from app.ranker import SkillWeaveRanker
from app.retrieval import OpenSearchRetriever


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
INDEX_PATH = Path(os.getenv("INDEX_PATH", ROOT / "artifacts" / "demo-index.json"))
LTR_MODEL_PATH = Path(
    os.getenv(
        "LTR_MODEL_PATH",
        ROOT / "artifacts" / "models" / "ltr-quality-final.trees.json",
    )
)
RANKER = SkillWeaveRanker(
    INDEX_PATH,
    ltr_model_path=LTR_MODEL_PATH,
    candidate_retriever=OpenSearchRetriever.from_environment(),
)
QUERY_NORMALIZER = BedrockQueryNormalizer.from_environment()
GRAPH_PROVIDER = GraphFeatureProvider.from_environment()
FULL_CORPUS_JOB_COUNT = max(
    0, int(os.getenv("OPENSEARCH_DOCUMENT_COUNT", "0"))
)


def response(
    status: int,
    body: str | bytes | dict[str, Any],
    content_type: str = "application/json; charset=utf-8",
) -> dict[str, Any]:
    if isinstance(body, dict):
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        encoded = False
    elif isinstance(body, bytes):
        payload = base64.b64encode(body).decode()
        encoded = True
    else:
        payload = body
        encoded = False
    return {
        "statusCode": status,
        "headers": {
            "content-type": content_type,
            "cache-control": "no-store" if content_type.startswith("application/json") else "no-cache",
            "x-content-type-options": "nosniff",
            "content-security-policy": "default-src 'self'; style-src 'self' 'unsafe-inline'",
        },
        "isBase64Encoded": encoded,
        "body": payload,
    }


def parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def search(event: dict[str, Any], trace: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    body = parse_body(event)
    query = body.get("query", body.get("ks", ""))
    if not isinstance(query, str) or not query.strip():
        return response(
            400,
            {
                "error": {
                    "code": "invalid_query",
                    "message": "query (or ks) is required and must be non-empty",
                }
            },
        )
    location = body.get("location_code", body.get("c0"))
    duty = body.get("duty_code", body.get("d0"))
    include_graph = body.get("use_graph", True)
    if not isinstance(include_graph, bool):
        return response(
            400, {"error": {"code": "invalid_request", "message": "use_graph must be boolean"}}
        )
    normalization = QUERY_NORMALIZER.normalize(query)
    embedded_graph_version = RANKER.metadata.get(
        "graph_version", RANKER.metadata.get("index_version", "")
    )
    graph = GraphExpansion(
        backend="embedded_artifact",
        version=str(embedded_graph_version),
    )
    if include_graph and GRAPH_PROVIDER is not None:
        fallback_ids = RANKER.parse_intent(
            query, location, duty, normalized_query=normalization.query
        ).skills
        graph = GRAPH_PROVIDER.expand(normalization.query, fallback_ids)
    ranked = RANKER.search(
        query,
        location_code=location,
        duty_code=duty,
        top_k=body.get("top_k", 20),
        include_graph=include_graph,
        normalized_query=normalization.query,
        resolved_skill_ids=graph.canonical_ids or None,
        external_relations=(
            graph.relations if GRAPH_PROVIDER is not None else None
        ),
        structured_intent=normalization.intent,
    )
    rows = ranked["results"]
    payload: dict[str, Any] = {
        "request_id": "req_" + uuid.uuid4().hex[:16],
        "result": rows,
        "empStr": ",".join(row["job_id"] for row in rows),
        "meta": {
            "count": len(rows),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "graph_enabled": include_graph,
            "graph_backend": graph.backend if include_graph else "disabled",
            "graph_version": graph.version,
            "resolved_skills": list(ranked["intent"].skills),
            "index_version": RANKER.metadata.get("index_version"),
            "ranking_model": (
                RANKER.ltr_model.metadata.get("source_model")
                if RANKER.ltr_model is not None
                else "heuristic_fallback"
            ),
            "candidate_source": ranked["candidate_source"],
            "degraded_components": normalization.merge_degraded_components(
                [*ranked["degraded_components"], *graph.degraded_components]
            ),
            "query_normalization": normalization.metadata(),
        },
    }
    if trace:
        payload["trace"] = [
            {"job_id": row["job_id"], "rank": row["rank"], "paths": row["graph_trace"]}
            for row in rows[:5]
        ]
    return response(200, payload)


def static(path: str) -> dict[str, Any]:
    relative = unquote(path).lstrip("/") or "index.html"
    file_path = (WEB_ROOT / relative).resolve()
    if WEB_ROOT.resolve() not in file_path.parents or not file_path.is_file():
        file_path = WEB_ROOT / "index.html"
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type == "application/javascript":
        content_type += "; charset=utf-8"
    payload = file_path.read_bytes()
    if content_type.startswith("text/") or content_type.startswith("application/javascript"):
        return response(200, payload.decode("utf-8"), content_type)
    return response(200, payload, content_type)


def normalized_path(event: dict[str, Any]) -> str:
    context = event.get("requestContext", {})
    request = context.get("http", {})
    path = request.get("path", event.get("path", "/"))
    stage = context.get("stage")
    if isinstance(stage, str) and stage and stage != "$default":
        prefix = f"/{stage}"
        if path == prefix or path == f"{prefix}/":
            return "/"
        if path.startswith(f"{prefix}/"):
            return path[len(prefix) :]
    return path


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request = event.get("requestContext", {}).get("http", {})
    method = request.get("method", event.get("httpMethod", "GET"))
    path = normalized_path(event)
    try:
        if method == "GET" and path == "/health":
            return response(
                200,
                {
                    "status": "ok",
                    "service": "skillweave-search",
                    "index_version": RANKER.metadata.get("index_version"),
                    "jobs": len(RANKER.jobs),
                    "full_corpus_jobs": (
                        FULL_CORPUS_JOB_COUNT
                        if RANKER.candidate_retriever is not None
                        and FULL_CORPUS_JOB_COUNT > 0
                        else None
                    ),
                    "full_corpus_retrieval": RANKER.candidate_retriever is not None,
                    "bedrock_query_normalization": QUERY_NORMALIZER.enabled,
                    "graph_backend": (
                        "neptune_analytics" if GRAPH_PROVIDER is not None else "embedded_artifact"
                    ),
                },
            )
        if method == "GET" and path == "/api/v1/meta":
            return response(
                200,
                {
                    "metadata": RANKER.metadata,
                    "job_count": len(RANKER.jobs),
                    "embedded_job_count": len(RANKER.jobs),
                    "full_corpus_job_count": (
                        FULL_CORPUS_JOB_COUNT
                        if RANKER.candidate_retriever is not None
                        and FULL_CORPUS_JOB_COUNT > 0
                        else None
                    ),
                    "search_corpus_job_count": (
                        FULL_CORPUS_JOB_COUNT
                        if RANKER.candidate_retriever is not None
                        and FULL_CORPUS_JOB_COUNT > 0
                        else len(RANKER.jobs)
                    ),
                    "skill_count": len(RANKER.skills),
                    "search_scope": (
                        "full_corpus_opensearch"
                        if RANKER.candidate_retriever is not None
                        else "embedded_12000"
                    ),
                },
            )
        if method == "POST" and path == "/api/v1/jobs/search":
            return search(event)
        if method == "POST" and path == "/api/v1/graph/trace":
            return search(event, trace=True)
        if method == "GET":
            return static(path)
        return response(404, {"error": {"code": "not_found", "message": "endpoint not found"}})
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return response(
            400, {"error": {"code": "invalid_request", "message": str(exc)}}
        )
    except Exception:
        # Detailed exception is emitted to CloudWatch by the Lambda runtime.
        return response(
            500, {"error": {"code": "internal_error", "message": "search failed safely"}}
        )
