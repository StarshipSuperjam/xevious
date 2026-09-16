# Brag and Garu Zakato (a teleporting fan-shooter pair and a straight-flying detonator)

- Mechanic: The special-pairs Zakato family (AIR-08) — the two **Brag Zakato** variants, **rnd** `0x16` and
  **close-Y** `0x17`, and the **Garu Zakato** `0x18`. They extend the base Zakato line ([record
  034](034-zakato-teleporters.md)) with two distinct escalations. The **Brags** teleport in exactly like the
  base Zakato — held **indestructible** while a ~20-frame sparkle plays — then become hittable, aim at the craft
  on the generic **32-magnitude** (2 px/frame) tier, and instead of the base line's single aimed shot they end
  in a **5-bullet aimed radiating fan** (two angle-steps apart, on the faster 48 tier) before self-destructing
  and **awarding nothing**; the rnd variant fires on a random **1-64** fuse, the close-Y variant when the craft
  draws level in the lateral axis. Shot down while active a Brag scores 600 (rnd) or 1,500 (close-Y). The
  **Garu** is the opposite: it enters **immediately active** — **no teleport**, hittable at once — at a random
  lateral column, flies **dead straight** down the scroll axis at 3 px/frame (`dX = 48`, `dY = 0`), and carries
  a **32-63-frame fuse**; left alone it **detonates** into a **16-bullet 360° ring** (even angles 0,2,…,30) **and
  4 Brag Sparios** launched on cardinal velocities into the four adjacent flying slots, then **vanishes with no
  burst and no score**. Shot before the fuse elapses it scores 1,000 through the shared flying-hit path. The
  detonation is the sole spawner of the Brag Spario ([record 035](035-giddo-and-brag-spario.md)) and a live
  driver of the shared radiating emitter ([record 026](026-enemy-bullets-and-collision-death.md)).
