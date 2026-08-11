#!/usr/bin/env python3
"""Generate deterministic Scratch costumes for the HUD glyph set and life icon,
and attach the extend/1UP sound to the Stage.

Media (docs/mechanics/010-hud-glyph-assets.md, docs/mechanics/012-hud.md): this
generator owns the `hud` target's costumes — the white digit/glyph set every
digit and label but "HIGH SCORE" switches between, the yellow hs/* set the
"HIGH SCORE" label switches to (arcade fidelity: that one HUD label renders
yellow, everything else white), and the life/ship icon — plus the Stage's
`extend` sound, played on every bonus-life grant. tools/game_director.py's
hud_blocks() is the reader: it switches these glyph costumes every frame (the
score/high-score digit roles) or once at spawn (the life icon and the two
label rows), and its check-bonus-life path plays the extend sound. It mirrors
tools/sprite_extractor.py's structure and reuses its low-level PNG/Image
helpers, but owns a different manifest (assets/hud-font/manifest.json) built
for a monospace glyph cell rather than per-animation sprite frames.

Ownership (see tools/game_director.py HUD_TARGET comment): tools/game_director.py
owns the `hud` target's EXISTENCE and BLOCKS. This module owns that target's
COSTUMES only, and separately owns the Stage's `extend` sound entry. Every other
target, and every other field of the `hud` target and the Stage, is left untouched.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import io
import json
from pathlib import Path
import re
import sys
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sprite_extractor as se  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = ROOT / "assets" / "hud-font"
MANIFEST_PATH = FONT_DIR / "manifest.json"
DERIVATIVE_PROVENANCE_PATH = FONT_DIR / "provenance.json"
SOUND_SOURCE_PATH = ROOT / "assets" / "hud-sounds" / "extend.wav"
ASSET_DIR = ROOT / "src" / "xevious" / "assets"
OVERLAY_PROVENANCE_PATH = ASSET_DIR / "provenance.json"
PROJECT_PATH = ROOT / "src" / "xevious" / "project.json"
GENERATOR_VERSION = 1
HUD_TARGET = "hud"
SOUND_NAME = "extend"
GLYPH_NAME = re.compile(r"^(?:digit|glyph)/[0-9A-Z]$")

# Fixed monospace cell every glyph is centered on before downscaling, and the
# nearest-neighbor factor applied to the assembled cell. 100 is the smallest
# multiple of 4 that comfortably holds the sheet's widest measured glyph rect
# (98 px), leaving a symmetric 1 px margin; 100 / 4 = 25 divides evenly, so the
# decimation below is exact with no rounding.
DOWNSCALE = 4
# Costumes present in this exact order on the `hud` target (digits, then the
# uppercase letters HUD readouts need, then the life icon). Decoupled from the
# manifest's own storage order so re-ordering the manifest can never reorder
# the generated project.
COSTUME_ORDER = [
    "digit/0", "digit/1", "digit/2", "digit/3", "digit/4",
    "digit/5", "digit/6", "digit/7", "digit/8", "digit/9",
    "glyph/A", "glyph/C", "glyph/E", "glyph/G", "glyph/H", "glyph/I",
    "glyph/M", "glyph/O", "glyph/P", "glyph/R", "glyph/S", "glyph/U", "glyph/V",
    "hs/C", "hs/E", "hs/G", "hs/H", "hs/I", "hs/O", "hs/R", "hs/S",
    "life/ship",
]

# The 8 letters in "HIGH SCORE" (H, I, G, S, C, O, R, E — H used twice in the label, one
# costume), recolored YELLOW instead of white: the arcade HUD renders that one label yellow,
# everything else white. Each hs/<letter> reuses the SAME source rect as its white
# glyph/<letter> counterpart (see _glyph_source below) — a pure recolor, never a new crop.
HS_LETTERS = ("C", "E", "G", "H", "I", "O", "R", "S")
WHITE_INK = (255, 255, 255, 255)
YELLOW_INK = (255, 255, 0, 255)
_INK_BY_RECOLOR = {"white": WHITE_INK, "yellow": YELLOW_INK}


class HudGlyphsError(RuntimeError):
    """The HUD glyph manifest or a generated output is invalid."""


@dataclass(frozen=True)
class GlyphOutput:
    name: str
    filename: str
    png: bytes
    size: int  # square final bitmap edge length, in pixels


@dataclass(frozen=True)
class LifeIconOutput:
    name: str
    filename: str
    png: bytes
    canvas: tuple[int, int]
    anchor: tuple[int, int]


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
        raise HudGlyphsError(f"{label} fields are invalid: {'; '.join(details)}")


def _sheet_record(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise HudGlyphsError(f"{label} must be an object")
    _require_keys(value, {"asset", "sha256", "source", "credit", "license"}, label)
    if not isinstance(value["asset"], str) or not value["asset"]:
        raise HudGlyphsError(f"{label} has no asset path")
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise HudGlyphsError(f"{label} has an invalid SHA-256")
    for field in ("source", "credit", "license"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise HudGlyphsError(f"{label} has no recorded {field}")
    return value


def validate_manifest(manifest: object) -> dict:
    if not isinstance(manifest, dict):
        raise HudGlyphsError("HUD font manifest must be one JSON object")
    _require_keys(
        manifest,
        {
            "version",
            "generator_version",
            "font_sheet",
            "life_icon_sheet",
            "extend_sound",
            "matte",
            "cell_canvas",
            "cell_anchor",
            "downscale",
            "bitmap_resolution",
            "glyph_threshold",
            "glyphs",
            "life_icon",
        },
        "manifest",
    )
    if manifest.get("version") != 1:
        raise HudGlyphsError("manifest must use version 1")
    if manifest.get("generator_version") != GENERATOR_VERSION:
        raise HudGlyphsError(f"manifest must select generator version {GENERATOR_VERSION}")
    _sheet_record(manifest["font_sheet"], "font_sheet")
    _sheet_record(manifest["life_icon_sheet"], "life_icon_sheet")
    _sheet_record(manifest["extend_sound"], "extend_sound")
    matte = manifest.get("matte")
    if (
        not isinstance(matte, list)
        or len(matte) != 3
        or any(not isinstance(channel, int) or not 0 <= channel <= 255 for channel in matte)
    ):
        raise HudGlyphsError("manifest matte must contain three byte values")
    canvas = manifest.get("cell_canvas")
    anchor = manifest.get("cell_anchor")
    if (
        not isinstance(canvas, list)
        or len(canvas) != 2
        or any(not isinstance(value, int) or value <= 0 for value in canvas)
    ):
        raise HudGlyphsError("manifest cell_canvas must be two positive integers")
    if (
        not isinstance(anchor, list)
        or len(anchor) != 2
        or any(not isinstance(value, int) or value < 0 for value in anchor)
    ):
        raise HudGlyphsError("manifest cell_anchor must be two non-negative integers")
    downscale = manifest.get("downscale")
    if not isinstance(downscale, int) or downscale <= 0:
        raise HudGlyphsError("manifest downscale must be a positive integer")
    if canvas[0] % downscale or canvas[1] % downscale:
        raise HudGlyphsError("cell_canvas must be evenly divisible by downscale")
    resolution = manifest.get("bitmap_resolution")
    if not isinstance(resolution, (int, float)) or resolution <= 0:
        raise HudGlyphsError("manifest bitmap_resolution must be a positive number")
    threshold = manifest.get("glyph_threshold")
    if not isinstance(threshold, int) or not 0 < threshold <= 255:
        raise HudGlyphsError("manifest glyph_threshold must be an integer in 1-255")
    glyphs = manifest.get("glyphs")
    if not isinstance(glyphs, list) or not glyphs:
        raise HudGlyphsError("manifest must contain a non-empty glyphs list")
    names: set[str] = set()
    for index, glyph in enumerate(glyphs):
        label = f"glyphs[{index}]"
        if not isinstance(glyph, dict):
            raise HudGlyphsError(f"{label} must be an object")
        _require_keys(glyph, {"name", "rect"}, label)
        name = glyph.get("name")
        if not isinstance(name, str) or not GLYPH_NAME.fullmatch(name):
            raise HudGlyphsError(f"{label} has an invalid glyph name")
        if name in names:
            raise HudGlyphsError(f"duplicate glyph name: {name}")
        names.add(name)
        rect = glyph.get("rect")
        if (
            not isinstance(rect, list)
            or len(rect) != 4
            or any(not isinstance(value, int) for value in rect)
        ):
            raise HudGlyphsError(f"{label} rect must be four integers")
        x0, y0, x1, y1 = rect
        if x0 < 0 or y0 < 0 or x1 < x0 or y1 < y0:
            raise HudGlyphsError(f"{label} rect must be an ordered inclusive box")
        if (x1 - x0 + 1) > canvas[0] or (y1 - y0 + 1) > canvas[1]:
            raise HudGlyphsError(f"{label} crop does not fit the cell canvas")
    # The manifest defines only the white digit/glyph set; the yellow hs/* "HIGH SCORE"
    # costumes are derived from those same entries at render time (see _glyph_source), so
    # they are never named in the manifest itself.
    expected_names = {
        entry
        for entry in COSTUME_ORDER
        if entry != "life/ship" and not entry.startswith("hs/")
    }
    if names != expected_names:
        raise HudGlyphsError(
            "manifest glyphs must name exactly the required digit/letter set: "
            + ", ".join(sorted(expected_names))
        )
    life_icon = manifest.get("life_icon")
    if not isinstance(life_icon, dict):
        raise HudGlyphsError("manifest life_icon must be an object")
    _require_keys(life_icon, {"name", "rect", "canvas", "anchor"}, "life_icon")
    if life_icon.get("name") != "life/ship":
        raise HudGlyphsError("life_icon name must be life/ship")
    life_rect = life_icon.get("rect")
    if (
        not isinstance(life_rect, list)
        or len(life_rect) != 4
        or any(not isinstance(value, int) for value in life_rect)
    ):
        raise HudGlyphsError("life_icon rect must be four integers")
    return manifest


def load_manifest(path: Path = MANIFEST_PATH) -> tuple[dict, bytes]:
    try:
        data = path.read_bytes()
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HudGlyphsError(f"cannot read HUD font manifest {path}: {exc}") from exc
    return validate_manifest(value), data


def _binarize_glyph(
    source: se.Image,
    rect: tuple[int, int, int, int],
    threshold: int,
    ink: tuple[int, int, int, int] = WHITE_INK,
) -> se.Image:
    x0, y0, x1, y1 = rect
    if x1 >= source.width or y1 >= source.height:
        raise HudGlyphsError(f"glyph rect {rect} falls outside the font sheet")
    width = x1 - x0 + 1
    height = y1 - y0 + 1
    pixels = []
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            red, green, blue, _alpha = source.pixel(x, y)
            if red >= threshold or green >= threshold or blue >= threshold:
                pixels.append((0, 0, 0, 0))
            else:
                pixels.append(ink)
    if not any(pixel[3] for pixel in pixels):
        raise HudGlyphsError(f"glyph rect {rect} produced no ink pixels")
    return se.Image(width, height, tuple(pixels))


def _downscale_nearest(image: se.Image, factor: int) -> se.Image:
    if image.width % factor or image.height % factor:
        raise HudGlyphsError("cannot downscale an image whose size is not a multiple of factor")
    new_width = image.width // factor
    new_height = image.height // factor
    pixels = [
        image.pixel(x * factor, y * factor)
        for y in range(new_height)
        for x in range(new_width)
    ]
    return se.Image(new_width, new_height, tuple(pixels))


def _glyph_source(manifest: dict, name: str) -> tuple[dict, str]:
    """Return (manifest glyph entry providing the source rect, recolor label) for a
    rendered costume name. The 8 yellow hs/<letter> costumes reuse the SAME rect as
    their white glyph/<letter> counterpart — a pure recolor, never a new crop."""
    if name.startswith("hs/"):
        letter = name.split("/", 1)[1]
        source_name = f"glyph/{letter}"
        recolor = "yellow"
    else:
        source_name = name
        recolor = "white"
    glyph = next(g for g in manifest["glyphs"] if g["name"] == source_name)
    return glyph, recolor


def render_glyphs(manifest: dict) -> list[GlyphOutput]:
    validate_manifest(manifest)
    sheet_record = manifest["font_sheet"]
    sheet_path = FONT_DIR / sheet_record["asset"]
    try:
        sheet_bytes = sheet_path.read_bytes()
    except OSError as exc:
        raise HudGlyphsError(f"cannot read font sheet {sheet_path}: {exc}") from exc
    actual_hash = se._sha256(sheet_bytes)
    if actual_hash != sheet_record["sha256"]:
        raise HudGlyphsError(
            f"font sheet hash changed: expected {sheet_record['sha256']}, found {actual_hash}"
        )
    sheet = se.decode_png(sheet_bytes, "HUD font sheet")
    canvas = tuple(manifest["cell_canvas"])
    anchor = tuple(manifest["cell_anchor"])
    factor = manifest["downscale"]
    threshold = manifest["glyph_threshold"]

    def render_one(name: str) -> GlyphOutput:
        glyph, recolor = _glyph_source(manifest, name)
        rect = tuple(glyph["rect"])
        crop = _binarize_glyph(sheet, rect, threshold, _INK_BY_RECOLOR[recolor])
        placed = se._place_on_canvas(crop, canvas, anchor)
        final = _downscale_nearest(placed, factor)
        png = se.encode_png(final)
        return GlyphOutput(name, f"{se._md5(png)}.png", png, final.width)

    # The manifest's own white digit/glyph set, in manifest order, then the 8 yellow
    # hs/* "HIGH SCORE" letters, in HS_LETTERS order.
    outputs = [render_one(glyph["name"]) for glyph in manifest["glyphs"]]
    outputs += [render_one(f"hs/{letter}") for letter in HS_LETTERS]
    return outputs


def render_life_icon(manifest: dict) -> LifeIconOutput:
    validate_manifest(manifest)
    sheet_record = manifest["life_icon_sheet"]
    sheet_path = ROOT / sheet_record["asset"]
    try:
        sheet_bytes = sheet_path.read_bytes()
    except OSError as exc:
        raise HudGlyphsError(f"cannot read life icon sheet {sheet_path}: {exc}") from exc
    actual_hash = se._sha256(sheet_bytes)
    if actual_hash != sheet_record["sha256"]:
        raise HudGlyphsError(
            f"life icon sheet hash changed: expected {sheet_record['sha256']}, found {actual_hash}"
        )
    sheet = se.decode_png(sheet_bytes, "life icon sheet")
    matte = tuple(manifest["matte"])
    life_icon = manifest["life_icon"]
    rect = tuple(life_icon["rect"])
    canvas = tuple(life_icon["canvas"])
    anchor = tuple(life_icon["anchor"])
    crop = se._crop_and_remove_matte(sheet, rect, matte)
    placed = se._place_on_canvas(crop, canvas, anchor)
    png = se.encode_png(placed)
    return LifeIconOutput(life_icon["name"], f"{se._md5(png)}.png", png, canvas, anchor)


def _read_sound_source() -> bytes:
    try:
        data = SOUND_SOURCE_PATH.read_bytes()
    except OSError as exc:
        raise HudGlyphsError(f"cannot read extend sound source {SOUND_SOURCE_PATH}: {exc}") from exc
    return data


def render_extend_sound(manifest: dict) -> tuple[dict, bytes, str]:
    """Return (Scratch sound dict, wav bytes, filename)."""
    validate_manifest(manifest)
    record = manifest["extend_sound"]
    data = _read_sound_source()
    actual_hash = se._sha256(data)
    if actual_hash != record["sha256"]:
        raise HudGlyphsError(
            f"extend sound hash changed: expected {record['sha256']}, found {actual_hash}"
        )
    if not (data.startswith(b"RIFF") and data[8:12] == b"WAVE"):
        raise HudGlyphsError("extend sound source is not a RIFF/WAVE file")
    try:
        with wave.open(io.BytesIO(data)) as handle:
            frame_count = handle.getnframes()
            rate = handle.getframerate()
            channels = handle.getnchannels()
    except wave.Error as exc:
        raise HudGlyphsError(f"cannot parse extend sound WAV header: {exc}") from exc
    if channels not in (1, 2):
        raise HudGlyphsError(f"extend sound has an unexpected channel count: {channels}")
    asset_id = se._md5(data)
    filename = f"{asset_id}.wav"
    sound = {
        "name": SOUND_NAME,
        "assetId": asset_id,
        "dataFormat": "wav",
        "format": "",
        "rate": rate,
        "sampleCount": frame_count,
        "md5ext": filename,
    }
    return sound, data, filename


def _glyph_costume(output: GlyphOutput, manifest: dict) -> dict:
    factor = manifest["downscale"]
    anchor = manifest["cell_anchor"]
    center_x = anchor[0] / factor
    center_y = anchor[1] / factor
    return {
        "name": output.name,
        "bitmapResolution": manifest["bitmap_resolution"],
        "dataFormat": "png",
        "assetId": output.filename.removesuffix(".png"),
        "md5ext": output.filename,
        "rotationCenterX": center_x,
        "rotationCenterY": center_y,
    }


def _life_costume(output: LifeIconOutput) -> dict:
    return {
        "name": output.name,
        "bitmapResolution": 1,
        "dataFormat": "png",
        "assetId": output.filename.removesuffix(".png"),
        "md5ext": output.filename,
        "rotationCenterX": output.anchor[0],
        "rotationCenterY": output.anchor[1],
    }


def expected_project(
    project: dict,
    glyph_outputs: list[GlyphOutput],
    life_output: LifeIconOutput,
    manifest: dict,
    sound: dict,
) -> dict:
    result = copy.deepcopy(project)
    hud = next((target for target in result["targets"] if target.get("name") == HUD_TARGET), None)
    if hud is None:
        raise HudGlyphsError(
            "Scratch project has no hud target; run tools/game_director.py generate first"
        )
    costumes_by_name = {output.name: _glyph_costume(output, manifest) for output in glyph_outputs}
    costumes_by_name[life_output.name] = _life_costume(life_output)
    missing = set(COSTUME_ORDER) - set(costumes_by_name)
    if missing:
        raise HudGlyphsError(f"missing rendered costumes: {', '.join(sorted(missing))}")
    hud["costumes"] = [costumes_by_name[name] for name in COSTUME_ORDER]
    stage = next(target for target in result["targets"] if target.get("isStage"))
    stage["sounds"] = [
        entry for entry in stage["sounds"] if entry.get("name") != SOUND_NAME
    ] + [sound]
    return result


def _overlay_glyph_record(manifest: dict, output: GlyphOutput) -> dict:
    sheet = manifest["font_sheet"]
    source_glyph, recolor = _glyph_source(manifest, output.name)
    return {
        "origin": (
            f"Recolored, monospace-cell derivative of {sheet['source']}; "
            f"glyph {output.name} generated by assets/hud-font/manifest.json"
        ),
        "license": sheet["license"],
        "notes": (
            f"Credit: {sheet['credit']}. The repository operator did not create this "
            f"asset. Source {sheet['asset']} at SHA-256 {sheet['sha256']}; crop "
            f"{source_glyph['rect']}, cell canvas {manifest['cell_canvas']}, cell anchor "
            f"{manifest['cell_anchor']}, {manifest['downscale']}x nearest-neighbor "
            f"downscale, bitmapResolution {manifest['bitmap_resolution']}, "
            f"recolor={recolor}."
        ),
    }


def _overlay_life_record(manifest: dict, output: LifeIconOutput) -> dict:
    sheet = manifest["life_icon_sheet"]
    life_icon = manifest["life_icon"]
    return {
        "origin": (
            f"Gameplay-ready derivative of {sheet['source']}; frame {output.name} "
            "generated by assets/hud-font/manifest.json"
        ),
        "license": sheet["license"],
        "notes": (
            f"Sheet credit: {sheet['credit']}. The repository operator did not create "
            f"this asset. Source {sheet['asset']} at SHA-256 {sheet['sha256']}; crop "
            f"{life_icon['rect']}, canvas {life_icon['canvas']}, anchor {life_icon['anchor']}."
        ),
    }


def _overlay_sound_record(manifest: dict, filename: str) -> dict:
    record = manifest["extend_sound"]
    return {
        "origin": (
            f"Unmodified copy of {record['source']}; committed at "
            "assets/hud-sounds/extend.wav and attached as the Stage 'extend' sound"
        ),
        "license": record["license"],
        "notes": (
            f"Credit: {record['credit']}. The repository operator did not create this "
            f"asset. Source SHA-256 {record['sha256']}; no audio transformation applied."
        ),
    }


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HudGlyphsError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HudGlyphsError(f"{path} must contain one JSON object")
    return value


def _prior_output_records() -> dict[str, dict]:
    if not DERIVATIVE_PROVENANCE_PATH.exists():
        return {}
    prior = _read_json(DERIVATIVE_PROVENANCE_PATH)
    outputs = prior.get("outputs")
    if not isinstance(outputs, dict):
        raise HudGlyphsError(f"{DERIVATIVE_PROVENANCE_PATH} has no outputs object")
    return outputs


def _derivative_provenance(
    manifest: dict,
    manifest_bytes: bytes,
    glyph_outputs: list[GlyphOutput],
    life_output: LifeIconOutput,
    sound_filename: str,
) -> dict:
    outputs = {}
    for output in glyph_outputs:
        source_glyph, recolor = _glyph_source(manifest, output.name)
        outputs[output.filename] = {
            "kind": "glyph",
            "name": output.name,
            "rect": source_glyph["rect"],
            "recolor": recolor,
            "generator_version": GENERATOR_VERSION,
        }
    outputs[life_output.filename] = {
        "kind": "life_icon",
        "name": life_output.name,
        "rect": manifest["life_icon"]["rect"],
        "generator_version": GENERATOR_VERSION,
    }
    outputs[sound_filename] = {
        "kind": "sound",
        "name": SOUND_NAME,
        "generator_version": GENERATOR_VERSION,
    }
    return {
        "version": 1,
        "generator_version": GENERATOR_VERSION,
        "manifest_sha256": se._sha256(manifest_bytes),
        "outputs": outputs,
    }


def _expected_state() -> tuple[
    list[GlyphOutput],
    LifeIconOutput,
    dict,
    bytes,
    bytes,
    bytes,
    bytes,
    set[str],
]:
    manifest, manifest_bytes = load_manifest()
    glyph_outputs = render_glyphs(manifest)
    life_output = render_life_icon(manifest)
    sound, sound_bytes, sound_filename = render_extend_sound(manifest)
    prior_outputs = set(_prior_output_records())
    current_project = _read_json(PROJECT_PATH)
    project_bytes = se._ordered_json_bytes(
        expected_project(current_project, glyph_outputs, life_output, manifest, sound)
    )
    overlay = _read_json(OVERLAY_PROVENANCE_PATH)
    if overlay.get("version") != 1 or not isinstance(overlay.get("assets"), dict):
        raise HudGlyphsError("overlay provenance must use version 1")
    assets = {
        name: record
        for name, record in overlay["assets"].items()
        if name not in prior_outputs
    }
    for output in glyph_outputs:
        assets[output.filename] = _overlay_glyph_record(manifest, output)
    assets[life_output.filename] = _overlay_life_record(manifest, life_output)
    assets[sound_filename] = _overlay_sound_record(manifest, sound_filename)
    assets = dict(sorted(assets.items()))
    overlay_bytes = se._ordered_json_bytes({"version": 1, "assets": assets})
    derivative_provenance_bytes = se._ordered_json_bytes(
        _derivative_provenance(manifest, manifest_bytes, glyph_outputs, life_output, sound_filename)
    )
    return (
        glyph_outputs,
        life_output,
        {"sound": sound, "bytes": sound_bytes, "filename": sound_filename},
        project_bytes,
        overlay_bytes,
        derivative_provenance_bytes,
        sound_bytes,
        prior_outputs,
    )


def generate() -> None:
    (
        glyph_outputs,
        life_output,
        sound_info,
        project_bytes,
        overlay_bytes,
        derivative_provenance_bytes,
        sound_bytes,
        prior_outputs,
    ) = _expected_state()
    expected_names = {output.filename for output in glyph_outputs} | {
        life_output.filename,
        sound_info["filename"],
    }
    for stale in sorted(prior_outputs - expected_names):
        stale_path = ASSET_DIR / stale
        if stale_path.is_file() and not stale_path.is_symlink():
            stale_path.unlink()
    for output in glyph_outputs:
        (ASSET_DIR / output.filename).write_bytes(output.png)
    (ASSET_DIR / life_output.filename).write_bytes(life_output.png)
    (ASSET_DIR / sound_info["filename"]).write_bytes(sound_bytes)
    PROJECT_PATH.write_bytes(project_bytes)
    OVERLAY_PROVENANCE_PATH.write_bytes(overlay_bytes)
    DERIVATIVE_PROVENANCE_PATH.write_bytes(derivative_provenance_bytes)
    check_repository()


def _require_bytes(path: Path, expected: bytes) -> None:
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise HudGlyphsError(f"missing generated output {path}") from exc
    if actual != expected:
        raise HudGlyphsError(
            f"generated output is stale; run hud_glyphs.py generate: {path}"
        )


def check_repository() -> int:
    (
        glyph_outputs,
        life_output,
        sound_info,
        project_bytes,
        overlay_bytes,
        derivative_provenance_bytes,
        sound_bytes,
        prior_outputs,
    ) = _expected_state()
    expected_names = {output.filename for output in glyph_outputs} | {
        life_output.filename,
        sound_info["filename"],
    }
    stale = prior_outputs - expected_names
    if stale:
        raise HudGlyphsError("stale generated HUD assets: " + ", ".join(sorted(stale)))
    for output in glyph_outputs:
        _require_bytes(ASSET_DIR / output.filename, output.png)
    _require_bytes(ASSET_DIR / life_output.filename, life_output.png)
    _require_bytes(ASSET_DIR / sound_info["filename"], sound_bytes)
    _require_bytes(PROJECT_PATH, project_bytes)
    _require_bytes(OVERLAY_PROVENANCE_PATH, overlay_bytes)
    _require_bytes(DERIVATIVE_PROVENANCE_PATH, derivative_provenance_bytes)
    return len(glyph_outputs) + 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "check"))
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            generate()
            count = check_repository()
            print(f"generated and verified {count} costumes")
        else:
            count = check_repository()
            print(f"verified {count} costumes")
        return 0
    except (HudGlyphsError, se.SpriteExtractionError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
