---
name: engine-bad
description: A negative fixture skill for engine/check/skill-coherence — operator-typed but not locked.
invocation: operator-typed
---

# engine-bad (negative fixture)

This skill declares `invocation: operator-typed` but does not carry `disable-model-invocation: true`,
so the model could still self-invoke it — the safety mismatch the coherence gate must catch.
