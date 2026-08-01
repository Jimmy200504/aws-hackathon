from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUEUE = ROOT / "artifacts" / "district-collocation-queue.json"

_spec = importlib.util.spec_from_file_location(
    "judge_district_collocations", ROOT / "scripts" / "judge_district_collocations.py"
)
judge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(judge)


QUEUE_FIXTURE = {
    "occurrence_review_queue": [
        {
            "surface": "北區",
            "layer": "suffix_dropped",
            "counties": ["台中市", "台南市"],
            "collocations": [
                {
                    "following": "業",
                    "example": "北區業務部專員",
                    "postings": 408,
                    "precision": 0.0466,
                    "label": "not_place",
                    "needs_semantic_judgement": False,
                },
                {
                    "following": "和",
                    "example": "北區和緯路四段332",
                    "postings": 159,
                    "precision": 0.805,
                    "label": None,
                    "needs_semantic_judgement": True,
                },
                {
                    "following": "育",
                    "example": "北區育德路16號)",
                    "postings": 42,
                    "precision": 1.0,
                    "label": "place",
                    "needs_semantic_judgement": False,
                },
            ],
        }
    ]
}


def fixture_queue(directory: str) -> Path:
    path = Path(directory) / "queue.json"
    path.write_text(json.dumps(QUEUE_FIXTURE, ensure_ascii=False), encoding="utf-8")
    return path


class ItemSelectionTests(unittest.TestCase):
    def test_validate_mode_takes_only_labelled_collocations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            items = judge.load_items(fixture_queue(directory), "validate")
        self.assertEqual({item["following"] for item in items}, {"業", "育"})
        for item in items:
            self.assertIn(item["label"], {"place", "not_place"})

    def test_apply_mode_takes_only_the_unlabelled_middle_band(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            items = judge.load_items(fixture_queue(directory), "apply")
        self.assertEqual([item["following"] for item in items], ["和"])
        self.assertIsNone(items[0]["label"])

    def test_items_are_ordered_by_posting_volume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            items = judge.load_items(fixture_queue(directory), "validate")
        self.assertEqual([item["postings"] for item in items], [408, 42])


class BatchRenderingTests(unittest.TestCase):
    def test_rendered_batch_exposes_only_the_judgement_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            items = judge.load_items(fixture_queue(directory), "validate")
        rendered = judge.render_batch(items)
        # The label and the measured precision are the answer key; leaking them
        # into the prompt would make the validation score meaningless.
        self.assertNotIn("not_place", rendered)
        self.assertNotIn("0.0466", rendered)
        self.assertNotIn("408", rendered)
        for line in rendered.splitlines()[1:]:
            self.assertEqual(
                set(json.loads(line)), {"id", "surface", "following", "example"}
            )

    def test_batch_ids_are_positional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            items = judge.load_items(fixture_queue(directory), "validate")
        ids = [json.loads(line)["id"] for line in judge.render_batch(items).splitlines()[1:]]
        self.assertEqual(ids, list(range(len(items))))


class ScoringTests(unittest.TestCase):
    def test_confusion_matrix_counts_each_cell(self) -> None:
        rows = [
            {"label": "place", "verdict": "place", "postings": 100},
            {"label": "place", "verdict": "not_place", "postings": 10},
            {"label": "not_place", "verdict": "not_place", "postings": 50},
            {"label": "not_place", "verdict": "place", "postings": 5},
        ]
        scores = judge.score(rows)
        self.assertEqual(scores["scored"], 4)
        self.assertAlmostEqual(scores["accuracy"], 0.5)
        self.assertEqual(scores["confusion"]["place_not_place"], 1)
        # One true positive against one false positive.
        self.assertAlmostEqual(scores["place_precision"], 0.5)

    def test_posting_weighted_accuracy_reflects_volume_not_row_count(self) -> None:
        # One correct call on a 1,000-posting collocation outweighs three wrong
        # calls on tiny ones, which is the error the corpus actually feels.
        rows = [
            {"label": "place", "verdict": "place", "postings": 1000},
            {"label": "place", "verdict": "not_place", "postings": 10},
            {"label": "place", "verdict": "not_place", "postings": 10},
        ]
        scores = judge.score(rows)
        self.assertAlmostEqual(scores["accuracy"], 1 / 3, places=4)
        self.assertAlmostEqual(scores["postings_weighted_accuracy"], 1000 / 1020, places=4)

    def test_unjudged_rows_are_not_scored(self) -> None:
        rows = [
            {"label": "place", "verdict": None, "postings": 10},
            {"label": None, "verdict": "place", "postings": 10},
        ]
        self.assertEqual(judge.score(rows)["scored"], 0)


class CacheTests(unittest.TestCase):
    def test_validate_and_apply_rows_never_mix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(row, ensure_ascii=False)
                    for row in (
                        {
                            "key": "北區\t業", "mode": "validate", "verdict": "not_place",
                            "prompt_version": judge.PROMPT_VERSION,
                        },
                        {
                            "key": "北區\t和", "mode": "apply", "verdict": "place",
                            "prompt_version": judge.PROMPT_VERSION,
                        },
                    )
                ),
                encoding="utf-8",
            )
            self.assertEqual(list(judge.load_cache(path, "validate")), ["北區\t業"])
            self.assertEqual(list(judge.load_cache(path, "apply")), ["北區\t和"])

    def test_corrupt_lines_are_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            good = json.dumps(
                {
                    "key": "a", "mode": "apply", "verdict": "place",
                    "prompt_version": judge.PROMPT_VERSION,
                }
            )
            path.write_text(f"{good}\nnot json\n\n", encoding="utf-8")
            self.assertEqual(list(judge.load_cache(path, "apply")), ["a"])

    def test_a_reworded_prompt_does_not_inherit_the_previous_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "key": "a", "mode": "apply", "verdict": "place",
                        "prompt_version": "district-collocation-v1",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(judge.load_cache(path, "apply"), {})

    def test_absent_cache_is_empty(self) -> None:
        self.assertEqual(judge.load_cache(Path("no-such-cache.jsonl"), "apply"), {})


