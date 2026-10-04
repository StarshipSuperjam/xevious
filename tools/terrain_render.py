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

Usage:
    python tools/terrain_render.py render --checkout PATH
    python tools/terrain_render.py verify --checkout PATH
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--checkout", required=True, type=Path,
                             help="path to the pinned jotd666/xevious checkout")
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            return cmd_render(args.checkout)
        return cmd_verify(args.checkout)
    except SpriteExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
