---
required_sections: ["Headline", "What this touches", "What I'll run", "How careful — your choice", "Your call"]
allowed_sections: ["If this weakens a safety guardrail"]
length_budget: 70
---

<!-- The plan-gate consent surface. The orchestrator fills this in plain language and shows it to the operator BEFORE doing the work, so the spend is approved before it happens. Two rules bind every line: (1) plain language only — never a review-pass's internal name, a depth number, or any engine vocabulary; (2) never say how long the work will take or name a cost — the engine has no method to know either, and a made-up number is the false confidence the trust model refuses. State what will RUN and let the operator judge the spend from that. Fill each <...>; the depth wording below is fixed copy, shown as-is. The suggested depth (the Headline's care level) is your judgment, derived from the change's risk alone — what review is installed and available, and what the change sits next to (its neighbours and whether it sits near known trouble); state only what genuinely shaped it. It is never lowered by a depth the operator has preferred before — the recommendation is risk-truth. On the fast path (a trivial single reversible change) this whole surface collapses to just the Headline, relayed as one plain line with the other sections skipped; a change that weakens a guardrail or touches a schema is never trivial — it fills the whole surface and its headline stays visibly weightier, so habituation never dulls the high-stakes consent. -->

## Headline

**<One plain sentence that varies with the change — what it touches and the care it suggests; this is the line that gets read, the detail below is what it cites. e.g. "This changes your sign-in flow and the database. I'd suggest a thorough review — security matters here.">**

## What this touches

<The parts of the project this change reaches, in plain terms — which areas and why they matter — so the operator can see how far it reaches.>

## What I'll run

<The review passes and checks this depth will actually run — the scope of the spend being approved — and what is missing. Never a time or a cost figure. If no review packs are installed, say so plainly: "No review packs are installed, so beyond the automatic checks this rests on your read at merge." If any part of the engine the review relies on is currently unavailable, say that in plain words too. If a review that runs at this depth would run the operator's code in a throwaway copy to judge it, say that plainly here — "to check this, the engine may run your code in a throwaway copy; it never touches your real project" — but ONLY when that is genuinely in scope for this change and depth, never as a blanket warning.>

## How careful — your choice

You choose how careful this should be; the suggestion above is the default. What each level actually adds depends on which review packs are installed — "What I'll run" above is the authoritative list for this change.

- **Quick check** — I look it over myself and run the automatic checks (the completeness and guardrail checks that run on every change) — no extra review passes. The lightest: least gets caught before it ships, so you lean most on your own read at merge.
- **Standard review** — the usual review passes for a change like this, where review packs are installed — a middle amount of checking.
- **Thorough review** — every review pass available, looking hardest at the risky parts — the most gets caught before it ships.

<Whenever no review packs are installed you MUST add this note, so the choice is never misread as buying review that will not run: "No review packs are installed yet, so standard and thorough currently run the same as quick — just the automatic checks. Installing a review pack is what adds deeper review.">

## If this weakens a safety guardrail

<Include this section ONLY when the change would weaken one of the engine's own guardrails — turning off a check, loosening a block, or editing the protection files. Name, in plain words, WHICH protection weakens and what the AI could then do unwatched, then the deliberate-confirm note. e.g. "⚠️ This change would weaken a safety guardrail: it turns off the check that blocks unreviewed merges. If it merges, I could change protected files without that check catching it. You'll confirm this deliberately at merge — a separate step from the normal merge click." Describe the actual protection THIS change weakens — do not reuse the example unless it genuinely fits. Delete this whole section when nothing weakens; strengthening a guardrail is never flagged.>

## Your call

<The consent ask in plain words: go ahead at the suggested depth, pick a different depth, or install a review pack first. Nothing starts until the operator approves.>
