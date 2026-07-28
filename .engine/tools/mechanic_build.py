#!/usr/bin/env python3
"""The engine-mechanic build entry (eADR-0026): the FAIL-CLOSED preflight that authorizes a cross-repo build.

WHAT IT DOES. An engine-mechanic is a deployed engine whose product is a repository the operator OWNS
(engine-template), checked out SEPARATELY beside it. Before a mechanic session builds that product and opens a
direct pull request into the separate checkout, this preflight resolves the committed build target
(`product_build_target`), resolves this machine's local path to the checkout, and REFUSES unless the checkout is
genuinely that product on a trusted host and safe to write into. On success it emits the verified checkout path
and the verified product slug; the runbook then runs the ordinary Build steps as subprocesses INSIDE that
checkout (`cwd=<checkout>` + `GITHUB_REPOSITORY=<verified slug>`).

WHY THIS FILE IS GUARDED (it is in weakening_guard._FLOOR_ENFORCEMENT_HOOKS). The belt
`product_checkout_matches` is the last line of defence behind a live cross-repo WRITE: it authorizes the
mechanic to run the checkout's own committed `.engine` tools and open a pull request against it. A weakening of
this belt — fail-open on doubt, or an unanchored host parse that accepts a look-alike origin — would let the
mechanic execute an attacker-controlled checkout's code locally and write against the wrong repository, with NO
on-disk floored correlate any check could catch. So a change here routes through the guardrail-ack, exactly like
the other runtime enforcement gates (modes.py, close.py). A unit test alone is the wrong instrument: the same
pull request that flips the belt fail-open can edit the test that would have caught it.

DISPOSITION — FAIL-CLOSED throughout. Unlike checkout_health.py (fail-soft-QUIET read-only probes, which return
None/no-signal on doubt because a stranded LOCAL checkout cannot reach a protected branch anyway), every gate
here authorizes an outward write, so on ANY uncertainty it DENIES — `product_checkout_matches` returns False
(never None), and `resolve_build_target` returns a refusal (never a path). Do NOT 'harmonize' any of these to a
quiet None for consistency with checkout_health: that would flip a live write gate fail-OPEN.

THE HOST ANCHOR (security boundary). `_github_slug` parses owner/repo ONLY from a genuine github.com origin. A
look-alike host (`notgithub.com`, `github.com.evil.com`) or a non-github host must NOT parse to a real slug —
because under the subprocess-in-place build model a checkout whose origin matches the target gets its OWN
`.engine` tools executed locally. The anchor is what stops a phished `ENGINE_PRODUCT_CHECKOUT` from turning into
local code execution.

CONTRACT. An operation tool invoked by the engine/operator and narrated by build-orchestration.md's owned-product
mechanic arm — never by the validator. OFFLINE and READ-ONLY: it inspects local git + the manifest and decides;
it never fetches, branches, commits, or opens a pull request itself (the runbook's ordinary Build steps do that,
in the checkout).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checkout_health  # noqa: E402  (the OFFLINE readers + fail-soft health probes; this module adds the gate)

# owner/repo parsed ONLY from a genuine github.com origin. The leading host anchor is load-bearing: `github.com`
# must be the URL host (after an optional scheme and optional `user@`), never a substring of a look-alike
# (`notgithub.com`, `github.com.evil.com`) — see the module docstring's HOST ANCHOR note.
_GITHUB_SLUG_RE = re.compile(r"^(?:(?:https?|ssh)://)?(?:[^@/]+@)?github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$")

# Plain-language refusal messages (operator-facing: name the cause AND the remedy, never the raw token).
_REFUSALS = {
    "not-a-mechanic": (
        "This engine has no product build target set, so there is nothing to build in another checkout. "
        "(That is the normal state for an engine that builds its own repository.)"),
    "path-unset": (
        "A product build target is set, but this machine's path to its checkout is not. "
        "Set ENGINE_PRODUCT_CHECKOUT, or write the path into .engine/mechanic/product-checkout-path."),
    "checkout-unreadable": (
        "The product checkout path does not point to a readable git checkout (no origin remote found). "
        "Check that ENGINE_PRODUCT_CHECKOUT / the product-checkout-path file points at your product clone."),
    "origin-untrusted-host": (
        "The product checkout's origin is not a github.com repository (or is a look-alike host). The mechanic "
        "only builds against a genuine github.com origin. Re-clone the product from github.com/<owner>/<repo>."),
    "origin-mismatch": (
        "The product checkout's origin does not match the committed build target. The mechanic refuses to write "
        "into a checkout that is not the product it is configured to build. Point ENGINE_PRODUCT_CHECKOUT at the "
        "correct clone."),
    "checkout-unhealthy": (
        "The product checkout has uncommitted work, a detached HEAD, or a paused git operation. Commit, clean, "
        "or finish it first — the mechanic will not branch on top of unsaved work in your checkout."),
}


def _run(cmd: list, cwd: str | None = None, timeout: int = 30) -> str | None:
    """Run a local git command and return raw stdout, or None on any non-zero / failure. Never raises — every
    read is best-effort. Kept self-contained (not delegated to checkout_health) so the gate's security-critical
    origin read does not depend on another module's IO helper."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd)
        return out.stdout if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a missing binary / timeout / OS error all degrade to "unavailable"
        return None


