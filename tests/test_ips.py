from __future__ import annotations

import unittest

from pipeline.ips import estimate_rank_propensity, ips_weight


class IPSTests(unittest.TestCase):
    def test_propensity_is_monotonic_and_clipped(self) -> None:
        impressions = []
        for rank, clicks in [(1, 50), (2, 30), (3, 10)]:
            impressions.extend((rank, int(i < clicks)) for i in range(100))
        propensity = estimate_rank_propensity(impressions, max_rank=3)
        self.assertEqual(propensity[1], 1.0)
        self.assertGreaterEqual(propensity[1], propensity[2])
        self.assertGreaterEqual(propensity[2], propensity[3])
        self.assertLessEqual(ips_weight(3, propensity, clip=5), 5)


if __name__ == "__main__":
    unittest.main()
