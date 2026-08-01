#!/usr/bin/env python3
"""Resolve每筆職缺的 L4 行政區，train-only，逐條記錄 provenance。

`職缺.csv` 只有縣市級的 `工作城市`，所以 L4 只能從 JD 文字取得。這支腳本用
官方 368 個行政區名比對，並處理兩件字串比對一定會踩到的事。

歧義：`北區` 同時存在於台中市、台南市、新竹市，`東區` 有四個。單看區名無法
定位，因此每一筆比對都必須與該職缺自己的 `工作城市` 一致，否則丟棄。這條規則
同時擋掉把「越南」「日本」當行政區的錯誤，因為它們不是任何縣市的行政區。

省略後綴：職稱常把「信義區」寫成「信義」。但剝掉後綴會產生大量常用詞——
「三重」是三重區也是「三重防護」，「淡水」是淡水區也是淡水魚，「中正」是
中正區也是全台最常見的路名。哪些安全不靠判斷，靠量測：如果一個省略形真的
指地方，出現它的職缺會集中在該行政區所屬的縣市；如果它只是普通詞彙，分布會
貼近該縣市的基準率。虛無假設因此是「隨機出現」，而不是一個憑感覺挑的百分比。

門檻直接設在「發布出去的節點有多少比例是錯的」，不用相關性的代理指標。倍數
（lift）的上限是 1/基準率，`大安區` 橫跨台中市與台北市（合計語料 32%）時永遠
到不了 3 倍；固定的「距離隨機多遠」則反過來誤殺 `雙溪`、`北埔` 這些精確度
60~73% 的偏鄉區。誤留率則兩邊都對，而且它本身就是要控制的量。

只讀 `職缺最後修改時間 <= graph cutoff` 的職缺。用 cutoff 後的 JD 文字建立
圖節點會洩漏評測窗。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import normalize

DEFAULT_DATA = ROOT / "data" / "dataset"
DEFAULT_OUTPUT = ROOT / "artifacts" / "job-districts.json"
DEFAULT_REPORT = ROOT / "reports" / "job-district-extraction.json"
GRAPH_CUTOFF = datetime.fromisoformat("2026-06-05 23:59:59.999")
DOMESTIC_TOP_REGION = "台灣"
DISTRICT_SUFFIXES = "區市鎮鄉"
HAN = re.compile(r"[\u4e00-\u9fff]")

LAYER_FULL = "full_name"
LAYER_STRIPPED = "suffix_dropped"


def load_districts(path: Path) -> tuple[set[str], dict[str, dict[str, str]], dict[str, set[str]]]:
    """Counties, plus surface -> {county: district} for both surface layers."""
    counties: set[str] = set()
    full: dict[str, dict[str, str]] = defaultdict(dict)
    stripped_raw: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if normalize(row.get("CodeNameC", "")) != DOMESTIC_TOP_REGION:
                continue
            code_type = row.get("CodeType", "")
            name_a = normalize(row.get("CodeNameA", ""))
            name_b = normalize(row.get("CodeNameB", ""))
            if code_type == "2" and name_a:
                counties.add(name_a)
            elif code_type == "3" and name_a and name_b:
                full[name_a][name_b] = name_a
                if len(name_a) > 1 and name_a[-1] in DISTRICT_SUFFIXES:
                    short = name_a[:-1]
                    # A single character is not a usable surface: 東區 -> 東.
                    if len(short) >= 2:
                        stripped_raw[short][name_b].add(name_a)
    stripped: dict[str, dict[str, str]] = {}
    intra_county_collisions: dict[str, set[str]] = {}
    for short, per_county in stripped_raw.items():
        collisions = {
            county for county, names in per_county.items() if len(names) > 1
        }
        if collisions:
            # Two districts in one county sharing a short form cannot be resolved.
            intra_county_collisions[short] = collisions
            continue
        stripped[short] = {
            county: next(iter(names)) for county, names in per_county.items()
        }
    return counties, {"full": dict(full), "stripped": stripped}, intra_county_collisions


def build_pattern(surfaces: list[str]) -> re.Pattern[str]:
    # Longest first so 信義區 wins over 信義 at the same position.
    ordered = sorted(surfaces, key=len, reverse=True)
    return re.compile("|".join(re.escape(surface) for surface in ordered))


def wilson_bounds(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a proportion, so a small sample is penalised, not vetoed.

    A hard minimum-appearance floor discards obviously correct rural surfaces:
    鹿野 appears 18 times at 100% precision against a 0.36% base rate. A binomial
    interval keeps that decision available while still refusing to trust a handful
    of noisy matches, and it does not disadvantage the sparse eastern counties the
    L4 layer most needs to cover.
    """
    if total <= 0:
        return 0.0, 1.0
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / total + z * z / (4 * total * total)
        )
        / denominator
    )
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _odds(proportion: float) -> float:
    return proportion / (1.0 - proportion) if proportion < 1.0 else math.inf


