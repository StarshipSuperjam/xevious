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
# The in-flight bomb's accelerating scroll-axis velocity (init_bombing $188C `_dX`). A Stage
# variable for the same reason as the guard: the walk thread writes it and the bomb renderer reads
# it (to pick its falling frame). Cleared to 0 on every reset scope alongside the guard.
BOMB_DX_ID = "weapon-bomb-dx"
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
# AIR-06 fire-permission per-slot fields (the reference's per-object `_FFREQ`/`_TIMER`), shared
# infrastructure for every firing family: `slot fire mask` is the family's fire-permission mask
# captured at spawn; `slot fire timer` is the per-slot fire countdown the shared gate decrements.
# Both stay 0 for non-firing occupants (clear-slots zeroes them), so they are inert unless a family
# writes them at spawn.
SLOT_FIRE_MASK_ID = "slot-fire-mask"  # captured fire-permission mask (_FFREQ)
SLOT_FIRE_TIMER_ID = "slot-fire-timer"  # per-slot fire countdown byte (_TIMER)
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
    (SLOT_FIRE_MASK_ID, "slot fire mask"),
    (SLOT_FIRE_TIMER_ID, "slot fire timer"),
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
# GND (ground.barra #70): the Garu Barra's indestructible 2x2 base is stamped this NON-ACTIVE state
# sentinel so the bomb-vs-ground detector's `==SLOT_ACTIVE` gate excludes it for free (no collision-group
# edit) — exactly the arcade's `_STATE==3` on the outer section (handle_20_Garu_Barra $1A89, "can't
# destroy outer section"). Numerically equal to SHOT_SPENT, but in the disjoint ground-slot band, and it
# mirrors the arcade's own reuse of state 3 for an inert object. The renderer draws the base off `slot
# type` (== GARU_BARRA_TYPE) with this sentinel state, never off `==SLOT_ACTIVE`.
SLOT_GARU_BASE = 3
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
# WPN-02 player-shot vs flying-enemy window. The reference's `check_shot_hit_flying_enemy` ($19A6)
# uses `sub #16; add #32` (Y) and `sub #8; add #16` (X) → shotY-enemyY in [-16,15], enemyX-shotX in
# [-8,7] half-pixel shadow units. This port DOUBLES that to (32,64,16,32) — a deliberate, recorded
# deviation (playtest-driven) for two reasons the reference didn't face:
#   1. TUNNELING. The blaster shot travels `changeyby 20` = 20 stage-px/frame ÷ RENDER_ROW_STAGE(8) =
#      2.5 cells/frame, while the reference window is only 2 cells tall — so the per-frame step
#      overshoots the window and shots skip clean over a Toroid (every shot in a held stream shares
#      the craft-row sampling phase, so a Toroid in a gap is immune to the whole stream: the operator
#      saw "multiple rounds into a group and nothing happens"). The reference never tunnels because
#      its shot speed and window are balanced at the arcade's finer step; our DY was a preserved-
#      baseline the movement slice never reconciled. A 4-cell-tall window (64 shadow) exceeds the
#      2.5-cell step (with margin for the enemy's own closing motion), so every crossing is sampled.
#   2. SPRITE MATCH. The Toroid renders as a 36-px sprite (16-px costume at size 225); the reference
#      window covered ~the central 40% of that, so bullets visibly overlapping the sprite missed.
#      The doubled window (±2 cells Y ≈ 32 px, ±1 cell X ≈ 30 px) matches the rendered body, so a
#      shot touching the Toroid kills it — the arcade "mow-down" feel. The tight craft HURTBOX
#      (HIT_WINDOW_BULLET_FLYING, single cell) is intentionally NOT widened: forgiving offence,
#      precise defence.
HIT_WINDOW_SHOT_FLYING = (32, 64, 16, 32)
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
# GND-05 bomb-vs-ground window (docs/spec/ground-objects.md), in the same half-pixel shadow units,
# as (y_bias, y_width, x_bias, x_width). The reference's `check_object_on_target` ($1A3D) reads both
# the bomb target (slot 0x20) and each object from `sprite_shadow_msb` and range-checks the delta on
# each axis via the carry idiom: scroll axis (spriteY) `sub #10; add #20` -> target-obj in [-10, 9];
# lateral axis (spriteX) `subq #5; add #10` -> obj-target in [-5, 4]. Kept at the reference size (NOT
# widened like the shot window): a bomb is a placed strike, not a fast-stepping projectile, so it
# never tunnels, and the arcade footprint is the faithful feel.
HIT_WINDOW_BOMB_GROUND = (10, 20, 5, 10)

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
# GND dispatch: an add_ground_object record needs THREE runtime scalars the single `schedule arg`
# column cannot carry, so they ride three more parallel schedule columns — object type (the ground
# dispatch discriminator), slot (0-15), and sprite_y (0-255, the lateral field position). Every
# non-ground row fills 0 in all three; those fillers are inert because the ground columns are read
# only when the record's handler is add_ground_object. Populated here, once and for all, so the
# columns are complete for the ground dispatch that consumes them.
GROUND_OBJECT_TYPE_ID = "area-schedule-ground-type"
GROUND_OBJECT_SLOT_ID = "area-schedule-ground-slot"
GROUND_OBJECT_SPRITE_Y_ID = "area-schedule-ground-sprite-y"
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
ADD_GROUND_OBJECT_HANDLER = "add_ground_object"

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
# The Logram fire-mask display name + id, captured into a spawned Logram's `slot fire mask` at dispatch
# (GND #69) and consumed by its aimed-shot gate (Commit 7). Derived from FIRE_MASK_FAMILIES so the two
# never drift if a family's id is renamed.
FIRE_MASK_LOGRAM_NAME = next(n for s, n, i in FIRE_MASK_FAMILIES if s == "logram")
FIRE_MASK_LOGRAM_ID = next(i for s, n, i in FIRE_MASK_FAMILIES if s == "logram")

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
# the reference's cpy_dY_dX_to_obj — the extractor records the byte-order there). The baked tiers:
# the 24-magnitude table (the Toroid's 1.5 px/frame approach), the 32-magnitude generic table (aimed
# bullets at 2 px/frame), and the 48-magnitude terrazi/torkan table (Terrazi's 3 px/frame approach,
# `angle_dX_dY_terrazi_torkan_tbl` 6325-6357). The 33-entry `octant table` is the quantizer's lookup
# (get_index_for_angle). The 48 tier is read by install_init_terrazi to aim the Terrazi — like the
# hit-window constants, baked so the consumer just reads it.
OCTANT_TABLE_ID = "octant-table"
AIM_DY_24_ID = "aim-dy-24"  # Toroid approach tier (magnitude 24 = 1.5 px/frame)
AIM_DX_24_ID = "aim-dx-24"
AIM_DY_32_ID = "aim-dy-32"  # aimed-bullet / generic tier (magnitude 32 = 2 px/frame)
AIM_DX_32_ID = "aim-dx-32"
AIM_DY_48_ID = "aim-dy-48"  # Terrazi/Torkan approach tier (magnitude 48 = 3 px/frame)
AIM_DX_48_ID = "aim-dx-48"


def _load_aiming_tables() -> dict[str, list[int]]:
    data = _load_spec_data("aiming.json")["aiming"]
    tables = {"octant": list(data["octant_table"]["values"])}
    for tier in ("toroid", "generic", "terrazi_torkan"):
        vectors = data["angle_tables"][tier]["vectors"]
        tables[f"{tier}_dy"] = [v["dy"] for v in vectors]
        tables[f"{tier}_dx"] = [v["dx"] for v in vectors]
    return tables


_AIMING = _load_aiming_tables()
OCTANT_TABLE = _AIMING["octant"]
AIM_DY_24, AIM_DX_24 = _AIMING["toroid_dy"], _AIMING["toroid_dx"]
AIM_DY_32, AIM_DX_32 = _AIMING["generic_dy"], _AIMING["generic_dx"]
AIM_DY_48, AIM_DX_48 = _AIMING["terrazi_torkan_dy"], _AIMING["terrazi_torkan_dx"]

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
# GND-05: the bomb-vs-ground overlap detector. Unlike the air detector (called once per active
# flying slot with `slot index` set), this sweeps the 16 ground slots INTERNALLY against the fixed
# bomb-target slot, mirroring the reference's `handle_bombed_obj_and_award_points` ($19EE) which
# loops all 16 objects when a bomb finishes. It has no caller this commit (Commit 4 wires it from
# the bomb-finish); the harness proves it by calling it directly.
CHECK_GROUND_HIT_PROCCODE = "check ground hit"
# WPN-04 player-ground-targeting (#67): the sight that leads the craft every tick (update_crosshair
# $16E8) and the one-tick bomb weapon step — arm on a fresh press, otherwise fly the in-flight bomb
# and, when it lands, resolve the ground objects under it (init_bombing $188C, check_bomb_finished
# $190B). Both are Stage warp procs run from the walk thread; the crosshair/bomb-target/bomb sprites
# are pure renderers of their slots.
TRACK_CROSSHAIR_PROCCODE = "track crosshair"
ADVANCE_BOMB_PROCCODE = "advance bomb"
# GND (area.ground-dispatch #69): the per-tick update for a spawned ground object — the first
# TERRAIN-LOCKED scroller. Unlike a flying enemy (which moves by its own velocity), a ground object
# advances its scroll-axis position by AREA_PROGRESS_STEP each tick (scroll_sprite_X $30E8: the terrain
# scroll_delta doubled, +16 units/arcade-frame = +32/tick) and is culled once it scrolls off the bottom
# of the field. Per-family behaviour (Barra crater, Logram open/fire) layers on this in later commits.
ADVANCE_GROUND_PROCCODE = "advance ground"
# GND (ground.barra #70): the per-tick update for a Barra — the passive terrain target. It scrolls while
# ACTIVE and, once bombed (state HIT), advances the crater/explosion clock (slot timer) while continuing
# to scroll, converting to a persistent crater. Mirrors the flying families' per-family `update <family>`
# split (an active/hit control_if_else), calling the shared `advance ground` scroller for the motion.
UPDATE_BARRA_PROCCODE = "update barra"
# GND (ground.barra #70): the per-tick update for a Garu Barra — a two-slot object. Both the
# indestructible 2x2 base (state sentinel SLOT_GARU_BASE) and the destructible node (state ACTIVE)
# scroll with the terrain; the ONLY per-state difference is the node's death: when the node is bombed
# (state HIT) it plays the SHORTER explode-and-remove burst (explode_and_remove_object $3216: a 4-frame
# phase, 7 frames, then remove) and VANISHES — no crater, unlike the Barra. The base is never hit (the
# detector's ==ACTIVE gate rejects the sentinel), so it only ever scrolls until it culls off-field.
UPDATE_GARU_PROCCODE = "update garu"
# GND (ground.logram #71): the per-tick update for a Logram — the dome that opens, fires ONE aimed shot at
# full-open, and closes on a masked-random cycle (handle_logram_main $1B64). Like the Barra it is
# terrain-locked and, once bombed (state HIT), craters PERSISTENTLY via handle_bomb_explosion — the SAME
# routine the Barra uses, NOT the Garu node's explode-and-remove — so its HIT branch is the Barra's. Its
# ACTIVE behaviour is a two-phase fire timer (a wait countdown, then an open/close count-up with a single
# shot at the midpoint); both states share the terrain scroll + off-field cull of `advance ground`.
UPDATE_LOGRAM_PROCCODE = "update logram"
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
INIT_TERRAZI_PROCCODE = "init terrazi"
UPDATE_TERRAZI_PROCCODE = "update terrazi"
INIT_KAPI_PROCCODE = "init kapi"
UPDATE_KAPI_PROCCODE = "update kapi"
INIT_TORKAN_PROCCODE = "init torkan"
UPDATE_TORKAN_PROCCODE = "update torkan"
FIRE_GATE_PROCCODE = "fire permission gate"  # the shared, family-agnostic periodic-fire gate
CULL_SLOT_PROCCODE = "cull slot"
# DEBUG (temporary playtest tool, tracked for removal): while the debug key is held, force the flying
# formation to a Terrazi wave so a family that only spawns at high AI levels is reachable for a
# playtest. Amends the locked control mapping (needs guardrail-ack). See docs/spec/core-game-systems.md
# and the removal issue #119 (remove once all aerial families are built and playtested).
DEBUG_SPAWN_PROCCODE = "debug spawn wave"
DEBUG_SPAWN_KEY = "t"  # T = cycle a single debug enemy through the buildable families
DEBUG_SPAWN_INDEX_ID = "debug-spawn-index"  # which DEBUG_SPAWN_FAMILIES entry T brings in next
# The flying-type-table offset whose 6-slot run is all Terrazi (0x11) — the game's own Terrazi
# formation offset (formation_table indices 110-115); the spawner reads positions offset+1..offset+6.
TERRAZI_FORMATION_OFFSET = 78
# The flying-type-table offset whose 6-slot run is all Kapi (0x10) — code 16 sits at 0-based positions
# 69-74 (object-types.json), the same six-wide derivation as the Terrazi offset. Used by the debug
# spawner to force a Kapi wave.
KAPI_FORMATION_OFFSET = 69
# The flying-type-table offset whose 6-slot run is all Torkan (0x0F) — code 15 sits at 0-based positions
# 25-30 (object-types.json), the same six-wide derivation as the Kapi/Terrazi offsets. The debug key
# forces THIS all-Torkan run; the natural area-1 waves reach Torkan through the AI-level formation table
# instead (other offsets), so a built Torkan still appears in normal area-1 play at standard difficulty
# — not only via the debug key. See docs/mechanics/029 deviation 7 for the schedule trace.
TORKAN_FORMATION_OFFSET = 25
# The flying-type-table offsets whose runs select each Zoshi type (object-types.json 0-based starts):
# rnd (0x0C) at 31-36 and top (0x0D) at 45-50 are full six-wide runs like the other families; bottom
# (0x0E) has NO six-wide run (its longest is the three-wide 51-53), so its offset points at that run's
# start. That shorter run is immaterial to the debug spawner, which forces `formation count` = 1 and so
# reads only the run's first position; the natural area waves reach every Zoshi type through the AI-level
# formation table (other offsets), independent of these debug offsets.
ZOSHI_RND_FORMATION_OFFSET = 31
ZOSHI_TOP_FORMATION_OFFSET = 45
ZOSHI_BOTTOM_FORMATION_OFFSET = 51
# The flying-type-table offsets for the two Jara types (object-types.json 0-based run starts): the
# 0x55 shooter run is codes 13-18 and the 0x56 silent run is codes 19-24 (both full six-wide, like
# the other families). The PAIR offset 18 straddles the boundary: its two-slot window reads codes[18]
# = 0x55 then codes[19] = 0x56, so a debug spawn of COUNT 2 there brings in one shooter AND one
# silent — the adjacent shooter+silent run the arcade wave data emits. Two independent craft-excluding
# random-Y draws (one per spawn) put them at different rows, so they cross the proximity band at
# different moments and peel opposite ways: the EMERGENT pair, made watchable on demand (see #74,
# docs/mechanics/031). The natural area waves reach both types through the AI-level formation table.
JARA_SHOOTER_FORMATION_OFFSET = 13
JARA_SILENT_FORMATION_OFFSET = 19
JARA_PAIR_FORMATION_OFFSET = 18
# The Terrazi family's fire-permission mask Stage var (set live by the area schedule's
# `fire_mask_terrazi` record; one of FIRE_MASK_FAMILIES). Captured into `slot fire mask` at spawn.
FIRE_MASK_TERRAZI_ID = "fire-mask-terrazi"
# The Kapi family's fire-permission mask Stage var (set live by the area schedule's `fire_mask_kapi`
# record; one of FIRE_MASK_FAMILIES). Captured into `slot fire mask` at spawn, consumed by the dive.
FIRE_MASK_KAPI_ID = "fire-mask-kapi"
# The Zoshi family's fire-permission mask Stage var (set live by the area schedule's `fire_mask_zoshi`
# record; one of FIRE_MASK_FAMILIES). Captured into `slot fire mask` at spawn, consumed by the shared
# Zoshi fire block (which also re-headings the enemy's own drift on the same trigger).
FIRE_MASK_ZOSHI_ID = "fire-mask-zoshi"

# Object type codes the flying dispatch handles (object-types.json). With AIR-04 Jara built, the
# slice-10 aerial families (Toroid, Terrazi, Kapi, Torkan, Zoshi, Jara) all spawn. Other flying
# enemies the formation table still emits — Giddo Spario (0x08) and the Zakato variants (0x12-0x17) —
# remain unhandled (not in FLYING_HANDLED_TYPES) and are silently skipped by the spawner until their
# own slices; the "fewer enemies" deviation is retired only for the built families, not all aerials.
TOROID_TYPE = 10  # 0x0A, non-shooting
TOROID_SHOOTS_TYPE = 11  # 0x0B, fires one aimed bullet at the swing trigger
# AIR-03 Zoshi (Octopus): three object types sharing one movement/anim/fire core (handle_0C/0D/0E,
# 3412-3499). All three drift on the 24-magnitude toroid tier (1.5 px/frame) aimed at the craft and
# fire the shared aimed bullet under the Zoshi mask; they differ only in spawn entry and in how each
# RE-HEADINGS its OWN drift at each shot — top/bottom re-aim toward the craft, rnd veers to a RANDOM
# angle (the distinctive erratic flyer). The arcade's "random" is this ENEMY MOVEMENT, never the shot.
ZOSHI_RND_TYPE = 12  # 0x0C, handle_0C_Zoshi_rnd: top-entry, 70 pts, RANDOM drift re-heading each shot
ZOSHI_TOP_TYPE = 13  # 0x0D, handle_0D_Zoshi_top: top-entry, 100 pts, re-aims drift toward the craft
ZOSHI_BOTTOM_TYPE = 14  # 0x0E, handle_0E_Zoshi_bottom: bottom-entry (fixed row), 100 pts, re-aims toward craft
TORKAN_TYPE = 15  # 0x0F, attack-and-retreat: one aimed shot, hover/animate, then flee AWAY at speed
KAPI_TYPE = 16  # 0x10, the first peel-away DIVING aerial family (handle_10_Kapi)
TERRAZI_TYPE = 17  # 0x11, the first periodically-firing aerial family (handle_11_Terrazi)
# AIR-04 Jara (Spinner): two INDEPENDENT object types over one shared init + one shared update
# (handle_55_Jara_shoots / handle_56_Jara, 3502-3599). These are the arcade's own codes 0x55/0x56
# (NOT sequential after Terrazi — the flying type table is loaded and hash-verified verbatim, and the
# dispatch is by direct equality, so a family's port type byte MUST equal its arcade code). Both
# cruise aimed at the craft on the fast 48-tier holding a static frame, then peel AWAY and spin when
# the craft enters a lateral proximity band; the 0x55 shooter also fires one aimed bullet at the turn.
JARA_SHOOTER_TYPE = 85  # 0x55, handle_55_Jara_shoots: fires exactly one aimed bullet at the turn
JARA_SILENT_TYPE = 86  # 0x56, handle_56_Jara: identical motion/anim but never fires
FLYING_HANDLED_TYPES = (
    TOROID_TYPE,
    TOROID_SHOOTS_TYPE,
    ZOSHI_RND_TYPE,
    ZOSHI_TOP_TYPE,
    ZOSHI_BOTTOM_TYPE,
    TORKAN_TYPE,
    KAPI_TYPE,
    TERRAZI_TYPE,
    JARA_SHOOTER_TYPE,
    JARA_SILENT_TYPE,
)
# DEBUG (tracked for removal, #119): the families the T key cycles through, one at a time — each a
# (type, formation offset, spawn count) whose offset points the spawner at a run of that family and
# whose count is how many to bring in as one group (almost always 1). T brings in the entry at `debug
# spawn index`, then advances the index (mod len). The type element documents which family the offset
# selects (the present check that keeps a group solo is family-agnostic). The final Jara entry is the
# one exception to count 1: it spawns the shooter+silent PAIR (count 2 at the straddling offset 18) so
# the operator can watch the emergent split — two Jara at different random Y peeling opposite ways.
DEBUG_SPAWN_FAMILIES = (
    (TERRAZI_TYPE, TERRAZI_FORMATION_OFFSET, 1),
    (KAPI_TYPE, KAPI_FORMATION_OFFSET, 1),
    (TORKAN_TYPE, TORKAN_FORMATION_OFFSET, 1),
    (ZOSHI_TOP_TYPE, ZOSHI_TOP_FORMATION_OFFSET, 1),
    (ZOSHI_BOTTOM_TYPE, ZOSHI_BOTTOM_FORMATION_OFFSET, 1),
    (ZOSHI_RND_TYPE, ZOSHI_RND_FORMATION_OFFSET, 1),
    (JARA_SHOOTER_TYPE, JARA_SHOOTER_FORMATION_OFFSET, 1),  # shooter solo
    (JARA_SILENT_TYPE, JARA_SILENT_FORMATION_OFFSET, 1),  # silent solo
    (JARA_SHOOTER_TYPE, JARA_PAIR_FORMATION_OFFSET, 2),  # emergent pair: one 0x55 + one 0x56
)
TOROID_PTS = 3  # 1-based value-table position of 30 points (init_toroid PTS byte 6)
TOROID_INIT_CODE = 8  # face-on sprite code at spawn (codes 8..15 cycle during the swing)

# GND ground-object types — the arcade object codes (obj_handler_tbl 6196), dispatched by direct
# equality like the flying families. This PR builds Barra (#70) and Logram (#71); the other ground
# codes present in the schedules (Zolbak 0x1F, Derota 0x2C/0x2D, ...) stay on the empty seam for their
# own slices, so an add_ground_object record for an unbuilt type advances the cursor without spawning.
BARRA_TYPE = 30  # 0x1E, handle_1E_Barra: passive terrain target, never fires, crater on death
GARU_BARRA_TYPE = 32  # 0x20, handle_20_Garu_Barra: indestructible base + destructible node (Commit 6)
LOGRAM_TYPE = 38  # 0x26, handle_26_Logram: open/close dome, one aimed shot at full-open (Commit 7)
GROUND_HANDLED_TYPES = (BARRA_TYPE, GARU_BARRA_TYPE, LOGRAM_TYPE)  # spawned by this PR
BARRA_PTS = 6  # 1-based value-table position of 100 points (handle_1E_Barra _PTS=15 -> object_value_tbl)
LOGRAM_PTS = 10  # 1-based value-table position of 300 points (handle_logram_init _PTS=27)
GARU_BARRA_PTS = 10  # 1-based value-table position of 300 points (handle_20_Garu_Barra node _PTS=27)

# GND crater/explosion (handle_bomb_explosion $3186 / bomb_explosion_finished $31D7): a bombed passive
# ground object (Barra) plays the shared bomb-explosion animation, then becomes a PERSISTENT scrolling
# crater. `slot timer` (reset to 0 by the ground detector at the hit, exactly as the air detector resets
# it) is the clock in arcade-frame units (TICK_TIMER_STEP per tick). The arcade advances the 7-frame
# animation on every 8th arcade frame (`TIMER & 7`); at animation frame 7 it switches to the crater and
# flickers codes 0xA6/0xA7 (bomb_explosion_finished) indefinitely while it keeps scrolling. There is NO
# free-on-clock — a crater is culled only once it scrolls off the bottom, unlike the flying explosion
# (install_explode_toroid_tick) which frees its own slot on the clock.
GROUND_EXPLOSION_PHASE_FRAMES = 8  # animation frame = floor(slot timer / 8) (arcade `TIMER >> 3`)
GROUND_EXPLOSION_FRAME_COUNT = 7  # animation frames 0..6 play, then the crater begins
GROUND_CRATER_START_FRAMES = GROUND_EXPLOSION_PHASE_FRAMES * GROUND_EXPLOSION_FRAME_COUNT  # 56
GROUND_CRATER_FLICKER_FRAMES = 4  # crater alternates 0xA6/0xA7 every 4 frames (arcade `countup >> 2`)

# GND (ground.barra #70) Garu node death (explode_and_remove_object $3216): a bombed Garu node plays the
# SHORTER explode-and-remove burst and then VANISHES (no crater), unlike the Barra. The arcade advances
# this burst on every 4th arcade frame (`TIMER & 3`) — twice the cadence of the Barra crater's every-8th
# — and removes the object at animation frame 7 (`(TIMER >> 2) == 7`). In `slot timer` arcade-frame units
# that is a 4-frame phase and removal at frame 7 => timer 28. There is no persistent crater: the object
# is culled the instant the burst finishes, so it never reaches the frame-7 costume on screen.
GARU_EXPLOSION_PHASE_FRAMES = 4  # burst frame = floor(slot timer / 4) (arcade `TIMER >> 2`)
GARU_REMOVE_FRAMES = GARU_EXPLOSION_PHASE_FRAMES * GROUND_EXPLOSION_FRAME_COUNT  # 28: remove at frame 7

