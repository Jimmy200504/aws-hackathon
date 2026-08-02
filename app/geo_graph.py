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

**This layer now acts on candidate selection**, unlike `app/region_graph.py`.
`GeoExpansion.substitutability` is handed to `app/ranker.py`, which withholds the
out-of-area penalty from a district the graph vouches for. That is a recall
change and it does move results, so `meta.geo_trace.applied_to_ranking` reports
`True` whenever the ranker was given the expansion.

It adds no positive weight. A vouched district scores neutral on the location
feature where an unrelated one scores negative, and the substitutability itself
is carried as an unweighted feature for a future model to price. Inventing a
magnitude here would be the one thing the cost model above refuses to do.

No offline lift is claimed, because none can be measured: the benchmark reranks
candidate sets that the current system already county-filtered, so 84.3% of its
cases hold candidates from at most one county and cannot express a cross-district
substitution. `docs/evaluation-limits.md` records that measurement. The effect is
visible only where recall was the binding constraint - a search for 林口區 could
not reach 龜山區, its cheapest substitute, because 龜山區 is in 桃園市.
"""
from __future__ import annotations

import heapq
import json
import logging
import math
import os
import re
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DISTRICT_GRAPH = ROOT / "artifacts" / "district-graph.json"
DEFAULT_AUTHORED = ROOT / "config" / "geo-authored.json"
# Only the corpus-validated subset is loaded. The authored source table lives at
# config/geo-l5-table.json and is deliberately not read here: entries reach the
# graph by surviving scripts/validate_l5_table.py, not by being written down.
DEFAULT_L5 = ROOT / "config" / "geo-l5-published.json"
DEFAULT_ADJACENCY = ROOT / "config" / "geo-adjacency.json"
# District name surfaces, for reading a place out of the query text rather than
# out of a filter code. Built by scripts/build_l4_table.py.
DEFAULT_L4 = ROOT / "config" / "geo-l4-districts.json"

# Surfaces read as ordinary words rather than place references, with the job
# corpus precision that supports the reading. Precision is the share of postings
# containing the string that sit in a county holding that district, taken from
# reports/job-district-extraction.json.
#
# These are excluded by judgement, not by a threshold. Each is a common word or
# one of Taiwan's commonest street names, so a searcher typing it bare is
# usually not naming the district. Precision is corroboration, not the
# criterion: 林口 (0.5386) and 萬華 (0.6956) score no better and are
# unambiguously place names, which is why no precision cut-off is applied here.
# No query-side labels exist, so calling this measured would overstate it.
#
# The job-side extractor rejects 58 short forms; only these are rejected here.
# It also drops 七美, 北竿 and the other offshore names for having almost no
# postings to judge, which is a sample-size verdict rather than a word-collision
# one. A searcher who types 七美鄉 means 七美鄉, so those stay usable.
WORD_COLLISION_SURFACES: Mapping[str, float] = {
    "成功": 0.0058,
    "和平": 0.0863,
    "中西": 0.0804,
    "中正": 0.1217,
    "復興": 0.2207,
    "中山": 0.2309,
    "大同": 0.2586,
    "三民": 0.4181,
}
# Official names that read as a generic noun rather than a place: 北區 is usually
# a sales territory and 新社區 usually a newly built residential block. 東區
# (0.7899) and 南區 (0.6055) are left out of this list on purpose. They are real
# district names whose only problem is naming several counties at once, which the
# county-hint rule already handles.
GENERIC_FULL_NAMES: Mapping[str, float] = {"北區": 0.3591, "新社區": 0.1571}

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


@dataclass(frozen=True)
class GeoExpansion:
    """One request's geographic reading: what was named, and what substitutes.

    Computed once per request and consumed twice, by candidate selection and by
    `meta.geo_trace`. Two separate computations would let the panel describe an
    expansion the ranker did not use, which is the failure mode this exists to
    prevent.
    """

    from_codes: tuple[str, ...] = ()
    from_text: tuple[str, ...] = ()
    notes: tuple[dict[str, Any], ...] = ()
    county_hint: tuple[str, ...] = ()
    reaches: tuple[GeoReach, ...] = ()

    @property
    def searched(self) -> tuple[str, ...]:
        """Districts the request actually named, filter codes first."""
        return tuple(dict.fromkeys((*self.from_codes, *self.from_text)))

    @property
    def skipped(self) -> tuple[dict[str, Any], ...]:
        return tuple(note for note in self.notes if note.get("skipped"))

    def __bool__(self) -> bool:
        return bool(self.searched or self.skipped)

    @property
    def substitutability(self) -> dict[str, float]:
        """District node -> how substitutable it is for what was searched.

        A named district scores 1.0; a reached one scores the product of the hop
        weights along its route. Nothing else appears, so a caller cannot read
        this as "anywhere in the county".
        """
        weights = {node: 1.0 for node in self.searched}
        for reach in self.reaches:
            # A district that was named outright is not downgraded by also being
            # reachable from another named district.
            weights.setdefault(reach.district, reach.substitutability)
        return weights

    @property
    def counties(self) -> tuple[str, ...]:
        """Counties holding any searched or reached district, in first-seen order.

        The retrieval layer needs these to widen a county filter far enough to
        reach a cross-county substitute: 林口區's cheapest neighbour is 龜山區,
        which is in 桃園市, so a 新北市 filter excludes it by construction.
        """
        seen: dict[str, None] = {}
        for node in (*self.searched, *(reach.district for reach in self.reaches)):
            seen.setdefault(node.split("/", 1)[0], None)
        return tuple(seen)


class GeoGraph:
    """Read-only layered geo graph with a stdlib shortest-path query."""

    DEGRADED_COMPONENT = "geo_graph"

    def __init__(
        self,
        district_graph_path: Path | None = None,
        authored_path: Path | None = None,
        l5_path: Path | None = None,
        adjacency_path: Path | None = None,
        l4_path: Path | None = None,
        *,
        cutoff_date: str = DEFAULT_CUTOFF,
        max_cost: float = DEFAULT_MAX_COST,
        limit: int = DEFAULT_LIMIT,
        include_authored: bool = True,
    ) -> None:
        self.district_graph_path = Path(district_graph_path or DEFAULT_DISTRICT_GRAPH)
        self.authored_path = Path(authored_path or DEFAULT_AUTHORED)
        self.l5_path = Path(l5_path or DEFAULT_L5)
        self.adjacency_path = Path(adjacency_path or DEFAULT_ADJACENCY)
        self.l4_path = Path(l4_path or DEFAULT_L4)
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
        self.l5_evidence: dict[str, dict[str, Any]] = {}
        # Query-text surfaces: surface -> {county: district node}. A surface with
        # more than one county needs a hint before it can resolve.
        self.text_surfaces: dict[str, dict[str, str]] = {}
        # Surfaces that are matched but never resolved, so that a longer blocked
        # form cannot be defeated by a shorter allowed one. surface -> note.
        self.blocked_surfaces: dict[str, dict[str, Any]] = {}
        self._text_pattern: re.Pattern[str] | None = None
        self._adjacency: dict[str, dict[str, GeoEdge]] = {}
        self._load()

    # ------------------------------------------------------------------ build

    @classmethod
    def from_environment(cls) -> GeoGraph:
        override = os.getenv("GEO_GRAPH_PATH")
        authored = os.getenv("GEO_AUTHORED_PATH")
        l5 = os.getenv("GEO_L5_PATH")
        adjacency = os.getenv("GEO_ADJACENCY_PATH")
        l4 = os.getenv("GEO_L4_PATH")
        return cls(
            Path(override) if override else None,
            Path(authored) if authored else None,
            Path(l5) if l5 else None,
            Path(adjacency) if adjacency else None,
            Path(l4) if l4 else None,
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
            self._load_l5()
            self._load_adjacency()
        self._load_text_surfaces()

    def _load_text_surfaces(self) -> None:
        """Index the surfaces a searcher might type, so text can name a district.

        `resolve` only reads filter codes, which leaves the commonest way of
        naming a place unhandled: typing it. This builds the surface index for
        that, from official district names plus every alias the authored and L5
        layers already registered.

        Two exclusions apply, documented on WORD_COLLISION_SURFACES and
        GENERIC_FULL_NAMES. An excluded surface is still put in the match pattern
        and resolved to nothing, because removing it outright does not exclude it:
        every text containing 新社區 also contains 新社, so dropping the long form
        while keeping the short one would let the block through unchanged. Keeping
        it in the pattern lets longest-match consume it and report why.
        """
        surfaces: dict[str, dict[str, str]] = {}
        blocked: dict[str, dict[str, Any]] = {}

        def add(surface: str, county: str, district: str) -> None:
            node = f"{county}/{district}"
            if surface and node in self.districts:
                surfaces.setdefault(surface, {})[county] = node

        def block(surface: str, reason: str, precision: float) -> None:
            blocked.setdefault(
                surface,
                {
                    "surface": surface,
                    "skipped": reason,
                    "job_corpus_precision": precision,
                    "source": "reports/job-district-extraction.json",
                },
            )

        # The graph's own node names are always indexed, so a deployment without
        # the L4 config still resolves official names and a test fixture does not
        # need the real table to exercise this path.
        for node, meta in self.districts.items():
            county, district = meta.get("county", ""), meta.get("district", "")
            if not county or not district:
                continue
            if district in GENERIC_FULL_NAMES:
                block(district, "generic_word", GENERIC_FULL_NAMES[district])
                continue
            add(district, county, district)

        # The table adds the suffix-dropped forms, which the node names do not
        # carry, and is the source of record for cross-county fan-out.
        try:
            payload = json.loads(self.l4_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.warning("L4 surface table unavailable: %s", type(exc).__name__)
            payload = {}
        layers = payload.get("surfaces", {})
        for layer, excluded, reason in (
            ("full_name", GENERIC_FULL_NAMES, "generic_word"),
            ("suffix_dropped", WORD_COLLISION_SURFACES, "word_collision"),
        ):
            for surface, per_county in (layers.get(layer) or {}).items():
                if surface in excluded:
                    block(surface, reason, excluded[surface])
                    continue
                for county, district in per_county.items():
                    add(surface, county, district)

        # A blocked string that some other layer also registers as a resolvable
        # surface stays resolvable: the alias and full-name layers each passed a
        # gate of their own, and the block is about one spelling, not the place.
        for surface in tuple(blocked):
            if surface in surfaces or surface in self.aliases:
                blocked.pop(surface)

        self.text_surfaces = surfaces
        self.blocked_surfaces = blocked
        # L3 living areas and L5 landmarks are matched too, but they are not part
        # of the county-keyed index and are never treated as ambiguous. 東區
        # naming a different district in four counties is ambiguity; 竹科 naming
        # 新竹市東區 and 新竹縣寶山鄉 is one place that spans two, and 北海岸 spans
        # five districts of a single county. Collapsing those by county would
        # silently keep whichever member happened to be read last.
        #
        # The 19 entries flagged requires_occurrence_filter are usable here: that
        # flag is about matching a bare substring in job text, not about a
        # searcher naming the place.
        ordered = sorted(
            set(surfaces) | set(self.aliases) | set(blocked), key=len, reverse=True
        )
        self._text_pattern = (
            re.compile("|".join(re.escape(surface) for surface in ordered))
            if ordered
            else None
        )

    def _load_adjacency(self) -> None:
        """Fill gaps in the behaviour graph with hand-authored land borders.

        Behaviour covers 355 of 368 districts; the rest are rural or offshore
        and nobody searches them, so the graph simply has nothing to say. A map
        does, and this supplies it - subject to the same rule the shortcuts
        follow, that an authored edge never overwrites a measured one.

        The weight is not invented. For each commute grade the median
        substitutability of adjacency pairs that *do* carry a behaviour edge is
        computed here, and pairs with no behaviour edge inherit that median. So
        an authored border is priced at what comparable measured borders turned
        out to be worth, and grades whose measured median is zero - `hard` and
        `impassable` - contribute no edge at all.
        """
        try:
            payload = json.loads(self.adjacency_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.warning("Adjacency map unavailable: %s", type(exc).__name__)
            return

        observed: dict[str, list[float]] = {}
        missing: list[tuple[str, str, str]] = []
        for edge in payload.get("edges", []):
            a, b = edge.get("a"), edge.get("b")
            if a not in self.districts or b not in self.districts:
                continue
            bucket = f"{edge.get('commute')}|{edge.get('scope')}"
            existing = self._adjacency.get(a, {}).get(b)
            if existing is not None and existing.provenance == BEHAVIOUR:
                observed.setdefault(bucket, []).append(existing.weight)
            elif existing is None:
                missing.append((a, b, bucket))

        calibration = {
            bucket: round(statistics.median(values), 5)
            for bucket, values in observed.items()
            if values
        }
        self.metadata["adjacency_calibration"] = calibration
        added = 0
        for a, b, bucket in missing:
            weight = calibration.get(bucket, 0.0)
            if weight <= 0.0:
                continue
            self._link(a, b, GeoEdge(weight=weight, provenance=AUTHORED, label="相鄰"))
            added += 1
        self.metadata["adjacency_edges_added"] = added

    def _load_l5(self) -> None:
        """Corpus-validated landmarks, stations, parks and arterial roads."""
        try:
            payload = json.loads(self.l5_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.warning("L5 table unavailable: %s", type(exc).__name__)
            return
        self.metadata["l5_gate"] = payload.get("gate")
        for entry in payload.get("entries", []):
            if "living_area" in (entry.get("kind") or ""):
                # L3 is registered from config/geo-authored.json, which owns it.
                # The L5 validator measures these surfaces for their text
                # evidence; registering them here as well would put the same
                # grouping in the graph twice under two different ids.
                self.l5_evidence[entry["surface"]] = {
                    "kind": entry.get("kind"),
                    "appearances": entry.get("appearances"),
                    "concentration": entry.get("concentration"),
                    "registered_as": "L3",
                }
                continue
            members = [d for d in entry.get("districts", []) if d in self.districts]
            if not members:
                continue
            self._register_group(
                entry["id"],
                "L5",
                members,
                aliases=[entry["surface"]],
            )
            self.l5_evidence[entry["surface"]] = {
                "kind": entry.get("kind"),
                "appearances": entry.get("appearances"),
                "concentration": entry.get("concentration"),
                "district_agreement": entry.get("district_agreement"),
            }

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

    def resolve_text(
        self, text: str, counties: Iterable[str] = ()
    ) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
        """Districts named in free query text, plus what was skipped and why.

        Returns `(districts, notes)`. Longest surface wins at each position, so
        八里區 is read as the district rather than as the 八里 short form.

        A surface naming districts in several counties does not resolve unless a
        hint narrows it: 東區 exists in four counties, and picking one would put
        a place in the searcher's mouth. `counties` accepts whatever the request
        already knows, such as the county behind a filter code or the location
        the normalizer read out of the text.

        Every skip is reported in `notes` rather than dropped silently, so the
        response can say why a place the searcher clearly typed did not expand.
        """
        if not self.enabled or not self._text_pattern or not text:
            return (), []
        hint = {name for name in counties if name}
        resolved: dict[str, None] = {}
        notes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for match in self._text_pattern.finditer(text):
            surface = match.group(0)
            if surface in seen:
                continue
            seen.add(surface)
            # Consulted before anything can resolve, and matched at full length
            # so a shorter allowed form inside a blocked one cannot defeat it.
            # A county hint does not lift a block. The hint for 新社區 業務 is
            # 台中市, but the normalizer read that county out of 新社區 itself, so
            # treating it as corroboration would just be the same reading twice.
            blocked = self.blocked_surfaces.get(surface)
            if blocked is not None:
                notes.append(dict(blocked))
                continue
            # A named group resolves to all of its members. It is one place, so
            # spanning several districts is the answer rather than a conflict.
            members = self.resolve_alias(surface)
            if members:
                for node in members:
                    resolved.setdefault(node, None)
                notes.append(
                    {
                        "surface": surface,
                        "districts": list(members),
                        "groups": list(self.aliases.get(surface, ())),
                        "evidence": self.l5_evidence.get(surface),
                    }
                )
                continue
            per_county = self.text_surfaces.get(surface, {})
            narrowed = {
                county: node for county, node in per_county.items() if county in hint
            }
            candidates = narrowed or per_county
            if len(candidates) > 1:
                notes.append(
                    {
                        "surface": surface,
                        "skipped": "ambiguous",
                        "counties": sorted(candidates),
                        "reason": "names a district in more than one county and no hint narrowed it",
                    }
                )
                continue
            for node in candidates.values():
                resolved.setdefault(node, None)
                notes.append(
                    {
                        "surface": surface,
                        "district": node,
                        # True when the surface named several counties and the
                        # hint picked one, so a reader can tell an unambiguous
                        # name from a choice the request made on their behalf.
                        "narrowed_by_hint": len(per_county) > 1 and bool(narrowed),
                        "evidence": self.l5_evidence.get(surface),
                    }
                )
        return tuple(resolved), notes

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

    def for_request(
        self,
        codes: Iterable[str] | None,
        locations: Mapping[str, Sequence[str]] | None = None,
        *,
        query: str = "",
        counties: Iterable[str] = (),
    ) -> GeoExpansion:
        """Read one request's geography, once.

        A district can arrive two ways. `codes` is the filter the caller sent,
        which only resolves when it is district-level. `query` is the text the
        searcher typed, which is how a place is usually named and which no code
        path can see.
        """
        if not self.enabled:
            return GeoExpansion()
        # Callers hand over whatever the request knows about location, which
        # includes 台灣 and district names as well as counties. Only county names
        # can narrow a surface, and a field called county_hint should not report
        # anything else.
        hint = tuple(
            dict.fromkeys(name for name in counties if name in self.county_districts)
        )
        from_codes = self.resolve(codes, locations)
        from_text, text_notes = self.resolve_text(query, hint)
        searched = tuple(dict.fromkeys((*from_codes, *from_text)))
        return GeoExpansion(
            from_codes=from_codes,
            from_text=from_text,
            notes=tuple(text_notes),
            county_hint=hint,
            reaches=tuple(self.expand(searched)),
        )

    def trace(
        self,
        codes: Iterable[str] | None,
        locations: Mapping[str, Sequence[str]] | None = None,
        *,
        query: str = "",
        counties: Iterable[str] = (),
        applied_to_ranking: bool = False,
    ) -> dict[str, Any] | None:
        """Convenience wrapper: read the request and render the payload."""
        return self.trace_payload(
            self.for_request(codes, locations, query=query, counties=counties),
            applied_to_ranking=applied_to_ranking,
        )

    def trace_payload(
        self, expansion: GeoExpansion, *, applied_to_ranking: bool = False
    ) -> dict[str, Any] | None:
        """`meta.geo_trace` for an expansion, or None when there is nothing to say.

        A payload is also returned when nothing resolved but a skip was recorded,
        because "東區 names four counties, so it was not expanded" is the answer
        to a question the searcher just asked. Silence would read as the graph
        having no opinion.

        `applied_to_ranking` is supplied by the caller rather than assumed here,
        because this object cannot know whether the expansion it describes was
        handed to candidate selection. Reporting it as applied when it was not,
        or the reverse, is the one error that would make the panel a lie.
        """
        if not self.enabled or not expansion:
            return None
        from_codes, from_text = expansion.from_codes, expansion.from_text
        searched = expansion.searched
        text_notes = list(expansion.notes)
        expansions = list(expansion.reaches)
        return {
            "schema": "skillweave-geo-graph-v1",
            "resolved_from": {
                "filter_codes": list(from_codes),
                "query_text": list(from_text),
            },
            "query_text_matches": text_notes,
            # Echoed so a hint-narrowed resolution can be checked against what
            # the request actually supplied, rather than taken on trust.
            "county_hint": list(expansion.county_hint),
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
            # True when candidate selection was given this expansion. It changes
            # the result set, so claiming otherwise would be false; it is also
            # not a positive weight, so `ranking_effect` states exactly what it
            # did rather than leaving a bare boolean to be over-read.
            "applied_to_ranking": bool(applied_to_ranking and searched),
            "ranking_effect": (
                "the out-of-area penalty is withheld from districts listed above; "
                "no positive weight is added, and substitutability is carried as "
                "an unweighted feature"
                if applied_to_ranking and searched
                else "none; this payload is evidence only"
            ),
            # No offline number backs the expansion: the benchmark reranks
            # candidate sets that are already county-filtered, so 84.3% of its
            # cases cannot express a cross-district substitution at all. This is
            # a recall change, reported as such and not as a measured lift.
            "offline_lift_measured": False,
        }


# --------------------------------------------------------------- spec parity

def build_geo_graph(
    base_path: Path | str | None = None,
    special_path: Path | str | None = None,
    cutoff_date: str = DEFAULT_CUTOFF,
    l5_path: Path | str | None = None,
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
        Path(l5_path) if l5_path else None,
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
