"""Deterministic extractor for reference-derived spec data tables.

Reads the pinned ``jotd666/xevious`` checkout (never vendored into this
repository) and emits the machine-readable data files under ``docs/spec/data/``
that the product spec cites. Every emitted value carries the source file,
label, and line range it was decoded from, so an auditor can re-derive it
against a fresh clone at the pinned commit.

The area-schedule decode is proven, not trusted: each ``area_N_obj_tbl_normal``
byte stream is walked record-by-record using the same dispatch the reference
implements (``sub_fn_2__handle_objects`` -> ``obj_fn_tbl`` ->
``sub_fn_2_handler``), and extraction fails loudly unless every table is
consumed exactly -- starting at its label, ending on its final byte, with no
leftover bytes, no overrun, and no byte drawn from any ``*_super`` table.

Usage:
    python tools/reference_extract.py --checkout PATH [--out docs/spec/data]
    python tools/reference_extract.py --checkout PATH --verify

``--verify`` re-derives everything and byte-compares against the committed
output files instead of writing them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PINNED_COMMIT = "71473685a8c7856c8401c8519276cd97a38d4183"

# SHA-256 of each reference source file at the pinned commit. Extraction
# refuses to run against bytes that differ, so a moved pin or a tampered
# checkout cannot silently change the spec's ground truth.
EXPECTED_SHA256 = {
    "src/xevious_sub.68k": "e2d8a77e1c9b6190949aa00ae86fc1398022c90e285d7fda38920d1d17c77e4a",
    "src/xevious_main.68k": "bd23912e5cc25dfe7ebb69c043ab098fead300949b066ece972d5ddda29de77e",
    "src/map_rom.68k": "f96a17e75caa788589755bb39fb0097a17a51d957663a54885f97b7835a7d7d6",
    "src/xevious_ram.68k": "fc40e0e8b939f665d38812e62f6dbcc994606ff0afaf34e6d9e585ae424a4773",
    "src/xevious.inc": "56b9b0e22d77c53bed7a8b31c2d8c38e5e68f94434319bfa5f524210df01ab66",
}

SUB = "src/xevious_sub.68k"

# Parameter bytes each schedule handler consumes AFTER the two-byte record
# header (scroll row, object type). Derived by reading each consumer routine's
# advancement of the schedule pointer (``area_obj_ptr``) in the reference;
# the citation is the routine label in src/xevious_sub.68k.
#
# ``None`` marks a handler the normal tables must never invoke: the two
# ``null_fn`` slots never advance the schedule pointer (dispatching one would
# hang the reference), so meeting one in a decode proves the walk is wrong.
# Handler 15 (Domogram) is variable-length and handled specially.
HANDLER_PARAM_BYTES = {
    0: 1,      # sub_2_fb_0__type_only: RAM offset
    1: 2,      # sub_2_fn_1__ground_object: RAM offset, sprite Y
    2: 1,      # sub_2_fn_2__set_flying_enemies: signed formation offset
    3: 0,      # sub_2_fn_3__inc_enemy_AI_and_flying_enemies: reads no bytes
    4: None,   # null_fn (UNUSED slot)
    5: 0,      # sub_2_fn_5__reset_flying_enemies
    6: 1,      # sub_2_fn_6__set_bacura_inc_cnt
    7: 0,      # sub_2_fn_7__reset_num_bacura
    8: 1,      # sub_2_fn_8__fire_freq_mask_derota
    9: 1,      # sub_2_fn_9__fire_freq_mask_logram
    10: 1,     # sub_2_fn_10__gnd_stop_firing_row
    11: 1,     # sub_2_fn_11__fire_freq_mask_zoshi
    12: 1,     # sub_2_fn_12__fire_freq_mask_terrazi
    13: 1,     # sub_2_fn_13__fire_freq_mask_kapi
    14: None,  # null_fn (UNUSED slot)
    15: -1,    # sub_2_fn_15__domogram: 3 fixed + 2 * count (variable)
    16: 1,     # sub_2_fn_16__fire_freq_mask_boza_logram
    17: 1,     # sub_2_fn_17__fire_freq_mask_domogram
    18: 0,     # sub_2_fn_18__sheonite_start
    19: 0,     # sub_2_fn_19__sheonite_end
    20: 0,     # sub_2_fn_20__andor_genesis_start
    21: 0,     # sub_2_fn_21__andor_genesis_end
    22: 1,     # sub_2_fn_22__fire_freq_mask_andor_genesis
    23: 0,     # sub_2_fn_23__adjust_AI_level_based_on_score
}

HANDLER_NAMES = {
    0: "add_object",
    1: "add_ground_object",
    2: "set_flying_formation",
    3: "raise_ai_level_and_set_formation",
    5: "reset_flying_formation",
    6: "set_bacura_count",
    7: "reset_bacura_count",
    8: "fire_mask_derota",
    9: "fire_mask_logram",
    10: "ground_stop_firing_row",
    11: "fire_mask_zoshi",
    12: "fire_mask_terrazi",
    13: "fire_mask_kapi",
    15: "add_domogram_with_path",
    16: "fire_mask_boza_logram",
    17: "fire_mask_domogram",
    18: "sheonite_start",
    19: "sheonite_end",
    20: "andor_genesis_start",
    21: "andor_genesis_end",
    22: "fire_mask_andor_genesis",
    23: "adjust_ai_level_from_score",
}

PARAM_FIELDS = {
    0: ["slot"],
    1: ["slot", "sprite_y"],
    2: ["formation_offset"],
    6: ["count"],
    8: ["mask"],
    9: ["mask"],
    10: ["row"],
    11: ["mask"],
    12: ["mask"],
    13: ["mask"],
    16: ["mask"],
    17: ["mask"],
    22: ["mask"],
}

LABEL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
BYTE_RE = re.compile(r"^\s+\.byte\s+(.+?)\s*(\|.*)?$")
LONG_REF_RE = re.compile(r"^\s+\.long\s+([A-Za-z_][A-Za-z0-9_]*)\s*(\|.*)?$")

NORMAL_TYPE_MAX = 0x57  # types above this exist only for Super Xevious
MAIN = "src/xevious_main.68k"


class ExtractionError(RuntimeError):
    pass


def parse_value(token: str) -> int:
    token = token.strip()
    if token.lower().startswith("0x"):
        return int(token, 16)
    return int(token, 10)


class SourceFile:
    """A reference source file with label-addressed byte-stream access."""

    def __init__(self, checkout: Path, relpath: str):
        self.relpath = relpath
        path = checkout / relpath
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        expected = EXPECTED_SHA256[relpath]
        if digest != expected:
            raise ExtractionError(
                f"{relpath}: SHA-256 {digest} does not match the pinned "
                f"commit's recorded hash {expected}; refusing to extract"
            )
        self.lines = data.decode("utf-8", errors="replace").splitlines()
        self.labels: dict[str, int] = {}
        for idx, line in enumerate(self.lines):
            match = LABEL_RE.match(line)
            if match:
                # First definition wins; duplicates are recorded elsewhere.
                self.labels.setdefault(match.group(1), idx)

    def bytes_under_label(self, label: str) -> tuple[list[int], list[int], int, int]:
        """All ``.byte`` values from ``label`` to the next label.

        Returns (values, per-byte line numbers (1-based), first line, last line).
        """
        if label not in self.labels:
            raise ExtractionError(f"{self.relpath}: label {label} not found")
        start = self.labels[label]
        values: list[int] = []
        line_numbers: list[int] = []
        last_data_line = start
        for idx in range(start + 1, len(self.lines)):
            line = self.lines[idx]
            if LABEL_RE.match(line):
                break
            match = BYTE_RE.match(line)
            if match:
                for token in match.group(1).split(","):
                    values.append(parse_value(token))
                    line_numbers.append(idx + 1)
                last_data_line = idx
        return values, line_numbers, start + 1, last_data_line + 1


def load_obj_fn_tbl(sub: SourceFile) -> list[int]:
    values, _, _, _ = sub.bytes_under_label("obj_fn_tbl")
    if len(values) != 93:
        raise ExtractionError(
            f"obj_fn_tbl: expected 93 entries (87 normal + 6 Super), got {len(values)}"
        )
    return values


def decode_area_table(
    sub: SourceFile, area: int, obj_fn_tbl: list[int]
) -> dict:
    label = f"area_{area}_obj_tbl_normal"
    values, line_numbers, first_line, last_line = sub.bytes_under_label(label)
    if not values:
        raise ExtractionError(f"{label}: no data bytes found")

    records = []
    terminator = None
    pos = 0
    while pos < len(values):
        if len(values) - pos == 1:
            # Every normal table ends with one 0x0D byte: a sentinel row that
            # can never match, because the area advances when the scroll
            # counter's high byte reaches 0x0E (sub_fn_3__handle_next_area).
            if values[pos] != 0x0D:
                raise ExtractionError(
                    f"{label}: unexpected tail byte 0x{values[pos]:02X} "
                    f"(expected the 0x0D sentinel)"
                )
            terminator = values[pos]
            pos += 1
            break
        row, obj_type = values[pos], values[pos + 1]
        header_line = line_numbers[pos]
        if not 1 <= obj_type <= len(obj_fn_tbl):
            raise ExtractionError(
                f"{label}: object type 0x{obj_type:02X} at offset {pos} is "
                f"outside obj_fn_tbl"
            )
        if obj_type > NORMAL_TYPE_MAX:
            raise ExtractionError(
                f"{label}: object type 0x{obj_type:02X} at offset {pos} is "
                f"Super-only; a normal table must never schedule it"
            )
        handler = obj_fn_tbl[obj_type - 1]  # table is 1-based in the reference
        width = HANDLER_PARAM_BYTES.get(handler)
        if width is None:
            raise ExtractionError(
                f"{label}: object type 0x{obj_type:02X} at offset {pos} "
                f"dispatches to unused handler {handler}; decode is wrong"
            )
        pos += 2
        if handler == 15:
            if pos + 3 > len(values):
                raise ExtractionError(f"{label}: truncated Domogram record")
            slot, sprite_y, count = values[pos], values[pos + 1], values[pos + 2]
            pos += 3
            path = values[pos : pos + 2 * count]
            if len(path) != 2 * count:
                raise ExtractionError(
                    f"{label}: Domogram path truncated (wanted {2 * count} bytes)"
                )
            pos += 2 * count
            params: dict[str, object] = {
                "slot": slot,
                "sprite_y": sprite_y,
                "path_vector_count": count,
                "path": [
                    {"dy": path[i], "dx": path[i + 1]}
                    for i in range(0, len(path), 2)
                ],
            }
        else:
            raw = values[pos : pos + width]
            if len(raw) != width:
                raise ExtractionError(
                    f"{label}: truncated params for type 0x{obj_type:02X}"
                )
            pos += width
            names = PARAM_FIELDS.get(handler, [])
            params = {name: value for name, value in zip(names, raw)}
            if handler == 2 and raw:
                offset = raw[0]
                params["formation_offset"] = offset - 256 if offset > 127 else offset
        records.append(
            {
                "scroll_row": row,
                "object_type": obj_type,
                "handler": HANDLER_NAMES[handler],
                "params": params,
                "source_line": header_line,
            }
        )

    if pos != len(values):
        raise ExtractionError(
            f"{label}: {len(values) - pos} leftover bytes after final record"
        )
    if terminator is None:
        raise ExtractionError(f"{label}: missing 0x0D end sentinel")

    return {
        "area": area,
        "source": {
            "file": SUB,
            "label": label,
            "lines": [first_line, last_line],
            "byte_length": len(values),
        },
        "end_sentinel": terminator,
        "records": records,
    }


def decode_formation_table(sub: SourceFile) -> dict:
    """The flying-formation table, including its negative-offset half.

    ``sub_2_fn_2__set_flying_enemies`` sign-extends the schedule byte and
    doubles it before indexing ``flying_enemy_type_offset_tbl_normal``, so the
    64 bytes physically preceding the label are addressable entries -32..-1.
    """
    label = "flying_enemy_type_offset_tbl_normal"
    if label not in sub.labels:
        raise ExtractionError(f"{SUB}: label {label} not found")
    label_line = sub.labels[label]

    # Collect contiguous .byte lines immediately above the label (the
    # negative-offset block sits between the pointer long and the label).
    prefix: list[tuple[int, list[int]]] = []
    for idx in range(label_line - 1, -1, -1):
        line = sub.lines[idx]
        match = BYTE_RE.match(line)
        if match:
            prefix.append((idx + 1, [parse_value(t) for t in match.group(1).split(",")]))
            continue
        if line.strip() == "" or line.strip().startswith("*"):
            continue
        break
    prefix.reverse()
    negative_values: list[int] = []
    for _, vals in prefix:
        negative_values.extend(vals)

    positive_values, _, first_line, last_line = sub.bytes_under_label(label)
    if len(negative_values) % 2 or len(positive_values) % 2:
        raise ExtractionError(f"{label}: odd byte count in formation table")
    if len(negative_values) != 64:
        raise ExtractionError(
            f"{label}: expected 64 negative-offset bytes, got {len(negative_values)}"
        )
    # The schedule byte indexing this table is sign-extended, so index +127 is
    # the highest reachable entry (256 bytes). The bytes that follow, up to the
    # *_super label, are the Super table's own negative-offset block —
    # unreachable from the normal game and excluded from this project. Slice
    # them off, and insist the leftover is exactly that 64-byte block so a
    # layout change in the reference fails loudly instead of shifting data.
    if len(positive_values) not in (256, 256 + 64):
        raise ExtractionError(
            f"{label}: expected 256 reachable bytes (+ optional 64-byte Super "
            f"negative block), got {len(positive_values)}"
        )
    positive_values = positive_values[:256]

    def pairs(values: list[int], base_index: int) -> list[dict]:
        out = []
        for i in range(0, len(values), 2):
            out.append(
                {
                    "index": base_index + i // 2,
                    "enemy_count": values[i],
                    "type_table_offset": values[i + 1],
                }
            )
        return out

    entries = pairs(negative_values, -(len(negative_values) // 2)) + pairs(
        positive_values, 0
    )
    return {
        "source": {
            "file": SUB,
            "label": label,
            "lines": [prefix[0][0] if prefix else first_line, last_line],
            "note": (
                "indices below zero are the bytes physically preceding the "
                "label; the reference indexes this table with a sign-extended "
                "doubled offset"
            ),
        },
        "entries": entries,
    }


WORD_RE = re.compile(r"^\s+\.word\s+(.+?)\s*(\|.*)?$")


def words_under_label(source: SourceFile, label: str) -> tuple[list[int], int, int]:
    """All ``.word`` values from ``label`` to the next label."""
    if label not in source.labels:
        raise ExtractionError(f"{source.relpath}: label {label} not found")
    start = source.labels[label]
    values: list[int] = []
    last = start
    for idx in range(start + 1, len(source.lines)):
        line = source.lines[idx]
        if LABEL_RE.match(line):
            break
        match = WORD_RE.match(line)
        if match:
            for token in match.group(1).split(","):
                values.append(parse_value(token))
            last = idx
    return values, start + 1, last + 1


def bcd_decode(value: int) -> int:
    """Read a hex value's nibbles as decimal digits (BCD)."""
    result = 0
    for digit in f"{value:x}":
        if not digit.isdigit():
            raise ExtractionError(f"non-BCD nibble in 0x{value:X}")
        result = result * 10 + int(digit)
    return result


