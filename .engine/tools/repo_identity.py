#!/usr/bin/env python3
"""repo_identity — the ONE place the engine answers "is this checkout the engine's own home repo?".

The engine distinguishes its own home repository (the workshop where the engine is built and self-hosts its
growth) from a downstream copy someone made of it ("Use this template", or a clone-and-push). Historically
several detectors keyed that judgment off a string in the root `CLAUDE.md` — a proxy that both TRAVELS into
every copy and disappears the moment that file is promoted to the deployed floor. This module replaces that
proxy with the structural, NON-inherited signal: a copy's git origin differs from the `home_repository` its
inherited manifest records, while the home repo has `origin == home`.

Kept deliberately DEPENDENCY-LIGHT (stdlib + `validate` only) so the lean HARD CI safety checks
(`memory_pointer_public_safety_check`, `census_completeness_check`, `hard_check_bite_check`) can gate on it
WITHOUT dragging the modes/close/hooks lifecycle machinery that lives in `module_coherence` into a
`pull_request_target`-adjacent path. `module_coherence` re-exports the slug primitives from here rather than
keeping its own, so the two can never drift apart. Other modules — `first_run_health`, `boot`, `instantiator`,
`weakening_guard` — still carry their own origin/home readers with their own fail-directions; this is the
single home for the primitives it defines, though not the only reader of that state in the tree.

Two contracts callers rely on:
  - `is_home_repo(root)` reads the EXAMINED checkout's ON-DISK git origin (`git -C <root> remote get-url
    origin`), NEVER this process's `GITHUB_REPOSITORY` env. That keeps it judging the checkout it was handed
    (fixture-forceable in tests, correct inside a nested deployed-projection run) rather than a process-global
    fact an ambient env var could flip.
  - It fails TOWARD home: when origin or home cannot be confidently read it returns True. That is the safe
    direction for every scope detector that gates on it — a HARD safety check RUNS, the leftover-LICENSE offer
    stays quiet, the demo-census check runs — never the reverse. (The destructive first-run `retire`/`verify`
    lifecycle is guarded by the `--first-run` token, NOT by this predicate, precisely because an origin read
    must never be the sole thing standing between a bare hand-run and an irreversible self-delete.)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (ROOT + load_json — the one JSON reader, fail-loud on a malformed manifest)

_ENGINE_MANIFEST_REL = ".engine/engine.json"
# `owner/repo` out of an https or ssh GitHub remote URL, tolerating a trailing `.git` and/or slash. The host is
# ANCHORED to the scheme/userinfo boundary (`//github.com`, `git@github.com`, or a bare-start `github.com`) so a
# look-alike host — `notgithub.com/evil/repo`, `evilgithub.com/owner/repo`, or a `github.com` path segment under
# another host — cannot mis-parse into a slug that `slug_eq` would then read as the engine's home. IGNORECASE
# because host names are case-insensitive by specification (`GitHub.com` is `github.com`); ASCII so the fold
# stays ASCII-only — without it Unicode case-folding lets a homograph host (`gİthub.com`, where U+0130 folds to
# `i`) satisfy the `github.com` literal and slip through as a look-alike. The flags fold only the literal host,
# never the structural anchors, so no look-alike host is newly accepted (StarshipSuperjam/engine-template#625).
_GITHUB_SLUG_RE = re.compile(r"(?:^|@|//)github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", re.IGNORECASE | re.ASCII)


def _run(args: list) -> "str | None":
    """Run a git command OFFLINE and return its stripped stdout, or None on any failure (non-zero, missing
    binary, timeout). Never raises — a caller that cannot read git degrades, it does not crash."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out is None or out.returncode != 0:   # `out is None` only under a mocked subprocess (a test stubbing
        return None                          # subprocess.run -> None); honor the "never raises" contract there
    return out.stdout.strip() or None


def normalize_slug(slug: "str | None") -> "str | None":
    """A GitHub `owner/repo` slug normalized for equality — strip surrounding whitespace and a trailing slash,
    case-fold (GitHub slugs are case-insensitive), and drop a trailing `.git`. `None`/blank -> `None`. This is
    the ONE slug-normalizer the home/own comparison uses; `module_coherence` re-exports it so the comparison is
    single-homed rather than re-implemented per call site."""
    if not isinstance(slug, str):
        return None
    s = slug.strip().rstrip("/").casefold()
    if s.endswith(".git"):
        s = s[:-4]
    return s or None


