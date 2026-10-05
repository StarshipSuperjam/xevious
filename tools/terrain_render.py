#!/usr/bin/env python3
"""Render the Xevious terrain from the arcade's own map ROM and background tiles.

The arcade draws its scrolling ground from a 128-column x 256-row map of 8x8
background tiles. The map lives in three ROMs (2a/2b/2c) that the pinned reference
(``jotd666/xevious`` @ the commit below) transcribes in ``src/map_rom.68k`` and
decodes with ``xevious_bb_r`` (``src/xevious_sub.68k`` 1558-1624, a MAME
transcription), called twice per map cell by ``get_map_row`` (247-290): first for
the cell's colour (attribute) byte, then for its tile byte. The tiles and colours
are in ``assets/amiga/xevious_gfx.c``: ``bg_tile[512][64]`` (one 2-bit value per
pixel), ``bg_tile_clut[128][4]`` and ``palette[128][3]``.

Every area flies the whole length of this one map at its own sideways start
column, so one master image serves all sixteen areas. This tool renders it:

* ``assets/terrain/arcade_map.png`` -- 1024 x 2048, one pixel per arcade pixel.
  Map row R is at y = 8R (row 255, where every area starts, is at the bottom;
  rows enter the screen at its top as the area runs 255 -> 0). Map column c is at
  x = (127 - c) * 8, mirrored the way the reference places it on screen
  (``amiga.68k`` ``GET_XY_FROM_OFFSET`` 134-142: X = 32 - plane column), so an
  area's start column is the right-hand edge of what it shows.
* ``assets/terrain/forest_filler.png`` -- 224 x 512, the forest pattern
  ``fill_bg_with_forest`` (``xevious_main.68k`` 648-669) writes over the whole
  background plane at a (re)start. Its 28 columns are the visible plane columns
  (screen-left first); its 64 rows are laid on the map row grid, row r holding
  plane row (r + 3) mod 64 -- where ``get_map_row`` would write map row r.

A tile is drawn the way the reference's own renderer draws it (``amiga.68k``
1292-1390, and ``assets/amiga/bg_data_to_png.py``): attribute bit 0 is tile code
bit 8; the colour table is ``((attr & 0x3F) >> 2) | ((attr & 3) << 5)``, plus 0x10
when tile bit 7 is set; attribute bit 7 mirrors the tile, bit 6 flips it.

``verify`` re-renders both images and compares them, pixel for pixel, with the
committed files and their provenance, and checks the decode against the
reference's own background video-RAM snapshot (``assets/amiga/bg_data_scroll``):
a run of consecutive plane rows must match the decoded map exactly, both bytes,
across all 32 plane columns.

The module also derives, from the reference renderer's own terms, where the arcade
draws a map row and a ground object on screen (the "screen phase" constants), and
``calibrate`` draws an area's strip of the master map with each of its scheduled
ground objects boxed at that derived position -- a picture to check landmarks by eye.

``generate`` slices the committed master map and filler into the two terrain strips'
six costumes (four bands, the restart band, the filler), writes them to
``src/xevious/assets/`` and the strip targets' costume lists in ``project.json``, with
their provenance; ``check`` verifies all of that is current. Run it after
``tools/game_director.py generate``.

Usage:
    python tools/terrain_render.py render --checkout PATH
    python tools/terrain_render.py verify --checkout PATH
    python tools/terrain_render.py calibrate --area N [--out PATH]
    python tools/terrain_render.py generate
    python tools/terrain_render.py check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

# The tool lives in tools/; reuse the extractor's PNG signature/chunk layout and error type.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sprite_extractor import PNG_SIGNATURE, SpriteExtractionError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TERRAIN_DIR = ROOT / "assets" / "terrain"
MAP_PNG = TERRAIN_DIR / "arcade_map.png"
FILLER_PNG = TERRAIN_DIR / "forest_filler.png"
PROVENANCE = TERRAIN_DIR / "provenance.json"

PINNED_COMMIT = "71473685a8c7856c8401c8519276cd97a38d4183"
REFERENCE_REPO = "https://github.com/jotd666/xevious"

MAP_ROM = "src/map_rom.68k"
GFX_C = "assets/amiga/xevious_gfx.c"
SNAPSHOT = "assets/amiga/bg_data_scroll"

# SHA-256 of each reference file this tool reads, at the pinned commit. Rendering
# refuses to run against bytes that differ, so a moved pin or a tampered checkout
# cannot silently change the terrain.
EXPECTED_SHA256 = {
    MAP_ROM: "f96a17e75caa788589755bb39fb0097a17a51d957663a54885f97b7835a7d7d6",
    GFX_C: "3028308f85c742b1cf5569bb031ebb9c06df22943ddf3494ca5bda0c59cf66d4",
    SNAPSHOT: "4ac845012afa4c624afc3d93aff70c79471ccaa382fc455c55c5833be85a8cef",
}

# ROM sizes, from the map_rom.68k section comments ("xvi_9.2a ($1000 bytes)" etc).
ROM_SIZES = {"rom2a": 0x1000, "rom2b": 0x2000, "rom2c": 0x1000}

MAP_COLUMNS = 128
MAP_ROWS = 256
TILE = 8
MAP_WIDTH = MAP_COLUMNS * TILE    # 1024
MAP_HEIGHT = MAP_ROWS * TILE      # 2048

# The background plane: 32 columns x 64 rows, offset = plane column << 6 | plane row.
PLANE_COLUMNS = 32
PLANE_ROWS = 64
# Visible plane columns: amiga.68k keeps 0 <= 29 - plane column < 28 (1292-1301),
# i.e. plane columns 2..29, which get_map_row fills from map columns offset..offset+27.
VISIBLE_COLUMNS = 28
FIRST_VISIBLE_PLANE_COLUMN = 2
FILLER_WIDTH = VISIBLE_COLUMNS * TILE   # 224
FILLER_HEIGHT = PLANE_ROWS * TILE       # 512

# fill_bg_with_forest: colour byte 0 everywhere; tiles 0x88.. over the first 28
# plane offsets, then each later offset copies the tile 28 offsets before it.
FOREST_FIRST_TILE = 0x88
FOREST_PERIOD = 0x1C

# Strict snapshot rule: the longest run of consecutive plane rows that match the decode
# exactly (both bytes, all 32 plane columns) must be exactly this one. The snapshot was
# taken early in area 9 (start column 42, area_offset_in_map_tbl): plane rows 28..63
# and 0..1 hold map rows 217..254, each at plane row (R + 3) mod 64 as get_map_row
# writes it. The remaining plane rows 2..27 hold no map row: tile 0 / colour 0 outside
# plane column 0, whose rows 2..27 carry fill_bg_with_forest's first 28 tiles. (The
# source copies that pattern across the whole plane; the snapshot's port evidently stopped
# the copy early, so the filler follows the source code, not this snapshot.)
EXPECTED_SNAPSHOT_OFFSET = 42
EXPECTED_SNAPSHOT_FIRST_PLANE_ROW = 28
EXPECTED_SNAPSHOT_MAP_ROWS = tuple(range(217, 255))

# --- screen phase: where the arcade draws a map row and a ground object --------------------------
#
# In arcade display lines (line 0 = the top of the 288-line playfield, lines counting DOWN the screen)
# and display pixels across (the background plane's own x), from the terms the reference applies. C is
# the scroll counter (`scroll_cntr`), counting down 0x10 a frame, i.e. COUNTER_UNITS_PER_LINE a line.
#
# A map row. get_map_row writes map row R to plane row (R + PLANE_ROW_BIAS) & 63 (xevious_sub.68k 272);
# amiga.68k draws plane row r at plane line 8 * ((r + AMIGA_TILE_ROW_BIAS) & 63) + AMIGA_BG_LINE_BIAS
# (GET_XY_FROM_OFFSET 134-142, the -2 at 136; the bg_screen_data - 4 lines base at 1372); and the plane is
# shown from line ((C + SCROLL_REGISTER_BIAS) >> 5) & 0x1FF (sub_fn_30__handle_scroll 234-245: the 0x40 at
# 238, doubled at 240). So row R's top edge is on display line 8R - C/32 + TERRAIN_ROW_PHASE_LINES (mod the
# 512-line plane). The -14 at sub 260 (add.b #0xF2) only decides WHEN a row is written: as the counter's
# high byte first reaches R + 14, about fifteen rows above the top edge.
PLANE_ROW_BIAS = 3            # xevious_sub.68k 272 (addq.w #3)
AMIGA_TILE_ROW_BIAS = -2      # amiga.68k 136 (subq.w #2)
AMIGA_BG_LINE_BIAS = -4       # amiga.68k 1372 (bg_screen_data - NB_BYTES_PER_ROW * 4)
SCROLL_REGISTER_BIAS = 0x80   # xevious_sub.68k 238-240 (0x40, doubled)
COUNTER_UNITS_PER_LINE = 32   # xevious_sub.68k 242 (lsr.w #5)
TERRAIN_ROW_PHASE_LINES = (
    TILE * (PLANE_ROW_BIAS + AMIGA_TILE_ROW_BIAS) + AMIGA_BG_LINE_BIAS
    - SCROLL_REGISTER_BIAS // COUNTER_UNITS_PER_LINE
)
assert TERRAIN_ROW_PHASE_LINES == 0, TERRAIN_ROW_PHASE_LINES

# A ground object. osd_update_sprite_shadow (amiga.68k 1651-1698) takes _X >> 5 as the sprite's line and
# 256 - (_Y >> 5) as its x, and the draw takes 32 lines (1865) and 8 px (2624) off them for the 16 x 16
# cell's top-left corner. So its centre is on line _X/32 + GROUND_CENTRE_LINE_BIAS, at background x
# GROUND_CENTRE_PX_BIAS - _Y/32.
#
# Not applied: the Amiga's 2-px shift of its foreground playfield (bplcon1, amiga.68k 984). Its comment
# says it lines the foreground TILES up with the background tiles ("can be seen in the title screen");
# the sprites ride along only because the Amiga draws them into that playfield. The arcade's own data says
# sprites line up with the background without it: Namco's purpose-built two-dome clearings (four of them,
# shared by areas 1, 6 and 15) centre their scheduled domes to the pixel, both ways, with the shift left
# out, and two pixels off with it in (tests/test_terrain_render.py pins this).
AMIGA_SPRITE_LINE_BIAS = -32  # amiga.68k 1865 (sub.w #4*8)
AMIGA_SPRITE_PX_BIAS = -8     # amiga.68k 2624 (subq.w #8)
AMIGA_SPRITE_MIRROR = 256     # amiga.68k 1697-1698 (neg.w; add.w #32*8)
SPRITE_CELL = 16
GROUND_CENTRE_LINE_BIAS = AMIGA_SPRITE_LINE_BIAS + SPRITE_CELL // 2                     # -24
GROUND_CENTRE_PX_BIAS = AMIGA_SPRITE_MIRROR + AMIGA_SPRITE_PX_BIAS + SPRITE_CELL // 2   # 256

# Which map row a ground object sits on. sub_fn_2__handle_objects (xevious_sub.68k 574-599) fires a record
# when the counter's high byte equals its row S, and sub_2_fn_1__ground_object (673) leaves _X = 0; the
# object then moves down with the scroll (_X +0x10 a frame as C -0x10). The port fires on its first tick at
# row S (C = 256S + 224) and scrolls the new object in that same tick, so from then on _X = 256(S + 1) - C:
# the arcade's own _X to within half a line (one frame). Its centre line minus row R's top line is then
# 8(S + 1) + GROUND_CENTRE_LINE_BIAS - TERRAIN_ROW_PHASE_LINES - 8R, a constant: the object's centre is on
# the top edge of map row S + GROUND_OBJECT_ROW_OFFSET, so its 16 x 16 cell covers rows S - 3 and S - 2.
_CENTRE_LINES_FROM_ROW_S = TILE + GROUND_CENTRE_LINE_BIAS - TERRAIN_ROW_PHASE_LINES
assert _CENTRE_LINES_FROM_ROW_S % TILE == 0, _CENTRE_LINES_FROM_ROW_S
GROUND_OBJECT_ROW_OFFSET = _CENTRE_LINES_FROM_ROW_S // TILE
assert GROUND_OBJECT_ROW_OFFSET == -2, GROUND_OBJECT_ROW_OFFSET

# Across. get_map_row's i-th cell (column -2 + i, sub 264) holds map column offset - 1 + i (262) at plane
# column (column + 3) & 31, and amiga.68k draws plane column p at x = 8 * (32 - p) (GET_XY_FROM_OFFSET
# 138-140). So map column offset + j has its left edge at background x TERRAIN_COLUMN0_LEFT_PX - 8j.
TERRAIN_COLUMN0_LEFT_PX = TILE * (PLANE_COLUMNS - FIRST_VISIBLE_PLANE_COLUMN)  # 240

# The ground families the calibration image boxes as exact: single-slot objects that only ever move with
# the scroll (handle_1B_Derota, handle_1E_Barra, handle_1F_Zolbak, handle_26_Logram). Every other ground
# record is boxed at the same base position, which its family's own offsets or movement then move away from.
STATIC_GROUND_TYPES = (0x1B, 0x1E, 0x1F, 0x26)


def map_row_top_line(map_row: int, counter: int) -> int:
    """Display line of map row R's top edge at scroll counter C (mod the 2048-line map)."""
    return (TILE * map_row - counter // COUNTER_UNITS_PER_LINE + TERRAIN_ROW_PHASE_LINES) % MAP_HEIGHT


def ground_centre_line(slot_x: int) -> int:
    """Display line of a ground object's centre (slot x = the arcade's _X)."""
    return slot_x // COUNTER_UNITS_PER_LINE + GROUND_CENTRE_LINE_BIAS


def ground_centre_map_y(trigger_row: int, row_offset: int = GROUND_OBJECT_ROW_OFFSET) -> int:
    """Master-map y of a ground object's centre, fired at trigger row S (row R's top is at y = 8R)."""
    return (TILE * (trigger_row + row_offset)) % MAP_HEIGHT


def ground_centre_map_x(area_offset: int, sprite_y: int, px_bias: int = GROUND_CENTRE_PX_BIAS) -> int:
    """Master-map x of a ground object's centre (map column c spans x = (127 - c) * 8 .. + 8)."""
    background_x = px_bias - sprite_y
    return background_x - TERRAIN_COLUMN0_LEFT_PX + (MAP_COLUMNS - 1 - area_offset) * TILE


# --- the terrain strips: what the port shows of the map, from the clock ----------------------------------
#
# The arcade never holds the whole map on screen: get_map_row writes each map row into the 64-row plane about
# fourteen rows before it scrolls into view, with the area offset current at that moment, and a (re)start
# fills the plane with forest first. So what is on screen is a pure function of the clock and two columns:
# the area's own, and the one the rows still on screen were written with. The port shows it with two strip
# sprites over the map cut into four bands of 64 rows. A band is 512 lines, more than the 288-line screen, so
# at most two bands are on screen, and they are neighbours: one even (band 0 or 2), one odd (1 or 3). The even
# strip shows the even band, the odd strip the odd one.
#
# Which column a band was written with. The current area's rows are written from the completion of the
# area before it (row 255 at counter high byte 0x0D) to its own completion (row 0 at 0x0E, still with its
# own offset: the write comes before handle_next_area swaps it). So bands 1-3 on screen are always the
# current area's. Band 0 is on screen only early in an area (the previous area's last rows, scrolling out)
# or late (the current area's last rows, written from progress 49152), so it takes the previous column
# while progress is below half the counter span. After a re-top there is no previous area: everything not
# yet written is forest. That is all of band 0 until the area's end, so the even strip shows the forest
# filler in its place. It is also row 255, the first row above the re-top screen, because writing starts
# at row 254 (high byte 0x0D -> 0x0C). So after a re-top the odd strip's band 3 uses a restart costume
# whose row 255 is transparent, letting the filler strip behind it show through there.
#
# Seams. Each band costume carries two overlap rows above its top (the bottom of the band above it, from
# the same column), and the filler carries three: rows 61-63 of its pattern, i.e. map rows 253-255. The
# upper strip draws in front, so the lower strip's overlap is hidden behind it. Scratch fences a sprite that
# overlaps the stage by less than 15 units (scratch-render RenderWebGL.js FENCE_WIDTH, over the costume's
# whole box). So a strip shows only while at least TERRAIN_SHOW_LINES of its costume are on stage: 12 lines
# for the fence plus a 2-line margin. While an entering band is still hidden, the band below shows its
# overlap rows in the top <= 13 lines. Those are the same map rows unless the two bands were written with
# different columns. That happens for the first few lines of a new area's rows, which show the old column,
# and for rows 253-254 after a re-top, which show forest. It is a recorded Scratch necessity.
SCROLL_COUNTER_INIT = 0x0D00     # xevious_main.68k 471: the counter at game start and each new life
SCROLL_COUNTER_SPAN = 0x10000    # the 16-bit counter wraps; one area flies the whole map
MAP_LINES_PER_COUNTER_SPAN = SCROLL_COUNTER_SPAN // COUNTER_UNITS_PER_LINE
assert MAP_LINES_PER_COUNTER_SPAN == MAP_HEIGHT
SCREEN_LINES = 288
TERRAIN_BAND_ROWS = 64
TERRAIN_BANDS = MAP_ROWS // TERRAIN_BAND_ROWS          # 4
TERRAIN_BAND_LINES = TERRAIN_BAND_ROWS * TILE           # 512
assert SCREEN_LINES < TERRAIN_BAND_LINES and TERRAIN_BANDS % 2 == 0
BAND_OVERLAP_ROWS = 2
FILLER_OVERLAP_ROWS = 3
BAND_OVERLAP_LINES = BAND_OVERLAP_ROWS * TILE           # 16
FILLER_OVERLAP_LINES = FILLER_OVERLAP_ROWS * TILE       # 24
# Band 0 takes the previous area's column below this progress (see above: band 0 is never on screen from
# progress 12544 to 52480, and the current area writes it from 49152).
TERRAIN_PREVIOUS_BAND0_BELOW = SCROLL_COUNTER_SPAN // 2  # 32768
NO_PREVIOUS_COLUMN = -1  # after a re-top: band 0 is forest filler
# The stage map (game_director's PRES-01 render map): line 0 is the stage top, 1.25 units per arcade pixel,
# and the visible columns' centre (background x 136) is stage x 0.
STAGE_PER_PX = 1.25
STAGE_TOP = 180
STAGE_FENCE_UNITS = 15   # scratch-render RenderWebGL.js FENCE_WIDTH
TERRAIN_SHOW_MARGIN_LINES = 2
TERRAIN_SHOW_LINES = int(STAGE_FENCE_UNITS / STAGE_PER_PX) + TERRAIN_SHOW_MARGIN_LINES  # 14
VISIBLE_CENTRE_PX = TERRAIN_COLUMN0_LEFT_PX + TILE - VISIBLE_COLUMNS * TILE // 2      # 136
# A band costume is the map's full width at one costume px per arcade px, rotation centre at its band top
# (x = MAP_WIDTH / 2). Its column `area offset` lands at background x TERRAIN_COLUMN0_LEFT_PX, so the sprite
# x for area offset col is BAND_X_PER_COLUMN * col + BAND_X_AT_COLUMN0.
BAND_X_PER_COLUMN = int(STAGE_PER_PX * TILE)  # 10
BAND_X_AT_COLUMN0 = int(STAGE_PER_PX * (
    MAP_WIDTH // 2 - TILE * (MAP_COLUMNS - 1) + TERRAIN_COLUMN0_LEFT_PX - VISIBLE_CENTRE_PX
))  # -500
# The filler costume is the 28 visible columns, fixed to the screen: its left edge at background x 24.
FILLER_LEFT_PX = TERRAIN_COLUMN0_LEFT_PX - TILE * (VISIBLE_COLUMNS - 1)
FILLER_X = int(STAGE_PER_PX * (FILLER_LEFT_PX + FILLER_WIDTH // 2 - VISIBLE_CENTRE_PX))  # 0
assert (BAND_X_PER_COLUMN, BAND_X_AT_COLUMN0, FILLER_X) == (10, -500, 0)

BAND_COSTUMES = ("terrain band 0", "terrain band 1", "terrain band 2", "terrain band 3")
RESTART_COSTUME = "terrain band 3 restart"
FILLER_COSTUME = "terrain filler"
EVEN_COSTUMES = (BAND_COSTUMES[0], BAND_COSTUMES[2], FILLER_COSTUME)
ODD_COSTUMES = (BAND_COSTUMES[1], BAND_COSTUMES[3], RESTART_COSTUME)


@dataclass(frozen=True)
class StripState:
    costume: str
    x: int
    y: float
    shown: bool
    top_line: int  # display line of the band top (row 64b, or filler row 0); the costume's rotation centre


@dataclass(frozen=True)
class TerrainState:
    even: StripState
    odd: StripState
    even_behind: bool  # the even strip is the lower one, so it draws behind the odd strip


def terrain_line(progress: int) -> int:
    """The scroll counter in lines (0..2047): map row R's top edge is on display line 8R - this."""
    return ((SCROLL_COUNTER_INIT - progress) % SCROLL_COUNTER_SPAN) // COUNTER_UNITS_PER_LINE


def _strip(parity: int, progress: int, column: int, previous: int) -> StripState:
    c = terrain_line(progress)
    # The top line of the nearest band of this parity, in [-512, 512): the only one that can be on screen.
    top = (TERRAIN_BAND_LINES * parity - c + TERRAIN_BAND_LINES) % (2 * TERRAIN_BAND_LINES) - TERRAIN_BAND_LINES
    band = (top + c) % MAP_HEIGHT // TERRAIN_BAND_LINES
    overlap = BAND_OVERLAP_LINES
    if band == 0:
        band_column = previous if progress < TERRAIN_PREVIOUS_BAND0_BELOW else column
        if band_column == NO_PREVIOUS_COLUMN:
            costume, x, overlap = FILLER_COSTUME, FILLER_X, FILLER_OVERLAP_LINES
        else:
            costume, x = BAND_COSTUMES[0], BAND_X_PER_COLUMN * band_column + BAND_X_AT_COLUMN0
    else:
        costume = RESTART_COSTUME if band == 3 and previous == NO_PREVIOUS_COLUMN else BAND_COSTUMES[band]
        x = BAND_X_PER_COLUMN * column + BAND_X_AT_COLUMN0
    shown = (
        top >= TERRAIN_SHOW_LINES - TERRAIN_BAND_LINES
        and top <= SCREEN_LINES - TERRAIN_SHOW_LINES + overlap
    )
    return StripState(costume, x, STAGE_TOP - STAGE_PER_PX * top, shown, top)


def terrain_state(progress: int, column: int, previous: int) -> TerrainState:
    """What the two terrain strips show at a clock value: `column` is the area's map start column (offset),
    `previous` the column the outgoing rows were written with (NO_PREVIOUS_COLUMN after a re-top)."""
    even = _strip(0, progress, column, previous)
    odd = _strip(1, progress, column, previous)
    return TerrainState(even, odd, even.top_line > odd.top_line)


@dataclass(frozen=True)
class PadFit:
    pad: tuple[int, int, int, int]       # x0, x1, y0, y1 (inclusive) in the master map
    objects: tuple[tuple[int, int, int], ...]  # (area, trigger row, type)
    dx: float                            # object group centre minus pad centre
    dy: float


def designed_pad_fits(
    map_raw: bytes,
    schedules: list[dict],
    offsets: list[int],
    *,
    px_bias: int = GROUND_CENTRE_PX_BIAS,
    row_offset: int = GROUND_OBJECT_ROW_OFFSET,
    min_objects: int = 4,
) -> list[PadFit]:
    """How centred the static ground objects sit on the map's purpose-built clearings.

    Each static object's pad is the one-colour region under its centre (4-connected, within 40 px). Pads
    that hold at least `min_objects` scheduled objects, across all areas, are the designed ones; for each,
    the offset of the objects' combined 16 x 16 cells from the pad's centre. A correct screen phase puts
    every designed pad's objects dead centre."""
    def colour(x: int, y: int) -> bytes:
        at = ((y % MAP_HEIGHT) * MAP_WIDTH + x) * 4
        return map_raw[at:at + 3]

    pads: dict[tuple[int, int, int, int], list[tuple[int, int, int, int, int]]] = {}
    for area in schedules:
        offset = offsets[area["area"] - 1]
        for record in area["records"]:
            if record["handler"] != "add_ground_object" or record["object_type"] not in STATIC_GROUND_TYPES:
                continue
            cx = ground_centre_map_x(offset, record["params"]["sprite_y"], px_bias)
            cy = ground_centre_map_y(record["scroll_row"], row_offset)
            if not 3 <= cx < MAP_WIDTH - 3:
                continue
            window = [(cx + dx, cy + dy) for dx in range(-3, 4) for dy in range(-3, 4)]
            around = [colour(x, y) for x, y in window]
            pad_colour = max(sorted(set(around)), key=around.count)
            seen: set[tuple[int, int]] = set()
            stack = [(x, y) for x, y in window if colour(x, y) == pad_colour]
            xs: list[int] = []
            ys: list[int] = []
            while stack:
                x, y = stack.pop()
                if (x, y) in seen or abs(x - cx) > 40 or abs(y - cy) > 40 or not 0 <= x < MAP_WIDTH:
                    continue
                seen.add((x, y))
                if colour(x, y) != pad_colour:
                    continue
                xs.append(x)
                ys.append(y)
                stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
            key = (min(xs), max(xs), min(ys), max(ys))
            pads.setdefault(key, []).append((area["area"], record["scroll_row"], record["object_type"], cx, cy))
    fits = []
    half = SPRITE_CELL // 2
    for (x0, x1, y0, y1), objects in sorted(pads.items()):
        if len(objects) < min_objects:
            continue
        ox0 = min(o[3] for o in objects) - half
        ox1 = max(o[3] for o in objects) + half
        oy0 = min(o[4] for o in objects) - half
        oy1 = max(o[4] for o in objects) + half
        fits.append(PadFit(
            pad=(x0, x1, y0, y1),
            objects=tuple(o[:3] for o in objects),
            dx=((ox0 + ox1) - (x0 + x1 + 1)) / 2,
            dy=((oy0 + oy1) - (y0 + y1 + 1)) / 2,
        ))
    return fits


@dataclass(frozen=True)
class Roms:
    rom2a: bytes
    rom2b: bytes
    rom2c: bytes


@dataclass(frozen=True)
class Gfx:
    bg_tile: list[list[int]]
    bg_tile_clut: list[list[int]]
    palette: list[list[int]]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_reference(checkout: Path, rel: str) -> bytes:
    path = checkout / rel
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SpriteExtractionError(f"cannot read reference file {rel}: {exc}") from exc
    actual = _sha256(data)
    expected = EXPECTED_SHA256[rel]
    if actual != expected:
        raise SpriteExtractionError(
            f"reference file {rel} hash changed: expected {expected}, got {actual}. "
            f"Is the checkout at commit {PINNED_COMMIT}?"
        )
    return data


def parse_map_roms(text: str) -> Roms:
    """Collect each ``romNN:`` label's ``.byte`` lines from map_rom.68k."""
    roms: dict[str, bytearray] = {}
    current: str | None = None
    for line in text.splitlines():
        label = re.match(r"^(rom2[abc]):\s*$", line)
        if label:
            current = label.group(1)
            roms[current] = bytearray()
            continue
        stripped = line.strip()
        if current is not None and stripped.startswith(".byte"):
            roms[current].extend(int(tok, 16) for tok in re.findall(r"0x([0-9A-Fa-f]{2})", stripped))
    for name, size in ROM_SIZES.items():
        if len(roms.get(name, b"")) != size:
            raise SpriteExtractionError(
                f"{MAP_ROM}: {name} has {len(roms.get(name, b''))} bytes, expected {size:#x}"
            )
    return Roms(bytes(roms["rom2a"]), bytes(roms["rom2b"]), bytes(roms["rom2c"]))


def _extract_c_array(text: str, name: str, rows: int, cols: int) -> list[list[int]]:
    """Parse a ``name[...][cols] = { ... }`` array of hex bytes by brace-walking."""
    match = re.search(re.escape(name) + r"\[[^\]]*\]\[" + str(cols) + r"\]\s*=", text)
    if match is None:
        raise SpriteExtractionError(f"array {name}[][{cols}] not found in {GFX_C}")
    start = text.index("{", match.end())
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
        index += 1
    body = re.sub(r"//[^\n]*", "", text[start:index + 1])
    numbers = [int(token, 16) for token in re.findall(r"0x([0-9a-fA-F]+)", body)]
    if len(numbers) != rows * cols:
        raise SpriteExtractionError(
            f"array {name}: expected {rows * cols} bytes, parsed {len(numbers)}"
        )
    return [numbers[r * cols:(r + 1) * cols] for r in range(rows)]


def parse_gfx(text: str) -> Gfx:
    return Gfx(
        bg_tile=_extract_c_array(text, "bg_tile", 512, 64),
        bg_tile_clut=_extract_c_array(text, "bg_tile_clut", 128, 4),
        palette=_extract_c_array(text, "palette", 128, 3),
    )


def load(checkout: Path) -> tuple[Roms, Gfx]:
    roms = parse_map_roms(read_reference(checkout, MAP_ROM).decode("latin-1"))
    gfx = parse_gfx(read_reference(checkout, GFX_C).decode("latin-1"))
    return roms, gfx


# --- the map decode (xevious_bb_r, xevious_sub.68k 1558-1624) -------------------------------

def bb_read(roms: Roms, bs0: int, bs1: int, want_tile: bool) -> int:
    """One xevious_bb_r call: bs0 = map row, bs1 = map column; colour byte or tile byte."""
    bs0 &= 0xFF
    bs1 &= 0xFF
    adr_2b = ((bs1 & 0x7E) << 6) | (bs0 >> 1)
    d4 = roms.rom2a[adr_2b >> 1]
    if adr_2b & 1:
        d4 = (d4 & 0xF0) << 4
    else:
        d4 = (d4 & 0x0F) << 8
    dat1 = d4 | roms.rom2b[adr_2b]
    adr_2c = ((dat1 & 0x1FF) << 2) | ((bs1 & 1) << 1) | (bs0 & 1)
    if dat1 & 0x400:
        adr_2c ^= 1
    if dat1 & 0x200:
        adr_2c ^= 2
    if want_tile:
        return roms.rom2c[adr_2c | 0x800]
    dat2 = roms.rom2c[adr_2c]
    attr = (dat2 & 0x3F) | ((dat2 << 1) & 0x80) | ((dat2 >> 1) & 0x40)
    if dat1 & 0x400:
        attr ^= 0x40
    if dat1 & 0x200:
        attr ^= 0x80
    return attr


def decode_map(roms: Roms) -> list[list[tuple[int, int]]]:
    """cells[row][column] = (colour byte, tile byte) for the whole 128 x 256 map."""
    return [
        [(bb_read(roms, row, col, False), bb_read(roms, row, col, True)) for col in range(MAP_COLUMNS)]
        for row in range(MAP_ROWS)
    ]


# --- the plane layout get_map_row writes (xevious_sub.68k 247-290) --------------------------

def plane_offset(map_row: int, column_index: int) -> int:
    """Plane offset get_map_row writes for the column_index-th (0..31) cell of a row."""
    d0 = (0xFE00 + 0x100 * column_index) & 0xFFFF | (map_row & 0xFF)
    low = (d0 + 3) & 0x3F
    high = (((d0 + 0x300) & 0xFFFF) >> 2) & 0x07C0
    return high | low


def map_column_for(area_offset: int, column_index: int) -> int:
    """Map column of get_map_row's column_index-th cell: the routine starts at offset - 1."""
    return (area_offset - 1 + column_index) & 0xFF


# --- tile drawing ------------------------------------------------------------------------

def tile_code(attr: int, tile: int) -> int:
    return tile | ((attr & 1) << 8)


def tile_clut(attr: int, tile: int) -> int:
    clut = ((attr & 0x3F) >> 2) | ((attr & 3) << 5)
    if tile & 0x80:
        clut |= 0x10
    return clut


def tile_rows(gfx: Gfx, attr: int, tile: int) -> list[bytes]:
    """The 8 RGBA rows of one background cell, flipped as its colour byte says."""
    data = gfx.bg_tile[tile_code(attr, tile)]
    entries = gfx.bg_tile_clut[tile_clut(attr, tile)]
    colours = [bytes(gfx.palette[entries[v]]) + b"\xff" for v in range(4)]
    rows = [b"".join(colours[data[y * TILE + x]] for x in range(TILE)) for y in range(TILE)]
    if attr & 0x80:  # X-flip: mirror each row
        rows = [b"".join(row[x * 4:x * 4 + 4] for x in reversed(range(TILE))) for row in rows]
    if attr & 0x40:  # Y-flip
        rows.reverse()
    return rows


class _TileCache:
    def __init__(self, gfx: Gfx) -> None:
        self.gfx = gfx
        self.cache: dict[tuple[int, int], list[bytes]] = {}

    def rows(self, attr: int, tile: int) -> list[bytes]:
        key = (attr, tile)
        found = self.cache.get(key)
        if found is None:
            found = self.cache[key] = tile_rows(self.gfx, attr, tile)
        return found


def forest_tile(plane_column: int, plane_row: int) -> int:
    """fill_bg_with_forest's tile at a plane cell (its colour byte is 0)."""
    return FOREST_FIRST_TILE + ((plane_column * PLANE_ROWS + plane_row) % FOREST_PERIOD)


def render_map(cells: list[list[tuple[int, int]]], gfx: Gfx) -> bytes:
    """Raw RGBA rows (no PNG filter bytes) of the 1024 x 2048 master map."""
    cache = _TileCache(gfx)
    out = bytearray()
    for row in range(MAP_ROWS):
        line_tiles = [cache.rows(*cells[row][col]) for col in reversed(range(MAP_COLUMNS))]
        for y in range(TILE):
            for rows in line_tiles:
                out += rows[y]
    return bytes(out)


def render_filler(gfx: Gfx) -> bytes:
    """Raw RGBA rows of the 224 x 512 forest filler, on the map row grid."""
    cache = _TileCache(gfx)
    out = bytearray()
    visible = range(FIRST_VISIBLE_PLANE_COLUMN, FIRST_VISIBLE_PLANE_COLUMN + VISIBLE_COLUMNS)
    for r in range(PLANE_ROWS):
        plane_row = (r + 3) % PLANE_ROWS
        # Screen-left first: X = 32 - plane column, so the highest visible plane column is leftmost.
        line_tiles = [cache.rows(0, forest_tile(pc, plane_row)) for pc in reversed(visible)]
        for y in range(TILE):
            for rows in line_tiles:
                out += rows[y]
    return bytes(out)


# --- PNG (the repo's fixed encoder layout, written straight from raw rows) ---------------

def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload)) + kind + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_rgba(width: int, height: int, raw: bytes) -> bytes:
    """Byte-identical to sprite_extractor.encode_png (filter 0 rows, zlib level 9), but from raw rows."""
    if len(raw) != width * height * 4:
        raise SpriteExtractionError("raw RGBA size does not match its dimensions")
    stride = width * 4
    filtered = bytearray()
    for y in range(height):
        filtered.append(0)
        filtered += raw[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(filtered), level=9))
        + _chunk(b"IEND", b"")
    )


