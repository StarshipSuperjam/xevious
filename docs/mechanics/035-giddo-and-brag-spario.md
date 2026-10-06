# Giddo and Brag Spario (a straight aim-once flyby and an accelerating homer)

- Mechanic: The two Spario projectile families (AIR-10) — **Giddo Spario** `0x08` and **Brag Spario** `0x09` —
  the small payloads a Zakato-line enemy releases. They sit at opposite ends of the flying-motion spectrum and
  share only the slot fields and the flying-hit path with the rest of the aerial roster. The **Giddo** is aimed
  at the craft **exactly once** at spawn, on the fast **64-magnitude** (4 px/frame) `sheonite` angle table, then
  flies **dead straight** on that fixed velocity — it **never fires** and **never re-aims** — and on death plays
  its **own short ~8-frame burst** (the single documented exception to the shared ~20-frame flying explosion)
  before vanishing; it scores 10. The **Brag** is an **accelerating homer**: every tick it nudges its velocity
  toward the craft by a fixed step on **each** axis — with **no clamp**, so it accelerates unbounded — and a
  shot never destroys it: each hit scores 500 (the port has no super-xevious 2,000 tier) and uses up the shot
  while it keeps flying, leaving only off-screen (corrected in slice 21, [record 056](056-release-fidelity.md) item 6). A Giddo appears
  as a solo formation flyby (and through the debug cycle); a **Brag arrives only 4-at-a-time from the Garu Zakato
  detonation** (AIR-08, [air.special-pairs]), never from a formation wave, so its spawner is built in that leaf
  and this record covers the projectile behavior. Both reuse the per-slot fields and aim/allocation machinery of
  the earlier families ([record 023](023-aiming-and-slot-positions.md) slot fields and aim tiers,
  [record 024](024-toroid-vertical-slice.md) the shared flying lifecycle) and stand beside the base Zakato
  ([record 034](034-zakato-teleporters.md)).
