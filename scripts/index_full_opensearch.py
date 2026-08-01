#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.embeddings import BedrockEmbeddingClient, EMBEDDING_DIM, job_embedding_text
from app.job_fields import derive_job_fields
from pipeline.deterministic_extract import (
    ExactAliasMatcher,
    OntologyTerm,
    extract_job,
    load_ontology,
)

DATA = ROOT / "data" / "dataset"
DEMO_INDEX = ROOT / "artifacts" / "demo-index.json"
TRAIN_CUTOFF = datetime.fromisoformat("2026-06-05 23:59:59.999")
MIN_DATE = datetime.fromisoformat("2024-01-01 00:00:00")

def parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return MIN_DATE


def compile_alias_matcher(
    skills: dict[str, dict[str, Any]],
) -> tuple[ExactAliasMatcher, dict[str, list[str]]]:
    matcher = ExactAliasMatcher(
        OntologyTerm(
            node_id=skill_id,
            label=str(spec.get("label", skill_id)),
            aliases=tuple(str(value) for value in spec.get("aliases", ())),
            node_type=str(spec.get("type", "Skill")),
        )
        for skill_id, spec in sorted(skills.items())
    )
    return matcher, {
        alias: [node_id] for alias, node_id in matcher.alias_to_node.items()
    }


def mapping(
    *,
    serverless: bool = False,
    local_single_node: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "mappings": {
            "dynamic": False,
            "_source": {"excludes": ["description_search"]},
            "properties": {
                "id": {"type": "keyword"},
                "title": {
                    "type": "text",
                    "analyzer": "cjk",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
                },
                "description": {"type": "text", "analyzer": "cjk"},
                "description_search": {"type": "text", "analyzer": "cjk"},
                "salary": {"type": "keyword", "index": False},
                "salary_min": {"type": "float"},
                "salary_max": {"type": "float"},
                "salary_type": {"type": "keyword"},
                "is_remote": {"type": "boolean"},
                "city": {
                    "type": "text",
                    "analyzer": "cjk",
                    "fields": {"keyword": {"type": "keyword"}},
                },
                "categories": {
                    "type": "text",
                    "analyzer": "cjk",
                    "fields": {"keyword": {"type": "keyword"}},
                },
                "industry": {"type": "text", "analyzer": "cjk"},
                "company_id": {"type": "keyword"},
                "modified_at": {"type": "keyword"},
                "post_cutoff_jd": {"type": "boolean"},
                "graph_eligible": {"type": "boolean"},
                "skills": {"type": "keyword"},
                "latest_skills": {"type": "keyword"},
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
                        "parameters": {
                            "ef_construction": 256,
                            "m": 16,
                        },
                    },
                },
            },
        },
    }
    # OpenSearch Serverless manages these values and rejects attempts to
    # override them. Provisioned OpenSearch domains can still tune them.
    if not serverless:
        result["settings"] = {
            "index": {
                "number_of_shards": 1 if local_single_node else 4,
                "number_of_replicas": 0 if local_single_node else 1,
                "refresh_interval": "30s",
                "knn": True,
            }
        }
    return result


class SignedOpenSearchClient:
    def __init__(self, endpoint: str, region: str, timeout_seconds: float) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self.timeout_seconds = timeout_seconds
        self.service = "aoss" if ".aoss." in endpoint else "es"
        self.sign_requests = not endpoint.startswith(
            ("http://127.0.0.1", "http://localhost")
        )
        self.local_single_node = not self.sign_requests
        self._session = None
        if self.sign_requests:
            try:
                from botocore.session import get_session
            except ImportError as exc:
                raise SystemExit(
                    "Install requirements-production.lock before indexing AWS OpenSearch"
                ) from exc
            self._session = get_session()

    def request(
        self,
        method: str,
        path: str,
        payload: bytes = b"",
        *,
        content_type: str = "application/json",
        acceptable: tuple[int, ...] = (200, 201),
    ) -> dict[str, Any]:
        url = self.endpoint + path
        for attempt in range(12):
            headers = {"content-type": content_type}
            if self.service == "aoss":
                headers["x-amz-content-sha256"] = hashlib.sha256(
                    payload
                ).hexdigest()
            if self.sign_requests:
                from botocore.auth import SigV4Auth
                from botocore.awsrequest import AWSRequest

                credentials = self._session.get_credentials()
                if credentials is None:
                    raise RuntimeError("AWS credentials are unavailable")
                request_to_sign = AWSRequest(
                    method=method,
                    url=url,
                    data=payload,
                    headers=headers,
                )
                SigV4Auth(
                    credentials.get_frozen_credentials(),
                    self.service,
                    self.region,
                ).add_auth(request_to_sign)
                headers = dict(request_to_sign.headers.items())
            request = Request(
                url,
                data=payload or None,
                method=method,
                headers=headers,
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    status = response.status
                    body = response.read()
            except HTTPError as exc:
                status = exc.code
                body = exc.read()
            except (URLError, ConnectionError, TimeoutError):
                if attempt == 11:
                    raise
                time.sleep(5)
                continue
            if status not in {403, 429, 500, 502, 503, 504} or attempt == 11:
                break
            time.sleep(5)
        if status not in acceptable:
            detail = body.decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"OpenSearch {method} {path} returned {status}: {detail}")
        if not body:
            return {}
        decoded = json.loads(body)
        if not isinstance(decoded, dict):
            raise RuntimeError("OpenSearch response must be an object")
        return decoded


