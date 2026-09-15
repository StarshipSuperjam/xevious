# Ground-bombing pipeline — GND-01, GND-03, and the ground dispatch/awards/targeting systems

- Mechanic: the first working ground game — the bombing half of Xevious that had never existed in the
  port. This one record covers the five leaves that ship together as one pipeline: the two ground
  *objects* that gave the slice something to bomb — `ground.barra` (#70) and `ground.logram` (#71) — and
  the three *systems* each `blocked_by` them: `area.ground-dispatch` (#69, spawning ground objects onto the
  scrolling terrain), `economy.ground-awards` (#68, scoring a bombed ground object through the shared
  award path), and `player.ground-targeting` (#67, the faithful bomb reticle with its 96-px craft-offset
  lead). Like the air pipeline before it ([024](024-toroid-vertical-slice.md), [025](025-blaster-to-air-hit.md),
  [026](026-enemy-bullets-and-collision-death.md)), an object cannot honestly be proven playable without
  the pipeline that spawns, targets, hits, and scores it, so the five ship and are recorded together.

- Derived behavior: bombing is now a complete loop — a ground object is dispatched onto the terrain,
  scrolls with it, is aimed at by a lead reticle, is destroyed by a bomb (never the blaster), and scores.
  - **Ground dispatch and the terrain-locked scroll (AREA-02).** A scheduled `add_ground_object` record
    stamps a ground slot at the top of the field with its object type, per-area fire mask, and sprite row,
    then — unlike a flying enemy, which holds its own path — the object moves **only** with the map: each
    tick the ground branch of the slot walk advances `slot x` by one `AREA_PROGRESS_STEP` (32 units),
    carrying the object *down* the field toward the craft, and culls it once its row passes the off-field
    limit. This is the port's first terrain-locked scroller; every ground object and every crater rides it.
  - **The bomb reticle leads the craft by 96 px (WPN-03/WPN-04/WPN-05).** The crosshair (slot 35) tracks
    the craft every tick with a fixed forward lead of −3072 `slot x` units (= −96 px = −12 cells), the
    port of `init_bombing`'s `_X + 0xF400`. On a bomb press the bomb-target (slot 33) **locks** at the
    crosshair's position and thereafter scrolls with the terrain like any ground object, while the bomb
    (slot 34) accelerates toward it; when the bomb's row reaches the target's (`bomb x ≥ target x`) the
    bomb finishes and the ground-hit sweep runs. The reticle is a pure renderer of slots 33/34/35 — it is
    not arrow-key movable (the interim manual-movement block was removed).
  - **Ground awards through the one shared path (ECO-01).** A dedicated ground-hit sweep compares each
    ACTIVE ground slot to the bomb-target slot within the ground overlap window (Y ± 10, X ± 5, the port
    of `check_object_on_target`); on overlap it reads the object's point value from the value table by
    `slot pts` and calls the same `resolve hit` → `score` path every kill in the game already uses. The
    blaster's air-hit sweep is left untouched — the blaster structurally cannot select a ground slot, so a
    shot on a ground cell never scores.
  - **Barra and Garu Barra (GND-01).** Barra is the passive target: terrain-fixed, never fires, and on a
    bomb hit it plays the shared explosion and **converts to a persistent crater** that keeps scrolling in
    its slot until culled — it is never removed on the clock. Garu Barra is a pair: a double-size base born
    permanently **indestructible** (stamped a non-ACTIVE sentinel the award sweep's `== ACTIVE` gate
    rejects for free, and drawn off `slot type` rather than `slot state`) plus a destructible scoring node
    offset 8 px to the side that, when bombed, **vanishes cleanly** (explode-and-remove, no crater). Barra
    scores 100; the Garu node scores 300; the base never scores.
  - **Logram (GND-03).** The opening dome: after a masked random wait it runs a 28-tick open-and-close
    cycle through its seven-stage sprite sequence `{0x2C,0x2D,0x2E,0x2F,0x2E,0x2D,0x2C}` on the every-8th-
    arcade-frame cadence (scaled so a stage advances when `tick mod 4 == 0`), **fires exactly one aimed
    bullet at the fully-open midpoint** (tick 12, stage 0x2F) straight through the aimed-bullet allocator,
    then rolls a fresh wait at stage 7. It only arms once it has scrolled past the area's stop-firing row,
    and a bomb hit at any stage craters persistently like a Barra. Logram scores 300.

- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`, `src/xevious_main.68k`. Bomb reticle and flight: the 96-px forward lead is `init_bombing` (2445–2501) (`_X + 0xF400`) and the bomb finishes when its row reaches the target in `check_bomb_finished` (2502–2596). Ground-hit selection and award: the overlap window is `check_object_on_target` (2629–2643) and point crediting for a bombed active object is `handle_bombed_obj_and_award_points` (2597–2623). Barra and Garu: `handle_1E_Barra` (2644–2654) (never fires, crater on death) and `handle_20_Garu_Barra` (2657–2682) (indestructible base plus an 8-px lateral destructible node). Logram: `handle_logram_init` (2722–2732), `handle_logram_main` (2733–2759), and `start_logram_shot_timer` (2765–2772) (masked wait, seven-stage cycle, one shot at full open, re-roll, stop-firing-row arming). Destruction outcomes: land objects leave a persistent scrolling crater in `handle_bomb_explosion` (4904–4941) while scoring nodes and water objects are removed by `explode_and_remove_object` (4963–4997); the terrain scroll and off-field cull the crater rides are `scroll_sprite_X` (4849–4854) and `check_scroll_offscreen` (4827–4834). Placements, fire masks, stop-firing rows, and sprite rows per area are the committed [schedule data](../spec/data/area-schedules.json); the settled behaviour is [ground objects](../spec/ground-objects.md); point values are [scoring, lives, and game over](../spec/scoring-lives-and-game-over.md).

- Transfer class: General behavior and numeric constants (instruction-derived control flow — the reticle
  lead offset, the terrain-scroll step, the overlap window, the Logram open/close cadence and single-shot
  timing — over the committed, hash-pinned schedule and scoring tables; no source text or media copied).

- Scratch interpretation: the ground pipeline is built entirely in the `.sb3` generator
  `tools/game_director.py`, mirroring the air pipeline's shape — stage warp threads own the logic, sprite
  targets are pure renderers reading the parallel 64-entry slot lists, and hit resolution is the single
  type-agnostic `resolve hit` → `score` path. New this slice: the schedule ingest fills `object_type` /
  `slot` / `sprite_y` columns for every ground record; `add_ground_object` (wired into `_consume_schedule`)
  stamps a ground slot at the top-of-field row with its type, mask, and value; the ground branch of
  `install_advance_slots` increments `slot x` by `AREA_PROGRESS_STEP` each tick and culls off-field;
  `install_check_ground_hit` sweeps the ground slots against the bomb-target slot and awards through the
  shared path; the walk-thread crosshair/bomb-target/bomb procs port the reticle lead, the target lock, and
  the bomb flight; and the Barra, Garu, and Logram renderers/handlers implement the per-object behaviour.
  The crater is the object's own slot left in a bombed state that keeps scrolling; the Garu base is a
  non-ACTIVE sentinel the award gate ignores. Render layering (port render decision, not a slot-list or
  behaviour change): the ground renderers deliberately do not `go to front` — a ground object rests on the
  terrain, so its static layer order (already above the never-fronting terrain strips) leaves it below the
  craft and the bomb sight, both of which front themselves every tick. This keeps the crosshair/bomb-target
  the player aims with, and the craft that flies over the terrain, visible above the ground objects.

- Scratch evidence: harness scenarios with biting negatives, structural guards each paired with a severing
  negative, and fresh `.play` evidence markers.
  - Harness (`harness/lib/catalog.js`, negatives in `harness/lib/mutate.js`): the ground-dispatch/scroll,
    ground-award, bomb-reticle-lead, Barra crater/blaster-immunity, Garu base/node, and Logram
    open-fire-close scenarios, each proven against the real build and a mutated build that fails the same
    assertion.
  - Structural (`tests/test_scratch_project.py`): `_gnd_dispatch_failures`, `_gnd_awards_failures`,
    `_gnd01_failures`, and `_gnd03_failures` — present-and-negative guards pinning the warp wiring
    (spawn stamps top-of-field, the terrain scroll and cull, the shared award path, the persistent crater,
    the indestructible base, the destructible node, the single full-open Logram shot, its arming and
    cadence gates, and its stage-7 recycle), with `test_*_authoring_present` / `test_*_negative_fixtures`
    for each.
  - Records / roadmap: fresh `roadmap-evidence: <OBLIG> success|failure` markers for every obligation of
    all five leaves (WPN-03/04/05, ECO-01, AREA-02, GND-01, GND-03), plus the deterministic-build gates
    `test_two_clean_processes_build_identical_bytes` and `test_game_director_generator_is_current`.
  - Operator playtest: the on-screen feel of the bombing loop — the reticle sitting ahead of the craft, a
    bomb landing on a Barra and leaving a crater that scrolls away, the blaster refusing to destroy it, the
    Garu base surviving while its node vanishes, and a Logram's open-fire-close rhythm.

- Acceptance criteria: Engine — the harness scenarios pass with their biting negatives; the four structural
  guards hold and every negative bites; the build is byte-identical across two clean processes and the
  generator is current; every ground citation resolves at the pin. Operator — bombing a Barra craters,
  scrolls the crater away, and scores 100; the blaster never destroys a Barra; a Garu base is indestructible
  while its node vanishes for 300; a Logram opens, fires one shot at full open, and closes; and the reticle
  leads the craft by ~96 px so a bomb lands ahead of it.

- Fidelity status: GND-01 (Barra / Garu Barra), GND-03 (Logram), AREA-02 (ground dispatch), ECO-01 (ground
  awards), and WPN-03/WPN-04/WPN-05 (bomb targeting, flight, and ground hit) are **built** — the pipeline is
  live and proven end-to-end in the harness and structural guards, with the on-screen feel confirmed by the
  operator playtest. The remaining ground families (`ground.barra-variants`, and the Zolbak / Derota /
  Boza Logram / Grobda / Domogram families of slices 12–13) stay `provisional` under the now-locked ground
  spec and are not built here.

- License status: The pinned reference states no reusable license; only instruction-derived behaviour and
  the committed, hash-pinned tables (the [schedule data](../spec/data/area-schedules.json) and the scoring
  values) are used, cited to the settled spec and the data files, and no reference source text or media was
  reproduced. Ground sprite art is credited in `src/xevious/assets/provenance.json`
  (https://www.spriters-resource.com/arcade/xevious/).

- Known deviations or uncertainty: two direction/sign facts were verified against the source and pin down
  the build against prose that had them backwards.
  - **The terrain scroll increases `slot x` (moves the object down toward the craft), and the bomb finishes
    when `target x ≤ bomb x`.** The plan prose described both signs the other way; the source
    (`scroll_sprite_X`, `check_bomb_finished`) and the built behaviour agree on the direction recorded here
    — the source wins, and the build was not bent to the prose.
  - **The Garu node's 8-px offset is lateral, and its row is set absolutely.** The source sets the node
    `_Y = base_Y − 0x100` (an 8-px offset on the lateral axis) and its `_X` (row) to an absolute value, not
    a base-relative row offset; the locked spec was corrected to say "8-px lateral" at settle time
    ([ground objects](../spec/ground-objects.md)), and the build follows the source. The on-screen
    direction of the node is a point for the operator playtest to confirm.
  - **Logram cadence is scaled from arcade frames to ticks.** The arcade animates on every 8th arcade
    frame; the once-per-tick walk is two arcade frames, so a stage advances every 4th tick, landing the
    single full-open shot at tick 12 of the 28-tick cycle. The mechanism (one aimed shot at full open, one
    per cycle) is proven; the exact felt cadence is an operator-playtest observation.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
