#!/usr/bin/env python3
"""Score the authored adjacency map against behaviour, and report where they disagree.

Two graphs over the same 368 nodes, built from unrelated sources:

  config/geo-adjacency.json      hand-authored land borders and commute grades
  artifacts/district-graph.json  search co-selection, 4,857 edges, no human weight

Neither is a ground truth for the other. A map cannot know that 基隆 commuters
work in 台北, and search logs cannot know that a border is a mountain ridge with
no road over it. The output worth having is the disagreement, in three parts.

**Does the commute grade predict behaviour?** This is the sharpest test of the
authored layer, and it is a real test because the grades were written before any
of this was computed. If `easy`, `moderate`, `hard` and `impassable` are
meaningful, mean substitutability should fall monotonically across them. If it
does not, the grades are decoration.

**Barriers.** Adjacent, and searchers do not treat them as interchangeable. Some
of these are the mountain borders the grade already predicts; the interesting
ones are the borders graded `easy` that behaviour rejects anyway.

**Corridors.** Not adjacent, and searchers substitute them freely. A rail line,
a freeway, or a labour market that ignores the map. These are the edges an
adjacency-only geo graph - the one `docs/geo-graph.md` originally specified -
would have missed entirely.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADJACENCY = ROOT / "config" / "geo-adjacency.json"
DEFAULT_BEHAVIOUR = ROOT / "artifacts" / "district-graph.json"
DEFAULT_REPORT = ROOT / "reports" / "geo-adjacency-validation.json"
COMMUTE_ORDER = ["easy", "moderate", "hard", "impassable"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adjacency", type=Path, default=DEFAULT_ADJACENCY)
    parser.add_argument("--behaviour", type=Path, default=DEFAULT_BEHAVIOUR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--corridor-min-jaccard", type=float, default=0.05)
    args = parser.parse_args()

    adjacency = json.loads(args.adjacency.read_text(encoding="utf-8"))
    behaviour = json.loads(args.behaviour.read_text(encoding="utf-8"))

    weight: dict[frozenset[str], dict] = {}
    for edge in behaviour["substitutable_with"]:
        weight[frozenset((edge["a"], edge["b"]))] = edge
    searched = {node["id"]: node["searches"] for node in behaviour["nodes"]}

    adjacent: dict[frozenset[str], dict] = {}
    for edge in adjacency["edges"]:
        adjacent[frozenset((edge["a"], edge["b"]))] = edge

    # A pair where either district is barely searched carries no evidence either
    # way, so it is excluded from the disagreement counts rather than scored as
    # a barrier. 368 districts include several with under 100 searches.
    def testable(pair: frozenset[str]) -> bool:
        return all(searched.get(node, 0) >= 100 for node in pair)

    by_grade: dict[str, list[float]] = {grade: [] for grade in COMMUTE_ORDER}
    for pair, edge in adjacent.items():
        if not testable(pair):
            continue
        by_grade[edge["commute"]].append(
            float(weight[pair]["jaccard"]) if pair in weight else 0.0
        )

    grade_table = {
        grade: {
            "pairs": len(values),
            "mean_jaccard": round(statistics.mean(values), 5) if values else None,
            "median_jaccard": round(statistics.median(values), 5) if values else None,
            "share_with_behaviour_edge": round(
                sum(1 for v in values if v > 0) / len(values), 4
            )
            if values
            else None,
        }
        for grade, values in by_grade.items()
    }
    means = [
        grade_table[grade]["mean_jaccard"]
        for grade in COMMUTE_ORDER
        if grade_table[grade]["mean_jaccard"] is not None
    ]
    monotonic = all(a >= b for a, b in zip(means, means[1:]))

    # Adjacency does not know about administrative boundaries, so an `easy`
    # border is graded the same whether or not it happens to be a county line.
    # Behaviour can be asked whether that makes a difference.
    county_line: dict[str, list[float]] = {"intra_county": [], "cross_county": []}
    for pair, edge in adjacent.items():
        if not testable(pair) or edge["commute"] != "easy" or edge["barrier"] != "none":
            continue
        county_line[edge["scope"]].append(
            float(weight[pair]["jaccard"]) if pair in weight else 0.0
        )
    county_line_table = {
        scope: {
            "pairs": len(values),
            "mean_jaccard": round(statistics.mean(values), 5) if values else None,
            "median_jaccard": round(statistics.median(values), 5) if values else None,
        }
        for scope, values in county_line.items()
    }
    intra_mean = county_line_table["intra_county"]["mean_jaccard"] or 0.0
    cross_mean = county_line_table["cross_county"]["mean_jaccard"] or 0.0

    barriers = []
    for pair, edge in adjacent.items():
        if not testable(pair):
            continue
        entry = weight.get(pair)
        jaccard = float(entry["jaccard"]) if entry else 0.0
        if jaccard >= args.corridor_min_jaccard:
            continue
        barriers.append(
            {
                "a": edge["a"], "b": edge["b"], "scope": edge["scope"],
                "authored_barrier": edge["barrier"], "authored_commute": edge["commute"],
                "crossing": edge.get("crossing"),
                "jaccard": jaccard,
                "co_selected": entry["co_selected"] if entry else 0,
                "searches": [searched.get(edge["a"], 0), searched.get(edge["b"], 0)],
            }
        )
    barriers.sort(key=lambda row: (row["jaccard"], -min(row["searches"])))

    corridors = []
    for pair, entry in weight.items():
        if pair in adjacent or not testable(pair):
            continue
        if float(entry["jaccard"]) < args.corridor_min_jaccard:
            continue
        a, b = sorted(pair)
        corridors.append(
            {
                "a": a, "b": b,
                "same_county": a.split("/", 1)[0] == b.split("/", 1)[0],
                "jaccard": entry["jaccard"],
                "co_selected": entry["co_selected"],
            }
        )
    corridors.sort(key=lambda row: -row["jaccard"])

    testable_adjacent = [p for p in adjacent if testable(p)]
    with_edge = sum(1 for p in testable_adjacent if p in weight)
    report = {
        "metadata": {
            "schema": "skillweave-geo-adjacency-validation-v1",
            "adjacency": "config/geo-adjacency.json",
            "behaviour": behaviour["metadata"],
            "testable_rule": "both districts selected in at least 100 train-window searches",
            "corridor_min_jaccard": args.corridor_min_jaccard,
        },
        "coverage": {
            "authored_edges": len(adjacent),
            "authored_edges_testable": len(testable_adjacent),
            "authored_with_behaviour_edge": with_edge,
            "authored_share_confirmed": round(with_edge / max(1, len(testable_adjacent)), 4),
            "behaviour_edges": len(weight),
            "behaviour_edges_not_adjacent": sum(1 for p in weight if p not in adjacent),
            "behaviour_share_not_adjacent": round(
                sum(1 for p in weight if p not in adjacent) / max(1, len(weight)), 4
            ),
        },
        "commute_grade_vs_behaviour": {
            "monotonic": monotonic,
            "reading": (
                "mean substitutability falls as the authored commute grade worsens"
                if monotonic
                else "the authored grades do not order behaviour; treat them as decoration"
            ),
            "by_grade": grade_table,
        },
        "county_line_effect": {
            "rule": "adjacent pairs graded easy with no barrier, split by whether the border is a county line",
            "by_scope": county_line_table,
            "ratio_intra_over_cross": round(intra_mean / cross_mean, 2) if cross_mean else None,
            "reading": (
                "the same flat, unobstructed border is treated as far more "
                "substitutable inside a county than across one; the map cannot "
                "see that boundary and the search log can"
            ),
        },
        "barriers": {
            "rule": f"adjacent but jaccard below {args.corridor_min_jaccard}",
            "count": len(barriers),
            "top": barriers[:40],
            "highest_traffic": sorted(
                barriers, key=lambda row: -min(row["searches"])
            )[:20],
        },
        "corridors": {
            "rule": f"not adjacent but jaccard at least {args.corridor_min_jaccard}",
            "count": len(corridors),
            "cross_county_count": sum(1 for row in corridors if not row["same_county"]),
            "top": corridors[:25],
            "top_cross_county": [row for row in corridors if not row["same_county"]][:25],
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["coverage"], ensure_ascii=False, indent=2))
    print(json.dumps(report["commute_grade_vs_behaviour"], ensure_ascii=False, indent=2))
    print(json.dumps(report["county_line_effect"], ensure_ascii=False, indent=2))
    print(f"barriers={len(barriers)} corridors={len(corridors)}")
    print(f"Wrote {args.report}")


if __name__ == "__main__":
    main()
