#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data" / "dataset"
DEMO_INDEX = ROOT / "artifacts" / "demo-index.json"
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
) -> tuple[re.Pattern[str], dict[str, list[str]]]:
    alias_to_skills: dict[str, list[str]] = {}
    for skill_id, spec in skills.items():
        for value in [spec.get("label", ""), *spec.get("aliases", [])]:
            alias = norm(str(value))
            if alias:
                alias_to_skills.setdefault(alias, []).append(skill_id)
    alternatives = "|".join(
        (
            rf"(?<![a-z0-9.+#]){re.escape(alias)}(?![a-z0-9.+#])"
            if alias.isascii()
            else re.escape(alias)
        )
        for alias in sorted(alias_to_skills, key=len, reverse=True)
    )
    return re.compile(alternatives, re.IGNORECASE), alias_to_skills


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
                "skill_labels": {"type": "text", "analyzer": "cjk"},
                "skill_evidence": {"type": "object", "enabled": False},
                "skill_confidence": {"type": "object", "enabled": False},
                "skill_provenance": {"type": "object", "enabled": False},
                "freshness": {"type": "float"},
                "view_count": {"type": "integer"},
                "apply_count": {"type": "integer"},
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
    pattern: re.Pattern[str],
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
    pattern: re.Pattern[str],
    alias_to_skills: dict[str, list[str]],
) -> dict[str, Any]:
    modified = parse_time(row["職缺最後修改時間"])
    graph_eligible = modified <= TRAIN_CUTOFF
    if graph_eligible:
        skill_ids, evidence, confidence = matched_skills(
            row, pattern, alias_to_skills
        )
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
    return {
        "id": row["職缺編號"],
        "title": row["職務名稱"].strip(),
        "description": row["職務內容"].strip()[:420],
        "description_search": row["職務內容"].strip(),
        "salary": row["薪資"].replace("‧", " · "),
        "city": row["工作城市"],
        "categories": categories,
        "industry": row["產業中類"] or row["產業大類"],
        "company_id": row["廠商編號"],
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
        # Full train-only behavior aggregates can be published in a later
        # version without changing the online feature contract.
        "view_count": 0,
        "apply_count": 0,
    }


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
    args = parser.parse_args()
    if not args.dry_run and not args.endpoint:
        raise SystemExit("--endpoint or OPENSEARCH_ENDPOINT is required")
    if args.batch_size < 1 or args.batch_size > 2000:
        raise SystemExit("--batch-size must be between 1 and 2000")

    demo = json.loads(args.demo_index.read_text(encoding="utf-8"))
    skills = demo["skills"]
    pattern, alias_to_skills = compile_alias_matcher(skills)
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

    indexed = graph_jobs = batches = 0
    started = time.monotonic()
    pending: list[dict[str, Any]] = []
    source_path = args.data_dir / "職缺.csv"
    with source_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if args.max_records > 0 and indexed >= args.max_records:
                break
            document = job_document(
                row, skills, pattern, alias_to_skills
            )
            pending.append(document)
            graph_jobs += int(document["graph_eligible"])
            indexed += 1
            if len(pending) < args.batch_size:
                continue
            if client is not None:
                response = client.request(
                    "POST",
                    "/_bulk",
                    bulk_payload(args.index, pending),
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
            pending.clear()
            batches += 1
            if batches % 25 == 0:
                elapsed = max(0.001, time.monotonic() - started)
                print(
                    f"Indexed {indexed:,} jobs · {indexed / elapsed:,.0f} jobs/s",
                    flush=True,
                )

    if pending and client is not None:
        response = client.request(
            "POST",
            "/_bulk",
            bulk_payload(args.index, pending),
            content_type="application/x-ndjson",
        )
        if response.get("errors"):
            raise RuntimeError("final OpenSearch bulk request contained errors")

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
            if verified_count == indexed or time.monotonic() >= deadline:
                break
            time.sleep(5)
        if verified_count != indexed:
            raise RuntimeError(
                f"index count mismatch: expected {indexed:,}, got {verified_count:,}"
            )

    report = {
        "schema": "skillweave-full-corpus-index-v1",
        "index": args.index,
        "source": str(source_path),
        "source_jobs_indexed": indexed,
        "verified_index_count": verified_count,
        "graph_eligible_jobs": graph_jobs,
        "cold_start_jobs": indexed - graph_jobs,
        "all_source_jobs_are_search_targets": (
            verified_count == indexed if verified_count is not None else None
        ),
        "dry_run": args.dry_run,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
