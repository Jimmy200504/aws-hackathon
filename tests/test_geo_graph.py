from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.lambda_handler as lambda_handler
from app.geo_graph import GeoGraph, build_geo_graph, get_expanded_locations
from app.lambda_handler import handler

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts" / "district-graph.json"
AUTHORED = ROOT / "config" / "geo-authored.json"
L5_SOURCE = ROOT / "config" / "geo-l5-table.json"
L5_PUBLISHED = ROOT / "config" / "geo-l5-published.json"
L4_TABLE = ROOT / "config" / "geo-l4-districts.json"

# 甲市 has a strong neighbour (乙區), a weak one (丙區), and one reachable only
# by going through 乙區. 丁區 sits in another county so cross-county traversal
# is covered too.
GRAPH_FIXTURE = {
    "metadata": {
        "schema": "skillweave-district-graph-v1",
        "dataset_version": "1111-2026-06-01_2026-06-07",
        "graph_cutoff": "2026-06-05 23:59:59.999000",
    },
    "nodes": [
        {"id": "甲市/一區", "county": "甲市", "district": "一區", "codes": ["900101"]},
        {"id": "甲市/乙區", "county": "甲市", "district": "乙區", "codes": ["900102"]},
        {"id": "甲市/丙區", "county": "甲市", "district": "丙區", "codes": ["900103"]},
        {"id": "甲市/戊區", "county": "甲市", "district": "戊區", "codes": ["900104"]},
        {"id": "乙縣/丁區", "county": "乙縣", "district": "丁區", "codes": ["900201"]},
        {"id": "乙縣/己區", "county": "乙縣", "district": "己區", "codes": ["900202"]},
    ],
    "substitutable_with": [
        {"a": "甲市/一區", "b": "甲市/乙區", "jaccard": 0.5, "co_selected": 5000},
        {"a": "甲市/一區", "b": "甲市/丙區", "jaccard": 0.1, "co_selected": 900},
        {"a": "甲市/乙區", "b": "甲市/戊區", "jaccard": 0.5, "co_selected": 4000},
        {"a": "甲市/一區", "b": "乙縣/丁區", "jaccard": 0.2, "co_selected": 1500},
    ],
}

AUTHORED_FIXTURE = {
    "schema": "skillweave-geo-authored-v1",
    "regions": [{"id": "L1/測試大區", "counties": ["甲市", "乙縣"]}],
    "living_areas": [
        {
            "id": "L3/測試生活圈",
            "aliases": ["測試生活圈"],
            "districts": ["甲市/一區", "甲市/乙區"],
        }
    ],
    "sites": [
        {
            "id": "L5/通過園區",
            "aliases": ["通過園區"],
            "districts": ["甲市/乙區", "乙縣/丁區"],
            "published": True,
        },
        {
            "id": "L5/退回園區",
            "aliases": ["退回園區"],
            "districts": ["甲市/丙區"],
            "published": False,
            "rejected_reason": "below the alias gate",
        },
    ],
    "shortcuts": [
        {
            "a": "甲市/一區",
            "b": "乙縣/己區",
            "label": "早通車路線",
            "implied_substitutability": 0.3,
            "effective_date": "2026-05-12",
            "provenance": "external",
        },
        {
            "a": "甲市/丙區",
            "b": "甲市/戊區",
            "label": "晚通車路線",
            "implied_substitutability": 0.9,
            "effective_date": "2026-06-30",
            "provenance": "external",
        },
        {
            "a": "甲市/一區",
            "b": "甲市/乙區",
            "label": "已有行為證據的路線",
            "implied_substitutability": 0.99,
            "effective_date": "2026-01-01",
            "provenance": "external",
        },
    ],
}


def event(method: str, path: str, body: dict | None = None) -> dict:
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


def fixture_graph(**kwargs) -> GeoGraph:
    directory = tempfile.TemporaryDirectory()
    base = Path(directory.name) / "district-graph.json"
    special = Path(directory.name) / "geo-authored.json"
    base.write_text(json.dumps(GRAPH_FIXTURE, ensure_ascii=False), encoding="utf-8")
    special.write_text(json.dumps(AUTHORED_FIXTURE, ensure_ascii=False), encoding="utf-8")
    graph = build_geo_graph(base, special, **kwargs)
    graph._fixture_directory = directory  # keep the temp dir alive
    return graph


