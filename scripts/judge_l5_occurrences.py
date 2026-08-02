#!/usr/bin/env python3
"""Rescue L5 surfaces that are real places and ordinary words at the same time.

`scripts/validate_l5_table.py` rejects a surface when its corpus mentions do not
concentrate in the claimed county. For most rejections that is the right answer.
For a specific subset it throws away a real place:

    保安   2,944 mentions, 22% in 台南   保安人員 is a security guard
    成功   8,597 mentions, 26% in 台中   成功 is success
    豐富   9,269 mentions, 28% in 苗栗   豐富的經驗 is rich experience

All three are genuine TRA stations. The surface-level gate cannot keep them,
for the same structural reason the district extractor could not keep 北區.

`scripts/judge_district_collocations.py` already tried a model on that shape of
problem and failed, and the measurement said why: its feature was the single Han
character following the surface, which resolves the easy cases and carries no
information about the ambiguous ones. The conclusion recorded in
`docs/evaluation-limits.md` was that this needs a different feature, not a
different model. This script is that change - the model sees a real text window
around each mention.

The experiment is self-scoring, which is what makes it worth running. A mention
of 保安 can only be the station if the posting is in 台南, so county consistency
is a per-occurrence signal that costs nothing and was never shown to the model.
If the model is separating place from non-place, the mentions it accepts must
concentrate in the claimed county far more than the unfiltered baseline does.
If concentration barely moves, the model is not adding information, exactly as
the collocation attempt was not.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import normalize

DEFAULT_TABLE = ROOT / "config" / "geo-l5-table.json"
DEFAULT_VALIDATION = ROOT / "reports" / "l5-table-validation.json"
DEFAULT_DATA = ROOT / "data" / "dataset"
DEFAULT_SAMPLES = ROOT / "artifacts" / "l5-occurrence-samples.json"
DEFAULT_CACHE = ROOT / "artifacts" / "l5-occurrences.jsonl"
DEFAULT_REPORT = ROOT / "reports" / "l5-occurrence-judgement.json"
DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
GRAPH_CUTOFF = datetime.fromisoformat("2026-06-05 23:59:59.999")
MIN_INTERVAL_SECONDS = 1.05
ALLOWED_REGIONS = {"us-east-1", "us-west-2"}
BATCH_SIZE = 20
WINDOW = 40
PROMPT_VERSION = "l5-occurrence-v1"
RESCUABLE = {"misassigned_county", "ambiguous_surface"}

SYSTEM_PROMPT = """\
你要判斷職缺文字裡的一個詞，是不是在指某個特定地點。

每一筆給你：
  surface   要判斷的詞
  place     這個詞作為地名時所指的地方（車站／園區／地標與其所在行政區）
  text      職缺原文片段，surface 出現在其中

問題：**在這段文字裡**，surface 是不是在指 place 這個地點？

判 yes：
  - 明確在講位置：「近保安車站」「保安路上」「工作地點：保安」
  - 用它描述通勤或周邊：「鄰近OO站」「步行至OO園區」
判 no：
  - 它在這裡是普通詞彙，與地點無關
    例：保安人員／保安器材、成功案例／成功錄取、豐富的經驗、幸福企業、
        和平相處、光復節、市政府標案（指機關不是捷運站）
  - 它是別的專有名詞的一部分，與該地點無關
    例：公司名、產品名、其他縣市的同名道路

只看文字本身判斷，不要猜測職缺可能在哪。
只輸出 JSON，對每一筆的 id 給出 verdict。"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "judgements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["yes", "no"]},
                },
                "required": ["id", "verdict"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["judgements"],
    "additionalProperties": False,
}


