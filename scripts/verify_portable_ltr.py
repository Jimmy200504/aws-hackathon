#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tree_ranker import PortableTreeRanker


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify portable tree inference against native XGBoost"
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=ROOT / "artifacts" / "models" / "ltr-quality-final.ubj",
    )
    parser.add_argument(
        "--portable",
        type=Path,
        default=(
            ROOT
            / "artifacts"
            / "models"
            / "ltr-quality-final.trees.json"
        ),
    )
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports" / "portable-ltr-parity.json",
    )
    args = parser.parse_args()
    try:
        import numpy as np
        import xgboost as xgb
    except ImportError as exc:
        raise SystemExit("Run with the pinned LTR environment") from exc

    portable = PortableTreeRanker(args.portable)
    rows = [
        json.loads(line)["features"]
        for line in args.pairs.read_text(encoding="utf-8").splitlines()
        if line
    ]
    matrix = np.asarray(
        [
            [features.get(name, 0.0) for name in portable.features]
            for features in rows
        ],
        dtype=np.float32,
    )
    native = xgb.XGBRanker()
    native.load_model(args.model)
    native_scores = native.predict(matrix)
    portable_scores = np.asarray(
        [
            portable.predict(features, include_graph=True)
            for features in rows
        ]
    )
    native_centered = native_scores - native_scores[0]
    portable_centered = portable_scores - portable_scores[0]
    max_error = float(
        np.max(np.abs(native_centered - portable_centered))
    )
    report = {
        "metadata": {
            "schema": "skillweave-portable-ltr-parity-v1",
            "rows": len(rows),
            "native_model_sha256": sha256(args.model),
            "portable_model_sha256": sha256(args.portable),
        },
        "max_centered_absolute_error": max_error,
        "tolerance": 1e-6,
        "passed": max_error <= 1e-6,
        "interpretation": (
            "A constant base score is irrelevant to ranking; centered scores "
            "must agree within float32 tolerance."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