def decode_rgba(data: bytes, label: str) -> tuple[int, int, bytes]:
    """Decode a PNG written by encode_rgba back to (width, height, raw rows)."""
    if not data.startswith(PNG_SIGNATURE):
        raise SpriteExtractionError(f"{label} has no PNG signature")
    position = len(PNG_SIGNATURE)
    header = None
    idat = bytearray()
    while position < len(data):
        length = struct.unpack_from(">I", data, position)[0]
        kind = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + length]
        position += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            idat += payload
        elif kind == b"IEND":
            break
    if header is None or header[2:] != (8, 6, 0, 0, 0):
        raise SpriteExtractionError(f"{label} is not an 8-bit RGBA PNG from this tool")
    width, height = header[0], header[1]
    filtered = zlib.decompress(bytes(idat))
    stride = width * 4
    if len(filtered) != height * (stride + 1):
        raise SpriteExtractionError(f"{label} has the wrong amount of image data")
    raw = bytearray()
    for y in range(height):
        start = y * (stride + 1)
        if filtered[start] != 0:
            raise SpriteExtractionError(f"{label} row {y} uses a PNG filter this tool never writes")
        raw += filtered[start + 1:start + 1 + stride]
    return width, height, bytes(raw)


# --- the snapshot check (assets/amiga/bg_data_scroll) -----------------------------------

