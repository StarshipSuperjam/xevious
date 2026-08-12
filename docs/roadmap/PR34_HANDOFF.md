# PR #34 roadmap handoff

PR [#34](https://github.com/StarshipSuperjam/xevious/pull/34) remains the active slice-7 build in its existing Claude session. The roadmap migration did not change its branch, commits, draft state, body, labels, milestone, or Project fields.

Before that session submits the PR for merge:

1. Replace the stale `Part of #17` reference with `Closes #56` and `Part of #18`.
2. Do not close a slice-8 integration leaf. Enemy-dependent gameplay acceptance stays open until enemies exist in the playable build.
3. Reconcile the branch onto current `main` so the roadmap closure check runs.
4. After the operator tests the exact head commit, add the required commit-specific playtest record and `playtest-approved` label.
5. Verify GitHub's computed closing-issue list contains only [#56](https://github.com/StarshipSuperjam/xevious/issues/56).

Issue #56 is the leaf for difficulty models and live state. Parent capability [#18](https://github.com/StarshipSuperjam/xevious/issues/18) remains open until all of its native sub-issues close.
