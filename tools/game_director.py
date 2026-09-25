#!/usr/bin/env python3
"""Generate and verify the slice-2 Scratch game director."""

from __future__ import annotations

import argparse
from collections import namedtuple
import copy
import functools
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
# GND-05 (ground.boza-logram #87) cross-slot link (the reference's per-object `_EXTRA` pointer, port as a
# slot index rather than a RAM pointer). A Boza Logram is a five-slot composite: the four outer domes each
# carry the 1-based ground-slot index of their shared centre here, and the centre carries 0. So `slot link`
# doubles as the outer/centre discriminator (>0 = outer, 0 = centre) AND the address an outer writes to when
# it downgrades the centre's point value on being hit (update_centre_points_value $1E1E). It stays 0 for
# every non-Boza occupant (clear-slots zeroes it), so it is inert unless spawn_boza writes it.
SLOT_LINK_ID = "slot-link"  # cross-slot link (Boza outer -> centre slot index; _EXTRA)
# GND-07 (ground.domogram #89): the count of path vectors still to load (the reference's per-object _NVEC, in the
# timer table at offset 1). A Domogram follows a scripted path with a per-slot pointer (`slot link` = _EXTRA, the
# 1-based index of the NEXT step in the shared path columns) and the current step's remaining duration
# (`slot flag` = _VECLEN); this third counter says how many steps remain. When it reaches 0 the follower stops
# loading and holds the last vector (domogram_done_all_vectors). It stays 0 for every non-Domogram occupant
# (clear-slots zeroes it), so it is inert unless a Domogram spawn writes it.
SLOT_VEC_LEFT_ID = "slot-vec-left"  # path vectors remaining to load (_NVEC)
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
    (SLOT_LINK_ID, "slot link"),
    (SLOT_VEC_LEFT_ID, "slot vec left"),
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
# AIR-07/AIR-08 Zakato phase sentinels. A teleporting or self-detonating Zakato is stamped a NON-ACTIVE
# flying-slot state so the shared `check air hit` gate (`== SLOT_ACTIVE`) excludes it for free — the same
# device the Garu Barra base uses (SLOT_GARU_BASE above), and the arcade's own `_STATE=3` "indestructible"
# during the teleport sparkle (init_teleport $2826) and "benign" during the self-destruct explosion
# (zakato_shoot $25B9, brag_zakato_explode $2791). These live in the disjoint FLYING band (59-64) and are
# read ONLY by the Zakato update's phase dispatch; every other flying seam keys off `slot type != 0`
# (spawn refill, walk occupancy, renderer) or `== SLOT_ACTIVE` (hit/craft collision), never these values.
# The one slot-state -> HIT write stays inside `resolve hit` (SYS-03): a self-destruct never writes HIT, so
# it never scores; a player hit still routes through `resolve hit` -> SLOT_HIT -> the shared explosion.
SLOT_TELEPORT = 4  # teleport-in sparkle phase: indestructible, not yet hittable
SLOT_SELF_EXPLODE = 5  # fired-and-vanishing phase: benign, animating its own burst, awards nothing
# WPN-01 (player.bacura-bounce #77): a player shot that has struck a Bacura is stamped this NON-ACTIVE
# shot-slot state by `check shot bacura`. Distinct from SHOT_SPENT (3) so the blaster clone can tell a
# bacura bounce (reverse + animate, then delete — arcade shot_destroyed 2400-2417) from an ordinary
# air-kill spend (delete at once). The Bacura itself is never touched: the shot bounces, the slab lives.
SHOT_BOUNCE = 6
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
# WPN-01 player-shot vs Bacura window. The reference's `check_shot_hit_bacura` ($19CB, 2583-2595) uses
# `sub #24; add #32` (Y) and `sub #8; add #16` (X) → shotY-bacuraY in [-24,7], bacuraX-shotX in [-8,7]
# half-pixel shadow units — a taller low bias than the flying box (the slab sits lower). This port DOUBLES
# it to (48,64,16,32) for the SAME recorded reasons as HIT_WINDOW_SHOT_FLYING above: the shot is the same
# fast mover (changeyby 20 = 2.5 cells/frame), so the 2-cell arcade Y window tunnels — a shot would skip
# clean over the thin (one-row-tall) slab instead of bouncing; and the doubled box matches the rendered
# 24x16 slab body. Same fast-shot detector class, same ratified deviation — not a new one.
HIT_WINDOW_SHOT_BACURA = (48, 64, 16, 32)
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
# GND-07 (ground.domogram #89): a Domogram carries a SCRIPTED PATH the three ground scalars cannot express,
# so two more per-record schedule columns give each row the 1-based START index of its path in the shared
# step columns and the step COUNT (0 for every non-Domogram row). The shared step columns themselves
# (duration + vector index per step, concatenated across all instances) are DOMOGRAM_PATH_*_ID below.
SCHEDULE_DOMOGRAM_PATH_START_ID = "area-schedule-domogram-path-start"
SCHEDULE_DOMOGRAM_PATH_COUNT_ID = "area-schedule-domogram-path-count"
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
# GND-07 (ground.domogram #89): the Domogram spawn handler. Like add_ground_object it places one ground object
# (object_type + params.slot + params.sprite_y), but it ALSO carries a scripted path (params.path = a list of
# {duration, vector_index} steps, params.path_step_count = its length) the follower consumes.
ADD_DOMOGRAM_HANDLER = "add_domogram_with_path"
# AIR-11 (air.bacura #81): the two Bacura schedule handlers. `set_bacura_count` (arcade opcode 0x22,
# sub_2_fn_6__set_bacura_inc_cnt $075D: `move.b (a0)+,(bacura_inc_cnt)`) sets the per-window increment
# quota; `reset_bacura_count` (opcode 0x23, sub_2_fn_7__reset_num_bacura $05D8: `clr.b (num_bacura)`)
# clears the active count. The pump (install_pump_bacura) consumes both.
SET_BACURA_COUNT_HANDLER = "set_bacura_count"
RESET_BACURA_COUNT_HANDLER = "reset_bacura_count"

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
# The Derota fire-mask display name + id, captured into a spawned Derota / Garu Derota node's
# `slot fire mask` at dispatch (GND-04) and consumed by the shared fire-permission gate. Derived from
# FIRE_MASK_FAMILIES so the two never drift if a family's id is renamed.
FIRE_MASK_DEROTA_NAME = next(n for s, n, i in FIRE_MASK_FAMILIES if s == "derota")
FIRE_MASK_DEROTA_ID = next(i for s, n, i in FIRE_MASK_FAMILIES if s == "derota")

# GND-05 (ground.boza-logram #87): the Boza Logram outer domes fire on the same shared gate under their own
# captured mask (ffreq_mask_boza_logram, handle_boza_logram_outer). Derived from FIRE_MASK_FAMILIES too.
FIRE_MASK_BOZA_NAME = next(n for s, n, i in FIRE_MASK_FAMILIES if s == "boza_logram")
FIRE_MASK_BOZA_ID = next(i for s, n, i in FIRE_MASK_FAMILIES if s == "boza_logram")

# GND-07 (ground.domogram #89): a Domogram fires on the same shared gate under its own captured mask
# (ffreq_mask_domogram, handle_2E_Domogram). Derived from FIRE_MASK_FAMILIES too.
FIRE_MASK_DOMOGRAM_NAME = next(n for s, n, i in FIRE_MASK_FAMILIES if s == "domogram")
FIRE_MASK_DOMOGRAM_ID = next(i for s, n, i in FIRE_MASK_FAMILIES if s == "domogram")

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


def _load_domogram_vectors() -> tuple[list[int], list[int]]:
    # GND-07 (ground.domogram #89): the 32 (dY,dX) path deltas (domogram.json, from domogram_vector_tbl),
    # split into two parallel port lists in index order — `dx` on the SCROLL/depth axis and `dy` on the
    # LATERAL axis (the arcade object's _dX/_dY; a Domogram follows both). Decoded here to list-position order
    # (position p, 1-based, is index p-1), fail LOUD if the entries are not exactly indices 0..31 once each, so
    # a future domogram.json regeneration that dropped or duplicated a vector is caught here, not in play. The
    # raw deltas feed straight into the velocity-only seam (`advance ground moving`, TICK_VELOCITY_SCALE*delta) —
    # no scroll baseline: the terrain scroll is baked into the delta (dX 8 = scroll-matched), so dX 16 races down
    # at 2x scroll and dX 0 holds on the terrain (docs/mechanics/043, plan-review MAJOR #1).
    vectors = _load_spec_data("domogram.json")["vector_table"]["vectors"]
    ordered = sorted(vectors, key=lambda v: v["index"])
    if [v["index"] for v in ordered] != list(range(len(ordered))):
        raise SystemExit(
            f"domogram.json must define exactly the contiguous vector indices 0..{len(ordered) - 1}, once each"
        )
    return [v["dx"] for v in ordered], [v["dy"] for v in ordered]


DOMOGRAM_VECTOR_DX, DOMOGRAM_VECTOR_DY = _load_domogram_vectors()

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
AIM_DY_64_ID = "aim-dy-64"  # Sheonite/Giddo-Spario tier (magnitude 64 = 4 px/frame)
AIM_DX_64_ID = "aim-dx-64"


def _load_aiming_tables() -> dict[str, list[int]]:
    data = _load_spec_data("aiming.json")["aiming"]
    tables = {"octant": list(data["octant_table"]["values"])}
    for tier in ("toroid", "generic", "terrazi_torkan", "sheonite"):
        vectors = data["angle_tables"][tier]["vectors"]
        tables[f"{tier}_dy"] = [v["dy"] for v in vectors]
        tables[f"{tier}_dx"] = [v["dx"] for v in vectors]
    return tables


_AIMING = _load_aiming_tables()
OCTANT_TABLE = _AIMING["octant"]
AIM_DY_24, AIM_DX_24 = _AIMING["toroid_dy"], _AIMING["toroid_dx"]
AIM_DY_32, AIM_DX_32 = _AIMING["generic_dy"], _AIMING["generic_dx"]
AIM_DY_48, AIM_DX_48 = _AIMING["terrazi_torkan_dy"], _AIMING["terrazi_torkan_dx"]
AIM_DY_64, AIM_DX_64 = _AIMING["sheonite_dy"], _AIMING["sheonite_dx"]

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

# AIR-12 radiating-spread emission: the shared mechanism that fires ONE non-homing bullet at an
# EXPLICIT direction the caller chooses, rather than one aimed at the craft. The angle is a 5-bit
# index (0..31) into the 48-magnitude tier (`angle_dX_dY_terrazi_torkan_tbl`, 3 px/frame), matching
# the reference's `init_radiating_bullet` ($32C4 -> `cpy_dY_dX_to_obj` $3383). The Brag Zakato fan
# and Garu Zakato ring (slice 11, air.special-pairs) drive this in a loop, stepping the angle. The
# emitted bullet is an ordinary BULLET_TYPE slot that flies straight via `install_update_bullet` --
# the port folds the arcade's straight `handle_07_Garu_Zakato_Bullet` into the single non-homing
# bullet update (recorded in 026), so radiating bullets need no distinct motion, only this emitter.
RADIATING_ANGLE_ID = "radiating-angle"  # caller-supplied 5-bit direction (0..31); index = angle+1
RADIATING_EMIT_PROCCODE = "emit radiating bullet"

# WPN-01 (player.bacura-bounce #77): the shot-vs-Bacura detector, called per live slab from
# `update bacura`. It marks an overlapping player shot for the bounce (SHOT_BOUNCE) and never touches the
# slab — the reference routes a shot that hits a Bacura through `deactivate_shot` (STATE=3 + BACURA_HIT_SND)
# while the Bacura is untouched (check_shot_hit_bacura 2583-2595; deactivate_shot 2557-2561).
CHECK_SHOT_BACURA_PROCCODE = "check shot bacura"

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
# GND-06/07 (ground.grobda #88 / ground.domogram #89): the per-tick motion for a SELF-MOVING ground object —
# the first ground slot that moves under its own velocity rather than being glued to the terrain scroll. It
# mirrors the source's move_object_dX / move_object_dX_dY (xevious_main.68k 4817-4846): move by
# TICK_VELOCITY_SCALE * (slot dx, slot dy), then the SAME off-bottom cull `advance ground` uses. There is NO
# AREA_PROGRESS_STEP scroll baseline added here: the source has no separate scroll term for a moving object —
# the terrain scroll is BAKED INTO the stored delta (raw 8 = scroll-matched, TICK_VELOCITY_SCALE*8 = 32 =
# AREA_PROGRESS_STEP), so adding a baseline would DOUBLE the along-scroll speed. Static families keep
# `advance ground`; a Grobda leaves `slot dy` 0 (scroll-axis-only) and a Domogram drives both axes.
ADVANCE_GROUND_MOVING_PROCCODE = "advance ground moving"
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
# GND-02 (ground.zolbak #85): the per-tick update for a Zolbak — a passive dome that NEVER fires. It is
# the Barra crater model (terrain scroll while ACTIVE; a persistent crater once bombed) plus ONE extra
# behaviour on the first HIT tick: reduce the enemy AI level by 2, floored at 0 (handle_1F_Zolbak ->
# reduce_enemy_ai_by_2). Guarded on `slot timer == 0` so it fires exactly once per kill, never every
# crater tick.
UPDATE_ZOLBAK_PROCCODE = "update zolbak"
# GND-04 (ground.derota #86): the per-tick update for a Derota — a plain periodic aimed turret (NO
# open/close dome cycle, unlike the Logram). While ACTIVE and still high enough on the field (arm gate
# `cur_row <= ground stop firing row`, arcade `gnd_stop_firing_row` compare) it drives the SHARED
# fire-permission gate (chk_timer_fire_bullet_reinit_timer): one aimed bullet per masked-random reload.
# Once bombed (HIT) it craters PERSISTENTLY exactly like the Barra (handle_bomb_explosion). Both states
# scroll + cull via `advance ground`.
UPDATE_DEROTA_PROCCODE = "update derota"
# GND-04 (ground.derota #86): the per-tick update for a Garu Derota part — a two-slot object (like the
# Garu Barra) but with a FIRING node. The indestructible 2x2 base (state SLOT_GARU_BASE) only ever
# scrolls; the destructible node (state ACTIVE) drives the shared fire-permission gate EVERY active tick
# (garu_derota_handler calls chk_timer_fire_bullet_reinit_timer with NO stop-firing-row gate, unlike the
# single Derota) and, once bombed (HIT), plays the SHORTER explode-and-remove burst and VANISHES (no
# crater), exactly like the Garu Barra node.
UPDATE_GARU_DEROTA_PROCCODE = "update garu derota"
# GND-05 (ground.boza-logram #87): the per-tick update for one slot of a Boza Logram composite
# (handle_2D_Boza_Logram $1CDE). All five slots share BOZA_LOGRAM_TYPE, so this ONE proc branches on
# `slot link`: an OUTER (link > 0) is a full Logram (open/close + one aimed shot on the same masked-random
# cycle) that, once bombed, downgrades its linked centre's point value to 600 (update_centre_points_value
# $1E1E) and then craters; the CENTRE (link == 0) never fires, and when bombed it cascades — setting its four
# outers HIT directly (destroy_all_outer_lograms $1D8A), which clears them WITHOUT scoring — before it craters.
# Both roles scroll + cull via `advance ground`.
UPDATE_BOZA_PROCCODE = "update boza"
# GND-06 (ground.grobda #88): the tank/stingray family. One proc for all 12 live variants; it branches
# internally on `slot type` for the per-variant reticle trigger + reaction, and on land-vs-water for the
# death (crater vs vanish). Every variant moves by its own velocity through `advance ground moving`.
UPDATE_GROBDA_PROCCODE = "update grobda"
# GND-07 (ground.domogram #89): the path-driven slider that fires. One proc: while ACTIVE it follows its
# scripted path (holding each vector for its duration, then the last vector forever) through `advance ground
# moving`, and runs its masked-random shot cycle (a 24-frame animation firing one aimed bullet at the midpoint);
# once bombed (HIT) it craters PERSISTENTLY like the Barra (handle_bomb_explosion) via `advance ground`.
UPDATE_DOMOGRAM_PROCCODE = "update domogram"
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
INIT_ZAKATO_PROCCODE = "init zakato"  # AIR-07: shared initializer for the four base Zakato variants
UPDATE_ZAKATO_PROCCODE = "update zakato"  # AIR-07: shared teleport/active/self-destruct phase dispatch
INIT_GIDDO_SPARIO_PROCCODE = "init giddo spario"  # AIR-10: aim-once 64-tier flyby init
UPDATE_GIDDO_SPARIO_PROCCODE = "update giddo spario"  # AIR-10: straight flight + own short burst
EXPLODE_GIDDO_SPARIO_PROCCODE = "explode giddo spario tick"  # AIR-10: the 8-frame burst exception
INIT_BRAG_SPARIO_PROCCODE = "init brag spario"  # AIR-10: accelerating-homer init
UPDATE_BRAG_SPARIO_PROCCODE = "update brag spario"  # AIR-10: per-tick homing acceleration
INIT_BRAG_ZAKATO_PROCCODE = "init brag zakato"  # AIR-08: shared teleport-in init for both Brag variants
UPDATE_BRAG_ZAKATO_PROCCODE = "update brag zakato"  # AIR-08: teleport/active/self-destruct + terminal fan
BRAG_ZAKATO_SHOOT_PROCCODE = "brag zakato shoot"  # AIR-08: the terminal 5-bullet aimed radiating fan
INIT_GARU_ZAKATO_PROCCODE = "init garu zakato"  # AIR-08: no-teleport straight flyer init (random lateral Y)
UPDATE_GARU_ZAKATO_PROCCODE = "update garu zakato"  # AIR-08: straight flight + fuse -> detonate
GARU_ZAKATO_DETONATE_PROCCODE = "garu zakato detonate"  # AIR-08: 16-bullet ring + 4 Brag Sparios, then free
INIT_BACURA_PROCCODE = "init bacura"  # AIR-11: stamp one slab into a reserved-band slot at the top row
UPDATE_BACURA_PROCCODE = "update bacura"  # AIR-11: craft-touch death + drift down + cull; NO shot hit-test
PUMP_BACURA_PROCCODE = "pump bacura"  # AIR-11: per-tick inc->init live spawn pump (main_fn_5 + main_fn_3)
FIRE_GATE_PROCCODE = "fire permission gate"  # the shared, family-agnostic periodic-fire gate
CULL_SLOT_PROCCODE = "cull slot"
# DEBUG (temporary playtest tool, tracked for removal): while the debug key is held, force the flying
# formation to a Terrazi wave so a family that only spawns at high AI levels is reachable for a
# playtest. Amends the locked control mapping (needs guardrail-ack). See docs/spec/core-game-systems.md
# and the removal issue #119 (remove once all aerial families are built and playtested).
DEBUG_SPAWN_PROCCODE = "debug spawn wave"
DEBUG_SPAWN_KEY = "t"  # T = cycle a single debug enemy through the buildable families
DEBUG_SPAWN_INDEX_ID = "debug-spawn-index"  # which DEBUG_SPAWN_FAMILIES entry T brings in next
# DEBUG (temporary playtest tool, tracked for removal #119): the GROUND analog of the T key. Ground objects
# only enter by scrolling up from the area schedule — a narrow, one-shot, non-repeatable window — so a
# specific ground family (a five-slot Boza composite especially) is impractical to reach for a bomb test.
# While the debug ground key (G) is held, CYCLE the built ground families one at a time into the ground band
# from the top of the field, so each family's whole lifecycle (enter, scroll, fire if it fires, bomb ->
# crater/score) is reachable in isolation and repeatably. Like the T key it self-gates on the key (normal
# play untouched) and amends the LOCKED control mapping (docs/spec/core-game-systems.md; needs guardrail-ack).
DEBUG_GROUND_SPAWN_PROCCODE = "debug ground spawn"
DEBUG_GROUND_KEY = "g"  # G = cycle a single debug GROUND family (G for ground; freed when the death fixtures went)
DEBUG_GROUND_INDEX_ID = "debug-ground-index"  # which DEBUG_GROUND_FAMILIES entry G brings in next
DEBUG_GROUND_SPRITE_Y = 112  # lateral column for the debug spawn — a central, common column (schedule median)
# DEBUG (temporary playtest tool, tracked for removal #119): a PAUSE/FREEZE key so the operator can stop the
# action on a single frame and take an OS screenshot of a ground- or air-enemy issue to report. It is a TOGGLE
# on the P key (tap to freeze, tap again to resume) — deliberately a toggle, not a hold, so the operator has
# both hands free to drive the OS screenshot tool while the frame is held. While paused, the whole per-tick
# walk (input, area clock, object walk, bomb, spawns, death) is skipped; only the toggle's own rising-edge
# detector runs each tick, so a second tap resumes. It amends the LOCKED control mapping (needs guardrail-ack)
# and is never pressed by the headless harness (`debug paused` defaults 0), so automated play is unaffected.
DEBUG_PAUSE_PROCCODE = "debug pause toggle"
DEBUG_PAUSE_KEY = "p"  # P = pause/resume (toggle) for the playtest
PAUSED_ID = "debug-paused"  # 1 while frozen, 0 while running; the walk body is gated on == 0
PAUSE_KEY_HELD_ID = "debug-pause-key-held"  # previous-tick P sample, for a rising-edge (tap) toggle
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
# The flying-type-table offsets whose runs select each base Zakato variant (object-types.json 0-based run
# starts): slow (0x12) at 54-56 and closeY (0x13) at 57-59 are three-wide runs; fast (0x14) at 60-65 is
# six-wide; cont (0x15) at 110-113 is four-wide. The debug spawner forces `formation count` = 1, so only
# the run's first position is read and the shorter runs are immaterial. NATURAL reachability differs by
# variant: the area-1..16 formation waves DO reach fast (0x14, e.g. areas 4/9/14) and cont (0x15, areas
# 9/14) through the AI-level formation table, so those two appear in normal play; slow (0x12) and closeY
# (0x13) are NOT scheduled in any built area, so the debug key is the only way to see them until later
# areas are wired (see docs/mechanics/034 for the schedule trace).
ZAKATO_SLOW_FORMATION_OFFSET = 54
ZAKATO_CLOSEY_FORMATION_OFFSET = 57
ZAKATO_FAST_FORMATION_OFFSET = 60
ZAKATO_CONT_FORMATION_OFFSET = 110
# AIR-08 Brag Zakato: the flying-type-table runs whose first code selects each Brag variant
# (object-types.json 0-based): rnd (0x16) is a four-wide run at 84-87, closeY (0x17) a four-wide run
# at 88-91. The debug spawner forces `formation count` = 1, so only the run's first position is read.
# NATURAL reachability is AI-level/formation-table dependent (set_flying_formation's signed offset is
# an index into flying_enemy_offset_tbl, not a direct type-table offset), so the debug key is the
# deterministic playtest lever, exactly as for the base slow/closeY variants.
BRAG_ZAKATO_RND_FORMATION_OFFSET = 84
BRAG_ZAKATO_CLOSEY_FORMATION_OFFSET = 88
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
# slice-10 aerial families (Toroid, Terrazi, Kapi, Torkan, Zoshi, Jara) all spawn. AIR-07 (slice 11)
# retires the deviation for the four base Zakato teleporter variants (0x12-0x15); Giddo Spario (0x08),
# Brag Spario (0x09), the Brag Zakato pair (0x16/0x17) and the Garu Zakato (0x18) arrive with the rest
# of the Zakato cluster (AIR-08/AIR-10). Any still-unhandled formation type is silently skipped by the
# spawner (the "fewer enemies" deviation), retired only for the built families, not all aerials.
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
# AIR-07 Zakato (Teleporter): four object types (handle_12-15, 3733-3859) sharing one teleport-in
# sparkle, one hittable-active phase and one self-destruct burst; they differ only in movement, in WHEN
# they fire their single aimed bullet, and in points. All four teleport in invulnerable, become hittable,
# fire ONE generic aimed bullet (init_new_bullet 3764) then self-destruct awarding nothing; killed while
# active they score their variant value. Codes 0x12-0x15 (18-21).
ZAKATO_SLOW_TYPE = 18  # 0x12, handle_12_Zakato_slow: drifts straight 1 px/f, random 0-255 fuse, 100 pts
ZAKATO_CLOSEY_TYPE = 19  # 0x13, handle_13_Zakato_closeY: drifts straight 1 px/f, fires when level in Y, 200 pts
ZAKATO_FAST_TYPE = 20  # 0x14, handle_14_Zakato_fast: aimed 2 px/f, random 0-63 fuse, 150 pts
ZAKATO_CONT_TYPE = 21  # 0x15, handle_15_Zakato: aimed 2 px/f, fires when level in Y, 300 pts
ZAKATO_TYPES = (ZAKATO_SLOW_TYPE, ZAKATO_CLOSEY_TYPE, ZAKATO_FAST_TYPE, ZAKATO_CONT_TYPE)
# AIR-07 Zakato behaviour constants (handle_12-15, 3733-3859; init_teleport 3994; zakato_teleport 3961;
# zakato_shoot 3761; zakato_explode 3931). Points are 1-based value-table positions (VALUE_TABLE_POINTS):
# the arcade _PTS bytes 15/21/18/27 name arcade-table slots for 100/200/150/300, which are positions
# 6/8/7/10 in this port's 22-entry decoded table (same remap as TERRAZI_PTS 14 -> 700).
ZAKATO_SLOW_PTS = 6  # 100 points (handle_12 _PTS byte 15)
ZAKATO_CLOSEY_PTS = 8  # 200 points (handle_13 _PTS byte 21)
ZAKATO_FAST_PTS = 7  # 150 points (handle_14 _PTS byte 18)
ZAKATO_CONT_PTS = 10  # 300 points (handle_15 _PTS byte 27)
ZAKATO_MAIN_CODE = 0x11  # active-phase body sprite code (zakato_NN_main move.b #0x11,_CODE)
# The teleport-in sparkle and the self-destruct burst share ONE 6-frame set (arcade sprite codes
# 4,5,6,7,8,0xC): zakato_exploding_sprite_tbl runs them FORWARD (4->0xC) for the burst, and
# zakato_teleport_sprite_tbl is the same list REVERSED (0xC->4) for the sparkle. Each shows 5 phases,
# 4 arcade frames apart ((timer>>2)&7, done at >=5 -> ~20 frames), like the shared Toroid burst.
ZAKATO_BURST_FRAMES = 6
ZAKATO_ANIM_PHASE_FRAMES = 4  # arcade frames per sparkle/burst phase (timer>>2)
ZAKATO_ANIM_PHASES = 5  # phases shown before the phase completes (cmp #5, jcc done)
# Both the teleport-in sparkle and the self-destruct burst run ANIM_PHASES phases of ANIM_PHASE_FRAMES
# each = 20 arcade frames (equal to the shared flying burst TOROID_HIT_DURATION_FRAMES, so the self-
# destruct reuses `explode toroid tick` for its clock-and-free). The update advances `slot timer` by
# TICK_TIMER_STEP/tick, so completion is `slot timer >= ZAKATO_PHASE_FRAMES`.
ZAKATO_PHASE_FRAMES = ZAKATO_ANIM_PHASE_FRAMES * ZAKATO_ANIM_PHASES  # 20
# Straight drifters (slow/closeY) move on the scroll axis at the arcade's raw dX=16 (1 px/frame),
# dY=0 (handle_12/13 3736-3737); fast/continuous aim at the craft on the 32-magnitude generic tier
# (angle_dX_dY_tbl, 2 px/frame), like the Kapi/Torkan approach. The two fused variants draw a random
# shot countdown when the teleport completes: slow (rng & 0xff)+1 = 1-256, fast (rng & 0x3f)+1 = 1-64
# (3745-3748 / 3814-3817). The Y-triggered variants (closeY/continuous) fire when |solvalou.Y - self.Y|
# is within the arcade's close band (3793-3797) rather than on a countdown.
ZAKATO_STRAIGHT_DX = 16  # raw scroll-axis velocity for slow/closeY (arcade dX=16, 1 px/frame)
ZAKATO_SLOW_FUSE_SPAN = 256  # (rng mod 256) + 1 = 1-256 arcade frames (handle_12 and.w #0xff)
ZAKATO_FAST_FUSE_SPAN = 64  # (rng mod 64) + 1 = 1-64 arcade frames (handle_14 and.w #0x3f)
# Close-in-Y fire trigger: the arcade tests (solvalou.Y - self.Y - 4 + 8) carrying, i.e. the signed MSB
# difference lands in [-4, 3] cells (3793-3796). `slot y` is the lateral axis in 1/32-px units; a cell is
# SLOT_UNITS_PER_CELL. Fire when the lateral offset (player col - self col) is within this band.
ZAKATO_CLOSEY_LOW = -4
ZAKATO_CLOSEY_HIGH = 3
# AIR-10 Spario: two INDEPENDENT projectile-like flyers with distinct motion and distinct death.
# Giddo Spario (handle_08_Giddo_Spario 5219-5240) is aimed ONCE at the craft at spawn on the fast
# 64-magnitude tier (angle_dX_dY_sheonite_tbl, 4 px/frame — faster than any other family), then flies
# straight and NEVER fires; killed, it plays its OWN short burst (giddo_spario_hit 5241-5253), the one
# documented exception to the shared ~20-frame flying explosion. Brag Spario (handle_09_Brag_Spario
# 3080-3129) is an accelerating homer: each frame it nudges its velocity by +/-2 raw toward the craft
# on each axis (0 if aligned) and moves by the accumulated velocity, unbounded; it uses the shared
# flying explosion. Brag Sparios also arrive four-at-a-time from the Garu Zakato detonation (AIR-08,
# air.special-pairs) — this handler exists first so that consumer can spawn them.
GIDDO_SPARIO_TYPE = 8  # 0x08, handle_08_Giddo_Spario: aim-once 4 px/f flyby, no fire, own short burst
BRAG_SPARIO_TYPE = 9  # 0x09, handle_09_Brag_Spario: accelerating homer, shared explosion
GIDDO_SPARIO_PTS = 1  # 10 points (handle_08 _PTS byte 0 -> value-table position 1)
BRAG_SPARIO_PTS = 12  # 500 points (handle_09 _PTS byte 33 -> position 12; the port has no super-xevious)
# Giddo flight animation: the arcade cycles CODE through 4 frames from its clock ((TIMER>>1)&3, 5229-5233);
# the port derives the frame from `slot timer` in the renderer. Spawn on frame 0.
GIDDO_SPARIO_INIT_CODE = 0
GIDDO_SPARIO_FLIGHT_FRAMES = 4  # flight sprites (arcade CODE 0..3)
# Giddo's OWN short burst (giddo_spario_hit 5241-5253): the arcade shows 4 burst sprites (CODE 4..7),
# each for 2 arcade frames ((TIMER>>1), remove at ==4), so 8 arcade frames total — versus the shared
# 20-frame flying burst. It keeps moving on its velocity while the burst plays, like the shared one.
GIDDO_SPARIO_BURST_FRAMES = 4  # burst sprites (arcade CODE 4..7)
GIDDO_SPARIO_HIT_DURATION_FRAMES = 8  # burst runs 8 arcade frames, then the slot frees
# Brag homing acceleration: the arcade adds +/-2 raw to each velocity axis per arcade frame toward the
# craft (0 when the MSB cells are equal); one port tick is two arcade frames, so the per-tick step is
# 2*2 = 4 raw, matching the port's 2-frames-per-tick velocity convention (slot dx/dy hold raw arcade
# velocity, moved by TICK_VELOCITY_SCALE). Velocity is unbounded, exactly as the arcade (no clamp).
BRAG_SPARIO_ACCEL = 4  # raw velocity step per tick per axis (arcade +/-2/frame over 2 frames)
BRAG_SPARIO_INIT_CODE = 0  # single body sprite; the arcade animates via ATTR flip, not CODE (3117-3119)
# Giddo Spario's flying-type-table run (object-types.json 0-based): 0x08 is a six-wide run starting at
# offset 39 (also 94/102/114). The debug spawner forces count 1 and reads only the run's first code, so
# offset 39 gives the operator a solo Giddo on demand; natural area waves reach it through the AI-level
# formation table. Brag Spario (0x09) is NOT in the type table at all — it is never a formation enemy;
# it spawns only four-at-a-time from the Garu Zakato detonation (AIR-08, same PR), so it has no debug
# formation entry and its in-play proof arrives with air.special-pairs.
GIDDO_SPARIO_FORMATION_OFFSET = 39
# AIR-08 Brag Zakato (Cracker) + Garu Zakato (Bullseye): the "special pairs" — the last of the Zakato
# cluster. Two Brag variants teleport in exactly like the base Zakato (init_teleport, ~20-frame sparkle,
# indestructible during it) but END their life with a terminal 5-bullet AIMED radiating FAN (two
# angle-steps apart) rather than a single aimed bullet: the rnd variant on a 1-64 random fuse
# (handle_16 3863), the closeY variant when the craft is level in Y (handle_17 3893). Both are aimed at
# the craft on the 32-magnitude generic tier while alive (calc_dX_dY_for_vector_to_solvalou), body code
# 0x12. The Garu Zakato does NOT teleport (handle_18 4010): it enters at a random lateral column
# (gen_random_Y_store_obj), flies STRAIGHT down the scroll axis at 3 px/frame (dX=48), and — left alone
# — detonates on a 32-63 fuse into a 16-bullet 360-degree ring PLUS 4 Brag Sparios and vanishes with NO
# explosion or score (init_garu_zakato_explosion 5075); shot first, it scores its value on the shared
# flying kill. Codes 0x16/0x17/0x18 (22/23/24); the port type byte equals the arcade handle number.
BRAG_ZAKATO_RND_TYPE = 22  # 0x16, handle_16_Brag_Zakato_rnd: teleport in, random 1-64 fuse, terminal fan
BRAG_ZAKATO_CLOSEY_TYPE = 23  # 0x17, handle_17_Brag_Zakato_closeY: teleport in, fires level-in-Y, terminal fan
GARU_ZAKATO_TYPE = 24  # 0x18, handle_18_Garu_Zakato: no teleport, straight 3 px/f, fuse -> ring + 4 Sparios
# AIR-09 Sheonite (indestructible escort PAIR). The two arcade object codes handle_31_right_sheonite (0x31,
# main:4052) and handle_32_left_sheonite (0x32, main:4162). Both set _STATE=3 (indestructible) on their
# first walk, so EVERY hit test — the shot/score test AND the craft-collision test — skips them (all gate
# STATE==2). The port realizes that total inertness structurally: the Sheonite walk branch OMITS the
# CHECK_AIR_HIT call (like the Bacura), AND no craft-overlap detector targets its slots. The pair collides
# with nothing (no shot, no score, no bomb, no craft-death — confirmed at the pin + operator). Unlike the
# Bacura, its codes never collide with SHOT_TYPE, so it lives in the shared flying pool and dispatches by
# `walk type` (the Jara two-type OR model), needing no reserved band.
RIGHT_SHEONITE_TYPE = 49  # 0x31, handle_31_right_sheonite: homes, docks, then retreats along the scroll axis
LEFT_SHEONITE_TYPE = 50  # 0x32, handle_32_left_sheonite: homes, docks, then vanishes
BRAG_ZAKATO_TYPES = (BRAG_ZAKATO_RND_TYPE, BRAG_ZAKATO_CLOSEY_TYPE)
# Points are 1-based value-table positions (VALUE_TABLE_POINTS): rnd 600 -> 13, closeY 1500 -> 18, Garu
# 1000 -> 17 (arcade _PTS bytes 36/51/48 name those arcade-table slots; same decoded remap as the base
# Zakato). Brag Zakato bodies use arcade code 0x12, the Garu 0x13 (cosmetic — the renderer derives its
# frame from state/clock, not the code byte, like every ported family).
BRAG_ZAKATO_RND_PTS = 13  # 600 points (handle_16 _PTS byte 36)
BRAG_ZAKATO_CLOSEY_PTS = 18  # 1,500 points (handle_17 _PTS byte 51)
GARU_ZAKATO_PTS = 17  # 1,000 points (handle_18 _PTS byte 48)
BRAG_ZAKATO_MAIN_CODE = 0x12  # active-phase body sprite code (brag_zakato_NN_main move.b #0x12,_CODE)
GARU_ZAKATO_MAIN_CODE = 0x13  # active-phase body sprite code (handle_18 move.b #0x13,_CODE)
# Brag Zakato rnd draws a 1-64 fuse on teleport completion ((rng & 0x3f)+1, 3874-3875) — the same span
# as the base fast variant; the closeY variant has NO fuse (it fires on the lateral-proximity test, the
# same [-4, 3] cell band as the base closeY, reusing ZAKATO_CLOSEY_LOW/HIGH). The Garu draws a 32-63
# fuse at spawn ((rng & 0x1f)+32, 4018-4021) — span 32, offset 32.
BRAG_ZAKATO_RND_FUSE_SPAN = 64  # (rng mod 64) + 1 = 1-64 arcade frames (handle_16 and.w #0x3f)
GARU_ZAKATO_FUSE_SPAN = 32  # (rng mod 32) + offset = the 32-63 fuse (handle_18 and.b #0x1f)
GARU_ZAKATO_FUSE_OFFSET = 32  # +32 -> 32-63 (handle_18 add.b #32)
GARU_STRAIGHT_DX = 48  # raw scroll-axis velocity for the Garu (arcade dX=48, 3 px/frame)
# The terminal fan (brag_zakato_shoot 5054) aims at the craft, converts the aim angle to a radiating
# index ((base - 32) >> 3 & 0x1f — the arcade sub #32 / ror.b #3 / and #0x1f) and emits 5 bullets two
# steps apart (addq #2 / and #0x1f). The Garu ring (init_garu_zakato_explosion 5075) emits 16 bullets at
# even angles 0,2,..,30 (a full 360-degree ring), then spawns 4 Brag Sparios into the slots ADJACENT to
# the Garu with cardinal velocities from brag_spario_dX/dY_tbl.
BRAG_ZAKATO_FAN_COUNT = 5  # bullets in the terminal aimed fan (moveq #5-1)
BRAG_ZAKATO_FAN_STEP = 2  # angle step between fan bullets (addq #2)
BRAG_ZAKATO_FAN_BASE_BIAS = 32  # the arcade sub #32 before the >>3 index fold
GARU_RING_COUNT = 16  # bullets in the 360-degree detonation ring (moveq #16-1)
GARU_RING_STEP = 2  # angle step -> even angles 0,2,..,30 (addq #2, and #30)
GARU_SPARIO_COUNT = 4  # Brag Sparios spawned by the detonation (moveq #4-1)
# The 4 detonation Sparios take cardinal velocities from brag_spario_dX_tbl/brag_spario_dY_tbl (5106/5112,
# 0xE0 == signed -32): (dX,dY) = (+32,0),(0,-32),(-32,0),(0,+32) — down/left/up/right on the port axes
# (arcade _dX == scroll axis == slot dx; _dY == lateral == slot dy). Magnitude 32 raw = 2 px/frame.
BRAG_SPARIO_SPAWN_DX = (32, 0, -32, 0)
BRAG_SPARIO_SPAWN_DY = (0, -32, 0, 32)
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
    GIDDO_SPARIO_TYPE,
    BRAG_SPARIO_TYPE,
    ZAKATO_SLOW_TYPE,
    ZAKATO_CLOSEY_TYPE,
    ZAKATO_FAST_TYPE,
    ZAKATO_CONT_TYPE,
    BRAG_ZAKATO_RND_TYPE,
    BRAG_ZAKATO_CLOSEY_TYPE,
    GARU_ZAKATO_TYPE,
    RIGHT_SHEONITE_TYPE,
    LEFT_SHEONITE_TYPE,
)
# AIR-11 Bacura (indestructible slab). Arcade code 0x01 (main_fn_3__init_bacura 5188 writes _TYPE=1 into
# the 0x10-0x1F object band). BACURA_TYPE keeps that arcade value, but the port must NOT dispatch it by
# `slot type == 1`: the shot slots (SHOT_SLOTS 37-39) also carry slot type 1 (SHOT_TYPE), so a type-1
# equality branch in the walk would run the Bacura handler over live shots. Instead the walk dispatches
# the Bacura by BAND MEMBERSHIP (slot index in BACURA_SLOTS 17-32, which only ever holds Bacura), and the
# renderer — whose clones are bound to band slots — can safely read `slot type == BACURA_TYPE` because a
# band slot only ever holds 0 or 1. The shot-invulnerability is not a state flag: the Bacura walk branch
# simply OMITS the CHECK_AIR_HIT_PROCCODE call every flying family makes, so no shot ever hit-tests it.
BACURA_TYPE = 1  # 0x01, handle_01_Bacura: drifts down its own band, never destroyed/scored
BACURA_DRIFT_DX = 16  # raw scroll-axis velocity (arcade _dX=16 => 4*16 units/tick = 1 px/frame down)
# WPN-01 (player.bacura-bounce #77) shot rebound. When a player shot is marked SHOT_BOUNCE by
# `check shot bacura`, its blaster clone reverses and animates in place before deleting, instead of
# vanishing at once. The arcade's `shot_destroyed` (2400-2417) sets the reflected shot _dX=+24 = 1/4 of
# the normal 6 px/frame, reversed — so from the port's forward `changeyby 20` the reversed step is
# 20 * (1/4) reversed = -5 stage-px/frame (NOT a naive halve, NOT a literal 1.5). The animation runs the
# reference's 8 frames (_TIMER 0..7, deleted at 8; sprite code 0x18+((TIMER>>1)&3), four costume codes).
BACURA_BOUNCE_DY = -5  # reversed shot step during the bounce (arcade reflected _dX=+24 = 1/4, reversed)
BACURA_BOUNCE_FRAMES = 8  # bounce animation length (arcade shot_destroyed deletes at _TIMER==8)
# AIR-11 live spawn pipeline (main_fn_3__init_bacura 5188-5199, main_fn_5__inc_num_bacura 5201-5217).
# The schedule sets `bacura inc cnt` (a per-window quota); the pump admits one slab per arcade second
# into the reserved band, refilling any band slot whose slab has drifted off and culled. All three are
# Stage-written spawn state (default 0), re-topped per area alongside the schedule cursor.
NUM_BACURA_ID = "num-bacura"  # slabs the init pump keeps alive in the first N band slots
BACURA_INC_CNT_ID = "bacura-inc-cnt"  # remaining one-per-second increments (set by set_bacura_count)
ONE_SECOND_CNTR_ID = "one-second-cntr"  # frames until the next increment (main_fn_5 one_second_cntr=60)
BACURA_SEED_SLOT_ID = "bacura-seed-slot"  # init-pump loop cursor (0-based offset into the band)
BACURA_BAND_SIZE = BACURA_SLOTS[1] - BACURA_SLOTS[0] + 1  # 16 reserved slots (0x10-0x1F)
BACURA_INC_PERIOD_FRAMES = 60  # main_fn_5 sets one_second_cntr=60; counted down TICK_TIMER_STEP/tick
# AIR-09 Sheonite escort-pair port model. The pair lives in the shared flying pool (two fixed adjacent
# slots) and runs an explicit phase machine carried in `slot flag` (slot state stays SLOT_ACTIVE; the
# renderer reads slot flag + slot type + slot timer). Phases: HOME (home onto the craft at the shared
# 64-magnitude aim tier, 4 px/frame, re-aimed each tick) -> LOCK (track a fixed offset beside the live
# craft, recomputed each tick) -> COMBINE (dock: a 32-frame dwell) -> RETREAT (right only: drift away along
# the scroll axis at 6 px/frame; the left side vanishes at combine end). Never hit-tested (see the type
# note above): the walk branch omits CHECK_AIR_HIT and no craft-overlap detector targets these slots.
SHEONITE_TARGET = "sheonite"
SHEONITE_CLONE_SLOT_ID = "sheonite-clone-slot"  # sprite-local: which flying slot this clone renders
UPDATE_SHEONITE_PROCCODE = "update sheonite"
SHEONITE_RIGHT_SLOT = FLYING_SLOTS[1]  # 0x3f, the arcade's right-Sheonite object slot
SHEONITE_LEFT_SLOT = FLYING_SLOTS[1] - 1  # 0x3e, the arcade's left-Sheonite object slot
# Phase constants (carried in `slot flag`).
SHEONITE_PHASE_HOME = 0
SHEONITE_PHASE_LOCK = 1
SHEONITE_PHASE_COMBINE = 2
SHEONITE_PHASE_RETREAT = 3
# Lock offset from the live craft cell: 2 cells ahead on the scroll axis (arcade _X-0x200) and 2 cells to
# either lateral side (right _Y-0x200, left _Y+0x200; 0x200 = 2 cells of 256 units).
SHEONITE_LOCK_LEAD = 2  # cells ahead of the craft on the scroll axis (toward the top of the field)
SHEONITE_LOCK_FLANK = 2  # cells to the side (right locks -flank, left +flank)
# Retreat is the arcade _dX=0xFFA0 = -96 raw (=> 4*-96 units/tick / 32 units-px / 2 frames = -6 px/frame,
# away from the craft along the scroll axis); homing reuses the live 64-magnitude aim tier (4 px/frame).
SHEONITE_RETREAT_DX = -96
SHEONITE_COMBINE_DWELL_FRAMES = 32  # dock dwell (arcade r/l_sheonite_combining: 0xe0->wrap up / 0x20->0 down = 32)
# Render-only animation (like the Jara). Costume ordinals 1..10 == arcade sprite codes 0x30..0x39:
# spin/01..04 (0x30-0x33) in HOME/LOCK/RETREAT, then combine/01..03 (right, 0x34-0x36) and combine/04..06
# (left, 0x37-0x39) in COMBINE. The exact per-side arcade code-table permutation is a cosmetic simplified
# to a plain cycle (recorded as a port note); the behaviour it drives is faithful.
SHEONITE_ANIM_PERIOD = 2  # arcade-frames per animation step (slot timer advances TICK_TIMER_STEP=2/tick)
SHEONITE_SPIN_FRAMES = 4  # spin costume count (ordinals 1..4)
SHEONITE_COMBINE_ANIM_FRAMES = 3  # combine costume count per side (right 5..7, left 8..10)
SHEONITE_SPIN_BASE_ORDINAL = 1  # spin costumes start at ordinal 1
SHEONITE_RIGHT_COMBINE_BASE_ORDINAL = SHEONITE_SPIN_FRAMES + 1  # 5
SHEONITE_LEFT_COMBINE_BASE_ORDINAL = SHEONITE_SPIN_FRAMES + SHEONITE_COMBINE_ANIM_FRAMES + 1  # 8
SHEONITE_RENDER_SIZE = 225  # match the shared on-screen scale (a 16-px sprite at ~2.25 stage px/px)
# Stage-written escort state. `sheonite end flag` is the schedule on/off flag (sheonite_start clears it,
# sheonite_end raises it); the pair only leaves LOCK once it is set. The two temps are per-tick machinery:
# a phase snapshot (so a mid-tick transition does not cascade into a later branch this tick) and the
# resolved lateral lock cell (right vs left).
SHEONITE_END_FLAG_ID = "sheonite-end-flag"
SHEONITE_PHASE_TMP_ID = "sheonite-phase"
SHEONITE_LOCK_COL_ID = "sheonite-lock-col"
# AIR-09 schedule handlers (area-schedules.json opcodes 0x33/0x34, sub_2_fn_18/19 sheonite_start/end).
# start stamps the pair and clears the end-flag; end raises it. On/off flags, not a per-second pump.
SHEONITE_START_HANDLER = "sheonite_start"
SHEONITE_END_HANDLER = "sheonite_end"
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
    (GIDDO_SPARIO_TYPE, GIDDO_SPARIO_FORMATION_OFFSET, 1),  # solo Giddo Spario (fast aim-once flyby)
    # The four base Zakato variants, one at a time. fast/cont also appear in natural area waves, but the
    # T key gives the operator a solo of each variant on demand — and it is the ONLY way to see slow/closeY,
    # which no built area schedules (see the ZAKATO_*_FORMATION_OFFSET note above).
    (ZAKATO_SLOW_TYPE, ZAKATO_SLOW_FORMATION_OFFSET, 1),
    (ZAKATO_CLOSEY_TYPE, ZAKATO_CLOSEY_FORMATION_OFFSET, 1),
    (ZAKATO_FAST_TYPE, ZAKATO_FAST_FORMATION_OFFSET, 1),
    (ZAKATO_CONT_TYPE, ZAKATO_CONT_FORMATION_OFFSET, 1),
    # AIR-08: the two Brag Zakato variants spawn through the normal formation path (their type-table run
    # first code selects them), one at a time.
    (BRAG_ZAKATO_RND_TYPE, BRAG_ZAKATO_RND_FORMATION_OFFSET, 1),
    (BRAG_ZAKATO_CLOSEY_TYPE, BRAG_ZAKATO_CLOSEY_FORMATION_OFFSET, 1),
    # AIR-08: the Garu Zakato is NOT in the flying type table (its only arcade spawn is the area
    # `add_object` schedule, not yet consumed by the port — a documented follow-up). So it cannot come in
    # through the formation spawner: its count is 0 (the spawner brings in nothing) and a dedicated
    # direct-stamp branch in this proc stamps it into the first flying slot instead. Offset is immaterial
    # at count 0.
    (GARU_ZAKATO_TYPE, 0, 0),
    # AIR-11: the Bacura is not a flying-pool type at all — it lives in its own reserved band (17-32) and
    # is spawned live by the area schedule. Like the Garu it has no formation-table run, so its count is 0
    # (the formation spawner brings in nothing) and a dedicated direct-stamp branch stamps one slab into
    # BACURA_SLOTS[0] instead. Offset is immaterial at count 0.
    (BACURA_TYPE, 0, 0),
    # AIR-09: the Sheonite is a schedule-spawned PAIR (sheonite_start/end), not a formation type, so its
    # count is 0 and a dedicated direct-stamp branch stamps BOTH slots (right + left). The debug stamp also
    # pre-arms the end-flag so the pair completes its lifecycle and self-culls (so holding T does not stall
    # the cursor on an escort that would otherwise lock beside the craft forever). Keyed on the right type.
    (RIGHT_SHEONITE_TYPE, 0, 0),
)
TOROID_PTS = 3  # 1-based value-table position of 30 points (init_toroid PTS byte 6)
TOROID_INIT_CODE = 8  # face-on sprite code at spawn (codes 8..15 cycle during the swing)

