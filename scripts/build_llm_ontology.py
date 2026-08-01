#!/usr/bin/env python3
"""Merge Bedrock-extracted skill nodes into an ontology the benchmark can read.

The release benchmark fixture is built from the reviewed bootstrap seed only, so
Amazon Bedrock output has never entered the offline measurement. This script
produces a merged ontology that keeps the two provenances in separate node-ID
namespaces, which lets `pipeline/evaluate_ltr.py --baseline-scope llm_off`
isolate the generative-AI contribution.
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import LLM_SKILL_PREFIX, normalize

DEFAULT_DEMO = ROOT / "artifacts" / "demo-index.json"
DEFAULT_SEED = ROOT / "config" / "skill_ontology.seed.json"
DEFAULT_OUTPUT = ROOT / "artifacts" / "skill_ontology.seed_plus_llm.json"
MIN_ALIAS_CHARS = 2
MAX_ALIAS_CHARS = 60


def seed_alias_index(seed_skills: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    for skill_id, spec in seed_skills.items():
        for value in [spec.get("label", ""), *spec.get("aliases", [])]:
            normalized = normalize(str(value))
            if normalized:
                aliases.add(normalized)
        aliases.add(normalize(skill_id))
    return aliases


def usable_aliases(spec: dict[str, Any], taken: set[str]) -> list[str]:
    """Aliases that are safe to publish for one LLM node.

    An alias already owned by the reviewed seed is dropped: keeping it would
    make one surface form resolve to two nodes and double-count the same
    evidence in both feature families.
    """
    result: list[str] = []
    for value in [spec.get("label", ""), *spec.get("aliases", [])]:
        text = unicodedata.normalize("NFKC", str(value)).strip()
        normalized = normalize(text)
        if not normalized or normalized in taken:
            continue
        if not (MIN_ALIAS_CHARS <= len(normalized) <= MAX_ALIAS_CHARS):
            continue
        result.append(text)
        taken.add(normalized)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-index", type=Path, default=DEFAULT_DEMO)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    seed = json.loads(args.seed.read_text(encoding="utf-8"))
    demo = json.loads(args.demo_index.read_text(encoding="utf-8"))
    published = demo["skills"]

    taken = seed_alias_index(seed["skills"])
    merged: dict[str, Any] = dict(seed["skills"])
    rejected: Counter[str] = Counter()
    accepted = 0
    for skill_id, spec in published.items():
        if not skill_id.startswith(LLM_SKILL_PREFIX):
            continue
        if spec.get("provenance") != "amazon_bedrock_structured_extraction":
            rejected["missing_bedrock_provenance"] += 1
            continue
        aliases = usable_aliases(spec, taken)
        if not aliases:
            rejected["all_aliases_owned_by_seed_or_out_of_range"] += 1
            continue
        merged[skill_id] = {
            "type": spec.get("type", "Skill"),
            "label": spec.get("label", skill_id),
            "aliases": aliases,
            # Bedrock relation proposals stay quarantined pending corpus
            # corroboration, so LLM nodes contribute direct evidence only.
            "related": {},
            "provenance": spec["provenance"],
            "extractor_model": spec.get("extractor_model"),
            "prompt_version": spec.get("prompt_version"),
        }
        accepted += 1

    ontology = {
        "schema_version": seed.get("schema_version", "skillgraph-v1"),
        "provenance": {
            "kind": "reviewed-bootstrap-plus-bedrock-pilot",
            "purpose": (
                "Offline ablation that isolates the Amazon Bedrock contribution "
                "from the reviewed seed ontology"
            ),
            "seed_nodes": len(seed["skills"]),
            "llm_nodes": accepted,
            "llm_nodes_rejected": dict(rejected),
            "llm_node_prefix": LLM_SKILL_PREFIX,
            "llm_relations_published": 0,
            "warning": (
                "LLM nodes originate from a bounded 200-record train-only "
                "pilot. Coverage is far below the full corpus; treat any "
                "measured lift as a lower bound, not a production estimate."
            ),
        },
        "skills": merged,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(ontology, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "seed_nodes": len(seed["skills"]),
                "llm_nodes_accepted": accepted,
                "llm_nodes_rejected": dict(rejected),
                "total_nodes": len(merged),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
