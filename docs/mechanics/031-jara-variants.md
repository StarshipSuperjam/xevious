# Jara variants (two independent object types over one shared approach/turn core)

- Mechanic: The Jara family (AIR-04) — two distinct flying object types, the **shooter** `0x55` and the
  **silent** `0x56`, that share **one** init and **one** approach/turn core rather than one type with a
  variant tag; the core branches on type only to fire. Both spawn at a random lateral column excluding the
  craft's, cruise straight on a fast craft-aimed vector with a **static** sprite, and then — the instant the
  craft enters a lateral proximity band — commit **one-way** to a turn that **peels away** from the craft's
  column while **spinning** a 6-frame cycle. The shooter additionally fires **exactly one** aimed bullet at
  the turn instant; the silent type never fires. There is **no pair linkage** in the arcade: no
  leader/follower, no sibling pointer, no shared state, no separation code. The "Jara pair" is a spawn-stream
  convention (wave data emits adjacent runs of the two types) and the split is **emergent** — two Jara spawn
  at different random columns and cross the band on different ticks and opposite sides, so they peel apart on
  their own. It shares the blaster hit window, the hit explosion, the aimed-bullet allocation, and the
  per-slot fields with the earlier aerial families ([record 023](023-aiming-and-slot-positions.md) slot
  fields, [record 026](026-enemy-bullets-and-collision-death.md) bullets), reuses the Torkan one-way `slot
  flag` phase machine ([record 029](029-torkan-attack-and-retreat.md)) and the Zoshi multi-type idiom
  ([record 030](030-zoshi-variants.md)), and peels on the same lateral axis as the Kapi dive
  ([record 028](028-kapi-peel-away-dive.md)).
- Derived behavior: Both types enter aimed at the craft on the **fast 3 px/frame tier** (the Terrazi/Torkan
  angle table) at a random lateral column that excludes ±8 of the craft's column, holding the static frame
  `0xA0` with a pulsing colour and **no spin**, and score **150 pts each, scored independently**. `jara_init`
  captures **no fire mask** — Jara is the only shooter with no `_FFREQ`. Each tick during approach the core
  measures the lateral gap `solvalou._Y − self._Y`; while the craft is outside a **[−6, +5] lateral band** the Jara
  just cruises. On the first tick the craft is inside the band the object commits **one-way** to its turn:
  it ramps its lateral velocity **away** from the craft's column (`±1/frame`, forward/scroll velocity
  untouched), the side chosen by the sign of `solvalou._Y − self._Y` (craft at/right → peel one way, craft
  left → the other), and it now spins the 6-frame cycle `0xA0..0xA5` advancing every two frames — the
  away-from-craft arc using the reversed frame order. The **shooter** fires **one** aimed bullet at that turn
  instant and never again; the **silent** type fires nothing. The bullet is a TYPE-6 that re-vectors onto the
  craft each frame in the arcade (homing); this port aims it once at allocation
  ([record 026](026-enemy-bullets-and-collision-death.md)). The one-way commit is structural in the arcade:
  the coroutine re-entry address is set **only inside the two turn arms**, so once turning the object resumes
  directly in the turn and never re-runs approach, the proximity check, or the fire. Exit is the standard
  offscreen cull; the ramping lateral velocity carries it off.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`. Line citations are
  `src/xevious_main.68k` unless noted. The two handlers are `handle_56_Jara` (3502–3511, the silent type: init,
  proximity check, and the `jara_set_clr_and_move` cruise) and `handle_55_Jara_shoots` (3537–3542, the shooter:
  the same init and proximity check jumping to `jara_shoot`), over the shared `jara_init` (3577–3586: the
  craft-excluding random column via `gen_rnd_spriteY` 5156–5169, the toward-craft aim via
  `calc_dX_dY_for_vector_to_solvalou` 5119 on the fast `angle_dX_dY_terrazi_torkan_tbl` 6325 (loaded at
  `jara_init` 3581–3582), `_PTS = 18` = 150 pts 3583, the initial code `0xA0` 3584, and the anim-timer clear
  3585 — with **no** `_FFREQ` write). The
  proximity band is `jara_check_proximity` (3588–3595): `(solvalou._Y − self._Y) − 6 + 0x0c` sets carry when
  the craft is within the [−6, +5] band on `_Y`. The one-way turn is `jara_set_dir` (3513–3515, side from the sign of
  `solvalou._Y − self._Y`) into `jara_moving_right` (3517–3534, `subq #1,(_dY,a5)` 3532, forward
  `jara_right_sprite_tbl` `0xA0..0xA5` 3571–3572) or `jara_moving_left` (3552–3569, `addq #1,(_dY,a5)` 3567,
  reversed `jara_left_sprite_tbl` `0xA5..0xA0` 3574–3575); both arms set the coroutine re-entry address (the
  `SET_REENTRY_ADDR_HERE` macro, at `jara_moving_right` 3518 and `jara_moving_left` 3553), the only place it is
  set, which is what makes the turn and the fire one-way.
  The single shot is `jara_shoot` (3544–3550): `init_new_bullet` (5012) **once**, then fall through to the
  turn; the bullet is a TYPE-6 re-aimed at the craft every frame by `handle_06_Bullet` (4278–4283). The spin is
  `(TIMER>>1) & 7` reset at 6 (3521–3528 / 3556–3563) — six codes advancing every two frames. The behavior is a
  port mapping of that logic, not copied text; the Jara paragraph of
  [aerial enemies](../spec/aerial-enemies.md) is the settled description this slice implements (corrected this
  slice — see deviation 5).