# GND ground-object types — the arcade object codes (obj_handler_tbl 6196), dispatched by direct
# equality like the flying families. This PR builds Barra (#70) and Logram (#71); the other ground
# codes present in the schedules (Zolbak 0x1F, Derota 0x2C/0x2D, ...) stay on the empty seam for their
# own slices, so an add_ground_object record for an unbuilt type advances the cursor without spawning.
BARRA_TYPE = 30  # 0x1E, handle_1E_Barra: passive terrain target, never fires, crater on death
ZOLBAK_TYPE = 31  # 0x1F, handle_1F_Zolbak: passive dome; on death reduces the enemy AI level by 2 (GND-02)
GARU_BARRA_TYPE = 32  # 0x20, handle_20_Garu_Barra: indestructible base + destructible node (Commit 6)
LOGRAM_TYPE = 38  # 0x26, handle_26_Logram: open/close dome, one aimed shot at full-open (Commit 7)
DEROTA_TYPE = 27  # 0x1B, handle_1B_Derota: periodic aimed turret, craters on death, 1000 pts (GND-04)
GARU_DEROTA_TYPE = 33  # 0x21, handle_21_Garu_Derota: indestructible base + firing destructible node (GND-04)
BOZA_LOGRAM_TYPE = 45  # 0x2D, handle_2D_Boza_Logram: 5-slot composite (4 outer Lograms + 1 centre), GND-05
DOMOGRAM_TYPE = 46  # 0x2E, handle_2E_Domogram: path-driven mover that fires one aimed shot per animation (GND-07)
# GND-06 (ground.grobda #88): the tank/stingray family — the first SELF-MOVING ground object. 12 live
# variants (handle_2C_Grobda_stationary + handle_35..40, skipping the unused 0x37 null slot). All share ONE
# update proc and ONE tank costume set, differing only in reticle trigger, reaction, points, and land-crater
# vs water-vanish death — decided inside `update grobda` on slot type. NONE fires. Every variant moves under
# its own velocity through `advance ground moving` (dY cleared; scroll-axis only).
#
# The per-variant reaction is a single parameterized state machine keyed on `slot flag` (0 pre-trigger, 1
# reacting, 2 latched). Fields (all source-exact, xevious_main.68k 4289-4611):
#   type          the arcade object code
#   pts           1-based VALUE_TABLE_POINTS position (200->8, 400->11, 600->13, 1000->17, 1500->18,
#                 2000->19, 2500->20, 10000->22)
#   dx0           initial stored velocity (raw): 8 = stop (scroll-matched, appears stationary), 14 = forward
#   trigger       the reticle object slot to test, or None (never reacts): CROSSHAIR_SLOT (in-crosshairs) or
#                 BOMB_TARGET_SLOT (targeted by a dropped bomb). Read UNCONDITIONALLY, matching the source
#                 (check_grobda_in_crosshairs / check_targeted_and_init_timer read the object table with no
#                 in-flight gate), so a targeted variant reacts to the frozen last-target cell between bombs.
#   react_dx      velocity while reacting (14 fwd / 2 back / 22 dart), or None for a never-reacting variant
#   end_dx        velocity after the 48-frame reaction timer expires, or None = hold react_dx permanently
#                 (the crosshair->forward-forever variants, which have no timer)
#   rearm         True: after the timer, return to `slot flag` 0 (repeatable — 0x3C darts each time it is
#                 re-targeted); False: latch at `slot flag` 2
#   water         True: on a bomb hit it VANISHES (explode_and_remove_object, like a Garu node); False: it
#                 craters PERSISTENTLY (handle_bomb_explosion, like the Barra/Zolbak)
GrobdaVariant = namedtuple(
    "GrobdaVariant", "type pts dx0 trigger react_dx end_dx rearm water"
)
GROBDA_VARIANTS = (
    GrobdaVariant(0x2C, 8, 8, None, None, None, False, False),  # stationary, 200
    GrobdaVariant(0x35, 11, 14, None, None, None, False, False),  # forward, 400
    GrobdaVariant(0x36, 13, 8, CROSSHAIR_SLOT, 14, None, False, False),  # crosshairs -> fwd forever, 600
    GrobdaVariant(0x38, 17, 14, CROSSHAIR_SLOT, 8, 14, False, False),  # fwd, crosshairs -> stop 48f -> fwd, 1000
    GrobdaVariant(0x39, 18, 8, BOMB_TARGET_SLOT, 2, 8, False, False),  # targeted -> back 48f -> stop, 1500
    GrobdaVariant(0x3A, 19, 14, CROSSHAIR_SLOT, 22, 14, False, False),  # fwd, crosshairs -> dart 48f -> fwd, 2000
    GrobdaVariant(0x3B, 20, 14, BOMB_TARGET_SLOT, 2, 14, False, False),  # fwd, targeted -> back 48f -> fwd, 2500
    GrobdaVariant(0x3C, 22, 8, BOMB_TARGET_SLOT, 22, 8, True, False),  # targeted -> dart 48f -> stop -> rearm, 10000
    GrobdaVariant(0x3D, 8, 8, None, None, None, False, True),  # stationary, water, 200
    GrobdaVariant(0x3E, 11, 14, None, None, None, False, True),  # forward, water, 400
    GrobdaVariant(0x3F, 13, 8, CROSSHAIR_SLOT, 14, None, False, True),  # crosshairs -> fwd forever, water, 600
    GrobdaVariant(0x40, 20, 14, BOMB_TARGET_SLOT, 2, 14, False, True),  # fwd, targeted -> back 48f -> fwd, water, 2500
)
GROBDA_TYPES = tuple(v.type for v in GROBDA_VARIANTS)
GROBDA_WATER_TYPES = tuple(v.type for v in GROBDA_VARIANTS if v.water)
# The reticle alignment band (check_grobda_in_crosshairs 4589 / check_targeted_and_init_timer 4574): the
# per-axis cell offset `target_cell - grobda_cell` lies in [-2,+1] on BOTH the depth (row) and lateral (col)
# axes (the reference's `subq #2; addq #4; jcc` carry test on the position MSBs). True exactly inside the band.
GROBDA_RETICLE_LOW = -2
GROBDA_RETICLE_HIGH = 1
# The reaction lasts 48 arcade-frames (check_grobda_in_crosshairs / check_targeted set _TIMER=48), counted
# down by the frame-step convention (TICK_TIMER_STEP per tick), like every other ground/air timer.
GROBDA_REACTION_FRAMES = 48
GROBDA_FLAG_PRETRIGGER = 0  # slot flag: waiting for the reticle
GROBDA_FLAG_REACTING = 1  # slot flag: counting the 48-frame reaction down
GROBDA_FLAG_LATCHED = 2  # slot flag: reaction done, holding end_dx (non-repeatable variants)
# Every ground type this project SPAWNS from an add_ground_object schedule record. Barra/Garu Barra/Logram
# shipped in slice 9; slice 12 adds Zolbak, Derota, and Garu Derota; slice 13 adds the Boza Logram and Grobda.
GROUND_HANDLED_TYPES = (
    BARRA_TYPE,
    ZOLBAK_TYPE,
    GARU_BARRA_TYPE,
    LOGRAM_TYPE,
    DEROTA_TYPE,
    GARU_DEROTA_TYPE,
    BOZA_LOGRAM_TYPE,
    *GROBDA_TYPES,
    DOMOGRAM_TYPE,
)
# GND-07 (ground.domogram #89): the path-driven "Defence Site/Slider" — the first ground family that BOTH
# moves under its own velocity AND fires. All source-exact from handle_2E_Domogram (xevious_main.68k 4620-4692):
#   * 800 pts (_PTS=42 -> VALUE_TABLE_POINTS position 15); land death craters PERSISTENTLY (handle_bomb_explosion,
#     the Barra model — a Domogram is a land slider, NOT a water/vanish object).
#   * Follows a SCRIPTED path: a list of (duration, vector_index) steps. Each step holds one of the 32
#     domogram_vector_tbl (dY,dX) deltas for `duration` arcade-frames; when the path ends it HOLDS the last
#     vector forever (domogram_done_all_vectors moves the coroutine re-entry past the vector-advance). The port
#     flattens every instance's path into two shared step columns and follows it with a per-slot pointer (_EXTRA),
#     a remaining-vector count (_NVEC) and the current step's remaining duration (_VECLEN).
#   * Fires ON its family fire mask (ffreq_mask_domogram): a masked-random shot timer (_TIMER=(rand & mask)+1)
#     counts DOWN on the every-8th-frame phase while the object is high enough on the field
#     (cur_row <= gnd_stop_firing_row); at 0 it starts a 24-arcade-frame animation (_TYPE), fires ONE aimed
#     bullet at the animation MIDPOINT (frame 12) and re-rolls the shot timer, then holds fire until the anim ends.
# The 24-frame animation cycles domogram_sprite_tbl {0x3C,0x3D,0x3E,0x3F,0x3E,0x3D}; the sprite is a render-only
# function of the anim timer (the walk maintains _TYPE, the renderer maps it to a costume), like the Grobda roll.
DOMOGRAM_PTS = 15  # 1-based value-table position of 800 points (handle_2E_Domogram _PTS=42 -> object_value_tbl)
DOMOGRAM_ANIM_FRAMES = 24  # _TYPE animation timer init (move.b #24,(_TYPE)) — a 24-arcade-frame animation
DOMOGRAM_FIRE_FRAME = 12  # fire ONE aimed bullet when the anim timer decrements to 12 (arcade `cmp #12,d0`)
DOMOGRAM_VECLEN_INIT = 1  # _VECLEN init (move.b #1,(_VECLEN)) — 1 so the first main tick loads the first vector
# domogram_sprite_tbl (xevious_main.68k 4691-4692): the 6-step sprite cycle over 4 distinct codes 0x3C..0x3F.
DOMOGRAM_SPRITE_CODES = (0x3C, 0x3D, 0x3E, 0x3F, 0x3E, 0x3D)
# The costume ordinal (1-based) each animation-index step selects: code 0x3C->1, 0x3D->2, 0x3E->3, 0x3F->4. The
# arcade reads `(_TYPE >> 2) & 7` (0..7) into the 6-entry sprite table; indices 6/7 are unreachable in play
# (the anim timer only reaches (_TYPE>>2)<=5 once the same-tick fall-through decrements the freshly-set 24), so
# they pad to the idle frame (ordinal 1). Derived from the source sprite table so the two never drift.
DOMOGRAM_FRAME_ORDINALS = [c - 0x3C + 1 for c in DOMOGRAM_SPRITE_CODES] + [1, 1]
# For the debug ground key: the vector a debug-spawned Domogram holds (no scripted path in the debug tool). Index
# 8 = (dX 8 scroll-matched depth, dY 8 lateral) — it traverses the field at the terrain rate while drifting
# laterally, so the operator has a long, bombable pass to watch it fire. See _debug_ground_seed.
DOMOGRAM_DEBUG_VECTOR_INDEX = 8
# DEBUG (tracked for removal #119): the families the ground debug key (G) cycles through, one at a time, in
# roadmap order. Each entry is (object type, seed shape); the shape picks the shared seed builder
# (_ground_seed_single / _garu / _garu_derota / _boza) so the debug spawn is the scheduled spawn's exact shape.
# Extended as later ground families are built (Grobda, Domogram in slice 13's second build PR) — no new key.
DEBUG_GROUND_FAMILIES = (
    (BARRA_TYPE, "single"),
    (ZOLBAK_TYPE, "single"),
    (GARU_BARRA_TYPE, "garu"),
    (LOGRAM_TYPE, "single"),
    (DEROTA_TYPE, "single"),
    (GARU_DEROTA_TYPE, "garu_derota"),
    (BOZA_LOGRAM_TYPE, "boza"),
    # GND-06 (ground.grobda #88): a representative spread the operator can cycle to verify the tank family —
    # a stationary land tank, a crosshair-reactive mover, a bomb-targeted darter (the 10,000 tier), and a
    # water variant that vanishes on a hit. Every Grobda uses the single-slot seed shape.
    (0x2C, "single"),  # stationary (land)
    (0x36, "single"),  # moves forward once in the crosshairs (land)
    (0x39, "single"),  # darts back when targeted, then stops (land)
    (0x3C, "single"),  # darts forward when targeted, re-arms — 10,000 pts (land)
    (0x40, "single"),  # forward, darts back when targeted (water, vanishes on a hit)
    # GND-07 (ground.domogram #89): a single Domogram the operator can watch cross the field and fire one aimed
    # shot per animation, then bomb for the land crater. The debug seed uses an empty path + a representative
    # diagonal vector (the scheduled path decode is exercised by the round-trip golden and the harness).
    (DOMOGRAM_TYPE, "domogram"),
)
BARRA_PTS = 6  # 1-based value-table position of 100 points (handle_1E_Barra _PTS=15 -> object_value_tbl)
ZOLBAK_PTS = 8  # 1-based value-table position of 200 points (handle_1F_Zolbak _PTS=21)
LOGRAM_PTS = 10  # 1-based value-table position of 300 points (handle_logram_init _PTS=27)
GARU_BARRA_PTS = 10  # 1-based value-table position of 300 points (handle_20_Garu_Barra node _PTS=27)
DEROTA_PTS = 17  # 1-based value-table position of 1,000 points (handle_1B_Derota init _PTS=48)
GARU_DEROTA_PTS = 19  # 1-based value-table position of 2,000 points (handle_21_Garu_Derota node _PTS=54)
# GND-05 (ground.boza-logram #87) point values (1-based value-table positions; VALUE_TABLE_POINTS). The five
# slots score type-agnostically through the shared ground hit sweep off each slot's own `slot pts`:
#   * each OUTER dome: 300 pts (handle_boza_logram_outer _PTS=27 -> position 10, the same as a lone Logram);
#   * the CENTRE: 2,000 pts (handle_boza_logram_centre _PTS=54 -> position 19) — but the instant ANY outer is
#     hit, that outer downgrades the centre to 600 pts (update_centre_points_value _PTS=36 -> position 13).
# The scoring asymmetry ("2,000 if the centre is bombed first, else 600; 300 per directly-bombed outer") falls
# out of the shared sweep for free: the centre-first cascade sets the outers HIT directly (bypassing the award
# path), so only a DIRECTLY bombed slot ever scores. See install_update_boza.
BOZA_OUTER_PTS = 10  # 1-based value-table position of 300 points (outer _PTS=27)
BOZA_CENTRE_PTS = 19  # 1-based value-table position of 2,000 points (centre _PTS=54, before any outer hit)
BOZA_CENTRE_DOWNGRADED_PTS = 13  # 1-based value-table position of 600 points (centre _PTS=36, after an outer hit)
# The 5-slot composite geometry (handle_2D_Boza_Logram $1CDE). Slots 0..3 are the four outer domes; slot 4 is
# the centre. `slot x` (scroll/depth axis) offsets copy boza_logram_spriteX_tbl {0, 0x180, 0x180, 0x300,
# 0x180}; `slot y` (lateral axis) offsets copy the _Y adjust {+0, +0x180, -0x180, +0, +0}. 0x180 = 384 units =
# 12 px and 0x300 = 768 units = 24 px at SLOT_UNITS_PER_PIXEL (arcade and port share the 32-units/px scale, as
# the Garu's 0x100 = 8-px cell offset already established). Expressed in px here, scaled at spawn.
# PORT NECESSITY (composite render fidelity, docs/mechanics/042): the ground cell->stage map is ANAMORPHIC
# (RENDER_COL_STAGE 15 px/cell laterally vs RENDER_ROW_STAGE 8 px/cell in depth), but a dome sprite is
# isotropic. Feeding the raw depth offsets through the depth scale renders the five equal-size domes ~1.9x
# closer vertically than laterally, collapsing top/middle/bottom into one blob. So the DEPTH offsets are
# scaled by RENDER_COL_STAGE/RENDER_ROW_STAGE at spawn (see _ground_seed_boza) — vertical dome spacing then
# renders at the same px/arcade-px as lateral, so the composite reads as the arcade's isotropic diamond.
# `slot x` == the rendered position == the bomb-hit position, so aim and the index-addressed cascade are
# unchanged; only the composite's internal depth spacing widens (a first-ever multi-slot-composite concern).
BOZA_SLOT_COUNT = 5
BOZA_CENTRE_OFFSET = 4  # the centre is the 5th slot (base + 4); outers are base + 0..3
BOZA_DEPTH_OFFSETS_PX = (0, 12, 12, 24, 12)  # slot x offsets (boza_logram_spriteX_tbl / SLOT_UNITS_PER_PIXEL)
BOZA_LATERAL_OFFSETS_PX = (0, 12, -12, 0, 0)  # slot y offsets (the _Y adjust / SLOT_UNITS_PER_PIXEL)
# GND-02 (ground.zolbak #85): a bombed Zolbak reduces the adaptive enemy AI level by 2, floored at 0
# (handle_1F_Zolbak -> reduce_enemy_ai_by_2 $1B1F: `subq #2,d0; jcc; moveq #0`). This eases subsequent
# formation pressure — the whole point of the family. It is the ONE post-hit global side-effect any ground
# family has; `update zolbak` runs it exactly once per kill (guarded on the first HIT tick, `slot timer`
# still 0 from the detector) so a persistent crater does not re-trigger it every tick.
AI_LEVEL_ZOLBAK_DROP = 2

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

# AIR-07 Zakato renderer constants. One persistent clone per flying slot, keyed on the slot's phase in
# `slot state`, which the update machine sequences (SLOT_TELEPORT -> SLOT_ACTIVE -> SLOT_SELF_EXPLODE, or
# SLOT_ACTIVE -> SLOT_HIT). Costume ordinals: 1 = the single active body (arcade code 0x11); 2.. = the
# shared solv_death burst (the recorded cosmetic stand-in every flying family appends, records 025/031).
# The Zakato's own teleport/self-destruct sprites (arcade codes 4,5,6,7,8,0xC — zakato_teleport_sprite_tbl
# / zakato_exploding_sprite_tbl) are a DEFERRED cosmetic like the other families' bursts: all three of the
# Zakato's animated phases render from the shared burst — the teleport-in sparkle plays it REVERSED (the
# arcade's sparkle is that same six-frame set run backwards, 3986-3992), the self-destruct and the shot
# kill play it FORWARD. The self-destruct stays at normal scale (a small pop) while the shot kill grows at
# the big phase like every other flying kill, keeping the two visually distinct.
ZAKATO_TARGET = "zakato"
ZAKATO_CLONE_SLOT_ID = "zakato-clone-slot"  # sprite-local: which flying slot this clone renders
ZAKATO_RENDER_SIZE = 225  # 1x1 (16-px) sprite (_ATTR #0x80), the shared flying scale
ZAKATO_BODY_ORDINAL = 1  # costume 1: the active body (arcade code 0x11)
ZAKATO_BURST_ORDINAL_BASE = ZAKATO_BODY_ORDINAL + 1  # 2: first shared solv_death burst frame

# AIR-10 Spario renderer constants (shared shape for Giddo and Brag). One persistent clone per flying slot,
# drawn when its slot holds the family's type, hidden otherwise; the clone writes no state. IMPORTANT: the
# CrazyCarl aerial-enemies rip carries NO Spario sprites (it labels Toroid/Torkan/Zoshi/Jara/Kapi/Terrazi/
# Zakato/Brag-Zakato/Sheonite/Bacura/Shooting-Star only), so a distinct Spario costume cannot be sourced or
# operator-pixel-verified. Both families therefore stand in the Zakato body frame (a small dark blob — and
# the Sparios are the payload a Zakato releases, so the stand-in reads sensibly) as a DEFERRED cosmetic with
# its reason recorded, exactly as every family defers its own burst to the shared solv_death frames. The
# Giddo's 4-frame flight loop (arcade CODE 0..3, 5229-5233) and short 4-code burst (codes 4..7), and the
# Brag's ATTR flip mirror (3116-3119), are all deferred with it; the mechanically-meaningful distinctions
# (aim-once flyby vs accelerating homer, and the Giddo's SHORT 8-frame burst duration) live in the handlers.
# Costume layout on each target: ordinal 1 = the Zakato body stand-in, ordinals 2.. = the shared burst.
SPARIO_BODY_ORDINAL = 1  # costume 1: the Zakato body stand-in
SPARIO_BURST_ORDINAL_BASE = SPARIO_BODY_ORDINAL + 1  # 2: first shared solv_death burst frame
SPARIO_RENDER_SIZE = 225  # 1x1 (16-px) sprite, the shared flying scale

GIDDO_SPARIO_TARGET = "giddo-spario"
GIDDO_SPARIO_CLONE_SLOT_ID = "giddo-spario-clone-slot"  # sprite-local: which flying slot this clone renders

BRAG_SPARIO_TARGET = "brag-spario"
BRAG_SPARIO_CLONE_SLOT_ID = "brag-spario-clone-slot"  # sprite-local: which flying slot this clone renders

# AIR-08: the Garu Zakato renderer reuses the shared Spario factory (ACTIVE body stand-in + HIT burst,
# no teleport phase) — it has its OWN clone pool over the flying slots. The Brag Zakato needs NO new
# target: it teleports and self-destructs exactly like the base Zakato, so it folds into the Zakato
# renderer (its `is_zakato` gate is extended to the two Brag types).
GARU_ZAKATO_TARGET = "garu-zakato"
GARU_ZAKATO_CLONE_SLOT_ID = "garu-zakato-clone-slot"  # sprite-local: which flying slot this clone renders
# AIR-08 Garu detonation temporaries (Stage-scoped): the detonation copies the Garu's cell into the 16
# ring bullets and the 4 spawned Sparios, then frees the Garu slot. `slot index` is repointed at each
# spawned Sparios during the spawn loop, so the Garu's own cell and slot number are captured up front.
GARU_DET_X_ID = "garu-det-x"  # the detonating Garu's scroll-axis position, copied into its spawns
GARU_DET_Y_ID = "garu-det-y"  # the detonating Garu's lateral position, copied into its spawns
GARU_DET_SLOT_ID = "garu-det-slot"  # the detonating Garu's own flying slot (to compute adjacency + free it)

# AIR-11 (air.bacura #81) renderer constants. Unlike the flying families, the Bacura draws one persistent
# clone per BACURA-BAND slot (17-32), each a pure per-tick function of its slot: a SINGLE static costume
# (a tumble frame) while the slot holds BACURA_TYPE, hidden otherwise. There is NO hit/explosion phase at
# all — the Bacura is never destroyed. But the slab is NOT static: `handle_01_Bacura` (xevious_main.68k
# 4253-4262) resumes every frame via save_PC_to_fn_tbl_and_ret and reselects the sprite CODE from the live
# _X — `(_X>>6)&0x0e` indexes bacura_sprite_tbl (8 colour/code pairs, 4268-4276), so the slab visibly
# TUMBLES through 8 frames (edge-on -> broadside -> edge-on) as it drifts. In the port, `slot x` carries the
# same 32-units-per-pixel scale as arcade `_X`, so the frame index is (floor(slot x / 128)) mod 8 = (_X>>7)&7
# and the costume is bacura/slab/0{index+1}. The eight frames are mirrored on in expected_project (no death
# append, like the enemy_bullet). The clone writes no state.
BACURA_TARGET = "bacura"
BACURA_CLONE_SLOT_ID = "bacura-clone-slot"  # sprite-local: which Bacura-band slot this clone renders
BACURA_RENDER_SIZE = 225  # the shared on-screen scale (~2.25 stage px per native px)
BACURA_TUMBLE_FRAMES = 8  # bacura/slab/01..08 — the tumble cycle (bacura_sprite_tbl has 8 entries)
BACURA_TUMBLE_UNITS_PER_FRAME = 128  # slot-x units per frame flip: (_X>>7) => /128 (arcade lsr#6 + and#0x0e)

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

# GND-02 (ground.zolbak #85) renderer constants. A Zolbak renders EXACTLY like a Barra — an idle dome
# that craters on a bomb hit — so its costume layout mirrors the Barra target: 1 = the idle dome
# (zolbak/idle/01), 2.. = the shared solv_death explosion burst, then the two crater frames last. The
# AI-level side-effect lives in `update zolbak`, not here; the renderer is a pure function of slot state.
ZOLBAK_TARGET = "zolbak"
ZOLBAK_CLONE_SLOT_ID = "zolbak-clone-slot"  # sprite-local: which ground slot this clone renders
ZOLBAK_IDLE_ORDINAL = 1  # costume 1: the Zolbak idle dome (zolbak/idle/01)
ZOLBAK_EXPLODE_BASE_ORDINAL = 2  # costume 2..: the shared explosion burst (explode_01..)
ZOLBAK_CRATER_BASE_ORDINAL = ZOLBAK_EXPLODE_BASE_ORDINAL + EXPLODE_COSTUME_COUNT  # 10: crater frames follow

# GND (ground.barra #70) Garu Barra renderer constants. The Garu is TWO objects in adjacent ground slots
# sharing one type (GARU_BARRA_TYPE): a 2x2 indestructible base (state SLOT_GARU_BASE) and a 1x1
# destructible node (state ACTIVE/HIT). One `garu` target's clone pool covers the ground band; each clone
# branches on its slot's STATE — base vs node — because both carry the same slot type. Costume layout:
#   1..2  garu/base frames (01 = closed pyramid, 02 = the red-socket base). The EXPOSED base holds frame
#         02 (the lit red socket) statically. The arcade base colour-pulses pulsing_colour_1 — a red-light
#         glow — but Scratch cannot pulse the red lights alone (a hue shift greens them; a brightness pulse
#         flashes the whole base) and the crop-only sheet has no red-off cell, so the pulse is a recorded
#         port necessity (operator decision 2026-09-24). Frame 01 is retained as a crop but not rendered;
#   3     the node idle pyramid (garu/node reuses the Barra pyramid, mirrored from barra/idle);
#   4..11 the shared solv_death burst the node's explode-and-remove plays before it vanishes.
# The base is drawn TWICE the linear size of the node (arcade _ATTR=3, 2x2) — its costume is a 32-px
# canvas vs the node's 16-px, so the SAME GROUND_RENDER_SIZE yields ~2x on screen (no extra scaling).
GARU_TARGET = "garu"
GARU_CLONE_SLOT_ID = "garu-clone-slot"  # sprite-local: which ground slot this clone renders
GARU_BASE_EXPOSED_COSTUME = "garu/base/02"  # the exposed base holds the red-socket frame (02); no pulse (port necessity)
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

# GND-04 (ground.derota #86) Derota renderer constants. A Derota renders like a Barra — one idle turret
# frame that craters on a bomb hit (handle_1B_Derota craters via handle_bomb_explosion, NOT the Garu
# node's explode-and-remove) — so its costume layout mirrors the Barra target: 1 = the idle turret
# (derota/idle/01), 2.. = the shared solv_death burst, then the two crater frames. The firing is in
# `update derota`; the renderer is a pure function of slot state.
DEROTA_TARGET = "derota"
DEROTA_CLONE_SLOT_ID = "derota-clone-slot"  # sprite-local: which ground slot this clone renders
DEROTA_IDLE_ORDINAL = 1  # costume 1: the Derota idle turret (derota/idle/01)
DEROTA_EXPLODE_BASE_ORDINAL = 2  # costume 2..: the shared explosion burst (explode_01..)
DEROTA_CRATER_BASE_ORDINAL = DEROTA_EXPLODE_BASE_ORDINAL + EXPLODE_COSTUME_COUNT  # 10: crater frames follow

