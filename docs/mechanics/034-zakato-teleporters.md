# Zakato teleporters (four variants over one teleport / active / self-destruct core)

- Mechanic: The base Zakato family (AIR-07) — four flying object types, **slow** `0x12`, **close-Y** `0x13`,
  **fast** `0x14` and **continuous** `0x15`, that share **one** teleport-in phase, **one** hittable active
  body and **one** self-destruct burst, differing only in how they move, in **when** they fire their single
  shot, and in points. All four **teleport in**: they arrive **indestructible** and hold in place while a
  ~20-frame sparkle plays, then become hittable and start moving. Each fires **exactly one** aimed bullet —
  the slow/fast variants on a random countdown, the close-Y/continuous variants when the craft draws level in
  the lateral axis — and then **self-destructs**, playing its own burst and vanishing while **awarding
  nothing**. A Zakato shot down while active instead scores its variant value (100 / 200 / 150 / 300) through
  the shared flying-hit path. It shares the blaster hit window, the shared explosion, the aimed-bullet
  allocator and the per-slot fields with the earlier aerial families ([record 023](023-aiming-and-slot-positions.md)
  slot fields, [record 026](026-enemy-bullets-and-collision-death.md) bullets), reuses the multi-type idiom of
  Zoshi/Jara ([record 030](030-zoshi-variants.md), [record 031](031-jara-variants.md)) and the fast/32-tier
  craft aim of Kapi/Terrazi ([record 028](028-kapi-peel-away-dive.md)).
- Derived behavior: Each Zakato is stamped **indestructible** at spawn (`_STATE = 3`) and holds in place while
  the teleport-in sparkle animates its 6-code table in reverse over ~20 frames; the object is unkillable in
  this phase. When the sparkle completes the handler falls through to its `zakato_NN_main` body, which stamps
  the active body code `0x11`, sets `_STATE = 2` (active/hittable), and commits the variant's motion and shot
  schedule: the **straight** variants (slow/close-Y) descend on the raw scroll-axis velocity `_dX = 16`,
  `_dY = 0`; the **aimed** variants (fast/continuous) aim at the craft's current cell on the 32-magnitude
  generic angle table. The **fused** variants seed a random shot countdown at commit — slow `(rng & 0xff) + 1`
  = 1–256 frames, fast `(rng & 0x3f) + 1` = 1–64 frames — and fire when it decrements to zero; the **Y-triggered**
  variants (close-Y/continuous) fire the instant the signed MSB gap `solvalou._Y − self._Y` lands in the close
  band (`− 4` then `+ 8`, carrying → the `[−4, +3]` cell band). Firing is terminal: `zakato_shoot` allocates
  **one** aimed bullet, re-inits the timer, sets `_STATE = 3` ("flag benign"), and falls into
  `zakato_explode_and_remove`, which plays the forward 6-code burst and frees the slot **with no score**. The
  points are `_PTS` bytes 15 / 21 / 18 / 27 (100 / 200 / 150 / 300). The teleport X-scatter (`init_teleport`
  picks a random column) is the arcade's cosmetic re-placement; the shared explosion, the aimed bullet and the
  fast angle table are the derived data of the earlier records.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. Line citations are
  `src/xevious_main.68k` unless noted. The four handlers are `handle_12_Zakato_slow` 3733–3742 (init_teleport,
  `_PTS = 15` = 100 pts, `_dY = 0`, `_dX = 16`), `handle_13_Zakato_closeY` 3775–3784 (`_PTS = 21` = 200 pts,
  straight), `handle_14_Zakato_fast` 3804–3811 (`_PTS = 18` = 150 pts) and `handle_15_Zakato` 3834–3841
  (`_PTS = 27` = 300 pts), each calling `init_teleport` 3994–4004 (`_STATE = 3` indestructible, random X, the
  craft-independent random Y via `gen_random_Y_store_obj` 5147) then `zakato_teleport` 3961–3967 (the sparkle
  clock, carry-clear on completion) and, until it completes, `scroll_sprite_X`. The active bodies are
  `zakato_12_main` 3744–3759 (random fuse `(rng & 0xff) + 1` via `pseudo_random_gen` 1428, `_CODE = 0x11`,
  `_STATE = 2`, `subq/jeq` shot on the fuse, else `move_object_dX_dY` 4817), `zakato_13_main` 3786–3800 (the
  close-Y test `(solvalou._Y − self._Y) − 4 + 8` → carry → `zakato_shoot`), `zakato_14_main` 3813–3830 (random
  fuse `(rng & 0x3f) + 1`, aim via `calc_dX_dY_for_vector_to_solvalou` 5119 on `angle_dX_dY_tbl` 6360) and
  `zakato_15_main` 3843–3859 (aimed, close-Y trigger). The `_STATE = 3` hit test jumps to `flying_enemy_hit`
  4865. The single shot and suicide are `zakato_shoot` 3761–3764 (`init_new_bullet` 5012 once, `_STATE = 3`
  benign) falling into `zakato_explode_and_remove` 3766–3771 → `zakato_explode` 3931–3950 (the forward burst
  `zakato_exploding_sprite_tbl` 3953–3959, `(TIMER>>2) & 7`, done at `≥ 5`) → `remove_zakato` 3926–3929
  (clears `_TYPE`/`_STATE`, **no score**). The sparkle animation is `zakato_teleport_sparkles` 3969–3983 over
  the reversed table `zakato_teleport_sprite_tbl` 3986–3992. The behavior is a port mapping of that logic, not
  copied text; the Zakato paragraph of [aerial enemies](../spec/aerial-enemies.md) is the settled description
  this slice implements.
