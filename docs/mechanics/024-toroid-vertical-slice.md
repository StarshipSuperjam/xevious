# Toroid — the first live flying enemy (spawn, aim, move, swing, cull)

- Mechanic: The Toroid vertical slice (AIR-01.toroid) — the first live flying enemy. The formation
  wave spawns it into a flying slot, it aims at the craft on the 24-magnitude homing tier and
  approaches, then when it draws nearly level swings by REVERSING its lateral course (peeling away
  from the side it was closing on, the arcade `toroid_toggle_dir` bounce — not a homing dive), animates its flap,
  and is culled off the play field with its scroll-axis position kept for the refill. This is the
  first live consumer of the shared RNG (SYS-04 becomes a present consumer) and of the slice-7
  formation state, and the first author of the per-slot position/motion fields laid down dormant in
  [record 023](023-aiming-and-slot-positions.md).
- Derived behavior: While a wave is active (`formation count` = N > 0), the spawner refills the first
  N flying slots (0x3A–0x3F) each frame from the wave's type run; a slot re-spawns the moment its
  occupant leaves, so the wave is continuous pressure until a reset-formation record zeroes N. A
  Toroid draws a random lateral spawn column (reject-and-redraw, never within eight columns of the
  craft), inherits the previous occupant's scroll-axis position, aims at the craft, and approaches at
  1.5 px/frame. When the craft's lateral offset falls in [-2, 1] columns it commits — once — to a
  swing that REVERSES its lateral velocity: it nudges the aimed (craft-ward) `slot dy` by one unit per
  frame against the approach (unbounded), so it decelerates, reverses, and peels away from the side it
  was closing on — the reference `toroid_toggle_dir` toggle, not a homing dive — while cycling its eight
  flap codes in opposite order per direction. It is culled when it scrolls past the bottom
  (row ≥ 40), off the top (row ≤ -2), or off the side (col ≥ 31); the cull frees only occupancy, so a
  refilled slot inherits its scroll row (a replacement can enter mid-field, not always from the top).
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. The formation
  spawner is `main_fn_4__spawn_flying_enemies` (`src/xevious_main.68k` 5171–5186); the Toroid init and
  update are `init_toroid` / the object handler around 3289–3321 (flap) and the lateral-swing trigger;
  the random spawn column is `gen_rnd_spriteY` (5155–5169); the refill that keeps position is
  `add_obj_handler` (4801–4815) with `check_scroll_offscreen` (4827–4839). The aim tables and slot
  fields are the ones decoded in [record 023](023-aiming-and-slot-positions.md). The behavior is a
  port mapping of that logic, not copied text; the shared-rules and Toroid paragraphs of
  [aerial enemies](../spec/aerial-enemies.md) are the settled description this slice implements.
- Transfer class: Behavioral port (instruction-derived control flow and numeric constants; no source
  text, ROM, or media copied). The homing tables and slot layout are the derived data of record 023.
- Scratch interpretation: The ordered walk `advance slots` (SYS-04) dispatches each occupied slot by
  type; Toroid types 0x0A (silent) and 0x0B (shoots — its shot lands in a later commit) call the warp
  proc `update toroid`. `spawn flying enemies` runs after the walk each tick (the reference's
  main_fn_2 → main_fn_4 order), refilling empty flying slots from `flying type table` at `formation
  type offset`; `init toroid` draws the spawn column from `rng step` (bounded at 16 attempts), stamps
  the slot, and aims it via `compute aim index` on the `aim dx 24`/`aim dy 24` tier. Gameplay math is
  exact integer arithmetic in arcade units (256 units = one 8-px cell; one build tick = two arcade
  frames, so a tick moves `slot x/y += 4·velocity` and the timer by 2); the port scale is applied
  only at the renderer and the single read of the craft's live cell (`read player cell`). Six
  persistent `toroid` clones, created once on entering play and bound to the six flying slots, render
  the pool each tick — hide on an empty slot, else show at the mapped position with the flap costume.
