#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import normalize


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish validated Bedrock mentions to the compact demo graph"
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=ROOT / "artifacts" / "demo-index.json",
    )
    parser.add_argument("--accepted", type=Path, required=True)
    args = parser.parse_args()
    index = json.loads(args.index.read_text(encoding="utf-8"))
    jobs = {job["id"]: job for job in index["jobs"]}
    alias_map: dict[str, str] = {}
    for skill_id, skill in index["skills"].items():
        for alias in [
            skill_id,
            skill.get("label", ""),
            *skill.get("aliases", []),
        ]:
            if normalize(alias):
                alias_map.setdefault(normalize(alias), skill_id)
    records = [
        json.loads(line)
        for line in args.accepted.read_text(
            encoding="utf-8"
        ).splitlines()
        if line
    ]
    published_edges = new_nodes = existing_nodes = 0
    for record in records:
        job = jobs.get(record["job_id"])
        if job is None:
            continue
        per_job = 0
        for mention in record.get("mentions", []):
            canonical = str(mention["canonical_skill"]).strip()
            surface = str(mention["surface"]).strip()
            skill_id = alias_map.get(normalize(canonical))
            if skill_id is None:
                skill_id = alias_map.get(normalize(surface))
            if skill_id is None:
                digest = hashlib.sha256(
                    normalize(canonical).encode()
                ).hexdigest()[:16]
                skill_id = f"bedrock.{digest}"
                index["skills"][skill_id] = {
                    "type": "Skill",
                    "label": canonical,
                    "aliases": sorted(
                        {value for value in [canonical, surface] if value}
                    ),
                    "related": {},
                    "provenance": "amazon_bedrock_structured_extraction",
                    "extractor_model": record["model_id"],
                    "prompt_version": record["prompt_version"],
                }
                for alias in [canonical, surface]:
                    if normalize(alias):
                        alias_map.setdefault(normalize(alias), skill_id)
                new_nodes += 1
            else:
                existing_nodes += 1
            if skill_id not in job.setdefault("skills", []):
                job["skills"].append(skill_id)
            job.setdefault("skill_confidence", {})[skill_id] = max(
                float(
                    job.get("skill_confidence", {}).get(skill_id, 0.0)
                ),
                float(mention["confidence"]),
            )
            job.setdefault("skill_evidence", {})[skill_id] = mention[
                "evidence"
            ]
            job.setdefault("skill_provenance", {})[skill_id] = {
                "source": "amazon_bedrock_structured_extraction",
                "model_id": record["model_id"],
                "prompt_version": record["prompt_version"],
                "evidence_field": mention["evidence_field"],
                "validated": True,
            }
            published_edges += 1
            per_job += 1
        job["bedrock_validated_skill_count"] = per_job
    index["metadata"]["bedrock_pilot"] = {
        "status": "bounded_real_train_only_pilot",
        "records_accepted": len(records),
        "published_mention_edges": published_edges,
        "new_skill_nodes": new_nodes,
        "existing_skill_node_matches": existing_nodes,
        "relations_published": 0,
        "relation_policy": "requires separate corpus corroboration",
        "model_id": (
            records[0]["model_id"] if records else None
        ),
        "prompt_version": (
            records[0]["prompt_version"] if records else None
        ),
    }
    args.index.write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        json.dumps(index["metadata"]["bedrock_pilot"], indent=2)
    )


if __name__ == "__main__":
    main()
