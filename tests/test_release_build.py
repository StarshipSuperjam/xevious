from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import scratch_project as scratch  # noqa: E402

RELEASE_ARCHIVE = ROOT / "release" / "Xevious.sb3"


class ReleaseBuildTests(unittest.TestCase):
    def test_committed_release_matches_a_fresh_deterministic_build(self) -> None:
        # The committed release file must be exactly what the source builds, so it
        # cannot go stale when src/ changes. Rebuild it with:
        #   python3 tools/playtest_package.py --output release/Xevious.sb3
        self.assertTrue(RELEASE_ARCHIVE.is_file(), f"missing {RELEASE_ARCHIVE}")
        with tempfile.TemporaryDirectory() as temp:
            built = Path(temp) / "Xevious.sb3"
            scratch.build_project(output=built)
            self.assertEqual(
                RELEASE_ARCHIVE.read_bytes(),
                built.read_bytes(),
                "release/Xevious.sb3 differs from a fresh build of src/xevious",
            )


if __name__ == "__main__":
    unittest.main()
