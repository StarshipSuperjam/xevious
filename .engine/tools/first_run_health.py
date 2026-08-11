#!/usr/bin/env python3
"""first_run_health — the standing "this copy hasn't finished first-run setup" detector (issue StarshipSuperjam/engine-template#353).

Catches when a repo created from the engine template ("Use this template", or a clone-and-push of it) is
still sitting in its un-transformed, as-copied shape — so [boot] can OFFER to walk the operator
through `/engine-setup` on the first session, instead of the repo silently reporting itself "already set up"
and stranding the adopter. The detect / surface / consent split mirrors the leftover-LICENSE detector
(`license_health`) and the stranded-checkout detector (`checkout_health`): provisioning detects, boot
surfaces, the operator consents.

Why a detector is needed at all: `instantiator.is_provisioned()` keys "already set up" off the presence of
`.engine/engine.json`, but that manifest TRAVELS with the template — every generated copy inherits it — so
presence alone cannot tell a fresh copy from a finished one. This module derives the un-set-up state from
OBSERVABLE installed shape instead, using two grounded signals:

  1. DOWNSTREAM COPY — the recorded update home (`.engine/engine.json` `home_repository`) is a DIFFERENT
     repository than this checkout's own git origin. The workshop where the engine is built has
     origin == home; every downstream copy inherits the upstream home while its origin is the new repo.
     Compared with `module_coherence.slug_eq` (casefold / `.git` / slash) so an SSH-vs-HTTPS or case-skewed
     origin never mis-reads, and SAFE-quiet (no fire) whenever origin or home cannot be read.
  2. SETUP NOT FINISHED — the one-time setup tool `.engine/tools/instantiator.py` is STILL PRESENT. That
     tool self-deletes at `retire`, the last step of first-run, so its presence is the design's own "not
     done yet" signal (the same `os.path.isfile(...instantiator.py)` check the instantiator's arrival/finish
     demos already use). Keying on the TOOL — not on whether the floor CLAUDE.md was swapped — covers BOTH
     an untouched fresh copy and a setup INTERRUPTED partway (before or after the floor swap): the remedy is
     identical, since `/engine-setup` resumes idempotently (StarshipSuperjam/engine-template#519 §1).

A FORK of the engine's own home (a contributor's fork: origin != home, and the one-time setup tool still
present, because a fork of the engine's repo carries it) is the one offline false-positive the two signals
cannot separate from a fresh copy — the two are identical on disk, since a fork and a fresh copy inherit the
same committed files. It is separated ONLINE by `forked_from_home`, a best-effort, token-gated GitHub read of
the repo's fork parentage (a fork of the engine home is NOT an adopter to nag). Offline (no token) the offer
still shows; it is read-only and low-harm, and the engine's own development actually happens in worktrees on
`origin == home`, where the downstream-copy signal never fires.

OFFLINE + READ-ONLY at the core (`detect_first_run_pending`), the online parentage read kept OFF that
critical path (the `checkout_health`/`license_health` offline-online seam). Fix-never-here: the offer routes
to `/engine-setup`; boot renders the plain-language offer and never runs setup itself.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import module_coherence  # noqa: E402  (slug_eq — the ONE normalized slug comparison, single-homed there)

# The one-time setup tool, relative to a checkout root. Its presence is the design's "first-run not finished"
# signal — `instantiator.retire` self-deletes it as the final setup step.
_SETUP_TOOL_REL = os.path.join(".engine", "tools", "instantiator.py")
_MANIFEST_REL = os.path.join(".engine", "engine.json")
# host-anchored: github.com must be the URL host, never a substring of a look-alike (notgithub.com,
# github.com.evil.com) — consistent with mechanic_build/boot's belts (defense-in-depth; this parser only
# decides whether to OFFER first-run setup, but a mis-parse should never treat a look-alike as the home).
# IGNORECASE reads a mixed-case host (`GitHub.com`); ASCII keeps that fold ASCII-only so a Unicode homograph
# (`gİthub.com`, where U+0130 folds to `i`) cannot satisfy the host literal (StarshipSuperjam/engine-template#625).
_GITHUB_SLUG_RE = re.compile(
    r"^(?:(?:https?|ssh)://)?(?:[^@/]+@)?github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", re.IGNORECASE | re.ASCII)


def _run(cmd: list, cwd: str | None = None, timeout: int = 30) -> str | None:
    """Run a local git command and return raw stdout, or None on any non-zero / failure. Never raises —
    every read is best-effort (the `checkout_health` / `license_health` convention)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd)
        return out.stdout if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a missing binary / timeout / OS error all degrade to "unavailable"
        return None


