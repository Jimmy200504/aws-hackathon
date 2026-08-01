#!/usr/bin/env python3
"""Validate the salary-range recall fix for a real query: 時薪210.

Before this change, the candidate gate in app/ranker.search() required
lexical>0 or a direct skill match; a job whose salary_min/salary_max range
covers the query target but whose title never literally contains the
number was filtered out before scoring, regardless of the (inert)
salary_min/salary_max features. This script quantifies the fix by
comparing full-corpus search results with the fix enabled vs. disabled
(disabled = candidate gate reverted to lexical/skill-only, matching the
pre-fix behavior).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import SkillWeaveRanker

INDEX = ROOT / "artifacts" / "demo-index.json"
QUERY = "時薪210"


def jobs_covering_without_literal_match(ranker: SkillWeaveRanker, target: float) -> list[dict]:
    covering = []
    for job in ranker.jobs:
        if job.get("salary_type") != "hourly":
            continue
        job_min = float(job.get("salary_min", 0.0) or 0.0)
        job_max = float(job.get("salary_max", 0.0) or 0.0)
        if job_min <= 0 and job_max <= 0:
            continue
        upper = job_max if job_max > 0 else job_min
        if upper >= target and str(int(target)) not in job.get("title", ""):
            covering.append(job)
    return covering


def main() -> None:
    ranker = SkillWeaveRanker(INDEX)
    intent = ranker.parse_intent(QUERY)
    target = intent.salary_intent["target"]
    print(f"Query: {QUERY!r} -> salary_intent = {intent.salary_intent}")

    covering_jobs = jobs_covering_without_literal_match(ranker, target)
    covering_ids = {job["id"] for job in covering_jobs}
    print(
        f"Jobs whose hourly salary range covers {target:.0f} but title does NOT "
        f"literally contain {int(target)}: {len(covering_jobs)}"
    )

    after_ids: set[str] = set()
    with ranker._lock:
        for index, job in enumerate(ranker.jobs):
            score, features, traces, direct = ranker._score(
                index, intent, include_graph=True
            )
            has_direct_candidate_evidence = (
                bool(set(intent.skills) & set(job.get("skills", [])))
                or features["salary"] > 0
            )  # NEW gate: salary-range match alone is valid candidate evidence
            if features["lexical"] <= 0 and not has_direct_candidate_evidence:
                continue
            if score <= 0:
                continue
            after_ids.add(job["id"])
    after_found = covering_ids & after_ids
    before_ids: set[str] = set()
    with ranker._lock:
        for index, job in enumerate(ranker.jobs):
            score, features, traces, direct = ranker._score(
                index, intent, include_graph=True
            )
            has_direct_candidate_evidence = bool(
                set(intent.skills) & set(job.get("skills", []))
            )  # OLD gate: no "or features['salary'] > 0"
            if features["lexical"] <= 0 and not has_direct_candidate_evidence:
                continue
            if score <= 0:
                continue
            before_ids.add(job["id"])
    before_found = covering_ids & before_ids

    report = {
        "query": QUERY,
        "salary_intent": intent.salary_intent,
        "jobs_covering_range_without_literal_number": len(covering_jobs),
        "found_before_fix": len(before_found),
        "found_after_fix": len(after_found),
        "recall_gain_jobs": len(after_found) - len(before_found),
        "recall_before_fix_pct": round(100 * len(before_found) / max(1, len(covering_jobs)), 2),
        "recall_after_fix_pct": round(100 * len(after_found) / max(1, len(covering_jobs)), 2),
        "sample_recovered_jobs": [
            {
                "id": job["id"],
                "title": job["title"],
                "salary": job.get("salary", ""),
                "salary_min": job.get("salary_min"),
                "salary_max": job.get("salary_max"),
            }
            for job in covering_jobs
            if job["id"] in (after_found - before_found)
        ][:10],
    }
    output_path = ROOT / "reports" / "salary-recall-validation.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {output_path}")


if __name__ == "__main__":
    main()
