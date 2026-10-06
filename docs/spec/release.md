---
status: locked
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Release validation

Covers the two release roadmap leaves, `release.full-soak` (RELEASE-01) and `release.audit` (RELEASE-02),
delivered in slice 21 of [the engineering plan](../BUILD_PLAN.md). Unlike the other capability documents
this one adds no gameplay: it says what must be shown about the finished game before it is called a release
candidate, and how. Gameplay behaviour stays owned by the other documents in [the index](index.md); where a
soak finds the build disagreeing with them, the build is fixed against the pinned reference, never this
document relaxed.

## Summary

Two gates stand between the last gameplay change and the release candidate. The **soak** runs the whole
campaign the way a player meets it — every area in order, the loop from area 16 back to area 7, deaths, game
overs, two players, and the cabinet flow around them — and shows that nothing leaks, starves, or drifts. The
**audit** shows that what ships is clean: every piece of media is named with its source, every catalog row
is accounted for, no debug control is left in the game, and the documentation describes the build that
ships.

## Behavior

**The soak (RELEASE-01).** The soak is a set of headless runs on the built project plus one operator run of
the final `.sb3`.

- *The campaign.* An accelerated real game runs from area 1 through area 16 and loops into area 7, with the
  craft kept alive by the harness's dormant invulnerability hook (a variable the game itself never sets) and
  driven so that it moves and fires. Every area's schedule is consumed in full, `add_object` records
  included; a record the arcade itself would drop (its slot busy that frame) counts as consumed and is
  logged.
- *The clone and list envelope.* Scratch allows 300 clones. Through the campaign, the title and attract
  screens, initials entry, and a two-player game, the peak stays at least 50 clones under that limit, and
  the clone count and the game's lists return to their baseline after each death, each game over, and a
  stop and reload.
- *Repeatability.* With the same random seed, two campaign runs give the same trace at every area entry
  (the seeded generator of [Core game systems](core-game-systems.md), SYS-04).
- *Stop and reload.* Stopping the project mid-game and starting it again leaves no leftover clone, sound,
  or state from the stopped game.
- *Scratch 3 acceptance.* The operator plays the final `.sb3` in Scratch 3 through the release checklist in
  [the playtest checklist](../PLAYTEST_CHECKLIST.md): title to game over, both players, both weapons, a
  representative enemy roster, the secrets, Andor Genesis, high scores and initials, the area loop, and stop
  and reload.

Any defect the soak finds is fixed in the same delivery, each fix recorded like any other mechanics change.

**The audit (RELEASE-02).**

- *Media.* Every image and sound in the built `.sb3` appears in [the asset credits](../ASSET_CREDITS.md)
  and in its provenance record, and every credited asset is in the build; no ROM data and no media without a
  recorded source ships. The rights caveat on reference-derived graphics stays stated.
- *Catalog.* Every row of [the mechanics catalog](../MECHANICS_CATALOG.md) is built, excluded, or an accepted
  deviation recorded in a mechanics record; every Uncertain marker in the capability documents is either
  resolved against the pinned reference or accepted with its reason.
- *No debug controls.* The summon and isolation keys used during development are gone from the shipped
  build. The harness invulnerability hook stays: the game never sets it, and no key or control reaches it.
- *Documentation.* The README carries the release notes, and the engineering plan and catalog describe the
  build that ships.

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| Every area 1–16 and the loop into 7 runs with its schedule fully consumed | Headless soak run over the built project, recording every schedule step | engine |
| Clone peak stays at least 50 under 300 and returns to baseline after deaths, game overs, and stop/reload | Headless soak counting clones and list lengths at every tick | engine |
| Two seeded runs give the same trace at every area entry | Headless determinism run, compared trace by trace | engine |
| The final `.sb3` plays correctly through the release checklist in Scratch 3 | Operator plays the release checklist on the final build | operator |
| Every asset in the build is credited with its source, and every credited asset ships | Test cross-checking the asset credits, the provenance records, and the built `.sb3` | engine |
| Every catalog row is built, excluded, or an accepted deviation; no Uncertain marker is left open | Catalog and capability-document review recorded in the audit | operator |
| No debug control remains in the shipped build | Structural test over the built project | engine |
