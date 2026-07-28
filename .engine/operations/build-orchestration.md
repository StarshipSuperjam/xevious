---
title: Build orchestration — how Build work happens, from draft pull request to submitted pull request
---

## Purpose

How a Build session turns intent into a merged change. An orchestrating session opens a **draft pull
request** (the claim), plans the work as an ordered commit sequence, has the plan and then the result
reviewed by cold-context passes at a depth the operator approves, integrates as the **single writer of the
final commits**, and **submits the pull request for the operator's merge** — the only unbypassable gate.
The submitted pull request is the close; the forward plan, when written, lives in a build Issue. Enter
this runbook to understand or explain how a build is planned, reviewed, assembled, and submitted, and why
each gate exists.

## Steps

The review passes at each gate are **derived** by querying each installed review persona's own `role`/`lens`
frontmatter — so a standalone operator- or module-authored persona is admitted and checked like any that
arrives in a pack; none installed means a **disclosed no-op pass**, never a silent green. The canonical
persona set lives in `.claude/agents/` (on Codex the reviewers are the same personas' committed renders under
`.codex/agents/`); a change to the engine's Codex adapter surfaces is judged against the engine's own
decision records (`.engine/contracts/` — eADR-0034) and, after merge, the live pass in
`.engine/operations/codex-validation.md`. The one mechanical hook is the pull-request
**Review** section's presence-gate (the completeness check over `.github/pull_request_template.md`);
everything else is a deliberate-effort nudge whose only wall is the protected-branch merge.

1. **Plan — open the claim and propose coverage.** Open a **draft pull request** and keep it a draft for
   its whole working life: the checks run on a draft exactly as on a ready one (so never open it ready just
   to make CI run), and a draft **cannot be merged**, which stops an in-progress change from merging before
   its review gate finishes. Plan the change as an ordered commit sequence, and **write it as the build
   Issue's checklist proportionately** (format in Notes): required when the work is routine-distributed across
   unattended sessions, offered as a progress view for an interactive multi-commit build (otherwise the
   orchestrator holds the sequence in-session), and skipped on the fast path (Notes). **Propose the coverage
   from what the work needs** — run the impact check (`.engine/operations/knowledge-impact-check.md`) for what
   depends on the parts you change and what checks or governs them, and weigh it with what review is installed
   and available and what sits next to the work — its neighbours and any open trouble nearby. The suggested
   depth is risk-derived, never lowered by a depth the operator has preferred before. **Title the pull request
   `Kind: what changed`** — the kinds are listed in `.github/pull_request_template.md`, which `gh pr create`
   never renders, so the title is authored here; the release notes group the merged list by that prefix. Settle
   the plan before moving on.
2. **Relay the risk assessment — the plan-gate consent surface.** Relay it to the operator **in chat**
   (it reaches them only as assistant text — no hook renders to their screen), filled from the
   `risk-assessment` template (`.engine/templates/risk-assessment.md`): the plain-language headline, what
   the change touches, **what will run** (the passes this depth runs, and what is missing — never a time or
   cost figure, which the engine cannot know; a made-up number is the false confidence the trust model
   refuses), the how-careful depth choice, and — only when the change weakens an engine guardrail — the
   plain-language warning naming which protection weakens and what the AI could then do unwatched.
   Applying the `guardrail-ack` is the operator's act, never the engine's: when a change weakens a guardrail,
   surface it and leave the gate red for the operator to clear — the engine never labels its own change to
   clear its own gate. The
   operator iterates the plan to solid and approves the plan and the depth **before any work starts**. This
   plan gate (steps 1–2) *always runs as a shape*, even with zero review packs — its depth collapses to a
   single plain-language headline on the fast path (Notes), but the gate itself is never skipped.
3. **Plan-review — cold review before building.** The installed plan-review passes run cold-context at the
   approved depth, before any implementation; each finding takes one disposition per the finding-disposition
   policy (`.engine/policies/finding-disposition.md`) — fix in line, log a tracked Issue, or escalate. After
   the audit the orchestrator synthesizes the findings into one recommended call plus the trade, re-engaging
   the operator on a material finding and **always** on an unresolved `blocking`-severity finding — never
   self-judging a blocking finding into a silent "logged and proceed". No packs installed means a disclosed
   no-op told plainly, never a green "passed".