class GeoCostModelTests(unittest.TestCase):
    def test_cost_is_negative_log_of_substitutability(self) -> None:
        reach = {r.district: r for r in fixture_graph().expand(["甲市/一區"])}
        self.assertAlmostEqual(reach["甲市/乙區"].cost, -math.log(0.5))
        self.assertAlmostEqual(reach["甲市/乙區"].substitutability, 0.5)

    def test_two_hop_cost_multiplies_the_hop_weights(self) -> None:
        # 一區 -> 乙區 -> 戊區 at 0.5 each must price as the joint 0.25.
        reach = {r.district: r for r in fixture_graph().expand(["甲市/一區"])}
        self.assertAlmostEqual(reach["甲市/戊區"].substitutability, 0.25)
        self.assertEqual(len(reach["甲市/戊區"].hops), 2)
        self.assertEqual(
            reach["甲市/戊區"].path, ("甲市/一區", "甲市/乙區", "甲市/戊區")
        )

    def test_budget_excludes_routes_beyond_it(self) -> None:
        graph = fixture_graph()
        near = {r.district for r in graph.expand(["甲市/一區"], max_cost=0.8)}
        self.assertIn("甲市/乙區", near)  # cost 0.693
        self.assertNotIn("甲市/戊區", near)  # cost 1.386

    def test_nearer_district_outranks_the_further_one(self) -> None:
        ordered = [r.district for r in fixture_graph().expand(["甲市/一區"])]
        self.assertLess(ordered.index("甲市/乙區"), ordered.index("甲市/丙區"))

    def test_cross_county_traversal_is_not_blocked(self) -> None:
        reach = {r.district for r in fixture_graph().expand(["甲市/一區"])}
        self.assertIn("乙縣/丁區", reach)

    def test_sources_are_never_returned_as_expansions(self) -> None:
        reach = {r.district for r in fixture_graph().expand(["甲市/一區", "甲市/乙區"])}
        self.assertNotIn("甲市/一區", reach)
        self.assertNotIn("甲市/乙區", reach)

    def test_output_is_deterministic(self) -> None:
        graph = fixture_graph()
        first = [r.payload() for r in graph.expand(["甲市/一區"])]
        second = [r.payload() for r in graph.expand(["甲市/一區"])]
        self.assertEqual(first, second)

    def test_limit_is_respected(self) -> None:
        self.assertEqual(len(fixture_graph(limit=1).expand(["甲市/一區"])), 1)
        self.assertEqual(fixture_graph(limit=0).expand(["甲市/一區"]), [])


class TemporalFilterTests(unittest.TestCase):
    def test_edge_effective_after_the_cutoff_is_excluded(self) -> None:
        graph = fixture_graph(cutoff_date="2026-06-01")
        excluded = {item["id"] for item in graph.excluded_edges if item["kind"] == "shortcut"}
        self.assertIn("甲市/丙區--甲市/戊區", excluded)
        # Without the late shortcut, 戊區 is only reachable the long way round:
        # 丙區 -> 一區 -> 乙區 -> 戊區, which needs a budget past 3.0.
        reach = {r.district: r for r in graph.expand(["甲市/丙區"], max_cost=9.0)}
        self.assertGreater(len(reach["甲市/戊區"].hops), 1)

    def test_same_edge_is_admitted_once_the_cutoff_moves_past_it(self) -> None:
        graph = fixture_graph(cutoff_date="2026-07-01")
        excluded = {item["id"] for item in graph.excluded_edges if item["kind"] == "shortcut"}
        self.assertNotIn("甲市/丙區--甲市/戊區", excluded)
        reach = {r.district: r for r in graph.expand(["甲市/丙區"])}
        self.assertEqual(len(reach["甲市/戊區"].hops), 1)

    def test_edge_effective_before_the_cutoff_is_present(self) -> None:
        reach = {r.district: r for r in fixture_graph().expand(["甲市/一區"])}
        self.assertIn("乙縣/己區", reach)
        self.assertEqual(reach["乙縣/己區"].provenance, ("external",))

    def test_unparseable_effective_date_drops_the_edge(self) -> None:
        payload = json.loads(json.dumps(AUTHORED_FIXTURE))
        payload["shortcuts"][0]["effective_date"] = "not-a-date"
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "district-graph.json"
            special = Path(directory) / "geo-authored.json"
            base.write_text(json.dumps(GRAPH_FIXTURE, ensure_ascii=False), encoding="utf-8")
            special.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            graph = build_geo_graph(base, special)
        self.assertNotIn("乙縣/己區", {r.district for r in graph.expand(["甲市/一區"])})