def score_triple(b0: int, b1: int, b2: int) -> int:
    """Decode the reference's 3-byte little-endian BCD score (implicit x10)."""
    return (bcd_decode(b0) + bcd_decode(b1) * 100 + bcd_decode(b2) * 10000) * 10


def decode_score_tables(main: SourceFile) -> dict:
    """The scoring economy's labeled tables, machine-readable."""
    value_bytes, _, v_first, _ = main.bytes_under_label("object_value_tbl")
    pts_bytes, _, _, p_last = main.bytes_under_label("pts_10000")
    all_bytes = value_bytes + pts_bytes
    if len(all_bytes) % 3:
        raise ExtractionError("object_value_tbl: byte count not a multiple of 3")
    master = [
        {"offset": i, "points": score_triple(*all_bytes[i : i + 3])}
        for i in range(0, len(all_bytes), 3)
    ]
    if len(master) != 22 or master[0]["points"] != 10 or master[-1]["points"] != 10000:
        raise ExtractionError("object_value_tbl: decoded shape does not match the reference")

    lives, _, l_first, l_last = main.bytes_under_label("starting_solvalou_tbl")
    if len(lives) != 4:
        raise ExtractionError("starting_solvalou_tbl: expected 4 entries")

    def bonus_words(label: str) -> list[int | None]:
        words, _, _ = words_under_label(main, label)
        if len(words) != 8:
            raise ExtractionError(f"{label}: expected 8 entries")
        return [None if w == 0xFFFF else bcd_decode(w) * 1000 for w in words]

    hs_bytes, _, h_first, h_last = main.bytes_under_label("ROM_high_score_tbl_normal")
    if len(hs_bytes) != 5 * 16:
        raise ExtractionError("ROM_high_score_tbl_normal: expected 5 16-byte entries")
    high_scores = [score_triple(*hs_bytes[i : i + 3]) for i in range(0, 80, 16)]

    src = lambda label, lines: {"file": MAIN, "label": label, "lines": lines}
    return {
        "master_value_table": {
            "source": src("object_value_tbl", [v_first, p_last]),
            "note": (
                "an object's points index is a byte offset into this table; "
                "3-byte little-endian BCD with an implicit x10"
            ),
            "entries": master,
        },
        "starting_lives": {
            "source": src("starting_solvalou_tbl", [l_first, l_last]),
            "note": "indexed by DIP switch A bits 5-6 (raw index; physical switch mapping unrecorded)",
            "values": lives,
        },
        "first_bonus_thresholds": {
            "source": src("first_bonus_life_tbls", [l_first, l_last]),
            "note": (
                "points; null = bonus lives disabled at that setting. Table "
                "selection between the two carries the recorded uncertainty in "
                "the scoring document (the reference's two selection sites "
                "disagree)"
            ),
            "table_5": bonus_words("first_bonus_life_Ks_5"),
            "table_123": bonus_words("first_bonus_life_Ks_123"),
        },
        "repeat_bonus_increments": {
            "source": src("bonus_tbl_ptrs", [l_first, l_last]),
            "note": "points added to the threshold after each award; null = disabled",
            "table_5": bonus_words("bonus_tbl_5"),
            "table_123": bonus_words("bonus_tbl_123"),
        },
        "high_score_defaults": {
            "source": src("ROM_high_score_tbl_normal", [h_first, h_last]),
            "note": (
                "scores of the five default best-five entries; the name fields "
                "are the ROM's own credit strings and are not transcribed"
            ),
            "scores": high_scores,
        },
    }


