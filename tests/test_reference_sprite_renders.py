"""Sprite data the build derives from the pinned reference's graphics (assets/amiga/xevious_gfx.c).

- The Sol Tower rise sheet (tools/sol_tower_render.py) re-derives byte-for-byte from the pin.
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
import game_director as director  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
