from __future__ import annotations

import heapq
import json
import logging
import math
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.tree_ranker import PortableTreeRanker

_EN_TOKEN = re.compile(r"[a-z0-9][a-z0-9.+#/-]*")
_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[\s,，、/／|｜;；:：()（）\[\]【】{}「」『』·・_]+")
LOGGER = logging.getLogger(__name__)


def normalize(text: str | None) -> str:
    value = unicodedata.normalize("NFKC", text or "").lower().strip()
    value = value.replace("臺", "台")
    value = value.replace("react.js", "reactjs").replace("react js", "reactjs")
    value = value.replace("node js", "node.js").replace("nodejs", "node.js")
    value = value.replace("c sharp", "c#").replace("cplusplus", "c++")
    return _SPACE.sub(" ", value)


def lexical_units(text: str | None) -> set[str]:
    value = normalize(text)
    if not value:
        return set()
    parts = {part for part in _PUNCT.split(value) if part}
    parts.update(_EN_TOKEN.findall(value))
    han_runs = re.findall(r"[\u3400-\u9fff]+", value)
    for run in han_runs:
        parts.add(run)
        if len(run) > 1:
            parts.update(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) > 2:
            parts.update(run[i : i + 3] for i in range(len(run) - 2))
    return parts


def _as_codes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_as_codes(item))
        return result
    return [str(value)]


@dataclass(frozen=True)
class QueryIntent:
    raw: str
    normalized: str
    units: frozenset[str]
    skills: tuple[str, ...]
    location_codes: tuple[str, ...]
    duty_codes: tuple[str, ...]


