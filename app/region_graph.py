"""Inspectable region expansion backed by the behaviour-derived region graph.

`artifacts/region-graph.json` records which counties job seekers themselves
treat as interchangeable, measured two independent ways (search co-selection
and pre-cutoff application flow). This module turns that graph into a
read-only explanation surface: given the counties a search filtered on, it
reports which other counties the behaviour data supports, and why.

Two properties are deliberate.

Nothing here changes ranking. `applied_to_ranking` is always False. The graph
acts at retrieval expansion, and the offline benchmark is a re-ranking
benchmark whose candidate sets are already county-filtered, so wiring this
into the score would produce a feature with zero variance inside the
candidate group. `docs/evaluation-limits.md` records the measurement.

Output is deterministic for a given artifact. Expansions are ordered by a
total ordering with the county name as the final tie-break, so the same
request always renders the same explanation.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT = ROOT / "artifacts" / "region-graph.json"

# Below this share, fewer than one in twenty searchers of the source county
# also select the target county, which is not enough behavioural support to
# present the target as an alternative location.
DEFAULT_MIN_CONDITIONAL = 0.05
DEFAULT_LIMIT = 3

CO_SELECTION_GATE = "co_selection"
COMMUTE_FLOW_GATE = "commute_flow"


@dataclass(frozen=True)
class RegionExpansion:
    """One county the behaviour graph supports adding, with its evidence."""

    county: str
    source_county: str
    evidence: tuple[str, ...]
    co_selected: int | None = None
    jaccard: float | None = None
    conditional: float | None = None
    reverse_conditional: float | None = None
    applications: int | None = None
    reverse_applications: int | None = None
    asymmetry: float | None = None

    @property
    def explanation(self) -> str:
        if CO_SELECTION_GATE in self.evidence and self.conditional is not None:
            sentence = (
                f"{self.conditional:.1%} 搜尋{self.source_county}的求職者"
                f"同時勾選{self.county}"
            )
            if self.co_selected is not None:
                sentence += f"（{self.co_selected:,} 次共同勾選）"
            return sentence
        if COMMUTE_FLOW_GATE in self.evidence and self.applications is not None:
            return (
                f"{self.applications} 筆{self.source_county}求職者應徵"
                f"{self.county}職缺，反向僅 {self.reverse_applications} 筆"
            )
        return f"{self.source_county} 與 {self.county} 具行為關聯"

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "county": self.county,
            "from": self.source_county,
            "evidence": list(self.evidence),
            "explanation": self.explanation,
        }
        if CO_SELECTION_GATE in self.evidence or self.conditional is not None:
            body["co_selection"] = {
                "co_selected": self.co_selected,
                "jaccard": self.jaccard,
                "p_target_given_source": self.conditional,
                "p_source_given_target": self.reverse_conditional,
            }
        if self.applications is not None:
            body["commute_flow"] = {
                "applications": self.applications,
                "reverse_applications": self.reverse_applications,
                "asymmetry": self.asymmetry,
            }
        return body

    def sort_key(self) -> tuple[float, int, str]:
        return (
            -(self.conditional or 0.0),
            -(self.applications or 0),
            self.county,
        )


class RegionGraph:
    """Read-only view over `artifacts/region-graph.json`."""

    DEGRADED_COMPONENT = "region_graph"

    def __init__(
        self,
        artifact_path: Path | None = None,
        *,
        min_conditional: float = DEFAULT_MIN_CONDITIONAL,
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        self.artifact_path = Path(artifact_path) if artifact_path else DEFAULT_ARTIFACT
        self.min_conditional = float(min_conditional)
        self.limit = max(0, int(limit))
        self.metadata: dict[str, Any] = {}
        self.counties: frozenset[str] = frozenset()
        self._co_selection: dict[str, dict[str, dict[str, Any]]] = {}
        self._commutes: dict[str, dict[str, dict[str, Any]]] = {}
        self._load()

    @classmethod
    def from_environment(cls) -> RegionGraph:
        override = os.getenv("REGION_GRAPH_PATH")
        return cls(
            Path(override) if override else None,
            min_conditional=float(
                os.getenv("REGION_GRAPH_MIN_CONDITIONAL", DEFAULT_MIN_CONDITIONAL)
            ),
            limit=int(os.getenv("REGION_GRAPH_LIMIT", DEFAULT_LIMIT)),
        )

    def _load(self) -> None:
        try:
            payload = json.loads(self.artifact_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A deployment package without the artifact must still serve search.
            LOGGER.warning(
                "Region graph unavailable; region trace disabled: %s", type(exc).__name__
            )
            return
        self.metadata = payload.get("metadata", {})
        counties: set[str] = set()
        for edge in payload.get("substitutable_with", []):
            a, b = edge.get("a"), edge.get("b")
            if not a or not b:
                continue
            counties.update((a, b))
            shared = {"co_selected": edge.get("co_selected"), "jaccard": edge.get("jaccard")}
            # conditional_a_given_b is P(a | b), so it belongs to source b.
            self._co_selection.setdefault(b, {})[a] = {
                **shared,
                "conditional": edge.get("conditional_a_given_b"),
                "reverse_conditional": edge.get("conditional_b_given_a"),
            }
            self._co_selection.setdefault(a, {})[b] = {
                **shared,
                "conditional": edge.get("conditional_b_given_a"),
                "reverse_conditional": edge.get("conditional_a_given_b"),
            }
        for edge in payload.get("commutes_to", []):
            source, target = edge.get("source"), edge.get("target")
            if not source or not target:
                continue
            counties.update((source, target))
            self._commutes.setdefault(source, {})[target] = {
                "applications": edge.get("applications"),
                "reverse_applications": edge.get("reverse_applications"),
                "asymmetry": edge.get("asymmetry"),
            }
        self.counties = frozenset(counties)

    @property
    def enabled(self) -> bool:
        return bool(self.counties)

    def resolve(
        self, codes: Iterable[str] | None, locations: Mapping[str, Sequence[str]]
    ) -> tuple[str, ...]:
        """Map filter codes to counties using the index location lookup.

        `locations` orders names from most to least specific, for example
        `['東區', '新竹市', '台灣']`, so the first known county wins.
        """
        resolved: dict[str, None] = {}
        for code in codes or ():
            for name in locations.get(str(code), ()):
                if name in self.counties:
                    resolved.setdefault(name, None)
                    break
        return tuple(resolved)

    def expand(self, searched: Sequence[str]) -> list[RegionExpansion]:
        """Counties the behaviour graph supports adding to the searched set."""
        if not self.enabled or not searched:
            return []
        already = set(searched)
        best: dict[str, RegionExpansion] = {}
        for source in searched:
            targets = set(self._co_selection.get(source, {})) | set(
                self._commutes.get(source, {})
            )
            for target in targets:
                if target in already:
                    continue
                co = self._co_selection.get(source, {}).get(target, {})
                flow = self._commutes.get(source, {}).get(target, {})
                conditional = co.get("conditional")
                asymmetry = flow.get("asymmetry")
                gates: list[str] = []
                if conditional is not None and conditional >= self.min_conditional:
                    gates.append(CO_SELECTION_GATE)
                if asymmetry is not None and asymmetry > 0:
                    gates.append(COMMUTE_FLOW_GATE)
                if not gates:
                    continue
                candidate = RegionExpansion(
                    county=target,
                    source_county=source,
                    evidence=tuple(gates),
                    co_selected=co.get("co_selected"),
                    jaccard=co.get("jaccard"),
                    conditional=conditional,
                    reverse_conditional=co.get("reverse_conditional"),
                    applications=flow.get("applications"),
                    reverse_applications=flow.get("reverse_applications"),
                    asymmetry=asymmetry,
                )
                incumbent = best.get(target)
                if incumbent is None or candidate.sort_key() < incumbent.sort_key():
                    best[target] = candidate
        ordered = sorted(best.values(), key=RegionExpansion.sort_key)
        return ordered[: self.limit]

    def trace(
        self, codes: Iterable[str] | None, locations: Mapping[str, Sequence[str]]
    ) -> dict[str, Any] | None:
        """Region expansion payload for `meta.region_trace`, or None."""
        if not self.enabled:
            return None
        searched = self.resolve(codes, locations)
        if not searched:
            return None
        expansions = self.expand(searched)
        return {
            "schema": self.metadata.get("schema"),
            "dataset_version": self.metadata.get("dataset_version"),
            "graph_cutoff": self.metadata.get("graph_cutoff"),
            "searched_counties": list(searched),
            "min_conditional": self.min_conditional,
            "expansions": [expansion.payload() for expansion in expansions],
            # The graph acts at retrieval expansion. This response reports what
            # the behaviour data supports; it does not reorder any result.
            "applied_to_ranking": False,
        }