- Derived behavior: **Brag rnd** (`handle_16_Brag_Zakato_rnd`) runs `init_teleport`, stamps `_PTS` (600 pts),
  and while teleporting scrolls in place; on completion (`zakato_teleport` clears carry) `brag_zakato_16_main`
  draws a **1-64 fuse** (`pseudo_random_gen`, `and #0x3f`, `addq #1`), stamps `_CODE = 0x12`, aims at the craft
  over `angle_dX_dY_tbl` (the 32-tier), sets `_STATE = 2`, and each active frame — unless shot
  (`_STATE == 3` → `flying_enemy_hit`) — decrements the fuse (`subq.b #1,(_TIMER)`) and at zero jumps to
  `brag_zakato_explode`, else moves on its aimed velocity. **Brag close-Y** (`handle_17_Brag_Zakato_closeY`) is
  identical but stamps `_PTS` (1,500 pts), takes **no fuse**, and triggers on a **lateral-proximity** test
  instead: `solvalou.Y − self.Y`, `subq #4`, `addq #8`, `jcs brag_zakato_explode` — the same `[-4, 3]` level-in-Y
  band as the base close-Y. Either trigger reaches `brag_zakato_explode`, which seeds `_TIMER = 0xff`, flags
  `_STATE = 3` (benign), calls **`brag_zakato_shoot`**, then falls into the shared `zakato_explode_and_remove`
  (the ~20-frame burst that frees the slot with **no score**). `brag_zakato_shoot` computes the craft-aim angle
  (`get_index_for_angle` on the x/y MSB diffs), folds it to a radiating index (`sub #32`, `ror.b #3`,
  `and #0x1f`), then emits **5 bullets** two steps apart (`find_idle_and_init_radiating_bullet`, `addq #2`,
  `and #0x1f`). **Garu** (`handle_18_Garu_Zakato`) sets `_STATE = 2` (active at once), draws a **craft-including
  random Y** (`gen_random_Y_store_obj`), stamps `_CODE = 0x13` and `_PTS` (1,000 pts), sets `_dX = 48` / `_dY = 0`
  (straight), and draws a **32-63 fuse** (`pseudo_random_gen`, `and #0x1f`, `add #32`). Each active frame, unless
  shot, it decrements the fuse and at zero jumps to `garu_zakato_explode`, else moves straight. `garu_zakato_explode`
  calls `init_garu_zakato_explosion` then `clr _TYPE`/`clr _STATE` — **no burst, no score**.
  `init_garu_zakato_explosion` emits a **16-bullet ring** from angle 0 (`addq #2`, `and #30`), then copies the
  Garu's cell (obj `0x3B`) into the **4 following objects** (`0x3C`-`0x3F`), writes the cardinal velocities from
  `brag_spario_dX_tbl` / `brag_spario_dY_tbl` and `_TYPE = 9` (Brag Spario) into each.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. Line citations are
  `src/xevious_main.68k` unless noted. Brag rnd: `handle_16_Brag_Zakato_rnd` 3863-3889 (`init_teleport`, `_PTS`
  600, the `zakato_teleport` gate, `brag_zakato_16_main` fuse `(rnd & 0x3f) + 1` = 1-64, `_CODE = 0x12`, aim over
  `angle_dX_dY_tbl` 32-tier, `_STATE = 2`, the `subq _TIMER` → `jeq brag_zakato_explode`, `move_object_dX_dY`).
  Brag close-Y: `handle_17_Brag_Zakato_closeY` 3893-3918 (`_PTS` 1,500, `brag_zakato_17_main` `_CODE = 0x12`, aim
  32-tier, the level-in-Y `subq #4` / `addq #8` / `jcs` proximity test). `brag_zakato_explode` 3920-3924
  (`_TIMER = 0xff`, `_STATE = 3` benign, `brag_zakato_shoot`, `zakato_explode_and_remove`). `brag_zakato_shoot`
  5054-5073 (the x/y MSB diffs, `get_index_for_angle`, the `sub #32` / `ror.b #3` / `and #0x1f` index fold, the
  5-bullet `find_idle_and_init_radiating_bullet` loop with `addq #2` / `and #0x1f`). Garu:
  `handle_18_Garu_Zakato` 4010-4029 (`_STATE = 2`, `gen_random_Y_store_obj`, `_CODE = 0x13`, `_PTS` 1,000,
  `_dX = 48` / `_dY = 0`, fuse `(rnd & 0x1f) + 32` = 32-63, `subq _TIMER` → `jeq garu_zakato_explode`,
  `move_object_dX_dY`) and `garu_zakato_explode` 4031-4038 (`init_garu_zakato_explosion`, `clr _TYPE`/`_STATE` —
  no burst, no score). `init_garu_zakato_explosion` 5075-5104 (the 16-bullet ring from angle 0 with `addq #2` /
  `and #30`, the copy of obj `0x3B`'s cell into `0x3C`-`0x3F`, the `_TYPE = 9` stamp) reading
  `brag_spario_dX_tbl` 5106-5110 (`0x20, 0, 0xE0, 0` = 32, 0, −32, 0) and `brag_spario_dY_tbl` 5112-5116
  (`0, 0xE0, 0, 0x20` = 0, −32, 0, 32). The behavior is a port mapping of that logic, not copied text; the
  special-pairs paragraphs of [aerial enemies](../spec/aerial-enemies.md) are the settled description this slice
  implements.
- Transfer class: Behavioral port (instruction-derived control flow and numeric constants; no source text, ROM,
  or media copied). The aim tables, value table and slot layout are the derived data of records
  [022](022-fire-permission-masks.md), [023](023-aiming-and-slot-positions.md) and
  [025](025-blaster-to-air-hit.md); the shared radiating emitter is [record 026](026-enemy-bullets-and-collision-death.md).
- Scratch interpretation: The ordered walk `advance slots` (SYS-04) dispatches the two Brag types
  (`BRAG_ZAKATO_RND_TYPE` = 22, `BRAG_ZAKATO_CLOSEY_TYPE` = 23) to a **single** `update brag zakato` (they differ
  only in the fan trigger, branched inside on `slot type`) and the Garu (`GARU_ZAKATO_TYPE` = 24) to its own
  `update garu zakato`; the Brags share one `init brag zakato`, the Garu has `init garu zakato`. The axes follow
  the family convention: `slot x` is the scroll/forward row (arcade `_X`), `slot y` the lateral column (arcade
  `_Y`). With no coroutine re-entry the port carries the phase explicitly in `slot state`.
  - **Brag.** `install_init_brag_zakato` clones the base Zakato teleport-in exactly — the craft-excluding spawn
    column, `SLOT_TELEPORT` (indestructible), `slot code` = `BRAG_ZAKATO_MAIN_CODE` (`0x12`), and the per-variant
    `slot pts` — capturing **no** fire mask and seeding **no** fire timer at spawn (the fuse is drawn later, on
    teleport completion). `install_update_brag_zakato` runs the shared teleport → active → self-destruct machine:
    on completion it aims both variants through `compute aim` over the **32-tier** `aim dx 32` / `aim dy 32`
    tables, commits `SLOT_ACTIVE`, and (rnd only) seeds `slot fire timer` = `(rng mod 64) + 1`. Each active tick
    it checks craft collision, decrements the rnd fuse, and on its trigger (rnd: fuse ≤ 0; close-Y: the craft is
    in the `[-4, 3]` lateral band) calls **`brag zakato shoot`**, flips to `SLOT_SELF_EXPLODE`, and zeroes the
    velocity; `SLOT_SELF_EXPLODE` and `SLOT_HIT` both play the **shared** `explode toroid tick`. `install_brag_zakato_shoot`
    aims at the craft, folds the aim base to a radiating index (`floor(((aim base − 32) mod 256) / 8) mod 32`,
    the arcade `sub #32` / `ror.b #3` / `and #0x1f`), then loops **5 times** calling the shared `emit radiating
    bullet` and stepping `radiating angle` by 2 — all five leave the Brag's own cell.
  - **Garu.** `install_init_garu_zakato` draws the spawn column with the **craft-gap exclusion OFF**
    (`gen_random_Y_store_obj`), commits `SLOT_ACTIVE` at once (**the biting contrast** with the Brag's
    `SLOT_TELEPORT`), sets `slot dx` = `GARU_STRAIGHT_DX` (48) / `slot dy` = 0, stamps `slot code`
    (`0x13`) and `slot pts` (1,000), and seeds `slot fire timer` = `(rng mod 32) + 32`. `install_update_garu_zakato`
    has **no** teleport or self-destruct phase: a `SLOT_HIT` Garu plays the shared burst; an active one checks
    craft collision, decrements the fuse, and at zero calls **`garu zakato detonate`** (which frees the slot),
    else moves straight and culls. `install_garu_zakato_detonate` captures the Garu's slot/cell, emits the
    **16-bullet ring** (a 16-count loop over `emit radiating bullet` stepping `radiating angle` by 2 from 0),
    then writes **4 Brag Sparios** into the four **adjacent** flying slots (`gslot+1 … gslot+4`) — copying the
    Garu's cell, the cardinal `slot dx`/`slot dy` from the arcade tables, and `BRAG_SPARIO_TYPE`, then calling
    the shared `init brag spario` (which stamps only the non-kinematic fields) — and finally **frees** the Garu
    (`slot type`/`slot state` = 0) while restoring `slot index` to the Garu's own slot (both the free target and
    the advance-slots loop cursor).

  All three bodies render through the shared Zakato renderer (a `garu zakato` costume-mirror target added beside
  the Spario mirrors), one clone per flying slot, drawing the exclude-craft body while active and forwarding to
  the shared burst frames while hit.
