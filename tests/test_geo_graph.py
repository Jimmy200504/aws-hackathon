from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.lambda_handler as lambda_handler
from app.geo_graph import (
    GENERIC_FULL_NAMES,
    WORD_COLLISION_SURFACES,
    GeoGraph,
    build_geo_graph,
    get_expanded_locations,
)
from app.lambda_handler import handler
from app.ranker import SkillWeaveRanker

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts" / "district-graph.json"
AUTHORED = ROOT / "config" / "geo-authored.json"
L5_SOURCE = ROOT / "config" / "geo-l5-table.json"
L5_PUBLISHED = ROOT / "config" / "geo-l5-published.json"
L4_TABLE = ROOT / "config" / "geo-l4-districts.json"
SIDE_CAR = ROOT / "artifacts" / "demo-job-districts.json"

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

    def test_a_bare_graph_call_claims_no_ranking_effect(self) -> None:
        # The graph on its own cannot know whether a ranker was handed the
        # expansion, so it must not assume one was. Only the caller that passed it
        # to candidate selection may say so.
        trace = fixture_graph().trace(["900101"])
        self.assertIsNotNone(trace)
        self.assertIs(trace["applied_to_ranking"], False)
        self.assertIn("evidence only", trace["ranking_effect"])

    def test_a_ranking_effect_is_only_claimed_when_a_district_resolved(self) -> None:
        graph = fixture_graph()
        applied = graph.trace(["900101"], applied_to_ranking=True)
        self.assertIs(applied["applied_to_ranking"], True)
        self.assertIn("penalty is withheld", applied["ranking_effect"])
        # A code that names no district expands nothing, so there is nothing for
        # candidate selection to have applied.
        self.assertIsNone(graph.trace(["999999"], applied_to_ranking=True))

    def test_no_offline_lift_is_claimed_for_the_expansion(self) -> None:
        # The benchmark reranks candidate sets that are already county-filtered,
        # so it cannot measure a cross-district substitution either way.
        trace = fixture_graph().trace(["900101"], applied_to_ranking=True)
        self.assertIs(trace["offline_lift_measured"], False)


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
        # so neither adds an edge and neither moves a weight. The adjacency map
        # does add edges, which is why this counts shortcuts rather than edges.
        self.assertTrue(self.graph.corroborated)
        for row in self.graph.corroborated:
            hop = self.graph._adjacency[row["a"]][row["b"]]
            self.assertEqual(hop.provenance, "behaviour")
            self.assertAlmostEqual(hop.weight, row["behaviour_substitutability"])
        added = self.graph.metadata.get("adjacency_edges_added", 0)
        behaviour_only = build_geo_graph(ARTIFACT, AUTHORED, include_authored=False)
        self.assertEqual(self.graph.edge_count, behaviour_only.edge_count + added)

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


