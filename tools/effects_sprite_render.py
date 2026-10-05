#!/usr/bin/env python3
"""Render the explosion, bomb and crosshair frames from the pinned arcade reference.

CAB-05 (slice 20 PR-4) replaces the shared eight-frame stand-in burst, the bomb,
the crosshair and the enemy bullet with the arcade's own sprites, decoded from
the pinned reference's graphics data (``assets/amiga/xevious_gfx.c``) with the
same decoder as the Andor parts (``tools/andor_sprite_render.py``).

Three explosion families, each with its own sprite codes and colour:

- player explosion -- ``solvalou_explosion_tbl`` (``src/xevious_main.68k``
  2093-2100): C0, C1, C4 (2x2), C8 (2x2), C2, C3, CC (2x2). Step ``n`` is drawn
  at colour ``0x30 + n`` (``explode_solvalou`` 2040, 2064).
- air explosion -- ``flying_obj_explosion_sprites`` (4892-4897): 70, 71, then
  74, 78, 7C at 2x2, colour 7 (``flying_enemy_hit`` 4866).
- ground explosion -- ``bomb_explosion_animation_tbl`` (4944-4951): 60, 61,
  then 64, 68, 6C at 2x2, then 62, 63, colour 0x0C (4905); then the crater
  A6/A7 at colour 0x0D (``bomb_explosion_finished`` 4931-4942).

The player and air explosions flip every frame (``_ATTR`` bits 2-3 from the
frame counter, 2072-2075 / 4884-4887); those flips are left to the sprite
extractor's ``flips`` attribute, so only the unflipped picture is rendered here.

Explosion cells are 32x32 and every frame is centred in its cell. The arcade
draws a 2x2 sprite from the 1x1 position extending 16 px right and down
(``sprite_draw_double_width_and_height``, ``src/amiga/amiga.68k`` 2529-2544:
codes base+2 / base+0 on the top row, base+3 / base+1 below, the low two code
bits masked off), but each explosion moves the object one position MSB (8 px)
up and left on screen as it enters a 2x2 frame and back as it leaves one
(``_X`` -1 / ``_Y`` +1: player 2056-2063 with 2102-2111, air 4872-4875, ground
4919-4923 with 4953-4961; on screen ``_X`` runs down and ``_Y`` runs left,
``amiga.68k`` 1677-1697 and 2026). The nudge cancels exactly half the 2x2's
overhang, so every frame of an explosion shares one centre: a 1x1 frame sits at
(8, 8) in its cell and a 2x2 frame fills it. Because each frame is centred, the
extractor's flip about the canvas centre mirrors it in place -- the Neo Geo
renderer's 2x2 flip (``src/neogeo/neogeo.68k`` 951-961: sub-cell codes swapped,
the block left where it is) -- and the build needs no per-frame offset.

Bank-1 sprites (``_ATTR`` bit 7; ``amiga.68k`` 1774-1778 adds 0x100 to the code),
16x16 cells:

- crosshair -- tile 0x114 (``main_fn_1__handle_solvalou`` 1998). Colour 32
  idle, 33 with a bomb in flight, plus 9 while the frame counter's bit 2 is set
  over a ground target: 41 / 42 (``handle_crosshairs`` 2239-2281).
- bomb target -- the same tile at colour 0x22 (``init_bombing`` 2469).
- bomb -- tiles 0x11C, 0x11D, 0x11E (2468, 2476-2483) at colours
  ``0x25 + ((TIMER >> 2) & 3)`` (2484-2488). The enemy bullet is tile 0x11E
  (4763) at the same four colours (``sub_fn_5__handle_pulsing_colours``,
  ``src/xevious_sub.68k`` 208-218), so it reuses the bomb's last four cells
  rather than repeating identical pictures here.

A colour-table entry of 0x80 is beyond the 128-colour palette and draws nothing
(colour table 0 is all 0x80), so it is rendered as matte, like pixel value 0.

Output is byte-deterministic (the repo's own encoder), so an auditor can
re-derive the committed sheet from a fresh clone at the pin.

Usage:
    python tools/effects_sprite_render.py --checkout PATH --out src/xevious/assets/NAME.png
    python tools/effects_sprite_render.py --checkout PATH --verify
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
from sprite_extractor import Image, SpriteExtractionError, decode_png, encode_png  # noqa: E402

SHEET_NAME = "effects"

DOUBLE_CODE_MASK = 0x1FC   # sprite_draw_double_width_and_height: and.w #0x1FC,d0
BANK_1 = 0x100             # _ATTR bit 7 selects the second graphics bank (amiga.68k 1774-1778)
TRANSPARENT_ENTRY = 0x80   # a colour-table entry past the 128-colour palette: draws nothing

# (code, is_2x2) per frame, in animation order.
PLAYER_EXPLOSION = [(0xC0, False), (0xC1, False), (0xC4, True), (0xC8, True),
                    (0xC2, False), (0xC3, False), (0xCC, True)]
PLAYER_EXPLOSION_CLUT_BASE = 0x30  # step n is drawn at colour 0x30 + n
AIR_EXPLOSION = [(0x70, False), (0x71, False), (0x74, True), (0x78, True), (0x7C, True)]
AIR_EXPLOSION_CLUT = 7
GROUND_EXPLOSION = [(0x60, False), (0x61, False), (0x64, True), (0x68, True), (0x6C, True),
                    (0x62, False), (0x63, False)]
GROUND_EXPLOSION_CLUT = 0x0C
CRATER_CODES = [0xA6, 0xA7]
CRATER_CLUT = 0x0D

CROSSHAIR_CODE = BANK_1 + 0x14
CROSSHAIR_CLUTS = [32, 33, 41, 42]  # idle, bombing, idle over a target, bombing over a target
BOMB_TARGET_CLUT = 0x22
BOMB_CODES = [BANK_1 + 0x1C, BANK_1 + 0x1D, BANK_1 + 0x1E]
BOMB_CLUTS = [0x25, 0x26, 0x27, 0x28]

EXPLOSION_CELL = 32
ONE_BY_ONE_ORIGIN = 8   # a 1x1 frame's top-left inside an explosion cell (centred)
TWO_BY_TWO_ORIGIN = 0   # a 2x2 frame fills the cell (nudged half its overhang up-left)
SMALL_CELL = 16
SHEET_WIDTH = EXPLOSION_CELL * 7
# Row origins (must match the manifest effects rects).
PLAYER_ROW_Y = 0
AIR_ROW_Y = EXPLOSION_CELL
GROUND_ROW_Y = EXPLOSION_CELL * 2
SMALL_ROW_Y = EXPLOSION_CELL * 3            # crater x2, crosshair x4, bomb target
BOMB_ROW_Y = SMALL_ROW_Y + SMALL_CELL       # bomb: 3 codes x 4 colours, code-major
SHEET_HEIGHT = BOMB_ROW_Y + SMALL_CELL


def explosion_cell_origins(count: int, row_y: int) -> list[tuple[int, int]]:
    return [(EXPLOSION_CELL * index, row_y) for index in range(count)]


def small_cell_origins(count: int, row_y: int, first: int = 0) -> list[tuple[int, int]]:
    return [(SMALL_CELL * (first + index), row_y) for index in range(count)]


CRATER_ORIGINS = small_cell_origins(2, SMALL_ROW_Y)
CROSSHAIR_ORIGINS = small_cell_origins(4, SMALL_ROW_Y, first=2)
BOMB_TARGET_ORIGIN = small_cell_origins(1, SMALL_ROW_Y, first=6)[0]
BOMB_ORIGINS = small_cell_origins(len(BOMB_CODES) * len(BOMB_CLUTS), BOMB_ROW_Y)


def _tile(gfx: _Gfx, code: int, clut: int) -> list[tuple[int, int, int, int]]:
    """A 16x16 tile (row-major RGBA). Pixel value 0 and colour-table entry 0x80 are transparent."""
    entries = gfx.sprite_clut[clut]
    out: list[tuple[int, int, int, int]] = []
    for value in gfx.sprite[code]:
        if value == 0 or entries[value] == TRANSPARENT_ENTRY:
            out.append((0, 0, 0, 0))
            continue
        red, green, blue = gfx.palette[entries[value]]
        if (red, green, blue) == MATTE:
            raise SpriteExtractionError(
                f"sprite {code:#x} CLUT {clut:#x} paints an opaque matte-coloured pixel; "
                f"the matte {MATTE} would be keyed out downstream"
            )
        out.append((red, green, blue, 255))
    if all(px[3] == 0 for px in out):
        raise SpriteExtractionError(f"sprite {code:#x} CLUT {clut:#x} draws nothing")
    return out


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

    def explosion(frames: list[tuple[int, bool]], cluts: list[int], row_y: int) -> None:
        for (code, is_2x2), clut, (cell_x, cell_y) in zip(
            frames, cluts, explosion_cell_origins(len(frames), row_y)
        ):
            if not is_2x2:
                blit(_tile(gfx, code, clut), cell_x + ONE_BY_ONE_ORIGIN, cell_y + ONE_BY_ONE_ORIGIN)
                continue
            base = code & DOUBLE_CODE_MASK
            for sub, (sx, sy) in enumerate(ARMOR_SUBTILE_ORIGINS):
                blit(_tile(gfx, base + sub, clut),
                     cell_x + TWO_BY_TWO_ORIGIN + sx, cell_y + TWO_BY_TWO_ORIGIN + sy)

    explosion(PLAYER_EXPLOSION,
              [PLAYER_EXPLOSION_CLUT_BASE + step for step in range(len(PLAYER_EXPLOSION))],
              PLAYER_ROW_Y)
    explosion(AIR_EXPLOSION, [AIR_EXPLOSION_CLUT] * len(AIR_EXPLOSION), AIR_ROW_Y)
    explosion(GROUND_EXPLOSION, [GROUND_EXPLOSION_CLUT] * len(GROUND_EXPLOSION), GROUND_ROW_Y)
    for code, origin in zip(CRATER_CODES, CRATER_ORIGINS):
        blit(_tile(gfx, code, CRATER_CLUT), *origin)
    for clut, origin in zip(CROSSHAIR_CLUTS, CROSSHAIR_ORIGINS):
        blit(_tile(gfx, CROSSHAIR_CODE, clut), *origin)
    blit(_tile(gfx, CROSSHAIR_CODE, BOMB_TARGET_CLUT), *BOMB_TARGET_ORIGIN)
    bomb_cells = [(code, clut) for code in BOMB_CODES for clut in BOMB_CLUTS]
    for (code, clut), origin in zip(bomb_cells, BOMB_ORIGINS):
        blit(_tile(gfx, code, clut), *origin)

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
                       help="compare against the committed effects sheet")
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
                    f"MISMATCH: re-rendered effects sheet differs from committed "
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
