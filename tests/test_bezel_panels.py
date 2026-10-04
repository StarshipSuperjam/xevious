from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import bezel_panels as bp  # noqa: E402
import sprite_extractor as se  # noqa: E402


class BezelPanelsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest, _ = bp.load_manifest()
        cls.output = bp.render_frame(cls.manifest)
        cls.image = se.decode_png(cls.output.png, "bezel frame")

    def test_manifest_and_committed_outputs_are_current(self) -> None:
        self.assertEqual(1, bp.check_repository())

    def test_rendering_is_byte_deterministic(self) -> None:
        again = bp.render_frame(self.manifest)
        self.assertEqual((self.output.filename, self.output.png), (again.filename, again.png))

    def test_frame_is_opaque_panels_around_a_transparent_window(self) -> None:
        # PRES-01 (record 054): 960x720 at bitmap resolution 2 is the whole 480x360 stage; the 560-px
        # window is the 280-unit arcade screen, flanked by two 200-px (100-unit) panels.
        image = self.image
        self.assertEqual((960, 720), (image.width, image.height))
        self.assertEqual(2, self.output.bitmap_resolution)
        for y in range(0, image.height, 37):
            for x in range(0, image.width, 13):
                alpha = image.pixel(x, y)[3]
                if 200 <= x < 760:
                    self.assertEqual(0, alpha, (x, y))
                else:
                    self.assertEqual(255, alpha, (x, y))
        # Biting edges: the last panel column and the first window column on each side.
        for y in (0, 359, 719):
            self.assertEqual(255, image.pixel(199, y)[3])
            self.assertEqual(0, image.pixel(200, y)[3])
            self.assertEqual(0, image.pixel(759, y)[3])
            self.assertEqual(255, image.pixel(760, y)[3])

    def test_source_is_pinned_by_hash(self) -> None:
        data = (bp.BEZEL_DIR / self.manifest["source"]["file"]).read_bytes()
        self.assertEqual(self.manifest["source"]["sha256"], se._sha256(data))
        tampered = dict(self.manifest, source=dict(self.manifest["source"], sha256="0" * 64))
        with self.assertRaises(bp.BezelPanelsError):
            bp.load_source(tampered)

    def test_provenance_records_no_reusable_license(self) -> None:
        overlay = json.loads(bp.OVERLAY_PROVENANCE_PATH.read_text(encoding="utf-8"))
        record = overlay["assets"][self.output.filename]
        self.assertIn("No reusable license", record["license"])
        self.assertIn("estefan3112", record["notes"])
        self.assertIn(self.manifest["source"]["sha256"], record["notes"])

    def test_missing_bezel_target_fails_loudly(self) -> None:
        with self.assertRaises(bp.BezelPanelsError):
            bp.expected_project({"targets": []}, self.output)


if __name__ == "__main__":
    unittest.main()