class AdjacencyTests(unittest.TestCase):
    """The hand-authored land-border map and what behaviour says about it."""

    ADJACENCY = ROOT / "config" / "geo-adjacency.json"
    VALIDATION = ROOT / "reports" / "geo-adjacency-validation.json"

    def setUp(self) -> None:
        if not self.ADJACENCY.is_file() or not ARTIFACT.is_file():
            self.skipTest("adjacency artifacts not present")
        self.adjacency = json.loads(self.ADJACENCY.read_text(encoding="utf-8"))

    def test_covers_every_district_and_names_them_correctly(self) -> None:
        graph = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        nodes = {node["id"] for node in graph["nodes"]}
        for edge in self.adjacency["edges"]:
            self.assertIn(edge["a"], nodes)
            self.assertIn(edge["b"], nodes)
        self.assertEqual(self.adjacency["unknown_nodes"], [])

    def test_no_district_is_stranded_without_explanation(self) -> None:
        # A district with no land neighbour must be a recorded island, not an
        # oversight in the table.
        self.assertEqual(self.adjacency["districts_with_no_land_neighbour"], [])

    def test_edges_are_undirected_and_unique(self) -> None:
        seen = {frozenset((e["a"], e["b"])) for e in self.adjacency["edges"]}
        self.assertEqual(len(seen), len(self.adjacency["edges"]))
        for edge in self.adjacency["edges"]:
            self.assertNotEqual(edge["a"], edge["b"])

    def test_impassable_borders_carry_a_named_barrier(self) -> None:
        for edge in self.adjacency["edges"]:
            if edge["commute"] in {"hard", "impassable"}:
                self.assertNotEqual(edge["barrier"], "none", f"{edge['a']}--{edge['b']}")

    def test_authored_adjacency_never_overwrites_a_behaviour_edge(self) -> None:
        graph = build_geo_graph(ARTIFACT, AUTHORED)
        behaviour = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        for edge in behaviour["substitutable_with"]:
            hop = graph._adjacency[edge["a"]][edge["b"]]
            self.assertEqual(hop.provenance, "behaviour")
            self.assertAlmostEqual(hop.weight, edge["jaccard"])

    def test_authored_edges_are_priced_from_measured_medians(self) -> None:
        graph = build_geo_graph(ARTIFACT, AUTHORED)
        calibration = graph.metadata["adjacency_calibration"]
        self.assertTrue(calibration)
        weights = {
            edge.weight
            for targets in graph._adjacency.values()
            for edge in targets.values()
            if edge.provenance == "authored"
        }
        # Every authored weight is one of the measured medians, never a number
        # written into the table by hand.
        self.assertTrue(weights <= set(calibration.values()))

    def test_switching_the_authored_layer_off_returns_pure_behaviour(self) -> None:
        with_authored = build_geo_graph(ARTIFACT, AUTHORED)
        behaviour_only = build_geo_graph(ARTIFACT, AUTHORED, include_authored=False)
        self.assertGreater(with_authored.edge_count, behaviour_only.edge_count)
        self.assertEqual(
            behaviour_only.edge_count,
            len(json.loads(ARTIFACT.read_text(encoding="utf-8"))["substitutable_with"]),
        )


class AdjacencyValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        report = ROOT / "reports" / "geo-adjacency-validation.json"
        if not report.is_file():
            self.skipTest("geo-adjacency-validation.json not present")
        self.report = json.loads(report.read_text(encoding="utf-8"))

    def test_commute_grade_orders_behaviour(self) -> None:
        # The grades were authored before any of this was computed, so this is
        # a real prediction rather than a description.
        self.assertTrue(self.report["commute_grade_vs_behaviour"]["monotonic"])

    def test_impassable_borders_have_no_behaviour_edge_at_all(self) -> None:
        grades = self.report["commute_grade_vs_behaviour"]["by_grade"]
        self.assertEqual(grades["impassable"]["share_with_behaviour_edge"], 0.0)

    def test_most_authored_adjacency_is_confirmed_by_behaviour(self) -> None:
        self.assertGreater(self.report["coverage"]["authored_share_confirmed"], 0.85)

    def test_most_behaviour_edges_are_not_adjacent(self) -> None:
        # The finding that makes an adjacency-only geo graph the wrong model.
        self.assertGreater(self.report["coverage"]["behaviour_share_not_adjacent"], 0.5)

    def test_the_county_line_effect_is_reported(self) -> None:
        effect = self.report["county_line_effect"]
        self.assertGreater(effect["ratio_intra_over_cross"], 1.0)


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
        self.assertTrue(trace["expansions"])
        for expansion in trace["expansions"]:
            self.assertTrue(expansion["explanation"])
            self.assertTrue(set(expansion["provenance"]) <= {"behaviour", "authored", "external"})

    def test_the_served_trace_reports_that_candidate_selection_used_it(self) -> None:
        if not lambda_handler.GEO_GRAPH.enabled:
            self.skipTest("district graph artifact not available")
        if not lambda_handler.RANKER.job_districts:
            self.skipTest("job district side-car not available")
        trace = self.search({"query": "作業員", "location_code": ["100226"]})["meta"][
            "geo_trace"
        ]
        self.assertIs(trace["applied_to_ranking"], True)
        self.assertIn("results_from_expanded_districts", trace)

    def test_the_trace_reports_no_effect_when_the_side_car_is_absent(self) -> None:
        """Without the join key the expansion cannot act, and must not claim to.

        This is the deployed OpenSearch path's situation as well: the live index
        has no district field, so an expansion there is inert until it is
        reindexed. Reporting it as applied would overstate what shipped.
        """
        if not lambda_handler.GEO_GRAPH.enabled:
            self.skipTest("district graph artifact not available")
        with patch.dict(lambda_handler.RANKER.job_districts, {}, clear=True):
            body = self.search({"query": "作業員", "location_code": ["100226"]})
        self.assertIs(body["meta"]["geo_trace"]["applied_to_ranking"], False)
        self.assertEqual(body["meta"]["geo_trace"]["results_from_expanded_districts"], 0)


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


