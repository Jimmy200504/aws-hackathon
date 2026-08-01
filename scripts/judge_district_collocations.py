#!/usr/bin/env python3
"""Resolve occurrence-level district ambiguity with Bedrock, measured first.

`scripts/extract_job_districts.py` decides whether to publish a surface by its
kept error rate across the whole corpus. That is a surface-level decision, and
some surfaces are not uniform enough for any surface-level answer to be right.
北區 is the clearest case:

    北區業  n=408  p= 4.66%   北區業務部專員      the sales region, not a district
    北區和  n=159  p=80.50%   北區和緯路四段      台南市北區, a district
    北區忠  n=146  p=72.60%   北區忠明路          台中市北區, a district

Accepting 北區 keeps 北區業務. Rejecting it throws away 和緯/忠明/三民. The
split is semantic, not statistical, which is what a language model is for.

Two modes, and the first is not optional:

    --mode validate   run on the 511 collocations that already carry a label,
                      derived from measured error rates rather than opinion,
                      and report accuracy before anything is trusted
    --mode apply      run on the 2,558 that need judgement

The labelled bands are deliberately conservative - `place` needs a kept error
rate under 3%, `not_place` over 50% - and the middle is left unlabelled. A
single 10% threshold would have labelled 北區和緯 (12%) as a negative even
though it is a real street in a real district, and the model would then be
penalised for answering correctly.

Results are an append-only JSONL cache keyed by surface and following
character, so an expired Workshop Studio credential can be replaced and the run
resumed without paying for work that already succeeded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_QUEUE = ROOT / "reports" / "job-district-extraction.json"
DEFAULT_CACHE = ROOT / "artifacts" / "district-collocations.jsonl"
DEFAULT_REPORT = ROOT / "reports" / "district-collocation-judgement.json"
DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# The event rule caps Bedrock at one request per second and allows only
# us-east-1 / us-west-2. A small margin absorbs clock granularity.
MIN_INTERVAL_SECONDS = 1.05
ALLOWED_REGIONS = {"us-east-1", "us-west-2"}
BATCH_SIZE = 25
PROMPT_VERSION = "district-collocation-v2"

# v1 asked whether the surface "is being used as a district", which is a
# question about grammar. The labels answer a question about location: does a
# posting carrying this string actually sit in that district. The two come
# apart on branch and plant names - 中壢店 is not grammatically an address, but
# a 中壢店 is almost always in 中壢區 - and v1 scored 66.54% by rejecting them.
# v2 asks the location question directly and supplies the one piece of domain
# knowledge that separates the two error classes: whether the surface is a
# distinctive place name or one of the auspicious words used as a street name
# in nearly every Taiwanese city.
SYSTEM_PROMPT = """\
你要判斷：職缺文字裡出現這個地名字串時，能不能據此推斷「這則職缺就位於該地名所指的行政區」。

每一筆給你三個欄位：
  surface    比對到的行政區名（可能省略「區/鄉/鎮/市」後綴）
  following  這個字串在職缺原文中緊接著的下一個字（可能為空）
  example    職缺原文片段

問題不是「這個字串在文法上是不是行政區」，而是
「看到這個字串，能不能推斷職缺的所在地」。兩者不一樣，請以後者為準。

關鍵區分：surface 是「專屬地名」還是「全台通用詞」。

A. 專屬地名 —— 台灣只有這一處叫這個名字
   中壢 桃園 板橋 樹林 新莊 士林 內湖 南港 淡水 鶯歌 平鎮 八德 龜山 大雅 清水 新市 …
   這類 surface **通常判 place**，包括後面接店名、廠名、校名、路名的情形：
     中壢店 → 分店以所在地命名，該店就在中壢區
     桃園廠 → 廠區以所在地命名
     樹林中山、新莊路222號、板橋四川店 → 都在該行政區內
   例外：明確指向另一個地方時判 not_place
     例如「桃園蘆竹」是桃園市蘆竹區，主體是蘆竹不是桃園區

B. 全台通用詞 —— 幾乎每個縣市都有同名的路、站或店
   中正 中山 復興 和平 三民 大同 民生 民權 中華 光復 成功 建國 忠孝 仁愛 四維 八德 文化 中興
   這類字同時是某個行政區的名字，也是全台最常見的路名。
   **後面接路名、街、巷、段、號、站、店、門市時通常判 not_place**，
   因為那多半是別的縣市的同名道路，推斷不出職缺在哪一區：
     中正路 中山北路 復興站 三民路 和平東路 大同店 → not_place
   只有明確寫出行政區欄位（如「中正區」「三民區」）才判 place。

其他 not_place 的情形：
  - 業務區域，不是行政區：北區業務、南區門市（指北台灣、南台灣）、北區各大百貨
  - 一般詞彙不是地名：三重防護、酸鹼中和、淡水魚、新社區（新建社區）、大樹藥局
  - 機構名要看它實際登記的行政區，可能與名稱裡的地名不同；
    醫院、大學、園區常以鄰近地名命名，實際卻在隔壁行政區甚至隔壁縣市

