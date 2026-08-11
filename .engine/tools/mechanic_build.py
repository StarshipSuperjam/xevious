#!/usr/bin/env python3
"""The engine-mechanic build entry (eADR-0026): the FAIL-CLOSED gate behind an engine-mechanic's cross-repo build.
It exposes two verbs — `preflight` (decide only) and `worktree` (decide, then cut the isolated build workspace).

WHAT IT DOES. An engine-mechanic is a deployed engine whose product is a repository the operator OWNS
(engine-template), checked out SEPARATELY beside it. Before a mechanic session builds that product and opens a
direct pull request, both verbs resolve the committed build target (`product_build_target`), resolve this
machine's local path to the checkout, and REFUSE unless the checkout's `origin` is genuinely that product on a
trusted host. They then diverge:

  - `preflight` additionally requires the shared checkout to be lossless (clean, on a branch) and, on success,
    emits the verified checkout path + slug. OFFLINE and READ-ONLY — it decides, it never writes. It is the
    identity-and-safety CHECK a session runs to confirm the arrangement is sound.
  - `worktree <name>` does NOT require the shared checkout clean — a peer session mid-build is a legitimate
    state, and the build never happens in the shared checkout. Instead it fetches and cuts a fresh, ISOLATED
    worktree of the product from `origin/<default>`, homed in the mechanic's own durable state area
    (`<engine root>/.engine/mechanic/worktrees/<name>`), and emits THAT worktree's path (as
    ENGINE_PRODUCT_WORKTREE, a DISTINCT name from preflight's ENGINE_PRODUCT_CHECKOUT — see below) + the slug.
    It NEVER moves the shared checkout's HEAD, index, or working tree; its only writes are the new worktree and
    its branch, plus the shared repo's own git metadata (fetch refs, the worktree admin entry). The build then
    runs INSIDE that worktree (`cwd=<worktree>` + `GITHUB_REPOSITORY=<verified slug>`), so concurrent sessions
    never collide on one shared working tree — the sprawl and branch-switch harms that a per-build worktree
    replaces (StarshipSuperjam/engine-template#902).

WHY THE TWO EMISSIONS USE DIFFERENT NAMES. `ENGINE_PRODUCT_CHECKOUT` is the DURABLE per-machine pointer to the
product clone (checkout_health reads it env-first, ahead of the gitignored path file). If `worktree` emitted its
EPHEMERAL path under that same name, a later `preflight` in the session would resolve to the worktree but
health-assess the MAIN checkout it links to (checkout_health.checkout_lossless resolves a linked worktree's main,
not the worktree) — emitting a path it never checked, a fail-open. So the worktree path travels as
ENGINE_PRODUCT_WORKTREE and the durable pointer keeps its one meaning.

WHY THIS FILE IS GUARDED (it is in weakening_guard._FLOOR_ENFORCEMENT_HOOKS). The belt
`product_checkout_matches` is the last line of defence behind a live cross-repo WRITE: it authorizes the
mechanic to run the checkout's own committed `.engine` tools and open a pull request against it. A weakening of
this belt — fail-open on doubt, or an unanchored host parse that accepts a look-alike origin — would let the
mechanic execute an attacker-controlled checkout's code locally and write against the wrong repository, with NO
on-disk floored correlate any check could catch. So a change here routes through the guardrail-ack — killswitch
tier (eADR-0040), unlike the disclosure-tier hook substrate (modes.py, close.py). A unit test alone is the wrong instrument: the same
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
mechanic arm — never by the validator. `preflight` is OFFLINE and READ-ONLY: it inspects local git + the manifest
and decides, never writing. `worktree` is the one verb that ACTS: it fetches and creates a worktree + branch. It
still commits nothing and opens no pull request — the runbook's ordinary Build steps do that, inside the emitted
worktree. Neither verb ever writes to the shared checkout's working tree or moves its HEAD.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checkout_health  # noqa: E402  (the OFFLINE readers + fail-soft health probes; this module adds the gate)

# owner/repo parsed ONLY from a genuine github.com origin. The leading host anchor is load-bearing: `github.com`
# must be the URL host (after an optional scheme and optional `user@`), never a substring of a look-alike
# (`notgithub.com`, `github.com.evil.com`) — see the module docstring's HOST ANCHOR note. IGNORECASE folds the
# literal host (`GitHub.com` == `github.com`, case-insensitive by spec); ASCII keeps that fold ASCII-only, so a
# Unicode homograph (`gİthub.com`, where U+0130 folds to `i`) cannot satisfy the `github.com` literal and pass
# this belt as a genuine origin. The flags never touch the structural anchors, so the security boundary is
# unchanged — no look-alike host is newly accepted; a genuine mixed-case origin that previously mis-classified
# as `untrusted-host` is now read correctly (StarshipSuperjam/engine-template#625).
_GITHUB_SLUG_RE = re.compile(r"^(?:(?:https?|ssh)://)?(?:[^@/]+@)?github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$",
                             re.IGNORECASE | re.ASCII)

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
    # `worktree` verb refusals (identity reuses the taxonomy above; these are the workspace-creation reasons):
    "bad-name": (
        "The worktree name is not allowed. Use letters, digits, dot, dash or underscore (no leading dash or "
        "dot, no path separators, no '..'). Pick a simple name like the issue number and a short slug."),
    "engine-root-unresolved": (
        "Could not resolve this engine's own checkout root, so there is nowhere to home the build worktree. "
        "Run this from inside the engine-mechanic checkout (a normal git working tree)."),
    "default-unresolved": (
        "Could not determine the product's default branch with confidence, so the mechanic will not cut a "
        "worktree from a guessed base. Ensure the product clone has a tracked origin/HEAD (git remote set-head "
        "origin -a)."),
    "fetch-failed": (
        "Could not fetch the product's origin after retrying, so the worktree base could be stale. A concurrent "
        "peer session fetching the same clone can cause a transient git lock (it usually self-heals on retry); "
        "otherwise check your network and the product clone's origin. Run `git -C <product checkout> fetch "
        "origin` yourself to see the exact error — the mechanic will not build from an unfetched base."),
    "origin-moved": (
        "The product checkout's origin changed while preparing the worktree, so the mechanic stopped rather "
        "than write against a repository it did not verify. Re-run once the origin is stable."),
    "worktree-exists": (
        "A build worktree of that name already exists. Another session may be using it — pick a different name, "
        "or if it is yours and finished, remove it first (git -C <product checkout> worktree remove <path>)."),
    "branch-exists": (
        "A build branch of that name already exists. Another session may have claimed this build — pick a "
        "different name, or if it is yours and merged, delete it: git -C <product checkout> branch -D "
        "claude/<name>."),
    "worktree-add-failed": (
        "Creating the build worktree failed (git reported an error). Run `git -C <product checkout> worktree add "
        "<path> -b claude/<name> origin/<default>` yourself to see the exact error; check git is recent enough "
        "(2.17+) and the product clone is healthy, then try again."),
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


def _resolve_verified_identity(cwd: str | None = None) -> tuple[str | None, str | None, str | None]:
    """FAIL-CLOSED identity resolution shared by both verbs: the committed target AND a resolved checkout path
    whose `origin` is the host-anchored match — WITHOUT the working-tree health leg. Returns
    `(checkout_path, product_slug, refusal)` with exactly one side populated, using the same ordered,
    mutually-exclusive taxonomy as `resolve_build_target` up to (not including) the health check:
    `not-a-mechanic` -> `path-unset` -> `checkout-unreadable` -> `origin-untrusted-host` -> `origin-mismatch`.

    This is the SINGLE identity gate: `resolve_build_target` adds the cleanliness leg on top for the in-place
    check, and `create_worktree` uses it WITHOUT that leg — deliberately, because a fresh worktree is cut from
    `origin/<default>` and cannot ingest the shared checkout's working state, so a peer mid-build there is
    legitimate. Keeping this factored means the host-anchored security predicate is defined once and cannot
    drift between the two verbs (the same discipline `_classify_origin` already applies)."""
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
    return (path, target, None)


def resolve_build_target(cwd: str | None = None) -> tuple[str | None, str | None, str | None]:
    """FAIL-CLOSED resolution of the mechanic's IN-PLACE build target. Returns `(checkout_path, product_slug,
    refusal)` with exactly one side populated:
      - a refusal (path/slug None) — one of the ordered, mutually-exclusive reasons in `_REFUSALS`:
        `not-a-mechanic` (no target recorded) -> `path-unset` (target recorded, local path missing) ->
        `checkout-unreadable` (path is not a readable git checkout) -> `origin-untrusted-host` (origin is not a
        genuine github.com repo) -> `origin-mismatch` (origin read but != target) -> `checkout-unhealthy`
        (dirty / detached / paused git op).
      - `(path, slug, None)` — verified: the checkout at `path` is the committed target on github.com AND is
        safe to write into. `slug` is the committed target (canonical), suitable for `GITHUB_REPOSITORY`.

    INVARIANT (pinned by test): NEVER returns a path unless the host-anchored belt passed AND the health check
    passed — this is the whole authorization, so no early-out may bypass either. The belt is the shared
    `_resolve_verified_identity`; the health leg is added here and here only."""
    path, target, refusal = _resolve_verified_identity(cwd)
    if refusal:
        return (None, None, refusal)
    health = checkout_health.checkout_lossless(path)
    if health is None or not health[0]:
        return (None, None, "checkout-unhealthy")
    return (path, target, None)


# A build-worktree name: a filesystem leaf AND (as `claude/<name>`) a git branch. The conservative charset keeps
# it from becoming a traversal (`../`), a git option (a leading `-`), or a forged stdout line (a newline) — the
# three ways an unvalidated name would breach this guarded tool's containment or channel discipline. Anchored,
# ASCII, no leading dash/dot; `..` is rejected separately (belt-and-braces with a realpath containment check).
_WORKTREE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$", re.ASCII)
_WORKTREES_REL = os.path.join(".engine", "mechanic", "worktrees")


def _valid_worktree_name(name: str | None) -> bool:
    """True only for a name that is safe as both a path leaf and a git ref segment (see `_WORKTREE_NAME_RE`).
    `..` is impossible under the charset (a dot is allowed but `..` needs two adjacent dots with nothing else at
    a boundary the regex still permits, e.g. `a..b`), so it is rejected explicitly rather than trusted to the
    pattern."""
    return bool(name) and bool(_WORKTREE_NAME_RE.match(name)) and ".." not in name


def _branch_exists(product_path: str, branch: str) -> bool:
    """True if `branch` already exists as a local branch in the product clone (a fail-closed pre-check so the
    verb refuses with a plain reason instead of letting `worktree add` error opaquely)."""
    return _run(["git", "-C", product_path, "rev-parse", "--verify", "--quiet",
                 f"refs/heads/{branch}"]) is not None


def _prune_stale_dest(product_path: str, dest: str) -> None:
    """Clear a stale registration blocking `dest` (a worktree whose directory is gone), so re-using a name after
    its folder was manually deleted does not wedge on a phantom entry. This is GATED to run only when `dest`
    itself is registered-but-missing, so the ordinary create path (a fresh name) prunes nothing. NOTE the git
    call it makes — `git worktree prune` — is inherently repo-wide (git has no per-path prune), so once it fires
    it clears EVERY stale registration in the clone, not only `dest`'s. That is still safe: prune removes solely
    entries whose directories no longer exist, so a peer's LIVE worktree (its directory present) is never
    touched — it can, at most, also clear some other already-dead registration a bit earlier than a manual
    prune would have."""
    if os.path.exists(dest):
        return
    listing = _run(["git", "-C", product_path, "worktree", "list", "--porcelain"]) or ""
    dest_real = os.path.realpath(dest)
    registered = any(
        os.path.realpath(line[len("worktree "):].strip()) == dest_real
        for line in listing.splitlines() if line.startswith("worktree "))
    if registered:
        _run(["git", "-C", product_path, "worktree", "prune"])


# Bounded retry for the fetch: concurrent `worktree` calls against the ONE shared clone can collide on git's
# `.git/config`/ref locks during fetch — a peer session mid-build is exactly the supported case — and fail
# spuriously. A short bounded retry lets that self-heal (the StarshipSuperjam/engine-template#704 self-healing-retry pattern), so a transient
# lock is not misreported as a stale base. Kept as module constants so a test can zero the backoff.
_FETCH_ATTEMPTS = 3
_FETCH_BACKOFF_SEC = 0.5


def _fetch_origin_with_retry(product_path: str) -> bool:
    """`git fetch origin`, retried a bounded number of times on failure (True on success, False if all attempts
    failed). Fetch is idempotent, so retrying is safe; the retry absorbs the transient shared-`.git` lock
    contention two concurrent `worktree` cuts can cause."""
    for attempt in range(_FETCH_ATTEMPTS):
        if _run(["git", "-C", product_path, "fetch", "origin"]) is not None:
            return True
        if attempt + 1 < _FETCH_ATTEMPTS:
            time.sleep(_FETCH_BACKOFF_SEC)
    return False


def create_worktree(name: str, cwd: str | None = None) -> tuple[str | None, str | None, str | None, str | None]:
    """FAIL-CLOSED: cut a fresh, ISOLATED worktree of the verified product from `origin/<default>`, homed under
    the mechanic's own durable `.engine/mechanic/worktrees/<name>`. Returns `(worktree_path, product_slug,
    base_ref, refusal)` with either the first three populated (success; `base_ref` is `origin/<default>`, the
    base the worktree was cut from — the ref to diff a build against) or only `refusal`. The identity gate is the
    shared `_resolve_verified_identity`
    (host-anchored origin match) — WITHOUT the shared checkout's cleanliness leg, by design: the build never
    touches the shared working tree, so a peer mid-build there is legitimate. It NEVER moves the shared
    checkout's HEAD/index/tree; its only writes are the new worktree, its `claude/<name>` branch, and the shared
    repo's own git metadata (fetch refs + the worktree admin entry).

    Verify-then-write discipline (the `--repo`-pinning idea, applied to a creating verb): the verified `origin`
    URL is captured, and re-read immediately before `worktree add` — a mid-operation origin repoint refuses
    (`origin-moved`) rather than letting the cut follow a repository nobody verified. A failed fetch refuses
    (`fetch-failed`); it never falls back to a stale remote-tracking ref."""
    if not _valid_worktree_name(name):
        return (None, None, None, "bad-name")
    product_path, target, refusal = _resolve_verified_identity(cwd)
    if refusal:
        return (None, None, None, refusal)
    root = checkout_health.engine_common_checkout(cwd)
    if not root:
        return (None, None, None, "engine-root-unresolved")
    worktrees_dir = os.path.join(root, _WORKTREES_REL)
    dest = os.path.join(worktrees_dir, name)
    # Containment: redundant with the charset today (which forbids path separators, so dest's parent is always
    # worktrees_dir), kept so a later charset change that allowed subdirectories cannot silently open a
    # traversal. Re-verify this guard if the charset is ever loosened — today it is defense-in-depth, not a
    # live second line.
    if os.path.realpath(os.path.dirname(dest)) != os.path.realpath(worktrees_dir):
        return (None, None, None, "bad-name")
    branch = f"claude/{name}"
    # Cheap, local collision checks first — a name clash is reported without any network work. Prune a stale
    # registration for THIS dest (missing directory) so a re-used name does not wedge on a phantom entry.
    _prune_stale_dest(product_path, dest)
    if os.path.exists(dest):
        return (None, None, None, "worktree-exists")
    if _branch_exists(product_path, branch):
        return (None, None, None, "branch-exists")
    # Now the network path, verify-then-write: capture the verified origin, resolve the default, fetch (with a
    # bounded retry for transient concurrent-lock contention), and re-verify the origin has not moved before the
    # cut.
    origin_url = _git_origin_url(product_path)   # the verified origin, captured before any write
    default = checkout_health.confident_default_branch(product_path)
    if not default:
        return (None, None, None, "default-unresolved")
    if not _fetch_origin_with_retry(product_path):
        return (None, None, None, "fetch-failed")
    if _git_origin_url(product_path) != origin_url:   # re-verify: a mid-operation repoint stops the write
        return (None, None, None, "origin-moved")
    os.makedirs(worktrees_dir, exist_ok=True)
    if _run(["git", "-C", product_path, "worktree", "add", dest, "-b", branch,
             f"origin/{default}"]) is None:
        return (None, None, None, "worktree-add-failed")
    return (dest, target, f"origin/{default}", None)


def main(argv: list | None = None) -> int:
    """CLI with two verbs. `preflight`: on success prints the verified environment to STDOUT (two `KEY=value`
    lines the runbook reads — `ENGINE_PRODUCT_CHECKOUT` and `GITHUB_REPOSITORY`) and exits 0. `worktree <name>`:
    on success prints `ENGINE_PRODUCT_WORKTREE` (the isolated worktree path — a DISTINCT name from preflight's,
    so the durable pointer is never overwritten), `ENGINE_PRODUCT_BASE` (the `origin/<default>` ref it was cut
    from — the base a build diffs against), and `GITHUB_REPOSITORY`, and exits 0. On any refusal either verb
    prints a plain-language reason + remedy to STDERR, leaves STDOUT empty, and exits non-zero. The channel
    discipline is safety-load-bearing: a refusal string must never reach stdout, where the runbook would consume
    it as a path."""
    parser = argparse.ArgumentParser(
        prog="mechanic_build.py",
        description="The engine-mechanic build gate: verify the product checkout, and cut the build worktree.")
    subs = parser.add_subparsers(dest="verb")
    subs.add_parser("preflight", help="resolve+verify the product checkout; emit its env or refuse fail-closed")
    wt = subs.add_parser("worktree", help="verify, then cut an isolated build worktree; emit its env or refuse")
    wt.add_argument("name", help="a short name (issue number + slug); becomes the worktree dir and claude/<name>")
    args = parser.parse_args(argv)
    if args.verb == "preflight":
        path, slug, refusal = resolve_build_target()
        if refusal:
            sys.stderr.write(_REFUSALS[refusal] + "\n")
            return 1
        sys.stdout.write(f"ENGINE_PRODUCT_CHECKOUT={path}\nGITHUB_REPOSITORY={slug}\n")
        return 0
    if args.verb == "worktree":
        path, slug, base, refusal = create_worktree(args.name)
        if refusal:
            sys.stderr.write(_REFUSALS[refusal] + "\n")
            return 1
        sys.stdout.write(f"ENGINE_PRODUCT_WORKTREE={path}\nENGINE_PRODUCT_BASE={base}\nGITHUB_REPOSITORY={slug}\n")
        return 0
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