- Transfer class: Behavioral port (instruction-derived control flow and numeric constants; no source text,
  ROM, or media copied). The aim table and slot layout are the derived data of records
  [022](022-fire-permission-masks.md) and [023](023-aiming-and-slot-positions.md).
- Scratch interpretation: The ordered walk `advance slots` (SYS-04) dispatches a Jara-typed slot (type
  `JARA_SHOOTER_TYPE` = 85 = `0x55` or `JARA_SILENT_TYPE` = 86 = `0x56`) through **one** branch to the warp
  proc `update jara`; `spawn flying enemies` inits both types through the **one** shared `init jara`. The axes
  follow the family convention: `slot x` is the scroll/forward row (arcade `_X`), `slot y` the lateral column
  (arcade `_Y`); the proximity band and the peel ramp are both on the lateral column, matching the arcade's
  `_Y`. `install_init_jara` draws the spawn column through the shared helper with the **craft-gap exclusion on**
  (reject within `SPAWN_CRAFT_GAP` = 8 columns of the craft), aims the initial drift on the fast
  `aim dx 48`/`aim dy 48` tier, sets `slot pts` to the value-table slot for 150 pts, sets `slot code` to
  `0xA0`, clears the anim timer, and sets `slot flag` to `APPROACH` (0). It captures **no** fire mask and seeds
  **no** fire timer — the distinctive negative. The shared `update jara` runs under the HIT-vs-ACTIVE guard
  (explode on HIT, else the blaster hit check then the active body) and, when active:
  - transition: when `slot flag == APPROACH` **and** the lateral offset `player col − self col` is within the
    band `[JARA_PROXIMITY_LOW, JARA_PROXIMITY_HIGH]` = `[−6, 5]`, latch the turn — set `slot flag` to
    `TURN_MINUS` (1) when the offset is `≥ 0` (craft at or right of the column) or `TURN_PLUS` (2) when it is
    `< 0`, and, **only inside that transition and only for `slot type == JARA_SHOOTER_TYPE`**, fire **one**
    aimed bullet through the shared `_fire_aimed_bullet` (which reads the 32-tier for the bullet). The silent
    type reaches the same gate but has no fire call;
  - peel: when `slot flag == TURN_MINUS` ramp `slot dy` by `−JARA_TURN_LATERAL_ACCEL`; when `TURN_PLUS` ramp it
    by `+JARA_TURN_LATERAL_ACCEL` (= 2 per tick). `slot dx` is left untouched;
  - move and cull: `slot x += 4 * slot dx` / `slot y += 4 * slot dy`, four-edge cull frees the slot.

  The render target `jara_blocks` holds the static frame during `APPROACH` and, once turning, cycles the six
  frames by `phase = floor(slot timer / JARA_ANIM_PERIOD) mod JARA_ANIM_FRAMES` — `TURN_MINUS` forward
  (`phase + 1`), `TURN_PLUS` reversed (`FRAMES − phase`), mirroring the arcade's right/left tables. The
  fire-once guarantee is **structural**: the single `_fire_aimed_bullet` call is nested inside
  `if slot type == SHOOTER` inside the `if flag == APPROACH and in band` transition gate, so once `slot flag`
  leaves `APPROACH` the fire can never recur — the same downward-SUBSTACK structural gate that closed the
  Torkan "fires every tick" gap ([record 029](029-torkan-attack-and-retreat.md)).
