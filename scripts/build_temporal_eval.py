#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "dataset"
INDEX = ROOT / "artifacts" / "demo-index.json"
OUTPUT = ROOT / "artifacts" / "temporal-eval.json"


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def stable_key(value: str) -> str:
    return hashlib.sha256(("1111:" + value).encode()).hexdigest()


def load_events(
    data_dir: Path, days: set[str], eligible_jobs: set[str]
) -> dict[str, dict[str, list[tuple[datetime, int]]]]:
    events: dict[str, dict[str, list[tuple[datetime, int]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    sources = [
        ("職缺瀏覽_20260601_20260607.csv", "employeeNo", "dateIn", 1),
        ("主動應徵_0601-0607.csv", "empNo", "datein", 2),
    ]
    for filename, job_field, time_field, grade in sources:
        print(f"Reading {filename}…", flush=True)
        with (data_dir / filename).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                timestamp = row[time_field]
                if timestamp[:10] not in days:
                    continue
                talent = row["talentNo"]
                job_id = row[job_field]
                if talent == "0" or job_id not in eligible_jobs:
                    continue
                events[talent][job_id].append((dt(timestamp), grade))
    for jobs in events.values():
        for values in jobs.values():
            values.sort()
    return events


def relevance_after(
    values: list[tuple[datetime, int]], search_time: datetime, window: timedelta
) -> int:
    index = bisect.bisect_left(values, (search_time, 0))
    grade = 0
    end = search_time + window
    while index < len(values) and values[index][0] <= end:
        grade = max(grade, values[index][1])
        index += 1
    return grade


def build_cases(
    data_dir: Path,
    days: set[str],
    events: dict[str, dict[str, list[tuple[datetime, int]]]],
    eligible_jobs: set[str],
    max_per_day: int,
    window_minutes: int,
    candidate_depth: int,
) -> dict[str, list[dict]]:
    reservoirs: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    window = timedelta(minutes=window_minutes)
    print("Reading search log and constructing temporally ordered qrels…", flush=True)
    with (data_dir / "userSearchLog_20260601_20260607.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            day = row["search_time"][:10]
            talent = row["talentNo"]
            if day not in days or talent == "0" or talent not in events or not row["empStr"]:
                continue
            exposed = list(
                dict.fromkeys(
                    job_id
                    for job_id in row["empStr"].split(",")[:candidate_depth]
                    if job_id in eligible_jobs
                )
            )
            if len(exposed) < 2:
                continue
            searched_at = dt(row["search_time"])
            qrels: dict[str, int] = {}
            for job_id in exposed:
                values = events[talent].get(job_id)
                grade = relevance_after(values, searched_at, window) if values else 0
                qrels[job_id] = grade
            if max(qrels.values(), default=0) <= 0:
                continue
            identity = "|".join(
                [row["ks"], row["c0"], row["d0"], row["search_time"], talent]
            )
            case = {
                "query_id": "q_" + stable_key(identity)[:16],
                "query": row["ks"],
                "location_code": [x for x in row["c0"].split(",") if x],
                "duty_code": [x for x in row["d0"].split(",") if x],
                "search_time": row["search_time"],
                "candidates": exposed,
                "qrels": qrels,
                "label_policy": "max(view=1, apply=2) in [search_time, search_time+30m]",
            }
            priority = stable_key(identity)
            bucket = reservoirs[day]
            bucket.append((priority, case))
            if len(bucket) > max_per_day * 3:
                bucket.sort(key=lambda item: item[0])
                del bucket[max_per_day:]
    return {
        day: [case for _, case in sorted(values, key=lambda item: item[0])[:max_per_day]]
        for day, values in reservoirs.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build leak-safe temporal reranking qrels")
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--index", type=Path, default=INDEX)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--days", default="2026-06-06,2026-06-07")
    parser.add_argument("--max-per-day", type=int, default=500)
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--candidate-depth", type=int, default=100)
    args = parser.parse_args()
    days = {value.strip() for value in args.days.split(",") if value.strip()}
    artifact = json.loads(args.index.read_text(encoding="utf-8"))
    # Only graph-cutoff-eligible jobs are benchmarked in this local ablation.
    # Cold-start is reported separately and never gains graph features.
    eligible = {job["id"] for job in artifact["jobs"] if job.get("graph_eligible")}
    events = load_events(args.data_dir, days, eligible)
    cases = build_cases(
        args.data_dir,
        days,
        events,
        eligible,
        args.max_per_day,
        args.window_minutes,
        args.candidate_depth,
    )
    payload = {
        "metadata": {
            "schema": "skillweave-temporal-qrels-v1",
            "days": sorted(days),
            "candidate_policy": f"rerank original exposure top-{args.candidate_depth}",
            "event_window_minutes": args.window_minutes,
            "anonymous_users_excluded": True,
            "future_event_direction": "events must occur at or after query",
            "graph_cutoff_eligible_only": True,
            "random_seed": 1111,
            "cases_per_day": {day: len(rows) for day, rows in cases.items()},
            "caveat": "The provided data has no session_id. A documented 30-minute forward window approximates session attribution.",
        },
        "splits": {
            "validation": cases.get("2026-06-06", []),
            "test": cases.get("2026-06-07", []),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload["metadata"], ensure_ascii=False, indent=2))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
