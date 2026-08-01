"""In-memory geo graph over districts, assembled from behaviour plus authored layers.

`docs/geo-graph.md` asks for a layered geographic graph so that a search for a
district with no jobs degrades toward genuinely nearby districts instead of
either returning nothing or falling back to the whole county. This builds that
graph, with three changes to the spec that `docs/geo-graph-handoff.md` measured
the need for.

**No networkx.** Around 370 nodes and 5,000 edges. Dijkstra over `heapq` is
thirty lines and keeps the production dependency list at eight packages.

**One cost unit, derived not authored.** The spec priced `is_adjacent_to` at 20
minutes and a `shortcut` at 10. Nothing in the dataset supports either number.
Instead an edge costs `-log(substitutability)`, where substitutability is the
Jaccard overlap of the two districts' searcher populations. Costs then add along
a path exactly when the substitutabilities multiply, so a two-hop route is
priced as the joint event it is, and no minute figure is ever invented.

**Levels are set membership, not weighted edges.** The spec routed through
parent nodes and priced `is_part_of` at 999 to stop the graph short-circuiting
through them. But then no budget can ever reach an L3 node, which defeats the
purpose of having one. Here L1/L3/L5 are containers answering `members_of`, and
all distance is measured on L4 edges.

Every edge carries its provenance (`behaviour`, `authored`, `external`) so the
authored contribution can be switched off and measured separately. Authored
edges never lower a behaviour weight; they can only raise it, so switching them
off returns the graph to pure behaviour.

As with `app/region_graph.py`, nothing here changes ranking. The offline
benchmark is a re-ranking benchmark whose candidate sets are already
county-filtered, so a geographic feature would have zero variance inside the
candidate group. `docs/evaluation-limits.md` records that measurement.
"""
from __future__ import annotations

import heapq
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DISTRICT_GRAPH = ROOT / "artifacts" / "district-graph.json"
DEFAULT_AUTHORED = ROOT / "config" / "geo-authored.json"

# The graph is built as of the first evaluation day, so an edge that only comes
# into existence later must not be present. Passed to `build_geo_graph`.
DEFAULT_CUTOFF = "2026-06-01"

# A budget of 3.0 admits any route whose substitutabilities multiply to at least
# e**-3 = 0.0498, i.e. roughly one in twenty searchers of the origin also chose
# the destination. That matches REGION_GRAPH_MIN_CONDITIONAL, which uses the
# same one-in-twenty reasoning at county level.
DEFAULT_MAX_COST = 3.0
DEFAULT_LIMIT = 5

BEHAVIOUR = "behaviour"
AUTHORED = "authored"
EXTERNAL = "external"


def _cost(weight: float) -> float:
    """Turn a 0-1 substitutability into an additive path cost."""
    if weight <= 0.0:
        return math.inf
    return -math.log(min(weight, 1.0))


@dataclass(frozen=True)
class GeoEdge:
    """One undirected district-to-district hop and where its weight came from."""

    weight: float
    provenance: str
    co_selected: int | None = None
    label: str | None = None
    note: str | None = None

    @property
    def cost(self) -> float:
        return _cost(self.weight)


@dataclass(frozen=True)
class GeoHop:
    source: str
    target: str
    edge: GeoEdge


@dataclass(frozen=True)
class GeoReach:
    """A district reachable within budget, with the route that got there."""

    district: str
    cost: float
    path: tuple[str, ...]
    hops: tuple[GeoHop, ...] = ()

    @property
    def substitutability(self) -> float:
        """The product of the hop weights, i.e. e**-cost."""
        return math.exp(-self.cost)

    @property
    def provenance(self) -> tuple[str, ...]:
        """Every provenance used along the route, in first-seen order."""
        seen: dict[str, None] = {}
        for hop in self.hops:
            seen.setdefault(hop.edge.provenance, None)
        return tuple(seen)

    @property
    def explanation(self) -> str:
        if not self.hops:
            return f"{self.district} 即為搜尋地區"
        first = self.hops[0]
        if len(self.hops) == 1:
            if first.edge.provenance == BEHAVIOUR and first.edge.co_selected:
                return (
                    f"{first.edge.co_selected:,} 次搜尋同時勾選"
                    f"{_short(first.source)}與{_short(first.target)}"
                )
            if first.edge.label:
                return (
                    f"{_short(first.source)} 與 {_short(first.target)} 之間"
                    f"由{first.edge.label}連接（外部資料，非本資料集導出）"
                )
            return f"{_short(first.source)} 與 {_short(first.target)} 直接相連"
        route = " → ".join(_short(node) for node in self.path)
        return f"經 {len(self.hops)} 段路徑：{route}"

    def payload(self) -> dict[str, Any]:
        return {
            "district": self.district,
            "cost": round(self.cost, 4),
            "substitutability": round(self.substitutability, 5),
            "path": list(self.path),
            "provenance": list(self.provenance),
            "explanation": self.explanation,
            "hops": [
                {
                    "from": hop.source,
                    "to": hop.target,
                    "weight": round(hop.edge.weight, 5),
                    "provenance": hop.edge.provenance,
                    "co_selected": hop.edge.co_selected,
                }
                for hop in self.hops
            ],
        }