def _main_checkout(cwd: str | None = None) -> str | None:
    """Resolve the operator's MAIN checkout from this session's worktree, OFFLINE (the path only). The main
    worktree is listed FIRST by `git worktree list --porcelain`; falls back to `--git-common-dir`'s parent.
    None on a bare repo or when it cannot be resolved. Mirrors `license_health._main_checkout` so the
    first-run verdict is judged against the operator's real product checkout, not an engine worktree."""
    porcelain = _run(["git", "worktree", "list", "--porcelain"], cwd=cwd)
    if porcelain:
        first = porcelain.split("\n\n", 1)[0]   # the first stanza is the main worktree
        path = None
        bare = False
        for line in first.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif line.strip() == "bare":
                bare = True
        if bare:
            return None
        if path:
            return path
    common = _run(["git", "rev-parse", "--git-common-dir"], cwd=cwd)
    if common:
        common = common.strip()
        base = cwd or os.getcwd()
        abs_common = common if os.path.isabs(common) else os.path.join(base, common)
        abs_common = os.path.normpath(os.path.abspath(abs_common))
        if os.path.basename(abs_common) == ".git":
            return os.path.dirname(abs_common)
    return None


def _origin_slug(main: str) -> str | None:
    """The `owner/repo` of the EXAMINED checkout's git origin remote, OFFLINE. Read from the checkout on disk
    (NOT this process's `GITHUB_REPOSITORY` env), so a fixture/test can set a fake origin and the detector
    reads THAT — the injection seam boot's env-first `repo_slug()` cannot provide. None when there is no
    origin remote or the URL is not a recognizable GitHub slug."""
    url = _run(["git", "-C", main, "remote", "get-url", "origin"])
    if not url:
        return None
    m = _GITHUB_SLUG_RE.search(url.strip())
    return m.group(1) if m else None


def _recorded_home(main: str) -> str | None:
    """The `home_repository` slug recorded in the EXAMINED checkout's installed manifest, OFFLINE. None when
    the manifest is absent, unreadable, malformed, or records no/blank home (all fail-soft — an unreadable
    home means "cannot place", never a crash and never a fire)."""
    try:
        with open(os.path.join(main, _MANIFEST_REL), encoding="utf-8") as fh:
            home = json.load(fh).get("home_repository")
        return home if isinstance(home, str) and home.strip() else None
    except Exception:  # noqa: BLE001 — absent / unreadable / malformed manifest -> cannot place -> quiet
        return None


def detect_first_run_pending(cwd: str | None = None) -> dict | None:
    """OFFLINE, READ-ONLY. Returns {"present": True, "main": <path>, "home": <slug>, "own": <slug>} when the
    examined main checkout is a DOWNSTREAM copy (recorded home != its own origin) whose first-run setup is
    NOT finished (the one-time setup tool is still present); else None. Every non-fire path is fail-soft
    quiet (unresolvable checkout, no origin, no/absent home, home == own the workshop, tool already retired,
    or any error) — it never crashes boot's SessionStart, and never fires where it cannot positively place
    the repo. Does NOT read `GITHUB_REPOSITORY` or `home_repository()` from this process's root — everything
    is read from the examined checkout, so the verdict is about the operator's repo and is fixture-forceable."""
    try:
        main = _main_checkout(cwd)
        if main is None:
            return None
        if not os.path.isfile(os.path.join(main, _SETUP_TOOL_REL)):
            return None                       # setup tool retired -> first-run finished -> nothing to offer
        home = _recorded_home(main)
        own = _origin_slug(main)
        if not module_coherence.is_downstream_copy(own, home):
            return None                       # workshop (home == own), or origin/home unreadable -> quiet
        return {"present": True, "main": main, "home": home, "own": own}
    except Exception:  # noqa: BLE001 — any unexpected failure degrades this one signal, never the pack
        return None


