// The scenario catalog IS the coverage checklist. Each entry maps a previously-regressed,
// VM-observable behavior to one scenario, and carries the mutation that makes THIS
// scenario's assertion go red. `drive` runs the same way for the positive (real build)
// and negative (mutated build) cases; the runner asserts the positive passes and the
// negative fails, so an assertion that does not actually bite is caught.
//
// Behaviors that are NOT VM-observable (rendered collision, the shot's touching-frame
// replenish, the bomb's flight duration, visuals/audio/feel) are deliberately excluded
// and listed in EXCLUSIONS — they remain the operator playtest's job.
import assert from 'node:assert/strict';
import {
  step,
  keyDown,
  keyUp,
  tapKey,
  readVar,
  cloneCount,
  cloneReports,
  constants,
  variable,
} from './harness.js';
import { reachPlaying, stateOf } from './build.js';
import * as mutate from './mutate.js';

// Every read resolves through a manifest id (hard-errors on a rename), including the
// scope-duplicated ones: `terrain-scroll-step-a` is area_01a's, distinct from area_01b's.
const state = stateOf;
const epoch = (vm) => readVar(vm, 'game-director-epoch');
const outcome = (vm) => readVar(vm, 'game-director-death-outcome');
const bombInFlight = (vm) => readVar(vm, 'weapon-bomb-in-flight');
const scrollA = (vm) => readVar(vm, 'terrain-scroll-step-a');
const shotSlotTypes = (vm) => readVar(vm, 'slot-type').slice(36, 39);