4. **Implement — one of three strategies, chosen at Plan.** *Orchestrator-inline* for tiny or
   tightly-coupled work; *parallel workers*, each in its own isolated worktree returning mechanical work
   product (not commits), when the work is loosely-coupled and decomposable and holding the whole result
   while generating it would lose grounding; *time-distributed routine* for large decomposable bulk work
   (Notes). Delegation buys cohesion under context pressure, not speed. **Clear the deferred-work markers in
   what you touch.** Run `engine_todo.py list` before building in an area: each marker is work owed to that
   code, so clear what this change covers, and record a concern for any you cannot — the turn-close gate then
   holds until each has a disposition. **Iterate with scoped test runs, not the full suite.** While implementing, run just the test module(s) covering what you touched by
   narrowing the discovery pattern — `uv run --directory .engine --frozen -- python -m unittest discover
   -s tools -p 'test_<name>.py' -b` finishes in seconds. The full suite stays the pre-submission gate
   (step 6), run once when the work is ready — serial (the proven command), and runnable in the background
   so it never blocks the session. A scoped run accelerates the loop; it is never the merge signal — only
   the full suite (step 6) and CI are.
5. **Integrate — the orchestrator is the single writer.** Review each work product for correctness and fit,
   revise what does not cohere, and author the final commit(s) with the whole result in view. A failed
   worker leaves a missing planned commit the orchestrator re-dispatches or completes — no phantom-slot
   class, because the plan plus git state are the record. Then **reconcile the pull request's base against
   the default branch and regenerate the engine's internal index files — the knowledge graph and the
   self-map — last, from the reconciled tree**. A textual conflict on those is **spurious** (both sides
   regenerate the same sources): clear it and regenerate unconditionally, never a side-pick or hand-merge.
   The load-bearing guarantee is **reconcile-before-merge** — the eventual merge must already be clean,
   because the server-side merge button cannot run a local fix. A quiet Review-record line states how many
   index files were regenerated and that no work was lost — the operator meets the disclosure, never the
   conflict.
6. **Pre-submission review — gated behind green validation.** Confirm the validation suite
   (`.engine/suites.json`) is green first — run `uv run --directory .engine --frozen -- python
   tools/validate.py --suite CI` and the self-tests `uv run --directory .engine --frozen -- python -m unittest
   discover -s tools -p 'test_*.py' -b` (the same commands CI runs) — cold review is not spent on code that
   fails its checks. The `--frozen` keeps a test run from quietly rewriting the locked `uv.lock`, and the `-b`
   keeps the `Ran N … OK` summary visible: it buffers each test's stdout so the walkthrough output the
   `test_*.py` self-tests emit while exercising their demos does not bury the tail. **The self-test suite
   runs about 4 minutes (4,000+ tests, varying with machine and cache)** — run it with a generous timeout
   or in the background: a tool whose command timeout defaults to ~2 minutes cuts it off mid-run, which
   reads as a hang rather than a failure. Then
   the installed pre-submission passes run cold-context and findings are dispositioned — **record the
   reviewed commit (`git rev-parse HEAD`) at this launch**. Validation reruns on every change including
   post-audit fixes, but the cold review does **not** blanket-rerun on them. Instead, after the fixes,
   **measure the post-review divergence** (`git diff --shortstat <reviewed>..HEAD`) and weigh it with the
   *nature* of what changed to make a **proportional re-audit judgment** — a large cosmetic delta may
   warrant nothing, a small logic change a close look. The magnitude is data behind the call, never a
   threshold that fires a rerun (a fixed trigger spends cold-review effort out of proportion to its value).
   When warranted, **re-invoke the pre-submission passes that fit the repair, scoped to the post-review
   diff, before the record is finalized** — an independent cold read of the repair, sized to the risk: the
   coupled `spec-conformance` + `divergence-hunter` pair where a `locked` requirement exists to check
   against, else its disclosed no-op (Notes), the read then leaning on the other installed passes and the
   recorded correlate. The re-audit is never itself a gate; a `blocking` finding it surfaces gates the merge
   as any finding does. **If the branch is rebased after the review launch, re-record the reviewed commit —
   the rebase onto new `main` is itself grounds to re-review — else the divergence conflates upstream
   churn.** Author the record as the last act before ready; a later push (including the re-audit's own fix)
   re-measures and re-judges. The Review record states the reviewed→submitted commits, the measured
   divergence, and the disposition (see Notes).
   **Re-derive every not-applicable carve-out the negative-fixture meta-check lists.** When that meta-check
   (`engine/check/hard-check-bite`) reports a hard check as *not applicable* (its loud soft note — a check
   exempted from a negative fixture), the gate does not take the disclosure's word: for each one it re-derives
   the bound — confirming the check's *aimed* failure cannot be triggered by any committed input in CI (so the
   only seedable path is the fail-closed one, which would be a false witness), not merely that the disclosure
   carries the right property string. Anything that no longer holds becomes a finding; the per-carve-out
   re-derivation is recorded in the Review section. This is the standing control behind the meta-check's printed
   "re-derived at the review gate"; the meta-check checks the disclosure, the gate checks the world.
