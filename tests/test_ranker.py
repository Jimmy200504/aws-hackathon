from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.ranker import SkillWeaveRanker, normalize


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / "artifacts" / "demo-index.json").is_file(), "demo index missing")
class RankerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ranker = SkillWeaveRanker(ROOT / "artifacts" / "demo-index.json")

    def test_short_js_alias_does_not_match_inside_nodejs(self) -> None:
        intent = self.ranker.parse_intent("Node.js 後端工程師")
        self.assertIn("skill.nodejs", intent.skills)
        self.assertNotIn("skill.javascript", intent.skills)

    def test_explicit_normalized_query_preserves_raw_query_for_trace(self) -> None:
        intent = self.ranker.parse_intent(
            "node js backend",
            normalized_query="Node.js 後端工程師",
        )
        self.assertEqual(intent.raw, "node js backend")
        # The scoring surface is the normalized query, because that is what
        # serving_query_key() keys the behavior graph with.
        self.assertEqual(intent.normalized, "node.js 後端工程師")
        # The rewrite is retained for provenance and widens alias resolution.
        self.assertEqual(intent.llm_surface, "node.js 後端工程師")
        self.assertIn("skill.nodejs", intent.skills)
        self.assertIn("occupation.backend", intent.skills)
        self.assertIn("occupation.backend", intent.llm_only_skills)

    def test_rank_is_contiguous_and_job_ids_are_unique(self) -> None:
        rows = self.ranker.search("行政助理", top_k=20)["results"]
        self.assertGreaterEqual(len(rows), 10)
        self.assertEqual([row["rank"] for row in rows], list(range(1, len(rows) + 1)))
        self.assertEqual(len({row["job_id"] for row in rows}), len(rows))

    def test_location_condition_controls_top_results(self) -> None:
        rows = self.ranker.search(
            "行政助理", location_code=["100200"], top_k=10
        )["results"]
        self.assertTrue(rows)
        self.assertTrue(all(row["city"] == "新北市" for row in rows[:5]))

    def test_nodejs_result_has_direct_evidence(self) -> None:
        rows = self.ranker.search("後端工程師 Node.js", top_k=5)["results"]
        self.assertTrue(rows)
        self.assertTrue(
            "node" in rows[0]["title"].lower()
            or "Node.js" in rows[0]["matched_skills"]
        )

    def test_future_modified_jobs_have_no_graph_edges(self) -> None:
        future = [job for job in self.ranker.jobs if not job["graph_eligible"]]
        self.assertTrue(future)
        self.assertTrue(all(not job["skills"] for job in future))

    def test_empty_query_returns_no_results(self) -> None:
        self.assertEqual(self.ranker.search("   ")["results"], [])

    def test_graph_toggle_changes_cloud_skill_ranking(self) -> None:
        baseline = self.ranker.search(
            "AWS Docker Kubernetes",
            top_k=10,
            include_graph=False,
        )["results"]
        graph = self.ranker.search(
            "AWS Docker Kubernetes",
            top_k=10,
            include_graph=True,
        )["results"]
        self.assertNotEqual(
            [row["job_id"] for row in baseline],
            [row["job_id"] for row in graph],
        )
        self.assertTrue(any(row["features"]["graph"] > 0 for row in graph))

    def test_results_expose_salary_and_remote_fields(self) -> None:
        rows = self.ranker.search("行政助理", top_k=5)["results"]
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("salary_min", row)
            self.assertIn("salary_max", row)
            self.assertIn("salary_type", row)
            self.assertIn("is_remote", row)
            self.assertIsInstance(row["is_remote"], bool)



