#!/usr/bin/env python3
"""Download and verify the local Skill Graph SQLite index Release asset.

This is the counterpart to ``scripts/build_local_graph_index.py``. It lets a
user who does not deploy AWS (no Neptune Analytics) fetch a prebuilt,
production-scale RELATED_TO skill graph index and use it locally instead of
the 63-node bootstrap fixture embedded in ``artifacts/demo-index.json``. See
``app/graph_provider.py::LocalGraphProvider`` for how the index is queried,
and the README for the three-tier fallback order (Neptune -> local index ->
embedded fixture).

Zero third-party dependencies: uses ``urllib.request`` only, consistent with
the local-demo zero-dependency requirement in .kiro/steering/tech.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

DEFAULT_OUTPUT = Path("artifacts/skill-graph-local-index.sqlite3")
DEFAULT_MANIFEST_SUFFIX = ".manifest.json"
CHUNK_SIZE = 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, destination: Path, *, timeout: float) -> None:
    request = Request(url, headers={"User-Agent": "skillweave-local-graph-index/1"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    with urlopen(request, timeout=timeout) as response, temporary.open("wb") as handle:
        total = 0
        while True:
            block = response.read(CHUNK_SIZE)
            if not block:
                break
            handle.write(block)
            total += len(block)
    temporary.replace(destination)


def _load_manifest(url: str, *, timeout: float) -> dict:
    request = Request(url, headers={"User-Agent": "skillweave-local-graph-index/1"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def download_index(
    *,
    index_url: str,
    manifest_url: str | None,
    output: Path,
    manifest_output: Path | None,
    expected_sha256: str | None,
    timeout: float,
    force: bool,
) -> dict:
    manifest_output = manifest_output or output.with_suffix(output.suffix + DEFAULT_MANIFEST_SUFFIX)
    manifest: dict | None = None

    if manifest_url:
        print(f"Fetching manifest: {manifest_url}")
        manifest = _load_manifest(manifest_url, timeout=timeout)
        manifest_output.parent.mkdir(parents=True, exist_ok=True)
        manifest_output.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        expected_sha256 = expected_sha256 or manifest.get("sha256")

    if output.is_file() and not force:
        if expected_sha256 and _sha256_file(output) == expected_sha256:
            print(f"Already present and verified: {output}")
            return manifest or {"sha256": expected_sha256}
        if not expected_sha256:
            raise SystemExit(
                f"{output} already exists and no expected sha256 was provided "
                "(pass --sha256, --manifest-url, or --force to overwrite)"
            )

    print(f"Downloading index: {index_url}")
    _download(index_url, output, timeout=timeout)

    actual_sha256 = _sha256_file(output)
    if expected_sha256:
        if actual_sha256 != expected_sha256:
            output.unlink(missing_ok=True)
            raise SystemExit(
                f"SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}. "
                "The downloaded file was removed."
            )
        print(f"Verified sha256: {actual_sha256}")
    else:
        print(
            f"Warning: no expected sha256 to verify against (got {actual_sha256}). "
            "Pass --sha256 or --manifest-url to enable integrity verification."
        )

    return manifest or {"sha256": actual_sha256}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Download the prebuilt local Skill Graph SQLite index from a "
            "GitHub Release asset (or any HTTPS URL) and verify its integrity."
        )
    )
    parser.add_argument(
        "--index-url",
        required=True,
        help="HTTPS URL to the sqlite3 index Release asset",
    )
    parser.add_argument(
        "--manifest-url",
        default=None,
        help="optional HTTPS URL to the accompanying manifest.json (provides sha256 automatically)",
    )
    parser.add_argument("--sha256", default=None, help="expected SHA-256 of the index file")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--manifest-output",
        type=Path,
        default=None,
        help="where to save the manifest (defaults to <output>.manifest.json)",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--force", action="store_true", help="re-download even if a verified copy already exists"
    )
    args = parser.parse_args()

    try:
        manifest = download_index(
            index_url=args.index_url,
            manifest_url=args.manifest_url,
            output=args.output,
            manifest_output=args.manifest_output,
            expected_sha256=args.sha256,
            timeout=args.timeout,
            force=args.force,
        )
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any network/IO failure clearly
        raise SystemExit(f"Download failed: {type(exc).__name__}: {exc}") from exc

    print(f"Local graph index ready at {args.output}")
    if manifest.get("edge_count"):
        print(f"  edges: {manifest['edge_count']:,}")
    if manifest.get("graph_version"):
        print(f"  graph_version: {manifest['graph_version']}")
    print(f"Set LOCAL_GRAPH_INDEX_PATH={args.output} to use it (see README).")


if __name__ == "__main__":
    main()