- Derived behavior: **Giddo** (`handle_08_Giddo_Spario`) inits `_STATE = 2`, draws a **craft-independent random Y**
  (`gen_random_Y_store_obj` — an in-range clamp only, *no* craft-proximity reject, and no `init_teleport` `+1`
  offset because Giddo does not teleport in), aims once at the craft's current cell through `calc_dX_dY_for_vector_to_solvalou`
  over `angle_dX_dY_sheonite_tbl` (the 64-magnitude / 4 px-frame tier), and stamps `_PTS = 0` (10 pts). Each
  subsequent frame, unless it has been shot (`_STATE == 3` → `giddo_spario_hit`), it advances a 4-frame flight
  animation from `countup_timer_1` (`_CODE = (t>>1) & 3`, codes 0x100–0x103 in bank 1, a new frame every 2 frames;
  colour `((t>>1)>>2) & 3 + 0x26`, stepping 0x26–0x29 every 8 frames) and moves on its **fixed**
  `_dX/_dY` via `move_object_dX_dY` — no fire, no re-aim. When shot, `giddo_spario_hit` runs its **own** burst:
  it seeds `_TIMER = 0xff`, then each frame increments and tests `(_TIMER>>1) == 4` (~8 frames), drawing burst
  codes `(_TIMER>>1) + 4` (0x104–0x107, two frames each, at the colour the flight last wrote — the hit never
  writes the colour) while still drifting, then `remove_giddo_spario` clears `_TYPE`/`_STATE`. **Brag**
  (`handle_09_Brag_Spario`) inits `_STATE = 2`, single body `_CODE = 0x15` (0x115) at colour 0x26, `_PTS = 33`
  (500 pts). Each frame it
  recomputes an acceleration per axis by an **MSB compare** of Solvalou against itself: on the scroll axis, if
  `solvalou._X < self._X` then `ddX = −2`, if equal `0`, else `+2` (`brag_spario_ddX_sub_2` /
  `brag_spario_update_ddX`); the same on the lateral axis for `_Y` (`brag_spario_ddY_sub_2` /
  `brag_spario_update_dY`). It **adds** `ddX`/`ddY` into the running `_dX`/`_dY` — so the velocity ramps every
  frame with **no clamp** — animates a single body via ATTR flip bits (`countup_timer_1 & 0x0c`, a new flip every
  4 frames), and moves on
  the accumulated velocity. It has **no hit branch** and writes `_STATE = 2` every frame, so a shot (which hits
  only a `_STATE == 2` enemy and sets 3) scores 500 and is consumed but never stops it; it is removed only when
  it leaves the screen. This record first said it died to the shared explosion; slice 21 corrected that.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. Line citations are
  `src/xevious_main.68k` unless noted. Giddo: `handle_08_Giddo_Spario` 5219–5240 (`_STATE = 2`, the aim-once
  `sheonite` tier, `_PTS = 0`, the 4-frame flight animation, `move_object_dX_dY`), `giddo_spario_hit` 5241–5253
  (the own ~8-frame burst, `(_TIMER>>1) == 4` remove test, burst codes `+4`) and `remove_giddo_spario` 5254–5257
  (clears `_TYPE`/`_STATE`, no score), calling `gen_random_Y_store_obj` 5147 (craft-independent Y — in-range clamp, no craft reject),
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
  - **Giddo.** `install_init_giddo_spario` draws the spawn column with the **craft-gap exclusion OFF**
    (`exclude_craft=False` → `gen_random_Y_store_obj`, no craft reject; no `col_offset`, since Giddo does not
    teleport in), enters at
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
    and culls. Since slice 21 it never explodes: it tests the craft only while `SLOT_ACTIVE`, sets a `SLOT_HIT`
    slot back to `SLOT_ACTIVE`, offers the shared air-shot test at its drawn position, and only then moves, so a
    hit scores 500 and spends the shot while the Spario keeps flying (the arcade's collision tests read the
    sprite positions snapshotted at the start of the frame, and its craft test skips an enemy in state 3).

  Both bodies render through a shared `_spario_blocks` helper (`giddo spario`/`brag spario` targets), one clone
  per flying slot. Since slice 21 (`presentation.reference-art`) they draw the pinned frames. The Giddo's update
  writes its colour index (`floor(tick / 4) mod 4`, 0x26–0x29) into `slot flag` each flying tick and leaves it on
  a hit; the renderer draws flight frame `tick mod 4` at that colour while `SLOT_ACTIVE` and burst frame
  `floor(clock / 2)` at the kept colour while `SLOT_HIT` — its **own** four-code burst, not the shared air
  explosion. The Brag always draws the 0x115 body, its flip bits `floor(tick / 2) mod 4` — on the upright screen
  `_ATTR` bit 3 mirrors left-to-right and bit 2 top-to-bottom, so the body cycles
  none → top-to-bottom → left-to-right → both — through the shared `_flip_costume_offset`, and carries no
  explosion costumes. The Neo Geo renderer in the reference passes both bits to the hardware
  (`src/neogeo/neogeo.68k` 927–929, 965); the Amiga renderer tests only bit 3, as its left-to-right mirror
  (`src/amiga/amiga.68k` 2614), so on the Amiga the Spario shows only none and left-to-right. The port follows
  the game logic and the Neo Geo renderer.

  The arcade Giddo's spawn frame draws before its handler writes a code or colour (`save_PC_to_fn_tbl_and_ret`
  returns at 5226, `src/xevious_main.68k` 185–187), so for that one frame it shows whatever the slot last held;
  the port writes the flight colour at init instead, a port choice like the Garu's
  ([record 036](036-brag-and-garu-zakato.md)).
