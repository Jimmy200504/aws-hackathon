#!/usr/bin/env python3
"""Build a behaviour-derived region substitutability graph, train-only.

A map says two counties touch. This asks a different question: which counties do
job seekers themselves treat as interchangeable? Two independent signals answer
it without any hand-assigned weight.

  SUBSTITUTABLE_WITH   undirected. One search event whose c0 lists several
                       counties is the user declaring those counties equivalent
                       for that search, so no inference is required. Weight is
                       the Jaccard overlap of their search populations.

  COMMUTES_TO          directed. An application whose job county the talent never
                       searched. Commuting flows toward employment centres are
                       asymmetric, which an adjacency graph cannot express.

Both signals are restricted to the train window and the graph cutoff, matching
the rolling-snapshot policy the behavior graph already uses. Reading evaluation
week behaviour here would leak the labels the ranker is scored against.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import normalize

DEFAULT_DATA = ROOT / "data" / "dataset"
DEFAULT_OUTPUT = ROOT / "artifacts" / "region-graph.json"
TRAIN_CUTOFF = datetime.fromisoformat("2026-06-05 23:59:59.999")
DOMESTIC_TOP_REGION = "台灣"


def load_region_codes(path: Path) -> tuple[dict[str, str], set[str]]:
    """Map every location code to its domestic county, dropping overseas rows.

    Overseas selections are excluded because co-selecting whole continents
    expresses "anywhere abroad", not substitutability between two labour
    markets, and would otherwise dominate the ranking.
    """
    code_to_county: dict[str, str] = {}
    counties: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            top = normalize(row.get("CodeNameC", ""))
            if top != DOMESTIC_TOP_REGION:
                continue
            name_a = normalize(row.get("CodeNameA", ""))
            name_b = normalize(row.get("CodeNameB", ""))
            code_type = row.get("CodeType", "")
            county = ""
            if code_type == "2" and name_a:
                county = name_a
            elif code_type == "3" and name_b:
                county = name_b
            if not county or county == DOMESTIC_TOP_REGION:
                continue
            code_to_county[row["CodeNo"]] = county
            counties.add(county)
    return code_to_county, counties


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--train-days",
        default="2026-06-01,2026-06-02,2026-06-03,2026-06-04,2026-06-05",
    )
    parser.add_argument(
        "--min-co-selected",
        type=int,
        default=100,
        help="drop pairs below this support so noise is not published as an edge",
    )
    parser.add_argument("--min-flow", type=int, default=30)
    args = parser.parse_args()

    train_days = {d.strip() for d in args.train_days.split(",") if d.strip()}
    code_to_county, counties = load_region_codes(args.data_dir / "城市對照表.csv")
    started = time.monotonic()

    pair_counts: Counter[tuple[str, str]] = Counter()
    county_searches: Counter[str] = Counter()
    talent_counties: dict[str, set[str]] = defaultdict(set)
    searches = multi_county = skipped_day = 0

    print("Pass 1/3 · train-window co-selection…", flush=True)
    with (args.data_dir / "userSearchLog_20260601_20260607.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            if row.get("talentNo") == "0" or not row.get("empStr"):
                continue
            if row.get("search_time", "")[:10] not in train_days:
                skipped_day += 1
                continue
            codes = [c for c in row.get("c0", "").split(",") if c.strip()]
            if not codes:
                continue
            selected = {code_to_county[c] for c in codes if c in code_to_county}
            if not selected:
                continue
            searches += 1
            for county in selected:
                county_searches[county] += 1
            talent_counties[row["talentNo"]] |= selected
            if len(selected) >= 2:
                multi_county += 1
                for a, b in combinations(sorted(selected), 2):
                    pair_counts[(a, b)] += 1

    print("Pass 2/3 · job county lookup…", flush=True)
    job_county: dict[str, str] = {}
    with (args.data_dir / "職缺.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            city = normalize(row.get("工作城市", ""))
            if city in counties:
                job_county[row["職缺編號"]] = city

    print("Pass 3/3 · pre-cutoff cross-county applications…", flush=True)
    flow: Counter[tuple[str, str]] = Counter()
    applications = inside = outside = unresolved = post_cutoff = 0
    with (args.data_dir / "主動應徵_0601-0607.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            stamp = row.get("datein", "")
            try:
                if datetime.fromisoformat(stamp) > TRAIN_CUTOFF:
                    post_cutoff += 1
                    continue
            except ValueError:
                post_cutoff += 1
                continue
            target = job_county.get(row.get("empNo", ""))
            wanted = talent_counties.get(row.get("talentNo", ""))
            if not target or not wanted:
                unresolved += 1
                continue
            applications += 1
            if target in wanted:
                inside += 1
                continue
            outside += 1
            for source in wanted:
                flow[(source, target)] += 1

    substitutable = []
    for (a, b), count in pair_counts.items():
        if count < args.min_co_selected:
            continue
        union = county_searches[a] + county_searches[b] - count
        substitutable.append(
            {
                "a": a,
                "b": b,
                "co_selected": count,
                "jaccard": round(count / union, 5) if union else 0.0,
                "conditional_a_given_b": round(count / county_searches[b], 5)
                if county_searches[b]
                else 0.0,
                "conditional_b_given_a": round(count / county_searches[a], 5)
                if county_searches[a]
                else 0.0,
            }
        )
    substitutable.sort(key=lambda edge: -edge["jaccard"])

    commutes = []
    for (source, target), count in flow.items():
        if count < args.min_flow:
            continue
        reverse = flow.get((target, source), 0)
        commutes.append(
            {
                "source": source,
                "target": target,
                "applications": count,
                "reverse_applications": reverse,
                # 1.0 means entirely one-directional, 0.0 means balanced.
                "asymmetry": round((count - reverse) / (count + reverse), 4)
                if count + reverse
                else 0.0,
            }
        )
    commutes.sort(key=lambda edge: -edge["applications"])

    payload = {
        "metadata": {
            "schema": "skillweave-region-graph-v1",
            "method": "behaviour-derived; no hand-assigned edge weight",
            "dataset_version": "1111-2026-06-01_2026-06-07",
            "graph_cutoff": TRAIN_CUTOFF.isoformat(sep=" "),
            "train_days": sorted(train_days),
            "leakage_policy": (
                "co-selection restricted to train days; applications restricted "
                "to on-or-before the graph cutoff"
            ),
            "domestic_only": True,
            "overseas_excluded_reason": (
                "co-selecting continents expresses 'anywhere abroad', not "
                "substitutability between two labour markets"
            ),
            "min_co_selected": args.min_co_selected,
            "min_flow": args.min_flow,
            "random_seed": 1111,
        },
        "stats": {
            "counties": len(counties),
            "train_searches_with_region_filter": searches,
            "searches_spanning_multiple_counties": multi_county,
            "multi_county_rate": round(multi_county / max(1, searches), 4),
            "eval_window_searches_skipped": skipped_day,
            "applications_resolved": applications,
            "applications_inside_searched_counties": inside,
            "applications_outside_searched_counties": outside,
            "cross_county_rate": round(outside / max(1, applications), 4),
            "applications_unresolved": unresolved,
            "applications_after_cutoff_excluded": post_cutoff,
            "substitutable_edges": len(substitutable),
            "commute_edges": len(commutes),
            "elapsed_seconds": round(time.monotonic() - started, 1),
        },
        "substitutable_with": substitutable,
        "commutes_to": commutes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload["stats"], ensure_ascii=False, indent=2))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
