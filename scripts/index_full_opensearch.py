#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import sys
import threading
import time
import unicodedata
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
from app.alias_matcher import AliasMatcher
from app.job_fields import derive_job_fields

DATA = ROOT / "data" / "dataset"
DEMO_INDEX = ROOT / "artifacts" / "demo-index.json"
BEHAVIOR = ROOT / "artifacts" / "job-behavior.json"
TRAIN_CUTOFF = datetime.fromisoformat("2026-06-05 23:59:59.999")
MIN_DATE = datetime.fromisoformat("2024-01-01 00:00:00")


def norm(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower().replace("臺", "台")
    return re.sub(r"\s+", " ", text).strip()


def parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return MIN_DATE


def compile_alias_matcher(
    skills: dict[str, dict[str, Any]],
) -> tuple[AliasMatcher, dict[str, list[str]]]:
    alias_to_skills: dict[str, list[str]] = {}
    for skill_id, spec in skills.items():
        for value in [spec.get("label", ""), *spec.get("aliases", [])]:
            alias = norm(str(value))
            if alias and (alias.isascii() or len(alias) >= 2):
                alias_to_skills.setdefault(alias, []).append(skill_id)
    # A single regex alternation becomes unusable once the ontology grows past
    # a few thousand aliases; the automaton scans each string once.
    return AliasMatcher(alias_to_skills), alias_to_skills


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
                "employment_type": {"type": "keyword"},
                "shifts": {"type": "keyword"},
                "education": {"type": "keyword"},
                "experience": {"type": "keyword"},
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
        attempts: int = 5,
    ) -> dict[str, Any]:
        """Send one signed request, retrying transient transport failures.

        A full-corpus pass is thousands of sequential bulk requests over tens of
        minutes; a single dropped connection would otherwise discard the whole
        run. Each attempt is re-signed because SigV4 covers the timestamp.
        """
        last_error: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return self._request_once(
                    method, path, payload, content_type, acceptable
                )
            except (URLError, ConnectionError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt == attempts:
                    break
                delay = min(30.0, 2.0**attempt)
                print(
                    f"  transient {type(exc).__name__} on {method} {path}; "
                    f"retry {attempt}/{attempts - 1} in {delay:.0f}s",
                    flush=True,
                )
                time.sleep(delay)
        raise RuntimeError(
            f"OpenSearch {method} {path} failed after {attempts} attempts: {last_error}"
        ) from last_error

    def _request_once(
        self,
        method: str,
        path: str,
        payload: bytes,
        content_type: str,
        acceptable: tuple[int, ...],
    ) -> dict[str, Any]:
        url = self.endpoint + path
        headers = {"content-type": content_type}
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
    pattern: AliasMatcher,
    alias_to_skills: dict[str, list[str]],
) -> tuple[list[str], dict[str, str], dict[str, float]]:
    fields = [
        ("職務名稱", row["職務名稱"], 0.96),
        (
            "結構化技能欄位",
            " ".join(
                [row["電腦技能資料"], row["工作技能"], row["專業證照"]]
            ),
            0.93,
        ),
        (
            "職務分類",
            " ".join([row["職務大類"], row["職務中類"], row["職務小類"]]),
            0.84,
        ),
        ("職務內容", row["職務內容"], 0.79),
    ]
    resolved: dict[str, tuple[str, str, float]] = {}
    searchable = norm(" ".join(value for _, value, _ in fields))
    for match in pattern.finditer(searchable):
        alias = norm(match.group(0))
        for skill_id in alias_to_skills.get(alias, []):
            if skill_id in resolved:
                continue
            for field_name, source, confidence in fields:
                if alias and alias in norm(source):
                    resolved[skill_id] = (field_name, alias, confidence)
                    break
    evidence = {
        skill_id: f"{field_name}：{alias}"
        for skill_id, (field_name, alias, _) in resolved.items()
    }
    confidence = {
        skill_id: score for skill_id, (_, _, score) in resolved.items()
    }
    return sorted(resolved), evidence, confidence