7. **Submit — mark the draft ready and hand to the human gate.** Fill the pull-request contract including
   the **Review** section by **reading `.github/pull_request_template.md` in full, never grepping it for
   headers** — each section is a bold summary line, then bullets, then an italic `*Impact:*` line, none of
   which a header scan reveals. **Fill from the template's literal text, never reconstruct the body from
   memory** — reconstruction silently drops the leading consent preamble (the blockquote above the first
   heading), most often when a `Closes #N` / narration line is prepended; carry it verbatim and unwrapped
   (the completeness gate matches its anchors, and a hard wrap inside one reads as absent). **Run the close-linkage pre-flight** (`close_linkage_preflight.py check`) and
   fold its lines into Review, applying any disclosed defang it emits (see Notes). **Render the
   change-profile** (`scope_profile.py`) into `Scope` and fill the `Behaviors` section — the plain-language
   shape of the change (size, kinds of surface touched, where it lands) and the falsifiable capabilities it
   delivers, each naming its test or demo. Both are report-only: the profile gates nothing and the Behaviors
   nudge is soft, there so the operator weighs a change by what it does, not its line count; a change with
   nothing observable (a dependency bump, a docs-only edit) says so in Behaviors and moves on. **Mark the pull request
   ready** (`gh pr ready`) — the act that submits it —
   **only once** validation is green, the pre-submission review is clean (no unresolved `blocking` or
   `serious` finding), and every post-review fix is pushed; until then it stays a **draft**, which cannot be
   merged. A build session is **done when the pull request is submitted**; merge-and-walk leaves nothing
   dangling.

## Done when

A draft pull request was opened as the claim; the plan and result were reviewed to the approved depth (or
disclosed un-reviewed where no pack is installed); the orchestrator authored the cohesive final commits;
validation is green; the pull-request contract, Review section included, is filled in plain language; and
the pull request is marked ready (`gh pr ready`) and so submitted for the operator's merge. The build
Issue, where one was written, closes as its commits land.

## Notes

**The skeleton is posture, named at its honest tier.** Nothing mechanically forces a session to run the review
passes, run them at the approved depth, or halt on a finding before merge — the same honest limit the
`operating-modes` write-gate and `close-turn` disposition gate carry. The one mechanical hook (the Review
presence-gate, named in Steps) checks only that the section is *present*, never that it is truthful — like every section, that stays posture.

**The Review record** states, in plain language a non-engineer reads at the merge: the depth that ran, the
review passes that ran (as plain checks, never their internal names), that each gate completed, **whether a
review ran the operator's code in a throwaway copy to judge it** (said plainly, never left silent, since
running their code can have effects they would not expect), the findings' dispositions, and — when
post-audit fixes were made — a plain line that **leads with the consequence** (a minor touch-up, or a
change large enough that the merged version differs materially from the reviewed one), says whether a
re-audit ran and what it found, and beneath it a **plain-language sentence of the two commits and what
changed between them** — the reviewed and submitted commit ids and the added / deleted-or-modified line
counts as a net change (the orchestrator measures this with `git diff --shortstat`, but the record reads as
a sentence, never raw diff shorthand or a command to run); the completeness check confirms the Review
section is filled — not that this divergence line is present, nor that its figure is true, which rests on
those recorded commits and on the re-audit when one ran. With no review packs installed it says so plainly
— "no extra review ran", never a green pass — and carries the standing caveat that it is the engine's own
account and the operator's merge is the real gate. A trivial fast-path build fills it with a truthful one.

