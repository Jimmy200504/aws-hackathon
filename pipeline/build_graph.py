"""Exact-alias resolution, statistical relations, and immutable graph export."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from itertools import chain
from pathlib import Path
from typing import Any, Iterable, Iterator

from pipeline.deterministic_extract import EXTRACTOR_VERSION, load_ontology, sha256_file
from pipeline.graph_artifacts import ArtifactWriter, build_neptune_csv_streaming
from pipeline.skill_graph import CanonicalNode, ExactEntityResolver, publish_relations, relation_candidates

SCOPES = ("evaluation-cutoff", "latest")
RESOLVER_VERSION = "deterministic-exact-resolver-v1"


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    paths = sorted(path.glob("part-*.jsonl")) if path.is_dir() else [path]
    for candidate in paths:
        if not candidate.exists():
            continue
        with candidate.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return list(iter_jsonl(path))


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        for row in rows:
            handle.write((json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode())


def seed_nodes(path: Path, icap_path: Path | None = None) -> list[CanonicalNode]:
    return [
        CanonicalNode(
            term.node_id,
            term.node_type,
            term.label,
            term.aliases,
            provenance={
                "kind": term.source,
                **({"standard_code": term.standard_code} if term.standard_code else {}),
                **({"standard_version": term.standard_version} if term.standard_version else {}),
                **({"source_url": term.source_url} if term.source_url else {}),
            },
        )
        for term in load_ontology(path, icap_path)
        if term.node_type == "Skill"
    ]


def _json_line(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(_json_line(value))
    temporary.replace(path)


def _resolve_input_identity(input_path: Path) -> str:
    extraction_manifest = input_path.parent / "manifest.json"
    if input_path.is_dir() and extraction_manifest.is_file():
        return hashlib.sha256(extraction_manifest.read_bytes()).hexdigest()
    if input_path.is_file():
        return sha256_file(input_path)
    digest = hashlib.sha256()
    for part in sorted(input_path.glob("part-*.jsonl")):
        digest.update(part.name.encode())
        digest.update(sha256_file(part).encode())
    return digest.hexdigest()


def resolve_stage(
    input_path: Path,
    seed_path: Path,
    output_dir: Path,
    icap_path: Path | None = None,
    *,
    part_size: int = 1000,
    max_records: int = 0,
) -> None:
    """Resolve one scope with bounded memory and resumable 1,000-row commits."""
    if part_size < 1:
        raise ValueError("part_size must be positive")
    ontology_nodes = seed_nodes(seed_path, icap_path)
    resolver = ExactEntityResolver(ontology_nodes)
    ontology_rows = [{
        "id": node.node_id,
        "type": node.node_type,
        "label": node.label,
        "aliases": list(node.aliases),
        "status": node.status,
        "provenance": node.provenance or {"kind": "reviewed_seed"},
    } for node in ontology_nodes]
    ontology_hash = hashlib.sha256(
        seed_path.read_bytes() + (icap_path.read_bytes() if icap_path and icap_path.exists() else b"")
    ).hexdigest()
    configuration = {
        "resolver": RESOLVER_VERSION,
        "input_identity": _resolve_input_identity(input_path),
        "ontology_hash": ontology_hash,
        "part_size": part_size,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "checkpoint.json"
    final_paths = {
        "nodes": output_dir / "nodes.jsonl",
        "edges": output_dir / "job-skill-edges.jsonl",
        "jobs": output_dir / "jobs.jsonl",
        "resolutions": output_dir / "resolutions.jsonl",
    }
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.exists() else {
        **configuration,
        "processed": 0,
        "complete": False,
        "offsets": {name: 0 for name in ("job_nodes", "edges", "jobs", "resolutions")},
        "occupations": {},
    }
    if any(checkpoint.get(key) != value for key, value in configuration.items()):
        raise RuntimeError("resolver checkpoint input/configuration mismatch")
    if checkpoint.get("complete") is True:
        if not all(path.is_file() for path in final_paths.values()):
            raise RuntimeError("completed resolver checkpoint has missing outputs")
        return

    partial_paths = {
        name: output_dir / f".{name}.partial.jsonl"
        for name in ("job_nodes", "edges", "jobs", "resolutions")
    }
    offsets = {name: int(checkpoint.get("offsets", {}).get(name, 0)) for name in partial_paths}
    handles: dict[str, Any] = {}
    for name, path in partial_paths.items():
        handle = path.open("r+b") if path.exists() else path.open("w+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() < offsets[name]:
            handle.close()
            raise RuntimeError(f"resolver partial output is shorter than checkpoint: {path}")
        handle.truncate(offsets[name])
        handle.seek(offsets[name])
        handles[name] = handle

    occupations: dict[str, dict[str, Any]] = dict(checkpoint.get("occupations", {}))
    buffers: dict[str, list[bytes]] = {name: [] for name in partial_paths}
    pending_occupations: dict[str, dict[str, Any]] = {}
    buffered_jobs = 0
    resume_processed = int(checkpoint.get("processed", 0))
    committed = resume_processed

    def commit(processed: int) -> None:
        nonlocal committed, buffered_jobs
        for name, handle in handles.items():
            for payload in buffers[name]:
                handle.write(payload)
            handle.flush()
            offsets[name] = handle.tell()
            buffers[name].clear()
        occupations.update(pending_occupations)
        pending_occupations.clear()
        committed = processed
        buffered_jobs = 0
        _atomic_json(checkpoint_path, {
            **configuration,
            "processed": committed,
            "complete": False,
            "offsets": offsets,
            "occupations": occupations,
        })

    source_count = 0
    stopped_at_limit = False
    try:
        for row in iter_jsonl(input_path):
            if max_records and source_count >= max_records:
                stopped_at_limit = True
                break
            source_count += 1
            if source_count <= resume_processed:
                continue
            job_id = str(row["job_id"])
            source_id = f"job:{job_id}"
            buffers["job_nodes"].append(_json_line({
                "id": source_id,
                "type": "Job",
                "label": job_id,
                "status": "active",
                "source_modified_at": row.get("source_modified_at", ""),
                "provenance": {"kind": EXTRACTOR_VERSION},
            }))
            skill_ids: list[str] = []
            skill_evidence: dict[str, str] = {}
            job_edges: dict[str, dict[str, Any]] = {}
            for mention in row.get("mentions", []):
                resolution = resolver.resolve(str(mention.get("surface", "")), "Skill")
                buffers["resolutions"].append(_json_line(resolution.__dict__))
                if resolution.node_id is None:
                    continue
                claimed = str(mention.get("node_id", resolution.node_id))
                if claimed != resolution.node_id:
                    buffers["resolutions"].append(_json_line({
                        "surface": mention.get("surface", ""),
                        "decision": "CLAIMED_NODE_MISMATCH",
                        "claimed_node_id": claimed,
                        "node_id": resolution.node_id,
                    }))
                    continue
                skill_ids.append(resolution.node_id)
                skill_evidence[resolution.node_id] = str(mention.get("evidence", ""))
                raw_edge = f"{source_id}\0REQUIRES\0{resolution.node_id}"
                edge_id = "requires:" + hashlib.sha256(raw_edge.encode()).hexdigest()[:20]
                job_edges[edge_id] = {
                    "id": edge_id,
                    "source_id": source_id,
                    "target_id": resolution.node_id,
                    "type": "REQUIRES",
                    "weight": float(mention.get("confidence", 0)),
                    "confidence": float(mention.get("confidence", 0)),
                    "requirement_level": mention.get("requirement_level", "mentioned"),
                    "evidence": [mention.get("evidence", "")],
                    "evidence_field": mention.get("evidence_field", ""),
                    "source_modified_at": row.get("source_modified_at", ""),
                    "provenance": {
                        "kind": "deterministic_exact_alias",
                        "extractor": EXTRACTOR_VERSION,
                        "ontology_source": mention.get("ontology_source", "reviewed_seed"),
                    },
                    "validated": True,
                }

            for occupation in row.get("occupations", []):
                duty_code = str(occupation.get("duty_code", "")).strip()
                occupation_id = str(occupation.get("node_id", f"duty.{duty_code}"))
                if not duty_code or occupation_id != f"duty.{duty_code}":
                    continue
                pending_occupations[occupation_id] = {
                    "id": occupation_id,
                    "type": "Occupation",
                    "label": str(occupation.get("label", duty_code)),
                    "aliases": [],
                    "status": "active",
                    "duty_code": duty_code,
                    "provenance": {"kind": "1111_duty_taxonomy"},
                }
                raw_edge = f"{source_id}\0INSTANCE_OF\0{occupation_id}"
                edge_id = "instance:" + hashlib.sha256(raw_edge.encode()).hexdigest()[:20]
                job_edges[edge_id] = {
                    "id": edge_id,
                    "source_id": source_id,
                    "target_id": occupation_id,
                    "type": "INSTANCE_OF",
                    "weight": 1.0,
                    "confidence": 1.0,
                    "evidence": [occupation.get("label", "")],
                    "evidence_field": occupation.get("evidence_field", ""),
                    "source_modified_at": row.get("source_modified_at", ""),
                    "provenance": {"kind": "1111_duty_taxonomy", "duty_code": duty_code},
                    "validated": True,
                }
            buffers["edges"].extend(_json_line(job_edges[key]) for key in sorted(job_edges))
            buffers["jobs"].append(_json_line({
                "job_id": job_id,
                "company_id": str(row.get("company_id", "")),
                "skills": sorted(set(skill_ids)),
                "skill_evidence": dict(sorted(skill_evidence.items())),
                "source_modified_at": row.get("source_modified_at", ""),
            }))
            buffered_jobs += 1
            if buffered_jobs == part_size:
                commit(source_count)
        if buffered_jobs and not stopped_at_limit:
            commit(source_count)
    finally:
        for handle in handles.values():
            handle.close()

    if stopped_at_limit:
        return
    if source_count < resume_processed:
        raise RuntimeError("resolver input is shorter than checkpoint")

    nodes_temporary = final_paths["nodes"].with_suffix(".tmp")
    with nodes_temporary.open("wb") as target:
        for row in sorted(ontology_rows, key=lambda item: str(item["id"])):
            target.write(_json_line(row))
        for occupation_id in sorted(occupations):
            target.write(_json_line(occupations[occupation_id]))
        with partial_paths["job_nodes"].open("rb") as source:
            shutil.copyfileobj(source, target, length=1024 * 1024)
    nodes_temporary.replace(final_paths["nodes"])
    for name, final_name in (("edges", "edges"), ("jobs", "jobs"), ("resolutions", "resolutions")):
        temporary = final_paths[final_name].with_suffix(".tmp")
        if temporary.exists():
            temporary.unlink()
        os.link(partial_paths[name], temporary)
        temporary.replace(final_paths[final_name])
    _atomic_json(checkpoint_path, {
        **configuration,
        "processed": committed,
        "complete": True,
        "offsets": offsets,
        "occupations": occupations,
    })
    for path in partial_paths.values():
        path.unlink(missing_ok=True)


def relations_stage(jobs_path: Path, output_dir: Path) -> None:
    candidates = relation_candidates(iter_jsonl(jobs_path))
    accepted, rejected = publish_relations(candidates)
    write_jsonl(output_dir / "relation-candidates.jsonl", (row.__dict__ for row in candidates))
    write_jsonl(output_dir / "relation-edges.jsonl", ({
        **row.__dict__,
        "id": row.edge_id,
        "type": row.relation_type,
        "provenance": {
            "kind": "full_corpus_cooccurrence",
            "rules_version": row.rules_version,
            "corpus_hash": row.corpus_hash,
        },
        "validated": True,
    } for row in accepted))
    write_jsonl(output_dir / "relation-rejections.jsonl", rejected)


def export_stage(args: argparse.Namespace) -> dict[str, Any]:
    scope = str(getattr(args, "scope", "evaluation-cutoff"))
    writer = ArtifactWriter(args.output, args.run_id, scope=scope)
    accepted_files = writer.write_parts("extraction/accepted", iter_jsonl(args.accepted))
    quarantine_files = writer.write_parts("extraction/quarantine", iter_jsonl(args.quarantine))
    surface_candidate_files = writer.write_parts(
        "review/surface-candidates",
        iter_jsonl(args.surface_candidates) if getattr(args, "surface_candidates", None) else (),
    )
    writer.write_parts(
        "review/surface-frequency",
        iter_jsonl(args.surface_frequency) if getattr(args, "surface_frequency", None) else (),
    )
    icap_candidate_files = writer.write_parts(
        "review/icap-vocabulary-candidates",
        iter_jsonl(args.icap_candidates) if getattr(args, "icap_candidates", None) else (),
    )
    writer.write_parts("nodes", iter_jsonl(args.nodes))
    writer.write_parts("edges", chain(iter_jsonl(args.job_edges), iter_jsonl(args.relation_edges)))

    accepted_count = sum(item.records for item in accepted_files)
    quarantine_count = sum(item.records for item in quarantine_files)
    if accepted_count + quarantine_count != int(args.input_count):
        raise ValueError("silent loss: every input must be accepted or quarantined")
    _, _, graph_stats = build_neptune_csv_streaming(
        writer,
        iter_jsonl(args.nodes),
        chain(iter_jsonl(args.job_edges), iter_jsonl(args.relation_edges)),
        cutoff=args.cutoff,
        enforce_cutoff=scope == "evaluation-cutoff",
    )
    report = {
        "inputs": int(args.input_count),
        "accepted": accepted_count,
        "quarantine": quarantine_count,
        **graph_stats,
        "silent_loss": 0,
        "cutoff": args.cutoff,
    }
    writer.write_json("quality-report.json", report)
    metadata = {
        "run_id": args.run_id,
        "scope": scope,
        "cutoff": args.cutoff,
        "graph_version": args.graph_version,
        "extractor": EXTRACTOR_VERSION,
        "model_id": None,
        "llm_requests": 0,
        "embedding_requests": 0,
        "ontology_hash": getattr(args, "ontology_hash", ""),
        "rules_hash": getattr(args, "rules_hash", ""),
        "input_hash": getattr(args, "input_hash", ""),
        "accepted": accepted_count,
        "quarantine": quarantine_count,
        "eligible": int(args.input_count),
        "default_serving": scope == "evaluation-cutoff",
        "candidate_nodes_published": 0,
        "surface_candidates": sum(item.records for item in surface_candidate_files),
        "icap_candidates": sum(item.records for item in icap_candidate_files),
    }
    return writer.finalize(metadata)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="stage", required=True)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--input", type=Path)
    resolve.add_argument("--input-root", type=Path)
    resolve.add_argument("--seed", type=Path, required=True)
    resolve.add_argument("--icap", type=Path)
    resolve.add_argument("--output", type=Path)
    resolve.add_argument("--output-root", type=Path)
    resolve.add_argument("--all-scopes", action="store_true")
    relations = subparsers.add_parser("relations")
    relations.add_argument("--jobs", type=Path)
    relations.add_argument("--jobs-root", type=Path)
    relations.add_argument("--output", type=Path)
    relations.add_argument("--output-root", type=Path)
    relations.add_argument("--all-scopes", action="store_true")
    export = subparsers.add_parser("export")
    export.add_argument("--nodes", type=Path)
    export.add_argument("--job-edges", type=Path)
    export.add_argument("--relation-edges", type=Path)
    export.add_argument("--accepted", type=Path)
    export.add_argument("--quarantine", type=Path)
    export.add_argument("--surface-candidates", type=Path)
    export.add_argument("--surface-frequency", type=Path)
    export.add_argument("--icap-candidates", type=Path)
    export.add_argument("--extraction-root", type=Path)
    export.add_argument("--resolved-root", type=Path)
    export.add_argument("--relations-root", type=Path)
    export.add_argument("--all-scopes", action="store_true")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--run-id", required=True)
    export.add_argument("--scope", choices=("evaluation-cutoff", "latest"), default="evaluation-cutoff")
    export.add_argument("--graph-version", required=True)
    export.add_argument("--cutoff", required=True)
    export.add_argument("--input-count", type=int)
    export.add_argument("--ontology-hash", default="")
    export.add_argument("--rules-hash", default="")
    export.add_argument("--input-hash", default="")
    args = parser.parse_args()
    if args.stage == "resolve":
        if args.all_scopes:
            if not args.input_root or not args.output_root:
                parser.error("resolve --all-scopes requires --input-root and --output-root")
            for scope in SCOPES:
                resolve_stage(args.input_root / scope / "accepted", args.seed, args.output_root / scope, args.icap)
        elif args.input and args.output:
            resolve_stage(args.input, args.seed, args.output, args.icap)
        else:
            parser.error("resolve requires --input and --output")
    elif args.stage == "relations":
        if args.all_scopes:
            if not args.jobs_root or not args.output_root:
                parser.error("relations --all-scopes requires --jobs-root and --output-root")
            for scope in SCOPES:
                relations_stage(args.jobs_root / scope / "jobs.jsonl", args.output_root / scope)
        elif args.jobs and args.output:
            relations_stage(args.jobs, args.output)
        else:
            parser.error("relations requires --jobs and --output")
    else:
        if args.all_scopes:
            if not args.extraction_root or not args.resolved_root or not args.relations_root:
                parser.error("export --all-scopes requires extraction/resolved/relations roots")
            for scope in SCOPES:
                extraction_manifest = json.loads((args.extraction_root / scope / "manifest.json").read_text(encoding="utf-8"))
                scoped = argparse.Namespace(
                    nodes=args.resolved_root / scope / "nodes.jsonl",
                    job_edges=args.resolved_root / scope / "job-skill-edges.jsonl",
                    relation_edges=args.relations_root / scope / "relation-edges.jsonl",
                    accepted=args.extraction_root / scope / "accepted",
                    quarantine=args.extraction_root / scope / "quarantine",
                    surface_candidates=args.extraction_root / scope / "surface-candidates.jsonl",
                    surface_frequency=args.extraction_root / scope / "surface-frequency.jsonl",
                    icap_candidates=args.extraction_root / scope / "icap-vocabulary-candidates.jsonl",
                    output=args.output,
                    run_id=args.run_id,
                    scope=scope,
                    graph_version=f"{args.graph_version}-{scope}",
                    cutoff=args.cutoff,
                    input_count=int(extraction_manifest["eligible"]),
                    ontology_hash=extraction_manifest.get("ontology_hash", ""),
                    rules_hash=extraction_manifest.get("rules_hash", ""),
                    input_hash=extraction_manifest.get("input_hash", ""),
                )
                export_stage(scoped)
        elif all((args.nodes, args.job_edges, args.relation_edges, args.accepted, args.quarantine, args.input_count is not None)):
            export_stage(args)
        else:
            parser.error("export requires single-scope paths or --all-scopes roots")


if __name__ == "__main__":
    main()
