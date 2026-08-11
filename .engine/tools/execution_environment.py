#!/usr/bin/env python3
"""Execution-environment awareness — observe the runtime doing the work, compare it against the operator's
committed qualification baseline (.engine/state/execution.json), and report a posture the engine uses to
orient ITSELF (not the operator, who already sees the runtime in their harness).

The split this module implements, mirroring standing_situation.py's live-vs-committed discipline but inverting
its refresh semantics:

  - The BASELINE (execution.json) is a FROZEN operator judgment: which environments are qualified, and a
    snapshot of the instruction-floor hashes / engine release / repo slug at the moment of qualification. It
    is written only by record_qualification() and becomes true only when the operator merges it. It is NEVER
    auto-refreshed — the whole point is to notice drift away from the frozen snapshot.
  - The OBSERVATION is derived LIVE and cheap each boot: the runtime (injected — providers.detect), the repo
    origin slug, the engine release, and the sha256 of the current instruction-floor files. Nothing about the
    observation is committed.
  - compare() yields one of four postures:
      matched      — qualified for THIS repo, every snapshot component verifiable and equal. The environment's
                     own posture guidance loads.
      changed      — qualified for this repo, every component verifiable, but one drifted. Conservative posture
                     + a re-qualify alarm.
      unqualified  — no qualification for this repo (genesis, a baseline qualified for a DIFFERENT repo, or one
                     whose live repo can't be resolved — a shipped/foreign or unverifiable baseline reads as
                     not-ours rather than as spurious drift). Conservative posture, calm.
      unknown      — the baseline could not be read at all. Conservative posture, stated plainly.

Two safety rules the postures enforce, both learned at the plan gate:
  1. A qualified entry with ANY unverifiable component (a null recorded hash, a live floor file that can't be
     read now, or a live repo slug that can't be resolved) NEVER resolves to matched — an un-checkable
     component is not a pass (it would silently disable drift detection or the repo scoping). It degrades to
     the conservative posture.
  2. record_qualification REFUSES to stamp qualified when a component is unobservable, so a qualified baseline
     never carries a null snapshot field in the first place.

This module is self-contained: it imports only the standard library and the stdlib-only `moment` time seam,
reads only committed files under the repo root, and takes the runtime as an injected value. It performs no writes except through record_qualification(),
which writes execution.json atomically and NEVER commits — the operator's merge is the qualification act.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import sys

import moment  # the trailing-Z time seam; a stdlib-only leaf, so this module stays substantively self-contained

# The genesis baseline — the single source of truth for the shape's zero value. instantiator seeds this and
# read_baseline() synthesizes it when the file is absent, so the two never drift out of one definition.
_GENESIS_ENVIRONMENT = {
    "status": "unqualified",
    "as_of": None,
    "repo": None,
    "engine_release": None,
    "floors": {},
    "model_alias": None,
    "evidence": None,
}


def genesis_baseline() -> dict:
    """A fresh genesis record (both environments unqualified). A new dict every call — callers may mutate it."""
    return {
        "schema_version": 1,
        "environments": {
            "claude": dict(_GENESIS_ENVIRONMENT),
            "codex": dict(_GENESIS_ENVIRONMENT),
        },
    }


ENVIRONMENTS = ("claude", "codex")
_BASELINE_REL = os.path.join(".engine", "state", "execution.json")
_POLICY_REL = os.path.join(".engine", "policies", "model-routing.md")

# The safe fallback posture — always available in code, so the engine has careful guidance even when the
# policy file is missing or unparseable. The operator tunes the rendered posture text in model-routing.md;
# this constant is the floor beneath it, never a "future" placeholder.
_CONSERVATIVE_DEFAULT = [
    "Execution environment is not a verified qualified match here — run your full, careful ceremony.",
    "Make no model-dependent shortcuts; the running model's identity is not verified by the engine.",
]


class BaselineUnreadable(Exception):
    """Raised when execution.json exists but cannot be read or parsed. Never conflated with a MISSING file
    (which is benign — a repo that predates this feature has no baseline and sits, honestly, unqualified):
    a present-but-corrupt baseline is an unavailability, so the posture degrades to 'unknown' (conservative,
    stated plainly) rather than being read as genesis."""


class QualificationRefused(Exception):
    """Raised by record_qualification() when a component the qualified snapshot must freeze cannot be observed
    (the repo origin, the engine release, or an instruction-floor file). Refusing to stamp qualified with a
    null snapshot field is what keeps a qualified baseline from silently disabling its own drift detection."""


def _repo_root() -> str:
    """The repository root — the directory holding CLAUDE.md, AGENTS.md and .engine/ — three levels up from
    this file (.engine/tools/execution_environment.py)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sha256_file(path: str) -> str | None:
    """'sha256:' + hex over the raw bytes of a file, or None when it cannot be read (absence or an unreadable
    file both read as None — 'unverifiable', never a false hash)."""
    try:
        with open(path, "rb") as fh:
            return "sha256:" + hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def floor_paths(root: str) -> list[str]:
    """The instruction-floor files that steer the assistant, as repo-relative posix keys, in a stable order:
    CLAUDE.md and AGENTS.md at the root, then every .engine/conduct/*.md in sorted order. Only files that
    EXIST are listed — a floor present at qualification but gone now is absent here, which compare() reads as
    drift; the conduct set is walked live (not a fixed list) so an operator-added conduct code is tracked."""
    paths = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        if os.path.isfile(os.path.join(root, name)):
            paths.append(name)
    conduct_dir = os.path.join(root, ".engine", "conduct")
    if os.path.isdir(conduct_dir):
        for fn in sorted(os.listdir(conduct_dir)):
            if fn.endswith(".md") and os.path.isfile(os.path.join(conduct_dir, fn)):
                paths.append(f".engine/conduct/{fn}")
    return paths


