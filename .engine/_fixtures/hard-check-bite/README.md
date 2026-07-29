# `hard-check-bite/` — the meta-check's own negative fixture (self-coverage)

The negative-fixture meta-check is itself a `custom/script` check, so it must be provable-to-bite like every
other check it judges — otherwise the checker-of-checkers is unfalsifiable. This fixture is the seeded
**mini-scenario** the meta-check is pointed at (via the `ENGINE_ROSTER_KINDS` / `ENGINE_FIXTURE_ROOT` /
`ENGINE_ROSTER_DIR` env overrides its `main()` reads) to prove it goes red when a unit fails to bite.

`scenario-fixtures/kind-presence/` holds a **non-biting** presence fixture: its `input.md` is *complete* (every
required section present and filled), so the presence check does NOT fire — yet its `expect.json` says a bite was
expected. Run against this scenario, the meta-check must therefore emit a hard "did NOT catch" finding. This
fixture's top-level `expect.json` asserts exactly that.

The scenario contains no `custom/script` instance and does not contain the meta-check itself, so the run does not
re-enter the meta-check — the regress terminates with no meta-meta-check. The disjoint *missing-fixture* leg of
the negative-fixture completeness requirement (a unit present with no fixture at all) is exercised directly in `test_hard_check_bite.py`.