Into that same Review record, fold the **unresolved-conversation pre-arm** verbatim (a collapsed notice
`.engine/tools/unresolved_conversation_notice.py` prints): an unresolved review comment greys the merge button, a
state a non-engineer cannot self-diagnose, so it explains — before they reach the button — why it's greyed, that
they may resolve the comment once they've read and accepted it, and how to reach one hidden as *outdated* after a
rebase. Standing copy — never fetched, never auto-resolved.

**The consumed-review-lenses record.** The fenced block below records which build stage runs which installed
review; the `lens-consumption` check reads it and goes red if a review is installed that no stage runs.
product-design's spec-lock ceremony is the plan-review four's **second consumer** (it runs the same four on
a description, when installed). Machine-read — the tokens are lens names, **never operator-facing wording**.
At the pre-submission gate `spec-conformance` and `divergence-hunter` are a **coupled pair** — the systematic
conformance pass and its adversarial partner, run as two decorrelated cold contexts against a `locked`
requirement, never one without the other: a depth that runs the conformance lens runs the hunter with it, and
where nothing is `locked` to check against, both are the same disclosed no-op.

```text
consumed-review-lenses:
  plan-review gate: product-intent, architecture, feasibility, risk-governance
  product-design spec-lock ceremony: product-intent, architecture, feasibility, risk-governance
  pre-submission gate: spec-conformance, divergence-hunter, usability, technical-integrity, security-governance
```

**The stranded-conflict case is not yet self-healing.** A sibling pull request can merge mid-flight after
integrate's reconcile, stranding a conflict; only its *resolution* leaves the operator's hands. The engine
**surfaces the stranded pull request at the next session's start and offers a one-step fix the assistant
runs on the operator's say-so** — reconcile against the latest default branch, regenerate the two index
files from the reconciled tree, lossless-or-it-does-not-run; if anything but those two files clashed, it
changes nothing and routes the operator to a plain-language decision. Never the operator resolving it by
hand.

**The fast path — depth scales to a real floor.** A trivial single reversible change takes the fast path:
orchestrator-inline, no Issue checklist, zero lenses, and the plan gate collapses to a single plain-language
headline — the operator enters Build, sees the headline, validation runs, and merges (*one entry, one glance,
one merge*), earned by reversibility. Only a genuinely trivial change qualifies: a change that **weakens a
guardrail or touches a schema is never fast-path** — it proposes the full suite, and its headline stays
**visibly weightier** than the trivial confirm, so habituation never erodes the high-stakes consent. Even on
the fast path the operator can always ask for a closer look before merging — the collapse is the default, not a
ceiling.

**Routine is the same workflow, time-distributed.** For large, cleanly-decomposable bulk work the implement
phase is spread across unattended sessions: an interactive Plan records the commit sequence and scope-lock
in the build Issue, unattended sessions add commits within that scope and report progress from git and the
checklist, and an interactive Finalize integrates, reviews for cohesion, validates, and submits. Its
cohesion guarantee is planned-up-front-plus-checked-at-Finalize, weaker than interactive Build's continuous
assembly and acceptable only for decomposable work. **Decomposability is a Plan-time judgment, not an enforced
property**: at Plan the orchestrator assesses whether the work chunks cleanly and, when it is too coupled to
split safely, says so and **recommends interactive Build instead** — nothing mechanically stops routine being
pointed at coupled work, so this is honest advice, not a gate.

**The build-Issue body + checklist + scope-lock format.** The build Issue is engine-authored, so its body
realizes the control-plane engine-authored-issue body contract through the shared issue-authoring helper
(`.engine/tools/issue_author.py`), never a human web issue template. Its parts are filled from the build:
*what this is* (the build it tracks and why) and *what happens next* — the ordered commit sequence as a
machine-readable checklist ("N of M done"; the next unchecked item is the next chunk), with the permitted
write-scope alongside it as the union of the planned chunks' declared paths. Both live in the build Issue,
authored at Plan, GitHub-native and cold-readable, carrying the engine-domain label.