class QueryNormalizationBoundaryTests(unittest.TestCase):
    """Where a query rewrite is allowed to reach, and where it must not.

    The rewrite is the scoring surface: scripts/build_benchmark_fixture.py keys
    the train-only behavior graph with serving_query_key(), which is normalize()
    over the normalizer output, so the ranker has to look those edges up with the
    same string. A mismatch is silent, and reads zero for the entire
    behavior_query_* family rather than raising.

    Alias resolution is the separate concern. It reads the raw surface as well,
    so a rewrite can only add canonical nodes and never drop one the raw query
    would have matched.
    """

    def setUp(self) -> None:
        artifact = {
            "metadata": {"index_version": "test"},
            "locations": {},
            "duties": {},
            "skills": {
                "skill.nodejs": {
                    "label": "Node.js",
                    "aliases": ["node.js", "nodejs"],
                    "related": {},
                },
                "occupation.teacher": {
                    "label": "美語老師",
                    "aliases": ["english teacher", "美語老師"],
                    "related": {},
                },
            },
            "behavior_graph": {
                "query_job": {"美語老師": {"job-1": [4, 3, 5]}},
                "query_skill": {"美語老師": {"occupation.teacher": [4, 3, 5]}},
                "job_global": {"job-1": [6, 4, 6]},
                "company_global": {"c1": [9, 5, 7]},
                "global_totals": [30, 8, 12],
            },
            "jobs": [
                {
                    "id": "job-1",
                    "title": "美語老師",
                    "description": "",
                    "categories": [],
                    "city": "",
                    "industry": "",
                    "company_id": "c1",
                    "graph_eligible": True,
                    "skills": ["occupation.teacher"],
                    "skill_confidence": {"occupation.teacher": 0.96},
                    "skill_evidence": {"occupation.teacher": "美語老師"},
                    "view_count": 0,
                    "apply_count": 0,
                    "freshness": 0,
                }
            ],
        }
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "index.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        self.ranker = SkillWeaveRanker(path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _features(self, normalized_query=None) -> dict[str, float]:
        intent = self.ranker.parse_intent("美語老師", normalized_query=normalized_query)
        _, features, _, _ = self.ranker._score(0, intent, include_graph=True)
        return features

    def test_scoring_surface_is_the_key_the_fixture_builder_writes(self) -> None:
        """Pin the one thing whose failure mode is silence.

        scripts/build_benchmark_fixture.py composes its behavior-graph keys as
        normalize(normalizer_output). If parse_intent ever keys on the raw query
        again, every rewritten query misses and behavior_query_* reads zero with
        no error, so assert the composition rather than trusting a comment.
        """
        rewrite = "美語老師 english teacher"
        intent = self.ranker.parse_intent("美語老師", normalized_query=rewrite)
        self.assertEqual(intent.normalized, normalize(rewrite))

    def test_behavior_edges_are_hit_when_keyed_on_the_rewrite(self) -> None:
        rewrite = "美語老師 english teacher"
        key = normalize(rewrite)
        graph = self.ranker.behavior_graph
        graph["query_job"][key] = {"job-1": [4, 3, 5]}
        graph["query_skill"][key] = {"occupation.teacher": [4, 3, 5]}
        features = self._features(rewrite)
        self.assertEqual(features["behavior_query_job_seen"], 1.0)
        self.assertEqual(features["behavior_query_skill_seen_count"], 1.0)

    def test_a_rewrite_does_not_zero_the_behavior_family_by_accident(self) -> None:
        """A rewrite with no matching edge must degrade, not corrupt.

        The raw-keyed edge in the fixture is deliberately not re-keyed here, so
        this is the miss case: the features read zero, every other family still
        computes, and the response stays well-formed.
        """
        rewritten = self._features("完全不同的字串")
        self.assertEqual(rewritten["behavior_query_job_seen"], 0.0)
        self.assertEqual(self._features()["behavior_query_job_seen"], 1.0)

    def test_rewrite_can_only_add_resolved_skills(self) -> None:
        baseline = self.ranker.parse_intent("美語老師")
        rewritten = self.ranker.parse_intent(
            "美語老師", normalized_query="美語老師 Node.js"
        )
        self.assertLessEqual(set(baseline.skills), set(rewritten.skills))
        self.assertIn("skill.nodejs", rewritten.skills)
        self.assertEqual(rewritten.llm_only_skills, ("skill.nodejs",))

    def test_rewrite_that_drops_a_term_keeps_the_raw_resolution(self) -> None:
        rewritten = self.ranker.parse_intent("美語老師", normalized_query="teaching job")
        self.assertIn("occupation.teacher", rewritten.skills)
        self.assertEqual(rewritten.llm_only_skills, ())

    def test_identical_rewrite_is_not_recorded_as_an_llm_surface(self) -> None:
        intent = self.ranker.parse_intent("美語老師", normalized_query="美語老師")
        self.assertIsNone(intent.llm_surface)
        self.assertEqual(intent.llm_only_skills, ())


