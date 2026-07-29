#!/usr/bin/env python3
"""Emit a project's build phases as native GitHub Milestones, from a product-design build order.

build-orchestration (core) owns Milestone production: it produces the Milestones (native GitHub Milestones,
via `gh api`), consumes product-design's committed *build-plan* as the grouping input, and absent a build-plan
it plans the Milestone itself. This tool is that producer's mechanical core: it reads the committed build order (`docs/spec/build-plan.md` — the
product-design module's artifact), derives the ordered list of phase names, and creates one GitHub Milestone per
phase, **idempotently** (a re-run never duplicates a phase).

SELF-CONTAINED ON PURPOSE. This is a CORE tool, but the build order is authored by the OPTIONAL product-design
module. So it imports NO product-design code — a required tool must not depend on an optional module, or it would
crash the "absent a build order, plan the phase yourself" path on every repo that never installed product-design.
It therefore carries its own minimal pipe-table parser (a knowing duplicate of a trivial parse); its GitHub
boundary builds requests through the shared `github_client` (the gh-client consolidation engine-template #295
began) while keeping its own injectable-transport seam (`telemetry.GitHubIssues` / `standing_situation`).

Idempotency (the `gh api` Milestones surface has no upsert): list every existing milestone (`state=all`, so a
CLOSED phase is not recreated) paginated to exhaustion, match by trimmed title, and POST only the missing ones.
`emit` degrades LOUD — on any mid-run failure it raises and changes nothing further; because creation is
per-title and the re-list skips what already exists, a re-run finishes the rest.

Operator demo (faked GitHub, real logic — no real Milestones, no token):
  uv run --directory .engine -- python tools/milestone_emit.py demo
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the sibling tools dir, for github_client
import github_client  # noqa: E402 — the shared authenticated GitHub API client; request-build

# ---- constants -------------------------------------------------------------

USER_AGENT = "engine-milestone-emit"

# Where the committed build order lives (the product-design module's artifact), relative to the repo root.
_BUILD_ORDER_REL = "docs/spec/build-plan.md"

# <repo>/.engine/tools/milestone_emit.py -> <repo>. A pure leaf: computed from __file__, no sibling import.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class MilestoneEmitError(Exception):
    """A GitHub read/write failed (an HTTP >= 400, an unreachable host, or an unexpected shape). It is NEVER
    swallowed as success: a failure read as "done" could leave phases half-created and silently wrong. The
    caller (the orchestrator) surfaces it; a re-run finishes the rest (already-created phases are skipped on the
    next list)."""


# ---- pure core: derive the ordered phase list from the build order ---------------------------------

def derive_phases(text: str) -> list:
    """The ordered, de-duplicated, trimmed list of phase names from a build order's table. PURE — a string in,
    a list out, no IO. The build order is a markdown pipe table `| Phase | Capability | Doc |`; we take the
    first column (Phase), preserve first-appearance order, and drop blanks, the header row, and the
    `| --- | --- |` separator row. The header is dropped by POSITION (the first table row), not by matching the
    word "Phase" — so a real phase a build order happens to name "Phase" is not silently lost. Minimal on
    purpose: a CORE tool must not import the OPTIONAL product-design parser (see the module docstring), so it
    re-derives this trivial parse itself."""
    phases: list = []
    seen: set = set()
    header_skipped = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue                                     # not a table row
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        if not header_skipped:                           # the first table row is the column header (by position)
            header_skipped = True
            continue
        if not first or set(first) <= set("-: "):        # an empty first cell, or a | --- | :--: | separator row
            continue
        if first not in seen:
            seen.add(first)
            phases.append(first)
    return phases


def read_build_order(root: str | None = None) -> str | None:
    """The build order's text, BOM-stripped, or None when there is no build order (the honest normal state for a
    repo with no product spec yet, or a build the operator drives without one). `utf-8-sig` strips a BOM, exactly
    as the spec readers do, so a build order authored on Windows derives the same phases it passes coverage with."""
    path = os.path.join(root if root is not None else _ROOT, _BUILD_ORDER_REL)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()


def phases_for(root: str | None = None) -> list:
    """The ordered phase list for the repo at `root` (default: this repo), or [] when there is no build order."""
    text = read_build_order(root)
    return derive_phases(text) if text is not None else []


# ---- the GitHub boundary (the only network seam; transport is injectable) --------------------------

class GitHubMilestones:
    """The Milestone boundary. Mirrors the engine's injectable-transport seam (`telemetry.GitHubIssues` /
    `standing_situation`): `transport(method, path, body) -> (status, json)` is injectable, so the demo and
    tests fake ONLY the network and run the real list-then-create logic. A read/write failure RAISES (never a
    partial "done"). Its `_http` builds requests through the shared `github_client`, keeping its own status/error handling."""

    def __init__(self, repo: str, token: str, transport=None):
        self.repo = repo
        self.token = token
        self._transport = transport or self._http

    def _http(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = github_client.request(path, self.token, user_agent=USER_AGENT, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:             # 4xx/5xx — surface the status, never swallow
            return exc.code, None
        except urllib.error.URLError as exc:              # network unreachable — a read/write failure
            raise MilestoneEmitError(f"GitHub is unreachable: {exc}") from exc

    def existing_titles(self, per_page: int = 100) -> set:
        """Every existing milestone title — `state=all` (open AND closed, so a closed phase is not recreated),
        trimmed, paginated to exhaustion by a page-walk until a short page (the `telemetry` pattern; the
        transport seam returns no Link header, so the walk increments `page` until a page is under the limit).
        RAISES on any HTTP error — an auth/scope failure must never read as "no milestones", which would recreate
        every phase. `per_page` is the page size (100 in production; a small value lets a test force multi-page
        over a small fixture, exercising the real continuation)."""
        titles: set = set()
        page = 1
        while True:
            status, data = self._transport(
                "GET", f"/repos/{self.repo}/milestones?state=all&per_page={per_page}&page={page}", None)
            if status >= 400 or data is None:
                raise MilestoneEmitError(f"GitHub returned {status} listing milestones")
            if not isinstance(data, list):
                raise MilestoneEmitError("milestones response was not a list")
            for m in data:
                title = (m.get("title") or "").strip() if isinstance(m, dict) else ""
                if title:
                    titles.add(title)
            if len(data) < per_page:
                break
            page += 1
        return titles

    def create(self, title: str) -> None:
        """Create one milestone. RAISES on failure (never swallowed) — the caller stops and a re-run finishes the
        rest (this title is skipped on the next list)."""
        status, _ = self._transport("POST", f"/repos/{self.repo}/milestones", {"title": title})
        if status >= 400:
            raise MilestoneEmitError(f"GitHub returned {status} creating the phase '{title}'")


def emit(gh, phases: list) -> list:
    """Create one milestone per phase that does not already exist, in order; return the titles created (those
    already present are skipped — the idempotency floor). Lists existing titles ONCE up front (every page,
    `state=all`), then creates the missing ones. Degrades LOUD: a failure mid-run raises and leaves the rest
    uncreated; a re-run finishes them (already-created phases are skipped on the next list). Matches by trimmed
    title, so a whitespace-only difference is not a new phase."""
    existing = gh.existing_titles()
    created: list = []
    for title in phases:
        t = title.strip()
        if not t or t in existing:
            continue
        gh.create(t)
        created.append(t)
        existing.add(t)              # also dedupes a phase name repeated within one build order
    return created


# ---- operator-runnable demo (real emit logic; only the GitHub network is faked) --------------------

class _FakeGitHub:
    """A fake network for the demo/tests: a transport over an in-memory milestone list, so the REAL
    `existing_titles` (pagination) + `create` logic run unchanged — only the network is faked
    ([[demo-must-exercise-real-logic]]). It honors the `per_page` the caller requests (so a small page size
    genuinely forces the real page-walk to continue), and `fail_on` makes one create fail, to show the
    degrade-loud / re-runnable path."""

    def __init__(self, existing=(), *, fail_on=None):
        self.titles = list(existing)
        self.created: list = []
        self.fail_on = fail_on

    def transport(self, method, path, body):
        if method == "GET" and "/milestones" in path:
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            per_page = int(re.search(r"[?&]per_page=(\d+)", path).group(1))
            start = (page - 1) * per_page
            chunk = self.titles[start:start + per_page]
            return 200, [{"title": t} for t in chunk]
        if method == "POST" and path.endswith("/milestones"):
            title = (body or {}).get("title")
            if title == self.fail_on:
                return 500, None
            self.titles.append(title)
            self.created.append(title)
            return 201, {"title": title}
        return 404, None


def _client(fake) -> GitHubMilestones:
    return GitHubMilestones("demo/your-project", "fake-token", transport=fake.transport)


def _demo() -> int:
    print("What build-orchestration does with a build order — phases become native GitHub milestones,\n"
          "created once and never duplicated on a re-run. No real GitHub call is made.\n")
    order = ["Foundation", "Core flows", "Polish"]

    # (1) A fresh project: every phase in the build order becomes a milestone.
    f1 = _FakeGitHub()
    c1 = emit(_client(f1), order)
    print(f"1) A fresh build order {order}:")
    print(f"   created the phases: {c1}\n")

    # (2) Run it again with nothing changed: no duplicates (the idempotency floor).
    f2 = _FakeGitHub(existing=order)
    c2 = emit(_client(f2), order)
    print("2) The same build order, run a second time:")
    print(f"   created: {c2 or 'nothing — every phase already exists'}\n")

    # (3) The build order gains/renames a phase: only the new one is created (re-sequencing is free).
    f3 = _FakeGitHub(existing=order)
    c3 = emit(_client(f3), ["Foundation", "Core flows", "Hardening"])
    print("3) The build order replaces 'Polish' with 'Hardening':")
    print(f"   created only the new phase: {c3}\n")

    # (4) Pagination: the milestone list is read across every page. With three existing phases and a page size
    #     of two, "Polish" lands on page 2 — the page-walk must continue to find it (else it would be recreated).
    f4 = _FakeGitHub(existing=order)
    seen_across_pages = _client(f4).existing_titles(per_page=2)
    c4 = emit(_client(f4), order)
    print("4) A project whose milestone list spans more than one page (page size 2 here):")
    print(f"   the list read every page: {sorted(seen_across_pages)}")
    print(f"   so emit created: {c4 or 'nothing — the page-2 phase was found, not recreated'}\n")

    # (5) Normalization: a stray-whitespace title matches the existing one (no duplicate).
    f5 = _FakeGitHub(existing=["Foundation"])
    c5 = emit(_client(f5), ["  Foundation  "])
    print("5) A phase name with stray spaces around an existing one:")
    print(f"   created: {c5 or 'nothing — it matched the existing phase after trimming'}\n")

    # (6) Degrade loud, then resume: a create fails mid-run; a re-run finishes the rest with no duplicates.
    f6 = _FakeGitHub(fail_on="Core flows")
    raised = False
    try:
        emit(_client(f6), order)
    except MilestoneEmitError:
        raised = True
    after_fail = list(f6.created)
    f6.fail_on = None                                 # the transient failure clears before the re-run
    c6b = emit(_client(f6), order)                    # re-run over the same (now-partly-populated) project
    print("6) A create fails part-way through, then the build is re-run:")
    print(f"   first run created {after_fail} then failed loud; the re-run finished {c6b}\n")

    # (7) No build order -> nothing to emit (the "absent a build order" no-op).
    c7 = emit(_client(_FakeGitHub()), phases_for("/does/not/exist"))
    print("7) A build with no build order:")
    print(f"   created: {c7 or 'nothing — there is no build order to read'}\n")

    # Self-check: each invariant must hold, or the demo fails (a falsification that can fail).
    checks = {
        "fresh build order creates every phase": c1 == order,
        "a second run creates nothing (idempotent)": c2 == [],
        "only a new/renamed phase is created": c3 == ["Hardening"],
        "the milestone list reads every page (pagination)": seen_across_pages == set(order),
        "a phase already present is not recreated": c4 == [],
        "a stray-whitespace title is not a new phase (normalization)": c5 == [],
        "a mid-run failure is loud": raised,
        "the re-run finishes the rest with no duplicates": c6b == ["Core flows", "Polish"]
        and sorted(f6.created) == sorted(order),
        "no build order emits nothing": c7 == [],
    }
    bad = [name for name, ok in checks.items() if not ok]
    if bad:
        print("DEMO UNEXPECTED — these invariants did not hold:", file=sys.stderr)
        for name in bad:
            print(f"  - {name}", file=sys.stderr)
        return 1
    print("All phase-emission invariants held: created once, never duplicated — across re-runs, pagination,\n"
          "stray whitespace, a mid-run failure, and an absent build order.")
    return 0


# ---- CLI -------------------------------------------------------------------

def _gh_from_env() -> GitHubMilestones | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("usage: milestone_emit.py emit   (needs GITHUB_REPOSITORY and GITHUB_TOKEN in the environment, "
              "as in CI)", file=sys.stderr)
        return None
    return GitHubMilestones(repo, token)


def _show() -> int:
    phases = phases_for()
    if not phases:
        print(f"No build order at {_BUILD_ORDER_REL} — nothing to emit.")
        return 0
    print("Phases in the build order (each becomes a milestone, in this order):")
    for p in phases:
        print(f"  - {p}")
    return 0


def _emit() -> int:
    gh = _gh_from_env()
    if gh is None:
        return 2
    phases = phases_for()
    if not phases:
        print(f"No build order at {_BUILD_ORDER_REL} — nothing to emit.")
        return 0
    created = emit(gh, phases)
    if created:
        print("Created these phases: " + ", ".join(created))
    else:
        print("Every phase already exists — nothing to create.")
    return 0


def main(argv: list | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "show":
        return _show()
    if argv and argv[0] == "emit":
        return _emit()
    print("usage: milestone_emit.py [show|emit|demo]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