def detect_home_workshop(cwd: str | None = None) -> dict | None:
    """OFFLINE, READ-ONLY. Returns {"present": True, "main": <path>, "home": <slug>, "own": <slug>} when the
    examined main checkout IS the engine's own home — its git origin equals the recorded home_repository. The
    STRICT-POSITIVE complement of detect_first_run_pending's downstream-copy test: it fires ONLY when BOTH the
    origin and the recorded home resolve AND match — unlike `repo_identity.is_home_repo`, which fails TOWARD
    home for its safety-check callers. That direction is deliberate here: an unreadable-origin deployed repo
    must NOT get the home framing, because the home overlay points a session at `engine-development.md`, which
    is retired from a generated copy and does not exist there. None on any non-home or unresolvable case; never
    crashes boot. Mutually exclusive with detect_first_run_pending (a placed checkout is home XOR a downstream
    copy)."""
    try:
        main = _main_checkout(cwd)
        if main is None:
            return None
        home = _recorded_home(main)
        own = _origin_slug(main)
        # Strict positive: both must resolve AND name the same repo. `not is_downstream_copy` ALONE is too weak
        # (it is also False when origin/home are unreadable); requiring `own and home` present closes that gap,
        # so an unreadable origin reads as "not confirmably home" and the overlay stays quiet.
        if not (own and home and not module_coherence.is_downstream_copy(own, home)):
            return None
        return {"present": True, "main": main, "home": home, "own": own}
    except Exception:  # noqa: BLE001 — any unexpected failure degrades this one signal, never the pack
        return None


# The LOCAL awaiting-landing marker instantiator.retire drops when first-run setup is APPLIED but not yet landed
# through review (StarshipSuperjam/engine-template#810). It lives under the boot presentation cache — an engine-managed GITIGNORED path (the
# `.engine/boot/.cache/` block ships in the template), so it is LOCAL: never committed, so it does not travel
# through the landing commit, and it survives the operator's own branch->PR->merge->pull. Its presence is what
# lets the post-landing "Setup is now complete" confirmation fire EXACTLY ONCE in the operator's own checkout;
# a repo set up before this shipped (no marker) never shows the confirmation spuriously.
_LANDING_MARKER_REL = os.path.join(".engine", "boot", ".cache", "first-run-landing.json")


def mark_first_run_applied(main: str) -> bool:
    """Write the local awaiting-landing marker after retire applies setup. FAIL-SOFT: a write failure only means
    the one-time completion confirmation won't fire (setup itself still succeeded); it never raises into retire."""
    try:
        path = os.path.join(main, _LANDING_MARKER_REL)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "awaiting_landing": True}, fh)
        return True
    except Exception:  # noqa: BLE001 — best-effort; retire must complete regardless of a marker write failure
        return False


def clear_first_run_marker(main: str) -> None:
    """Remove the awaiting-landing marker once the completion confirmation has been surfaced (show-once). FAIL-SOFT:
    a failed clear only risks the confirmation showing again next start, never a crash."""
    try:
        os.remove(os.path.join(main, _LANDING_MARKER_REL))
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 — a failed clear is harmless (at worst a duplicate confirmation)
        pass


def _current_branch(main: str) -> str | None:
    b = _run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "HEAD"])
    return b.strip() if b and b.strip() else None


