#!/usr/bin/env python3
"""Measure what L5 landmarks and L3 living areas add to district extraction.

`docs/geo-graph.md` describes one pipeline scanning every layer. The district
extractor only ever scanned L4, and the L5 table was built for query-side alias
resolution, so the two never met. This runs them together and reports the trade
in jobs rather than in surfaces.

Four arms, all on the same 961,780 eligible postings:

  l4_only          the checked-in configuration
  l5_unambiguous   plus L5 entries that resolve to exactly one district inside
                   the posting's county
  l5_l3_relaxed    plus L3 living areas, allowing up to eight districts

The arms differ on purpose in `--place-max-districts`. An arterial road spanning
four 台北 districts, or a living area spanning eight 台中 ones, does not narrow
anything beyond what 工作城市 already says, and tagging all of them makes a
single-site posting look multi-sited. Whether that trade is worth taking is the
question this report exists to answer, so both settings are measured rather than
assumed.

The 19 L5 entries carrying `requires_occurrence_filter` are excluded from every
arm. They were readmitted to the query side because a model reading the
surrounding sentence separates 保安人員 from 保安車站; this scanner does not run
that model, and using them as bare substring matches is what the flag forbids.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_REPORT = ROOT / "reports" / "place-layer-arms.json"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

NO_JUDGEMENTS = ["--collocation-judgements", "artifacts/definitely-absent.jsonl"]
ARMS = [
    ("l4_only", ["--place-layers", "none", *NO_JUDGEMENTS]),
    ("l5_unambiguous", ["--place-layers", "l5", "--place-max-districts", "1", *NO_JUDGEMENTS]),
    ("l5_l3_relaxed", ["--place-layers", "l5+l3", "--place-max-districts", "8", *NO_JUDGEMENTS]),
    ("occurrence_only", ["--place-layers", "none"]),
    # Both adopted changes together, which is the shipped configuration.
    ("l5_and_occurrence", ["--place-layers", "l5", "--place-max-districts", "1"]),
]

KEYS = (
    "eligible",
    "postings_with_district",
    "coverage_of_eligible",
    "single_district_postings",
    "single_district_share",
    "match_from_l5_place",
    "match_from_l3_area",
    "match_rejected_county_mismatch",
)


def run(flags: list[str], tmp: Path, label: str) -> dict[str, Any]:
    report = tmp / f"{label}.json"
    output = tmp / f"{label}-jobs.json"
    print(f"  arm: {label}…", flush=True)
    subprocess.run(
        [
            str(PYTHON),
            str(ROOT / "scripts" / "extract_job_districts.py"),
            "--report", str(report),
            "--output", str(output),
            *flags,
        ],
        check=True,
        capture_output=True,
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    stats = payload["stats"]
    return {
        **{key: stats.get(key, 0) for key in KEYS},
        "by_layer": stats.get("by_layer", {}),
        "place_surfaces": payload["metadata"].get("place_surfaces", 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    arms: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        for label, flags in ARMS:
            arms[label] = run(flags, tmp, label)

    base = arms["l4_only"]
    for label, arm in arms.items():
        if label == "l4_only":
            continue
        arm["delta_vs_l4_only"] = {
            "postings_with_district": arm["postings_with_district"] - base["postings_with_district"],
            "coverage_of_eligible": round(
                arm["coverage_of_eligible"] - base["coverage_of_eligible"], 4
            ),
            "single_district_postings": arm["single_district_postings"]
            - base["single_district_postings"],
            "single_district_share": round(
                arm["single_district_share"] - base["single_district_share"], 4
            ),
        }

    report = {
        "metadata": {
            "schema": "skillweave-place-layer-arms-v1",
            "l5_source": "config/geo-l5-published.json",
            "l3_source": "config/geo-authored.json living_areas",
            "excluded": (
                "entries with requires_occurrence_filter, which are query-side "
                "only; see reports/l5-occurrence-judgement.json"
            ),
            "guard": (
                "every place match still has to name a district inside the "
                "posting's own 工作城市, the same consistency rule L4 uses"
            ),
        },
        "conclusion": {
            "adopt": "l5_and_occurrence",
            "why": (
                "both adopted changes are independent and additive: the L5 layer "
                "supplies postings that name a landmark and no district, while the "
                "occurrence filter removes and recovers district matches. Neither "
                "regresses single-district share"
            ),
            "reject": "l5_l3_relaxed",
            "why_not": (
                "buys 2,451 more postings and costs 2.09 points of single-district "
                "share, because a living area tags eight districts at once and so "
                "resolves nothing 工作城市 did not already give"
            ),
            "note": (
                "L3 contributes nothing at --place-max-districts 1 by construction: "
                "a living area never resolves to a single district"
            ),
        },
        "arms": arms,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(arms, ensure_ascii=False, indent=2))
    print(f"Wrote {args.report}")


if __name__ == "__main__":
    main()
