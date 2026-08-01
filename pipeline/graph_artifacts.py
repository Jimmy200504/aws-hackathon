"""Immutable, deterministic Skill Graph artifact and Neptune CSV publisher."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def chunks(records: Iterable[dict[str, Any]], size: int = 1000) -> Iterator[list[dict[str, Any]]]:
    part: list[dict[str, Any]] = []
    for record in records:
        part.append(record)
        if len(part) == size:
            yield part
            part = []
    if part:
        yield part


@dataclass(frozen=True)
class ArtifactFile:
    path: str
    sha256: str
    records: int
    bytes: int


class ArtifactWriter:
    """Writes a run once; identical resume writes are accepted, drift is not."""

    def __init__(self, root: str | Path, run_id: str, *, scope: str | None = None) -> None:
        if not run_id or "/" in run_id or ".." in run_id:
            raise ValueError("invalid run_id")
        if scope is not None and scope not in {"evaluation-cutoff", "latest"}:
            raise ValueError("invalid artifact scope")
        self.root = Path(root) / "runs" / run_id
        if scope:
            self.root /= scope
        self.root.mkdir(parents=True, exist_ok=True)
        self.files: list[ArtifactFile] = []

    def _write_immutable(self, relative: str, payload: bytes, records: int) -> ArtifactFile:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(payload).hexdigest()
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"immutable artifact conflict: {relative}")
        else:
            path.write_bytes(payload)
        artifact = ArtifactFile(relative, digest, records, len(payload))
        self.files.append(artifact)
        return artifact

    def write_parts(self, name: str, records: Iterable[dict[str, Any]], part_size: int = 1000) -> list[ArtifactFile]:
        result: list[ArtifactFile] = []
        for index, part in enumerate(chunks(records, part_size)):
            payload = b"".join(canonical_json(record) for record in part)
            result.append(self._write_immutable(f"{name}/part-{index:06d}.jsonl", payload, len(part)))
        if not result:
            result.append(self._write_immutable(f"{name}/part-000000.jsonl", b"", 0))
        return result

    def write_json(self, relative: str, value: Any) -> ArtifactFile:
        return self._write_immutable(relative, canonical_json(value), 1)

    def write_csv(self, relative: str, rows: list[dict[str, Any]], fieldnames: list[str]) -> ArtifactFile:
        return self.write_csv_rows(relative, rows, fieldnames)

    def write_csv_rows(
        self,
        relative: str,
        rows: Iterable[dict[str, Any]],
        fieldnames: list[str],
    ) -> ArtifactFile:
        """Write a CSV without retaining the complete payload in memory."""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        records = 0
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fieldnames})
                records += 1
        digest = _sha256_path(temporary)
        size = temporary.stat().st_size
        if path.exists():
            if _sha256_path(path) != digest:
                temporary.unlink()
                raise RuntimeError(f"immutable artifact conflict: {relative}")
            temporary.unlink()
        else:
            temporary.replace(path)
        artifact = ArtifactFile(relative, digest, records, size)
        self.files.append(artifact)
        return artifact

    def finalize(self, metadata: dict[str, Any]) -> dict[str, Any]:
        manifest = {
            **metadata,
            "files": [asdict(item) for item in sorted(self.files, key=lambda item: item.path)],
        }
        manifest["manifest_hash"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
        self.write_json("manifest.json", manifest)
        return manifest


def upload_run_to_s3(
    client: Any,
    bucket: str,
    prefix: str,
    writer: ArtifactWriter,
) -> list[str]:
    """Upload immutable run files. Existing unequal objects abort the publish."""
    uploaded: list[str] = []
    run_prefix = prefix.strip("/")
    for path in sorted(item for item in writer.root.rglob("*") if item.is_file()):
        relative = path.relative_to(writer.root).as_posix()
        key = f"{run_prefix}/{writer.root.name}/{relative}" if run_prefix else f"{writer.root.name}/{relative}"
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        try:
            existing = client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:
            error = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if error not in {"404", "NoSuchKey", "NotFound"}:
                raise
        else:
            metadata_digest = existing.get("Metadata", {}).get("sha256")
            if metadata_digest != digest:
                raise RuntimeError(f"immutable S3 artifact conflict: s3://{bucket}/{key}")
            uploaded.append(key)
            continue
        client.put_object(
            Bucket=bucket, Key=key, Body=payload,
            Metadata={"sha256": digest},
            ContentType="application/json" if path.suffix in {".json", ".jsonl"} else "text/csv",
        )
        uploaded.append(key)
    return uploaded


def publish_serving_pointer(
    client: Any,
    bucket: str,
    key: str,
    *,
    run_id: str,
    graph_id: str,
    graph_version: str,
    manifest_hash: str,
    scope: str = "evaluation-cutoff",
) -> None:
    """Write the small mutable pointer only after all release gates pass."""
    payload = canonical_json({
        "run_id": run_id, "graph_id": graph_id, "graph_version": graph_version,
        "manifest_hash": manifest_hash, "scope": scope,
    })
    client.put_object(
        Bucket=bucket, Key=key, Body=payload, ContentType="application/json",
        Metadata={"manifest-sha256": hashlib.sha256(payload).hexdigest()},
    )


NODE_HEADERS = ["~id", "~label", "label:String", "node_type:String", "status:String", "aliases:String[]"]
EDGE_HEADERS = [
    "~id", "~from", "~to", "~label", "weight:Double", "confidence:Double",
    "support_jobs:Int", "support_companies:Int", "evidence:String", "validated:Boolean",
    "requirement_level:String", "evidence_field:String", "rules_version:String",
    "corpus_hash:String", "provenance:String",
]


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_neptune_csv_streaming(
    writer: ArtifactWriter,
    nodes: Iterable[dict[str, Any]],
    edges: Iterable[dict[str, Any]],
    *,
    cutoff: str | None = None,
    enforce_cutoff: bool = True,
) -> tuple[ArtifactFile, ArtifactFile, dict[str, Any]]:
    """Validate and emit Neptune CSV with a disk-backed uniqueness index."""
    descriptor, index_name = tempfile.mkstemp(prefix=".graph-index-", suffix=".sqlite3", dir=writer.root)
    os.close(descriptor)
    index_path = Path(index_name)
    connection = sqlite3.connect(index_path)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    connection.execute("CREATE TABLE edges (id TEXT PRIMARY KEY)")
    node_counts: Counter[str] = Counter()
    edge_counts: Counter[str] = Counter()
    non_job_nodes: set[str] = set()
    node_count = 0
    edge_count = 0
    cutoff_time = datetime.fromisoformat(cutoff) if cutoff else None

    def node_rows() -> Iterator[dict[str, Any]]:
        nonlocal node_count
        for node in nodes:
            if node.get("status", "active") == "candidate":
                raise ValueError("candidate nodes cannot enter Neptune CSV")
            node_id = str(node["id"])
            try:
                connection.execute("INSERT INTO nodes(id) VALUES (?)", (node_id,))
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"duplicate node ID: {node_id}") from exc
            node_type = str(node["type"])
            if node_type != "Job":
                non_job_nodes.add(node_id)
            node_counts[node_type] += 1
            node_count += 1
            if node_count % 50000 == 0:
                connection.commit()
            yield {
                "~id": node_id,
                "~label": node_type,
                "label:String": node.get("label", ""),
                "node_type:String": node_type,
                "status:String": node.get("status", "active"),
                "aliases:String[]": ";".join(sorted(set(node.get("aliases", ())))),
            }

    last_source_id = ""
    last_source_exists = False

    def node_exists(node_id: str) -> bool:
        nonlocal last_source_id, last_source_exists
        if node_id in non_job_nodes:
            return True
        if node_id == last_source_id:
            return last_source_exists
        last_source_id = node_id
        last_source_exists = connection.execute(
            "SELECT 1 FROM nodes WHERE id = ?", (node_id,)
        ).fetchone() is not None
        return last_source_exists

    def edge_rows() -> Iterator[dict[str, Any]]:
        nonlocal edge_count
        for edge in edges:
            edge_id = str(edge["id"])
            source_id = str(edge["source_id"])
            target_id = str(edge["target_id"])
            if not node_exists(source_id) or not node_exists(target_id):
                raise ValueError(f"referential integrity failure: {edge_id}")
            try:
                connection.execute("INSERT INTO edges(id) VALUES (?)", (edge_id,))
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"duplicate edge ID: {edge_id}") from exc
            edge_type = str(edge["type"])
            if cutoff_time and enforce_cutoff and edge_type in {"REQUIRES", "INSTANCE_OF"}:
                modified_at = edge.get("source_modified_at")
                if modified_at and datetime.fromisoformat(str(modified_at)) > cutoff_time:
                    raise ValueError("cutoff graph contains a future Job edge")
            if edge.get("status", "active") == "active" and not edge.get("provenance"):
                raise ValueError(f"active edge has no provenance: {edge_id}")
            if edge_type in {"PREREQUISITE_OF", "SPECIALIZATION_OF"}:
                raise ValueError("directed semantic relations are not publishable")
            edge_counts[edge_type] += 1
            edge_count += 1
            if edge_count % 50000 == 0:
                connection.commit()
            yield {
                "~id": edge_id,
                "~from": source_id,
                "~to": target_id,
                "~label": edge_type,
                "weight:Double": edge.get("weight", 1.0),
                "confidence:Double": edge.get("confidence", 1.0),
                "support_jobs:Int": edge.get("support_jobs", 0),
                "support_companies:Int": edge.get("support_companies", 0),
                "evidence:String": json.dumps(edge.get("evidence", []), ensure_ascii=False, separators=(",", ":")),
                "validated:Boolean": str(bool(edge.get("validated", True))).lower(),
                "requirement_level:String": edge.get("requirement_level", ""),
                "evidence_field:String": edge.get("evidence_field", ""),
                "rules_version:String": edge.get("rules_version", ""),
                "corpus_hash:String": edge.get("corpus_hash", ""),
                "provenance:String": json.dumps(
                    edge.get("provenance", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            }

    try:
        node_file = writer.write_csv_rows("neptune/nodes.csv", node_rows(), NODE_HEADERS)
        connection.commit()
        edge_file = writer.write_csv_rows("neptune/edges.csv", edge_rows(), EDGE_HEADERS)
        connection.commit()
        return node_file, edge_file, {
            "nodes": node_count,
            "edges": edge_count,
            "nodes_by_type": dict(sorted(node_counts.items())),
            "edges_by_type": dict(sorted(edge_counts.items())),
            "referential_integrity": True,
        }
    finally:
        connection.close()
        index_path.unlink(missing_ok=True)


def build_neptune_csv(
    writer: ArtifactWriter,
    nodes: Iterable[dict[str, Any]],
    edges: Iterable[dict[str, Any]],
) -> tuple[ArtifactFile, ArtifactFile]:
    node_file, edge_file, _ = build_neptune_csv_streaming(writer, nodes, edges)
    return node_file, edge_file


def quality_report(
    inputs: int,
    accepted: Iterable[dict[str, Any]],
    quarantine: Iterable[dict[str, Any]],
    nodes: Iterable[dict[str, Any]],
    edges: Iterable[dict[str, Any]],
    cutoff: str,
    *,
    enforce_cutoff: bool = True,
) -> dict[str, Any]:
    accepted_rows, quarantine_rows = list(accepted), list(quarantine)
    node_rows, edge_rows = list(nodes), list(edges)
    if len(accepted_rows) + len(quarantine_rows) != inputs:
        raise ValueError("silent loss: every input must be accepted or quarantined")
    node_ids = [str(node["id"]) for node in node_rows]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("node IDs are not unique")
    node_set = set(node_ids)
    if any(edge["source_id"] not in node_set or edge["target_id"] not in node_set for edge in edge_rows):
        raise ValueError("edge referential integrity failure")
    cutoff_time = datetime.fromisoformat(cutoff)
    for edge in edge_rows:
        if enforce_cutoff and edge.get("type") in {"REQUIRES", "INSTANCE_OF"} and edge.get("source_modified_at"):
            if datetime.fromisoformat(edge["source_modified_at"]) > cutoff_time:
                raise ValueError("cutoff graph contains a future Job edge")
        if edge.get("status", "active") == "active" and not edge.get("provenance"):
            raise ValueError(f"active edge has no provenance: {edge['id']}")
        if edge.get("type") in {"PREREQUISITE_OF", "SPECIALIZATION_OF"}:
            raise ValueError("directed semantic relations are not publishable")
    if any(node.get("status") == "candidate" for node in node_rows):
        raise ValueError("candidate nodes cannot enter the serving graph")
    return {
        "inputs": inputs, "accepted": len(accepted_rows), "quarantine": len(quarantine_rows),
        "nodes": len(node_rows), "edges": len(edge_rows),
        "nodes_by_type": dict(sorted(Counter(node["type"] for node in node_rows).items())),
        "edges_by_type": dict(sorted(Counter(edge["type"] for edge in edge_rows).items())),
        "silent_loss": 0, "referential_integrity": True, "cutoff": cutoff,
    }