class LlmProvenanceIsolationTests(unittest.TestCase):
    """The generative-AI contribution must be measurable on its own.

    If a Bedrock-extracted node leaks into the reviewed-seed feature family, no
    downstream ablation can separate "the graph helps" from "the LLM helps",
    which is exactly the claim the theme gate asks about.
    """

    def setUp(self) -> None:
        artifact = {
            "metadata": {"index_version": "test"},
            "locations": {},
            "duties": {},
            "skills": {
                "skill.python": {
                    "label": "Python",
                    "aliases": ["python"],
                    "related": {},
                },
                "bedrock.deadbeef": {
                    "label": "crop cultivation",
                    "aliases": ["作物種植"],
                    "related": {},
                    "provenance": "amazon_bedrock_structured_extraction",
                },
            },
            "behavior_graph": {},
            "jobs": [
                {
                    "id": "job-seed-only",
                    "title": "Python engineer",
                    "description": "",
                    "categories": [],
                    "city": "",
                    "industry": "",
                    "company_id": "c1",
                    "graph_eligible": True,
                    "skills": ["skill.python"],
                    "skill_confidence": {"skill.python": 0.9},
                    "skill_evidence": {"skill.python": "Python engineer"},
                    "view_count": 0,
                    "apply_count": 0,
                    "freshness": 0,
                },
                {
                    "id": "job-llm-only",
                    "title": "作物種植 專員",
                    "description": "",
                    "categories": [],
                    "city": "",
                    "industry": "",
                    "company_id": "c1",
                    "graph_eligible": True,
                    "skills": ["bedrock.deadbeef"],
                    "skill_confidence": {"bedrock.deadbeef": 0.9},
                    "skill_evidence": {"bedrock.deadbeef": "負責作物種植"},
                    "view_count": 0,
                    "apply_count": 0,
                    "freshness": 0,
                },
            ],
        }
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "index.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        self.ranker = SkillWeaveRanker(path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _features(self, query: str, index: int) -> dict[str, float]:
        intent = self.ranker.parse_intent(query)
        _, features, _, _ = self.ranker._score(index, intent, include_graph=True)
        return features

    def test_seed_hit_does_not_populate_llm_family(self) -> None:
        features = self._features("python", 0)
        self.assertGreater(features["seed_graph_raw"], 0)
        self.assertEqual(features["seed_direct_match_count"], 1.0)
        self.assertEqual(features["llm_graph_raw"], 0.0)
        self.assertEqual(features["llm_direct_match_count"], 0.0)
        self.assertEqual(features["llm_job_skill_count"], 0.0)

    def test_llm_hit_does_not_populate_seed_family(self) -> None:
        features = self._features("作物種植", 1)
        self.assertGreater(features["llm_graph_raw"], 0)
        self.assertEqual(features["llm_direct_match_count"], 1.0)
        self.assertEqual(features["llm_job_skill_count"], 1.0)
        self.assertEqual(features["seed_graph_raw"], 0.0)
        self.assertEqual(features["seed_direct_match_count"], 0.0)
        self.assertEqual(features["seed_job_skill_count"], 0.0)

    def test_llm_family_is_disjoint_from_release_feature_set(self) -> None:
        from pipeline.train_ltr import (
            LLM_GRAPH_FEATURES,
            QUALITY_MINIMAL_FEATURES,
        )

        self.assertFalse(
            set(LLM_GRAPH_FEATURES) & set(QUALITY_MINIMAL_FEATURES),
            "the frozen release feature set must not gain LLM features",
        )


class GraphIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        skills = {
            "skill.python": {
                "label": "Python",
                "aliases": ["python"],
                "related": {},
            },
            "occupation.software": {
                "type": "Occupation",
                "label": "軟體工程師",
                "aliases": ["software engineer", "軟體工程師"],
                "related": {},
            },
        }
        for index in range(12):
            skills[f"duty.{index}"] = {
                "label": f"Python duty {index}",
                "aliases": ["python"],
                "related": {},
            }
        artifact = {
            "metadata": {"index_version": "test"},
            "locations": {},
            "duties": {},
            "skills": skills,
            "behavior_graph": {
                "query_job": {"python": {"job-1": [2, 1, 2]}},
                "query_skill": {"python": {"skill.python": [2, 1, 2]}},
                "job_global": {"job-1": [4, 2, 3]},
                "company_global": {"company-1": [8, 3, 5]},
                "global_totals": [20, 4, 6],
                "snapshots": {
                    "2026-06-01": {
                        "query_job": {},
                        "query_skill": {},
                        "job_global": {},
                        "company_global": {},
                        "global_totals": [0, 0, 0],
                    }
                },
            },
            "jobs": [
                {
                    "id": "job-1",
                    "title": "Python engineer",
                    "description": "",
                    "categories": [],
                    "city": "",
                    "industry": "",
                    "company_id": "company-1",
                    "graph_eligible": True,
                    "skills": ["skill.python", "occupation.software"],
                    "skill_confidence": {
                        "skill.python": 0.9,
                        "occupation.software": 1.0,
                    },
                    "skill_evidence": {
                        "skill.python": "Python engineer",
                        "occupation.software": "software engineer",
                    },
                    "view_count": 0,
                    "apply_count": 0,
                    "freshness": 0,
                }
            ],
        }
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "index.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        self.ranker = SkillWeaveRanker(path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_large_duty_taxonomy_cannot_evict_seed_skill(self) -> None:
        intent = self.ranker.parse_intent("python")
        self.assertIn("skill.python", intent.skills)
        self.assertEqual(
            sum(skill.startswith("duty.") for skill in intent.skills), 8
        )

    def test_rolling_behavior_snapshot_excludes_same_day_edges(self) -> None:
        intent = self.ranker.parse_intent("python")
        _, full, _, _ = self.ranker._score(0, intent, include_graph=True)
        _, day_one, _, _ = self.ranker._score(
            0,
            intent,
            include_graph=True,
            behavior_snapshot_day="2026-06-01",
        )
        self.assertEqual(full["behavior_query_job_seen"], 1.0)
        self.assertEqual(day_one["behavior_query_job_seen"], 0.0)
        self.assertEqual(full["behavior_job_global_seen"], 1.0)
        self.assertEqual(full["behavior_company_global_seen"], 1.0)
        self.assertEqual(day_one["behavior_job_global_seen"], 0.0)
        self.assertEqual(day_one["behavior_company_global_seen"], 0.0)

    def test_graph_trace_keeps_canonical_ids_and_adds_display_names(self) -> None:
        row = self.ranker.search("python", top_k=1)["results"][0]
        trace = next(
            item
            for item in row["graph_trace"]
            if "Skill:skill.python" in item["path"]
        )
        self.assertIn("Skill:skill.python", trace["path"])
        self.assertIn("Skill:Python", trace["display_path"])
        self.assertEqual(trace["edge_directions"], ["forward", "reverse"])

    def test_occupation_trace_has_typed_node_and_job_to_occupation_direction(self) -> None:
        row = self.ranker.search("software engineer", top_k=1)["results"][0]
        trace = next(
            item
            for item in row["graph_trace"]
            if "Occupation:occupation.software" in item["path"]
        )
        self.assertEqual(
            trace["display_path"],
            ["Query:software engineer", "Occupation:軟體工程師", "Job:job-1"],
        )
        self.assertEqual(trace["edges"], ["RESOLVES_TO", "INSTANCE_OF"])
        self.assertEqual(trace["edge_directions"], ["forward", "reverse"])


class RemoteWorkFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        artifact = {
            "metadata": {"index_version": "test"},
            "locations": {},
            "duties": {},
            "skills": {
                "skill.python": {
                    "label": "Python",
                    "aliases": ["python"],
                    "related": {},
                }
            },
            "behavior_graph": {},
            "jobs": [
                {
                    "id": "job-remote",
                    "title": "Python 後端工程師",
                    "description": "全遠端工作，需自備電腦",
                    "categories": ["軟體工程"],
                    "city": "台北市",
                    "industry": "資訊軟體",
                    "company_id": "company-1",
                    "graph_eligible": True,
                    "skills": ["skill.python"],
                    "skill_confidence": {"skill.python": 0.9},
                    "skill_evidence": {"skill.python": "Python 後端工程師"},
                    "view_count": 0,
                    "apply_count": 0,
                    "freshness": 0,
                    "salary_min": 50000.0,
                    "salary_max": 70000.0,
                    "salary_type": "monthly",
                    "is_remote": True,
                },
                {
                    "id": "job-onsite",
                    "title": "Python 後端工程師",
                    "description": "需至台北市辦公室上班",
                    "categories": ["軟體工程"],
                    "city": "台北市",
                    "industry": "資訊軟體",
                    "company_id": "company-2",
                    "graph_eligible": True,
                    "skills": ["skill.python"],
                    "skill_confidence": {"skill.python": 0.9},
                    "skill_evidence": {"skill.python": "Python 後端工程師"},
                    "view_count": 0,
                    "apply_count": 0,
                    "freshness": 0,
                    "salary_min": 50000.0,
                    "salary_max": 70000.0,
                    "salary_type": "monthly",
                    "is_remote": False,
                },
            ],
        }
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "index.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        self.ranker = SkillWeaveRanker(path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_remote_query_ranks_remote_job_first(self) -> None:
        rows = self.ranker.search("Python 後端工程師 遠端", top_k=10)["results"]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["job_id"], "job-remote")
        self.assertTrue(rows[0]["is_remote"])
        remote_row = next(row for row in rows if row["job_id"] == "job-remote")
        onsite_row = next(row for row in rows if row["job_id"] == "job-onsite")
        self.assertGreater(
            remote_row["features"]["remote"], onsite_row["features"]["remote"]
        )

    def test_non_remote_query_does_not_penalize_onsite_job(self) -> None:
        rows = self.ranker.search("Python 後端工程師", top_k=10)["results"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["features"]["remote"], 0.0)

    def test_intent_detects_remote_terms(self) -> None:
        self.assertTrue(self.ranker.parse_intent("遠端 python 工程師").wants_remote)
        self.assertTrue(self.ranker.parse_intent("在家工作 客服").wants_remote)
        self.assertFalse(self.ranker.parse_intent("python 工程師").wants_remote)


class SalaryRangeFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        artifact = {
            "metadata": {"index_version": "test"},
            "locations": {},
            "duties": {},
            "skills": {},
            "behavior_graph": {},
            "jobs": [
                {
                    # Range covers the query target (210) but the title
                    # never literally says "210" -- this is exactly the
                    # recall gap the salary feature must close.
                    "id": "job-covers-no-literal-210",
                    "title": "居家照顧服務員",
                    "description": "時薪範圍依經驗調整",
                    "categories": ["居家照顧"],
                    "city": "",
                    "industry": "",
                    "company_id": "company-1",
                    "graph_eligible": True,
                    "skills": [],
                    "view_count": 0,
                    "apply_count": 0,
                    "freshness": 0,
                    "salary_min": 200.0,
                    "salary_max": 300.0,
                    "salary_type": "hourly",
                    "is_remote": False,
                },
                {
                    # Below the target: should not surface for 時薪210.
                    "id": "job-below-target",
                    "title": "洗碗人員",
                    "description": "",
                    "categories": [],
                    "city": "",
                    "industry": "",
                    "company_id": "company-2",
                    "graph_eligible": True,
                    "skills": [],
                    "view_count": 0,
                    "apply_count": 0,
                    "freshness": 0,
                    "salary_min": 180.0,
                    "salary_max": 190.0,
                    "salary_type": "hourly",
                    "is_remote": False,
                },
                {
                    # Different salary_type (monthly): not comparable to an
                    # hourly query, must be neither rewarded nor penalized.
                    "id": "job-different-type",
                    "title": "行政助理",
                    "description": "",
                    "categories": [],
                    "city": "",
                    "industry": "",
                    "company_id": "company-3",
                    "graph_eligible": True,
                    "skills": [],
                    "view_count": 0,
                    "apply_count": 0,
                    "freshness": 0,
                    "salary_min": 30000.0,
                    "salary_max": 35000.0,
                    "salary_type": "monthly",
                    "is_remote": False,
                },
            ],
        }
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "index.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        self.ranker = SkillWeaveRanker(path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_finds_job_whose_range_covers_target_without_literal_match(self) -> None:
        rows = self.ranker.search("時薪210", top_k=10)["results"]
        job_ids = [row["job_id"] for row in rows]
        self.assertIn("job-covers-no-literal-210", job_ids)
        covering_row = next(
            row for row in rows if row["job_id"] == "job-covers-no-literal-210"
        )
        self.assertGreater(covering_row["features"]["salary"], 0)

    def test_job_below_target_is_penalized_not_boosted(self) -> None:
        rows = self.ranker.search(
            "時薪210",
            top_k=10,
            candidate_ids={"job-covers-no-literal-210", "job-below-target"},
        )["results"]
        below_row = next(
            (row for row in rows if row["job_id"] == "job-below-target"), None
        )
        if below_row is not None:
            self.assertLess(below_row["features"]["salary"], 0)

    def test_mismatched_salary_type_is_neutral(self) -> None:
        intent = self.ranker.parse_intent("時薪210")
        _, features, _, _ = self.ranker._score(
            2, intent, include_graph=True
        )  # job-different-type
        self.assertEqual(features["salary"], 0.0)

    def test_intent_parses_salary_condition(self) -> None:
        intent = self.ranker.parse_intent("時薪210")
        self.assertIsNotNone(intent.salary_intent)
        self.assertEqual(intent.salary_intent["salary_type"], "hourly")
        self.assertEqual(intent.salary_intent["target"], 210.0)

    def test_query_without_salary_condition_has_neutral_feature(self) -> None:
        intent = self.ranker.parse_intent("居家照顧服務員")
        self.assertIsNone(intent.salary_intent)
        _, features, _, _ = self.ranker._score(0, intent, include_graph=True)
        self.assertEqual(features["salary"], 0.0)


