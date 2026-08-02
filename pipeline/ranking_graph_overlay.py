from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


OVERLAY_SCHEMA = "skillweave-ranking-graph-overlay-v1"
_SPACE = re.compile(r"\s+")


def _normalize_query(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).lower().strip().replace("臺", "台")
    value = value.replace("react.js", "reactjs").replace("react js", "reactjs")
    value = value.replace("node js", "node.js").replace("nodejs", "node.js")
    value = value.replace("c sharp", "c#").replace("cplusplus", "c++")
    return _SPACE.sub(" ", value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.strip())


def _active_skill_nodes(
    nodes_path: Path,
    reviewed_ontology_path: Path | None,
) -> dict[str, dict[str, Any]]:
    blocked: dict[str, list[str]] = {}
    if reviewed_ontology_path is not None:
        reviewed = json.loads(reviewed_ontology_path.read_text(encoding="utf-8"))
        blocked = {
            skill_id: list(spec.get("blocked_phrases", []))
            for skill_id, spec in reviewed.get("skills", {}).items()
            if spec.get("blocked_phrases")
        }

    output: dict[str, dict[str, Any]] = {}
    for node in _read_jsonl(nodes_path):
        if (
            node.get("type") != "Skill"
            or node.get("status") != "active"
            or not str(node.get("id", "")).startswith("skill.")
        ):
            continue
        skill_id = str(node["id"])
        spec = {
            "type": "Skill",
            "label": node.get("label", skill_id),
            "aliases": list(node.get("aliases", [])),
            "related": {},
            "provenance": node.get("provenance", {}),
        }
        if blocked.get(skill_id):
            spec["blocked_phrases"] = blocked[skill_id]
        output[skill_id] = spec
    if not output:
        raise ValueError(f"no active Skill nodes found in {nodes_path}")
    return output


def _relation_map(
    relation_edges_path: Path,
    active_skill_ids: set[str],
) -> tuple[dict[str, dict[str, dict[str, Any]]], int]:
    related: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    edge_count = 0
    seen: set[str] = set()
    for edge in _read_jsonl(relation_edges_path):
        if edge.get("type") != "RELATED_TO" or not edge.get("validated", False):
            raise ValueError("ranking overlay accepts only validated RELATED_TO edges")
        source = str(edge["source_id"])
        target = str(edge["target_id"])
        if source not in active_skill_ids or target not in active_skill_ids:
            raise ValueError(f"relation references inactive skill: {source} -> {target}")
        key = "\0".join(sorted((source, target)))
        if key in seen:
            raise ValueError(f"duplicate undirected relation: {source} <-> {target}")
        seen.add(key)
        metadata = {
            "weight": float(edge["weight"]),
            "confidence": float(edge.get("confidence", edge["weight"])),
            "relation_type": "RELATED_TO",
            "edge_id": edge.get("edge_id", edge.get("id")),
            "support_jobs": int(edge["support_jobs"]),
            "support_companies": int(edge["support_companies"]),
            "evidence": list(edge.get("evidence", []))[:3],
            "rules_version": edge.get("rules_version"),
            "corpus_hash": edge.get("corpus_hash"),
        }
        related[source][target] = metadata
        related[target][source] = dict(metadata)
        edge_count += 1
    return dict(related), edge_count


def _rebuild_query_skill_edges(
    qrels: dict[str, Any],
    jobs_by_id: dict[str, dict[str, Any]],
    behavior_graph: dict[str, Any],
) -> None:
    train_cases = qrels.get("splits", {}).get("train", [])
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in train_cases:
        by_day[str(case["day"])].append(case)

    cumulative: dict[str, dict[str, list[int]]] = defaultdict(dict)
    snapshots: dict[str, dict[str, dict[str, list[int]]]] = {}
    for day in sorted(by_day):
        snapshots[day] = {
            query: {skill: list(stats) for skill, stats in edges.items()}
            for query, edges in cumulative.items()
        }
        for case in by_day[day]:
            query = _normalize_query(str(case.get("query", "")))
            if not query:
                continue
            for job_id in case.get("candidates", []):
                job = jobs_by_id.get(str(job_id))
                if job is None:
                    continue
                grade = int(case.get("qrels", {}).get(job_id, 0))
                for skill_id in job.get("skills", []):
                    stats = cumulative[query].setdefault(skill_id, [0, 0, 0])
                    stats[0] += 1
                    stats[1] += int(grade > 0)
                    stats[2] += grade

    behavior_graph["query_skill"] = {
        query: dict(edges) for query, edges in cumulative.items()
    }
    for day, snapshot in behavior_graph.get("snapshots", {}).items():
        snapshot["query_skill"] = snapshots.get(day, {})


