## Purpose

This file is deliberately COMPLETE — every required section is present and filled in — so the
presence check does NOT fire against it.

## Scope

That is the point: it is a *non-biting* fixture. Its expect.json still says a bite was expected,
so the meta-check, run against this scenario, must report that the check did NOT catch it.

## Detail

The Detail section is present and substantive here, unlike the real `kind-presence` fixture which
omits it. This completeness is what makes the scenario a self-falsification for the meta-check.