# GND (ground.logram #71) fire/animation cycle (handle_logram_main $1B64). The whole timer runs on the
# arcade's every-8th-frame phase (`countup_timer_1 & 7`), which is the port's every-4th-TICK phase
# (FIRE_GATE_PHASE_TICKS; 1 tick = 2 arcade frames). Two phases keyed by `slot flag`: a WAIT countdown (a
# masked-random delay, `slot fire timer` decremented to 0) then an ANIMATE count-up (`slot fire timer`
# 1..28). Each animate step derives the dome stage `(_TIMER>>2)&7` (0..6), FIRES one aimed bullet at
# `_TIMER==12` (stage 3, the fully-open dome), and writes the dome costume ordinal for the stage; at stage
# 7 (`_TIMER==28`) it re-rolls a fresh masked wait (start_logram_shot_timer $1BC9) and returns to WAIT.
LOGRAM_WAIT_PHASE = 0  # slot flag: counting the masked-random delay down
LOGRAM_ANIMATE_PHASE = 1  # slot flag: counting the open/close animation up
LOGRAM_FIRE_TIMER = 12  # fire one aimed bullet when slot fire timer reaches 12 (arcade `cmp #0x0c`)
LOGRAM_STAGE_PHASE = 4  # dome stage = floor(slot fire timer / 4) ...
LOGRAM_STAGE_MOD = 8  # ... mod 8 (arcade `lsr #2; and #7`)
LOGRAM_RECYCLE_STAGE = 7  # at stage 7 the cycle restarts: re-roll the wait, back to the closed dome
LOGRAM_STAGE_PEAK = 3  # dome ordinal = LOGRAM_OPEN_FRAME_COUNT - abs(stage - 3): the triangle {1,2,3,4,3,2,1}

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
SLOT_UNITS_PER_PIXEL = SLOT_UNITS_PER_CELL // 8  # 32; a cell is 8 px. Ground spawn maps the schedule's
# sprite_y byte (a lateral PIXEL position) to slot y with the reference's `lsl #5` (x32), sub_2_fn_1.
TICK_VELOCITY_SCALE = 4  # 1 tick = 2 arcade frames; each applies 2*velocity => 4*velocity/tick
TICK_TIMER_STEP = 2  # the animation clock advances 2 arcade frames per tick
TOROID_SWING_ACCEL = 2  # lateral velocity change per tick (1 unit/frame * 2 frames)
CULL_ROW_MAX = 40  # >= 0x28 rows (past the bottom) -> offscreen
CULL_ROW_MIN = -2  # <= -2 rows (past the top, the reference's byte-wrap) -> offscreen
CULL_COL_MAX = 31  # >= 0x1F columns -> offscreen (lateral)
CULL_COL_MIN = -2  # <= -2 columns -> offscreen (left edge; bullets can fly out any side)
TOROID_SPAWN_ROW = 0  # new/refilled flying enemies enter from the top row (see install_init_toroid)

# WPN-04 bombing geometry (init_bombing $188C / update_crosshair $16E8), all on the scroll/depth axis
# (slot x). The crosshair and the locked bomb target sit a fixed distance AHEAD of the craft:
# arcade `solvalou_X + 0xF400` = -3072 units = -12 cells = -96 stage-px (lower slot x is up-screen).
BOMB_TARGET_LEAD = -12 * SLOT_UNITS_PER_CELL  # 0xF400 at the pin
FRAMES_PER_TICK = 2  # 1 port tick = 2 arcade frames (the established scroll cadence)
BOMB_ACCEL_PER_FRAME = 2  # the bomb's `_dX` gains -2 per arcade frame, then `_X += _dX*2`
# The bomb target scrolls with the terrain — the same scroll_delta the ground uses (scroll_sprite_X
# $30E8: +16 units/arcade-frame). Per tick that is AREA_PROGRESS_STEP (32); per frame, half of it.
SCROLL_UNITS_PER_FRAME = AREA_PROGRESS_STEP // FRAMES_PER_TICK  # 16

# AIR-06 Terrazi (handle_11_Terrazi 3667-3729): the first periodically-firing aerial family. Aimed
# approach on the 48-magnitude (3 px/frame) tier; while distant it fires under its mask (the shared
# fire-permission gate). Inside a narrow LATERAL window (the same axis the Toroid swing uses) it stops
# firing and GLIDES: the LATERAL velocity is set to a slow +/-2 drift by side while the SCROLL/forward
# velocity decelerates and reverses, peeling its forward approach away over ~24 frames. Type 0x11, 700 pts.
TERRAZI_PTS = 14  # 1-based value-table position of 700 points (handle_11 PTS byte 39)
TERRAZI_INIT_CODE = 1  # spawn sprite code (_CODE=0x01); the roll animation is derived render-only
TERRAZI_FLAG_APPROACH = 0
TERRAZI_FLAG_GLIDE = 1
# Glide window: the LATERAL offset (player col - slot col) lies in [LOW, HIGH] — the reference's
# `subq #4 / addq #8` carry test on solvalou._Y - self._Y (3683-3692), where `_Y` is the lateral axis
# (`dir_delta_tbl` 2172: Up/Down move `_dX`, Left/Right move `_dY`). True exactly on the offset in
# [-4, 3]. Mirrors the Toroid window convention (the arcade byte compare read as a port cell offset).
TERRAZI_GLIDE_LOW = -4
TERRAZI_GLIDE_HIGH = 3
TERRAZI_GLIDE_DRIFT = 2  # LATERAL velocity SET at glide entry (_dY=+/-2, 3694-3699); sign by side
# Forward/scroll decel per TICK. The reference decrements the SCROLL velocity by 2 PER FRAME
# (`subq #2,_dX` 3715); a tick is 2 arcade frames, so the per-tick delta is 4 — the same frame->tick
# doubling as the
# Toroid swing accel (1/frame -> 2/tick).
TERRAZI_GLIDE_DECEL = 4

# AIR-06 fire-permission gate (chk_timer_fire_bullet_reinit_timer 4999-5010). The reference gates fire
# on a GLOBAL 8-arcade-frame phase (`countup_timer_1 & 7 == 0`); a tick is 2 arcade frames, so the port
# phase is every 4th tick. On a phase tick it decrements the per-slot fire countdown as a BYTE (with
# 256-wrap, so a spawn draw of 0 wraps to 255 then counts down — the reference's byte underflow) and,
# at zero, fires one aimed bullet and reloads the countdown to (rng & mask) + 1. The mask is a contiguous
# low-bit fire-frequency byte, so `rng & mask` is reproduced as `rng mod (mask+1)` — exact for every
# flying family's scheduled masks (Terrazi 3/7, Zoshi 15/31, Kapi 3/7); the boss `andor_genesis` mask 47
# is the one non-contiguous byte and is flagged for its own leaf. Recorded in record 027.
FIRE_GATE_PHASE_TICKS = 4  # 8 arcade frames / 2 frames-per-tick
FIRE_TIMER_BYTE_MOD = 256  # the countdown is a byte; decrement wraps mod 256 (reference underflow)
TERRAZI_FIRE_SUPPRESS = 255  # glide sets the fire countdown to 0xff to suppress fire (3699)

# AIR-05 Kapi (handle_10_Kapi 3602-3623, kapi_10_fire 3624-3665): the first peel-away DIVING aerial
# family. It approaches SILENTLY on its aimed 2 px/frame velocity (the 32-magnitude generic tier,
# angle_dX_dY_tbl 6360) and does NOT fire while approaching. Each tick it counts an initial delay
# down; at zero it latches a dive side ONCE and commits: the LATERAL velocity accelerates AWAY from
# the craft's column (`ddY` = sign of self._Y - solvalou._Y, latched at kapi_10_fire 3626-3633 and
# never recomputed — the Toroid swing kinematics, so it decelerates, crosses zero, and peels away),
# the SCROLL/forward velocity decelerates by 2 px/frame (`subq #2,_dX` 3651), and it fires EVERY tick
# under the Kapi mask (no suppression, unlike Terrazi's glide). Type 0x10, 300 pts.
KAPI_PTS = 10  # 1-based value-table position of 300 points (handle_10_Kapi PTS byte 27 -> table 10)
KAPI_INIT_CODE = 0x20  # spawn/approach sprite code (_CODE=0x20); the dive animates 0x20..0x26
KAPI_FLAG_APPROACH = 0
# Two latched dive sides, mirroring the Toroid's SWING_* exactly: the peel-away lateral-accel sign,
# fixed at the dive trigger and never recomputed (recomputing would re-home after the velocity crosses
# zero). offset = player col - self col; higher col = higher x = right, so a NEGATIVE lateral accel
# peels toward lower columns. Source ddY = +1 iff solvalou._Y < self._Y (i.e. offset < 0), else -1.
KAPI_FLAG_DIVE_MINUS = 1  # _dY -= accel: craft at/right laterally (offset >= 0) — peel toward low col
KAPI_FLAG_DIVE_PLUS = 2  # _dY += accel: craft left laterally (offset < 0) — peel toward high col
# Dive tuning, in slot units, scaled per tick (1 tick = 2 arcade frames):
KAPI_DIVE_LATERAL_ACCEL = 2  # lateral velocity change per tick (ddY = +/-1/frame * 2 frames)
KAPI_DIVE_SCROLL_DECEL = 4  # forward/scroll decel per tick (`subq #2,_dX` = 2/frame * 2 frames)
# Initial approach delay before the dive, in ARCADE FRAMES. The reference's code adds 63 then 48 (an
# unmasked +111) but its own comment reads "48-111" (3616-3618) — a probable transcription slip; the
# locked spec follows the commented range and records the deviation (F4, carried forward from #117's
# fire-delay decision). So (rng mod 64) + 48 => 48-111, consumed 2 frames/tick during the approach.
KAPI_APPROACH_DELAY_BASE = 48
KAPI_APPROACH_DELAY_SPAN = 64

# AIR-02 Torkan (handle_0F_Torkan 3357-3377, torkan_shoot 3378-3394, torkan_update_dir 3395-3411): the
# attack-and-retreat aerial family. It spawns aimed TOWARD the craft on the 32-magnitude generic tier
# (2 px/frame, angle_dX_dY_tbl 6360) with a plain spawn-column draw (NO craft exclusion, like the Kapi),
# approaches while counting a shot delay down, then at zero fires EXACTLY ONE aimed bullet directly
# (init_new_bullet 5012 — NOT the fire-permission gate, no mask, never repeats). It then HOVERS in place
# animating a 7-frame roll for ~28 frames, then re-aims ONCE 180 degrees AWAY from the craft on the fast
# 48-magnitude tier (3 px/frame, angle_dX_dY_terrazi_torkan_tbl 6325) and flees straight until culled.
# Type 0x0F, 50 pts.
TORKAN_PTS = 4  # 1-based value-table position of 50 points (handle_0F PTS byte 9 -> value 50)
TORKAN_INIT_CODE = 0x10  # spawn/approach sprite code (_CODE=0x10); the hover animates 0x10..0x16
TORKAN_FLAG_APPROACH = 0  # aimed toward the craft, counting the shot delay down
TORKAN_FLAG_HOVER = 1  # fired; holding position, animating the roll until the hover window ends
TORKAN_FLAG_FLEE = 2  # re-aimed 180 deg away; fleeing straight at 3 px/frame until culled
# Shot delay before the single fire, in ARCADE FRAMES: (rnd & 0x3f) + 0x40 = 64-127 (handle_0F 3366-3367),
# consumed 2 frames/tick during the approach. A clean masked draw (no transcription quirk, unlike Kapi's).
TORKAN_SHOT_DELAY_BASE = 64
TORKAN_SHOT_DELAY_SPAN = 64
# Hover window end, in ARCADE FRAMES: the arcade ends the hover when (timer>>2)&0xf == 7, first true at
# timer == 28 (torkan_shoot 3384-3391). `slot timer` counts arcade frames (advances 2/tick, 2 frames/tick).
TORKAN_HOVER_END = 28
# The retreat's 180-degree flip. The arcade retreat (torkan_update_dir 3399-3404) takes the raw angle of
# the vector TOWARD the craft via get_index_for_angle, adds 0x80 to that byte, then falls into
# get_dX_dY_and_cpy_to_obj (`lsr.b #3`) — which, UNLIKE the toward/spawn path
# (calc_dX_dY_for_vector_to_solvalou 3365, the `addq #4` "black magic"), applies NO +4 rounding. So the
# port derives the away index from the UN-rounded folded angle the shared quantizer leaves in `aim base`
# (0..255): (aim base >> 3 + 16) mod 32, 1-based. 0x80 >> 3 = 16 = half of the 32-entry circle.
TORKAN_REAIM_HALF_TURN = 16

# AIR-03 Zoshi (Octopus) — handle_0C_Zoshi_rnd / handle_0D_Zoshi_top / handle_0E_Zoshi_bottom
# (3412-3499). Three object types over one shared movement/anim/fire core. All three drift on the
# 24-magnitude toroid tier (1.5 px/frame, angle_dX_dY_toroid_tbl 6394 -> the port's `aim d? 24` lists),
# initial heading aimed at the craft (calc_dX_dY_for_vector_to_solvalou at init). Each fires the shared
# aimed bullet under the Zoshi fire mask on a masked-periodic timer; on each fire it also RE-HEADINGS its
# OWN drift — top/bottom re-aim toward the craft, rnd draws a RANDOM angle. That erratic movement is the
# whole distinction the arcade calls "random"; every Zoshi SHOT is aimed at the craft (init_new_bullet ->
# the TYPE-6 homing bullet the port models as fire-once-aimed, record 026). See docs/mechanics/030.
ZOSHI_PTS_AIMED = 6  # top/bottom: 1-based value-table position of 100 points (0D/0E PTS byte 15 -> 100)
ZOSHI_PTS_RND = 5  # rnd: 1-based value-table position of 70 points (0C PTS byte 12 -> value 70)
ZOSHI_INIT_CODE = 0x28  # spin sprite code base (_CODE = 0x28 + (timer & 3)); frames 0x28..0x2B
ZOSHI_ANIM_FRAMES = 4  # zoshi/spin/01..04 — the 4-code spin cycle (0x28..0x2B, `& 3`)
# The bottom-entry Zoshi enters from the bottom of the screen at a fixed scroll row (handle_0E
# `move.b #40,(_X,a5)`) and drifts UP toward the craft; the other two enter from the top row like the
# rest of the flying families (the no-enemy-scroll deviation). Row 40 is the bottom cull threshold
# (CULL_ROW_MAX), so the enemy sits at the very bottom edge on the entry tick and the move-before-cull
# ordering (shared by every family) carries it up into view before the cull test runs — faithful to the
# arcade's from-the-bottom entry.
ZOSHI_BOTTOM_EDGE_X = 40

INIT_ZOSHI_TOP_PROCCODE = "init zoshi top"
INIT_ZOSHI_BOTTOM_PROCCODE = "init zoshi bottom"
INIT_ZOSHI_RND_PROCCODE = "init zoshi rnd"
UPDATE_ZOSHI_PROCCODE = "update zoshi"

# AIR-04 Jara (Spinner) — handle_55_Jara_shoots / handle_56_Jara (3502-3599). Two INDEPENDENT object
# types over one shared init + one shared update. Both aim the initial drift at the craft on the fast
# 48-magnitude tier (3 px/frame, angle_dX_dY_terrazi_torkan_tbl 3581), draw a craft-EXCLUDING random Y
# (gen_rnd_spriteY 3580, the +/-8 reject), and score 150 pts (_PTS byte 18 -> table pos 7). They cruise
# straight on that fixed vector holding the static frame 0xA0 (jara_set_clr_and_move 3508 never touches
# _CODE/_TIMER) until jara_check_proximity (3588-3595) reports the craft within the LATERAL band; on
# that first close tick they commit ONE-WAY to a turn (jara_set_dir 3513) that ramps the lateral
# velocity +/-1/frame AWAY from the craft (jara_moving_right `subq #1,_dY` 3532 / jara_moving_left
# `addq #1,_dY` 3567) and spins the 6-frame animation. _dX (scroll/forward) is UNTOUCHED — unlike Kapi's
# dive, which also decelerates _dX. The shooter (0x55) ALSO fires exactly one aimed bullet at the turn
# instant (jara_shoot 3544 -> init_new_bullet, nested in the transition so it cannot repeat); the silent
# (0x56) never fires. No fire mask exists (jara_init never sets _FFREQ). See docs/mechanics/031.
JARA_PTS = 7  # 1-based value-table position of 150 points (_PTS byte 18 = 3*(7-1))
JARA_INIT_CODE = 0xA0  # spawn/approach sprite code (_CODE=0xA0); the spin animates 0xA0..0xA5
JARA_ANIM_FRAMES = 6  # jara/spin/01..06 (sprite codes 0xA0..0xA5)
# Anim period, in ARCADE FRAMES: the arcade advances the sprite code every 2 frames (`_TIMER>>1` then
# `&7`, reset at 6 -> the 0..5 cycle each frame-pair, 3521-3528). `slot timer` counts arcade frames
# (advances 2/tick), so phase = floor(slot timer / 2) mod 6 gives one frame per tick, the same pacing.
JARA_ANIM_PERIOD = 2
# Slot sub-state (`slot flag`): pre-turn approach, then a committed peel side, mirroring Kapi's dive
# side latch EXACTLY. The proximity gap and the peel are both on the LATERAL axis (arcade `_Y` = the
# port's col; jara_check_proximity reads solvalou._Y - self._Y). Turn side = sign of the lateral offset
# (player col - self col) at the turn instant: craft at/right laterally (offset >= 0) -> jara_moving_
# right `subq #1,_dY` (TURN_MINUS, forward spin order); craft left (offset < 0) -> jara_moving_left
# `addq #1,_dY` (TURN_PLUS, reversed spin order). Latched once at the turn and never recomputed.
JARA_FLAG_APPROACH = 0
JARA_FLAG_TURN_MINUS = 1  # _dY -= accel: craft at/right laterally (offset >= 0), forward spin
JARA_FLAG_TURN_PLUS = 2  # _dY += accel: craft left laterally (offset < 0), reversed spin
JARA_TURN_LATERAL_ACCEL = 2  # lateral velocity change per tick (`_dY +/- 1`/frame * 2 frames/tick)
# Proximity band (jara_check_proximity 3591-3594): offset = player col - self col; the arcade computes
# (craft._Y - self._Y), `subq #6`, then `add #0x0c`, setting the carry (close) exactly when the offset
# is in [-6, +5] — ASYMMETRIC (the +6 boundary is EXCLUDED by the unsigned byte carry). The turn
# commits on the first tick the offset enters this band; the side then splits it at offset 0.
JARA_PROXIMITY_LOW = -6
JARA_PROXIMITY_HIGH = 5

INIT_JARA_PROCCODE = "init jara"
UPDATE_JARA_PROCCODE = "update jara"

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

# AIR-06 Terrazi renderer: one persistent clone per flying slot (59-64), gated on the Terrazi type,
# costumed by the 7-frame roll cycle extracted onto the shared sprite-extraction proof (record 002's
# pen). game_director owns the target's existence + render blocks; sprite_extractor owns the costumes.
# The roll frame is derived render-only from the slot's animation clock (`slot timer`), so the craft
# visibly rolls without the walk writing `slot code` — the reference's `_ddX` sprite-code advance,
# reproduced on the render side (every ~8 arcade frames through the 7 frames).
TERRAZI_TARGET = "terrazi"
TERRAZI_CLONE_SLOT_ID = "terrazi-clone-slot"  # sprite-local: which flying slot this clone renders
TERRAZI_RENDER_SIZE = 225  # match the Toroid's on-screen scale (a 16-px sprite at ~2.25 stage px/px)
TERRAZI_ROLL_FRAMES = 7  # terrazi/roll/01..07
TERRAZI_ROLL_PERIOD = 8  # advance the roll every ~8 arcade frames (`_ddX >> 3`); slot timer ~= frames

# AIR-05 Kapi renderer: one persistent clone per flying slot, same pool pattern as the Terrazi/Toroid.
# While approaching it holds the static entry frame; while diving the 7-frame dive animation is derived
# render-only from the slot's animation clock — an 8-phase cycle whose 8th phase HOLDS the last frame
# (the reference's `d0 = (TIMER1>>3) & 7`, hold at d0 == 7 -> loc_2455 3654), then loops.
KAPI_TARGET = "kapi"
KAPI_CLONE_SLOT_ID = "kapi-clone-slot"  # sprite-local: which flying slot this clone renders
KAPI_RENDER_SIZE = 225  # match the shared on-screen scale (a 16-px sprite at ~2.25 stage px/px)
KAPI_DIVE_FRAMES = 7  # kapi/dive/01..07 (sprite codes 0x20..0x26)
KAPI_DIVE_PERIOD = 8  # advance the dive frame every ~8 arcade frames (`TIMER1>>3`); slot timer ~= frames
KAPI_DIVE_PHASES = 8  # the animation clock cycles 0..7; phase 7 holds the last frame (loc_2455)

# AIR-02 Torkan renderer: one persistent clone per flying slot, same pool pattern as the others. While
# approaching it holds the static entry frame (0x10); during the hover it sweeps a 7-frame roll
# (0x10..0x16), one frame per 4 arcade frames (`timer>>2`), a single 0..6 sweep (the hover ends at phase
# 7, so no wrap); while fleeing it holds the last frame (0x16).
TORKAN_TARGET = "torkan"
TORKAN_CLONE_SLOT_ID = "torkan-clone-slot"  # sprite-local: which flying slot this clone renders
TORKAN_RENDER_SIZE = 225  # match the shared on-screen scale (a 16-px sprite at ~2.25 stage px/px)
TORKAN_ANIM_FRAMES = 6  # torkan/roll/01..06 — see below. The arcade cycles SEVEN sprite codes
# (0x10..0x16) during the hover (torkan_shoot 3383-3390: `d0 = (TIMER>>2)&0xf`, running 0..6 before it
# exits at 7, so codes 0x10+0..0x10+6). CrazyCarl's aerial-enemies rip provides only SIX distinct Torkan
# rotation frames, so the 7th code-step (d0=6) HOLDS the last available frame — the same "hold the last
# frame" idiom the Kapi uses for its 8th dive phase. The hover-window length (28 frames) and the re-aim
# boundary are driven by the timer, not the art, so they stay exactly faithful (deviation recorded 029).
TORKAN_ANIM_PERIOD = 4  # advance the hover frame every 4 arcade frames (`timer>>2`); slot timer ~= frames

# AIR-03 Zoshi renderer constants. One persistent clone per flying slot draws the spinning octopus; the
# 4-code spin (0x28..0x2B) is written into `slot code` by the shared update each active tick (from the
# global frame, `0x28 + (tick & 3)`), so the render reads only the Stage slot lists — the same clone-pool
# pattern as the Kapi/Torkan. The shared explosion frames follow the four spin frames (ordinals 5..).
ZOSHI_TARGET = "zoshi"
ZOSHI_CLONE_SLOT_ID = "zoshi-clone-slot"  # sprite-local: which flying slot this clone renders
ZOSHI_RENDER_SIZE = 225  # match the shared on-screen scale (a 16-px sprite at ~2.25 stage px/px)

# AIR-04 Jara renderer constants. One persistent clone per flying slot draws the spinner; unlike the
# Zoshi (which writes `slot code` each tick), the spin frame is derived render-only from the slot's
# animation clock — the Kapi/Terrazi/Torkan idiom. While APPROACHING it holds the static entry frame
# 0xA0; while TURNED it cycles the 6 spin frames, in FORWARD order (jara/spin/01..06) for the TURN_MINUS
# side and REVERSED (06..01) for TURN_PLUS — the arcade's jara_right_sprite_tbl / jara_left_sprite_tbl
# (3571-3575). The shared explosion frames follow the six spin frames (ordinals 7..).
JARA_TARGET = "jara"
JARA_CLONE_SLOT_ID = "jara-clone-slot"  # sprite-local: which flying slot this clone renders
JARA_RENDER_SIZE = 225  # match the shared on-screen scale (a 16-px sprite at ~2.25 stage px/px)

# GND (ground.barra #70) Barra renderer constants. Unlike a flying family (one clone per flying slot), a
# ground family draws one persistent clone per GROUND slot (1..16), each a pure per-tick function of its
# slot's live state: the single Barra idle pyramid (code 0x17) while ACTIVE, then — once bombed (HIT) —
# the shared bomb-explosion burst for the first GROUND_CRATER_START_FRAMES, then the flickering crater.
# Costume ordinals on the barra target: 1 = barra/idle, 2.. = the shared solv_death explosion burst,
# then the two crater frames appended last (see expected_project's mirror). The clone writes no state.
BARRA_TARGET = "barra"
BARRA_CLONE_SLOT_ID = "barra-clone-slot"  # sprite-local: which ground slot this clone renders
GROUND_RENDER_SIZE = 225  # match the shared on-screen scale (a 16-px sprite at ~2.25 stage px/px)
EXPLODE_COSTUME_COUNT = 8  # the shared solv_death burst is 8 costumes (explode_01..08)
BARRA_IDLE_ORDINAL = 1  # costume 1: the Barra idle pyramid (barra/idle/01)
BARRA_EXPLODE_BASE_ORDINAL = 2  # costume 2..: the shared explosion burst (explode_01..)
BARRA_CRATER_BASE_ORDINAL = BARRA_EXPLODE_BASE_ORDINAL + EXPLODE_COSTUME_COUNT  # 10: crater frames follow

# GND (ground.barra #70) Garu Barra renderer constants. The Garu is TWO objects in adjacent ground slots
# sharing one type (GARU_BARRA_TYPE): a 2x2 indestructible base (state SLOT_GARU_BASE) and a 1x1
# destructible node (state ACTIVE/HIT). One `garu` target's clone pool covers the ground band; each clone
# branches on its slot's STATE — base vs node — because both carry the same slot type. Costume layout:
#   1..2  garu/base pulse frames (the arcade base cycles pulsing_colour_1; the two sheet frames — plain
#         pyramid / red-glow core — stand in, alternated on the global `tick` like the Zoshi spin);
#   3     the node idle pyramid (garu/node reuses the Barra pyramid, mirrored from barra/idle);
#   4..11 the shared solv_death burst the node's explode-and-remove plays before it vanishes.
# The base is drawn TWICE the linear size of the node (arcade _ATTR=3, 2x2) — its costume is a 32-px
# canvas vs the node's 16-px, so the SAME GROUND_RENDER_SIZE yields ~2x on screen (no extra scaling).
GARU_TARGET = "garu"
GARU_CLONE_SLOT_ID = "garu-clone-slot"  # sprite-local: which ground slot this clone renders
GARU_BASE_IDLE_ORDINAL = 1  # costumes 1..2: the 2x2 base pulse frames (garu/base/01..02)
GARU_BASE_PULSE_FRAMES = 2  # the base alternates its two pulse frames
GARU_BASE_PULSE_TICKS = 4  # ticks per pulse frame (8 arcade frames, the arcade global-animation phase)
GARU_NODE_IDLE_ORDINAL = 3  # costume 3: the node idle pyramid (garu/node, mirrored from barra/idle)
GARU_EXPLODE_BASE_ORDINAL = 4  # costumes 4..: the shared explosion burst the node plays before removal

