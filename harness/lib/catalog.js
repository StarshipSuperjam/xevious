// The scenario catalog IS the coverage checklist. Each entry maps a previously-regressed,
// VM-observable behavior to one scenario, and carries the mutation that makes THIS
// scenario's assertion go red. `drive` runs the same way for the positive (real build)
// and negative (mutated build) cases; the runner asserts the positive passes and the
// negative fails, so an assertion that does not actually bite is caught.
//
// Behaviors that are NOT VM-observable (rendered collision, the bomb's flight duration,
// visuals/audio/feel) are deliberately excluded
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
import { reachPlaying, reachPlaying2P, stateOf, insertCoin, loadArtifact } from './build.js';
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

// CAB-01 (slice 17): green-flag and step past the title hold to the first attract demo (playing with the
// attract flag still raised). The arcade title stage runs 744 frames before it auto-advances to the demo;
// at FRAMES_PER_TICK=2 that is 372 ticks, so a 500-tick budget clears it. The title ticks are cheap (the
// walk only runs while playing). Returns true once the cabinet is demonstrating a game to an empty arcade.
function reachDemo(vm) {
  vm.greenFlag();
  step(vm, 1);
  let t = 0;
  while (!(state(vm) === 'playing' && readVar(vm, 'cabinet-attract') === 1) && t < 600) {
    step(vm, 1);
    t += 1;
  }
  return state(vm) === 'playing' && readVar(vm, 'cabinet-attract') === 1;
}

// CAB-01 (slice 17): drive a live demo craft to its death and on to the attract sub-state it routes to. A
// demo runs with the craft vulnerable (`invuln` stays 0, unlike `reachPlaying`), so a Toroid seeded on the
// craft's cell each tick forces the death the arcade demo reaches when `scroll_disabled` fires. Steps until
// `dest` (or the budget) and reports whether it landed. The demo-death branch never spends a craft.
function killDemoToState(vm, dest, budget = 40) {
  let reached = false;
  for (let i = 0; i < budget && !reached; i += 1) {
    seedCraftHit(vm);
    step(vm, 1);
    if (state(vm) === dest) reached = true;
  }
  return reached;
}

// reachDemo, then kill the first demo into the best-five (attract-scores) screen it routes to after demo 1.
function reachScores(vm) {
  if (!reachDemo(vm)) return false;
  return killDemoToState(vm, 'attract-scores');
}

// reachScores, then step past the 512-frame (~256-tick) best-five hold into the SECOND demo — playing, with
// the attract flag still raised and the stage index advanced to 2 (the two-demos-per-cycle arcade loop).
function reachSecondDemo(vm) {
  if (!reachScores(vm)) return false;
  let t = 0;
  while (!(state(vm) === 'playing' && readVar(vm, 'cabinet-attract') === 1) && t < 400) {
    step(vm, 1);
    t += 1;
  }
  return state(vm) === 'playing' && readVar(vm, 'cabinet-attract') === 1;
}

// Every read resolves through a manifest id (hard-errors on a rename), including the
// scope-duplicated ones: `terrain-scroll-step-a` is area_01a's, distinct from area_01b's.
const state = stateOf;
const epoch = (vm) => readVar(vm, 'game-director-epoch');
const outcome = (vm) => readVar(vm, 'game-director-death-outcome');
const bombInFlight = (vm) => readVar(vm, 'weapon-bomb-in-flight');
const scrollA = (vm) => readVar(vm, 'terrain-scroll-step-a');
// Step until `pred(vm)` holds or the budget runs out; returns whether it held. Used where a finish now
// routes through the terminal GAME OVER hold (high-score-entry -> game-over -> title) rather than straight
// to the title, so reaching the title takes more than a couple of pumps.
function stepUntil(vm, pred, budget = 240) {
  let t = 0;
  while (!pred(vm) && t < budget) {
    step(vm, 1);
    t += 1;
  }
  return pred(vm);
}

// CAB-04 (cabinet.high-scores, slice 19): drop the cabinet directly into the high-score-entry screen in
// isolation. The game-over routing that reaches it in real play arrives in C4; until then the C3 entry
// screen is driven on its own. We arm the entry scope the way the real routing will — set the reset scope,
// fire `director reset` so entry_reset inits the entry machinery (char = space/ring index 26 — the blanked
// cell — buffer = '', cell = 0, timer = the full 2048-frame countdown), seed the rank-in result
// (`entry row`/`entry player`) the finish writes back, set
// the state and (optionally) override the countdown, then fire `director enter` so the entry_enter countdown
// loop starts. A boot step after green flag is required before any key press: scratch-vm "when key pressed"
// hats only arm after the first step (keypress-hats-need-boot-step), and a second step lets the enter loop
// take hold. Returns true once the cabinet is on the entry screen.
function enterEntry(vm, { row = 3, player = 0, timer } = {}) {
  vm.greenFlag();
  step(vm, 2); // settle boot->title AND arm the key hats (boot step)
  writeVar(vm, 'game-director-reset-scope', 'entry');
  fireBroadcast(vm, 'director reset'); // entry_reset: entry char = space (26), buffer = '', cell = 0, timer = full
  writeVar(vm, 'cabinet-entry-row', row);
  writeVar(vm, 'cabinet-entry-player', player);
  writeVar(vm, 'game-director-state', 'high-score-entry');
  if (timer !== undefined) writeVar(vm, 'cabinet-entry-timer', timer);
  fireBroadcast(vm, 'director enter'); // entry_enter: start the fixed-countdown loop
  step(vm, 1);
  return state(vm) === 'high-score-entry';
}

// --- BOSS-01 / andor.lifecycle (#94) shared scenario helpers -------------------------------------------
// Geometry read straight from tools/game_director.py (source-verified against the pin). The master's
// shared anchor `andor master x` descends from ANDOR_START_X (off the top edge) toward ANDOR_HOLD_X by
// ANDOR_DESCEND_STEP each tick and holds; on the end flag it retreats up by ANDOR_LEAVE_STEP and, once
// clear of the top, tears the composite down. Each visible part pins to the anchor + its per-type offset.
// GROUND band = Scratch slots 1..16 (JS index 0..15); the arm stamps the 15 parts into Scratch slots
// 2..16 (arcade obj 1..15), master LAST at Scratch slot 16 (JS 15); the obj-0 flag slot (JS 0) stays free.
const ANDOR = {
  START_X: -2048,
  HOLD_X: 4096,
  DESCEND_STEP: 64,
  LEAVE_STEP: 32,
  LATERAL_Y: 3712,
  MASTER_TYPE: 75, // 0x4B invisible master
  CORE_TYPE: 74, // 0x4A
  BASE_SLOT: 1, // GROUND_SLOTS[0]; parts occupy Scratch slots base+1..base+15, master at base+15 (16)
};
// The source arm order (ANDOR_GENESIS_DATA): 9 armor (0x41..0x49), 4 ports (0x52,0x51,0x50,0x4F), core
// (0x4A), master (0x4B) — obj 1..15 -> Scratch slots 2..16.
const ANDOR_PART_TYPES_BY_OBJ = [
  0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x52, 0x51, 0x50, 0x4f, 0x4a, 0x4b,
];

/** A put(id, jsIndex, value) writer into a Stage slot list (kebab manifest id). */
function slotPutter(vm) {
  return (id, i, v) => {
    readVar(vm, id)[i] = v;
  };
}
/** Zero the whole ground band (Scratch slots 1..16 = JS 0..15) type+state — the isolation pre-seed every
 * boss scenario shares (freeze the director first so the live walk can't re-stamp it). */
function clearGroundBand(vm) {
  const put = slotPutter(vm);
  for (let s = 0; s <= 15; s += 1) {
    put('slot-type', s, 0);
    put('slot-state', s, 0);
  }
}
/** Read a ground-pool clone's graphic effect by the Scratch slot it is bound to (its sprite-local
 * `ground clone slot`). cloneReports does not surface effects, so read the clone target directly. */
function groundCloneEffect(vm, scratchSlot, effect = 'color') {
  const slotName = variable('ground-clone-slot').name; // "ground clone slot"
  for (const c of vm.runtime.targets) {
    if (c.isStage || c.isOriginal || !c.sprite || c.sprite.name !== 'ground') continue;
    let bound = null;
    for (const id of Object.keys(c.variables)) {
      if (c.variables[id].name === slotName) bound = c.variables[id].value;
    }
    if (Number(bound) === scratchSlot) {
      return {
        effect: (c.effects && c.effects[effect]) || 0,
        visible: c.visible,
        costume: c.sprite.costumes[c.currentCostume] ? c.sprite.costumes[c.currentCostume].name : null,
      };
    }
  }
  return null;
}

