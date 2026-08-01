#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.deterministic_extract import (
    ExactAliasMatcher,
    load_ontology,
    normalize_surface,
)
from scripts.index_full_opensearch import SignedOpenSearchClient


def document_id(surface: str) -> str:
    return "alias-" + hashlib.sha256(surface.encode()).hexdigest()[:24]


def bulk_payload(index: str, documents: list[dict[str, Any]]) -> bytes:
    lines: list[str] = []
    for document in documents:
        lines.append(
            json.dumps(
                {"index": {"_index": index, "_id": document_id(document["normalized_surface"])}},
                separators=(",", ":"),
            )
        )
        lines.append(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish immutable reviewed exact aliases to OpenSearch"
    )
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--ontology", type=Path, default=ROOT / "config/skill_ontology.seed.json")
    parser.add_argument("--icap", type=Path, default=ROOT / "config/icap_vocabulary.reviewed.json")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/skill-alias-index-v2.json")
    args = parser.parse_args()

    terms = load_ontology(args.ontology, args.icap)
    matcher = ExactAliasMatcher(terms)
    by_id = {term.node_id: term for term in terms}
    documents = []
    for surface, canonical_id in sorted(matcher.alias_to_node.items()):
        term = by_id[canonical_id]
        documents.append(
            {
                "normalized_surface": surface,
                "canonical_ids": [canonical_id],
                "canonical_label": term.label,
                "node_type": term.node_type,
                "blocked_phrases": sorted(
                    {
                        normalize_surface(phrase)
                        for phrase in term.blocked_phrases
                        if normalize_surface(phrase)
                    }
                ),
                "resolution": "EXACT_REVIEWED_ALIAS",
            }
        )

    client = SignedOpenSearchClient(args.endpoint, args.region, 20)
    mapping = {
        "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 1}},
        "mappings": {
            "dynamic": False,
            "properties": {
                "normalized_surface": {
                    "type": "text",
                    "analyzer": "cjk",
                    "fields": {"keyword": {"type": "keyword"}},
                },
                "canonical_ids": {"type": "keyword"},
                "canonical_label": {"type": "keyword"},
                "node_type": {"type": "keyword"},
                "blocked_phrases": {"type": "keyword"},
                "resolution": {"type": "keyword"},
            },
        },
    }
    client.request("PUT", f"/{args.index}", json.dumps(mapping).encode())
    response = client.request(
        "POST",
        "/_bulk",
        bulk_payload(args.index, documents),
        content_type="application/x-ndjson",
    )
    if response.get("errors"):
        raise RuntimeError("alias index bulk request contains item errors")
    client.request("POST", f"/{args.index}/_refresh")
    count = client.request("GET", f"/{args.index}/_count")
    published = int(count.get("count", -1))
    checks = {
        "non_empty": bool(documents),
        "all_exact_reviewed": all(
            document["resolution"] == "EXACT_REVIEWED_ALIAS" for document in documents
        ),
        "collision_free": len(documents)
        == len({document["normalized_surface"] for document in documents}),
        "published_count_matches": published == len(documents),
    }
    report = {
        "metadata": {
            "schema": "skillweave-skill-alias-index-v1",
            "verified_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "index": args.index,
            "endpoint": args.endpoint,
            "resolution": "exact_reviewed_alias_only",
        },
        "passed": all(checks.values()),
        "checks": checks,
        "documents": len(documents),
        "published": published,
        "blocked_phrase_documents": sum(bool(row["blocked_phrases"]) for row in documents),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