def _default_branch(main: str) -> str | None:
    """The default branch name from `origin/HEAD`, OFFLINE. None when unresolvable (a fresh clone may not have set
    it) — the caller then treats durability as unconfirmable and stays quiet, never guessing."""
    head = _run(["git", "-C", main, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
    if head and head.strip().startswith("origin/"):
        return head.strip().split("origin/", 1)[1] or None
    return None


def detect_setup_landed(cwd: str | None = None) -> dict | None:
    """OFFLINE, READ-ONLY (StarshipSuperjam/engine-template#810). Returns {"present": True, "main": <path>} when first-run setup was APPLIED in
    this checkout (the local awaiting-landing marker exists) AND the transformation is now DURABLE: the one-time
    setup tool is retired, the working tree is CLEAN, and the checkout sits on its DEFAULT branch (so the setup
    was committed and landed through review, not left uncommitted or parked on a setup branch). This is the
    post-landing "Setup is now complete" confirmation boot surfaces ONCE (then clears the marker). None — quiet —
    when there is no marker (a repo set up before this shipped, or already confirmed), the state is not yet
    durable (applied but not landed), git state is unreadable, or the checkout cannot be resolved. Deliberately
    OFFLINE: 'durable' means committed + clean + on the default branch, never a network freshness guarantee."""
    try:
        main = _main_checkout(cwd)
        if main is None:
            return None
        if not os.path.isfile(os.path.join(main, _LANDING_MARKER_REL)):
            return None                                   # no marker -> nothing to confirm
        if os.path.isfile(os.path.join(main, _SETUP_TOOL_REL)):
            return None                                   # setup tool still present -> retire hasn't finished
        status = _run(["git", "-C", main, "status", "--porcelain"])
        if status is None or status.strip():
            return None                                   # unreadable or dirty -> applied but not yet durable
        current, default = _current_branch(main), _default_branch(main)
        if not current or not default or current != default:
            return None                                   # off the default (e.g., still on a setup branch)
        return {"present": True, "main": main}
    except Exception:  # noqa: BLE001 — any failure degrades this one signal, never the pack
        return None


def forked_from_home(repo: str | None, token: str | None, home: str | None, transport=None) -> bool | None:
    """ONLINE, best-effort, READ-ONLY: is `repo` a FORK of the engine's own home `home`? True (suppress the
    offer — a fork of the engine home is a contributor's fork, not an adopter to nag), False (not a fork of
    home — a genuine template copy / clone, keep offering), or None when it can't be determined (no
    repo/token/home, offline, or any error — the caller treats None as "don't suppress, offer normally").
    Kept OFF `detect_first_run_pending` so a network round-trip never sits on the offline detector's critical
    path. Only a fork OF THE HOME suppresses: a template-generated copy and a clone-and-push copy are both
    `fork == false`, so neither is silenced and the dead-on-arrival case they represent is still caught.
    `transport(method, path, body) -> (status, json)` is injectable (the `GitHubIssues` seam) so a test drives
    the real fork/parent decision logic without a network round-trip. NOTE (issue StarshipSuperjam/engine-template#353): this deliberately
    silences a FORK-based *adopter* too (indistinguishable from a contributor's fork through this one signal);
    a fork-adopter is still rescued via `/engine-setup show`, which does not apply this suppressor. Revisit
    when the project's contribution model is defined (see README 'Contributing')."""
    if not repo or not token or not home:
        return None
    try:
        import telemetry
        gh = telemetry.GitHubIssues(repo, token, transport=transport)
        status, data = gh._transport("GET", f"/repos/{repo}", None)
        if status >= 400 or not isinstance(data, dict):
            return None
        if not data.get("fork"):
            return False
        parent = (data.get("parent") or {}).get("full_name")
        return module_coherence.slug_eq(parent, home)
    except Exception:  # noqa: BLE001 — any read failure degrades this one signal quietly
        return None


# ---- in-tool demo: a self-checking falsification (issue StarshipSuperjam/engine-template#353) --------------------------------

def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False)


def _fixture(tmp: str, name: str, *, origin: str, home: str,
             tool_present: bool = True, floor_swapped: bool = False) -> str:
    """A throwaway committed git checkout with a set origin remote, an installed manifest recording `home`,
    optionally the one-time setup tool, and a placeholder root CLAUDE.md. The CLAUDE.md content is incidental —
    detect_first_run_pending keys on the origin vs recorded home and the setup tool's presence, never the file's
    text — so `floor_swapped` only varies a cosmetic label (a fresh copy inherits the committed floor either way
    since StarshipSuperjam/engine-template#323)."""
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, ".engine", "tools"), exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "remote", "add", "origin", origin)
    with open(os.path.join(root, _MANIFEST_REL), "w", encoding="utf-8") as fh:
        json.dump({"engine_release": "0.0.0", "home_repository": home}, fh)
    if tool_present:
        with open(os.path.join(root, _SETUP_TOOL_REL), "w", encoding="utf-8") as fh:
            fh.write("# the one-time setup tool (placeholder for the fixture)\n")
    with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write("# Your project runs on an Engine\n" if floor_swapped
                 else "# a fresh copy of the engine template\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return root


def _demo() -> int:
    import tempfile
    home = "StarshipSuperjam/engine-template"
    print("What this proves: the offer fires on a fresh (or partway-set-up) copy of the template and stays")
    print("quiet in the workshop and in a finished project.\n")
    with tempfile.TemporaryDirectory() as tmp:
        fresh = _fixture(tmp, "fresh", origin="https://github.com/adopter/their-product.git", home=home)
        workshop = _fixture(tmp, "workshop", origin=f"https://github.com/{home}.git", home=home)
        finished = _fixture(tmp, "finished", origin="https://github.com/adopter/their-product.git",
                            home=home, tool_present=False, floor_swapped=True)
        half = _fixture(tmp, "half", origin="https://github.com/adopter/their-product.git",
                        home=home, tool_present=True, floor_swapped=True)

        d_fresh = detect_first_run_pending(cwd=fresh)
        d_workshop = detect_first_run_pending(cwd=workshop)
        d_finished = detect_first_run_pending(cwd=finished)
        d_half = detect_first_run_pending(cwd=half)

        print(f"1) A fresh 'Use this template' copy (origin != home, setup tool present) -> OFFERS: "
              f"{d_fresh is not None}")
        print(f"2) The workshop where the engine is built (origin == home)               -> quiet (None): "
              f"{d_workshop is None}")
        print(f"3) A finished project (setup tool retired)                               -> quiet (None): "
              f"{d_finished is None}")
        print(f"4) A setup INTERRUPTED after the floor swap (tool still present)         -> OFFERS (resume): "
              f"{d_half is not None}")

        print("\n5) The plain-language line the operator sees (an onboarding OFFER):\n")
        import boot  # lazy: boot is fully loaded by demo time
        signals = boot.gather_signals()
        signals["first_run"] = {"present": True, "main": "/your/project/folder",
                                "home": home, "own": "adopter/their-product"}
        rendered = boot.render_dashboard(signals)
        print(rendered)

        # Self-check: the detector separates the four shapes, and boot renders a non-empty offer for the fire.
        ok = (d_fresh is not None and d_workshop is None and d_finished is None and d_half is not None
              and "set up" in rendered.lower())
        if not ok:
            print("\nDEMO UNEXPECTED: detection or the boot offer line did not behave as expected.",
                  file=sys.stderr)
            return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "check":
        d = detect_first_run_pending()
        if d is None:
            print("First-run setup looks finished (or this is the workshop / can't be placed).")
        else:
            print(f"First-run setup has not finished for {d['main']} "
                  f"(origin {d['own']} != update home {d['home']}).")
        return 0
    print("usage: first_run_health.py [demo|check]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
