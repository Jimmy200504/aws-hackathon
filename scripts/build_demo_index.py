#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import math
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "dataset"
DEFAULT_OUTPUT = ROOT / "artifacts" / "demo-index.json"
DEFAULT_ONTOLOGY = ROOT / "config" / "skill_ontology.seed.json"
TRAIN_CUTOFF = datetime.fromisoformat("2026-06-05 23:59:59.999")
MIN_DATE = datetime.fromisoformat("2024-01-01 00:00:00")
def norm(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower().replace("臺", "台")
    return re.sub(r"\s+", " ", text).strip()


def parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return MIN_DATE


def compile_alias_matcher(aliases: dict[str, list[str]]) -> tuple[re.Pattern, dict[str, list[str]]]:
    alias_to_skills: dict[str, list[str]] = {}
    for skill_id, values in aliases.items():
        for value in values:
            candidate = norm(value)
            if candidate:
                alias_to_skills.setdefault(candidate, []).append(skill_id)
    # One compiled pass is dramatically faster than O(jobs × aliases) scans.
    # Longest-first keeps ReactJS from being swallowed by the shorter React.
    ordered = sorted(alias_to_skills, key=len, reverse=True)
    alternatives = "|".join(
        (
            rf"(?<![a-z0-9.+#]){re.escape(value)}(?![a-z0-9.+#])"
            if value.isascii()
            else re.escape(value)
        )
        for value in ordered
    )
    return re.compile(alternatives, re.IGNORECASE), alias_to_skills


def load_lookup(path: Path, names: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            values = [row.get(name, "").strip() for name in names if row.get(name, "").strip()]
            result[row["CodeNo"]] = list(dict.fromkeys(values))
    return result


def select_jobs(
    data_dir: Path,
    ontology: dict,
    per_skill: int,
    per_category: int,
    max_jobs: int,
) -> tuple[list[dict], dict]:
    aliases: dict[str, list[str]] = {
        skill_id: [spec.get("label", ""), *spec.get("aliases", [])]
        for skill_id, spec in ontology["skills"].items()
    }
    alias_pattern, alias_to_skills = compile_alias_matcher(aliases)
    heaps: dict[str, list[tuple[float, str, dict]]] = {skill_id: [] for skill_id in aliases}
    common_heaps: dict[str, list[tuple[float, str, dict]]] = {}
    seen_rows = 0
    future_modified = 0
    missing_text = 0
    source = data_dir / "職缺.csv"
    with source.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            seen_rows += 1
            modified = parse_time(row["職缺最後修改時間"])
            graph_eligible = modified <= TRAIN_CUTOFF
            future_modified += not graph_eligible
            title = row["職務名稱"].strip()
            description = row["職務內容"].strip()
            missing_text += not bool(title or description)
            categories = [
                value
                for value in [row["職務大類"], row["職務中類"], row["職務小類"]]
                if value and value != "NULL"
            ]
            searchable = norm(
                " ".join(
                    [
                        title,
                        row["電腦技能資料"],
                        row["工作技能"],
                        row["專業證照"],
                        *categories,
                    ]
                )
            )
            matched_aliases: dict[str, str] = {}
            evidence: dict[str, str] = {}
            confidence: dict[str, float] = {}
            for match in alias_pattern.finditer(searchable):
                matched_alias = norm(match.group(0))
                for skill_id in alias_to_skills.get(matched_alias, []):
                    matched_aliases.setdefault(skill_id, matched_alias)
            matched = list(matched_aliases)
            if not matched:
                continue
            normalized_title = norm(title)
            normalized_structured = norm(row["電腦技能資料"] + " " + row["工作技能"])
            for skill_id, matched_alias in matched_aliases.items():
                if matched_alias in normalized_title:
                    evidence[skill_id] = f"職稱：{title[:80]}"
                    confidence[skill_id] = 0.96
                elif matched_alias in normalized_structured:
                    evidence[skill_id] = f"結構化技能欄位：{matched_alias}"
                    confidence[skill_id] = 0.93
                else:
                    evidence[skill_id] = f"職稱／分類文字：{matched_alias}"
                    confidence[skill_id] = 0.79
            days = max(0.0, (min(modified, TRAIN_CUTOFF) - MIN_DATE).total_seconds() / 86400)
            completeness = min(1.0, len(description) / 450)
            priority = days + completeness + (0.25 if graph_eligible else 0.0)
            job = {
                "id": row["職缺編號"],
                "title": title,
                "description": description[:420],
                "salary": row["薪資"].replace("‧", " · "),
                "city": row["工作城市"],
                "categories": categories,
                "industry": row["產業中類"] or row["產業大類"],
                "company_id": row["廠商編號"],
                "modified_at": row["職缺最後修改時間"],
                "graph_eligible": graph_eligible,
                "skills": sorted(set(matched)) if graph_eligible else [],
                "skill_evidence": evidence if graph_eligible else {},
                "skill_confidence": confidence if graph_eligible else {},
                "freshness": round(
                    max(0.0, min(1.0, (modified - MIN_DATE).total_seconds() / max(1, (TRAIN_CUTOFF - MIN_DATE).total_seconds()))),
                    4,
                ),
                "view_count": 0,
                "apply_count": 0,
            }
            for skill_id in matched:
                heap = heaps[skill_id]
                item = (priority, job["id"], job)
                if len(heap) < per_skill:
                    heapq.heappush(heap, item)
                elif item[:2] > heap[0][:2]:
                    heapq.heapreplace(heap, item)
            if categories:
                category = categories[-1]
                heap = common_heaps.setdefault(category, [])
                item = (priority, job["id"], job)
                if len(heap) < per_category:
                    heapq.heappush(heap, item)
                elif item[:2] > heap[0][:2]:
                    heapq.heapreplace(heap, item)

    selected: dict[str, dict] = {}
    for heap in heaps.values():
        for _, job_id, job in heap:
            selected[job_id] = job
    for heap in common_heaps.values():
        for _, job_id, job in heap:
            selected.setdefault(job_id, job)
    jobs = sorted(selected.values(), key=lambda job: (job["title"], job["id"]))[:max_jobs]
    stats = {
        "source_job_rows": seen_rows,
        "future_modified_excluded_from_graph": future_modified,
        "missing_job_text": missing_text,
        "selected_demo_jobs": len(jobs),
    }
    return jobs, stats


def add_behavior_counts(data_dir: Path, jobs: list[dict]) -> dict:
    selected = {job["id"]: job for job in jobs}
    view_rows = apply_rows = 0
    selected_views = selected_applies = 0
    with (data_dir / "職缺瀏覽_20260601_20260607.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            view_rows += 1
            job = selected.get(row["employeeNo"])
            if job is not None:
                job["view_count"] += 1
                selected_views += 1
    with (data_dir / "主動應徵_0601-0607.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            apply_rows += 1
            job = selected.get(row["empNo"])
            if job is not None:
                job["apply_count"] += 1
                selected_applies += 1
    return {
        "source_view_rows": view_rows,
        "source_apply_rows": apply_rows,
        "selected_job_views": selected_views,
        "selected_job_applies": selected_applies,
    }


def schema_fingerprint(data_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in [
        "城市對照表.csv",
        "職務對照表.csv",
        "職缺.csv",
        "userSearchLog_20260601_20260607.csv",
        "職缺瀏覽_20260601_20260607.csv",
        "主動應徵_0601-0607.csv",
    ]:
        path = data_dir / name
        with path.open("rb") as handle:
            digest.update(handle.readline())
        digest.update(str(path.stat().st_size).encode())
    return digest.hexdigest()[:16]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a compact real-data demo index")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--ontology", type=Path, default=DEFAULT_ONTOLOGY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--per-skill", type=int, default=100)
    parser.add_argument("--per-category", type=int, default=30)
    parser.add_argument("--max-jobs", type=int, default=12000)
    parser.add_argument("--skip-behavior", action="store_true")
    args = parser.parse_args()

    ontology = json.loads(args.ontology.read_text(encoding="utf-8"))
    print("Selecting representative jobs with temporal graph gating…", flush=True)
    jobs, stats = select_jobs(
        args.data_dir, ontology, args.per_skill, args.per_category, args.max_jobs
    )
    if not args.skip_behavior:
        print("Aggregating view/apply signals for selected jobs…", flush=True)
        stats.update(add_behavior_counts(args.data_dir, jobs))

    locations = load_lookup(
        args.data_dir / "城市對照表.csv", ["CodeNameA", "CodeNameB", "CodeNameC"]
    )
    duties = load_lookup(
        args.data_dir / "職務對照表.csv", ["CodeNameA", "CodeNameB", "CodeNameC", "CodeNameEN"]
    )
    artifact = {
        "metadata": {
            "index_version": "demo-2026.06.05-v1",
            "dataset_version": "1111-2026-06-01_2026-06-07",
            "schema_fingerprint": schema_fingerprint(args.data_dir),
            "graph_train_cutoff": TRAIN_CUTOFF.isoformat(sep=" "),
            "graph_builder": "reviewed-bootstrap-fixture",
            "production_graph_builder": "amazon-bedrock-structured-extraction",
            "random_seed": 1111,
            "stats": stats,
            "limitations": [
                "Compact laptop artifact; production candidate retrieval uses OpenSearch.",
                "Bootstrap ontology is a validation fixture, not claimed as Bedrock-generated benchmark output.",
                "Jobs modified after the graph cutoff use an explicit cold-start path and contribute no JD-derived graph edges."
            ],
        },
        "locations": locations,
        "duties": duties,
        "skills": ontology["skills"],
        "jobs": jobs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(json.dumps(artifact["metadata"], ensure_ascii=False, indent=2))
    print(f"Wrote {len(jobs):,} jobs to {args.output} ({args.output.stat().st_size / 1_048_576:.1f} MiB)")


if __name__ == "__main__":
    main()