# GND (ground.logram #71) Logram renderer constants. One persistent clone per GROUND slot (1..16), the
# same terrain-band pool as the Barra/Garu, each a pure per-tick function of its slot's live state. While
# ACTIVE the dome shows the open/close frame `update logram` wrote into `slot code` (ordinals 1..4 =
# logram/open/01..04, arcade codes 0x2C..0x2F); once bombed (HIT) it craters IDENTICALLY to the Barra
# (handle_bomb_explosion): the shared solv_death burst, then the flickering crater. Costume layout:
#   1..4   logram/open/01..04 (the dome open/close frames; `slot code` indexes them directly);
#   5..12  the shared solv_death explosion burst (the ground bomb-burst is a deferred cosmetic stand-in);
#   13..14 the two crater frames the HIT renderer flickers between once the burst finishes.
LOGRAM_TARGET = "logram"
LOGRAM_CLONE_SLOT_ID = "logram-clone-slot"  # sprite-local: which ground slot this clone renders
LOGRAM_OPEN_FRAME_COUNT = 4  # logram/open/01..04 (the dome open/close cycle, arcade codes 0x2C..0x2F)
LOGRAM_CLOSED_ORDINAL = 1  # costume 1: the closed dome (0x2C), the spawn + wait-phase frame
LOGRAM_EXPLODE_BASE_ORDINAL = LOGRAM_OPEN_FRAME_COUNT + 1  # 5: shared explosion burst follows the dome frames
LOGRAM_CRATER_BASE_ORDINAL = LOGRAM_EXPLODE_BASE_ORDINAL + EXPLODE_COSTUME_COUNT  # 13: crater frames last


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


def _ground_scalars(record: dict) -> tuple[int, int, int]:
    # GND: the three runtime-readable scalars an add_ground_object record needs, pre-decoded from the
    # opaque JSON payload (Scratch cannot parse JSON at runtime) — object_type (the ground dispatch
    # discriminator), slot (0-15), sprite_y (0-255). Every other handler needs none -> (0, 0, 0); those
    # fillers are inert because the ground columns are read only when the handler is add_ground_object.
    if record["handler"] != ADD_GROUND_OBJECT_HANDLER:
        return 0, 0, 0
    params = record.get("params", {})
    return record["object_type"], params["slot"], params["sprite_y"]


def _load_area_schedule(
    area_number: int,
) -> tuple[
    list[str], list[int], list[str], list[int], list[int], list[int], list[int]
]:
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
    ground_types: list[int] = []
    ground_slots: list[int] = []
    ground_sprite_ys: list[int] = []
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
        ground_type, ground_slot, ground_sprite_y = _ground_scalars(record)
        ground_types.append(ground_type)
        ground_slots.append(ground_slot)
        ground_sprite_ys.append(ground_sprite_y)
    handlers.append(SCHEDULE_SENTINEL_HANDLER)
    rows.append(area["end_sentinel"])
    payloads.append("")
    args.append(0)
    ground_types.append(0)
    ground_slots.append(0)
    ground_sprite_ys.append(0)
    return handlers, rows, payloads, args, ground_types, ground_slots, ground_sprite_ys


def _load_all_area_schedules() -> tuple[
    list[str],
    list[int],
    list[str],
    list[int],
    list[int],
    list[int],
    list[int],
    list[int],
    list[int],
]:
    # AREA-03: flatten all 16 normal area schedules into the parallel columns, with two 16-entry
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
    ground_types: list[int] = []
    ground_slots: list[int] = []
    ground_sprite_ys: list[int] = []
    starts: list[int] = []
    ends: list[int] = []
    cursor = 1  # 1-based, matching Scratch list indexing and the runtime `schedule cursor`
    for area_number in range(AREA_FIRST, AREA_MAX + 1):
        (
            area_handlers,
            area_rows,
            area_payloads,
            area_args,
            area_ground_types,
            area_ground_slots,
            area_ground_sprite_ys,
        ) = _load_area_schedule(area_number)
        starts.append(cursor)  # this area's first index (before advancing the cursor)
        handlers.extend(area_handlers)
        rows.extend(area_rows)
        payloads.extend(area_payloads)
        args.extend(area_args)
        ground_types.extend(area_ground_types)
        ground_slots.extend(area_ground_slots)
        ground_sprite_ys.extend(area_ground_sprite_ys)
        cursor += len(area_rows)
        ends.append(cursor - 1)  # this area's last index (after advancing; inclusive)
    return (
        handlers,
        rows,
        payloads,
        args,
        ground_types,
        ground_slots,
        ground_sprite_ys,
        starts,
        ends,
    )


(
    SCHEDULE_HANDLERS,
    SCHEDULE_ROWS,
    SCHEDULE_PAYLOADS,
    SCHEDULE_ARGS,
    SCHEDULE_GROUND_TYPES,
    SCHEDULE_GROUND_SLOTS,
    SCHEDULE_GROUND_SPRITE_YS,
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
    # WPN-04: the walk thread owns the bomb logic and fires these two purely for the bomb renderer's
    # sounds — `bomb` on arm (drop), `bomb landed` on finish (explosion). The retired arrow-key target
    # sprite's bounds broadcasts (target-bounds-*) are gone: the crosshair is a pure renderer now.
    "bomb": "broadcastMsgId-bomb-release",
    "bomb landed": "broadcastMsgId-bomb-landed",
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
    kapi_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(KAPI_TYPE)),
        [blocks.call_proc(UPDATE_KAPI_PROCCODE, warp=True)],
    )
    torkan_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(TORKAN_TYPE)),
        [blocks.call_proc(UPDATE_TORKAN_PROCCODE, warp=True)],
    )
    terrazi_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(TERRAZI_TYPE)),
        [blocks.call_proc(UPDATE_TERRAZI_PROCCODE, warp=True)],
    )
    # AIR-03: the three Zoshi object types (rnd 0x0C / top 0x0D / bottom 0x0E) share ONE update proc
    # (handle_0C/0D/0E all fall through to zoshi_0D_main); dispatch them with a single OR branch, as the
    # Toroid ORs 0x0A/0x0B. The per-type difference (aimed vs random drift re-heading) is decided inside
    # `update zoshi` on `slot type`.
    is_zoshi = blocks.op_or(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZOSHI_RND_TYPE)),
        blocks.op_or(
            blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZOSHI_TOP_TYPE)),
            blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZOSHI_BOTTOM_TYPE)),
        ),
    )
    zoshi_branch = blocks.if_reporter(
        is_zoshi, [blocks.call_proc(UPDATE_ZOSHI_PROCCODE, warp=True)]
    )
    # AIR-04: the two Jara object types (shooter 0x55 / silent 0x56) share ONE update proc — both fall
    # through the same approach/turn/anim core (handle_55 and handle_56 differ only in the one-shot fire
    # the shooter adds at the turn). Dispatch with a single OR branch, as the Zoshi ORs its three types;
    # the per-type difference (fire vs no fire) is decided inside `update jara` on `slot type`.
    is_jara = blocks.op_or(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(JARA_SHOOTER_TYPE)),
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(JARA_SILENT_TYPE)),
    )
    jara_branch = blocks.if_reporter(
        is_jara, [blocks.call_proc(UPDATE_JARA_PROCCODE, warp=True)]
    )
    bullet_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(BULLET_TYPE)),
        [blocks.call_proc(UPDATE_BULLET_PROCCODE, warp=True)],
    )
    # GND (#69): every ground family shares the terrain-locked scroll + off-field cull of `advance
    # ground`, but each family that has per-state behaviour of its own gets a thin wrapper proc that
    # layers it on before delegating to `advance ground`. The Barra (#70) wraps it with the HIT
    # explosion clock (`update barra`); the Garu Barra (0x20, #70) with the node's explode-and-remove
    # (`update garu`) — the shared type dispatches BOTH its parts, which branch on state inside the proc.
    # The Logram (#71) wraps it with `update logram` for its open/close + single aimed shot; it too
    # delegates to `advance ground` for the shared terrain scroll + off-field cull.
    barra_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(BARRA_TYPE)),
        [blocks.call_proc(UPDATE_BARRA_PROCCODE, warp=True)],
    )
    garu_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(GARU_BARRA_TYPE)),
        [blocks.call_proc(UPDATE_GARU_PROCCODE, warp=True)],
    )
    logram_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(LOGRAM_TYPE)),
        [blocks.call_proc(UPDATE_LOGRAM_PROCCODE, warp=True)],
    )
    dispatch = blocks.if_reporter(occupied, [read_type, toroid_branch, kapi_branch, torkan_branch, terrazi_branch, zoshi_branch, jara_branch, bullet_branch, barra_branch, garu_branch, logram_branch])
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
    # Each delta is rebuilt FRESH for every comparison: a reporter block can attach to only one
    # parent, so reusing one `d_y`/`d_x` block across the `<` and `>` checks would let the second
    # steal it from the first, leaving the lower-bound compare with an empty operand (a dead bound
    # that widened the hit box to a quadrant). Lambdas keep every operand its own subtree.
    d_y = lambda: blocks.op_sub(
        blocks.op_mul(variable("player row", PLAYER_ROW_ID), number(SHADOW_PER_CELL)),
        sh(_cur_item(blocks, "slot x", SLOT_X_ID)),
    )
    d_x = lambda: blocks.op_sub(
        sh(_cur_item(blocks, "slot y", SLOT_Y_ID)),
        blocks.op_mul(variable("player col", PLAYER_COL_ID), number(SHADOW_PER_CELL)),
    )
    hit_y = blocks.op_and(
        blocks.op_not(blocks.op_lt(d_y(), number(dy_low))),
        blocks.op_not(blocks.op_gt(d_y(), number(dy_high))),
    )
    hit_x = blocks.op_and(
        blocks.op_not(blocks.op_lt(d_x(), number(dx_low))),
        blocks.op_not(blocks.op_gt(d_x(), number(dx_high))),
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


def _draw_spawn_column(blocks: Blocks, exclude_craft: bool = True) -> tuple[list, str]:
    # Shared bounded spawn-column draw for the flying families: draw a lateral column from the shared
    # stream, reject-and-redraw until it is on-screen (`rnd & 31`, reject >= 25, + 3 => column 3..27),
    # or give up after SPAWN_DRAW_ATTEMPTS tries (the recorded bounded-draw deviation). On success it
    # sets `slot y` and `spawn found`; on exhaustion the slot is left empty (the caller's stamp is gated
    # on `spawn found`, so the slot is retried next tick). Returns the `spawn attempts`/`spawn found`
    # reset blocks and the draw loop for the caller to chain ahead of its own family-specific stamp/aim.
    # Separate `rng mod (mask+1)` reads because a reporter cannot be shared across parents (it is stolen
    # from the first).
    #
    # `exclude_craft` selects the two arcade draw routines: the DEFAULT (`gen_rnd_spriteY` 5155-5169,
    # Toroid/Terrazi) ALSO rejects any column within SPAWN_CRAFT_GAP of the craft; `exclude_craft=False`
    # is the Kapi draw (`gen_random_Y_store_obj` 5147-5154) — the SAME in-range clamp with NO
    # craft-proximity reject, so a Kapi can spawn directly over the craft's column.
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
    accept_body = [
        _set_cur_item(
            blocks,
            "slot y",
            SLOT_Y_ID,
            blocks.op_mul(blocks.op_add(blocks.op_mod(variable("rng out", RNG_OUT_ID), number(SPAWN_COL_MASK + 1)), number(SPAWN_COL_OFFSET)), number(SLOT_UNITS_PER_CELL)),
        ),
        blocks.set_var("spawn found", SPAWN_FOUND_ID, number(1)),
    ]
    if exclude_craft:
        col = blocks.op_add(blocks.op_mod(variable("rng out", RNG_OUT_ID), number(SPAWN_COL_MASK + 1)), number(SPAWN_COL_OFFSET))
        far_enough = blocks.op_not(
            blocks.op_lt(
                blocks.op_abs(blocks.op_sub(variable("player col", PLAYER_COL_ID), col)),
                number(SPAWN_CRAFT_GAP),
            )
        )
        inner = [blocks.if_reporter(far_enough, accept_body)]
    else:
        inner = accept_body
    valid = blocks.if_reporter(in_range, inner)
    blocks.substack(
        draw_loop,
        [
            blocks.call_proc(RNG_PROCCODE, warp=True),
            valid,
            blocks.change_var("spawn attempts", SPAWN_ATTEMPTS_ID, 1),
        ],
    )
    return reset, draw_loop


def install_init_toroid(blocks: Blocks) -> None:
    # AIR-01: initialize the flying slot at `slot index` as a Toroid of type `walk type`. Draw a
    # lateral spawn column from the shared stream (reject-and-redraw, bounded, `_draw_spawn_column`);
    # on a successful draw, stamp the slot occupied and aim it at the craft on the 24-magnitude tier. On
    # exhaustion, leave the slot empty (retried next tick) — the recorded bounded-draw deviation. The
    # scroll-axis position is NOT set: a refill inherits the previous occupant's row (coded), 0 at cold
    # start.
    definition = _install_warp_proc(blocks, INIT_TOROID_PROCCODE)
    reset, draw_loop = _draw_spawn_column(blocks)
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
        # Rebuild each delta FRESH per comparison — a reporter block attaches to only one parent, so
        # sharing one `d_y`/`d_x` block across `<` and `>` lets the second steal it from the first,
        # leaving the lower bound with an empty operand (a dead bound that made a shot hit any enemy
        # in its row/column regardless of the other axis). Lambdas give every compare its own subtree.
        d_y = lambda: blocks.op_sub(sh(shot_x(s)), sh(_cur_item(blocks, "slot x", SLOT_X_ID)))
        d_x = lambda: blocks.op_sub(sh(_cur_item(blocks, "slot y", SLOT_Y_ID)), sh(shot_y(s)))
        hit_y = blocks.op_and(
            blocks.op_not(blocks.op_lt(d_y(), number(dy_low))),
            blocks.op_not(blocks.op_gt(d_y(), number(dy_high))),
        )
        hit_x = blocks.op_and(
            blocks.op_not(blocks.op_lt(d_x(), number(dx_low))),
            blocks.op_not(blocks.op_gt(d_x(), number(dx_high))),
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


def install_check_ground_hit(blocks: Blocks) -> None:
    # GND-05 / ECO-01: test every ground object against the locked bomb target and score each one the
    # bomb lands on, type-agnostically. Mirrors the reference's `handle_bombed_obj_and_award_points`
    # ($19EE): when a bomb finishes, loop the 16 ground objects, and for each ACTIVE one on target set
    # its state HIT and add its point value from the value table. So this sweeps GROUND_SLOTS (1-16)
    # INTERNALLY (unrolled, one gate per slot) against the fixed bomb-target slot, rather than being
    # called once per slot like the air detector. Each on-target ACTIVE slot resolves independently:
    # `resolve hit` marks it HIT (so it can't re-score) and routes to the single `score` path, exactly
    # as the arcade awards per on-target object. The window is the reference's shadow-MSB compare
    # (`check_object_on_target` $1A3D): each position floored to its half-px shadow MSB, then the
    # (bias,width) window HIT_WINDOW_BOMB_GROUND on the exact half-px delta (no mod-256 wrap). The
    # blaster deliberately cannot reach ground objects, so install_check_air_hit is left untouched.
    # No caller yet: Commit 4's bomb-finish (`check_bomb_finished` $190B) will invoke it; the harness
    # proves it directly this commit.
    definition = _install_warp_proc(blocks, CHECK_GROUND_HIT_PROCCODE)
    y_bias, y_width, x_bias, x_width = HIT_WINDOW_BOMB_GROUND
    dy_low, dy_high = -y_bias, y_width - y_bias - 1
    dx_low, dx_high = -x_bias, x_width - x_bias - 1
    sh = lambda expr: blocks.op_floor(blocks.op_div(expr, number(SLOT_UNITS_PER_SHADOW)))
    # Every position reporter is a FRESH-per-call lambda (the bomb target too): a reporter attaches to
    # only one parent, so a single shared `target x`/`target y` block would be stolen by its second use
    # across the 16 slots and the two axis compares, leaving dead operands (memory: reporter-steal).
    target_x = lambda: blocks.list_item("slot x", SLOT_X_ID, number(BOMB_TARGET_SLOT))
    target_y = lambda: blocks.list_item("slot y", SLOT_Y_ID, number(BOMB_TARGET_SLOT))
    obj_x = lambda s: blocks.list_item("slot x", SLOT_X_ID, number(s))
    obj_y = lambda s: blocks.list_item("slot y", SLOT_Y_ID, number(s))

    body: list[str] = []
    for s in range(GROUND_SLOTS[0], GROUND_SLOTS[1] + 1):
        # Only ACTIVE ground objects score. The reference gates on `_STATE==2` (active); the port maps
        # that active state to SLOT_ACTIVE. The Garu Barra base (Commit 6) is stamped a non-ACTIVE
        # sentinel so this same gate excludes it for free (the arcade's indestructible outer, `_STATE==3`).
        obj_live = blocks.op_eq(
            blocks.list_item("slot state", SLOT_STATE_ID, number(s)), number(SLOT_ACTIVE)
        )
        # Rebuild each delta FRESH per comparison — a reporter attaches to only one parent, so sharing
        # one d_y/d_x across `<` and `>` lets the second steal it from the first, leaving a dead bound
        # (memory: dsl-reporter-single-parent-steal). Lambdas give every compare its own subtree. The
        # bomb target is the reference; the swept object is slot s, matching the reference's delta
        # directions (scroll axis target-obj; lateral axis obj-target).
        d_y = lambda: blocks.op_sub(sh(target_x()), sh(obj_x(s)))
        d_x = lambda: blocks.op_sub(sh(obj_y(s)), sh(target_y()))
        hit_y = blocks.op_and(
            blocks.op_not(blocks.op_lt(d_y(), number(dy_low))),
            blocks.op_not(blocks.op_gt(d_y(), number(dy_high))),
        )
        hit_x = blocks.op_and(
            blocks.op_not(blocks.op_lt(d_x(), number(dx_low))),
            blocks.op_not(blocks.op_gt(d_x(), number(dx_high))),
        )
        overlap = blocks.op_and(obj_live, blocks.op_and(hit_y, hit_x))
        body.append(
            blocks.if_reporter(
                overlap,
                [
                    blocks.set_var("hit slot", HIT_SLOT_ID, number(s)),
                    blocks.set_var_expr(
                        "award value",
                        AWARD_VALUE_ID,
                        blocks.list_item(
                            "value table",
                            VALUE_TABLE_ID,
                            blocks.list_item("slot pts", SLOT_PTS_ID, number(s)),
                        ),
                    ),
                    blocks.call_proc(RESOLVE_HIT_PROCCODE, warp=True),
                    # `resolve hit` marks the slot HIT and scores but does NOT touch the slot timer.
                    # Reset it here (mirroring the air detector at install_check_air_hit) so the ground
                    # explosion clock — floor(slot timer / 8) through the 7 burst frames, then the
                    # persistent crater — starts from 0 on the tick of the hit. The renderer reads this
                    # same timer; the walk's update-barra advances it (TICK_TIMER_STEP/tick).
                    blocks.list_replace("slot timer", SLOT_TIMER_ID, number(s), number(0)),
                ],
            )
        )
    blocks.chain(definition, body)


def install_track_crosshair(blocks: Blocks) -> None:
    # WPN-04 bomb sight (update_crosshair $16E8): every tick the crosshair leads the craft by a fixed
    # depth offset — arcade `crosshair _X = solvalou_X + 0xF400`, `_Y = solvalou_Y`. `read player cell`
    # has already cached the craft's (row, col) in cells, so the crosshair slot is (row*256 + LEAD, col*256)
    # in slot units; the renderer maps that to 96 stage-px ahead of the ship on the same lateral column.
    # The crosshair carries no gameplay state — it is only marked ACTIVE so its renderer shows it while
    # playing. The reference's on-target colour flash (check_targeted_ground_object) is a deferred cosmetic.
    definition = _install_warp_proc(blocks, TRACK_CROSSHAIR_PROCCODE)
    set_x = blocks.list_replace(
        "slot x",
        SLOT_X_ID,
        number(CROSSHAIR_SLOT),
        blocks.op_add(
            blocks.op_mul(variable("player row", PLAYER_ROW_ID), number(SLOT_UNITS_PER_CELL)),
            number(BOMB_TARGET_LEAD),
        ),
    )
    set_y = blocks.list_replace(
        "slot y",
        SLOT_Y_ID,
        number(CROSSHAIR_SLOT),
        blocks.op_mul(variable("player col", PLAYER_COL_ID), number(SLOT_UNITS_PER_CELL)),
    )
    set_state = blocks.list_replace(
        "slot state", SLOT_STATE_ID, number(CROSSHAIR_SLOT), number(SLOT_ACTIVE)
    )
    blocks.chain(definition, [set_x, set_y, set_state])


def install_advance_bomb(blocks: Blocks) -> None:
    # WPN-04 bombing, one tick of the bomb weapon (init_bombing $188C, check_bomb_finished $190B).
    # ARM (mirroring init_bombing's `bomb target idle` gate): on a fresh B press while no bomb is in
    # flight, lock the bomb target at the crosshair (target _X/_Y = crosshair _X/_Y) and drop the bomb
    # from the craft (bomb _X/_Y = solvalou _X/_Y) with zero velocity. Otherwise ADVANCE the in-flight
    # bomb by FRAMES_PER_TICK arcade-frame sub-steps (a port tick is two frames): each sub-step
    # accelerates the bomb toward the terrain (`_dX -= 2; _X += _dX*2`, so the bomb's slot x DECREASES —
    # up-screen, ahead) while the bomb target scrolls DOWN with the terrain (+SCROLL_UNITS_PER_FRAME,
    # the same scroll_delta the ground uses). The bomb finishes the sub-step the target catches it on the
    # depth axis (`target_x >= bomb_x`, check_bomb_finished's not-borrow) — which resolves every ground
    # object under the target (`check ground hit`) and clears the weapon. Arm and advance are the two
    # arms of one if/else, so arming costs no advance on its own tick (init_bombing sets up, then the
    # bomb-active block runs on the following frames). Sounds are fired to the bomb renderer as broadcasts.
    definition = _install_warp_proc(blocks, ADVANCE_BOMB_PROCCODE)
    crosshair_x = lambda: blocks.list_item("slot x", SLOT_X_ID, number(CROSSHAIR_SLOT))
    crosshair_y = lambda: blocks.list_item("slot y", SLOT_Y_ID, number(CROSSHAIR_SLOT))
    bomb_x = lambda: blocks.list_item("slot x", SLOT_X_ID, number(BOMB_SLOT))
    target_x = lambda: blocks.list_item("slot x", SLOT_X_ID, number(BOMB_TARGET_SLOT))

    # arm gate: (key b pressed) AND (bomb in flight == 0). Build the AND first, then wire its two
    # boolean children into OPERAND1/OPERAND2 (the bomb_blocks arm-gate idiom), so neither reporter is
    # re-parented out from under it.
    arm_gate = blocks.add("control_if_else")
    pressed_and_idle = blocks.add("operator_and")
    b_pressed = blocks.key_pressed(pressed_and_idle, "b")
    idle = blocks.var_equals(pressed_and_idle, "bomb in flight", BOMB_INFLIGHT_ID, 0)
    blocks.blocks[pressed_and_idle]["inputs"] = {
        "OPERAND1": [2, b_pressed],
        "OPERAND2": [2, idle],
    }
    blocks.blocks[arm_gate]["inputs"]["CONDITION"] = [2, pressed_and_idle]
    arm_body = [
        # bomb drops from the craft (solvalou depth/lateral), zero velocity.
        blocks.list_replace(
            "slot x",
            SLOT_X_ID,
            number(BOMB_SLOT),
            blocks.op_mul(variable("player row", PLAYER_ROW_ID), number(SLOT_UNITS_PER_CELL)),
        ),
        blocks.list_replace(
            "slot y",
            SLOT_Y_ID,
            number(BOMB_SLOT),
            blocks.op_mul(variable("player col", PLAYER_COL_ID), number(SLOT_UNITS_PER_CELL)),
        ),
        # bomb target locks at the crosshair's current lead position.
        blocks.list_replace("slot x", SLOT_X_ID, number(BOMB_TARGET_SLOT), crosshair_x()),
        blocks.list_replace("slot y", SLOT_Y_ID, number(BOMB_TARGET_SLOT), crosshair_y()),
        blocks.set_var("bomb dx", BOMB_DX_ID, number(0)),
        blocks.list_replace("slot state", SLOT_STATE_ID, number(BOMB_SLOT), number(SLOT_ACTIVE)),
        blocks.list_replace(
            "slot state", SLOT_STATE_ID, number(BOMB_TARGET_SLOT), number(SLOT_ACTIVE)
        ),
        blocks.set_var("bomb in flight", BOMB_INFLIGHT_ID, number(1)),
        blocks.send("bomb"),  # drop sound (bomb renderer)
    ]
    blocks.substack(arm_gate, arm_body)

    # advance branch (SUBSTACK2): fly the in-flight bomb two arcade-frame sub-steps this tick.
    substep_guard = blocks.add("control_if")
    still_flying = blocks.var_equals(substep_guard, "bomb in flight", BOMB_INFLIGHT_ID, 1)
    blocks.blocks[substep_guard]["inputs"]["CONDITION"] = [2, still_flying]
    # target_x >= bomb_x  <=>  NOT (target_x < bomb_x)   (check_bomb_finished's not-borrow)
    finished = blocks.op_not(blocks.op_lt(target_x(), bomb_x()))
    finish_body = [
        blocks.call_proc(CHECK_GROUND_HIT_PROCCODE, warp=True),
        blocks.list_replace("slot state", SLOT_STATE_ID, number(BOMB_SLOT), number(0)),
        blocks.list_replace("slot state", SLOT_STATE_ID, number(BOMB_TARGET_SLOT), number(0)),
        blocks.set_var("bomb in flight", BOMB_INFLIGHT_ID, number(0)),
        blocks.send("bomb landed"),  # explosion sound (bomb renderer)
    ]
    substep_body = [
        blocks.set_var_expr(
            "bomb dx",
            BOMB_DX_ID,
            blocks.op_sub(variable("bomb dx", BOMB_DX_ID), number(BOMB_ACCEL_PER_FRAME)),
        ),
        blocks.list_replace(
            "slot x",
            SLOT_X_ID,
            number(BOMB_SLOT),
            blocks.op_add(bomb_x(), blocks.op_mul(variable("bomb dx", BOMB_DX_ID), number(2))),
        ),
        blocks.list_replace(
            "slot x",
            SLOT_X_ID,
            number(BOMB_TARGET_SLOT),
            blocks.op_add(target_x(), number(SCROLL_UNITS_PER_FRAME)),
        ),
        blocks.if_reporter(finished, finish_body),
    ]
    blocks.substack(substep_guard, substep_body)
    flight_loop = blocks.add("control_repeat", inputs={"TIMES": number(FRAMES_PER_TICK)})
    blocks.substack(flight_loop, [substep_guard])
    # the whole advance runs only when a bomb is actually in flight (the else arm can be reached with
    # no bomb armed — B not pressed and none flying).
    advance_gate = blocks.add("control_if")
    in_flight = blocks.var_equals(advance_gate, "bomb in flight", BOMB_INFLIGHT_ID, 1)
    blocks.blocks[advance_gate]["inputs"]["CONDITION"] = [2, in_flight]
    blocks.substack(advance_gate, [flight_loop])
    blocks.substack(arm_gate, [advance_gate], name="SUBSTACK2")

    blocks.chain(definition, [arm_gate])


def install_advance_ground(blocks: Blocks) -> None:
    # GND (area.ground-dispatch #69): one tick of a spawned ground object at `slot index` — the first
    # TERRAIN-LOCKED scroller. A flying enemy moves by its own (slot dx, slot dy); a ground object is
    # glued to the terrain, so each tick it only advances its scroll-axis position by AREA_PROGRESS_STEP
    # (scroll_sprite_X $30E8: scroll_delta -8 doubled -> +16 units/arcade-frame = +32/tick), moving DOWN
    # the field toward the craft (higher slot x = further down). It is then culled once it scrolls off the
    # bottom (row >= CULL_ROW_MAX); a ground object never leaves the top or sides, so only the bottom edge
    # is tested. Per-family behaviour (Barra's crater on hit, Logram's open/close + fire) layers on top in
    # Commits 6-7; the scroll+cull here is shared by every ground family.
    definition = _install_warp_proc(blocks, ADVANCE_GROUND_PROCCODE)
    scroll = _set_cur_item(
        blocks,
        "slot x",
        SLOT_X_ID,
        blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), number(AREA_PROGRESS_STEP)),
    )
    off_bottom = blocks.op_not(blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MAX)))
    cull = blocks.if_reporter(off_bottom, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    blocks.chain(definition, [scroll, cull])


def install_update_barra(blocks: Blocks) -> None:
    # GND-01 / ground.barra (#70): one tick of a Barra at `slot index`. A Barra never fires and never
    # moves under its own power — it is glued to the terrain and scrolls with it — so both an ACTIVE
    # (idle pyramid) and a HIT (exploding, then a persistent crater) Barra share the terrain scroll +
    # off-field cull of `advance ground`. The ONLY per-state difference is the explosion clock: a HIT
    # Barra advances `slot timer` (TICK_TIMER_STEP/tick) so the renderer can walk it through the 7
    # bomb-explosion burst frames (handle_bomb_explosion $3186, floor(timer/8)) and then flip to the
    # flickering crater (bomb_explosion_finished $31D7). UNLIKE the flying explosion (install_explode
    # _toroid_tick), the crater is NEVER freed on its clock — the arcade crater scrolls forever until it
    # leaves the field, so `advance ground`'s bottom-edge cull is its only removal path. The detector
    # zeroed `slot timer` on the tick of the hit, so the clock starts from 0.
    definition = _install_warp_proc(blocks, UPDATE_BARRA_PROCCODE)
    tick_clock = _set_cur_item(
        blocks,
        "slot timer",
        SLOT_TIMER_ID,
        blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP)),
    )
    top = blocks.add("control_if_else")
    is_hit = blocks.op_eq(_cur_item(blocks, "slot state", SLOT_STATE_ID), number(SLOT_HIT))
    blocks.blocks[top]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = top
    blocks.substack(top, [tick_clock, blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)])
    blocks.substack(
        top, [blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)], name="SUBSTACK2"
    )
    blocks.chain(definition, [top])