# GND-04 (ground.derota #86) Garu Derota renderer constants. Like the Garu Barra it is TWO slots sharing
# one type (GARU_DEROTA_TYPE): a 2x2 indestructible base (state SLOT_GARU_BASE) and a 1x1 FIRING
# destructible node (state ACTIVE/HIT). Costume layout mirrors the Garu Barra:
#   1..2  garu-derota/base frames (01 = closed centre, 02 = the open red firing centre). The EXPOSED base
#         holds frame 02 (the lit red centre) statically; the arcade's pulsing_colour_1 red-light glow is a
#         recorded port necessity (Scratch cannot pulse the red alone; the crop-only sheet has no red-off
#         cell; operator decision 2026-09-24). Frame 01 is retained as a crop but not rendered;
#   3     the node turret (garu-derota/node reuses the single Derota turret, derota/idle);
#   4..11 the shared solv_death burst the node's explode-and-remove plays before it vanishes.
# The base is a 32-px canvas vs the node's 16-px, so the SAME GROUND_RENDER_SIZE yields the arcade's 2x2
# base over the 1x1 node with no extra scaling. PORT NOTE (recorded in mechanics 041): the sheet has no
# separate small Garu-Derota node bitmap, so the node reuses the Derota turret — faithful, since the arcade
# node carries the same code 0x27 as the single Derota and fires the same masked aimed bullet.
GARU_DEROTA_TARGET = "garu derota"
GARU_DEROTA_CLONE_SLOT_ID = "garu-derota-clone-slot"  # sprite-local: which ground slot this clone renders
GARU_DEROTA_BASE_EXPOSED_COSTUME = "garu-derota/base/02"  # exposed base holds the red firing-centre frame (02); no pulse (port necessity)
GARU_DEROTA_NODE_IDLE_ORDINAL = 3  # costume 3: the node turret (garu-derota/node, mirrored from derota/idle)
GARU_DEROTA_EXPLODE_BASE_ORDINAL = 4  # costumes 4..: the shared burst the node plays before removal

# GND-05 (ground.boza-logram #87) renderer constants. One `boza` target's clone pool covers the ground band
# (1..16); each clone branches on its slot's `slot link` — an OUTER (link > 0) renders the open/close dome
# frame `update boza` wrote into `slot code` (ordinals 1..4, the SAME logram/open frames mirrored in by ref),
# and the CENTRE (link == 0) renders its one bullseye frame (ordinal 5). Both crater IDENTICALLY to the Barra
# once HIT (handle_bomb_explosion). Costume layout:
#   1..4   logram/open/01..04 (the outer dome open/close frames; an outer's `slot code` indexes them directly);
#   5      boza-centre/core/01 (the centre's red/black bullseye, arcade code 0x3a — the one new crop);
#   6..13  the shared solv_death explosion burst (the ground bomb-burst is the deferred cosmetic stand-in);
#   14..15 the two crater frames the HIT renderer flickers between once the burst finishes.
# The four outer domes reuse the Logram crops by ref; only the centre is a new sheet crop.
BOZA_TARGET = "boza"
BOZA_CLONE_SLOT_ID = "boza-clone-slot"  # sprite-local: which ground slot this clone renders
BOZA_OUTER_CLOSED_ORDINAL = 1  # costume 1: the closed outer dome (0x2C), the spawn + wait-phase frame
BOZA_CENTRE_ORDINAL = 5  # costume 5: the centre bullseye (boza-centre/core/01)
BOZA_EXPLODE_BASE_ORDINAL = BOZA_CENTRE_ORDINAL + 1  # 6: shared explosion burst follows the dome + centre
BOZA_CRATER_BASE_ORDINAL = BOZA_EXPLODE_BASE_ORDINAL + EXPLODE_COSTUME_COUNT  # 14: crater frames last

# GND-06 (ground.grobda #88) tank/stingray renderer. All 12 variants share ONE target and ONE costume set:
# the four tank/roll frames (ordinals 1..4; the arcade rolls _CODE through 0x4c..0x4f), then the shared
# solv_death burst (ordinals 5..12 — the ground bomb-burst is the deferred cosmetic stand-in every ground
# family uses), then the two crater frames (ordinals 13..14) a LAND variant flickers between once the burst
# finishes. A WATER variant's explode-and-remove burst draws the same ordinals 5.. and is culled before it
# reaches a crater. The roll frame is a RENDER-ONLY function of the global tick + `slot dx` (the walk writes no
# `slot code`), like the Terrazi roll: stopped (dx 8) holds frame 0, forward (14) cycles, back (2) reverses,
# dart (22) cycles at double rate — mirroring animate_grobda_forwards/backwards/fast_forward (4297-4321).
GROBDA_TARGET = "grobda"
GROBDA_CLONE_SLOT_ID = "grobda-clone-slot"  # sprite-local: which ground slot this clone renders
GROBDA_ROLL_FRAME_COUNT = 4  # tank/roll/01..04 (ordinals 1..4)
GROBDA_EXPLODE_BASE_ORDINAL = GROBDA_ROLL_FRAME_COUNT + 1  # 5: shared explosion burst follows the roll frames
GROBDA_CRATER_BASE_ORDINAL = GROBDA_EXPLODE_BASE_ORDINAL + EXPLODE_COSTUME_COUNT  # 13: crater frames last
GROBDA_STOPPED_DX = 8  # raw dx that reads as "stopped" (scroll-matched) — holds roll frame 0, no animation
GROBDA_DART_DX = 22  # raw dx of a dart — the fast-forward animation cadence
GROBDA_ROLL_PERIOD = 2  # ticks per tread frame while rolling (full 4-frame cycle every 8 ticks)
GROBDA_ROLL_PERIOD_FAST = 1  # a darting Grobda rolls its tread one frame per tick

# GND-07 (ground.domogram #89) renderer. ONE target, one costume set: the four idle/animation frames
# (domogram/idle/01..04, ordinals 1..4 — the codes 0x3C..0x3F the sprite cycles), then the shared solv_death
# burst (ordinals 5..12), then the two crater frames (ordinals 13..14) a bombed LAND Domogram flickers between.
# The animation frame is a RENDER-ONLY function of the anim timer (`slot fire timer` = _TYPE); the walk maintains
# the timer and the renderer maps it to a costume, like the Grobda roll (no `slot code` write).
DOMOGRAM_TARGET = "domogram"
DOMOGRAM_CLONE_SLOT_ID = "domogram-clone-slot"  # sprite-local: which ground slot this clone renders
DOMOGRAM_ANIM_FRAME_COUNT = 4  # domogram/idle/01..04 (ordinals 1..4)
DOMOGRAM_EXPLODE_BASE_ORDINAL = DOMOGRAM_ANIM_FRAME_COUNT + 1  # 5: shared explosion burst follows the anim frames
DOMOGRAM_CRATER_BASE_ORDINAL = DOMOGRAM_EXPLODE_BASE_ORDINAL + EXPLODE_COSTUME_COUNT  # 13: crater frames last
DOMOGRAM_FRAME_ORD_ID = "domogram-frame-ord"  # read-only render lookup: anim index (0..7) -> costume ordinal
# GND-07 shared runtime tables (read-only). The 32-entry vector table (dx = scroll/depth axis, dy = lateral),
# and the flattened per-step path columns walked by the follower (duration + vector index per step). Their
# builders and length invariant are near the schedule loaders; these IDs register them as Scratch lists.
DOMOGRAM_VECTOR_DX_ID = "domogram-vector-dx"
DOMOGRAM_VECTOR_DY_ID = "domogram-vector-dy"
DOMOGRAM_PATH_DURATION_ID = "domogram-path-duration"
DOMOGRAM_PATH_VECTOR_ID = "domogram-path-vector"


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
    if handler == SET_BACURA_COUNT_HANDLER:
        return params["count"]  # AIR-11: the increment quota the pump ramps num_bacura up by
    return 0


def _ground_scalars(record: dict) -> tuple[int, int, int]:
    # GND: the three runtime-readable scalars a ground-placement record needs, pre-decoded from the opaque
    # JSON payload (Scratch cannot parse JSON at runtime) — object_type (the ground dispatch discriminator),
    # slot (0-15), sprite_y (0-255). BOTH ground placement handlers carry them at the same JSON locations:
    # add_ground_object (the static + Grobda families) and GND-07 add_domogram_with_path (the Domogram, which
    # additionally carries a scripted path decoded by _load_domogram_paths). Every other handler needs none ->
    # (0, 0, 0); those fillers are inert because the ground columns are read only under those two handlers.
    if record["handler"] not in (ADD_GROUND_OBJECT_HANDLER, ADD_DOMOGRAM_HANDLER):
        return 0, 0, 0
    params = record.get("params", {})
    return record["object_type"], params["slot"], params["sprite_y"]


def _load_domogram_paths() -> tuple[list[int], list[int], list[int], list[int]]:
    # GND-07 (ground.domogram #89): flatten every Domogram instance's scripted path into two SHARED step
    # columns (each step's duration + its vector index into DOMOGRAM_VECTOR_*), and produce two per-record
    # columns aligned 1:1 with the flattened schedule (SCHEDULE_HANDLERS): `path start` (1-based index of the
    # instance's FIRST step in the shared columns, 0 for non-Domogram rows and sentinels) and `path count`
    # (params.path_step_count, 0 otherwise). The follower seeds `slot link` = path start and `slot vec left` =
    # path count, then walks the step columns. Iterated in the SAME area/record order as _load_all_area_schedules
    # (areas AREA_FIRST..AREA_MAX, each area's records then one materialized sentinel) so the per-record columns
    # line up with the schedule columns by construction; a module-load length assertion guards against drift.
    # A round-trip golden (tests/test_spec_docs.py) proves the decoded steps equal the committed JSON paths.
    areas = _load_spec_data("area-schedules.json")["areas"]
    by_area = {a["area"]: a for a in areas}
    path_starts: list[int] = []
    path_counts: list[int] = []
    step_durations: list[int] = []
    step_vectors: list[int] = []
    for area_number in range(AREA_FIRST, AREA_MAX + 1):
        for record in by_area[area_number]["records"]:
            if record["handler"] == ADD_DOMOGRAM_HANDLER:
                params = record["params"]
                steps = params["path"]
                if len(steps) != params["path_step_count"]:
                    raise SystemExit(
                        f"area {area_number} Domogram path_step_count "
                        f"{params['path_step_count']} != len(path) {len(steps)}"
                    )
                path_starts.append(len(step_durations) + 1)  # 1-based index of this instance's first step
                path_counts.append(len(steps))
                for step in steps:
                    step_durations.append(step["duration"])
                    step_vectors.append(step["vector_index"])
            else:
                path_starts.append(0)
                path_counts.append(0)
        path_starts.append(0)  # materialized end sentinel row
        path_counts.append(0)
    return path_starts, path_counts, step_durations, step_vectors


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

# GND-07: the two per-record path columns (aligned 1:1 with SCHEDULE_HANDLERS) + the two SHARED step
# columns walked by the Domogram follower. Built in the SAME area/record/sentinel order as the schedule,
# so the per-record columns line up by construction — asserted LOUD here so any drift fails at module load,
# not silently at runtime.
(
    SCHEDULE_DOMOGRAM_PATH_START,
    SCHEDULE_DOMOGRAM_PATH_COUNT,
    DOMOGRAM_PATH_DURATION,
    DOMOGRAM_PATH_VECTOR,
) = _load_domogram_paths()
if len(SCHEDULE_DOMOGRAM_PATH_START) != len(SCHEDULE_HANDLERS):
    raise SystemExit(
        f"Domogram path columns ({len(SCHEDULE_DOMOGRAM_PATH_START)}) must align 1:1 with the "
        f"flattened schedule ({len(SCHEDULE_HANDLERS)})"
    )

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
    # AUDIO: the shot×Bacura bounce runs on a blaster clone, which cannot play a Stage-owned
    # sound directly; it broadcasts this and the Stage plays BACURA_HIT_SND (src deactivate_shot
    # xevious_main.68k:2559). All other arcade SFX play from Stage-thread procs directly.
    "sfx bacura": "broadcastMsgId-sfx-bacura",
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
    # AIR-07: the four base Zakato object types (slow 0x12 / closeY 0x13 / fast 0x14 / cont 0x15) share
    # ONE update proc — all four run the same teleport-in / active / self-destruct machine, branching
    # inside `update zakato` on `slot type` for their movement (straight vs aimed) and fire trigger
    # (random fuse vs level-in-Y). Dispatch with a single OR branch, as the Zoshi/Jara do.
    is_zakato = blocks.op_or(
        blocks.op_or(
            blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZAKATO_SLOW_TYPE)),
            blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZAKATO_CLOSEY_TYPE)),
        ),
        blocks.op_or(
            blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZAKATO_FAST_TYPE)),
            blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZAKATO_CONT_TYPE)),
        ),
    )
    zakato_branch = blocks.if_reporter(
        is_zakato, [blocks.call_proc(UPDATE_ZAKATO_PROCCODE, warp=True)]
    )
    # AIR-10: the two Spario types each have their OWN update — Giddo (0x08) flies straight and plays its
    # own short burst; Brag (0x09) is the accelerating homer. Unlike Zoshi/Jara/Zakato these share no
    # core, so each gets its own single-type branch.
    giddo_spario_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(GIDDO_SPARIO_TYPE)),
        [blocks.call_proc(UPDATE_GIDDO_SPARIO_PROCCODE, warp=True)],
    )
    brag_spario_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(BRAG_SPARIO_TYPE)),
        [blocks.call_proc(UPDATE_BRAG_SPARIO_PROCCODE, warp=True)],
    )
    # AIR-08: the two Brag Zakato variants (0x16/0x17) share ONE update (teleport/active/self-destruct +
    # terminal fan, branching inside on `slot type` for the fuse-vs-proximity trigger) — a single OR
    # branch, like the base Zakato. The Garu Zakato (0x18) has its OWN update (no teleport, straight, fuse
    # -> detonate) — a single-type branch.
    is_brag_zakato = blocks.op_or(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(BRAG_ZAKATO_RND_TYPE)),
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(BRAG_ZAKATO_CLOSEY_TYPE)),
    )
    brag_zakato_branch = blocks.if_reporter(
        is_brag_zakato, [blocks.call_proc(UPDATE_BRAG_ZAKATO_PROCCODE, warp=True)]
    )
    garu_zakato_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(GARU_ZAKATO_TYPE)),
        [blocks.call_proc(UPDATE_GARU_ZAKATO_PROCCODE, warp=True)],
    )
    # AIR-09: the two Sheonite object types (right 0x31 / left 0x32) share ONE update proc — both run the
    # same home/lock/combine machine, branching inside on `slot type` for the lateral flank (right -flank,
    # left +flank) and the exit (right retreats, left vanishes). Dispatch with a single OR branch, like the
    # Jara/Zakato. `update sheonite` NEVER calls CHECK_AIR_HIT and raises no craft-hit: the pair is inert.
    is_sheonite = blocks.op_or(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(RIGHT_SHEONITE_TYPE)),
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(LEFT_SHEONITE_TYPE)),
    )
    sheonite_branch = blocks.if_reporter(
        is_sheonite, [blocks.call_proc(UPDATE_SHEONITE_PROCCODE, warp=True)]
    )
    # AIR-11: the Bacura is the one occupant dispatched by BAND MEMBERSHIP rather than by `walk type`. Its
    # slot type (BACURA_TYPE=1) collides with SHOT_TYPE — the shot slots (37-39) also carry type 1 — so a
    # `walk type == 1` branch would run the Bacura handler over live shots. The Bacura band (17-32) only
    # ever holds Bacura, so a slot-index range test dispatches it unambiguously. `update bacura` OMITS the
    # CHECK_AIR_HIT call every flying family makes; that omission IS the shot-invulnerability.
    in_bacura_band = blocks.op_and(
        blocks.op_not(blocks.op_lt(cursor(), number(BACURA_SLOTS[0]))),
        blocks.op_not(blocks.op_gt(cursor(), number(BACURA_SLOTS[1]))),
    )
    bacura_branch = blocks.if_reporter(
        in_bacura_band, [blocks.call_proc(UPDATE_BACURA_PROCCODE, warp=True)]
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
    zolbak_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZOLBAK_TYPE)),
        [blocks.call_proc(UPDATE_ZOLBAK_PROCCODE, warp=True)],
    )
    derota_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(DEROTA_TYPE)),
        [blocks.call_proc(UPDATE_DEROTA_PROCCODE, warp=True)],
    )
    garu_derota_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(GARU_DEROTA_TYPE)),
        [blocks.call_proc(UPDATE_GARU_DEROTA_PROCCODE, warp=True)],
    )
    # GND-05 (#87): all five slots of a Boza Logram composite share BOZA_LOGRAM_TYPE and ONE update proc,
    # which branches internally on `slot link` (outer dome vs centre bullseye) — a single-type branch.
    boza_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(BOZA_LOGRAM_TYPE)),
        [blocks.call_proc(UPDATE_BOZA_PROCCODE, warp=True)],
    )
    # GND-06 (#88): the 12 live Grobda variants each carry a distinct `slot type` (0x2C + 0x35..0x40) but
    # share ONE update proc, which branches internally on `slot type` for the per-variant reaction. Dispatch
    # them with a single folded-OR branch — a Grobda is the first ground family that MOVES under its own
    # velocity, so `update grobda` routes motion through `advance ground moving` rather than `advance ground`.
    is_grobda = functools.reduce(
        blocks.op_or,
        (blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(t)) for t in GROBDA_TYPES),
    )
    grobda_branch = blocks.if_reporter(
        is_grobda, [blocks.call_proc(UPDATE_GROBDA_PROCCODE, warp=True)]
    )
    # GND-07 (#89): the Domogram (single type 0x2E) is the first ground family that both MOVES on a scripted
    # path AND fires; `update domogram` routes its motion through `advance ground moving`.
    domogram_branch = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(DOMOGRAM_TYPE)),
        [blocks.call_proc(UPDATE_DOMOGRAM_PROCCODE, warp=True)],
    )
    dispatch = blocks.if_reporter(occupied, [read_type, toroid_branch, kapi_branch, torkan_branch, terrazi_branch, zoshi_branch, jara_branch, zakato_branch, giddo_spario_branch, brag_spario_branch, brag_zakato_branch, garu_zakato_branch, sheonite_branch, bacura_branch, bullet_branch, barra_branch, garu_branch, logram_branch, zolbak_branch, derota_branch, garu_derota_branch, boza_branch, grobda_branch, domogram_branch])
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


def _craft_overlap_reporter(blocks: Blocks, window: tuple = HIT_WINDOW_BULLET_FLYING) -> str:
    """PLY-02: boolean — does the current slot (`slot index`) overlap the craft's cell within `window`
    (default HIT_WINDOW_BULLET_FLYING, the shared flying/bullet box)? The craft is placed at player
    row/col scaled to shadow half-px (cell-quantized); the object is floored to its shadow MSB. Y is
    the scroll axis, X the lateral, matching the reference's byte compare. AIR-11 (air.bacura) passes
    the wider HIT_WINDOW_BACURA — the reference's `check_bacura_hit_solvalou` (2225-2237) uses the same
    compare against a larger box than the flying/bullet check."""
    y_bias, y_width, x_bias, x_width = window
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


def _draw_spawn_column(blocks: Blocks, exclude_craft: bool = True, col_offset: int = 0) -> tuple[list, str]:
    # Shared bounded spawn-column draw for the flying families: draw a lateral column from the shared
    # stream, reject-and-redraw until it is on-screen (`rnd & 31`, reject >= 25, + 3 => column 3..27),
    # or give up after SPAWN_DRAW_ATTEMPTS tries (the recorded bounded-draw deviation). On success it
    # sets `slot y` and `spawn found`; on exhaustion the slot is left empty (the caller's stamp is gated
    # on `spawn found`, so the slot is retried next tick). Returns the `spawn attempts`/`spawn found`
    # reset blocks and the draw loop for the caller to chain ahead of its own family-specific stamp/aim.
    # Separate `rng mod (mask+1)` reads because a reporter cannot be shared across parents (it is stolen
    # from the first).
    #
    # `exclude_craft` selects the two arcade draw routines: the DEFAULT (`gen_rnd_spriteY` 5156-5169,
    # Toroid/Terrazi/Jara) ALSO rejects any column within SPAWN_CRAFT_GAP of the craft; `exclude_craft=False`
    # is the Kapi/Zakato/Spario draw (`gen_random_Y_store_obj` 5147-5154) — the SAME in-range clamp with NO
    # craft-proximity reject, so those families can spawn directly over the craft's column.
    # `col_offset` adds a fixed lateral cell to the accepted column: the teleport families (base Zakato,
    # Brag Zakato) draw through `init_teleport` (3994), which does `add.b #1,(_Y,a5)` (4000) AFTER the
    # craft-independent draw — a +1-cell teleport offset. It is applied to the stamped column only, not to
    # the (unused-here) craft comparison, matching the arcade order.
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
            blocks.op_mul(blocks.op_add(blocks.op_mod(variable("rng out", RNG_OUT_ID), number(SPAWN_COL_MASK + 1)), number(SPAWN_COL_OFFSET + col_offset)), number(SLOT_UNITS_PER_CELL)),
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
                    # AUDIO: FLYING_ENEMY_HIT_SND on a scored flying kill (src xevious_main.68k:2537,
                    # check_shot_hit_flying_enemy). Stage-owned sound, played on the Stage thread.
                    blocks.play_sound("air_destroy"),
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
                    # AUDIO: GROUND_EXPLOSION_SND on a scored ground kill (src xevious_main.68k:2615).
                    # Stage-owned sound, played on the Stage thread.
                    blocks.play_sound("ground_destroy"),
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


def install_advance_ground_moving(blocks: Blocks) -> None:
    # GND-06/07 (ground.grobda #88 / ground.domogram #89): one tick of a SELF-MOVING ground object at
    # `slot index` — the first ground slot that moves by its OWN velocity instead of the fixed terrain
    # scroll. Mirrors the source's move_object_dX / move_object_dX_dY (xevious_main.68k 4817-4846): the
    # scroll-axis position (`slot x`) advances by TICK_VELOCITY_SCALE * `slot dx` and the lateral position
    # (`slot y`) by TICK_VELOCITY_SCALE * `slot dy`, then the SAME bottom-edge cull `advance ground` uses.
    # There is NO AREA_PROGRESS_STEP scroll baseline: the source applies no separate scroll term to a moving
    # object, so the terrain scroll is baked into the stored delta (raw 8 = scroll-matched, so a "stopped"
    # object still drifts DOWN the field at the scroll rate and eventually culls). A Grobda leaves `slot dy`
    # 0 (it moves scroll-axis-only, `activate_and_set_grobda_dX` 4574-4579 clears _dY); a Domogram drives
    # both axes from its path vector. Culling stays bottom-only, like `advance ground`: even a backward
    # Grobda (raw dX 2 -> +8/tick absolute) still creeps down the field, never off the top or sides.
    definition = _install_warp_proc(blocks, ADVANCE_GROUND_MOVING_PROCCODE)
    move_x = _set_cur_item(
        blocks,
        "slot x",
        SLOT_X_ID,
        blocks.op_add(
            _cur_item(blocks, "slot x", SLOT_X_ID),
            blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)),
        ),
    )
    move_y = _set_cur_item(
        blocks,
        "slot y",
        SLOT_Y_ID,
        blocks.op_add(
            _cur_item(blocks, "slot y", SLOT_Y_ID),
            blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)),
        ),
    )
    off_bottom = blocks.op_not(blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MAX)))
    cull = blocks.if_reporter(off_bottom, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    blocks.chain(definition, [move_x, move_y, cull])


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


def install_update_zolbak(blocks: Blocks) -> None:
    # GND-02 / ground.zolbak (#85): one tick of a Zolbak. It IS the Barra crater model — a passive dome
    # that never fires, scrolls with the terrain while ACTIVE, and craters PERSISTENTLY once bombed
    # (handle_bomb_explosion, the SAME routine the Barra uses, NOT the Garu node's explode-and-remove) —
    # with ONE extra behaviour on the FIRST HIT tick: reduce the enemy AI level by 2, floored at 0
    # (handle_1F_Zolbak -> reduce_enemy_ai_by_2 $1B1F). The detector zeroed `slot timer` at the hit, so
    # `slot timer == 0` uniquely marks that first HIT tick (the crater clock only climbs afterwards) — the
    # reduction runs EXACTLY ONCE per kill, never on every crater tick. Faithful to the arcade
    # `subq #2,d0; jcc; moveq #0` (subtract 2, clamp the unsigned underflow to 0), reading and writing the
    # SAME `ai level` the difficulty director grows and folds.
    definition = _install_warp_proc(blocks, UPDATE_ZOLBAK_PROCCODE)
    reduce_ai = blocks.if_reporter(
        blocks.op_eq(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(0)),
        [
            blocks.change_var("ai level", AI_LEVEL_ID, -AI_LEVEL_ZOLBAK_DROP),
            blocks.if_reporter(
                blocks.op_lt(variable("ai level", AI_LEVEL_ID), number(0)),
                [blocks.set_var("ai level", AI_LEVEL_ID, number(0))],
            ),
        ],
    )
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
    # HIT: reduce the AI level once (guarded), advance the crater clock, then scroll + cull.
    blocks.substack(
        top, [reduce_ai, tick_clock, blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)]
    )
    # ACTIVE: just the shared terrain scroll + off-field cull (a Zolbak never fires).
    blocks.substack(
        top, [blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)], name="SUBSTACK2"
    )
    blocks.chain(definition, [top])


def install_update_derota(blocks: Blocks) -> None:
    # GND-04 / ground.derota (#86): one tick of a Derota — a plain periodic aimed turret. There is NO
    # open/close dome cycle (unlike the Logram): once bombed (HIT) it craters PERSISTENTLY like the Barra
    # (handle_bomb_explosion), and while ACTIVE it drives the SHARED fire-permission gate
    # (chk_timer_fire_bullet_reinit_timer) — one aimed bullet per masked-random reload — but ONLY while
    # still high enough on the field. The arcade skips the fire when `gnd_stop_firing_row < _X` (the row
    # MSB): it fires only while `cur_row <= ground stop firing row`, the same arm gate the Logram uses.
    # The shared gate owns the 8-arcade-frame (every-4th-tick) cadence, so the arm gate here is just the
    # row test — no extra phase wrap. `advance ground` (scroll + off-field cull) runs every tick.
    definition = _install_warp_proc(blocks, UPDATE_DEROTA_PROCCODE)
    armed = blocks.op_not(
        blocks.op_gt(_cur_row(blocks), variable("ground stop firing row", GROUND_STOP_FIRING_ROW_ID))
    )
    fire = blocks.if_reporter(armed, [blocks.call_proc(FIRE_GATE_PROCCODE, warp=True)])
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
    # HIT: the Barra crater clock, then scroll + cull.
    blocks.substack(top, [tick_clock, blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)])
    # ACTIVE: fire (if armed) via the shared gate, then scroll + cull.
    blocks.substack(
        top, [fire, blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)], name="SUBSTACK2"
    )
    blocks.chain(definition, [top])


def install_update_garu_derota(blocks: Blocks) -> None:
    # GND-04 / ground.derota (#86): one tick of a Garu Derota part. Like the Garu Barra it is two adjacent
    # slots sharing GARU_DEROTA_TYPE: the indestructible 2x2 base (state SLOT_GARU_BASE) and the
    # destructible node (state ACTIVE, then HIT once bombed). The ONE difference from the Garu Barra is
    # that the node FIRES: garu_derota_handler ($1CBE) calls chk_timer_fire_bullet_reinit_timer every
    # active tick with NO stop-firing-row gate (unlike the single Derota). So an ACTIVE node drives the
    # shared fire-permission gate unconditionally; the base (sentinel state) never fires and only scrolls.
    # The node's death mirrors the Garu Barra node exactly (explode_and_remove_object $3216): a HIT node
    # advances its burst clock and REMOVES itself once the burst finishes (timer >= GARU_REMOVE_FRAMES) —
    # it vanishes with no crater. The base is never hit (the detector's ==ACTIVE gate rejects the
    # sentinel), so it only ever scrolls until it culls off-field.
    definition = _install_warp_proc(blocks, UPDATE_GARU_DEROTA_PROCCODE)
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
    # Base (sentinel) or ACTIVE node: an ACTIVE node fires the shared gate; both then scroll + cull.
    node_fire = blocks.if_reporter(
        blocks.op_eq(_cur_item(blocks, "slot state", SLOT_STATE_ID), number(SLOT_ACTIVE)),
        [blocks.call_proc(FIRE_GATE_PROCCODE, warp=True)],
    )
    blocks.substack(
        top, [node_fire, blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)], name="SUBSTACK2"
    )
    blocks.chain(definition, [top])


def install_update_boza(blocks: Blocks) -> None:
    # GND-05 / ground.boza-logram (#87): one tick of a single slot of a Boza Logram composite
    # (handle_2D_Boza_Logram $1CDE). All five slots share BOZA_LOGRAM_TYPE and THIS one proc, which branches
    # on `slot link` — the port of the arcade's per-object `_EXTRA` pointer (memory: the cross-slot link):
    #   * an OUTER dome (link > 0, holding the CENTRE's slot index) behaves EXACTLY like a lone Logram
    #     (install_update_logram / handle_boza_logram_outer $1F..): the arm+cadence-gated open/close/fire
    #     cycle on its captured mask, cratering PERSISTENTLY once bombed. Its ONE extra behaviour, on HIT, is
    #     to rewrite the CENTRE slot's `slot pts` to the 600-point position (update_centre_points_value: the
    #     arcade writes `_EXTRA->_PTS` every hit frame, so this unguarded idempotent write is faithful). The
    #     centre was already scored at its full/current value when the bomb resolved, so this only lowers what
    #     a LATER bomb on the centre would award.
    #   * the CENTRE (link == 0) never fires (handle_boza_logram_centre): ACTIVE it only scrolls; bombed it
    #     craters PERSISTENTLY like the Barra AND cascades — destroy_all_outer_lograms sets every outer's
    #     state directly to HIT, BYPASSING the award sweep, so a centre-first bomb clears the outers for NO
    #     score (the type-agnostic `check ground hit` only awards an ACTIVE slot; a cascaded outer is already
    #     HIT). The cascade addresses the four outer slots directly (centre index - 1..-4 — the outers are
    #     stamped at base+0..3, the centre at base+4), the port of the arcade walking `_EXTRA`. It is
    #     idempotent (an already-HIT outer keeps its own crater clock — no timer touch), matching the arcade
    #     routine, so it runs every centre-HIT tick with no guard.
    # The walk sweeps ascending slot index, so the outers (base+0..3) update BEFORE the centre (base+4): a
    # centre-first cascade marks the outers this same tick and their crater clocks (timer 0 from spawn) begin
    # cleanly on the next tick.
    definition = _install_warp_proc(blocks, UPDATE_BOZA_PROCCODE)
    fire_timer = lambda: _cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID)

    # ---- OUTER (link > 0): the Logram open/close/fire cycle + the centre-value downgrade on HIT. ----
    def animate_step() -> list[str]:
        # One ANIMATE-phase step, built FRESH each call (used at the WAIT->ANIMATE fall-through and in the
        # steady ANIMATE branch), identical in shape to install_update_logram.animate_step so an outer Boza
        # dome animates and fires exactly like a lone Logram. No reporter/statement is shared between parents.
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
        dome_ordinal = blocks.op_sub(
            number(LOGRAM_OPEN_FRAME_COUNT),
            blocks.op_abs(blocks.op_sub(stage(), number(LOGRAM_STAGE_PEAK))),
        )
        blocks.substack(
            recycle, [_set_cur_item(blocks, "slot code", SLOT_CODE_ID, dome_ordinal)], name="SUBSTACK2"
        )
        return [inc, fire, recycle]

    phase = blocks.add("control_if_else")
    in_wait = blocks.op_eq(_cur_item(blocks, "slot flag", SLOT_FLAG_ID), number(LOGRAM_WAIT_PHASE))
    blocks.blocks[phase]["inputs"]["CONDITION"] = [2, in_wait]
    blocks.blocks[in_wait]["parent"] = phase
    dec = _set_cur_item(
        blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_sub(fire_timer(), number(1))
    )
    transition = blocks.if_reporter(
        blocks.op_eq(fire_timer(), number(0)),
        [_set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(LOGRAM_ANIMATE_PHASE)), *animate_step()],
    )
    blocks.substack(phase, [dec, transition])
    blocks.substack(phase, animate_step(), name="SUBSTACK2")

    armed = blocks.op_not(
        blocks.op_gt(_cur_row(blocks), variable("ground stop firing row", GROUND_STOP_FIRING_ROW_ID))
    )
    on_phase = blocks.op_eq(
        blocks.op_mod(variable("tick", TICK_ID), number(FIRE_GATE_PHASE_TICKS)), number(0)
    )
    arm = blocks.if_reporter(blocks.op_and(armed, on_phase), [phase])

    # HIT: rewrite the linked centre slot's value to the 600-point position (`slot link` holds that index),
    # then the Barra crater clock; else the ACTIVE arm/animate. Both then scroll + cull via advance ground.
    downgrade = blocks.list_replace(
        "slot pts",
        SLOT_PTS_ID,
        _cur_item(blocks, "slot link", SLOT_LINK_ID),
        number(BOZA_CENTRE_DOWNGRADED_PTS),
    )
    outer_tick_clock = _set_cur_item(
        blocks,
        "slot timer",
        SLOT_TIMER_ID,
        blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP)),
    )
    outer = blocks.add("control_if_else")
    outer_hit = blocks.op_eq(_cur_item(blocks, "slot state", SLOT_STATE_ID), number(SLOT_HIT))
    blocks.blocks[outer]["inputs"]["CONDITION"] = [2, outer_hit]
    blocks.blocks[outer_hit]["parent"] = outer
    blocks.substack(
        outer,
        [downgrade, outer_tick_clock, blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)],
    )
    blocks.substack(
        outer, [arm, blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)], name="SUBSTACK2"
    )

    # ---- CENTRE (link == 0): never fires; on HIT craters + cascades all four outers to HIT. ----
    cascade = [
        blocks.list_replace(
            "slot state",
            SLOT_STATE_ID,
            blocks.op_sub(variable("slot index", SLOT_INDEX_ID), number(k)),
            number(SLOT_HIT),
        )
        for k in range(1, BOZA_CENTRE_OFFSET + 1)
    ]
    centre_tick_clock = _set_cur_item(
        blocks,
        "slot timer",
        SLOT_TIMER_ID,
        blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP)),
    )
    centre = blocks.add("control_if_else")
    centre_hit = blocks.op_eq(_cur_item(blocks, "slot state", SLOT_STATE_ID), number(SLOT_HIT))
    blocks.blocks[centre]["inputs"]["CONDITION"] = [2, centre_hit]
    blocks.blocks[centre_hit]["parent"] = centre
    blocks.substack(
        centre,
        [*cascade, centre_tick_clock, blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)],
    )
    blocks.substack(
        centre, [blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)], name="SUBSTACK2"
    )

    # ---- Top: branch on `slot link` (0 = centre, >0 = outer). ----
    top = blocks.add("control_if_else")
    at_centre = blocks.op_eq(_cur_item(blocks, "slot link", SLOT_LINK_ID), number(0))
    blocks.blocks[top]["inputs"]["CONDITION"] = [2, at_centre]
    blocks.blocks[at_centre]["parent"] = top
    blocks.substack(top, [centre])
    blocks.substack(top, [outer], name="SUBSTACK2")
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


def _grobda_reticle_hit(blocks: Blocks, slot_const: int) -> str:
    """GND-06: boolean — is the current Grobda's cell inside the [-2,+1] alignment band of the reticle object
    at `slot_const` (CROSSHAIR_SLOT or BOMB_TARGET_SLOT) on BOTH axes? Mirrors check_grobda_in_crosshairs
    (4589) / check_targeted_and_init_timer (4574): `d = target_cell - grobda_cell` in [GROBDA_RETICLE_LOW,
    GROBDA_RETICLE_HIGH] for the depth row (slot x MSB) and the lateral col (slot y MSB). The reticle object is
    read UNCONDITIONALLY (the source has no in-flight gate), so a targeted variant reacts to the frozen
    last-bomb-target cell between bombs. Each delta is rebuilt FRESH — a reporter attaches to one parent only,
    so a shared subtree would be stolen by the second use, leaving the first compare with an empty operand."""
    target_row = lambda: blocks.op_floor(
        blocks.op_div(blocks.list_item("slot x", SLOT_X_ID, number(slot_const)), number(SLOT_UNITS_PER_CELL))
    )
    target_col = lambda: blocks.op_floor(
        blocks.op_div(blocks.list_item("slot y", SLOT_Y_ID, number(slot_const)), number(SLOT_UNITS_PER_CELL))
    )
    d_row = lambda: blocks.op_sub(target_row(), _cur_row(blocks))
    d_col = lambda: blocks.op_sub(target_col(), _cur_col(blocks))
    row_ok = blocks.op_and(
        blocks.op_not(blocks.op_lt(d_row(), number(GROBDA_RETICLE_LOW))),
        blocks.op_not(blocks.op_gt(d_row(), number(GROBDA_RETICLE_HIGH))),
    )
    col_ok = blocks.op_and(
        blocks.op_not(blocks.op_lt(d_col(), number(GROBDA_RETICLE_LOW))),
        blocks.op_not(blocks.op_gt(d_col(), number(GROBDA_RETICLE_HIGH))),
    )
    return blocks.op_and(row_ok, col_ok)


