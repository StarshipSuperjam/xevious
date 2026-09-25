# Grobda and Domogram — GND-06 / GND-07, the self-moving ground families

- Mechanic: the last two slice-13 ground leaves, and the first ground objects that move under their OWN
  velocity instead of the fixed terrain scroll. The **Grobda** (`0x2C` + `0x35`–`0x40`, twelve live variants)
  is a reticle-reactive tank that never fires; the **Domogram** (`0x2E`) is a scripted-path shooter that fires
  one aimed shot per animation cycle. Both build on the slice-9 ground pipeline
  ([033](033-ground-bombing-pipeline.md)) and the slice-12/13 dome/turret waves
  ([041](041-ground-domes-and-turrets.md), [042](042-boza-logram.md)); their one genuinely new subsystem is a
  shared per-slot velocity mover, built once and used by both. This record covers the two leaves
  `ground.grobda` (#88, GND-06) and `ground.domogram` (#89, GND-07).

- Derived behavior: two families over one new movement seam.
  - **The shared velocity seam — a ground object that moves by its own delta, not the scroll.** Every ground
    family before these was terrain-locked: it advanced only with the fixed scroll and was culled off the
    bottom. A Grobda and a Domogram instead advance each tick by their OWN stored velocity — `slot x` by
    `4 × slot dx`, `slot y` by `4 × slot dy` — with **no scroll term added on top**. The arcade bakes the
    scroll INTO the stored delta (raw dX 8 is scroll-matched, so a "stopped" object still drifts down the field
    at the scroll rate), so adding a separate scroll baseline would double the along-scroll speed. The off-field
    cull is unchanged from the terrain scroller.
  - **The Grobda: twelve variants, none fires, all react to the reticle.** A Grobda arms a 48-frame reaction
    only while the craft's reticle sits inside a **[−2, +1]** per-axis alignment band of its cell, on
    both the depth and lateral axes. Two reticle windows drive it: the moving **crosshair** (led ahead of the
    craft) for the "in-crosshairs" variants, and the frozen **bomb target** cell for the "targeted" variants.
    The twelve variants span: stationary; forward; crosshairs→forward-forever; forward→crosshairs-stop-48f→
    resume; targeted→back-48f→stop; forward→crosshairs-dart-48f→resume; forward→targeted-back-48f→resume;
    targeted→dart-48f→stop→**re-arm** (`0x3C`, the only repeatable one, worth 10,000); and the four water
    mirrors. Raw handler velocities are 8 (stop/scroll-matched), 14 (forward), 2 (backward) and 22 (dart), all
    on the scroll axis only (the lateral delta is cleared). A Grobda **never fires**. A bombed **land** variant
    craters persistently like a Barra; a bombed **water** variant plays the explode-and-remove burst and
    vanishes.
  - **The Domogram: a scripted path that also shoots.** The first ground family that both moves and fires. Each
    tick it advances a **path coroutine**: it holds the current vector for a stored duration, then loads the
    next step's `(dY, dX)` from a 32-entry vector table by the step's stored index; when the path is exhausted
    it holds the last vector forever. In parallel it runs the shared stop-firing-row gate — while still high
    enough on the field and on the every-8th-frame phase, it counts a masked shot timer down, and on expiry
    starts a **24-frame** shot animation that fires ONE aimed bullet at the animation **midpoint (frame 12)**,
    then reloads the timer with a fresh masked-random delay. It scores **800**; a bomb craters it persistently
    like a Barra.

- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`; citations are
  `src/xevious_main.68k` unless noted. **Grobda** — the twelve variant handlers are `handle_2C_Grobda_stationary`
  4289–4300, `handle_35_Grobda_fwd` 4301–4311, `handle_38_Grobda_fwd_crosshairs_stop_fwd` 4339–4349,
  `handle_39_Grobda_targeted_back_stop` 4365–4375, `handle_3C_Grobda_targeted_fast_fwd` 4447–4470 (the sole
  re-arming variant), `handle_3D_Grobda_stationary_in_water` 4471–4481 and
  `handle_40_Grobda_fwd_targeted_back_fwd_in_water` 4511–4573 (none of the twelve calls a fire routine). The
  activation helper `activate_and_set_grobda_dX` 4574–4579 sets the reaction velocity into `_dX` and CLEARS
  `_dY` (scroll-axis-only motion); the two reticle windows are `check_targeted_and_init_timer` 4581–4596
  (reads the bomb-target object at `obj_tbl` slot `0x20`) and `check_grobda_in_crosshairs` 4597–4610 (reads the
  crosshairs object at slot `0x22`), each a `subq #2 / addq #4` unsigned-range test that matches a per-axis
  delta of −2..+1 and, on a match, arms `_TIMER=48`. **Domogram** — `handle_2E_Domogram` 4620–4648 inits the
  object (`_PTS=42`=800, `_VECLEN=1`, the captured `ffreq_mask_domogram`, a masked-random `_TIMER`, and `_TYPE`
  cleared as the shot-animation timer) and runs the path coroutine (decrement `_VECLEN`, and on zero read the
  next duration + vector index through the `_EXTRA` pointer and `cpy_dY_dX_to_obj` from the table, then
  decrement `_NVEC`); `domogram_done_all_vectors` 4649–4654 holds the last vector when `_NVEC` reaches 0;
  `domogram_main` 4655–4667 gates firing on `gnd_stop_firing_row` (`jcs` when `gnd_stop_firing_row < _X`) and
  the `countup_timer_1 & 7` phase, starting a 24-frame animation on shot-timer expiry; `domogram_shooting`
  4668–4688 fires one `init_new_bullet` when `_TYPE` decrements to 12 and reloads the masked shot timer; the
  velocity table is `domogram_vector_tbl` 4695–4744 (dY,dX pairs). The shared movers are `move_object_dX_dY`
  4817–4823 (two-axis) and `move_object_dX` 4842–4848 (one-axis), each `_X/_Y += 2 × _dX/_dY` with no scroll
  term; the SEPARATE static-terrain scroll used by terrain-locked objects is `scroll_sprite_X` 4849–4855
  (`_X += 2 × −scroll_delta`). Placements and fire masks per area are the committed
  [schedule data](../spec/data/area-schedules.json); the Domogram vector table is
  [domogram.json](../spec/data/domogram.json); the settled behaviour is
  [ground objects](../spec/ground-objects.md); point values are
  [scoring, lives, and game over](../spec/scoring-lives-and-game-over.md).

- Transfer class: General behavior and numeric constants (instruction-derived control flow — the velocity
  mover, the twelve Grobda reaction tables, the [−2, +1] reticle band, the Domogram path coroutine and its
  midpoint fire — over the committed, hash-pinned schedule, vector, and scoring tables; no source text or media
  copied).

- Scratch interpretation: both families are added to the `.sb3` generator `tools/game_director.py` along the
  same seams the slice-9/12/13 families use — type codes in `GROUND_HANDLED_TYPES`, spawn branches that stamp
  one slot each, walk-dispatch entries routing each type to `install_update_grobda` / `install_update_domogram`,
  and renderers reading the parallel slot lists (the twelve Grobda variants share one tank tread costume set;
  the Domogram carries its own idle set). The new movement subsystem is one shared warp proc,
  `advance ground moving` (`install_advance_ground_moving`), that both families call while ACTIVE in place of
  the terrain scroller `advance ground`.
  - **Port necessity — a per-slot velocity mover, distinct from the terrain scroller.** The arcade has two
    move routines: `move_object_dX`/`move_object_dX_dY` (velocity) for self-moving objects and `scroll_sprite_X`
    (terrain scroll) for terrain-locked ones, and each handler calls exactly one. The port had only the
    terrain scroller (`advance ground`, `slot x += AREA_PROGRESS_STEP`). GND-06/07 add the velocity analog:
    `advance ground moving` advances `slot x`/`slot y` by `TICK_VELOCITY_SCALE × slot dx`/`slot dy` and runs the
    same off-field cull, with **no `AREA_PROGRESS_STEP` baseline**. A Grobda's raw dX 8 is scroll-matched, so
    `TICK_VELOCITY_SCALE × 8 = 32 = AREA_PROGRESS_STEP` — a stopped Grobda drifts down at exactly the scroll
    rate, and adding a baseline would double it. Static families keep `advance ground` unchanged; only these two
    families (and only while ACTIVE) use the mover. A struck (HIT) Grobda or Domogram craters in place and so
    reverts to the terrain scroller — the crater is terrain-locked, exactly like a Barra.
  - **Port necessity — `TICK_VELOCITY_SCALE = 4`, and raw deltas stored unconverted.** The arcade moves
    `2 × velocity` per frame and runs two frames per port tick, so the port applies `4 × velocity` per tick.
    The stored deltas (Grobda's 8/14/2/22; the Domogram vector table) are kept RAW — no net-of-scroll
    conversion at ingest — so the Domogram vector table stays byte-equal to the committed
    [domogram.json](../spec/data/domogram.json) and the golden round-trip compares decoded columns directly.
  - **Port necessity — reaction and animation timers are arcade-frame counts, stepped by the frame convention.**
    The Grobda's 48-frame reaction and the Domogram's 24-frame shot animation are counted DOWN by
    `TICK_TIMER_STEP` (2 frames/tick), like every other ground/air timer — not by literal tick counts. 24 and
    12 are both even, so the midpoint `== 12` check is hit exactly.
  - **Port necessity — one Grobda update proc over a variant table, not twelve patched handlers.** The arcade
    patches twelve separate handlers into its object table. The Scratch walk dispatches by object type, so the
    port cannot patch twelve handlers; instead one `update grobda` proc iterates a `GROBDA_VARIANTS` table
    (type, points, initial dX, trigger reticle, reaction dX, end dX, re-arm flag, water flag) and builds each
    variant's arm/countdown/latch reaction inline. This is a structural consolidation, the same one
    [041](041-ground-domes-and-turrets.md)/[042](042-boza-logram.md) made for the Garu and Boza composites, not
    a behavioural change.
  - **Port necessity — the Domogram path is decoded into runtime slot columns.** The arcade holds the path as a
    pointer (`_EXTRA`) into a flat `duration, vector_index` byte stream, with `_NVEC` steps remaining and
    `_VECLEN` frames left on the current vector. Scratch has no pointers, so the ingest flattens every instance's
    path into shared step columns (`domogram path vector` / `domogram path duration`) and the runtime carries
    the pointer as a 1-based index in `slot link` (`_EXTRA`), the steps-remaining in `slot vec left` (`_NVEC`),
    and the frames-left in `slot flag` (`_VECLEN`); the shot-animation timer `_TYPE` is `slot fire timer`. This
    is the first ground payload beyond the three scalar columns, so a round-trip golden proves the decoded
    columns and the 32-entry vector table equal the committed data.
  - **State mapping.** The arcade's active `_STATE=2` and hit `_STATE=3` map to the port's `SLOT_ACTIVE=1` and
    `SLOT_HIT=2` — the same mapping the shared ground detector already uses. The Grobda reaction phase carried
    in the port's `slot flag` (0 pre-trigger, 1 reacting, 2 latched) tracks the arcade's re-entry-address /
    timer state machine; the water variants' explode-and-remove burst reuses the Garu node's remove-frame path.
    The arcade `_PTS` bytes (21/30/36/48/51/54/57/63 for the Grobda tiers, 42 for the Domogram) map to the
    port's 1-based value-table positions (8/11/13/17/18/19/20/22, and 15 for 800).

- Scratch evidence: harness scenarios with biting negatives, structural guards each paired with a severing
  negative, and fresh roadmap-evidence markers.
  - Harness (`harness/lib/catalog.js`, negatives in `harness/lib/mutate.js`):
    `grobda-moves-by-its-own-velocity-no-double-scroll` (a stopped 0x2C drifts at exactly 32/tick and a forward
    0x35 at 56/tick, scroll-axis-only, never firing — proving the seam adds no scroll baseline),
    `grobda-reacts-to-reticle-only-inside-the-band` (a 0x38 arms its stop reaction only when the crosshair is
    inside the band, holds its forward velocity out of band, never fires),
    `grobda-land-craters-water-vanishes` (a bombed 0x2C craters persistently; a bombed 0x3D vanishes mid-field
    on the 28-frame remove clock), `domogram-follows-scripted-path-then-holds-last-vector` (the follower decodes
    two vectors from the shared columns and holds the last when the path is exhausted),
    `domogram-fires-one-aimed-shot-at-anim-midpoint-gated` (it fires exactly one aimed bullet at animation
    frame 12, and is silent with its shot countdown untouched past the stop-firing row), and
    `domogram-craters-when-bombed` (a struck Domogram scrolls with the terrain and ignores its own leftover
    velocity, cratering persistently). Each is proven against the real build and a mutated build that fails the
    same assertion. `ground-dispatch-spawns-scoped` and the debug-key cycle scenario are widened so the now-built
    Grobda and Domogram types count as in-scope handled families.
  - Structural (`tests/test_scratch_project.py`): `_gnd06_failures` — present-and-negative guards pinning the
    Grobda (twelve variants stamped under their types; none fires; the reticle windows read the bomb-target and
    crosshair slots on a [−2, +1] band; a reaction commits react dX, arms the 48-frame timer, and re-arms vs
    latches; the velocity seam moves the slot; land craters vs water vanishes; spawn seeds and clears the reused
    columns). `_gnd07_failures` — guards pinning the Domogram (state split; persistent crater; the path decoded
    into runtime columns; the follower holds the last vector; the 24-frame midpoint fire arm-gated on the
    stop-firing row and phase-gated; the velocity seam moves the slot; spawn primes the path). Each clause is
    paired with a biting negative. A `domogram.json` round-trip golden in `tests/test_spec_docs.py` compares the
    build's decoded path columns and 32-entry vector table against the committed data.
  - Records / roadmap: fresh `roadmap-evidence: GND-06 success|failure` and `roadmap-evidence: GND-07 success|
    failure` markers on the added tests, plus the deterministic-build gates
    `test_two_clean_processes_build_identical_bytes` and `test_game_director_generator_is_current`.
  - Operator playtest: the on-screen feel — a Grobda reacts to the reticle (stops, backs, or darts per variant)
    and never fires, land ones cratering and water ones vanishing; a Domogram patrols its scripted path and
    fires one aimed shot at its animation midpoint; bomb it for 800.

- Acceptance criteria: Engine — the six harness scenarios pass with their biting negatives; the
  `_gnd06_failures` and `_gnd07_failures` structural guards hold and every negative bites; the `domogram.json`
  round-trip golden holds; the build is byte-identical across two clean processes and the generator is current;
  every ground, domogram, and mechanics citation resolves at the pin. Operator — a Grobda reacts to the reticle
  per variant and never fires (land craters, water vanishes); a Domogram follows its scripted path and fires one
  aimed shot at its animation midpoint; bombing it scores 800.

- Fidelity status: GND-06 (Grobda) and GND-07 (Domogram) are **built** — live and proven in the harness and the
  structural guards, with the on-screen feel to be confirmed by the operator playtest. With these two leaves the
  slice-13 ground half is complete; the remaining ground roster (slice 14: `ground.sol-tower`,
  `ground.bonus-flag`, and the hidden credit event) stays `provisional` under the locked ground spec and is not
  built here.

- License status: The pinned reference states no reusable license; only instruction-derived behaviour and the
  committed, hash-pinned tables (the [schedule data](../spec/data/area-schedules.json), the
  [Domogram vector table](../spec/data/domogram.json), and the scoring values) are used, cited to the settled
  spec and the data files, and no reference source text or media was reproduced. Ground sprite art is credited
  in `src/xevious/assets/provenance.json` (https://www.spriters-resource.com/arcade/xevious/); the twelve Grobda
  variants share one tank tread crop set and the Domogram carries its own idle set.

- Known deviations or uncertainty: no locked-spec correction to the Grobda or Domogram *behaviour* this leaf —
  the source and the settled [ground objects](../spec/ground-objects.md) spec already agree, so this PR carries
  **no `guardrail-ack`**. The five port necessities above (the per-slot velocity mover distinct from the terrain
  scroller; `TICK_VELOCITY_SCALE=4` with raw deltas; frame-stepped timers; one Grobda proc over a variant table;
  and the Domogram path decoded into runtime columns) are structural translations into Scratch's flat slot lists
  and tick convention, not behavioural changes. The off-field cull for the moving seam is bottom-only, matching
  the established ground scroller `advance ground` — the arcade `check_scroll_offscreen` also tests the lateral
  edge, but ground objects scroll down and move gently, so the bottom edge is their only realistic exit (the
  same convention every prior ground family uses). The exact on-screen rhythm of each Grobda variant's reaction
  and the Domogram's patrol-and-fire cadence remain for the operator playtest to confirm, along with the
  operator's pixel-verification of the shared tank tread and Domogram idle crops.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