def install_update_garu(blocks: Blocks) -> None:
    # GND-01 / ground.barra (#70): one tick of a Garu Barra part at `slot index`. A Garu is two adjacent
    # slots sharing GARU_BARRA_TYPE: the indestructible 2x2 base (state SLOT_GARU_BASE) and the
    # destructible node (state ACTIVE, then HIT once bombed). Both are terrain-locked and just scroll —
    # so the base and an un-bombed node both fall through to the shared `advance ground` scroll + off-field
    # cull. The ONE per-state difference is the node's death: mirroring explode_and_remove_object ($3216),
    # a HIT node advances its burst clock (`slot timer`, zeroed by the detector) and, once the burst
    # finishes (timer >= GARU_REMOVE_FRAMES = frame 7), REMOVES itself — it vanishes with no crater, unlike
    # the Barra. While the burst still plays it keeps scrolling (the arcade calls scroll_sprite_X each
    # step), so a mid-field kill drifts with the terrain until it pops.
    definition = _install_warp_proc(blocks, UPDATE_GARU_PROCCODE)
    top = blocks.add("control_if_else")
    is_hit = blocks.op_eq(_cur_item(blocks, "slot state", SLOT_STATE_ID), number(SLOT_HIT))
    blocks.blocks[top]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = top
    tick_clock = _set_cur_item(
        blocks,
        "slot timer",
        SLOT_TIMER_ID,
        blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP)),
    )
    done = blocks.op_not(
        blocks.op_lt(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(GARU_REMOVE_FRAMES))
    )
    finish = blocks.add("control_if_else")
    blocks.blocks[finish]["inputs"]["CONDITION"] = [2, done]
    blocks.blocks[done]["parent"] = finish
    blocks.substack(finish, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    blocks.substack(
        finish, [blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)], name="SUBSTACK2"
    )
    # HIT node: advance the burst clock, then either remove (burst done) or keep scrolling.
    blocks.substack(top, [tick_clock, finish])
    # Base (sentinel) or ACTIVE node: just the shared terrain scroll + off-field cull.
    blocks.substack(
        top, [blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)], name="SUBSTACK2"
    )
    blocks.chain(definition, [top])


def install_update_logram(blocks: Blocks) -> None:
    # GND-01 / ground.logram (#71): one tick of a Logram at `slot index`, mirroring handle_logram_main
    # ($1B64). A Logram is terrain-locked (it only ever scrolls) and, once bombed (state HIT), craters
    # PERSISTENTLY exactly like the Barra (handle_bomb_explosion $3186 — the SAME routine, NOT the Garu
    # node's explode-and-remove), so its HIT branch is identical to `update barra`: advance the crater
    # clock (`slot timer`, zeroed by the detector at the hit) and scroll. Its ACTIVE behaviour is the
    # open/close + single-shot cycle, gated two ways before the timer advances:
    #   * ARMING — only while the object is still high enough on the field (`cur_row <= ground stop firing
    #     row`); below that the arcade takes handle_logram_exit ($1BDC) and only scrolls (`jcs` at $1B77).
    #   * CADENCE — the whole timer advances on the arcade's every-8th-frame phase (`countup_timer_1 & 7`),
    #     i.e. every 4th tick here (FIRE_GATE_PHASE_TICKS; 1 tick = 2 arcade frames).
    # Then two phases by `slot flag`: WAIT counts `slot fire timer` DOWN (the masked-random delay); on
    # hitting 0 it flips to ANIMATE and runs one animate step the SAME tick (the arcade's fall-through
    # past SET_REENTRY_ADDR_HERE). ANIMATE counts `slot fire timer` UP, fires ONE aimed bullet at 12
    # (stage 3, dome fully open), writes the dome costume ordinal for the stage, and at stage 7 re-rolls a
    # fresh wait and returns to WAIT (holding the closed dome). `advance ground` (scroll + off-field cull)
    # runs EVERY tick in both states — the arcade scrolls the sprite on every frame regardless of phase.
    definition = _install_warp_proc(blocks, UPDATE_LOGRAM_PROCCODE)
    fire_timer = lambda: _cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID)

    def animate_step() -> list[str]:
        # One ANIMATE-phase step. Built FRESH each call (used both at the WAIT->ANIMATE fall-through and in
        # the steady ANIMATE branch), so no reporter or statement block is shared between two parents.
        inc = _set_cur_item(
            blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_add(fire_timer(), number(1))
        )
        fire = blocks.if_reporter(
            blocks.op_eq(fire_timer(), number(LOGRAM_FIRE_TIMER)), _fire_aimed_bullet(blocks)
        )
        stage = lambda: blocks.op_mod(
            blocks.op_floor(blocks.op_div(fire_timer(), number(LOGRAM_STAGE_PHASE))),
            number(LOGRAM_STAGE_MOD),
        )
        recycle = blocks.add("control_if_else")
        at_recycle = blocks.op_eq(stage(), number(LOGRAM_RECYCLE_STAGE))
        blocks.blocks[recycle]["inputs"]["CONDITION"] = [2, at_recycle]
        blocks.blocks[at_recycle]["parent"] = recycle
        # Stage 7: re-roll the masked-random wait (start_logram_shot_timer $1BC9) and return to WAIT. The
        # dome costume is NOT touched, so it holds the stage-6 closed frame through the wait.
        blocks.substack(
            recycle,
            [
                blocks.call_proc(RNG_PROCCODE, warp=True),
                _set_cur_item(
                    blocks,
                    "slot fire timer",
                    SLOT_FIRE_TIMER_ID,
                    blocks.op_add(
                        blocks.op_mod(
                            variable("rng out", RNG_OUT_ID),
                            blocks.op_add(
                                _cur_item(blocks, "slot fire mask", SLOT_FIRE_MASK_ID), number(1)
                            ),
                        ),
                        number(1),
                    ),
                ),
                _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(LOGRAM_WAIT_PHASE)),
            ],
        )
        # Stages 0..6: dome ordinal = LOGRAM_OPEN_FRAME_COUNT - |stage - peak|, the triangle {1,2,3,4,3,2,1}.
        dome_ordinal = blocks.op_sub(
            number(LOGRAM_OPEN_FRAME_COUNT),
            blocks.op_abs(blocks.op_sub(stage(), number(LOGRAM_STAGE_PEAK))),
        )
        blocks.substack(
            recycle, [_set_cur_item(blocks, "slot code", SLOT_CODE_ID, dome_ordinal)], name="SUBSTACK2"
        )
        return [inc, fire, recycle]

    # WAIT vs ANIMATE (by slot flag).
    phase = blocks.add("control_if_else")
    in_wait = blocks.op_eq(_cur_item(blocks, "slot flag", SLOT_FLAG_ID), number(LOGRAM_WAIT_PHASE))
    blocks.blocks[phase]["inputs"]["CONDITION"] = [2, in_wait]
    blocks.blocks[in_wait]["parent"] = phase
    # WAIT: decrement the delay; when it reaches 0, enter ANIMATE and run one animate step this tick
    # (arcade `subq #1,_TIMER; jne set_logram_colour`, then the fall-through animate block).
    dec = _set_cur_item(
        blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_sub(fire_timer(), number(1))
    )
    transition = blocks.if_reporter(
        blocks.op_eq(fire_timer(), number(0)),
        [_set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(LOGRAM_ANIMATE_PHASE)), *animate_step()],
    )
    blocks.substack(phase, [dec, transition])
    blocks.substack(phase, animate_step(), name="SUBSTACK2")

    # ARMING + CADENCE gate wraps ONLY the timer phase; the scroll below always runs.
    armed = blocks.op_not(
        blocks.op_gt(_cur_row(blocks), variable("ground stop firing row", GROUND_STOP_FIRING_ROW_ID))
    )
    on_phase = blocks.op_eq(
        blocks.op_mod(variable("tick", TICK_ID), number(FIRE_GATE_PHASE_TICKS)), number(0)
    )
    arm = blocks.if_reporter(blocks.op_and(armed, on_phase), [phase])

    # HIT: the Barra crater clock; else the ACTIVE arm/animate. Both then scroll + cull via advance ground.
    tick_clock = _set_cur_item(
        blocks,
        "slot timer",
        SLOT_TIMER_ID,
        blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP)),
    )
    top = blocks.add("control_if_else")
    is_hit = blocks.op_eq(_cur_item(blocks, "slot state", SLOT_STATE_ID), number(SLOT_HIT))
    blocks.blocks[top]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = top
    blocks.substack(top, [tick_clock, blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)])
    blocks.substack(
        top, [arm, blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)], name="SUBSTACK2"
    )
    blocks.chain(definition, [top])


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


def _fire_aimed_bullet(blocks: Blocks) -> list[str]:
    # AIR-12: fire ONE aimed generic bullet from the current slot — the shared aim+alloc body reused by
    # every firing path. Allocate an idle bullet slot; on success, place the bullet at the slot's cell
    # and aim it at the craft's current cell on the 32-magnitude tier (the reference's generic 2 px/frame
    # bullet table, `init_new_bullet`). This is only the WHAT-to-fire; the WHEN is the caller's:
    #  - the shooting Toroid (type 0x0B) fires this once, event-driven, at its swing commit (a distinct,
    #    non-periodic firing model — a documented exception to the fire-permission gate, recorded in 027);
    #  - the fire-permission gate (`install_fire_permission_gate`) fires this periodically under the
    #    family mask. Neither consults a mask HERE; the gate owns the rate.
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
        _fire_aimed_bullet(blocks),
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


def install_init_terrazi(blocks: Blocks) -> None:
    # AIR-06: initialize the flying slot at `slot index` as a Terrazi of type `walk type`
    # (handle_11_Terrazi 3667-3679). Same bounded lateral spawn-column draw and top-row entry as the
    # Toroid (init_toroid's recorded deviations apply unchanged), but aimed on the 48-magnitude tier
    # (3 px/frame, `angle_dX_dY_terrazi_torkan_tbl`) and stamped with the Terrazi's points/flag/code.
    # It also captures the fire-permission state (`_FFREQ`/`_TIMER` at 3674-3678) into the two shared
    # per-slot fire fields, so a freshly spawned Terrazi carries its own countdown for the shared gate.
    definition = _install_warp_proc(blocks, INIT_TERRAZI_PROCCODE)
    reset, draw_loop = _draw_spawn_column(blocks)
    # On a successful draw, stamp the slot and aim it at the craft (48-magnitude tier, 3 px/frame).
    stamp = blocks.if_reporter(
        blocks.op_eq(variable("spawn found", SPAWN_FOUND_ID), number(1)),
        [
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, variable("walk type", WALK_TYPE_ID)),
            _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_ACTIVE)),
            # Enter from the TOP row, like the Toroid: this self-propelled port has no enemy scroll, so
            # the scroll row is reset before aiming so every wave streams in from the top with room to
            # reach its glide window (the same recorded deviation as install_init_toroid).
            _set_cur_item(blocks, "slot x", SLOT_X_ID, number(TOROID_SPAWN_ROW * SLOT_UNITS_PER_CELL)),
            blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
            blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
            blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 48", AIM_DX_48_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 48", AIM_DY_48_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
            _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(TERRAZI_FLAG_APPROACH)),
            _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(TERRAZI_INIT_CODE)),
            _set_cur_item(blocks, "slot pts", SLOT_PTS_ID, number(TERRAZI_PTS)),
            # Fire-permission capture-at-spawn (3674-3678): snapshot the family mask into the per-slot
            # field, then seed the fire countdown to `rng & mask` (NO +1 at spawn — the spawn/reload
            # asymmetry; the reload adds 1). A fresh RNG draw, after the spawn-column draws, in walk
            # order. `rng & mask` is `rng mod (mask+1)` for the contiguous fire-frequency mask.
            _set_cur_item(blocks, "slot fire mask", SLOT_FIRE_MASK_ID, variable("fire mask terrazi", FIRE_MASK_TERRAZI_ID)),
            blocks.call_proc(RNG_PROCCODE, warp=True),
            _set_cur_item(
                blocks,
                "slot fire timer",
                SLOT_FIRE_TIMER_ID,
                blocks.op_mod(
                    variable("rng out", RNG_OUT_ID),
                    blocks.op_add(_cur_item(blocks, "slot fire mask", SLOT_FIRE_MASK_ID), number(1)),
                ),
            ),
        ],
    )
    blocks.chain(definition, [*reset, draw_loop, stamp])


def install_update_terrazi(blocks: Blocks) -> None:
    # AIR-06: advance the Terrazi at `slot index` by one tick (handle_11_Terrazi 3680-3729). While
    # APPROACHING it flies on its aimed 3 px/frame velocity; each tick it tests the LATERAL window
    # (player col - slot col in [LOW, HIGH], the reference's carry test on `_Y` at 3683-3692 — `_Y` is
    # the lateral axis, `dir_delta_tbl` 2172; the same axis the Toroid swing triggers on). Outside the
    # window it keeps approaching and fires under its mask. Inside the window it commits to a GLIDE
    # (`terrazi_main_cont`): the LATERAL velocity is SET to a slow +/-2 drift by side (`_dY` 3694-3699)
    # and, while gliding, the SCROLL/forward velocity decelerates by DECEL each tick (`subq #2,_dX`
    # 3715), crossing zero and reversing over ~24 frames so it peels its forward approach away. Once
    # committed it never re-tests. Shares the flying hit window / explosion with the Toroid. Sprite-code
    # animation (`_ddX` at 3705-3711) is derived render-only from the slot clock.
    definition = _install_warp_proc(blocks, UPDATE_TERRAZI_PROCCODE)
    flag = lambda: _cur_item(blocks, "slot flag", SLOT_FLAG_ID)
    col_offset = lambda: blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))

    # Glide trigger (only while approaching): LOW <= lateral offset <= HIGH -> commit the glide. The
    # lateral drift sign mirrors the reference (3694-3699): craft at/right laterally (offset >= 0) drifts
    # one way (-DRIFT); craft left (offset < 0) drifts the other (+DRIFT).
    at_or_above_low = blocks.op_not(blocks.op_lt(col_offset(), number(TERRAZI_GLIDE_LOW)))
    at_or_below_high = blocks.op_not(blocks.op_gt(col_offset(), number(TERRAZI_GLIDE_HIGH)))
    in_window = blocks.op_and(at_or_above_low, at_or_below_high)
    drift = blocks.add("control_if_else")
    craft_at_or_right = blocks.op_not(blocks.op_lt(col_offset(), number(0)))  # offset >= 0
    blocks.blocks[drift]["inputs"]["CONDITION"] = [2, craft_at_or_right]
    blocks.blocks[craft_at_or_right]["parent"] = drift
    blocks.substack(drift, [_set_cur_item(blocks, "slot dy", SLOT_DY_ID, number(-TERRAZI_GLIDE_DRIFT))])
    blocks.substack(drift, [_set_cur_item(blocks, "slot dy", SLOT_DY_ID, number(TERRAZI_GLIDE_DRIFT))], name="SUBSTACK2")
    trigger = blocks.if_reporter(
        blocks.op_and(blocks.op_eq(flag(), number(TERRAZI_FLAG_APPROACH)), in_window),
        [
            drift,
            _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(TERRAZI_FLAG_GLIDE)),
            # Suppress fire during the glide (the reference sets `_TIMER = 0xff` at 3699): the gate,
            # still called each tick, counts down from 255 and won't reach 0 in the enemy's short life.
            _set_cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, number(TERRAZI_FIRE_SUPPRESS)),
        ],
    )
    # While gliding, decelerate and reverse the SCROLL/forward velocity (`subq #2,_dX`); the lateral
    # drift set at the trigger holds (the reference's `_dY += _ddY` with _ddY = 0). Runs on the trigger
    # tick too, matching the reference's fall-through from the glide entry into the same-frame decel/move.
    glide = blocks.if_reporter(
        blocks.op_eq(flag(), number(TERRAZI_FLAG_GLIDE)),
        [_set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.op_sub(_cur_item(blocks, "slot dx", SLOT_DX_ID), number(TERRAZI_GLIDE_DECEL)))],
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
    # The glide reverses the Terrazi's forward motion (it can back off the TOP) and its lateral drift can
    # carry it off a side; the same explicit four-edge cull as the Toroid (this port's signed columns
    # need the left edge that the reference's byte-wrap handles implicitly).
    offscreen = blocks.op_or(blocks.op_or(off_bottom, off_top), blocks.op_or(off_right, off_left))
    cull = blocks.if_reporter(offscreen, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    state = lambda: _cur_item(blocks, "slot state", SLOT_STATE_ID)
    # PLY-02: an active Terrazi touching the craft's cell kills it (raises `player hit`), checked at the
    # tick-start position before it moves or culls — the shared flying-vs-craft window.
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks), [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))]
    )
    # Periodic masked fire (the reference calls `chk_timer_fire_bullet_reinit_timer` on both the distant
    # and glide paths): the shared gate is called each active tick. While distant it fires under the
    # captured mask; during the glide the fire countdown is pinned to 255, so it stays silent.
    fire = blocks.call_proc(FIRE_GATE_PROCCODE, warp=True)
    normal = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_ACTIVE)),
        [craft_hit, trigger, glide, fire, *move, cull],
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


def install_init_kapi(blocks: Blocks) -> None:
    # AIR-05: initialize the flying slot at `slot index` as a Kapi of type `walk type` (handle_10_Kapi
    # 3602-3618). Same top-row entry as the other flying families, but with a PLAIN spawn-column draw —
    # NO craft-proximity exclusion (`gen_random_Y_store_obj` 3605 / 5147-5154, so a Kapi can spawn
    # directly over the craft's column, unlike the Toroid/Terrazi) — and aimed on the 32-magnitude
    # generic tier (2 px/frame, `angle_dX_dY_tbl` 6360). Stamps the Kapi's points/flag/code, captures
    # the Kapi fire mask for the dive's gate, and seeds the initial approach delay (48-111 frames, the
    # recorded fire-delay deviation) into `slot fire timer` — the reference reuses `_TIMER` for the
    # approach delay (3616-3618) and Kapi never calls the gate while approaching, so the field is free.
    definition = _install_warp_proc(blocks, INIT_KAPI_PROCCODE)
    reset, draw_loop = _draw_spawn_column(blocks, exclude_craft=False)
    stamp = blocks.if_reporter(
        blocks.op_eq(variable("spawn found", SPAWN_FOUND_ID), number(1)),
        [
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, variable("walk type", WALK_TYPE_ID)),
            _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_ACTIVE)),
            # Enter from the TOP row, like the Toroid/Terrazi (the same self-propelled-port deviation:
            # no enemy scroll, so every wave streams in from the top with room to aim and dive).
            _set_cur_item(blocks, "slot x", SLOT_X_ID, number(TOROID_SPAWN_ROW * SLOT_UNITS_PER_CELL)),
            blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
            blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
            blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 32", AIM_DX_32_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 32", AIM_DY_32_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
            _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(KAPI_FLAG_APPROACH)),
            _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(KAPI_INIT_CODE)),
            _set_cur_item(blocks, "slot pts", SLOT_PTS_ID, number(KAPI_PTS)),
            # Capture the Kapi fire mask for the dive's gate (the reference snapshots `_FFREQ` at spawn,
            # 3612). The dive consumes it; the approach never fires.
            _set_cur_item(blocks, "slot fire mask", SLOT_FIRE_MASK_ID, variable("fire mask kapi", FIRE_MASK_KAPI_ID)),
            # Approach delay 48-111 frames = (rng mod 64) + 48 (the F4 fire-delay deviation, carried
            # forward). A fresh RNG draw after the spawn-column draws, in walk order. Stored in `slot
            # fire timer` (the reference's `_TIMER` reuse) and counted down 2/tick during the approach.
            blocks.call_proc(RNG_PROCCODE, warp=True),
            _set_cur_item(
                blocks,
                "slot fire timer",
                SLOT_FIRE_TIMER_ID,
                blocks.op_add(
                    blocks.op_mod(variable("rng out", RNG_OUT_ID), number(KAPI_APPROACH_DELAY_SPAN)),
                    number(KAPI_APPROACH_DELAY_BASE),
                ),
            ),
        ],
    )
    blocks.chain(definition, [*reset, draw_loop, stamp])