def install_update_grobda(blocks: Blocks) -> None:
    # GND-06 (ground.grobda #88): one tick of a Grobda tank at `slot index`, for all 12 live variants
    # (handle_2C + handle_35..40, mirroring xevious_main.68k 4289-4611). A Grobda NEVER fires. Once bombed
    # (state HIT) a LAND variant craters PERSISTENTLY like the Barra (handle_bomb_explosion — the crater is
    # terrain-locked, so its HIT branch scrolls via `advance ground`, NOT the mover) and a WATER variant plays
    # the explode-and-remove burst and VANISHES like a Garu node (explode_and_remove_object). While ACTIVE it
    # moves under its own velocity through `advance ground moving` (Commit 1) and runs its per-variant reticle
    # reaction. `slot timer` is reused across the two states — the reaction countdown while ACTIVE, the
    # crater/burst clock while HIT — safely, because the ground detector zeroes it at the hit and the states
    # are exclusive. `slot flag` is the reaction phase (0 pre-trigger, 1 reacting, 2 latched). The tank roll is
    # a render-only function of the global tick + `slot dx` (grobda_blocks), so the walk writes no `slot code`.
    definition = _install_warp_proc(blocks, UPDATE_GROBDA_PROCCODE)
    cur_flag = lambda: _cur_item(blocks, "slot flag", SLOT_FLAG_ID)
    set_dx = lambda v: _set_cur_item(blocks, "slot dx", SLOT_DX_ID, number(v))
    set_flag = lambda v: _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(v))
    set_timer = lambda v: _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(v))

    def countdown_step(variant: GrobdaVariant) -> list[str]:
        # One reaction-countdown step (the source's `subq #1,_TIMER` per frame): decrement by the frame-step;
        # on reaching 0 apply end_dx and either RE-ARM (flag 0, repeatable 0x3C) or LATCH (flag 2). Built fresh
        # each call — it runs both at the trigger-tick fall-through and on every steady reacting tick.
        dec = _set_cur_item(
            blocks,
            "slot timer",
            SLOT_TIMER_ID,
            blocks.op_sub(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP)),
        )
        expired = blocks.op_not(
            blocks.op_gt(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(0))
        )
        end_flag = GROBDA_FLAG_PRETRIGGER if variant.rearm else GROBDA_FLAG_LATCHED
        finish = blocks.if_reporter(expired, [set_dx(variant.end_dx), set_flag(end_flag)])
        return [dec, finish]

    def active_reaction(variant: GrobdaVariant) -> list[str]:
        # The per-variant reticle reaction (motion only). A never-reacting variant (stationary / forward)
        # simply rides the velocity seeded at spawn — no statements. A reacting variant arms on `slot flag` 0.
        if variant.trigger is None:
            return []
        if variant.end_dx is None:
            # Permanent (crosshairs -> forward forever, 0x36/0x3F): commit react_dx and latch; no timer.
            arm = blocks.if_reporter(
                _grobda_reticle_hit(blocks, variant.trigger),
                [set_dx(variant.react_dx), set_flag(GROBDA_FLAG_REACTING)],
            )
            return [
                blocks.if_reporter(
                    blocks.op_eq(cur_flag(), number(GROBDA_FLAG_PRETRIGGER)), [arm]
                )
            ]
        # Timed: pre-trigger arms + counts down the SAME tick (the source jumps straight into the reaction
        # label and decrements once); an if/else on `slot flag == 0` keeps the trigger tick from ALSO running
        # the steady countdown branch, which would double-decrement.
        phase = blocks.add("control_if_else")
        is_pre = blocks.op_eq(cur_flag(), number(GROBDA_FLAG_PRETRIGGER))
        blocks.blocks[phase]["inputs"]["CONDITION"] = [2, is_pre]
        blocks.blocks[is_pre]["parent"] = phase
        arm = blocks.if_reporter(
            _grobda_reticle_hit(blocks, variant.trigger),
            [
                set_dx(variant.react_dx),
                set_timer(GROBDA_REACTION_FRAMES),
                set_flag(GROBDA_FLAG_REACTING),
                *countdown_step(variant),
            ],
        )
        blocks.substack(phase, [arm])
        reacting = blocks.if_reporter(
            blocks.op_eq(cur_flag(), number(GROBDA_FLAG_REACTING)), countdown_step(variant)
        )
        blocks.substack(phase, [reacting], name="SUBSTACK2")
        return [phase]

    def hit_branch(variant: GrobdaVariant) -> list[str]:
        tick_clock = _set_cur_item(
            blocks,
            "slot timer",
            SLOT_TIMER_ID,
            blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP)),
        )
        if variant.water:
            # Water: explode-and-remove burst, then VANISH (like a Garu node) — no persistent crater.
            done = blocks.op_not(
                blocks.op_lt(
                    _cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(GARU_REMOVE_FRAMES)
                )
            )
            finish = blocks.add("control_if_else")
            blocks.blocks[finish]["inputs"]["CONDITION"] = [2, done]
            blocks.blocks[done]["parent"] = finish
            blocks.substack(finish, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
            blocks.substack(
                finish, [blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)], name="SUBSTACK2"
            )
            return [tick_clock, finish]
        # Land: the Barra crater clock, then the terrain scroll + off-field cull.
        return [tick_clock, blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)]

    branches: list[str] = []
    for variant in GROBDA_VARIANTS:
        state = blocks.add("control_if_else")
        is_hit = blocks.op_eq(_cur_item(blocks, "slot state", SLOT_STATE_ID), number(SLOT_HIT))
        blocks.blocks[state]["inputs"]["CONDITION"] = [2, is_hit]
        blocks.blocks[is_hit]["parent"] = state
        blocks.substack(state, hit_branch(variant))
        blocks.substack(
            state,
            [*active_reaction(variant), blocks.call_proc(ADVANCE_GROUND_MOVING_PROCCODE, warp=True)],
            name="SUBSTACK2",
        )
        branches.append(
            blocks.if_reporter(
                blocks.op_eq(_cur_item(blocks, "slot type", SLOT_TYPE_ID), number(variant.type)),
                [state],
            )
        )
    blocks.chain(definition, branches)


def install_update_domogram(blocks: Blocks) -> None:
    # GND-07 (ground.domogram #89): one tick of a Domogram at `slot index`, mirroring handle_2E_Domogram
    # ($2ED6). It is the FIRST ground family that both MOVES under its own velocity (a scripted path) AND
    # FIRES. Once bombed (state HIT) it craters PERSISTENTLY like the Barra (handle_bomb_explosion — the
    # crater is terrain-locked, so its HIT branch scrolls via `advance ground`, NOT the mover). While ACTIVE,
    # each tick runs, in the arcade's order: (1) the PATH FOLLOWER, (2) the FIRE logic, (3) the MOVE via
    # `advance ground moving` (Commit 1). The slot columns are repurposed per the family map: `slot flag` =
    # _VECLEN (frames left on the current vector), `slot link` = _EXTRA (1-based index into the shared step
    # columns), `slot vec left` = _NVEC (scripted vectors remaining), `slot fire timer` = _TYPE (the 24-frame
    # shot-animation timer), `slot timer` = the shot timer while ACTIVE / the crater clock while HIT (the
    # detector zeroes it at the hit, and the states are exclusive), `slot fire mask` = _FFREQ, and
    # `slot dx/dy` = the current vector's velocity. `slot code` is not written — the renderer derives the
    # sprite from _TYPE, like the Grobda roll.
    definition = _install_warp_proc(blocks, UPDATE_DOMOGRAM_PROCCODE)
    veclen = lambda: _cur_item(blocks, "slot flag", SLOT_FLAG_ID)  # _VECLEN
    nvec = lambda: _cur_item(blocks, "slot vec left", SLOT_VEC_LEFT_ID)  # _NVEC
    ptr = lambda: _cur_item(blocks, "slot link", SLOT_LINK_ID)  # _EXTRA (1-based flat step index)
    anim = lambda: _cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID)  # _TYPE (shot anim timer)
    shot = lambda: _cur_item(blocks, "slot timer", SLOT_TIMER_ID)  # _TIMER (shot timer, ACTIVE)

    def path_advance() -> list[str]:
        # The arcade's path coroutine ($2F0E): each tick `subq #1,_VECLEN`; when it reaches 0, load the next
        # step — read its duration into _VECLEN and its vector index, look up (dY,dX) from domogram_vector_tbl
        # into _dY/_dX, advance the path pointer, and `subq #1,_NVEC`; when _NVEC hits 0 the object holds the
        # last vector FOREVER (domogram_done_all_vectors + SET_REENTRY_ADDR_HERE). Port: decrement _VECLEN by
        # the frame-step (TICK_TIMER_STEP); load when it falls to <= 0; guard the whole thing on _NVEC > 0 so
        # an exhausted path simply holds the last vector (no further loads). The stored vector index is 0..31
        # (0-based, as in the source); a Scratch list is 1-based, so the table lookup uses index + 1.
        vec_index = lambda: blocks.list_item(
            "domogram path vector", DOMOGRAM_PATH_VECTOR_ID, ptr()
        )
        table_index = lambda: blocks.op_add(vec_index(), number(1))
        load = [
            _set_cur_item(
                blocks,
                "slot dx",
                SLOT_DX_ID,
                blocks.list_item("domogram vector dx", DOMOGRAM_VECTOR_DX_ID, table_index()),
            ),
            _set_cur_item(
                blocks,
                "slot dy",
                SLOT_DY_ID,
                blocks.list_item("domogram vector dy", DOMOGRAM_VECTOR_DY_ID, table_index()),
            ),
            _set_cur_item(
                blocks,
                "slot flag",
                SLOT_FLAG_ID,
                blocks.list_item("domogram path duration", DOMOGRAM_PATH_DURATION_ID, ptr()),
            ),
            _set_cur_item(blocks, "slot link", SLOT_LINK_ID, blocks.op_add(ptr(), number(1))),
            _set_cur_item(
                blocks, "slot vec left", SLOT_VEC_LEFT_ID, blocks.op_sub(nvec(), number(1))
            ),
        ]
        dec = _set_cur_item(
            blocks, "slot flag", SLOT_FLAG_ID, blocks.op_sub(veclen(), number(TICK_TIMER_STEP))
        )
        need_load = blocks.op_not(blocks.op_gt(veclen(), number(0)))  # _VECLEN <= 0
        return [
            blocks.if_reporter(
                blocks.op_gt(nvec(), number(0)),
                [dec, blocks.if_reporter(need_load, load)],
            )
        ]

    def shooting_step() -> list[str]:
        # One frame of the shot animation (domogram_shooting $2F76): `subq #1,_TYPE`, and when _TYPE reaches
        # 12 (the animation midpoint of 24) fire ONE aimed bullet at the craft and reload the shot timer with
        # a fresh masked-random delay. Port decrements by the frame-step (24->12 in six ticks = the 12-frame
        # midpoint); 24 and 12 are both even so the `== 12` check is hit exactly. Built FRESH each call (used
        # both at the WAIT->animate fall-through and on steady animating ticks) — no reporter shared across parents.
        dec = _set_cur_item(
            blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_sub(anim(), number(TICK_TIMER_STEP))
        )
        reload_shot = blocks.op_add(
            blocks.op_mod(
                variable("rng out", RNG_OUT_ID),
                blocks.op_add(_cur_item(blocks, "slot fire mask", SLOT_FIRE_MASK_ID), number(1)),
            ),
            number(1),
        )
        fire = blocks.if_reporter(
            blocks.op_eq(anim(), number(DOMOGRAM_FIRE_FRAME)),
            [
                *_fire_aimed_bullet(blocks),
                blocks.call_proc(RNG_PROCCODE, warp=True),
                _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, reload_shot),
            ],
        )
        return [dec, fire]

    def fire_logic() -> list[str]:
        # domogram_main ($2F54): if the shot-animation timer (_TYPE) is already running, just step it; else,
        # only while still high enough on the field (`cur_row <= ground stop firing row`, arcade `jcs` when
        # `gnd_stop_firing_row < _X`) AND on the arcade's every-8th-frame phase (`countup_timer_1 & 7 == 0`,
        # i.e. every 4th tick here), count the shot timer DOWN by 1; on reaching 0 start a 24-frame animation
        # (_TYPE = 24) and run one shooting step the SAME tick (the arcade's fall-through into domogram_shooting).
        armed = blocks.op_not(
            blocks.op_gt(_cur_row(blocks), variable("ground stop firing row", GROUND_STOP_FIRING_ROW_ID))
        )
        on_phase = blocks.op_eq(
            blocks.op_mod(variable("tick", TICK_ID), number(FIRE_GATE_PHASE_TICKS)), number(0)
        )
        dec_shot = _set_cur_item(
            blocks, "slot timer", SLOT_TIMER_ID, blocks.op_sub(shot(), number(1))
        )
        start = blocks.if_reporter(
            blocks.op_not(blocks.op_gt(shot(), number(0))),  # shot timer <= 0
            [
                _set_cur_item(
                    blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, number(DOMOGRAM_ANIM_FRAMES)
                ),
                *shooting_step(),
            ],
        )
        gated = blocks.if_reporter(blocks.op_and(armed, on_phase), [dec_shot, start])
        top = blocks.add("control_if_else")
        animating = blocks.op_gt(anim(), number(0))
        blocks.blocks[top]["inputs"]["CONDITION"] = [2, animating]
        blocks.blocks[animating]["parent"] = top
        blocks.substack(top, shooting_step())
        blocks.substack(top, [gated], name="SUBSTACK2")
        return [top]

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
    # HIT: the Barra crater clock, then the terrain scroll + off-field cull (the crater is terrain-locked).
    blocks.substack(top, [tick_clock, blocks.call_proc(ADVANCE_GROUND_PROCCODE, warp=True)])
    # ACTIVE: follow the path, run the fire logic, then move under the object's own velocity.
    blocks.substack(
        top,
        [
            *path_advance(),
            *fire_logic(),
            blocks.call_proc(ADVANCE_GROUND_MOVING_PROCCODE, warp=True),
        ],
        name="SUBSTACK2",
    )
    blocks.chain(definition, [top])


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


def install_emit_radiating_bullet(blocks: Blocks) -> None:
    # AIR-12: emit ONE non-homing bullet from the slot at `slot index`, aimed at the EXPLICIT
    # direction `radiating angle` (0..31) rather than at the craft -- the shared radiating-spread
    # mechanism the Brag Zakato fan and Garu Zakato ring consume (`init_radiating_bullet` $32C4).
    # Allocate an idle bullet, copy the firing slot's cell into it, and set its velocity from the
    # 48-magnitude tier (`angle_dX_dY_terrazi_torkan_tbl`, 3 px/frame -- FASTER than the 2 px/frame
    # aimed generic bullet) at the given angle. The port lists are 1-based, so the table index is
    # `(radiating angle mod 32) + 1` -- the reference's `& 0x1f` 5-bit wrap made explicit here so the
    # emitter is self-contained (its ring/fan callers step the raw angle and this owns the wrap).
    # The bullet then flies straight under `install_update_bullet` like every other enemy bullet; the
    # arcade's separate straight `handle_07` handler collapses into that one update in this port (026).
    definition = _install_warp_proc(blocks, RADIATING_EMIT_PROCCODE)
    bindex = lambda: variable("bullet alloc result", BULLET_ALLOC_RESULT_ID)
    angle_index = lambda: blocks.op_add(
        blocks.op_mod(variable("radiating angle", RADIATING_ANGLE_ID), number(32)), number(1)
    )
    got = blocks.op_gt(variable("bullet alloc result", BULLET_ALLOC_RESULT_ID), number(0))
    placed = blocks.if_reporter(
        got,
        [
            blocks.list_replace("slot x", SLOT_X_ID, bindex(), _cur_item(blocks, "slot x", SLOT_X_ID)),
            blocks.list_replace("slot y", SLOT_Y_ID, bindex(), _cur_item(blocks, "slot y", SLOT_Y_ID)),
            blocks.list_replace("slot dx", SLOT_DX_ID, bindex(), blocks.list_item("aim dx 48", AIM_DX_48_ID, angle_index())),
            blocks.list_replace("slot dy", SLOT_DY_ID, bindex(), blocks.list_item("aim dy 48", AIM_DY_48_ID, angle_index())),
            blocks.list_replace("slot timer", SLOT_TIMER_ID, bindex(), number(0)),
            blocks.list_replace("slot code", SLOT_CODE_ID, bindex(), number(BULLET_INIT_CODE)),
            blocks.list_replace("slot flag", SLOT_FLAG_ID, bindex(), number(0)),
        ],
    )
    blocks.chain(definition, [blocks.call_proc(ALLOC_BULLET_PROCCODE, warp=True), placed])


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


def install_init_bacura(blocks: Blocks) -> None:
    # AIR-11: stamp one Bacura slab into the reserved-band slot at `slot index` (main_fn_3__init_bacura
    # 5188-5199, handle_01_Bacura 4247-4264). The arcade activates a run of the 0x10-0x1F object band as
    # _TYPE=1, then handle_01 sets _STATE=2 (active), _dX=16, _dY=0 and a RANDOM lateral column
    # (gen_random_Y_store_obj -> col 3..27, then addq #1 -> 4..28); it READS but never SETS _X, so a fresh
    # slab starts at the TOP (_X=0) and drifts down. Port: the craft-independent draw (`_draw_spawn_column`
    # exclude_craft=False) with col_offset=1 reproduces cols 4..28 exactly; the slab enters at the top row
    # (slot x=0) and drifts at BACURA_DRIFT_DX. There is no aim, no fire, no fuse, and no points (never
    # scored). Arcade _STATE=2 (active) maps to the PORT's SLOT_ACTIVE (=1), not port state 2 (SLOT_HIT).
    definition = _install_warp_proc(blocks, INIT_BACURA_PROCCODE)
    reset, draw_loop = _draw_spawn_column(blocks, exclude_craft=False, col_offset=1)
    stamp = blocks.if_reporter(
        blocks.op_eq(variable("spawn found", SPAWN_FOUND_ID), number(1)),
        [
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, number(BACURA_TYPE)),
            _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_ACTIVE)),
            # Enter from the TOP row (arcade never sets _X, so a fresh slab starts at 0) and drift down.
            _set_cur_item(blocks, "slot x", SLOT_X_ID, number(TOROID_SPAWN_ROW * SLOT_UNITS_PER_CELL)),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, number(BACURA_DRIFT_DX)),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, number(0)),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
        ],
    )
    blocks.chain(definition, [*reset, draw_loop, stamp])


def install_check_shot_bacura(blocks: Blocks) -> None:
    # WPN-01 (player.bacura-bounce #77): test the live Bacura at `slot index` against the three player-shot
    # slots and, on the first overlapping ACTIVE shot, route THAT SHOT to the bounce — the slab is never
    # touched. This is a deliberate sibling of install_check_air_hit, NOT a reuse: the air detector resolves
    # a hit (score + explosion + SHOT_SPENT), which is exactly wrong for a Bacura (indestructible, worth
    # nothing). Here the only consequence is stamping the shot slot SHOT_BOUNCE, a non-ACTIVE state the
    # blaster clone reads next iteration to reverse+animate itself before deleting (arcade check_shot_hit_
    # bacura 2583-2595 -> deactivate_shot 2557-2561: STATE=3 + BACURA_HIT_SND, Bacura untouched). Called
    # per live slab from `update bacura`; the Bacura band is otherwise absent from every hit/score sweep.
    # Window is the shadow-MSB compare with HIT_WINDOW_SHOT_BACURA (the arcade box doubled for the same
    # anti-tunneling + sprite-match reasons ratified for HIT_WINDOW_SHOT_FLYING).
    definition = _install_warp_proc(blocks, CHECK_SHOT_BACURA_PROCCODE)
    y_bias, y_width, x_bias, x_width = HIT_WINDOW_SHOT_BACURA
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
        bacura_live = blocks.op_eq(_cur_item(blocks, "slot state", SLOT_STATE_ID), number(SLOT_ACTIVE))
        # Fresh delta subtree per compare (a reporter attaches to a single parent); same orientation as
        # install_check_air_hit — slot x is the scroll axis (governed by the taller y window), slot y lateral.
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
            blocks.op_and(shot_live, bacura_live), blocks.op_and(hit_y, hit_x)
        )
        # Bounce, don't kill: mark ONLY the shot slot. No hit slot, no award, no `resolve hit`, no timer,
        # and the Bacura's own slot is left exactly as it was — it keeps drifting, indestructible.
        body.append(
            blocks.if_reporter(
                overlap,
                [blocks.list_replace("slot state", SLOT_STATE_ID, number(s), number(SHOT_BOUNCE))],
            )
        )
    blocks.chain(definition, body)


def install_update_bacura(blocks: Blocks) -> None:
    # AIR-11: advance the Bacura at `slot index` by one tick (handle_01_Bacura 4247-4264). The Bacura is
    # indestructible: this handler deliberately OMITS the CHECK_AIR_HIT call every flying family makes, so
    # no player shot ever HIT-tests it (there is no HIT state, no explosion, no score) — that omission IS
    # the shot-invulnerability. It DOES run the WPN-01 shot-bounce detector (`check shot bacura`), which
    # only marks an overlapping shot for its rebound and never touches the slab. Per tick it (1) kills the
    # craft on contact using the WIDER HIT_WINDOW_BACURA (check_bacura_hit_solvalou 2225-2237, the same
    # overlap compare as the flying check but a larger box), checked at the tick-start position; (2) marks
    # any overlapping player shot for the bounce; (3) drifts DOWN the scroll axis at BACURA_DRIFT_DX
    # (1 px/frame, dy=0); and (4) culls once it scrolls off the bottom. It enters at the top and only moves
    # down, so the bottom edge is its only exit (unlike the maneuvering flying families, no four-edge cull).
    definition = _install_warp_proc(blocks, UPDATE_BACURA_PROCCODE)
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks, HIT_WINDOW_BACURA),
        [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))],
    )
    shot_bounce = blocks.call_proc(CHECK_SHOT_BACURA_PROCCODE, warp=True)
    move = [
        _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
        _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_add(_cur_item(blocks, "slot y", SLOT_Y_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)))),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP))),
    ]
    off_bottom = blocks.op_not(blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MAX)))
    cull = blocks.if_reporter(off_bottom, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    blocks.chain(definition, [craft_hit, shot_bounce, *move, cull])


def install_pump_bacura(blocks: Blocks) -> None:
    # AIR-11 live spawn pump — one atomic pass per tick from the walk thread (after `advance area`, so
    # this tick's schedule has already loaded the counts). Two halves, faithful to the arcade's two
    # coroutines (which the port flattens into per-tick steps — Scratch has no coroutine yield):
    #
    # INC (main_fn_5__inc_num_bacura 5201-5217): while `bacura inc cnt` remains, count `one second cntr`
    # down TICK_TIMER_STEP arcade frames per tick from 60 (one arcade second); each time it reaches 0,
    # admit one slab (num_bacura += 1), spend one increment (bacura inc cnt -= 1), and reload the counter.
    # The admit is clamped to the 16-slot band: the arcade has no explicit num_bacura clamp (the band is
    # the physical limit and the real quotas stay well under it — Area 3 totals 8), so the clamp is a
    # defensive port guard that never bites under the committed schedules but keeps the init loop from
    # ever walking past the reserved band.
    #
    # INIT (main_fn_3__init_bacura 5188-5199): keep the first `num bacura` band slots populated. The arcade
    # re-asserts _TYPE=1 for those objects each pass; handle_01_Bacura inits an object exactly once (its
    # first coroutine step) then only drifts. The port reproduces that by stamping a fresh slab (init
    # bacura) ONLY into an EMPTY band slot (slot type == 0), and re-filling a slot the instant its slab has
    # drifted off and culled — a live, mid-drift slab (slot type != 0) is left untouched, never reset to
    # the top. `init bacura` operates on `slot index`, so the loop points it at each band slot in turn.
    definition = _install_warp_proc(blocks, PUMP_BACURA_PROCCODE)

    # --- inc ---
    admit = blocks.if_reporter(
        blocks.op_not(blocks.op_gt(variable("one second cntr", ONE_SECOND_CNTR_ID), number(0))),
        [
            blocks.if_reporter(
                blocks.op_lt(variable("num bacura", NUM_BACURA_ID), number(BACURA_BAND_SIZE)),
                [blocks.change_var("num bacura", NUM_BACURA_ID, 1)],
            ),
            blocks.change_var("bacura inc cnt", BACURA_INC_CNT_ID, -1),
            blocks.set_var("one second cntr", ONE_SECOND_CNTR_ID, number(BACURA_INC_PERIOD_FRAMES)),
        ],
    )
    inc = blocks.if_reporter(
        blocks.op_gt(variable("bacura inc cnt", BACURA_INC_CNT_ID), number(0)),
        [
            blocks.change_var("one second cntr", ONE_SECOND_CNTR_ID, -TICK_TIMER_STEP),
            admit,
        ],
    )

    # --- init ---
    set_seed = blocks.set_var("bacura seed slot", BACURA_SEED_SLOT_ID, number(0))
    init_loop = blocks.add(
        "control_repeat", inputs={"TIMES": variable("num bacura", NUM_BACURA_ID)}
    )
    set_slot_index = blocks.set_var_expr(
        "slot index",
        SLOT_INDEX_ID,
        blocks.op_add(number(BACURA_SLOTS[0]), variable("bacura seed slot", BACURA_SEED_SLOT_ID)),
    )
    fill = blocks.if_reporter(
        blocks.op_eq(
            blocks.list_item("slot type", SLOT_TYPE_ID, variable("slot index", SLOT_INDEX_ID)),
            number(0),
        ),
        [blocks.call_proc(INIT_BACURA_PROCCODE, warp=True)],
    )
    blocks.substack(
        init_loop,
        [set_slot_index, fill, blocks.change_var("bacura seed slot", BACURA_SEED_SLOT_ID, 1)],
    )

    blocks.chain(definition, [inc, set_seed, init_loop])


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


def install_init_zakato(blocks: Blocks) -> None:
    # AIR-07: initialize the flying slot at `slot index` as a Zakato of type `walk type` (handle_12-15
    # 3733-3859; init_teleport 3994). All four base variants share this initializer; they differ only in
    # the points stamped here and in the movement / shot trigger the update commits once the teleport-in
    # completes. The Zakato TELEPORTS in: it is stamped SLOT_TELEPORT — indestructible, since the shared
    # `check air hit` gate skips any non-ACTIVE slot (the arcade's `_STATE=3` at init_teleport 3995) — and
    # holds in place while the ~20-frame sparkle plays (rendered from `slot timer`, the reversed burst).
    # When the sparkle ends the update flips it to SLOT_ACTIVE and stamps its aimed/straight velocity and
    # shot fuse. Top-row entry via the shared spawn-column draw; the arcade's random teleport X
    # (init_teleport 3996-3999) is a deferred cosmetic deviation, the same no-enemy-scroll top entry every
    # flying family uses. No fire mask is captured — a Zakato fires exactly one bullet, structurally, not
    # under the periodic gate.
    definition = _install_warp_proc(blocks, INIT_ZAKATO_PROCCODE)
    # init_teleport draws the entry column CRAFT-INDEPENDENTLY: gen_random_Y_store_obj (5147-5154) does the
    # in-range clamp with NO craft-proximity reject, so a base Zakato CAN teleport in over/adjacent to the
    # craft's column (source wins over the earlier craft-excluding assumption). init_teleport then does
    # `add.b #1,(_Y,a5)` (4000) — the +1-cell teleport offset (col_offset=1).
    reset, draw_loop = _draw_spawn_column(blocks, exclude_craft=False, col_offset=1)
    wt = lambda: variable("walk type", WALK_TYPE_ID)
    # Per-variant points, stamped by type (the four handlers' distinct _PTS bytes -> value-table positions).
    pts_stamps = [
        blocks.if_reporter(
            blocks.op_eq(wt(), number(t)),
            [_set_cur_item(blocks, "slot pts", SLOT_PTS_ID, number(p))],
        )
        for t, p in (
            (ZAKATO_SLOW_TYPE, ZAKATO_SLOW_PTS),
            (ZAKATO_CLOSEY_TYPE, ZAKATO_CLOSEY_PTS),
            (ZAKATO_FAST_TYPE, ZAKATO_FAST_PTS),
            (ZAKATO_CONT_TYPE, ZAKATO_CONT_PTS),
        )
    ]
    stamp = blocks.if_reporter(
        blocks.op_eq(variable("spawn found", SPAWN_FOUND_ID), number(1)),
        [
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, wt()),
            # SLOT_TELEPORT: invulnerable and not yet moving — the sparkle plays in place, then the update
            # transitions to SLOT_ACTIVE. Not SLOT_ACTIVE, so `check air hit` cannot score it mid-teleport.
            _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_TELEPORT)),
            _set_cur_item(blocks, "slot x", SLOT_X_ID, number(TOROID_SPAWN_ROW * SLOT_UNITS_PER_CELL)),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, number(0)),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, number(0)),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
            _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(ZAKATO_MAIN_CODE)),
            *pts_stamps,
            # AUDIO: TELEPORT_SND on the teleport-in (src init_teleport xevious_main.68k:4004).
            blocks.play_sound("zakato"),
        ],
    )
    blocks.chain(definition, [*reset, draw_loop, stamp])


def install_update_zakato(blocks: Blocks) -> None:
    # AIR-07: advance the Zakato at `slot index` by one tick — the three-phase teleport/active/self-
    # destruct machine the arcade sequences through its per-object re-entry PC (handle_12-15 3733-3859,
    # zakato_teleport 3961, zakato_shoot 3761, zakato_explode 3931). With no per-slot PC, the port carries
    # the phase EXPLICITLY in `slot state`:
    #   SLOT_TELEPORT      teleporting in: indestructible (the shared `check air hit` gate ignores any
    #                      non-ACTIVE slot, so it is unkillable here — the arcade's `_STATE=3` at
    #                      init_teleport 3995), holding in place while the ~20-frame reversed sparkle plays.
    #   SLOT_ACTIVE        hittable and moving: it fires EXACTLY ONE aimed bullet — on a random countdown
    #                      (slow/fast) or when the craft is level in the lateral axis (closeY/cont) — then
    #                      flips itself to SLOT_SELF_EXPLODE; killed by a shot first, it scores its value.
    #   SLOT_SELF_EXPLODE  fired and vanishing: benign (again ignored by the hit gate), holding still while
    #                      its own 20-frame burst plays, then freed awarding NOTHING (zakato_explode_and_
    #                      remove 3766 -> remove_zakato 3926, no score).
    #   SLOT_HIT           shot down while active: the SHARED flying explosion (`explode toroid tick`), its
    #                      value already scored by the detector — exactly like every other flying family.
    # The self-destruct reuses `explode toroid tick` (same 20-frame free clock); it differs from a shot
    # kill only in the sprite the renderer draws (self burst vs shared burst, keyed on the state) and in
    # awarding nothing (the detector, not the tick, awards — and it never ran on a self-destructing slot).
    definition = _install_warp_proc(blocks, UPDATE_ZAKATO_PROCCODE)
    state = lambda: _cur_item(blocks, "slot state", SLOT_STATE_ID)
    wt = lambda: _cur_item(blocks, "slot type", SLOT_TYPE_ID)
    timer = lambda: _cur_item(blocks, "slot timer", SLOT_TIMER_ID)
    fuse = lambda: _cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID)
    col_offset = lambda: blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))
    is_straight = lambda: blocks.op_or(blocks.op_eq(wt(), number(ZAKATO_SLOW_TYPE)), blocks.op_eq(wt(), number(ZAKATO_CLOSEY_TYPE)))
    is_fused = lambda: blocks.op_or(blocks.op_eq(wt(), number(ZAKATO_SLOW_TYPE)), blocks.op_eq(wt(), number(ZAKATO_FAST_TYPE)))
    is_proximity = lambda: blocks.op_or(blocks.op_eq(wt(), number(ZAKATO_CLOSEY_TYPE)), blocks.op_eq(wt(), number(ZAKATO_CONT_TYPE)))

    # --- TELEPORT phase: hold in place, advance the sparkle clock; on completion commit to ACTIVE. ---
    # Straight variants (slow/closeY) descend on the raw scroll-axis velocity dX=16, dY=0 (handle_12/13
    # 3736-3737); aimed variants (fast/cont) aim at the craft's CURRENT cell on the 32-magnitude generic
    # tier at the completion instant (zakato_14/15_main's calc_dX_dY_for_vector_to_solvalou 3822/3848).
    set_straight = blocks.if_reporter(
        is_straight(),
        [
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, number(ZAKATO_STRAIGHT_DX)),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, number(0)),
        ],
    )
    set_aimed = blocks.if_reporter(
        blocks.op_not(is_straight()),
        [
            blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
            blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
            blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 32", AIM_DX_32_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 32", AIM_DY_32_ID, variable("aim index", AIM_INDEX_ID))),
        ],
    )
    # Draw the one-shot random fuse for the fused variants (slow (rng mod 256)+1, fast (rng mod 64)+1,
    # 3745-3748 / 3814-3817). The Y-triggered variants leave `slot fire timer` unused. A fresh RNG draw
    # in walk order, like the Kapi approach-delay draw.
    seed_slow_fuse = blocks.if_reporter(
        blocks.op_eq(wt(), number(ZAKATO_SLOW_TYPE)),
        [
            blocks.call_proc(RNG_PROCCODE, warp=True),
            _set_cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_add(blocks.op_mod(variable("rng out", RNG_OUT_ID), number(ZAKATO_SLOW_FUSE_SPAN)), number(1))),
        ],
    )
    seed_fast_fuse = blocks.if_reporter(
        blocks.op_eq(wt(), number(ZAKATO_FAST_TYPE)),
        [
            blocks.call_proc(RNG_PROCCODE, warp=True),
            _set_cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_add(blocks.op_mod(variable("rng out", RNG_OUT_ID), number(ZAKATO_FAST_FUSE_SPAN)), number(1))),
        ],
    )
    commit_active = [
        _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_ACTIVE)),
        _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(ZAKATO_MAIN_CODE)),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
        set_straight,
        set_aimed,
        seed_slow_fuse,
        seed_fast_fuse,
    ]
    # Advance the sparkle clock 2/tick; `timer()` re-reads the list, so the completion test sees the
    # incremented value. Completes when it reaches ZAKATO_PHASE_FRAMES (20). The commit runs the SAME tick
    # the sparkle ends, matching the arcade fall-through from zakato_teleport into zakato_NN_main; the
    # ACTIVE gate below then also runs this tick (its ==ACTIVE gate is now satisfied), as the arcade does.
    teleport = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_TELEPORT)),
        [
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, blocks.op_add(timer(), number(TICK_TIMER_STEP))),
            blocks.if_reporter(blocks.op_not(blocks.op_lt(timer(), number(ZAKATO_PHASE_FRAMES))), commit_active),
        ],
    )

    # --- ACTIVE phase: craft collision, fire-once trigger, else move + cull. ---
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks), [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))]
    )
    # Fused variants count the fuse down 2/tick (2 arcade frames/tick); the Y-triggered variants never
    # touch it. `fuse()` re-reads the list so the trigger sees the decremented value.
    dec_fuse = blocks.if_reporter(
        is_fused(),
        [_set_cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_sub(fuse(), number(TICK_TIMER_STEP)))],
    )
    # Fire trigger: fused variants at fuse <= 0 (subq/jeq 3755/3826); proximity variants when the craft is
    # level in the lateral axis, offset in [LOW, HIGH] (the carrying MSB test 3793-3796). Either way it
    # fires ONE aimed bullet then flips to SELF_EXPLODE — vanishing benign, awarding nothing.
    fired_fused = blocks.op_and(is_fused(), blocks.op_not(blocks.op_gt(fuse(), number(0))))
    in_band = blocks.op_and(
        blocks.op_not(blocks.op_lt(col_offset(), number(ZAKATO_CLOSEY_LOW))),
        blocks.op_not(blocks.op_gt(col_offset(), number(ZAKATO_CLOSEY_HIGH))),
    )
    fired_prox = blocks.op_and(is_proximity(), in_band)
    fire_now = blocks.op_or(fired_fused, fired_prox)
    # Self-destruct: fire the one bullet, flip to SELF_EXPLODE, zero the velocity (the arcade stops calling
    # move_object_dX_dY and only scroll-drifts — which this no-enemy-scroll port renders as holding still),
    # and reset the burst clock.
    on_fire = [
        *_fire_aimed_bullet(blocks),
        _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_SELF_EXPLODE)),
        _set_cur_item(blocks, "slot dx", SLOT_DX_ID, number(0)),
        _set_cur_item(blocks, "slot dy", SLOT_DY_ID, number(0)),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
    ]
    # Move by 4*velocity per tick, advance the body animation clock, then cull off any edge (the same
    # explicit four-edge cull as the other flying families).
    move = [
        _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
        _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_add(_cur_item(blocks, "slot y", SLOT_Y_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)))),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP))),
    ]
    off_bottom = blocks.op_not(blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MAX)))
    off_top = blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MIN + 1))
    off_right = blocks.op_not(blocks.op_lt(_cur_col(blocks), number(CULL_COL_MAX)))
    off_left = blocks.op_lt(_cur_col(blocks), number(CULL_COL_MIN + 1))
    offscreen = blocks.op_or(blocks.op_or(off_bottom, off_top), blocks.op_or(off_right, off_left))
    cull = blocks.if_reporter(offscreen, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    fire_choice = blocks.add("control_if_else")
    blocks.blocks[fire_now]["parent"] = fire_choice
    blocks.blocks[fire_choice]["inputs"]["CONDITION"] = [2, fire_now]
    blocks.substack(fire_choice, on_fire)
    blocks.substack(fire_choice, [*move, cull], name="SUBSTACK2")
    active = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_ACTIVE)),
        [craft_hit, dec_fuse, fire_choice],
    )

    # --- SELF_EXPLODE phase: play out the burst clock and free (no score). ---
    self_explode = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_SELF_EXPLODE)),
        [blocks.call_proc(EXPLODE_TICK_PROCCODE, warp=True)],
    )

    # Top: a shot kill (SLOT_HIT) plays the SHARED flying explosion; otherwise offer to the shot detector
    # (a no-op unless ACTIVE) then run the phase machine. The detector may flip ACTIVE->HIT this tick; the
    # ACTIVE gate re-reads state and is then skipped, so the hit explosion starts next tick (as elsewhere).
    top = blocks.add("control_if_else")
    is_hit = blocks.op_eq(state(), number(SLOT_HIT))
    blocks.blocks[top]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = top
    blocks.substack(top, [blocks.call_proc(EXPLODE_TICK_PROCCODE, warp=True)])
    blocks.substack(
        top,
        [blocks.call_proc(CHECK_AIR_HIT_PROCCODE, warp=True), teleport, active, self_explode],
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


def install_init_giddo_spario(blocks: Blocks) -> None:
    # AIR-10: initialize the flying slot at `slot index` as a Giddo Spario (handle_08_Giddo_Spario
    # 5219-5240, first-call init). Craft-INDEPENDENT random-Y draw (gen_random_Y_store_obj 5222 — the
    # in-range clamp with NO craft-proximity reject, so a Giddo CAN appear over the craft's column; source
    # wins over the earlier craft-excluding assumption), top-row entry (the shared no-enemy-scroll
    # deviation), aimed ONCE at the craft on the fast 64-magnitude tier (4 px/frame, angle_dX_dY_sheonite_tbl
    # via calc_dX_dY_for_vector_to_solvalou 5223-5224). Unlike the teleport families it calls
    # gen_random_Y_store_obj directly (no init_teleport), so it takes NO +1 offset. It captures NO fire mask
    # and seeds no fire timer: Giddo never fires. `slot timer` is the flight/burst clock.
    definition = _install_warp_proc(blocks, INIT_GIDDO_SPARIO_PROCCODE)
    reset, draw_loop = _draw_spawn_column(blocks, exclude_craft=False)  # gen_random_Y_store_obj (handle_08 5222)
    stamp = blocks.if_reporter(
        blocks.op_eq(variable("spawn found", SPAWN_FOUND_ID), number(1)),
        [
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, variable("walk type", WALK_TYPE_ID)),
            _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_ACTIVE)),
            _set_cur_item(blocks, "slot x", SLOT_X_ID, number(TOROID_SPAWN_ROW * SLOT_UNITS_PER_CELL)),
            blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
            blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
            blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 64", AIM_DX_64_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 64", AIM_DY_64_ID, variable("aim index", AIM_INDEX_ID))),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
            _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(0)),
            _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(GIDDO_SPARIO_INIT_CODE)),
            _set_cur_item(blocks, "slot pts", SLOT_PTS_ID, number(GIDDO_SPARIO_PTS)),
        ],
    )
    blocks.chain(definition, [*reset, draw_loop, stamp])


