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

# SYS-02 per-slot position/motion fields — the entity slice (8) is the first author of these,
# so they land here (record 005 deferred them "until a consumer authors positions centrally").
# Eight parallel 64-entry lists beside `slot type`/`slot state`, all in the reference's own units:
# `slot x` is the scroll axis and `slot y` the lateral axis, both 16-bit 1/32-px fixed point
# (256 units = one 8-px row/column, so `row = floor(x/256)`); `slot dx`/`slot dy` are the raw
# signed velocity deltas (applied doubled per arcade frame); `slot timer` counts arcade frames;
# `slot code` is the sprite code the renderer maps to a costume; `slot pts` is the 1-based
# `value table` position of the occupant's score (so `resolve hit` is type-agnostic); `slot flag`
# is a per-type sub-state (Toroid: 0 pre-trigger / 1 swing-right / 2 swing-left).
# OWNERSHIP DIFFERS BY SLOT RANGE: for the walk-driven occupants (flying enemies, enemy bullets)
# these lists are AUTHORITATIVE — the warp walk writes them. For the player shots (37-39) `slot x`/
# `slot y` are a one-tick-lagged MIRROR the blaster clone writes for collision-read-only (the clone
# still owns its own motion; slice 8). Reader of a shot's x/y is the walk; writer is the clone.
SLOT_X_ID = "slot-x"  # scroll axis, 1/32 px
SLOT_Y_ID = "slot-y"  # lateral axis, 1/32 px
SLOT_DX_ID = "slot-dx"  # scroll-axis velocity (raw signed delta)
SLOT_DY_ID = "slot-dy"  # lateral velocity (raw signed delta; first byte of an aim-table entry)
SLOT_TIMER_ID = "slot-timer"  # arcade-frame animation/phase clock
SLOT_CODE_ID = "slot-code"  # sprite code (renderer maps to a costume)
SLOT_PTS_ID = "slot-pts"  # 1-based value-table position of the occupant's score
SLOT_FLAG_ID = "slot-flag"  # per-type sub-state (Toroid swing: 0 none / 1 right / 2 left)
# Every position/motion list, paired (id, display name), so clear-slots and the registration
# stay in lockstep — adding a field here is the single edit that flows to both.
SLOT_FIELD_LISTS = (
    (SLOT_X_ID, "slot x"),
    (SLOT_Y_ID, "slot y"),
    (SLOT_DX_ID, "slot dx"),
    (SLOT_DY_ID, "slot dy"),
    (SLOT_TIMER_ID, "slot timer"),
    (SLOT_CODE_ID, "slot code"),
    (SLOT_PTS_ID, "slot pts"),
    (SLOT_FLAG_ID, "slot flag"),
)

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
# A player shot marked spent by the walk's shot-vs-air detector (distinct from SLOT_HIT so the one
# slot-state->HIT write stays inside `resolve hit`, SYS-03's single-hit invariant). Its clone sees
# state != ACTIVE next iteration, frees its slot, and deletes.
SHOT_SPENT = 3
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

# AIR-12 enemy-bullet pool. The 19 bullet slots (40-58) exist as a range and a collision-group
# member (#14); the allocator (added as foundation, now LIVE — the shooting Toroid calls it to
# fire) mirrors `alloc shot slot` but with its OWN result var (never
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
# WPN-02 player-shot vs flying-enemy window (`check_shot_hit_flying_enemy` $19A6): the reference's
# `sub #16; add #32` (Y) and `sub #8; add #16` (X) idiom on the shadow MSBs, i.e. shotY-enemyY in
# [-16,15] and enemyX-shotX in [-8,7] half-pixel shadow units. Same (bias,width) convention as the
# windows above; this is the first one with a live detector (the walk's shot-vs-air pass, slice 8).
HIT_WINDOW_SHOT_FLYING = (16, 32, 8, 16)
# Shadow (half-pixel) unit expressed in the slot lists' 1/32-px units: 1 half-px = 16 units. The
# detector floors each slot position to its shadow MSB before differencing, matching the reference's
# byte compare — but on the EXACT half-px delta (no mod-256 wrap), so it never produces the
# reference's rare wrap-around phantom hit between objects ~128 half-px apart (recorded deviation).
SLOT_UNITS_PER_SHADOW = 16
# One 8-px cell in shadow half-pixels (256 slot units / 16 = 16). The craft's live position is read at
# cell resolution (`read player cell`, for the aim), so its collision box is placed at player_row/col *
# this — a cell-quantized craft hit box (recorded deviation): the reference tracks the craft's sub-cell
# shadow, this port rounds it to its cell, the same rounding the aim already uses.
SHADOW_PER_CELL = 16

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
# ~169 craft (reachable by repeated bonus-life awards toward the score cap) the icons run off the
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
# (The debug S scoring fixture that stood in for a points producer was retired in slice 8, when the
# blaster-to-air hit began producing `award value` from the struck enemy's `slot pts`.)

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

# DIF-01 / FORM-01 adaptive difficulty and normal flying formations
# (docs/spec/difficulty-and-formations.md). One accumulating AI level, raised on a
# schedule and folded back at 0x80, plus the formation the incoming wave uses. The AI
# level and the formation are selected from the SAME table by two DIFFERENT indices,
# matching the reference the spec cites:
#   * set_flying_formation (sub_2_fn_2): index = the record's signed offset (NOT the AI
#     level) — sign-extended, addressing the 2-byte entries; the offset is the index.
#   * raise_ai_level_and_set_formation (sub_2_fn_3): index = the raised, folded AI level
#     (no record offset).
# The formation table (formations.json) is decoded to logical entries index -32..127, so
# the reference's byte "doubling" is already absorbed; we store count and type-offset as
# two parallel logical lists. `formation index` is a transient working register (the
# lookup index), machinery like the rng working vars. Fire masks / adjust arrive in the
# later commits of this slice; their schedule scalars are already decoded into the
# `schedule arg` column here so the column is complete once and for all.
AI_LEVEL_ID = "difficulty-ai-level"
FORMATION_COUNT_ID = "formation-count"
FORMATION_TYPE_OFFSET_ID = "formation-type-offset"
FORMATION_INDEX_ID = "formation-index"  # transient lookup index (machinery)
AI_ADJUST_ID = "difficulty-ai-adjust"  # DIF-02 transient score re-tune addend (machinery)
SCHEDULE_ARG_ID = "area-schedule-arg"  # 4th parallel schedule column (runtime scalar)
DIFFICULTY_INCREMENT_ID = "difficulty-increment"  # baked [2,0,6,16], indexed by DIP
FORMATION_COUNT_TABLE_ID = "formation-count-table"  # 160 entries, index -32..127
FORMATION_TYPE_OFFSET_TABLE_ID = "formation-type-offset-table"

# Schedule handler strings this slice dispatches on (keys into the discriminated schedule
# records). Spawn/boss handlers stay on the empty seam (slice 8).
RAISE_HANDLER = "raise_ai_level_and_set_formation"
ADJUST_HANDLER = "adjust_ai_level_from_score"
SET_FORMATION_HANDLER = "set_flying_formation"
RESET_FORMATION_HANDLER = "reset_flying_formation"
FIRE_MASK_PREFIX = "fire_mask_"
GROUND_STOP_FIRING_HANDLER = "ground_stop_firing_row"

# DIF-03 per-family fire-permission masks. Area schedules set one mask byte per firing family; the
# byte gates how often that family may fire, and the per-family firing that consumes each mask is the
# enemy slices' (8+). Each family is (handler suffix, Stage display name, Stage id). The handler is
# FIRE_MASK_PREFIX + suffix.
FIRE_MASK_FAMILIES = [
    ("derota", "fire mask derota", "fire-mask-derota"),
    ("logram", "fire mask logram", "fire-mask-logram"),
    ("zoshi", "fire mask zoshi", "fire-mask-zoshi"),
    ("terrazi", "fire mask terrazi", "fire-mask-terrazi"),
    ("kapi", "fire mask kapi", "fire-mask-kapi"),
    ("boza_logram", "fire mask boza logram", "fire-mask-boza-logram"),
    ("domogram", "fire mask domogram", "fire-mask-domogram"),
    ("andor_genesis", "fire mask andor genesis", "fire-mask-andor-genesis"),
]
GROUND_STOP_FIRING_ROW_ID = "ground-stop-firing-row"

# Project-defined cabinet difficulty DIP index (four-marker placeholder; the spec records
# no arcade power-on default, like RNG_COLD_START_SEED). Index 0 selects increment +2 —
# the LOWEST setting that still PROGRESSES (index 1 = +0 would make every raise inert and
# the difficulty director look dead on the monitors). An INDEPENDENT cabinet switch from
# DIP_STARTING_ITEM / DIP_BONUS_ITEM. It is CONSUMED LIVE this slice (it scales the
# observable AI-level growth), so its growth RATE is placeholder-driven and is NOT a
# fidelity claim — only the growth MECHANISM is. Recorded in docs/mechanics/019.
DIFFICULTY_DIP_INDEX = 0
AI_LEVEL_FOLD_THRESHOLD = 0x80  # a raise reaching >= 128 folds back (never clamps)
AI_LEVEL_FOLD_SUBTRACT = 0x40  # ... by subtracting 64 once
FORMATION_MIN_INDEX = -32  # formations.json domain lower bound (bytes before the label)
FORMATION_TABLE_LEN = 160  # entries, index -32..127 inclusive


def _load_difficulty_increments() -> list[int]:
    # DIF-01: the four cabinet AI-level increments [2,0,6,16] (difficulty.json), ingested
    # (never authored), verified against the hash manifest at load.
    return list(_load_spec_data("difficulty.json")["difficulty_tbl"]["values"])


def _load_formation_tables() -> tuple[list[int], list[int]]:
    # FORM-01: the normal flying-formation table (formations.json), decoded to logical
    # entries index -32..127, split into two parallel lists in list-position order (position
    # p, 1-based, is index p - 1 + FORMATION_MIN_INDEX). Fail LOUD if the entries are not
    # exactly that contiguous domain, once each (mirrors the area-set check).
    entries = _load_spec_data("formations.json")["formation_table"]["entries"]
    ordered = sorted(entries, key=lambda e: e["index"])
    expected = list(range(FORMATION_MIN_INDEX, FORMATION_MIN_INDEX + FORMATION_TABLE_LEN))
    if [e["index"] for e in ordered] != expected:
        raise SystemExit(
            "formations.json must define exactly the contiguous indices "
            f"{FORMATION_MIN_INDEX}..{FORMATION_MIN_INDEX + FORMATION_TABLE_LEN - 1}, once each"
        )
    counts = [e["enemy_count"] for e in ordered]
    offsets = [e["type_table_offset"] for e in ordered]
    return counts, offsets


DIFFICULTY_INCREMENTS = _load_difficulty_increments()
FORMATION_COUNTS, FORMATION_TYPE_OFFSETS = _load_formation_tables()

# The spawner refills the first `formation count` flying slots (FLYING_SLOTS), so no formation may
# ask for more enemies than there are flying slots — otherwise the extra `data_replaceitemoflist`
# writes would fall out of range and silently under-spawn. Fail LOUD at generation instead, so a
# future formations.json regeneration that breaches the capacity is caught here, not in play.
_FLYING_SLOT_CAPACITY = FLYING_SLOTS[1] - FLYING_SLOTS[0] + 1
if max(FORMATION_COUNTS) > _FLYING_SLOT_CAPACITY:
    raise SystemExit(
        f"formations.json max enemy_count {max(FORMATION_COUNTS)} exceeds the "
        f"{_FLYING_SLOT_CAPACITY} flying slots ({FLYING_SLOTS[0]}-{FLYING_SLOTS[1]})"
    )

