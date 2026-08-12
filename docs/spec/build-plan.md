# Build order

Phases run in the order they first appear; each row links a capability to the document a build checks
its work against. Sequencing detail within phases lives in [the engineering plan](../BUILD_PLAN.md)
(slices 2a–21), which this order groups; where they disagree, this file is normative for what must be
built, the engineering plan for how it is staged. The regression-recovery work (issue #13) is the first
item of the Recovery phase.

The [dependency-aware roadmap manifest](../roadmap/manifest.json) is the checked projection from these
capabilities and the engineering slices to GitHub component leaves. It does not change this order. A
leaf under a draft capability document is provisional and cannot close until that document is settled.

| Phase | Capability | Doc |
| --- | --- | --- |
| Recovery | Player craft and weapons | [Player craft and weapons](player-craft-and-weapons.md) |
| Recovery | Area progression and terrain | [Area progression and terrain](area-progression-and-terrain.md) |
| Recovery | Core game systems | [Core game systems](core-game-systems.md) |
| Foundation | Core game systems | [Core game systems](core-game-systems.md) |
| Foundation | Scoring, lives, and game over | [Scoring, lives, and game over](scoring-lives-and-game-over.md) |
| Campaign | Area progression and terrain | [Area progression and terrain](area-progression-and-terrain.md) |
| Campaign | Difficulty and formations | [Difficulty and formations](difficulty-and-formations.md) |
| Enemies | Aerial enemies | [Aerial enemies](aerial-enemies.md) |
| Enemies | Ground objects | [Ground objects](ground-objects.md) |
| Boss and secrets | Andor Genesis | [Andor Genesis](andor-genesis.md) |
| Boss and secrets | Secrets | [Secrets](secrets.md) |
| Cabinet | Cabinet flow | [Cabinet flow](cabinet-flow.md) |
| Polish | Audio and presentation | [Audio and presentation](audio-and-presentation.md) |