def install_explode_giddo_spario_tick(blocks: Blocks) -> None:
    # AIR-10: advance a struck Giddo Spario's OWN short burst one tick (giddo_spario_hit 5241-5253) — the
    # one documented exception to the shared ~20-frame flying burst. Like the shared burst it keeps
    # drifting on its velocity while the burst plays (the renderer maps the clock to a burst sprite), but
    # it frees after only GIDDO_SPARIO_HIT_DURATION_FRAMES (8) rather than TOROID_HIT_DURATION_FRAMES (20).
    definition = _install_warp_proc(blocks, EXPLODE_GIDDO_SPARIO_PROCCODE)
    move = [
        _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
        _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_add(_cur_item(blocks, "slot y", SLOT_Y_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)))),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP))),
    ]
    done = blocks.op_not(blocks.op_lt(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(GIDDO_SPARIO_HIT_DURATION_FRAMES)))
    free = blocks.if_reporter(done, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    blocks.chain(definition, [*move, free])


def install_update_giddo_spario(blocks: Blocks) -> None:
    # AIR-10: advance the Giddo Spario at `slot index` by one tick (handle_08_Giddo_Spario 5219-5253).
    # While ACTIVE it flies straight on its once-aimed 4 px/frame velocity (move_object_dX_dY 5238), never
    # fires, and advances its animation clock; it culls off any edge. It uses its OWN burst on death
    # (state HIT -> `explode giddo spario tick`), NOT the shared `explode toroid tick`. The same shared
    # flying-vs-craft window applies (an active Giddo on the craft's cell kills it).
    definition = _install_warp_proc(blocks, UPDATE_GIDDO_SPARIO_PROCCODE)
    state = lambda: _cur_item(blocks, "slot state", SLOT_STATE_ID)
    move = [
        _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
        _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_add(_cur_item(blocks, "slot y", SLOT_Y_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)))),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP))),
    ]
    off_bottom = blocks.op_not(blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MAX)))
    off_top = blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MIN + 1))
    off_right = blocks.op_not(blocks.op_lt(_cur_col(blocks), number(CULL_COL_MAX)))
    off_left = blocks.op_lt(_cur_col(blocks), number(CULL_COL_MIN + 1))
    offscreen = blocks.op_or(blocks.op_or(off_bottom, off_top), blocks.op_or(off_right, off_left))
    cull = blocks.if_reporter(offscreen, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks), [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))]
    )
    normal = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_ACTIVE)),
        [craft_hit, *move, cull],
    )
    top = blocks.add("control_if_else")
    is_hit = blocks.op_eq(state(), number(SLOT_HIT))
    blocks.blocks[top]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = top
    blocks.substack(top, [blocks.call_proc(EXPLODE_GIDDO_SPARIO_PROCCODE, warp=True)])
    blocks.substack(
        top,
        [blocks.call_proc(CHECK_AIR_HIT_PROCCODE, warp=True), normal],
        name="SUBSTACK2",
    )
    blocks.chain(definition, [top])


def install_init_brag_spario(blocks: Blocks) -> None:
    # AIR-10: stamp the non-kinematic fields of a Brag Spario at `slot index` (handle_09_Brag_Spario
    # first-call init 3081-3090). The Garu Zakato detonation (AIR-08, air.special-pairs) is the ONLY
    # spawner: it writes each slot's TYPE, position (copied from the Garu) and cardinal initial velocity
    # (brag_spario_dX/dY_tbl) directly, then calls this to stamp state/code/points/clock. Brag never
    # spawns from a formation wave, so there is no spawn-flying branch for it. Points 500 (no super).
    definition = _install_warp_proc(blocks, INIT_BRAG_SPARIO_PROCCODE)
    blocks.chain(definition, [
        _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_ACTIVE)),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
        _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(0)),
        _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(BRAG_SPARIO_INIT_CODE)),
        _set_cur_item(blocks, "slot pts", SLOT_PTS_ID, number(BRAG_SPARIO_PTS)),
    ])


def install_update_brag_spario(blocks: Blocks) -> None:
    # AIR-10: advance the Brag Spario at `slot index` by one tick (handle_09_Brag_Spario 3092-3121). It is
    # an accelerating homer: each tick it nudges its velocity toward the craft by +/-BRAG_SPARIO_ACCEL on
    # EACH axis — scroll axis (slot dx) by the sign of (player row - slot row), lateral (slot dy) by the
    # sign of (player col - slot col), with NO change on an axis already aligned to the craft's cell (the
    # arcade's MSB compare: jcs -2 / jeq 0 / else +2, 3095-3109). Velocity is unbounded, exactly as the
    # arcade (no clamp). Then it moves by the accumulated velocity, advances its flip-animation clock, and
    # culls off any edge. Shares the flying hit window and the shared ~20-frame burst on death.
    definition = _install_warp_proc(blocks, UPDATE_BRAG_SPARIO_PROCCODE)
    state = lambda: _cur_item(blocks, "slot state", SLOT_STATE_ID)
    row_offset = lambda: blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))
    col_offset = lambda: blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))
    # Scroll-axis acceleration: craft ahead (offset > 0) -> +accel; craft behind (offset < 0) -> -accel;
    # aligned (offset == 0) -> no change. Two guarded nudges leave the aligned case untouched.
    accel_dx_plus = blocks.if_reporter(
        blocks.op_gt(row_offset(), number(0)),
        [_set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.op_add(_cur_item(blocks, "slot dx", SLOT_DX_ID), number(BRAG_SPARIO_ACCEL)))],
    )
    accel_dx_minus = blocks.if_reporter(
        blocks.op_lt(row_offset(), number(0)),
        [_set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.op_sub(_cur_item(blocks, "slot dx", SLOT_DX_ID), number(BRAG_SPARIO_ACCEL)))],
    )
    accel_dy_plus = blocks.if_reporter(
        blocks.op_gt(col_offset(), number(0)),
        [_set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.op_add(_cur_item(blocks, "slot dy", SLOT_DY_ID), number(BRAG_SPARIO_ACCEL)))],
    )
    accel_dy_minus = blocks.if_reporter(
        blocks.op_lt(col_offset(), number(0)),
        [_set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.op_sub(_cur_item(blocks, "slot dy", SLOT_DY_ID), number(BRAG_SPARIO_ACCEL)))],
    )
    move = [
        _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
        _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_add(_cur_item(blocks, "slot y", SLOT_Y_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)))),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP))),
    ]
    off_bottom = blocks.op_not(blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MAX)))
    off_top = blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MIN + 1))
    off_right = blocks.op_not(blocks.op_lt(_cur_col(blocks), number(CULL_COL_MAX)))
    off_left = blocks.op_lt(_cur_col(blocks), number(CULL_COL_MIN + 1))
    offscreen = blocks.op_or(blocks.op_or(off_bottom, off_top), blocks.op_or(off_right, off_left))
    cull = blocks.if_reporter(offscreen, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks), [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))]
    )
    normal = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_ACTIVE)),
        [craft_hit, accel_dx_plus, accel_dx_minus, accel_dy_plus, accel_dy_minus, *move, cull],
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


def _stamp_sheonite(blocks: Blocks, slot_number: int, type_number: int, flank_sign: int) -> list[str]:
    # AIR-09: stamp one half of the Sheonite escort pair into a fixed flying slot. The arcade
    # sheonite_start (sub_2_fn_18 sub:530-537) writes only `_TYPE` (0x31 -> slot 0x3f, 0x32 -> 0x3e) and
    # clears the end-flag; each object's handler sets its own STATE/position on its first walk (main:4052
    # right / 4162 left). The port folds that first-call setup into the stamp so the slot is renderable and
    # walkable immediately: state ACTIVE (the pair is never SLOT_HIT — it is inert), phase HOME in `slot
    # flag`, velocity 0 (HOME re-aims it on its first tick), clock 0.
    #
    # PORT NECESSITY (entry position): the arcade right half enters at the fixed lateral _Y=0x30 (48 cells),
    # off the port's 0..31 lateral field (the port compresses the arcade's wider internal lateral
    # coordinate). Rather than an off-field entry that would cull instantly, the port enters each half at
    # the top row directly above its own dock cell (player col +/- flank), so the pair descends from ahead
    # and docks beside the craft. The observable behaviour (a pair homing onto the craft, docking, peeling
    # off) is preserved; the absolute arcade entry column is not portable. Recorded in the mechanics record.
    return [
        blocks.list_replace("slot type", SLOT_TYPE_ID, number(slot_number), number(type_number)),
        blocks.list_replace("slot state", SLOT_STATE_ID, number(slot_number), number(SLOT_ACTIVE)),
        blocks.list_replace("slot flag", SLOT_FLAG_ID, number(slot_number), number(SHEONITE_PHASE_HOME)),
        blocks.list_replace("slot x", SLOT_X_ID, number(slot_number), number(TOROID_SPAWN_ROW * SLOT_UNITS_PER_CELL)),
        blocks.list_replace(
            "slot y",
            SLOT_Y_ID,
            number(slot_number),
            blocks.op_mul(
                blocks.op_add(variable("player col", PLAYER_COL_ID), number(flank_sign * SHEONITE_LOCK_FLANK)),
                number(SLOT_UNITS_PER_CELL),
            ),
        ),
        blocks.list_replace("slot dx", SLOT_DX_ID, number(slot_number), number(0)),
        blocks.list_replace("slot dy", SLOT_DY_ID, number(slot_number), number(0)),
        blocks.list_replace("slot timer", SLOT_TIMER_ID, number(slot_number), number(0)),
    ]


def install_update_sheonite(blocks: Blocks) -> None:
    # AIR-09: advance the Sheonite at `slot index` by one tick — the shared home/lock/combine/exit machine
    # for BOTH the right (0x31, handle_31_right_sheonite main:4052) and left (0x32, handle_32_left_sheonite
    # main:4162) escort. The phase lives in `slot flag`; a snapshot (`sheonite phase`) taken at the top of
    # the tick drives the branch ladder, so a transition written this tick does not cascade into a later
    # branch. The proc NEVER calls CHECK_AIR_HIT and raises NO craft-hit: both arcade handlers set _STATE=3
    # (indestructible), which every hit test skips (STATE==2 gate) — the pair is wholly inert (no shot, no
    # score, no bomb, no craft-death). The omission here IS that inertness (see the type note ~:1076).
    definition = _install_warp_proc(blocks, UPDATE_SHEONITE_PROCCODE)

    # Fresh reporters per use: a reporter attaches to only one parent, so reusing one would let the second
    # consumer steal it from the first (silent empty operand). Every helper rebuilds its subtree.
    is_right = lambda: blocks.op_eq(_cur_item(blocks, "slot type", SLOT_TYPE_ID), number(RIGHT_SHEONITE_TYPE))
    phase = lambda: variable("sheonite phase", SHEONITE_PHASE_TMP_ID)
    lock_row = lambda: blocks.op_sub(variable("player row", PLAYER_ROW_ID), number(SHEONITE_LOCK_LEAD))
    lock_col = lambda: variable("sheonite lock col", SHEONITE_LOCK_COL_ID)

    def step_timer() -> str:
        return _set_cur_item(
            blocks, "slot timer", SLOT_TIMER_ID,
            blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP)),
        )

    def move_by_velocity() -> list[str]:
        return [
            _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
            _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_add(_cur_item(blocks, "slot y", SLOT_Y_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)))),
        ]

    def snap_to_lock() -> list[str]:
        # LOCK/COMBINE hold a fixed offset beside the LIVE craft, recomputed each tick (arcade
        # set_r/l_sheonite_above_solvalou main:4140/4237: (_X-0x200, _Y-/+0x200) = 2 cells ahead, 2 aside).
        return [
            _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_mul(lock_row(), number(SLOT_UNITS_PER_CELL))),
            _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_mul(lock_col(), number(SLOT_UNITS_PER_CELL))),
        ]

    # --- setup: resolve this side's lateral lock cell, then snapshot the phase ---
    lock_col_setup = blocks.add("control_if_else")
    setup_cond = is_right()
    blocks.blocks[lock_col_setup]["inputs"]["CONDITION"] = [2, setup_cond]
    blocks.blocks[setup_cond]["parent"] = lock_col_setup
    blocks.substack(
        lock_col_setup,
        [blocks.set_var_expr("sheonite lock col", SHEONITE_LOCK_COL_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), number(SHEONITE_LOCK_FLANK)))],
    )
    blocks.substack(
        lock_col_setup,
        [blocks.set_var_expr("sheonite lock col", SHEONITE_LOCK_COL_ID, blocks.op_add(variable("player col", PLAYER_COL_ID), number(SHEONITE_LOCK_FLANK)))],
        name="SUBSTACK2",
    )
    snapshot = blocks.set_var_expr("sheonite phase", SHEONITE_PHASE_TMP_ID, _cur_item(blocks, "slot flag", SLOT_FLAG_ID))

    # --- HOME: fly onto the lock target at the shared 64-magnitude aim tier (4 px/frame), re-aimed each
    # tick (main:4057-4066 / 4166-4175). Switch to LOCK once the scroll axis reaches the lock line
    # (arcade compares solvalou_X-2 == sheonite_X, main:4067-4069 / 4176-4178; port uses >= so a 4-px step
    # cannot skip the exact cell). ---
    home_aim = [
        blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(lock_row(), _cur_row(blocks))),
        blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(lock_col(), _cur_col(blocks))),
        blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
        _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 64", AIM_DX_64_ID, variable("aim index", AIM_INDEX_ID))),
        _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 64", AIM_DY_64_ID, variable("aim index", AIM_INDEX_ID))),
    ]
    reached = blocks.op_not(blocks.op_lt(_cur_row(blocks), lock_row()))  # slot row >= lock_row
    to_lock = blocks.if_reporter(reached, [_set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(SHEONITE_PHASE_LOCK))])
    home_branch = blocks.if_reporter(
        blocks.op_eq(phase(), number(SHEONITE_PHASE_HOME)),
        [*home_aim, *move_by_velocity(), step_timer(), to_lock],
    )

    # --- LOCK: hold the offset beside the live craft, advancing the spin clock. The pair only leaves LOCK
    # once sheonite_end has raised the end-flag (arcade waits on sheonite_end_flag, main:4074 / 4187); on
    # that transition reset the clock so COMBINE's dwell counts from 0. ---
    end_set = blocks.op_not(blocks.op_eq(variable("sheonite end flag", SHEONITE_END_FLAG_ID), number(0)))
    to_combine = blocks.if_reporter(
        end_set,
        [
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
            _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(SHEONITE_PHASE_COMBINE)),
        ],
    )
    lock_branch = blocks.if_reporter(
        blocks.op_eq(phase(), number(SHEONITE_PHASE_LOCK)),
        [*snap_to_lock(), step_timer(), to_combine],
    )

    # --- COMBINE: dock beside the craft for SHEONITE_COMBINE_DWELL_FRAMES (arcade r/l_sheonite_combining
    # main:4102/4215: 32 frames each side). When the dwell completes the right half retreats (arcade
    # r_sheonite_retreat main:4120: _dX=-96 -> -6 px/frame away up the scroll axis) and the left half
    # vanishes (arcade l_sheonite_remove main:4232: clears _TYPE/_STATE -> cull). ---
    combine_exit = blocks.add("control_if_else")
    exit_cond = is_right()
    blocks.blocks[combine_exit]["inputs"]["CONDITION"] = [2, exit_cond]
    blocks.blocks[exit_cond]["parent"] = combine_exit
    blocks.substack(
        combine_exit,
        [
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, number(SHEONITE_RETREAT_DX)),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, number(0)),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
            _set_cur_item(blocks, "slot flag", SLOT_FLAG_ID, number(SHEONITE_PHASE_RETREAT)),
            # AUDIO: SHEONITE_SND on the RIGHT half's retreat only (src r_sheonite_retreat
            # xevious_main.68k:4128, after _dX=0xFFA0). The left half (SUBSTACK2) vanishes silently
            # (l_sheonite_remove main:4232 plays no sound).
            blocks.play_sound("sheonite"),
        ],
    )
    blocks.substack(combine_exit, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)], name="SUBSTACK2")
    dwell_done = blocks.op_not(blocks.op_lt(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(SHEONITE_COMBINE_DWELL_FRAMES)))
    combine_done = blocks.if_reporter(dwell_done, [combine_exit])
    combine_branch = blocks.if_reporter(
        blocks.op_eq(phase(), number(SHEONITE_PHASE_COMBINE)),
        [*snap_to_lock(), step_timer(), combine_done],
    )

    # --- RETREAT (right only): drift up the scroll axis on the retreat velocity until it clears the top of
    # the field, then cull. The left half never reaches this phase (it culls at combine end). ---
    retreat_move = [
        _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
    ]
    off_top = blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MIN + 1))
    retreat_cull = blocks.if_reporter(off_top, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    retreat_branch = blocks.if_reporter(
        blocks.op_eq(phase(), number(SHEONITE_PHASE_RETREAT)),
        [*retreat_move, step_timer(), retreat_cull],
    )

    blocks.chain(definition, [lock_col_setup, snapshot, home_branch, lock_branch, combine_branch, retreat_branch])


def install_init_brag_zakato(blocks: Blocks) -> None:
    # AIR-08: shared teleport-in init for the two Brag Zakato variants (handle_16/17 3863/3893, via the
    # shared init_teleport 3994). Identical to the base Zakato init — same ~20-frame teleport-in sparkle,
    # indestructible (SLOT_TELEPORT) while it plays — except for the points and the type stamped. The
    # update then aims it, drives its terminal fan trigger (random fuse for rnd / level-in-Y for closeY)
    # and its self-destruct. Top-row entry via the shared spawn column, exactly as install_init_zakato:
    # CRAFT-INDEPENDENT draw (gen_random_Y_store_obj, no craft reject) plus the +1-cell teleport offset
    # (init_teleport 4000). The arcade's random teleport X (init_teleport 3996-3999) is the same deferred
    # no-enemy-scroll cosmetic every ported flying family shares.
    definition = _install_warp_proc(blocks, INIT_BRAG_ZAKATO_PROCCODE)
    reset, draw_loop = _draw_spawn_column(blocks, exclude_craft=False, col_offset=1)  # mirrors install_init_zakato
    wt = lambda: variable("walk type", WALK_TYPE_ID)
    pts_stamps = [
        blocks.if_reporter(
            blocks.op_eq(wt(), number(t)),
            [_set_cur_item(blocks, "slot pts", SLOT_PTS_ID, number(p))],
        )
        for t, p in (
            (BRAG_ZAKATO_RND_TYPE, BRAG_ZAKATO_RND_PTS),
            (BRAG_ZAKATO_CLOSEY_TYPE, BRAG_ZAKATO_CLOSEY_PTS),
        )
    ]
    stamp = blocks.if_reporter(
        blocks.op_eq(variable("spawn found", SPAWN_FOUND_ID), number(1)),
        [
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, wt()),
            _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_TELEPORT)),
            _set_cur_item(blocks, "slot x", SLOT_X_ID, number(TOROID_SPAWN_ROW * SLOT_UNITS_PER_CELL)),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, number(0)),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, number(0)),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
            _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(BRAG_ZAKATO_MAIN_CODE)),
            *pts_stamps,
            # AUDIO: TELEPORT_SND on the teleport-in (src init_teleport xevious_main.68k:4004),
            # shared with the base Zakato — same teleport cue.
            blocks.play_sound("zakato"),
        ],
    )
    blocks.chain(definition, [*reset, draw_loop, stamp])


def install_update_brag_zakato(blocks: Blocks) -> None:
    # AIR-08: advance the Brag Zakato at `slot index` by one tick — the same teleport/active/self-destruct
    # machine as the base Zakato (handle_16/17 3863-3924), differing in two ways: (1) BOTH variants aim at
    # the craft on the 32-magnitude tier when the teleport completes (there is no straight Brag), and (2)
    # they end with a 5-bullet aimed radiating FAN (brag_zakato_shoot) instead of a single aimed bullet.
    # The rnd variant fires on a 1-64 random fuse (brag_zakato_16_main 3872); the closeY variant when the
    # craft is level in the lateral axis, the same [-4, 3] cell band as the base closeY (3911-3915). Either
    # way it fires the fan, flips to SELF_EXPLODE (benign, the hit gate ignores it) and plays out the
    # shared ~20-frame burst before it frees, awarding nothing (brag_zakato_explode 3920 -> the shared
    # zakato_explode_and_remove). Shot while active, it scores its value on the shared flying kill.
    definition = _install_warp_proc(blocks, UPDATE_BRAG_ZAKATO_PROCCODE)
    state = lambda: _cur_item(blocks, "slot state", SLOT_STATE_ID)
    wt = lambda: _cur_item(blocks, "slot type", SLOT_TYPE_ID)
    timer = lambda: _cur_item(blocks, "slot timer", SLOT_TIMER_ID)
    fuse = lambda: _cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID)
    col_offset = lambda: blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))
    is_fused = lambda: blocks.op_eq(wt(), number(BRAG_ZAKATO_RND_TYPE))
    is_proximity = lambda: blocks.op_eq(wt(), number(BRAG_ZAKATO_CLOSEY_TYPE))

    # --- TELEPORT phase: hold in place, advance the sparkle clock; on completion aim + commit to ACTIVE. ---
    set_aimed = [
        blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
        blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
        blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
        _set_cur_item(blocks, "slot dx", SLOT_DX_ID, blocks.list_item("aim dx 32", AIM_DX_32_ID, variable("aim index", AIM_INDEX_ID))),
        _set_cur_item(blocks, "slot dy", SLOT_DY_ID, blocks.list_item("aim dy 32", AIM_DY_32_ID, variable("aim index", AIM_INDEX_ID))),
    ]
    # The rnd variant draws a 1-64 fuse on teleport completion ((rng mod 64)+1, 3874-3875); the closeY
    # variant leaves `slot fire timer` unused (it fires on the proximity test).
    seed_rnd_fuse = blocks.if_reporter(
        is_fused(),
        [
            blocks.call_proc(RNG_PROCCODE, warp=True),
            _set_cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_add(blocks.op_mod(variable("rng out", RNG_OUT_ID), number(BRAG_ZAKATO_RND_FUSE_SPAN)), number(1))),
        ],
    )
    commit_active = [
        _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_ACTIVE)),
        _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(BRAG_ZAKATO_MAIN_CODE)),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
        *set_aimed,
        seed_rnd_fuse,
    ]
    teleport = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_TELEPORT)),
        [
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, blocks.op_add(timer(), number(TICK_TIMER_STEP))),
            blocks.if_reporter(blocks.op_not(blocks.op_lt(timer(), number(ZAKATO_PHASE_FRAMES))), commit_active),
        ],
    )

    # --- ACTIVE phase: craft collision, terminal-fan trigger, else move + cull. ---
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks), [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))]
    )
    dec_fuse = blocks.if_reporter(
        is_fused(),
        [_set_cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_sub(fuse(), number(TICK_TIMER_STEP)))],
    )
    fired_fused = blocks.op_and(is_fused(), blocks.op_not(blocks.op_gt(fuse(), number(0))))
    in_band = blocks.op_and(
        blocks.op_not(blocks.op_lt(col_offset(), number(ZAKATO_CLOSEY_LOW))),
        blocks.op_not(blocks.op_gt(col_offset(), number(ZAKATO_CLOSEY_HIGH))),
    )
    fired_prox = blocks.op_and(is_proximity(), in_band)
    fire_now = blocks.op_or(fired_fused, fired_prox)
    # Self-destruct: fire the 5-bullet aimed fan, flip to SELF_EXPLODE, zero the velocity, reset the burst
    # clock. brag_zakato_shoot reads `slot index` (still this Brag) for the firing cell, so it runs first.
    on_fire = [
        blocks.call_proc(BRAG_ZAKATO_SHOOT_PROCCODE, warp=True),
        _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_SELF_EXPLODE)),
        _set_cur_item(blocks, "slot dx", SLOT_DX_ID, number(0)),
        _set_cur_item(blocks, "slot dy", SLOT_DY_ID, number(0)),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
    ]
    move = [
        _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
        _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_add(_cur_item(blocks, "slot y", SLOT_Y_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)))),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP))),
    ]
    off_bottom = blocks.op_not(blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MAX)))
    off_top = blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MIN + 1))
    off_right = blocks.op_not(blocks.op_lt(_cur_col(blocks), number(CULL_COL_MAX)))
    off_left = blocks.op_lt(_cur_col(blocks), number(CULL_COL_MIN + 1))
    offscreen = blocks.op_or(blocks.op_or(off_bottom, off_top), blocks.op_or(off_right, off_left))
    cull = blocks.if_reporter(offscreen, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    fire_choice = blocks.add("control_if_else")
    blocks.blocks[fire_now]["parent"] = fire_choice
    blocks.blocks[fire_choice]["inputs"]["CONDITION"] = [2, fire_now]
    blocks.substack(fire_choice, on_fire)
    blocks.substack(fire_choice, [*move, cull], name="SUBSTACK2")
    active = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_ACTIVE)),
        [craft_hit, dec_fuse, fire_choice],
    )

    # --- SELF_EXPLODE phase: play out the shared burst clock and free (no score). ---
    self_explode = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_SELF_EXPLODE)),
        [blocks.call_proc(EXPLODE_TICK_PROCCODE, warp=True)],
    )

    top = blocks.add("control_if_else")
    is_hit = blocks.op_eq(state(), number(SLOT_HIT))
    blocks.blocks[top]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = top
    blocks.substack(top, [blocks.call_proc(EXPLODE_TICK_PROCCODE, warp=True)])
    blocks.substack(
        top,
        [blocks.call_proc(CHECK_AIR_HIT_PROCCODE, warp=True), teleport, active, self_explode],
        name="SUBSTACK2",
    )
    blocks.chain(definition, [top])


def install_brag_zakato_shoot(blocks: Blocks) -> None:
    # AIR-08: the terminal 5-bullet aimed radiating fan (brag_zakato_shoot 5054). Aim at the craft's
    # current cell, convert the aim angle to a radiating index — the arcade `sub #32 / ror.b #3 / and
    # #0x1f`, i.e. ((aim base - 32) mod 256) >> 3 masked to 0..31 — then emit 5 bullets two angle-steps
    # apart via the shared radiating emitter (48-magnitude, 3 px/frame). `slot index` (the firing Brag) is
    # preserved across the emits (the emitter uses its own bullet cursor), so all 5 leave the Brag's cell.
    definition = _install_warp_proc(blocks, BRAG_ZAKATO_SHOOT_PROCCODE)
    set_diffs = [
        blocks.set_var_expr("aim dx diff", AIM_DX_DIFF_ID, blocks.op_sub(variable("player row", PLAYER_ROW_ID), _cur_row(blocks))),
        blocks.set_var_expr("aim dy diff", AIM_DY_DIFF_ID, blocks.op_sub(variable("player col", PLAYER_COL_ID), _cur_col(blocks))),
        blocks.call_proc(COMPUTE_AIM_PROCCODE, warp=True),
    ]
    # radiating base = floor(((aim base - 32) mod 256) / 8) mod 32.
    base_index = blocks.op_mod(
        blocks.op_floor(
            blocks.op_div(
                blocks.op_mod(
                    blocks.op_add(blocks.op_sub(variable("aim base", AIM_BASE_ID), number(BRAG_ZAKATO_FAN_BASE_BIAS)), number(256)),
                    number(256),
                ),
                number(8),
            )
        ),
        number(32),
    )
    set_angle = blocks.set_var_expr("radiating angle", RADIATING_ANGLE_ID, base_index)
    loop = blocks.add("control_repeat", inputs={"TIMES": number(BRAG_ZAKATO_FAN_COUNT)})
    blocks.substack(
        loop,
        [
            blocks.call_proc(RADIATING_EMIT_PROCCODE, warp=True),
            blocks.change_var("radiating angle", RADIATING_ANGLE_ID, BRAG_ZAKATO_FAN_STEP),
        ],
    )
    blocks.chain(definition, [*set_diffs, set_angle, loop])


def install_init_garu_zakato(blocks: Blocks) -> None:
    # AIR-08: initialize the flying slot at `slot index` as a Garu Zakato (handle_18 4010). Unlike the
    # teleporting Zakato/Brag it enters IMMEDIATELY ACTIVE (no sparkle, hittable at once) at a RANDOM
    # lateral column (gen_random_Y_store_obj, the no-craft-reject draw), and flies straight down the
    # scroll axis at 3 px/frame (dX=48, dY=0). It draws a 32-63 fuse into `slot fire timer` at spawn; the
    # update counts it down and — if the Garu is not shot first — detonates. Points 1,000.
    definition = _install_warp_proc(blocks, INIT_GARU_ZAKATO_PROCCODE)
    reset, draw_loop = _draw_spawn_column(blocks, exclude_craft=False)  # gen_random_Y_store_obj (handle_18 4013)
    stamp = blocks.if_reporter(
        blocks.op_eq(variable("spawn found", SPAWN_FOUND_ID), number(1)),
        [
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, number(GARU_ZAKATO_TYPE)),
            _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(SLOT_ACTIVE)),
            _set_cur_item(blocks, "slot x", SLOT_X_ID, number(TOROID_SPAWN_ROW * SLOT_UNITS_PER_CELL)),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, number(GARU_STRAIGHT_DX)),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, number(0)),
            _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, number(0)),
            _set_cur_item(blocks, "slot code", SLOT_CODE_ID, number(GARU_ZAKATO_MAIN_CODE)),
            _set_cur_item(blocks, "slot pts", SLOT_PTS_ID, number(GARU_ZAKATO_PTS)),
            blocks.call_proc(RNG_PROCCODE, warp=True),
            _set_cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_add(blocks.op_mod(variable("rng out", RNG_OUT_ID), number(GARU_ZAKATO_FUSE_SPAN)), number(GARU_ZAKATO_FUSE_OFFSET))),
        ],
    )
    blocks.chain(definition, [*reset, draw_loop, stamp])


def install_update_garu_zakato(blocks: Blocks) -> None:
    # AIR-08: advance the Garu Zakato at `slot index` by one tick (handle_18 4022-4029). No teleport / no
    # self-destruct phase — it is ACTIVE from spawn. Each tick: if shot (SLOT_HIT) it plays the SHARED
    # flying explosion (its value already scored by the detector); otherwise it counts its fuse down and,
    # at zero, DETONATES (a 16-bullet ring + 4 Brag Sparios) and VANISHES with no burst and no score
    # (garu_zakato_explode 4031 clr TYPE/STATE). Not yet detonating, it moves straight and culls off-edge.
    definition = _install_warp_proc(blocks, UPDATE_GARU_ZAKATO_PROCCODE)
    state = lambda: _cur_item(blocks, "slot state", SLOT_STATE_ID)
    fuse = lambda: _cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID)
    craft_hit = blocks.if_reporter(
        _craft_overlap_reporter(blocks), [blocks.set_var("player hit", PLAYER_HIT_ID, number(1))]
    )
    dec_fuse = _set_cur_item(blocks, "slot fire timer", SLOT_FIRE_TIMER_ID, blocks.op_sub(fuse(), number(TICK_TIMER_STEP)))
    move = [
        _set_cur_item(blocks, "slot x", SLOT_X_ID, blocks.op_add(_cur_item(blocks, "slot x", SLOT_X_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dx", SLOT_DX_ID)))),
        _set_cur_item(blocks, "slot y", SLOT_Y_ID, blocks.op_add(_cur_item(blocks, "slot y", SLOT_Y_ID), blocks.op_mul(number(TICK_VELOCITY_SCALE), _cur_item(blocks, "slot dy", SLOT_DY_ID)))),
        _set_cur_item(blocks, "slot timer", SLOT_TIMER_ID, blocks.op_add(_cur_item(blocks, "slot timer", SLOT_TIMER_ID), number(TICK_TIMER_STEP))),
    ]
    off_bottom = blocks.op_not(blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MAX)))
    off_top = blocks.op_lt(_cur_row(blocks), number(CULL_ROW_MIN + 1))
    off_right = blocks.op_not(blocks.op_lt(_cur_col(blocks), number(CULL_COL_MAX)))
    off_left = blocks.op_lt(_cur_col(blocks), number(CULL_COL_MIN + 1))
    offscreen = blocks.op_or(blocks.op_or(off_bottom, off_top), blocks.op_or(off_right, off_left))
    cull = blocks.if_reporter(offscreen, [blocks.call_proc(CULL_SLOT_PROCCODE, warp=True)])
    # Fuse elapsed? detonate (which frees this slot); else move + cull. `fuse()` re-reads the list after
    # the decrement, so the test sees the decremented value (subq then jeq, 4025-4026).
    fuse_choice = blocks.add("control_if_else")
    fuse_done = blocks.op_not(blocks.op_gt(fuse(), number(0)))
    blocks.blocks[fuse_choice]["inputs"]["CONDITION"] = [2, fuse_done]
    blocks.blocks[fuse_done]["parent"] = fuse_choice
    blocks.substack(fuse_choice, [blocks.call_proc(GARU_ZAKATO_DETONATE_PROCCODE, warp=True)])
    blocks.substack(fuse_choice, [*move, cull], name="SUBSTACK2")
    active = blocks.if_reporter(
        blocks.op_eq(state(), number(SLOT_ACTIVE)),
        [craft_hit, dec_fuse, fuse_choice],
    )
    top = blocks.add("control_if_else")
    is_hit = blocks.op_eq(state(), number(SLOT_HIT))
    blocks.blocks[top]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = top
    blocks.substack(top, [blocks.call_proc(EXPLODE_TICK_PROCCODE, warp=True)])
    blocks.substack(
        top,
        [blocks.call_proc(CHECK_AIR_HIT_PROCCODE, warp=True), active],
        name="SUBSTACK2",
    )
    blocks.chain(definition, [top])


def install_garu_zakato_detonate(blocks: Blocks) -> None:
    # AIR-08: the Garu detonation (garu_zakato_explode 4031 -> init_garu_zakato_explosion 5075). Called
    # with `slot index` = the detonating Garu. First emit a 16-bullet 360-degree ring (even angles
    # 0,2,..,30) from the Garu's cell via the shared radiating emitter; then spawn 4 Brag Sparios into the
    # 4 flying slots ADJACENT to the Garu (the arcade clobbers obj 0x3C-0x3F, the 4 objects after the Garu
    # at 0x3B) at the Garu's cell with the cardinal velocities from brag_spario_dX/dY_tbl; then FREE the
    # Garu slot (the arcade clr TYPE/STATE — no self-burst, no score). The final restore of `slot index`
    # to the Garu's own slot both frees it AND restores the advance-slots loop cursor. CONTRACT: the Garu
    # occupies the FIRST flying slot (its only spawner, the debug key, stamps it there), so its 4
    # successors lie in the flying band; the natural add_object spawn (deferred follow-up) must preserve
    # that placement. DEVIATION: the 4 Sparios update once on the tick they spawn (the walk reaches their
    # higher slot indices later this same pass) — a one-tick head start, recorded in the mechanics note.
    definition = _install_warp_proc(blocks, GARU_ZAKATO_DETONATE_PROCCODE)
    gslot = lambda: variable("garu det slot", GARU_DET_SLOT_ID)
    gx = lambda: variable("garu det x", GARU_DET_X_ID)
    gy = lambda: variable("garu det y", GARU_DET_Y_ID)
    capture = [
        blocks.set_var("garu det slot", GARU_DET_SLOT_ID, variable("slot index", SLOT_INDEX_ID)),
        blocks.set_var_expr("garu det x", GARU_DET_X_ID, _cur_item(blocks, "slot x", SLOT_X_ID)),
        blocks.set_var_expr("garu det y", GARU_DET_Y_ID, _cur_item(blocks, "slot y", SLOT_Y_ID)),
    ]
    # Ring: 16 bullets at even angles 0,2,..,30. `slot index` is still the Garu, so the emitter copies its
    # cell into each ring bullet.
    ring = blocks.add("control_repeat", inputs={"TIMES": number(GARU_RING_COUNT)})
    blocks.substack(
        ring,
        [
            blocks.call_proc(RADIATING_EMIT_PROCCODE, warp=True),
            blocks.change_var("radiating angle", RADIATING_ANGLE_ID, GARU_RING_STEP),
        ],
    )
    set_ring_angle = blocks.set_var("radiating angle", RADIATING_ANGLE_ID, number(0))
    # Spawn the 4 Brag Sparios into the adjacent slots (gslot+1 .. gslot+4): repoint `slot index`, copy
    # the Garu's cell, set the cardinal velocity + type, then stamp state/code/points via the shared init.
    spawn_body: list[str] = []
    for k in range(GARU_SPARIO_COUNT):
        spawn_body += [
            blocks.set_var_expr("slot index", SLOT_INDEX_ID, blocks.op_add(gslot(), number(k + 1))),
            _set_cur_item(blocks, "slot x", SLOT_X_ID, gx()),
            _set_cur_item(blocks, "slot y", SLOT_Y_ID, gy()),
            _set_cur_item(blocks, "slot dx", SLOT_DX_ID, number(BRAG_SPARIO_SPAWN_DX[k])),
            _set_cur_item(blocks, "slot dy", SLOT_DY_ID, number(BRAG_SPARIO_SPAWN_DY[k])),
            _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, number(BRAG_SPARIO_TYPE)),
            blocks.call_proc(INIT_BRAG_SPARIO_PROCCODE, warp=True),
        ]
    # Free the Garu slot AND restore the loop cursor to it.
    free = [
        blocks.set_var("slot index", SLOT_INDEX_ID, gslot()),
        _set_cur_item(blocks, "slot type", SLOT_TYPE_ID, number(0)),
        _set_cur_item(blocks, "slot state", SLOT_STATE_ID, number(0)),
    ]
    # AUDIO: GARU_ZAKATO_SND on the detonation (src garu_zakato_explode xevious_main.68k:4033).
    detonate_sound = blocks.play_sound("garu_zakato")
    blocks.chain(definition, [*capture, detonate_sound, set_ring_angle, ring, *spawn_body, *free])


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
    # AIR-07: all four base Zakato object types (slow 0x12 / closeY 0x13 / fast 0x14 / cont 0x15) run the
    # SAME shared initializer (they teleport in identically; they differ only in the update's movement and
    # fire trigger). One OR branch of four, as the dispatch ORs them.
    spawn_zakato = blocks.if_reporter(
        blocks.op_or(
            blocks.op_or(
                blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZAKATO_SLOW_TYPE)),
                blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZAKATO_CLOSEY_TYPE)),
            ),
            blocks.op_or(
                blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZAKATO_FAST_TYPE)),
                blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(ZAKATO_CONT_TYPE)),
            ),
        ),
        [blocks.call_proc(INIT_ZAKATO_PROCCODE, warp=True)],
    )
    # AIR-10: Giddo Spario runs its own initializer (aim-once at the 64 tier). Brag Spario has no
    # formation entry — it never spawns from a wave, only from the Garu detonation (air.special-pairs) —
    # so it needs no spawn branch here; only its update and init proc (called by that detonation) exist.
    spawn_giddo_spario = blocks.if_reporter(
        blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(GIDDO_SPARIO_TYPE)),
        [blocks.call_proc(INIT_GIDDO_SPARIO_PROCCODE, warp=True)],
    )
    # AIR-08: both Brag Zakato variants (rnd 0x16 / closeY 0x17) run the SAME shared teleport-in init
    # (they differ only in the update's fan trigger). One OR branch, as the dispatch ORs them. The Garu
    # Zakato has NO formation entry — it is absent from the flying type table (its only arcade spawn is
    # the area add_object schedule, not yet consumed by the port) — so it has no spawn-flying branch; the
    # debug key stamps it directly for playtesting (a documented deferred follow-up for natural spawn).
    spawn_brag_zakato = blocks.if_reporter(
        blocks.op_or(
            blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(BRAG_ZAKATO_RND_TYPE)),
            blocks.op_eq(variable("walk type", WALK_TYPE_ID), number(BRAG_ZAKATO_CLOSEY_TYPE)),
        ),
        [blocks.call_proc(INIT_BRAG_ZAKATO_PROCCODE, warp=True)],
    )
    bounds_gate = blocks.if_reporter(in_bounds, [set_type, spawn_toroid, spawn_kapi, spawn_torkan, spawn_terrazi, spawn_zoshi_top, spawn_zoshi_bottom, spawn_zoshi_rnd, spawn_jara, spawn_zakato, spawn_giddo_spario, spawn_brag_zakato])
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
    # AIR-11: also wait on the Bacura band (17-32). The Bacura lives in its OWN band, not the flying pool,
    # so without this the cursor would flash past the Bacura entry while a slab is still drifting (the same
    # family of trap as the PR-A homer stall). A live Bacura is invulnerable and self-culls off the bottom,
    # so this is a BOUNDED wait, not a permanent stall. The `clear` step below deliberately does NOT wipe
    # the band — the slab is left to drift off on its own (which also lets the operator exercise the #77
    # shot-bounce on it in isolation before it leaves).
    for slot in range(BACURA_SLOTS[0], BACURA_SLOTS[1] + 1):
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
    # AIR-08: the Garu Zakato is not in the flying type table, so the formation spawner (which runs right
    # after this) cannot bring it in — its DEBUG_SPAWN_FAMILIES count is 0. Stamp it directly into the
    # first flying slot instead (INIT_GARU_ZAKATO draws its own random lateral column), so holding T shows
    # a solo Garu that flies straight and detonates. Guarded on its family index; runs on the fresh spawn
    # only, before the index advances. This is the same standing reachability tool the base slow/closeY
    # Zakato lean on (debug-key-only until later areas are wired); the natural add_object spawn (areas
    # 9/10/14) is a documented follow-up.
    garu_debug_index = next(
        index for index, (family_type, _offset, _count) in enumerate(DEBUG_SPAWN_FAMILIES) if family_type == GARU_ZAKATO_TYPE
    )
    garu_stamp = blocks.if_reporter(
        blocks.op_eq(variable("debug spawn index", DEBUG_SPAWN_INDEX_ID), number(garu_debug_index)),
        [
            blocks.set_var("slot index", SLOT_INDEX_ID, number(FLYING_SLOTS[0])),
            blocks.call_proc(INIT_GARU_ZAKATO_PROCCODE, warp=True),
        ],
    )
    # AIR-11: the Bacura is likewise not in the flying type table (it has its own band, spawned live by the
    # area schedule), so its DEBUG_SPAWN_FAMILIES count is 0 and the formation spawner brings it in nothing.
    # Stamp one slab directly into the FIRST BACURA-band slot instead (INIT_BACURA draws its own random
    # lateral column and enters at the top), so holding T shows a solo slab that drifts down and can't be
    # destroyed. Guarded on its family index; runs on the fresh spawn only, before the index advances.
    bacura_debug_index = next(
        index for index, (family_type, _offset, _count) in enumerate(DEBUG_SPAWN_FAMILIES) if family_type == BACURA_TYPE
    )
    bacura_stamp = blocks.if_reporter(
        blocks.op_eq(variable("debug spawn index", DEBUG_SPAWN_INDEX_ID), number(bacura_debug_index)),
        [
            blocks.set_var("slot index", SLOT_INDEX_ID, number(BACURA_SLOTS[0])),
            blocks.call_proc(INIT_BACURA_PROCCODE, warp=True),
        ],
    )
    # AIR-09: the Sheonite is a schedule-spawned PAIR, not a formation type, so its count is 0 and the
    # formation spawner brings it in nothing. Stamp BOTH halves directly (right into 0x3f, left into 0x3e)
    # — the same slots the natural sheonite_start uses — so holding T shows the escort pair on demand. The
    # debug stamp ALSO pre-arms the end-flag (=1) so the pair completes its whole lifecycle and self-culls
    # (dock -> right retreats off the top, left vanishes); without it the pair would lock beside the craft
    # forever and stall the T cursor (the PR-A homer-stall trap). The `clear` step above has already wiped
    # the flying slots this fresh-spawn tick, so the two stamps land in freshly-empty slots. Guarded on the
    # right family index; runs on the fresh spawn only, before the index advances.
    sheonite_debug_index = next(
        index for index, (family_type, _offset, _count) in enumerate(DEBUG_SPAWN_FAMILIES) if family_type == RIGHT_SHEONITE_TYPE
    )
    sheonite_stamp = blocks.if_reporter(
        blocks.op_eq(variable("debug spawn index", DEBUG_SPAWN_INDEX_ID), number(sheonite_debug_index)),
        [
            *_stamp_sheonite(blocks, SHEONITE_RIGHT_SLOT, RIGHT_SHEONITE_TYPE, -1),
            *_stamp_sheonite(blocks, SHEONITE_LEFT_SLOT, LEFT_SHEONITE_TYPE, +1),
            blocks.set_var("sheonite end flag", SHEONITE_END_FLAG_ID, number(1)),
        ],
    )
    blocks.substack(
        branch,
        [*clear, *set_count, garu_stamp, bacura_stamp, sheonite_stamp, advance_index],
        name="SUBSTACK2",
    )
    blocks.substack(gate, [*set_offset, branch])
    blocks.chain(definition, [gate])


