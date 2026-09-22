# Sheonite (the indestructible escort pair)

- Mechanic: The Sheonite (AIR-09) is an **escort pair** — a right half (`0x31`) and a left half (`0x32`) —
  that is **schedule-spawned** as a couple, homes onto the Solvalou, **docks** beside it for a short dwell, and
  then **peels off**: the right half retreats up the scroll axis (with a sound), while the left half simply
  vanishes. Both halves are **wholly indestructible and wholly inert**: a player shot cannot hurt them, a bomb
  cannot reach them, they never score, and — the point that separates them from the Bacura — **touching one
  does not kill the craft**. They are not a formation family: a schedule opcode `sheonite_start` stamps the two
  halves into two fixed flying-object slots and clears an end-flag, and a later opcode `sheonite_end` raises
  that end-flag to tell them it is time to leave. Their inertness is not a flag the collision code checks; it
  is a **routing** fact — both halves run at the arcade's indestructible `_STATE == 3`, and **every** hit test
  (the shot/score test and the craft-collision test alike) gates on `_STATE == 2`, so a Sheonite is silently
  skipped by all of them. They share the per-slot fields and the flying-slot pool with the earlier aerial
  families ([record 023](023-aiming-and-slot-positions.md) slot fields), but deliberately touch **none** of the
  hit paths ([record 025](025-blaster-to-air-hit.md) the shot detector they skip,
  [record 026](026-enemy-bullets-and-collision-death.md) the craft-death path they skip).
- Derived behavior: `sheonite_start` writes only the two `_TYPE`s (`0x31` → the sixth flying object slot
  `0x3f`, `0x32` → the fifth `0x3e`) and clears `sheonite_end_flag`; it does **not** set `_STATE`. Each half's
  **walk handler** sets `_STATE = 3` (indestructible) on its first walk and then runs a small state machine off
  the shared free-running frame counter `countup_timer_1`:
  - **Home.** The handler aims at the Solvalou on the shadow-MSB bytes — right at `(craft.X − 2, craft.Y − 2)`,
    left at `(craft.X − 2, craft.Y + 2)` — through `angle_dX_dY_sheonite_tbl` (magnitude `0x40`), giving a
    steady homing velocity that `move_object_dX_dY` doubles.
  - **Lock.** Once the half reaches the craft's scroll row it holds a fixed offset of `(X − 0x200, Y ∓ 0x200)`
    recomputed **every frame** from the live craft slot, so it tracks the craft while docked.
  - **Dock.** When the exit gate is not yet open the half docks: the right half's `r_sheonite_combining` inits
    `_TIMER = 0xe0` and **counts up** (`addq #1`, retreat on the byte-wrap), and the left half's
    `l_sheonite_combining` inits `_TIMER = 0x20` and **counts down** (`subq #1`, leave at zero) — **both are
    32-frame** dwells (`0x100 − 0xe0 = 0x20 = 32`), the count directions differ but the frame counts do not.
  - **Exit gate.** The transition out of lock fires only when `countup_timer_1 == 0` **and**
    `sheonite_end_flag != 0`: raising the end-flag does not retreat the pair immediately; it waits for the next
    frame-counter wrap.
  - **Exit.** The right half `r_sheonite_retreat` sets `_dX = 0xFFA0` (−96) and `_dY = 0`, plays `SHEONITE_SND`,
    and re-registers as active — so it slides up the scroll axis away from the craft. The left half
    `l_sheonite_remove` clears `_TYPE` and `_STATE` — it vanishes with no explosion and no sound.
  Neither half ever calls a fire routine, and neither is ever offered to a hit test, so neither can be
  destroyed, scored, or made to hurt the craft.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. Line citations are
  `src/xevious_main.68k` unless noted. The right half is `handle_31_right_sheonite` 4052–4083 (`_STATE = 3` at
  4053, the homing aim at `(X−2, Y−2)` through `angle_dX_dY_sheonite_tbl`, and the exit gate 4072–4075
  `countup_timer_1 == 0` AND `sheonite_end_flag != 0`); its dock is `r_sheonite_combining` 4102–4118
  (`_TIMER = 0xe0`, count-up, retreat on wrap) and its lock offset `set_r_sheonite_above_solvalou` 4140–4148
  (`X − 0x200`); its exit is `r_sheonite_retreat` 4120–4138 (`_dX = 0xFFA0` = −96, `SHEONITE_SND`). The left
  half is `handle_32_left_sheonite` 4162–4196 (`_STATE = 3` at 4163, aim at `(X−2, Y+2)`, exit gate 4185–4188);
  its dock is `l_sheonite_combining` 4215–4230 (`_TIMER = 0x20`, count-down, leave at zero) and its exit
  `l_sheonite_remove` 4232–4235 (clears `_TYPE` and `_STATE`). The homing table is
  `angle_dX_dY_sheonite_tbl` 6290. The pair is spawned and retired by the schedule routines
  (`src/xevious_sub.68k`) `sub_2_fn_18__sheonite_start` 530–537 (`0x31` → slot `0x3f`, `0x32` → slot `0x3e`,
  `clr sheonite_end_flag`) and `sub_2_fn_19__sheonite_end` 539–542 (`sheonite_end_flag = 1`). Indestructibility
  and full inertness are **routing** facts, not a flag: `check_shot_hit_flying_enemy` 2565–2567 and
  `check_bullet_or_flying_hit_solvalou` 2207–2209 both gate on `_STATE == 2`, so a `_STATE == 3` Sheonite is
  skipped by the shot/score test and by the craft-collision test alike. The behavior is a port mapping of that
  logic, not copied text; the Sheonite paragraph of [aerial enemies](../spec/aerial-enemies.md) is the settled
  description this slice implements.