def bounded_error(value: float, cap: float = 99.0) -> float | None:
    """Report an error rate readably. A surface with zero correct matches has
    unbounded odds, which is a verdict rather than a number worth printing."""
    if not math.isfinite(value):
        return None
    return round(min(value, cap), 4)


def kept_error_rate(inside: int, appearances: int, expected: float) -> float:
    """Upper bound on the share of kept matches that are not place references.

    A mention outside the candidate counties is certainly not a place reference,
    and a non-place word appears at roughly the same rate in every county, so the
    mentions the county guard cannot remove scale with the candidate counties'
    share of the corpus. Gating on this rather than on a correlation statistic
    matters because a ratio has ceiling 1/expected, which makes 大安區
    unreachable, while a fixed distance-from-chance silently rejects 雙溪 and
    北埔 at 60-73% precision. This quantity is the one actually being controlled:
    the fraction of published nodes that are wrong.
    """
    outside = appearances - inside
    if appearances <= 0 or expected >= 1.0:
        return math.inf
    _, outside_upper = wilson_bounds(outside, appearances)
    return _odds(outside_upper) * _odds(expected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--max-error",
        type=float,
        default=0.10,
        help="reject a surface whose kept matches would be wrong more often than "
        "this, measured on the conservative bound",
    )
    parser.add_argument(
        "--min-appearances",
        type=int,
        default=5,
        help="a surface needs this many postings before it is judged at all",
    )
    parser.add_argument(
        "--review-min-appearances",
        type=int,
        default=400,
        help="collocations are reported for surfaces at least this frequent",
    )
    parser.add_argument(
        "--review-max-precision",
        type=float,
        default=0.93,
        help="accepted surfaces below this precision still carry occurrence-level error",
    )
    parser.add_argument(
        "--label-place-max-error",
        type=float,
        default=0.03,
        help="a collocation this clean is a trustworthy positive label",
    )
    parser.add_argument(
        "--label-not-place-min-error",
        type=float,
        default=0.50,
        help="a collocation this dirty is a trustworthy negative label",
    )
    parser.add_argument(
        "--label-min-postings",
        type=int,
        default=30,
        help="a collocation needs this much support before it can be a label",
    )
    parser.add_argument(
        "--review-collocations",
        type=int,
        default=30,
        help="collocations kept per surface in the report; the default keeps the "
        "checked-in report readable, and scripts/judge_district_collocations.py "
        "needs all of them, so raise it and write to a separate --report path",
    )
    args = parser.parse_args()

    started = time.monotonic()
    counties, tables, collisions = load_districts(args.data_dir / "城市對照表.csv")
    full_table, stripped_table = tables["full"], tables["stripped"]
    print(
        f"{len(counties)} counties, {len(full_table)} full names, "
        f"{len(stripped_table)} suffix-dropped surfaces, "
        f"{len(collisions)} dropped for intra-county collision",
        flush=True,
    )
    surface_layer = {name: LAYER_FULL for name in full_table}
    for name in stripped_table:
        surface_layer.setdefault(name, LAYER_STRIPPED)
    candidates = {**{k: v for k, v in stripped_table.items()}, **full_table}
    pattern = build_pattern(list(surface_layer))
    # Eight counties are named after their own seat, so the same two characters
    # can qualify a county or name a district. 桃園大園 is 大園區 inside 桃園市,
    # not 桃園區. Restricting the rule to these surfaces keeps 中和永和, which is
    # two districts of 新北市 listed together, intact.
    seat_surface = {
        surface: county
        for surface, per_county in candidates.items()
        for county in per_county
        if surface + "市" == county or surface + "縣" == county
    }
    print(f"county-seat surfaces: {sorted(seat_surface)}", flush=True)

    stats = Counter()
    base_rate: Counter[str] = Counter()
    surface_counties: dict[str, Counter[str]] = defaultdict(Counter)
    buffered: list[tuple[str, str, dict[str, str]]] = []
    # (surface, next Han character) -> [appearances, appearances inside a
    # candidate county]. A surface-level verdict cannot separate 三重店 from
    # 三重防護; the collocation is the smallest unit that can.
    #
    # The key is one character, not a fixed-width window. A two-character window
    # splits one street across 中正路1, 中正路7, 中正路二 and 中正路, which
    # fragments the support the deterministic test needs. One character keeps
    # 北區和緯 and 北區忠明 apart while collapsing the digit and punctuation
    # variants of the same phrase.
    collocations: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    colloc_example: dict[tuple[str, str], str] = {}

    print("Scanning pre-cutoff postings…", flush=True)
    with (args.data_dir / "職缺.csv").open(encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), 1):
            if index % 200_000 == 0:
                print(f"  {index:,} rows", flush=True)
            stats["rows"] += 1
            try:
                if datetime.fromisoformat(row.get("職缺最後修改時間", "")) > GRAPH_CUTOFF:
                    stats["post_cutoff_excluded"] += 1
                    continue
            except ValueError:
                stats["unparsable_timestamp"] += 1
                continue
            county = normalize(row.get("工作城市", ""))
            if county not in counties:
                stats["county_unresolvable"] += 1
                continue
            stats["eligible"] += 1
            base_rate[county] += 1

            title = normalize(row.get("職務名稱", ""))
            content = normalize(row.get("職務內容", ""))
            found: dict[str, str] = {}
            contexts: dict[tuple[str, str], str] = {}
            for field, text in (("title", title), ("content", content)):
                if not text:
                    continue
                spans = [(m.start(), m.end(), m.group(0)) for m in pattern.finditer(text)]
                qualifier = set()
                for position in range(len(spans) - 1):
                    _, a_end, a_surface = spans[position]
                    b_start, _, b_surface = spans[position + 1]
                    if a_end != b_start or seat_surface.get(a_surface) != county:
                        continue
                    if county in candidates[b_surface]:
                        # 桃園 immediately before 大園 is qualifying the county.
                        qualifier.add(position)
                for position, (start, end, surface) in enumerate(spans):
                    if position in qualifier:
                        stats["match_rejected_county_qualifier"] += 1
                        continue
                    following = text[end : end + 1]
                    if following and surface + following in counties:
                        # 桃園市 names the county, not 桃園區, so a posting reading
                        # 桃園市大園區 must not be tagged 桃園區. The county guard
                        # cannot catch this, because 桃園市 does contain a 桃園區.
                        # 彰化縣, 屏東縣, 苗栗縣, 南投縣, 台東縣, 花蓮縣 and 宜蘭縣
                        # collide with their own seat the same way. The full-name
                        # layer still matches 彰化市 directly, since the pattern
                        # prefers the longer surface.
                        stats["match_rejected_is_county_name"] += 1
                        continue
                    # Title wins: it is the shorter, higher-signal field.
                    if surface not in found or field == "title":
                        found[surface] = field
                    key = following if HAN.match(following) else ""
                    contexts.setdefault((surface, key), text[start : end + 8].strip())
            if not found:
                continue
            for surface in found:
                surface_counties[surface][county] += 1
            for key, snippet in contexts.items():
                inside = county in candidates[key[0]]
                entry = collocations[key]
                entry[0] += 1
                entry[1] += int(inside)
                colloc_example.setdefault(key, snippet)
            buffered.append((row["職缺編號"], county, found))

    total_eligible = sum(base_rate.values()) or 1
    share = {name: count / total_eligible for name, count in base_rate.items()}

    judged: dict[str, dict] = {}
    for surface, layer in surface_layer.items():
        observed = surface_counties.get(surface, Counter())
        appearances = sum(observed.values())
        candidate_counties = set(candidates[surface])
        inside = sum(count for c, count in observed.items() if c in candidate_counties)
        expected = sum(share.get(c, 0.0) for c in candidate_counties)
        precision = inside / appearances if appearances else 0.0
        lift = precision / expected if expected else 0.0
        # The same gate applies to both layers. Trusting a full official name
        # unconditionally lets 新社區 through, where the text almost always means
        # a newly built residential community rather than 台中市新社區, and 北區,
        # which is usually "the northern region" of a sales territory.
        error_rate = kept_error_rate(inside, appearances, expected)
        accepted = (
            appearances >= args.min_appearances and error_rate <= args.max_error
        )
        judged[surface] = {
            "surface": surface,
            "layer": layer,
            "counties": sorted(candidate_counties),
            "appearances": appearances,
            "inside_candidate_counties": inside,
            "precision": round(precision, 4),
            "expected_share": round(expected, 4),
            "lift": round(lift, 3),
            "kept_error_rate_upper_95": bounded_error(error_rate),
            "accepted": accepted,
            # Mentions outside a candidate county are certainly not place
            # references, and a non-place word appears at roughly the same rate
            # everywhere. Scaling those by the candidate counties' share of the
            # corpus estimates how many kept matches are still wrong, which is
            # the error the county guard cannot remove.
            "estimated_false_keeps": round(
                (appearances - inside) / (1.0 - expected) * expected
            )
            if expected < 1.0
            else 0,
        }

    accepted_surfaces = {name for name, row in judged.items() if row["accepted"]}
    rows_out = []
    layer_hits = Counter()
    field_hits = Counter()
    for job_id, county, found in buffered:
        resolved: dict[str, dict[str, str]] = {}
        for surface, field in found.items():
            if surface not in accepted_surfaces:
                stats["match_rejected_surface"] += 1
                continue
            district = candidates[surface].get(county)
            if district is None:
                # The surface names a district, but not in this posting's county.
                stats["match_rejected_county_mismatch"] += 1
                continue
            layer = surface_layer[surface]
            previous = resolved.get(district)
            if previous is None or (
                previous["layer"] == LAYER_STRIPPED and layer == LAYER_FULL
            ):
                resolved[district] = {"surface": surface, "layer": layer, "field": field}
        if not resolved:
            continue
        for district, meta in resolved.items():
            layer_hits[meta["layer"]] += 1
            field_hits[meta["field"]] += 1
        rows_out.append(
            {
                "job_id": job_id,
                "county": county,
                "districts": [
                    {"district": district, **meta}
                    for district, meta in sorted(resolved.items())
                ],
            }
        )

    unique = sum(1 for row in rows_out if len(row["districts"]) == 1)
    payload = {
        "metadata": {
            "schema": "skillweave-job-districts-v1",
            "dataset_version": "1111-2026-06-01_2026-06-07",
            "graph_cutoff": GRAPH_CUTOFF.isoformat(sep=" "),
            "leakage_policy": "only postings last modified on or before the graph cutoff",
            "disambiguation": "a matched surface is kept only if it names a district inside the posting's own 工作城市",
            "surface_layers": [LAYER_FULL, LAYER_STRIPPED],
            "surface_gate": "conservative upper bound on the share of kept matches "
            "that are not place references, applied to both layers",
            "max_error": args.max_error,
            "min_appearances": args.min_appearances,
            "collocation_key": "the single Han character following the surface",
            "label_place_max_error": args.label_place_max_error,
            "label_not_place_min_error": args.label_not_place_min_error,
            "label_min_postings": args.label_min_postings,
            "random_seed": 1111,
        },
        "stats": {
            **dict(stats),
            "postings_with_district": len(rows_out),
            "coverage_of_eligible": round(len(rows_out) / total_eligible, 4),
            "coverage_of_all_rows": round(len(rows_out) / max(1, stats["rows"]), 4),
            "single_district_postings": unique,
            "single_district_share": round(unique / max(1, len(rows_out)), 4),
            "by_layer": dict(layer_hits),
            "by_field": dict(field_hits),
            "accepted_surfaces": len(accepted_surfaces),
            "rejected_surfaces": len(judged) - len(accepted_surfaces),
        },
        "jobs": rows_out,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )

    # Surfaces where an occurrence-level decision is still needed: either the
    # surface was rejected outright but is frequent enough that blanket rejection
    # costs real coverage, or it was accepted while still carrying error.
    review_targets = [
        row
        for row in judged.values()
        if row["appearances"] >= args.review_min_appearances
        and (not row["accepted"] or row["precision"] < args.review_max_precision)
    ]
    review_targets.sort(key=lambda row: -row["estimated_false_keeps"])
    review = []
    for row in review_targets[:40]:
        surface = row["surface"]
        expected = row["expected_share"]
        entries = []
        for (candidate_surface, following), (total, inside) in collocations.items():
            if candidate_surface != surface or total < 5:
                continue
            colloc_error = kept_error_rate(inside, total, expected)
            # Only the two clean bands become labels. A collocation in between is
            # exactly where the surface-level gate already fails: 北區和緯 lands
            # at 12% error and is a real street in 台南市北區, so calling it a
            # negative would penalise a model for answering correctly.
            if total < args.label_min_postings:
                verdict = None
            elif colloc_error <= args.label_place_max_error:
                verdict = "place"
            elif colloc_error >= args.label_not_place_min_error:
                verdict = "not_place"
            else:
                verdict = None
            entries.append(
                {
                    "following": following,
                    "example": colloc_example.get((surface, following), ""),
                    "postings": total,
                    "precision": round(inside / total, 4),
                    "kept_error_rate_upper_95": bounded_error(colloc_error),
                    "label": verdict,
                    "needs_semantic_judgement": verdict is None,
                }
            )
        entries.sort(key=lambda entry: -entry["postings"])
        review.append(
            {
                **{
                    key: row[key]
                    for key in (
                        "surface",
                        "layer",
                        "counties",
                        "appearances",
                        "precision",
                        "kept_error_rate_upper_95",
                        "accepted",
                        "estimated_false_keeps",
                    )
                },
                "collocations_total": len(entries),
                "collocations_labelled_place": sum(
                    1 for entry in entries if entry["label"] == "place"
                ),
                "collocations_labelled_not_place": sum(
                    1 for entry in entries if entry["label"] == "not_place"
                ),
                "collocations_needing_judgement": sum(
                    1 for entry in entries if entry["needs_semantic_judgement"]
                ),
                "collocations": entries[: args.review_collocations],
            }
        )

    stripped_rows = [row for row in judged.values() if row["layer"] == LAYER_STRIPPED]
    stripped_rows.sort(
        key=lambda row: (
            row["kept_error_rate_upper_95"]
            if row["kept_error_rate_upper_95"] is not None
            else math.inf
        )
    )
    report = {
        "metadata": payload["metadata"],
        "stats": payload["stats"],
        "county_base_rate": {
            name: round(value, 4)
            for name, value in sorted(share.items(), key=lambda kv: -kv[1])
        },
        "intra_county_collisions": {
            name: sorted(value) for name, value in sorted(collisions.items())
        },
        "suffix_dropped_accepted": [row for row in stripped_rows if row["accepted"]],
        "suffix_dropped_rejected": [
            row for row in stripped_rows if not row["accepted"]
        ],
        "occurrence_review_summary": {
            "surfaces": len(review),
            "estimated_wrong_keeps": sum(
                row["estimated_false_keeps"] for row in review
            ),
            "collocations_total": sum(row["collocations_total"] for row in review),
            "labelled_place": sum(row["collocations_labelled_place"] for row in review),
            "labelled_not_place": sum(
                row["collocations_labelled_not_place"] for row in review
            ),
            "needing_semantic_judgement": sum(
                row["collocations_needing_judgement"] for row in review
            ),
            "note": "The labelled bands are a held-out set for measuring a model "
            "before it is trusted on the rest. Nothing here has been sent to a model.",
        },
        "full_name_rejected": sorted(
            (
                row
                for row in judged.values()
                if row["layer"] == LAYER_FULL and not row["accepted"]
            ),
            key=lambda row: -row["appearances"],
        ),
        "occurrence_review_queue": review,
    }
    report["metadata"]["elapsed_seconds"] = round(time.monotonic() - started, 1)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload["stats"], ensure_ascii=False, indent=2), flush=True)
    print(f"Wrote {args.output} and {args.report}", flush=True)


if __name__ == "__main__":
    main()