def slug_eq(a: "str | None", b: "str | None") -> bool:
    """True iff two `owner/repo` slugs are the SAME repository, compared as EXACT normalized full slugs (never
    a repo-name-only match). Safety-load-bearing: it is the switch that lets the engine's own source travel to
    the engine's home, so a loose match (name-only, substring) would let engine code ride into a look-alike
    third-party repo. `None` on either side is never equal (an unconfirmed home never satisfies it)."""
    na, nb = normalize_slug(a), normalize_slug(b)
    return na is not None and na == nb


_READ_HOME = object()  # sentinel: "no home passed -> read THIS repo's home", distinct from a passed home of None


def _manifest(root: "str | None") -> "dict | None":
    """The engine manifest (`.engine/engine.json`) of `root` (default: this process's `validate.ROOT`) as a
    dict, or None when absent. A present-but-MALFORMED manifest RAISES (via `validate.load_json`) — the
    fail-loud commitment `home_repository` preserves for the update path."""
    path = os.path.join(root or validate.ROOT, _ENGINE_MANIFEST_REL)
    return validate.load_json(path) if os.path.isfile(path) else None


def home_repository(root: "str | None" = None) -> "str | None":
    """The engine's HOME repository slug (`owner/repo`) recorded in `root`'s manifest — the single coordinate
    for where the engine fetches its own updates from AND where a fork-native deployment escalates a
    contribution to (schema: engine.v1.json `home_repository`). `None` when the manifest is absent or records
    no/blank home. A present-but-MALFORMED manifest RAISES (loud): `is_downstream_copy_strict` calls this
    directly, and `release_cut` reaches it through `module_manager._home_repository` (which delegates here via
    `module_coherence`) — both rely on a corrupt manifest never being silently read as "no home"."""
    engine = _manifest(root) or {}
    home = engine.get("home_repository")
    return home if isinstance(home, str) and home.strip() else None


def default_branch(root: "str | None" = None) -> "str | None":
    """The repo's default branch NAME recorded in `root`'s manifest (schema: engine.v1.json `default_branch`) —
    derived from GitHub/git at first-run setup and preserved as operator config. `None` when the manifest is
    absent, records no/blank default, OR is malformed. UNLIKE `home_repository`, this reader is fail-SOFT on a
    malformed manifest (returns None, never raises): no default-branch caller needs the loud failure the update
    path relies on, every caller wants a fallback branch instead, and `boot` reads this at IMPORT time where a
    raise would crash nearly every tool in the tree. This is the single recorded-value reader —
    `checkout_health._persisted_default_branch` delegates here."""
    try:
        engine = _manifest(root) or {}
    except Exception:  # noqa: BLE001 — a malformed manifest degrades to "no recorded default", never crashes
        return None
    branch = engine.get("default_branch")
    return branch.strip() if isinstance(branch, str) and branch.strip() else None


