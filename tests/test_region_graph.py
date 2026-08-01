from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.lambda_handler as lambda_handler
from app.lambda_handler import handler
from app.region_graph import RegionGraph

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts" / "region-graph.json"

# Two searched counties, one strong symmetric partner, one commute-only target,
# and one pair far below the publication gate.
FIXTURE = {
    "metadata": {
        "schema": "skillweave-region-graph-v1",
        "dataset_version": "1111-2026-06-01_2026-06-07",
        "graph_cutoff": "2026-06-05 23:59:59.999000",
    },
    "substitutable_with": [
        {
            "a": "甲市",
            "b": "乙縣",
            "co_selected": 1000,
            "jaccard": 0.4,
            "conditional_a_given_b": 0.60,
            "conditional_b_given_a": 0.50,
        },
        {
            "a": "甲市",
            "b": "丁縣",
            "co_selected": 120,
            "jaccard": 0.001,
            "conditional_a_given_b": 0.30,
            "conditional_b_given_a": 0.01,
        },
    ],
    "commutes_to": [
        {
            "source": "甲市",
            "target": "丙市",
            "applications": 163,
            "reverse_applications": 6,
            "asymmetry": 0.929,
        },
        {
            "source": "丙市",
            "target": "甲市",
            "applications": 6,
            "reverse_applications": 163,
            "asymmetry": -0.929,
        },
    ],
}


def event(method: str, path: str, body: dict | None = None) -> dict:
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


def fixture_graph(**kwargs) -> RegionGraph:
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name) / "region-graph.json"
    path.write_text(json.dumps(FIXTURE, ensure_ascii=False), encoding="utf-8")
    graph = RegionGraph(path, **kwargs)
    graph._fixture_directory = directory  # keep the temp dir alive
    return graph


class RegionGraphGateTests(unittest.TestCase):
    def test_conditional_orientation_is_directional(self) -> None:
        graph = fixture_graph()
        # conditional_a_given_b is P(甲市 | 乙縣), so it belongs to source 乙縣.
        from_b = {e.county: e.conditional for e in graph.expand(["乙縣"])}
        from_a = {e.county: e.conditional for e in graph.expand(["甲市"])}
        self.assertAlmostEqual(from_b["甲市"], 0.60)
        self.assertAlmostEqual(from_a["乙縣"], 0.50)

    def test_weak_co_selection_is_not_published(self) -> None:
        counties = {e.county for e in fixture_graph().expand(["甲市"])}
        # P(丁縣 | 甲市) is 0.01, far below the 0.05 gate, and there is no flow.
        self.assertNotIn("丁縣", counties)
        # The reverse direction P(甲市 | 丁縣) is 0.30 and does clear the gate.
        self.assertIn("甲市", {e.county for e in fixture_graph().expand(["丁縣"])})

    def test_commute_only_target_is_published_with_its_gate(self) -> None:
        expansions = {e.county: e for e in fixture_graph().expand(["甲市"])}
        self.assertIn("丙市", expansions)
        self.assertEqual(expansions["丙市"].evidence, ("commute_flow",))
        self.assertIsNone(expansions["丙市"].conditional)

    def test_negative_asymmetry_is_not_published(self) -> None:
        # 丙市 -> 甲市 has asymmetry -0.929, so 甲市 is not a supported target.
        self.assertEqual(fixture_graph().expand(["丙市"]), [])

    def test_searched_counties_are_never_expanded_into(self) -> None:
        counties = {e.county for e in fixture_graph().expand(["甲市", "乙縣"])}
        self.assertNotIn("甲市", counties)
        self.assertNotIn("乙縣", counties)

    def test_limit_is_respected(self) -> None:
        self.assertEqual(len(fixture_graph(limit=1).expand(["甲市"])), 1)
        self.assertEqual(fixture_graph(limit=0).expand(["甲市"]), [])

    def test_output_is_deterministic(self) -> None:
        graph = fixture_graph()
        first = [e.payload() for e in graph.expand(["甲市", "丁縣"])]
        second = [e.payload() for e in graph.expand(["甲市", "丁縣"])]
        self.assertEqual(first, second)

    def test_missing_artifact_disables_without_raising(self) -> None:
        graph = RegionGraph(ROOT / "artifacts" / "definitely-absent.json")
        self.assertFalse(graph.enabled)
        self.assertIsNone(graph.trace(["100100"], {"100100": ["台北市", "台灣"]}))

    def test_district_code_rolls_up_to_its_county(self) -> None:
        graph = fixture_graph()
        locations = {"900101": ["某區", "甲市", "台灣"], "900100": ["甲市", "台灣"]}
        self.assertEqual(graph.resolve(["900101"], locations), ("甲市",))
        self.assertEqual(graph.resolve(["900101", "900100"], locations), ("甲市",))

    def test_unknown_and_overseas_codes_resolve_to_nothing(self) -> None:
        graph = fixture_graph()
        locations = {"110100": ["北京市", "中國"], "100000": ["台灣"]}
        self.assertEqual(graph.resolve(["110100", "100000", "zzz"], locations), ())
        self.assertIsNone(graph.trace(["110100"], locations))

    def test_trace_never_claims_a_ranking_effect(self) -> None:
        trace = fixture_graph().trace(["900100"], {"900100": ["甲市", "台灣"]})
        self.assertIsNotNone(trace)
        self.assertIs(trace["applied_to_ranking"], False)