- Scratch evidence: `install_init_brag_zakato`, `install_update_brag_zakato`, `install_brag_zakato_shoot`,
  `install_init_garu_zakato`, `install_update_garu_zakato` and `install_garu_zakato_detonate` (the lifecycle
  procs, reusing `compute aim`, the 32-tier aim tables, the shared `emit radiating bullet`, the shared
  `explode toroid tick`, `init brag spario` and the family move/cull), the Brag OR-branch and Garu branch in
  `install_advance_slots`, the Brag OR-branch in `install_spawn_flying` (and the **absence** of a Garu one — the
  Garu has no formation entry), the Garu debug-key stamp into the first flying slot, `garu_zakato_blocks` for the
  render, and the `BRAG_ZAKATO_*` / `GARU_*` tuning constants in `tools/game_director.py`; the structural
  contract `_air08_failures` and its per-clause negatives (`test_special_pairs_slice_authoring_present` /
  `test_special_pairs_slice_negative_fixtures`) in `tests/test_scratch_project.py`, whose clauses pin the two
  lifecycles as warp, the by-type spawn/dispatch, the **biting entry contrast** (Brag spawns `SLOT_TELEPORT`,
  Garu spawns `SLOT_ACTIVE`), the Brag 32-tier aim, the fire-fan-then-self-explode transition, the shared burst
  reuse, the 5-bullet fan and 16-bullet ring emit counts/steps, the Brag `(rng mod 64) + 1` and Garu
  `(rng mod 32) + 32` fuse draws, the detonation's 4 cardinal-velocity Sparios and the Garu free; the live
  scenarios in `harness/lib/catalog.js` (`brag-zakato-fires-five-bullet-fan` asserting the 5 aimed radiating
  bullets fanned two steps apart with the Brag flipping to self-explode, and
  `garu-zakato-detonates-into-ring-and-four-sparios` asserting the 16-bullet ring, the 4 cardinal-velocity Brag
  Sparios in the adjacent slots, and the freed Garu), each with a biting negative.