def matched_skills(
    row: dict[str, str],
    pattern: ExactAliasMatcher,
    alias_to_skills: dict[str, list[str]],
) -> tuple[list[str], dict[str, str], dict[str, float]]:
    del alias_to_skills
    extracted = extract_job(row, pattern)
    evidence = {
        mention["node_id"]: str(mention["evidence"])
        for mention in extracted["mentions"]
    }
    confidence = {
        mention["node_id"]: float(mention["confidence"])
        for mention in extracted["mentions"]
    }
    return sorted(evidence), evidence, confidence


def job_document(
    row: dict[str, str],
    skills: dict[str, dict[str, Any]],
    pattern: ExactAliasMatcher,
    alias_to_skills: dict[str, list[str]],
    embedding_client: BedrockEmbeddingClient | None = None,
) -> dict[str, Any]:
    modified = parse_time(row["職缺最後修改時間"])
    graph_eligible = modified <= TRAIN_CUTOFF
    latest_skill_ids, latest_evidence, latest_confidence = matched_skills(
        row, pattern, alias_to_skills
    )
    if graph_eligible:
        skill_ids, evidence, confidence = latest_skill_ids, latest_evidence, latest_confidence
    else:
        skill_ids, evidence, confidence = [], {}, {}
    categories = [
        value
        for value in [row["職務大類"], row["職務中類"], row["職務小類"]]
        if value and value != "NULL"
    ]
    freshness = max(
        0.0,
        min(
            1.0,
            (modified - MIN_DATE).total_seconds()
            / max(1.0, (TRAIN_CUTOFF - MIN_DATE).total_seconds()),
        ),
    )
    document = {
        "id": row["職缺編號"],
        "title": row["職務名稱"].strip(),
        "description": row["職務內容"].strip()[:420],
        "description_search": row["職務內容"].strip(),
        "salary": row["薪資"].replace("‧", " · "),
        **derive_job_fields(row),
        "city": row["工作城市"],
        "categories": categories,
        "industry": row["產業中類"] or row["產業大類"],
        "company_id": row["廠商編號"],
        "modified_at": row["職缺最後修改時間"],
        "post_cutoff_jd": not graph_eligible,
        "graph_eligible": graph_eligible,
        "skills": skill_ids,
        "latest_skills": latest_skill_ids,
        "skill_labels": [
            str(skills[skill_id].get("label", skill_id))
            for skill_id in skill_ids
            if skill_id in skills
        ],
        "skill_evidence": evidence,
        "skill_confidence": confidence,
        "skill_provenance": {
            skill_id: "deterministic_alias_full_corpus_v1"
            for skill_id in skill_ids
        },
        "freshness": round(freshness, 4),
        "view_count": 0,
        "apply_count": 0,
    }
    # Generate embedding for hybrid retrieval
    if embedding_client is not None:
        embedding = embedding_client.embed(job_embedding_text(row))
        if embedding is not None:
            document["embedding"] = embedding
    return document


