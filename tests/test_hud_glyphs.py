from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import hud_glyphs as hg  # noqa: E402
import sprite_extractor as se  # noqa: E402


class HudGlyphsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest, cls.manifest_bytes = hg.load_manifest()

    def test_manifest_and_committed_outputs_are_current(self) -> None:
        count = hg.check_repository()
        self.assertEqual(32, count)

    def test_rendering_is_byte_deterministic(self) -> None:
        first_glyphs = hg.render_glyphs(self.manifest)
        second_glyphs = hg.render_glyphs(self.manifest)
        self.assertEqual(
            [(item.filename, item.png) for item in first_glyphs],
            [(item.filename, item.png) for item in second_glyphs],
        )
        first_life = hg.render_life_icon(self.manifest)
        second_life = hg.render_life_icon(self.manifest)
        self.assertEqual(
            (first_life.filename, first_life.png),
            (second_life.filename, second_life.png),
        )

    def test_source_sheet_hashes_remain_pinned(self) -> None:
        font_sheet = self.manifest["font_sheet"]
        data = (hg.FONT_DIR / font_sheet["asset"]).read_bytes()
        self.assertEqual(font_sheet["sha256"], hashlib.sha256(data).hexdigest())

        life_sheet = self.manifest["life_icon_sheet"]
        data = (ROOT / life_sheet["asset"]).read_bytes()
        self.assertEqual(life_sheet["sha256"], hashlib.sha256(data).hexdigest())

        sound = self.manifest["extend_sound"]
        data = hg.SOUND_SOURCE_PATH.read_bytes()
        self.assertEqual(sound["sha256"], hashlib.sha256(data).hexdigest())

    def test_glyph_outputs_are_uniform_square_rgba_with_transparency(self) -> None:
        outputs = hg.render_glyphs(self.manifest)
        self.assertEqual(32 - 1, len(outputs))
        expected_size = self.manifest["cell_canvas"][0] // self.manifest["downscale"]
        for output in outputs:
            decoded = se.decode_png(output.png)
            self.assertEqual((expected_size, expected_size), (decoded.width, decoded.height))
            self.assertTrue(any(pixel[3] == 0 for pixel in decoded.pixels))
            # Every opaque glyph pixel is exactly its recolor-target ink color (white for
            # glyph/digit, yellow for the hs/* "HIGH SCORE" set) — the recolor step never
            # leaves an intermediate shade.
            ink = hg.YELLOW_INK if output.name.startswith("hs/") else hg.WHITE_INK
            for pixel in decoded.pixels:
                self.assertIn(pixel, ((0, 0, 0, 0), ink))
            self.assertTrue(any(pixel == ink for pixel in decoded.pixels))

    def test_high_score_letters_are_yellow_and_share_white_letter_rects(self) -> None:
        # The 8 hs/* costumes are a pure recolor of their glyph/<letter> counterpart: same
        # source rect, only the ink color differs.
        outputs = {output.name: output for output in hg.render_glyphs(self.manifest)}
        self.assertEqual(set(f"hs/{letter}" for letter in hg.HS_LETTERS), {
            name for name in outputs if name.startswith("hs/")
        })
        for letter in hg.HS_LETTERS:
            white_glyph, white_recolor = hg._glyph_source(self.manifest, f"glyph/{letter}")
            yellow_glyph, yellow_recolor = hg._glyph_source(self.manifest, f"hs/{letter}")
            self.assertEqual("white", white_recolor)
            self.assertEqual("yellow", yellow_recolor)
            self.assertEqual(white_glyph["rect"], yellow_glyph["rect"])

    def test_life_icon_keeps_its_own_colors_on_a_16x16_canvas(self) -> None:
        output = hg.render_life_icon(self.manifest)
        decoded = se.decode_png(output.png)
        self.assertEqual((16, 16), (decoded.width, decoded.height))
        self.assertTrue(any(pixel[3] == 0 for pixel in decoded.pixels))
        self.assertTrue(any(pixel[3] == 255 for pixel in decoded.pixels))
        # The craft keeps its native palette rather than being recolored, unlike
        # the glyphs: some opaque pixel must be a color other than pure white.
        self.assertTrue(
            any(pixel[3] == 255 and pixel[:3] != (255, 255, 255) for pixel in decoded.pixels)
        )

    def test_extend_sound_matches_stage_convention(self) -> None:
        sound, data, filename = hg.render_extend_sound(self.manifest)
        self.assertEqual("extend", sound["name"])
        self.assertEqual("wav", sound["dataFormat"])
        self.assertEqual(filename, sound["md5ext"])
        self.assertTrue(data.startswith(b"RIFF"))
        self.assertEqual(b"WAVE", data[8:12])
        self.assertEqual(hashlib.md5(data, usedforsecurity=False).hexdigest(), sound["assetId"])

    def test_hud_target_carries_expected_glyph_and_life_costumes(self) -> None:
        project = json.loads(hg.PROJECT_PATH.read_text(encoding="utf-8"))
        hud = next(target for target in project["targets"] if target["name"] == hg.HUD_TARGET)
        self.assertFalse(hud["visible"])
        # hud_glyphs.py owns costumes only; the hud target's blocks (empty through the
        # media-only commit, populated from the ECO-02 HUD-render commit on) are
        # game_director.py's territory — see tests/test_scratch_project.py instead.
        self.assertEqual("don't rotate", hud["rotationStyle"])
        self.assertEqual(
            hg.COSTUME_ORDER,
            [costume["name"] for costume in hud["costumes"]],
        )
        for name in ("digit/0", "digit/9", "glyph/A", "glyph/V", "hs/H", "hs/S"):
            costume = next(c for c in hud["costumes"] if c["name"] == name)
            self.assertEqual("png", costume["dataFormat"])
            self.assertEqual(self.manifest["bitmap_resolution"], costume["bitmapResolution"])
        life_costume = next(c for c in hud["costumes"] if c["name"] == "life/ship")
        self.assertEqual(1, life_costume["bitmapResolution"])

    def test_stage_carries_the_extend_sound_alongside_historical_sounds(self) -> None:
        project = json.loads(hg.PROJECT_PATH.read_text(encoding="utf-8"))
        stage = next(target for target in project["targets"] if target["isStage"])
        names = [sound["name"] for sound in stage["sounds"]]
        self.assertEqual(["Game Start.mp3", "BGM.mp3", "extend"], names)

    def test_every_derivative_has_complete_provenance(self) -> None:
        provenance = json.loads(
            hg.DERIVATIVE_PROVENANCE_PATH.read_text(encoding="utf-8")
        )
        overlay = json.loads(
            hg.OVERLAY_PROVENANCE_PATH.read_text(encoding="utf-8")
        )["assets"]
        glyphs = hg.render_glyphs(self.manifest)
        life = hg.render_life_icon(self.manifest)
        _sound, _data, sound_filename = hg.render_extend_sound(self.manifest)
        expected_filenames = {output.filename for output in glyphs} | {
            life.filename,
            sound_filename,
        }
        self.assertEqual(expected_filenames, set(provenance["outputs"]))
        for filename in expected_filenames:
            self.assertIn(filename, overlay)
            record = overlay[filename]
            self.assertTrue(record["origin"].strip())
            self.assertTrue(record["license"].strip())
            self.assertIn("did not create", record["notes"])

    def test_glyph_and_digit_zero_may_legitimately_share_one_asset(self) -> None:
        # The source font draws the letter O and the digit 0 identically, so
        # their rendered costumes point at the same content-addressed PNG.
        # This is a deliberate, harmless consequence of content-addressed
        # asset naming, not a rendering bug — recorded in docs/mechanics/010.
        glyphs = {output.name: output.filename for output in hg.render_glyphs(self.manifest)}
        self.assertEqual(glyphs["glyph/O"], glyphs["digit/0"])

    # -- Negative fixtures (deepcopy-and-corrupt), mirroring test_sprite_extractor.py --

    def test_duplicate_glyph_name_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["glyphs"][1]["name"] = manifest["glyphs"][0]["name"]
        with self.assertRaisesRegex(hg.HudGlyphsError, "duplicate glyph name"):
            hg.validate_manifest(manifest)

    def test_missing_required_glyph_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["glyphs"].pop()
        with self.assertRaisesRegex(hg.HudGlyphsError, "exactly the required digit/letter set"):
            hg.validate_manifest(manifest)

    def test_unknown_manifest_field_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["unreviewed"] = True
        with self.assertRaisesRegex(hg.HudGlyphsError, "unknown unreviewed"):
            hg.validate_manifest(manifest)

    def test_oversized_glyph_rect_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["glyphs"][0]["rect"] = [0, 0, 200, 10]
        with self.assertRaisesRegex(hg.HudGlyphsError, "does not fit the cell canvas"):
            hg.validate_manifest(manifest)

    def test_cell_canvas_not_divisible_by_downscale_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["cell_canvas"] = [101, 101]
        with self.assertRaisesRegex(hg.HudGlyphsError, "evenly divisible by downscale"):
            hg.validate_manifest(manifest)

    def test_unexpected_font_sheet_hash_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["font_sheet"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(hg.HudGlyphsError, "font sheet hash changed"):
            hg.render_glyphs(manifest)

    def test_unexpected_life_icon_sheet_hash_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["life_icon_sheet"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(hg.HudGlyphsError, "life icon sheet hash changed"):
            hg.render_life_icon(manifest)

    def test_unexpected_sound_hash_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["extend_sound"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(hg.HudGlyphsError, "extend sound hash changed"):
            hg.render_extend_sound(manifest)

    def test_glyph_rect_outside_sheet_is_rejected(self) -> None:
        source = se.decode_png(
            (hg.FONT_DIR / self.manifest["font_sheet"]["asset"]).read_bytes()
        )
        with self.assertRaisesRegex(hg.HudGlyphsError, "falls outside the font sheet"):
            hg._binarize_glyph(source, (0, 0, source.width, 5), 128)

    def test_expected_project_requires_existing_hud_target(self) -> None:
        project = json.loads(hg.PROJECT_PATH.read_text(encoding="utf-8"))
        project = copy.deepcopy(project)
        project["targets"] = [
            target for target in project["targets"] if target["name"] != hg.HUD_TARGET
        ]
        glyphs = hg.render_glyphs(self.manifest)
        life = hg.render_life_icon(self.manifest)
        sound, _data, _filename = hg.render_extend_sound(self.manifest)
        with self.assertRaisesRegex(hg.HudGlyphsError, "no hud target"):
            hg.expected_project(project, glyphs, life, self.manifest, sound)


if __name__ == "__main__":
    unittest.main()