class RegionGraphArtifactTests(unittest.TestCase):
    """The checked-in artifact must stay consistent with what the docs claim."""

    def setUp(self) -> None:
        if not ARTIFACT.is_file():
            self.skipTest("region-graph.json not present")
        self.graph = RegionGraph(ARTIFACT)

    def test_artifact_records_provenance(self) -> None:
        metadata = self.graph.metadata
        self.assertEqual(metadata["schema"], "skillweave-region-graph-v1")
        for field in ("dataset_version", "graph_cutoff", "random_seed", "leakage_policy"):
            self.assertIn(field, metadata)

    def test_graph_cutoff_precedes_the_evaluation_window(self) -> None:
        # Reading 06-06 or 06-07 behaviour would leak the scored labels.
        self.assertTrue(self.graph.metadata["graph_cutoff"].startswith("2026-06-05"))
        self.assertEqual(
            self.graph.metadata["train_days"],
            ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"],
        )

    def test_twenty_two_domestic_counties_and_no_overseas_node(self) -> None:
        self.assertEqual(len(self.graph.counties), 22)
        for name in ("台灣", "中國", "亞洲", "大洋洲"):
            self.assertNotIn(name, self.graph.counties)

    def test_expansion_excludes_the_searched_county(self) -> None:
        expansions = self.graph.expand(["新竹市"])
        self.assertTrue(expansions)
        self.assertNotIn("新竹市", {e.county for e in expansions})


class RegionTraceContractTests(unittest.TestCase):
    def search(self, body: dict) -> dict:
        result = handler(event("POST", "/api/v1/jobs/search", body), None)
        self.assertEqual(result["statusCode"], 200)
        return json.loads(result["body"])

    def test_official_contract_fields_are_preserved(self) -> None:
        body = self.search({"query": "作業員", "location_code": ["100600"], "top_k": 10})
        self.assertIn("request_id", body)
        self.assertIn("empStr", body)
        self.assertEqual([row["rank"] for row in body["result"]], list(range(1, 11)))

    def test_region_trace_is_omitted_without_a_domestic_county(self) -> None:
        self.assertNotIn("region_trace", self.search({"query": "作業員"})["meta"])
        overseas = self.search({"query": "作業員", "location_code": ["110100"]})
        self.assertNotIn("region_trace", overseas["meta"])
        unknown = self.search({"query": "作業員", "location_code": ["zzz"]})
        self.assertNotIn("region_trace", unknown["meta"])

    def test_region_trace_reports_evidence_for_a_county_search(self) -> None:
        if not lambda_handler.REGION_GRAPH.enabled:
            self.skipTest("region graph artifact not available")
        trace = self.search(
            {"query": "作業員", "location_code": ["100600"]}
        )["meta"]["region_trace"]
        self.assertEqual(trace["searched_counties"], ["新竹市"])
        self.assertIs(trace["applied_to_ranking"], False)
        self.assertTrue(trace["expansions"])
        for expansion in trace["expansions"]:
            self.assertTrue(expansion["explanation"])
            self.assertTrue(
                set(expansion["evidence"]) <= {"co_selection", "commute_flow"}
            )

    def test_ranking_is_unchanged_by_the_region_trace(self) -> None:
        if not lambda_handler.REGION_GRAPH.enabled:
            self.skipTest("region graph artifact not available")
        body = {"query": "作業員", "location_code": ["100600"], "top_k": 10}
        with_graph = self.search(body)
        self.assertIn("region_trace", with_graph["meta"])
        disabled = RegionGraph(ROOT / "artifacts" / "definitely-absent.json")
        with patch.object(lambda_handler, "REGION_GRAPH", disabled):
            without_graph = self.search(body)
        self.assertNotIn("region_trace", without_graph["meta"])
        self.assertEqual(
            [row["job_id"] for row in with_graph["result"]],
            [row["job_id"] for row in without_graph["result"]],
        )


if __name__ == "__main__":
    unittest.main()
