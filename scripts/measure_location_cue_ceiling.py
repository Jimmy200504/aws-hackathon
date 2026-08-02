#!/usr/bin/env python3
"""Measure how much district coverage is left to win, before anyone tries to win it.

The extractor resolves a district for 27.79% of eligible postings. The obvious
next move is a bigger place-name table, and the obvious question is whether that
would help. It is answerable directly: take the postings that resolved to no
district and ask what location information they contain at all.

Two passes, because the loose version of this measurement is badly wrong and it
is worth recording why.

  loose   any of 路/街/巷, any park word, any landmark word
  tight   an actual address pattern (X路N號, N段, N巷), a park name, a landmark

The loose pass reports 24.44% and is not usable: 路 matches 網路 and 通路, and
內科 matches 內科醫師 rather than 內湖科技園區. The tight pass reports 2.16%.
A ceiling estimate that is off by a factor of eleven would have justified a
great deal of work that could not have paid off.

The second question is whether L1 macro-regions and L3 living areas are worth
scanning for at all, since they are the two layers `docs/geo-graph.md` asks for
that the extractor does not use. They are counted here with their county
distribution, which is what shows that most of their occurrences are sales
territories rather than locations.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import normalize

DEFAULT_DATA = ROOT / "data" / "dataset"
DEFAULT_DISTRICTS = ROOT / "artifacts" / "job-districts.json"
DEFAULT_AUTHORED = ROOT / "config" / "geo-authored.json"
DEFAULT_REPORT = ROOT / "reports" / "location-cue-ceiling.json"
GRAPH_CUTOFF = datetime.fromisoformat("2026-06-05 23:59:59.999")

NUM = "0-9０-９一二三四五六七八九十"
# A real address, not 網路 or 通路.
ADDRESS = re.compile(rf"[路街][{NUM}]{{1,4}}[段巷弄號]|[路街大道][^，。\s]{{0,6}}[{NUM}]{{1,4}}號|[{NUM}]{{1,4}}號")
PARK = re.compile(r"(工業區|科學園區|加工出口區|科技園區|工業園區|軟體園區|生醫園區)")
LANDMARK = re.compile(r"(竹科|南科|中科|七期|北車|大墩|文心|南崁|頂崁|大發|樹谷|南港軟體|林口長庚)")
LOOSE_ROAD = re.compile(r"[路街巷弄大道]")
LOOSE_LANDMARK = re.compile(r"(竹科|南科|中科|內科|七期|北車|文心|大墩)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--districts", type=Path, default=DEFAULT_DISTRICTS)
    parser.add_argument("--authored", type=Path, default=DEFAULT_AUTHORED)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    resolved = {
        job["job_id"]
        for job in json.loads(args.districts.read_text(encoding="utf-8"))["jobs"]
    }
    print(f"postings already resolved to a district: {len(resolved):,}", flush=True)

    counties: set[str] = set()
    with (args.data_dir / "城市對照表.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if normalize(row.get("CodeNameC", "")) == "台灣" and row.get("CodeType") == "2":
                counties.add(normalize(row.get("CodeNameA", "")))

    authored = json.loads(args.authored.read_text(encoding="utf-8"))
    macro: dict[str, str] = {}
    for region in authored["regions"]:
        macro[region["id"].split("/", 1)[1]] = "L1"
    for area in authored["living_areas"]:
        for alias in area.get("aliases", []):
            macro[alias] = "L3"
    for extra in ("北台灣", "中台灣", "南台灣", "東台灣", "大台北", "大高雄", "大台中"):
        macro.setdefault(extra, "colloquial")
    macro_pattern = re.compile("|".join(re.escape(s) for s in sorted(macro, key=len, reverse=True)))

    stats = Counter()
    macro_hits: Counter[str] = Counter()
    macro_counties: dict[str, Counter[str]] = {}
    started = time.monotonic()

    with (args.data_dir / "職缺.csv").open(encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), 1):
            if index % 400_000 == 0:
                print(f"  {index:,} rows", flush=True)
            try:
                if datetime.fromisoformat(row.get("職缺最後修改時間", "")) > GRAPH_CUTOFF:
                    continue
            except ValueError:
                continue
            county = normalize(row.get("工作城市", ""))
            if county not in counties:
                continue
            stats["eligible"] += 1
            text = f"{normalize(row.get('職務名稱', ''))} {normalize(row.get('職務內容', ''))}"

            for surface in {m.group(0) for m in macro_pattern.finditer(text)}:
                macro_hits[surface] += 1
                macro_counties.setdefault(surface, Counter())[county] += 1

            if row["職缺編號"] in resolved:
                continue
            stats["unresolved"] += 1

            loose = bool(
                LOOSE_ROAD.search(text) or PARK.search(text) or LOOSE_LANDMARK.search(text)
                or any(name in text for name in counties)
            )
            stats["loose_cue"] += int(loose)
            address, park, landmark = (
                bool(ADDRESS.search(text)), bool(PARK.search(text)), bool(LANDMARK.search(text))
            )
            stats["tight_address"] += int(address)
            stats["tight_park"] += int(park)
            stats["tight_landmark"] += int(landmark)
            stats["tight_cue"] += int(address or park or landmark)

    unresolved = max(1, stats["unresolved"])
    report = {
        "metadata": {
            "schema": "skillweave-location-cue-ceiling-v1",
            "dataset_version": "1111-2026-06-01_2026-06-07",
            "graph_cutoff": GRAPH_CUTOFF.isoformat(sep=" "),
            "leakage_policy": "only postings last modified on or before the graph cutoff",
            "resolved_source": str(args.districts.name),
            "elapsed_seconds": round(time.monotonic() - started, 1),
        },
        "corpus": {
            "eligible": stats["eligible"],
            "resolved_to_a_district": len(resolved),
            "unresolved": stats["unresolved"],
        },
        "ceiling": {
            "loose_cue": stats["loose_cue"],
            "loose_share": round(stats["loose_cue"] / unresolved, 4),
            "loose_warning": (
                "not usable: 路 matches 網路 and 通路, 內科 matches 內科醫師. "
                "Recorded because the loose figure overstates the ceiling by 11x "
                "and would have justified work that could not pay off"
            ),
            "tight_cue": stats["tight_cue"],
            "tight_share": round(stats["tight_cue"] / unresolved, 4),
            "tight_breakdown": {
                "address": stats["tight_address"],
                "industrial_park": stats["tight_park"],
                "landmark": stats["tight_landmark"],
            },
            "no_cue_at_all": unresolved - stats["tight_cue"],
            "no_cue_share": round((unresolved - stats["tight_cue"]) / unresolved, 4),
            "reading": (
                "the unresolved postings overwhelmingly do not state a location "
                "beyond 工作城市, so text extraction is near its ceiling and a "
                "larger place-name table cannot move coverage far"
            ),
        },
        "macro_surfaces": {
            "rule": "L1 regions, L3 living areas and colloquial macro terms, counted over all eligible postings",
            "reading": (
                "most occurrences are sales territories rather than locations "
                "(駐區業務代表(中彰投)), and L1 is coarser than 工作城市 already is, "
                "which is why these layers are not scanned by the extractor"
            ),
            "surfaces": [
                {
                    "surface": surface,
                    "layer": macro[surface],
                    "postings": count,
                    "share_of_eligible": round(count / max(1, stats["eligible"]), 5),
                    "top_counties": dict(macro_counties[surface].most_common(3)),
                }
                for surface, count in macro_hits.most_common()
            ],
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["ceiling"], ensure_ascii=False, indent=2))
    print(f"Wrote {args.report}")


if __name__ == "__main__":
    main()
