#!/usr/bin/env python3
"""Generate deterministic Scratch costumes from credited Xevious sprite sheets."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import zlib


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "assets" / "sprite-extraction" / "manifest.json"
DERIVATIVE_PROVENANCE_PATH = (
    ROOT / "assets" / "sprite-extraction" / "provenance.json"
)
ASSET_DIR = ROOT / "src" / "xevious" / "assets"
OVERLAY_PROVENANCE_PATH = ASSET_DIR / "provenance.json"
PROJECT_PATH = ROOT / "src" / "xevious" / "project.json"
CONTACT_SHEET_PATH = ROOT / "docs" / "images" / "sprite-extraction-proof.png"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
GENERATOR_VERSION = 1
GENERATED_TARGET = "toroid_sprite_proof"
MANAGED_TARGETS = {"solvalou", GENERATED_TARGET}
FRAME_NAME = re.compile(r"^[a-z0-9]+(?:[/-][a-z0-9]+)*$")


class SpriteExtractionError(RuntimeError):
    """The sprite manifest or a generated output is invalid."""


@dataclass(frozen=True)
class Image:
    width: int
    height: int
    pixels: tuple[tuple[int, int, int, int], ...]

    def pixel(self, x: int, y: int) -> tuple[int, int, int, int]:
        return self.pixels[y * self.width + x]


@dataclass(frozen=True)
class Derivative:
    frame: dict
    filename: str
    png: bytes
    image: Image


def _ordered_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return (
        struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    )


def encode_png(image: Image) -> bytes:
    """Encode an 8-bit RGBA image with fixed, deterministic PNG settings."""
    if image.width <= 0 or image.height <= 0:
        raise SpriteExtractionError("cannot encode an empty image")
    if len(image.pixels) != image.width * image.height:
        raise SpriteExtractionError("image pixel count does not match its dimensions")
    raw = bytearray()
    for y in range(image.height):
        raw.append(0)
        for pixel in image.pixels[y * image.width:(y + 1) * image.width]:
            if len(pixel) != 4 or any(
                not isinstance(channel, int) or not 0 <= channel <= 255
                for channel in pixel
            ):
                raise SpriteExtractionError("image contains an invalid RGBA pixel")
            raw.extend(pixel)
    ihdr = struct.pack(">IIBBBBB", image.width, image.height, 8, 6, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), level=9))
        + _chunk(b"IEND", b"")
    )


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def decode_png(data: bytes, label: str = "PNG") -> Image:
    """Decode the non-interlaced 8-bit RGB/RGBA subset used by the sheets."""
    if not data.startswith(PNG_SIGNATURE):
        raise SpriteExtractionError(f"{label} has no PNG signature")
    position = len(PNG_SIGNATURE)
    header = None
    compressed = bytearray()
    saw_end = False
    while position < len(data):
        if position + 12 > len(data):
            raise SpriteExtractionError(f"{label} has a truncated PNG chunk")
        length = struct.unpack_from(">I", data, position)[0]
        position += 4
        kind = data[position:position + 4]
        position += 4
        end = position + length
        if end + 4 > len(data):
            raise SpriteExtractionError(f"{label} has a truncated PNG payload")
        payload = data[position:end]
        expected_crc = struct.unpack_from(">I", data, end)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise SpriteExtractionError(f"{label} has an invalid PNG checksum")
        position = end + 4
        if kind == b"IHDR":
            if header is not None or length != 13:
                raise SpriteExtractionError(f"{label} has an invalid PNG header")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            if payload:
                raise SpriteExtractionError(f"{label} has an invalid PNG end chunk")
            saw_end = True
            break
        elif kind[:1].isupper():
            raise SpriteExtractionError(
                f"{label} uses unsupported critical PNG chunk {kind!r}"
            )
    if header is None or not saw_end or not compressed:
        raise SpriteExtractionError(f"{label} is missing required PNG chunks")
    width, height, depth, color_type, compression, filtering, interlace = header
    if (
        not width
        or not height
        or depth != 8
        or color_type not in (2, 6)
        or compression != 0
        or filtering != 0
        or interlace != 0
    ):
        raise SpriteExtractionError(
            f"{label} must be a non-interlaced 8-bit RGB or RGBA PNG"
        )
    bytes_per_pixel = 3 if color_type == 2 else 4
    row_bytes = width * bytes_per_pixel
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise SpriteExtractionError(f"{label} has invalid compressed pixels") from exc
    if len(raw) != height * (row_bytes + 1):
        raise SpriteExtractionError(f"{label} has an unexpected pixel-data length")
    previous = bytearray(row_bytes)
    pixels: list[tuple[int, int, int, int]] = []
    offset = 0
    for _y in range(height):
        filter_type = raw[offset]
        offset += 1
        encoded = raw[offset:offset + row_bytes]
        offset += row_bytes
        decoded = bytearray(row_bytes)
        for index, value in enumerate(encoded):
            left = decoded[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = (
                previous[index - bytes_per_pixel]
                if index >= bytes_per_pixel
                else 0
            )
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                predictor = _paeth(left, above, upper_left)
            else:
                raise SpriteExtractionError(
                    f"{label} uses unsupported PNG filter {filter_type}"
                )
            decoded[index] = (value + predictor) & 0xFF
        for index in range(0, row_bytes, bytes_per_pixel):
            red, green, blue = decoded[index:index + 3]
            alpha = decoded[index + 3] if bytes_per_pixel == 4 else 255
            pixels.append((red, green, blue, alpha))
        previous = decoded
    return Image(width, height, tuple(pixels))


def _rect(value: object, label: str) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(item, int) for item in value)
    ):
        raise SpriteExtractionError(f"{label} must be four integers")
    x, y, width, height = value
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise SpriteExtractionError(f"{label} must have non-negative origin and size")
    return x, y, width, height


def _pair(value: object, label: str, *, positive: bool) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(not isinstance(item, int) for item in value)
    ):
        raise SpriteExtractionError(f"{label} must be two integers")
    minimum = 1 if positive else 0
    if any(item < minimum for item in value):
        qualifier = "positive" if positive else "non-negative"
        raise SpriteExtractionError(f"{label} values must be {qualifier}")
    return value[0], value[1]


def _intersects(
    left: tuple[int, int, int, int],
    right: tuple[int, int, int, int],
) -> bool:
    left_x, left_y, left_width, left_height = left
    right_x, right_y, right_width, right_height = right
    return (
        left_x < right_x + right_width
        and right_x < left_x + left_width
        and left_y < right_y + right_height
        and right_y < left_y + left_height
    )


def _require_keys(value: dict, expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = expected - actual
        unknown = actual - expected
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise SpriteExtractionError(f"{label} fields are invalid: {'; '.join(details)}")


def validate_manifest(manifest: object) -> dict:
    if not isinstance(manifest, dict):
        raise SpriteExtractionError("manifest must be one JSON object")
    _require_keys(
        manifest,
        {
            "version",
            "generator_version",
            "matte",
            "contact_sheet",
            "sheets",
            "frames",
        },
        "manifest",
    )
    if manifest.get("version") != 1:
        raise SpriteExtractionError("manifest must use version 1")
    if manifest.get("generator_version") != GENERATOR_VERSION:
        raise SpriteExtractionError(
            f"manifest must select generator version {GENERATOR_VERSION}"
        )
    matte = manifest.get("matte")
    if (
        not isinstance(matte, list)
        or len(matte) != 3
        or any(not isinstance(channel, int) or not 0 <= channel <= 255 for channel in matte)
    ):
        raise SpriteExtractionError("manifest matte must contain three byte values")
    if manifest.get("contact_sheet") != CONTACT_SHEET_PATH.relative_to(ROOT).as_posix():
        raise SpriteExtractionError("manifest contact_sheet must use the canonical path")
    sheets = manifest.get("sheets")
    if not isinstance(sheets, dict) or not sheets:
        raise SpriteExtractionError("manifest must contain a non-empty sheets object")
    for sheet_name, sheet in sheets.items():
        if not isinstance(sheet_name, str) or not isinstance(sheet, dict):
            raise SpriteExtractionError("manifest sheet entries must be named objects")
        _require_keys(
            sheet,
            {
                "asset",
                "sha256",
                "source",
                "credit",
                "license",
                "excluded_rects",
            },
            f"sheet {sheet_name}",
        )
        asset = sheet.get("asset")
        sha256 = sheet.get("sha256")
        if (
            not isinstance(asset, str)
            or not re.fullmatch(r"[0-9a-f]{32}\.png", asset)
        ):
            raise SpriteExtractionError(f"sheet {sheet_name} has an invalid asset name")
        if (
            not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        ):
            raise SpriteExtractionError(f"sheet {sheet_name} has an invalid SHA-256")
        for field in ("source", "credit", "license"):
            if not isinstance(sheet.get(field), str) or not sheet[field].strip():
                raise SpriteExtractionError(
                    f"sheet {sheet_name} has no recorded {field}"
                )
        exclusions = sheet.get("excluded_rects")
        if not isinstance(exclusions, list):
            raise SpriteExtractionError(
                f"sheet {sheet_name} excluded_rects must be a list"
            )
        for index, exclusion in enumerate(exclusions):
            _rect(exclusion, f"sheet {sheet_name} excluded_rects[{index}]")
    frames = manifest.get("frames")
    if not isinstance(frames, list) or not frames:
        raise SpriteExtractionError("manifest must contain a non-empty frames list")
    names: set[str] = set()
    occupied: dict[str, list[tuple[str, tuple[int, int, int, int]]]] = {}
    animations: dict[tuple[str, str], tuple[tuple[int, int], tuple[int, int]]] = {}
    for index, frame in enumerate(frames):
        label = f"frames[{index}]"
        if not isinstance(frame, dict):
            raise SpriteExtractionError(f"{label} must be an object")
        _require_keys(
            frame,
            {
                "name",
                "sheet",
                "rect",
                "canvas",
                "anchor",
                "target",
                "family",
                "animation",
                "duration_frames",
            },
            label,
        )
        name = frame.get("name")
        if not isinstance(name, str) or not FRAME_NAME.fullmatch(name):
            raise SpriteExtractionError(f"{label} has an invalid frame name")
        if name in names:
            raise SpriteExtractionError(f"duplicate frame name: {name}")
        names.add(name)
        sheet_name = frame.get("sheet")
        if sheet_name not in sheets:
            raise SpriteExtractionError(f"{name} names unknown sheet {sheet_name!r}")
        target = frame.get("target")
        if target not in MANAGED_TARGETS:
            raise SpriteExtractionError(f"{name} names unmanaged target {target!r}")
        family = frame.get("family")
        animation = frame.get("animation")
        if (
            not isinstance(family, str)
            or not family
            or not isinstance(animation, str)
            or not animation
        ):
            raise SpriteExtractionError(f"{name} needs family and animation")
        duration = frame.get("duration_frames")
        if not isinstance(duration, int) or duration <= 0:
            raise SpriteExtractionError(f"{name} needs a positive duration_frames")
        frame_rect = _rect(frame.get("rect"), f"{name}.rect")
        canvas = _pair(frame.get("canvas"), f"{name}.canvas", positive=True)
        anchor = _pair(frame.get("anchor"), f"{name}.anchor", positive=False)
        if anchor[0] > canvas[0] or anchor[1] > canvas[1]:
            raise SpriteExtractionError(f"{name} anchor falls outside its canvas")
        offset_x = anchor[0] - frame_rect[2] // 2
        offset_y = anchor[1] - frame_rect[3] // 2
        if (
            offset_x < 0
            or offset_y < 0
            or offset_x + frame_rect[2] > canvas[0]
            or offset_y + frame_rect[3] > canvas[1]
        ):
            raise SpriteExtractionError(f"{name} crop does not fit its anchored canvas")
        for other_name, other_rect in occupied.setdefault(sheet_name, []):
            if _intersects(frame_rect, other_rect):
                raise SpriteExtractionError(
                    f"overlapping frame rectangles: {other_name} and {name}"
                )
        occupied[sheet_name].append((name, frame_rect))
        for exclusion in sheets[sheet_name]["excluded_rects"]:
            if _intersects(frame_rect, _rect(exclusion, "excluded rectangle")):
                raise SpriteExtractionError(
                    f"{name} crop touches an excluded label or credit panel"
                )
        animation_key = (family, animation)
        alignment = (canvas, anchor)
        prior = animations.setdefault(animation_key, alignment)
        if prior != alignment:
            raise SpriteExtractionError(
                f"{family}/{animation} frames need one canvas and anchor"
            )
    return manifest


def load_manifest(path: Path = MANIFEST_PATH) -> tuple[dict, bytes]:
    try:
        data = path.read_bytes()
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpriteExtractionError(f"cannot read manifest {path}: {exc}") from exc
    return validate_manifest(value), data


def _crop_and_remove_matte(
    source: Image,
    rect: tuple[int, int, int, int],
    matte: tuple[int, int, int],
) -> Image:
    x, y, width, height = rect
    if x + width > source.width or y + height > source.height:
        raise SpriteExtractionError("frame rectangle falls outside its source sheet")
    pixels = [
        source.pixel(source_x, source_y)
        for source_y in range(y, y + height)
        for source_x in range(x, x + width)
    ]
    connected: set[int] = set()
    queue: deque[tuple[int, int]] = deque()
    for edge_x in range(width):
        queue.append((edge_x, 0))
        queue.append((edge_x, height - 1))
    for edge_y in range(height):
        queue.append((0, edge_y))
        queue.append((width - 1, edge_y))
    while queue:
        current_x, current_y = queue.popleft()
        index = current_y * width + current_x
        if index in connected or pixels[index][:3] != matte:
            continue
        connected.add(index)
        if current_x:
            queue.append((current_x - 1, current_y))
        if current_x + 1 < width:
            queue.append((current_x + 1, current_y))
        if current_y:
            queue.append((current_x, current_y - 1))
        if current_y + 1 < height:
            queue.append((current_x, current_y + 1))
    rgba = tuple(
        (red, green, blue, 0 if index in connected else alpha)
        for index, (red, green, blue, alpha) in enumerate(pixels)
    )
    if not any(pixel[3] for pixel in rgba):
        raise SpriteExtractionError("frame crop contains no opaque artwork")
    return Image(width, height, rgba)


def _place_on_canvas(
    crop: Image,
    canvas: tuple[int, int],
    anchor: tuple[int, int],
) -> Image:
    canvas_width, canvas_height = canvas
    offset_x = anchor[0] - crop.width // 2
    offset_y = anchor[1] - crop.height // 2
    pixels = [(0, 0, 0, 0)] * (canvas_width * canvas_height)
    for y in range(crop.height):
        for x in range(crop.width):
            pixels[(offset_y + y) * canvas_width + offset_x + x] = crop.pixel(x, y)
    return Image(canvas_width, canvas_height, tuple(pixels))


def render_derivatives(
    manifest: dict,
    asset_dir: Path = ASSET_DIR,
) -> list[Derivative]:
    validate_manifest(manifest)
    matte = tuple(manifest["matte"])
    decoded: dict[str, Image] = {}
    for sheet_name, sheet in manifest["sheets"].items():
        path = asset_dir / sheet["asset"]
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SpriteExtractionError(f"cannot read source sheet {path}: {exc}") from exc
        actual_hash = _sha256(data)
        if actual_hash != sheet["sha256"]:
            raise SpriteExtractionError(
                f"source sheet {sheet_name} hash changed: "
                f"expected {sheet['sha256']}, found {actual_hash}"
            )
        decoded[sheet_name] = decode_png(data, f"source sheet {sheet_name}")
    derivatives = []
    for frame in manifest["frames"]:
        rect = tuple(frame["rect"])
        crop = _crop_and_remove_matte(decoded[frame["sheet"]], rect, matte)
        image = _place_on_canvas(
            crop,
            tuple(frame["canvas"]),
            tuple(frame["anchor"]),
        )
        png = encode_png(image)
        derivatives.append(
            Derivative(frame, f"{_md5(png)}.png", png, image)
        )
    return derivatives


FONT = {
    " ": ("000", "000", "000", "000", "000"),
    ",": ("000", "000", "000", "010", "100"),
    "-": ("000", "000", "111", "000", "000"),
    "/": ("001", "001", "010", "100", "100"),
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "010", "100", "111"),
    "3": ("110", "001", "010", "001", "110"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "110"),
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("011", "100", "100", "100", "011"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("011", "100", "101", "101", "011"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "010"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    "O": ("010", "101", "101", "101", "010"),
    "P": ("110", "101", "110", "100", "100"),
    "Q": ("010", "101", "101", "111", "011"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("011", "100", "010", "001", "110"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
}


def _paint(
    pixels: list[tuple[int, int, int, int]],
    width: int,
    height: int,
    x: int,
    y: int,
    color: tuple[int, int, int, int],
) -> None:
    if 0 <= x < width and 0 <= y < height:
        pixels[y * width + x] = color


def _draw_text(
    pixels: list[tuple[int, int, int, int]],
    width: int,
    height: int,
    text: str,
    x: int,
    y: int,
    scale: int = 2,
) -> None:
    cursor = x
    for character in text.upper():
        glyph = FONT.get(character)
        if glyph is None:
            raise SpriteExtractionError(
                f"contact sheet font has no glyph for {character!r}"
            )
        for glyph_y, row in enumerate(glyph):
            for glyph_x, bit in enumerate(row):
                if bit == "1":
                    for delta_y in range(scale):
                        for delta_x in range(scale):
                            _paint(
                                pixels,
                                width,
                                height,
                                cursor + glyph_x * scale + delta_x,
                                y + glyph_y * scale + delta_y,
                                (235, 235, 235, 255),
                            )
        cursor += 4 * scale


def render_contact_sheet(derivatives: list[Derivative]) -> bytes:
    columns = 4
    cell_width = 224
    cell_height = 96
    rows = (len(derivatives) + columns - 1) // columns
    width = columns * cell_width
    height = rows * cell_height
    pixels = [(24, 28, 34, 255)] * (width * height)
    for index, derivative in enumerate(derivatives):
        column = index % columns
        row = index // columns
        cell_x = column * cell_width
        cell_y = row * cell_height
        for x in range(cell_x, cell_x + cell_width):
            _paint(pixels, width, height, x, cell_y, (76, 86, 98, 255))
            _paint(
                pixels,
                width,
                height,
                x,
                cell_y + cell_height - 1,
                (76, 86, 98, 255),
            )
        for y in range(cell_y, cell_y + cell_height):
            _paint(pixels, width, height, cell_x, y, (76, 86, 98, 255))
            _paint(
                pixels,
                width,
                height,
                cell_x + cell_width - 1,
                y,
                (76, 86, 98, 255),
            )
        scale = 3
        image_x = cell_x + 8
        image_y = cell_y + 8
        for source_y in range(derivative.image.height):
            for source_x in range(derivative.image.width):
                color = derivative.image.pixel(source_x, source_y)
                if color[3] == 0:
                    color = (
                        (178, 184, 191, 255)
                        if (source_x + source_y) % 2
                        else (218, 222, 226, 255)
                    )
                for delta_y in range(scale):
                    for delta_x in range(scale):
                        _paint(
                            pixels,
                            width,
                            height,
                            image_x + source_x * scale + delta_x,
                            image_y + source_y * scale + delta_y,
                            color,
                        )
        anchor_x, anchor_y = derivative.frame["anchor"]
        center_x = image_x + anchor_x * scale
        center_y = image_y + anchor_y * scale
        for delta in range(-4, 5):
            _paint(
                pixels,
                width,
                height,
                center_x + delta,
                center_y,
                (255, 64, 96, 255),
            )
            _paint(
                pixels,
                width,
                height,
                center_x,
                center_y + delta,
                (255, 64, 96, 255),
            )
        frame = derivative.frame
        rect = frame["rect"]
        _draw_text(
            pixels,
            width,
            height,
            frame["name"],
            cell_x + 62,
            cell_y + 8,
        )
        _draw_text(
            pixels,
            width,
            height,
            f"R{rect[0]},{rect[1]},{rect[2]}X{rect[3]}",
            cell_x + 62,
            cell_y + 26,
        )
        canvas = frame["canvas"]
        anchor = frame["anchor"]
        _draw_text(
            pixels,
            width,
            height,
            f"C{canvas[0]}X{canvas[1]} A{anchor[0]},{anchor[1]}",
            cell_x + 62,
            cell_y + 44,
        )
    return encode_png(Image(width, height, tuple(pixels)))


def _costume(derivative: Derivative) -> dict:
    anchor_x, anchor_y = derivative.frame["anchor"]
    asset_id = derivative.filename.removesuffix(".png")
    return {
        "name": derivative.frame["name"],
        "bitmapResolution": 1,
        "dataFormat": "png",
        "assetId": asset_id,
        "md5ext": derivative.filename,
        "rotationCenterX": anchor_x,
        "rotationCenterY": anchor_y,
    }


def _generated_target(costumes: list[dict], layer_order: int) -> dict:
    return {
        "isStage": False,
        "name": GENERATED_TARGET,
        "variables": {},
        "lists": {},
        "broadcasts": {},
        "blocks": {},
        "comments": {},
        "currentCostume": 0,
        "costumes": costumes,
        "sounds": [],
        "volume": 100,
        "layerOrder": layer_order,
        "visible": False,
        "x": 0,
        "y": 0,
        "size": 100,
        "direction": 90,
        "draggable": False,
        "rotationStyle": "don't rotate",
    }


def expected_project(
    project: dict,
    derivatives: list[Derivative],
    prior_frame_names: set[str] | None = None,
) -> dict:
    project = json.loads(json.dumps(project))
    managed_names = {
        derivative.frame["name"]
        for derivative in derivatives
    } | (prior_frame_names or set())
    # Preserve the generated target's existing draw layer if it is already in the project, so a
    # SIBLING target another generator adds (e.g. game_director's gameplay targets) cannot shift it
    # — otherwise recomputing max(existing)+1 makes the two generators disagree over this one field
    # and neither reaches a fixpoint (order-independence, arch review 3a). Only when the target is
    # absent (a first extraction) is a fresh top layer assigned.
    prior_layer = next(
        (
            target.get("layerOrder")
            for target in project["targets"]
            if target.get("name") == GENERATED_TARGET and isinstance(target.get("layerOrder"), int)
        ),
        None,
    )
    project["targets"] = [
        target
        for target in project["targets"]
        if target.get("name") != GENERATED_TARGET
    ]
    solvalou = next(
        (target for target in project["targets"] if target.get("name") == "solvalou"),
        None,
    )
    if solvalou is None:
        raise SpriteExtractionError("Scratch project has no solvalou target")
    solvalou["costumes"] = [
        costume
        for costume in solvalou["costumes"]
        if costume.get("name") not in managed_names
    ]
    by_target: dict[str, list[dict]] = {}
    for derivative in derivatives:
        by_target.setdefault(derivative.frame["target"], []).append(
            _costume(derivative)
        )
    solvalou["costumes"].extend(by_target.get("solvalou", []))
    existing_orders = [
        target.get("layerOrder")
        for target in project["targets"]
        if isinstance(target.get("layerOrder"), int)
    ]
    generated = _generated_target(
        by_target.get(GENERATED_TARGET, []),
        prior_layer if prior_layer is not None else max(existing_orders, default=-1) + 1,
    )
    insertion = next(
        (
            index
            for index, target in enumerate(project["targets"])
            if target.get("name") == "sprite_sheets"
        ),
        len(project["targets"]),
    )
    project["targets"].insert(insertion, generated)
    return project


def _derivative_provenance(
    manifest: dict,
    manifest_bytes: bytes,
    derivatives: list[Derivative],
    contact_sheet: bytes,
) -> dict:
    outputs = {}
    for derivative in derivatives:
        frame = derivative.frame
        sheet = manifest["sheets"][frame["sheet"]]
        outputs[derivative.filename] = {
            "frame": frame["name"],
            "target": frame["target"],
            "source_asset": sheet["asset"],
            "source_sha256": sheet["sha256"],
            "source": sheet["source"],
            "credit": sheet["credit"],
            "license": sheet["license"],
            "rect": frame["rect"],
            "canvas": frame["canvas"],
            "anchor": frame["anchor"],
            "generator_version": GENERATOR_VERSION,
        }
    return {
        "version": 1,
        "generator_version": GENERATOR_VERSION,
        "manifest_sha256": _sha256(manifest_bytes),
        "outputs": outputs,
        "contact_sheet": {
            "file": CONTACT_SHEET_PATH.relative_to(ROOT).as_posix(),
            "sha256": _sha256(contact_sheet),
            "frames": [derivative.frame["name"] for derivative in derivatives],
        },
    }


def _overlay_record(manifest: dict, derivative: Derivative) -> dict:
    frame = derivative.frame
    sheet = manifest["sheets"][frame["sheet"]]
    return {
        "origin": (
            f"Gameplay-ready derivative of {sheet['source']}; "
            f"frame {frame['name']} generated by "
            "assets/sprite-extraction/manifest.json"
        ),
        "license": sheet["license"],
        "notes": (
            f"Sheet credit: {sheet['credit']}. The repository operator did not "
            f"create this asset. Source {sheet['asset']} at SHA-256 "
            f"{sheet['sha256']}; crop {frame['rect']}, canvas {frame['canvas']}, "
            f"anchor {frame['anchor']}."
        ),
    }


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpriteExtractionError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SpriteExtractionError(f"{path} must contain one JSON object")
    return value


def _prior_output_records() -> dict[str, dict]:
    if not DERIVATIVE_PROVENANCE_PATH.exists():
        return {}
    prior = _read_json(DERIVATIVE_PROVENANCE_PATH)
    outputs = prior.get("outputs")
    if not isinstance(outputs, dict):
        raise SpriteExtractionError(
            f"{DERIVATIVE_PROVENANCE_PATH} has no outputs object"
        )
    if any(
        not isinstance(filename, str) or not isinstance(record, dict)
        for filename, record in outputs.items()
    ):
        raise SpriteExtractionError(
            f"{DERIVATIVE_PROVENANCE_PATH} has invalid output records"
        )
    return outputs


def _expected_state() -> tuple[
    list[Derivative],
    bytes,
    bytes,
    bytes,
    bytes,
    set[str],
]:
    manifest, manifest_bytes = load_manifest()
    derivatives = render_derivatives(manifest)
    contact_sheet = render_contact_sheet(derivatives)
    prior_records = _prior_output_records()
    prior_outputs = set(prior_records)
    prior_frame_names = {
        record["frame"]
        for record in prior_records.values()
        if isinstance(record.get("frame"), str)
    }
    current_project = _read_json(PROJECT_PATH)
    project_bytes = _ordered_json_bytes(
        expected_project(current_project, derivatives, prior_frame_names)
    )
    overlay = _read_json(OVERLAY_PROVENANCE_PATH)
    if overlay.get("version") != 1 or not isinstance(overlay.get("assets"), dict):
        raise SpriteExtractionError("overlay provenance must use version 1")
    assets = {
        name: record
        for name, record in overlay["assets"].items()
        if name not in prior_outputs
    }
    for derivative in derivatives:
        assets[derivative.filename] = _overlay_record(manifest, derivative)
    assets = dict(sorted(assets.items()))
    overlay_bytes = _ordered_json_bytes({"version": 1, "assets": assets})
    derivative_provenance = _derivative_provenance(
        manifest,
        manifest_bytes,
        derivatives,
        contact_sheet,
    )
    derivative_provenance_bytes = _ordered_json_bytes(derivative_provenance)
    return (
        derivatives,
        contact_sheet,
        project_bytes,
        overlay_bytes,
        derivative_provenance_bytes,
        prior_outputs,
    )


def generate() -> None:
    (
        derivatives,
        contact_sheet,
        project_bytes,
        overlay_bytes,
        derivative_provenance_bytes,
        prior_outputs,
    ) = _expected_state()
    expected_names = {derivative.filename for derivative in derivatives}
    for stale in sorted(prior_outputs - expected_names):
        stale_path = ASSET_DIR / stale
        if stale_path.is_file() and not stale_path.is_symlink():
            stale_path.unlink()
    for derivative in derivatives:
        (ASSET_DIR / derivative.filename).write_bytes(derivative.png)
    PROJECT_PATH.write_bytes(project_bytes)
    OVERLAY_PROVENANCE_PATH.write_bytes(overlay_bytes)
    DERIVATIVE_PROVENANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DERIVATIVE_PROVENANCE_PATH.write_bytes(derivative_provenance_bytes)
    CONTACT_SHEET_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTACT_SHEET_PATH.write_bytes(contact_sheet)
    check_repository()


def _require_bytes(path: Path, expected: bytes) -> None:
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise SpriteExtractionError(f"missing generated output {path}") from exc
    if actual != expected:
        raise SpriteExtractionError(
            f"generated output is stale; run sprite_extractor.py generate: {path}"
        )


def check_repository() -> tuple[int, str]:
    (
        derivatives,
        contact_sheet,
        project_bytes,
        overlay_bytes,
        derivative_provenance_bytes,
        prior_outputs,
    ) = _expected_state()
    expected_names = {derivative.filename for derivative in derivatives}
    stale = prior_outputs - expected_names
    if stale:
        raise SpriteExtractionError(
            "stale generated sprite assets: " + ", ".join(sorted(stale))
        )
    for derivative in derivatives:
        _require_bytes(ASSET_DIR / derivative.filename, derivative.png)
    _require_bytes(CONTACT_SHEET_PATH, contact_sheet)
    _require_bytes(PROJECT_PATH, project_bytes)
    _require_bytes(OVERLAY_PROVENANCE_PATH, overlay_bytes)
    _require_bytes(DERIVATIVE_PROVENANCE_PATH, derivative_provenance_bytes)
    return len(derivatives), _sha256(contact_sheet)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "check"))
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            generate()
            count, contact_hash = check_repository()
            print(f"generated and verified {count} costumes")
        else:
            count, contact_hash = check_repository()
            print(f"verified {count} costumes")
        print(f"contact sheet SHA-256: {contact_hash}")
        return 0
    except (SpriteExtractionError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
