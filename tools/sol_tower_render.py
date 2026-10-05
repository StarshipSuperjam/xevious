#!/usr/bin/env python3
"""Render the Sol Tower rise frames from the pinned arcade reference.

The Sol Tower (object type 0x1D) rises in seven steps once a bomb reveals it.
``handle_sol_tower_rising`` (``src/xevious_main.68k`` 3040-3057) takes each
step's sprite code from ``sol_tower_animation_tbl`` (3068-3078): 1x1 codes
0xA8, 0xA9, 0xAA, 0xAB for steps 0-3, then, from step 4, ``_ATTR = 3`` (2x2)
with codes 0xAE, 0xB2, 0xB6. The Spriters Resource rip only offers small
16-px pictures of the tower, half the size of the arcade's risen 2x2 sprite,
so the frames are decoded from the pinned reference's own graphics -- the same
source and decoder as the Andor parts (``tools/andor_sprite_render.py``).

A 2x2 sprite is drawn by ``sprite_draw_double_width_and_height``
(``src/amiga/amiga.68k`` 2529-2544): the low two code bits are masked off
(``and.w #0x1FC``), and codes base+0..+3 are laid at the 16-px sub-cells the
Andor armour uses. So 0xAE draws 0xAC-0xAF, 0xB2 draws 0xB0-0xB3 and 0xB6
draws 0xB4-0xB7.

Colour: every step writes ``_COLOUR = pulsing_colour_1``, which cycles CLUTs
7..0x0B (``colour_lut_pulsing_1``, ``src/xevious_sub.68k`` 229-230). Those five
CLUTs differ only in entry 7, and no Sol Tower pixel uses value 7, so the tower
looks the same on every pulse step; the tiles are decoded at CLUT 7.

Output is byte-deterministic (the repo's own encoder), so an auditor can
re-derive the committed sheet from a fresh clone at the pin.

Usage:
    python tools/sol_tower_render.py --checkout PATH --out src/xevious/assets/NAME.png
    python tools/sol_tower_render.py --checkout PATH --verify
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

SHEET_NAME = "sol_tower"

# sol_tower_animation_tbl, in rise-step order (rise/01..07).
SMALL_CODES = [0xA8, 0xA9, 0xAA, 0xAB]  # steps 0-3, _ATTR 0 (1x1)
BIG_CODES = [0xAE, 0xB2, 0xB6]          # steps 4-6, _ATTR 3 (2x2)
DOUBLE_CODE_MASK = 0x1FC                # sprite_draw_double_width_and_height: and.w #0x1FC,d0
SOL_CLUT = 7                            # colour_lut_pulsing_1[0]; the tower never uses entry 7

# Every frame gets a 32x32 cell with its picture laid from the cell's top-left, exactly as the arcade lays
# the sprite from the object's position (a 1x1 tile fills the top-left 16x16; a 2x2 the whole cell). One
# canvas for the whole animation, so the build places every rise frame with the same 2x2 shift.
CELL = 32
SHEET_WIDTH = CELL * 7
SHEET_HEIGHT = CELL
# Cell top-left origins on the sheet (must match the manifest sol_tower rects).
SMALL_CELL_ORIGINS = [(0, 0), (32, 0), (64, 0), (96, 0)]  # rise/01..04
BIG_CELL_ORIGINS = [(128, 0), (160, 0), (192, 0)]         # rise/05..07


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

    for code, (cell_x, cell_y) in zip(SMALL_CODES, SMALL_CELL_ORIGINS):
        blit(gfx.tile_rgba(code, SOL_CLUT), cell_x, cell_y)
    for code, (cell_x, cell_y) in zip(BIG_CODES, BIG_CELL_ORIGINS):
        base = code & DOUBLE_CODE_MASK
        for sub, (sx, sy) in enumerate(ARMOR_SUBTILE_ORIGINS):
            blit(gfx.tile_rgba(base + sub, SOL_CLUT), cell_x + sx, cell_y + sy)

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
                       help="compare against the committed sol_tower sheet")
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
                    f"MISMATCH: re-rendered Sol Tower sheet differs from committed "
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
