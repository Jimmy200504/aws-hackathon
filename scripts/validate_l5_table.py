#!/usr/bin/env python3
"""Test every authored L5 entry against the job corpus before publishing it.

`config/geo-l5-table.json` is written from geographic knowledge, not from the
dataset, which means it can be wrong in three distinct ways. Each gets its own
check here, because they fail differently and only two of them are visible to a
single test.

  invented      a station or park that does not exist. No corpus support, so
                `appearances` is zero or trivially small.
  misassigned   a real place put in the wrong county. Support exists but
                concentrates somewhere other than the claimed county.
  misplaced     a real place in the right county but the wrong district. County
                concentration cannot see this at all, so postings that mention
                the surface are cross-checked against the L4 districts the
                extractor independently resolved for the same postings.

The concentration gate reuses the threshold `scripts/mine_region_aliases.py`
established: ten non-place control words top out at 26.66% and the highest
rejected real candidate reaches 46.47%, so 0.60 sits between the two
populations with margin rather than on a boundary.

Nothing is published on the author's say-so. An entry that no posting mentions
is not evidence of absence - it may simply be a place nobody advertises jobs in -
so it is reported as `unsupported` rather than `wrong`, and left out of the
published set either way.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import normalize

DEFAULT_TABLE = ROOT / "config" / "geo-l5-table.json"
DEFAULT_DATA = ROOT / "data" / "dataset"
DEFAULT_DISTRICTS = ROOT / "artifacts" / "job-districts.json"
DEFAULT_REPORT = ROOT / "reports" / "l5-table-validation.json"
DEFAULT_PUBLISHED = ROOT / "config" / "geo-l5-published.json"
GRAPH_CUTOFF = datetime.fromisoformat("2026-06-05 23:59:59.999")
STATION_KINDS = {"metro_station", "lrt_station", "hsr_station", "tra_station"}


def variants(entry: dict) -> list[str]:
    """Surface forms a posting might use. Stations are often written with 站."""
    forms = {entry["surface"]}
    if any(kind in STATION_KINDS for kind in entry["kind"].split("+")):
        if not entry["surface"].endswith("站"):
            forms.add(entry["surface"] + "站")
    return sorted(forms)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--districts", type=Path, default=DEFAULT_DISTRICTS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--published", type=Path, default=DEFAULT_PUBLISHED)
    parser.add_argument("--min-appearances", type=int, default=20)
    parser.add_argument("--min-concentration", type=float, default=0.60)
    parser.add_argument(
        "--min-district-agreement",
        type=float,
        default=0.50,
        help="share of co-resolved postings whose L4 district is one this entry claims",
    )
    args = parser.parse_args()

    table = json.loads(args.table.read_text(encoding="utf-8"))
    entries = {entry["surface"]: entry for entry in table["entries"]}
    form_to_surface: dict[str, str] = {}
    for entry in entries.values():
        for form in variants(entry):
            form_to_surface.setdefault(form, entry["surface"])
    # Longest first so 台北車站 wins over 北車 where both could match.
    pattern = re.compile(
        "|".join(re.escape(form) for form in sorted(form_to_surface, key=len, reverse=True))
    )
    print(f"{len(entries)} entries, {len(form_to_surface)} surface forms", flush=True)

    job_districts: dict[str, list[str]] = {}
    if args.districts.is_file():
        payload = json.loads(args.districts.read_text(encoding="utf-8"))
        for job in payload["jobs"]:
            job_districts[job["job_id"]] = [
                f"{job['county']}/{item['district']}" for item in job["districts"]
            ]
        print(f"L4 cross-check available for {len(job_districts):,} postings", flush=True)

    counties: set[str] = set()
    with (args.data_dir / "城市對照表.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if normalize(row.get("CodeNameC", "")) == "台灣" and row.get("CodeType") == "2":
                counties.add(normalize(row.get("CodeNameA", "")))

    seen: dict[str, Counter[str]] = defaultdict(Counter)
    resolved: dict[str, Counter[str]] = defaultdict(Counter)
    # Posting-level, not pair-level. A posting that resolves to three districts
    # must count once, otherwise its own two non-matching districts dilute the
    # one that matches and a correct entry scores below 0.5 by arithmetic.
    agreement_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    examples: dict[str, str] = {}
    stats = Counter()
    started = time.monotonic()

    with (args.data_dir / "職缺.csv").open(encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), 1):
            if index % 300_000 == 0:
                print(f"  {index:,} rows", flush=True)
            stats["rows"] += 1
            try:
                if datetime.fromisoformat(row.get("職缺最後修改時間", "")) > GRAPH_CUTOFF:
                    continue
            except ValueError:
                continue
            county = normalize(row.get("工作城市", ""))
            if county not in counties:
                continue
            stats["eligible"] += 1
            title = normalize(row.get("職務名稱", ""))
            content = normalize(row.get("職務內容", ""))
            text = f"{title} {content}"
            hits = {form_to_surface[m.group(0)] for m in pattern.finditer(text)}
            if not hits:
                continue
            stats["postings_with_any_l5"] += 1
            districts = job_districts.get(row["職缺編號"], ())
            for surface in hits:
                seen[surface][county] += 1
                for district in districts:
                    resolved[surface][district] += 1
                if districts:
                    tally = agreement_counts[surface]
                    tally[0] += 1
                    if not set(districts).isdisjoint(entries[surface]["districts"]):
                        tally[1] += 1
                if surface not in examples:
                    position = text.find(surface)
                    examples[surface] = text[max(0, position - 12) : position + 24].strip()

    rows_out = []
    for surface, entry in sorted(entries.items()):
        observed = seen.get(surface, Counter())
        appearances = sum(observed.values())
        top_county, top_count = (observed.most_common(1) or [(None, 0)])[0]
        concentration = top_count / appearances if appearances else 0.0
        claimed = set(entry["counties"])

        co = resolved.get(surface, Counter())
        co_total, agree = agreement_counts.get(surface, [0, 0])
        agreement = agree / co_total if co_total else None

        if appearances < args.min_appearances:
            verdict = "unsupported"
        elif top_county not in claimed:
            verdict = "misassigned_county"
        elif concentration < args.min_concentration:
            verdict = "ambiguous_surface"
        elif agreement is not None and co_total >= 20 and agreement < args.min_district_agreement:
            verdict = "district_disputed"
        else:
            verdict = "published"

        rows_out.append(
            {
                "surface": surface,
                "kind": entry["kind"],
                "claimed_counties": entry["counties"],
                "claimed_districts": entry["districts"],
                "appearances": appearances,
                "top_county": top_county,
                "concentration": round(concentration, 4),
                "county_distribution": dict(observed.most_common(4)),
                "l4_cross_check": {
                    "postings_with_l4": co_total,
                    "agreeing": agree,
                    "agreement": round(agreement, 4) if agreement is not None else None,
                    "top_districts": dict(co.most_common(4)),
                }
                if co_total
                else None,
                "example": examples.get(surface),
                "verdict": verdict,
            }
        )

    by_verdict = Counter(row["verdict"] for row in rows_out)
    published = [row for row in rows_out if row["verdict"] == "published"]
    report = {
        "metadata": {
            "schema": "skillweave-l5-table-validation-v1",
            "source_table": "config/geo-l5-table.json",
            "dataset_version": "1111-2026-06-01_2026-06-07",
            "graph_cutoff": GRAPH_CUTOFF.isoformat(sep=" "),
            "leakage_policy": "only postings last modified on or before the graph cutoff",
            "min_appearances": args.min_appearances,
            "min_concentration": args.min_concentration,
            "min_district_agreement": args.min_district_agreement,
            "gate_calibration": (
                "0.60 sits between the 0.2666 ceiling of the ten non-place control "
                "words and the 0.4647 of the highest rejected candidate in "
                "reports/region-alias-candidates.json"
            ),
            "elapsed_seconds": round(time.monotonic() - started, 1),
        },
        "corpus": dict(stats),
        "summary": {
            "authored": len(rows_out),
            **{verdict: count for verdict, count in sorted(by_verdict.items())},
            "published_share": round(len(published) / max(1, len(rows_out)), 4),
        },
        "entries": sorted(rows_out, key=lambda row: (row["verdict"], -row["appearances"])),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    args.published.write_text(
        json.dumps(
            {
                "schema": "skillweave-geo-l5-published-v1",
                "provenance": "authored, corpus-validated",
                "validation_report": "reports/l5-table-validation.json",
                "gate": {
                    "min_appearances": args.min_appearances,
                    "min_concentration": args.min_concentration,
                    "min_district_agreement": args.min_district_agreement,
                },
                "entries": [
                    {
                        "id": f"L5/{row['surface']}",
                        "surface": row["surface"],
                        "kind": row["kind"],
                        "districts": row["claimed_districts"],
                        "appearances": row["appearances"],
                        "concentration": row["concentration"],
                        "district_agreement": (row["l4_cross_check"] or {}).get("agreement"),
                    }
                    for row in sorted(published, key=lambda row: -row["appearances"])
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Wrote {args.report} and {args.published}")


if __name__ == "__main__":
    main()
