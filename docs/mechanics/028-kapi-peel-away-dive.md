# Kapi and the peel-away dive (the first timer-triggered diving aerial family)

- Mechanic: The Kapi family (AIR-05.kapi) — the first aerial enemy that approaches **silently** and then
  commits to a **peel-away dive**. A Kapi spawns from the formation wave, aims at the craft on the
  32-magnitude (2 px/frame) tier, and glides in **without firing** while a spawn-set countdown runs down;
  when the countdown expires it latches a dive **once** — a fixed lateral acceleration that curves it
  **away** from the craft's column while it decelerates its forward run — and from that instant it
  **fires every tick** through the shared fire-permission gate with no suppression. It shares the blaster
  hit window and the hit explosion with the Toroid, and it reuses the family-agnostic fire-permission gate
  and the two per-slot fire fields laid down for the Terrazi in
  [record 027](027-terrazi-and-fire-permission.md) (masks from [record 022](022-fire-permission-masks.md),
  slot fields from [record 023](023-aiming-and-slot-positions.md)).
- Derived behavior: A Kapi draws a **plain random lateral spawn column** — unlike the Toroid and Terrazi,
  it uses the *non-excluding* draw with **no** craft-proximity reject-and-redraw — and enters from the top
  row, aims at the craft at 2 px/frame, and seeds an approach countdown to a random `48–111` frames. While
  that countdown runs it moves under its aim and **does not fire** (the fire gate is never called). When the
  countdown reaches zero it commits — once — to the dive: it latches a lateral acceleration sign `ddY` from
  the craft's side at that instant (`ddY = +1` if the craft is at a smaller lateral coordinate than the
  Kapi, else `−1` — the sign of `self.lateral − craft.lateral`), and thereafter each frame it
  **accelerates its lateral velocity `_dY` by `ddY` (±1/frame)** so the lateral velocity carries it
  **away** from the craft's column, and **decelerates its forward/scroll velocity `_dX` by 2/frame** so its
  advance slows. The side is latched **once** and never recomputed, so the curve does not re-home after the
  lateral velocity crosses zero — the same latched-swing shape the Toroid uses, on the same lateral axis.
  From the dive commit it fires one aimed bullet through the shared gate on every firing phase with **no**
  fire-suppression (it does not set its countdown to 255 the way the Terrazi does on its glide). It is
  culled when it leaves the play field on any edge, and it dies to a blaster shot or kills the craft on
  contact through the shared windows.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. Line citations are
  `src/xevious_main.68k` unless noted. The Kapi handler is `handle_10_Kapi` (3602–3621, the silent aimed
  approach and the countdown) and `kapi_10_fire` (3624–3663, the once-latched `ddY` from the craft's side,
  the per-frame `add ddY,_dY` lateral accelerate and `subq #2,_dX` forward/scroll decelerate, and the
  fire-every-frame path with its sprite animation and its `loc_2455` hold branch); its aim uses the generic
  `angle_dX_dY_tbl` (6360) through `calc_dX_dY_for_vector_to_solvalou` (5119–5146). The spawn column is the
  non-excluding `gen_random_Y_store_obj` (5147–5154). The fire-permission gate is
  `chk_timer_fire_bullet_reinit_timer` (4999–5010). The per-area fire mask is `ffreq_mask_kapi`
  (7 in area 1, 3 later — both contiguous, so `random & mask` is exact as `random mod (mask+1)`), read live
  from the area schedule the same way every family reads its mask (record 022). The behavior is a port
  mapping of that logic, not copied text; the shared-rules and Kapi paragraphs of
  [aerial enemies](../spec/aerial-enemies.md) are the settled description this slice implements.
- Transfer class: Behavioral port (instruction-derived control flow and numeric constants; no source text,
  ROM, or media copied). The aim table, slot layout, and fire-permission masks are the derived data of
  records [022](022-fire-permission-masks.md) and [023](023-aiming-and-slot-positions.md).
- Scratch interpretation: The ordered walk `advance slots` (SYS-04) dispatches a Kapi-typed slot (0x10) to
  the warp proc `update kapi`; `spawn flying enemies` inits it by type through `init kapi`. `init kapi`
  draws the spawn column through the shared helper **with the craft-gap exclusion switched off** (the
  reference's plain random draw), aims on the `aim dx 32`/`aim dy 32` tier, sets `slot pts` to the 300-point
  value-table slot and `slot code` to 0x20, captures the family mask into `slot fire mask`, and seeds `slot
  fire timer` to `(rng mod 64) + 48` (the `48–111` approach delay). `update kapi` decrements that approach
  countdown each active tick by the two-frame tick step and, at or below zero, commits the dive **once**: it
  latches the side into `slot flag` by testing the lateral offset `(craft column − self column)` — `>= 0`
  selects the minus branch (`slot dy −= 2` per tick), `< 0` the plus branch (`slot dy += 2` per tick), the
  exact port of `ddY = sign(self.lateral − craft.lateral)` — arms `slot fire timer` to 1 so the gate fires
  at once, and clears the approach state. While diving it also drives `slot dx −= 4` per tick (the `2/frame`
  forward/scroll deceleration, doubled for the two-frame tick) and calls the standalone `fire permission
  gate` every tick — the fire gate is gated only on "not still approaching", never suppressed — so the Kapi
  keeps firing for the rest of its life. The per-tick constants are the per-frame reference values times two
  (one build tick is two arcade frames): lateral `±1/frame → ±2/tick`, scroll `−2/frame → −4/tick`. Gameplay
  math is exact integer arithmetic in arcade units.
- Scratch evidence: `install_init_kapi`, `install_update_kapi` (reusing `install_fire_permission_gate` and
  `_fire_aimed_bullet`), the `_draw_spawn_column(exclude_craft=False)` branch that expresses the
  non-excluding draw, the Kapi branch in `install_advance_slots`, the per-type init dispatch in
  `install_spawn_flying`, and the Kapi tuning constants in `tools/game_director.py`; the structural contract
  `_air05_failures` and its per-clause negatives (`test_kapi_slice_authoring_present` /
  `test_kapi_slice_negative_fixtures`) in `tests/test_scratch_project.py`, whose clauses pin the lifecycle
  procs, the by-type spawn/dispatch, the 32-tier aim, the dive's lateral-accelerate (`slot dy` carries the
  `±2` step) and scroll-decelerate (`slot dx` carries the `−4` step) — a biting pair that fails if the axes
  are swapped — the once-latched side and — structurally — that the side latch stays nested inside the
  `slot flag == approach` gate (the "latched once, never recomputed" guard: a re-homing latch moved outside
  that gate bites, since the settling harness cannot observe a single re-latched frame), the fire capture,
  that the gate is driven during the dive, and — statically — that the update body contains **no**
  `slot fire timer = 255` suppression; the live scenarios
  in `harness/lib/catalog.js` (Kapi spawns-and-dives asserting `slot dy` grows away and `slot dx` decreases,
  fires-while-diving, and the debug-key cycle), each with a biting negative.