export const SCENARIOS = [
  {
    key: 'shot-cap-ceiling',
    behavior:
      'Held fire never puts a shot on the field while all 3 shot slots are live, and fires again as soon as one frees',
    playtestStep: 2,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // PRES-01: a shot now expires at arcade row 0 (about 12 ticks of flight), sooner than two 10-tick
      // reloads, so live held fire tops out at two shots and the third slot is never needed. One headless pump
      // is also many ticks (wall-clock paced), so a shot can be born and retired between two samples. The
      // ceiling is therefore exercised at the allocator, pacing-invariantly: hold the three shot slots live
      // with placeholder occupants (no clone owns them, so nothing frees them) and hold fire.
      const types = () => readVar(vm, 'slot-type');
      const states = () => readVar(vm, 'slot-state');
      const occupy = (indices) => {
        for (const i of indices) {
          types()[i] = 1;
          states()[i] = 1;
        }
      };
      keyDown(vm, ' ');
      // Phase 1: all three slots live. No shot may spawn, and the reload is never consumed (an allocation
      // failure leaves it primed), so the counter keeps climbing — at least one tick per pump.
      let fullClones = 0;
      for (let i = 0; i < 60; i += 1) {
        occupy([36, 37, 38]);
        step(vm, 1);
        fullClones = Math.max(fullClones, cloneCount(vm, 'blaster'));
      }
      const fullReload = readVar(vm, 'weapon-blaster-reload');
      // Phase 2: free one slot (keep the other two occupied). Held fire now cycles real shots through that one
      // slot: every spawn resets the reload, so it stays bounded by one shot's flight time however many pumps
      // pass, and no more than one shot clone is ever on the field.
      types()[38] = 0;
      states()[38] = 0;
      let freedClones = 0;
      for (let i = 0; i < 60; i += 1) {
        occupy([36, 37]);
        step(vm, 1);
        freedClones = Math.max(freedClones, cloneCount(vm, 'blaster'));
      }
      const freedReload = readVar(vm, 'weapon-blaster-reload');
      keyUp(vm, ' ');
      return { fullClones, fullReload, freedClones, freedReload };
    },
    assert(obs) {
      assert.equal(obs.fullClones, 0, 'with all three shot slots live, held fire puts no shot on the field');
      assert.ok(obs.fullReload > 60, `a refused fire leaves the reload primed (reload ${obs.fullReload})`);
      assert.ok(obs.freedClones <= 1, `with one slot free, at most one shot is ever on the field (${obs.freedClones})`);
      assert.ok(obs.freedReload < 40, `held fire keeps spawning through the freed slot (reload ${obs.freedReload})`);
    },
    // Make the allocator treat a LIVE slot (type 1) as free, so it claims an occupied slot past the ceiling →
    // a shot spawns while all three are live and the reload is consumed → the phase-1 assertions go red.
    negativeMutation: (p) => mutate.changeListItemEqualsOperand(p, 'blaster', 'slot type', 0, 1),
  },
  {
    key: 'pres01-craft-stops-at-stop-lines',
    // roadmap-evidence: PRES-01 success  (the craft held into each edge settles exactly on its stop line and
    //   never passes it — the arcade clamp through the render map, with right still moving +x)
    behavior:
      'PRES-01: the craft held into each edge stops exactly at its arcade stop line (top 30, bottom -170, sides ±130) and never passes it; right arrow still moves the craft right',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const craft = vm.runtime.getSpriteTargetByName('solvalou');
      const out = {};
      // Each push is longer than the farthest stop line is from anywhere on the field (200 units at 2.5 per
      // tick = 80 ticks; 260 at 3.75 = 70), and the extreme is tracked on EVERY pump, so an overshoot that is
      // later corrected would still show. (A pump runs several ticks headless, so the per-tick speeds are
      // pinned structurally in tests/test_scratch_project.py, PRES01-craft-speed.)
      const push = (key, axis, pick) => {
        keyDown(vm, key);
        let extreme = craft[axis];
        for (let i = 0; i < 100; i += 1) {
          step(vm, 1);
          extreme = pick(extreme, craft[axis]);
        }
        keyUp(vm, key);
        step(vm, 1);
        return { final: craft[axis], extreme };
      };
      out.up = push('ArrowUp', 'y', Math.max);
      out.down = push('ArrowDown', 'y', Math.min);
      out.right = push('ArrowRight', 'x', Math.max);
      out.left = push('ArrowLeft', 'x', Math.min);
      return out;
    },
    assert(obs) {
      assert.equal(obs.up.final, constants.craft_y_top, 'held up, the craft settles on the top stop line');
      assert.equal(obs.up.extreme, constants.craft_y_top, 'the craft never passes the top stop line');
      assert.equal(obs.down.final, constants.craft_y_bottom, 'held down, the craft settles on the bottom stop line');
      assert.equal(obs.down.extreme, constants.craft_y_bottom, 'the craft never passes the bottom stop line');
      assert.equal(obs.right.final, constants.craft_x_limit, 'held right, the craft settles on the right stop line');
      assert.equal(obs.right.extreme, constants.craft_x_limit, 'the craft never passes the right stop line');
      assert.equal(obs.left.final, -constants.craft_x_limit, 'held left, the craft settles on the left stop line');
      assert.equal(obs.left.extreme, -constants.craft_x_limit, 'the craft never passes the left stop line');
    },
    // roadmap-evidence: PRES-01 failure  (with the top limit disarmed the craft runs on to the Scratch stage
    //   fence above the stop line, so the top-stop assertions go red)
    negativeMutation: (p) => mutate.raiseGreaterThreshold(p, 'solvalou', constants.craft_y_top, 99999),
  },
  {
    key: 'pres01-right-arrow-lowers-player-col',
    // roadmap-evidence: PRES-01 success  (the mirror: the craft held right reads a LOWER arcade lateral column
    //   than held left — the arcade's lateral Y increases to the left — and each stop reads its arcade column)
    behavior:
      'PRES-01: the left-right mirror — holding right moves the craft to arcade lateral column 2 (Y 16, the right stop) and holding left to column 28 (Y 224), because the arcade\'s lateral axis increases to the LEFT; every aim and hit test reads this column',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const hold = (key) => {
        keyDown(vm, key);
        for (let i = 0; i < 100; i += 1) step(vm, 1);
        keyUp(vm, key);
        step(vm, 1);
        return readVar(vm, 'player-col');
      };
      return { right: hold('ArrowRight'), left: hold('ArrowLeft') };
    },
    assert(obs) {
      assert.equal(Number(obs.right), 2, 'held right, the craft reads arcade column 2 (Y 16, the right stop)');
      assert.equal(Number(obs.left), 28, 'held left, the craft reads arcade column 28 (Y 224, the left stop)');
      assert.ok(Number(obs.right) < Number(obs.left), 'right is the LOWER lateral column (lateral Y increases left)');
    },
    // roadmap-evidence: PRES-01 failure  (with the player read's offset sign flipped the craft reads a column
    //   off the arcade grid, so both stop-column assertions go red)
    negativeMutation: (p) => mutate.changeAddLiteral(p, 'Stage', -150, 150),
  },
  {
    key: 'pres01-shot-expires-past-row-0',
    // roadmap-evidence: PRES-01 success  (a held-fire shot travels on past the top of the window and is retired
    //   as it reaches arcade row 0 — drawn only inside the window, hit-tested up to row 0 — and its slot frees
    //   for the next shot, so held fire keeps replenishing)
    behavior:
      'PRES-01: a player shot is drawn only inside the window (up to stage y 180), keeps travelling and is hit-tested through the hidden rows above it, and is retired as it reaches arcade row 0 — its freed slot lets held fire keep firing beyond the 3-shot ceiling',
    playtestStep: 2,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const shots = new Map(); // clone id -> highest y the shot was seen DRAWN at
      // The hit position the walk tests: the slot x the shot mirrors (JS indices of the three shot slots).
      const shotSlots = [36, 37, 38];
      let reach = Infinity;
      // One harness step runs several ticks and a shot covers its last step-unit before row 0 in ONE tick, so
      // sampling only at step boundaries caught it there by luck (an intermittent red). Sample the shot slots
      // after every thread step the sequencer runs instead.
      const seq = vm.runtime.sequencer;
      const original = seq.stepThread;
      seq.stepThread = function hooked(thread) {
        original.call(this, thread);
        const types = readVar(vm, 'slot-type');
        const xs = readVar(vm, 'slot-x');
        for (const s of shotSlots) if (Number(types[s]) !== 0) reach = Math.min(reach, Number(xs[s]));
      };
      keyDown(vm, ' ');
      try {
        for (let i = 0; i < 60; i += 1) {
          step(vm, 1);
          for (const t of vm.runtime.targets) {
            if (t.isStage || t.isOriginal || !t.sprite || t.sprite.name !== 'blaster') continue;
            if (!shots.has(t.id)) shots.set(t.id, -Infinity);
            if (t.visible) shots.set(t.id, Math.max(shots.get(t.id), t.y));
          }
        }
      } finally {
        seq.stepThread = original;
      }
      keyUp(vm, ' ');
      const tops = [...shots.values()];
      // Phase 2: shots fired from the craft's top stop (y 30, the shortest run to row 0) are still never drawn
      // above the window. (That each shot is hit-tested at its spawn row — mirror before move — is pinned
      // structurally as PRES01-shot-hit-reach: a pump runs several ticks headless, so the first mirror is
      // overwritten before it can be sampled.)
      keyDown(vm, 'ArrowUp');
      for (let i = 0; i < 100; i += 1) step(vm, 1);
      const craft = vm.runtime.getSpriteTargetByName('solvalou');
      const craftY = craft.y;
      let drawnFromTop = -Infinity;
      keyDown(vm, ' ');
      for (let i = 0; i < 30; i += 1) {
        step(vm, 1);
        for (const t of vm.runtime.targets) {
          if (t.isStage || t.isOriginal || !t.sprite || t.sprite.name !== 'blaster' || !t.visible) continue;
          drawnFromTop = Math.max(drawnFromTop, t.y);
        }
      }
      keyUp(vm, ' ');
      keyUp(vm, 'ArrowUp');
      return { fired: shots.size, highest: Math.max(...tops), reach, craftY, drawnFromTop };
    },
    assert(obs) {
      assert.ok(
        obs.fired > constants.shot_slot_count,
        `held fire replenishes past the 3-shot ceiling as shots expire (fired ${obs.fired})`,
      );
      // One shot step in slot units (SHOT_STEP 15 stage units = 1.5 rows).
      const stepUnits = (15 * constants.slot_units_per_cell) / constants.render_row_stage;
      assert.ok(obs.highest <= constants.render_stage_top, `no shot is drawn above the window (highest y ${obs.highest})`);
      assert.ok(
        obs.highest > constants.render_stage_top - 15,
        `a shot was drawn up to the top of the window (highest y ${obs.highest})`,
      );
      // Hidden past the window, the shot still travels and is hit-tested up to row 0: the slot x the walk reads
      // gets within one shot step of row 0, and never past it.
      assert.ok(
        obs.reach >= 0 && obs.reach < stepUnits,
        `the shot's hit position reaches row 0 before it retires, never past it (closest slot x ${obs.reach})`,
      );
      assert.equal(obs.craftY, constants.craft_y_top, 'precondition: the craft sits at its top stop');
      assert.ok(obs.drawnFromTop > constants.craft_y_top, 'precondition: shots fired from the top stop were drawn');
      assert.ok(obs.drawnFromTop <= constants.render_stage_top, `no shot from the top stop is drawn above the window`);
    },
    // roadmap-evidence: PRES-01 failure  (with the row-0 expiry disarmed the first three shots fly on past row 0
    //   and never free their slots, so held fire stalls at three)
    negativeMutation: (p) => mutate.raiseGreaterThreshold(p, 'blaster', constants.render_row_top, 99999),
  },
  {
    key: 'pres01-world-hidden-off-field',
    // roadmap-evidence: PRES-01 success  (a live world object is shown only while its row is inside the window,
    //   rows 4-39, and hidden in rows 0-3 above the stage top where Scratch would fence it onto the edge)
    behavior:
      'PRES-01: a live world object is drawn only while its row is inside the window (rows 4-39) — a Bacura held at row 3 (alive, above the window) is hidden, and the same slab at rows 4 and 39 is shown',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 1); // director enter creates one render clone per Bacura band slot
      const slotName = variable('bacura-clone-slot').name;
      const slot = 16; // JS index; Scratch 1-based slot 17 (first Bacura band slot)
      // Isolate (as bacura-tumbles-by-position): clear the band and stop the pump, then seed one held slab.
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
      put('slot-dx', 0);
      put('slot-dy', 0);
      writeVar(vm, 'slot-index', slot + 1);
      const U = constants.slot_units_per_cell;
      const at = (row, offset) => {
        put('slot-x', row * U + offset);
        step(vm, 1);
        const rep = cloneReports(vm, 'bacura', [slotName]).find((r) => Number(r.vars[slotName]) === slot + 1);
        return {
          visible: rep ? rep.visible : null,
          alive: readVar(vm, 'slot-type')[slot] === 1,
          x: Number(readVar(vm, 'slot-x')[slot]),
        };
      };
      // Row 3 is a live object (the walk culls only at row <= -2), so hiding it is the gate's work. It is
      // seeded at the row's start so any scroll during the step keeps it inside row 3 (checked by the assert).
      return { row4: at(4, 32), row3: at(3, 0), row39: at(constants.render_view_rows - 1, 32) };
    },
    assert(obs) {
      const first = constants.render_view_first_row * constants.slot_units_per_cell;
      assert.equal(obs.row4.visible, true, 'a slab at row 4, the first row inside the window, is shown');
      assert.equal(obs.row3.alive, true, 'precondition: the slab at row 3 is still a live object');
      assert.ok(obs.row3.x < first, `precondition: the slab is still in row 3 when sampled (slot x ${obs.row3.x})`);
      assert.equal(obs.row3.visible, false, 'a live slab at row 3, above the window, is hidden');
      assert.equal(obs.row39.visible, true, 'a slab at row 39, the last on-field row, is shown');
    },
    // roadmap-evidence: PRES-01 failure  (with the gate's lower bound widened, the live slab at row 3 is
    //   drawn fenced onto the stage top and the hidden assertion goes red)
    negativeMutation: (p) => {
      const t = p.targets.find((x) => x.name === 'bacura');
      let patched = 0;
      for (const b of Object.values(t.blocks)) {
        if (b.opcode !== 'operator_not' || !b.inputs.OPERAND) continue;
        const lt = t.blocks[b.inputs.OPERAND[1]];
        if (!lt || lt.opcode !== 'operator_lt') continue;
        const lhs = t.blocks[lt.inputs.OPERAND1[1]];
        const rhs = lt.inputs.OPERAND2[1];
        const isSlotX = lhs && lhs.opcode === 'data_itemoflist' && lhs.fields.LIST[0] === 'slot x';
        const first = constants.render_view_first_row * constants.slot_units_per_cell;
        if (isSlotX && Array.isArray(rhs) && Number(rhs[1]) === first) {
          lt.inputs.OPERAND2 = [1, [4, '-99999']];
          patched += 1;
        }
      }
      if (!patched) throw new Error("mutate: no in-view gate 'not (slot x < first row)' on bacura");
    },
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
      // craft alive so it does not die back to the title before we observe playing. CAB-02 (slice 17):
      // the start now costs a credit, so bank one first or the held start is a silent no-op.
      insertCoin(vm, 1);
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
    // CAB-02 (cabinet.attract-credits, slice 17), coin side: the always-on coin poll adds one credit per
    // coin on the rising edge, and the bank caps at 99 (arcade `cmp.b #0x99` BCD ceiling; sub 171-206).
    key: 'coins-bank-credits',
    behavior: 'Each coin banks one credit; the credit bank caps at 99',
    playtestStep: 1,
    async drive(vm) {
      vm.greenFlag();
      step(vm, 1);
      const boot = readVar(vm, 'cabinet-credits'); // 0 at power-on
      insertCoin(vm, 2);
      const afterTwo = readVar(vm, 'cabinet-credits'); // two coins -> two credits
      // Seed one below the cap and insert two: one lands, the second is rejected at the ceiling.
      writeVar(vm, 'cabinet-credits', 98);
      insertCoin(vm, 2);
      const capped = readVar(vm, 'cabinet-credits'); // 99, not 100
      return { boot, afterTwo, capped };
    },
    assert(obs) {
      assert.equal(obs.boot, 0, 'no credits at power-on');
      assert.equal(obs.afterTwo, 2, 'two coins bank two credits');
      assert.equal(obs.capped, 99, 'the credit bank caps at 99');
    },
    // Sever the coin poll body: coins can no longer bank → afterTwo is 0, the assertion fails.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'cabinet coin poll'),
  },
  {
    // CAB-02, start side: a 1-player start is credit-gated — ignored at 0 credits, and when a credit is
    // banked it spends exactly one and begins the game (arcade `cmp.b #1,(num_credits) ; jcs` then `sbcd`).
    key: 'one-player-start-costs-a-credit',
    behavior: 'A 1P start is ignored without a credit, and spends exactly one when it has it',
    playtestStep: 1,
    async drive(vm) {
      vm.greenFlag();
      step(vm, 1);
      // A start press with an empty bank is ignored: the machine stays at the title.
      tapKey(vm, ' ');
      step(vm, 3);
      const noCreditState = state(vm); // 'title'
      // Bank one credit, then start: it reaches playing and the bank drops back to 0.
      insertCoin(vm, 1);
      writeVar(vm, 'invuln', 1);
      tapKey(vm, ' ');
      let started = false;
      for (let i = 0; i < 120 && !started; i += 1) {
        step(vm, 1);
        if (state(vm) === 'playing') started = true;
      }
      const spent = readVar(vm, 'cabinet-credits'); // 0 after spending the one credit
      return { noCreditState, started, spent };
    },
    assert(obs) {
      assert.equal(obs.noCreditState, 'title', 'a start with no credit is ignored');
      assert.equal(obs.started, true, 'a start with a credit reaches playing');
      assert.equal(obs.spent, 0, 'a 1P start spends exactly one credit');
    },
    // Freeze every `change credits` (the +1 coin and the -1 start) → the banked coin never lands, the
    // credited start never fires, `started` is false: the assertion fails.
    negativeMutation: (p) => mutate.freezeVariableChange(p, 'Stage', 'credits'),
  },
  {
    // CAB-01 (cabinet.attract-credits, slice 17): an idle cabinet auto-launches its demo. The title stage
    // holds 744 frames (~372 ticks) then advances to `playing` with the attract flag still raised — a game
    // demonstrated to an empty arcade, not a real game. (arcade `attract_mode_main_loop` main 359-370; the
    // title stage is main 1217-1296.)
    key: 'attract-title-launches-demo',
    behavior: 'An idle title auto-launches the attract demo (playing, with the attract flag still raised)',
    playtestStep: 1,
    async drive(vm) {
      const launched = reachDemo(vm); // steps past the ~372-tick title hold
      return { launched, st: state(vm), attract: readVar(vm, 'cabinet-attract') };
    },
    assert(obs) {
      assert.equal(obs.launched, true, 'the idle title auto-advances to the demo');
      assert.equal(obs.st, 'playing', 'the demo runs on the playing state');
      assert.equal(obs.attract, 1, 'the demo keeps the attract flag raised');
    },
    // Remove the title -> playing edge so the auto-launch is a silent no-op → the demo never starts.
    negativeMutation: (p) => mutate.removeAllowedTransition(p, 'title -> playing'),
  },
  {
    // CAB-01: the demo ends the way the arcade demo does — the craft dies (no timer). A demo death routes to
    // the best-five (attract-scores) screen, spends no craft, and keeps the attract cycle running; it never
    // hits the real death paths (respawn / game-over). (demo exit main 1298-1328; the death branch spends no
    // life because the score/lives setup runs only on the credited-start path, `coined_up` main 398-417.)
    key: 'attract-demo-death-to-best-five',
    behavior: 'A demo death routes to the best-five screen, spends no craft, and stays in the attract cycle',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(reachDemo(vm), 'precondition: the demo is running');
      const routed = killDemoToState(vm, 'attract-scores');
      return { routed, st: state(vm), attract: readVar(vm, 'cabinet-attract') };
    },
    assert(obs) {
      assert.equal(obs.routed, true, 'a demo death reaches the best-five screen');
      assert.equal(obs.st, 'attract-scores', 'the demo does not fall through to a real death path');
      assert.equal(obs.attract, 1, 'the attract cycle keeps running through the best-five screen');
    },
    // Remove the playing -> attract-scores edge so a demo death cannot route to the best-five screen.
    negativeMutation: (p) => mutate.removeAllowedTransition(p, 'playing -> attract-scores'),
  },
  {
    // CAB-01: the best-five screen is itself timed (512 frames, ~256 ticks) and then launches the SECOND
    // demo of the cycle — two demos per attract cycle, title -> demo -> best-five -> demo -> title. The
    // second demo runs at stage index 2. (best-five stage main 1336-1344; the shared demo handler is
    // `attract_mode_jump_tbl` main 1211-1216, dispatched for both demo slots.)
    key: 'attract-scores-launches-second-demo',
    behavior: 'The best-five screen is timed and launches the second demo of the attract cycle',
    playtestStep: 1,
    async drive(vm) {
      const launched = reachSecondDemo(vm); // demo 1 -> best-five hold -> demo 2
      return {
        launched,
        st: state(vm),
        attract: readVar(vm, 'cabinet-attract'),
        stage: readVar(vm, 'cabinet-attract-stage'),
      };
    },
    assert(obs) {
      assert.equal(obs.launched, true, 'the best-five hold advances to the second demo');
      assert.equal(obs.st, 'playing', 'the second demo runs on the playing state');
      assert.equal(obs.attract, 1, 'the second demo keeps the attract flag raised');
      assert.equal(obs.stage, 2, 'the second demo runs at attract stage index 2');
    },
    // Remove the attract-scores -> playing edge so the best-five hold can never launch the second demo.
    negativeMutation: (p) => mutate.removeAllowedTransition(p, 'attract-scores -> playing'),
  },
  {
    // CAB-01: after the SECOND demo the cycle returns to the title (not back to the best-five) — the demo
    // death handler reads the stage index (2, not 1) and routes to `title`, closing the loop. The attract
    // flag stays raised: the cabinet is still idling.
    key: 'attract-second-demo-death-returns-to-title',
    behavior: 'The second demo death closes the cycle back to the title, still idling',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(reachSecondDemo(vm), 'precondition: the second demo is running');
      const routed = killDemoToState(vm, 'title');
      return { routed, st: state(vm), attract: readVar(vm, 'cabinet-attract') };
    },
    assert(obs) {
      assert.equal(obs.routed, true, 'the second demo death reaches the title');
      assert.equal(obs.st, 'title', 'the cycle closes back to the title, not the best-five screen');
      assert.equal(obs.attract, 1, 'the cabinet stays in its attract cycle');
    },
    // Remove the playing -> title edge so the second demo death cannot close the cycle back to the title.
    negativeMutation: (p) => mutate.removeAllowedTransition(p, 'playing -> title'),
  },
  {
    // CAB-02: inserting a coin during the attract cycle resets the machine to the title and banks the credit
    // (it does not start a game — that still needs a start press). The attract flag stays raised: the coin
    // returns to the title but the cabinet is still in its attract cycle. (arcade `coined_up` main 377-388
    // resets the attract state on coin-in from any attract sub-state.)
    key: 'attract-coin-aborts-to-title',
    behavior: 'A coin during the demo returns to the title and banks the credit, still idling',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(reachDemo(vm), 'precondition: the demo is running');
      const before = readVar(vm, 'cabinet-credits'); // 0 — no credits banked yet this power-on
      insertCoin(vm, 1); // the always-on poll banks the credit AND the abort fires on the same edge tick
      step(vm, 3); // let the abort transition's reset/enter broadcasts settle onto the title
      return {
        before,
        credits: readVar(vm, 'cabinet-credits'),
        st: state(vm),
        attract: readVar(vm, 'cabinet-attract'),
      };
    },
    assert(obs) {
      assert.equal(obs.before, 0, 'the demo runs with no credits banked');
      assert.equal(obs.st, 'title', 'a coin during the demo returns to the title');
      assert.equal(obs.credits, 1, 'the inserted coin is banked, not lost');
      assert.equal(obs.attract, 1, 'the coin returns to the title but stays in the attract cycle');
    },
    // Remove the playing -> title edge so the coin abort is a silent no-op → the demo keeps running.
    negativeMutation: (p) => mutate.removeAllowedTransition(p, 'playing -> title'),
  },
  {
    // CAB-02: a credit-gated real start atomically clears the attract flag, so a started game can never
    // inherit the attract cycle (no auto-pilot, scoring, real deaths). `reachPlaying` is the shared start
    // path (~20 scenarios), so this also pins that every gameplay scenario runs a REAL game, not a demo.
    key: 'attract-cleared-on-credited-start',
    behavior: 'A credited start clears the attract flag — a started game is a real game, never a demo',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: a credited start reaches playing');
      return { st: state(vm), attract: readVar(vm, 'cabinet-attract') };
    },
    assert(obs) {
      assert.equal(obs.st, 'playing', 'the credited start reaches playing');
      assert.equal(obs.attract, 0, 'the started game clears the attract flag (a real game, not a demo)');
    },
    // Pin `attract` set to 1 so the credited start cannot clear it → the started game looks like a demo.
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'attract', 1),
  },
  {
    // CAB-01 (cabinet.attract-credits, slice 17): the self-playing demo's auto-pilot actually drives the
    // craft. During a demo the six keyboard reads are replaced by the virtual input register (`input up/
    // down/left/right/fire`) that `install_attract_pilot` writes each tick, so the craft moves with no keys
    // touched. Steps a live demo (invuln pinned so it does not die and route away mid-observation) and
    // watches the craft's logical position (`player-row`/`player-col`) wander from where it entered.
    // (arcade auto-pilot `gen_rnd_dir` xevious_main 2156-2165, dispatched when `attract_mode_stage != 0`.)
    // roadmap-evidence: CAB-01 success  (the demo craft moves only because the auto-pilot feeds the virtual
    //   input register the movement seams read via input_active)
    key: 'attract-pilot-drives-craft',
    behavior: 'The demo auto-pilot drives the craft — it moves with no keyboard input',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(reachDemo(vm), 'precondition: the demo is running');
      const r0 = readVar(vm, 'player-row');
      const c0 = readVar(vm, 'player-col');
      let maxRowDev = 0;
      let maxColDev = 0;
      for (let i = 0; i < 64; i += 1) {
        writeVar(vm, 'invuln', 1); // keep the demo craft alive so we watch it move, not die
        step(vm, 1);
        maxRowDev = Math.max(maxRowDev, Math.abs(readVar(vm, 'player-row') - r0));
        maxColDev = Math.max(maxColDev, Math.abs(readVar(vm, 'player-col') - c0));
      }
      return { maxRowDev, maxColDev, attract: readVar(vm, 'cabinet-attract') };
    },
    assert(obs) {
      assert.equal(obs.attract, 1, 'the craft moves while still in the attract demo');
      assert.ok(
        obs.maxRowDev > 0 || obs.maxColDev > 0,
        'the auto-pilot moves the demo craft (no keyboard input)',
      );
    },
    // Sever the auto-pilot: the virtual input register stays at its demo-entry zero, so no movement seam
    // ever fires and the craft never moves → both deviations are 0, the assertion fails.
    // roadmap-evidence: CAB-01 failure  (neutralizing the pilot proc leaves the craft motionless)
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'attract pilot'),
  },
  {
    // CAB-01 / SYS-04 (cabinet-flow reproducibility): the auto-pilot is deterministic — two demos seeded
    // from the same RNG cold-start replay the identical input stream. The pilot draws direction and fire
    // from the shared RNG (`rng out`) at a fixed position in the ordered walk, so its output is a pure
    // function of (seed, tick). This captures that output DIRECTLY (a tick-pump that seeds the seed, sets
    // `tick`, runs the `attract pilot` proc, and reads its six output flags) rather than per-`step()`
    // craft sampling — one `step()` settles a machine-dependent number of internal ticks, so a live sample
    // is not a stable stream (deterministic-per-tick-testing-of-walk-procs). Two identical pumps from the
    // same seed must yield byte-identical traces; the direction stays in the reject-sampled 0..8 range and
    // decomposes into the four flags exactly per `dir_delta_tbl` (xevious_main 2156-2180); fire is the
    // 1-in-16 draw (`gen_rnd_shot` 2351-2354). RNG_COLD_START_SEED = 0x4A39, the demo-entry reseed value.
    // roadmap-evidence: CAB-01 success  (two seeded pilot runs replay the identical, source-faithful stream)
    key: 'attract-pilot-reproducible',
    behavior: 'Two seeded auto-pilot runs replay the identical input stream (0..8 dir, exact flag decomposition, 1-in-16 fire)',
    playtestStep: 1,
    async drive(vm) {
      const inputs = [
        'cabinet-input-up',
        'cabinet-input-down',
        'cabinet-input-left',
        'cabinet-input-right',
        'cabinet-input-fire',
      ];
      // A deterministic tick-pump: reseed to the demo-entry cold-start, then for each tick set `tick`,
      // run the pilot proc, and record [dir, up, down, left, right, fire]. No greenFlag, so the only
      // thread is the one the proc call pushes — nothing else advances state (a clean, fixed environment).
      const pump = (ticks) => {
        writeVar(vm, 'rng-state', 0x4a39); // RNG_COLD_START_SEED
        writeVar(vm, 'cabinet-pilot-dir', 8); // neutral, as the demo-entry reset leaves it
        for (const id of inputs) writeVar(vm, id, 0);
        const trace = [];
        for (let t = 0; t < ticks; t += 1) {
          writeVar(vm, 'tick', t);
          callProc(vm, 'Stage', 'attract pilot');
          step(vm, 1);
          trace.push([
            readVar(vm, 'cabinet-pilot-dir'),
            readVar(vm, 'cabinet-input-up'),
            readVar(vm, 'cabinet-input-down'),
            readVar(vm, 'cabinet-input-left'),
            readVar(vm, 'cabinet-input-right'),
            readVar(vm, 'cabinet-input-fire'),
          ]);
        }
        return trace;
      };
      const traceA = pump(64);
      const traceB = pump(64); // re-seeds internally, so this is a second demo from the same cold-start
      return { traceA, traceB };
    },
    assert(obs) {
      assert.deepEqual(obs.traceA, obs.traceB, 'two seeded demos replay the identical input stream');
      const dirs = obs.traceA.map((e) => e[0]);
      assert.ok(
        dirs.every((d) => Number.isInteger(d) && d >= 0 && d <= 8),
        'every drawn direction is reject-sampled into 0..8',
      );
      assert.ok(
        dirs.some((d) => d < 8),
        'the pilot actually steers (at least one non-neutral direction)',
      );
      // Each direction decomposes into the four flags exactly per dir_delta_tbl (U / U+R / R / D+R / D /
      // D+L / L / U+L / none): up={0,1,7}, right={1,2,3}, down={3,4,5}, left={5,6,7}, dir 8 = none.
      for (const [d, up, down, left, right, fire] of obs.traceA) {
        assert.equal(up, d === 0 || d === 1 || d === 7 ? 1 : 0, `up flag matches dir ${d}`);
        assert.equal(right, d === 1 || d === 2 || d === 3 ? 1 : 0, `right flag matches dir ${d}`);
        assert.equal(down, d === 3 || d === 4 || d === 5 ? 1 : 0, `down flag matches dir ${d}`);
        assert.equal(left, d === 5 || d === 6 || d === 7 ? 1 : 0, `left flag matches dir ${d}`);
        assert.ok(fire === 0 || fire === 1, 'fire is a clean 0/1 flag');
      }
      assert.ok(
        obs.traceA.some((e) => e[5] === 1),
        'the 1-in-16 fire draw presses the button over the window',
      );
    },
    // Sever the pilot: the proc runs to a no-op, so `pilot dir` stays neutral (8) and every input flag
    // stays 0 for the whole trace → the "at least one non-neutral direction" assertion fails.
    // roadmap-evidence: CAB-01 failure  (a severed pilot produces an all-neutral, non-steering stream)
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'attract pilot'),
  },
  {
    // CAB-01 / SEC-03 coupling (slice 17): the arcade silently removes the hidden-credit object during
    // attract — `handle_53_Easter_Egg` (xevious_main 5989) checks `tst.w attract_mode_stage` and jumps to
    // `remove_easter_egg` (6013-6016) before any of its logic. The demo shares the playing state and runs
    // the same area schedule, so a scheduled Credit CAN be stamped into a ground slot during a demo; the
    // attract gate in `update easter egg` culls it the tick its slot is dispatched, lowering the display
    // signal — the demo can never arm a bomb-reveal target. Seeds a hidden Credit into a ground slot during
    // a demo (with ground spawns suppressed so nothing refills it) and confirms it is culled, signal down.
    // roadmap-evidence: CAB-01 success  (a Credit seeded into a demo is culled, its overlay signal cleared)
    key: 'attract-suppresses-hidden-credit',
    behavior: 'The hidden-credit object is silently removed during a demo (SEC-03 attract coupling)',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(reachDemo(vm), 'precondition: the demo is running');
      suppressGroundSpawns(vm); // nothing refills the slot after the cull
      const put = (id, i, v) => {
        const a = readVar(vm, id);
        a[i] = v;
      };
      const SLOT = 8; // a mid ground slot (Scratch 1..16 = JS 0..15; obj-0 flag slot stays free)
      put('slot-type', SLOT, 83); // EASTER_EGG_TYPE (0x53), hidden Credit
      put('slot-state', SLOT, 1); // ACTIVE
      put('slot-flag', SLOT, 0); // HIDDEN phase
      put('slot-timer', SLOT, 0);
      put('slot-x', SLOT, 3000);
      put('slot-y', SLOT, 3000);
      const seededType = readVar(vm, 'slot-type')[SLOT];
      for (let i = 0; i < 4; i += 1) {
        writeVar(vm, 'invuln', 1); // keep the demo alive so the walk keeps dispatching the slot
        step(vm, 1);
      }
      return {
        seededType,
        culledType: readVar(vm, 'slot-type')[SLOT],
        culledState: readVar(vm, 'slot-state')[SLOT],
        showing: readVar(vm, 'sec-easter-egg-showing'),
      };
    },
    assert(obs) {
      assert.equal(obs.seededType, 83, 'the hidden Credit was seeded into the demo');
      assert.equal(obs.culledType, 0, 'the Credit slot is culled (type cleared) during the demo');
      assert.equal(obs.culledState, 0, 'the Credit slot is freed during the demo');
      assert.equal(obs.showing, 0, 'the credit overlay signal is never raised in attract');
    },
    // Sever the whole `update easter egg` proc (which carries the attract cull gate): the seeded Credit is
    // never removed, so its slot type stays 83 → the assertion fails.
    // roadmap-evidence: CAB-01 failure  (without the attract gate the Credit survives into the demo)
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update easter egg'),
  },
  {
    // CAB-04 (cabinet.high-scores, slice 19): the LIVE best-five table renders during the attract-scores
    // sub-state, and the live gameplay HUD is held OFF while it shows. Slice 17's single baked `best-five`
    // costume is retired: the table is now drawn cell-by-cell with the start_screen clone-role idiom — a rank
    // clone (attract role == 7, costume rank/<row>, the 1ST..5TH ordinal since PRES-01) per row, plus name-letter (role 8) and score-digit
    // (role 9) cells — each reading the `high score table`/`high score names` Stage lists live. This scenario
    // pins the CAB-01 compositional invariant that survives: the five rank digits show (so the table is on
    // screen) and the HUD spawn is excluded during attract-scores (its gate carries
    // `game state != "attract-scores"`), so the live score/high-score digit clones do not draw over the table.
    // The exact glyph/liveness mapping is pinned by high-score-table-live below.
    // roadmap-evidence: CAB-04 success  (the live table's rank digits dress themselves and the HUD is suppressed)
    key: 'attract-scores-render',
    behavior:
      'The live best-five table renders during attract-scores (per-cell port-font clones) and the HUD digits are held off',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(reachScores(vm), 'precondition: the attract cycle reaches the best-five screen');
      step(vm, 3); // let the transition retire the demo HUD clones and the table cells dress themselves
      const roleName = variable('attract-display-role').name; // "attract role"
      const rowName = variable('attract-display-row').name; // "attract row"
      const rankCells = cloneReports(vm, 'start_screen', [roleName, rowName]).filter(
        (r) => r.vars[roleName] === 7 && r.visible && /^rank\/[1-5]$/.test(r.costume || ''),
      );
      const rankCostumes = rankCells.map((r) => r.costume).sort();
      const hudDigits = cloneReports(vm, 'hud').filter((r) => /^digit\/[0-9]$/.test(r.costume || ''));
      return { st: state(vm), rankCostumes, hudDigits: hudDigits.length };
    },
    assert(obs) {
      assert.equal(obs.st, 'attract-scores', 'the observation is taken on the best-five screen');
      assert.deepEqual(
        obs.rankCostumes,
        ['rank/1', 'rank/2', 'rank/3', 'rank/4', 'rank/5'],
        'all five rank cells show (the live table is on screen, one ordinal per row)',
      );
      assert.equal(obs.hudDigits, 0, 'the live HUD score digits are held off while the table shows');
    },
    // Break the rank-cell dispatch (`attract role == 7`): the rank clones never match their role branch, so
    // none switches to its rank/<row> costume nor shows → the five-rank-cells assertion fails.
    // roadmap-evidence: CAB-04 failure  (a broken rank-role dispatch leaves the live table's ranks unrendered)
    negativeMutation: (p) => mutate.changeVarEqualsOperand(p, 'start_screen', 'attract role', 7, 999),
  },
  {
    // CAB-01 (cabinet.attract-credits, slice 17): the attract-display clones do not leak across cycles. The
    // start_screen static clones (the CREDIT label, the credit digits, the prompt) have no self-delete; they
    // rely on `common_stop(clones=True)` retiring every clone on each state transition. Without that the port
    // would accumulate a fresh CREDIT/digit/prompt clone every title -> demo -> best-five -> demo -> title lap
    // — a slow leak the single-state playing census can never see (feasibility plan-review S4). Runs three full
    // attract laps, sampling the start_screen clone count at the same phase (a settled title) each lap, and
    // asserts the count returns to its baseline instead of climbing. (The CAB-04 live best-five table cells are
    // created in attract-scores, not title; high-score-clone-no-leak samples those.)
    // roadmap-evidence: CAB-01 success  (per-transition clone retirement keeps the count flat across laps)
    key: 'attract-clone-no-leak',
    behavior: 'The attract-display clones retire per transition — their count returns to baseline each cycle',
    playtestStep: 1,
    async drive(vm) {
      vm.greenFlag();
      step(vm, 1);
      const atDemo = () => state(vm) === 'playing' && readVar(vm, 'cabinet-attract') === 1;
      const stepUntil = (pred, budget) => {
        let t = 0;
        while (!pred() && t < budget) {
          step(vm, 1);
          t += 1;
        }
        return pred();
      };
      // Advance to `dest`, killing any demo on the way. A demo can start and die inside a single pump (the
      // arcade-size craft hit box, PRES-01, ends a demo fast — and under the leak negative the saturated clone
      // pool ends it at once), so the lap is driven by its destination states, not by catching the demo live.
      const reach = (dest, budget) => {
        let t = 0;
        while (state(vm) !== dest && t < budget) {
          if (atDemo()) seedCraftHit(vm);
          step(vm, 1);
          t += 1;
        }
        return state(vm) === dest;
      };
      const samples = [];
      for (let cycle = 0; cycle < 3; cycle += 1) {
        assert.ok(stepUntil(() => state(vm) === 'title', 600), `cycle ${cycle}: the cabinet rests at title`);
        step(vm, 3); // let the title spawn its CREDIT/digit/prompt clones before sampling
        samples.push(cloneCount(vm, 'start_screen'));
        assert.ok(reach('attract-scores', 700), `cycle ${cycle}: title -> demo 1 -> best-five`);
        assert.ok(reach('title', 500), `cycle ${cycle}: best-five -> demo 2 -> title`);
      }
      return { samples };
    },
    assert(obs) {
      assert.ok(obs.samples[0] > 0, 'the title spawns its attract-display clones');
      for (let i = 1; i < obs.samples.length; i += 1) {
        assert.ok(
          obs.samples[i] <= obs.samples[0],
          `no attract-clone leak: lap ${i} holds ${obs.samples[i]} clones vs the ${obs.samples[0]} baseline`,
        );
      }
    },
    // Remove every `delete this clone` on start_screen so no attract clone is ever retired: each title lap
    // stacks fresh CREDIT/digit/prompt clones on the un-retired previous ones → the count climbs past
    // baseline and the no-leak assertion fails.
    // roadmap-evidence: CAB-01 failure  (without clone retirement the attract displays accumulate each lap)
    negativeMutation: (p) => mutate.removeDeleteThisClone(p, 'start_screen'),
  },
  {
    // CAB-04/ECO-04 (cabinet.high-scores, slice 19): the LIVE best-five table. `rank in` is a no-arg Stage warp
    // proc that reads `entry score` (the game-over routing sets it to the finishing player's score), scans ranks
    // 5..1 for the smallest rank the score reaches-or-beats (`>=`, a tie places — move_high_score_entry_down's
    // fall-through, xevious_main.68k:1653-1656), and on a place shifts the scores AND `high score names` down one
    // in lockstep (dropping the old fifth) and inserts the score with a blank name, leaving the rank in
    // `entry row` (0 = did not place). This is the C1 guard for riskiest-seam #2 — an off-by-one in the lockstep
    // shift silently corrupts the table or desyncs names<->scores; the exact expected-table/names assertions
    // below bind to the whole shift, not just a count. Driven in isolation via callProc over a seeded table (no
    // play state — rank in reads only Stage lists/vars). The real game calls rank in through a warp
    // procedures_call (atomic), but the harness's callProc pushes a thread directly on the definition, which
    // runs in NON-warp mode — so its repeat loops yield and a single step advances only part way. We therefore
    // drive it with NO green flag (nothing else competes for the step budget) and pump to completion; the final
    // list state is warp-independent, so the settled result equals the atomic one.
    // roadmap-evidence: CAB-04 success  (a qualifying score ranks in; the table stays length 5 and names track)
    key: 'high-score-rank-insert',
    behavior: 'rank in places a qualifying score into the live best-five table, shifting names in lockstep',
    playtestStep: 1,
    async drive(vm) {
      // Seed a known table + names by mutating the live arrays in place (not replacing the references).
      const seed = (id, vals) => {
        const a = readVar(vm, id);
        a.splice(0, a.length, ...vals);
      };
      // Run rank in to completion over a fresh seed and report the settled state. 40 pumps is far past the
      // ~9 loop iterations, and once the thread finishes extra pumps are no-ops, so the result is deterministic.
      const rankIn = (table, names, score) => {
        seed('eco-high-score-table', table);
        seed('eco-high-score-names', names);
        writeVar(vm, 'cabinet-entry-score', score);
        callProc(vm, 'Stage', 'rank in');
        step(vm, 40);
        return {
          row: readVar(vm, 'cabinet-entry-row'),
          table: readVar(vm, 'eco-high-score-table').slice(),
          names: readVar(vm, 'eco-high-score-names').slice(),
        };
      };
      // Qualifying score lands at rank 3 (beats 30000, below 35000).
      const placed = rankIn(
        [40000, 35000, 30000, 25000, 20000],
        ['STK', 'M.N', 'EVE', 'S.O', 'S.K'],
        32000,
      );
      // A non-qualifying score (below the current fifth place) must NOT place and must leave both lists intact.
      const skipped = rankIn(
        [40000, 35000, 32000, 30000, 25000],
        ['STK', 'M.N', '', 'EVE', 'S.O'],
        10000,
      );
      // A tie with the current fifth place still places (>= semantics), at rank 5.
      const tied = rankIn(
        [40000, 35000, 32000, 30000, 25000],
        ['STK', 'M.N', '', 'EVE', 'S.O'],
        25000,
      );
      return { placed, skipped, tied };
    },
    assert(obs) {
      // Qualifying: rank 3, scores shift down + old fifth dropped, names shift in lockstep, new row name blank.
      assert.equal(obs.placed.row, 3, 'a qualifying score records its insertion rank');
      assert.deepEqual(
        obs.placed.table,
        [40000, 35000, 32000, 30000, 25000],
        'the score inserts at rank 3 and lower entries shift down one, dropping the old fifth',
      );
      assert.deepEqual(
        obs.placed.names,
        ['STK', 'M.N', '', 'EVE', 'S.O'],
        'names shift down in lockstep with scores and the new row blanks its name',
      );
      assert.equal(obs.placed.table.length, 5, 'the table stays exactly five entries');
      assert.equal(obs.placed.names.length, 5, 'the names list stays exactly five entries');
      // Non-qualifying: entry row 0 (did not place) and both lists untouched.
      assert.equal(obs.skipped.row, 0, 'a sub-fifth score does not place (entry row 0)');
      assert.deepEqual(
        obs.skipped.table,
        [40000, 35000, 32000, 30000, 25000],
        'a non-qualifying score leaves the table unchanged',
      );
      assert.deepEqual(
        obs.skipped.names,
        ['STK', 'M.N', '', 'EVE', 'S.O'],
        'a non-qualifying score leaves the names unchanged',
      );
      // Tie with fifth place places at rank 5 (>= semantics, the arcade fall-through).
      assert.equal(obs.tied.row, 5, 'a score tying fifth place still places, at rank 5');
      assert.deepEqual(
        obs.tied.table,
        [40000, 35000, 32000, 30000, 25000],
        'the tie places at rank 5 (an equal score takes the last slot)',
      );
    },
    // Empty the `rank in` proc body: a qualifying score then never ranks in, so entry row stays 0 and the table
    // is untouched → the placement assertions fail. Surgical to this proc (its callers and all other state are
    // left intact).
    // roadmap-evidence: CAB-04 failure  (with rank in neutralized a qualifying score never enters the table)
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'rank in'),
  },
  {
    // CAB-04 (cabinet.high-scores, slice 19): the LIVE best-five compositor — riskiest seam #1. Each table
    // cell is a start_screen clone that re-reads the Stage lists EVERY tick (the credit-digit idiom), so the
    // table tracks a mid-session rank-in with no re-entry. A name cell shows glyph/<letter> for the live
    // letter and HIDES past the name's end (letter_of → "") or on a space (the sheet font has no space glyph);
    // a score cell shows digit/<d> for the live place digit (leading-zero-preserving, matching the HUD row);
    // a rank cell shows rank/<row> (the 1ST..5TH ordinal, PRES-01). This scenario pins the exact costume mapping for a default row AND proves
    // liveness: mutating the lists in place (not replacing the references) and stepping twice re-dresses the
    // cells. Default row 1 is "STK" / 40000 (HIGH_SCORE_NAME_DEFAULTS[0] / HIGH_SCORE_DEFAULTS[0]).
    // roadmap-evidence: CAB-04 success  (the live cells map list values to glyph/digit costumes and track edits)
    key: 'high-score-table-live',
    behavior: 'The live best-five cells render the Stage lists as glyph/digit costumes and re-read them each tick',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(reachScores(vm), 'precondition: the attract cycle reaches the best-five screen');
      step(vm, 3); // let the table cells dress from the default lists
      const roleName = variable('attract-display-role').name; // "attract role"
      const rowName = variable('attract-display-row').name; // "attract row"
      const placeName = variable('attract-display-place').name; // "attract place"
      // A name/score cell is identified by (role, row, place); a rank cell by (role, row).
      const cell = (role, row, place) =>
        cloneReports(vm, 'start_screen', [roleName, rowName, placeName]).find(
          (r) =>
            r.vars[roleName] === role &&
            r.vars[rowName] === row &&
            (place === undefined || r.vars[placeName] === place),
        ) || null;
      const name = (row, place) => cell(8, row, place); // place is the 1-based letter index
      const score = (row, place) => cell(9, row, place); // place 0 = units .. 6 = millions
      const snap = (c) => (c ? { costume: c.costume, visible: c.visible } : null);
      // Default render of row 1: name "STK", score 40000 ("0040000").
      const def = {
        rank1: snap(cell(7, 1)),
        n1: snap(name(1, 1)),
        n2: snap(name(1, 2)),
        n3: snap(name(1, 3)),
        n4: snap(name(1, 4)), // past "STK" -> blank -> hidden
        sUnits: snap(score(1, 0)), // 40000 -> units digit 0
        sTenK: snap(score(1, 4)), // 40000 -> ten-thousands digit 4
      };
      // Liveness: edit the lists IN PLACE, then step so the looping cells re-read and re-dress.
      const names = readVar(vm, 'eco-high-score-names');
      const table = readVar(vm, 'eco-high-score-table');
      names[0] = 'ZZ'; // row 1 name now two letters
      table[0] = 12345; // row 1 score now "0012345"
      step(vm, 2);
      const live = {
        n1: snap(name(1, 1)), // -> Z
        n2: snap(name(1, 2)), // -> Z
        n3: snap(name(1, 3)), // past "ZZ" -> now hidden
        sUnits: snap(score(1, 0)), // 12345 -> units digit 5
      };
      return { st: state(vm), def, live };
    },
    assert(obs) {
      assert.equal(obs.st, 'attract-scores', 'the observation is taken on the best-five screen');
      // Default row 1 render.
      assert.deepEqual(obs.def.rank1, { costume: 'rank/1', visible: true }, 'rank cell shows rank/<row> (1ST)');
      assert.deepEqual(obs.def.n1, { costume: 'glyph/S', visible: true }, 'name letter 1 of "STK" is S');
      assert.deepEqual(obs.def.n2, { costume: 'glyph/T', visible: true }, 'name letter 2 of "STK" is T');
      assert.deepEqual(obs.def.n3, { costume: 'glyph/K', visible: true }, 'name letter 3 of "STK" is K');
      assert.equal(obs.def.n4 && obs.def.n4.visible, false, 'a name cell past the name end is hidden');
      assert.deepEqual(obs.def.sUnits, { costume: 'digit/0', visible: true }, '40000 units digit is 0');
      assert.deepEqual(obs.def.sTenK, { costume: 'digit/4', visible: true }, '40000 ten-thousands digit is 4');
      // Live edits are picked up without re-entering attract-scores.
      assert.deepEqual(obs.live.n1, { costume: 'glyph/Z', visible: true }, 'name letter 1 tracks the live edit (Z)');
      assert.deepEqual(obs.live.n2, { costume: 'glyph/Z', visible: true }, 'name letter 2 tracks the live edit (Z)');
      assert.equal(obs.live.n3 && obs.live.n3.visible, false, 'the now-past-end letter 3 hides live');
      assert.deepEqual(obs.live.sUnits, { costume: 'digit/5', visible: true }, 'the score units digit tracks the live edit (5)');
    },
    // Break the name-cell dispatch (`attract role == 8`): the name clones never match their role branch, so
    // they never switch to a glyph/<letter> costume nor show → the default-name assertions fail.
    // roadmap-evidence: CAB-04 failure  (a broken name-role dispatch leaves the live table's names unrendered)
    negativeMutation: (p) => mutate.changeVarEqualsOperand(p, 'start_screen', 'attract role', 8, 999),
  },
  {
    // CAB-04 (cabinet.high-scores, slice 19): the LIVE table cells do not leak. Entering attract-scores stamps
    // exactly 91 clones — 90 cells (5 rows × (1 rank + 10 name + 7 score), all role 7/8/9) plus the PRES-01
    // header (role 14); they must ALL retire on
    // the transition out — each cell self-deletes when its loop exits (`repeat until not attract-scores` → hide
    // → delete this clone) AND common_stop(clones=True) is the backstop. Without retirement every best-five
    // visit would stack a fresh 90-cell table on the previous one, climbing toward the scratch-vm 300-clone
    // ceiling. We assert retirement DIRECTLY (table-role clone count is 90 on screen, then 0 after the screen
    // advances to demo 2) rather than comparing counts across laps — a cross-lap climb is masked once the 300
    // ceiling caps clone creation, so a flat-count assertion would not bind. Companion to attract-clone-no-leak
    // (which samples the title-state CREDIT/digit/prompt clones, absent here).
    // roadmap-evidence: CAB-04 success  (the ~90 table cells are all retired on the transition out of attract-scores)
    key: 'high-score-clone-no-leak',
    behavior: 'The live best-five table cells are all retired on the transition out of attract-scores',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(reachScores(vm), 'precondition: the attract cycle reaches the best-five screen');
      step(vm, 3); // let the best-five screen stamp its table cells
      // Count the WHOLE start_screen clone pool, not role-filtered: a leak build blows past the 300-clone
      // ceiling and overwrites the leaked clones' `attract role` var, so a role filter reads 0 and misses
      // them. The total census is immune to both — the field is torn down in attract-scores, so the only
      // start_screen clones alive are this screen's 90 table cells and its header.
      const present = cloneCount(vm, 'start_screen');
      // The best-five hold auto-advances to demo 2 (~256 ticks); step until the screen leaves attract-scores.
      let t = 0;
      while (state(vm) === 'attract-scores' && t < 400) {
        step(vm, 1);
        t += 1;
      }
      assert.notEqual(state(vm), 'attract-scores', 'the best-five screen advances out within budget');
      step(vm, 5); // let the transition's director-stop retire the cells
      return { present, stateAfter: state(vm), afterExit: cloneCount(vm, 'start_screen') };
    },
    assert(obs) {
      assert.equal(obs.present, 91, 'the best-five screen stamps exactly 90 table cells (5 × (1 + 10 + 7)) and the header');
      // Demo 2 keeps only the handful of ordinary attract clones (≈6); the table cells are all gone. A
      // leak build carries all 90 cells (capped at the 300 ceiling) past the transition → far above this.
      assert.ok(obs.afterExit < 30, `the table cells are retired on leaving attract-scores (saw ${obs.afterExit})`);
    },
    // Remove every `delete this clone` on start_screen (the per-cell self-delete AND the common_stop backstop)
    // so no table cell is ever retired: the 90 cells survive past the transition out → afterExit stays pinned
    // at the 300 ceiling and the retirement assertion fails. The present==90 half also bites (the leak re-runs
    // the spawn to the ceiling, so present reads 300). Ceiling- and role-independent.
    // roadmap-evidence: CAB-04 failure  (without clone retirement the live table cells survive the transition)
    negativeMutation: (p) => mutate.removeDeleteThisClone(p, 'start_screen'),
  },
  {
    // CAB-04 (cabinet.high-scores, slice 19): the initials-entry INPUT — riskiest seam #5. On the entry screen
    // Up/Down cycle the active letter over the 27-symbol ring (A-Z then space), wrapping both ways (the floored
    // mod: Down from A -> the space at index 26, Up from the space -> A; xevious_main.68k:1736-1744,1773-1781);
    // Space commits the active ring letter onto `name buffer`, advances the cursor and resets the active letter,
    // and the tenth committed character finishes entry — writing the typed name into `high score names` at the
    // rank `rank in` recorded (`entry row`) and returning to the attract cycle (attract=1 -> title). The hats are
    // state-gated to high-score-entry, so they never fight the title selector or the craft. Driven directly into
    // the entry screen (the game-over routing that reaches it in real play arrives in C4). A boot step before the
    // first press is required (keypress-hats-need-boot-step), handled inside enterEntry.
    // roadmap-evidence: CAB-04 success  (Up/Down cycles the ring with both-way wrap; Space commits + advances;
    //   the tenth character finishes and the typed initials land in high score names at the entry rank)
    key: 'high-score-entry-letters',
    behavior: 'Up/Down cycle the entry ring (both-way wrap), Space commits, and the tenth char finishes into the table',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(enterEntry(vm, { row: 3, timer: 1000000 }), 'precondition: the cabinet reaches the entry screen');
      // Seed a known names list so the finished name can be read back at its rank row (rank 3 -> JS index 2).
      const names = readVar(vm, 'eco-high-score-names');
      names.splice(0, names.length, 'STK', 'M.N', 'EVE', 'S.O', 'S.K');

      const char = () => readVar(vm, 'cabinet-entry-char');
      const start = char(); // a fresh cell sits on the blanked SPACE symbol (ring index 26)
      tapKey(vm, 'ArrowUp'); // wrap up: 26 (space) -> 0 (A)
      const wrapUp = char();
      tapKey(vm, 'ArrowDown'); // wrap down: 0 (A) -> 26 (space)
      const wrapDown = char();
      tapKey(vm, 'ArrowUp'); // 26 -> 0 (A)
      tapKey(vm, 'ArrowUp'); // 0 -> 1 (B)
      tapKey(vm, 'ArrowUp'); // 1 -> 2 (C)
      const climbed = char();

      // Type "ABABABABAB". Each cell's active letter is set explicitly, then Space commits it (after a commit
      // the hat resets the active letter to SPACE — the blanked next cell — so selecting per cell is robust to
      // that reset). The tenth commit finishes entry; the finish then runs the terminal GAME OVER hold.
      const typed = 'ABABABABAB';
      for (let i = 0; i < typed.length; i += 1) {
        writeVar(vm, 'cabinet-entry-char', typed[i] === 'B' ? 1 : 0);
        tapKey(vm, ' ');
      }
      // The name lands (list_replace) before the finish transitions out of the entry screen; the cabinet then
      // runs high-score-entry -> game-over (the GAME OVER hold) -> title, raising attract in the terminal hold.
      const landed = readVar(vm, 'eco-high-score-names')[2];
      const reachedTitle = stepUntil(vm, (v) => state(v) === 'title');
      return {
        start,
        wrapUp,
        wrapDown,
        climbed,
        stateAfter: reachedTitle ? 'title' : state(vm),
        landed,
        attract: readVar(vm, 'cabinet-attract'),
      };
    },
    assert(obs) {
      assert.equal(obs.start, 26, 'a fresh cell starts on the blanked SPACE symbol (ring index 26)');
      assert.equal(obs.wrapUp, 0, 'Up from the space wraps to the first letter (A)');
      assert.equal(obs.wrapDown, 26, 'Down from A wraps back to the space (index 26)');
      assert.equal(obs.climbed, 2, 'two further Up presses advance the active letter to C (index 2)');
      assert.equal(obs.landed, 'ABABABABAB', 'the typed initials land in high score names at the entry rank');
      assert.equal(obs.stateAfter, 'title', 'the tenth character finishes entry; after the GAME OVER hold the cabinet is back at the title');
      assert.equal(obs.attract, 1, 'the terminal GAME OVER hold re-raises the attract flag for the cabinet cycle');
    },
    // Pin `name buffer` to "" so no committed letter ever accumulates: the tenth-char finish then writes an empty
    // name and the typed-initials assertion fails (the cursor/cell machinery is untouched, so only the buffer
    // accumulation — the behaviour this scenario proves — breaks).
    // roadmap-evidence: CAB-04 failure  (with name buffer pinned empty the committed initials never accumulate)
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'name buffer', ''),
  },
  {
    // CAB-04 (cabinet.high-scores, slice 19): the FIXED total countdown. `entry timer` is armed once at entry
    // start (the entry-scope reset) and counts down one per frame, never reset by input (countdown_timer_1 is
    // seeded once and decremented unconditionally, xevious_main.68k:1701,1721-1728). When it reaches zero it
    // commits whatever was typed so far and leaves the screen (name_entry_finished :1757-1769). We type a partial
    // name, arm a short countdown and let it expire; the partial must land at the entry rank and the cabinet must
    // return to the title. This is the timeout half of riskiest-seam #5 (the letters scenario is the input half).
    // roadmap-evidence: CAB-04 success  (the fixed countdown expiring commits the partial name and routes to title)
    key: 'high-score-entry-timeout',
    behavior:
      'The fixed entry countdown expiring commits the typed cells AND the letter scrolled but not yet confirmed, then returns to the attract cycle',
    playtestStep: 1,
    async drive(vm) {
      assert.ok(enterEntry(vm, { row: 4 }), 'precondition: the cabinet reaches the entry screen');
      const names = readVar(vm, 'eco-high-score-names');
      names.splice(0, names.length, 'STK', 'M.N', 'EVE', 'S.O', 'S.K');
      // Type a partial "AB" (2 of 10 cells committed with Space); each cell's letter is set explicitly, since a
      // commit resets the active letter to the blanked space for the next cell.
      writeVar(vm, 'cabinet-entry-char', 0);
      tapKey(vm, ' '); // commit A
      writeVar(vm, 'cabinet-entry-char', 1);
      tapKey(vm, ' '); // commit B
      const committed = readVar(vm, 'cabinet-entry-name-buffer');
      // Scroll the third cell to 'C' but do NOT Space-confirm it, then let the fixed countdown expire. The
      // commit-in-flight on finish keeps this scrolled-but-unconfirmed letter (faithful to name_entry_finished,
      // which stops on whatever letter is showing) — so the landed name is "ABC", not "AB".
      writeVar(vm, 'cabinet-entry-char', 2); // 'C', in flight (not Space-committed)
      writeVar(vm, 'cabinet-entry-timer', 2); // arm a short countdown; the entry loop decrements it to zero
      const reachedTitle = stepUntil(vm, (v) => state(v) === 'title');
      return {
        committed,
        stateAfter: reachedTitle ? 'title' : state(vm),
        landed: readVar(vm, 'eco-high-score-names')[3], // rank 4 -> JS index 3
        attract: readVar(vm, 'cabinet-attract'),
      };
    },
    assert(obs) {
      assert.equal(obs.committed, 'AB', 'the Space-committed cells accumulate into the name buffer before the timeout');
      assert.equal(obs.stateAfter, 'title', 'the fixed countdown expiring finishes entry and (via the GAME OVER hold) returns to the title');
      assert.equal(obs.landed, 'ABC', 'the timeout commits the typed cells AND the in-flight scrolled letter into the entry rank');
      assert.equal(obs.attract, 1, 'the terminal GAME OVER hold after the timeout finish re-raises the attract flag');
    },
    // Freeze `change entry timer by` so the countdown never decrements: the timer stays positive, the entry never
    // finishes, and the cabinet never leaves high-score-entry → the return-to-title assertion fails.
    // roadmap-evidence: CAB-04 failure  (without the decrement the fixed countdown never expires and entry hangs)
    negativeMutation: (p) => mutate.freezeVariableChange(p, 'Stage', 'entry timer'),
  },
  {
    // ECO-04 (economy.game-over-routing, slice 19): the end-of-game ROUTING acts on the qualification verdict,
    // and it runs at the DEATH decision (state player-dead, the last craft gone), BEFORE any GAME OVER hold —
    // faithful to the arcade, which calls check_for_high_score the instant the game ends and reaches the
    // game_over hold only after name entry (xevious_main.68k:546, :1671-1672, :1757-1769). A qualifying score
    // (>= fifth place) ranks into the live table and transitions player-dead -> high-score-entry, tagging the
    // entering player/row/score; a sub-fifth score skips entry and runs player-dead -> game-over, whose terminal
    // hold returns to the attract cycle at the title. Driven by firing the `death complete` receiver directly
    // over a seeded table + score with the craft counter drained (the terminal branch), so the routing is tested
    // at its real decision point without a rendered collision.
    // roadmap-evidence: ECO-04 success  (a qualifying score routes player-dead -> high-score-entry and ranks in
    //   at its rank; a sub-fifth score skips entry and returns to the title via the terminal GAME OVER hold)
    key: 'high-score-qualify-enters',
    behavior:
      'A qualifying end-of-game score routes the death decision into the initials screen and ranks in; a sub-fifth score skips entry and returns to the title',
    playtestStep: 6,
    async drive(vm) {
      vm.greenFlag();
      step(vm, 2); // boot to the title and arm the director receivers
      const seed = (id, vals) => {
        const a = readVar(vm, id);
        a.splice(0, a.length, ...vals);
      };
      const route = (score, { twoPlayer = 0, currPlayer = 0, otherScore = 0 } = {}) => {
        seed('eco-high-score-table', [40000, 35000, 30000, 25000, 20000]);
        seed('eco-high-score-names', ['STK', 'M.N', 'EVE', 'S.O', 'S.K']);
        writeVar(vm, 'eco-score', score);
        writeVar(vm, 'other-score', otherScore);
        writeVar(vm, 'cabinet-two-player', twoPlayer);
        writeVar(vm, 'cabinet-curr-player', currPlayer);
        writeVar(vm, 'eco-craft', 0); // no craft left: the death decision is terminal (reaches the route)
        writeVar(vm, 'other-craft', 0); // and the other player is out too, so the 2P alternate path is skipped
        writeVar(vm, 'cabinet-entry-row', 0);
        writeVar(vm, 'cabinet-entry-player', 9); // sentinel: the routing must set it
        writeVar(vm, 'game-director-state', 'player-dead');
        fireBroadcast(vm, 'death complete');
        stepUntil(vm, (v) => state(v) === 'high-score-entry' || state(v) === 'title');
        return {
          state: state(vm),
          qualified: Number(readVar(vm, 'eco-qualified')),
          row: readVar(vm, 'cabinet-entry-row'),
          player: Number(readVar(vm, 'cabinet-entry-player')),
          entryScore: Number(readVar(vm, 'cabinet-entry-score')),
          table: readVar(vm, 'eco-high-score-table').slice(),
        };
      };
      // Qualifying one-player score 32000 (>= 20000 fifth) ranks at rank 3 (beats 30000, below 35000).
      const qualify = route(32000);
      // Sub-fifth one-player score 10000 (< 20000 fifth) does not qualify.
      const skip = route(10000);
      return { qualify, skip };
    },
    assert(obs) {
      assert.equal(obs.qualify.state, 'high-score-entry', 'a qualifying score routes into the initials screen');
      assert.equal(obs.qualify.qualified, 1, 'the qualification verdict is set for a qualifying score');
      assert.equal(obs.qualify.row, 3, 'the qualifying score ranks in at its rank (3)');
      assert.equal(obs.qualify.player, 0, 'the entering player is the current (last-dier) player');
      assert.equal(obs.qualify.entryScore, 32000, 'the entry score is the qualifying player score');
      assert.deepEqual(
        obs.qualify.table,
        [40000, 35000, 32000, 30000, 25000],
        'the qualifying score ranks into the live table before entry',
      );
      assert.equal(obs.skip.state, 'title', 'a sub-fifth score skips entry and returns to the title');
      assert.equal(obs.skip.qualified, 0, 'a sub-fifth score does not qualify');
    },
    // Remove the player-dead -> high-score-entry edge: the qualifying score's transition is then a no-op, so the
    // cabinet never reaches the entry screen → the routing assertion fails. The sub-fifth -> game-over -> title
    // path is untouched, so only the routing-to-entry behaviour breaks.
    // roadmap-evidence: ECO-04 failure  (without the routing edge a qualifying score cannot reach the entry screen)
    negativeMutation: (p) => mutate.removeAllowedTransition(p, 'player-dead -> high-score-entry'),
  },
  {
    // ECO-04 (economy.game-over-routing, slice 19): two-player SEQUENTIAL entry. At a two-player both-out (state
    // player-dead, both craft counters drained) the routing runs at the DEATH decision and enters the CURRENT
    // player first (rank in, arm `entry recheck`) via player-dead -> high-score-entry; on finish
    // `_high_score_finish` consumes the flag, re-checks the OTHER player against the now-shifted fifth place, and
    // re-arms the entry screen for them via a high-score-entry -> high-score-entry self-transition (so the
    // PLAYER-2 tag re-reads `entry player`). The port batches the arcade's per-player-at-own-game-over entries at
    // this single both-out point (recorded divergence). Both finishes here are the tenth-char Space finish; each
    // typed name lands at its own rank. A boot step arms the key hats (keypress-hats-need-boot-step); the
    // self-transition re-arms.
    // roadmap-evidence: ECO-04 success  (both qualifying players enter sequentially — current first, then the
    //   other after a self-transition re-arm — and each typed name lands at its correct rank)
    key: 'high-score-two-player-both',
    behavior:
      'A two-player both-out where both scores qualify runs two sequential initials entries (current then other), each name landing at its own rank',
    playtestStep: 6,
    async drive(vm) {
      vm.greenFlag();
      step(vm, 2);
      const seed = (id, vals) => {
        const a = readVar(vm, id);
        a.splice(0, a.length, ...vals);
      };
      seed('eco-high-score-table', [40000, 35000, 30000, 25000, 20000]);
      seed('eco-high-score-names', ['STK', 'M.N', 'EVE', 'S.O', 'S.K']);
      writeVar(vm, 'eco-score', 32000); // current player (curr player 0) — ranks at 3
      writeVar(vm, 'other-score', 28000); // other player (player 1) — ranks at 5 after the first insert
      writeVar(vm, 'cabinet-two-player', 1);
      writeVar(vm, 'cabinet-curr-player', 0);
      writeVar(vm, 'eco-craft', 0); // both craft counters drained: terminal, both-out
      writeVar(vm, 'other-craft', 0); // → the 2P alternate (hand-off) path is skipped, reaching the route
      writeVar(vm, 'game-director-state', 'player-dead');
      fireBroadcast(vm, 'death complete');
      stepUntil(vm, (v) => state(v) === 'high-score-entry' || state(v) === 'title');
      const firstState = state(vm);
      const firstPlayer = Number(readVar(vm, 'cabinet-entry-player'));
      const firstRow = readVar(vm, 'cabinet-entry-row');

      // Finish player 1's entry: ten 'A's. Each cell's active letter is set explicitly (a commit resets the
      // active letter to the blanked space), then Space commits it. The tenth commit finishes and the recheck
      // re-arms the screen for player 2.
      for (let i = 0; i < 10; i += 1) {
        writeVar(vm, 'cabinet-entry-char', 0); // 'A'
        tapKey(vm, ' ');
      }
      stepUntil(vm, (v) => state(v) === 'high-score-entry' || state(v) === 'title'); // settle into the second entry
      const midState = state(vm);
      const midPlayer = Number(readVar(vm, 'cabinet-entry-player'));
      const midRow = readVar(vm, 'cabinet-entry-row');

      // Finish player 2's entry: ten 'B's (set 'B' per cell, Space commit). The tenth commit finishes with the
      // recheck already consumed, so the cabinet runs the terminal GAME OVER hold back to the title.
      for (let i = 0; i < 10; i += 1) {
        writeVar(vm, 'cabinet-entry-char', 1); // 'B'
        tapKey(vm, ' ');
      }
      stepUntil(vm, (v) => state(v) === 'title');
      return {
        firstState,
        firstPlayer,
        firstRow,
        midState,
        midPlayer,
        midRow,
        finalState: state(vm),
        names: readVar(vm, 'eco-high-score-names').slice(),
        attract: Number(readVar(vm, 'cabinet-attract')),
      };
    },
    assert(obs) {
      assert.equal(obs.firstState, 'high-score-entry', 'the current player enters first');
      assert.equal(obs.firstPlayer, 0, 'the first entrant is the current player (player 0)');
      assert.equal(obs.firstRow, 3, 'the current player ranks in at rank 3');
      assert.equal(obs.midState, 'high-score-entry', 'finishing the first entry re-arms the screen for the other player');
      assert.equal(obs.midPlayer, 1, 'the second entrant is the other player (player 1)');
      assert.equal(obs.midRow, 5, 'the other player ranks in at rank 5 against the now-shifted fifth place');
      assert.equal(obs.names[2], 'AAAAAAAAAA', 'the first player name lands at its rank (3 -> index 2)');
      assert.equal(obs.names[4], 'BBBBBBBBBB', 'the second player name lands at its rank (5 -> index 4)');
      assert.equal(obs.finalState, 'title', 'both entries done, the cabinet returns to the attract cycle at the title');
      assert.equal(obs.attract, 1, 'the final finish re-raises the attract flag');
    },
    // Remove the high-score-entry -> high-score-entry self-edge: after the first player finishes, the re-arm
    // transition is a no-op, so the other player never enters and the second name never lands → the sequential
    // assertions fail. The first entry (via game-over -> high-score-entry) is untouched.
    // roadmap-evidence: ECO-04 failure  (without the self-edge the second qualifying player's re-arm cannot fire)
    negativeMutation: (p) => mutate.removeAllowedTransition(p, 'high-score-entry -> high-score-entry'),
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
      // The terminal death runs the best-five route at the death decision; a non-qualifying score (the fresh
      // craft scores nothing) records the game-over outcome and transitions player-dead -> game-over, whose
      // terminal hold returns to the title. stepUntil rides through the extra routing transition and the hold.
      const reachedTitle = stepUntil(vm, (v) => state(v) === 'title');
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
      // Force every spawnable flying type to the SHOOTING Toroid (type 0x0B = 11) so a shooter spawns and
      // fires almost immediately (~2 pumps) instead of waiting for the wave scheduler to pick a shooter —
      // that spawn-wait is what flaked red on a slow/loaded CI runner, where each wall-clock-bounded pump
      // (see harness.js header) advances fewer internal ticks. The window must stay SHORT and cannot be
      // widened to compensate: past ~60 pumps the game scrolls into the Andor Genesis boss, whose ports
      // fire through the same shared alloc signal and would break the negative's isolation. A slow runner
      // only helps both margins — the forced Toroid still fires early while the boss onset moves later.
      const typeTable = readVar(vm, 'flying-type-table');
      for (let i = 0; i < typeTable.length; i += 1) typeTable[i] = 11;
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
      'A new-life death advances the area when the row the arcade reads after its 44 ticks of post-death scrolling is in the near-end window [0x0E,0x43], else restarts it — and area 16 in-window wraps to 7',
    playtestStep: 5,
    async drive(vm) {
      // The live death->respawn sequence completes within a single headless pump, so it cannot be
      // paused to inject a death position. Instead drive `area_reset` in isolation: green-flag to a
      // settled state, inject the new-life scope + a chosen area number + a chosen frozen death-tick
      // `area progress`, fire `director reset`, and read the resulting area number — exactly the
      // checkpoint decision. The checkpoint projects 44 ticks (1408 progress) ahead, so the window's
      // edges in death-tick progress are: projected row 67 first at 50080, row 15 at 63616.
      const trial = (progress, area) => {
        vm.greenFlag();
        step(vm, 2);
        writeVar(vm, 'game-director-reset-scope', 'new-life');
        writeVar(vm, 'area-number', area);
        writeVar(vm, 'area-progress', progress);
        fireBroadcast(vm, 'director reset');
        step(vm, 1);
        return readVar(vm, 'area-number');
      };
      return {
        high: trial(50080, 5), // projected row 67 (0x43) — window high edge
        mid: trial(57984, 5), // projected row 41
        low: trial(63616, 5), // projected row 15 — the last tick before the projection completes the area
        aboveWindow: trial(50048, 5), // projected row 68, just above 0x43
        top: trial(0, 5), // projected row 7, below the window
        wrap16: trial(57984, 16), // in-window death in area 16
      };
    },
    assert(obs) {
      assert.equal(obs.high, 6, 'a death projecting to row 67 (window high edge) advances the area');
      assert.equal(obs.mid, 6, 'a death projecting to row 41 advances the area');
      assert.equal(obs.low, 6, 'a death projecting to row 15 advances the area');
      assert.equal(obs.aboveWindow, 5, 'a death projecting to row 68 restarts (holds the area)');
      assert.equal(obs.top, 5, 'a death at the area top restarts (holds the area)');
      assert.equal(obs.wrap16, 7, 'an in-window death in area 16 wraps to area 7');
    },
    // Raise the window's lower bound (row > 13) out of reach, so no death is ever near-end and the
    // in-window advances never happen → the advance assertions fail.
    negativeMutation: (p) => mutate.raiseGreaterThreshold(p, 'Stage', 13, 999),
  },
  {
    // AREA-01 (slice 20): the projection's area-change edge. The arcade keeps scrolling for 88 frames
    // after a death with the area completion live (xevious_main.68k 507-521; xevious_sub.68k 696-730), so a
    // death in the last 37-44 ticks of an area completes it during the explosion and THEN reads row 0x0E —
    // skipping the next area too; a death in the 8-tick carry window at the start of an area (row 0x0E,
    // progress -480..-256) reads row 9 after the scroll and restarts.
    // roadmap-evidence: AREA-01 success  (a death 37-44 ticks before the end of an area skips the next area,
    //   one 36 ticks before advances once, and a carry-window death restarts — live, through area_reset)
    key: 'checkpoint-projected-area-change',
    behavior:
      'A death in the last 37-44 ticks of an area completes it during the explosion and skips the next area too, while a death in the carry window at the start of an area restarts it',
    playtestStep: 5,
    async drive(vm) {
      const trial = (progress, area) => {
        vm.greenFlag();
        step(vm, 2);
        writeVar(vm, 'game-director-reset-scope', 'new-life');
        writeVar(vm, 'area-number', area);
        writeVar(vm, 'area-progress', progress);
        fireBroadcast(vm, 'director reset');
        step(vm, 1);
        return readVar(vm, 'area-number');
      };
      return {
        skipFirst: trial(63648, 5), // projects to 65056: completes, carries to -480 (row 0x0E) -> advances again
        skipLast: trial(63872, 5), // projects to 65280 -> -256, still row 0x0E
        afterSkip: trial(63904, 5), // projects to 65312 -> -224, row 0x0D: completion only
        skip16: trial(63648, 16), // completes 16 -> 7, then the band advances 7 -> 8
        carryStart: trial(-480, 5), // carry window: projects to 928, row 9
        carryEnd: trial(-256, 5),
      };
    },
    assert(obs) {
      assert.equal(obs.skipFirst, 7, 'a death 44 ticks before the end skips the next area');
      assert.equal(obs.skipLast, 7, 'a death 37 ticks before the end skips the next area');
      assert.equal(obs.afterSkip, 6, 'a death 36 ticks before the end advances one area');
      assert.equal(obs.skip16, 8, 'the skip wraps 16 -> 7 and then advances to 8');
      assert.equal(obs.carryStart, 5, 'a death at the start of the carry window restarts the area');
      assert.equal(obs.carryEnd, 5, 'a death at the end of the carry window restarts the area');
    },
    // roadmap-evidence: AREA-01 failure  (a checkpoint that reads the frozen death-tick position — no
    //   projection — misses the skip and advances on a carry-window death)
    negativeMutation: (p) => mutate.changeAddLiteral(p, 'Stage', 1408, 0),
  },
  {
    // AREA-01 (slice 20): completing an area carries the scroll clock, as the arcade does (its
    // `sub_fn_3__handle_next_area`, xevious_sub.68k 696-730, never resets the counter): `area progress`
    // drops by 65536 to the same counter value and keeps counting, rather than re-topping to 0. Live:
    // seed area progress two ticks short of completion during play and let the walk run. The walk's
    // `tick` advances once per walk tick, right after `advance area`, so `area progress` must equal
    // seed + 32*ticks - 65536 exactly (one carry; a re-top would leave 32*(ticks after completion)).
    // roadmap-evidence: AREA-01 success  (the clock carries across a live area completion and the 8 carry
    //   ticks at row 0x0E never complete the area a second time)
    key: 'area-clock-carry',
    behavior:
      'Completing an area carries the scroll clock (progress drops by 65536 and keeps counting) instead of re-topping it, and the carry window at row 0x0E never completes the area twice',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const seed = 64992; // two ticks short of 65056, area 1's completion
      writeVar(vm, 'area-number', 3);
      writeVar(vm, 'area-progress', seed);
      const tick0 = readVar(vm, 'tick');
      let ticks = 0;
      for (let i = 0; i < 40 && ticks < 12; i += 1) {
        step(vm, 1);
        ticks = readVar(vm, 'tick') - tick0;
      }
      return {
        ticks,
        progress: readVar(vm, 'area-progress'),
        area: readVar(vm, 'area-number'),
        state: readVar(vm, 'game-director-state'),
      };
    },
    assert(obs) {
      assert.equal(obs.state, 'playing', 'precondition: still playing (no death reset the clock)');
      assert.ok(obs.ticks >= 12, `the walk ran past the 8-tick carry window (ran ${obs.ticks})`);
      assert.ok(obs.ticks < 2000, 'the walk stopped before the next area could complete');
      assert.equal(obs.area, 4, 'the area completed exactly once');
      assert.equal(
        obs.progress,
        64992 + 32 * obs.ticks - 65536,
        'area progress carried by 65536 across the completion and kept counting',
      );
    },
    // Undo the carry (change by -65536 -> by 0): progress keeps climbing past completion, still at row
    // 0x0E for 7 more ticks with progress > 0, so the area completes again on each of them.
    negativeMutation: (p) => mutate.changeVariableChangeBy(p, 'Stage', 'area progress', -65536, 0),
  },
  {
    // AREA-01 (slice 20): the terrain phase. tools/terrain_render.py derives from the reference renderer
    // that map row R's top edge sits 8R - C/32 + phase lines below the playfield top (xevious_sub.68k
    // 234-245, 272; amiga.68k 136, 1372) and a ground sprite's centre at slot x/32 - 24 (amiga.68k
    // 1651-1698, 1865), so an object the schedule fires at row S rides with its centre on the top edge of
    // map row S - 2 — the cell Namco's own clearings are drawn round (pinned by tests/test_terrain_render.py).
    // Live: seed the clock two ticks above area 1's first ground records (row 214: a Barra in slot 2 and a
    // Zolbak in slot 3) with the schedule cursor on them, let the walk fire and scroll them, and check every
    // observation lands each object exactly on its derived map line — the same constants the terrain draws by.
    // roadmap-evidence: AREA-01 success  (live schedule-fired ground objects ride on map row S - 2, to the
    //   line, at every position observed as they cross the field)
    key: 'ground-object-map-row-phase',
    behavior:
      'A ground object the area schedule fires at row S rides with its centre on the top edge of map row S - 2 all the way down the field, the landmark the terrain draws there',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const S = 214;
      const rows = readVar(vm, 'area-schedule-trigger-row').map(Number);
      const gslot = readVar(vm, 'area-schedule-ground-slot').map(Number);
      const first = Number(readVar(vm, 'area-schedule-start')[0]); // 1-based, area 1
      const last = Number(readVar(vm, 'area-schedule-end')[0]);
      let cursor = 0;
      for (let i = first; i <= last; i += 1) {
        if (rows[i - 1] === S) {
          cursor = i;
          break;
        }
      }
      assert.ok(cursor > 0, 'precondition: area 1 has records at row 214');
      assert.deepEqual(
        [cursor, cursor + 1].map((i) => gslot[i - 1]),
        [2, 3],
        'precondition: the row-214 records stamp slots 2 and 3',
      );
      // C = 256*(S+1) + 32: two ticks before row S begins (row S fires on its first tick, C = 256*S + 224).
      const C0 = 256 * (S + 1) + 32;
      writeVar(vm, 'area-progress', (((constants.area_counter_init - C0) % 65536) + 65536) % 65536);
      writeVar(vm, 'area-schedule-cursor', cursor);
      // One headless step runs the live walk for a wall-clock-dependent number of ticks, so tick it by hand:
      // freeze the walk and call its two clock procs in the walk's own order, one tick at a time, so every
      // tick of the crossing is observed (48 ticks: six rows, well short of the next area-1 ground record).
      writeVar(vm, 'game-director-state', 'frozen');
      const samples = [];
      for (let t = 0; t < 48; t += 1) {
        callProc(vm, 'Stage', 'advance area');
        step(vm, 2);
        callProc(vm, 'Stage', 'advance slots');
        step(vm, 2);
        if (Number(readVar(vm, 'area-schedule-cursor')) <= cursor + 1) continue; // not fired yet
        const C = (((constants.area_counter_init - Number(readVar(vm, 'area-progress'))) % 65536) + 65536) % 65536;
        const types = readVar(vm, 'slot-type').map(Number);
        const xs = readVar(vm, 'slot-x').map(Number);
        // Scratch ground slot GROUND_SLOTS[0] + k = 1 + k is JS index k.
        for (const slot of [2, 3]) samples.push({ slot, type: types[slot], x: xs[slot], C });
      }
      return { samples };
    },
    assert(obs) {
      const line = (v) => ((v % 2048) + 2048) % 2048;
      const S = 214;
      for (const [slot, type] of [
        [2, 0x1e],
        [3, 0x1f],
      ]) {
        const seen = obs.samples.filter((s) => s.slot === slot);
        assert.equal(seen.length, 47, `slot ${slot}: row 214 fired on the second tick and was watched for 47`);
        assert.ok(seen.every((s) => s.type === type), `slot ${slot}: holds its row-214 object throughout`);
        const xs = new Set(seen.map((s) => s.x));
        assert.equal(xs.size, seen.length, `slot ${slot}: the object scrolled every tick`);
        for (const s of seen) {
          const centre = s.x / constants.counter_units_per_line + constants.ground_centre_line_bias;
          const rowTop =
            8 * (S + constants.ground_object_row_offset) -
            s.C / constants.counter_units_per_line +
            constants.terrain_row_phase_lines;
          assert.equal(line(centre), line(rowTop), `slot ${slot} at slot x ${s.x}, counter ${s.C}: on map row S - 2`);
        }
      }
    },
    // roadmap-evidence: AREA-01 failure  (ground seeders that start an object one tick down the field put
    //   it a line off its landmark)
    negativeMutation: (p) => mutate.changeListReplaceLiteral(p, 'Stage', 'slot x', 0, 32),
  },
  {
    // AREA-01 (slice 20): what the two terrain strips show is a pure function of the clock and two map columns
    // (tools/terrain_render.terrain_state, checked against an independent model of the arcade's 64-row plane by
    // tests/test_terrain_render.py). The Stage's `update terrain` proc computes it in blocks. Here the built proc
    // is driven through the model's own samples — every tick a strip's costume, x, shown flag or draw order
    // changes and the tick before it, over a whole area after a re-top and one entered from the area before,
    // plus the completion tick — and must reproduce every output exactly.
    // roadmap-evidence: AREA-01 success  (the built terrain proc shows each band, the filler and the restart band
    //   with the model's column, position and visibility at every transition of an area's clock)
    key: 'terrain-state-matches-model',
    behavior:
      "The terrain strips' band, sideways position, height and visibility follow the area clock exactly as the model of the arcade's background plane does",
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      writeVar(vm, 'game-director-state', 'frozen');
      const fields = ['costume', 'x', 'y', 'shown'];
      const results = [];
      for (const s of constants.terrain_state_samples) {
        writeVar(vm, 'area-progress', s.progress);
        writeVar(vm, 'area-terrain-column', s.column);
        writeVar(vm, 'area-previous-terrain-column', s.previous);
        callProc(vm, 'Stage', 'update terrain');
        step(vm, 1);
        const got = { even_behind: Number(readVar(vm, 'terrain-even-behind')) };
        for (const parity of ['even', 'odd']) {
          got[parity] = {};
          for (const f of fields) {
            const v = readVar(vm, `terrain-${parity}-${f}`);
            got[parity][f] = f === 'costume' ? String(v) : Number(v);
          }
        }
        results.push({ sample: s, got });
      }
      return { results };
    },
    assert(obs) {
      assert.ok(obs.results.length >= 40, `every model sample was driven (${obs.results.length})`);
      const kinds = new Set(obs.results.map((r) => `${r.sample.even.costume}|${r.sample.odd.costume}`));
      for (const pair of ['terrain filler|terrain band 3 restart', 'terrain band 0|terrain band 3', 'terrain band 2|terrain band 1']) {
        assert.ok(kinds.has(pair), `the samples include ${pair}`);
      }
      for (const { sample: s, got } of obs.results) {
        const at = `progress ${s.progress}, column ${s.column}, previous ${s.previous}`;
        for (const parity of ['even', 'odd']) {
          assert.deepEqual(got[parity], s[parity], `${parity} strip at ${at}`);
        }
        assert.equal(got.even_behind, s.even_behind, `draw order at ${at}`);
      }
    },
    // roadmap-evidence: AREA-01 failure  (a terrain proc that never puts the forest filler in band 0's place
    //   after a re-top shows band 0 at a column that does not exist)
    negativeMutation: (p) => mutate.changeVarEqualsOperand(p, 'Stage', 'terrain band column', -1, '__never__'),
  },
  {
    // AREA-01 (slice 20): the walk keeps the terrain state current. `update terrain` runs in every walk tick
    // after the clock moves and at the end of every re-top; a re-top clears the previous column (the arcade
    // fills the plane with forest), and an area's completion hands the outgoing column to the band still on
    // screen. Live: a re-top leaves no previous column; live play moves the strips, and recomputing them from
    // the clock afterwards changes nothing (the walk left them current); a seeded completion from area 1 sets
    // the previous column to area 1's, which the band-0 strip then shows while the odd strip takes area 2's.
    key: 'terrain-state-follows-clock',
    behavior:
      "The terrain strips move with the area clock during play, restart from forest at a re-top, and keep the old area's column for its last rows after an area change",
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const snap = () =>
        ['even', 'odd'].flatMap((parity) =>
          ['costume', 'x', 'y', 'shown'].map((f) => String(readVar(vm, `terrain-${parity}-${f}`))),
        );
      const retop = {
        area: Number(readVar(vm, 'area-number')),
        previous: Number(readVar(vm, 'area-previous-terrain-column')),
        evenCostume: String(readVar(vm, 'terrain-even-costume')),
      };
      const before = snap();
      const progress0 = Number(readVar(vm, 'area-progress'));
      step(vm, 1);
      const live = { progressMoved: Number(readVar(vm, 'area-progress')) !== progress0, state: snap() };
      writeVar(vm, 'game-director-state', 'frozen');
      callProc(vm, 'Stage', 'update terrain');
      step(vm, 1);
      const recomputed = snap();
      // Area 1's completion tick: row 0x0E with progress > 0 (AREA_COMPLETE_PROGRESS is the first such tick).
      const area1Column = Number(readVar(vm, 'area-terrain-column'));
      writeVar(vm, 'area-progress', 65024);
      callProc(vm, 'Stage', 'advance area');
      step(vm, 2);
      callProc(vm, 'Stage', 'update terrain');
      step(vm, 1);
      const completion = {
        area: Number(readVar(vm, 'area-number')),
        progress: Number(readVar(vm, 'area-progress')),
        column: Number(readVar(vm, 'area-terrain-column')),
        previous: Number(readVar(vm, 'area-previous-terrain-column')),
        even: { costume: String(readVar(vm, 'terrain-even-costume')), x: Number(readVar(vm, 'terrain-even-x')) },
        odd: { costume: String(readVar(vm, 'terrain-odd-costume')), x: Number(readVar(vm, 'terrain-odd-x')) },
      };
      return { retop, before, live, recomputed, area1Column, completion };
    },
    assert(obs) {
      assert.equal(obs.retop.area, 1, 'precondition: the game starts in area 1');
      assert.equal(obs.retop.previous, -1, 'a re-top leaves no previous column');
      assert.equal(obs.retop.evenCostume, 'terrain filler', 'so band 0 is the forest filler');
      assert.equal(obs.live.progressMoved, true, 'precondition: the clock ran during the live step');
      assert.notDeepEqual(obs.live.state, obs.before, 'the strips moved with the clock during play');
      assert.deepEqual(obs.recomputed, obs.live.state, 'the walk left the strips current with the clock');
      const c = obs.completion;
      assert.equal(c.area, 2, 'the seeded completion advanced to area 2');
      assert.equal(c.progress, 65056 - 65536, 'the clock carried across the completion');
      assert.equal(c.previous, obs.area1Column, "the previous column is area 1's");
      assert.notEqual(c.column, obs.area1Column, "precondition: area 2's column differs from area 1's");
      assert.equal(c.even.costume, 'terrain band 0', "area 1's last rows show as band 0");
      assert.equal(c.even.x, 10 * obs.area1Column - 500, "band 0 keeps area 1's column");
      assert.equal(c.odd.x, 10 * c.column - 500, "the odd strip takes area 2's column");
    },
    // Remove the walk's terrain update (and the re-top's): the strips never leave their defaults.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update terrain'),
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
      // PRES-01: sampled from row 4, the first row inside the window (rows 0-3 are hidden); 4 rows = 1024
      // units = 8 whole buckets, so bucket k still draws frame k.
      const UNITS = 128;
      const FRAMES = 8;
      const BASE = constants.render_view_first_row * constants.slot_units_per_cell;
      const samples = [];
      for (let k = 0; k < FRAMES; k += 1) {
        put('slot-x', BASE + UNITS * k + 32); // mid-bucket, clear of the 128-unit boundary
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
      'The shot-vs-air hit box is the reference 32-px lateral band (enemy minus shot in [-16, 15] px): a controlled shot on the Toroid column, one column either side, or two columns on the low-delta side scores; two columns on the other side, or three off, does not',
    playtestStep: 6,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      // Park the Toroid 8 columns from the craft so the real tapped shot (craft column) can never reach
      // it — only the CONTROLLED shot we seed into a real detector slot (37) can score it. `eResult`
      // maps offset -> score delta; the enemy is re-parked before each probe (a scoring hit frees it).
      // One column is 256 slot units = 8 px, so a shot dCol columns over sits at lateral delta
      // enemy - shot = -8*dCol px: +2 columns is -16 (the band's low edge, a hit), -2 is +16 (past 15).
      // The Toroid steers sideways toward the craft while a step runs several ticks, and a shot that misses
      // on the first tick stays in depth range for a tick or two — so a MISS probe must put the Toroid on the
      // side where that drift carries it AWAY from the shot. `side` -1 parks it below the craft column
      // (drift raises enemy - shot), +1 above (drift lowers it); hits land on the first tick either way.
      const eResult = (dCol, side = -1) => {
        const pr = readVar(vm, 'player-row'),
          pc = readVar(vm, 'player-col');
        const eRow = pr - 6,
          eCol = pc + 8 * side;
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
      return {
        onCol: eResult(0),
        onePlus: eResult(1),
        oneMinus: eResult(-1),
        twoPlus: eResult(2),
        twoMinus: eResult(-2),
        threePlus: eResult(3, 1), // delta -24: parked above the craft so the drift deepens the miss
        award: readVar(vm, 'eco-value-table')[2],
      };
    },
    assert(obs) {
      assert.equal(obs.onCol, obs.award, 'a shot on the Toroid column scores');
      assert.equal(obs.onePlus, obs.award, 'a shot one column over (delta -8 px) scores');
      assert.equal(obs.oneMinus, obs.award, 'a shot one column the other way (delta +8 px) scores');
      assert.equal(obs.twoPlus, obs.award, 'a shot at delta -16 px (the low edge) scores');
      assert.equal(obs.twoMinus, 0, 'a shot at delta +16 px (one past the high edge 15) does NOT score');
      assert.equal(obs.threePlus, 0, 'a shot at delta -24 px does NOT score');
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
      'The bomb-vs-ground hit box is the reference shadow window (lateral [-10, 9] px, depth [-5, 4] two-px units — a 20 x 20 px box): an object under the target scores, one at the window edge scores, one past it on EITHER axis does not',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const LAT = 32; // SLOT_UNITS_PER_LATERAL_SHADOW: one lateral shadow unit is one arcade px (32 slot units)
      const DEP = 64; // SLOT_UNITS_PER_DEPTH_SHADOW: one depth shadow unit is two arcade px (64 slot units)
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
      // Target on shadow-aligned positions so each probe's shadow delta is exact: slot y on a whole px
      // (4096 / 32) and slot x on a whole depth unit ((5120 + 256) / 64 = 84). The detector floors each
      // position to its shadow byte, then tests lateral delta = lat(obj_y)-lat(target_y) in [-10, 9] and
      // depth delta = dep(obj_x)-dep(target_x) in [-5, 4]. Each scoring probe marks the object HIT, so
      // every probe re-seeds the object ACTIVE first.
      const tX = 5120,
        tY = 4096;
      const probe = (dLat, dDep) => {
        put('slot-type', 15, 30);
        put('slot-state', 15, 1);
        put('slot-pts', 15, 6);
        put('slot-x', 15, tX + dDep * DEP);
        put('slot-y', 15, tY + dLat * LAT);
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
        latHi: probe(9, 0),
        latHiOut: probe(10, 0),
        latLo: probe(-10, 0),
        latLoOut: probe(-11, 0),
        depHi: probe(0, 4),
        depHiOut: probe(0, 5),
        depLo: probe(0, -5),
        depLoOut: probe(0, -6),
        // Far off on one axis while dead-on the other: a dropped bound (the reporter-steal bug)
        // would make one axis always-hit, so these MUST miss.
        farLat: probe(-40, 0),
        farDep: probe(0, -40),
      };
    },
    assert(obs) {
      assert.equal(obs.center, obs.award, 'dead-on the target scores');
      assert.equal(obs.latHi, obs.award, 'the lateral high edge (+9 px) scores');
      assert.equal(obs.latLo, obs.award, 'the lateral low edge (-10 px) scores');
      assert.equal(obs.depHi, obs.award, 'the depth high edge (+4) scores');
      assert.equal(obs.depLo, obs.award, 'the depth low edge (-5) scores');
      assert.equal(obs.latHiOut, 0, 'one past the lateral high edge (+10 px) does NOT score');
      assert.equal(obs.latLoOut, 0, 'one past the lateral low edge (-11 px) does NOT score');
      assert.equal(obs.depHiOut, 0, 'one past the depth high edge (+5) does NOT score');
      assert.equal(obs.depLoOut, 0, 'one past the depth low edge (-6) does NOT score');
      assert.equal(obs.farLat, 0, 'far off laterally does NOT score (both axes bind)');
      assert.equal(obs.farDep, 0, 'far off in depth does NOT score (both axes bind)');
    },
    // Widen the lateral high bound (`> 9`, shared only by the ground detector and the flag's box) so a
    // probe one past the edge now scores → the latHiOut miss assertion fails, proving the bound binds.
    negativeMutation: (p) => mutate.raiseGreaterThreshold(p, 'Stage', '9', '40'),
  },
  {
    key: 'pres01-bomb-between-pair-hits-both',
    // roadmap-evidence: PRES-01 success  (a bomb dropped midway between two ground objects 16 px apart
    //   sideways destroys both — the 20-px reference box on whole-pixel lateral shadow units)
    behavior:
      'PRES-01: a bomb that lands midway between two ground objects 16 px apart sideways destroys BOTH and scores both values, as in the arcade (each is 8 px off the target, inside the [-10, 9] px lateral band)',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      const tX = 5120,
        tY = 4096;
      // Two Barras at the target depth, 8 px (256 slot units) either side of the target laterally.
      for (const [slot, dy] of [
        [14, -256],
        [15, 256],
      ]) {
        put('slot-type', slot, 30);
        put('slot-state', slot, 1);
        put('slot-pts', slot, 6);
        put('slot-x', slot, tX);
        put('slot-y', slot, tY + dy);
      }
      put('slot-x', 32, tX);
      put('slot-y', 32, tY);
      const award = readVar(vm, 'eco-value-table')[5];
      const s0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      return {
        award,
        delta: readVar(vm, 'eco-score') - s0,
        leftState: readVar(vm, 'slot-state')[14],
        rightState: readVar(vm, 'slot-state')[15],
      };
    },
    assert(obs) {
      assert.equal(obs.delta, 2 * obs.award, 'both objects of the pair score');
      assert.equal(obs.leftState, 2, 'the left object of the pair is struck');
      assert.equal(obs.rightState, 2, 'the right object of the pair is struck');
    },
    // Put the lateral shadow back on half-pixel units (`/ 32` → `/ 16`, the pre-PRES-01 misread): each
    // object is then 16 shadow units off, past the band, and the bomb misses both → the pair assertions fail.
    negativeMutation: (p) => mutate.changeDivideLiteral(p, 'Stage', 32, 16),
  },
  {
    key: 'pres01-bomb-off-pair-misses',
    // roadmap-evidence: PRES-01 failure  (a bomb midway between two ground objects 22 px apart is 11 px from
    //   each — past both lateral edges — and destroys neither: the box does not over-reach)
    behavior:
      'PRES-01: a bomb that lands midway between two ground objects 22 px apart sideways destroys NEITHER (each is 11 px off the target, past the [-10, 9] px lateral band) — the wider box does not over-reach',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      const tX = 5120,
        tY = 4096;
      for (const [slot, dy] of [
        [14, -352],
        [15, 352],
      ]) {
        put('slot-type', slot, 30);
        put('slot-state', slot, 1);
        put('slot-pts', slot, 6);
        put('slot-x', slot, tX);
        put('slot-y', slot, tY + dy);
      }
      put('slot-x', 32, tX);
      put('slot-y', 32, tY);
      const s0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      return {
        delta: readVar(vm, 'eco-score') - s0,
        leftState: readVar(vm, 'slot-state')[14],
        rightState: readVar(vm, 'slot-state')[15],
      };
    },
    assert(obs) {
      assert.equal(obs.delta, 0, 'neither object of the wide pair scores');
      assert.equal(obs.leftState, 1, 'the left object stays active');
      assert.equal(obs.rightState, 1, 'the right object stays active');
    },
    // Widen the lateral high bound (`> 9` → `> 40`): the +11 px object now scores → the miss assertions fail.
    negativeMutation: (p) => mutate.raiseGreaterThreshold(p, 'Stage', '9', '40'),
  },
  {
    key: 'pres01-crosshair-follows-exact-craft',
    // roadmap-evidence: PRES-01 success  (the crosshair follows the craft's exact position, not its rounded
    //   8-px cell, so it moves smoothly with the ship instead of jumping cell to cell)
    behavior:
      "PRES-01: the bomb crosshair follows the craft's EXACT position — a craft placed between cells puts the sight 96 px ahead of that exact spot, not of the rounded 8-px cell — so the sight glides with the ship instead of jumping",
    playtestStep: 3,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const craft = vm.runtime.getSpriteTargetByName('solvalou');
      // A position deliberately between cells on both axes (render map x = 150 - 10*col, y = 210 - 10*row):
      // col = (150 - 13.75) / 10 = 13.625, row = (210 + 41.25) / 10 = 25.125.
      craft.setXY(13.75, -41.25);
      step(vm, 2);
      return {
        craftX: craft.x,
        craftY: craft.y,
        slotX: readVar(vm, 'player-slot-x'),
        slotY: readVar(vm, 'player-slot-y'),
        row: readVar(vm, 'player-row'),
        crosshairX: readVar(vm, 'slot-x')[34], // Scratch slot 35 -> JS index 34
        crosshairY: readVar(vm, 'slot-y')[34],
      };
    },
    assert(obs) {
      assert.equal(obs.craftX, 13.75, 'precondition: the craft held its between-cell x');
      assert.equal(obs.craftY, -41.25, 'precondition: the craft held its between-cell y');
      assert.equal(obs.slotX, 25.125 * 256, 'the craft depth is read exactly (row 25.125), not rounded');
      assert.equal(obs.slotY, 13.625 * 256, 'the craft lateral is read exactly (col 13.625), not rounded');
      assert.notEqual(obs.slotX, obs.row * 256, 'the exact read differs from the rounded cell here');
      assert.equal(obs.crosshairX, obs.slotX - 3072, 'the sight leads the EXACT craft depth by 96 px');
      assert.equal(obs.crosshairY, obs.slotY, 'the sight shares the EXACT craft lateral position');
    },
    // Sever the exact read (pin `player slot x` to a whole-cell constant): the sight no longer follows the
    // craft's true depth → the exact-read and lead assertions fail.
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'player slot x', 6144),
  },
  {
    key: 'hud-glyphs-never-stack-through-a-death',
    // ECO-02 / PLY-02 (slice 20 playtest: "The HUD font goes bold when I die"): a real craft death must rebuild
    // the HUD exactly once. The death path used to broadcast `craft changed` just before `transition to
    // player-dead`; the life clones that spawned were created after `director stop` went out, survived it, and
    // then ran the HUD's own director-enter spawn, stacking 2-3 copies of every glyph.
    behavior:
      'Through a real craft death the HUD is rebuilt exactly once: at player-dead and after the respawn, no two visible HUD glyph clones share a position (stacked duplicates made the HUD text look bold)',
    playtestStep: 5,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      writeVar(vm, 'invuln', 0); // a real, killable craft
      const craft0 = readVar(vm, 'eco-craft');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      const stacked = () => {
        const seen = new Map();
        for (const t of vm.runtime.targets) {
          if (t.isOriginal || t.sprite.name !== 'hud' || !t.visible) continue;
          const k = `${Math.round(t.x * 10)},${Math.round(t.y * 10)}`;
          seen.set(k, (seen.get(k) || 0) + 1);
        }
        return { visible: [...seen.values()].reduce((a, n) => a + n, 0), stacked: [...seen.values()].filter((n) => n > 1).length };
      };
      // A whole death (explosion -> player-dead -> respawn) can run inside ONE harness step, so player-dead is
      // not reliably visible at a step boundary. Sample instead after every thread step the sequencer runs
      // while the state is player-dead (the HUD's director-enter spawn is one of those threads), keeping the
      // worst stacking seen. On the first player-dead sample, clear the attacker and restore invulnerability
      // so the respawned craft is not killed again inside the same step.
      const seq = vm.runtime.sequencer;
      const original = seq.stepThread;
      let atDead = null;
      seq.stepThread = function hooked(thread) {
        original.call(this, thread);
        if (readVar(vm, 'game-director-state') !== 'player-dead') return;
        if (atDead === null) {
          atDead = { visible: 0, stacked: 0 };
          put('slot-type', 63, 0);
          put('slot-state', 63, 0);
          writeVar(vm, 'invuln', 1);
        }
        const now = stacked();
        atDead.visible = Math.max(atDead.visible, now.visible);
        atDead.stacked = Math.max(atDead.stacked, now.stacked);
      };
      try {
        // Park a Toroid on the craft's exact position each frame until the death lands.
        for (let i = 0; i < 60 && atDead === null; i += 1) {
          put('slot-type', 63, 10);
          put('slot-state', 63, 1);
          put('slot-x', 63, readVar(vm, 'player-slot-x'));
          put('slot-y', 63, readVar(vm, 'player-slot-y'));
          put('slot-dx', 63, 0);
          put('slot-dy', 63, 0);
          put('slot-flag', 63, 0);
          step(vm, 1);
        }
        let t = 0;
        while (readVar(vm, 'game-director-state') !== 'playing' && t < 200) {
          step(vm, 1);
          t += 1;
        }
      } finally {
        seq.stepThread = original;
      }
      step(vm, 2);
      return {
        atDead,
        craftLost: craft0 - readVar(vm, 'eco-craft'),
        afterRespawn: stacked(),
        respawned: readVar(vm, 'game-director-state'),
      };
    },
    assert(obs) {
      assert.ok(obs.atDead, 'precondition: the craft died and player-dead was observed');
      assert.equal(obs.craftLost, 1, 'precondition: exactly one craft was lost');
      assert.equal(obs.respawned, 'playing', 'precondition: the next craft respawned');
      assert.ok(obs.atDead.visible > 0, 'the HUD is on screen at player-dead');
      assert.equal(obs.atDead.stacked, 0, 'no HUD glyph is stacked on another at player-dead (no bold text)');
      assert.equal(obs.afterRespawn.stacked, 0, 'no HUD glyph is stacked on another after the respawn');
    },
    // Restore the pre-fix ordering — `craft changed` broadcast just before `transition to player-dead` — so the
    // racing life clones survive the stop and re-run the HUD spawn -> the no-stack assertion fails.
    negativeMutation: (p) => mutate.insertBroadcastBeforeTransition(p, 'Stage', 'player-dead', 'craft changed'),
  },
  {
    key: 'bomb-crosshair-leads-craft',
    behavior:
      'The bomb crosshair (slot 35) leads the craft by a fixed 96-px (-3072 unit) forward depth offset each tick while sharing the craft column — the reticle sits ahead of the craft (init_bombing solvalou_X + 0xF400), not on it',
    playtestStep: 3,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2); // let the walk read the craft position and track the crosshair
      const craftX = readVar(vm, 'player-slot-x'); // exact craft depth (PRES-01), not the rounded cell
      const craftY = readVar(vm, 'player-slot-y');
      return {
        crosshairX: readVar(vm, 'slot-x')[34], // Scratch slot 35 -> JS index 34
        crosshairY: readVar(vm, 'slot-y')[34],
        crosshairState: readVar(vm, 'slot-state')[34],
        leadExpect: craftX - 3072, // craft depth + BOMB_TARGET_LEAD (-12 cells * 256)
        lateralExpect: craftY,
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
    // Zero the forward lead so the crosshair sits on the craft depth → the lead assertion fails.
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
      const craftX = readVar(vm, 'player-slot-x'); // exact craft depth (PRES-01)
      const craftY = readVar(vm, 'player-slot-y');
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
        leadExpect: craftX - 3072,
        lateralExpect: craftY,
        craftDepth: craftX,
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
          } else if (t === 83) {
            // SEC-03 (secrets.hidden-credit #93, slice 14): the hidden Credit (0x53) is now built and spawns
            // from the schedule like a single-slot ground family (permanently invisible until bombed, then it
            // reveals the ~2s credit overlay), so seeing one in a slot is in scope. Its bomb-to-reveal / hold /
            // min-score behaviour is proved by the dedicated hidden-credit-* scenarios; here we only assert it
            // is not treated as unhandled leakage.
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
    // GND-07 (ground.domogram #89) lateral-cull fix (operator playtest, 2026-09-28): a self-moving ground
    // object must be removed at a LATERAL screen edge, not only off the bottom.
    key: 'domogram-culls-at-a-lateral-edge-not-only-the-bottom',
    behavior:
      "A self-moving ground object (the `advance ground moving` seam, shared by the Domogram's ACTIVE mover) is culled off ANY off-field edge, source-exact to check_scroll_offscreen ($30B4): the lateral test removes it when the column MSB is off either side — the RIGHT edge (cur_col >= CULL_COL_MAX = 0x1f) or, via the source's byte-wrap of a negative _Y, the LEFT edge (cur_col < 0) — while a mid-field object and both inclusive boundaries (col 0 and col 30) survive, and the bottom cull (cur_row >= 0x28) still fires. Before this fix a Domogram driven sideways slid along the screen edge forever until it scrolled off the bottom.",
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      writeVar(vm, 'game-director-state', 'frozen');
      // Seed a single ACTIVE Domogram (JS slot 15 == Scratch 1-based slot 16) with the given position and
      // velocity, everything else cleared, then run `advance ground moving` (the changed seam) once per tick.
      const seedMover = (x, y, dx, dy) => {
        for (let s = 0; s < 16; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
        put('slot-type', 15, 46); // Domogram (0x2E)
        put('slot-state', 15, 1); // ACTIVE
        put('slot-x', 15, x);
        put('slot-y', 15, y);
        put('slot-dx', 15, dx);
        put('slot-dy', 15, dy);
        writeVar(vm, 'slot-index', 16);
      };
      const runToCullOrHold = (ticks) => {
        for (let t = 0; t < ticks; t += 1) {
          callProc(vm, 'Stage', 'advance ground moving');
          step(vm, 1);
          if (readVar(vm, 'slot-state')[15] === 0) {
            return { culled: true, tick: t + 1, col: readVar(vm, 'slot-y')[15] / 256, row: readVar(vm, 'slot-x')[15] / 256 };
          }
        }
        return { culled: false, col: readVar(vm, 'slot-y')[15] / 256, row: readVar(vm, 'slot-x')[15] / 256 };
      };

      // Probe R-cull: col 30, no vertical motion (dx 0, so it CANNOT bottom-cull — isolates the lateral edge),
      // dy 64 -> +256/tick lands exactly on col 31 (0x1f) on the first tick.
      seedMover(0, 30 * 256, 0, 64);
      const rightCull = runToCullOrHold(4);
      // Probe R-inside: col 30 held still (dy 0) is ON-field (valid 0..30) and must NEVER cull.
      seedMover(0, 30 * 256, 0, 0);
      const rightHold = runToCullOrHold(4);
      // Probe L-cull: col 0, dy -64 -> -256/tick reaches col -1 on the first tick (source byte-wrap edge).
      seedMover(0, 0, 0, -64);
      const leftCull = runToCullOrHold(4);
      // Probe L-inside: col 0 held still is the inclusive LEFT boundary and must NEVER cull.
      seedMover(0, 0, 0, 0);
      const leftHold = runToCullOrHold(4);
      // Probe mid-field: col 15 held still must never cull (the cull is edge-conditional, not "always").
      seedMover(0, 15 * 256, 0, 0);
      const midHold = runToCullOrHold(4);
      // Probe bottom (regression guard for the OR-refactor): row 39, dx 8 -> +32/tick crosses row 40 (0x28)
      // on the first tick; the bottom cull must still fire alongside the new lateral edges.
      seedMover(40 * 256 - 32, 15 * 256, 8, 0);
      const bottomCull = runToCullOrHold(4);

      return { rightCull, rightHold, leftCull, leftHold, midHold, bottomCull };
    },
    assert(obs) {
      assert.equal(obs.rightCull.culled, true, 'a mover driven RIGHT is culled at the lateral edge');
      assert.equal(obs.rightCull.tick, 1, 'it culls the tick it reaches col 31 (0x1f)');
      assert.equal(obs.rightCull.col, 31, 'the right edge cull fires exactly at col 31, inclusive (source _Y MSB >= 0x1f)');
      assert.equal(obs.rightHold.culled, false, 'col 30 is ON-field (valid 0..30) and is never culled — the right boundary is exclusive of 30');
      assert.equal(obs.leftCull.culled, true, 'a mover driven LEFT is culled at the lateral edge');
      assert.equal(obs.leftCull.tick, 1, 'it culls the tick it reaches col -1');
      assert.equal(obs.leftCull.col, -1, 'the left edge cull fires at col -1 (source byte-wrap of a negative _Y), NOT col -2');
      assert.equal(obs.leftHold.culled, false, 'col 0 is the inclusive LEFT boundary and is never culled');
      assert.equal(obs.midHold.culled, false, 'a mid-field (col 15) object is never culled — the cull is edge-conditional');
      assert.equal(obs.bottomCull.culled, true, 'the bottom cull (row >= 40) still fires after adding the lateral edges');
      assert.equal(obs.bottomCull.row, 40, 'the bottom cull fires exactly at row 40 (0x28), unchanged by the OR-refactor');
    },
    // Sever the moving seam: with `advance ground moving` neutralized the object never moves and never culls,
    // so every "is culled" probe (right, left, bottom) stays ACTIVE -> those assertions go red. Proves the
    // cull is carried by the changed seam, not by something else stepping the slot.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'advance ground moving'),
  },
  {
    // SEC-01 / ground.sol-tower (#90): the hidden citadel's reveal -> 7-step rise -> two-stage scoring.
    key: 'sol-tower-reveals-rises-then-a-second-bomb-craters-scoring-both-stages',
    behavior:
      'A hidden (ACTIVE, invisible) Sol Tower bombed scores its 2,000 and enters the 7-step rise (state HIT); while rising it is NOT re-scoreable; after the rise it returns to a live ACTIVE target (flag RISEN) so a SECOND bomb scores 2,000 AGAIN, then it craters PERSISTENTLY (stays HIT) — two scoring stages, the same value',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      const SOL_TOWER_TYPE = 29; // 0x1D
      const SOL_TOWER_PTS = 19; // 1-based value-table position of 2,000 (normal cabinet)
      const SLOT_ACTIVE = 1;
      const SOL_HIDDEN = 0;
      const SOL_RISEN = 2;
      // Freeze the walk so each manual callProc is exactly one tick and the live ground spawner cannot stamp
      // OTHER objects into the band mid-step (see bomb-kills-ground-and-scores). Clear the band first.
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      writeVar(vm, 'slot-index', 16); // point the per-slot update cursor at the seeded tower (Scratch 16 -> JS 15)
      const seedTower = (x, y) => {
        put('slot-type', 15, SOL_TOWER_TYPE);
        put('slot-state', 15, SLOT_ACTIVE); // ACTIVE but HIDDEN => invisible, still bombable/scoreable
        put('slot-flag', 15, SOL_HIDDEN);
        put('slot-pts', 15, SOL_TOWER_PTS);
        put('slot-x', 15, x);
        put('slot-y', 15, y);
        put('slot-timer', 15, 0);
      };
      // Bomb target = Scratch slot 33 -> JS index 32 (the locked reticle the ground detector reads).
      const aimBomb = (x, y) => {
        put('slot-x', 32, x);
        put('slot-y', 32, y);
      };
      const objX = () => readVar(vm, 'slot-x')[15];
      const objY = () => readVar(vm, 'slot-y')[15];
      seedTower(5120, 4096);
      aimBomb(5120, 4096);
      const award = readVar(vm, 'eco-value-table')[SOL_TOWER_PTS - 1];
      // STAGE 1: bomb the hidden tower. The shared ACTIVE-only detector scores 2,000 and marks it HIT.
      const s0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const stage1 = readVar(vm, 'eco-score') - s0;
      const stateAfterBomb1 = readVar(vm, 'slot-state')[15];
      // One update reveals the tower (flag HIDDEN -> RISING) and starts the rise clock; it stays HIT.
      callProc(vm, 'Stage', 'update sol tower');
      step(vm, 1);
      // MID-RISE: a rising tower (state HIT, not ACTIVE) must NOT be re-scoreable. Re-aim at its scrolled cell.
      aimBomb(objX(), objY());
      const sMid = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const midRiseScore = readVar(vm, 'eco-score') - sMid;
      // Drive the rise to completion: at step 7 (timer 112, 2 frames/tick) it flips back to ACTIVE + RISEN.
      let risenTick = -1;
      for (let t = 1; t <= 120; t += 1) {
        callProc(vm, 'Stage', 'update sol tower');
        step(vm, 1);
        if (readVar(vm, 'slot-state')[15] === SLOT_ACTIVE) {
          risenTick = t;
          break;
        }
      }
      const flagAfterRise = readVar(vm, 'slot-flag')[15];
      // STAGE 2: the risen tower is a live ACTIVE target again -> a second bomb scores 2,000 AGAIN.
      aimBomb(objX(), objY());
      const s2 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const stage2 = readVar(vm, 'eco-score') - s2;
      const stateAfterBomb2 = readVar(vm, 'slot-state')[15];
      // The destroy stage craters PERSISTENTLY: one more update keeps it HIT (never ACTIVE again) and a THIRD
      // bomb cannot re-score it.
      callProc(vm, 'Stage', 'update sol tower');
      step(vm, 1);
      const stateAfterDestroyTick = readVar(vm, 'slot-state')[15];
      aimBomb(objX(), objY());
      const s3 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const thirdBombScore = readVar(vm, 'eco-score') - s3;
      return {
        award,
        stage1,
        stateAfterBomb1,
        midRiseScore,
        risenTick,
        flagAfterRise,
        stage2,
        stateAfterBomb2,
        stateAfterDestroyTick,
        thirdBombScore,
        SOL_RISEN,
      };
    },
    assert(obs) {
      assert.equal(obs.award, 2000, 'the Sol Tower is worth its 2,000-pt value-table entry (normal cabinet)');
      assert.equal(obs.stage1, 2000, 'STAGE 1: bombing the hidden tower scores its 2,000 once');
      assert.equal(obs.stateAfterBomb1, 2, 'the bombed tower is marked HIT');
      assert.equal(obs.midRiseScore, 0, 'a rising tower (HIT, not ACTIVE) is NOT re-scoreable between the two stages');
      assert.ok(obs.risenTick > 0, 'the tower completes its rise and returns to a live target');
      assert.ok(obs.risenTick <= 60, 'the rise completes on the ~56-tick clock (step 7 at timer 112, 2 frames/tick)');
      assert.equal(obs.flagAfterRise, obs.SOL_RISEN, 'the risen tower carries the RISEN phase');
      assert.equal(obs.stage2, 2000, 'STAGE 2: the risen ACTIVE target scores 2,000 AGAIN on the second bomb');
      assert.equal(obs.stateAfterBomb2, 2, 'the second bomb marks the risen tower HIT');
      assert.equal(obs.stateAfterDestroyTick, 2, 'the destroy stage craters PERSISTENTLY (stays HIT, never ACTIVE again)');
      assert.equal(obs.thirdBombScore, 0, 'the persistent crater cannot be scored a third time');
    },
    // Sever the Sol Tower update: the bombed tower never reveals/rises, so it never returns to ACTIVE -> the
    // rise-completion and second-stage-scoring assertions go red.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update sol tower'),
  },
  {
    // Slice-15 PR-1: the shared ground-renderer clone pool. The 10 former full-band ground families (Barra,
    // Sol Tower, Garu, Logram, Zolbak, Derota, Garu Derota, Boza, Grobda, Domogram) each rendered as their own
    // 16-clone pool (160 clones) against scratch-vm's hard 300-clone ceiling; they are collapsed into ONE
    // 16-clone "ground" pool. This is the whole point of the refactor, so it must show up as exactly one ground
    // band of 16 clones, no legacy per-family render target left behind, and a materially lower live clone
    // total (the ~144 clones the merge frees are the headroom the slice-16 Andor boss needs).
    key: 'ground-pool-is-one-shared-clone-band',
    behavior:
      'The ten former full-band ground families render through ONE shared "ground" clone pool of exactly 16 clones (one per ground slot), not ten separate 16-clone pools; no legacy per-family render target survives, and the live clone total sits well under the scratch-vm 300-clone ceiling with real headroom (the ~144 clones the merge frees)',
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Free-run a little so the field is populated: the ground band is stamped at director enter and the
      // flyer pools + bullets fill in as areas scroll, so the live total reflects real play.
      step(vm, 200);
      const LEGACY = [
        'barra', 'sol-tower', 'garu', 'logram', 'zolbak',
        'derota', 'garu derota', 'boza', 'grobda', 'domogram',
      ];
      const byName = {};
      for (const c of vm.runtime.targets) {
        if (c.isStage || c.isOriginal || !c.sprite) continue;
        byName[c.sprite.name] = (byName[c.sprite.name] || 0) + 1;
      }
      const totalClones = Object.values(byName).reduce((a, b) => a + b, 0);
      return {
        groundClones: cloneCount(vm, 'ground'),
        legacyClones: LEGACY.reduce((a, n) => a + (byName[n] || 0), 0),
        totalClones,
      };
    },
    assert(obs) {
      assert.equal(
        obs.groundClones,
        16,
        'the shared ground pool renders as exactly 16 clones (one per ground slot)',
      );
      assert.equal(obs.legacyClones, 0, 'no legacy per-family ground render pool survives the merge');
      // Pre-refactor the ground band alone was 160 clones and the whole field ran ~292 against the 300
      // ceiling; with the merge it must sit well under it. 250 is a generous bound the pre-refactor project
      // (160 ground clones) could never satisfy, so a regression that re-splits the pool trips it.
      assert.ok(
        obs.totalClones < 250,
        `the live clone total (${obs.totalClones}) sits under the 300 ceiling with headroom`,
      );
      assert.ok(
        300 - obs.totalClones >= 50,
        `the merge leaves real headroom under the 300-clone ceiling (${300 - obs.totalClones} free)`,
      );
    },
    // Break the ground pool's `if state == playing` spawn gate so it creates ZERO clones → the 16-clone
    // census assertion goes red (the shared pool never materializes).
    negativeMutation: (p) => mutate.changeEqualsOperand(p, 'ground', 'playing', '__never__'),
  },
  {
    // Slice-15 PR-1: render-equivalence of the shared ground pool. Each ground clone reads its slot's live
    // `slot type` and dispatches to that family's costume subtree, with the family's costume ordinals rebased
    // into ONE combined 129-costume list (GROUND_FAMILY_OFFSETS via _sw). The correctness crux (the riskiest
    // seam in the plan) is that a clone on a slot of family X shows a costume from X's OWN band — a wrong
    // offset would send it into another family's costumes. This drives live play and, for every ACTIVE ground
    // slot whose clone is drawn, asserts the clone's current costume belongs to that slot type's family.
    key: 'ground-pool-dispatches-costume-by-slot-type',
    behavior:
      "Every ground clone renders its slot type's OWN family costume: a clone bound to an ACTIVE slot of family X (read from the shared slot lists) shows a costume from X's band in the combined ground costume list — proving the shared pool's slot-type dispatch and the per-family costume-ordinal rebase are correct",
    playtestStep: 4,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const slotName = variable('ground-clone-slot').name; // "ground clone slot"
      const GROBDA = new Set([44, 53, 54, 56, 57, 58, 59, 60, 61, 62, 63, 64]);
      // type -> { name, ok(costumeName) } for the ACTIVE (state 1) render of that family. Restricting to
      // ACTIVE + visible keeps this to each family's idle/active costume (the shared HIT burst/crater frames
      // are index-selected and proven by the per-family scenarios), so a costume outside the family's band
      // means the slot-type dispatch or the offset rebase sent the clone to the wrong costumes.
      const familyOf = (t) => {
        if (t === 30 || t === 32)
          return { name: 'barra/garu', ok: (c) => c === 'barra/idle/01' || /^garu\/base\//.test(c) };
        if (t === 31) return { name: 'zolbak', ok: (c) => c === 'zolbak/idle/01' };
        if (t === 27 || t === 33)
          return { name: 'derota/garu-derota', ok: (c) => c === 'derota/idle/01' || /^garu-derota\/base\//.test(c) };
        if (t === 38) return { name: 'logram', ok: (c) => /^logram\/open\/0[1-4]$/.test(c) };
        if (t === 45)
          return { name: 'boza', ok: (c) => c === 'boza-centre/core/01' || /^logram\/open\/0[1-4] #boza$/.test(c) };
        if (GROBDA.has(t)) return { name: 'grobda', ok: (c) => /^grobda\/roll\/0[1-4]$/.test(c) };
        if (t === 46) return { name: 'domogram', ok: (c) => /^domogram\/idle\/0[1-4]$/.test(c) };
        if (t === 29) return { name: 'sol-tower', ok: (c) => /^sol-tower\//.test(c) };
        return null; // Bacura and any non-full-band type are not this pool's job
      };
      const prevType = new Array(16).fill(0);
      const seen = new Set();
      const mismatches = [];
      let observations = 0;
      const HARD_CAP = 5000;
      for (let i = 0; i < HARD_CAP; i += 1) {
        step(vm, 1);
        const types = readVar(vm, 'slot-type');
        const states = readVar(vm, 'slot-state');
        for (const rep of cloneReports(vm, 'ground', [slotName])) {
          const slot = rep.vars[slotName]; // Scratch 1-based slot
          const s = slot - 1;
          if (s < 0 || s > 15) continue;
          if (!rep.visible) continue; // the render arm sets show() only when a family arm matched this tick
          if (states[s] !== 1) continue; // ACTIVE render only
          const t = types[s];
          if (t === 0) continue;
          if (prevType[s] !== t) continue; // steady across two ticks: the clone rendered this settled type
          const fam = familyOf(t);
          if (!fam) continue;
          observations += 1;
          seen.add(fam.name);
          if (!fam.ok(rep.costume || '')) {
            mismatches.push({ slot, type: t, family: fam.name, costume: rep.costume });
          }
        }
        for (let s = 0; s < 16; s += 1) prevType[s] = types[s];
        if (seen.has('barra/garu') && seen.has('logram') && observations > 50) break;
        if (readVar(vm, 'area-number') >= 6) break;
      }
      return { mismatches, seen: [...seen], observations };
    },
    assert(obs) {
      assert.ok(obs.observations > 0, 'at least one ACTIVE ground clone was observed rendering');
      assert.deepEqual(
        obs.mismatches,
        [],
        "every ACTIVE ground clone shows a costume from its own slot type's family (no wrong-band dispatch or offset)",
      );
      assert.ok(
        obs.seen.includes('logram'),
        'a Logram (a non-zero-offset family) was observed rendering through the shared pool (non-vacuous)',
      );
    },
    // Zero the Logram family's costume-ordinal offset (39) in the combined list, so every Logram clone
    // switches into the WRONG (Barra) band instead of logram/open — the exact class of bug the per-family
    // ordinal rebase risks. Logram still dispatches and shows (visible), so it is observed with a wrong
    // costume and the no-mismatch assertion goes red.
    negativeMutation: (p) => mutate.changeAddLiteral(p, 'ground', '39', '0'),
  },
  {
    // BOSS-01 / andor.lifecycle (#94): the arrival state machine (handle_4B_Andor_Genesis, xevious_main.68k
    // 5386-5406). Driven deterministically: freeze the director and tick `update andor master` by hand
    // (callProc + step), reading the shared anchor var — pacing-invariant, not a live-walk timing assertion.
    key: 'boss-arrives-and-holds',
    behavior:
      'The Andor Genesis master descends from off the top edge (ANDOR_START_X) toward the fixed hold row by a constant step each tick, lands EXACTLY on the hold row (clamped, never overshooting), and then holds with no further motion while the end flag is clear',
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      writeVar(vm, 'game-director-state', 'frozen');
      clearGroundBand(vm);
      writeVar(vm, 'andor-genesis-end-flag', 0);
      writeVar(vm, 'andor-master-x', ANDOR.START_X);
      writeVar(vm, 'andor-master-y', ANDOR.LATERAL_Y);
      const x0 = readVar(vm, 'andor-master-x');
      const tick = () => {
        callProc(vm, 'Stage', 'update andor master');
        step(vm, 1);
      };
      tick();
      const afterOne = readVar(vm, 'andor-master-x');
      // Run well past the arrival window ((HOLD-START)/STEP = 96 ticks lands exactly; 120 is a safe margin).
      for (let i = 0; i < 120; i += 1) tick();
      const atHold = readVar(vm, 'andor-master-x');
      tick(); // one more tick at the hold row must not move
      const heldNext = readVar(vm, 'andor-master-x');
      // Clamp proof: from just below the hold row a full step would overshoot -> clamp back to exactly hold.
      writeVar(vm, 'andor-master-x', ANDOR.HOLD_X - ANDOR.DESCEND_STEP / 2); // 4064; +64 -> 4128 > hold
      tick();
      const clamped = readVar(vm, 'andor-master-x');
      return { x0, afterOne, atHold, heldNext, clamped };
    },
    assert(obs) {
      assert.equal(obs.x0, ANDOR.START_X, 'the master starts off the top edge at ANDOR_START_X');
      assert.equal(
        obs.afterOne,
        ANDOR.START_X + ANDOR.DESCEND_STEP,
        'one tick descends the anchor by exactly one descend step',
      );
      assert.equal(obs.atHold, ANDOR.HOLD_X, 'the descent lands exactly on the hold row (ANDOR_HOLD_X)');
      assert.equal(obs.heldNext, ANDOR.HOLD_X, 'at the hold row the master holds — no further motion');
      assert.equal(
        obs.clamped,
        ANDOR.HOLD_X,
        'a step that would overshoot the hold row is clamped back to it (never past)',
      );
    },
    // Empty the master proc -> the anchor never descends from START -> the "reaches hold" clauses go red.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update andor master'),
  },
  {
    // BOSS-01 / andor.lifecycle (#94): per-frame composite alignment (handle_41..4F part offsets,
    // xevious_main.68k 5447-5983). `update andor part` pins the slot at `slot index` to the shared anchor +
    // the part's per-type offset. Asserted against the anchor VAR (not the master slot), so the one-tick
    // ascending-walk lag (finding #5) can never flake this.
    key: 'boss-parts-align-to-master',
    behavior:
      "Every visible Andor part pins rigidly to the master's shared anchor plus its own per-type composite offset: `update andor part` sets the slot's x/y to `andor master x/y` + the part's offset (from the owned offset lists) regardless of the slot's prior position, and when the anchor moves each part tracks it by the same delta — the composite moves as one body",
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = slotPutter(vm);
      writeVar(vm, 'game-director-state', 'frozen');
      clearGroundBand(vm);
      const depth = readVar(vm, 'andor-part-depth');
      const lateral = readVar(vm, 'andor-part-lateral');
      // Sample three parts spanning the offset table — an armor plate, a gun port, the core — each on its
      // own Scratch slot. Seed each slot's x/y to a WRONG sentinel first, so a passing assertion can only
      // mean the proc RE-COMPUTED the position from the anchor (not that it happened to match a seed).
      const cases = [
        { slot: 2, type: 0x41 }, // armor (obj 1)
        { slot: 14, type: 0x4f }, // gun port (obj 13)
        { slot: 15, type: 0x4a }, // core (obj 14)
      ];
      const align = (slot, type) => {
        const i = slot - 1;
        put('slot-type', i, type);
        put('slot-state', i, 1);
        put('slot-x', i, 999999); // wrong sentinel
        put('slot-y', i, -999999);
        writeVar(vm, 'slot-index', slot);
        callProc(vm, 'Stage', 'update andor part');
        step(vm, 1);
        return { type, off: type - 0x41, x: readVar(vm, 'slot-x')[i], y: readVar(vm, 'slot-y')[i] };
      };
      writeVar(vm, 'andor-master-x', ANDOR.HOLD_X);
      writeVar(vm, 'andor-master-y', ANDOR.LATERAL_Y);
      const first = cases.map((c) => align(c.slot, c.type));
      // Rigidity: move the anchor by a delta and re-run; every part must track by the SAME delta.
      const delta = 512;
      writeVar(vm, 'andor-master-x', ANDOR.HOLD_X + delta);
      writeVar(vm, 'andor-master-y', ANDOR.LATERAL_Y - delta);
      const second = cases.map((c) => align(c.slot, c.type));
      return { depth, lateral, first, second, delta };
    },
    assert(obs) {
      for (const p of obs.first) {
        assert.equal(
          p.x,
          ANDOR.HOLD_X + obs.depth[p.off],
          `part 0x${p.type.toString(16)} slot x = anchor x + its depth offset`,
        );
        assert.equal(
          p.y,
          ANDOR.LATERAL_Y + obs.lateral[p.off],
          `part 0x${p.type.toString(16)} slot y = anchor y + its lateral offset`,
        );
      }
      for (const p of obs.second) {
        assert.equal(
          p.x,
          ANDOR.HOLD_X + obs.delta + obs.depth[p.off],
          `part 0x${p.type.toString(16)} tracks the moved anchor x rigidly (same delta)`,
        );
        assert.equal(
          p.y,
          ANDOR.LATERAL_Y - obs.delta + obs.lateral[p.off],
          `part 0x${p.type.toString(16)} tracks the moved anchor y rigidly (same delta)`,
        );
      }
      // Non-vacuous: the sampled parts sit at DISTINCT composite offsets (a real octagon, not all-centre).
      const offs = obs.first.map((p) => `${obs.depth[p.off]},${obs.lateral[p.off]}`);
      assert.ok(new Set(offs).size >= 2, 'the sampled parts have distinct composite offsets');
    },
    // Empty the part proc -> slot x/y stay at the wrong sentinel -> every alignment clause goes red.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update andor part'),
  },
  {
    // BOSS-01 / andor.lifecycle (#94): the departure + teardown (andor_genesis_leave 5426-5438,
    // remove_andor_genesis 5440-5443). On the end flag the master retreats up and, once clear of the top,
    // frees every part slot (the port consolidates the parts' self-removal into the master's teardown —
    // record 046 deviation #3). No reward is given (destruction is slice 16).
    key: 'boss-departs-on-end-flag',
    behavior:
      'On the end flag the Andor Genesis master retreats upward by a constant step each tick (back toward ANDOR_START_X) and, once it clears the top edge, TEARS DOWN the whole composite — every one of the 15 part slots is freed (type and state 0) and the end flag is consumed — with no reward',
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = slotPutter(vm);
      writeVar(vm, 'game-director-state', 'frozen');
      clearGroundBand(vm);
      // Seed the full composite held on-field (all 15 slots occupied), then raise the end flag.
      for (let n = 1; n <= 15; n += 1) {
        put('slot-type', ANDOR.BASE_SLOT + n - 1, ANDOR_PART_TYPES_BY_OBJ[n - 1]);
        put('slot-state', ANDOR.BASE_SLOT + n - 1, 1);
      }
      writeVar(vm, 'andor-master-x', ANDOR.HOLD_X);
      writeVar(vm, 'andor-master-y', ANDOR.LATERAL_Y);
      writeVar(vm, 'andor-genesis-end-flag', 1);
      const tick = () => {
        callProc(vm, 'Stage', 'update andor master');
        step(vm, 1);
      };
      const xStart = readVar(vm, 'andor-master-x');
      // RETREAT: a few ticks while still on-screen — the anchor decreases by exactly one leave step/tick.
      const retreatSamples = [];
      for (let i = 0; i < 5; i += 1) {
        tick();
        retreatSamples.push(readVar(vm, 'andor-master-x'));
      }
      // Run out until it clears the top and tears down (HOLD->below START at 32/tick ~= 192 ticks; 400 safe).
      let tornDown = false;
      for (let i = 0; i < 400 && !tornDown; i += 1) {
        tick();
        const types = readVar(vm, 'slot-type');
        tornDown = ANDOR_PART_TYPES_BY_OBJ.every((_, n) => types[ANDOR.BASE_SLOT + n] === 0);
      }
      const types = readVar(vm, 'slot-type');
      const states = readVar(vm, 'slot-state');
      return {
        xStart,
        retreatSamples,
        tornDown,
        freedTypes: ANDOR_PART_TYPES_BY_OBJ.map((_, n) => types[ANDOR.BASE_SLOT + n]),
        freedStates: ANDOR_PART_TYPES_BY_OBJ.map((_, n) => states[ANDOR.BASE_SLOT + n]),
        endFlag: readVar(vm, 'andor-genesis-end-flag'),
      };
    },
    assert(obs) {
      assert.equal(obs.xStart, ANDOR.HOLD_X, 'the composite starts held on-field');
      for (let i = 0; i < obs.retreatSamples.length; i += 1) {
        assert.equal(
          obs.retreatSamples[i],
          ANDOR.HOLD_X - ANDOR.LEAVE_STEP * (i + 1),
          'the master retreats up by exactly one leave step each tick',
        );
      }
      assert.ok(obs.tornDown, 'once clear of the top the composite tears down within the departure window');
      assert.deepEqual(obs.freedTypes, new Array(15).fill(0), 'every one of the 15 part slots is freed (type 0)');
      assert.deepEqual(
        obs.freedStates,
        new Array(15).fill(0),
        'every one of the 15 part slots is freed (state 0)',
      );
      assert.equal(obs.endFlag, 0, 'the end flag is consumed by the teardown');
    },
    // Break the `end flag == 1` gate so departure never triggers -> no retreat, no teardown -> red.
    negativeMutation: (p) =>
      mutate.changeVarEqualsOperand(p, 'Stage', 'andor genesis end flag', 1, 999),
  },
  {
    // BOSS-01 / andor.lifecycle (#94): the ground band re-isolates in BOTH directions (plan finding #2). The
    // boss confines its mutable state to shared vars + slot type/state/x/y, so (a) armed over a stale slot it
    // overwrites the stale position from the anchor and touches no cross-field, and (b) on teardown it leaves
    // no residual boss field behind. Both directions are asserted; the vacuity negative severs direction (a).
    key: 'boss-band-isolation',
    behavior:
      "The Andor composite confines its mutable state to shared variables + slot type/state/x/y, so the ground band re-isolates both ways: (a) armed over a slot holding a STALE object, the part proc recomputes slot x/y from the anchor and leaves every cross-field (flag/pts/timer/code/dx/dy) untouched; (b) through its whole life and teardown the boss writes none of those cross-fields, so the vacated slot is freed clean for the next normal occupant",
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = slotPutter(vm);
      writeVar(vm, 'game-director-state', 'frozen');
      clearGroundBand(vm);
      const slot = 5; // Scratch slot 5 -> obj 4 -> an armor plate type
      const i = slot - 1;
      const type = ANDOR_PART_TYPES_BY_OBJ[slot - ANDOR.BASE_SLOT - 1];
      const depth = readVar(vm, 'andor-part-depth');
      const lateral = readVar(vm, 'andor-part-lateral');
      const off = type - 0x41;
      // (a) Pre-load the slot with a STALE object's cross-fields + garbage x/y, then arm the boss part over
      // it and run one alignment tick.
      const SENT_A = { flag: 7, pts: 13, timer: 99, code: 42, dx: -55, dy: 66 };
      put('slot-flag', i, SENT_A.flag);
      put('slot-pts', i, SENT_A.pts);
      put('slot-timer', i, SENT_A.timer);
      put('slot-code', i, SENT_A.code);
      put('slot-dx', i, SENT_A.dx);
      put('slot-dy', i, SENT_A.dy);
      put('slot-x', i, 123456); // stale garbage position
      put('slot-y', i, 654321);
      put('slot-type', i, type);
      put('slot-state', i, 1);
      writeVar(vm, 'andor-master-x', ANDOR.HOLD_X);
      writeVar(vm, 'andor-master-y', ANDOR.LATERAL_Y);
      writeVar(vm, 'slot-index', slot);
      callProc(vm, 'Stage', 'update andor part');
      step(vm, 1);
      const aRecomputedX = readVar(vm, 'slot-x')[i];
      const aRecomputedY = readVar(vm, 'slot-y')[i];
      const aCross = {
        flag: readVar(vm, 'slot-flag')[i],
        pts: readVar(vm, 'slot-pts')[i],
        timer: readVar(vm, 'slot-timer')[i],
        code: readVar(vm, 'slot-code')[i],
        dx: readVar(vm, 'slot-dx')[i],
        dy: readVar(vm, 'slot-dy')[i],
      };
      // (b) Fresh cross-field sentinels on the same slot, seed the full composite, raise the end flag and run
      // to teardown; the boss must leave those fields untouched through its whole life (owns only type/state/x/y).
      clearGroundBand(vm);
      const SENT_B = { flag: 3, pts: 9, timer: 44, code: 21, dx: 12, dy: -8 };
      put('slot-flag', i, SENT_B.flag);
      put('slot-pts', i, SENT_B.pts);
      put('slot-timer', i, SENT_B.timer);
      put('slot-code', i, SENT_B.code);
      put('slot-dx', i, SENT_B.dx);
      put('slot-dy', i, SENT_B.dy);
      for (let n = 1; n <= 15; n += 1) {
        put('slot-type', ANDOR.BASE_SLOT + n - 1, ANDOR_PART_TYPES_BY_OBJ[n - 1]);
        put('slot-state', ANDOR.BASE_SLOT + n - 1, 1);
      }
      writeVar(vm, 'andor-master-x', ANDOR.HOLD_X);
      writeVar(vm, 'andor-master-y', ANDOR.LATERAL_Y);
      writeVar(vm, 'andor-genesis-end-flag', 1);
      let tornDown = false;
      for (let k = 0; k < 400 && !tornDown; k += 1) {
        callProc(vm, 'Stage', 'update andor master');
        step(vm, 1);
        tornDown = readVar(vm, 'slot-type')[i] === 0;
      }
      const bCross = {
        flag: readVar(vm, 'slot-flag')[i],
        pts: readVar(vm, 'slot-pts')[i],
        timer: readVar(vm, 'slot-timer')[i],
        code: readVar(vm, 'slot-code')[i],
        dx: readVar(vm, 'slot-dx')[i],
        dy: readVar(vm, 'slot-dy')[i],
      };
      return {
        off,
        depth,
        lateral,
        aRecomputedX,
        aRecomputedY,
        aCross,
        sentA: SENT_A,
        tornDown,
        bCross,
        sentB: SENT_B,
        bFreedType: readVar(vm, 'slot-type')[i],
        bFreedState: readVar(vm, 'slot-state')[i],
      };
    },
    assert(obs) {
      // (a) stale x/y overwritten from the anchor + offset (stale garbage ignored)
      assert.equal(
        obs.aRecomputedX,
        ANDOR.HOLD_X + obs.depth[obs.off],
        '(a) the part recomputes slot x from the anchor, ignoring the stale value',
      );
      assert.equal(
        obs.aRecomputedY,
        ANDOR.LATERAL_Y + obs.lateral[obs.off],
        '(a) the part recomputes slot y from the anchor, ignoring the stale value',
      );
      // (a) the boss part proc wrote ONLY x/y — every cross-field is untouched (no bleed into the slot)
      assert.deepEqual(obs.aCross, obs.sentA, '(a) the boss part proc leaves every slot cross-field untouched');
      // (b) the composite freed the slot, and left no residual boss field for the next occupant
      assert.ok(obs.tornDown, '(b) the departing composite frees the slot');
      assert.equal(obs.bFreedType, 0, '(b) the vacated slot type is 0');
      assert.equal(obs.bFreedState, 0, '(b) the vacated slot state is 0');
      assert.deepEqual(
        obs.bCross,
        obs.sentB,
        '(b) the boss leaves no residual cross-field on the vacated slot through its whole life',
      );
    },
    // Empty the part proc -> direction (a) never recomputes the stale x/y -> the (a) clauses go red.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update andor part'),
  },
  {
    // BOSS-01 / andor.lifecycle (#94) finding #1 (the single biggest correctness trap): the `color` graphic
    // effect PERSISTS on the 16 reused ground clones. The boss arms are the only ground arms that set it, so
    // the render loop clears it for every clone each tick; a clone that drew a boss part must show NO residual
    // tint on the next normal ground object. Driven live (the render loop must run), with ground spawns
    // suppressed + the band cleared so nothing else stamps the slot under test.
    key: 'non-boss-clone-has-no-residual-tint',
    behavior:
      'A ground clone that drew a boss part (its `color` graphic effect set from the shared colour var) shows ZERO colour effect on the next tick when its slot becomes a normal ground object — the per-tick clear at the top of the render dispatch prevents boss tint from bleeding onto ordinary ground',
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      suppressGroundSpawns(vm);
      const put = slotPutter(vm);
      // Clear the live band so only the slot under test renders, and pin the anchor on-field so the part the
      // walk aligns each tick stays on-screen. No master TYPE is seeded (so nothing resets the colour var).
      clearGroundBand(vm);
      writeVar(vm, 'andor-master-x', ANDOR.HOLD_X);
      writeVar(vm, 'andor-master-y', ANDOR.LATERAL_Y);
      writeVar(vm, 'andor-genesis-colour', 5); // a non-zero palette index -> a visible tint via the boss arm
      const slot = 2; // Scratch slot 2 (JS 1)
      const i = slot - 1;
      // Draw a boss part (armor) at the slot: the boss arm sets the colour effect on this clone.
      put('slot-type', i, 0x41);
      put('slot-state', i, 1);
      put('slot-x', i, 3000);
      put('slot-y', i, 3000);
      step(vm, 2);
      const boss = groundCloneEffect(vm, slot);
      // Now flip the same slot to a NORMAL ground object (Barra); the top-of-loop clear must zero the effect.
      // A live step runs the walk to settling (~200+ ticks at +32 scroll each), so seed the Barra near the top of
      // the visible rows (row 8) and give it ONE step: from slot x 3000 two steps scrolled it to ~8.8k, and a
      // slow run carried it past row 40 (10240), where the in-view gate hides it and the check went vacuous.
      put('slot-type', i, 30); // BARRA_TYPE
      put('slot-state', i, 1);
      put('slot-x', i, 2048);
      put('slot-y', i, 3000);
      step(vm, 1);
      const normal = groundCloneEffect(vm, slot);
      return { boss, normal };
    },
    assert(obs) {
      assert.ok(obs.boss, 'the ground clone bound to the test slot exists');
      assert.ok(obs.boss.effect > 0, 'the clone drawing a boss part has a non-zero colour effect (tinted)');
      assert.ok(obs.normal, 'the same ground clone is still present after the slot becomes normal');
      assert.equal(
        obs.normal.effect,
        0,
        'the clone shows NO residual colour effect once it draws a normal ground object',
      );
      assert.ok(
        obs.normal.visible && /^barra\//.test(obs.normal.costume || ''),
        'non-vacuous: the clone actually rendered a normal (Barra) ground object',
      );
    },
    // Delete the per-tick clear-graphic-effects -> the boss tint persists onto the normal object -> red.
    negativeMutation: (p) => mutate.removeClearGraphicEffects(p, 'ground'),
  },
  {
    // BOSS-01 / andor.lifecycle (#94): the LIVE debug-key summon path (install_debug_ground_spawn) — the one
    // path the operator actually drives. Every OTHER boss scenario above FREEZES the walk and calls the update
    // procs by hand, so none of them exercised the debug key's arm/dismiss handler. That gap let a same-tick
    // self-dismiss ship: the original dismiss read the master slot AFTER the arm stamped it and set the end flag
    // on the very press that summoned the boss, so the master tore the composite down at START_X before it could
    // descend — "Andor never shows up; the ground enemies just start over" (operator playtest, 2026-09-27). The
    // fix gates the dismiss on a FRESH press (rising edge of `debug ground key held`) AND a boss already present
    // at the start of the tick. This scenario drives the real key end-to-end: HOLD G to summon and hold (the end
    // flag must stay 0 while held), then RELEASE + a fresh press to dismiss (end flag set -> master retreats off
    // the top -> composite freed). It is the regression net the frozen-walk scenarios could not be.
    key: 'boss-summoned-and-dismissed-by-debug-key',
    behavior:
      "Holding the debug ground key (G) with the family cursor on the Andor entry ARMS all 15 composite parts and the invisible master, which then descends from off the top edge while the key stays held — the end flag stays 0, so the boss is NOT self-dismissed on the press that summoned it; releasing G and pressing it again (a fresh rising edge, boss present) sets the end flag, and the master retreats off the top and frees every boss slot",
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      // Suppress the scheduled ground stream and clear the live band so the ONLY ground object that can appear is
      // the debug key's, then park the family cursor on the Andor entry (last in DEBUG_GROUND_FAMILIES) so the
      // next fresh, field-empty spawn arms the boss.
      suppressGroundSpawns(vm);
      clearGroundBand(vm);
      const ANDOR_FAMILY_INDEX = 16; // index of (ANDOR_MASTER_TYPE, 'andor') in DEBUG_GROUND_FAMILIES
      writeVar(vm, 'debug-ground-index', ANDOR_FAMILY_INDEX);
      const masterJs = ANDOR.BASE_SLOT + 15 - 1; // Scratch slot 16 -> JS index 15
      const armedCount = () => {
        const t = readVar(vm, 'slot-type');
        let n = 0;
        for (let s = 0; s <= 15; s += 1) {
          const x = t[s];
          if ((x >= 0x41 && x <= 0x4b) || (x >= 0x4f && x <= 0x52)) n += 1;
        }
        return n;
      };
      // SUMMON: hold G. The first field-empty tick arms the composite; subsequent held ticks let the master
      // descend. Sample the end flag across several held pumps -> it must never be raised while held.
      keyDown(vm, 'g');
      step(vm, 1);
      const armedFirst = armedCount();
      const masterFirst = readVar(vm, 'slot-type')[masterJs];
      const endHeld = [readVar(vm, 'andor-genesis-end-flag')];
      for (let i = 0; i < 3; i += 1) {
        step(vm, 1);
        endHeld.push(readVar(vm, 'andor-genesis-end-flag'));
      }
      const armedHeld = armedCount();
      const masterHeld = readVar(vm, 'slot-type')[masterJs];
      const xHeld = readVar(vm, 'andor-master-x');
      // DISMISS: release, then a fresh press. The rising edge with the boss present raises the end flag; the
      // master then retreats off the top and frees every boss slot.
      keyUp(vm, 'g');
      step(vm, 1);
      keyDown(vm, 'g');
      let endRaised = false;
      let tornDown = false;
      for (let i = 0; i < 12 && !tornDown; i += 1) {
        step(vm, 1);
        if (readVar(vm, 'andor-genesis-end-flag') === 1) endRaised = true;
        if (armedCount() === 0) tornDown = true;
      }
      keyUp(vm, 'g');
      return { armedFirst, masterFirst, armedHeld, masterHeld, xHeld, endHeld, endRaised, tornDown };
    },
    assert(obs) {
      // Summoned: all 15 parts armed, the invisible master typed at Scratch slot 16.
      assert.equal(obs.armedFirst, 15, 'holding G on the Andor cursor arms all 15 composite parts');
      assert.equal(obs.masterFirst, ANDOR.MASTER_TYPE, 'the invisible master is typed at Scratch slot 16');
      // NOT self-dismissed while held: the end flag stays 0 across every held pump — the biting check for the
      // shipped same-tick self-dismiss bug.
      for (const e of obs.endHeld) {
        assert.equal(e, 0, 'the end flag is NOT raised while G is held (no same-tick self-dismiss)');
      }
      // Still up and descending after the held pumps (moved off START_X toward the hold row).
      assert.equal(obs.armedHeld, 15, 'the composite stays armed while G is held (not torn down)');
      assert.equal(obs.masterHeld, ANDOR.MASTER_TYPE, 'the master stays present while G is held');
      assert.ok(
        obs.xHeld > ANDOR.START_X,
        `the master descends from START_X while held (x=${obs.xHeld} > ${ANDOR.START_X})`,
      );
      // Dismissed by a fresh press: the end flag is raised and the whole composite is freed.
      assert.ok(obs.endRaised, 'a fresh G press with the boss present raises the end flag (dismiss)');
      assert.ok(obs.tornDown, 'after the dismiss the master retreats off the top and frees every boss slot');
    },
    // Reproduce the shipped bug: flip the rising-edge guard from `debug ground key held == 0` to `== 1`, so a
    // HELD key (held is set to 1 each tick) fires the dismiss every tick -> the boss is self-dismissed on the
    // press that summons it and torn down before it can hold -> the "armed while held" / "end stays 0" checks bite.
    negativeMutation: (p) => mutate.changeVarEqualsOperand(p, 'Stage', 'debug ground key held', 0, 1),
  },
  {
    // BOSS-02 / andor.defenses (#95): the four gun ports fire on the SHARED periodic gate under the boss fire
    // mask captured at arm (update andor part -> fire permission gate, gated on SLOT_ACTIVE, xevious_main.68k
    // 5533/5584/5635/5686 -> chk_timer_fire_bullet_reinit_timer 4999-5010), and the NON-contiguous mask 47
    // reloads BIT-EXACTLY as (rng & 47) -> values in {0-15, 32-47}, never 16-31 (the shared gate's `rng mod 48`
    // would be wrong — riskiest seam). Driven per-tick: freeze the director and hand-drive the procs (callProc +
    // step), pinning `tick` on-phase so the every-4th-tick cadence passes on each manual call. The harness runs
    // to settling, not per-frame, so the mask cadence is pinned statically by sampling the reload distribution.
    key: 'andor-ports-fire-under-mask',
    behavior:
      "An ACTIVE Andor gun port fires one aimed bullet through the shared periodic gate on the fire-mask cadence; a non-ACTIVE (bombed/cascaded) port stops firing while still pinned; and the port's non-contiguous mask-47 reload is bit-exact `rng & 47`, only ever producing reload intervals in {1-16, 33-48} (masked {0-15, 32-47}) and NEVER the 16-31 band a plain `rng mod 48` would give",
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2); // settle the craft so the aimed-bullet allocator has a live target (see logram-fires-once)
      const put = slotPutter(vm);
      writeVar(vm, 'game-director-state', 'frozen');
      clearGroundBand(vm);
      writeVar(vm, 'andor-master-x', ANDOR.HOLD_X);
      writeVar(vm, 'andor-master-y', ANDOR.LATERAL_Y);
      writeVar(vm, 'tick', 0); // on-phase: tick mod FIRE_GATE_PHASE_TICKS == 0 on every manual call
      const PORT_SLOT = 14; // Scratch slot 14 -> obj 13 -> gun port (type 0x4F); JS index 13
      const i = PORT_SLOT - 1;
      const clearBullets = () => {
        for (let s = 39; s <= 57; s += 1) {
          // BULLET_SLOTS 40-58 -> JS 39-57: keep the shared pool from filling across 240 reload samples
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
      };
      const seedPort = (slotState) => {
        put('slot-type', i, 0x4f); // gun port (type 0x4F)
        put('slot-state', i, slotState);
        put('slot-fire-mask', i, 47); // the boss fire mask
        put('slot-fire-timer', i, 1); // dec -> 0 this tick -> fire
        put('slot-timer', i, 0);
        put('slot-x', i, ANDOR.HOLD_X);
        put('slot-y', i, ANDOR.LATERAL_Y);
        writeVar(vm, 'slot-index', PORT_SLOT);
      };
      // (a) an ACTIVE port fires this tick (through `update andor part`'s ACTIVE-gated fire branch).
      clearBullets();
      seedPort(1); // SLOT_ACTIVE
      writeVar(vm, 'bullet-alloc-result', 0);
      callProc(vm, 'Stage', 'update andor part');
      step(vm, 1);
      const activeFired = readVar(vm, 'bullet-alloc-result') > 0;
      // (b) a non-ACTIVE (HIT) port does NOT fire — the arcade routes a hit port to explosion, not the timer.
      clearBullets();
      seedPort(2); // SLOT_HIT
      writeVar(vm, 'bullet-alloc-result', 0);
      callProc(vm, 'Stage', 'update andor part');
      step(vm, 1);
      const deadFired = readVar(vm, 'bullet-alloc-result') > 0;
      // (c) mask-47 reload distribution: drive the fire gate directly on the ACTIVE port many times and collect
      // each reloaded interval. reload = (rng & 47) + 1, so masked = interval - 1 must never fall in 16-31.
      seedPort(1);
      const masked = [];
      for (let n = 0; n < 240; n += 1) {
        clearBullets();
        put('slot-fire-timer', i, 1); // dec -> 0 -> fire+reload this call
        writeVar(vm, 'tick', 0);
        writeVar(vm, 'slot-index', PORT_SLOT);
        callProc(vm, 'Stage', 'fire permission gate');
        step(vm, 1);
        masked.push(readVar(vm, 'slot-fire-timer')[i] - 1);
      }
      return { activeFired, deadFired, masked };
    },
    assert(obs) {
      assert.equal(obs.activeFired, true, 'an ACTIVE gun port fires an aimed bullet on the gate cadence');
      assert.equal(obs.deadFired, false, 'a non-ACTIVE (bombed/cascaded) port does not fire while still pinned');
      assert.deepEqual(
        obs.masked.filter((m) => m >= 16 && m <= 31),
        [],
        'mask-47 reload NEVER lands in the 16-31 band (bit 4 is masked out) — the non-contiguous AND is exact',
      );
      assert.ok(obs.masked.every((m) => m >= 0 && m <= 47), 'every reload is a valid rng&47 interval (masked 0-47)');
      assert.ok(obs.masked.some((m) => m <= 15), 'non-vacuous: some reloads land in the low {0-15} band');
      assert.ok(obs.masked.some((m) => m >= 32), 'non-vacuous: some reloads land in the high {32-47} band');
    },
    // Break the mask-47 recognition (`item(slot index) of slot fire mask == 47`) so the gate falls through to the
    // CONTIGUOUS `rng mod (mask+1)` = `rng mod 48` reload -> values spread uniformly 0-47, landing in the
    // forbidden 16-31 band -> the non-contiguous-mask assertion bites. (The port still fires, isolating the bug
    // to the mask arithmetic.)
    negativeMutation: (p) => mutate.changeListItemEqualsOperand(p, 'Stage', 'slot fire mask', 47, 999),
  },
  {
    // BOSS-02 / andor.defenses (#95): the nine armor plates are born the ANDOR_ARMOR_IMMUNE sentinel state (NOT
    // SLOT_ACTIVE) — handle_41..49 each set `_STATE=3 | indestructible`, xevious_main.68k:5771-5983 — so the
    // shared ground detector's `== SLOT_ACTIVE` gate skips them: a bomb dead on an armor plate scores NOTHING and
    // never marks it struck, while the SAME bomb on the SAME cell destroys an ACTIVE core for 4,000 (the detector
    // is live). Mirrors garu-base-is-indestructible: the control proves it is the sentinel, not a dead detector.
    key: 'andor-armor-is-immune',
    behavior:
      'A bomb dead on an Andor armor plate (the ANDOR_ARMOR_IMMUNE sentinel state, not ACTIVE) scores NOTHING and never marks the plate struck, while the SAME bomb on the SAME cell destroys+scores an ACTIVE core for 4,000 — so the immunity is the plate sentinel failing the detector ACTIVE gate, measured against a genuinely live detector',
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = slotPutter(vm);
      writeVar(vm, 'game-director-state', 'frozen');
      // (a) an armor plate (immune sentinel) dead on the bomb target scores nothing.
      clearGroundBand(vm);
      put('slot-type', 14, 0x41); // armor plate type (obj 1), placed in a ground slot for the shared cell
      put('slot-state', 14, 3); // ANDOR_ARMOR_IMMUNE sentinel (!= SLOT_ACTIVE 1)
      put('slot-pts', 14, 21); // would be worth 4000 IF scored — proves the gate, not a zero-pts accident
      put('slot-x', 14, 5120);
      put('slot-y', 14, 4096);
      put('slot-x', 32, 5120); // locked bomb target on the plate cell
      put('slot-y', 32, 4096);
      const armorScore0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const armorDelta = readVar(vm, 'eco-score') - armorScore0;
      const armorState = readVar(vm, 'slot-state')[14];
      // (b) live control: an ACTIVE core on the identical cell DOES score 4000, so the zero above is real immunity.
      clearGroundBand(vm);
      put('slot-type', 14, 74); // core
      put('slot-state', 14, 1); // ACTIVE
      put('slot-pts', 14, 21);
      put('slot-x', 14, 5120);
      put('slot-y', 14, 4096);
      put('slot-x', 32, 5120);
      put('slot-y', 32, 4096);
      const coreScore0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const coreDelta = readVar(vm, 'eco-score') - coreScore0;
      return { armorDelta, armorState, coreDelta, award: readVar(vm, 'eco-value-table')[20] };
    },
    assert(obs) {
      assert.equal(obs.armorDelta, 0, 'a bomb dead on an armor plate scores NOTHING (immune sentinel fails the ACTIVE gate)');
      assert.equal(obs.armorState, 3, 'the armor plate is never marked struck — it keeps its immune sentinel');
      assert.equal(obs.award, 4000, 'the control core is worth its 4,000-pt value-table entry');
      assert.equal(obs.coreDelta, obs.award, 'control: the SAME bomb on the SAME cell destroys+scores an ACTIVE core');
    },
    // Empty the ground detector: the control core no longer scores -> the control assertion fails, proving the
    // armor zero is measured against a live detector (not a dead seed). Mirrors garu-base-is-indestructible.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'check ground hit'),
  },
  {
    // BOSS-03 / andor.core-destruction (#96): bombing the core (ACTIVE, worth 4,000) scores 4,000 once through
    // the shared ground detector and marks it HIT; the core then bursts in place under `update andor part` and,
    // when its burst finishes (floor(slot timer / 8) >= EXPLODE_COSTUME_COUNT, i.e. timer >= 64), CONVERTS in
    // place to the indestructible fly-up Bragza (ANDOR_BRAGZA_TYPE + immune sentinel) — faithful to
    // andor_genesis_core_hit waiting for `_STATE==4` before the conversion (xevious_main.68k:5475-5491).
    key: 'andor-core-bomb-scores-and-destroys',
    behavior:
      "Bombing the Andor core (ACTIVE, worth 4,000) scores exactly 4,000 once through the shared ground detector and marks the core HIT; a second bomb on the HIT core never re-scores; the core then bursts in place and, when its burst finishes, CONVERTS to the indestructible fly-up Bragza (type ANDOR_BRAGZA_TYPE, immune sentinel), never re-bombable",
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = slotPutter(vm);
      writeVar(vm, 'game-director-state', 'frozen');
      clearGroundBand(vm);
      writeVar(vm, 'andor-master-x', ANDOR.HOLD_X);
      writeVar(vm, 'andor-master-y', ANDOR.LATERAL_Y);
      const CORE = 14; // Scratch slot 15 -> JS index 14 (the core, obj 14)
      put('slot-type', CORE, 74); // ANDOR_CORE_TYPE
      put('slot-state', CORE, 1); // ACTIVE (bombable)
      put('slot-pts', CORE, 21); // 1-based value-table position of 4000
      put('slot-x', CORE, 5120);
      put('slot-y', CORE, 4096);
      put('slot-x', 32, 5120); // locked bomb target on the core cell
      put('slot-y', 32, 4096);
      const award = readVar(vm, 'eco-value-table')[20]; // position 21 -> JS index 20 = 4000
      const score0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const scoreDelta = readVar(vm, 'eco-score') - score0;
      const hitState = readVar(vm, 'slot-state')[CORE];
      // A second bomb on the now-HIT core must not re-score.
      put('slot-x', 32, 5120);
      put('slot-y', 32, 4096);
      const reScore0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const reScoreDelta = readVar(vm, 'eco-score') - reScore0;
      // Burst then convert: drive `update andor part` on the core; the burst clock climbs 2/tick, and the core
      // converts on the tick floor(timer/8) >= 8 (timer 64 -> 32 ticks).
      put('slot-timer', CORE, 0); // the detector zeroed the burst clock on the hit tick
      writeVar(vm, 'slot-index', 15); // Scratch 1-based core slot
      const snaps = [];
      for (let t = 0; t < 32; t += 1) {
        callProc(vm, 'Stage', 'update andor part');
        step(vm, 1);
        snaps.push({
          type: readVar(vm, 'slot-type')[CORE],
          state: readVar(vm, 'slot-state')[CORE],
          timer: readVar(vm, 'slot-timer')[CORE],
        });
      }
      return { award, scoreDelta, hitState, reScoreDelta, snaps };
    },
    assert(obs) {
      assert.equal(obs.award, 4000, 'the core is worth its 4,000-pt value-table entry');
      assert.equal(obs.scoreDelta, obs.award, 'bombing the core scores exactly 4,000 once (shared ground detector)');
      assert.equal(obs.hitState, 2, 'the bombed core is marked HIT (state 2), so it cannot re-score');
      assert.equal(obs.reScoreDelta, 0, 'a second bomb on the HIT core scores nothing');
      const mid = obs.snaps[30]; // tick 31: timer 62, still bursting as the core
      assert.equal(mid.type, 74, 'mid-burst the core is still the core (type 0x4A)');
      assert.equal(mid.state, 2, 'mid-burst the core is still HIT (bursting)');
      assert.equal(mid.timer, 62, 'the burst clock counts 2 frames/tick');
      const done = obs.snaps[31]; // tick 32: timer reaches 64 -> burst finishes -> convert
      assert.equal(done.type, 76, 'the finished core CONVERTS to the fly-up Bragza (ANDOR_BRAGZA_TYPE 0x4C)');
      assert.equal(done.state, 3, 'the converted Bragza carries the immune sentinel (never re-bombable)');
    },
    // Empty the shared ground detector: the core never scores, never goes HIT, so it never bursts or converts ->
    // the 4,000 award, the HIT mark, and the Bragza conversion all go red.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'check ground hit'),
  },
  {
    // BOSS-03 / andor.core-destruction (#96): the core-death cascade. The arcade cascade is a PER-PORT poll: each
    // gun port's own handler tests `cmp #3,(core _STATE); jeq <xx>_gun_port_hit` (5523/5574/5625/5676) and routes
    // ITSELF to its explosion, staying `_STATE=2` (active/bombable) throughout the burst. So `update andor master`
    // (dispatched LAST) does NOT flip the port states — on core HIT it only flashes the shared colour and latches
    // the destroyed clock ONCE. The cascade proper lives in `update andor part`: an ACTIVE port whose core is HIT
    // bursts (its explosion clock advances) while remaining SLOT_ACTIVE, awarding nothing on its own.
    key: 'andor-core-cascades-ports',
    behavior:
      "When the core is HIT, `update andor master` flashes the shared colour and latches the destroyed clock ONCE without flipping any port state and without scoring; the per-port cascade lives in `update andor part`, where an ACTIVE gun port whose core is HIT advances its own explosion clock (bursts) while staying SLOT_ACTIVE (bombable), and a later master tick advances the destroyed departure rather than re-latching or re-scoring",
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = slotPutter(vm);
      writeVar(vm, 'game-director-state', 'frozen');
      clearGroundBand(vm);
      writeVar(vm, 'andor-master-x', ANDOR.HOLD_X);
      writeVar(vm, 'andor-master-y', ANDOR.LATERAL_Y);
      writeVar(vm, 'andor-genesis-end-flag', 0);
      writeVar(vm, 'andor-destroyed-timer', 0);
      // core slot 15 (JS 14) HIT; ports slots 11-14 (JS 10-13) all still ACTIVE with clean burst clocks.
      put('slot-type', 14, 74);
      put('slot-state', 14, 2); // core HIT (bombed this tick or a prior one)
      const PORTS = [10, 11, 12, 13]; // JS indices for Scratch slots 11-14 (the four gun ports)
      const portTypes = [0x52, 0x51, 0x50, 0x4f];
      PORTS.forEach((js, k) => {
        put('slot-type', js, portTypes[k]);
        put('slot-state', js, 1); // ACTIVE
        put('slot-timer', js, 0);
      });
      writeVar(vm, 'slot-index', 16); // the master slot
      const score0 = readVar(vm, 'eco-score');
      // (a) master tick: latch + flash, NO port flip, NO score.
      callProc(vm, 'Stage', 'update andor master');
      step(vm, 1);
      const masterPortStates = PORTS.map((js) => readVar(vm, 'slot-state')[js]);
      const cascadeScore = readVar(vm, 'eco-score') - score0;
      const destroyedTimer1 = readVar(vm, 'andor-destroyed-timer');
      const colour = readVar(vm, 'andor-genesis-colour');
      // (b) per-port cascade: drive `update andor part` on an ACTIVE port while the core is HIT -> it bursts
      // (explosion clock advances) while staying SLOT_ACTIVE (bombable). Core is still HIT (the master never
      // converts it — that is the part proc's finished-burst job, not exercised here).
      put('slot-state', 10, 1); // ACTIVE
      put('slot-timer', 10, 0);
      writeVar(vm, 'slot-index', 11); // Scratch slot of JS index 10
      callProc(vm, 'Stage', 'update andor part');
      step(vm, 1);
      const portAfterState = readVar(vm, 'slot-state')[10];
      const portAfterTimer = readVar(vm, 'slot-timer')[10];
      // (c) fires once: a later master tick advances the destroyed departure (clock > 1), not re-latch or score.
      writeVar(vm, 'slot-index', 16);
      callProc(vm, 'Stage', 'update andor master');
      step(vm, 1);
      const destroyedTimer2 = readVar(vm, 'andor-destroyed-timer');
      const scoreAfter2 = readVar(vm, 'eco-score') - score0;
      return {
        masterPortStates,
        cascadeScore,
        destroyedTimer1,
        destroyedTimer2,
        colour,
        portAfterState,
        portAfterTimer,
        scoreAfter2,
      };
    },
    assert(obs) {
      assert.deepEqual(obs.masterPortStates, [1, 1, 1, 1], 'the master does NOT flip the port states — the cascade is a per-port poll, so every port stays SLOT_ACTIVE');
      assert.equal(obs.cascadeScore, 0, 'the destruction awards NOTHING (only a direct bomb scores a port)');
      assert.equal(obs.destroyedTimer1, 1, 'the destroyed clock latches to 1 on the core-death tick');
      assert.equal(obs.colour, 29, 'the shared colour flashes to the destroyed flash value (0x1d)');
      assert.equal(obs.portAfterState, 1, 'a core-cascaded ACTIVE port stays SLOT_ACTIVE (bombable for its 1,000) while it bursts');
      assert.ok(obs.portAfterTimer > 0, 'the cascaded port advances its own explosion clock (it is bursting)');
      assert.ok(obs.destroyedTimer2 > 1, 'the sequence fires ONCE: a later tick advances the destroyed departure, it does not re-latch');
      assert.equal(obs.scoreAfter2, 0, 'no score is awarded across the whole destruction (cascade scores nothing)');
    },
    // Graft a score write into the master proc so the destruction appears to award points -> the "awards NOTHING"
    // assertion bites (an omission invariant: the correct behaviour is that no score is written, so the biting
    // negative must GRAFT the forbidden write). 'score' is the actual stage score variable.
    negativeMutation: (p) => mutate.graftVariableSetOnProc(p, 'Stage', 'update andor master', 'score', 999),
  },
  {
    // BOSS-02/03 (#95/#96): the core-death FRAME is source-exact. In the arcade each gun port polls the core
    // (`cmp #3,(core _STATE)`, 5523) BEFORE its fire jsr (5533), so on the frame the core dies a port diverts to
    // its explosion and NO extra volley leaves (DH-1). And the cascaded port stays `_STATE=2` throughout that
    // explosion (only gun_port_explosion_finished, 5721, sets state 4), so a bomb landing on it mid-burst still
    // scores its 1,000 (DH-2). Both were wrong under the old master-driven cascade; this pins the fixed model.
    key: 'andor-death-tick-source-exact',
    behavior:
      "On the core-death frame a gun port fires NO extra volley (its fire is suppressed while the core is HIT — the per-port core poll precedes the fire, DH-1), proven against a live control where the same ACTIVE port with the core still ACTIVE does fire; and a core-cascaded port that is mid-burst but still SLOT_ACTIVE is bombable for its full 1,000 (DH-2)",
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = slotPutter(vm);
      writeVar(vm, 'game-director-state', 'frozen');
      const PORT = 10; // JS index of a gun port slot (Scratch slot 11)
      const CORE = 14; // JS index of the core slot (Scratch slot 15)
      const clearBullets = () => {
        for (let s = 39; s <= 57; s += 1) {
          put('slot-type', s, 0);
          put('slot-state', s, 0);
        }
      };
      const seedFiringPort = () => {
        put('slot-type', PORT, 0x52);
        put('slot-state', PORT, 1); // ACTIVE
        put('slot-fire-mask', PORT, 47);
        put('slot-fire-timer', PORT, 1); // dec -> 0 this call -> would fire
        put('slot-timer', PORT, 0); // not yet bursting
        put('slot-x', PORT, 5120);
        put('slot-y', PORT, 4096);
        writeVar(vm, 'tick', 0); // on-phase for the fire gate
        writeVar(vm, 'slot-index', PORT + 1);
      };
      // (DH-1a) core HIT -> the port must NOT fire on the death frame.
      clearGroundBand(vm);
      put('slot-type', CORE, 74);
      put('slot-state', CORE, 2); // core HIT
      seedFiringPort();
      clearBullets();
      writeVar(vm, 'bullet-alloc-result', 0);
      callProc(vm, 'Stage', 'update andor part');
      step(vm, 1);
      const firedOnDeathTick = readVar(vm, 'bullet-alloc-result') > 0;
      const portStateOnDeathTick = readVar(vm, 'slot-state')[PORT]; // still ACTIVE (cascading, not flipped)
      // (DH-1b) live control: core ACTIVE (not hit) -> the SAME port DOES fire, proving the suppression is real.
      clearGroundBand(vm);
      put('slot-type', CORE, 74);
      put('slot-state', CORE, 1); // core ACTIVE
      seedFiringPort();
      clearBullets();
      writeVar(vm, 'bullet-alloc-result', 0);
      callProc(vm, 'Stage', 'update andor part');
      step(vm, 1);
      const firedWithCoreAlive = readVar(vm, 'bullet-alloc-result') > 0;
      // (DH-2) a cascaded port that is still ACTIVE but mid-burst is bombable for its full 1,000.
      clearGroundBand(vm);
      put('slot-type', CORE, 74);
      put('slot-state', CORE, 2); // core HIT
      put('slot-type', PORT, 0x52);
      put('slot-state', PORT, 1); // ACTIVE (cascading)
      put('slot-pts', PORT, 17); // 1-based value-table position of 1,000
      put('slot-timer', PORT, 20); // mid-burst
      put('slot-x', PORT, 5120);
      put('slot-y', PORT, 4096);
      put('slot-x', 32, 5120); // locked bomb target on the port cell
      put('slot-y', 32, 4096);
      const portAward = readVar(vm, 'eco-value-table')[16]; // position 17 -> JS index 16
      const s0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const bombDelta = readVar(vm, 'eco-score') - s0;
      const portStateAfterBomb = readVar(vm, 'slot-state')[PORT];
      return {
        firedOnDeathTick,
        portStateOnDeathTick,
        firedWithCoreAlive,
        portAward,
        bombDelta,
        portStateAfterBomb,
      };
    },
    assert(obs) {
      assert.equal(obs.firedOnDeathTick, false, 'DH-1: a gun port fires NO extra volley on the core-death frame (fire suppressed while the core is HIT)');
      assert.equal(obs.portStateOnDeathTick, 1, 'the cascading port stays SLOT_ACTIVE on the death frame (not flipped to HIT)');
      assert.equal(obs.firedWithCoreAlive, true, 'live control: the SAME ACTIVE port with the core still ACTIVE DOES fire — the suppression is real, not a dead gate');
      assert.equal(obs.portAward, 1000, 'a gun port is worth its 1,000-pt value-table entry');
      assert.equal(obs.bombDelta, 1000, 'DH-2: a core-cascaded port that is still ACTIVE (mid-burst) is bombable for its full 1,000');
      assert.equal(obs.portStateAfterBomb, 2, 'the directly-bombed cascaded port is now marked HIT (scored)');
    },
    // Break the core-state poll (`slot state == SLOT_HIT`, value 2) so `not(core is hit)` is always true and the
    // fire is no longer suppressed on the core-death frame -> DH-1 (firedOnDeathTick) bites. The live control
    // (core ACTIVE) still fires either way, so the failing assertion is precisely the death-frame suppression.
    negativeMutation: (p) => mutate.changeListItemEqualsOperand(p, 'Stage', 'slot state', 2, 999),
  },
  {
    // BOSS-03 / andor.core-destruction (#96): the destroyed departure tears the composite DOWN. Once the destroyed
    // clock is latched, `update andor master` sinks the wreck at 2x scroll and, when it clears the field
    // (andor_genesis_destroyed -> remove_andor_genesis, 5409-5443), runs a TYPE-AWARE teardown that frees every
    // boss slot (armor/ports/core/master) EXCEPT a slot that now holds the fly-up Bragza — the converted core keeps
    // flying (F1). Proven live by driving the departure to completion with a co-seeded Bragza that must survive.
    key: 'andor-destroyed-departure-tears-down',
    behavior:
      'Once the destroyed clock is latched, `update andor master` sinks the wreck off the field and then frees every boss slot (armor, gun ports, core, master) — but a slot that has converted to the fly-up Bragza is spared by the type-aware teardown, so the converted core keeps flying while the rest of the composite is torn down',
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = slotPutter(vm);
      writeVar(vm, 'game-director-state', 'frozen');
      clearGroundBand(vm);
      writeVar(vm, 'andor-master-x', ANDOR.HOLD_X);
      writeVar(vm, 'andor-master-y', ANDOR.LATERAL_Y);
      writeVar(vm, 'andor-genesis-end-flag', 0);
      // Seed the whole composite: armor JS 1-9 (immune sentinel), ports JS 10-13, core JS 14, master JS 15.
      for (let js = 1; js <= 9; js += 1) {
        put('slot-type', js, 0x40 + js); // armor types 0x41..0x49
        put('slot-state', js, 3); // ANDOR_ARMOR_IMMUNE sentinel
      }
      const portTypes = [0x52, 0x51, 0x50, 0x4f];
      portTypes.forEach((t, k) => {
        put('slot-type', 10 + k, t);
        put('slot-state', 10 + k, 1);
      });
      // The core slot (JS 14) has already CONVERTED to the fly-up Bragza — it must SURVIVE the teardown while the
      // rest of the composite is freed. (The teardown iterates the boss slots JS 1..15; a spare-slot Bragza outside
      // that range would never be touched, so the guard is only meaningfully exercised on the core slot itself.)
      const CORE = 14;
      put('slot-type', CORE, 76); // ANDOR_BRAGZA_TYPE (converted core, still flying)
      put('slot-state', CORE, 3); // immune sentinel
      put('slot-type', 15, 75); // master
      put('slot-state', 15, 1);
      // Latch the destroyed departure and run it to completion.
      writeVar(vm, 'andor-destroyed-timer', 1);
      writeVar(vm, 'slot-index', 16);
      let torn = false;
      for (let t = 0; t < 200; t += 1) {
        callProc(vm, 'Stage', 'update andor master');
        step(vm, 1);
        if (readVar(vm, 'slot-type')[15] === 0) {
          torn = true;
          break;
        }
      }
      // The boss slots EXCEPT the converted core (JS 14): armor JS 1-9, ports JS 10-13, master JS 15.
      const otherSlots = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15];
      const otherTypes = otherSlots.map((js) => readVar(vm, 'slot-type')[js]);
      return {
        torn,
        otherTypes,
        bragzaType: readVar(vm, 'slot-type')[CORE],
        bragzaState: readVar(vm, 'slot-state')[CORE],
        endFlag: readVar(vm, 'andor-genesis-end-flag'),
        destroyedTimer: readVar(vm, 'andor-destroyed-timer'),
      };
    },
    assert(obs) {
      assert.equal(obs.torn, true, 'the destroyed departure reaches the teardown (the master slot is freed) within its sink');
      assert.deepEqual(obs.otherTypes, new Array(14).fill(0), 'every boss slot except the converted core (armor, gun ports, master) is freed by the teardown');
      assert.equal(obs.bragzaType, 76, 'the converted-core Bragza slot is SPARED — it keeps flying while the composite is torn down');
      assert.equal(obs.bragzaState, 3, 'the spared Bragza keeps its immune sentinel state');
      assert.equal(obs.endFlag, 0, 'the teardown consumes the end flag');
      assert.equal(obs.destroyedTimer, 0, 'the teardown clears the destroyed clock');
    },
    // Corrupt the Bragza-spare guard so the teardown clobbers the converted core too -> the "Bragza survives"
    // assertion bites. changeListItemEqualsOperand rewrites the `slot type[core] == ANDOR_BRAGZA_TYPE` guard.
    negativeMutation: (p) => mutate.changeListItemEqualsOperand(p, 'Stage', 'slot type', 76, 999),
  },
  {
    // BOSS-03 / andor.core-destruction (#96): the converted core flies as the indestructible Bragza
    // (handle_Bragza, xevious_main.68k:5493-5504: `_dX=0xffd0` = up the scroll axis, `_dY` cleared). `update
    // andor bragza` moves the slot UP by exactly ANDOR_BRAGZA_STEP each tick (slot x decreasing, slot y held) and
    // culls it once it clears the top of the field. The 4-frame costume + colour cycle are cosmetic (render arm).
    key: 'andor-bragza-flies-up',
    behavior:
      'The converted core flies as the indestructible Bragza: `update andor bragza` moves its slot UP the scroll axis by exactly ANDOR_BRAGZA_STEP each tick (slot x decreasing, slot y held constant) and culls the slot once it clears the top of the field',
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = slotPutter(vm);
      writeVar(vm, 'game-director-state', 'frozen');
      clearGroundBand(vm);
      const B = 14; // any ground slot; use JS index 14 (Scratch slot 15)
      put('slot-type', B, 76); // ANDOR_BRAGZA_TYPE
      put('slot-state', B, 3); // immune sentinel
      put('slot-x', B, 2000);
      put('slot-y', B, 4096);
      writeVar(vm, 'slot-index', 15);
      const xs = [];
      for (let t = 0; t < 4; t += 1) {
        callProc(vm, 'Stage', 'update andor bragza');
        step(vm, 1);
        xs.push({ x: readVar(vm, 'slot-x')[B], y: readVar(vm, 'slot-y')[B] });
      }
      // Cull: place it one step short of the cull row so the next tick's fly-up carries it past ANDOR_BRAGZA_CULL_X
      // (-2048) and frees the slot — the Bragza's only exit.
      put('slot-type', B, 76);
      put('slot-state', B, 3);
      put('slot-x', B, -2048 + 96 - 1); // ANDOR_BRAGZA_CULL_X + ANDOR_BRAGZA_STEP - 1
      put('slot-y', B, 4096);
      writeVar(vm, 'slot-index', 15);
      callProc(vm, 'Stage', 'update andor bragza');
      step(vm, 1);
      return { xs, culledType: readVar(vm, 'slot-type')[B], culledState: readVar(vm, 'slot-state')[B] };
    },
    assert(obs) {
      assert.deepEqual(
        obs.xs.map((s) => s.x),
        [1904, 1808, 1712, 1616],
        'the Bragza flies UP by exactly 96 units/tick (slot x decreasing)',
      );
      assert.ok(obs.xs.every((s) => s.y === 4096), 'the Bragza holds its lateral position (slot y constant)');
      assert.equal(obs.culledType, 0, 'once it clears the top of the field the Bragza slot is culled (type 0)');
      assert.equal(obs.culledState, 0, 'the culled Bragza slot is freed (state 0) so it can be reused');
    },
    // Empty the Bragza update: the slot never flies or culls -> the up-flight and cull assertions go red.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update andor bragza'),
  },
  {
    // BOSS-03 / andor.core-destruction (#96): the preserved arcade shell-slot leftover bug. The invisible master
    // (obj 0x0F) is forced ACTIVE at spawn with its `_PTS` NEVER initialised (handle_4B 5378-5385 + sub_2_fn_20
    // 546-549), and it shares the core's cell — so a single bomb on the core awards BOTH the core's 4,000 AND the
    // master's stale leftover value. The port pins the master slot onto the core's cell each tick (update andor
    // master) with its `slot pts` left un-seeded, reproducing the double award; without the tracking the master
    // stays off-cell and only the core scores.
    key: 'andor-shell-slot-leftover-award',
    behavior:
      'The preserved arcade shell-slot bug: the invisible master is ACTIVE with its `slot pts` never initialized, and `update andor master` pins its slot onto the core cell each tick — so ONE bomb on the core awards BOTH the core 4,000 AND the master stale leftover value (here 200), once; without the per-tick tracking the master stays off-cell and only the core scores',
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      step(vm, 2);
      const put = slotPutter(vm);
      writeVar(vm, 'game-director-state', 'frozen');
      clearGroundBand(vm);
      writeVar(vm, 'andor-genesis-end-flag', 0);
      writeVar(vm, 'andor-destroyed-timer', 0);
      const cellX = ANDOR.HOLD_X;
      const cellY = ANDOR.LATERAL_Y;
      writeVar(vm, 'andor-master-x', cellX);
      writeVar(vm, 'andor-master-y', cellY);
      // Core (slot 15 / JS 14): ACTIVE, worth 4000, sitting on the shared cell.
      put('slot-type', 14, 74);
      put('slot-state', 14, 1);
      put('slot-pts', 14, 21); // value-table position of 4000
      put('slot-x', 14, cellX);
      put('slot-y', 14, cellY);
      // Master (slot 16 / JS 15): ACTIVE with a KNOWN stale leftover pts (value-table position 8 -> 200), seeded
      // OFF the cell so only the per-tick tracking can co-locate it with the core.
      put('slot-type', 15, 75);
      put('slot-state', 15, 1);
      put('slot-pts', 15, 8); // stale leftover -> 200
      put('slot-x', 15, 99999);
      put('slot-y', 15, 99999);
      writeVar(vm, 'slot-index', 16);
      callProc(vm, 'Stage', 'update andor master'); // pins the master slot onto the anchor (= the core cell)
      step(vm, 1);
      const masterX = readVar(vm, 'slot-x')[15];
      // Bomb the shared cell.
      put('slot-x', 32, cellX);
      put('slot-y', 32, cellY);
      const coreValue = readVar(vm, 'eco-value-table')[20]; // 4000
      const staleValue = readVar(vm, 'eco-value-table')[7]; // 200
      const score0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const delta = readVar(vm, 'eco-score') - score0;
      // Once: a second bomb (both slots now HIT) awards nothing more.
      put('slot-x', 32, cellX);
      put('slot-y', 32, cellY);
      const reScore0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const reDelta = readVar(vm, 'eco-score') - reScore0;
      return { masterX, cellX, coreValue, staleValue, delta, reDelta };
    },
    assert(obs) {
      assert.equal(obs.masterX, obs.cellX, 'the master slot is pinned onto the core cell each tick');
      assert.equal(obs.coreValue, 4000, 'the core is worth 4,000');
      assert.equal(obs.staleValue, 200, 'the seeded stale leftover value is 200');
      assert.equal(
        obs.delta,
        obs.coreValue + obs.staleValue,
        'one core bomb awards BOTH the core 4,000 AND the stale master value (the shell bug) = 4,200',
      );
      assert.equal(obs.reDelta, 0, 'the shell award happens once — a second bomb scores nothing (both slots now HIT)');
    },
    // Empty the master update: the master slot is never tracked onto the core cell, so it stays off-cell and the
    // detector never awards its stale value -> the double-award (4,200) assertion bites.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update andor master'),
  },
  {
    // BOSS-02/03 debug-summon band protection (operator playtest fix, 2026-09-28: "it retreated in pieces").
    // A debug-G-summoned Andor Genesis drops into whatever area is live, whose schedule may still have pending
    // add_ground_object / add_domogram records. The original suppression withheld those stamps only while the G
    // key was HELD -- but the boss DEPARTS after G is released (its dismiss is a fresh G press, then the master
    // retreats over the following G-up ticks), so the resuming schedule stamps landed in the boss's own ground
    // slots (Scratch 2..16) and overwrote the composite one plate at a time as it retreated. The fix ALSO
    // withholds every schedule ground stamp while the invisible master occupies its slot (andor_boss_present),
    // for the boss's whole lifecycle -- hold through retreat through teardown. Real play is untouched: in areas
    // 4/9/14 every add_ground_object record fires above andor_genesis_start and has scrolled off before the boss
    // arms, so no schedule ground stamp is ever live while the master is present. Driven LIVE (real step() runs
    // _consume_schedule): with the boss present the area-1 schedule scrolls a stream of ground records past the
    // band and NONE lands in it; without the guard (negative) the band is cannibalized from the fourth step on.
    key: 'andor-debug-summon-band-not-cannibalized-by-schedule',
    behavior:
      'While a debug-summoned Andor Genesis master occupies its slot, the area schedule\'s own ground stamps (add_ground_object / add_domogram) are withheld from the entire ground band -- so the part slots freed as the composite retreats are never refilled by a foreign ground type. Modelled mid-retreat (master present, its 14 part slots already empty): the live area-1 schedule tries to place its own ground records into those free slots every tick and the boss-present guard withholds every one; with the guard removed the schedule floods the band within a few ticks',
    playtestStep: 8,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = slotPutter(vm);
      clearGroundBand(vm);
      // Model the boss mid-retreat: the invisible master still occupies its slot (the boss-present guard is live
      // for the WHOLE lifecycle -- descend, hold, retreat, teardown) while its 14 part slots have already been
      // freed. Leaving those part slots EMPTY is the deterministic form of the shipped bug's trigger: the live
      // area-1 schedule below has real ground records to place and free band slots to place them in, so with the
      // guard OFF it floods the band every tick (58 stray stamps from step 3 in area 1). Holding (end flag 0)
      // keeps the master stable so the guard stays live across every step -- no pacing-fragile wait for a
      // retreating slot to free at exactly the tick a schedule record happens to fire (that race passed under one
      // node runtime and not another; this models the same guarantee without depending on the schedule's timing).
      put('slot-type', ANDOR.BASE_SLOT + 14, ANDOR.MASTER_TYPE); // master at JS 15 (arcade obj 15)
      put('slot-state', ANDOR.BASE_SLOT + 14, 1);
      writeVar(vm, 'andor-master-x', ANDOR.HOLD_X);
      writeVar(vm, 'andor-master-y', ANDOR.LATERAL_Y);
      writeVar(vm, 'andor-genesis-end-flag', 0);
      writeVar(vm, 'andor-destroyed-timer', 0);
      const andorTypes = new Set([...ANDOR_PART_TYPES_BY_OBJ, 0x4c]); // the 15 part types + Bragza (0x4C)
      const foreign = [];
      let masterPresentEachStep = true;
      // Keep the passive craft alive + isolate the ground band from the port bullets: clear the enemy-bullet
      // (JS 39-57) and flying (JS 58-63) bands each frame before stepping.
      const clearTraffic = () => {
        const t = readVar(vm, 'slot-type');
        const s = readVar(vm, 'slot-state');
        for (let js = 39; js <= 63; js += 1) {
          t[js] = 0;
          s[js] = 0;
        }
      };
      // Step live so the schedule genuinely runs beneath the boss; watch the 14 freed part slots (JS 1-14) for any
      // foreign ground type, and confirm the master held its own slot every tick (so the guard was live throughout).
      for (let k = 0; k < 20; k += 1) {
        clearTraffic();
        step(vm, 1);
        const t = readVar(vm, 'slot-type');
        if (t[ANDOR.BASE_SLOT + 14] !== ANDOR.MASTER_TYPE) masterPresentEachStep = false;
        for (let n = 1; n <= 14; n += 1) {
          const v = t[ANDOR.BASE_SLOT + n - 1];
          if (v !== 0 && !andorTypes.has(v)) foreign.push({ step: k, obj: n, type: v });
        }
      }
      const stateAfter = readVar(vm, 'game-director-state');
      return { foreign, stateAfter, masterPresentEachStep };
    },
    assert(obs) {
      assert.equal(
        obs.stateAfter,
        'playing',
        'the game keeps playing while the boss holds, so the live schedule genuinely ran under it',
      );
      assert.ok(
        obs.masterPresentEachStep,
        'the invisible master occupied its slot on every step, so the boss-present guard was live throughout -- the band stayed clean because the guard withheld the schedule, not because the master had already gone',
      );
      assert.deepEqual(
        obs.foreign,
        [],
        'no foreign (non-Andor) ground type ever lands in the freed part band while the master is present -- the schedule\'s own ground stamps are withheld for the boss\'s whole lifecycle',
      );
    },
    // Disable the boss-present guard (item(16) of (slot type) == 75 -> == 999) so the schedule's ground stamps
    // resume into the band while the master is present -> the freed band is flooded -> the band-clean (and
    // master-held) assertions bite.
    negativeMutation: (p) => mutate.changeListItemEqualsOperand(p, 'Stage', 'slot type', 75, 999),
  },
  {
    // SEC-02 / secrets.bonus-flag (#91): reveal-scores-once + fly-over collection (proximity, not a weapon).
    key: 'bonus-flag-revealed-by-bomb-scores-once-then-collected-by-flyover-not-a-weapon',
    behavior:
      'A hidden (ACTIVE) Bonus Flag bombed scores its 1,000 ONCE via the shared ground detector and is REVEALED (held HIT, so a second bomb never re-scores it); a revealed flag is then collected by the craft FLYING OVER it (proximity, NOT a weapon) — a bomb on a revealed flag does not collect it, and a flag the craft is not over is not collected',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      const BONUS_FLAG_TYPE = 84; // 0x54
      const BONUS_FLAG_PTS = 17; // 1-based value-table position of 1,000
      const SLOT_ACTIVE = 1;
      const SLOT_HIT = 2;
      const FLAG_HIDDEN = 0;
      const FLAG_REVEALED = 1;
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      writeVar(vm, 'slot-index', 16);
      // Observe collection through the POINTS award arm (score += 10,000), so set the DIP marker to points.
      writeVar(vm, 'eco-flag-awards-craft', 0);
      const aimBomb = (x, y) => {
        put('slot-x', 32, x);
        put('slot-y', 32, y);
      };
      const flagValue = readVar(vm, 'eco-value-table')[BONUS_FLAG_PTS - 1];
      // REVEAL: bomb the hidden flag. The shared ACTIVE-only detector scores its 1,000 and marks it HIT.
      put('slot-type', 15, BONUS_FLAG_TYPE);
      put('slot-state', 15, SLOT_ACTIVE);
      put('slot-flag', 15, FLAG_HIDDEN);
      put('slot-pts', 15, BONUS_FLAG_PTS);
      put('slot-x', 15, 5120);
      put('slot-y', 15, 4096);
      put('slot-timer', 15, 0);
      aimBomb(5120, 4096);
      const s0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const revealScore = readVar(vm, 'eco-score') - s0;
      const stateAfterBomb = readVar(vm, 'slot-state')[15];
      // One update: HIT & HIDDEN -> flip to REVEALED, keep HIT (never ACTIVE again), scroll.
      callProc(vm, 'Stage', 'update bonus flag');
      step(vm, 1);
      const flagAfterReveal = readVar(vm, 'slot-flag')[15];
      const stateAfterReveal = readVar(vm, 'slot-state')[15];
      // A SECOND bomb on the revealed (HIT) flag must NOT re-score it. Re-aim at its scrolled cell.
      aimBomb(readVar(vm, 'slot-x')[15], readVar(vm, 'slot-y')[15]);
      const s1 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const reBombScore = readVar(vm, 'eco-score') - s1;
      // NOT COLLECTED when the craft is NOT over it: a revealed flag with the craft on a far lateral column is
      // not collected by an update tick (proximity gate — and no weapon collects it) -> it stays occupied.
      put('slot-type', 15, BONUS_FLAG_TYPE);
      put('slot-state', 15, SLOT_HIT);
      put('slot-flag', 15, FLAG_REVEALED);
      put('slot-x', 15, 2 * 256); // row 2
      put('slot-y', 15, 0); // column 0
      writeVar(vm, 'player-row', 2);
      writeVar(vm, 'player-col', 20); // craft 20 columns away laterally
      // PRES-01: the collection box compares the craft's EXACT slot position, not its cell.
      writeVar(vm, 'player-slot-x', 2 * 256);
      writeVar(vm, 'player-slot-y', 20 * 256);
      const s2 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'update bonus flag');
      step(vm, 1);
      const awayScore = readVar(vm, 'eco-score') - s2;
      const typeAfterAway = readVar(vm, 'slot-type')[15];
      // FLY OVER: the craft over the flag's cell collects it (proximity) -> the POINTS arm awards 10,000 and
      // the flag is removed. Re-seed at a fresh cell dead-on the craft.
      put('slot-type', 15, BONUS_FLAG_TYPE);
      put('slot-state', 15, SLOT_HIT);
      put('slot-flag', 15, FLAG_REVEALED);
      put('slot-x', 15, 2 * 256); // row 2
      put('slot-y', 15, 3 * 256); // column 3
      writeVar(vm, 'player-row', 2);
      writeVar(vm, 'player-col', 3); // dead-on overlap
      writeVar(vm, 'player-slot-x', 2 * 256); // PRES-01: the box reads the exact slot position
      writeVar(vm, 'player-slot-y', 3 * 256);
      const s3 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'update bonus flag');
      step(vm, 1);
      const flyoverScore = readVar(vm, 'eco-score') - s3;
      const typeAfterCollect = readVar(vm, 'slot-type')[15];
      const stateAfterCollect = readVar(vm, 'slot-state')[15];
      return {
        flagValue,
        revealScore,
        stateAfterBomb,
        flagAfterReveal,
        stateAfterReveal,
        reBombScore,
        awayScore,
        typeAfterAway,
        flyoverScore,
        typeAfterCollect,
        stateAfterCollect,
      };
    },
    assert(obs) {
      assert.equal(obs.flagValue, 1000, 'the Bonus Flag is worth its 1,000-pt value-table entry');
      assert.equal(obs.revealScore, 1000, 'bombing the hidden flag scores its 1,000 via the shared ground detector');
      assert.equal(obs.stateAfterBomb, 2, 'the bombed flag is marked HIT');
      assert.equal(obs.flagAfterReveal, 1, 'one update reveals the flag (flag -> REVEALED)');
      assert.equal(obs.stateAfterReveal, 2, 'the revealed flag is held HIT (never ACTIVE again)');
      assert.equal(obs.reBombScore, 0, 'a second bomb never re-scores the revealed flag (the reveal scores exactly once)');
      assert.equal(obs.awayScore, 0, 'a revealed flag the craft is NOT over is not collected');
      assert.equal(obs.typeAfterAway, 84, 'the un-flown-over flag stays on the field (no weapon collects it)');
      assert.equal(obs.flyoverScore, 10000, 'flying the craft OVER the revealed flag collects it (proximity) -> the points arm awards 10,000');
      assert.equal(obs.typeAfterCollect, 0, 'collection removes the flag from the field');
      assert.equal(obs.stateAfterCollect, 0, 'the collected flag slot is freed');
    },
    // Sever the Bonus Flag update: the bombed flag never reveals and a fly-over never collects -> the reveal /
    // fly-over-collection assertions go red (the shared-detector reveal score still lands, so only the update
    // clauses bite, which is the point).
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update bonus flag'),
  },
  {
    // SEC-02 / ECO-03 (#91, #92): the collection award honours the DIP choice (extra craft vs 10,000 points).
    key: 'bonus-flag-collection-award-honours-the-dip-choice',
    behavior:
      'Collecting a revealed Bonus Flag by fly-over awards the DIP-selected prize: with `flag awards craft` set it grants an EXTRA CRAFT (+1 life, no score); cleared it awards 10,000 POINTS instead (and no life) — never both',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      const BONUS_FLAG_TYPE = 84;
      const BONUS_FLAG_PTS = 17;
      const SLOT_HIT = 2;
      const FLAG_REVEALED = 1;
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      writeVar(vm, 'slot-index', 16);
      // Isolate the FLAG'S award from the incidental score-threshold bonus life: push the next-bonus threshold
      // far out of reach so the 10,000-point arm cannot trip an extend (that extend is a separate system, its
      // own scenario) — this test asks only what the flag itself grants.
      writeVar(vm, 'eco-next-bonus', 999999999);
      const seedRevealedUnderCraft = () => {
        put('slot-type', 15, BONUS_FLAG_TYPE);
        put('slot-state', 15, SLOT_HIT);
        put('slot-flag', 15, FLAG_REVEALED);
        put('slot-pts', 15, BONUS_FLAG_PTS);
        put('slot-x', 15, 2 * 256); // row 2
        put('slot-y', 15, 3 * 256); // column 3
        put('slot-timer', 15, 0);
        writeVar(vm, 'player-row', 2);
        writeVar(vm, 'player-col', 3); // dead-on overlap
        writeVar(vm, 'player-slot-x', 2 * 256); // PRES-01: the box reads the exact slot position
        writeVar(vm, 'player-slot-y', 3 * 256);
      };
      // CRAFT arm: flag awards craft -> +1 life, no score.
      writeVar(vm, 'eco-flag-awards-craft', 1);
      seedRevealedUnderCraft();
      const craft0 = readVar(vm, 'eco-craft');
      const scoreC0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'update bonus flag');
      step(vm, 1);
      const craftGain = readVar(vm, 'eco-craft') - craft0;
      const scoreOnCraftArm = readVar(vm, 'eco-score') - scoreC0;
      // POINTS arm: flag awards points -> +10,000 score, no life.
      writeVar(vm, 'eco-flag-awards-craft', 0);
      seedRevealedUnderCraft();
      const craftP0 = readVar(vm, 'eco-craft');
      const scoreP0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'update bonus flag');
      step(vm, 1);
      const scoreGain = readVar(vm, 'eco-score') - scoreP0;
      const craftOnPointsArm = readVar(vm, 'eco-craft') - craftP0;
      return { craftGain, scoreOnCraftArm, scoreGain, craftOnPointsArm };
    },
    assert(obs) {
      assert.equal(obs.craftGain, 1, 'the CRAFT arm grants exactly one extra craft');
      assert.equal(obs.scoreOnCraftArm, 0, 'the CRAFT arm awards no points (the extra life is the whole prize)');
      assert.equal(obs.scoreGain, 10000, 'the POINTS arm awards 10,000 points');
      assert.equal(obs.craftOnPointsArm, 0, 'the POINTS arm grants no extra craft (never both)');
    },
    // Break the award selector: force the `flag awards craft == 1` gate to compare against a value it never
    // holds, so the craft arm never runs -> the CRAFT-arm assertions (craft +1, no score) go red.
    negativeMutation: (p) => mutate.changeVarEqualsOperand(p, 'Stage', 'flag awards craft', 1, 999),
  },
  {
    // SEC-03 / secrets.hidden-credit (#93): bomb-to-reveal a held ~2s overlay, min score, self-removal.
    key: 'hidden-credit-bomb-reveals-a-held-overlay-then-self-removes-for-the-min-score',
    behavior:
      'The hidden Credit is invisible and shows NOTHING until bombed; a bomb scores its 10 (minimum value) and, on the next update, raises the credit-overlay signal and FREEZES the egg in place (no scroll); the overlay holds ~2s (128 frames, 2/tick), then the egg lowers the signal and removes itself',
    playtestStep: 7,
    async drive(vm) {
      assert.ok(reachPlaying(vm), 'precondition: game reaches playing');
      const put = (id, i, v) => {
        readVar(vm, id)[i] = v;
      };
      const EASTER_EGG_TYPE = 83; // 0x53
      const EASTER_EGG_PTS = 1; // _PTS==0 -> value-table position 1 = 10 (the minimum)
      const SLOT_ACTIVE = 1;
      const EGG_HIDDEN = 0;
      writeVar(vm, 'game-director-state', 'frozen');
      for (let s = 0; s < 16; s += 1) {
        put('slot-type', s, 0);
        put('slot-state', s, 0);
      }
      writeVar(vm, 'slot-index', 16);
      writeVar(vm, 'sec-easter-egg-showing', 0);
      const seedEgg = (x, y) => {
        put('slot-type', 15, EASTER_EGG_TYPE);
        put('slot-state', 15, SLOT_ACTIVE);
        put('slot-flag', 15, EGG_HIDDEN);
        put('slot-pts', 15, EASTER_EGG_PTS);
        put('slot-x', 15, x);
        put('slot-y', 15, y);
        put('slot-timer', 15, 0);
      };
      const aimBomb = (x, y) => {
        put('slot-x', 32, x);
        put('slot-y', 32, y);
      };
      const eggValue = readVar(vm, 'eco-value-table')[EASTER_EGG_PTS - 1];
      // TRIGGERS ONLY WHEN BOMBED: an un-bombed (ACTIVE) egg run through its update raises NO overlay signal
      // and just scrolls with the terrain.
      seedEgg(2 * 256, 2 * 256);
      callProc(vm, 'Stage', 'update easter egg');
      step(vm, 1);
      const signalUnbombed = readVar(vm, 'sec-easter-egg-showing');
      const xAfterActiveTick = readVar(vm, 'slot-x')[15];
      // BOMB: the shared ACTIVE-only detector scores the egg's minimum 10 and marks it HIT — showing nothing yet.
      seedEgg(5120, 4096);
      aimBomb(5120, 4096);
      const s0 = readVar(vm, 'eco-score');
      callProc(vm, 'Stage', 'check ground hit');
      step(vm, 1);
      const bombScore = readVar(vm, 'eco-score') - s0;
      const stateAfterBomb = readVar(vm, 'slot-state')[15];
      const signalAfterBomb = readVar(vm, 'sec-easter-egg-showing');
      const xBeforeReveal = readVar(vm, 'slot-x')[15];
      // REVEAL: one update raises the overlay signal and FREEZES the egg (no scroll on the hit branch).
      callProc(vm, 'Stage', 'update easter egg');
      step(vm, 1);
      const signalAfterReveal = readVar(vm, 'sec-easter-egg-showing');
      const xAfterReveal = readVar(vm, 'slot-x')[15];
      const flagAfterReveal = readVar(vm, 'slot-flag')[15];
      // HOLD: the overlay holds through its display window (128 frames, 2/tick => 64 showing ticks). After the
      // reveal the clock is 0; 63 showing ticks reach frame 126 (< 128), so the signal is still up.
      for (let t = 0; t < 63; t += 1) {
        callProc(vm, 'Stage', 'update easter egg');
        step(vm, 1);
      }
      const signalDuringHold = readVar(vm, 'sec-easter-egg-showing');
      const xDuringHold = readVar(vm, 'slot-x')[15];
      // EXPIRE: the next showing tick reaches frame 128 -> lower the signal and remove the egg.
      callProc(vm, 'Stage', 'update easter egg');
      step(vm, 1);
      const signalAfterExpire = readVar(vm, 'sec-easter-egg-showing');
      const typeAfterExpire = readVar(vm, 'slot-type')[15];
      const stateAfterExpire = readVar(vm, 'slot-state')[15];
      return {
        eggValue,
        signalUnbombed,
        xAfterActiveTick,
        bombScore,
        stateAfterBomb,
        signalAfterBomb,
        xBeforeReveal,
        signalAfterReveal,
        xAfterReveal,
        flagAfterReveal,
        signalDuringHold,
        xDuringHold,
        signalAfterExpire,
        typeAfterExpire,
        stateAfterExpire,
      };
    },
    assert(obs) {
      assert.equal(obs.eggValue, 10, 'the hidden Credit is worth the minimum 10-pt value-table entry');
      assert.equal(obs.signalUnbombed, 0, 'an un-bombed egg shows NOTHING (the overlay signal stays down)');
      assert.equal(obs.xAfterActiveTick, 2 * 256 + 32, 'an un-bombed egg just scrolls with the terrain (+32/tick)');
      assert.equal(obs.bombScore, 10, 'bombing the egg scores its minimum 10 via the shared ground detector');
      assert.equal(obs.stateAfterBomb, 2, 'the bombed egg is marked HIT');
      assert.equal(obs.signalAfterBomb, 0, 'the bomb alone does not raise the overlay (the update does, next tick)');
      assert.equal(obs.signalAfterReveal, 1, 'the reveal update raises the credit-overlay signal');
      assert.equal(obs.xAfterReveal, obs.xBeforeReveal, 'the revealed egg FREEZES in place (no scroll on the hit branch)');
      assert.equal(obs.flagAfterReveal, 1, 'the revealed egg carries the SHOWING phase');
      assert.equal(obs.signalDuringHold, 1, 'the overlay holds up through its ~2s display window');
      assert.equal(obs.xDuringHold, obs.xBeforeReveal, 'the egg stays frozen for the whole hold');
      assert.equal(obs.signalAfterExpire, 0, 'at the end of the window the overlay signal is lowered');
      assert.equal(obs.typeAfterExpire, 0, 'the egg removes itself from the field');
      assert.equal(obs.stateAfterExpire, 0, 'the removed egg slot is freed');
    },
    // Sever the egg update: a bombed egg never raises the overlay, never holds, never self-removes, and an
    // un-bombed egg never even scrolls -> the reveal / hold / removal / scroll assertions go red.
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'update easter egg'),
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
  {
    // CAB-03 (cabinet.two-player, slice 18): the `swap players` primitive — the port's
    // swap_curr_other_player (xevious_main 671-679). It exchanges every one of the 14 persistent
    // per-player fields between the current player's live vars and the inactive player's `other <x>`
    // shadow, and touches nothing else. This commit installs the proc with no trigger yet; the
    // alternation that calls it (and its CAB-03 acceptance evidence) arrive in a later commit.
    key: 'player-context-swap',
    behavior:
      '`swap players` exchanges all 14 persistent per-player fields (score, craft, next bonus, area, ai level, ground-stop row, 8 fire masks) with the inactive-player shadow and leaves the shared RNG seed untouched',
    playtestStep: 1,
    async drive(vm) {
      // The 14 persistent per-player fields as (live id, shadow id) pairs — the same set the
      // generator derives from PLAYER_CONTEXT_FIELDS. Seed each live var and its `other <x>` shadow to
      // DISJOINT sentinel ranges (live = 100+i, shadow = 200+i) so that a field left un-swapped, or one
      // whose value leaks in from a different field, is caught by that field's exact assertion. If a
      // field were ever dropped from the swap set its shadow id would vanish and readVar would hard-error
      // here — so this positive is itself the guard against a silently missed field.
      const fields = [
        ['eco-score', 'other-score'],
        ['eco-craft', 'other-craft'],
        ['eco-next-bonus', 'other-next-bonus'],
        ['area-number', 'other-area-number'],
        ['difficulty-ai-level', 'other-ai-level'],
        ['ground-stop-firing-row', 'other-ground-stop-firing-row'],
        ['fire-mask-derota', 'other-fire-mask-derota'],
        ['fire-mask-logram', 'other-fire-mask-logram'],
        ['fire-mask-zoshi', 'other-fire-mask-zoshi'],
        ['fire-mask-terrazi', 'other-fire-mask-terrazi'],
        ['fire-mask-kapi', 'other-fire-mask-kapi'],
        ['fire-mask-boza-logram', 'other-fire-mask-boza-logram'],
        ['fire-mask-domogram', 'other-fire-mask-domogram'],
        ['fire-mask-andor-genesis', 'other-fire-mask-andor-genesis'],
      ];
      // The shared RNG seed sits OUTSIDE the arcade's swapped 64-byte block (pseudo_random_seed,
      // xevious_ram 120), so a 2P game stays deterministic from one stream — swap must NOT touch it.
      const rngBefore = 4242;
      writeVar(vm, 'rng-state', rngBefore);
      fields.forEach(([liveId, shadowId], i) => {
        writeVar(vm, liveId, 100 + i);
        writeVar(vm, shadowId, 200 + i);
      });
      callProc(vm, 'Stage', 'swap players'); // warp proc — runs to completion in one step
      step(vm, 1);
      const after = fields.map(([liveId, shadowId]) => [readVar(vm, liveId), readVar(vm, shadowId)]);
      return { fields, after, rngBefore, rngAfter: readVar(vm, 'rng-state') };
    },
    assert(obs) {
      obs.fields.forEach(([liveId, shadowId], i) => {
        const [live, shadow] = obs.after[i];
        assert.equal(
          Number(live),
          200 + i,
          `swap players moves the inactive-player value into live '${liveId}'`,
        );
        assert.equal(
          Number(shadow),
          100 + i,
          `swap players moves the current value into shadow '${shadowId}'`,
        );
      });
      assert.equal(
        Number(obs.rngAfter),
        obs.rngBefore,
        'swap players leaves the shared RNG seed untouched (a 2P game stays one deterministic stream)',
      );
    },
    // Pin the live `score` write so `swap players` can no longer move the shadow score back into it →
    // the score field's exact per-field assertion (live == 200+0) fails, proving the per-field checks
    // bite (omit one field from the swap and that field's own assertion goes red, not a coarse one).
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'score', -1),
  },
  {
    // CAB-02 (cabinet.two-player, slice 18): the title 1P/2P selector and the credit-gated 2P start. A
    // port necessity (no cabinet start buttons): the arrows choose the mode at the title and Space starts
    // it — 1P costs one credit, 2P costs two. On a 2P start `copy players` seeds player 2 identical-fresh
    // from player 1 (the arcade coined_up P2 seed).
    // roadmap-evidence: CAB-02 success  (down/up arrows pick 2P/1P at the title; a two-credit Space start
    //   begins a two-player game with player 1 active and player 2 seeded fresh from player 1)
    key: 'two-player-start',
    behavior:
      'The title up/down arrows select 1P/2P and a credit-gated Space starts the chosen mode — a 2P start needs two credits, sets two-player with player 1 active, and seeds player 2 fresh from player 1',
    playtestStep: 1,
    async drive(vm) {
      vm.greenFlag();
      // One boot pump: the selector key hats only start listening once the runtime has stepped, and the
      // director-state var already reads 'title' from its initial value, so without this the wait loop
      // exits at zero steps and the first arrow tap lands before the hats are live.
      step(vm, 1);
      let g = 0;
      while (stateOf(vm) !== 'title' && g < 50) {
        step(vm, 1);
        g += 1;
      }
      writeVar(vm, 'invuln', 1);
      // The selector: down -> 2P (bottom option), up -> 1P (top option). Each hat sets its bound, so the
      // reads are exact.
      tapKey(vm, 'ArrowDown');
      const selAfterDown = readVar(vm, 'cabinet-start-selection');
      tapKey(vm, 'ArrowUp');
      const selAfterUp = readVar(vm, 'cabinet-start-selection');
      // Choose 2P with only ONE credit banked: below the two-credit cost, so Space is a silent no-op.
      tapKey(vm, 'ArrowDown');
      insertCoin(vm, 1);
      tapKey(vm, ' ');
      const stateOneCredit = stateOf(vm);
      const creditsOneCredit = readVar(vm, 'cabinet-credits');
      // Bank the second credit and start the two-player game.
      insertCoin(vm, 1);
      tapKey(vm, ' ');
      let t = 0;
      while (stateOf(vm) !== 'playing' && t < 150) {
        step(vm, 1);
        t += 1;
      }
      return {
        selAfterDown,
        selAfterUp,
        stateOneCredit,
        creditsOneCredit,
        started: stateOf(vm) === 'playing',
        twoPlayer: readVar(vm, 'cabinet-two-player'),
        currPlayer: readVar(vm, 'cabinet-curr-player'),
        creditsAfter: readVar(vm, 'cabinet-credits'),
        // copy players must have seeded each shadow from the fresh player-1 state:
        craft: readVar(vm, 'eco-craft'),
        otherCraft: readVar(vm, 'other-craft'),
        area: readVar(vm, 'area-number'),
        otherArea: readVar(vm, 'other-area-number'),
        score: readVar(vm, 'eco-score'),
        otherScore: readVar(vm, 'other-score'),
      };
    },
    assert(obs) {
      assert.equal(Number(obs.selAfterDown), 2, 'the down arrow selects a two-player game');
      assert.equal(Number(obs.selAfterUp), 1, 'the up arrow selects a one-player game');
      assert.equal(obs.stateOneCredit, 'title', 'a 2P start with only one credit does not start');
      assert.equal(
        Number(obs.creditsOneCredit),
        1,
        'an under-cost 2P Space press spends nothing and stays at the title',
      );
      assert.ok(obs.started, 'a 2P start with two credits reaches playing');
      assert.equal(Number(obs.twoPlayer), 1, 'the started game is a two-player game');
      assert.equal(Number(obs.currPlayer), 0, 'player 1 is the active player first');
      assert.equal(Number(obs.creditsAfter), 0, 'a two-player start costs two credits');
      // Player 1 is genuinely fresh, and player 2 was seeded from it (not left at a stale/zero shadow).
      assert.equal(Number(obs.score), 0, 'player 1 starts at zero score');
      assert.ok(Number(obs.craft) > 0, 'player 1 starts with craft');
      assert.equal(Number(obs.otherCraft), Number(obs.craft), 'player 2 craft seeded fresh from player 1');
      assert.equal(Number(obs.otherArea), Number(obs.area), 'player 2 area seeded fresh from player 1');
      assert.equal(Number(obs.otherScore), Number(obs.score), 'player 2 score seeded fresh from player 1');
    },
    // Neutralize `copy players`: the 2P game still starts, but player 2's shadows are never seeded from
    // player 1 — `other craft` stays 0 while player 1's fresh `craft` is > 0 → the seed assertion fails.
    // roadmap-evidence: CAB-02 failure  (without the P2 seed, player 2 is not initialised from player 1)
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'copy players'),
  },
  {
    // CAB-02 (slice 18): the on-screen 1P/2P selector DISPLAY. At the title both option labels ("1 PLAYER"
    // and "2 PLAYERS") are shown as start_screen clones; the one whose option matches the live
    // `start selection` renders at full opacity (ghost 0) and the other is dimmed (ghost 60), re-picked
    // every tick so an up/down arrow flips which label is armed on the next frame. This is the visible
    // half of the selector — the two-player-start scenario above covers the start logic.
    // roadmap-evidence: CAB-02 success  (the title shows both 1P/2P labels and highlights the armed one,
    //   tracking the up/down arrow selection live)
    key: 'two-player-selector-display',
    behavior:
      'At the title, both 1P and 2P selector labels are shown and the armed one (by start selection) is highlighted (ghost 0) while the other is dimmed, updating live as the up/down arrows change the choice',
    playtestStep: 1,
    async drive(vm) {
      vm.greenFlag();
      step(vm, 1); // boot pump so the clone roles spawn and the selector hats go live
      let g = 0;
      while (stateOf(vm) !== 'title' && g < 50) {
        step(vm, 1);
        g += 1;
      }
      // Read the two selector clones by costume name: {ghost, visible} for each option label.
      const readSelector = () => {
        const out = {};
        for (const t of vm.runtime.targets) {
          if (t.isOriginal || t.isStage || !t.sprite || t.sprite.name !== 'start_screen') continue;
          const costume = t.getCurrentCostume();
          if (!costume) continue;
          if (costume.name === 'select-1p') out.oneP = { ghost: t.effects.ghost, visible: t.visible };
          if (costume.name === 'select-2p') out.twoP = { ghost: t.effects.ghost, visible: t.visible };
        }
        return out;
      };
      step(vm, 2); // let the dim loops settle on the default selection
      const atDefault = readSelector();
      tapKey(vm, 'ArrowDown'); // arm 2P (bottom option)
      step(vm, 2);
      const atTwoP = readSelector();
      tapKey(vm, 'ArrowUp'); // back to 1P (top option)
      step(vm, 2);
      const atOneP = readSelector();
      return { atDefault, atTwoP, atOneP };
    },
    assert(obs) {
      // Both labels are always present and visible so the choice is discoverable.
      for (const [label, snap] of [
        ['default', obs.atDefault],
        ['after down', obs.atTwoP],
        ['after up', obs.atOneP],
      ]) {
        assert.ok(snap.oneP && snap.twoP, `both selector labels are shown (${label})`);
        assert.ok(snap.oneP.visible && snap.twoP.visible, `both selector labels are visible (${label})`);
      }
      // Default selection is 1P: the 1P label is highlighted, the 2P label dimmed.
      assert.equal(Number(obs.atDefault.oneP.ghost), 0, 'the 1P label is highlighted by default');
      assert.ok(Number(obs.atDefault.twoP.ghost) > 0, 'the 2P label is dimmed by default');
      // Down arrow arms 2P: the highlight moves to the 2P label.
      assert.equal(Number(obs.atTwoP.twoP.ghost), 0, 'the down arrow highlights the 2P label');
      assert.ok(Number(obs.atTwoP.oneP.ghost) > 0, 'the 1P label dims when 2P is armed');
      // Up arrow returns to 1P: the highlight moves back.
      assert.equal(Number(obs.atOneP.oneP.ghost), 0, 'the up arrow highlights the 1P label again');
      assert.ok(Number(obs.atOneP.twoP.ghost) > 0, 'the 2P label dims when 1P is armed');
    },
    // Break only the 2P display role's `start selection == 2` match (scoped to start_screen, so the Stage
    // start gate is untouched): the 2P label can then never register as armed, so it stays dimmed even
    // after the down arrow selects it — the "down arrow highlights the 2P label" assertion fails.
    // roadmap-evidence: CAB-02 failure  (the display no longer tracks the armed selection)
    negativeMutation: (p) => mutate.changeVarEqualsOperand(p, 'start_screen', 'start selection', 2, 9),
  },
  {
    // CAB-03 (cabinet.two-player, slice 18): alternation on craft death — the port's `next_player`
    // (xevious_main 674: swap_curr_other_player + eor curr_player). On a craft death in a two-player game,
    // if the OTHER player still holds craft, the handler swaps the two players' saved state and toggles
    // `curr player`, so control passes to the other player — strict alternating play. Driven in isolation
    // (fireBroadcast 'death complete' against an injected player-dead state), the same director-receiver
    // isolation the near-end-checkpoint scenario uses: a live death->respawn completes within one headless
    // pump and cannot be paused to read the handoff.
    // roadmap-evidence: CAB-03 success  (a craft death with the other player alive swaps state and toggles
    //   the active player; a second death swaps back)
    key: 'two-player-alternation',
    behavior:
      'On a craft death in a two-player game with the other player still holding craft, the handler swaps the saved player state and toggles the active player — and a second death swaps back',
    playtestStep: 5,
    async drive(vm) {
      vm.greenFlag();
      step(vm, 2);
      // Two-player game, player 1 active, both players holding craft; distinct P1 (live) and P2 (other/shadow)
      // score + area so the swap is observable per field. `area progress` sits at the area top, BELOW the
      // near-end window, so the outgoing player's checkpoint does not advance their area (which would confound
      // the saved-area read).
      const setupDead = (currPlayer) => {
        writeVar(vm, 'game-director-state', 'player-dead');
        writeVar(vm, 'cabinet-two-player', 1);
        writeVar(vm, 'cabinet-curr-player', currPlayer);
        writeVar(vm, 'area-progress', 0);
        // A real game, not an attract demo: green flag leaves the cabinet in attract, and since PRES-01's
        // arcade-size craft box a demo craft can die inside the settle steps, ending the demo and resetting
        // the scores before the second swap is read.
        writeVar(vm, 'cabinet-attract', 0);
      };
      writeVar(vm, 'eco-craft', 2); // P1 (live) still has craft: a non-terminal death, so no banner
      writeVar(vm, 'other-craft', 3); // P2 (other) has craft: the other can take over -> alternate
      writeVar(vm, 'eco-score', 1111);
      writeVar(vm, 'other-score', 2222);
      writeVar(vm, 'area-number', 5);
      writeVar(vm, 'other-area-number', 9);
      setupDead(0);
      fireBroadcast(vm, 'death complete');
      step(vm, 3); // swap+toggle complete on the first stepped frame; the rest settle the incoming re-top
      const afterFirst = {
        currPlayer: readVar(vm, 'cabinet-curr-player'),
        liveScore: readVar(vm, 'eco-score'),
        liveArea: readVar(vm, 'area-number'),
        otherScore: readVar(vm, 'other-score'),
        otherCraft: readVar(vm, 'other-craft'),
        bannerPlayer: readVar(vm, 'cabinet-banner-player'),
      };
      // Second death: player 2 is now active and both still hold craft -> alternation swaps back to player 1.
      setupDead(1);
      fireBroadcast(vm, 'death complete');
      step(vm, 3);
      const afterSecond = {
        currPlayer: readVar(vm, 'cabinet-curr-player'),
        liveScore: readVar(vm, 'eco-score'),
        liveArea: readVar(vm, 'area-number'),
      };
      return { afterFirst, afterSecond };
    },
    assert(obs) {
      // First death: active player toggles 0 -> 1, player 2's saved state is now live, player 1's is saved.
      assert.equal(Number(obs.afterFirst.currPlayer), 1, 'a craft death toggles the active player to player 2');
      assert.equal(Number(obs.afterFirst.liveScore), 2222, 'player 2 score becomes the live score after the swap');
      assert.equal(Number(obs.afterFirst.liveArea), 9, 'player 2 area becomes the live area after the swap');
      assert.equal(Number(obs.afterFirst.otherScore), 1111, 'player 1 score is saved to the shadow');
      assert.equal(Number(obs.afterFirst.otherCraft), 2, 'player 1 craft is saved to the shadow');
      assert.equal(Number(obs.afterFirst.bannerPlayer), -1, 'a non-terminal death shows no elimination banner');
      // Second death swaps back: strict alternating play.
      assert.equal(Number(obs.afterSecond.currPlayer), 0, 'a second craft death toggles back to player 1');
      assert.equal(Number(obs.afterSecond.liveScore), 1111, 'player 1 state returns to live on the swap back');
      assert.equal(Number(obs.afterSecond.liveArea), 5, 'player 1 area returns to live on the swap back');
    },
    // Neutralize `swap players`: the active player still toggles, but the saved per-player state never moves,
    // so player 2's score never becomes live -> the swap assertion fails.
    // roadmap-evidence: CAB-03 failure  (without the state swap, alternation carries the wrong player's game)
    negativeMutation: (p) => mutate.neutralizeProc(p, 'Stage', 'swap players'),
  },
  {
    // AREA-01 (slice 20) / ARCH-5: the two-player handoff applies the projected near-end checkpoint to the
    // OUTGOING player's area before the swap, then puts the clock at the area top so the INCOMING player's
    // new-life re-top (which runs the same checkpoint first) leaves their area alone. Same director-receiver
    // isolation as two-player-alternation.
    // roadmap-evidence: AREA-01 success  (a two-player death 44 ticks before the end skips the outgoing
    //   player's next area, and the incoming player resumes their own area unadvanced)
    key: 'two-player-checkpoint-outgoing-only',
    behavior:
      "On a two-player handoff the projected near-end checkpoint advances only the outgoing player's area; the incoming player resumes their own area",
    playtestStep: 5,
    async drive(vm) {
      vm.greenFlag();
      step(vm, 2);
      writeVar(vm, 'game-director-state', 'player-dead');
      writeVar(vm, 'cabinet-two-player', 1);
      writeVar(vm, 'cabinet-curr-player', 0);
      writeVar(vm, 'cabinet-attract', 0);
      writeVar(vm, 'eco-craft', 2);
      writeVar(vm, 'other-craft', 3);
      writeVar(vm, 'area-number', 5);
      writeVar(vm, 'other-area-number', 9);
      writeVar(vm, 'area-progress', 63648); // projects to completion + row 0x0E: skip 5 -> 7
      fireBroadcast(vm, 'death complete');
      step(vm, 3);
      return {
        currPlayer: readVar(vm, 'cabinet-curr-player'),
        liveArea: readVar(vm, 'area-number'),
        savedArea: readVar(vm, 'other-area-number'),
      };
    },
    assert(obs) {
      assert.equal(Number(obs.currPlayer), 1, 'precondition: the handoff passed control to player 2');
      assert.equal(Number(obs.savedArea), 7, "the outgoing player's near-end death skipped their next area");
      assert.equal(Number(obs.liveArea), 9, "the incoming player resumes their own area, unadvanced");
    },
    // Pin every `set area progress` to the death position: the handoff no longer puts the clock at the
    // area top, so the incoming player's re-top re-runs the checkpoint on the outgoing player's position
    // and advances THEIR area (9 -> 11).
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'area progress', 63648),
  },
  {
    // CAB-03 (slice 18): solo continuation — when the OTHER player is already out, a craft death does NOT
    // alternate; the current player simply respawns and plays on (the swap is gated on the other player still
    // holding craft, xevious_main 682). Same director-receiver isolation as two-player-alternation.
    // roadmap-evidence: CAB-03 success  (with the other player out, a death respawns the current player with
    //   no swap and no active-player toggle)
    key: 'two-player-solo-continue',
    behavior:
      'When the other player is already out, a craft death in a two-player game respawns the current player with no swap and no active-player toggle',
    playtestStep: 5,
    async drive(vm) {
      vm.greenFlag();
      step(vm, 2);
      writeVar(vm, 'game-director-state', 'player-dead');
      writeVar(vm, 'cabinet-two-player', 1);
      writeVar(vm, 'cabinet-curr-player', 0);
      writeVar(vm, 'area-progress', 0);
      writeVar(vm, 'eco-craft', 2); // the current player still has craft -> respawn
      writeVar(vm, 'other-craft', 0); // the other player is OUT -> no alternation
      writeVar(vm, 'eco-score', 1111);
      writeVar(vm, 'other-score', 2222);
      fireBroadcast(vm, 'death complete');
      step(vm, 3);
      return {
        currPlayer: readVar(vm, 'cabinet-curr-player'),
        liveScore: readVar(vm, 'eco-score'),
        otherScore: readVar(vm, 'other-score'),
        outcome: outcome(vm),
        reachedState: state(vm),
      };
    },
    assert(obs) {
      assert.equal(Number(obs.currPlayer), 0, 'the active player does NOT toggle when the other is out');
      assert.equal(Number(obs.liveScore), 1111, 'the current player state is untouched (no swap)');
      assert.equal(Number(obs.otherScore), 2222, 'the out player shadow is untouched');
      assert.equal(obs.outcome, 'respawn', 'a solo continuation records the respawn outcome');
      // The solo respawn path has no timed hold, so within these steps it has already run
      // respawning -> new-life -> resetting -> playing; the meaningful check is that the
      // survivor progressed past the death rather than the exact intermediate state.
      assert.ok(
        ['respawning', 'resetting', 'playing'].includes(obs.reachedState),
        'the current player respawns back toward play (does not stall at player-dead)',
      );
    },
    // Remove player-dead -> respawning so the solo continuation cannot respawn -> the respawn assertion fails.
    // roadmap-evidence: CAB-03 failure  (the survivor cannot continue solo)
    negativeMutation: (p) => mutate.removeAllowedTransition(p, 'player-dead -> respawning'),
  },
  {
    // CAB-03 (slice 18): both players out -> game over, and the cabinet returns to a one-player, player-1
    // default (`game_over_1_player` xevious_main 68B forces curr_player=0). Driven live (like death-game-over)
    // because reaching the title needs the full game-over hold + `game over complete` + cold-start chain the
    // solvalou drives; the isolation scenarios above cover the handler's immediate branch. The other player is
    // seeded out and the current player left on its last craft, so one death is terminal for the whole game.
    // roadmap-evidence: CAB-03 success  (the last craft of a two-player game ends it, returns to the title, and
    //   resets the cabinet to one-player / player 1)
    key: 'two-player-both-out-gameover',
    behavior:
      'When both players are out, a two-player game reaches game over, returns to the title, and resets the cabinet to a one-player, player-1 default',
    playtestStep: 5,
    async drive(vm) {
      assert.ok(reachPlaying2P(vm), 'precondition: a two-player game reaches playing');
      // Contact kills (invuln off); the other player is already out and the current player is on its last
      // craft, so this death is terminal for the whole game.
      writeVar(vm, 'invuln', 0);
      writeVar(vm, 'other-craft', 0);
      writeVar(vm, 'eco-craft', 1);
      let reachedTitle = false;
      for (let i = 0; i < 60 && !reachedTitle; i += 1) {
        seedCraftHit(vm);
        step(vm, 1);
        if (state(vm) === 'title') reachedTitle = true;
      }
      return {
        reachedTitle,
        currPlayer: readVar(vm, 'cabinet-curr-player'),
        twoPlayer: readVar(vm, 'cabinet-two-player'),
      };
    },
    assert(obs) {
      assert.equal(obs.reachedTitle, true, 'both players out returns to the title');
      assert.equal(Number(obs.currPlayer), 0, 'game over resets the active player to player 1');
      assert.equal(Number(obs.twoPlayer), 0, 'game over returns the cabinet to a one-player default');
    },
    // Pin `two player` so the cold-start game-over reset can never clear it: the game still ends and reaches
    // the title, but the cabinet stays two-player -> the one-player-reset assertion fails.
    // roadmap-evidence: CAB-03 failure  (the cabinet never returns to its one-player default after a 2P game)
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'two player', 1),
  },
  {
    // CAB-03 (slice 18): the "GAME OVER PLAYER n" elimination banner. When a player loses their last craft but
    // the OTHER player is still in, the arcade shows a brief "GAME OVER PLAYER n" banner during the handoff
    // before the survivor takes over (game_over xevious_main 549-591). The handler raises `banner player` to
    // the eliminated player for BANNER_HOLD_TICKS (a real-frame hold, since the banner must dwell), then lowers
    // it and swaps to the survivor. Same director-receiver isolation as the alternation scenarios.
    // roadmap-evidence: CAB-03 success  (eliminating a player with the other still in raises the banner naming
    //   that player for the hold, then clears it and hands off to the survivor)
    key: 'two-player-banner',
    behavior:
      'Eliminating a player while the other is still in raises the "GAME OVER PLAYER n" banner naming the eliminated player for the hold, then clears it and hands off to the survivor',
    playtestStep: 5,
    async drive(vm) {
      vm.greenFlag();
      step(vm, 2);
      writeVar(vm, 'game-director-state', 'player-dead');
      writeVar(vm, 'cabinet-two-player', 1);
      writeVar(vm, 'cabinet-curr-player', 0);
      writeVar(vm, 'area-progress', 0);
      writeVar(vm, 'eco-craft', 0); // the current player (player 1) is ELIMINATED
      writeVar(vm, 'other-craft', 3); // the other player (player 2) is still in -> banner + handoff
      fireBroadcast(vm, 'death complete');
      step(vm, 1); // into the banner hold: `banner player` now names the eliminated player
      const bannerDuringHold = readVar(vm, 'cabinet-banner-player');
      const currDuringHold = readVar(vm, 'cabinet-curr-player');
      // Exhaust the banner hold, then a margin so the deferred swap/toggle run. The hold is
      // `hold_frames(BANNER_HOLD_TICKS)` with BANNER_HOLD_TICKS == GAME_OVER_HOLD_TICKS == 64
      // arcade half-frames; `hold_frames` paces one iteration per 2 headless `_step` calls
      // (FRAMES_PER_TICK == 2, as the attract dwells do), so the hold clears at ~128 frames.
      step(vm, 140);
      return {
        bannerDuringHold,
        currDuringHold,
        bannerAfter: readVar(vm, 'cabinet-banner-player'),
        currAfter: readVar(vm, 'cabinet-curr-player'),
      };
    },
    assert(obs) {
      assert.equal(Number(obs.bannerDuringHold), 0, 'the banner names the eliminated player (player 1) during the hold');
      assert.equal(Number(obs.currDuringHold), 0, 'the active player has not yet handed off while the banner shows');
      assert.equal(Number(obs.bannerAfter), -1, 'the banner clears after the hold');
      assert.equal(Number(obs.currAfter), 1, 'the survivor (player 2) is active after the handoff');
    },
    // Pin `banner player` so it can never be raised to the eliminated player: the banner never shows during the
    // hold -> the "banner names the eliminated player" assertion fails. (The swap/toggle still run, so this
    // bites the banner specifically, not the handoff.)
    // roadmap-evidence: CAB-03 failure  (the elimination banner never appears)
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'banner player', -1),
  },
  {
    key: 'two-player-hud-render',
    behavior:
      'A two-player game draws the active player\'s nUP label AND the other player\'s steady nUP label plus their frozen second score row, each nUP label reading the correct player number; a one-player game draws only the single 1UP label and no second row',
    // roadmap-evidence: ECO-02 success  (the 2P HUD adds the second score row + 2UP label; 1P has neither)
    playtestStep: 6,
    async drive(vm) {
      // Clone roles emitted by tools/game_director.py's HUD dispatch (the HUD_ROLE_* constants):
      const ROLE_PRIMARY_LABEL = 4; //   HUD_ROLE_LABEL_1UP        — the active player's nUP (flashing)
      const ROLE_OTHER_SCORE = 8; //     HUD_ROLE_OTHER_SCORE_DIGIT — the other player's frozen score row
      const ROLE_SECONDARY_LABEL = 9; // HUD_ROLE_LABEL_2UP        — the other player's nUP (steady)
      const roleName = variable('hud-role').name;
      // A label group is three clones {digit/N, glyph/U, glyph/P}; its leading `digit/N` names the player.
      const labelLead = (v, role) =>
        cloneReports(v, 'hud', [roleName])
          .filter((r) => Number(r.vars[roleName]) === role)
          .map((r) => r.costume)
          .find((c) => /^digit\//.test(c || ''));
      const roleCount = (v, role) =>
        cloneReports(v, 'hud', [roleName]).filter((r) => Number(r.vars[roleName]) === role).length;

      // The build under test (possibly mutated) in a two-player game, player 1 active.
      assert.ok(reachPlaying2P(vm), 'precondition: a two-player game reaches playing');
      step(vm, 30);
      const twoP = {
        two: Number(readVar(vm, 'cabinet-two-player')),
        curr: Number(readVar(vm, 'cabinet-curr-player')),
        primaryLead: labelLead(vm, ROLE_PRIMARY_LABEL),
        secondaryLead: labelLead(vm, ROLE_SECONDARY_LABEL),
        otherScoreDigits: roleCount(vm, ROLE_OTHER_SCORE),
      };

      // A clean, unmutated one-player game for the negative-space comparison: no second row, no 2UP label.
      const vm1 = await loadArtifact();
      assert.ok(reachPlaying(vm1), 'precondition: a one-player game reaches playing');
      step(vm1, 30);
      const oneP = {
        two: Number(readVar(vm1, 'cabinet-two-player')),
        primaryLead: labelLead(vm1, ROLE_PRIMARY_LABEL),
        secondaryLabels: roleCount(vm1, ROLE_SECONDARY_LABEL),
        otherScoreDigits: roleCount(vm1, ROLE_OTHER_SCORE),
      };
      return { twoP, oneP };
    },
    assert(obs) {
      // Two-player, player 1 active: primary label reads "1UP", the steady other label reads "2UP",
      // and the other player's frozen 7-digit score row is present.
      assert.equal(obs.twoP.two, 1, 'the build under test is in a two-player game');
      assert.equal(obs.twoP.curr, 0, 'player 1 is the active player at a fresh 2P start');
      assert.equal(obs.twoP.primaryLead, 'digit/1', 'the active player\'s label reads 1UP');
      assert.equal(obs.twoP.secondaryLead, 'digit/2', 'the other player\'s steady label reads 2UP');
      assert.equal(obs.twoP.otherScoreDigits, 7, 'the other player\'s frozen 7-digit score row is drawn');
      // One-player: only the single 1UP label — no 2UP label and no second score row.
      assert.equal(obs.oneP.two, 0, 'the comparison build is a one-player game');
      assert.equal(obs.oneP.primaryLead, 'digit/1', 'the sole label reads 1UP');
      assert.equal(obs.oneP.secondaryLabels, 0, 'a one-player game draws no 2UP label');
      assert.equal(obs.oneP.otherScoreDigits, 0, 'a one-player game draws no second score row');
    },
    // Pin `two player` to 0 so the two-player-only secondary group (the 2UP label + the other score row)
    // never spawns in the build under test — the 2P assertions fail. The 1P comparison vm is loaded fresh
    // and unmutated, so the negative bites only the 2P side, exactly the ECO-02 addition.
    // roadmap-evidence: ECO-02 failure  (the 2P HUD loses its second row and 2UP label)
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'two player', 0),
  },
  {
    key: 'two-player-clone-no-leak',
    behavior:
      'The two-player HUD peak clone census sits well under the scratch-vm ceiling, and the count returns to that baseline across repeated alternations (no clones leak); the active nUP label tracks the current player across each handoff',
    // roadmap-evidence: ECO-02 success  (2P peak census stays under the ceiling and returns to baseline; label follows curr player)
    playtestStep: 6,
    async drive(vm) {
      const ROLE_PRIMARY_LABEL = 4; //   HUD_ROLE_LABEL_1UP  — the active player's nUP
      const ROLE_SECONDARY_LABEL = 9; // HUD_ROLE_LABEL_2UP  — the other player's nUP
      const roleName = variable('hud-role').name;
      const labelLead = (role) =>
        cloneReports(vm, 'hud', [roleName])
          .filter((r) => Number(r.vars[roleName]) === role)
          .map((r) => r.costume)
          .find((c) => /^digit\//.test(c || ''));
      const totalClones = () => {
        let n = 0;
        for (const c of vm.runtime.targets) if (!c.isStage && !c.isOriginal && c.sprite) n += 1;
        return n;
      };
      const clearEnemy = (slot = 63) => {
        const a = readVar(vm, 'slot-state');
        a[slot] = 0;
      };
      // One alternation: keep both players stocked and vulnerable, seed a contact hit until the active
      // player leaves 'playing' (a death registered), then stop seeding, restore invulnerability, and let
      // the swap+respawn carry the incoming player back to a populated 'playing' state.
      const alternate = (killBudget = 160, recoverBudget = 300) => {
        const before = Number(readVar(vm, 'cabinet-curr-player'));
        let killed = false;
        for (let i = 0; i < killBudget && !killed; i += 1) {
          writeVar(vm, 'other-craft', 3);
          writeVar(vm, 'eco-craft', 3);
          writeVar(vm, 'invuln', 0);
          seedCraftHit(vm);
          step(vm, 1);
          if (Number(readVar(vm, 'cabinet-curr-player')) !== before) killed = true;
        }
        clearEnemy();
        writeVar(vm, 'invuln', 1);
        for (let i = 0; i < recoverBudget; i += 1) {
          writeVar(vm, 'other-craft', 3);
          writeVar(vm, 'eco-craft', 3);
          clearEnemy();
          step(vm, 1);
          if (stateOf(vm) === 'playing') {
            step(vm, 150); // let the incoming player's field repopulate to its steady census
            return { killed, curr: Number(readVar(vm, 'cabinet-curr-player')) };
          }
        }
        return { killed, curr: Number(readVar(vm, 'cabinet-curr-player')) };
      };

      // reachPlaying is a ONE-player start, so the existing ground-pool census (which uses it) never sees
      // the 2P peak. Reach the two-player peak explicitly and census THAT.
      assert.ok(reachPlaying2P(vm), 'precondition: a two-player game reaches playing');
      step(vm, 40);
      const base = {
        total: totalClones(),
        hud: cloneCount(vm, 'hud'),
        curr: Number(readVar(vm, 'cabinet-curr-player')),
        primaryLead: labelLead(ROLE_PRIMARY_LABEL),
        secondaryLead: labelLead(ROLE_SECONDARY_LABEL),
      };
      const alt1 = alternate(); // player 1 -> player 2
      const afterAlt1 = { total: totalClones(), hud: cloneCount(vm, 'hud'), curr: alt1.curr, primaryLead: labelLead(ROLE_PRIMARY_LABEL) };
      const alt2 = alternate(); // player 2 -> player 1
      const afterAlt2 = { total: totalClones(), hud: cloneCount(vm, 'hud'), curr: alt2.curr, primaryLead: labelLead(ROLE_PRIMARY_LABEL) };
      return { base, afterAlt1, afterAlt2 };
    },
    assert(obs) {
      // Peak 2P census sits under the scratch-vm MAX_CLONE_COUNT (300) with ample headroom — the plan's
      // explicit `300 - total >= 50` at the 2P peak, which the 1P ground-pool census never reaches.
      const peak = Math.max(obs.base.total, obs.afterAlt1.total, obs.afterAlt2.total);
      assert.ok(peak < 250, `two-player peak clone census stays well under the ceiling (peak=${peak})`);
      assert.ok(300 - peak >= 50, `two-player peak leaves >=50 clone headroom (headroom=${300 - peak})`);
      // No leak: the HUD clone count returns to its 2P baseline after each alternation.
      assert.equal(obs.afterAlt1.hud, obs.base.hud, 'HUD clone count returns to baseline after the first handoff');
      assert.equal(obs.afterAlt2.hud, obs.base.hud, 'HUD clone count returns to baseline after the second handoff');
      assert.equal(obs.afterAlt1.total, obs.base.total, 'total clone count returns to baseline after the first handoff');
      assert.equal(obs.afterAlt2.total, obs.base.total, 'total clone count returns to baseline after the second handoff');
      // The active nUP label tracks the current player across each handoff (1UP -> 2UP -> 1UP).
      assert.equal(obs.base.curr, 0, 'player 1 is active at 2P start');
      assert.equal(obs.base.primaryLead, 'digit/1', 'the active label reads 1UP for player 1');
      assert.equal(obs.afterAlt1.curr, 1, 'player 2 is active after the first handoff');
      assert.equal(obs.afterAlt1.primaryLead, 'digit/2', 'the active label reads 2UP for player 2');
      assert.equal(obs.afterAlt2.curr, 0, 'player 1 is active again after the second handoff');
      assert.equal(obs.afterAlt2.primaryLead, 'digit/1', 'the active label reads 1UP again for player 1');
    },
    // Pin `curr player` to 0 so a handoff can never make player 2 active: the active label never flips to
    // "2UP" and `afterAlt1.curr == 1` fails. (The clone census would still hold, so this bites the
    // active-label-tracks-current-player half specifically.)
    // roadmap-evidence: ECO-02 failure  (the active nUP label no longer follows the current player)
    negativeMutation: (p) => mutate.pinVariableSet(p, 'Stage', 'curr player', 0),
  },
];

// VM-cannot-observe behaviors that stay the operator playtest's job, named so "complete"
// is honest: the net covers the logic layer of these areas, never the on-screen result.
export const EXCLUSIONS = [
  'The bomb flight/explosion duration and true concurrent lockout (timing collapses headless)',
  'Collision-driven death from an enemy or bullet (rendered collision)',
  "Sprite visibility, layering, a costume's rendered pixels, audio, and overall feel (the digit " +
    'scenario observes WHICH costume a clone switches to — deterministic state — never how it looks)',
];