- Acceptance criteria: A Brag Zakato teleports in indestructible, becomes hittable, and on its trigger (rnd: a
  random fuse; close-Y: the craft drawing level laterally) fires a **5-bullet aimed fan** then vanishes awarding
  nothing, while a Brag shot down while active scores 600 (rnd) / 1,500 (close-Y); a Garu Zakato enters active
  (no teleport), flies **straight**, and — left alone — **detonates** into a 16-bullet ring plus 4 Brag Sparios
  and vanishes with no score, while a Garu shot before its fuse scores 1,000 (harness
  `brag-zakato-fires-five-bullet-fan`, `garu-zakato-detonates-into-ring-and-four-sparios`, each with a biting
  negative); the operator playtest confirms the felt behavior — a Brag flowering into a bullet fan, and a Garu
  bursting into a full ring flanked by four homing Sparios.
- Fidelity status: Verified line-by-line against the pinned reference this slice (`handle_16_Brag_Zakato_rnd`,
  `handle_17_Brag_Zakato_closeY`, `brag_zakato_explode`, `brag_zakato_shoot`, `handle_18_Garu_Zakato`,
  `garu_zakato_explode`, `init_garu_zakato_explosion` and the `brag_spario_dX_tbl` / `brag_spario_dY_tbl` cardinal
  tables were read at the pin). The behavior matches the reference within the recorded deviations.
- License status: The reference states no reusable license; only instruction-derived behavior and numeric
  constants are transferred (recorded in [the index](../spec/index.md) and the data files). No source text is
  reproduced. The three bodies reuse the credited Aerial Enemies Zakato body frame
  (`src/xevious/assets/provenance.json`, `https://www.spriters-resource.com/arcade/xevious/`, sheet author
  "CrazyCarl"); the distinct Garu art is deferred to a later art pass, so no new sprite crop was added and no
  crop rect required operator pixel-verification for this family.
- Known deviations or uncertainty: (1) **Two arcade frames per tick (tick scaling).** The per-frame reference
  rates are doubled for the port's two-frame tick — the teleport and fuse clocks step `TICK_TIMER_STEP = 2` per
  tick and bodies move by the shared `×4` position step — the same tick scaling every family uses. (2) **MSB byte
  compare expressed as cell arithmetic.** The close-Y level-in-Y test (`_Y` MSB diff, `subq #4` / `addq #8` /
  `jcs`) is expressed as the `player col − slot col` in the `[-4, 3]` lateral band with `>`/`<` guards — the same
  no-bitwise idiom recorded for the base Zakato ([record 034](034-zakato-teleporters.md)). (3) **Coroutine
  re-entry / overloaded `_STATE = 3` expressed as explicit phase states.** The arcade holds its phase in the
  coroutine resume address and reuses `_STATE = 3` as both the shot-down flag and the post-fire benign flag; the
  port splits these into explicit `slot state` sentinels — `SLOT_TELEPORT` (Brag only, indestructible sparkle),
  `SLOT_ACTIVE`, `SLOT_HIT` (shot down → shared burst), `SLOT_SELF_EXPLODE` (Brag post-fire, benign → shared
  burst) — the same explicit-phase mapping recorded for Zakato. (4) **Fixed-adjacency detonation with a one-tick
  Sparios head start.** The arcade writes the 4 Brag Sparios into the 4 objects **immediately after** the Garu
  (obj `0x3C`-`0x3F` after `0x3B`); the port reproduces that adjacency by writing into `gslot+1 … gslot+4` in the
  6-slot flying pool, which requires the Garu to occupy the first flying slot — its only current spawner (the
  debug key) stamps it there. Because those higher slot indices are reached **later in the same** `advance slots`
  pass, the 4 Sparios update once on the tick they spawn — a **one-tick head start** (recorded here; the natural
  add_object spawn that would place a Garu at an arbitrary slot is a deferred follow-up, see (6)). (5) **No enemy
  scroll.** The port has no background-scroll term for flying slots, so the arcade's scroll-during-flight renders
  as pure `slot dx`/`slot dy` motion — the same "no enemy scroll" deviation class recorded for every flying
  family. (6) **Garu natural-spawn reachability deferred.** In the arcade the Garu Zakato is scheduled by the
  area `add_object` records (areas 9/10/14), a spawn source the port has not yet built (the schedule-consumer
  seam is dormant); until it lands, the Garu is reachable for playtest via the **debug key** (which stamps a solo
  Garu into the first flying slot), and the two Brag variants arrive through the normal formation waves. Recorded
  so a reviewer does not read the Garu's absence from natural play as a build gap. (7) **Garu art deferred.** The
  Garu draws the Zakato body frame as a documented stand-in (see License status); the motion, points, fuse and
  detonation are unaffected.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
