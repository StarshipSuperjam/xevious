# Xevious

A finished Scratch 3 port of the 1983 arcade game. Start at [README.md](README.md).

## Arcade reference fidelity

This game's behaviour is defined by the pinned arcade source `jotd666/xevious` at
commit `71473685a8c7856c8401c8519276cd97a38d4183`, not by `docs/spec/`. The spec is
a derived index of that source; where they disagree, the source wins and the spec
is corrected — never the build changed to match wrong prose.

- Get a verified local checkout once with `python3 tools/reference_checkout.py ensure`
  (a no-op with no network afterwards); print its path with `python3 tools/reference_checkout.py path`.
  Open the cited source lines before accepting any claim about how the game behaves.
- Every citation in `docs/spec/` and `docs/mechanics/` must resolve to a real label
  and line range at the pin: `python3 tools/reference_citations.py --checkout "$(python3 tools/reference_checkout.py path)"`.
- A build for a playtest is produced only through `python3 tools/playtest_package.py`,
  which runs the reference checks first and refuses to emit a build while any
  citation is unresolved.

The policy is `docs/REFERENCE_POLICY.md`; the citation grammar is in `docs/spec/index.md`.
