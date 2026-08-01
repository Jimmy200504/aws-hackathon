#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.ranking_graph_overlay import build_ranking_graph_overlay


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bind a frozen ranking fixture to the deterministic v2 cutoff graph"
    )
    parser.add_argument("--base-index", type=Path, required=True)
    parser.add_argument("--qrels", type=Path, required=True)
    parser.add_argument("--graph-manifest", type=Path, required=True)
    parser.add_argument("--nodes", type=Path, required=True)
    parser.add_argument("--resolved-jobs", type=Path, required=True)
    parser.add_argument("--job-edges", type=Path, required=True)
    parser.add_argument("--relation-edges", type=Path, required=True)
    parser.add_argument("--reviewed-ontology", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metadata = build_ranking_graph_overlay(
        base_index_path=args.base_index,
        qrels_path=args.qrels,
        graph_manifest_path=args.graph_manifest,
        nodes_path=args.nodes,
        resolved_jobs_path=args.resolved_jobs,
        job_edges_path=args.job_edges,
        relation_edges_path=args.relation_edges,
        reviewed_ontology_path=args.reviewed_ontology,
        output_path=args.output,
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
