#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import SkillWeaveRanker


INDEX = ROOT / "artifacts" / "benchmark-index.json"
QRELS = ROOT / "artifacts" / "temporal-eval.json"
OUTPUT = ROOT / "artifacts" / "ltr"


def load_query_normalization(path: Path | None) -> dict[str, str]:
    """Map raw query -> Bedrock-normalized query for offline replay.

    Degraded rows are skipped: replaying a deterministic fallback as if it were
    a Bedrock rewrite would attribute a null result to the LLM.
    """
    if path is None:
        return {}
    mapping: dict[str, str] = {}
    skipped = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("degraded") or row.get("source") != "amazon_bedrock":
                skipped += 1
                continue
            mapping[row["query"]] = row["normalized"]
    print(
        f"Loaded {len(mapping):,} Bedrock query rewrites "
        f"({skipped:,} degraded rows skipped)",
        flush=True,
    )
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialize grouped LTR feature rows")
    parser.add_argument("--index", type=Path, default=INDEX)
    parser.add_argument("--qrels", type=Path, default=QRELS)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument(
        "--query-normalization",
        type=Path,
        default=None,
        help="JSONL cache from scripts/normalize_eval_queries.py",
    )
    args = parser.parse_args()
    normalization = load_query_normalization(args.query_normalization)
    ranker = SkillWeaveRanker(args.index, graph_novelty_threshold=1.0)
    evaluation = json.loads(args.qrels.read_text(encoding="utf-8"))
    job_to_index = {job["id"]: index for index, job in enumerate(ranker.jobs)}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "skillweave-ltr-pairs-v1",
        "index_version": ranker.metadata["index_version"],
        "qrels_schema": evaluation["metadata"]["schema"],
        "random_seed": 1111,
        "query_normalization": {
            "source": str(args.query_normalization) if args.query_normalization else None,
            "rewrites_available": len(normalization),
        },
        "splits": {},
    }
    for split in ["train", "validation", "test"]:
        cases = evaluation["splits"].get(split, [])
        output_path = args.output_dir / f"{split}.jsonl"
        rows_written = groups_written = llm_widened_groups = 0
        with output_path.open("w", encoding="utf-8") as output:
            for case in cases:
                available = [
                    job_id for job_id in case["candidates"] if job_id in job_to_index
                ]
                if len(available) < 2:
                    continue
                if max((case["qrels"].get(job_id, 0) for job_id in available), default=0) <= 0:
                    continue
                intent = ranker.parse_intent(
                    case["query"],
                    case["location_code"],
                    case["duty_code"],
                    normalized_query=normalization.get(case["query"]),
                )
                if intent.llm_only_skills:
                    llm_widened_groups += 1
                group_rows = []
                for exposure_rank, job_id in enumerate(available, 1):
                    _, features, _, _ = ranker._score(
                        job_to_index[job_id],
                        intent,
                        include_graph=True,
                        behavior_snapshot_day=(
                            case.get("day") if split == "train" else None
                        ),
                    )
                    # Production OpenSearch contributes a retrieval score/rank
                    # before LTR. The organizer fixture exposes only the
                    # original candidate order, so preserve rank-derived priors
                    # under an explicit retrieval feature family.
                    features.update(
                        {
                            "retrieval_rank": float(exposure_rank),
                            "retrieval_reciprocal_rank": 1.0 / exposure_rank,
                            "retrieval_log_rank": math.log1p(exposure_rank),
                            "retrieval_top1": float(exposure_rank == 1),
                            "retrieval_top3": float(exposure_rank <= 3),
                            "retrieval_top10": float(exposure_rank <= 10),
                        }
                    )
                    group_rows.append(
                        {
                            "query_id": case["query_id"],
                            "job_id": job_id,
                            "label": int(case["qrels"].get(job_id, 0)),
                            "exposure_rank": exposure_rank,
                            "features": features,
                        }
                    )
                for row in group_rows:
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                rows_written += len(group_rows)
                groups_written += 1
        manifest["splits"][split] = {
            "groups": groups_written,
            "rows": rows_written,
            # How many groups gained at least one canonical node that the raw
            # query alone could not resolve. This is the only channel through
            # which query normalization can move a metric.
            "groups_widened_by_llm": llm_widened_groups,
            "path": str(output_path),
        }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