def install_update_kapi(blocks: Blocks) -> None:
    # AIR-05: advance the Kapi at `slot index` by one tick (handle_10_Kapi 3602-3623, kapi_10_fire
    # 3624-3665). While APPROACHING it flies straight on its aimed 2 px/frame velocity and does NOT
    # fire; each tick it counts the initial delay (held in `slot fire timer`) down by 2 (2 arcade
    # frames/tick), and at <= 0 commits the dive: it latches the peel-away side ONCE (kapi_10_fire
    # 3626-3633), arms the fire gate, and resets the animation clock. While DIVING it accelerates the
    # LATERAL velocity by the latched +/-accel (peeling AWAY from the craft's column — the Toroid swing
    # kinematics, so it decelerates, crosses zero and reverses), decelerates the SCROLL velocity by
    # DECEL (`subq #2,_dX` 3651), and fires EVERY tick under the mask (no suppression, unlike Terrazi's
    # glide). Shares the flying hit window / explosion; the 7-code dive animation is derived render-only
    # from the slot clock.
    definition = _install_warp_proc(blocks, UPDATE_KAPI_PROCCODE)
    flag = lambda: _cur_item(blocks, "slot flag", SLOT_FLAG_ID)
    col_offset = lambda: blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))
    fire_timer = lambda: _cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID)

    # Latch the peel-away side (kapi_10_fire 3626-3633): `ddY` = sign of self._Y - solvalou._Y, i.e.
    # AWAY from the craft. In port terms craft at/right laterally (offset = player col - self col >= 0)
    # -> the aimed lateral velocity is positive, so DIVE_MINUS peels it back toward lower columns;
    # craft left (offset < 0) -> DIVE_PLUS. Identical side mapping to the Toroid swing. Latched once at
    # dive entry and NEVER recomputed (recomputing would re-home after the velocity crosses zero).
    side = blocks.add("control_if_else")
    craft_at_or_right = blocks.op_not(blocks.op_lt(col_offset(), number(0)))  # offset >= 0
    blocks.blocks[side]["inputs"]["CONDITION"] = [2, craft_at_or_right]
    blocks.blocks[craft_at_or_right]["parent"] = side
    blocks.substack(side, [_set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(KAPI_FLAG_DIVE_MINUS))])
    blocks.substack(side, [_set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(KAPI_FLAG_DIVE_PLUS))], name="SUBSTACK2")
    # Approach: count the initial delay down 2/tick; when it reaches <= 0, commit the dive. The delay
    # lives in `slot fire timer` (the reference's `_TIMER` reuse); `fire_timer()` re-reads the list, so
    # the trigger sees the just-decremented value.
    trigger = blocks.if_reporter(
        blocks.op_not(blocks.op_gt(fire_timer(), number(0))),  # slot fire timer <= 0
        [
            side,
            # Arm the fire gate for the dive (`move.b #1,_TIMER` 3634 -> fires on the next phase tick)
            # and reset the dive animation clock (`clr TIMER1` 3625).
            _set_cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, number(1)),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
        ],
    )
    approach = blocks.if_reporter(
        blocks.op_eq(flag(), number(KAPI_FLAG_APPROACH)),
        [
            _set_cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_sub(fire_timer(), number(TICK_TIMER_STEP))),
            trigger,
        ],
    )
    # Dive fire: the reference calls `chk_timer_fire_bullet_reinit_timer` every dive frame (3634/3638/
    # 3651/3663) and never suppresses. Gated to the dive so the approach stays silent; runs on the
    # trigger tick too (the flag was just flipped to a dive side), matching the reference's fall-through
    # from the dive entry into the same-frame fire.
    fire = blocks.if_reporter(
        blocks.op_not(blocks.op_eq(flag(), number(KAPI_FLAG_APPROACH))),
        [blocks.call_proc(FIRE_GATE_PROCCODE, warp=True)],
    )
    # Dive kinematics, by latched side: accelerate the LATERAL velocity away from the craft AND
    # decelerate the SCROLL/forward velocity (both `_dY += ddY` 3650 and `subq #2,_dX` 3651 run every
    # dive frame). Runs on the trigger tick too (fall-through).
    dive_minus = blocks.if_reporter(
        blocks.op_eq(flag(), number(KAPI_FLAG_DIVE_MINUS)),
        [
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.op_sub(_cur_item(blocks, "slot dy", SLOT_DY_ID), number(KAPI_DIVE_LATERAL_ACCEL))),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.op_sub(_cur_item(blocks, "slot dx", SLOT_DX_ID), number(KAPI_DIVE_SCROLL_DECEL))),
        ],
    )
    dive_plus = blocks.if_reporter(
        blocks.op_eq(flag(), number(KAPI_FLAG_DIVE_PLUS)),
        [
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.op_add(_cur_item(blocks, "slot dy", SLOT_DY_ID), number(KAPI_DIVE_LATERAL_ACCEL))),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.op_sub(_cur_item(blocks, "slot dx", SLOT_DX_ID), number(KAPI_DIVE_SCROLL_DECEL))),
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
    # The dive peels off a side and its scroll decel can carry it back off the TOP; the same explicit
    # four-edge cull as the Toroid/Terrazi (this port's signed columns need the left edge the
    # reference's byte-wrap handles implicitly).
    offscreen = blocks.op_or(blocks.op_or(off_bottom, off_top), blocks.op_or(off_right, off_left))
    cull = blocks.if_reporter(offscreen, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    state = lambda: _cur_item(blocks, "slot state", SLOT_STATE_ID)
    # PLY-02: an active Kapi touching the craft's cell kills it (raises `player hit`), checked at the
    # tick-start position before it moves or culls — the shared flying-vs-craft window.
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks), [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))]
    )
    # Ordered body: offer to the shot detector (via the wrapper below), then approach-countdown/trigger,
    # fire (dive only), the latched dive kinematics, move, cull. Fire precedes the kinematics so the
    # aimed bullet leaves from the slot's pre-move position, matching kapi_10_fire's order.
    normal = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_ACTIVE)),
        [craft_hit, approach, fire, dive_minus, dive_plus, *move, cull],
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


def install_init_torkan(blocks: Blocks) -> None:
    # AIR-02: initialize the flying slot at `slot index` as a Torkan of type `walk type` (handle_0F_Torkan
    # 3357-3377). Plain spawn-column draw with NO craft-proximity exclusion (gen_random_Y_store_obj 3360 /
    # 5147-5154, like the Kapi), top-row entry, aimed TOWARD the craft on the 32-magnitude generic tier
    # (2 px/frame, angle_dX_dY_tbl 6360 via the `+4`-rounded toward path). Stamps the Torkan's
    # points/flag/code and seeds the single-shot delay (64-127 frames) into `slot fire timer`, counted
    # down 2/tick during the approach. Torkan fires ONCE directly at expiry, so — unlike the Terrazi/Kapi
    # — it captures NO fire mask (it never calls the shared fire-permission gate).
    definition = _install_warp_proc(blocks, INIT_TORKAN_PROCCODE)
    reset, draw_loop = _draw_spawn_column(blocks, exclude_craft=False)
    stamp = blocks.if_reporter(
        blocks.op_eq(variable("spawn found", SPAWN_FOUND_ID), number(1)),
        [
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, variable("walk type", WALK_TYPE_ID)),
            _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_ACTIVE)),
            # Enter from the TOP row, like the other flying families (the same self-propelled-port
            # deviation: no enemy scroll, so every wave streams in from the top with room to aim/fire).
            _set_cur_item(blocks, "slot x", SLOT_X_ID, number(TOROID_SPAWN_ROW * SLOT_UNITS_PER_CELL)),
            blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
            blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
            blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 32", AIM_DX_32_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 32", AIM_DY_32_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
            _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(TORKAN_FLAG_APPROACH)),
            _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(TORKAN_INIT_CODE)),
            _set_cur_item(blocks, "slot pts", SLOT_PTS_ID, number(TORKAN_PTS)),
            # Single-shot delay 64-127 frames = (rng mod 64) + 64 (handle_0F 3366-3367). A fresh RNG draw
            # after the spawn-column draws, in walk order. Stored in `slot fire timer` and counted down
            # 2/tick during the approach; at <= 0 the Torkan fires ONCE and enters the hover.
            blocks.call_proc(RNG_PROCCODE, warp=True),
            _set_cur_item(
                blocks,
                "slot fire timer",
                SLOT_FIRE_TIMER_ID,
                blocks.op_add(
                    blocks.op_mod(variable("rng out", RNG_OUT_ID), number(TORKAN_SHOT_DELAY_SPAN)),
                    number(TORKAN_SHOT_DELAY_BASE),
                ),
            ),
        ],
    )
    blocks.chain(definition, [*reset, draw_loop, stamp])


def install_update_torkan(blocks: Blocks) -> None:
    # AIR-02: advance the Torkan at `slot index` by one tick (handle_0F_Torkan 3357-3377, torkan_shoot
    # 3378-3394, torkan_update_dir 3395-3411). Three phases on `slot flag`:
    #  - APPROACH: fly on the aimed 2 px/frame velocity and count the shot delay (held in `slot fire
    #    timer`) down 2/tick; at <= 0 fire EXACTLY ONE aimed bullet DIRECTLY (no gate, no mask), reset the
    #    animation clock, and enter HOVER. The fire and the flag flip are nested in the SAME expiry gate,
    #    so the shot cannot repeat on a later tick.
    #  - HOVER: hold position (`slot dx/dy = 0`) while the animation clock runs. The port has no enemy
    #    scroll to carry the arcade's `scroll_sprite_X` hover drift (3392), so the hold is screen-static
    #    where the arcade drifts down with the terrain — a recorded deviation (record 029). When the clock
    #    reaches TORKAN_HOVER_END, re-aim ONCE and enter FLEE.
    #  - FLEE: fly straight on the away vector until culled (no per-tick work beyond the shared move).
    # The re-aim reproduces torkan_update_dir (get_index_for_angle -> +0x80 -> get_dX_dY_and_cpy_to_obj),
    # which applies NO +4 rounding (unlike the toward/spawn aim): set the TOWARD diffs, run the shared
    # quantizer for its UN-rounded folded angle (`aim base`), then take (aim base >> 3 + half-turn) mod 32
    # as the away index into the fast 48-tier (3 px/frame). Nested in the window gate, so it happens once.
    # Shares the flying hit window / explosion; the 7-frame roll is derived render-only from the slot clock.
    definition = _install_warp_proc(blocks, UPDATE_TORKAN_PROCCODE)
    flag = lambda: _cur_item(blocks, "slot flag", SLOT_FLAG_ID)
    fire_timer = lambda: _cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID)
    slot_timer = lambda: _cur_item(blocks, "slot timer", SLOT_TIMER_ID)

    # APPROACH -> HOVER: count the shot delay down; at <= 0 fire ONCE and enter the hover. `fire_timer()`
    # re-reads the list, so the trigger sees the just-decremented value.
    fire_and_hover = blocks.if_reporter(
        blocks.op_not(blocks.op_gt(fire_timer(), number(0))),  # slot fire timer <= 0
        [
            *_fire_aimed_bullet(blocks),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),  # start the hover clock clean
            _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(TORKAN_FLAG_HOVER)),
        ],
    )
    approach = blocks.if_reporter(
        blocks.op_eq(flag(), number(TORKAN_FLAG_APPROACH)),
        [
            _set_cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_sub(fire_timer(), number(TICK_TIMER_STEP))),
            fire_and_hover,
        ],
    )
    # HOVER -> FLEE: hold position; when the clock reaches the window end, re-aim ONCE away and flee.
    reaim_and_flee = blocks.if_reporter(
        blocks.op_not(blocks.op_lt(slot_timer(), number(TORKAN_HOVER_END))),  # slot timer >= HOVER_END
        [
            blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
            blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
            blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
            # Away index from the UN-rounded folded angle: (aim base >> 3 + 16) mod 32, 1-based. This is
            # the arcade's `add.b #0x80` on the raw angle byte carried through the shared `lsr.b #3`, with
            # NO +4 (that rounding lives only on the toward/spawn path, calc_dX_dY_for_vector_to_solvalou).
            blocks.set_var_expr(
                "aim index",
                AIM_INDEX_ID,
                blocks.op_add(
                    blocks.op_mod(
                        blocks.op_add(
                            blocks.op_floor(blocks.op_div(variable("aim base", AIM_BASE_ID), number(8))),
                            number(TORKAN_REAIM_HALF_TURN),
                        ),
                        number(32),
                    ),
                    number(1),
                ),
            ),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 48", AIM_DX_48_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 48", AIM_DY_48_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(TORKAN_FLAG_FLEE)),
        ],
    )
    hover = blocks.if_reporter(
        blocks.op_eq(flag(), number(TORKAN_FLAG_HOVER)),
        [
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, number(0)),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, number(0)),
            reaim_and_flee,
        ],
    )
    # Move by 4*velocity per tick (2 arcade frames), advance the animation clock, then cull. During HOVER
    # the velocity is zeroed so only the clock advances (the hold); APPROACH/FLEE move on their vector.
    move = [
        _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
        _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_add(_cur_item(blocks, "slot y", SLOT_Y_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)))),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP))),
    ]
    off_bottom = blocks.op_not(blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MAX)))
    off_top = blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MIN + 1))  # row <= -2  ==  row < -1
    off_right = blocks.op_not(blocks.op_lt(_cur_col(blocks), number(CULL_COL_MAX)))
    off_left = blocks.op_lt(_cur_col(blocks), number(CULL_COL_MIN + 1))  # col <= -2 (left edge)
    # The flee peels off any edge (its away vector points off-screen), and the shared signed-column cull
    # needs the explicit left edge the reference's byte-wrap handles implicitly (as Toroid/Terrazi/Kapi).
    offscreen = blocks.op_or(blocks.op_or(off_bottom, off_top), blocks.op_or(off_right, off_left))
    cull = blocks.if_reporter(offscreen, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    state = lambda: _cur_item(blocks, "slot state", SLOT_STATE_ID)
    # PLY-02: an active Torkan touching the craft's cell kills it (raises `player hit`), checked at the
    # tick-start position before it moves or culls — the shared flying-vs-craft window.
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks), [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))]
    )
    normal = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_ACTIVE)),
        [craft_hit, approach, hover, *move, cull],
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


def install_init_jara(blocks: Blocks) -> None:
    # AIR-04: initialize the flying slot at `slot index` as a Jara of type `walk type` (jara_init
    # 3577-3586, shared by both 0x55 and 0x56). Craft-EXCLUDING random-Y draw (gen_rnd_spriteY 3580,
    # the +/-8 reject, the same as the Toroid/Terrazi), top-row entry (the shared no-enemy-scroll
    # deviation), aimed at the craft on the fast 48-magnitude tier (3 px/frame,
    # angle_dX_dY_terrazi_torkan_tbl 3581). Stamps the Jara's points/flag/code. Unlike the Terrazi/Kapi
    # it captures NO fire mask and seeds NO fire timer (jara_init never sets _FFREQ): the 0x55 shooter
    # fires exactly once at the turn, structurally gated, and the 0x56 silent never fires. Shared by both
    # types — only the update branches on shooter vs silent.
    definition = _install_warp_proc(blocks, INIT_JARA_PROCCODE)
    reset, draw_loop = _draw_spawn_column(blocks)  # default exclude_craft=True (gen_rnd_spriteY)
    stamp = blocks.if_reporter(
        blocks.op_eq(variable("spawn found", SPAWN_FOUND_ID), number(1)),
        [
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, variable("walk type", WALK_TYPE_ID)),
            _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_ACTIVE)),
            # Enter from the TOP row, like the other flying families (no enemy scroll, so every wave
            # streams in from the top with room to reach its proximity band and turn).
            _set_cur_item(blocks, "slot x", SLOT_X_ID, number(TOROID_SPAWN_ROW * SLOT_UNITS_PER_CELL)),
            blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
            blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
            blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 48", AIM_DX_48_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 48", AIM_DY_48_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
            _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(JARA_FLAG_APPROACH)),
            _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(JARA_INIT_CODE)),
            _set_cur_item(blocks, "slot pts", SLOT_PTS_ID, number(JARA_PTS)),
        ],
    )
    blocks.chain(definition, [*reset, draw_loop, stamp])


def install_update_jara(blocks: Blocks) -> None:
    # AIR-04: advance the Jara at `slot index` by one tick (handle_55/56 3502-3599, shared core). While
    # APPROACHING it cruises straight on its aimed 3 px/frame velocity holding the static frame (no anim
    # advance) and does NOT fire; each tick it tests the LATERAL offset (player col - self col) against
    # the proximity band [LOW, HIGH] (the reference's carry test on `_Y`, 3591-3594). On the first tick
    # inside the band it commits ONE-WAY to a turn: it latches the peel-away side by the offset sign
    # (jara_set_dir 3513-3515, the same side mapping as the Kapi dive), and — for the 0x55 shooter ONLY —
    # fires EXACTLY ONE aimed bullet DIRECTLY (jara_shoot 3544 -> init_new_bullet), both nested in the
    # SAME transition gate so neither can recur. While TURNED it ramps the LATERAL velocity by +/-accel
    # in the latched direction (peeling away; _dX untouched, unlike the Kapi dive) and spins the 6-frame
    # animation (render-only). One-way: it never re-tests proximity and never re-fires. Shares the flying
    # hit window / explosion; the spin animation is derived render-only from the slot clock.
    definition = _install_warp_proc(blocks, UPDATE_JARA_PROCCODE)
    flag = lambda: _cur_item(blocks, "slot flag", SLOT_FLAG_ID)
    col_offset = lambda: blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))

    # Latch the peel-away side (jara_set_dir 3513-3515): craft at/right laterally (offset >= 0) ->
    # TURN_MINUS (`subq #1,_dY`); craft left (offset < 0) -> TURN_PLUS (`addq #1,_dY`). Identical side
    # mapping to the Kapi dive. Latched once at the turn and never recomputed.
    side = blocks.add("control_if_else")
    craft_at_or_right = blocks.op_not(blocks.op_lt(col_offset(), number(0)))  # offset >= 0
    blocks.blocks[side]["inputs"]["CONDITION"] = [2, craft_at_or_right]
    blocks.blocks[craft_at_or_right]["parent"] = side
    blocks.substack(side, [_set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(JARA_FLAG_TURN_MINUS))])
    blocks.substack(side, [_set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(JARA_FLAG_TURN_PLUS))], name="SUBSTACK2")
    # The 0x55 shooter fires ONE aimed bullet at the turn instant (jara_shoot 3544-3547); the 0x56
    # silent has no fire. Nested in the transition gate below, so it happens exactly once.
    shoots = blocks.if_reporter(
        blocks.op_eq(_cur_item(blocks, "slot type", SLOT_TYPE_ID), number(JARA_SHOOTER_TYPE)),
        _fire_aimed_bullet(blocks),
    )
    # Turn trigger (only while approaching): LOW <= lateral offset <= HIGH -> commit the one-way turn.
    # Latch the side, fire (shooter only), and reset the animation clock so the spin starts clean at the
    # entry frame (`clr _TIMER`-equivalent, matching the arcade's per-turn reset at 3528/3563). The fire
    # and the side latch are both nested HERE, so once the flag leaves APPROACH neither can run again.
    at_or_above_low = blocks.op_not(blocks.op_lt(col_offset(), number(JARA_PROXIMITY_LOW)))
    at_or_below_high = blocks.op_not(blocks.op_gt(col_offset(), number(JARA_PROXIMITY_HIGH)))
    in_band = blocks.op_and(at_or_above_low, at_or_below_high)
    trigger = blocks.if_reporter(
        blocks.op_and(blocks.op_eq(flag(), number(JARA_FLAG_APPROACH)), in_band),
        [
            side,
            shoots,
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
        ],
    )
    # Turn kinematics, by latched side: ramp the LATERAL velocity away from the craft. Runs on the
    # trigger tick too (the flag was just flipped to a turn side), matching the arcade's fall-through
    # from jara_set_dir into the same-frame `_dY +/- 1` and move. _dX is UNTOUCHED (no scroll decel).
    turn_minus = blocks.if_reporter(
        blocks.op_eq(flag(), number(JARA_FLAG_TURN_MINUS)),
        [_set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.op_sub(_cur_item(blocks, "slot dy", SLOT_DY_ID), number(JARA_TURN_LATERAL_ACCEL)))],
    )
    turn_plus = blocks.if_reporter(
        blocks.op_eq(flag(), number(JARA_FLAG_TURN_PLUS)),
        [_set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.op_add(_cur_item(blocks, "slot dy", SLOT_DY_ID), number(JARA_TURN_LATERAL_ACCEL)))],
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
    # The peel carries the Jara off a side; the same explicit four-edge cull as the other flying
    # families (this port's signed columns need the left edge the reference's byte-wrap handles).
    offscreen = blocks.op_or(blocks.op_or(off_bottom, off_top), blocks.op_or(off_right, off_left))
    cull = blocks.if_reporter(offscreen, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    state = lambda: _cur_item(blocks, "slot state", SLOT_STATE_ID)
    # PLY-02: an active Jara touching the craft's cell kills it (raises `player hit`), checked at the
    # tick-start position before it moves or culls — the shared flying-vs-craft window.
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks), [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))]
    )
    # Ordered body: offer to the shot detector (via the wrapper below), then the turn trigger (which
    # fires the shooter's one bullet from the slot's pre-move position, matching jara_shoot's order), the
    # latched turn ramp, move, cull.
    normal = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_ACTIVE)),
        [craft_hit, trigger, turn_minus, turn_plus, *move, cull],
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


def _install_zoshi_init(
    blocks: Blocks,
    proccode: str,
    pts: int,
    *,
    exclude_craft: bool,
    bottom_entry: bool,
) -> None:
    # AIR-03 shared Zoshi initializer body (handle_0C/0D/0E init, 3412-3499). Draw a lateral spawn
    # column from the shared stream, stamp the slot, aim the INITIAL drift at the craft on the
    # 24-magnitude toroid tier (1.5 px/frame, calc_dX_dY_for_vector_to_solvalou -> angle_dX_dY_toroid_tbl),
    # capture the Zoshi fire mask, and seed the shot timer. The three thin wrappers below select the two
    # arcade differences: the spawn-column draw (bottom uses gen_rnd_spriteY, the craft-EXCLUDING draw;
    # top/rnd use gen_random_Y_store_obj, the plain draw) and the entry row (bottom enters at the fixed
    # bottom row 40 and drifts up; top/rnd enter from the top row, the shared no-enemy-scroll deviation).
    definition = _install_warp_proc(blocks, proccode)
    reset, draw_loop = _draw_spawn_column(blocks, exclude_craft=exclude_craft)
    entry_row = ZOSHI_BOTTOM_EDGE_X if bottom_entry else TOROID_SPAWN_ROW
    stamp = blocks.if_reporter(
        blocks.op_eq(variable("spawn found", SPAWN_FOUND_ID), number(1)),
        [
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, variable("walk type", WALK_TYPE_ID)),
            _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_ACTIVE)),
            # Set the entry row BEFORE aiming so the initial drift is computed from the true spawn row.
            _set_cur_item(blocks, "slot x", SLOT_X_ID, number(entry_row * SLOT_UNITS_PER_CELL)),
            # Aim the initial drift at the craft on the 24-magnitude tier (all three variants, 3428/3468/
            # 3466 -> zoshi_0D_init's calc_dX_dY_for_vector_to_solvalou). The 0C erratic veer only emerges
            # later, at each fire trigger.
            blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
            blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
            blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 24", AIM_DX_24_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 24", AIM_DY_24_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
            # Zoshi has no maneuver phases (it always spins and fires); `slot flag` is unused (0). The
            # aimed-vs-random re-heading keys on `slot type`, never on `slot flag`.
            _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(0)),
            _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(ZOSHI_INIT_CODE)),
            _set_cur_item(blocks, "slot pts", SLOT_PTS_ID, number(pts)),
            # Fire-permission capture-at-spawn: snapshot the Zoshi mask into the per-slot field, then seed
            # the shot timer to (rng & mask) + 1. Unlike the Terrazi's spawn seed (rng & mask, no +1),
            # the arcade Zoshi ADDS 1 at spawn too (0D/0C `addq.b #1,d0` before `move.b d0,(_TIMER)`), so
            # the countdown is at least 1 and reaches its first fire without a byte-underflow wait. A fresh
            # RNG draw after the spawn-column draws, in walk order; `rng & mask` == `rng mod (mask+1)` for
            # the contiguous fire-frequency mask.
            _set_cur_item(blocks, "slot fire mask", SLOT_FIRE_MASK_ID, variable("fire mask zoshi", FIRE_MASK_ZOSHI_ID)),
            blocks.call_proc(RNG_PROCCODE, warp=True),
            _set_cur_item(
                blocks,
                "slot fire timer",
                SLOT_FIRE_TIMER_ID,
                blocks.op_add(
                    blocks.op_mod(
                        variable("rng out", RNG_OUT_ID),
                        blocks.op_add(_cur_item(blocks, "slot fire mask", SLOT_FIRE_MASK_ID), number(1)),
                    ),
                    number(1),
                ),
            ),
        ],
    )
    blocks.chain(definition, [*reset, draw_loop, stamp])


def install_init_zoshi_top(blocks: Blocks) -> None:
    # AIR-03 top-entry Zoshi (handle_0D_Zoshi_top 3421-3426): plain spawn-column draw (gen_random_Y_
    # store_obj 3424, NO craft exclusion), top-row entry, 100 pts. Re-aims its drift toward the craft
    # at each shot (the shared update's non-rnd branch).
    _install_zoshi_init(blocks, INIT_ZOSHI_TOP_PROCCODE, ZOSHI_PTS_AIMED, exclude_craft=False, bottom_entry=False)


def install_init_zoshi_bottom(blocks: Blocks) -> None:
    # AIR-03 bottom-entry Zoshi (handle_0E_Zoshi_bottom 3414-3419): craft-EXCLUDING spawn-column draw
    # (gen_rnd_spriteY 3417 — the same craft-proximity reject the Toroid/Terrazi use), fixed bottom-row
    # entry (`_X = #40`), 100 pts. Re-aims its drift toward the craft at each shot.
    _install_zoshi_init(blocks, INIT_ZOSHI_BOTTOM_PROCCODE, ZOSHI_PTS_AIMED, exclude_craft=True, bottom_entry=True)


def install_init_zoshi_rnd(blocks: Blocks) -> None:
    # AIR-03 random-veer Zoshi (handle_0C_Zoshi_rnd 3463-3470): plain spawn-column draw (gen_random_Y_
    # store_obj 3466, NO craft exclusion), top-row entry, 70 pts. Re-headings its drift to a RANDOM angle
    # at each shot (the shared update's rnd branch) — the distinctive erratic flyer. Its SHOT is still
    # aimed at the craft, exactly like the other two.
    _install_zoshi_init(blocks, INIT_ZOSHI_RND_PROCCODE, ZOSHI_PTS_RND, exclude_craft=False, bottom_entry=False)


