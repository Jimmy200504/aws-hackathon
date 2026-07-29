import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InfrastructureTemplateTests(unittest.TestCase):
    def test_reserved_concurrency_can_be_omitted_for_low_quota_accounts(
        self,
    ) -> None:
        template = (ROOT / "infra/template.yaml").read_text(encoding="utf-8")
        self.assertIn("Default: 0", template)
        self.assertIn("UseReservedConcurrency:", template)
        self.assertIn("ReservedConcurrentExecutions: !If", template)
        self.assertIn("!Ref AWS::NoValue", template)


if __name__ == "__main__":
    unittest.main()
