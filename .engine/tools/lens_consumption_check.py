#!/usr/bin/env python3
"""Lens-consumption coherence guard — the custom/script entry for engine/check/lens-consumption.

Runs as a `custom/script` check in the CI suite: it discovers the present review personas
(`.claude/agents/*.md`, reusing `agent_coherence_check.engine_agents`), reads the CONSUMED review-lens
set that build orchestration records (the `consumed-review-lenses` block in
`.engine/operations/build-orchestration.md`), and runs the pure dangling-lens leg
(`validate.dangling_lens_findings`) over them. It emits a finding for every INSTALLED review lens that
no build stage consumes — an installed-yet-unconsumed review is one that ships but never runs against
the operator's changes, exactly the coherence hole the agents surface says must be disclosed and never
left as a check-only signal.

This is the "build-orchestration slice" the agent grammar's dangling-lens posture was deferred to: the
consumed set (which gate consumes which lens) is build orchestration's to record, and this consumer
diffs the installed personas against it. Today installed == consumed (the four plan-review + five
pre-submission lenses are all consumed), so the check is green; it bites only when a review persona is
installed carrying a lens no stage lists.

FAIL-CLOSED on BOTH inputs. The consumed set is read from build-orchestration; if that file is missing
or its `consumed-review-lenses` block is absent/empty, this RAISES — the custom/script kind turns the
crash into a hard fail-closed finding, so a parse miss can never read as "nothing dangling". The
installed set is discovered from the real `.claude/agents` tree via `engine_agents` (rooted at
validate.ROOT); a read error there raises the same way, while a GENUINE empty roster (no review packs
installed) is a legitimate pass — zero installed lenses dangle.

Reads local committed files only — no network, no token — so it runs unchanged in the head-checkout
engine-ci context. Emits finding.v1 JSON on stdout and returns 0 on a successful evaluation. `demo`
prints an operator-runnable fail-then-pass narration of the guard.
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import agent_coherence_check  # noqa: E402  (reuse engine_agents + emit — no duplication of discovery)

_BUILD_ORCH_REL = ".engine/operations/build-orchestration.md"
_SENTINEL = "consumed-review-lenses"
_MESSAGE = ("Wire that review into a build stage that consumes its lens — record it in the "
            "consumed-review-lenses block in .engine/operations/build-orchestration.md — or remove the "
            "persona from .claude/agents/. Until then it is installed but never runs, so it checks "
            "nothing while looking like an active review.")


def _consumed_from_notes(notes: str) -> set:
    """Parse the `consumed-review-lenses` fenced block out of build-orchestration's Notes body and
    return the union of every lens token it lists. RAISES if the block is absent or lists no lens —
    the consumer's fail-closed guard, so an unparseable consumed set becomes a hard finding, never a
    silent empty set. A fenced block is used (not bare backticks) because Notes prose carries other
    backticked tokens; the fence bounds the machine-readable data unambiguously."""
    fenced, in_fence, block = [], False, []
    for line in notes.splitlines():
        if validate.FENCE_RE.match(line):
            if in_fence:
                fenced.append(block)
                block = []
            in_fence = not in_fence
            continue
        if in_fence:
            block.append(line)
    target = next((b for b in fenced if any(ln.strip().startswith(_SENTINEL) for ln in b)), None)
    if target is None:
        raise ValueError(f"{_BUILD_ORCH_REL} carries no `{_SENTINEL}` block in its Notes — the "
                         f"consumed review-lens set is missing, so lens-consumption cannot be judged.")
    tokens: set = set()
    for line in target:
        s = line.strip()
        if not s or s.startswith(_SENTINEL) or ":" not in s:
            continue
        _, rhs = s.split(":", 1)
        tokens.update(t.strip() for t in rhs.split(",") if t.strip())
    if not tokens:
        raise ValueError(f"the `{_SENTINEL}` block in {_BUILD_ORCH_REL} lists no lenses.")
    return tokens


def consumed_lenses(root: str | None = None) -> set:
    """The consumed review-lens set build orchestration records. Reads the real committed
    build-orchestration.md (from validate.ROOT, so working-dir independent); raises on a read or
    parse miss (fail-closed)."""
    path = os.path.join(root or validate.ROOT, _BUILD_ORCH_REL)
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    notes = validate.section_blocks(body).get("Notes", "")
    return _consumed_from_notes(notes)


def _demo() -> int:
    """An operator-runnable fail-then-pass demonstration over the REAL guard, the REAL personas, and
    the REAL consumed set. Nothing on disk changes — the "broken" variant is an extra persona built in
    memory. It shows every installed review is consumed by a stage, and that the guard catches a review
    that is installed but wired into no stage."""
    tier = "hard"
    review_roles = {"plan-review", "pre-submission-review"}
    present = agent_coherence_check.engine_agents()
    consumed = consumed_lenses()
    reviews = [a for a in present if a.get("role") in review_roles and a.get("lens")]
    print("Your installed reviews, and whether a build stage actually runs each one against your "
          "changes:\n")
    if not reviews:
        print("  (no review packs are installed yet — nothing to consume)")
        return 0
    for a in reviews:
        lens = a.get("lens")
        mark = "run by a stage" if lens in consumed else "INSTALLED BUT NO STAGE RUNS IT"
        print(f"  {str(a.get('name')):32} {mark}")

    clean = validate.dangling_lens_findings(present, consumed, tier, _MESSAGE)
    if clean:
        print("\nThe safety check found an installed review that no stage runs (see engine-ci).")
    else:
        print("\nThe safety check: all clear — every installed review is run by a build stage.")

    orphan = {"name": "review-nobody-runs", "role": "plan-review", "lens": "a-lens-no-stage-consumes"}
    print("\nNow suppose someone installed a review that no stage runs (shown here in memory only — "
          "your files are untouched):")
    found = validate.dangling_lens_findings([orphan], consumed, tier, _MESSAGE)
    if found:
        print("  -> the safety check turns RED: the review is installed but no stage runs it, so it "
              "would silently check nothing. The build is blocked until it is wired into a stage or "
              "removed.")
    else:
        print("\nDEMO UNEXPECTED: the guard did not flag the unrun review.", file=sys.stderr)
        return 1
    print("\nThat is the safety net: a review can't be installed and then never actually run — the "
          "check catches it before it could be merged.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_AGENT_FIXTURE_DIR (unset in production) lets the negative-fixture meta-check point the
    # persona scan at a seeded non-.claude fixture dir carrying an unconsumed lens, so the guard is
    # witnessed biting a real bad input (#286). The consumed set is always the real committed one.
    agents = agent_coherence_check.engine_agents(
        agents_dir=validate.env_override_path("ENGINE_AGENT_FIXTURE_DIR"))
    consumed = consumed_lenses()
    return agent_coherence_check.emit(validate.dangling_lens_findings(agents, consumed, tier, _MESSAGE))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
