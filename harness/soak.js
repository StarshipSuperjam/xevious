// The release soak (RELEASE-01, release.full-soak, StarshipSuperjam/xevious#105, slice 21). Long runs over the
// shipped build that the scenario net in scenarios.test.js cannot afford: the whole campaign, areas 1 to 16 and the
// loop into 7, played by the harness the way a player meets it, plus the cabinet flow around it. What it shows is
// set by docs/spec/release.md:
//
//   - the campaign: every area's schedule is consumed in full, add_object records included (a record the arcade
//     itself drops because its slot is busy counts as consumed and is logged);
//   - the clone envelope: the clone count stays at least 50 under scratch-vm's 300 through the campaign, the title
//     and attract screens, initials entry and a two-player game, and the clone count and the game's lists return to
//     their baseline after each death, each game over, and a stop and reload;
//   - repeatability: two campaign runs give the same trace at every area entry;
//   - stop and reload: no clone, thread, sound or state outlives a stopped game.
//
// Not a gameplay gate and no substitute for the operator's playtest (README). It runs in its own CI job,
// `runtime-soak` (`node --test soak.js`); the file is not named *.test.js, so the scenario net's `node --test`
// leaves it out.
//
// Pacing. scratch-vm ends a frame once a redraw has been requested, and only a renderer requests one. In the
// editor the stage changes every frame of play (the terrain scrolls every tick), so each frame runs one pass of
// every thread and the walk advances one tick. Headless, nothing requests a redraw and a pump keeps running passes
// until a wall-clock budget is spent: how many ticks share a frame then depends on the machine, and so does the
// game's outcome (area 1's score swung between 1430 and 2980 across runs). Every soak VM is therefore paced the
// way the editor runs it, one pass per pump (`paceLikeTheEditor`), which makes the runs depend on the game alone:
// two campaigns can be compared, and a reloaded game against a fresh one, on any machine. The inputs, the clone
// peak and every area's record are still taken inside traps on the Stage variables the game writes
// (trapStageVar), on the exact tick they change.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  loadBuild,
  step,
  readVar,
  writeVar,
  keyDown,
  keyUp,
  recordSounds,
  trapStageVar,
  paceLikeTheEditor,
} from './lib/harness.js';
import { reachPlaying, reachPlaying2P, loadMutatedSource } from './lib/build.js';

const CLONE_LIMIT = 300; // scratch-vm MAX_CLONE_COUNT
const HEADROOM = 50; // docs/spec/release.md: the peak stays at least 50 under the limit
const PEAK_CEILING = CLONE_LIMIT - HEADROOM;
const AREA_CHANGES = 16; // 1 -> 2 ... 15 -> 16, then 16 -> 7 (the loop)
const CAMPAIGN_TICKS = 40000; // the campaign is 2033 + 15 * 2048 = 32753 ticks; the rest is slack
const PUMP_CEILING = 100000; // a backstop only: a paced pump is one frame, one tick of play
const REACH_PUMPS = 2000; // the title to playing: a credit, the start, and the READY hold, in frames
const TITLE_PUMPS = 100; // a green flag reaches the title in two frames

const load = async () => paceLikeTheEditor(await loadBuild());

const clones = (vm) => vm.runtime._cloneCounter;
const state = (vm) => readVar(vm, 'game-director-state');

// The schedule row a record sits on, as a distance into its area: the row counts down from 0x0D through 0x00 and
// wraps to 0xFF down to 0x0E, where the area completes (docs/spec/area-progression-and-terrain.md).
const rowDistance = (row) => (0x0d - row + 256) % 256;

// Where each area's schedule cursor must stand when the area completes. The cursor reaches the area's sentinel
// (its `end`, a row-0x0D record that never fires) once every record has fired, unless a record sits on a row the
// scroll has already passed when the cursor reaches it: that record can never fire and the cursor stops on it, in
// the arcade as in the port. Area 14's final formation reset is the one such record (the recorded reference
// anomaly in docs/spec/area-progression-and-terrain.md), so its cursor stops one short of its sentinel.
function expectedStops(vm) {
  const starts = readVar(vm, 'area-schedule-start').map(Number);
  const ends = readVar(vm, 'area-schedule-end').map(Number);
  const rows = readVar(vm, 'area-schedule-trigger-row').map(Number);
  return starts.map((start, a) => {
    for (let i = start + 1; i < ends[a]; i += 1) {
      if (rowDistance(rows[i - 1]) < rowDistance(rows[i - 2])) return { area: a + 1, start, stop: i, anomaly: true };
    }
    return { area: a + 1, start, stop: ends[a], anomaly: false };
  });
}

