---
id: eADR-0035
title: "Deferred work is recorded at the site with one engine-namespaced marker; a tracked issue is the escalation, never the entry fee"
status: accepted
date: 2026-07-26
---

## Decision

Work knowingly left unbuilt is recorded at the site in source, using one engine-namespaced marker whose trigger is
the token `ENGINE-TODO` followed by a colon, or by a parenthesised issue reference and a colon. The marker is
recognised when that trigger is either the first token immediately following a comment leader on its line, or the
first non-whitespace token on its line; a description is required and an issue reference is optional. Writing one
costs nothing beyond the description — no issue, milestone, or owner reference is required — because a tracked
issue is the escalation for a marker nobody clears, never the price of recording one. The recognition rule is a
frozen on-disk format that may only ever widen what it accepts, never narrow it. A deferral that leaves nothing
owed to the code is not a marker at all but a carve-out, recorded in the pull request body and never in the code.

## Significance

This settles that a deferral has a machine-findable home at the site of the work, so any later session enumerates
every outstanding one in a single command instead of discovering them by luck. It fixes the recognition rule as a
frozen format, because the parser travels by the engine overlay while the markers travel in each repository's own
committed source: the only possible skew is a new parser meeting old markers, which is why the rule may widen and
never narrow — a narrowing change, including a bug fix that tightens, would redden committed source across every
deployed repository with no migration path. It reserves the parenthetical beyond an issue reference, so the
grammar can extend later without breaking deployed parsers, and it holds the hard tier to the single unambiguous
case of an anchored trigger with an empty description, leaving every other shape soft. It also defines the
previously undefined carve-out, closing a bypass through which any author could have claimed a prose note was
already the recorded decision. Later work must not mint a second marker for this job, must not make a reference
mandatory, and must not narrow the recognition rule.

Several limits are part of the decision rather than gaps in it, and each is a place the scan shows less than
everything. A surface with no comment syntax carries no marker: a deferral concerning such a file is recorded in
the code that owns the behaviour, naming that file. Inside a block comment written in the leading-star style, the
marker is written without the star, because the star is the markdown bullet and admitting it as a leader made an
ordinary list item a marker. A file that is not valid text, or is larger than a source file carrying a
hand-written note would ever be, is not scanned. And in a deployed repository, markers in files an engine update
overwrites are not surfaced, because a local fix to them is wiped on the next update; that skip is disclosed by
the tool rather than silent, and it never extends to operator-owned files that happen to live inside the engine's
own directory.

One condition is never a limit but a reported failure: if the list of tracked files cannot be read at all, the
scan says so and the check goes red, rather than reporting the clean result an empty list would otherwise
produce. A confident "nothing outstanding" that actually means "I could not look" is the worst output available
here, so it is the one degradation that is refused.

## Rationale

The engine already forbids a code note that narrates build status rather than describing the code, and it already
requires a deferral to be an explicitly recorded decision. What it lacked was any sanctioned way to record one, so
the cheapest compliant response to a flagged prose deferral was to reword it — which deletes the information
instead of keeping it. A coined marker supplies the missing outlet, and it escapes the engine's standing refusal
to keep a forbidden-word list because that rule governs jargon in operator-facing copy and grades prose; matching
a token the engine itself defines, in source comments, is neither.

The anchoring rule is the part experience forced. Anchoring only at the start of a line fails open on a trailing
comment after code, which is the idiomatic short note in Python — the author believes a deferral was recorded
while nothing can see it, and a silent false negative is worse than the prose it replaced. Requiring only that a
comment leader precede the trigger fails the other way, matching every heading and every citation that names the
form. Requiring the trigger to be the first token after the leader, or the first token on the line, admits the
trailing comment and the docstring interior — where this repository's real notes live — while excluding headings,
citations, and prose that mentions the form inline. The markdown bullet character is excluded from the leader set
for the same reason: it made an ordinary list item a marker.

The cost asymmetry is deliberate. Recording must be nearly free or sessions will keep writing prose to avoid the
ceremony, and a plan-gated run may legitimately leave many markers behind while it proceeds; charging an issue per
marker would file dozens for work already scheduled, which is the ceremony the design exists to avoid.

## Anti-choice

The strongest rejected alternative was to require every marker to cite a tracked issue, making the issue tracker
the register of deferred work. It lost on volume and on behaviour: a run spanning many changes produces markers in
proportion to the work, so the tracker would fill with items for work already planned and being actively done, and
the cost of recording would push authors back to prose — defeating the marker's only purpose. A weaker variant,
one program-level issue or milestone per run that every marker cites, was also rejected: it is lighter but still
ceremony at creation time, and it invents a program object the engine does not otherwise have.

A second rejected alternative was to detect prose deferrals mechanically by matching their idioms, with a ratchet
forbidding the count from rising. It is refused on principle, not on accuracy: a maintained list of forbidden
words invites list-growth and teaches that word-banning is a writing function, which is the rule the engine holds
elsewhere. Its measured false-positive rate is severe — the overwhelming majority of idiom hits are the engine's
own domain vocabulary — but a later attempt that narrowed the list to improve that rate would still be barred.

A third was recording the deferral only in the pull request body, reusing the existing gate that resolves cited
issues. That is genuinely cheaper and needs no new grammar, and it remains the right home for a carve-out. It lost
for the marker's case because it loses locality and post-merge survival: a reader of the code sees nothing, and
the record is only discoverable by someone who already knows which change to look up.

## Status

accepted
