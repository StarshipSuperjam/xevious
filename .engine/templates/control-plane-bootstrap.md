<!-- The operator-facing copy for the control-plane bootstrap (the tool `tools/bootstrap.py` reads these
sections by heading and renders them; built-in fallbacks keep it working if this file is ever missing).
This is the single review surface for what the operator is told when the safety gate is turned on or can't
be. Plain language only — keep the engine's internal machinery out of what the operator reads: name each thing
by what it does for them, not by its engine/maintainer term. That is a relevance judgment made in the writing
and the review, never a banned-word list (none is kept here or anywhere). The scope literal the operator will
see on GitHub's screen (`repo`) AND the sweeping, full-control-sounding way the screen describes it are named
and defused here before they appear — never softened into a milder paraphrase that would let the scary wording
land uninterpreted. The exact on-screen label is GitHub's to word and re-verified live at build, so this copy
names its felt intensity ("full control of your repositories") durably rather than quoting a dated string. Edit
the wording here; the section HEADINGS are stable keys the tool matches, so don't rename them. -->

## Before you approve

I'm about to turn on your safety gate — the branch protection that keeps work from reaching your main
branch without passing checks and your review. To do that I need permission to manage this repository's
settings. GitHub will show an authorization screen for `repo` access, and it describes that access in sweeping
terms — it reads as full control of your repositories. That sounds far broader than what happens here: it's the
standard GitHub permission for turning on the review gate, and it reaches only repositories you already
control — not your wider account, and nothing you don't already own. Approving it lets me set the protection
rules; I can't grant it to myself, which is the point. Nothing changes until you approve.

## When it's on

Your safety gate is on. The main branch now requires a pull request, passing checks, and resolved review
comments before anything merges — and it can't be force-pushed or deleted.

## When it was already on

Your safety gate is already on — nothing to change. (Safe to run any time; it never weakens protection
that's already in place.)

## When it couldn't be confirmed

I set up branch protection but couldn't read back to confirm it actually took (GitHub didn't answer just
now). Don't assume it's on — check your repository's branch settings, or run this again in a moment.

## If it couldn't turn on — you don't administer this repository

I couldn't turn on branch protection — this account doesn't administer the repository. Protection is not
active, so work can merge unreviewed. Next step: ask whoever owns the repository to run this setup and
approve the screen. I'll keep reminding you until it's on.

## If it couldn't turn on — your organization blocks the permission

I couldn't turn on branch protection — your organization's settings blocked the permission it needs.
Protection is not active, so work can merge unreviewed. The way forward is to ask your organization's admin to
allow it — creating branch protection here needs an admin's permission, which I can't grant myself. I'll keep
reminding you until it's on.

## If it couldn't turn on — the approval didn't save

The authorization screen completed but the permission didn't save (some sign-in methods do this).
Protection is still off, so work can merge unreviewed. Let's try once more, or sign in again first. I'll
keep reminding you until it's on.

## Removing the engine — keep or remove your safety rule

I set up a safety rule on your main branch that requires checks to pass and a pull request before anything
merges. Removing the engine takes my checks out of that rule. I can keep the rule — your main branch stays
protected, just without my checks — or remove it entirely. Keep it unless you're sure you want it gone; I'll
never remove protection without you choosing.

## When the safety rule is kept

I took my checks out of your main-branch safety rule and kept the rule itself, so your main branch still
requires a pull request and can't be force-pushed or deleted.

## When the safety rule is removed

I removed the main-branch safety rule entirely — the one I had set up. Your main branch is no longer
protected. To turn protection back on later, run the engine setup again.

## When there was no engine safety rule

There was no engine safety rule on your main branch to remove — nothing to change here.

## When your own rule was there — I added my checks to it

Your main branch was already protected by your own rule, so I added my two checks to that rule rather than
creating a second one — and where your rule was missing a piece of the safety floor (blocking force-pushes,
blocking branch deletion, or requiring a pull request), I added that too. I changed nothing else of yours:
your rule's other settings are exactly as you had them.

## When I added my checks but one floor piece is yours to decide

Your main branch was already protected by your own rule, so I added my two checks to it (and any missing
force-push/deletion/pull-request protection). One part of the safety floor I couldn't turn on without changing
a rule you set yourself, so I left it exactly as you have it: {gaps}. You can switch that on yourself in your
branch's rules if you want it — leaving it as is means a change can still reach your main branch under that
rule's current terms.

## When you have more than one rule covering main

Your project has more than one rule covering your main branch, so I didn't change any of them — I couldn't be
sure which one to add my checks to without risking your setup. Instead I added my own rule with my two checks,
alongside yours. Your branch is protected by both; if you'd rather have my checks in one of your existing
rules, tell me which and I'll move them.

## When your main branch was already protected your own way

Your main branch is already protected by your own settings, so I added my two checks in my own rule alongside
what you have — I didn't touch your existing protection. Your branch is now covered by both; nothing of yours
changed.

## When the rule was yours — I only removed what I added

I took my two checks — and any force-push/deletion/pull-request protection I had added — back out of your own
branch-protection rule, and left the rest of that rule exactly as it was. There's no keep-or-remove choice
here because the rule is yours, not one I created; I only added to it, so I only removed what I added.