@dataclass(frozen=True)
class SnapshotMatch:
    area_offset: int
    first_plane_row: int
    map_rows: tuple[int, ...]


def snapshot_runs(cells: list[list[tuple[int, int]]], snapshot: bytes) -> list[SnapshotMatch]:
    """Every maximal run of consecutive plane rows that equals a decoded map row, exactly.

    The snapshot is 0x800 colour bytes then 0x800 tile bytes, indexed by plane offset.
    For a candidate area offset, plane row p holds map row R with (R + 3) % 64 == p
    (four candidates); it matches when all 32 cells get_map_row writes for R equal the
    snapshot. Runs must step R by -1 per plane row going up the screen (+1 going down).
    """
    if len(snapshot) != 0x1000:
        raise SpriteExtractionError(f"{SNAPSHOT} is {len(snapshot)} bytes, expected 0x1000")
    colour, tiles = snapshot[:0x800], snapshot[0x800:]
    matches: list[SnapshotMatch] = []
    for area_offset in range(MAP_COLUMNS):
        # Which map row (if any) each plane row holds exactly, at this area offset.
        held: list[int | None] = []
        for plane_row in range(PLANE_ROWS):
            found = None
            for k in range(MAP_ROWS // PLANE_ROWS):
                map_row = (plane_row - 3) % PLANE_ROWS + PLANE_ROWS * k
                if all(
                    (colour[plane_offset(map_row, i)], tiles[plane_offset(map_row, i)])
                    == cells[map_row][map_column_for(area_offset, i) & 0x7F]
                    for i in range(PLANE_COLUMNS)
                ):
                    found = map_row
                    break
            held.append(found)

        def continues(plane_row: int) -> bool:
            # Plane row p continues the run above it when p-1 holds map row R-1 (the plane is cyclic).
            above = held[(plane_row - 1) % PLANE_ROWS]
            return (held[plane_row] is not None and above is not None
                    and above == (held[plane_row] - 1) % MAP_ROWS)

        for start in range(PLANE_ROWS):
            if held[start] is None or continues(start):
                continue
            run = [held[start]]
            plane_row = (start + 1) % PLANE_ROWS
            while plane_row != start and continues(plane_row):
                run.append(held[plane_row])
                plane_row = (plane_row + 1) % PLANE_ROWS
            matches.append(SnapshotMatch(area_offset, start, tuple(run)))
    return matches


def best_snapshot_match(cells: list[list[tuple[int, int]]], snapshot: bytes) -> SnapshotMatch:
    runs = snapshot_runs(cells, snapshot)
    if not runs:
        raise SpriteExtractionError(f"no plane row of {SNAPSHOT} matches the decoded map")
    return max(runs, key=lambda m: (len(m.map_rows), -m.area_offset, -m.first_plane_row))


# --- provenance ---------------------------------------------------------------------------

def _ordered_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def provenance_entries(map_png: bytes, filler_png: bytes) -> dict:
    reference = {
        "repository": REFERENCE_REPO,
        "commit": PINNED_COMMIT,
        "inputs": {MAP_ROM: EXPECTED_SHA256[MAP_ROM], GFX_C: EXPECTED_SHA256[GFX_C]},
    }
    license_note = (
        "Arcade map and tile data of Namco's Xevious (1983), transcribed in the pinned "
        "reference; third-party copyrighted material, same class as the arcade sprites "
        "the project ships; rights review before broader distribution"
    )
    return {
        "arcade_map.png": {
            "sha256": _sha256(map_png),
            "dimensions": [MAP_WIDTH, MAP_HEIGHT],
            "generator": "tools/terrain_render.py render",
            "description": (
                "The whole 128 x 256 background map, one pixel per arcade pixel: map row R "
                "at y = 8R, map column c at x = (127 - c) * 8 (mirrored as on screen)"
            ),
            "reference": reference,
            "license": license_note,
        },
        "forest_filler.png": {
            "sha256": _sha256(filler_png),
            "dimensions": [FILLER_WIDTH, FILLER_HEIGHT],
            "generator": "tools/terrain_render.py render",
            "description": (
                "fill_bg_with_forest's pattern over the 28 visible plane columns "
                "(screen-left first); row r holds plane row (r + 3) mod 64"
            ),
            "reference": reference,
            "license": license_note,
        },
    }


PROVENANCE_NOTE = (
    "Terrain source art. arcade_map.png and forest_filler.png are rendered by "
    "tools/terrain_render.py from the pinned arcade reference's map ROM and background "
    "tiles (see 'rendered'); `verify` re-derives them. xevious_area_map.png, the fan map, "
    "is kept as a visual cross-check only and is not read by the build."
)


def updated_provenance(current: dict, map_png: bytes, filler_png: bytes) -> dict:
    result = dict(current)
    result["note"] = PROVENANCE_NOTE
    result["rendered"] = provenance_entries(map_png, filler_png)
    return result


# --- commands -----------------------------------------------------------------------------

def render_all(checkout: Path) -> tuple[bytes, bytes, list[list[tuple[int, int]]]]:
    roms, gfx = load(checkout)
    cells = decode_map(roms)
    map_png = encode_rgba(MAP_WIDTH, MAP_HEIGHT, render_map(cells, gfx))
    filler_png = encode_rgba(FILLER_WIDTH, FILLER_HEIGHT, render_filler(gfx))
    return map_png, filler_png, cells


def cmd_render(checkout: Path) -> int:
    map_png, filler_png, _ = render_all(checkout)
    TERRAIN_DIR.mkdir(parents=True, exist_ok=True)
    MAP_PNG.write_bytes(map_png)
    FILLER_PNG.write_bytes(filler_png)
    current = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    PROVENANCE.write_bytes(_ordered_json_bytes(updated_provenance(current, map_png, filler_png)))
    print(f"wrote {MAP_PNG.relative_to(ROOT)} ({MAP_WIDTH}x{MAP_HEIGHT}, sha256 {_sha256(map_png)})")
    print(f"wrote {FILLER_PNG.relative_to(ROOT)} ({FILLER_WIDTH}x{FILLER_HEIGHT}, sha256 {_sha256(filler_png)})")
    return 0


def cmd_verify(checkout: Path) -> int:
    map_png, filler_png, cells = render_all(checkout)
    failures: list[str] = []
    for path, fresh in ((MAP_PNG, map_png), (FILLER_PNG, filler_png)):
        if not path.exists():
            failures.append(f"{path.relative_to(ROOT)} is missing")
            continue
        if decode_rgba(path.read_bytes(), path.name) != decode_rgba(fresh, "fresh render"):
            failures.append(f"{path.relative_to(ROOT)} differs from a fresh render at the pin")
    recorded = json.loads(PROVENANCE.read_text(encoding="utf-8")).get("rendered", {})
    for name, path in (("arcade_map.png", MAP_PNG), ("forest_filler.png", FILLER_PNG)):
        if path.exists() and recorded.get(name, {}).get("sha256") != _sha256(path.read_bytes()):
            failures.append(f"provenance sha256 for {name} does not match the committed file")
    match = best_snapshot_match(cells, read_reference(checkout, SNAPSHOT))
    expected = SnapshotMatch(EXPECTED_SNAPSHOT_OFFSET, EXPECTED_SNAPSHOT_FIRST_PLANE_ROW,
                             EXPECTED_SNAPSHOT_MAP_ROWS)
    if match != expected:
        failures.append(
            f"decode's best match to {SNAPSHOT} is offset {match.area_offset}, plane row "
            f"{match.first_plane_row}, {len(match.map_rows)} rows; expected offset "
            f"{expected.area_offset}, plane row {expected.first_plane_row}, map rows "
            f"{expected.map_rows[0]}..{expected.map_rows[-1]}"
        )
    if failures:
        for failure in failures:
            print(f"MISMATCH: {failure}", file=sys.stderr)
        return 1
    print(
        f"OK: terrain renders match the pin; the decode reproduces {len(match.map_rows)} consecutive "
        f"plane rows of {SNAPSHOT} (area offset {match.area_offset}, map rows "
        f"{match.map_rows[0]}..{match.map_rows[-1]})"
    )
    return 0


# --- calibration image -----------------------------------------------------------------------

SCHEDULES_JSON = ROOT / "docs" / "spec" / "data" / "area-schedules.json"
TERRAIN_JSON = ROOT / "docs" / "spec" / "data" / "terrain.json"
CALIBRATION_DIR = ROOT / "dist" / "terrain-calibration"
CALIBRATION_MARGIN = 32  # px of map shown each side of the 28 visible columns, dimmed
GROUND_HANDLERS = ("add_ground_object", "add_domogram_with_path")
STATIC_BOX = (255, 255, 0)
OTHER_BOX = (255, 0, 255)


@dataclass(frozen=True)
class CalibrationBox:
    trigger_row: int
    object_type: int
    sprite_y: int
    centre_x: int  # in the calibration image
    centre_y: int
    static: bool


def area_offsets() -> list[int]:
    return list(json.loads(TERRAIN_JSON.read_text(encoding="utf-8"))["area_offset_in_map_tbl"]["values"])


def calibration_boxes(area: int) -> list[CalibrationBox]:
    """Every scheduled ground object of an area, at its derived centre in that area's calibration image."""
    offset = area_offsets()[area - 1]
    left = (MAP_COLUMNS - VISIBLE_COLUMNS - offset) * TILE - CALIBRATION_MARGIN
    schedules = json.loads(SCHEDULES_JSON.read_text(encoding="utf-8"))["areas"]
    (entry,) = [a for a in schedules if a["area"] == area]
    boxes = []
    for record in entry["records"]:
        if record["handler"] not in GROUND_HANDLERS:
            continue
        sprite_y = record["params"]["sprite_y"]
        boxes.append(CalibrationBox(
            trigger_row=record["scroll_row"],
            object_type=record["object_type"],
            sprite_y=sprite_y,
            centre_x=ground_centre_map_x(offset, sprite_y) - left,
            centre_y=ground_centre_map_y(record["scroll_row"]),
            static=record["object_type"] in STATIC_GROUND_TYPES,
        ))
    return boxes


def render_calibration(area: int, map_png: bytes) -> bytes:
    """The area's strip of the master map (its 28 visible columns plus a dimmed margin each side), with
    each scheduled ground object's 16 x 16 cell outlined at its derived position: yellow for the static
    single-slot families, magenta for the rest (base position only)."""
    width, height, raw = decode_rgba(map_png, "arcade_map.png")
    if (width, height) != (MAP_WIDTH, MAP_HEIGHT):
        raise SpriteExtractionError("arcade_map.png is not the 1024 x 2048 master map")
    offset = area_offsets()[area - 1]
    left = (MAP_COLUMNS - VISIBLE_COLUMNS - offset) * TILE - CALIBRATION_MARGIN
    out_width = VISIBLE_COLUMNS * TILE + 2 * CALIBRATION_MARGIN
    image = bytearray(out_width * height * 4)
    for y in range(height):
        for x in range(out_width):
            source_x = left + x
            at = (y * out_width + x) * 4
            if not 0 <= source_x < width:
                image[at:at + 4] = b"\x00\x00\x00\xff"
                continue
            pixel = raw[(y * width + source_x) * 4:(y * width + source_x) * 4 + 4]
            if not CALIBRATION_MARGIN <= x < out_width - CALIBRATION_MARGIN:
                pixel = bytes((pixel[0] // 3, pixel[1] // 3, pixel[2] // 3, 255))
            image[at:at + 4] = pixel
    half = SPRITE_CELL // 2
    for box in calibration_boxes(area):
        colour = bytes(STATIC_BOX if box.static else OTHER_BOX) + b"\xff"
        for d in range(SPRITE_CELL):
            for x, y in (
                (box.centre_x - half + d, box.centre_y - half),
                (box.centre_x - half + d, box.centre_y + half - 1),
                (box.centre_x - half, box.centre_y - half + d),
                (box.centre_x + half - 1, box.centre_y - half + d),
            ):
                if 0 <= x < out_width:
                    at = ((y % height) * out_width + x) * 4
                    image[at:at + 4] = colour
    return encode_rgba(out_width, height, bytes(image))


def cmd_calibrate(area: int, out: Path | None) -> int:
    if not 1 <= area <= 16:
        raise SpriteExtractionError(f"area must be 1..16, not {area}")
    path = out or CALIBRATION_DIR / f"area-{area:02d}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_calibration(area, MAP_PNG.read_bytes()))
    boxes = calibration_boxes(area)
    print(
        f"wrote {path} (area {area}, start column {area_offsets()[area - 1]}): "
        f"{sum(b.static for b in boxes)} static and {sum(not b.static for b in boxes)} other ground objects, "
        f"each centred on the top edge of map row S{GROUND_OBJECT_ROW_OFFSET:+d}"
    )
    return 0


# --- the strip costumes (generate / check) ----------------------------------------------------
#
# Ownership (the bezel's split): tools/game_director.py owns the two strip targets' existence, blocks, size and
# position; this module owns their costumes, the overlay provenance records for them, and the "costumes" part of
# assets/terrain/provenance.json. The costumes are sliced from the committed master map and filler, at one
# costume px per arcade px (bitmapResolution 1); the strips' size of 125 makes that the 1.25 render scale.

ASSET_DIR = ROOT / "src" / "xevious" / "assets"
OVERLAY_PROVENANCE = ASSET_DIR / "provenance.json"
PROJECT_PATH = ROOT / "src" / "xevious" / "project.json"
STRIP_TARGETS = {"even": "area_01a", "odd": "area_01b"}
COSTUME_GENERATOR_VERSION = 1
LICENSE = "No reusable license specified by source; third-party copyrighted material"


@dataclass(frozen=True)
class StripCostume:
    name: str
    source: str        # file under assets/terrain/
    rows: str          # which map rows it holds, for the provenance note
    png: bytes
    width: int
    height: int
    centre: tuple[int, int]

    @property
    def filename(self) -> str:
        return hashlib.md5(self.png).hexdigest() + ".png"


def _rows(raw: bytes, width: int, ys: list[int]) -> bytearray:
    stride = width * 4
    out = bytearray()
    for y in ys:
        out += raw[y * stride:(y + 1) * stride]
    return out


def strip_costumes(map_png: bytes, filler_png: bytes) -> dict[str, StripCostume]:
    """The six strip costumes, by name: a band is its 64 map rows with the two rows above it on top (the band
    top, the rotation centre, is costume row 16); the restart band is band 3 with map row 255 transparent; the
    filler is the 64-row forest pattern with its last three rows on top (rotation centre row 24)."""
    width, height, raw = decode_rgba(map_png, MAP_PNG.name)
    if (width, height) != (MAP_WIDTH, MAP_HEIGHT):
        raise SpriteExtractionError(f"{MAP_PNG.name} is not the {MAP_WIDTH} x {MAP_HEIGHT} master map")
    band_height = TERRAIN_BAND_LINES + BAND_OVERLAP_LINES
    costumes: dict[str, StripCostume] = {}
    for band, name in enumerate(BAND_COSTUMES):
        first = TERRAIN_BAND_ROWS * band
        ys = [(TERRAIN_BAND_LINES * band + k) % MAP_HEIGHT for k in range(-BAND_OVERLAP_LINES, TERRAIN_BAND_LINES)]
        pixels = _rows(raw, width, ys)
        overlap = f"{(first - BAND_OVERLAP_ROWS) % MAP_ROWS}..{(first - 1) % MAP_ROWS}"
        costumes[name] = StripCostume(
            name, MAP_PNG.name, f"map rows {first}..{first + TERRAIN_BAND_ROWS - 1}, overlap rows {overlap} on top",
            encode_rgba(width, band_height, bytes(pixels)), width, band_height, (width // 2, BAND_OVERLAP_LINES),
        )
        if band == TERRAIN_BANDS - 1:
            # Map row 255 is the band's last TILE costume rows: a re-top never writes it (see the model above).
            pixels[(band_height - TILE) * width * 4:] = bytes(TILE * width * 4)
            costumes[RESTART_COSTUME] = StripCostume(
                RESTART_COSTUME, MAP_PNG.name,
                f"map rows {first}..{MAP_ROWS - 2} (row {MAP_ROWS - 1} transparent), overlap rows {overlap} on top",
                encode_rgba(width, band_height, bytes(pixels)), width, band_height, (width // 2, BAND_OVERLAP_LINES),
            )
    f_width, f_height, f_raw = decode_rgba(filler_png, FILLER_PNG.name)
    if (f_width, f_height) != (FILLER_WIDTH, FILLER_HEIGHT):
        raise SpriteExtractionError(f"{FILLER_PNG.name} is not the {FILLER_WIDTH} x {FILLER_HEIGHT} filler")
    filler_height = FILLER_HEIGHT + FILLER_OVERLAP_LINES
    ys = [(k % FILLER_HEIGHT) for k in range(-FILLER_OVERLAP_LINES, FILLER_HEIGHT)]
    costumes[FILLER_COSTUME] = StripCostume(
        FILLER_COSTUME, FILLER_PNG.name,
        f"pattern rows 0..{PLANE_ROWS - 1}, rows {PLANE_ROWS - FILLER_OVERLAP_ROWS}..{PLANE_ROWS - 1} again on top",
        encode_rgba(f_width, filler_height, bytes(_rows(f_raw, f_width, ys))), f_width, filler_height,
        (f_width // 2, FILLER_OVERLAP_LINES),
    )
    return costumes


def _costume_record(costume: StripCostume) -> dict:
    return {
        "name": costume.name,
        "bitmapResolution": 1,
        "dataFormat": "png",
        "assetId": costume.filename.removesuffix(".png"),
        "md5ext": costume.filename,
        "rotationCenterX": costume.centre[0],
        "rotationCenterY": costume.centre[1],
    }


def expected_strip_project(project: dict, costumes: dict[str, StripCostume]) -> dict:
    """The project with each strip target's costumes set to its three, resting on its cold-start costume."""
    result = json.loads(json.dumps(project))
    cold_start = terrain_state(0, 0, NO_PREVIOUS_COLUMN)
    for parity, names, rest in (
        ("even", EVEN_COSTUMES, cold_start.even.costume), ("odd", ODD_COSTUMES, cold_start.odd.costume),
    ):
        target = next((t for t in result["targets"] if t.get("name") == STRIP_TARGETS[parity]), None)
        if target is None:
            raise SpriteExtractionError(
                f"Scratch project has no {STRIP_TARGETS[parity]} target; run tools/game_director.py generate first"
            )
        target["costumes"] = [_costume_record(costumes[name]) for name in names]
        target["currentCostume"] = names.index(rest)
    return result


def _overlay_record(costume: StripCostume, source_sha256: str) -> dict:
    return {
        "origin": (
            f"Terrain strip costume '{costume.name}' (AREA-01) sliced by tools/terrain_render.py generate from "
            f"assets/terrain/{costume.source}, which tools/terrain_render.py render decodes from the pinned arcade "
            f"reference {REFERENCE_REPO} @{PINNED_COMMIT} ({MAP_ROM}, {GFX_C})"
        ),
        "license": LICENSE,
        "notes": (
            "Credit: Namco (arcade map ROM and background tiles); transcribed in the pinned reference by jotd666. "
            "The repository operator did not create this asset. "
            f"Source assets/terrain/{costume.source} at SHA-256 {source_sha256}; {costume.rows}; "
            f"{costume.width}x{costume.height}, bitmapResolution 1, rotation centre {list(costume.centre)}."
        ),
    }


def _expected_costume_state() -> tuple[dict[str, StripCostume], bytes, bytes, bytes, set[str]]:
    map_png, filler_png = MAP_PNG.read_bytes(), FILLER_PNG.read_bytes()
    costumes = strip_costumes(map_png, filler_png)
    terrain_provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    prior = set(terrain_provenance.get("costumes", {}).get("outputs", {}))
    project = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))
    project_bytes = _ordered_json_bytes(expected_strip_project(project, costumes))
    overlay = json.loads(OVERLAY_PROVENANCE.read_text(encoding="utf-8"))
    if overlay.get("version") != 1 or not isinstance(overlay.get("assets"), dict):
        raise SpriteExtractionError("overlay provenance must use version 1")
    assets = {name: record for name, record in overlay["assets"].items() if name not in prior}
    source_sha = {MAP_PNG.name: _sha256(map_png), FILLER_PNG.name: _sha256(filler_png)}
    for costume in costumes.values():
        assets[costume.filename] = _overlay_record(costume, source_sha[costume.source])
    overlay_bytes = _ordered_json_bytes({"version": 1, "assets": dict(sorted(assets.items()))})
    terrain_provenance["costumes"] = {
        "generator": "tools/terrain_render.py generate",
        "generator_version": COSTUME_GENERATOR_VERSION,
        "sources": source_sha,
        "outputs": {
            costume.filename: {"name": costume.name, "target": STRIP_TARGETS[
                "even" if costume.name in EVEN_COSTUMES else "odd"
            ]}
            for costume in costumes.values()
        },
    }
    return costumes, project_bytes, overlay_bytes, _ordered_json_bytes(terrain_provenance), prior


def _require_bytes(path: Path, expected: bytes) -> None:
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise SpriteExtractionError(f"missing generated output {path}") from exc
    if actual != expected:
        raise SpriteExtractionError(f"generated output is stale; run terrain_render.py generate: {path}")


def check_costumes() -> int:
    costumes, project_bytes, overlay_bytes, provenance_bytes, prior = _expected_costume_state()
    current = {costume.filename for costume in costumes.values()}
    stale = prior - current
    if stale:
        raise SpriteExtractionError("stale generated terrain costumes: " + ", ".join(sorted(stale)))
    for costume in costumes.values():
        _require_bytes(ASSET_DIR / costume.filename, costume.png)
    _require_bytes(PROJECT_PATH, project_bytes)
    _require_bytes(OVERLAY_PROVENANCE, overlay_bytes)
    _require_bytes(PROVENANCE, provenance_bytes)
    return len(costumes)


def cmd_generate() -> int:
    costumes, project_bytes, overlay_bytes, provenance_bytes, prior = _expected_costume_state()
    current = {costume.filename for costume in costumes.values()}
    for stale in sorted(prior - current):
        path = ASSET_DIR / stale
        if path.is_file() and not path.is_symlink():
            path.unlink()
    for costume in costumes.values():
        (ASSET_DIR / costume.filename).write_bytes(costume.png)
    PROJECT_PATH.write_bytes(project_bytes)
    OVERLAY_PROVENANCE.write_bytes(overlay_bytes)
    PROVENANCE.write_bytes(provenance_bytes)
    print(f"generated and verified {check_costumes()} terrain strip costumes")
    return 0


def cmd_check() -> int:
    print(f"verified {check_costumes()} terrain strip costumes")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--checkout", required=True, type=Path,
                             help="path to the pinned jotd666/xevious checkout")
    calibrate = sub.add_parser("calibrate")
    calibrate.add_argument("--area", required=True, type=int, help="area number, 1..16")
    calibrate.add_argument("--out", type=Path, help="output PNG (default dist/terrain-calibration/area-NN.png)")
    sub.add_parser("generate")
    sub.add_parser("check")
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            return cmd_render(args.checkout)
        if args.command == "calibrate":
            return cmd_calibrate(args.area, args.out)
        if args.command == "generate":
            return cmd_generate()
        if args.command == "check":
            return cmd_check()
        return cmd_verify(args.checkout)
    except SpriteExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