注意台灣有多個縣市共用同一個區名（北區在台中、台南、新竹都有；東區有四個）。
你不需要指出是哪一個縣市，只需要判斷能不能推斷出所在行政區。

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
                    "verdict": {"type": "string", "enum": ["place", "not_place"]},
                },
                "required": ["id", "verdict"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["judgements"],
    "additionalProperties": False,
}


def split_of(key: str) -> str:
    """Deterministic dev/holdout half for a collocation key.

    Prompt wording is iterated against `dev` only. Without this the 511
    labelled collocations would be both the thing the prompt is tuned on and
    the thing it is scored on, and the reported accuracy would be a description
    of the tuning rather than a measurement. `docs/evaluation-limits.md`
    records the same failure being caught once already.
    """
    return "dev" if int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % 2 == 0 else "holdout"


def _repo_relative(path: Path) -> str:
    """Repo-relative POSIX path, falling back to the absolute one."""
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def load_items(queue_path: Path, mode: str) -> list[dict[str, Any]]:
    """Flatten the review queue into per-collocation judgement items."""
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = []
    for entry in payload["occurrence_review_queue"]:
        for collocation in entry["collocations"]:
            label = collocation.get("label")
            needs = collocation.get("needs_semantic_judgement")
            if mode == "validate" and not label:
                continue
            if mode == "apply" and not needs:
                continue
            items.append(
                {
                    "key": f"{entry['surface']}\t{collocation['following']}",
                    "surface": entry["surface"],
                    "layer": entry["layer"],
                    "counties": entry["counties"],
                    "following": collocation["following"],
                    "example": collocation["example"],
                    "postings": collocation["postings"],
                    "precision": collocation["precision"],
                    "label": label,
                }
            )
    # Largest first: a wrong call on a high-posting collocation costs the most.
    items.sort(key=lambda item: (-item["postings"], item["key"]))
    return items


def load_cache(
    path: Path, mode: str, prompt_version: str = PROMPT_VERSION
) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    cache: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Validate and apply runs are kept apart: a validate row must never
            # be counted as production output. Prompt versions are kept apart
            # too, so a reworded prompt is re-measured rather than silently
            # inheriting the previous prompt's answers.
            if (
                row.get("key")
                and row.get("mode") == mode
                and row.get("prompt_version") == prompt_version
            ):
                cache[row["key"]] = row
    return cache


def render_batch(batch: list[dict[str, Any]]) -> str:
    lines = []
    for index, item in enumerate(batch):
        lines.append(
            json.dumps(
                {
                    "id": index,
                    "surface": item["surface"],
                    "following": item["following"],
                    "example": item["example"],
                },
                ensure_ascii=False,
            )
        )
    return "判斷以下 %d 筆：\n%s" % (len(batch), "\n".join(lines))


def bedrock_client(region: str):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            connect_timeout=10.0,
            read_timeout=120.0,
            retries={"max_attempts": 5, "mode": "adaptive"},
        ),
    )


