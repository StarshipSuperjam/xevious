"""Lay out a Scratch 3 target's top-level scripts so none overlap in the editor.

The editor draws each script at the x/y stored on its top block and never moves it on
load, so a generator that guesses positions leaves tall scripts running into the next
one. This module computes every script's drawn height the way the Scratch 3 editor's
block renderer does (scratch-blocks 1.3.0, `core/block_render_svg_vertical.js`) and
stacks the scripts in one column with the gap the editor's own "Clean up Blocks" leaves
(`core/workspace_svg.js` `cleanUp`).

It knows nothing about any particular game: only block shapes. An opcode missing from
SHAPES is refused rather than guessed, so a new kind of block can't quietly overlap.
"""

from __future__ import annotations

from typing import Any

# scratch-blocks 1.3.0 `core/block_render_svg_vertical.js` constants, in workspace units.
GRID_UNIT = 4
MIN_BLOCK_Y = 12 * GRID_UNIT  # a statement block's row; also Clean up's gap between scripts
MIN_BLOCK_Y_REPORTER = 10 * GRID_UNIT  # a reporter's row
MIN_BLOCK_Y_SINGLE_FIELD_OUTPUT = 8 * GRID_UNIT  # a lone-field shadow (number, text, menu)
MIN_STATEMENT_INPUT_HEIGHT = 6 * GRID_UNIT  # an empty C-block mouth
EXTRA_STATEMENT_ROW_Y = 8 * GRID_UNIT  # the arm under a mouth, or the "else" row between two
NOTCH_HEIGHT = 2 * GRID_UNIT  # the tab under a block with a next connection
INLINE_PADDING_Y = 1 * GRID_UNIT  # above and below a block plugged into a value input
DEFINE_ROW_EXTRA = 4 * GRID_UNIT  # the define hat's padding round its prototype (`renderDefineBlock_`)
START_HAT_HEIGHT = 16  # how far a hat's curve rises above its block's top edge

# How each opcode is drawn. Kinds:
#   hat       no previous connection, has a next one; the curve rises above the top edge
#   define    procedures_definition: a hat whose prototype sits in a statement row, with
#             no arm under it
#   stack     previous and next connections
#   cap       previous connection only (nothing can attach below)
#   stop      control_stop: a cap unless its mutation says it has a next connection
#   c         a C-block with the listed mouths, in order, and a next connection
#   c_cap     a C-block with no next connection (forever)
#   reporter  a round or hexagonal reporter
#   menu      a menu: a lone field when it is a shadow, a reporter otherwise (the
#             argument reporter is a shadow too, but scratch-blocks draws it full height)
# Value inputs need no listing: an empty value slot is never taller than its block's own
# minimum row, so only the blocks actually plugged in can grow a row.
SHAPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "event_whenflagclicked": ("hat", ()),
    "event_whenbroadcastreceived": ("hat", ()),
    "event_whenkeypressed": ("hat", ()),
    "control_start_as_clone": ("hat", ()),
    "procedures_definition": ("define", ("custom_block",)),
    "procedures_prototype": ("stack", ()),
    "procedures_call": ("stack", ()),
    "control_create_clone_of": ("stack", ()),
    "control_wait": ("stack", ()),
    "control_delete_this_clone": ("cap", ()),
    "control_stop": ("stop", ()),
    "control_if": ("c", ("SUBSTACK",)),
    "control_if_else": ("c", ("SUBSTACK", "SUBSTACK2")),
    "control_repeat": ("c", ("SUBSTACK",)),
    "control_repeat_until": ("c", ("SUBSTACK",)),
    "control_forever": ("c_cap", ("SUBSTACK",)),
    "data_setvariableto": ("stack", ()),
    "data_changevariableby": ("stack", ()),
    "data_replaceitemoflist": ("stack", ()),
    "event_broadcast": ("stack", ()),
    "event_broadcastandwait": ("stack", ()),
    "looks_cleargraphiceffects": ("stack", ()),
    "looks_goforwardbackwardlayers": ("stack", ()),
    "looks_gotofrontback": ("stack", ()),
    "looks_hide": ("stack", ()),
    "looks_seteffectto": ("stack", ()),
    "looks_setsizeto": ("stack", ()),
    "looks_show": ("stack", ()),
    "looks_switchcostumeto": ("stack", ()),
    "motion_changexby": ("stack", ()),
    "motion_changeyby": ("stack", ()),
    "motion_goto": ("stack", ()),
    "motion_gotoxy": ("stack", ()),
    "motion_setx": ("stack", ()),
    "motion_sety": ("stack", ()),
    "sound_play": ("stack", ()),
    "sound_playuntildone": ("stack", ()),
    "sound_setvolumeto": ("stack", ()),
    "sound_stopallsounds": ("stack", ()),
    "argument_reporter_string_number": ("reporter", ()),
    "data_itemoflist": ("reporter", ()),
    "data_listcontainsitem": ("reporter", ()),
    "motion_xposition": ("reporter", ()),
    "motion_yposition": ("reporter", ()),
    "operator_add": ("reporter", ()),
    "operator_and": ("reporter", ()),
    "operator_divide": ("reporter", ()),
    "operator_equals": ("reporter", ()),
    "operator_gt": ("reporter", ()),
    "operator_join": ("reporter", ()),
    "operator_letter_of": ("reporter", ()),
    "operator_lt": ("reporter", ()),
    "operator_mathop": ("reporter", ()),
    "operator_mod": ("reporter", ()),
    "operator_multiply": ("reporter", ()),
    "operator_not": ("reporter", ()),
    "operator_or": ("reporter", ()),
    "operator_round": ("reporter", ()),
    "operator_subtract": ("reporter", ()),
    "sensing_keypressed": ("reporter", ()),
    "sensing_of": ("reporter", ()),
    "control_create_clone_of_menu": ("menu", ()),
    "looks_costume": ("menu", ()),
    "motion_goto_menu": ("menu", ()),
    "sensing_keyoptions": ("menu", ()),
    "sensing_of_object_menu": ("menu", ()),
    "sound_sounds_menu": ("menu", ()),
}

