#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ranker import SkillWeaveRanker
from app.query_normalizer import BedrockQueryNormalizer, QueryIntentVocabulary


def build_offline_normalizer() -> tuple[BedrockQueryNormalizer | None, int]:
    """Return a cache-only normalizer, or None when no intents are available.

    Training features must match what the API computes, and the API runs every
    query through the normalizer. This deliberately passes `model_id=None` so
    the run stays offline and reproducible: a cached intent is used when one
    exists and the deterministic reading is used otherwise, but no training run
    can silently depend on a live Bedrock call.
    """
    try:
        vocabulary = QueryIntentVocabulary.load()
    except Exception:
        return None, 0
    normalizer = BedrockQueryNormalizer(None, vocabulary=vocabulary)
    return normalizer, normalizer.load_intents()


INDEX = ROOT / "artifacts" / "benchmark-index.json"
QRELS = ROOT / "artifacts" / "temporal-eval.json"
OUTPUT = ROOT / "artifacts" / "ltr"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialize grouped LTR feature rows")
    parser.add_argument("--index", type=Path, default=INDEX)
    parser.add_argument("--qrels", type=Path, default=QRELS)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    ranker = SkillWeaveRanker(args.index, graph_novelty_threshold=1.0)
    normalizer, preloaded_intents = build_offline_normalizer()
    print(
        f"Query normalization: {preloaded_intents:,} precomputed intents"
        if normalizer is not None
        else "Query normalization: unavailable, features match the pre-normalizer contract"
    )
    evaluation = json.loads(args.qrels.read_text(encoding="utf-8"))
    job_to_index = {job["id"]: index for index, job in enumerate(ranker.jobs)}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "skillweave-ltr-pairs-v1",
        # Recorded so a model can never be compared against pairs built under a
        # different normalization contract without it being visible.
        "query_normalization": {
            "applied": normalizer is not None,
            "precomputed_intents": preloaded_intents,
        },
        "index_version": ranker.metadata["index_version"],
        "index_sha256": sha256_file(args.index),
        "qrels_schema": evaluation["metadata"]["schema"],
        "qrels_sha256": sha256_file(args.qrels),
        "graph_version": ranker.metadata.get("graph_version"),
        "graph_manifest_hash": ranker.metadata.get("graph_manifest_hash"),
        "random_seed": 1111,
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
                normalization = (
                    normalizer.normalize(case["query"])
                    if normalizer is not None
                    else None
                )
                intent = ranker.parse_intent(
                    case["query"],
                    case["location_code"],
                    case["duty_code"],
                    normalized_query=(
                        normalization.query if normalization is not None else None
                    ),
                    structured_intent=(
                        normalization.intent if normalization is not None else None
                    ),
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
            "sha256": sha256_file(output_path),
        }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
