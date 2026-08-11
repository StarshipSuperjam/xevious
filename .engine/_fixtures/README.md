# `.engine/_fixtures/` — the negative-fixture namespace (test data, not a surface)

This directory holds **negative fixtures**: deliberately-broken inputs the engine runs its own `hard` checks
against to prove each one actually *bites* (it catches a known-bad example, rather than passing green while doing
nothing). It is the standing, CI-enforced "checker-of-checkers" the negative-fixture meta-check uses.

The design: the meta-check law, the by-presence fixture grammar, and this reserved namespace as a Tier-2 leaf
under `.engine/`.

## What lives here

A negative fixture is **bound to its logic unit by presence** (a naming/location convention, not a rule field —
the check schema is unchanged). Each in-scope hard check-logic unit gets one subdirectory, discovered by name:

- a **check-kind callable** (the closed core kinds, plus any module-added kind) → `kind-<kind>/`
  (e.g. `kind-schema/`, `kind-coverage/`). A module-added kind's fixture lives here too, **bound to the kind
  by this naming convention** — this is what the design means by "co-located with the callable": the binding
  is by presence, not physical adjacency (the callable lives at `.engine/tools/<module>/kind_<kind>.py`; every
  fixture, module or core, lives only here). The kind is owned by its module and removed on uninstall; a fixture
  left behind after its kind is gone is **inert** (pruned + coverage-exempt, below) — never a stranded gate. So a
  module `provides` its kind callable, **not** its fixture.
- a **`custom/script` check instance** → `<check-id-stem>/`, the rule id minus `engine/check/`
  (e.g. `census-completeness/`)

Each unit directory holds a `rule.json` (the transient rule the meta-check runs), the seeded bad input (a single
bad file; or, for the repo-global `coverage`/`coherence` kinds, a malformed mini-tree or a `manifests.json` data
literal), and an `expect.json` sidecar. The sidecar declares **`{"severity": ..., "message_contains": ...}`** — the
meta-check asserts by **set-membership** that a finding of that severity carrying that message token is present
(never order/count). The token is required, and it is what distinguishes the unit's *intended* finding from an
unrelated bite: a fixture that fail-closes for the wrong reason fires a hard finding but not the expected one, so it
does not satisfy the assertion. A unit with no statically-decidable CI failure path instead carries a
`not-applicable.json` whose `property` is exactly `"no statically-decidable failure path in the CI environment"`
(verbatim — a compressed slug would reopen the self-classification escape this exact-string rule closes); the meta-check lists
every such carve-out loudly and it is re-derived at the review gate.

A unit whose failure path is reachable only in the engine's OWN home repository instead carries a
`construction-scoped.json` whose `property` is exactly
`"no reachable failure path outside the construction repository"` — the same verbatim discipline, and honored
only where the checkout's git origin is not the update home its manifest records. **That filename and that
string are a FROZEN on-disk format.** They keep the retired "construction" wording on purpose: the check
script and the declaration reach a deployed repo by two different delivery paths (the engine overlay and the
fixture reconcile), so changing either side turns the meta-check hard-red in a repo running an engine old
enough to receive one without the other — and this namespace is a never-delete carve-out of that reconcile, so
a renamed file's old copy would strand permanently. The engine's prose vocabulary moved on; this format did
not. `TestLiveDeclarationsAreHonored` pins both sides against the shipped files.

**Every fixture must live under `.engine/_fixtures/` and nowhere else.** The exclusions that shield these files
are anchored on this exact path, so a fixture placed outside the namespace (or under a near-miss sibling like
`_fixtures-schema/`) would get none of the shielding and red the real suite.

## Why it is invisible to the real checks

These files are **test data, not a governed surface**. They are intentionally excluded from the live validation
suite so a committed bad input neither reds the real checks nor reads as an orphan/uncatalogued surface:

- `catalog-coverage` lists `.engine/_fixtures/` as infrastructure (not a catalogued surface).
- `link-integrity` excludes it (a fixture's deliberately-broken Markdown link must not fail CI).
- the module-coherence ownership walk prunes it (no module `provides` a fixture).

The knowledge graph never fingerprints these files (it entitizes only files that are *both* claimed by a module's
`provides` *and* under a catalogued surface location — fixtures are neither), so nothing here needs a `graph.json`
regen.
