# Torkan attack and retreat (the first three-phase aerial family)

- Mechanic: The Torkan family (AIR-02.torkan) — the first aerial enemy that runs a **three-phase**
  approach → attack → retreat coroutine rather than a two-state dive. A Torkan spawns from the
  formation wave, aims at the craft on the 32-magnitude (2 px/frame) tier, and approaches while an
  initial shot delay runs down; when the delay expires it fires **one** aimed bullet **directly** —
  not through the shared fire-permission gate, and with no fire mask — then stops advancing on its own
  vector, hovers in place while it animates for a fixed window, and finally re-aims **once** 180°
  **away** from the craft onto the fast 48-magnitude (3 px/frame) tier and flees straight until culled.
  It shares the blaster hit window and the hit explosion with the Toroid, and reuses the aimed-bullet
  allocation and the per-slot slot fields laid down for the earlier firing families
  ([record 027](027-terrazi-and-fire-permission.md), slot fields from
  [record 023](023-aiming-and-slot-positions.md)) — but it deliberately does **not** use the periodic
  fire-permission gate ([record 022](022-fire-permission-masks.md)); its single un-gated shot is the
  faithful mapping of the arcade routine.
- Derived behavior: A Torkan draws a **plain random lateral spawn column** — the *non-excluding* draw,
  with **no** craft-proximity reject-and-redraw (the same draw the Kapi uses,
  [record 028](028-kapi-peel-away-dive.md)) — enters from the top row, aims at the craft at 2 px/frame,
  scores 50, and seeds an initial shot delay of a random `64–127` frames. **Phase 1 (approach):** it
  moves under its aim and counts the shot delay down each frame; it does not fire yet. **Phase 2 (fire,
  once):** when the delay reaches zero it fires exactly one aimed bullet directly and transitions — the
  shot is issued inside the expiry transition, so it can never repeat. **Phase 3 (hover/animate):** it
  ceases moving on its own vector (it only rides the background scroll in the arcade) and cycles a
  seven-code sprite animation (`0x10`–`0x16`) for a 28-frame window driven by its own timer. **Phase 4
  (re-aim, once, then flee):** at the end of that window it recomputes the angle to the craft, adds
  `0x80` — a true 180° flip — and reads the **fast** 48-magnitude tier, so the retreat vector points
  **directly away** from the craft at 3 px/frame; from then on it simply flies that away vector until it
  leaves the play field. It is culled on any edge, and it dies to a blaster shot or kills the craft on
  contact through the shared windows.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. Line citations are
  `src/xevious_main.68k` unless noted. The Torkan handler is `handle_0F_Torkan` (3357–3376, the spawn,
  the toward aim, the 50-point score, the `(rnd & 0x3f) + 0x40` = `64–127` shot delay, and the
  per-frame countdown that branches to the shot at zero), `torkan_shoot` (3378–3393, the single direct
  `init_new_bullet`, then the `(TIMER>>2)&0x0f` clock that animates codes `0x10`–`0x16` while riding
  `scroll_sprite_X` and branches to the re-aim when that field reaches `7` — i.e. after the timer
  advances 28), and `torkan_update_dir` (3395–3407, the one-time re-aim: `get_index_for_angle` on the
  craft offset, `add.b #0x80`, the fast `angle_dX_dY_terrazi_torkan_tbl` (6325), then flee via
  `move_object_dX_dY`). The toward aim is the generic `angle_dX_dY_tbl` (6360) through
  `calc_dX_dY_for_vector_to_solvalou` (5119–5128); the spawn column is the non-excluding
  `gen_random_Y_store_obj` (5147–5154); the direct shot is `init_new_bullet` (5012). The behavior is a
  port mapping of that logic, not copied text; the shared-rules and Torkan paragraphs of
  [aerial enemies](../spec/aerial-enemies.md) are the settled description this slice implements.
- Transfer class: Behavioral port (instruction-derived control flow and numeric constants; no source
  text, ROM, or media copied). The aim tables and slot layout are the derived data of records
  [022](022-fire-permission-masks.md) and [023](023-aiming-and-slot-positions.md).