**Grouping product work into phases.** When a build realizes product work and the project carries a committed
build order (`docs/spec/build-plan.md`, the [product-design](../modules/product-design/manifest.json) module's
artifact), group the work under native GitHub phases at Plan — the Milestone *is* the plan. Run
`.engine/tools/milestone_emit.py emit`: it reads the build order and creates one phase per entry, never
duplicating one on a re-run, then assign each open work Issue to its phase (`gh issue edit <n> --milestone
<phase>`). The phase names are the build order's own, shown to the operator in plain language — never engine or
review vocabulary. **Absent a build order there is nothing to consume and the build plans its phase itself.** The
build order is a consumed input, authored by the module, never here. **Before a phase's work starts, confirm it
is ready** — run `.engine/tools/build_readiness.py check --phase <phase>`: it names any piece the phase
schedules that is not yet settled, since building a phase from an unsettled description builds from an
incomplete spec. Advisory, never a merge gate.

**Checking against the settled description.** When a build realizes a product-design work item, resolve the
**settled description** at Plan — `.engine/tools/spec_referent.py resolve` on the build Issue's `Builds to:` work
item (it follows work item → its `docs/spec/` document → that document's acceptance criteria, gated on a settled
description). Hand those criteria **verbatim** (never a summary or a built-vs-spec judgment of your own) to the
plan-review and pre-submission passes as the description they check against; when none resolves the pass
discloses that plainly — never a silent pass. The **same one resolution** (consumed, not re-resolved) fills the
**Review** record's operator-runnable acceptance steps (`spec_referent.py review-steps`): the steps the operator
can run themselves, copied verbatim into two plain groups — "things you can confirm yourself" and "things I
checked for you" — or a plain reason-named line when nothing is operator-runnable (an in-tool demo and a CLI-only
check go on the engine's account). It is an offer for when the change matters, not a duty, and an unrun step is a
promise, not proof — never beside a green check; a step the operator will actually run beats one they won't (a
screen they click over a paste-this-command); and a step must be able to fail — it exercises the real changed
surface, never a staged recipe that can only succeed (posture, not a gate). The resolution holds with or without the optional product-design
module; a read failure is surfaced loudly, never read as "no description".

**Authoring a product description is the intake's job, not this runbook's.** When the work is to *describe* a
product — write up or plan what the operator wants built, with no settled description to realize yet — route
to the [engine-design intake](product-intake.md) by default (the operator runs `engine-design`, or you follow
`product-intake.md` with them); it produces the structured, checked description this runbook then realizes.
**Do not hand-author a product description as free prose in its place** — a loosely-written spec skips the
checks and the operator's settling. This is *product-description* work specifically: the ordinary small change
and the trivial fast path realize a change directly and need no formal description.

**The close-linkage pre-flight.** At submit, before marking ready, the orchestrator compares what the pull
request **will** close — GitHub's computed linkage (`gh pr view --json closingIssuesReferences`, `gh api
graphql` beneath it) **plus** the closing keywords in the integrated commit messages, which that field does
not reflect — against what the pull request **declares**: a deliberate `Closes #N` line versus a `Part of #N`
dependency in its own Scope/Out-of-scope. Two contradictions are decidable without guessing intent: an issue
the change will close while declaring itself only *part of* it, and the comma-trap (`Closes #1, #2` links only
`#1`). **Detect-and-surface, never silent-and-unilateral:** the default is a plain Review line the operator
reads at the merge; only an **unambiguously-accidental, body-sourced** keyword (declared *part of*, no
deliberate close line, uniquely locatable) is **neutralized** — a minimal keyword-only edit of the engine's
own PR body, never a narrative rewrite, never product scope — and the removal is **disclosed** in Review. A
commit-sourced or cross-repo close is surfaced, never defanged; an unreadable will-close set fails closed to
the could-not-read line, never a false "nothing will close". It is **not a gate** — the comparison is
mechanical but rides the AI-authored Review record at its posture tier, bounded by the operator's own GitHub
"will close" view. `close_linkage_preflight.py check --pr N --base REF` emits the lines and any defanged body.

**Some pieces are owned elsewhere, not authored here.** The **routine entry** (`/engine-routine` and its
procedure — the scope-lock read at boot and per commit, the first-fire echo, the misfire-as-Issue) is the
routine-mode package's, and the **non-interactive permission posture** that makes an unattended run unable
to ask is settled where routine is exercised. The engine-authored-issue body contract and its helper are
core/control-plane's; the step that ensures the engine-domain label exists is provisioning's; and the
*human* web issue templates are a separate control-plane artifact a person files through. This runbook
fixes only the distributed-implement *workflow shape* and the build-Issue *format*.