// Every original target's lists, by `<target>.<name>`, with their lengths.
function listLengths(vm) {
  const out = {};
  for (const t of vm.runtime.targets) {
    if (!t.isOriginal) continue;
    for (const v of Object.values(t.variables)) {
      if (v.type === 'list') out[`${t.isStage ? 'Stage' : t.sprite.name}.${v.name}`] = v.value.length;
    }
  }
  return out;
}

// The scripted pilot, keyed on the walk's own tick so it is identical whatever the pumps do: fire held, the craft
// swept left and right every 64 ticks, and a bomb pressed for 4 of every 32 ticks. A craft that moves and shoots
// keeps homing enemies and surviving Sparios from parking in the flying slots, as a live player does.
function pilot(vm) {
  const held = new Set();
  const press = (key, down) => {
    if (down && !held.has(key)) {
      keyDown(vm, key);
      held.add(key);
    } else if (!down && held.has(key)) {
      keyUp(vm, key);
      held.delete(key);
    }
  };
  return (tick) => {
    press(' ', true);
    const leftward = Math.floor(tick / 64) % 2 === 0;
    press('ArrowLeft', leftward);
    press('ArrowRight', !leftward);
    press('b', tick % 32 < 4);
  };
}

// One accelerated real game from area 1 to the loop into area 7, the craft kept alive by the dormant `invuln`
// hook. Returns the per-area records (one per area change) and the clone peak. With `stopAtFirstFailure` it stops
// pumping as soon as an area completes with its schedule not consumed (the negative).
async function runCampaign(vm, { stopAtFirstFailure = false, areaChanges = AREA_CHANGES } = {}) {
  assert.ok(reachPlaying(vm, REACH_PUMPS), 'precondition: the game reaches playing');
  assert.equal(Number(readVar(vm, 'invuln')), 1, 'precondition: the harness keeps the craft alive');
  const stops = expectedStops(vm);
  const handlers = readVar(vm, 'area-schedule-handler');
  const ends = readVar(vm, 'area-schedule-end').map(Number);
  const steer = pilot(vm);
  const areas = [];
  const failures = [];
  let peak = clones(vm);
  let ticks = 0;
  let pendingSets = 0;
  let placementDrops = 0;
  let pendingSlot = 0;
  const releases = [
    trapStageVar(vm, 'tick', (tick) => {
      ticks += 1;
      steer(Number(tick));
      peak = Math.max(peak, clones(vm));
    }),
    trapStageVar(vm, 'pending-object-type', (type, old) => {
      if (Number(type) > 0) {
        pendingSets += 1;
        pendingSlot = Number(readVar(vm, 'pending-object-slot'));
      } else if (Number(old) > 0 && pendingSlot > 0) {
        // The placement step clears the register after placing; an empty slot here means it dropped the record.
        if (Number(readVar(vm, 'slot-type')[pendingSlot - 1]) === 0) placementDrops += 1;
        pendingSlot = 0;
      }
    }),
    // `area-number` is written before `_enter_next_area` repoints the cursor, so this reads the outgoing area's
    // final cursor on the completion tick itself.
    trapStageVar(vm, 'area-number', (to, from) => {
      const area = Number(from);
      if (Number(to) === area) return;
      const expected = stops[area - 1];
      let adds = 0;
      for (let i = expected.start; i < ends[area - 1]; i += 1) if (handlers[i - 1] === 'add_object') adds += 1;
      const record = {
        from: area,
        to: Number(to),
        tick: Number(readVar(vm, 'tick')),
        cursor: Number(readVar(vm, 'area-schedule-cursor')),
        stop: expected.stop,
        anomaly: expected.anomaly,
        adds,
        dropped: adds - pendingSets + placementDrops,
        score: Number(readVar(vm, 'eco-score')),
        rng: Number(readVar(vm, 'rng-state')),
        clones: clones(vm),
        row: Number(readVar(vm, 'player-row')),
        col: Number(readVar(vm, 'player-col')),
      };
      areas.push(record);
      if (record.cursor !== record.stop) failures.push(record);
      pendingSets = 0;
      placementDrops = 0;
    }),
  ];
  let pumps = 0;
  try {
    while (areas.length < areaChanges && ticks < CAMPAIGN_TICKS && pumps < PUMP_CEILING) {
      if (stopAtFirstFailure && failures.length) break;
      step(vm, 1);
      pumps += 1;
      if (state(vm) !== 'playing') break;
    }
  } finally {
    releases.forEach((release) => release());
    for (const key of [' ', 'ArrowLeft', 'ArrowRight', 'b']) keyUp(vm, key);
  }
  return { areas, failures, peak, ticks, pumps, state: state(vm), stops };
}

