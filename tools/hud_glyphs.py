#!/usr/bin/env python3
"""Generate deterministic Scratch costumes for the HUD glyph set and life icon,
and attach the added Stage sounds (the extend/1UP cue and the arcade gameplay SFX).

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
COSTUMES only, and separately owns the Stage's ADDED sounds — the `extend` cue and
the arcade gameplay SFX committed under assets/game-sounds/ (see load_game_sounds_
manifest / render_game_sounds). Those SFX are gameplay sounds, not HUD glyphs; they
live here only because this module is already the single writer of project.json and
the overlay provenance, and game_director's play points reference them by name. The
base music/start sounds and every other target and field are left untouched.
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
# AUDIO: the arcade gameplay sound effects, committed byte-for-byte under assets/game-sounds/ and
# attached (unmodified) as additional Stage sounds. This module already owns the Stage's single added
# sound (`extend`) plus the final project.json / overlay-provenance write, so it is the natural single
# writer for these too. They are gameplay SFX, not HUD glyphs — kept here only to preserve one writer.
GAME_SOUNDS_DIR = ROOT / "assets" / "game-sounds"
GAME_SOUNDS_MANIFEST_PATH = GAME_SOUNDS_DIR / "manifest.json"
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


@dataclass(frozen=True)
class GameSoundOutput:
    name: str  # Stage sound name, referenced by tools/game_director.py play points
    filename: str  # content-hash <md5>.wav under src/xevious/assets/
    sound: dict  # the Scratch sound dict attached to the Stage
    wav: bytes  # the committed source bytes, copied unmodified
    record: dict  # the assets/game-sounds/manifest.json entry (provenance)


# SEC-03 hidden-credit overlay (secrets.hidden-credit #93; game_director.py easter_egg_blocks / display_easter_egg
# xevious_main.68k 6018-6048). game_director owns the `easter-egg` target's existence + blocks; this module owns
# its single COSTUME — a pre-composed two-line credit bitmap the overlay shows for ~2 s when the hidden Credit is
# bombed. The WORDING is this project's own original content (never the arcade str_program_by_EVEZOO text —
# docs/REFERENCE_POLICY.md), but the letterforms are rendered from the SAME high-res Xevious HUD font sheet the
# HUD readouts and the CAB-01 attract text use (assets/hud-font/xevious_hud_font.png, "Xevious HUD font
# recreation" by Patrick H. Lauke, CC-BY 3.0). The operator chose to reuse the one Xevious font already in the
# project everywhere rather than keep a second, self-contained pixel font; the sheet carries the full A-Z set the
# wording needs (the arcade HUD *manifest* only crops the readout subset, but SHEET_TEXT_RECTS below adds the
# rest from the same sheet). Recorded as a port necessity in docs/mechanics/044.
CREDIT_TARGET = "easter-egg"
CREDIT_COSTUME_NAME = "credit"
# This project's own original placeholder wording (operator's choice), uppercase, two lines.
CREDIT_TEXT_LINES = ("XEVIOUS PORT", "BY STARSHIP SUPERJAM")
CREDIT_INK = (255, 255, 255, 255)  # white, legible over the play field
CREDIT_TRANSPARENT = (0, 0, 0, 0)

# CAB-01 attract-screen overlays on the start_screen target (slice 17). Rendered from the SAME high-res
# Xevious HUD font sheet as the SEC-03 credit above and the HUD readouts, and the same project-original
# stance for the CONTENT: the credit counter, the CREDIT / PUSH START / INSERT COIN prompts, and the
# default best-five table are the port's own strings, NOT the ROM's default name strings; only the
# letterforms are the credited CC-BY font. See docs/mechanics 037 (CAB-01).
ATTRACT_TARGET = "start_screen"
ATTRACT_BEST_FIVE_NAME = "best-five"
ATTRACT_LABELS = (
    ("credit-label", "CREDIT"),
    ("push-start", "PUSH START"),
    ("insert-coin", "INSERT COIN"),
)
# The default best-five INITIALS are this project's own placeholder content (the operator's choice),
# NOT the arcade ROM's default name strings (docs/REFERENCE_POLICY.md forbids transcribing in-game
# text). They live here as a source constant — the same home and stance as CREDIT_TEXT_LINES above —
# rather than in the reference-extracted docs/spec/data/scores.json (that file is decode-only, guarded
# by a digest manifest). Only the SCORES they pair with are the reference-derived arcade defaults, and
# those are read from scores.json. Every glyph used here must have a SHEET_TEXT_RECTS entry (a test pins
# it); high-score-entry that would let a player set these stays slice 19 (CAB-04).
ATTRACT_DEFAULT_INITIALS = ("STK", "M.N", "EVE", "S.O", "S.K")
SCORES_DATA_PATH = ROOT / "docs" / "spec" / "data" / "scores.json"

# CAB-01 attract text renders from the SAME high-res Xevious HUD font sheet the HUD score/label
# readouts use (assets/hud-font/xevious_hud_font.png, "Xevious HUD font recreation" by Patrick H.
# Lauke, CC-BY 3.0), NOT a separate port pixel font: the operator chose to reuse the Xevious font
# already in the project rather than add a second one. The sheet carries the full A-Z / 0-9 /
# punctuation set; the HUD manifest only crops the subset the readouts use (see COSTUME_ORDER), so
# these rects add the remaining glyphs the attract strings need — cropped from the same sheet, with
# the same operator-verified crop convention as the manifest (inclusive [x0, y0, x1, y1], the full
# row-band height so every glyph shares one baseline). Bottom-aligning the crops in a fixed cell
# puts caps/digits on that baseline and drops the period to it as a low dot. Segmented from the
# credited sheet and cross-checked against the manifest's own rects (A/C/I match exactly).
SHEET_TEXT_RECTS = {
    "A": [13, 12, 110, 109], "B": [125, 12, 222, 109], "C": [237, 12, 334, 109],
    "D": [349, 12, 446, 109], "E": [461, 12, 558, 109], "F": [573, 12, 670, 109],
    "G": [685, 12, 782, 109], "H": [797, 12, 894, 109], "I": [937, 12, 978, 109],
    "J": [1021, 12, 1090, 109], "K": [1133, 12, 1230, 109], "L": [1245, 12, 1342, 109],
    "M": [1357, 12, 1454, 109], "N": [1469, 12, 1566, 109], "O": [1581, 12, 1678, 109],
    "P": [1693, 12, 1790, 109], "Q": [1805, 12, 1902, 109], "R": [1917, 12, 2014, 109],
    "S": [13, 138, 110, 235], "T": [125, 138, 222, 235], "U": [237, 138, 334, 235],
    "V": [349, 138, 446, 235], "W": [461, 138, 558, 235], "X": [573, 138, 670, 235],
    "Y": [685, 138, 782, 235], "Z": [797, 138, 894, 235],
    "0": [909, 138, 1006, 235], "1": [1035, 138, 1090, 235], "2": [1133, 138, 1230, 235],
    "3": [1245, 138, 1342, 235], "4": [1357, 138, 1454, 235], "5": [1469, 138, 1566, 235],
    "6": [1581, 138, 1678, 235], "7": [1693, 138, 1790, 235], "8": [1805, 138, 1902, 235],
    "9": [1917, 138, 2014, 235],
    ".": [27, 572, 54, 613],
}
# Native (sheet-pixel) cell the crops are laid out on before decimation. 100 clears the widest
# measured glyph rect (98 px) with a symmetric 1 px margin and divides evenly by the downscale, so
# the decimation is exact (mirrors the HUD manifest's 100/4 geometry). One space char = one empty
# advance. Monospace so the best-five rank/initials/score columns line up.
SHEET_TEXT_CELL_W = 100
SHEET_TEXT_CELL_H = 100
SHEET_TEXT_GLYPH_GAP = 8  # native columns between cells; 108 total advance, divisible by 4
SHEET_TEXT_LINE_GAP = 20  # native rows between lines; 120 total, divisible by 4
SHEET_TEXT_DOWNSCALE = 4  # 100 px cell -> 25 px costume cell, matching the HUD glyph pixel size

# The SEC-03 hidden-credit overlay uses the SAME sheet and compositor, but its longest line
# ("BY STARSHIP SUPERJAM", 20 chars) will not fit the 480 px stage at the 27 px attract advance
# (20 * 27 > 540). Render it at a smaller cell so the two-line credit sits within the stage:
# 100 px cell / downscale 5 -> 20 px cells, 22 px advance, so line 2 is 20 * 110 - 10 = 2190 native
# -> 438 px < 480. Gaps chosen divisible by the downscale so the decimation stays exact.
SHEET_CREDIT_GLYPH_GAP = 10  # native columns between cells; 110 advance, divisible by 5
SHEET_CREDIT_LINE_GAP = 20  # native rows between lines; 120 pitch, divisible by 5
SHEET_CREDIT_DOWNSCALE = 5  # 100 px cell -> 20 px costume cell; keeps the widest credit line on-stage


@dataclass(frozen=True)
class CreditOutput:
    name: str
    filename: str  # content-hash <md5>.png under src/xevious/assets/
    png: bytes
    width: int
    height: int


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


def _load_font_sheet(manifest: dict) -> se.Image:
    """Decode the HUD font sheet, verifying it against its recorded SHA-256.

    Shared by render_glyphs (the HUD glyph subset) and render_attract_costumes (the
    attract text lines): both crop the same credited sheet, so both must see the same
    integrity check and the same decoded pixels."""
    record = manifest["font_sheet"]
    sheet_path = FONT_DIR / record["asset"]
    try:
        sheet_bytes = sheet_path.read_bytes()
    except OSError as exc:
        raise HudGlyphsError(f"cannot read font sheet {sheet_path}: {exc}") from exc
    actual_hash = se._sha256(sheet_bytes)
    if actual_hash != record["sha256"]:
        raise HudGlyphsError(
            f"font sheet hash changed: expected {record['sha256']}, found {actual_hash}"
        )
    return se.decode_png(sheet_bytes, "HUD font sheet")


def render_glyphs(manifest: dict) -> list[GlyphOutput]:
    validate_manifest(manifest)
    sheet = _load_font_sheet(manifest)
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


def render_credit(sheet: se.Image, threshold: int) -> CreditOutput:
    """Compose the two-line hidden-credit overlay bitmap (SEC-03) from the high-res Xevious HUD
    font sheet — the same sheet and compositor as the attract text, at the smaller SEC-03 cell
    so the widest line ("BY STARSHIP SUPERJAM") stays within the 480 px stage. The wording is the
    port's own original content; only the letterforms are the credited CC-BY font."""
    return render_sheet_text_costume(
        sheet,
        threshold,
        CREDIT_COSTUME_NAME,
        CREDIT_TEXT_LINES,
        glyph_gap=SHEET_CREDIT_GLYPH_GAP,
        line_gap=SHEET_CREDIT_LINE_GAP,
        downscale=SHEET_CREDIT_DOWNSCALE,
    )


