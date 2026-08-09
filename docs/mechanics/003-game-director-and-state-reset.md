# Game director and state reset

- Mechanic: Explicit Stage-owned game-state director and serialized reset protocol.
- Derived behavior: Title, ready, playing, player-dead, respawning, and game-over form one guarded lifecycle; transitions cancel old work before finite scope-specific cleanup and state entry.
- Reference provenance: `jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183`; `src/xevious_main.68k`: `main_thread_main_loop`, `main_gameplay_loop`, `finish_solvalou_exploding`, `game_over`, `game_over_1_player`, `display_game_over`; canonical historical behavior in `assets/original/Xevious.sb3` for the retained title, movement, weapons, terrain, sound, and death demonstration.
- Transfer class: General behavior, numeric constant, and historical baseline.
- Scratch interpretation: The Stage owns `game state`, `state epoch`, `reset scope`, `death outcome`, an inspectable allowed-transition list, and one `transition to () reset ()` custom block. The block enters a temporary resetting state, waits for `director stop`, stops audio, waits for finite `director reset` handlers, then publishes the destination through `director enter`. D and G are temporary respawn and terminal-death fixtures whose outcome decision remains Stage-owned.
- Scratch evidence: Stage variables and `allowed transitions`; broadcasts `director stop`, `director reset`, `director enter`, `ready complete`, `death complete`, and `game over complete`; finite reset handlers in `solvalou`, `blaster`, `area_01a`, `area_01b`, `start_screen`, `solv_death`, `target_a`, `target_b`, and `bomb`; deterministic generation in `tools/game_director.py`; structural checks in `tests/test_scratch_project.py`.
- Acceptance criteria: Green flag from every state produces the same title; Space performs one title-to-ready-to-playing sequence; READY holds one second; D performs one 0.7-second death animation and one respawn READY; G performs one death animation, a two-second GAME OVER hold, and cold-title return; gameplay input is inert outside playing; new-life reset preserves terrain while cold-start/new-game rewind it; old scripts, sounds, and clones cannot act after transition cleanup.
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