def install_update_zoshi(blocks: Blocks) -> None:
    # AIR-03: advance the Zoshi at `slot index` by one tick — the one movement/anim/fire core shared by
    # all three types (zoshi_0D_main / zoshi_0C_main and their inline fire blocks, 3432-3499). Each active
    # tick it drifts on its current 24-tier velocity, animates the 4-code spin, and runs a masked-periodic
    # fire block. On the fire trigger it RE-HEADINGS its OWN drift (the only per-type branch), fires ONE
    # aimed bullet, and reloads the shot timer:
    #  - top/bottom (type != ZOSHI_RND_TYPE): re-aim the drift TOWARD the craft on the 24-tier
    #    (calc_dX_dY_for_vector_to_solvalou, 3446-3447).
    #  - rnd (type == ZOSHI_RND_TYPE): re-heading the drift to a RANDOM angle on the 24-tier
    #    (pseudo_random_gen -> get_dX_dY_and_cpy_to_obj, 3487-3489). This is where the arcade's "random"
    #    lives — the ENEMY'S MOVEMENT, never the shot. All three fire the identical aimed bullet.
    # The fire block replicates the shared fire-permission gate INLINE (the arcade Zoshi carries its own
    # bespoke fire block rather than calling chk_timer_fire_bullet_reinit_timer, because the re-heading and
    # the bullet spawn share one trigger): the 8-arcade-frame phase (tick & 3 == 0 -> every 4th tick), a
    # byte decrement of the per-slot countdown, and at zero the re-heading + fire + reload to
    # (rng & mask) + 1. Shares the flying hit window / explosion; the spin is written into `slot code` from
    # the global frame and drawn render-only.
    definition = _install_warp_proc(blocks, UPDATE_ZOSHI_PROCCODE)
    slot_type = lambda: _cur_item(blocks, "slot type", SLOT_TYPE_ID)
    fire_timer = lambda: _cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID)

    # Re-heading the enemy's OWN drift at the fire trigger, branched on the slot type. Fresh reporters
    # per read (a reporter attaches to one parent only), so every operand keeps its own subtree.
    reaim_toward = [
        blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
        blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
        blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
        _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 24", AIM_DX_24_ID, variable("aim index", AIM_INDEX_ID))),
        _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 24", AIM_DY_24_ID, variable("aim index", AIM_INDEX_ID))),
    ]
    # rnd re-heading: draw a random byte and set the drift from a random 24-tier direction. The index is
    # floor(rng / 8) + 1 (1..32) — the port's no-bitwise form of get_dX_dY_and_cpy_to_obj's
    # `(rand >> 3) & 0x1f` (Scratch has no bitwise ops; the +1 makes it a 1-based list index). The arcade's
    # own `d0 -> d2` handoff for this draw is a leftover-register quirk of the 68K transcode (pseudo_random_
    # gen returns in d0, the table index is read from d2, 1428-1445 / 3487-3489); the port has no leftover
    # register, so this is rendered as a clean random draw — a documented, operator-approved deviation
    # (docs/mechanics/030). Nothing about "aimed shot, not random shot" depends on that quirk: the shot is
    # the TYPE-6 homing bullet and this index writes the ENEMY'S drift, not the bullet's.
    reaim_random = [
        blocks.call_proc(RNG_PROCCODE, warp=True),
        blocks.set_var_expr(
            "aim index",
            AIM_INDEX_ID,
            blocks.op_add(blocks.op_floor(blocks.op_div(variable("rng out", RNG_OUT_ID), number(8))), number(1)),
        ),
        _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 24", AIM_DX_24_ID, variable("aim index", AIM_INDEX_ID))),
        _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 24", AIM_DY_24_ID, variable("aim index", AIM_INDEX_ID))),
    ]
    reheading = blocks.add("control_if_else")
    is_rnd = blocks.op_eq(slot_type(), number(ZOSHI_RND_TYPE))
    blocks.blocks[reheading]["inputs"]["CONDITION"] = [2, is_rnd]
    blocks.blocks[is_rnd]["parent"] = reheading
    blocks.substack(reheading, reaim_random)
    blocks.substack(reheading, reaim_toward, name="SUBSTACK2")

    # Reload the shot timer under the Zoshi mask: (rng mod (mask+1)) + 1, the arcade's `(rng & mask) + 1`.
    reload = [
        blocks.call_proc(RNG_PROCCODE, warp=True),
        _set_cur_item(
            blocks,
            "slot fire timer",
            SLOT_FIRE_TIMER_ID,
            blocks.op_add(
                blocks.op_mod(
                    variable("rng out", RNG_OUT_ID),
                    blocks.op_add(_cur_item(blocks, "slot fire mask", SLOT_FIRE_MASK_ID), number(1)),
                ),
                number(1),
            ),
        ),
    ]
    # On the fire trigger (countdown reached 0): re-heading the drift, fire ONE aimed bullet (the shared
    # aim/alloc body, identical for all three), then reload. The re-heading runs BEFORE the fire so the new
    # drift takes effect on this tick's move (the arcade re-aims then moves); _fire_aimed_bullet recomputes
    # its own toward-craft aim for the BULLET and does not disturb the drift just written.
    on_zero = blocks.if_reporter(
        blocks.op_eq(fire_timer(), number(0)),
        [reheading, *_fire_aimed_bullet(blocks), *reload],
    )
    dec = _set_cur_item(
        blocks,
        "slot fire timer",
        SLOT_FIRE_TIMER_ID,
        blocks.op_mod(
            blocks.op_add(blocks.op_sub(fire_timer(), number(1)), number(FIRE_TIMER_BYTE_MOD)),
            number(FIRE_TIMER_BYTE_MOD),
        ),
    )
    on_phase = blocks.if_reporter(
        blocks.op_eq(blocks.op_mod(variable("tick", TICK_ID), number(FIRE_GATE_PHASE_TICKS)), number(0)),
        [dec, on_zero],
    )

    # Spin animation (render data): code = 0x28 + (global frame & 3), written each active tick so the
    # renderer reads only the Stage slot lists (`slot code`). The arcade drives the spin from the GLOBAL
    # countup_timer_1 (zoshi_0D_main 3453-3455 / zoshi_0C_main 3493-3495), so all Zoshi spin in lockstep;
    # `tick` is the port's global frame counter. The arcade advances one frame per arcade frame, but a tick
    # is two arcade frames, so this four-code cycle runs at HALF the arcade rate — the one per-frame rate
    # that cannot double at 2-frames-per-tick (mechanics record 030, deviation 8).
    anim = _set_cur_item(
        blocks,
        "slot code",
        SLOT_CODE_ID,
        blocks.op_add(number(ZOSHI_INIT_CODE), blocks.op_mod(variable("tick", TICK_ID), number(ZOSHI_ANIM_FRAMES))),
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
    # A Zoshi's random/toward re-heading can send it off any edge; the same explicit four-edge cull as the
    # other flying families (this port's signed columns need the left edge the reference's byte-wrap
    # handles implicitly).
    offscreen = blocks.op_or(blocks.op_or(off_bottom, off_top), blocks.op_or(off_right, off_left))
    cull = blocks.if_reporter(offscreen, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    state = lambda: _cur_item(blocks, "slot state", SLOT_STATE_ID)
    # PLY-02: an active Zoshi touching the craft's cell kills it (raises `player hit`), checked at the
    # tick-start position before it moves or culls — the shared flying-vs-craft window.
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks), [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))]
    )
    normal = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_ACTIVE)),
        [craft_hit, on_phase, anim, *move, cull],
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


def install_fire_permission_gate(blocks: Blocks) -> None:
    # AIR-06 shared, family-agnostic periodic-fire gate (chk_timer_fire_bullet_reinit_timer 4999-5010).
    # Operates on the current slot (`slot index`): every firing family calls this each active tick after
    # capturing its mask into `slot fire mask` at spawn. Faithful reproduction of the reference:
    #  1. GLOBAL phase — only proceed on the 8-arcade-frame boundary (every 4th tick).
    #  2. BYTE decrement of the per-slot fire countdown (`--_TIMER`), wrapping mod 256 so a spawn draw of
    #     0 wraps to 255 and counts down (the reference's underflow), never a permanent no-fire.
    #  3. At zero, FIRE one aimed bullet (the shared aim/alloc body) and RELOAD the countdown to
    #     (rng & mask) + 1 — no zero-suppression branch: the mask CAPS the reload interval (mask 0 =>
    #     reload 1 => fastest, larger mask => rarer). `rng & mask` is `rng mod (mask+1)` for the
    #     contiguous fire-frequency masks the schedule uses (recorded in 027).
    definition = _install_warp_proc(blocks, FIRE_GATE_PROCCODE)
    timer = lambda: _cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID)
    on_phase = blocks.op_eq(
        blocks.op_mod(variable("tick", TICK_ID), number(FIRE_GATE_PHASE_TICKS)), number(0)
    )
    dec = _set_cur_item(
        blocks,
        "slot fire timer",
        SLOT_FIRE_TIMER_ID,
        blocks.op_mod(
            blocks.op_add(blocks.op_sub(timer(), number(1)), number(FIRE_TIMER_BYTE_MOD)),
            number(FIRE_TIMER_BYTE_MOD),
        ),
    )
    reload = [
        blocks.call_proc(RNG_PROCCODE, warp=True),
        _set_cur_item(
            blocks,
            "slot fire timer",
            SLOT_FIRE_TIMER_ID,
            blocks.op_add(
                blocks.op_mod(
                    variable("rng out", RNG_OUT_ID),
                    blocks.op_add(_cur_item(blocks, "slot fire mask", SLOT_FIRE_MASK_ID), number(1)),
                ),
                number(1),
            ),
        ),
    ]
    fired = blocks.if_reporter(
        blocks.op_eq(timer(), number(0)),
        [*_fire_aimed_bullet(blocks), *reload],
    )
    phase = blocks.if_reporter(on_phase, [dec, fired])
    blocks.chain(definition, [phase])


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
    # Per-type init dispatch: each handled family runs its own initializer. Unhandled formation types
    # fall through unspawned until their slice (the recorded fewer-enemies deviation).
    is_toroid_spawn = blocks.op_or(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(TOROID_TYPE)),
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(TOROID_SHOOTS_TYPE)),
    )
    spawn_toroid = blocks.if_reporter(is_toroid_spawn, [blocks.call_proc(INIT_TOROID_PROCCODE, warp=True)])
    spawn_kapi = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(KAPI_TYPE)),
        [blocks.call_proc(INIT_KAPI_PROCCODE, warp=True)],
    )
    spawn_torkan = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(TORKAN_TYPE)),
        [blocks.call_proc(INIT_TORKAN_PROCCODE, warp=True)],
    )
    spawn_terrazi = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(TERRAZI_TYPE)),
        [blocks.call_proc(INIT_TERRAZI_PROCCODE, warp=True)],
    )
    # AIR-03: the three Zoshi object types each run their own thin initializer (they differ only in spawn
    # draw / entry row / points), then share `update zoshi`.
    spawn_zoshi_top = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZOSHI_TOP_TYPE)),
        [blocks.call_proc(INIT_ZOSHI_TOP_PROCCODE, warp=True)],
    )
    spawn_zoshi_bottom = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZOSHI_BOTTOM_TYPE)),
        [blocks.call_proc(INIT_ZOSHI_BOTTOM_PROCCODE, warp=True)],
    )
    spawn_zoshi_rnd = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZOSHI_RND_TYPE)),
        [blocks.call_proc(INIT_ZOSHI_RND_PROCCODE, warp=True)],
    )
    # AIR-04: both Jara object types (shooter 0x55 / silent 0x56) run the SAME shared initializer
    # (jara_init 3577-3586 — they differ only in the update's one-shot fire); one OR branch, as the
    # dispatch ORs them. Adjacent shooter/silent positions in a wave run thus spawn one of each.
    spawn_jara = blocks.if_reporter(
        blocks.op_or(
            blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(JARA_SHOOTER_TYPE)),
            blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(JARA_SILENT_TYPE)),
        ),
        [blocks.call_proc(INIT_JARA_PROCCODE, warp=True)],
    )
    bounds_gate = blocks.if_reporter(in_bounds, [set_type, spawn_toroid, spawn_kapi, spawn_torkan, spawn_terrazi, spawn_zoshi_top, spawn_zoshi_bottom, spawn_zoshi_rnd, spawn_jara])
    empty_gate = blocks.if_reporter(empty, [bounds_gate])
    blocks.substack(loop, [set_slot, empty_gate, blocks.change_var("spawn cursor", SPAWN_CURSOR_ID, 1)])
    blocks.chain(definition, [set_i, loop])


def install_debug_spawn_wave(blocks: Blocks) -> None:
    # ENGINE-TODO(#119): remove this temporary debug spawn key (and its locked-spec control-mapping
    # amendment) once every aerial family is built and playtested, so reachability no longer needs it.
    # DEBUG / TEMPORARY (tracked for removal): while the debug key (T) is held, CYCLE through the
    # buildable enemy families ONE AT A TIME so the operator can watch each enemy's full lifecycle
    # (approach, fire, its family's manoeuvre, exit) instead of a confusing six-at-once wave. Each tick:
    # point the formation at the CURRENT family's offset (`debug spawn index` selects the DEBUG_SPAWN_
    # FAMILIES entry); if any flying enemy is already on the field, set the spawn count to 0 (let that
    # one live out its life alone); otherwise clear the flying slots, set the count to 1 so the shared
    # spawner (which runs right after this in the walk) brings in exactly one fresh enemy from the top,
    # and ADVANCE the index (mod len) so the next fresh spawn is the next family — holding T walks
    # Terrazi -> Kapi -> (wrap). It self-gates on the key, so normal play is untouched when the key is
    # not held. Reachability recurs for every future aerial family (each just appends one DEBUG_SPAWN_
    # FAMILIES entry, no new key), so this stays a dev tool until they are all built and playtested, then
    # it is removed (it amends the locked control mapping — see core-game-systems.md and issue #119).
    definition = _install_warp_proc(blocks, DEBUG_SPAWN_PROCCODE)
    gate = blocks.add("control_if")
    pressed = blocks.key_pressed(gate, DEBUG_SPAWN_KEY)
    blocks.blocks[gate]["inputs"]["CONDITION"] = [2, pressed]

    # Point the formation at the current family's offset (an if-chain over DEBUG_SPAWN_FAMILIES keyed by
    # `debug spawn index`). Set every tick, before the spawner runs; the fresh-spawn branch below then
    # advances the index for next time.
    set_offset = [
        blocks.if_reporter(
            blocks.op_eq(variable("debug spawn index", DEBUG_SPAWN_INDEX_ID), number(index)),
            [blocks.set_var("formation type offset", FORMATION_TYPE_OFFSET_ID, number(offset))],
        )
        for index, (_family_type, offset, _count) in enumerate(DEBUG_SPAWN_FAMILIES)
    ]
    # The current family's group size, set only on a fresh spawn (below): 1 for every solo family, 2
    # for the Jara pair entry (offset 18, whose two-slot window brings in one shooter + one silent).
    set_count = [
        blocks.if_reporter(
            blocks.op_eq(variable("debug spawn index", DEBUG_SPAWN_INDEX_ID), number(index)),
            [blocks.set_var("formation count", FORMATION_COUNT_ID, number(count))],
        )
        for index, (_family_type, _offset, count) in enumerate(DEBUG_SPAWN_FAMILIES)
    ]

    # Any flying enemy already on the field?  (OR over the six flying slots — family-agnostic, so the
    # spawned enemy, whatever family, lives out its life before the next one arrives.)
    present = None
    for slot in range(FLYING_SLOTS[0], FLYING_SLOTS[1] + 1):
        occupied = blocks.op_not(
            blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, number(slot)), number(0))
        )
        present = occupied if present is None else blocks.op_or(present, occupied)

    branch = blocks.add("control_if_else")
    blocks.blocks[branch]["inputs"]["CONDITION"] = [2, present]
    blocks.blocks[present]["parent"] = branch
    # An enemy is alive: spawn nothing more this tick (keep it a solo).
    blocks.substack(branch, [blocks.set_var("formation count", FORMATION_COUNT_ID, number(0))])
    # Field empty: clear the flying slots and bring in this family's group (count 1, or 2 for the Jara
    # pair) from the top, then advance the family index. Free each slot the same way `cull slot` does —
    # BOTH `slot type` and `slot state` to 0 — so no slot is left type-empty but state-stale (a
    # half-freed slot the walk could misread). This wipes any live flying enemy on the field with no
    # explosion or score, which is the intended cost of the one-at-a-time isolation (the operator sees a
    # clean single enemy or pair); the playtest checklist notes it so it does not read as a bug.
    clear = [
        block
        for slot in range(FLYING_SLOTS[0], FLYING_SLOTS[1] + 1)
        for block in (
            blocks.list_replace("slot type", SLOT_TYPE_ID, number(slot), number(0)),
            blocks.list_replace("slot state", SLOT_STATE_ID, number(slot), number(0)),
        )
    ]
    advance_index = blocks.set_var_expr(
        "debug spawn index",
        DEBUG_SPAWN_INDEX_ID,
        blocks.op_mod(
            blocks.op_add(variable("debug spawn index", DEBUG_SPAWN_INDEX_ID), number(1)),
            number(len(DEBUG_SPAWN_FAMILIES)),
        ),
    )
    blocks.substack(
        branch,
        [*clear, *set_count, advance_index],
        name="SUBSTACK2",
    )
    blocks.substack(gate, [*set_offset, branch])
    blocks.chain(definition, [gate])


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

    # GND (#69) ground-record columns at the cursor. Each returns a FRESH reporter so it can be
    # consumed as its own input (a reporter attaches to only one parent — reuse would silently steal it).
    def ground_type_at_cursor() -> str:
        return blocks.list_item("schedule ground type", GROUND_OBJECT_TYPE_ID, cursor())

    def ground_slot_at_cursor() -> str:
        return blocks.list_item("schedule ground slot", GROUND_OBJECT_SLOT_ID, cursor())

    def ground_sprite_y_at_cursor() -> str:
        return blocks.list_item("schedule ground sprite y", GROUND_OBJECT_SPRITE_Y_ID, cursor())

    def ground_target_slot() -> str:
        # 1-based Scratch slot in the ground band: band base (GROUND_SLOTS[0]) + the record's 0-based slot.
        return blocks.op_add(number(GROUND_SLOTS[0]), ground_slot_at_cursor())

    def ground_target_slot_next() -> str:
        # The next ground-band slot (N+1): a Garu Barra occupies two adjacent slots — the base at N and
        # its destructible node at N+1 (handle_20_Garu_Barra stamps a5 and a5+_OBJSIZE).
        return blocks.op_add(number(GROUND_SLOTS[0] + 1), ground_slot_at_cursor())

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
    # GND (area.ground-dispatch #69): add_ground_object spawns a terrain-locked ground object into its
    # ground-band slot. Mirrors sub_2_fn_1__ground_object ($073F: it sets only _TYPE and _Y, leaving
    # _X = 0 at the top of the field) plus the per-family init the arcade runs on the object handler's
    # first coroutine step, relocated to spawn time in the port: _PTS by family, and (Logram) the
    # captured fire mask. slot y = sprite_y << 5 (x32), matching the arcade lsl #5; slot x starts at 0
    # (top-of-field) and `advance ground` scrolls it DOWN each tick. Only the two families built this
    # PR (Barra 0x1E, Logram 0x26) spawn; every other add_ground_object record (Zolbak, Garu Barra
    # until Commit 6, Domogram, ...) advances the cursor WITHOUT stamping a slot, so no unbuilt family
    # renders a live-but-inert object. Each column reader and target-slot index is rebuilt fresh per use.
    spawn_ground = [
        blocks.list_replace("slot type", SLOT_TYPE_ID, ground_target_slot(), ground_type_at_cursor()),
        blocks.list_replace("slot state", SLOT_STATE_ID, ground_target_slot(), number(SLOT_ACTIVE)),
        blocks.list_replace("slot x", SLOT_X_ID, ground_target_slot(), number(0)),
        blocks.list_replace(
            "slot y",
            SLOT_Y_ID,
            ground_target_slot(),
            blocks.op_mul(ground_sprite_y_at_cursor(), number(SLOT_UNITS_PER_PIXEL)),
        ),
        blocks.if_reporter(
            blocks.op_eq(ground_type_at_cursor(), number(BARRA_TYPE)),
            [blocks.list_replace("slot pts", SLOT_PTS_ID, ground_target_slot(), number(BARRA_PTS))],
        ),
        blocks.if_reporter(
            blocks.op_eq(ground_type_at_cursor(), number(LOGRAM_TYPE)),
            [
                blocks.list_replace(
                    "slot pts", SLOT_PTS_ID, ground_target_slot(), number(LOGRAM_PTS)
                ),
                blocks.list_replace(
                    "slot fire mask",
                    SLOT_FIRE_MASK_ID,
                    ground_target_slot(),
                    variable(FIRE_MASK_LOGRAM_NAME, FIRE_MASK_LOGRAM_ID),
                ),
                # Seed the open/close cycle (handle_logram_init $1B49): closed dome (_CODE=0x2C), WAIT phase,
                # and a masked-random initial delay (_TIMER=(rand & mask)+1). A ground slot is only ever a
                # ground object, but cull only clears type/state, so a slot reused from a prior Logram can
                # hold a stale flag/timer/code — seed all three explicitly rather than trust the cleared slot.
                blocks.list_replace(
                    "slot code", SLOT_CODE_ID, ground_target_slot(), number(LOGRAM_CLOSED_ORDINAL)
                ),
                blocks.list_replace(
                    "slot flag", SLOT_FLAG_ID, ground_target_slot(), number(LOGRAM_WAIT_PHASE)
                ),
                blocks.call_proc(RNG_PROCCODE, warp=True),
                blocks.list_replace(
                    "slot fire timer",
                    SLOT_FIRE_TIMER_ID,
                    ground_target_slot(),
                    blocks.op_add(
                        blocks.op_mod(
                            variable("rng out", RNG_OUT_ID),
                            blocks.op_add(
                                variable(FIRE_MASK_LOGRAM_NAME, FIRE_MASK_LOGRAM_ID), number(1)
                            ),
                        ),
                        number(1),
                    ),
                ),
            ],
        ),
    ]
    is_barra_or_logram = blocks.op_or(
        blocks.op_eq(ground_type_at_cursor(), number(BARRA_TYPE)),
        blocks.op_eq(ground_type_at_cursor(), number(LOGRAM_TYPE)),
    )
    # GND (ground.barra #70): the Garu Barra is a TWO-slot object (handle_20_Garu_Barra $1A89), so it does
    # not fit the single-slot spawn_ground. Base @ N: the indestructible 2x2 (state SLOT_GARU_BASE, so the
    # detector's ==ACTIVE gate rejects it), at slot x = 0 (top of field) and the record's lateral sprite_y.
    # Node @ N+1: the destructible core (state ACTIVE, 300 pts), placed at the arcade's absolute offsets —
    # slot x = 1 cell (_X = 0x0100) and slot y = base_y - 1 cell (_Y = base_Y - 0x0100, the verified 8-px
    # LATERAL offset, GND-01). Both scroll together at the shared terrain rate. slot y = sprite_y << 5 (x32).
    spawn_garu = [
        blocks.list_replace("slot type", SLOT_TYPE_ID, ground_target_slot(), ground_type_at_cursor()),
        blocks.list_replace("slot state", SLOT_STATE_ID, ground_target_slot(), number(SLOT_GARU_BASE)),
        blocks.list_replace("slot x", SLOT_X_ID, ground_target_slot(), number(0)),
        blocks.list_replace(
            "slot y",
            SLOT_Y_ID,
            ground_target_slot(),
            blocks.op_mul(ground_sprite_y_at_cursor(), number(SLOT_UNITS_PER_PIXEL)),
        ),
        blocks.list_replace("slot type", SLOT_TYPE_ID, ground_target_slot_next(), ground_type_at_cursor()),
        blocks.list_replace("slot state", SLOT_STATE_ID, ground_target_slot_next(), number(SLOT_ACTIVE)),
        blocks.list_replace("slot x", SLOT_X_ID, ground_target_slot_next(), number(SLOT_UNITS_PER_CELL)),
        blocks.list_replace(
            "slot y",
            SLOT_Y_ID,
            ground_target_slot_next(),
            blocks.op_sub(
                blocks.op_mul(ground_sprite_y_at_cursor(), number(SLOT_UNITS_PER_PIXEL)),
                number(SLOT_UNITS_PER_CELL),
            ),
        ),
        blocks.list_replace("slot pts", SLOT_PTS_ID, ground_target_slot_next(), number(GARU_BARRA_PTS)),
    ]
    add_ground_branch = blocks.if_reporter(
        blocks.op_eq(handler_at_cursor(), text(ADD_GROUND_OBJECT_HANDLER)),
        [
            blocks.if_reporter(is_barra_or_logram, spawn_ground),
            blocks.if_reporter(
                blocks.op_eq(ground_type_at_cursor(), number(GARU_BARRA_TYPE)), spawn_garu
            ),
        ],
    )
    # ENGINE-TODO: the remaining spawn / boss handler dispatch (add_domogram_with_path, add_object,
    # *bacura*, andor_genesis_*, sheonite_*) lands with the later enemy slices. The DIF/FORM handlers
    # (raise, adjust, set/reset formation, the 8 fire masks, ground-stop) and add_ground_object (the
    # two built ground families) are wired above; the still-unhandled spawn/boss records advance the
    # cursor and count the fire only.
    blocks.substack(
        loop,
        [
            raise_branch,
            adjust_branch,
            set_branch,
            reset_branch,
            *mask_branches,
            ground_stop_branch,
            add_ground_branch,
            blocks.change_var("schedule fired", SCHEDULE_FIRED_ID, 1),
            blocks.change_var("schedule cursor", SCHEDULE_CURSOR_ID, 1),
        ],
    )
    return [loop]


