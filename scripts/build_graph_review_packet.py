#!/usr/bin/env python3
"""Build a deterministic, human-only review packet for the cutoff graph."""
from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from pipeline.build_graph import iter_jsonl


REVIEW_VERSION = "deterministic-graph-human-review-v1"
DEFAULT_SEED = "skill-graph-release-review-v1"
RAW_REVIEW_FIELDS = (
    "職務名稱", "職務內容", "職務大類", "職務中類", "職務小類",
    "電腦技能資料", "工作技能", "專業證照", "附加條件",
)


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_cell(value: Any) -> str:
    text = str(value or "")
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _sample_score(seed: str, stratum: str, job_id: str) -> int:
    return int(hashlib.sha256(f"{seed}\0{stratum}\0{job_id}".encode()).hexdigest(), 16)


def deterministic_job_sample(
    records: Iterable[dict[str, Any]],
    *,
    per_stratum: int,
    seed: str = DEFAULT_SEED,
) -> list[dict[str, Any]]:
    """Select the lowest stable hashes from mentioned and no-mention jobs."""
    heaps: dict[str, list[tuple[int, str, dict[str, Any]]]] = {"mentioned": [], "no_mentions": []}
    for row in records:
        stratum = "mentioned" if row.get("mentions") else "no_mentions"
        job_id = str(row.get("job_id", ""))
        score = _sample_score(seed, stratum, job_id)
        sampled = dict(row)
        sampled["sample_stratum"] = stratum
        sampled["sample_hash"] = f"{score:064x}"
        entry = (-score, job_id, sampled)
        heap = heaps[stratum]
        if len(heap) < per_stratum:
            heapq.heappush(heap, entry)
        elif score < -heap[0][0]:
            heapq.heapreplace(heap, entry)
    missing = [name for name, values in heaps.items() if len(values) != per_stratum]
    if missing:
        raise ValueError(f"insufficient rows for review strata: {', '.join(missing)}")
    return sorted(
        (entry[2] for values in heaps.values() for entry in values),
        key=lambda row: (row["sample_stratum"], row["sample_hash"], str(row["job_id"])),
    )


def _load_raw_jobs(path: Path, job_ids: set[str]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            job_id = str(row.get("職缺編號", ""))
            if job_id in job_ids:
                result.setdefault(job_id, row)
                if len(result) == len(job_ids):
                    break
    missing = sorted(job_ids - set(result))
    if missing:
        raise ValueError(f"sampled jobs missing from source CSV: {missing[:5]}")
    return result


def _write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: _safe_cell(row.get(name, "")) for name in fieldnames})
            count += 1
    return count


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def carry_forward_reviews(
    current_rows: list[dict[str, Any]],
    previous_rows: Iterable[dict[str, str]],
    *,
    key: str,
    locked_fields: tuple[str, ...],
    review_fields: tuple[str, ...],
    required_review_fields: tuple[str, ...],
) -> int:
    """Copy adjudication only when every locked source field is unchanged."""
    previous_by_key = {str(row.get(key, "")): row for row in previous_rows}
    carried = 0
    for row in current_rows:
        previous = previous_by_key.get(str(row.get(key, "")))
        if previous is None:
            continue
        if any(_safe_cell(row.get(field, "")) != str(previous.get(field, "")) for field in locked_fields):
            continue
        if any(not str(previous.get(field, "")).strip() for field in required_review_fields):
            continue
        for field in review_fields:
            row[field] = previous.get(field, "")
        carried += 1
    return carried


def _skill_labels(nodes_path: Path) -> dict[str, str]:
    return {
        str(row["id"]): str(row.get("label", row["id"]))
        for row in iter_jsonl(nodes_path)
        if row.get("type") == "Skill"
    }


