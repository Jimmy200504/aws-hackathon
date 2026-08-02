#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.graph_release import evaluate_release_gates


DEFAULT_RELEASE_ROOT = (
    ROOT
    / "artifacts/skill-graph-full-v2/release/runs"
    / "deterministic-v1-rules-v2-full/evaluation-cutoff"
)
DEFAULT_QUALITY = DEFAULT_RELEASE_ROOT / "quality-report.json"
DEFAULT_GRAPH_MANIFEST = DEFAULT_RELEASE_ROOT / "manifest.json"
DEFAULT_GOLD = ROOT / "reports/deterministic-graph-gold-v2.json"
DEFAULT_RANKING = ROOT / "reports/ltr-quality-deterministic-v2.json"
DEFAULT_RUNTIME = ROOT / "reports/deterministic-graph-managed-runtime.json"
DEFAULT_FALLBACK = ROOT / "reports/deterministic-graph-runtime-preflight.json"
DEFAULT_INVENTORY = ROOT / "reports/deterministic-corpus-inventory.json"
DEFAULT_RELEASE = ROOT / "release-manifest.json"
DEFAULT_OUTPUT = ROOT / "reports/deterministic-graph-release-gates.json"


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed verifier for deterministic v2 graph serving approval"
    )
    parser.add_argument("--quality", type=Path, default=DEFAULT_QUALITY)
    parser.add_argument("--graph-manifest", type=Path, default=DEFAULT_GRAPH_MANIFEST)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--ranking", type=Path, default=DEFAULT_RANKING)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--fallback", type=Path, default=DEFAULT_FALLBACK)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--release-manifest", type=Path, default=DEFAULT_RELEASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    quality = load(args.quality)
    graph_manifest = load(args.graph_manifest)
    gold = load(args.gold)
    ranking = load(args.ranking)
    runtime = load(args.runtime)
    fallback = load(args.fallback)
    inventory = load(args.inventory)
    release = load(args.release_manifest)
    runtime_checks = runtime.get("checks", {})
    api_smoke_checks = (
        "health_contract",
        "metadata_contract",
        "full_corpus_scope_when_required",
        "full_corpus_count_when_required",
        "full_corpus_candidate_source_when_required",
        "graph_on_contract",
        "graph_off_contract",
        "neptune_backend_when_required",
        "graph_version_when_required",
        "load_all_http_200",
        "load_all_top_10",
        "load_full_corpus_when_required",
        "load_neptune_when_required",
        "load_graph_version_when_required",
    )
    api_smoke_passed = all(
        runtime_checks.get(name) is True for name in api_smoke_checks
    )
    fallback_passed = (
        fallback.get("passed") is True
        and fallback.get("checks", {}).get("neptune_failure_disclosed") is True
        and fallback.get("checks", {}).get("all_http_200") is True
        and fallback.get("checks", {}).get("all_top_10") is True
    )
    service_latency = runtime.get("load", {}).get("service_latency_ms", {})
    latency = float(service_latency.get("p95", 999_999))
    production_manifest = {
        "processed": inventory.get("processed"),
        "cutoff_eligible": inventory.get("cutoff_eligible"),
        "model_id": graph_manifest.get("model_id"),
        "llm_requests": graph_manifest.get("llm_requests"),
        "embedding_requests": graph_manifest.get("embedding_requests"),
        "default_scope": release.get("graph_build", {}).get("default_scope"),
        "candidate_nodes_published": graph_manifest.get(
            "candidate_nodes_published"
        ),
    }
    result = evaluate_release_gates(
        quality=quality,
        gold=gold,
        graph_off=ranking.get("baseline_no_graph", {}),
        graph_on=ranking.get("skill_graph", {}),
        api_smoke_passed=api_smoke_passed,
        degraded_fallback_passed=fallback_passed,
        search_p95_ms=latency,
        production_manifest=production_manifest,
    )
    paths = {
        "quality": args.quality,
        "graph_manifest": args.graph_manifest,
        "gold": args.gold,
        "ranking": args.ranking,
        "managed_runtime": args.runtime,
        "degraded_fallback": args.fallback,
        "corpus_inventory": args.inventory,
    }
    report = {
        "metadata": {
            "schema": "skillweave-deterministic-graph-release-gates-v1",
            "verified_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "graph_version": graph_manifest.get("graph_version"),
            "graph_manifest_hash": graph_manifest.get("manifest_hash"),
            "policy": "fail_closed_before_serving_manifest_switch",
        },
        "passed": result.passed,
        "serving_approved": result.passed,
        "serving_approval_status": (
            "release_gates_passed"
            if result.passed
            else "blocked_managed_runtime_gates"
        ),
        "checks": result.checks,
        "failures": result.failures,
        "runtime_observed": {
            "graph_backend": runtime.get("observed", {}).get("graph_backend"),
            "graph_version": runtime.get("observed", {}).get("graph_version"),
            "p95_ms": latency,
            "transport_p95_ms": runtime.get("load", {})
            .get("latency_ms", {})
            .get("p95"),
            "p95_gate_basis": "response.meta.latency_ms (service-side)",
            "requests": runtime.get("load", {}).get("requests"),
            "concurrency": runtime.get("load", {}).get("concurrency"),
        },
        "sources": {
            name: {"path": str(path), "sha256": digest(path)}
            for name, path in paths.items()
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