def _load_best_five() -> tuple[list[str], list[int]]:
    """The project's default best-five rows: project-original initials paired with the
    reference-derived arcade default scores.

    The initials are the port's own placeholder content (ATTRACT_DEFAULT_INITIALS, a source
    constant — scores.json is reference-decode-only and cannot carry project-original data).
    The scores are the arcade defaults read from docs/spec/data/scores.json, so the rendered
    table can never drift from the reference data a test pins. The two must be paired
    one-for-one; a mismatch is a wiring error, not a soft fallback."""
    data = json.loads(SCORES_DATA_PATH.read_text())
    scores = data["tables"]["high_score_defaults"].get("scores")
    initials = list(ATTRACT_DEFAULT_INITIALS)
    if not isinstance(scores, list) or not scores:
        raise HudGlyphsError("scores.json high_score_defaults.scores is missing")
    if len(initials) != len(scores):
        raise HudGlyphsError(
            "ATTRACT_DEFAULT_INITIALS and scores.json best-five scores must be the same length"
        )
    return [str(entry) for entry in initials], [int(entry) for entry in scores]


def _best_five_row(rank: int, initials: str, score: int) -> str:
    # One fixed-width best-five row: rank(1) + gap(2) + initials(3) + gap(2) + score. With the
    # default data every row is 13 monospace cells, so the five centered lines column-align.
    return f"{rank}  {initials}  {score}"


