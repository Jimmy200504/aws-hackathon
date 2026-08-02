"""Project the district extraction onto the demo index, as a small side-car.

`app/geo_graph.py` can say that 龜山區 substitutes for 林口區, but nothing could
act on it: a job record carries `city` (a county) and no district, so there was
no join key from an expanded district back to a posting.

`artifacts/job-districts.json` has the join, for 267,306 postings of the full
corpus. It is 43 MB and gitignored, and the demo needs 574 of its rows. So this
writes just those, keyed by the demo index's own job ids.

Why a side-car rather than a district field on the demo index: the index is
hash-pinned in `release-manifest.json`, so adding a field to it invalidates a
published release hash and forces every downstream confirmation to be re-run.
The side-car is additive and carries its own provenance.

Usage:
    python scripts/build_demo_job_districts.py
    python scripts/build_demo_job_districts.py --check    # verify, write nothing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEMO_INDEX = ROOT / "artifacts" / "demo-index.json"
DEFAULT_SOURCE = ROOT / "artifacts" / "job-districts.json"
DEFAULT_OUTPUT = ROOT / "artifacts" / "demo-job-districts.json"

SCHEMA = "skillweave-demo-job-districts-v1"


def build(demo_index_path: Path, source_path: Path) -> dict[str, Any]:
    demo = json.loads(demo_index_path.read_text(encoding="utf-8"))
    demo_ids = {str(job["id"]) for job in demo["jobs"]}

    payload = json.loads(source_path.read_text(encoding="utf-8"))
    source_meta = payload.get("metadata", {})
    source_stats = payload.get("stats", {})

    # A posting naming two districts is kept as both. 13.2% of annotated
    # postings do this, usually because the JD lists the sites it is hiring for,
    # and a searcher who names either one wants that posting.
    districts: dict[str, list[str]] = {}
    layers: dict[str, int] = {}
    for row in payload["jobs"]:
        job_id = str(row["job_id"])
        if job_id not in demo_ids:
            continue
        county = row["county"]
        nodes = districts.setdefault(job_id, [])
        for entry in row.get("districts", []):
            node = f"{county}/{entry['district']}"
            if node not in nodes:
                nodes.append(node)
            layers[entry["layer"]] = layers.get(entry["layer"], 0) + 1
    # Sorted so the artifact is byte-identical for a given pair of inputs.
    districts = {
        job_id: sorted(nodes) for job_id, nodes in sorted(districts.items()) if nodes
    }

    return {
        "schema": SCHEMA,
        "provenance": "projection of artifacts/job-districts.json onto the demo index",
        "source": str(source_path.relative_to(ROOT)).replace("\\", "/"),
        "source_schema": source_meta.get("schema"),
        "contains": (
            "job id -> district nodes, for demo index postings only; no applicant, "
            "search or employer information"
        ),
        # Recorded on the artifact rather than inferred later, so a stale
        # side-car cannot be paired with a rebuilt index without it showing.
        "dataset_version": source_meta.get("dataset_version")
        or demo["metadata"].get("dataset_version"),
        "graph_cutoff": source_meta.get("cutoff") or source_meta.get("graph_cutoff"),
        "index_version": demo["metadata"].get("index_version"),
        "schema_fingerprint": demo["metadata"].get("schema_fingerprint"),
        "random_seed": demo["metadata"].get("random_seed"),
        "extractor": source_meta.get("extractor"),
        "counts": {
            "demo_jobs": len(demo_ids),
            "annotated_jobs": len(districts),
            "coverage": round(len(districts) / max(1, len(demo_ids)), 4),
            "district_nodes": len({node for nodes in districts.values() for node in nodes}),
            "multi_district_jobs": sum(1 for nodes in districts.values() if len(nodes) > 1),
            "by_layer": dict(sorted(layers.items())),
            # The full-corpus figure, so the demo's thin coverage is not mistaken
            # for the extractor's.
            "source_postings_with_district": source_stats.get("postings_with_district"),
            "source_coverage_of_eligible": source_stats.get("coverage_of_eligible"),
        },
        "jobs": districts,
    }


def serialise(artifact: dict[str, Any]) -> str:
    return json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-index", type=Path, default=DEFAULT_DEMO_INDEX)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="rebuild in memory and compare with the checked-in file",
    )
    args = parser.parse_args(argv)

    if not args.source.is_file():
        print(
            f"{args.source} is absent. It is gitignored and rebuilt in ~75s by "
            "scripts/extract_job_districts.py.",
            file=sys.stderr,
        )
        return 2

    artifact = build(args.demo_index, args.source)
    body = serialise(artifact)
    if args.check:
        if not args.output.is_file():
            print(f"{args.output} is missing", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != body:
            print(
                "side-car is stale: "
                f"checked-in sha256={hashlib.sha256(current.encode()).hexdigest()[:12]} "
                f"rebuilt sha256={hashlib.sha256(body.encode()).hexdigest()[:12]}",
                file=sys.stderr,
            )
            return 1
        print("side-car matches its inputs")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(body, encoding="utf-8")
    print(json.dumps(artifact["counts"], ensure_ascii=False, indent=2))
    print(f"Wrote {args.output} ({args.output.stat().st_size / 1024:.1f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
