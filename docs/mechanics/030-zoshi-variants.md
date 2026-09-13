# Zoshi variants (three object types over one shared movement core)

- Mechanic: The Zoshi family (AIR-03) — three distinct scheduled object types that share **one**
  movement/animation/fire core rather than one type with a variant tag. All three spin a four-code
  animation, drift on the slow toroid tier aimed at the craft, and fire the **same aimed shot** on a
  masked periodic cadence; they differ only in their spawn draw, entry row, points, and — the distinctive
  trait — how each re-headings **its own drift** at every shot. The top and bottom variants re-aim their
  drift **toward the craft** each shot (a homing-ish curve); the random variant re-headings its drift to
  an **erratic** angle each shot — the arcade's "random Zoshi," whose *movement* wanders, not its shot.
  It shares the blaster hit window, the hit explosion, the aimed-bullet allocation, and the per-slot slot
  fields with the earlier aerial families ([record 027](027-terrazi-and-fire-permission.md), slot fields
  from [record 023](023-aiming-and-slot-positions.md)), and it fires through the shared periodic
  fire-permission gate ([record 022](022-fire-permission-masks.md)) under the Zoshi mask.
- Derived behavior: All three enter aimed at the craft on the toroid (1.5 px/frame) tier and score by
  variant. **Top** (`0x0D`, 100 pts): plain random lateral spawn column (the non-excluding draw), top-row
  entry. **Bottom** (`0x0E`, 100 pts): craft-excluding spawn column (the reject-and-redraw draw the
  Toroid/Terrazi use), fixed **bottom-edge** entry row (`_X = #40`), then drifts up-field. **Random**
  (`0x0C`, 70 pts): plain random spawn column, top-row entry. Every tick each Zoshi spins its sprite
  (`0x28 + (clock & 3)` → codes `0x28`–`0x2B`), moves under its drift, and is culled off any edge. On its
  masked fire tick a Zoshi does three things in order: (1) re-headings its own drift — top/bottom recompute
  the vector **to the craft**, the random variant draws a **random** heading; (2) fires one aimed bullet;
  (3) reloads its shot timer under the Zoshi fire-frequency mask. The bullet is the shared craft-aimed
  bullet — in the arcade a TYPE-6 bullet that re-vectors onto the craft **every frame** (homing); in this
  port the pre-existing fire-once-aimed bullet ([record 026](026-enemy-bullets-and-collision-death.md)).
  Either way the **shot is aimed at the craft for all three variants**; only the enemy's own drift differs.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. Line citations are
  `src/xevious_main.68k` unless noted. The three handlers are `handle_0E_Zoshi_bottom` (3414–3419, the
  `_X = #40` fixed bottom entry and the craft-excluding `gen_rnd_spriteY` draw, then a jump into the shared
  init), `handle_0D_Zoshi_top` (3422–3426, the plain `gen_random_Y_store_obj` draw), and
  `handle_0C_Zoshi_rnd` (3463–3491, the plain draw, the 70-point score, and its own fire block), over the
  shared `zoshi_0D_init` (3427–3451: the initial toward-craft aim via `calc_dX_dY_for_vector_to_solvalou`,
  the 100-point score, the mask capture and shot-timer seed, the every-8-frame countdown, and — at expiry —
  the timer reload, the **toward-craft** drift re-aim at 3446–3447, and `init_new_bullet` at 3448) and the
  spin cores `zoshi_0D_main` (3452–3461) / `zoshi_0C_main` (3492–3499). The random variant's re-heading is
  `handle_0C_Zoshi_rnd` 3487–3489 (`pseudo_random_gen`, then `get_dX_dY_and_cpy_to_obj` 5129–5134, which
  writes the **enemy's** `_dX`/`_dY` from an angle index). The toward aim is `calc_dX_dY_for_vector_to_solvalou`
  (5119–5128, which also writes the enemy's `_dX`/`_dY`) on `angle_dX_dY_toroid_tbl` (6394). The shared
  bullet is `init_new_bullet` (5012–5018) → `found_idle_bullet_slot` (5039–5041) →
  `set_state_and_copy_obj_coords` (5032–5035), which copies only the firer's **position** to the bullet —
  never a direction — after which `handle_06_Bullet` (4278–4283) re-aims that TYPE-6 bullet at the craft
  every frame. The behavior is a port mapping of that logic, not copied text; the Zoshi paragraph of
  [aerial enemies](../spec/aerial-enemies.md) is the settled description this slice implements (corrected
  this slice — see deviation 2).
- Transfer class: Behavioral port (instruction-derived control flow and numeric constants; no source text,
  ROM, or media copied). The aim table and slot layout are the derived data of records
  [022](022-fire-permission-masks.md) and [023](023-aiming-and-slot-positions.md).
- Scratch interpretation: The ordered walk `advance slots` (SYS-04) dispatches a Zoshi-typed slot (type
  `0x0C`, `0x0D`, or `0x0E`) through **one** branch to the warp proc `update zoshi`; `spawn flying enemies`
  inits each by type through its own thin `init zoshi top` / `init zoshi bottom` / `init zoshi rnd`. Each
  init shares one body (`_install_zoshi_init`): draw the spawn column through the shared helper (bottom with
  the craft-gap exclusion **on**, top/rnd **off**), set the entry row (bottom = `ZOSHI_BOTTOM_EDGE_X` cells,
  top/rnd = the top row), aim the initial drift on the `aim dx 24`/`aim dy 24` toroid tier, set `slot pts`
  to the variant's value-table slot (100 or 70 pts), set `slot code` to `0x28`, capture the Zoshi mask into
  `slot fire mask`, and seed `slot fire timer` to `(rng mod (mask + 1)) + 1`. `slot flag` is unused (0):
  the aimed-vs-random re-heading keys on `slot type`, never on `slot flag`. The shared `update zoshi` runs
  under the active-slot gate:
  - animate: `slot code = 0x28 + (tick mod ZOSHI_ANIM_FRAMES)` (codes `0x28`–`0x2B`);
  - fire, masked and phase-gated: when `tick mod FIRE_GATE_PHASE_TICKS == 0` **and** the per-slot
    `slot fire timer` counts down to `0`, re-heading the drift by type — for `slot type == ZOSHI_RND_TYPE`
    draw a random heading (`rng step`, then index `floor(rng out / 8) + 1`, read `aim dx 24`/`aim dy 24`);
    otherwise recompute the toward-craft aim (`compute aim index`, read the **same** 24-tier) — then fire
    **one** shared aimed bullet (`_fire_aimed_bullet`, which reads the 32-tier `aim dx 32`/`aim dy 32` for
    the bullet), and reload `slot fire timer` to `(rng mod (mask + 1)) + 1`;
  - move and cull: `slot x += 4 * slot dx` / `slot y += 4 * slot dy`, four-edge cull frees the slot.

  The 24-vs-32 tier split is the clean discriminator between the **enemy drift** (24 tier) and the **bullet
  aim** (32 tier). The per-tick constants are the per-frame reference values times two (one build tick is
  two arcade frames): the every-8-frame fire phase becomes every `FIRE_GATE_PHASE_TICKS = 4` ticks,
  velocities scaled by the shared `×4` position step. Gameplay math is exact integer arithmetic in arcade
  units.
- Scratch evidence: `_install_zoshi_init` and the three thin wrappers `install_init_zoshi_top` /
  `install_init_zoshi_bottom` / `install_init_zoshi_rnd`, the shared `install_update_zoshi` (reusing
  `_fire_aimed_bullet`, `COMPUTE_AIM_PROCCODE`, the `RNG_PROCCODE` draw, and the toroid-tier tables), the
  single Zoshi branch in `install_advance_slots`, the three per-type spawn branches in
  `install_spawn_flying`, `zoshi_blocks` for the four-frame spin render, and the `ZOSHI_*` tuning constants
  in `tools/game_director.py`; the structural contract `_air03_failures` and its per-clause negatives
  (`test_zoshi_slice_authoring_present` / `test_zoshi_slice_negative_fixtures`) in
  `tests/test_scratch_project.py`, whose clauses pin the four lifecycle procs, the by-type spawn and the
  single shared dispatch, the 24-tier initial aim, the 100/100/70 awards, the bottom fixed-edge entry, the
  mask capture, that all three fire exactly **one** shared aimed bullet reading the 32-tier, the phase- and
  timer-gated fire cadence, and — structurally, since the settling harness cannot separate a random draw
  from an aim over a single frame — that the **random** branch re-headings from an `rng step` draw with
  **no** `compute aim` call while the **aimed** branch re-headings from `compute aim` with **no** `rng`
  draw (the biting aimed-vs-random pair, on the enemy drift, not the bullet); the live scenarios in
  `harness/lib/catalog.js` (`zoshi-top-aims-and-fires` asserting the shot's lateral velocity points back
  toward the craft column, `zoshi-rnd-veers-erratically` asserting the enemy's drift takes a variety of
  headings where a toward-aim would hold one, `zoshi-bottom-enters-edge` asserting the bottom variant is
  its own reachable type, and the extended `debug-key-cycles-families`), each with a biting negative.
- Acceptance criteria: Three Zoshi object types spawn by type from the debug cycle (and the natural wave),
  each drifting aimed at the craft and spinning its four-frame animation; all three fire the **same** aimed
  shot toward the craft on the masked cadence; the top and bottom variants curve their own drift toward the
  craft at each shot while the random variant veers to erratic headings; the bottom variant enters at the
  fixed bottom edge and the other two from the top; points are 100/100/70 (harness
  `zoshi-top-aims-and-fires`, `zoshi-rnd-veers-erratically`, `zoshi-bottom-enters-edge`,
  `debug-key-cycles-families`, each with a biting negative; the exact bottom entry row is pinned in
  `_air03_failures`, which the settling harness cannot observe — see deviation 7); the operator playtest
  confirms the felt behavior — three flyers, all shooting at the craft, the random one **wandering** in its
  movement while its shot still tracks the craft.
- Fidelity status: Verified line-by-line against the pinned reference this slice (the three handlers, the
  shared init and spin cores, the bullet allocation and copy routine, the TYPE-6 bullet handler, the aim
  and re-heading routines, and the toroid tier table were read at the pin). The behavior matches the
  reference within the recorded deviations below; the spec's Zoshi paragraph was corrected to the source in
  the same slice under `guardrail-ack` (deviation 2).
- License status: The reference states no reusable license; only instruction-derived behavior and numeric
  constants are transferred (recorded in [the index](../spec/index.md) and the data files). No source text
  is reproduced.
- Known deviations or uncertainty: (1) **No enemy scroll; top/random enter from the top row.** The port has
  no background-scroll term for flying slots (they move purely by `slot dx`/`slot dy`), so the top and
  random variants enter from the top row and drift under their aim — the same "no enemy scroll" deviation
  class already recorded for the Toroid, Terrazi, Kapi, and Torkan. The bottom variant's fixed `_X = #40`
  edge entry is reproduced literally. (2) **Corrected the spec's "random-direction shot" to an aimed shot
  with erratic movement (spec error).** The settled prose read that the random variant "fires in a
  genuinely random direction (its angle index is a raw draw from the stream)." The source contradicts this:
  a Zoshi bullet is a TYPE-6 bullet, and `set_state_and_copy_obj_coords` copies only the firer's **position**
  to it (no direction), after which `handle_06_Bullet` re-aims it at the craft every frame — so **all three
  variants' shots home on the craft**. The `handle_0C` random draw writes the **enemy's** `_dX`/`_dY` (a5),
  driving `move_object_dX_dY` — i.e. the enemy's *movement* is what wanders. The source's own `handle_0C`
  comment "shoots in a random direction" is a loose annotation of that draw, not the bullet's flight. The
  build follows the source; the spec was corrected under `guardrail-ack`. (3) **The random re-heading is a
  clean RNG draw, not the arcade's stale-register value.** In the arcade the `0x0C` fire block calls
  `pseudo_random_gen` (result in `d0`) but then `get_dX_dY_and_cpy_to_obj` reads the angle index from
  `d2` — a register `pseudo_random_gen` never writes (a leftover from the prior aim computation) — so the
  arcade's erratic heading is a stale-register value, not a fresh draw. The port has no leftover-register
  state, so it renders the erratic re-heading as a clean `rng step` draw. The observable — an unpredictable
  heading that varies shot to shot — is faithful; the exact sequence is not, and cannot be without
  modelling register aliasing. (4) **Fire-once-aimed bullet, not per-frame homing.** The arcade TYPE-6
  bullet re-vectors onto the craft every frame; this port aims the bullet once at allocation and does not
  re-home ([record 026](026-enemy-bullets-and-collision-death.md), pre-existing). The shot is still aimed
  at the craft for all three variants at fire time; it simply does not curve after launch. (5) **Masked
  spawn-timer seed for all three; the arcade `0x0D`/`0x0E` spawn quirk is not reproduced.** The arcade
  `0x0D`/`0x0E` spawn seeds the first shot timer with `and.b d0,(_FFREQ,a5)` (3432) — the AND operands
  swapped versus the `0x0C` spawn's `and.b (_FFREQ,a5),d0` — which stores an **unmasked** `rnd + 1` first
  timer and corrupts the object's mask field to `mask & rnd`; every re-fire block (all three) masks
  correctly. This is a transcode artifact, not intended cadence; the port seeds all three uniformly with
  the masked `(rng mod (mask + 1)) + 1` that the `0x0C` spawn and all three re-fires use. (6) **No-bitwise
  angle index.** Scratch has no bitwise ops, so the arcade's `lsr #3` / `and #0x1f` angle fold is expressed
  arithmetically as `floor(rng out / 8) + 1` (1-based index into the 32-entry tier table), the same
  no-bitwise idiom recorded for the fire masks (record 022) and Torkan (record 029). (7) **The fixed bottom
  entry row is a spawn-instant value the settling harness cannot read, so it is pinned structurally.** Both
  the top-row (0) and bottom-edge (40) entrants converge on and overshoot the craft row within a single
  settling step (the harness runs an unfixed number of ticks per `_step`), so no post-settling row
  threshold faithfully separates them; the exact entry row `ZOSHI_BOTTOM_EDGE_X * SLOT_UNITS_PER_CELL` is
  pinned as an exact-value clause in `_air03_failures` (`zoshi-bottom-fixed-edge-entry`), and the live
  `zoshi-bottom-enters-edge` scenario asserts the bottom variant's reachability instead. (8) **Two arcade
  frames per tick.** All per-frame reference rates are doubled for the port's two-frame tick (the
  every-8-frame fire phase becomes every 4 ticks; velocities scaled by the shared position step), the same
  tick scaling every family uses. (9) **Four extracted frames match the four arcade spin codes.** The
  arcade spins codes `0x28`–`0x2B` (four codes); the Aerial Enemies rip supplies four distinct Zoshi
  frames, so the render maps one-to-one with no hold-last idiom, on the shared 16×16 sprite cell, mirrored
  by family prefix onto the shared sprite-extraction proof, playing the shared explosion on a hit.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
