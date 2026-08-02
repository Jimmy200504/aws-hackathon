#!/usr/bin/env python3
"""Score a completed deterministic Skill Graph human-review packet."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any


THRESHOLDS = {
    "mention_precision": 0.95,
    "mention_recall": 0.85,
    "exact_alias_precision": 0.995,
    "published_relation_precision": 0.90,
}
NON_HUMAN_REVIEWERS = {"ai", "auto", "automated", "chatgpt", "codex", "llm"}


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _review_integer(row: dict[str, str], field: str, row_number: int, maximum: int | None = None) -> int:
    value = str(row.get(field, "")).strip()
    if not value:
        raise ValueError(f"row {row_number}: {field} is required")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"row {row_number}: {field} must be an integer") from exc
    if parsed < 0 or (maximum is not None and parsed > maximum):
        raise ValueError(f"row {row_number}: {field} is outside the allowed range")
    return parsed


def _human_reviewer(row: dict[str, str], row_number: int, *, prefix: str = "row") -> str:
    reviewer = str(row.get("reviewer", "")).strip()
    if not reviewer:
        raise ValueError(f"{prefix} {row_number}: reviewer is required")
    if reviewer.casefold() in NON_HUMAN_REVIEWERS:
        raise ValueError(f"{prefix} {row_number}: reviewer must identify a human reviewer")
    return reviewer


def _published_count(row: dict[str, str], row_number: int) -> int:
    try:
        mentions = json.loads(str(row.get("published_mentions_json", "")))
    except json.JSONDecodeError as exc:
        raise ValueError(f"row {row_number}: published_mentions_json is invalid") from exc
    if not isinstance(mentions, list):
        raise ValueError(f"row {row_number}: published_mentions_json must be a list")
    derived = len(mentions)
    raw = str(row.get("published_mention_count", "")).strip()
    if raw:
        declared = _review_integer(row, "published_mention_count", row_number)
        if declared != derived:
            raise ValueError(f"row {row_number}: published mention count does not match locked JSON")
    return derived


def score_review_packet(packet_dir: Path) -> dict[str, Any]:
    manifest_path = packet_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "awaiting_human_review":
        raise ValueError("review packet manifest has an unexpected status")
    jobs = _read_csv(packet_dir / "job-review.csv")
    relations = _read_csv(packet_dir / "relation-review.csv")
    if len(jobs) != int(manifest.get("job_review_rows", -1)):
        raise ValueError("job review row count does not match packet manifest")
    if len(relations) != int(manifest.get("relation_review_rows", -1)):
        raise ValueError("relation review row count does not match packet manifest")

    published = valid = missed = incorrect_alias = 0
    reviewers: set[str] = set()
    for index, row in enumerate(jobs, 2):
        reviewer = _human_reviewer(row, index)
        reviewers.add(reviewer)
        row_published = _published_count(row, index)
        row_valid = _review_integer(row, "valid_published_mentions", index, row_published)
        row_missed = _review_integer(row, "missed_reviewed_mentions", index)
        row_incorrect = _review_integer(row, "incorrect_alias_matches", index, row_published)
        if row_incorrect > row_published - row_valid:
            raise ValueError(
                f"row {index}: incorrect_alias_matches cannot exceed invalid published mentions"
            )
        published += row_published
        valid += row_valid
        missed += row_missed
        incorrect_alias += row_incorrect
    if published == 0 or valid + missed == 0:
        raise ValueError("job review has no measurable mentions")

    valid_relations = 0
    for index, row in enumerate(relations, 2):
        reviewer = _human_reviewer(row, index, prefix="relation row")
        reviewers.add(reviewer)
        decision = str(row.get("is_valid", "")).strip()
        if decision not in {"0", "1"}:
            raise ValueError(f"relation row {index}: is_valid must be 0 or 1")
        valid_relations += int(decision)
    if not relations:
        raise ValueError("relation review is empty")

    metrics = {
        "mention_precision": round(valid / published, 8),
        "mention_recall": round(valid / (valid + missed), 8),
        "exact_alias_precision": round((published - incorrect_alias) / published, 8),
        "published_relation_precision": round(valid_relations / len(relations), 8),
    }
    checks = {name: metrics[name] >= threshold for name, threshold in THRESHOLDS.items()}
    quality_gate_passed = all(checks.values())
    result = {
        "status": "passed" if quality_gate_passed else "failed",
        "human_review_complete": True,
        "quality_gate_passed": quality_gate_passed,
        "serving_approved": False,
        "serving_approval_status": "pending_ranking_and_runtime_gates",
        "packet_manifest_hash": manifest.get("manifest_hash"),
        "annotated_file_hashes": {
            "job-review.csv": _sha256_path(packet_dir / "job-review.csv"),
            "relation-review.csv": _sha256_path(packet_dir / "relation-review.csv"),
        },
        "reviewers": sorted(reviewers),
        "counts": {
            "reviewed_jobs": len(jobs),
            "published_mentions": published,
            "valid_published_mentions": valid,
            "missed_reviewed_mentions": missed,
            "incorrect_alias_matches": incorrect_alias,
            "reviewed_relations": len(relations),
            "valid_relations": valid_relations,
        },
        **metrics,
        "thresholds": THRESHOLDS,
        "checks": checks,
    }
    result["report_hash"] = hashlib.sha256(_canonical_json(result)).hexdigest()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Score a completed graph review packet")
    parser.add_argument("--packet", type=Path, default=Path("artifacts/skill-graph-review/deterministic-v1-full"))
    parser.add_argument("--output", type=Path, default=Path("reports/deterministic-graph-gold.json"))
    args = parser.parse_args()
    try:
        result = score_review_packet(args.packet)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"human review is incomplete or invalid: {exc}") from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(_canonical_json(result))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
