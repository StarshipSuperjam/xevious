## Purpose

This file is a deliberately-broken example. All three required sections are present and filled,
and Purpose and Scope each carry a filled Impact line — so the section-presence leg passes and
the ONLY thing left to fail on is the unfilled Impact line in `Detail` below.

*Impact: proves the filled-Impact leg bites on its own, not on a missing or empty section.*

## Scope

It exercises the `filled_subsection_label` param: every section has real content, so a bite here
can only come from the new leg, never from the old missing/empty-section path.

*Impact: isolates the new enforcement so the checker-of-checkers cannot pass over dead logic.*

## Detail

This section is present and filled with real content, but its Impact line is left as the
unfilled template placeholder — exactly the state the sharpened check must catch.

*Impact: <what this section delivers>*
