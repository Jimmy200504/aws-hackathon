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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a retry input containing only fatal Bedrock records"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--quarantine", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    retry_ids = {
        row["job_id"]
        for row in read_jsonl(args.quarantine)
        if row.get("fatal")
    }
    rows = [
        row for row in read_jsonl(args.input) if row["job_id"] in retry_ids
    ]
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(f"Wrote {len(rows)} fatal retries to {args.output}")


if __name__ == "__main__":
    main()
