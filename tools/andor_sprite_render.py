#!/usr/bin/env python3
"""Render the Andor Genesis part tiles from the pinned arcade reference.

The Andor Genesis boss is a 15-slot composite. Its parts are NOT broken out by
any Spriters Resource rip -- that sheet only shows fully assembled octagons,
whose naive slices bake the core hub into the centre plate and leave matte in
the round corners, so they cannot form the individually-addressable tiles the
composite (and slice 16's destruction) needs.

The true separable tiles live in the pinned reference's own graphics pipeline
(``jotd666/xevious`` @ the commit below), exactly where the Special Flag sprite
was taken from (manifest sheet ``bonus_flag``): ``assets/amiga/xevious_gfx.c``
holds ``sprite[320][256]`` (each sprite = 16x16 px, one byte per px = an index
0..7 into that sprite's CLUT; 0 = transparent), ``sprite_clut[128][8]`` (each
CLUT = 8 palette indices) and ``palette[128][3]`` (RGB). A tile pixel's colour
is ``palette[ sprite_clut[clut][value] ]``.

This tool renders the 14 part cells the build crops (9 armor 2x2 + 4 gun ports
1x1 + 1 core 1x1, all at the parts' arcade CLUT colour 3) onto the shared matte
sheet, in the fixed grid the sprite manifest crops. The part sprite codes come
from each part handler's ``_CODE`` at the pin, and the 2x2 armor sub-tile layout
matches the reference's own ``sprite_specific.py`` Andor compositor. Output is
byte-deterministic (the repo's own encoder), so an auditor can re-derive the
committed sheet from a fresh clone at the pin.

Usage:
    python tools/andor_sprite_render.py --checkout PATH --out src/xevious/assets/NAME.png
    python tools/andor_sprite_render.py --checkout PATH --verify

``--verify`` re-renders and compares (decoded pixels) against the committed sheet
the manifest's ``andor_genesis`` entry points at, instead of writing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# The tool lives in tools/; reuse the extractor's deterministic PNG codec + Image.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sprite_extractor import Image, encode_png, decode_png, SpriteExtractionError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "assets" / "sprite-extraction" / "manifest.json"
ASSET_DIR = ROOT / "src" / "xevious" / "assets"

PINNED_COMMIT = "71473685a8c7856c8401c8519276cd97a38d4183"

# SHA-256 of each reference file this tool decodes, at the pinned commit. Rendering
# refuses to run against bytes that differ, so a moved pin or a tampered checkout
# cannot silently change the art. (andor_genesis_sprite_dump.bin is not decoded
# here -- it is the ground truth for the composite OFFSETS in game_director.py --
# but it is pinned there in the same spirit.)
EXPECTED_SHA256 = {
    "assets/amiga/xevious_gfx.c": (
        "3028308f85c742b1cf5569bb031ebb9c06df22943ddf3494ca5bda0c59cf66d4"
    ),
}

GFX_C = "assets/amiga/xevious_gfx.c"

# The shared sheet matte (manifest ``matte``): the extractor keys this colour to
# transparent when it crops, so transparent sprite pixels (value 0) are painted
# matte here and come back out as alpha downstream -- the bonus_flag convention.
MATTE = (0, 128, 0)

# Every visible Andor part copies andor_genesis_colour into _COLOUR each frame,
# so the boss pulses live in play. The static tiles are decoded at CLUT 3 -- the
# _COLOUR the reference's own per-sprite dump (andor_genesis_sprite_dump.bin)
# records for every Andor part -- and the Scratch colour effect drives the pulse
# on top of that base. (The live palette byte cycles [2,3,4,5,6,5,4,3]; 3 is the
# dump-recorded resting value, not the whole cycle.)
PART_CLUT = 3

# Part sprite _CODE at the pin (xevious_main.68k). Armor plates handle_41..49 have
# _ATTR=3 (2x2 -> four consecutive sprite codes); gun ports handle_4F..52 and the
# core handle_4A are 1x1. Ordinals match the manifest frame order (armor plate/01
# = type 0x41 .. plate/09 = 0x49; port muzzle/01 = 0x4F .. /04 = 0x52).
ARMOR_CODES = [88, 128, 132, 136, 140, 144, 148, 152, 156]  # 0x41..0x49
PORT_CODES = [92, 93, 94, 95]                                # 0x4F..0x52
CORE_CODE = 16                                               # 0x4A
# BOSS-03 (andor.core-destruction #96): the fly-up Bragza the destroyed core becomes is a 1x1 sprite
# animated over four codes 0xb8..0xbb (handle_Bragza: `_CODE = 0xb8 + ((countup_timer_1>>1)&3)`,
# xevious_main.68k:5498) and a cycling colour 0x15..0x1c (`_COLOUR = 0x15 + ((countup_timer_1>>1)&7)`,
# 5501). Unlike the parts (CLUT 3), the Bragza has its OWN colour cycle; the static tiles are decoded at
# the cycle BASE (CLUT 0x15) and the Scratch colour effect drives the live cycle on top (the parts'
# convention). Ordinals match the manifest frame order (fly/01..04 == codes 0xb8..0xbb).
BRAGZA_CODES = [0xB8, 0xB9, 0xBA, 0xBB]                      # 184..187
BRAGZA_CLUT = 0x15                                           # 21: the base of the Bragza colour cycle

# The reference's own 2x2 compositor (assets/amiga/sprite_specific.py) lays a
# _ATTR=3 sprite's four codes at these 16-px sub-cell origins, in code+0..+3 order.
ARMOR_SUBTILE_ORIGINS = [(16, 0), (16, 16), (0, 0), (0, 16)]  # code+0,+1,+2,+3

TILE = 16
SHEET_WIDTH = 96
SHEET_HEIGHT = 128  # a fifth 16px row (y=112) for the four Bragza cells (BOSS-03)
# Cell top-left origins on the sheet (must match manifest andor_genesis rects).
ARMOR_CELL_ORIGINS = [
    (0, 0), (32, 0), (64, 0),       # plate/01 02 03
    (0, 32), (32, 32), (64, 32),    # plate/04 05 06
    (0, 64), (32, 64), (64, 64),    # plate/07 08 09
]
PORT_CELL_ORIGINS = [(0, 96), (16, 96), (32, 96), (48, 96)]  # muzzle/01..04
CORE_CELL_ORIGIN = (64, 96)                                   # core/01 (flips downstream)
BRAGZA_CELL_ORIGINS = [(0, 112), (16, 112), (32, 112), (48, 112)]  # fly/01..04


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_reference(checkout: Path, rel: str) -> str:
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
    return data.decode("latin-1")


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
    body = text[start:index + 1]
    numbers = [int(token, 16) for token in re.findall(r"0x([0-9a-fA-F]+)", body)]
    if len(numbers) != rows * cols:
        raise SpriteExtractionError(
            f"array {name}: expected {rows * cols} bytes, parsed {len(numbers)}"
        )
    return [numbers[r * cols:(r + 1) * cols] for r in range(rows)]


class _Gfx:
    def __init__(self, text: str) -> None:
        self.sprite = _extract_c_array(text, "sprite", 320, 256)
        self.palette = _extract_c_array(text, "palette", 128, 3)
        self.sprite_clut = _extract_c_array(text, "sprite_clut", 128, 8)

    def tile_rgba(self, code: int, clut: int) -> list[tuple[int, int, int, int]]:
        """A 16x16 tile (row-major RGBA); value 0 -> transparent (matte here)."""
        data = self.sprite[code]
        entries = self.sprite_clut[clut]
        out: list[tuple[int, int, int, int]] = []
        for value in data:
            if value == 0:
                out.append((0, 0, 0, 0))
                continue
            red, green, blue = self.palette[entries[value]]
            if (red, green, blue) == MATTE:
                raise SpriteExtractionError(
                    f"sprite {code} CLUT {clut} paints an opaque matte-coloured pixel; "
                    f"the matte {MATTE} would be keyed out downstream"
                )
            out.append((red, green, blue, 255))
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

    # Armor: each plate is four consecutive sprite codes laid out 2x2.
    for code, (cell_x, cell_y) in zip(ARMOR_CODES, ARMOR_CELL_ORIGINS):
        for sub, (sx, sy) in enumerate(ARMOR_SUBTILE_ORIGINS):
            blit(gfx.tile_rgba(code + sub, PART_CLUT), cell_x + sx, cell_y + sy)
    # Gun ports: single 16x16 tiles.
    for code, (cell_x, cell_y) in zip(PORT_CODES, PORT_CELL_ORIGINS):
        blit(gfx.tile_rgba(code, PART_CLUT), cell_x, cell_y)
    # Core: single 16x16 base tile; the extractor derives its 4 flip orientations.
    blit(gfx.tile_rgba(CORE_CODE, PART_CLUT), *CORE_CELL_ORIGIN)
    # Bragza: four 16x16 anim frames, decoded at the Bragza colour cycle's base CLUT (not the parts' CLUT 3).
    for code, (cell_x, cell_y) in zip(BRAGZA_CODES, BRAGZA_CELL_ORIGINS):
        blit(gfx.tile_rgba(code, BRAGZA_CLUT), cell_x, cell_y)

    return Image(SHEET_WIDTH, SHEET_HEIGHT, tuple(pixels))


def _committed_sheet_path() -> Path:
    manifest = json.loads(MANIFEST_PATH.read_text())
    asset = manifest["sheets"]["andor_genesis"]["asset"]
    return ASSET_DIR / asset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path,
                        help="path to the pinned jotd666/xevious checkout")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path, help="write the rendered sheet PNG here")
    group.add_argument("--verify", action="store_true",
                       help="compare against the committed andor_genesis sheet")
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
                    f"MISMATCH: re-rendered Andor sheet differs from committed "
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
