---
title: Read an existing project to get grounded — the brownfield onboarding read
---

## Purpose

How the engine gets to know a project it has just **joined**. On a project that already existed before the
engine arrived, the engine is a new contributor: it has none of the memory a project it grew up with would
carry. So the **first thing to do after setup — before any build** is a read: go through the existing project
in Explore mode and write down what matters, so that every session after this one starts grounded instead of
re-reading the whole project from scratch. This is not part of first-run setup — setup places the engine's
files; this reads the project's. Enter this right after the arrival pull request is merged (the engine-arrival
runbook hands off here), or any time the operator asks the engine to "get up to speed" on the project. It only
reads the project and writes to the engine's own memory; it changes none of the project's files.

## Steps

Run these in **Explore** mode — reading and note-taking, not building. If the session is in Build, say so and
switch back to Explore first; nothing here needs to write a project file.

1. **Confirm the stance and the shape of the read.** Make sure the session is exploring, and tell the operator
   plainly what you are about to do: read their project to get grounded, write nothing to their files, and save
   what you learn to the engine's memory so future sessions start from it. Ask if there is anything in
   particular they want you to understand first.
2. **Read the project from the outside in.** Start with what orients a newcomer — the README, any `docs/`, the
   top-level layout — then the code and configuration that carry the project's real shape: how it is structured,
   what its main pieces are, how they fit together, how it is built and run, and the conventions it follows.
   Read enough to reason about the project; you are building understanding, not a catalog.
3. **Write down what will matter next time — to the engine's memory, not the project.** Capture the durable
   understanding by pinning it (`pins.py add`), so it survives into later sessions: what
   the project is and does, how it is laid out, the conventions and decisions that are not obvious from any one
   file, and anything that would trip up a session that had not read it. Keep it to what is durable and
   non-obvious — not a restatement of what the code already says plainly.
4. **Tell the operator what you learned, and what you did not reach.** Give them a short, plain-language summary
   of your understanding of the project and name the parts you did not get to or were unsure about — so they can
   correct a wrong read or point you at what you missed before it hardens into memory.
5. **Hand off to the first build.** With the project understood and the understanding saved, the engine is ready
   to build against it. Point the operator at how to start the first piece of work — to **describe a new
   capability** first, `/engine-design` (it lays the description out with them and checks it holds together);
   to build from something already settled, a plan they approve or `/engine-start` — so the onboarding read
   leads straight into grounded work rather than a cold start.

## Done when

The engine has read the existing project in Explore mode, saved a durable, non-obvious understanding of it to
its own memory (so the next cold session starts grounded rather than re-reading the project), and told the
operator plainly what it now understands and what it did not reach — leaving the project's own files untouched
and the engine ready to build against the project it now knows.

## Notes

- **Read-only on the project, always.** This operation writes only to the engine's own memory; it never edits,
  moves, or commits a project file. Getting grounded is a read, not a change.
- **Grounding is not a one-time act.** The onboarding read is the first, deliberate pass so the engine is not
  cold on day one; memory keeps growing as the engine works on the project. Do not try to capture everything at
  once — capture what matters now, and let the rest accrue as it comes up.
- **Explore, not Build.** The read happens in the exploration stance by design: it reasons and remembers without
  changing the project, so the operator's first sight of the engine on their code is a careful reader, not an
  eager editor.
