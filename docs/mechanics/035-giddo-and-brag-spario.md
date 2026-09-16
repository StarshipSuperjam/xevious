# Giddo and Brag Spario (a straight aim-once flyby and an accelerating homer)

- Mechanic: The two Spario projectile families (AIR-10) — **Giddo Spario** `0x08` and **Brag Spario** `0x09` —
  the small payloads a Zakato-line enemy releases. They sit at opposite ends of the flying-motion spectrum and
  share only the slot fields and the flying-hit path with the rest of the aerial roster. The **Giddo** is aimed
  at the craft **exactly once** at spawn, on the fast **64-magnitude** (4 px/frame) `sheonite` angle table, then
  flies **dead straight** on that fixed velocity — it **never fires** and **never re-aims** — and on death plays
  its **own short ~8-frame burst** (the single documented exception to the shared ~20-frame flying explosion)
  before vanishing; it scores 10. The **Brag** is an **accelerating homer**: every tick it nudges its velocity
  toward the craft by a fixed step on **each** axis — with **no clamp**, so it accelerates unbounded — and it
  shares the standard ~20-frame burst; it scores 500 (the port has no super-xevious 2,000 tier). A Giddo appears
  as a solo formation flyby (and through the debug cycle); a **Brag arrives only 4-at-a-time from the Garu Zakato
  detonation** (AIR-08, [air.special-pairs]), never from a formation wave, so its spawner is built in that leaf
  and this record covers the projectile behavior. Both reuse the per-slot fields and aim/allocation machinery of
  the earlier families ([record 023](023-aiming-and-slot-positions.md) slot fields and aim tiers,
  [record 024](024-toroid-vertical-slice.md) the shared flying lifecycle) and stand beside the base Zakato
  ([record 034](034-zakato-teleporters.md)).
- Derived behavior: **Giddo** (`handle_08_Giddo_Spario`) inits `_STATE = 2`, draws a **craft-excluding random Y**
  (`gen_random_Y_store_obj`), aims once at the craft's current cell through `calc_dX_dY_for_vector_to_solvalou`
  over `angle_dX_dY_sheonite_tbl` (the 64-magnitude / 4 px-frame tier), and stamps `_PTS = 0` (10 pts). Each
  subsequent frame, unless it has been shot (`_STATE == 3` → `giddo_spario_hit`), it advances a 4-frame flight
  animation from `countup_timer_1` (`_CODE = (t>>1) & 3`, colour `(t>>2) & 3 + 0x26`) and moves on its **fixed**
  `_dX/_dY` via `move_object_dX_dY` — no fire, no re-aim. When shot, `giddo_spario_hit` runs its **own** burst:
  it seeds `_TIMER = 0xff`, then each frame increments and tests `(_TIMER>>1) == 4` (~8 frames), drawing burst
  codes `(_TIMER>>1) + 4` while still drifting, then `remove_giddo_spario` clears `_TYPE`/`_STATE`. **Brag**
  (`handle_09_Brag_Spario`) inits `_STATE = 2`, single body `_CODE = 0x15`, `_PTS = 33` (500 pts). Each frame it
  recomputes an acceleration per axis by an **MSB compare** of Solvalou against itself: on the scroll axis, if
  `solvalou._X < self._X` then `ddX = −2`, if equal `0`, else `+2` (`brag_spario_ddX_sub_2` /
  `brag_spario_update_ddX`); the same on the lateral axis for `_Y` (`brag_spario_ddY_sub_2` /
  `brag_spario_update_dY`). It **adds** `ddX`/`ddY` into the running `_dX`/`_dY` — so the velocity ramps every
  frame with **no clamp** — animates a single body via ATTR flip bits (`countup_timer_1 & 0x0c`), and moves on
  the accumulated velocity. On death it uses the **shared** explosion, not its own.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. Line citations are
  `src/xevious_main.68k` unless noted. Giddo: `handle_08_Giddo_Spario` 5219–5240 (`_STATE = 2`, the aim-once
  `sheonite` tier, `_PTS = 0`, the 4-frame flight animation, `move_object_dX_dY`), `giddo_spario_hit` 5241–5253
  (the own ~8-frame burst, `(_TIMER>>1) == 4` remove test, burst codes `+4`) and `remove_giddo_spario` 5254–5257
  (clears `_TYPE`/`_STATE`, no score), calling `gen_random_Y_store_obj` 5147 (craft-excluding Y),
  `calc_dX_dY_for_vector_to_solvalou` 5119 over `angle_dX_dY_sheonite_tbl` 6290 and `move_object_dX_dY` 4817.
  Brag: `handle_09_Brag_Spario` 3080–3121 (`_STATE = 2`, `_CODE = 0x15`, `_PTS = 33` = 500, the per-axis MSB
  compare, the unbounded `add.w ddX/ddY` velocity update, the ATTR-flip animation, `move_object_dX_dY`) with the
  four accel arms `brag_spario_update_ddX` 3101, `brag_spario_update_dY` 3111, `brag_spario_ddX_sub_2` 3123 and
  `brag_spario_ddY_sub_2` 3127. The behavior is a port mapping of that logic, not copied text; the Spario
  paragraphs of [aerial enemies](../spec/aerial-enemies.md) are the settled description this slice implements.
