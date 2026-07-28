#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release-manifest.json"
PLACEHOLDER_HOSTS = {
    "example.com",
    "example.net",
    "example.org",
    "localhost",
    "127.0.0.1",
}


def validate_public_https_url(value: str) -> str:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or host in PLACEHOLDER_HOSTS
        or any(host.endswith(f".{reserved}") for reserved in PLACEHOLDER_HOSTS)
        or "placeholder" in value.lower()
    ):
        raise argparse.ArgumentTypeError(
            "deliverable URL must be a non-placeholder public HTTPS URL"
        )
    return value.strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register verified external deliverable URLs"
    )
    parser.add_argument("--aws-url", type=validate_public_https_url)
    parser.add_argument("--github-url", type=validate_public_https_url)
    parser.add_argument("--demo-video-url", type=validate_public_https_url)
    args = parser.parse_args()
    updates = {
        "aws_url": args.aws_url,
        "github_url": args.github_url,
        "demo_video_url": args.demo_video_url,
    }
    updates = {key: value for key, value in updates.items() if value is not None}
    if not updates:
        parser.error("provide at least one URL")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    external = manifest.setdefault("external_deliverables", {})
    external.update(updates)
    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(external, ensure_ascii=False, indent=2))
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_submission_packet.py")],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "audit_submission.py")],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    main()
