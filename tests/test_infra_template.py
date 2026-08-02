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
        self.assertIn(
            "Default: global.anthropic.claude-haiku-4-5-20251001-v1:0",
            template,
        )
        self.assertIn("BEDROCK_QUERY_MODEL_ID: !Ref BedrockQueryModelId", template)
        self.assertIn("bedrock:InvokeModel", template)

    def test_hybrid_vector_retrieval_is_opt_in(self) -> None:
        """Shipping a non-empty default would embed queries against an index
        that may hold no vectors, paying Bedrock latency for zero recall."""
        template = (ROOT / "infra/template.yaml").read_text(encoding="utf-8")
        self.assertIn("BedrockEmbeddingModelId:", template)
        self.assertIn(
            "BEDROCK_EMBEDDING_MODEL_ID: !Ref BedrockEmbeddingModelId", template
        )
        parameter = template.split("BedrockEmbeddingModelId:", 1)[1]
        self.assertIn('Default: ""', parameter.split("Description:", 1)[0])

    def test_optional_neptune_runtime_is_read_only_and_bounded(self) -> None:
        template = (ROOT / "infra/template.yaml").read_text(encoding="utf-8")
        self.assertIn("NEPTUNE_GRAPH_ID: !Ref NeptuneGraphId", template)
        self.assertIn('GRAPH_QUERY_TIMEOUT_MS: "500"', template)
        self.assertIn("neptune-graph:ReadDataViaQuery", template)
        self.assertNotIn("neptune-graph:WriteDataViaQuery", template)

    def test_graph_pipeline_uses_fargate_and_sequential_stages(self) -> None:
        template = (ROOT / "infra/graph-pipeline.yaml").read_text(encoding="utf-8")
        self.assertIn("RequiresCompatibilities: [FARGATE]", template)
        self.assertIn("StartAt: DeterministicExtract", template)
        self.assertIn("ResolveExactAliases:", template)
        self.assertIn("BuildStatisticalRelations:", template)
        self.assertIn("ExportAndValidate:", template)
        self.assertNotIn("bedrock:InvokeModel", template)
        self.assertNotIn("ClassifyRelations:", template)


if __name__ == "__main__":
    unittest.main()
