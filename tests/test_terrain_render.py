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
from unittest import mock

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


class ScreenPhaseTests(unittest.TestCase):
    """AREA-01 landmark alignment: where the arcade draws a map row and a ground object."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.map_png = tr.MAP_PNG.read_bytes()
        _, _, cls.map_raw = tr.decode_rgba(cls.map_png, "arcade map")
        cls.schedules = json.loads(tr.SCHEDULES_JSON.read_text(encoding="utf-8"))["areas"]
        cls.offsets = tr.area_offsets()

    def test_phase_terms_derive_the_constants(self) -> None:
        # Map row R's top: 8 * ((R + 3) - 2) - 4 - (C + 0x80) / 32 = 8R - C/32 exactly.
        self.assertEqual(0, tr.TERRAIN_ROW_PHASE_LINES)
        # A ground sprite's centre: _X/32 - 32 + 8; across, 256 - _Y/32 - 8 + 8 (no foreground shift).
        self.assertEqual(-24, tr.GROUND_CENTRE_LINE_BIAS)
        self.assertEqual(256, tr.GROUND_CENTRE_PX_BIAS)
        self.assertEqual(-2, tr.GROUND_OBJECT_ROW_OFFSET)
        self.assertEqual(240, tr.TERRAIN_COLUMN0_LEFT_PX)
        # The 28 visible columns span background x 24..247 and a centred sprite (sprite_y 128) is at 128:
        # both centred on the playfield's 224-px width, 8 px in from x 16 (the stage map's x = 1.25u - 170).
        self.assertEqual(24, tr.TERRAIN_COLUMN0_LEFT_PX - tr.TILE * (tr.VISIBLE_COLUMNS - 1))
        self.assertEqual(136, (24 + 248) // 2)

    def test_a_port_spawned_object_rides_on_map_row_s_minus_2(self) -> None:
        # The port fires a ground record on its first tick at row S (counter 256S + 224) and scrolls the new
        # object that tick, so after t more ticks slot x = 32(t + 1) and the counter is 256S + 224 - 32t.
        for trigger_row in (255, 214, 120, 13, 1):
            for t in range(0, 360):
                counter = (256 * trigger_row + 224 - 32 * t) % 65536
                slot_x = 32 * (t + 1)
                self.assertEqual((256 * (trigger_row + 1)) % 65536, (slot_x + counter) % 65536)
                centre = tr.ground_centre_line(slot_x) % tr.MAP_HEIGHT
                self.assertEqual(tr.map_row_top_line(trigger_row - 2, counter), centre, (trigger_row, t))
                self.assertNotEqual(tr.map_row_top_line(trigger_row - 1, counter), centre)

    def test_designed_clearings_centre_their_ground_objects(self) -> None:
        # roadmap-evidence: AREA-01 success  (the derived screen phase puts every scheduled static ground
        #   object dead centre on the map's purpose-built two-dome clearings, both ways, to the pixel)
        fits = tr.designed_pad_fits(self.map_raw, self.schedules, self.offsets)
        self.assertGreaterEqual(len(fits), 4)
        self.assertGreaterEqual(sum(len(f.objects) for f in fits), 20)
        self.assertEqual({1, 6, 15}, {o[0] for f in fits for o in f.objects})
        for fit in fits:
            self.assertEqual((0.0, 0.0), (fit.dx, fit.dy), fit)

    def test_a_shifted_phase_misses_the_clearings(self) -> None:
        # roadmap-evidence: AREA-01 failure  (the Amiga's 2-px foreground shift, or a one-row phase slip,
        #   puts the objects off centre on every designed clearing)
        shifted = tr.designed_pad_fits(self.map_raw, self.schedules, self.offsets,
                                       px_bias=tr.GROUND_CENTRE_PX_BIAS + 2)
        self.assertTrue(shifted)
        self.assertTrue(all(f.dx == 2.0 for f in shifted), shifted)
        slipped = tr.designed_pad_fits(self.map_raw, self.schedules, self.offsets,
                                       row_offset=tr.GROUND_OBJECT_ROW_OFFSET + 1)
        self.assertTrue(slipped)
        self.assertTrue(all(f.dy != 0.0 for f in slipped), slipped)

    def test_calibration_image_boxes_each_object_at_its_derived_cell(self) -> None:
        image = tr.render_calibration(1, self.map_png)
        self.assertEqual(image, tr.render_calibration(1, self.map_png))
        width, height, raw = tr.decode_rgba(image, "calibration")
        self.assertEqual((224 + 2 * tr.CALIBRATION_MARGIN, 2048), (width, height))
        boxes = tr.calibration_boxes(1)
        self.assertEqual(25, len(boxes))
        barra = next(b for b in boxes if b.trigger_row == 214 and b.object_type == 0x1E)
        self.assertTrue(barra.static)
        # Area 1 starts at column 36: the image's x 0 is master x (128 - 28 - 36) * 8 - 32 = 480.
        self.assertEqual(tr.ground_centre_map_x(36, 128) - 480, barra.centre_x)
        self.assertEqual(8 * 212, barra.centre_y)
        corner = ((barra.centre_y - 8) * width + barra.centre_x - 8) * 4
        self.assertEqual(bytes(tr.STATIC_BOX) + b"\xff", raw[corner:corner + 4])
        inside = (barra.centre_y * width + barra.centre_x) * 4
        source = (barra.centre_y * tr.MAP_WIDTH + barra.centre_x + 480) * 4
        self.assertEqual(self.map_raw[source:source + 4], raw[inside:inside + 4])


FOREST = "forest"


class ArcadePlane:
    """The arcade's background plane, written the way the reference writes it -- an oracle that shares
    nothing with terrain_state but the screen phase.

    get_map_row (xevious_sub.68k 247-290) writes map row (high byte - 14) & 0xFF into plane row
    (row + 3) & 63 each time the counter's high byte drops, with the area offset current then; a re-top
    (main_gameplay_loop xevious_main.68k 466-490) fills the plane with forest (fill_bg_with_forest
    648-669) and sets the counter to 0x0D00; handle_next_area (xevious_sub.68k 696-730) swaps the offset
    after that tick's write. The port's clock does the same: completion at high byte 0x0E with progress
    above 0, carrying the counter. It mirrors the port's `terrain column` / `previous terrain column`."""

    def __init__(self, offsets: list[int], area: int) -> None:
        self.offsets = offsets
        self.retop(area)

    def retop(self, area: int) -> None:
        self.area = area
        self.progress = 0
        self.plane: list[tuple[int, int] | None] = [None] * tr.PLANE_ROWS
        self.column = self.offsets[area - 1]
        self.previous = tr.NO_PREVIOUS_COLUMN

    def counter(self) -> int:
        return (0x0D00 - self.progress) % 0x10000

    def tick(self) -> bool:
        before = self.counter() >> 8
        self.progress += 32
        high = self.counter() >> 8
        if high != before:
            row = (high - 14) & 0xFF
            self.plane[(row + 3) & 63] = (row, self.offsets[self.area - 1])
        if high == 14 and self.progress > 0:
            self.area = 7 if self.area == 16 else self.area + 1
            self.progress -= 0x10000
            self.previous, self.column = self.column, self.offsets[self.area - 1]
            return True
        return False

    def screen(self) -> list[object]:
        c = self.counter() // 32
        out: list[object] = []
        for line in range(tr.SCREEN_LINES):
            row = (line + c) // 8 % 256
            cell = self.plane[(row + 3) & 63]
            assert cell is None or cell[0] == row, (line, row, cell)  # never a stale row on screen
            out.append(FOREST if cell is None else cell)
        return out


