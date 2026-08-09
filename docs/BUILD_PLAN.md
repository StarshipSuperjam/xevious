# Xevious end-to-end build plan

## Outcome

Turn the preserved 2017 Scratch proof of concept into a reproducible Scratch
interpretation of the normal Namco arcade release of Xevious.

The finished project must:

- preserve the original archive and deterministic build boundary;
- implement the player-visible mechanics in
  [the mechanics catalog](MECHANICS_CATALOG.md);
- use normal-game data, not Super Xevious content;
- complete area 16 by returning to area 7 instead of showing a win screen;
- record exact reference provenance and the Scratch interpretation for every
  behavior change; and
- run in Scratch 3 without arcade ROM files or runtime access to the reference
  repository.

## Settled boundaries

The normal Namco arcade game is authoritative. The implementation may use
mechanics, constants, scores, timing, formations, schedules, collision rules,
and tables published in
[`jotd666/xevious`](https://github.com/jotd666/xevious) at commit
[`71473685a8c7856c8401c8519276cd97a38d4183`](https://github.com/jotd666/xevious/tree/71473685a8c7856c8401c8519276cd97a38d4183).
Each use cites the commit, file, and source label. Scratch blocks and data
structures are written for this project; assembly text is not copied.

Arcade ROM acquisition, opening, extraction, and redistribution are outside
the project. Arcade observation is selective fidelity QA for ambiguities,
known reference bugs, normal-versus-Super branches, and port-only additions;
it is not a second provenance prerequisite for every mechanic.

Use the reference's `*_normal` tables and normal branches. Exclude the
Super-only Galaxian, jet, helicopter, tank, bridge, Super schedules, and
score-reset traps.

The five imported sprite sheets remain unchanged and retain their existing
provenance. Gameplay-ready derivatives follow
[the sprite extraction design](SPRITE_EXTRACTION.md). Media provenance and
mechanics provenance are separate controls.

## Current baseline

The canonical Scratch project already provides a title screen, Space-key
start, bounded Solvalou movement, looping area-1 terrain, blaster and bomb
animations, music, and a manual `D` death/restart demonstration.

It does not yet provide enemies, combat resolution, scoring, a life economy,
area scheduling or progression, difficulty, secrets, Andor Genesis, cabinet
flow, or a completed game loop. Existing mechanics are retained where
possible but remain partial until they meet a catalog acceptance criterion.

## Architecture seams

The architecture — the game director, area director, entity pool, collision groups, data tables,
presentation separation, and determinism seams — is normatively described in the
[architecture overview](architecture.md); this plan sequences work against those seams without restating
them.

The first entity change includes a measured clone/performance spike. If one
shared target is not maintainable, use one target per behavior family while
preserving the lifecycle and collision interfaces.

## Pull-request build order

Every slice extracts only the costumes it needs and updates its catalog rows
plus a `docs/mechanics/` record. A slice closes only when its declared
behavior, provenance, automated validation, deterministic build, and
operator-runnable Scratch check agree.

| Slice | Scope and dependency | Acceptance gate | Scratch check and mechanics record |
| --- | --- | --- | --- |
| 1. Sprite extraction proof — complete | Deterministic manifest extraction, source hashes, matte removal, canvases, anchors, provenance, and Solvalou/Toroid proof costumes. Depends on the preserved archive boundary. | Two runs are byte-identical; sheets remain byte-identical; crops avoid labels and credits; generated source verifies. | Inspect the contact sheet and animation anchors. Record `002-sprite-extraction-proof.md`. |
| 2. Game director and state reset — current | Stage-owned title, ready, playing, player-dead, respawning, and game-over states; serialized reset scopes and cancellation. Depends on slice 1 only for the current canonical source. | Only Stage writes director variables; every normal state change uses one transition procedure; cleanup finishes before entry; reset handlers terminate; obsolete `begin`/`death` control is absent. | Exercise the state/input/reset matrix below. Record `003-game-director-and-state-reset.md` and mark SYS-01 present. |
| 3. Entity pool and collision foundation | Shared clone lifecycle, collision groups, single-hit resolution, off-screen cleanup, and air/ground/bullet/effect fixtures. Depends on SYS-01. | Repeated worst-case spawn/removal leaks no state; groups cannot cross-hit or double-resolve; measured clone load stays responsive. | Run the clone spike and each collision fixture in Scratch 3. Record SYS-02 and SYS-03. |
| 4. Score, HUD, lives, death, and respawn | Score/high-score cap, object awards, HUD, lives, bonus thresholds, collision death, safe respawn, and game over. Depends on slices 2–3. | Awards occur once; HUD matches state; life loss, bonus awards, respawn, and last-life game over repeat deterministically. | Run score/life fixtures through ordinary and last-life deaths. Record ECO-01–04 and PLY-02. |
| 5. Area clock and scheduler foundation | Monotonic terrain position, table representation, ordered event dispatch, area boundary seam, and one small schedule fixture. Depends on slices 2–3. | Position never rewinds during a life; fixture events fire once in order; transitions leave no old-area work. | Accelerate and pause the fixture around its boundaries. Record AREA-01 and AREA-02 foundation. |
| 6. All normal area schedules and 1–16→7 trace | Import normal object schedules for all areas and the normal loop, using the slice-5 interfaces. Depends on slice 5. | Accelerated trace visits 1–16 then 7; no Super-only table, row, or unknown object is present. | Observe transition checkpoints and the 16→7 return. Record AREA-03 and AREA-04. |
| 7. Shared RNG, difficulty, and formations | One advancing pseudo-random stream, normal settings, adaptive difficulty seam, fire-frequency interfaces, and normal formation data. Depends on slices 4–6. | Seeded runs repeat; consumers share one stream; difficulty fixtures order correctly; formations preserve normal type/count/offset/order. | Replay seeded formation and pressure fixtures. Record SYS-04, DIF-01–03, and FORM-01. |
| 8. Toroid vertical slice | Toroid formation, movement, animation, optional shot, bullets, blaster collision, score, player collision, explosion, and cleanup. Depends on slices 3–4 and 7. | A normal encounter completes through exit, score, or player death without test-only intervention. | Play both firing and non-firing seeded encounters. Record AIR-01 and relevant AIR-12 behavior. |
| 9. Barra/Logram vertical slice | Placement, scrolling, reticle targeting, bomb travel/impact, reactions, scores, ground fire, and cleanup. Depends on slices 3–7. | Both normal encounters can be targeted, bombed, scored, survived, or lost end to end. | Play seeded Barra and Logram encounters. Record GND-01, GND-03, and completed WPN-03–05 behavior. |
| 10. Torkan, Zoshi, Jara, Kapi, and Terazzi | First flying-family roster using the stable entity, formation, RNG, and difficulty interfaces. Depends on slice 8. | Each family fixture completes its distinct movement/fire/hit/exit path without new lifecycle seams. | Play one seeded fixture per family. Record AIR-02–06. |
| 11. Zakato families, Sheonite, Spario, and Bacura | Remaining normal flying families and special paired/projectile/collision behavior. Depends on slices 8 and 10. | Variants stay distinct; paired entities leave no orphan; Bacura collision/resistance is isolated; full air fixture has no unknown type. | Play each family plus a mixed air-pressure fixture. Record AIR-07–11 and finish AIR-12. |
| 12. Barra families, Zolbak, Logram, and Derota | First broader ground roster plus Zolbak difficulty effect. Depends on slice 9. | Variants react, fire, score, and clean up distinctly; Zolbak applies its effect once. | Play one seeded fixture per family and difficulty effect. Record GND-01–04. |
| 13. Boza Logram, Grobda, and Domogram | Composite, land/water, targeting, patrol, fire, score, and cleanup variants. Depends on slice 12. | Composite parts coordinate; Grobda variants remain distinct; patrols and cleanup leave no stale actor. | Play composite and representative land/water/patrol fixtures. Record GND-05–07. |
| 14. Sol Towers, Bonus Flags, and confirmed hidden events | Normal secret triggers, staged reveals, rewards, and only arcade-confirmed hidden presentation. Depends on slices 4, 6, 9, and 12. | Secrets reveal only under recorded conditions and award once; unconfirmed behavior remains visibly uncertain, not guessed. | Trigger, miss, and repeat each secret fixture. Record SEC-01–03. |
| 15. Andor lifecycle, parts, arrival, and departure | Composite allocation, alignment, animation, boss window, and non-destruction departure. Depends on slices 3, 6–7, and 11–13. | Parts arrive and remain synchronized, fit the clone envelope, and depart/clean up as one encounter. | Observe arrival, sustained encounter, and timeout departure. Record BOSS-01. |
| 16. Andor combat, defenses, core, and destruction | Gun ports, Bragza, fire, valid core path, score, destruction, and cleanup. Depends on slices 9, 11, and 15. | Invalid hits do not destroy the boss; valid core sequence destroys/scores once; survival and departure remain valid. | Play destruction, survival, and timeout paths. Record BOSS-02 and BOSS-03. |
| 17. Attract, credits, and one-player cabinet flow | Attract sequence, credit cap/input, 1P start, and return flow. Depends on slices 2, 4, 6, and representative combat. | Cold start cycles attract without mutating score/lives; credits start exactly one affordable 1P game. | Observe a full attract cycle and credit/start/game-over return. Record CAB-01 and 1P CAB-02. |
| 18. Two-player alternation and independent state | 2P selection, player switching, and independent score/lives/area/bonus state. Depends on slices 4, 6, and 17. | Alternation occurs at the proper boundary and neither player's state leaks into the other. | Play asymmetric two-player deaths and returns. Record CAB-03 and finish CAB-02. |
| 19. High-score table and initials | Five-entry ranking, insertion, initials entry, and cabinet return; persistent storage remains excluded. Depends on slices 4 and 17–18. | Qualifying scores insert at the correct rank; non-qualifying scores skip entry; initials and return complete. | Test top, middle, bottom, tie, and non-qualifying scores. Record CAB-04. |
| 20. Audio, animation, presentation, and fidelity | Encounter cues, score/life feedback, warnings, transitions, animation timing, anchors, state-bound sound fit, and accepted fidelity corrections. Depends on all implemented gameplay slices. | Every cue has a state-safe trigger and fits its state window without an unintended cutoff; animations remain aligned; catalog deviations and uncertainties are explicit. | Run a representative full presentation checklist, including the slice-2 death cue cutoff. Record CAB-05 and updated affected mechanics. |
| 21. Full soak and release audit | Accelerated 1–16→7 runs, clone/performance soak, complete Scratch 3 acceptance, provenance/license audit, and release documentation. Depends on slices 1–20. | All deterministic checks pass; catalog rows are present, excluded, or accepted deviations; no ROM or unprovenanced media exists; performance envelope holds. | Run title-to-game-over, both players/weapons, representative roster, secrets, Andor, high scores, area loop, and stop/reload checks. Record the release audit and any final mechanics corrections. |

## Slice 2 director and reset contract

The director's transition contract — allowed state edges, reset scopes and their postconditions, and the
per-state input rules — is normatively owned by the product spec's
[core game systems document](spec/core-game-systems.md); the presentation timings around those states are
project-defined placeholders recorded there. This plan sequences the work; it no longer restates the
contract.

## Per-slice provenance workflow

1. Select catalog rows and pin the reference commit.
2. Read the cited labels and tables.
3. Describe the mechanic, including exact constants or structured data used.
4. Classify inputs as behavior, numeric constant, structured table/schedule,
   or media.
5. Implement original Scratch blocks and names.
6. Record Scratch targets, broadcasts, variables, lists, and fixtures.
7. Use arcade observation only for ambiguity, known bugs, target splits,
   port-only behavior, or final fidelity.
8. Run project checks and the slice's Scratch acceptance steps.

## Risks kept visible

- The reference says one gameplay bug remains to be confirmed. Records must
  distinguish repo-derived from arcade-confirmed behavior.
- The pinned reference states no reusable license. Provenance is not
  permission or legal clearance.
- Scratch has a finite clone budget and cooperative scheduling. The entity
  spike and accelerated schedule soak precede roster expansion.
- Supplied media also has no reusable license stated. Broader distribution or
  promotion still needs a rights review.
- Exact crop rectangles, anchors, timing, hitboxes, scores, and schedules are
  recorded when parsed, not guessed in this plan.

## Continuing the roadmap

After each slice PR is ready to merge, report the next planned slice briefly.
Do not replan the project between slices unless implementation exposes a real
dependency, reference ambiguity, normal/Super separation problem, Scratch
performance limit, or unacceptable asset provenance.