class ProvenanceTests(unittest.TestCase):
    def test_authored_edge_never_overrides_a_behaviour_weight(self) -> None:
        # The fixture asserts 0.99 for a pair the logs already price at 0.5.
        graph = fixture_graph()
        reach = {r.district: r for r in graph.expand(["甲市/一區"])}
        self.assertAlmostEqual(reach["甲市/乙區"].substitutability, 0.5)
        self.assertEqual(reach["甲市/乙區"].provenance, ("behaviour",))
        corroborated = {(row["a"], row["b"]) for row in graph.corroborated}
        self.assertIn(("甲市/一區", "甲市/乙區"), corroborated)

    def test_behaviour_only_graph_drops_every_authored_layer(self) -> None:
        graph = fixture_graph(include_authored=False)
        self.assertEqual(graph.groups, {})
        self.assertNotIn("乙縣/己區", {r.district for r in graph.expand(["甲市/一區"])})
        for reach in graph.expand(["甲市/一區"]):
            self.assertEqual(reach.provenance, ("behaviour",))

    def test_every_hop_carries_a_known_provenance(self) -> None:
        graph = fixture_graph()
        for reach in graph.expand(["甲市/一區"], max_cost=9.0, limit=len(graph.districts)):
            for hop in reach.hops:
                self.assertIn(hop.edge.provenance, {"behaviour", "authored", "external"})

    def test_behaviour_explanation_cites_its_co_selection_count(self) -> None:
        reach = {r.district: r for r in fixture_graph().expand(["甲市/一區"])}
        self.assertIn("5,000", reach["甲市/乙區"].explanation)


class MembershipTests(unittest.TestCase):
    """Levels are containers, not weighted edges (docs/geo-graph-handoff.md 3.4)."""

    def test_group_membership_is_a_set_query(self) -> None:
        graph = fixture_graph()
        self.assertEqual(
            set(graph.members_of("L3/測試生活圈")), {"甲市/一區", "甲市/乙區"}
        )
        self.assertIn("L1/測試大區", graph.groups_of("乙縣/丁區"))

    def test_region_membership_expands_through_its_counties(self) -> None:
        members = set(fixture_graph().members_of("L1/測試大區"))
        self.assertIn("甲市/一區", members)
        self.assertIn("乙縣/己區", members)

    def test_alias_resolves_an_out_of_vocabulary_place_name(self) -> None:
        graph = fixture_graph()
        self.assertEqual(
            set(graph.resolve_alias("通過園區")), {"甲市/乙區", "乙縣/丁區"}
        )

    def test_rejected_site_is_recorded_but_never_resolvable(self) -> None:
        graph = fixture_graph()
        self.assertEqual(graph.resolve_alias("退回園區"), ())
        self.assertNotIn("L5/退回園區", graph.groups)
        self.assertIn(
            "L5/退回園區", {item["id"] for item in graph.excluded_edges}
        )

    def test_membership_does_not_create_a_traversal_shortcut(self) -> None:
        # 丙區 and 乙區 share L1/測試大區 but have no edge, so the route between
        # them must still pay for the real hops rather than jump via the parent.
        reach = {r.district: r for r in fixture_graph().expand(["甲市/丙區"])}
        self.assertEqual(reach["甲市/乙區"].path, ("甲市/丙區", "甲市/一區", "甲市/乙區"))


class ResolutionTests(unittest.TestCase):
    def test_district_codes_resolve_to_their_node(self) -> None:
        self.assertEqual(fixture_graph().resolve(["900101"]), ("甲市/一區",))

    def test_unknown_codes_resolve_to_nothing(self) -> None:
        graph = fixture_graph()
        self.assertEqual(graph.resolve(["zzz", "110100"]), ())
        self.assertIsNone(graph.trace(["zzz"]))

    def test_missing_artifact_disables_without_raising(self) -> None:
        graph = GeoGraph(ROOT / "artifacts" / "definitely-absent.json")
        self.assertFalse(graph.enabled)
        self.assertIsNone(graph.trace(["100226"]))

    def test_trace_never_claims_a_ranking_effect(self) -> None:
        trace = fixture_graph().trace(["900101"])
        self.assertIsNotNone(trace)
        self.assertIs(trace["applied_to_ranking"], False)


