#!/usr/bin/env python3
"""Propose reviewed-ontology additions from statistical surface candidates.

The deterministic graph build emits ``review/surface-candidates`` — normalized
surface forms seen in real job postings that did NOT resolve to a reviewed
ontology node. They are deliberately not serving-eligible: a human must review
them before they become graph nodes — the production graph build uses no LLM
or embedding, and only reviewed aliases resolve.

This script ranks those candidates by corpus support and filters out ones that
would collide with existing reviewed aliases, so a reviewer can work from a
short, high-signal list instead of 2,659 raw rows. It only proposes; it never
writes to config/skill_ontology.seed.json.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SEED = ROOT / "config" / "skill_ontology.seed.json"
_SPACE = re.compile(r"\s+")


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower().replace("臺", "台")
    return _SPACE.sub(" ", text).strip()


def existing_aliases(seed_path: Path) -> set[str]:
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    taken: set[str] = set()
    for spec in seed.get("skills", {}).values():
        for value in (spec.get("label", ""), *spec.get("aliases", ())):
            if normalized := normalize(str(value)):
                taken.add(normalized)
    return taken


def load_candidates(candidates_dir: Path) -> list[dict]:
    rows: list[dict] = []
    parts = sorted(glob.glob(str(candidates_dir / "part-*.jsonl")))
    if not parts:
        raise SystemExit(f"no surface-candidate parts found under {candidates_dir}")
    for part in parts:
        with open(part, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def is_noise(surface: str) -> bool:
    """Drop surfaces that cannot be a meaningful skill node."""
    if len(surface) < 2:
        return True
    # Pure digits / version fragments carry no standalone skill meaning.
    if re.fullmatch(r"[\d\s.\-_/]+", surface):
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Rank statistical surface candidates for human ontology review. "
            "Proposes only; does not modify the reviewed seed ontology."
        )
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        required=True,
        help="path to a review/surface-candidates directory from a graph build",
    )
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument(
        "--min-jobs",
        type=int,
        default=2000,
        help="minimum supporting job count (default: 2000)",
    )
    parser.add_argument(
        "--min-companies",
        type=int,
        default=500,
        help="minimum distinct supporting companies (default: 500)",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="optional JSON path to write the proposal list",
    )
    args = parser.parse_args()

    taken = existing_aliases(args.seed)
    rows = load_candidates(args.candidates)

    proposals = []
    for row in rows:
        surface = normalize(str(row.get("normalized_surface", "")))
        if not surface or is_noise(surface) or surface in taken:
            continue
        jobs = int(row.get("support_jobs", 0))
        companies = int(row.get("support_companies", 0))
        if jobs < args.min_jobs or companies < args.min_companies:
            continue
        proposals.append(
            {
                "surface": surface,
                "support_jobs": jobs,
                "support_companies": companies,
                "evidence_fields": row.get("evidence_fields", []),
                "candidate_id": row.get("candidate_id", ""),
            }
        )

    proposals.sort(key=lambda item: (-item["support_jobs"], item["surface"]))
    proposals = proposals[: args.limit]

    print(
        f"{len(rows):,} raw candidates -> {len(proposals):,} proposals "
        f"(min_jobs={args.min_jobs:,}, min_companies={args.min_companies:,}, "
        f"excluding {len(taken):,} existing aliases)"
    )
    print()
    print(f"{'jobs':>9} {'companies':>10}  {'evidence':<22} surface")
    for item in proposals:
        fields = ",".join(item["evidence_fields"])[:20]
        print(
            f"{item['support_jobs']:>9,} {item['support_companies']:>10,}  "
            f"{fields:<22} {item['surface']}"
        )

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "schema": "ontology-expansion-proposals-v1",
                    "thresholds": {
                        "min_jobs": args.min_jobs,
                        "min_companies": args.min_companies,
                    },
                    "raw_candidates": len(rows),
                    "proposals": proposals,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