def job_document(
    row: dict[str, str],
    skills: dict[str, dict[str, Any]],
    pattern: AliasMatcher,
    alias_to_skills: dict[str, list[str]],
    behavior: dict[str, list[int]] | None = None,
    embedding_client: Any = None,
) -> dict[str, Any]:
    modified = parse_time(row["職缺最後修改時間"])
    graph_eligible = modified <= TRAIN_CUTOFF
    # The cutoff exists to keep the offline graph/no-graph ablation free of
    # future information. The live index has no such leakage concern, and
    # withholding skills from the 24% of postings modified after the cutoff
    # only removes signal from a quarter of the corpus. Keep `graph_eligible`
    # as the honest provenance flag; annotate every posting.
    skill_ids, evidence, confidence = matched_skills(row, pattern, alias_to_skills)
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
    views, applies = (behavior or {}).get(row["職缺編號"], (0, 0))
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
        # Attribute queries (現領/正職/兼職/工讀/晚班/暑期) are a double-digit
        # share of the search log but occur in job free text far less often than
        # they are searched for. Their answer is these structured columns.
        "employment_type": row.get("職缺屬性", "").strip(),
        "shifts": [
            value.strip()
            for value in row.get("工時", "").split(",")
            if value.strip() and value.strip() != "NULL"
        ],
        "education": row.get("學歷需求", "").strip(),
        "experience": row.get("工作經驗需求", "").strip(),
        "modified_at": row["職缺最後修改時間"],
        "post_cutoff_jd": not graph_eligible,
        "graph_eligible": graph_eligible,
        "skills": skill_ids,
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
        # Train-window aggregates from scripts/build_job_behavior.py. Publishing
        # zeroes here silently disabled the `behavior` ranking feature for the
        # entire live corpus.
        "view_count": views,
        "apply_count": applies,
    }
    # Hybrid retrieval is opt-in: embedding generation is a synchronous
    # per-document Bedrock call, which dominates the whole pass when enabled.
    if embedding_client is not None:
        embedding = embedding_client.embed(job_embedding_text(row))
        if embedding is not None:
            document["embedding"] = embedding
    return document