class SplitTests(unittest.TestCase):
    def test_split_is_deterministic(self) -> None:
        self.assertEqual(judge.split_of("北區\t和"), judge.split_of("北區\t和"))

    def test_split_covers_both_halves_and_only_those(self) -> None:
        if not QUEUE.is_file():
            self.skipTest("district-collocation-queue.json not present")
        halves = [judge.split_of(item["key"]) for item in judge.load_items(QUEUE, "validate")]
        self.assertEqual(set(halves), {"dev", "holdout"})
        # Roughly balanced, so neither half is a trivial sample.
        self.assertGreater(min(halves.count("dev"), halves.count("holdout")), 200)


class ContractTests(unittest.TestCase):
    def test_output_schema_admits_only_the_two_verdicts(self) -> None:
        verdict = judge.OUTPUT_SCHEMA["properties"]["judgements"]["items"][
            "properties"
        ]["verdict"]
        self.assertEqual(verdict["enum"], ["place", "not_place"])

    def test_rate_limit_stays_under_the_event_ceiling(self) -> None:
        self.assertGreater(judge.MIN_INTERVAL_SECONDS, 1.0)

    def test_allowed_regions_match_the_event_rules(self) -> None:
        self.assertEqual(judge.ALLOWED_REGIONS, {"us-east-1", "us-west-2"})


_ex_spec = importlib.util.spec_from_file_location(
    "extract_job_districts", ROOT / "scripts" / "extract_job_districts.py"
)
extract = importlib.util.module_from_spec(_ex_spec)
_ex_spec.loader.exec_module(extract)