def bulk_payload(index: str, documents: Iterable[dict[str, Any]]) -> bytes:
    lines: list[str] = []
    for document in documents:
        lines.append(
            json.dumps(
                {"index": {"_index": index, "_id": document["id"]}},
                separators=(",", ":"),
            )
        )
        lines.append(
            json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def send_bulk_batch(
    client: SignedOpenSearchClient,
    index: str,
    documents: list[dict[str, Any]],
) -> int:
    response = client.request(
        "POST",
        "/_bulk",
        bulk_payload(index, documents),
        content_type="application/x-ndjson",
    )
    if response.get("errors"):
        failures = [
            item
            for item in response.get("items", [])
            if int(next(iter(item.values())).get("status", 500)) >= 300
        ]
        raise RuntimeError(
            "OpenSearch bulk indexing failed: "
            + json.dumps(failures[:3], ensure_ascii=False)
        )
    return len(documents)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index every supplied job into a full-corpus OpenSearch index"
    )
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--demo-index", type=Path, default=DEMO_INDEX)
    parser.add_argument(
        "--endpoint",
        default=os.getenv("OPENSEARCH_ENDPOINT", ""),
        help="OpenSearch domain or Serverless collection endpoint",
    )
    parser.add_argument(
        "--index",
        default=os.getenv("OPENSEARCH_INDEX", "skillweave-jobs-v1"),
    )
    parser.add_argument(
        "--region", default=os.getenv("AWS_REGION", "us-east-1")
    )
    parser.add_argument("--batch-size", type=int, default=400)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--start-record", type=int, default=0)
    parser.add_argument("--expected-count", type=int, default=0)
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--skip-create",
        action="store_true",
        help="Index into an existing compatible index",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate records without sending them",
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Generate embeddings via Bedrock for hybrid retrieval",
    )
    args = parser.parse_args()
    if not args.dry_run and not args.endpoint:
        raise SystemExit("--endpoint or OPENSEARCH_ENDPOINT is required")
    if args.batch_size < 1 or args.batch_size > 2000:
        raise SystemExit("--batch-size must be between 1 and 2000")
    if args.workers < 1 or args.workers > 8:
        raise SystemExit("--workers must be between 1 and 8")
    if args.start_record < 0 or args.expected_count < 0:
        raise SystemExit("--start-record and --expected-count must be non-negative")

    ontology_path = ROOT / "config" / "skill_ontology.seed.json"
    icap_path = ROOT / "config" / "icap_vocabulary.reviewed.json"
    terms = load_ontology(ontology_path, icap_path)
    skills = {
        term.node_id: {
            "type": term.node_type,
            "label": term.label,
            "aliases": list(term.aliases),
        }
        for term in terms
    }
    pattern, alias_to_skills = compile_alias_matcher(skills)
    embedding_client = (
        BedrockEmbeddingClient.from_environment() if args.embed else None
    )
    if embedding_client is not None and not embedding_client.enabled:
        print("WARNING: --embed specified but BEDROCK_EMBEDDING_MODEL_ID not set; "
              "using default amazon.titan-embed-text-v2:0")
    client = (
        None
        if args.dry_run
        else SignedOpenSearchClient(
            args.endpoint, args.region, args.timeout_seconds
        )
    )
    index_path = f"/{quote(args.index, safe='-_.')}"
    if client is not None and not args.skip_create:
        client.request(
            "PUT",
            index_path,
            json.dumps(
                mapping(
                    serverless=client.service == "aoss",
                    # A local Docker node cannot allocate replicas and does not
                    # benefit from multiple primary shards for this corpus size.
                    local_single_node=client.local_single_node,
                ),
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    indexed = graph_jobs = completed = source_rows_seen = 0
    started = time.monotonic()
    pending: list[dict[str, Any]] = []
    in_flight: set[Future[int]] = set()
    next_progress = 10_000
    source_path = args.data_dir / "職缺.csv"
    executor = (
        ThreadPoolExecutor(max_workers=args.workers)
        if client is not None
        else None
    )

    def collect(done: set[Future[int]]) -> None:
        nonlocal completed, next_progress
        for future in done:
            in_flight.remove(future)
            completed += future.result()
        if completed >= next_progress:
            elapsed = max(0.001, time.monotonic() - started)
            print(
                f"Indexed {completed:,} jobs · {completed / elapsed:,.0f} jobs/s",
                flush=True,
            )
            next_progress = (completed // 10_000 + 1) * 10_000

    try:
        with source_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if args.max_records > 0 and indexed >= args.max_records:
                    break
                source_rows_seen += 1
                graph_jobs += int(
                    parse_time(row["職缺最後修改時間"]) <= TRAIN_CUTOFF
                )
                if source_rows_seen <= args.start_record:
                    continue
                document = job_document(
                    row,
                    skills,
                    pattern,
                    alias_to_skills,
                    embedding_client,
                )
                pending.append(document)
                indexed += 1
                if len(pending) < args.batch_size:
                    continue
                documents, pending = pending, []
                if executor is None:
                    completed += len(documents)
                    collect(set())
                    continue
                in_flight.add(
                    executor.submit(send_bulk_batch, client, args.index, documents)
                )
                if len(in_flight) >= args.workers * 2:
                    done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    collect(done)

        if pending:
            if executor is None:
                completed += len(pending)
                collect(set())
            else:
                in_flight.add(
                    executor.submit(send_bulk_batch, client, args.index, pending)
                )
        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            collect(done)
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    expected_count = args.expected_count or source_rows_seen
    verified_count = None
    if client is not None:
        if client.service != "aoss":
            client.request("POST", f"{index_path}/_refresh", b"{}")
        # Search collections become visible asynchronously. Polling also
        # avoids declaring a partial index complete immediately after _bulk.
        deadline = time.monotonic() + 180
        while True:
            count = client.request("POST", f"{index_path}/_count", b"{}")
            verified_count = int(count.get("count", -1))
            if verified_count == expected_count or time.monotonic() >= deadline:
                break
            time.sleep(5)
        if verified_count != expected_count:
            raise RuntimeError(
                f"index count mismatch: expected {expected_count:,}, got {verified_count:,}"
            )

    report = {
        "schema": "skillweave-full-corpus-index-v1",
        "index": args.index,
        "source": str(source_path),
        "source_jobs_indexed": expected_count,
        "jobs_processed_this_run": indexed,
        "start_record": args.start_record,
        "verified_index_count": verified_count,
        "graph_eligible_jobs": graph_jobs,
        "cold_start_jobs": expected_count - graph_jobs,
        "all_source_jobs_are_search_targets": (
            verified_count == expected_count if verified_count is not None else None
        ),
        "dry_run": args.dry_run,
        "workers": args.workers,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
