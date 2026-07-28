from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


ALLOWED_MENTION_TYPES = {
    "language",
    "framework",
    "library",
    "tool",
    "platform",
    "database",
    "method",
    "certification",
    "domain",
    "soft_skill",
}
ALLOWED_LEVELS = {"required", "preferred", "mentioned"}
ALLOWED_RELATIONS = {"RELATED_TO", "PREREQUISITE_OF", "SPECIALIZATION_OF"}


@dataclass
class ValidationResult:
    accepted_mentions: list[dict[str, Any]] = field(default_factory=list)
    accepted_relations: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return bool(self.accepted_mentions) and not any(
            item["severity"] == "fatal" for item in self.rejected
        )


def validate_extraction(
    source_fields: dict[str, str],
    source_modified_at: str,
    graph_cutoff: str,
    payload: dict[str, Any],
    confidence_threshold: float = 0.75,
) -> ValidationResult:
    result = ValidationResult()
    modified = datetime.fromisoformat(source_modified_at)
    cutoff = datetime.fromisoformat(graph_cutoff)
    if modified > cutoff:
        result.rejected.append(
            {
                "severity": "fatal",
                "code": "future_source",
                "message": "source_modified_at is after graph cutoff",
            }
        )
        return result
    if not isinstance(payload, dict):
        result.rejected.append(
            {"severity": "fatal", "code": "invalid_payload", "message": "payload must be object"}
        )
        return result

    canonical: set[str] = set()
    for mention in payload.get("mentions", []):
        if not isinstance(mention, dict):
            result.rejected.append(
                {"severity": "warning", "code": "invalid_mention", "message": "mention is not object"}
            )
            continue
        evidence = str(mention.get("evidence", "")).strip()
        evidence_field = str(mention.get("evidence_field", "")).strip()
        skill = str(mention.get("canonical_skill", "")).strip()
        mention_type = str(mention.get("type", "")).strip()
        level = str(mention.get("level", "")).strip()
        try:
            confidence = float(mention.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0
        errors: list[str] = []
        if not skill:
            errors.append("missing canonical_skill")
        if mention_type not in ALLOWED_MENTION_TYPES:
            errors.append("unsupported mention type")
        if level not in ALLOWED_LEVELS:
            errors.append("unsupported requirement level")
        if not (0 <= confidence <= 1) or confidence < confidence_threshold:
            errors.append("confidence below publish threshold")
        if evidence_field not in source_fields:
            errors.append("unknown evidence_field")
        elif not evidence or evidence not in source_fields[evidence_field]:
            errors.append("evidence is not an exact source substring")
        if errors:
            result.rejected.append(
                {
                    "severity": "warning",
                    "code": "mention_rejected",
                    "message": f"{skill or '<empty>'}: " + "; ".join(errors),
                }
            )
            continue
        accepted = dict(mention)
        accepted["confidence"] = confidence
        accepted["validated"] = True
        result.accepted_mentions.append(accepted)
        canonical.add(skill)

    for relation in payload.get("relations", []):
        if not isinstance(relation, dict):
            continue
        source = str(relation.get("source", "")).strip()
        target = str(relation.get("target", "")).strip()
        relation_type = str(relation.get("type", "")).strip()
        try:
            confidence = float(relation.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0
        errors: list[str] = []
        if relation_type not in ALLOWED_RELATIONS:
            errors.append("unsupported relation type")
        if not source or not target or source == target:
            errors.append("invalid relation endpoints")
        # At least one endpoint must be grounded in this JD. A separate corpus
        # corroboration stage must validate the other endpoint before publish.
        if source not in canonical and target not in canonical:
            errors.append("relation is ungrounded in accepted mentions")
        if confidence < 0.85 or confidence > 1:
            errors.append("relation confidence below threshold")
        if errors:
            result.rejected.append(
                {
                    "severity": "warning",
                    "code": "relation_rejected",
                    "message": f"{source}->{target}: " + "; ".join(errors),
                }
            )
            continue
        accepted = dict(relation)
        accepted["confidence"] = confidence
        accepted["validated"] = True
        accepted["requires_corpus_corroboration"] = True
        result.accepted_relations.append(accepted)
    return result