def _strip_content(strip: tr.StripState, line: int) -> object:
    """What one strip draws at a display line (None: nothing, or a transparent pixel)."""
    rel = line - strip.top_line
    if strip.costume == tr.FILLER_COSTUME:
        return FOREST if -tr.FILLER_OVERLAP_LINES <= rel < tr.TERRAIN_BAND_LINES else None
    if not -tr.BAND_OVERLAP_LINES <= rel < tr.TERRAIN_BAND_LINES:
        return None
    band = 3 if strip.costume == tr.RESTART_COSTUME else tr.BAND_COSTUMES.index(strip.costume)
    row = (tr.TERRAIN_BAND_ROWS * band + rel // 8) % 256
    if strip.costume == tr.RESTART_COSTUME and row == 255:
        return None
    return (row, (strip.x - tr.BAND_X_AT_COLUMN0) // tr.BAND_X_PER_COLUMN)


def _model_screen(state: tr.TerrainState) -> list[object]:
    order = (state.odd, state.even) if state.even_behind else (state.even, state.odd)
    out: list[object] = []
    for line in range(tr.SCREEN_LINES):
        content = None
        for strip in order:
            if strip.shown:
                content = _strip_content(strip, line)
                if content is not None:
                    break
        out.append(content)
    return out


class TerrainStateTests(unittest.TestCase):
    """AREA-01: the two terrain strips show what the arcade's plane shows, tick for tick."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.offsets = tr.area_offsets()
        cls.report = cls._scenarios()

    @staticmethod
    def _check(plane: ArcadePlane, report: dict) -> None:
        state = tr.terrain_state(plane.progress, plane.column, plane.previous)
        report["states"].append(state)
        for line, (want, got) in enumerate(zip(plane.screen(), _model_screen(state))):
            if want != got:
                report["residual"].append((plane.area, plane.progress, line, want, got))

    @classmethod
    def _run(cls, plane: ArcadePlane, ticks: int, report: dict) -> None:
        cls._check(plane, report)
        for _ in range(ticks):
            report["changes"] += plane.tick()
            cls._check(plane, report)

    @classmethod
    def _scenarios(cls) -> dict:
        report: dict = {"changes": 0, "states": [], "residual": []}
        # A cold start through two area changes (1 -> 2 -> 3).
        plane = ArcadePlane(cls.offsets, 1)
        cls._run(plane, 2 * 2048 + 300, report)
        # The 16 -> 7 loop, from a re-top in area 16.
        plane.retop(16)
        cls._run(plane, 2048 + 300, report)
        # A new life mid-area (a re-top into the same area, 1000 ticks in) and on through its end.
        plane.retop(5)
        cls._run(plane, 1000, report)
        plane.retop(5)
        cls._run(plane, 2048 + 300, report)
        return report

    def test_strips_show_what_the_arcade_plane_shows(self) -> None:
        # roadmap-evidence: AREA-01 success  (an arcade-plane simulation -- rows written 14 ahead with the
        #   offset current then, forest after a re-top -- matches the strips line for line on every tick of a
        #   cold start, two area changes, the 16 -> 7 loop and a new life; only the recorded top <= 13 lines
        #   differ while an entering band is still hidden)
        report = self.report
        self.assertEqual(4, report["changes"])
        residual = report["residual"]
        self.assertTrue(all(line < tr.TERRAIN_SHOW_LINES - 1 for _a, _p, line, _w, _g in residual), residual[:5])
        # The residual is only the two recorded cases: the old column above a new area's first rows, and
        # forest for rows 253-254 after a re-top. Never a gap.
        self.assertTrue(all(g is not None for *_rest, g in residual))
        for _area, _progress, _line, want, got in residual:
            if got == FOREST:
                self.assertIn(want[0], (253, 254))
            else:
                self.assertEqual(want[0], got[0])
                self.assertNotEqual(want[1], got[1])
        self.assertTrue(any(g == FOREST for *_rest, g in residual))
        self.assertTrue(any(g != FOREST for *_rest, g in residual))

    def test_a_model_without_the_previous_column_or_restart_costume_disagrees(self) -> None:
        # roadmap-evidence: AREA-01 failure  (band 0 taking the new column at once, or a re-top's band 3
        #   drawing its unwritten row 255, shows the wrong ground well below the recorded top lines)
        for name, value in (("TERRAIN_PREVIOUS_BAND0_BELOW", 0), ("RESTART_COSTUME", tr.BAND_COSTUMES[3])):
            with self.subTest(name), mock.patch.object(tr, name, value):
                report = self._scenarios()
                self.assertTrue(
                    any(line >= tr.TERRAIN_SHOW_LINES for _a, _p, line, _w, _g in report["residual"])
                )

    def test_a_shown_strip_is_never_fenced(self) -> None:
        # scratch-render pushes a sprite back until its costume box overlaps the stage by 15 units; a strip
        # is shown only where it overlaps by more, and every area's column keeps it on stage across.
        states = self.report["states"]
        shown = 0
        for state in states:
            for strip in (state.even, state.odd):
                if not strip.shown:
                    continue
                shown += 1
                overlap = tr.FILLER_OVERLAP_LINES if strip.costume == tr.FILLER_COSTUME else tr.BAND_OVERLAP_LINES
                top = strip.y + tr.STAGE_PER_PX * overlap
                bottom = strip.y - tr.STAGE_PER_PX * tr.TERRAIN_BAND_LINES
                self.assertGreaterEqual(top, -(tr.STAGE_TOP - tr.STAGE_FENCE_UNITS) + 2, strip)
                self.assertLessEqual(bottom, tr.STAGE_TOP - tr.STAGE_FENCE_UNITS - 2, strip)
        self.assertGreater(shown, len(states))
        for column in set(self.offsets) | {0, tr.MAP_COLUMNS - tr.VISIBLE_COLUMNS}:
            x = tr.BAND_X_PER_COLUMN * column + tr.BAND_X_AT_COLUMN0
            half = tr.STAGE_PER_PX * tr.MAP_WIDTH / 2
            self.assertLessEqual(x - half, -140)
            self.assertGreaterEqual(x + half, 140)

    def test_band_and_filler_x_put_the_visible_columns_on_the_render_map(self) -> None:
        # Map column `offset` has its left edge at background x 240, stage x 1.25 * (240 - 136) = 130; the
        # band costume has column m at x (127 - m) * 8 about its centre 512.
        for column in set(self.offsets):
            x = tr.BAND_X_PER_COLUMN * column + tr.BAND_X_AT_COLUMN0
            for j in (0, 13, 27):
                costume_x = (tr.MAP_COLUMNS - 1 - (column + j)) * tr.TILE
                self.assertEqual(130 - 10 * j, x + tr.STAGE_PER_PX * (costume_x - tr.MAP_WIDTH // 2))
        # The filler's left edge is background x 24, stage -140.
        self.assertEqual(-140, tr.FILLER_X - tr.STAGE_PER_PX * tr.FILLER_WIDTH / 2)

    def test_restart_shows_forest_then_the_area_top(self) -> None:
        # At a re-top the screen is forest, all of it the even strip's filler; the odd strip (band 1, far
        # below) is hidden. The first map row to reach the screen is 254, through the restart costume.
        state = tr.terrain_state(0, 36, tr.NO_PREVIOUS_COLUMN)
        self.assertEqual((tr.FILLER_COSTUME, 0, True), (state.even.costume, state.even.x, state.even.shown))
        self.assertEqual(-104, state.even.top_line)
        self.assertFalse(state.odd.shown)
        late = tr.terrain_state(32 * 200, 36, tr.NO_PREVIOUS_COLUMN)
        self.assertEqual((tr.RESTART_COSTUME, -140, True), (late.odd.costume, late.odd.x, late.odd.shown))
        self.assertTrue(late.even_behind)
        # Mid-area the even strip is band 2 at the area's column; band 0 returns with it near the end.
        self.assertEqual(tr.BAND_COSTUMES[2], tr.terrain_state(32 * 1000, 36, -1).even.costume)
        end = tr.terrain_state(65056 - 32, 36, tr.NO_PREVIOUS_COLUMN)
        self.assertEqual((tr.BAND_COSTUMES[0], -140), (end.even.costume, end.even.x))


class StripCostumeTests(unittest.TestCase):
    """AREA-01: the six strip costumes sliced from the committed master map and filler (`generate` / `check`)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.map_png, cls.filler_png = tr.MAP_PNG.read_bytes(), tr.FILLER_PNG.read_bytes()
        cls.costumes = tr.strip_costumes(cls.map_png, cls.filler_png)
        _w, _h, cls.map_raw = tr.decode_rgba(cls.map_png, "map")
        _w, _h, cls.filler_raw = tr.decode_rgba(cls.filler_png, "filler")

    @staticmethod
    def _row(raw: bytes, width: int, y: int) -> bytes:
        return raw[y * width * 4:(y + 1) * width * 4]

    def _costume_raw(self, name: str) -> tuple[int, int, bytes]:
        return tr.decode_rgba(self.costumes[name].png, name)

    def test_committed_costumes_project_and_provenance_are_current(self) -> None:
        self.assertEqual(6, tr.check_costumes())

    def test_slicing_is_byte_deterministic(self) -> None:
        again = tr.strip_costumes(self.map_png, self.filler_png)
        self.assertEqual(
            {n: (c.filename, c.png) for n, c in self.costumes.items()},
            {n: (c.filename, c.png) for n, c in again.items()},
        )
        self.assertEqual(6, len({c.filename for c in self.costumes.values()}))

    def test_band_costumes_are_their_map_rows_with_the_overlap_on_top(self) -> None:
        # Band b is map pixel rows 512b-16 .. 512b+511 (mod 2048), its top edge at costume row 16 -- the
        # rotation centre the strip model's y puts on the band's top line.
        for band, name in enumerate(tr.BAND_COSTUMES):
            width, height, raw = self._costume_raw(name)
            self.assertEqual((1024, 528), (width, height))
            self.assertEqual((512, 16), self.costumes[name].centre)
            self.assertEqual(tr.BAND_OVERLAP_LINES, self.costumes[name].centre[1])
            for k in (0, 15, 16, 300, 527):
                want = self._row(self.map_raw, 1024, (512 * band + k - 16) % 2048)
                self.assertEqual(want, self._row(raw, width, k), (name, k))
        # Biting: band 0's overlap really wraps to the map's last rows (not rows 0..15 again).
        _w, _h, raw0 = self._costume_raw(tr.BAND_COSTUMES[0])
        self.assertNotEqual(self._row(self.map_raw, 1024, 0), self._row(raw0, 1024, 0))

    def test_restart_costume_is_band_3_without_row_255(self) -> None:
        width, height, band3 = self._costume_raw(tr.BAND_COSTUMES[3])
        _w, _h, restart = self._costume_raw(tr.RESTART_COSTUME)
        cut = (height - tr.TILE) * width * 4
        self.assertEqual(band3[:cut], restart[:cut])
        self.assertTrue(all(a == 0 for a in restart[cut + 3::4]))
        self.assertTrue(any(a != 0 for a in band3[cut + 3::4]))  # row 255 itself is drawn ground
        self.assertEqual(self.costumes[tr.BAND_COSTUMES[3]].centre, self.costumes[tr.RESTART_COSTUME].centre)

    def test_filler_costume_wraps_its_pattern_over_the_top(self) -> None:
        width, height, raw = self._costume_raw(tr.FILLER_COSTUME)
        self.assertEqual((224, 536), (width, height))
        self.assertEqual((112, tr.FILLER_OVERLAP_LINES), self.costumes[tr.FILLER_COSTUME].centre)
        for k in (0, 23, 24, 535):
            want = self._row(self.filler_raw, 224, (k - tr.FILLER_OVERLAP_LINES) % tr.FILLER_HEIGHT)
            self.assertEqual(want, self._row(raw, width, k), k)

    def test_each_strip_holds_its_parity_and_rests_on_its_cold_start_costume(self) -> None:
        project = json.loads(tr.PROJECT_PATH.read_text(encoding="utf-8"))
        targets = {t["name"]: t for t in project["targets"]}
        cold = tr.terrain_state(0, 0, tr.NO_PREVIOUS_COLUMN)
        for parity, names, rest in (
            ("even", tr.EVEN_COSTUMES, cold.even.costume), ("odd", tr.ODD_COSTUMES, cold.odd.costume),
        ):
            target = targets[tr.STRIP_TARGETS[parity]]
            self.assertEqual(list(names), [c["name"] for c in target["costumes"]])
            self.assertEqual({1}, {c["bitmapResolution"] for c in target["costumes"]})
            self.assertEqual(
                [self.costumes[n].filename for n in names], [c["md5ext"] for c in target["costumes"]]
            )
            self.assertEqual(rest, target["costumes"][target["currentCostume"]]["name"])
        self.assertEqual(tr.FILLER_COSTUME, cold.even.costume)

    def test_missing_strip_target_fails_loudly(self) -> None:
        with self.assertRaises(se.SpriteExtractionError):
            tr.expected_strip_project({"targets": []}, self.costumes)

    def test_provenance_credits_namco_and_pins_the_sources(self) -> None:
        overlay = json.loads(tr.OVERLAY_PROVENANCE.read_text(encoding="utf-8"))["assets"]
        terrain = json.loads(tr.PROVENANCE.read_text(encoding="utf-8"))["costumes"]
        self.assertEqual(
            {c.filename: {"name": c.name, "target": tr.STRIP_TARGETS["even" if c.name in tr.EVEN_COSTUMES else "odd"]}
             for c in self.costumes.values()},
            terrain["outputs"],
        )
        source_sha = {tr.MAP_PNG.name: se._sha256(self.map_png), tr.FILLER_PNG.name: se._sha256(self.filler_png)}
        self.assertEqual(source_sha, terrain["sources"])
        for costume in self.costumes.values():
            record = overlay[costume.filename]
            self.assertIn("No reusable license", record["license"])
            self.assertIn("Namco", record["notes"])
            self.assertIn(source_sha[costume.source], record["notes"])
            self.assertIn(tr.PINNED_COMMIT, record["origin"])


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
