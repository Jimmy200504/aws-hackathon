from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_full_graph_build import BuildConfig, build_plan, run_pipeline


class FullGraphBuildTests(unittest.TestCase):
    def fixture(self, root: Path) -> BuildConfig:
        jobs = root / "jobs.csv"
        duty = root / "duty.csv"
        seed = root / "seed.json"
        fields = [
            "職缺編號", "廠商編號", "職缺最後修改時間", "職務名稱", "職務內容",
            "職務大類", "職務中類", "職務小類", "電腦技能資料", "工作技能", "專業證照",
        ]
        with jobs.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow({
                "職缺編號": "j1", "廠商編號": "c1", "職缺最後修改時間": "2026-06-01 00:00:00",
                "職務名稱": "Python 工程師", "職務內容": "需具備 Python", "職務大類": "資訊",
                "職務中類": "軟體", "職務小類": "工程師", "電腦技能資料": "Python",
                "工作技能": "", "專業證照": "",
            })
        duty.write_text(
            "CodeNo,CodeNameA,CodeNameB,CodeNameC\n"
            "100000,資訊,資訊,資訊\n"
            "100100,軟體,軟體,資訊\n"
            "100101,工程師,軟體,資訊\n",
            encoding="utf-8-sig",
        )
        seed.write_text(json.dumps({
            "skills": {"skill.python": {"type": "Skill", "label": "Python", "aliases": []}},
        }), encoding="utf-8")
        return BuildConfig(
            input_path=jobs,
            duty_map=duty,
            seed=seed,
            icap=None,
            work_root=root / "work",
            run_id="test-full",
            graph_version="test-v1",
            part_size=1,
        )

    def test_plan_uses_only_deterministic_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self.fixture(Path(temporary))
            plan = build_plan(config)
            command_text = " ".join(argument for item in plan for argument in item["command"])
            self.assertEqual([item["stage"] for item in plan], ["extract", "resolve", "relations", "export"])
            self.assertNotIn("bedrock", command_text.casefold())
            self.assertNotIn("embedding", command_text.casefold())

    def test_complete_pipeline_resumes_at_stage_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self.fixture(Path(temporary))
            first = run_pipeline(config)
            second = run_pipeline(config)
            self.assertTrue(all(item["status"] == "completed" for item in first))
            self.assertTrue(all(item["status"] == "skipped" for item in second))
            manifest_path = config.release_root / "runs/test-full/evaluation-cutoff/manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["llm_requests"], 0)
            self.assertEqual(manifest["embedding_requests"], 0)
            self.assertTrue((config.work_root / "pipeline-state.json").is_file())


if __name__ == "__main__":
    unittest.main()
