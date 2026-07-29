#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export XGBoost ranking trees for dependency-free Lambda inference"
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=ROOT / "artifacts" / "models" / "ltr-quality-final.ubj",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            ROOT
            / "artifacts"
            / "models"
            / "ltr-quality-final.trees.json"
        ),
    )
    args = parser.parse_args()
    try:
        import xgboost as xgb
    except ImportError as exc:
        raise SystemExit("Run with the pinned LTR environment") from exc

    from pipeline.evaluate_ltr import ABLATION_BASE_FEATURES

    manifest_path = args.model.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model = xgb.XGBRanker()
    model.load_model(args.model)
    booster = model.get_booster()
    trees = [
        json.loads(tree)
        for tree in booster.get_dump(dump_format="json")
    ]
    artifact = {
        "schema": "skillweave-portable-xgboost-v1",
        "metadata": {
            "source_model": args.model.name,
            "objective": manifest["objective"],
            "feature_set": manifest["feature_set"],
            "tree_count": len(trees),
            "random_seed": manifest["random_seed"],
            "xgboost_version": manifest["xgboost_version"],
            "inference": "sum exported tree leaves; base score omitted for ranking",
        },
        "features": manifest["features"],
        "baseline_features": sorted(ABLATION_BASE_FEATURES),
        "trees": trees,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        f"Wrote {args.output} ({len(trees)} trees, "
        f"{args.output.stat().st_size / 1024:.1f} KiB)"
    )


if __name__ == "__main__":
    main()
