<!-- The body template for a BUILD ISSUE — the forward plan for a build distributed across more than one
session ([build-orchestration] step 1). The orchestrator authors this at Plan and applies the engine-domain
label. The checklist is the machine-readable progress record ("N of M done" — the next unchecked item is
the next chunk); the scope-lock is the permitted write-scope (the union of the chunks' declared paths).
Fill every <...>; keep the section headings (they are read as structure). Plain language — an approver who
is not an engineer reads this. -->

## Goal

**<One sentence: what this build delivers and why — the outcome, not the mechanics.>**

- <Supporting detail: the locked source or originating Issue this builds to; add bullets as needed.>

## Plan — commit sequence (0 of N done)

<The ordered chunks this build lands, one per planned commit. The next unchecked box is the next chunk; a
worker that fails leaves its box unchecked (the plan plus git state are the record — there is no phantom
slot). Update the "0 of N" count as boxes are checked.>

- [ ] <Chunk 1 — what it adds/changes, in plain terms.>
- [ ] <Chunk 2 — …>
- [ ] <Chunk 3 — …>

## Scope-lock — where this build may write

<The permitted write-scope: the union of the planned chunks' declared paths. Work stays inside this scope;
a change that needs a path outside it is re-planned here first, not written silently.>

- `<path/or/glob the build may write>`
- `<another path/or/glob>`

## How it's reviewed and merged

- **Review depth:** <the depth agreed at Plan — what review passes will run, or that none are installed.>
- **Merges as:** a single reviewed pull request through the protected-branch gate; this Issue closes as its
  commits land.

## References

- Builds to: <link the locked source / originating Issue / spec being satisfied.>
- Pull request: <link the PR once opened.>
