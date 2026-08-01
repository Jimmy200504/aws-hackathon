#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_demo_index import (
    MIN_DATE,
    TRAIN_CUTOFF,
    compile_alias_matcher,
    load_lookup,
    norm,
    parse_time,
    schema_fingerprint,
)
from app.job_fields import derive_job_fields


DATA = ROOT / "data" / "dataset"
ONTOLOGY = ROOT / "config" / "skill_ontology.seed.json"
QRELS_OUTPUT = ROOT / "artifacts" / "temporal-eval.json"
INDEX_OUTPUT = ROOT / "artifacts" / "benchmark-index.json"


def stable_hash(value: str) -> int:
    return int(hashlib.sha256(("1111:" + value).encode()).hexdigest()[:12], 16)


def load_duty_ontology(data_dir: Path) -> dict[str, dict]:
    """Build unambiguous occupation nodes from the organizer's duty table."""
    raw: list[tuple[str, dict, list[str]]] = []
    alias_frequency: Counter[str] = Counter()
    with (data_dir / "職務對照表.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            label = row["CodeNameA"].strip()
            if not label:
                continue
            aliases = [label, row["CodeNameEN"].strip()]
            aliases.extend(
                re.split(r"<br\s*/?>|[、;；\n]", row["CodeAlike"], flags=re.IGNORECASE)
            )
            cleaned = []
            for alias in aliases:
                value = norm(re.sub(r"<[^>]+>", " ", alias)).strip(" /,，")
                if len(value) < 2 or len(value) > 60:
                    continue
                cleaned.append(value)
            cleaned = list(dict.fromkeys(cleaned))[:30]
            for alias in set(cleaned):
                alias_frequency[alias] += 1
            raw.append((row["CodeNo"], row, cleaned))
    nodes: dict[str, dict] = {}
    for code, row, aliases in raw:
        label = row["CodeNameA"].strip()
        # Ambiguous aliases remain query text, but are not forced to one node.
        accepted = [
            alias for alias in aliases if alias == norm(label) or alias_frequency[alias] <= 2
        ]
        nodes[f"duty.{code}"] = {
            "type": "Occupation",
            "label": label,
            "aliases": accepted,
            "related": {},
            "provenance": "organizer_duty_reference",
            "duty_code": code,
            "parent": [row["CodeNameB"], row["CodeNameC"]],
        }
    return nodes


def read_sampled_searches(
    data_dir: Path,
    days: set[str],
    train_days: set[str],
    train_sample_basis_points: int,
    eval_sample_basis_points: int,
    candidate_depth: int,
    test_day: str,
    test_sample_bucket_start: int,
    test_sample_basis_points: int,
) -> tuple[list[dict], set[str], set[str]]:
    rows: list[dict] = []
    users: set[str] = set()
    exposed_jobs: set[str] = set()
    print("Pass 1/4 · deterministic search-session sample…", flush=True)
    with (data_dir / "userSearchLog_20260601_20260607.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            talent = row["talentNo"]
            day = row["search_time"][:10]
            if (
                day not in days
                or talent == "0"
                or not row["empStr"]
            ):
                continue
            identity = "|".join([talent, row["search_time"], row["ks"], row["c0"], row["d0"]])
            sample_basis_points = (
                train_sample_basis_points
                if day in train_days
                else (
                    test_sample_basis_points
                    if day == test_day
                    else eval_sample_basis_points
                )
            )
            bucket = stable_hash(identity) % 10_000
            bucket_start = (
                test_sample_bucket_start
                if day == test_day and day not in train_days
                else 0
            )
            if not bucket_start <= bucket < bucket_start + sample_basis_points:
                continue
            candidates = list(dict.fromkeys(row["empStr"].split(",")[:candidate_depth]))
            if len(candidates) < 2:
                continue
            record = {
                "identity": identity,
                "talent": talent,
                "query": row["ks"],
                "location_code": [x for x in row["c0"].split(",") if x],
                "duty_code": [x for x in row["d0"].split(",") if x],
                "search_time": datetime.fromisoformat(row["search_time"]),
                "day": row["search_time"][:10],
                "candidates": candidates,
            }
            rows.append(record)
            users.add(talent)
            exposed_jobs.update(candidates)
    return rows, users, exposed_jobs


def read_relevant_events(
    data_dir: Path,
    days: set[str],
    users: set[str],
    exposed_jobs: set[str],
) -> dict[tuple[str, str], list[tuple[datetime, int]]]:
    events: dict[tuple[str, str], list[tuple[datetime, int]]] = defaultdict(list)
    print("Pass 2/4 · future-only view/apply attribution…", flush=True)
    sources = [
        ("職缺瀏覽_20260601_20260607.csv", "employeeNo", "dateIn", 1),
        ("主動應徵_0601-0607.csv", "empNo", "datein", 2),
    ]
    for filename, job_field, time_field, grade in sources:
        with (data_dir / filename).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                talent = row["talentNo"]
                job_id = row[job_field]
                timestamp = row[time_field]
                if (
                    timestamp[:10] in days
                    and talent in users
                    and job_id in exposed_jobs
                ):
                    events[(talent, job_id)].append((datetime.fromisoformat(timestamp), grade))
    for values in events.values():
        values.sort()
    return events


def grade_in_window(
    values: list[tuple[datetime, int]] | None,
    start: datetime,
    minutes: int,
) -> int:
    if not values:
        return 0
    index = bisect.bisect_left(values, (start, 0))
    end = start + timedelta(minutes=minutes)
    best = 0
    while index < len(values) and values[index][0] <= end:
        best = max(best, values[index][1])
        index += 1
    return best


def aggregate_cases(
    searches: list[dict],
    events: dict[tuple[str, str], list[tuple[datetime, int]]],
    train_days: set[str],
    max_train_per_day: int,
    max_eval_per_day: int,
    max_test_per_day: int,
    test_day: str,
    window_minutes: int,
) -> dict[str, list[dict]]:
    grouped: dict[tuple[str, str, str, str], dict] = {}
    print("Pass 3/4 · aggregate graded (query, job) pairs…", flush=True)
    for row in searches:
        qrels = {
            job_id: grade_in_window(
                events.get((row["talent"], job_id)),
                row["search_time"],
                window_minutes,
            )
            for job_id in row["candidates"]
        }
        if max(qrels.values(), default=0) <= 0:
            continue
        key = (
            row["day"],
            row["query"],
            ",".join(row["location_code"]),
            ",".join(row["duty_code"]),
        )
        case = grouped.setdefault(
            key,
            {
                "query_id": "q_" + hashlib.sha256("|".join(key).encode()).hexdigest()[:16],
                "day": row["day"],
                "query": row["query"],
                "location_code": row["location_code"],
                "duty_code": row["duty_code"],
                "candidates": [],
                "qrels": {},
                "_seen": set(),
            },
        )
        for job_id in row["candidates"]:
            if job_id not in case["_seen"] and len(case["candidates"]) < 200:
                case["_seen"].add(job_id)
                case["candidates"].append(job_id)
            case["qrels"][job_id] = max(case["qrels"].get(job_id, 0), qrels[job_id])
    by_day: dict[str, list[dict]] = defaultdict(list)
    for (day, *_), case in grouped.items():
        case.pop("_seen")
        case["label_policy"] = (
            f"max(view=1, apply=2) in [search_time, search_time+{window_minutes}m], "
            "aggregated by (query, location, duty, job)"
        )
        by_day[day].append(case)
    return {
        day: sorted(rows, key=lambda case: stable_hash(case["query_id"]))[
            : (
                max_train_per_day
                if day in train_days
                else max_test_per_day if day == test_day else max_eval_per_day
            )
        ]
        for day, rows in by_day.items()
    }


def build_job(
    row: dict,
    alias_pattern: re.Pattern,
    alias_to_skills: dict[str, list[str]],
    train_title_snapshot: tuple[str, str] | None = None,
) -> dict:
    modified = parse_time(row["職缺最後修改時間"])
    jd_graph_eligible = modified <= TRAIN_CUTOFF
    graph_eligible = jd_graph_eligible or train_title_snapshot is not None
    title = row["職務名稱"].strip()
    description = row["職務內容"].strip()
    categories = [
        value
        for value in [row["職務大類"], row["職務中類"], row["職務小類"]]
        if value and value != "NULL"
    ]
    if jd_graph_eligible:
        graph_source_text = " ".join(
            [
                title,
                row["電腦技能資料"],
                row["工作技能"],
                row["專業證照"],
                *categories,
            ]
        )
        graph_title = title
        graph_structured = row["電腦技能資料"] + " " + row["工作技能"]
        graph_source = "train_eligible_jd"
        graph_source_time = row["職缺最後修改時間"]
    elif train_title_snapshot is not None:
        graph_title, graph_source_time = train_title_snapshot
        graph_source_text = graph_title
        graph_structured = ""
        graph_source = "train_apply_title_snapshot"
    else:
        graph_source_text = ""
        graph_title = ""
        graph_structured = ""
        graph_source = "cold_start_no_train_text"
        graph_source_time = ""
    searchable = norm(graph_source_text)
    matched_aliases: dict[str, str] = {}
    for match in alias_pattern.finditer(searchable):
        matched_alias = norm(match.group(0))
        for skill_id in alias_to_skills.get(matched_alias, []):
            matched_aliases.setdefault(skill_id, matched_alias)
    title_norm = norm(graph_title)
    structured_norm = norm(graph_structured)
    evidence: dict[str, str] = {}
    confidence: dict[str, float] = {}
    for skill_id, alias in matched_aliases.items():
        if alias in title_norm:
            evidence[skill_id] = (
                f"訓練期應徵職稱快照：{graph_title[:80]}"
                if graph_source == "train_apply_title_snapshot"
                else f"職稱：{graph_title[:80]}"
            )
            confidence[skill_id] = 0.96
        elif alias in structured_norm:
            evidence[skill_id] = f"結構化技能欄位：{alias}"
            confidence[skill_id] = 0.93
        else:
            evidence[skill_id] = f"職稱／分類文字：{alias}"
            confidence[skill_id] = 0.79
    freshness = max(
        0.0,
        min(
            1.0,
            (modified - MIN_DATE).total_seconds()
            / max(1.0, (TRAIN_CUTOFF - MIN_DATE).total_seconds()),
        ),
    )
    return {
        "id": row["職缺編號"],
        "title": title,
        "description": description[:420],
        "salary": row["薪資"].replace("‧", " · "),
        **derive_job_fields(row),
        "city": row["工作城市"],
        "categories": categories,
        "industry": row["產業中類"] or row["產業大類"],
        "company_id": row["廠商編號"],
        "modified_at": row["職缺最後修改時間"],
        "post_cutoff_jd": not jd_graph_eligible,
        "graph_eligible": graph_eligible,
        "graph_source": graph_source,
        "graph_source_time": graph_source_time,
        "skills": sorted(matched_aliases) if graph_eligible else [],
        "skill_evidence": evidence if graph_eligible else {},
        "skill_confidence": confidence if graph_eligible else {},
        "freshness": round(freshness, 4),
        # Evaluation deliberately excludes global popularity to reduce leakage
        # and keep graph/no-graph as the only changed feature family.
        "view_count": 0,
        "apply_count": 0,
    }


def load_train_title_snapshots(
    data_dir: Path, candidate_ids: set[str]
) -> dict[str, tuple[str, str]]:
    """Return latest pre-cutoff apply-time title for judged candidate jobs."""
    snapshots: dict[str, tuple[str, str]] = {}
    with (data_dir / "主動應徵_0601-0607.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            timestamp = row["datein"]
            job_id = row["empNo"]
            if (
                job_id not in candidate_ids
                or not row["empName"].strip()
                or datetime.fromisoformat(timestamp) > TRAIN_CUTOFF
            ):
                continue
            previous = snapshots.get(job_id)
            if previous is None or timestamp > previous[1]:
                snapshots[job_id] = (row["empName"].strip(), timestamp)
    return snapshots


def build_train_behavior_graph(
    cases: dict[str, list[dict]],
    jobs: list[dict],
    train_days: set[str],
) -> dict[str, dict]:
    """Build rolling exposure-normalized Query→Skill/Job train-only edges."""
    jobs_by_id = {job["id"]: job for job in jobs}
    query_job: dict[str, dict[str, list[int]]] = defaultdict(dict)
    query_skill: dict[str, dict[str, list[int]]] = defaultdict(dict)
    job_global: dict[str, list[int]] = {}
    company_global: dict[str, list[int]] = {}
    global_totals = [0, 0, 0]
    snapshots: dict[str, dict] = {}

    def snapshot() -> dict:
        return {
            "query_job": {
                query: {edge: list(stats) for edge, stats in edges.items()}
                for query, edges in query_job.items()
            },
            "query_skill": {
                query: {edge: list(stats) for edge, stats in edges.items()}
                for query, edges in query_skill.items()
            },
            "job_global": {
                edge: list(stats) for edge, stats in job_global.items()
            },
            "company_global": {
                edge: list(stats) for edge, stats in company_global.items()
            },
            "global_totals": list(global_totals),
        }

    for day in sorted(train_days):
        # A row on this day can only see graph edges from earlier days.
        snapshots[day] = snapshot()
        for case in cases.get(day, []):
            query = norm(case["query"])
            if not query:
                continue
            for job_id in case["candidates"]:
                job = jobs_by_id.get(job_id)
                if job is None:
                    continue
                grade = int(case["qrels"].get(job_id, 0))
                job_stats = query_job[query].setdefault(job_id, [0, 0, 0])
                job_stats[0] += 1
                job_stats[1] += int(grade > 0)
                job_stats[2] += grade
                global_job_stats = job_global.setdefault(job_id, [0, 0, 0])
                global_job_stats[0] += 1
                global_job_stats[1] += int(grade > 0)
                global_job_stats[2] += grade
                global_totals[0] += 1
                global_totals[1] += int(grade > 0)
                global_totals[2] += grade
                company_id = str(job.get("company_id", "")).strip()
                if company_id:
                    company_stats = company_global.setdefault(
                        company_id, [0, 0, 0]
                    )
                    company_stats[0] += 1
                    company_stats[1] += int(grade > 0)
                    company_stats[2] += grade
                for skill_id in job.get("skills", []):
                    skill_stats = query_skill[query].setdefault(
                        skill_id, [0, 0, 0]
                    )
                    skill_stats[0] += 1
                    skill_stats[1] += int(grade > 0)
                    skill_stats[2] += grade
    return {
        "query_job": {query: edges for query, edges in query_job.items()},
        "query_skill": {query: edges for query, edges in query_skill.items()},
        "job_global": job_global,
        "company_global": company_global,
        "global_totals": global_totals,
        "snapshots": snapshots,
        "provenance": {
            "source": "train_qrels_only",
            "train_days": sorted(train_days),
            "rolling_snapshot_policy": "strictly earlier train days",
            "edge_stats": "[exposures, positive_events, graded_relevance_sum]",
            "global_prior_policy": "job/company aggregates use train qrels only",
        },
    }


def write_fixture(
    data_dir: Path,
    ontology_path: Path,
    cases: dict[str, list[dict]],
    qrels_output: Path,
    index_output: Path,
    train_days: set[str],
    train_sample_basis_points: int,
    eval_sample_basis_points: int,
    window_minutes: int,
    test_day: str,
    test_sample_bucket_start: int,
    test_sample_basis_points: int,
) -> None:
    ontology = json.loads(ontology_path.read_text(encoding="utf-8"))
    duty_ontology = load_duty_ontology(data_dir)
    ontology["skills"].update(duty_ontology)
    aliases = {
        skill_id: [spec.get("label", ""), *spec.get("aliases", [])]
        for skill_id, spec in ontology["skills"].items()
    }
    pattern, alias_map = compile_alias_matcher(aliases)
    candidate_ids = {
        job_id
        for split_cases in cases.values()
        for case in split_cases
        for job_id in case["candidates"]
    }
    title_snapshots = load_train_title_snapshots(data_dir, candidate_ids)
    jobs: list[dict] = []
    print(f"Pass 4/4 · materialize {len(candidate_ids):,} judged candidates…", flush=True)
    with (data_dir / "職缺.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["職缺編號"] in candidate_ids:
                jobs.append(
                    build_job(
                        row,
                        pattern,
                        alias_map,
                        title_snapshots.get(row["職缺編號"]),
                    )
                )
    found = {job["id"] for job in jobs}
    for split_cases in cases.values():
        for case in split_cases:
            case["candidates"] = [job for job in case["candidates"] if job in found]
            case["qrels"] = {job: grade for job, grade in case["qrels"].items() if job in found}
    behavior_graph = build_train_behavior_graph(cases, jobs, train_days)
    qrels = {
        "metadata": {
            "schema": "skillweave-temporal-qrels-v2",
            "candidate_policy": "rerank original exposure top-100; aggregate max label by query/job",
            "event_window_minutes": window_minutes,
            "train_search_sample_basis_points": train_sample_basis_points,
            "eval_search_sample_basis_points": eval_sample_basis_points,
            "test_day": test_day,
            "test_sample_bucket_start": test_sample_bucket_start,
            "test_sample_bucket_end_exclusive": (
                test_sample_bucket_start + test_sample_basis_points
            ),
            "test_search_sample_basis_points": test_sample_basis_points,
            "anonymous_users_excluded": True,
            "future_event_direction": "events must occur at or after query",
            "random_seed": 1111,
            "cases_per_day": {day: len(rows) for day, rows in cases.items()},
            "caveat": "No session_id is provided; a documented forward window approximates session attribution.",
        },
        "splits": {
            "train": [
                case
                for day in sorted(train_days)
                for case in cases.get(day, [])
            ],
            "validation": cases.get("2026-06-06", []),
            "test": cases.get("2026-06-07", []),
        },
    }
    benchmark_index = {
        "metadata": {
            "index_version": "benchmark-2026.06.05-v1",
            "dataset_version": "1111-2026-06-01_2026-06-07",
            "schema_fingerprint": schema_fingerprint(data_dir),
            "graph_train_cutoff": TRAIN_CUTOFF.isoformat(sep=" "),
            "graph_builder": "reviewed-bootstrap-fixture",
            "random_seed": 1111,
            "purpose": "Temporal graph/no-graph reranking ablation only",
            "stats": {
                "requested_candidates": len(candidate_ids),
                "materialized_candidates": len(jobs),
                "train_apply_title_snapshots": len(title_snapshots),
                "seed_skill_nodes": len(ontology["skills"]) - len(duty_ontology),
                "duty_occupation_nodes": len(duty_ontology),
                "behavior_query_nodes": len(behavior_graph["query_job"]),
                "behavior_query_job_edges": sum(
                    len(edges) for edges in behavior_graph["query_job"].values()
                ),
                "behavior_query_skill_edges": sum(
                    len(edges) for edges in behavior_graph["query_skill"].values()
                ),
            },
        },
        "locations": load_lookup(
            data_dir / "城市對照表.csv", ["CodeNameA", "CodeNameB", "CodeNameC"]
        ),
        "duties": load_lookup(
            data_dir / "職務對照表.csv",
            ["CodeNameA", "CodeNameB", "CodeNameC", "CodeNameEN"],
        ),
        "skills": ontology["skills"],
        "behavior_graph": behavior_graph,
        "jobs": jobs,
    }
    qrels_output.parent.mkdir(parents=True, exist_ok=True)
    index_output.parent.mkdir(parents=True, exist_ok=True)
    qrels_output.write_text(json.dumps(qrels, ensure_ascii=False), encoding="utf-8")
    index_output.write_text(
        json.dumps(benchmark_index, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(json.dumps(qrels["metadata"], ensure_ascii=False, indent=2))
    print(json.dumps(benchmark_index["metadata"]["stats"], ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build sampled temporal qrels + judged index")
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--ontology", type=Path, default=ONTOLOGY)
    parser.add_argument("--qrels-output", type=Path, default=QRELS_OUTPUT)
    parser.add_argument("--index-output", type=Path, default=INDEX_OUTPUT)
    parser.add_argument(
        "--days",
        default="2026-06-01,2026-06-02,2026-06-03,2026-06-04,2026-06-05,2026-06-06,2026-06-07",
    )
    parser.add_argument(
        "--train-days",
        default="2026-06-01,2026-06-02,2026-06-03,2026-06-04,2026-06-05",
    )
    parser.add_argument("--train-sample-basis-points", type=int, default=30)
    parser.add_argument("--eval-sample-basis-points", type=int, default=200)
    parser.add_argument("--test-day", default="2026-06-07")
    parser.add_argument("--test-sample-bucket-start", type=int, default=200)
    parser.add_argument("--test-sample-basis-points", type=int, default=200)
    parser.add_argument("--candidate-depth", type=int, default=100)
    parser.add_argument("--max-train-per-day", type=int, default=400)
    parser.add_argument("--max-eval-per-day", type=int, default=500)
    parser.add_argument("--max-test-per-day", type=int, default=500)
    parser.add_argument("--window-minutes", type=int, default=30)
    args = parser.parse_args()
    days = {day.strip() for day in args.days.split(",") if day.strip()}
    train_days = {day.strip() for day in args.train_days.split(",") if day.strip()}
    if not train_days.issubset(days):
        raise SystemExit("--train-days must be a subset of --days")
    searches, users, jobs = read_sampled_searches(
        args.data_dir,
        days,
        train_days,
        args.train_sample_basis_points,
        args.eval_sample_basis_points,
        args.candidate_depth,
        args.test_day,
        args.test_sample_bucket_start,
        args.test_sample_basis_points,
    )
    events = read_relevant_events(args.data_dir, days, users, jobs)
    cases = aggregate_cases(
        searches,
        events,
        train_days,
        args.max_train_per_day,
        args.max_eval_per_day,
        args.max_test_per_day,
        args.test_day,
        args.window_minutes,
    )
    write_fixture(
        args.data_dir,
        args.ontology,
        cases,
        args.qrels_output,
        args.index_output,
        train_days,
        args.train_sample_basis_points,
        args.eval_sample_basis_points,
        args.window_minutes,
        args.test_day,
        args.test_sample_bucket_start,
        args.test_sample_basis_points,
    )


if __name__ == "__main__":
    main()
