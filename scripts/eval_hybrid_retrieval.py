#!/usr/bin/env python3
"""Evaluate hybrid retrieval (BM25 + kNN) vs BM25-only.

Indexes the demo-index jobs into a local OpenSearch with embeddings,
then runs temporal-eval test queries in both modes and compares recall.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.embeddings import BedrockEmbeddingClient, EMBEDDING_DIM, job_embedding_text
from app.retrieval import OpenSearchRetriever

ENDPOINT = "http://127.0.0.1:9200"
INDEX_NAME = "skillweave-hybrid-eval"


def create_index(endpoint: str, index: str) -> None:
    """Create a fresh index with kNN mapping."""
    import urllib.request
    import urllib.error

    # Delete if exists
    try:
        req = urllib.request.Request(f"{endpoint}/{index}", method="DELETE")
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError:
        pass

    mapping = {
        "settings": {
            "index": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "refresh_interval": "-1",
                "knn": True,
            }
        },
        "mappings": {
            "dynamic": False,
            "properties": {
                "id": {"type": "keyword"},
                "title": {"type": "text", "analyzer": "cjk",
                          "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}},
                "description": {"type": "text", "analyzer": "cjk"},
                "salary": {"type": "keyword", "index": False},
                "salary_min": {"type": "float"},
                "salary_max": {"type": "float"},
                "salary_type": {"type": "keyword"},
                "is_remote": {"type": "boolean"},
                "city": {"type": "text", "analyzer": "cjk",
                         "fields": {"keyword": {"type": "keyword"}}},
                "categories": {"type": "text", "analyzer": "cjk",
                               "fields": {"keyword": {"type": "keyword"}}},
                "industry": {"type": "text", "analyzer": "cjk"},
                "company_id": {"type": "keyword"},
                "modified_at": {"type": "keyword"},
                "post_cutoff_jd": {"type": "boolean"},
                "graph_eligible": {"type": "boolean"},
                "skills": {"type": "keyword"},
                "skill_labels": {"type": "text", "analyzer": "cjk"},
                "skill_evidence": {"type": "object", "enabled": False},
                "skill_confidence": {"type": "object", "enabled": False},
                "skill_provenance": {"type": "object", "enabled": False},
                "freshness": {"type": "float"},
                "view_count": {"type": "integer"},
                "apply_count": {"type": "integer"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": EMBEDDING_DIM,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "lucene",
                        "parameters": {"ef_construction": 256, "m": 16},
                    },
                },
            },
        },
    }
    payload = json.dumps(mapping).encode()
    req = urllib.request.Request(
        f"{endpoint}/{index}",
        data=payload,
        method="PUT",
        headers={"content-type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=30)
    print(f"Created index {index}")


def index_jobs(
    endpoint: str,
    index: str,
    jobs: list[dict],
    embedder: BedrockEmbeddingClient,
    batch_size: int = 200,
    max_workers: int = 8,
) -> int:
    """Index jobs with embeddings into OpenSearch."""
    import urllib.request

    # Generate embeddings in parallel
    print(f"Generating embeddings for {len(jobs)} jobs...")
    texts = [job_embedding_text(job) for job in jobs]
    embeddings: list[list[float] | None] = [None] * len(texts)

    def embed_one(idx: int) -> tuple[int, list[float] | None]:
        return idx, embedder.embed(texts[idx])

    embedded_count = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(embed_one, i) for i in range(len(texts))]
        for i, future in enumerate(as_completed(futures)):
            idx, vec = future.result()
            embeddings[idx] = vec
            if vec is not None:
                embedded_count += 1
            if (i + 1) % 500 == 0:
                print(f"  Embedded {i+1}/{len(texts)} ({embedded_count} success)")

    print(f"Embedding done: {embedded_count}/{len(texts)} successful")

    # Bulk index
    indexed = 0
    for batch_start in range(0, len(jobs), batch_size):
        batch = jobs[batch_start:batch_start + batch_size]
        lines = []
        for i, job in enumerate(batch):
            doc = dict(job)
            emb = embeddings[batch_start + i]
            if emb is not None:
                doc["embedding"] = emb
            lines.append(json.dumps({"index": {"_index": index, "_id": doc["id"]}}, separators=(",", ":")))
            lines.append(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
        payload = ("\n".join(lines) + "\n").encode()
        req = urllib.request.Request(
            f"{endpoint}/_bulk",
            data=payload,
            method="POST",
            headers={"content-type": "application/x-ndjson"},
        )
        resp = urllib.request.urlopen(req, timeout=60)
        result = json.loads(resp.read())
        if result.get("errors"):
            print(f"WARNING: bulk errors in batch starting at {batch_start}")
        indexed += len(batch)

    # Refresh
    req = urllib.request.Request(f"{endpoint}/{index}/_refresh", data=b"{}", method="POST",
                                headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=30)
    print(f"Indexed {indexed} jobs")
    return embedded_count


def evaluate_retrieval(
    retriever: OpenSearchRetriever,
    cases: list[dict],
    limit: int = 200,
) -> dict[str, float]:
    """Evaluate retrieval recall: how many relevant candidates are retrieved."""
    total_queries = 0
    recall_at_k = 0.0
    hit_at_1 = 0
    hit_at_10 = 0
    mrr_sum = 0.0

    for case in cases:
        query = case["query"]
        qrels = case.get("qrels", {})
        relevant = {jid for jid, grade in qrels.items() if grade > 0}
        if not relevant:
            continue

        location_codes = case.get("location_code", [])
        duty_codes = case.get("duty_code", [])

        try:
            candidates = retriever.retrieve(
                query,
                limit=limit,
                location_names=[],  # We don't have name lookup here
                duty_names=[],
                wants_remote=False,
            )
        except Exception as exc:
            print(f"  Query failed: {exc}")
            continue

        retrieved_ids = [c["id"] for c in candidates]
        found = set(retrieved_ids) & relevant

        total_queries += 1
        recall_at_k += len(found) / len(relevant) if relevant else 0

        # MRR and Hit@K
        first_relevant_rank = None
        for rank, jid in enumerate(retrieved_ids, 1):
            if jid in relevant:
                if first_relevant_rank is None:
                    first_relevant_rank = rank
                break

        if first_relevant_rank is not None:
            mrr_sum += 1.0 / first_relevant_rank
            if first_relevant_rank <= 1:
                hit_at_1 += 1
            if first_relevant_rank <= 10:
                hit_at_10 += 1

    if total_queries == 0:
        return {"queries": 0}

    return {
        "queries": total_queries,
        "recall@200": round(recall_at_k / total_queries, 4),
        "mrr": round(mrr_sum / total_queries, 4),
        "hit@1": round(hit_at_1 / total_queries, 4),
        "hit@10": round(hit_at_10 / total_queries, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate hybrid vs BM25-only retrieval")
    parser.add_argument("--index-source", type=Path, default=ROOT / "artifacts" / "benchmark-index.json")
    parser.add_argument("--qrels", type=Path, default=ROOT / "artifacts" / "temporal-eval.json")
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument("--index", default=INDEX_NAME)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--max-queries", type=int, default=200,
                        help="Max queries to evaluate (for speed)")
    parser.add_argument("--skip-index", action="store_true",
                        help="Skip indexing, use existing index")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports" / "hybrid-retrieval-eval.json")
    args = parser.parse_args()

    # Load data
    artifact = json.loads(args.index_source.read_text(encoding="utf-8"))
    jobs = artifact["jobs"]
    job_ids_in_index = {job["id"] for job in jobs}
    print(f"Source index: {len(jobs)} jobs")

    qrels_data = json.loads(args.qrels.read_text(encoding="utf-8"))
    # Use validation split for evaluation
    cases = qrels_data["splits"].get("validation", [])
    # Filter to cases where candidates are in our index
    filtered_cases = []
    for case in cases:
        candidates_in_index = [c for c in case.get("candidates", []) if c in job_ids_in_index]
        relevant_in_index = {
            jid: grade for jid, grade in case.get("qrels", {}).items()
            if jid in job_ids_in_index and grade > 0
        }
        if relevant_in_index and len(candidates_in_index) >= 2:
            filtered_cases.append(case)
    filtered_cases = filtered_cases[:args.max_queries]
    print(f"Evaluating {len(filtered_cases)} queries (from {len(cases)} total)")

    embedder = BedrockEmbeddingClient.from_environment()
    if not embedder.enabled:
        raise SystemExit("BEDROCK_EMBEDDING_MODEL_ID must be set")

    # Index with embeddings
    if not args.skip_index:
        create_index(args.endpoint, args.index)
        embedded = index_jobs(args.endpoint, args.index, jobs, embedder)
        print(f"\nIndex ready: {len(jobs)} jobs, {embedded} with embeddings\n")

    # Evaluate BM25-only
    print("=" * 60)
    print("Evaluating BM25-only retrieval...")
    retriever_bm25 = OpenSearchRetriever(
        args.endpoint, args.index, timeout_seconds=10.0, embedding_client=None
    )
    bm25_results = evaluate_retrieval(retriever_bm25, filtered_cases, args.limit)
    print(f"BM25-only: {json.dumps(bm25_results, indent=2)}")

    # Evaluate Hybrid (BM25 + kNN)
    print("\nEvaluating Hybrid (BM25 + kNN) retrieval...")
    retriever_hybrid = OpenSearchRetriever(
        args.endpoint, args.index, timeout_seconds=10.0, embedding_client=embedder
    )
    hybrid_results = evaluate_retrieval(retriever_hybrid, filtered_cases, args.limit)
    print(f"Hybrid:    {json.dumps(hybrid_results, indent=2)}")

    # Compare
    print("\n" + "=" * 60)
    print("COMPARISON")
    print("=" * 60)
    for metric in ["recall@200", "mrr", "hit@1", "hit@10"]:
        bm25_val = bm25_results.get(metric, 0)
        hybrid_val = hybrid_results.get(metric, 0)
        delta = hybrid_val - bm25_val
        pct = (delta / bm25_val * 100) if bm25_val > 0 else 0
        print(f"  {metric:12s}: BM25={bm25_val:.4f}  Hybrid={hybrid_val:.4f}  Δ={delta:+.4f} ({pct:+.1f}%)")

    report = {
        "schema": "hybrid-retrieval-eval-v1",
        "index_jobs": len(jobs),
        "queries_evaluated": len(filtered_cases),
        "retrieval_limit": args.limit,
        "bm25_only": bm25_results,
        "hybrid_bm25_knn": hybrid_results,
        "relative_lift": {
            metric: round(
                (hybrid_results.get(metric, 0) - bm25_results.get(metric, 0))
                / max(bm25_results.get(metric, 0), 1e-9),
                4,
            )
            for metric in ["recall@200", "mrr", "hit@1", "hit@10"]
        },
        "embedding_model": "amazon.titan-embed-text-v2:0",
        "fusion_method": "reciprocal_rank_fusion_k60",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
