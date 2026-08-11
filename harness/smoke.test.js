import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadBuild, greenFlag, step, readVar, constants } from './lib/harness.js';

// Foundational proof that the shipped build loads and runs headless. Everything else in
// the harness rests on this; if it breaks, no scenario result can be trusted.
test('the built .sb3 loads headless and rests in title', async () => {
  const vm = await loadBuild();
  assert.equal(readVar(vm, 'game-director-state'), 'title');
  greenFlag(vm);
  step(vm, 6);
  assert.equal(
    readVar(vm, 'game-director-state'),
    'title',
    'green flag should establish and hold the title state',
  );
});

// The manifest is the single source for identifiers and the shot-cap ceiling.
test('the identifier manifest exposes the shot-cap ceiling', () => {
  assert.equal(constants.shot_slot_count, 3);
});
