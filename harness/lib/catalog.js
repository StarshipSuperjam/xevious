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
  callProc,
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
// Suppress ALL ground-object spawns for the rest of the run by emptying the schedule's ground-object
// type column (the ground analogue of forcing the flying type table to the non-shooting Toroid). With no
// type to stamp, the ground dispatch spawns nothing, so the first ground firer — the Logram, which opens
// on a masked-random timer and allocates one aimed enemy bullet through the SAME `bullet alloc result`
// signal the flying firing scenarios watch — never appears and cannot contaminate an isolate-one-flying-
// firer scenario (whose negative would otherwise never bite: a bullet still allocates even with the
// flying proc severed). This MUST be a one-time source-data write, NOT a per-tick slot-band clear: one
// `step()` settles through many internal ticks, during which the live schedule both spawns a Logram and
// drives it to its ANIMATE fire, so clearing the slot band between pumps cannot catch it — only emptying
// the spawn source keeps it from ever appearing. This is the ground extension of the
// live-behavior-contaminates-older-unit-scenarios isolation pattern.
function suppressGroundSpawns(vm) {
  const groundType = readVar(vm, 'area-schedule-ground-type');
  for (let i = 0; i < groundType.length; i += 1) groundType[i] = 0;
}

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
    // Break the arm guard (Stage `advance bomb`'s `bomb in flight == 0`, moved off the bomb sprite in
    // the ground-targeting rework) so a press never arms → the armed-seen assertion fails.
    negativeMutation: (p) => mutate.changeVarEqualsOperand(p, 'Stage', 'bomb in flight', 0, 99),
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
      // Suppress ground spawns (a live Logram fires through the same alloc signal) and reset it (a ground
      // firer may have tripped it during settling), so only a live shooting Toroid can move it.
      suppressGroundSpawns(vm);
      writeVar(vm, 'bullet-alloc-result', 0);
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
      // long enough to cross into area 4 so all nine DIF-03 targets are actually exercised — and is held
      // generously wide (area 4 is normally reached ~frame 34 and each mask, once scheduled, PERSISTS for
      // ~120 frames) so that scratch-vm execution jitter in area-progression timing (which surfaced as an
      // intermittent CI miss of the persistent Andor mask at the old 130-frame budget) cannot step past it.
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
      for (let i = 0; i < 260; i += 1) {
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
    key: 'live-pressure-density',
    behavior:
      'DIF-01/FORM-01 (.play): the live formation count tracks the committed count table indexed by the raised AI level — waves vary (the arcade sawtooth), not a constant or monotonic growth, and stay within 1..6',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // The raise handler folds the AI level then re-selects the formation, setting `formation count`
      // to `item (formation index + 33) of formation count table` (slot = index - FORMATION_MIN_INDEX
      // + 1, FORMATION_MIN_INDEX = -32). The existing `difficulty-and-formations` scenario only proves
      // *a* count is set; this proves the CORRESPONDENCE holds live against the in-VM table: every
      // count set equals the table entry the live index points at, the counts VARY across pumps (the
      // arcade sawtooth — refuting the old "waves grow denser" prose), and they stay in the 1..6 band.
      const table = readVar(vm, 'formation-count-table'); // 0-based JS array of the 160 entries
      let aiRose = false;
      let mismatches = 0;
      let checked = 0;
      let countInRange = true;
      const distinct = new Set();
      for (let i = 0; i < 200; i += 1) {
        step(vm, 1);
        if (readVar(vm, 'difficulty-ai-level') > 0) aiRose = true;
        const count = readVar(vm, 'formation-count');
        if (count > 0) {
          const index = readVar(vm, 'formation-index');
          const slot0 = Number(index) + 32; // 0-based: slot (1-based) = index + 33
          if (slot0 >= 0 && slot0 < table.length) {
            checked += 1;
            distinct.add(Number(count));
            if (count < 1 || count > 6) countInRange = false;
            if (Number(count) !== Number(table[slot0])) mismatches += 1;
          }
        }
      }
      return { aiRose, mismatches, checked, countInRange, distinct: distinct.size };
    },
    assert(obs) {
      assert.equal(obs.aiRose, true, 'the AI level climbs as raise records fire');
      assert.ok(obs.checked >= 2, 'the density chain sets a formation count from the table live');
      assert.equal(
        obs.mismatches,
        0,
        'every live count equals the committed table entry at the live index',
      );
      assert.ok(
        obs.distinct >= 2,
        'wave size varies with the AI level (the sawtooth, not a constant/monotonic growth)',
      );
      assert.equal(obs.countInRange, true, 'every selected wave size stays within the recorded 1..6');
    },
    // Pin `formation count` to a constant so it no longer tracks the table: the distinct-values set
    // collapses to one and the constant mismatches the table at most indices → the correspondence
    // and variation assertions fail.
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'formation count', 3),
  },
  {
    key: 'live-pressure-adaptive',
    behavior:
      'DIF-02 (.play): a heavy score with craft in reserve re-tunes the AI level past the raise-only fold ceiling (the score adjust is NOT folded, unlike raises)',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Raises fold at 0x80 (>=128 subtracts 64 once), so the AI level from raises ALONE can never be
      // observed >= 128. The score adjust adds floor(floor(score/1000)/craft) (capped 16) WITHOUT
      // folding, so a heavy score with craft in reserve is the ONLY way the live AI level crosses 128.
      // Inject that state and pump area 1: crossing 128 is the adjust's unique signature (the raise-
      // only baseline tops out at 126 here — see the `difficulty-and-formations` scenario).
      writeVar(vm, 'eco-score', 999000);
      writeVar(vm, 'eco-craft', 1);
      let maxAi = 0;
      for (let i = 0; i < 200; i += 1) {
        step(vm, 1);
        maxAi = Math.max(maxAi, Number(readVar(vm, 'difficulty-ai-level')));
      }
      return { maxAi };
    },
    assert(obs) {
      assert.ok(
        obs.maxAi >= 128,
        'the score adjust pushes the AI level past the raise-only fold ceiling (127)',
      );
    },
    // Sever the adjust dispatch (its handler == comparison never matches) so only raises drive the AI
    // level → it folds and can never be observed >= 128 → the assertion fails.
    negativeMutation: (p) =>
      mutate.changeEqualsOperand(p, 'Stage', 'adjust_ai_level_from_score', '__never__'),
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
      'A Terrazi drawing level with the craft LATERALLY commits a GLIDE that decelerates and REVERSES its forward/scroll velocity (the arcade terrazi_main_cont peel-away), not a straight homing dive',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Seed one Terrazi LATERALLY level with the craft (col offset 0, inside the [-4,3] glide window —
      // the arcade triggers the glide on `_Y`, the lateral axis) with a forward/scroll approach velocity.
      // On the trigger tick it latches GLIDE and, while gliding, decrements the SCROLL velocity by DECEL
      // each tick (`subq #2,_dX`), so `slot dx` crosses zero and goes NEGATIVE — the decelerate-and-
      // reverse of its forward approach. Placed several rows ahead so it is level laterally but not
      // overlapping the craft's cell (invuln is on from reachPlaying regardless).
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      const slot = 63;
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      put('slot-type', slot, 17);
      put('slot-state', slot, 1);
      put('slot-x', slot, (pr - 8) * 256); // eight rows ahead in scroll, not overlapping the craft cell
      put('slot-y', slot, pc * 256); // same lateral column as the craft => col offset 0, inside the window
      put('slot-dx', slot, 8); // a forward/scroll approach the glide must decelerate and reverse
      put('slot-dy', slot, 0);
      put('slot-flag', slot, 0); // APPROACH — eligible to trigger the glide
      put('slot-timer', slot, 0);
      put('slot-code', slot, 1);
      // One pump settles well past the trigger: the glide latches (flag = GLIDE) and the forward velocity
      // decelerates below zero. The enemy naturally backs off-field and is culled within the settle (a
      // pump is many ticks, not one — see step()), but cull keeps `slot flag`/`slot dx`, so the
      // committed-glide and reversed-forward evidence survives to read (the same reason the Toroid swing
      // scenario reads its post-cull `slot dy`). Magnitude is not asserted, only the sign flip.
      step(vm, 1);
      return { dx: readVar(vm, 'slot-dx')[slot], flag: readVar(vm, 'slot-flag')[slot] };
    },
    assert(obs) {
      assert.equal(obs.flag, 1, 'the Terrazi committed its glide (flag = GLIDE)');
      assert.ok(
        obs.dx < 0,
        `a gliding Terrazi decelerates and reverses its forward approach (dx < 0); got dx=${obs.dx}`,
      );
    },
    // Empty `update terrazi` so the glide never runs → flag stays 0 and dx stays 8 → the assertion bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update terrazi'),
  },
  {
    key: 'terrazi-fires-under-mask',
    behavior:
      'A distant Terrazi fires aimed bullets through the shared fire-permission gate under its captured mask (the periodic-fire path, not the Toroid one-shot)',
    playtestStep: 5,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Isolate the Terrazi fire from live shooting Toroids: make every spawnable flying type the
      // NON-shooting Toroid (type 10, never fires) and clear the flying slots, so `bullet alloc result`
      // can only move if the Terrazi's gate fires. Seed one Terrazi LATERALLY off the craft's line (col
      // offset 12, well outside the [-4,3] glide window on `_Y`, so it stays in the firing approach state
      // and never commits the fire-suppressing glide) with mask 0 (reload 1 => fires on every 8-frame
      // phase, the fastest cap) and a countdown of 1 (fires on the first phase). It is stationary
      // (dx=dy=0) so it never drifts into the window. Reset the shared alloc signal, then pump until a
      // bullet allocates.
      const typeTable = readVar(vm, 'flying-type-table');
      for (let i = 0; i < typeTable.length; i += 1) typeTable[i] = 10;
      const slotType = readVar(vm, 'slot-type');
      for (const s of FLYING_SLOT_INDICES) slotType[s] = 0;
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      const slot = 63;
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      put('slot-type', slot, 17);
      put('slot-state', slot, 1);
      put('slot-x', slot, (pr - 20) * 256); // 20 rows ahead in scroll — no longer what gates the glide
      put('slot-y', slot, (pc - 12) * 256); // 12 columns aside => col offset 12, outside the [-4,3] window
      put('slot-dx', slot, 0); // stationary and laterally distant: it stays in the firing approach state
      put('slot-dy', slot, 0);
      put('slot-flag', slot, 0); // APPROACH
      put('slot-fire-mask', slot, 0); // mask 0 => reload 1 => fires every phase (fastest cap)
      put('slot-fire-timer', slot, 1); // fires on the first phase tick
      suppressGroundSpawns(vm); // no live Logram may move the shared alloc signal
      writeVar(vm, 'bullet-alloc-result', 0);
      let fired = false;
      for (let i = 0; i < 12 && !fired; i += 1) {
        step(vm, 1);
        if (readVar(vm, 'bullet-alloc-result') > 0) fired = true;
      }
      return { fired };
    },
    assert(obs) {
      assert.equal(obs.fired, true, 'the distant Terrazi allocated an aimed bullet through the fire gate');
    },
    // Empty the shared `fire permission gate` so the Terrazi never fires and no other firing path exists
    // (all spawnable types are non-shooting) → `bullet alloc result` stays 0 → the assertion bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'fire permission gate'),
  },
  {
    key: 'kapi-spawns-and-dives',
    behavior:
      'A Kapi approaches silently, then at its countdown expiry latches a PEEL-AWAY dive that ACCELERATES its lateral velocity away from the craft column while DECELERATING its forward/scroll velocity (the arcade kapi_10_fire), not a homing dive or a swing on the scroll axis',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Seed one Kapi in APPROACH, laterally aside from the craft on the LARGER-column side
      // (self col = craft col + 12), with a forward/scroll approach velocity and an approach countdown
      // about to expire. On the trigger tick `update kapi` latches the dive ONCE: because the craft is at
      // a SMALLER lateral coordinate (offset = craft − self < 0) it takes the DIVE_PLUS branch (flag 2),
      // and thereafter each tick ACCELERATES `slot dy` upward (dy += ACCEL) so the enemy peels AWAY to an
      // even larger column, and DECELERATES `slot dx` (dx -= DECEL) so its forward run crosses toward/below
      // its start. A single pump settles past the trigger; cull keeps `slot flag`/`slot dx`/`slot dy`, so
      // the committed-dive evidence survives to read (the same post-cull read the Terrazi glide uses).
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      const slot = 63;
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      put('slot-type', slot, 16);
      put('slot-state', slot, 1);
      put('slot-x', slot, (pr - 8) * 256); // eight rows ahead in scroll, not overlapping the craft cell
      put('slot-y', slot, (pc + 12) * 256); // 12 columns aside on the larger-column side => offset < 0
      put('slot-dx', slot, 8); // a forward/scroll approach the dive must decelerate
      put('slot-dy', slot, 0); // no lateral drift until the dive accelerates it
      put('slot-flag', slot, 0); // APPROACH — eligible to trigger the dive
      put('slot-fire-timer', slot, 1); // approach countdown about to expire => dive triggers this pump
      put('slot-timer', slot, 0);
      put('slot-code', slot, 32); // 0x20, the approach/first dive sprite code
      step(vm, 1);
      return {
        flag: readVar(vm, 'slot-flag')[slot],
        dy: readVar(vm, 'slot-dy')[slot],
        dx: readVar(vm, 'slot-dx')[slot],
      };
    },
    assert(obs) {
      assert.equal(obs.flag, 2, 'the Kapi latched its peel-away dive on the craft-is-smaller side (DIVE_PLUS)');
      assert.ok(
        obs.dy > 0,
        `a diving Kapi accelerates its LATERAL velocity away from the craft column (dy > 0); got dy=${obs.dy}`,
      );
      assert.ok(
        obs.dx < 8,
        `a diving Kapi decelerates its FORWARD/scroll velocity (dx < 8); got dx=${obs.dx}`,
      );
    },
    // Empty `update kapi` so the dive never runs → flag stays 0, dy stays 0, dx stays 8 → every clause bites
    // (and an axis swap — accelerating dx / decelerating dy — would fail the dy>0 and dx<8 pair).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update kapi'),
  },
  {
    key: 'kapi-fires-while-diving',
    behavior:
      'A Kapi is SILENT during its approach and fires aimed bullets through the shared gate every tick once it is diving — no fire-suppression on the dive (unlike the Terrazi glide)',
    playtestStep: 5,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Isolate the Kapi fire from live shooting Toroids: make every spawnable flying type the
      // NON-shooting Toroid (type 10) and clear the flying slots, so `bullet alloc result` can only move
      // if the Kapi's gate fires. Seed one Kapi in APPROACH, stationary (dx=dy=0) and near the craft's
      // column, with mask 0 (reload 1 => fires on every phase) and an approach countdown of 1 so the dive
      // triggers on the first active tick; the trigger arms the fire countdown to 1 and the gate then fires
      // every dive tick with no suppression. Reset the shared alloc signal, then pump until a bullet
      // allocates.
      const typeTable = readVar(vm, 'flying-type-table');
      for (let i = 0; i < typeTable.length; i += 1) typeTable[i] = 10;
      const slotType = readVar(vm, 'slot-type');
      for (const s of FLYING_SLOT_INDICES) slotType[s] = 0;
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      const slot = 63;
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      put('slot-type', slot, 16);
      put('slot-state', slot, 1);
      put('slot-x', slot, (pr - 10) * 256); // ten rows ahead in scroll
      put('slot-y', slot, pc * 256); // near the craft's column so it stays on-field through the dive
      put('slot-dx', slot, 0); // stationary: no reliance on drift
      put('slot-dy', slot, 0);
      put('slot-flag', slot, 0); // APPROACH — silent until the dive triggers
      put('slot-fire-mask', slot, 0); // mask 0 => reload 1 => fires every phase
      put('slot-fire-timer', slot, 1); // approach countdown expires on the first active tick
      put('slot-timer', slot, 0);
      put('slot-code', slot, 32);
      suppressGroundSpawns(vm); // no live Logram may move the shared alloc signal
      writeVar(vm, 'bullet-alloc-result', 0);
      let fired = false;
      for (let i = 0; i < 12 && !fired; i += 1) {
        step(vm, 1);
        if (readVar(vm, 'bullet-alloc-result') > 0) fired = true;
      }
      return { fired };
    },
    assert(obs) {
      assert.equal(obs.fired, true, 'a diving Kapi allocated an aimed bullet through the fire gate');
    },
    // Empty the shared `fire permission gate` so the Kapi never fires and no other firing path exists
    // (all spawnable types are non-shooting) → `bullet alloc result` stays 0 → the assertion bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'fire permission gate'),
  },
  {
    key: 'torkan-approaches-and-fires',
    behavior:
      'A Torkan approaches aimed at the craft and, at its shot-delay expiry, fires ONE aimed bullet DIRECTLY (via the allocator, not the shared periodic fire gate and with no fire mask) — the arcade torkan_shoot single un-masked shot',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Isolate the Torkan shot from live shooting Toroids: make every spawnable flying type the
      // NON-shooting Toroid (type 10) and clear the flying slots, so `bullet alloc result` can only move
      // if the Torkan itself fires. Seed one Torkan in APPROACH near the craft's column, with a shot
      // countdown about to expire. A single pump settles the whole approach->fire->hover->flee arc; the
      // shot allocates exactly at the expiry.
      const typeTable = readVar(vm, 'flying-type-table');
      for (let i = 0; i < typeTable.length; i += 1) typeTable[i] = 10;
      const slotType = readVar(vm, 'slot-type');
      for (const s of FLYING_SLOT_INDICES) slotType[s] = 0;
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      const slot = 63;
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      put('slot-type', slot, 15);
      put('slot-state', slot, 1);
      put('slot-x', slot, (pr - 10) * 256); // ten rows ahead in scroll, aimed back toward the craft
      put('slot-y', slot, pc * 256); // near the craft's column so it stays on-field to fire
      put('slot-dx', slot, 32); // the aimed toward-approach velocity (generic 2 px/frame tier)
      put('slot-dy', slot, 0);
      put('slot-flag', slot, 0); // APPROACH — the shot triggers at the countdown expiry
      put('slot-fire-timer', slot, 2); // shot countdown expires on the first active tick
      put('slot-timer', slot, 0);
      put('slot-code', slot, 16); // 0x10, the approach sprite code
      suppressGroundSpawns(vm); // no live Logram may move the shared alloc signal
      writeVar(vm, 'bullet-alloc-result', 0);
      let fired = false;
      for (let i = 0; i < 12 && !fired; i += 1) {
        step(vm, 1);
        if (readVar(vm, 'bullet-alloc-result') > 0) fired = true;
      }
      return { fired };
    },
    assert(obs) {
      assert.equal(obs.fired, true, 'an approaching Torkan allocated an aimed bullet directly at its shot-delay expiry');
    },
    // Empty `update torkan` so the approach never reaches its shot → no bullet allocates (no other firing
    // path exists, every spawnable type being non-shooting) → `bullet alloc result` stays 0 → the assertion bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update torkan'),
  },
  {
    key: 'torkan-reaims-and-flees',
    behavior:
      'After it fires and hovers, a Torkan re-aims ONCE 180 degrees AWAY from the craft and flees on the FAST (3 px/frame) tier — the arcade torkan_update_dir +0x80 flip onto the terrazi/torkan tier, NOT a homing re-aim or the slow approach tier',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Freeze the walk so one callProc == one deterministic tick, then hand-drive the whole arc a tick at a
      // time. The earlier version pumped a single `step()` and read `slot dx` once at settling; but `step()`
      // runs the update a machine-speed-dependent number of times, and the HOVER phase ZEROES `slot dx`
      // (game_director install_update_torkan) — so on slower CI the read landed mid-HOVER and flaked dx=0.
      // Here we watch for the exact APPROACH -> HOVER -> FLEE transition and capture the committed FLEE
      // velocity the tick `slot flag` first reaches FLEE, before any further move drifts the slot off-field.
      writeVar(vm, 'game-director-state', 'frozen');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      for (const s of FLYING_SLOT_INDICES) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      const slot = 63; // JS index; Scratch flying slot 64
      // Seed one Torkan in APPROACH ten rows AHEAD of the craft on its column, so the aimed approach points
      // TOWARD the craft (+dx, larger row). During HOVER it holds position (dx=0), so at re-aim the geometry
      // is FIXED at (pr-10, pc) vs the craft at (pr, pc): a pure +row toward-vector whose 180-degree flip is
      // a pure -row away-vector on the fast 48-tier (|dx| = 48 > the 32 approach). No settling, no drift.
      put('slot-type', slot, 15); // TORKAN_TYPE
      put('slot-state', slot, 1); // SLOT_ACTIVE
      put('slot-x', slot, (pr - 10) * 256);
      put('slot-y', slot, pc * 256);
      put('slot-dx', slot, 32); // seeded toward-approach velocity (positive); the retreat must reverse it
      put('slot-dy', slot, 0);
      put('slot-flag', slot, 0); // TORKAN_FLAG_APPROACH
      put('slot-fire-timer', slot, 2); // fire on the first tick -> HOVER
      put('slot-timer', slot, 0);
      put('slot-code', slot, 16);
      writeVar(vm, 'slot-index', slot + 1); // Scratch 1-based
      // APPROACH (fire) -> HOVER (hold, dx=0, ~14 ticks to the 28-frame window end) -> FLEE. Capture dx/dy
      // the tick `slot flag` first reaches FLEE (2): the committed away velocity, constant thereafter.
      let fleeTick = null;
      let fleeDx = null;
      let fleeDy = null;
      for (let t = 1; t <= 40 && fleeTick === null; t += 1) {
        callProc(vm, 'Stage', 'update torkan');
        step(vm, 1);
        if (readVar(vm, 'slot-flag')[slot] === 2) {
          fleeTick = t;
          fleeDx = readVar(vm, 'slot-dx')[slot];
          fleeDy = readVar(vm, 'slot-dy')[slot];
        }
      }
      return { fleeTick, dx: fleeDx, dy: fleeDy };
    },
    assert(obs) {
      assert.notEqual(
        obs.fleeTick,
        null,
        'the Torkan completes the arc and reaches the FLEE phase (slot flag 2) within the driven window',
      );
      assert.ok(
        obs.dx < 0,
        `a fleeing Torkan reverses its course AWAY from the craft (dx flips negative from the +32 approach); got dx=${obs.dx}`,
      );
      assert.ok(
        Math.abs(obs.dx) > 32,
        `a fleeing Torkan flees on the FAST 3 px/frame tier (|dx| > the 32 approach magnitude); got dx=${obs.dx}`,
      );
    },
    // Empty `update torkan` so the seeded approach never fires, hovers or re-aims → `slot flag` never reaches
    // FLEE → fleeTick stays null (the notEqual clause bites) and `slot dx` stays the seeded +32 (the reversed
    // and faster-tier clauses would bite too).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update torkan'),
  },
  {
    key: 'zoshi-top-aims-and-fires',
    behavior:
      'A top-entry Zoshi (type 13) fires the SHARED aimed bullet through _fire_aimed_bullet when its masked fire countdown expires on the phase boundary — the arcade zoshi_0D fire allocates an aimed TYPE-6 shot (all three variants fire the same aimed bullet), never a random-direction shot',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Isolate the Zoshi shot from live shooting Toroids: make every spawnable flying type the
      // NON-shooting Toroid (type 10) and clear the flying slots, so `bullet alloc result` can only move
      // if the seeded Zoshi itself fires. Seed one top Zoshi OFF the craft's column and re-plant it +
      // re-prime its countdown each tick until it reaches a fire on the 4-tick phase boundary.
      //
      // WHY THE ASSERTION IS `fired`, NOT A BULLET DIRECTION (settling trap — same hazard the sibling
      // `zoshi-bottom-enters-edge` was designed around). `_step()` runs an unfixed, machine-speed-
      // dependent number of ticks to settling (harness.js lib header), and the top Zoshi RE-AIMS ITS OWN
      // DRIFT TOWARD THE CRAFT on every fire tick — so within one settling step it homes onto (and
      // overshoots) the craft's column, and the LAST fire captured is from ~the craft's own column, where
      // the lateral aim component is ~0 and its sign is a coin flip. A single-tick `bullet dy < 0` read is
      // therefore flaky by construction (empirically ~2/3 fail). The shot's aim correctness is instead
      // verified: (a) structurally in tests/test_scratch_project.py::_air03_failures (all three variants
      // share the one _fire_aimed_bullet call reading the 32-tier aim), and (b) by the operator playtest.
      // Here we assert only the pacing-invariant fact this scenario CAN observe: the shared fire path
      // allocates a bullet — the aimed-shot allocation, matching the sibling aimed-fire scenarios
      // (torkan-approaches-and-fires, terrazi, kapi).
      const typeTable = readVar(vm, 'flying-type-table');
      for (let i = 0; i < typeTable.length; i += 1) typeTable[i] = 10;
      const slotType = readVar(vm, 'slot-type');
      for (const s of FLYING_SLOT_INDICES) slotType[s] = 0;
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      const slot = 63;
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      suppressGroundSpawns(vm); // no live Logram may move the shared alloc signal
      writeVar(vm, 'bullet-alloc-result', 0);
      let fired = false;
      for (let i = 0; i < 24 && !fired; i += 1) {
        put('slot-type', slot, 13);
        put('slot-state', slot, 1);
        put('slot-x', slot, (pr - 10) * 256); // ten rows ahead in scroll
        put('slot-y', slot, (pc + 8) * 256); // eight columns to the side
        put('slot-dx', slot, 24);
        put('slot-dy', slot, 0);
        put('slot-flag', slot, 0);
        put('slot-fire-timer', slot, 1); // expires on the next phase boundary
        put('slot-timer', slot, 0);
        put('slot-code', slot, 40); // 0x28 spin base
        step(vm, 1);
        if (readVar(vm, 'bullet-alloc-result') > 0) fired = true;
      }
      return { fired };
    },
    assert(obs) {
      assert.equal(
        obs.fired,
        true,
        'a top Zoshi allocated a bullet through the shared aimed-fire path when its masked countdown expired on the phase boundary',
      );
    },
    // Empty `update zoshi` so the seeded Zoshi never runs its fire block → no bullet allocates (every
    // spawnable type being non-shooting) → `bullet alloc result` stays 0 → the fired assertion bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update zoshi'),
  },
  {
    key: 'zoshi-bottom-enters-edge',
    behavior:
      'The bottom-entry Zoshi (type 14) is its own reachable object type with its own initializer — the arcade zoshi_0E bottom variant is a distinct spawnable, brought in here through the shared debug spawn cycle (its FIXED bottom-edge entry row 40 is the exact-value contract locked structurally in tests/test_scratch_project.py::_air03_failures, which the settling harness cannot observe — see note)',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Reachability, not entry row. The bottom variant's DISTINCT trait is its fixed bottom-edge spawn
      // row (40), set once by `init zoshi bottom`. That is a SPAWN-INSTANT value the settling harness
      // cannot read: `_step()` runs an unfixed, machine-speed-dependent number of ticks (harness.js
      // header), and BOTH top (row 0) and bottom (row 40) entrants converge on and overshoot the craft
      // row, so within a single settling step they roam the same span (measured: top reaches ~38, bottom
      // drops to ~15) — any post-settling row threshold is unfaithful. The exact entry row 40 is therefore
      // pinned as an EXACT-VALUE structural contract in _air03_failures (zoshi-bottom-fixed-edge-entry).
      // What IS pacing-invariant here is REACHABILITY: that the bottom variant is its own type with its own
      // initializer that stamps a live type-14 slot. The shared debug cursor gallops by >1 per settling step
      // (see debug-key-cycles-families), so an ordered per-family window is racy; instead hold the key,
      // accumulate the types seen over a sustained hold, and require the bottom variant (type 14) to appear
      // — order-independent, so the galloping cursor cannot false-fail it. Do NOT clear the field manually
      // (the debug wave clears its own slots; a manual clear would drive the normal spawner and leak
      // debug-only families, breaking the negative). The negative neutralizes the SHARED `init zoshi
      // bottom` (used by both the debug and normal spawn paths, game_director.py install_spawn_flying), so
      // no path can stamp a type-14 slot and the negative cannot be masked by normal-play leakage.
      //
      // Robustness (contention): `step()` bounds each settling pump by WALL CLOCK (loadBuild sets
      // currentStepTime; harness.js header), so under full-suite CPU load a single pump advances far fewer
      // internal ticks. The debug cursor's per-family dwell has grown every slice (Zakato, Bacura, and now
      // the Sheonite escort, whose homing pair holds the field for a long bounded lifecycle at the tail of
      // the cycle), so a free-galloping cursor completes fewer full cycles per budget and the bottom
      // variant's brief live window can fall between two observed pumps — an intermittent false-fail. So
      // rather than wait for the cursor to WANDER to the bottom variant, PIN the debug spawn cursor to its
      // family index each pump: the debug gate then spawns the bottom variant (through the same shared debug
      // spawn path this scenario is about) as soon as the field is clear and keeps re-spawning it, so a live
      // type-14 slot is reliably present to observe. This removes the timing race while still proving
      // reachability VIA THE DEBUG CYCLE (the gate, not a hand-called init). ZOSHI_BOTTOM is index 4 in
      // game_director.py DEBUG_SPAWN_FAMILIES (terrazi, kapi, torkan, zoshi-top, zoshi-bottom, ...); a
      // family reorder makes the POSITIVE fail loudly here rather than silently drift. The negative still
      // bites: neutralizing `init zoshi bottom` (the shared initializer that stamps the type-14 slot on both
      // the debug and normal paths) means no type-14 slot is ever stamped, even with the cursor pinned.
      const ZOSHI_BOTTOM_DEBUG_INDEX = 4;
      keyDown(vm, 't');
      const seen = new Set();
      for (let i = 0; i < 100; i += 1) {
        writeVar(vm, 'debug-spawn-index', ZOSHI_BOTTOM_DEBUG_INDEX);
        step(vm, 1);
        const type = readVar(vm, 'slot-type');
        for (const s of FLYING_SLOT_INDICES) if (type[s] !== 0) seen.add(type[s]);
      }
      keyUp(vm, 't');
      return { saw: seen.has(14) };
    },
    assert(obs) {
      assert.equal(obs.saw, true, 'the bottom Zoshi (type 14) is reachable — its initializer stamps a live type-14 slot');
    },
    // Empty `init zoshi bottom` so the bottom spawn never stamps its slot (type/state/entry row) → no
    // type-14 slot ever appears → the saw assertion bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'init zoshi bottom'),
  },
  {
    key: 'zoshi-rnd-veers-erratically',
    behavior:
      'A random-veer Zoshi (type 12) re-headings its OWN drift to an UNPREDICTABLE angle at each shot (the arcade zoshi_0C erratic movement) — the "random" is the enemy MOVEMENT, not the shot; seeded ON the craft column, a faithful 0C walks a VARIETY of headings where a toward-craft re-aim would hold a single one',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const typeTable = readVar(vm, 'flying-type-table');
      for (let i = 0; i < typeTable.length; i += 1) typeTable[i] = 10;
      const slotType = readVar(vm, 'slot-type');
      for (const s of FLYING_SLOT_INDICES) slotType[s] = 0;
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      const slot = 63;
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      // Seed a rnd Zoshi ON the craft's column and ahead in scroll, with a fixed initial straight-ahead
      // drift. Re-plant its position and re-prime its countdown each tick so it keeps firing on-field, and
      // clear the enemy-bullet slots each tick so its own aimed shots cannot kill the craft mid-run. A
      // faithful 0C re-headings to a RANDOM 24-tier angle at each fire, walking the drift across many
      // distinct headings; a toward-craft re-aim (seeded on the craft column) would hold a single heading.
      const headings = new Set();
      for (let i = 0; i < 40; i += 1) {
        const st = readVar(vm, 'slot-type');
        for (let b = 39; b <= 57; b += 1) st[b] = 0; // clear enemy-bullet slots (indices 40..58)
        put('slot-type', slot, 12);
        put('slot-state', slot, 1);
        put('slot-x', slot, (pr - 10) * 256);
        put('slot-y', slot, pc * 256); // ON the craft column: a toward-aim keeps dy=0, a random one does not
        put('slot-flag', slot, 0);
        put('slot-fire-timer', slot, 1);
        put('slot-code', slot, 40);
        if (i === 0) {
          put('slot-dx', slot, 24); // fixed initial heading; the re-heading is what varies it
          put('slot-dy', slot, 0);
        }
        step(vm, 1);
        headings.add(`${readVar(vm, 'slot-dx')[slot]},${readVar(vm, 'slot-dy')[slot]}`);
      }
      return { distinct: headings.size };
    },
    assert(obs) {
      assert.ok(
        obs.distinct >= 2,
        `a random-veer Zoshi takes a VARIETY of headings across its shots (distinct headings >= 2); got ${obs.distinct}`,
      );
    },
    // Empty `update zoshi` so the seeded rnd Zoshi never re-headings → its drift stays the fixed seeded
    // (24, 0) for the whole run → exactly one distinct heading → the variety assertion bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update zoshi'),
  },
  {
    key: 'jara-shooter-fires-at-proximity',
    behavior:
      'A 0x55 Jara shooter cruises its aimed approach SILENTLY, then the instant the craft is within its lateral proximity band it commits its one-way turn and fires EXACTLY ONE aimed bullet DIRECTLY (via the allocator, no fire mask, no shared periodic gate) — the arcade jara_shoot single proximity-triggered shot',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Isolate the Jara shot from live shooting Toroids: make every spawnable flying type the NON-shooting
      // Toroid (type 10) and clear the flying slots, so `bullet alloc result` can only move if the seeded
      // Jara itself fires. Seed one 0x55 shooter in APPROACH ON the craft's column (lateral offset 0, well
      // inside the [-6,+5] band) and a few rows ahead in scroll, stationary (dx=dy=0) so it stays on-field
      // to reach its turn. On the first active tick `update jara` sees flag==APPROACH && in-band and commits
      // the turn, firing one aimed bullet. Reset the shared alloc signal, then pump until a bullet allocates.
      const typeTable = readVar(vm, 'flying-type-table');
      for (let i = 0; i < typeTable.length; i += 1) typeTable[i] = 10;
      const slotType = readVar(vm, 'slot-type');
      for (const s of FLYING_SLOT_INDICES) slotType[s] = 0;
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      const slot = 63;
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      put('slot-type', slot, 85); // the 0x55 shooter
      put('slot-state', slot, 1);
      put('slot-x', slot, (pr - 8) * 256); // eight rows ahead in scroll, not overlapping the craft cell
      put('slot-y', slot, pc * 256); // ON the craft's column => lateral offset 0, inside the turn band
      put('slot-dx', slot, 0); // stationary: the shot triggers on proximity, not on drift
      put('slot-dy', slot, 0);
      put('slot-flag', slot, 0); // APPROACH — the turn+shot trigger the first in-band tick
      put('slot-timer', slot, 0);
      put('slot-code', slot, 160); // 0xA0, the approach sprite code
      suppressGroundSpawns(vm); // no live Logram may move the shared alloc signal
      writeVar(vm, 'bullet-alloc-result', 0);
      let fired = false;
      for (let i = 0; i < 12 && !fired; i += 1) {
        step(vm, 1);
        if (readVar(vm, 'bullet-alloc-result') > 0) fired = true;
      }
      return { fired };
    },
    assert(obs) {
      assert.equal(obs.fired, true, 'an approaching 0x55 Jara allocated one aimed bullet directly when the craft entered its proximity band');
    },
    // Empty `update jara` so the approach never reaches its turn → no bullet allocates (no other firing path
    // exists, every spawnable type being non-shooting) → `bullet alloc result` stays 0 → the assertion bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update jara'),
  },
  {
    key: 'jara-peels-and-spins',
    behavior:
      'A Jara cruises straight until the craft is within its lateral band, then commits a ONE-WAY turn that RAMPS its lateral velocity AWAY from the craft column (slot dy grows in the peel direction) while leaving its forward/scroll velocity UNTOUCHED (slot dx unchanged, unlike the Kapi dive) — the arcade jara_moving_right/left ±1 lateral ramp',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Seed one 0x56 silent Jara in APPROACH a few rows ahead in scroll, laterally aside on the SMALLER-
      // column side of the craft (self col = craft col - 4, so the lateral offset = craft - self = +4, inside
      // the [-6,+5] band and >= 0 => the TURN_MINUS side). On the first in-band tick `update jara` latches
      // the turn ONCE (flag -> 1) and thereafter each tick DECREMENTS slot dy (peeling to an even smaller
      // column, AWAY from the craft) while never touching slot dx. A single pump settles past the trigger;
      // cull preserves slot flag/dx/dy, so the committed peel survives to read (the same post-cull read the
      // Kapi dive uses). The 6-frame spin is render-only (derived from the advancing slot clock in the Jara
      // target) and is pinned structurally in _air04_failures; here the observable is the peel kinematics.
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      const slot = 63;
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      put('slot-type', slot, 86); // the 0x56 silent (motion is identical to the shooter; no fire to isolate)
      put('slot-state', slot, 1);
      put('slot-x', slot, (pr - 8) * 256); // eight rows ahead in scroll
      put('slot-y', slot, (pc - 4) * 256); // 4 columns aside on the smaller-column side => offset +4, in band
      put('slot-dx', slot, 8); // a forward/scroll velocity the turn must leave UNTOUCHED
      put('slot-dy', slot, 0); // no lateral drift until the turn ramps it
      put('slot-flag', slot, 0); // APPROACH — eligible to trigger the turn
      put('slot-timer', slot, 0);
      put('slot-code', slot, 160); // 0xA0, the approach sprite code
      step(vm, 1);
      return {
        flag: readVar(vm, 'slot-flag')[slot],
        dy: readVar(vm, 'slot-dy')[slot],
        dx: readVar(vm, 'slot-dx')[slot],
      };
    },
    assert(obs) {
      assert.equal(obs.flag, 1, 'the Jara latched its one-way turn on the craft-is-larger side (TURN_MINUS)');
      assert.ok(
        obs.dy < 0,
        `a turning Jara ramps its LATERAL velocity AWAY from the craft column (dy < 0); got dy=${obs.dy}`,
      );
      assert.equal(
        obs.dx,
        8,
        `a turning Jara leaves its FORWARD/scroll velocity UNTOUCHED (dx stays the seeded 8); got dx=${obs.dx}`,
      );
    },
    // Empty `update jara` so the turn never runs → flag stays 0, dy stays 0 → the turn/peel clauses bite (and
    // an axis swap — ramping dx instead of dy — would fail the dy<0 / dx==8 pair).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update jara'),
  },
  {
    key: 'zakato-teleports-in-then-commits-active',
    behavior:
      'A Zakato teleports in HELD IN PLACE and not yet hittable (state SLOT_TELEPORT, dx=dy=0) while its ~20-frame sparkle plays; when the sparkle clock completes `update zakato` flips it to the hittable SLOT_ACTIVE and stamps its movement (an aimed variant gets a non-zero velocity toward the craft) — the arcade zakato_teleport -> zakato_NN_main fall-through (3961 -> 3733)',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Freeze the walk so one `update zakato` call is exactly one tick (a settling pump would run the
      // update ~220x and race the seeded slot straight through active/self-destruct to freed; see the
      // harness pacing + live-contamination notes). Clear the flying band, seed one CONTINUOUS (0x15,
      // aimed) Zakato mid-teleport at an INTERIOR row (not the top edge — the commit tick immediately runs
      // the active move+cull, and a slot at row 0 would cull before the ACTIVE state could be read) and
      // well OUTSIDE its lateral proximity band (so on commit it moves rather than firing), then hand-drive
      // the update a tick at a time.
      writeVar(vm, 'game-director-state', 'frozen');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      for (const s of FLYING_SLOT_INDICES) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      const slot = 63;
      const pc = readVar(vm, 'player-col');
      put('slot-type', slot, 21); // cont (0x15): aims at the craft on commit → observable non-zero velocity
      put('slot-state', slot, 4); // SLOT_TELEPORT: indestructible, holding in place
      put('slot-x', slot, 10 * 256); // interior row, clear of the top/bottom cull edges
      put('slot-y', slot, (pc - 8) * 256); // 8 columns aside: an on-field column outside the [-4,3] band (so
      // no fire on commit) yet not off the left edge (so the commit-tick active move does not cull it)
      put('slot-dx', slot, 0);
      put('slot-dy', slot, 0);
      put('slot-timer', slot, 0);
      writeVar(vm, 'slot-index', slot + 1); // Scratch 1-based
      const states = [];
      let teleportTicks = 0;
      let committedDx = null;
      let committedDy = null;
      for (let t = 0; t < 15; t += 1) {
        callProc(vm, 'Stage', 'update zakato');
        step(vm, 1);
        const st = readVar(vm, 'slot-state')[slot];
        states.push(st);
        if (st === 4) teleportTicks += 1;
        if (st === 1) {
          committedDx = readVar(vm, 'slot-dx')[slot];
          committedDy = readVar(vm, 'slot-dy')[slot];
          break;
        }
      }
      return { states, teleportTicks, committedDx, committedDy };
    },
    assert(obs) {
      assert.ok(obs.teleportTicks >= 1, 'the Zakato holds in SLOT_TELEPORT while the sparkle plays (the indestructible teleport-in phase)');
      assert.ok(obs.states.includes(1), 'the teleport commits to the hittable SLOT_ACTIVE when the sparkle clock completes');
      assert.ok(
        obs.committedDx !== 0 || obs.committedDy !== 0,
        `a committed aimed Zakato stamps a non-zero velocity toward the craft; got dx=${obs.committedDx}, dy=${obs.committedDy}`,
      );
    },
    // Empty `update zakato` so the sparkle clock never advances → the slot stays SLOT_TELEPORT forever and
    // never commits to ACTIVE → the `states includes 1` assertion bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update zakato'),
  },
  {
    key: 'zakato-fires-once-then-self-destructs',
    behavior:
      'An ACTIVE fused Zakato whose shot fuse has elapsed fires EXACTLY ONE aimed bullet (via the allocator) then flips ITSELF to the benign SLOT_SELF_EXPLODE with its velocity zeroed, plays out its own ~20-frame burst and frees the slot awarding NOTHING — the arcade zakato_shoot -> zakato_explode_and_remove one-shot suicide (3761 -> 3766/3926)',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Freeze the walk (one call == one tick). Seed one fast (0x14, fused) Zakato ACTIVE off the craft
      // cell with its fuse one tick from elapsing, then hand-drive the update. On the first tick the fuse
      // decrements to <= 0, so it fires one aimed bullet and self-destructs; subsequent ticks play out the
      // burst clock and free the slot. Scoring is structurally guarded (zakato-self-destruct-no-score); the
      // score-unchanged check here is a runtime backstop against a stray award on the suicide path.
      writeVar(vm, 'game-director-state', 'frozen');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      for (const s of FLYING_SLOT_INDICES) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      const slot = 63;
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      put('slot-type', slot, 20); // fast (0x14): fires on the fuse, not on Y-proximity
      put('slot-state', slot, 1); // SLOT_ACTIVE, hittable and moving
      put('slot-x', slot, (pr - 6) * 256); // off the craft cell so no craft-collision confounds the read
      put('slot-y', slot, (pc - 8) * 256);
      put('slot-dx', slot, 8); // a live velocity the self-destruct must zero
      put('slot-dy', slot, 8);
      put('slot-fire-timer', slot, 2); // decrements 2/tick → 0 this tick → the fuse elapses and it fires
      put('slot-timer', slot, 0);
      writeVar(vm, 'slot-index', slot + 1);
      const score0 = readVar(vm, 'eco-score');
      writeVar(vm, 'bullet-alloc-result', 0);
      callProc(vm, 'Stage', 'update zakato');
      step(vm, 1);
      const afterFire = {
        fired: readVar(vm, 'bullet-alloc-result') > 0,
        state: readVar(vm, 'slot-state')[slot],
        dx: readVar(vm, 'slot-dx')[slot],
        dy: readVar(vm, 'slot-dy')[slot],
      };
      // Play out the self-destruct burst; it frees on its own 20-frame clock (2/tick, ~10 ticks).
      for (let t = 0; t < 14; t += 1) {
        callProc(vm, 'Stage', 'update zakato');
        step(vm, 1);
      }
      return {
        ...afterFire,
        freedType: readVar(vm, 'slot-type')[slot],
        freedState: readVar(vm, 'slot-state')[slot],
        score0,
        score1: readVar(vm, 'eco-score'),
      };
    },
    assert(obs) {
      assert.equal(obs.fired, true, 'a fused Zakato at fuse<=0 allocates exactly one aimed bullet');
      assert.equal(obs.state, 5, 'having fired, the Zakato flips ITSELF to the benign SLOT_SELF_EXPLODE');
      assert.equal(obs.dx, 0, 'the self-destructing Zakato zeroes its scroll-axis velocity');
      assert.equal(obs.dy, 0, 'the self-destructing Zakato zeroes its lateral velocity');
      assert.equal(obs.freedType, 0, 'the self-destruct burst frees the slot (type cleared) when its clock completes');
      assert.equal(obs.freedState, 0, 'the freed slot state is cleared so it can be reused');
      assert.equal(obs.score1, obs.score0, 'a Zakato that self-destructs after firing awards NOTHING');
    },
    // Empty `update zakato` so the fuse never elapses and it never fires → `bullet alloc result` stays 0
    // and the state never leaves ACTIVE → the fired/state assertions bite.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update zakato'),
  },
  {
    key: 'radiating-bullet-emits-at-explicit-angle',
    behavior:
      'The shared radiating emitter (emit radiating bullet) allocates a fresh enemy bullet from the firing slot and gives it the 48-magnitude (3 px/frame) velocity for the CALLER-CHOSEN direction index — NOT one aimed at the craft and NOT the 2 px/frame generic aimed tier — so the Brag Zakato fan and Garu Zakato ring can lay bullets on explicit angles (init_radiating_bullet 32C4 -> cpy_dY_dX_to_obj 3383, angle_dX_dY_terrazi_torkan_tbl). Two emissions at different angles land on their two distinct table vectors, and each is a live BULLET_TYPE slot the ordinary bullet sweep then flies straight.',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Freeze so callProc == one deterministic invocation. Clear BOTH the flying band and the whole
      // 19-slot bullet band (JS index 39..57 == Scratch bullet slots 40..58) so allocations are
      // predictable and no stray live bullet confounds the reads.
      writeVar(vm, 'game-director-state', 'frozen');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      for (const s of FLYING_SLOT_INDICES) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      for (let js = 39; js <= 57; js += 1) {
        put('slot-type', js, 0);
        put('slot-state', js, 0);
      }
      const slot = 63; // JS index; Scratch flying slot 64
      // Firing cell is an INTERIOR position deliberately NOT aligned with the craft — the whole point
      // of the radiating mechanism is that the direction is the caller's, independent of the craft, so
      // a craft-aim would land on a different vector. `emit radiating bullet` reads only this position.
      const fx = 12 * 256;
      const fy = 9 * 256;
      put('slot-type', slot, 20);
      put('slot-state', slot, 1);
      put('slot-x', slot, fx);
      put('slot-y', slot, fy);
      writeVar(vm, 'slot-index', slot + 1);
      const aimDx48 = readVar(vm, 'aim-dx-48');
      const aimDy48 = readVar(vm, 'aim-dy-48');
      const aimDx32 = readVar(vm, 'aim-dx-32');
      const emit = (angle) => {
        writeVar(vm, 'radiating-angle', angle);
        writeVar(vm, 'bullet-alloc-result', 0);
        callProc(vm, 'Stage', 'emit radiating bullet');
        step(vm, 1);
        const b = readVar(vm, 'bullet-alloc-result');
        const js = b - 1; // Scratch 1-based alloc index -> JS array index
        return {
          b,
          dx: readVar(vm, 'slot-dx')[js],
          dy: readVar(vm, 'slot-dy')[js],
          x: readVar(vm, 'slot-x')[js],
          y: readVar(vm, 'slot-y')[js],
          type: readVar(vm, 'slot-type')[js],
          state: readVar(vm, 'slot-state')[js],
        };
      };
      const A = 4; // ter48 (dx,dy) = (34,34); generic32 = (23,23) — a biting tier difference
      const B = 12; // ter48 (dx,dy) = (-34,34) — a different explicit direction
      const a = emit(A);
      const b = emit(B);
      return {
        a,
        b,
        expAdx: aimDx48[A],
        expAdy: aimDy48[A],
        expBdx: aimDx48[B],
        expBdy: aimDy48[B],
        gen32dxA: aimDx32[A],
        fx,
        fy,
      };
    },
    assert(obs) {
      assert.ok(obs.a.b > 0, 'the emitter allocates a bullet slot for the first emission');
      assert.ok(obs.b.b > 0, 'the emitter allocates a second bullet slot for the second emission');
      assert.notEqual(obs.a.b, obs.b.b, 'two emissions occupy two DIFFERENT bullet slots (fresh alloc each)');
      assert.equal(obs.a.dx, obs.expAdx, 'emission A gets the 48-tier dX for its explicit angle');
      assert.equal(obs.a.dy, obs.expAdy, 'emission A gets the 48-tier dY for its explicit angle');
      assert.equal(obs.b.dx, obs.expBdx, 'emission B gets the 48-tier dX for ITS explicit angle');
      assert.equal(obs.b.dy, obs.expBdy, 'emission B gets the 48-tier dY for ITS explicit angle');
      assert.ok(
        obs.a.dx !== obs.b.dx || obs.a.dy !== obs.b.dy,
        'two different angles produce two different velocity vectors (the caller angle is honored, not hardcoded)',
      );
      assert.notEqual(
        obs.a.dx,
        obs.gen32dxA,
        'the radiating bullet uses the faster 48 (3 px/f) tier, not the generic 32 (2 px/f) aimed tier',
      );
      assert.equal(obs.a.x, obs.fx, "the bullet spawns at the firing slot's scroll-axis cell (x copied)");
      assert.equal(obs.a.y, obs.fy, "the bullet spawns at the firing slot's lateral cell (y copied)");
      assert.equal(obs.a.type, 2, 'the emitted slot is stamped BULLET_TYPE so the bullet sweep advances it');
      assert.equal(obs.a.state, 1, 'the emitted bullet is ACTIVE');
    },
    // Empty `emit radiating bullet` so nothing is ever allocated → `bullet alloc result` stays 0,
    // `b - 1 = -1` reads undefined velocities and the alloc/vector assertions all bite.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'emit radiating bullet'),
  },
  {
    key: 'giddo-spario-flies-straight-and-self-bursts-short',
    behavior:
      'A Giddo Spario is aimed ONCE at spawn and then flies dead straight — `update giddo spario` never re-aims, so an ACTIVE Giddo keeps the exact velocity it was seeded with while it advances by 4*velocity/tick (handle_08_Giddo_Spario move_object_dX_dY 5238). On death it uses its OWN short burst, not the shared one: a HIT Giddo frees its slot on the 8-frame giddo_spario_hit clock (~4 ticks at 2/tick), far sooner than the 20-frame shared flying burst (~10 ticks) — the single documented exception, giddo_spario_hit 5241-5253.',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Freeze the walk so one callProc == one tick (a settling pump would run the update ~220x and race
      // the slot off-field). Clear the flying band, then seed one Giddo and hand-drive it a tick at a time.
      writeVar(vm, 'game-director-state', 'frozen');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      const clearBand = () => {
        for (const s of FLYING_SLOT_INDICES) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
      };
      const slot = 63; // JS index; Scratch flying slot 64
      // --- Phase A: an ACTIVE Giddo flies straight on its once-set velocity (no re-aim). ---
      clearBand();
      put('slot-type', slot, 8); // GIDDO_SPARIO_TYPE
      put('slot-state', slot, 1); // SLOT_ACTIVE
      put('slot-x', slot, 10 * 256); // interior row, clear of the cull edges
      put('slot-y', slot, 12 * 256); // interior column
      put('slot-dx', slot, 12); // a live once-aimed velocity the straight flyby must PRESERVE
      put('slot-dy', slot, -8);
      put('slot-timer', slot, 0);
      writeVar(vm, 'slot-index', slot + 1); // Scratch 1-based
      const dxSeq = [];
      const dySeq = [];
      for (let t = 0; t < 3; t += 1) {
        callProc(vm, 'Stage', 'update giddo spario');
        step(vm, 1);
        dxSeq.push(readVar(vm, 'slot-dx')[slot]);
        dySeq.push(readVar(vm, 'slot-dy')[slot]);
      }
      const flightX = readVar(vm, 'slot-x')[slot];
      const flightY = readVar(vm, 'slot-y')[slot];
      // --- Phase B: a HIT Giddo frees on its OWN short 8-frame burst clock. ---
      clearBand();
      put('slot-type', slot, 8);
      put('slot-state', slot, 2); // SLOT_HIT: routes to `explode giddo spario tick`
      put('slot-x', slot, 10 * 256);
      put('slot-y', slot, 12 * 256);
      put('slot-dx', slot, 4);
      put('slot-dy', slot, 0);
      put('slot-timer', slot, 0);
      writeVar(vm, 'slot-index', slot + 1);
      let freedTick = null;
      for (let t = 1; t <= 12; t += 1) {
        callProc(vm, 'Stage', 'update giddo spario');
        step(vm, 1);
        if (freedTick === null && readVar(vm, 'slot-type')[slot] === 0) freedTick = t;
      }
      return { dxSeq, dySeq, flightX, flightY, freedTick };
    },
    assert(obs) {
      assert.deepEqual(
        obs.dxSeq,
        [12, 12, 12],
        `a Giddo flies straight: its once-aimed scroll velocity is NEVER re-aimed; got dx sequence ${JSON.stringify(obs.dxSeq)}`,
      );
      assert.deepEqual(
        obs.dySeq,
        [-8, -8, -8],
        `a Giddo's lateral velocity is likewise held constant (no re-aim); got dy sequence ${JSON.stringify(obs.dySeq)}`,
      );
      assert.equal(obs.flightX, 10 * 256 + 3 * 4 * 12, 'the Giddo advances by 4*dx per tick along the scroll axis');
      assert.equal(obs.flightY, 12 * 256 + 3 * 4 * -8, 'the Giddo advances by 4*dy per tick laterally');
      assert.ok(
        obs.freedTick !== null && obs.freedTick <= 5,
        `a struck Giddo frees on its OWN 8-frame burst (~4 ticks), well before the 20-frame shared burst (~10 ticks); freed at tick ${obs.freedTick}`,
      );
    },
    // Empty `update giddo spario` so the ACTIVE slot never moves (flight displacement stays 0) and the HIT
    // slot never advances its burst clock (never frees) → the displacement and free assertions bite.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update giddo spario'),
  },
  {
    key: 'brag-spario-accelerates-toward-craft',
    behavior:
      'A Brag Spario is an accelerating homer: every ACTIVE tick `update brag spario` nudges its velocity toward the craft by BRAG_SPARIO_ACCEL (4 raw units) on EACH axis — the scroll axis by the sign of (player row - slot row), the lateral axis by the sign of (player col - slot col) — with no clamp (handle_09_Brag_Spario 3092-3121). Seeded from rest with the craft ahead and to one side, |dx| and |dy| ramp 4,8,12,16 in lockstep — the sharpest contrast with the Giddo, which never re-aims.',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      writeVar(vm, 'game-director-state', 'frozen');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      for (const s of FLYING_SLOT_INDICES) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      const slot = 63;
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      put('slot-type', slot, 9); // BRAG_SPARIO_TYPE
      put('slot-state', slot, 1); // SLOT_ACTIVE
      // Six cells behind and six cells to one side of the craft: both offsets stay POSITIVE across the run
      // (the tiny per-tick displacement never overtakes the craft), so both axes accelerate in the + sign.
      put('slot-x', slot, (pr - 6) * 256);
      put('slot-y', slot, (pc - 6) * 256);
      put('slot-dx', slot, 0); // seeded from REST: the homing must build the velocity itself
      put('slot-dy', slot, 0);
      put('slot-timer', slot, 0);
      writeVar(vm, 'slot-index', slot + 1);
      const dxSeq = [];
      const dySeq = [];
      for (let t = 0; t < 4; t += 1) {
        callProc(vm, 'Stage', 'update brag spario');
        step(vm, 1);
        dxSeq.push(readVar(vm, 'slot-dx')[slot]);
        dySeq.push(readVar(vm, 'slot-dy')[slot]);
      }
      return { dxSeq, dySeq };
    },
    assert(obs) {
      assert.deepEqual(
        obs.dxSeq,
        [4, 8, 12, 16],
        `the Brag accelerates toward the craft on the scroll axis by 4/tick, unbounded; got dx sequence ${JSON.stringify(obs.dxSeq)}`,
      );
      assert.deepEqual(
        obs.dySeq,
        [4, 8, 12, 16],
        `the Brag accelerates toward the craft on the lateral axis by 4/tick, unbounded; got dy sequence ${JSON.stringify(obs.dySeq)}`,
      );
    },
    // Empty `update brag spario` so the velocity never ramps from rest → the acceleration sequences bite.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update brag spario'),
  },
  {
    key: 'brag-zakato-fires-five-bullet-fan',
    behavior:
      'A fused Brag Zakato whose shot fuse has elapsed fires a TERMINAL 5-bullet aimed radiating FAN — five fresh enemy bullets at the 48-magnitude (3 px/f) tier, two radiating-steps apart around the craft-aim direction (brag_zakato_shoot 5054) — then flips ITSELF to the benign SLOT_SELF_EXPLODE with its velocity zeroed, awarding NOTHING (brag_zakato_explode 3920). The sharpest contrast with the base Zakato, which fires a SINGLE aimed bullet.',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Freeze so one callProc == one deterministic tick. Clear the flying band AND the whole 19-slot
      // bullet band (JS 39..57 == Scratch bullet slots 40..58) so the fan's allocations are the only live
      // bullets and no stray shot confounds the count.
      writeVar(vm, 'game-director-state', 'frozen');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      for (const s of FLYING_SLOT_INDICES) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      for (let js = 39; js <= 57; js += 1) {
        put('slot-type', js, 0);
        put('slot-state', js, 0);
      }
      const slot = 63;
      const pr = readVar(vm, 'player-row');
      const pc = readVar(vm, 'player-col');
      put('slot-type', slot, 22); // BRAG_ZAKATO_RND_TYPE: fires on the random fuse, not Y-proximity
      put('slot-state', slot, 1); // SLOT_ACTIVE
      put('slot-x', slot, (pr - 6) * 256); // off the craft cell so no craft-collision confounds the read
      put('slot-y', slot, (pc - 8) * 256);
      put('slot-dx', slot, 8); // a live velocity the self-destruct must zero
      put('slot-dy', slot, 8);
      put('slot-fire-timer', slot, 2); // 2/tick → 0 this tick → the fuse elapses and it fires the fan
      put('slot-timer', slot, 0);
      writeVar(vm, 'slot-index', slot + 1);
      const aimDx48 = readVar(vm, 'aim-dx-48');
      const aimDy48 = readVar(vm, 'aim-dy-48');
      const score0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'update brag zakato');
      step(vm, 1);
      // Collect the freshly-allocated enemy bullets (BULLET_TYPE, ACTIVE) across the 19-slot bullet band.
      const bullets = [];
      for (let js = 39; js <= 57; js += 1) {
        if (readVar(vm, 'slot-type')[js] === 2 && readVar(vm, 'slot-state')[js] === 1) {
          bullets.push({
            dx: readVar(vm, 'slot-dx')[js],
            dy: readVar(vm, 'slot-dy')[js],
            x: readVar(vm, 'slot-x')[js],
            y: readVar(vm, 'slot-y')[js],
          });
        }
      }
      // Recover the fan's aim-centred base angle: the shoot loop leaves `radiating angle` at
      // base + FAN_COUNT*FAN_STEP (5 emits, +2 each), so base = (radiating angle - 10) mod 32.
      const base = (((readVar(vm, 'radiating-angle') - 10) % 32) + 32) % 32;
      const expected = [];
      for (let i = 0; i < 5; i += 1) {
        const a = (base + 2 * i) % 32;
        expected.push(`${aimDx48[a]},${aimDy48[a]}`);
      }
      return {
        count: bullets.length,
        vectors: bullets.map((b) => `${b.dx},${b.dy}`).sort(),
        expected: expected.sort(),
        atCell: bullets.every((b) => b.x === (pr - 6) * 256 && b.y === (pc - 8) * 256),
        distinctVectors: new Set(bullets.map((b) => `${b.dx},${b.dy}`)).size,
        state: readVar(vm, 'slot-state')[slot],
        dx: readVar(vm, 'slot-dx')[slot],
        dy: readVar(vm, 'slot-dy')[slot],
        scoreDelta: readVar(vm, 'eco-score') - score0,
      };
    },
    assert(obs) {
      assert.equal(obs.count, 5, 'the terminal fan emits exactly 5 enemy bullets');
      assert.deepEqual(
        obs.vectors,
        obs.expected,
        'the 5 fan bullets take the 48-tier vectors at the aim-centred angles two steps apart',
      );
      assert.ok(obs.distinctVectors >= 2, 'the fan spreads across distinct directions (not five identical shots)');
      assert.equal(obs.atCell, true, "every fan bullet leaves the Brag Zakato's own cell");
      assert.equal(obs.state, 5, 'having fired, the Brag flips ITSELF to the benign SLOT_SELF_EXPLODE');
      assert.equal(obs.dx, 0, 'the self-destructing Brag zeroes its scroll-axis velocity');
      assert.equal(obs.dy, 0, 'the self-destructing Brag zeroes its lateral velocity');
      assert.equal(obs.scoreDelta, 0, 'a Brag Zakato that self-destructs after firing awards NOTHING');
    },
    // Empty `brag zakato shoot` so the fan never allocates → count 0 ≠ 5 and the vector/count assertions bite
    // (the state flip still runs, so the fan-specific checks are what carry the proof).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'brag zakato shoot'),
  },
  {
    key: 'garu-zakato-detonates-into-ring-and-four-sparios',
    behavior:
      'A Garu Zakato whose fuse elapses DETONATES: it lays a 16-bullet 360-degree ring (the even radiating angles 0,2,..,30 at the 48-magnitude tier, from its own cell) AND spawns 4 Brag Sparios into the 4 flying slots ADJACENT to it (the arcade clobbers obj 0x3C-0x3F) at its cell with the four CARDINAL velocities (±32 on each axis, brag_spario_dX/dY_tbl), then VANISHES with no burst and no score (garu_zakato_explode 4031 → init_garu_zakato_explosion 5075).',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Freeze so one callProc == one deterministic tick. Clear the flying band (and its velocities) AND
      // the bullet band so the detonation's ring + Sparios are the only live entities read back.
      writeVar(vm, 'game-director-state', 'frozen');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      for (const s of FLYING_SLOT_INDICES) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
        put('slot-dx', s, 0);
        put('slot-dy', s, 0);
      }
      for (let js = 39; js <= 57; js += 1) {
        put('slot-type', js, 0);
        put('slot-state', js, 0);
      }
      // The Garu must occupy the FIRST flying slot (FLYING_SLOTS[0] == JS 58, Scratch slot-index 59) so its
      // 4 successors — the slots the detonation writes (gslot+1..+4 == JS 59..62) — stay in-band; its only
      // spawner (the debug key) stamps it there. This adjacency is the arcade's obj 0x3C-0x3F clobber.
      const garu = 58;
      const gx = 11 * 256;
      const gy = 9 * 256;
      put('slot-type', garu, 24); // GARU_ZAKATO_TYPE
      put('slot-state', garu, 1); // SLOT_ACTIVE
      put('slot-x', garu, gx);
      put('slot-y', garu, gy);
      put('slot-dx', garu, 48);
      put('slot-dy', garu, 0);
      put('slot-fire-timer', garu, 2); // 2/tick → 0 this tick → the fuse elapses and it detonates
      writeVar(vm, 'slot-index', garu + 1);
      const aimDx48 = readVar(vm, 'aim-dx-48');
      const aimDy48 = readVar(vm, 'aim-dy-48');
      const score0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'update garu zakato');
      step(vm, 1);
      // Ring bullets: BULLET_TYPE + ACTIVE across the 19-slot bullet band.
      const bullets = [];
      for (let js = 39; js <= 57; js += 1) {
        if (readVar(vm, 'slot-type')[js] === 2 && readVar(vm, 'slot-state')[js] === 1) {
          bullets.push({
            dx: readVar(vm, 'slot-dx')[js],
            dy: readVar(vm, 'slot-dy')[js],
            x: readVar(vm, 'slot-x')[js],
            y: readVar(vm, 'slot-y')[js],
          });
        }
      }
      const ringExpected = [];
      for (let a = 0; a < 32; a += 2) ringExpected.push(`${aimDx48[a]},${aimDy48[a]}`);
      // The 4 Sparios land in the adjacent slots garu+1..garu+4 (JS 59..62).
      const sparios = [59, 60, 61, 62].map((js) => ({
        type: readVar(vm, 'slot-type')[js],
        state: readVar(vm, 'slot-state')[js],
        dx: readVar(vm, 'slot-dx')[js],
        dy: readVar(vm, 'slot-dy')[js],
        x: readVar(vm, 'slot-x')[js],
        y: readVar(vm, 'slot-y')[js],
      }));
      return {
        ringCount: bullets.length,
        ringVectors: bullets.map((b) => `${b.dx},${b.dy}`).sort(),
        ringExpected: ringExpected.sort(),
        ringAtCell: bullets.every((b) => b.x === gx && b.y === gy),
        sparioTypes: sparios.map((s) => s.type),
        sparioStates: sparios.map((s) => s.state),
        sparioVels: sparios.map((s) => `${s.dx},${s.dy}`),
        sparioAtCell: sparios.every((s) => s.x === gx && s.y === gy),
        garuType: readVar(vm, 'slot-type')[garu],
        garuState: readVar(vm, 'slot-state')[garu],
        slotIndex: readVar(vm, 'slot-index'),
        scoreDelta: readVar(vm, 'eco-score') - score0,
      };
    },
    assert(obs) {
      assert.equal(obs.ringCount, 16, 'the detonation lays exactly 16 ring bullets');
      assert.deepEqual(
        obs.ringVectors,
        obs.ringExpected,
        'the 16 ring bullets take the 48-tier vectors at the 16 even angles 0,2,..,30 (a full 360° ring)',
      );
      assert.equal(obs.ringAtCell, true, "every ring bullet leaves the Garu's own cell");
      assert.deepEqual(obs.sparioTypes, [9, 9, 9, 9], 'the 4 slots adjacent to the Garu become Brag Sparios (type 9)');
      assert.deepEqual(obs.sparioStates, [1, 1, 1, 1], 'each spawned Brag Spario is ACTIVE');
      assert.deepEqual(
        obs.sparioVels,
        ['32,0', '0,-32', '-32,0', '0,32'],
        'the 4 Sparios launch on the four cardinal velocities (brag_spario_dX/dY_tbl)',
      );
      assert.equal(obs.sparioAtCell, true, "each Brag Spario spawns at the Garu's cell");
      assert.equal(obs.garuType, 0, 'the detonating Garu frees its own slot (type cleared) — no crater, no burst');
      assert.equal(obs.garuState, 0, 'the detonating Garu clears its slot state');
      assert.equal(
        obs.slotIndex,
        59,
        "detonate restores the walk cursor (slot index) to the Garu's slot so the ordered walk resumes correctly",
      );
      assert.equal(obs.scoreDelta, 0, 'a Garu that detonates on its fuse awards NOTHING (it was not shot)');
    },
    // Empty `garu zakato detonate` so no ring/Sparios are laid and the Garu is never freed → the ring count,
    // Spario and free assertions all bite.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'garu zakato detonate'),
  },
  {
    key: 'bacura-spawns-into-band-one-per-second',
    behavior:
      'AIR-11 (.play): the live spawn pump admits Bacura slabs into the reserved band (slots 17-32) one per second up to the scheduled quota and no further — each new slab enters at the top row and carries the downward drift velocity',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 1); // warm the walk once live before isolating
      // Freeze the walk so ONLY our pump call drives the spawn — no area clock, no schedule interference
      // (the same isolation the garu-node / bomb-ground scenarios use for a hand-called handler). Then clear
      // the reserved band and prime the inc coroutine's state directly: quota 3 and the one-second counter at
      // its full reload value (BACURA_INC_PERIOD_FRAMES = 60). We do NOT re-arm the counter between admits —
      // the pump's OWN reload must carry the period, so the counter is left to count all the way down each
      // time. That is what actually exercises the cadence: at TICK_TIMER_STEP = 2 arcade frames/tick, a full
      // 60-frame second is PERIOD_TICKS = 30 pump ticks, so admits must land exactly 30 ticks apart. (An
      // earlier version re-armed the counter to 2 every tick, collapsing the period to one-admit-per-tick and
      // proving only the quota clamp — a regression to the period constant would have stayed green.)
      const PERIOD_FRAMES = 60; // BACURA_INC_PERIOD_FRAMES (main_fn_5 reload one_second_cntr=60)
      const STEP = 2; // TICK_TIMER_STEP arcade frames per tick
      const PERIOD_TICKS = PERIOD_FRAMES / STEP; // 30 pump ticks per admitted slab
      writeVar(vm, 'game-director-state', 'frozen');
      const BAND_LO = 16, BAND_HI = 31; // JS indices for Bacura slots 17..32
      for (let s = BAND_LO; s <= BAND_HI; s += 1) {
        readVar(vm, 'slot-type')[s] = 0;
        readVar(vm, 'slot-state')[s] = 0;
      }
      const quota = 3;
      writeVar(vm, 'num-bacura', 0);
      writeVar(vm, 'bacura-inc-cnt', quota);
      writeVar(vm, 'one-second-cntr', PERIOD_FRAMES); // full reload — the pump counts it down itself
      // Drive whole periods with a few ticks of margin; record num-bacura after every tick.
      const ticks = quota * PERIOD_TICKS + 5;
      const counts = [];
      for (let i = 0; i < ticks; i += 1) {
        callProc(vm, 'Stage', 'pump bacura');
        step(vm, 1);
        counts.push(Number(readVar(vm, 'num-bacura')));
      }
      // The 1-based tick indices where a slab was admitted, and the per-admit jump (must be exactly +1).
      const admitTicks = [];
      let maxJump = 0;
      let prev = 0;
      for (let i = 0; i < counts.length; i += 1) {
        const jump = counts[i] - prev;
        if (jump > maxJump) maxJump = jump;
        if (jump > 0) admitTicks.push(i + 1);
        prev = counts[i];
      }
      const gaps = admitTicks.slice(1).map((t, i) => t - admitTicks[i]);
      const type = readVar(vm, 'slot-type');
      const x = readVar(vm, 'slot-x');
      const dx = readVar(vm, 'slot-dx');
      const banded = [];
      for (let s = BAND_LO; s <= BAND_HI; s += 1) if (type[s] === 1) banded.push(s);
      return {
        quota,
        periodTicks: PERIOD_TICKS,
        admitTicks,
        gaps,
        maxJump,
        firstAdmitTick: admitTicks[0],
        gapsAllOnePeriod: gaps.every((g) => g === PERIOD_TICKS),
        maxCount: Math.max(...counts),
        finalCount: counts[counts.length - 1],
        bandedCount: banded.length,
        allInBand: banded.every((s) => s >= BAND_LO && s <= BAND_HI),
        allTopRow: banded.every((s) => x[s] === 0),
        allDrift: banded.every((s) => dx[s] === 16),
      };
    },
    assert(obs) {
      assert.equal(obs.maxJump, 1, 'no tick ever admits more than one slab (no burst)');
      assert.equal(obs.admitTicks.length, obs.quota, 'the pump admits exactly the scheduled quota of slabs');
      assert.equal(obs.firstAdmitTick, obs.periodTicks, 'the first slab is admitted exactly one arcade second (30 ticks) after the count is set — no early admit');
      assert.equal(obs.gapsAllOnePeriod, true, 'each further slab is admitted exactly one arcade second (30 ticks) after the last — the real BACURA_INC_PERIOD_FRAMES cadence, not one-per-tick');
      assert.equal(obs.maxCount, obs.quota, 'the pump never admits past the scheduled quota');
      assert.equal(obs.finalCount, obs.quota, 'the count settles at the quota and never retreats');
      assert.equal(obs.bandedCount, obs.quota, 'exactly the quota of slabs occupy the reserved band');
      assert.equal(obs.allInBand, true, 'every admitted slab sits in the reserved Bacura band (slots 17-32)');
      assert.equal(obs.allTopRow, true, 'every admitted slab enters at the top row (slot x = 0)');
      assert.equal(obs.allDrift, true, 'every admitted slab carries the downward drift velocity (dx = 16)');
    },
    // Pin the pump's `one second cntr` reload to 0: the FIRST admit still lands one full period after the
    // writeVar-primed 60 (that seed is a VM write, not a Scratch set), but every reload after it is 0, so the
    // counter is <= 0 on the very next tick and the pump admits every tick thereafter. The quota is still
    // reached, so this bites the CADENCE assertions specifically (gaps collapse from 30 to 1) — proving the
    // period coverage is real, exactly the regression class (a broken period reload) the divergence review flagged.
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'one second cntr', 0),
  },
  {
    key: 'bacura-drifts-down-the-field-indestructibly',
    behavior:
      'AIR-11 (.play): a live Bacura slab drifts DOWN the scroll axis at its own velocity each tick (slot x += 64/tick = 1 px/frame, dy = 0) and stays present — the slab is never destroyed or scored by the walk that advances it',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 1);
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 16; s <= 31; s += 1) {
        readVar(vm, 'slot-type')[s] = 0;
        readVar(vm, 'slot-state')[s] = 0;
      }
      const slot = 16; // JS index; Scratch 1-based slot 17 (first Bacura slot)
      const put = (id, v) => {
        readVar(vm, id)[slot] = v;
      };
      put('slot-type', 1); // BACURA_TYPE
      put('slot-state', 1); // SLOT_ACTIVE
      put('slot-x', 5 * 256); // mid-field, clear of the craft and the bottom cull row
      put('slot-y', 20 * 256);
      put('slot-dx', 16); // BACURA_DRIFT_DX
      put('slot-dy', 0);
      writeVar(vm, 'slot-index', slot + 1); // point the handler at this slab
      const xs = [];
      const ys = [];
      for (let t = 0; t < 4; t += 1) {
        callProc(vm, 'Stage', 'update bacura');
        step(vm, 1);
        xs.push(Number(readVar(vm, 'slot-x')[slot]));
        ys.push(Number(readVar(vm, 'slot-y')[slot]));
      }
      return {
        xs,
        ys,
        firstDelta: xs[0] - 5 * 256,
        monotonic: xs.every((v, i) => i === 0 || v > xs[i - 1]),
        lateralHeld: ys.every((v) => v === 20 * 256),
        aliveType: readVar(vm, 'slot-type')[slot],
        aliveState: readVar(vm, 'slot-state')[slot],
      };
    },
    assert(obs) {
      assert.equal(obs.firstDelta, 64, 'the slab advances 64 units/tick down the scroll axis (1 px/frame)');
      assert.equal(obs.monotonic, true, 'the slab keeps drifting down every tick');
      assert.equal(obs.lateralHeld, true, 'the slab does not drift laterally (dy = 0)');
      assert.equal(obs.aliveType, 1, 'the slab that the walk advanced is still present (never destroyed)');
      assert.equal(obs.aliveState, 1, 'the slab stays active — the walk neither hit-marks nor scores it');
    },
    // Empty `update bacura` so the slab never advances → firstDelta 0, not monotonic → the drift assertions bite.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update bacura'),
  },
  {
    key: 'bacura-bounces-the-shot-and-survives',
    behavior:
      'WPN-01 (.play): a player shot overlapping a Bacura is marked for the bounce (shot slot state -> SHOT_BOUNCE = 6) while the slab is left untouched — the shot reflects, the indestructible slab keeps drifting, and nothing is scored',
    playtestStep: 6,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // The shot-vs-Bacura detector is dispatched from the LIVE walk (per live slab, from `update bacura`);
      // a hand-called detector from a frozen VM does not fire (the air detector shares this live-warming
      // need). So drive it live and re-seed the slab + an overlapping shot each frame (the same live re-seed
      // the barra-blaster scenario uses), isolating from spurious kills by suppressing ground spawns and
      // clearing the flying band. The observable is the shot's slot state flipping to SHOT_BOUNCE (6) while
      // the slab's own slot stays a live Bacura.
      step(vm, 2);
      suppressGroundSpawns(vm);
      const slot = 20; // JS; a mid-band Bacura slot, Scratch 1-based 21
      const shotJs = 36; // JS; first player-shot slot (SHOT_SLOTS 37-39 -> JS 36-38)
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      const score0 = Number(readVar(vm, 'eco-score'));
      let bounced = false;
      let slabAliveAtBounce = false;
      for (let i = 0; i < 8 && !bounced; i += 1) {
        // Clear the flying band and every OTHER Bacura slot so nothing else is offered to a detector.
        for (let s = 58; s <= 63; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
        for (let s = 16; s <= 31; s += 1) if (s !== slot) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
        // A live slab, clear of the craft, and a live player shot on the SAME cell.
        put('slot-type', slot, 1); // BACURA_TYPE
        put('slot-state', slot, 1); // SLOT_ACTIVE
        put('slot-x', slot, 6 * 256);
        put('slot-y', slot, 18 * 256);
        put('slot-dx', slot, 0); // hold it in place so the overlap is deterministic across the window
        put('slot-dy', slot, 0);
        put('slot-type', shotJs, 1); // SHOT_TYPE
        put('slot-state', shotJs, 1); // SLOT_ACTIVE
        put('slot-x', shotJs, 6 * 256);
        put('slot-y', shotJs, 18 * 256);
        step(vm, 1);
        if (Number(readVar(vm, 'slot-state')[shotJs]) === 6) {
          bounced = true;
          slabAliveAtBounce =
            readVar(vm, 'slot-type')[slot] === 1 && readVar(vm, 'slot-state')[slot] === 1;
        }
      }
      return { bounced, slabAliveAtBounce, scoreDelta: Number(readVar(vm, 'eco-score')) - score0 };
    },
    assert(obs) {
      assert.equal(obs.bounced, true, 'the overlapping shot is marked for the bounce (slot state -> SHOT_BOUNCE)');
      assert.equal(obs.slabAliveAtBounce, true, 'the slab is untouched by the bounce — still a live Bacura');
      assert.equal(obs.scoreDelta, 0, 'bouncing a shot off a Bacura scores nothing');
    },
    // Empty `check shot bacura` so an overlapping shot is never marked → it is never SHOT_BOUNCE → the bounce
    // assertion bites (the slab-alive clause alone would pass vacuously, so the mark is what proves it).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'check shot bacura'),
  },
  {
    key: 'bacura-touch-raises-craft-death',
    behavior:
      'AIR-11 (.play): a Bacura overlapping the craft raises the player-hit death signal through the wider Bacura collision box, and a slab one cell off does NOT — the slab kills on contact even though it is itself indestructible',
    playtestStep: 5,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 1);
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 16; s <= 31; s += 1) {
        readVar(vm, 'slot-type')[s] = 0;
        readVar(vm, 'slot-state')[s] = 0;
      }
      const slot = 16;
      const pr = Number(readVar(vm, 'player-row'));
      const pc = Number(readVar(vm, 'player-col'));
      const put = (id, v) => {
        readVar(vm, id)[slot] = v;
      };
      put('slot-type', 1);
      put('slot-state', 1);
      put('slot-dx', 16);
      put('slot-dy', 0);
      writeVar(vm, 'slot-index', slot + 1);
      // On the craft's exact cell -> player hit raised.
      put('slot-x', pr * 256);
      put('slot-y', pc * 256);
      writeVar(vm, 'player-hit', 0);
      callProc(vm, 'Stage', 'update bacura');
      step(vm, 1);
      const onCell = Number(readVar(vm, 'player-hit'));
      // One row and one column off -> outside even the wider Bacura box -> no death.
      put('slot-x', (pr - 6) * 256);
      put('slot-y', (pc - 6) * 256);
      writeVar(vm, 'player-hit', 0);
      callProc(vm, 'Stage', 'update bacura');
      step(vm, 1);
      const offCell = Number(readVar(vm, 'player-hit'));
      return { onCell, offCell };
    },
    assert(obs) {
      assert.equal(obs.onCell, 1, 'a Bacura on the craft cell raises the player-hit death signal');
      assert.equal(obs.offCell, 0, 'a Bacura clear of the craft raises no death');
    },
    // Pin every `set player hit` to 0 so the craft-touch consequence can never fire → onCell stays 0 → the
    // death assertion bites (proving it is the Bacura's craft_hit that raises the signal).
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'player hit', 0),
  },
  {
    key: 'bacura-tumbles-with-position',
    behavior:
      'AIR-11 (.play): the Bacura render clone selects its costume by position — the drawn frame index is (floor(slot x / 128)) mod 8, the port image of the arcade (_X>>7)&7 — so as the slab advances it cycles through all eight tumble frames (edge-on → broadside → edge-on) rather than showing one fixed costume (the grey-square defect)',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 1); // director enter creates one render clone per Bacura band slot
      const slotName = variable('bacura-clone-slot').name;
      const slot = 16; // JS index; Scratch 1-based slot 17 (first Bacura band slot)
      // Isolate: clear the band and stop the pump so nothing else is admitted, then seed one held slab.
      for (let s = 16; s <= 31; s += 1) {
        readVar(vm, 'slot-type')[s] = 0;
        readVar(vm, 'slot-state')[s] = 0;
      }
      writeVar(vm, 'num-bacura', 0);
      writeVar(vm, 'bacura-inc-cnt', 0);
      const put = (id, v) => {
        readVar(vm, id)[slot] = v;
      };
      put('slot-type', 1); // BACURA_TYPE
      put('slot-state', 1); // SLOT_ACTIVE
      put('slot-y', 20 * 256);
      put('slot-dx', 0); // hold position: the sampled frame is a pure function of the slot x we set
      put('slot-dy', 0);
      writeVar(vm, 'slot-index', slot + 1);
      // The render clone reads slot x each frame and switches costume; walk the slab across the 8 buckets.
      const UNITS = 128;
      const FRAMES = 8;
      const samples = [];
      for (let k = 0; k < FRAMES; k += 1) {
        put('slot-x', UNITS * k + 32); // mid-bucket, clear of the 128-unit boundary
        step(vm, 1);
        const rep = cloneReports(vm, 'bacura', [slotName]).find(
          (r) => Number(r.vars[slotName]) === slot + 1,
        );
        const m = rep && rep.costume ? /^bacura\/slab\/0([1-8])$/.exec(rep.costume) : null;
        samples.push({
          k,
          costume: rep ? rep.costume : null,
          index: m ? Number(m[1]) - 1 : null,
          visible: rep ? rep.visible : null,
        });
      }
      return {
        indices: samples.map((s) => s.index),
        distinct: new Set(samples.map((s) => s.costume)).size,
        allVisible: samples.every((s) => s.visible === true),
      };
    },
    assert(obs) {
      assert.deepEqual(
        obs.indices,
        [0, 1, 2, 3, 4, 5, 6, 7],
        'the drawn frame index tracks (floor(slot x / 128)) mod 8 across the 8 position buckets',
      );
      assert.equal(obs.distinct, 8, 'all eight tumble frames are shown as the slab advances — not one stuck costume');
      assert.equal(obs.allVisible, true, 'the slab clone is shown at each sampled position');
    },
    // Collapse the tumble index (mod 8 → mod 1 = 0 always) so the render pins to a single frame → indices all
    // 0 and distinct === 1 → both tumble assertions bite (this is exactly the grey-square regression).
    negativeMutation: (p) => {
      const t = p.targets.find((x) => x.name === 'bacura');
      for (const b of Object.values(t.blocks)) {
        if (
          b.opcode === 'operator_mod' &&
          b.inputs &&
          b.inputs.NUM2 &&
          Array.isArray(b.inputs.NUM2[1]) &&
          Number(b.inputs.NUM2[1][1]) === 8
        ) {
          b.inputs.NUM2 = [1, [4, '1']];
          return;
        }
      }
      throw new Error('bacura-tumbles-with-position negative: no mod-8 index block found to collapse');
    },
  },
  {
    key: 'sheonite-homes-onto-the-craft-and-locks',
    behavior:
      'AIR-09 (.play): a homing Sheonite half aims onto its lock target beside the craft at the shared 64-tier, advances DOWN the scroll axis toward it each tick, and flips to LOCK once it reaches the lock line (player row - 2) — the pair flies in and docks rather than drifting straight past',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 1); // warm the walk once live before isolating (the aim tables must be built)
      // Freeze the walk so only our seeded half is advanced by the hand-called updater, and clear the
      // shared flying band so nothing else is offered to it (the flying-pool isolation the Jara/Spario
      // scenarios use). The Sheonite dispatches by `walk type` from this same band.
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 58; s <= 63; s += 1) {
        readVar(vm, 'slot-type')[s] = 0;
        readVar(vm, 'slot-state')[s] = 0;
      }
      const slot = 63; // JS; SHEONITE_RIGHT_SLOT 0x3f (Scratch 1-based 64)
      const pr = Number(readVar(vm, 'player-row'));
      const pc = Number(readVar(vm, 'player-col'));
      const put = (id, v) => {
        readVar(vm, id)[slot] = v;
      };
      put('slot-type', 49); // RIGHT_SHEONITE_TYPE
      put('slot-state', 1); // SLOT_ACTIVE
      put('slot-x', 0); // start at the top of the field, well up-field of the lock line
      put('slot-y', pc * 256);
      put('slot-dx', 0);
      put('slot-dy', 0);
      put('slot-flag', 0); // SHEONITE_PHASE_HOME
      put('slot-timer', 0);
      writeVar(vm, 'slot-index', slot + 1);
      const lockRow = pr - 2; // SHEONITE_LOCK_LEAD = 2
      const rows = [];
      const dxs = [];
      let flag = 0;
      for (let t = 0; t < lockRow + 40 && flag !== 1; t += 1) {
        callProc(vm, 'Stage', 'update sheonite');
        step(vm, 1);
        rows.push(Math.floor(Number(readVar(vm, 'slot-x')[slot]) / 256));
        dxs.push(Number(readVar(vm, 'slot-dx')[slot]));
        flag = Number(readVar(vm, 'slot-flag')[slot]);
      }
      return {
        firstDx: dxs[0],
        finalRow: rows[rows.length - 1],
        lockRow,
        monotonic: rows.every((v, i) => i === 0 || v >= rows[i - 1]),
        lockedFlag: flag,
      };
    },
    assert(obs) {
      assert.ok(obs.firstDx > 0, 'the homing half aims DOWN the scroll axis toward the craft (positive scroll-axis velocity)');
      assert.equal(obs.monotonic, true, 'it advances toward the lock line every tick — never retreating while homing');
      assert.ok(obs.finalRow >= obs.lockRow, 'it reaches the lock line (player row - 2)');
      assert.equal(obs.lockedFlag, 1, 'on reaching the lock line it flips to LOCK — it docks rather than flying past');
    },
    // Empty `update sheonite` so the half never aims or advances → firstDx 0, never reaches the lock line,
    // never flips to LOCK → the home/lock assertions bite.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update sheonite'),
  },
  {
    key: 'sheonite-lock-holds-until-the-end-flag-then-combines',
    behavior:
      'AIR-09 (.play): a Sheonite in LOCK holds beside the craft while the end-flag is clear and only flips to COMBINE (resetting its dwell clock) once sheonite_end raises the end-flag — the release waits on the flag, it is not immediate',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 1);
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 58; s <= 63; s += 1) {
        readVar(vm, 'slot-type')[s] = 0;
        readVar(vm, 'slot-state')[s] = 0;
      }
      const slot = 63;
      const put = (id, v) => {
        readVar(vm, id)[slot] = v;
      };
      put('slot-type', 49); // RIGHT_SHEONITE_TYPE
      put('slot-state', 1);
      put('slot-flag', 1); // SHEONITE_PHASE_LOCK
      put('slot-timer', 10);
      writeVar(vm, 'slot-index', slot + 1);
      // End-flag clear: LOCK holds across several ticks.
      writeVar(vm, 'sheonite-end-flag', 0);
      for (let t = 0; t < 4; t += 1) {
        callProc(vm, 'Stage', 'update sheonite');
        step(vm, 1);
      }
      const heldFlag = Number(readVar(vm, 'slot-flag')[slot]);
      // Raise the end-flag: the next tick flips LOCK -> COMBINE and resets the dwell clock.
      writeVar(vm, 'sheonite-end-flag', 1);
      callProc(vm, 'Stage', 'update sheonite');
      step(vm, 1);
      const afterFlag = Number(readVar(vm, 'slot-flag')[slot]);
      const afterTimer = Number(readVar(vm, 'slot-timer')[slot]);
      return { heldFlag, afterFlag, afterTimer };
    },
    assert(obs) {
      assert.equal(obs.heldFlag, 1, 'while the end-flag is clear the pair holds in LOCK (does not advance to COMBINE)');
      assert.equal(obs.afterFlag, 2, 'once sheonite_end raises the end-flag the pair flips to COMBINE');
      assert.equal(obs.afterTimer, 0, 'the dwell clock is reset on entering COMBINE so the dock dwell counts from 0');
    },
    // Empty `update sheonite` so the LOCK->COMBINE transition never fires → afterFlag stays LOCK → bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update sheonite'),
  },
  {
    key: 'sheonite-combine-exit-retreats-the-right-and-vanishes-the-left',
    behavior:
      'AIR-09 (.play): when the dock dwell completes the right half takes the retreat velocity and enters RETREAT (drifting up the scroll axis away from the craft) while the left half is culled (vanishes) — the pair peels off asymmetrically',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 1);
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 58; s <= 63; s += 1) {
        readVar(vm, 'slot-type')[s] = 0;
        readVar(vm, 'slot-state')[s] = 0;
      }
      const RIGHT = 63; // SHEONITE_RIGHT_SLOT 0x3f
      const LEFT = 62; // SHEONITE_LEFT_SLOT 0x3e
      const seed = (slot, type) => {
        const put = (id, v) => {
          readVar(vm, id)[slot] = v;
        };
        put('slot-type', type);
        put('slot-state', 1);
        put('slot-flag', 2); // SHEONITE_PHASE_COMBINE
        put('slot-timer', 32); // >= SHEONITE_COMBINE_DWELL_FRAMES (the dwell is complete)
        put('slot-dx', 0);
        put('slot-dy', 0);
      };
      seed(RIGHT, 49);
      seed(LEFT, 50);
      // Advance the right half one tick past the dwell.
      writeVar(vm, 'slot-index', RIGHT + 1);
      callProc(vm, 'Stage', 'update sheonite');
      step(vm, 1);
      const rightFlag = Number(readVar(vm, 'slot-flag')[RIGHT]);
      const rightDx = Number(readVar(vm, 'slot-dx')[RIGHT]);
      // Advance the left half one tick past the dwell.
      writeVar(vm, 'slot-index', LEFT + 1);
      callProc(vm, 'Stage', 'update sheonite');
      step(vm, 1);
      const leftType = Number(readVar(vm, 'slot-type')[LEFT]);
      const leftState = Number(readVar(vm, 'slot-state')[LEFT]);
      return { rightFlag, rightDx, leftType, leftState };
    },
    assert(obs) {
      assert.equal(obs.rightFlag, 3, 'the right half enters RETREAT when the dwell completes');
      assert.equal(obs.rightDx, -96, 'the right half takes the retreat velocity (SHEONITE_RETREAT_DX = -96 -> -6 px/frame up the scroll axis, away from the craft)');
      assert.equal(obs.leftType, 0, 'the left half is culled at dwell end — its slot type cleared, so it vanishes');
      assert.equal(obs.leftState, 0, 'the culled left half is no longer active');
    },
    // Empty `update sheonite` so neither half exits COMBINE → right never retreats, left never culls → bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update sheonite'),
  },
  {
    key: 'sheonite-is-inert-on-the-craft-cell',
    behavior:
      'AIR-09 (.play): the Sheonite is wholly inert — a half sitting on the craft cell across HOME/LOCK/COMBINE raises NO player-hit death signal, is never hit-marked or scored, and stays a live escort (the corrected no-collision-of-any-kind contract: the arcade STATE=3 handlers are skipped by every hit test, so the port omits the craft detector entirely)',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 1);
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 58; s <= 63; s += 1) {
        readVar(vm, 'slot-type')[s] = 0;
        readVar(vm, 'slot-state')[s] = 0;
      }
      const slot = 63; // right half
      const pr = Number(readVar(vm, 'player-row'));
      const pc = Number(readVar(vm, 'player-col'));
      const put = (id, v) => {
        readVar(vm, id)[slot] = v;
      };
      put('slot-type', 49); // RIGHT_SHEONITE_TYPE
      put('slot-state', 1); // SLOT_ACTIVE
      put('slot-dx', 0);
      put('slot-dy', 0);
      writeVar(vm, 'slot-index', slot + 1);
      const score0 = Number(readVar(vm, 'eco-score'));
      let maxHit = 0;
      // Park the half exactly on the craft cell through each phase and advance it; the craft never dies.
      for (const phase of [0, 1, 2]) {
        put('slot-flag', phase);
        put('slot-timer', 0);
        put('slot-x', pr * 256);
        put('slot-y', pc * 256);
        writeVar(vm, 'player-hit', 0);
        callProc(vm, 'Stage', 'update sheonite');
        step(vm, 1);
        maxHit = Math.max(maxHit, Number(readVar(vm, 'player-hit')));
      }
      return {
        maxHit,
        aliveType: Number(readVar(vm, 'slot-type')[slot]),
        aliveState: Number(readVar(vm, 'slot-state')[slot]),
        scoreDelta: Number(readVar(vm, 'eco-score')) - score0,
      };
    },
    assert(obs) {
      assert.equal(obs.maxHit, 0, 'a Sheonite on the craft cell raises NO player-hit death signal in any phase — it is inert');
      assert.equal(obs.aliveType, 49, 'the escort is never hit-marked or removed by a collision (its type is unchanged)');
      assert.equal(obs.aliveState, 1, 'the escort stays active — no hit test ever marks it');
      assert.equal(obs.scoreDelta, 0, 'the inert escort is never scored');
    },
    // Inertness is realized by OMISSION (update sheonite writes no `player hit`), so there is no block to
    // neutralize — the negative must GRAFT the forbidden write. A `set player hit = 1` spliced onto the
    // updater is exactly the regression class (wrongly cloning Bacura's craft-death onto the inert pair):
    // now the craft dies while a Sheonite is advanced, so the inertness assertion bites.
    negativeMutation: (p) => mutate.graftVariableSetOnProc(p, 'Stage', 'update sheonite', 'player hit', 1),
  },
  {
    key: 'debug-key-cycles-families',
    behavior:
      'The temporary debug key (T) brings enemies in through the shared spawner and, spawn by spawn, advances its family cursor through every built family (self-extending to the newly built Zakato entries), so each family can be cycled to for playtesting (tracked for removal)',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Isolate the AIR debug cycle from live ground firers. GND-07's Domogram is self-moving: a path
      // segment with dx=0 holds it on-screen indefinitely (the terrain scrolls past it) while it fires an
      // aimed bullet into the flying band every few frames — a steady stream that intermittently re-occupies
      // the band and blocks the debug wave's field-empty gate, stalling the cursor before it completes. (A
      // static Logram scrolled off with the terrain and stopped firing, so it never stalled this.) Zero the
      // ground-type column so no ground family spawns at all — same isolation enemy-bullet-fires uses; it
      // touches only schedule data, never the debug air wave.
      suppressGroundSpawns(vm);
      // Family PRESENCE cannot prove the debug key did anything: normal play eventually scrolls into zones
      // that spawn every family too (measured with no key held — all of types 12..17 appear within ~80
      // settling steps, type 15 as early as step ~2), so accumulating seen types is confounded and cannot
      // make the negative bite. The debug-specific, pacing-invariant signal is the CURSOR itself: the
      // `debug spawn index` advances one step per fresh debug spawn and wraps mod len(DEBUG_SPAWN_FAMILIES)
      // (game_director.py install_debug_spawn_wave); NORMAL play never touches it. Hold T, sweep the cursor,
      // and collect the distinct residues seen — proving it self-extends across every built family rather
      // than stopping at a fixed set. The exact residue→family binding is pinned structurally in
      // tests/test_scratch_project.py (DEBUG_SPAWN_FAMILIES); this scenario proves the cursor drives the
      // whole cycle at runtime.
      //
      // The cursor only advances on a FRESH debug spawn — i.e. when the flying band is empty (the debug wave
      // brings in one solo, then waits for it to leave before the next). But the tail of the cycle includes
      // the Garu Zakato, whose detonation seeds 4 Brag Sparios — accelerating homers that, against this
      // harness's stationary, non-firing craft, orbit forever and never cull. Passively held, the cursor
      // therefore parks at the family after Garu and never completes the cycle (measured: it froze after 9
      // of 17 residues even over 3000 frames). So we clear the flying band ourselves each frame to reopen
      // the field-empty gate — this does NOT drive the normal spawner: while T is held the debug wave sets
      // `formation count`/`formation type offset` every tick before the spawner runs, so the only family
      // that can enter is the debug wave's current one, and only the debug wave ever writes the cursor.
      // How far the cursor jumps between our per-frame samples varies (in the opening frames several fresh
      // spawns land in one settling, so it can step by >1), so "reached the max" is not "saw every residue".
      // But across successive wraps every residue 0..N-1 is eventually sampled, so we loop until the set is
      // a complete contiguous run 0..max (no residue skipped) that reaches the last built family. When each
      // gate reopens is subject to scratch-vm execution jitter (full coverage was measured between ~50 and
      // ~195 frames across runs), so budget a generous cap (early-exit on completion keeps the common case
      // fast) and let the count self-extend: a new family just pushes `max` up, no threshold to re-tune.
      keyDown(vm, 't');
      const cursors = new Set([readVar(vm, 'debug-spawn-index')]);
      let anyFlying = false;
      let maxCursor = 0;
      for (let i = 0; i < 600; i += 1) {
        const slotType = readVar(vm, 'slot-type');
        const slotState = readVar(vm, 'slot-state');
        for (const s of FLYING_SLOT_INDICES) { slotType[s] = 0; slotState[s] = 0; }
        step(vm, 1);
        const cursor = readVar(vm, 'debug-spawn-index');
        cursors.add(cursor);
        if (cursor > maxCursor) maxCursor = cursor;
        const type = readVar(vm, 'slot-type');
        if (FLYING_SLOT_INDICES.some((s) => type[s] !== 0)) anyFlying = true;
        // Complete: every residue 0..max collected (contiguous) and reached the last built family (>=16).
        if (cursors.size === maxCursor + 1 && maxCursor >= 16) break;
      }
      keyUp(vm, 't');
      const contiguous = cursors.size === maxCursor + 1;
      return { distinctCursors: cursors.size, maxCursor, contiguous, anyFlying };
    },
    assert(obs) {
      assert.equal(obs.anyFlying, true, 'holding the debug key stamps flying enemies through the shared spawner');
      assert.ok(obs.contiguous, `the debug cursor steps +1 with no skips (residues 0..${obs.maxCursor} with no gaps); saw ${obs.distinctCursors} distinct`);
      assert.ok(
        obs.maxCursor >= 16,
        `the debug cycle self-extends through every built family slot (residues 0..16, incl. the new Giddo Spario, four base Zakato, two Brag Zakato and Garu Zakato entries); reached ${obs.maxCursor}`,
      );
    },
    // Empty `debug spawn wave` so the key never advances its cursor → `debug spawn index` stays 0 →
    // maxCursor == 0 → the self-extension assertion (maxCursor >= 16) bites (normal play leaves the cursor
    // untouched, so it cannot mask the mutation).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'debug spawn wave'),
  },
  {
    key: 'debug-ground-key-cycles-families',
    behavior:
      'The temporary debug ground key (G) stamps a built GROUND family into the band through the shared ground seed builders and, spawn by spawn, advances its family cursor through every built ground family (self-extending as later ground families are built), so each can be cycled to for a bomb playtest (tracked for removal, #119)',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // The G key is the ground analog of the T key. Family PRESENCE alone cannot make the negative bite — the
      // area schedule scrolls ground families in on its own — so isolate the debug tool two ways: (1) suppress
      // every SCHEDULED ground spawn (empty the schedule's ground-type column) so the ONLY ground objects that
      // can appear are the debug key's, and (2) watch the debug-specific, pacing-invariant signal —
      // `debug ground index`, which advances one step per FRESH debug ground spawn and wraps mod
      // len(DEBUG_GROUND_FAMILIES) (game_director.py install_debug_ground_spawn); normal play never touches it.
      // The exact residue→family binding is pinned in game_director.py (DEBUG_GROUND_FAMILIES); this scenario
      // proves the cursor drives the whole cycle at runtime and that a fresh spawn actually stamps the band.
      //
      // The cursor only advances on a FRESH spawn — i.e. when the ground band is empty (the tool stamps one
      // family, then defers until it scrolls off). Ground objects always scroll DOWN and cull off the field, so
      // this never stalls; but to sweep the whole cycle quickly we clear the ground band (JS slots 0..15)
      // ourselves each frame to reopen the field-empty gate. That does NOT drive the schedule (suppressed
      // above): only the debug tool ever stamps ground or writes the cursor. How far the cursor jumps between
      // samples varies (a family may cull within one settling), so we loop until the residues form a complete
      // contiguous run 0..max that reaches the last built family; a new family just pushes max up, no threshold
      // to re-tune.
      suppressGroundSpawns(vm);
      keyDown(vm, 'g');
      const cursors = new Set([readVar(vm, 'debug-ground-index')]);
      let anyGround = false;
      let maxCursor = 0;
      const LAST = 6; // Boza Logram is the 7th built ground family (index 6); self-extends as more are built
      for (let i = 0; i < 600; i += 1) {
        const slotType = readVar(vm, 'slot-type');
        const slotState = readVar(vm, 'slot-state');
        for (let s = 0; s < 16; s += 1) { slotType[s] = 0; slotState[s] = 0; }
        step(vm, 1);
        const cursor = readVar(vm, 'debug-ground-index');
        cursors.add(cursor);
        if (cursor > maxCursor) maxCursor = cursor;
        const type = readVar(vm, 'slot-type');
        for (let s = 0; s < 16; s += 1) if (type[s] !== 0) anyGround = true;
        // Complete: every residue 0..max collected (contiguous) and reached the last built family (>= 6).
        if (cursors.size === maxCursor + 1 && maxCursor >= LAST) break;
      }
      keyUp(vm, 'g');
      const contiguous = cursors.size === maxCursor + 1;
      return { distinctCursors: cursors.size, maxCursor, contiguous, anyGround };
    },
    assert(obs) {
      assert.equal(obs.anyGround, true, 'holding the debug ground key stamps a ground family into the band');
      assert.ok(obs.contiguous, `the debug ground cursor steps +1 with no skips (residues 0..${obs.maxCursor} with no gaps); saw ${obs.distinctCursors} distinct`);
      assert.ok(
        obs.maxCursor >= 6,
        `the debug ground cycle self-extends through every built ground family (residues 0..6: Barra, Zolbak, Garu Barra, Logram, Derota, Garu Derota, Boza Logram); reached ${obs.maxCursor}`,
      );
    },
    // Empty `debug ground spawn` so the key never stamps or advances → `debug ground index` stays 0 →
    // maxCursor == 0 and no ground ever appears (the schedule is suppressed) → both the self-extension
    // (>= 6) and the anyGround assertions bite (normal play never touches the cursor, so nothing masks it).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'debug ground spawn'),
  },
  {
    key: 'debug-ground-key-isolates-normal-enemies',
    behavior:
      'While the temporary debug ground key (G) is held it isolates the ground family under test (parity with the T aerial key): the normal flying-formation stream is suppressed — the formation-wave count is pinned at 0 and the flying band is cleared every tick — so no normal enemies enter the screen and the operator can focus on the ground family alone (tracked for removal, #119)',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // The operator's report: holding G still let normal flying waves pour in. The fix makes the G proc
      // suppress the flying stream every tick it is held (game_director.py install_debug_ground_spawn: zero
      // `formation count`, clear the flying band) so the spawner below it brings in nothing. Prove it by
      // holding G through a long window in which normal play WOULD spawn flying enemies — the T-key scenario
      // (debug-key-cycles-families) measures every flying type appearing within ~80 settling steps with no key
      // held — and asserting the flying band NEVER populates. We clear the GROUND band each frame only so the
      // debug tool keeps cycling; that never drives the flying spawner (only the schedule/spawner does, and G
      // pins its count to 0). Deliberately NO suppressGroundSpawns: the normal stream must stay live so the
      // negative (which strips the suppression) actually spawns and the assertion can bite.
      keyDown(vm, 'g');
      let anyFlying = false;
      for (let i = 0; i < 140; i += 1) {
        const slotType = readVar(vm, 'slot-type');
        const slotState = readVar(vm, 'slot-state');
        for (let s = 0; s < 16; s += 1) { slotType[s] = 0; slotState[s] = 0; }
        step(vm, 1);
        const type = readVar(vm, 'slot-type');
        if (FLYING_SLOT_INDICES.some((s) => type[s] !== 0)) anyFlying = true;
      }
      const formationCount = readVar(vm, 'formation-count');
      keyUp(vm, 'g');
      return { anyFlying, formationCount };
    },
    assert(obs) {
      assert.equal(
        obs.anyFlying,
        false,
        'while G is held no normal flying enemy ever enters the flying band (the normal stream is isolated)',
      );
      assert.equal(
        obs.formationCount,
        0,
        'while G is held the formation-wave count is pinned at 0 so the flying spawner brings in nothing',
      );
    },
    // Empty `debug ground spawn` so the flying-stream suppression that lives inside it (the formation-count
    // zero + flying-band clear) is gone → the normal schedule spawns flying formations again within the
    // window → anyFlying becomes true → the isolation assertion bites. Normal play never suppresses the
    // stream, so nothing masks the mutation.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'debug ground spawn'),
  },
  {
    key: 'debug-pause-key-freezes-and-resumes-the-walk',
    behavior:
      'The temporary debug pause key (P) is a freeze/resume TOGGLE: a tap freezes the whole tick so the walk stops advancing (letting the operator screenshot a ground-enemy issue), and a second tap resumes it (tracked for removal, #119)',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 1); // warm the walk live once so `tick` is advancing
      // `tick` advances only inside ADVANCE_SLOTS, which runs only while NOT paused (game_director.py wraps
      // the whole walk-loop body in `if debug paused == 0`, with the pause toggle running first and OUTSIDE
      // that gate). So a frozen `tick` == a frozen screen. P is a rising-edge TAP toggle, so tapKey (one down
      // pump, one up pump) flips it exactly once.
      tapKey(vm, 'p'); // first tap -> paused
      const paused = readVar(vm, 'debug-paused');
      const tickAtPause = readVar(vm, 'tick');
      step(vm, 5); // P no longer held; the walk must stay frozen across every pump
      const tickWhilePaused = readVar(vm, 'tick');
      tapKey(vm, 'p'); // second tap -> resume
      const resumed = readVar(vm, 'debug-paused');
      step(vm, 3);
      const tickAfterResume = readVar(vm, 'tick');
      return { paused, tickAtPause, tickWhilePaused, resumed, tickAfterResume };
    },
    assert(obs) {
      assert.equal(obs.paused, 1, 'a tap of P engages the freeze (debug paused == 1)');
      assert.equal(
        obs.tickWhilePaused,
        obs.tickAtPause,
        'while frozen the walk does not advance (tick is held across the paused pumps)',
      );
      assert.equal(obs.resumed, 0, 'a second tap of P releases the freeze (debug paused == 0)');
      assert.ok(
        obs.tickAfterResume > obs.tickAtPause,
        'after the resume tap the walk advances again (tick climbs)',
      );
    },
    // Empty `debug pause toggle` so a P tap never flips `debug paused` → it stays 0 → the walk runs through
    // the "paused" pumps → tick advances while we expect it frozen → the freeze assertion bites. (The resume
    // path is vacuously fine because the walk was never frozen; the freeze assertion is the one that catches.)
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'debug pause toggle'),
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
    key: 'bomb-kills-ground-and-scores',
    behavior:
      'A bomb whose locked target overlaps an active ground object resolves the hit through the single score path: the score rises by exactly the object value once and the object is marked struck',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      // Freeze the walk so the manual `check ground hit` call is the ONLY thing that runs on the step:
      // with the live ground spawner (area.ground-dispatch), an un-frozen pump would consume the area
      // schedule and stamp OTHER ground objects into the band mid-step, contaminating this detector
      // unit test. Frozen, no spawn/scroll runs; the callProc-pushed detector still executes. Clear the
      // ground band first so any object spawned before `playing` was reached is not swept either.
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      // Seed an ACTIVE Barra in the last ground slot (Scratch slot 16 -> JS index 15) and the locked
      // bomb target (Scratch slot 33 -> JS index 32) at the SAME cell. slot pts 6 is the Barra's
      // 1-based value-table position (100 pts). The detector has no in-project caller yet (the
      // bomb-finish wiring is a later commit), so run it directly with callProc, then step once.
      put('slot-type', 15, 30); // Barra ground-object marker (0x1E); its renderer arrives a later commit
      put('slot-state', 15, 1); // ACTIVE
      put('slot-pts', 15, 6);
      put('slot-x', 15, 5120);
      put('slot-y', 15, 4096);
      put('slot-x', 32, 5120);
      put('slot-y', 32, 4096);
      const award = readVar(vm, 'eco-value-table')[5]; // value-table position 6 -> JS index 5 = 100
      const score0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      return {
        delta: readVar(vm, 'eco-score') - score0,
        award,
        objState: readVar(vm, 'slot-state')[15],
      };
    },
    assert(obs) {
      assert.equal(obs.award, 100, 'the seeded Barra is worth its 100-pt value-table entry');
      assert.equal(obs.delta, obs.award, 'the bomb scores exactly the ground object value once');
      assert.equal(obs.objState, 2, 'the struck object is marked HIT (state 2), so it cannot re-score');
    },
    // Empty the bomb-vs-ground detector so no overlap is ever resolved → the score never rises.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'check ground hit'),
  },
  {
    key: 'bomb-ground-window-bounded',
    behavior:
      'The bomb-vs-ground hit box is the reference shadow window (scroll axis ±10, lateral ±5): an object under the target scores, one at the window edge scores, one past it on EITHER axis does not',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const SH = 16; // SLOT_UNITS_PER_SHADOW: one shadow half-pixel is 16 slot units
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      // Freeze the walk and clear the ground band so the live ground spawner cannot stamp other objects
      // into the band mid-step: each probe's `check ground hit` then sees exactly the one seeded object
      // (see bomb-kills-ground-and-scores for the same isolation).
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      // Target on shadow-aligned cells so each probe's shadow delta is exact. The detector floors each
      // position to its shadow MSB, then tests scroll-axis delta = sh(target_x)-sh(obj_x) in [-10, 9]
      // and lateral delta = sh(obj_y)-sh(target_y) in [-5, 4]. Each scoring probe frees nothing (it
      // marks the object HIT), so every probe re-seeds the object ACTIVE first.
      const tX = 5120,
        tY = 4096;
      const probe = (dy, dx) => {
        put('slot-type', 15, 30);
        put('slot-state', 15, 1);
        put('slot-pts', 15, 6);
        put('slot-x', 15, tX - dy * SH);
        put('slot-y', 15, tY + dx * SH);
        put('slot-x', 32, tX);
        put('slot-y', 32, tY);
        const s0 = readVar(vm, 'eco-score');
        callProc(vm, 'Stage', 'check ground hit');
        step(vm, 1);
        return readVar(vm, 'eco-score') - s0;
      };
      return {
        award: readVar(vm, 'eco-value-table')[5],
        center: probe(0, 0),
        yHi: probe(9, 0),
        yHiOut: probe(10, 0),
        yLo: probe(-10, 0),
        yLoOut: probe(-11, 0),
        xHi: probe(0, 4),
        xHiOut: probe(0, 5),
        xLo: probe(0, -5),
        xLoOut: probe(0, -6),
        // Far off on one axis while dead-on the other: a dropped bound (the reporter-steal bug)
        // would make one axis always-hit, so these MUST miss.
        farY: probe(-40, 0),
        farX: probe(0, -40),
      };
    },
    assert(obs) {
      assert.equal(obs.center, obs.award, 'dead-on the target scores');
      assert.equal(obs.yHi, obs.award, 'the scroll-axis high edge (+9) scores');
      assert.equal(obs.yLo, obs.award, 'the scroll-axis low edge (-10) scores');
      assert.equal(obs.xHi, obs.award, 'the lateral high edge (+4) scores');
      assert.equal(obs.xLo, obs.award, 'the lateral low edge (-5) scores');
      assert.equal(obs.yHiOut, 0, 'one past the scroll-axis high edge (+10) does NOT score');
      assert.equal(obs.yLoOut, 0, 'one past the scroll-axis low edge (-11) does NOT score');
      assert.equal(obs.xHiOut, 0, 'one past the lateral high edge (+5) does NOT score');
      assert.equal(obs.xLoOut, 0, 'one past the lateral low edge (-6) does NOT score');
      assert.equal(obs.farY, 0, 'far off the scroll axis does NOT score (both axes bind)');
      assert.equal(obs.farX, 0, 'far off the lateral axis does NOT score (both axes bind)');
    },
    // Widen the scroll-axis high bound (`> 9`, unique to the ground detector's 16 unrolled slots) so a
    // probe one past the edge now scores → the yHiOut miss assertion fails, proving the bound binds.
    negativeMutation: (p) => mutate.raiseGreaterThreshold(p, 'Stage', '9', '40'),
  },
  {
    key: 'bomb-crosshair-leads-craft',
    behavior:
      'The bomb crosshair (slot 35) leads the craft by a fixed 96-px (-3072 unit) forward depth offset each tick while sharing the craft column — the reticle sits ahead of the craft (init_bombing solvalou_X + 0xF400), not on it',
    playtestStep: 3,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2); // let the walk cache the craft cell and track the crosshair
      const row = readVar(vm, 'player-row');
      const col = readVar(vm, 'player-col');
      return {
        crosshairX: readVar(vm, 'slot-x')[34], // Scratch slot 35 -> JS index 34
        crosshairY: readVar(vm, 'slot-y')[34],
        crosshairState: readVar(vm, 'slot-state')[34],
        leadExpect: row * 256 - 3072, // craft depth + BOMB_TARGET_LEAD (-12 cells * 256)
        lateralExpect: col * 256,
      };
    },
    assert(obs) {
      assert.equal(
        obs.crosshairX,
        obs.leadExpect,
        'the crosshair leads the craft by -3072 units (96 px forward)',
      );
      assert.equal(
        obs.crosshairY,
        obs.lateralExpect,
        'the crosshair shares the craft column (laterally aligned)',
      );
      assert.equal(obs.crosshairState, 1, 'the crosshair slot is active (drawn)');
    },
    // Zero the forward lead so the crosshair sits on the craft (row*256) → the lead assertion fails.
    negativeMutation: (p) => mutate.changeAddLiteral(p, 'Stage', -3072, 0),
  },
  {
    key: 'bomb-target-locks-ahead',
    behavior:
      'Arming a bomb locks the bomb target (slot 33) at the crosshair lead ahead of the craft and drops the bomb (slot 34) from the craft depth behind it — the target is set from the sight, never steered by the player',
    playtestStep: 3,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const row = readVar(vm, 'player-row');
      const col = readVar(vm, 'player-col');
      // Freeze the walk so ONE advance-bomb call is exactly one tick: a settling pump otherwise runs
      // the walk ~220 iterations and flies the bomb to completion (see the harness pacing note).
      writeVar(vm, 'game-director-state', 'frozen');
      keyDown(vm, 'b');
      callProc(vm, 'Stage', 'advance bomb'); // arm tick — the if/else arms only, no advance
      step(vm, 1);
      keyUp(vm, 'b');
      return {
        targetX: readVar(vm, 'slot-x')[32], // Scratch slot 33 -> JS index 32
        targetY: readVar(vm, 'slot-y')[32],
        bombX: readVar(vm, 'slot-x')[33], // Scratch slot 34 -> JS index 33
        inFlight: readVar(vm, 'weapon-bomb-in-flight'),
        leadExpect: row * 256 - 3072,
        lateralExpect: col * 256,
        craftDepth: row * 256,
      };
    },
    assert(obs) {
      assert.equal(obs.inFlight, 1, 'the bomb arms into flight');
      assert.equal(
        obs.targetX,
        obs.leadExpect,
        'the bomb target locks at the crosshair lead (96 px ahead of the craft)',
      );
      assert.equal(obs.targetY, obs.lateralExpect, 'the bomb target shares the craft column');
      assert.equal(
        obs.bombX,
        obs.craftDepth,
        'the bomb drops from the craft depth, behind the locked target',
      );
    },
    // Zero the lead so the sight (and thus the locked target) sits on the craft → the lock assertion
    // fails (target == craft depth == bomb, the "lands at the craft" regression).
    negativeMutation: (p) => mutate.changeAddLiteral(p, 'Stage', -3072, 0),
  },
  {
    key: 'bomb-finish-resolves-ground',
    behavior:
      "An in-flight bomb reaching its target (target_x >= bomb_x) resolves ground objects under the locked target through advance-bomb's own finish path — the score rises by the object value once and the weapon clears — proving the finish is wired to check-ground-hit (not just the detector in isolation)",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      // Freeze the walk and hand-drive one advance-bomb tick (see the harness pacing note).
      writeVar(vm, 'game-director-state', 'frozen');
      // Clear the ground band first — mirroring bomb-kills-ground-and-scores. The pre-freeze live pump
      // (reachPlaying + step) runs the area ground spawner, which can leave an ACTIVE object in the band;
      // if one lands inside the bomb's shadow window it would be swept by the finish too, double-scoring.
      // (This surfaced when slice-11's aerial families added during-play behavior that shifts spawn timing
      // — memory: live behavior contaminates older step()-through-play scenarios. Freeze + clear re-isolates
      // so ONLY the hand-seeded object is under the target.)
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      // Seed an ACTIVE Barra (pts pos 6 -> 100) at a cell, the locked bomb target dead-on it, and the
      // in-flight bomb one sub-step from catching the target (target just behind the bomb).
      const gx = 5120;
      const gy = 4096;
      put('slot-type', 15, 30); // Barra ground-object marker (0x1E), last ground slot (16 -> idx 15)
      put('slot-state', 15, 1); // ACTIVE
      put('slot-pts', 15, 6);
      put('slot-x', 15, gx);
      put('slot-y', 15, gy);
      put('slot-x', 32, gx); // bomb target (slot 33) dead-on the object
      put('slot-y', 32, gy);
      put('slot-state', 32, 1);
      put('slot-x', 33, gx + 4); // bomb (slot 34) just ahead; sub-step 1 pulls it back to gx
      put('slot-state', 33, 1);
      writeVar(vm, 'weapon-bomb-in-flight', 1);
      writeVar(vm, 'weapon-bomb-dx', 0);
      const award = readVar(vm, 'eco-value-table')[5]; // value-table position 6 -> JS index 5 = 100
      const score0 = readVar(vm, 'eco-score');
      // Sub-step 1: dx -= 2 (bomb -> gx), target += 16 (-> gx+16); target >= bomb now holds, so the
      // finish fires check-ground-hit and clears the weapon.
      callProc(vm, 'Stage', 'advance bomb');
      step(vm, 1);
      return {
        award,
        delta: readVar(vm, 'eco-score') - score0,
        objState: readVar(vm, 'slot-state')[15],
        inFlight: readVar(vm, 'weapon-bomb-in-flight'),
        targetState: readVar(vm, 'slot-state')[32],
        bombState: readVar(vm, 'slot-state')[33],
      };
    },
    assert(obs) {
      assert.equal(obs.award, 100, 'the seeded Barra is worth its 100-pt value-table entry');
      assert.equal(obs.delta, obs.award, "the bomb's finish scores the ground object exactly once");
      assert.equal(obs.objState, 2, 'the struck object is marked HIT (state 2)');
      assert.equal(obs.inFlight, 0, 'the finish clears the weapon so it can re-arm');
      assert.equal(obs.targetState, 0, 'the bomb target slot is cleared at finish');
      assert.equal(obs.bombState, 0, 'the bomb slot is cleared at finish');
    },
    // Empty check-ground-hit: the finish still clears the weapon but nothing scores → delta 0 fails
    // (the finish wiring runs, but the resolved-hit path it calls is gone).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'check ground hit'),
  },
  {
    key: 'ground-dispatch-spawns-scoped',
    behavior:
      'Playing through the opening areas spawns the built ground families (Barra 0x1E in area 1, Logram 0x26 in area 2, Garu Barra 0x20 in area 3) into the ground band (slots 1-16) via add_ground_object — ACTIVE, at the family score position, with the Logram capturing the live Logram fire mask — while any unhandled/unbuilt ground type is scoped out (never stamped into a slot). Zolbak 0x1F, Derota 0x1B and Garu Derota 0x21 (slice 12), the Boza Logram 0x2D (GND-05), the Grobda roster 0x2C/0x35-0x40 (GND-06) and the Domogram 0x2E (GND-07, via add_domogram_with_path) are now all built, so they legitimately reach slots too and are in scope here — no slice-13 ground leaf remains out of scope. The Sol Tower 0x1D (SEC-01, slice 14) is also built now and spawns from the schedule, so it too is in scope.',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Live free-run across the opening areas (like area-clock-scheduler / fire-permission-masks): as
      // each area scrolls it consumes add_ground_object records. The families this scenario asserts on
      // first spawn in different areas — Barra (0x1E) in area 1, Logram (0x26) in area 2, Garu Barra
      // (0x20) in area 3 — interleaved with the now-built slice-12 turret/dome families (Zolbak 0x1F,
      // Derota 0x1B, Garu Derota 0x21), the now-built Boza Logram (0x2D, GND-05) and the now-built Grobda
      // roster (0x2C + 0x35-0x40, GND-06) and the now-built Domogram (0x2E, GND-07, via add_domogram_with_path),
      // which are all in scope; only a truly unhandled/unbuilt type must never reach a slot. The Logram fire mask (record 2,
      // value 0x25) is set before the first Logram, so a
      // spawned Logram captures it; read the slot mask and the Stage mask in the SAME settled sample so
      // the compare is consistent even as later areas re-set the mask. Garu Barra spawns two adjacent
      // slots sharing type 0x20 — the destructible node (ACTIVE) and the indestructible base (state
      // sentinel SLOT_GARU_BASE = 3); both are in-scope here.
      // GND-06 (ground.grobda #88): the 12 live Grobda type codes (0x2C=44 + 0x35..0x40, skipping the null
      // 0x37=55) — decimal, matching the readVar('slot-type') values. Kept local to this scope-gate scenario.
      const GROBDA_TYPES = new Set([44, 53, 54, 56, 57, 58, 59, 60, 61, 62, 63, 64]);
      let barraSeen = false;
      let logramSeen = false;
      let garuSeen = false;
      let barraOk = false;
      let logramOk = false;
      let onlyHandledTypes = true;
      let logramSlotMask = null;
      let logramStageMask = null;
      const types = readVar(vm, 'slot-type');
      const states = readVar(vm, 'slot-state');
      const pts = readVar(vm, 'slot-pts');
      const fmask = readVar(vm, 'slot-fire-mask');
      // Pace-invariant termination. The harness pumps by wall-clock budget, so a fixed pump count is a
      // machine-speed-dependent proxy for scroll depth — on a slower or more heavily loaded runner each
      // pump advances less game, so a fixed budget can stop before the deeper areas (and the area-3 Garu
      // spawn) are reached. Instead: scan until every in-scope family has been seen (early exit on a fast
      // machine) or until the run has advanced past area 3 (area number >= 5, so area 3 was fully
      // traversed on ANY machine), whichever comes first. HARD_CAP only guards a build that never
      // advances the area; the negative fixture's broken dispatch stamps nothing but still lets the area
      // clock run, so it exits at the area bound with nothing seen and the assertions below fail, as they
      // must.
      const HARD_CAP = 5000;
      for (let i = 0; i < HARD_CAP; i += 1) {
        step(vm, 1);
        for (let s = 0; s < 16; s += 1) {
          // ground band = Scratch slots 1..16 -> JS indices 0..15
          const t = types[s];
          if (t === 0) continue;
          if (t === 30) {
            barraSeen = true;
            if (states[s] === 1 && pts[s] === 6) barraOk = true;
          } else if (t === 32) {
            // Garu Barra: node (state ACTIVE) or base (state SLOT_GARU_BASE = 3), both in-scope.
            garuSeen = true;
          } else if (t === 38) {
            logramSeen = true;
            if (states[s] === 1 && pts[s] === 10) {
              logramOk = true;
              if (logramSlotMask === null) {
                logramSlotMask = fmask[s];
                logramStageMask = readVar(vm, 'fire-mask-logram');
              }
            }
          } else if (t === 29) {
            // SEC-01 (ground.sol-tower #90, slice 14): the hidden Sol Tower (0x1D) is now built and spawns
            // from the schedule like a single-slot ground family (invisible until bombed, then it rises and
            // becomes a bombable target), so seeing one in a slot is in scope. Its reveal / rise / two-stage
            // scoring behaviour is proved by the dedicated sol-tower-* scenarios; here we only assert it is
            // not treated as unhandled leakage.
          } else if (t === 31 || t === 27 || t === 33 || t === 45) {
            // Zolbak (0x1F), Derota (0x1B), Garu Derota (0x21) are the slice-12 ground families, and the
            // Boza Logram (0x2D, GND-05) is a slice-13 family built earlier this slice — they are all now
            // handled types that legitimately reach a slot (the Boza stamps all five of its slots — four
            // outers + one centre — under 0x2D), so seeing them is in scope (not a "scoped out" violation).
            // Their own behaviour is proved by the dedicated zolbak-/derota-/garu-derota-/boza-* scenarios;
            // here we only assert they are not treated as unhandled leakage.
          } else if (GROBDA_TYPES.has(t)) {
            // GND-06 (ground.grobda #88, this PR): the 12 Grobda variants (0x2C + 0x35-0x40, skipping the
            // null 0x37) are now built and spawn from the schedule like any single-slot ground family, so
            // seeing one in a slot is in scope. Their reticle reaction / motion / land-crater-vs-water-vanish
            // behaviour is proved by the dedicated grobda-* scenarios; here we only assert they are not
            // treated as unhandled leakage.
          } else if (t === 46) {
            // GND-07 (ground.domogram #89, this PR): the Domogram (0x2E) is now built and spawns from the
            // schedule via add_domogram_with_path like a single-slot ground family (it then follows its
            // scripted path and fires), so seeing one in a slot is in scope. Its path-follow / midpoint-fire
            // behaviour is proved by the dedicated domogram-* scenarios; here we only assert it is not
            // treated as unhandled leakage. With it built, no slice-13 ground leaf remains out of scope.
          } else {
            onlyHandledTypes = false;
          }
        }
        if (barraSeen && garuSeen && logramSeen && barraOk && logramOk) break;
        if (readVar(vm, 'area-number') >= 5) break;
      }
      return {
        barraSeen,
        logramSeen,
        garuSeen,
        barraOk,
        logramOk,
        onlyHandledTypes,
        logramSlotMask,
        logramStageMask,
      };
    },
    assert(obs) {
      assert.equal(obs.barraSeen, true, 'a Barra (0x1E) is spawned into the ground band');
      assert.equal(obs.garuSeen, true, 'a Garu Barra (0x20) is spawned into the ground band');
      assert.equal(obs.logramSeen, true, 'a Logram (0x26) is spawned into the ground band');
      assert.equal(obs.barraOk, true, 'the spawned Barra is ACTIVE at its 100-pt value position (6)');
      assert.equal(obs.logramOk, true, 'the spawned Logram is ACTIVE at its 300-pt value position (10)');
      assert.equal(
        obs.onlyHandledTypes,
        true,
        'no unhandled/unbuilt ground type is ever stamped into a slot (only built families spawn; with GND-07 Domogram built, no slice-13 ground leaf remains out of scope)',
      );
      assert.ok(obs.logramStageMask > 0, 'the schedule set a live Logram fire mask before the spawn');
      assert.equal(
        obs.logramSlotMask,
        obs.logramStageMask,
        "the spawned Logram captures the area's Logram fire mask into its slot",
      );
    },
    // Break the add_ground_object dispatch (its handler == comparison never matches) so no ground
    // object is ever stamped → barraSeen / logramSeen fail.
    negativeMutation: (p) =>
      mutate.changeEqualsOperand(p, 'Stage', 'add_ground_object', '__never__'),
  },
  {
    key: 'ground-object-scrolls-with-terrain',
    behavior:
      'A spawned ground object is terrain-locked: each tick advance-ground advances its scroll-axis position (slot x) by exactly AREA_PROGRESS_STEP (32 = +16 units/frame doubled) DOWN the field while its lateral column holds, and it is culled once it scrolls off the bottom (row >= 40)',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      // Freeze the walk so ONE advance-ground call is exactly one tick (a settling pump would run the
      // walk ~220 iterations; see the harness pacing note). Seed an ACTIVE Barra at the top of the
      // field (slot x 0) in the last ground slot (Scratch 16 -> JS index 15) and point the shared slot
      // cursor at it, then hand-drive advance-ground one tick at a time.
      writeVar(vm, 'game-director-state', 'frozen');
      const GY = 4096;
      put('slot-type', 15, 30); // Barra (0x1E)
      put('slot-state', 15, 1); // ACTIVE
      put('slot-pts', 15, 6);
      put('slot-x', 15, 0); // top of the field
      put('slot-y', 15, GY);
      writeVar(vm, 'slot-index', 16); // Scratch 1-based slot 16 -> the seeded object
      const xs = [];
      const ys = [];
      for (let t = 0; t < 3; t += 1) {
        callProc(vm, 'Stage', 'advance ground');
        step(vm, 1);
        xs.push(readVar(vm, 'slot-x')[15]);
        ys.push(readVar(vm, 'slot-y')[15]);
      }
      // Cull: re-seed the object one scroll step short of the bottom row (40*256 - 32), so the next
      // advance scrolls it to row 40 (>= CULL_ROW_MAX) and frees the slot (type/state -> 0).
      put('slot-type', 15, 30);
      put('slot-state', 15, 1);
      put('slot-x', 15, 40 * 256 - 32);
      writeVar(vm, 'slot-index', 16);
      callProc(vm, 'Stage', 'advance ground');
      step(vm, 1);
      return {
        xs,
        ys,
        culledType: readVar(vm, 'slot-type')[15],
        culledState: readVar(vm, 'slot-state')[15],
        gy: GY,
      };
    },
    assert(obs) {
      assert.deepEqual(
        obs.xs,
        [32, 64, 96],
        'the ground object scrolls DOWN by exactly 32 units/tick (terrain-locked)',
      );
      assert.deepEqual(
        obs.ys,
        [obs.gy, obs.gy, obs.gy],
        'the lateral column holds while it scrolls (only the scroll axis moves)',
      );
      assert.equal(obs.culledType, 0, 'an object scrolled off the bottom (row >= 40) is culled (type cleared)');
      assert.equal(obs.culledState, 0, 'the culled slot is freed (state cleared) so it can be reused');
    },
    // Zero the scroll step (the unique `operator_add` literal 32 on the Stage, in advance-ground) so
    // slot x never advances → the [32,64,96] drift assertion fails (the object is frozen in place).
    negativeMutation: (p) => mutate.changeAddLiteral(p, 'Stage', 32, 0),
  },
  {
    key: 'barra-craters-persists-and-scrolls',
    behavior:
      'A bombed Barra (state HIT) runs its explosion clock and becomes a PERSISTENT scrolling crater: `update barra` advances the clock (2 arcade frames/tick) AND keeps scrolling it with the terrain (32/tick), and — UNLIKE a flying kill, which frees on its clock at 20 frames — it is NEVER freed on the clock (it stays occupied and HIT well past both the 20-frame flying duration and the 56-frame crater start), removed only when it culls off the bottom of the field',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      // Freeze the walk so one `update barra` call is exactly one tick (a settling pump would run the
      // walk ~220 iterations and the live spawner would stamp other ground objects mid-step; see the
      // harness pacing + live-contamination notes). Clear the ground band, then seed a struck Barra at
      // the top of the field (slot x 0) in the last ground slot (Scratch 16 -> JS index 15), point the
      // shared cursor at it, and hand-drive `update barra` a tick at a time.
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      const GY = 4096;
      put('slot-type', 15, 30); // Barra (0x1E)
      put('slot-state', 15, 2); // HIT — the bomb has struck it; the crater clock starts here
      put('slot-pts', 15, 6);
      put('slot-x', 15, 0); // top of the field
      put('slot-y', 15, GY);
      put('slot-timer', 15, 0); // the detector zeroes the clock on the hit tick
      writeVar(vm, 'slot-index', 16); // Scratch 1-based slot 16 -> the seeded object
      const xs = [];
      const N = 30; // 30 ticks -> clock 60 frames: past the 20-frame flying free AND the 56-frame crater start
      for (let t = 0; t < N; t += 1) {
        callProc(vm, 'Stage', 'update barra');
        step(vm, 1);
        xs.push(readVar(vm, 'slot-x')[15]);
      }
      const persisted = {
        type: readVar(vm, 'slot-type')[15],
        state: readVar(vm, 'slot-state')[15],
        timer: readVar(vm, 'slot-timer')[15],
        x: readVar(vm, 'slot-x')[15],
      };
      // Cull: re-seed the crater one scroll step short of the bottom row (40*256 - 32), so the next
      // `update barra` scrolls it to row 40 (>= CULL_ROW_MAX) and frees the slot (type/state -> 0) —
      // the crater's ONLY removal path.
      put('slot-type', 15, 30);
      put('slot-state', 15, 2);
      put('slot-x', 15, 40 * 256 - 32);
      writeVar(vm, 'slot-index', 16);
      callProc(vm, 'Stage', 'update barra');
      step(vm, 1);
      return {
        xs,
        persisted,
        n: N,
        culledType: readVar(vm, 'slot-type')[15],
        culledState: readVar(vm, 'slot-state')[15],
      };
    },
    assert(obs) {
      assert.deepEqual(
        obs.xs.slice(0, 3),
        [32, 64, 96],
        'a struck Barra keeps scrolling DOWN by exactly 32 units/tick (the crater is terrain-locked)',
      );
      const monotonic = obs.xs.every((x, i) => i === 0 || x === obs.xs[i - 1] + 32);
      assert.equal(monotonic, true, 'the crater scrolls a steady 32/tick for the whole run');
      assert.equal(obs.persisted.timer, obs.n * 2, 'the crater clock keeps counting (2 frames/tick) and is never reset');
      assert.ok(obs.persisted.timer > 20, 'the clock runs past the 20-frame flying-explosion free without freeing');
      assert.ok(obs.persisted.timer > 56, 'the clock runs past the 56-frame crater start without freeing');
      assert.equal(obs.persisted.type, 30, 'the crater stays OCCUPIED on its clock (never freed like a flying kill)');
      assert.equal(obs.persisted.state, 2, 'the crater stays HIT on its clock (a persistent crater, not a vanishing burst)');
      assert.equal(obs.culledType, 0, 'a crater scrolled off the bottom (row >= 40) is finally culled (type cleared)');
      assert.equal(obs.culledState, 0, 'the culled crater slot is freed (state cleared) so it can be reused');
    },
    // Sever the Barra's whole per-tick update: with `update barra` neutralized, a struck Barra neither
    // advances its crater clock nor scrolls → the [32,64,96] drift and the clock-advance assertions fail
    // (no crater ever forms or moves).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update barra'),
  },
  {
    key: 'barra-blaster-cannot-destroy',
    behavior:
      'The blaster (air weapon) structurally cannot destroy a ground object: the shot-vs-air detector is dispatched only from FLYING enemy updates, so a Barra (routed to `update barra`) is never offered to it. The SAME controlled shot on the SAME cell scores an overlapping flying enemy but scores NOTHING against an overlapping ground Barra',
    playtestStep: 7,
    async drive(vm) {
      // Live-drive both probes exactly like `air-shot-hit-column-bounded`: the shot-vs-air detector is
      // dispatched from the live flying-enemy walk (a hand-called detector needs live warming, and driving
      // the real walk is what proves the routing anyway). Invuln stays ON from reachPlaying so the craft
      // never dies. Each probe fires an identical CONTROLLED shot in a real detector slot (SHOT_SLOTS =
      // 37-39, JS index 37) on a cell 6 rows / 8 columns off the craft — far enough that only the seeded
      // shot reaches the target, not the craft's own tapped shot.
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      const pr = readVar(vm, 'player-row'),
        pc = readVar(vm, 'player-col');
      const eRow = pr - 6,
        eCol = pc - 8;
      const clearBands = () => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
        for (let s = 58; s <= 63; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
        put('slot-type', 37, 0);
        put('slot-state', 37, 0);
      };
      const seedShot = () => {
        put('slot-type', 37, 1); // SHOT_TYPE controlled shot in a real detector slot
        put('slot-state', 37, 1);
        put('slot-x', 37, eRow * 256);
        put('slot-y', 37, eCol * 256);
      };
      // Positive control: an ACTIVE flying Toroid (slot 64 -> JS 63) under the shot IS destroyed + scores.
      // It is stationary (dx=dy=0) and flyers never scroll, so it stays put to be hit before the pump
      // settles.
      clearBands();
      put('slot-type', 63, 10); // Toroid
      put('slot-state', 63, 1); // ACTIVE
      put('slot-pts', 63, 3);
      put('slot-x', 63, eRow * 256);
      put('slot-y', 63, eCol * 256);
      put('slot-dx', 63, 0);
      put('slot-dy', 63, 0);
      put('slot-flag', 63, 9);
      put('slot-timer', 63, 0);
      put('slot-code', 63, 8);
      seedShot();
      const flyScore0 = readVar(vm, 'eco-score');
      step(vm, 1);
      const flyDelta = readVar(vm, 'eco-score') - flyScore0;
      // Immunity: the SAME shot over an ACTIVE ground Barra (slot 16 -> JS 15) scores nothing. The Barra
      // routes to `update barra`, never to the air detector, so it is never even offered for a shot hit.
      // (During the settling pump the terrain-locked Barra scrolls DOWN the field and is finally culled —
      // culling never scores, so the unchanged score is the immunity observable that survives the pump.)
      clearBands();
      put('slot-type', 15, 30); // Barra
      put('slot-state', 15, 1); // ACTIVE
      put('slot-pts', 15, 6);
      put('slot-x', 15, eRow * 256);
      put('slot-y', 15, eCol * 256);
      seedShot();
      const gndScore0 = readVar(vm, 'eco-score');
      step(vm, 1);
      return {
        flyDelta,
        gndDelta: readVar(vm, 'eco-score') - gndScore0,
        award: readVar(vm, 'eco-value-table')[2], // Toroid pts 3 -> value-table position 3 -> JS index 2
      };
    },
    assert(obs) {
      assert.ok(obs.award > 0, 'the control enemy is worth a positive value');
      assert.equal(obs.flyDelta, obs.award, 'control: the shot DOES destroy+score an overlapping flying enemy');
      assert.equal(obs.gndDelta, 0, 'the identical shot on the identical cell scores NOTHING against a ground Barra');
    },
    // Empty the shot-vs-air detector: the flying control no longer scores → the control assertion fails,
    // proving the shot mechanism (not a dead seed) is what the ground immunity is measured against.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'check air shot hit'),
  },
  {
    key: 'garu-node-scores-and-vanishes',
    behavior:
      "A Garu Barra node is the destructible half (state ACTIVE, worth 300): a bomb on its cell resolves through the shared ground detector for exactly its 300-pt value-table entry, and once struck (state HIT) `update garu` runs the node's burst clock and — mirroring the arcade's explode_and_remove_object, NOT the Barra crater — REMOVES the node when the burst finishes (timer >= GARU_REMOVE_FRAMES = 28, i.e. frame 7). It scrolls with the terrain while the burst plays, then vanishes leaving no persistent crater",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      const clearBand = () => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
      };
      // Freeze the walk so a manual detector/update call is the only thing that runs on the step (the
      // live ground spawner would otherwise stamp other objects into the band mid-step; see
      // bomb-kills-ground-and-scores / barra-craters for the same isolation).
      writeVar(vm, 'game-director-state', 'frozen');
      // --- Scoring: an ACTIVE node (type 32, pts pos 10 -> 300) under the locked bomb target scores 300.
      clearBand();
      put('slot-type', 15, 32); // Garu Barra (0x20); the node half is state ACTIVE
      put('slot-state', 15, 1); // ACTIVE (destructible)
      put('slot-pts', 15, 10); // 1-based value-table position of 300
      put('slot-x', 15, 5120);
      put('slot-y', 15, 4096);
      put('slot-x', 32, 5120); // locked bomb target (Scratch slot 33 -> JS index 32), same cell
      put('slot-y', 32, 4096);
      const award = readVar(vm, 'eco-value-table')[9]; // value-table position 10 -> JS index 9 = 300
      const score0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const scoreDelta = readVar(vm, 'eco-score') - score0;
      const nodeState = readVar(vm, 'slot-state')[15];
      // --- Death: a struck node (state HIT) bursts, scrolls, then REMOVES itself at frame 7 (no crater).
      clearBand();
      put('slot-type', 15, 32);
      put('slot-state', 15, 2); // HIT — the detector zeroed the burst clock on the hit tick
      put('slot-pts', 15, 10);
      put('slot-x', 15, 0); // top of the field
      put('slot-y', 15, 4096);
      put('slot-timer', 15, 0);
      writeVar(vm, 'slot-index', 16); // Scratch 1-based slot 16 -> the seeded node
      const snaps = [];
      const N = 14; // 14 ticks -> clock 28 (= GARU_REMOVE_FRAMES): the burst finishes and the node is removed
      for (let t = 0; t < N; t += 1) {
        callProc(vm, 'Stage', 'update garu');
        step(vm, 1);
        snaps.push({
          x: readVar(vm, 'slot-x')[15],
          type: readVar(vm, 'slot-type')[15],
          state: readVar(vm, 'slot-state')[15],
          timer: readVar(vm, 'slot-timer')[15],
        });
      }
      return { award, scoreDelta, nodeState, snaps };
    },
    assert(obs) {
      assert.equal(obs.award, 300, 'a Garu node (pts position 10) is worth its 300-pt value-table entry');
      assert.equal(obs.scoreDelta, obs.award, 'a bomb on the node cell scores exactly 300 once (shared ground detector)');
      assert.equal(obs.nodeState, 2, 'the struck node is marked HIT (state 2), so it cannot re-score');
      assert.deepEqual(
        obs.snaps.slice(0, 3).map((s) => s.x),
        [32, 64, 96],
        'the bursting node scrolls DOWN with the terrain (32/tick) while its burst plays',
      );
      const mid = obs.snaps[12]; // 13th tick: timer 26, still mid-burst
      assert.equal(mid.type, 32, 'mid-burst the node is still present (type held)');
      assert.equal(mid.state, 2, 'mid-burst the node is still HIT (bursting, not yet removed)');
      assert.equal(mid.timer, 26, 'the burst clock counts 2 frames/tick');
      const gone = obs.snaps[13]; // 14th tick: timer 28 = GARU_REMOVE_FRAMES -> removed
      assert.equal(gone.timer, 28, 'the node is removed exactly when its burst finishes (frame 7 = 28 frames)');
      assert.equal(gone.type, 0, 'the node VANISHES (type cleared) — no persistent crater, unlike the Barra');
      assert.equal(gone.state, 0, 'the removed node slot is freed (state cleared) so it can be reused');
    },
    // Sever the node's whole per-tick update: with `update garu` neutralized, a struck node neither bursts,
    // scrolls, nor removes itself → it persists stuck-HIT forever (a non-vanishing crater), so both the
    // scroll drift and the frame-7 removal assertions go red.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update garu'),
  },
  {
    key: 'garu-base-is-indestructible',
    behavior:
      "A Garu Barra base is the indestructible half: it is stamped a non-ACTIVE sentinel state (SLOT_GARU_BASE) that the ground detector's `== ACTIVE` gate rejects, so a bomb dead on the base scores NOTHING and never marks it struck — while the SAME bomb on the SAME cell destroys an ACTIVE node for 300, proving the detector is live and it is specifically the base's sentinel that is immune. On its own tick the base just scrolls with the terrain, persisting (never HIT, never clock-removed)",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      const clearBand = () => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
      };
      writeVar(vm, 'game-director-state', 'frozen'); // isolate each manual call (see the ground scenarios)
      // --- Immunity: the base (state SLOT_GARU_BASE = 3) dead on the bomb target scores nothing.
      clearBand();
      put('slot-type', 15, 32); // Garu Barra (0x20); the base half carries the sentinel state
      put('slot-state', 15, 3); // SLOT_GARU_BASE — the detector's `== ACTIVE (1)` gate excludes it
      put('slot-pts', 15, 10);
      put('slot-x', 15, 5120);
      put('slot-y', 15, 4096);
      put('slot-x', 32, 5120); // locked bomb target on the exact base cell
      put('slot-y', 32, 4096);
      const baseScore0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const baseDelta = readVar(vm, 'eco-score') - baseScore0;
      const baseState = readVar(vm, 'slot-state')[15];
      // --- Live control: an ACTIVE node on the identical cell DOES score 300, so the zero above is real
      // immunity, not a dead detector.
      clearBand();
      put('slot-type', 15, 32);
      put('slot-state', 15, 1); // ACTIVE node
      put('slot-pts', 15, 10);
      put('slot-x', 15, 5120);
      put('slot-y', 15, 4096);
      put('slot-x', 32, 5120);
      put('slot-y', 32, 4096);
      const nodeScore0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const nodeDelta = readVar(vm, 'eco-score') - nodeScore0;
      // --- Persistence: the base's own tick just scrolls it (never HIT, never removed on a clock).
      clearBand();
      put('slot-type', 15, 32);
      put('slot-state', 15, 3); // SLOT_GARU_BASE
      put('slot-x', 15, 0); // top of the field
      put('slot-y', 15, 4096);
      writeVar(vm, 'slot-index', 16);
      const xs = [];
      for (let t = 0; t < 3; t += 1) {
        callProc(vm, 'Stage', 'update garu');
        step(vm, 1);
        xs.push(readVar(vm, 'slot-x')[15]);
      }
      return {
        baseDelta,
        baseState,
        nodeDelta,
        award: readVar(vm, 'eco-value-table')[9],
        xs,
        persistType: readVar(vm, 'slot-type')[15],
        persistState: readVar(vm, 'slot-state')[15],
      };
    },
    assert(obs) {
      assert.equal(obs.baseDelta, 0, 'a bomb dead on the Garu base scores NOTHING (its sentinel state fails the ACTIVE gate)');
      assert.equal(obs.baseState, 3, 'the base is never marked struck — it keeps its SLOT_GARU_BASE sentinel');
      assert.equal(obs.award, 300, 'the control node is worth a positive 300-pt value');
      assert.equal(obs.nodeDelta, obs.award, 'control: the SAME bomb on the SAME cell destroys+scores an ACTIVE node');
      assert.deepEqual(obs.xs, [32, 64, 96], 'the base scrolls DOWN with the terrain (32/tick) on its own tick');
      assert.equal(obs.persistType, 32, 'the base persists OCCUPIED (never consumed by a bomb)');
      assert.equal(obs.persistState, 3, 'the base persists as the sentinel (never flips to HIT)');
    },
    // Empty the ground detector: the control node no longer scores (nodeDelta 0) → the control assertion
    // fails, proving the base's zero is measured against a genuinely live detector (not a dead seed).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'check ground hit'),
  },
  {
    key: 'logram-fires-once-at-full-open',
    behavior:
      'An armed Logram (0x26) opens and closes on a dome cycle and fires EXACTLY ONE aimed bullet at the midpoint (fully-open dome), mirroring handle_logram_main ($1B64): `update logram` counts its ANIMATE timer up, writes the dome costume ordinal for each stage (the open/peak/close triangle 1→4→1), allocates one aimed shot the single tick the timer hits 12 (stage 3, dome fully open), and at stage 7 re-rolls a fresh masked-random wait and returns to WAIT — it does NOT fire every animating tick, nor at the wrong stage',
    playtestStep: 5,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      // Freeze the walk so one `update logram` call is exactly one tick (a settling pump would run the walk
      // ~220 iterations and the live spawner would stamp other ground objects mid-step; see the harness
      // pacing + live-contamination notes). Clear the ground band, pin `tick` to a phase-gate multiple so
      // the every-4th-tick cadence gate passes on every hand-driven call, and lift the ground-stop-firing
      // row well above the object's row so the arm gate stays satisfied through the whole cycle.
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      writeVar(vm, 'tick', 0); // on-phase (tick mod 4 == 0) true for every manual call
      writeVar(vm, 'ground-stop-firing-row', 100); // armed regardless of the slow scroll
      const pc = readVar(vm, 'player-col');
      // Seed one ACTIVE Logram already in the ANIMATE phase with its timer at 0, in the last ground slot
      // (Scratch 16 -> JS index 15), fire mask 0 (so the recycle re-roll is the deterministic (rng%1)+1 = 1)
      // at the top of the field (row 0), and point the shared cursor at it.
      put('slot-type', 15, 38); // Logram (0x26)
      put('slot-state', 15, 1); // ACTIVE
      put('slot-pts', 15, 10);
      put('slot-flag', 15, 1); // ANIMATE phase
      put('slot-fire-timer', 15, 0); // start of the up-count
      put('slot-fire-mask', 15, 0); // recycle re-roll => (rng mod 1) + 1 = 1
      put('slot-x', 15, 0); // row 0 (armed)
      put('slot-y', 15, pc * 256);
      put('slot-code', 15, 1); // closed dome
      writeVar(vm, 'slot-index', 16); // Scratch 1-based slot 16 -> the seeded Logram
      // Drive one full ANIMATE cycle (timer 1..27, then the stage-7 recycle at 28) a tick at a time,
      // resetting the shared alloc signal before each call so a fire is attributed to the exact tick.
      const codes = [];
      const fireAt = [];
      for (let t = 0; t < 28; t += 1) {
        writeVar(vm, 'bullet-alloc-result', 0);
        callProc(vm, 'Stage', 'update logram');
        step(vm, 1);
        codes.push(readVar(vm, 'slot-code')[15]);
        if (readVar(vm, 'bullet-alloc-result') > 0) {
          fireAt.push({ call: t + 1, timer: readVar(vm, 'slot-fire-timer')[15], code: codes[t] });
        }
      }
      return {
        codes: codes.slice(0, 27), // calls 1..27 span stages 0..6; call 28 is the recycle
        fireAt,
        flagAfter: readVar(vm, 'slot-flag')[15],
        timerAfter: readVar(vm, 'slot-fire-timer')[15],
      };
    },
    assert(obs) {
      assert.deepEqual(
        obs.fireAt,
        [{ call: 12, timer: 12, code: 4 }],
        'the Logram fires EXACTLY ONCE per cycle, on the single tick its timer hits 12 (stage 3, dome fully open ordinal 4) — not every animating tick and not at the wrong stage',
      );
      assert.deepEqual(
        obs.codes,
        [1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 3, 3, 3, 3, 2, 2, 2, 2, 1, 1, 1, 1],
        'the dome costume ordinal walks the open→peak→close triangle across the 7 stages (closed 1 → fully open 4 → closed 1)',
      );
      assert.equal(obs.flagAfter, 0, 'at stage 7 the Logram recycles back to the WAIT phase');
      assert.equal(obs.timerAfter, 1, 'the recycle re-rolls a fresh masked-random wait (mask 0 => (rng mod 1) + 1 = 1)');
    },
    // Break the single-shot fire guard `item(slot index) of (slot fire timer) == 12` (both copies — the
    // WAIT->ANIMATE fall-through and the steady ANIMATE branch) so the timer never triggers a shot → no
    // bullet ever allocates → fireAt is empty → the exactly-once assertion bites. (The animation and
    // recycle still run, so this isolates the fire, not the whole proc.)
    negativeMutation: (p) => mutate.changeListItemEqualsOperand(p, 'Stage', 'slot fire timer', 12, 999),
  },
  {
    key: 'logram-craters-when-bombed',
    behavior:
      'A bombed Logram (state HIT) craters PERSISTENTLY exactly like a Barra (handle_bomb_explosion $3186, the SAME routine — NOT the Garu node explode-and-remove): `update logram` advances the crater clock (`slot timer`, 2 arcade frames/tick) AND keeps scrolling it with the terrain (32/tick), never freeing it on the clock (it stays occupied and HIT well past the 20-frame flying free and the 56-frame crater start), removed only when it culls off the bottom of the field',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      // Freeze the walk so one `update logram` call is exactly one tick (see the Barra crater scenario for
      // the identical isolation). Clear the ground band, then seed a struck Logram at the top of the field
      // (slot x 0) in the last ground slot (Scratch 16 -> JS index 15) and hand-drive `update logram`.
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      put('slot-type', 15, 38); // Logram (0x26)
      put('slot-state', 15, 2); // HIT — the bomb has struck it; the crater clock starts here
      put('slot-pts', 15, 10);
      put('slot-x', 15, 0); // top of the field
      put('slot-y', 15, 4096);
      put('slot-timer', 15, 0); // the detector zeroes the clock on the hit tick
      writeVar(vm, 'slot-index', 16);
      const xs = [];
      const N = 30; // 30 ticks -> clock 60 frames: past the 20-frame flying free AND the 56-frame crater start
      for (let t = 0; t < N; t += 1) {
        callProc(vm, 'Stage', 'update logram');
        step(vm, 1);
        xs.push(readVar(vm, 'slot-x')[15]);
      }
      const persisted = {
        type: readVar(vm, 'slot-type')[15],
        state: readVar(vm, 'slot-state')[15],
        timer: readVar(vm, 'slot-timer')[15],
      };
      // Cull: re-seed the crater one scroll step short of the bottom row (40*256 - 32), so the next
      // `update logram` scrolls it to row 40 (>= CULL_ROW_MAX) and frees the slot — the crater's ONLY exit.
      put('slot-type', 15, 38);
      put('slot-state', 15, 2);
      put('slot-x', 15, 40 * 256 - 32);
      writeVar(vm, 'slot-index', 16);
      callProc(vm, 'Stage', 'update logram');
      step(vm, 1);
      return {
        xs,
        persisted,
        n: N,
        culledType: readVar(vm, 'slot-type')[15],
        culledState: readVar(vm, 'slot-state')[15],
      };
    },
    assert(obs) {
      assert.deepEqual(
        obs.xs.slice(0, 3),
        [32, 64, 96],
        'a struck Logram keeps scrolling DOWN by exactly 32 units/tick (the crater is terrain-locked)',
      );
      const monotonic = obs.xs.every((x, i) => i === 0 || x === obs.xs[i - 1] + 32);
      assert.equal(monotonic, true, 'the crater scrolls a steady 32/tick for the whole run');
      assert.equal(obs.persisted.timer, obs.n * 2, 'the crater clock keeps counting (2 frames/tick) and is never reset');
      assert.ok(obs.persisted.timer > 56, 'the clock runs past the 56-frame crater start without freeing (persistent, like the Barra)');
      assert.equal(obs.persisted.type, 38, 'the crater stays OCCUPIED on its clock (never freed like a flying kill or the Garu node)');
      assert.equal(obs.persisted.state, 2, 'the crater stays HIT on its clock (a persistent crater, not a vanishing burst)');
      assert.equal(obs.culledType, 0, 'a crater scrolled off the bottom (row >= 40) is finally culled (type cleared)');
      assert.equal(obs.culledState, 0, 'the culled crater slot is freed (state cleared) so it can be reused');
    },
    // Sever the Logram's whole per-tick update: with `update logram` neutralized, a struck Logram neither
    // advances its crater clock nor scrolls → the [32,64,96] drift and the clock-advance assertions fail.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update logram'),
  },
  {
    key: 'zolbak-craters-and-reduces-ai',
    behavior:
      "A Zolbak (0x1F) is the Barra crater model with ONE extra behaviour: a bomb on an ACTIVE Zolbak scores its 200-pt value once through the shared ground detector, and once struck (state HIT) `update zolbak` — on the FIRST HIT tick only (uniquely marked by `slot timer == 0`, since the detector zeroed the clock and the update only climbs it afterwards) — reduces the adaptive enemy AI level by EXACTLY 2 (handle_1F_Zolbak -> reduce_enemy_ai_by_2), then craters PERSISTENTLY exactly like a Barra (scrolls 32/tick, clock counts 2/tick, never freed on the clock). The AI drop happens ONCE per kill, never again on the crater ticks",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      // Freeze the walk so each manual call is exactly one tick (a settling pump would run the walk ~220
      // iterations and the live spawner would stamp other ground objects mid-step; see the Barra crater
      // scenario for the identical isolation).
      writeVar(vm, 'game-director-state', 'frozen');
      const clearBand = () => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
      };
      // --- Scoring: an ACTIVE Zolbak (type 31, pts pos 8 -> 200) under the locked bomb target scores 200.
      clearBand();
      put('slot-type', 15, 31); // Zolbak (0x1F)
      put('slot-state', 15, 1); // ACTIVE (destructible)
      put('slot-pts', 15, 8); // 1-based value-table position of 200
      put('slot-x', 15, 5120);
      put('slot-y', 15, 4096);
      put('slot-x', 32, 5120); // locked bomb target (Scratch slot 33 -> JS index 32), same cell
      put('slot-y', 32, 4096);
      const award = readVar(vm, 'eco-value-table')[7]; // value-table position 8 -> JS index 7 = 200
      const score0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const scoreDelta = readVar(vm, 'eco-score') - score0;
      // --- AI reduction + crater: a struck Zolbak drops the AI level once, then craters persistently.
      clearBand();
      writeVar(vm, 'difficulty-ai-level', 6); // a known live AI level to watch fall
      put('slot-type', 15, 31);
      put('slot-state', 15, 2); // HIT — the detector zeroed the clock on the hit tick
      put('slot-pts', 15, 8);
      put('slot-x', 15, 0); // top of the field
      put('slot-y', 15, 4096);
      put('slot-timer', 15, 0);
      writeVar(vm, 'slot-index', 16); // Scratch 1-based slot 16 -> the seeded Zolbak
      const xs = [];
      const N = 30; // 30 ticks -> clock 60 frames: past the 20-frame flying free AND the 56-frame crater start
      let aiAfterFirst = null;
      for (let t = 0; t < N; t += 1) {
        callProc(vm, 'Stage', 'update zolbak');
        step(vm, 1);
        xs.push(readVar(vm, 'slot-x')[15]);
        if (t === 0) aiAfterFirst = readVar(vm, 'difficulty-ai-level');
      }
      return {
        award,
        scoreDelta,
        aiBefore: 6,
        aiAfterFirst,
        aiAfterAll: readVar(vm, 'difficulty-ai-level'),
        xs,
        n: N,
        persistedType: readVar(vm, 'slot-type')[15],
        persistedState: readVar(vm, 'slot-state')[15],
        persistedTimer: readVar(vm, 'slot-timer')[15],
      };
    },
    assert(obs) {
      assert.equal(obs.award, 200, 'a Zolbak (pts position 8) is worth its 200-pt value-table entry');
      assert.equal(obs.scoreDelta, obs.award, 'a bomb on the Zolbak cell scores exactly 200 once (shared ground detector)');
      assert.equal(obs.aiAfterFirst, 4, 'the FIRST HIT tick reduces the AI level by exactly 2 (6 -> 4)');
      assert.equal(obs.aiAfterAll, 4, 'the drop happens ONCE per kill — it never fires again on the crater ticks (still 4 after 30 ticks)');
      assert.deepEqual(
        obs.xs.slice(0, 3),
        [32, 64, 96],
        'the struck Zolbak craters and keeps scrolling DOWN by exactly 32 units/tick (terrain-locked)',
      );
      assert.equal(obs.persistedType, 31, 'the crater stays OCCUPIED on its clock (never freed like a flying kill)');
      assert.equal(obs.persistedState, 2, 'the crater stays HIT (a persistent crater, like the Barra — not a vanishing Garu node)');
      assert.equal(obs.persistedTimer, obs.n * 2, 'the crater clock keeps counting (2 frames/tick) and is never reset');
    },
    // Freeze the `change ai level by` block (its VALUE -> 0) so a bombed Zolbak no longer reduces the AI
    // level → aiAfterFirst stays 6 → the "6 -> 4" assertion fails. The crater still scrolls, so this
    // isolates the AI-reduction seam (the one genuinely novel behaviour) from the shared crater model.
    negativeMutation: (p) => mutate.freezeVariableChange(p, 'Stage', 'ai level'),
  },
  {
    key: 'zolbak-ai-reduction-floors-at-zero',
    behavior:
      "The Zolbak AI reduction floors at zero: reduce_enemy_ai_by_2 does `subq #2; jcc; moveq #0` — subtract 2, but clamp the unsigned underflow to 0. So bombing a Zolbak when the AI level is 1 leaves it at 0, NOT -1, while bombing one at a comfortable level 5 drops it the full 2 to 3 (the clamp only catches the underflow, it is not a blanket zero)",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen'); // isolate each manual call (see the crater scenarios)
      const seedStruckZolbak = () => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
        put('slot-type', 15, 31); // Zolbak (0x1F)
        put('slot-state', 15, 2); // HIT — first HIT tick (slot timer 0) runs the reduction
        put('slot-pts', 15, 8);
        put('slot-x', 15, 0);
        put('slot-y', 15, 4096);
        put('slot-timer', 15, 0);
        writeVar(vm, 'slot-index', 16);
      };
      // --- Floor: AI level 1 -> a -2 drop underflows and CLAMPS to 0 (not -1).
      seedStruckZolbak();
      writeVar(vm, 'difficulty-ai-level', 1);
      callProc(vm, 'Stage', 'update zolbak');
      step(vm, 1);
      const floored = readVar(vm, 'difficulty-ai-level');
      // --- Normal: AI level 5 -> a full -2 drop to 3 (no clamp; proves the floor is not a blanket zero).
      seedStruckZolbak();
      writeVar(vm, 'difficulty-ai-level', 5);
      callProc(vm, 'Stage', 'update zolbak');
      step(vm, 1);
      const normalDrop = readVar(vm, 'difficulty-ai-level');
      return { floored, normalDrop };
    },
    assert(obs) {
      assert.equal(obs.floored, 0, 'reducing an AI level of 1 by 2 CLAMPS to 0 (the unsigned underflow is floored, not -1)');
      assert.equal(obs.normalDrop, 3, 'reducing an AI level of 5 by 2 lands at 3 — the clamp only catches the underflow, it is not a blanket zero');
    },
    // Pin every `set ai level` to 5: the floor clamp's `set ai level = 0` (the ONLY set that runs here, on
    // the underflow path) now sets 5 instead of 0 → the floored assertion (== 0) fails. The normal-drop path
    // never reaches a set (3 is not < 0), so it still lands at 3 — biting the floor specifically.
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'ai level', 5),
  },
  {
    key: 'derota-fires-when-armed-silent-past-stop-row',
    behavior:
      "A Derota (0x1B) is a plain periodic aimed turret (NO Logram open/close dome): each active tick it drives the SHARED fire-permission gate (chk_timer_fire_bullet_reinit_timer) to fire one aimed bullet per masked reload — but ONLY while still high enough on the field. The arcade gates the fire on `gnd_stop_firing_row` (handle_1B_Derota): it fires only while `cur_row <= ground stop firing row`, and is SILENT (the gate never even runs, so its fire countdown is untouched) once it has scrolled past that row",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      const pc = readVar(vm, 'player-col');
      const seedActiveDerota = () => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
        writeVar(vm, 'tick', 0); // on-phase (tick mod 4 == 0): the shared gate's 8-frame cadence passes
        put('slot-type', 15, 27); // Derota (0x1B)
        put('slot-state', 15, 1); // ACTIVE
        put('slot-pts', 15, 17); // 1-based value-table position of 1000
        put('slot-x', 15, 20 * 256); // row 20
        put('slot-y', 15, pc * 256);
        put('slot-fire-mask', 15, 0); // reload => (rng mod 1) + 1 = 1 (deterministic)
        put('slot-fire-timer', 15, 1); // one on-phase decrement -> 0 -> fire this tick
        writeVar(vm, 'slot-index', 16);
        writeVar(vm, 'bullet-alloc-result', 0);
      };
      // --- Armed: the stop-firing row is BELOW the object's row (20 <= 30) -> it fires.
      seedActiveDerota();
      writeVar(vm, 'ground-stop-firing-row', 30);
      callProc(vm, 'Stage', 'update derota');
      step(vm, 1);
      const firedArmed = readVar(vm, 'bullet-alloc-result');
      // --- Past the row: the stop-firing row is ABOVE the object's row (20 > 10) -> it is silent.
      seedActiveDerota();
      writeVar(vm, 'ground-stop-firing-row', 10);
      callProc(vm, 'Stage', 'update derota');
      step(vm, 1);
      const firedPast = readVar(vm, 'bullet-alloc-result');
      const fireTimerPast = readVar(vm, 'slot-fire-timer')[15];
      return { firedArmed, firedPast, fireTimerPast };
    },
    assert(obs) {
      assert.ok(obs.firedArmed > 0, 'an armed Derota (row <= stop-firing row) fires an aimed bullet through the shared gate');
      assert.equal(obs.firedPast, 0, 'past the stop-firing row the Derota is SILENT — the arm gate blocks the fire');
      assert.equal(obs.fireTimerPast, 1, 'past the row the shared gate never runs: the fire countdown is left untouched (still 1)');
    },
    // Sever the Derota's whole per-tick update: with `update derota` neutralized, the armed probe no longer
    // fires → firedArmed drops to 0 → the "armed fires" assertion fails, proving the silent-past-row result
    // is measured against a genuinely live turret (not a dead seed).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update derota'),
  },
  {
    key: 'derota-craters-when-bombed',
    behavior:
      'A bombed Derota (state HIT) craters PERSISTENTLY exactly like a Barra (handle_bomb_explosion, NOT the Garu node explode-and-remove): `update derota` advances the crater clock (2 frames/tick) AND keeps scrolling it with the terrain (32/tick), never freeing it on the clock, removed only when it culls off the bottom of the field',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      put('slot-type', 15, 27); // Derota (0x1B)
      put('slot-state', 15, 2); // HIT — the crater clock starts here
      put('slot-pts', 15, 17);
      put('slot-x', 15, 0); // top of the field
      put('slot-y', 15, 4096);
      put('slot-timer', 15, 0);
      writeVar(vm, 'slot-index', 16);
      const xs = [];
      const N = 30; // 30 ticks -> clock 60 frames: past the flying free AND the crater start
      for (let t = 0; t < N; t += 1) {
        callProc(vm, 'Stage', 'update derota');
        step(vm, 1);
        xs.push(readVar(vm, 'slot-x')[15]);
      }
      const persisted = {
        type: readVar(vm, 'slot-type')[15],
        state: readVar(vm, 'slot-state')[15],
        timer: readVar(vm, 'slot-timer')[15],
      };
      // Cull: re-seed one scroll step short of the bottom row so the next tick scrolls it to row 40 and frees it.
      put('slot-type', 15, 27);
      put('slot-state', 15, 2);
      put('slot-x', 15, 40 * 256 - 32);
      writeVar(vm, 'slot-index', 16);
      callProc(vm, 'Stage', 'update derota');
      step(vm, 1);
      return {
        xs,
        persisted,
        n: N,
        culledType: readVar(vm, 'slot-type')[15],
        culledState: readVar(vm, 'slot-state')[15],
      };
    },
    assert(obs) {
      assert.deepEqual(
        obs.xs.slice(0, 3),
        [32, 64, 96],
        'a struck Derota keeps scrolling DOWN by exactly 32 units/tick (the crater is terrain-locked)',
      );
      const monotonic = obs.xs.every((x, i) => i === 0 || x === obs.xs[i - 1] + 32);
      assert.equal(monotonic, true, 'the crater scrolls a steady 32/tick for the whole run');
      assert.equal(obs.persisted.timer, obs.n * 2, 'the crater clock keeps counting (2 frames/tick) and is never reset');
      assert.ok(obs.persisted.timer > 56, 'the clock runs past the 56-frame crater start without freeing (persistent, like the Barra)');
      assert.equal(obs.persisted.type, 27, 'the crater stays OCCUPIED on its clock (never freed like a flying kill or the Garu node)');
      assert.equal(obs.persisted.state, 2, 'the crater stays HIT (a persistent crater, not a vanishing burst)');
      assert.equal(obs.culledType, 0, 'a crater scrolled off the bottom (row >= 40) is finally culled (type cleared)');
      assert.equal(obs.culledState, 0, 'the culled crater slot is freed (state cleared) so it can be reused');
    },
    // Sever the Derota's whole per-tick update: a struck Derota neither advances its crater clock nor
    // scrolls → the [32,64,96] drift and the clock-advance assertions fail.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update derota'),
  },
  {
    key: 'garu-derota-base-indestructible',
    behavior:
      "A Garu Derota base is the indestructible half: like the Garu Barra base it carries a non-ACTIVE sentinel state (SLOT_GARU_BASE) that the ground detector's `== ACTIVE` gate rejects, so a bomb dead on the base scores NOTHING and never marks it struck — while the SAME bomb on the SAME cell destroys an ACTIVE node for its 2000-pt value, proving the detector is live and it is specifically the base's sentinel that is immune. On its own tick the base just scrolls with the terrain, persisting (never HIT, never clock-removed)",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      const clearBand = () => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
      };
      writeVar(vm, 'game-director-state', 'frozen'); // isolate each manual call (see the ground scenarios)
      // --- Immunity: the base (state SLOT_GARU_BASE = 3) dead on the bomb target scores nothing.
      clearBand();
      put('slot-type', 15, 33); // Garu Derota (0x21); the base half carries the sentinel state
      put('slot-state', 15, 3); // SLOT_GARU_BASE — the detector's `== ACTIVE (1)` gate excludes it
      put('slot-pts', 15, 19);
      put('slot-x', 15, 5120);
      put('slot-y', 15, 4096);
      put('slot-x', 32, 5120); // locked bomb target on the exact base cell
      put('slot-y', 32, 4096);
      const baseScore0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const baseDelta = readVar(vm, 'eco-score') - baseScore0;
      const baseState = readVar(vm, 'slot-state')[15];
      // --- Live control: an ACTIVE node on the identical cell DOES score 2000.
      clearBand();
      put('slot-type', 15, 33);
      put('slot-state', 15, 1); // ACTIVE node
      put('slot-pts', 15, 19); // value-table position 19 -> 2000
      put('slot-x', 15, 5120);
      put('slot-y', 15, 4096);
      put('slot-x', 32, 5120);
      put('slot-y', 32, 4096);
      const nodeScore0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const nodeDelta = readVar(vm, 'eco-score') - nodeScore0;
      // --- Persistence: the base's own tick just scrolls it (never HIT, never removed on a clock).
      clearBand();
      put('slot-type', 15, 33);
      put('slot-state', 15, 3); // SLOT_GARU_BASE
      put('slot-x', 15, 0);
      put('slot-y', 15, 4096);
      writeVar(vm, 'slot-index', 16);
      const xs = [];
      for (let t = 0; t < 3; t += 1) {
        callProc(vm, 'Stage', 'update garu derota');
        step(vm, 1);
        xs.push(readVar(vm, 'slot-x')[15]);
      }
      return {
        baseDelta,
        baseState,
        nodeDelta,
        award: readVar(vm, 'eco-value-table')[18], // value-table position 19 -> JS index 18 = 2000
        xs,
        persistType: readVar(vm, 'slot-type')[15],
        persistState: readVar(vm, 'slot-state')[15],
      };
    },
    assert(obs) {
      assert.equal(obs.baseDelta, 0, 'a bomb dead on the Garu Derota base scores NOTHING (its sentinel state fails the ACTIVE gate)');
      assert.equal(obs.baseState, 3, 'the base is never marked struck — it keeps its SLOT_GARU_BASE sentinel');
      assert.equal(obs.award, 2000, 'the control node is worth a positive 2000-pt value');
      assert.equal(obs.nodeDelta, obs.award, 'control: the SAME bomb on the SAME cell destroys+scores an ACTIVE node for 2000');
      assert.deepEqual(obs.xs, [32, 64, 96], 'the base scrolls DOWN with the terrain (32/tick) on its own tick');
      assert.equal(obs.persistType, 33, 'the base persists OCCUPIED (never consumed by a bomb)');
      assert.equal(obs.persistState, 3, 'the base persists as the sentinel (never flips to HIT)');
    },
    // Empty the ground detector: the control node no longer scores (nodeDelta 0) → the control assertion
    // fails, proving the base's zero is measured against a genuinely live detector (not a dead seed).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'check ground hit'),
  },
  {
    key: 'garu-derota-node-fires-scores-and-vanishes',
    behavior:
      "A Garu Derota node is the destructible FIRING half (worth 2000): a bomb on its cell resolves through the shared ground detector for exactly 2000; while ACTIVE it fires one aimed bullet per masked reload through the shared gate UNCONDITIONALLY — with NO stop-firing-row gate (unlike the single Derota, garu_derota_handler omits the row check) — so it fires even below a stop-firing row that would silence a Derota; and once struck (state HIT) `update garu derota` runs the node's burst clock and REMOVES the node when the burst finishes (timer >= GARU_REMOVE_FRAMES = 28, frame 7), mirroring explode_and_remove_object — it VANISHES leaving no persistent crater",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      const pc = readVar(vm, 'player-col');
      const clearBand = () => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
      };
      // --- Scoring: an ACTIVE node (type 33, pts pos 19 -> 2000) under the locked bomb target scores 2000.
      clearBand();
      put('slot-type', 15, 33); // Garu Derota (0x21); the node half is state ACTIVE
      put('slot-state', 15, 1); // ACTIVE (destructible)
      put('slot-pts', 15, 19);
      put('slot-x', 15, 5120);
      put('slot-y', 15, 4096);
      put('slot-x', 32, 5120);
      put('slot-y', 32, 4096);
      const award = readVar(vm, 'eco-value-table')[18];
      const score0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const scoreDelta = readVar(vm, 'eco-score') - score0;
      const nodeState = readVar(vm, 'slot-state')[15];
      // --- Unconditional fire: an ACTIVE node fires even BELOW a stop-firing row that would silence a Derota.
      clearBand();
      writeVar(vm, 'tick', 0); // on-phase for the shared gate's 8-frame cadence
      put('slot-type', 15, 33);
      put('slot-state', 15, 1); // ACTIVE node
      put('slot-pts', 15, 19);
      put('slot-x', 15, 20 * 256); // row 20
      put('slot-y', 15, pc * 256);
      put('slot-fire-mask', 15, 0); // reload => (rng mod 1) + 1 = 1
      put('slot-fire-timer', 15, 1); // one on-phase decrement -> 0 -> fire
      writeVar(vm, 'slot-index', 16);
      writeVar(vm, 'ground-stop-firing-row', 10); // row 20 > 10: a single Derota WOULD be silent here
      writeVar(vm, 'bullet-alloc-result', 0);
      callProc(vm, 'Stage', 'update garu derota');
      step(vm, 1);
      const firedNode = readVar(vm, 'bullet-alloc-result');
      // --- Death: a struck node (state HIT) bursts, scrolls, then REMOVES itself at frame 7 (no crater).
      clearBand();
      put('slot-type', 15, 33);
      put('slot-state', 15, 2); // HIT — the detector zeroed the burst clock on the hit tick
      put('slot-pts', 15, 19);
      put('slot-x', 15, 0); // top of the field
      put('slot-y', 15, 4096);
      put('slot-timer', 15, 0);
      writeVar(vm, 'slot-index', 16);
      const snaps = [];
      const N = 14; // 14 ticks -> clock 28 (= GARU_REMOVE_FRAMES): the burst finishes and the node is removed
      for (let t = 0; t < N; t += 1) {
        callProc(vm, 'Stage', 'update garu derota');
        step(vm, 1);
        snaps.push({
          x: readVar(vm, 'slot-x')[15],
          type: readVar(vm, 'slot-type')[15],
          state: readVar(vm, 'slot-state')[15],
          timer: readVar(vm, 'slot-timer')[15],
        });
      }
      return { award, scoreDelta, nodeState, firedNode, snaps };
    },
    assert(obs) {
      assert.equal(obs.award, 2000, 'a Garu Derota node (pts position 19) is worth its 2000-pt value-table entry');
      assert.equal(obs.scoreDelta, obs.award, 'a bomb on the node cell scores exactly 2000 once (shared ground detector)');
      assert.equal(obs.nodeState, 2, 'the struck node is marked HIT (state 2), so it cannot re-score');
      assert.ok(obs.firedNode > 0, 'the ACTIVE node fires UNCONDITIONALLY — even below a stop-firing row that would silence a single Derota');
      assert.deepEqual(
        obs.snaps.slice(0, 3).map((s) => s.x),
        [32, 64, 96],
        'the bursting node scrolls DOWN with the terrain (32/tick) while its burst plays',
      );
      const mid = obs.snaps[12]; // 13th tick: timer 26, still mid-burst
      assert.equal(mid.type, 33, 'mid-burst the node is still present (type held)');
      assert.equal(mid.state, 2, 'mid-burst the node is still HIT (bursting, not yet removed)');
      assert.equal(mid.timer, 26, 'the burst clock counts 2 frames/tick');
      const gone = obs.snaps[13]; // 14th tick: timer 28 = GARU_REMOVE_FRAMES -> removed
      assert.equal(gone.timer, 28, 'the node is removed exactly when its burst finishes (frame 7 = 28 frames)');
      assert.equal(gone.type, 0, 'the node VANISHES (type cleared) — no persistent crater, unlike the Derota');
      assert.equal(gone.state, 0, 'the removed node slot is freed (state cleared) so it can be reused');
    },
    // Sever the Garu Derota's whole per-tick update: the ACTIVE node no longer fires (firedNode 0) and a
    // struck node neither bursts nor removes itself → the unconditional-fire and frame-7 removal assertions
    // both go red.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update garu derota'),
  },
  {
    key: 'boza-outer-scores-300-and-craters',
    behavior:
      'An OUTER Boza dome (type 0x2D, `slot link` > 0 pointing at its centre) is a lone Logram for scoring and death: a bomb on its cell resolves through the shared ground detector for exactly 300 (pts position 10), then `update boza` craters it PERSISTENTLY exactly like a Barra/Logram — the HIT branch scrolls it DOWN with the terrain (32/tick) and climbs its crater clock (2 frames/tick), never freeing it on the clock, removed only when it culls off the bottom of the field',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      // Freeze the walk so one `update boza` call is exactly one tick (see the Logram crater scenario for the
      // identical isolation). All five Boza slots share type 0x2D and the one `update boza` proc; an outer is
      // distinguished by `slot link` > 0 (the port of the arcade `_EXTRA` pointer, holding the centre's index).
      writeVar(vm, 'game-director-state', 'frozen');
      const clearBand = () => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
      };
      // --- Scoring: an ACTIVE outer dome (type 45, pts pos 10 -> 300) under the locked bomb target scores 300.
      clearBand();
      put('slot-type', 15, 45); // Boza Logram (0x2D)
      put('slot-state', 15, 1); // ACTIVE (destructible)
      put('slot-pts', 15, 10); // 1-based value-table position of 300
      put('slot-link', 15, 16); // outer: points at a centre slot (link > 0)
      put('slot-x', 15, 5120);
      put('slot-y', 15, 4096);
      put('slot-x', 32, 5120); // locked bomb target (Scratch slot 33 -> JS index 32), same cell
      put('slot-y', 32, 4096);
      const award = readVar(vm, 'eco-value-table')[9]; // value-table position 10 -> JS index 9 = 300
      const score0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const scoreDelta = readVar(vm, 'eco-score') - score0;
      const outerStateAfterBomb = readVar(vm, 'slot-state')[15];
      // --- Crater: a struck outer (state HIT) craters PERSISTENTLY. Re-seed it at the top of the field
      // (slot x 0) so the drift reads cleanly; its `slot link` points at a cleared slot (JS 0) so the
      // idempotent per-tick centre-value downgrade write lands harmlessly (no centre in this isolation).
      clearBand();
      put('slot-type', 15, 45);
      put('slot-state', 15, 2); // HIT — the detector zeroed the crater clock on the hit tick
      put('slot-pts', 15, 10);
      put('slot-link', 15, 1); // downgrade write targets the (empty) JS-0 slot: harmless in isolation
      put('slot-x', 15, 0); // top of the field
      put('slot-y', 15, 4096);
      put('slot-timer', 15, 0);
      writeVar(vm, 'slot-index', 16);
      const xs = [];
      const N = 30; // 30 ticks -> clock 60 frames: past the 20-frame flying free AND the 56-frame crater start
      for (let t = 0; t < N; t += 1) {
        callProc(vm, 'Stage', 'update boza');
        step(vm, 1);
        xs.push(readVar(vm, 'slot-x')[15]);
      }
      const persisted = {
        type: readVar(vm, 'slot-type')[15],
        state: readVar(vm, 'slot-state')[15],
        timer: readVar(vm, 'slot-timer')[15],
      };
      // Cull: re-seed the crater one scroll step short of the bottom row, so the next `update boza` scrolls it
      // to row 40 (>= CULL_ROW_MAX) and frees the slot — the crater's ONLY exit (as for the Logram/Barra).
      put('slot-type', 15, 45);
      put('slot-state', 15, 2);
      put('slot-link', 15, 1);
      put('slot-x', 15, 40 * 256 - 32);
      writeVar(vm, 'slot-index', 16);
      callProc(vm, 'Stage', 'update boza');
      step(vm, 1);
      return {
        award,
        scoreDelta,
        outerStateAfterBomb,
        xs,
        persisted,
        n: N,
        culledType: readVar(vm, 'slot-type')[15],
        culledState: readVar(vm, 'slot-state')[15],
      };
    },
    assert(obs) {
      assert.equal(obs.award, 300, 'an outer Boza dome (pts position 10) is worth its 300-pt value-table entry');
      assert.equal(obs.scoreDelta, obs.award, 'a bomb on the outer cell scores exactly 300 once (shared ground detector)');
      assert.equal(obs.outerStateAfterBomb, 2, 'the struck outer is marked HIT (state 2), so it cannot re-score');
      assert.deepEqual(
        obs.xs.slice(0, 3),
        [32, 64, 96],
        'a struck outer keeps scrolling DOWN by exactly 32 units/tick (the crater is terrain-locked, like a Logram)',
      );
      const monotonic = obs.xs.every((x, i) => i === 0 || x === obs.xs[i - 1] + 32);
      assert.equal(monotonic, true, 'the crater scrolls a steady 32/tick for the whole run');
      assert.equal(obs.persisted.timer, obs.n * 2, 'the crater clock keeps counting (2 frames/tick) and is never reset');
      assert.ok(obs.persisted.timer > 56, 'the clock runs past the 56-frame crater start without freeing (persistent, like the Barra)');
      assert.equal(obs.persisted.type, 45, 'the crater stays OCCUPIED on its clock (never freed like a flying kill or the Garu node)');
      assert.equal(obs.persisted.state, 2, 'the crater stays HIT on its clock (a persistent crater, not a vanishing burst)');
      assert.equal(obs.culledType, 0, 'a crater scrolled off the bottom (row >= 40) is finally culled (type cleared)');
      assert.equal(obs.culledState, 0, 'the culled crater slot is freed (state cleared) so it can be reused');
    },
    // Sever the Boza's whole per-tick update: a struck outer neither advances its crater clock nor scrolls →
    // the [32,64,96] drift and the clock-advance assertions go red (the scoring is the shared detector's, so it
    // still stands — the crater is what `update boza` owns).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update boza'),
  },
  {
    key: 'boza-outer-hit-downgrades-centre-value',
    behavior:
      "Bombing an OUTER Boza dome downgrades its linked CENTRE's value from 2,000 to 600 (update_centre_points_value): while the outer is HIT, `update boza` rewrites the centre slot's `slot pts` (via the outer's `slot link`) to the 600-pt position, so a LATER bomb on the still-ACTIVE centre scores 600, not 2,000. The centre itself is untouched otherwise — only its future award drops",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      // Centre at JS 15 (Scratch 16), one outer at JS 14 linked to it. Different cells so each bomb resolves
      // exactly one slot: the outer at x 6000, the centre at x 5120.
      put('slot-type', 15, 45); // centre
      put('slot-state', 15, 1); // ACTIVE
      put('slot-pts', 15, 19); // full centre value: position 19 -> 2000
      put('slot-link', 15, 0); // centre: link == 0
      put('slot-x', 15, 5120);
      put('slot-y', 15, 4096);
      put('slot-type', 14, 45); // outer
      put('slot-state', 14, 1); // ACTIVE
      put('slot-pts', 14, 10); // outer value: 300
      put('slot-link', 14, 16); // points at the centre (Scratch index 16)
      put('slot-x', 14, 6000);
      put('slot-y', 14, 4096);
      const full = readVar(vm, 'eco-value-table')[18]; // position 19 -> JS 18 = 2000
      const downgraded = readVar(vm, 'eco-value-table')[12]; // position 13 -> JS 12 = 600
      // --- Bomb the outer: it scores 300 and is marked HIT; the centre value is still full at this instant.
      put('slot-x', 32, 6000);
      put('slot-y', 32, 4096);
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const centrePtsBefore = readVar(vm, 'slot-pts')[15];
      // --- Update the HIT outer: its HIT branch rewrites the linked centre's `slot pts` to the 600 position.
      writeVar(vm, 'slot-index', 15); // Scratch index of the outer (JS 14)
      callProc(vm, 'Stage', 'update boza');
      step(vm, 1);
      const centrePtsAfter = readVar(vm, 'slot-pts')[15];
      // --- Now bomb the still-ACTIVE centre: it scores its DOWNGRADED value (600), not the original 2,000.
      put('slot-x', 32, 5120);
      put('slot-y', 32, 4096);
      const score0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const centreDelta = readVar(vm, 'eco-score') - score0;
      return { full, downgraded, centrePtsBefore, centrePtsAfter, centreDelta };
    },
    assert(obs) {
      assert.equal(obs.full, 2000, 'an undamaged centre (pts position 19) is worth 2,000');
      assert.equal(obs.downgraded, 600, 'the downgraded position (13) is worth 600');
      assert.equal(obs.centrePtsBefore, 19, 'the centre keeps its full value until the struck outer is updated');
      assert.equal(obs.centrePtsAfter, 13, 'the HIT outer rewrites the linked centre to the 600-pt position');
      assert.equal(obs.centreDelta, 600, 'a LATER bomb on the downgraded centre scores 600, NOT 2,000');
    },
    // Break the outer HIT gate (`item of slot state == 2` -> == 999): the struck outer takes the ACTIVE branch,
    // so the downgrade never runs and the centre stays worth 2,000 -> the centreDelta assertion goes red.
    negativeMutation: (p) => mutate.changeListItemEqualsOperand(p, 'Stage', 'slot state', 2, 999),
  },
  {
    key: 'boza-centre-first-cascade-clears-outers-for-free',
    behavior:
      'Bombing the CENTRE Boza slot first clears its outers for NO score (destroy_all_outer_lograms): the centre scores its full 2,000 through the shared detector, then `update boza` cascades — it writes all four outer slots (centre index − 1..−4) directly to HIT, BYPASSING the award sweep. Because the type-agnostic ground detector only credits an ACTIVE slot, the cascaded outers are already HIT and can never be scored — a later bomb on one adds nothing',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      // Four outers at JS 11..14 (Scratch 12..15), the centre at JS 15 (Scratch 16). The cascade addresses
      // centre-index − 1..−4 (Scratch 15..12 -> JS 14..11), so these four outers are exactly its targets.
      for (let js = 11; js <= 14; js += 1) {
        put('slot-type', js, 45);
        put('slot-state', js, 1); // ACTIVE
        put('slot-pts', js, 10); // 300 each — would score if the detector ever credited them
        put('slot-link', js, 16); // point at the centre
        put('slot-x', js, 6000); // OFF the centre's cell so the centre bomb resolves only the centre
        put('slot-y', js, 4096);
      }
      put('slot-type', 15, 45); // centre
      put('slot-state', 15, 1); // ACTIVE
      put('slot-pts', 15, 19); // full 2,000
      put('slot-link', 15, 0); // centre: link == 0
      put('slot-x', 15, 5120);
      put('slot-y', 15, 4096);
      const full = readVar(vm, 'eco-value-table')[18]; // 2000
      // --- Bomb the centre: it scores 2,000; the outers (off-cell) are NOT credited by the sweep.
      put('slot-x', 32, 5120);
      put('slot-y', 32, 4096);
      const score0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const centreDelta = readVar(vm, 'eco-score') - score0;
      const outersBeforeCascade = [11, 12, 13, 14].map((js) => readVar(vm, 'slot-state')[js]);
      // --- Update the HIT centre: it cascades all four outers directly to HIT, awarding NOTHING.
      writeVar(vm, 'slot-index', 16); // Scratch index of the centre (JS 15)
      const score1 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'update boza');
      step(vm, 1);
      const cascadeDelta = readVar(vm, 'eco-score') - score1;
      const outersAfterCascade = [11, 12, 13, 14].map((js) => readVar(vm, 'slot-state')[js]);
      // --- Prove "for free": re-bomb a now-HIT outer's cell; a HIT slot is never credited -> no extra score.
      put('slot-x', 32, 6000);
      put('slot-y', 32, 4096);
      const score2 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const freeloadDelta = readVar(vm, 'eco-score') - score2;
      return {
        full,
        centreDelta,
        outersBeforeCascade,
        cascadeDelta,
        outersAfterCascade,
        freeloadDelta,
      };
    },
    assert(obs) {
      assert.equal(obs.full, 2000, 'the centre (pts position 19) is worth 2,000');
      assert.equal(obs.centreDelta, 2000, 'a centre-first bomb scores exactly 2,000 once (shared ground detector)');
      assert.deepEqual(obs.outersBeforeCascade, [1, 1, 1, 1], 'the four outers are still ACTIVE the instant the centre is struck');
      assert.equal(obs.cascadeDelta, 0, 'the cascade awards NOTHING — it writes outer HIT state directly, bypassing the award sweep');
      assert.deepEqual(obs.outersAfterCascade, [2, 2, 2, 2], 'the cascade marks all four outers HIT (destroy_all_outer_lograms)');
      assert.equal(obs.freeloadDelta, 0, 'a later bomb on a cascaded (already-HIT) outer scores nothing — the outers were cleared for free');
    },
    // Break the top outer/centre discriminator (`item of slot link == 0` -> == 999): the centre (link 0) now
    // takes the OUTER branch, so the cascade never runs and the four outers stay ACTIVE -> the outersAfter
    // assertion goes red.
    negativeMutation: (p) => mutate.changeListItemEqualsOperand(p, 'Stage', 'slot link', 0, 999),
  },
  {
    key: 'grobda-moves-by-its-own-velocity-no-double-scroll',
    behavior:
      "A Grobda is the FIRST ground object that moves under its OWN velocity (`advance ground moving`, the Commit-1 seam) rather than the fixed terrain scroll: each ACTIVE tick its `slot x` advances by exactly TICK_VELOCITY_SCALE (4) * `slot dx`, with NO separate AREA_PROGRESS_STEP scroll baseline added — the scroll is BAKED INTO the stored delta (raw dX 8 = scroll-matched, so a 'stopped' 0x2C still drifts DOWN at exactly 32/tick, and a forward 0x35 with raw dX 14 moves at 56/tick). Adding a baseline would double the stopped drift to 64 — the plan-review MAJOR trap this pins. It moves scroll-axis-only (`slot dy` 0, so `slot y` never moves), and it NEVER fires",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      writeVar(vm, 'bullet-alloc-result', 0);
      const seedGrobda = (type, dx0) => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
        put('slot-type', 15, type);
        put('slot-state', 15, 1); // ACTIVE
        put('slot-x', 15, 0); // top of the field
        put('slot-y', 15, 8 * 256); // an arbitrary lateral column, held fixed
        put('slot-dx', 15, dx0); // the raw handler dX (scroll baked in)
        put('slot-dy', 15, 0); // Grobda clears _dY: scroll-axis-only motion
        put('slot-flag', 15, 0); // PRETRIGGER (a non-reacting variant never leaves it)
        put('slot-timer', 15, 0);
        writeVar(vm, 'slot-index', 16);
      };
      const runDeltas = (type, dx0, n) => {
        seedGrobda(type, dx0);
        const xs = [];
        const ys = [];
        for (let t = 0; t < n; t += 1) {
          callProc(vm, 'Stage', 'update grobda');
          step(vm, 1);
          xs.push(readVar(vm, 'slot-x')[15]);
          ys.push(readVar(vm, 'slot-y')[15]);
        }
        return { xs, ys };
      };
      // Stationary 0x2C (raw dX 8, 200 pts): scroll-matched -> exactly +32/tick, NEVER +64.
      const stopped = runDeltas(0x2c, 8, 3);
      // Forward 0x35 (raw dX 14, 400 pts): 4*14 = +56/tick.
      const forward = runDeltas(0x35, 14, 3);
      const fired = readVar(vm, 'bullet-alloc-result');
      return { stopped, forward, fired };
    },
    assert(obs) {
      assert.deepEqual(
        obs.stopped.xs,
        [32, 64, 96],
        "a 'stopped' Grobda (raw dX 8) drifts DOWN at exactly 32/tick — the scroll is baked into the delta, NOT added on top (double-scroll would read 64,128,192)",
      );
      assert.equal(
        obs.stopped.xs[0],
        32,
        'the first stopped step is exactly 32, never the 64 a doubled scroll baseline would produce (the plan-review MAJOR trap)',
      );
      assert.deepEqual(
        obs.stopped.ys,
        [2048, 2048, 2048],
        'a Grobda moves scroll-axis-only: `slot dy` is 0, so `slot y` never moves',
      );
      assert.deepEqual(
        obs.forward.xs,
        [56, 112, 168],
        'a forward Grobda (raw dX 14) moves at 4*14 = 56/tick through the velocity-only seam',
      );
      assert.equal(obs.fired, 0, 'a Grobda NEVER fires — no bullet is ever allocated on its tick');
    },
    // Sever the shared velocity seam itself: with `advance ground moving` a no-op, neither Grobda moves at
    // all -> the stopped deltas collapse to [0,0,0] and the first deepEqual goes red, proving the drift is
    // produced by the live per-slot mover (not the terrain scroll, which `update grobda` does NOT call while ACTIVE).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'advance ground moving'),
  },
  {
    key: 'grobda-reacts-to-reticle-only-inside-the-band',
    behavior:
      "A reticle-reactive Grobda arms its reaction ONLY inside its [-2,+1] per-axis alignment band, on BOTH the depth (row) and lateral (col) axes, and does so from EITHER reticle window: the moving CROSSHAIR (slot 35, JS 34) for the 0x38 crosshairs variant (forward -> stop 48f -> resume) and the frozen BOMB TARGET (slot 33, JS 32) for the 0x3B targeted variant (forward -> back 48f -> resume). Aligned it commits react_dX, latches the 48-frame reaction (`slot flag` -> REACTING, `slot timer` 48 counted down one frame-step the same tick) and drops its forward roll; on the LOW bound (d = -2) it still arms; one past the HIGH bound (d = +2) or off the OTHER axis it does NOT arm (holds forward velocity + PRETRIGGER). A sign flip, a widened band, or a dropped axis would wrongly arm one of these boundary probes. Either way it never fires",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      writeVar(vm, 'bullet-alloc-result', 0);
      const seedGrobda = (type, dx0) => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
        put('slot-type', 15, type);
        put('slot-state', 15, 1); // ACTIVE
        put('slot-x', 15, 10 * 256); // row 10
        put('slot-y', 15, 8 * 256); // col 8
        put('slot-dx', 15, dx0); // rolling forward (raw dX)
        put('slot-dy', 15, 0);
        put('slot-flag', 15, 0); // PRETRIGGER (waiting for the reticle)
        put('slot-timer', 15, 0);
        writeVar(vm, 'slot-index', 16);
      };
      // The two reticle windows live in reserved high slots outside the 0..15 ground band: CROSSHAIR_SLOT 35
      // -> JS index 34, BOMB_TARGET_SLOT 33 -> JS index 32. For each probe the NON-trigger reticle is parked
      // far away, so only the window under test is ever in range.
      const CROSS = 34;
      const BOMB = 32;
      const setReticle = (jsIndex, row, col) => {
        put('slot-x', jsIndex, row * 256);
        put('slot-y', jsIndex, col * 256);
      };
      const react = () => ({
        dx: readVar(vm, 'slot-dx')[15],
        flag: readVar(vm, 'slot-flag')[15],
        timer: readVar(vm, 'slot-timer')[15],
      });
      // Fresh-seed a Grobda of `type` (rolling at dx0), place its trigger reticle at cell (row,col) and the
      // OTHER reticle out of range, run one live update tick, read the reaction. The tank sits at cell (10, 8),
      // so the alignment delta is d = reticle_cell - (10, 8), and the band is [-2,+1] on BOTH axes.
      const probe = (type, dx0, triggerSlot, parkSlot, row, col) => {
        seedGrobda(type, dx0);
        setReticle(parkSlot, 99, 99);
        setReticle(triggerSlot, row, col);
        callProc(vm, 'Stage', 'update grobda');
        step(vm, 1);
        return react();
      };
      // --- Crosshair window (slot 35), 0x38: react_dX = 8 (stop). Four probes pin the band geometry.
      const aligned = probe(0x38, 14, CROSS, BOMB, 10, 8); // d = (0, 0): deep inside the band -> arms
      const lowEdge = probe(0x38, 14, CROSS, BOMB, 8, 8); // d_row = -2: inclusive LOW bound -> still arms
      const highEdgeOut = probe(0x38, 14, CROSS, BOMB, 12, 8); // d_row = +2: one past +1 -> NO arm (pins the sign)
      const offAxis = probe(0x38, 14, CROSS, BOMB, 10, 20); // d_row = 0 (in) but d_col = +12 (out) -> NO arm
      // --- Bomb-target window (slot 33), 0x3B: react_dX = 2 (back). Proves the OTHER reticle slot drives a
      // live reaction (not only the crosshair) and that its band gates the same way.
      const targetedAligned = probe(0x3B, 14, BOMB, CROSS, 10, 8); // bomb target on the tank's cell -> arms
      const targetedOut = probe(0x3B, 14, BOMB, CROSS, 22, 8); // bomb target 12 rows away -> NO arm
      const fired = readVar(vm, 'bullet-alloc-result');
      return { aligned, lowEdge, highEdgeOut, offAxis, targetedAligned, targetedOut, fired };
    },
    assert(obs) {
      assert.equal(obs.aligned.dx, 8, 'crosshair aligned (d = 0,0), the 0x38 Grobda commits react_dX = 8 (stops rolling forward)');
      assert.equal(obs.aligned.flag, 1, 'aligned, the reaction latches: `slot flag` -> REACTING (1)');
      assert.equal(obs.aligned.timer, 46, 'aligned, the 48-frame reaction is armed AND counted down one frame-step the same tick (48 -> 46)');
      assert.equal(obs.lowEdge.dx, 8, 'd_row = -2 is the inclusive LOW bound of [-2,+1] -> the Grobda still arms (react_dX = 8)');
      assert.equal(obs.lowEdge.flag, 1, 'd_row = -2 (inclusive) -> the reaction latches');
      assert.equal(obs.highEdgeOut.dx, 14, 'd_row = +2 is one past the +1 high bound -> NO arm; holds forward velocity (14). A sign flip would misread this as -2 and wrongly arm');
      assert.equal(obs.highEdgeOut.flag, 0, 'd_row = +2 out of band -> the flag stays PRETRIGGER (0)');
      assert.equal(obs.offAxis.dx, 14, 'd_col = +12 out of band (with d_row in band) -> NO arm; proves BOTH axes gate the reaction (a dropped column check would arm here)');
      assert.equal(obs.offAxis.flag, 0, 'off the lateral axis, the flag stays PRETRIGGER (0)');
      assert.equal(obs.targetedAligned.dx, 2, 'the 0x3B targeted variant reads the BOMB TARGET slot (33) and, aligned, commits its react_dX = 2 (reverses)');
      assert.equal(obs.targetedAligned.flag, 1, 'bomb-target aligned -> the targeted reaction latches (REACTING)');
      assert.equal(obs.targetedOut.dx, 14, 'bomb target 12 rows away -> the targeted variant does NOT arm (holds forward velocity 14)');
      assert.equal(obs.targetedOut.flag, 0, 'bomb target out of band -> the flag stays PRETRIGGER (0)');
      assert.equal(obs.fired, 0, 'a Grobda NEVER fires, in band or out, from either reticle window');
    },
    // Sever the Grobda update: no probe arms -> aligned.dx stays 14 (the seed), so `aligned.dx == 8` goes red,
    // proving every reaction is measured against a live updater and that it is alignment (not the seed) that
    // drops the velocity to the reaction value.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update grobda'),
  },
  {
    key: 'grobda-land-craters-water-vanishes',
    behavior:
      'A bombed LAND Grobda (0x2C) craters PERSISTENTLY like a Barra (handle_bomb_explosion — terrain-locked, so its HIT branch scrolls with the terrain via `advance ground` and is removed only by the bottom-edge cull), while a bombed WATER Grobda (0x3D) plays the explode-and-remove burst and VANISHES mid-field on its own clock after GARU_REMOVE_FRAMES (28) frames, exactly like a Garu node — it never becomes a lasting crater',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      const seedHit = (type, x) => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
        put('slot-type', 15, type);
        put('slot-state', 15, 2); // HIT
        put('slot-x', 15, x);
        put('slot-y', 15, 8 * 256);
        put('slot-timer', 15, 0); // the detector zeroed the clock on the hit tick
        writeVar(vm, 'slot-index', 16);
      };
      // --- LAND 0x2C: a persistent crater. Scrolls a steady 32/tick and never frees on its clock.
      seedHit(0x2c, 0);
      const landXs = [];
      const N = 20; // 20 ticks -> clock 40 frames, past the 28-frame burst window without vanishing
      for (let t = 0; t < N; t += 1) {
        callProc(vm, 'Stage', 'update grobda');
        step(vm, 1);
        landXs.push(readVar(vm, 'slot-x')[15]);
      }
      const landPersist = {
        type: readVar(vm, 'slot-type')[15],
        state: readVar(vm, 'slot-state')[15],
        timer: readVar(vm, 'slot-timer')[15],
      };
      // Cull: one scroll step short of the bottom -> the next tick scrolls it to row 40 and frees it.
      seedHit(0x2c, 40 * 256 - 32);
      callProc(vm, 'Stage', 'update grobda');
      step(vm, 1);
      const landCulled = readVar(vm, 'slot-type')[15];
      // --- WATER 0x3D: explode-and-remove. Vanishes on its clock at frame 28, well short of the bottom.
      seedHit(0x3d, 0);
      let waterCullTick = -1;
      let waterCullRow = -1;
      for (let t = 1; t <= 16; t += 1) {
        callProc(vm, 'Stage', 'update grobda');
        step(vm, 1);
        if (readVar(vm, 'slot-type')[15] === 0 && waterCullTick === -1) {
          waterCullTick = t;
          waterCullRow = readVar(vm, 'slot-x')[15] / 256;
        }
      }
      return { landXs, landPersist, landCulled, n: N, waterCullTick, waterCullRow };
    },
    assert(obs) {
      assert.deepEqual(
        obs.landXs.slice(0, 3),
        [32, 64, 96],
        'a struck LAND Grobda keeps scrolling DOWN 32/tick (the crater is terrain-locked, like a Barra)',
      );
      assert.equal(obs.landPersist.timer, obs.n * 2, 'the land crater clock keeps counting (2 frames/tick) and is never reset');
      assert.ok(obs.landPersist.timer > 28, 'the land crater persists past the 28-frame burst window (it is NOT an explode-and-remove burst)');
      assert.equal(obs.landPersist.type, 0x2c, 'the land crater stays OCCUPIED on its clock');
      assert.equal(obs.landPersist.state, 2, 'the land crater stays HIT (a persistent crater)');
      assert.equal(obs.landCulled, 0, 'a land crater scrolled off the bottom (row >= 40) is finally culled');
      assert.equal(obs.waterCullTick, 14, 'a struck WATER Grobda VANISHES on its clock at frame 28 (tick 14: timer 2*14 = 28 >= GARU_REMOVE_FRAMES)');
      assert.ok(obs.waterCullRow < 5, 'the water Grobda vanishes MID-FIELD (row < 5), on its clock — NOT by scrolling off the bottom');
    },
    // Sever the Grobda update: the land crater neither scrolls nor advances its clock (the [32,64,96] drift
    // and clock assertions go red) AND the water Grobda never reaches its remove frame (waterCullTick stays -1),
    // proving both crater/vanish paths are driven by the live updater.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update grobda'),
  },
  {
    key: 'domogram-follows-scripted-path-then-holds-last-vector',
    behavior:
      "A Domogram follows a SCRIPTED path (handle_2E_Domogram's path coroutine): each vector is held for its `duration` frames, then the next step loads a (dY,dX) velocity from the 32-entry vector table by its stored 0-based index. When the path is EXHAUSTED (_NVEC hits 0) it HOLDS the last vector FOREVER — no further loads. This is the first ground payload beyond the three scalar columns: the step columns (`domogram path vector` / `domogram path duration`) are decoded into the runtime `slot dx/dy` through `slot flag` (_VECLEN), `slot link` (_EXTRA pointer) and `slot vec left` (_NVEC)",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      // Author a tiny 2-step path in the shared step columns (JS 0,1 <-> Scratch pointer 1,2):
      //   step 1: vector index 8 -> table (dY,dX) = (8,8), held 6 frames
      //   step 2: vector index 0 -> table (dY,dX) = (0,16), held long (the LAST vector, then held forever)
      put('domogram-path-vector', 0, 8);
      put('domogram-path-duration', 0, 6);
      put('domogram-path-vector', 1, 0);
      put('domogram-path-duration', 1, 100);
      put('slot-type', 15, 46); // Domogram (0x2E)
      put('slot-state', 15, 1); // ACTIVE
      put('slot-x', 15, 0);
      put('slot-y', 15, 8 * 256);
      put('slot-flag', 15, 1); // _VECLEN = 1: the first tick loads the first vector (DOMOGRAM_VECLEN_INIT)
      put('slot-link', 15, 1); // _EXTRA: 1-based pointer at step 1
      put('slot-vec-left', 15, 2); // _NVEC: two scripted steps
      put('slot-dx', 15, 0);
      put('slot-dy', 15, 0);
      put('slot-timer', 15, 999); // shot timer high: no fire during the path probe
      put('slot-fire-timer', 15, 0);
      writeVar(vm, 'ground-stop-firing-row', -1); // disarm the fire gate (row 0 > -1) so the path is isolated
      writeVar(vm, 'slot-index', 16);
      const dxs = [];
      const dys = [];
      for (let t = 0; t < 7; t += 1) {
        callProc(vm, 'Stage', 'update domogram');
        step(vm, 1);
        dxs.push(readVar(vm, 'slot-dx')[15]);
        dys.push(readVar(vm, 'slot-dy')[15]);
      }
      const nvec = readVar(vm, 'slot-vec-left')[15];
      return { dxs, dys, nvec };
    },
    assert(obs) {
      assert.deepEqual(
        obs.dxs,
        [8, 8, 8, 16, 16, 16, 16],
        'the follower loads step 1 (dX 8) held for its 6-frame duration (3 ticks at 2 frames/tick), then step 2 (dX 16) — and HOLDS 16 forever once the path is exhausted',
      );
      assert.deepEqual(
        obs.dys,
        [8, 8, 8, 0, 0, 0, 0],
        'the paired dY decodes from the same vector table (8 then 0), held past path exhaustion',
      );
      assert.equal(obs.nvec, 0, 'the path is exhausted (_NVEC 2 -> 0): the last vector is then held with no further loads');
    },
    // Sever the Domogram update: the path never decodes, so `slot dx` stays at its seeded 0 -> the dxs
    // deepEqual ([8,8,8,8,...]) goes red, proving the decoded velocities come from the live path follower.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update domogram'),
  },
  {
    key: 'domogram-fires-one-aimed-shot-at-anim-midpoint-gated',
    behavior:
      "A Domogram fires exactly ONE aimed bullet per shot cycle at its animation MIDPOINT (domogram_shooting): when its masked shot timer expires it starts a 24-frame animation (_TYPE = 24) and fires a single aimed shot the frame the animation reaches 12 (the midpoint). Starting a shot is gated TWO ways (domogram_main): only while still armed (`cur_row <= ground stop firing row`) AND only on the every-4th-tick fire phase (`tick mod 4 == 0`, the arcade's `countup_timer_1 & 7` every-8th-frame cadence). Past the stop-firing row it is SILENT and its shot countdown is never even touched; off the fire phase the shot-start gate never opens, so the countdown is likewise never decremented and no animation begins (once an animation IS running the phase is ignored by design). It never fires more than one shot per cycle",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      const pc = readVar(vm, 'player-col');
      const seedActiveDomogram = () => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
        writeVar(vm, 'tick', 0); // on-phase (tick mod 4 == 0): the shot-start phase gate passes
        put('slot-type', 15, 46); // Domogram (0x2E)
        put('slot-state', 15, 1); // ACTIVE
        put('slot-pts', 15, 15); // 800 pts (DOMOGRAM_PTS)
        put('slot-x', 15, 20 * 256); // row 20
        put('slot-y', 15, pc * 256);
        put('slot-fire-mask', 15, 0); // reload => (rng mod 1) + 1 = 1 (deterministic)
        put('slot-timer', 15, 1); // shot timer: one on-phase decrement -> 0 -> start the animation
        put('slot-fire-timer', 15, 0); // anim idle
        put('slot-dx', 15, 0);
        put('slot-dy', 15, 0);
        put('slot-vec-left', 15, 0); // path exhausted: it stays put (no drift) during the fire probe
        writeVar(vm, 'slot-index', 16);
        writeVar(vm, 'bullet-alloc-result', 0);
      };
      // --- Armed: stop-firing row BELOW the object (20 <= 30). Drive until the aimed shot fires at frame 12.
      seedActiveDomogram();
      writeVar(vm, 'ground-stop-firing-row', 30);
      let fireTick = -1;
      let animAtFire = -1;
      for (let t = 1; t <= 8; t += 1) {
        writeVar(vm, 'bullet-alloc-result', 0);
        callProc(vm, 'Stage', 'update domogram');
        step(vm, 1);
        if (readVar(vm, 'bullet-alloc-result') > 0 && fireTick === -1) {
          fireTick = t;
          animAtFire = readVar(vm, 'slot-fire-timer')[15];
        }
      }
      // --- Past the row: stop-firing row ABOVE the object (20 > 10) -> silent, shot countdown untouched.
      seedActiveDomogram();
      writeVar(vm, 'ground-stop-firing-row', 10);
      let firedPast = 0;
      for (let t = 1; t <= 8; t += 1) {
        callProc(vm, 'Stage', 'update domogram');
        step(vm, 1);
        firedPast += readVar(vm, 'bullet-alloc-result') > 0 ? 1 : 0;
      }
      const shotTimerPast = readVar(vm, 'slot-timer')[15];
      const animPast = readVar(vm, 'slot-fire-timer')[15];
      // --- Off the fire phase: armed (row 20 <= 30) but the tick is frozen OFF the every-4th-tick phase, so
      // the shot-start gate (armed AND tick mod 4 == 0) never opens: the countdown is never decremented and no
      // animation ever begins. This is the ONLY place the phase can be shown to discriminate — once running,
      // the animation steps every tick regardless of phase. Dropping the phase gate would fire here.
      seedActiveDomogram();
      writeVar(vm, 'ground-stop-firing-row', 30);
      writeVar(vm, 'tick', 1); // 1 mod 4 != 0 -> off-phase, and frozen there for the whole probe
      let firedOffPhase = 0;
      for (let t = 1; t <= 8; t += 1) {
        callProc(vm, 'Stage', 'update domogram');
        step(vm, 1);
        firedOffPhase += readVar(vm, 'bullet-alloc-result') > 0 ? 1 : 0;
      }
      const shotTimerOffPhase = readVar(vm, 'slot-timer')[15];
      const animOffPhase = readVar(vm, 'slot-fire-timer')[15];
      return { fireTick, animAtFire, firedPast, shotTimerPast, animPast, firedOffPhase, shotTimerOffPhase, animOffPhase };
    },
    assert(obs) {
      assert.equal(obs.fireTick, 6, 'the aimed shot fires on tick 6: start (anim 24 -> 22) + five steps to 12 (the 24-frame animation midpoint)');
      assert.equal(obs.animAtFire, 12, 'it fires exactly at the animation midpoint (_TYPE decremented to 12)');
      assert.equal(obs.firedPast, 0, 'past the stop-firing row the Domogram is SILENT — no aimed shot is ever fired');
      assert.equal(obs.shotTimerPast, 1, 'past the row the fire gate never runs: the shot countdown is left untouched (still 1)');
      assert.equal(obs.animPast, 0, 'past the row no animation ever starts (_TYPE stays idle)');
      assert.equal(obs.firedOffPhase, 0, 'off the every-4th-tick fire phase the shot-start gate never opens -> no shot is ever fired');
      assert.equal(obs.shotTimerOffPhase, 1, 'off-phase the fire gate never runs: the shot countdown is left untouched (still 1) — proving the phase gate discriminates, not just the row gate');
      assert.equal(obs.animOffPhase, 0, 'off-phase no animation ever starts (_TYPE stays idle)');
    },
    // Sever the Domogram update: the armed probe never starts its animation, so it never fires -> fireTick
    // stays -1 (!= 6), proving the midpoint fire is measured against a genuinely live shooter.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update domogram'),
  },
  {
    key: 'domogram-craters-when-bombed',
    behavior:
      'A bombed Domogram (state HIT) craters PERSISTENTLY exactly like a Barra (handle_bomb_explosion, NOT explode-and-remove): its HIT branch advances the crater clock (2 frames/tick) AND keeps scrolling with the terrain (32/tick via `advance ground`, NOT the velocity mover), never freeing on the clock — removed only when it culls off the bottom of the field',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      put('slot-type', 15, 46); // Domogram (0x2E)
      put('slot-state', 15, 2); // HIT — the crater clock starts here
      put('slot-x', 15, 0); // top of the field
      put('slot-y', 15, 8 * 256);
      put('slot-timer', 15, 0);
      put('slot-dx', 15, 99); // a nonzero leftover velocity: the HIT branch must IGNORE it (scroll, not move)
      put('slot-dy', 15, 99);
      writeVar(vm, 'slot-index', 16);
      const xs = [];
      const N = 30;
      for (let t = 0; t < N; t += 1) {
        callProc(vm, 'Stage', 'update domogram');
        step(vm, 1);
        xs.push(readVar(vm, 'slot-x')[15]);
      }
      const persisted = {
        type: readVar(vm, 'slot-type')[15],
        state: readVar(vm, 'slot-state')[15],
        timer: readVar(vm, 'slot-timer')[15],
        y: readVar(vm, 'slot-y')[15],
      };
      put('slot-type', 15, 46);
      put('slot-state', 15, 2);
      put('slot-x', 15, 40 * 256 - 32);
      writeVar(vm, 'slot-index', 16);
      callProc(vm, 'Stage', 'update domogram');
      step(vm, 1);
      return {
        xs,
        persisted,
        n: N,
        culledType: readVar(vm, 'slot-type')[15],
        culledState: readVar(vm, 'slot-state')[15],
      };
    },
    assert(obs) {
      assert.deepEqual(
        obs.xs.slice(0, 3),
        [32, 64, 96],
        'a struck Domogram scrolls DOWN by exactly 32/tick via the terrain scroll — its own (leftover) velocity is IGNORED while HIT',
      );
      assert.equal(obs.persisted.y, 8 * 256, 'the HIT branch does NOT run the velocity mover: `slot y` never moves despite the nonzero leftover dy');
      const monotonic = obs.xs.every((x, i) => i === 0 || x === obs.xs[i - 1] + 32);
      assert.equal(monotonic, true, 'the crater scrolls a steady 32/tick for the whole run');
      assert.equal(obs.persisted.timer, obs.n * 2, 'the crater clock keeps counting (2 frames/tick) and is never reset');
      assert.ok(obs.persisted.timer > 56, 'the clock runs past the 56-frame crater start without freeing (persistent, like the Barra)');
      assert.equal(obs.persisted.type, 46, 'the crater stays OCCUPIED on its clock (never freed like a flying kill)');
      assert.equal(obs.persisted.state, 2, 'the crater stays HIT (a persistent crater, not a vanishing burst)');
      assert.equal(obs.culledType, 0, 'a crater scrolled off the bottom (row >= 40) is finally culled');
      assert.equal(obs.culledState, 0, 'the culled crater slot is freed so it can be reused');
    },
    // Sever the Domogram update: a struck Domogram neither advances its crater clock nor scrolls -> the
    // [32,64,96] drift and clock assertions go red.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update domogram'),
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