def render_sheet_text_costume(
    sheet: se.Image,
    threshold: int,
    name: str,
    lines: tuple[str, ...],
    *,
    cell_w: int = SHEET_TEXT_CELL_W,
    cell_h: int = SHEET_TEXT_CELL_H,
    glyph_gap: int = SHEET_TEXT_GLYPH_GAP,
    line_gap: int = SHEET_TEXT_LINE_GAP,
    downscale: int = SHEET_TEXT_DOWNSCALE,
) -> CreditOutput:
    """Compose one costume of centered text lines from the high-res Xevious HUD font sheet.

    The single compositor for every sheet-font overlay — the CAB-01 attract text
    (render_attract_costumes, at the default attract geometry) and the SEC-03 hidden credit
    (render_credit, at a smaller cell so its longest line stays on-stage). Each glyph is
    cropped from the credited sheet by its SHEET_TEXT_RECTS entry, placed in a fixed monospace
    cell — centered horizontally, bottom-aligned to a shared baseline — and the assembled lines
    are decimated by `downscale`. Monospace so the best-five columns align; a space is one empty
    advance. A character with no rect fails loudly so a reworded overlay never silently drops a
    glyph (the sheet only lacks lowercase, which these strings never use)."""
    for line in lines:
        for char in line:
            if char != " " and char not in SHEET_TEXT_RECTS:
                raise HudGlyphsError(
                    f"overlay text {name!r} needs glyph {char!r}, which has no SHEET_TEXT_RECTS entry"
                )
    advance = cell_w + glyph_gap
    line_pitch = cell_h + line_gap

    def line_width(line: str) -> int:
        return max(0, len(line) * advance - glyph_gap)

    base_width = max(line_width(line) for line in lines)
    base_height = len(lines) * line_pitch - line_gap
    pixels = [CREDIT_TRANSPARENT] * (base_width * base_height)
    for row, line in enumerate(lines):
        x_start = (base_width - line_width(line)) // 2  # center each line horizontally
        y_start = row * line_pitch
        for col, char in enumerate(line):
            if char == " ":
                continue
            crop = _binarize_glyph(sheet, tuple(SHEET_TEXT_RECTS[char]), threshold, CREDIT_INK)
            cx = x_start + col * advance + (cell_w - crop.width) // 2
            cy = y_start + (cell_h - crop.height)  # bottom-align to the baseline
            for gy in range(crop.height):
                for gx in range(crop.width):
                    pixel = crop.pixel(gx, gy)
                    if pixel[3]:
                        pixels[(cy + gy) * base_width + (cx + gx)] = pixel
    base = se.Image(base_width, base_height, tuple(pixels))
    scaled = _downscale_nearest(base, downscale)
    png = se.encode_png(scaled)
    return CreditOutput(name, f"{se._md5(png)}.png", png, scaled.width, scaled.height)