def install_advance_area(blocks: Blocks) -> None:
    # AREA-01/AREA-02 area clock + scheduler: one atomic (warp) pass per tick, called from the walk
    # thread BEFORE `advance slots` — matching the reference frame order (handle_next_area ->
    # handle_objects -> object updates) and fixing the PHASE order the enemy slices inherit while
    # both dispatch bodies are still empty. (Spawning a Logram now draws ONE RNG value here for its
    # masked-random initial fire delay, mirroring handle_logram_init — the arcade draws at init too; it
    # runs before the walk phase's own draws, so a Logram-spawn tick shifts that tick's stream by one.)
    # Advances the monotonic position and derives the row once; then a single `if/else` either completes
    # the area (advance 16 -> 7 and re-top) OR consumes the schedule for this row — never both on one tick.
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
    install_init_terrazi(blocks)
    install_init_kapi(blocks)
    install_init_torkan(blocks)
    install_init_zoshi_top(blocks)
    install_init_zoshi_bottom(blocks)
    install_init_zoshi_rnd(blocks)
    install_init_jara(blocks)
    install_check_air_hit(blocks)
    install_check_ground_hit(blocks)
    install_track_crosshair(blocks)
    install_advance_bomb(blocks)
    install_advance_ground(blocks)
    install_update_barra(blocks)
    install_update_garu(blocks)
    install_update_logram(blocks)
    install_explode_toroid_tick(blocks)
    install_update_bullet(blocks)
    install_update_toroid(blocks)
    install_update_terrazi(blocks)
    install_update_kapi(blocks)
    install_update_torkan(blocks)
    install_update_zoshi(blocks)
    install_update_jara(blocks)
    install_fire_permission_gate(blocks)
    install_cull_slot(blocks)
    install_advance_slots(blocks)
    install_spawn_flying(blocks)
    install_debug_spawn_wave(blocks)  # DEBUG / temporary (tracked for removal)
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
            # WPN-04: the bomb sight leads the craft (needs the just-cached player cell).
            blocks.call_proc(TRACK_CROSSHAIR_PROCCODE, warp=True),
            blocks.call_proc(ADVANCE_AREA_PROCCODE, warp=True),
            blocks.call_proc(ADVANCE_SLOTS_PROCCODE, warp=True),
            # WPN-04: arm/fly the bomb AFTER the terrain has scrolled this tick, so the landing
            # compare sees the same-tick ground positions (handle_bombing runs late in the frame).
            blocks.call_proc(ADVANCE_BOMB_PROCCODE, warp=True),
            # DEBUG (temporary, tracked for removal): overrides the scheduled formation to a Terrazi
            # wave while the debug key is held, so the spawner below fills a Terrazi wave for playtest.
            blocks.call_proc(DEBUG_SPAWN_PROCCODE, warp=True),
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
            # WPN-04: disarm the bomb weapon on every reset scope (a bomb in flight interrupted by a
            # death or a new game must not strand the one-bomb lockout). The slot states for the
            # bomb/target/crosshair are already zeroed by `clear slots` above.
            blocks.set_var("bomb in flight", BOMB_INFLIGHT_ID, number(0)),
            blocks.set_var("bomb dx", BOMB_DX_ID, number(0)),
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
    # The craft is self-bounded on all four sides against the frame borders (update_solvalou_sprite_XY
    # $15C1 clamps the craft's own position on both axes; the sight/crosshair is a pure +offset lead and
    # never gates craft movement). The top bound used to be indirect — via the crosshair sprite touching
    # frame_t — which is retired now the crosshair is a pure renderer, so the craft takes its own top
    # bound here alongside the other three.
    for frame, opcode, input_name, amount in (
        ("frame_t", "motion_changeyby", "DY", -7),
        ("frame_b", "motion_changeyby", "DY", 7),
        ("frame_l", "motion_changexby", "DX", 7),
        ("frame_r", "motion_changexby", "DX", -7),
    ):
        correction = blocks.add("control_if")
        blocks.blocks[correction]["inputs"]["CONDITION"] = [
            2,
            blocks.touching(correction, frame),
        ]
        blocks.substack(
            correction,
            [blocks.add(opcode, inputs={input_name: number(amount)})],
        )
        movement_body.append(correction)
    blocks.substack(movement, movement_body)
    playing = blocks.if_state("playing", [blocks.show(), movement])
    dead = blocks.if_either_state("player-dead", "game-over", [blocks.hide()])
    blocks.chain(enter, [snapshot, title, ready, playing, dead])
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


def _render_stage_x(blocks: Blocks, slot: int) -> str:
    # Inverse of read_player's lateral map: stage x = (slot y / 256) * RENDER_COL_STAGE - RENDER_COL_OFFSET.
    # A fresh reporter subtree per call (reporters attach to a single parent).
    return blocks.op_sub(
        blocks.op_mul(
            blocks.op_div(blocks.list_item("slot y", SLOT_Y_ID, number(slot)), number(SLOT_UNITS_PER_CELL)),
            number(RENDER_COL_STAGE),
        ),
        number(RENDER_COL_OFFSET),
    )


def _render_stage_y(blocks: Blocks, slot: int) -> str:
    # Inverse of read_player's depth map: stage y = RENDER_ROW_TOP - (slot x / 256) * RENDER_ROW_STAGE.
    return blocks.op_sub(
        number(RENDER_ROW_TOP),
        blocks.op_mul(
            blocks.op_div(blocks.list_item("slot x", SLOT_X_ID, number(slot)), number(SLOT_UNITS_PER_CELL)),
            number(RENDER_ROW_STAGE),
        ),
    )


def slot_marker_blocks(name: str, slot: int, costume: str) -> dict[str, dict[str, Any]]:
    # WPN-04 pure single-slot renderer for the crosshair (35) and the bomb target (33). Each is one
    # persistent sprite (not a clone pool — there is exactly one of each) that, while playing, shows
    # itself at its slot's mapped stage position when the slot is ACTIVE and hides otherwise. It writes
    # no state; the walk-thread procs own the slot's position and active flag.
    blocks = Blocks(name)
    common_stop(blocks, hide=True)
    blocks.chain(blocks.receive("director reset"), [blocks.hide()])

    enter = blocks.receive("director enter")
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_active = blocks.op_eq(
        blocks.list_item("slot state", SLOT_STATE_ID, number(slot)), number(SLOT_ACTIVE)
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_active]
    blocks.blocks[is_active]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.switch_costume(costume),
            blocks.go_expr(_render_stage_x(blocks, slot), _render_stage_y(blocks, slot)),
            blocks.to_front(),  # B9: the reticle/target render above the terrain
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(enter, [blocks.if_state("playing", [loop])])
    return blocks.blocks


def bomb_blocks() -> dict[str, dict[str, Any]]:
    # WPN-04 pure renderer for the in-flight bomb (slot 34). Like slot_marker_blocks, but it also
    # animates through the 5 bomb frames as the bomb accelerates (the falling frame is a render-only
    # function of the bomb's velocity, kept in range by mod 5) and plays the drop/explosion sounds the
    # walk thread broadcasts on arm/finish (the sounds live on this sprite). It writes no game state.
    blocks = Blocks("bomb")
    common_stop(blocks, hide=True)
    blocks.chain(blocks.receive("director reset"), [blocks.hide()])
    # Sound-only receivers: the walk thread owns the bomb logic and fires these on arm / landing.
    blocks.chain(blocks.receive("bomb"), [blocks.play_sound("bomb_drop")])
    blocks.chain(blocks.receive("bomb landed"), [blocks.play_sound("bomb_explode")])

    enter = blocks.receive("director enter")
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_active = blocks.op_eq(
        blocks.list_item("slot state", SLOT_STATE_ID, number(BOMB_SLOT)), number(SLOT_ACTIVE)
    )
    # Falling frame ordinal 1..5 from |bomb dx| (grows as it accelerates); mod 5 keeps it in range.
    ordinal = blocks.op_add(
        number(1),
        blocks.op_mod(
            blocks.op_floor(
                blocks.op_div(
                    blocks.op_abs(variable("bomb dx", BOMB_DX_ID)), number(BOMB_ACCEL_PER_FRAME)
                )
            ),
            number(5),
        ),
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_active]
    blocks.blocks[is_active]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.switch_costume_expr(ordinal),
            blocks.go_expr(_render_stage_x(blocks, BOMB_SLOT), _render_stage_y(blocks, BOMB_SLOT)),
            blocks.to_front(),  # B9: the bomb renders above the terrain
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(enter, [blocks.if_state("playing", [loop])])
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


def barra_blocks() -> dict[str, dict[str, Any]]:
    # GND-01 Barra renderer (game_director owns these blocks; sprite_extractor owns the costumes).
    # One persistent clone per GROUND slot (1..16), the same clone-pool pattern as the Toroid but over
    # the terrain band instead of the flying band. Each clone is a pure per-tick function of its slot's
    # live state: shown, positioned (arcade cell -> stage px, the SAME mapping as every family renderer),
    # and costumed when the slot holds a Barra; hidden otherwise. The clone writes no state.
    #
    # Costume by state:
    #   ACTIVE -> the idle pyramid (ordinal BARRA_IDLE_ORDINAL).
    #   HIT    -> the bomb-explosion clock (slot timer, reset to 0 by the detector, advanced by
    #             `update barra`). For the first GROUND_CRATER_START_FRAMES (7 burst frames x 8) it plays
    #             the shared solv_death burst (floor(timer/8) selects the frame, arcade `TIMER >> 3` in
    #             handle_bomb_explosion $3186 — the distinct 0x60.. bomb-burst sprites are a deferred
    #             cosmetic, so the shared aerial burst stands in). After that it is a PERSISTENT crater
    #             (bomb_explosion_finished $31D7) flickering between the two crater costumes every
    #             GROUND_CRATER_FLICKER_FRAMES (arcade `countup >> 2 & 1`), scrolling with the terrain
    #             until it culls off the bottom. There is no size-doubling and no free-on-clock — unlike
    #             the flying burst, the ground crater never removes itself on its clock.
    blocks = Blocks(BARRA_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("barra clone slot", BARRA_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(GROUND_SLOTS[0], GROUND_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("barra clone slot", BARRA_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_barra = blocks.op_eq(
        blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(BARRA_TYPE)
    )
    # Terrain-locked position — identical cell->stage mapping to every family renderer.
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
    # HIT costume: explosion burst until the crater begins, then the flickering crater.
    explode_ordinal = blocks.op_add(
        number(BARRA_EXPLODE_BASE_ORDINAL),
        blocks.op_floor(
            blocks.op_div(
                blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()),
                number(GROUND_EXPLOSION_PHASE_FRAMES),
            )
        ),
    )
    crater_ordinal = blocks.op_add(
        number(BARRA_CRATER_BASE_ORDINAL),
        blocks.op_mod(
            blocks.op_floor(
                blocks.op_div(
                    blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()),
                    number(GROUND_CRATER_FLICKER_FRAMES),
                )
            ),
            number(2),
        ),
    )
    hit_costume = blocks.add("control_if_else")
    cratered = blocks.op_not(
        blocks.op_lt(
            blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()),
            number(GROUND_CRATER_START_FRAMES),
        )
    )
    blocks.blocks[hit_costume]["inputs"]["CONDITION"] = [2, cratered]
    blocks.blocks[cratered]["parent"] = hit_costume
    blocks.substack(hit_costume, [blocks.switch_costume_expr(crater_ordinal)])
    blocks.substack(hit_costume, [blocks.switch_costume_expr(explode_ordinal)], name="SUBSTACK2")

    state_render = blocks.add("control_if_else")
    is_hit = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT))
    blocks.blocks[state_render]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = state_render
    blocks.substack(state_render, [hit_costume])
    # ACTIVE: the single idle pyramid. It is a fixed costume, so select it by name (switch_costume_expr
    # obscures a menu with a runtime reporter — for a constant the by-name switch is the direct tool).
    blocks.substack(
        state_render,
        [blocks.switch_costume("barra/idle/01")],
        name="SUBSTACK2",
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_barra]
    blocks.blocks[is_barra]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.go_expr(stage_x, stage_y),
            state_render,
            blocks.add("looks_setsizeto", inputs={"SIZE": number(GROUND_RENDER_SIZE)}),
            blocks.to_front(),
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(clone, [blocks.hide(), loop])
    return blocks.blocks


def garu_blocks() -> dict[str, dict[str, Any]]:
    # GND-01 Garu Barra renderer (game_director owns these blocks; sprite_extractor owns the costumes).
    # One persistent clone per GROUND slot (1..16), the same terrain-band clone pool as the Barra. A Garu
    # occupies two adjacent slots that both carry GARU_BARRA_TYPE, so each clone that sees its slot holding
    # a Garu branches on the slot's STATE to know which part it is:
    #   SLOT_GARU_BASE -> the 2x2 indestructible base, pulsing between its two frames on the global `tick`
    #                     (the arcade cycles pulsing_colour_1; the frame swap stands in). Its costume is a
    #                     32-px canvas, so the shared GROUND_RENDER_SIZE draws it ~2x the node (arcade 2x2).
    #   SLOT_ACTIVE    -> the destructible node's idle pyramid (garu/node, mirrored from the Barra pyramid).
    #   SLOT_HIT       -> the node's explode-and-remove burst (explode_and_remove_object $3216): the shared
    #                     solv_death frames indexed floor(timer/4). `update garu` removes the slot when the
    #                     burst finishes, so the clone hides on the next tick — no crater. The clone writes
    #                     no state.
    blocks = Blocks(GARU_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("garu clone slot", GARU_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(GROUND_SLOTS[0], GROUND_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("garu clone slot", GARU_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_garu = blocks.op_eq(
        blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(GARU_BARRA_TYPE)
    )
    # Terrain-locked position — identical cell->stage mapping to every family renderer.
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
    # The node's HIT burst frame: floor(slot timer / 4), the shorter explode-and-remove cadence.
    burst_ordinal = blocks.op_add(
        number(GARU_EXPLODE_BASE_ORDINAL),
        blocks.op_floor(
            blocks.op_div(
                blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()),
                number(GARU_EXPLOSION_PHASE_FRAMES),
            )
        ),
    )
    # The base pulse frame: alternate the two frames on the global tick (like the Zoshi lockstep spin).
    base_ordinal = blocks.op_add(
        number(GARU_BASE_IDLE_ORDINAL),
        blocks.op_mod(
            blocks.op_floor(
                blocks.op_div(variable("tick", TICK_ID), number(GARU_BASE_PULSE_TICKS))
            ),
            number(GARU_BASE_PULSE_FRAMES),
        ),
    )
    # State cascade: HIT (node exploding) -> base (sentinel) -> ACTIVE node.
    base_or_node = blocks.add("control_if_else")
    is_base = blocks.op_eq(
        blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_GARU_BASE)
    )
    blocks.blocks[base_or_node]["inputs"]["CONDITION"] = [2, is_base]
    blocks.blocks[is_base]["parent"] = base_or_node
    blocks.substack(base_or_node, [blocks.switch_costume_expr(base_ordinal)])
    # ACTIVE node idle: the pyramid is a fixed costume (mirrored from the Barra), so select it by name —
    # switch_costume_expr obscures a menu with a runtime reporter; for a constant the by-name switch is direct.
    blocks.substack(base_or_node, [blocks.switch_costume("barra/idle/01")], name="SUBSTACK2")

    state_render = blocks.add("control_if_else")
    is_hit = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT))
    blocks.blocks[state_render]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = state_render
    blocks.substack(state_render, [blocks.switch_costume_expr(burst_ordinal)])
    blocks.substack(state_render, [base_or_node], name="SUBSTACK2")

    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_garu]
    blocks.blocks[is_garu]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.go_expr(stage_x, stage_y),
            state_render,
            blocks.add("looks_setsizeto", inputs={"SIZE": number(GROUND_RENDER_SIZE)}),
            blocks.to_front(),
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(clone, [blocks.hide(), loop])
    return blocks.blocks


def logram_blocks() -> dict[str, dict[str, Any]]:
    # GND (ground.logram #71) Logram renderer (game_director owns these blocks; sprite_extractor owns the
    # costumes). One persistent clone per GROUND slot (1..16), the same terrain-band clone pool as the
    # Barra/Garu, each a pure per-tick function of its slot's live state:
    #   ACTIVE -> the open/close dome frame `update logram` wrote into `slot code` (ordinals 1..4 =
    #             logram/open/01..04). The dome sits closed (ordinal 1) during the wait and opens to 4
    #             (full) at the shot, so the renderer just mirrors `slot code` via the reporter switch.
    #   HIT    -> IDENTICAL to the Barra crater (handle_bomb_explosion): the shared solv_death burst for
    #             the first GROUND_CRATER_START_FRAMES (floor(timer/8)), then a PERSISTENT crater flickering
    #             the two crater frames (floor(timer/4) mod 2), scrolling until it culls. No free-on-clock.
    # The clone writes no state.
    blocks = Blocks(LOGRAM_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("logram clone slot", LOGRAM_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(GROUND_SLOTS[0], GROUND_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("logram clone slot", LOGRAM_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_logram = blocks.op_eq(
        blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(LOGRAM_TYPE)
    )
    # Terrain-locked position — identical cell->stage mapping to every family renderer.
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
    # HIT costume: explosion burst until the crater begins, then the flickering crater (Barra-identical).
    explode_ordinal = blocks.op_add(
        number(LOGRAM_EXPLODE_BASE_ORDINAL),
        blocks.op_floor(
            blocks.op_div(
                blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()),
                number(GROUND_EXPLOSION_PHASE_FRAMES),
            )
        ),
    )
    crater_ordinal = blocks.op_add(
        number(LOGRAM_CRATER_BASE_ORDINAL),
        blocks.op_mod(
            blocks.op_floor(
                blocks.op_div(
                    blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()),
                    number(GROUND_CRATER_FLICKER_FRAMES),
                )
            ),
            number(2),
        ),
    )
    hit_costume = blocks.add("control_if_else")
    cratered = blocks.op_not(
        blocks.op_lt(
            blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()),
            number(GROUND_CRATER_START_FRAMES),
        )
    )
    blocks.blocks[hit_costume]["inputs"]["CONDITION"] = [2, cratered]
    blocks.blocks[cratered]["parent"] = hit_costume
    blocks.substack(hit_costume, [blocks.switch_costume_expr(crater_ordinal)])
    blocks.substack(hit_costume, [blocks.switch_costume_expr(explode_ordinal)], name="SUBSTACK2")

    state_render = blocks.add("control_if_else")
    is_hit = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT))
    blocks.blocks[state_render]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = state_render
    blocks.substack(state_render, [hit_costume])
    # ACTIVE: the dome frame the update wrote into `slot code` (a runtime ordinal, so the reporter switch —
    # a numeric costume value selects that 1-based costume, exactly as the burst/crater ordinals do).
    blocks.substack(
        state_render,
        [blocks.switch_costume_expr(blocks.list_item("slot code", SLOT_CODE_ID, slotvar()))],
        name="SUBSTACK2",
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_logram]
    blocks.blocks[is_logram]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.go_expr(stage_x, stage_y),
            state_render,
            blocks.add("looks_setsizeto", inputs={"SIZE": number(GROUND_RENDER_SIZE)}),
            blocks.to_front(),
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(clone, [blocks.hide(), loop])
    return blocks.blocks


def terrazi_blocks() -> dict[str, dict[str, Any]]:
    # AIR-06 Terrazi renderer (game_director owns these blocks; sprite_extractor owns the costumes).
    # One persistent clone per flying slot (59..64), the same pool pattern as the Toroid: shown and
    # positioned when its slot holds a Terrazi, hidden otherwise. The clone writes no state. The roll
    # frame is a render-only function of the slot's animation clock (`slot timer`), so the craft rolls
    # through its 7 frames every ~8 arcade frames (the reference's `_ddX` sprite-code advance) without
    # the walk writing `slot code`. On a hit it plays the shared explosion (the solv_death frames
    # appended after the 7 roll frames, ordinals 8..), exactly like the Toroid.
    blocks = Blocks(TERRAZI_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("terrazi clone slot", TERRAZI_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(FLYING_SLOTS[0], FLYING_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("terrazi clone slot", TERRAZI_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_terrazi = blocks.op_eq(
        blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(TERRAZI_TYPE)
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
    # Roll frame (render-only): (floor(timer / PERIOD) mod FRAMES) + 1 -> costume ordinal 1..7.
    roll_ordinal = blocks.op_add(
        blocks.op_mod(
            blocks.op_floor(blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TERRAZI_ROLL_PERIOD))),
            number(TERRAZI_ROLL_FRAMES),
        ),
        number(1),
    )
    # Shared explosion frames while HIT: the clock selects a phase mapping to the solv_death costumes
    # appended after the 7 roll frames (ordinal 8..); the burst doubles at the 2x phase (record 025).
    phase_for_costume = blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
    )
    explode_ordinal = blocks.op_add(number(TERRAZI_ROLL_FRAMES + 1), phase_for_costume)
    phase_for_size = blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
    )
    size_branch = blocks.add("control_if_else")
    is_big = blocks.op_eq(phase_for_size, number(TOROID_BIG_PHASE))
    blocks.blocks[size_branch]["inputs"]["CONDITION"] = [2, is_big]
    blocks.blocks[is_big]["parent"] = size_branch
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(TOROID_EXPLODE_SIZE)})])
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(TERRAZI_RENDER_SIZE)})], name="SUBSTACK2")
    state_render = blocks.add("control_if_else")
    is_hit = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT))
    blocks.blocks[state_render]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = state_render
    blocks.substack(state_render, [blocks.switch_costume_expr(explode_ordinal), size_branch])
    blocks.substack(
        state_render,
        [
            blocks.switch_costume_expr(roll_ordinal),
            blocks.add("looks_setsizeto", inputs={"SIZE": number(TERRAZI_RENDER_SIZE)}),
        ],
        name="SUBSTACK2",
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_terrazi]
    blocks.blocks[is_terrazi]["parent"] = render
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


def kapi_blocks() -> dict[str, dict[str, Any]]:
    # AIR-05 Kapi renderer (game_director owns these blocks; sprite_extractor owns the costumes). One
    # persistent clone per flying slot (59..64), the same pool pattern as the Terrazi/Toroid: shown and
    # positioned when its slot holds a Kapi, hidden otherwise. The clone writes no state. While the slot
    # is APPROACHING it holds the static entry frame (silent approach); while DIVING the 7-frame dive
    # animation is derived render-only from the slot's animation clock — an 8-phase cycle whose 8th
    # phase HOLDS the last frame (the reference's `d0 = (TIMER1>>3) & 7`, held at 7 -> loc_2455 3654),
    # then loops. On a hit it plays the shared explosion (the solv_death frames appended after the 7
    # dive frames, ordinals 8..), exactly like the Toroid/Terrazi.
    blocks = Blocks(KAPI_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("kapi clone slot", KAPI_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(FLYING_SLOTS[0], FLYING_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("kapi clone slot", KAPI_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_kapi = blocks.op_eq(
        blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(KAPI_TYPE)
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
    # Dive animation clock (render-only): phase = floor(timer / PERIOD) mod 8. A fresh reporter per read
    # (a reporter cannot be shared across parents — it is stolen by the first).
    phase = lambda: blocks.op_mod(
        blocks.op_floor(blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(KAPI_DIVE_PERIOD))),
        number(KAPI_DIVE_PHASES),
    )
    # Dive costume: phase 0..6 -> frame ordinal 1..7; phase 7 HOLDS the last frame (loc_2455).
    dive_costume = blocks.add("control_if_else")
    holds = blocks.op_gt(phase(), number(KAPI_DIVE_FRAMES - 1))  # phase > 6  ==  phase == 7
    blocks.blocks[dive_costume]["inputs"]["CONDITION"] = [2, holds]
    blocks.blocks[holds]["parent"] = dive_costume
    blocks.substack(dive_costume, [blocks.switch_costume("kapi/dive/07")])
    blocks.substack(dive_costume, [blocks.switch_costume_expr(blocks.op_add(phase(), number(1)))], name="SUBSTACK2")
    # Active costume: hold the static entry frame while approaching (silent approach), else the dive.
    active_costume = blocks.add("control_if_else")
    is_approach = blocks.op_eq(blocks.list_item("slot flag", SLOT_FLAG_ID, slotvar()), number(KAPI_FLAG_APPROACH))
    blocks.blocks[active_costume]["inputs"]["CONDITION"] = [2, is_approach]
    blocks.blocks[is_approach]["parent"] = active_costume
    blocks.substack(active_costume, [blocks.switch_costume("kapi/dive/01")])
    blocks.substack(active_costume, [dive_costume], name="SUBSTACK2")
    # Shared explosion frames while HIT: the clock selects a phase mapping to the solv_death costumes
    # appended after the 7 dive frames (ordinal 8..); the burst doubles at the 2x phase (record 025).
    phase_for_costume = blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
    )
    explode_ordinal = blocks.op_add(number(KAPI_DIVE_FRAMES + 1), phase_for_costume)
    phase_for_size = blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
    )
    size_branch = blocks.add("control_if_else")
    is_big = blocks.op_eq(phase_for_size, number(TOROID_BIG_PHASE))
    blocks.blocks[size_branch]["inputs"]["CONDITION"] = [2, is_big]
    blocks.blocks[is_big]["parent"] = size_branch
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(TOROID_EXPLODE_SIZE)})])
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(KAPI_RENDER_SIZE)})], name="SUBSTACK2")
    state_render = blocks.add("control_if_else")
    is_hit = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT))
    blocks.blocks[state_render]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = state_render
    blocks.substack(state_render, [blocks.switch_costume_expr(explode_ordinal), size_branch])
    blocks.substack(
        state_render,
        [
            active_costume,
            blocks.add("looks_setsizeto", inputs={"SIZE": number(KAPI_RENDER_SIZE)}),
        ],
        name="SUBSTACK2",
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_kapi]
    blocks.blocks[is_kapi]["parent"] = render
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


