# Runtime harness — a pre-playtest regression tripwire

This is a small headless test harness that loads the shipped build
(`dist/Xevious.sb3`) into the **official** `scratch-vm`, drives scripted input, and
asserts on game-state variables and clone counts. It runs before the operator's
playtest to catch logic/state regressions early.

**It is not a gameplay gate, and it verifies nothing on screen.** It runs with no
renderer, so it can observe deterministic logic but cannot see anything rendered. The
operator's Scratch 3 playtest remains the sole gate for all gameplay — see
[../docs/principles.md](../docs/principles.md) and
[../docs/PLAYTEST_CHECKLIST.md](../docs/PLAYTEST_CHECKLIST.md). A green harness never
advances a pull request out of draft and never lets a playtest step be skipped.

## What it can and cannot observe

**Can (deterministic state/logic):**

- Stage and sprite-local variables (game state, `bomb in flight`, `scroll step`,
  reload counters, `tick`, `state epoch`…).
- Clone counts (e.g. live player-shot clones).
- Broadcast-driven state transitions and keyboard-driven logic.

**Cannot (stays the playtest's job):**

- Rendered pixel/sprite collision. The VM runs without `scratch-render`, so
  `touching` reporters read false. (A player shot used to end on `touching frame_t`,
  which never fired headless; since the frame borders were removed (PRES-01) it expires
  by position at arcade row 0, so the shot ceiling *and* the replenish are both asserted.)
- How sprites are layered and drawn (rendered pixels, z-order on screen), audio, and
  overall feel. The VM does track a clone's `visible` flag and current costume, and
  scenarios read those as state.

## Division of labor with the Python suite

- **Python (`tests/`)** owns static structure, provenance, data, and byte-determinism —
  what a script can decide by reading `project.json`.
- **This harness** owns dynamic runtime *outcomes* — what the running logic does over
  time. It must **not** re-encode block-graph shape assertions the Python suite already
  makes; if you find yourself asserting structure here, it belongs in Python.

## Identifiers and determinism

- Variable names/scopes come from `../src/xevious/runtime_identifiers.json`, emitted by
  `tools/game_director.py`. The harness resolves ids through
  [`lib/identifiers.js`](lib/identifiers.js) and hard-errors on a missing id, so a
  generator rename becomes a red test, not a silent pass.
- Determinism is of *outcomes*, not pacing. With no renderer, one `runtime._step()`
  advances the game to a settling point — an unfixed, machine-speed-dependent number of
  internal ticks, not one tick — so scenarios assert pacing-invariant state (ceilings,
  sticky flags, reachability), never exact timing. The project's own fixed-seed RNG
  covers the random stream. Frame-accurate stepping would need a renderer or a patched VM
  (what Whisker uses); this net deliberately does not depend on it. It never calls
  `vm.start()`.

## Fidelity caveat

A green run certifies the **pinned** `scratch-vm` (see `package.json`), which is the
same engine stock Scratch 3 runs but not guaranteed identical to the exact version in
the operator's Scratch 3 / TurboWarp. Treat it as a close proxy for the played build's
logic layer, not proof of it.

## Running

```bash
harness/run.sh            # builds the .sb3 from source, then runs the harness
```

Or in two steps:

```bash
python tools/scratch_project.py build   # writes dist/Xevious.sb3
cd harness && npm ci --ignore-scripts && node --test
```

The "No storage module present" warnings on load are expected and harmless: without a
renderer the VM cannot build costume/sound skins, which does not affect logic.

## Adding coverage

Authoring a scenario for a new VM-observable behavior is part of building that behavior,
not a one-time seeding. Scenarios live in the catalog at
[`lib/catalog.js`](lib/catalog.js); add the behavior's `drive`/`assert` and a
`negativeMutation` proving that scenario's assertion actually bites.