- Transfer class: Behavioral port (instruction-derived control flow and numeric constants; no source text, ROM,
  or media copied). The aim tables, value table and slot layout are the derived data of records
  [022](022-fire-permission-masks.md), [023](023-aiming-and-slot-positions.md) and
  [025](025-blaster-to-air-hit.md).
- Scratch interpretation: The ordered walk `advance slots` (SYS-04) dispatches a Giddo-typed slot
  (`GIDDO_SPARIO_TYPE` = 8) to `update giddo spario` and a Brag-typed slot (`BRAG_SPARIO_TYPE` = 9) to
  `update brag spario`; each has its own init proc. The axes follow the family convention: `slot x` is the
  scroll/forward row (arcade `_X`), `slot y` the lateral column (arcade `_Y`). With no coroutine re-entry, the
  port carries the phase explicitly in `slot state`.
  - **Giddo.** `install_init_giddo_spario` draws the spawn column with the **craft-gap exclusion on**, enters at
    the shared top row, stamps `SLOT_ACTIVE`, aims **once** through the shared `compute aim` over the new
    64-magnitude `aim dx 64`/`aim dy 64` tables, captures **no** fire mask and seeds **no** fire timer (Giddo
    never fires), and stamps `slot pts` = `GIDDO_SPARIO_PTS`. `install_update_giddo_spario` runs a top
    HIT-vs-else guard: on `SLOT_HIT` it calls its **own** `explode giddo spario tick` (which moves on the fixed
    velocity, advances the clock `2/tick`, and frees on the **8-frame** `GIDDO_SPARIO_HIT_DURATION_FRAMES` — far
    shorter than the shared 20); on `SLOT_ACTIVE` it checks the shared air-hit detector, checks craft collision,
    then **moves on its fixed `slot dx`/`slot dy` with no velocity write** and culls off any edge. That the
    Giddo flies straight is structural — there is no acceleration or re-aim statement anywhere in its update.
  - **Brag.** `install_init_brag_spario` stamps only the non-kinematic fields (`SLOT_ACTIVE`, code, points,
    clock); the Garu Zakato detonation (AIR-08) writes each slot's type, position and cardinal initial velocity
    directly, then calls this — there is **no** spawn-flying branch for Brag, because it never spawns from a
    wave. `install_update_brag_spario` accelerates toward the craft with **two guarded nudges per axis**: on the
    scroll axis, `slot dx += BRAG_SPARIO_ACCEL` when `player row − slot row > 0` and `slot dx −= …` when `< 0`
    (the aligned `== 0` case is left untouched by both guards); the same on the lateral axis with
    `player col − slot col`. It then moves on the accumulated velocity (`4×`), advances its flip-animation clock,
    and culls; on `SLOT_HIT` it uses the **shared** `explode toroid tick`.

  Both bodies render through a shared `_spario_blocks` helper (`giddo spario`/`brag spario` targets), one clone
  per flying slot, drawing a static body stand-in while `SLOT_ACTIVE` and forwarding to the shared burst frames
  while `SLOT_HIT` (Brag's helper adds the big-phase size branch). Because the aerial sprite rip carries **no
  Spario sprites** (see License status), both bodies reuse the Zakato body frame as a documented stand-in.
- Scratch evidence: `install_init_giddo_spario`, `install_update_giddo_spario`, `install_explode_giddo_spario_tick`,
  `install_init_brag_spario` and `install_update_brag_spario` (the lifecycle procs, reusing `compute aim`, the
  new 64-tier `aim dx 64`/`aim dy 64` tables, the craft-excluding `_draw_spawn_column`, the shared
  `explode toroid tick` for Brag and the family move/cull), the Giddo/Brag branches in `install_advance_slots`,
  the Giddo branch in `install_spawn_flying` (and the **absence** of a Brag one), `giddo_spario_blocks` /
  `brag_spario_blocks` for the render, the Giddo entry in `DEBUG_SPAWN_FAMILIES`, and the `GIDDO_SPARIO_*` /
  `BRAG_SPARIO_*` tuning constants in `tools/game_director.py`; the structural contract `_air10_failures` and its
  per-clause negatives (`test_spario_slice_authoring_present` / `test_spario_slice_negative_fixtures`) in
  `tests/test_scratch_project.py`, whose clauses pin the two lifecycles, the by-type dispatch, that Giddo aims
  once on the 64 tier and captures no fire mask, that Giddo **never** writes an acceleration (a corrupter that
  adds one bites), that Giddo frees on its own short clock while Brag uses the shared burst, that Brag
  accelerates by `±BRAG_SPARIO_ACCEL` on each axis under the sign guards (corrupters that flip a sign or drop a
  guard bite), and the per-family points; the live scenarios in `harness/lib/catalog.js`
  (`giddo-spario-flies-straight-and-self-bursts-short` asserting the constant once-aimed velocity, the `4×`
  displacement and the short self-burst free, and `brag-spario-accelerates-toward-craft` asserting the
  `4,8,12,16` velocity ramp on both axes), each with a biting negative.
- Acceptance criteria: A Giddo Spario spawns (debug cycle and the solo formation flyby), aims once at the craft,
  flies **dead straight** without ever firing or re-aiming, and — shot down — plays a **short** burst and
  vanishes noticeably faster than the shared explosion; a Brag Spario (spawned from the Garu detonation once
  AIR-08 lands, or via the harness) **accelerates toward the craft** on both axes, homing in with rising speed;
  a Giddo scores 10 and a Brag 500 through the shared flying-hit path (harness
  `giddo-spario-flies-straight-and-self-bursts-short`, `brag-spario-accelerates-toward-craft`, each with a biting
  negative); the operator playtest confirms the felt behavior — a Spario streaking straight past, and a Brag
  chasing the craft with quickening speed.
- Fidelity status: Verified line-by-line against the pinned reference this slice (`handle_08_Giddo_Spario`,
  `giddo_spario_hit`, `remove_giddo_spario`, `handle_09_Brag_Spario` and its four accel arms, plus
  `gen_random_Y_store_obj`, `calc_dX_dY_for_vector_to_solvalou`, `angle_dX_dY_sheonite_tbl` and
  `move_object_dX_dY` were read at the pin). The behavior matches the reference within the recorded deviations.
- License status: The reference states no reusable license; only instruction-derived behavior and numeric
  constants are transferred (recorded in [the index](../spec/index.md) and the data files). No source text is
  reproduced. **No Spario sprites exist in the credited Aerial Enemies rip** (`src/xevious/assets/provenance.json`,
  `https://www.spriters-resource.com/arcade/xevious/`, sheet author "CrazyCarl"), so both Spario bodies reuse
  the Zakato body frame as a **documented stand-in** — a small dark blob standing in for the payload a Zakato
  releases — and the death burst reuses the shared explosion frames; no new sprite crop was added, so no crop
  rect required operator pixel-verification for this family.
- Known deviations or uncertainty: (1) **Two arcade frames per tick (tick scaling).** The per-frame reference
  rates are doubled for the port's two-frame tick — Giddo's own burst clock advances `TICK_TIMER_STEP = 2` per
  tick (freeing at `GIDDO_SPARIO_HIT_DURATION_FRAMES = 8`), and both bodies move by the shared `×4` position
  step — the same tick scaling every family uses. (2) **MSB byte compare expressed as cell arithmetic.** Scratch
  has no MSB/carry test, so Brag's per-axis `cmp.b (MSB)` → `jcs −2 / jeq 0 / else +2` is expressed as the
  arithmetic sign of `player row − slot row` (scroll) and `player col − slot col` (lateral) with `> 0`/`< 0`
  guards that leave the aligned case untouched — the same no-bitwise idiom recorded for the earlier families,
  on the family axis convention. (3) **Coroutine re-entry / overloaded `_STATE = 3` expressed as explicit phase
  states.** The arcade holds its phase in the coroutine resume address and reuses `_STATE = 3` as the shot-down
  hit flag; the port has no resume, so it splits the phases into explicit `slot state` sentinels — `SLOT_ACTIVE`
  (flying), `SLOT_HIT` (shot down: Giddo → its own tick, Brag → the shared tick) — the same explicit-phase
  mapping recorded for Zakato ([record 034](034-zakato-teleporters.md)). (4) **Missing Spario sprites → Zakato
  body stand-in.** Because the rip has no Spario art, both bodies draw the Zakato body frame and defer their
  distinct 4-frame Giddo flight animation and Brag ATTR-flip mirror to a later art pass — a cosmetic deviation;
  the motion, points and lifecycle are unaffected. (5) **Unbounded acceleration retained.** The arcade applies
  **no** clamp to Brag's ramping velocity; the port matches this exactly (no clamp), so a Brag can outrun the
  craft's own speed — faithful, not a bug. (6) **No enemy scroll.** The port has no background-scroll term for
  flying slots, so the arcade's scroll-during-flight renders as pure `slot dx`/`slot dy` motion — the same "no
  enemy scroll" deviation class recorded for every flying family. (7) **64-magnitude aim tier added.** Giddo's
  4 px/frame aim needed a new tier beyond the 24/32/48 tables — the `aim dx 64`/`aim dy 64` lists (the
  `sheonite` tier), shared with the forthcoming Sheonite family. (8) **Brag reachability.** In the built areas
  1–16 a Brag appears only from the Garu Zakato detonation (AIR-08); until that leaf lands, the Brag lifecycle
  is exercised by the harness scenario and the (Giddo-only) debug cycle, not natural play — recorded so a
  reviewer does not read its absence from the debug cycle as a build gap.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
