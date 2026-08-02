from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.build_graph import export_stage, read_jsonl, resolve_stage


class GraphPipelineIntegrationTests(unittest.TestCase):
    def test_fake_extraction_to_neptune_artifacts_has_no_silent_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted.jsonl"
            rows = []
            for index in range(3):
                rows.append({
                    "job_id": str(index), "company_id": f"company-{index % 2}",
                    "source_modified_at": "2026-06-01 00:00:00",
                    "mentions": [{
                        "surface": "Microsoft Excel", "node_id": "skill.excel", "type": "skill", "confidence": 0.99,
                        "evidence": "Microsoft Excel",
                    }],
                    "occupations": [],
                })
            accepted.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
            resolved = root / "resolved"
            resolve_stage(accepted, Path("config/skill_ontology.seed.json"), resolved)
            nodes = read_jsonl(resolved / "nodes.jsonl")
            edges = read_jsonl(resolved / "job-skill-edges.jsonl")
            self.assertEqual(len([row for row in nodes if row["type"] == "Job"]), 3)
            self.assertEqual(len(edges), 3)

            quarantine = root / "quarantine.jsonl"
            quarantine.write_text("", encoding="utf-8")
            relation_edges = root / "relation-edges.jsonl"
            relation_edges.write_text("", encoding="utf-8")
            args = type("Args", (), {
                "nodes": resolved / "nodes.jsonl", "job_edges": resolved / "job-skill-edges.jsonl",
                "relation_edges": relation_edges, "accepted": accepted, "quarantine": quarantine,
                "output": root / "artifacts", "run_id": "run-test", "graph_version": "cutoff-v1",
                "cutoff": "2026-06-05 23:59:59", "input_count": 3,
            })()
            with patch("pipeline.build_graph.read_jsonl", side_effect=AssertionError("bulk read is forbidden")):
                export_stage(args)
            scope_root = root / "artifacts/runs/run-test/evaluation-cutoff"
            manifest = json.loads((scope_root / "manifest.json").read_text(encoding="utf-8"))
            quality = json.loads((scope_root / "quality-report.json").read_text(encoding="utf-8"))
            self.assertEqual(quality["silent_loss"], 0)
            self.assertTrue(quality["referential_integrity"])
            self.assertEqual(manifest["graph_version"], "cutoff-v1")
            self.assertEqual(manifest["extractor"], "deterministic-v1")
            self.assertIsNone(manifest["model_id"])
            self.assertEqual(manifest["llm_requests"], 0)
            self.assertTrue((scope_root / "neptune/nodes.csv").is_file())

    def test_resolver_checkpoint_resume_matches_clean_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted.jsonl"
            rows = [{
                "job_id": str(index),
                "company_id": f"company-{index % 2}",
                "source_modified_at": "2026-06-01 00:00:00",
                "mentions": [{
                    "surface": "Microsoft Excel",
                    "node_id": "skill.excel",
                    "confidence": 1.0,
                    "evidence": "Microsoft Excel",
                    "evidence_field": "電腦技能資料",
                }],
                "occupations": [],
            } for index in range(5)]
            accepted.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            resumed, clean = root / "resumed", root / "clean"
            seed = Path("config/skill_ontology.seed.json")
            resolve_stage(accepted, seed, resumed, part_size=2, max_records=2)
            partial_checkpoint = json.loads((resumed / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertFalse(partial_checkpoint["complete"])
            self.assertEqual(partial_checkpoint["processed"], 2)
            self.assertFalse((resumed / "nodes.jsonl").exists())

            resolve_stage(accepted, seed, resumed, part_size=2)
            resolve_stage(accepted, seed, clean, part_size=2)
            resumed_files = {
                path.relative_to(resumed): path.read_bytes()
                for path in resumed.rglob("*") if path.is_file()
            }
            clean_files = {
                path.relative_to(clean): path.read_bytes()
                for path in clean.rglob("*") if path.is_file()
            }
            self.assertEqual(resumed_files, clean_files)
            self.assertEqual(json.loads((resumed / "checkpoint.json").read_text())["processed"], 5)


if __name__ == "__main__":
    unittest.main()
