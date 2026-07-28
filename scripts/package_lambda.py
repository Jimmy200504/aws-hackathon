#!/usr/bin/env python3
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "dist" / "skillweave-lambda.zip"


def main() -> None:
    parser = argparse.ArgumentParser(description="Create deterministic Lambda source bundle")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    include = [
        *sorted((ROOT / "app").glob("*.py")),
        *sorted((ROOT / "web").glob("*")),
        ROOT / "artifacts" / "demo-index.json",
    ]
    missing = [path for path in include if not path.is_file()]
    if missing:
        raise SystemExit("Missing package inputs: " + ", ".join(map(str, missing)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # ZIP_DEFLATED output can differ across zlib releases. The compact artifact
    # stays well below Lambda's limit, so store fixed bytes for a portable hash.
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in include:
            relative = path.relative_to(ROOT)
            info = zipfile.ZipInfo(str(relative), date_time=(2026, 6, 7, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    print(f"Wrote {args.output} ({args.output.stat().st_size / 1_048_576:.1f} MiB)")


if __name__ == "__main__":
    main()
