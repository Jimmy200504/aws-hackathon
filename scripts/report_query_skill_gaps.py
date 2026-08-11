#!/usr/bin/env python3
"""Find high-traffic search queries that resolve to no reviewed skill node.

Item 1-style expansion (adding tool names mined from job postings) mostly adds
surfaces that jobs already state literally, so HAS_SKILL matches them directly
and RELATED_TO never has to fire. The edges that actually change ranking are
the ones reached when a query resolves to skill A while the relevant job only
carries skill B.

This script attacks that from the demand side instead: it ranks the real search
log by query frequency and reports which queries the current reviewed ontology
cannot resolve to any Skill node. Those are the gaps where added nodes (and the
statistically derived relations they unlock) can influence retrieval.

Read-only; proposes nothing and writes no ontology.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import SkillWeaveRanker

DEFAULT_LOG = ROOT / "data" / "dataset" / "userSearchLog_cleaned.csv"
DEFAULT_INDEX = ROOT / "artifacts" / "demo-index.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Rank real search queries that the reviewed ontology cannot resolve "
            "to any Skill node, to target ontology work at retrieval-relevant gaps."
        )
    )
    parser.add_argument("--search-log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument(
        "--max-rows",
        type=int,
        default=3_000_000,
        help="cap rows scanned from the search log",
    )
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument(
        "--min-count", type=int, default=200, help="minimum query frequency"
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    counts: Counter[str] = Counter()
    scanned = 0
    with args.search_log.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            scanned += 1
            if scanned > args.max_rows:
                break
            query = (row.get("ks") or "").strip()
            if query:
                counts[query] += 1

    ranker = SkillWeaveRanker(args.index)
    total_queries = sum(counts.values())

    unresolved: list[dict] = []
    resolved_traffic = 0
    for query, count in counts.most_common():
        if count < args.min_count:
            break
        intent = ranker.parse_intent(query, None, None)
        skills = [s for s in intent.skills if s.startswith("skill.")]
        if skills:
            resolved_traffic += count
            continue
        unresolved.append(
            {
                "query": query,
                "count": count,
                "share": count / total_queries,
                "resolved_occupations": [
                    s for s in intent.skills if not s.startswith("skill.")
                ],
            }
        )

    print(
        f"scanned {scanned:,} log rows, {len(counts):,} distinct queries, "
        f"{total_queries:,} total searches"
    )
    print(
        f"ontology skill nodes: "
        f"{sum(1 for s in ranker.skills.values() if s.get('type', 'Skill') == 'Skill')}"
    )
    print(
        f"head queries (count >= {args.min_count:,}) resolving to >=1 skill: "
        f"{resolved_traffic:,} searches"
    )
    print(f"head queries resolving to NO skill: {len(unresolved):,} distinct")
    print()
    print(f"{'count':>9} {'share':>7}  {'occupation match':<28} query")
    for item in unresolved[: args.limit]:
        occ = ",".join(item["resolved_occupations"])[:26] or "-"
        print(
            f"{item['count']:>9,} {item['share'] * 100:>6.3f}%  {occ:<28} {item['query'][:40]}"
        )

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "schema": "unresolved-query-gaps-v1",
                    "scanned_rows": scanned,
                    "distinct_queries": len(counts),
                    "total_searches": total_queries,
                    "min_count": args.min_count,
                    "unresolved": unresolved[: args.limit],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
