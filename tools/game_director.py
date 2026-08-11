#!/usr/bin/env python3
"""Generate and verify the slice-2 Scratch game director."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import scratch_project


ROOT = Path(__file__).resolve().parents[1]
PROJECT_PATH = ROOT / "src" / "xevious" / scratch_project.PROJECT_JSON
# Cross-language identifier index the JS runtime harness (harness/) consumes so it
# never keeps a third, silently-drifting copy of the project's variable names. Emitted
# beside project.json (not under assets/, so it never enters the built .sb3) and kept
# in sync by check(); see test_runtime_identifier_manifest_is_current.
MANIFEST_PATH = ROOT / "src" / "xevious" / "runtime_identifiers.json"
MANIFEST_SCHEMA = "xevious-runtime-identifiers/1"

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
GAME_OVER_HOLD_TICKS = 64  # arcade 128-frame GAME OVER hold (`game_over` 549-591; ECO-04)

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

# AIR-12 enemy-bullet pool foundation — DORMANT this slice (no firer). The 19 bullet slots
# (40-58) already exist as a range and a collision-group member (#14); this slice adds the
# allocator a firer will call, mirroring `alloc shot slot` but with its OWN result var (never
# the blaster's). The aimed vector, ballistic movement, expiry margins, colour pulse, and the
# slot x/y authoring are AIR-12's firing behaviour, owned by the air slice (slice 8); this
# allocator may be REVISED there if aimed bullets seed a position/vector at allocation.
BULLET_TYPE = 2  # enemy-bullet occupancy marker (SHOT_TYPE=1; per-entity codes grow by slice)
BULLET_ALLOC_RESULT_ID = "bullet-alloc-result"
# The allocator's own sweep cursor — deliberately NOT the shared `slot index`. A firer will
# most naturally call `alloc bullet slot` from inside the `advance slots` per-slot dispatch,
# which is itself sweeping on `slot index`; nesting two loops on one cursor would corrupt the
# outer sweep (warp atomicity prevents inter-thread preemption, not same-variable reentrancy).
BULLET_CURSOR_ID = "bullet-cursor"
ALLOC_BULLET_PROCCODE = "alloc bullet slot"

# PLY-02 collision hit windows, in the reference's half-pixel "shadow" units as
# (y_bias, y_width, x_bias, x_width) — recorded now as DORMANT data (no detector this
# slice). The collision slice converts shadow->pixel and applies the port scale; the
# unit label lives here and in docs/mechanics/008 so that conversion is not lost. Two
# windows: the shared enemy-bullet/flying-enemy window, and the distinct, larger Bacura one.
HIT_WINDOW_BULLET_FLYING = (8, 16, 4, 8)
HIT_WINDOW_BACURA = (28, 40, 8, 16)

# ECO-02 HUD target (docs/mechanics/010, docs/mechanics/012). game_director owns this target's
# EXISTENCE and BLOCKS — the HUD render itself (hud_blocks(), installed below); its costumes
# (the white glyph/digit set, the yellow hs/* "HIGH SCORE" set, and the life icon) are owned
# entirely by tools/hud_glyphs.py, mirroring the solvalou split (one generator owns blocks, the
# other owns costumes, neither touches the other's field).
HUD_TARGET = "hud"

# ECO-02 HUD render (docs/mechanics/012). The hud sprite stays hidden and only ever spawns
# clones; every clone's costume/position is display logic reading score/high score/craft — the
# HUD never writes them (the existing director-variable write-forbid guard already spans every
# non-Stage target, so it enforces this read-only invariant for free). All HUD state below is
# sprite-local to the hud target, never a Stage variable.
HUD_ROLE_ID = "hud-role"
HUD_PLACE_ID = "hud-place"
HUD_DIVISOR_ID = "hud-divisor"
HUD_LIFE_INDEX_ID = "hud-life-index"
HUD_LIFE_COUNT_ID = "hud-life-count"
HUD_IS_CLONE_ID = "hud-is-clone"
# Role tags snapshotted into each clone at creation (the blaster clone-slot idiom): which of the
# five clone kinds this clone is. 0 (unset) never matches any role, so it also doubles as the
# original sprite's permanent "I am not a clone" marker for `hud is clone` gating.
HUD_ROLE_SCORE_DIGIT = 1
HUD_ROLE_HIGH_SCORE_DIGIT = 2
HUD_ROLE_LIFE = 3
HUD_ROLE_LABEL_1UP = 4
HUD_ROLE_LABEL_HIGH_SCORE = 5
HUD_ROLE_GAME_OVER_GLYPH = 6  # ECO-04: the "GAME OVER" text, distinct from every other role
HUD_DIGIT_PLACES = 7  # 0 (units) .. 6 (millions) — SCORE_CAP (9,999,990) is 7 BCD digits
HUD_DIGIT_SPACING = 14
# Project-defined top-band layout (stage -240..240 x, -180..180 y, +y up); the operator
# fine-tunes exact placement at playtest (no reference basis this commit — see ECO-02 record).
HUD_SCORE_LEFT_X = -220  # place 6 (leftmost, most significant digit)
HUD_SCORE_Y = 155
HUD_HIGH_SCORE_LEFT_X = -20
HUD_HIGH_SCORE_Y = 155
HUD_LABEL_Y = 172
HUD_1UP_LEFT_X = -192
HUD_HIGH_SCORE_LABEL_LEFT_X = -40
HUD_LIFE_LEFT_X = -220
HUD_LIFE_Y = 128
HUD_LIFE_SPACING = 18
# Rendered life-icon cap (usability fix): uncapped, the row is one clone per `craft`, and at
# ~169 craft (reachable by holding the debug S key to the score cap) the icons run off the
# right edge of the 480-wide stage. Capping the RENDERED row at 9 ends it at x = HUD_LIFE_LEFT_X
# + (HUD_LIFE_MAX - 1) * HUD_LIFE_SPACING = -220 + 8*18 = -76, clear of the high-score group at
# x=-20. The true `craft` count (and the score digits the cap-test actually exercises) is
# unaffected — only the icon DISPLAY is bounded.
HUD_LIFE_MAX = 9
HUD_1UP_FLASH_HOLD_TICKS = 15  # project-defined flash cadence, no reference basis
# (glyph costume, slot) pairs — slot spacing leaves a gap for the untyped space in "HIGH SCORE".
HUD_1UP_LABEL = (("digit/1", 0), ("glyph/U", 1), ("glyph/P", 2))
# "HIGH SCORE" renders in the yellow hs/* costume set (arcade fidelity: that one HUD label is
# yellow, everything else — score/high-score digits, 1UP, GAME OVER — is white).
HUD_HIGH_SCORE_LABEL = (
    ("hs/H", 0), ("hs/I", 1), ("hs/G", 2), ("hs/H", 3),
    ("hs/S", 5), ("hs/C", 6), ("hs/O", 7), ("hs/R", 8), ("hs/E", 9),
)
# ECO-04: "GAME OVER", centered on the stage (slot 4 — the untyped space between the two
# words — sits at x=0). Fully unrolled like the two label rows above, so no runtime index
# var is needed; the HUD_ROLE_GAME_OVER_GLYPH clones are static once spawned.
HUD_GAME_OVER_LEFT_X = -64
HUD_GAME_OVER_Y = 8
HUD_GAME_OVER_SPACING = 16
HUD_GAME_OVER_LABEL = (
    ("glyph/G", 0), ("glyph/A", 1), ("glyph/M", 2), ("glyph/E", 3),
    ("glyph/O", 5), ("glyph/V", 6), ("glyph/E", 7), ("glyph/R", 8),
)
HUD_SPAWN_CRAFT_PROCCODE = "hud spawn craft"

# ECO-01 scoring path (docs/spec/scoring-lives-and-game-over.md). Every award routes through
# one Stage `score` proc: add the pending award, pin at the 3-byte BCD ceiling, lift the
# running high score, then run the bonus-life check. The `score` variable is written ONLY
# inside this proc — that is the "single scoring path" guarantee (SYS-03 / ECO-01), enforced
# by _eco01_failures. The HUD reads score/high score; only the Stage writes them.
SCORE_ID = "eco-score"
HIGH_SCORE_ID = "eco-high-score"
# The resolved point value to add — a MACHINERY seam (parallel to `hit slot`): set by the
# collision detector the enemy slice (slice 8) wires, so it is not write-forbidden to sprites.
# The debug scoring fixture below sets it this slice so the economy is operator-verifiable.
AWARD_VALUE_ID = "eco-award-value"
SCORE_CAP = 9_999_990  # set_score_to_9999990: three BCD bytes, x10 implicit
HIGH_SCORE_START = 40_000  # top default best-five entry (high_score_defaults[0])
CHECK_BONUS_PROCCODE = "check bonus life"
# The 22 object point values in table order (docs/spec/data/scores.json master_value_table,
# BCD-decoded). INDEX CONVENTION (cross-slice seam, pinned in docs/mechanics/009): `value
# table` position i (1-based) holds entries[i-1].points; the enemy slice resolves an object's
# points via this list and sets `award value` to that points value. Ingested here, not authored.
VALUE_TABLE_ID = "eco-value-table"
VALUE_TABLE_POINTS = [
    10, 20, 30, 50, 70, 100, 150, 200, 250, 300, 400,
    500, 600, 700, 800, 900, 1000, 1500, 2000, 2500, 4000, 10000,
]
# Debug scoring fixture: while playing, pressing S sets the award-value seam to the top
# value-table entry (10,000) and runs the one `score` path — exactly as slice 8's collision
# detector will — so score, cap, high score, the bonus award, and the HUD digits are
# operator-verifiable before an enemy exists to award points. Holding S accelerates toward
# the cap. A stand-in producer of `award value`, removed with the D/G death fixtures when the
# real collision trigger lands (slice 8).
SCORE_FIXTURE_KEY = "s"

# ECO-03 lives and bonus economy (docs/spec/data/scores.json; docs/spec/scoring-lives-and-game-over.md).
# Starting craft come from a DIP-indexed table; bonus craft are granted as the score passes a
# threshold that then advances by a per-setting increment. A `null` threshold disables bonuses
# (BONUS_DISABLED sentinel — real thresholds are >= 10,000). Once the score is pinned at the cap,
# every further award grants a craft (the recorded arcade quirk). The runtime reads the live
# ingested tables at the fixed DIP index, so the committed data is the single source of truth.
LIVES_ID = "eco-craft"
NEXT_BONUS_ID = "eco-next-bonus"
STARTING_LIVES_ID = "eco-starting-lives"
FIRST_BONUS_123_ID = "eco-first-bonus-123"
FIRST_BONUS_5_ID = "eco-first-bonus-5"
REPEAT_BONUS_123_ID = "eco-repeat-bonus-123"
REPEAT_BONUS_5_ID = "eco-repeat-bonus-5"
BONUS_DISABLED = 0  # the `null`-threshold (bonuses off) sentinel; real thresholds are >= 10,000
# DIP defaults — a project choice, recorded with its uncertainty (docs/mechanics/011): the
# raw-index->physical-switch mapping is unrecorded upstream, and the 123-vs-5 table selection
# carries the reference's own recorded inconsistency (the build follows the repeat-award site).
# Starting item 4 of [5,2,1,3] -> 3 craft; bonus item 1 of the 1/2/3-lives tables -> first bonus
# 20,000 then every 60,000.
DIP_STARTING_ITEM = 4
DIP_BONUS_ITEM = 1
STARTING_LIVES = [5, 2, 1, 3]
# `null` (bonuses disabled at that setting) -> BONUS_DISABLED; the data-equality test maps it the
# same way. Both table pairs are ingested for the data-equality criterion and a future DIP config;
# the runtime uses the 1/2/3-lives pair at the default DIP.
FIRST_BONUS_123 = [20000, 10000, 10000, 20000, 20000, 20000, 20000, BONUS_DISABLED]
FIRST_BONUS_5 = [20000, 10000, 20000, 20000, 20000, 30000, 20000, BONUS_DISABLED]
REPEAT_BONUS_123 = [60000, 40000, 50000, 50000, 70000, 80000, 60000, BONUS_DISABLED]
REPEAT_BONUS_5 = [70000, 50000, 50000, 60000, 80000, 100000, 80000, BONUS_DISABLED]

# ECO-04 game over (docs/spec/scoring-lives-and-game-over.md `check_for_high_score` 1618-1672;
# docs/spec/data/scores.json high_score_defaults). Losing the last craft first runs the best-five
# check: `qualified` records whether the final score beats fifth place — a VERDICT ONLY. The
# initials-entry screen a qualifying score would show (cabinet-flow.md) is DEFERRED to slice 19;
# both a qualifying and a non-qualifying score still show GAME OVER and return to title here.
QUALIFIED_ID = "eco-qualified"
HIGH_SCORE_TABLE_ID = "eco-high-score-table"
HIGH_SCORE_DEFAULTS = [40_000, 35_000, 30_000, 25_000, 20_000]  # high_score_defaults.scores

# AREA-01 area scroll clock (docs/spec/area-progression-and-terrain.md, locked). One
# monotonic per-area position drives the terrain, the object scheduler, and the area loop.
# The reference runs a 16-bit scroll counter initialized to 0x0D00 and decreased by 16 per
# arcade frame; its high byte is the descending "scroll row" (0x0D..0x00, wrapping to 0xFF
# and continuing down), and the area completes when that row reaches 0x0E. We store the
# monotonic INCREASING `area progress` (0 up to ~0xFF00; completion actually fires at 65056,
# see AREA_COMPLETE_ROW) as the SOLE position authority — so within an area the position never
# rewinds, resetting to 0 only when the area completes and the area number advances — and DERIVE
# the arcade scroll row once per tick: row = floor(((0x0D00 - area progress) mod 0x10000) / 256).
# Cadence: 1 build tick = 2 arcade frames, so `area progress` advances 32 units per tick;
# 256 is divisible by 32, so every row is visited (no schedule trigger is skipped).
AREA_PROGRESS_ID = "area-progress"
AREA_NUMBER_ID = "area-number"
SCROLL_ROW_ID = "area-scroll-row"
# Dormant seam: the per-area terrain start column, set on area entry from the ingested
# offset table. No consumer this slice (the visual terrain stays decoupled); the
# presentation slice (20) couples the visual scroll to the clock and reads this.
TERRAIN_COLUMN_ID = "area-terrain-column"
AREA_MAP_COLUMN_ID = "area-map-column"
ADVANCE_AREA_PROCCODE = "advance area"
AREA_PROGRESS_STEP = 32  # 16 counter units/frame * 2 frames/tick
AREA_COUNTER_INIT = 0x0D00  # 3328; the reference scroll-counter start (row 0x0D)
AREA_COUNTER_WRAP = 0x10000  # 65536; the 16-bit counter wrap makes the row descent continuous
AREA_ROW_DIVISOR = 0x100  # 256; a scroll "row" is the counter's high byte
AREA_COMPLETE_ROW = 0x0E  # 14; the area completes at the first tick the derived row reaches this
AREA_TOP_ROW = 0x0D  # 13; the row at area top (progress 0), also each table's end sentinel
AREA_FIRST = 1
AREA_MAX = 16
AREA_LOOP_BACK = 7  # completing area 16 continues at area 7, not area 1 and not a win screen
# The near-end checkpoint (docs/mechanics/003, 013): a death with the frozen scroll row in
# [0x0E, 0x43] advances to the next area instead of restarting the current one. Checked as
# `row > 13 AND row < 68` (Scratch has no <=). The row-14 edge is a vacuous runtime state
# (completion resets the area before a death can be observed at row 14), but the boundary
# logic must still handle it; the reachable checkpoint floor at death is row 15.
AREA_CHECKPOINT_LOW_EXCL = 0x0D  # 13; the frozen row must be strictly greater (>= 0x0E)
AREA_CHECKPOINT_HIGH_EXCL = 0x44  # 68; the frozen row must be strictly less (<= 0x43)

SPEC_DATA_DIR = ROOT / "docs" / "spec" / "data"


def _load_spec_data(name: str, *, data_dir: Path = SPEC_DATA_DIR) -> Any:
    # Load a committed reference-data file, verifying its bytes against the pinned SHA-256 in
    # docs/spec/data/manifest.json BEFORE parsing — so a stale, hand-edited, or corrupted data
    # file fails the build LOUDLY at ingest (mirroring tools/hud_glyphs.py's asset-hash guard),
    # never silently baking into project.json. The manifest is the single source of the
    # sanctioned hashes; regenerating the data (tools/reference_extract.py) is the only way to
    # change them.
    raw = (data_dir / name).read_bytes()
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    expected = manifest["files"].get(name)
    if expected is None:
        raise SystemExit(f"{name} is not registered in docs/spec/data/manifest.json")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise SystemExit(
            f"docs/spec/data/{name} hash changed: expected {expected}, found {actual}; "
            f"regenerate the data with tools/reference_extract.py — never hand-edit it"
        )
    return json.loads(raw.decode("utf-8"))


def _load_terrain_columns() -> list[int]:
    # AREA-01: the 16 per-area terrain start columns, INGESTED (not authored) from the
    # committed, hash-pinned reference data (verified against docs/spec/data/manifest.json at
    # load). One transcription, by the generator — the Scratch list is a faithful copy of the
    # JSON, verified by the golden in tests/test_spec_docs.py.
    data = _load_spec_data("terrain.json")
    return list(data["area_offset_in_map_tbl"]["values"])


AREA_MAP_COLUMNS = _load_terrain_columns()

# AREA-02/AREA-03 area object scheduler (docs/spec/area-progression-and-terrain.md). Each area has one
# schedule table consumed strictly in order: a record fires when the scroll row equals its trigger row,
# then the cursor advances. All 16 normal areas are ingested into the SAME three flattened columns; the
# two 16-entry index lists (start/end) carry each area's 1-based INCLUSIVE span into those columns, so
# the runtime consume reads an area's slice by indexing the lists with the live `area number` — no code
# path is per-area (that is why slice 6 moves no runtime block). Every handler's variable `params`
# (slot/sprite_y, mask, row, count, formation_offset, path, ...) is carried faithfully as an opaque JSON
# PAYLOAD so no field is dropped and the schema never has to grow; the handlers themselves (spawn,
# formation, difficulty, boss) arrive with the enemy slices (8+), so the per-record dispatch is an empty
# seam.
SCHEDULE_HANDLER_ID = "area-schedule-handler"
SCHEDULE_TRIGGER_ROW_ID = "area-schedule-trigger-row"
SCHEDULE_PAYLOAD_ID = "area-schedule-payload"
AREA_SCHEDULE_START_ID = "area-schedule-start"
AREA_SCHEDULE_END_ID = "area-schedule-end"
SCHEDULE_CURSOR_ID = "area-schedule-cursor"
SCHEDULE_FIRED_ID = "area-schedule-fired"
SCHEDULE_SENTINEL_HANDLER = "sentinel"


def _load_area_schedule(area_number: int) -> tuple[list[str], list[int], list[str]]:
    # AREA-02: ingest one area's schedule from the committed, hash-pinned reference data as three
    # faithful parallel columns (handler, trigger row, opaque payload). The end sentinel (a scalar
    # in the JSON) is MATERIALIZED as the terminal row so the table is self-terminating and the
    # extractor's "every table decodes to its sentinel" invariant is reproduced. object_type +
    # params are serialized deterministically (sorted keys) into the payload; source_line is
    # provenance, not runtime data, and is deliberately not ingested. The round-trip golden in
    # tests/test_spec_docs.py proves nothing is dropped.
    data = _load_spec_data("area-schedules.json")
    area = next(a for a in data["areas"] if a["area"] == area_number)
    handlers: list[str] = []
    rows: list[int] = []
    payloads: list[str] = []
    for record in area["records"]:
        handlers.append(record["handler"])
        rows.append(record["scroll_row"])
        payloads.append(
            json.dumps(
                {"object_type": record["object_type"], "params": record["params"]},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    handlers.append(SCHEDULE_SENTINEL_HANDLER)
    rows.append(area["end_sentinel"])
    payloads.append("")
    return handlers, rows, payloads


def _load_all_area_schedules() -> tuple[
    list[str], list[int], list[str], list[int], list[int]
]:
    # AREA-03: flatten all 16 normal area schedules into three parallel columns, with two 16-entry
    # index lists giving each area's 1-based INCLUSIVE span [start..end] into those columns. Areas are
    # visited by explicit number (not JSON array order); an up-front check requires exactly areas
    # AREA_FIRST..AREA_MAX, once each, so a missing OR duplicated area fails LOUD with a clear message
    # (not a bare StopIteration, and not a silently-swallowed duplicate). Each area contributes its
    # records + one materialized sentinel, so its span length is len(records)+1; the spans are
    # contiguous and cover the whole flattened table (the end of area AREA_MAX equals len(handlers)). No
    # per-slice total is hardcoded — it falls out of the concatenation. The per-area round-trip golden in
    # tests/test_spec_docs.py re-derives these spans independently from the JSON record counts and
    # compares the flattened windows to the source records, so an offset off-by-one that leaked one area
    # into the next would fail there.
    defined = sorted(a["area"] for a in _load_spec_data("area-schedules.json")["areas"])
    if defined != list(range(AREA_FIRST, AREA_MAX + 1)):
        raise SystemExit(
            f"area-schedules.json must define exactly areas {AREA_FIRST}..{AREA_MAX}, "
            f"once each; found {defined}"
        )
    handlers: list[str] = []
    rows: list[int] = []
    payloads: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    cursor = 1  # 1-based, matching Scratch list indexing and the runtime `schedule cursor`
    for area_number in range(AREA_FIRST, AREA_MAX + 1):
        area_handlers, area_rows, area_payloads = _load_area_schedule(area_number)
        starts.append(cursor)  # this area's first index (before advancing the cursor)
        handlers.extend(area_handlers)
        rows.extend(area_rows)
        payloads.extend(area_payloads)
        cursor += len(area_rows)
        ends.append(cursor - 1)  # this area's last index (after advancing; inclusive)
    return handlers, rows, payloads, starts, ends


(
    SCHEDULE_HANDLERS,
    SCHEDULE_ROWS,
    SCHEDULE_PAYLOADS,
    AREA_SCHEDULE_START,
    AREA_SCHEDULE_END,
) = _load_all_area_schedules()

MESSAGES = {
    "director enter": "broadcastMsgId-director-enter",
    "director stop": "broadcastMsgId-director-stop",
    "director reset": "broadcastMsgId-director-reset",
    "ready complete": "broadcastMsgId-ready-complete",
    "death complete": "broadcastMsgId-death-complete",
    "game over complete": "broadcastMsgId-game-over-complete",
    "craft changed": "broadcastMsgId-craft-changed",
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

    def not_either_state(self, parent: str, left: str, right: str) -> str:
        block_id = self.add("operator_not")
        self.blocks[block_id]["parent"] = parent
        either = self.either_state(block_id, left, right)
        self.blocks[block_id]["inputs"] = {"OPERAND": [2, either]}
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

    def if_not_either_state(self, left: str, right: str, body: list[str]) -> str:
        block_id = self.add("control_if")
        condition = self.not_either_state(block_id, left, right)
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

    def go_expr(self, x: Any, y: Any) -> str:
        # Like go(), but X/Y accept a reporter (nested block id) or a value-input spec,
        # for a position computed at runtime (e.g. a clone-spawn index).
        block_id = self.add("motion_gotoxy")
        inputs: dict[str, Any] = {}
        for slot, spec in (("X", x), ("Y", y)):
            if isinstance(spec, str):
                inputs[slot] = [2, spec]
                self.blocks[spec]["parent"] = block_id
            else:
                inputs[slot] = spec
        self.blocks[block_id]["inputs"] = inputs
        return block_id

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

    def switch_costume_expr(self, reporter_id: str) -> str:
        # Like switch_costume(), but the costume NAME is computed at runtime (a reporter,
        # e.g. a joined "digit/<n>" string). The costume input is a MENU input, so the
        # reporter must OBSCURE a costume-menu shadow ([3, reporter, shadow]) — a bare
        # [2, reporter] leaves the menu input unread and the switch never happens.
        menu = self.add(
            "looks_costume", fields={"COSTUME": ["digit/0", None]}, shadow=True
        )
        block_id = self.add(
            "looks_switchcostumeto", inputs={"COSTUME": [3, reporter_id, menu]}
        )
        self.blocks[reporter_id]["parent"] = block_id
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

    def if_var_equals(
        self, name: str, variable_id: str, value: int, body: list[str]
    ) -> str:
        block_id = self.add("control_if")
        condition = self.var_equals(block_id, name, variable_id, value)
        self.blocks[block_id]["inputs"]["CONDITION"] = [2, condition]
        self.substack(block_id, body)
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
        # scratch-vm reads arithmetic operands from NUM1/NUM2 but comparison/boolean
        # operands from OPERAND1/OPERAND2 (see scratch3_operators.js). Attaching to the
        # wrong pair leaves the reporter's inputs unread, so it silently evaluates to
        # NaN at runtime — invisible to structural tests but fatal to the digit HUD/RNG.
        if opcode in (
            "operator_add",
            "operator_subtract",
            "operator_multiply",
            "operator_divide",
            "operator_mod",
        ):
            slot1, slot2 = "NUM1", "NUM2"
        else:
            slot1, slot2 = "OPERAND1", "OPERAND2"
        inputs: dict[str, Any] = {}
        for slot, spec in ((slot1, operand1), (slot2, operand2)):
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
        # scratch-vm registers multiply as `operator_multiply`; `operator_mult` is an
        # unknown opcode the runtime resolves to nothing (returns undefined).
        return self._reporter("operator_multiply", a, b)

    def op_add(self, a: Any, b: Any) -> str:
        return self._reporter("operator_add", a, b)

    def op_sub(self, a: Any, b: Any) -> str:
        return self._reporter("operator_subtract", a, b)

    def op_div(self, a: Any, b: Any) -> str:
        return self._reporter("operator_divide", a, b)

    def op_eq(self, a: Any, b: Any) -> str:
        return self._reporter("operator_equals", a, b)

    def op_gt(self, a: Any, b: Any) -> str:
        return self._reporter("operator_gt", a, b)

    def op_join(self, a: Any, b: Any) -> str:
        block_id = self.add("operator_join")
        inputs: dict[str, Any] = {}
        for slot, spec in (("STRING1", a), ("STRING2", b)):
            if isinstance(spec, str):
                inputs[slot] = [2, spec]
                self.blocks[spec]["parent"] = block_id
            else:
                inputs[slot] = spec
        self.blocks[block_id]["inputs"] = inputs
        return block_id

    def op_floor(self, operand: Any) -> str:
        # scratch-vm's mathop reads its function from the OPERATOR field (not OPERATION);
        # a wrong key leaves the operator unset and mathop returns 0 for every input.
        block_id = self.add("operator_mathop", fields={"OPERATOR": ["floor", None]})
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


def _advance_area_number(blocks: Blocks) -> str:
    # AREA-01 area increment with the 16 -> 7 loop (completing area 16 continues at area 7).
    # One source, called from both the completion branch and the near-end checkpoint. Returns
    # the single control block id.
    branch = blocks.add("control_if_else")
    is_last = blocks.var_equals(branch, "area number", AREA_NUMBER_ID, AREA_MAX)
    blocks.blocks[branch]["inputs"]["CONDITION"] = [2, is_last]
    blocks.substack(branch, [blocks.set_var("area number", AREA_NUMBER_ID, number(AREA_LOOP_BACK))])
    blocks.substack(
        branch, [blocks.change_var("area number", AREA_NUMBER_ID, 1)], name="SUBSTACK2"
    )
    return branch


def _set_scroll_row(blocks: Blocks) -> str:
    # scroll row = floor(((AREA_COUNTER_INIT - area progress) mod AREA_COUNTER_WRAP) / 256),
    # built through the centralized operator helpers (never inline operator blocks — wrong
    # slot keys there are invisible to structural tests and silently evaluate to NaN).
    delta = blocks.op_sub(number(AREA_COUNTER_INIT), variable("area progress", AREA_PROGRESS_ID))
    wrapped = blocks.op_mod(delta, number(AREA_COUNTER_WRAP))
    divided = blocks.op_div(wrapped, number(AREA_ROW_DIVISOR))
    floored = blocks.op_floor(divided)
    return blocks.set_var_expr("scroll row", SCROLL_ROW_ID, floored)


def _enter_area_top(blocks: Blocks) -> list[str]:
    # The state every area entry establishes (fresh game, new life, area completion): progress at
    # the top, the derived row snapped to the area-top row, the per-area terrain start column, and
    # (AREA-02) the schedule cursor pointed at the area's first record with the per-area fired
    # counter zeroed — so every entry point re-tops the schedule consistently.
    return [
        blocks.set_var("area progress", AREA_PROGRESS_ID, number(0)),
        blocks.set_var("scroll row", SCROLL_ROW_ID, number(AREA_TOP_ROW)),
        blocks.set_var_expr(
            "terrain column",
            TERRAIN_COLUMN_ID,
            blocks.list_item(
                "area map column", AREA_MAP_COLUMN_ID, variable("area number", AREA_NUMBER_ID)
            ),
        ),
        blocks.set_var_expr(
            "schedule cursor",
            SCHEDULE_CURSOR_ID,
            blocks.list_item(
                "area schedule start", AREA_SCHEDULE_START_ID, variable("area number", AREA_NUMBER_ID)
            ),
        ),
        blocks.set_var("schedule fired", SCHEDULE_FIRED_ID, number(0)),
    ]


def _consume_schedule(blocks: Blocks) -> list[str]:
    # AREA-02 ordered dispatch: consume every record at the cursor whose trigger row equals the
    # current scroll row, in order, advancing the cursor. Fire-once is guaranteed by the monotonic
    # cursor over monotonic progress. The loop stops when the record's trigger no longer matches the
    # row OR the cursor passes the area's end index (`cursor > end`, the belt-and-suspenders bound
    # slice 6 relies on so one area never bleeds into the next). The sentinel never fires because
    # the dispatch reads the POST-increment row, which is <= 12 until the wrap and never the area-top
    # row 0x0D. The per-record handler dispatch is an EMPTY seam this slice (like advance-slots);
    # `schedule fired` is the observable that events fire once, in order.
    loop = blocks.add("control_repeat_until")

    def cursor() -> list[Any]:
        return variable("schedule cursor", SCHEDULE_CURSOR_ID)

    end = blocks.list_item(
        "area schedule end", AREA_SCHEDULE_END_ID, variable("area number", AREA_NUMBER_ID)
    )
    past_end = blocks.op_gt(cursor(), end)
    trigger = blocks.list_item("schedule trigger row", SCHEDULE_TRIGGER_ROW_ID, cursor())
    row_matches = blocks.op_eq(trigger, variable("scroll row", SCROLL_ROW_ID))
    row_differs = blocks.add("operator_not")
    blocks.blocks[row_matches]["parent"] = row_differs
    blocks.blocks[row_differs]["inputs"] = {"OPERAND": [2, row_matches]}
    stop = blocks.add("operator_or")
    blocks.blocks[past_end]["parent"] = stop
    blocks.blocks[row_differs]["parent"] = stop
    blocks.blocks[stop]["inputs"] = {"OPERAND1": [2, past_end], "OPERAND2": [2, row_differs]}
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, stop]
    # ENGINE-TODO: the per-record handler dispatch (spawn / formation / difficulty / boss, keyed on
    # `schedule handler` + `schedule payload`) lands with the enemy slices (8+); the consume today
    # advances the cursor and counts the fire, with no per-handler behaviour.
    blocks.substack(
        loop,
        [
            blocks.change_var("schedule fired", SCHEDULE_FIRED_ID, 1),
            blocks.change_var("schedule cursor", SCHEDULE_CURSOR_ID, 1),
        ],
    )
    return [loop]


def install_advance_area(blocks: Blocks) -> None:
    # AREA-01/AREA-02 area clock + scheduler: one atomic (warp) pass per tick, called from the walk
    # thread BEFORE `advance slots` — matching the reference frame order (handle_next_area ->
    # handle_objects -> object updates) and fixing the PHASE order the enemy slices inherit while
    # both dispatch bodies are still empty (no RNG is drawn in either phase yet). Advances the
    # monotonic position and derives the row once; then a single `if/else` either completes the area
    # (advance 16 -> 7 and re-top) OR consumes the schedule for this row — never both on one tick.
    definition = _install_warp_proc(blocks, ADVANCE_AREA_PROCCODE)
    step = blocks.change_var("area progress", AREA_PROGRESS_ID, AREA_PROGRESS_STEP)
    set_row = _set_scroll_row(blocks)
    completion = blocks.add("control_if_else")
    complete = blocks.var_equals(completion, "scroll row", SCROLL_ROW_ID, AREA_COMPLETE_ROW)
    blocks.blocks[completion]["inputs"]["CONDITION"] = [2, complete]
    blocks.substack(completion, [_advance_area_number(blocks), *_enter_area_top(blocks)])
    blocks.substack(completion, _consume_schedule(blocks), name="SUBSTACK2")
    blocks.chain(definition, [step, set_row, completion])


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
    # ECO-01: the single scoring path everything routes through, so scoring can never
    # double-count or bypass the cap. Add the pending award to the score, pin it at the
    # 9,999,990 BCD ceiling (set_score_to_9999990), lift the running high score, then run the
    # bonus-life check after every award (check_for_extra_solvalou). `award value` is the
    # resolved point value, set by the collision detector a later slice wires (machinery seam,
    # parallel to `hit slot`); the debug S fixture sets it this slice.
    definition = _install_warp_proc(blocks, SCORE_PROCCODE)
    # NOTE: `set score = op_add(score, award value)` does NOT evaluate in the Scratch VM
    # (a `set var = operator(...)` value-input the runtime leaves unread); `change ... by` does.
    add_award = blocks.add(
        "data_changevariableby",
        inputs={"VALUE": variable("award value", AWARD_VALUE_ID)},
        fields={"VARIABLE": ["score", SCORE_ID]},
    )
    cap_if = blocks.add("control_if")
    cap_cond = blocks.greater(cap_if, "score", SCORE_ID, SCORE_CAP)
    blocks.blocks[cap_if]["inputs"]["CONDITION"] = [2, cap_cond]
    blocks.substack(cap_if, [blocks.set_var("score", SCORE_ID, number(SCORE_CAP))])
    high_if = blocks.add("control_if")
    high_cond = blocks.add(
        "operator_gt",
        inputs={
            "OPERAND1": variable("score", SCORE_ID),
            "OPERAND2": variable("high score", HIGH_SCORE_ID),
        },
    )
    blocks.blocks[high_cond]["parent"] = high_if
    blocks.blocks[high_if]["inputs"]["CONDITION"] = [2, high_cond]
    blocks.substack(
        high_if, [blocks.set_var("high score", HIGH_SCORE_ID, variable("score", SCORE_ID))]
    )
    blocks.chain(
        definition,
        [add_award, cap_if, high_if, blocks.call_proc(CHECK_BONUS_PROCCODE, warp=True)],
    )


def install_check_bonus_life(blocks: Blocks) -> None:
    # ECO-03: grant a bonus craft as the score passes the current threshold, then advance the
    # threshold by the per-setting increment. A disabled setting (BONUS_DISABLED sentinel) never
    # grants. Once the score is pinned at the cap, every award grants a craft (the recorded arcade
    # quirk: the threshold can no longer exceed the score). Called by `score` after every award.
    definition = _install_warp_proc(blocks, CHECK_BONUS_PROCCODE)

    def grant() -> list[str]:
        return [
            blocks.change_var("craft", LIVES_ID, 1),
            blocks.play_sound("extend"),
            blocks.send("craft changed"),
        ]

    # score >= next bonus, as `not (score < next bonus)` (thresholds are exact 10,000 multiples).
    below = blocks.add(
        "operator_lt",
        inputs={
            "OPERAND1": variable("score", SCORE_ID),
            "OPERAND2": variable("next bonus", NEXT_BONUS_ID),
        },
    )
    at_or_past = blocks.add("operator_not", inputs={"OPERAND": [2, below]})
    blocks.blocks[below]["parent"] = at_or_past
    advance = blocks.set_var_expr(
        "next bonus",
        NEXT_BONUS_ID,
        blocks.op_add(
            variable("next bonus", NEXT_BONUS_ID),
            blocks.list_item("repeat bonus 123", REPEAT_BONUS_123_ID, number(DIP_BONUS_ITEM)),
        ),
    )
    normal_if = blocks.if_reporter(at_or_past, grant() + [advance])

    # cap quirk vs the ordinary threshold: at the pinned cap, grant every award.
    quirk = blocks.add("control_if_else")
    at_cap = blocks.var_equals(quirk, "score", SCORE_ID, SCORE_CAP)
    blocks.blocks[quirk]["inputs"]["CONDITION"] = [2, at_cap]
    blocks.substack(quirk, grant())
    blocks.substack(quirk, [normal_if], name="SUBSTACK2")

    # the whole check only runs when bonuses are enabled (threshold sentinel is non-zero).
    enabled_if = blocks.add("control_if")
    enabled = blocks.greater(enabled_if, "next bonus", NEXT_BONUS_ID, BONUS_DISABLED)
    blocks.blocks[enabled_if]["inputs"]["CONDITION"] = [2, enabled]
    blocks.substack(enabled_if, [quirk])
    blocks.chain(definition, [enabled_if])


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
    install_advance_area(blocks)
    install_score(blocks)
    install_check_bonus_life(blocks)
    install_resolve_hit(blocks)
    install_alloc_bullet_slot(blocks)

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

    # Death triggers — stand-ins until a real attacker exists (slice 8), now driving the real
    # life economy instead of a hardcoded outcome. D takes one hit (lose a craft); G drains to
    # the terminal life so the game-over path is reachable in one press. The death-complete
    # handler decides respawn-vs-game-over from the craft counter, not from which key was pressed.
    d_hat = blocks.key("d")
    blocks.chain(
        d_hat,
        [
            blocks.if_state(
                "playing",
                [
                    blocks.change_var("craft", LIVES_ID, -1),
                    blocks.send("craft changed"),
                    blocks.call_transition("player-dead", "none"),
                ],
            )
        ],
    )
    g_hat = blocks.key("g")
    blocks.chain(
        g_hat,
        [
            blocks.if_state(
                "playing",
                [
                    blocks.set_var("craft", LIVES_ID, number(0)),
                    blocks.send("craft changed"),
                    blocks.call_transition("player-dead", "none"),
                ],
            )
        ],
    )

    # Debug scoring fixture (S): set the award-value seam to the top value-table entry and run
    # the one `score` path, so the economy is operator-verifiable before an enemy awards points.
    # Removed with the D/G fixtures when the real collision trigger lands (slice 8).
    score_key = blocks.key(SCORE_FIXTURE_KEY)
    set_award = blocks.set_var_expr(
        "award value",
        AWARD_VALUE_ID,
        blocks.list_item("value table", VALUE_TABLE_ID, number(len(VALUE_TABLE_POINTS))),
    )
    blocks.chain(
        score_key,
        [blocks.if_state("playing", [set_award, blocks.call_proc(SCORE_PROCCODE, warp=True)])],
    )

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
    # Decide from the craft counter (PLY-02): a craft left means respawn; none left means game
    # over. `death outcome` now RECORDS the decision (kept, not removed, so the transition-cleanup
    # opcode sequence and the reset-scope matrix stay byte-identical) — it is no longer the input.
    decide = blocks.add("control_if_else")
    has_craft = blocks.greater(decide, "craft", LIVES_ID, 0)
    blocks.blocks[decide]["inputs"]["CONDITION"] = [2, has_craft]
    blocks.substack(
        decide,
        [
            blocks.set_var("death outcome", OUTCOME_ID, text("respawn")),
            blocks.call_transition("respawning", "new-life"),
        ],
    )
    blocks.substack(
        decide,
        [
            blocks.set_var("death outcome", OUTCOME_ID, text("game-over")),
            blocks.call_transition("game-over", "game-over"),
        ],
        name="SUBSTACK2",
    )
    blocks.chain(death, [blocks.if_state("player-dead", [decide])])

    game_over = blocks.receive("game over complete")
    # ECO-04 best-five check: qualified = the final score beats fifth place in the ingested
    # high-score table. A verdict only (the initials-entry screen a qualifying score would show
    # is deferred to the cabinet-flow slice, 19) — computed here, before the transition back to
    # title resets `reset scope` and (via the cold-start scope) the score itself.
    set_qualified = blocks.set_var_expr(
        "qualified",
        QUALIFIED_ID,
        blocks.op_gt(
            variable("score", SCORE_ID),
            blocks.list_item("high score table", HIGH_SCORE_TABLE_ID, number(5)),
        ),
    )
    blocks.chain(
        game_over,
        [
            blocks.if_state(
                "game-over",
                [set_qualified, blocks.call_transition("title", "cold-start")],
            )
        ],
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

    # SYS-04 / AREA-01 centralized ordered update: a second `director enter` thread (parallel
    # to the BGM loop above) drives one atomic pass per tick while playing. `advance area`
    # runs BEFORE `advance slots` (the reference's area/schedule phase precedes the object
    # walk), fixing the phase order the enemy slices inherit while both dispatch bodies are
    # still empty.
    walk_enter = blocks.receive("director enter")
    walk_loop = blocks.add("control_repeat_until")
    walk_condition = blocks.not_state(walk_loop, "playing")
    blocks.blocks[walk_loop]["inputs"]["CONDITION"] = [2, walk_condition]
    blocks.substack(
        walk_loop,
        [
            blocks.call_proc(ADVANCE_AREA_PROCCODE, warp=True),
            blocks.call_proc(ADVANCE_SLOTS_PROCCODE, warp=True),
        ],
    )
    blocks.chain(walk_enter, [blocks.if_state("playing", [walk_loop])])

    # A NEW Stage `director reset` receiver — kept out of the transition procedure body
    # so its pinned opcode sequence stays byte-identical. `reset scope` is already set
    # before the reset broadcast fires. It (SYS-02) clears the object slots on every
    # reset — all reset scopes clear transient gameplay — and, on a world reset
    # (cold-start / new-game), seeds the shared stream (SYS-04, so seeded runs repeat)
    # and starts the frame clock at zero.
    stage_reset = blocks.receive("director reset")
    # High score is the RUNNING best: it persists across a new game and is restored to the
    # default top entry only at cold start (power-on). Score restarts every new game.
    high_reset = blocks.add("control_if")
    high_scope = blocks.scope_is(high_reset, "cold-start")
    blocks.blocks[high_reset]["inputs"]["CONDITION"] = [2, high_scope]
    blocks.substack(
        high_reset,
        [blocks.set_var("high score", HIGH_SCORE_ID, number(HIGH_SCORE_START))],
    )
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
                    blocks.set_var("score", SCORE_ID, number(0)),
                    # ECO-04: the best-five verdict is only meaningful for the game just ended.
                    blocks.set_var("qualified", QUALIFIED_ID, number(0)),
                    # ECO-03: starting craft and the first bonus threshold, read live from the
                    # ingested DIP tables (the committed data is the one source of truth).
                    blocks.set_var_expr(
                        "craft",
                        LIVES_ID,
                        blocks.list_item(
                            "starting lives", STARTING_LIVES_ID, number(DIP_STARTING_ITEM)
                        ),
                    ),
                    blocks.set_var_expr(
                        "next bonus",
                        NEXT_BONUS_ID,
                        blocks.list_item(
                            "first bonus 123", FIRST_BONUS_123_ID, number(DIP_BONUS_ITEM)
                        ),
                    ),
                ],
            ),
            high_reset,
        ],
    )

    # AREA-01 area-state reset — a SEPARATE `director reset` receiver, kept off stage_reset's
    # pinned opcode chain (like the eight existing reset receivers, each branching on its own
    # scope for its own concern). It touches only the area vars, so the unordered same-target
    # hat execution is safe. A world reset (cold-start / new-game) returns to area 1 and re-tops;
    # a new life runs the NEAR-END CHECKPOINT: a death with the frozen scroll row in [0x0E, 0x43]
    # advances to the next area instead of restarting (discharging docs/mechanics/003, 013),
    # then re-tops. On a scope-`none` transition (e.g. the death itself) and on game-over this
    # receiver does nothing, so `area progress`/`scroll row` stay frozen through the death
    # sequence and the checkpoint reads the real death-tick row.
    area_reset = blocks.receive("director reset")
    world_area = reset_if(
        blocks,
        ("cold-start", "new-game"),
        [blocks.set_var("area number", AREA_NUMBER_ID, number(AREA_FIRST)), *_enter_area_top(blocks)],
    )
    new_life = blocks.add("control_if")
    new_life_scope = blocks.scope_is(new_life, "new-life")
    blocks.blocks[new_life]["inputs"]["CONDITION"] = [2, new_life_scope]
    near_end = blocks.add("operator_and")
    low = blocks.greater(near_end, "scroll row", SCROLL_ROW_ID, AREA_CHECKPOINT_LOW_EXCL)
    high = blocks.op_gt(number(AREA_CHECKPOINT_HIGH_EXCL), variable("scroll row", SCROLL_ROW_ID))
    blocks.blocks[high]["parent"] = near_end
    blocks.blocks[near_end]["inputs"] = {"OPERAND1": [2, low], "OPERAND2": [2, high]}
    checkpoint = blocks.if_reporter(near_end, [_advance_area_number(blocks)])
    blocks.substack(new_life, [checkpoint, *_enter_area_top(blocks)])
    blocks.chain(area_reset, [world_area, new_life])

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
    # A2: the invented GAME OVER speech bubble is removed; the text is the HUD's glyph-costume
    # "GAME OVER" (ECO-04, hud_blocks), gated on `game state` == game-over.
    # ECO-04: the 128-frame (64-tick) GAME OVER hold, epoch-guarded exactly like the death
    # explosion above — the hold itself runs unconditionally, but the epoch check right before
    # the broadcast means a superseding transition (which bumps the epoch) cancels a stale hold,
    # so `game over complete` is never sent outside the guard.
    over = blocks.if_state(
        "game-over",
        [
            blocks.show(),
            blocks.hold_ticks(GAME_OVER_HOLD_TICKS),
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
    rewind = [
        switch,
        blocks.go(0, start_y),
        blocks.set_var("scroll step", step_id, number(initial_step)),
        blocks.send_backward(),  # B9: terrain sits behind the sprites
        blocks.show(),
    ]
    # Rewind to the strip's top on cold-start, new-game, AND new-life: a new life now restarts
    # the current area from its top, the arcade rule the locked area-progression spec makes
    # normative — retiring the interim B11 preserve-terrain-on-death fixture (audit 2026-08-09).
    # The visual strip stays DECOUPLED from the area clock this slice (only area-1 art exists);
    # the near-end checkpoint lives in the Stage `area_reset` receiver, where it advances the
    # AREA NUMBER on a near-end death. Coupling this visual scroll to the clock is the
    # presentation slice's (20) work.
    reset_control = blocks.add("control_if")
    tail = blocks.add("operator_or")
    ng = blocks.scope_is(tail, "new-game")
    nl = blocks.scope_is(tail, "new-life")
    blocks.blocks[tail]["inputs"] = {"OPERAND1": [2, ng], "OPERAND2": [2, nl]}
    condition = blocks.add("operator_or")
    blocks.blocks[tail]["parent"] = condition
    cs = blocks.scope_is(condition, "cold-start")
    blocks.blocks[condition]["inputs"] = {"OPERAND1": [2, cs], "OPERAND2": [2, tail]}
    blocks.blocks[condition]["parent"] = reset_control
    blocks.blocks[reset_control]["inputs"]["CONDITION"] = [2, condition]
    blocks.substack(reset_control, rewind)
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


def install_alloc_bullet_slot(blocks: Blocks) -> None:
    # Allocate the first idle enemy-bullet slot (40-58): `bullet alloc result` becomes that
    # index, or stays 0 when all 19 are live — the 19-bullet cap. Warp (atomic); a cursor
    # sweep over the dedicated slots (19 of them, vs the shot allocator's 3). DORMANT this
    # slice: no firer calls it (aimed-vector firing is AIR-12, slice 8). Its result var is
    # its own, and so is its cursor (`bullet-cursor`, not the shared `slot index`) — so a
    # firer can call this from inside the `advance slots` sweep without corrupting it.
    definition = _install_warp_proc(blocks, ALLOC_BULLET_PROCCODE)
    cursor = lambda: variable("bullet cursor", BULLET_CURSOR_ID)
    reset_result = blocks.set_var(
        "bullet alloc result", BULLET_ALLOC_RESULT_ID, number(0)
    )
    reset_cursor = blocks.set_var("bullet cursor", BULLET_CURSOR_ID, number(BULLET_SLOTS[0]))
    loop = blocks.add(
        "control_repeat",
        inputs={"TIMES": number(BULLET_SLOTS[1] - BULLET_SLOTS[0] + 1)},
    )
    unallocated = blocks.op_eq(
        variable("bullet alloc result", BULLET_ALLOC_RESULT_ID), number(0)
    )
    slot_free = blocks.op_eq(
        blocks.list_item("slot type", SLOT_TYPE_ID, cursor()), number(0)
    )
    condition = blocks.add("operator_and")
    blocks.blocks[condition]["inputs"] = {
        "OPERAND1": [2, unallocated],
        "OPERAND2": [2, slot_free],
    }
    blocks.blocks[unallocated]["parent"] = condition
    blocks.blocks[slot_free]["parent"] = condition
    allocate = blocks.if_reporter(
        condition,
        [
            blocks.list_replace(
                "slot type", SLOT_TYPE_ID, cursor(), number(BULLET_TYPE)
            ),
            blocks.list_replace(
                "slot state", SLOT_STATE_ID, cursor(), number(SLOT_ACTIVE)
            ),
            blocks.set_var(
                "bullet alloc result", BULLET_ALLOC_RESULT_ID, cursor()
            ),
        ],
    )
    advance = blocks.change_var("bullet cursor", BULLET_CURSOR_ID, 1)
    blocks.substack(loop, [allocate, advance])
    blocks.chain(definition, [reset_result, reset_cursor, loop])


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


def install_hud_spawn_craft(blocks: Blocks) -> None:
    # ECO-02: (re)spawn one life/ship clone per remaining craft, left to right, capped at
    # HUD_LIFE_MAX rendered icons so the row can never run off-stage (usability fix; the
    # true `craft` count is UNAFFECTED — only the icon DISPLAY is bounded, via a hud-local
    # counter capped before the spawn loop reads it). A warp (atomic) block so the whole
    # row appears in a single frame; called both by the initial director-enter spawn and
    # again whenever `craft changed` fires (a bonus grant now, a death later), so the row
    # always reflects the live `craft` count (up to the cap).
    definition = _install_warp_proc(blocks, HUD_SPAWN_CRAFT_PROCCODE)
    set_role = blocks.set_var("hud role", HUD_ROLE_ID, number(HUD_ROLE_LIFE))
    set_index = blocks.set_var("hud life index", HUD_LIFE_INDEX_ID, number(0))
    set_count = blocks.set_var(
        "hud life count", HUD_LIFE_COUNT_ID, variable("craft", LIVES_ID)
    )
    cap_if = blocks.add("control_if")
    cap_cond = blocks.greater(cap_if, "hud life count", HUD_LIFE_COUNT_ID, HUD_LIFE_MAX)
    blocks.blocks[cap_if]["inputs"]["CONDITION"] = [2, cap_cond]
    blocks.substack(
        cap_if,
        [blocks.set_var("hud life count", HUD_LIFE_COUNT_ID, number(HUD_LIFE_MAX))],
    )
    loop = blocks.add(
        "control_repeat", inputs={"TIMES": variable("hud life count", HUD_LIFE_COUNT_ID)}
    )
    x_expr = blocks.op_add(
        number(HUD_LIFE_LEFT_X),
        blocks.op_mul(
            variable("hud life index", HUD_LIFE_INDEX_ID), number(HUD_LIFE_SPACING)
        ),
    )
    go = blocks.go_expr(x_expr, number(HUD_LIFE_Y))
    create = blocks.create_clone()
    advance = blocks.change_var("hud life index", HUD_LIFE_INDEX_ID, 1)
    blocks.substack(loop, [go, create, advance])
    blocks.chain(definition, [set_role, set_index, set_count, cap_if, loop])


def hud_blocks() -> dict[str, dict[str, Any]]:
    # ECO-02 HUD render (docs/mechanics/012). The original hud sprite stays hidden and
    # only ever spawns clones (three kinds, tagged by a snapshotted `hud role`): 7 score
    # digits, 7 high-score digits, and a craft-sized row of life icons, plus two glyph
    # labels ("1UP" flashing, "HIGH SCORE" static). Clones are cleared on every
    # `director stop` (common_stop's clones=True) and rebuilt on `director enter`
    # whenever the state is HUD-visible (anything but title/boot) — director stop always
    # precedes director enter on every transition, so nothing ever double-stacks.
    blocks = Blocks("hud")
    common_stop(blocks, hide=True, clones=True)
    install_hud_spawn_craft(blocks)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for place in range(HUD_DIGIT_PLACES):
        x = HUD_SCORE_LEFT_X + (HUD_DIGIT_PLACES - 1 - place) * HUD_DIGIT_SPACING
        spawn_body += [
            blocks.set_var("hud role", HUD_ROLE_ID, number(HUD_ROLE_SCORE_DIGIT)),
            blocks.set_var("hud place", HUD_PLACE_ID, number(place)),
            blocks.go(x, HUD_SCORE_Y),
            blocks.create_clone(),
        ]
    for place in range(HUD_DIGIT_PLACES):
        x = HUD_HIGH_SCORE_LEFT_X + (HUD_DIGIT_PLACES - 1 - place) * HUD_DIGIT_SPACING
        spawn_body += [
            blocks.set_var("hud role", HUD_ROLE_ID, number(HUD_ROLE_HIGH_SCORE_DIGIT)),
            blocks.set_var("hud place", HUD_PLACE_ID, number(place)),
            blocks.go(x, HUD_HIGH_SCORE_Y),
            blocks.create_clone(),
        ]
    for glyph, slot in HUD_1UP_LABEL:
        x = HUD_1UP_LEFT_X + slot * HUD_DIGIT_SPACING
        spawn_body += [
            blocks.set_var("hud role", HUD_ROLE_ID, number(HUD_ROLE_LABEL_1UP)),
            blocks.switch_costume(glyph),
            blocks.go(x, HUD_LABEL_Y),
            blocks.create_clone(),
        ]
    for glyph, slot in HUD_HIGH_SCORE_LABEL:
        x = HUD_HIGH_SCORE_LABEL_LEFT_X + slot * HUD_DIGIT_SPACING
        spawn_body += [
            blocks.set_var("hud role", HUD_ROLE_ID, number(HUD_ROLE_LABEL_HIGH_SCORE)),
            blocks.switch_costume(glyph),
            blocks.go(x, HUD_LABEL_Y),
            blocks.create_clone(),
        ]
    # ECO-04: "GAME OVER", spawned only while `game state` is game-over (nested inside the
    # broader not-title/not-boot gate above, which already covers this state).
    game_over_body: list[str] = []
    for glyph, slot in HUD_GAME_OVER_LABEL:
        x = HUD_GAME_OVER_LEFT_X + slot * HUD_GAME_OVER_SPACING
        game_over_body += [
            blocks.set_var("hud role", HUD_ROLE_ID, number(HUD_ROLE_GAME_OVER_GLYPH)),
            blocks.switch_costume(glyph),
            blocks.go(x, HUD_GAME_OVER_Y),
            blocks.create_clone(),
        ]
    spawn_body.append(blocks.if_state("game-over", game_over_body))
    # The life-icon row is spawned by the shared proc below (also used on `craft changed`).
    spawn_body.append(blocks.call_proc(HUD_SPAWN_CRAFT_PROCCODE, warp=True))
    gate = blocks.if_not_either_state("title", "boot", spawn_body)
    blocks.chain(enter, [gate])

    # Each clone snapshots its role (and, for digit clones, its place) at creation — the
    # blaster clone-slot idiom — then dispatches on that role. `hud is clone` is marked
    # here unconditionally, on every clone, so the original (which never runs this hat)
    # stays the only instance where it reads 0 — the craft-changed handler below's "am I
    # the original" gate.
    clone = blocks.add("control_start_as_clone", top_level=True)
    mark_clone = blocks.set_var("hud is clone", HUD_IS_CLONE_ID, number(1))

    def digit_role_body(var_name: str, var_id: str) -> list[str]:
        # 10^place, computed once at clone start (place never changes for this clone).
        set_divisor = blocks.set_var("hud divisor", HUD_DIVISOR_ID, number(1))
        divisor_loop = blocks.add(
            "control_repeat", inputs={"TIMES": variable("hud place", HUD_PLACE_ID)}
        )
        blocks.substack(
            divisor_loop,
            [
                blocks.set_var_expr(
                    "hud divisor",
                    HUD_DIVISOR_ID,
                    blocks.op_mul(variable("hud divisor", HUD_DIVISOR_ID), number(10)),
                )
            ],
        )
        # Every tick while HUD-visible: digit = floor(value / 10^place) mod 10, shown as
        # leading-zero-preserving digit/D (deterministic integer math, arcade-faithful).
        # Update every tick while the HUD is visible; stop (fall through to hide+delete) only
        # when the state returns to title/boot. `repeat until` halts when its condition is TRUE,
        # so the condition is "we have LEFT to title/boot" — not its negation.
        tick_loop = blocks.add("control_repeat_until")
        tick_condition = blocks.either_state(tick_loop, "title", "boot")
        blocks.blocks[tick_loop]["inputs"]["CONDITION"] = [2, tick_condition]
        digit_expr = blocks.op_mod(
            blocks.op_floor(
                blocks.op_div(
                    variable(var_name, var_id), variable("hud divisor", HUD_DIVISOR_ID)
                )
            ),
            number(10),
        )
        name_expr = blocks.op_join(text("digit/"), digit_expr)
        blocks.substack(tick_loop, [blocks.switch_costume_expr(name_expr)])
        return [
            # Compute 10^place while still hidden, then show and update the costume every tick
            # (the first iteration sets the right digit before the frame renders — no flash).
            set_divisor,
            divisor_loop,
            blocks.to_front(),
            blocks.show(),
            tick_loop,
            blocks.hide(),
            blocks.add("control_delete_this_clone"),
        ]

    score_role = blocks.if_var_equals(
        "hud role", HUD_ROLE_ID, HUD_ROLE_SCORE_DIGIT, digit_role_body("score", SCORE_ID)
    )
    high_role = blocks.if_var_equals(
        "hud role",
        HUD_ROLE_ID,
        HUD_ROLE_HIGH_SCORE_DIGIT,
        digit_role_body("high score", HIGH_SCORE_ID),
    )
    # A life clone must SHOW the ship icon — switch to it explicitly rather than inherit
    # whatever costume the sprite last held at spawn (which is a label glyph).
    life_role = blocks.if_var_equals(
        "hud role",
        HUD_ROLE_ID,
        HUD_ROLE_LIFE,
        [blocks.switch_costume("life/ship"), blocks.to_front(), blocks.show()],
    )
    # 1UP: flashes (show/hide, held HUD_1UP_FLASH_HOLD_TICKS each way) for as long as the
    # HUD is visible, epoch/state-safe via the same title/boot guard as the digit loops.
    flash_loop = blocks.add("control_repeat_until")
    flash_condition = blocks.either_state(flash_loop, "title", "boot")
    blocks.blocks[flash_loop]["inputs"]["CONDITION"] = [2, flash_condition]
    blocks.substack(
        flash_loop,
        [
            blocks.hold_ticks(HUD_1UP_FLASH_HOLD_TICKS),
            blocks.hide(),
            blocks.hold_ticks(HUD_1UP_FLASH_HOLD_TICKS),
            blocks.show(),
        ],
    )
    label_1up_role = blocks.if_var_equals(
        "hud role",
        HUD_ROLE_ID,
        HUD_ROLE_LABEL_1UP,
        [
            blocks.to_front(),
            blocks.show(),
            flash_loop,
            blocks.hide(),
            blocks.add("control_delete_this_clone"),
        ],
    )
    label_hs_role = blocks.if_var_equals(
        "hud role",
        HUD_ROLE_ID,
        HUD_ROLE_LABEL_HIGH_SCORE,
        [blocks.to_front(), blocks.show()],
    )
    # ECO-04: each "GAME OVER" glyph clone is static once spawned (like the "HIGH SCORE"
    # label above) — director-stop's clone-clear (common_stop) retires it on the next
    # transition, so it never needs to delete itself here.
    game_over_glyph_role = blocks.if_var_equals(
        "hud role",
        HUD_ROLE_ID,
        HUD_ROLE_GAME_OVER_GLYPH,
        [blocks.to_front(), blocks.show()],
    )
    blocks.chain(
        clone,
        [
            mark_clone,
            score_role,
            high_role,
            life_role,
            label_1up_role,
            label_hs_role,
            game_over_glyph_role,
        ],
    )

    # ECO-03's bonus grant broadcasts `craft changed`; every life-icon clone deletes
    # itself, and only the original (the sole instance where `hud is clone` stays 0)
    # rebuilds the row from the live `craft` count via the shared spawn proc.
    craft_changed = blocks.receive("craft changed")
    delete_life = blocks.if_var_equals(
        "hud role", HUD_ROLE_ID, HUD_ROLE_LIFE, [blocks.add("control_delete_this_clone")]
    )
    respawn = blocks.if_var_equals(
        "hud is clone",
        HUD_IS_CLONE_ID,
        0,
        [blocks.call_proc(HUD_SPAWN_CRAFT_PROCCODE, warp=True)],
    )
    blocks.chain(craft_changed, [delete_life, respawn])

    return blocks.blocks


def _ensure_hud_target(project: dict[str, Any]) -> None:
    """Create or update the `hud` target's EXISTENCE and BLOCKS only.

    Costumes are never touched here: when the target already exists (because
    tools/hud_glyphs.py already attached its glyph/life costumes), whatever
    costume list is present is preserved untouched, exactly like the
    solvalou split lets sprite_extractor own that target's costumes while
    this module owns its blocks.
    """
    existing = next(
        (target for target in project["targets"] if target.get("name") == HUD_TARGET),
        None,
    )
    if existing is not None:
        # hud blocks are (re)installed unconditionally by expected_project's replacements
        # map below — nothing to do here but leave the existing target's costumes, sounds,
        # and every other field untouched.
        return
    insertion = next(
        (
            index
            for index, target in enumerate(project["targets"])
            if target.get("name") in ("toroid_sprite_proof", "sprite_sheets")
        ),
        len(project["targets"]),
    )
    existing_orders = [
        target.get("layerOrder")
        for target in project["targets"]
        if isinstance(target.get("layerOrder"), int)
    ]
    hud_target = {
        "isStage": False,
        "name": HUD_TARGET,
        "variables": {},
        "lists": {},
        "broadcasts": {},
        "blocks": {},
        "comments": {},
        "currentCostume": 0,
        "costumes": [],
        "sounds": [],
        "volume": 100,
        "layerOrder": max(existing_orders, default=-1) + 1,
        "visible": False,
        "x": 0,
        "y": 0,
        "size": 100,
        "direction": 90,
        "draggable": False,
        "rotationStyle": "don't rotate",
    }
    project["targets"].insert(insertion, hud_target)


def expected_project(project: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(project)
    _ensure_hud_target(result)
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
        BULLET_ALLOC_RESULT_ID,
        BULLET_CURSOR_ID,
        SCORE_ID,
        HIGH_SCORE_ID,
        AWARD_VALUE_ID,
        LIVES_ID,
        NEXT_BONUS_ID,
        QUALIFIED_ID,
        AREA_PROGRESS_ID,
        AREA_NUMBER_ID,
        SCROLL_ROW_ID,
        TERRAIN_COLUMN_ID,
        SCHEDULE_CURSOR_ID,
        SCHEDULE_FIRED_ID,
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
        # AIR-12 enemy-bullet allocator result and its own sweep cursor (dormant; both are
        # the allocator's own, never shared with the blaster or the slot-sweep cursor).
        BULLET_ALLOC_RESULT_ID: ["bullet alloc result", 0],
        BULLET_CURSOR_ID: ["bullet cursor", 0],
        # ECO-01 economy: the running score and high score (Stage-written, HUD reads only) and
        # the award-value seam (machinery, set by the collision detector a later slice wires).
        SCORE_ID: ["score", 0],
        HIGH_SCORE_ID: ["high score", HIGH_SCORE_START],
        AWARD_VALUE_ID: ["award value", 0],
        # ECO-03 lives economy: remaining craft and the next bonus-life threshold (seeded from
        # the DIP tables on a world reset).
        LIVES_ID: ["craft", 0],
        NEXT_BONUS_ID: ["next bonus", 0],
        # ECO-04: the best-five verdict, recorded (never a sprite write) when the game over
        # complete receiver runs, and reset only on a world reset (cold-start/new-game).
        QUALIFIED_ID: ["qualified", 0],
        # AREA-01 area state (Stage-written, sprite-read, write-forbidden — NOT machinery).
        # `area progress` is the monotonic position authority; `scroll row` is its once-per-tick
        # derivation; `area number` tracks 1..16 (16 -> 7 loop); `terrain column` is the dormant
        # per-area start-column seam. Defaults are the area-1 top, re-established on cold-start.
        AREA_PROGRESS_ID: ["area progress", 0],
        AREA_NUMBER_ID: ["area number", AREA_FIRST],
        SCROLL_ROW_ID: ["scroll row", AREA_TOP_ROW],
        TERRAIN_COLUMN_ID: ["terrain column", AREA_MAP_COLUMNS[0]],
        # AREA-02 scheduler state (Stage-written, write-forbidden): the 1-based cursor into the
        # flattened schedule lists and the per-area count of records fired (the observable).
        SCHEDULE_CURSOR_ID: ["schedule cursor", 1],
        SCHEDULE_FIRED_ID: ["schedule fired", 0],
    }
    owned_lists = {
        ALLOWED_ID,
        SLOT_TYPE_ID,
        SLOT_STATE_ID,
        VALUE_TABLE_ID,
        STARTING_LIVES_ID,
        FIRST_BONUS_123_ID,
        FIRST_BONUS_5_ID,
        REPEAT_BONUS_123_ID,
        REPEAT_BONUS_5_ID,
        HIGH_SCORE_TABLE_ID,
        AREA_MAP_COLUMN_ID,
        SCHEDULE_HANDLER_ID,
        SCHEDULE_TRIGGER_ROW_ID,
        SCHEDULE_PAYLOAD_ID,
        AREA_SCHEDULE_START_ID,
        AREA_SCHEDULE_END_ID,
    }
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
        # ECO-01 object point values (docs/spec/data/scores.json master_value_table), in table
        # order; position i (1-based) = entries[i-1].points. Slice 8 resolves award value here.
        VALUE_TABLE_ID: ["value table", list(VALUE_TABLE_POINTS)],
        # ECO-03 lives/bonus tables (docs/spec/data/scores.json), `null` mapped to the
        # BONUS_DISABLED sentinel. Both bonus pairs are ingested; the runtime reads the 1/2/3-lives
        # pair at the default DIP.
        STARTING_LIVES_ID: ["starting lives", list(STARTING_LIVES)],
        FIRST_BONUS_123_ID: ["first bonus 123", list(FIRST_BONUS_123)],
        FIRST_BONUS_5_ID: ["first bonus 5", list(FIRST_BONUS_5)],
        REPEAT_BONUS_123_ID: ["repeat bonus 123", list(REPEAT_BONUS_123)],
        REPEAT_BONUS_5_ID: ["repeat bonus 5", list(REPEAT_BONUS_5)],
        # ECO-04 best-five table (docs/spec/data/scores.json high_score_defaults.scores),
        # in rank order; position 5 (1-based) is the fifth-place cutoff the game-over-complete
        # receiver compares the final score against.
        HIGH_SCORE_TABLE_ID: ["high score table", list(HIGH_SCORE_DEFAULTS)],
        # AREA-01 per-area terrain start columns (docs/spec/data/terrain.json
        # area_offset_in_map_tbl), indexed by area number 1-16. Ingested, not authored; a
        # read-only reference table set on area entry, never written by a sprite.
        AREA_MAP_COLUMN_ID: ["area map column", list(AREA_MAP_COLUMNS)],
        # AREA-03 schedule table (docs/spec/data/area-schedules.json), ALL 16 normal areas flattened
        # into three faithful parallel columns, each area = its records + a materialized sentinel row.
        # The two 16-entry index lists give each area's 1-based inclusive span into the columns, read at
        # runtime by area number. All read-only authority, sprite-write-forbidden.
        SCHEDULE_HANDLER_ID: ["schedule handler", list(SCHEDULE_HANDLERS)],
        SCHEDULE_TRIGGER_ROW_ID: ["schedule trigger row", list(SCHEDULE_ROWS)],
        SCHEDULE_PAYLOAD_ID: ["schedule payload", list(SCHEDULE_PAYLOADS)],
        AREA_SCHEDULE_START_ID: ["area schedule start", list(AREA_SCHEDULE_START)],
        AREA_SCHEDULE_END_ID: ["area schedule end", list(AREA_SCHEDULE_END)],
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
        "hud": hud_blocks(),
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
        elif target["name"] == "hud":
            # ECO-02: all HUD state is sprite-local (never a Stage variable) — the role
            # and place snapshotted into each clone at creation, the cached 10^place
            # divisor, the life-icon spawn cursor, and the original-vs-clone marker.
            target["variables"] = target["variables"] | {
                HUD_ROLE_ID: ["hud role", 0],
                HUD_PLACE_ID: ["hud place", 0],
                HUD_DIVISOR_ID: ["hud divisor", 1],
                HUD_LIFE_INDEX_ID: ["hud life index", 0],
                HUD_LIFE_COUNT_ID: ["hud life count", 0],
                HUD_IS_CLONE_ID: ["hud is clone", 0],
            }
    return result


def project_bytes(project: dict[str, Any]) -> bytes:
    return scratch_project._ordered_json_bytes(project)


def identifier_manifest(project: dict[str, Any]) -> dict[str, Any]:
    """Name↔id↔scope index the JS runtime harness reads.

    Keyed by the Scratch variable/list id — the stable identity that a display-name
    rename does not touch — with the current display name and owning target read
    straight from the generated project, so the manifest cannot drift from what ships.
    The harness resolves variables by these ids (game_director's own id constants) and
    hard-errors on a missing id, so a rename or removal here surfaces as a red harness
    test rather than a vacuous read of `undefined`.
    """
    variables: dict[str, Any] = {}
    for target in project["targets"]:
        scope = "Stage" if target.get("isStage") else target["name"]
        for var_id, entry in target.get("variables", {}).items():
            variables[var_id] = {"name": entry[0], "scope": scope, "kind": "variable"}
        for list_id, entry in target.get("lists", {}).items():
            variables[list_id] = {"name": entry[0], "scope": scope, "kind": "list"}
    constants = {
        # Player-shot cap: SHOT_SLOTS is an inclusive index range, so its width is the
        # ceiling the headless harness can observe (the touching-frame replenish it
        # cannot — that stays the playtest's). Only constants the harness actually consumes
        # are emitted; a future scenario adds its own here rather than carrying dead keys.
        "shot_slot_count": SHOT_SLOTS[1] - SHOT_SLOTS[0] + 1,
    }
    return {
        "schema": MANIFEST_SCHEMA,
        "constants": constants,
        "variables": variables,
    }


def manifest_bytes(project: dict[str, Any]) -> bytes:
    manifest = identifier_manifest(project)
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


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
    expected = expected_project(current)
    PROJECT_PATH.write_bytes(project_bytes(expected))
    MANIFEST_PATH.write_bytes(manifest_bytes(expected))
    print(f"generated {PROJECT_PATH.relative_to(ROOT)}")
    print(f"generated {MANIFEST_PATH.relative_to(ROOT)}")


def check() -> None:
    current = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))
    expected = expected_project(current)
    if PROJECT_PATH.read_bytes() != project_bytes(expected):
        raise SystemExit(
            "game director source is stale; inspect imported block changes before "
            "running tools/game_director.py generate"
        )
    if not MANIFEST_PATH.exists() or MANIFEST_PATH.read_bytes() != manifest_bytes(expected):
        raise SystemExit(
            "runtime identifier manifest is stale; run tools/game_director.py generate"
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
