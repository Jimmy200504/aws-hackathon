from __future__ import annotations

import heapq
import inspect
import json
import logging
import math
import re
import threading
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from app.job_fields import is_remote_job, parse_salary_intent
from app.tree_ranker import PortableTreeRanker

_EN_TOKEN = re.compile(r"[a-z0-9][a-z0-9.+#/-]*")
_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[\s,，、/／|｜;；:：()（）\[\]【】{}「」『』·・_]+")
LOGGER = logging.getLogger(__name__)

# Same remote-work vocabulary used to derive `is_remote` at index time. A
# query containing one of these terms expresses remote-work intent, mirrored
# against userSearchLog_20260601_20260607.csv's most frequent `ks` values
# (遠端, 在家工作, 居家辦公, WFH, ...).
_REMOTE_QUERY_TERMS = (
    "遠端",
    "在家工作",
    "在家上班",
    "在家兼職",
    "居家辦公",
    "居家工作",
    "wfh",
    "work from home",
    "remote work",
    "remote job",
)


def wants_remote(normalized_query: str) -> bool:
    return any(term in normalized_query for term in _REMOTE_QUERY_TERMS)


def wants_salary(normalized_query: str) -> dict[str, Any] | None:
    """Parse an explicit salary condition (type + target number) from the
    normalized query, e.g. 月薪4萬以上, 時薪200, 年薪百萬. Returns None when
    the query does not state a salary type + number.
    """
    return parse_salary_intent(normalized_query)


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


def signed_match_score(value: float, reward: float, penalty: float) -> float:
    if value > 0:
        return reward
    if value < 0:
        return penalty
    return 0.0


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


def _as_strings(value: Any) -> list[str]:
    """Read a normalizer-supplied name list defensively.

    The structured intent crosses a process boundary as JSON, so a malformed or
    partially-parsed payload must degrade to "no inferred names" rather than
    raise inside ranking.
    """
    if not value or isinstance(value, (str, bytes, dict)):
        return []
    try:
        return [str(item).strip() for item in value if str(item).strip()]
    except TypeError:
        return []


def _as_name(value: Any) -> str:
    if not value or isinstance(value, (list, tuple, set, dict)):
        return ""
    return normalize(str(value))