class FakeFullCorpusRetriever:
    def __init__(self, *, fail: bool = False, telemetry: dict | None = None) -> None:
        self.fail = fail
        self._telemetry = telemetry

    def last_retrieval_telemetry(self) -> dict:
        return dict(self._telemetry or {})

    def retrieve(
        self,
        query,
        *,
        limit,
        location_names,
        duty_names,
        wants_remote=False,
        salary_intent=None,
        intent=None,
    ):
        if self.fail:
            raise RuntimeError("simulated OpenSearch outage")
        return [
            {
                "id": "job-outside-embedded-demo",
                "title": "Python 資料工程師",
                "description": "使用 Python 建立資料處理服務",
                "salary": "月薪 · 60000 · 90000",
                "city": "台北市",
                "categories": ["軟體工程", "資料工程師"],
                "industry": "資訊軟體",
                "company_id": "company-full",
                "modified_at": "2026-06-01 12:00:00",
                "graph_eligible": True,
                "skills": ["skill.python"],
                "skill_evidence": {"skill.python": "職稱：Python"},
                "skill_confidence": {"skill.python": 0.96},
                "freshness": 1.0,
                "view_count": 0,
                "apply_count": 0,
                "_retrieval_score": 10.0,
            }
        ]


@unittest.skipUnless((ROOT / "artifacts" / "demo-index.json").is_file(), "demo index missing")
class FullCorpusRankerTests(unittest.TestCase):
    def test_external_candidate_can_rank_when_absent_from_embedded_demo(self) -> None:
        ranker = SkillWeaveRanker(
            ROOT / "artifacts" / "demo-index.json",
            candidate_retriever=FakeFullCorpusRetriever(),
        )
        result = ranker.search("Python 資料工程師", top_k=10)
        self.assertEqual(result["candidate_source"], "opensearch_full_corpus")
        self.assertEqual(result["degraded_components"], [])
        self.assertEqual(
            result["results"][0]["job_id"], "job-outside-embedded-demo"
        )

    def test_retrieval_failure_is_disclosed_when_using_embedded_fallback(self) -> None:
        ranker = SkillWeaveRanker(
            ROOT / "artifacts" / "demo-index.json",
            candidate_retriever=FakeFullCorpusRetriever(fail=True),
        )
        result = ranker.search("行政助理", top_k=5)
        self.assertEqual(result["candidate_source"], "embedded_12000_fallback")
        self.assertEqual(result["degraded_components"], ["opensearch"])
        self.assertEqual(result["retrieval_mode"], "embedded_index")
        self.assertEqual(len(result["results"]), 5)

    def test_hybrid_retrieval_mode_is_reported(self) -> None:
        ranker = SkillWeaveRanker(
            ROOT / "artifacts" / "demo-index.json",
            candidate_retriever=FakeFullCorpusRetriever(
                telemetry={"mode": "hybrid_bm25_knn", "knn_degraded": False}
            ),
        )
        result = ranker.search("Python 資料工程師", top_k=10)
        self.assertEqual(result["retrieval_mode"], "hybrid_bm25_knn")
        self.assertEqual(result["degraded_components"], [])

    def test_failed_vector_leg_is_disclosed_as_degraded(self) -> None:
        ranker = SkillWeaveRanker(
            ROOT / "artifacts" / "demo-index.json",
            candidate_retriever=FakeFullCorpusRetriever(
                telemetry={"mode": "bm25_only", "knn_degraded": True}
            ),
        )
        result = ranker.search("Python 資料工程師", top_k=10)
        self.assertEqual(result["retrieval_mode"], "bm25_only")
        self.assertIn("opensearch_knn", result["degraded_components"])
        # Losing the vector leg must not lose the candidate itself.
        self.assertEqual(result["candidate_source"], "opensearch_full_corpus")


