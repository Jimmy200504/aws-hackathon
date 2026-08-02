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
    # Without the vocabulary and prompt the query normalizer silently falls back
    # to the legacy string contract: no closed vocabulary, no validation, no
    # structure. The intent files are optional -- a Lambda invocation serves one
    # request, so neither batching nor pre-warming applies there, and shipping
    # the precomputed head of the query distribution is what makes structured
    # intents reachable; absent, every query just takes the live path.
    required_config = [
        ROOT / "config" / "query-intent-vocab.json",
        ROOT / "config" / "query-intent-prompt.txt",
    ]
    optional_config = [
        ROOT / "config" / "query-intents.json",
        ROOT / "config" / "query-intents-release.json",
    ]
    required_web = [
        ROOT / "web" / "index.html",
        ROOT / "web" / "app.js",
        ROOT / "web" / "styles.css",
    ]
    include = [
        *sorted((ROOT / "app").glob("*.py")),
        *required_config,
        *(path for path in optional_config if path.is_file()),
        *required_web,
        *(
            path
            for path in sorted((ROOT / "web").glob("*"))
            if path not in required_web
        ),
        ROOT / "artifacts" / "demo-index.json",
        (
            ROOT
            / "artifacts"
            / "models"
            / "ltr-quality-remote-salary-intent.trees.json"
        ),
    ]
    # Both merge sides listed the config files; a duplicate name makes the
    # archive larger and makes unzip prompt for overwrite.
    include = list(dict.fromkeys(include))
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
