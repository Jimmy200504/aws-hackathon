#!/usr/bin/env python3
"""Zero-LLM, resumable extraction shared by search indexing and graph builds."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator


EXTRACTOR_VERSION = "deterministic-v1"
RULES_VERSION = "deterministic-extraction-rules-v2"
DEFAULT_CUTOFF = "2026-06-05 23:59:59.999"
PART_SIZE = 1000

STRUCTURED_FIELDS = ("電腦技能資料", "工作技能", "專業證照")
FIELD_SPECS = (
    ("電腦技能資料", 1.0, 0),
    ("工作技能", 1.0, 0),
    ("專業證照", 1.0, 0),
    ("職務名稱", 0.96, 1),
    ("職務小類", 0.90, 2),
    ("職務中類", 0.90, 2),
    ("職務大類", 0.90, 2),
    ("職務內容", 0.84, 3),
)

_SPACE = re.compile(r"\s+")
_TECH_PUNCTUATION = frozenset(".+#/-")
_TOKEN_SEPARATOR = re.compile(r"(?:<br\s*/?>|[\r\n,，、;；|／/]+)", re.IGNORECASE)
_NULL_SURFACES = frozenset({"", "null", "none", "n/a", "na", "無", "不拘", "未填寫", "無要求"})
_NEGATED_BEFORE = re.compile(
    r"(?:不(?:需(?:要)?|必|要求|限|熟悉|精通|具備|會)|無需|毋須|免(?:具備|要求)|"
    r"not\s+required|no\s+need(?:\s+to)?)\s*.{0,12}$",
    re.IGNORECASE,
)
_NEGATED_AFTER = re.compile(r"^.{0,8}(?:非|不是|並非)(?:必要|必須|要求)|^.{0,8}可有可無", re.IGNORECASE)
_REQUIRED = re.compile(r"(?:必須|需具備|需要具備|要求具備|熟悉|精通)", re.IGNORECASE)
_PREFERRED = re.compile(r"(?:加分|尤佳|優先)", re.IGNORECASE)


def normalize_surface(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold().replace("臺", "台")
    text = _SPACE.sub(" ", text).strip()
    return re.sub(r"\s*([.+#/-])\s*", r"\1", text)


def _normalized_with_offsets(value: str) -> tuple[str, list[tuple[int, int]]]:
    expanded: list[tuple[str, int, int]] = []
    for index, character in enumerate(value):
        fragment = unicodedata.normalize("NFKC", character).casefold().replace("臺", "台")
        expanded.extend((item, index, index + 1) for item in fragment)

    collapsed: list[tuple[str, int, int]] = []
    for character, start, end in expanded:
        if character.isspace():
            if collapsed and collapsed[-1][0] != " ":
                collapsed.append((" ", start, end))
            elif collapsed and collapsed[-1][0] == " ":
                previous = collapsed[-1]
                collapsed[-1] = (" ", previous[1], end)
        else:
            collapsed.append((character, start, end))
    while collapsed and collapsed[0][0] == " ":
        collapsed.pop(0)
    while collapsed and collapsed[-1][0] == " ":
        collapsed.pop()

    compact: list[tuple[str, int, int]] = []
    for index, item in enumerate(collapsed):
        character = item[0]
        if character == " ":
            previous = collapsed[index - 1][0] if index else ""
            following = collapsed[index + 1][0] if index + 1 < len(collapsed) else ""
            if previous in _TECH_PUNCTUATION or following in _TECH_PUNCTUATION:
                continue
        compact.append(item)
    return "".join(item[0] for item in compact), [(item[1], item[2]) for item in compact]


def _ascii_boundary_ok(text: str, start: int, end: int, alias: str) -> bool:
    if not alias.isascii():
        return True
    boundary_chars = "abcdefghijklmnopqrstuvwxyz0123456789.+#"
    return not (
        (start > 0 and text[start - 1] in boundary_chars)
        or (end < len(text) and text[end] in boundary_chars)
    )


@dataclass(frozen=True)
class OntologyTerm:
    node_id: str
    label: str
    aliases: tuple[str, ...]
    node_type: str = "Skill"
    source: str = "reviewed_seed"
    standard_code: str | None = None
    standard_version: str | None = None
    source_url: str | None = None
    blocked_phrases: tuple[str, ...] = ()


class ExactAliasMatcher:
    """Longest exact alias matching with seed-first collision handling."""

    def __init__(self, terms: Iterable[OntologyTerm]) -> None:
        materialized = tuple(terms)
        self.terms = {term.node_id: term for term in materialized}
        aliases: dict[str, list[str]] = defaultdict(list)
        for term in materialized:
            if term.node_type != "Skill":
                continue
            for raw_alias in (term.label, *term.aliases):
                alias = normalize_surface(raw_alias)
                if alias and term.node_id not in aliases[alias]:
                    aliases[alias].append(term.node_id)
        self.ambiguous_aliases = frozenset(alias for alias, ids in aliases.items() if len(ids) != 1)
        self.alias_to_node = {
            alias: ids[0] for alias, ids in aliases.items() if len(ids) == 1
        }
        self.aliases = tuple(sorted(self.alias_to_node, key=lambda value: (-len(value), value)))
        self.pattern = re.compile("|".join(re.escape(alias) for alias in self.aliases)) if self.aliases else None

    def exact_node_id(self, surface: str) -> str | None:
        return self.alias_to_node.get(normalize_surface(surface))

    def match_field(self, field: str, source: str, confidence: float) -> list[dict[str, Any]]:
        normalized, offsets = _normalized_with_offsets(source)
        candidates: list[tuple[int, int, str, str]] = []
        for match in self.pattern.finditer(normalized) if self.pattern else ():
            alias = match.group(0)
            node_id = self.alias_to_node[alias]
            if (
                _ascii_boundary_ok(normalized, match.start(), match.end(), alias)
                and not self._is_blocked_phrase(normalized, match.start(), match.end(), self.terms[node_id])
            ):
                candidates.append((match.start(), match.end(), alias, node_id))

        selected: list[tuple[int, int, str, str]] = []
        occupied: set[int] = set()
        for candidate in sorted(candidates, key=lambda item: (-(item[1] - item[0]), item[0], item[2], item[3])):
            span = set(range(candidate[0], candidate[1]))
            if span & occupied:
                continue
            selected.append(candidate)
            occupied.update(span)

        mentions: list[dict[str, Any]] = []
        for start, end, alias, node_id in sorted(selected):
            original_start = offsets[start][0]
            original_end = offsets[end - 1][1]
            if self._is_negated(source, original_start, original_end):
                continue
            term = self.terms[node_id]
            evidence = source[original_start:original_end]
            mentions.append({
                "node_id": node_id,
                "surface": evidence,
                "canonical_skill": term.label,
                "type": "skill",
                "requirement_level": self._requirement_level(source, original_start, original_end),
                "evidence": evidence,
                "evidence_field": field,
                "confidence": confidence,
                "match_rule": "longest_exact_reviewed_alias",
                "ontology_source": term.source,
                **({"standard_code": term.standard_code} if term.standard_code else {}),
                **({"standard_version": term.standard_version} if term.standard_version else {}),
                **({"source_url": term.source_url} if term.source_url else {}),
            })
        return mentions

    @staticmethod
    def _is_blocked_phrase(normalized: str, start: int, end: int, term: OntologyTerm) -> bool:
        for raw_phrase in term.blocked_phrases:
            phrase = normalize_surface(raw_phrase)
            offset = normalized.find(phrase)
            while phrase and offset >= 0:
                if offset <= start and end <= offset + len(phrase):
                    return True
                offset = normalized.find(phrase, offset + 1)
        return False

    @staticmethod
    def _context(source: str, start: int, end: int) -> tuple[str, str, str]:
        clause_start = max(source.rfind(mark, 0, start) for mark in ("。", "；", ";", "\n", "！", "？")) + 1
        endings = [position for mark in ("。", "；", ";", "\n", "！", "？") if (position := source.find(mark, end)) >= 0]
        clause_end = min(endings) if endings else len(source)
        return (
            normalize_surface(source[max(clause_start, start - 24):start]),
            normalize_surface(source[end:min(clause_end, end + 16)]),
            normalize_surface(source[clause_start:clause_end]),
        )

    @classmethod
    def _is_negated(cls, source: str, start: int, end: int) -> bool:
        before, after, _ = cls._context(source, start, end)
        return bool(_NEGATED_BEFORE.search(before) or _NEGATED_AFTER.search(after))

    @classmethod
    def _requirement_level(cls, source: str, start: int, end: int) -> str:
        before, after, clause = cls._context(source, start, end)
        local = f"{before[-20:]} {after[:12]}"
        if _PREFERRED.search(local) or _PREFERRED.search(clause):
            return "preferred"
        if _REQUIRED.search(before[-20:]) or _REQUIRED.search(clause):
            return "required"
        return "mentioned"


def load_ontology(seed_path: Path, icap_path: Path | None = None) -> list[OntologyTerm]:
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    terms = [
        OntologyTerm(
            node_id=node_id,
            label=str(spec["label"]),
            aliases=tuple(str(value) for value in spec.get("aliases", ())),
            node_type=str(spec.get("type", "Skill")),
            blocked_phrases=tuple(str(value) for value in spec.get("blocked_phrases", ())),
        )
        for node_id, spec in sorted(seed.get("skills", {}).items())
    ]
    occupied = {
        normalize_surface(alias)
        for term in terms
        for alias in (term.label, *term.aliases)
        if term.node_type == "Skill"
    }
    if icap_path is None or not icap_path.exists():
        return terms
    document = json.loads(icap_path.read_text(encoding="utf-8"))
    for standard in document.get("standards", []):
        if standard.get("reuse_review", {}).get("serving_approved") is not True:
            continue
        code = str(standard["standard_code"])
        version = str(standard["version"])
        for index, item in enumerate(standard.get("vocabulary", []), 1):
            if item.get("kind") not in {"K", "S"}:
                continue
            label = str(item.get("label", "")).strip()
            if not label:
                continue
            aliases = tuple(str(value) for value in item.get("aliases", ()))
            normalized = [normalize_surface(value) for value in (label, *aliases)]
            # Reviewed seed aliases and collision decisions always win.
            aliases = tuple(value for value in aliases if normalize_surface(value) not in occupied)
            if normalize_surface(label) in occupied:
                continue
            node_id = str(item.get("node_id") or f"icap.{code.lower()}.{item['kind'].lower()}{index:02d}")
            terms.append(OntologyTerm(
                node_id=node_id,
                label=label,
                aliases=aliases,
                source="icap_reviewed",
                standard_code=code,
                standard_version=version,
                source_url=str(standard.get("source_url", "")),
            ))
            occupied.update(value for value in normalized if value)
    return terms


class DutyTaxonomy:
    FIELD_COLUMNS = {
        "職務大類": "CodeNameC",
        "職務中類": "CodeNameB",
        "職務小類": "CodeNameA",
    }

    def __init__(self, rows: Iterable[dict[str, str]]) -> None:
        self.rows = tuple(dict(row) for row in rows)
        self.by_code = {str(row.get("CodeNo", "")): row for row in self.rows if row.get("CodeNo")}
        self.by_field: dict[str, dict[str, dict[str, str]]] = {field: {} for field in self.FIELD_COLUMNS}
        for field, column in self.FIELD_COLUMNS.items():
            candidates: dict[str, list[dict[str, str]]] = defaultdict(list)
            for row in self.rows:
                value = normalize_surface(row.get(column, ""))
                if not value:
                    continue
                if field == "職務大類" and not row.get("CodeNameA") == row.get("CodeNameB") == row.get("CodeNameC"):
                    continue
                if field == "職務中類" and row.get("CodeNameA") != row.get("CodeNameB"):
                    continue
                if field == "職務小類" and normalize_surface(row.get("CodeNameA", "")) != value:
                    continue
                candidates[value].append(row)
            self.by_field[field] = {
                value: sorted(values, key=lambda item: str(item.get("CodeNo", "")))[0]
                for value, values in candidates.items()
            }

    @classmethod
    def from_csv(cls, path: Path) -> "DutyTaxonomy":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return cls(csv.DictReader(handle))

    def map_job(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        resolved: dict[str, dict[str, Any]] = {}
        for raw_code in _split_values(job.get("職務對照表", job.get("duty_code", ""))):
            row = self.by_code.get(raw_code)
            if row:
                resolved[raw_code] = self._occupation(row, "職務對照表")
        for field, column in self.FIELD_COLUMNS.items():
            value = str(job.get(field, "")).strip()
            if normalize_surface(value) in _NULL_SURFACES:
                continue
            row = self.by_field[field].get(normalize_surface(value))
            if row:
                code = str(row["CodeNo"])
                resolved[code] = self._occupation(row, field)
        return [resolved[code] for code in sorted(resolved)]

    @staticmethod
    def _occupation(row: dict[str, str], field: str) -> dict[str, Any]:
        code = str(row["CodeNo"])
        return {
            "node_id": f"duty.{code}",
            "duty_code": code,
            "label": str(row.get("CodeNameA") or row.get("CodeNameB") or row.get("CodeNameC") or code),
            "confidence": 1.0,
            "source": "1111_duty_taxonomy",
            "evidence_field": field,
        }


def _split_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in _TOKEN_SEPARATOR.split(str(value or "")) if item.strip()]


def unknown_structured_surfaces(job: dict[str, Any], matcher: ExactAliasMatcher) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for field in STRUCTURED_FIELDS:
        for surface in _split_values(job.get(field, "")):
            normalized = normalize_surface(surface)
            key = (field, normalized)
            if normalized in _NULL_SURFACES or key in seen or matcher.exact_node_id(surface):
                continue
            seen.add(key)
            result.append({"surface": surface, "normalized_surface": normalized, "evidence_field": field})
    return result


def extract_job(
    job: dict[str, Any],
    matcher: ExactAliasMatcher,
    duty_taxonomy: DutyTaxonomy | None = None,
) -> dict[str, Any]:
    job_id = str(job.get("職缺編號", job.get("job_id", ""))).strip()
    company_id = str(job.get("廠商編號", job.get("company_id", ""))).strip()
    modified_at = str(job.get("職缺最後修改時間", job.get("source_modified_at", ""))).strip()
    if not job_id:
        raise ValueError("missing job ID")
    datetime.fromisoformat(modified_at)

    best: dict[str, tuple[tuple[int, int, str], dict[str, Any]]] = {}
    for field, confidence, priority in FIELD_SPECS:
        source = str(job.get(field, "") or "")
        if normalize_surface(source) in _NULL_SURFACES:
            continue
        for mention in matcher.match_field(field, source, confidence):
            current = best.get(mention["node_id"])
            rank = (priority, -len(mention["evidence"]), mention["evidence_field"])
            if current is None or rank < current[0]:
                best[mention["node_id"]] = (rank, mention)
    mentions = [item[1] for item in sorted(best.values(), key=lambda item: (item[0], item[1]["node_id"]))]
    return {
        "job_id": job_id,
        "company_id": company_id,
        "source_modified_at": modified_at,
        "occupations": duty_taxonomy.map_job(job) if duty_taxonomy else [],
        "mentions": mentions,
        "unknown_surfaces": unknown_structured_surfaces(job, matcher),
        "status": "ok" if mentions else "no_mentions",
        "extractor": EXTRACTOR_VERSION,
    }


@dataclass
class CandidateAggregate:
    surfaces: set[str] = field(default_factory=set)
    support_jobs: int = 0
    companies: set[str] = field(default_factory=set)
    fields: set[str] = field(default_factory=set)


def aggregate_candidates(records: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    aggregates: dict[str, CandidateAggregate] = defaultdict(CandidateAggregate)
    for record in records:
        seen_in_job: set[str] = set()
        for item in record.get("unknown_surfaces", ()):
            normalized = normalize_surface(str(item.get("normalized_surface", item.get("surface", ""))))
            if not normalized:
                continue
            aggregate = aggregates[normalized]
            aggregate.surfaces.add(str(item.get("surface", "")))
            if normalized not in seen_in_job:
                aggregate.support_jobs += 1
                seen_in_job.add(normalized)
            company = str(record.get("company_id", ""))
            if company:
                aggregate.companies.add(company)
            aggregate.fields.add(str(item.get("evidence_field", "")))
    frequency: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for normalized, aggregate in sorted(aggregates.items()):
        row = {
            "candidate_id": "candidate:" + hashlib.sha256(normalized.encode()).hexdigest()[:16],
            "normalized_surface": normalized,
            "surfaces": sorted(aggregate.surfaces),
            "support_jobs": aggregate.support_jobs,
            "support_companies": len(aggregate.companies),
            "evidence_fields": sorted(aggregate.fields - {""}),
            "status": "review_required",
            "serving_eligible": False,
        }
        frequency.append(row)
        if row["support_jobs"] >= 3 and row["support_companies"] >= 2:
            candidates.append(row)
    return candidates, frequency


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iter_jsonl_parts(path: Path) -> Iterator[dict[str, Any]]:
    paths = sorted(path.glob("part-*.jsonl")) if path.is_dir() else [path]
    for part in paths:
        if not part.exists():
            continue
        with part.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise RuntimeError(f"immutable part conflict: {path}")
        return
    path.write_bytes(payload)


def _write_part(root: Path, scope: str, kind: str, index: int, records: list[dict[str, Any]]) -> None:
    payload = b"".join(canonical_json(record) for record in records)
    _write_immutable(root / scope / kind / f"part-{index:06d}.jsonl", payload)


def _write_checkpoint(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical_json(value))
    temporary.replace(path)


def _icap_candidate_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for standard in document.get("standards", []):
        if standard.get("reuse_review", {}).get("serving_approved") is True:
            continue
        for item in standard.get("vocabulary", []):
            if item.get("kind") not in {"K", "S"}:
                continue
            rows.append({
                "standard_code": standard.get("standard_code"),
                "standard_version": standard.get("version"),
                "standard_name": standard.get("name"),
                "source_url": standard.get("source_url"),
                "kind": item.get("kind"),
                "label": item.get("label"),
                "aliases": item.get("aliases", []),
                "status": "reuse_review_required",
                "serving_eligible": False,
            })
    return sorted(rows, key=lambda row: (str(row["standard_code"]), str(row["kind"]), str(row["label"])))


def run_csv_extraction(
    jobs_path: Path,
    duty_path: Path,
    seed_path: Path,
    output_root: Path,
    *,
    cutoff: str = DEFAULT_CUTOFF,
    icap_path: Path | None = None,
    part_size: int = PART_SIZE,
    max_records: int = 0,
) -> dict[str, Any]:
    if part_size < 1:
        raise ValueError("part_size must be positive")
    input_hash = sha256_file(jobs_path)
    ontology_hash = hashlib.sha256(seed_path.read_bytes() + (icap_path.read_bytes() if icap_path and icap_path.exists() else b"")).hexdigest()
    duty_hash = sha256_file(duty_path)
    configuration = {
        "extractor": EXTRACTOR_VERSION,
        "rules_version": RULES_VERSION,
        "rules_hash": hashlib.sha256(RULES_VERSION.encode()).hexdigest(),
        "input_hash": input_hash,
        "ontology_hash": ontology_hash,
        "duty_taxonomy_hash": duty_hash,
        "cutoff": cutoff,
        "part_size": part_size,
    }
    checkpoint_path = output_root / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.exists() else {**configuration, "completed_parts": 0, "processed": 0, "complete": False}
    if any(checkpoint.get(key) != value for key, value in configuration.items()):
        raise RuntimeError("checkpoint input/configuration mismatch")
    completed_parts = int(checkpoint.get("completed_parts", 0))
    resume_processed = int(checkpoint.get("processed", 0))
    cutoff_time = datetime.fromisoformat(cutoff)
    matcher = ExactAliasMatcher(load_ontology(seed_path, icap_path))
    taxonomy = DutyTaxonomy.from_csv(duty_path)
    buffer: list[dict[str, str]] = []
    source_count = 0
    last_committed_count = resume_processed
    stopped_at_limit = False

    def commit(part_index: int, rows: list[dict[str, str]]) -> None:
        latest_accepted: list[dict[str, Any]] = []
        latest_quarantine: list[dict[str, Any]] = []
        cutoff_accepted: list[dict[str, Any]] = []
        cutoff_quarantine: list[dict[str, Any]] = []
        for row in rows:
            try:
                extracted = extract_job(row, matcher, taxonomy)
            except Exception as exc:
                rejected = {
                    "job_id": str(row.get("職缺編號", "")),
                    "reason": type(exc).__name__,
                    "message": str(exc),
                    "extractor": EXTRACTOR_VERSION,
                }
                latest_quarantine.append(rejected)
                cutoff_quarantine.append(rejected)
                continue
            latest_accepted.append(extracted)
            if datetime.fromisoformat(extracted["source_modified_at"]) <= cutoff_time:
                cutoff_accepted.append(extracted)
        for scope, accepted, quarantine in (
            ("latest", latest_accepted, latest_quarantine),
            ("evaluation-cutoff", cutoff_accepted, cutoff_quarantine),
        ):
            _write_part(output_root, scope, "accepted", part_index, accepted)
            _write_part(output_root, scope, "quarantine", part_index, quarantine)

    with jobs_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if max_records and source_count >= max_records:
                stopped_at_limit = True
                break
            source_count += 1
            if source_count <= resume_processed:
                continue
            buffer.append(row)
            if len(buffer) == part_size:
                part_index = completed_parts
                commit(part_index, buffer)
                buffer = []
                completed_parts += 1
                last_committed_count = source_count
                checkpoint = {**configuration, "completed_parts": completed_parts, "processed": last_committed_count, "complete": False}
                _write_checkpoint(checkpoint_path, checkpoint)
    if buffer and not stopped_at_limit:
        part_index = completed_parts
        commit(part_index, buffer)
        completed_parts += 1
        last_committed_count = source_count
        checkpoint = {**configuration, "completed_parts": completed_parts, "processed": last_committed_count, "complete": False}
        _write_checkpoint(checkpoint_path, checkpoint)

    is_complete = not stopped_at_limit
    checkpoint = {**configuration, "completed_parts": completed_parts, "processed": last_committed_count, "complete": is_complete}
    _write_checkpoint(checkpoint_path, checkpoint)
    manifests: dict[str, Any] = {}
    for scope in ("evaluation-cutoff", "latest"):
        (output_root / scope).mkdir(parents=True, exist_ok=True)
        accepted_path = output_root / scope / "accepted"
        quarantine_path = output_root / scope / "quarantine"
        accepted = sum(1 for _ in iter_jsonl_parts(accepted_path))
        quarantine = sum(1 for _ in iter_jsonl_parts(quarantine_path))
        candidates, frequency = aggregate_candidates(iter_jsonl_parts(accepted_path))
        (output_root / scope / "surface-candidates.jsonl").write_bytes(b"".join(canonical_json(row) for row in candidates))
        (output_root / scope / "surface-frequency.jsonl").write_bytes(b"".join(canonical_json(row) for row in frequency))
        icap_candidates = _icap_candidate_rows(icap_path)
        (output_root / scope / "icap-vocabulary-candidates.jsonl").write_bytes(
            b"".join(canonical_json(row) for row in icap_candidates)
        )
        manifest = {
            **configuration,
            "scope": scope,
            "model_id": None,
            "llm_requests": 0,
            "embedding_requests": 0,
            "processed": last_committed_count,
            "eligible": accepted + quarantine,
            "accepted": accepted,
            "quarantine": quarantine,
            "post_cutoff_excluded": last_committed_count - accepted - quarantine if scope == "evaluation-cutoff" else 0,
            "surface_candidates": len(candidates),
            "icap_candidates": len(icap_candidates),
            "complete": is_complete,
        }
        (output_root / scope / "manifest.json").write_bytes(canonical_json(manifest))
        manifests[scope] = manifest
    return manifests


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic 1111 job skill extraction")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--duty-map", type=Path, required=True)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--icap", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--graph-cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--part-size", type=int, default=PART_SIZE)
    parser.add_argument("--max-records", type=int, default=0)
    args = parser.parse_args()
    manifests = run_csv_extraction(
        args.input, args.duty_map, args.seed, args.output,
        cutoff=args.graph_cutoff, icap_path=args.icap,
        part_size=args.part_size, max_records=args.max_records,
    )
    print(json.dumps(manifests, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
