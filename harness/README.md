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
  `touching` reporters read false. Notably, a player shot's lifecycle *ends* on
  `touching frame_t`; headless, that never fires, so shot clones never expire and slots
  never replenish. The harness therefore asserts only the shot-cap **ceiling** (≤ 3
  live shots), never the replenish.
- Sprite visibility, layering, costume/skin state, audio, and overall feel.

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
- Determinism comes from the project's own fixed-seed RNG plus discrete stepping (one
  `runtime._step()` == one tick). The harness never calls `vm.start()`.

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
not a one-time seeding. New scenarios are declarative data in
[`scenarios/`](scenarios); add the behavior's expected end-state there and a negative
fixture proving that scenario's assertion actually bites.