# AIR-01 / AIR-12 32-direction homing-aim tables (aiming.json), INGESTED (never authored),
# verified against the hash manifest at load. Each speed tier is two parallel 32-entry lists,
# `aim dy N` / `aim dx N`, storing the (dy, dx) velocity pair per direction index (dy first, per
# the reference's cpy_dY_dX_to_obj — the extractor records the byte-order there). This slice bakes
# only the two tiers Toroid uses: the 24-magnitude table (its 1.5 px/frame approach) and the
# 32-magnitude generic table (its aimed bullet at 2 px/frame). The 33-entry `octant table` is the
# quantizer's lookup (get_index_for_angle). Dormant DATA this slice (the aim proc and its callers
# land in the next commit) — like the hit-window constants, baked now so the consumer just reads it.
OCTANT_TABLE_ID = "octant-table"
AIM_DY_24_ID = "aim-dy-24"  # Toroid approach tier (magnitude 24 = 1.5 px/frame)
AIM_DX_24_ID = "aim-dx-24"
AIM_DY_32_ID = "aim-dy-32"  # aimed-bullet / generic tier (magnitude 32 = 2 px/frame)
AIM_DX_32_ID = "aim-dx-32"


def _load_aiming_tables() -> dict[str, list[int]]:
    data = _load_spec_data("aiming.json")["aiming"]
    tables = {"octant": list(data["octant_table"]["values"])}
    for tier in ("toroid", "generic"):
        vectors = data["angle_tables"][tier]["vectors"]
        tables[f"{tier}_dy"] = [v["dy"] for v in vectors]
        tables[f"{tier}_dx"] = [v["dx"] for v in vectors]
    return tables


_AIMING = _load_aiming_tables()
OCTANT_TABLE = _AIMING["octant"]
AIM_DY_24, AIM_DX_24 = _AIMING["toroid_dy"], _AIMING["toroid_dx"]
AIM_DY_32, AIM_DX_32 = _AIMING["generic_dy"], _AIMING["generic_dx"]

# --- AIR-01 Toroid live-combat machinery (slice 8) ---------------------------------------------
# The 32-direction aim quantizer's working vars (custom blocks have no locals): the two input diffs
# (player - slot, in 8-px units), the large/small/swap/base/fine intermediates, and the resolved
# 1-based direction index. `compute aim index` reads the two diff vars and writes `aim index`.
AIM_DX_DIFF_ID = "aim-dx-diff"  # scroll-axis diff (player row - slot row)
AIM_DY_DIFF_ID = "aim-dy-diff"  # lateral diff (player col - slot col)
AIM_LARGE_ID = "aim-large"
AIM_SMALL_ID = "aim-small"
AIM_SWAP_ID = "aim-swap"  # 1 when |dy| > |dx| (the reflect branch)
AIM_BASE_ID = "aim-base"  # quadrant-folded base index (0..255)
AIM_FINE_ID = "aim-fine"  # (base + 4) mod 256, before the >>3 & 0x1f
AIM_INDEX_ID = "aim-index"  # resolved 1-based index into the 32-entry aim lists
COMPUTE_AIM_PROCCODE = "compute aim index"

# The craft's live position, read once per walk (via sensing_of on the solvalou sprite) and mapped
# back to arcade 8-px row/column, so every slot's aim/collision test uses one cached pair.
PLAYER_ROW_ID = "player-row"  # scroll axis
PLAYER_COL_ID = "player-col"  # lateral axis
READ_PLAYER_PROCCODE = "read player cell"
# WPN-02: the shot-vs-air overlap detector (walk-driven, per active flying slot) and the per-tick
# explosion advance for a struck Toroid.
CHECK_AIR_HIT_PROCCODE = "check air shot hit"
EXPLODE_TICK_PROCCODE = "explode toroid tick"
# AIR-12 / PLY-02: the enemy-bullet per-tick update (aim-once-then-fly, cull, craft collision) and the
# player-hit flag it (and the flying-enemy craft check) raise for the non-warp walk thread to act on.
UPDATE_BULLET_PROCCODE = "update bullet"
PLAYER_HIT_ID = "player-hit"
# Debug/test invulnerability flag (default 0). When 1, the walk still RAISES `player hit` on contact
# but the death is not triggered — a dormant hook the headless harness sets so its agency-less craft
# survives while it observes the schedule/spawner (a stationary craft with no shooting/dodging is
# killed by homing enemies within one headless pump). Never set by game logic, so real play is
# unaffected; it is the seam a future "invulnerability" easter-egg key could toggle.
INVULN_ID = "invuln"
BULLET_INIT_CODE = 0  # enemy-bullet sprite code at spawn (renderer stand-in ignores the pulse)

# The spawner's own sweep cursor (like the bullet allocator's — never the shared `slot index`); the
# per-dispatch type register; and the spawn-draw attempt counter.
SPAWN_CURSOR_ID = "spawn-cursor"
WALK_TYPE_ID = "walk-type"
SPAWN_ATTEMPTS_ID = "spawn-attempts"
SPAWN_FOUND_ID = "spawn-found"  # set when the bounded spawn-column draw accepts a column
SPAWN_FLYING_PROCCODE = "spawn flying enemies"
INIT_TOROID_PROCCODE = "init toroid"
UPDATE_TOROID_PROCCODE = "update toroid"
CULL_SLOT_PROCCODE = "cull slot"

# Object type codes this slice's flying dispatch handles (object-types.json). Other formation-named
# families (e.g. Torkan, code 15, which area 1 also names) are SKIPPED by the spawner until their
# own slice builds them — a recorded deviation (fewer enemies than the arcade pre-slice-10).
TOROID_TYPE = 10  # 0x0A, non-shooting
TOROID_SHOOTS_TYPE = 11  # 0x0B, fires one aimed bullet at the swing trigger
FLYING_HANDLED_TYPES = (TOROID_TYPE, TOROID_SHOOTS_TYPE)
TOROID_PTS = 3  # 1-based value-table position of 30 points (init_toroid PTS byte 6)
TOROID_INIT_CODE = 8  # face-on sprite code at spawn (codes 8..15 cycle during the swing)

# Slot sub-state (`slot flag`) for the Toroid: pre-trigger, then a committed swing side.
TOROID_FLAG_APPROACH = 0
TOROID_FLAG_SWING_RIGHT = 1
TOROID_FLAG_SWING_LEFT = 2
# Swing trigger: the lateral-column offset (player col - slot col) lies in [LOW, HIGH] (is_close_to
# _solvalou_Y, 20CB); direction is the sign of that offset.
TOROID_SWING_LOW = -2
TOROID_SWING_HIGH = 1

# Motion / cull, in slot units (1/32 px; row = floor(slot x / 256), col = floor(slot y / 256)).
SLOT_UNITS_PER_CELL = 256
TICK_VELOCITY_SCALE = 4  # 1 tick = 2 arcade frames; each applies 2*velocity => 4*velocity/tick
TICK_TIMER_STEP = 2  # the animation clock advances 2 arcade frames per tick
TOROID_SWING_ACCEL = 2  # lateral velocity change per tick (1 unit/frame * 2 frames)
CULL_ROW_MAX = 40  # >= 0x28 rows (past the bottom) -> offscreen
CULL_ROW_MIN = -2  # <= -2 rows (past the top, the reference's byte-wrap) -> offscreen
CULL_COL_MAX = 31  # >= 0x1F columns -> offscreen (lateral)
CULL_COL_MIN = -2  # <= -2 columns -> offscreen (left edge; bullets can fly out any side)
TOROID_SPAWN_ROW = 0  # new/refilled flying enemies enter from the top row (see install_init_toroid)

# FORM-01 spawner draw (gen_rnd_spriteY 5155-5169): lateral column = (rnd & 31), reject >= 25, + 3
# => column 3..27; also reject a column within SPAWN_CRAFT_GAP of the craft. The reference loops
# unbounded; the port bounds it at SPAWN_DRAW_ATTEMPTS and, on exhaustion, skips the spawn this tick
# (retried next) — a deterministic, seeded-reproducible deviation (~0.5%), recorded in record 024.
SPAWN_COL_MASK = 31
SPAWN_COL_REJECT_AT = 25
SPAWN_COL_OFFSET = 3
SPAWN_CRAFT_GAP = 8
SPAWN_DRAW_ATTEMPTS = 16

# FORM-01 flying-enemy type table (object-types.json), baked so the spawner reads the wave's type
# codes; and the Toroid costume-ordinal map (sprite code 8..15 -> one of the 7 turn frames, the 8th
# reusing frame 6 — an 8-onto-7 palindrome wrap, the missing 8th phase recorded uncertain).
FLYING_TYPE_TABLE_ID = "flying-type-table"
TOROID_FRAME_ID = "toroid-frame"
TOROID_FRAME_MAP = [1, 2, 3, 4, 5, 6, 7, 6]


def _load_flying_type_table() -> list[int]:
    # AIR-01/FORM-01: the flying-enemy type codes (object-types.json), INGESTED, hash-verified at load.
    return list(_load_spec_data("object-types.json")["flying_enemy_type_table"]["codes"])


FLYING_TYPE_CODES = _load_flying_type_table()

# AIR-01 Toroid renderer target (game_director owns its EXISTENCE + BLOCKS; tools/sprite_extractor.py
# owns its COSTUMES — the same split as hud / hud_glyphs and solvalou). One persistent clone per
# flying slot renders that slot's live state; the original stays hidden.
TOROID_TARGET = "toroid"
# The gameplay Toroid target reuses the costumes already extracted onto the sprite-extraction proof
# target (record 002) — the same 7 verified turn frames, by md5 reference. This deliberately keeps a
# SINGLE owner of the toroid target (existence + blocks + these referenced costumes) rather than the
# two-generator split hud uses: the extractor's overlap guard forbids duplicate crops, and retiring
# the proof to re-own the frames would churn record 002; referencing the already-verified assets is
# the smaller, lower-risk change and removes the cross-generator ordering coupling entirely. Recorded
# as a deviation in docs/mechanics/024.
TOROID_PROOF_TARGET = "toroid_sprite_proof"
TOROID_CLONE_SLOT_ID = "toroid-clone-slot"  # sprite-local: which flying slot this clone renders
# Port render map (arcade cell -> stage px), applied ONLY here and in the one player read. Independent
# per-axis (core-game-systems "not one ratified factor"): lateral column c -> x = c*15 - 240 (the
# 256-col space across the play width); scroll row r -> y = 155 - r*8 (rows down the play height).
# The craft's port spawn (0, -85) fixes the anchors: col 16 -> x 0, row 30 -> y -85. Operator-tuned,
# confirmed by eye at playtest; render-only, so it never touches a slot list or the build hash.
RENDER_COL_STAGE = 15
RENDER_COL_OFFSET = 240
RENDER_ROW_TOP = 155
RENDER_ROW_STAGE = 8
TOROID_RENDER_SIZE = 225  # 16-px sprite at ~2.25 stage px/px, matching solvalou's on-screen scale
# WPN-02 hit/explosion state (`flying_enemy_hit` 4865–4902): a struck flying enemy explodes over 20
# arcade frames = 10 ticks, five 4-frame phases, still drifting on its velocity; at arcade frame 8 the
# sprite doubles (2x) with a one-cell recentre; then the slot is freed. While exploding it neither hits
# nor is hit. The explosion sprite reuses the verified solv_death frames as a recorded stand-in (record
# 025) — the mechanic (explode → score → gone) is exact; dedicated Toroid-burst crops are deferred.
TOROID_HIT_DURATION_FRAMES = 20
TOROID_EXPLOSION_PHASE_FRAMES = 4  # 20 / 4 = five phases
TOROID_EXPLOSION_PHASES = 5
TOROID_TURN_FRAME_COUNT = 7  # turn costumes precede the referenced explosion costumes on the target
TOROID_BIG_PHASE = 2  # the 2x phase (arcade frame 8): size doubles, sprite recentres one cell
TOROID_EXPLODE_SIZE = 450  # 2x TOROID_RENDER_SIZE for the big phase
# AIR-12 enemy-bullet renderer: one persistent clone per bullet slot (40-58), a small stand-in sprite
# (dedicated bullet crops + the reference's 4-colour pulse deferred with the other art, record 026).
ENEMY_BULLET_TARGET = "enemy_bullet"
ENEMY_BULLET_CLONE_SLOT_ID = "enemy-bullet-clone-slot"
ENEMY_BULLET_RENDER_SIZE = 90  # a small dot relative to the 225 enemy scale


