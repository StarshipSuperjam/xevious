---
title: Closing a turn — the disposition gate and ambient capture
---

## Purpose

Keep a turn honest about what it raised. Every turn ends through a `Stop` hook that does exactly two
things: it relays the turn's notes to memory, and it makes sure **every concern the session raised this
turn reaches a decision before the turn ends** — fixed in line, saved as a tracked follow-up, or flagged
for a human. Enter this runbook whenever you need to understand or explain why a turn was held open, what
"saved as a follow-up" did, or why the engine never simply drops something it noticed.

## Steps

The mechanism is `.engine/tools/close.py`, wired as the `Stop` hook in `.claude/settings.json`. The
disposition gate reads an ephemeral, session-scoped checklist of the concerns raised this turn; the
lifecycle:

1. **Raising a concern records it.** Under the standing pushback habit, a concern the session surfaces is
   written to the checklist, undispositioned (`close.py record`). The checklist lives outside the
   repository, only for this session, and is never committed — it is a working list, not an archive.
2. **A concern needs a decision before the turn ends.** While the checklist holds an undispositioned
   entry, the turn is **held**: the session is pushed back to give that concern one of three durable
   outcomes — **fix it in line**, **save it as a tracked follow-up item**, or **flag it for a human**
   (`close.py dispose`). A turn that raised nothing, or whose concerns are all decided, ends normally.
3. **A run of pushback can't trap you.** If the decision keeps not being made, the platform force-ends the
   turn after a bounded number of holds. At that point any still-open concern is **automatically saved as
   a tracked follow-up item so it is never lost**, and the turn ends — it can never deadlock.
4. **Unattended runs are satisfiable too.** A scheduled (unattended) run can't ask a human, so it discharges
   an open concern by saving it as a tracked follow-up — the same "save it" outcome, applied without a human.

To see what the gate decides without ending a real turn (the operator demo): `python tools/close.py demo`,
or drive the pieces directly with `record` / `pending` / `dispose` / `summary` / `clear`.

## Done when

A raised concern is held until it has a decision; a turn that raised nothing (or settled everything) ends
quietly; and a forced end-of-turn saves any leftover as a tracked follow-up rather than dropping it. When
everything raised has been handled, the session can state it plainly — "everything I flagged this turn is
handled — 1 fixed, 1 saved as a follow-up item" — instead of leaving you to re-read the transcript.

## Notes

The gate is a **strong default over what was recorded, not an absolute guarantee** — stated honestly, never
overstated. It can only hold a turn on a concern that was actually written down (writing concerns down is the
session's discipline); a concern noticed but never recorded is not caught here. The one unbypassable backstop
is **the human review when a change is merged** — the change set is reviewed there regardless of anything the
turn-end gate did. If the gate itself cannot run, it lets the turn **end** (it never strands you mid-turn) and
says so, with the work flagged for a closer look.

Two honesty notes on what you see. The **hold** (the pushback that keeps a turn open until a concern is
decided) is the reliable signal — it is delivered straight to the assistant, which acts on it and, because no
hook channel reaches your screen, relays it to you in plain words: a concern that needs your decision is
passed on **emphatically** so it is never silently dropped. The **clean end-of-turn summary** ("everything I
flagged this turn is handled…") is narrated by the assistant, not printed by the gate, and is **quiet when
nothing needed action** — no per-turn banner; a saved follow-up's durable record is the tracked item
itself, which the engine brings back to you in plain language the next time it starts up. When the engine
saves a leftover as a tracked item, it only ever creates and touches items **it opened and labelled itself** —
it never changes or closes the issues you created. And by default a turn is held **only** when the session
explicitly flagged something this turn — an ordinary turn ends without interruption.

Ambient capture is **memory's** mechanism, not this gate's: closing a turn only *triggers* it and never
holds the turn on it. The memory store has shipped, so the trigger is live: closing a turn relays the turn's
delta to memory's ledger. It stays best-effort and fail-soft — any fault is a silent no-op and never holds or
strands the turn. This runbook covers turn close only; submitting the finished work as a pull request is
the build-orchestration runbook's.