export const SCENARIOS = [
  {
    key: 'shot-cap-ceiling',
    behavior: 'Held fire never puts more than the 3-shot ceiling on the field',
    playtestStep: 2,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      keyDown(vm, ' ');
      let maxClones = 0;
      for (let i = 0; i < 15; i += 1) {
        step(vm, 1);
        maxClones = Math.max(maxClones, cloneCount(vm, 'blaster'));
      }
      keyUp(vm, ' ');
      return { maxClones, shotSlots: shotSlotTypes(vm) };
    },
    assert(obs) {
      assert.equal(obs.maxClones, constants.shot_slot_count, 'shots on field hit the ceiling');
      assert.deepEqual(obs.shotSlots, [1, 1, 1], 'all three shot slots become active');
    },
    // Break the alloc gate (alloc result > 0) so no shot ever spawns → ceiling assertion fails.
    negativeMutation: (p) => mutate.raiseGreaterThreshold(p, 'blaster', 0, 99999),
  },
  {
    key: 'bomb-arm-gated',
    behavior: 'A bomb press during play arms a bomb through the one-bomb guard',
    playtestStep: 3,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      keyDown(vm, 'b');
      let armedSeen = false;
      for (let i = 0; i < 6; i += 1) {
        step(vm, 1);
        if (bombInFlight(vm) === 1) armedSeen = true;
      }
      keyUp(vm, 'b');
      return { armedSeen };
    },
    assert(obs) {
      assert.equal(obs.armedSeen, true, 'pressing b arms the bomb');
    },
    // Break the arm guard (bomb in flight == 0) so a press never arms → assertion fails.
    negativeMutation: (p) => mutate.changeEqualsOperand(p, 'bomb', 0, 99),
  },
  {
    key: 'terrain-wrap',
    behavior: 'The terrain scroll counter advances and wraps on its counted cycle',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Each pump advances the counter by hundreds, so a handful covers several full cycles.
      let prev = scrollA(vm);
      let increased = false;
      let wrapped = false;
      for (let i = 0; i < 40; i += 1) {
        step(vm, 1);
        const v = scrollA(vm);
        if (v > prev) increased = true;
        if (v < prev) wrapped = true;
        prev = v;
      }
      return { increased, wrapped };
    },
    assert(obs) {
      assert.equal(obs.increased, true, 'scroll counter advances while playing');
      assert.equal(obs.wrapped, true, 'scroll counter wraps on its cycle');
    },
    // Freeze the counter so it never advances or wraps → assertion fails.
    negativeMutation: (p) => mutate.freezeVariableChange(p, 'area_01a', 'scroll step'),
  },
  {
    key: 'start-and-input-gating',
    behavior: 'Green flag rests in title, input is gated there, and start reaches playing',
    playtestStep: 1,
    async drive(vm) {
      vm.greenFlag();
      step(vm, 1);
      const titleState = state(vm);
      const epochBefore = epoch(vm);
      tapKey(vm, 'd'); // gameplay keys must do nothing at title
      tapKey(vm, 'b');
      step(vm, 3);
      const gatedHeld = state(vm) === 'title' && epoch(vm) === epochBefore;
      tapKey(vm, ' ');
      let reached = false;
      for (let i = 0; i < 120 && !reached; i += 1) {
        step(vm, 1);
        if (state(vm) === 'playing') reached = true;
      }
      return { titleState, gatedHeld, reached };
    },
    assert(obs) {
      assert.equal(obs.titleState, 'title', 'green flag rests in title');
      assert.equal(obs.gatedHeld, true, 'gameplay input is ignored at title');
      assert.equal(obs.reached, true, 'pressing start reaches playing');
    },
    // Remove title -> ready so start can never reach playing → assertion fails.
    negativeMutation: (p) => mutate.removeAllowedTransition(p, 'title -> ready'),
  },
  {
    key: 'death-respawn',
    behavior: 'A death during play runs death -> respawn and returns to playing',
    playtestStep: 5,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const epoch0 = epoch(vm);
      tapKey(vm, 'd');
      for (let i = 0; i < 20; i += 1) step(vm, 1);
      return { outcome: outcome(vm), finalState: state(vm), epochDelta: epoch(vm) - epoch0 };
    },
    assert(obs) {
      assert.equal(obs.outcome, 'respawn', 'a d-death sets the respawn outcome');
      assert.equal(obs.finalState, 'playing', 'the craft respawns back into play');
      assert.ok(obs.epochDelta >= 2, 'the death and respawn each advance the state epoch');
    },
    // Remove player-dead -> respawning so the craft cannot return to play → assertion fails.
    negativeMutation: (p) => mutate.removeAllowedTransition(p, 'player-dead -> respawning'),
  },
  {
    key: 'death-game-over',
    behavior: 'A terminal death runs death -> game-over and returns to title',
    playtestStep: 5,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      tapKey(vm, 'g');
      let reachedTitle = false;
      for (let i = 0; i < 80 && !reachedTitle; i += 1) {
        step(vm, 1);
        if (state(vm) === 'title') reachedTitle = true;
      }
      return { reachedTitle };
    },
    assert(obs) {
      assert.equal(obs.reachedTitle, true, 'game over returns to the title screen');
    },
    // Remove player-dead -> game-over so it cannot reach title → assertion fails.
    negativeMutation: (p) => mutate.removeAllowedTransition(p, 'player-dead -> game-over'),
  },
  {
    key: 'score-digits-render',
    behavior:
      'Score and high-score HUD digit clones display the running values as digit costumes, not a stuck glyph',
    playtestStep: 6,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // S is the scoring fixture: each press awards a fixed value from the master table.
      for (let i = 0; i < 3; i += 1) tapKey(vm, 's');
      step(vm, 20);
      const roleName = variable('hud-role').name;
      const placeName = variable('hud-place').name;
      // Decode each 7-place digit-clone group (score, high score) from its costumes:
      // most-significant place first, e.g. costumes 0,0,3,0,0,0,0 (place 6..0) → 30000.
      const PLACES = 7;
      const byRole = new Map();
      for (const r of cloneReports(vm, 'hud', [roleName, placeName])) {
        const m = /^digit\/([0-9])$/.exec(r.costume || '');
        if (!m) continue;
        const role = r.vars[roleName];
        if (!byRole.has(role)) byRole.set(role, new Map());
        byRole.get(role).set(r.vars[placeName], Number(m[1]));
      }
      const decoded = [];
      for (const places of byRole.values()) {
        if (places.size !== PLACES) continue; // skip the lone digit in the 1UP label
        let value = 0;
        for (let place = PLACES - 1; place >= 0; place -= 1) value = value * 10 + (places.get(place) ?? 0);
        decoded.push(value);
      }
      return {
        score: readVar(vm, 'eco-score'),
        high: readVar(vm, 'eco-high-score'),
        decoded: decoded.sort((a, b) => a - b),
      };
    },
    assert(obs) {
      assert.ok(obs.score > 0, 'the S fixture raised the score above zero');
      assert.deepEqual(
        obs.decoded,
        [obs.score, obs.high].sort((a, b) => a - b),
        'the two 7-digit HUD groups decode to the live score and high score',
      );
    },
    // Break floor() so every digit becomes floor(...)=0 → all digit/0, decoding to 0 (≠ score).
    negativeMutation: (p) => mutate.misnameMathopOperator(p, 'hud'),
  },
];

// VM-cannot-observe behaviors that stay the operator playtest's job, named so "complete"
// is honest: the net covers the logic layer of these areas, never the on-screen result.
export const EXCLUSIONS = [
  "A shot freeing its slot on reaching the top frame (touching-frame collision — headless can't see it)",
  'The bomb flight/explosion duration and true concurrent lockout (timing collapses headless)',
  'Collision-driven death from an enemy or bullet (rendered collision)',
  "Sprite visibility, layering, a costume's rendered pixels, audio, and overall feel (the digit " +
    'scenario observes WHICH costume a clone switches to — deterministic state — never how it looks)',
];