def embedding_bulk_payload(
    index: str, vectors: Iterable[tuple[str, list[float]]]
) -> bytes:
    """Bulk body that adds only the embedding field to existing documents.

    A full `index` action would resend every field of all 1.2M documents just to
    attach a vector. Partial `update` keeps the backfill independent of the
    document schema and leaves the index queryable throughout.
    """
    lines: list[str] = []
    for job_id, vector in vectors:
        lines.append(
            json.dumps(
                {"update": {"_index": index, "_id": job_id}},
                separators=(",", ":"),
            )
        )
        lines.append(
            json.dumps({"doc": {"embedding": vector}}, separators=(",", ":"))
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


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


def run_embedding_backfill(
    args: Any, client: Any, embedding_client: Any
) -> None:
    """Vectorize an already-indexed corpus with concurrent Bedrock calls.

    Progress is printed per batch with the row offset so an interrupted run can
    resume with --skip-records. Rows whose embedding fails are counted and
    skipped rather than aborting the pass: a partially vectorized index is still
    correct because the API reports the real vector coverage and the kNN leg
    only ever appends candidates.
    """
    source_path = args.data_dir / "職缺.csv"
    batch_size = max(1, min(args.batch_size, 500))
    workers = max(1, min(args.embed_workers, 128))
    embedded = failed = rows = 0
    started = time.monotonic()

    def embed_row(row: dict[str, str]) -> tuple[str, list[float]] | None:
        job_id = row.get("職缺編號", "").strip()
        if not job_id:
            return None
        vector = embedding_client.embed(job_embedding_text(row))
        if vector is None:
            return None
        return job_id, vector

    def flush(pool: Any, batch: list[dict[str, str]]) -> None:
        nonlocal embedded, failed
        results = list(pool.map(embed_row, batch))
        vectors = [item for item in results if item is not None]
        failed += len(results) - len(vectors)
        if not vectors:
            return
        response = client.request(
            "POST",
            "/_bulk",
            embedding_bulk_payload(args.index, vectors),
            content_type="application/x-ndjson",
        )
        if response.get("errors"):
            errored = [
                item
                for item in response.get("items", [])
                if int(next(iter(item.values())).get("status", 500)) >= 300
            ]
            raise RuntimeError(
                "Embedding backfill bulk failed: "
                + json.dumps(errored[:3], ensure_ascii=False)
            )
        embedded += len(vectors)

    # One pool for the whole pass: a four-hour run must not pay thread setup on
    # every batch, and reusing threads keeps the boto3 connection pool warm.
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        with source_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for skipped, _ in enumerate(reader, 1):
                if skipped >= args.skip_records:
                    break
            if args.skip_records:
                print(f"Resuming after {args.skip_records:,} rows", flush=True)
            batch: list[dict[str, str]] = []
            for row in reader:
                if args.max_records > 0 and rows >= args.max_records:
                    break
                batch.append(row)
                rows += 1
                if len(batch) < batch_size:
                    continue
                flush(pool, batch)
                batch.clear()
                elapsed = max(0.001, time.monotonic() - started)
                print(
                    f"Embedded {embedded:,} jobs · {embedded / elapsed:,.0f} jobs/s "
                    f"· failed {failed:,} · row offset {args.skip_records + rows:,}",
                    flush=True,
                )
            if batch:
                flush(pool, batch)

    elapsed = max(0.001, time.monotonic() - started)
    print(
        json.dumps(
            {
                "mode": "embed_only_backfill",
                "index": args.index,
                "rows_read": rows,
                "embedded": embedded,
                "failed": failed,
                "embed_workers": workers,
                "elapsed_seconds": round(elapsed, 1),
                "jobs_per_second": round(embedded / elapsed, 1),
                "embedding_model_id": embedding_client.model_id,
                "resume_with_skip_records": args.skip_records + rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


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
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="Bulk requests kept in flight; overlaps parsing with network round trips",
    )
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument(
        "--skip-records",
        type=int,
        default=0,
        help=(
            "Resume a full-corpus pass by skipping this many leading CSV rows. "
            "Writes are keyed by _id, so re-covering rows is harmless; this only "
            "avoids repeating work after a dropped connection."
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--skip-create",
        action="store_true",
        help="Index into an existing compatible index",
    )
    parser.add_argument(
        "--behavior",
        type=Path,
        default=BEHAVIOR,
        help="Aggregates from scripts/build_job_behavior.py; skipped if missing",
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Generate embeddings via Bedrock for hybrid retrieval (slow)",
    )
    parser.add_argument(
        "--embed-only",
        action="store_true",
        help=(
            "Backfill embeddings onto an existing index without touching any "
            "other field. Embeddings are generated concurrently, so this is the "
            "only practical way to vectorize the full corpus; --embed generates "
            "them one at a time inside the single-threaded document builder."
        ),
    )
    parser.add_argument(
        "--embed-workers",
        type=int,
        default=64,
        help=(
            "Concurrent Bedrock embedding calls for --embed-only. Measured "
            "throughput in us-east-1: 34/s at 32, 82/s at 64, 86/s at 96, so 64 "
            "is where the curve flattens for a full-corpus pass."
        ),
    )
    parser.add_argument(
        "--update-mapping",
        action="store_true",
        help=(
            "PUT the new field properties onto an existing index before "
            "indexing. Adding fields is a non-breaking mapping update on a "
            "provisioned domain, so an already-deployed index can be upgraded "
            "in place: --update-mapping --skip-create re-bulks every document "
            "by _id and no delete or alias swap is needed."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate records without sending them",
    )
    args = parser.parse_args()
    if not args.dry_run and not args.endpoint:
        raise SystemExit("--endpoint or OPENSEARCH_ENDPOINT is required")
    if args.batch_size < 1 or args.batch_size > 2000:
        raise SystemExit("--batch-size must be between 1 and 2000")

    demo = json.loads(args.demo_index.read_text(encoding="utf-8"))
    skills = demo["skills"]
    pattern, alias_to_skills = compile_alias_matcher(skills)
    embedding_client = (
        BedrockEmbeddingClient.from_environment()
        if args.embed or args.embed_only
        else None
    )
    behavior: dict[str, list[int]] = {}
    if args.behavior and args.behavior.is_file():
        behavior = json.loads(args.behavior.read_text(encoding="utf-8"))["counts"]
        print(f"Loaded behavior for {len(behavior):,} jobs", flush=True)
    else:
        print(
            "No behavior artifact; view_count/apply_count will be 0. "
            "Run scripts/build_job_behavior.py first.",
            flush=True,
        )
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
    if client is not None and args.update_mapping:
        properties = mapping(serverless=client.service == "aoss")["mappings"][
            "properties"
        ]
        client.request(
            "PUT",
            f"{index_path}/_mapping",
            json.dumps({"properties": properties}, separators=(",", ":")).encode(
                "utf-8"
            ),
        )
        print(f"Updated mapping on {args.index} with {len(properties)} properties")

    if args.embed_only:
        if client is None:
            raise SystemExit("--embed-only requires a real endpoint")
        if embedding_client is None or not embedding_client.enabled:
            raise SystemExit("--embed-only requires BEDROCK_EMBEDDING_MODEL_ID")
        run_embedding_backfill(args, client, embedding_client)
        return

    def send(payload: bytes) -> None:
        response = client.request(
            "POST",
            "/_bulk",
            payload,
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

    indexed = graph_jobs = batches = 0
    started = time.monotonic()
    pending: list[dict[str, Any]] = []
    source_path = args.data_dir / "職缺.csv"
    # Sending one bulk request at a time leaves the whole round trip idle while
    # the next batch is parsed. Overlapping a bounded number of in-flight
    # requests is what turns a multi-hour full-corpus pass into minutes; the
    # semaphore keeps queued payloads from growing without limit.
    in_flight = threading.Semaphore(max(1, args.max_workers))
    failures: list[BaseException] = []
    failure_lock = threading.Lock()

    def dispatch(payload: bytes) -> None:
        try:
            send(payload)
        except BaseException as exc:  # surfaced on the main thread below
            with failure_lock:
                failures.append(exc)
        finally:
            in_flight.release()

    def submit(pool: Any, payload: bytes) -> None:
        with failure_lock:
            if failures:
                raise failures[0]
        in_flight.acquire()
        pool.submit(dispatch, payload)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, args.max_workers)
    ) as pool:
        with source_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for skipped, _ in enumerate(reader, 1):
                if skipped >= args.skip_records:
                    break
            if args.skip_records:
                print(f"Resuming after {args.skip_records:,} rows", flush=True)
            for row in reader:
                if args.max_records > 0 and indexed >= args.max_records:
                    break
                document = job_document(
                    row, skills, pattern, alias_to_skills, behavior, embedding_client
                )
                pending.append(document)
                graph_jobs += int(document["graph_eligible"])
                indexed += 1
                if len(pending) < args.batch_size:
                    continue
                if client is not None:
                    submit(pool, bulk_payload(args.index, pending))
                pending.clear()
                batches += 1
                if batches % 25 == 0:
                    elapsed = max(0.001, time.monotonic() - started)
                    print(
                        f"Indexed {indexed:,} jobs · {indexed / elapsed:,.0f} jobs/s",
                        flush=True,
                    )
        if pending and client is not None:
            submit(pool, bulk_payload(args.index, pending))
    if failures:
        raise failures[0]

    verified_count = None
    # A partial run (--max-records) or an in-place field upgrade (--skip-create)
    # overwrites documents by _id inside an index that already holds the whole
    # corpus, so the document count is expected not to equal this run's total.
    partial_update = args.max_records > 0 or args.skip_create
    if client is not None:
        if client.service != "aoss":
            client.request("POST", f"{index_path}/_refresh", b"{}")
        # Search collections become visible asynchronously. Polling also
        # avoids declaring a partial index complete immediately after _bulk.
        deadline = time.monotonic() + 180
        while True:
            count = client.request("POST", f"{index_path}/_count", b"{}")
            verified_count = int(count.get("count", -1))
            if verified_count >= indexed or time.monotonic() >= deadline:
                break
            time.sleep(5)
        if not partial_update and verified_count != indexed:
            raise RuntimeError(
                f"index count mismatch: expected {indexed:,}, got {verified_count:,}"
            )
        if partial_update and verified_count < indexed:
            raise RuntimeError(
                f"index holds {verified_count:,} documents after writing {indexed:,}"
            )

    report = {
        "schema": "skillweave-full-corpus-index-v1",
        "index": args.index,
        "source": str(source_path),
        "source_jobs_indexed": indexed,
        "verified_index_count": verified_count,
        "graph_eligible_jobs": graph_jobs,
        "cold_start_jobs": indexed - graph_jobs,
        "partial_update": partial_update,
        "all_source_jobs_are_search_targets": (
            verified_count >= indexed if verified_count is not None else None
        ),
        "dry_run": args.dry_run,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
