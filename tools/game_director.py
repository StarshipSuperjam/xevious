#!/usr/bin/env python3
"""Generate and verify the slice-2 Scratch game director."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import scratch_project


ROOT = Path(__file__).resolve().parents[1]
PROJECT_PATH = ROOT / "src" / "xevious" / scratch_project.PROJECT_JSON

STATE_ID = "game-director-state"
EPOCH_ID = "game-director-epoch"
SCOPE_ID = "game-director-reset-scope"
OUTCOME_ID = "game-director-death-outcome"
ALLOWED_ID = "game-director-allowed-transitions"
SOLVALOU_EPOCH_ID = "solvalou-director-entry-epoch"
DEATH_EPOCH_ID = "solv-death-director-entry-epoch"
# Weapon state cleared by the reset scopes (never director `game state`). The bomb
# guard is a Stage variable so the one-bomb poller and the in-flight bomb — which may
# run on different threads — share it; the reload counter is blaster-local.
BOMB_INFLIGHT_ID = "weapon-bomb-in-flight"
RELOAD_ID = "weapon-blaster-reload"
# Per-strip terrain scroll counter (preserved across a new life; only cold-start /
# new-game rewinds it), driving the counted-cycle wrap that replaces the position
# test Scratch fencing made unreachable (audit B3).
TERRAIN_STEP_A_ID = "terrain-scroll-step-a"
TERRAIN_STEP_B_ID = "terrain-scroll-step-b"

# Gameplay timing is counted in build ticks — 1 build tick = 2 arcade frames
# (core-game-systems.md units rule); arcade-frame originals live in their locked
# spec sections and are cited, never restated, in docs/mechanics/003.
RELOAD_TICKS = 10  # arcade 20-frame blaster reload (player-craft WPN-01)
EXPLOSION_STEPS = 7  # 7 costume cycles ...
EXPLOSION_HOLD_TICKS = 4  # ... of 8 arcade frames each = 56 frames = 28 ticks (PLY-02)
POST_DEATH_PAUSE_TICKS = 16  # arcade 32-frame post-explosion pause (PLY-02)
READY_HOLD_TICKS = 30  # project-defined READY beat (no reference basis; core-game-systems)

# SYS-04 shared pseudo-random stream. The update rule and its golden fixtures are the
# normative record in docs/spec/data/rng.json (mirrored by tools/reference_extract.py
# rng_step); they are cited, never restated here. `rng step` is a warp (atomic) Stage
# custom block; `rng state` is the shared 16-bit seed and `rng out` its latest byte.
# The other four are per-step working values (Scratch custom blocks have no locals, so
# they are Stage variables) — machinery, not director state. No consumer draws from the
# stream this slice; every consumer arrives with the enemy slices.
RNG_STATE_ID = "rng-state"
RNG_OUT_ID = "rng-out"
RNG_HIGH_ID = "rng-high"
RNG_NEW_LOW_ID = "rng-new-low"
RNG_NEW_HIGH_ID = "rng-new-high"
RNG_XFLAG_ID = "rng-extend-flag"
RNG_PROCCODE = "rng step"
# Project-defined cold-start seed: the spec records no arcade power-on seed yet, so this
# is a project-defined placeholder (four-marker rule) pending that value. It only fixes
# repeatability (seeded runs repeat); no consumer reads it this slice. Recorded in
# docs/mechanics/004.
RNG_COLD_START_SEED = 0x4A39

# SYS-02 entity slot model. 64 fixed object slots; the arcade object slot 0xNN maps to
# Scratch list index NN+1 (lists are 1-based). The ranges and capacities are recorded in
# player-craft-and-weapons.md (their normative home) and reproduced here as generator
# constants, with both columns, for traceability. Only `slot type` (0 = empty, skipped
# by the walk) and `slot state` (0 idle / 1 active / 2 hit) exist this slice — the
# position/age fields land with the first entity that authors them (centrally, in the
# enemy slice; there is no position writer while the mirror is deferred).
SLOT_TYPE_ID = "slot-type"
SLOT_STATE_ID = "slot-state"
SLOT_INDEX_ID = "slot-index"  # clear-slots loop cursor (machinery, not slot data)
SLOT_COUNT = 64
CLEAR_SLOTS_PROCCODE = "clear slots"
#                       index lo..hi   arcade 0xNN..0xMM   capacity
GROUND_SLOTS = (1, 16)  # ....... 1-16   0x00-0x0F ........ 16
BACURA_SLOTS = (17, 32)  # ...... 17-32   0x10-0x1F ........ 16
BOMB_TARGET_SLOT = 33  # ........ 33      0x20
BOMB_SLOT = 34  # ............... 34      0x21
CROSSHAIR_SLOT = 35  # .......... 35      0x22
SOLVALOU_SLOT = 36  # ........... 36      0x23
SHOT_SLOTS = (37, 39)  # ........ 37-39   0x24-0x26 ........ 3
BULLET_SLOTS = (40, 58)  # ...... 40-58   0x27-0x39 ........ 19
FLYING_SLOTS = (59, 64)  # ...... 59-64   0x3A-0x3F ........ 6

# SYS-04 centralized ordered update (architecture.md key decision): the Stage walks the
# slots in index order each tick as one ATOMIC (warp) pass — the shape that preserves
# the reference's random-stream draw order (free-running per-clone threads are ruled out
# for stream consumers). `tick` is the authoritative gameplay frame counter the future
# area director builds on. Dormant this slice: no slot dispatches work and no consumer
# draws from the stream, so the pass only advances the clock.
TICK_ID = "tick"
ADVANCE_SLOTS_PROCCODE = "advance slots"

# SYS-02 player-shot capacity: at most 3 live shots, one per dedicated slot (37-39).
# Allocation over those fixed slots is what binds the cap (audit A3, deferred here from
# #13). SLOT_ACTIVE marks an allocated slot; SHOT_TYPE is the player-shot occupancy
# marker (per-entity type codes arrive with the dispatch the enemy slice builds).
SLOT_ACTIVE = 1
SHOT_TYPE = 1
ALLOC_RESULT_ID = "blaster-alloc-result"
CLONE_SLOT_ID = "blaster-clone-slot"
ALLOC_SHOT_PROCCODE = "alloc shot slot"

# SYS-03 collision groups and single-hit resolution. Exactly five groups (below), no
# others. A hit resolves exactly once through one path: `resolve hit` marks the struck
# slot HIT and routes to the single `score` hook, so nothing can double-score.
# FOUNDATION-ONLY / provisional skeleton: no enemy-side participant exists yet, so no
# collision is detected — the per-group overlap detection and the exception verdicts are
# delegated to the enemy/ground/boss/secrets slices (as the spec's SYS-03 exception
# table delegates them). This slice lays the single-hit path and the group vocabulary.
SLOT_HIT = 2
HIT_SLOT_ID = "hit-slot"
RESOLVE_HIT_PROCCODE = "resolve hit"
SCORE_PROCCODE = "score"
_PLAYER = (SOLVALOU_SLOT, SOLVALOU_SLOT)
_BOMB = (BOMB_SLOT, BOMB_SLOT)
# (attacker range, victim range) for each of the five groups (core-game-systems SYS-03).
COLLISION_GROUPS = (
    (SHOT_SLOTS, FLYING_SLOTS),   # player shots vs air enemies
    (_BOMB, GROUND_SLOTS),        # bombs vs ground objects
    (BULLET_SLOTS, _PLAYER),      # enemy shots vs the player
    (FLYING_SLOTS, _PLAYER),      # air enemies vs the player
    (BACURA_SLOTS, _PLAYER),      # Bacura vs the player
)

MESSAGES = {
    "director enter": "broadcastMsgId-director-enter",
    "director stop": "broadcastMsgId-director-stop",
    "director reset": "broadcastMsgId-director-reset",
    "ready complete": "broadcastMsgId-ready-complete",
    "death complete": "broadcastMsgId-death-complete",
    "game over complete": "broadcastMsgId-game-over-complete",
    "bomb": "broadcastMsgId-bomb-release",
    "target_b": "broadcastMsgId-target-bounds-bottom",
    "target_l": "broadcastMsgId-target-bounds-left",
    "target_r": "broadcastMsgId-target-bounds-right",
    "target_t": "broadcastMsgId-target-bounds-top",
}

PROCCODE = "transition to %s reset %s"
ARG_IDS = ["director-destination", "director-scope"]


def number(value: int | float) -> list[Any]:
    return [1, [4, value]]


def text(value: str) -> list[Any]:
    return [1, [10, value]]


def variable(name: str, variable_id: str) -> list[Any]:
    return [3, [12, name, variable_id], [10, ""]]


def broadcast(name: str, message_id: str) -> list[Any]:
    return [1, [11, name, message_id]]


class Blocks:
    def __init__(self, target: str) -> None:
        self.target = target.replace("_", "-")
        self.blocks: dict[str, dict[str, Any]] = {}
        self.counter = 0
        self.y = 20

    def add(
        self,
        opcode: str,
        *,
        inputs: dict[str, Any] | None = None,
        fields: dict[str, Any] | None = None,
        shadow: bool = False,
        top_level: bool = False,
        mutation: dict[str, Any] | None = None,
    ) -> str:
        self.counter += 1
        block_id = f"gd-{self.target}-{self.counter:03d}"
        block: dict[str, Any] = {
            "opcode": opcode,
            "next": None,
            "parent": None,
            "inputs": inputs or {},
            "fields": fields or {},
            "shadow": shadow,
            "topLevel": top_level,
        }
        if top_level:
            block["x"] = 20
            block["y"] = self.y
            self.y += 150
        if mutation is not None:
            block["mutation"] = mutation
        self.blocks[block_id] = block
        return block_id

    def chain(self, parent: str, children: list[str]) -> None:
        previous = parent
        for child in children:
            self.blocks[previous]["next"] = child
            self.blocks[child]["parent"] = previous
            previous = child

    def substack(self, control: str, children: list[str], name: str = "SUBSTACK") -> None:
        if not children:
            return
        self.blocks[control]["inputs"][name] = [2, children[0]]
        self.blocks[children[0]]["parent"] = control
        for left, right in zip(children, children[1:]):
            self.blocks[left]["next"] = right
            self.blocks[right]["parent"] = left

    def flag(self) -> str:
        return self.add("event_whenflagclicked", top_level=True)

    def receive(self, name: str) -> str:
        return self.add(
            "event_whenbroadcastreceived",
            fields={"BROADCAST_OPTION": [name, MESSAGES[name]]},
            top_level=True,
        )

    def key(self, key: str) -> str:
        return self.add(
            "event_whenkeypressed",
            fields={"KEY_OPTION": [key, None]},
            top_level=True,
        )

    def set_var(self, name: str, variable_id: str, value: Any) -> str:
        return self.add(
            "data_setvariableto",
            inputs={"VALUE": value},
            fields={"VARIABLE": [name, variable_id]},
        )

    def change_var(self, name: str, variable_id: str, value: int) -> str:
        return self.add(
            "data_changevariableby",
            inputs={"VALUE": number(value)},
            fields={"VARIABLE": [name, variable_id]},
        )

    def equals_var(self, parent: str, name: str, variable_id: str, value: str) -> str:
        block_id = self.add(
            "operator_equals",
            inputs={"OPERAND1": variable(name, variable_id), "OPERAND2": text(value)},
        )
        self.blocks[block_id]["parent"] = parent
        return block_id

    def state_is(self, parent: str, value: str) -> str:
        return self.equals_var(parent, "game state", STATE_ID, value)

    def scope_is(self, parent: str, value: str) -> str:
        return self.equals_var(parent, "reset scope", SCOPE_ID, value)

    def not_state(self, parent: str, value: str) -> str:
        block_id = self.add("operator_not")
        self.blocks[block_id]["parent"] = parent
        equals = self.state_is(block_id, value)
        self.blocks[block_id]["inputs"] = {"OPERAND": [2, equals]}
        return block_id

    def either_state(self, parent: str, left: str, right: str) -> str:
        block_id = self.add("operator_or")
        self.blocks[block_id]["parent"] = parent
        left_id = self.state_is(block_id, left)
        right_id = self.state_is(block_id, right)
        self.blocks[block_id]["inputs"] = {
            "OPERAND1": [2, left_id],
            "OPERAND2": [2, right_id],
        }
        return block_id

    def either_scope(self, parent: str, left: str, right: str) -> str:
        block_id = self.add("operator_or")
        self.blocks[block_id]["parent"] = parent
        left_id = self.scope_is(block_id, left)
        right_id = self.scope_is(block_id, right)
        self.blocks[block_id]["inputs"] = {
            "OPERAND1": [2, left_id],
            "OPERAND2": [2, right_id],
        }
        return block_id

    def epoch_matches(self, parent: str, local_id: str) -> str:
        block_id = self.add(
            "operator_equals",
            inputs={
                "OPERAND1": variable("entry epoch", local_id),
                "OPERAND2": variable("state epoch", EPOCH_ID),
            },
        )
        self.blocks[block_id]["parent"] = parent
        return block_id

    def if_epoch_state(self, local_id: str, state: str, body: list[str]) -> str:
        block_id = self.add("control_if")
        condition = self.add("operator_and")
        self.blocks[condition]["parent"] = block_id
        epoch = self.epoch_matches(condition, local_id)
        expected_state = self.state_is(condition, state)
        self.blocks[condition]["inputs"] = {
            "OPERAND1": [2, epoch],
            "OPERAND2": [2, expected_state],
        }
        self.blocks[block_id]["inputs"]["CONDITION"] = [2, condition]
        self.substack(block_id, body)
        return block_id

    def if_epoch_either_state(
        self,
        local_id: str,
        left: str,
        right: str,
        body: list[str],
    ) -> str:
        block_id = self.add("control_if")
        condition = self.add("operator_and")
        self.blocks[condition]["parent"] = block_id
        epoch = self.epoch_matches(condition, local_id)
        expected_state = self.either_state(condition, left, right)
        self.blocks[condition]["inputs"] = {
            "OPERAND1": [2, epoch],
            "OPERAND2": [2, expected_state],
        }
        self.blocks[block_id]["inputs"]["CONDITION"] = [2, condition]
        self.substack(block_id, body)
        return block_id

    def if_state(self, state: str, body: list[str]) -> str:
        block_id = self.add("control_if")
        condition = self.state_is(block_id, state)
        self.blocks[block_id]["inputs"]["CONDITION"] = [2, condition]
        self.substack(block_id, body)
        return block_id

    def if_either_state(self, left: str, right: str, body: list[str]) -> str:
        block_id = self.add("control_if")
        condition = self.either_state(block_id, left, right)
        self.blocks[block_id]["inputs"]["CONDITION"] = [2, condition]
        self.substack(block_id, body)
        return block_id

    def send(self, name: str, *, wait: bool = False) -> str:
        return self.add(
            "event_broadcastandwait" if wait else "event_broadcast",
            inputs={"BROADCAST_INPUT": broadcast(name, MESSAGES[name])},
        )

    def call_transition(self, destination: str, scope: str) -> str:
        mutation = {
            "tagName": "mutation",
            "children": [],
            "proccode": PROCCODE,
            "argumentids": json.dumps(ARG_IDS, separators=(",", ":")),
            "warp": "false",
        }
        return self.add(
            "procedures_call",
            inputs={
                ARG_IDS[0]: text(destination),
                ARG_IDS[1]: text(scope),
            },
            mutation=mutation,
        )

    def hide(self) -> str:
        return self.add("looks_hide")

    def show(self) -> str:
        return self.add("looks_show")

    def go(self, x: int, y: int) -> str:
        return self.add("motion_gotoxy", inputs={"X": number(x), "Y": number(y)})

    def go_to_sprite(self, sprite: str) -> str:
        menu = self.add(
            "motion_goto_menu",
            fields={"TO": [sprite, None]},
            shadow=True,
        )
        block_id = self.add("motion_goto", inputs={"TO": [1, menu]})
        self.blocks[menu]["parent"] = block_id
        return block_id

    def create_clone(self) -> str:
        menu = self.add(
            "control_create_clone_of_menu",
            fields={"CLONE_OPTION": ["_myself_", None]},
            shadow=True,
        )
        block_id = self.add("control_create_clone_of", inputs={"CLONE_OPTION": [1, menu]})
        self.blocks[menu]["parent"] = block_id
        return block_id

    def key_pressed(self, parent: str, key: str) -> str:
        menu = self.add(
            "sensing_keyoptions",
            fields={"KEY_OPTION": [key, None]},
            shadow=True,
        )
        block_id = self.add("sensing_keypressed", inputs={"KEY_OPTION": [1, menu]})
        self.blocks[block_id]["parent"] = parent
        self.blocks[menu]["parent"] = block_id
        return block_id

    def touching(self, parent: str, sprite: str) -> str:
        menu = self.add(
            "sensing_touchingobjectmenu",
            fields={"TOUCHINGOBJECTMENU": [sprite, None]},
            shadow=True,
        )
        block_id = self.add(
            "sensing_touchingobject",
            inputs={"TOUCHINGOBJECTMENU": [1, menu]},
        )
        self.blocks[block_id]["parent"] = parent
        self.blocks[menu]["parent"] = block_id
        return block_id

    def hold_ticks(self, ticks: int) -> str:
        # An empty repeat yields one frame (tick) per iteration under Scratch's
        # screen refresh — a wall-clock-free hold, per the units rule.
        block_id = self.add("control_repeat", inputs={"TIMES": number(ticks)})
        return block_id

    def glide(self, seconds: float, x: int, y: int) -> str:
        return self.add(
            "motion_glidesecstoxy",
            inputs={"SECS": number(seconds), "X": number(x), "Y": number(y)},
        )

    def to_front(self) -> str:
        return self.add(
            "looks_gotofrontback",
            fields={"FRONT_BACK": ["front", None]},
        )

    def send_backward(self, layers: int = 1) -> str:
        return self.add(
            "looks_goforwardbackwardlayers",
            inputs={"NUM": number(layers)},
            fields={"FORWARD_BACKWARD": ["backward", None]},
        )

    def switch_costume(self, costume: str) -> str:
        menu = self.add(
            "looks_costume", fields={"COSTUME": [costume, None]}, shadow=True
        )
        block_id = self.add(
            "looks_switchcostumeto", inputs={"COSTUME": [1, menu]}
        )
        self.blocks[menu]["parent"] = block_id
        return block_id

    def play_sound(self, sound: str) -> str:
        menu = self.add(
            "sound_sounds_menu", fields={"SOUND_MENU": [sound, None]}, shadow=True
        )
        block_id = self.add("sound_play", inputs={"SOUND_MENU": [1, menu]})
        self.blocks[menu]["parent"] = block_id
        return block_id

    def greater(self, parent: str, name: str, variable_id: str, value: int) -> str:
        block_id = self.add(
            "operator_gt",
            inputs={"OPERAND1": variable(name, variable_id), "OPERAND2": number(value)},
        )
        self.blocks[block_id]["parent"] = parent
        return block_id

    def var_equals(self, parent: str, name: str, variable_id: str, value: int) -> str:
        block_id = self.add(
            "operator_equals",
            inputs={"OPERAND1": variable(name, variable_id), "OPERAND2": number(value)},
        )
        self.blocks[block_id]["parent"] = parent
        return block_id

    def stop_others(self) -> str:
        return self.add(
            "control_stop",
            fields={"STOP_OPTION": ["other scripts in sprite", None]},
            mutation={"tagName": "mutation", "children": [], "hasnext": "true"},
        )

    # Arithmetic reporters. Each operand is either a value-input spec (number()/
    # variable()) or a nested reporter's block id (str); a nested reporter has its
    # parent wired here so the tree serializes correctly.
    def _reporter(self, opcode: str, operand1: Any, operand2: Any) -> str:
        block_id = self.add(opcode)
        inputs: dict[str, Any] = {}
        for slot, spec in (("OPERAND1", operand1), ("OPERAND2", operand2)):
            if isinstance(spec, str):
                inputs[slot] = [2, spec]
                self.blocks[spec]["parent"] = block_id
            else:
                inputs[slot] = spec
        self.blocks[block_id]["inputs"] = inputs
        return block_id

    def op_mod(self, a: Any, b: Any) -> str:
        return self._reporter("operator_mod", a, b)

    def op_mul(self, a: Any, b: Any) -> str:
        return self._reporter("operator_mult", a, b)

    def op_add(self, a: Any, b: Any) -> str:
        return self._reporter("operator_add", a, b)

    def op_div(self, a: Any, b: Any) -> str:
        return self._reporter("operator_divide", a, b)

    def op_eq(self, a: Any, b: Any) -> str:
        return self._reporter("operator_equals", a, b)

    def op_floor(self, operand: Any) -> str:
        block_id = self.add("operator_mathop", fields={"OPERATION": ["floor", None]})
        if isinstance(operand, str):
            self.blocks[block_id]["inputs"] = {"NUM": [2, operand]}
            self.blocks[operand]["parent"] = block_id
        else:
            self.blocks[block_id]["inputs"] = {"NUM": operand}
        return block_id

    def set_var_expr(self, name: str, variable_id: str, reporter_id: str) -> str:
        # Set a variable to a reporter expression (the [3, reporter, shadow] input
        # shape, as install_transition_procedure uses for its argument reporters).
        block_id = self.set_var(name, variable_id, [3, reporter_id, [10, ""]])
        self.blocks[reporter_id]["parent"] = block_id
        return block_id

    def if_reporter(self, condition_id: str, body: list[str]) -> str:
        block_id = self.add("control_if")
        self.blocks[condition_id]["parent"] = block_id
        self.blocks[block_id]["inputs"]["CONDITION"] = [2, condition_id]
        self.substack(block_id, body)
        return block_id

    # Indexed list access (INDEX/ITEM operands take value-input specs or nested
    # reporter block ids, wired like the arithmetic reporters above).
    def list_replace(self, list_name: str, list_id: str, index: Any, item: Any) -> str:
        block_id = self.add(
            "data_replaceitemoflist", fields={"LIST": [list_name, list_id]}
        )
        inputs: dict[str, Any] = {}
        for slot, spec in (("INDEX", index), ("ITEM", item)):
            if isinstance(spec, str):
                inputs[slot] = [2, spec]
                self.blocks[spec]["parent"] = block_id
            else:
                inputs[slot] = spec
        self.blocks[block_id]["inputs"] = inputs
        return block_id

    def list_item(self, list_name: str, list_id: str, index: Any) -> str:
        block_id = self.add("data_itemoflist", fields={"LIST": [list_name, list_id]})
        if isinstance(index, str):
            self.blocks[block_id]["inputs"] = {"INDEX": [2, index]}
            self.blocks[index]["parent"] = block_id
        else:
            self.blocks[block_id]["inputs"] = {"INDEX": index}
        return block_id

    def call_proc(self, proccode: str, *, warp: bool) -> str:
        return self.add(
            "procedures_call",
            mutation={
                "tagName": "mutation",
                "children": [],
                "proccode": proccode,
                "argumentids": "[]",
                "warp": "true" if warp else "false",
            },
        )


def install_transition_procedure(blocks: Blocks) -> None:
    definition = blocks.add("procedures_definition", top_level=True)
    prototype = blocks.add(
        "procedures_prototype",
        shadow=True,
        mutation={
            "tagName": "mutation",
            "children": [],
            "proccode": PROCCODE,
            "argumentids": json.dumps(ARG_IDS, separators=(",", ":")),
            "argumentnames": json.dumps(["destination", "scope"], separators=(",", ":")),
            "argumentdefaults": json.dumps(["", "none"], separators=(",", ":")),
            "warp": "false",
        },
    )
    blocks.blocks[definition]["inputs"] = {"custom_block": [1, prototype]}
    blocks.blocks[prototype]["parent"] = definition
    prototype_inputs = {}
    for argument_id, name in zip(ARG_IDS, ("destination", "scope")):
        reporter = blocks.add(
            "argument_reporter_string_number",
            fields={"VALUE": [name, None]},
            shadow=True,
        )
        blocks.blocks[reporter]["parent"] = prototype
        prototype_inputs[argument_id] = [1, reporter]
    blocks.blocks[prototype]["inputs"] = prototype_inputs

    increment = blocks.change_var("state epoch", EPOCH_ID, 1)
    resetting = blocks.set_var("game state", STATE_ID, text("resetting"))
    stop = blocks.send("director stop", wait=True)
    stop_sounds = blocks.add("sound_stopallsounds")
    set_scope = blocks.set_var("reset scope", SCOPE_ID, text(""))
    scope_reporter = blocks.add(
        "argument_reporter_string_number",
        fields={"VALUE": ["scope", None]},
    )
    blocks.blocks[scope_reporter]["parent"] = set_scope
    blocks.blocks[set_scope]["inputs"]["VALUE"] = [3, scope_reporter, [10, ""]]
    clear_outcome = reset_if(
        blocks,
        ("cold-start", "cold-start"),
        [blocks.set_var("death outcome", OUTCOME_ID, text(""))],
    )
    reset = blocks.send("director reset", wait=True)
    set_destination = blocks.set_var("game state", STATE_ID, text(""))
    destination_reporter = blocks.add(
        "argument_reporter_string_number",
        fields={"VALUE": ["destination", None]},
    )
    blocks.blocks[destination_reporter]["parent"] = set_destination
    blocks.blocks[set_destination]["inputs"]["VALUE"] = [
        3,
        destination_reporter,
        [10, ""],
    ]
    enter = blocks.send("director enter")
    allowed = blocks.add("control_if")
    contains = blocks.add(
        "data_listcontainsitem",
        fields={"LIST": ["allowed transitions", ALLOWED_ID]},
    )
    blocks.blocks[contains]["parent"] = allowed
    edge = blocks.add("operator_join")
    blocks.blocks[edge]["parent"] = contains
    source_and_arrow = blocks.add(
        "operator_join",
        inputs={
            "STRING1": variable("game state", STATE_ID),
            "STRING2": text(" -> "),
        },
    )
    blocks.blocks[source_and_arrow]["parent"] = edge
    destination_for_edge = blocks.add(
        "argument_reporter_string_number",
        fields={"VALUE": ["destination", None]},
    )
    blocks.blocks[destination_for_edge]["parent"] = edge
    blocks.blocks[edge]["inputs"] = {
        "STRING1": [3, source_and_arrow, [10, ""]],
        "STRING2": [3, destination_for_edge, [10, ""]],
    }
    blocks.blocks[contains]["inputs"] = {"ITEM": [3, edge, [10, ""]]}
    blocks.blocks[allowed]["inputs"]["CONDITION"] = [2, contains]
    blocks.chain(definition, [allowed])
    blocks.substack(
        allowed,
        [
            increment,
            resetting,
            stop,
            stop_sounds,
            set_scope,
            clear_outcome,
            reset,
            set_destination,
            enter,
        ],
    )


def install_rng_step(blocks: Blocks) -> None:
    # SYS-04: one atomic (warp) advance of the shared 16-bit stream, mirroring
    # tools/reference_extract.py rng_step exactly. Arithmetic only (Scratch has no
    # bitwise ops); the golden test in tests/test_spec_docs.py interprets these very
    # blocks against docs/spec/data/rng.json. No caller this slice.
    definition = _install_warp_proc(blocks, RNG_PROCCODE)

    state = lambda: variable("rng state", RNG_STATE_ID)
    high = lambda: variable("rng high", RNG_HIGH_ID)

    # high = floor(state / 256); new_low = (5*(state mod 256) + 1) mod 256 — both read
    # the OLD state (rng state is rewritten last).
    set_high = blocks.set_var_expr(
        "rng high", RNG_HIGH_ID, blocks.op_floor(blocks.op_div(state(), number(256)))
    )
    new_low_expr = blocks.op_mod(
        blocks.op_add(blocks.op_mul(number(5), blocks.op_mod(state(), number(256))), number(1)),
        number(256),
    )
    set_new_low = blocks.set_var_expr("rng new low", RNG_NEW_LOW_ID, new_low_expr)

    # extend flag: the low-byte wrap carry — 1 when new_low == 0 (i.e. 5*low mod 256 ==
    # 255) — then forced to 1 when high bits 7 and 2 are equal (both set or both clear).
    set_flag_zero = blocks.set_var("rng extend", RNG_XFLAG_ID, number(0))
    carry_if = blocks.if_reporter(
        blocks.op_eq(variable("rng new low", RNG_NEW_LOW_ID), number(0)),
        [blocks.set_var("rng extend", RNG_XFLAG_ID, number(1))],
    )
    bit7 = blocks.op_mod(blocks.op_floor(blocks.op_div(high(), number(128))), number(2))
    bit2 = blocks.op_mod(blocks.op_floor(blocks.op_div(high(), number(4))), number(2))
    force_if = blocks.if_reporter(
        blocks.op_eq(bit7, bit2),
        [blocks.set_var("rng extend", RNG_XFLAG_ID, number(1))],
    )

    # new_high = (high*2 | extend) mod 256 (high*2 is even, so + is |); output = (new_low
    # + new_high) mod 256; state = new_high*256 + new_low.
    new_high_expr = blocks.op_mod(
        blocks.op_add(
            blocks.op_mul(high(), number(2)), variable("rng extend", RNG_XFLAG_ID)
        ),
        number(256),
    )
    set_new_high = blocks.set_var_expr("rng new high", RNG_NEW_HIGH_ID, new_high_expr)
    out_expr = blocks.op_mod(
        blocks.op_add(
            variable("rng new low", RNG_NEW_LOW_ID),
            variable("rng new high", RNG_NEW_HIGH_ID),
        ),
        number(256),
    )
    set_out = blocks.set_var_expr("rng out", RNG_OUT_ID, out_expr)
    state_expr = blocks.op_add(
        blocks.op_mul(variable("rng new high", RNG_NEW_HIGH_ID), number(256)),
        variable("rng new low", RNG_NEW_LOW_ID),
    )
    set_state = blocks.set_var_expr("rng state", RNG_STATE_ID, state_expr)

    blocks.chain(
        definition,
        [
            set_high,
            set_new_low,
            set_flag_zero,
            carry_if,
            force_if,
            set_new_high,
            set_out,
            set_state,
        ],
    )


def install_clear_slots(blocks: Blocks) -> None:
    # SYS-02: reset every object slot to empty/idle. A warp (atomic) block so the
    # 64-slot sweep costs no ticks; run on every director reset (all scopes clear
    # transient gameplay). Only `slot type` and `slot state` exist this slice.
    definition = _install_warp_proc(blocks, CLEAR_SLOTS_PROCCODE)

    cursor = lambda: variable("slot index", SLOT_INDEX_ID)
    set_index = blocks.set_var("slot index", SLOT_INDEX_ID, number(1))
    loop = blocks.add("control_repeat", inputs={"TIMES": number(SLOT_COUNT)})
    blocks.substack(
        loop,
        [
            blocks.list_replace("slot type", SLOT_TYPE_ID, cursor(), number(0)),
            blocks.list_replace("slot state", SLOT_STATE_ID, cursor(), number(0)),
            blocks.change_var("slot index", SLOT_INDEX_ID, 1),
        ],
    )
    blocks.chain(definition, [set_index, loop])


def install_advance_slots(blocks: Blocks) -> None:
    # SYS-04 centralized ordered update: one atomic (warp) pass over the 64 slots in
    # ascending index order, advancing the tick clock. Dispatch of each occupied slot's
    # per-type behavior is deferred — no entity type acts this slice.
    definition = _install_warp_proc(blocks, ADVANCE_SLOTS_PROCCODE)

    cursor = lambda: variable("slot index", SLOT_INDEX_ID)
    advance_tick = blocks.change_var("tick", TICK_ID, 1)
    set_index = blocks.set_var("slot index", SLOT_INDEX_ID, number(1))
    loop = blocks.add("control_repeat", inputs={"TIMES": number(SLOT_COUNT)})
    is_empty = blocks.op_eq(
        blocks.list_item("slot type", SLOT_TYPE_ID, cursor()), number(0)
    )
    occupied = blocks.add("operator_not")
    blocks.blocks[occupied]["inputs"] = {"OPERAND": [2, is_empty]}
    blocks.blocks[is_empty]["parent"] = occupied
    # ENGINE-TODO: per-type dispatch of each occupied slot lands with the enemy slice;
    # today the ordered atomic pass visits occupied slots but has no per-type behavior.
    dispatch = blocks.if_reporter(occupied, [])
    blocks.substack(loop, [dispatch, blocks.change_var("slot index", SLOT_INDEX_ID, 1)])
    blocks.chain(definition, [advance_tick, set_index, loop])


def _install_warp_proc(blocks: Blocks, proccode: str) -> str:
    """A no-argument warp custom-block definition; returns the definition id."""
    definition = blocks.add("procedures_definition", top_level=True)
    prototype = blocks.add(
        "procedures_prototype",
        shadow=True,
        mutation={
            "tagName": "mutation",
            "children": [],
            "proccode": proccode,
            "argumentids": "[]",
            "argumentnames": "[]",
            "argumentdefaults": "[]",
            "warp": "true",
        },
    )
    blocks.blocks[definition]["inputs"] = {"custom_block": [1, prototype]}
    blocks.blocks[prototype]["parent"] = definition
    return definition


def install_score(blocks: Blocks) -> None:
    # SYS-03: the single scoring path everything routes through — so a hit can never
    # double-score. Empty this slice; award / high-score / cap land with ECO-01.
    _install_warp_proc(blocks, SCORE_PROCCODE)
    # ENGINE-TODO: scoring (award, high score, 9,999,990 cap) lands with ECO-01; every
    # future scoring route calls through this one `score` hook.


def install_resolve_hit(blocks: Blocks) -> None:
    # SYS-03: resolve one collision exactly once — mark the struck slot HIT and route to
    # the single score path. The struck slot is `hit slot`, set by the detector that a
    # later slice wires (the per-group overlap detection is delegated there).
    definition = _install_warp_proc(blocks, RESOLVE_HIT_PROCCODE)
    blocks.chain(
        definition,
        [
            blocks.list_replace(
                "slot state",
                SLOT_STATE_ID,
                variable("hit slot", HIT_SLOT_ID),
                number(SLOT_HIT),
            ),
            blocks.call_proc(SCORE_PROCCODE, warp=True),
        ],
    )


def stage_blocks() -> dict[str, dict[str, Any]]:
    blocks = Blocks("stage")
    install_transition_procedure(blocks)
    install_rng_step(blocks)
    install_clear_slots(blocks)
    install_advance_slots(blocks)
    install_score(blocks)
    install_resolve_hit(blocks)

    flag = blocks.flag()
    blocks.chain(
        flag,
        [
            blocks.set_var("state epoch", EPOCH_ID, number(0)),
            blocks.set_var("death outcome", OUTCOME_ID, text("")),
            blocks.set_var("game state", STATE_ID, text("boot")),
            blocks.call_transition("title", "cold-start"),
        ],
    )

    space = blocks.key("space")
    blocks.chain(space, [blocks.if_state("title", [blocks.call_transition("ready", "new-game")])])

    for key, outcome in (("d", "respawn"), ("g", "game-over")):
        hat = blocks.key(key)
        set_outcome = blocks.set_var("death outcome", OUTCOME_ID, text(outcome))
        transition = blocks.call_transition("player-dead", "none")
        blocks.chain(hat, [blocks.if_state("playing", [set_outcome, transition])])

    ready = blocks.receive("ready complete")
    blocks.chain(
        ready,
        [
            blocks.if_either_state(
                "ready",
                "respawning",
                [blocks.call_transition("playing", "none")],
            )
        ],
    )

    death = blocks.receive("death complete")
    respawn_if = blocks.add("control_if")
    respawn_condition = blocks.equals_var(
        respawn_if, "death outcome", OUTCOME_ID, "respawn"
    )
    blocks.blocks[respawn_if]["inputs"]["CONDITION"] = [2, respawn_condition]
    blocks.substack(respawn_if, [blocks.call_transition("respawning", "new-life")])
    game_over_if = blocks.add("control_if")
    game_over_condition = blocks.equals_var(
        game_over_if, "death outcome", OUTCOME_ID, "game-over"
    )
    blocks.blocks[game_over_if]["inputs"]["CONDITION"] = [2, game_over_condition]
    blocks.substack(game_over_if, [blocks.call_transition("game-over", "game-over")])
    blocks.chain(
        death,
        [blocks.if_state("player-dead", [respawn_if, game_over_if])],
    )

    game_over = blocks.receive("game over complete")
    blocks.chain(
        game_over,
        [blocks.if_state("game-over", [blocks.call_transition("title", "cold-start")])],
    )

    enter = blocks.receive("director enter")
    start_sound = blocks.add(
        "sound_playuntildone",
        inputs={"SOUND_MENU": [1, blocks.add(
            "sound_sounds_menu",
            fields={"SOUND_MENU": ["Game Start.mp3", None]},
            shadow=True,
        )]},
    )
    sound_menu_id = blocks.blocks[start_sound]["inputs"]["SOUND_MENU"][1]
    blocks.blocks[sound_menu_id]["parent"] = start_sound
    loop = blocks.add("control_repeat_until")
    stop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, stop_condition]
    bgm_menu = blocks.add(
        "sound_sounds_menu",
        fields={"SOUND_MENU": ["BGM.mp3", None]},
        shadow=True,
    )
    bgm = blocks.add("sound_playuntildone", inputs={"SOUND_MENU": [1, bgm_menu]})
    blocks.blocks[bgm_menu]["parent"] = bgm
    blocks.substack(loop, [bgm])
    blocks.chain(enter, [blocks.if_state("playing", [start_sound, loop])])

    # SYS-04 centralized ordered update: a second `director enter` thread (parallel to
    # the BGM loop above) drives one `advance slots` atomic pass per tick while playing.
    walk_enter = blocks.receive("director enter")
    walk_loop = blocks.add("control_repeat_until")
    walk_condition = blocks.not_state(walk_loop, "playing")
    blocks.blocks[walk_loop]["inputs"]["CONDITION"] = [2, walk_condition]
    blocks.substack(walk_loop, [blocks.call_proc(ADVANCE_SLOTS_PROCCODE, warp=True)])
    blocks.chain(walk_enter, [blocks.if_state("playing", [walk_loop])])

    # A NEW Stage `director reset` receiver — kept out of the transition procedure body
    # so its pinned opcode sequence stays byte-identical. `reset scope` is already set
    # before the reset broadcast fires. It (SYS-02) clears the object slots on every
    # reset — all reset scopes clear transient gameplay — and, on a world reset
    # (cold-start / new-game), seeds the shared stream (SYS-04, so seeded runs repeat)
    # and starts the frame clock at zero.
    stage_reset = blocks.receive("director reset")
    blocks.chain(
        stage_reset,
        [
            blocks.call_proc(CLEAR_SLOTS_PROCCODE, warp=True),
            reset_if(
                blocks,
                ("cold-start", "new-game"),
                [
                    blocks.set_var("rng state", RNG_STATE_ID, number(RNG_COLD_START_SEED)),
                    blocks.set_var("tick", TICK_ID, number(0)),
                ],
            ),
        ],
    )
    return blocks.blocks


def common_stop(blocks: Blocks, *, hide: bool, clones: bool = False) -> None:
    hat = blocks.receive("director stop")
    commands = [blocks.stop_others(), blocks.add("sound_stopallsounds")]
    if clones:
        commands.append(blocks.add("control_delete_this_clone"))
    if hide:
        commands.append(blocks.hide())
    blocks.chain(hat, commands)


def reset_if(
    blocks: Blocks,
    scopes: tuple[str, str],
    commands: list[str],
) -> str:
    control = blocks.add("control_if")
    condition = blocks.either_scope(control, *scopes)
    blocks.blocks[control]["inputs"]["CONDITION"] = [2, condition]
    blocks.substack(control, commands)
    return control


def solvalou_blocks() -> dict[str, dict[str, Any]]:
    blocks = Blocks("solvalou")
    common_stop(blocks, hide=True)
    reset = blocks.receive("director reset")
    blocks.chain(reset, [reset_if(blocks, ("cold-start", "new-game"), [blocks.go(0, -85), blocks.hide()]), reset_if(blocks, ("new-life", "game-over"), [blocks.go(0, -85), blocks.hide()])])

    enter = blocks.receive("director enter")
    snapshot = blocks.set_var(
        "entry epoch",
        SOLVALOU_EPOCH_ID,
        variable("state epoch", EPOCH_ID),
    )
    title = blocks.if_state("title", [blocks.hide()])
    # A1: the invented READY speech bubble is removed, but its 30-tick READY beat is
    # kept — re-expressed as a tick-counted hold (project-defined placeholder, no
    # reference basis; core-game-systems). Removing it bare would silently collapse the
    # recorded READY hold to zero.
    ready_hold = blocks.hold_ticks(READY_HOLD_TICKS)
    ready_body = [
        blocks.go(0, -85),
        blocks.show(),
        ready_hold,
        blocks.if_epoch_either_state(
            SOLVALOU_EPOCH_ID,
            "ready",
            "respawning",
            [blocks.send("ready complete")],
        ),
    ]
    ready = blocks.if_either_state("ready", "respawning", ready_body)
    movement = blocks.add("control_repeat_until")
    movement_condition = blocks.not_state(movement, "playing")
    blocks.blocks[movement]["inputs"]["CONDITION"] = [2, movement_condition]
    # B9: the craft fronts itself every tick, so it renders above the terrain, the
    # shots, and the frame borders (which the audit found were covering the ship).
    movement_body = [blocks.to_front()]
    for key, (opcode, input_name, amount) in {
        "left arrow": ("motion_changexby", "DX", -7),
        "right arrow": ("motion_changexby", "DX", 7),
        "up arrow": ("motion_changeyby", "DY", 7),
        "down arrow": ("motion_changeyby", "DY", -7),
    }.items():
        pressed = blocks.add("control_if")
        blocks.blocks[pressed]["inputs"]["CONDITION"] = [
            2,
            blocks.key_pressed(pressed, key),
        ]
        blocks.substack(
            pressed,
            [blocks.add(opcode, inputs={input_name: number(amount)})],
        )
        movement_body.append(pressed)
    for frame, opcode, input_name, amount, message in (
        ("frame_b", "motion_changeyby", "DY", 7, "target_b"),
        ("frame_l", "motion_changexby", "DX", 7, "target_l"),
        ("frame_r", "motion_changexby", "DX", -7, "target_r"),
    ):
        correction = blocks.add("control_if")
        blocks.blocks[correction]["inputs"]["CONDITION"] = [
            2,
            blocks.touching(correction, frame),
        ]
        blocks.substack(
            correction,
            [
                blocks.add(opcode, inputs={input_name: number(amount)}),
                blocks.send(message),
            ],
        )
        movement_body.append(correction)
    blocks.substack(movement, movement_body)
    playing = blocks.if_state("playing", [blocks.show(), movement])
    dead = blocks.if_either_state("player-dead", "game-over", [blocks.hide()])
    blocks.chain(enter, [snapshot, title, ready, playing, dead])

    top = blocks.receive("target_t")
    blocks.chain(top, [blocks.add("motion_changeyby", inputs={"DY": number(-7)})])
    return blocks.blocks


def title_blocks() -> dict[str, dict[str, Any]]:
    blocks = Blocks("start-screen")
    common_stop(blocks, hide=True)
    reset = blocks.receive("director reset")
    blocks.chain(reset, [blocks.hide()])
    enter = blocks.receive("director enter")
    # B4: the logo enters at the top and glides to center (baseline: 1 s from y=250).
    # Preserved-baseline presentation; the glide is wall-clock (a presentation beat,
    # not gameplay timing).
    blocks.chain(
        enter,
        [blocks.if_state("title", [blocks.go(0, 250), blocks.show(), blocks.glide(1, 0, 0)])],
    )
    return blocks.blocks


def death_blocks() -> dict[str, dict[str, Any]]:
    blocks = Blocks("solv-death")
    common_stop(blocks, hide=True)
    reset = blocks.receive("director reset")
    blocks.chain(reset, [blocks.hide()])
    enter = blocks.receive("director enter")
    snapshot = blocks.set_var(
        "entry epoch",
        DEATH_EPOCH_ID,
        variable("state epoch", EPOCH_ID),
    )
    # B5/B10: the ~56-frame (28-tick) explosion, then a 32-frame (16-tick) pause before
    # the respawn transition, so the transition's stop-all-sounds no longer truncates
    # the death cue (measured 1.361 s < 28+16 ticks = 1.467 s). Holds are flat, empty
    # repeats — one tick each, so the total is exactly the counted ticks. Arcade frame
    # counts cite PLY-02; only the tick roundings live here.
    explosion: list[str] = [blocks.switch_costume("explode_01")]
    for _ in range(EXPLOSION_STEPS):
        explosion.append(blocks.hold_ticks(EXPLOSION_HOLD_TICKS))
        explosion.append(blocks.add("looks_nextcostume"))
    death_body = [
        blocks.go_to_sprite("solvalou"),
        blocks.to_front(),  # B9: the explosion renders above the terrain
        blocks.show(),
        blocks.play_sound("solvalou_death"),
        *explosion,
        blocks.hold_ticks(POST_DEATH_PAUSE_TICKS),
        blocks.if_epoch_state(
            DEATH_EPOCH_ID, "player-dead", [blocks.send("death complete")]
        ),
    ]
    dead = blocks.if_state("player-dead", death_body)
    # A2: the invented GAME OVER speech bubble is removed. The game-over presentation —
    # its 128-frame hold included — is owned by ECO-04 and deferred there; recorded,
    # not silently dropped.
    over = blocks.if_state(
        "game-over",
        [
            blocks.show(),
            blocks.if_epoch_state(
                DEATH_EPOCH_ID,
                "game-over",
                [blocks.send("game over complete")],
            ),
        ],
    )
    blocks.chain(enter, [snapshot, dead, over])
    return blocks.blocks


def terrain_blocks(
    name: str, costume: str, start_y: int, step_id: str, initial_step: int
) -> dict[str, dict[str, Any]]:
    blocks = Blocks(name)
    common_stop(blocks, hide=False)
    reset = blocks.receive("director reset")
    switch = blocks.switch_costume(costume)
    # Cold-start / new-game rewind to the strip's top and re-seed its scroll counter;
    # a new life preserves both (the recorded B11 terrain-on-death fixture), so the
    # strip resumes seamlessly rather than restarting.
    reset_control = reset_if(
        blocks,
        ("cold-start", "new-game"),
        [
            switch,
            blocks.go(0, start_y),
            blocks.set_var("scroll step", step_id, number(initial_step)),
            blocks.send_backward(),  # B9: terrain sits behind the sprites
            blocks.show(),
        ],
    )
    blocks.chain(reset, [reset_control])

    enter = blocks.receive("director enter")
    loop = blocks.add("control_repeat_until")
    condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, condition]
    move = blocks.add("motion_changeyby", inputs={"DY": number(-1)})
    advance = blocks.change_var("scroll step", step_id, 1)
    # B3: counted-cycle wrap (baseline: 690 steps per strip). The former position test
    # (y < -345) was unreachable — Scratch fencing pins a full-height strip at -345, so
    # both strips parked and the screen went black. Counting the steps always fires.
    wrap_if = blocks.add("control_if")
    reached = blocks.greater(wrap_if, "scroll step", step_id, 689)
    blocks.blocks[wrap_if]["inputs"]["CONDITION"] = [2, reached]
    blocks.substack(
        wrap_if,
        [
            blocks.set_var("scroll step", step_id, number(0)),
            blocks.go(0, 345),
            blocks.add("looks_nextcostume"),
        ],
    )
    blocks.substack(loop, [move, advance, wrap_if])
    blocks.chain(
        enter,
        [blocks.if_state("playing", [blocks.send_backward(), blocks.show(), loop])],
    )
    return blocks.blocks


def install_alloc_shot_slot(blocks: Blocks) -> None:
    # Allocate the first idle player-shot slot (37-39): `alloc result` becomes that index,
    # or stays 0 when all three are live — the structural 3-shot cap (audit A3). Warp
    # (atomic), unrolled over the three dedicated slots (no cursor). Global slot lists.
    definition = _install_warp_proc(blocks, ALLOC_SHOT_PROCCODE)

    body = [blocks.set_var("alloc result", ALLOC_RESULT_ID, number(0))]
    for index in range(SHOT_SLOTS[0], SHOT_SLOTS[1] + 1):
        unallocated = blocks.op_eq(
            variable("alloc result", ALLOC_RESULT_ID), number(0)
        )
        slot_free = blocks.op_eq(
            blocks.list_item("slot type", SLOT_TYPE_ID, number(index)), number(0)
        )
        condition = blocks.add("operator_and")
        blocks.blocks[condition]["inputs"] = {
            "OPERAND1": [2, unallocated],
            "OPERAND2": [2, slot_free],
        }
        blocks.blocks[unallocated]["parent"] = condition
        blocks.blocks[slot_free]["parent"] = condition
        body.append(
            blocks.if_reporter(
                condition,
                [
                    blocks.list_replace(
                        "slot type", SLOT_TYPE_ID, number(index), number(SHOT_TYPE)
                    ),
                    blocks.list_replace(
                        "slot state", SLOT_STATE_ID, number(index), number(SLOT_ACTIVE)
                    ),
                    blocks.set_var("alloc result", ALLOC_RESULT_ID, number(index)),
                ],
            )
        )
    blocks.chain(definition, body)


def blaster_blocks() -> dict[str, dict[str, Any]]:
    blocks = Blocks("blaster")
    common_stop(blocks, hide=True, clones=True)
    install_alloc_shot_slot(blocks)
    # Reset clears the reload counter (WPN-01: a fresh press fires at once) so holding
    # fire through death never delays the first post-respawn shot.
    reset = blocks.receive("director reset")
    blocks.chain(
        reset,
        [
            blocks.add("control_delete_this_clone"),
            blocks.set_var("blaster reload", RELOAD_ID, number(RELOAD_TICKS)),
            blocks.hide(),
        ],
    )

    # B1: polled fire under the director-enter loop (the established pattern), not an
    # OS-repeat key hat. Fire immediately when ready, then reload every RELOAD_TICKS
    # ticks while held; releasing re-primes so the next press fires at once.
    enter = blocks.receive("director enter")
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]

    advance = blocks.change_var("blaster reload", RELOAD_ID, 1)
    fire_gate = blocks.add("control_if")
    space_and_ready = blocks.add("operator_and")
    blocks.blocks[space_and_ready]["parent"] = fire_gate
    pressed = blocks.key_pressed(space_and_ready, "space")
    ready = blocks.greater(space_and_ready, "blaster reload", RELOAD_ID, RELOAD_TICKS - 1)
    blocks.blocks[space_and_ready]["inputs"] = {
        "OPERAND1": [2, pressed],
        "OPERAND2": [2, ready],
    }
    blocks.blocks[fire_gate]["inputs"]["CONDITION"] = [2, space_and_ready]
    # A3 3-shot cap: allocate a shot slot first; only on success (a free slot) do we
    # spawn AND consume the reload. If all three slots are live the reload is NOT reset,
    # so fire happens the instant a slot frees — B1 cadence preserved, no clone/slot
    # mismatch (every create_clone is dominated by a successful alloc).
    alloc_call = blocks.call_proc(ALLOC_SHOT_PROCCODE, warp=True)
    alloc_ok = blocks.add(
        "operator_gt",
        inputs={
            "OPERAND1": variable("alloc result", ALLOC_RESULT_ID),
            "OPERAND2": number(0),
        },
    )
    spawn = blocks.if_reporter(
        alloc_ok,
        [
            blocks.go_to_sprite("solvalou"),
            blocks.create_clone(),
            blocks.set_var("blaster reload", RELOAD_ID, number(0)),
        ],
    )
    blocks.substack(fire_gate, [alloc_call, spawn])
    release_gate = blocks.add("control_if")
    not_pressed = blocks.add("operator_not")
    blocks.blocks[not_pressed]["parent"] = release_gate
    released = blocks.key_pressed(not_pressed, "space")
    blocks.blocks[not_pressed]["inputs"] = {"OPERAND": [2, released]}
    blocks.blocks[release_gate]["inputs"]["CONDITION"] = [2, not_pressed]
    blocks.substack(
        release_gate,
        [blocks.set_var("blaster reload", RELOAD_ID, number(RELOAD_TICKS))],
    )
    blocks.substack(loop, [advance, fire_gate, release_gate])
    blocks.chain(
        enter,
        [
            blocks.if_state(
                "playing",
                [blocks.set_var("blaster reload", RELOAD_ID, number(RELOAD_TICKS)), loop],
            )
        ],
    )

    # B8: the shot flies forward at the baseline speed and expires the instant it
    # reaches the top border — no edge-parking, no fixed step count. Direction and
    # top-expiry cite WPN-01; the DY magnitude is preserved-baseline (spatial factor
    # unratified until the movement slice).
    clone = blocks.add("control_start_as_clone", top_level=True)
    travel = blocks.add("control_repeat_until")
    at_top = blocks.touching(travel, "frame_t")
    blocks.blocks[travel]["inputs"]["CONDITION"] = [2, at_top]
    blocks.substack(
        travel,
        [
            blocks.add("motion_changeyby", inputs={"DY": number(20)}),
            blocks.add("looks_nextcostume"),
        ],
    )
    # The clone snapshots `alloc result` (its allocated index) into its own `clone slot`
    # at birth, and frees that slot on expiry — so every delete path returns the slot to
    # the pool and the cap can never desync. (director stop / reset paths are covered by
    # the Stage's clear-slots on every reset.)
    blocks.chain(
        clone,
        [
            blocks.set_var(
                "clone slot", CLONE_SLOT_ID, variable("alloc result", ALLOC_RESULT_ID)
            ),
            blocks.to_front(),  # B9: shots render above the terrain
            blocks.show(),
            blocks.play_sound("blaster"),
            travel,
            blocks.list_replace(
                "slot type", SLOT_TYPE_ID, variable("clone slot", CLONE_SLOT_ID), number(0)
            ),
            blocks.list_replace(
                "slot state",
                SLOT_STATE_ID,
                variable("clone slot", CLONE_SLOT_ID),
                number(0),
            ),
            blocks.add("control_delete_this_clone"),
        ],
    )
    return blocks.blocks


def bomb_blocks() -> dict[str, dict[str, Any]]:
    blocks = Blocks("bomb")
    common_stop(blocks, hide=True)
    # Reset unconditionally re-arms the bomb — every transition passes through reset,
    # and the reset-scope postconditions require "clear bomb". Without this an in-flight
    # bomb interrupted by a death (a routine sequence) would strand the guard set and
    # lock out bombing for the rest of the game.
    reset = blocks.receive("director reset")
    blocks.chain(
        reset,
        [blocks.set_var("bomb in flight", BOMB_INFLIGHT_ID, number(0)), blocks.hide()],
    )

    # B2: one bomb at a time. The poller arms a bomb only when the slot is idle
    # (WPN-04: arming requires the bomb-target slot idle) and broadcasts `bomb`, which
    # drives the drop below plus the crosshair release (B6) and the impact marker (B7).
    enter = blocks.receive("director enter")
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    arm_gate = blocks.add("control_if")
    idle_and_pressed = blocks.add("operator_and")
    blocks.blocks[idle_and_pressed]["parent"] = arm_gate
    b_pressed = blocks.key_pressed(idle_and_pressed, "b")
    slot_idle = blocks.var_equals(idle_and_pressed, "bomb in flight", BOMB_INFLIGHT_ID, 0)
    blocks.blocks[idle_and_pressed]["inputs"] = {
        "OPERAND1": [2, b_pressed],
        "OPERAND2": [2, slot_idle],
    }
    blocks.blocks[arm_gate]["inputs"]["CONDITION"] = [2, idle_and_pressed]
    blocks.substack(
        arm_gate,
        [
            blocks.set_var("bomb in flight", BOMB_INFLIGHT_ID, number(1)),
            blocks.send("bomb"),
        ],
    )
    blocks.substack(loop, [arm_gate])
    blocks.chain(enter, [blocks.if_state("playing", [loop])])

    # The drop: to the ship, then a two-stage fall, then re-arm the slot (the natural
    # resolve-time clear; the reset above is the death-interrupt backstop). The re-arm
    # timing is preserved-baseline (baseline ~0.75 s cooldown); the arcade re-arm path
    # is unpinned in the reference (WPN-04).
    release = blocks.receive("bomb")
    flight = blocks.add("control_repeat", inputs={"TIMES": number(12)})
    blocks.substack(flight, [blocks.add("motion_changeyby", inputs={"DY": number(5)})])
    explode = blocks.add("control_repeat", inputs={"TIMES": number(4)})
    blocks.substack(explode, [blocks.add("looks_nextcostume"), blocks.hold_ticks(2)])
    blocks.chain(
        release,
        [
            blocks.to_front(),  # B9: the bomb renders above the terrain
            blocks.go_to_sprite("solvalou"),
            blocks.switch_costume("bomb_01"),
            blocks.show(),
            blocks.play_sound("bomb_drop"),
            flight,
            blocks.play_sound("bomb_explode"),
            explode,
            blocks.hide(),
            blocks.set_var("bomb in flight", BOMB_INFLIGHT_ID, number(0)),
        ],
    )
    return blocks.blocks


def target_blocks(name: str, y: int) -> dict[str, dict[str, Any]]:
    blocks = Blocks(name)
    common_stop(blocks, hide=True)
    reset = blocks.receive("director reset")
    blocks.chain(reset, [blocks.go(0, y), blocks.hide()])
    enter = blocks.receive("director enter")
    if name == "target_a":
        movement = blocks.add("control_repeat_until")
        movement_condition = blocks.not_state(movement, "playing")
        blocks.blocks[movement]["inputs"]["CONDITION"] = [2, movement_condition]
        movement_body = []
        for key, (opcode, input_name, amount) in {
            "left arrow": ("motion_changexby", "DX", -7),
            "right arrow": ("motion_changexby", "DX", 7),
            "up arrow": ("motion_changeyby", "DY", 7),
            "down arrow": ("motion_changeyby", "DY", -7),
        }.items():
            pressed = blocks.add("control_if")
            blocks.blocks[pressed]["inputs"]["CONDITION"] = [
                2,
                blocks.key_pressed(pressed, key),
            ]
            blocks.substack(
                pressed,
                [blocks.add(opcode, inputs={input_name: number(amount)})],
            )
            movement_body.append(pressed)
        top = blocks.add("control_if")
        blocks.blocks[top]["inputs"]["CONDITION"] = [
            2,
            blocks.touching(top, "frame_t"),
        ]
        blocks.substack(
            top,
            [
                blocks.add("motion_changeyby", inputs={"DY": number(-7)}),
                blocks.send("target_t"),
            ],
        )
        movement_body.append(top)
        blocks.substack(movement, movement_body)
        blocks.chain(
            enter,
            [
                blocks.if_state(
                    "playing", [blocks.go(0, y), blocks.to_front(), blocks.show(), movement]
                )
            ],
        )
        for message, opcode, input_name, amount in (
            ("target_b", "motion_changeyby", "DY", 7),
            ("target_l", "motion_changexby", "DX", 7),
            ("target_r", "motion_changexby", "DX", -7),
        ):
            correction = blocks.receive(message)
            blocks.chain(
                correction,
                [blocks.add(opcode, inputs={input_name: number(amount)})],
            )
        # B6: the crosshair plays its release animation on each bomb, then returns to
        # its base costume — restored from the frozen single-costume reticle.
        release = blocks.receive("bomb")
        anim = blocks.add("control_repeat", inputs={"TIMES": number(3)})
        blocks.substack(anim, [blocks.add("looks_nextcostume"), blocks.hold_ticks(2)])
        blocks.chain(release, [anim, blocks.switch_costume("target_01")])
    else:
        blocks.chain(enter, [blocks.if_state("playing", [blocks.hide()])])
        # B7: target_b is the ground-impact marker — restored from the inert hide-only
        # sprite. On each bomb it appears at the crosshair and drifts, per the baseline.
        release = blocks.receive("bomb")
        drift = blocks.add("control_repeat", inputs={"TIMES": number(20)})
        blocks.substack(drift, [blocks.add("motion_changeyby", inputs={"DY": number(-1)})])
        blocks.chain(
            release,
            [
                blocks.go_to_sprite("target_a"),
                blocks.switch_costume("target_03"),
                blocks.to_front(),
                blocks.show(),
                drift,
                blocks.hide(),
            ],
        )
    return blocks.blocks


def expected_project(project: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(project)
    stage = next(target for target in result["targets"] if target["isStage"])
    owned_stage_variables = {
        STATE_ID,
        EPOCH_ID,
        SCOPE_ID,
        OUTCOME_ID,
        BOMB_INFLIGHT_ID,
        RNG_STATE_ID,
        RNG_OUT_ID,
        RNG_HIGH_ID,
        RNG_NEW_LOW_ID,
        RNG_NEW_HIGH_ID,
        RNG_XFLAG_ID,
        SLOT_INDEX_ID,
        TICK_ID,
        HIT_SLOT_ID,
    }
    preserved_variables = {
        variable_id: value
        for variable_id, value in stage["variables"].items()
        if variable_id not in owned_stage_variables
        and value[0] not in {"death", "stage"}
    }
    stage["variables"] = preserved_variables | {
        STATE_ID: ["game state", "title"],
        EPOCH_ID: ["state epoch", 0],
        SCOPE_ID: ["reset scope", "cold-start"],
        OUTCOME_ID: ["death outcome", ""],
        # Shared weapon state — the one-bomb lockout the poller and the in-flight bomb
        # both read; cleared by every reset scope (bomb_blocks).
        BOMB_INFLIGHT_ID: ["bomb in flight", 0],
        # SYS-04 shared stream: the seed, its latest output byte, and the four per-step
        # working values (custom blocks have no locals). Cited to rng.json / SYS-04.
        RNG_STATE_ID: ["rng state", 0],
        RNG_OUT_ID: ["rng out", 0],
        RNG_HIGH_ID: ["rng high", 0],
        RNG_NEW_LOW_ID: ["rng new low", 0],
        RNG_NEW_HIGH_ID: ["rng new high", 0],
        RNG_XFLAG_ID: ["rng extend", 0],
        # SYS-02 slot-sweep loop cursor (machinery, not slot data).
        SLOT_INDEX_ID: ["slot index", 0],
        # SYS-04 authoritative gameplay frame counter (advanced by the ordered pass).
        TICK_ID: ["tick", 0],
        # SYS-03 struck slot for the single-hit resolution path (set by a later detector).
        HIT_SLOT_ID: ["hit slot", 0],
    }
    owned_lists = {ALLOWED_ID, SLOT_TYPE_ID, SLOT_STATE_ID}
    preserved_lists = {
        list_id: value
        for list_id, value in stage["lists"].items()
        if list_id not in owned_lists
    }
    stage["lists"] = preserved_lists | {
        ALLOWED_ID: [
            "allowed transitions",
            [
                "boot -> title",
                "title -> ready",
                "ready -> playing",
                "playing -> player-dead",
                "player-dead -> respawning",
                "player-dead -> game-over",
                "respawning -> playing",
                "game-over -> title",
            ],
        ],
        # SYS-02 object slots (index NN+1 = arcade slot 0xNN): type 0 = empty (skipped),
        # state 0 = idle. Fixed length 64; alloc/free change entries, never length.
        SLOT_TYPE_ID: ["slot type", [0] * SLOT_COUNT],
        SLOT_STATE_ID: ["slot state", [0] * SLOT_COUNT],
    }
    stage["broadcasts"] = {message_id: name for name, message_id in MESSAGES.items()}

    replacements = {
        "Stage": stage_blocks(),
        "solvalou": solvalou_blocks(),
        "blaster": blaster_blocks(),
        # The two strips leapfrog: the scroll counter wraps at 690 steps, and each
        # strip's seed sets its phase so they tile seamlessly (baseline geometry).
        # area_01a starts 335 steps into the cycle (baseline pre-roll), so it wraps
        # first after 335 steps: seed 690 - 335 = 355. area_01b runs a full cycle from
        # its start: seed 0.
        "area_01a": terrain_blocks("area_01a", "area01_12-0", -15, TERRAIN_STEP_A_ID, 355),
        "area_01b": terrain_blocks("area_01b", "area01_11-0", 344, TERRAIN_STEP_B_ID, 0),
        "start_screen": title_blocks(),
        "solv_death": death_blocks(),
        "target_a": target_blocks("target_a", 15),
        "target_b": target_blocks("target_b", 2),
        "bomb": bomb_blocks(),
    }
    for target in result["targets"]:
        if target["name"] in replacements:
            target["blocks"] = replacements[target["name"]]
        if target["name"] == "solvalou":
            target["variables"] = target["variables"] | {
                SOLVALOU_EPOCH_ID: ["entry epoch", 0]
            }
        elif target["name"] == "solv_death":
            target["variables"] = target["variables"] | {
                DEATH_EPOCH_ID: ["entry epoch", 0]
            }
        elif target["name"] == "blaster":
            target["variables"] = target["variables"] | {
                RELOAD_ID: ["blaster reload", RELOAD_TICKS],
                # Player-shot allocation result (cap gate) and each clone's own slot index.
                ALLOC_RESULT_ID: ["alloc result", 0],
                CLONE_SLOT_ID: ["clone slot", 0],
            }
        elif target["name"] == "area_01a":
            target["variables"] = target["variables"] | {
                TERRAIN_STEP_A_ID: ["scroll step", 355]
            }
        elif target["name"] == "area_01b":
            target["variables"] = target["variables"] | {
                TERRAIN_STEP_B_ID: ["scroll step", 0]
            }
    return result


def project_bytes(project: dict[str, Any]) -> bytes:
    return scratch_project._ordered_json_bytes(project)


def source_has_local_changes() -> bool:
    relative = str(PROJECT_PATH.relative_to(ROOT))
    for args in (
        ["git", "diff", "--quiet", "--", relative],
        ["git", "diff", "--cached", "--quiet", "--", relative],
    ):
        result = subprocess.run(args, cwd=ROOT, check=False)
        if result.returncode == 1:
            return True
        if result.returncode != 0:
            raise SystemExit("could not verify the Scratch source worktree before generating")
    return False


def generate() -> None:
    if source_has_local_changes():
        raise SystemExit(
            "refusing to overwrite locally edited Scratch source; commit the import, "
            "then port owned block changes into tools/game_director.py"
        )
    current = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))
    PROJECT_PATH.write_bytes(project_bytes(expected_project(current)))
    print(f"generated {PROJECT_PATH.relative_to(ROOT)}")


def check() -> None:
    current = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))
    expected = project_bytes(expected_project(current))
    actual = PROJECT_PATH.read_bytes()
    if actual != expected:
        raise SystemExit(
            "game director source is stale; inspect imported block changes before "
            "running tools/game_director.py generate"
        )
    print("game director source is current")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "check"))
    args = parser.parse_args(argv)
    if args.command == "generate":
        generate()
    else:
        check()
    return 0


if __name__ == "__main__":
    sys.exit(main())
