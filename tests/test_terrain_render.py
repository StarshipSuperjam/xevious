"""Guards for tools/terrain_render.py -- the terrain rendered from the arcade map ROM and tiles.

The checks that need the pinned reference checkout (the decode against the reference's own
background video-RAM snapshot, and re-rendering the committed images) skip when no verified
checkout is present, as in the project CI; `python tools/terrain_render.py verify` runs them
in the playtest handover (tools/playtest_package.py). Everything else runs everywhere: the
committed images, their provenance, the fan-map orientation cross-check, and the tile, plane
and filler rules on synthetic data.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import reference_checkout as checkout  # noqa: E402
import sprite_extractor as se  # noqa: E402
import terrain_render as tr  # noqa: E402


def _reference_dir() -> Path | None:
    path = checkout.default_dir()
    if checkout.head_commit(path) != tr.PINNED_COMMIT:
        return None
    if not all((path / rel).exists() for rel in tr.EXPECTED_SHA256):
        return None
    return path


REFERENCE = _reference_dir()


class CommittedTerrainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.map_png = tr.MAP_PNG.read_bytes()
        cls.filler_png = tr.FILLER_PNG.read_bytes()
        cls.map_w, cls.map_h, cls.map_raw = tr.decode_rgba(cls.map_png, "arcade map")
        cls.provenance = json.loads(tr.PROVENANCE.read_text(encoding="utf-8"))

    def test_images_have_the_map_and_filler_geometry(self) -> None:
        # 128 x 256 map cells of 8 x 8 px; the filler is 28 visible plane columns x 64 plane rows.
        self.assertEqual((1024, 2048), (self.map_w, self.map_h))
        width, height, raw = tr.decode_rgba(self.filler_png, "forest filler")
        self.assertEqual((224, 512), (width, height))
        # The background layer is opaque everywhere.
        self.assertTrue(all(a == 255 for a in self.map_raw[3::4]))
        self.assertTrue(all(a == 255 for a in raw[3::4]))

    def test_provenance_pins_the_committed_images_and_reference_inputs(self) -> None:
        rendered = self.provenance["rendered"]
        self.assertEqual(se._sha256(self.map_png), rendered["arcade_map.png"]["sha256"])
        self.assertEqual(se._sha256(self.filler_png), rendered["forest_filler.png"]["sha256"])
        for record in rendered.values():
            self.assertEqual(tr.PINNED_COMMIT, record["reference"]["commit"])
            self.assertEqual(
                {tr.MAP_ROM: tr.EXPECTED_SHA256[tr.MAP_ROM], tr.GFX_C: tr.EXPECTED_SHA256[tr.GFX_C]},
                record["reference"]["inputs"],
            )
            self.assertIn("rights review", record["license"])
        # The fan map stays recorded, as a cross-check only.
        self.assertIn("xevious_area_map.png", self.provenance["sources"])
        self.assertIn("cross-check only", self.provenance["note"])

    def test_fan_map_agrees_only_at_the_screen_orientation_and_exact_registration(self) -> None:
        # Independent cross-check: the committed render must line up with the fan-made map
        # (2048 x 1024, landscape) turned a quarter-turn clockwise, pixel-registered -- the
        # mirrored placement and the map row/column grid both show up here. Colours differ
        # slightly (the fan map's palette), so it is a mean-difference comparison: the true
        # orientation at zero shift must be far closer than any other orientation or any shift.
        fan = se.decode_png((tr.TERRAIN_DIR / "xevious_area_map.png").read_bytes(), "fan map")
        fw, fh = fan.width, fan.height
        self.assertEqual((2048, 1024), (fw, fh))
        orientations = {
            "clockwise": lambda x, y: (y, fh - 1 - x),
            "anticlockwise": lambda x, y: (fw - 1 - y, x),
            "clockwise-mirrored": lambda x, y: (y, x),
            "anticlockwise-mirrored": lambda x, y: (fw - 1 - y, fh - 1 - x),
        }

        def mean_difference(orient, dx: int = 0, dy: int = 0) -> float:
            total = count = 0
            # A stride of 7 (coprime to the 8-px tile) samples every pixel phase within a tile.
            for y in range(16, self.map_h - 16, 7):
                for x in range(16, self.map_w - 16, 7):
                    i = (y * self.map_w + x) * 4
                    other = fan.pixel(*orient(x + dx, y + dy))
                    total += sum(abs(self.map_raw[i + c] - other[c]) for c in range(3))
                    count += 1
            return total / count

        true = mean_difference(orientations["clockwise"])
        self.assertLess(true, 25.0)
        for name, orient in orientations.items():
            if name != "clockwise":
                self.assertGreater(mean_difference(orient), 3 * true, name)
        for dx, dy in ((4, 0), (-4, 0), (0, 4), (0, -4), (8, 0), (0, 8)):
            self.assertGreater(mean_difference(orientations["clockwise"], dx, dy), 1.8 * true, (dx, dy))


class RulesTests(unittest.TestCase):
    def test_encoder_is_the_repo_encoder_and_round_trips(self) -> None:
        pixels = tuple((x * 40, y * 60, (x + y) * 20, 255) for y in range(3) for x in range(5))
        image = se.Image(5, 3, pixels)
        raw = b"".join(bytes(p) for p in pixels)
        self.assertEqual(se.encode_png(image), tr.encode_rgba(5, 3, raw))
        self.assertEqual((5, 3, raw), tr.decode_rgba(tr.encode_rgba(5, 3, raw), "round trip"))

    def test_tile_code_and_colour_table_follow_the_reference_renderer(self) -> None:
        # amiga.68k 1304-1337: attribute bit 0 is tile code bit 8; the colour table is
        # attr bits 5..2 -> 3..0 and bits 1..0 -> 6..5, plus 0x10 when tile bit 7 is set.
        self.assertEqual(0x05, tr.tile_code(0x00, 0x05))
        self.assertEqual(0x105, tr.tile_code(0x01, 0x05))
        self.assertEqual(0x0F, tr.tile_clut(0x3C, 0x00))
        self.assertEqual(0x60, tr.tile_clut(0x03, 0x00))
        self.assertEqual(0x10, tr.tile_clut(0x00, 0x80))
        self.assertEqual(0x10, tr.tile_clut(0x00, 0x88))  # the forest filler: colour byte 0
        self.assertEqual(0x7F, tr.tile_clut(0xFF, 0x80))

    def test_flip_bits_mirror_and_flip_one_tile(self) -> None:
        # A synthetic tile whose value encodes its own position, with four distinct colours.
        tile = [(1 if x < 4 else 2) if y < 4 else 3 for y in range(8) for x in range(8)]
        tile[0] = 0
        gfx = tr.Gfx(
            bg_tile=[tile] * 512,
            bg_tile_clut=[[0, 1, 2, 3]] * 128,
            palette=[[10, 0, 0], [20, 0, 0], [30, 0, 0], [40, 0, 0]] + [[0, 0, 0]] * 124,
        )
        plain = tr.tile_rows(gfx, 0x00, 0x01)
        self.assertEqual(bytes([10, 0, 0, 255]), plain[0][0:4])         # top-left
        mirrored = tr.tile_rows(gfx, 0x80, 0x01)                         # bit 7: X-flip
        self.assertEqual(bytes([10, 0, 0, 255]), mirrored[0][28:32])     # now top-right
        self.assertEqual(bytes([30, 0, 0, 255]), mirrored[0][0:4])
        flipped = tr.tile_rows(gfx, 0x40, 0x01)                          # bit 6: Y-flip
        self.assertEqual(bytes([10, 0, 0, 255]), flipped[7][0:4])        # now bottom-left
        self.assertEqual(bytes([40, 0, 0, 255]), flipped[0][0:4])
        both = tr.tile_rows(gfx, 0xC0, 0x01)
        self.assertEqual(bytes([10, 0, 0, 255]), both[7][28:32])

    def test_plane_layout_matches_get_map_row(self) -> None:
        # get_map_row (xevious_sub.68k 247-290): the i-th cell of map row R lands at plane column (i + 1) & 31,
        # plane row (R + 3) & 63; it reads map column offset - 1 + i, so plane columns 2..29
        # (the visible ones) carry map columns offset..offset+27.
        for row in range(256):
            for i in range(32):
                offset = tr.plane_offset(row, i)
                self.assertEqual(((i + 1) & 31, (row + 3) & 63), (offset >> 6, offset & 63))
        for area_offset in (0, 42, 100):
            visible = [tr.map_column_for(area_offset, pc - 1)
                       for pc in range(tr.FIRST_VISIBLE_PLANE_COLUMN,
                                       tr.FIRST_VISIBLE_PLANE_COLUMN + tr.VISIBLE_COLUMNS)]
            self.assertEqual(list(range(area_offset, area_offset + 28)), visible)

    def test_every_area_start_column_shows_inside_the_map(self) -> None:
        terrain = json.loads((ROOT / "docs" / "spec" / "data" / "terrain.json").read_text(encoding="utf-8"))
        offsets = terrain["area_offset_in_map_tbl"]["values"]
        self.assertEqual(16, len(offsets))
        for offset in offsets:
            self.assertTrue(0 <= offset and offset + tr.VISIBLE_COLUMNS <= tr.MAP_COLUMNS, offset)

    def test_forest_filler_matches_the_source_fill_loop(self) -> None:
        # Simulate fill_bg_with_forest (xevious_main.68k 648-669) on a plane: 28 tiles from
        # 0x88, then 0x7E4 copies of the byte 28 offsets back.
        plane = [0] * 0x800
        offset = 0
        for k in range(0x1C):
            plane[offset] = 0x88 + k
            offset += 1
        source = 0
        for _ in range(0x7E4):
            plane[offset] = plane[source]
            source += 1
            offset += 1
        self.assertEqual(0x800, offset)
        for value in range(0x800):
            self.assertEqual(plane[value], tr.forest_tile(value >> 6, value & 63), value)

    def test_snapshot_runs_need_every_column_and_consecutive_rows(self) -> None:
        # A synthetic map whose every cell is distinct, written into a plane the way
        # get_map_row writes it for area offset 5, map rows 100..109.
        cells = [[((row * 7 + col) & 0xFF, (row * 3 + col * 5) & 0xFF) for col in range(128)]
                 for row in range(256)]
        snap = bytearray(0x1000)
        for row in range(100, 110):
            for i in range(32):
                o = tr.plane_offset(row, i)
                attr, tile = cells[row][tr.map_column_for(5, i) & 0x7F]
                snap[o], snap[0x800 + o] = attr, tile
        best = tr.best_snapshot_match(cells, bytes(snap))
        self.assertEqual((5, (100 + 3) & 63, tuple(range(100, 110))),
                         (best.area_offset, best.first_plane_row, best.map_rows))
        # One wrong byte in one column of row 104 splits the run.
        snap[0x800 + tr.plane_offset(104, 17)] ^= 1
        best = tr.best_snapshot_match(cells, bytes(snap))
        self.assertEqual(tuple(range(105, 110)), best.map_rows)


@unittest.skipUnless(REFERENCE is not None, "no verified reference checkout at the pin")
class ReferenceTerrainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.roms, cls.gfx = tr.load(REFERENCE)
        cls.cells = tr.decode_map(cls.roms)
        cls.snapshot = tr.read_reference(REFERENCE, tr.SNAPSHOT)

    def test_decode_reproduces_the_reference_screen_snapshot(self) -> None:
        # The reference's own bg video-RAM dump (taken early in area 9, start column 42):
        # 38 consecutive plane rows equal the decode exactly, both bytes, all 32 columns.
        best = tr.best_snapshot_match(self.cells, self.snapshot)
        self.assertEqual(
            (tr.EXPECTED_SNAPSHOT_OFFSET, tr.EXPECTED_SNAPSHOT_FIRST_PLANE_ROW, tr.EXPECTED_SNAPSHOT_MAP_ROWS),
            (best.area_offset, best.first_plane_row, best.map_rows),
        )
        self.assertEqual(42, json.loads((ROOT / "docs" / "spec" / "data" / "terrain.json")
                                        .read_text(encoding="utf-8"))["area_offset_in_map_tbl"]["values"][8])

    def test_a_wrong_colour_decode_fails_the_snapshot(self) -> None:
        # Negative: dropping xevious_bb_r's bit 6/7 swap of the colour byte must break the match.
        def unswapped(roms, bs0, bs1, want_tile):
            value = tr.bb_read(roms, bs0, bs1, want_tile)
            if want_tile:
                return value
            return (value & 0x3F) | ((value << 1) & 0x80) | ((value >> 1) & 0x40)

        cells = [[(unswapped(self.roms, r, c, False), unswapped(self.roms, r, c, True))
                  for c in range(128)] for r in range(256)]
        best = tr.best_snapshot_match(cells, self.snapshot)
        self.assertLess(len(best.map_rows), len(tr.EXPECTED_SNAPSHOT_MAP_ROWS))

    def test_committed_images_match_a_fresh_render_and_rendering_is_deterministic(self) -> None:
        map_png, filler_png, _ = tr.render_all(REFERENCE)
        again_map, again_filler, _ = tr.render_all(REFERENCE)
        self.assertEqual((map_png, filler_png), (again_map, again_filler))
        self.assertEqual(tr.decode_rgba(tr.MAP_PNG.read_bytes(), "committed"), tr.decode_rgba(map_png, "fresh"))
        self.assertEqual(tr.decode_rgba(tr.FILLER_PNG.read_bytes(), "committed"),
                         tr.decode_rgba(filler_png, "fresh"))
        self.assertEqual(0, tr.cmd_verify(REFERENCE))

    def test_a_changed_reference_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / tr.MAP_ROM
            target.parent.mkdir(parents=True)
            shutil.copyfile(REFERENCE / tr.MAP_ROM, target)
            with target.open("ab") as handle:
                handle.write(b"\n")
            with self.assertRaises(se.SpriteExtractionError):
                tr.read_reference(Path(tmp), tr.MAP_ROM)


if __name__ == "__main__":
    unittest.main()