- Acceptance criteria: A Kapi spawns by type from the formation wave from a plain random column, approaches
  silently at 2 px/frame, latches its peel-away dive at countdown expiry (lateral velocity accelerates
  **away** from the craft's column while forward velocity decelerates), and fires aimed bullets through the
  shared gate every tick from the dive with no suppression (harness `kapi-spawns-and-dives`,
  `kapi-fires-while-diving`, `debug-key-cycles-families`, each with a biting negative); the family's aim/latch/capture
  wiring and the axis directions hold (`_air05_failures`, each clause corrupted bites); the operator playtest
  confirms the felt behavior — the silent aimed approach, then a dive that **peels away** from the craft's
  column (not a homing dive, and not a swing on the forward axis) while the forward run slows, with firing
  beginning at the dive.
- Fidelity status: Verified line-by-line against the pinned reference this slice (the cited handler, fire
  routine, gate, aim table, and spawn draw were read at the pin). The behavior matches the reference within
  the recorded deviations below; the spec's Kapi paragraph was settled to the source in the same slice,
  correcting a prose error (see deviation 1).
- License status: The reference states no reusable license; only instruction-derived behavior and numeric
  constants are transferred (recorded in [the index](../spec/index.md) and the data files). No source text
  is reproduced.
- Known deviations or uncertainty: (1) **Corrected axis and direction of the dive (spec error).** The
  settled prose called the dive "vertical acceleration / horizontal deceleration" and read as a homing
  approach. The source accelerates the **lateral** axis (`_dY`, this codebase's `_dY`=lateral,
  `_dX`=scroll convention, records 023/027) and decelerates the **forward/scroll** axis (`_dX`), and the
  latched `ddY = sign(self.lateral − craft.lateral)` curves the Kapi **away** from the craft's column, not
  toward it — a Toroid-style latched peel-away, not a homing dive. The build follows the registers; the
  spec paragraph was corrected under `guardrail-ack`. (2) **Spawn has no craft-proximity exclusion.** Kapi
  draws its column with `gen_random_Y_store_obj` (a plain random lateral coordinate), where the Toroid and
  Terrazi use the *excluding* `gen_rnd_spriteY`; the port expresses this by calling the shared spawn-column
  helper with the exclusion switched off, so a Kapi can legitimately appear in the craft's column.
  (3) **Approach delay `48–111` follows the comment over the literal.** The source computes the countdown as
  `pseudo_random_gen` then two `add.b` of `#63` and `#48`, but the inline comment reads `48-111`; a plain
  add would give `random + 111`, which cannot produce that range, whereas masking to `random & 63` then
  `+ 48` does. The locked spec decided (carried forward, no new decision) to follow the commented `48–111`
  range as the probable intent of a transcription slip; the port seeds `(rng mod 64) + 48`. (4) **Fire mask
  as a modulo cap.** Scratch has no bitwise AND, so `random & mask` is reproduced as `random mod (mask+1)` —
  exact for Kapi's contiguous masks (7 and 3), the same shared-gate model and limitation recorded for the
  Terrazi (record 027, deviation 1). (5) **Dive animation is render-derived.** The reference advances the
  Kapi sprite code `0x20..0x26` from a per-object counter (`_TIMER1 >> 3`, seven frames, holding the last on
  the eighth phase); the port does not write `slot code` in the walk but derives the dive frame render-only
  from the slot's animation clock (`slot timer`), cycling the seven frames in forward order every ~8 arcade
  frames and holding the last — the same visible cadence with no build-logic change. The seven frames are
  extracted from the same Aerial Enemies sheet as the Toroid and Terrazi, onto the shared sprite-extraction
  proof, on a fixed 16×16 cell (Xevious hardware sprites are 16×16); the render clone mirrors them and plays
  the shared explosion on a hit. (6) **Two arcade frames per tick.** All per-frame reference rates are
  doubled for the port's two-frame tick (lateral `±1/frame → ±2/tick`, scroll `−2/frame → −4/tick`, the
  countdown decremented by the two-frame step), the same tick scaling every family uses. (7) **Not in area
  1's baseline waves.** Type 0x10 appears only at higher-AI-level formations, so the family is proven through
  seeded harness scenarios and the debug spawn key rather than area-1 live density.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
