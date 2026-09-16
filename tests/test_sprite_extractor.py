from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import sprite_extractor as extractor  # noqa: E402


class SpriteExtractorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest, cls.manifest_bytes = extractor.load_manifest()

    def test_manifest_and_committed_outputs_are_current(self) -> None:
        count, contact_hash = extractor.check_repository()
        # 51: the historical 10 (3 solvalou + 7 toroid), the 7 Terrazi roll frames (AIR-06),
        # the 7 Kapi dive frames (AIR-05), the 6 Torkan roll frames (AIR-02), the 4 Zoshi
        # spin frames (AIR-03), the 6 Jara spin frames (AIR-04), the 1 Zakato body frame (AIR-07),
        # the 1 Bacura slab frame (AIR-11), and the 9 ground frames (GND: 1 Barra idle, 4 Logram
        # open stages, 2 crater variants, 2 Garu base pulse frames).
        self.assertEqual(51, count)
        self.assertEqual(64, len(contact_hash))

    def test_rendering_is_byte_deterministic(self) -> None:
        first = extractor.render_derivatives(self.manifest)
        second = extractor.render_derivatives(self.manifest)
        self.assertEqual(
            [(item.filename, item.png) for item in first],
            [(item.filename, item.png) for item in second],
        )
        self.assertEqual(
            extractor.render_contact_sheet(first),
            extractor.render_contact_sheet(second),
        )

    def test_source_sheet_hashes_remain_pinned(self) -> None:
        for sheet in self.manifest["sheets"].values():
            data = (extractor.ASSET_DIR / sheet["asset"]).read_bytes()
            self.assertEqual(sheet["sha256"], hashlib.sha256(data).hexdigest())

    def test_outputs_are_rgba_with_transparency_and_stable_anchors(self) -> None:
        derivatives = extractor.render_derivatives(self.manifest)
        for derivative in derivatives:
            decoded = extractor.decode_png(derivative.png)
            # Each costume renders to its declared canvas (16x16 for a 1x1 sprite; the 2x2 Garu Barra
            # base is 32x32), anchored at the canvas centre. The intent is unchanged — RGBA with real
            # transparency and a stable centred anchor — just no longer hard-coded to the 1x1 size.
            canvas = derivative.frame["canvas"]
            self.assertEqual(tuple(canvas), (decoded.width, decoded.height))
            # Every crop with matte around/inside it keys transparent; the two genuinely solid
            # sprites are the 2x2 Garu Barra base (a filled foundation block) and the Bacura slab
            # (a solid indestructible panel), whose crops hold no matte — so neither has a
            # transparent pixel to assert, but both must still be fully opaque RGBA.
            name = derivative.frame["name"]
            if not name.startswith("garu/") and not name.startswith("bacura/"):
                self.assertTrue(any(pixel[3] == 0 for pixel in decoded.pixels))
            self.assertTrue(any(pixel[3] == 255 for pixel in decoded.pixels))
            self.assertEqual([canvas[0] // 2, canvas[1] // 2], derivative.frame["anchor"])

    def test_all_matte_is_transparent_including_enclosed(self) -> None:
        # (0,128,0) is the sheet's reserved matte, so every matte pixel is background —
        # even one fully enclosed by opaque artwork. An interior matte region is negative
        # space (the Toroid ring's centre, the gaps around Kapi's eyes) that must show the
        # game background through, not render as a green blob. Biting on the enclosed
        # centre: under the old edge-connected-only rule it stayed opaque (0,128,0,255).
        matte = (0, 128, 0, 255)
        opaque = (255, 255, 255, 255)
        ring = ((1, 1), (2, 1), (3, 1), (1, 2), (3, 2), (1, 3), (2, 3), (3, 3))
        pixels = [matte] * 25
        for x, y in ring:
            pixels[y * 5 + x] = opaque
        source = extractor.Image(5, 5, tuple(pixels))
        cropped = extractor._crop_and_remove_matte(
            source,
            (0, 0, 5, 5),
            (0, 128, 0),
        )
        # the enclosed matte centre is now transparent (the biting change from the old rule)
        self.assertEqual(0, cropped.pixel(2, 2)[3])
        # edge-reachable matte is transparent too
        self.assertEqual(0, cropped.pixel(0, 0)[3])
        # opaque artwork is never touched
        for x, y in ring:
            self.assertEqual(opaque, cropped.pixel(x, y))

    def test_duplicate_frame_name_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["frames"][1]["name"] = manifest["frames"][0]["name"]
        with self.assertRaisesRegex(
            extractor.SpriteExtractionError,
            "duplicate frame name",
        ):
            extractor.validate_manifest(manifest)

    def test_unknown_manifest_field_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["frames"][0]["unreviewed"] = True
        with self.assertRaisesRegex(
            extractor.SpriteExtractionError,
            "unknown unreviewed",
        ):
            extractor.validate_manifest(manifest)

    def test_overlapping_frame_rectangles_are_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["frames"][1]["rect"] = manifest["frames"][0]["rect"]
        with self.assertRaisesRegex(
            extractor.SpriteExtractionError,
            "overlapping frame rectangles",
        ):
            extractor.validate_manifest(manifest)

    def test_inconsistent_animation_anchor_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["frames"][4]["anchor"] = [7, 8]
        with self.assertRaisesRegex(
            extractor.SpriteExtractionError,
            "one canvas and anchor",
        ):
            extractor.validate_manifest(manifest)

    def test_crop_touching_label_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["frames"][3]["rect"] = [87, 7, 12, 12]
        with self.assertRaisesRegex(
            extractor.SpriteExtractionError,
            "excluded label or credit panel",
        ):
            extractor.validate_manifest(manifest)

    def test_unexpected_source_hash_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["sheets"]["solvalou"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(
            extractor.SpriteExtractionError,
            "source sheet solvalou hash changed",
        ):
            extractor.render_derivatives(manifest)

    def test_scratch_targets_use_costumes_per_family(self) -> None:
        project = json.loads(extractor.PROJECT_PATH.read_text(encoding="utf-8"))
        solvalou = next(
            target for target in project["targets"] if target["name"] == "solvalou"
        )
        toroid = next(
            target
            for target in project["targets"]
            if target["name"] == extractor.GENERATED_TARGET
        )
        self.assertEqual(
            [
                "solvalou/flight/01",
                "solvalou/flight/02",
                "solvalou/flight/03",
            ],
            [costume["name"] for costume in solvalou["costumes"][-3:]],
        )
        # The shared proof pen holds every enemy family's crops, in manifest order; each render target
        # mirrors only its own family's frames (game_director, keyed by the `<family>/` name prefix).
        self.assertEqual(
            [f"toroid/turn/{index:02d}" for index in range(1, 8)]
            + [f"terrazi/roll/{index:02d}" for index in range(1, 8)]
            + [f"kapi/dive/{index:02d}" for index in range(1, 8)]
            + [f"torkan/roll/{index:02d}" for index in range(1, 7)]
            + [f"zoshi/spin/{index:02d}" for index in range(1, 5)]
            + [f"jara/spin/{index:02d}" for index in range(1, 7)]
            + ["zakato/body/01"]
            + ["bacura/slab/01"]
            + ["barra/idle/01"]
            + [f"logram/open/{index:02d}" for index in range(1, 5)]
            + [f"crater/idle/{index:02d}" for index in range(1, 3)]
            + [f"garu/base/{index:02d}" for index in range(1, 3)],
            [costume["name"] for costume in toroid["costumes"]],
        )
        self.assertFalse(toroid["visible"])
        self.assertEqual({}, toroid["blocks"])
        self.assertEqual("don't rotate", toroid["rotationStyle"])
        for costume in solvalou["costumes"][-3:] + toroid["costumes"]:
            # Every 1x1 sprite is centred at (8,8); the 2x2 Garu Barra base (32x32 canvas) at (16,16);
            # the Bacura slab (24x16 canvas, wider than tall) at (12,8).
            if costume["name"].startswith("garu/"):
                expected_center = (16, 16)
            elif costume["name"].startswith("bacura/"):
                expected_center = (12, 8)
            else:
                expected_center = (8, 8)
            self.assertEqual(
                expected_center,
                (
                    costume["rotationCenterX"],
                    costume["rotationCenterY"],
                ),
            )

    def test_every_derivative_has_complete_provenance(self) -> None:
        provenance = json.loads(
            extractor.DERIVATIVE_PROVENANCE_PATH.read_text(encoding="utf-8")
        )
        overlay = json.loads(
            extractor.OVERLAY_PROVENANCE_PATH.read_text(encoding="utf-8")
        )["assets"]
        derivatives = extractor.render_derivatives(self.manifest)
        self.assertEqual(
            {derivative.filename for derivative in derivatives},
            set(provenance["outputs"]),
        )
        for derivative in derivatives:
            record = provenance["outputs"][derivative.filename]
            self.assertEqual(derivative.frame["name"], record["frame"])
            self.assertEqual(64, len(record["source_sha256"]))
            self.assertIn("No reusable license specified", record["license"])
            self.assertIn(derivative.filename, overlay)
            self.assertIn("did not create", overlay[derivative.filename]["notes"])


if __name__ == "__main__":
    unittest.main()
