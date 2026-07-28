import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.external_release_preflight import (
    sha256_path,
    tracked_sensitive_paths,
)


class ExternalReleasePreflightTests(unittest.TestCase):
    def test_hashes_large_files_in_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.bin"
            content = b"skillweave" * 200_000
            path.write_bytes(content)
            self.assertEqual(
                sha256_path(path),
                hashlib.sha256(content).hexdigest(),
            )

    def test_detects_only_forbidden_tracked_release_material(self) -> None:
        self.assertEqual(
            tracked_sensitive_paths(
                [
                    "README.md",
                    "data/dataset/README.md",
                    "data/dataset/jobs.csv",
                    "docs/brief.PDF",
                    "config/allowed.csv",
                ]
            ),
            ["data/dataset/jobs.csv", "docs/brief.PDF"],
        )


if __name__ == "__main__":
    unittest.main()
