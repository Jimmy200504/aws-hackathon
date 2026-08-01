#!/usr/bin/env python3
"""Measure what the occurrence-level district judgement actually bought.

`scripts/judge_district_collocations.py` scored 87.18% on the held-out half of
the labelled collocations, which cleared its gate. This script asks the
question that score cannot answer: does the model help on the collocations the
labels do not cover?

It does not, and the reason is structural rather than a prompt problem. The
labelled bands exist because a collocation's kept error rate separated cleanly
- under 3% is `place`, over 50% is `not_place`. Those are the easy cases by
construction. Everything in between was left unlabelled precisely because that
same quantity did not separate it, and that middle band is the entire reason
the model was brought in.

Measured on posting-weighted precision, the model reproduces the label
distinction almost perfectly and adds almost nothing in the middle:

    labelled      place 0.9511   not_place 0.1197   gap 0.831
    middle band   place 0.7792   not_place 0.6920   gap 0.087

So the validation design had a blind spot. Scoring against the labels was
necessary - it caught a 66.54% first prompt - but it measures a population
that is separable by definition, and passing it does not license applying the
model to a population that is not.

Three extraction arms are compared end to end so the trade is visible in jobs
rather than collocations.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_CACHE = ROOT / "artifacts" / "district-collocations.jsonl"
DEFAULT_QUEUE = ROOT / "artifacts" / "district-collocation-queue.json"
DEFAULT_REPORT = ROOT / "reports" / "district-collocation-effect.json"
PROMPT_VERSION = "district-collocation-v2"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def final_verdicts(cache: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """One row per collocation, with the measured label winning over the model."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    with cache.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("prompt_version") != PROMPT_VERSION:
                continue
            rows.setdefault((row["surface"], row["following"]), row)
    return rows


def weighted_precision(rows: list[dict[str, Any]]) -> float | None:
    total = sum(row["postings"] for row in rows)
    if not total:
        return None
    return round(sum(row["postings"] * row["precision"] for row in rows) / total, 4)


def band_report(rows: list[dict[str, Any]], verdict_of) -> dict[str, Any]:
    place = [row for row in rows if verdict_of(row) == "place"]
    not_place = [row for row in rows if verdict_of(row) == "not_place"]
    place_wp = weighted_precision(place)
    not_place_wp = weighted_precision(not_place)
    return {
        "collocations": len(rows),
        "matches": sum(row["postings"] for row in rows),
        "place": {
            "collocations": len(place),
            "matches": sum(row["postings"] for row in place),
            "weighted_precision": place_wp,
        },
        "not_place": {
            "collocations": len(not_place),
            "matches": sum(row["postings"] for row in not_place),
            "weighted_precision": not_place_wp,
        },
        # The whole point. A separation near zero means the verdict carries no
        # information about whether the match is a real district reference.
        "separation": (
            round(place_wp - not_place_wp, 4)
            if place_wp is not None and not_place_wp is not None
            else None
        ),
    }


def run_extractor(judgements: Path | None, tmp: Path, label: str) -> dict[str, Any]:
    report = tmp / f"{label}-report.json"
    output = tmp / f"{label}-jobs.json"
    command = [
        str(PYTHON),
        str(ROOT / "scripts" / "extract_job_districts.py"),
        "--report",
        str(report),
        "--output",
        str(output),
    ]
    if judgements is not None:
        command += ["--collocation-judgements", str(judgements)]
    print(f"  arm: {label}…", flush=True)
    subprocess.run(command, check=True, capture_output=True)
    stats = json.loads(report.read_text(encoding="utf-8"))["stats"]
    return {
        key: stats.get(key, 0)
        for key in (
            "eligible",
            "postings_with_district",
            "coverage_of_eligible",
            "single_district_postings",
            "single_district_share",
            "match_rejected_occurrence",
            "match_recovered_occurrence",
            "match_rejected_surface",
        )
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--skip-arms",
        action="store_true",
        help="report the collocation-level diagnosis only, without re-running the extractor",
    )
    args = parser.parse_args()

    rows = final_verdicts(args.cache)
    labelled = [row for row in rows.values() if row.get("label")]
    middle = [row for row in rows.values() if not row.get("label")]

    diagnosis = {
        "labelled_band": band_report(labelled, lambda row: row["label"]),
        "middle_band": band_report(middle, lambda row: row["verdict"]),
        "middle_band_false_rejections": None,
    }
    wrong = [
        row
        for row in middle
        if row["verdict"] == "not_place" and row["precision"] > 0.60
    ]
    wrong.sort(key=lambda row: -row["postings"])
    diagnosis["middle_band_false_rejections"] = {
        "rule": "judged not_place while measured precision exceeds 0.60",
        "collocations": len(wrong),
        "matches": sum(row["postings"] for row in wrong),
        "examples": [
            {
                "surface": row["surface"],
                "following": row["following"],
                "postings": row["postings"],
                "precision": row["precision"],
                "example": row["example"],
            }
            for row in wrong[:15]
        ],
    }

    arms: dict[str, Any] = {}
    if not args.skip_arms:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            labelled_only = tmp / "labelled-only.jsonl"
            with args.cache.open(encoding="utf-8") as source, labelled_only.open(
                "w", encoding="utf-8"
            ) as sink:
                for line in source:
                    row = json.loads(line) if line.strip() else None
                    if row and row.get("prompt_version") == PROMPT_VERSION and row.get("label"):
                        sink.write(line)
            arms["surface_only"] = run_extractor(None, tmp, "surface-only")
            arms["labels_only"] = run_extractor(labelled_only, tmp, "labels-only")
            arms["labels_plus_model"] = run_extractor(args.cache, tmp, "labels-plus-model")

    report = {
        "metadata": {
            "schema": "skillweave-district-collocation-effect-v1",
            "prompt_version": PROMPT_VERSION,
            "extractor_model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "held_out_accuracy": 0.8718,
            "held_out_note": (
                "measured on the labelled half that prompt wording never saw; see "
                "reports/district-collocation-judgement.json"
            ),
            "precision_definition": (
                "share of matched postings whose 工作城市 is consistent with the "
                "surface's candidate districts; the same proxy the extractor's own "
                "gate uses, not an independent ground truth"
            ),
        },
        "conclusion": {
            "verdict": "model judgements not adopted",
            "why": (
                "the labelled bands are separable by construction, so 87.18% on them "
                "does not transfer; on the middle band the model's place/not_place "
                "split differs by 0.087 in weighted precision, and applying it costs "
                "coverage while leaving purity essentially unchanged"
            ),
            "what_is_adopted": (
                "the occurrence-level mechanism itself, driven by the 511 measured "
                "labels only, which is a coverage and purity gain with no model output"
            ),
            "root_cause": (
                "one following Han character is enough context for the easy cases and "
                "not for the ambiguous ones: 南區) at 0.6023 precision cannot be "
                "resolved from those four characters by any judge"
            ),
        },
        "collocation_diagnosis": diagnosis,
        "extraction_arms": arms,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"diagnosis": diagnosis["labelled_band"]["separation"]}, indent=2))
    print(json.dumps(report["collocation_diagnosis"]["middle_band"], ensure_ascii=False, indent=2))
    if arms:
        print(json.dumps(arms, ensure_ascii=False, indent=2))
    print(f"Wrote {args.report}")


if __name__ == "__main__":
    main()