- Scratch evidence: `install_advance_slots` (the Toroid dispatch), `install_spawn_flying`,
  `install_init_toroid`, `install_update_toroid`, `install_cull_slot`, `install_compute_aim_index`,
  and `install_read_player_cell` in `tools/game_director.py`, plus the `toroid` target and its clone
  renderer; the structural contract `_air01_failures` and its per-clause negatives
  (`test_toroid_slice_authoring_present` / `test_toroid_slice_negative_fixtures`) in
  `tests/test_scratch_project.py`; the spawn-draw model fixture `ToroidSpawnDraw` in
  `tests/test_spec_docs.py`; the live scenarios `toroid-wave-spawns-and-moves`, `rng-draw-order`, and
  `toroid-swing-reverses-away` (which binds the swing's *direction* — a right-swing reverses the lateral
  velocity away from the craft, `slot dy < 0`, the arcade `toroid_toggle_dir` bounce — so the sign cannot
  silently invert into a homing dive), each with a biting negative, in `harness/lib/catalog.js`.
- Acceptance criteria: A live Toroid occupies a flying slot from the formation wave and advances
  under its own velocity each tick (harness `toroid-wave-spawns-and-moves`, negative: `update toroid`
  neutralized → no movement); the spawner draws the shared RNG in walk order, following the LFSR from
  a seeded state (harness `rng-draw-order`, negative: `rng step` neutralized → no draws); the
  lifecycle procedures are warp-atomic, the spawner and walk drive the Toroid, the cull inherits
  scroll position, and the spawn draw is bounded (`_air01_failures`, each clause corrupted bites);
  every accepted spawn column is valid and clears the craft, and the draw terminates within the cap
  with a rare exhaustion tail (`ToroidSpawnDraw` over the committed RNG seeds); two clean builds stay
  byte-identical and survive the build→import round-trip. On-screen feel — the wave's look, the swing
  and flap, the enemy scale/aspect — stays the operator playtest's job.
- Fidelity status: **Live and playable, first flying enemy.** Spawn → aim → approach → swing → flap →
  cull all run live and are exercised headlessly. The kill path (blaster-to-air hit, 30-point award,
  explosion) and the type-0x0B shot and player-collision death land in the next commits of this slice;
  until then a Toroid cannot be destroyed and its shot is not yet fired.
- License status: The pinned reference repository states no reusable license; no reference source
  text or media was copied — the control flow and constants are an instruction-derived port. The
  Toroid costumes are the verified `toroid/turn/*` crops of the Aerial Enemies sheet, credited to
  https://www.spriters-resource.com/arcade/xevious/ in `src/xevious/assets/provenance.json`.
- Known deviations or uncertainty: (1) **Costume by reference.** `game_director` binds the `toroid`
  target's costumes to the seven verified `toroid_sprite_proof` crops by reference rather than
  `sprite_extractor` re-cropping them (the overlap guard blocks a duplicate crop of the same rects);
  the proof target is retained as the crop's provenance home. (2) **Inherited-scroll refill.** The
  cull keeps `slot x`/`slot y`, so a refilled slot re-enters at the previous occupant's scroll row —
  faithful to `check_scroll_offscreen`, but a replacement can appear mid-field rather than from the
  top. (3) **Spawner culls unhandled types.** Area 1's formations name other families (e.g. Torkan)
  too; this slice spawns only the handled Toroid types {0x0A, 0x0B} and skips the rest until their
  slice, so area 1 shows fewer enemies than the arcade until slice 10. (4) **Bounded spawn draw.**
  The reference redraws until it finds a valid column; this port caps the draw at 16 attempts and
  skips the spawn for the tick on exhaustion (retried next tick). Over the committed RNG golden seeds
  the exhaustion rate is ≈1.3% (`ToroidSpawnDraw`), a deterministic, recorded departure from
  unbounded redraw. (5) **Eighth flap phase.** Eight sprite codes map onto seven verified frames with
  a palindromic wrap `[1,2,3,4,5,6,7,6]`; the eighth phase's exact frame is recorded uncertain.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
