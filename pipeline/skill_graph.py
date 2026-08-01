"""Deterministic exact resolution and corpus-statistical Skill Graph edges."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from pipeline.deterministic_extract import normalize_surface


RELATION_RULES_VERSION = "statistical-related-to-v1"
COLLISION_GROUPS = (
    frozenset(("java", "javascript")),
    frozenset(("c", "c++")),
    frozenset(("c", "c#")),
    frozenset(("react", "next.js")),
)


def is_forbidden_merge(left: str, right: str) -> bool:
    return frozenset((normalize_surface(left), normalize_surface(right))) in COLLISION_GROUPS


def stable_node_id(node_type: str, label: str) -> str:
    normalized = normalize_surface(label)
    digest = hashlib.sha256(f"{node_type.lower()}\0{normalized}".encode()).hexdigest()[:16]
    return f"{node_type.lower()}:{digest}"


@dataclass(frozen=True)
class CanonicalNode:
    node_id: str
    node_type: str
    label: str
    aliases: tuple[str, ...] = ()
    status: str = "active"
    provenance: dict[str, Any] | None = None


@dataclass(frozen=True)
class Resolution:
    surface: str
    normalized_surface: str
    node_id: str | None
    decision: str
    confidence: float
    status: str


class ExactEntityResolver:
    """Resolve only unique reviewed aliases; unknowns never become serving nodes."""

    def __init__(self, nodes: Iterable[CanonicalNode]) -> None:
        self.nodes = {node.node_id: node for node in nodes}
        aliases: dict[str, set[str]] = defaultdict(set)
        for node in self.nodes.values():
            for alias in (node.label, *node.aliases):
                if normalized := normalize_surface(alias):
                    aliases[normalized].add(node.node_id)
        self.aliases = aliases

    def resolve(self, surface: str, node_type: str = "Skill") -> Resolution:
        normalized = normalize_surface(surface)
        exact = sorted(
            node_id for node_id in self.aliases.get(normalized, ())
            if self.nodes[node_id].node_type == node_type
        )
        if len(exact) == 1:
            return Resolution(surface, normalized, exact[0], "EXACT_REVIEWED_ALIAS", 1.0, "active")
        if len(exact) > 1:
            return Resolution(surface, normalized, None, "AMBIGUOUS_REVIEWED_ALIAS", 0.0, "candidate")
        return Resolution(surface, normalized, None, "UNKNOWN_SURFACE", 0.0, "candidate")


# Compatibility name for local callers; behavior is intentionally exact-only.
EntityResolver = ExactEntityResolver


def map_occupation(
    job: dict[str, Any], duty_lookup: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    """Historical-pilot compatibility wrapper using exact duty lookup only."""
    values: list[str] = []
    for field in ("職務對照表", "職務分類", "職務大類", "職務中類", "職務小類"):
        value = job.get(field, "")
        values.extend(value if isinstance(value, list) else str(value).replace("，", ",").split(","))
    resolved: dict[str, dict[str, Any]] = {}
    for raw in values:
        surface = str(raw).strip()
        item = duty_lookup.get(surface)
        if not item:
            continue
        code = str(item.get("duty_code", item.get("CodeNo", "")))
        if code:
            resolved[code] = {
                "node_id": f"duty.{code}",
                "duty_code": code,
                "label": item.get("label", item.get("CodeNameA", surface)),
                "confidence": 1.0,
                "source": "1111_duty_taxonomy",
            }
    return [resolved[code] for code in sorted(resolved)]


@dataclass(frozen=True)
class RelationCandidate:
    source_id: str
    target_id: str
    support_jobs: int
    support_companies: int
    lift: float
    npmi: float
    statistical_confidence: float
    evidence: tuple[Any, ...]
    rules_version: str
    corpus_hash: str


@dataclass(frozen=True)
class PublishedRelation:
    edge_id: str
    source_id: str
    target_id: str
    relation_type: str
    weight: float
    confidence: float
    support_jobs: int
    support_companies: int
    evidence: tuple[Any, ...]
    rules_version: str
    corpus_hash: str


def _normalized_confidence(npmi: float, lift: float, support_jobs: int) -> float:
    normalized_npmi = max(0.0, min(1.0, (npmi - 0.15) / 0.85))
    normalized_lift = max(0.0, min(1.0, (lift - 2.0) / 8.0))
    return round(
        0.6 * normalized_npmi
        + 0.3 * normalized_lift
        + 0.1 * min(support_jobs / 100.0, 1.0),
        6,
    )


def _grounded_pair_evidence(row: dict[str, Any], source: str, target: str) -> dict[str, str] | None:
    evidence = row.get("skill_evidence", {})
    if not isinstance(evidence, dict):
        return None
    left, right = str(evidence.get(source, "")), str(evidence.get(target, ""))
    if not left or not right:
        return None
    return {"job_id": str(row.get("job_id", "")), "source_evidence": left, "target_evidence": right}


def relation_candidates(
    jobs: Iterable[dict[str, Any]],
    *,
    min_jobs: int = 20,
    min_companies: int = 5,
    min_lift: float = 2.0,
    min_npmi: float = 0.15,
    neighbor_cap: int = 20,
) -> list[RelationCandidate]:
    total = 0
    skill_jobs: Counter[str] = Counter()
    pair_jobs: Counter[tuple[str, str]] = Counter()
    pair_companies: dict[tuple[str, str], set[str]] = defaultdict(set)
    pair_evidence: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    corpus_digest = hashlib.sha256()
    for row in jobs:
        total += 1
        skills = sorted(set(str(skill) for skill in row.get("skills", ()) if str(skill)))
        canonical = json.dumps({
            "job_id": str(row.get("job_id", "")),
            "company_id": str(row.get("company_id", "")),
            "skills": skills,
            "skill_evidence": row.get("skill_evidence", {}),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        corpus_digest.update(canonical.encode())
        corpus_digest.update(b"\n")
        skill_jobs.update(skills)
        for index, source in enumerate(skills):
            for target in skills[index + 1:]:
                pair = (source, target)
                pair_jobs[pair] += 1
                company_id = str(row.get("company_id", ""))
                if company_id:
                    pair_companies[pair].add(company_id)
                grounded = _grounded_pair_evidence(row, source, target)
                if grounded and len(pair_evidence[pair]) < 3:
                    pair_evidence[pair].append(grounded)
    if total == 0:
        return []
    corpus_hash = corpus_digest.hexdigest()
    eligible: list[RelationCandidate] = []
    for (source, target), support in pair_jobs.items():
        companies = len(pair_companies[(source, target)])
        if support < min_jobs or companies < min_companies:
            continue
        p_xy = support / total
        p_x, p_y = skill_jobs[source] / total, skill_jobs[target] / total
        lift = p_xy / max(1e-12, p_x * p_y)
        pmi = math.log(max(1e-12, lift))
        npmi = pmi / -math.log(p_xy) if p_xy < 1 else 1.0
        if lift < min_lift or npmi < min_npmi:
            continue
        eligible.append(RelationCandidate(
            source_id=source,
            target_id=target,
            support_jobs=support,
            support_companies=companies,
            lift=round(lift, 8),
            npmi=round(npmi, 8),
            statistical_confidence=_normalized_confidence(npmi, lift, support),
            evidence=tuple(pair_evidence[(source, target)]),
            rules_version=RELATION_RULES_VERSION,
            corpus_hash=corpus_hash,
        ))

    by_node: dict[str, list[RelationCandidate]] = defaultdict(list)
    for candidate in eligible:
        by_node[candidate.source_id].append(candidate)
        by_node[candidate.target_id].append(candidate)
    selected: Counter[tuple[str, str]] = Counter()
    for values in by_node.values():
        for item in sorted(
            values,
            key=lambda value: (
                -value.statistical_confidence,
                -value.npmi,
                -value.lift,
                -value.support_jobs,
                value.source_id,
                value.target_id,
            ),
        )[:neighbor_cap]:
            selected[(item.source_id, item.target_id)] += 1
    return sorted(
        (item for item in eligible if selected[(item.source_id, item.target_id)] == 2),
        key=lambda item: (item.source_id, item.target_id),
    )


def publish_relations(
    candidates: Iterable[RelationCandidate],
    *,
    degree_cap: int = 20,
) -> tuple[list[PublishedRelation], list[dict[str, Any]]]:
    accepted: list[PublishedRelation] = []
    rejected: list[dict[str, Any]] = []
    degree: Counter[str] = Counter()
    ordered = sorted(
        candidates,
        key=lambda item: (
            -item.statistical_confidence,
            item.source_id,
            item.target_id,
        ),
    )
    for candidate in ordered:
        if degree[candidate.source_id] >= degree_cap or degree[candidate.target_id] >= degree_cap:
            rejected.append({
                "source_id": candidate.source_id,
                "target_id": candidate.target_id,
                "reason": "degree_cap",
            })
            continue
        raw = f"{candidate.source_id}\0RELATED_TO\0{candidate.target_id}"
        accepted.append(PublishedRelation(
            edge_id="edge:" + hashlib.sha256(raw.encode()).hexdigest()[:20],
            source_id=candidate.source_id,
            target_id=candidate.target_id,
            relation_type="RELATED_TO",
            weight=candidate.statistical_confidence,
            confidence=candidate.statistical_confidence,
            support_jobs=candidate.support_jobs,
            support_companies=candidate.support_companies,
            evidence=candidate.evidence[:3],
            rules_version=candidate.rules_version,
            corpus_hash=candidate.corpus_hash,
        ))
        degree[candidate.source_id] += 1
        degree[candidate.target_id] += 1
    return sorted(accepted, key=lambda item: item.edge_id), sorted(
        rejected, key=lambda item: (item["source_id"], item["target_id"])
    )