def install_debug_ground_spawn(blocks: Blocks) -> None:
    # ENGINE-TODO(#119): remove this temporary debug ground key (and its locked-spec control-mapping amendment)
    # once every ground family is built and playtested, so reachability no longer needs it.
    # DEBUG / TEMPORARY (tracked for removal): the ground analog of the T key. Ground objects only enter by
    # scrolling up from the area schedule — a narrow, one-shot, non-repeatable window — so a specific ground
    # family (a five-slot Boza composite especially) is impractical to reach for a bomb test. While the debug
    # ground key (G) is held, CYCLE through the built ground families ONE AT A TIME: each tick, if any ground
    # slot is occupied, stamp nothing (let the current family scroll down / crater / cull); otherwise clear the
    # ground band, stamp the CURRENT family (`debug ground index` selects the DEBUG_GROUND_FAMILIES entry) at the
    # band base in a central lateral column via the SHARED seed builders, and ADVANCE the index (mod len) so the
    # next fresh spawn is the next family — holding G walks Barra -> ... -> Boza -> (wrap). It self-gates on the
    # key, so normal play is untouched when G is not held. Called in the walk AFTER the ground walk (so the
    # field-empty gate reads the fully-settled post-cull band) and outside the ADVANCE_AREA -> ADVANCE_SLOTS
    # pair the area clock requires stay adjacent; a fresh stamp scrolls on the NEXT walk (an immaterial one-tick
    # delay for a top-of-field spawn) and then travels toward the craft to be bombed. It defers to any scheduled
    # ground object (only fills a genuinely empty field). Ground objects always scroll down and cull off the
    # field (a crater too), so the "let it live" wait is BOUNDED — the cursor never stalls. Reachability recurs for every
    # future ground family (each just appends one DEBUG_GROUND_FAMILIES entry, no new key), so this stays a dev
    # tool until they are all built and playtested, then it is removed (it amends the locked control mapping —
    # see core-game-systems.md and issue #119).
    definition = _install_warp_proc(blocks, DEBUG_GROUND_SPAWN_PROCCODE)
    gate = blocks.add("control_if")
    pressed = blocks.key_pressed(gate, DEBUG_GROUND_KEY)
    blocks.blocks[gate]["inputs"]["CONDITION"] = [2, pressed]

    # Any ground object already on the field? (OR over the whole ground band — family-agnostic, so whatever
    # family is spawned lives out its scroll/crater before the next arrives. A bombed family keeps a non-zero
    # `slot type` while its crater scrolls, so it too holds the cursor until it culls — a bounded wait.)
    present = None
    for slot in range(GROUND_SLOTS[0], GROUND_SLOTS[1] + 1):
        occupied = blocks.op_not(
            blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, number(slot)), number(0))
        )
        present = occupied if present is None else blocks.op_or(present, occupied)
    field_empty = blocks.op_not(present)

    # Field empty: free the whole ground band the same way `cull slot` does — BOTH `slot type` and `slot state`
    # to 0 — so no slot is left type-empty but state-stale (a half-freed slot the walk could misread). The
    # field-empty gate means nothing live is wiped; this is belt-and-suspenders against a stale state byte,
    # matching the T-key tool.
    clear = [
        block
        for slot in range(GROUND_SLOTS[0], GROUND_SLOTS[1] + 1)
        for block in (
            blocks.list_replace("slot type", SLOT_TYPE_ID, number(slot), number(0)),
            blocks.list_replace("slot state", SLOT_STATE_ID, number(slot), number(0)),
        )
    ]
    # One stamp branch per family, guarded on the current index; exactly one runs on a fresh spawn. Each uses the
    # SAME seed builders as the schedule ingest (via _debug_ground_seed), so the debug spawn is faithful.
    stamps = [
        blocks.if_reporter(
            blocks.op_eq(variable("debug ground index", DEBUG_GROUND_INDEX_ID), number(index)),
            _debug_ground_seed(blocks, family_type, shape),
        )
        for index, (family_type, shape) in enumerate(DEBUG_GROUND_FAMILIES)
    ]
    advance_index = blocks.set_var_expr(
        "debug ground index",
        DEBUG_GROUND_INDEX_ID,
        blocks.op_mod(
            blocks.op_add(variable("debug ground index", DEBUG_GROUND_INDEX_ID), number(1)),
            number(len(DEBUG_GROUND_FAMILIES)),
        ),
    )
    spawn = blocks.if_reporter(field_empty, [*clear, *stamps, advance_index])
    # ISOLATION (parity with the T key): while G is held, suppress the normal enemy stream so ONLY the debug
    # ground family is on screen — otherwise the operator cannot focus on the family under test. Three sources
    # feed the field, so all three are stopped while G is held: (a) the flying formation spawner — zero
    # `formation count` (SPAWN_FLYING runs right after this in the walk and brings in nothing) and clear the
    # flying band so any in-flight wave vanishes; (b) the Bacura pump — its walk call is gated on G-not-held
    # (see the tick loop), and the band is cleared here so any drifting slab goes; (c) the area schedule's own
    # add_ground_object stamps — gated on G-not-held in `_consume_schedule`, so the debug family is the sole
    # ground object. The clears drop live enemies with no explosion or score, the intended cost of the
    # one-at-a-time isolation (the checklist notes it so it does not read as a bug). All of this is scoped to
    # the key-held gate, so normal play is untouched when G is not held.
    suppress_air = [
        blocks.set_var("formation count", FORMATION_COUNT_ID, number(0)),
        *[
            block
            for slot in range(FLYING_SLOTS[0], FLYING_SLOTS[1] + 1)
            for block in (
                blocks.list_replace("slot type", SLOT_TYPE_ID, number(slot), number(0)),
                blocks.list_replace("slot state", SLOT_STATE_ID, number(slot), number(0)),
            )
        ],
        *[
            block
            for slot in range(BACURA_SLOTS[0], BACURA_SLOTS[1] + 1)
            for block in (
                blocks.list_replace("slot type", SLOT_TYPE_ID, number(slot), number(0)),
                blocks.list_replace("slot state", SLOT_STATE_ID, number(slot), number(0)),
            )
        ],
    ]
    blocks.substack(gate, [*suppress_air, spawn])
    blocks.chain(definition, [gate])


def install_debug_pause(blocks: Blocks) -> None:
    # ENGINE-TODO(#119): remove this temporary debug pause key (and its locked-spec control-mapping amendment)
    # once the ground families are built and playtested, alongside the T and G debug keys.
    # DEBUG / TEMPORARY (tracked for removal): a freeze/resume TOGGLE on the pause key (P) so the operator can
    # stop the screen and take a screenshot of a ground-enemy issue without playing on. It is a TAP toggle, not
    # hold-to-pause, so both hands are free for an OS screenshot: each tick this proc samples P and flips
    # `debug paused` on the RISING edge only (P down now, up last tick), tracked via `debug pause key held`.
    # `debug paused` gates the walk-loop body (the body runs only while it is 0), and this toggle proc is called
    # in the walk OUTSIDE that gate so a second tap can always resume. Both new vars default to 0, and the
    # harness never presses P, so `debug paused` stays 0 there and the build stays deterministic. It amends the
    # locked control mapping — see core-game-systems.md and issue #119.
    definition = _install_warp_proc(blocks, DEBUG_PAUSE_PROCCODE)
    gate = blocks.add("control_if_else")
    pressed = blocks.key_pressed(gate, DEBUG_PAUSE_KEY)
    blocks.blocks[gate]["inputs"]["CONDITION"] = [2, pressed]

    # P held down this tick: on the RISING edge only (held == 0 last tick) flip paused (1 - paused), then
    # remember P is down so holding it does not re-toggle every tick.
    rising = blocks.if_reporter(
        blocks.op_eq(variable("debug pause key held", PAUSE_KEY_HELD_ID), number(0)),
        [
            blocks.set_var_expr(
                "debug paused",
                PAUSED_ID,
                blocks.op_sub(number(1), variable("debug paused", PAUSED_ID)),
            )
        ],
    )
    blocks.substack(
        gate,
        [rising, blocks.set_var("debug pause key held", PAUSE_KEY_HELD_ID, number(1))],
    )
    # P up: clear the held sample so the next press is a fresh rising edge.
    blocks.substack(
        gate,
        [blocks.set_var("debug pause key held", PAUSE_KEY_HELD_ID, number(0))],
        name="SUBSTACK2",
    )
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
        # AIR-11: re-top the Bacura spawn state alongside the schedule cursor, so each area rebuilds its
        # slab population from its own set/reset_bacura_count records and no count bleeds across an area
        # boundary or a respawn. (Under the committed schedules every Bacura window resets num_bacura to 0
        # before its area ends, so this is a no-op there; it hardens the port against a carried count.)
        blocks.set_var("num bacura", NUM_BACURA_ID, number(0)),
        blocks.set_var("bacura inc cnt", BACURA_INC_CNT_ID, number(0)),
        blocks.set_var("one second cntr", ONE_SECOND_CNTR_ID, number(0)),
        # AIR-09: clear the Sheonite end-flag at each area entry so a raised flag never bleeds across an
        # area boundary or a respawn (the natural sheonite_start also clears it, but a debug pre-arm or a
        # partial run must not carry a stuck "time to leave" into the next area).
        blocks.set_var("sheonite end flag", SHEONITE_END_FLAG_ID, number(0)),
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