class QueryTextResolutionTests(unittest.TestCase):
    """A searcher names a place by typing it far more often than by sending a code.

    Two things this has to keep apart, because conflating them was the original
    bug. A surface naming a *different* district in several counties is ambiguous
    and must not resolve on a guess. A surface naming *one place that spans*
    several districts is not ambiguous at all, and every member is the answer.
    """

    def test_a_node_name_in_the_query_resolves(self) -> None:
        districts, _ = fixture_graph().resolve_text("一區 銀行辦事員")
        self.assertEqual(districts, ("甲市/一區",))

    def test_a_group_alias_resolves_to_every_member(self) -> None:
        # 通過園區 spans two districts in two counties. Keying the surface index
        # by county collapsed this to whichever member was read last.
        districts, _ = fixture_graph().resolve_text("通過園區 工程師")
        self.assertEqual(set(districts), {"甲市/乙區", "乙縣/丁區"})

    def test_a_living_area_alias_resolves_to_every_member(self) -> None:
        districts, _ = fixture_graph().resolve_text("測試生活圈 服務員")
        self.assertEqual(set(districts), {"甲市/一區", "甲市/乙區"})

    def test_a_rejected_site_alias_stays_unresolvable_from_text(self) -> None:
        districts, _ = fixture_graph().resolve_text("退回園區 作業員")
        self.assertEqual(districts, ())

    def test_text_with_no_place_name_resolves_to_nothing(self) -> None:
        districts, notes = fixture_graph().resolve_text("銀行辦事員")
        self.assertEqual(districts, ())
        self.assertEqual(notes, [])

    def test_longest_surface_wins_at_a_position(self) -> None:
        graph = fixture_graph()
        # 通過園區 must be read as the site rather than as a bare 園區 fragment.
        districts, notes = graph.resolve_text("通過園區")
        self.assertEqual({note["surface"] for note in notes}, {"通過園區"})
        self.assertEqual(set(districts), {"甲市/乙區", "乙縣/丁區"})

    def test_trace_records_which_source_produced_each_district(self) -> None:
        graph = fixture_graph()
        trace = graph.trace(["900101"], query="通過園區")
        self.assertEqual(trace["resolved_from"]["filter_codes"], ["甲市/一區"])
        self.assertEqual(
            set(trace["resolved_from"]["query_text"]), {"甲市/乙區", "乙縣/丁區"}
        )
        # A district reached by both routes is not searched twice.
        self.assertEqual(
            len(trace["searched_districts"]), len(set(trace["searched_districts"]))
        )

    def test_disabled_graph_resolves_no_text(self) -> None:
        graph = GeoGraph(ROOT / "artifacts" / "definitely-absent.json")
        self.assertEqual(graph.resolve_text("一區"), ((), []))


