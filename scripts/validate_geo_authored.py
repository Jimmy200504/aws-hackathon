#!/usr/bin/env python3
"""Score every hand-drawn grouping in config/geo-authored.json against behaviour.

The behaviour-derived layers of the geo graph carry their own evidence: an edge
exists because searchers co-selected its endpoints. The authored layers (L1
regions, L3 living areas, L5 sites) carry none, which is the standard objection
to any hand-built ontology - nobody can check it.

They can be checked here, because they make a falsifiable claim. Saying
"台中海線 is one living area" predicts that searchers treat its members as
interchangeable more than chance would explain. That prediction is testable
against artifacts/district-graph.json, which was built without seeing this file.

The null baseline is drawn from the same counties as the group, not from all of
Taiwan. Two random 台中 districts already share more searchers than two random
Taiwanese districts, so a nationwide null would let any same-county grouping
pass and would measure nothing.

Groups that fail are not deleted. A grouping that looks obvious on a map and
that the data rejects is the most informative row in the output.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_AUTHORED = ROOT / "config" / "geo-authored.json"
DEFAULT_DISTRICT_GRAPH = ROOT / "artifacts" / "district-graph.json"
DEFAULT_REGION_GRAPH = ROOT / "artifacts" / "region-graph.json"
DEFAULT_REPORT = ROOT / "reports" / "geo-authored-validation.json"

SAMPLES = 20000
SEED = 1111
# Below this many combinations the null is enumerated exactly instead of
# sampled. Several groups land within 0.001 of their p95, which is inside
# sampling noise, so a sampled null would decide those verdicts by luck.
EXACT_LIMIT = 300_000


def load_pairs(path: Path, key: str, ends: tuple[str, str]) -> dict[frozenset[str], float]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    weights: dict[frozenset[str], float] = {}
    for edge in payload.get(key, []):
        a, b = edge.get(ends[0]), edge.get(ends[1])
        if a and b:
            weights[frozenset((a, b))] = float(edge.get("jaccard") or 0.0)
    return weights


def cohesion(members: list[str], weights: dict[frozenset[str], float]) -> float:
    """Mean pairwise jaccard, counting an absent edge as zero.

    Absent must count as zero rather than be skipped: a group whose members
    were never co-selected is exactly the group the test should reject, and
    averaging only over present edges would score it as if it had no members.
    """
    pairs = list(combinations(members, 2))
    if not pairs:
        return 0.0
    return sum(weights.get(frozenset(pair), 0.0) for pair in pairs) / len(pairs)


def evaluate(
    members: list[str],
    pool: list[str],
    weights: dict[frozenset[str], float],
    rng: random.Random,
) -> dict[str, object]:
    observed = cohesion(members, weights)
    pairs = list(combinations(members, 2))
    present = sum(1 for pair in pairs if frozenset(pair) in weights)
    result: dict[str, object] = {
        "members": len(members),
        "pairs": len(pairs),
        "pairs_with_edge": present,
        "cohesion": round(observed, 5),
        "pair_detail": [
            {
                "a": a,
                "b": b,
                "jaccard": weights.get(frozenset((a, b))),
            }
            for a, b in sorted(pairs)
        ],
    }
    # A group that is most of its own pool cannot be distinguished from it: the
    # random draws keep re-drawing the group's own members.
    result["group_share_of_pool"] = (
        round(len(members) / len(pool), 3) if pool else None
    )
    if len(pool) <= len(members):
        # The group is the whole pool, so there is nothing to compare against.
        result["null"] = None
        result["passed"] = None
        result["verdict"] = "pool_too_small"
        return result

    total = math.comb(len(pool), len(members))
    if total <= EXACT_LIMIT:
        draws = sorted(
            cohesion(list(combo), weights)
            for combo in combinations(pool, len(members))
        )
        mode = "exact"
    else:
        draws = sorted(
            cohesion(rng.sample(pool, len(members)), weights) for _ in range(SAMPLES)
        )
        mode = "sampled"
    p95 = draws[int(0.95 * (len(draws) - 1))]
    below = sum(1 for value in draws if value < observed)
    result["null"] = {
        "mode": mode,
        "pool": len(pool),
        "draws": len(draws),
        "combinations": total,
        "mean": round(statistics.mean(draws), 5),
        "p95": round(p95, 5),
        "max": round(draws[-1], 5),
    }
    percentile = below / len(draws)
    result["percentile"] = round(percentile, 4)
    result["lift_over_null_mean"] = (
        round(observed / statistics.mean(draws), 2) if statistics.mean(draws) else None
    )
    # The verdict is the one-sided percentile, not `observed > p95`. On small
    # pools the null is discrete enough that the two disagree: 北車 clears the
    # p95 value while ranking only 93.9th of 66 possible pairs, and the rank is
    # the statistic that means something.
    result["passed"] = bool(percentile >= 0.95)
    result["verdict"] = "supported" if percentile >= 0.95 else "not_supported"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authored", type=Path, default=DEFAULT_AUTHORED)
    parser.add_argument("--district-graph", type=Path, default=DEFAULT_DISTRICT_GRAPH)
    parser.add_argument("--region-graph", type=Path, default=DEFAULT_REGION_GRAPH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    authored = json.loads(args.authored.read_text(encoding="utf-8"))
    district_payload = json.loads(args.district_graph.read_text(encoding="utf-8"))
    district_weights = load_pairs(args.district_graph, "substitutable_with", ("a", "b"))
    county_weights = load_pairs(args.region_graph, "substitutable_with", ("a", "b"))

    by_county: dict[str, list[str]] = {}
    known_districts: set[str] = set()
    for node in district_payload["nodes"]:
        known_districts.add(node["id"])
        by_county.setdefault(node["county"], []).append(node["id"])
    counties = sorted(by_county)

    rng = random.Random(SEED)
    unknown: list[dict[str, str]] = []

    def check(group_id: str, members: list[str], universe: set[str]) -> list[str]:
        good = []
        for member in members:
            if member in universe:
                good.append(member)
            else:
                unknown.append({"group": group_id, "member": member})
        return good

    regions = []
    for region in authored.get("regions", []):
        members = check(region["id"], region["counties"], set(counties))
        outcome = evaluate(members, counties, county_weights, rng)
        regions.append({"id": region["id"], **outcome})

    living = []
    for area in authored.get("living_areas", []):
        members = check(area["id"], area["districts"], known_districts)
        pool = sorted(
            {
                node
                for member in members
                for node in by_county.get(member.split("/", 1)[0], [])
            }
        )
        outcome = evaluate(members, pool, district_weights, rng)
        living.append(
            {
                "id": area["id"],
                "pool_counties": sorted({m.split("/", 1)[0] for m in members}),
                **outcome,
            }
        )

    sites = []
    for site in authored.get("sites", []):
        members = check(site["id"], site["districts"], known_districts)
        entry: dict[str, object] = {
            "id": site["id"],
            "alias_gate_published": site.get("published"),
            "alias_concentration": site.get("alias_concentration"),
        }
        if len(members) < 2:
            entry.update(
                {
                    "members": len(members),
                    "cohesion": None,
                    "passed": None,
                    "verdict": "single_district_not_testable",
                }
            )
        else:
            pool = sorted(
                {
                    node
                    for member in members
                    for node in by_county.get(member.split("/", 1)[0], [])
                }
            )
            entry.update(evaluate(members, pool, district_weights, rng))
        sites.append(entry)

    shortcuts = []
    for edge in authored.get("shortcuts", []):
        members = check(edge.get("relation", "shortcut"), [edge["a"], edge["b"]], known_districts)
        observed = district_weights.get(frozenset((edge["a"], edge["b"])))
        shortcuts.append(
            {
                "a": edge["a"],
                "b": edge["b"],
                "effective_date": edge.get("effective_date"),
                "provenance": edge.get("provenance"),
                "behaviour_jaccard": observed,
                "note": (
                    "an authored shortcut whose endpoints also carry a behaviour "
                    "edge is corroborated, not proven; the dataset window has no "
                    "before/after contrast for either opening"
                ),
            }
        )

    def summarise(rows: list[dict[str, object]]) -> dict[str, int]:
        return {
            "total": len(rows),
            "supported": sum(1 for row in rows if row.get("passed") is True),
            "not_supported": sum(1 for row in rows if row.get("passed") is False),
            "not_testable": sum(1 for row in rows if row.get("passed") is None),
        }

    report = {
        "metadata": {
            "schema": "skillweave-geo-authored-validation-v1",
            "authored_source": str(args.authored.relative_to(ROOT)).replace("\\", "/"),
            "district_graph": district_payload["metadata"],
            "method": (
                "cohesion = mean pairwise jaccard over the group, absent edge = 0; "
                "null = 2,000 random same-size groups drawn from the districts of "
                "the same counties; supported = cohesion above the null p95"
            ),
            "samples": SAMPLES,
            "random_seed": SEED,
        },
        "summary": {
            "regions": summarise(regions),
            "living_areas": summarise(living),
            "sites": summarise(sites),
            "unknown_members": len(unknown),
        },
        "unknown_members": unknown,
        "regions": regions,
        "living_areas": living,
        "sites": sites,
        "shortcuts": shortcuts,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    for row in regions + living + sites:
        if "cohesion" in row and row.get("cohesion") is not None:
            null = row.get("null") or {}
            print(
                f"  {row['id']:<22} cohesion={row['cohesion']:.4f} "
                f"p95={null.get('p95')} pct={row.get('percentile')} "
                f"({null.get('mode')}) -> {row['verdict']}"
            )
    print(f"Wrote {args.report}")


if __name__ == "__main__":
    main()