@unittest.skipUnless(
    (ROOT / "artifacts" / "demo-index.json").is_file(), "demo index missing"
)
class InferredIntentFeatureTests(unittest.TestCase):
    """A location the user typed outranks the caller's filter code.

    The filter is often a leftover from an earlier search; the query text is a
    deliberate statement. These tests pin the precedence in both directions so
    the override cannot silently regress into "inferred is ignored" (the bug
    this replaced) or "inferred is required" (which would break every caller
    that sends codes without running normalization).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.ranker = SkillWeaveRanker(ROOT / "artifacts" / "demo-index.json")

    @staticmethod
    def _intent(locations, *, confidence: float = 0.9, duties=(), company=None):
        from app.query_normalizer import StructuredQueryIntent

        return StructuredQueryIntent.from_dict(
            {
                "intent_type": "mixed",
                "duty_categories": list(duties),
                "locations": list(locations),
                "employment_types": [],
                "shifts": [],
                "salary_type": None,
                "company": company,
                "keep_terms": [],
                "confidence": confidence,
            }
        )

    @staticmethod
    def _cities(result) -> list[str]:
        return [row.get("city", "") for row in result["results"]]

    def test_inferred_location_overrides_caller_filter_code(self) -> None:
        # 100100 is 台北市; the query text says 台中市 and must win.
        overridden = self.ranker.search(
            "作業員",
            location_code=["100100"],
            top_k=10,
            structured_intent=self._intent(["台中市"]),
        )
        inferred_only = self.ranker.search(
            "作業員", top_k=10, structured_intent=self._intent(["台中市"])
        )
        self.assertEqual(self._cities(overridden), self._cities(inferred_only))
        self.assertIn("台中市", self._cities(overridden))

    def test_absent_inferred_location_leaves_filter_code_behaviour_unchanged(
        self,
    ) -> None:
        baseline = self.ranker.search("作業員", location_code=["100100"], top_k=10)
        with_empty_intent = self.ranker.search(
            "作業員",
            location_code=["100100"],
            top_k=10,
            structured_intent=self._intent([]),
        )
        self.assertEqual(
            self._cities(baseline), self._cities(with_empty_intent)
        )

    def test_intent_features_are_emitted_for_training(self) -> None:
        result = self.ranker.search(
            "作業員",
            top_k=5,
            structured_intent=self._intent(
                ["台中市"], confidence=0.42, duties=["包裝員／作業員"]
            ),
        )
        features = result["results"][0]["features"]
        for name in (
            "intent_duty_match",
            "intent_company_match",
            "intent_location_inferred",
            "intent_confidence",
        ):
            self.assertIn(name, features)
        self.assertEqual(features["intent_location_inferred"], 1.0)
        self.assertAlmostEqual(features["intent_confidence"], 0.42)

    def test_missing_structured_intent_yields_inert_features(self) -> None:
        result = self.ranker.search("作業員", top_k=5)
        features = result["results"][0]["features"]
        self.assertEqual(features["intent_location_inferred"], 0.0)
        self.assertEqual(features["intent_duty_match"], 0.0)
        self.assertEqual(features["intent_company_match"], 0.0)
        self.assertEqual(features["intent_confidence"], 0.0)

    def test_malformed_structured_intent_degrades_instead_of_raising(self) -> None:
        class Broken:
            locations = {"not": "a list"}
            duty_categories = None
            company = 12345
            confidence = "high"

        result = self.ranker.search("作業員", top_k=5, structured_intent=Broken())
        self.assertEqual(len(result["results"]), 5)
        self.assertEqual(
            result["results"][0]["features"]["intent_confidence"], 0.0
        )


if __name__ == "__main__":
    unittest.main()