def _schedule_arg(record: dict) -> int:
    # DIF-01/03 + FORM-01: the single runtime-readable scalar each dispatched handler needs,
    # pre-decoded from the opaque JSON payload (Scratch cannot parse JSON at runtime). A
    # set-formation record carries its signed formation index; a fire-mask its byte; the
    # ground-stop-firing row its row. Every other handler (raise/adjust/reset, and the
    # spawn/boss kinds still on the empty dispatch seam) needs no scalar -> 0.
    handler = record["handler"]
    params = record.get("params", {})
    if handler == SET_FORMATION_HANDLER:
        return params["formation_offset"]
    if handler.startswith(FIRE_MASK_PREFIX):
        return params["mask"]
    if handler == GROUND_STOP_FIRING_HANDLER:
        return params["row"]
    return 0


def _load_area_schedule(
    area_number: int,
) -> tuple[list[str], list[int], list[str], list[int]]:
    # AREA-02: ingest one area's schedule from the committed, hash-pinned reference data as four
    # faithful parallel columns (handler, trigger row, opaque payload, and DIF-01/03+FORM-01's
    # runtime scalar `arg`). The end sentinel (a scalar in the JSON) is MATERIALIZED as the terminal
    # row so the table is self-terminating and the extractor's "every table decodes to its sentinel"
    # invariant is reproduced. object_type + params are serialized deterministically (sorted keys)
    # into the payload; source_line is provenance, not runtime data, and is deliberately not ingested.
    # The `arg` column carries the one scalar the runtime dispatch reads per record (Scratch cannot
    # parse the JSON payload) — see _schedule_arg; the sentinel's arg is 0. The round-trip golden in
    # tests/test_spec_docs.py proves nothing is dropped and all four columns stay the same length.
    data = _load_spec_data("area-schedules.json")
    area = next(a for a in data["areas"] if a["area"] == area_number)
    handlers: list[str] = []
    rows: list[int] = []
    payloads: list[str] = []
    args: list[int] = []
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
        args.append(_schedule_arg(record))
    handlers.append(SCHEDULE_SENTINEL_HANDLER)
    rows.append(area["end_sentinel"])
    payloads.append("")
    args.append(0)
    return handlers, rows, payloads, args


def _load_all_area_schedules() -> tuple[
    list[str], list[int], list[str], list[int], list[int], list[int]
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
    args: list[int] = []
    starts: list[int] = []
    ends: list[int] = []
    cursor = 1  # 1-based, matching Scratch list indexing and the runtime `schedule cursor`
    for area_number in range(AREA_FIRST, AREA_MAX + 1):
        area_handlers, area_rows, area_payloads, area_args = _load_area_schedule(area_number)
        starts.append(cursor)  # this area's first index (before advancing the cursor)
        handlers.extend(area_handlers)
        rows.extend(area_rows)
        payloads.extend(area_payloads)
        args.extend(area_args)
        cursor += len(area_rows)
        ends.append(cursor - 1)  # this area's last index (after advancing; inclusive)
    return handlers, rows, payloads, args, starts, ends


(
    SCHEDULE_HANDLERS,
    SCHEDULE_ROWS,
    SCHEDULE_PAYLOADS,
    SCHEDULE_ARGS,
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

    def op_lt(self, a: Any, b: Any) -> str:
        return self._reporter("operator_lt", a, b)

    def op_and(self, a: Any, b: Any) -> str:
        # Boolean reporters attach to OPERAND1/OPERAND2 (the non-arithmetic pair).
        return self._reporter("operator_and", a, b)

    def op_or(self, a: Any, b: Any) -> str:
        return self._reporter("operator_or", a, b)

    def op_not(self, operand: str) -> str:
        block_id = self.add("operator_not", inputs={"OPERAND": [2, operand]})
        self.blocks[operand]["parent"] = block_id
        return block_id

    def op_abs(self, operand: Any) -> str:
        # scratch-vm mathop reads OPERATOR (not OPERATION); a wrong key returns 0 for every input.
        return self._mathop("abs", operand)

    def op_round(self, operand: Any) -> str:
        # `round` is its OWN block (operator_round), NOT an operator_mathop function — mathop only
        # knows floor/ceiling/abs/sqrt/trig/log, so "round" there is an unknown op that returns 0.
        block_id = self.add("operator_round")
        if isinstance(operand, str):
            self.blocks[block_id]["inputs"] = {"NUM": [2, operand]}
            self.blocks[operand]["parent"] = block_id
        else:
            self.blocks[block_id]["inputs"] = {"NUM": operand}
        return block_id

    def _mathop(self, fn: str, operand: Any) -> str:
        block_id = self.add("operator_mathop", fields={"OPERATOR": [fn, None]})
        if isinstance(operand, str):
            self.blocks[block_id]["inputs"] = {"NUM": [2, operand]}
            self.blocks[operand]["parent"] = block_id
        else:
            self.blocks[block_id]["inputs"] = {"NUM": operand}
        return block_id

    def sensing_of(self, prop: str, sprite: str) -> str:
        # Read another sprite's property (e.g. "x position") via sensing_of; the OBJECT operand is a
        # shadow menu naming the sprite. Used to read the live craft position into the walk.
        menu = self.add(
            "sensing_of_object_menu", shadow=True, fields={"OBJECT": [sprite, None]}
        )
        block_id = self.add(
            "sensing_of",
            fields={"PROPERTY": [prop, None]},
            inputs={"OBJECT": [1, menu]},
        )
        self.blocks[menu]["parent"] = block_id
        return block_id

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

    def xposition(self) -> str:
        return self.add("motion_xposition")

    def yposition(self) -> str:
        return self.add("motion_yposition")

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
    # Zero every slot list — type/state and all eight position/motion fields — so a reset leaves
    # an identical clean slate (the seeded-replay determinism leans on this). Every `slot *` list
    # is cleared here; a structural test asserts the set matches the registered slot lists.
    clears = [
        blocks.list_replace("slot type", SLOT_TYPE_ID, cursor(), number(0)),
        blocks.list_replace("slot state", SLOT_STATE_ID, cursor(), number(0)),
    ]
    clears += [
        blocks.list_replace(name, list_id, cursor(), number(0))
        for list_id, name in SLOT_FIELD_LISTS
    ]
    clears.append(blocks.change_var("slot index", SLOT_INDEX_ID, 1))
    blocks.substack(loop, clears)
    blocks.chain(definition, [set_index, loop])


def install_advance_slots(blocks: Blocks) -> None:
    # SYS-04 centralized ordered update: one atomic (warp) pass over the 64 slots in ascending
    # index order, advancing the tick clock and dispatching each occupied slot by its type. The
    # Toroid (types 0x0A / 0x0B) is the first live occupant (slice 8); other occupant types keep
    # the empty seam their slices fill. Empty slots are skipped first (the cheap fast path).
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
    # ENGINE-TODO: the other flying/ground/boss families append their per-type branches to this
    # walk dispatch as their slices build them (the occupant's type is read once into `walk type`
    # first, then dispatched — Toroid and enemy-bullet branches are wired this slice).
    read_type = blocks.set_var_expr(
        "walk type", WALK_TYPE_ID, blocks.list_item("slot type", SLOT_TYPE_ID, cursor())
    )
    is_toroid = blocks.op_or(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(TOROID_TYPE)),
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(TOROID_SHOOTS_TYPE)),
    )
    toroid_branch = blocks.if_reporter(
        is_toroid, [blocks.call_proc(UPDATE_TOROID_PROCCODE, warp=True)]
    )
    bullet_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(BULLET_TYPE)),
        [blocks.call_proc(UPDATE_BULLET_PROCCODE, warp=True)],
    )
    dispatch = blocks.if_reporter(occupied, [read_type, toroid_branch, bullet_branch])
    blocks.substack(loop, [dispatch, blocks.change_var("slot index", SLOT_INDEX_ID, 1)])
    blocks.chain(definition, [advance_tick, set_index, loop])


def _cur_item(blocks: Blocks, name: str, list_id: str) -> str:
    """`item (slot index) of <list>` — the field of the slot the walk/spawner is on."""
    return blocks.list_item(name, list_id, variable("slot index", SLOT_INDEX_ID))


def _set_cur_item(blocks: Blocks, name: str, list_id: str, value: Any) -> str:
    return blocks.list_replace(name, list_id, variable("slot index", SLOT_INDEX_ID), value)


def _cur_row(blocks: Blocks) -> str:
    """floor(slot x / 256) — the current slot's scroll-axis row in 8-px cells."""
    return blocks.op_floor(blocks.op_div(_cur_item(blocks, "slot x", SLOT_X_ID), number(SLOT_UNITS_PER_CELL)))


def _cur_col(blocks: Blocks) -> str:
    """floor(slot y / 256) — the current slot's lateral column in 8-px cells."""
    return blocks.op_floor(blocks.op_div(_cur_item(blocks, "slot y", SLOT_Y_ID), number(SLOT_UNITS_PER_CELL)))


def _craft_overlap_reporter(blocks: Blocks) -> str:
    """PLY-02: boolean — does the current slot (`slot index`) overlap the craft's cell within the shared
    flying/bullet hit window (HIT_WINDOW_BULLET_FLYING)? The craft is placed at player row/col scaled to
    shadow half-px (cell-quantized); the object is floored to its shadow MSB. Y is the scroll axis, X the
    lateral, matching the reference's `check_bullet_or_flying_hit_solvalou` byte compare."""
    y_bias, y_width, x_bias, x_width = HIT_WINDOW_BULLET_FLYING
    dy_low, dy_high = -y_bias, y_width - y_bias - 1
    dx_low, dx_high = -x_bias, x_width - x_bias - 1
    sh = lambda expr: blocks.op_floor(blocks.op_div(expr, number(SLOT_UNITS_PER_SHADOW)))
    craft_y = blocks.op_mul(variable("player row", PLAYER_ROW_ID), number(SHADOW_PER_CELL))
    d_y = blocks.op_sub(craft_y, sh(_cur_item(blocks, "slot x", SLOT_X_ID)))
    hit_y = blocks.op_and(
        blocks.op_not(blocks.op_lt(d_y, number(dy_low))),
        blocks.op_not(blocks.op_gt(d_y, number(dy_high))),
    )
    craft_x = blocks.op_mul(variable("player col", PLAYER_COL_ID), number(SHADOW_PER_CELL))
    d_x = blocks.op_sub(sh(_cur_item(blocks, "slot y", SLOT_Y_ID)), craft_x)
    hit_x = blocks.op_and(
        blocks.op_not(blocks.op_lt(d_x, number(dx_low))),
        blocks.op_not(blocks.op_gt(d_x, number(dx_high))),
    )
    return blocks.op_and(hit_y, hit_x)


