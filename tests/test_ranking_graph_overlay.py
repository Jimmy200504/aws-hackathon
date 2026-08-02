from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.ranker import SkillWeaveRanker
from pipeline.ranking_graph_overlay import build_ranking_graph_overlay


class RankingGraphOverlayTests(unittest.TestCase):
    def test_overlay_is_cutoff_bound_and_relations_are_undirected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def write_json(name: str, value: object) -> Path:
                path = root / name
                path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                return path

            def write_jsonl(name: str, rows: list[dict]) -> Path:
                path = root / name
                path.write_text(
                    "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8",
                )
                return path

            base = write_json(
                "base.json",
                {
                    "metadata": {"index_version": "frozen", "stats": {}},
                    "locations": {},
                    "duties": {},
                    "skills": {
                        "skill.old": {"type": "Skill", "label": "old", "aliases": [], "related": {}},
                        "duty.1": {"type": "Occupation", "label": "工程師", "aliases": ["工程師"], "related": {}},
                    },
                    "behavior_graph": {
                        "query_skill": {"python": {"skill.old": [1, 1, 1]}},
                        "snapshots": {"2026-06-01": {"query_skill": {}}},
                    },
                    "jobs": [
                        {"id": "1", "title": "keep", "skills": ["skill.old"], "graph_eligible": True},
                        {"id": "2", "title": "absent", "skills": ["skill.old"], "graph_eligible": True},
                    ],
                },
            )
            qrels = write_json(
                "qrels.json",
                {
                    "splits": {
                        "train": [
                            {
                                "day": "2026-06-01",
                                "query": "python",
                                "candidates": ["1", "2"],
                                "qrels": {"1": 2, "2": 0},
                            }
                        ]
                    }
                },
            )
            manifest = write_json(
                "manifest.json",
                {
                    "scope": "evaluation-cutoff",
                    "cutoff": "2026-06-05 23:59:59.999",
                    "graph_version": "v2-cutoff",
                    "llm_requests": 0,
                    "embedding_requests": 0,
                },
            )
            nodes = write_jsonl(
                "nodes.jsonl",
                [
                    {"id": "skill.python", "type": "Skill", "status": "active", "label": "Python", "aliases": ["python"]},
                    {"id": "skill.sql", "type": "Skill", "status": "active", "label": "SQL", "aliases": ["sql"]},
                    {"id": "skill.candidate", "type": "Skill", "status": "candidate", "label": "candidate", "aliases": []},
                ],
            )
            jobs = write_jsonl(
                "jobs.jsonl",
                [{"job_id": "1", "source_modified_at": "2026-06-01 00:00:00", "skills": ["skill.python"]}],
            )
            job_edges = write_jsonl(
                "job-edges.jsonl",
                [
                    {"source_id": "job:1", "target_id": "skill.python", "type": "REQUIRES", "confidence": 0.95, "evidence": ["Python"], "provenance": {"kind": "exact"}},
                    {"source_id": "job:1", "target_id": "duty.1", "type": "INSTANCE_OF", "confidence": 1.0, "evidence": ["工程師"], "provenance": {"kind": "duty"}},
                ],
            )
            relations = write_jsonl(
                "relations.jsonl",
                [{"id": "edge:1", "source_id": "skill.python", "target_id": "skill.sql", "type": "RELATED_TO", "validated": True, "weight": 0.5, "confidence": 0.5, "support_jobs": 25, "support_companies": 6, "evidence": [{"job_id": "1"}], "rules_version": "stats-v1", "corpus_hash": "abc"}],
            )
            ontology = write_json(
                "ontology.json",
                {"skills": {"skill.python": {"blocked_phrases": ["not python"]}}},
            )
            output = root / "overlay.json"
            metadata = build_ranking_graph_overlay(
                base_index_path=base,
                qrels_path=qrels,
                graph_manifest_path=manifest,
                nodes_path=nodes,
                resolved_jobs_path=jobs,
                job_edges_path=job_edges,
                relation_edges_path=relations,
                reviewed_ontology_path=ontology,
                output_path=output,
            )
            artifact = json.loads(output.read_text(encoding="utf-8"))
            by_id = {job["id"]: job for job in artifact["jobs"]}

            self.assertEqual(by_id["1"]["title"], "keep")
            self.assertEqual(by_id["1"]["skills"], ["duty.1", "skill.python"])
            self.assertFalse(by_id["2"]["graph_eligible"])
            self.assertEqual(by_id["2"]["skills"], [])
            self.assertNotIn("skill.old", artifact["skills"])
            self.assertNotIn("skill.candidate", artifact["skills"])
            self.assertEqual(
                artifact["skills"]["skill.python"]["related"]["skill.sql"]["edge_id"],
                "edge:1",
            )
            self.assertIn("skill.python", artifact["skills"]["skill.sql"]["related"])
            self.assertEqual(
                artifact["skills"]["skill.python"]["blocked_phrases"], ["not python"]
            )
            self.assertEqual(
                artifact["behavior_graph"]["query_skill"]["python"]["skill.python"],
                [1, 1, 2],
            )
            self.assertFalse(metadata["graph_overlay"]["model_retrained"])
            self.assertFalse(metadata["graph_overlay"]["qrels_or_split_changed"])
            self.assertEqual(metadata["stats"]["candidate_nodes_published"], 0)
            ranker = SkillWeaveRanker(output)
            self.assertNotIn("skill.python", ranker.parse_intent("not python").skills)
            self.assertIn("skill.python", ranker.parse_intent("python").skills)

    def test_rejects_future_job_in_cutoff_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base.json"
            base.write_text(json.dumps({"metadata": {"index_version": "x"}, "skills": {}, "jobs": [{"id": "1"}]}))
            qrels = root / "qrels.json"
            qrels.write_text(json.dumps({"splits": {"train": []}}))
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"scope": "evaluation-cutoff", "cutoff": "2026-06-05 23:59:59", "graph_version": "v2", "llm_requests": 0, "embedding_requests": 0}))
            nodes = root / "nodes.jsonl"
            nodes.write_text(json.dumps({"id": "skill.python", "type": "Skill", "status": "active", "label": "Python"}) + "\n")
            jobs = root / "jobs.jsonl"
            jobs.write_text(json.dumps({"job_id": "1", "source_modified_at": "2026-06-06 00:00:00"}) + "\n")
            empty = root / "empty.jsonl"
            empty.write_text("")
            with self.assertRaisesRegex(ValueError, "future job"):
                build_ranking_graph_overlay(
                    base_index_path=base,
                    qrels_path=qrels,
                    graph_manifest_path=manifest,
                    nodes_path=nodes,
                    resolved_jobs_path=jobs,
                    job_edges_path=empty,
                    relation_edges_path=empty,
                    output_path=root / "out.json",
                )


if __name__ == "__main__":
    unittest.main()