class GeoArtifactTests(unittest.TestCase):
    """The checked-in artifacts must stay consistent with what the docs claim."""

    def setUp(self) -> None:
        if not ARTIFACT.is_file():
            self.skipTest("district-graph.json not present")
        self.graph = build_geo_graph(ARTIFACT, AUTHORED)

    def test_artifact_records_provenance(self) -> None:
        metadata = self.graph.metadata
        self.assertEqual(metadata["schema"], "skillweave-district-graph-v1")
        for field in ("dataset_version", "graph_cutoff", "random_seed", "leakage_policy"):
            self.assertIn(field, metadata)

    def test_graph_cutoff_precedes_the_evaluation_window(self) -> None:
        # Reading 06-06 or 06-07 behaviour would leak the scored labels.
        self.assertTrue(self.graph.metadata["graph_cutoff"].startswith("2026-06-05"))
        self.assertEqual(
            self.graph.metadata["train_days"],
            ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"],
        )

    def test_every_district_in_the_official_code_table_is_a_node(self) -> None:
        self.assertEqual(len(self.graph.districts), 368)

    def test_node_ids_are_county_qualified(self) -> None:
        # 北區 exists in 台中, 台南 and 新竹; a bare district name would collide.
        north = [node for node in self.graph.districts if node.endswith("/北區")]
        self.assertGreater(len(north), 1)
        for node in self.graph.districts:
            self.assertIn("/", node)

    def test_spec_example_ranks_淡水區_above_林口區_and_汐止區(self) -> None:
        # docs/geo-graph.md predicts 淡水 over 林口, and 汐止 as the failure mode
        # a naive county-wide fallback would produce.
        ordered = [
            reach.district
            for reach in self.graph.expand(
                ["新北市/八里區"], max_cost=9.0, limit=len(self.graph.districts)
            )
        ]
        self.assertLess(
            ordered.index("新北市/淡水區"), ordered.index("新北市/林口區")
        )
        self.assertLess(
            ordered.index("新北市/林口區"), ordered.index("新北市/汐止區")
        )

    def test_three_ying_shortcut_is_filtered_at_the_default_cutoff(self) -> None:
        excluded = {
            item["effective_date"]
            for item in self.graph.excluded_edges
            if item["kind"] == "shortcut"
        }
        self.assertIn("2026-06-30", excluded)

    def test_authored_shortcuts_do_not_change_any_edge_weight(self) -> None:
        # Both authored shortcuts land on pairs the search logs already connect,
        # so the behaviour-only graph must have exactly the same edges.
        behaviour_only = build_geo_graph(ARTIFACT, AUTHORED, include_authored=False)
        self.assertEqual(self.graph.edge_count, behaviour_only.edge_count)
        self.assertTrue(self.graph.corroborated)

    def test_published_sites_resolve_and_rejected_ones_do_not(self) -> None:
        self.assertEqual(
            set(self.graph.resolve_alias("竹科")), {"新竹市/東區", "新竹縣/寶山鄉"}
        )
        # 內科 is 25.8% concentrated: in job text it is the medical department.
        self.assertEqual(self.graph.resolve_alias("內科"), ())