def install_compute_aim_index(blocks: Blocks) -> None:
    # AIR-01/AIR-12 aim quantizer (get_index_for_angle 0EB2): turn the vector (aim dx diff, aim dy
    # diff) — player minus slot, in 8-px cells — into a 1-based index into the 32-entry aim tables.
    # Exact integer reproduction of the reference: octant lookup on floor(32*small/large), reflected
    # across 45 deg when |dy|>|dx|, then quadrant-folded by the diff signs, then ((base+4) mod 256)>>3.
    definition = _install_warp_proc(blocks, COMPUTE_AIM_PROCCODE)
    dx = lambda: variable("aim dx diff", AIM_DX_DIFF_ID)
    dy = lambda: variable("aim dy diff", AIM_DY_DIFF_ID)
    large = lambda: variable("aim large", AIM_LARGE_ID)
    small = lambda: variable("aim small", AIM_SMALL_ID)
    base = lambda: variable("aim base", AIM_BASE_ID)

    set_large = blocks.set_var_expr("aim large", AIM_LARGE_ID, blocks.op_abs(dx()))
    set_small = blocks.set_var_expr("aim small", AIM_SMALL_ID, blocks.op_abs(dy()))
    # if |dy| > |dx|: swap so large=max, small=min, and remember the swap (reflect branch).
    swap_if = blocks.add("control_if_else")
    need_swap = blocks.op_gt(small(), large())
    blocks.blocks[swap_if]["inputs"]["CONDITION"] = [2, need_swap]
    blocks.substack(
        swap_if,
        [
            blocks.set_var("aim base", AIM_BASE_ID, large()),  # aim base as a scratch temp
            blocks.set_var("aim large", AIM_LARGE_ID, small()),
            blocks.set_var("aim small", AIM_SMALL_ID, base()),
            blocks.set_var("aim swap", AIM_SWAP_ID, number(1)),
        ],
    )
    blocks.substack(swap_if, [blocks.set_var("aim swap", AIM_SWAP_ID, number(0))], name="SUBSTACK2")
    # base = large==0 ? 0 : octant[floor(32*small/large)+1], reflected when swapped.
    zero_if = blocks.add("control_if_else")
    large_zero = blocks.op_eq(large(), number(0))
    blocks.blocks[zero_if]["inputs"]["CONDITION"] = [2, large_zero]
    blocks.substack(zero_if, [blocks.set_var("aim base", AIM_BASE_ID, number(0))])
    ratio = blocks.op_floor(
        blocks.op_div(blocks.op_mul(number(32), small()), large())
    )
    octant = blocks.list_item("octant table", OCTANT_TABLE_ID, blocks.op_add(ratio, number(1)))
    set_from_octant = blocks.set_var_expr("aim base", AIM_BASE_ID, octant)
    reflect_if = blocks.if_reporter(
        blocks.op_eq(variable("aim swap", AIM_SWAP_ID), number(1)),
        [blocks.set_var_expr("aim base", AIM_BASE_ID, blocks.op_sub(number(0x40), base()))],
    )
    blocks.substack(zero_if, [set_from_octant, reflect_if], name="SUBSTACK2")
    # quadrant fold by the ORIGINAL diff signs.
    dx_neg = blocks.if_reporter(
        blocks.op_lt(dx(), number(0)),
        [blocks.set_var_expr("aim base", AIM_BASE_ID, blocks.op_sub(number(0x80), base()))],
    )
    dy_neg = blocks.if_reporter(
        blocks.op_lt(dy(), number(0)),
        [blocks.set_var_expr("aim base", AIM_BASE_ID, blocks.op_mod(blocks.op_sub(number(256), base()), number(256)))],
    )
    # aim fine = (base + 4) mod 256; aim index = floor(fine/8) + 1 (1-based into the 32-entry lists).
    set_fine = blocks.set_var_expr(
        "aim fine", AIM_FINE_ID, blocks.op_mod(blocks.op_add(base(), number(4)), number(256))
    )
    set_index = blocks.set_var_expr(
        "aim index",
        AIM_INDEX_ID,
        blocks.op_add(blocks.op_floor(blocks.op_div(variable("aim fine", AIM_FINE_ID), number(8))), number(1)),
    )
    blocks.chain(definition, [set_large, set_small, swap_if, zero_if, dx_neg, dy_neg, set_fine, set_index])


def install_read_player_cell(blocks: Blocks) -> None:
    # Read the craft's live stage position once per walk and map it back to arcade 8-px cells, so
    # every slot's aim and collision test uses one cached (player row, player col). Inverse of the
    # render map: col = round((x + 240)/15); row = round((155 - y)/8).
    definition = _install_warp_proc(blocks, READ_PLAYER_PROCCODE)
    craft_x = blocks.sensing_of("x position", "solvalou")
    craft_y = blocks.sensing_of("y position", "solvalou")
    set_col = blocks.set_var_expr(
        "player col",
        PLAYER_COL_ID,
        blocks.op_round(blocks.op_div(blocks.op_add(craft_x, number(RENDER_COL_OFFSET)), number(RENDER_COL_STAGE))),
    )
    set_row = blocks.set_var_expr(
        "player row",
        PLAYER_ROW_ID,
        blocks.op_round(blocks.op_div(blocks.op_sub(number(RENDER_ROW_TOP), craft_y), number(RENDER_ROW_STAGE))),
    )
    blocks.chain(definition, [set_col, set_row])


def install_init_toroid(blocks: Blocks) -> None:
    # AIR-01: initialize the flying slot at `slot index` as a Toroid of type `walk type`. Draw a
    # lateral spawn column from the shared stream (reject-and-redraw, bounded); on a successful draw,
    # stamp the slot occupied and aim it at the craft on the 24-magnitude tier. On exhaustion, leave
    # the slot empty (retried next tick) — the recorded bounded-draw deviation. The scroll-axis
    # position is NOT set: a refill inherits the previous occupant's row (coded), 0 at cold start.
    definition = _install_warp_proc(blocks, INIT_TOROID_PROCCODE)
    reset = [
        blocks.set_var("spawn attempts", SPAWN_ATTEMPTS_ID, number(0)),
        blocks.set_var("spawn found", SPAWN_FOUND_ID, number(0)),
    ]
    draw_loop = blocks.add("control_repeat_until")
    done = blocks.op_or(
        blocks.op_eq(variable("spawn found", SPAWN_FOUND_ID), number(1)),
        blocks.op_gt(variable("spawn attempts", SPAWN_ATTEMPTS_ID), number(SPAWN_DRAW_ATTEMPTS - 1)),
    )
    blocks.blocks[draw_loop]["inputs"]["CONDITION"] = [2, done]
    candidate = blocks.op_mod(variable("rng out", RNG_OUT_ID), number(SPAWN_COL_MASK + 1))
    in_range = blocks.op_lt(candidate, number(SPAWN_COL_REJECT_AT))
    col = blocks.op_add(blocks.op_mod(variable("rng out", RNG_OUT_ID), number(SPAWN_COL_MASK + 1)), number(SPAWN_COL_OFFSET))
    far_enough = blocks.op_not(
        blocks.op_lt(
            blocks.op_abs(blocks.op_sub(variable("player col", PLAYER_COL_ID), col)),
            number(SPAWN_CRAFT_GAP),
        )
    )
    accept = blocks.if_reporter(
        far_enough,
        [
            _set_cur_item(
                blocks,
                "slot y",
                SLOT_Y_ID,
                blocks.op_mul(blocks.op_add(blocks.op_mod(variable("rng out", RNG_OUT_ID), number(SPAWN_COL_MASK + 1)), number(SPAWN_COL_OFFSET)), number(SLOT_UNITS_PER_CELL)),
            ),
            blocks.set_var("spawn found", SPAWN_FOUND_ID, number(1)),
        ],
    )
    valid = blocks.if_reporter(in_range, [accept])
    blocks.substack(
        draw_loop,
        [
            blocks.call_proc(RNG_PROCCODE, warp=True),
            valid,
            blocks.change_var("spawn attempts", SPAWN_ATTEMPTS_ID, 1),
        ],
    )
    # On a successful draw, stamp the slot and aim it at the craft (24-magnitude tier).
    stamp = blocks.if_reporter(
        blocks.op_eq(variable("spawn found", SPAWN_FOUND_ID), number(1)),
        [
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, variable("walk type", WALK_TYPE_ID)),
            _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_ACTIVE)),
            # Enter from the TOP row. The reference's world-scroll carries flying enemies down from the
            # top; this self-propelled port has no enemy scroll, so a refilled slot would otherwise
            # inherit the previous occupant's (mid-field) scroll position and aim a steep short-range
            # dive that clips the craft before its swing can divert. Set the scroll row to the top BEFORE
            # aiming so every wave streams in from the top and has room to swing (recorded deviation).
            _set_cur_item(blocks, "slot x", SLOT_X_ID, number(TOROID_SPAWN_ROW * SLOT_UNITS_PER_CELL)),
            blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
            blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
            blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 24", AIM_DX_24_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 24", AIM_DY_24_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
            _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(TOROID_FLAG_APPROACH)),
            _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(TOROID_INIT_CODE)),
            _set_cur_item(blocks, "slot pts", SLOT_PTS_ID, number(TOROID_PTS)),
        ],
    )
    blocks.chain(definition, [*reset, draw_loop, stamp])