def longs_under_label(source: SourceFile, label: str) -> tuple[list[tuple[str, int]], int, int]:
    """All ``.long <symbol>`` entries from ``label`` to the next label."""
    if label not in source.labels:
        raise ExtractionError(f"{source.relpath}: label {label} not found")
    start = source.labels[label]
    entries: list[tuple[str, int]] = []
    last = start
    for idx in range(start + 1, len(source.lines)):
        line = source.lines[idx]
        if LABEL_RE.match(line):
            break
        match = LONG_REF_RE.match(line)
        if match:
            entries.append((match.group(1), idx + 1))
            last = idx
    return entries, start + 1, last + 1


def decode_object_registry(main: SourceFile, sub: SourceFile, obj_fn_tbl: list[int]) -> dict:
    """The object-type registry: main sprite handler + schedule action per code.

    Mechanically derived: obj_handler_tbl in the main file is a 1-based jump
    table (entry N-1 handles type N; add_obj_handler subtracts 1 before
    indexing), and the handler label itself carries the object's name in the
    form handle_<hex>_<Name>. The sub file's obj_fn_tbl gives each code's
    schedule action.
    """
    entries, first_line, last_line = longs_under_label(main, "obj_handler_tbl")
    if len(entries) != len(obj_fn_tbl):
        raise ExtractionError(
            f"obj_handler_tbl has {len(entries)} entries but obj_fn_tbl has "
            f"{len(obj_fn_tbl)}; the two dispatch tables must agree"
        )
    handler_name_re = re.compile(r"^handle_([0-9A-Fa-f]{2})_(.+)$")
    types = []
    for index, (symbol, line) in enumerate(entries):
        code = index + 1
        entry: dict[str, object] = {
            "code": code,
            "super_only": code > NORMAL_TYPE_MAX,
            "schedule_action": HANDLER_NAMES.get(obj_fn_tbl[index], "none"),
        }
        match = handler_name_re.match(symbol)
        if symbol == "null_fn":
            entry["main_handler"] = None
        else:
            entry["main_handler"] = {"label": symbol, "line": line}
            if match:
                if int(match.group(1), 16) != code:
                    raise ExtractionError(
                        f"obj_handler_tbl entry {index} points at {symbol} but "
                        f"dispatches type 0x{code:02X}; table decode is wrong"
                    )
                entry["name"] = match.group(2).replace("_", " ")
        types.append(entry)
    return {
        "source": {
            "file": MAIN,
            "label": "obj_handler_tbl",
            "lines": [first_line, last_line],
            "note": (
                "1-based dispatch: entry N-1 handles object type N; names are "
                "the reference's own handler labels. schedule_action comes "
                "from obj_fn_tbl in the sub file."
            ),
        },
        "types": types,
    }


