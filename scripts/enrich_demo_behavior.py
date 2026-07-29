#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Attach train-only behavior priors to the compact demo index"
    )
    parser.add_argument(
        "--demo-index",
        type=Path,
        default=ROOT / "artifacts" / "demo-index.json",
    )
    parser.add_argument(
        "--benchmark-index",
        type=Path,
        required=True,
        help="Quality benchmark index whose behavior graph is train-only",
    )
    args = parser.parse_args()
    demo = json.loads(args.demo_index.read_text(encoding="utf-8"))
    benchmark = json.loads(
        args.benchmark_index.read_text(encoding="utf-8")
    )
    source = benchmark["behavior_graph"]
    job_ids = {job["id"] for job in demo["jobs"]}
    company_ids = {
        str(job.get("company_id", "")) for job in demo["jobs"]
    }
    job_skills = {
        skill_id
        for job in demo["jobs"]
        for skill_id in job.get("skills", [])
    }
    query_job = {
        query: filtered
        for query, edges in source["query_job"].items()
        if (
            filtered := {
                job_id: stats
                for job_id, stats in edges.items()
                if job_id in job_ids
            }
        )
    }
    query_skill = {
        query: filtered
        for query, edges in source["query_skill"].items()
        if (
            filtered := {
                skill_id: stats
                for skill_id, stats in edges.items()
                if skill_id in job_skills
            }
        )
    }
    demo["behavior_graph"] = {
        "query_job": query_job,
        "query_skill": query_skill,
        "job_global": {
            job_id: stats
            for job_id, stats in source["job_global"].items()
            if job_id in job_ids
        },
        "company_global": {
            company_id: stats
            for company_id, stats in source["company_global"].items()
            if company_id in company_ids
        },
        "global_totals": source["global_totals"],
        "provenance": {
            **source["provenance"],
            "compact_filter": "demo jobs, companies, and their skill nodes only",
        },
    }
    demo["metadata"]["ranking_model"] = "ltr-quality-final"
    demo["metadata"]["behavior_graph_source"] = "train_qrels_only"
    args.demo_index.write_text(
        json.dumps(demo, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        f"Wrote {args.demo_index}: {len(query_job):,} query-job nodes, "
        f"{len(query_skill):,} query-skill nodes"
    )


if __name__ == "__main__":
    main()