def _git_origin_url(checkout_path: str | None) -> str | None:
    """The raw `origin` remote URL of the checkout at `checkout_path`, or None if unresolvable (bad path, not a
    git repo, no origin remote)."""
    if not checkout_path or not str(checkout_path).strip():
        return None
    url = _run(["git", "-C", checkout_path, "remote", "get-url", "origin"])
    return url.strip() if url and url.strip() else None


def _github_slug(url: str | None) -> str | None:
    """owner/repo IF AND ONLY IF `url` is a genuine github.com origin (SSH or HTTPS). None for any other host —
    the host anchor is the security boundary (see the module docstring): a look-alike host must NOT parse to a
    real slug, because under subprocess-in-place a matched checkout's own `.engine` tools are executed locally."""
    if not url:
        return None
    m = _GITHUB_SLUG_RE.search(url.strip())
    return m.group(1) if m else None


def _classify_origin(target_slug: str | None, checkout_path: str | None) -> str:
    """The SINGLE source of the host-anchored authorization compare — both the boolean belt and the resolver read
    it, so the security predicate is defined once and cannot drift between them. Classifies the checkout's origin
    against the committed target, fail-closed, into exactly one of:
      - `"unreadable"`     — blank inputs, or no readable `origin` remote at `checkout_path`;
      - `"untrusted-host"` — an origin is present but is NOT a genuine github.com repo (a look-alike host);
      - `"mismatch"`       — a genuine github slug that does NOT equal the committed target;
      - `"ok"`             — a genuine github origin whose slug equals the committed target.
    Any doubt lands on a non-`"ok"` value (DENY); it never raises."""
    if not target_slug or not str(target_slug).strip() or not checkout_path:
        return "unreadable"
    url = _git_origin_url(checkout_path)
    if not url:
        return "unreadable"
    slug = _github_slug(url)
    if not slug:
        return "untrusted-host"
    from repo_identity import slug_eq  # lazy: keep the identity seam off any import surface this tool rides
    return "ok" if slug_eq(target_slug, slug) else "mismatch"


def product_checkout_matches(target_slug: str | None, checkout_path: str | None) -> bool:
    """FAIL-CLOSED belt: True ONLY when `target_slug` (the committed product_build_target) equals the `origin`
    slug of a genuine github.com checkout at `checkout_path`. Any doubt returns False (DENY), NEVER None — a
    missing/blank slug on either side, an unreadable origin, an untrusted/look-alike host, or a mismatch. This is
    the last line of defence behind the guardrail-ack; see the module docstring for why it is fail-closed and
    host-anchored. It and `resolve_build_target` share `_classify_origin`, so the compare cannot diverge."""
    return _classify_origin(target_slug, checkout_path) == "ok"


def resolve_build_target(cwd: str | None = None) -> tuple[str | None, str | None, str | None]:
    """FAIL-CLOSED resolution of the mechanic's build target. Returns `(checkout_path, product_slug, refusal)`
    with exactly one side populated:
      - a refusal (path/slug None) — one of the ordered, mutually-exclusive reasons in `_REFUSALS`:
        `not-a-mechanic` (no target recorded) -> `path-unset` (target recorded, local path missing) ->
        `checkout-unreadable` (path is not a readable git checkout) -> `origin-untrusted-host` (origin is not a
        genuine github.com repo) -> `origin-mismatch` (origin read but != target) -> `checkout-unhealthy`
        (dirty / detached / paused git op).
      - `(path, slug, None)` — verified: the checkout at `path` is the committed target on github.com AND is
        safe to write into. `slug` is the committed target (canonical), suitable for `GITHUB_REPOSITORY`.

    INVARIANT (pinned by test): NEVER returns a path unless the host-anchored belt passed AND the health check
    passed — this is the whole authorization, so no early-out may bypass either."""
    target = checkout_health.recorded_product_build_target(cwd)
    if not target:
        return (None, None, "not-a-mechanic")
    path, state = checkout_health.resolve_product_checkout(cwd)
    if state == "path-unset" or not path:
        return (None, None, "path-unset")
    origin = _classify_origin(target, path)   # the shared, host-anchored compare (also the boolean belt)
    if origin != "ok":
        return (None, None, {"unreadable": "checkout-unreadable",
                             "untrusted-host": "origin-untrusted-host",
                             "mismatch": "origin-mismatch"}[origin])
    health = checkout_health.checkout_lossless(path)
    if health is None or not health[0]:
        return (None, None, "checkout-unhealthy")
    return (path, target, None)


def main(argv: list | None = None) -> int:
    """CLI. `preflight`: on success prints the verified environment to STDOUT (two `KEY=value` lines the runbook
    reads — `ENGINE_PRODUCT_CHECKOUT` and `GITHUB_REPOSITORY`) and exits 0; on any refusal prints a plain-language
    reason + remedy to STDERR, leaves STDOUT empty, and exits non-zero. The channel discipline is
    safety-load-bearing: a refusal string must never reach stdout, where the runbook would consume it as a path."""
    parser = argparse.ArgumentParser(
        prog="mechanic_build.py",
        description="The engine-mechanic build preflight: verify the product checkout before a cross-repo build.")
    parser.add_subparsers(dest="verb").add_parser(
        "preflight", help="resolve+verify the product checkout; emit its env or refuse fail-closed")
    args = parser.parse_args(argv)
    if args.verb == "preflight":
        path, slug, refusal = resolve_build_target()
        if refusal:
            sys.stderr.write(_REFUSALS[refusal] + "\n")
            return 1
        sys.stdout.write(f"ENGINE_PRODUCT_CHECKOUT={path}\nGITHUB_REPOSITORY={slug}\n")
        return 0
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
