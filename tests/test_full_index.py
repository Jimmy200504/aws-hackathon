from __future__ import annotations

import json
import unittest

from scripts.index_full_opensearch import (
    bulk_payload,
    compile_alias_matcher,
    job_document,
    mapping,
)


def row(*, modified_at: str, title: str = "一般行政人員") -> dict[str, str]:
    return {
        "職缺編號": "job-1",
        "職務名稱": title,
        "職務內容": "使用 Python 整理資料",
        "薪資": "月薪‧40000‧50000",
        "職務大類": "行政",
        "職務中類": "行政人員",
        "職務小類": "行政助理",
        "工作城市": "台北市",
        "電腦技能資料": "Python",
        "工作技能": "",
        "專業證照": "",
        "廠商編號": "company-1",
        "產業大類": "資訊",
        "產業中類": "軟體",
        "職缺最後修改時間": modified_at,
    }


class FullCorpusIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skills = {
            "skill.python": {
                "label": "Python",
                "aliases": ["python"],
            }
        }
        self.pattern, self.aliases = compile_alias_matcher(self.skills)

    def test_post_cutoff_job_is_still_annotated_but_flagged(self) -> None:
        """The cutoff is offline-ablation provenance, not a live-index gate.

        Withholding skills here would leave a quarter of the corpus with no
        graph signal for a leakage risk the live path does not have.
        """
        document = job_document(
            row(modified_at="2026-06-20 00:00:00", title="Python 工程師"),
            self.skills,
            self.pattern,
            self.aliases,
        )
        self.assertEqual(document["id"], "job-1")
        self.assertFalse(document["graph_eligible"])
        self.assertTrue(document["post_cutoff_jd"])
        self.assertEqual(document["skills"], ["skill.python"])
        self.assertIn("Python", document["description_search"])

    def test_structured_attribute_columns_are_indexed(self) -> None:
        source = row(modified_at="2026-06-05 12:00:00")
        source.update(
            {
                "職缺屬性": "兼職",
                "工時": "晚班,假日班",
                "學歷需求": "高中職",
                "工作經驗需求": "不拘",
            }
        )
        document = job_document(source, self.skills, self.pattern, self.aliases)
        self.assertEqual(document["employment_type"], "兼職")
        self.assertEqual(document["shifts"], ["晚班", "假日班"])
        self.assertEqual(document["education"], "高中職")
        self.assertEqual(document["experience"], "不拘")

    def test_cutoff_eligible_job_gets_grounded_skill(self) -> None:
        document = job_document(
            row(modified_at="2026-06-05 12:00:00", title="Python 工程師"),
            self.skills,
            self.pattern,
            self.aliases,
        )
        self.assertTrue(document["graph_eligible"])
        self.assertEqual(document["skills"], ["skill.python"])
        self.assertIn("Python", document["skill_labels"])

    def test_bulk_payload_preserves_job_id(self) -> None:
        document = job_document(
            row(modified_at="2026-06-05 12:00:00"),
            self.skills,
            self.pattern,
            self.aliases,
        )
        lines = bulk_payload("jobs-v1", [document]).decode().splitlines()
        action = json.loads(lines[0])
        source = json.loads(lines[1])
        self.assertEqual(action["index"]["_id"], "job-1")
        self.assertEqual(source["id"], "job-1")

    def test_serverless_mapping_does_not_override_managed_shards(self) -> None:
        self.assertNotIn("settings", mapping(serverless=True))
        self.assertIn("settings", mapping(serverless=False))
        local = mapping(local_single_node=True)
        self.assertEqual(local["settings"]["index"]["number_of_shards"], 1)
        self.assertEqual(local["settings"]["index"]["number_of_replicas"], 0)


if __name__ == "__main__":
    unittest.main()
