# memory-pointer-public-safety negative fixture

This fixture proves `engine/check/memory-pointer-public-safety` bites a configured pointer committed to
the public construction template.

**Why it differs from the other fixtures (commit-before-bite):** the real check reads the committed
pointer via `git show HEAD:` (deliberately, so a maintainer's local `skip-worktree` edit is ignored).
The meta-check's seam (`ENGINE_POINTER_REL`) redirects that `git show HEAD:` read at `pointer.json`
here — so this fixture only bites once `pointer.json` is **committed at HEAD**. The live witness is the
CI run on the committed PR; a local run before committing the fixture will (correctly) not yet see it.

The coordinates here are obviously-fake placeholders. The real check reads the real pointer path, never
this one, so committing this fixture never trips the real gate.
