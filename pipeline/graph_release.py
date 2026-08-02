"""Fail-closed quality and serving gates for a blue/green graph release."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReleaseGateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)


def evaluate_release_gates(
    *,
    quality: dict[str, Any],
    gold: dict[str, float],
    graph_off: dict[str, float],
    graph_on: dict[str, float],
    api_smoke_passed: bool,
    degraded_fallback_passed: bool,
    search_p95_ms: float,
    production_manifest: dict[str, Any] | None = None,
) -> ReleaseGateResult:
    checks = {
        "no_silent_loss": quality.get("silent_loss") == 0,
        "referential_integrity": quality.get("referential_integrity") is True,
        "mention_precision": gold.get("mention_precision", 0) >= 0.95,
        "mention_recall": gold.get("mention_recall", 0) >= 0.85,
        "alias_precision": gold.get(
            "exact_alias_precision", gold.get("alias_auto_merge_precision", 0)
        ) >= 0.995,
        "relation_precision": gold.get("published_relation_precision", 0) >= 0.90,
        "api_smoke": api_smoke_passed,
        "degraded_fallback": degraded_fallback_passed,
        "latency": search_p95_ms < 800,
    }
    shared_metrics = sorted(set(graph_off) & set(graph_on))
    checks["ranking_non_regression"] = bool(shared_metrics) and all(
        float(graph_on[name]) >= float(graph_off[name]) for name in shared_metrics
    )
    ndcg_metrics = [name for name in shared_metrics if name.lower().startswith("ndcg")]
    checks["ndcg_positive_lift"] = any(
        float(graph_on[name]) > float(graph_off[name]) for name in ndcg_metrics
    )
    if production_manifest is not None:
        checks.update({
            "full_corpus_processed": production_manifest.get("processed") == 1_218_635,
            "cutoff_eligible_count": production_manifest.get("cutoff_eligible") == 967_377,
            "offline_zero_llm": (
                production_manifest.get("model_id") is None
                and production_manifest.get("llm_requests") == 0
                and production_manifest.get("embedding_requests") == 0
            ),
            "cutoff_default": production_manifest.get("default_scope") == "evaluation-cutoff",
            "candidate_isolation": production_manifest.get("candidate_nodes_published", 0) == 0,
        })
    failures = [name for name, passed in checks.items() if not passed]
    return ReleaseGateResult(not failures, failures, checks)