- Scratch evidence: `install_init_giddo_spario`, `install_update_giddo_spario`, `install_explode_giddo_spario_tick`,
  `install_init_brag_spario` and `install_update_brag_spario` (the lifecycle procs, reusing `compute aim`, the
  new 64-tier `aim dx 64`/`aim dy 64` tables, the craft-independent `_draw_spawn_column` (`exclude_craft=False`, no `col_offset`), the shared
  air-shot test that Brag offers before its move, and the family move/cull), the Giddo/Brag branches in `install_advance_slots`,
  the Giddo branch in `install_spawn_flying` (and the **absence** of a Brag one), `giddo_spario_blocks` /
  `brag_spario_blocks` for the render (with `_set_giddo_colour` writing the Giddo's colour index), the Giddo entry in `DEBUG_SPAWN_FAMILIES`, and the `GIDDO_SPARIO_*` /
  `BRAG_SPARIO_*` tuning constants in `tools/game_director.py`; the structural contract `_air10_failures` and its
  per-clause negatives (`test_spario_slice_authoring_present` / `test_spario_slice_negative_fixtures`) in
  `tests/test_scratch_project.py`, whose clauses pin the two lifecycles, the by-type dispatch, that Giddo aims
  once on the 64 tier and captures no fire mask, that Giddo **never** writes an acceleration (a corrupter that
  adds one bites), that Giddo frees on its own short clock while a shot never destroys a Brag (the craft test only while active, then
  back to active, the shot test and the move, and a renderer that draws only the body), that Brag
  accelerates by `±BRAG_SPARIO_ACCEL` on each axis under the sign guards (corrupters that flip a sign or drop a
  guard bite), and the per-family points; the live scenarios in `harness/lib/catalog.js`
  (`giddo-spario-flies-straight-and-self-bursts-short` asserting the constant once-aimed velocity, the `4×`
  displacement and the short self-burst free, and `brag-spario-accelerates-toward-craft` asserting the
  `4,8,12,16` velocity ramp on both axes, and, since slice 21, `brag-spario-survives-a-shot` asserting a hit
  scores 500, spends the shot and leaves the Spario flying), each with a biting negative. Slice 21
  (`presentation.reference-art`) adds the `giddo-colour-cycles-hit-keeps-it`, `giddo-renders-pinned-frames` and
  `brag-spario-renders-pinned-frames` clauses of `_reference_art_consumer_failures` (with biting negatives in
  `test_reference_art_consumer_negatives`) and the scenarios `reference-art-enemy-frames` and
  `reference-art-body-colours-follow-the-clock`, which seed each phase and tick and assert the drawn costume.
- Acceptance criteria: A Giddo Spario spawns (debug cycle and the solo formation flyby), aims once at the craft,
  flies **dead straight** without ever firing or re-aiming, and — shot down — plays a **short** burst and
  vanishes noticeably faster than the shared explosion; a Brag Spario (spawned from the Garu detonation once
  AIR-08 lands, or via the harness) **accelerates toward the craft** on both axes, homing in with rising speed, and a
  shot scores 500 without destroying it;
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
  reproduced. The credited Aerial Enemies rip has no Spario sprites, so until slice 21 both bodies drew the
  Zakato body as a stand-in and the Giddo's burst reused the shared explosion. Since slice 21 the Giddo's flight
  and hit frames at its four colours and the Brag's 0x115 body are decoded from the pinned reference's graphics
  data (`assets/amiga/xevious_gfx.c`) by `tools/reference_art_render.py`, credited in
  `src/xevious/assets/provenance.json` and [the asset credits](../ASSET_CREDITS.md) under the same rights caveat
  as the terrain. The frames are cut from a sheet the renderer re-derives byte-for-byte (`--verify`); the
  operator confirms the art at the playtest.
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
  (flying), `SLOT_HIT` (shot down: Giddo → its own tick, Brag → set back to `SLOT_ACTIVE` the next tick, since a shot never destroys it) — the same explicit-phase
  mapping recorded for Zakato ([record 034](034-zakato-teleporters.md)). (4) **Missing Spario sprites — resolved in slice 21.** The bodies
  used to draw the Zakato body as a stand-in; they now draw the Giddo's 4-frame flight animation, colour cycle
  and own burst, and the Brag's spin flips, from the pinned reference (`presentation.reference-art`). The Giddo's
  colour is kept as an index (0–3 for 0x26–0x29) in `slot flag`, because Scratch costumes are pre-coloured. (5) **Unbounded acceleration retained.** The arcade applies
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
