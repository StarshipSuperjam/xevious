---
status: draft
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Andor Genesis

Covers mechanics catalog rows BOSS-01 through BOSS-03. Values cite the pinned reference
(`reference_pin` in [the index](index.md)) as `file label lines`; citations are `src/xevious_main.68k`
unless noted. Score values are owned by [Scoring, lives, and game over](scoring-lives-and-game-over.md).

## Summary

The mothership: a fifteen-part composite that arrives at scheduled points in the campaign, fights as one
coordinated encounter, and either dies by its core or departs when the terrain scrolls past its scripted
exit point. It is the game's only boss and the sternest test of the entity and collision systems working
as one.

## Behavior

**Arrival and composition (BOSS-01).** The area schedule's *andor-genesis-start* record bulk-arms
fifteen objects in one step from the boss's own layout list (`src/xevious_sub.68k`
`sub_2_fn_20__andor_genesis_start` 544–563, `andor_genesis_data` 565–566; each placement appears in
[data/area-schedules.json](data/area-schedules.json)): one master motion controller, one central core,
four corner gun ports (bottom-right, bottom-left, top-right, top-left), and nine armor plates forming
the 3×3 body (handlers 5386–5983, sprite layout per handler). The composite moves as one — parts follow
the master's motion — and animates its ports and core.

**Fighting it (BOSS-02).** The nine armor plates are indestructible: they are born in the already-hit
state and never react to weapons; they exist to be the body (all nine handlers share this pattern,
5771–5983). The four gun ports fire per the boss's fire-permission mask
([Difficulty and formations](difficulty-and-formations.md)) and are individually bombable. Bragza — the
boss's own projectile-spawning defense — emerges on the core's destruction sequence: the core object
converts in place to an indestructible Bragza that flies off under constant velocity (5486–5504; the
reference's own comment and its motion vector disagree about the flight axis — recorded as an observed
source discrepancy, resolution deferred to arcade observation). Enemy shots from the ports use the
shared bullet rules ([Aerial enemies](aerial-enemies.md)).

**Destruction and departure (BOSS-03).** Only bombing the core kills the boss: the core takes a bomb
like any ground target, scores its value, and its destruction cascades — each surviving gun port
auto-explodes when it sees the core hit (5523–5677, per-port checks). The shell slot's leftover-value
scoring bug is arcade behavior, preserved and flagged
([Scoring, lives, and game over](scoring-lives-and-game-over.md)). If the core is never destroyed, the
boss leaves when the schedule's *andor-genesis-end* record fires at its scripted scroll row — a
map-scripted exit, not a countdown timer (`src/xevious_sub.68k` `sub_2_fn_21__andor_genesis_end`
569–572; `andor_genesis_leave` 5426–5443) — and the encounter ends unrewarded.

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| The boss arrives at its scheduled rows with the recorded fifteen-part layout | Data fixture: build's boss layout equals the committed layout list; play confirms the arrival | operator |
| Armor plates never die; ports are individually bombable; only the core ends the encounter | Play a boss encounter exercising all three | operator |
| Core destruction cascades the surviving ports and awards through the single scoring path once each | Deterministic fixture over the destruction sequence | engine |
| An undamaged boss departs at its scripted scroll row, not on a timer | Accelerated schedule trace reaches the end record; play confirms the departure | operator |
| Port fire obeys the boss's fire-permission mask from the schedule | Seeded fixture: port fire patterns follow the recorded mask | engine |
| The composite stays within the entity budget with all fifteen parts plus normal traffic live | Performance soak on the built `.sb3` during a boss encounter | engine |