def render_attract_costumes(sheet: se.Image, threshold: int) -> list[CreditOutput]:
    """The CAB-01 attract-screen overlays on the start_screen target, in the Xevious HUD font.

    Digit costumes drive the live credit counter (title_blocks switches a digit clone to
    `digit/<n>` each tick); the CREDIT / PUSH START / INSERT COIN labels and the default
    best-five table are static. Rendered from the same credited HUD font sheet the score/label
    readouts use (render_sheet_text_costume); the best-five initials are the operator's
    placeholders (ATTRACT_DEFAULT_INITIALS), not the ROM's default name strings."""
    outputs = [
        render_sheet_text_costume(sheet, threshold, f"digit/{d}", (str(d),)) for d in range(10)
    ]
    for name, text in ATTRACT_LABELS:
        outputs.append(render_sheet_text_costume(sheet, threshold, name, (text,)))
    initials, scores = _load_best_five()
    rows = tuple(
        _best_five_row(rank, ini, score)
        for rank, (ini, score) in enumerate(zip(initials, scores), start=1)
    )
    outputs.append(render_sheet_text_costume(sheet, threshold, ATTRACT_BEST_FIVE_NAME, rows))
    return outputs


def _credit_costume(output: CreditOutput) -> dict:
    return {
        "name": output.name,
        "bitmapResolution": 1,
        "dataFormat": "png",
        "assetId": output.filename.removesuffix(".png"),
        "md5ext": output.filename,
        "rotationCenterX": output.width // 2,
        "rotationCenterY": output.height // 2,
    }


def _overlay_credit_record(manifest: dict, output: CreditOutput) -> dict:
    sheet = manifest["font_sheet"]
    return {
        "origin": (
            f"Hidden-credit overlay '{output.name}' (SEC-03) composited by tools/hud_glyphs.py "
            f"(render_credit) from {sheet['source']}"
        ),
        "license": sheet["license"],
        "notes": (
            f"Credit: {sheet['credit']}. The repository operator did not create the font. "
            f"Source {sheet['asset']} at SHA-256 {sheet['sha256']}; glyphs cropped by "
            f"SHEET_TEXT_RECTS, laid out on a {SHEET_TEXT_CELL_W}px monospace cell and "
            f"{SHEET_CREDIT_DOWNSCALE}x nearest-neighbor decimated, white ink on transparent, "
            "bitmapResolution 1. The two-line WORDING is the repository operator's own content: "
            f"{' / '.join(CREDIT_TEXT_LINES)} — NOT arcade art and NOT the arcade "
            "str_program_by_EVEZOO credit; the port's own placeholder text set in the credited font."
        ),
    }


