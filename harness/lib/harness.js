// Headless scratch-vm harness core.
//
// This is a PRE-PLAYTEST INTERNAL-STATE REGRESSION TRIPWIRE, not a gameplay gate. It
// runs the shipped build in the official scratch-vm with no renderer and reads back
// game-state variables and clone counts. It can therefore observe deterministic logic
// (counters, flags, broadcasts, clone allocation) but NOT anything rendered: pixel or
// sprite collision, visibility, layering, audio, or feel. Those stay the operator's
// playtest — see harness/README.md and docs/principles.md.
//
// Determinism here is of OUTCOMES, not of pacing. Scenarios assert on pacing-invariant
// state — ceilings, sticky flags, reachability — plus the project's own fixed-seed RNG.
// They do NOT assume a fixed number of ticks per call: with no renderer attached,
// scratch-vm never sets `redrawRequested`, so the sequencer runs threads to their settling
// point within a wall-clock budget — an unfixed, machine-speed-dependent number of internal
// ticks per `_step()`, not one tick. Frame-accurate (one-tick) stepping needs a renderer or
// a patched VM (what Whisker uses); this net deliberately does not depend on it, because
// timing and feel are the operator playtest's job. We never call vm.start().
import VM from 'scratch-vm';
import { readFileSync, statSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { REPO_ROOT, variable, constants } from './identifiers.js';

export const DIST_SB3 = resolve(REPO_ROOT, 'dist', 'Xevious.sb3');
export const PROJECT_JSON = resolve(REPO_ROOT, 'src', 'xevious', 'project.json');

const BUILD_HINT =
  'Build it first: `python tools/scratch_project.py build` (or run harness/run.sh).';

/** Fail loudly rather than silently pass against a missing or stale build (M3). */
export function assertFreshBuild() {
  if (!existsSync(DIST_SB3)) {
    throw new Error(`harness: ${DIST_SB3} is missing. ${BUILD_HINT}`);
  }
  if (statSync(PROJECT_JSON).mtimeMs > statSync(DIST_SB3).mtimeMs) {
    throw new Error(
      `harness: dist/Xevious.sb3 is older than src/xevious/project.json — ` +
        `it would test a stale build. ${BUILD_HINT}`,
    );
  }
}

/** Load the freshly built .sb3 into a rendererless VM. */
export async function loadBuild() {
  assertFreshBuild();
  const buf = readFileSync(DIST_SB3);
  const vm = new VM();
  vm.setTurboMode(false);
  await vm.loadProject(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
  // We drive the sequencer by hand (never vm.start()), so the runtime's per-frame time
  // budget is never initialised. Set it so the sequencer's WORK_TIME (0.75 * currentStepTime)
  // is a real number — otherwise it is NaN and threads never step at all. This bounds work by
  // WALL CLOCK, not by tick count (see the header note on pacing).
  vm.runtime.currentStepTime = 1000 / 30;
  return vm;
}

/**
 * Pump the runtime `times` (each call is one `runtime._step()`). With no renderer, a single
 * pump advances the game to a settling point — an unfixed number of internal ticks — so this
 * is NOT a tick count. Scenarios pump until a pacing-invariant outcome holds, never to assert
 * exact timing.
 */
export function step(vm, times = 1) {
  for (let i = 0; i < times; i += 1) vm.runtime._step();
}

export function greenFlag(vm) {
  vm.greenFlag();
}

export function keyDown(vm, key) {
  vm.postIOData('keyboard', { key, isDown: true });
}

export function keyUp(vm, key) {
  vm.postIOData('keyboard', { key, isDown: false });
}

/** Tap a key: down one pump, up one pump. */
export function tapKey(vm, key, { down = 1, up = 1 } = {}) {
  keyDown(vm, key);
  step(vm, down);
  keyUp(vm, key);
  step(vm, up);
}

function targetForScope(vm, scope) {
  if (scope === 'Stage') return vm.runtime.getTargetForStage();
  const target = vm.runtime.targets.find(
    (t) => !t.isStage && t.isOriginal && t.sprite && t.sprite.name === scope,
  );
  if (!target) throw new Error(`harness: no original target for scope '${scope}'`);
  return target;
}

/** Read a variable by display name within a given scope ('Stage' or a sprite name). */
export function readVariable(vm, scope, name) {
  const target = targetForScope(vm, scope);
  for (const id of Object.keys(target.variables)) {
    if (target.variables[id].name === name) return target.variables[id].value;
  }
  throw new Error(`harness: variable '${name}' not found on scope '${scope}'`);
}

/** Read a variable by its stable manifest id (resolves name + scope, hard-errors on miss). */
export function readVar(vm, id) {
  const { scope, name } = variable(id);
  return readVariable(vm, scope, name);
}

/** Count live clones of a sprite (originals excluded). */
export function cloneCount(vm, spriteName) {
  return vm.runtime.targets.filter(
    (t) => !t.isStage && !t.isOriginal && t.sprite && t.sprite.name === spriteName,
  ).length;
}

export { constants, variable };
