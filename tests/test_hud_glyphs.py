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
        # 77: the 33 HUD/credit/sound outputs + the 13 slice-17 CAB-01 attract overlays on
        # start_screen (10 credit digits + CREDIT/PUSH START/INSERT COIN labels)
        # + the 2 slice-18 CAB-02 1P/2P start-selector labels ("1 PLAYER" / "2 PLAYERS")
        # + the 2 slice-18 CAB-03 "GAME OVER PLAYER n" elimination-banner costumes on the HUD target
        # + the 27 slice-19 CAB-04 per-letter name-cell glyphs on start_screen (A-Z and "."), which
        # render the LIVE best-five names/scores; the slice-17 single baked "best-five" table costume
        # is retired (so the slice-17 attract overlay count drops 14 -> 13). 51 - 1 + 27 = 77.
        # + the 4 slice-19 CAB-04 initials-entry screen costumes on start_screen (CONGRATULATIONS /
        # ENTER YOUR INITIALS headers and the PLAYER 1 / PLAYER 2 tags), port-original UI text in the
        # credited font at the SMALL_TEXT_GEOM cell (glyphs already in SHEET_TEXT_RECTS). 77 + 4 = 81.
        # + the 6 slice-20 PRES-01 best-five costumes on start_screen (the header and the ordinal ranks
        # 1ST..5TH, ATTRACT_TABLE_LABELS); PUSH START became PUSH START BUTTON in place. 81 + 6 = 87.
        # + the slice-21 "START SPACE KEY" title hint, re-rendered through the same pipeline. 87 + 1 = 88.
        # + slice 21 (#31): the banner is two rows, so its 2 "GAME OVER PLAYER n" costumes become 3 (GAME OVER,
        # PLAYER 1, PLAYER 2). 88 + 1 = 89.
        self.assertEqual(89, count)

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
        # CAB-03 (slice 18; two rows since slice 21): the three banner costumes are appended to the HUD
        # target AFTER the fixed COSTUME_ORDER glyphs (so the per-glyph indices never shift), in
        # BANNER_LABELS order — the game_director banner clone switches to them by name.
        self.assertEqual(
            hg.COSTUME_ORDER + [name for name, _text in hg.BANNER_LABELS],
            [costume["name"] for costume in hud["costumes"]],
        )
        for name in ("digit/0", "digit/9", "glyph/A", "glyph/V", "hs/H", "hs/S"):
            costume = next(c for c in hud["costumes"] if c["name"] == name)
            self.assertEqual("png", costume["dataFormat"])
            self.assertEqual(self.manifest["bitmap_resolution"], costume["bitmapResolution"])
        life_costume = next(c for c in hud["costumes"] if c["name"] == "life/ship")
        self.assertEqual(1, life_costume["bitmapResolution"])

    def test_stage_carries_the_added_sounds_alongside_historical_sounds(self) -> None:
        # The Stage carries the two historical base sounds, then hud_glyphs.py's added
        # sounds: the "extend" cue, then the seven arcade gameplay-SFX cues in name order
        # (AUDIO; docs/mechanics/040-arcade-sound-cues.md — bonus_flag added for SEC-02, slice 14;
        # CAB-05 slice 20 adds the coin, high/top-score, Andor and five base-sound replacement cues).
        project = json.loads(hg.PROJECT_PATH.read_text(encoding="utf-8"))
        stage = next(target for target in project["targets"] if target["isStage"])
        names = [sound["name"] for sound in stage["sounds"]]
        self.assertEqual(
            ["Game Start.mp3", "BGM.mp3", "extend", "air_destroy", "andor_genesis", "bacura",
             "bgm", "blaster_fire", "bonus_flag", "credit", "garu_zakato", "ground_destroy",
             "name_entry", "name_entry_top", "sheonite", "solvalou_explode", "start",
             "zakato", "zapper_fire"],
            names,
        )

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
        game_sounds = hg.render_game_sounds()
        sheet = hg._load_font_sheet(self.manifest)
        threshold = self.manifest["glyph_threshold"]
        credit = hg.render_credit(sheet, threshold)
        attract = hg.render_attract_costumes(sheet, threshold)
        attract_filenames = {output.filename for output in attract}
        banner = hg.render_banner_costumes(sheet, threshold)
        banner_filenames = {output.filename for output in banner}
        expected_filenames = {output.filename for output in glyphs} | {
            life.filename,
            sound_filename,
            credit.filename,
        } | {output.filename for output in game_sounds} | attract_filenames | banner_filenames
        self.assertEqual(expected_filenames, set(provenance["outputs"]))
        sheet_license = self.manifest["font_sheet"]["license"]
        for filename in expected_filenames:
            self.assertIn(filename, overlay)
            record = overlay[filename]
            self.assertTrue(record["origin"].strip())
            self.assertTrue(record["license"].strip())
            if filename == credit.filename:
                # SEC-03: the hidden-credit overlay's WORDING is the port's own original content, but
                # since the operator chose to render all text from the one credited Xevious HUD font
                # sheet, its letterforms are that CC-BY font — so the record carries BOTH the
                # third-party "did not create the font" attribution AND the project-original stance
                # for the wording (operator's own content, not arcade art).
                self.assertEqual(sheet_license, record["license"])
                self.assertIn("did not create the font", record["notes"])
                self.assertIn("operator's own content", record["notes"])
                self.assertIn("NOT arcade art", record["notes"])
            elif filename in banner_filenames:
                # CAB-03 (slice 18): the "GAME OVER PLAYER n" banners render from the SAME credited
                # CC-BY sheet, so they carry the font attribution; their WORDING is arcade-faithful
                # English UI text set in that font — not arcade art and not transcribed ROM text.
                self.assertEqual(sheet_license, record["license"])
                self.assertIn("did not create the font", record["notes"])
                self.assertIn("not transcribed ROM text", record["notes"])
            elif filename in attract_filenames:
                # CAB-01 (slice 17): the attract overlays render from the SAME credited CC-BY sheet,
                # so they carry the font attribution; their CONTENT (prompts, placeholder initials)
                # is still project-original — never the ROM default name strings.
                self.assertEqual(sheet_license, record["license"])
                self.assertIn("did not create the font", record["notes"])
                self.assertIn("NOT the ROM default name strings", record["notes"])
            else:
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
        credit = hg.render_credit(
            hg._load_font_sheet(self.manifest), self.manifest["glyph_threshold"]
        )
        with self.assertRaisesRegex(hg.HudGlyphsError, "no hud target"):
            hg.expected_project(project, glyphs, life, self.manifest, sound, credit)


if __name__ == "__main__":
    unittest.main()