// The comparable part of an area-entry record: everything the game decided, nothing about the pumps.
const entryTrace = (areas) => areas.map(({ from, to, tick, cursor, score, rng, clones: c, row, col }) => ({
  from, to, tick, cursor, score, rng, clones: c, row, col,
}));

let firstCampaign = null;

test('campaign: areas 1 to 16 and the loop into 7, every schedule consumed, within the clone headroom', async () => {
  const vm = await load();
  const run = await runCampaign(vm);
  firstCampaign = run;
  for (const a of run.areas) {
    console.log(
      `soak: area ${a.from} -> ${a.to} at tick ${a.tick}: cursor ${a.cursor}/${a.stop}${a.anomaly ? ' (recorded anomaly)' : ''}, `
      + `add_object ${a.adds} (dropped ${a.dropped}), clones ${a.clones}`,
    );
  }
  console.log(`soak: ${run.ticks} ticks in ${run.pumps} pumps, clone peak ${run.peak}`);
  assert.equal(run.state, 'playing', 'the invulnerable craft is still playing at the end');
  assert.deepEqual(
    run.areas.map((a) => `${a.from}->${a.to}`),
    ['1->2', '2->3', '3->4', '4->5', '5->6', '6->7', '7->8', '8->9', '9->10', '10->11', '11->12', '12->13',
      '13->14', '14->15', '15->16', '16->7'],
    'the campaign runs every area in order and loops from 16 into 7',
  );
  assert.deepEqual(run.failures, [], 'every area completes with its schedule consumed');
  assert.deepEqual(
    run.stops.filter((s) => s.anomaly).map((s) => s.area),
    [14],
    'the only record that can never fire is area 14\'s recorded anomaly',
  );
  assert.ok(run.areas.some((a) => a.adds > 0), 'the campaign meets add_object records');
  assert.ok(run.peak <= PEAK_CEILING, `clone peak ${run.peak} keeps ${HEADROOM} clones of headroom under ${CLONE_LIMIT}`);
});

test('repeatability: a second campaign gives the same trace at every area entry', async () => {
  assert.ok(firstCampaign && firstCampaign.areas.length === AREA_CHANGES, 'precondition: the first campaign completed');
  const vm = await load();
  const run = await runCampaign(vm);
  assert.deepEqual(entryTrace(run.areas), entryTrace(firstCampaign.areas), 'the two runs agree at every area entry');
});

