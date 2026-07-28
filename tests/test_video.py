import json
import unittest
from pathlib import Path

from scripts.render_demo_video import timestamp


ROOT = Path(__file__).resolve().parents[1]


class DemoVideoSourceTests(unittest.TestCase):
    def test_scene_contract_is_exactly_five_minutes(self) -> None:
        scenes = json.loads(
            (ROOT / "video" / "scenes.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(scenes), 8)
        self.assertEqual(sum(scene["duration"] for scene in scenes), 300)
        self.assertEqual(
            [scene["slide"] for scene in scenes],
            list(range(1, 9)),
        )
        self.assertTrue(
            all(scene["caption"] and scene["narration"] for scene in scenes)
        )

    def test_srt_timestamp_rounding(self) -> None:
        self.assertEqual(timestamp(0), "00:00:00,000")
        self.assertEqual(timestamp(299.9), "00:04:59,900")
        self.assertEqual(timestamp(300), "00:05:00,000")


if __name__ == "__main__":
    unittest.main()
