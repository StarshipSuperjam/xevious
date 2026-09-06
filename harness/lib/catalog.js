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

// Seed a deterministic blaster-to-air kill: place a live Toroid in the last flying slot (index 63,
// which the walk sweeps last) and an active player shot in a shot slot at the SAME cell, so the walk's
// shot-vs-air detector resolves the overlap on the first tick — before the spawner refills anything.
// Returns the Toroid's expected award (its value-table entry). Writes the slot lists directly (the
// blaster clone normally mirrors the shot's position; here we place it), so no firing/aiming is needed.
function seedAirKill(vm, { enemySlot = 63, shotSlot = 36, cellX = 5000, cellY = 4000 } = {}) {
  const put = (id, i, v) => {
    const a = readVar(vm, id);
    a[i] = v;
  };
  put('slot-type', enemySlot, 10);
  put('slot-state', enemySlot, 1);
  put('slot-pts', enemySlot, 3);
  put('slot-x', enemySlot, cellX);
  put('slot-y', enemySlot, cellY);
  put('slot-dx', enemySlot, 0);
  put('slot-dy', enemySlot, 0);
  put('slot-timer', enemySlot, 0);
  put('slot-flag', enemySlot, 0);
  put('slot-code', enemySlot, 8);
  put('slot-type', shotSlot, 1);
  put('slot-state', shotSlot, 1);
  put('slot-x', shotSlot, cellX);
  put('slot-y', shotSlot, cellY);
  return readVar(vm, 'eco-value-table')[2]; // Toroid pts = 3 (1-based) -> value-table index 2
}

