---
required_sections: ["Decision", "Significance", "Rationale", "Anti-choice", "Status"]
allowed_sections: ["Supersedes"]
length_budget: 120
---

<!-- Two eADR homes, told apart by folder: the engine's own founding
canon lives here in .engine/contracts/ and is replaced wholesale on an engine update; a DEPLOYMENT's own
engine-decision eADRs live in .engine/contracts/instance/ and are preserved across every update. If you are
recording a decision this project made about ITS OWN engine, author it under instance/ (see
.engine/contracts/instance/README.md).

Naming (eADR-0017): the engine's own canon records here in .engine/contracts/ are named `eADR-####`
(bare — e.g. eADR-0017). A deployment's OWN record under instance/ is named `<project-slug>-eADR-####` (e.g.
acme-eADR-0007): the project-slug prefix keeps a deployment's own record from ever clashing with the engine's
canon as that canon grows. The slug is the project's identity resolved at first-run, lowercased with any
non-alphanumeric run collapsed to a single hyphen; BOTH the frontmatter `id` and the filename carry it
(e.g. acme-eADR-0007-a-short-slug.md). Which population a record belongs to is decided by its folder, never by
the name — so a canon record stays bare and only an instance/ record takes the prefix. This guidance is not
part of the record — do not copy it into the eADR. -->

## Decision

<State the single decision in one or two sentences — what was chosen, in plain words. Name the thing decided, not the discussion that led to it.>

## Significance

<Explain why this decision is worth keeping forever: what it locks in, and what later work now has to respect. If it does not constrain future choices, it probably does not need its own record.>

## Rationale

<Give the reasoning in two to five sentences — the forces that mattered and the trade-off that was made — with enough background that a reader a year from now understands why this was chosen, not just what.>

## Anti-choice

<Name the strongest alternative that was seriously weighed and turned down, and say plainly why it lost. Every decision worth recording had a real alternative; if none comes to mind, reconsider whether this needs a contract.>

## Status

<One word — proposed (drafted, not yet in force), accepted (in force now), or superseded (replaced by a later decision). Keep it the same as the status in the top block above.>

## Supersedes

<Include this section only when a deployment's own (instance/) decision replaces an earlier one in that same stream: name the earlier record (e.g. acme-eADR-0003) and add one line on what changed. Delete this whole section for a first-of-its-kind decision. A founding canon record never uses this section — the canon is revised in place and carried forward by an engine release, not superseded.>
