from __future__ import annotations

import unittest

from app.job_fields import derive_job_fields, is_remote_job, parse_salary


class RemoteDetectionTests(unittest.TestCase):
    def test_matches_common_remote_terms(self) -> None:
        self.assertTrue(is_remote_job("後端工程師（遠端工作）", ""))
        self.assertTrue(is_remote_job("客服專員", "本職缺可在家工作，彈性上班時間"))
        self.assertTrue(is_remote_job("Remote Software Engineer", ""))
        self.assertTrue(is_remote_job("行政助理", "居家辦公，需自備電腦"))
        self.assertTrue(is_remote_job("", "WFH 全遠端職缺"))

    def test_does_not_match_in_person_jobs(self) -> None:
        self.assertFalse(is_remote_job("門市人員", "需於實體門市服務客戶"))
        self.assertFalse(is_remote_job("作業員", ""))

    def test_home_care_jobs_are_not_misclassified_as_remote(self) -> None:
        # 居家照顧/居家服務 are in-person home-visit care roles, not
        # work-from-home jobs, even though they contain "居家".
        self.assertFalse(is_remote_job("居家照顧服務員", "至案家提供照顧服務"))
        self.assertFalse(is_remote_job("居家服務督導員", ""))
        self.assertFalse(is_remote_job("居家護理師", "居家訪視個案"))

    def test_empty_text_is_not_remote(self) -> None:
        self.assertFalse(is_remote_job("", ""))
        self.assertFalse(is_remote_job(None, None))


class SalaryParsingTests(unittest.TestCase):
    def test_parses_monthly_salary_range(self) -> None:
        result = parse_salary("月薪‧30000‧35000", "30000", "35000")
        self.assertEqual(result["salary_min"], 30000.0)
        self.assertEqual(result["salary_max"], 35000.0)
        self.assertEqual(result["salary_type"], "monthly")

    def test_parses_hourly_salary(self) -> None:
        result = parse_salary("時薪‧196‧196", "196", "196")
        self.assertEqual(result["salary_min"], 196.0)
        self.assertEqual(result["salary_type"], "hourly")

    def test_negotiable_salary_with_floor_only(self) -> None:
        result = parse_salary(
            "面議（經常性薪資達4萬元或以上）‧40000‧", "40000", ""
        )
        self.assertEqual(result["salary_min"], 40000.0)
        self.assertEqual(result["salary_max"], 0.0)
        self.assertEqual(result["salary_type"], "negotiable")

    def test_missing_and_null_values_default_to_zero(self) -> None:
        result = parse_salary("", None, "NULL")
        self.assertEqual(result["salary_min"], 0.0)
        self.assertEqual(result["salary_max"], 0.0)
        self.assertEqual(result["salary_type"], "unknown")

    def test_swapped_bounds_are_normalized(self) -> None:
        result = parse_salary("月薪‧40000‧30000", "40000", "30000")
        self.assertEqual(result["salary_min"], 30000.0)
        self.assertEqual(result["salary_max"], 40000.0)


class DeriveJobFieldsTests(unittest.TestCase):
    def test_combines_salary_and_remote_signals(self) -> None:
        row = {
            "職務名稱": "後端工程師（遠端）",
            "職務內容": "使用 Python 開發服務",
            "薪資": "月薪‧45000‧60000",
            "薪資下限": "45000",
            "薪資上限": "60000",
        }
        fields = derive_job_fields(row)
        self.assertEqual(fields["salary_min"], 45000.0)
        self.assertEqual(fields["salary_max"], 60000.0)
        self.assertEqual(fields["salary_type"], "monthly")
        self.assertTrue(fields["is_remote"])


if __name__ == "__main__":
    unittest.main()
