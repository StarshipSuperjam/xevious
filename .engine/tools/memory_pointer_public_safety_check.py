#!/usr/bin/env python3
"""Memory-backup pointer public-safety guard (StarshipSuperjam/engine-template#224) — the thin custom/script entry for
engine/check/memory-pointer-public-safety.

The committed memory-backup pointer (.engine/memory-backup/pointer.json) carries the vault's COORDINATES
(owner/repo/namespace). In a DEPLOYED project committing them is the operator's own choice — the
config-not-data carve-out, and the saved-memory read is legitimate on a public repo (only the digest CONTENT is
gated, never the pointer). But the PUBLIC engine-template CONSTRUCTION repo must always ship the unconfigured
PLACEHOLDER, so a maintainer's real vault coordinates can never travel to everyone who uses the template. This
backstop catches an accidental CONFIGURED pointer committed to the construction repo.

It is HOME-SCOPED: it acts only in the engine's own home repository — the checkout whose git origin equals the
recorded `home_repository`, the non-inherited signal a downstream copy never carries (via the shared
`repo_identity.is_home_repo` seam). So it no-ops in any generated/deployed repo — never flagging the operator's
own legitimate choice. (Historically this keyed off the root CLAUDE.md "construction governance" marker, a proxy
that both TRAVELS into every copy and disappears the moment that file becomes the deployed floor; the structural
origin==home signal replaces it.) It reads the COMMITTED pointer via `git show HEAD:` (not the working tree), so the maintainer's local
skip-worktree-configured pointer — which is never committed — is correctly ignored and a local floor run stays
green.

It reads local committed state only — no network, no token. It emits finding.v1 JSON on stdout (the
custom/script machine channel) and returns 0: an empty array when safe (placeholder, or not the engine's own
home repository, or the committed state can't be determined), one `hard` finding when a CONFIGURED pointer is
committed in the engine's own home. Best-effort: an unreadable committed pointer degrades to a pass — a
backstop never false-fails a build over a condition it can't read. The GATE degrades the other way, toward
running: an unreadable origin or manifest fails TOWARD home, so the guard would rather check unnecessarily
than skip in silence.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import repo_identity  # noqa: E402  (is_home_repo — the shared origin==home seam this now gates on)

POINTER_REL = ".engine/memory-backup/pointer.json"


def _in_home_repo() -> bool:
    """True iff this checkout is the engine's OWN home repo — its git origin equals the recorded
    `home_repository`, the non-inherited signal a downstream copy never carries. Delegates to the shared
    `repo_identity.is_home_repo` seam, which fails TOWARD home under an unreadable origin — the safe direction
    for this backstop (it RUNS rather than silently no-opping). Kept as a named predicate because both this
    check's tests monkeypatch it to drive the gate; the name is legacy (it answers "is this the home repo?")."""
    return repo_identity.is_home_repo(validate.ROOT)


def _committed_pointer_text(pointer_rel: str = POINTER_REL) -> "str | None":
    try:
        out = subprocess.run(["git", "show", f"HEAD:{pointer_rel}"], cwd=validate.ROOT,
                             capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — git absent / detached HEAD / timeout -> can't determine -> pass (backstop)
        return None
    return out.stdout if out.returncode == 0 else None


def is_configured_pointer(text: str) -> bool:
    """Configured == it carries vault coordinates; the placeholder is {"schema_version": 1, "configured": false}."""
    try:
        p = json.loads(text)
    except Exception:  # noqa: BLE001 — an unparseable committed pointer is a schema concern, not this one's
        return False
    return isinstance(p, dict) and all(isinstance(p.get(k), str) and p.get(k)
                                       for k in ("owner", "repo", "namespace"))


def check(pointer_rel: str = POINTER_REL) -> "dict | None":
    """The guard result: a `hard` finding when a configured pointer is committed in the home repo, else
    None (safe / not applicable). Separated from main() so a test can drive it directly. `pointer_rel` is the
    committed path read via `git show HEAD:` — overridable so the negative-fixture meta-check can point it at a
    committed fixture pointer; the home-scope gate is NOT overridable (a backdoor past it would defeat
    this safety check)."""
    if not _in_home_repo():
        return None
    text = _committed_pointer_text(pointer_rel)
    if text is None or not is_configured_pointer(text):
        return None
    return validate.finding(
        "hard",
        "The committed memory-backup pointer in this public template carries real vault coordinates instead of "
        'the unconfigured placeholder. Restore the placeholder ({"schema_version": 1, "configured": false}) so a '
        "maintainer's private vault location never ships to everyone who uses the template, and keep your real "
        f"pointer local with `git update-index --skip-worktree {POINTER_REL}`.",
        {"file": POINTER_REL, "line": None})


def main() -> int:
    # ENGINE_POINTER_REL (unset in production) lets the negative-fixture meta-check redirect the
    # `git show HEAD:` read at a committed fixture pointer carrying placeholder-violating coordinates,
    # so this safety gate is witnessed biting a real bad input (StarshipSuperjam/engine-template#286). It is a repo-relative pathspec
    # (passed verbatim to `git show HEAD:`), NOT resolved to an absolute path. The committed-state read
    # means the fixture only bites once committed at HEAD (so the live witness is in CI).
    pointer_rel = os.environ.get("ENGINE_POINTER_REL") or POINTER_REL
    f = check(pointer_rel)
    print(json.dumps([f] if f is not None else []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