def build_review_packet(
    *,
    jobs_csv: Path,
    accepted_path: Path,
    nodes_path: Path,
    relation_candidates_path: Path,
    relation_edges_path: Path,
    surface_candidates_path: Path,
    extraction_manifest_path: Path,
    graph_manifest_path: Path,
    output_dir: Path,
    per_stratum: int = 120,
    seed: str = DEFAULT_SEED,
    carry_forward_from: Path | None = None,
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite an existing review packet: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    sample = deterministic_job_sample(iter_jsonl(accepted_path), per_stratum=per_stratum, seed=seed)
    raw_jobs = _load_raw_jobs(jobs_csv, {str(row["job_id"]) for row in sample})

    job_fields = [
        "sample_stratum", "sample_hash", "job_id", "source_modified_at", *RAW_REVIEW_FIELDS,
        "published_mention_count", "published_mentions_json",
        "valid_published_mentions", "missed_reviewed_mentions", "incorrect_alias_matches",
        "reviewer", "review_notes",
    ]
    job_rows = []
    for row in sample:
        raw = raw_jobs[str(row["job_id"])]
        mentions = row.get("mentions", [])
        job_rows.append({
            "sample_stratum": row["sample_stratum"],
            "sample_hash": row["sample_hash"],
            "job_id": row["job_id"],
            "source_modified_at": row.get("source_modified_at", ""),
            **{field: raw.get(field, "") for field in RAW_REVIEW_FIELDS},
            "published_mention_count": len(mentions),
            "published_mentions_json": json.dumps(mentions, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "valid_published_mentions": "",
            "missed_reviewed_mentions": "",
            "incorrect_alias_matches": "",
            "reviewer": "",
            "review_notes": "",
        })
    carried_jobs = 0
    if carry_forward_from is not None:
        carried_jobs = carry_forward_reviews(
            job_rows,
            _read_csv(carry_forward_from / "job-review.csv"),
            key="job_id",
            locked_fields=(
                "job_id", "source_modified_at", *RAW_REVIEW_FIELDS, "published_mentions_json",
            ),
            review_fields=(
                "valid_published_mentions", "missed_reviewed_mentions", "incorrect_alias_matches",
                "reviewer", "review_notes",
            ),
            required_review_fields=(
                "valid_published_mentions", "missed_reviewed_mentions", "incorrect_alias_matches", "reviewer",
            ),
        )
    job_count = _write_csv(output_dir / "job-review.csv", job_fields, job_rows)

    labels = _skill_labels(nodes_path)
    candidate_by_pair = {
        (str(row["source_id"]), str(row["target_id"])): row
        for row in iter_jsonl(relation_candidates_path)
    }
    relation_fields = [
        "edge_id", "source_id", "source_label", "target_id", "target_label",
        "support_jobs", "support_companies", "lift", "npmi", "statistical_confidence",
        "evidence_json", "is_valid", "reviewer", "review_notes",
    ]
    relation_rows = []
    for edge in iter_jsonl(relation_edges_path):
        pair = (str(edge["source_id"]), str(edge["target_id"]))
        candidate = candidate_by_pair.get(pair)
        if candidate is None:
            raise ValueError(f"published relation has no candidate statistics: {pair}")
        relation_rows.append({
            "edge_id": edge["id"],
            "source_id": pair[0],
            "source_label": labels.get(pair[0], pair[0]),
            "target_id": pair[1],
            "target_label": labels.get(pair[1], pair[1]),
            "support_jobs": candidate["support_jobs"],
            "support_companies": candidate["support_companies"],
            "lift": candidate["lift"],
            "npmi": candidate["npmi"],
            "statistical_confidence": candidate["statistical_confidence"],
            "evidence_json": json.dumps(edge.get("evidence", []), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "is_valid": "",
            "reviewer": "",
            "review_notes": "",
        })
    relation_rows.sort(key=lambda row: str(row["edge_id"]))
    carried_relations = 0
    if carry_forward_from is not None:
        carried_relations = carry_forward_reviews(
            relation_rows,
            _read_csv(carry_forward_from / "relation-review.csv"),
            key="edge_id",
            locked_fields=("edge_id", "source_id", "source_label", "target_id", "target_label", "evidence_json"),
            review_fields=("is_valid", "reviewer", "review_notes"),
            required_review_fields=("is_valid", "reviewer"),
        )
    relation_count = _write_csv(output_dir / "relation-review.csv", relation_fields, relation_rows)

    surface_fields = [
        "candidate_id", "normalized_surface", "surfaces_json", "support_jobs", "support_companies",
        "evidence_fields", "decision", "canonical_node_id", "reviewer", "review_notes",
    ]
    surfaces = sorted(
        iter_jsonl(surface_candidates_path),
        key=lambda row: (-int(row["support_jobs"]), -int(row["support_companies"]), str(row["normalized_surface"])),
    )
    surface_count = _write_csv(output_dir / "surface-candidate-review.csv", surface_fields, ({
        "candidate_id": row["candidate_id"],
        "normalized_surface": row["normalized_surface"],
        "surfaces_json": json.dumps(row.get("surfaces", []), ensure_ascii=False, separators=(",", ":")),
        "support_jobs": row["support_jobs"],
        "support_companies": row["support_companies"],
        "evidence_fields": ";".join(row.get("evidence_fields", [])),
        "decision": "",
        "canonical_node_id": "",
        "reviewer": "",
        "review_notes": "",
    } for row in surfaces))

    readme = (
        "# Deterministic Skill Graph 人工審閱\n\n"
        "此封包只用於 evaluation-cutoff release gate，不會自動修改 ontology 或 serving graph。\n\n"
        "1. `job-review.csv`：逐列填寫 valid_published_mentions、missed_reviewed_mentions、"
        "incorrect_alias_matches、reviewer。三個計數皆須為非負整數。\n"
        "2. `relation-review.csv`：逐列將 is_valid 填為 1 或 0，並填 reviewer。\n"
        "3. `surface-candidate-review.csv`：是下一版 ontology queue，不影響本次 gate，可稍後審閱。\n"
        "4. 完成後執行 `python3 scripts/score_graph_review.py --packet "
        f"{output_dir.as_posix()}`。未完成的列會 fail closed，且不會產生 gold report。\n"
    )
    (output_dir / "README.md").write_text(readme, encoding="utf-8")
    extraction_manifest = json.loads(extraction_manifest_path.read_text(encoding="utf-8"))
    graph_manifest = json.loads(graph_manifest_path.read_text(encoding="utf-8"))
    files = []
    for path in sorted(output_dir.iterdir()):
        if path.name == "manifest.json" or not path.is_file():
            continue
        files.append({"path": path.name, "sha256": _sha256_path(path), "bytes": path.stat().st_size})
    manifest = {
        "review_version": REVIEW_VERSION,
        "status": "awaiting_human_review",
        "serving_approved": False,
        "scope": "evaluation-cutoff",
        "sample_seed": seed,
        "job_sample_per_stratum": per_stratum,
        "job_review_rows": job_count,
        "job_reviews_carried_forward": carried_jobs,
        "job_reviews_requiring_review": job_count - carried_jobs,
        "relation_review_rows": relation_count,
        "relation_reviews_carried_forward": carried_relations,
        "relation_reviews_requiring_review": relation_count - carried_relations,
        "surface_candidate_rows": surface_count,
        "input_hash": extraction_manifest.get("input_hash"),
        "ontology_hash": extraction_manifest.get("ontology_hash"),
        "graph_manifest_hash": graph_manifest.get("manifest_hash"),
        "carry_forward_packet_manifest_hash": (
            json.loads((carry_forward_from / "manifest.json").read_text(encoding="utf-8")).get("manifest_hash")
            if carry_forward_from is not None else None
        ),
        "carry_forward_annotated_hashes": ({
            "job-review.csv": _sha256_path(carry_forward_from / "job-review.csv"),
            "relation-review.csv": _sha256_path(carry_forward_from / "relation-review.csv"),
        } if carry_forward_from is not None else None),
        "files": files,
    }
    manifest["manifest_hash"] = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    (output_dir / "manifest.json").write_bytes(_canonical_json(manifest))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic graph human-review CSVs")
    parser.add_argument("--jobs", type=Path, default=Path("data/dataset/職缺.csv"))
    parser.add_argument("--accepted", type=Path, default=Path("artifacts/skill-graph-full/extraction/evaluation-cutoff/accepted"))
    parser.add_argument("--nodes", type=Path, default=Path("artifacts/skill-graph-full/resolved/evaluation-cutoff/nodes.jsonl"))
    parser.add_argument("--relation-candidates", type=Path, default=Path("artifacts/skill-graph-full/relations/evaluation-cutoff/relation-candidates.jsonl"))
    parser.add_argument("--relation-edges", type=Path, default=Path("artifacts/skill-graph-full/relations/evaluation-cutoff/relation-edges.jsonl"))
    parser.add_argument("--surface-candidates", type=Path, default=Path("artifacts/skill-graph-full/extraction/evaluation-cutoff/surface-candidates.jsonl"))
    parser.add_argument("--extraction-manifest", type=Path, default=Path("artifacts/skill-graph-full/extraction/evaluation-cutoff/manifest.json"))
    parser.add_argument("--graph-manifest", type=Path, default=Path("artifacts/skill-graph-full/release/runs/deterministic-v1-full/evaluation-cutoff/manifest.json"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/skill-graph-review/deterministic-v1-full"))
    parser.add_argument("--per-stratum", type=int, default=120)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--carry-forward-from", type=Path)
    args = parser.parse_args()
    manifest = build_review_packet(
        jobs_csv=args.jobs,
        accepted_path=args.accepted,
        nodes_path=args.nodes,
        relation_candidates_path=args.relation_candidates,
        relation_edges_path=args.relation_edges,
        surface_candidates_path=args.surface_candidates,
        extraction_manifest_path=args.extraction_manifest,
        graph_manifest_path=args.graph_manifest,
        output_dir=args.output,
        per_stratum=args.per_stratum,
        seed=args.seed,
        carry_forward_from=args.carry_forward_from,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
