#!/usr/bin/env python3
"""Verify local dataset inputs match the fingerprints the team builds from.

The deterministic pipeline reproduces byte-identical graph and benchmark
artifacts from identical inputs — that property is what lets the repository
track only fingerprints instead of distributing tens of gigabytes of derived
data. It also means a mismatched input is the one failure mode that silently
makes two developers' results incomparable.

Run this before `make quality`, `make index` or `make graph-full-build`.

The CSVs carry pseudonymized identifiers and must not be published, so this
script only compares digests; it never uploads or prints record content.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FINGERPRINTS = ROOT / "config" / "dataset-fingerprints.json"
DEFAULT_DATA_DIR = ROOT / "data" / "dataset"
BLOCK = 1024 * 1024


def digest_and_lines(path: Path) -> tuple[str, int, int]:
    sha = hashlib.sha256()
    lines = 0
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(BLOCK), b""):
            sha.update(block)
            lines += block.count(b"\n")
            size += len(block)
    return sha.hexdigest(), size, lines


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check dataset inputs against config/dataset-fingerprints.json"
    )
    parser.add_argument("--fingerprints", type=Path, default=DEFAULT_FINGERPRINTS)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="compare size only; skips hashing multi-gigabyte files",
    )
    args = parser.parse_args()

    document = json.loads(args.fingerprints.read_text(encoding="utf-8"))
    expected = document["files"]
    derived = document.get("derived", {})

    print(f"dataset_version: {document.get('dataset_version')}")
    print(f"mode: {'size only' if args.quick else 'sha256'}")
    print()

    failures: list[str] = []
    for name, want in expected.items():
        path = args.data_dir / name
        if not path.is_file():
            hint = derived.get(name)
            note = f" ({hint})" if hint else ""
            print(f"  MISSING  {name}{note}")
            failures.append(name)
            continue
        size = path.stat().st_size
        if args.quick:
            ok = size == want["bytes"]
            detail = "" if ok else f" size {size:,} != {want['bytes']:,}"
        else:
            sha, size, lines = digest_and_lines(path)
            ok = sha == want["sha256"]
            detail = ""
            if not ok:
                detail = f" sha256 {sha[:16]}… != {want['sha256'][:16]}…"
                if size != want["bytes"]:
                    detail += f", size {size:,} != {want['bytes']:,}"
                if lines != want["lines"]:
                    detail += f", lines {lines:,} != {want['lines']:,}"
        print(f"  {'OK      ' if ok else 'MISMATCH'} {name}{detail}")
        if not ok:
            failures.append(name)

    print()
    if failures:
        print(f"{len(failures)} input(s) do not match the expected fingerprints:")
        for name in failures:
            hint = derived.get(name)
            if hint:
                print(f"  - {name}: {hint}")
            else:
                print(f"  - {name}: obtain the original organizer file")
        print()
        print(
            "Artifacts built from these inputs will not be comparable with the "
            "team's. Resolve the differences before building."
        )
        sys.exit(1)
    print("All dataset inputs match. Derived artifacts will be reproducible.")


if __name__ == "__main__":
    main()