**A cross-repo contribution runs these same gates — by ONE OF TWO paths, split by whether the operator OWNS the
target (eADR-0026).**

**Owned product — the engine-mechanic building engine-template (a DIRECT draft pull request).** When this
deployment records an executable `product_build_target` — a product the operator OWNS, checked out SEPARATELY
beside the mechanic — it builds that product and opens a **plain owned draft pull request into the operator's own
checkout**, NOT through `external-contribution-submit` (that path is for the un-owned case below):

- **First-time setup (once per machine — the fork inherits everything else).** The committed `product_build_target`
  slug travels with the engine, so a co-maintainer who forks the mechanic already carries "I build this product";
  the ONE per-machine thing to set is where their local clone of it lives. If there is no clone yet, **clone the
  owned product as its own folder NEXT TO the engine's folder, never inside it** — the engine's folder IS the
  Engine, so the product is its sibling, each with its own `origin`:

  ```
  ~/code/my-engine-mechanic/     <- the Engine (this folder)
  ~/code/engine-template/        <- the product it builds (a separate clone)
  ```

  Then record the path: write it into **`.engine/mechanic/product-checkout-path`** — one line, gitignored, so it
  is durable AND stays on this machine. (`ENGINE_PRODUCT_CHECKOUT` also works and takes precedence, but an
  environment variable set inside a session does not survive it, so it suits a one-off override, not setup.) A
  `~`-relative path is fine — the reader expands it. Boot surfaces a setup offer whenever the target is recorded
  but the path is missing or points at nothing.
- **Preflight from the mechanic tree.** Run `mechanic_build.py preflight`. It REFUSES fail-closed — with a plain
  reason + remedy — unless the checkout is genuinely that product on a real `github.com` origin and clean to
  write into; on success it emits the verified `ENGINE_PRODUCT_CHECKOUT` and `GITHUB_REPOSITORY`.
- **Build in-place.** `cd` into the emitted path and run every product step as a subprocess INSIDE the checkout —
  `uv run --directory <checkout>/.engine …` with `GITHUB_REPOSITORY=<emitted slug>` exported — so the checkout's
  own tools, its validator, and `gh` resolve engine-template natively. The mechanic's own hooks act on the
  mechanic tree, so **run the product's index regeneration and validation EXPLICITLY as in-checkout subprocess
  steps**, never assumed. Branch from the checkout's default, implement, and run the plan-review and
  pre-submission passes above against the product diff in the checkout.
- **Scan for this repo's OWN references before opening — run this one from the MECHANIC tree, not the
  checkout.** Every other step above runs inside the product checkout; this one deliberately does not. Run
  `uv run --directory .engine -- python tools/local_references.py scan --ref <the checkout's default branch>
  --checkout <the emitted path>`. It reads the vocabulary from **here** — the repository whose shorthand
  would dangle — and scans the diff **there**. Run it inside the checkout and it reads the product's own
  declaration — which ships ABSENT — so it would report that nothing was checked, on the one path with
  no merge gate behind it. If it names anything, rewrite each one to
  say what it MEANS rather than what it refers to; the operator may wave one through, and that is their call.
  **This is a mandated step, not a wall:** the mechanic does not own the product's CI, so no merge gate is
  available on this path — the discipline is the instrument, and a skipped step is a real gap, not a caught one.
- **Open pinned to the verified slug.** `gh pr create --draft --repo <emitted slug>`, later `gh pr ready`.
  Passing the belt-verified slug as `--repo` makes the write target the verified repo — not a fresh read of the
  cwd's origin — which is what closes the gap between verify and write; a mid-session origin repoint cannot
  redirect it.

**The merge gate on that pull request is the operator's OWN engine-template gate — the same human, not an
independent reviewer.** What keeps that honest is NON-REFLEXIVITY: the mechanic upgrades only to human-approved
RELEASED engine-template, never its own unmerged branch — a human-review-grade rule, not a machine proof. (The
mechanic's product, update home, and any engine-fix target are all engine-template, so an engine fix ALSO takes
this direct path, never `submit`; a mis-route to `submit` still opens a sound pull request into engine-template —
safe by benign construction — but the rule is **owned → direct**.)