def install_check_air_hit(blocks: Blocks) -> None:
    # WPN-02: test the active flying enemy at `slot index` against the three player-shot slots (their
    # live positions mirrored into slot x/y by the blaster clone). On the first overlapping ACTIVE
    # shot, resolve the hit: mark the enemy struck and score its value type-agnostically (`resolve hit`
    # reads `slot pts` into the value table), start its explosion clock, and mark the shot spent so its
    # clone self-destroys and frees its slot. Each later shot check is gated on the enemy still being
    # ACTIVE, so one enemy resolves at most one hit per tick. The window is the reference's shadow-MSB
    # compare (`check_shot_hit_flying_enemy`): each position floored to its half-px shadow MSB, then the
    # (bias,width) window HIT_WINDOW_SHOT_FLYING — on the exact half-px delta (no mod-256 wrap).
    definition = _install_warp_proc(blocks, CHECK_AIR_HIT_PROCCODE)
    y_bias, y_width, x_bias, x_width = HIT_WINDOW_SHOT_FLYING
    dy_low, dy_high = -y_bias, y_width - y_bias - 1
    dx_low, dx_high = -x_bias, x_width - x_bias - 1
    sh = lambda expr: blocks.op_floor(blocks.op_div(expr, number(SLOT_UNITS_PER_SHADOW)))
    shot_x = lambda s: blocks.list_item("slot x", SLOT_X_ID, number(s))
    shot_y = lambda s: blocks.list_item("slot y", SLOT_Y_ID, number(s))

    body: list[str] = []
    for s in range(SHOT_SLOTS[0], SHOT_SLOTS[1] + 1):
        shot_live = blocks.op_and(
            blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, number(s)), number(SHOT_TYPE)),
            blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, number(s)), number(SLOT_ACTIVE)),
        )
        enemy_live = blocks.op_eq(_cur_item(blocks, "slot state", SLOT_STATE_ID), number(SLOT_ACTIVE))
        d_y = blocks.op_sub(sh(shot_x(s)), sh(_cur_item(blocks, "slot x", SLOT_X_ID)))
        hit_y = blocks.op_and(
            blocks.op_not(blocks.op_lt(d_y, number(dy_low))),
            blocks.op_not(blocks.op_gt(d_y, number(dy_high))),
        )
        d_x = blocks.op_sub(sh(_cur_item(blocks, "slot y", SLOT_Y_ID)), sh(shot_y(s)))
        hit_x = blocks.op_and(
            blocks.op_not(blocks.op_lt(d_x, number(dx_low))),
            blocks.op_not(blocks.op_gt(d_x, number(dx_high))),
        )
        overlap = blocks.op_and(
            blocks.op_and(shot_live, enemy_live), blocks.op_and(hit_y, hit_x)
        )
        body.append(
            blocks.if_reporter(
                overlap,
                [
                    blocks.set_var("hit slot", HIT_SLOT_ID, variable("slot index", SLOT_INDEX_ID)),
                    blocks.set_var_expr(
                        "award value",
                        AWARD_VALUE_ID,
                        blocks.list_item(
                            "value table", VALUE_TABLE_ID, _cur_item(blocks, "slot pts", SLOT_PTS_ID)
                        ),
                    ),
                    blocks.call_proc(RESOLVE_HIT_PROCCODE, warp=True),
                    _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
                    blocks.list_replace("slot state", SLOT_STATE_ID, number(s), number(SHOT_SPENT)),
                ],
            )
        )
    blocks.chain(definition, body)


def install_explode_toroid_tick(blocks: Blocks) -> None:
    # WPN-02: advance a struck Toroid's explosion one tick. It keeps drifting on its velocity while the
    # burst plays (the renderer maps the clock to a phase), and is freed once the recorded duration
    # elapses. The clock reuses `slot timer` (reset to 0 by the detector at the hit); movement mirrors
    # `update toroid`'s move so a mid-approach kill still coasts. No cull-window test here — an exploding
    # enemy always frees on its own clock, even if it drifts off-field first.
    definition = _install_warp_proc(blocks, EXPLODE_TICK_PROCCODE)
    move = [
        _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
        _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_add(_cur_item(blocks, "slot y", SLOT_Y_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)))),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP))),
    ]
    done = blocks.op_not(blocks.op_lt(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TOROID_HIT_DURATION_FRAMES)))
    free = blocks.if_reporter(done, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    blocks.chain(definition, [*move, free])


def install_update_bullet(blocks: Blocks) -> None:
    # AIR-12: advance the enemy bullet at `slot index` one tick. It was aimed once at the craft when it
    # was fired (the 32-magnitude tier), so it flies straight: move by velocity, raise `player hit` if it
    # overlaps the craft (PLY-02), and cull off any screen edge. `slot code` pulses the (deferred) colour
    # cycle. No re-aim — a fired bullet does not track.
    definition = _install_warp_proc(blocks, UPDATE_BULLET_PROCCODE)
    move = [
        _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
        _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_add(_cur_item(blocks, "slot y", SLOT_Y_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)))),
    ]
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks), [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))]
    )
    off_bottom = blocks.op_not(blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MAX)))
    off_top = blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MIN + 1))
    off_right = blocks.op_not(blocks.op_lt(_cur_col(blocks), number(CULL_COL_MAX)))
    off_left = blocks.op_lt(_cur_col(blocks), number(CULL_COL_MIN + 1))
    offscreen = blocks.op_or(blocks.op_or(off_bottom, off_top), blocks.op_or(off_right, off_left))
    cull = blocks.if_reporter(offscreen, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    blocks.chain(definition, [*move, craft_hit, cull])


def _fire_toroid_bullet(blocks: Blocks) -> list[str]:
    # AIR-12: the shooting Toroid (type 0x0B) fires one aimed bullet at the moment it commits its swing
    # (once — the reference fires on a timer while level; firing once here is the recorded slice-8
    # simplification, deferring the fire-rate mask to slice 10, so DIF-03.play is not claimed). Allocate
    # an idle bullet slot; on success, place the bullet at the Toroid and aim it at the craft's current
    # cell on the 32-magnitude tier (the reference's generic bullet table). No mask is consulted.
    bindex = lambda: variable("bullet alloc result", BULLET_ALLOC_RESULT_ID)
    got = blocks.op_gt(variable("bullet alloc result", BULLET_ALLOC_RESULT_ID), number(0))
    placed = blocks.if_reporter(
        got,
        [
            blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
            blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
            blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
            blocks.list_replace("slot x", SLOT_X_ID, bindex(), _cur_item(blocks, "slot x", SLOT_X_ID)),
            blocks.list_replace("slot y", SLOT_Y_ID, bindex(), _cur_item(blocks, "slot y", SLOT_Y_ID)),
            blocks.list_replace("slot dx", SLOT_DX_ID, bindex(), blocks.list_item("aim dx 32", AIM_DX_32_ID, variable("aim index", AIM_INDEX_ID))),
            blocks.list_replace("slot dy", SLOT_DY_ID, bindex(), blocks.list_item("aim dy 32", AIM_DY_32_ID, variable("aim index", AIM_INDEX_ID))),
            blocks.list_replace("slot timer", SLOT_TIMER_ID, bindex(), number(0)),
            blocks.list_replace("slot code", SLOT_CODE_ID, bindex(), number(BULLET_INIT_CODE)),
            blocks.list_replace("slot flag", SLOT_FLAG_ID, bindex(), number(0)),
        ],
    )
    return [blocks.call_proc(ALLOC_BULLET_PROCCODE, warp=True), placed]


def install_update_toroid(blocks: Blocks) -> None:
    # AIR-01: advance the Toroid at `slot index` by one tick. Before its swing trigger it approaches
    # on its aimed velocity; when nearly level with the craft laterally (offset in [-2,1]) it commits
    # to a swing toward the craft's side, accelerating sideways and animating; then it moves, and is
    # culled off the play field (its scroll-axis position kept for the refill, per the coded refill).
    definition = _install_warp_proc(blocks, UPDATE_TOROID_PROCCODE)
    flag = lambda: _cur_item(blocks, "slot flag", SLOT_FLAG_ID)
    offset = lambda: blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))

    # Swing trigger (only while approaching): LOW <= offset <= HIGH -> commit a swing by the sign.
    at_or_above_low = blocks.op_not(blocks.op_lt(offset(), number(TOROID_SWING_LOW)))
    at_or_below_high = blocks.op_not(blocks.op_gt(offset(), number(TOROID_SWING_HIGH)))
    in_window = blocks.op_and(at_or_above_low, at_or_below_high)
    side = blocks.add("control_if_else")
    craft_to_right = blocks.op_not(blocks.op_lt(offset(), number(0)))
    blocks.blocks[side]["inputs"]["CONDITION"] = [2, craft_to_right]
    blocks.substack(side, [_set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(TOROID_FLAG_SWING_RIGHT))])
    blocks.substack(side, [_set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(TOROID_FLAG_SWING_LEFT))], name="SUBSTACK2")
    # On the swing commit, a type-0x0B (shooting) Toroid fires one aimed bullet.
    shoots = blocks.if_reporter(
        blocks.op_eq(_cur_item(blocks, "slot type", SLOT_TYPE_ID), number(TOROID_SHOOTS_TYPE)),
        _fire_toroid_bullet(blocks),
    )
    trigger = blocks.if_reporter(
        blocks.op_and(blocks.op_eq(flag(), number(TOROID_FLAG_APPROACH)), in_window),
        [side, shoots],
    )
    # animation phase = floor(timer/2) mod 8.
    anim = lambda: blocks.op_mod(
        blocks.op_floor(blocks.op_div(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(2))), number(8)
    )
    # The swing REVERSES the Toroid's lateral velocity so it peels away from its approach line — the
    # reference's `toroid_toggle_dir`/`toroid_swing_right`/`toroid_swing_left` ($204F-$2091). The Toroid
    # spawns aimed at the craft (`slot dy` points toward it), so nudging `slot dy` AGAINST that heading
    # each tick decelerates the approach, then curves it away — the arcade "swing," not a homing dive.
    # `SWING_RIGHT` is committed when the craft is at the higher column (offset >= 0, so the aimed
    # `slot dy` is positive) and does `slot dy -= accel` (`subq #1,_dY`); `SWING_LEFT` mirrors it with
    # `slot dy += accel` (`addq #1,_dY`). Per-direction opposite animation order matches the reference
    # (right = descending F..8, left = ascending 8..F).
    swing_right = blocks.if_reporter(
        blocks.op_eq(flag(), number(TOROID_FLAG_SWING_RIGHT)),
        [
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.op_sub(_cur_item(blocks, "slot dy", SLOT_DY_ID), number(TOROID_SWING_ACCEL))),
            _set_cur_item(blocks, "slot code", SLOT_CODE_ID, blocks.op_sub(number(15), anim())),
        ],
    )
    swing_left = blocks.if_reporter(
        blocks.op_eq(flag(), number(TOROID_FLAG_SWING_LEFT)),
        [
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.op_add(_cur_item(blocks, "slot dy", SLOT_DY_ID), number(TOROID_SWING_ACCEL))),
            _set_cur_item(blocks, "slot code", SLOT_CODE_ID, blocks.op_add(number(8), anim())),
        ],
    )
    # Move by 4*velocity per tick (2 arcade frames), advance the animation clock, then cull.
    move = [
        _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
        _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_add(_cur_item(blocks, "slot y", SLOT_Y_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)))),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP))),
    ]
    off_bottom = blocks.op_not(blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MAX)))
    off_top = blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MIN + 1))  # row <= -2  ==  row < -1
    off_right = blocks.op_not(blocks.op_lt(_cur_col(blocks), number(CULL_COL_MAX)))
    off_left = blocks.op_lt(_cur_col(blocks), number(CULL_COL_MIN + 1))  # col <= -2 (left edge)
    # The swing sends a Toroid off EITHER lateral edge. The reference culls a left exit via its 8-bit
    # column wrapping past the right threshold; this port uses signed columns, so it needs an explicit
    # left-edge cull too — without it a left-fleeing Toroid never frees its slot and slides off-screen.
    offscreen = blocks.op_or(blocks.op_or(off_bottom, off_top), blocks.op_or(off_right, off_left))
    cull = blocks.if_reporter(offscreen, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    # A struck Toroid (state HIT) runs its explosion instead of the normal update — while exploding it
    # neither hits nor is hit. Otherwise it first offers itself to the shot detector (which may flip it
    # to HIT this tick); the approach/swing/move/cull then runs only if it is still ACTIVE.
    state = lambda: _cur_item(blocks, "slot state", SLOT_STATE_ID)
    # PLY-02: an active flying enemy touching the craft's cell kills it (raises `player hit` for the
    # non-warp walk thread to act on) — checked at the tick-start position, before it moves or culls.
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks), [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))]
    )
    normal = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_ACTIVE)),
        [craft_hit, trigger, swing_right, swing_left, *move, cull],
    )
    top = blocks.add("control_if_else")
    is_hit = blocks.op_eq(state(), number(SLOT_HIT))
    blocks.blocks[top]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = top
    blocks.substack(top, [blocks.call_proc(EXPLODE_TICK_PROCCODE, warp=True)])
    blocks.substack(
        top,
        [blocks.call_proc(CHECK_AIR_HIT_PROCCODE, warp=True), normal],
        name="SUBSTACK2",
    )
    blocks.chain(definition, [top])


