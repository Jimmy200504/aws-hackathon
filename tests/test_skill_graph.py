from __future__ import annotations

import tempfile
import unittest

from pipeline.graph_artifacts import ArtifactWriter, build_neptune_csv, quality_report
from pipeline.skill_graph import (
    CanonicalNode,
    ExactEntityResolver,
    RelationCandidate,
    publish_relations,
    is_forbidden_merge,
    relation_candidates,
    stable_node_id,
)


class SkillGraphTests(unittest.TestCase):
    def test_stable_id_and_exact_only_resolution(self) -> None:
        self.assertEqual(stable_node_id("Skill", "Excel"), stable_node_id("Skill", "Ｅｘｃｅｌ"))
        resolver = ExactEntityResolver([
            CanonicalNode("skill.excel", "Skill", "Excel", ("Microsoft Excel",)),
            CanonicalNode("skill.java", "Skill", "Java", ()),
        ])
        self.assertEqual(resolver.resolve("microsoft excel").node_id, "skill.excel")
        unknown = resolver.resolve("JavaScript")
        self.assertIsNone(unknown.node_id)
        self.assertEqual(unknown.decision, "UNKNOWN_SURFACE")
        self.assertTrue(is_forbidden_merge("Java", "JavaScript"))
        self.assertTrue(is_forbidden_merge("React", "Next.js"))
        self.assertFalse(is_forbidden_merge("Excel", "Microsoft Excel"))

    def test_relation_is_pure_statistical_related_to(self) -> None:
        jobs = [{
            "job_id": str(index), "skills": ["a", "b"], "company_id": str(index % 5),
            "skill_evidence": {"a": f"A-{index}", "b": f"B-{index}"},
        } for index in range(20)] + [{
            "job_id": f"noise-{index}", "skills": [f"noise-{index}"], "company_id": "z",
            "skill_evidence": {},
        } for index in range(80)]
        candidates = relation_candidates(jobs)
        self.assertEqual([(row.source_id, row.target_id) for row in candidates], [("a", "b")])
        accepted, rejected = publish_relations(candidates)
        self.assertEqual(len(accepted), 1)
        self.assertFalse(rejected)
        edge = accepted[0]
        self.assertEqual(edge.relation_type, "RELATED_TO")
        self.assertEqual(edge.weight, edge.confidence)
        self.assertEqual(len(edge.evidence), 3)
        self.assertEqual(edge.rules_version, "statistical-related-to-v1")
        self.assertTrue(edge.corpus_hash)

    def test_published_degree_cap_is_deterministic(self) -> None:
        candidates = [RelationCandidate(
            "hub", f"n{index:02d}", 20 + index, 5, 2.5, 0.2,
            0.5 + index / 1000, (), "statistical-related-to-v1", "hash",
        ) for index in range(25)]
        accepted, rejected = publish_relations(candidates, degree_cap=20)
        self.assertEqual(len(accepted), 20)
        self.assertEqual(len(rejected), 5)

    def test_candidates_cannot_enter_neptune(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            writer = ArtifactWriter(temporary, "run-1")
            with self.assertRaises(ValueError):
                build_neptune_csv(writer, [{"id": "candidate:x", "type": "Skill", "status": "candidate"}], [])

    def test_artifacts_validate_references_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            writer = ArtifactWriter(temporary, "run-1")
            nodes = [{"id": "job:1", "type": "Job"}, {"id": "skill:x", "type": "Skill"}]
            edges = [{
                "id": "edge:1", "source_id": "job:1", "target_id": "skill:x", "type": "REQUIRES",
                "source_modified_at": "2026-06-01 00:00:00", "provenance": {"kind": "exact"},
            }]
            build_neptune_csv(writer, nodes, edges)
            report = quality_report(1, [{"job_id": "1"}], [], nodes, edges, "2026-06-05 23:59:59")
            self.assertTrue(report["referential_integrity"])


if __name__ == "__main__":
    unittest.main()
