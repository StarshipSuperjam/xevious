# Ground domes and turrets — GND-01 variants, GND-02 (Zolbak), and GND-04 (Derota / Garu Derota)

- Mechanic: the second wave of ground families, built on the slice-9 ground pipeline
  ([033](033-ground-bombing-pipeline.md)). One record covers the three slice-12 leaves that ship together:
  `ground.barra-variants` (#84, GND-01.variants) — closed as already-delivered because the Barra family's
  only variant at the pin is the Garu Barra, built in slice 9; `ground.zolbak` (#85, GND-02) — the passive
  detector dome whose destruction eases enemy pressure; and `ground.derota` (#86, GND-04) — the Derota
  firing turret and the Garu Derota firing base+node pair. All three reuse the shared ground scroller,
  the type-agnostic bomb-hit/award sweep, and (for the turrets) the shared fire-permission gate the
  slice-9/10 families already established, so they add object behaviour, not new pipeline.

- Derived behavior: two net-new firing/pressure families join the passive Barra and the arming Logram.
  - **GND-01.variants — the Barra family has one variant, already built.** At the pin the Barra family is
    exactly base Barra `0x1E` and Garu Barra `0x20` (there is no other Barra-family object code). The Garu
    Barra — an indestructible double-size base plus an 8-px-lateral destructible node worth 300 that
    vanishes cleanly — shipped in slice 9 (record GND-01). This leaf adds no gameplay; it closes against
    that build, proven by the structural guard's Garu-Barra variant clauses.
  - **Zolbak (GND-02) — the passive dome that eases pressure.** The Zolbak is the Barra crater model with
    one extra behaviour: it never fires, scores 200, and on a bomb hit it plays the shared explosion and
    converts to a persistent crater that scrolls until culled (never removed on the clock) — exactly like a
    Barra. The addition is that on the **first** hit tick it reduces the adaptive enemy AI level by exactly
    2, clamped to 0 on the unsigned underflow (a kill at level 1 lands at 0, not −1; a kill at level 5
    drops the full 2 to 3). The reduction runs once per kill: the bomb-hit sweep zeroes the crater clock at
    the hit, so `slot timer == 0` uniquely marks that first tick, and the crater clock only climbs
    afterwards. It reads and writes the same `ai level` the difficulty director grows and folds
    ([032](032-live-pressure.md)).
  - **Derota (GND-04) — a plain periodic aimed turret, 1000 pts.** Unlike the Logram there is **no**
    open/close dome cycle: while active the Derota drives the shared fire-permission gate
    (`chk_timer_fire_bullet_reinit_timer`) to fire one aimed bullet per masked-random reload, but only
    while still high enough on the field — armed by `cur_row <= gnd_stop_firing_row`. Once it scrolls past
    that row it is silent, and the gate never even runs, so its countdown is untouched. A bomb hit craters
    persistently like a Barra.
  - **Garu Derota (GND-04) — a firing base+node pair, 2000 pts.** The Garu Derota is the Garu Barra's
    two-slot shape (indestructible `SLOT_GARU_BASE` double-size base plus a destructible node centred on it)
    with one difference: the node **fires**. Its node drives the same shared fire-permission
    gate every active tick, but — unlike the single Derota — **unconditionally of the row** (the arcade
    `garu_derota_handler` omits the stop-firing-row skip), so it fires even below a row that would silence
    a Derota. The node scores 2000 and, when bombed, explode-and-removes (vanishes at `GARU_REMOVE_FRAMES`,
    no crater); the base is never scored and only scrolls.

- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`, `src/xevious_main.68k`.
  GND-01 variant: the indestructible base plus 8-px-lateral destructible node is `handle_20_Garu_Barra`
  (2657–2675) with its centre `garu_barra_handler` (2678–2682) (hit ⇒ explode-and-remove). Zolbak
  (GND-02): the passive 200-pt dome that never fires and craters on death is `handle_1F_Zolbak` (2686–2696)
  (`_PTS=21`, `_STATE=2`), and the on-death AI reduction — subtract 2, clamp the unsigned underflow to 0 —
  is `reduce_enemy_ai_by_2` (2698–2704) (`subq #2; jcc; moveq #0`, then `handle_bomb_explosion`). Derota
  (GND-04): the periodic turret is `handle_1B_Derota` (2792–2794) into `init_derota` (2799–2818) — `_PTS=48`
  (1,000 pts), the masked reload `_TIMER=(rand & mask)+1`, and the stop-firing-row arm (`cmp` of `_X`
  against `gnd_stop_firing_row`, `jcs` skip so it fires only while `_X <= gnd_stop_firing_row`). Garu
  Derota (GND-04): the firing base+node pair is `handle_21_Garu_Derota` (2821–2844) (base `_STATE=3`
  indestructible 2x2, node `_STATE=2` `_PTS=54` = 2,000 pts, node `_FFREQ=ffreq_mask_derota`) whose node
  handler `garu_derota_handler` (2846–2852) calls `chk_timer_fire_bullet_reinit_timer` with **no** row gate
  (hit ⇒ explode-and-remove). Placements, fire masks, and stop-firing rows per area are the committed
  [schedule data](../spec/data/area-schedules.json); the settled behaviour is
  [ground objects](../spec/ground-objects.md); point values are
  [scoring, lives, and game over](../spec/scoring-lives-and-game-over.md).

- Transfer class: General behavior and numeric constants (instruction-derived control flow — the AI-level
  −2 clamp, the Derota stop-firing-row arm, the Garu Derota node's unconditional fire, the masked reload —
  over the committed, hash-pinned schedule and scoring tables; no source text or media copied).

- Scratch interpretation: the two families are added to the `.sb3` generator `tools/game_director.py` along
  the same seams the slice-9 families use — a type code in `GROUND_HANDLED_TYPES`, a spawn branch that
  stamps the slot's value (and, for the turrets, the captured Derota fire mask and a masked initial
  reload), a walk-dispatch entry routing the type to its `install_update_*` proc, and a renderer reading
  the parallel slot lists. `install_update_zolbak` clones the Barra crater wrapper and adds the guarded
  AI-level reduction in its HIT branch (`if slot timer == 0: change ai level by −2; if ai level < 0: set
  ai level 0`), keeping the reduction to the first hit tick. `install_update_derota` clones the arm gate
  (`armed = not(cur_row > ground stop firing row)`) and drives the shared fire-permission gate while armed,
  with no dome animation. `install_update_garu_derota` mirrors the Garu Barra base+node wrapper but, for an
  ACTIVE node, drives the shared gate unconditionally. Bomb-hit and scoring need no per-family change — the
  shared `install_check_ground_hit` scores any ACTIVE ground slot by its `slot pts`, so each family only
  stamps the right value and state at spawn.
  - **Port necessity — one wrapper per type, branching on slot state.** The arcade patches a *separate*
    handler into its object table for the Garu Derota node (`garu_derota_handler`), distinct from the base.
    The Scratch walk dispatches by object *type*, and the base and node share `GARU_DEROTA_TYPE`, so the
    port cannot patch two handlers; instead one `update garu derota` proc branches on slot state
    (`SLOT_GARU_BASE` base vs `SLOT_ACTIVE`/`SLOT_HIT` node) to run the base scroll, the node fire, or the
    node explode-and-remove. The node's fire path reuses the same shared fire-permission gate the single
    Derota uses (the arcade calls the same `chk_timer_fire_bullet_reinit_timer` from both), so this is a
    structural consolidation of two handlers, not a behavioural change.

- Scratch evidence: harness scenarios with biting negatives, structural guards each paired with a severing
  negative, and fresh roadmap-evidence markers.
  - Harness (`harness/lib/catalog.js`, negatives in `harness/lib/mutate.js`):
    `zolbak-craters-and-reduces-ai` and `zolbak-ai-reduction-floors-at-zero` (the 200-pt crater plus the
    once-per-kill AI drop and its zero floor); `derota-fires-when-armed-silent-past-stop-row` and
    `derota-craters-when-bombed`; `garu-derota-base-indestructible` and
    `garu-derota-node-fires-scores-and-vanishes` (the 2000-pt node fires with no row gate and vanishes
    while the base survives). Each is proven against the real build and a mutated build that fails the same
    assertion. `ground-dispatch-spawns-scoped` is widened so the now-built Zolbak/Derota/Garu-Derota types
    count as in-scope handled families.
  - Structural (`tests/test_scratch_project.py`): `_gnd02_failures` and `_gnd04_failures` — present-and-
    negative guards pinning the Zolbak wrapper (warp, never fires, craters persistently, reduces the AI
    level by 2 once per kill, floored at 0, awards 200) and the Derota/Garu Derota wrappers (Derota fires
    the shared gate arm-gated on the stop-firing row, craters, awards 1000; Garu Derota node fires under
    `state == ACTIVE` with no row gate, explode-and-removes, base stamped `SLOT_GARU_BASE`, node awards
    2000). GND-01.variants closes through the existing `_gnd01_failures` Garu-Barra clauses
    (`test_barra_variants_closure_present` / `_negative`).
  - Records / roadmap: fresh `roadmap-evidence: GND-01 success|failure`, `GND-02 success|failure`, and
    `GND-04 success|failure` markers on the added tests, plus the deterministic-build gates
    `test_two_clean_processes_build_identical_bytes` and `test_game_director_generator_is_current`.
  - Operator playtest: the on-screen feel — bombing a Zolbak craters it, scores 200, and visibly eases the
    following pressure; a Derota fires aimed shots once it scrolls in and is destroyed for 1000; a Garu
    Derota base is indestructible while its node fires and scores 2000.

- Acceptance criteria: Engine — the six harness scenarios pass with their biting negatives; the two new
  structural guards hold and every negative bites; the GND-01.variants closure clauses hold; the build is
  byte-identical across two clean processes and the generator is current; every ground citation resolves at
  the pin. Operator — bombing a Zolbak craters, scores 200, and eases subsequent pressure; a Derota fires
  aimed shots when armed and is destroyed by a bomb for 1000; a Garu Derota base survives while its node
  fires and scores 2000; and the slice-9 Garu Barra still behaves (base indestructible, node +300) for the
  GND-01.variants closure.

- Fidelity status: GND-01.variants (Garu Barra, already delivered slice 9), GND-02 (Zolbak), and GND-04
  (Derota / Garu Derota) are **built** — live and proven in the harness and structural guards, with the
  on-screen feel to be confirmed by the operator playtest. The remaining ground families
  (`ground.boza-logram`, `ground.grobda`, `ground.domogram` of slice 13) stay `provisional` under the
  locked ground spec and are not built here.

- License status: The pinned reference states no reusable license; only instruction-derived behaviour and
  the committed, hash-pinned tables (the [schedule data](../spec/data/area-schedules.json) and the scoring
  values) are used, cited to the settled spec and the data files, and no reference source text or media was
  reproduced. Ground sprite art is credited in `src/xevious/assets/provenance.json`
  (https://www.spriters-resource.com/arcade/xevious/).

- Known deviations or uncertainty: one locked-spec correction and one felt-cadence observation.
  - **The Derota fire masks are frequency/cadence masks, not fire-permission masks.** The locked
    [ground objects](../spec/ground-objects.md) spec described `ffreq_mask_derota` as a "fire-permission"
    mask; the source (`init_derota`, `garu_derota_handler`) uses it only to scale the reload timer
    `_TIMER=(rand & mask)+1` — there is no "mask ⇒ don't fire" branch. The only on/off gate is
    `gnd_stop_firing_row`. This matches the correction record [032](032-live-pressure.md) already made for
    the difficulty spec; the ground-objects prose is corrected here to match (source wins), under the
    operator's `guardrail-ack`.
  - **The Garu Derota node's felt fire cadence is an operator observation.** The node fires one aimed
    bullet per masked reload with no row gate; the mechanism is proven in the harness, but the exact
    on-screen rhythm relative to a Derota is a point for the operator playtest to confirm.
  - **Port necessity — the node reuses the base's own frame and is centred on it, not drawn as a separate
    offset sprite.** The arcade Garu is a 2×2 base plus a small 1×1 node; it offsets the node by +1 cell on
    each axis (`_X = 0x0100`, `_Y = base_Y - 0x0100`) to centre a **corner-anchored** node inside a
    corner-anchored base. An early port copied that offset literally while drawing the node as a *separate,
    smaller* sprite (the Barra idle pyramid / a lone Derota turret) on top of the full base — so the node
    read as a **second** pyramid/turret poking out of a corner (the "doubling" the operator saw in the PR
    #139 playtest). In the port both costumes are **centre-anchored** 32-px frames, and the two sheet frames
    are simply the two game states of the one mound: `garu/base/01` is the bare pyramid (Barra) /
    `garu-derota/base/01` the closed turret, and `.../02` is the same mound *with* its live core/open turret.
    So the base clone draws frame 01 and the node clone draws frame 02 **centred on the very same cell**
    (zero relative offset) — together they read as one cored mound, and bombing the node reveals the bare
    base. The node's `slot x`/`slot y` therefore equal the base's; because `slot x`/`slot y` are both the
    drawn position and the bomb-hit position, the hit cell now sits under the visible core (bomb-aim
    *improves*), and the base↔node linkage is by slot **index** (N+1), not coordinate, so it is unaffected.
    (The earlier attempt to instead scale the depth offset by the anamorphic ratio, `GARU_NODE_DEPTH_UNITS`,
    only slid the second sprite and was reverted.) The Boza composite keeps its own anamorphic spacing —
    see [042](042-boza-logram.md) (`GROUND_DEPTH_UNITS_PER_PX`).
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
