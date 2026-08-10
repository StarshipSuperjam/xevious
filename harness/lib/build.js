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
import { loadBuild, greenFlag, step, tapKey, readVar } from './harness.js';

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

/** Green-flag to title, then press start and step until the game is playing. */
export function reachPlaying(vm, budget = 150) {
  greenFlag(vm);
  step(vm, 1);
  tapKey(vm, ' ');
  let t = 0;
  while (stateOf(vm) !== 'playing' && t < budget) {
    step(vm, 1);
    t += 1;
  }
  return stateOf(vm) === 'playing';
}

export { stateOf };