def _overlay_attract_record(manifest: dict, output: CreditOutput) -> dict:
    sheet = manifest["font_sheet"]
    return {
        "origin": (
            f"Attract-screen text overlay '{output.name}' composited by tools/hud_glyphs.py "
            f"(render_attract_costumes) from {sheet['source']}"
        ),
        "license": sheet["license"],
        "notes": (
            f"Credit: {sheet['credit']}. The repository operator did not create the font. "
            f"Source {sheet['asset']} at SHA-256 {sheet['sha256']}; glyphs cropped by "
            f"SHEET_TEXT_RECTS, laid out on a {SHEET_TEXT_CELL_W}px monospace cell and "
            f"{SHEET_TEXT_DOWNSCALE}x nearest-neighbor decimated, white ink on transparent, "
            "bitmapResolution 1. Costumes on the start_screen target: the credit-counter digits, "
            "the CREDIT / PUSH START / INSERT COIN prompts, and the default best-five table "
            "(initials from the ATTRACT_DEFAULT_INITIALS source constant, the operator's "
            "placeholders, NOT the ROM default name strings; paired with the arcade default "
            "scores from docs/spec/data/scores.json)."
        ),
    }


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


_GAME_SOUND_KEYS = {
    "name", "file", "arcade_sound", "arcade_id", "play_point",
    "source", "credit", "license", "sha256",
}