class OccurrenceVerdictTests(unittest.TestCase):
    """The rule that turns per-collocation verdicts into a per-posting decision."""

    JUDGEMENTS = {
        ("中山", "路"): "not_place",
        ("中山", "區"): "place",
        ("北區", "業"): "not_place",
    }

    def verdict(self, surface: str, keys) -> str | None:
        return extract.occurrence_verdict(self.JUDGEMENTS, surface, frozenset(keys))

    def test_one_place_occurrence_carries_the_posting(self) -> None:
        # 中山區...中山路 is in 中山區; the road mention does not undo the district.
        self.assertEqual(self.verdict("中山", ["路", "區"]), "place")

    def test_all_negative_occurrences_reject(self) -> None:
        self.assertEqual(self.verdict("中山", ["路"]), "not_place")
        self.assertEqual(self.verdict("北區", ["業"]), "not_place")

    def test_an_unjudged_occurrence_defers_to_the_surface_gate(self) -> None:
        # Returning not_place here would reject on the strength of one judged
        # occurrence while another was never looked at.
        self.assertIsNone(self.verdict("中山", ["路", "堂"]))

    def test_no_judgement_at_all_defers(self) -> None:
        self.assertIsNone(self.verdict("板橋", ["店"]))
        self.assertIsNone(self.verdict("中山", []))


class JudgementLoaderTests(unittest.TestCase):
    def rows(self, *rows) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "cache.jsonl"
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
            encoding="utf-8",
        )
        return path

    def test_measured_label_outranks_the_model(self) -> None:
        path = self.rows(
            {
                "surface": "北區", "following": "和", "label": "place",
                "verdict": "not_place", "mode": "validate",
                "prompt_version": "district-collocation-v2",
            },
        )
        loaded = extract.load_occurrence_judgements(path, "district-collocation-v2")
        self.assertEqual(loaded[("北區", "和")], "place")

    def test_model_fills_only_the_unlabelled_middle(self) -> None:
        path = self.rows(
            {
                "surface": "南區", "following": "_", "label": None,
                "verdict": "not_place", "mode": "apply",
                "prompt_version": "district-collocation-v2",
            },
        )
        loaded = extract.load_occurrence_judgements(path, "district-collocation-v2")
        self.assertEqual(loaded[("南區", "_")], "not_place")

    def test_other_prompt_versions_are_ignored(self) -> None:
        path = self.rows(
            {
                "surface": "北區", "following": "和", "label": None,
                "verdict": "place", "mode": "apply",
                "prompt_version": "district-collocation-v1",
            },
        )
        self.assertEqual(
            extract.load_occurrence_judgements(path, "district-collocation-v2"), {}
        )

    def test_absent_path_disables_the_feature(self) -> None:
        self.assertEqual(extract.load_occurrence_judgements(None, "v2"), {})
        self.assertEqual(
            extract.load_occurrence_judgements(Path("no-such.jsonl"), "v2"), {}
        )


class QueueArtifactTests(unittest.TestCase):
    """The regenerated queue must hold every collocation, not the report's top 30."""

    def setUp(self) -> None:
        if not QUEUE.is_file():
            self.skipTest("district-collocation-queue.json not present")

    def test_queue_holds_the_full_labelled_and_unlabelled_sets(self) -> None:
        self.assertEqual(len(judge.load_items(QUEUE, "validate")), 511)
        self.assertEqual(len(judge.load_items(QUEUE, "apply")), 2558)

    def test_labelled_set_keeps_both_classes(self) -> None:
        labels = [item["label"] for item in judge.load_items(QUEUE, "validate")]
        self.assertEqual(labels.count("place"), 365)
        self.assertEqual(labels.count("not_place"), 146)

    def test_known_hard_case_is_in_the_judgement_set(self) -> None:
        # 林口長庚 is administratively 桃園市龜山區, not 新北市林口區.
        keys = {item["key"] for item in judge.load_items(QUEUE, "apply")}
        keys |= {item["key"] for item in judge.load_items(QUEUE, "validate")}
        self.assertIn("林口\t長", keys)


if __name__ == "__main__":
    unittest.main()