**Un-owned upstream — a fork escalating an engine fix to a project the operator does NOT own (a cross-fork pull
request).** This is delivered through `external-contribution-submit`, not a session-owned draft — a path with no
built-in gate linkage. So **run the plan-review and pre-submission passes above before submitting it**; the
submit tool records on the prepared pull request and in its body whether that review ran — an honest
disclosure, never a substitute for it. **When the target gates body completeness (engine-template does), author
the full body to its template (as you would an in-repo PR's) and pass it via `submit(authored_body=...)`**
(#557) — submit won't open an unfilled template against the engine's home, and only advises it elsewhere.

**A recognized automation's pull request carries a disclosed not-applicable check — relay both decisions
plainly.** Walking the operator to merge a dependency-update pull request from a recognized automation
(Dependabot), the `engine-ci` green includes a **disclosed not-applicable pass** for the PR-body
completeness check: it does not bind for the automation's own pull requests, so it was **not verified** —
green means *not applicable*, never *checked and passed*. Keep that distinct from the **`guardrail-ack`**
label the operator actively applies, which still gates the locked-dependency change (changing pinned
dependencies is exactly what a person should consciously approve). Every other check still runs; the merge
stays the operator's.

**A fail-open finding is surfaced in the Validation section.** When filling the pull request's **Validation**
section, surface any open **fail-open finding** the engine is carrying — a safety gate that could *not run*
(a crashed hook or an unhealthy tool-runtime), promoted to a tracked engine finding and carried at boot — as
a **named line, distinct from an ordinary pass or fail**: "*a safety check could not run on this change:
what it would have checked; this work was not verified for X*." It is **non-blocking** and only informs the
operator's consent at the merge — never a new gate. If none is open, say nothing; this is a surfacing duty,
not a section to always fill.

**An engine live-helper (MCP substrate) that is off is surfaced here too** — the submit-time half of boot's same
notice (`.engine/tools/boot.py` `mcp_availability_check`). Reuse the result of that current-provider procedure
from this session; if it did not run, run that canonical procedure now — never substitute a check of the
initially surfaced tool list. For any helper that failed, add a **named, non-blocking line** saying that the
change was authored on the committed-file fallback and using the procedure's diagnosis: an undiscovered helper
gets the provider's trust/approval-and-restart fix, while a discovered helper whose health call failed is named
as registered-but-not-answering and offered diagnosis without falsely blaming trust. If both helpers passed,
say nothing. This paragraph deliberately owns no second detection algorithm; boot's provider-specific procedure
is the single source.

**Build each slice to its full capability.** Each pull request drives the work it touches to its full agreed
requirement — the settled description's acceptance criteria where one resolves, the slice's own complete
behaviour otherwise. A partial or deferred build is a divergence, not a smaller change: a deferral is an
explicitly recorded decision (a tracked issue or a logged carve-out), never a quiet stub or a leg left
unwired, and a change is measured by the capability it delivers, not by effort or count. The pre-submission
spec-conformance and divergence-hunter passes flag an under-build as a divergence; this is the intent the
builder holds *before* that catch. This is the full statement; the conduct floor carries its terse form.

**Write a deferral where the work is, not in prose.** Work genuinely owed to the code is recorded at the site
with the engine's marker: the token `ENGINE-TODO` then a colon (or a parenthesised issue number and a colon),
then what is not built and what the code does instead. It is read as the first thing after a comment leader or
first on its line, so a trailing note and a docstring both work — and naming the form inline in backticks does
not create one. No issue is required: a tracked issue is the escalation for a marker nobody clears, never the
price of recording one. A decision *not* to build is a carve-out in the pull-request body instead (`eADR-0035`).

**Ground-truth load-bearing claims first-hand.** Before resting a gate, an escalation, or a merge consent on a
claim, verify it against the source yourself: read the locked or settled specification directly rather than a
summary of it; check a platform or harness capability against the installed binary, not its documentation; and
re-verify a subagent's finding or a code comment's implied authority before relaying it — a code comment
carries no design authority, and a delegated reader's reach to the source is proven, not assumed. The evidence
bundle is only as strong as the ground truth beneath it.