# Compact input primitives (sb3 serialization): numbers, colour, text and broadcast menus
# are lone-field shadows; variables and lists are reporters.
LONE_FIELD_PRIMITIVES = {4, 5, 6, 7, 8, 9, 10, 11}
REPORTER_PRIMITIVES = {12, 13}

# Every kind `_kind` can return: the shapes the renderer draws, each measured in the editor.
DRAWN_KINDS = frozenset({"hat", "define", "stack", "cap", "c", "c_cap", "reporter", "field"})

# Drawn kinds with a next connection: anything chained under one that lacks
# it is something the editor refuses to load (see `is_cap`).
_HAS_NEXT = {"hat", "define", "stack", "c"}


def _shape(block: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    try:
        return SHAPES[block["opcode"]]
    except KeyError:
        raise ValueError(
            f"script_layout: no shape for opcode {block['opcode']!r}; add it to SHAPES"
        ) from None


def _kind(block: dict[str, Any]) -> str:
    """How this block is drawn, with the two kinds that depend on the block resolved:
    control_stop to "stack" or "cap", and a menu to "field" (a lone-field shadow) or
    "reporter"."""
    kind = _shape(block)[0]
    if kind == "stop":
        # scratch-blocks builds control_stop with a next connection only when its mutation
        # says hasnext (what "other scripts in sprite" sets); with no mutation it is a cap.
        mutation = block.get("mutation") or {}
        kind = "stack" if str(mutation.get("hasnext", "false")).lower() == "true" else "cap"
    elif kind == "menu":
        kind = "field" if block.get("shadow") else "reporter"
    assert kind in DRAWN_KINDS, f"script_layout: {block['opcode']} resolves to unknown kind {kind!r}"
    return kind


def has_next_connection(block: dict[str, Any]) -> bool:
    return _kind(block) in _HAS_NEXT


def is_cap(block: dict[str, Any]) -> bool:
    """True for a block nothing may be chained under.

    The runtime would happily run such a chain, but the editor builds the block without
    a next connection, fails to attach what follows, and abandons the rest of the
    sprite's workspace: the sprite opens with scripts missing.
    """
    return _kind(block) in {"cap", "c_cap"}


def _input_child_height(blocks: dict[str, Any], value: Any) -> int | None:
    """Height of what an input shows: its block, or its shadow when nothing covers it."""
    if not isinstance(value, list) or len(value) < 2:
        return None
    shown = value[1]
    if shown is None and len(value) > 2:
        shown = value[2]
    if isinstance(shown, str):
        return block_height(blocks, shown)
    if isinstance(shown, list):
        if shown[0] in LONE_FIELD_PRIMITIVES:
            return MIN_BLOCK_Y_SINGLE_FIELD_OUTPUT
        if shown[0] in REPORTER_PRIMITIVES:
            return MIN_BLOCK_Y_REPORTER
        raise ValueError(f"script_layout: unknown input primitive {shown!r}")
    return None


def _last_in_stack(blocks: dict[str, Any], block_id: str) -> dict[str, Any]:
    block = blocks[block_id]
    while block["next"]:
        block = blocks[block["next"]]
    return block


def _mouth_row(blocks: dict[str, Any], block: dict[str, Any], name: str) -> int:
    """A statement row: the nested stack, less the tab it tucks into the arm below."""
    value = block["inputs"].get(name)
    child = value[1] if isinstance(value, list) and len(value) > 1 else None
    if not isinstance(child, str):
        return MIN_STATEMENT_INPUT_HEIGHT
    height = stack_height(blocks, child)
    if has_next_connection(_last_in_stack(blocks, child)):
        height -= NOTCH_HEIGHT
    return max(MIN_STATEMENT_INPUT_HEIGHT, height)


def block_height(blocks: dict[str, Any], block_id: str) -> int:
    """The drawn height of one block (what scratch-blocks stores as `block.height`)."""
    block = blocks[block_id]
    kind, mouths = _kind(block), _shape(block)[1]
    if kind == "field":
        return MIN_BLOCK_Y_SINGLE_FIELD_OUTPUT

    # The first row holds the label and every value input ahead of the first mouth.
    first_row = MIN_BLOCK_Y_REPORTER if kind == "reporter" else MIN_BLOCK_Y
    for name, value in block["inputs"].items():
        if name in mouths:
            continue
        child = _input_child_height(blocks, value)
        if child is not None:
            first_row = max(first_row, child + 2 * INLINE_PADDING_Y)

    if kind == "reporter":
        return first_row
    if kind == "define":
        height = _mouth_row(blocks, block, mouths[0]) + DEFINE_ROW_EXTRA
    else:
        height = first_row
        for mouth in mouths:
            height += _mouth_row(blocks, block, mouth)
            # An "else" row between two mouths; the arm under the last one.
            height += EXTRA_STATEMENT_ROW_Y
    if has_next_connection(block):
        height += NOTCH_HEIGHT
    return height


def stack_height(blocks: dict[str, Any], block_id: str) -> int:
    """A block and everything chained under it, each joint overlapping by one tab
    (scratch-blocks `getHeightWidth`)."""
    height = block_height(blocks, block_id)
    next_id = blocks[block_id]["next"]
    while next_id:
        height += block_height(blocks, next_id) - NOTCH_HEIGHT
        next_id = blocks[next_id]["next"]
    return height


def top_level_ids(blocks: dict[str, Any]) -> list[str]:
    """Top-level scripts in the order they were generated."""
    ids = []
    for block_id, block in blocks.items():
        if not isinstance(block, dict):
            # sb3's compact form for a variable or list reporter left loose on the canvas.
            raise ValueError(f"script_layout: cannot place compact top-level block {block_id}")
        if block.get("topLevel"):
            ids.append(block_id)
    return ids


def lay_out(blocks: dict[str, Any]) -> None:
    """Place every top-level script in one column, in generation order, each one the
    editor's Clean up gap below the last. Idempotent."""
    y = 0
    for block_id in top_level_ids(blocks):
        blocks[block_id]["x"] = 0
        blocks[block_id]["y"] = y
        y += stack_height(blocks, block_id) + MIN_BLOCK_Y


def blocks_under_caps(blocks: dict[str, Any]) -> list[str]:
    """Cap blocks that have something chained under them (see `is_cap`)."""
    return [
        block_id
        for block_id, block in blocks.items()
        if isinstance(block, dict) and block.get("next") and is_cap(block)
    ]