# GND ground-object seed builders — the block sequences that stamp a ground family into its band slot(s).
# Shared by the schedule ingest (`_consume_schedule`, driven by the area-schedule cursor) and the debug
# ground key (`install_debug_ground_spawn`, driven by fixed debug constants), so both paths seed a family
# identically. Each takes zero-arg callables that return a FRESH reporter per call — a reporter attaches to
# only one parent, so reusing one would silently steal it (the same rule the cursor accessors follow):
# `slot`/`slot_next`/`slot_at(i)` give the ground-band target slot(s), `type_val` the object type, and
# `sprite_y` the lateral sprite row. The schedule ingest passes its cursor accessors; the debug key passes
# `lambda`s over debug constants. Emitted block order matches the former inline lists, so the schedule
# path's generated blocks are unchanged by this extraction.
def _ground_seed_single(blocks: Blocks, *, slot, type_val, sprite_y) -> list[str]:
    # GND (area.ground-dispatch #69): a single-slot ground object (Barra 0x1E, Zolbak 0x1F, Logram 0x26,
    # Derota 0x1B). Mirrors sub_2_fn_1__ground_object ($073F: it sets only _TYPE and _Y, leaving _X = 0 at
    # the top of the field) plus the per-family init the arcade runs on the object handler's first coroutine
    # step, relocated to spawn time in the port: _PTS by family, and (Logram/Derota) the captured fire mask
    # + masked-random initial reload. slot y = sprite_y << 5 (x32); slot x starts at 0 (top-of-field) and
    # `advance ground` scrolls it DOWN each tick. Each column reader and target-slot index is rebuilt fresh.
    return [
        blocks.list_replace("slot type", SLOT_TYPE_ID, slot(), type_val()),
        blocks.list_replace("slot state", SLOT_STATE_ID, slot(), number(SLOT_ACTIVE)),
        blocks.list_replace("slot x", SLOT_X_ID, slot(), number(0)),
        blocks.list_replace(
            "slot y",
            SLOT_Y_ID,
            slot(),
            blocks.op_mul(sprite_y(), number(SLOT_UNITS_PER_PIXEL)),
        ),
        blocks.if_reporter(
            blocks.op_eq(type_val(), number(BARRA_TYPE)),
            [blocks.list_replace("slot pts", SLOT_PTS_ID, slot(), number(BARRA_PTS))],
        ),
        blocks.if_reporter(
            # GND-02 (ground.zolbak #85): a passive dome — like the Barra it only needs its point value at
            # spawn (200 pts); it never fires, so no fire mask / timer. The crater clock is zeroed by the
            # detector at the hit, and `update zolbak` runs the AI-level reduction there, not here.
            blocks.op_eq(type_val(), number(ZOLBAK_TYPE)),
            [blocks.list_replace("slot pts", SLOT_PTS_ID, slot(), number(ZOLBAK_PTS))],
        ),
        blocks.if_reporter(
            blocks.op_eq(type_val(), number(LOGRAM_TYPE)),
            [
                blocks.list_replace(
                    "slot pts", SLOT_PTS_ID, slot(), number(LOGRAM_PTS)
                ),
                blocks.list_replace(
                    "slot fire mask",
                    SLOT_FIRE_MASK_ID,
                    slot(),
                    variable(FIRE_MASK_LOGRAM_NAME, FIRE_MASK_LOGRAM_ID),
                ),
                # Seed the open/close cycle (handle_logram_init $1B49): closed dome (_CODE=0x2C), WAIT phase,
                # and a masked-random initial delay (_TIMER=(rand & mask)+1). A ground slot is only ever a
                # ground object, but cull only clears type/state, so a slot reused from a prior Logram can
                # hold a stale flag/timer/code — seed all three explicitly rather than trust the cleared slot.
                blocks.list_replace(
                    "slot code", SLOT_CODE_ID, slot(), number(LOGRAM_CLOSED_ORDINAL)
                ),
                blocks.list_replace(
                    "slot flag", SLOT_FLAG_ID, slot(), number(LOGRAM_WAIT_PHASE)
                ),
                blocks.call_proc(RNG_PROCCODE, warp=True),
                blocks.list_replace(
                    "slot fire timer",
                    SLOT_FIRE_TIMER_ID,
                    slot(),
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
        blocks.if_reporter(
            # GND-04 (ground.derota #86): a periodic aimed turret (1000 pts). Unlike the Logram it has NO
            # dome cycle, so it needs only its point value, the captured Derota fire mask, and a
            # masked-random initial reload for the shared fire-permission gate (init_derota $1C1C:
            # `_TIMER=(rand & mask)+1`). cull clears only type/state, so seed the mask + timer explicitly.
            blocks.op_eq(type_val(), number(DEROTA_TYPE)),
            [
                blocks.list_replace(
                    "slot pts", SLOT_PTS_ID, slot(), number(DEROTA_PTS)
                ),
                blocks.list_replace(
                    "slot fire mask",
                    SLOT_FIRE_MASK_ID,
                    slot(),
                    variable(FIRE_MASK_DEROTA_NAME, FIRE_MASK_DEROTA_ID),
                ),
                blocks.call_proc(RNG_PROCCODE, warp=True),
                blocks.list_replace(
                    "slot fire timer",
                    SLOT_FIRE_TIMER_ID,
                    slot(),
                    blocks.op_add(
                        blocks.op_mod(
                            variable("rng out", RNG_OUT_ID),
                            blocks.op_add(
                                variable(FIRE_MASK_DEROTA_NAME, FIRE_MASK_DEROTA_ID), number(1)
                            ),
                        ),
                        number(1),
                    ),
                ),
            ],
        ),
        # GND-06 (ground.grobda #88): a self-moving tank. Each of the 12 variants seeds its own value-table
        # position and its initial scroll-axis velocity (activate_and_set_grobda_dX 4574-4579: raw 8 stop /
        # 14 forward / 2 back / 22 dart); the shared branch clears the lateral velocity (Grobda moves
        # scroll-axis-only — _dY is cleared), and zeroes the reaction phase + timer so a slot reused from a
        # prior occupant carries no stale reaction. Motion runs through `advance ground moving`, not the
        # terrain scroll. `slot dy` = 0 also makes the two-axis mover degrade to one axis for Grobda.
        *[
            blocks.if_reporter(
                blocks.op_eq(type_val(), number(variant.type)),
                [
                    blocks.list_replace("slot pts", SLOT_PTS_ID, slot(), number(variant.pts)),
                    blocks.list_replace("slot dx", SLOT_DX_ID, slot(), number(variant.dx0)),
                ],
            )
            for variant in GROBDA_VARIANTS
        ],
        blocks.if_reporter(
            functools.reduce(
                blocks.op_or,
                (blocks.op_eq(type_val(), number(t)) for t in GROBDA_TYPES),
            ),
            [
                blocks.list_replace("slot dy", SLOT_DY_ID, slot(), number(0)),
                blocks.list_replace("slot flag", SLOT_FLAG_ID, slot(), number(GROBDA_FLAG_PRETRIGGER)),
                blocks.list_replace("slot timer", SLOT_TIMER_ID, slot(), number(0)),
            ],
        ),
    ]


def _ground_seed_domogram(
    blocks: Blocks, *, slot, sprite_y, path_start, path_count
) -> list[str]:
    # GND-07 (ground.domogram #89): stamp a Domogram into one ground band slot, mirroring the once-only init
    # of handle_2E_Domogram ($2ED6): state ACTIVE, 800 pts (DOMOGRAM_PTS), the captured fire mask, a
    # masked-random initial shot timer (_TIMER = (rand & mask)+1), the anim timer (_TYPE) cleared, and the
    # path coroutine primed — _VECLEN = 1 (slot flag) so the FIRST ACTIVE tick loads the first vector, the
    # path pointer (slot link) = this instance's 1-based start in the shared step columns, and _NVEC (slot
    # vec left) = the step count. slot x starts at 0 (top of field) and slot y = sprite_y << 5; the velocity
    # (slot dx/dy) starts 0 and is set when the first vector loads. cull clears only type/state, so every
    # repurposed slot field is seeded explicitly rather than trusted clean.
    return [
        blocks.list_replace("slot type", SLOT_TYPE_ID, slot(), number(DOMOGRAM_TYPE)),
        blocks.list_replace("slot state", SLOT_STATE_ID, slot(), number(SLOT_ACTIVE)),
        blocks.list_replace("slot x", SLOT_X_ID, slot(), number(0)),
        blocks.list_replace(
            "slot y", SLOT_Y_ID, slot(), blocks.op_mul(sprite_y(), number(SLOT_UNITS_PER_PIXEL))
        ),
        blocks.list_replace("slot pts", SLOT_PTS_ID, slot(), number(DOMOGRAM_PTS)),
        blocks.list_replace("slot dx", SLOT_DX_ID, slot(), number(0)),
        blocks.list_replace("slot dy", SLOT_DY_ID, slot(), number(0)),
        blocks.list_replace(
            "slot fire mask",
            SLOT_FIRE_MASK_ID,
            slot(),
            variable(FIRE_MASK_DOMOGRAM_NAME, FIRE_MASK_DOMOGRAM_ID),
        ),
        blocks.list_replace("slot fire timer", SLOT_FIRE_TIMER_ID, slot(), number(0)),
        blocks.list_replace("slot flag", SLOT_FLAG_ID, slot(), number(DOMOGRAM_VECLEN_INIT)),
        blocks.list_replace("slot link", SLOT_LINK_ID, slot(), path_start()),
        blocks.list_replace("slot vec left", SLOT_VEC_LEFT_ID, slot(), path_count()),
        blocks.call_proc(RNG_PROCCODE, warp=True),
        blocks.list_replace(
            "slot timer",
            SLOT_TIMER_ID,
            slot(),
            blocks.op_add(
                blocks.op_mod(
                    variable("rng out", RNG_OUT_ID),
                    blocks.op_add(
                        variable(FIRE_MASK_DOMOGRAM_NAME, FIRE_MASK_DOMOGRAM_ID), number(1)
                    ),
                ),
                number(1),
            ),
        ),
    ]


def _ground_seed_garu(blocks: Blocks, *, slot, slot_next, type_val, sprite_y) -> list[str]:
    # GND (ground.barra #70): the Garu Barra is a TWO-slot object (handle_20_Garu_Barra $1A89). Base @ N:
    # the indestructible 2x2 (state SLOT_GARU_BASE, so the detector's ==ACTIVE gate rejects it) that
    # colour-PULSES (the flashing base, arcade _CODE=0x48) and REMAINS when the top is bombed. Node @ N+1: the
    # destructible pyramid top (state ACTIVE, 300 pts, arcade _CODE=0x17 = the Barra pyramid, barra/idle) that
    # you bomb AWAY to expose the flashing base. Both scroll at the shared terrain rate.
    #
    # Port necessity (centre-anchor): the arcade node carries absolute offsets _X=+0x0100 (+1 cell) and
    # _Y=base_Y-0x0100 only to re-centre a CORNER-anchored node inside a corner-anchored 2x2 base. The port's
    # go_expr places every sprite by its CENTRE, so that corner-centring must become a ZERO relative offset:
    # the node is seeded on the base's own cell (slot x = 0, same slot y) so the pyramid top sits centred on
    # the flashing base — offsetting it instead makes the top poke out a corner ("doubling").
    return [
        blocks.list_replace("slot type", SLOT_TYPE_ID, slot(), type_val()),
        blocks.list_replace("slot state", SLOT_STATE_ID, slot(), number(SLOT_GARU_BASE)),
        blocks.list_replace("slot x", SLOT_X_ID, slot(), number(0)),
        blocks.list_replace(
            "slot y",
            SLOT_Y_ID,
            slot(),
            blocks.op_mul(sprite_y(), number(SLOT_UNITS_PER_PIXEL)),
        ),
        blocks.list_replace("slot type", SLOT_TYPE_ID, slot_next(), type_val()),
        blocks.list_replace("slot state", SLOT_STATE_ID, slot_next(), number(SLOT_ACTIVE)),
        blocks.list_replace("slot x", SLOT_X_ID, slot_next(), number(0)),
        blocks.list_replace(
            "slot y",
            SLOT_Y_ID,
            slot_next(),
            blocks.op_mul(sprite_y(), number(SLOT_UNITS_PER_PIXEL)),
        ),
        blocks.list_replace("slot pts", SLOT_PTS_ID, slot_next(), number(GARU_BARRA_PTS)),
    ]


def _ground_seed_garu_derota(blocks: Blocks, *, slot, slot_next, type_val, sprite_y) -> list[str]:
    # GND-04 (ground.derota #86): the Garu Derota is the Garu Barra's two-slot shape but the node FIRES
    # (handle_21_Garu_Derota $1C61). Base @ N: the indestructible 2x2 that colour-PULSES (the flashing base,
    # arcade _CODE=0x44) and REMAINS. Node @ N+1: the destructible turret top (arcade _CODE=0x27 = the Derota
    # turret, derota/idle) that you bomb AWAY to expose the flashing base; it additionally gets 2000 pts, the
    # captured Derota fire mask, and a masked-random initial reload for the shared fire-permission gate
    # (`_TIMER=(rand & mask)+1` on the node object). cull clears only type/state, so seed mask + timer.
    #
    # Port necessity (centre-anchor): identical to the Garu Barra — the arcade node's _X=+0x0100 / _Y adjust is
    # corner-centring for a corner-anchored 2x2 base, so under the port's centre-anchored go_expr the node is
    # seeded on the base's own cell (zero relative offset) to centre the turret top on the flashing base.
    return [
        blocks.list_replace("slot type", SLOT_TYPE_ID, slot(), type_val()),
        blocks.list_replace("slot state", SLOT_STATE_ID, slot(), number(SLOT_GARU_BASE)),
        blocks.list_replace("slot x", SLOT_X_ID, slot(), number(0)),
        blocks.list_replace(
            "slot y",
            SLOT_Y_ID,
            slot(),
            blocks.op_mul(sprite_y(), number(SLOT_UNITS_PER_PIXEL)),
        ),
        blocks.list_replace("slot type", SLOT_TYPE_ID, slot_next(), type_val()),
        blocks.list_replace("slot state", SLOT_STATE_ID, slot_next(), number(SLOT_ACTIVE)),
        blocks.list_replace("slot x", SLOT_X_ID, slot_next(), number(0)),
        blocks.list_replace(
            "slot y",
            SLOT_Y_ID,
            slot_next(),
            blocks.op_mul(sprite_y(), number(SLOT_UNITS_PER_PIXEL)),
        ),
        blocks.list_replace("slot pts", SLOT_PTS_ID, slot_next(), number(GARU_DEROTA_PTS)),
        blocks.list_replace(
            "slot fire mask",
            SLOT_FIRE_MASK_ID,
            slot_next(),
            variable(FIRE_MASK_DEROTA_NAME, FIRE_MASK_DEROTA_ID),
        ),
        blocks.call_proc(RNG_PROCCODE, warp=True),
        blocks.list_replace(
            "slot fire timer",
            SLOT_FIRE_TIMER_ID,
            slot_next(),
            blocks.op_add(
                blocks.op_mod(
                    variable("rng out", RNG_OUT_ID),
                    blocks.op_add(variable(FIRE_MASK_DEROTA_NAME, FIRE_MASK_DEROTA_ID), number(1)),
                ),
                number(1),
            ),
        ),
    ]


def _ground_seed_boza(blocks: Blocks, *, slot_at, type_val, sprite_y) -> list[str]:
    # GND-05 (ground.boza-logram #87): a FIVE-slot composite (handle_2D_Boza_Logram $1CDE). The four OUTER
    # domes (base+0..3) and the CENTRE (base+4) each become their own ground slot, positioned by the arcade's
    # per-object tables (BOZA_DEPTH_OFFSETS_PX / BOZA_LATERAL_OFFSETS_PX scaled by SLOT_UNITS_PER_PIXEL). All
    # five start ACTIVE with `slot timer` 0 (a clean crater/burst clock; cull clears only type/state). Each
    # OUTER is a lone Logram (300 pts, closed dome, WAIT phase, captured Boza mask + masked-random initial
    # delay) and stores the CENTRE's slot index in `slot link` (the port of the arcade `_EXTRA` pointer). The
    # CENTRE (2,000 pts) never fires and stores `slot link` 0, which marks it as the centre for the walk's
    # branch and holds its full value until an outer hit downgrades it. `slot_at(i)` returns a FRESH reporter.
    seed: list[str] = []
    # Depth offsets carry an extra isotropic factor so the composite renders as the arcade's square diamond
    # rather than a vertically-collapsed blob: the anamorphic cell->stage map spaces lateral at RENDER_COL_STAGE
    # px/cell but depth at only RENDER_ROW_STAGE px/cell, so a raw depth offset renders RENDER_COL_STAGE/
    # RENDER_ROW_STAGE too tight for the isotropic dome sprites. Lateral is already at the sprite scale, so it
    # keeps the plain per-pixel scale. (See the BOZA_DEPTH_OFFSETS_PX note; port necessity in docs/mechanics/042.)
    depth_units_per_px = SLOT_UNITS_PER_PIXEL * RENDER_COL_STAGE // RENDER_ROW_STAGE  # 32 * 15 // 8 = 60
    for i in range(BOZA_SLOT_COUNT):
        depth_units = BOZA_DEPTH_OFFSETS_PX[i] * depth_units_per_px
        lateral_units = BOZA_LATERAL_OFFSETS_PX[i] * SLOT_UNITS_PER_PIXEL
        seed.append(
            blocks.list_replace("slot type", SLOT_TYPE_ID, slot_at(i), type_val())
        )
        seed.append(
            blocks.list_replace("slot state", SLOT_STATE_ID, slot_at(i), number(SLOT_ACTIVE))
        )
        seed.append(
            blocks.list_replace("slot x", SLOT_X_ID, slot_at(i), number(depth_units))
        )
        seed.append(
            blocks.list_replace(
                "slot y",
                SLOT_Y_ID,
                slot_at(i),
                blocks.op_add(
                    blocks.op_mul(sprite_y(), number(SLOT_UNITS_PER_PIXEL)),
                    number(lateral_units),
                ),
            )
        )
        seed.append(
            blocks.list_replace("slot timer", SLOT_TIMER_ID, slot_at(i), number(0))
        )
        if i == BOZA_CENTRE_OFFSET:
            # CENTRE (handle_boza_logram_centre): 2,000 pts, never fires, `slot link` 0 (the centre marker).
            seed.append(
                blocks.list_replace("slot pts", SLOT_PTS_ID, slot_at(i), number(BOZA_CENTRE_PTS))
            )
            seed.append(
                blocks.list_replace("slot link", SLOT_LINK_ID, slot_at(i), number(0))
            )
        else:
            # OUTER dome (handle_boza_logram_outer): 300 pts, the Logram open/close/fire machine, and a
            # `slot link` back to the centre slot (base+4). Seed the closed dome, WAIT phase, captured mask,
            # and the masked-random initial delay `_TIMER=(rand & mask)+1` — the once-only arcade init.
            seed.append(
                blocks.list_replace("slot pts", SLOT_PTS_ID, slot_at(i), number(BOZA_OUTER_PTS))
            )
            seed.append(
                blocks.list_replace(
                    "slot link", SLOT_LINK_ID, slot_at(i), slot_at(BOZA_CENTRE_OFFSET)
                )
            )
            seed.append(
                blocks.list_replace(
                    "slot fire mask",
                    SLOT_FIRE_MASK_ID,
                    slot_at(i),
                    variable(FIRE_MASK_BOZA_NAME, FIRE_MASK_BOZA_ID),
                )
            )
            seed.append(
                blocks.list_replace(
                    "slot code", SLOT_CODE_ID, slot_at(i), number(BOZA_OUTER_CLOSED_ORDINAL)
                )
            )
            seed.append(
                blocks.list_replace(
                    "slot flag", SLOT_FLAG_ID, slot_at(i), number(LOGRAM_WAIT_PHASE)
                )
            )
            seed.append(blocks.call_proc(RNG_PROCCODE, warp=True))
            seed.append(
                blocks.list_replace(
                    "slot fire timer",
                    SLOT_FIRE_TIMER_ID,
                    slot_at(i),
                    blocks.op_add(
                        blocks.op_mod(
                            variable("rng out", RNG_OUT_ID),
                            blocks.op_add(
                                variable(FIRE_MASK_BOZA_NAME, FIRE_MASK_BOZA_ID), number(1)
                            ),
                        ),
                        number(1),
                    ),
                )
            )
    return seed


def _debug_ground_seed(blocks: Blocks, family_type: int, shape: str) -> list[str]:
    # DEBUG (tracked for removal #119): build ONE ground family's spawn from fixed debug constants — the band
    # base slot and a central lateral column (DEBUG_GROUND_SPRITE_Y) — through the SAME shared seed builders the
    # area schedule uses, so a debug-stamped family is the scheduled family's exact shape (only the slot and
    # column are fixed, not the behaviour). `shape` picks the builder. Each factory returns a FRESH reporter per
    # call (a reporter attaches to one parent only — reuse silently steals it), exactly as the cursor accessors do.
    base = GROUND_SLOTS[0]
    type_val = lambda: number(family_type)
    sprite_y = lambda: number(DEBUG_GROUND_SPRITE_Y)
    if shape == "single":
        return _ground_seed_single(
            blocks, slot=lambda: number(base), type_val=type_val, sprite_y=sprite_y
        )
    if shape == "garu":
        return _ground_seed_garu(
            blocks,
            slot=lambda: number(base),
            slot_next=lambda: number(base + 1),
            type_val=type_val,
            sprite_y=sprite_y,
        )
    if shape == "garu_derota":
        return _ground_seed_garu_derota(
            blocks,
            slot=lambda: number(base),
            slot_next=lambda: number(base + 1),
            type_val=type_val,
            sprite_y=sprite_y,
        )
    if shape == "boza":
        return _ground_seed_boza(
            blocks, slot_at=lambda i: number(base + i), type_val=type_val, sprite_y=sprite_y
        )
    if shape == "domogram":
        # GND-07 (#89): the debug spawn cannot carry a scripted path (the path columns live only in the schedule),
        # so seed with an EMPTY path (count 0 -> the follower holds its vector forever) and directly seed a
        # representative diagonal vector (DOMOGRAM_DEBUG_VECTOR_INDEX: scroll-matched depth + lateral drift), so
        # the operator sees a Domogram cross the field and fire without needing the full schedule path decode.
        return _ground_seed_domogram(
            blocks,
            slot=lambda: number(base),
            sprite_y=sprite_y,
            path_start=lambda: number(0),
            path_count=lambda: number(0),
        ) + [
            blocks.list_replace(
                "slot dx", SLOT_DX_ID, number(base),
                number(DOMOGRAM_VECTOR_DX[DOMOGRAM_DEBUG_VECTOR_INDEX]),
            ),
            blocks.list_replace(
                "slot dy", SLOT_DY_ID, number(base),
                number(DOMOGRAM_VECTOR_DY[DOMOGRAM_DEBUG_VECTOR_INDEX]),
            ),
        ]
    raise ValueError(f"unknown debug ground seed shape: {shape!r}")


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

    # GND-07 (#89) Domogram path columns at the cursor (fresh reporter per call — a reporter attaches to one
    # parent only). `path start` is the 1-based index of the instance's first step in the shared step columns.
    def ground_path_start_at_cursor() -> str:
        return blocks.list_item(
            "schedule domogram path start", SCHEDULE_DOMOGRAM_PATH_START_ID, cursor()
        )

    def ground_path_count_at_cursor() -> str:
        return blocks.list_item(
            "schedule domogram path count", SCHEDULE_DOMOGRAM_PATH_COUNT_ID, cursor()
        )

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
    # GND (area.ground-dispatch #69) + GND-01..05: stamp a ground family into its band slot(s) via the
    # shared seed builders (_ground_seed_single / _garu / _garu_derota / _boza). The schedule ingest drives
    # them with the cursor accessors; the debug ground key (install_debug_ground_spawn) drives the same
    # builders with fixed debug constants. Only families built to date stamp; every other add_ground_object
    # record advances the cursor WITHOUT stamping a slot, so no unbuilt family renders a live-but-inert object.
    spawn_ground = _ground_seed_single(
        blocks,
        slot=ground_target_slot,
        type_val=ground_type_at_cursor,
        sprite_y=ground_sprite_y_at_cursor,
    )
    # GND-06 (ground.grobda #88): all 12 Grobda variants are single-slot ground objects (a self-moving tank
    # occupies one band slot), so they seed through the same spawn_ground/_ground_seed_single as the static
    # single-slot families — the per-variant velocity + reaction phase is seeded inside that builder.
    is_single_slot_ground = functools.reduce(
        blocks.op_or,
        (blocks.op_eq(ground_type_at_cursor(), number(t)) for t in GROBDA_TYPES),
        blocks.op_or(
            blocks.op_or(
                blocks.op_eq(ground_type_at_cursor(), number(BARRA_TYPE)),
                blocks.op_eq(ground_type_at_cursor(), number(ZOLBAK_TYPE)),
            ),
            blocks.op_or(
                blocks.op_eq(ground_type_at_cursor(), number(LOGRAM_TYPE)),
                blocks.op_eq(ground_type_at_cursor(), number(DEROTA_TYPE)),
            ),
        ),
    )
    # GND (ground.barra #70): the Garu Barra is a TWO-slot object (handle_20_Garu_Barra $1A89), so it does
    # not fit the single-slot spawn_ground. Base @ N: the indestructible 2x2 (state SLOT_GARU_BASE, so the
    # detector's ==ACTIVE gate rejects it), at slot x = 0 (top of field) and the record's lateral sprite_y.
    # Node @ N+1: the destructible core (state ACTIVE, 300 pts), placed at the arcade's absolute offsets —
    # slot x = 1 cell (_X = 0x0100) and slot y = base_y - 1 cell (_Y = base_Y - 0x0100, the verified 8-px
    # LATERAL offset, GND-01). Both scroll together at the shared terrain rate. slot y = sprite_y << 5 (x32).
    spawn_garu = _ground_seed_garu(
        blocks,
        slot=ground_target_slot,
        slot_next=ground_target_slot_next,
        type_val=ground_type_at_cursor,
        sprite_y=ground_sprite_y_at_cursor,
    )
    # GND-04 (ground.derota #86): the Garu Derota is the Garu Barra's two-slot shape (indestructible 2x2
    # base @ N, destructible node @ N+1) but the node FIRES (handle_21_Garu_Derota $1C61). Base and node
    # placement are identical to the Garu Barra; the node additionally gets 2000 pts, the captured Derota
    # fire mask, and a masked-random initial reload for the shared fire-permission gate
    # (`_TIMER=(rand & mask)+1` on the node object). cull clears only type/state, so seed mask + timer.
    spawn_garu_derota = _ground_seed_garu_derota(
        blocks,
        slot=ground_target_slot,
        slot_next=ground_target_slot_next,
        type_val=ground_type_at_cursor,
        sprite_y=ground_sprite_y_at_cursor,
    )
    # GND-05 (ground.boza-logram #87): a Boza Logram is a FIVE-slot composite (handle_2D_Boza_Logram $1CDE
    # stamps 5 objects), so it does not fit the single-slot spawn_ground OR the two-slot Garu shape. The four
    # OUTER domes (base+0..3) and the CENTRE (base+4) each become their own ground slot, positioned by the
    # arcade's per-object tables written straight into `_X`/`_Y`: depth (slot x) = boza_logram_spriteX_tbl
    # {0,0x180,0x180,0x300,0x180} and lateral (slot y) = base_Y + {0,+0x180,-0x180,0,0}, i.e. the px offsets
    # BOZA_DEPTH_OFFSETS_PX / BOZA_LATERAL_OFFSETS_PX scaled by SLOT_UNITS_PER_PIXEL. All five start ACTIVE
    # with `slot timer` 0 (a clean crater/burst clock; cull clears only type/state, so seed it). Each OUTER
    # is a lone Logram (300 pts, closed dome, WAIT phase, captured Boza mask + masked-random initial delay —
    # handle_boza_logram_outer's once-only init) and stores the CENTRE's slot index in `slot link` (the port
    # of the arcade `_EXTRA` pointer). The CENTRE (2,000 pts) never fires and stores `slot link` 0, which both
    # marks it as the centre for the walk's branch and holds its full value until an outer hit downgrades it.
    spawn_boza = _ground_seed_boza(
        blocks,
        slot_at=lambda i: blocks.op_add(number(GROUND_SLOTS[0] + i), ground_slot_at_cursor()),
        type_val=ground_type_at_cursor,
        sprite_y=ground_sprite_y_at_cursor,
    )
    # GND-07 (ground.domogram #89): a Domogram is a single-slot self-moving ground object placed by its own
    # `add_domogram_with_path` handler (NOT add_ground_object), because it carries a scripted path the three
    # ground scalars cannot. It seeds into the same ground band slot as the single-slot families, plus the two
    # path columns (start + count) that prime the follower.
    spawn_domogram = _ground_seed_domogram(
        blocks,
        slot=ground_target_slot,
        sprite_y=ground_sprite_y_at_cursor,
        path_start=ground_path_start_at_cursor,
        path_count=ground_path_count_at_cursor,
    )
    # DEBUG / TEMPORARY (tracked for removal, #119): while the G ground-debug key is held, do NOT stamp the
    # schedule's own add_ground_object records — the debug key owns the ground band so the operator sees one
    # built family at a time, isolated from normal play. The cursor still advances at the loop's end regardless,
    # so no schedule record is skipped or replayed; only the stamp is withheld while G is held. When G is not
    # held this is exactly the original condition, so normal play is untouched.
    add_ground_branch = blocks.if_reporter(
        blocks.op_and(
            blocks.op_eq(handler_at_cursor(), text(ADD_GROUND_OBJECT_HANDLER)),
            blocks.op_not(blocks.key_pressed(loop, DEBUG_GROUND_KEY)),
        ),
        [
            blocks.if_reporter(is_single_slot_ground, spawn_ground),
            blocks.if_reporter(
                blocks.op_eq(ground_type_at_cursor(), number(GARU_BARRA_TYPE)), spawn_garu
            ),
            blocks.if_reporter(
                blocks.op_eq(ground_type_at_cursor(), number(GARU_DEROTA_TYPE)), spawn_garu_derota
            ),
            blocks.if_reporter(
                blocks.op_eq(ground_type_at_cursor(), number(BOZA_LOGRAM_TYPE)), spawn_boza
            ),
        ],
    )
    # GND-07 (ground.domogram #89): the Domogram's own placement handler. Like add_ground_object it is withheld
    # while the G ground-debug key owns the band (the cursor still advances at the loop end, so no record is
    # skipped or replayed). The ground-type guard mirrors every other ground family: a real Domogram record
    # always carries type 0x2E, so this never changes normal play, but it keeps the Domogram spawn keyed off
    # `area-schedule-ground-type` exactly like the static/Grobda/Garu/Boza branches — so a test (or a debug
    # aid) that zeroes that column to isolate a scenario suppresses the Domogram uniformly with the rest.
    add_domogram_branch = blocks.if_reporter(
        blocks.op_and(
            blocks.op_and(
                blocks.op_eq(handler_at_cursor(), text(ADD_DOMOGRAM_HANDLER)),
                blocks.op_eq(ground_type_at_cursor(), number(DOMOGRAM_TYPE)),
            ),
            blocks.op_not(blocks.key_pressed(loop, DEBUG_GROUND_KEY)),
        ),
        spawn_domogram,
    )
    # AIR-11 (air.bacura #81): set_bacura_count (op 0x22) loads the per-window increment quota
    # (sub_2_fn_6__set_bacura_inc_cnt $075D); the pump then admits one slab per arcade second. Reload
    # `one second cntr` here so the first admit lands ~1s after the record fires (the arcade's main_fn_5
    # sets it on its first run). reset_bacura_count (op 0x23) clears the active count
    # (sub_2_fn_7__reset_num_bacura $05D8: `clr.b (num_bacura)`), stopping refills — slabs already
    # drifting are left to cull on their own.
    set_bacura_branch = blocks.if_reporter(
        blocks.op_eq(handler_at_cursor(), text(SET_BACURA_COUNT_HANDLER)),
        [
            blocks.set_var_expr("bacura inc cnt", BACURA_INC_CNT_ID, arg_at_cursor()),
            blocks.set_var("one second cntr", ONE_SECOND_CNTR_ID, number(BACURA_INC_PERIOD_FRAMES)),
        ],
    )
    reset_bacura_branch = blocks.if_reporter(
        blocks.op_eq(handler_at_cursor(), text(RESET_BACURA_COUNT_HANDLER)),
        [blocks.set_var("num bacura", NUM_BACURA_ID, number(0))],
    )
    # AIR-09 (air.sheonite #79): sheonite_start (op 0x33, sub_2_fn_18 sub:530-537) stamps the escort PAIR
    # into the two fixed flying slots (0x31 -> 0x3f right, 0x32 -> 0x3e left) and clears the end-flag;
    # sheonite_end (op 0x34, sub_2_fn_19 sub:539-542) raises the end-flag, releasing the pair from LOCK
    # into the dock/peel-off. On/off flags, not a per-second pump. These records already live in the loaded
    # area schedules (area 9 spawns the pair at row 179 and ends it at 159); wiring the branches makes them
    # spawn. The pair is inert, so nothing here touches the hit/score/bomb path.
    sheonite_start_branch = blocks.if_reporter(
        blocks.op_eq(handler_at_cursor(), text(SHEONITE_START_HANDLER)),
        [
            *_stamp_sheonite(blocks, SHEONITE_RIGHT_SLOT, RIGHT_SHEONITE_TYPE, -1),
            *_stamp_sheonite(blocks, SHEONITE_LEFT_SLOT, LEFT_SHEONITE_TYPE, +1),
            blocks.set_var("sheonite end flag", SHEONITE_END_FLAG_ID, number(0)),
        ],
    )
    sheonite_end_branch = blocks.if_reporter(
        blocks.op_eq(handler_at_cursor(), text(SHEONITE_END_HANDLER)),
        [blocks.set_var("sheonite end flag", SHEONITE_END_FLAG_ID, number(1))],
    )
    # ENGINE-TODO: the remaining spawn / boss handler dispatch (add_object, andor_genesis_*) lands with the
    # later enemy slices. The DIF/FORM handlers (raise, adjust, set/reset formation, the 8 fire masks,
    # ground-stop), add_ground_object (the built static + Grobda ground families) and add_domogram_with_path
    # (GND-07) are wired above; the still-unhandled spawn/boss records advance the cursor and count the fire only.
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
            add_domogram_branch,
            set_bacura_branch,
            reset_bacura_branch,
            sheonite_start_branch,
            sheonite_end_branch,
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
    install_init_zakato(blocks)
    install_init_giddo_spario(blocks)
    install_init_brag_spario(blocks)
    install_init_brag_zakato(blocks)
    install_init_garu_zakato(blocks)
    install_init_bacura(blocks)
    install_check_air_hit(blocks)
    install_check_ground_hit(blocks)
    install_track_crosshair(blocks)
    install_advance_bomb(blocks)
    install_advance_ground(blocks)
    install_advance_ground_moving(blocks)
    install_update_barra(blocks)
    install_update_garu(blocks)
    install_update_logram(blocks)
    install_update_zolbak(blocks)
    install_update_derota(blocks)
    install_update_garu_derota(blocks)
    install_update_boza(blocks)
    install_update_grobda(blocks)
    install_update_domogram(blocks)
    install_explode_toroid_tick(blocks)
    install_explode_giddo_spario_tick(blocks)
    install_update_bullet(blocks)
    install_update_toroid(blocks)
    install_update_terrazi(blocks)
    install_update_kapi(blocks)
    install_update_torkan(blocks)
    install_update_zoshi(blocks)
    install_update_jara(blocks)
    install_update_zakato(blocks)
    install_update_giddo_spario(blocks)
    install_update_brag_spario(blocks)
    install_update_brag_zakato(blocks)
    install_brag_zakato_shoot(blocks)
    install_update_garu_zakato(blocks)
    install_garu_zakato_detonate(blocks)
    install_check_shot_bacura(blocks)
    install_update_bacura(blocks)
    install_pump_bacura(blocks)
    install_update_sheonite(blocks)  # AIR-09
    install_fire_permission_gate(blocks)
    install_cull_slot(blocks)
    install_advance_slots(blocks)
    install_spawn_flying(blocks)
    install_debug_spawn_wave(blocks)  # DEBUG / temporary (tracked for removal)
    install_debug_ground_spawn(blocks)  # DEBUG / temporary (tracked for removal, #119)
    install_debug_pause(blocks)  # DEBUG / temporary (tracked for removal, #119)
    install_advance_area(blocks)
    install_score(blocks)
    install_check_bonus_life(blocks)
    install_resolve_hit(blocks)
    install_alloc_bullet_slot(blocks)
    install_emit_radiating_bullet(blocks)

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

    # AUDIO: sound-only receiver for the shot×Bacura bounce. The bounce runs on a blaster clone
    # (blaster_blocks) that cannot play a Stage-owned sound directly, so it broadcasts `sfx bacura`
    # and the Stage plays BACURA_HIT_SND here (src deactivate_shot xevious_main.68k:2559).
    blocks.chain(blocks.receive("sfx bacura"), [blocks.play_sound("bacura")])

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
    # DEBUG (temporary, tracked for removal #119): while G is held, the Bacura pump is suppressed too, so no
    # slabs drift in during ground isolation (parity with the T key). The G proc already zeros `formation count`
    # and clears the flying/bacura bands each tick; gating the pump call stops it re-admitting. When G is not
    # held this is exactly the original unconditional call.
    pump_bacura = blocks.if_reporter(
        blocks.op_not(blocks.key_pressed(walk_loop, DEBUG_GROUND_KEY)),
        [
            # AIR-11: the Bacura live-spawn pump runs in the spawn phase, after ADVANCE_AREA has loaded
            # this tick's set/reset_bacura_count records and after the walk — so a freshly-stamped slab
            # first drifts on the NEXT tick, matching the arcade's handle_01_Bacura (init, then yield)
            # and the flying spawner above (spawn late, drive next tick).
            blocks.call_proc(PUMP_BACURA_PROCCODE, warp=True),
        ],
    )
    # The whole tick — read, area clock, walk, bomb, spawns, death — runs only while NOT paused. The
    # ADVANCE_AREA -> ADVANCE_SLOTS pair stays adjacent inside this body, so the area-clock adjacency contract
    # holds; the pause gate merely wraps the body.
    tick_body = [
        blocks.call_proc(READ_PLAYER_PROCCODE, warp=True),
        # WPN-04: the bomb sight leads the craft (needs the just-cached player cell).
        blocks.call_proc(TRACK_CROSSHAIR_PROCCODE, warp=True),
        blocks.call_proc(ADVANCE_AREA_PROCCODE, warp=True),
        blocks.call_proc(ADVANCE_SLOTS_PROCCODE, warp=True),
        # WPN-04: arm/fly the bomb AFTER the terrain has scrolled this tick, so the landing
        # compare sees the same-tick ground positions (handle_bombing runs late in the frame).
        blocks.call_proc(ADVANCE_BOMB_PROCCODE, warp=True),
        # DEBUG (temporary, tracked for removal #119): while G is held, cycle one built GROUND family
        # into the band. Placed after the ground walk (ADVANCE_SLOTS) so the field-empty gate reads the
        # fully-settled post-cull band, and outside the ADVANCE_AREA -> ADVANCE_SLOTS pair the area clock
        # requires be adjacent. The stamp scrolls on the NEXT walk, then travels down to the craft — a
        # one-tick delay that is immaterial for a top-of-field spawn. Self-gated on the key; no effect on
        # normal play, and it defers to any scheduled ground object (only fills a genuinely empty field).
        blocks.call_proc(DEBUG_GROUND_SPAWN_PROCCODE, warp=True),
        # DEBUG (temporary, tracked for removal): overrides the scheduled formation to a Terrazi
        # wave while the debug key is held, so the spawner below fills a Terrazi wave for playtest.
        blocks.call_proc(DEBUG_SPAWN_PROCCODE, warp=True),
        blocks.call_proc(SPAWN_FLYING_PROCCODE, warp=True),
        pump_bacura,
        death_check,
    ]
    run_when_unpaused = blocks.if_reporter(
        blocks.op_eq(variable("debug paused", PAUSED_ID), number(0)),
        tick_body,
    )
    # DEBUG (temporary, tracked for removal #119): the pause TOGGLE runs FIRST and OUTSIDE the freeze gate, so a
    # tap of P can always flip `debug paused` back to 0 and resume. The harness never presses P, so `debug
    # paused` stays 0 there and the full tick runs every frame as before — the build stays deterministic.
    blocks.substack(
        walk_loop,
        [
            blocks.call_proc(DEBUG_PAUSE_PROCCODE, warp=True),
            run_when_unpaused,
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
    # WPN-01 shot bounce: the travel loop above exits the instant the walk marks this shot non-ACTIVE.
    # When that mark is SHOT_BOUNCE (a `check shot bacura` overlap), the shot does not simply vanish — it
    # rebounds. Reverse it (BACURA_BOUNCE_DY = -5 stage-px/frame, the arcade's reflected 1/4-speed) and run
    # the reference's BACURA_BOUNCE_FRAMES (8) costume frames in place, then fall through to the shared
    # free+delete below. The Bacura is untouched; only the shot animates away. Ordinary air-kill spends
    # (SHOT_SPENT) and top-expiry (still ACTIVE) skip this branch and delete at once as before. The
    # real BACURA_HIT_SND now plays (src deactivate_shot xevious_main.68k:2559): this branch runs on a
    # blaster clone, which cannot play the Stage-owned `bacura` sound directly, so it broadcasts
    # `sfx bacura` and the Stage's receiver plays it.
    bounce_anim = blocks.add("control_repeat", inputs={"TIMES": number(BACURA_BOUNCE_FRAMES)})
    blocks.substack(
        bounce_anim,
        [
            blocks.add("motion_changeyby", inputs={"DY": number(BACURA_BOUNCE_DY)}),
            blocks.add("looks_nextcostume"),
        ],
    )
    bounce = blocks.if_reporter(
        blocks.op_eq(
            blocks.list_item("slot state", SLOT_STATE_ID, variable("clone slot", CLONE_SLOT_ID)),
            number(SHOT_BOUNCE),
        ),
        [blocks.send("sfx bacura"), bounce_anim],
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
            bounce,
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
            # WPN-04 layering: a ground object sits ON the terrain, under the craft that
            # flies over it and the bomb sight the player aims with. Unlike the flying
            # renderers it does NOT go to front — its static layerOrder is already above
            # the terrain strips (which never front themselves), so leaving it unfronted
            # keeps it below the craft/crosshair/bomb-target (which do front every tick)
            # while staying above the ground. Fronting here is what put a Barra over the
            # sight the player was aiming with.
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
    # State cascade: HIT (node exploding) -> base (sentinel) -> ACTIVE node.
    base_or_node = blocks.add("control_if_else")
    is_base = blocks.op_eq(
        blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_GARU_BASE)
    )
    blocks.blocks[base_or_node]["inputs"]["CONDITION"] = [2, is_base]
    blocks.blocks[is_base]["parent"] = base_or_node
    # The indestructible base holds the red-socket frame (02) statically: bomb the top and the lit red socket
    # is what shows beneath. The arcade colour-pulses the base's red lights (pulsing_colour_1); Scratch can
    # pulse neither the red lights alone (a hue shift greens them, a brightness pulse flashes the whole base)
    # nor synthesise a red-off crop, so the red-light glow is a recorded port necessity (operator decision
    # 2026-09-24) and the exposed base simply shows the steady lit red socket.
    blocks.substack(base_or_node, [blocks.switch_costume(GARU_BASE_EXPOSED_COSTUME)])
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
            # WPN-04 layering: like the Barra, the Garu base/node stay on the terrain,
            # under the craft and the bomb sight — its static layerOrder is already above
            # the (never-fronting) terrain, so it is left unfronted. See barra_blocks.
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
            # WPN-04 layering: like the Barra, the Logram dome/crater stays on the terrain,
            # under the craft and the bomb sight — its static layerOrder is already above
            # the (never-fronting) terrain, so it is left unfronted. See barra_blocks.
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(clone, [blocks.hide(), loop])
    return blocks.blocks


def boza_blocks() -> dict[str, dict[str, Any]]:
    # GND-05 (ground.boza-logram #87) renderer (game_director owns these blocks; sprite_extractor owns the
    # costumes). One persistent clone per GROUND slot (1..16), the same terrain-band clone pool as every
    # ground family, each a pure per-tick function of its slot's live state. All five composite parts share
    # BOZA_LOGRAM_TYPE, so the clone branches on `slot link` (the outer/centre discriminator, mirroring the
    # walk's own branch):
    #   OUTER (link > 0)  ACTIVE -> the open/close dome frame `update boza` wrote into `slot code` (ordinals
    #                              1..4 = logram/open/01..04, REUSED for the identical arcade sprites 0x2C..0x2F).
    #   CENTRE (link == 0) ACTIVE -> the fixed bullseye costume BOZA_CENTRE_ORDINAL (5 = boza-centre/core/01,
    #                              arcade code 0x3a); the centre never animates, so `slot code` is not read.
    #   EITHER            HIT    -> IDENTICAL to the Barra/Logram crater (handle_bomb_explosion): the shared
    #                              burst for the first GROUND_CRATER_START_FRAMES (floor(timer/8)), then the
    #                              PERSISTENT two-frame flickering crater (floor(timer/4) mod 2), scrolling
    #                              until it culls. No free-on-clock. The clone writes no state.
    blocks = Blocks(BOZA_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("boza clone slot", BOZA_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(GROUND_SLOTS[0], GROUND_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("boza clone slot", BOZA_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_boza = blocks.op_eq(
        blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(BOZA_LOGRAM_TYPE)
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
        number(BOZA_EXPLODE_BASE_ORDINAL),
        blocks.op_floor(
            blocks.op_div(
                blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()),
                number(GROUND_EXPLOSION_PHASE_FRAMES),
            )
        ),
    )
    crater_ordinal = blocks.op_add(
        number(BOZA_CRATER_BASE_ORDINAL),
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

    # ACTIVE costume: the CENTRE (link == 0) shows the fixed bullseye; an OUTER mirrors its `slot code` dome.
    active_costume = blocks.add("control_if_else")
    is_centre = blocks.op_eq(blocks.list_item("slot link", SLOT_LINK_ID, slotvar()), number(0))
    blocks.blocks[active_costume]["inputs"]["CONDITION"] = [2, is_centre]
    blocks.blocks[is_centre]["parent"] = active_costume
    # The centre's bullseye is a single fixed costume (BOZA_CENTRE_ORDINAL = 5), so select it by name.
    blocks.substack(active_costume, [blocks.switch_costume("boza-centre/core/01")])
    blocks.substack(
        active_costume,
        [blocks.switch_costume_expr(blocks.list_item("slot code", SLOT_CODE_ID, slotvar()))],
        name="SUBSTACK2",
    )

    state_render = blocks.add("control_if_else")
    is_hit = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT))
    blocks.blocks[state_render]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = state_render
    blocks.substack(state_render, [hit_costume])
    blocks.substack(state_render, [active_costume], name="SUBSTACK2")

    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_boza]
    blocks.blocks[is_boza]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.go_expr(stage_x, stage_y),
            state_render,
            blocks.add("looks_setsizeto", inputs={"SIZE": number(GROUND_RENDER_SIZE)}),
            # WPN-04 layering: like the Barra/Logram, the Boza dome/bullseye/crater stays on the terrain,
            # under the craft and the bomb sight — its static layerOrder is already above the (never-fronting)
            # terrain, so it is left unfronted. See barra_blocks.
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(clone, [blocks.hide(), loop])
    return blocks.blocks


def _passive_ground_blocks(
    target: str,
    clone_slot_name: str,
    clone_slot_id: str,
    slot_type: int,
    idle_costume: str,
    explode_base: int,
    crater_base: int,
) -> dict[str, dict[str, Any]]:
    # GND shared renderer for a single-slot passive-or-crater ground family (Barra crater model): one
    # persistent clone per GROUND slot (1..16), a pure per-tick function of its slot's live state — an idle
    # costume while ACTIVE; while HIT the bomb-explosion clock (slot timer, zeroed by the detector) plays
    # the shared solv_death burst (floor(timer/8)) then a PERSISTENT flickering crater. Identical to
    # barra_blocks except target / clone-slot var / type / idle costume / costume ordinals — the Zolbak and
    # Derota renderers are this same model (a Zolbak's AI-level side-effect and a Derota's firing both live
    # in their update procs, not in the pure renderer). See barra_blocks for the layering rationale.
    blocks = Blocks(target)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable(clone_slot_name, clone_slot_id)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(GROUND_SLOTS[0], GROUND_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var(clone_slot_name, clone_slot_id, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_family = blocks.op_eq(
        blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(slot_type)
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
    explode_ordinal = blocks.op_add(
        number(explode_base),
        blocks.op_floor(
            blocks.op_div(
                blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()),
                number(GROUND_EXPLOSION_PHASE_FRAMES),
            )
        ),
    )
    crater_ordinal = blocks.op_add(
        number(crater_base),
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
    blocks.substack(state_render, [blocks.switch_costume(idle_costume)], name="SUBSTACK2")

    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_family]
    blocks.blocks[is_family]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.go_expr(stage_x, stage_y),
            state_render,
            blocks.add("looks_setsizeto", inputs={"SIZE": number(GROUND_RENDER_SIZE)}),
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(clone, [blocks.hide(), loop])
    return blocks.blocks


def zolbak_blocks() -> dict[str, dict[str, Any]]:
    # GND-02 (ground.zolbak #85) renderer — the Barra crater model with the Zolbak dome costume. The
    # AI-level reduction on death is in `update zolbak`; the renderer is a pure function of slot state.
    return _passive_ground_blocks(
        ZOLBAK_TARGET,
        "zolbak clone slot",
        ZOLBAK_CLONE_SLOT_ID,
        ZOLBAK_TYPE,
        "zolbak/idle/01",
        ZOLBAK_EXPLODE_BASE_ORDINAL,
        ZOLBAK_CRATER_BASE_ORDINAL,
    )


def derota_blocks() -> dict[str, dict[str, Any]]:
    # GND-04 (ground.derota #86) renderer — the Barra crater model with the Derota turret costume. A Derota
    # craters on a bomb hit exactly like the Barra; its periodic firing is in `update derota`.
    return _passive_ground_blocks(
        DEROTA_TARGET,
        "derota clone slot",
        DEROTA_CLONE_SLOT_ID,
        DEROTA_TYPE,
        "derota/idle/01",
        DEROTA_EXPLODE_BASE_ORDINAL,
        DEROTA_CRATER_BASE_ORDINAL,
    )


def garu_derota_blocks() -> dict[str, dict[str, Any]]:
    # GND-04 (ground.derota #86) Garu Derota renderer (game_director owns these blocks; sprite_extractor
    # owns the costumes). Structurally identical to the Garu Barra renderer — one clone per GROUND slot,
    # branching on the slot's STATE because base and node share GARU_DEROTA_TYPE:
    #   SLOT_GARU_BASE -> the 2x2 indestructible base, pulsing its two 32-px frames on the global tick
    #                     (closed / open-firing centre; stands in for the arcade's pulsing_colour_1 base).
    #   SLOT_ACTIVE    -> the destructible FIRING node's turret (garu-derota/node, reused from derota/idle).
    #   SLOT_HIT       -> the node's explode-and-remove burst (floor(timer/4)); `update garu derota` removes
    #                     the slot when the burst finishes, so the clone hides next tick — no crater.
    # The node's firing is in `update garu derota`; the renderer is a pure function of slot state.
    blocks = Blocks(GARU_DEROTA_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("garu derota clone slot", GARU_DEROTA_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(GROUND_SLOTS[0], GROUND_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("garu derota clone slot", GARU_DEROTA_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_garu = blocks.op_eq(
        blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(GARU_DEROTA_TYPE)
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
    burst_ordinal = blocks.op_add(
        number(GARU_DEROTA_EXPLODE_BASE_ORDINAL),
        blocks.op_floor(
            blocks.op_div(
                blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()),
                number(GARU_EXPLOSION_PHASE_FRAMES),
            )
        ),
    )
    base_or_node = blocks.add("control_if_else")
    is_base = blocks.op_eq(
        blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_GARU_BASE)
    )
    blocks.blocks[base_or_node]["inputs"]["CONDITION"] = [2, is_base]
    blocks.blocks[is_base]["parent"] = base_or_node
    # The indestructible base holds the open red firing-centre frame (02) statically (see the Garu Barra
    # renderer): the arcade's pulsing_colour_1 red-light glow is a recorded port necessity, not reproduced
    # (Scratch cannot pulse the red alone; the crop-only sheet has no red-off cell; operator decision
    # 2026-09-24).
    blocks.substack(base_or_node, [blocks.switch_costume(GARU_DEROTA_BASE_EXPOSED_COSTUME)])
    blocks.substack(base_or_node, [blocks.switch_costume("derota/idle/01")], name="SUBSTACK2")

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
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(clone, [blocks.hide(), loop])
    return blocks.blocks


def grobda_blocks() -> dict[str, dict[str, Any]]:
    # GND-06 (ground.grobda #88) renderer (game_director owns these blocks; sprite_extractor owns the
    # costumes). One persistent clone per GROUND slot (1..16), a pure per-tick function of the slot's live
    # state — the 12 variants share ONE tank costume set. While ACTIVE the tread ROLL is a render-only
    # function of the GLOBAL tick + `slot dx` (the walk never writes `slot code`, and `slot timer` is busy
    # with the reaction countdown): a stopped tank (dx == GROBDA_STOPPED_DX) holds roll frame 1; a forward
    # tank cycles the 4 tread frames, a darting tank cycles them faster, and a reversing tank cycles them
    # backward. While HIT a LAND Grobda plays the shared solv_death burst then a PERSISTENT flickering crater
    # (the Barra model); a WATER Grobda plays the burst at the Garu-node cadence and VANISHES (the slot is
    # culled by `update grobda`, so the clone hides next tick — no crater). Position/scale match every other
    # ground family.
    blocks = Blocks(GROBDA_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("grobda clone slot", GROBDA_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(GROUND_SLOTS[0], GROUND_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("grobda clone slot", GROBDA_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_grobda = functools.reduce(
        blocks.op_or,
        (blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(t)) for t in GROBDA_TYPES),
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
    cur_dx = lambda: blocks.list_item("slot dx", SLOT_DX_ID, slotvar())
    # A forward tread phase at `period`: (floor(tick/period) mod FRAMES) + 1 -> ordinal 1..4. A reverse phase
    # runs the same cycle backward. Each reporter is rebuilt fresh (a reporter binds to one parent only).
    fwd_ordinal = lambda period: blocks.op_add(
        blocks.op_mod(
            blocks.op_floor(blocks.op_div(variable("tick", TICK_ID), number(period))),
            number(GROBDA_ROLL_FRAME_COUNT),
        ),
        number(1),
    )
    reverse_ordinal = blocks.op_sub(
        number(GROBDA_ROLL_FRAME_COUNT),
        blocks.op_mod(
            blocks.op_floor(blocks.op_div(variable("tick", TICK_ID), number(GROBDA_ROLL_PERIOD))),
            number(GROBDA_ROLL_FRAME_COUNT),
        ),
    )
    # ACTIVE tread costume: stopped -> hold frame 1; dart -> fast forward; back -> reverse; else forward.
    def _if_else(cond: str, then_body: list[str], else_body: list[str]) -> str:
        node = blocks.add("control_if_else")
        blocks.blocks[node]["inputs"]["CONDITION"] = [2, cond]
        blocks.blocks[cond]["parent"] = node
        blocks.substack(node, then_body)
        blocks.substack(node, else_body, name="SUBSTACK2")
        return node
    roll_costume = _if_else(
        blocks.op_eq(cur_dx(), number(GROBDA_STOPPED_DX)),
        [blocks.switch_costume("grobda/roll/01")],
        [
            _if_else(
                blocks.op_eq(cur_dx(), number(GROBDA_DART_DX)),
                [blocks.switch_costume_expr(fwd_ordinal(GROBDA_ROLL_PERIOD_FAST))],
                [
                    _if_else(
                        blocks.op_lt(cur_dx(), number(GROBDA_STOPPED_DX)),
                        [blocks.switch_costume_expr(reverse_ordinal)],
                        [blocks.switch_costume_expr(fwd_ordinal(GROBDA_ROLL_PERIOD))],
                    )
                ],
            )
        ],
    )
    # HIT costume: land craters (burst -> persistent flicker), water bursts at the Garu-node cadence & vanishes.
    is_water = functools.reduce(
        blocks.op_or,
        (blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(t)) for t in GROBDA_WATER_TYPES),
    )
    land_explode = blocks.op_add(
        number(GROBDA_EXPLODE_BASE_ORDINAL),
        blocks.op_floor(
            blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(GROUND_EXPLOSION_PHASE_FRAMES))
        ),
    )
    crater_ordinal = blocks.op_add(
        number(GROBDA_CRATER_BASE_ORDINAL),
        blocks.op_mod(
            blocks.op_floor(
                blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(GROUND_CRATER_FLICKER_FRAMES))
            ),
            number(2),
        ),
    )
    cratered = blocks.op_not(
        blocks.op_lt(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(GROUND_CRATER_START_FRAMES))
    )
    land_costume = _if_else(cratered, [blocks.switch_costume_expr(crater_ordinal)], [blocks.switch_costume_expr(land_explode)])
    water_explode = blocks.op_add(
        number(GROBDA_EXPLODE_BASE_ORDINAL),
        blocks.op_floor(
            blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(GARU_EXPLOSION_PHASE_FRAMES))
        ),
    )
    hit_costume = _if_else(is_water, [blocks.switch_costume_expr(water_explode)], [land_costume])

    state_render = _if_else(
        blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT)),
        [hit_costume],
        [roll_costume],
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_grobda]
    blocks.blocks[is_grobda]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.go_expr(stage_x, stage_y),
            state_render,
            blocks.add("looks_setsizeto", inputs={"SIZE": number(GROUND_RENDER_SIZE)}),
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(clone, [blocks.hide(), loop])
    return blocks.blocks


def domogram_blocks() -> dict[str, dict[str, Any]]:
    # GND-07 (ground.domogram #89) renderer (game_director owns these blocks; sprite_extractor owns the
    # costumes). One persistent clone per GROUND slot (1..16), a pure per-tick function of the slot's live
    # state. While ACTIVE the sprite ANIMATES only while firing: the arcade selects the costume from
    # `(_TYPE >> 2) & 7` indexing domogram_sprite_tbl (the walk never writes `slot code`), so the renderer
    # reads `slot fire timer` (_TYPE, the shot-animation timer, 0 when idle) the same way — anim index =
    # floor(_TYPE / 4) mod 8, mapped to a costume ordinal through the baked `domogram frame ord` list (idle
    # _TYPE 0 -> index 0 -> ordinal 1, the resting sprite). While HIT it craters PERSISTENTLY like the Barra:
    # the shared solv_death burst, then a flickering crater (a Domogram always craters on land — it has no
    # water variant). Position/scale match every other ground family.
    blocks = Blocks(DOMOGRAM_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("domogram clone slot", DOMOGRAM_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(GROUND_SLOTS[0], GROUND_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("domogram clone slot", DOMOGRAM_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_domogram = blocks.op_eq(
        blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(DOMOGRAM_TYPE)
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

    def _if_else(cond: str, then_body: list[str], else_body: list[str]) -> str:
        node = blocks.add("control_if_else")
        blocks.blocks[node]["inputs"]["CONDITION"] = [2, cond]
        blocks.blocks[cond]["parent"] = node
        blocks.substack(node, then_body)
        blocks.substack(node, else_body, name="SUBSTACK2")
        return node

    # ACTIVE sprite: the arcade's `(_TYPE >> 2) & 7` index into domogram_sprite_tbl, ported as a lookup of the
    # baked `domogram frame ord` list at (floor(_TYPE/4) mod 8) + 1 (1-based Scratch list). _TYPE is even and
    # <= 22 when rendered, so the live index is 0..5; the list's two trailing entries are inert padding.
    anim_index = blocks.op_add(
        blocks.op_mod(
            blocks.op_floor(
                blocks.op_div(
                    blocks.list_item("slot fire timer", SLOT_FIRE_TIMER_ID, slotvar()), number(4)
                )
            ),
            number(8),
        ),
        number(1),
    )
    active_costume = blocks.switch_costume_expr(
        blocks.list_item("domogram frame ord", DOMOGRAM_FRAME_ORD_ID, anim_index)
    )

    # HIT sprite: the Barra land-crater model — the shared burst then a persistent 2-frame flicker.
    land_explode = blocks.op_add(
        number(DOMOGRAM_EXPLODE_BASE_ORDINAL),
        blocks.op_floor(
            blocks.op_div(
                blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(GROUND_EXPLOSION_PHASE_FRAMES)
            )
        ),
    )
    crater_ordinal = blocks.op_add(
        number(DOMOGRAM_CRATER_BASE_ORDINAL),
        blocks.op_mod(
            blocks.op_floor(
                blocks.op_div(
                    blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(GROUND_CRATER_FLICKER_FRAMES)
                )
            ),
            number(2),
        ),
    )
    cratered = blocks.op_not(
        blocks.op_lt(
            blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(GROUND_CRATER_START_FRAMES)
        )
    )
    hit_costume = _if_else(
        cratered, [blocks.switch_costume_expr(crater_ordinal)], [blocks.switch_costume_expr(land_explode)]
    )

    state_render = _if_else(
        blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT)),
        [hit_costume],
        [active_costume],
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_domogram]
    blocks.blocks[is_domogram]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.go_expr(stage_x, stage_y),
            state_render,
            blocks.add("looks_setsizeto", inputs={"SIZE": number(GROUND_RENDER_SIZE)}),
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


def zakato_blocks() -> dict[str, dict[str, Any]]:
    # AIR-07 Zakato renderer (game_director owns these blocks; sprite_extractor owns the costumes). One
    # persistent clone per flying slot (59..64), the same pool pattern as the Jara/Kapi: shown and
    # positioned when its slot holds ANY of the four base Zakato types, hidden otherwise. The clone writes
    # no state. It draws whichever of the four phases the slot's `slot state` names — the phase sequence the
    # update machine drives:
    #   SLOT_ACTIVE       the single static body (ordinal 1, arcade code 0x11).
    #   SLOT_TELEPORT     the teleport-in sparkle: the shared burst played REVERSED (the arcade sparkle is
    #                     the exploding six-frame set run backwards, 3986-3992), from the slot clock.
    #   SLOT_SELF_EXPLODE the self-destruct: the shared burst FORWARD at normal scale — a small pop.
    #   SLOT_HIT          the shot kill: the shared burst FORWARD, doubling at the big phase like every
    #                     other flying kill (so it reads bigger than the self-destruct).
    # All three animated phases run the same solv_death stand-in (record note in the constants); the
    # Zakato's own teleport/burst sprites are a deferred cosmetic, as with the other families.
    blocks = Blocks(ZAKATO_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("zakato clone slot", ZAKATO_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(FLYING_SLOTS[0], FLYING_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("zakato clone slot", ZAKATO_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    stype = lambda: blocks.list_item("slot type", SLOT_TYPE_ID, slotvar())
    # AIR-07 base variants + AIR-08 Brag variants: all six teleport in, hold a static body while active,
    # and play the shared burst for their self-destruct / shot-kill — the same four render phases — so the
    # Brag rnd/closeY fold into this renderer (they need no target of their own). The Garu Zakato does NOT
    # (no teleport) and has its own Spario-factory renderer instead.
    is_zakato = blocks.op_or(
        blocks.op_or(
            blocks.op_or(blocks.op_eq(stype(), number(ZAKATO_SLOW_TYPE)), blocks.op_eq(stype(), number(ZAKATO_CLOSEY_TYPE))),
            blocks.op_or(blocks.op_eq(stype(), number(ZAKATO_FAST_TYPE)), blocks.op_eq(stype(), number(ZAKATO_CONT_TYPE))),
        ),
        blocks.op_or(blocks.op_eq(stype(), number(BRAG_ZAKATO_RND_TYPE)), blocks.op_eq(stype(), number(BRAG_ZAKATO_CLOSEY_TYPE))),
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
    # Burst phase = floor(slot timer / phase-frames) — the arcade `TIMER>>2`. Fresh per read (a reporter
    # attaches to only one parent). Both the teleport and self-destruct clocks and the shared-hit clock use
    # the same 4-frame period (ZAKATO_ANIM_PHASE_FRAMES == TOROID_EXPLOSION_PHASE_FRAMES).
    phase = lambda: blocks.op_floor(
        blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(ZAKATO_ANIM_PHASE_FRAMES))
    )
    # SLOT_SELF_EXPLODE vs SLOT_ACTIVE (the innermost pair): the self-destruct plays the burst forward at
    # normal scale; the active phase holds the static body.
    self_or_active = blocks.add("control_if_else")
    is_self = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_SELF_EXPLODE))
    blocks.blocks[self_or_active]["inputs"]["CONDITION"] = [2, is_self]
    blocks.blocks[is_self]["parent"] = self_or_active
    blocks.substack(
        self_or_active,
        [
            blocks.switch_costume_expr(blocks.op_add(number(ZAKATO_BURST_ORDINAL_BASE), phase())),
            blocks.add("looks_setsizeto", inputs={"SIZE": number(ZAKATO_RENDER_SIZE)}),
        ],
    )
    blocks.substack(
        self_or_active,
        [
            # ACTIVE holds the static body — a fixed costume, so switch by name (switch_costume_expr
            # obscures a menu with a runtime reporter; for a constant the by-name switch is direct).
            blocks.switch_costume("zakato/body/01"),
            blocks.add("looks_setsizeto", inputs={"SIZE": number(ZAKATO_RENDER_SIZE)}),
        ],
        name="SUBSTACK2",
    )
    # SLOT_TELEPORT vs the rest: the sparkle plays the burst REVERSED (ordinal base + (PHASES-1 - phase)).
    tele_or_rest = blocks.add("control_if_else")
    is_tele = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_TELEPORT))
    blocks.blocks[tele_or_rest]["inputs"]["CONDITION"] = [2, is_tele]
    blocks.blocks[is_tele]["parent"] = tele_or_rest
    blocks.substack(
        tele_or_rest,
        [
            blocks.switch_costume_expr(
                blocks.op_sub(number(ZAKATO_BURST_ORDINAL_BASE + ZAKATO_ANIM_PHASES - 1), phase())
            ),
            blocks.add("looks_setsizeto", inputs={"SIZE": number(ZAKATO_RENDER_SIZE)}),
        ],
    )
    blocks.substack(tele_or_rest, [self_or_active], name="SUBSTACK2")
    # SLOT_HIT (the shared flying explosion): forward burst, doubling at the big phase — exactly the other
    # families' hit render.
    explode_ordinal = blocks.op_add(number(ZAKATO_BURST_ORDINAL_BASE), phase())
    size_branch = blocks.add("control_if_else")
    is_big = blocks.op_eq(phase(), number(TOROID_BIG_PHASE))
    blocks.blocks[size_branch]["inputs"]["CONDITION"] = [2, is_big]
    blocks.blocks[is_big]["parent"] = size_branch
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(TOROID_EXPLODE_SIZE)})])
    blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(ZAKATO_RENDER_SIZE)})], name="SUBSTACK2")
    state_render = blocks.add("control_if_else")
    is_hit = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT))
    blocks.blocks[state_render]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = state_render
    blocks.substack(state_render, [blocks.switch_costume_expr(explode_ordinal), size_branch])
    blocks.substack(state_render, [tele_or_rest], name="SUBSTACK2")

    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_zakato]
    blocks.blocks[is_zakato]["parent"] = render
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


def _spario_blocks(target: str, clone_var_name: str, clone_var_id: str, type_code: int, big_phase: bool) -> dict[str, dict[str, Any]]:
    # AIR-10 shared Spario renderer (game_director owns these blocks; the costumes are the Zakato body
    # stand-in + the shared solv_death burst mirrored on in expected_project). One persistent clone per
    # flying slot (59..64), the same pool pattern as the Jara/Zakato: shown and positioned when its slot
    # holds `type_code`, hidden otherwise. The clone writes no state. While ACTIVE it holds the static body
    # stand-in (ordinal 1); on a hit it plays the shared burst FORWARD from the slot clock. `big_phase`
    # selects the burst scale: the Brag uses the shared ~20-frame flying kill (doubling at the 2x big phase,
    # like every other family); the Giddo's SHORT 8-frame own-burst (giddo_spario_hit 5241-5252) plays at
    # normal scale — a small pop, never reaching the big phase (its handler frees it at frame 8).
    blocks = Blocks(target)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable(clone_var_name, clone_var_id)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(FLYING_SLOTS[0], FLYING_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var(clone_var_name, clone_var_id, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_family = blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(type_code))
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
    # Shared explosion on a hit: forward burst from the slot clock (the arcade `TIMER>>2`, fresh per read).
    explode_ordinal = blocks.op_add(
        number(SPARIO_BURST_ORDINAL_BASE),
        blocks.op_floor(blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))),
    )
    hit_body: list[str] = [blocks.switch_costume_expr(explode_ordinal)]
    if big_phase:
        # The Brag kill doubles at the 2x big phase like every other flying kill.
        size_branch = blocks.add("control_if_else")
        phase_for_size = blocks.op_floor(
            blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(TOROID_EXPLOSION_PHASE_FRAMES))
        )
        is_big = blocks.op_eq(phase_for_size, number(TOROID_BIG_PHASE))
        blocks.blocks[size_branch]["inputs"]["CONDITION"] = [2, is_big]
        blocks.blocks[is_big]["parent"] = size_branch
        blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(TOROID_EXPLODE_SIZE)})])
        blocks.substack(size_branch, [blocks.add("looks_setsizeto", inputs={"SIZE": number(SPARIO_RENDER_SIZE)})], name="SUBSTACK2")
        hit_body.append(size_branch)
    else:
        # The Giddo's short own-burst stays at normal scale — a small pop.
        hit_body.append(blocks.add("looks_setsizeto", inputs={"SIZE": number(SPARIO_RENDER_SIZE)}))
    state_render = blocks.add("control_if_else")
    is_hit = blocks.op_eq(blocks.list_item("slot state", SLOT_STATE_ID, slotvar()), number(SLOT_HIT))
    blocks.blocks[state_render]["inputs"]["CONDITION"] = [2, is_hit]
    blocks.blocks[is_hit]["parent"] = state_render
    blocks.substack(state_render, hit_body)
    blocks.substack(
        state_render,
        [
            # The body stand-in is a fixed costume (the mirrored-in Zakato blob, ordinal 1), so switch by
            # name — a constant costume needs no runtime reporter, exactly like the Zakato active body.
            blocks.switch_costume("zakato/body/01"),
            blocks.add("looks_setsizeto", inputs={"SIZE": number(SPARIO_RENDER_SIZE)}),
        ],
        name="SUBSTACK2",
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_family]
    blocks.blocks[is_family]["parent"] = render
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