class L5TableTests(unittest.TestCase):
    """Only the corpus-validated subset of the authored table reaches the graph."""

    def setUp(self) -> None:
        if not ARTIFACT.is_file() or not L5_PUBLISHED.is_file():
            self.skipTest("geo artifacts not present")
        self.graph = build_geo_graph(ARTIFACT, AUTHORED)
        self.published = json.loads(L5_PUBLISHED.read_text(encoding="utf-8"))
        self.source = json.loads(L5_SOURCE.read_text(encoding="utf-8"))

    def test_published_is_a_strict_subset_of_the_authored_table(self) -> None:
        authored = {entry["surface"] for entry in self.source["entries"]}
        published = {entry["surface"] for entry in self.published["entries"]}
        self.assertTrue(published < authored)

    def test_every_published_entry_carries_its_measured_evidence(self) -> None:
        for entry in self.published["entries"]:
            self.assertGreaterEqual(entry["appearances"], self.published["gate"]["min_appearances"])
            if entry.get("requires_occurrence_filter"):
                # Admitted by the occurrence filter instead; the surface gate is
                # the thing it failed, so it cannot also be asserted here.
                continue
            self.assertGreaterEqual(
                entry["concentration"], self.published["gate"]["min_concentration"]
            )

    def test_landmarks_resolve_to_their_districts(self) -> None:
        self.assertEqual(set(self.graph.resolve_alias("中壢工業區")), {"桃園市/中壢區"})
        self.assertEqual(set(self.graph.resolve_alias("南港軟體園區")), {"台北市/南港區"})
        self.assertEqual(set(self.graph.resolve_alias("高鐵台中站")), {"台中市/烏日區"})

    def test_an_arterial_road_resolves_to_every_district_it_runs_through(self) -> None:
        # A road is not a point; claiming one district would be a false claim.
        self.assertGreater(len(self.graph.resolve_alias("忠孝東路")), 1)

    def test_common_words_that_are_also_station_names_are_not_published(self) -> None:
        # 保安 is a security guard, 成功 is success, 幸福 is a benefit adjective.
        # All three are real station names and all three fail the gate. The
        # occurrence model was run on them too and accepted 1, 1 and 4 mentions
        # out of 60, which confirms the rejection rather than overturning it.
        for surface in ("保安", "成功", "幸福"):
            self.assertEqual(self.graph.resolve_alias(surface), ())

    def test_rescued_surfaces_are_marked_as_needing_the_occurrence_filter(self) -> None:
        rescued = [
            entry for entry in self.published["entries"]
            if entry.get("requires_occurrence_filter")
        ]
        self.assertTrue(rescued)
        for entry in rescued:
            # Admitted despite failing the surface gate, so the evidence for the
            # exception has to travel with it: filtered concentration clears the
            # gate, and it is higher than the unfiltered sample.
            filtered = entry["occurrence_filter"]
            self.assertGreaterEqual(
                filtered["accepted_concentration"],
                self.published["gate"]["min_concentration"],
            )
            self.assertGreater(
                filtered["accepted_concentration"], filtered["sample_concentration"]
            )

    def test_a_surface_the_model_could_not_rescue_stays_out(self) -> None:
        # 民權西路 is accepted by the model on 57 of 60 mentions, so nothing is
        # filtered and county consistency does not move (0.5167 -> 0.4912). A
        # surface the model reads as a place everywhere gains nothing from
        # word-sense filtering.
        self.assertEqual(self.graph.resolve_alias("民權西路"), ())

    def test_a_wrong_place_claim_is_fixed_in_the_table_not_by_the_model(self) -> None:
        # 青埔 first named the 高雄 metro station and failed at 0.97 concentration
        # in 桃園 - the model cannot repair that, because it is not a word-sense
        # error. Correcting the entry to 桃園 is what published it.
        self.assertEqual(
            set(self.graph.resolve_alias("青埔")), {"桃園市/中壢區", "桃園市/大園區"}
        )

    def test_the_source_table_is_never_loaded_directly(self) -> None:
        rejected = {entry["surface"] for entry in self.source["entries"]} - {
            entry["surface"] for entry in self.published["entries"]
        }
        # config/geo-authored.json carries its own small site list through a
        # separate gate, so a surface it publishes is legitimately present even
        # when the L5 table rejected it. 南科 is both.
        separately_published = {
            site["id"].split("/", 1)[1]
            for site in json.loads(AUTHORED.read_text(encoding="utf-8"))["sites"]
            if site.get("published", True)
        }
        rejected -= separately_published
        self.assertTrue(rejected)
        for surface in rejected:
            self.assertNotIn(f"L5/{surface}", self.graph.aliases.get(surface, []))
        self.assertNotIn("保安", self.graph.aliases)


class L4TableTests(unittest.TestCase):
    """The checked-in district table is the one the extractor actually runs on."""

    def setUp(self) -> None:
        if not L4_TABLE.is_file():
            self.skipTest("geo-l4-districts.json not present")
        self.table = json.loads(L4_TABLE.read_text(encoding="utf-8"))

    def test_covers_every_domestic_district(self) -> None:
        self.assertEqual(self.table["counts"]["counties"], 22)
        self.assertEqual(self.table["counts"]["districts"], 368)

    def test_agrees_with_the_behaviour_graph_node_set(self) -> None:
        if not ARTIFACT.is_file():
            self.skipTest("district-graph.json not present")
        graph = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        self.assertEqual(
            {f"{row['county']}/{row['district']}" for row in self.table["districts"]},
            {node["id"] for node in graph["nodes"]},
        )
        self.assertEqual(
            {row["code"] for row in self.table["districts"]},
            {code for node in graph["nodes"] for code in node["codes"]},
        )

    def test_carries_no_dataset_content(self) -> None:
        # Public administrative geography only. If a future edit pulls anything
        # from the organizer corpus into this file, that is a publication. Only
        # the data sections are checked; the prose fields legitimately name the
        # dataset columns they were derived from.
        data = {
            key: self.table[key]
            for key in ("counties", "districts", "surfaces", "intra_county_collisions")
        }
        blob = json.dumps(data, ensure_ascii=False)
        for field in ("職缺", "empNo", "talentNo", "職務名稱", "職務內容", "薪資"):
            self.assertNotIn(field, blob)

    def test_data_sections_hold_only_place_names_and_codes(self) -> None:
        for row in self.table["districts"]:
            self.assertEqual(set(row), {"code", "county", "district"})
            self.assertTrue(row["code"].isdigit())
            self.assertEqual(len(row["code"]), 6)

    def test_short_forms_stay_resolvable(self) -> None:
        stripped = self.table["surfaces"]["suffix_dropped"]
        # 八里區 -> 八里 keeps its county mapping, and no surface is one char.
        self.assertEqual(stripped["八里"], {"新北市": "八里區"})
        for surface in stripped:
            self.assertGreaterEqual(len(surface), 2)

    def test_ambiguous_short_forms_map_per_county(self) -> None:
        # 大安 is a district in both 台北市 and 台中市; the table keeps both, and
        # the posting's own 工作城市 is what picks between them.
        self.assertEqual(
            self.table["surfaces"]["suffix_dropped"]["大安"],
            {"台中市": "大安區", "台北市": "大安區"},
        )


