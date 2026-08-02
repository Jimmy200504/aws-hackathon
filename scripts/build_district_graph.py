#!/usr/bin/env python3
"""Build a behaviour-derived district substitutability graph, train-only.

`scripts/build_region_graph.py` answers "which counties do job seekers treat as
interchangeable" and stops there, because `職缺.csv` carries only a county-level
`工作城市`. But the query side is finer than the job side: 73.05% of all `c0`
selections are district codes, and 366 of 368 domestic districts get selected at
least once (`reports/location-code-levels.json`). So the same question is
answerable one level down, and answering it removes the need for the geo graph
spec's hand-assigned `is_adjacent_to = 20`.

  SUBSTITUTABLE_WITH   undirected. One search whose c0 lists several districts
                       is the user declaring those districts equivalent for that
                       search. Weight is the Jaccard overlap of their search
                       populations, plus both conditional probabilities, because
                       the overlap is often strongly one-directional.

Two things this build does that the earlier measurement did not.

Cross-county district pairs are included. The measurement grouped selections by
county before pairing them, so 汐止區/南港區 — a corridor that straddles the
新北市/台北市 line — could not appear. Commuting does not stop at the county
boundary, and `same_county` on every edge keeps the two populations separable.

Searches selecting more than `--max-districts-per-search` districts are dropped.
That is 0.01% of multi-district searches but ~0.4% of all pair evidence, and the
behaviour it encodes is "anywhere", not substitutability — the same reason
`build_region_graph.py` drops overseas co-selection.

No `COMMUTES_TO` edge is emitted at this level. Application flow needs the job's
district, and jobs resolve to a district for only 27.79% of the corpus
(`reports/job-district-extraction.json`), which is both partial and
systematically non-random. County-level commute edges stay in the region graph
until that coverage is understood.
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
DEFAULT_OUTPUT = ROOT / "artifacts" / "district-graph.json"
TRAIN_CUTOFF = datetime.fromisoformat("2026-06-05 23:59:59.999")
DOMESTIC_TOP_REGION = "台灣"


def load_district_codes(path: Path) -> dict[str, tuple[str, str]]:
    """Map every domestic district code to its `(county, district)` pair.

    CodeType 3 rows carry the district in CodeNameA and its county in
    CodeNameB. County rows (CodeType 2) are deliberately not loaded: a search
    that filters on 新北市 says nothing about which districts inside it the
    searcher would accept.
    """
    table = ROOT / "config" / "geo-l4-districts.json"
    if table.is_file():
        # Same reason as scripts/extract_job_districts.py: the checked-in table
        # is the one a reader can inspect, so it is the one that runs.
        payload = json.loads(table.read_text(encoding="utf-8"))
        return {
            row["code"]: (row["county"], row["district"]) for row in payload["districts"]
        }
    districts: dict[str, tuple[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if normalize(row.get("CodeNameC", "")) != DOMESTIC_TOP_REGION:
                continue
            if row.get("CodeType") != "3":
                continue
            district = normalize(row.get("CodeNameA", ""))
            county = normalize(row.get("CodeNameB", ""))
            if not district or not county or county == DOMESTIC_TOP_REGION:
                continue
            districts[row["CodeNo"]] = (county, district)
    return districts


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
        default=30,
        help="drop pairs below this support so noise is not published as an edge",
    )
    parser.add_argument(
        "--max-districts-per-search",
        type=int,
        default=10,
        help="drop 'select everything' searches, which express reach not substitutability",
    )
    args = parser.parse_args()

    train_days = {day.strip() for day in args.train_days.split(",") if day.strip()}
    code_to_district = load_district_codes(args.data_dir / "城市對照表.csv")
    node_codes: dict[str, set[str]] = defaultdict(set)
    for code, (county, district) in code_to_district.items():
        node_codes[f"{county}/{district}"].add(code)
    started = time.monotonic()
    print(f"district codes: {len(code_to_district)} -> {len(node_codes)} nodes", flush=True)

    stats: Counter[str] = Counter()
    district_searches: Counter[str] = Counter()
    pair_counts: Counter[tuple[str, str]] = Counter()

    log_path = args.data_dir / "userSearchLog_20260601_20260607.csv"
    with log_path.open(encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), 1):
            if index % 1_000_000 == 0:
                print(f"  {index:,} rows", flush=True)
            stats["rows"] += 1
            if row.get("talentNo") == "0" or not row.get("empStr"):
                stats["anonymous_or_no_exposure"] += 1
                continue
            if row.get("search_time", "")[:10] not in train_days:
                stats["eval_window_searches_skipped"] += 1
                continue
            selected = {
                code_to_district[code]
                for code in (row.get("c0") or "").split(",")
                if code in code_to_district
            }
            if not selected:
                continue
            if len(selected) > args.max_districts_per_search:
                stats["searches_dropped_broad_selection"] += 1
                stats["pairs_dropped_broad_selection"] += (
                    len(selected) * (len(selected) - 1) // 2
                )
                continue
            stats["searches_with_district_filter"] += 1
            keys = sorted(f"{county}/{district}" for county, district in selected)
            for key in keys:
                district_searches[key] += 1
            if len(keys) < 2:
                continue
            stats["searches_spanning_multiple_districts"] += 1
            for a, b in combinations(keys, 2):
                pair_counts[(a, b)] += 1

    edges = []
    for (a, b), count in pair_counts.items():
        if count < args.min_co_selected:
            continue
        union = district_searches[a] + district_searches[b] - count
        edges.append(
            {
                "a": a,
                "b": b,
                "same_county": a.split("/", 1)[0] == b.split("/", 1)[0],
                "co_selected": count,
                "jaccard": round(count / union, 5) if union else 0.0,
                # conditional_a_given_b is P(a | b): the share of searchers who
                # picked b that also picked a. Naming matches the region graph.
                "conditional_a_given_b": round(count / district_searches[b], 5)
                if district_searches[b]
                else 0.0,
                "conditional_b_given_a": round(count / district_searches[a], 5)
                if district_searches[a]
                else 0.0,
            }
        )
    # Total order so the artifact is byte-identical across runs.
    edges.sort(key=lambda edge: (-edge["jaccard"], -edge["co_selected"], edge["a"], edge["b"]))

    linked = {key for edge in edges for key in (edge["a"], edge["b"])}
    nodes = [
        {
            "id": key,
            "county": key.split("/", 1)[0],
            "district": key.split("/", 1)[1],
            "codes": sorted(node_codes[key]),
            "searches": district_searches.get(key, 0),
            "degree": sum(1 for edge in edges if key in (edge["a"], edge["b"])),
        }
        for key in sorted(node_codes)
    ]

    cross = sum(1 for edge in edges if not edge["same_county"])
    payload = {
        "metadata": {
            "schema": "skillweave-district-graph-v1",
            "method": "behaviour-derived; no hand-assigned edge weight",
            "dataset_version": "1111-2026-06-01_2026-06-07",
            "graph_cutoff": TRAIN_CUTOFF.isoformat(sep=" "),
            "train_days": sorted(train_days),
            "leakage_policy": (
                "district co-selection restricted to train days; anonymous "
                "talentNo dropped"
            ),
            "domestic_only": True,
            "provenance": "behaviour",
            "min_co_selected": args.min_co_selected,
            "max_districts_per_search": args.max_districts_per_search,
            "commute_edges_excluded_reason": (
                "application flow needs the job's district, and only 27.79% of "
                "jobs resolve to one; see reports/job-district-extraction.json"
            ),
            "random_seed": 1111,
        },
        "stats": {
            "districts_in_code_table": len(nodes),
            "districts_selected": len(district_searches),
            "districts_with_at_least_one_edge": len(linked),
            "search_rows": stats["rows"],
            "searches_with_district_filter": stats["searches_with_district_filter"],
            "searches_spanning_multiple_districts": stats[
                "searches_spanning_multiple_districts"
            ],
            "searches_dropped_broad_selection": stats["searches_dropped_broad_selection"],
            "pairs_dropped_broad_selection": stats["pairs_dropped_broad_selection"],
            "eval_window_searches_skipped": stats["eval_window_searches_skipped"],
            "anonymous_or_no_exposure": stats["anonymous_or_no_exposure"],
            "pairs_observed": len(pair_counts),
            "substitutable_edges": len(edges),
            "cross_county_edges": cross,
            "same_county_edges": len(edges) - cross,
            "elapsed_seconds": round(time.monotonic() - started, 1),
        },
        "nodes": nodes,
        "substitutable_with": edges,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload["stats"], ensure_ascii=False, indent=2))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
