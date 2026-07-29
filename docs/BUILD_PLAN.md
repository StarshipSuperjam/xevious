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

- **Game director:** one explicit state owns title, attract, ready, playing,
  player-dead, respawning, game-over, high-score entry, and player change.
- **Area director:** one monotonic area clock owns terrain position, scheduled
  objects, formations, boss windows, transitions, and the area 16-to-7 loop.
- **Entity pool:** clone-based targets share spawn, update, hit, explosion,
  and removal rules while staying below Scratch's clone limit.
- **Collision groups:** blaster-to-air, bomb-to-ground,
  enemy-bullet-to-player, flying-enemy-to-player, and Bacura-to-player remain
  distinct. A hit is resolved once and cannot score twice.
- **Data tables:** schedules, formations, scores, timing, and difficulty live
  as inspectable Scratch lists or generated Scratch data, separate from
  scripts.
- **Presentation:** costumes, animation timing, anchors, sounds, and HUD
  rendering remain separate from entity rules.
- **Determinism:** generated assets and generated Scratch edits are
  reproducible from committed inputs.

The first entity change includes a measured clone/performance spike. If one
shared target is not maintainable, use one target per behavior family while
preserving the lifecycle and collision interfaces.

## Pull-request build order

Every slice updates its catalog rows and adds or changes a
`docs/mechanics/` record. It is complete only when behavior, provenance,
automatic validation, and the operator-runnable Scratch check agree.

### 1. Sprite extraction proof

Build a deterministic manifest-driven extractor with source-hash validation,
edge-connected matte removal, stable canvases and anchors, derivative
provenance, and a contact sheet. Prove it with Solvalou and Toroid.

Gate: two runs produce identical outputs; source sheets stay byte-identical;
derivatives contain no labels or credit panels; animations do not jump; and
`scratch_project.py verify` accepts the overlay.

### 2. Game director and state reset

Add explicit title, ready, playing, player-dead, respawning, and game-over
states plus one reset path for green flag, new game, new life, and game over.

Gate: transitions happen once, scripts from the old state stop acting, and
green flag always returns to the same title state.

### 3. Entity lifecycle and collision foundation

Add pooled clones, collision groups, single-hit resolution, off-screen
cleanup, and fixtures for an air target, ground target, bullet, and effect.

Gate: repeated spawn/removal does not leak state, collision groups cannot
cross-hit, and the worst scheduled fixture stays responsive in Scratch 3.

### 4. Score, HUD, lives, death, and respawn

Add score, high score, score cap, object values, HUD, starting lives,
bonus-life thresholds, Bonus Flag life behavior, collision death, safe
respawn, and game over.

Gate: fixture objects award exactly once; life loss, awards, respawn, and game
over repeat correctly; HUD values always match internal state.

### 5. Area clock, normal schedules, and transitions

Parse the normal object and formation tables for all 16 areas, bind them to
terrain position, and add transitions plus the area 16-to-7 loop.

Gate: an accelerated trace visits 1–16 then 7; a schedule fixture preserves
event order; no Super-only row or object appears.

### 6. Air-combat vertical slice

Deliver a complete Toroid encounter: formation spawn, movement, optional
fire, enemy bullets, blaster hits, score, explosion, player collision, and
cleanup.

Gate: the normal encounter plays from spawn through score or player death
without test-only intervention.

### 7. Ground-combat vertical slice

Deliver Barra and Logram placement, scrolling, crosshair targeting, bomb
travel and impact, object reactions, score, ground fire, and cleanup.

Gate: a normal encounter can be targeted, bombed, scored, and survived or
lost end to end.

### 8. Difficulty, formations, and remaining roster

Implement the cataloged flying and ground families, adaptive AI, normal
formations, fire masks, pseudo-random behavior, and score values.

Gate: fixtures cover every normal enemy type and difficulty step; an
accelerated full schedule creates no unknown type.

### 9. Secrets and Andor Genesis

Implement Sol Towers, Bonus Flags, Zolbak effects, the normal hidden copyright
event if confirmed, and Andor's parts, gun ports, core, Bragza defenses,
destruction, and departure.

Gate: secrets reveal only under recorded conditions; Andor can be survived,
destroyed through its valid target path, or allowed to leave.

### 10. Cabinet flow and two-player game

Implement attract mode, credits, 1P/2P start, player switching, game-over
screens, the five-entry high-score table, and initials. Pause and persistent
high-score storage remain excluded port additions.

Gate: attract mode cycles from a cold start; credits select the right mode; 2P
state remains independent; a qualifying score enters the table.

### 11. Audio, polish, and release validation

Complete encounter cues, score/life feedback, warnings, transitions,
animations, and final fidelity corrections.

Gate: all deterministic checks pass; each catalog row is complete, excluded,
or an accepted deviation; areas 1–16 and the loop pass an accelerated soak;
Scratch 3 covers title-to-game-over, both weapons, representative enemies,
secrets, Andor, and 2P; no ROM file or unprovenanced media is present.

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

## Starting the next phase

After this plan PR merges, the next PR is **Sprite extraction proof**. Nothing
else is needed from the operator before it starts. A later slice stops for a
new decision only if the reference is ambiguous, normal and Super cannot be
separated confidently, Scratch cannot meet the performance envelope, or an
asset lacks acceptable provenance.