def judge_batch(client, model_id: str, batch: list[dict[str, Any]]) -> dict[int, str]:
    response = client.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": render_batch(batch)}]}],
        outputConfig={
            "textFormat": {
                "type": "json_schema",
                "structure": {
                    "jsonSchema": {
                        "schema": json.dumps(
                            OUTPUT_SCHEMA, ensure_ascii=False, separators=(",", ":")
                        ),
                        "name": "district_collocation_judgement",
                        "description": "Per-collocation place judgement",
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


def score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Confusion matrix against the held-out labels."""
    matrix = {
        "place_place": 0,
        "place_not_place": 0,
        "not_place_place": 0,
        "not_place_not_place": 0,
    }
    for row in rows:
        if not row.get("label") or not row.get("verdict"):
            continue
        matrix[f"{row['label']}_{row['verdict']}"] += 1
    total = sum(matrix.values())
    tp = matrix["place_place"]
    fp = matrix["not_place_place"]
    fn = matrix["place_not_place"]
    tn = matrix["not_place_not_place"]
    return {
        "scored": total,
        "accuracy": round((tp + tn) / total, 4) if total else None,
        "place_precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "place_recall": round(tp / (tp + fn), 4) if tp + fn else None,
        "not_place_precision": round(tn / (tn + fn), 4) if tn + fn else None,
        "not_place_recall": round(tn / (tn + fp), 4) if tn + fp else None,
        "confusion": matrix,
        "postings_weighted_accuracy": (
            round(
                sum(
                    row["postings"]
                    for row in rows
                    if row.get("label") and row.get("verdict") == row["label"]
                )
                / max(1, sum(row["postings"] for row in rows if row.get("label"))),
                4,
            )
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--mode", choices=("validate", "apply"), default="validate")
    parser.add_argument(
        "--split",
        choices=("dev", "holdout", "all"),
        default="all",
        help="validate mode only: iterate prompt wording against dev, and touch "
        "holdout once when the wording is final",
    )
    parser.add_argument("--model-id", default=DEFAULT_MODEL)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--limit", type=int, default=0, help="stop after N items")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the first rendered batch and exit without calling Bedrock",
    )
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=0.85,
        help="validate mode exits non-zero below this, so apply is not run on a model that has not earned it",
    )
    args = parser.parse_args()

    if args.region not in ALLOWED_REGIONS:
        raise SystemExit(
            f"--region {args.region} is outside the event's allowed regions "
            f"{sorted(ALLOWED_REGIONS)}"
        )

    items = load_items(args.queue, args.mode)
    if args.mode == "validate" and args.split != "all":
        items = [item for item in items if split_of(item["key"]) == args.split]
    cache = load_cache(args.cache, args.mode)
    todo = [item for item in items if item["key"] not in cache]
    if args.limit > 0:
        todo = todo[: args.limit]
    batches = [
        todo[index : index + args.batch_size]
        for index in range(0, len(todo), args.batch_size)
    ]

    header = {
        "mode": args.mode,
        "split": args.split,
        "items": len(items),
        "already_cached": len(items) - len([i for i in items if i["key"] not in cache]),
        "to_call": len(todo),
        "batches": len(batches),
        "batch_size": args.batch_size,
        "model_id": args.model_id,
        "region": args.region,
        "min_interval_seconds": MIN_INTERVAL_SECONDS,
        "estimated_minutes": round(len(batches) * MIN_INTERVAL_SECONDS / 60, 1),
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:16],
    }
    print(json.dumps(header, ensure_ascii=False, indent=2), flush=True)

    if args.dry_run:
        if batches:
            print("\n--- system prompt ---\n" + SYSTEM_PROMPT)
            print("\n--- first rendered batch ---\n" + render_batch(batches[0]))
        print("\ndry run: no Bedrock call made, no cache written")
        return

    if batches:
        client = bedrock_client(args.region)
        args.cache.parent.mkdir(parents=True, exist_ok=True)
        last_call = 0.0
        started = time.monotonic()
        failed = 0
        with args.cache.open("a", encoding="utf-8") as handle:
            for number, batch in enumerate(batches, 1):
                wait = MIN_INTERVAL_SECONDS - (time.monotonic() - last_call)
                if wait > 0:
                    time.sleep(wait)
                last_call = time.monotonic()
                try:
                    verdicts = judge_batch(client, args.model_id, batch)
                except Exception as exc:
                    # One bad batch must not lose the batches already paid for.
                    failed += 1
                    print(
                        f"  batch {number}/{len(batches)} failed: {type(exc).__name__}",
                        flush=True,
                    )
                    continue
                for index, item in enumerate(batch):
                    verdict = verdicts.get(index)
                    if verdict is None:
                        failed += 1
                        continue
                    handle.write(
                        json.dumps(
                            {
                                **{k: v for k, v in item.items() if k != "key"},
                                "key": item["key"],
                                "verdict": verdict,
                                "mode": args.mode,
                                "extractor_model": args.model_id,
                                "prompt_version": PROMPT_VERSION,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                handle.flush()
                if number % 10 == 0 or number == len(batches):
                    print(
                        "  batch %d/%d  %.1f min elapsed"
                        % (number, len(batches), (time.monotonic() - started) / 60),
                        flush=True,
                    )
        observed_rps = len(batches) / max(0.001, time.monotonic() - started)
        if observed_rps > 1.0:
            raise SystemExit("rate ceiling violated: observed %.3f RPS" % observed_rps)
        header["observed_rps"] = round(observed_rps, 4)
        header["unanswered_items"] = failed

    rows = list(load_cache(args.cache, args.mode).values())
    if args.mode == "validate" and args.split != "all":
        rows = [row for row in rows if split_of(row["key"]) == args.split]
    report: dict[str, Any] = {
        "metadata": {
            "schema": "skillweave-district-collocation-judgement-v1",
            "mode": args.mode,
            "split": args.split,
            "source_queue": _repo_relative(args.queue),
            "extractor_model": args.model_id,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": header["prompt_sha256"],
            "batch_size": args.batch_size,
            "temperature": 0,
            **{k: v for k, v in header.items() if k in ("observed_rps", "unanswered_items")},
        },
        "counts": {
            "items_in_mode": len(items),
            "judged": len(rows),
            "place": sum(1 for row in rows if row.get("verdict") == "place"),
            "not_place": sum(1 for row in rows if row.get("verdict") == "not_place"),
        },
    }
    if args.mode == "validate":
        report["scores"] = score(rows)
        report["disagreements"] = sorted(
            (
                {
                    "surface": row["surface"],
                    "following": row["following"],
                    "example": row["example"],
                    "postings": row["postings"],
                    "precision": row["precision"],
                    "label": row["label"],
                    "verdict": row["verdict"],
                }
                for row in rows
                if row.get("label") and row.get("verdict") != row["label"]
            ),
            key=lambda row: -row["postings"],
        )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report.get("scores", report["counts"]), ensure_ascii=False, indent=2))
    print(f"Wrote {args.report}")

    if args.mode == "validate" and report["scores"]["scored"]:
        accuracy = report["scores"]["accuracy"]
        if accuracy < args.min_accuracy:
            raise SystemExit(
                "accuracy %.4f is below --min-accuracy %.2f; applying this model "
                "to the unlabelled collocations would not be justified"
                % (accuracy, args.min_accuracy)
            )


if __name__ == "__main__":
    main()