def giddo_spario_blocks() -> dict[str, dict[str, Any]]:
    # AIR-10: the Giddo Spario clone pool — its SHORT own-burst plays the shared frames at normal scale.
    return _spario_blocks(
        GIDDO_SPARIO_TARGET, "giddo spario clone slot", GIDDO_SPARIO_CLONE_SLOT_ID, GIDDO_SPARIO_TYPE, big_phase=False
    )


def brag_spario_blocks() -> dict[str, dict[str, Any]]:
    # AIR-10: the Brag Spario clone pool — its kill uses the shared ~20-frame flying burst (big-phase 2x).
    return _spario_blocks(
        BRAG_SPARIO_TARGET, "brag spario clone slot", BRAG_SPARIO_CLONE_SLOT_ID, BRAG_SPARIO_TYPE, big_phase=True
    )


def garu_zakato_blocks() -> dict[str, dict[str, Any]]:
    # AIR-08: the Garu Zakato clone pool — no teleport phase (ACTIVE body stand-in + the shared ~20-frame
    # flying kill burst, big-phase 2x), so it reuses the shared Spario renderer factory. When it DETONATES
    # (fuse elapsed) it frees its own slot with no burst, so the clone simply hides — the ring bullets and
    # the 4 Brag Sparios it spawns are drawn by their own pools.
    return _spario_blocks(
        GARU_ZAKATO_TARGET, "garu zakato clone slot", GARU_ZAKATO_CLONE_SLOT_ID, GARU_ZAKATO_TYPE, big_phase=True
    )


def bacura_blocks() -> dict[str, dict[str, Any]]:
    # AIR-11 Bacura renderer (game_director owns the blocks; the eight tumble costumes are mirrored on in
    # expected_project). One persistent clone per BACURA-BAND slot (17..32), created on director enter while
    # playing and cleared on stop. Each clone shows the slab at its slot's mapped position when the slot holds
    # a Bacura, else hides. There is NO hit/explosion phase — the Bacura is never destroyed — so unlike the
    # flying families this has no burst branch. But the slab is NOT static: the arcade `handle_01_Bacura`
    # (xevious_main.68k 4253-4262) resumes every frame and reselects the sprite CODE from the live _X, so the
    # slab TUMBLES through the 8 frames of bacura_sprite_tbl as it drifts. Port `slot x` carries the same
    # 32-units-per-pixel scale as arcade `_X`, so the frame index = (floor(slot x / 128)) mod 8 = (_X>>7)&7 and
    # the shown costume is bacura/slab/0{index+1}. The clone writes no state (the walk owns the slot). It is
    # safe to key rendering on `slot type == BACURA_TYPE` here even though that value collides with SHOT_TYPE:
    # these clones are bound to BACURA-band slots, which only ever hold a Bacura (0 or BACURA_TYPE) — the
    # type-vs-band hazard is only in the walk.
    blocks = Blocks(BACURA_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("bacura clone slot", BACURA_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(BACURA_SLOTS[0], BACURA_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("bacura clone slot", BACURA_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_bacura = blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(BACURA_TYPE))
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
    # Position-driven tumble: frame index = (floor(slot x / 128)) mod 8 = arcade (_X>>7)&7, costume name
    # "bacura/slab/0" + (index+1). The join's STRING1 is a text literal; STRING2 is the (index+1) reporter.
    frame_index = blocks.op_mod(
        blocks.op_floor(
            blocks.op_div(
                blocks.list_item("slot x", SLOT_X_ID, slotvar()),
                number(BACURA_TUMBLE_UNITS_PER_FRAME),
            )
        ),
        number(BACURA_TUMBLE_FRAMES),
    )
    costume_name = blocks.op_join(
        text("bacura/slab/0"), blocks.op_add(frame_index, number(1))
    )
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_bacura]
    blocks.blocks[is_bacura]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.go_expr(stage_x, stage_y),
            blocks.switch_costume_expr(costume_name),
            blocks.add("looks_setsizeto", inputs={"SIZE": number(BACURA_RENDER_SIZE)}),
            blocks.to_front(),
            blocks.show(),
        ],
    )
    blocks.substack(render, [blocks.hide()], name="SUBSTACK2")
    blocks.substack(loop, [render])
    blocks.chain(clone, [blocks.hide(), loop])
    return blocks.blocks


def sheonite_blocks() -> dict[str, dict[str, Any]]:
    # AIR-09 Sheonite renderer (game_director owns the blocks; the ten costumes are mirrored on in
    # expected_project). One persistent clone per flying slot (59..64), the same pool pattern as the Jara,
    # shown and positioned when its slot holds EITHER Sheonite type (0x31 right / 0x32 left), hidden
    # otherwise. The clone writes no state. There is NO hit/explosion phase — the pair is inert, never
    # SLOT_HIT — so unlike the killable flyers this has no burst branch. Animation is render-only, driven by
    # the slot's `slot flag` (phase) and `slot timer` (clock): in HOME/LOCK/RETREAT it cycles the 4 spin
    # frames (ordinals 1..4); while COMBINE (docking) it cycles the 3 combine frames for its side (right
    # ordinals 5..7, left 8..10). Costume ordinals 1..10 == arcade sprite codes 0x30..0x39; the exact
    # per-side arcade code permutation is a cosmetic simplified to a plain cycle (recorded as a port note).
    blocks = Blocks(SHEONITE_TARGET)
    common_stop(blocks, hide=True, clones=True)
    slotvar = lambda: variable("sheonite clone slot", SHEONITE_CLONE_SLOT_ID)

    enter = blocks.receive("director enter")
    spawn_body: list[str] = []
    for slot in range(FLYING_SLOTS[0], FLYING_SLOTS[1] + 1):
        spawn_body += [
            blocks.set_var("sheonite clone slot", SHEONITE_CLONE_SLOT_ID, number(slot)),
            blocks.create_clone(),
        ]
    blocks.chain(enter, [blocks.if_state("playing", spawn_body)])

    clone = blocks.add("control_start_as_clone", top_level=True)
    loop = blocks.add("control_repeat_until")
    loop_condition = blocks.not_state(loop, "playing")
    blocks.blocks[loop]["inputs"]["CONDITION"] = [2, loop_condition]
    is_sheonite = blocks.op_or(
        blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(RIGHT_SHEONITE_TYPE)),
        blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(LEFT_SHEONITE_TYPE)),
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
    # Render-only animation clock. A fresh reporter per read (a reporter cannot be shared across parents —
    # the first parent steals it).
    spin_phase = lambda: blocks.op_mod(
        blocks.op_floor(blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(SHEONITE_ANIM_PERIOD))),
        number(SHEONITE_SPIN_FRAMES),
    )
    combine_phase = lambda: blocks.op_mod(
        blocks.op_floor(blocks.op_div(blocks.list_item("slot timer", SLOT_TIMER_ID, slotvar()), number(SHEONITE_ANIM_PERIOD))),
        number(SHEONITE_COMBINE_ANIM_FRAMES),
    )
    # Combine costume: the base ordinal depends on the side (right 5..7 / left 8..10).
    combine_costume = blocks.add("control_if_else")
    is_right = blocks.op_eq(blocks.list_item("slot type", SLOT_TYPE_ID, slotvar()), number(RIGHT_SHEONITE_TYPE))
    blocks.blocks[combine_costume]["inputs"]["CONDITION"] = [2, is_right]
    blocks.blocks[is_right]["parent"] = combine_costume
    blocks.substack(combine_costume, [blocks.switch_costume_expr(blocks.op_add(combine_phase(), number(SHEONITE_RIGHT_COMBINE_BASE_ORDINAL)))])
    blocks.substack(combine_costume, [blocks.switch_costume_expr(blocks.op_add(combine_phase(), number(SHEONITE_LEFT_COMBINE_BASE_ORDINAL)))], name="SUBSTACK2")
    # Phase select: COMBINE (slot flag == 2) shows the combine cycle; every other phase shows the spin cycle.
    costume_sel = blocks.add("control_if_else")
    is_combine = blocks.op_eq(blocks.list_item("slot flag", SLOT_FLAG_ID, slotvar()), number(SHEONITE_PHASE_COMBINE))
    blocks.blocks[costume_sel]["inputs"]["CONDITION"] = [2, is_combine]
    blocks.blocks[is_combine]["parent"] = costume_sel
    blocks.substack(costume_sel, [combine_costume])
    blocks.substack(costume_sel, [blocks.switch_costume_expr(blocks.op_add(spin_phase(), number(SHEONITE_SPIN_BASE_ORDINAL)))], name="SUBSTACK2")
    render = blocks.add("control_if_else")
    blocks.blocks[render]["inputs"]["CONDITION"] = [2, is_sheonite]
    blocks.blocks[is_sheonite]["parent"] = render
    blocks.substack(
        render,
        [
            blocks.go_expr(stage_x, stage_y),
            costume_sel,
            blocks.add("looks_setsizeto", inputs={"SIZE": number(SHEONITE_RENDER_SIZE)}),
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
    _ensure_gameplay_target(result, ZAKATO_TARGET)
    _ensure_gameplay_target(result, GIDDO_SPARIO_TARGET)
    _ensure_gameplay_target(result, BRAG_SPARIO_TARGET)
    _ensure_gameplay_target(result, GARU_ZAKATO_TARGET)
    _ensure_gameplay_target(result, BACURA_TARGET)
    _ensure_gameplay_target(result, SHEONITE_TARGET)
    _ensure_gameplay_target(result, BARRA_TARGET)
    _ensure_gameplay_target(result, GARU_TARGET)
    _ensure_gameplay_target(result, LOGRAM_TARGET)
    _ensure_gameplay_target(result, ZOLBAK_TARGET)
    _ensure_gameplay_target(result, DEROTA_TARGET)
    _ensure_gameplay_target(result, GARU_DEROTA_TARGET)
    _ensure_gameplay_target(result, BOZA_TARGET)
    _ensure_gameplay_target(result, GROBDA_TARGET)
    _ensure_gameplay_target(result, DOMOGRAM_TARGET)
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
    # AIR-07: the Zakato renderer mirrors its single active body frame (ordinal 1, arcade code 0x11), then
    # the shared explosion frames (the same solv_death burst appended after it, ordinals 2..9) — which the
    # teleport (reversed), self-destruct (forward) and shot-kill (forward) phases all draw from, the
    # deferred-family-burst stand-in every flying family uses.
    zakato = next((t for t in result["targets"] if t.get("name") == ZAKATO_TARGET), None)
    if proof is not None and zakato is not None:
        zakato["costumes"] = proof_by_family("zakato/")
        if death is not None:
            zakato["costumes"].extend(copy.deepcopy(death["costumes"]))
        zakato["currentCostume"] = 0
    # AIR-10: the Giddo and Brag Spario renderers both mirror the ZAKATO body frame as their body stand-in
    # (ordinal 1) — the CrazyCarl aerial rip carries no Spario sprite, so the Zakato blob stands in as a
    # DEFERRED cosmetic (reason recorded in the constants and the mechanics record) — then the shared
    # solv_death burst (ordinals 2..9) their hit draws from. Idempotent; a no-op when any source is absent.
    for spario_name in (GIDDO_SPARIO_TARGET, BRAG_SPARIO_TARGET, GARU_ZAKATO_TARGET):
        spario = next((t for t in result["targets"] if t.get("name") == spario_name), None)
        if proof is not None and spario is not None:
            spario["costumes"] = proof_by_family("zakato/")
            if death is not None:
                spario["costumes"].extend(copy.deepcopy(death["costumes"]))
            spario["currentCostume"] = 0
    # AIR-11: the Bacura renderer mirrors its eight tumble frames (bacura/slab/01..08, ordinals 1..8) — and
    # NOTHING else. The Bacura is never destroyed, so unlike every flying family it appends NO shared
    # solv_death burst (it has no HIT/explosion phase at all). The renderer picks one of these eight by
    # position each frame, so the slab tumbles as it drifts. Idempotent; a no-op when the proof source is
    # absent (generation runs to a fixpoint).
    bacura = next((t for t in result["targets"] if t.get("name") == BACURA_TARGET), None)
    if proof is not None and bacura is not None:
        bacura["costumes"] = proof_by_family("bacura/")
        bacura["currentCostume"] = 0
    # AIR-09: the Sheonite renderer mirrors its ten frames (sheonite/spin/01..04 then sheonite/combine/01..06,
    # ordinals 1..10 == arcade codes 0x30..0x39) — and NOTHING else. Like the Bacura the pair is inert (never
    # destroyed), so it appends NO shared solv_death burst (it has no HIT/explosion phase). The renderer picks
    # the spin cycle (1..4) or the per-side combine cycle (right 5..7 / left 8..10) by phase + clock.
    # Idempotent; a no-op when the proof source is absent (generation runs to a fixpoint).
    sheonite = next((t for t in result["targets"] if t.get("name") == SHEONITE_TARGET), None)
    if proof is not None and sheonite is not None:
        sheonite["costumes"] = proof_by_family("sheonite/")
        sheonite["currentCostume"] = 0
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
    # GND-01: the Garu Barra renderer mirrors its two 2x2 base frames (ordinals 1..2; the exposed base holds
    # ordinal 2, the red socket — ordinal 1 is retained as a crop but not rendered), then the node
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
    # GND-02 (ground.zolbak #85): the Zolbak renderer mirrors its single idle dome frame (ordinal 1), then the
    # shared explosion burst (ordinals 2..9) and the two crater frames (ordinals 10..11) — the SAME crater as
    # the Barra, since a bombed Zolbak runs the shared ground bomb pipeline. Zolbak never fires; its only
    # distinction is the on-death AI-level reduction, which lives in install_update_zolbak, not the renderer.
    # Idempotent; a no-op when any source is absent (generation runs to a fixpoint).
    zolbak = next((t for t in result["targets"] if t.get("name") == ZOLBAK_TARGET), None)
    if proof is not None and zolbak is not None:
        zolbak["costumes"] = proof_by_family("zolbak/")
        if death is not None:
            zolbak["costumes"].extend(copy.deepcopy(death["costumes"]))
        zolbak["costumes"].extend(proof_by_family("crater/"))
        zolbak["currentCostume"] = 0
    # GND-04 (ground.derota #86): the Derota renderer mirrors its single idle turret frame (ordinal 1), then the
    # shared explosion burst (ordinals 2..9) and the two crater frames (ordinals 10..11) — the SAME crater as
    # the Barra. Derota is a plain periodic turret (no open/close dome cycle); its firing lives in
    # install_update_derota. Idempotent; a no-op when any source is absent (generation runs to a fixpoint).
    derota = next((t for t in result["targets"] if t.get("name") == DEROTA_TARGET), None)
    if proof is not None and derota is not None:
        derota["costumes"] = proof_by_family("derota/")
        if death is not None:
            derota["costumes"].extend(copy.deepcopy(death["costumes"]))
        derota["costumes"].extend(proof_by_family("crater/"))
        derota["currentCostume"] = 0
    # GND-04 (ground.derota #86): the Garu Derota renderer mirrors its two 2x2 base frames (ordinals
    # 1..2; the exposed base holds ordinal 2, the red firing centre — ordinal 1 is retained as a crop but not
    # rendered), then the node turret frame (ordinal 3 — the node reuses the Derota turret bitmap, a documented
    # port necessity: the sheet has no separate small node cell and the node carries turret code 0x27), then
    # the shared explosion burst (ordinals 4..11) the node plays before it vanishes. The base is indestructible;
    # the node fires (install_update_garu_derota). Idempotent; a no-op when any source is absent.
    garu_derota = next((t for t in result["targets"] if t.get("name") == GARU_DEROTA_TARGET), None)
    if proof is not None and garu_derota is not None:
        garu_derota["costumes"] = proof_by_family("garu-derota/")
        garu_derota["costumes"].extend(proof_by_family("derota/"))
        if death is not None:
            garu_derota["costumes"].extend(copy.deepcopy(death["costumes"]))
        garu_derota["currentCostume"] = 0
    # GND-05 (ground.boza-logram #87): the Boza renderer mirrors the 4 Logram open/close dome frames (ordinals
    # 1..4 — the outer domes REUSE the identical arcade sprites 0x2C..0x2F, so they reference the same crops),
    # then the single centre bullseye frame (ordinal 5 = boza-centre/, arcade code 0x3a), then the shared
    # explosion burst (ordinals 6..13) and the two crater frames (ordinals 14..15) — the SAME crater as the
    # Barra, since both an outer and the centre run handle_bomb_explosion on a bomb hit. Idempotent; a no-op
    # when any source is absent (generation runs to a fixpoint).
    boza = next((t for t in result["targets"] if t.get("name") == BOZA_TARGET), None)
    if proof is not None and boza is not None:
        boza["costumes"] = proof_by_family("logram/")
        boza["costumes"].extend(proof_by_family("boza-centre/"))
        if death is not None:
            boza["costumes"].extend(copy.deepcopy(death["costumes"]))
        boza["costumes"].extend(proof_by_family("crater/"))
        boza["currentCostume"] = 0
    # GND-06 (ground.grobda #88): the Grobda renderer mirrors its 4 tank tread frames (grobda/roll/01..04,
    # ordinals 1..4 — the 12 variants share ONE tank costume set), then the shared explosion burst (ordinals
    # 5..12) that BOTH the land crater and the water explode-and-remove draw from, then the two crater frames
    # (ordinals 13..14) — the SAME crater as the Barra, since a bombed LAND Grobda runs handle_bomb_explosion
    # (a water Grobda never shows them; it vanishes). Idempotent; a no-op when any source is absent (fixpoint).
    grobda = next((t for t in result["targets"] if t.get("name") == GROBDA_TARGET), None)
    if proof is not None and grobda is not None:
        grobda["costumes"] = proof_by_family("grobda/")
        if death is not None:
            grobda["costumes"].extend(copy.deepcopy(death["costumes"]))
        grobda["costumes"].extend(proof_by_family("crater/"))
        grobda["currentCostume"] = 0
    # GND-07 (ground.domogram #89): the Domogram target mirrors its 4 idle/animation frames (ordinals 1..4),
    # then the shared solv_death burst (ordinals 5..12) and the two crater frames (13..14) — a land-only
    # cratering family, so the ordinal layout matches DOMOGRAM_EXPLODE_BASE_ORDINAL / DOMOGRAM_CRATER_BASE_ORDINAL.
    domogram = next((t for t in result["targets"] if t.get("name") == DOMOGRAM_TARGET), None)
    if proof is not None and domogram is not None:
        domogram["costumes"] = proof_by_family("domogram/")
        if death is not None:
            domogram["costumes"].extend(copy.deepcopy(death["costumes"]))
        domogram["costumes"].extend(proof_by_family("crater/"))
        domogram["currentCostume"] = 0
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
        RADIATING_ANGLE_ID,
        # AIR-08 Garu Zakato detonation temps: the captured Garu x/y and its own slot index, used to
        # place the 16-bullet ring and the 4 adjacent Brag Sparios before the Garu frees its slot.
        GARU_DET_X_ID,
        GARU_DET_Y_ID,
        GARU_DET_SLOT_ID,
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
        # DEBUG (tracked for removal, #119): the G-key GROUND family-cycle cursor.
        DEBUG_GROUND_INDEX_ID,
        # DEBUG (tracked for removal, #119): the P-key freeze/resume toggle and its rising-edge sample.
        PAUSED_ID,
        PAUSE_KEY_HELD_ID,
        # AIR-11: the live Bacura spawn pump's state (main_fn_5 inc counter + main_fn_3 init loop).
        NUM_BACURA_ID,
        BACURA_INC_CNT_ID,
        ONE_SECOND_CNTR_ID,
        BACURA_SEED_SLOT_ID,
        # AIR-09: the Sheonite escort's schedule on/off flag plus the two per-tick update temps (phase
        # snapshot + resolved lateral lock cell).
        SHEONITE_END_FLAG_ID,
        SHEONITE_PHASE_TMP_ID,
        SHEONITE_LOCK_COL_ID,
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
        # AIR-12 radiating-spread emission: the caller-supplied explicit direction (0..31) the shared
        # `emit radiating bullet` reads; transient, default 0 (set by the ring/fan emitters, slice 11).
        RADIATING_ANGLE_ID: ["radiating angle", 0],
        # AIR-08 Garu Zakato detonation temps (slice 11): the captured Garu x/y and its own slot index.
        GARU_DET_X_ID: ["garu det x", 0],
        GARU_DET_Y_ID: ["garu det y", 0],
        GARU_DET_SLOT_ID: ["garu det slot", 0],
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
        # DEBUG (tracked for removal, #119): the G-key GROUND family-cycle cursor (0-based into
        # DEBUG_GROUND_FAMILIES); starts at the first family.
        DEBUG_GROUND_INDEX_ID: ["debug ground index", 0],
        # DEBUG (tracked for removal, #119): the P-key freeze toggle (1 = frozen) and its previous-tick
        # P sample for rising-edge detection; both start at 0 so the walk runs and the harness is unaffected.
        PAUSED_ID: ["debug paused", 0],
        PAUSE_KEY_HELD_ID: ["debug pause key held", 0],
        # AIR-11: Bacura live-spawn pump state (re-topped per area in _enter_area_top).
        NUM_BACURA_ID: ["num bacura", 0],
        BACURA_INC_CNT_ID: ["bacura inc cnt", 0],
        ONE_SECOND_CNTR_ID: ["one second cntr", 0],
        BACURA_SEED_SLOT_ID: ["bacura seed slot", 0],
        # AIR-09: the Sheonite escort's schedule on/off flag (cleared per area in _enter_area_top) and its
        # two per-tick update temps (phase snapshot + resolved lateral lock cell).
        SHEONITE_END_FLAG_ID: ["sheonite end flag", 0],
        SHEONITE_PHASE_TMP_ID: ["sheonite phase", 0],
        SHEONITE_LOCK_COL_ID: ["sheonite lock col", 0],
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
        AIM_DY_64_ID,
        AIM_DX_64_ID,
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
        SCHEDULE_DOMOGRAM_PATH_START_ID,
        SCHEDULE_DOMOGRAM_PATH_COUNT_ID,
        DOMOGRAM_VECTOR_DX_ID,
        DOMOGRAM_VECTOR_DY_ID,
        DOMOGRAM_PATH_DURATION_ID,
        DOMOGRAM_PATH_VECTOR_ID,
        DOMOGRAM_FRAME_ORD_ID,
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
        AIM_DY_64_ID: ["aim dy 64", list(AIM_DY_64)],
        AIM_DX_64_ID: ["aim dx 64", list(AIM_DX_64)],
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
        # GND-07 (ground.domogram #89): two more per-record schedule columns (same length as the others,
        # 0 on every non-Domogram row) — the 1-based start index of each Domogram's path in the shared step
        # columns, and its step count. The follower seeds `slot link` from start and `slot vec left` from count.
        SCHEDULE_DOMOGRAM_PATH_START_ID: ["schedule domogram path start", list(SCHEDULE_DOMOGRAM_PATH_START)],
        SCHEDULE_DOMOGRAM_PATH_COUNT_ID: ["schedule domogram path count", list(SCHEDULE_DOMOGRAM_PATH_COUNT)],
        # GND-07 shared read-only runtime tables: the 32-entry (dx=scroll/depth, dy=lateral) vector table
        # (domogram_vector_tbl), the flattened path steps (duration + vector index, concatenated across every
        # instance in schedule order), and the anim-index -> costume-ordinal render map (domogram_sprite_tbl).
        DOMOGRAM_VECTOR_DX_ID: ["domogram vector dx", list(DOMOGRAM_VECTOR_DX)],
        DOMOGRAM_VECTOR_DY_ID: ["domogram vector dy", list(DOMOGRAM_VECTOR_DY)],
        DOMOGRAM_PATH_DURATION_ID: ["domogram path duration", list(DOMOGRAM_PATH_DURATION)],
        DOMOGRAM_PATH_VECTOR_ID: ["domogram path vector", list(DOMOGRAM_PATH_VECTOR)],
        DOMOGRAM_FRAME_ORD_ID: ["domogram frame ord", list(DOMOGRAM_FRAME_ORDINALS)],
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
        "zakato": zakato_blocks(),
        "giddo-spario": giddo_spario_blocks(),
        "brag-spario": brag_spario_blocks(),
        "garu-zakato": garu_zakato_blocks(),
        "bacura": bacura_blocks(),
        "sheonite": sheonite_blocks(),
        "barra": barra_blocks(),
        "garu": garu_blocks(),
        "logram": logram_blocks(),
        "zolbak": zolbak_blocks(),
        "derota": derota_blocks(),
        "garu derota": garu_derota_blocks(),
        "boza": boza_blocks(),
        "grobda": grobda_blocks(),
        "domogram": domogram_blocks(),
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
        elif target["name"] == ZAKATO_TARGET:
            # AIR-07: likewise, the only Zakato render state is which flying slot each clone draws; the
            # phase (teleport/active/self-destruct/hit) lives in the Stage slot lists the clone reads.
            target["variables"] = target["variables"] | {
                ZAKATO_CLONE_SLOT_ID: ["zakato clone slot", 0],
            }
        elif target["name"] == GIDDO_SPARIO_TARGET:
            # AIR-10: likewise, the only Giddo render state is which flying slot each clone draws; the flight
            # phase and hit clock live in the Stage slot lists the clone reads.
            target["variables"] = target["variables"] | {
                GIDDO_SPARIO_CLONE_SLOT_ID: ["giddo spario clone slot", 0],
            }
        elif target["name"] == BRAG_SPARIO_TARGET:
            # AIR-10: likewise, the only Brag render state is which flying slot each clone draws.
            target["variables"] = target["variables"] | {
                BRAG_SPARIO_CLONE_SLOT_ID: ["brag spario clone slot", 0],
            }
        elif target["name"] == GARU_ZAKATO_TARGET:
            # AIR-08: likewise, the only Garu Zakato render state is which flying slot each clone draws.
            target["variables"] = target["variables"] | {
                GARU_ZAKATO_CLONE_SLOT_ID: ["garu zakato clone slot", 0],
            }
        elif target["name"] == BACURA_TARGET:
            # AIR-11: likewise, the only Bacura render state is which BACURA-band slot each clone draws; the
            # slab has no phases at all, so there is nothing else to track.
            target["variables"] = target["variables"] | {
                BACURA_CLONE_SLOT_ID: ["bacura clone slot", 0],
            }
        elif target["name"] == SHEONITE_TARGET:
            # AIR-09: likewise, the only Sheonite render state is which flying slot each clone draws; the
            # phase (home/lock/combine/retreat) and clock live in the Stage slot lists the clone reads.
            target["variables"] = target["variables"] | {
                SHEONITE_CLONE_SLOT_ID: ["sheonite clone slot", 0],
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
        elif target["name"] == ZOLBAK_TARGET:
            # GND-02 (ground.zolbak #85): likewise, the only Zolbak render state is which GROUND slot each
            # clone draws; the idle/crater frame lives in the Stage slot lists the clone reads.
            target["variables"] = target["variables"] | {
                ZOLBAK_CLONE_SLOT_ID: ["zolbak clone slot", 0],
            }
        elif target["name"] == DEROTA_TARGET:
            # GND-04 (ground.derota #86): likewise, the only Derota render state is which GROUND slot each
            # clone draws; the idle/crater frame lives in the Stage slot lists the clone reads.
            target["variables"] = target["variables"] | {
                DEROTA_CLONE_SLOT_ID: ["derota clone slot", 0],
            }
        elif target["name"] == GARU_DEROTA_TARGET:
            # GND-04 (ground.derota #86): likewise, the only Garu Derota render state is which GROUND slot
            # each clone draws; the base pulse and node frames live in the Stage slot lists the clone reads.
            target["variables"] = target["variables"] | {
                GARU_DEROTA_CLONE_SLOT_ID: ["garu derota clone slot", 0],
            }
        elif target["name"] == BOZA_TARGET:
            # GND-05 (ground.boza-logram #87): likewise, the only Boza render state is which GROUND slot each
            # clone draws; the dome frame, centre discriminator, and crater clock live in the Stage slot lists
            # the clone reads (branching on `slot link` for the outer-dome vs centre-bullseye costume).
            target["variables"] = target["variables"] | {
                BOZA_CLONE_SLOT_ID: ["boza clone slot", 0],
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
