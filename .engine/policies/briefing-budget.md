---
title: Briefing budget
status: accepted
date: 2026-08-09
established_by: eADR-0033
values:
  excerpt_chars: 200
  pin_index_title_chars: 80
  posture_lines_max: 8
  posture_chars_max: 700
  neighborhood_groups_max: 8
  dashboard_chars_max: 4500
  margin_floor_chars: 300
---

## Rule

The session-start briefing (the boot pack) is measured against the platform's per-value size limit
*before* it is injected, and it is made to fit by setting aside whole lower-value components in a
fixed order — never by cutting a component off mid-sentence. Two things are held, every session:

- The **governance and consent content is never set aside**, and the **status dashboard is the last
  thing set aside** — kept in every ordinary session, yielded only under extreme pressure (after every
  other component, when a heavy load of governance alarms — which themselves can never be set aside —
  leaves no room). A **margin canary** holds a stated character **margin** (`margin_floor_chars`) under
  the size limit in the clean case, so ordinary *structural* growth of the never-shed content is caught
  before it starts eroding the dashboard's room. That margin has a hard minimum set in code that this
  file may raise but never lower. (It is not an absolute promise the dashboard never sheds: enough
  simultaneous alarms will still set it aside — alarms outrank a status readout — and that shed is
  disclosed, never silent.)
- Every component that can grow has a **character budget** and a **place in the set-aside order**
  (see Scope). When a component would exceed the size limit, the lowest-value components are set
  aside first, and their absence is disclosed in plain words — including a distinct, always-shown
  alert if the operator's own pinned notes overflow, so the operator learns to prune them rather
  than losing them silently.

The dials live in this file's `values` block — in plain sight, read directly by the engine at pack
build, not buried as constants — so the priorities can be read and reviewed.

## Scope

This governs the AI-facing boot pack assembled at session start and how it is fit to the platform's
per-value output size limit: the character bounds on each growing component and the order components
are set aside under pressure. It governs the **mechanical fit** to a physical size limit — not the
**cognitive priority** of what to surface, which is the attention policy's job (its item-count
budgets and its five-kind ranking). The two are deliberately separate: attention decides *what is
worth showing and how much room each kind of thing gets*; this policy decides *how the assembled
result is trimmed to physically fit the platform's byte limit*. It does not govern the per-prompt
scent or any surface other than the boot pack.

The growing components and their dials:

- `excerpt_chars` — the longest a single quoted line from the operator's own past sessions
  ("where we left off") may run before it is clipped. Applies to conversational quotes only, never
  to a pinned note's text.
- `pin_index_title_chars` — pins are shown as a compact **index**: one title-length line each so
  every pin is always visible, with its full text pulled on request. This is the longest a single
  pin's index line may run.
- `posture_lines_max` / `posture_chars_max` — the most lines and characters the execution-posture
  relay (always-shown operating guidance) may occupy before it is clipped to a pointer. Shipped well
  above the real posture size, so clipping is insurance, never the normal case.
- `neighborhood_groups_max` — the most relationship groups the work-neighbourhood summary lists
  before the remainder is disclosed as a count.
- `dashboard_chars_max` — a **growth alarm**, not a trimmer: the size the status dashboard is not
  expected to exceed. The margin canary budgets the never-shed core against this figure, so a
  dashboard that grows past it fails the check loudly rather than silently eroding the margin.
- `margin_floor_chars` — the character margin the never-shed core must keep under the size limit in
  the worst modelled case. A hard minimum in code bounds this from below.

The fixed set-aside order (first set aside → last kept): the work neighbourhood, then where-we-left-
off, then the pin index, then the status dashboard. Governance and consent content is never set
aside.

## Rationale

Past the platform's per-value limit the platform silently replaces the value with a truncated
preview — which would drop the operator's status and the AI's orientation with no notice, the exact
failure this policy exists to prevent (a session once told the operator "nothing alarming was cut"
about content it never saw). Bounding each growing component and setting aside whole low-value pieces
first — with a plain disclosure of what went — keeps the consent-critical content and the dashboard
always present, and the margin keeps one extra line from silently tipping the core over.

Raise a budget when a component legitimately needs more room and the margin allows it; lower one when
a component is crowding the core. Raise `margin_floor_chars` to demand more headroom; it cannot be
lowered past the code minimum, because the number that defines "eroded" must not itself be quietly
editable — otherwise the guarantee against silent erosion could be removed at one remove.

## Enforcement-tier

**Layered.** The `values` are read live at pack build by a reader that never raises: a missing or
malformed file falls back to the shipped defaults compiled into code, so the boot pack always
assembles. The set-aside itself is mechanical, in the pack assembler. Two hard checks run in CI: a
**margin canary** asserts the never-shed core fits the size limit with `margin_floor_chars` to spare
under the worst modelled case, and **per-component budget tests** each fail — naming the component —
when one outgrows its budget. The **hard code minimum** on the margin is what this policy cannot
weaken: a test asserts `margin_floor_chars` is at least that minimum. The pin-overflow alert is a
posture surfaced to the assistant to relay; the protected-branch merge remains the real gate.
