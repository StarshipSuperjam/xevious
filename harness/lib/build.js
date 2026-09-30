// Loading helpers for scenarios.
//
// Positive scenarios run against the SHIPPED artifact (dist/Xevious.sb3) — the thing the
// operator actually plays. Negative fixtures run against an in-memory mutation of the
// source project (loaded as a JSON object, no zip), mirroring the Python suite's
// deep-copy-and-mutate discipline: nothing broken is ever committed.
import VM from 'scratch-vm';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { REPO_ROOT } from './identifiers.js';
import { loadBuild, greenFlag, step, tapKey, readVar, writeVar } from './harness.js';

export { loadBuild as loadArtifact };

const PROJECT_JSON = resolve(REPO_ROOT, 'src', 'xevious', 'project.json');

/** Deep-copy the source project, apply `mutate`, and load the result headless. */
export async function loadMutatedSource(mutate) {
  const project = JSON.parse(readFileSync(PROJECT_JSON, 'utf8'));
  mutate(project);
  const vm = new VM();
  vm.setTurboMode(false);
  await vm.loadProject(JSON.stringify(project));
  vm.runtime.currentStepTime = 1000 / 30;
  return vm;
}

const stateOf = (vm) => readVar(vm, 'game-director-state');

/**
 * Green-flag to title, then press start and step until the game is playing, with the craft made
 * invulnerable for the run. Slice 8's live flying enemies home on and kill the craft, and one
 * headless pump runs hundreds of game ticks with no agency to shoot or dodge — so without this an
 * unattended craft dies (and respawns, resetting the area and deleting renderer clones) before a
 * scenario can observe anything. `invuln` is a dormant debug flag (never set by game logic); setting
 * it here keeps the reach reliable and the craft alive for observational scenarios. Death scenarios
 * clear it (`writeVar(vm,'invuln',0)`) to exercise real player death. Space is HELD (not tapped) and
 * we stop at the first playing tick — a tap pumps once more after release and could overshoot.
 */
/**
 * Insert `count` coins by tapping the coin key (C). Each tap is a press+release cycle; the Stage's
 * always-on coin poll registers the rising edge and adds one credit (capped at 99), so after this call
 * `credits` has grown by `count` (fewer if the cap is hit). CAB-02 (slice 17).
 */
export function insertCoin(vm, count = 1) {
  for (let i = 0; i < count; i += 1) tapKey(vm, 'c');
}

export function reachPlaying(vm, budget = 150) {
  greenFlag(vm);
  step(vm, 1);
  writeVar(vm, 'invuln', 1);
  // CAB-02 (slice 17): the title->ready start now costs a credit, so bank one first. Tapping C lets the
  // Stage's always-on coin poll register the rising edge and raise `credits` to 1; without it the
  // credit-gated start hat is a silent no-op and the craft never leaves the title. `start selection`
  // defaults to 1 (one-player), so no arrow press is needed here (CAB-02 slice 18).
  insertCoin(vm, 1);
  // Tap start (press + release) so the title->ready edge fires but space is NOT held into playing —
  // a held space would make the blaster fire on the first playing tick, leaving stray shots that a
  // later scenario would see kill enemies. `invuln` keeps the craft alive so the reach is reliable.
  tapKey(vm, ' ');
  let t = 0;
  while (stateOf(vm) !== 'playing' && t < budget) {
    step(vm, 1);
    t += 1;
  }
  return stateOf(vm) === 'playing';
}

/**
 * Reach a TWO-PLAYER game (CAB-02/CAB-03, slice 18): wait for the title, bank two credits, choose 2P at
 * the title with the right-arrow selector, and start with Space. Mirrors reachPlaying but for the 2P path
 * — the precondition the 2UP HUD and alternation scenarios build on. The craft is made invulnerable for
 * the run, exactly like reachPlaying.
 */
export function reachPlaying2P(vm, budget = 150) {
  greenFlag(vm);
  // One boot pump after the green flag: the "when key pressed" selector hats only become live once the
  // runtime has stepped, and the director-state var reads 'title' from its initial value, so the wait loop
  // below would otherwise exit at zero steps and the arrow tap would land before the hats are listening.
  step(vm, 1);
  let g = 0;
  while (stateOf(vm) !== 'title' && g < 50) {
    step(vm, 1);
    g += 1;
  }
  writeVar(vm, 'invuln', 1);
  insertCoin(vm, 2); // a two-player start costs two credits
  tapKey(vm, 'ArrowRight'); // title selector: choose 2P (start selection -> 2)
  tapKey(vm, ' '); // start the two-player game
  let t = 0;
  while (stateOf(vm) !== 'playing' && t < budget) {
    step(vm, 1);
    t += 1;
  }
  return stateOf(vm) === 'playing';
}

export { stateOf };
