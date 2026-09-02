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
import { readFileSync } from 'node:fs';
import {
  step,
  keyDown,
  keyUp,
  tapKey,
  readVar,
  writeVar,
  fireBroadcast,
  cloneCount,
  cloneReports,
  constants,
  variable,
} from './harness.js';
import { reachPlaying, stateOf } from './build.js';
import * as mutate from './mutate.js';

// The committed RNG fixture (the shared LFSR's byte stream from each seed) — the model the live
// draw order is checked against, read from the same file the Python model fixtures pin so the two
// can never drift. Slot 59..64 are the six flying slots (list index 58..63) the Toroid wave fills.
const RNG_FIXTURES = JSON.parse(
  readFileSync(new URL('../../docs/spec/data/rng.json', import.meta.url)),
).generator.fixture_sequences;
const FLYING_SLOT_INDICES = [58, 59, 60, 61, 62, 63];

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
  {
    key: 'area-clock-scheduler',
    behavior:
      'The area clock advances a monotonic position, completes areas (advancing the area number), and the schedule consumes records once each in order',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // `area progress` is a sawtooth (it climbs within an area, then resets at completion), so
      // assert on pacing-invariant facts: it is SEEN to advance, the area number advances as
      // areas complete, `schedule fired` climbs, and WITHIN an area it never decreases (records
      // fire once, in order) — it only resets at a boundary, where the area number also changes.
      let progressAdvanced = false;
      let firedSeen = 0;
      let areaAdvances = 0;
      let firedMonotonicWithinArea = true;
      let prevProgress = readVar(vm, 'area-progress');
      let prevArea = readVar(vm, 'area-number');
      let prevFired = readVar(vm, 'area-schedule-fired');
      for (let i = 0; i < 80; i += 1) {
        step(vm, 1);
        const progress = readVar(vm, 'area-progress');
        const area = readVar(vm, 'area-number');
        const fired = readVar(vm, 'area-schedule-fired');
        if (progress > prevProgress) progressAdvanced = true;
        if (area !== prevArea) areaAdvances += 1;
        else if (fired < prevFired) firedMonotonicWithinArea = false;
        firedSeen = Math.max(firedSeen, fired);
        prevProgress = progress;
        prevArea = area;
        prevFired = fired;
      }
      return { progressAdvanced, firedSeen, areaAdvances, firedMonotonicWithinArea };
    },
    assert(obs) {
      assert.equal(obs.progressAdvanced, true, 'area progress advances while playing');
      assert.ok(obs.areaAdvances >= 1, 'the area number advances as areas complete');
      assert.ok(obs.firedSeen >= 1, 'the schedule consumes records (schedule fired climbs)');
      assert.equal(
        obs.firedMonotonicWithinArea,
        true,
        'within an area, records fire once (schedule fired never decreases except at a boundary)',
      );
    },
    // Freeze the area clock so the monotonic position never advances → progress1 === progress0.
    negativeMutation: (p) => mutate.freezeVariableChange(p, 'Stage', 'area progress'),
  },
  {
    key: 'near-end-checkpoint',
    behavior:
      'A new-life death advances the area when the frozen scroll row is in the near-end window [0x0E,0x43], else restarts it — and area 16 in-window wraps to 7',
    playtestStep: 5,
    async drive(vm) {
      // The live death->respawn sequence completes within a single headless pump, so it cannot be
      // paused to inject a frozen row. Instead drive `area_reset` in isolation: green-flag to a
      // settled state, inject the new-life scope + a chosen area number + a chosen frozen scroll
      // row, fire `director reset`, and read the resulting area number — exactly the death-tick
      // checkpoint decision, at every boundary.
      const trial = (row, area) => {
        vm.greenFlag();
        step(vm, 2);
        writeVar(vm, 'game-director-reset-scope', 'new-life');
        writeVar(vm, 'area-number', area);
        writeVar(vm, 'area-scroll-row', row);
        fireBroadcast(vm, 'director reset');
        step(vm, 1);
        return readVar(vm, 'area-number');
      };
      return {
        low: trial(14, 5), // 0x0E — window low edge
        mid: trial(40, 5),
        high: trial(67, 5), // 0x43 — window high edge
        belowTop: trial(13, 5), // area-top row, below the window
        aboveWindow: trial(68, 5), // just above 0x43
        wrap16: trial(40, 16), // in-window death in area 16
      };
    },
    assert(obs) {
      assert.equal(obs.low, 6, 'a death at row 14 (window low edge) advances the area');
      assert.equal(obs.mid, 6, 'a death at row 40 advances the area');
      assert.equal(obs.high, 6, 'a death at row 67 (window high edge) advances the area');
      assert.equal(obs.belowTop, 5, 'a death at row 13 restarts (holds the area)');
      assert.equal(obs.aboveWindow, 5, 'a death at row 68 restarts (holds the area)');
      assert.equal(obs.wrap16, 7, 'an in-window death in area 16 wraps to area 7');
    },
    // Raise the window's lower bound (row > 13) out of reach, so no death is ever near-end and the
    // in-window advances never happen → the advance assertions fail.
    negativeMutation: (p) => mutate.raiseGreaterThreshold(p, 'Stage', 13, 999),
  },
  {
    key: 'difficulty-and-formations',
    behavior:
      'The area schedule raises the AI level (folding back below 0x80) and selects a valid flying formation live',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Live pacing (like area-clock-scheduler): as raise / set-formation records fire, `ai level`
      // climbs and `formation count` takes a formation-table value. The AI level starts at 0 on a
      // new game and must never reach 0x80 — a raise folds it back first. The EXACT (count, offset)
      // table correspondence is the model fixture's job (test_spec_docs); this proves it runs live.
      let aiRose = false;
      let maxAi = 0;
      let formationSelections = 0;
      let countInRange = true;
      for (let i = 0; i < 140; i += 1) {
        step(vm, 1);
        const ai = readVar(vm, 'difficulty-ai-level');
        const count = readVar(vm, 'formation-count');
        if (ai > 0) aiRose = true;
        maxAi = Math.max(maxAi, ai);
        if (count > 0) {
          formationSelections += 1;
          if (count < 1 || count > 6) countInRange = false;
        }
      }
      return { aiRose, maxAi, formationSelections, countInRange };
    },
    assert(obs) {
      assert.equal(obs.aiRose, true, 'the AI level climbs as raise records fire');
      assert.ok(obs.maxAi < 128, 'the AI level stays below 0x80 (a raise folds it back)');
      assert.ok(obs.formationSelections >= 1, 'a flying formation is selected live (count set)');
      assert.equal(obs.countInRange, true, 'the selected wave size stays in the recorded range 1..6');
    },
    // Break the raise dispatch (its handler == comparison never matches) so the AI level never
    // rises → the aiRose assertion fails.
    negativeMutation: (p) =>
      mutate.changeEqualsOperand(p, 'Stage', 'raise_ai_level_and_set_formation', '__never__'),
  },
  {
    key: 'fire-permission-masks',
    behavior: 'Area-schedule fire-mask records set the per-family fire-permission masks live',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // The schedule sets the fire masks from the record bytes as it scrolls. One headless pump
      // covers many game ticks, so a specific transient value (logram is set to 255, then 31 within
      // one pump) can be stepped over — assert the robust fact instead: each family mask, the
      // ground-stop-firing row, and Andor Genesis (first scheduled in area 4) are SEEN set to a
      // non-zero scheduled value. (The FIRING that consumes them is the enemy slices'.) The window is
      // long enough to cross into area 4 so all nine DIF-03 targets are actually exercised.
      let logramSet = false;
      let otherMaskSet = false;
      let andorSet = false;
      let groundStopSet = false;
      const others = [
        'fire-mask-derota',
        'fire-mask-zoshi',
        'fire-mask-terrazi',
        'fire-mask-kapi',
        'fire-mask-boza-logram',
        'fire-mask-domogram',
      ];
      for (let i = 0; i < 130; i += 1) {
        step(vm, 1);
        if (readVar(vm, 'fire-mask-logram') > 0) logramSet = true;
        if (readVar(vm, 'fire-mask-andor-genesis') > 0) andorSet = true;
        if (readVar(vm, 'ground-stop-firing-row') > 0) groundStopSet = true;
        for (const id of others) if (readVar(vm, id) > 0) otherMaskSet = true;
      }
      return { logramSet, otherMaskSet, andorSet, groundStopSet };
    },
    assert(obs) {
      assert.equal(obs.logramSet, true, 'the logram fire mask is set to a non-zero scheduled value');
      assert.equal(obs.otherMaskSet, true, 'other family fire masks are set live too');
      assert.equal(obs.andorSet, true, 'the Andor Genesis fire mask is set live (area 4)');
      assert.equal(obs.groundStopSet, true, 'the ground-stop-firing row is set live');
    },
    // Break the logram mask branch (its handler == comparison never matches) so it is never set →
    // the logramSet assertion fails.
    negativeMutation: (p) => mutate.changeEqualsOperand(p, 'Stage', 'fire_mask_logram', '__never__'),
  },
  {
    key: 'toroid-wave-spawns-and-moves',
    behavior:
      'The formation spawner fills flying slots with live Toroids that then move under their own velocity each tick, drawn by six persistent clones',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Over the window: a flying slot (59..64) is SEEN holding a live Toroid (type 10/11), and a
      // slot that stays a Toroid across a tick with no intervening empty is SEEN to change position
      // — that is movement, not a refill (a cull frees the slot first, so prevType would be 0). The
      // renderer pool is a fixed six clones bound to the six flying slots.
      let toroidSeen = false;
      let movedSeen = false;
      const prevType = {};
      const prevX = {};
      const prevY = {};
      for (let i = 0; i < 60; i += 1) {
        step(vm, 1);
        const type = readVar(vm, 'slot-type');
        const x = readVar(vm, 'slot-x');
        const y = readVar(vm, 'slot-y');
        for (const s of FLYING_SLOT_INDICES) {
          const t = type[s];
          if (t === 10 || t === 11) {
            toroidSeen = true;
            if (prevType[s] === t && (x[s] !== prevX[s] || y[s] !== prevY[s])) movedSeen = true;
          }
          prevType[s] = t;
          prevX[s] = x[s];
          prevY[s] = y[s];
        }
      }
      return { toroidSeen, movedSeen, clones: cloneCount(vm, 'toroid') };
    },
    assert(obs) {
      assert.equal(obs.toroidSeen, true, 'a live Toroid occupies a flying slot');
      assert.equal(obs.movedSeen, true, 'a live Toroid advances its position under its own velocity');
      assert.equal(obs.clones, 6, 'the Toroid renderer pool is the fixed six flying-slot clones');
    },
    // Empty `update toroid` so Toroids still spawn but no occupant ever advances → movedSeen false.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update toroid'),
  },
  {
    key: 'rng-draw-order',
    behavior:
      'The Toroid spawner consumes the shared RNG in walk order — the live draw stream follows the LFSR model from a seeded state',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Seed the shared state to a fixture seed, then let the spawner draw: `rng out` holds the
      // latest draw, so each tick where it changes is a strictly-later position in the LFSR stream.
      // The observed values must therefore be an ordered subsequence of the fixture outputs — proof
      // the RNG is consumed forward from the seed, in order, with no re-seed or divergence. The
      // window stays short so cumulative draws stay inside the 256-entry fixture (no wrap).
      const seed = 4660;
      const fixture = RNG_FIXTURES.find((s) => s.seed === seed).outputs;
      writeVar(vm, 'rng-state', seed);
      let prev = readVar(vm, 'rng-out');
      const observed = [];
      for (let i = 0; i < 8; i += 1) {
        step(vm, 1);
        const out = readVar(vm, 'rng-out');
        if (out !== prev) {
          observed.push(out);
          prev = out;
        }
      }
      let ptr = -1;
      let ordered = true;
      for (const v of observed) {
        const at = fixture.indexOf(v, ptr + 1);
        if (at < 0) {
          ordered = false;
          break;
        }
        ptr = at;
      }
      return { count: observed.length, ordered };
    },
    assert(obs) {
      assert.ok(obs.count >= 3, 'the spawner draws from the shared RNG while waves fill');
      assert.equal(obs.ordered, true, 'the live draw stream is an ordered subsequence of the LFSR model');
    },
    // Empty `rng step` so `rng out` never advances → no draws are observed → the count assertion fails.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'rng step'),
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