class QueryTextArtifactTests(unittest.TestCase):
    """The real surface index, including the exclusions the corpus measured."""

    def setUp(self) -> None:
        if not ARTIFACT.is_file() or not L4_TABLE.is_file():
            self.skipTest("geo artifacts not present")
        self.graph = build_geo_graph(ARTIFACT, AUTHORED)

    def test_the_spec_example_resolves_from_text_alone(self) -> None:
        districts, _ = self.graph.resolve_text("八里區 銀行辦事員")
        self.assertEqual(districts, ("新北市/八里區",))

    def test_the_suffix_dropped_form_also_resolves(self) -> None:
        districts, _ = self.graph.resolve_text("八里 銀行辦事員")
        self.assertEqual(districts, ("新北市/八里區",))

    def test_a_park_alias_spanning_two_counties_resolves_to_both(self) -> None:
        districts, _ = self.graph.resolve_text("竹科 工程師")
        self.assertEqual(set(districts), {"新竹市/東區", "新竹縣/寶山鄉"})

    def test_a_living_area_resolves_to_all_five_of_its_districts(self) -> None:
        districts, _ = self.graph.resolve_text("北海岸 服務員")
        self.assertEqual(
            set(districts),
            {
                "新北市/淡水區",
                "新北市/三芝區",
                "新北市/石門區",
                "新北市/金山區",
                "新北市/萬里區",
            },
        )

    def test_a_name_shared_by_several_counties_needs_a_hint(self) -> None:
        # 東區 is a district in 台中, 台南, 嘉義 and 新竹. Picking one would put a
        # place in the searcher's mouth.
        districts, notes = self.graph.resolve_text("東區 店員")
        self.assertEqual(districts, ())
        skipped = [note for note in notes if note.get("skipped") == "ambiguous"]
        self.assertEqual([note["surface"] for note in skipped], ["東區"])
        self.assertGreater(len(skipped[0]["counties"]), 1)

    def test_the_hint_resolves_the_shared_name(self) -> None:
        districts, notes = self.graph.resolve_text("東區 店員", ("台南市",))
        self.assertEqual(districts, ("台南市/東區",))
        # Flagged, because a reader has to be able to tell a name that stood on
        # its own from one the request chose on the searcher's behalf.
        self.assertIs(notes[0]["narrowed_by_hint"], True)

    def test_an_unambiguous_name_is_not_marked_as_hint_narrowed(self) -> None:
        _, notes = self.graph.resolve_text("八里區 銀行辦事員", ("新北市",))
        self.assertIs(notes[0]["narrowed_by_hint"], False)

    def test_word_collision_surfaces_never_resolve(self) -> None:
        # Ordinary words or the commonest street names in Taiwan, not places.
        for surface in WORD_COLLISION_SURFACES:
            districts, notes = self.graph.resolve_text(f"{surface} 業務")
            self.assertEqual(districts, (), surface)
            self.assertEqual(
                [note["skipped"] for note in notes], ["word_collision"], surface
            )

    def test_a_word_collision_still_resolves_at_its_full_official_name(self) -> None:
        # The block is on one spelling, not on the place. 中山 is a road name
        # everywhere; 中山區 is a district, ambiguous only across two counties.
        districts, _ = self.graph.resolve_text("中山區 業務", ("台北市",))
        self.assertEqual(districts, ("台北市/中山區",))

    def test_a_generic_official_name_is_excluded_outright(self) -> None:
        for surface in GENERIC_FULL_NAMES:
            districts, notes = self.graph.resolve_text(f"{surface} 業務")
            self.assertEqual(districts, (), surface)
            self.assertEqual(
                [note["skipped"] for note in notes], ["generic_word"], surface
            )

    def test_a_block_is_not_defeated_by_a_shorter_form_inside_it(self) -> None:
        # The bug this guards: 新社區 was dropped from the index instead of being
        # blocked in the pattern, so every 新社區 matched the surviving 新社 and
        # resolved anyway. 新社 on its own is a place name and stays usable.
        blocked, notes = self.graph.resolve_text("新社區 業務")
        self.assertEqual(blocked, ())
        self.assertEqual([note["surface"] for note in notes], ["新社區"])
        allowed, _ = self.graph.resolve_text("新社 農場")
        self.assertEqual(allowed, ("台中市/新社區",))

    def test_every_blocked_surface_carries_its_measured_precision(self) -> None:
        report = json.loads(
            (ROOT / "reports" / "job-district-extraction.json").read_text("utf-8")
        )
        measured = {
            row["surface"]: row["precision"]
            for key in ("suffix_dropped_rejected", "full_name_rejected")
            for row in report[key]
        }
        self.assertTrue(self.graph.blocked_surfaces)
        for surface, note in self.graph.blocked_surfaces.items():
            # A number in a comment that the checked-in report does not carry is
            # a claim about the data that nobody can audit.
            self.assertIn(surface, measured, surface)
            self.assertAlmostEqual(
                note["job_corpus_precision"], measured[surface], places=4, msg=surface
            )

    def test_no_blocked_surface_is_also_resolvable(self) -> None:
        overlap = set(self.graph.blocked_surfaces) & (
            set(self.graph.text_surfaces) | set(self.graph.aliases)
        )
        self.assertEqual(overlap, set())

    def test_a_rural_name_the_job_side_gate_rejected_still_resolves(self) -> None:
        # 七美鄉 was rejected for the job corpus on one posting of evidence, which
        # is a sample-size verdict rather than a word-collision one. A searcher
        # who types it means it.
        districts, _ = self.graph.resolve_text("七美鄉 廚師")
        self.assertEqual(districts, ("澎湖縣/七美鄉",))

    def test_text_resolution_reaches_the_expansion_the_spec_predicts(self) -> None:
        trace = self.graph.trace(None, query="八里區 銀行辦事員")
        self.assertEqual(trace["resolved_from"]["query_text"], ["新北市/八里區"])
        self.assertEqual(trace["resolved_from"]["filter_codes"], [])
        nearest = trace["expansions"][0]
        self.assertEqual(nearest["district"], "新北市/淡水區")
        # A direct graph call, so no ranker was involved to apply it.
        self.assertIs(trace["applied_to_ranking"], False)