class GeoTraceContractTests(unittest.TestCase):
    def search(self, body: dict) -> dict:
        result = handler(event("POST", "/api/v1/jobs/search", body), None)
        self.assertEqual(result["statusCode"], 200)
        return json.loads(result["body"])

    def test_official_contract_fields_are_preserved(self) -> None:
        body = self.search({"query": "作業員", "location_code": ["100226"], "top_k": 10})
        self.assertIn("request_id", body)
        self.assertIn("empStr", body)
        self.assertEqual([row["rank"] for row in body["result"]], list(range(1, 11)))

    def test_geo_trace_is_omitted_without_a_district_code(self) -> None:
        self.assertNotIn("geo_trace", self.search({"query": "作業員"})["meta"])
        # 100600 is 新竹市, a county code: region_trace applies, geo_trace does not.
        county = self.search({"query": "作業員", "location_code": ["100600"]})
        self.assertNotIn("geo_trace", county["meta"])
        unknown = self.search({"query": "作業員", "location_code": ["zzz"]})
        self.assertNotIn("geo_trace", unknown["meta"])

    def test_geo_trace_reports_evidence_for_a_district_search(self) -> None:
        if not lambda_handler.GEO_GRAPH.enabled:
            self.skipTest("district graph artifact not available")
        trace = self.search(
            {"query": "作業員", "location_code": ["100226"]}
        )["meta"]["geo_trace"]
        self.assertEqual(trace["searched_districts"], ["新北市/八里區"])
        self.assertIs(trace["applied_to_ranking"], False)
        self.assertTrue(trace["expansions"])
        for expansion in trace["expansions"]:
            self.assertTrue(expansion["explanation"])
            self.assertTrue(set(expansion["provenance"]) <= {"behaviour", "authored", "external"})

    def test_ranking_is_unchanged_by_the_geo_trace(self) -> None:
        if not lambda_handler.GEO_GRAPH.enabled:
            self.skipTest("district graph artifact not available")
        body = {"query": "作業員", "location_code": ["100226"], "top_k": 10}
        with_graph = self.search(body)
        self.assertIn("geo_trace", with_graph["meta"])
        disabled = GeoGraph(ROOT / "artifacts" / "definitely-absent.json")
        with patch.object(lambda_handler, "GEO_GRAPH", disabled):
            without_graph = self.search(body)
        self.assertNotIn("geo_trace", without_graph["meta"])
        self.assertEqual(
            [row["job_id"] for row in with_graph["result"]],
            [row["job_id"] for row in without_graph["result"]],
        )


class SpecInterfaceTests(unittest.TestCase):
    """docs/geo-graph.md names these two entry points."""

    def test_get_expanded_locations_orders_by_distance(self) -> None:
        graph = fixture_graph()
        results = get_expanded_locations(graph, "甲市/一區", 9.0)
        self.assertEqual([r.cost for r in results], sorted(r.cost for r in results))

    def test_build_geo_graph_accepts_a_cutoff_date(self) -> None:
        early = fixture_graph(cutoff_date="2026-01-01")
        self.assertNotIn(
            "乙縣/己區", {r.district for r in early.expand(["甲市/一區"])}
        )


if __name__ == "__main__":
    unittest.main()
