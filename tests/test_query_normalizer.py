from __future__ import annotations

import json
import threading
import time
import unittest

from app.query_normalizer import (
    BedrockQueryNormalizer,
    QueryIntentVocabulary,
    build_expansion,
)


VOCAB = QueryIntentVocabulary(
    {
        "duty_categories": ["行政人員", "門市／店員／專櫃人員", "護理師", "冷凍空調技術人員／安裝員"],
        "duty_aliases": {"行政助理": "行政人員", "店員": "門市／店員／專櫃人員", "護士": "護理師"},
        "cities": ["台北市", "桃園市"],
        "location_aliases": {"台北市": "台北市", "桃園市": "桃園市", "中壢區": "桃園市"},
        "employment_types": ["全職", "兼職", "工讀"],
        "shifts": ["日班", "晚班", "輪班"],
        "salary_types": ["月薪", "時薪", "日薪"],
        "salary_type_map": {"月薪": "monthly", "時薪": "hourly", "日薪": "daily"},
        "prompt_block": "[管理幕僚] 行政人員",
    }
)


def intent_payload(index: int, query: str, **overrides: object) -> dict:
    payload = {
        "id": index,
        "intent_type": "occupation",
        "duty_categories": ["行政人員"],
        "locations": [],
        "employment_types": [],
        "shifts": [],
        "salary_type": None,
        "company": None,
        "keep_terms": [query],
        "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


class FakeBedrock:
    """Records batch sizes so tests can assert on request coalescing."""

    def __init__(self, delay: float = 0.0, **overrides: object) -> None:
        self.delay = delay
        self.overrides = overrides
        self.batch_sizes: list[int] = []
        self.request: dict | None = None
        self._lock = threading.Lock()

    def converse(self, **kwargs):
        body = json.loads(kwargs["messages"][0]["content"][0]["text"])
        with self._lock:
            self.batch_sizes.append(len(body))
            self.request = kwargs
        if self.delay:
            time.sleep(self.delay)
        results = [
            intent_payload(item["id"], item["q"], **self.overrides) for item in body
        ]
        return {
            "output": {
                "message": {
                    "content": [
                        {"text": json.dumps({"results": results}, ensure_ascii=False)}
                    ]
                }
            }
        }


class FailingBedrock:
    def converse(self, **kwargs):
        raise TimeoutError("simulated timeout")


def normalizer(client: object, **kwargs: object) -> BedrockQueryNormalizer:
    options: dict = {
        "client": client,
        "vocabulary": VOCAB,
        "max_batch": 10,
        "max_wait_seconds": 0.15,
        "deadline_seconds": 5.0,
    }
    options.update(kwargs)
    return BedrockQueryNormalizer("model-id", **options)


class BatchCoalescingTests(unittest.TestCase):
    def test_concurrent_queries_become_one_bedrock_request(self) -> None:
        client = FakeBedrock(delay=0.05)
        subject = normalizer(client)

        results = subject.normalize_many([f"query-{index}" for index in range(10)])

        self.assertEqual(client.batch_sizes, [10])
        self.assertEqual({result.source for result in results}, {"amazon_bedrock"})
        self.assertEqual(subject.batch_stats["batches_dispatched"], 1)
        self.assertEqual(subject.batch_stats["queries_dispatched"], 10)

    def test_batch_closes_at_max_batch_before_the_wait_window(self) -> None:
        client = FakeBedrock()
        subject = normalizer(client, max_batch=4, max_wait_seconds=30.0)

        started = time.monotonic()
        subject.normalize_many([f"query-{index}" for index in range(4)])
        elapsed = time.monotonic() - started

        self.assertEqual(client.batch_sizes, [4])
        self.assertLess(elapsed, 5.0, "a full batch must not wait out the window")

    def test_repeated_query_is_served_from_cache_without_a_request(self) -> None:
        client = FakeBedrock()
        subject = normalizer(client)

        subject.normalize("行政助理")
        repeated = subject.normalize("行政助理")

        self.assertEqual(len(client.batch_sizes), 1)
        self.assertEqual(repeated.source, "amazon_bedrock_cached")
        self.assertEqual(subject.batch_stats["cache_hits"], 1)

    def test_deadline_miss_degrades_now_and_warms_the_cache(self) -> None:
        client = FakeBedrock(delay=0.6)
        subject = normalizer(client, max_wait_seconds=0.05, deadline_seconds=0.1)

        degraded = subject.normalize("行政助理")

        self.assertEqual(degraded.source, "deterministic_fallback")
        self.assertTrue(degraded.degraded)
        self.assertEqual(
            degraded.merge_degraded_components(["opensearch", "opensearch"]),
            ["opensearch", "bedrock_query_normalizer"],
        )
        for _ in range(50):
            if subject.batch_stats["batches_dispatched"]:
                break
            time.sleep(0.05)
        warmed = subject.normalize("行政助理")
        self.assertEqual(warmed.source, "amazon_bedrock_cached")


class StructuredOutputTests(unittest.TestCase):
    def test_uses_bedrock_structured_output_and_expands_the_query(self) -> None:
        client = FakeBedrock(
            duty_categories=["行政助理"],
            locations=["桃園市中壢區"],
            employment_types=["兼職"],
            shifts=["晚班"],
            salary_type="時薪",
        )
        subject = normalizer(client)

        result = subject.normalize("行政 pt 時薪")

        self.assertEqual(result.source, "amazon_bedrock")
        self.assertFalse(result.degraded)
        self.assertEqual(result.intent.duty_categories, ("行政人員",))
        self.assertEqual(result.intent.locations, ("桃園市",))
        self.assertEqual(result.intent.employment_types, ("兼職",))
        self.assertEqual(result.intent.shifts, ("晚班",))
        # Aligned with the canonical type app/job_fields.py indexes.
        self.assertEqual(result.intent.salary_type, "hourly")
        self.assertIn("中壢區", result.intent.keep_terms)
        # The scored query stays the user's own words: folding duty labels in
        # would dilute exact_title / title_phrase for queries that already match.
        self.assertTrue(result.query.startswith("行政 pt 時薪"))
        self.assertNotIn("行政人員", result.query)
        # The taxonomy expansion is offered separately, for a rescue pass only.
        self.assertTrue(result.expansion.startswith("行政人員"))
        self.assertEqual(result.expansion, result.intent.recall_expansion)
        output = client.request["outputConfig"]["textFormat"]
        self.assertEqual(output["type"], "json_schema")
        schema = json.loads(output["structure"]["jsonSchema"]["schema"])
        self.assertFalse(schema["additionalProperties"])

    def test_unknown_duty_and_ambiguous_district_are_demoted_to_terms(self) -> None:
        client = FakeBedrock(
            duty_categories=["宇宙飛船駕駛"],
            locations=["火星"],
        )
        subject = normalizer(client)

        result = subject.normalize("宇宙飛船")

        self.assertEqual(result.intent.duty_categories, ())
        self.assertEqual(result.intent.locations, ())
        self.assertIn("宇宙飛船駕駛", result.intent.keep_terms)
        self.assertIn("火星", result.intent.keep_terms)

    def test_salary_type_must_be_grounded_in_the_query_text(self) -> None:
        """現領 makes the model guess `daily`; its applies are monthly full-time."""
        client = FakeBedrock(salary_type="日薪")
        subject = normalizer(client)

        guessed = subject.normalize("現領")
        stated = subject.normalize("日領 現領")

        self.assertIsNone(guessed.intent.salary_type)
        self.assertEqual(stated.intent.salary_type, "daily")

    def test_district_in_keep_terms_corrects_a_wrong_city(self) -> None:
        """竹北市 is 新竹縣; the code table outranks the model's city guess."""
        client = FakeBedrock(locations=["新竹市"], keep_terms=["竹北市"])
        subject = BedrockQueryNormalizer(
            "model-id",
            client=client,
            vocabulary=QueryIntentVocabulary(
                {
                    "duty_categories": ["行政人員"],
                    "duty_aliases": {},
                    "cities": ["新竹市", "新竹縣"],
                    "location_aliases": {
                        "新竹市": "新竹市",
                        "新竹縣": "新竹縣",
                        "竹北市": "新竹縣",
                    },
                    "employment_types": [],
                    "shifts": [],
                    "salary_types": [],
                    "salary_type_map": {},
                    "prompt_block": "",
                }
            ),
            max_wait_seconds=0.05,
            deadline_seconds=5.0,
        )

        result = subject.normalize("竹北 門市")

        self.assertIn("新竹縣", result.intent.locations)

    def test_noise_verdict_still_preserves_the_user_terms(self) -> None:
        client = FakeBedrock(
            intent_type="noise", duty_categories=[], keep_terms=[], confidence=0.02
        )
        subject = normalizer(client)

        result = subject.normalize("104人力銀行////////")

        self.assertEqual(result.intent.intent_type, "noise")
        self.assertIn("104人力銀行////////", result.query)


class FallbackTests(unittest.TestCase):
    def test_failure_falls_back_without_logging_query_content(self) -> None:
        subject = normalizer(FailingBedrock())

        with self.assertLogs("app.query_normalizer", level="WARNING") as logs:
            result = subject.normalize("  Ｎｏｄｅ   js secret@example.com  ")

        self.assertEqual(result.source, "deterministic_fallback")
        self.assertTrue(result.degraded)
        self.assertIn("Node js secret@example.com", result.query)
        self.assertNotIn("secret@example.com", " ".join(logs.output))

    def test_unconfigured_local_mode_parses_against_the_vocabulary(self) -> None:
        subject = BedrockQueryNormalizer(None, vocabulary=VOCAB)

        result = subject.normalize("  護士   台北市  ")

        self.assertEqual(result.source, "deterministic")
        self.assertFalse(result.degraded)
        self.assertEqual(result.intent.duty_categories, ("護理師",))
        self.assertEqual(result.intent.locations, ("台北市",))
        self.assertEqual(result.metadata()["normalized_query"], result.query)

    def test_missing_vocabulary_keeps_the_legacy_string_contract(self) -> None:
        subject = BedrockQueryNormalizer(None, vocabulary=None)

        result = subject.normalize("  React   前端  ")

        self.assertEqual(
            result.metadata(),
            {
                "source": "deterministic",
                "model_id": None,
                "normalized_query": "React 前端",
            },
        )

    def test_rejects_oversized_query_before_calling_bedrock(self) -> None:
        client = FakeBedrock()
        subject = normalizer(client)
        with self.assertRaisesRegex(ValueError, "at most 500"):
            subject.normalize("x" * 501)
        self.assertEqual(client.batch_sizes, [])


class ExpansionTests(unittest.TestCase):
    def test_expansion_is_truncated_on_a_term_boundary(self) -> None:
        intent = VOCAB.validate(
            intent_payload(0, "x", duty_categories=["行政人員"], keep_terms=["術" * 40]),
            "原始查詢",
        )
        expansion = build_expansion("原始查詢", intent)
        self.assertLessEqual(len(expansion), 480)
        self.assertNotIn("  ", expansion)


if __name__ == "__main__":
    unittest.main()