def _origin_head(root: "str | None" = None) -> "str | None":
    """The branch `origin/HEAD` points at, read OFFLINE from disk (`git symbolic-ref refs/remotes/origin/HEAD`),
    stripped of its `origin/` prefix, or None. It follows a repo's live git default (set at clone, updated by
    `git remote set-head`), so it self-heals a repo whose manifest predates the recorded `default_branch` key."""
    args = ["git", "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"]
    if root is not None:
        args = ["git", "-C", root, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"]
    head = _run(args)
    if not head:
        return None
    return head.split("origin/", 1)[1] if head.startswith("origin/") else head


def resolve_default_branch(root: "str | None" = None, *, env_var: str = "PROTECTED_BRANCH") -> str:
    """The ONE default-branch resolver every Python caller shares, so the safety-gate signal, the CI merge-gate,
    and the gate's own one-click repair can never key off DIFFERENT branches: `env_var` override -> recorded
    manifest (`default_branch`) -> git `origin/HEAD` -> `"main"`. ALWAYS returns a non-empty name. Uses the
    `or` idiom, NEVER `os.environ.get(key, default)`, so a PRESENT-BUT-EMPTY env — a workflow expression that
    expanded to nothing on a trigger that carries no repository payload — falls through to the next source
    rather than pinning `""`. OFFLINE: in CI the workflow supplies the authoritative (non-PR-controllable)
    branch through `env_var`, so a stale or hostile recorded manifest can never override it to a greener
    answer; the `origin/HEAD` step self-heals a repo deployed BEFORE the recorded key existed. `env_var` is the
    workflow-set variable the call site reads — `PROTECTED_BRANCH` for the branch-protection paths,
    `GITHUB_DEFAULT_BRANCH` for the audit/telemetry paths."""
    env = os.environ.get(env_var)
    if env and env.strip():
        return env.strip()
    return default_branch(root) or _origin_head(root) or "main"


def origin_slug(root: "str | None" = None) -> "str | None":
    """The `owner/repo` of the checkout's git `origin` remote, read OFFLINE from disk (NOT this process's
    `GITHUB_REPOSITORY` env — that is a process-global fact, this must judge the examined checkout). `root`
    None reads the current process's checkout; a path reads `git -C <root>`. None when there is no origin
    remote or the URL is not a recognizable GitHub slug."""
    args = ["git", "remote", "get-url", "origin"]
    if root is not None:
        args = ["git", "-C", root, "remote", "get-url", "origin"]
    url = _run(args)
    if not url:
        return None
    m = _GITHUB_SLUG_RE.search(url)
    return m.group(1) if m else None


def is_downstream_copy(own_slug: "str | None", home_slug=_READ_HOME) -> bool:
    """True iff a repo is a DOWNSTREAM copy of the engine — its recorded update home is a DIFFERENT repository
    than its own origin `own_slug`. READ-ONLY, injectable, normalized; the ONE place the downstream-copy rule
    lives (re-exported by `module_coherence` so its callers — the instantiator `show` branch, the boot
    detector, `release_cut` — cannot drift).
      - both slugs are PARAMETERS. `own_slug` is always the caller's (never resolved here). OMIT `home_slug`
        to read THIS repo's recorded `home_repository()` fail-soft, or PASS the examined checkout's home —
        INCLUDING an explicit `None` for an absent home — used verbatim (the `_READ_HOME` sentinel keeps a
        passed `None` from silently falling back to this repo's home).
      - `slug_eq`-normalized, so SSH-vs-HTTPS or case skew never reads as "different".
      - SAFE, fail-toward-quiet: False (NOT a copy) whenever home is absent/blank/unreadable OR `own_slug` is
        None, so the workshop (home == own) and any repo whose origin cannot be read stay quiet. A MALFORMED
        manifest read here degrades to False (never crash a read-only caller), unlike the fail-LOUD
        `home_repository()` the update path relies on — which is why `is_downstream_copy_strict` passes the
        home in explicitly rather than omitting it (an argument is evaluated in ITS frame, so the raise never
        reaches this `except` at all)."""
    if home_slug is _READ_HOME:
        try:
            home_slug = home_repository()
        except Exception:  # noqa: BLE001 — a corrupt manifest degrades to "not a copy"; never crash the caller
            return False
    return bool(home_slug and own_slug and not slug_eq(home_slug, own_slug))


def is_home_repo(root: "str | None" = None) -> bool:
    """True iff `root` (default: this process's checkout) is the engine's OWN home repo — its on-disk git
    origin equals the `home_repository` its manifest records — OR its origin/home cannot be confidently placed.
    The inverse of `is_downstream_copy` over the EXAMINED checkout's on-disk origin: it fails TOWARD home, the
    safe direction for the scope detectors that gate on it (a HARD safety check RUNS, the leftover-LICENSE
    offer stays quiet, the demo-census check runs). Reads on-disk origin, never `GITHUB_REPOSITORY`, so it
    judges the checkout it is handed and a fixture can force either verdict by setting that checkout's origin.
    NOTE: the destructive first-run `retire`/`verify` lifecycle does NOT gate on this — it is `--first-run`
    token-guarded, so no origin read can stand between a bare hand-run and an irreversible self-delete."""
    own = origin_slug(root)
    try:
        home = home_repository(root)
    except Exception:  # noqa: BLE001 — a malformed manifest cannot place the repo -> fail toward home (safe)
        home = None
    return not is_downstream_copy(own, home)


def is_downstream_copy_strict(root: "str | None" = None) -> bool:
    """The fail-LOUD complement of `is_home_repo`: True iff `root` is a downstream copy of the engine, with a
    MALFORMED manifest RAISING rather than degrading.

    `is_home_repo` deliberately swallows that error and fails toward home — the right direction for a scope
    detector, whose worst case is checking unnecessarily. It is the WRONG direction for a caller whose quiet
    verdict is itself a reassurance to the operator: `overlay_disclosure.is_deployed` stays silent when this is
    False, and silence there reads as "nothing will be overwritten". A corrupt manifest must not be able to
    produce that. The load-bearing detail is the EXPLICIT home ARGUMENT: it is evaluated in this frame, so the
    raise happens before `is_downstream_copy` is entered and its fail-soft `except` never sees it. Collapsing
    this to `not is_home_repo(root)`, or dropping the explicit argument, silently restores the swallow —
    `TestIsDownstreamCopyStrict` pins both. (Widening `is_downstream_copy`'s own `try` does NOT affect this
    caller, for the same argument-evaluation reason; no test claims otherwise.)"""
    return is_downstream_copy(origin_slug(root), home_repository(root))