def decode_flying_enemy_types(main: SourceFile) -> dict:
    label = "flying_enemy_type_tbl_normal"
    values, _, first_line, last_line = main.bytes_under_label(label)
    if len(values) != 128:
        raise ExtractionError(
            f"{label}: expected 128 bytes, got {len(values)}"
        )
    return {
        "source": {
            "file": MAIN,
            "label": label,
            "lines": [first_line, last_line],
            "note": (
                "formation entries' type_table_offset indexes this table; a "
                "wave of N enemies takes N consecutive codes from its offset. "
                "Offsets 120-127 hold values equal to their own index, outside "
                "the valid type range; recorded as never-reached tail, meaning "
                "uncertain."
            ),
        },
        "codes": values,
    }


def decode_sub_table(sub: SourceFile, label: str, field: str, note: str) -> dict:
    values, _, first_line, last_line = sub.bytes_under_label(label)
    return {
        "source": {"file": SUB, "label": label, "lines": [first_line, last_line]},
        "field": field,
        "note": note,
        "values": values,
    }


def build_payloads(checkout: Path) -> dict[str, dict]:
    sub = SourceFile(checkout, SUB)
    main = SourceFile(checkout, MAIN)
    obj_fn_tbl = load_obj_fn_tbl(sub)
    areas = [decode_area_table(sub, area, obj_fn_tbl) for area in range(1, 17)]

    provenance = {
        "reference": "jotd666/xevious",
        "commit": PINNED_COMMIT,
        "source_sha256": EXPECTED_SHA256,
        "license_status": "no reusable license stated by the source repository",
        "note": (
            "Derived numeric data and orderings only. Reference symbol names "
            "and line numbers appear solely as citation locators; no assembly "
            "instructions, comments, or prose are reproduced. Line numbers "
            "refer to the pinned commit."
        ),
    }
    return {
        "area-schedules.json": {
            "provenance": provenance,
            "dispatch": {
                "file": SUB,
                "labels": ["sub_fn_2__handle_objects", "obj_fn_tbl", "sub_fn_2_handler"],
                "record_format": (
                    "records are [scroll_row, object_type, params...]; the type "
                    "indexes obj_fn_tbl (1-based) to a handler that consumes a "
                    "fixed parameter width, except the Domogram handler whose "
                    "third parameter byte declares how many 2-byte path vectors "
                    "follow"
                ),
            },
            "areas": areas,
        },
        "formations.json": {
            "provenance": provenance,
            "formation_table": decode_formation_table(sub),
        },
        "difficulty.json": {
            "provenance": provenance,
            "difficulty_tbl": decode_sub_table(
                sub,
                "difficulty_tbl",
                "ai_level_increment",
                "AI-level increment per raise record, indexed by the cabinet difficulty DIP setting",
            ),
        },
        "terrain.json": {
            "provenance": provenance,
            "area_offset_in_map_tbl": decode_sub_table(
                sub,
                "area_offset_in_map_tbl",
                "map_column_offset",
                "per-area terrain start column, indexed by area number 1-16",
            ),
        },
        "andor-genesis.json": {
            "provenance": provenance,
            "layout": decode_sub_table(
                sub,
                "andor_genesis_data",
                "object_type",
                "the fifteen object type codes bulk-armed at boss start, in spawn order slot 1..15",
            ),
        },
        "scores.json": {
            "provenance": provenance,
            "tables": decode_score_tables(main),
        },
        "object-types.json": {
            "provenance": provenance,
            "registry": decode_object_registry(main, sub, obj_fn_tbl),
            "flying_enemy_type_table": decode_flying_enemy_types(main),
        },
    }


def render(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=Path("docs/spec/data"))
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    try:
        payloads = build_payloads(args.checkout)
    except ExtractionError as err:
        print(f"extraction failed: {err}", file=sys.stderr)
        return 1

    if args.verify:
        failures = []
        for name, payload in payloads.items():
            path = args.out / name
            if not path.exists():
                failures.append(f"{path}: missing")
            elif path.read_text() != render(payload):
                failures.append(f"{path}: committed content differs from re-derivation")
        for failure in failures:
            print(failure, file=sys.stderr)
        if failures:
            return 1
        print(f"verified {len(payloads)} data files against {args.checkout}")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        (args.out / name).write_text(render(payload))
        area_count = len(payload.get("areas", []))
        suffix = f" ({area_count} areas, all consumed exactly)" if area_count else ""
        print(f"wrote {args.out / name}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
