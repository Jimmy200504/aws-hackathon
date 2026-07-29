import json
import tempfile
import unittest
from pathlib import Path

from app.tree_ranker import PortableTreeRanker


class PortableTreeRankerTests(unittest.TestCase):
    def test_predicts_tree_and_zeros_graph_features_for_baseline(self) -> None:
        artifact = {
            "schema": "skillweave-portable-xgboost-v1",
            "metadata": {"source_model": "fixture"},
            "features": ["lexical", "graph_signal"],
            "baseline_features": ["lexical"],
            "trees": [
                {
                    "nodeid": 0,
                    "split": "f1",
                    "split_condition": 0.5,
                    "yes": 1,
                    "no": 2,
                    "missing": 1,
                    "children": [
                        {"nodeid": 1, "leaf": -0.2},
                        {"nodeid": 2, "leaf": 0.3},
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            model = PortableTreeRanker(path)
            features = {"lexical": 1.0, "graph_signal": 1.0}
            self.assertAlmostEqual(
                model.predict(features, include_graph=True), 0.3
            )
            self.assertAlmostEqual(
                model.predict(features, include_graph=False), -0.2
            )


if __name__ == "__main__":
    unittest.main()
