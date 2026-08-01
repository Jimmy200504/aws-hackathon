#!/usr/bin/env python3
"""Mine colloquial place-name candidates from pre-cutoff JD text, train-only.

The official location table has no entry for 南科, 竹科, 北車 or 板後, yet those
strings appear in thousands of job postings. This script finds such strings so a
later step can ask an LLM what they mean, and it measures the one thing that
decides whether that LLM answer can be trusted: how concentrated each string is
in a single county.

Concentration is the independent corroboration. `工作城市` is a structured field
the LLM never reads, so if the model claims 南科 is in 台南市 and the postings
that mention 南科 are overwhelmingly 台南市 postings, the claim is grounded in
something other than the model's own output. The same check catches a model that
confuses 南科 with 竹科.

Candidates are anchored on location cue words rather than mined from every
n-gram. A bare frequency scan would return mostly ordinary vocabulary, and the
cue anchoring is also what keeps a full-corpus pass tractable.

Two controls are reported alongside the candidates so the gate can be judged
rather than assumed:

  named checklist    strings already known to be colloquial place names, which
                     should show high concentration
  negative controls  frequent non-location vocabulary, which should sit near the
                     corpus base rate

If the negative controls also concentrate, the gate discriminates nothing and
the design is not worth building on.

Only postings last modified on or before the graph cutoff are read. Creating
graph nodes from post-cutoff JD text would leak the evaluation window.
"""
from __future__ import annotations

import argparse
import csv
import json
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
DEFAULT_REPORT = ROOT / "reports" / "region-alias-candidates.json"
GRAPH_CUTOFF = datetime.fromisoformat("2026-06-05 23:59:59.999")
DOMESTIC_TOP_REGION = "台灣"

CHINESE = r"\u4e00-\u9fff"
# Cues that follow a place name. Only multi-character cues are used for the
# prefix form: a bare 近 matches 最近 and 附近 far more often than a location.
SUFFIX_CUES = (
    "科學園區",
    "工業區",
    "交流道",
    "捷運站",
    "火車站",
    "園區",
    "商圈",
    "夜市",
    "分店",
    "門市",
    "一帶",
    "附近",
    "廠區",
    "站",
    "廠",
    "路",
    "街",
)
PREFIX_CUES = ("位於", "鄰近", "靠近", "座落於", "坐落於")

NAMED_CHECKLIST = (
    "南科",
    "竹科",
    "中科",
    "內科",
    "北車",
    "嘉南",
    "板後",
    "公益",
    "文心",
    "大墩",
    "北北基",
    "桃竹苗",
    "中彰投",
    "雲嘉南",
)
# Frequent vocabulary with no location meaning. These should not concentrate.
NEGATIVE_CONTROLS = (
    "經驗",
    "團隊",
    "客戶",
    "加班",
    "輪班",
    "勞保",
    "獎金",
    "教育訓練",
    "責任",
    "配合",
)


def load_official_names(path: Path) -> tuple[set[str], set[str]]:
    """Domestic counties, and every official place name at any level."""
    counties: set[str] = set()
    official: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if normalize(row.get("CodeNameC", "")) != DOMESTIC_TOP_REGION:
                continue
            name_a = normalize(row.get("CodeNameA", ""))
            name_b = normalize(row.get("CodeNameB", ""))
            code_type = row.get("CodeType", "")
            if code_type == "2" and name_a:
                counties.add(name_a)
            elif code_type == "3" and name_b:
                counties.add(name_b)
            for name in (name_a, name_b):
                if name and name != DOMESTIC_TOP_REGION:
                    official.add(name)
                    # 三峽區 also appears as 三峽 in running text.
                    if len(name) > 2 and name[-1] in "區市鎮鄉縣":
                        official.add(name[:-1])
    return counties, official


def build_patterns() -> tuple[re.Pattern[str], re.Pattern[str]]:
    suffix = "|".join(re.escape(cue) for cue in SUFFIX_CUES)
    prefix = "|".join(re.escape(cue) for cue in PREFIX_CUES)
    return (
        re.compile(rf"([{CHINESE}]{{2,3}})(?={suffix})"),
        re.compile(rf"(?:{prefix})([{CHINESE}]{{2,3}})"),
    )


def candidates_in(text: str, patterns) -> set[str]:
    suffix_pattern, prefix_pattern = patterns
    found: set[str] = set()
    found.update(suffix_pattern.findall(text))
    found.update(prefix_pattern.findall(text))
    return found


def eligible_rows(data_dir: Path, counties: set[str], label: str):
    """Yield (county, text) for pre-cutoff postings with a resolvable county."""
    stats = Counter()
    with (data_dir / "職缺.csv").open(encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), 1):
            if index % 200_000 == 0:
                print(f"  {label}: {index:,} rows", flush=True)
            stats["rows"] += 1
            stamp = row.get("職缺最後修改時間", "")
            try:
                if datetime.fromisoformat(stamp) > GRAPH_CUTOFF:
                    stats["post_cutoff"] += 1
                    continue
            except ValueError:
                stats["unparsable_timestamp"] += 1
                continue
            county = normalize(row.get("工作城市", ""))
            if county not in counties:
                stats["no_county"] += 1
                continue
            stats["eligible"] += 1
            yield county, f"{row.get('職務名稱', '')} {row.get('職務內容', '')}"
    print(f"  {label} stats: {dict(stats)}", flush=True)
    eligible_rows.last_stats = dict(stats)