def install_cull_slot(blocks: Blocks) -> None:
    # Free the slot at `slot index` (type/state to empty). The position fields are left as-is (like the
    # reference's check_scroll_offscreen 30B4, which clears only type/state/extra); a refilled flying
    # slot no longer inherits that stale position — `init toroid` resets the scroll row to the top and
    # re-draws the lateral column, so every spawn enters cleanly from the top.
    definition = _install_warp_proc(blocks, CULL_SLOT_PROCCODE)
    blocks.chain(
        definition,
        [
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, number(0)),
            _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(0)),
        ],
    )


def install_spawn_flying(blocks: Blocks) -> None:
    # FORM-01 / AREA-02: after the object walk, refill the first `formation count` flying slots from
    # the wave's type run (flying type table at `formation type offset`). Only the types this slice
    # handles (Toroid 0x0A/0x0B) are spawned; other formation-named families are skipped until their
    # slice (recorded deviation). Uses `spawn cursor` as its loop index and `slot index` as the
    # target slot (free here — the walk that owns `slot index` has finished for this tick).
    definition = _install_warp_proc(blocks, SPAWN_FLYING_PROCCODE)
    i = lambda: variable("spawn cursor", SPAWN_CURSOR_ID)
    set_i = blocks.set_var("spawn cursor", SPAWN_CURSOR_ID, number(1))
    loop = blocks.add("control_repeat", inputs={"TIMES": variable("formation count", FORMATION_COUNT_ID)})
    set_slot = blocks.set_var_expr("slot index", SLOT_INDEX_ID, blocks.op_add(number(FLYING_SLOTS[0] - 1), i()))
    empty = blocks.op_eq(_cur_item(blocks, "slot type", SLOT_TYPE_ID), number(0))
    # type-table position = formation type offset + spawn cursor (1-based); guard the list bounds.
    pos = blocks.op_add(variable("formation type offset", FORMATION_TYPE_OFFSET_ID), i())
    in_bounds = blocks.op_and(
        blocks.op_not(blocks.op_lt(pos, number(1))),
        blocks.op_not(blocks.op_gt(blocks.op_add(variable("formation type offset", FORMATION_TYPE_OFFSET_ID), i()), number(len(FLYING_TYPE_CODES)))),
    )
    set_type = blocks.set_var_expr(
        "walk type",
        WALK_TYPE_ID,
        blocks.list_item("flying type table", FLYING_TYPE_TABLE_ID, blocks.op_add(variable("formation type offset", FORMATION_TYPE_OFFSET_ID), i())),
    )
    handled = blocks.op_or(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(TOROID_TYPE)),
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(TOROID_SHOOTS_TYPE)),
    )
    spawn_handled = blocks.if_reporter(handled, [blocks.call_proc(INIT_TOROID_PROCCODE, warp=True)])
    bounds_gate = blocks.if_reporter(in_bounds, [set_type, spawn_handled])
    empty_gate = blocks.if_reporter(empty, [bounds_gate])
    blocks.substack(loop, [set_slot, empty_gate, blocks.change_var("spawn cursor", SPAWN_CURSOR_ID, 1)])
    blocks.chain(definition, [set_i, loop])


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


def _select_formation(blocks: Blocks, index_value: Any) -> list[str]:
    # FORM-01: set the incoming wave's `formation count` + `formation type offset` from the
    # formation table, indexed by `index_value` (a reporter block id OR a value-input spec giving
    # the signed formation index — the record offset for a set-formation record, or the raised,
    # folded AI level for a raise record). The table is two parallel logical lists over index
    # FORMATION_MIN_INDEX..MAX, so the 1-based slot is index - FORMATION_MIN_INDEX + 1. Scratch
    # `item N of list` returns "" (not 0) for N outside 1..len, silently poisoning arithmetic, so
    # the assignment is GUARDED on BOTH bounds: an out-of-domain index leaves the prior formation
    # unchanged (no faithful ROM-adjacent value exists to fabricate). The build-time fixture in
    # tests/test_spec_docs.py proves the real committed schedules never leave the domain under this
    # slice's full dynamics (raises, set-formation, AND DIF-02's un-folded score adjust at its
    # worst-case cap), so the guard is a defensive dead branch; a future schedule/DIP change that
    # broke that margin would redden that fixture, not fail silently here.
    if isinstance(index_value, str):
        set_index = blocks.set_var_expr("formation index", FORMATION_INDEX_ID, index_value)
    else:
        set_index = blocks.set_var("formation index", FORMATION_INDEX_ID, index_value)

    def idx() -> list[Any]:
        return variable("formation index", FORMATION_INDEX_ID)

    def slot() -> str:
        return blocks.op_add(idx(), number(1 - FORMATION_MIN_INDEX))

    in_range = blocks.add("operator_and")
    lower = blocks.op_gt(idx(), number(FORMATION_MIN_INDEX - 1))  # index >= MIN
    upper = blocks.op_gt(number(FORMATION_MIN_INDEX + FORMATION_TABLE_LEN), idx())  # index <= MAX
    blocks.blocks[lower]["parent"] = in_range
    blocks.blocks[upper]["parent"] = in_range
    blocks.blocks[in_range]["inputs"] = {"OPERAND1": [2, lower], "OPERAND2": [2, upper]}
    set_count = blocks.set_var_expr(
        "formation count",
        FORMATION_COUNT_ID,
        blocks.list_item("formation count table", FORMATION_COUNT_TABLE_ID, slot()),
    )
    set_type = blocks.set_var_expr(
        "formation type offset",
        FORMATION_TYPE_OFFSET_ID,
        blocks.list_item("formation type offset table", FORMATION_TYPE_OFFSET_TABLE_ID, slot()),
    )
    guard = blocks.if_reporter(in_range, [set_count, set_type])
    return [set_index, guard]


