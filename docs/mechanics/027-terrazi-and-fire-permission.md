# Terrazi and the shared fire-permission gate (the first periodically-firing aerial family)

- Mechanic: The Terrazi family (AIR-06.terrazi) — the first aerial enemy that fires on a timer — and
  the reusable, family-agnostic **fire-permission gate** every future firing family will share. A
  Terrazi spawns from the formation wave, aims at the craft on the 48-magnitude (3 px/frame) homing
  tier and approaches while firing aimed bullets under its fire-permission mask; when it draws nearly
  level with the craft along the scroll axis it stops firing and **glides** — decelerating and reversing
  its lateral course, peeling the craft's approach line away — then leaves and is culled. It shares the
  blaster hit window and the hit explosion with the Toroid. This is the first consumer of the
  per-family fire-permission masks laid down in [record 022](022-fire-permission-masks.md), and the
  first author of the two per-slot fire fields added to the pool of
  [record 023](023-aiming-and-slot-positions.md).
- Derived behavior: A Terrazi draws a random lateral spawn column (the same bounded reject-and-redraw
  as the Toroid) and enters from the top row, aims at the craft, and approaches at 3 px/frame. Its
  periodic fire is gated to a **global** 8-frame phase shared by every firing object: on that phase it
  decrements a per-object countdown and, at zero, fires one aimed bullet and reloads the countdown to
  `(random & mask) + 1`. The **mask caps the interval** — it is not an on/off switch: mask 0 reloads to
  1 (fire on every 8-frame phase, the fastest), a larger mask draws a longer random reload. The mask is
  captured from the family's fire-permission byte at spawn, and the countdown is seeded to `random &
  mask` at spawn — **without** the reload's `+ 1` (the spawn/reload asymmetry). While it is more than a
  few rows from the craft along the scroll axis it fires this way; when the scroll-axis offset falls in
  the window `[-4, 3]` it commits — once — to a glide: it sets a slow ±2 scroll drift toward or away by
  side, pins its fire countdown to 255 so it stops firing, and each frame decelerates its lateral
  velocity by a fixed step, so that velocity crosses zero and reverses — the enemy peels away from the
  side it was closing on over roughly 24 frames (a derived span; the reference has no literal count).
  It is culled when it leaves the play field on any edge, and it dies to a blaster shot or kills the
  craft on contact through the shared windows. The catalog's older "expand attack" description has no
  support in the reference and is ruled out.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. The Terrazi handler
  is `handle_11_Terrazi` (`src/xevious_main.68k` 3667–3692, the aimed-approach and fire-while-distant
  path) and `terrazi_main_cont` (3693–3718, the glide with the lateral decelerate-and-reverse); its
  aim uses `angle_dX_dY_terrazi_torkan_tbl` (6325). The fire-permission gate is
  `chk_timer_fire_bullet_reinit_timer` (4999–5010) and its bullet allocation `init_new_bullet`
  (5012–5020). The spawn column is `gen_rnd_spriteY` (5156–5169) driven by
  `main_fn_4__spawn_flying_enemies` (5171–5186); the shared blaster hit window is
  `check_shot_hit_flying_enemy` (2565–2577). The behavior is a port mapping of that logic, not copied
  text; the shared-rules and Terrazi paragraphs of [aerial enemies](../spec/aerial-enemies.md) are the
  settled description this slice implements.
- Transfer class: Behavioral port (instruction-derived control flow and numeric constants; no source
  text, ROM, or media copied). The homing tables, slot layout, and fire-permission masks are the
  derived data of records [022](022-fire-permission-masks.md) and [023](023-aiming-and-slot-positions.md).
- Scratch interpretation: The ordered walk `advance slots` (SYS-04) dispatches a Terrazi-typed slot
  (0x11) to the warp proc `update terrazi`; `spawn flying enemies` inits it by type through `init
  terrazi` (the same bounded spawn-column draw as the Toroid, aimed on the `aim dx 48`/`aim dy 48`
  tier). Two shared per-slot fields carry the fire state — `slot fire mask` (the captured mask) and
  `slot fire timer` (the countdown) — both zeroed by `clear slots` so they are inert for non-firing
  occupants. The standalone, family-agnostic warp proc `fire permission gate` reproduces the reference
  exactly: it proceeds only on the global phase (`tick mod 4 == 0`, since one build tick is two arcade
  frames, so every 4th tick is the reference's every-8th frame); it byte-decrements the countdown
  (`mod 256`, so a spawn draw of 0 wraps to 255 and counts down — the reference's underflow); at zero
  it fires the shared aim/alloc body `_fire_aimed_bullet` (an aimed generic 2 px/frame bullet) and
  reloads the countdown to `(rng mod (mask+1)) + 1`. `rng mod (mask+1)` reproduces the reference's
  `random & mask` because every flying family's scheduled mask is a contiguous low-bit byte. `update
  terrazi` tests the scroll-axis window each active tick, commits the glide (setting the drift, the
  glide flag, and the 255 fire-suppression), decelerates and reverses the lateral velocity while
  gliding, calls the gate, moves by `4·velocity` per tick, and culls. Gameplay math is exact integer
  arithmetic in arcade units.
- Scratch evidence: `install_init_terrazi`, `install_update_terrazi`, `install_fire_permission_gate`,
  `_fire_aimed_bullet` (the shared aim/alloc, reused by the Toroid's one-shot and by the gate), the
  Terrazi branch in `install_advance_slots`, the per-type init dispatch in `install_spawn_flying`, and
  the two `SLOT_FIELD_LISTS` fire fields in `tools/game_director.py`; the structural contract
  `_air06_failures` and its per-clause negatives (`test_terrazi_slice_authoring_present` /
  `test_terrazi_slice_negative_fixtures`) in `tests/test_scratch_project.py`, whose ten clauses pin the
  lifecycle procs, the by-type spawn/dispatch, the fast-tier aim, the glide-and-reverse, and — statically,
  since the harness cannot see per-frame cadence — the gate's global-phase modulus and the reload `+ 1`
  (proving there is no zero-suppression); the live scenarios `terrazi-wave-spawns-and-moves`,
  `terrazi-glides-and-reverses`, and `terrazi-fires-under-mask` (isolated from live shooting Toroids),
  each with a biting negative, in `harness/lib/catalog.js`.
- Acceptance criteria: A Terrazi spawns by type from the formation wave and advances under its own
  aimed velocity, commits its glide-and-reverse in the scroll-axis window, and fires aimed bullets
  through the shared gate under its captured mask (harness `terrazi-wave-spawns-and-moves`,
  `terrazi-glides-and-reverses`, `terrazi-fires-under-mask`, each with a biting negative); the gate
  phases on the global 8-frame boundary and reloads `(random & mask) + 1` with no zero-suppression, and
  the family's aim/glide/capture wiring holds (`_air06_failures`, each clause corrupted bites); the
  operator playtest confirms the felt behavior — the aimed approach, the fire-while-distant then
  glide-and-reverse-while-near, and that the firing rate tracks the scheduled mask.
- Fidelity status: Verified line-by-line against the pinned reference this slice (the cited handler,
  gate, aim table, and shared windows were read at the pin). The behavior matches the reference within
  the recorded deviations below; the spec's Terrazi paragraph and fire-permission-gate description were
  settled to the source in the same slice.
- License status: The reference states no reusable license; only instruction-derived behavior and
  numeric constants are transferred (recorded in [the index](../spec/index.md) and the data files). No
  source text is reproduced.
- Known deviations or uncertainty: (1) **Fire mask as a modulo cap.** Scratch has no bitwise AND, so
  `random & mask` is reproduced as `random mod (mask+1)` — exact for a contiguous low-bit mask
  (`2^n − 1`). Every flying family's scheduled fire mask is contiguous (Terrazi 3/7, Zoshi 15/31, Kapi
  3/7); the only non-contiguous byte in the schedule is the boss `andor_genesis` mask 47, which is
  flagged to need a true bitwise AND when that leaf builds — this gate is correct for every family that
  reuses it today. (2) **Spawn/reload `+ 1` asymmetry.** The spawn seeds the countdown to `random &
  mask` (range `[0, mask]`) with no `+ 1`, while each reload adds `+ 1` (range `[1, mask+1]`); a spawn
  draw of 0 byte-underflows on the first decrement to 255 and counts down (reproduced by the `mod 256`
  wrap), so that Terrazi effectively does not fire in its short first life — the reference's exact
  behavior, not a port choice. (3) **~24-frame glide reversal is derived.** The lateral decel rate is
  exact (the reference's per-frame `−2`, doubled to `−4` per two-frame tick); the "~24 frames" total to
  cross zero and reverse depends on the aimed lateral magnitude at the trigger and is a derived
  estimate, with no literal source constant. (4) **Glide window in port cells.** The reference's
  scroll-axis window test is a byte compare (true on offset `[-4, 3]`); like the Toroid's lateral
  window, the port reads it as a cell offset — a modeling choice consistent across the two families.
  (5) **Toroid one-shot is an exception to the gate.** The shooting Toroid (0x0B) fires a single
  event-driven bullet at its swing commit — a distinct, non-periodic firing model that does NOT run
  through this gate; it shares only the aim/alloc body. (6) **Render deferred.** A live Terrazi slot is
  logically complete but not yet drawn — the render clone gates on the Toroid type, so a Terrazi slot
  is cleanly hidden until its sprite is staged (the operator's logic-first sequencing); the
  sprite-code flap animation (`_ddX`) is deferred with that render target. (7) **Not in area 1's
  baseline waves.** Type 0x11 appears only at higher-AI-level formations, so the family is proven
  through seeded harness scenarios rather than area-1 live density.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