def torkan_blocks() -> dict[str, dict[str, Any]]:
    # AIR-02 Torkan renderer (game_director owns the blocks; sprite_extractor owns the costumes). One
    # persistent clone per flying slot (59..64), the same pool pattern as the Kapi/Terrazi: shown and
    # positioned when its slot holds a Torkan, hidden otherwise. The clone writes no state. The roll is
    # derived render-only from the slot's flag and animation clock: while APPROACHING it holds the static
    # entry frame (01); while HOVERING it sweeps the 6 available frames once (one per TORKAN_ANIM_PERIOD
    # ticks-of-frames — the arcade's `(TIMER>>2)&0xf` frame select, torkan_shoot 3383-3390, which steps
    # SEVEN codes 0x10..0x16; the 6-frame rip has no distinct art for the 7th, so it holds frame 06);
    # while FLEEING it holds the last frame (06). On a hit it plays the shared explosion (the solv_death
    # frames appended after the roll frames, ordinals 7..), exactly like the Kapi/Terrazi.
    blocks = Blocks(TORKAN_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("torkan clone slot", TORKAN_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(FLYING_SLOTS[0], FLYING_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("torkan clone slot", TORKAN_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_torkan = blocks.op_eq(
        blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(TORKAN_TYPE)
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
    # Hover animation clock (render-only): phase = floor(timer / PERIOD). A fresh reporter per read
    # (a reporter cannot be shared across parents — it is stolen by the first). No mod: the sweep runs
    # once over the ~28-frame hover window, and the clamp below holds the last frame at the boundary.
    phase = lambda: blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TORKAN_ANIM_PERIOD))
    )
    # Hover costume: phase 0..5 -> frame ordinal 1..6; phase >= 6 HOLDS the last available frame (06),
    # covering the arcade's 7th code-step (0x16) that the 6-frame rip has no distinct art for.
    hover_costume = blocks.add("control_if_else")
    holds = blocks.op_gt(phase(), number(TORKAN_ANIM_FRAMES - 1))  # phase > 5
    blocks.blocks[hover_costume]["inputs"]["CONDITION"] = [2, holds]
    blocks.blocks[holds]["parent"] = hover_costume
    blocks.substack(hover_costume, [blocks.switch_costume("torkan/roll/06")])
    blocks.substack(hover_costume, [blocks.switch_costume_expr(blocks.op_add(phase(), number(1)))], name="SUBSTACK2")
    # Flee vs hover: while fleeing hold the last frame (06); otherwise (hovering) run the sweep.
    moving_costume = blocks.add("control_if_else")
    is_flee = blocks.op_eq(blocks.list_item("slot flag", SLOT_FLAG_ID, slotvar()), number(TORKAN_FLAG_FLEE))
    blocks.blocks[moving_costume]["inputs"]["CONDITION"] = [2, is_flee]
    blocks.blocks[is_flee]["parent"] = moving_costume
    blocks.substack(moving_costume, [blocks.switch_costume("torkan/roll/06")])
    blocks.substack(moving_costume, [hover_costume], name="SUBSTACK2")
    # Active costume: hold the static entry frame while approaching (silent approach), else hover/flee.
    active_costume = blocks.add("control_if_else")
    is_approach = blocks.op_eq(blocks.list_item("slot flag", SLOT_FLAG_ID, slotvar()), number(TORKAN_FLAG_APPROACH))
    blocks.blocks[active_costume]["inputs"]["CONDITION"] = [2, is_approach]
    blocks.blocks[is_approach]["parent"] = active_costume
    blocks.substack(active_costume, [blocks.switch_costume("torkan/roll/01")])
    blocks.substack(active_costume, [moving_costume], name="SUBSTACK2")
    # Shared explosion frames while HIT: the clock selects a phase mapping to the solv_death costumes
    # appended after the 7 roll frames (ordinal 8..); the burst doubles at the 2x phase (record 025).
    phase_for_costume = blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
    )
    explode_ordinal = blocks.op_add(number(TORKAN_ANIM_FRAMES + 1), phase_for_costume)
    phase_for_size = blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
    )
    size_branch = blocks.add("control_if_else")
    is_big = blocks.op_eq(phase_for_size, number(TOROID_BIG_PHASE))
    blocks.blocks[size_branch]["inputs"]["CONDITION"] = [2, is_big]
    blocks.blocks[is_big]["parent"] = size_branch
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(TOROID_EXPLODE_SIZE)})])
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(TORKAN_RENDER_SIZE)})], name="SUBSTACK2")
    state_render = blocks.add("control_if_else")
    is_hit = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT))
    blocks.blocks[state_render]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = state_render
    blocks.substack(state_render, [blocks.switch_costume_expr(explode_ordinal), size_branch])
    blocks.substack(
        state_render,
        [
            active_costume,
            blocks.add("looks_setsizeto", inputs={"SIZE": number(TORKAN_RENDER_SIZE)}),
        ],
        name="SUBSTACK2",
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_torkan]
    blocks.blocks[is_torkan]["parent"] = render
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


def zoshi_blocks() -> dict[str, dict[str, Any]]:
    # AIR-03 Zoshi renderer (game_director owns the blocks; sprite_extractor owns the costumes). One
    # persistent clone per flying slot (59..64), the same pool pattern as the Kapi/Terrazi/Torkan: shown
    # and positioned when its slot holds any of the three Zoshi types, hidden otherwise. The clone writes
    # no state. The 4-frame spin runs continuously from the moment it spawns (Zoshi has no silent-approach
    # phase — it always spins and fires): the update writes `slot code = 0x28 + (tick & 3)` each tick, so
    # the renderer reads that code back to the 1-based costume ordinal `(slot code - 0x28) + 1` (1..4).
    # Driving the frame from `slot code` keeps every Zoshi in lockstep, matching the arcade's global
    # countup_timer spin (zoshi_0D_main 3452-3455). On a hit it plays the shared explosion (the solv_death
    # frames appended after the 4 spin frames, ordinals 5..), exactly like the other flying families.
    blocks = Blocks(ZOSHI_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("zoshi clone slot", ZOSHI_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(FLYING_SLOTS[0], FLYING_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("zoshi clone slot", ZOSHI_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    # The three Zoshi object types share one renderer; show the clone for any of them.
    slot_type = lambda: blocks.list_item("slot type", SLOT_TYPE_ID, slotvar())
    is_zoshi = blocks.op_or(
        blocks.op_eq(slot_type(), number(ZOSHI_RND_TYPE)),
        blocks.op_or(
            blocks.op_eq(slot_type(), number(ZOSHI_TOP_TYPE)),
            blocks.op_eq(slot_type(), number(ZOSHI_BOTTOM_TYPE)),
        ),
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
    # Active costume: the continuous spin. `slot code` (0x28..0x2B) maps to the 1-based ordinal
    # (slot code - 0x28) + 1 = 1..4 over zoshi/spin/01..04. Fresh reporter per read (a reporter attaches
    # to one parent only).
    active_costume = blocks.switch_costume_expr(
        blocks.op_add(
            blocks.op_sub(blocks.list_item("slot code", SLOT_CODE_ID, slotvar()), number(ZOSHI_INIT_CODE)),
            number(1),
        )
    )
    # Shared explosion frames while HIT: the clock selects a phase mapping to the solv_death costumes
    # appended after the 4 spin frames (ordinal 5..); the burst doubles at the 2x phase (record 025).
    phase_for_costume = blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
    )
    explode_ordinal = blocks.op_add(number(ZOSHI_ANIM_FRAMES + 1), phase_for_costume)
    phase_for_size = blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
    )
    size_branch = blocks.add("control_if_else")
    is_big = blocks.op_eq(phase_for_size, number(TOROID_BIG_PHASE))
    blocks.blocks[size_branch]["inputs"]["CONDITION"] = [2, is_big]
    blocks.blocks[is_big]["parent"] = size_branch
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(TOROID_EXPLODE_SIZE)})])
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(ZOSHI_RENDER_SIZE)})], name="SUBSTACK2")
    state_render = blocks.add("control_if_else")
    is_hit = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT))
    blocks.blocks[state_render]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = state_render
    blocks.substack(state_render, [blocks.switch_costume_expr(explode_ordinal), size_branch])
    blocks.substack(
        state_render,
        [
            active_costume,
            blocks.add("looks_setsizeto", inputs={"SIZE": number(ZOSHI_RENDER_SIZE)}),
        ],
        name="SUBSTACK2",
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_zoshi]
    blocks.blocks[is_zoshi]["parent"] = render
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


def jara_blocks() -> dict[str, dict[str, Any]]:
    # AIR-04 Jara renderer (game_director owns these blocks; sprite_extractor owns the costumes). One
    # persistent clone per flying slot (59..64), the same pool pattern as the Kapi/Terrazi: shown and
    # positioned when its slot holds EITHER Jara type (0x55/0x56), hidden otherwise. The clone writes no
    # state. While the slot is APPROACHING it holds the static entry frame 0xA0 (silent cruise); while
    # TURNED it cycles the 6-frame spin derived render-only from the slot's animation clock — a plain
    # 6-phase loop (no hold, unlike the Kapi's 8th-phase hold; the arcade cycle wraps at 6, 3521-3528).
    # The frame ORDER follows the turn side: TURN_MINUS uses the FORWARD table (jara/spin/01..06 =
    # 0xA0..0xA5), TURN_PLUS the REVERSED table (06..01 = 0xA5..0xA0) — the arcade's jara_right/left_
    # sprite_tbl (3571-3575). On a hit it plays the shared explosion (the solv_death frames appended
    # after the 6 spin frames, ordinals 7..), exactly like the other families.
    blocks = Blocks(JARA_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("jara clone slot", JARA_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(FLYING_SLOTS[0], FLYING_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("jara clone slot", JARA_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_jara = blocks.op_or(
        blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(JARA_SHOOTER_TYPE)),
        blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(JARA_SILENT_TYPE)),
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
    # Spin animation clock (render-only): phase = floor(timer / PERIOD) mod FRAMES. A fresh reporter per
    # read (a reporter cannot be shared across parents — it is stolen by the first).
    phase = lambda: blocks.op_mod(
        blocks.op_floor(blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(JARA_ANIM_PERIOD))),
        number(JARA_ANIM_FRAMES),
    )
    # Turned costume: the frame order depends on the peel side. TURN_MINUS -> forward table, ordinal
    # phase+1 (phase 0 -> jara/spin/01 = 0xA0); TURN_PLUS -> reversed table, ordinal FRAMES-phase
    # (phase 0 -> jara/spin/06 = 0xA5). The arcade's jara_right/left_sprite_tbl indexed by the same phase.
    turn_costume = blocks.add("control_if_else")
    is_turn_minus = blocks.op_eq(blocks.list_item("slot flag", SLOT_FLAG_ID, slotvar()), number(JARA_FLAG_TURN_MINUS))
    blocks.blocks[turn_costume]["inputs"]["CONDITION"] = [2, is_turn_minus]
    blocks.blocks[is_turn_minus]["parent"] = turn_costume
    blocks.substack(turn_costume, [blocks.switch_costume_expr(blocks.op_add(phase(), number(1)))])
    blocks.substack(turn_costume, [blocks.switch_costume_expr(blocks.op_sub(number(JARA_ANIM_FRAMES), phase()))], name="SUBSTACK2")
    # Active costume: hold the static entry frame while approaching (silent cruise), else the spin.
    active_costume = blocks.add("control_if_else")
    is_approach = blocks.op_eq(blocks.list_item("slot flag", SLOT_FLAG_ID, slotvar()), number(JARA_FLAG_APPROACH))
    blocks.blocks[active_costume]["inputs"]["CONDITION"] = [2, is_approach]
    blocks.blocks[is_approach]["parent"] = active_costume
    blocks.substack(active_costume, [blocks.switch_costume("jara/spin/01")])
    blocks.substack(active_costume, [turn_costume], name="SUBSTACK2")
    # Shared explosion frames while HIT: the clock selects a phase mapping to the solv_death costumes
    # appended after the 6 spin frames (ordinal 7..); the burst doubles at the 2x phase (record 025).
    phase_for_costume = blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
    )
    explode_ordinal = blocks.op_add(number(JARA_ANIM_FRAMES + 1), phase_for_costume)
    phase_for_size = blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
    )
    size_branch = blocks.add("control_if_else")
    is_big = blocks.op_eq(phase_for_size, number(TOROID_BIG_PHASE))
    blocks.blocks[size_branch]["inputs"]["CONDITION"] = [2, is_big]
    blocks.blocks[is_big]["parent"] = size_branch
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(TOROID_EXPLODE_SIZE)})])
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(JARA_RENDER_SIZE)})], name="SUBSTACK2")
    state_render = blocks.add("control_if_else")
    is_hit = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT))
    blocks.blocks[state_render]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = state_render
    blocks.substack(state_render, [blocks.switch_costume_expr(explode_ordinal), size_branch])
    blocks.substack(
        state_render,
        [
            active_costume,
            blocks.add("looks_setsizeto", inputs={"SIZE": number(JARA_RENDER_SIZE)}),
        ],
        name="SUBSTACK2",
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_jara]
    blocks.blocks[is_jara]["parent"] = render
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
    _ensure_gameplay_target(result, TERRAZI_TARGET)
    _ensure_gameplay_target(result, KAPI_TARGET)
    _ensure_gameplay_target(result, TORKAN_TARGET)
    _ensure_gameplay_target(result, ZOSHI_TARGET)
    _ensure_gameplay_target(result, JARA_TARGET)
    _ensure_gameplay_target(result, BARRA_TARGET)
    _ensure_gameplay_target(result, GARU_TARGET)
    _ensure_gameplay_target(result, LOGRAM_TARGET)
    # AIR-01: mirror the proof target's verified turn costumes onto the gameplay toroid target (by
    # md5 reference — the same committed asset files, already provenance-recorded). Idempotent, so the
    # two stay in sync; a no-op when the proof costumes are absent (generation runs both to a fixpoint).
    # AIR-01 turn frames first (costume ordinals 1..7), then the WPN-02 explosion frames appended by
    # reference from solv_death (ordinals 8..15) — the recorded stand-in burst (record 025). Both are
    # already-verified, provenance-recorded assets; a no-op when either source is absent (fixpoint).
    # The shared sprite-extraction proof (record 002's pen) now holds more than one family's crops, so
    # each gameplay renderer mirrors ONLY its own family's frames, keyed by the `<family>/` name prefix
    # the extraction manifest assigns. Order within a family is preserved (costume ordinals 1..N).
    proof = next((t for t in result["targets"] if t.get("name") == TOROID_PROOF_TARGET), None)
    death = next((t for t in result["targets"] if t.get("name") == "solv_death"), None)
    proof_by_family = lambda prefix: (
        [c for c in copy.deepcopy(proof["costumes"]) if str(c.get("name", "")).startswith(prefix)]
        if proof is not None
        else []
    )
    toroid = next((t for t in result["targets"] if t.get("name") == TOROID_TARGET), None)
    if proof is not None and toroid is not None:
        toroid["costumes"] = proof_by_family("toroid/")
        if death is not None:
            toroid["costumes"].extend(copy.deepcopy(death["costumes"]))
        toroid["currentCostume"] = 0
    # AIR-06: the Terrazi renderer mirrors its 7 roll frames, then the shared explosion frames (the
    # same solv_death burst appended after them, ordinals 8.., exactly like the Toroid).
    terrazi = next((t for t in result["targets"] if t.get("name") == TERRAZI_TARGET), None)
    if proof is not None and terrazi is not None:
        terrazi["costumes"] = proof_by_family("terrazi/")
        if death is not None:
            terrazi["costumes"].extend(copy.deepcopy(death["costumes"]))
        terrazi["currentCostume"] = 0
    # AIR-05: the Kapi renderer mirrors its 7 dive frames, then the shared explosion frames (the same
    # solv_death burst appended after them, ordinals 8.., exactly like the Toroid/Terrazi).
    kapi = next((t for t in result["targets"] if t.get("name") == KAPI_TARGET), None)
    if proof is not None and kapi is not None:
        kapi["costumes"] = proof_by_family("kapi/")
        if death is not None:
            kapi["costumes"].extend(copy.deepcopy(death["costumes"]))
        kapi["currentCostume"] = 0
    # AIR-02: the Torkan renderer mirrors its 6 roll frames (TORKAN_ANIM_FRAMES; the rip has six for the
    # seven arcade codes, so the seventh code-step holds the last), then the shared explosion frames (the
    # same solv_death burst appended after them, ordinals 7.., exactly like the Toroid/Terrazi/Kapi).
    torkan = next((t for t in result["targets"] if t.get("name") == TORKAN_TARGET), None)
    if proof is not None and torkan is not None:
        torkan["costumes"] = proof_by_family("torkan/")
        if death is not None:
            torkan["costumes"].extend(copy.deepcopy(death["costumes"]))
        torkan["currentCostume"] = 0
    # AIR-03: the Zoshi renderer mirrors its 4 spin frames (ZOSHI_ANIM_FRAMES), then the shared explosion
    # frames (the same solv_death burst appended after them, ordinals 5.., exactly like the other families).
    zoshi = next((t for t in result["targets"] if t.get("name") == ZOSHI_TARGET), None)
    if proof is not None and zoshi is not None:
        zoshi["costumes"] = proof_by_family("zoshi/")
        if death is not None:
            zoshi["costumes"].extend(copy.deepcopy(death["costumes"]))
        zoshi["currentCostume"] = 0
    # AIR-04: the Jara renderer mirrors its 6 spin frames (JARA_ANIM_FRAMES), then the shared explosion
    # frames (the same solv_death burst appended after them, ordinals 7.., exactly like the other families).
    jara = next((t for t in result["targets"] if t.get("name") == JARA_TARGET), None)
    if proof is not None and jara is not None:
        jara["costumes"] = proof_by_family("jara/")
        if death is not None:
            jara["costumes"].extend(copy.deepcopy(death["costumes"]))
        jara["currentCostume"] = 0
    # GND-01: the Barra renderer mirrors its single idle pyramid frame (ordinal 1), then the shared
    # explosion burst (the same solv_death frames, ordinals 2..9 — the ground bomb-burst is a deferred
    # cosmetic, so the aerial burst stands in), then the two crater frames (ordinals 10..11) that the
    # HIT renderer flickers between once the burst finishes. Idempotent; a no-op when any source is
    # absent (generation runs to a fixpoint).
    barra = next((t for t in result["targets"] if t.get("name") == BARRA_TARGET), None)
    if proof is not None and barra is not None:
        barra["costumes"] = proof_by_family("barra/")
        if death is not None:
            barra["costumes"].extend(copy.deepcopy(death["costumes"]))
        barra["costumes"].extend(proof_by_family("crater/"))
        barra["currentCostume"] = 0
    # GND-01: the Garu Barra renderer mirrors its two 2x2 base pulse frames (ordinals 1..2), then the node
    # idle pyramid (ordinal 3, "garu/node reuses the Barra pyramid" -> the barra/idle frame mirrored in),
    # then the shared explosion burst (ordinals 4..11) the node plays before it vanishes. Idempotent; a
    # no-op when any source is absent (generation runs to a fixpoint).
    garu = next((t for t in result["targets"] if t.get("name") == GARU_TARGET), None)
    if proof is not None and garu is not None:
        garu["costumes"] = proof_by_family("garu/")
        garu["costumes"].extend(proof_by_family("barra/"))
        if death is not None:
            garu["costumes"].extend(copy.deepcopy(death["costumes"]))
        garu["currentCostume"] = 0
    # GND (ground.logram #71): the Logram renderer mirrors its 4 open/close dome frames (ordinals 1..4),
    # then the shared explosion burst (ordinals 5..12) and the two crater frames (ordinals 13..14) — the
    # SAME crater as the Barra, since a bombed Logram runs handle_bomb_explosion. Idempotent; a no-op when
    # any source is absent (generation runs to a fixpoint).
    logram = next((t for t in result["targets"] if t.get("name") == LOGRAM_TARGET), None)
    if proof is not None and logram is not None:
        logram["costumes"] = proof_by_family("logram/")
        if death is not None:
            logram["costumes"].extend(copy.deepcopy(death["costumes"]))
        logram["costumes"].extend(proof_by_family("crater/"))
        logram["currentCostume"] = 0
    # AIR-12: the enemy-bullet renderer uses a small stand-in — the Toroid's verified turn frames by
    # reference, drawn at a small size (dedicated bullet crops + the 4-colour pulse deferred, record 026).
    enemy_bullet = next((t for t in result["targets"] if t.get("name") == ENEMY_BULLET_TARGET), None)
    if proof is not None and enemy_bullet is not None:
        enemy_bullet["costumes"] = proof_by_family("toroid/")
        enemy_bullet["currentCostume"] = 0
    stage = next(target for target in result["targets"] if target["isStage"])
    owned_stage_variables = {
        STATE_ID,
        EPOCH_ID,
        SCOPE_ID,
        OUTCOME_ID,
        BOMB_INFLIGHT_ID,
        BOMB_DX_ID,
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
        # DEBUG (tracked for removal, #119): the T-key family-cycle cursor.
        DEBUG_SPAWN_INDEX_ID,
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
        # WPN-04: the in-flight bomb's accelerating scroll-axis velocity (init_bombing `_dX`).
        BOMB_DX_ID: ["bomb dx", 0],
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
        # DEBUG (tracked for removal, #119): the T-key family-cycle cursor (0-based into
        # DEBUG_SPAWN_FAMILIES); starts at the first family.
        DEBUG_SPAWN_INDEX_ID: ["debug spawn index", 0],
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
        AIM_DY_48_ID,
        AIM_DX_48_ID,
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
        GROUND_OBJECT_TYPE_ID,
        GROUND_OBJECT_SLOT_ID,
        GROUND_OBJECT_SPRITE_Y_ID,
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
        AIM_DY_48_ID: ["aim dy 48", list(AIM_DY_48)],
        AIM_DX_48_ID: ["aim dx 48", list(AIM_DX_48)],
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
        # into faithful parallel columns, each area = its records + a materialized sentinel row.
        # The two 16-entry index lists give each area's 1-based inclusive span into the columns, read at
        # runtime by area number. All read-only authority, sprite-write-forbidden.
        SCHEDULE_HANDLER_ID: ["schedule handler", list(SCHEDULE_HANDLERS)],
        SCHEDULE_TRIGGER_ROW_ID: ["schedule trigger row", list(SCHEDULE_ROWS)],
        SCHEDULE_PAYLOAD_ID: ["schedule payload", list(SCHEDULE_PAYLOADS)],
        # DIF-01/03 + FORM-01: the 4th parallel schedule column — the one runtime-readable scalar
        # each dispatched record needs (set-formation offset / fire-mask byte / ground-stop row; 0
        # otherwise), pre-decoded from the opaque payload. Same length as the other three columns.
        SCHEDULE_ARG_ID: ["schedule arg", list(SCHEDULE_ARGS)],
        # GND: three more parallel schedule columns carrying the add_ground_object scalars the single
        # `schedule arg` cannot — object type (the ground dispatch discriminator), slot (0-15), and
        # sprite_y (0-255). 0 on every non-ground row; same length as the other columns. Read-only
        # authority, sprite-write-forbidden, consumed by the ground dispatch.
        GROUND_OBJECT_TYPE_ID: ["schedule ground type", list(SCHEDULE_GROUND_TYPES)],
        GROUND_OBJECT_SLOT_ID: ["schedule ground slot", list(SCHEDULE_GROUND_SLOTS)],
        GROUND_OBJECT_SPRITE_Y_ID: ["schedule ground sprite y", list(SCHEDULE_GROUND_SPRITE_YS)],
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
        "target_a": slot_marker_blocks("target_a", CROSSHAIR_SLOT, "target_01"),
        "target_b": slot_marker_blocks("target_b", BOMB_TARGET_SLOT, "target_03"),
        "bomb": bomb_blocks(),
        "hud": hud_blocks(),
        "toroid": toroid_blocks(),
        "terrazi": terrazi_blocks(),
        "kapi": kapi_blocks(),
        "torkan": torkan_blocks(),
        "zoshi": zoshi_blocks(),
        "jara": jara_blocks(),
        "barra": barra_blocks(),
        "garu": garu_blocks(),
        "logram": logram_blocks(),
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
        elif target["name"] == TERRAZI_TARGET:
            # AIR-06: likewise, the only Terrazi render state is which flying slot each clone draws.
            target["variables"] = target["variables"] | {
                TERRAZI_CLONE_SLOT_ID: ["terrazi clone slot", 0],
            }
        elif target["name"] == KAPI_TARGET:
            # AIR-05: likewise, the only Kapi render state is which flying slot each clone draws.
            target["variables"] = target["variables"] | {
                KAPI_CLONE_SLOT_ID: ["kapi clone slot", 0],
            }
        elif target["name"] == TORKAN_TARGET:
            # AIR-02: likewise, the only Torkan render state is which flying slot each clone draws.
            target["variables"] = target["variables"] | {
                TORKAN_CLONE_SLOT_ID: ["torkan clone slot", 0],
            }
        elif target["name"] == ZOSHI_TARGET:
            # AIR-03: likewise, the only Zoshi render state is which flying slot each clone draws.
            target["variables"] = target["variables"] | {
                ZOSHI_CLONE_SLOT_ID: ["zoshi clone slot", 0],
            }
        elif target["name"] == JARA_TARGET:
            # AIR-04: likewise, the only Jara render state is which flying slot each clone draws.
            target["variables"] = target["variables"] | {
                JARA_CLONE_SLOT_ID: ["jara clone slot", 0],
            }
        elif target["name"] == ENEMY_BULLET_TARGET:
            # AIR-12: likewise, the only enemy-bullet render state is which bullet slot each clone draws.
            target["variables"] = target["variables"] | {
                ENEMY_BULLET_CLONE_SLOT_ID: ["enemy bullet clone slot", 0],
            }
        elif target["name"] == BARRA_TARGET:
            # GND-01: the only Barra render state is which GROUND slot each clone draws, snapshotted at
            # creation. All entity state lives in the Stage slot lists the clone reads.
            target["variables"] = target["variables"] | {
                BARRA_CLONE_SLOT_ID: ["barra clone slot", 0],
            }
        elif target["name"] == GARU_TARGET:
            # GND-01: likewise, the only Garu render state is which GROUND slot each clone draws.
            target["variables"] = target["variables"] | {
                GARU_CLONE_SLOT_ID: ["garu clone slot", 0],
            }
        elif target["name"] == LOGRAM_TARGET:
            # GND (ground.logram #71): likewise, the only Logram render state is which GROUND slot each
            # clone draws; the dome frame and crater clock live in the Stage slot lists the clone reads.
            target["variables"] = target["variables"] | {
                LOGRAM_CLONE_SLOT_ID: ["logram clone slot", 0],
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