- Scratch evidence: `install_init_jara` (shared) and `install_update_jara` (shared, reusing
  `_fire_aimed_bullet`, `COMPUTE_AIM`, the 48-tier `aim dx 48`/`aim dy 48` tables, the craft-excluding
  `_draw_spawn_column`, and the inlined move/cull), the single Jara branch in `install_advance_slots`, the two
  per-type spawn branches in `install_spawn_flying`, `jara_blocks` for the six-frame spin render, and the
  `JARA_*` tuning constants in `tools/game_director.py`; the structural contract `_air04_failures` and its
  per-clause negatives (`test_jara_slice_authoring_present` / `test_jara_slice_negative_fixtures`) in
  `tests/test_scratch_project.py`, whose clauses pin the shared lifecycle procs, the by-type spawn and the
  single shared dispatch, the 48-tier aim, the 150-pt award, the craft-excluding draw, the **absent** fire
  mask, that the shooter fires **exactly one** aimed bullet **nested in the APPROACH→TURN gate** (with
  corrupters that ungate the fire and that make the silent type fire), that the turn is proximity-gated, that
  TURN ramps `slot dy` **away** from the craft, and that the spin runs **only** after the turn; the live
  scenarios in `harness/lib/catalog.js` (`jara-shooter-fires-at-proximity` asserting the shooter allocates one
  bullet through the direct proximity-triggered path, `jara-peels-and-spins` asserting the one-way turn ramps
  the lateral velocity away while leaving the forward velocity untouched, and the extended
  `debug-key-cycles-families`), each with a biting negative.
- Acceptance criteria: Two Jara object types spawn by type from the debug cycle (and the natural wave), each
  cruising aimed at the craft at 3 px/frame with a **static** sprite; at a [−6, +5] lateral band each commits a
  one-way turn that peels **away** from the craft's column while spinning the six-frame cycle; the shooter
  fires **exactly one** aimed bullet at the turn and the silent type fires none; both score 150 pts,
  independently; adjacent spawns produce an **emergent pair that splits apart** (harness
  `jara-shooter-fires-at-proximity`, `jara-peels-and-spins`, `debug-key-cycles-families`, each with a biting
  negative); the operator playtest confirms the felt behavior — two flyers cruising in, then peeling apart with
  the shooter loosing a single shot, an emergent divergence rather than a synchronized split.
- Fidelity status: Verified line-by-line against the pinned reference this slice (the two handlers, the shared
  init, the proximity band, both turn arms and their sprite tables, the single-shot block, the bullet
  allocation and the TYPE-6 bullet handler, the craft-excluding random-column routine, and the fast aim table
  were read at the pin). The behavior matches the reference within the recorded deviations below; the spec's
  Jara paragraph was corrected to the source in the same slice under `guardrail-ack` (deviation 5).
- License status: The reference states no reusable license; only instruction-derived behavior and numeric
  constants are transferred (recorded in [the index](../spec/index.md) and the data files). No source text is
  reproduced. The six Jara sprite frames are the Aerial Enemies rip credited in
  `src/xevious/assets/provenance.json` (`https://www.spriters-resource.com/arcade/xevious/`, sheet author
  "CrazyCarl").