- Scratch interpretation: The ordered walk `advance slots` (SYS-04) dispatches a Torkan-typed slot
  (`0x0F`) to the warp proc `update torkan`; `spawn flying enemies` inits it by type through
  `init torkan`. `init torkan` draws the spawn column through the shared helper **with the craft-gap
  exclusion switched off**, aims on the `aim dx 32`/`aim dy 32` tier, sets `slot pts` to the 50-point
  value-table slot and `slot code` to `0x10`, sets `slot flag` to the approach phase, and seeds
  `slot fire timer` to `(rng mod 64) + 64` (the `64–127` shot delay). It captures **no** fire mask.
  `update torkan` is a three-flag phase machine under the active-slot gate, each transition nested
  inside its own phase's `if slot flag == <phase>` guard so it fires exactly once:
  - **approach** (`slot flag == 0`): decrement `slot fire timer` by the two-frame tick step; when it
    reaches zero, call `_fire_aimed_bullet` **once** (the same aimed bullet every family fires, aimed at
    the craft at allocation time), reset `slot timer` to 0, and set `slot flag` to hover. The fire lives
    inside this expiry transition, so it cannot re-fire the next tick.
  - **hover** (`slot flag == 1`): hold position by zeroing `slot dx` and `slot dy` (the no-enemy-scroll
    mapping of the arcade `scroll_sprite_X` hover, deviation 1); when `slot timer` reaches the 28-frame
    window end, re-aim **away**: set the aim diffs **toward** the craft (`player − self`, the same
    orientation as the spawn aim), call `compute aim index`, then add the half-turn to the derived index
    (below) — the flip, not the diffs, is what points the vector away — and read the **48-tier**
    `aim dx 48`/`aim dy 48` into `slot dx`/`slot dy`, and set `slot flag` to flee. The re-aim reuses the
    un-rounded folded aim base and adds `16`
    (`(floor(base/8) + 16) mod 32`), which is exactly the arcade's `add.b #0x80` before its `lsr #3`
    (0x80 is divisible by 8, so the flip is identical either side of the divide) — and it deliberately
    omits the toward path's `addq #4,d2` rounding, matching `torkan_update_dir`, which flips without it.
  - **flee** (`slot flag == 2`): no work beyond the shared move; the away vector is already set.

  The shared move advances `slot x += 4 * slot dx` / `slot y += 4 * slot dy` per tick and drives the
  animation clock; the four-edge cull frees the slot off-field. The per-tick constants are the per-frame
  reference values times two (one build tick is two arcade frames): the shot delay and hover window
  counted by the two-frame step, velocities scaled by the same `×4` position step every family uses.
  Gameplay math is exact integer arithmetic in arcade units.
- Scratch evidence: `install_init_torkan`, `install_update_torkan` (reusing `_fire_aimed_bullet` and
  `COMPUTE_AIM_PROCCODE`, and the `_draw_spawn_column(exclude_craft=False)` branch), the Torkan branch
  in `install_advance_slots`, the per-type init dispatch in `install_spawn_flying`, `torkan_blocks` for
  the phase-gated render, and the Torkan tuning constants in `tools/game_director.py`; the structural
  contract `_air02_failures` and its per-clause negatives (`test_torkan_slice_authoring_present` /
  `test_torkan_slice_negative_fixtures`) in `tests/test_scratch_project.py`, whose clauses pin the
  lifecycle procs, the by-type spawn/dispatch, the 32-tier toward aim, the 50-point award, that the shot
  is fired exactly once and **without** the fire gate, that the re-aim reads the **48-tier** and applies
  the half-turn (`+16`), that hover zeroes both velocity axes, and — structurally — that every
  phase-leaving flag write stays nested inside its phase gate (the "once-only" guard: the settling
  harness cannot observe a single re-fired or re-aimed frame, so it is guarded structurally); the live
  scenarios in `harness/lib/catalog.js` (`torkan-approaches-and-fires` asserting a bullet is allocated
  directly at the shot-delay expiry, `torkan-reaims-and-flees` asserting the settled velocity reverses
  **away** from the craft on the faster tier, and the extended `debug-key-cycles-families`), each with a
  biting negative.
- Acceptance criteria: A Torkan spawns by type from the formation wave from a plain random column,
  approaches aimed at 2 px/frame, fires **one** aimed bullet directly at its shot-delay expiry (no
  repeat, no gate), hovers and animates for the window, then re-aims once and flees **away** from the
  craft on the faster 3 px/frame tier (harness `torkan-approaches-and-fires`, `torkan-reaims-and-flees`,
  `debug-key-cycles-families`, each with a biting negative); the family's aim/fire/re-aim wiring, the
  fires-once/no-gate shape, the half-turn, and the hover hold all hold (`_air02_failures`, each clause
  corrupted bites); the operator playtest confirms the felt behavior — the aimed approach, a single
  shot, a brief hover, then a break **away** from the craft (not a homing re-aim, and not the slow
  approach speed) at the faster tier.
- Fidelity status: Verified line-by-line against the pinned reference this slice (the cited handler,
  shot routine, re-aim routine, both aim tables, the toward-aim rounding, and the spawn draw were read
  at the pin). The behavior matches the reference within the recorded deviations below; the spec's
  Torkan paragraph was settled to the source in the same slice, correcting a prose error (see deviation
  2).