class GeoSwitchContractTests(unittest.TestCase):
    """`use_geo_graph` turns the district layer off at the server.

    The demo switch has to remove the layer rather than hide it, otherwise the
    off state proves nothing about what the response contained.
    """

    def call(self, body: dict) -> dict:
        return handler(event("POST", "/api/v1/jobs/search", body), None)

    def search(self, body: dict) -> dict:
        result = self.call(body)
        self.assertEqual(result["statusCode"], 200)
        return json.loads(result["body"])

    def test_the_flag_defaults_to_on_so_older_callers_are_unaffected(self) -> None:
        if not lambda_handler.GEO_GRAPH.enabled:
            self.skipTest("district graph artifact not available")
        meta = self.search({"query": "八里區 銀行辦事員"})["meta"]
        self.assertIs(meta["geo_graph_enabled"], True)
        self.assertIn("geo_trace", meta)

    def test_switching_off_removes_the_trace_from_the_response(self) -> None:
        meta = self.search({"query": "八里區 銀行辦事員", "use_geo_graph": False})["meta"]
        self.assertIs(meta["geo_graph_enabled"], False)
        self.assertNotIn("geo_trace", meta)

    def test_the_switch_admits_a_cross_county_substitute(self) -> None:
        """The demonstrable case, pinned end to end.

        林口區's cheapest substitute is 龜山區, which is in 桃園市, so a 新北市
        county filter excludes it by construction. The demo index holds 作業員
        postings there, one of them titled 【林口半導體廠】. With the switch off
        they are unreachable; with it on they are candidates.

        This is the test that used to assert the opposite. It was correct while
        the graph was evidence only, and keeping it would now be pinning a claim
        the code no longer honours.
        """
        if not lambda_handler.GEO_GRAPH.enabled:
            self.skipTest("district graph artifact not available")
        if not lambda_handler.RANKER.job_districts:
            self.skipTest("job district side-car not available")
        base = {"query": "林口區 作業員", "top_k": 10}
        on = self.search(base)
        off = self.search({**base, "use_geo_graph": False})
        admitted = {row["job_id"] for row in on["result"]} - {
            row["job_id"] for row in off["result"]
        }
        self.assertTrue(admitted, "expansion admitted no posting")
        self.assertEqual(on["meta"]["geo_trace"]["results_from_expanded_districts"], len(admitted))
        rows = {row["job_id"]: row for row in on["result"]}
        for job_id in admitted:
            row = rows[job_id]
            # Admitted because the graph vouched for its district, and the
            # response says so per row rather than leaving it to be inferred.
            self.assertTrue(row["geo"]["substituted_for_searched_area"])
            self.assertGreater(row["geo"]["substitutability"], 0.0)
            self.assertNotEqual(row["city"], "新北市")
            self.assertIn("鄰近行政區", row["why"])

    def test_the_expansion_never_outranks_the_area_that_was_asked_for(self) -> None:
        """A substitute is admitted, not promoted.

        The location feature stays in the value set the ranking model was trained
        on: positive for the requested area, neutral for a vouched substitute,
        negative otherwise. Giving a substitute a positive weight would mean
        inventing a magnitude and feeding the model a value it never saw.
        """
        if not lambda_handler.GEO_GRAPH.enabled or not lambda_handler.RANKER.job_districts:
            self.skipTest("geo artifacts not available")
        rows = self.search({"query": "林口區 作業員", "top_k": 10})["result"]
        for row in rows:
            geo = row.get("geo")
            if geo and geo["substituted_for_searched_area"]:
                self.assertEqual(row["features"]["location"], 0.0)
            elif row["city"] == "新北市":
                self.assertGreater(row["features"]["location"], 0.0)

    def test_a_query_naming_no_district_is_untouched(self) -> None:
        """No district resolved means no expansion, so nothing may change."""
        base = {"query": "作業員", "top_k": 10}
        on = self.search(base)
        off = self.search({**base, "use_geo_graph": False})
        self.assertNotIn("geo_trace", on["meta"])
        self.assertEqual(
            [(row["job_id"], row["rank"], row["score"]) for row in on["result"]],
            [(row["job_id"], row["rank"], row["score"]) for row in off["result"]],
        )
        self.assertEqual(on["empStr"], off["empStr"])

    def test_the_switch_leaves_the_county_layer_alone(self) -> None:
        """Two switches, two layers. Turning the district layer off must not
        take region_trace with it, or the demo would misattribute the loss."""
        if not lambda_handler.REGION_GRAPH.enabled:
            self.skipTest("region graph artifact not available")
        meta = self.search(
            {"query": "作業員", "location_code": ["100200"], "use_geo_graph": False}
        )["meta"]
        self.assertIn("region_trace", meta)
        self.assertNotIn("geo_trace", meta)

    def test_a_non_boolean_flag_is_rejected(self) -> None:
        result = self.call({"query": "作業員", "use_geo_graph": "false"})
        self.assertEqual(result["statusCode"], 400)
        body = json.loads(result["body"])
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertIn("use_geo_graph", body["error"]["message"])

    def test_reported_state_follows_the_artifact_not_the_request(self) -> None:
        """A deployment without the artifact is off however the request asked."""
        disabled = GeoGraph(ROOT / "artifacts" / "definitely-absent.json")
        with patch.object(lambda_handler, "GEO_GRAPH", disabled):
            meta = self.search({"query": "八里區 銀行辦事員", "use_geo_graph": True})["meta"]
        self.assertIs(meta["geo_graph_enabled"], False)
        self.assertNotIn("geo_trace", meta)


