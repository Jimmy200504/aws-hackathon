#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replace fatal pilot records with their bounded retry result"
    )
    parser.add_argument("--accepted", type=Path, required=True)
    parser.add_argument("--quarantine", type=Path, required=True)
    parser.add_argument("--retry-accepted", type=Path, required=True)
    parser.add_argument("--retry-quarantine", type=Path, required=True)
    parser.add_argument("--output-accepted", type=Path, required=True)
    parser.add_argument("--output-quarantine", type=Path, required=True)
    args = parser.parse_args()
    retry_accepted = read_jsonl(args.retry_accepted)
    retry_quarantine = read_jsonl(args.retry_quarantine)
    retried_ids = {
        row["job_id"] for row in [*retry_accepted, *retry_quarantine]
    }
    accepted = [*read_jsonl(args.accepted), *retry_accepted]
    quarantine = [
        row
        for row in read_jsonl(args.quarantine)
        if row["job_id"] not in retried_ids
    ]
    quarantine.extend(retry_quarantine)
    accepted.sort(key=lambda row: row["job_id"])
    quarantine.sort(key=lambda row: row["job_id"])
    write_jsonl(args.output_accepted, accepted)
    write_jsonl(args.output_quarantine, quarantine)
    print(
        f"Merged {len(accepted)} accepted and "
        f"{len(quarantine)} quarantined records"
    )


if __name__ == "__main__":
    main()
