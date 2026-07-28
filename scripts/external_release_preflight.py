#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release-manifest.json"


def command(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            args=args,
            returncode=127,
            stdout="",
            stderr=str(exc),
        )


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tracked_sensitive_paths(paths: list[str]) -> list[str]:
    raw_suffixes = (
        ".csv",
        ".tsv",
        ".parquet",
        ".jsonl",
        ".ndjson",
        ".zip",
        ".gz",
    )
    return sorted(
        path
        for path in paths
        if (
            path.startswith("data/dataset/")
            and path.lower().endswith(raw_suffixes)
        )
        or (path.startswith("docs/") and path.lower().endswith(".pdf"))
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check whether SkillWeave can perform external release actions"
    )
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="exit non-zero unless credentials and local release prerequisites pass",
    )
    args = parser.parse_args()

    manifest: dict[str, Any] = json.loads(
        MANIFEST.read_text(encoding="utf-8")
    )
    release = str(manifest.get("release", ""))
    expected_video_hash = str(
        manifest.get("sha256", {}).get(
            "dist/skillweave-demo-5min.mp4", ""
        )
    )
    video = ROOT / "dist" / "skillweave-demo-5min.mp4"

    tools = {
        name: shutil.which(name) is not None
        for name in ("aws", "sam", "gh", "git", "curl", "python3")
    }
    aws_identity = command(
        ["aws", "sts", "get-caller-identity", "--output", "json"]
    )
    github_auth = command(["gh", "auth", "status"])
    worktree = command(["git", "status", "--porcelain"])
    head = command(["git", "rev-parse", "HEAD"])
    tag = command(["git", "rev-list", "-n", "1", release])
    origin = command(["git", "remote", "get-url", "origin"])
    tracked = command(["git", "ls-files"])
    sensitive = tracked_sensitive_paths(tracked.stdout.splitlines())
    external = manifest.get("external_deliverables", {})
    aws_action_needed = not external.get("aws_url")
    github_action_needed = (
        not external.get("github_url")
        or not external.get("demo_video_url")
    )

    checks = {
        "required_tools": all(tools.values()),
        "aws_authenticated": aws_identity.returncode == 0,
        "github_authenticated": github_auth.returncode == 0,
        "worktree_clean": worktree.returncode == 0
        and not worktree.stdout.strip(),
        "release_tag_at_head": (
            head.returncode == 0
            and tag.returncode == 0
            and head.stdout.strip() == tag.stdout.strip()
        ),
        "video_present_and_hashed": (
            video.is_file()
            and len(expected_video_hash) == 64
            and sha256_path(video) == expected_video_hash
        ),
        "no_sensitive_data_tracked": tracked.returncode == 0
        and not sensitive,
    }
    actions: list[str] = []
    if aws_action_needed and not checks["aws_authenticated"]:
        actions.append("Authenticate AWS: aws login (or aws configure sso).")
    if github_action_needed and not checks["github_authenticated"]:
        actions.append("Authenticate GitHub: gh auth login.")
    if github_action_needed and origin.returncode != 0:
        actions.append(
            "Choose OWNER/REPO; after review, create the public GitHub repository."
        )
    if aws_action_needed:
        actions.append("Deploy: ./scripts/deploy_compact_aws.sh")
    if github_action_needed:
        actions.append(
            "Publish the RC tag and MP4 as a public GitHub release, then register both URLs."
        )
    if not checks["worktree_clean"]:
        actions.append("Commit or otherwise resolve the local worktree changes.")
    if not checks["release_tag_at_head"]:
        actions.append(f"Ensure annotated tag {release} points at HEAD.")

    aws_account = None
    if aws_identity.returncode == 0:
        try:
            aws_account = json.loads(aws_identity.stdout).get("Account")
        except json.JSONDecodeError:
            checks["aws_authenticated"] = False

    report = {
        "schema": "skillweave-external-release-preflight-v1",
        "release": release,
        "ready_for_external_actions": (
            all(
                checks[name]
                for name in (
                    "required_tools",
                    "worktree_clean",
                    "release_tag_at_head",
                    "video_present_and_hashed",
                    "no_sensitive_data_tracked",
                )
            )
            and (checks["aws_authenticated"] or not aws_action_needed)
            and (
                checks["github_authenticated"]
                or not github_action_needed
            )
        ),
        "external_urls_complete": all(
            external.get(name)
            for name in ("aws_url", "github_url", "demo_video_url")
        ),
        "checks": checks,
        "tools": tools,
        "observed": {
            "aws_account": aws_account,
            "git_origin": (
                origin.stdout.strip() if origin.returncode == 0 else None
            ),
            "sensitive_tracked_paths": sensitive,
        },
        "next_actions": actions,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.require_ready and not report[
        "ready_for_external_actions"
    ] else 0


if __name__ == "__main__":
    raise SystemExit(main())
