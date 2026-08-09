# Game director and state reset

- Mechanic: Explicit Stage-owned game-state director and serialized reset protocol.
- Derived behavior: Title, ready, playing, player-dead, respawning, and game-over form one guarded lifecycle; transitions cancel old work before finite scope-specific cleanup and state entry.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`; `src/xevious_main.68k`: `main_thread_main_loop`, `main_gameplay_loop`, `finish_solvalou_exploding`, `game_over`, `game_over_1_player`, `display_game_over`; canonical historical behavior in `assets/original/Xevious.sb3` for the retained title, movement, weapons, terrain, sound, and death demonstration.
- Transfer class: General behavior, numeric constant, and historical baseline.
- Scratch interpretation: The Stage owns `game state`, `state epoch`, `reset scope`, `death outcome`, an inspectable allowed-transition list, and one `transition to () reset ()` custom block. The block enters a temporary resetting state, waits for `director stop`, stops audio, waits for finite `director reset` handlers, then publishes the destination through `director enter`. D and G are temporary respawn and terminal-death fixtures whose outcome decision remains Stage-owned.
- Scratch evidence: Stage variables (including the shared `bomb in flight` weapon guard) and `allowed transitions`; broadcasts `director stop`, `director reset`, `director enter`, `ready complete`, `death complete`, `game over complete`, and the `bomb` release broadcast; the blaster `blaster reload` counter and per-strip `scroll step` counters; finite reset handlers in `solvalou`, `blaster`, `area_01a`, `area_01b`, `start_screen`, `solv_death`, `target_a`, `target_b`, and `bomb`; deterministic generation in `tools/game_director.py`; structural and negative-fixture checks in `tests/test_scratch_project.py`.
- Acceptance criteria: Green flag from every state produces the same title (gliding in from the top); Space performs one title-to-ready-to-playing sequence; READY holds its 30-tick beat with no speech bubble; D performs the tick-counted explosion, the post-death pause, and one respawn READY; G performs the death sequence and cold-title return (the GAME OVER presentation, its hold included, is deferred to ECO-04); held fire polls at the blaster reload cadence and one bomb arms at a time; gameplay input is inert outside playing; new-life reset preserves terrain while cold-start/new-game rewind it; old scripts, sounds, and clones cannot act after transition cleanup.
- Fidelity status: Repo-derived lifecycle interpreted for the current partial project; D/G death outcomes are deliberate pre-life-economy fixtures.
- License status: The pinned reference repository states no reusable license; no reference source text or media was copied.
- Known deviations or uncertainty: Attract, credits, player alternation, real collision death, life-count decisions, scoring, enemies, and high-score flow remain later slices. READY uses a Scratch speech bubble and the fixture timing is project-defined pending the presentation fidelity slice.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.

## Correction (2026-08-09 fidelity audit)

This record's retention claims were audited against the preserved baseline and found overstated. The
slice did not retain canonical historical behavior for the title (the entry glide was replaced by
immediate placement) or for held fire, the bomb lockout, terrain wrapping, shot expiry, the crosshair
and impact-marker animations, layer ordering, or the post-death pause — all working baseline behaviors
this slice replaced or removed without disclosure. The new-life terrain preservation, recorded above as
a deliberate fixture, additionally displaced the baseline's own arcade-matching terrain restart on
death, which this record did not disclose. The full inventory and dispositions are in
[the committed fidelity audit](../audit/2026-08-09-fidelity-audit.md); the recovery obligations belong
to the regression-recovery build tracked from it.

## Recovery build (2026-08-09, issue #13)

The regression-recovery build discharges audit items B1-B10 and A1-A2, re-expressed inside this director
(not a revert to the baseline's `begin`/`death` broadcasts). Markers, per behavior:

- **B1 held fire / B8 shot** — polled fire with a reload counter; the reload cadence cites
  [player craft and weapons](../spec/player-craft-and-weapons.md) (WPN-01), as does the shot's forward
  travel and top-edge expiry. The shot's per-tick travel *magnitude* is preserved-baseline (the spatial
  factor is unratified until the movement slice).
- **B2 bomb lockout / B6 crosshair / B7 impact marker** — one bomb arms at a time (WPN-04); the `bomb`
  broadcast drives the drop, the crosshair release animation, and the `target_b` marker. The re-arm
  timing is preserved-baseline (baseline ~0.75 s cooldown); the reference re-arm path is unpinned (WPN-04).
- **B3 terrain** — counted-cycle wrap (690 steps per strip), preserved-baseline; the position-threshold
  wrap is recorded as unreachable under Scratch sprite fencing.
- **B4 title glide** — preserved-baseline (1 s glide from the top; no locked arcade value exists —
  [cabinet flow](../spec/cabinet-flow.md) owns the title stage and is draft).
- **B5 death cue / B10 post-death pause** — the explosion and pause cite
  [player craft and weapons](../spec/player-craft-and-weapons.md) (PLY-02). The pause lets the measured
  1.361 s death cue finish before the transition stops sounds (a ~0.1 s margin; confirmed at playtest).
- **B9 layering** — faithful restore of the baseline's own layer calls (craft to front each tick; terrain
  to back).
- **A1 READY bubble / A2 GAME OVER bubble** — removed. The READY *beat* is kept as a tick-counted hold
  (project-defined placeholder, no reference basis); the GAME OVER presentation and its hold are deferred
  to ECO-04.

**Tick conversion (units rule).** Gameplay timing is counted in build ticks (1 tick = 2 arcade frames;
[core game systems](../spec/core-game-systems.md)). Only the tick roundings live here; each arcade-frame
original stays in its locked spec section, cited above, never restated: blaster reload 10 ticks; explosion
7 holds of 4 ticks (28 ticks total); post-death pause 16 ticks; READY hold 30 ticks. The former wall-clock
`wait` blocks are gone from the touched gameplay scripts.

**No-regression control.** `tests/test_scratch_project.py` now encodes the recovery contract structurally
and adds the repository's first negative fixtures (one per restored behavior, proving the contract goes
red when broken). This catches removal and shape drift of the asserted blocks — a real gain over what let
PR #9 through — but not shape-preserving behavioral drift; the operator playtest remains the gameplay gate.
