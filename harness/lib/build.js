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
  // credit-gated start hat is a silent no-op and the craft never leaves the title.
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

export { stateOf };