def _as_confidence(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class QueryIntent:
    raw: str
    normalized: str
    units: frozenset[str]
    skills: tuple[str, ...]
    location_codes: tuple[str, ...]
    duty_codes: tuple[str, ...]
    wants_remote: bool = False
    salary_intent: dict[str, Any] | None = None
    # Read from the query text by the normalizer, as opposed to the codes the
    # caller supplied. Empty when normalization did not run, which keeps every
    # offline path that builds an intent without a normalizer unchanged.
    inferred_locations: tuple[str, ...] = ()
    inferred_duty_categories: tuple[str, ...] = ()
    inferred_company: str = ""
    # Carried as a feature rather than used as a cut-off. The override below is
    # unconditional because a location the user typed outranks a filter code,
    # but the ranker still sees how sure the normalizer was and can learn to
    # discount a low-confidence reading instead of obeying a fixed threshold.
    inferred_confidence: float = 0.0


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
        self.skill_blocked_phrases: dict[str, tuple[str, ...]] = {}
        for skill_id, skill in self.skills.items():
            aliases = set(skill.get("aliases", []))
            aliases.add(skill.get("label", skill_id))
            aliases.add(skill_id)
            for alias in aliases:
                key = normalize(alias)
                if key:
                    self.alias_to_skill[key] = skill_id
                    self.alias_to_skills.setdefault(key, []).append(skill_id)
            blocked_phrases = tuple(
                normalized
                for phrase in skill.get("blocked_phrases", [])
                if (normalized := normalize(phrase))
            )
            if blocked_phrases:
                self.skill_blocked_phrases[skill_id] = blocked_phrases
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
        structured_intent: Any = None,
    ) -> QueryIntent:
        normalized_value = normalize(
            query if normalized_query is None else normalized_query
        )
        units = lexical_units(normalized_value)
        resolved: list[tuple[int, str]] = []
        for match in self._alias_pattern.finditer(normalized_value):
            alias = normalize(match.group(0))
            for skill_id in self.alias_to_skills.get(alias, []):
                if any(
                    phrase in normalized_value
                    for phrase in self.skill_blocked_phrases.get(skill_id, ())
                ):
                    continue
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
            wants_remote=wants_remote(normalized_value),
            salary_intent=wants_salary(normalized_value),
            inferred_locations=tuple(
                normalize(name)
                for name in _as_strings(getattr(structured_intent, "locations", ()))
            ),
            inferred_duty_categories=tuple(
                normalize(name)
                for name in _as_strings(
                    getattr(structured_intent, "duty_categories", ())
                )
            ),
            inferred_company=_as_name(getattr(structured_intent, "company", "")),
            inferred_confidence=_as_confidence(
                getattr(structured_intent, "confidence", 0.0)
            ),
        )

    def _filter_names(self, codes: Iterable[str], lookup: dict[str, list[str]]) -> set[str]:
        names: set[str] = set()
        for code in codes:
            for name in lookup.get(code, []):
                names.add(normalize(name))
        return names

    def _graph_feature(
        self,
        intent: QueryIntent,
        job: dict[str, Any],
        include_graph: bool,
        external_relations: dict[str, dict[str, dict[str, Any]]] | None = None,
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
                        "path": [
                            f"Query:{intent.raw}",
                            f"Skill:{query_skill}",
                            f"Job:{job['id']}",
                        ],
                        "edges": ["RESOLVES_TO", "REQUIRES"],
                        "weight": round(confidence, 3),
                        "evidence": job.get("skill_evidence", {}).get(
                            query_skill, "structured field"
                        ),
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
            related: dict[str, Any] = (
                external_relations.get(query_skill, {})
                if external_relations is not None
                else self.skills.get(query_skill, {}).get("related", {})
            )
            for candidate_skill in job_skills:
                relation = related.get(candidate_skill, 0.0)
                relation_meta = relation if isinstance(relation, dict) else {}
                weight = float(relation_meta.get("weight", 0.0) if relation_meta else relation)
                if weight <= 0:
                    continue
                contribution = 2.6 * weight
                related_by_component[query_component] = max(
                    related_by_component[query_component], contribution
                )
                if contribution > best_related:
                    best_related = contribution
                    relation_evidence = relation_meta.get("evidence")
                    if not relation_evidence:
                        relation_evidence = self.skills.get(query_skill, {}).get(
                            "relation_evidence", {}
                        ).get(candidate_skill, "validated ontology relation")
                    best_related_trace = {
                        "path": [
                            f"Query:{intent.raw}",
                            f"Skill:{query_skill}",
                            f"Skill:{candidate_skill}",
                            f"Job:{job['id']}",
                        ],
                        "edges": [
                            "RESOLVES_TO",
                            relation_meta.get("relation_type", "RELATED_TO"),
                            "REQUIRES",
                        ],
                        "weight": round(weight, 3),
                        "edge_id": relation_meta.get("edge_id"),
                        "relation_confidence": relation_meta.get("confidence"),
                        "support_jobs": relation_meta.get("support_jobs"),
                        "support_companies": relation_meta.get("support_companies"),
                        "rules_version": relation_meta.get("rules_version"),
                        "corpus_hash": relation_meta.get("corpus_hash"),
                        "provenance": relation_meta.get(
                            "provenance", "reviewed_statistical_relation"
                        ),
                        "evidence": relation_evidence,
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
        external_relations: dict[str, dict[str, dict[str, Any]]] | None = None,
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
            external_relations,
        )

    def _score_job(
        self,
        job: dict[str, Any],
        fields: dict[str, str],
        job_units: set[str],
        intent: QueryIntent,
        include_graph: bool,
        behavior_snapshot_day: str | None = None,
        external_relations: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> tuple[float, dict[str, float], list[dict[str, Any]], list[str]]:
        q = intent.normalized

        exact_title = 1.0 if q and q == fields["title"] else 0.0
        title_phrase = 1.0 if q and q in fields["title"] else 0.0
        category_phrase = 1.0 if q and q in fields["category"] else 0.0
        description_phrase = 1.0 if q and q in fields["description"] else 0.0
        overlap = len(intent.units & job_units) / max(1, len(intent.units))
        title_overlap = len(
            intent.units & lexical_units(fields["title"])
        ) / max(1, len(intent.units))

        lexical = (
            18.0 * exact_title
            + 10.0 * title_phrase
            + 6.0 * category_phrase
            + 2.0 * description_phrase
            + 7.0 * title_overlap
            + 3.5 * overlap
        )

        # A location the user typed into the query outranks a filter code: the
        # code is often a leftover from a previous search or a default the user
        # never chose, while the text is a deliberate statement of intent. The
        # inferred names are already city names, so they bypass the code->name
        # lookup that `location_codes` needs.
        wanted_locations = set(intent.inferred_locations) or self._filter_names(
            intent.location_codes, self.locations
        )
        location_match = 0.0
        if wanted_locations:
            location_match = (
                1.0
                if any(name in fields["city"] for name in wanted_locations)
                else -1.0
            )

        wanted_duties = self._filter_names(intent.duty_codes, self.duties)
        duty_match = 0.0
        if wanted_duties:
            duty_match = (
                1.0
                if any(
                    name
                    and (
                        name in fields["category"]
                        or name in fields["title"]
                    )
                    for name in wanted_duties
                )
                else -0.7
            )

        # Remote-work is one of the most frequent explicit conditions in
        # search logs (遠端/在家工作/WFH) but is never present as a
        # structured field on the job posting itself, so it has to be
        # matched as an explicit query condition rather than folded into
        # generic lexical overlap.
        remote_match = 0.0
        if intent.wants_remote:
            remote_match = 1.0 if job.get("is_remote", False) else -1.0

        # Salary conditions (時薪200, 月薪4萬以上, 年薪百萬, ...) must be
        # compared against the job's structured salary_min/salary_max range,
        # not against whether the exact number happens to appear in the
        # title. A query target that falls anywhere in [salary_min,
        # salary_max] (or at/above salary_min when salary_max is unset,
        # e.g. "面議" floors) counts as a match; a job whose salary_type
        # does not match the query's salary_type (e.g. hourly vs monthly)
        # is not comparable and is neither rewarded nor penalized.
        salary_match = 0.0
        if intent.salary_intent is not None:
            job_salary_type = job.get("salary_type", "unknown")
            if job_salary_type == intent.salary_intent["salary_type"]:
                job_min = float(job.get("salary_min", 0.0) or 0.0)
                job_max = float(job.get("salary_max", 0.0) or 0.0)
                target = intent.salary_intent["target"]
                if job_min > 0 or job_max > 0:
                    upper_bound = job_max if job_max > 0 else job_min
                    if intent.salary_intent["comparator"] == "exact":
                        covers = job_min <= target <= upper_bound
                    else:
                        # "at_least": job seekers want *this number or more*.
                        # The job's own range must reach at or above the
                        # target somewhere in [job_min, upper_bound].
                        covers = upper_bound >= target
                    salary_match = 1.0 if covers else -1.0

        graph_raw, traces, direct_matches, graph_components = self._graph_feature(
            intent, job, include_graph, external_relations
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
            "location": signed_match_score(location_match, 2.8, -16.0),
            "duty": signed_match_score(duty_match, 2.4, -10.0),
            "remote": signed_match_score(remote_match, 2.4, -1.4),
            "salary": signed_match_score(salary_match, 2.4, -1.4),
            # Normalizer-derived evidence, exposed as plain 0/1 signals with no
            # hand-set magnitude. Until a model is trained on them these carry
            # no weight, which is deliberate: the retrieval-side boosts already
            # in production must not be double-counted by a guessed constant.
            "intent_duty_match": float(
                bool(intent.inferred_duty_categories)
                and any(
                    name in fields["category"] or name in fields["title"]
                    for name in intent.inferred_duty_categories
                )
            ),
            "intent_company_match": float(
                bool(intent.inferred_company)
                and (
                    intent.inferred_company in fields["title"]
                    or intent.inferred_company in fields["description"]
                )
            ),
            "intent_location_inferred": float(bool(intent.inferred_locations)),
            "intent_confidence": round(intent.inferred_confidence, 4),
            "is_remote": float(job.get("is_remote", False)),
            "salary_min": float(job.get("salary_min", 0.0) or 0.0),
            "salary_max": float(job.get("salary_max", 0.0) or 0.0),
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
            for name in ["lexical", "graph", "location", "duty", "remote", "salary", "behavior", "freshness"]
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
        resolved_skill_ids: Iterable[str] | None = None,
        external_relations: dict[str, dict[str, dict[str, Any]]] | None = None,
        structured_intent: Any = None,
    ) -> dict[str, Any]:
        intent = self.parse_intent(
            query,
            location_code,
            duty_code,
            normalized_query=normalized_query,
            structured_intent=structured_intent,
        )
        if resolved_skill_ids is not None:
            combined = tuple(dict.fromkeys((*resolved_skill_ids, *intent.skills)))[:16]
            # `replace` rather than a positional rebuild: the latter silently
            # dropped any field added to QueryIntent after it was written.
            intent = replace(intent, skills=combined)
        if not intent.normalized:
            return {
                "intent": intent,
                "results": [],
                "candidate_source": "none",
                "degraded_components": [],
            }
        limit = max(1, min(int(top_k), 100))
        stage_one, candidate_source, degraded_components = self._collect_stage_one(
            intent,
            structured_intent=structured_intent,
            limit=limit,
            include_graph=include_graph,
            candidate_ids=candidate_ids,
            external_relations=external_relations,
        )
        # Attribute and brand queries (現領, 萊爾富) name nothing that occurs in
        # job text, so the first pass comes back thin. Retry with the taxonomy
        # expansion only in that case: a query that already retrieves well must
        # not have five duty labels diluting its title-phrase evidence.
        expansion = getattr(structured_intent, "recall_expansion", "") or ""
        if expansion and len(stage_one) < limit:
            rescue_intent = self.parse_intent(
                query,
                location_code,
                duty_code,
                normalized_query=expansion,
                structured_intent=structured_intent,
            )
            rescue, rescue_source, rescue_degraded = self._collect_stage_one(
                rescue_intent,
                structured_intent=structured_intent,
                limit=limit,
                include_graph=include_graph,
                candidate_ids=candidate_ids,
                external_relations=external_relations,
            )
            seen = {item[1] for item in stage_one}
            stage_one = stage_one + [item for item in rescue if item[1] not in seen]
            candidate_source = f"{rescue_source}+taxonomy_expansion"
            degraded_components = list(
                dict.fromkeys(degraded_components + rescue_degraded)
            )
        ranked = self._rerank(stage_one, limit=limit, include_graph=include_graph)
        return self._assemble(
            intent, ranked, candidate_source, degraded_components
        )

    def _collect_stage_one(
        self,
        intent: QueryIntent,
        *,
        structured_intent: Any,
        limit: int,
        include_graph: bool,
        candidate_ids: set[str] | None,
        external_relations: dict[str, dict[str, dict[str, Any]]] | None,
    ) -> tuple[list[Any], str, list[str]]:
        degraded_components: list[str] = []
        external_candidates: list[dict[str, Any]] | None = None
        if self.candidate_retriever is not None and candidate_ids is None:
            try:
                retrieve = self.candidate_retriever.retrieve
                parameters = inspect.signature(retrieve).parameters
                supports_kwargs = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                optional_retrieval_kwargs = {
                    "wants_remote": intent.wants_remote,
                    "salary_intent": intent.salary_intent,
                    "intent": structured_intent,
                }
                if not supports_kwargs:
                    optional_retrieval_kwargs = {
                        name: value
                        for name, value in optional_retrieval_kwargs.items()
                        if name in parameters
                    }
                external_candidates = retrieve(
                    intent.normalized,
                    limit=max(200, limit),
                    location_names=self._filter_names(
                        intent.location_codes, self.locations
                    ),
                    duty_names=self._filter_names(intent.duty_codes, self.duties),
                    **optional_retrieval_kwargs,
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
                    job, fields, job_units, intent, include_graph,
                    external_relations=external_relations,
                )
                has_direct_candidate_evidence = bool(
                    set(intent.skills) & set(job.get("skills", []))
                ) or features["salary"] > 0
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
                        index, intent, include_graph,
                        external_relations=external_relations,
                    )
                    # A graph neighbor is a feature, not sufficient candidate evidence.
                    # Require lexical overlap, an exact canonical skill match, or a
                    # structured salary-range match (e.g. 時薪210 covered by a job's
                    # salary_min/salary_max even when the title never literally says
                    # "210").
                    has_direct_candidate_evidence = bool(
                        set(intent.skills) & set(job.get("skills", []))
                    ) or features["salary"] > 0
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
        return stage_one, candidate_source, degraded_components

    def _assemble(
        self,
        intent: QueryIntent,
        ranked: list[Any],
        candidate_source: str,
        degraded_components: list[str],
    ) -> dict[str, Any]:
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
                    "salary_min": job.get("salary_min", 0.0),
                    "salary_max": job.get("salary_max", 0.0),
                    "salary_type": job.get("salary_type", "unknown"),
                    "is_remote": bool(job.get("is_remote", False)),
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
        if features["remote"] > 0:
            evidence.append("遠端／在家工作條件吻合")
        if features["salary"] > 0:
            evidence.append("薪資範圍符合查詢條件")
        if features["behavior"] > 1:
            evidence.append("訓練期正向互動訊號")
        if not job.get("graph_eligible", False):
            evidence.append("新職缺：採冷啟動降級排序")
        return "；".join(evidence[:3]) or "綜合相關性排序"