// Place a live Toroid exactly on the craft's current cell so the walk's flying-vs-craft check raises
// `player hit` and (with invuln cleared) the craft dies — the deterministic PLY-02 trigger that
// replaces the retired D/G debug death keys.
function seedCraftHit(vm, enemySlot = 63) {
  const pr = readVar(vm, 'player-row');
  const pc = readVar(vm, 'player-col');
  const put = (id, i, v) => {
    const a = readVar(vm, id);
    a[i] = v;
  };
  put('slot-type', enemySlot, 10);
  put('slot-state', enemySlot, 1);
  put('slot-x', enemySlot, pr * 256);
  put('slot-y', enemySlot, pc * 256);
  put('slot-dx', enemySlot, 0);
  put('slot-dy', enemySlot, 0);
  put('slot-flag', enemySlot, 0);
}

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
      tapKey(vm, 'b'); // a gameplay key (bomb) must do nothing at title
      step(vm, 3);
      const gatedHeld = state(vm) === 'title' && epoch(vm) === epochBefore;
      // Hold start and stop at the first playing tick (a tap overshoots — see reachPlaying); keep the
      // craft alive so it does not die back to the title before we observe playing.
      writeVar(vm, 'invuln', 1);
      keyDown(vm, ' ');
      let reached = false;
      for (let i = 0; i < 120 && !reached; i += 1) {
        step(vm, 1);
        if (state(vm) === 'playing') reached = true;
      }
      keyUp(vm, ' ');
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
    behavior: 'A flying enemy touching the craft runs death -> respawn and returns to playing',
    playtestStep: 5,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Turn off the reach-time invulnerability so contact kills; keep lives high so a death respawns
      // rather than reaching game-over. A Toroid seeded on the craft's cell each pump forces the hit.
      writeVar(vm, 'invuln', 0);
      const epoch0 = epoch(vm);
      let returnedToPlaying = false;
      for (let i = 0; i < 20; i += 1) {
        writeVar(vm, 'eco-craft', 9999);
        seedCraftHit(vm);
        step(vm, 1);
        if (state(vm) === 'playing' && epoch(vm) > epoch0) returnedToPlaying = true;
      }
      return { outcome: outcome(vm), returnedToPlaying, epochDelta: epoch(vm) - epoch0 };
    },
    assert(obs) {
      assert.equal(obs.outcome, 'respawn', 'a non-terminal death sets the respawn outcome');
      assert.equal(obs.returnedToPlaying, true, 'the craft respawns back into play');
      assert.ok(obs.epochDelta >= 2, 'the death and respawn each advance the state epoch');
    },
    // Remove player-dead -> respawning so the craft cannot return to play → assertion fails.
    negativeMutation: (p) => mutate.removeAllowedTransition(p, 'player-dead -> respawning'),
  },
  {
    key: 'death-game-over',
    behavior: 'A terminal death (last craft) runs death -> game-over and returns to title',
    playtestStep: 5,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Contact kills (invuln off), one craft left: the last death is terminal.
      writeVar(vm, 'invuln', 0);
      writeVar(vm, 'eco-craft', 1);
      seedCraftHit(vm);
      let reachedTitle = false;
      for (let i = 0; i < 20 && !reachedTitle; i += 1) {
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
    key: 'enemy-bullet-fires',
    behavior:
      'A shooting Toroid (type 0x0B) allocates an aimed enemy bullet when it commits its swing',
    playtestStep: 5,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // As shooting Toroids in the live waves draw level with the craft they fire, allocating an enemy
      // bullet — `bullet alloc result` becomes that slot and stays non-zero after the first fire. (A
      // bullet flies and culls within one headless pump, so the allocation result is the stable signal;
      // the bullet actually killing the craft is a rendered collision, the operator playtest's.)
      let fired = false;
      for (let i = 0; i < 30 && !fired; i += 1) {
        step(vm, 1);
        if (readVar(vm, 'bullet-alloc-result') > 0) fired = true;
      }
      return { fired };
    },
    assert(obs) {
      assert.equal(obs.fired, true, 'a shooting Toroid allocated an enemy bullet');
    },
    // Empty `update toroid` so no Toroid ever swings or fires → no bullet is allocated.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update toroid'),
  },
  {
    key: 'score-digits-render',
    behavior:
      'Score and high-score HUD digit clones display the running values as digit costumes, not a stuck glyph',
    playtestStep: 6,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // The debug S fixture is gone: the score is earned by a real blaster-to-air kill, then read
      // back off the HUD digit clones.
      seedAirKill(vm);
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
      assert.ok(obs.score > 0, 'a blaster-to-air kill raised the score above zero');
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
    key: 'toroid-swing-reverses-away',
    behavior:
      'A Toroid drawing level with the craft REVERSES its lateral velocity, swinging away from its approach (the arcade toggle_dir bounce), not homing into the craft',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Seed one approaching Toroid whose column is one to the LEFT of the craft — so the craft is to
      // its right (offset = player col - slot col = +1, inside the [-2,1] swing-trigger window) and it
      // commits SWING_RIGHT. The reference (`toroid_toggle_dir` -> `toroid_swing_right`, `subq #1,_dY`)
      // nudges the lateral velocity AGAINST the craft-ward approach each tick, so it decelerates and
      // then peels AWAY: `slot dy` must go NEGATIVE (toward lower columns / away from the craft on the
      // right). The homing regression this session introduced drove it POSITIVE (into the craft); this
      // asserts the arcade bounce. Placed several rows ahead so it is level laterally but not
      // overlapping the craft (invuln is on from reachPlaying regardless).
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      const slot = 63;
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      put('slot-type', slot, 10); // non-shooting Toroid, so no bullet muddies the trace
      put('slot-state', slot, 1);
      put('slot-x', slot, (pr - 10) * 256); // 10 rows ahead of the craft
      put('slot-y', slot, (pc - 1) * 256); // one column left of the craft => offset +1, craft to the right
      put('slot-dx', slot, 0);
      put('slot-dy', slot, 0);
      put('slot-flag', slot, 0); // APPROACH — eligible to trigger the swing
      put('slot-timer', slot, 0);
      put('slot-code', slot, 8);
      // One pump runs the walk (settles well past the trigger tick); read the resulting lateral velocity.
      step(vm, 1);
      return { dy: readVar(vm, 'slot-dy')[slot], flag: readVar(vm, 'slot-flag')[slot] };
    },
    assert(obs) {
      assert.equal(obs.flag, 1, 'the Toroid committed a right swing (craft on the right)');
      assert.ok(
        obs.dy < 0,
        `a right-swinging Toroid reverses away from the craft (dy < 0, arcade bounce); got dy=${obs.dy}`,
      );
    },
    // Empty `update toroid` so the swing never runs → dy stays 0 (not < 0) → the assertion bites.
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
  {
    key: 'terrazi-wave-spawns-and-moves',
    behavior:
      'The formation spawner inits Terrazi-typed slots by type and the ordered walk advances them under their own aimed velocity each tick',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Terrazi (type 17) is not in area 1's baseline formation, so force a Terrazi wave through the
      // REAL spawner path: clear the flying slots, make every flying-type-table entry Terrazi, and set
      // a full formation count. The spawner then inits each empty flying slot as a Terrazi (proving the
      // spawn-by-type dispatch); the walk advances them (proving update). A slot that stays Terrazi
      // across a tick with no intervening empty is SEEN to change position — movement, not a refill.
      const typeTable = readVar(vm, 'flying-type-table');
      for (let i = 0; i < typeTable.length; i += 1) typeTable[i] = 17;
      writeVar(vm, 'formation-count', 6);
      const slotType = readVar(vm, 'slot-type');
      for (const s of FLYING_SLOT_INDICES) slotType[s] = 0;
      let terraziSeen = false;
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
          if (t === 17) {
            terraziSeen = true;
            if (prevType[s] === 17 && (x[s] !== prevX[s] || y[s] !== prevY[s])) movedSeen = true;
          }
          prevType[s] = t;
          prevX[s] = x[s];
          prevY[s] = y[s];
        }
      }
      return { terraziSeen, movedSeen };
    },
    assert(obs) {
      assert.equal(obs.terraziSeen, true, 'the spawner inits a Terrazi-typed slot by type');
      assert.equal(obs.movedSeen, true, 'a live Terrazi advances its position under its own velocity');
    },
    // Empty `update terrazi` so Terrazis still spawn but no occupant ever advances → movedSeen false.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update terrazi'),
  },
  {
    key: 'terrazi-glides-and-reverses',
    behavior:
      'A Terrazi drawing level with the craft in scroll commits a GLIDE that decelerates and REVERSES its lateral velocity (the arcade terrazi_main_cont peel-away), not a straight homing dive',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Seed one Terrazi level with the craft in the SCROLL axis (row offset 0, inside the [-4,3] glide
      // window) with a craft-ward lateral velocity. On the trigger tick it latches GLIDE and, while
      // gliding, decrements the lateral velocity by DECEL each tick (`subq #2,_dX`), so `slot dy` crosses
      // zero and goes NEGATIVE — the decelerate-and-reverse. Placed several columns to the side so it is
      // level in scroll but not overlapping the craft's cell (invuln is on from reachPlaying regardless).
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      const slot = 63;
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      put('slot-type', slot, 17);
      put('slot-state', slot, 1);
      put('slot-x', slot, pr * 256); // same scroll row as the craft => row offset 0, inside the window
      put('slot-y', slot, (pc + 5) * 256); // five columns aside, not overlapping the craft cell
      put('slot-dx', slot, 0);
      put('slot-dy', slot, 8); // a craft-ward lateral approach the glide must decelerate and reverse
      put('slot-flag', slot, 0); // APPROACH — eligible to trigger the glide
      put('slot-timer', slot, 0);
      put('slot-code', slot, 1);
      // One pump settles well past the trigger: the glide latches (flag = GLIDE) and the lateral
      // velocity decelerates below zero. The enemy naturally glides off-field and is culled within the
      // settle (a pump is many ticks, not one — see step()), but cull keeps `slot flag`/`slot dy`, so
      // the committed-glide and reversed-lateral evidence survives to read (the same reason the Toroid
      // swing scenario reads its post-cull `slot dy`). Magnitude is not asserted, only the sign flip.
      step(vm, 1);
      return { dy: readVar(vm, 'slot-dy')[slot], flag: readVar(vm, 'slot-flag')[slot] };
    },
    assert(obs) {
      assert.equal(obs.flag, 1, 'the Terrazi committed its glide (flag = GLIDE)');
      assert.ok(
        obs.dy < 0,
        `a gliding Terrazi decelerates and reverses its lateral course (dy < 0); got dy=${obs.dy}`,
      );
    },
    // Empty `update terrazi` so the glide never runs → flag stays 0 and dy stays 8 → the assertion bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update terrazi'),
  },
  {
    key: 'blaster-kills-toroid-and-scores',
    behavior:
      'A player shot overlapping a flying Toroid resolves the hit through the single score path: the score rises by the Toroid value and the shot is consumed',
    playtestStep: 6,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const score0 = readVar(vm, 'eco-score');
      const award = seedAirKill(vm);
      // One pump runs many game ticks; the walk resolves the seeded overlap on its first tick (before
      // the spawner refills), marks the shot spent, and scores exactly the Toroid's value once.
      step(vm, 1);
      return {
        delta: readVar(vm, 'eco-score') - score0,
        award,
        shotState: readVar(vm, 'slot-state')[36],
      };
    },
    assert(obs) {
      assert.equal(obs.delta, obs.award, 'the kill scores exactly the Toroid value once');
      assert.notEqual(obs.shotState, 1, 'the shot that resolved the hit is consumed (no longer active)');
    },
    // Empty the shot-vs-air detector so no overlap is ever resolved → the score never rises.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'check air shot hit'),
  },
  {
    key: 'air-shot-hit-column-bounded',
    behavior:
      'The shot-vs-air hit box spans the rendered Toroid width (±1 column) but no further: a controlled shot on-column or one column off scores, two columns off does not',
    playtestStep: 6,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      // Park the Toroid 8 columns from the craft so the real tapped shot (craft column) can never reach
      // it — only the CONTROLLED shot we seed into a real detector slot (37) can score it. `eResult`
      // maps offset -> score delta; the enemy is re-parked before each probe (a scoring hit frees it).
      const eResult = (dCol) => {
        const pr = readVar(vm, 'player-row'),
          pc = readVar(vm, 'player-col');
        const eRow = pr - 6,
          eCol = pc - 8;
        put('slot-type', 63, 10);
        put('slot-state', 63, 1);
        put('slot-pts', 63, 3);
        put('slot-x', 63, eRow * 256);
        put('slot-y', 63, eCol * 256);
        put('slot-dx', 63, 0);
        put('slot-dy', 63, 0);
        put('slot-flag', 63, 9);
        put('slot-timer', 63, 0);
        put('slot-code', 63, 8);
        const score0 = readVar(vm, 'eco-score');
        put('slot-type', 37, 1); // a controlled shot in a real detector slot (SHOT_SLOTS = 37-39)
        put('slot-state', 37, 1);
        put('slot-x', 37, eRow * 256);
        put('slot-y', 37, (eCol + dCol) * 256);
        step(vm, 1);
        return readVar(vm, 'eco-score') - score0;
      };
      return { onCol: eResult(0), oneOff: eResult(1), twoOff: eResult(2), award: readVar(vm, 'eco-value-table')[2] };
    },
    assert(obs) {
      assert.equal(obs.onCol, obs.award, 'a shot on the Toroid column scores');
      assert.equal(obs.oneOff, obs.award, 'a shot one column off still scores (within the rendered sprite)');
      assert.equal(obs.twoOff, 0, 'a shot two columns off does NOT score (past the sprite width)');
    },
    // Empty the shot-vs-air detector so no controlled shot ever resolves → the on-column assertion fails.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'check air shot hit'),
  },
  {
    key: 'craft-collision-is-single-cell',
    behavior:
      'A Toroid raises player-hit ONLY on the craft’s exact cell: one column off or one row off does not — the collision box is a single cell, not the quadrant above/beside the craft',
    playtestStep: 5,
    async drive(vm) {
      // invuln stays ON from reachPlaying: the craft cannot die, and `player hit` latches (it is cleared
      // only in the invuln-off death branch), so each seeded overlap is directly observable.
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const pr = readVar(vm, 'player-row'),
        pc = readVar(vm, 'player-col');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      const seedAt = (dRow, dCol) => {
        put('slot-type', 63, 10);
        put('slot-state', 63, 1);
        put('slot-x', 63, (pr + dRow) * 256);
        put('slot-y', 63, (pc + dCol) * 256);
        put('slot-dx', 63, 0);
        put('slot-dy', 63, 0);
        put('slot-flag', 63, 9);
        put('slot-timer', 63, 0);
        put('slot-code', 63, 8);
      };
      // One column beside the craft (same row): must NOT touch. The quadrant bug fired here.
      writeVar(vm, 'player-hit', 0);
      seedAt(0, 1);
      step(vm, 1);
      const offColumn = readVar(vm, 'player-hit');
      // One row above the craft (same column): must NOT touch. The quadrant bug fired here too.
      writeVar(vm, 'player-hit', 0);
      seedAt(1, 0);
      step(vm, 1);
      const offRow = readVar(vm, 'player-hit');
      // The craft's exact cell: MUST touch.
      writeVar(vm, 'player-hit', 0);
      seedAt(0, 0);
      step(vm, 1);
      const onCell = readVar(vm, 'player-hit');
      return { offColumn, offRow, onCell };
    },
    assert(obs) {
      assert.equal(obs.offColumn, 0, 'a Toroid one column beside the craft does NOT touch it');
      assert.equal(obs.offRow, 0, 'a Toroid one row above the craft does NOT touch it');
      assert.equal(obs.onCell, 1, 'a Toroid on the craft cell raises player hit');
    },
    // Neutralize the walk so the on-cell overlap is never checked → the on-cell assertion fails.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'advance slots'),
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