test('envelope: deaths and the game over return the clones and lists to their baseline', async () => {
  const vm = await load();
  vm.greenFlag();
  step(vm, 2);
  for (let g = 0; g < TITLE_PUMPS && state(vm) !== 'title'; g += 1) step(vm, 1);
  const titleBaseline = { clones: clones(vm), lists: listLengths(vm) };
  assert.ok(reachPlaying(vm, REACH_PUMPS), 'precondition: the game reaches playing');
  writeVar(vm, 'invuln', 0); // a real, mortal game: the craft dies to whatever reaches it
  const lifeStarts = [];
  let gameOver = false;
  let peak = clones(vm);
  const releases = [
    trapStageVar(vm, 'tick', () => {
      peak = Math.max(peak, clones(vm));
    }),
    trapStageVar(vm, 'game-director-state', (to, from) => {
      if (to === from) return;
      if (to === 'playing') lifeStarts.push({ clones: clones(vm), lists: listLengths(vm) });
      if (to === 'game-over') gameOver = true;
    }),
  ];
  try {
    for (let p = 0; p < PUMP_CEILING && !(gameOver && state(vm) === 'title'); p += 1) step(vm, 1);
  } finally {
    releases.forEach((release) => release());
  }
  assert.ok(gameOver && state(vm) === 'title', 'the mortal game ran to game over and back to the title');
  assert.ok(lifeStarts.length >= 2, 'precondition: the craft died and respawned');
  const [first, ...later] = lifeStarts;
  later.forEach((life, i) => {
    assert.equal(life.clones, first.clones, `life ${i + 2} starts with the first life's clone count`);
    assert.deepEqual(life.lists, first.lists, `life ${i + 2} starts with the first life's list lengths`);
  });
  assert.equal(clones(vm), titleBaseline.clones, 'after the game over the title has its boot clone count');
  assert.deepEqual(listLengths(vm), titleBaseline.lists, 'after the game over the lists have their boot lengths');
  assert.ok(peak <= PEAK_CEILING, `clone peak ${peak} keeps ${HEADROOM} clones of headroom`);
});

test('envelope: the title, attract, initials entry and a two-player game stay within the clone headroom', async () => {
  let peak = 0;
  let where = '';
  const watch = (vm, label) => {
    const c = clones(vm);
    if (c > peak) {
      peak = c;
      where = label;
    }
  };
  // The title and the attract cycle (a demo and the best-five screen), left to run on their own.
  const attract = await load();
  attract.greenFlag();
  step(attract, 1);
  const seen = new Set();
  let titleBaseline = null;
  const release = trapStageVar(attract, 'tick', () => watch(attract, 'attract demo'));
  try {
    for (let p = 0; p < 4 * REACH_PUMPS && seen.size < 3; p += 1) {
      step(attract, 1);
      const s = state(attract);
      watch(attract, s);
      if (s === 'title' && !titleBaseline) titleBaseline = { clones: clones(attract), lists: listLengths(attract) };
      if (s === 'playing' && Number(readVar(attract, 'cabinet-attract')) === 1) seen.add('demo');
      else if (s === 'title' || s === 'attract-scores') seen.add(s);
    }
  } finally {
    release();
  }
  assert.deepEqual([...seen].sort(), ['attract-scores', 'demo', 'title'], 'precondition: the title, a demo and the best-five screen all ran');

  // A two-player game, piloted: kept alive until player one's score would place in the best five (fifth place is
  // 20,000), then made mortal so both players lose every craft, alternating, to initials entry, the game over and
  // the title. (The port holds both players' entries to the point both are out, a recorded CAB-04 divergence from
  // the arcade's entry at each player's own game over; player one's score is checked there as `other score`.)
  const two = await load();
  assert.ok(reachPlaying2P(two, REACH_PUMPS), 'precondition: a two-player game starts');
  const steer = pilot(two);
  const states = new Set();
  const releases = [
    trapStageVar(two, 'tick', (tick) => {
      if (state(two) === 'playing') steer(Number(tick));
      watch(two, 'two-player game');
    }),
    trapStageVar(two, 'game-director-state', (to) => {
      states.add(to);
      if (to !== 'playing') for (const key of [' ', 'ArrowLeft', 'ArrowRight', 'b']) keyUp(two, key);
    }),
  ];
  try {
    for (let p = 0; p < PUMP_CEILING && Number(readVar(two, 'eco-score')) < 25000; p += 1) step(two, 1);
    assert.ok(Number(readVar(two, 'eco-score')) >= 25000, 'precondition: player one scored into the best five');
    writeVar(two, 'invuln', 0);
    for (let p = 0; p < PUMP_CEILING && !(states.has('game-over') && state(two) === 'title'); p += 1) {
      step(two, 1);
      watch(two, state(two));
    }
  } finally {
    releases.forEach((r) => r());
  }
  assert.equal(state(two), 'title', 'the two-player game ran to its game over and back to the title');
  assert.ok(states.has('high-score-entry'), 'a qualifying score went through initials entry');
  assert.equal(clones(two), titleBaseline.clones, 'the title after the game over has the boot clone count');
  assert.deepEqual(listLengths(two), titleBaseline.lists, 'the title after the game over has the boot list lengths');
  console.log(`soak: cabinet clone peak ${peak} (${where})`);
  assert.ok(peak <= PEAK_CEILING, `clone peak ${peak} (${where}) keeps ${HEADROOM} clones of headroom`);
});

