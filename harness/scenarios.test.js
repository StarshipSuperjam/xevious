import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadArtifact, loadMutatedSource } from './lib/build.js';
import { SCENARIOS, EXCLUSIONS } from './lib/catalog.js';

// Each scenario runs twice against the SAME drive + assertions:
//  - positive: the shipped artifact must pass;
//  - negative: a mutated build that breaks exactly this behavior must FAIL the assertions,
//    proving they bite (a green run is not vacuous).
for (const scenario of SCENARIOS) {
  test(`${scenario.key}: ${scenario.behavior}`, async () => {
    const vm = await loadArtifact();
    const observation = await scenario.drive(vm);
    scenario.assert(observation);
  });

  test(`${scenario.key}: negative fixture proves the assertion bites`, async () => {
    const vm = await loadMutatedSource(scenario.negativeMutation);
    const observation = await scenario.drive(vm);
    assert.throws(
      () => scenario.assert(observation),
      (err) => err instanceof assert.AssertionError,
      'the mutated build should fail this scenario, but the assertions passed — ' +
        'the check does not actually bind',
    );
  });
}

// Completeness is checkable, not asserted by adjective: every catalog entry names the
// previously-regressed behavior it covers and the playtest step it maps to, and the
// exclusions name what stays the playtest's job.
test('coverage catalog is complete and honestly scoped', () => {
  assert.ok(SCENARIOS.length >= 6, 'the initial VM-observable behavior set is covered');
  for (const s of SCENARIOS) {
    assert.ok(s.behavior && s.behavior.length > 10, `${s.key} names its behavior`);
    assert.ok(Number.isInteger(s.playtestStep), `${s.key} maps to a playtest step`);
    assert.equal(typeof s.negativeMutation, 'function', `${s.key} has a negative fixture`);
  }
  assert.ok(EXCLUSIONS.length >= 3, 'playtest-only behaviors are named, so "complete" is honest');
});