def build_ranking_graph_overlay(
    *,
    base_index_path: Path,
    qrels_path: Path,
    graph_manifest_path: Path,
    nodes_path: Path,
    resolved_jobs_path: Path,
    job_edges_path: Path,
    relation_edges_path: Path,
    output_path: Path,
    reviewed_ontology_path: Path | None = None,
) -> dict[str, Any]:
    base = json.loads(base_index_path.read_text(encoding="utf-8"))
    qrels = json.loads(qrels_path.read_text(encoding="utf-8"))
    graph_manifest = json.loads(graph_manifest_path.read_text(encoding="utf-8"))
    if graph_manifest.get("scope") != "evaluation-cutoff":
        raise ValueError("ranking release evaluation requires evaluation-cutoff graph")
    if graph_manifest.get("llm_requests") != 0 or graph_manifest.get("embedding_requests") != 0:
        raise ValueError("ranking graph must have zero offline LLM and embedding requests")

    cutoff = _parse_timestamp(str(graph_manifest["cutoff"]))
    active_skills = _active_skill_nodes(nodes_path, reviewed_ontology_path)
    related, relation_count = _relation_map(
        relation_edges_path, set(active_skills)
    )
    for skill_id, neighbors in related.items():
        active_skills[skill_id]["related"] = neighbors

    # Duty aliases remain those of the frozen fixture; deterministic duty
    # assignment below replaces every Job -> Occupation edge.
    preserved_duties = {
        node_id: spec
        for node_id, spec in base.get("skills", {}).items()
        if node_id.startswith("duty.")
    }
    base["skills"] = {**preserved_duties, **active_skills}

    jobs_by_id = {str(job["id"]): job for job in base.get("jobs", [])}
    for job in jobs_by_id.values():
        job["graph_eligible"] = False
        job["graph_source"] = "deterministic_v2_cutoff_absent"
        job["graph_source_time"] = ""
        job["skills"] = []
        job["skill_evidence"] = {}
        job["skill_confidence"] = {}
        job["skill_provenance"] = {}

    found_jobs: set[str] = set()
    for resolved in _read_jsonl(resolved_jobs_path):
        job_id = str(resolved["job_id"])
        job = jobs_by_id.get(job_id)
        if job is None:
            continue
        modified = _parse_timestamp(str(resolved["source_modified_at"]))
        if modified > cutoff:
            raise ValueError(f"cutoff graph contains future job {job_id}: {modified}")
        found_jobs.add(job_id)
        job["graph_eligible"] = True
        job["graph_source"] = "deterministic_v2_evaluation_cutoff"
        job["graph_source_time"] = resolved["source_modified_at"]

    published_job_edges = 0
    for edge in _read_jsonl(job_edges_path):
        source = str(edge.get("source_id", ""))
        if not source.startswith("job:"):
            continue
        job_id = source.removeprefix("job:")
        if job_id not in found_jobs:
            continue
        edge_type = edge.get("type")
        target = str(edge.get("target_id", ""))
        if edge_type == "REQUIRES":
            if target not in active_skills:
                raise ValueError(f"REQUIRES references inactive skill {target}")
        elif edge_type == "INSTANCE_OF":
            if target not in preserved_duties:
                raise ValueError(f"INSTANCE_OF references unknown duty {target}")
        else:
            raise ValueError(f"unsupported job edge type: {edge_type}")
        job = jobs_by_id[job_id]
        job["skills"].append(target)
        evidence = list(edge.get("evidence", []))
        job["skill_evidence"][target] = evidence[0] if evidence else ""
        job["skill_confidence"][target] = float(edge.get("confidence", 1.0))
        job["skill_provenance"][target] = edge.get("provenance", {})
        published_job_edges += 1

    for job in jobs_by_id.values():
        job["skills"] = sorted(set(job["skills"]))

    _rebuild_query_skill_edges(qrels, jobs_by_id, base.setdefault("behavior_graph", {}))

    graph_manifest_hash = sha256_file(graph_manifest_path)
    stats = base.setdefault("metadata", {}).setdefault("stats", {})
    stats.update(
        {
            "deterministic_cutoff_jobs": len(found_jobs),
            "deterministic_cutoff_job_edges": published_job_edges,
            "active_skill_nodes": len(active_skills),
            "published_related_to_edges": relation_count,
            "candidate_nodes_published": 0,
            "future_job_violations": 0,
        }
    )
    base["metadata"].update(
        {
            "index_version": f"{base['metadata']['index_version']}-deterministic-v2-cutoff",
            "graph_builder": "deterministic-v1-rules-v2",
            "graph_version": graph_manifest["graph_version"],
            "graph_manifest_hash": graph_manifest_hash,
            "graph_overlay": {
                "schema": OVERLAY_SCHEMA,
                "base_index_sha256": sha256_file(base_index_path),
                "qrels_sha256": sha256_file(qrels_path),
                "graph_manifest_path": str(graph_manifest_path),
                "graph_manifest_sha256": graph_manifest_hash,
                "declared_graph_manifest_hash": graph_manifest.get("manifest_hash"),
                "nodes_sha256": sha256_file(nodes_path),
                "resolved_jobs_sha256": sha256_file(resolved_jobs_path),
                "job_edges_sha256": sha256_file(job_edges_path),
                "relation_edges_sha256": sha256_file(relation_edges_path),
                "scope": "evaluation-cutoff",
                "model_retrained": False,
                "qrels_or_split_changed": False,
                "offline_llm_requests": 0,
                "embedding_requests": 0,
            },
        }
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(base, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(output_path)
    sidecar = {
        "schema": OVERLAY_SCHEMA,
        "index_path": str(output_path),
        "index_sha256": sha256_file(output_path),
        "metadata": base["metadata"],
    }
    sidecar_path = output_path.with_suffix(".manifest.json")
    sidecar_temporary = sidecar_path.with_suffix(sidecar_path.suffix + ".tmp")
    sidecar_temporary.write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sidecar_temporary.replace(sidecar_path)
    return base["metadata"]
