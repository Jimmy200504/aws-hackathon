#!/usr/bin/env python3
"""Build a compact local SQLite index of RELATED_TO skill graph edges.

The full deterministic Skill Graph build (``scripts/run_full_graph_build.py``)
produces ``edges/part-*.jsonl`` files that mix three edge types: ``REQUIRES``
and ``INSTANCE_OF`` (Job-to-Skill/Occupation, only useful for Neptune bulk
import) and ``RELATED_TO`` (Skill-to-Skill/Occupation, the only edge type the
online ranker reads for graph features; see ``app/ranker.py::_graph_feature``
and ``app/graph_provider.py::GraphFeatureProvider``).

This script extracts only the ``RELATED_TO`` edges and writes them into a
single SQLite file that a laptop can query with millisecond latency without
loading the full multi-gigabyte export into memory. The output is meant to be
distributed as a GitHub Release asset (see
``scripts/download_local_graph_index.py``) so a user who never deploys AWS
can still get the production-scale skill graph locally, instead of the
63-node bootstrap fixture embedded in ``artifacts/demo-index.json``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "local-graph-index-v1"


def iter_jsonl_parts(path: Path) -> Iterator[dict[str, Any]]:
    paths = sorted(path.glob("part-*.jsonl")) if path.is_dir() else [path]
    if not paths:
        raise ValueError(f"no jsonl parts found under {path}")
    for candidate in paths:
        with candidate.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_index(
    edges_path: Path,
    output_path: Path,
    *,
    graph_version: str,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Read RELATED_TO edges from jsonl parts and write a SQLite index.

    Only ``RELATED_TO`` rows are kept. Job edges (``REQUIRES``,
    ``INSTANCE_OF``) are skipped: they exist purely to build the Neptune bulk
    import CSV and are not consulted by the online ranker's graph feature.
    """
    if output_path.exists():
        output_path.unlink()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(output_path)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute(
        """
        CREATE TABLE relations (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_id TEXT NOT NULL,
            weight REAL NOT NULL,
            confidence REAL NOT NULL,
            support_jobs INTEGER NOT NULL,
            support_companies INTEGER NOT NULL,
            evidence TEXT NOT NULL,
            rules_version TEXT NOT NULL,
            corpus_hash TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id)
        )
        """
    )
    connection.execute(
        "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )

    edge_count = 0
    skipped_non_related = 0
    started = time.time()
    for row in iter_jsonl_parts(edges_path):
        if row.get("type") != "RELATED_TO":
            skipped_non_related += 1
            continue
        connection.execute(
            """
            INSERT INTO relations (
                source_id, target_id, edge_id, weight, confidence,
                support_jobs, support_companies, evidence, rules_version, corpus_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_id, target_id) DO NOTHING
            """,
            (
                str(row["source_id"]),
                str(row["target_id"]),
                str(row.get("id", "")),
                float(row.get("weight", 0.0)),
                float(row.get("confidence", 0.0)),
                int(row.get("support_jobs", 0)),
                int(row.get("support_companies", 0)),
                json.dumps(row.get("evidence", []), ensure_ascii=False),
                str(row.get("rules_version", "")),
                str(row.get("corpus_hash", "")),
            ),
        )
        edge_count += 1
        if edge_count % 50000 == 0:
            connection.commit()
    connection.execute(
        "CREATE INDEX idx_relations_source ON relations(source_id)"
    )
    connection.commit()

    built_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    metadata = {
        "schema": SCHEMA_VERSION,
        "graph_version": graph_version,
        "edge_count": edge_count,
        "built_at": built_at,
        "build_seconds": round(time.time() - started, 2),
    }
    connection.executemany(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        [(key, json.dumps(value)) for key, value in metadata.items()],
    )
    connection.commit()
    connection.close()

    sha256 = _sha256_file(output_path)
    manifest = {
        **metadata,
        "sha256": sha256,
        "bytes": output_path.stat().st_size,
        "skipped_non_related_edges": skipped_non_related,
        "source": str(edges_path),
    }
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a compact local SQLite index of RELATED_TO skill graph "
            "edges from a full deterministic Skill Graph build's edges/ "
            "jsonl parts."
        )
    )
    parser.add_argument(
        "--edges",
        type=Path,
        required=True,
        help="path to the edges/ directory (or a single jsonl file) from run_full_graph_build.py",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/skill-graph-local-index.sqlite3"),
        help="output SQLite file path",
    )
    parser.add_argument(
        "--graph-version",
        required=True,
        help="graph version label to record in metadata, e.g. deterministic-v1-rules-v2-latest",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="optional path to write a JSON manifest (sha256, edge count, size)",
    )
    args = parser.parse_args()

    manifest_path = args.manifest or args.output.with_suffix(args.output.suffix + ".manifest.json")
    manifest = build_index(
        args.edges,
        args.output,
        graph_version=args.graph_version,
        manifest_path=manifest_path,
    )
    print(f"Wrote {manifest['edge_count']:,} RELATED_TO edges to {args.output}")
    print(f"  size: {manifest['bytes']:,} bytes")
    print(f"  sha256: {manifest['sha256']}")
    print(f"  manifest: {manifest_path}")


if __name__ == "__main__":
    main()