class SkillWeaveRanker:
    """Dependency-free demo ranker mirroring the production feature contract.

    The checked-in demo artifact is intentionally small enough for a laptop.
    Production retrieval is expected to replace the scan with OpenSearch while
    retaining these feature names and the response contract.
    """

    def __init__(
        self,
        artifact_path: str | Path,
        graph_novelty_threshold: float = 10.0,
        ltr_model_path: str | Path | None = None,
        candidate_retriever: Any = None,
    ):
        self.artifact_path = Path(artifact_path)
        self.graph_novelty_threshold = max(0.1, float(graph_novelty_threshold))
        self.candidate_retriever = candidate_retriever
        self.ltr_model = (
            PortableTreeRanker(ltr_model_path)
            if ltr_model_path is not None and Path(ltr_model_path).is_file()
            else None
        )
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        with self.artifact_path.open(encoding="utf-8") as handle:
            artifact = json.load(handle)
        self.metadata = artifact["metadata"]
        self.jobs: list[dict[str, Any]] = artifact["jobs"]
        self.locations: dict[str, list[str]] = artifact.get("locations", {})
        self.duties: dict[str, list[str]] = artifact.get("duties", {})
        self.skills: dict[str, dict[str, Any]] = artifact["skills"]
        self.behavior_graph: dict[str, Any] = artifact.get("behavior_graph", {})
        behavior_sources = [
            self.behavior_graph,
            *self.behavior_graph.get("snapshots", {}).values(),
        ]
        for source in behavior_sources:
            totals = source.get("global_totals")
            if isinstance(totals, list) and len(totals) >= 3:
                continue
            job_global = source.get("job_global", {})
            source["global_totals"] = [
                sum(int(stats[index]) for stats in job_global.values())
                for index in range(3)
            ]
        self.query_job_edges: dict[str, dict[str, list[int]]] = (
            self.behavior_graph.get("query_job", {})
        )
        self.query_skill_edges: dict[str, dict[str, list[int]]] = (
            self.behavior_graph.get("query_skill", {})
        )
        self.alias_to_skill: dict[str, str] = {}
        self.alias_to_skills: dict[str, list[str]] = {}
        for skill_id, skill in self.skills.items():
            aliases = set(skill.get("aliases", []))
            aliases.add(skill.get("label", skill_id))
            aliases.add(skill_id)
            for alias in aliases:
                key = normalize(alias)
                if key:
                    self.alias_to_skill[key] = skill_id
                    self.alias_to_skills.setdefault(key, []).append(skill_id)
        alias_alternatives = "|".join(
            (
                rf"(?<![a-z0-9.+#]){re.escape(alias)}(?![a-z0-9.+#])"
                if alias.isascii()
                else re.escape(alias)
            )
            for alias in sorted(self.alias_to_skills, key=len, reverse=True)
        )
        self._alias_pattern = (
            re.compile(alias_alternatives, re.IGNORECASE)
            if alias_alternatives
            else re.compile(r"(?!x)x")
        )
        self._job_units: list[set[str]] = []
        self._job_norm: list[dict[str, str]] = []
        for job in self.jobs:
            fields = {
                "title": normalize(job.get("title")),
                "description": normalize(job.get("description")),
                "category": normalize(" ".join(job.get("categories", []))),
                "city": normalize(job.get("city")),
                "industry": normalize(job.get("industry")),
            }
            self._job_norm.append(fields)
            self._job_units.append(
                lexical_units(" ".join([fields["title"], fields["category"], fields["industry"]]))
            )

    def reload(self) -> None:
        with self._lock:
            self._load()

    def parse_intent(
        self,
        query: str,
        location_code: Any = None,
        duty_code: Any = None,
        normalized_query: str | None = None,
    ) -> QueryIntent:
        normalized_value = normalize(
            query if normalized_query is None else normalized_query
        )
        units = lexical_units(normalized_value)
        resolved: list[tuple[int, str]] = []
        for match in self._alias_pattern.finditer(normalized_value):
            alias = normalize(match.group(0))
            for skill_id in self.alias_to_skills.get(alias, []):
                resolved.append((len(alias), skill_id))
        # Longest aliases win and canonical nodes are unique.
        seen: set[str] = set()
        skills: list[str] = []
        for _, skill_id in sorted(resolved, reverse=True):
            if skill_id not in seen:
                seen.add(skill_id)
                skills.append(skill_id)
        seed_skills = [
            skill_id for skill_id in skills if not skill_id.startswith("duty.")
        ][:8]
        duty_skills = [
            skill_id for skill_id in skills if skill_id.startswith("duty.")
        ][:8]
        return QueryIntent(
            raw=query,
            normalized=normalized_value,
            units=frozenset(units),
            # Keep independently capped feature families. A large deterministic
            # duty taxonomy must never evict reviewed skill nodes and silently
            # contaminate a seed-only ablation.
            skills=tuple(seed_skills + duty_skills),
            location_codes=tuple(_as_codes(location_code)),
            duty_codes=tuple(_as_codes(duty_code)),
        )

    def _filter_names(self, codes: Iterable[str], lookup: dict[str, list[str]]) -> set[str]:
        names: set[str] = set()
        for code in codes:
            for name in lookup.get(code, []):
                names.add(normalize(name))
        return names

    def _graph_feature(
        self, intent: QueryIntent, job: dict[str, Any], include_graph: bool
    ) -> tuple[float, list[dict[str, Any]], list[str], dict[str, float]]:
        component_names = ("technical", "seed_occupation", "duty_occupation")
        empty_components = {
            **{name: 0.0 for name in component_names},
            "seed": 0.0,
            "seed_related": 0.0,
        }
        if not include_graph or not intent.skills or not job.get("graph_eligible", False):
            return 0.0, [], [], empty_components
        def component(skill_id: str) -> str:
            if skill_id.startswith("duty."):
                return "duty_occupation"
            if skill_id.startswith("occupation."):
                return "seed_occupation"
            return "technical"

        job_skills = set(job.get("skills", []))
        direct_score = 0.0
        best_related = 0.0
        direct_by_component = dict(empty_components)
        related_by_component = dict(empty_components)
        best_related_trace: dict[str, Any] | None = None
        traces: list[dict[str, Any]] = []
        direct_matches: list[str] = []
        for query_skill in intent.skills:
            query_component = component(query_skill)
            if query_skill in job_skills:
                confidence = float(job.get("skill_confidence", {}).get(query_skill, 0.82))
                contribution = 5.5 * confidence
                direct_score += contribution
                direct_by_component[query_component] += contribution
                direct_matches.append(query_skill)
                traces.append(
                    {
                        "path": [f"Query:{intent.raw}", f"Skill:{query_skill}", f"Job:{job['id']}"],
                        "edges": ["RESOLVES_TO", "REQUIRES"],
                        "weight": round(confidence, 3),
                        "evidence": job.get("skill_evidence", {}).get(query_skill, "structured field"),
                        "provenance": job.get(
                            "skill_provenance", {}
                        ).get(
                            query_skill,
                            self.skills.get(query_skill, {}).get(
                                "provenance", "reviewed_graph"
                            ),
                        ),
                    }
                )
                continue
            related = self.skills.get(query_skill, {}).get("related", {})
            for candidate_skill in job_skills:
                weight = float(related.get(candidate_skill, 0.0))
                if weight <= 0:
                    continue
                contribution = 2.6 * weight
                related_by_component[query_component] = max(
                    related_by_component[query_component], contribution
                )
                if contribution > best_related:
                    best_related = contribution
                    best_related_trace = {
                        "path": [
                            f"Query:{intent.raw}",
                            f"Skill:{query_skill}",
                            f"Skill:{candidate_skill}",
                            f"Job:{job['id']}",
                        ],
                        "edges": ["RESOLVES_TO", "RELATED_TO", "REQUIRES"],
                        "weight": round(weight, 3),
                        "evidence": self.skills.get(query_skill, {}).get("relation_evidence", {}).get(
                            candidate_skill, "validated ontology relation"
                        ),
                    }
        if best_related_trace is not None:
            traces.append(best_related_trace)
        # Related edges improve recall, but cannot overwhelm direct evidence.
        components = {
            name: min(direct_by_component[name], 8.5) + related_by_component[name]
            for name in component_names
        }
        components["seed"] = min(
            direct_by_component["technical"]
            + direct_by_component["seed_occupation"],
            8.5,
        ) + max(
            related_by_component["technical"],
            related_by_component["seed_occupation"],
        )
        components["seed_related"] = float(
            max(
                related_by_component["technical"],
                related_by_component["seed_occupation"],
            )
            > 0
        )
        return (
            min(direct_score, 8.5) + best_related,
            traces[:4],
            direct_matches,
            components,
        )

    def _score(
        self,
        index: int,
        intent: QueryIntent,
        include_graph: bool,
        behavior_snapshot_day: str | None = None,
    ) -> tuple[float, dict[str, float], list[dict[str, Any]], list[str]]:
        job = self.jobs[index]
        fields = self._job_norm[index]
        job_units = self._job_units[index]
        return self._score_job(
            job,
            fields,
            job_units,
            intent,
            include_graph,
            behavior_snapshot_day,
        )

    def _score_job(
        self,
        job: dict[str, Any],
        fields: dict[str, str],
        job_units: set[str],
        intent: QueryIntent,
        include_graph: bool,
        behavior_snapshot_day: str | None = None,
    ) -> tuple[float, dict[str, float], list[dict[str, Any]], list[str]]:
        q = intent.normalized

        exact_title = 1.0 if q and q == fields["title"] else 0.0
        title_phrase = 1.0 if q and q in fields["title"] else 0.0
        category_phrase = 1.0 if q and q in fields["category"] else 0.0
        description_phrase = 1.0 if q and q in fields["description"] else 0.0
        overlap = len(intent.units & job_units) / max(1, len(intent.units))
        title_overlap = len(intent.units & lexical_units(fields["title"])) / max(1, len(intent.units))

        lexical = (
            18.0 * exact_title
            + 10.0 * title_phrase
            + 6.0 * category_phrase
            + 2.0 * description_phrase
            + 7.0 * title_overlap
            + 3.5 * overlap
        )

        wanted_locations = self._filter_names(intent.location_codes, self.locations)
        location_match = 0.0
        if wanted_locations:
            location_match = 1.0 if any(name in fields["city"] for name in wanted_locations) else -1.0

        wanted_duties = self._filter_names(intent.duty_codes, self.duties)
        duty_match = 0.0
        if wanted_duties:
            duty_match = (
                1.0
                if any(name and (name in fields["category"] or name in fields["title"]) for name in wanted_duties)
                else -0.7
            )

        graph_raw, traces, direct_matches, graph_components = self._graph_feature(
            intent, job, include_graph
        )
        behavior_source = self.behavior_graph
        if behavior_snapshot_day:
            behavior_source = self.behavior_graph.get("snapshots", {}).get(
                behavior_snapshot_day, {}
            )
        query_job_edges = behavior_source.get("query_job", {})
        query_skill_edges = behavior_source.get("query_skill", {})
        job_global_edges = behavior_source.get("job_global", {})
        company_global_edges = behavior_source.get("company_global", {})
        global_totals = behavior_source.get("global_totals")
        if not isinstance(global_totals, list) or len(global_totals) < 3:
            global_totals = [0, 0, 0]
        query_job_stats = query_job_edges.get(q, {}).get(job["id"], [0, 0, 0])
        job_global_stats = job_global_edges.get(job["id"], [0, 0, 0])
        company_global_stats = company_global_edges.get(
            str(job.get("company_id", "")), [0, 0, 0]
        )
        query_skill_map = query_skill_edges.get(q, {})
        skill_stats = [
            query_skill_map[skill_id]
            for skill_id in job.get("skills", [])
            if skill_id in query_skill_map
        ]
        query_job_exposures = int(query_job_stats[0])
        query_job_positives = int(query_job_stats[1])
        query_job_grade_sum = int(query_job_stats[2])
        job_global_exposures = int(job_global_stats[0])
        job_global_positives = int(job_global_stats[1])
        job_global_grade_sum = int(job_global_stats[2])
        company_global_exposures = int(company_global_stats[0])
        company_global_positives = int(company_global_stats[1])
        company_global_grade_sum = int(company_global_stats[2])
        global_exposures = int(global_totals[0])
        global_positives = int(global_totals[1])
        global_grade_sum = int(global_totals[2])
        global_positive_prior = global_positives / max(1, global_exposures)
        global_grade_prior = global_grade_sum / max(1, 2 * global_exposures)
        query_skill_exposures = sum(int(stats[0]) for stats in skill_stats)
        query_skill_positives = sum(int(stats[1]) for stats in skill_stats)
        query_skill_grade_sum = sum(int(stats[2]) for stats in skill_stats)
        query_skill_max_positive_rate = max(
            (
                int(stats[1]) / max(1, int(stats[0]))
                for stats in skill_stats
            ),
            default=0.0,
        )
        # Graph should add information, not reward the same exact title evidence
        # twice. The novelty gate was selected on validation behavior: graph
        # contribution fades to zero as lexical confidence reaches 10.
        graph_novelty = max(0.0, 1.0 - lexical / self.graph_novelty_threshold)
        graph_score = graph_raw * graph_novelty
        related_path_count = sum(
            "RELATED_TO" in trace.get("edges", []) for trace in traces
        )
        apply_count = max(0, int(job.get("apply_count", 0)))
        view_count = max(0, int(job.get("view_count", 0)))
        behavior = min(2.5, 0.65 * math.log1p(apply_count) + 0.12 * math.log1p(view_count))
        freshness = max(0.0, min(1.0, float(job.get("freshness", 0.0))))

        features = {
            "lexical": round(lexical, 4),
            "graph": round(graph_score, 4),
            "graph_raw": round(graph_raw, 4),
            "technical_graph_raw": round(graph_components["technical"], 4),
            "seed_occupation_graph_raw": round(
                graph_components["seed_occupation"], 4
            ),
            "seed_graph_raw": round(graph_components["seed"], 4),
            "duty_occupation_graph_raw": round(
                graph_components["duty_occupation"], 4
            ),
            "graph_novelty": round(graph_novelty, 4),
            "direct_skill_count": float(len(direct_matches)),
            "llm_skill_match_count": float(
                sum(skill_id.startswith("skill.") for skill_id in direct_matches)
            ),
            "seed_occupation_match_count": float(
                sum(skill_id.startswith("occupation.") for skill_id in direct_matches)
            ),
            "duty_occupation_match_count": float(
                sum(skill_id.startswith("duty.") for skill_id in direct_matches)
            ),
            "related_path_count": float(related_path_count),
            "seed_related_path_count": graph_components["seed_related"],
            "exact_title": exact_title,
            "title_phrase": title_phrase,
            "category_phrase": category_phrase,
            "description_phrase": description_phrase,
            "query_unit_overlap": round(overlap, 4),
            "title_unit_overlap": round(title_overlap, 4),
            "location": round(2.8 if location_match > 0 else -16.0 if location_match < 0 else 0.0, 4),
            "duty": round(2.4 if duty_match > 0 else -10.0 if duty_match < 0 else 0.0, 4),
            "behavior": round(behavior, 4),
            "freshness": round(0.7 * freshness, 4),
            "post_cutoff_jd": float(
                job.get(
                    "post_cutoff_jd",
                    (
                        job.get("graph_source") != "train_eligible_jd"
                        if job.get("graph_source")
                        else not job.get("graph_eligible", False)
                    ),
                )
            ),
            "cold_start": float(not job.get("graph_eligible", False)),
            "job_skill_count": float(len(job.get("skills", []))),
            "technical_job_skill_count": float(
                sum(skill_id.startswith("skill.") for skill_id in job.get("skills", []))
            ),
            "seed_occupation_job_skill_count": float(
                sum(
                    skill_id.startswith("occupation.")
                    for skill_id in job.get("skills", [])
                )
            ),
            "duty_occupation_job_skill_count": float(
                sum(skill_id.startswith("duty.") for skill_id in job.get("skills", []))
            ),
            "seed_graph_cold_start": float(
                not any(
                    skill_id.startswith(("skill.", "occupation."))
                    for skill_id in job.get("skills", [])
                )
            ),
            "seed_job_skill_count": float(
                sum(
                    skill_id.startswith(("skill.", "occupation."))
                    for skill_id in job.get("skills", [])
                )
            ),
            "seed_direct_match_count": float(
                sum(
                    skill_id.startswith(("skill.", "occupation."))
                    for skill_id in direct_matches
                )
            ),
            "behavior_query_seen": float(q in query_job_edges),
            "behavior_query_job_seen": float(query_job_exposures > 0),
            "behavior_query_job_positive_rate": round(
                query_job_positives / max(1, query_job_exposures), 4
            ),
            "behavior_query_job_grade_rate": round(
                query_job_grade_sum / max(1, 2 * query_job_exposures), 4
            ),
            "behavior_query_job_exposures": float(query_job_exposures),
            "behavior_query_skill_seen_count": float(len(skill_stats)),
            "behavior_query_skill_positive_rate": round(
                query_skill_positives / max(1, query_skill_exposures), 4
            ),
            "behavior_query_skill_grade_rate": round(
                query_skill_grade_sum / max(1, 2 * query_skill_exposures), 4
            ),
            "behavior_query_skill_max_positive_rate": round(
                query_skill_max_positive_rate, 4
            ),
            "behavior_job_global_seen": float(job_global_exposures > 0),
            "behavior_job_global_exposures_log": round(
                math.log1p(job_global_exposures), 4
            ),
            "behavior_job_global_positive_rate": round(
                job_global_positives / max(1, job_global_exposures), 4
            ),
            "behavior_job_global_grade_rate": round(
                job_global_grade_sum / max(1, 2 * job_global_exposures), 4
            ),
            "behavior_job_global_positive_rate_smoothed": round(
                (
                    job_global_positives
                    + 5.0 * global_positive_prior
                )
                / (job_global_exposures + 5.0),
                4,
            ),
            "behavior_job_global_grade_rate_smoothed": round(
                (
                    job_global_grade_sum / 2.0
                    + 5.0 * global_grade_prior
                )
                / (job_global_exposures + 5.0),
                4,
            ),
            "behavior_company_global_seen": float(company_global_exposures > 0),
            "behavior_company_global_exposures_log": round(
                math.log1p(company_global_exposures), 4
            ),
            "behavior_company_global_positive_rate": round(
                company_global_positives / max(1, company_global_exposures), 4
            ),
            "behavior_company_global_grade_rate": round(
                company_global_grade_sum
                / max(1, 2 * company_global_exposures),
                4,
            ),
            "behavior_company_global_positive_rate_smoothed": round(
                (
                    company_global_positives
                    + 20.0 * global_positive_prior
                )
                / (company_global_exposures + 20.0),
                4,
            ),
            "behavior_company_global_grade_rate_smoothed": round(
                (
                    company_global_grade_sum / 2.0
                    + 20.0 * global_grade_prior
                )
                / (company_global_exposures + 20.0),
                4,
            ),
        }
        score = sum(
            features[name]
            for name in ["lexical", "graph", "location", "duty", "behavior", "freshness"]
        )
        return score, features, traces, direct_matches

    @staticmethod
    def _normalized_job_fields(
        job: dict[str, Any],
    ) -> tuple[dict[str, str], set[str]]:
        fields = {
            "title": normalize(job.get("title")),
            "description": normalize(job.get("description")),
            "category": normalize(" ".join(job.get("categories", []))),
            "city": normalize(job.get("city")),
            "industry": normalize(job.get("industry")),
        }
        units = lexical_units(
            " ".join([fields["title"], fields["category"], fields["industry"]])
        )
        return fields, units

    def _rerank(
        self,
        stage_one: list[
            tuple[
                float,
                str,
                dict[str, Any],
                dict[str, float],
                list[dict[str, Any]],
                list[str],
            ]
        ],
        *,
        limit: int,
        include_graph: bool,
    ) -> list[
        tuple[
            float,
            str,
            dict[str, Any],
            dict[str, float],
            list[dict[str, Any]],
            list[str],
        ]
    ]:
        if self.ltr_model is None:
            return stage_one[:limit]
        reranked = []
        for retrieval_rank, item in enumerate(stage_one, 1):
            item[3].setdefault(
                "retrieval_reciprocal_rank", 1.0 / retrieval_rank
            )
            ltr_score = self.ltr_model.predict(
                item[3], include_graph=include_graph
            )
            # The offline fixture reranks an organizer-provided retrieval
            # list. Keep a small deterministic title-relevance floor to limit
            # candidate-source distribution shift.
            online_score = (
                ltr_score
                + 0.30 * item[3]["title_unit_overlap"]
                + 0.05 * item[3]["exact_title"]
                + 0.01 * math.log1p(max(0.0, item[0]))
            )
            item[3]["stage_one_score"] = round(item[0], 4)
            item[3]["ltr_score"] = round(ltr_score, 6)
            item[3]["online_score"] = round(online_score, 6)
            reranked.append((online_score, *item[1:]))
        return sorted(reranked, key=lambda item: (-item[0], item[1]))[:limit]

    def search(
        self,
        query: str,
        location_code: Any = None,
        duty_code: Any = None,
        top_k: int = 20,
        include_graph: bool = True,
        candidate_ids: set[str] | None = None,
        normalized_query: str | None = None,
    ) -> dict[str, Any]:
        intent = self.parse_intent(
            query,
            location_code,
            duty_code,
            normalized_query=normalized_query,
        )
        if not intent.normalized:
            return {
                "intent": intent,
                "results": [],
                "candidate_source": "none",
                "degraded_components": [],
            }
        limit = max(1, min(int(top_k), 100))
        degraded_components: list[str] = []
        external_candidates: list[dict[str, Any]] | None = None
        if self.candidate_retriever is not None and candidate_ids is None:
            try:
                external_candidates = self.candidate_retriever.retrieve(
                    intent.normalized,
                    limit=max(200, limit),
                    location_names=self._filter_names(
                        intent.location_codes, self.locations
                    ),
                    duty_names=self._filter_names(intent.duty_codes, self.duties),
                )
            except Exception as exc:
                # Keep the API contract alive if full-corpus retrieval is
                # temporarily unavailable. The response metadata makes the
                # reduced fallback coverage explicit.
                LOGGER.warning(
                    "Full-corpus retrieval failed; using embedded fallback: %s",
                    type(exc).__name__,
                )
                degraded_components.append("opensearch")

        if external_candidates is not None:
            stage_one = []
            seen_ids: set[str] = set()
            for retrieval_rank, job in enumerate(external_candidates, 1):
                job_id = str(job.get("id", ""))
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                fields, job_units = self._normalized_job_fields(job)
                score, features, traces, direct = self._score_job(
                    job, fields, job_units, intent, include_graph
                )
                has_direct_candidate_evidence = bool(
                    set(intent.skills) & set(job.get("skills", []))
                )
                if features["lexical"] <= 0 and not has_direct_candidate_evidence:
                    continue
                if score <= 0:
                    continue
                features["retrieval_reciprocal_rank"] = 1.0 / retrieval_rank
                features["opensearch_score"] = float(
                    job.get("_retrieval_score", 0.0)
                )
                stage_one.append(
                    (score, job_id, job, features, traces, direct)
                )
            stage_one.sort(key=lambda item: (-item[0], item[1]))
            candidate_source = "opensearch_full_corpus"
        else:
            candidate_source = "embedded_12000_fallback"
            candidate_limit = max(limit, 100) if self.ltr_model is not None else limit
            heap: list[
                tuple[
                    float,
                    str,
                    dict[str, Any],
                    dict[str, float],
                    list[dict[str, Any]],
                    list[str],
                ]
            ] = []
            with self._lock:
                for index, job in enumerate(self.jobs):
                    if candidate_ids is not None and job["id"] not in candidate_ids:
                        continue
                    score, features, traces, direct = self._score(
                        index, intent, include_graph
                    )
                    # A graph neighbor is a feature, not sufficient candidate evidence.
                    # Require lexical overlap or an exact canonical skill match.
                    has_direct_candidate_evidence = bool(
                        set(intent.skills) & set(job.get("skills", []))
                    )
                    if (
                        features["lexical"] <= 0
                        and not has_direct_candidate_evidence
                    ):
                        continue
                    if score <= 0:
                        continue
                    item = (score, job["id"], job, features, traces, direct)
                    if len(heap) < candidate_limit:
                        heapq.heappush(heap, item)
                    elif item[:2] > heap[0][:2]:
                        heapq.heapreplace(heap, item)
            stage_one = sorted(heap, key=lambda item: (-item[0], item[1]))
        ranked = self._rerank(
            stage_one, limit=limit, include_graph=include_graph
        )
        results: list[dict[str, Any]] = []
        for rank, (score, _, job, features, traces, direct) in enumerate(ranked, 1):
            matched_labels = [
                self.skills[skill_id].get("label", skill_id)
                for skill_id in direct
                if skill_id in self.skills
            ]
            results.append(
                {
                    "job_id": job["id"],
                    "rank": rank,
                    "score": round(score, 4),
                    "title": job.get("title", ""),
                    "city": job.get("city", ""),
                    "salary": job.get("salary", ""),
                    "category": (job.get("categories") or [""])[-1],
                    "industry": job.get("industry", ""),
                    "matched_skills": matched_labels,
                    "why": self._explanation(features, matched_labels, job),
                    "features": features,
                    "graph_trace": traces,
                    "graph_eligible": bool(job.get("graph_eligible", False)),
                }
            )
        return {
            "intent": intent,
            "results": results,
            "candidate_source": candidate_source,
            "degraded_components": degraded_components,
        }

    @staticmethod
    def _explanation(
        features: dict[str, float], matched_skills: list[str], job: dict[str, Any]
    ) -> str:
        evidence: list[str] = []
        if features["lexical"] >= 8:
            evidence.append("職稱／職務文字高度吻合")
        elif features["lexical"] > 0:
            evidence.append("職務語意相符")
        if matched_skills:
            evidence.append("技能圖譜直接命中：" + "、".join(matched_skills[:3]))
        elif features["graph"] > 0:
            evidence.append("技能圖譜一跳關聯命中")
        if features["location"] > 0:
            evidence.append("地區條件吻合")
        if features["duty"] > 0:
            evidence.append("職務分類吻合")
        if features["behavior"] > 1:
            evidence.append("訓練期正向互動訊號")
        if not job.get("graph_eligible", False):
            evidence.append("新職缺：採冷啟動降級排序")
        return "；".join(evidence[:3]) or "綜合相關性排序"