- Transfer class: Behavioral port (instruction-derived control flow and numeric constants; no source text,
  ROM, or media copied). The aim table, value table and slot layout are the derived data of records
  [022](022-fire-permission-masks.md), [023](023-aiming-and-slot-positions.md) and
  [025](025-blaster-to-air-hit.md).
- Scratch interpretation: The ordered walk `advance slots` (SYS-04) dispatches a Zakato-typed slot (type
  `ZAKATO_SLOW_TYPE` = 18 / `ZAKATO_CLOSEY_TYPE` = 19 / `ZAKATO_FAST_TYPE` = 20 / `ZAKATO_CONT_TYPE` = 21)
  through **one** branch to the warp proc `update zakato`; `spawn flying enemies` inits all four types through
  the **one** shared `init zakato`. The axes follow the family convention: `slot x` is the scroll/forward row
  (arcade `_X`), `slot y` the lateral column (arcade `_Y`). With no per-object coroutine re-entry address, the
  port carries the phase **explicitly** in `slot state`:
  - `SLOT_TELEPORT` (4) — teleporting in: **indestructible**, because the shared `check air hit` gate scores
    only `== SLOT_ACTIVE` slots, so a teleporting Zakato is skipped (the arcade's `_STATE = 3` at
    `init_teleport`); it holds in place (`slot dx = slot dy = 0`) while the ~20-frame reversed sparkle plays.
  - `SLOT_ACTIVE` (1) — hittable and moving: it fires **exactly one** aimed bullet — on the random fuse
    (slow/fast) or when the lateral offset `player col − self col` is within `[ZAKATO_CLOSEY_LOW,
    ZAKATO_CLOSEY_HIGH]` = `[−4, 3]` (close-Y/continuous) — then flips **itself** to `SLOT_SELF_EXPLODE`;
    killed by a shot first, the detector flips it to `SLOT_HIT` and scores its value.
  - `SLOT_SELF_EXPLODE` (5) — fired and vanishing: benign (again skipped by the hit gate), holding still while
    its own 20-frame burst plays, then freed **awarding nothing**.
  - `SLOT_HIT` (2) — shot down while active: the **shared** flying explosion (`explode toroid tick`), its value
    already scored by the detector, exactly like every other flying family.

  `install_init_zakato` draws the spawn column through the shared helper with the **craft-gap exclusion OFF**
  (`gen_random_Y_store_obj` 5147 — an in-range clamp only, *no* craft-proximity reject, so a Zakato can
  teleport in over the craft's own column), plus the **`+1`-cell teleport offset** `init_teleport` applies
  (`add.b #1,(_Y,a5)` 4000, modelled as `col_offset=1`),
  stamps `SLOT_TELEPORT` (not `SLOT_ACTIVE` — the structural indestructibility), sets `slot code` to the active
  body ordinal, captures **no** fire mask (a Zakato fires structurally, not under the periodic gate), and
  stamps the per-variant `slot pts` by `walk type`. `install_update_zakato` runs a top HIT-vs-else guard
  (explode on `SLOT_HIT`, else the shot detector then the phase machine); the `SLOT_TELEPORT` arm advances the
  sparkle clock and, on completion, commits `SLOT_ACTIVE` + the straight `dx = 16` or the 32-tier aim + the
  slow/fast random fuse; the `SLOT_ACTIVE` arm checks craft collision, decrements the fuse for the fused
  variants, and — on the fuse-elapsed **or** in-band trigger — fires **one** aimed bullet via
  `_fire_aimed_bullet`, flips to `SLOT_SELF_EXPLODE`, and zeroes the velocity; otherwise it moves `4×` velocity
  and culls off any edge. The fire-once guarantee is **structural**: the single `_fire_aimed_bullet` call is
  nested in the fire arm of an `if/else` whose taken branch **leaves `SLOT_ACTIVE`**, so it can never recur —
  the same downward-SUBSTACK gate that closed the Torkan/Jara "fires every tick" gaps
  ([record 029](029-torkan-attack-and-retreat.md), [record 031](031-jara-variants.md)). The self-destruct
  reuses the shared 20-frame free clock (`explode toroid tick`); it differs from a shot kill only in the sprite
  the renderer draws (self burst vs shared burst, keyed on the state) and in awarding nothing — the detector,
  not the tick, awards, and it never runs on a self-destructing slot.
- Scratch evidence: `install_init_zakato` and `install_update_zakato` (the shared lifecycle procs, reusing
  `_fire_aimed_bullet`, `COMPUTE_AIM`, the 32-tier `aim dx 32`/`aim dy 32` tables, the craft-independent
  `_draw_spawn_column` (`exclude_craft=False`, `col_offset=1`), the shared `explode toroid tick`, and the
  inlined move/cull), the single Zakato branch
  in `install_advance_slots`, the four per-type spawn branches in `install_spawn_flying`, `zakato_blocks` for
  the body/self-burst render, the four base-Zakato entries in `DEBUG_SPAWN_FAMILIES` with their
  `ZAKATO_*_FORMATION_OFFSET` constants, and the `ZAKATO_*` tuning constants in `tools/game_director.py`; the
  structural contract `_air07_failures` and its per-clause negatives (`test_zakato_slice_authoring_present` /
  `test_zakato_slice_negative_fixtures`) in `tests/test_scratch_project.py`, whose clauses pin the shared
  lifecycle procs, the by-type spawn and single shared dispatch, that spawn stamps the **indestructible**
  `SLOT_TELEPORT` (not `SLOT_ACTIVE`), the per-variant points gated by `walk type`, the **absent** fire mask,
  the craft-independent draw (`gen_random_Y_store_obj`, no craft reject, `+1` teleport offset), that the
  teleport commits `SLOT_ACTIVE` and sets the straight `dx` / 32-tier aim,
  that it seeds the slow/fast random fuse, that it fires **exactly one** aimed bullet **then** self-destructs
  (with corrupters that ungate the fire and that skip the self-destruct flip), that the proximity band carries
  both constants, and that the self-destruct runs the shared tick and awards nothing while a shot-kill plays
  the shared explosion; the live scenarios in `harness/lib/catalog.js`
  (`zakato-teleports-in-then-commits-active` asserting the indestructible hold then the aimed commit,
  `zakato-fires-once-then-self-destructs` asserting the one-shot suicide that frees the slot without scoring,
  and the extended `debug-key-cycles-families`), each with a biting negative.
- Acceptance criteria: Four Zakato object types spawn by type from the debug cycle (and, for fast/continuous,
  the natural wave); each teleports in **indestructible** and held in place, then becomes hittable and moves —
  slow/close-Y straight, fast/continuous aimed at the craft; each fires **exactly one** aimed bullet (slow/fast
  on a random fuse, close-Y/continuous when the craft is level in the lateral axis) then **self-destructs
  awarding nothing**; a Zakato shot down while active scores 100 / 200 / 150 / 300 by variant (harness
  `zakato-teleports-in-then-commits-active`, `zakato-fires-once-then-self-destructs`,
  `debug-key-cycles-families`, each with a biting negative); the operator playtest confirms the felt behavior —
  a Zakato winking in, holding a beat, loosing a single shot, and vanishing on its own.
- Fidelity status: Verified line-by-line against the pinned reference this slice (the four handlers, the shared
  `init_teleport`, `zakato_teleport`, the two active-body forms, the random-fuse and close-Y triggers,
  `zakato_shoot`, `zakato_explode_and_remove`/`zakato_explode`/`remove_zakato`, and both sprite tables were
  read at the pin). The behavior matches the reference within the recorded deviations below.
- License status: The reference states no reusable license; only instruction-derived behavior and numeric
  constants are transferred (recorded in [the index](../spec/index.md) and the data files). No source text is
  reproduced. The single Zakato body frame is the Aerial Enemies rip credited in
  `src/xevious/assets/provenance.json` (`https://www.spriters-resource.com/arcade/xevious/`, sheet author
  "CrazyCarl"); the teleport sparkle and self-destruct burst reuse the shared explosion frames.
- Known deviations or uncertainty: (1) **Two arcade frames per tick (tick scaling).** The per-frame reference
  rates are doubled for the port's two-frame tick — the sparkle/burst clock advances `TICK_TIMER_STEP = 2` per
  tick (completing at `ZAKATO_PHASE_FRAMES = 20`, the shared flying-explosion duration), the fuse decrements
  `2` per tick, and velocities are scaled by the shared `×4` position step — the same tick scaling every family
  uses. (2) **Carry/bitwise math expressed as comparison and modulo.** Scratch has no carry flag or bitwise
  ops, so the arcade's close-Y band (the `subq #4` / `addq #8` byte-carry test) is expressed as the arithmetic
  comparison `−4 ≤ (player col − self col) ≤ 3`, the random fuse masks `& 0xff` / `& 0x3f` as `mod 256` /
  `mod 64`, and the sparkle/burst index `(TIMER>>2) & 7` (capped at five) as `floor(clock / 4) mod 8` in the
  renderer — the same no-bitwise idiom recorded for the fire masks (record 022) and the earlier families
  ([[verify-behavior-against-pinned-reference]] on the sign — the close-Y band is on the lateral `_Y` axis,
  matching the family convention). (3) **Coroutine re-entry and the overloaded `_STATE = 3` expressed as
  explicit phase states.** The arcade holds its phase in the coroutine resume address (`SET_REENTRY_ADDR_HERE`)
  and reuses `_STATE = 3` for **three** distinct meanings — indestructible-while-teleporting, the shot-down hit
  flag, and benign-after-firing — disambiguated only by which re-entry address is live. The port has no
  coroutine resume, so it splits these into explicit `slot state` sentinels: `SLOT_TELEPORT` (indestructible
  teleport-in), `SLOT_HIT` (shot down, shared explosion, scored) and `SLOT_SELF_EXPLODE` (benign self-destruct,
  no score) — the same explicit-phase mapping recorded for Torkan/Jara. (4) **One-shot fire via a
  phase-transition gate, not a resume-address lock.** The arcade guarantees the single shot because the resume
  address moves to `zakato_explode_and_remove`, so `zakato_shoot` runs once; the port guarantees it structurally
  by nesting the single `_fire_aimed_bullet` in the fire branch of an `if/else` whose taken branch leaves
  `SLOT_ACTIVE`. No fire mask and no fire timer of the periodic kind are used — faithful to the Zakato handlers
  never setting `_FFREQ`. (5) **Fire-once-aimed bullet, not per-frame homing.** The arcade bullet re-vectors onto
  the craft every frame; this port aims the bullet once at allocation and does not re-home
  ([record 026](026-enemy-bullets-and-collision-death.md), pre-existing). (6) **No enemy scroll.** The port has
  no background-scroll term for flying slots (they move purely by `slot dx`/`slot dy`), so the arcade's
  scroll-during-teleport and scroll-during-explode (`scroll_sprite_X`) render as holding still — the same "no
  enemy scroll" deviation class recorded for every flying family. (7) **Teleport X-scatter deferred.** The
  arcade `init_teleport` re-places the object at a random column as it winks in; the port enters from the shared
  top-row spawn column like every flying family and does not re-scatter — a cosmetic deviation, the shot
  schedule and phase machine are unaffected. (8) **Natural reachability differs by variant.** In the built
  areas 1–16 the formation waves reach only the **fast** (`0x14`) and **continuous** (`0x15`) variants (the
  `ZAKATO_FAST_FORMATION_OFFSET` = 60 and `ZAKATO_CONT_FORMATION_OFFSET` = 110 runs appear in the area
  schedules); **slow** (`0x12`) and **close-Y** (`0x13`) are not scheduled in any built area, so the debug key
  (their `DEBUG_SPAWN_FAMILIES` entries at offsets 54 and 57) is the only way to see them until later areas are
  wired — recorded so a reviewer does not read their absence from normal play as a build gap.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