def collect_samples(
    table: dict, validation: dict, data_dir: Path, per_surface: int
) -> dict[str, Any]:
    """One corpus pass, reservoir-sampling mentions of every rescuable surface."""
    entries = {entry["surface"]: entry for entry in table["entries"]}
    targets = {
        row["surface"]: row
        for row in validation["entries"]
        if row["verdict"] in RESCUABLE and row["appearances"] >= 200
    }
    print(f"rescuable surfaces: {len(targets)}", flush=True)
    pattern = re.compile("|".join(re.escape(s) for s in sorted(targets, key=len, reverse=True)))

    counties: set[str] = set()
    with (data_dir / "城市對照表.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if normalize(row.get("CodeNameC", "")) == "台灣" and row.get("CodeType") == "2":
                counties.add(normalize(row.get("CodeNameA", "")))

    rng = random.Random(1111)
    pool: dict[str, list[dict]] = defaultdict(list)
    totals: Counter[str] = Counter()
    with (data_dir / "職缺.csv").open(encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), 1):
            if index % 300_000 == 0:
                print(f"  {index:,} rows", flush=True)
            try:
                if datetime.fromisoformat(row.get("職缺最後修改時間", "")) > GRAPH_CUTOFF:
                    continue
            except ValueError:
                continue
            county = normalize(row.get("工作城市", ""))
            if county not in counties:
                continue
            text = f"{normalize(row.get('職務名稱', ''))} {normalize(row.get('職務內容', ''))}"
            seen: set[str] = set()
            for match in pattern.finditer(text):
                surface = match.group(0)
                if surface in seen:
                    continue
                seen.add(surface)
                totals[surface] += 1
                window = text[max(0, match.start() - WINDOW) : match.end() + WINDOW]
                item = {
                    "surface": surface,
                    "job_id": row["職缺編號"],
                    "county": county,
                    "text": window.strip(),
                }
                bucket = pool[surface]
                # Reservoir sampling keeps the sample representative of the whole
                # corpus rather than of whatever order the file happens to be in.
                if len(bucket) < per_surface:
                    bucket.append(item)
                else:
                    position = rng.randrange(totals[surface])
                    if position < per_surface:
                        bucket[position] = item

    samples = []
    for surface, items in sorted(pool.items()):
        target = targets[surface]
        entry = entries[surface]
        for item in items:
            samples.append(
                {
                    **item,
                    "key": hashlib.sha256(
                        f"{surface}\t{item['job_id']}".encode("utf-8")
                    ).hexdigest()[:16],
                    "claimed_counties": entry["counties"],
                    "claimed_districts": entry["districts"],
                    "kind": entry["kind"],
                    "baseline_concentration": target["concentration"],
                    "corpus_appearances": target["appearances"],
                }
            )
    return {
        "schema": "skillweave-l5-occurrence-samples-v1",
        "per_surface": per_surface,
        "surfaces": len(pool),
        "samples": len(samples),
        "corpus_totals": dict(totals),
        "items": samples,
    }


def render_batch(batch: list[dict]) -> str:
    lines = []
    for index, item in enumerate(batch):
        place = "、".join(item["claimed_districts"]) + f"（{item['kind']}）"
        lines.append(
            json.dumps(
                {"id": index, "surface": item["surface"], "place": place, "text": item["text"]},
                ensure_ascii=False,
            )
        )
    return "判斷以下 %d 筆：\n%s" % (len(batch), "\n".join(lines))


