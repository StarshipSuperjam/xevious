---
title: Describing what you want built
---

## What this covers

This is for the person who wants to tell the engine what to build and have it written down clearly enough that
the work which follows stays true to it. By the end you will know what the `engine-design` command does, how
to start it, where what you write is kept, what "settling" a description means, and what happens when you hand
it to a build.

## What you need to know

Before building anything, it helps to write down what you actually want — in plain language, organized so
nothing important is forgotten. The engine does this with you through one command, `engine-design`. You
describe what you want; it lays out the pieces, helps you write each one up, checks that every part is present
and well-formed, and keeps the result as ordinary files in your project under a folder called `docs/spec/`.

You stay in control of how much detail to capture, but the default is the full write-up. Alongside the
description of each capability under `docs/spec/`, the engine also writes the guiding principles behind your
product, an overview of how it fits together (with a simple diagram), and the guides the people who use it will
need. If you would rather capture just enough to get moving, say so and the engine keeps it light — the
description alone — and you can add the rest whenever you like; your choice is written down, so nothing is
assumed. The engine checks these fuller write-ups the same way it checks your description — that every part is
present and well-formed — but it never judges whether the design itself is *right*; that stays your call (and
your reviewers').

Each piece of your description moves through three plain stages as it matures: **not yet described** (a piece
you have named but not written up yet), **in progress** (you are writing it), and **settled** (you have looked
it over and accepted it as the description to build from). Settling something is your decision — the engine
never settles a description on its own. A settled description is not frozen forever: you can change or reopen it
later, but not quietly. When a change touches a settled part of your description, the engine asks you to confirm
it on the pull request — by applying the `guardrail-ack` label — before it can merge, so the record always shows
the change was deliberate, never a silent edit.

When you make a significant choice — settling something you weighed real alternatives for, reopening something
already settled, or adding or dropping a whole piece — the engine can also write a short **decision record**:
what you decided, why, and what you ruled out and why. These are kept as plain files under `docs/adr/`, numbered
in your project's own sequence, and they are what a later session reads before re-opening a choice, so it does
not re-argue ground you already settled. For a decision record the engine makes one small extra content check —
that each record it wrote still names what you ruled out — never whether your reasons were the right ones
(that stays your call). A record you keep in some other style is left untouched.

Once a piece is settled, you can hand it to a build. Two things follow, at two different moments. Right away,
the engine writes a **build order** — your settled pieces grouped into ordered, plainly-named phases — and opens
a **list of things to build**, one tracked item per piece, each pointing back at its description. Later, when a
build actually runs, that order is what groups the work into **visible phases you can watch progress against** —
the phases appear when a build is under way, not the moment you settle. And once there is a build order, a
settled piece left out of it will hold a merge until it is added, so nothing you settled is quietly dropped.

One thing the engine is careful about: when it checks your description, it is checking that every part is
*present and well-formed* — not whether the design is *right*. Whether the idea is a good one is your call (and
your reviewers'); the check only makes sure nothing is missing or malformed.

Everything lives in your own project as readable files, so you can open `docs/spec/` any time and see exactly
what was written. If your project's GitHub connection is not reachable, the engine still writes and checks your
description from files and tells you so, rather than stopping.

## Finding the commands

You start this by typing **`/engine-design`**. To see this and everything else you can ask the engine for, run
**`/engine-help`**, or type **`/engine`** to narrow your assistant's command menu to just the engine's
commands. You can also simply ask, in plain words — "help me describe what I want to build."

## Where to go next

When you are ready, type `/engine-design` and describe what you have in mind — the engine takes it from there,
one step at a time. If you would rather get oriented first, run `/engine-help` to see everything the engine can
do.