def load_game_sounds_manifest() -> dict:
    try:
        manifest = json.loads(GAME_SOUNDS_MANIFEST_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HudGlyphsError(
            f"cannot read game-sounds manifest {GAME_SOUNDS_MANIFEST_PATH}: {exc}"
        ) from exc
    if manifest.get("version") != 1 or not isinstance(manifest.get("sounds"), list):
        raise HudGlyphsError("game-sounds manifest must be version 1 with a sounds list")
    seen: set[str] = set()
    for entry in manifest["sounds"]:
        if not isinstance(entry, dict) or set(entry) != _GAME_SOUND_KEYS:
            raise HudGlyphsError(
                f"game-sounds entry must have exactly keys {sorted(_GAME_SOUND_KEYS)}"
            )
        if entry["name"] in seen:
            raise HudGlyphsError(f"duplicate game sound name: {entry['name']}")
        seen.add(entry["name"])
    return manifest


def render_game_sounds() -> list[GameSoundOutput]:
    """Return one GameSoundOutput per assets/game-sounds/ entry, sorted by name.

    Each wav is verified against its recorded SHA-256 and attached UNMODIFIED — the
    committed bytes are the Stage sound's bytes, addressed by their own md5.
    """
    manifest = load_game_sounds_manifest()
    outputs: list[GameSoundOutput] = []
    for record in manifest["sounds"]:
        source = GAME_SOUNDS_DIR / record["file"]
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise HudGlyphsError(f"cannot read game sound {source}: {exc}") from exc
        actual_hash = se._sha256(data)
        if actual_hash != record["sha256"]:
            raise HudGlyphsError(
                f"game sound {record['name']} hash changed: expected {record['sha256']}, "
                f"found {actual_hash}"
            )
        if not (data.startswith(b"RIFF") and data[8:12] == b"WAVE"):
            raise HudGlyphsError(f"game sound {record['name']} is not a RIFF/WAVE file")
        try:
            with wave.open(io.BytesIO(data)) as handle:
                frame_count = handle.getnframes()
                rate = handle.getframerate()
                channels = handle.getnchannels()
        except wave.Error as exc:
            raise HudGlyphsError(
                f"cannot parse game sound {record['name']} WAV header: {exc}"
            ) from exc
        if channels not in (1, 2):
            raise HudGlyphsError(
                f"game sound {record['name']} has an unexpected channel count: {channels}"
            )
        asset_id = se._md5(data)
        filename = f"{asset_id}.wav"
        sound = {
            "name": record["name"],
            "assetId": asset_id,
            "dataFormat": "wav",
            "format": "",
            "rate": rate,
            "sampleCount": frame_count,
            "md5ext": filename,
        }
        outputs.append(GameSoundOutput(record["name"], filename, sound, data, record))
    outputs.sort(key=lambda output: output.name)
    return outputs


def _overlay_game_sound_record(output: GameSoundOutput) -> dict:
    record = output.record
    return {
        "origin": (
            f"Unmodified copy of {record['source']}; committed at "
            f"assets/game-sounds/{record['file']} and attached as the Stage "
            f"'{output.name}' sound (arcade {record['arcade_sound']} {record['arcade_id']}, "
            f"played at {record['play_point']})"
        ),
        "license": record["license"],
        "notes": (
            f"Credit: {record['credit']}. The repository operator did not create this "
            f"asset. Source SHA-256 {record['sha256']}; no audio transformation applied."
        ),
    }


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
    credit_output: CreditOutput,
    game_sounds: list[GameSoundOutput] | None = None,
    attract_outputs: list[CreditOutput] | None = None,
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
    # SEC-03: attach the single generated credit costume to game_director's easter-egg target,
    # whose blocks switch to it while the hidden credit is revealed (game_director owns the target
    # + its empty-costume placeholder; this module fills the one costume).
    egg = next(
        (target for target in result["targets"] if target.get("name") == CREDIT_TARGET), None
    )
    if egg is None:
        raise HudGlyphsError(
            f"Scratch project has no {CREDIT_TARGET} target; run tools/game_director.py generate first"
        )
    egg["costumes"] = [_credit_costume(credit_output)]
    # CAB-01: attach the attract-screen overlays (credit digits/labels, best-five table) to
    # game_director's start_screen target, whose clone roles switch among them during title and
    # attract-scores. game_director owns the target and its base logo costume (kept as costume 0,
    # the default); this module appends the generated overlays. Filtering by the attract names
    # first keeps this idempotent — a re-run drops the prior overlays before re-appending, so the
    # logo stays index 0 and the order never drifts.
    attract_outputs = attract_outputs or []
    if attract_outputs:
        start_screen = next(
            (t for t in result["targets"] if t.get("name") == ATTRACT_TARGET), None
        )
        if start_screen is None:
            raise HudGlyphsError(
                f"Scratch project has no {ATTRACT_TARGET} target; run tools/game_director.py generate first"
            )
        attract_names = {output.name for output in attract_outputs}
        start_screen["costumes"] = [
            costume
            for costume in start_screen["costumes"]
            if costume.get("name") not in attract_names
        ] + [_credit_costume(output) for output in attract_outputs]
    stage = next(target for target in result["targets"] if target.get("isStage"))
    # Rebuild the Stage's added sounds deterministically: keep the base music/start sounds, then
    # `extend`, then the gameplay SFX in name order. Filtering by name first keeps this idempotent
    # (a re-run drops the prior copies before re-appending), so the order never drifts.
    game_sounds = game_sounds or []
    added_names = {SOUND_NAME} | {output.name for output in game_sounds}
    stage["sounds"] = (
        [entry for entry in stage["sounds"] if entry.get("name") not in added_names]
        + [sound]
        + [output.sound for output in game_sounds]
    )
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
    credit_output: CreditOutput,
    game_sounds: list[GameSoundOutput] | None = None,
    attract_outputs: list[CreditOutput] | None = None,
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
    outputs[credit_output.filename] = {
        "kind": "credit",
        "name": credit_output.name,
        "text": list(CREDIT_TEXT_LINES),
        "generator_version": GENERATOR_VERSION,
    }
    for output in game_sounds or []:
        outputs[output.filename] = {
            "kind": "game_sound",
            "name": output.name,
            "generator_version": GENERATOR_VERSION,
        }
    for output in attract_outputs or []:
        outputs[output.filename] = {
            "kind": "attract",
            "name": output.name,
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
    list[GameSoundOutput],
    CreditOutput,
    list[CreditOutput],
]:
    manifest, manifest_bytes = load_manifest()
    glyph_outputs = render_glyphs(manifest)
    life_output = render_life_icon(manifest)
    sound, sound_bytes, sound_filename = render_extend_sound(manifest)
    game_sounds = render_game_sounds()
    sheet = _load_font_sheet(manifest)
    threshold = manifest["glyph_threshold"]
    credit_output = render_credit(sheet, threshold)
    attract_outputs = render_attract_costumes(sheet, threshold)
    prior_outputs = set(_prior_output_records())
    current_project = _read_json(PROJECT_PATH)
    project_bytes = se._ordered_json_bytes(
        expected_project(
            current_project, glyph_outputs, life_output, manifest, sound,
            credit_output, game_sounds, attract_outputs,
        )
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
    # The life icon can dedup to an already-extracted sprite frame — the ship IS
    # solvalou/flight/01, so `render_life_icon([5,27,16,16])` produces byte-identical pixels
    # and the same content-hash filename. When that asset is already referenced by a non-HUD
    # costume it is already provenanced by the sprite extractor for the identical source crop:
    # keep that existing record rather than clobbering it, so the two generators do not fight
    # over the shared asset's single overlay entry. (The life icon's own crop stays fully
    # recorded in the derivative provenance.) Keying off the project's costume references — not
    # the prior-output set — keeps this idempotent even after the life filename becomes one of
    # the HUD's own recorded outputs.
    life_shared = any(
        costume.get("md5ext") == life_output.filename
        for target in current_project["targets"]
        if target.get("name") != "hud"
        for costume in target.get("costumes", [])
    )
    if life_shared and life_output.filename in overlay["assets"]:
        assets[life_output.filename] = overlay["assets"][life_output.filename]
    else:
        assets[life_output.filename] = _overlay_life_record(manifest, life_output)
    assets[sound_filename] = _overlay_sound_record(manifest, sound_filename)
    for output in game_sounds:
        assets[output.filename] = _overlay_game_sound_record(output)
    assets[credit_output.filename] = _overlay_credit_record(manifest, credit_output)
    for output in attract_outputs:
        assets[output.filename] = _overlay_attract_record(manifest, output)
    assets = dict(sorted(assets.items()))
    overlay_bytes = se._ordered_json_bytes({"version": 1, "assets": assets})
    derivative_provenance_bytes = se._ordered_json_bytes(
        _derivative_provenance(
            manifest, manifest_bytes, glyph_outputs, life_output, sound_filename,
            credit_output, game_sounds, attract_outputs,
        )
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
        game_sounds,
        credit_output,
        attract_outputs,
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
        game_sounds,
        credit_output,
        attract_outputs,
    ) = _expected_state()
    expected_names = {output.filename for output in glyph_outputs} | {
        life_output.filename,
        sound_info["filename"],
        credit_output.filename,
    } | {output.filename for output in game_sounds} | {
        output.filename for output in attract_outputs
    }
    for stale in sorted(prior_outputs - expected_names):
        stale_path = ASSET_DIR / stale
        if stale_path.is_file() and not stale_path.is_symlink():
            stale_path.unlink()
    for output in glyph_outputs:
        (ASSET_DIR / output.filename).write_bytes(output.png)
    (ASSET_DIR / life_output.filename).write_bytes(life_output.png)
    (ASSET_DIR / sound_info["filename"]).write_bytes(sound_bytes)
    (ASSET_DIR / credit_output.filename).write_bytes(credit_output.png)
    for output in game_sounds:
        (ASSET_DIR / output.filename).write_bytes(output.wav)
    for output in attract_outputs:
        (ASSET_DIR / output.filename).write_bytes(output.png)
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
        game_sounds,
        credit_output,
        attract_outputs,
    ) = _expected_state()
    expected_names = {output.filename for output in glyph_outputs} | {
        life_output.filename,
        sound_info["filename"],
        credit_output.filename,
    } | {output.filename for output in game_sounds} | {
        output.filename for output in attract_outputs
    }
    stale = prior_outputs - expected_names
    if stale:
        raise HudGlyphsError("stale generated HUD assets: " + ", ".join(sorted(stale)))
    for output in glyph_outputs:
        _require_bytes(ASSET_DIR / output.filename, output.png)
    _require_bytes(ASSET_DIR / life_output.filename, life_output.png)
    _require_bytes(ASSET_DIR / sound_info["filename"], sound_bytes)
    _require_bytes(ASSET_DIR / credit_output.filename, credit_output.png)
    for output in game_sounds:
        _require_bytes(ASSET_DIR / output.filename, output.wav)
    for output in attract_outputs:
        _require_bytes(ASSET_DIR / output.filename, output.png)
    _require_bytes(PROJECT_PATH, project_bytes)
    _require_bytes(OVERLAY_PROVENANCE_PATH, overlay_bytes)
    _require_bytes(DERIVATIVE_PROVENANCE_PATH, derivative_provenance_bytes)
    return len(glyph_outputs) + 2 + len(attract_outputs)


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