class QueryTextContractTests(unittest.TestCase):
    def search(self, body: dict) -> dict:
        result = handler(event("POST", "/api/v1/jobs/search", body), None)
        self.assertEqual(result["statusCode"], 200)
        return json.loads(result["body"])

    def test_a_typed_district_produces_a_geo_trace(self) -> None:
        if not lambda_handler.GEO_GRAPH.enabled:
            self.skipTest("district graph artifact not available")
        body = self.search({"query": "八里區 銀行辦事員", "top_k": 5})
        self.assertIn("request_id", body)
        self.assertIn("empStr", body)
        trace = body["meta"]["geo_trace"]
        self.assertEqual(trace["resolved_from"]["query_text"], ["新北市/八里區"])
        # The official contract fields are untouched by the expansion; only the
        # candidate set and this additive key change.
        self.assertEqual([row["rank"] for row in body["result"]], [1, 2, 3, 4, 5])
        self.assertIs(trace["offline_lift_measured"], False)

    def test_an_ambiguous_typed_name_expands_nothing_but_says_why(self) -> None:
        if not lambda_handler.GEO_GRAPH.enabled:
            self.skipTest("district graph artifact not available")
        trace = self.search({"query": "東區 店員"})["meta"]["geo_trace"]
        self.assertEqual(trace["searched_districts"], [])
        self.assertEqual(trace["expansions"], [])
        note = trace["query_text_matches"][0]
        self.assertEqual(note["surface"], "東區")
        self.assertEqual(note["skipped"], "ambiguous")
        self.assertGreater(len(note["counties"]), 1)

    def test_a_query_naming_no_place_carries_no_geo_trace(self) -> None:
        if not lambda_handler.GEO_GRAPH.enabled:
            self.skipTest("district graph artifact not available")
        self.assertNotIn("geo_trace", self.search({"query": "銀行辦事員"})["meta"])

    def test_a_county_filter_narrows_an_ambiguous_typed_name(self) -> None:
        """The filter code supplies the county the text could not.

        100600 is 新竹市, which is a county code and so resolves no district of
        its own. It is still the evidence that decides which 東區 was meant, and
        that only works if `Ranker.county_hints` and the graph's county keys
        agree on spelling after normalisation.
        """
        if not lambda_handler.GEO_GRAPH.enabled:
            self.skipTest("district graph artifact not available")
        trace = self.search({"query": "東區 店員", "location_code": ["100600"]})["meta"][
            "geo_trace"
        ]
        # 台灣 and 東區 also arrive from the request; neither is a county, and a
        # field named county_hint must not report them as one.
        self.assertEqual(trace["county_hint"], ["新竹市"])
        self.assertEqual(trace["resolved_from"]["filter_codes"], [])
        self.assertEqual(trace["resolved_from"]["query_text"], ["新竹市/東區"])
        self.assertIs(trace["query_text_matches"][0]["narrowed_by_hint"], True)

    def test_ranking_is_unchanged_by_text_resolution(self) -> None:
        if not lambda_handler.GEO_GRAPH.enabled:
            self.skipTest("district graph artifact not available")
        body = {"query": "八里區 銀行辦事員", "top_k": 10}
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