def concentration(counts: Counter[str]) -> tuple[str, float, int]:
    total = sum(counts.values())
    if not total:
        return "", 0.0, 0
    county, top = counts.most_common(1)[0]
    return county, top / total, total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--min-jobs",
        type=int,
        default=30,
        help="drop candidates seen in fewer postings than this",
    )
    parser.add_argument("--top", type=int, default=60, help="candidates listed in the report")
    args = parser.parse_args()

    started = time.monotonic()
    counties, official = load_official_names(args.data_dir / "城市對照表.csv")
    patterns = build_patterns()
    print(f"{len(counties)} counties, {len(official)} official place names", flush=True)

    print("Pass 1/2 · candidate frequency…", flush=True)
    totals: Counter[str] = Counter()
    base_rate: Counter[str] = Counter()
    for county, text in eligible_rows(args.data_dir, counties, "pass1"):
        base_rate[county] += 1
        for candidate in candidates_in(text, patterns):
            if candidate in official:
                continue
            totals[candidate] += 1
    pass1_stats = eligible_rows.last_stats
    surviving = {name for name, count in totals.items() if count >= args.min_jobs}
    print(
        f"  {len(totals):,} distinct candidates, {len(surviving):,} at >= {args.min_jobs} postings",
        flush=True,
    )

    print("Pass 2/2 · county distribution…", flush=True)
    by_county: dict[str, Counter[str]] = defaultdict(Counter)
    probe_by_county: dict[str, Counter[str]] = defaultdict(Counter)
    evidence: dict[str, str] = {}
    probes = tuple(NAMED_CHECKLIST) + tuple(NEGATIVE_CONTROLS)
    for county, text in eligible_rows(args.data_dir, counties, "pass2"):
        for candidate in candidates_in(text, patterns):
            if candidate not in surviving:
                continue
            by_county[candidate][county] += 1
            if candidate not in evidence:
                position = text.find(candidate)
                evidence[candidate] = text[max(0, position - 12) : position + 18].strip()
        for probe in probes:
            if probe in text:
                probe_by_county[probe][county] += 1
    pass2_stats = eligible_rows.last_stats

    total_eligible = sum(base_rate.values())
    base_county, base_share, _ = concentration(base_rate)

    ranked = []
    for candidate in sorted(surviving):
        county, share, total = concentration(by_county[candidate])
        ranked.append(
            {
                "surface": candidate,
                "postings": total,
                "top_county": county,
                "concentration": round(share, 4),
                "evidence": evidence.get(candidate, ""),
            }
        )
    ranked.sort(key=lambda row: (-row["concentration"], -row["postings"]))

    def probe_rows(names):
        rows = []
        for name in names:
            county, share, total = concentration(probe_by_county[name])
            rows.append(
                {
                    "surface": name,
                    "postings": total,
                    "top_county": county,
                    "concentration": round(share, 4),
                    "is_official_name": name in official,
                }
            )
        return rows

    buckets = Counter()
    for row in ranked:
        buckets[f"{int(row['concentration'] * 10) * 10}%"] += 1

    report = {
        "metadata": {
            "schema": "skillweave-region-alias-candidates-v1",
            "dataset_version": "1111-2026-06-01_2026-06-07",
            "graph_cutoff": GRAPH_CUTOFF.isoformat(sep=" "),
            "leakage_policy": "only postings last modified on or before the graph cutoff",
            "candidate_rule": "2-3 Han characters anchored on a location cue word, excluding official place names",
            "suffix_cues": list(SUFFIX_CUES),
            "prefix_cues": list(PREFIX_CUES),
            "min_jobs": args.min_jobs,
            "random_seed": 1111,
            "elapsed_seconds": None,
        },
        "corpus": {
            "pass1": pass1_stats,
            "pass2": pass2_stats,
            "eligible_postings": total_eligible,
            "base_rate_top_county": base_county,
            "base_rate_top_share": round(base_share, 4),
            "county_base_rate": {
                name: round(count / total_eligible, 4)
                for name, count in base_rate.most_common()
            },
        },
        "candidates": {
            "distinct_total": len(totals),
            "surviving_min_jobs": len(surviving),
            "concentration_buckets": dict(sorted(buckets.items())),
            "top": ranked[: args.top],
            "bottom": ranked[-args.top :],
        },
        "named_checklist": probe_rows(NAMED_CHECKLIST),
        "negative_controls": probe_rows(NEGATIVE_CONTROLS),
    }
    report["metadata"]["elapsed_seconds"] = round(time.monotonic() - started, 1)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote {args.report}", flush=True)


if __name__ == "__main__":
    main()
