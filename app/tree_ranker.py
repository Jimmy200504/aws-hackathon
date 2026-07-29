from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any


class PortableTreeRanker:
    """Small dependency-free predictor for exported XGBoost ranking trees."""

    def __init__(self, artifact_path: str | Path):
        self.artifact_path = Path(artifact_path)
        artifact = json.loads(self.artifact_path.read_text(encoding="utf-8"))
        if artifact.get("schema") != "skillweave-portable-xgboost-v1":
            raise ValueError("unsupported portable ranking model schema")
        self.features = tuple(artifact["features"])
        self.baseline_features = frozenset(artifact["baseline_features"])
        self.trees: tuple[dict[str, Any], ...] = tuple(artifact["trees"])
        self.metadata = artifact["metadata"]

    @staticmethod
    def _float32(value: float) -> float:
        return struct.unpack("!f", struct.pack("!f", float(value)))[0]

    @staticmethod
    def _tree_score(node: dict[str, Any], values: tuple[float, ...]) -> float:
        while "leaf" not in node:
            feature_index = int(str(node["split"]).removeprefix("f"))
            value = values[feature_index]
            threshold = PortableTreeRanker._float32(
                node["split_condition"]
            )
            target = node["yes"] if value < threshold else node["no"]
            node = next(
                child for child in node["children"] if child["nodeid"] == target
            )
        return float(node["leaf"])

    def predict(
        self,
        features: dict[str, float],
        *,
        include_graph: bool,
    ) -> float:
        allowed = None if include_graph else self.baseline_features
        values = tuple(
            self._float32(features.get(name, 0.0))
            if allowed is None or name in allowed
            else 0.0
            for name in self.features
        )
        return sum(self._tree_score(tree, values) for tree in self.trees)
