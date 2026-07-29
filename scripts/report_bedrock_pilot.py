#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create aggregate-only evidence for the Bedrock pilot"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--accepted", type=Path, required=True)
    parser.add_argument("--quarantine", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports" / "bedrock-pilot.json",
    )
    args = parser.parse_args()
    inputs = read_jsonl(args.input)
    accepted = read_jsonl(args.accepted)
    quarantine = read_jsonl(args.quarantine)
    mentions = [
        mention
        for row in accepted
        for mention in row.get("mentions", [])
    ]
    relations = [
        relation
        for row in accepted
        for relation in row.get("relations", [])
    ]
    rejection_count = sum(
        len(row.get("rejections", []))
        for row in [*accepted, *quarantine]
    )
    fatal_count = sum("fatal" in row for row in quarantine)
    types = collections.Counter(
        mention.get("type", "unknown") for mention in mentions
    )
    levels = collections.Counter(
        mention.get("level", "unknown") for mention in mentions
    )
    skills = collections.Counter(
        mention.get("canonical_skill", "") for mention in mentions
    )
    model_ids = sorted(
        {
            row["model_id"]
            for row in [*accepted, *quarantine]
            if row.get("model_id")
        }
    )
    input_tokens = sum(
        int(row.get("usage", {}).get("inputTokens", 0))
        for row in [*accepted, *quarantine]
    )
    output_tokens = sum(
        int(row.get("usage", {}).get("outputTokens", 0))
        for row in [*accepted, *quarantine]
    )
    report = {
        "metadata": {
            "schema": "skillweave-bedrock-pilot-v1",
            "analysis_status": "bounded_real_bedrock_train_only_pilot",
            "graph_cutoff": "2026-06-05 23:59:59.999",
            "prompt_version": "jd-skill-v3",
            "model_ids": model_ids,
            "input_sha256": sha256(args.input),
            "accepted_sha256": sha256(args.accepted),
            "quarantine_sha256": sha256(args.quarantine),
            "privacy": "aggregate-only report; no job or user identifiers",
        },
        "records": {
            "input": len(inputs),
            "accepted": len(accepted),
            "quarantined": len(quarantine),
            "fatal": fatal_count,
            "record_acceptance_rate": (
                len(accepted) / len(inputs) if inputs else 0.0
            ),
        },
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_usd": (
                input_tokens / 1_000_000 * 1.0
                + output_tokens / 1_000_000 * 5.0
                if model_ids
                == ["us.anthropic.claude-haiku-4-5-20251001-v1:0"]
                else None
            ),
            "pricing_assumption": (
                "Claude Haiku 4.5 standard: $1/M input, $5/M output; "
                "estimate excludes taxes and free credits"
            ),
        },
        "validated_graph": {
            "mentions": len(mentions),
            "relations_pending_corpus_corroboration": len(relations),
            "validator_rejections": rejection_count,
            "mention_types": dict(types.most_common()),
            "requirement_levels": dict(levels.most_common()),
            "top_canonical_skills": [
                {"skill": skill, "count": count}
                for skill, count in skills.most_common(15)
                if skill
            ],
        },
        "release_claim": (
            "Real Amazon Bedrock structured extraction pilot; not a full "
            "production corpus graph and not used to tune locked confirmations."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
