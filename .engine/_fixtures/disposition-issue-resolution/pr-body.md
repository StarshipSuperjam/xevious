<!-- Negative fixture for engine/check/disposition-issue-resolution (#292). A seeded PR body whose Review
     section cites a sentinel-nonexistent issue (#999999999, a reserved never-allocated number that resolves as
     a clean 404 against the repo under check). Witnessed LIVE by the negative-fixture meta-check: run with a
     token, the cited number resolves to nothing, so the check emits the aimed `unresolved` finding. Run offline
     it emits the distinct `unevaluable` (outage) finding, the asserted token is absent, and the meta-check
     reddens loudly — never a false witness. -->

## Purpose

A seeded pull-request body, not a real change.

## Review

A plain review ran. One finding was real but out of scope, so it was logged as a follow-up: tracked as #999999999.