def load_cache(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    cache: dict[str, dict] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("key") and row.get("prompt_version") == PROMPT_VERSION:
                cache[row["key"]] = row
    return cache


def judge_batch(client, model_id: str, batch: list[dict]) -> dict[int, str]:
    response = client.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": render_batch(batch)}]}],
        outputConfig={
            "textFormat": {
                "type": "json_schema",
                "structure": {
                    "jsonSchema": {
                        "schema": json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, separators=(",", ":")),
                        "name": "l5_occurrence_judgement",
                        "description": "Per-mention place judgement",
                    }
                },
            }
        },
        inferenceConfig={"temperature": 0, "maxTokens": 4096},
    )
    content = response["output"]["message"]["content"]
    text = next(block["text"] for block in content if "text" in block)
    payload = json.loads(text)
    return {
        int(row["id"]): row["verdict"]
        for row in payload["judgements"]
        if 0 <= int(row["id"]) < len(batch)
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE)
    parser.add_argument("--validation", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--per-surface", type=int, default=60)
    parser.add_argument("--model-id", default=DEFAULT_MODEL)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--resample", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-accepted", type=int, default=15)
    parser.add_argument("--min-concentration", type=float, default=0.60)
    args = parser.parse_args()

    if args.region not in ALLOWED_REGIONS:
        raise SystemExit(f"--region {args.region} is outside {sorted(ALLOWED_REGIONS)}")

    if args.resample or not args.samples.is_file():
        table = json.loads(args.table.read_text(encoding="utf-8"))
        validation = json.loads(args.validation.read_text(encoding="utf-8"))
        payload = collect_samples(table, validation, args.data_dir, args.per_surface)
        args.samples.parent.mkdir(parents=True, exist_ok=True)
        args.samples.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {args.samples}: {payload['samples']} samples", flush=True)
    payload = json.loads(args.samples.read_text(encoding="utf-8"))
    items = payload["items"]

    cache = load_cache(args.cache)
    todo = [item for item in items if item["key"] not in cache]
    batches = [todo[i : i + args.batch_size] for i in range(0, len(todo), args.batch_size)]
    print(
        json.dumps(
            {
                "surfaces": payload["surfaces"],
                "samples": len(items),
                "cached": len(items) - len(todo),
                "batches": len(batches),
                "estimated_minutes": round(len(batches) * MIN_INTERVAL_SECONDS / 60, 1),
                "prompt_version": PROMPT_VERSION,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    if args.dry_run:
        if batches:
            print("\n--- system prompt ---\n" + SYSTEM_PROMPT)
            print("\n--- first batch ---\n" + render_batch(batches[0]))
        return

    if batches:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "bedrock-runtime",
            region_name=args.region,
            config=Config(connect_timeout=10.0, read_timeout=120.0,
                          retries={"max_attempts": 5, "mode": "adaptive"}),
        )
        args.cache.parent.mkdir(parents=True, exist_ok=True)
        last = 0.0
        started = time.monotonic()
        with args.cache.open("a", encoding="utf-8") as handle:
            for number, batch in enumerate(batches, 1):
                wait = MIN_INTERVAL_SECONDS - (time.monotonic() - last)
                if wait > 0:
                    time.sleep(wait)
                last = time.monotonic()
                try:
                    verdicts = judge_batch(client, args.model_id, batch)
                except Exception as exc:
                    print(f"  batch {number}/{len(batches)} failed: {type(exc).__name__}", flush=True)
                    continue
                for index, item in enumerate(batch):
                    verdict = verdicts.get(index)
                    if verdict is None:
                        continue
                    handle.write(
                        json.dumps(
                            {
                                "key": item["key"], "surface": item["surface"],
                                "job_id": item["job_id"], "county": item["county"],
                                "text": item["text"], "verdict": verdict,
                                "prompt_version": PROMPT_VERSION,
                                "extractor_model": args.model_id,
                            },
                            ensure_ascii=False,
                        ) + "\n"
                    )
                handle.flush()
                if number % 20 == 0 or number == len(batches):
                    print("  batch %d/%d  %.1f min" % (number, len(batches),
                                                       (time.monotonic() - started) / 60), flush=True)

    judged = load_cache(args.cache)
    by_surface: dict[str, list[dict]] = defaultdict(list)
    claimed: dict[str, list[str]] = {}
    for item in items:
        row = judged.get(item["key"])
        if row:
            by_surface[item["surface"]].append({**item, "verdict": row["verdict"]})
        claimed[item["surface"]] = item["claimed_counties"]

    results = []
    for surface, rows in sorted(by_surface.items()):
        want = set(claimed[surface])
        accepted = [r for r in rows if r["verdict"] == "yes"]
        rejected = [r for r in rows if r["verdict"] == "no"]

        def share(subset: list[dict]) -> float | None:
            if not subset:
                return None
            return round(sum(1 for r in subset if r["county"] in want) / len(subset), 4)

        base = share(rows)
        acc = share(accepted)
        results.append(
            {
                "surface": surface,
                "claimed_counties": sorted(want),
                "sampled": len(rows),
                "accepted": len(accepted),
                "rejected": len(rejected),
                "sample_concentration": base,
                "accepted_concentration": acc,
                "rejected_concentration": share(rejected),
                "lift": round(acc - base, 4) if acc is not None and base is not None else None,
                "corpus_baseline_concentration": next(
                    (i["baseline_concentration"] for i in items if i["surface"] == surface), None
                ),
                # Rescue requires the filter to have done something. A surface
                # that already sat near the gate and did not move (新竹科學園區
                # at 0.0 lift, 長庚醫院 at -0.03) was not saved by the model, and
                # counting it would inflate the result this experiment reports.
                "rescued": bool(
                    acc is not None
                    and len(accepted) >= args.min_accepted
                    and acc >= args.min_concentration
                    and base is not None
                    and acc > base
                ),
            }
        )

    rescued = [r for r in results if r["rescued"]]
    accepted_all = [r for rows in by_surface.values() for r in rows if r["verdict"] == "yes"]
    all_rows = [r for rows in by_surface.values() for r in rows]
    pooled_before = (
        sum(1 for r in all_rows if r["county"] in set(claimed[r["surface"]])) / len(all_rows)
        if all_rows else None
    )
    pooled_after = (
        sum(1 for r in accepted_all if r["county"] in set(claimed[r["surface"]])) / len(accepted_all)
        if accepted_all else None
    )
    report = {
        "metadata": {
            "schema": "skillweave-l5-occurrence-judgement-v1",
            "prompt_version": PROMPT_VERSION,
            "extractor_model": args.model_id,
            "feature": "text window of +-%d characters around the mention" % WINDOW,
            "why_this_differs": (
                "scripts/judge_district_collocations.py gave the model one Han "
                "character of context and separated the ambiguous band by 0.087; "
                "docs/evaluation-limits.md concluded the feature was the problem"
            ),
            "scoring": (
                "county consistency, never shown to the model: a mention can only "
                "be the place if the posting sits in the claimed county"
            ),
            "min_accepted": args.min_accepted,
            "min_concentration": args.min_concentration,
        },
        "pooled": {
            "surfaces": len(results),
            "sampled": len(all_rows),
            "accepted": len(accepted_all),
            "concentration_before": round(pooled_before, 4) if pooled_before else None,
            "concentration_after": round(pooled_after, 4) if pooled_after else None,
            "lift": round(pooled_after - pooled_before, 4) if pooled_after and pooled_before else None,
            "rescued_surfaces": len(rescued),
        },
        "surfaces": sorted(results, key=lambda r: -(r["lift"] or 0)),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["pooled"], ensure_ascii=False, indent=2))
    print(f"Wrote {args.report}")


if __name__ == "__main__":
    main()