def _consume_schedule(blocks: Blocks) -> list[str]:
    # AREA-02 ordered dispatch: consume every record at the cursor whose trigger row equals the
    # current scroll row, in order, advancing the cursor. Fire-once is guaranteed by the monotonic
    # cursor over monotonic progress. The loop stops when the record's trigger no longer matches the
    # row OR the cursor passes the area's end index (`cursor > end`, the belt-and-suspenders bound
    # slice 6 relies on so one area never bleeds into the next). The sentinel never fires because
    # the dispatch reads the POST-increment row, which is <= 12 until the wrap and never the area-top
    # row 0x0D. The DIF-01/FORM-01 handlers (raise / set-formation / reset-formation) are wired
    # below; `schedule fired` still counts every record so the fire-once observable is unchanged.
    loop = blocks.add("control_repeat_until")

    def cursor() -> list[Any]:
        return variable("schedule cursor", SCHEDULE_CURSOR_ID)

    def handler_at_cursor() -> str:
        return blocks.list_item("schedule handler", SCHEDULE_HANDLER_ID, cursor())

    def arg_at_cursor() -> str:
        return blocks.list_item("schedule arg", SCHEDULE_ARG_ID, cursor())

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

    # DIF-01 raise: add the cabinet increment to the AI level, fold back once at >= 0x80, then
    # re-select the formation using the new AI level as the table index (no record offset).
    raise_body = [
        blocks.set_var_expr(
            "ai level",
            AI_LEVEL_ID,
            blocks.op_add(
                variable("ai level", AI_LEVEL_ID),
                blocks.list_item(
                    "difficulty increment",
                    DIFFICULTY_INCREMENT_ID,
                    number(DIFFICULTY_DIP_INDEX + 1),
                ),
            ),
        ),
        blocks.if_reporter(
            blocks.op_gt(variable("ai level", AI_LEVEL_ID), number(AI_LEVEL_FOLD_THRESHOLD - 1)),
            [blocks.change_var("ai level", AI_LEVEL_ID, -AI_LEVEL_FOLD_SUBTRACT)],
        ),
        *_select_formation(blocks, variable("ai level", AI_LEVEL_ID)),
    ]
    raise_branch = blocks.if_reporter(
        blocks.op_eq(handler_at_cursor(), text(RAISE_HANDLER)), raise_body
    )
    # DIF-02 score re-tune: add floor(floor(score / 1000) / craft), capped at 16, to the AI level —
    # so a player scoring heavily with craft in reserve meets sharper pressure. Guarded on craft > 0
    # (no divide-by-zero). Unlike the raise, the reference does NOT fold this add back.
    adjust_branch = blocks.if_reporter(
        blocks.op_eq(handler_at_cursor(), text(ADJUST_HANDLER)),
        [
            blocks.if_reporter(
                blocks.op_gt(variable("craft", LIVES_ID), number(0)),
                [
                    blocks.set_var_expr(
                        "ai adjust",
                        AI_ADJUST_ID,
                        blocks.op_floor(
                            blocks.op_div(
                                blocks.op_floor(
                                    blocks.op_div(variable("score", SCORE_ID), number(1000))
                                ),
                                variable("craft", LIVES_ID),
                            )
                        ),
                    ),
                    blocks.if_reporter(
                        blocks.op_gt(variable("ai adjust", AI_ADJUST_ID), number(16)),
                        [blocks.set_var("ai adjust", AI_ADJUST_ID, number(16))],
                    ),
                    blocks.set_var_expr(
                        "ai level",
                        AI_LEVEL_ID,
                        blocks.op_add(
                            variable("ai level", AI_LEVEL_ID),
                            variable("ai adjust", AI_ADJUST_ID),
                        ),
                    ),
                ],
            )
        ],
    )
    # FORM-01 set-formation: the record's signed offset IS the table index (no AI level added).
    set_branch = blocks.if_reporter(
        blocks.op_eq(handler_at_cursor(), text(SET_FORMATION_HANDLER)),
        _select_formation(blocks, arg_at_cursor()),
    )
    # FORM-01 reset-formation: zero the wave between formations.
    reset_branch = blocks.if_reporter(
        blocks.op_eq(handler_at_cursor(), text(RESET_FORMATION_HANDLER)),
        [
            blocks.set_var("formation count", FORMATION_COUNT_ID, number(0)),
            blocks.set_var("formation type offset", FORMATION_TYPE_OFFSET_ID, number(0)),
        ],
    )
    # DIF-03 fire-permission masks: each family's `fire_mask_<family>` record sets that family's mask
    # byte from the schedule arg; the `ground_stop_firing_row` record sets the ground-stop row. The
    # per-family firing that CONSUMES these lands with the enemy slices (8+).
    mask_branches = [
        blocks.if_reporter(
            blocks.op_eq(handler_at_cursor(), text(FIRE_MASK_PREFIX + suffix)),
            [blocks.set_var_expr(name, mask_id, arg_at_cursor())],
        )
        for suffix, name, mask_id in FIRE_MASK_FAMILIES
    ]
    ground_stop_branch = blocks.if_reporter(
        blocks.op_eq(handler_at_cursor(), text(GROUND_STOP_FIRING_HANDLER)),
        [blocks.set_var_expr("ground stop firing row", GROUND_STOP_FIRING_ROW_ID, arg_at_cursor())],
    )
    # ENGINE-TODO: the spawn / boss handler dispatch (add_ground_object, add_domogram_with_path,
    # add_object, *bacura*, andor_genesis_*, sheonite_*) lands with the enemy slices (8+). The
    # DIF/FORM handlers (raise, adjust, set/reset formation, the 8 fire masks, ground-stop) are all
    # wired above; the still-unhandled spawn/boss records advance the cursor and count the fire only.
    blocks.substack(
        loop,
        [
            raise_branch,
            adjust_branch,
            set_branch,
            reset_branch,
            *mask_branches,
            ground_stop_branch,
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
    install_compute_aim_index(blocks)
    install_read_player_cell(blocks)
    install_init_toroid(blocks)
    install_check_air_hit(blocks)
    install_explode_toroid_tick(blocks)
    install_update_bullet(blocks)
    install_update_toroid(blocks)
    install_cull_slot(blocks)
    install_advance_slots(blocks)
    install_spawn_flying(blocks)
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

    # (The D/G debug death keys are retired in slice 8: a real attacker now kills the craft — a flying
    # enemy or an enemy bullet touching the craft's cell raises `player hit`, and the walk thread runs
    # the player-dead transition, spending a craft. The death-complete handler still decides respawn vs
    # game-over from the craft counter. The debug S scoring fixture is likewise retired: the
    # blaster-to-air hit now produces `award value` from the struck enemy's `slot pts`.)

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

    # SYS-04 / AREA-01 centralized ordered update: a second `director enter` thread (parallel to the
    # BGM loop above) drives one atomic pass per tick while playing, in the reference's frame order —
    # read the craft's cell (so slots aim/collide against one cached position), advance the area
    # schedule, walk and dispatch the object slots, then refill flying slots from the formation.
    walk_enter = blocks.receive("director enter")
    walk_loop = blocks.add("control_repeat_until")
    walk_condition = blocks.not_state(walk_loop, "playing")
    blocks.blocks[walk_loop]["inputs"]["CONDITION"] = [2, walk_condition]
    # PLY-02: the warp walk only RAISES `player hit` (an enemy or bullet touched the craft's cell); the
    # death is triggered here, in the non-warp thread, as the loop's terminal statement — clear the flag,
    # spend a craft, and run the player-dead transition (the exact body the retired D key used). The
    # death-complete handler still decides respawn vs game-over from the craft counter.
    death_check = blocks.if_reporter(
        blocks.op_and(
            blocks.op_eq(variable("player hit", PLAYER_HIT_ID), number(1)),
            blocks.op_eq(variable("invuln", INVULN_ID), number(0)),
        ),
        [
            blocks.set_var("player hit", PLAYER_HIT_ID, number(0)),
            blocks.change_var("craft", LIVES_ID, -1),
            blocks.send("craft changed"),
            blocks.call_transition("player-dead", "none"),
        ],
    )
    blocks.substack(
        walk_loop,
        [
            blocks.call_proc(READ_PLAYER_PROCCODE, warp=True),
            blocks.call_proc(ADVANCE_AREA_PROCCODE, warp=True),
            blocks.call_proc(ADVANCE_SLOTS_PROCCODE, warp=True),
            blocks.call_proc(SPAWN_FLYING_PROCCODE, warp=True),
            death_check,
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

    # DIF-01 / FORM-01 difficulty-director reset — its OWN `director reset` receiver (like the eight
    # existing per-concern receivers, each branching on its own scope). Per the spec, the AI level,
    # formation, and fire masks are per-player game state: they PERSIST across death/respawn and
    # reset only for a new game — so this fires on cold-start / new-game only. It touches only the
    # difficulty vars (disjoint from every other receiver's), so the unordered same-target hat
    # execution is safe. (`formation index` is a transient lookup register, not reset here.)
    difficulty_reset = blocks.receive("director reset")
    blocks.chain(
        difficulty_reset,
        [
            reset_if(
                blocks,
                ("cold-start", "new-game"),
                [
                    blocks.set_var("ai level", AI_LEVEL_ID, number(0)),
                    blocks.set_var("formation count", FORMATION_COUNT_ID, number(0)),
                    blocks.set_var("formation type offset", FORMATION_TYPE_OFFSET_ID, number(0)),
                    blocks.set_var("ground stop firing row", GROUND_STOP_FIRING_ROW_ID, number(0)),
                    *(
                        blocks.set_var(name, mask_id, number(0))
                        for _suffix, name, mask_id in FIRE_MASK_FAMILIES
                    ),
                ],
            )
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
    # sweep over the dedicated slots (19 of them, vs the shot allocator's 3). LIVE this slice:
    # the shooting Toroid (`_fire_toroid_bullet`) calls it. Its result var is its own, and so
    # is its cursor (`bullet-cursor`, not the shared `slot index`) — so the firer can call this
    # from inside the `advance slots` sweep without corrupting the outer walk.
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
    # B8 top-expiry OR the walk marking this shot spent (WPN-02: on a resolved air hit the detector
    # sets the shot slot's state off ACTIVE; the clone sees it next iteration, frees its slot, and
    # deletes — so the slot is freed by the clone, never reallocated under a still-live clone).
    at_top = blocks.touching(travel, "frame_t")
    spent = blocks.op_not(
        blocks.op_eq(
            blocks.list_item("slot state", SLOT_STATE_ID, variable("clone slot", CLONE_SLOT_ID)),
            number(SLOT_ACTIVE),
        )
    )
    blocks.blocks[travel]["inputs"]["CONDITION"] = [2, blocks.op_or(at_top, spent)]
    # WPN-02 position mirror: each iteration write the shot's live cell (stage px -> slot units, the
    # render map inverted and floored) into its slot x/y, so the walk's shot-vs-air detector reads the
    # shot from the slot lists like any entity. One-tick lag vs the clone's pixel position (<=10 arcade
    # px at 6 px/frame), recorded in docs/mechanics/025.
    mirror_x = blocks.list_replace(
        "slot x",
        SLOT_X_ID,
        variable("clone slot", CLONE_SLOT_ID),
        blocks.op_floor(
            blocks.op_div(
                blocks.op_mul(
                    blocks.op_sub(number(RENDER_ROW_TOP), blocks.yposition()),
                    number(SLOT_UNITS_PER_CELL),
                ),
                number(RENDER_ROW_STAGE),
            )
        ),
    )
    mirror_y = blocks.list_replace(
        "slot y",
        SLOT_Y_ID,
        variable("clone slot", CLONE_SLOT_ID),
        blocks.op_floor(
            blocks.op_div(
                blocks.op_mul(
                    blocks.op_add(blocks.xposition(), number(RENDER_COL_OFFSET)),
                    number(SLOT_UNITS_PER_CELL),
                ),
                number(RENDER_COL_STAGE),
            )
        ),
    )
    blocks.substack(
        travel,
        [
            mirror_x,
            mirror_y,
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


def toroid_blocks() -> dict[str, dict[str, Any]]:
    # AIR-01 Toroid renderer (game_director owns these blocks; sprite_extractor owns the costumes).
    # One persistent clone per flying slot (59..64), spawned on director enter while playing and
    # cleared on director stop (common_stop clones=True). Each clone is a pure per-tick function of
    # its slot's live state: shown, positioned (arcade cell -> stage px), and costumed by the sprite
    # code when the slot holds a Toroid; hidden when the slot is empty. The clone writes no state.
    blocks = Blocks(TOROID_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("toroid clone slot", TOROID_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(FLYING_SLOTS[0], FLYING_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("toroid clone slot", TOROID_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_toroid = blocks.op_or(
        blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(TOROID_TYPE)),
        blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(TOROID_SHOOTS_TYPE)),
    )
    stage_x = blocks.op_sub(
        blocks.op_mul(
            blocks.op_div(blocks.list_item("slot y", SLOT_Y_ID, slotvar()), number(SLOT_UNITS_PER_CELL)),
            number(RENDER_COL_STAGE),
        ),
        number(RENDER_COL_OFFSET),
    )
    stage_y = blocks.op_sub(
        number(RENDER_ROW_TOP),
        blocks.op_mul(
            blocks.op_div(blocks.list_item("slot x", SLOT_X_ID, slotvar()), number(SLOT_UNITS_PER_CELL)),
            number(RENDER_ROW_STAGE),
        ),
    )
    turn_ordinal = blocks.list_item(
        "toroid frame",
        TOROID_FRAME_ID,
        blocks.op_sub(blocks.list_item("slot code", SLOT_CODE_ID, slotvar()), number(TOROID_INIT_CODE - 1)),
    )
    # WPN-02 explosion frames: while the slot is HIT, the clock (slot timer) selects an explosion phase,
    # which maps to the referenced explode costumes appended after the 7 turn frames (ordinal 8..). The
    # burst doubles size at TOROID_BIG_PHASE (the arcade frame-8 2x). The exact one-cell recentre of the
    # doubled frame is deferred with dedicated Toroid-burst crops (record 025); the stand-in centres on
    # the slot.
    phase_for_costume = blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
    )
    explode_ordinal = blocks.op_add(number(TOROID_TURN_FRAME_COUNT + 1), phase_for_costume)
    phase_for_size = blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
    )
    size_branch = blocks.add("control_if_else")
    is_big = blocks.op_eq(phase_for_size, number(TOROID_BIG_PHASE))
    blocks.blocks[size_branch]["inputs"]["CONDITION"] = [2, is_big]
    blocks.blocks[is_big]["parent"] = size_branch
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(TOROID_EXPLODE_SIZE)})])
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(TOROID_RENDER_SIZE)})], name="SUBSTACK2")
    state_render = blocks.add("control_if_else")
    is_hit = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT))
    blocks.blocks[state_render]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = state_render
    blocks.substack(state_render, [blocks.switch_costume_expr(explode_ordinal), size_branch])
    blocks.substack(
        state_render,
        [
            blocks.switch_costume_expr(turn_ordinal),
            blocks.add("looks_setsizeto", inputs={"SIZE": number(TOROID_RENDER_SIZE)}),
        ],
        name="SUBSTACK2",
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_toroid]
    blocks.blocks[is_toroid]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.go_expr(stage_x, stage_y),
            state_render,
            blocks.to_front(),
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(clone, [blocks.hide(), loop])
    return blocks.blocks


