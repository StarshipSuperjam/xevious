# Fidelity audit — current build vs the product spec (2026-08-09)

Two-directional audit of the built Scratch project against the product spec under `docs/spec/` and the
preserved 2017 baseline (`assets/original/Xevious.sb3`, structural record `docs/mechanics/000`).
Direction A hunts behavior present with no basis; Direction B hunts behavior the baseline or a merged
slice had that the current build lost. Every finding names its evidence in `tools/game_director.py`
(verified in sync with the committed `src/xevious/project.json`) or the baseline block graph.

**Method calibration: 5/5.** The audit's method independently re-derived all five known post-PR-#9
defects from the artifacts alone before anything else was counted — the READY bubble, the held-fire
stutter, the bomb overlap, the terrain blackout, and the lost title glide. A sweep that could not re-find
the known defects would have been reported as a broken method, not a clean build.

**Honest limits.** The game was not run: runtime claims rest on documented Scratch VM semantics —
key-press hats firing only on browser keydown (B1, B2), and sprite fencing clamping full-height sprites
(B3, B8). The operator's playtest remains the only real gameplay confirmation. Baseline scripts were read
completely; layer-art opacity (B9) and audio content (B5) were not rendered or listened to.

## Findings

| ID | Dir | Class | Finding | Evidence | Disposition |
| --- | --- | --- | --- | --- | --- |
| A1 | A | invention | READY speech bubble on the craft, gating the ready→playing transition; locked in by a test | `game_director.py:557-570`; `tests/test_scratch_project.py:936-937` | Recovery build removes it (and the asserting test rows) |
| A2 | A | invention | GAME OVER speech bubble on the explosion sprite — same class as A1 but previously unrecorded | `game_director.py:668-671`; test `:966-967` | Recovery build removes it |
| A3 | A | invention | No player-shot cap — unlimited simultaneous shots vs the spec's binding 3-slot limit (baseline also had no cap; not a regression) | `game_director.py:729-733` | Partial-build marker recorded here; fixed by the entity-pool slice |
| A4 | A | unmarked | Movement unit conversion (7 stage units/frame) and collision-based bounds are genuine port necessities but carried no recorded mapping | `game_director.py:577-610` | Marker added via this audit; record the conversion with the movement slice |
| A5 | A | unmarked | Transient `resetting` state was absent from the spec's state machine (marked only in mechanics record 003) | `game_director.py:364` | Spec amended (core-game-systems now records it) — closed |
| A6 | A | unmarked | `D`/`G` debug death keys accepted in playing, absent from the spec's input rules | `game_director.py:454-458` | Spec amended (fixtures named with removal condition) — closed |
| B1 | B | regression | Held-fire cadence replaced by a key-press hat: one shot, OS-repeat stutter, silenced entirely when an arrow key is pressed. Baseline polled with a steady 0.2 s loop; arcade rule is immediate shot then 20-frame reload | `game_director.py:729-733` vs baseline blaster loop | Recovery build restores polled fire with reload counter |
| B2 | B | regression | Single-active-bomb lockout removed — every keydown clones a new bomb. Baseline was a single sprite with a 0.75 s cooldown; arcade arms one bomb at a time | `game_director.py:749-753` | Recovery build restores the lockout |
| B3 | B | regression | Terrain wrap unreachable: strips are position-threshold wrapped at `y < −345`, but Scratch fencing pins a full-height sprite at exactly −345, so both strips park and the screen goes black (~11 s and ~23 s in), and every later respawn resumes on black. Baseline used a counted cycle that always fired | `game_director.py:709-718`; costume geometry from the archive | Recovery build restores counted-cycle wrapping; position-threshold wrap recorded as unsafe under fencing |
| B4 | B | regression | Title-logo glide (baseline: enter at top, 1 s glide to center) replaced by immediate center placement; record 003's claim of retained title behavior is false | `game_director.py:627` | Recovery build restores the glide; record 003 corrected |
| B5 | B | regression | Death cue truncated: the transition's stop-handlers halt all sounds ~0.7 s in, on every death; the baseline never stopped sounds and always let the cue finish | `game_director.py:366,524,653-659` | Recovery build; interacts with B10 |
| B6 | B | regression | Crosshair bomb-release animation lost — the `bomb` broadcast no longer exists and the reticle is frozen on one costume | `game_director.py:28-39,769-826` | Recovery build |
| B7 | B | regression | Bomb ground-impact marker (`target_b`) stripped to an inert hide-only sprite; baseline dropped a visible marker from the crosshair | `game_director.py:824-825` | Recovery build |
| B8 | B | regression | Shots no longer expire at the top: fenced at the edge they sit visible and animating ~0.43 s before deletion; travel speed also halved and animation slowed | `game_director.py:735-740` vs baseline edge-touch loop | Recovery build |
| B9 | B | regression | Per-frame layer management removed; saved layer order leaves shots under most sprites and the frame borders above the ship | absence throughout `game_director.py`; baseline front-layer calls | Recovery build (or a recorded deliberate layer-order substitution) |
| B10 | B | regression | Post-death pause removed (baseline: 3 s; arcade: 32 frames after the 56-frame explosion); build goes straight to respawn READY | `game_director.py:651-664` | Recovery build; restoring it also lets B5's cue finish |
| B11 | B | fixture, marked | Terrain preserved across death — properly recorded in the spec (three places) as an interim divergence; but the records did not disclose that the baseline *did* restart terrain on death, matching the arcade | `game_director.py:697-702`; baseline death broadcast | Record 003 corrected to disclose the displaced baseline behavior — closed |

Spec defects found while auditing, both fixed in this same change: the GAME OVER duration had two
conflicting normative homes (core-game-systems wrongly called it reference-less; the scoring document
owns its 128-frame value), and the index's incident account undercounted PR #9's damage (four removals;
the true count is ten — now stated).

## Counts

Unsupported inventions 3 (A1–A3) · legitimate-but-unmarked 4 (A4–A6, B11; all four now closed by spec
amendment or this record) · regressions 10 (B1–B10). The regression-recovery build — the first tracked
build item once the spec settles — owes B1–B10 plus A1–A2, and supersedes the abandoned
`codex/slice2-regression-recovery` branch.

## Disposition (2026-08-09, issue #13)

B1–B10 and A1–A2 are discharged by the regression-recovery build (issue #13): each restored per its
marker and the tick conversion recorded in
[game director mechanics record 003](../mechanics/003-game-director-and-state-reset.md), each removed
invention taken out with its asserting test rows. A3 (3-shot cap) and B11 (terrain-restart-on-death)
remain owed to the entity-pool and life-economy slices respectively, as recorded above.