- Known deviations or uncertainty: (1) **Two arcade frames per tick (tick scaling).** The per-frame reference
  rates are doubled for the port's two-frame tick — the arcade ramps the peel velocity `±1` per arcade frame,
  so the port ramps `slot dy` by `JARA_TURN_LATERAL_ACCEL = 2` per tick, and velocities are scaled by the
  shared `×4` position step — the same tick scaling every family uses. The spin advances every two frames in
  both (`(TIMER>>1)` in the arcade, `JARA_ANIM_PERIOD = 2` in the port), preserving the six-frame cycle.
  (2) **Carry/bitwise math expressed as comparison and modulo.** Scratch has no carry flag or bitwise ops, so
  the arcade's proximity band (the `subq #6` / `add #0x0c` byte-carry test) is expressed as the arithmetic
  comparison `−6 ≤ (player col − self col) ≤ 5`, and the anim index `(TIMER>>1) & 7` (capped at six) as
  `floor(slot timer / 2) mod 6` — the same no-bitwise idiom recorded for the fire masks (record 022) and
  Torkan (record 029). The observable band and cadence are faithful; the exact byte-overflow arithmetic is
  not modelled ([[verify-behavior-against-pinned-reference]] on the sign — the band and peel are both on the
  lateral `_Y` axis, matching the Kapi convention, not a vertical row). (3) **Coroutine re-entry expressed as
  an explicit phase state.** The arcade holds its phase in the coroutine resume address
  (`SET_REENTRY_ADDR_HERE`); the port has no coroutine resume, so the approach/turn phase lives in `slot flag`
  (`APPROACH` / `TURN_MINUS` / `TURN_PLUS`), the same explicit-phase mapping recorded for Torkan
  ([record 029](029-torkan-attack-and-retreat.md)). (4) **One-shot fire via a phase-transition gate, not a
  resume-address lock.** The arcade guarantees the single shot because the resume address moves into the turn
  arm, so `jara_shoot` runs once; the port guarantees it structurally by nesting the single
  `_fire_aimed_bullet` inside the `APPROACH→TURN` transition gate, which cannot recur once `slot flag` leaves
  `APPROACH`. No fire mask and no fire timer are used — faithful to `jara_init` never setting `_FFREQ`.
  (5) **Corrected the spec's "left/right spin" and coupled-pair framing (spec error).** The prior prose read
  the turn as a "left/right spin" and implied a coupled pair. The source shows the "left/right" names only the
  **spin frame order** (`jara_right_sprite_tbl` / `jara_left_sprite_tbl`), while the motion is a lateral peel
  **away** from the craft's column on `_Y`; and there is **no pair linkage** — the pair is a spawn-stream
  convention and the split is emergent. The build follows the source; the spec was corrected under
  `guardrail-ack`. (6) **Fire-once-aimed bullet, not per-frame homing.** The arcade TYPE-6 bullet re-vectors
  onto the craft every frame; this port aims the bullet once at allocation and does not re-home
  ([record 026](026-enemy-bullets-and-collision-death.md), pre-existing). The shot is still aimed at the craft
  at fire time; it simply does not curve after launch. (7) **No enemy scroll.** The port has no
  background-scroll term for flying slots (they move purely by `slot dx`/`slot dy`) — the same "no enemy
  scroll" deviation class recorded for the Toroid, Terrazi, Kapi, Torkan, and Zoshi. (8) **Six extracted frames
  match the six arcade spin codes.** The arcade spins codes `0xA0`–`0xA5` (six codes); the Aerial Enemies rip
  supplies six distinct Jara frames, so the render maps one-to-one with no hold-last idiom, on the shared 16×16
  sprite cell, mirrored by family prefix onto the shared sprite-extraction proof, playing the shared explosion
  on a hit. (9) **Emergent pairing/separation, no arcade linkage.** Recorded so a reviewer does not read the
  spec's "pair" as a coupled split: the two types are fully independent objects; the visual pair and its
  divergence arise from adjacent spawns crossing the proximity band at different times and on opposite sides,
  with nothing coordinating them. No engine `slot link` field was added.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
