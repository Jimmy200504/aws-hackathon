#!/usr/bin/env python3
"""Materialise the L4 district table so the layer is readable without the dataset.

L1, L3 and L5 are all checked in as config files. L4 was the exception: it is
derived at run time from `data/dataset/城市對照表.csv`, which `.gitignore`
excludes because organizer-provided data must not be published. The consequence
was that the largest and most load-bearing layer of the geo graph was the only
one a reader could not inspect.

What this writes is public administrative geography - 22 counties, 368
districts, their official codes - and carries no job, employer or search
information. It is the same reference table the government publishes.

It does not make `extract_job_districts.py` runnable without the dataset;
nothing can, because that script reads 1.2 million postings. What it makes
inspectable is the surface derivation, which is where the extractor's behaviour
actually comes from:

  full_name        八里區, 信義區          the official name
  suffix_dropped   八里, 信義              the form job titles actually use
  collisions       dropped, because two districts in one county sharing a short
                   form cannot be told apart

`scripts/extract_job_districts.py` and `scripts/build_district_graph.py` prefer
this file when it is present and fall back to the CSV, so the checked-in table
is the thing under test rather than a stale copy beside it.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import normalize

DEFAULT_SOURCE = ROOT / "data" / "dataset" / "城市對照表.csv"
DEFAULT_OUTPUT = ROOT / "config" / "geo-l4-districts.json"
DOMESTIC_TOP_REGION = "台灣"
DISTRICT_SUFFIXES = "區鄉鎮市"


def build(source: Path) -> dict:
    counties: set[str] = set()
    districts: list[dict[str, str]] = []
    full: dict[str, dict[str, str]] = defaultdict(dict)
    stripped_raw: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    with source.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if normalize(row.get("CodeNameC", "")) != DOMESTIC_TOP_REGION:
                continue
            code_type = row.get("CodeType", "")
            name_a = normalize(row.get("CodeNameA", ""))
            name_b = normalize(row.get("CodeNameB", ""))
            if code_type == "2" and name_a:
                counties.add(name_a)
            elif code_type == "3" and name_a and name_b:
                districts.append(
                    {"code": row["CodeNo"], "county": name_b, "district": name_a}
                )
                full[name_a][name_b] = name_a
                if len(name_a) > 1 and name_a[-1] in DISTRICT_SUFFIXES:
                    short = name_a[:-1]
                    # A single character is not a usable surface: 東區 -> 東.
                    if len(short) >= 2:
                        stripped_raw[short][name_b].add(name_a)

    stripped: dict[str, dict[str, str]] = {}
    collisions: dict[str, dict[str, list[str]]] = {}
    for short, per_county in stripped_raw.items():
        clashing = {
            county: sorted(names)
            for county, names in per_county.items()
            if len(names) > 1
        }
        if clashing:
            collisions[short] = clashing
            continue
        stripped[short] = {
            county: next(iter(names)) for county, names in per_county.items()
        }

    districts.sort(key=lambda row: row["code"])
    return {
        "schema": "skillweave-geo-l4-districts-v1",
        "provenance": "public administrative geography",
        "source": "data/dataset/城市對照表.csv, CodeType 2 = county and 3 = district",
        "contains": (
            "county and district names with their official location codes; no "
            "job, employer, applicant or search information"
        ),
        "surface_layers": {
            "full_name": "the official district name as written in the code table",
            "suffix_dropped": (
                "the name with its 區/鄉/鎮/市 suffix removed, which is the form "
                "job titles use; a short form under two characters is not emitted"
            ),
        },
        "collision_policy": (
            "a short form naming two districts inside one county is dropped "
            "entirely, because the posting's 工作城市 cannot disambiguate it"
        ),
        "counts": {
            "counties": len(counties),
            "districts": len(districts),
            "full_name_surfaces": len(full),
            "suffix_dropped_surfaces": len(stripped),
            "dropped_collisions": len(collisions),
        },
        "counties": sorted(counties),
        "districts": districts,
        "surfaces": {
            "full_name": {name: dict(sorted(m.items())) for name, m in sorted(full.items())},
            "suffix_dropped": {
                name: dict(sorted(m.items())) for name, m in sorted(stripped.items())
            },
        },
        "intra_county_collisions": dict(sorted(collisions.items())),
    }


def load_table(path: Path) -> tuple[set[str], dict[str, dict[str, dict[str, str]]], dict[str, set[str]]]:
    """Read the checked-in table in the shape `load_districts` returns."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    collisions = {
        short: set(per_county)
        for short, per_county in payload.get("intra_county_collisions", {}).items()
    }
    return (
        set(payload["counties"]),
        {
            "full": payload["surfaces"]["full_name"],
            "stripped": payload["surfaces"]["suffix_dropped"],
        },
        collisions,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.source.is_file():
        raise SystemExit(
            f"Missing {args.source}. This script regenerates the table from the "
            "organizer dataset; the generated table itself is checked in."
        )
    payload = build(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload["counts"], ensure_ascii=False, indent=2))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
