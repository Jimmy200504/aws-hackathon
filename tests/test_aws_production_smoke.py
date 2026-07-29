import unittest

from scripts.run_aws_production_smoke import endpoint, percentile, search_contract


class AwsProductionSmokeTests(unittest.TestCase):
    def test_stage_url_is_preserved_for_relative_endpoints(self) -> None:
        base = "https://api.example.test/prod/"
        self.assertEqual(
            endpoint(base, "api/v1/jobs/search"),
            "https://api.example.test/prod/api/v1/jobs/search",
        )

    def test_percentile_uses_nearest_rank(self) -> None:
        self.assertEqual(percentile([50, 10, 30, 20, 40], 0.95), 50)
        self.assertEqual(percentile([50, 10, 30, 20, 40], 0.50), 30)

    def test_search_contract_requires_top_10_unique_contiguous_rows(self) -> None:
        valid = {
            "result": [
                {"rank": rank, "job_id": f"job-{rank}"}
                for rank in range(1, 11)
            ]
        }
        self.assertTrue(search_contract(valid))
        invalid = {
            "result": [
                {"rank": rank, "job_id": "duplicate"}
                for rank in range(1, 11)
            ]
        }
        self.assertFalse(search_contract(invalid))


if __name__ == "__main__":
    unittest.main()
