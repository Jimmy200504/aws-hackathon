#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CUTOFF = "2026-06-05 23:59:59.999"
FIELDS = [
    "職務名稱",
    "職務內容",
    "電腦技能資料",
    "工作技能",
    "專業證照",
]


def stable_hash(value: str) -> str:
    return hashlib.sha256(("1111:" + value).encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a bounded, high-impact train-only Bedrock pilot"
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=ROOT / "artifacts" / "demo-index.json",
    )
    parser.add_argument(
        "--jobs-csv",
        type=Path,
        default=ROOT / "data" / "dataset" / "職缺.csv",
    )
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts" / "bedrock-pilot" / "input.jsonl",
    )
    args = parser.parse_args()
    index = json.loads(args.index.read_text(encoding="utf-8"))
    behavior = index.get("behavior_graph", {})
    global_stats = behavior.get("job_global", {})
    eligible = [
        job
        for job in index["jobs"]
        if job.get("graph_eligible", False)
    ]
    top_count = max(1, int(args.count * 0.6))
    popular = sorted(
        eligible,
        key=lambda job: (
            -int(global_stats.get(job["id"], [0])[0]),
            job["id"],
        ),
    )[:top_count]
    selected = {job["id"] for job in popular}
    diverse = sorted(
        (job for job in eligible if job["id"] not in selected),
        key=lambda job: stable_hash(job["id"]),
    )
    selected.update(
        job["id"]
        for job in diverse[: max(0, args.count - len(selected))]
    )

    records = []
    with args.jobs_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["職缺編號"] not in selected:
                continue
            if row["職缺最後修改時間"] > CUTOFF:
                continue
            records.append(
                {
                    "job_id": row["職缺編號"],
                    **{
                        field: row.get(field, "")[:6000]
                        for field in FIELDS
                    },
                    "職務分類": [
                        row.get("職務大類", ""),
                        row.get("職務中類", ""),
                        row.get("職務小類", ""),
                    ],
                    "職缺最後修改時間": row["職缺最後修改時間"],
                    "selection": (
                        "high_exposure"
                        if row["職缺編號"]
                        in {job["id"] for job in popular}
                        else "deterministic_diversity"
                    ),
                }
            )
    records.sort(key=lambda record: record["job_id"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "records": len(records),
                "high_exposure": sum(
                    row["selection"] == "high_exposure"
                    for row in records
                ),
                "deterministic_diversity": sum(
                    row["selection"] == "deterministic_diversity"
                    for row in records
                ),
                "characters": sum(
                    len(str(row.get(field, "")))
                    for row in records
                    for field in FIELDS
                ),
                "sha256": hashlib.sha256(
                    args.output.read_bytes()
                ).hexdigest(),
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