def enemy_bullet_blocks() -> dict[str, dict[str, Any]]:
    # AIR-12 enemy-bullet renderer (game_director owns the blocks; the costumes are the stand-in frames
    # mirrored on in expected_project). One persistent clone per bullet slot (40..58), created on
    # director enter while playing and cleared on stop. Each clone shows a small sprite at its slot's
    # mapped position when the slot holds a bullet, else hides. Writes no state (the walk owns the slot).
    blocks = Blocks(ENEMY_BULLET_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("enemy bullet clone slot", ENEMY_BULLET_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(BULLET_SLOTS[0], BULLET_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("enemy bullet clone slot", ENEMY_BULLET_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_bullet = blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(BULLET_TYPE))
    stage_x = blocks.op_sub(
        blocks.op_mul(
            blocks.op_div(blocks.list_item("slot y", SLOT_Y_ID, slotvar()), number(SLOT_UNITS_PER_CELL)),
            number(RENDER_COL_STAGE),
        ),
        number(RENDER_COL_OFFSET),
    )
    stage_y = blocks.op_sub(
        number(RENDER_ROW_TOP),
        blocks.op_mul(
            blocks.op_div(blocks.list_item("slot x", SLOT_X_ID, slotvar()), number(SLOT_UNITS_PER_CELL)),
            number(RENDER_ROW_STAGE),
        ),
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_bullet]
    blocks.blocks[is_bullet]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.go_expr(stage_x, stage_y),
            blocks.switch_costume("toroid/turn/01"),  # stand-in: the first mirrored frame, drawn small
            blocks.add("looks_setsizeto", inputs={"SIZE": number(ENEMY_BULLET_RENDER_SIZE)}),
            blocks.to_front(),
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(clone, [blocks.hide(), loop])
    return blocks.blocks


def _ensure_gameplay_target(project: dict[str, Any], name: str) -> None:
    """Create a gameplay sprite target's EXISTENCE (blocks come from the replacements map) if it is
    absent, else leave it untouched — the same create-if-absent, costumes-owned-elsewhere split as
    `_ensure_hud_target`, generalized. Order-independent: whether this module or the costume generator
    (tools/sprite_extractor.py) runs first, each preserves the other's field (arch review 3a)."""
    existing = next(
        (target for target in project["targets"] if target.get("name") == name),
        None,
    )
    if existing is not None:
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
    project["targets"].insert(
        insertion,
        {
            "isStage": False,
            "name": name,
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
        },
    )


def expected_project(project: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(project)
    _ensure_hud_target(result)
    _ensure_gameplay_target(result, TOROID_TARGET)
    _ensure_gameplay_target(result, ENEMY_BULLET_TARGET)
    # AIR-01: mirror the proof target's verified turn costumes onto the gameplay toroid target (by
    # md5 reference — the same committed asset files, already provenance-recorded). Idempotent, so the
    # two stay in sync; a no-op when the proof costumes are absent (generation runs both to a fixpoint).
    # AIR-01 turn frames first (costume ordinals 1..7), then the WPN-02 explosion frames appended by
    # reference from solv_death (ordinals 8..15) — the recorded stand-in burst (record 025). Both are
    # already-verified, provenance-recorded assets; a no-op when either source is absent (fixpoint).
    proof = next((t for t in result["targets"] if t.get("name") == TOROID_PROOF_TARGET), None)
    death = next((t for t in result["targets"] if t.get("name") == "solv_death"), None)
    toroid = next((t for t in result["targets"] if t.get("name") == TOROID_TARGET), None)
    if proof is not None and toroid is not None:
        toroid["costumes"] = copy.deepcopy(proof["costumes"])
        if death is not None:
            toroid["costumes"].extend(copy.deepcopy(death["costumes"]))
        toroid["currentCostume"] = 0
    # AIR-12: the enemy-bullet renderer uses a small stand-in — the same verified turn frames by
    # reference, drawn at a small size (dedicated bullet crops + the 4-colour pulse deferred, record 026).
    enemy_bullet = next((t for t in result["targets"] if t.get("name") == ENEMY_BULLET_TARGET), None)
    if proof is not None and enemy_bullet is not None:
        enemy_bullet["costumes"] = copy.deepcopy(proof["costumes"])
        enemy_bullet["currentCostume"] = 0
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
        AI_LEVEL_ID,
        FORMATION_COUNT_ID,
        FORMATION_TYPE_OFFSET_ID,
        FORMATION_INDEX_ID,
        AI_ADJUST_ID,
        GROUND_STOP_FIRING_ROW_ID,
        *(mask_id for _suffix, _name, mask_id in FIRE_MASK_FAMILIES),
        # AIR-01 Toroid live-combat machinery (slice 8): the aim quantizer's working vars, the
        # cached craft cell, and the spawner's cursor/attempt/found/type registers.
        AIM_DX_DIFF_ID,
        AIM_DY_DIFF_ID,
        AIM_LARGE_ID,
        AIM_SMALL_ID,
        AIM_SWAP_ID,
        AIM_BASE_ID,
        AIM_FINE_ID,
        AIM_INDEX_ID,
        PLAYER_ROW_ID,
        PLAYER_COL_ID,
        SPAWN_CURSOR_ID,
        SPAWN_ATTEMPTS_ID,
        SPAWN_FOUND_ID,
        WALK_TYPE_ID,
        PLAYER_HIT_ID,
        INVULN_ID,
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
        # DIF-01 / FORM-01 difficulty-director state (Stage-written, sprite-read, write-forbidden,
        # like the area state): the accumulating AI level, and the incoming wave's size + type-table
        # offset the slice-8 spawner will read. `formation index` is the transient lookup register
        # (machinery). All reset to 0 on a world reset; they persist across death/respawn.
        AI_LEVEL_ID: ["ai level", 0],
        FORMATION_COUNT_ID: ["formation count", 0],
        FORMATION_TYPE_OFFSET_ID: ["formation type offset", 0],
        FORMATION_INDEX_ID: ["formation index", 0],
        # DIF-02 transient score re-tune addend (machinery, like `formation index`).
        AI_ADJUST_ID: ["ai adjust", 0],
        # DIF-03 per-family fire-permission masks + the ground-stop-firing row (difficulty-director
        # state, Stage-written, sprite-read, write-forbidden). Set by the schedule; consumed by the
        # enemy slices (8+). All reset to 0 on a world reset, alongside the AI level and formation.
        GROUND_STOP_FIRING_ROW_ID: ["ground stop firing row", 0],
        **{mask_id: [name, 0] for _suffix, name, mask_id in FIRE_MASK_FAMILIES},
        # AIR-01 Toroid live-combat machinery (slice 8). The aim quantizer intermediates, the cached
        # craft cell (player row/col), and the spawner's registers — all transient, all default 0.
        AIM_DX_DIFF_ID: ["aim dx diff", 0],
        AIM_DY_DIFF_ID: ["aim dy diff", 0],
        AIM_LARGE_ID: ["aim large", 0],
        AIM_SMALL_ID: ["aim small", 0],
        AIM_SWAP_ID: ["aim swap", 0],
        AIM_BASE_ID: ["aim base", 0],
        AIM_FINE_ID: ["aim fine", 0],
        AIM_INDEX_ID: ["aim index", 0],
        PLAYER_ROW_ID: ["player row", 0],
        PLAYER_COL_ID: ["player col", 0],
        SPAWN_CURSOR_ID: ["spawn cursor", 0],
        SPAWN_ATTEMPTS_ID: ["spawn attempts", 0],
        SPAWN_FOUND_ID: ["spawn found", 0],
        WALK_TYPE_ID: ["walk type", 0],
        # PLY-02 (slice 8): raised by the walk when an enemy/bullet touches the craft's cell, cleared
        # by the non-warp walk thread that triggers the death.
        PLAYER_HIT_ID: ["player hit", 0],
        # Debug/test invulnerability seam (default 0; the harness sets it, never game logic).
        INVULN_ID: ["invuln", 0],
    }
    owned_lists = {
        ALLOWED_ID,
        SLOT_TYPE_ID,
        SLOT_STATE_ID,
        *(list_id for list_id, _name in SLOT_FIELD_LISTS),
        OCTANT_TABLE_ID,
        AIM_DY_24_ID,
        AIM_DX_24_ID,
        AIM_DY_32_ID,
        AIM_DX_32_ID,
        FLYING_TYPE_TABLE_ID,
        TOROID_FRAME_ID,
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
        SCHEDULE_ARG_ID,
        DIFFICULTY_INCREMENT_ID,
        FORMATION_COUNT_TABLE_ID,
        FORMATION_TYPE_OFFSET_TABLE_ID,
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
        # SYS-02 per-slot position/motion fields, all zeroed at generation (a slot is initialized
        # on allocation). `clear slots` re-zeroes them on every reset; a structural test pins that.
        **{list_id: [name, [0] * SLOT_COUNT] for list_id, name in SLOT_FIELD_LISTS},
        # AIR-01/AIR-12 homing-aim tables (aiming.json), baked as dormant read-only data this slice.
        # The octant quantizer table and the two speed tiers Toroid uses (24 = approach, 32 = bullet).
        OCTANT_TABLE_ID: ["octant table", list(OCTANT_TABLE)],
        AIM_DY_24_ID: ["aim dy 24", list(AIM_DY_24)],
        AIM_DX_24_ID: ["aim dx 24", list(AIM_DX_24)],
        AIM_DY_32_ID: ["aim dy 32", list(AIM_DY_32)],
        AIM_DX_32_ID: ["aim dx 32", list(AIM_DX_32)],
        # AIR-01/FORM-01 flying-enemy type table (object-types.json): the spawner reads the wave's
        # type codes at `formation type offset`. And the Toroid costume-ordinal map (sprite code
        # 8..15 -> costume 1..7, the 8th reusing 6): read-only render data.
        FLYING_TYPE_TABLE_ID: ["flying type table", list(FLYING_TYPE_CODES)],
        TOROID_FRAME_ID: ["toroid frame", list(TOROID_FRAME_MAP)],
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
        # DIF-01/03 + FORM-01: the 4th parallel schedule column — the one runtime-readable scalar
        # each dispatched record needs (set-formation offset / fire-mask byte / ground-stop row; 0
        # otherwise), pre-decoded from the opaque payload. Same length as the other three columns.
        SCHEDULE_ARG_ID: ["schedule arg", list(SCHEDULE_ARGS)],
        AREA_SCHEDULE_START_ID: ["area schedule start", list(AREA_SCHEDULE_START)],
        AREA_SCHEDULE_END_ID: ["area schedule end", list(AREA_SCHEDULE_END)],
        # DIF-01 cabinet AI-level increments [2,0,6,16] (difficulty.json), indexed by the DIP.
        DIFFICULTY_INCREMENT_ID: ["difficulty increment", list(DIFFICULTY_INCREMENTS)],
        # FORM-01 normal flying-formation table (formations.json), decoded to logical entries
        # index -32..127, split into two parallel lists: wave size and type-table offset. Read-only
        # authority, indexed at runtime by the folded AI level (raise) or the record offset (set).
        FORMATION_COUNT_TABLE_ID: ["formation count table", list(FORMATION_COUNTS)],
        FORMATION_TYPE_OFFSET_TABLE_ID: ["formation type offset table", list(FORMATION_TYPE_OFFSETS)],
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
        "toroid": toroid_blocks(),
        "enemy_bullet": enemy_bullet_blocks(),
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
        elif target["name"] == TOROID_TARGET:
            # AIR-01: the only toroid state is sprite-local — which flying slot each clone renders,
            # snapshotted at creation. All entity state lives in the Stage slot lists the clone reads.
            target["variables"] = target["variables"] | {
                TOROID_CLONE_SLOT_ID: ["toroid clone slot", 0],
            }
        elif target["name"] == ENEMY_BULLET_TARGET:
            # AIR-12: likewise, the only enemy-bullet render state is which bullet slot each clone draws.
            target["variables"] = target["variables"] | {
                ENEMY_BULLET_CLONE_SLOT_ID: ["enemy bullet clone slot", 0],
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
