"""Sprite data the build derives from the pinned reference's graphics (assets/amiga/xevious_gfx.c).

- The Sol Tower rise sheet (tools/sol_tower_render.py) re-derives byte-for-byte from the pin.
- The CAB-05 effects sheet (tools/effects_sprite_render.py: the three explosions, crater, crosshair, bomb
  target and bomb) re-derives byte-for-byte from the pin, from the reference's own code and colour tables.
- The slice-21 reference-art sheet (tools/reference_art_render.py: Giddo Spario, the pulsing Zakato bodies,
  the self-destruct and teleport frames, the Brag Spario, the shot and its rebound, the title sparkle)
  re-derives byte-for-byte from the pin; its sprite tables are read back from the source itself.
- The fair Bacura craft-kill box (game_director.BACURA_FRAME_OPAQUE, a recorded divergence in
  docs/mechanics/037) uses each tumble frame's opaque extents exactly as the reference tiles draw them.

The reference-reading tests skip when no verified checkout at the pin is present
(`python3 tools/reference_checkout.py ensure`); the arithmetic pins always run.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import andor_sprite_render as asr  # noqa: E402
import effects_sprite_render as fx  # noqa: E402
import game_director as director  # noqa: E402
import reference_art_render as art  # noqa: E402
import reference_checkout as checkout  # noqa: E402
import sol_tower_render as sol  # noqa: E402

PINNED_COMMIT = "71473685a8c7856c8401c8519276cd97a38d4183"
# bacura_sprite_tbl (src/xevious_main.68k), in tumble-frame order; each frame is a 1x2 sprite whose
# second tile is code+2 (sprite_draw_double_height, src/amiga/amiga.68k 2546-2552: `code` at x+16,
# `code+2` at x), so the 32x16 slab is [code+2 | code] along the lateral axis.
BACURA_CODES = (0x120, 0x121, 0x124, 0x125, 0x128, 0x129, 0x12C, 0x12D)
# The table's colour (0x2B/0x2C, alternating) only picks the palette; transparency is pixel value 0 in any
# CLUT, so the extents do not depend on it.
BACURA_CLUT = 0x2B


def _reference_dir() -> Path | None:
    path = checkout.default_dir()
    if checkout.head_commit(path) != PINNED_COMMIT:
        return None
    if not (path / asr.GFX_C).exists():
        return None
    return path


REFERENCE = _reference_dir()


class BacuraFairWindowTests(unittest.TestCase):
    def test_full_tile_reproduces_the_arcade_box(self) -> None:
        # The fair box is the arcade's craft core against a sub-rectangle of the slab; given the whole tile it
        # must be check_bacura_hit_solvalou's own window, or the formula is wrong.
        self.assertEqual(director.bacura_fair_window(0, 15, 0, 31), director.HIT_WINDOW_BACURA)

    def test_every_frame_box_is_symmetric_and_inside_the_arcade_box(self) -> None:
        arc_lat = director._hit_range(*director.HIT_WINDOW_BACURA[:2])
        arc_dep = director._hit_range(*director.HIT_WINDOW_BACURA[2:])
        self.assertEqual(len(director.BACURA_FRAME_OPAQUE), director.BACURA_TUMBLE_FRAMES)
        for frame, (top, bottom, left, right) in enumerate(director.BACURA_FRAME_OPAQUE):
            self.assertEqual((top + bottom, left + right), (15, 31), frame)
            window = director.bacura_fair_window(top, bottom, left, right)
            lat = director._hit_range(*window[:2])
            dep = director._hit_range(*window[2:])
            self.assertTrue(arc_lat[0] <= lat[0] <= lat[1] <= arc_lat[1], frame)
            self.assertTrue(arc_dep[0] <= dep[0] <= dep[1] <= arc_dep[1], frame)
        # No frame fills the whole tile, so every frame is strictly smaller than the arcade box on some axis.
        self.assertNotIn((0, 15, 0, 31), director.BACURA_FRAME_OPAQUE)

    def test_digit_strings_match_the_table(self) -> None:
        # The Scratch side reads T and L by `letter (frame+1) of` these strings; the runtime bounds
        # [L-12, 27-L] and [-floor((16-T)/2), floor((15-T)/2)] must equal bacura_fair_window per frame.
        for frame, (top, bottom, left, right) in enumerate(director.BACURA_FRAME_OPAQUE):
            t = int(director.BACURA_FRAME_TOP_DIGITS[frame])
            l = int(director.BACURA_FRAME_LEFT_DIGITS[frame])
            runtime = ((l - 12, 27 - l), (-((16 - t) // 2), (15 - t) // 2))
            window = director.bacura_fair_window(top, bottom, left, right)
            self.assertEqual(
                runtime, (director._hit_range(*window[:2]), director._hit_range(*window[2:])), frame
            )

    @unittest.skipIf(REFERENCE is None, "no verified reference checkout at the pin")
    def test_opaque_extents_match_the_reference_tiles(self) -> None:
        gfx = asr._Gfx(asr._read_reference(REFERENCE, asr.GFX_C))
        for frame, code in enumerate(BACURA_CODES):
            # Slab pixel (row, col): col 0-15 from tile code+2, col 16-31 from tile code.
            tiles = (gfx.tile_rgba(code + 2, BACURA_CLUT), gfx.tile_rgba(code, BACURA_CLUT))
            rows, cols = set(), set()
            for half, tile in enumerate(tiles):
                for y in range(asr.TILE):
                    for x in range(asr.TILE):
                        if tile[y * asr.TILE + x][3]:
                            rows.add(y)
                            cols.add(half * asr.TILE + x)
            self.assertEqual(
                (min(rows), max(rows), min(cols), max(cols)), director.BACURA_FRAME_OPAQUE[frame], frame
            )


class SolTowerRenderTests(unittest.TestCase):
    def test_layout_is_seven_32px_cells(self) -> None:
        self.assertEqual((sol.SHEET_WIDTH, sol.SHEET_HEIGHT), (32 * 7, 32))
        origins = sol.SMALL_CELL_ORIGINS + sol.BIG_CELL_ORIGINS
        self.assertEqual(origins, [(32 * i, 0) for i in range(7)])

    @unittest.skipIf(REFERENCE is None, "no verified reference checkout at the pin")
    def test_committed_sheet_rerenders_at_the_pin(self) -> None:
        self.assertEqual(sol.main(["--checkout", str(REFERENCE), "--verify"]), 0)


class EffectsRenderTests(unittest.TestCase):
    """CAB-05: the explosion, crater, crosshair, bomb-target and bomb cells (tools/effects_sprite_render.py)."""

    def test_codes_and_colours_are_the_reference_tables(self) -> None:
        # solvalou_explosion_tbl 2093-2100, flying_obj_explosion_sprites 4892-4897,
        # bomb_explosion_animation_tbl 4944-4951 (code, 2x2?) in animation order.
        self.assertEqual(fx.PLAYER_EXPLOSION, [(0xC0, False), (0xC1, False), (0xC4, True), (0xC8, True),
                                               (0xC2, False), (0xC3, False), (0xCC, True)])
        self.assertEqual(fx.AIR_EXPLOSION, [(0x70, False), (0x71, False), (0x74, True), (0x78, True),
                                            (0x7C, True)])
        self.assertEqual(fx.GROUND_EXPLOSION, [(0x60, False), (0x61, False), (0x64, True), (0x68, True),
                                               (0x6C, True), (0x62, False), (0x63, False)])
        self.assertEqual((fx.PLAYER_EXPLOSION_CLUT_BASE, fx.AIR_EXPLOSION_CLUT, fx.GROUND_EXPLOSION_CLUT),
                         (0x30, 7, 0x0C))
        self.assertEqual((fx.CRATER_CODES, fx.CRATER_CLUT), ([0xA6, 0xA7], 0x0D))
        # Bank 1 adds 0x100 (amiga.68k 1774-1778): crosshair 0x14, bomb 0x1C..0x1E.
        self.assertEqual(fx.CROSSHAIR_CODE, 0x114)
        self.assertEqual(fx.CROSSHAIR_CLUTS, [32, 33, 41, 42])
        self.assertEqual(fx.BOMB_TARGET_CLUT, 0x22)
        self.assertEqual(fx.BOMB_CODES, [0x11C, 0x11D, 0x11E])
        self.assertEqual(fx.BOMB_CLUTS, [0x25, 0x26, 0x27, 0x28])

    def test_layout(self) -> None:
        # 32-px explosion cells, every frame concentric: the arcade nudges the object half a 2x2's overhang
        # (8 px) up-left on entering a 2x2 frame, so a 1x1 frame and a 2x2 frame share one centre.
        self.assertEqual(fx.EXPLOSION_CELL, 2 * asr.TILE)
        self.assertEqual(fx.ONE_BY_ONE_ORIGIN + asr.TILE // 2, fx.EXPLOSION_CELL // 2)
        self.assertEqual(fx.TWO_BY_TWO_ORIGIN + asr.TILE, fx.EXPLOSION_CELL // 2)
        self.assertEqual(fx.ONE_BY_ONE_ORIGIN - fx.TWO_BY_TWO_ORIGIN, 8)  # one position MSB
        self.assertEqual((fx.SHEET_WIDTH, fx.SHEET_HEIGHT), (32 * 7, 32 * 3 + 16 * 2))
        small = fx.CRATER_ORIGINS + fx.CROSSHAIR_ORIGINS + [fx.BOMB_TARGET_ORIGIN]
        self.assertEqual(small, [(16 * i, 96) for i in range(7)])
        self.assertEqual(fx.BOMB_ORIGINS, [(16 * i, 112) for i in range(12)])

    @unittest.skipIf(REFERENCE is None, "no verified reference checkout at the pin")
    def test_committed_sheet_rerenders_at_the_pin(self) -> None:
        self.assertEqual(fx.main(["--checkout", str(REFERENCE), "--verify"]), 0)

    @unittest.skipIf(REFERENCE is None, "no verified reference checkout at the pin")
    def test_colour_entry_0x80_is_transparent(self) -> None:
        # Colour table 0 is all 0x80: past the 128-colour palette, so it draws nothing; the decoder must say so
        # rather than index past the palette.
        gfx = asr._Gfx(asr._read_reference(REFERENCE, asr.GFX_C))
        self.assertEqual(set(gfx.sprite_clut[0]), {fx.TRANSPARENT_ENTRY})
        self.assertEqual(len(gfx.palette), fx.TRANSPARENT_ENTRY)
        with self.assertRaisesRegex(fx.SpriteExtractionError, "draws nothing"):
            fx._tile(gfx, 0xC0, 0)  # every pixel transparent -> "draws nothing"


def _byte_table(label: str, source: str = "src/xevious_main.68k") -> list[int]:
    """The `.byte` values under `label:` in the pinned source, up to the next blank or non-.byte line."""
    lines = (REFERENCE / source).read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.split("|")[0].strip() == f"{label}:")
    values: list[int] = []
    for line in lines[start + 1:]:
        body = line.split("|")[0].strip()
        if not body.startswith(".byte"):
            break
        values += [int(token.strip(), 0) for token in body[len(".byte"):].split(",")]
    return values


class ReferenceArtRenderTests(unittest.TestCase):
    """presentation.reference-art: the enemy, shot and sparkle cells (tools/reference_art_render.py)."""

    def test_codes_and_colours(self) -> None:
        # Bank 1 adds 0x100 (amiga.68k 1774-1778). handle_08_Giddo_Spario 5219-5239 / giddo_spario_hit
        # 5241-5252; handle_09_Brag_Spario 3080-3121; bodies 3749 / 3877 / 4014; shot 2374-2388; rebound
        # shot_destroyed 2400-2417; title sparkle attract_mode_title_screen 1217-1290.
        self.assertEqual(art.GIDDO_FLY_CODES, [0x100, 0x101, 0x102, 0x103])
        self.assertEqual(art.GIDDO_HIT_CODES, [0x104, 0x105, 0x106, 0x107])
        self.assertEqual(art.GIDDO_CLUTS, [0x26, 0x27, 0x28, 0x29])
        self.assertEqual((art.BRAG_SPARIO_CODE, art.BRAG_SPARIO_CLUT), (0x115, 0x26))
        self.assertEqual((art.ZAKATO_BODY_CODE, art.BRAG_ZAKATO_BODY_CODE, art.GARU_ZAKATO_BODY_CODE),
                         (0x111, 0x112, 0x113))
        self.assertEqual(art.SHOT_CODES, [0x116, 0x117])
        self.assertEqual(art.SHOT_CLUTS, [0x23, 0x24])
        self.assertEqual((art.REBOUND_CODES, art.REBOUND_CLUT), ([0x118, 0x119, 0x11A, 0x11B], 0x23))
        self.assertEqual((art.SPARKLE_CODES, art.SPARKLE_CLUT), (list(range(0x130, 0x140)), 0x0F))
        self.assertEqual(art.TELEPORT_CLUT, 0x24)

    def test_layout(self) -> None:
        # Seven rows: Giddo flight, Giddo hit, bodies, self-destruct (the widest, 25 cells), Brag Spario + shot +
        # rebound, sparkle, then the 32x32 teleport cells with every frame centred.
        self.assertEqual((art.SHEET_WIDTH, art.SHEET_HEIGHT), (16 * 25, 16 * 6 + 32))
        self.assertEqual(art.GIDDO_FLY_ORIGINS, [(16 * i, 0) for i in range(16)])
        self.assertEqual(art.GIDDO_HIT_ORIGINS, [(16 * i, 16) for i in range(16)])
        bodies = [art.ZAKATO_BODY_ORIGIN] + art.BRAG_ZAKATO_BODY_ORIGINS + art.GARU_ZAKATO_BODY_ORIGINS
        self.assertEqual(bodies, [(16 * i, 32) for i in range(10)])
        self.assertEqual(art.SELF_DESTRUCT_ORIGINS, [(16 * i, 48) for i in range(25)])
        row5 = [art.BRAG_SPARIO_ORIGIN] + art.SHOT_ORIGINS + art.REBOUND_ORIGINS
        self.assertEqual(row5, [(16 * i, 64) for i in range(9)])
        self.assertEqual(art.SPARKLE_ORIGINS, [(16 * i, 80) for i in range(16)])
        self.assertEqual(art.TELEPORT_ORIGINS, [(32 * i, 96) for i in range(5)])
        self.assertEqual(fx.ONE_BY_ONE_ORIGIN + asr.TILE // 2, art.TELEPORT_CELL // 2)
        self.assertEqual(fx.TWO_BY_TWO_ORIGIN + asr.TILE, art.TELEPORT_CELL // 2)

    @unittest.skipIf(REFERENCE is None, "no verified reference checkout at the pin")
    def test_tables_are_read_from_the_source(self) -> None:
        # (code, attr) pairs; bank 1 is attr bit 7 and a 2x2 is attr bits 0-1 = 3. The renderer draws the five
        # frames the timer reaches ((TIMER >> 2) & 7 < 5).
        teleport = _byte_table("zakato_teleport_sprite_tbl")
        pairs = list(zip(teleport[0::2], teleport[1::2]))[:5]
        self.assertEqual(art.TELEPORT, [(art.BANK_1 + code, attr & 3 == 3) for code, attr in pairs])
        self.assertTrue(all(attr & 0x80 for _, attr in pairs))
        # zakato_explode never stores the attr byte, so its frames stay 1x1: only the codes matter.
        explode = _byte_table("zakato_exploding_sprite_tbl")
        self.assertEqual(art.SELF_DESTRUCT_CODES, [art.BANK_1 + code for code in explode[0::2][:5]])
        pulsing = _byte_table("colour_lut_pulsing_2", "src/xevious_sub.68k")
        self.assertEqual(art.PULSING_CLUTS, sorted(set(pulsing)))

    @unittest.skipIf(REFERENCE is None, "no verified reference checkout at the pin")
    def test_only_the_distinct_bodies_are_cut(self) -> None:
        # The Zakato body is one picture at every pulsing colour and the Brag Zakato's 0x14 is that picture, so
        # the sheet cuts the Zakato once and the Brag Zakato's other four; every cut body is distinct.
        gfx = asr._Gfx(asr._read_reference(REFERENCE, asr.GFX_C))
        zakato = fx._tile(gfx, art.ZAKATO_BODY_CODE, art.ZAKATO_BODY_CLUT)
        for clut in art.PULSING_CLUTS:
            self.assertEqual(fx._tile(gfx, art.ZAKATO_BODY_CODE, clut), zakato)
        self.assertEqual(fx._tile(gfx, art.BRAG_ZAKATO_BODY_CODE, art.PULSING_CLUTS[-1]), zakato)
        cut = [zakato]
        cut += [fx._tile(gfx, art.BRAG_ZAKATO_BODY_CODE, clut) for clut in art.BRAG_ZAKATO_BODY_CLUTS]
        cut += [fx._tile(gfx, art.GARU_ZAKATO_BODY_CODE, clut) for clut in art.PULSING_CLUTS]
        self.assertEqual(len({tuple(tile) for tile in cut}), len(cut))

    @unittest.skipIf(REFERENCE is None, "no verified reference checkout at the pin")
    def test_committed_sheet_rerenders_at_the_pin(self) -> None:
        self.assertEqual(art.main(["--checkout", str(REFERENCE), "--verify"]), 0)


if __name__ == "__main__":
    unittest.main()
