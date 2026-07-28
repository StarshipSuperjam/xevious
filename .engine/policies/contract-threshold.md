---
title: Contract threshold
status: accepted
date: 2026-06-03
values:
  contract_rate_max: 3
---

## Rule

A decision is recorded as its own contract (a permanent decision record) only when all four of these hold: it is architecturally significant, it constrains future work, it is hard to reverse, and it has a genuine alternative that was seriously weighed and turned down. Every decision below that bar is recorded in the pull request's description instead, which carries it as the durable record. As a safety net, an unusual burst of new contracts is flagged for a look:

- Contract-rate signal (`contract_rate_max`): when more contracts reach the accepted state within any 7-day stretch than the limit set in this policy's settings block — the `values` at the top of this file — the next start-up raises a gentle "are decisions being over-recorded?" note. To change this limit, type `/engine-tune` — it saves your choice so an engine update won't undo it.

How the set of records changes — and stays small — follows three rules, so the body of contracts grows only on real decisions, never on routine work:

- **A new record is added only for a genuinely new decision** that clears the four-part bar above. Routine work adds no record.
- **An accepted record of your own is never edited to change what it decided.** If one of your engine decisions is re-opened and genuinely changes, that is written as a *new* record which says it supersedes the earlier one; the earlier one is kept, never rewritten, so the history of why stays auditable. (The engine's own founding records are the exception — you don't edit those at all: a change to one rides an engine release, which revises it in place and carries it forward wholesale.)
- **A small correction or re-confirmation that does not change the decision is not a new record.** It rides the pull request's description — the same durable place everything below the bar goes — rather than spawning another contract.

## Scope

Applies to every decision made while working inside this engine — the live question "does this deserve its own permanent decision record, or does it belong in the pull request description?" It does not govern decisions about the product the engine is helping to build; those follow the product's own conventions.

## Rationale

Permanent decision records are only valuable if they stay rare and meaningful. If every small choice becomes one, the record turns into noise nobody reads, and the genuinely important decisions are lost in the pile. This rule keeps records reserved for the significant, hard-to-undo choices and routes everything else to the pull request, where it still lives durably. The burst signal exists so that a non-engineer notices — without having to watch for it — if decision records start accumulating faster than expected. To adjust it, type `/engine-tune` — raise the limit if a genuinely busy stretch makes the note fire too readily, lower it if you would rather be warned sooner.

## Enforcement-tier

A layered control, held three ways:

- **Posture** — the bar itself (significant, constraining, hard to reverse, with a real rejected alternative) is a judgment the author and the reviewer apply. No machine decides whether a decision clears it.
- **Hard-fail** — a check blocks the merge if a contract's Significance or Anti-choice section is left blank or left as the template's placeholder. Its limit, stated honestly: it confirms only that those two sections are filled in with some real text — never whether the content is genuinely significant or a genuine alternative. That judgment stays yours at the pull request.
- **Soft-warn** — the contract-rate note above is a nudge at the next start-up, never a block. It fires when more of your own engine decisions reach the accepted state within a 7-day stretch than the limit in this file's settings block, and it counts only your own decision records — never the engine's own founding ones, which travel with the engine.