class GeoExpansionTests(unittest.TestCase):
    """The object candidate selection and the trace both read."""

    def test_a_named_district_scores_one_and_a_reached_one_scores_its_route(self) -> None:
        expansion = fixture_graph().for_request(["900101"])
        weights = expansion.substitutability
        self.assertEqual(weights["甲市/一區"], 1.0)
        reached = {r.district: r.substitutability for r in expansion.reaches}
        self.assertTrue(reached)
        for node, share in reached.items():
            self.assertAlmostEqual(weights[node], share)
            self.assertGreater(share, 0.0)
            self.assertLessEqual(share, 1.0)

    def test_only_vouched_districts_appear_so_it_cannot_read_as_a_county(self) -> None:
        graph = fixture_graph()
        expansion = graph.for_request(["900101"])
        self.assertTrue(set(expansion.substitutability) <= set(graph.districts))
        # 戊區 is reachable only through 乙區; whatever the budget admits, nothing
        # outside the graph's own nodes may be listed as substitutable.
        for node in expansion.substitutability:
            self.assertIn("/", node)

    def test_a_district_named_outright_is_not_downgraded_by_also_being_reachable(
        self,
    ) -> None:
        expansion = fixture_graph().for_request(["900101", "900102"])
        self.assertEqual(expansion.substitutability["甲市/乙區"], 1.0)

    def test_counties_span_the_whole_expansion_so_a_filter_can_be_widened(self) -> None:
        expansion = fixture_graph().for_request(["900101"])
        self.assertIn("甲市", expansion.counties)
        for county in expansion.counties:
            self.assertNotIn("/", county)

    def test_an_empty_expansion_is_falsey_and_renders_no_payload(self) -> None:
        graph = fixture_graph()
        empty = graph.for_request(["999999"])
        self.assertFalse(empty)
        self.assertEqual(empty.substitutability, {})
        self.assertIsNone(graph.trace_payload(empty, applied_to_ranking=True))

    def test_the_trace_describes_the_same_expansion_the_ranker_was_given(self) -> None:
        # One computation, two consumers. If these could diverge the panel would
        # be describing districts candidate selection never saw.
        graph = fixture_graph()
        expansion = graph.for_request(["900101"])
        payload = graph.trace_payload(expansion)
        self.assertEqual(
            [item["district"] for item in payload["expansions"]],
            [reach.district for reach in expansion.reaches],
        )
        self.assertEqual(payload["searched_districts"], list(expansion.searched))


