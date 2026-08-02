#!/usr/bin/env python3
"""Generate and verify the slice-2 Scratch game director."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
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

MESSAGES = {
    "director enter": "broadcastMsgId-director-enter",
    "director stop": "broadcastMsgId-director-stop",
    "director reset": "broadcastMsgId-director-reset",
    "ready complete": "broadcastMsgId-ready-complete",
    "death complete": "broadcastMsgId-death-complete",
    "game over complete": "broadcastMsgId-game-over-complete",
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

    def wait(self, seconds: float) -> str:
        return self.add("control_wait", inputs={"DURATION": number(seconds)})

    def stop_others(self) -> str:
        return self.add(
            "control_stop",
            fields={"STOP_OPTION": ["other scripts in sprite", None]},
            mutation={"tagName": "mutation", "children": [], "hasnext": "true"},
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
    blocks.chain(
        definition,
        [increment, resetting, stop, stop_sounds, set_scope, reset, set_destination, enter],
    )


def stage_blocks() -> dict[str, dict[str, Any]]:
    blocks = Blocks("stage")
    install_transition_procedure(blocks)

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
    ready_message = blocks.add(
        "looks_sayforsecs",
        inputs={"MESSAGE": text("READY"), "SECS": number(1)},
    )
    ready_body = [
        blocks.go(0, -85),
        blocks.show(),
        ready_message,
        blocks.if_epoch_either_state(
            SOLVALOU_EPOCH_ID,
            "ready",
            "respawning",
            [blocks.send("ready complete")],
        ),
    ]
    ready = blocks.if_either_state("ready", "respawning", ready_body)
    playing = blocks.if_state("playing", [blocks.show()])
    dead = blocks.if_either_state("player-dead", "game-over", [blocks.hide()])
    blocks.chain(enter, [snapshot, title, ready, playing, dead])

    moves = {
        "left arrow": ("motion_changexby", "DX", -7),
        "right arrow": ("motion_changexby", "DX", 7),
        "up arrow": ("motion_changeyby", "DY", 7),
        "down arrow": ("motion_changeyby", "DY", -7),
    }
    for key, (opcode, input_name, amount) in moves.items():
        hat = blocks.key(key)
        move = blocks.add(opcode, inputs={input_name: number(amount)})
        edge = blocks.add("motion_ifonedgebounce")
        blocks.chain(hat, [blocks.if_state("playing", [move, edge])])
    return blocks.blocks


def title_blocks() -> dict[str, dict[str, Any]]:
    blocks = Blocks("start-screen")
    common_stop(blocks, hide=True)
    reset = blocks.receive("director reset")
    blocks.chain(reset, [blocks.hide()])
    enter = blocks.receive("director enter")
    blocks.chain(enter, [blocks.if_state("title", [blocks.go(0, 0), blocks.show()])])
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
    switch_first_menu = blocks.add(
        "looks_costume",
        fields={"COSTUME": ["explode_01", None]},
        shadow=True,
    )
    switch_first = blocks.add(
        "looks_switchcostumeto", inputs={"COSTUME": [1, switch_first_menu]}
    )
    blocks.blocks[switch_first_menu]["parent"] = switch_first
    repeat = blocks.add("control_repeat", inputs={"TIMES": number(7)})
    blocks.substack(repeat, [blocks.add("looks_nextcostume"), blocks.wait(0.1)])
    death_body = [blocks.go(0, -85), switch_first, blocks.show(), blocks.add(
        "sound_playuntildone",
        inputs={"SOUND_MENU": [1, blocks.add(
            "sound_sounds_menu",
            fields={"SOUND_MENU": ["solvalou_death", None]},
            shadow=True,
        )]},
    ), repeat, blocks.if_epoch_state(
        DEATH_EPOCH_ID,
        "player-dead",
        [blocks.send("death complete")],
    )]
    death_sound_menu = blocks.blocks[death_body[3]]["inputs"]["SOUND_MENU"][1]
    blocks.blocks[death_sound_menu]["parent"] = death_body[3]
    dead = blocks.if_state("player-dead", death_body)
    over_message = blocks.add(
        "looks_sayforsecs",
        inputs={"MESSAGE": text("GAME OVER"), "SECS": number(2)},
    )
    over = blocks.if_state(
        "game-over",
        [
            blocks.show(),
            over_message,
            blocks.if_epoch_state(
                DEATH_EPOCH_ID,
                "game-over",
                [blocks.send("game over complete")],
            ),
        ],
    )
    blocks.chain(enter, [snapshot, dead, over])
    return blocks.blocks


def terrain_blocks(name: str, costume: str, start_y: int) -> dict[str, dict[str, Any]]:
    blocks = Blocks(name)
    common_stop(blocks, hide=False)
    reset = blocks.receive("director reset")
    costume_menu = blocks.add(
        "looks_costume", fields={"COSTUME": [costume, None]}, shadow=True
    )
    switch = blocks.add("looks_switchcostumeto", inputs={"COSTUME": [1, costume_menu]})
    blocks.blocks[costume_menu]["parent"] = switch
    reset_control = reset_if(
        blocks,
        ("cold-start", "new-game"),
        [switch, blocks.go(0, start_y), blocks.show()],
    )
    blocks.chain(reset, [reset_control])

    enter = blocks.receive("director enter")
    loop = blocks.add("control_repeat_until")
    condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, condition]
    move = blocks.add("motion_changeyby", inputs={"DY": number(-1)})
    wrap_if = blocks.add("control_if")
    y_reporter = blocks.add("motion_yposition")
    less = blocks.add(
        "operator_lt",
        inputs={"OPERAND1": [3, y_reporter, [4, 0]], "OPERAND2": number(-345)},
    )
    blocks.blocks[y_reporter]["parent"] = less
    blocks.blocks[less]["parent"] = wrap_if
    blocks.blocks[wrap_if]["inputs"]["CONDITION"] = [2, less]
    blocks.substack(wrap_if, [blocks.go(0, 345), blocks.add("looks_nextcostume")])
    blocks.substack(loop, [move, wrap_if, blocks.wait(0.02)])
    blocks.chain(enter, [blocks.if_state("playing", [blocks.show(), loop])])
    return blocks.blocks


def blaster_blocks() -> dict[str, dict[str, Any]]:
    blocks = Blocks("blaster")
    common_stop(blocks, hide=True, clones=True)
    reset = blocks.receive("director reset")
    blocks.chain(reset, [blocks.add("control_delete_this_clone"), blocks.hide()])
    key = blocks.key("space")
    blocks.chain(
        key,
        [blocks.if_state("playing", [blocks.go_to_sprite("solvalou"), blocks.create_clone()])],
    )
    clone = blocks.add("control_start_as_clone", top_level=True)
    repeat = blocks.add("control_repeat", inputs={"TIMES": number(40)})
    blocks.substack(repeat, [blocks.add("motion_changeyby", inputs={"DY": number(10)}), blocks.add("looks_nextcostume"), blocks.wait(0.02)])
    sound_menu = blocks.add("sound_sounds_menu", fields={"SOUND_MENU": ["blaster", None]}, shadow=True)
    sound = blocks.add("sound_play", inputs={"SOUND_MENU": [1, sound_menu]})
    blocks.blocks[sound_menu]["parent"] = sound
    blocks.chain(clone, [blocks.go(0, -70), blocks.show(), sound, repeat, blocks.add("control_delete_this_clone")])
    return blocks.blocks


def bomb_blocks() -> dict[str, dict[str, Any]]:
    blocks = Blocks("bomb")
    common_stop(blocks, hide=True, clones=True)
    reset = blocks.receive("director reset")
    blocks.chain(reset, [blocks.add("control_delete_this_clone"), blocks.hide()])
    key = blocks.key("b")
    blocks.chain(
        key,
        [blocks.if_state("playing", [blocks.go_to_sprite("solvalou"), blocks.create_clone()])],
    )
    clone = blocks.add("control_start_as_clone", top_level=True)
    flight = blocks.add("control_repeat", inputs={"TIMES": number(12)})
    blocks.substack(flight, [blocks.add("motion_changeyby", inputs={"DY": number(5)}), blocks.wait(0.03)])
    explode = blocks.add("control_repeat", inputs={"TIMES": number(4)})
    blocks.substack(explode, [blocks.add("looks_nextcostume"), blocks.wait(0.05)])
    drop_menu = blocks.add("sound_sounds_menu", fields={"SOUND_MENU": ["bomb_drop", None]}, shadow=True)
    drop = blocks.add("sound_play", inputs={"SOUND_MENU": [1, drop_menu]})
    blocks.blocks[drop_menu]["parent"] = drop
    explode_menu = blocks.add("sound_sounds_menu", fields={"SOUND_MENU": ["bomb_explode", None]}, shadow=True)
    explode_sound = blocks.add("sound_play", inputs={"SOUND_MENU": [1, explode_menu]})
    blocks.blocks[explode_menu]["parent"] = explode_sound
    blocks.chain(clone, [blocks.go(0, -35), blocks.show(), drop, flight, explode_sound, explode, blocks.add("control_delete_this_clone")])
    return blocks.blocks


def target_blocks(name: str, y: int) -> dict[str, dict[str, Any]]:
    blocks = Blocks(name)
    common_stop(blocks, hide=True)
    reset = blocks.receive("director reset")
    blocks.chain(reset, [blocks.go(0, y), blocks.hide()])
    enter = blocks.receive("director enter")
    blocks.chain(enter, [blocks.if_state("playing", [blocks.go(0, y), blocks.show()])])
    if name == "target_a":
        for key, (opcode, input_name, amount) in {
            "left arrow": ("motion_changexby", "DX", -7),
            "right arrow": ("motion_changexby", "DX", 7),
            "up arrow": ("motion_changeyby", "DY", 7),
            "down arrow": ("motion_changeyby", "DY", -7),
        }.items():
            hat = blocks.key(key)
            blocks.chain(hat, [blocks.if_state("playing", [blocks.add(opcode, inputs={input_name: number(amount)})])])
    return blocks.blocks


def expected_project(project: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(project)
    stage = next(target for target in result["targets"] if target["isStage"])
    preserved_variables = {
        variable_id: value
        for variable_id, value in stage["variables"].items()
        if variable_id not in {STATE_ID, EPOCH_ID, SCOPE_ID, OUTCOME_ID}
        and value[0] not in {"death", "stage"}
    }
    stage["variables"] = preserved_variables | {
        STATE_ID: ["game state", "title"],
        EPOCH_ID: ["state epoch", 0],
        SCOPE_ID: ["reset scope", "cold-start"],
        OUTCOME_ID: ["death outcome", ""],
    }
    preserved_lists = {
        list_id: value
        for list_id, value in stage["lists"].items()
        if list_id != ALLOWED_ID
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
        ]
    }
    retained = {
        key: value
        for key, value in stage["broadcasts"].items()
        if value in {"bomb", "target_b", "target_l", "target_r", "target_t"}
    }
    stage["broadcasts"] = retained | {message_id: name for name, message_id in MESSAGES.items()}

    replacements = {
        "Stage": stage_blocks(),
        "solvalou": solvalou_blocks(),
        "blaster": blaster_blocks(),
        "area_01a": terrain_blocks("area_01a", "area01_12-0", -15),
        "area_01b": terrain_blocks("area_01b", "area01_11-0", 344),
        "start_screen": title_blocks(),
        "solv_death": death_blocks(),
        "target_a": target_blocks("target_a", -37),
        "target_b": target_blocks("target_b", -37),
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
    return result


def project_bytes(project: dict[str, Any]) -> bytes:
    return scratch_project._ordered_json_bytes(project)


def generate() -> None:
    current = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))
    PROJECT_PATH.write_bytes(project_bytes(expected_project(current)))
    print(f"generated {PROJECT_PATH.relative_to(ROOT)}")


def check() -> None:
    current = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))
    expected = project_bytes(expected_project(current))
    actual = PROJECT_PATH.read_bytes()
    if actual != expected:
        raise SystemExit("game director source is stale; run tools/game_director.py generate")
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
