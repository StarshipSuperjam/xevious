"""Guards for tools/script_layout.py -- placing each sprite's scripts so none overlap in the editor.

The height model is checked against heights measured in the Scratch 3 editor's own block
renderer (tests/fixtures/script_layout_editor_measurements.json: sample scripts covering every
shape the model knows, measured in scratch-blocks 1.3.0). The shipped project is then checked
to be laid out by the model, with no two scripts overlapping and nothing chained under a cap
block (which the editor refuses to load).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import game_director  # noqa: E402
import script_layout as sl  # noqa: E402

FIXTURE = json.loads((ROOT / "tests/fixtures/script_layout_editor_measurements.json").read_text())
PROJECT = json.loads((ROOT / "src/xevious/project.json").read_text())


def _block(opcode: str, **extra) -> dict:
    return {"opcode": opcode, "next": None, "parent": None, "inputs": {}, "fields": {},
            "shadow": False, "topLevel": False, **extra}


def _stack(*opcodes: str, top_id: str = "a") -> dict:
    """A one-script blocks dict: the opcodes chained under each other, the first on top."""
    ids = [f"{top_id}{i}" for i in range(len(opcodes))]
    blocks = {block_id: _block(opcode) for block_id, opcode in zip(ids, opcodes)}
    blocks[ids[0]]["topLevel"] = True
    for upper, lower in zip(ids, ids[1:]):
        blocks[upper]["next"] = lower
        blocks[lower]["parent"] = upper
    return blocks


def _overlapping_scripts(blocks: dict) -> list[tuple[str, str]]:
    """Pairs of top-level scripts whose drawn areas meet, or that sit closer than a hat's
    curve can rise (so a hat would draw over the script above it). Scripts are compared as
    full-width bands: every script here sits in one column."""
    spans = sorted(
        (blocks[block_id]["y"], blocks[block_id]["y"] + sl.stack_height(blocks, block_id), block_id)
        for block_id in sl.top_level_ids(blocks)
    )
    return [
        (upper_id, lower_id)
        for (_, upper_bottom, upper_id), (lower_top, _, lower_id) in zip(spans, spans[1:])
        if lower_top - upper_bottom <= sl.START_HAT_HEIGHT
    ]


class EditorMeasurementTests(unittest.TestCase):
    blocks = FIXTURE["blocks"]

    def test_every_kind_was_measured_in_the_editor(self) -> None:
        # Height follows from a block's kind, not its opcode, so a new opcode of a measured
        # kind needs only its SHAPES line. A new kind needs measuring first (the fixture's
        # `method` says how).
        measured = [self.blocks[block_id] for block_id in FIXTURE["block_heights"]]
        table_kinds = {kind for kind, _ in sl.SHAPES.values()}
        self.assertEqual(table_kinds - {sl.SHAPES[b["opcode"]][0] for b in measured}, set())
        drawn_kinds = {"hat", "define", "stack", "cap", "c", "c_cap", "reporter", "field"}
        self.assertEqual(drawn_kinds - {sl._kind(b) for b in measured}, set())
        mouth_counts = {len(mouths) for kind, mouths in sl.SHAPES.values() if kind == "c"}
        self.assertEqual(mouth_counts - {len(sl.SHAPES[b["opcode"]][1]) for b in measured
                                         if sl._kind(b) == "c"}, set())

    def test_block_heights_match_the_editor(self) -> None:
        for block_id, height in FIXTURE["block_heights"].items():
            with self.subTest(block_id=block_id, opcode=self.blocks[block_id]["opcode"]):
                self.assertEqual(sl.block_height(self.blocks, block_id), height)

    def test_script_heights_match_the_editor(self) -> None:
        self.assertEqual(set(FIXTURE["stack_heights"]), set(sl.top_level_ids(self.blocks)))
        for block_id, height in FIXTURE["stack_heights"].items():
            with self.subTest(block_id=block_id):
                self.assertEqual(sl.stack_height(self.blocks, block_id), height)

    def test_the_samples_cover_the_edge_cases(self) -> None:
        blocks = self.blocks
        measured = FIXTURE["block_heights"]
        drawn = [blocks[block_id] for block_id in measured]
        self.assertTrue(any(b["opcode"] == "control_if" and "SUBSTACK" not in b["inputs"] for b in drawn),
                        "an empty mouth")
        self.assertTrue(any(b["opcode"] == "control_if_else" and "SUBSTACK" not in b["inputs"]
                            and "SUBSTACK2" in b["inputs"] for b in drawn), "an empty first mouth over a filled else")
        self.assertTrue(any(b["opcode"] == "control_stop" and "mutation" not in b for b in drawn),
                        "control_stop with no mutation")
        self.assertTrue(any(b["opcode"] == "control_stop" and b.get("mutation", {}).get("hasnext") == "true"
                            for b in drawn), "control_stop with a next connection")
        script_ends = [sl._last_in_stack(blocks, top) for top in sl.top_level_ids(blocks)]
        self.assertTrue(any(sl.is_cap(b) and b["parent"] for b in script_ends), "a cap ending a script")
        self.assertTrue(any(b["opcode"] == "looks_costume" and not b["shadow"] for b in drawn),
                        "a menu that is not a shadow")
        self.assertTrue(any(b["topLevel"] and sl.SHAPES[b["opcode"]][0] == "reporter" for b in drawn),
                        "a reporter loose on the canvas")
        caps_in_mouths = [
            b for b in drawn if sl.SHAPES[b["opcode"]][0] in {"c", "c_cap"}
            for name in sl.SHAPES[b["opcode"]][1] if name in b["inputs"]
            if sl.is_cap(sl._last_in_stack(blocks, b["inputs"][name][1]))
        ]
        self.assertTrue(caps_in_mouths, "a mouth whose stack ends in a cap")


class ShapeRuleTests(unittest.TestCase):
    def test_an_unknown_opcode_is_refused(self) -> None:
        blocks = _stack("event_whenflagclicked", "pen_clear")
        with self.assertRaisesRegex(ValueError, "pen_clear"):
            sl.stack_height(blocks, "a0")
        with self.assertRaisesRegex(ValueError, "pen_clear"):
            sl.lay_out(blocks)

    def test_an_unknown_input_primitive_is_refused(self) -> None:
        blocks = _stack("motion_setx")
        blocks["a0"]["inputs"]["X"] = [1, [99, "?"]]
        with self.assertRaisesRegex(ValueError, "primitive"):
            sl.block_height(blocks, "a0")

    def test_a_compact_top_level_block_is_refused(self) -> None:
        blocks = _stack("event_whenflagclicked")
        blocks["loose"] = [12, "score", "score-id", 0, 0]
        with self.assertRaisesRegex(ValueError, "loose"):
            sl.lay_out(blocks)

    def test_which_blocks_are_caps(self) -> None:
        stop = _block("control_stop", fields={"STOP_OPTION": ["all", None]})
        stop_other = _block(
            "control_stop", fields={"STOP_OPTION": ["other scripts in sprite", None]},
            mutation={"tagName": "mutation", "children": [], "hasnext": "true"},
        )
        self.assertTrue(sl.is_cap(stop))
        self.assertFalse(sl.is_cap(stop_other))
        self.assertTrue(sl.is_cap(_block("control_delete_this_clone")))
        self.assertTrue(sl.is_cap(_block("control_forever")))
        for opcode in ("control_if", "looks_hide", "event_whenflagclicked", "procedures_definition"):
            self.assertFalse(sl.is_cap(_block(opcode)), opcode)


class LayOutTests(unittest.TestCase):
    def _laid_out_sample(self) -> dict:
        blocks = copy.deepcopy(FIXTURE["blocks"])
        for block in blocks.values():
            block.pop("x", None)
            block.pop("y", None)
        sl.lay_out(blocks)
        return blocks

    def test_scripts_are_stacked_in_order_one_clean_up_gap_apart(self) -> None:
        blocks = self._laid_out_sample()
        tops = sl.top_level_ids(blocks)
        self.assertEqual(blocks[tops[0]]["y"], 0)
        for upper, lower in zip(tops, tops[1:]):
            self.assertEqual(blocks[lower]["x"], 0)
            self.assertIs(type(blocks[lower]["y"]), int)
            self.assertEqual(
                blocks[lower]["y"], blocks[upper]["y"] + sl.stack_height(blocks, upper) + sl.MIN_BLOCK_Y
            )

    def test_with_editor_measured_heights_the_gap_is_exactly_clean_ups(self) -> None:
        blocks = self._laid_out_sample()
        tops = sl.top_level_ids(blocks)
        for upper, lower in zip(tops, tops[1:]):
            drawn_bottom = blocks[upper]["y"] + FIXTURE["stack_heights"][upper]
            self.assertEqual(blocks[lower]["y"] - drawn_bottom, sl.MIN_BLOCK_Y)

    def test_lay_out_is_idempotent(self) -> None:
        blocks = self._laid_out_sample()
        again = copy.deepcopy(blocks)
        sl.lay_out(again)
        self.assertEqual(again, blocks)

    def test_only_top_level_blocks_get_positions(self) -> None:
        blocks = self._laid_out_sample()
        for block_id, block in blocks.items():
            self.assertEqual("x" in block, bool(block["topLevel"]), block_id)

    def test_overlap_is_reported(self) -> None:
        blocks = self._laid_out_sample()
        self.assertEqual(_overlapping_scripts(blocks), [])
        tops = sl.top_level_ids(blocks)
        upper, lower = tops[0], tops[1]
        bottom = blocks[upper]["y"] + sl.stack_height(blocks, upper)
        blocks[lower]["y"] = bottom - 8
        self.assertEqual(_overlapping_scripts(blocks), [(upper, lower)])
        # Clear of the block but within a hat's curve still counts: the hat would draw over it.
        blocks[lower]["y"] = bottom + sl.START_HAT_HEIGHT
        self.assertEqual(_overlapping_scripts(blocks), [(upper, lower)])
        blocks[lower]["y"] = bottom + sl.START_HAT_HEIGHT + 1
        self.assertEqual(_overlapping_scripts(blocks), [])

    def test_a_block_under_a_cap_is_reported(self) -> None:
        blocks = _stack("event_whenbroadcastreceived", "control_delete_this_clone", "looks_hide")
        self.assertEqual(sl.blocks_under_caps(blocks), ["a1"])
        self.assertEqual(sl.blocks_under_caps(_stack("event_whenbroadcastreceived", "looks_hide",
                                                     "control_delete_this_clone")), [])


class ShippedProjectTests(unittest.TestCase):
    def test_every_sprite_is_laid_out_by_the_model(self) -> None:
        for target in PROJECT["targets"]:
            with self.subTest(target=target["name"]):
                relaid = copy.deepcopy(target["blocks"])
                sl.lay_out(relaid)
                self.assertEqual(relaid, target["blocks"])

    def test_no_two_scripts_overlap(self) -> None:
        for target in PROJECT["targets"]:
            with self.subTest(target=target["name"]):
                self.assertEqual(_overlapping_scripts(target["blocks"]), [])

    def test_nothing_is_chained_under_a_cap(self) -> None:
        for target in PROJECT["targets"]:
            with self.subTest(target=target["name"]):
                self.assertEqual(sl.blocks_under_caps(target["blocks"]), [])


class GeneratorGuardTests(unittest.TestCase):
    def test_the_generator_refuses_to_chain_under_a_cap(self) -> None:
        blocks = game_director.Blocks("guard")
        hat = blocks.flag()
        delete = blocks.add("control_delete_this_clone")
        with self.assertRaisesRegex(AssertionError, "control_delete_this_clone"):
            blocks.chain(hat, [delete, blocks.hide()])
        loop = blocks.add("control_if")
        with self.assertRaisesRegex(AssertionError, "control_delete_this_clone"):
            blocks.substack(loop, [blocks.add("control_delete_this_clone"), blocks.hide()])
        # Ending on the cap is fine.
        blocks.chain(blocks.flag(), [blocks.hide(), blocks.add("control_delete_this_clone")])


if __name__ == "__main__":
    unittest.main()
