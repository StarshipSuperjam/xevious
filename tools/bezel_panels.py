#!/usr/bin/env python3
"""Generate the deterministic cabinet-bezel costume that frames the arcade screen.

Presentation (docs/mechanics/054-arcade-screen-proportions.md, PRES-01): the port shows the
whole 224x288 arcade screen at one isotropic 1.25 stage units per arcade pixel — a 280x360
window centred on the 480x360 Scratch stage — and fills the two 100x360 side areas with
cabinet bezel art, the way the game sat behind its bezel in the cabinet. This generator reads
the operator-approved bezel source committed under assets/bezel/ (pinned by SHA-256 in
assets/bezel/manifest.json), box-averages its two side columns into opaque panels and emits
ONE full-stage costume: left panel, transparent window, right panel. One costume on the
target's original keeps the bezel at zero clones (the ground clone bands already sit at
scratch-vm's 300-clone ceiling).

Ownership (the same split as the easter-egg overlay): tools/game_director.py owns the `bezel`
target's EXISTENCE, BLOCKS and draw layer; this module owns its single COSTUME, the overlay
provenance record for it, and its own derivative provenance (assets/bezel/provenance.json). It
reuses tools/sprite_extractor.py's deterministic PNG encoder and JSON helpers; the source is a
palette PNG, which that module's decoder does not read, so the small palette decoder lives here.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import struct
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sprite_extractor as se  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
BEZEL_DIR = ROOT / "assets" / "bezel"
MANIFEST_PATH = BEZEL_DIR / "manifest.json"
DERIVATIVE_PROVENANCE_PATH = BEZEL_DIR / "provenance.json"
ASSET_DIR = ROOT / "src" / "xevious" / "assets"
OVERLAY_PROVENANCE_PATH = ASSET_DIR / "provenance.json"
PROJECT_PATH = ROOT / "src" / "xevious" / "project.json"
GENERATOR_VERSION = 1
BEZEL_TARGET = "bezel"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class BezelPanelsError(RuntimeError):
    """The bezel manifest, its source, or a generated output is invalid."""


@dataclass(frozen=True)
class BezelOutput:
    name: str
    filename: str  # content-hash <md5>.png under src/xevious/assets/
    png: bytes
    width: int
    height: int
    bitmap_resolution: int


@dataclass(frozen=True)
class PaletteImage:
    width: int
    height: int
    rows: tuple[bytes, ...]  # one palette-index byte per pixel
    palette: tuple[tuple[int, int, int, int], ...]


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BezelPanelsError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BezelPanelsError(f"{path} must contain one JSON object")
    return value


def _require_keys(value: object, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise BezelPanelsError(f"{label} must have exactly the keys {sorted(expected)}")
    return value


def _int_pair(value: object, label: str) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in value)
    ):
        raise BezelPanelsError(f"{label} must be two non-negative integers")
    return value[0], value[1]


def load_manifest() -> tuple[dict, bytes]:
    try:
        manifest_bytes = MANIFEST_PATH.read_bytes()
    except OSError as exc:
        raise BezelPanelsError(f"cannot read {MANIFEST_PATH}: {exc}") from exc
    manifest = _read_json(MANIFEST_PATH)
    _require_keys(manifest, {"version", "kind", "note", "source", "panels", "costume"}, "manifest")
    if manifest["version"] != 1 or manifest["kind"] != "bezel-source":
        raise BezelPanelsError("manifest must be version 1 of kind bezel-source")
    source = _require_keys(
        manifest["source"],
        {"file", "sha256", "dimensions", "url", "project", "credit", "license"},
        "manifest source",
    )
    for key in ("file", "sha256", "url", "project", "credit", "license"):
        if not isinstance(source[key], str) or not source[key]:
            raise BezelPanelsError(f"manifest source {key} must be a non-empty string")
    _int_pair(source["dimensions"], "manifest source dimensions")
    panels = _require_keys(
        manifest["panels"], {"note", "rows", "left_columns", "right_columns", "box"}, "manifest panels"
    )
    source_w, source_h = source["dimensions"]
    row0, row_count = _int_pair(panels["rows"], "panel rows")
    box_x, box_y = _int_pair(panels["box"], "panel box")
    if not box_x or not box_y or row_count % box_y or row0 + row_count > source_h:
        raise BezelPanelsError("panel rows must lie in the source and divide by the box height")
    for key in ("left_columns", "right_columns"):
        col0, col_count = _int_pair(panels[key], f"panel {key}")
        if not col_count or col_count % box_x or col0 + col_count > source_w:
            raise BezelPanelsError(f"panel {key} must lie in the source and divide by the box width")
    if panels["left_columns"][1] != panels["right_columns"][1]:
        raise BezelPanelsError("the two panels must be the same width")
    costume = _require_keys(
        manifest["costume"],
        {"name", "width", "height", "bitmap_resolution", "window_width"},
        "manifest costume",
    )
    panel_w = panels["left_columns"][1] // box_x
    if (
        costume["height"] != row_count // box_y
        or costume["width"] != 2 * panel_w + costume["window_width"]
        or costume["bitmap_resolution"] not in (1, 2)
    ):
        raise BezelPanelsError("costume geometry does not match the panel crop")
    return manifest, manifest_bytes


def decode_palette_png(data: bytes, label: str) -> PaletteImage:
    """Decode the non-interlaced 8-bit palette subset the bezel source uses (filter 0 rows)."""
    if not data.startswith(PNG_SIGNATURE):
        raise BezelPanelsError(f"{label} has no PNG signature")
    position = len(PNG_SIGNATURE)
    header = None
    palette_rgb = b""
    alpha = b""
    compressed = bytearray()
    saw_end = False
    while position < len(data):
        if position + 12 > len(data):
            raise BezelPanelsError(f"{label} has a truncated PNG chunk")
        length = struct.unpack_from(">I", data, position)[0]
        kind = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + length]
        end = position + 8 + length
        if end + 4 > len(data):
            raise BezelPanelsError(f"{label} has a truncated PNG payload")
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != struct.unpack_from(">I", data, end)[0]:
            raise BezelPanelsError(f"{label} has an invalid PNG checksum")
        position = end + 4
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"PLTE":
            palette_rgb = payload
        elif kind == b"tRNS":
            alpha = payload
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            saw_end = True
            break
        elif kind[:1].isupper():
            raise BezelPanelsError(f"{label} uses unsupported critical PNG chunk {kind!r}")
    if header is None or not saw_end or not compressed or not palette_rgb:
        raise BezelPanelsError(f"{label} is missing required PNG chunks")
    width, height, depth, color_type, compression, filtering, interlace = header
    if depth != 8 or color_type != 3 or compression or filtering or interlace:
        raise BezelPanelsError(f"{label} must be a non-interlaced 8-bit palette PNG")
    if len(palette_rgb) % 3 or len(alpha) > len(palette_rgb) // 3:
        raise BezelPanelsError(f"{label} has an invalid palette")
    palette = tuple(
        (
            palette_rgb[3 * index],
            palette_rgb[3 * index + 1],
            palette_rgb[3 * index + 2],
            alpha[index] if index < len(alpha) else 255,
        )
        for index in range(len(palette_rgb) // 3)
    )
    raw = zlib.decompress(bytes(compressed))
    stride = width + 1
    if len(raw) != height * stride:
        raise BezelPanelsError(f"{label} has an unexpected pixel-data length")
    rows = []
    for y in range(height):
        if raw[y * stride] != 0:
            raise BezelPanelsError(f"{label} row {y} uses PNG filter {raw[y * stride]}, expected 0")
        rows.append(raw[y * stride + 1:(y + 1) * stride])
    return PaletteImage(width, height, tuple(rows), palette)


def load_source(manifest: dict) -> PaletteImage:
    source = manifest["source"]
    path = BEZEL_DIR / source["file"]
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BezelPanelsError(f"cannot read bezel source {path}: {exc}") from exc
    if se._sha256(data) != source["sha256"]:
        raise BezelPanelsError(f"{path} does not match its recorded SHA-256")
    image = decode_palette_png(data, str(path))
    if [image.width, image.height] != source["dimensions"]:
        raise BezelPanelsError(f"{path} does not match its recorded dimensions")
    return image


def _render_panel(
    image: PaletteImage, row0: int, row_count: int, col0: int, col_count: int, box_x: int, box_y: int
) -> list[list[tuple[int, int, int, int]]]:
    # Each palette entry composited over opaque black (premultiplied, rounded) — the panels must be
    # fully opaque so they mask the playfield behind them — as one byte-translation table per channel.
    tables = [
        bytes(
            (image.palette[index][channel] * image.palette[index][3] + 127) // 255
            if index < len(image.palette)
            else 0
            for index in range(256)
        )
        for channel in range(3)
    ]
    area = box_x * box_y
    out_rows = []
    for out_y in range(row_count // box_y):
        sources = [
            image.rows[row0 + out_y * box_y + offset][col0:col0 + col_count]
            for offset in range(box_y)
        ]
        if max(max(row) for row in sources) >= len(image.palette):
            raise BezelPanelsError("bezel source uses a palette index outside its palette")
        channels = []
        for table in tables:
            column_sums = [sum(column) for column in zip(*(row.translate(table) for row in sources))]
            channels.append(
                [
                    (sum(column_sums[x:x + box_x]) + area // 2) // area
                    for x in range(0, col_count, box_x)
                ]
            )
        out_rows.append([(red, green, blue, 255) for red, green, blue in zip(*channels)])
    return out_rows


def render_frame(manifest: dict) -> BezelOutput:
    image = load_source(manifest)
    panels = manifest["panels"]
    costume = manifest["costume"]
    row0, row_count = panels["rows"]
    box_x, box_y = panels["box"]
    left = _render_panel(image, row0, row_count, *panels["left_columns"], box_x, box_y)
    right = _render_panel(image, row0, row_count, *panels["right_columns"], box_x, box_y)
    window = [(0, 0, 0, 0)] * costume["window_width"]
    pixels: list[tuple[int, int, int, int]] = []
    for left_row, right_row in zip(left, right):
        pixels.extend(left_row)
        pixels.extend(window)
        pixels.extend(right_row)
    png = se.encode_png(se.Image(costume["width"], costume["height"], tuple(pixels)))
    return BezelOutput(
        costume["name"],
        f"{se._md5(png)}.png",
        png,
        costume["width"],
        costume["height"],
        costume["bitmap_resolution"],
    )


def _costume(output: BezelOutput) -> dict:
    return {
        "name": output.name,
        "bitmapResolution": output.bitmap_resolution,
        "dataFormat": "png",
        "assetId": output.filename.removesuffix(".png"),
        "md5ext": output.filename,
        "rotationCenterX": output.width // 2,
        "rotationCenterY": output.height // 2,
    }


def expected_project(project: dict, output: BezelOutput) -> dict:
    result = json.loads(json.dumps(project))
    bezel = next(
        (target for target in result["targets"] if target.get("name") == BEZEL_TARGET), None
    )
    if bezel is None:
        raise BezelPanelsError(
            f"Scratch project has no {BEZEL_TARGET} target; run tools/game_director.py generate first"
        )
    bezel["costumes"] = [_costume(output)]
    bezel["currentCostume"] = 0
    return result


def _overlay_record(manifest: dict, output: BezelOutput) -> dict:
    source = manifest["source"]
    left0, width = manifest["panels"]["left_columns"]
    right0, _ = manifest["panels"]["right_columns"]
    row0, row_count = manifest["panels"]["rows"]
    box_x, box_y = manifest["panels"]["box"]
    return {
        "origin": (
            f"Cabinet bezel frame '{output.name}' (PRES-01) composited by tools/bezel_panels.py "
            f"from {source['url']}"
        ),
        "license": source["license"],
        "notes": (
            f"Credit: {source['credit']}. The repository operator did not create the artwork. "
            f"Source assets/bezel/{source['file']} at SHA-256 {source['sha256']}; the side columns "
            f"x {left0}..{left0 + width - 1} and x {right0}..{right0 + width - 1}, rows "
            f"{row0}..{row0 + row_count - 1}, box-averaged {box_x}x{box_y} and composited over opaque "
            f"black, either side of a transparent {manifest['costume']['window_width']}-px window; "
            f"{output.width}x{output.height}, bitmapResolution {output.bitmap_resolution}."
        ),
    }


def _derivative_provenance(manifest: dict, manifest_bytes: bytes, output: BezelOutput) -> dict:
    return {
        "version": 1,
        "generator_version": GENERATOR_VERSION,
        "manifest_sha256": se._sha256(manifest_bytes),
        "source_sha256": manifest["source"]["sha256"],
        "outputs": {
            output.filename: {
                "kind": "bezel",
                "name": output.name,
                "generator_version": GENERATOR_VERSION,
            }
        },
    }


def _prior_output_records() -> dict[str, dict]:
    if not DERIVATIVE_PROVENANCE_PATH.exists():
        return {}
    outputs = _read_json(DERIVATIVE_PROVENANCE_PATH).get("outputs")
    if not isinstance(outputs, dict):
        raise BezelPanelsError(f"{DERIVATIVE_PROVENANCE_PATH} has no outputs object")
    return outputs


def _expected_state() -> tuple[BezelOutput, bytes, bytes, bytes, set[str]]:
    manifest, manifest_bytes = load_manifest()
    output = render_frame(manifest)
    prior_outputs = set(_prior_output_records())
    project_bytes = se._ordered_json_bytes(expected_project(_read_json(PROJECT_PATH), output))
    overlay = _read_json(OVERLAY_PROVENANCE_PATH)
    if overlay.get("version") != 1 or not isinstance(overlay.get("assets"), dict):
        raise BezelPanelsError("overlay provenance must use version 1")
    assets = {
        name: record for name, record in overlay["assets"].items() if name not in prior_outputs
    }
    assets[output.filename] = _overlay_record(manifest, output)
    overlay_bytes = se._ordered_json_bytes({"version": 1, "assets": dict(sorted(assets.items()))})
    derivative_bytes = se._ordered_json_bytes(
        _derivative_provenance(manifest, manifest_bytes, output)
    )
    return output, project_bytes, overlay_bytes, derivative_bytes, prior_outputs


def generate() -> None:
    output, project_bytes, overlay_bytes, derivative_bytes, prior_outputs = _expected_state()
    for stale in sorted(prior_outputs - {output.filename}):
        stale_path = ASSET_DIR / stale
        if stale_path.is_file() and not stale_path.is_symlink():
            stale_path.unlink()
    (ASSET_DIR / output.filename).write_bytes(output.png)
    PROJECT_PATH.write_bytes(project_bytes)
    OVERLAY_PROVENANCE_PATH.write_bytes(overlay_bytes)
    DERIVATIVE_PROVENANCE_PATH.write_bytes(derivative_bytes)
    check_repository()


def _require_bytes(path: Path, expected: bytes) -> None:
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise BezelPanelsError(f"missing generated output {path}") from exc
    if actual != expected:
        raise BezelPanelsError(f"generated output is stale; run bezel_panels.py generate: {path}")


def check_repository() -> int:
    output, project_bytes, overlay_bytes, derivative_bytes, prior_outputs = _expected_state()
    stale = prior_outputs - {output.filename}
    if stale:
        raise BezelPanelsError("stale generated bezel assets: " + ", ".join(sorted(stale)))
    _require_bytes(ASSET_DIR / output.filename, output.png)
    _require_bytes(PROJECT_PATH, project_bytes)
    _require_bytes(OVERLAY_PROVENANCE_PATH, overlay_bytes)
    _require_bytes(DERIVATIVE_PROVENANCE_PATH, derivative_bytes)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "check"))
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            generate()
            print(f"generated and verified {check_repository()} bezel costume")
        else:
            print(f"verified {check_repository()} bezel costume")
    except BezelPanelsError as exc:
        print(f"bezel_panels: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
