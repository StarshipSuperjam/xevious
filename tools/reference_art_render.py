#!/usr/bin/env python3
"""Render the remaining enemy, shot and title-sparkle frames from the pinned arcade reference.

Slice 21 (``presentation.reference-art``; CAB-05, AIR-11) replaces the Spriters
Resource stand-ins that cannot show the arcade's colour steps, flips and frames
with the arcade's own sprites, decoded from the pinned reference's graphics data
(``assets/amiga/xevious_gfx.c``) with the effects sheet's decoder
(``tools/effects_sprite_render.py``). Every code below is a bank-1 tile
(``_ATTR`` bit 7; ``src/amiga/amiga.68k`` 1883-1886 adds 0x100). Line numbers
are ``src/xevious_main.68k`` unless named otherwise.

16x16 cells:

- Giddo Spario -- ``handle_08_Giddo_Spario`` 5219-5239: code
  ``0x100 + ((countup >> 1) & 3)`` at colour ``0x26 + ((countup >> 3) & 3)``,
  no flips. Its hit (``giddo_spario_hit`` 5241-5252) runs codes 0x104-0x107
  without rewriting the colour, so it keeps the last flight colour: both
  animations are rendered at all four colours, code-major.
- Brag Spario -- ``handle_09_Brag_Spario`` 3080-3121: code 0x115 at colour
  0x26, flipped by ``countup & 0x0C``; the flips are left to the extractor.
- Zakato, Brag Zakato and Garu Zakato bodies -- codes 0x111 (3749), 0x112
  (3877, 3903) and 0x113 (4014), each drawn at the pulsing colour
  (``colour_lut_pulsing_2``, ``src/xevious_sub.68k`` 208-232: 0x10-0x14). The
  Zakato body uses none of the pens those five colours change, and the Brag
  Zakato at 0x14 draws exactly that picture, so the sheet holds one Zakato body,
  the Brag Zakato at 0x10-0x13 and the Garu Zakato at all five; the build reuses
  the Zakato body for every Zakato colour and for the Brag Zakato's 0x14 (each
  costume must be a distinct picture).
- Zakato self-destruct -- ``zakato_exploding_sprite_tbl`` 3953-3959, codes
  0x104-0x108. ``zakato_explode`` (3931-3950) builds the size and flip bits but
  never stores them, so every frame is drawn 1x1 and unflipped, at the colour
  the body had when it fired: all five pulsing colours, code-major. A Zakato
  that fires on its first live frame never wrote the pulsing colour (each
  handler writes it after the fire test, e.g. 3757), so it keeps the teleport's
  0x24: those five frames sit on the teleport row, after the sparkle.
- the player's shot -- 2374-2388: code ``0x116 + ((countup >> 2) & 1)`` at
  colour ``0x23 + ((countup >> 1) & 1)``, code-major; and its rebound off a
  Bacura (``shot_destroyed`` 2400-2417), codes 0x118-0x11B at colour 0x23.
  Both flip ``_ATTR`` bit 3 every frame; the flips are left to the extractor.
- title sparkle -- ``attract_mode_title_screen`` 1217-1290: codes 0x130-0x13F
  at colour 0x0F, no flips; 0x130-0x137 as it appears and disappears (a code
  every 2 frames), 0x138-0x13F while it moves (a code every frame, 1255-1258).

32x32 cells: the Zakato teleport sparkle -- ``zakato_teleport_sprite_tbl``
3986-3992 at colour 0x24 (``init_teleport`` 3994-4002): 0x10C and 0x108 as
2x2 sprites, then 0x107, 0x106, 0x105 as 1x1, flipped by ``TIMER & 3``
(``zakato_teleport_sparkles`` 3969-3984 store the flip bits beside the size,
3978-3981; left to the extractor, which cuts the unflipped and x-flipped
frames: the build's slot clock steps 2 frames a tick, so it only draws the
even timer values, whose flip bits are none and x). Every frame is centred in its cell: a 1x1
frame sits at (8, 8) and a 2x2 frame fills the cell. A 2x2 sprite extends 16 px
right and down from the same position (``sprite_draw_double_width_and_height``,
``src/amiga/amiga.68k`` 2529-2544), so its centre sits 8 px right and down of a
1x1's; the build places the two 2x2 frames 8 px right and down
(``DOUBLE_TILE_STAGE_OFFSET``) until the timer-8 move, which cancels it.
The 2x2 frames are flipped as a whole, as the Neo Geo renderer does
(``src/neogeo/neogeo.68k`` 952-961: the sub-tile codes swapped, the block left
in place). The Amiga renderer drops the flip bits on a 2x2 (``move.w #3,d2``,
``src/amiga/amiga.68k`` 2530), so its teleport frames never flip; the game
logic sets them, so the build keeps them.

A colour-table entry of 0x80 is beyond the 128-colour palette and draws nothing,
so it is rendered as matte, like pixel value 0.

Output is byte-deterministic (the repo's own encoder), so an auditor can
re-derive the committed sheet from a fresh clone at the pin.

Usage:
    python tools/reference_art_render.py --checkout PATH --out src/xevious/assets/NAME.png
    python tools/reference_art_render.py --checkout PATH --verify
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from andor_sprite_render import (  # noqa: E402
    ARMOR_SUBTILE_ORIGINS,
    ASSET_DIR,
    GFX_C,
    MANIFEST_PATH,
    MATTE,
    TILE,
    _Gfx,
    _read_reference,
    _sha256,
)
from effects_sprite_render import (  # noqa: E402
    BANK_1,
    DOUBLE_CODE_MASK,
    ONE_BY_ONE_ORIGIN,
    TWO_BY_TWO_ORIGIN,
    _tile,
)
from sprite_extractor import Image, SpriteExtractionError, decode_png, encode_png  # noqa: E402

SHEET_NAME = "reference_art"

GIDDO_FLY_CODES = [BANK_1 + code for code in range(0x00, 0x04)]
GIDDO_HIT_CODES = [BANK_1 + code for code in range(0x04, 0x08)]
GIDDO_CLUTS = [0x26, 0x27, 0x28, 0x29]
BRAG_SPARIO_CODE = BANK_1 + 0x15
BRAG_SPARIO_CLUT = 0x26
ZAKATO_BODY_CODE = BANK_1 + 0x11
BRAG_ZAKATO_BODY_CODE = BANK_1 + 0x12
GARU_ZAKATO_BODY_CODE = BANK_1 + 0x13
PULSING_CLUTS = [0x10, 0x11, 0x12, 0x13, 0x14]   # colour_lut_pulsing_2's five distinct colours
# The Zakato body is the same picture at every pulsing colour, and the Brag Zakato's 0x14 is that
# picture too: one Zakato cell (at the first colour) and the Brag Zakato's four other colours.
ZAKATO_BODY_CLUT = PULSING_CLUTS[0]
BRAG_ZAKATO_BODY_CLUTS = PULSING_CLUTS[:4]
SELF_DESTRUCT_CODES = [BANK_1 + code for code in range(0x04, 0x09)]
SHOT_CODES = [BANK_1 + 0x16, BANK_1 + 0x17]
SHOT_CLUTS = [0x23, 0x24]
REBOUND_CODES = [BANK_1 + code for code in range(0x18, 0x1C)]
REBOUND_CLUT = 0x23
SPARKLE_CODES = [BANK_1 + code for code in range(0x30, 0x40)]
SPARKLE_CLUT = 0x0F
# (code, is_2x2) per teleport frame, in table order.
TELEPORT = [(BANK_1 + 0x0C, True), (BANK_1 + 0x08, True), (BANK_1 + 0x07, False),
            (BANK_1 + 0x06, False), (BANK_1 + 0x05, False)]
TELEPORT_CLUT = 0x24
# init_teleport's colour, kept by a Zakato that fires before it ever writes the pulsing colour.
SELF_DESTRUCT_TELEPORT_CLUT = TELEPORT_CLUT

SMALL_CELL = 16
TELEPORT_CELL = 32
SHEET_COLUMNS = len(SELF_DESTRUCT_CODES) * len(PULSING_CLUTS)   # the widest row: 25 cells
SHEET_WIDTH = SMALL_CELL * SHEET_COLUMNS
# Row origins (must match the manifest reference_art rects).
GIDDO_FLY_ROW_Y = 0
GIDDO_HIT_ROW_Y = SMALL_CELL
BODY_ROW_Y = SMALL_CELL * 2         # Zakato, Brag Zakato x4, Garu Zakato x5
SELF_DESTRUCT_ROW_Y = SMALL_CELL * 3
SHOT_ROW_Y = SMALL_CELL * 4        # Brag Spario, the shot (2 codes x 2 colours), its rebound
SPARKLE_ROW_Y = SMALL_CELL * 5
TELEPORT_ROW_Y = SMALL_CELL * 6   # the teleport sparkle, then the self-destruct at 0x24
SHEET_HEIGHT = TELEPORT_ROW_Y + TELEPORT_CELL


def small_cell_origins(count: int, row_y: int, first: int = 0) -> list[tuple[int, int]]:
    return [(SMALL_CELL * (first + index), row_y) for index in range(count)]


GIDDO_FLY_ORIGINS = small_cell_origins(len(GIDDO_FLY_CODES) * len(GIDDO_CLUTS), GIDDO_FLY_ROW_Y)
GIDDO_HIT_ORIGINS = small_cell_origins(len(GIDDO_HIT_CODES) * len(GIDDO_CLUTS), GIDDO_HIT_ROW_Y)
ZAKATO_BODY_ORIGIN = small_cell_origins(1, BODY_ROW_Y)[0]
BRAG_ZAKATO_BODY_ORIGINS = small_cell_origins(len(BRAG_ZAKATO_BODY_CLUTS), BODY_ROW_Y, first=1)
GARU_ZAKATO_BODY_ORIGINS = small_cell_origins(
    len(PULSING_CLUTS), BODY_ROW_Y, first=1 + len(BRAG_ZAKATO_BODY_CLUTS)
)
SELF_DESTRUCT_ORIGINS = small_cell_origins(
    len(SELF_DESTRUCT_CODES) * len(PULSING_CLUTS), SELF_DESTRUCT_ROW_Y
)
BRAG_SPARIO_ORIGIN = small_cell_origins(1, SHOT_ROW_Y)[0]
SHOT_ORIGINS = small_cell_origins(len(SHOT_CODES) * len(SHOT_CLUTS), SHOT_ROW_Y, first=1)
REBOUND_ORIGINS = small_cell_origins(len(REBOUND_CODES), SHOT_ROW_Y, first=1 + len(SHOT_ORIGINS))
SPARKLE_ORIGINS = small_cell_origins(len(SPARKLE_CODES), SPARKLE_ROW_Y)
TELEPORT_ORIGINS = [(TELEPORT_CELL * index, TELEPORT_ROW_Y) for index in range(len(TELEPORT))]
SELF_DESTRUCT_TELEPORT_ORIGINS = small_cell_origins(
    len(SELF_DESTRUCT_CODES), TELEPORT_ROW_Y, first=TELEPORT_CELL * len(TELEPORT) // SMALL_CELL
)


def code_major(codes: list[int], cluts: list[int]) -> list[tuple[int, int]]:
    return [(code, clut) for code in codes for clut in cluts]


def render_sheet(checkout: Path) -> Image:
    gfx = _Gfx(_read_reference(checkout, GFX_C))
    pixels = [MATTE + (255,)] * (SHEET_WIDTH * SHEET_HEIGHT)

    def blit(tile: list[tuple[int, int, int, int]], ox: int, oy: int) -> None:
        for ty in range(TILE):
            for tx in range(TILE):
                px = tile[ty * TILE + tx]
                if px[3] == 0:
                    continue  # leave matte
                pixels[(oy + ty) * SHEET_WIDTH + (ox + tx)] = px

    def cells(pairs: list[tuple[int, int]], origins: list[tuple[int, int]]) -> None:
        if len(pairs) != len(origins):
            raise SpriteExtractionError(f"{len(pairs)} cells for {len(origins)} origins")
        for (code, clut), origin in zip(pairs, origins):
            blit(_tile(gfx, code, clut), *origin)

    cells(code_major(GIDDO_FLY_CODES, GIDDO_CLUTS), GIDDO_FLY_ORIGINS)
    cells(code_major(GIDDO_HIT_CODES, GIDDO_CLUTS), GIDDO_HIT_ORIGINS)
    cells([(ZAKATO_BODY_CODE, ZAKATO_BODY_CLUT)], [ZAKATO_BODY_ORIGIN])
    cells(code_major([BRAG_ZAKATO_BODY_CODE], BRAG_ZAKATO_BODY_CLUTS), BRAG_ZAKATO_BODY_ORIGINS)
    cells(code_major([GARU_ZAKATO_BODY_CODE], PULSING_CLUTS), GARU_ZAKATO_BODY_ORIGINS)
    cells(code_major(SELF_DESTRUCT_CODES, PULSING_CLUTS), SELF_DESTRUCT_ORIGINS)
    cells([(BRAG_SPARIO_CODE, BRAG_SPARIO_CLUT)], [BRAG_SPARIO_ORIGIN])
    cells(code_major(SHOT_CODES, SHOT_CLUTS), SHOT_ORIGINS)
    cells([(code, REBOUND_CLUT) for code in REBOUND_CODES], REBOUND_ORIGINS)
    cells([(code, SPARKLE_CLUT) for code in SPARKLE_CODES], SPARKLE_ORIGINS)
    for (code, is_2x2), (cell_x, cell_y) in zip(TELEPORT, TELEPORT_ORIGINS):
        if not is_2x2:
            blit(_tile(gfx, code, TELEPORT_CLUT), cell_x + ONE_BY_ONE_ORIGIN, cell_y + ONE_BY_ONE_ORIGIN)
            continue
        base = code & DOUBLE_CODE_MASK
        for sub, (sx, sy) in enumerate(ARMOR_SUBTILE_ORIGINS):
            blit(_tile(gfx, base + sub, TELEPORT_CLUT),
                 cell_x + TWO_BY_TWO_ORIGIN + sx, cell_y + TWO_BY_TWO_ORIGIN + sy)
    cells([(code, SELF_DESTRUCT_TELEPORT_CLUT) for code in SELF_DESTRUCT_CODES], SELF_DESTRUCT_TELEPORT_ORIGINS)

    return Image(SHEET_WIDTH, SHEET_HEIGHT, tuple(pixels))


def _committed_sheet_path() -> Path:
    manifest = json.loads(MANIFEST_PATH.read_text())
    asset = manifest["sheets"][SHEET_NAME]["asset"]
    return ASSET_DIR / asset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path,
                        help="path to the pinned jotd666/xevious checkout")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path, help="write the rendered sheet PNG here")
    group.add_argument("--verify", action="store_true",
                       help="compare against the committed reference_art sheet")
    args = parser.parse_args(argv)

    try:
        image = render_sheet(args.checkout)
        png = encode_png(image)
        if args.verify:
            committed = _committed_sheet_path()
            want = decode_png(committed.read_bytes(), f"committed {committed.name}")
            if (want.width, want.height) != (image.width, image.height) or \
                    want.pixels != image.pixels:
                print(
                    f"MISMATCH: re-rendered reference_art sheet differs from committed "
                    f"{committed.name}", file=sys.stderr,
                )
                return 1
            print(f"OK: committed {committed.name} matches a fresh render at the pin")
            return 0
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(png)
        print(f"wrote {args.out} ({image.width}x{image.height}, sha256 {_sha256(png)})")
        return 0
    except SpriteExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
