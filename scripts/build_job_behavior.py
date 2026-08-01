#!/usr/bin/env python3
"""Aggregate per-job view and apply counts for the full-corpus index.

`scripts/index_full_opensearch.py` published `view_count: 0` / `apply_count: 0`
for every posting, which zeroes the `behavior` ranking feature on the live path
even though the organizer ships 8.2M view rows and 226k apply rows. This builds
the aggregate once so indexing stays a single streaming pass.

Counts are capped at the graph cutoff by default so the artifact can also feed
leakage-sensitive offline work; pass --no-cutoff for a live-only build.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "dataset"
OUTPUT = ROOT / "artifacts" / "job-behavior.json"
TRAIN_CUTOFF = "2026-06-05 23:59:59.999"

SOURCES = [
    ("職缺瀏覽_20260601_20260607.csv", "employeeNo", "dateIn", 0),
    ("主動應徵_0601-0607.csv", "empNo", "datein", 1),
]


def aggregate(data_dir: Path, cutoff: str | None) -> tuple[dict[str, list[int]], dict]:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    stats = {"view_rows": 0, "apply_rows": 0, "skipped_after_cutoff": 0}
    for filename, job_field, time_field, slot in SOURCES:
        path = data_dir / filename
        scanned = 0
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                scanned += 1
                if cutoff and row.get(time_field, "") > cutoff:
                    stats["skipped_after_cutoff"] += 1
                    continue
                job_id = (row.get(job_field) or "").strip()
                if job_id:
                    counts[job_id][slot] += 1
        stats["view_rows" if slot == 0 else "apply_rows"] = scanned
        print(f"  {filename}: {scanned:,} rows", flush=True)
    return dict(counts), stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--cutoff",
        default=TRAIN_CUTOFF,
        help="Ignore events after this timestamp (ISO, string comparison)",
    )
    parser.add_argument(
        "--no-cutoff",
        action="store_true",
        help="Count every event; use for a live-only index build",
    )
    args = parser.parse_args()
    cutoff = None if args.no_cutoff else args.cutoff
    if cutoff:
        # Fail loudly rather than silently comparing against a malformed string.
        datetime.fromisoformat(cutoff)

    print("Aggregating job behavior…", flush=True)
    counts, stats = aggregate(args.data_dir, cutoff)
    payload = {
        "schema": "skillweave-job-behavior-v1",
        "cutoff": cutoff,
        "stats": {
            **stats,
            "jobs_with_activity": len(counts),
            "total_views": sum(value[0] for value in counts.values()),
            "total_applies": sum(value[1] for value in counts.values()),
        },
        "counts": counts,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(json.dumps(payload["stats"], ensure_ascii=False, indent=2))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    sys.exit(main())
