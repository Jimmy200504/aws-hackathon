from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.ranker import SkillWeaveRanker


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


class GraphIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        skills = {
            "skill.python": {
                "label": "Python",
                "aliases": ["python"],
                "related": {},
            }
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
                    "skills": ["skill.python"],
                    "skill_confidence": {"skill.python": 0.9},
                    "skill_evidence": {"skill.python": "Python engineer"},
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


class FakeFullCorpusRetriever:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def retrieve(self, query, *, limit, location_names, duty_names):
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
        self.assertEqual(len(result["results"]), 5)


if __name__ == "__main__":
    unittest.main()