- License status: The reference states no reusable license; only instruction-derived behavior and
  numeric constants are transferred (recorded in [the index](../spec/index.md) and the data files). No
  source text is reproduced.
- Known deviations or uncertainty: (1) **Hover holds position (no enemy scroll).** The arcade hover
  rides the background scroll on the X axis (`scroll_sprite_X`) while holding its own vector; the port
  has no enemy-scroll term (flying slots move purely by `slot dx`/`slot dy`), so the faithful mapping is
  to hold position — zero both velocity axes — for the animate window. This is the same "no enemy
  scroll" deviation class already recorded for the Toroid, Terrazi, and Kapi. (2) **Corrected the
  re-aim from a repeating cycle to a one-time flip (spec error).** The settled prose read "on a
  ~64-frame cycle recomputes the angle … flips it 180°, and retreats," implying a repeating re-aim. The
  source re-aims **once** (`torkan_update_dir` runs a single time when the hover clock reaches its
  boundary, then the object is in a plain flee-and-move re-entry forever) and, crucially, adds a
  distinct hover/animate phase the prose omitted entirely. The build follows the source; the spec
  paragraph was corrected under `guardrail-ack`. (3) **Fires once, directly, with no mask — faithful,
  not a simplification.** Unlike the Terrazi and Kapi, which fire periodically through the shared
  fire-permission gate under a per-area mask, the Torkan issues a single `init_new_bullet` directly at
  its shot-delay expiry and never fires again; the port reproduces this by calling `_fire_aimed_bullet`
  once inside the expiry transition and installs **no** fire mask and **no** gate call for the family.
  A mask entry or a gated fire would be the error here. (4) **Retreat omits the toward-aim rounding.**
  The toward aim applies `addq #4,d2` ("black magic" rounding) before its `lsr #3`; the retreat
  (`torkan_update_dir`) flips with `add.b #0x80` and does **not** add the `#4`. The port reproduces the
  asymmetry: the re-aim reuses the un-rounded folded aim base and applies only the half-turn. (5) **Six
  extracted frames cover the seven arcade sprite codes (art availability).** The arcade animates seven
  codes `0x10`–`0x16` (one per four frames across the 28-frame hover). The Aerial Enemies rip supplies
  only **six** distinct Torkan frames; rather than fabricate a seventh, the render maps the seven code
  steps onto the six frames by **holding the last** frame on the seventh step — the same hold-last idiom
  the Kapi uses for its eighth animation phase. The hover window length (28 frames) and the re-aim
  boundary are timer-driven, not art-driven, so the timing stays faithful; only the final rendered frame
  repeats. The six frames are extracted from the same Aerial Enemies sheet as the other aerial families,
  onto the shared sprite-extraction proof, on a fixed 16×16 cell (Xevious hardware sprites are 16×16);
  the render clone mirrors them by family prefix and plays the shared explosion on a hit. (6) **Two
  arcade frames per tick.** All per-frame reference rates are doubled for the port's two-frame tick (the
  shot delay and hover window counted by the two-frame step, velocities scaled by the shared position
  step), the same tick scaling every family uses. (7) **Appears in area-1 waves at standard difficulty
  (difficulty-dependent); the debug key and seeded scenarios prove it in isolation.** Type `0x0F` is
  reached through the natural AI-level formation schedule, not only the debug key. Area 1 runs ten
  AI-raise records, each advancing `enemy_AI_level` — cleared to 0 at new-game start (`coined_up` 418) —
  by the difficulty increment and indexing the flying-formation table (`src/xevious_sub.68k`
  `sub_2_fn_3__inc_enemy_AI_and_flying_enemies` 317–330, reading `src/xevious_sub.68k` `difficulty_tbl`
  338–342 = 2/0/6/16). At the increment-2 setting the second raise reaches AI level 4, whose formation
  entry (`formation_table.entries` in `docs/spec/data/formations.json`) draws from type-table offset
  92 = 15/15/8/8/8/8 — two Torkans, early in area 1; other standard settings reach Torkan formations at
  other AI levels (e.g. increment 6 → AI level 24 → offset 88, ending in two Torkans). Only the
  increment-0 setting keeps `enemy_AI_level` at 0 and area 1 on entry 0, with no Torkan. So a built
  Torkan appears in natural area-1 waves at standard difficulty — it is wired into the ordinary spawn
  dispatch (`FLYING_HANDLED_TYPES`, the `spawn torkan` branch), not only the debug key. The all-Torkan
  run at type-table offset 25 that the debug key forces (`TORKAN_FORMATION_OFFSET`) is a separate,
  denser formation the natural area-1 schedule does not select; the seeded harness scenarios and the
  debug key are used to prove the behavior in **isolation** (a clean single- or six-Torkan wave), not
  because the family is unreachable in normal play.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