def _engine_release(root: str) -> str | None:
    """The engine release string from .engine/engine.json, or None when it cannot be read."""
    try:
        with open(os.path.join(root, ".engine", "engine.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        release = data.get("engine_release")
        return release if isinstance(release, str) and release else None
    except (OSError, ValueError):
        return None


# Host-anchored (^...) so a look-alike host (notgithub.com/owner/repo) can never match as a substring — the
# same discipline boot's repo_slug uses, because a mis-parsed slug would scope a qualification to the wrong repo.
# IGNORECASE: host names are case-insensitive by spec (`GitHub.com` == `github.com`). ASCII keeps the fold
# ASCII-only, so a Unicode homograph (`gİthub.com`, U+0130 folds to `i`) cannot satisfy the host literal. The
# flags fold only the literal host, not the structural anchors, so no look-alike is newly accepted (StarshipSuperjam/engine-template#625).
_SLUG_RE = re.compile(r"^(?:(?:https?|ssh)://)?(?:[^@/]+@)?github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$",
                      re.IGNORECASE | re.ASCII)


def current_repo(root: str) -> str | None:
    """The repository's git-origin slug (owner/name), read locally, or None on any failure. Used to scope a
    qualification to the repo it was made for; parsed from the origin URL so it needs no network."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    m = _SLUG_RE.search(out.stdout.strip())
    return m.group(1) if m else None


def observe(*, provider: str, repo: str | None, root: str | None = None) -> dict:
    """The live environment, from injected runtime + repo and the committed files under root. No model identity
    (the running model is not reliably observable at session start and never drives drift); no writes."""
    root = root or _repo_root()
    floors = {rel: _sha256_file(os.path.join(root, *rel.split("/"))) for rel in floor_paths(root)}
    return {
        "runtime": provider,
        "repo": repo,
        "engine_release": _engine_release(root),
        "floors": floors,
    }


def read_baseline(root: str | None = None) -> dict:
    """The committed baseline. A MISSING file returns a fresh genesis record (benign — unqualified). A present
    file that will not parse raises BaselineUnreadable (unavailability, never read as genesis)."""
    root = root or _repo_root()
    path = os.path.join(root, _BASELINE_REL)
    if not os.path.exists(path):
        return genesis_baseline()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise BaselineUnreadable(f"execution.json could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise BaselineUnreadable("execution.json is not a JSON object")
    return data


def compare(observed: dict, baseline: dict) -> dict:
    """The posture of the observed environment against the committed baseline — one of matched / changed /
    unqualified. (unknown comes only from a BaselineUnreadable upstream, handled in derive.) Returns
    {runtime, posture, drift}: drift is the list of changed components, populated only for 'changed'."""
    env = observed["runtime"]
    entry = (baseline.get("environments") or {}).get(env) or {}
    if entry.get("status") != "qualified":
        return {"runtime": env, "posture": "unqualified", "drift": []}
    # A qualification counts only in the repo it was made for. If the live repo can't be resolved, the repo
    # component is unverifiable — and Rule 1 says an un-checkable component never resolves to matched (a
    # foreign baseline whose floor hashes happen to match must not slip through), so degrade to conservative.
    # A resolved-but-different repo is a foreign/home-shipped baseline: calm (unqualified), never drift.
    if entry.get("repo"):
        if observed.get("repo") is None or entry["repo"] != observed["repo"]:
            return {"runtime": env, "posture": "unqualified", "drift": []}

    drift: list[str] = []
    unverifiable = False

    base_release = entry.get("engine_release")
    live_release = observed.get("engine_release")
    if base_release is None or live_release is None:
        unverifiable = True
    elif base_release != live_release:
        drift.append("engine release")

    base_floors = entry.get("floors") or {}
    live_floors = observed.get("floors") or {}
    for key in sorted(set(base_floors) | set(live_floors)):
        in_base, in_live = key in base_floors, key in live_floors
        base_hash, live_hash = base_floors.get(key), live_floors.get(key)
        if (in_base and base_hash is None) or (in_live and live_hash is None):
            unverifiable = True            # a recorded-null or a live-unreadable floor — cannot be checked
        elif not in_base or not in_live:
            drift.append(key)              # a floor file appeared or was removed since qualification
        elif base_hash != live_hash:
            drift.append(key)              # a floor file's content changed

    # Rule 1: an un-checkable component never resolves to matched — degrade to the conservative posture.
    if unverifiable:
        return {"runtime": env, "posture": "unqualified", "drift": []}
    if drift:
        return {"runtime": env, "posture": "changed", "drift": drift}
    return {"runtime": env, "posture": "matched", "drift": []}


def _policy_posture_block(root: str, name: str) -> list[str] | None:
    """The operator-tunable posture lines from model-routing.md's fenced block marked
    `<!-- posture:<name> -->`, or None when the file or the block is absent or unparseable. Deliberately
    tolerant: any miss returns None so resolve_posture falls back to the safe constant — the parse never
    raises into boot."""
    try:
        with open(os.path.join(root, _POLICY_REL), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    start = text.find(f"<!-- posture:{name} -->")
    if start < 0:
        return None
    fence = text.find("```", start)
    if fence < 0:
        return None
    body_start = text.find("\n", fence)
    end = text.find("```", body_start + 1)
    if body_start < 0 or end < 0:
        return None
    lines = [ln.rstrip() for ln in text[body_start + 1:end].splitlines() if ln.strip()]
    return lines or None


def resolve_posture(posture: str, root: str | None = None) -> list[str]:
    """The self-instruction lines the engine loads for a posture. A 'matched' environment loads the
    operator-authored qualified posture (or the safe constant if the policy is absent); every other posture
    loads the conservative default. Never raises — the constant is always available."""
    root = root or _repo_root()
    block = "qualified" if posture == "matched" else "conservative-default"
    return _policy_posture_block(root, block) or list(_CONSERVATIVE_DEFAULT)


def derive(*, provider: str, repo: str | None = None, root: str | None = None) -> dict:
    """The total, boot-safe entry point: observe + read the baseline + compare + resolve the posture lines,
    never raising. A missing baseline yields 'unqualified'; an unreadable one yields 'unknown'; any other
    failure also yields 'unknown' (conservative). The tool owns the posture decision AND its text; boot only
    relays. The returned dict carries {runtime, posture, drift, lines}."""
    root = root or _repo_root()
    try:
        if repo is None:
            repo = current_repo(root)
        observed = observe(provider=provider, repo=repo, root=root)
        result = compare(observed, read_baseline(root))
    except BaselineUnreadable:
        result = {"runtime": provider, "posture": "unknown", "drift": []}
    except Exception:
        result = {"runtime": provider, "posture": "unknown", "drift": []}
    try:
        result["lines"] = resolve_posture(result["posture"], root)
    except Exception:
        result["lines"] = list(_CONSERVATIVE_DEFAULT)
    return result


def _utcnow() -> str:
    return moment.utc_now()


def _write_atomic(root: str, data: dict) -> None:
    """Write execution.json as pretty JSON + trailing newline, atomically (temp + os.replace) so a crash never
    leaves a half-written baseline."""
    path = os.path.join(root, _BASELINE_REL)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def record_qualification(env: str, *, root: str | None = None, repo: str | None = None,
                         model_alias: str | None = None, evidence: str | None = None,
                         now: str | None = None) -> dict:
    """THE sole writer of execution.json. Stamps environment `env` as qualified with the LIVE-observed repo,
    engine release, and instruction-floor hashes, plus the operator-supplied model_alias/evidence. REFUSES
    (QualificationRefused) when the repo, the engine release, or any floor file cannot be observed — a
    qualified snapshot must never carry a null component. Writes the file only; it NEVER commits, because the
    operator's merge of the resulting diff IS the qualification act. `repo` defaults to the live git origin."""
    root = root or _repo_root()
    if env not in ENVIRONMENTS:
        raise ValueError(f"unknown environment {env!r}; expected one of {ENVIRONMENTS}")
    observed = observe(provider=env, repo=repo if repo is not None else current_repo(root), root=root)
    if observed["repo"] is None:
        raise QualificationRefused("the repository's git origin could not be determined")
    if observed["engine_release"] is None:
        raise QualificationRefused("the engine release could not be read from .engine/engine.json")
    floors = observed["floors"]
    if not floors or any(v is None for v in floors.values()):
        raise QualificationRefused(
            "an instruction-floor file could not be read; refusing to stamp qualified with an unverifiable floor")

    baseline = read_baseline(root)          # raises BaselineUnreadable rather than clobber a corrupt file
    entry = {
        "status": "qualified",
        "as_of": now or _utcnow(),
        "repo": observed["repo"],
        "engine_release": observed["engine_release"],
        "floors": floors,
        "model_alias": model_alias,
        "evidence": evidence,
    }
    baseline.setdefault("environments", {})[env] = entry
    _write_atomic(root, baseline)
    return entry


def main(argv: list | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "derive":
        env = argv[1] if len(argv) > 1 else "claude"
        print(json.dumps(derive(provider=env), indent=2))
        return 0
    if argv and argv[0] == "record":
        if len(argv) < 2 or argv[1] not in ENVIRONMENTS:
            print(f"usage: execution_environment.py record <{'|'.join(ENVIRONMENTS)}> "
                  f"[--model-alias A] [--evidence URL]", file=sys.stderr)
            return 2
        env = argv[1]
        model_alias = evidence = None
        rest = argv[2:]
        for i, tok in enumerate(rest):
            if tok == "--model-alias" and i + 1 < len(rest):
                model_alias = rest[i + 1]
            elif tok == "--evidence" and i + 1 < len(rest):
                evidence = rest[i + 1]
        try:
            entry = record_qualification(env, model_alias=model_alias, evidence=evidence)
        except QualificationRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print(f"recorded {env} as qualified (uncommitted — review and merge the diff to qualify):")
        print(json.dumps(entry, indent=2))
        return 0
    print(f"usage: execution_environment.py derive [{'|'.join(ENVIRONMENTS)}] | "
          f"record <{'|'.join(ENVIRONMENTS)}> [--model-alias A] [--evidence URL]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
