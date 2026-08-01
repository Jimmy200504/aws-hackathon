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

    def test_search_lambda_can_invoke_bedrock_query_normalizer(self) -> None:
        template = (ROOT / "infra/template.yaml").read_text(encoding="utf-8")
        self.assertIn("Default: us.anthropic.claude-sonnet-4-6", template)
        self.assertIn("BEDROCK_QUERY_MODEL_ID: !Ref BedrockQueryModelId", template)
        self.assertIn("bedrock:InvokeModel", template)


if __name__ == "__main__":
    unittest.main()