def _short(node: str) -> str:
    """'新北市/八里區' -> '八里區', for user-facing sentences."""
    return node.split("/", 1)[-1]


class GeoGraph:
    """Read-only layered geo graph with a stdlib shortest-path query."""

    DEGRADED_COMPONENT = "geo_graph"

    def __init__(
        self,
        district_graph_path: Path | None = None,
        authored_path: Path | None = None,
        *,
        cutoff_date: str = DEFAULT_CUTOFF,
        max_cost: float = DEFAULT_MAX_COST,
        limit: int = DEFAULT_LIMIT,
        include_authored: bool = True,
    ) -> None:
        self.district_graph_path = Path(district_graph_path or DEFAULT_DISTRICT_GRAPH)
        self.authored_path = Path(authored_path or DEFAULT_AUTHORED)
        self.cutoff_date = cutoff_date
        self.max_cost = float(max_cost)
        self.limit = max(0, int(limit))
        self.include_authored = bool(include_authored)

        self.metadata: dict[str, Any] = {}
        self.districts: dict[str, dict[str, Any]] = {}
        self.code_to_district: dict[str, str] = {}
        self.county_districts: dict[str, list[str]] = {}
        self.groups: dict[str, dict[str, Any]] = {}
        self.district_groups: dict[str, list[str]] = {}
        self.aliases: dict[str, list[str]] = {}
        self.excluded_edges: list[dict[str, Any]] = []
        self.corroborated: list[dict[str, Any]] = []
        self._adjacency: dict[str, dict[str, GeoEdge]] = {}
        self._load()

    # ------------------------------------------------------------------ build

    @classmethod
    def from_environment(cls) -> GeoGraph:
        override = os.getenv("GEO_GRAPH_PATH")
        authored = os.getenv("GEO_AUTHORED_PATH")
        return cls(
            Path(override) if override else None,
            Path(authored) if authored else None,
            cutoff_date=os.getenv("GEO_GRAPH_CUTOFF", DEFAULT_CUTOFF),
            max_cost=float(os.getenv("GEO_GRAPH_MAX_COST", DEFAULT_MAX_COST)),
            limit=int(os.getenv("GEO_GRAPH_LIMIT", DEFAULT_LIMIT)),
            include_authored=os.getenv("GEO_GRAPH_AUTHORED", "1") != "0",
        )

    def _load(self) -> None:
        try:
            payload = json.loads(self.district_graph_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A deployment package without the artifact must still serve search.
            LOGGER.warning(
                "Geo graph unavailable; geo trace disabled: %s", type(exc).__name__
            )
            return
        self.metadata = dict(payload.get("metadata", {}))
        for node in payload.get("nodes", []):
            node_id = node.get("id")
            if not node_id:
                continue
            self.districts[node_id] = node
            self.county_districts.setdefault(node.get("county", ""), []).append(node_id)
            for code in node.get("codes", []):
                self.code_to_district[str(code)] = node_id
        for edge in payload.get("substitutable_with", []):
            a, b, weight = edge.get("a"), edge.get("b"), edge.get("jaccard")
            if not a or not b or not weight:
                continue
            self._link(
                a,
                b,
                GeoEdge(
                    weight=float(weight),
                    provenance=BEHAVIOUR,
                    co_selected=edge.get("co_selected"),
                ),
            )
        if self.include_authored:
            self._load_authored()

    def _load_authored(self) -> None:
        try:
            authored = json.loads(self.authored_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.warning(
                "Authored geo layers unavailable: %s", type(exc).__name__
            )
            return
        self.metadata["authored_schema"] = authored.get("schema")

        for region in authored.get("regions", []):
            members = [
                district
                for county in region.get("counties", [])
                for district in self.county_districts.get(county, [])
            ]
            self._register_group(region["id"], "L1", members, region.get("counties", []))
        for area in authored.get("living_areas", []):
            self._register_group(
                area["id"],
                "L3",
                [d for d in area.get("districts", []) if d in self.districts],
                aliases=area.get("aliases", []),
            )
        for site in authored.get("sites", []):
            if not site.get("published", True):
                # Rejected by the alias concentration gate; kept in the config
                # file as a record, never loaded into the graph.
                self.excluded_edges.append(
                    {
                        "id": site["id"],
                        "kind": "site",
                        "reason": site.get("rejected_reason"),
                        "alias_concentration": site.get("alias_concentration"),
                    }
                )
                continue
            self._register_group(
                site["id"],
                "L5",
                [d for d in site.get("districts", []) if d in self.districts],
                aliases=site.get("aliases", []),
            )

        for edge in authored.get("shortcuts", []):
            a, b = edge.get("a"), edge.get("b")
            if a not in self.districts or b not in self.districts:
                continue
            effective = edge.get("effective_date")
            if effective and not self._in_effect(effective):
                self.excluded_edges.append(
                    {
                        "id": f"{a}--{b}",
                        "kind": "shortcut",
                        "reason": "effective_date after graph cutoff",
                        "effective_date": effective,
                        "cutoff_date": self.cutoff_date,
                    }
                )
                continue
            implied = float(edge.get("implied_substitutability") or 0.0)
            existing = self._adjacency.get(a, {}).get(b)
            if existing is not None:
                # Measured evidence outranks asserted evidence. An authored
                # edge supplies a hop where behaviour has none; it never
                # overwrites a weight the search logs already established,
                # otherwise the most visible number in the graph would be a
                # hand-written one. A pair that carries both is recorded as
                # corroborated and left at its behaviour weight.
                self.corroborated.append(
                    {
                        "a": a,
                        "b": b,
                        "label": edge.get("label"),
                        "effective_date": effective,
                        "implied_substitutability": implied,
                        "behaviour_substitutability": existing.weight,
                        "co_selected": existing.co_selected,
                    }
                )
                continue
            self._link(
                a,
                b,
                GeoEdge(
                    weight=implied,
                    provenance=edge.get("provenance", EXTERNAL),
                    label=edge.get("label"),
                    note=edge.get("note"),
                ),
            )

    def _in_effect(self, effective: str) -> bool:
        try:
            return date.fromisoformat(effective) <= date.fromisoformat(self.cutoff_date)
        except ValueError:
            LOGGER.warning("Unparseable effective_date %r; edge dropped", effective)
            return False

    def _register_group(
        self,
        group_id: str,
        level: str,
        members: Sequence[str],
        counties: Sequence[str] = (),
        aliases: Sequence[str] = (),
    ) -> None:
        self.groups[group_id] = {
            "id": group_id,
            "level": level,
            "members": list(members),
            "counties": list(counties),
            "aliases": list(aliases),
            "provenance": AUTHORED,
        }
        for member in members:
            self.district_groups.setdefault(member, []).append(group_id)
        for alias in aliases:
            self.aliases.setdefault(alias, []).append(group_id)

    def _link(self, a: str, b: str, edge: GeoEdge) -> None:
        if a not in self.districts or b not in self.districts:
            return
        self._adjacency.setdefault(a, {})[b] = edge
        self._adjacency.setdefault(b, {})[a] = edge

    # ------------------------------------------------------------------ query

    @property
    def enabled(self) -> bool:
        return bool(self._adjacency)

    @property
    def edge_count(self) -> int:
        return sum(len(targets) for targets in self._adjacency.values()) // 2

    def resolve(
        self, codes: Iterable[str] | None, locations: Mapping[str, Sequence[str]] | None = None
    ) -> tuple[str, ...]:
        """Map filter codes to district nodes.

        District codes resolve directly. County codes resolve to nothing here
        by design: a search filtered on the whole of 新北市 has not named a
        district, and inventing one would put words in the searcher's mouth.
        `app/region_graph.py` already handles that case at its own level.
        """
        resolved: dict[str, None] = {}
        for code in codes or ():
            node = self.code_to_district.get(str(code))
            if node:
                resolved.setdefault(node, None)
        return tuple(resolved)

    def members_of(self, group_id: str) -> tuple[str, ...]:
        """Districts inside an L1/L3/L5 container. Level is not a distance."""
        group = self.groups.get(group_id)
        return tuple(group["members"]) if group else ()

    def groups_of(self, district: str) -> tuple[str, ...]:
        return tuple(self.district_groups.get(district, ()))

    def resolve_alias(self, surface: str) -> tuple[str, ...]:
        """Districts an out-of-vocabulary place name expands to, e.g. 竹科."""
        members: dict[str, None] = {}
        for group_id in self.aliases.get(surface, ()):
            for member in self.members_of(group_id):
                members.setdefault(member, None)
        return tuple(members)

    def expand(
        self,
        sources: Sequence[str],
        *,
        max_cost: float | None = None,
        limit: int | None = None,
    ) -> list[GeoReach]:
        """Districts reachable from `sources` within the cost budget, nearest first.

        Dijkstra from a virtual super-source, so a multi-district search is one
        traversal and each result is attributed to whichever origin reached it
        most cheaply.

        `limit` of None uses the configured limit; 0 returns nothing, matching
        `app/region_graph.py`. Pass `limit=len(graph.districts)` for everything
        inside the budget.
        """
        if not self.enabled or not sources:
            return []
        budget = self.max_cost if max_cost is None else float(max_cost)
        cap = self.limit if limit is None else max(0, int(limit))
        origins = [node for node in sources if node in self.districts]
        if not origins:
            return []

        best: dict[str, float] = {node: 0.0 for node in origins}
        routes: dict[str, tuple[GeoHop, ...]] = {node: () for node in origins}
        paths: dict[str, tuple[str, ...]] = {node: (node,) for node in origins}
        queue: list[tuple[float, str]] = [(0.0, node) for node in origins]
        heapq.heapify(queue)
        settled: set[str] = set()

        while queue:
            cost, node = heapq.heappop(queue)
            if node in settled or cost > best.get(node, math.inf):
                continue
            settled.add(node)
            for neighbour, edge in self._adjacency.get(node, {}).items():
                candidate = cost + edge.cost
                if candidate > budget or candidate >= best.get(neighbour, math.inf):
                    continue
                best[neighbour] = candidate
                routes[neighbour] = routes[node] + (GeoHop(node, neighbour, edge),)
                paths[neighbour] = paths[node] + (neighbour,)
                heapq.heappush(queue, (candidate, neighbour))

        origin_set = set(origins)
        reached = [
            GeoReach(
                district=node,
                cost=cost,
                path=paths[node],
                hops=routes[node],
            )
            for node, cost in best.items()
            if node not in origin_set
        ]
        # Total order: cheapest first, then the shorter route, then by name, so
        # the same artifact always renders the same list.
        reached.sort(key=lambda reach: (round(reach.cost, 9), len(reach.hops), reach.district))
        return reached[:cap]

    def trace(
        self,
        codes: Iterable[str] | None,
        locations: Mapping[str, Sequence[str]] | None = None,
    ) -> dict[str, Any] | None:
        """Geo expansion payload for `meta.geo_trace`, or None when not applicable."""
        if not self.enabled:
            return None
        searched = self.resolve(codes, locations)
        if not searched:
            return None
        expansions = self.expand(searched)
        return {
            "schema": "skillweave-geo-graph-v1",
            "dataset_version": self.metadata.get("dataset_version"),
            "graph_cutoff": self.metadata.get("graph_cutoff"),
            "cutoff_date": self.cutoff_date,
            "searched_districts": list(searched),
            "groups": sorted(
                {group for node in searched for group in self.groups_of(node)}
            ),
            "cost_model": "-log(substitutability); hop weights multiply along a path",
            "max_cost": self.max_cost,
            "expansions": [reach.payload() for reach in expansions],
            "edges_excluded_by_cutoff": [
                item for item in self.excluded_edges if item["kind"] == "shortcut"
            ],
            # Authored shortcuts whose pair the search logs already connect. The
            # behaviour weight is what the graph uses; this records agreement.
            "authored_edges_corroborated": list(self.corroborated),
            # The graph acts at retrieval expansion. This reports what the data
            # supports; it does not reorder any result.
            "applied_to_ranking": False,
        }


# --------------------------------------------------------------- spec parity

def build_geo_graph(
    base_path: Path | str | None = None,
    special_path: Path | str | None = None,
    cutoff_date: str = DEFAULT_CUTOFF,
    **kwargs: Any,
) -> GeoGraph:
    """The `docs/geo-graph.md` entry point, over this repo's artifacts.

    `base_path` is the behaviour-derived district graph rather than a
    hand-written `geo_base.json`, and `special_path` is the authored overlay.
    Temporal filtering behaves as the spec requires: an edge is loaded only if
    its `effective_date` is on or before `cutoff_date`, and an edge without one
    is always in effect.
    """
    return GeoGraph(
        Path(base_path) if base_path else None,
        Path(special_path) if special_path else None,
        cutoff_date=cutoff_date,
        **kwargs,
    )


def get_expanded_locations(
    graph: GeoGraph, source_node: str, max_distance: float = DEFAULT_MAX_COST
) -> list[GeoReach]:
    """The spec's expansion query. `max_distance` is a `-log` cost budget.

    Returns every district inside the budget, unlike `GeoGraph.expand`, whose
    default limit is tuned for the API response.
    """
    return graph.expand(
        [source_node], max_cost=max_distance, limit=len(graph.districts)
    )