test('stop and reload: nothing from the stopped game survives into the next', async () => {
  // Stop is checked at the runtime (no clone, no thread, so no music loop, survives it); the reload by what the
  // project then does. Every variable is not compared one for one: the originals keep working registers (clone-loop
  // counters, the aim and RNG scratch values, `invuln` itself, which only the harness writes) that the game sets
  // before it reads them. What must hold is that the reloaded title is the boot title (clones, threads, lists,
  // sounds) and that the reloaded game plays area 1 exactly as the first campaign's fresh boot did — a comparison
  // the editor pacing makes exact (unpaced, a reloaded game drifts with the machine's speed, not with its state).
  assert.ok(firstCampaign && firstCampaign.areas.length > 0, 'precondition: the first campaign completed area 1');
  const vm = await load();
  const sounds = recordSounds(vm);
  const toTitle = () => {
    vm.greenFlag();
    step(vm, 2);
    for (let g = 0; g < TITLE_PUMPS && state(vm) !== 'title'; g += 1) step(vm, 1);
    return state(vm) === 'title';
  };
  const title = () => ({ clones: clones(vm), threads: vm.runtime.threads.length, lists: listLengths(vm) });
  assert.ok(toTitle(), 'precondition: boot reaches the title');
  const boot = title();
  const bootSounds = sounds.map((s) => s.sound);
  assert.ok(reachPlaying(vm, REACH_PUMPS), 'precondition: the game reaches playing');
  for (let p = 0; p < CAMPAIGN_TICKS && Number(readVar(vm, 'area-number')) < 2; p += 1) step(vm, 1);
  assert.equal(Number(readVar(vm, 'area-number')), 2, 'precondition: the stopped game is in area 2');
  vm.stopAll();
  assert.equal(clones(vm), 0, 'stop removes every clone');
  assert.equal(vm.runtime.threads.length, 0, 'stop ends every thread, the music loop included');
  sounds.length = 0;
  assert.ok(toTitle(), 'the reloaded project reaches the title');
  assert.deepEqual(title(), boot, 'the reloaded title has the boot clones, threads and list lengths');
  assert.deepEqual(sounds.map((s) => s.sound), bootSounds, 'the reloaded title plays what the boot did, nothing more');
  const run = await runCampaign(vm, { areaChanges: 1 });
  assert.deepEqual(entryTrace(run.areas), entryTrace(firstCampaign.areas.slice(0, 1)), 'the reloaded game plays area 1 as a fresh boot does');
});

// The negative: a build whose `advance area` never runs its schedule consume. The soak must catch it inside
// area 1 — the first completion finds area 1's cursor still on its first record.
test('negative: a build that skips the schedule consume fails the soak inside area 1', async () => {
  const vm = paceLikeTheEditor(await loadMutatedSource((p) => {
    const b = p.targets.find((t) => t.isStage).blocks;
    const proto = Object.keys(b).find(
      (k) => b[k] && b[k].opcode === 'procedures_prototype' && b[k].mutation && b[k].mutation.proccode === 'advance area',
    );
    const chain = [];
    for (let cur = b[b[proto].parent].next; cur; cur = b[cur].next) chain.push(cur);
    const consume = chain.findIndex((id) => b[id].opcode === 'control_repeat_until');
    if (consume < 1) throw new Error('soak negative: advance area has no schedule consume loop');
    const before = chain[consume - 1];
    const after = chain[consume + 1] || null;
    b[before].next = after;
    if (after) b[after].parent = before;
    b[chain[consume]].next = null;
    b[chain[consume]].parent = null;
    b[chain[consume]].topLevel = true;
    b[chain[consume]].x = 0;
    b[chain[consume]].y = 0;
  }));
  const run = await runCampaign(vm, { stopAtFirstFailure: true });
  assert.ok(run.failures.length > 0, 'the soak reports an unconsumed schedule');
  assert.equal(run.failures[0].from, 1, 'it fails inside area 1');
  assert.equal(run.areas.length, 1, 'it stops at the first completion');
});
