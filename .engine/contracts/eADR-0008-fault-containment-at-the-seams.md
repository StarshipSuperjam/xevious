---
id: eADR-0008
title: Fault-containment is earned at the seams, not conferred by the shape
status: accepted
date: 2026-06-29
---

## Decision

The engine is a small trusted core plus optional extensions, but the property that keeps one capability's failure from spreading is the discipline at the shared seams — wiring that is keyed, idempotent, and reversible; coherence validation; and never shipping what was not selected — not anything the architecture's shape grants on its own. Three things follow and are fixed. "Composed of parts" never means "fault-isolated"; any isolation claim is attributed to the seam discipline, never carried silently by the word. The shared core stays minimal because it is contagious — a defect in a foundation reaches every project built with it — so each candidate foundation must justify why it cannot instead be an optional extension. And a foundation earns its own packaged boundary only when it owns non-regenerable per-instance data or a seam other parts bind to; otherwise its files ride the core.

## Significance

This locks in where the firewall actually lives: in the wiring, not the partition diagram. Later work may not infer isolation from structure — a capability is contained only to the extent the seam it sits on is keyed, reversible, coherence-checked, and one-sided. That makes this law dependent on the seam vocabulary defined by the wiring-and-files law (eADR-0009): the closed, reversible declaration of files plus wiring is the concrete mechanism this containment claim rests on, and without it the claim is unsupported. Two further obligations bind downstream design: keep the shared core small, since every foundation in it is contagious by construction, and admit a new foundation to its own boundary only on the earned-boundary test above. The microkernel framing may be used only as an inspired-by analogy with its limit stated — true isolation comes from address spaces, while these extensions share mutable files.

## Rationale

Calling a system "composed of modules" is exact, but the inference "modular, therefore isolated" is false here and dangerous: these parts share mutable files rather than address spaces, so nothing about the shape contains a blast. Renaming the shape — to "microkernel" or anything else — would only re-mint the same false inference in fresh words. Naming the attribution instead, that isolation is earned at the seams, is the durable fix, and it is what justifies both keeping the trusted core minimal and investing rigor in the wiring rather than the diagram. The earned-boundary rule extends the same logic to packaging: optionality cannot decide structure when every foundation always ships, so a boundary is justified only by owned irreplaceable data or a bound seam — real coupling — not by ceremony.

## Anti-choice

The strongest rejected alternative was to rename the shape to "microkernel" and let the name carry the isolation guarantee, treating the partition itself as the containment story. It loses because it reproduces the exact category error it claims to solve: a real microkernel isolates through address spaces, whereas these extensions share mutable files, so the name would promise a firewall the structure does not provide and a later reader would re-derive "shaped like a kernel, therefore isolated." Leaving the attribution implicit was rejected for the same reason — the unstated assumption is precisely what manufactured the false inference in the first place. Giving every foundation its own packaged boundary regardless was also rejected: a boundary for a foundation that owns no irreplaceable data and no bound seam is ceremony without payoff, and it buries the migration of genuinely irreplaceable data inside a monolithic unit instead of an owned, legible one.

## Status

accepted