- Transfer class: Behavioral port (instruction-derived control flow and numeric constants; no source text,
  ROM, or media copied). The slot layout, the flying-slot pool and the hit paths it deliberately skips are the
  derived data of records [023](023-aiming-and-slot-positions.md), [025](025-blaster-to-air-hit.md) and
  [026](026-enemy-bullets-and-collision-death.md).
- Scratch interpretation: The Sheonite dispatches like every other flyer — by `walk type` — because its type
  codes (`49`/`50`) collide with nothing else in the walk, so it needs **no** reserved band (unlike the Bacura,
  whose code `1` clashes with `SHOT_TYPE`). The two halves stamp into two **fixed adjacent flying slots**
  (`SHEONITE_RIGHT_SLOT` = the pool's `0x3f`, `SHEONITE_LEFT_SLOT` = `0x3e`, mirroring the arcade), and a single
  `update sheonite` warp proc, dispatched under a **two-type OR** on `walk type` (the Jara pair model), drives
  both. Axes follow the family convention: `slot x` is the scroll/forward row (arcade `_X`), `slot y` the
  lateral column (arcade `_Y`). The state machine's phase lives in `slot flag`
  (`SHEONITE_PHASE_HOME`/`LOCK`/`COMBINE`/`RETREAT` = 0/1/2/3): HOME homes toward
  `(player row − SHEONITE_LOCK_LEAD, player col ∓ SHEONITE_LOCK_FLANK)`; LOCK recomputes that offset from the
  live craft each tick and waits; COMBINE docks for `SHEONITE_COMBINE_DWELL_FRAMES` (32) gated on the end-flag;
  and at dwell-end the **right** half writes `slot dx = SHEONITE_RETREAT_DX` (−96) and moves to RETREAT while
  the **left** half culls (its `slot type`/`slot state` cleared) — the retreat-versus-vanish asymmetry. Two
  facts make the pair distinct from every other flyer, and both are **structural**:
  - **Indestructible by omission.** `update sheonite` deliberately **omits** the `check air shot hit` call that
    every killable flyer makes, stamps **no** `slot pts`, runs **no** explosion and never writes `SLOT_HIT`,
    and calls no resolve-hit/score path. That omission *is* the shot-invulnerability — no shot ever hit-tests a
    Sheonite, so there is no hit, no explosion, and no score.
  - **Craft-inert by omission.** There is **no** craft-overlap detector over the Sheonite slots at all, and
    `update sheonite` writes **no** `player hit`. This is the port image of the arcade's `_STATE == 3` skipping
    the craft-collision test — so touching a Sheonite cannot kill the craft. (This is the corrected
    no-craft-death contract: the Bacura's craft-touch death, [record 037](037-bacura-slab.md), applies only
    because a Bacura runs at the active `_STATE == 2` and is craft-tested by the separate
    `check_bacura_hit_solvalou`; a Sheonite at `_STATE == 3` is skipped by every test, so cloning that death
    onto it would have been a fidelity bug.)

  `install_init_sheonite` stamps each half `slot state = SLOT_ACTIVE` (1 — the port image of the arcade's active
  registration; the indestructible `_STATE == 3` is expressed as the *absence* of every hit path, not a port
  state value) at `slot flag = SHEONITE_PHASE_HOME`, and clears `sheonite end flag`. The schedule wires
  `sheonite_start` (op `0x33`) and `sheonite_end` (op `0x34`) into `_consume_schedule` beside the ground and
  Bacura branches: `sheonite_start` direct-stamps both fixed slots and clears the end-flag; `sheonite_end`
  raises it — these are **on/off flags**, not a per-second pump. A T-key debug direct-stamp seeds the pair into
  the same two slots for isolated playtesting, with the debug cursor's field-empty gate extended to wait on the
  pair clearing (the pair pre-arms the end-flag so it self-culls). `sheonite_blocks` renders the pair over the
  two flying slots under the same two-type OR, cycling the ten frames (`sheonite/spin/01`…`04` while homing and
  locked, `sheonite/combine/01`…`06` while docking and retreating).
- Scratch evidence: `install_init_sheonite` and `install_update_sheonite` (the lifecycle + phase machine), the
  **two-type OR** Sheonite branch in `install_advance_slots` (dispatched by `walk type == RIGHT_SHEONITE_TYPE`
  OR `== LEFT_SHEONITE_TYPE`, calling `UPDATE_SHEONITE_PROCCODE` and **never** `CHECK_AIR_HIT`), the
  `sheonite_start` / `sheonite_end` schedule branches in `_consume_schedule`, the `sheonite end flag` /
  `sheonite phase` / `sheonite lock col` state variables, the Sheonite entry in `DEBUG_SPAWN_FAMILIES` plus its
  dedicated direct-stamp branch and extended field-empty gate, `sheonite_blocks` for the ten-costume render, and
  the `SHEONITE_*` tuning constants in `tools/game_director.py`; the structural contract `_air09_failures` and
  its per-clause negatives (`test_sheonite_slice_authoring_present` / `test_sheonite_slice_negative_fixtures`)
  in `tests/test_scratch_project.py`, whose clauses pin the warp lifecycle procs, the two-type dispatch, that
  the update **omits** the shot detector / explosion / score path (corrupters graft each back in), that it
  writes **no** `player hit` (a corrupter grafting one bites — the craft-inertness invariant), that
  `sheonite_start` stamps both fixed slots ACTIVE/HOME and clears the end-flag while `sheonite_end` raises it,
  that the machine aims, locks, waits on the end-flag to combine, and on dwell-end the right retreats on
  `SHEONITE_RETREAT_DX` while the pair culls, and that the renderer carries the ten frames on the OR over both
  types; and the harness scenarios `sheonite-homes-onto-the-craft-and-locks`,
  `sheonite-lock-holds-until-the-end-flag-then-combines`,
  `sheonite-combine-exit-retreats-the-right-and-vanishes-the-left`, and the keystone
  `sheonite-is-inert-on-the-craft-cell` (whose biting negative **grafts** the forbidden `player hit` write) in
  `harness/lib/catalog.js` / `harness/lib/mutate.js`.
- Acceptance criteria: In area 9 (and via the T-key debug spawn) the Sheonite pair homes onto the craft, docks
  beside it for ~32 frames, and then peels off — the right half retreating up the field with a sound, the left
  half vanishing; a player shot **cannot destroy** either half, a bomb cannot reach one, neither ever scores,
  and **touching one does not kill the craft**. The operator playtest confirms the felt behavior — an
  indestructible escort couple that docks alongside you and leaves, that you can neither shoot nor be killed by.
- Fidelity status: Verified line-by-line against the pinned reference this slice
  (`handle_31_right_sheonite`, `handle_32_left_sheonite`, both `*_combining` docks, `r_sheonite_retreat`,
  `l_sheonite_remove`, `set_r/l_sheonite_above_solvalou`, `angle_dX_dY_sheonite_tbl`, the
  `sub_2_fn_18/19__sheonite_start/end` schedule routines, and the `_STATE == 2` gates in
  `check_shot_hit_flying_enemy` and `check_bullet_or_flying_hit_solvalou` were read at the pin). The behavior
  matches the reference within the recorded deviations below. This slice also corrects the locked spec
  ([aerial enemies](../spec/aerial-enemies.md)): the dock is **~32 frames on both sides** (the `0xe0` = 224 is
  the right side's count-**up** init value, not a frame count), and the real asymmetry is **retreat versus
  vanish**, not the frame count — carried under `guardrail-ack`.
- License status: The reference states no reusable license; only instruction-derived behavior and numeric
  constants are transferred (recorded in [the index](../spec/index.md) and the data files). No source text is
  reproduced. The ten Sheonite frames (`sheonite/spin/01`…`04`, `sheonite/combine/01`…`06`) are the Aerial
  Enemies rip credited in `src/xevious/assets/provenance.json` (The Spriters Resource: Xevious (Arcade), Aerial
  Enemies, `https://www.spriters-resource.com/arcade/xevious/asset/42387/`).
- Known deviations or uncertainty: (1) **Two arcade frames per tick (tick scaling).** The per-frame reference
  rates carry the port's two-frame tick and its ×2/×32 unit scale: the homing magnitude `0x40` = 64 becomes
  **4 px/f** (×2 by `move_object_dX_dY`, ÷32 stage px), the retreat `−96` becomes **−6 px/f**, and the lock
  offset `0x200` ÷ 32 = **16 px** — the same tick and unit scaling every family uses. (2) **Indestructibility
  and craft-inertness via omission, not a flag.** The arcade skips the pair from every hit test by running it at
  `_STATE == 3`; the port reproduces this exactly — `update sheonite` has no `check air shot hit` call, no
  explosion, no score, and no `player hit` write, and no craft-overlap detector targets the Sheonite slots —
  rather than inventing an "invulnerable" state bit. In particular there is **no craft-touch death**: the
  earlier plan's craft-touch death was wrongly cloned from the Bacura, which is craft-lethal only because it
  runs at the active `_STATE == 2`. (3) **Dock dwell expressed as one 32-frame constant.** The arcade counts the
  right dock **up** from `0xe0` and the left dock **down** from `0x20`; both are 32 frames, so the port uses one
  `SHEONITE_COMBINE_DWELL_FRAMES = 32` for both halves and distinguishes them only by the retreat-versus-vanish
  exit. (4) **Entry position.** The arcade `sheonite_start` stamps only the `_TYPE`s and lets the handler home
  from wherever the slot sits; the port seeds the pair at the shared spawn position and homes from there — the
  homing and lock are unaffected. (5) **No enemy scroll term.** As with every flying family, the port has no
  background-scroll term; the pair moves purely by `slot dx`/`slot dy`, which already carry the full homing and
  retreat motion. (6) **Super Xevious content excluded.** The port's extract is normal-only by construction
  (mechanics 017); the pair's normal-area schedule (area 9) is preserved and any Super-only occurrence is
  intentionally absent.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
