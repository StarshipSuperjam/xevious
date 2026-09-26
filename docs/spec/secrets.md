---
status: locked
reference_verified_at: 71473685a8c7856c8401c8519276cd97a38d4183
---

# Secrets

Covers mechanics catalog rows SEC-01 through SEC-03. Values cite the pinned reference
(`reference_pin` in [the index](index.md)) as `file label lines`; citations are `src/xevious_main.68k`
unless noted. Score values are owned by
[Scoring, lives, and game over](scoring-lives-and-game-over.md).

License status of extracted values: the reference states no reusable license (recorded in [the index](index.md) and every data file).

## Summary

Three hidden things reward the curious bomber: Sol Towers that rise from empty ground, Bonus Flags that
grant points or a life, and one hidden credit message. All three follow the same idea — an invisible
object at a scheduled map position, revealed by a bomb — and their locations are part of the committed
area schedules, so the secrets sit exactly where the arcade put them.

## Behavior

**Sol Towers (SEC-01).** A Sol Tower is scheduled as an invisible ground object
([data/area-schedules.json](data/area-schedules.json) records every placement). A bomb hit on its hidden
position reveals it: it scores its reveal award and rises through a seven-step animation of ~112 frames
(~1.9 s), growing to double size partway (`handle_1D_Sol_Tower`, `handle_sol_tower_rising`,
`sol_tower_risen` 3010–3076). Once fully risen it is an ordinary bombable target; destroying it scores
the same value again — two scoring stages, reveal and destroy. (The reference's always-visible switch is
a development option, excluded.)

**Bonus Flags (SEC-02).** A Bonus Flag is scheduled invisible with a randomized vertical placement drawn
from the shared random stream. It is revealed by bombing it: a bomb landing on the still-hidden flag sets
it hit and, exactly like any bombed ground object, both makes it visible and scores its point value —
1,000 points. The reveal and its award are that one bomb hit, not two events, and once revealed the flag
sits in the hit state, so a second bomb cannot re-score it. Collection is then by flying the craft over
the revealed flag — proximity of the craft, not a weapon. Collecting always plays the flag sound and
removes the flag, and awards per the cabinet DIP switch: an extra craft, or 10,000 points
(`handle_54_Bonus_Flag`, `reveal_bonus_flag`, `score_bonus_flag`, `check_flag_collected` 3131–3188). The
flag rides the shared ground-bomb award loop, which sweeps all sixteen ground object slots — the flag
occupies one — and awards each active on-target object its point value
(`handle_bombed_obj_and_award_points` 2597–2627, `check_object_on_target` 2629–2640); the flag's point
index (`_PTS`=48, the value table's 1,000-point entry) is live data awarded on the reveal, not dead data.

**Hidden credit message (SEC-03).** One scheduled invisible object, when bombed, displays a hidden
message for 128 frames (~2.1 s) and awards the scoring table's minimum value; in attract mode the
object is silently removed instead (`handle_53_Easter_Egg`, `check_copyright_strings`,
`display_easter_egg` 5985–6048). The build reproduces the *event* — trigger, duration, minimal score —
but per the reference policy's in-game-text rule the displayed wording is **this project's own original
text**, never a transcription of the arcade's credit strings (a recorded deviation: the mechanic is the
secret, not the wording). The catalog's `uncertain` flag on this row stays until arcade observation
confirms the trigger's presentation details.

## Acceptance criteria

| Criterion | How verified | Who checks it |
| --- | --- | --- |
| Every secret's scheduled position comes from the committed schedule data, never invented placements | Data-comparison fixture: the build's secret placements equal the schedule records | engine |
| A bomb on a hidden Sol Tower reveals it, it rises in stages, and a second bomb destroys it — scoring at both stages | Play area 1's known tower position (per the committed schedule): reveal, then destroy | operator |
| Bombing a hidden Bonus Flag reveals it and scores 1,000 points once; a second bomb does not re-score it | Fixture: bomb the flag's slot, assert a single +1,000 award and that it becomes visible | engine |
| A revealed Bonus Flag is collected by fly-over, not by weapons, and awards per the configured setting | Play: reveal a flag, collect it, observe the award | operator |
| The hidden credit event triggers only when bombed in a live game and holds ~2 seconds | Play (or accelerated fixture) at its scheduled position | operator |
| The Bonus Flag's vertical placement draws from the shared random stream (seeded runs repeat) | Seeded fixture: identical seeds place the flag identically | engine |
