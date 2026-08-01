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
PROMPT_VERSION = "district-collocation-v1"

SYSTEM_PROMPT = """\
你要判斷台灣職缺文字裡的地名字串，是否真的在指「該職缺所在的行政區」。

每一筆給你三個欄位：
  surface    比對到的行政區名（可能省略「區/鄉/鎮/市」後綴）
  following  這個字串在職缺原文中緊接著的下一個字（可能為空）
  example    職缺原文片段

判斷標準只有一個：一則含有「surface + following」這個字串的職缺，
是否真的位於 surface 所指的那個行政區。

判為 place 的情形：
  - surface 後面接的是該行政區內的路名、地標、站名或門市名
    例：北區和緯路（台南市北區）、北區忠明路（台中市北區）
  - surface 本身就是地址的行政區欄位

判為 not_place 的情形：
  - surface 是更大範圍的業務區域，不是行政區
    例：北區業務、北區門市（指北台灣）、北區各大百貨
  - surface 是路名或建物名的一部分，而該路橫跨多個行政區
    例：中山北路、中正路、中山二路
  - surface 是一般詞彙或機構名，不是地名
    例：中正紀念堂以外的中正、三重防護、酸鹼中和、淡水魚、新社區（新建社區）
  - surface 屬於另一個縣市的同名行政區，會把職缺標到錯的地方

注意台灣有多個縣市共用同一個區名（北區在台中、台南、新竹都有；東區有四個），
但你不需要指出是哪一個縣市，只需要判斷它是否被當作行政區使用。

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


def load_cache(path: Path, mode: str) -> dict[str, dict[str, Any]]:
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
            # be counted as production output, or the held-out set stops being
            # held out.
            if row.get("key") and row.get("mode") == mode:
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
    report: dict[str, Any] = {
        "metadata": {
            "schema": "skillweave-district-collocation-judgement-v1",
            "mode": args.mode,
            "source_queue": str(args.queue.relative_to(ROOT)).replace("\\", "/"),
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