class JobDistrictSideCarTests(unittest.TestCase):
    """artifacts/demo-job-districts.json, the join key from a district to a job."""

    def setUp(self) -> None:
        if not SIDE_CAR.is_file():
            self.skipTest("demo job district side-car not present")
        self.payload = json.loads(SIDE_CAR.read_text(encoding="utf-8"))

    def test_the_artifact_records_its_provenance(self) -> None:
        self.assertEqual(self.payload["schema"], "skillweave-demo-job-districts-v1")
        for key in (
            "dataset_version",
            "graph_cutoff",
            "index_version",
            "schema_fingerprint",
            "random_seed",
        ):
            self.assertIsNotNone(self.payload.get(key), key)

    def test_it_is_keyed_to_the_demo_index_it_was_built_from(self) -> None:
        # District annotations keyed by another index's job ids would annotate the
        # wrong postings, which is worse than annotating none.
        if not ARTIFACT.is_file():
            self.skipTest("geo artifacts not present")
        demo = json.loads(
            (ROOT / "artifacts" / "demo-index.json").read_text(encoding="utf-8")
        )
        self.assertEqual(self.payload["index_version"], demo["metadata"]["index_version"])
        demo_ids = {str(job["id"]) for job in demo["jobs"]}
        self.assertTrue(set(self.payload["jobs"]) <= demo_ids)

    def test_every_annotated_district_exists_in_the_graph(self) -> None:
        if not ARTIFACT.is_file():
            self.skipTest("geo artifacts not present")
        graph = build_geo_graph(ARTIFACT, AUTHORED)
        for job_id, nodes in self.payload["jobs"].items():
            self.assertTrue(nodes, job_id)
            for node in nodes:
                # An annotation the graph cannot reach is dead weight that would
                # silently never match an expansion.
                self.assertIn(node, graph.districts, node)

    def test_the_thin_demo_coverage_is_stated_next_to_the_full_corpus_figure(self) -> None:
        counts = self.payload["counts"]
        self.assertLess(counts["coverage"], 0.10)
        # Recorded so 4.8% on the demo index is not mistaken for the extractor's
        # 27.8% of the eligible corpus.
        self.assertGreater(counts["source_coverage_of_eligible"], counts["coverage"])

    def test_it_is_byte_identical_to_a_rebuild_from_its_inputs(self) -> None:
        source = ROOT / "artifacts" / "job-districts.json"
        if not source.is_file():
            self.skipTest("artifacts/job-districts.json is gitignored and absent")
        from scripts.build_demo_job_districts import build, serialise

        rebuilt = serialise(build(ROOT / "artifacts" / "demo-index.json", source))
        self.assertEqual(SIDE_CAR.read_text(encoding="utf-8"), rebuilt)


class GeoShareTests(unittest.TestCase):
    """How a posting's districts turn into one substitutability number."""

    def setUp(self) -> None:
        self.ranker = lambda_handler.RANKER
        if not self.ranker.job_districts:
            self.skipTest("job district side-car not available")

    def test_a_posting_in_two_districts_takes_the_better_route(self) -> None:
        multi = next(
            (job_id for job_id, nodes in self.ranker.job_districts.items() if len(nodes) > 1),
            None,
        )
        if multi is None:
            self.skipTest("no multi-district posting in the side-car")
        first, second = self.ranker.job_districts[multi][:2]
        share = self.ranker._geo_share({"id": multi}, {first: 0.1, second: 0.9})
        self.assertEqual(share, 0.9)

    def test_an_unannotated_posting_scores_zero_and_keeps_its_old_behaviour(self) -> None:
        share = self.ranker._geo_share(
            {"id": "definitely-not-a-job-id"}, {"新北市/八里區": 1.0}
        )
        self.assertEqual(share, 0.0)

    def test_no_expansion_means_no_lookup(self) -> None:
        job_id = next(iter(self.ranker.job_districts))
        self.assertEqual(self.ranker._geo_share({"id": job_id}, None), 0.0)
        self.assertEqual(self.ranker._geo_share({"id": job_id}, {}), 0.0)

    def test_a_side_car_built_for_another_index_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "side-car.json"
            path.write_text(
                json.dumps({"index_version": "not-this-index", "jobs": {"1": ["甲市/一區"]}}),
                encoding="utf-8",
            )
            ranker = SkillWeaveRanker(
                self.ranker.artifact_path, job_districts_path=path
            )
            self.assertEqual(ranker.job_districts, {})


if __name__ == "__main__":
    unittest.main()
