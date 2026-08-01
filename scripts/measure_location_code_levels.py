#!/usr/bin/env python3
"""Measure what level of location code searchers actually select.

The geo graph spec binds jobs to L4 districts, but `職缺.csv` only carries a
county-level `工作城市`. That makes the query side the only place a district
signal can come from, so the question that decides whether L4 edges can carry
behavioural weight instead of hand-assigned ones is simple: when a searcher
sets `c0`, do they pick a district code or a county code?

If district selection is common, district co-selection is computable the same
way county co-selection already is, and a hand-authored adjacency table becomes
a fallback rather than the only option. If it is rare, L4 weights have to be
authored and labelled as such.

The run also evaluates the spec's own example directly. 八里區 is claimed to
belong with 淡水區, 林口區 and 五股區 rather than 汐止區; whether searchers
agree is measurable.

Train-window only, anonymous rows dropped, matching the leakage policy used by
scripts/build_region_graph.py.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import normalize

DEFAULT_DATA = ROOT / "data" / "dataset"
DEFAULT_REPORT = ROOT / "reports" / "location-code-levels.json"
DOMESTIC_TOP_REGION = "台灣"
SPEC_FOCUS_COUNTY = "新北市"
SPEC_FOCUS_DISTRICT = "八里區"


def load_code_levels(path: Path) -> tuple[dict[str, dict[str, str]], Counter[str]]:
    """code -> {level, county, name} for domestic codes, plus an inventory."""
    codes: dict[str, dict[str, str]] = {}
    inventory: Counter[str] = Counter()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = row["CodeNo"]
            top = normalize(row.get("CodeNameC", ""))
            code_type = row.get("CodeType", "")
            name_a = normalize(row.get("CodeNameA", ""))
            name_b = normalize(row.get("CodeNameB", ""))
            if top != DOMESTIC_TOP_REGION:
                inventory[f"overseas_type{code_type}"] += 1
                continue
            if code_type == "2" and name_a:
                codes[code] = {"level": "county", "county": name_a, "name": name_a}
                inventory["domestic_county"] += 1
            elif code_type == "3" and name_b and name_a:
                codes[code] = {"level": "district", "county": name_b, "name": name_a}
                inventory["domestic_district"] += 1
            else:
                inventory[f"domestic_other_type{code_type}"] += 1
    return codes, inventory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--train-days",
        default="2026-06-01,2026-06-02,2026-06-03,2026-06-04,2026-06-05",
    )
    parser.add_argument("--min-co-selected", type=int, default=30)
    args = parser.parse_args()

    train_days = {day.strip() for day in args.train_days.split(",") if day.strip()}
    codes, inventory = load_code_levels(args.data_dir / "城市對照表.csv")
    started = time.monotonic()
    print(f"code inventory: {dict(inventory)}", flush=True)

    log_path = args.data_dir / "userSearchLog_20260601_20260607.csv"
    with log_path.open(encoding="utf-8-sig", newline="") as handle:
        fields = csv.DictReader(handle).fieldnames or []
    print(f"search log columns: {fields}", flush=True)

    stats = Counter()
    selection_level = Counter()
    search_shape = Counter()
    district_searches: Counter[str] = Counter()
    district_pairs: Counter[tuple[str, str]] = Counter()
    district_pick_counts = Counter()

    with log_path.open(encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), 1):
            if index % 500_000 == 0:
                print(f"  {index:,} rows", flush=True)
            stats["rows"] += 1
            if row.get("talentNo") == "0" or not row.get("empStr"):
                stats["anonymous_or_no_exposure"] += 1
                continue
            if row.get("search_time", "")[:10] not in train_days:
                stats["outside_train_days"] += 1
                continue
            raw = [code for code in (row.get("c0") or "").split(",") if code.strip()]
            if not raw:
                stats["no_location_filter"] += 1
                continue
            stats["with_location_filter"] += 1

            levels = set()
            districts: set[str] = set()
            for code in raw:
                entry = codes.get(code)
                if entry is None:
                    selection_level["unknown_or_overseas"] += 1
                    levels.add("other")
                    continue
                selection_level[entry["level"]] += 1
                levels.add(entry["level"])
                if entry["level"] == "district":
                    districts.add(f"{entry['county']}/{entry['name']}")

            if levels == {"county"}:
                search_shape["county_only"] += 1
            elif levels == {"district"}:
                search_shape["district_only"] += 1
            elif "district" in levels and "county" in levels:
                search_shape["mixed_county_and_district"] += 1
            else:
                search_shape["other_only"] += 1

            if districts:
                stats["searches_with_any_district_code"] += 1
                district_pick_counts[min(len(districts), 6)] += 1
                for key in districts:
                    district_searches[key] += 1
                by_county: dict[str, list[str]] = defaultdict(list)
                for key in districts:
                    by_county[key.split("/", 1)[0]].append(key)
                for members in by_county.values():
                    for a, b in combinations(sorted(members), 2):
                        district_pairs[(a, b)] += 1

    def jaccard(a: str, b: str, count: int) -> float:
        union = district_searches[a] + district_searches[b] - count
        return round(count / union, 5) if union else 0.0

    supported = [
        {
            "a": a,
            "b": b,
            "co_selected": count,
            "jaccard": jaccard(a, b, count),
            "p_b_given_a": round(count / district_searches[a], 5)
            if district_searches[a]
            else 0.0,
            "p_a_given_b": round(count / district_searches[b], 5)
            if district_searches[b]
            else 0.0,
        }
        for (a, b), count in district_pairs.items()
        if count >= args.min_co_selected
    ]
    supported.sort(key=lambda row: -row["jaccard"])

    focus_key = f"{SPEC_FOCUS_COUNTY}/{SPEC_FOCUS_DISTRICT}"
    focus = [
        {
            "other": row["b"] if row["a"] == focus_key else row["a"],
            "co_selected": row["co_selected"],
            "jaccard": row["jaccard"],
            "p_other_given_focus": row["p_b_given_a"]
            if row["a"] == focus_key
            else row["p_a_given_b"],
        }
        for row in supported
        if focus_key in (row["a"], row["b"])
    ]
    focus.sort(key=lambda row: -row["jaccard"])

    total_selections = sum(selection_level.values())
    total_filtered = max(1, stats["with_location_filter"])
    report = {
        "metadata": {
            "schema": "skillweave-location-code-levels-v1",
            "dataset_version": "1111-2026-06-01_2026-06-07",
            "train_days": sorted(train_days),
            "leakage_policy": "train days only; anonymous talentNo dropped",
            "min_co_selected": args.min_co_selected,
            "random_seed": 1111,
            "elapsed_seconds": None,
        },
        "code_inventory": dict(inventory),
        "corpus": dict(stats),
        "code_selections": {
            "total": total_selections,
            "by_level": dict(selection_level),
            "district_share_of_selections": round(
                selection_level["district"] / max(1, total_selections), 4
            ),
        },
        "search_shape": {
            **dict(search_shape),
            "district_only_share": round(
                search_shape["district_only"] / total_filtered, 4
            ),
            "any_district_share": round(
                stats["searches_with_any_district_code"] / total_filtered, 4
            ),
        },
        "district_selection": {
            "distinct_districts_selected": len(district_searches),
            "districts_per_search": dict(sorted(district_pick_counts.items())),
            "top_districts": [
                {"district": name, "searches": count}
                for name, count in district_searches.most_common(20)
            ],
        },
        "district_co_selection": {
            "pairs_total": len(district_pairs),
            "pairs_at_min_support": len(supported),
            "top": supported[:40],
        },
        "spec_example_八里區": {
            "focus": focus_key,
            "focus_searches": district_searches.get(focus_key, 0),
            "neighbours_at_min_support": focus,
        },
    }
    report["metadata"]["elapsed_seconds"] = round(time.monotonic() - started, 1)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote {args.report}", flush=True)


if __name__ == "__main__":
    main()
