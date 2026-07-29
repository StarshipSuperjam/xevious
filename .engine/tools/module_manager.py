#!/usr/bin/env python3
"""Module manager — the permanent provisioning primitive that adds and removes engine modules
over a repo's life.

The manager provides **remove** + the **group-scoped uv-sync derivation**; **add**
(install a module at the current release) + its shared **fetch/overlay** primitive + the **sync-groups**
fixer; the **engine updater** — `upgrade` (the whole-engine vX -> vY
version move) and the **migrations** machinery it runs; and CODEOWNERS rendering + de-bootstrap +
clean whole-engine removal.

`upgrade` is the engine updater: fetch the tagged release (reusing
`_fetch_release_tree`), overlay the engine CODE of the present packages (driven off the present set, so a
deselected module is never resurrected; operator config + gitignored data preserved; `_within_root` fails a
containment escape closed BEFORE any write), apply/reverse wiring deltas, re-render the CODEOWNERS ownership
wall for the new release's engine paths (the design's upgrade re-render — `_refresh_codeowners`), re-sync the
tool-runtime, run the packages' `migrations` in dependency order, run coherence, and land it as a reviewed PR.
A `data` migration is **backup-first**: it is refused (pre-flight, before any overlay) unless a backup seam
is available (memory owns the mechanism, live via `memory.snapshot_for_migration`), so the engine never
changes un-backed-up data. It DEGRADES to the current version on an unreachable release.
FIXTURE-DEMOED: the real release fetch, the `uv sync` re-sync, the git/PR open, and a real data migration
are exercised by fixtures, not by a live release in this template repo (which cuts no releases of itself)
— the named inductive gaps.

`add` is the mirror of `remove`: fetch the module's files from
the tagged release, copy its `provides` into their surface homes, copy in its manifest, apply its `wires`,
record it in the engine manifest at its version, re-derive the dependency-group selection, and re-run
coherence. It refuses — in plain language — an already-installed module, a fetch whose manifest id does
not match, or a declared dependency that is absent / outside its range (plan_add, reusing the coherence
range rule so it stays single-homed). The release FETCH is one injectable boundary (_fetch_release_tree —
the tag's source archive) so the tests and the demo run the REAL overlay/wire/coherence on a local tree
and never touch the network; the concrete fetch is the named inductive gap (the construction repo has no
releases to exercise it).

`remove` is **manifest-derived reversal**: reverse the
module's declared `wires` (via the wiring library), delete the engine-identified files it
`provides`, drop it from the engine manifest, re-derive the tool-runtime dependency groups, and
re-run coherence. It is **reverse-dependency-aware** — it refuses, in plain language naming the
dependents, to remove a module another present module still `depends` on — and it declines a
**required** module (the permanent spine; removing the whole engine is a separate clean-removal
step — remove_engine). It touches **no** control-plane ruleset: an ordinary remove changes only
what runs INSIDE the stable engine CI check, not the bound check name, so it needs no operator-
privileged step. A `permission` a module added is
**left in place** and disclosed — a bare permission is not engine-identifiable, so reversal errs
toward leaving it.

The **uv-sync derivation**: each dep-carrying module
declares a [dependency-group] in .engine/pyproject.toml NAMED BY ITS `id`; the sync selection is
those group names that match a PRESENT manifest id, under PEP 735 name normalization. It reuses the
id the manifest already carries — it adds no manifest field. `remove` re-derives and rewrites
`[tool.uv] default-groups` so the CI/local `uv sync` selection stays correct without hand-
maintenance (the seam the pyproject comment cedes to the module manager).

Read-only discovery is reused from module_coherence (one present-set reader, no drift):
discover_manifests / load_engine_manifest / provides_claims / check_coherence.

CLI:
  python tools/module_manager.py status              # present modules, reverse-deps, group sync
  python tools/module_manager.py sync-groups         # re-derive + rewrite [tool.uv] default-groups
  python tools/module_manager.py add <id> [--json]   # fetch + install a module at the current release
  python tools/module_manager.py plan-remove <id>    # read-only: refusal reasons / what remove would do
  python tools/module_manager.py remove <id> [--json]
  python tools/module_manager.py upgrade [ref]           # preview an update (checks only; changes nothing)
  python tools/module_manager.py upgrade [ref] --confirm [--json]  # apply the whole-engine update vX -> vY
  python tools/module_manager.py demo                # mutation-free fail-then-pass (remove + add + upgrade; fixtures)
"""
from __future__ import annotations
import contextlib
import glob
import io
import json
import os
import re
import shutil
import sys
import tempfile

try:
    import tomllib  # stdlib, Python >=3.11 (the runtime's requires-python)
except ModuleNotFoundError:  # pragma: no cover - the runtime guarantees >=3.11
    tomllib = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402  (finding.v1 + ROOT + read)
import wiring            # noqa: E402  (the wiring library: reverse_all, apply, the shared-file constants)
import module_coherence  # noqa: E402  (the present-set reader + the coherence legs)
import bootstrap         # noqa: E402  (ControlPlane.de_bootstrap — the clean-removal control-plane leg; one-way)


# ---- paths (computed from validate.ROOT at CALL time so a test/demo can redirect ROOT) --------

def _engine_manifest_path() -> str:
    return os.path.join(validate.ROOT, module_coherence.ENGINE_MANIFEST_REL)


def _pyproject_path() -> str:
    return os.path.join(validate.ROOT, ".engine", "pyproject.toml")


def _modules_dir(module_id: str) -> str:
    return os.path.join(validate.ROOT, ".engine", "modules", module_id)


def _write_json(path: str, data) -> None:
    """2-space-indent + trailing-newline JSON writer (mirrors wiring._write_json) so an
    operator's later diff of engine.json stays minimal."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


# ---- group-scoped uv-sync derivation (pure where it counts) -----------------------------------

def normalize_pep735(name: str) -> str:
    """PEP 735 dependency-group name normalization (the PEP 503 rule it references): lowercase, and
    collapse every run of [-_.] to a single '-'. A module id (^[a-z][a-z0-9-]*$) already normalizes
    to itself, so id<->group matching is exact for well-formed ids."""
    return re.sub(r"[-_.]+", "-", name or "").lower()


def declared_dependency_groups(pyproject_path: str | None = None) -> set:
    """The [dependency-groups] names declared in pyproject.toml, PEP 735-normalized. Read-only
    (tomllib — exactly its remit)."""
    path = pyproject_path or _pyproject_path()
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return {normalize_pep735(g) for g in (data.get("dependency-groups") or {})}


def committed_default_groups(pyproject_path: str | None = None) -> list:
    """The [tool.uv] default-groups currently committed in pyproject.toml, PEP 735-normalized."""
    path = pyproject_path or _pyproject_path()
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    groups = ((data.get("tool") or {}).get("uv") or {}).get("default-groups") or []
    return [normalize_pep735(g) for g in groups]


def derive_uv_groups(manifests: list | None = None, pyproject_path: str | None = None) -> list:
    """The uv-sync group selection: the [dependency-groups] names that match a PRESENT manifest id,
    under PEP 735 normalization, sorted. A module with no Python dependencies declares no group, so it
    simply isn't in the intersection and contributes nothing to the sync (installed-means-present).
    Adds no manifest field — it reuses the id the manifest already carries."""
    if manifests is None:
        manifests = module_coherence.discover_manifests()
    present = {normalize_pep735(m.get("id", "")) for _p, m in manifests}
    return sorted(present & declared_dependency_groups(pyproject_path))


# Anchored to a SINGLE line ([^\]\n]* never crosses a newline), so a multi-line default-groups array
# does not match -> the caller fails open (a plain note, no write) rather than silently collapsing the
# operator's formatting. The committed selection is single-line, so normal operation is unaffected.
_DEFAULT_GROUPS_RE = re.compile(r"(?m)^(?P<pre>[ \t]*default-groups[ \t]*=[ \t]*)\[[^\]\n]*\][ \t]*$")


def rewrite_default_groups_text(text: str, new_groups: list) -> tuple:
    """Pure minimal-diff rewrite: replace the single-line `default-groups = [...]` array literal with
    `new_groups`, preserving every other byte (the comment block, [project], [dependency-groups]).
    Returns (new_text, changed). Raises ValueError if the line is absent, appears more than once, or is
    written as a multi-line array (the regex matches only a single line) — the caller fails open and
    never blind-writes or silently reformats (the wiring-library mutator posture). No TOML writer
    library is used (none is a dependency); tomllib reads, this rewrites the one line."""
    matches = list(_DEFAULT_GROUPS_RE.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one tool-runtime dependency-group selection line to "
                         f"update, found {len(matches)}; left the configuration unchanged.")
    rendered = "[" + ", ".join(f'"{g}"' for g in new_groups) + "]"
    m = matches[0]
    new_text = text[:m.start()] + m.group("pre") + rendered + text[m.end():]
    return new_text, (new_text != text)


def _maybe_rewrite_default_groups(new_groups: list, pyproject_path: str | None = None) -> bool:
    path = pyproject_path or _pyproject_path()
    if not os.path.exists(path):
        return False
    text = validate.read(path)
    new_text, changed = rewrite_default_groups_text(text, new_groups)
    if changed:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    return changed


def sync_groups(pyproject_path: str | None = None) -> dict:
    """Re-derive the tool-runtime dependency-group selection from the present module set and rewrite
    `[tool.uv] default-groups` to match. This is the standalone fixer the `uv-group-drift` check points at
    — `add`/`remove` already keep the selection derived as a side effect of changing the set, so this is
    for the rare drift (a hand-edit, a botched merge). Returns {groups, changed}."""
    groups = derive_uv_groups(pyproject_path=pyproject_path)
    changed = _maybe_rewrite_default_groups(groups, pyproject_path)
    return {"groups": groups, "changed": changed}


# ---- remove (pure refusal policy + live mutation glue) ----------------------------------------

def plan_remove(module_id: str, manifests: list | None = None) -> dict:
    """READ-ONLY: would removing `module_id` be refused, and why — or what would remove do? Pure given
    the manifest list (defaults to the live present set), so every refusal path is fixture-testable
    without touching disk. Refusals (plain language), in order:
      - the module is not installed;
      - another present module still `depends` on it (named) — the spec's reverse-dependency refusal,
        checked first so it is the surfaced, actionable reason for a depended-on module;
      - it is `required` (the permanent spine — removing the whole engine is a separate step)."""
    if manifests is None:
        manifests = module_coherence.discover_manifests()
    by_id = {m.get("id"): (p, m) for p, m in manifests}
    if module_id not in by_id:
        return {"module_id": module_id, "refused": True,
                "reason": f"There is no module named '{module_id}' installed."}
    _path, target = by_id[module_id]
    dependents = sorted(m.get("id") for _p, m in manifests
                        if m.get("id") != module_id and module_id in (m.get("depends") or {}))
    if dependents:
        names = ", ".join(f"'{d}'" for d in dependents)
        many = len(dependents) > 1
        word, verb, those = ("modules", "need", "those") if many else ("module", "needs", "that one")
        return {"module_id": module_id, "refused": True,
                "reason": f"Can't remove '{module_id}' — the {names} {word} still {verb} it. Remove "
                          f"{those} first, or keep '{module_id}'."}
    if target.get("status") == "required":
        return {"module_id": module_id, "refused": True,
                "reason": f"'{module_id}' is a required part of the engine and can't be removed on its "
                          f"own — removing it would break the engine. (Removing the engine entirely is a "
                          f"separate step.)"}
    return {"module_id": module_id, "refused": False, "reason": None,
            "status": target.get("status"), "wires": list(target.get("wires") or [])}


def _permission_residue(target: dict) -> list:
    """The plain-language disclosure for every `permission` the module added that remove leaves behind —
    names the value, the file, the reason, and that it is safe to remove by hand (F6)."""
    out = []
    for d in (target.get("wires") or []):
        if isinstance(d, dict) and d.get("type") == "permission":
            v = d.get("value")
            out.append(f'The permission "{v}" in .claude/settings.json was left in place. The engine '
                       f"can't be sure it belongs only to this module and not also to your own setup, so "
                       f"it never removes a shared permission. If it was only for this module, you can "
                       f"remove it yourself.")
    return out


def remove(module_id: str) -> dict:
    """Remove one installed module (manifest-derived reversal). Returns a structured result; the CLI
    renders it in plain language. Refuses (no mutation) per plan_remove; otherwise reverses wiring,
    deletes the engine-identified files it owns + its manifest folder, drops it from engine.json,
    re-derives the tool-runtime dependency groups, and re-runs coherence."""
    manifests = module_coherence.discover_manifests()
    plan = plan_remove(module_id, manifests)
    if plan["refused"]:
        plan["applied"] = False
        return plan
    by_id = {m.get("id"): (p, m) for p, m in manifests}
    manifest_path, target = by_id[module_id]
    result = {"module_id": module_id, "refused": False, "applied": True,
              "reversed": [], "left_in_place": _permission_residue(target),
              "deleted": [], "groups_after": None, "findings": []}

    # (1) reverse the module's wiring (idempotent; permission no-op leaves honest residue)
    for f in wiring.reverse_all(target.get("wires") or []):
        result["reversed"].append(validate.fmt(f))

    # (2) delete the engine-identified files the module owns — sole-owner, at ANY path (the reversal law
    #     deletes the engine-identified files a module provides regardless of where they live; whole-engine
    #     remove_engine already does this, so a per-module remove that stopped at .engine/ left a removed
    #     module's .claude/ personas + skills orphaned on disk — #409). A module's `provides` are always
    #     wholly engine-owned files; anything shared with the operator (a settings.json hook, a permission)
    #     arrives via `wires` and is reversed in step (1), so the sole-owner guard is the only gate needed.
    target_claims = module_coherence.provides_claims([(manifest_path, target)])
    others = [(p, m) for p, m in manifests if m.get("id") != module_id]
    other_claims = module_coherence.provides_claims(others)
    for rel in sorted(target_claims):
        if rel not in other_claims:
            try:
                os.remove(os.path.join(validate.ROOT, rel))
                result["deleted"].append(rel)
            except OSError as exc:
                result["left_in_place"].append(f"Could not delete {rel} ({exc}); remove it by hand.")
    mod_dir = _modules_dir(module_id)
    if os.path.isdir(mod_dir):
        shutil.rmtree(mod_dir)
        result["deleted"].append(f".engine/modules/{module_id}/")

    # (3) drop the module from the engine manifest
    engine = module_coherence.load_engine_manifest()
    if engine and module_id in (engine.get("packages") or {}):
        del engine["packages"][module_id]
        _write_json(_engine_manifest_path(), engine)

    # (4) re-derive + rewrite the tool-runtime dependency-group selection for the remaining set
    try:
        new_groups = derive_uv_groups(manifests=others)
        result["groups_after"] = new_groups
        _maybe_rewrite_default_groups(new_groups)
    except (OSError, ValueError) as exc:
        result["left_in_place"].append(f"(Could not update the tool-runtime dependency groups: {exc})")
    except Exception as exc:  # tomllib decode / unexpected — fail open, never crash the removal
        result["left_in_place"].append(f"(Could not update the tool-runtime dependency groups: {exc})")

    # (5) confirm the remaining set is consistent
    result["findings"] = module_coherence.check_coherence()
    return result


# ---- fetch / overlay (the shared release machinery: add uses it here; the engine updater reuses
#      it in `upgrade`) ----------------------------------------------------------------------

class _NoPublishedRelease(RuntimeError):
    """The home is reachable but has NO release to resolve (the releases API returned 200 with no
    `tag_name`) — a genuine missing-release condition, distinct from a transport failure, so the caller
    refuses LOUDLY naming the home rather than degrading it as a network problem (#367)."""


def _fetch_release_tree(ref: str, dest_dir: str, repo: str | None = None,
                        token: str | None = None) -> str:
    """Download the engine's SOURCE archive at the tagged release `ref`, extract it under `dest_dir`, and
    return the path to the extracted tree root (the directory that contains `.engine/`). THIS IS THE
    NETWORK BOUNDARY — `add` (and the later updater) accept an injected local `release_tree`, so the tests
    and the demo never reach the network: they pass a local tree and exercise the REAL overlay/wire/
    coherence logic. The concrete download-and-extract below is therefore the named inductive gap a fixture
    cannot discharge (it never runs in the construction repo — there are no releases to fetch).

    Build-spec leaf (recorded): the artifact is the tag's GitHub SOURCE archive (the `tarball` endpoint),
    NOT a curated release asset — the engine ships from one tagged release as one tree, so the source archive carries every module's files and resolves their
    `provides` globs, and no separate asset-build pipeline exists. `ref` is a TAG, pinned, never a moving
    branch (the supply-chain control)."""
    import tarfile                # local: only the real network path needs these
    import urllib.request
    import boot                   # lazy: only the real fetch needs the repo slug + token
    slug = repo or boot.repo_slug()
    if not slug:
        raise RuntimeError("could not determine the engine repository to fetch the release from.")
    tok = token if token is not None else boot.gh_token()
    url = f"https://api.github.com/repos/{slug}/tarball/{ref}"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "engine-module-manager"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
        payload = resp.read()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
        tops = {n.split("/", 1)[0] for n in tf.getnames() if n}
        if len(tops) != 1:
            raise RuntimeError(f"unexpected release archive layout (top-level entries: {sorted(tops)[:3]}).")
        tf.extractall(dest_dir, filter="data")   # filter='data' blocks path traversal / device entries (py3.12)
    return os.path.join(dest_dir, tops.pop())


def _archive_tree(ref: str, dest_dir: str) -> str:
    """The OFFLINE sibling of `_fetch_release_tree`: materialize a local tag/ref's tree via `git archive`
    piped into `dest_dir` — no network, no token. The cut-time deployment gate uses it to project a genuine
    past release to its deployed shape and practice-upgrade it to the release candidate, asserting the
    structural gate stays green — the proof a synthetic fixture cannot make. Returns `dest_dir` ITSELF: `git
    archive` writes the tree with NO owner-repo-sha wrapper directory (unlike GitHub's tarball), so there is no
    top-level dir to descend into (arch-N2). Raises if the ref's tree object is absent (a shallow checkout with
    no tags — the gate blocks the cut on that)."""
    import subprocess   # local: only the offline projection needs it
    os.makedirs(dest_dir, exist_ok=True)
    proc = subprocess.run(["git", "-C", validate.ROOT, "archive", "--format=tar", ref],
                          capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git archive {ref} failed: {(proc.stderr or b'').decode('utf-8', 'replace')[:200]}")
    import tarfile   # local: only the offline belt needs it
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tf:
        tf.extractall(dest_dir, filter="data")
    return dest_dir


def _resolve_release_ref(ref: str | None, repo: str | None = None, token: str | None = None) -> str:
    """Resolve a target release ref to a CONCRETE tag. A pinned tag/sha passes through unchanged; None or
    "latest" is resolved to the repository's latest published release tag via the GitHub releases API — so
    the engine never fetches, runs, or RECORDS a moving ref (the tag-pin is the supply-chain control). THE NETWORK BOUNDARY for ref resolution — only the real
    upgrade path reaches it (the injected release_tree path passes a concrete ref), so it is part of the
    same named inductive gap as the release fetch (never run in the construction repo)."""
    if ref and ref != "latest":
        return ref
    import urllib.request, json as _json, boot   # local: only the real resolve needs these
    slug = repo or boot.repo_slug()
    if not slug:
        raise RuntimeError("could not determine the engine repository to resolve the latest release.")
    tok = token if token is not None else boot.gh_token()
    url = f"https://api.github.com/repos/{slug}/releases/latest"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "engine-module-manager"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
        tag = (_json.loads(resp.read()) or {}).get("tag_name")
    if not tag:
        raise _NoPublishedRelease("the engine repository has no published release to update to.")
    return tag


def _home_repository() -> str | None:
    """The engine's HOME repository slug (`owner/repo`) recorded in the manifest — the single source of
    truth for where engine updates are fetched from (issue #367). None when the manifest
    records no home (a repo generated before this coordinate shipped). The release-fetch callers pass this
    as `repo=` so they resolve the HOME, never the deployed repo's own `origin` (which `boot.repo_slug()`
    returns and which has no engine releases). On a None home the caller REFUSES with a plain remedy and
    never falls back to origin — the engine does not guess a home.

    Delegates to `module_coherence.home_repository()`, the single accessor (also read by the
    external-contribution submit flow), so the field name and the absent/blank/unreadable -> None contract
    live in one place rather than two that could drift."""
    return module_coherence.home_repository()


def _release_is_missing(exc: BaseException) -> bool:
    """Split a release-fetch failure into its two operator-distinct outcomes (three-state resolution). True → the home is recorded but UNRESOLVABLE: the release/repo does not exist (HTTP 404
    — release-less, renamed, or removed home) OR the home is reachable but has no published release at all
    (`_NoPublishedRelease`, a 200 with no tag) — both refused LOUDLY naming the home. False → a transport
    failure (offline / DNS / timeout / other status), which DEGRADES to the current version.
    urllib raises HTTPError (a URLError subclass) carrying a numeric `.code` for an HTTP status; a bare
    URLError or socket error carries none."""
    import urllib.error
    if isinstance(exc, _NoPublishedRelease):
        return True
    return isinstance(exc, urllib.error.HTTPError) and getattr(exc, "code", None) == 404


def _within_root(rel: str) -> bool:
    """True iff repo-relative `rel` resolves INSIDE validate.ROOT — the overlay containment guard (the
    topology wall: an overlay places only engine-namespaced paths). A `provides` pattern that is absolute or
    climbs out with `..` would otherwise escape the repo; this fails it closed."""
    root = os.path.realpath(validate.ROOT)
    dst = os.path.realpath(os.path.join(validate.ROOT, rel))
    return dst == root or dst.startswith(root + os.sep)


# ---- add (pure refusal policy + live overlay glue) --------------------------------------------

def plan_add(module_id: str, candidate: dict, manifests: list | None = None) -> dict:
    """READ-ONLY: would adding `module_id` (whose fetched manifest is `candidate`) be refused, and why?
    Pure given the candidate manifest + the present set, so every refusal path is fixture-testable. Refusals
    (plain language), in order:
      - the module is already installed;
      - the fetched files do not contain a module whose id matches (a wrong/corrupt fetch);
      - a declared dependency is absent from the present set, or the present version is outside the declared
        range — surfaced by reusing validate.coherence_findings over the PROSPECTIVE set (present + candidate)
        and diffing against the present set, so the range rule stays single-homed with the coherence leg."""
    if manifests is None:
        manifests = module_coherence.discover_manifests()
    by_id = {m.get("id"): m for _p, m in manifests}
    if module_id in by_id:
        return {"module_id": module_id, "refused": True,
                "reason": f"'{module_id}' is already installed."}
    if not isinstance(candidate, dict) or candidate.get("id") != module_id:
        got = candidate.get("id") if isinstance(candidate, dict) else None
        return {"module_id": module_id, "refused": True,
                "reason": f"The fetched files don't contain a module named '{module_id}' "
                          f"(found {got!r} instead); nothing was changed."}
    present = [m for _p, m in manifests]
    base = validate.coherence_findings(present, "hard", "")
    after = validate.coherence_findings(present + [candidate], "hard", "")
    new = [f for f in after if f not in base]
    if new:
        reasons = " ".join(f.get("message", "").strip() for f in new)
        return {"module_id": module_id, "refused": True,
                "reason": f"Can't add '{module_id}' yet — {reasons}"}
    return {"module_id": module_id, "refused": False, "reason": None,
            "version": candidate.get("version"), "wires": list(candidate.get("wires") or [])}


def add(module_id: str, release_tree: str | None = None, ref: str | None = None) -> dict:
    """Add (install) one module at the current engine release: fetch the module's
    files from the tagged release, copy its `provides` into their surface homes, copy in its manifest, apply
    its `wires`, record it in the engine manifest, re-derive the tool-runtime dependency-group selection,
    and re-run coherence. Re-adding a module deselected at first run is this same path (its files were
    deleted). `release_tree` injects a local extracted release tree (the fetch boundary) for tests/the demo;
    None fetches the current release for real. Returns a structured result; the CLI renders it in plain
    language. Refuses (no mutation) per plan_add."""
    if not re.fullmatch(r"[a-z][a-z0-9-]*", module_id or ""):
        # bound a CLI-supplied id before it is ever path-joined (defense in depth; the manifest schema's
        # id pattern governs committed manifests, not this argument)
        return {"module_id": module_id, "refused": True, "applied": False,
                "reason": f"'{module_id}' is not a valid module id (lower-case letters, digits and hyphens, "
                          f"starting with a letter); nothing was changed."}
    manifests = module_coherence.discover_manifests()
    if module_id in {m.get("id") for _p, m in manifests}:
        return {"module_id": module_id, "refused": True, "applied": False,
                "reason": f"'{module_id}' is already installed."}
    result = {"module_id": module_id, "refused": False, "applied": False, "version": None,
              "copied": [], "applied_wires": [], "groups_after": None, "notes": [], "findings": []}
    tmp = None
    try:
        if release_tree is None:
            engine = module_coherence.load_engine_manifest()
            target_ref = ref or (engine or {}).get("engine_release")
            if not target_ref:
                return {"module_id": module_id, "refused": True, "applied": False,
                        "reason": "could not determine which engine release to fetch the module from."}
            # A module's files come from the engine's HOME release too, never this repo's own origin
            # (#367). Absent home -> refuse with a remedy; never fall back to origin.
            home = _home_repository()
            if not home:
                return {"module_id": module_id, "refused": True, "applied": False,
                        "reason": f"This engine has no update home recorded, so it can't fetch '{module_id}'. "
                                  f"Tell me the repository your engine updates from (for example your-org/your-engine) and I'll "
                                  f"record it, then you can add the module again. Nothing was changed."}
            tmp = tempfile.mkdtemp(prefix="engine-add-")
            try:
                release_tree = _fetch_release_tree(target_ref, tmp, repo=home)
            except Exception as exc:
                if _release_is_missing(exc):   # recorded home, but no such release/repo -> refuse, NAME it
                    return {"module_id": module_id, "refused": True, "applied": False,
                            "reason": f"Couldn't find release '{target_ref}' at your engine's update home, "
                                      f"{home}, to add '{module_id}' — that home may have no such release, or "
                                      f"it may have been renamed or removed. Nothing was changed. If the home "
                                      f"is wrong, update the recorded home and try again."}
                return {"module_id": module_id, "refused": True, "applied": False,   # transport -> degrade
                        "reason": f"Couldn't reach your engine's update home, {home}, to add '{module_id}' — "
                                  f"the network may be down, or the home may not be reachable right now. "
                                  f"Nothing was changed. ({exc})"}
        candidate_path = os.path.join(release_tree, ".engine", "modules", module_id, "manifest.json")
        if not os.path.isfile(candidate_path):
            return {"module_id": module_id, "refused": True, "applied": False,
                    "reason": f"The engine release does not contain a module named '{module_id}'."}
        candidate = validate.load_json(candidate_path)
        plan = plan_add(module_id, candidate, manifests)
        if plan["refused"]:
            plan["applied"] = False
            return plan

        # (1) collect the module's provided files from the release tree (same relpaths). The `provides`
        #     contract scopes a module's globs to its own files (the ownership leg enforces non-overlap).
        #     CONTAINMENT GUARD (the topology wall): every destination must resolve INSIDE the engine tree —
        #     an absolute or `..`-climbing pattern is refused before anything is copied, never written
        #     outside ROOT (the spec's "overlay only engine-namespaced paths" law, enforced not assumed).
        result["version"] = candidate.get("version")
        to_copy = []
        for _group, patterns in (candidate.get("provides") or {}).items():
            for pattern in patterns:
                for src in sorted(glob.glob(os.path.join(release_tree, pattern), recursive=True)):
                    if os.path.isfile(src):
                        to_copy.append((src, os.path.relpath(src, release_tree).replace(os.sep, "/")))
        escapes = [rel for _src, rel in to_copy if not _within_root(rel)]
        if escapes:
            shown = ", ".join(escapes[:3]) + ("…" if len(escapes) > 3 else "")
            return {"module_id": module_id, "refused": True, "applied": False,
                    "reason": f"Refused to add '{module_id}': it tried to place files outside the engine "
                              f"({shown}). Nothing was changed."}
        for src, rel in to_copy:
            dst = os.path.join(validate.ROOT, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            result["copied"].append(rel)
        # (2) copy in the module's manifest
        dst_manifest = os.path.join(_modules_dir(module_id), "manifest.json")
        os.makedirs(os.path.dirname(dst_manifest), exist_ok=True)
        shutil.copyfile(candidate_path, dst_manifest)
        result["copied"].append(f".engine/modules/{module_id}/manifest.json")
        # (3) apply the module's wiring (the real appliers)
        for f in wiring.apply_all(candidate.get("wires") or []):
            result["applied_wires"].append(validate.fmt(f))
        # (4) record it in the engine manifest at its version
        engine = module_coherence.load_engine_manifest() or {"packages": {}}
        engine.setdefault("packages", {})[module_id] = candidate.get("version")
        _write_json(_engine_manifest_path(), engine)
        # (5) re-derive + rewrite the dependency-group selection now that module_id is present. (The
        #     module's [dependency-groups] declaration + its uv.lock entries ship with the engine, so add
        #     flips only the SELECTION; an engine upgrade is what introduces a wholly new declaration.)
        try:
            new_groups = derive_uv_groups()
            result["groups_after"] = new_groups
            _maybe_rewrite_default_groups(new_groups)
        except Exception as exc:  # OSError / ValueError / tomllib decode — fail open, never crash the add
            result["notes"].append(f"(Could not update the tool-runtime dependency groups: {exc})")
        # (6) confirm the resulting set is consistent
        result["applied"] = True
        result["findings"] = module_coherence.check_coherence()
        return result
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


# ---- engine upgrade + migrations (the engine updater). FIXTURE-DEMOED — four boundaries never run in the construction
#      repo: (1) the real release FETCH (no releases), (2) the `uv sync` RE-SYNC from the overlaid lock,
#      (3) the git/PR OPEN, (4) a real DATA migration + its backup (the live `memory.snapshot_for_migration`
#      seam). Each is
#      injectable/skipped so tests + the demo run the REAL overlay / runner / coherence logic; "works on
#      the fixture ⇒ works for a real adopter" is the inductive step the fixture cannot discharge. ------

_UNSET = object()   # sentinel: "no GitHub boundary passed (resolve close._github)" vs "offline (None)"

# Root CLAUDE.md is keyed-MERGED on upgrade, not wholesale-overlaid: it carries the engine's `floor` as a
# comment-fenced section so a brownfield adopter's own CLAUDE.md co-exists with the engine's entries rather
# than being seized (the #234/#272 coexistence obligation). Since #323 the floor is sourced from the `floor`
# fence in the release's committed root CLAUDE.md/AGENTS.md (the promoted adopter floor) by `_merge_claude_floor`
# / `_read_release_floor`, not a whole-file `.deployed.md`.
_ROOT_CLAUDE_REL = "CLAUDE.md"
_ROOT_AGENTS_REL = "AGENTS.md"                     # the Codex floor — same keyed-merge/block-reverse posture
_FLOOR_FENCE = "floor"
_GITIGNORE_REL = ".gitignore"           # the foundation-ignores fence lives here (#409) — a shared keyed
#                                         file, so it is block-reversed like CODEOWNERS/CLAUDE.md, never
#                                         overlay-replaced (FOUNDATION_CODE) or wholesale-deleted (remove_engine)

# Engine CODE owned by no module's `provides` but replaced wholesale on upgrade.
# DERIVED from module_coherence.FOUNDATION_INFRA (the single source of the foundation-artifact set) minus
# the members the overlay must NOT fetch-and-replace: the engine manifest (engine.json — operator config
# whose package versions upgrade bumps in place, identity preserved); CODEOWNERS (re-rendered locally from
# the post-overlay engine path set by upgrade step (2d) / `_refresh_codeowners`, never fetched from a
# release — a release's block would carry the wrong owner + paths); and root CLAUDE.md (keyed-merged by
# `_merge_claude_floor`, which reads only the `floor` fence out of the release's root CLAUDE.md so operator
# content outside the fence is preserved and the release's own file never overlays an adopter's whole floor);
# and root `.gitignore`
# (the foundation-ignores fence is re-asserted locally by apply_foundation_ignores on upgrade — step (2f)
# below — never fetched, since a release's file would clobber the adopter's own ignore lines + module
# fences). Gitignored data and the deployment's per-instance eADR stream (`.engine/contracts/instance/`, off
# core's non-recursive `.engine/contracts/*.md` canon glob) are in no
# `provides`/FOUNDATION_CODE, so the overlay leaves them untouched (config + data preserved). A member may be
# a glob (the issue templates); the overlay loop below expands it against the release tree, so the issue
# templates are now refreshed on update (they were silently omitted before — single-homing closed that gap;
# forward-only).
# The five FOUNDATION_INFRA members the overlay must NOT fetch-and-replace — the engine manifest (identity,
# bumped in place), CODEOWNERS + the two root floors + root .gitignore (re-rendered / keyed-merged locally).
# Single-homed here so FOUNDATION_CODE (below) and the reconcile keep-set / carve-outs (issue #599) cannot drift.
_FOUNDATION_KEYED = (module_coherence.ENGINE_MANIFEST_REL, ".github/CODEOWNERS", _ROOT_CLAUDE_REL,
                     _ROOT_AGENTS_REL, _GITIGNORE_REL)
FOUNDATION_CODE = tuple(p for p in module_coherence.FOUNDATION_INFRA if p not in _FOUNDATION_KEYED)


class _UpgradeRefused(Exception):
    """A clean upgrade refusal carrying a plain-language reason — caught by upgrade() so a refusal returns
    a structured result (no traceback), with nothing applied."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---- migrations: the backup seam, the loader, the runner, the version-stamp check -----

def _resolve_backup_seam(backup):
    """The pre-migration backup seam a `data` migration uses. An injected callable (tests/demo) wins;
    otherwise MEMORY's snapshot mechanism if memory-substrate is installed AND a backup destination is
    configured, else None. The seam is a callable `seam(store, engine_version) -> a truthy snapshot
    handle`; **None means NO backup is available**, so the no-backup guard refuses every data migration
    (degrade loud, never silently mutate un-backed-up data). "Available" means a backup can actually be
    taken (mechanism installed + a vault set up) — NOT merely that the callable exists — so a repo with
    memory installed but no vault refuses cleanly instead of running a migration that fails mid-snapshot.
    Live via `memory.snapshot_for_migration` (+ `memory.migration_backup_available`): memory owns the
    mechanism AND the restore contract and may not be widened here. The handle's concrete shape is memory's
    leaf (the close._trigger_ambient_capture precedent). The snapshot lands as a distinct, retained git tag the
    routine backup never overwrites — memory's point-in-time pre-migration snapshot (resolving #287);
    the restore command targets that tag. This consumer widens nothing of that mechanism."""
    if backup is not None:
        return backup
    try:
        import memory  # noqa: F401 — ImportError (memory not installed) -> no seam
        fn = getattr(memory, "snapshot_for_migration", None)
        if not callable(fn):
            return None
        available = getattr(memory, "migration_backup_available", None)
        if callable(available) and not available():
            return None        # mechanism installed but no backup destination configured -> no backup available
        return fn
    except Exception:  # noqa: BLE001 — any failure obtaining the seam -> treat as "no backup available"
        return None


def _load_migration(module_dir: str, run_rel: str):
    """Load the migration at <module_dir>/<run_rel> and return its migrate(context) callable. Loaded under
    a UNIQUE synthetic module name (so two modules' migration files never collide in sys.modules) via the
    importlib spec loader — no sys.path mutation."""
    import importlib.util   # local: only the migration path needs it
    path = os.path.join(module_dir, run_rel)
    if not os.path.isfile(path):
        raise RuntimeError(f"migration file '{run_rel}' is missing")
    uniq = re.sub(r"[^a-z0-9]+", "_", os.path.relpath(path, validate.ROOT).lower())
    spec = importlib.util.spec_from_file_location(f"engine_migration_{uniq}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "migrate", None)
    if not callable(fn):
        raise RuntimeError(f"migration '{run_rel}' does not define a migrate(context) function")
    return fn


def _ver_key(v):
    """A LENGTH-NORMALIZED version tuple for RANGE-boundary comparison, so a two-part key ('0.4') and its
    three-part form ('0.4.0') compare EQUAL rather than '0.4' sorting BELOW '0.4.0' as a tuple prefix. Migration
    and retired-capability keys are conventionally MAJOR.MINOR.PATCH but that is NOT schema-enforced (the schemas
    constrain the entry VALUE, not the key), so a hand-authored two-part key at a range boundary is possible and
    must not fall on the wrong side. Padding is a no-op for conventional three-part (and any 4+-part, e.g. a
    digit-bearing pre-release suffix) tuples, so behaviour is unchanged for every real version in the tree.

    THE SINGLE NORMALIZER for version-key range comparison: called directly by select_migrations and
    select_retired_capabilities, and reached by release_cut._norm_ver (which delegates here) for BOTH
    release-cut accumulation guards. So editing this moves a release-refusal safety guard's normalization too —
    the mirrored regression tests (test_module_manager selector-boundary tests + test_release_cut
    test_rekeyed_migration_is_not_a_false_drop) are the backstop."""
    t = validate._ver_tuple(v)
    return (t + (0,) * (3 - len(t))) if len(t) < 3 else t


def select_migrations(from_versions: dict, target_versions: dict, manifests: list) -> list:
    """PURE: the migration entries an upgrade must run, in execution order. For each present module pick
    the `migrations` keys strictly ABOVE its from-version and AT-OR-BELOW its target-version; order modules
    by dependency (validate.topological_order) and, within a module, by ASCENDING version using
    validate._ver_tuple (NEVER string order — '0.10.0' must sort AFTER '0.9.0'). The range-boundary comparison
    uses the LENGTH-NORMALIZED key (_ver_key), matching the release-cut accumulation guard, so a two-part key
    ('0.4') never falls on the wrong side of its three-part boundary ('0.4.0'). `manifests` is a list of
    manifest dicts; `from_versions`/`target_versions` are {module_id: version}. Returns a list of
    {module_id, version, description, run, kind} — fixture-testable with no disk/network.

    ACCUMULATION CONTRACT (enforced at release-cut by release_cut._migration_accumulation_violations): because
    selection is by RANGE, a key removed from a manifest is simply never iterated for an engine sitting below it
    — silently skipped, never run. So a shipped migration key must NEVER be dropped; to retire a transform, keep
    its key and make its `run` a no-op. A release that drops a previously-shipped key is refused before the cut."""
    out = []
    for m in validate.topological_order(list(manifests)):
        mid = m.get("id")
        frm = _ver_key(from_versions.get(mid, "0"))
        tgt = _ver_key(target_versions.get(mid, from_versions.get(mid, "0")))
        for ver in sorted((m.get("migrations") or {}), key=validate._ver_tuple):
            if frm < _ver_key(ver) <= tgt:
                e = (m.get("migrations") or {})[ver] or {}
                out.append({"module_id": mid, "version": ver, "description": e.get("description"),
                            "run": e.get("run"), "kind": e.get("kind")})
    return out


def select_retired_capabilities(from_versions: dict, target_versions: dict, manifests: list) -> list:
    """PURE: the capability-retirement ANNOUNCEMENTS an upgrade must show, in module/version order. The exact
    RANGE selection as select_migrations — for each present module, every `retired_capabilities` key strictly
    ABOVE its from-version and AT-OR-BELOW its target-version, modules ordered by dependency and, within a
    module, by ASCENDING version (validate._ver_tuple, NEVER string order); the range-boundary comparison uses
    the LENGTH-NORMALIZED key (_ver_key), as select_migrations and the release-cut guard do. Returns a list of
    {module_id, version, description} — announcement-only: no `run`, no `kind`, NOTHING executes, so (unlike
    run_migrations) it can never refuse and needs no backup seam. Fixture-testable with no disk/network.

    ACCUMULATION CONTRACT (enforced at release-cut by release_cut._retired_capabilities_accumulation_violations):
    selection is by RANGE, so a key removed from a manifest is simply never iterated for an engine sitting below
    it — the notice silently vanishes for the very lagging upgrader it exists to reach. So a shipped
    retired-capabilities key must NEVER be dropped; and unlike a migration there is no no-op form to retire it
    to — the key is append-only for the life of the module."""
    out = []
    for m in validate.topological_order(list(manifests)):
        mid = m.get("id")
        frm = _ver_key(from_versions.get(mid, "0"))
        tgt = _ver_key(target_versions.get(mid, from_versions.get(mid, "0")))
        for ver in sorted((m.get("retired_capabilities") or {}), key=validate._ver_tuple):
            if frm < _ver_key(ver) <= tgt:
                e = (m.get("retired_capabilities") or {})[ver] or {}
                out.append({"module_id": mid, "version": ver, "description": e.get("description")})
    return out


def _bind_migration_id(seam, module_id: str, version: str, reversibility_floor: bool = False, sink=None):
    """Bind the migration's identity into the backup seam so memory names the pre-migration snapshot collision-free
    by it (the retained-tag mechanism). The migration calls `context['backup'](store, engine_version)`
    exactly as before — the migration id rides along, so migration authors need not know about it and module_manager
    stays a pure consumer that knows nothing of the snapshot's tag mechanism. `reversibility_floor` (True only for the
    first data migration of the upgrade — #303) likewise rides along so memory records THAT snapshot as the undo floor;
    module_manager passes only this boolean and never learns the snapshot's tag. Passing the extra kwargs is forward-
    compatible: a seam that ignores them still works (memory falls back to engine-version + generation).

    `sink`, when a list, receives each returned snapshot handle so the caller can read a plain property of the
    snapshot it needs to relay to the operator — whether the retained pre-update copy could be locked against
    hand-deletion — without module_manager learning anything about the snapshot's tag mechanism."""
    if seam is None:
        return None
    migration_id = f"{module_id}@{version}"

    def _seam(store, engine_version):
        handle = seam(store, engine_version, migration_id=migration_id, reversibility_floor=reversibility_floor)
        if sink is not None and isinstance(handle, dict):
            sink.append(handle)
        return handle
    return _seam


def run_migrations(selected: list, from_versions: dict, engine_version: str,
                   module_dir=None, backup=None) -> dict:
    """Run the SELECTED migrations (from select_migrations) in order. `module_dir(module_id)` returns that
    module's directory so `run` resolves (defaults to the live layout). `engine_version` is handed to each
    migration (a data migration stamps its snapshot with it). `backup` injects the seam (tests/demo); None
    resolves the real one. Returns {ran:[...], refused:[...]}.

    `config` migration -> runs directly (a reverted upgrade restores a committed file on its own).
    `data` migration  -> the NO-BACKUP GUARD: with no backup available it is REFUSED (degrade loud,
    nothing run); else the seam is handed to the migration in `context` so it snapshots its OWN store
    BEFORE mutating + stamps it with `engine_version` (backup-first reversibility). The guard is
    belt-and-suspenders with upgrade()'s pre-flight (which refuses the whole upgrade before overlaying if a
    data migration has no backup available), so run_migrations is also safe to call on its own. A data
    migration whose backup FAILS at run time (the seam returns a falsy handle, so its backup-first assert
    fires) is also caught and recorded as a refusal — never a raw traceback; upgrade() then declines to
    open the change for review (see the refused-result check there)."""
    if module_dir is None:
        module_dir = _modules_dir
    seam = _resolve_backup_seam(backup)
    result = {"ran": [], "refused": []}
    handles: list = []                                   # each data migration's snapshot handle, so after the run we
    #                                                      can relay one plain property (could the retained pre-update
    #                                                      copy be locked?) without learning the snapshot's tag mechanism.
    floor_taken = False                                  # #303: the FIRST data migration of this upgrade is the
    #                                                      reversibility floor — one run_migrations call == one upgrade
    #                                                      == one reversibility unit (true for the sole caller upgrade()).
    for item in selected:
        mid, ver, kind = item["module_id"], item["version"], item.get("kind")
        if kind == "data" and seam is None:
            result["refused"].append(
                f"Did not update stored data for '{mid}' to {ver}: no data backup is set up yet, and the "
                f"engine never changes stored data it can't first back up. Nothing was changed. Ask me to "
                f"set up a backup, then update again.")
            continue
        is_floor = kind == "data" and not floor_taken
        if kind == "data":
            floor_taken = True
        ctx = {"module_id": mid, "from_version": from_versions.get(mid), "to_version": ver,
               "engine_version": engine_version, "kind": kind,
               "backup": _bind_migration_id(seam, mid, ver, reversibility_floor=is_floor, sink=handles) if kind == "data" else None}
        if kind == "data":
            # Raise the in-flight-migration marker for the snapshot+mutate window so a concurrent compaction
            # refuses within it (the compaction↔migration ordering law). Lazy import (the
            # memory←boot back-edge, as the backup seam already is). FAIL CLOSED: if the marker can't be raised
            # (another memory write holds the single-writer lock), REFUSE the migration rather than run it
            # marker-less — an unguarded snapshot+mutate is exactly the interleave the marker prevents.
            from memory import capture as _capture, ledger as _ledger   # lazy back-edge
            _mig_dir = _ledger.ledger_dir()
            if not _capture.open_migration_window(_mig_dir):
                result["refused"].append(
                    f"Did not update stored data for '{mid}' to {ver}: another memory task was busy, so the "
                    f"update couldn't start safely. Nothing was changed. Try again in a moment.")
                continue
            # A data migration snapshots BEFORE mutating; if that backup can't be taken at run time (the seam
            # returns a falsy handle, so the migration's own backup-first assert fires) it must DEGRADE LOUD —
            # a clean refusal, never a raw traceback to the operator. The pre-flight + readiness probe catch
            # the common "no vault configured" case earlier; this catches a backup that fails at the moment of
            # the snapshot (a vault that went unreachable/public between pre-flight and run). The `finally` lowers
            # the marker whether the migration finished or refused.
            try:
                _load_migration(module_dir(mid), item["run"])(ctx)
            except Exception:  # noqa: BLE001 — backup-first means the failure is before mutating; degrade loud
                result["refused"].append(
                    f"Did not finish updating stored data for '{mid}' to {ver}: its backup could not be "
                    f"completed, so the update was stopped. Ask me to set up or check your backup, then "
                    f"update again.")
                continue
            finally:
                _capture.close_migration_window(_mig_dir)
        else:
            _load_migration(module_dir(mid), item["run"])(ctx)
        result["ran"].append(f"{mid} -> {ver} ({kind})")
    # A retained pre-update copy that could not be locked can still be deleted by hand; record that (once) so the
    # operator can be told to keep it. True only when a snapshot reported plainly that it could not be locked.
    result["backup_unprotected"] = any(isinstance(h, dict) and h.get("hardened") is False for h in handles)
    return result


def stamp_mismatch_finding(store_label: str, stamped_version: str, running_version: str,
                           restore_command: str):
    """PURE: the post-revert data-integrity check a data migration owns. After an upgrade pull request is
    reverted, the engine CODE returns to the older version, but a data migration that already reshaped a
    gitignored store is NOT reverted with it (the store is gitignored, outside the pull request). Each data
    migration stamps its snapshot with the engine-code version it ran at; if the running engine code is now
    OLDER than that stamp, the store is ahead of the code. Returns a hard finding.v1 carrying the plain-handle
    restore action, or None when there is no mismatch (running >= stamped). DETECTION is the migration system's
    logic (memory's `restore_vault.detect_migration_revert` is the live caller); SURFACING is boot's existing
    read-only open-findings path (boot needs no change). `restore_command` is a plain-handle action phrase, never
    a raw tag/ref — the finding message is operator-facing (boot.open_findings renders it)."""
    if validate._ver_tuple(running_version) >= validate._ver_tuple(stamped_version):
        return None
    return validate.finding(
        "hard",
        f"Your saved memory was changed by an engine update that isn't in place, so right now your "
        f"memory and the engine don't match. To line them up again, {restore_command}.")


def surface_stamp_mismatch(store_label: str, stamped_version: str, running_version: str,
                           restore_command: str, now: str, github=_UNSET):
    """Surface a detected version-stamp mismatch as ONE tracked engine finding via
    telemetry.promote_finding (NO auto-resolve — never closes other open Issues), which boot then renders
    through its read-only open-findings path. Reuses close's GitHub boundary + finding-record shape.
    Returns the Issue number, or None when there is no mismatch / GitHub is unreachable (the in-session
    surfacing + the merge wall remain). This is a READ-ONLY check — it calls promote_finding, NEVER runs
    migrate() (migration is never triggered at boot). Its live caller is memory's `restore_vault.detect_migration_revert`,
    which runs the offline code-older-than-data check and, when online, calls this to open the durable tracked Issue."""
    f = stamp_mismatch_finding(store_label, stamped_version, running_version, restore_command)
    if f is None:
        return None
    import hashlib            # lazy: this rare path keeps module_manager's common imports lean
    import close              # close owns the GitHub boundary + the finding-record shape (reuse, no copy)
    import telemetry
    gh = close._github() if github is _UNSET else github
    if gh is None:            # offline -> surfaced-in-session-not-tracked; the merge wall is the backstop
        return None
    digest = hashlib.sha1((f.get("message") or "").encode("utf-8")).hexdigest()[:12]
    record = {"source_id": f"migration/version-stamp/{digest}", "severity": telemetry.TRUST_CRITICAL,
              "message": f.get("message"), "location": f.get("location"),
              "first_seen": now, "last_seen": now}
    return telemetry.promote_finding(gh, record, now)


# ---- upgrade: overlay (off the PRESENT set) + wiring deltas + re-sync + migrations + coherence + PR ----

def _overlay_copy_map(tree_root: str, manifests_by_id: dict) -> dict:
    """{repo-relative-path -> source-abspath} the engine overlay copies, enumerated against `tree_root`:
    each present module's `provides` files + its own `manifest.json`, plus FOUNDATION_CODE (files the
    release ships wholesale). THE SINGLE SOURCE of the overlay membership: `_overlay_engine_code` copies
    this map from a downloaded RELEASE tree, and `overlay_replace_paths()` reads its keys against the LIVE
    tree — so the operator-facing "this file gets overwritten on the next update" disclosure cannot drift
    from what the update actually overwrites. A manifest also matched by a `provides` glob dedups to one
    entry (dict key). Module manifests are in no module's `provides`, so they are added explicitly here —
    the overlay overwrites them wholesale, and the disclosure must warn on them too."""
    to_copy: dict = {}
    for mid, man in manifests_by_id.items():
        for _group, patterns in (man.get("provides") or {}).items():
            for pattern in patterns:
                for src in glob.glob(os.path.join(tree_root, pattern), recursive=True):
                    if os.path.isfile(src):
                        to_copy[os.path.relpath(src, tree_root).replace(os.sep, "/")] = src
        to_copy[f".engine/modules/{mid}/manifest.json"] = os.path.join(
            tree_root, ".engine", "modules", mid, "manifest.json")
    for member in FOUNDATION_CODE:
        # Glob-expand each foundation member against the tree (a member may be a glob, e.g.
        # .github/ISSUE_TEMPLATE/*.md; glob.glob on a literal path returns it iff it exists). A literal
        # os.path.isfile on a glob string would silently drop the issue templates.
        for src in glob.glob(os.path.join(tree_root, member), recursive=True):
            if os.path.isfile(src):
                to_copy[os.path.relpath(src, tree_root).replace(os.sep, "/")] = src
    return to_copy


def overlay_replace_paths() -> set:
    """The repo-relative engine files the NEXT engine update would OVERWRITE, expanded against the LIVE
    tree — exactly the membership `_overlay_engine_code` copies (present modules' `provides` files + their
    manifests + FOUNDATION_CODE), via the shared `_overlay_copy_map`. This is what the merge-time
    upgrade-overwrite disclosure (`overlay_disclosure.py`) warns an operator about.

    An APPROXIMATION, named honestly: the true overwrite source is a future RELEASE tree, which this live
    tree only stands in for — a path this tree has that a future release drops (or vice-versa) is inherent
    slack, not a guarantee. What IS guaranteed is that it cannot drift from the overlay's own enumeration.

    DISTINCT from `module_coherence.engine_owned_paths()` — do NOT dedupe into it: that set unions the
    FULL `FOUNDATION_INFRA` (including the keyed-merge/CODEOWNERS carve-outs the overlay PRESERVES) and
    omits module manifests, so reusing it would both cry wolf on preserved files and miss the manifests."""
    manifests = {m.get("id"): m for _rel, m in module_coherence.discover_manifests()}
    return set(_overlay_copy_map(validate.ROOT, manifests).keys())


def _overlay_engine_code(release_tree: str, present_ids: list, exclude=None) -> tuple:
    """Overlay the engine CODE of the PRESENT packages from `release_tree`: each present module's
    `provides` files + its manifest, plus the FOUNDATION_CODE infra the release ships. Driven off the
    PRESENT set (never the release tree's modules/*), so a deselected module is NEVER resurrected. Operator config (engine.json identity, the policy-override) and gitignored
    data + the per-instance eADR stream (`.engine/contracts/instance/`) are in no
    `provides`/FOUNDATION_CODE, so they are untouched.
    CONTAINMENT GUARD (the topology wall): every destination must resolve INSIDE ROOT — fail closed BEFORE
    any write (the add path's containment-first pattern). `exclude` (a set of repo-relative paths) is NOT overwritten — the brownfield
    arrival passes the engine-exclusive paths an operator chose to keep ('leave-as-is', a class-1 collision),
    so the engine coexists around them rather than replacing them. Returns (copied_relpaths,
    {module_id: release_manifest})."""
    skip = set(exclude or ())
    candidates: dict = {}
    for mid in present_ids:
        man_src = os.path.join(release_tree, ".engine", "modules", mid, "manifest.json")
        if not os.path.isfile(man_src):
            raise _UpgradeRefused(f"the engine release does not contain the installed module '{mid}', so "
                                  f"the update was stopped and nothing was changed.")
        candidates[mid] = validate.load_json(man_src)
    # The overlay membership is single-homed in _overlay_copy_map, so the merge-time upgrade-overwrite
    # disclosure (overlay_replace_paths) reads the SAME enumeration against the live tree and cannot drift.
    to_copy = _overlay_copy_map(release_tree, candidates)
    escapes = sorted(rel for rel in to_copy if not _within_root(rel))
    if escapes:
        shown = ", ".join(escapes[:3]) + ("…" if len(escapes) > 3 else "")
        raise _UpgradeRefused(f"the update was stopped because it tried to place files outside the engine "
                              f"({shown}); nothing was changed.")
    copied = []
    for rel, src in sorted(to_copy.items()):
        if rel in skip:                      # an operator file the arrival is keeping (class-1 leave-as-is)
            continue
        dst = os.path.join(validate.ROOT, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        copied.append(rel)
    return copied, candidates


# ---- the release-authoritative synced surface (issue #599) --------------------------------------------
#
# The copy-only overlay (_overlay_copy_map) is a hand-enumerated subset, so every release that grows a new
# KIND of file silently under-covers it. The reconcile drives a deployed tree to `provision(release)` — the
# release AFTER the same first-run mutation — by reading the release's OWN self-description (manifests +
# FOUNDATION_CODE + the fixture namespace + first-run-assets.json), layered ON TOP OF the overlay so the
# operator-facing overwrite disclosure (overlay_replace_paths) never widens to files the reconcile only ADDS.


def _plain_oserror(exc: OSError) -> str:
    """The human half of an OSError for an operator-facing line — 'permission denied', not '[Errno 13] ...'."""
    return (getattr(exc, "strerror", None) or str(exc)).lower()


def _safe_retire_entry(p) -> bool:
    """True iff a retire-manifest entry is a safe, in-tree, repo-RELATIVE path that does NOT resolve to the
    repo root. Rejects the shapes that would widen a delete to the whole tree or outside it — an empty string,
    '.', '/', a '..' escape, or an absolute path (the whole-repo `rmtree` guard, security review)."""
    if not isinstance(p, str) or not p.strip() or os.path.isabs(p):
        return False
    norm = os.path.normpath(p)
    if norm in ("", ".", os.sep) or norm.startswith(".." + os.sep) or norm == "..":
        return False
    return _within_root(p) and os.path.abspath(os.path.join(validate.ROOT, p)) != os.path.abspath(validate.ROOT)


def retire_set(tree_root: str) -> tuple:
    """The first-run-only assets a release RETIRES from a deployed repo — the `provision()` projection, read
    from the release's OWN `.engine/provisioning/first-run-assets.json` (self-describing, core-owned, travels,
    and survives retirement). Returns (files:set, dirs:set) of repo-relative paths. RAISES `_UpgradeRefused`
    on an absent, unreadable, empty, or STRUCTURALLY-DANGEROUS manifest (an entry that is empty, absolute, or
    resolves to the repo root — which would turn a mis-authored release into a whole-tree delete): the upgrade
    reconcile fails LOUD (a clean refusal), never a silent fall-through to the un-projected template shape —
    which would resurrect the retired set the reconcile exists to keep out (risk-S3, security review)."""
    manifest = os.path.join(tree_root, ".engine", "provisioning", "first-run-assets.json")
    try:
        data = validate.load_json(manifest)
        files = {p for p in (data.get("files") or []) if isinstance(p, str)}
        dirs = {p for p in (data.get("directories") or []) if isinstance(p, str)}
    except Exception:   # noqa: BLE001 — absent/unreadable/malformed -> clean refusal, never silent
        raise _UpgradeRefused(
            "The update was applied to your working copy, but the new version's setup-file list was missing or "
            "unreadable, so the update was NOT opened for review and nothing was merged. Run the update again "
            "to retry, or ask me to undo the update's changes.")
    if not files and not dirs:
        raise _UpgradeRefused(
            "The update was applied to your working copy, but the new version's setup-file list was empty, so "
            "the update could not tell which setup-only files to keep out of your repo; it was NOT opened for "
            "review and nothing was merged. Run the update again to retry, or ask me to undo the update's "
            "changes.")
    if any(not _safe_retire_entry(p) for p in (files | dirs)):
        raise _UpgradeRefused(
            "The update was applied to your working copy, but the new version's setup-file list named an "
            "unusable path, so the update was stopped rather than risk removing the wrong thing — nothing was "
            "opened for review or merged. This is an engine defect to report; ask me to undo the update's "
            "changes.")
    return files, dirs


def engine_synced_map(tree_root: str, manifests_by_id: dict, *, project_retire: bool) -> dict:
    """{repo-relative -> source-abspath} the reconcile DELIVERS from `tree_root`: the copy-only overlay
    membership (`_overlay_copy_map` — provides ∪ manifests ∪ FOUNDATION_CODE) UNIONED with the committed
    fixture namespace (`module_coherence.FIXTURE_PATHS` — `.engine/_fixtures/**`, the file CATEGORY the
    hand-enumerated overlay silently missed, #599 class 3). When `project_retire` (the upgrade path), the
    release's OWN retire set is SUBTRACTED — the `provision()` projection that makes the surface the DEPLOYED
    shape, not the template shape, so a first-run-only file is never delivered onto a deployed repo. Arrival
    passes `project_retire=False` (it delivers the full template surface and runs `retire()` itself). Layered
    ON TOP OF `_overlay_copy_map`, never folded into it: that map's keys double as the overwrite disclosure
    (`overlay_replace_paths`), and a fixture is ADDED, not overwritten — folding it in would cry wolf. Adding
    a future committed namespace is a one-line union here — the single home the release-cut guard checks."""
    to_deliver = dict(_overlay_copy_map(tree_root, manifests_by_id))
    for ns in module_coherence.FIXTURE_PATHS:
        base = os.path.join(tree_root, *ns.split("/"))
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                src = os.path.join(dirpath, name)
                to_deliver[os.path.relpath(src, tree_root).replace(os.sep, "/")] = src
    if project_retire:
        r_files, r_dirs = retire_set(tree_root)
        dir_prefixes = tuple(d + "/" for d in r_dirs)
        to_deliver = {rel: src for rel, src in to_deliver.items()
                      if rel not in r_files and not rel.startswith(dir_prefixes)}
    return to_deliver


def engine_synced_paths(tree_root: str, manifests_by_id: dict, *, project_retire: bool) -> set:
    """The KEEP set the reconcile's delete leg protects: every `engine_synced_map` member (the DELIVER set)
    PLUS the five keyed/rendered foundation files FOUNDATION_CODE deliberately excludes (engine.json,
    CODEOWNERS, root CLAUDE.md/AGENTS.md, .gitignore) — re-rendered or keyed-merged locally, never delete
    candidates. DISTINCT from `module_coherence.engine_owned_paths` (which omits fixtures + manifests) — do
    not dedupe.

    INVARIANT (pinned by test_module_manager.TestReconcileDeliverySuperset, #599 Slice 3): this deliver set is a
    SUPERSET of every `provides`-owned `.engine/` file — the reconcile never ships less than the owned surface.
    Honest bound: that is all it proves. It does NOT prove a file is delivered to the RIGHT place, nor that an
    operator-authored file parked under an engine glob is classified correctly — those stay merge-gate concerns."""
    return set(engine_synced_map(tree_root, manifests_by_id, project_retire=project_retire).keys()) | set(_FOUNDATION_KEYED)


def _reconcile_carveouts() -> tuple:
    """The never-delete carve-outs for the reconcile delete leg, as (exact:set, prefixes:tuple): operator
    config, the pruned runtime roots, the deployment's per-instance eADR stream, the committed fixture
    namespace, and the five keyed/rendered foundation files. Mirrors module_coherence's carve-out sets so an
    operator's tuning, saved data, instance decision records, and product files are never delete candidates
    (they are outside `old_owned` by construction — this is the belt, not the sole protection)."""
    exact = set(module_coherence.OPERATOR_CONFIG) | set(_FOUNDATION_KEYED)
    prefixes = tuple(sorted(d + "/" for d in (set(module_coherence.PRUNE_PATHS)
                                              | set(module_coherence.DEPLOYMENT_CONTRACTS)
                                              | set(module_coherence.FIXTURE_PATHS))))
    return exact, prefixes


def _copy_synced(to_deliver: dict, *, exclude=None) -> list:
    """Copy every (repo-relative -> source-abspath) member the LIVE tree lacks or that DIFFERS, honoring an
    `exclude` skip set (arrival's class-1 'leave-as-is' keeps; the upgrade passes none). Containment
    fail-closed BEFORE any write (the topology wall, mirroring `_overlay_engine_code`). Returns the delivered
    repo-relative paths. Takes a precomputed map so a caller that also needs the KEEP set does not re-glob it."""
    import filecmp   # local: only the reconcile deliver needs the byte-compare
    escapes = sorted(rel for rel in to_deliver if not _within_root(rel))
    if escapes:
        shown = ", ".join(escapes[:3]) + ("…" if len(escapes) > 3 else "")
        raise _UpgradeRefused(f"the update was stopped because it tried to place files outside the engine "
                              f"({shown}); nothing was changed.")
    skip = set(exclude or ())
    delivered = []
    for rel, src in sorted(to_deliver.items()):
        if rel in skip:                          # an operator file arrival is keeping (class-1 leave-as-is)
            continue
        dst = os.path.join(validate.ROOT, rel)
        if os.path.isfile(dst) and filecmp.cmp(src, dst, shallow=False):
            continue                              # already present and identical — not a delivery
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        delivered.append(rel)
    return delivered


def _deliver_synced(tree_root: str, manifests_by_id: dict, *, project_retire: bool, exclude=None) -> list:
    """The shared deliver primitive for BOTH the upgrade tail reconcile (`project_retire=True`) and brownfield
    arrival (`project_retire=False`) — so arrival now delivers the fixture category too (#599). Computes the
    `engine_synced_map` and copies via `_copy_synced`."""
    return _copy_synced(engine_synced_map(tree_root, manifests_by_id, project_retire=project_retire),
                        exclude=exclude)


def _wiring_delta(old_by_id: dict, new_by_id: dict) -> dict:
    """PURE identity-keyed delta between the currently-installed manifests (`old_by_id`) and a release's
    manifests (`new_by_id`): {"added", "removed", "updated"}, each a list of (module_id, wire).

    Reads DECLARATIONS only — `wiring.declared_wire_identity`, never `is_applied`/`apply_all` — so a release's
    brand-new seam vocabulary is never executed by the running (pre-overlay) process (#594). It is the SINGLE
    SOURCE of the removal rule shared with `_apply_wiring_deltas`, so the read-only preview (`plan_upgrade`)
    cannot promise a wiring change the apply won't make:
      - removed  = an old engine-identifiable wire whose identity is gone in the new version — EXACTLY the set
        `_apply_wiring_deltas` reverses (identity-less wires, whose identity is None, are never reversed and so
        are never reported removed, matching the apply's reversal firewall).
      - added    = a new wire whose identity is absent from the old version (an identity-less new wire counts
        as added only when it is not byte-identical to an existing old wire — a genuinely-new seam has no
        identity and can only ever be an addition).
      - updated  = a wire present in both by identity but whose declaration content changed (an in-place
        re-apply — e.g. an `mcp` server, keyed on name, whose definition changed, or a `gitignore` fence,
        keyed on its key, whose lines changed): the apply re-applies it in place, so it is neither a removal
        nor an addition."""
    added, removed, updated = [], [], []
    for mid in sorted(set(old_by_id) | set(new_by_id)):
        old_list = (old_by_id.get(mid) or {}).get("wires") or []
        new_list = (new_by_id.get(mid) or {}).get("wires") or []
        old_by_key = {}
        for w in old_list:
            k = wiring.declared_wire_identity(w)
            if k is not None:
                old_by_key[k] = w
        new_ids = {wiring.declared_wire_identity(w) for w in new_list} - {None}
        for w in old_list:
            k = wiring.declared_wire_identity(w)
            if k is not None and k not in new_ids:          # gone in the new version -> the apply reverses it
                removed.append((mid, w))
        for w in new_list:
            if not isinstance(w, dict):
                continue                                     # a malformed (non-dict) wire — the apply's
                                                             # declared_wire_identity/apply_all ignore it too
            k = wiring.declared_wire_identity(w)
            if k is None:
                if w not in old_list:                        # identity-less new wire, not already present
                    added.append((mid, w))
            elif k not in old_by_key:
                added.append((mid, w))                       # genuinely-new identity
            elif old_by_key[k] != w:
                updated.append((mid, w))                     # same identity, changed content -> re-applied
    return {"added": added, "removed": removed, "updated": updated}


def _apply_wiring_deltas(old_by_id: dict, new_by_id: dict) -> list:
    """Reverse the wires a module no longer declares and (re)apply the wires it declares now (the
    scenario's 'apply/reverse wiring deltas'). For an unchanged version the delta is empty (apply_all is
    idempotent). A removed engine-identifiable wire is reversed so it does not linger; a same-identity
    content change is re-applied, and if a seam cannot update in place the forward coherence leg (step 5)
    catches the drift. The removal decision is single-homed in `_wiring_delta` so the read-only preview
    reports the same reversals this applies. Returns plain-language lines."""
    removed_by_mid: dict = {}
    for mid, w in _wiring_delta(old_by_id, new_by_id)["removed"]:
        removed_by_mid.setdefault(mid, []).append(w)
    lines = []
    for mid, new_m in new_by_id.items():
        for w in removed_by_mid.get(mid, []):               # reverse this module's removed wires first
            lines.append(validate.fmt(wiring.reverse(w)))
        for f in wiring.apply_all(new_m.get("wires") or []):  # then apply the new version's wires (idempotent)
            lines.append(validate.fmt(f))
    return lines


def _bump_engine_manifest(target_versions: dict, engine_release: str) -> dict:
    """Update the engine manifest in place: set engine_release + each present package's version to the
    release's, PRESERVING identity and any other operator-owned keys (engine.json is operator config, not
    overlaid). Returns the new manifest."""
    engine = module_coherence.load_engine_manifest() or {"packages": {}}
    # Release tags are v-prefixed (`v0.1.0` — the git/GitHub convention and what the maintainer sees), but
    # `engine_release` is stored BARE so it matches the bare per-package versions below; otherwise the
    # manifest would carry the engine version as `v0.1.0` and every package as `0.1.0` — a self-inconsistent
    # store the next cut's floor derivation would then oscillate. Strip a single leading `v` (comparison
    # already v-strips, so nothing behavioural changes — this only keeps the stored form consistent).
    engine["engine_release"] = engine_release[1:] if engine_release.startswith("v") else engine_release
    pkgs = engine.setdefault("packages", {})
    for mid, ver in target_versions.items():
        if mid in pkgs:
            pkgs[mid] = ver
    _write_json(_engine_manifest_path(), engine)
    return engine


def _resync_tool_runtime() -> bool:
    """Group-scoped `uv sync` rebuilds the tool-runtime from the overlaid lockfile BEFORE migrations run
    in it (provisioning step 3) — shelled via subprocess (the bootstrap.py pattern). It materializes the
    runtime only and never mutates a gitignored data store. Returns True on success. NEVER runs in tests /
    the demo (the injected-release path skips it) — one of the four named inductive gaps."""
    import subprocess   # local: only the real re-sync needs it
    try:
        subprocess.run(["uv", "sync"], cwd=os.path.join(validate.ROOT, ".engine"),
                       check=True, capture_output=True, timeout=300)
        return True
    except Exception:   # noqa: BLE001 — degrade: the caller surfaces a re-sync failure, never crashes
        return False


# The Markdown-structural characters a retired-capability description is escaped against so it renders as the
# author's LITERAL words in the PR body: inline code (`), emphasis (* _), links/images ([ ] !), inline HTML
# (< >), strikethrough (~). Each is backslash-escaped (GitHub renders '\x' as a literal 'x' for ASCII
# punctuation), so nothing the author wrote is deleted and no construct can reshape or disguise the notice.
# Leading block markers (#, >, -) need no handling: the "- " list prefix already puts the text in INLINE context,
# where they are literal — and deleting them (an earlier approach) silently corrupted content like '>50% mode'
# or '--force', which is exactly the notice this feature must render faithfully.
_MD_LITERAL = str.maketrans({c: "\\" + c for c in "\\`*_[]<>~!"})


def _retired_capability_text(description) -> str:
    """The retired-capability description as one plain single line for a TERMINAL surface (the upgrade preview and
    the applied-upgrade echo): collapse newlines and runs of whitespace to single spaces, and nothing else. The
    terminal is not Markdown, so the author's characters are shown VERBATIM — never stripped or altered, so a
    retired flag like '--force' or a claim like '>50% memory mode' survives exactly as written."""
    return " ".join(str(description or "").split()) or "a capability was removed"


def _retired_capability_line(description) -> str:
    """The description as a Markdown list item for the upgrade PR body's Scope section — the durable consent
    surface a non-engineer reads at the merge, and the FIRST free-text manifest field to render there (a
    migration's description never reaches the PR body). Render the author's words as LITERAL text: whitespace is
    collapsed (so no embedded newline breaks the list) and every Markdown-structural character is escaped (so an
    inline link, code span, HTML tag, or emphasis run can't reshape or disguise the notice) — while every
    character the author wrote SURVIVES, escaped rather than deleted. A truthful-rendering control, not a security
    boundary: the text is maintainer-authored, schema-validated, and human-reviewed at the release cut."""
    return "- " + _retired_capability_text(description).translate(_MD_LITERAL)


def render_upgrade_pr_body(from_versions: dict, target_versions: dict, result: dict) -> str:
    """The engine update's own pull-request body, authored in the repository template's shape — the eight
    required sections plus the consent preamble every engine pull request carries — so an engine update reads
    like every other engine pull request and clears the same body-completeness gate (the free-form body it
    replaces did not). Operator-facing and consent-critical: it is what a non-engineer reads to decide whether
    to merge an engine self-update, so it speaks of an *update* (never a release/publish — that is the other
    direction), carries every shared-file outcome the update produced OR refused (a floor the update could not
    touch must never be invisible at the merge), and its Validation section claims only the consistency check
    that actually ran before the update was opened — never a fuller CI pass the tail does not run.

    Reuses release_cut's public template helpers (`pr_section`/`template_preamble`) — one preamble source, no
    second copy to drift from the gate's anchor phrases. Imported LAZILY: release_cut imports this module, so a
    top-level import would cycle; both modules are fully loaded by the time an upgrade authors its body.
    Tolerant of a partial `result`: any outcome absent from it produces no line, never a fabricated
    'nothing happened' claim. The reconcile facts (#599) — fixtures delivered, floors created, and files
    removed (bucketed so an operator's file under an engine folder is surfaced, never removed silently) — ride
    the durable Scope, so the destructive delete leg is visible at the merge."""
    import release_cut  # noqa: E402 — lazy: avoids the release_cut<->module_manager import cycle (see docstring)

    # Scope — the version move, then every shared-file outcome the update produced or refused. BOTH the applied
    # and the could-not-apply notes are surfaced: a marked block the update left unchanged is a thing the
    # operator must see before merging, never a silent omission (the degraded/skipped branches below).
    scope = ["The version this update moves the engine to:"]
    for mid in sorted(target_versions):
        scope.append(f"- {mid}: {from_versions.get(mid, '—')} → {target_versions.get(mid)}")
    ran = (result.get("migrations") or {}).get("ran") or []
    if ran:
        # run_migrations formats each as "<mid> -> <ver> (<kind>)"; render it plainly — a unicode arrow to
        # match the version lines above, and the raw data/config category glossed. A data migration mutates
        # the operator's SAVED MEMORY, so it must never read more opaquely than a CODEOWNERS refresh.
        scope += ["", "Stored data or settings the update changed:"]
        scope += ["- " + r.replace(" -> ", " → ").replace("(data)", "(your saved memory)")
                           .replace("(config)", "(engine settings)") for r in ran]
        # A data migration's reversibility fact belongs on the DURABLE consent surface, not only the transient
        # CLI note the tail also composes: a recovery copy was saved first, and — when the engine could not
        # confirm it is locked — a keep-it heads-up. Read from the same migration result that note is built from.
        if any("(data)" in r for r in ran):
            saved = ("- Before changing your saved memory, the engine saved a recovery copy of it first, so "
                     "this update stays reversible — nothing for you to do.")
            if (result.get("migrations") or {}).get("backup_unprotected"):
                saved += (" One heads-up: the engine could not confirm that recovery copy is locked, so it "
                          "could be deleted by hand — leave it in place to keep the undo available.")
            scope.append(saved)
    # Capability retirements — the plain "you could do this before, and now you can't" line the operator would
    # otherwise never get. The description IS the whole notice (there is no kind/gloss to fall back on the way a
    # migration has), so unlike the migration block above this DOES render the authored description, literalized
    # so stray Markdown can't garble it.
    retired = result.get("retired_capabilities") or []
    if retired:
        scope += ["", f"Capabilities this update removed — things you could ask for before and no longer can "
                  f"({len(retired)}):"]
        scope += [_retired_capability_line(r.get("description")) for r in retired]
    shared: list = []
    co = result.get("codeowners")
    if co == "written":
        shared.append("- Refreshed the list of engine files that route to you for review, so this version's "
                      "new files are covered. Any review rules you added yourself are untouched.")
    elif co == "degraded":
        shared.append("- Could not refresh the engine-file review list (no account handle on record); your "
                      "existing CODEOWNERS file was left unchanged.")
    cf = result.get("claude_floor")
    if cf == "merged":
        shared.append("- Updated your project's working guide (the engine's marked block in CLAUDE.md) to this "
                      "version. Anything you wrote outside that block is untouched.")
    elif cf == "degraded":
        shared.append("- Could not update your project's working guide — the engine's marked block in CLAUDE.md "
                      "looked damaged, so I left the file unchanged. Check the marker lines, then update again.")
    elif cf == "skipped-no-section":
        shared.append("- Did not update your project's working guide — I found no engine marked block in "
                      "CLAUDE.md, so I left the file unchanged.")
    elif cf == "created":
        shared.append("- Created your project's working guide (the engine's marked block in CLAUDE.md) — this "
                      "version needs it and your repo did not have it yet.")
    af = result.get("agents_floor")
    if af == "merged":
        shared.append("- Updated the engine's Codex guide (the marked block in AGENTS.md) to this version. "
                      "Anything you wrote outside that block is untouched.")
    elif af == "degraded":
        shared.append("- Could not update the engine's Codex guide — the marked block in AGENTS.md looked "
                      "damaged, so I left the file unchanged. Check the marker lines, then update again.")
    elif af == "skipped-no-section":
        shared.append("- Did not update the engine's Codex guide — I found no engine marked block in AGENTS.md, "
                      "so I left the file unchanged.")
    elif af == "created":
        shared.append("- Created the engine's Codex guide (the marked block in AGENTS.md) — this version needs "
                      "it and your repo did not have it yet.")
    fi = (result.get("foundation_ignores") or {}).get("status")
    if fi == "written":
        shared.append("- Updated the engine's ignore list (the marked block in .gitignore that keeps the "
                      "engine's own tool files, per-session folders, and regenerable caches out of git) to this "
                      "version. Any ignore lines you added yourself are untouched.")
    elif fi == "degraded":
        shared.append("- Could not update the engine's ignore list — the marked block in .gitignore looked "
                      "damaged, so I left the file unchanged. Check the marker lines, then update again.")
    if shared:
        scope += ["", "What this update did to the engine's marked blocks in shared files:"] + shared

    # Reconcile outcomes (#599): files this version DELIVERED (fixtures an older update would have missed) and
    # files it REMOVED (renamed/dropped engine files, so stale copies don't linger). Removals are BUCKETED so a
    # file that merely sits under an engine folder — and could be one the operator added — is surfaced for a
    # deliberate look at the merge, never removed silently.
    fixtures = result.get("fixtures_delivered") or []
    removed = result.get("orphans_removed") or {}
    if fixtures:
        scope += ["", f"Engine files this version added that an older update would have missed, now delivered "
                  f"({len(fixtures)}):"]
        scope += [f"- {r}" for r in fixtures]
    eng_removed = removed.get("engine") or []
    if eng_removed:
        scope += ["", "Engine files this version dropped or renamed, now removed so stale copies don't linger:"]
        scope += [f"- {r}" for r in eng_removed]
    susp_removed = removed.get("suspect") or []
    if susp_removed:
        scope += ["", "Files removed that sat inside an engine folder this version no longer ships — if any of "
                  "these was a file you added yourself, tell me before merging and I'll restore it:"]
        scope += [f"- {r}" for r in susp_removed]
    left = removed.get("left_in_place") or []
    if left:
        scope += ["", "Files the update tried to remove but could not (remove them by hand if you don't need "
                  "them):"]
        scope += [f"- {r}" for r in left]

    out = ["# Updating the engine", "", release_cut.template_preamble(), ""]
    out += release_cut.pr_section(
        "Purpose",
        "This updates the engine to a newer released version, for you to review and merge.",
        ["- Merging this applies the update; closing it changes nothing and leaves your current version in "
         "place.",
         "- An update only ever moves the engine version forward."],
        "merging is your consent to run the updated engine; nothing changes until you merge.")
    out += release_cut.pr_section(
        "Scope",
        ("The version this update records, the shared-file blocks it refreshed, and the capabilities it retires."
         if retired else
         "The version this update records and the shared-file blocks it refreshed."),
        scope,
        ("the exact versions written into the engine's records, the marked-block refreshes noted, and — listed "
         "above — the things you could ask for before and no longer can."
         if retired else
         "these are the exact versions written into the engine's records, plus the marked-block refreshes noted."))
    out += release_cut.pr_section(
        "Out of scope",
        "What merging does not do.",
        ["- It does not change your own project files, code, or content.",
         "- It does not change any settings you configured yourself.",
         "- It changes nothing outside the engine's own files and its marked blocks in shared files."],
        "the update touches only engine-owned files and the engine's marked blocks in shared files.")
    risk_bullets = []
    if retired:
        # The one change in an update that alters what the operator can ASK the engine to do — surfaced here, in
        # "what to weigh before merging", so a header-skimming reader meets it and not only in the Scope body.
        risk_bullets.append(
            "- A capability you could use before is gone — see \"Capabilities this update removed\" under Scope. "
            "This is the one part of an update that changes what you can ask the engine to do, so read it before "
            "you merge.")
    risk_bullets += [
        "- An update replaces the engine's own tool and rule files with the new version's, and removes engine "
        "files this version renamed or dropped; your project content is not touched.",
        "- Every file this update removed is listed under Scope above — read them, and flag any that was "
        "yours before merging.",
        "- Any shared-file block the update could not refresh is also called out under Scope — read those "
        "before merging."]
    out += release_cut.pr_section(
        "Risk",
        "What to weigh before merging.",
        risk_bullets,
        ("a capability you could use is gone (see Scope); and the update changes and removes engine-owned files, "
         "with every removal and anything it could not apply disclosed in Scope."
         if retired else
         "the update changes and removes engine-owned files; every removal and anything it could not apply is "
         "disclosed in Scope."))
    out += release_cut.pr_section(
        "Validation",
        "What the engine checked before opening this.",
        ["- A structural consistency check on the rebuilt engine passed before this update was opened — the "
         "checks that catch a missing, orphaned, or mismatched engine file.",
         "- That is a structural check, not the engine's full check suite: the full suite runs here on this "
         "pull request, and your review at the merge is the real gate."],
        "a structural consistency check passed; the full suite runs on this pull request and the merge is "
        "still yours.")
    out += release_cut.pr_section(
        "Review",
        "How to act on this.",
        ["- Merge to apply the update.",
         "- Close it to decline — nothing changes and you stay on your current version.",
         "- To undo the update after merging, ask me to undo it or revert this pull request."],
        "merging applies the update, and it stays reversible afterward.")
    out += release_cut.pr_section(
        "Files of interest",
        "What this changes.",
        ["- The engine's version record (.engine/engine.json) and the module manifests.",
         "- The engine's own files this version added or removed, and its marked blocks in shared files "
         "(CODEOWNERS, CLAUDE.md, AGENTS.md, .gitignore), where this version updated them — each is noted "
         "under Scope."],
        "the changed files are the engine's own records and files plus its marked blocks in shared files.")
    out += release_cut.pr_section(
        "AI involvement",
        "Who did what.",
        ["- I assembled this update mechanically — fetching the new version, applying it, and running the "
         "engine's consistency check.",
         "- I did not decide to merge it; that decision is yours."],
        "the update was assembled by the engine; your merge is the decision.")
    return "\n".join(out)


def _github_error_detail(exc) -> str:
    """GitHub's human-readable reason from a FAILED API response body, safe to show the operator — WITHOUT
    surfacing anything sensitive. The body is field-validation JSON and never echoes request headers, so the
    auth token cannot leak through it. A 422 on `/pulls` carries a generic top-level `message` ("Validation
    Failed") with the ACTUAL cause in `errors[].message` (e.g. "A pull request already exists for …", "No
    commits between base and head"), so join the top-level message with each nested error. `exc.read()` yields
    bytes, may be empty, and can be read only ONCE; decode defensively and NEVER raise from here — a diagnostic
    helper must not replace the original HTTP failure with a read/parse error. Returns "" when there is nothing
    usable to add."""
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 — an unreadable body must not mask the HTTP error it is explaining
        return ""
    if not raw:
        return ""
    try:
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — a non-JSON body: a bounded slice of the raw text is still useful
        return raw.decode("utf-8", errors="replace")[:300].strip()
    if not isinstance(body, dict):
        return ""
    parts = []
    top = body.get("message")
    if isinstance(top, str) and top.strip():
        parts.append(top.strip())
    errs = body.get("errors")
    for err in errs if isinstance(errs, list) else []:      # `errors` may be absent or a non-list — never iterate a scalar
        msg = err.get("message") or err.get("code") if isinstance(err, dict) else None
        if isinstance(msg, str) and msg.strip():
            parts.append(msg.strip())
    return "; ".join(parts)


def _open_upgrade_pr(branch: str, title: str, body: str, repo=None, token=None) -> dict:
    """THE GIT+PR BOUNDARY (provisioning step 6): stage the overlaid change on a new branch, commit, push,
    and open a pull request so an upgrade is reviewed + reversible like any change. NET-NEW (no
    git-automation helper existed) — branch/commit/push via subprocess (the bootstrap.py pattern), the PR
    via POST /repos/{slug}/pulls built through the shared github_client.request (the one authenticated-Request
    home). INJECTED for tests + the demo (upgrade(opener=...)), so this real path NEVER runs in the construction
    repo — one of the four named inductive gaps (no release to upgrade to, no PR to open).

    On a failed POST it raises a DIAGNOSABLE, caller-agnostic RuntimeError (#672): the branch is already
    committed and pushed by the time the POST runs, so the message names the resolved repo/base/head/URL and
    GitHub's own safe reason (read via _github_error_detail — never the auth token or headers) and says the
    branch is already pushed so the recovery is to open the pull request by hand, not to re-run. A git step
    failing EARLIER (checkout/add/commit/push) raises the OPPOSITE contract — the branch was NOT pushed, so it
    names the failed step, surfaces git's stderr, and says to clear a leftover branch and run again — because a
    "branch is pushed" claim would be false there. Each caller frames its own surrounding recovery; this
    boundary supplies only the diagnostics both callers share."""
    import subprocess, urllib.request, urllib.error, json as _json, boot, github_client  # local: only the real open needs these
    import repo_identity  # local: the shared default-branch resolver (dependency-light)
    slug = repo or boot.repo_slug()
    tok = token if token is not None else boot.gh_token()
    if not slug or not tok:
        raise RuntimeError("could not determine the engine repository / credentials to open the update "
                           "pull request.")
    base = repo_identity.resolve_default_branch()
    # STAGE-AND-PUSH. A git step failing here means the branch was NOT (fully) pushed, so the recovery is the
    # OPPOSITE of the POST-failure case below — there is no branch to open a pull request from yet. The most
    # common cause is a leftover branch from an earlier partial attempt colliding on `checkout -b`, so the
    # message names the failed step, surfaces git's own stderr (captured but otherwise dropped), and says
    # plainly the branch is not pushed and how to clear the collision — never the false "already pushed" claim.
    for args in (["git", "checkout", "-b", branch], ["git", "add", "-A"],
                 ["git", "commit", "-m", title], ["git", "push", "-u", "origin", branch]):
        try:
            subprocess.run(args, cwd=validate.ROOT, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            err = exc.stderr.decode("utf-8", errors="replace").strip() if isinstance(exc.stderr, bytes) \
                else (exc.stderr or "").strip()
            head = (f"preparing the pull-request branch failed at `{' '.join(args)}`"
                    + (f": {err}" if err else f" (exit {exc.returncode})"))
            if args[1] == "checkout":
                # The CREATE step failed, so no branch (and no commit) exists yet — most often a name collision
                # with a leftover branch from an earlier attempt. Deleting that is safe (nothing new is on it).
                recovery = (f" — so the branch '{branch}' was not created and there is no pull request to open "
                            f"yet. A common cause is a leftover '{branch}' branch from an earlier attempt; "
                            f"remove it (locally with `git branch -D {branch}`, and on the remote if it was "
                            f"pushed) and run this again.")
            else:
                # A LATER step failed (add/commit/push): the branch was already created and may hold the
                # arrival's committed changes, so DO NOT tell the operator to delete it — that would discard
                # their work. A push failure (the common case) is usually authentication, network, or branch
                # protection. Fix the reported cause, then finish by hand.
                recovery = (f" — the branch '{branch}' was created and holds the arrival's changes, so do not "
                            f"delete it. The pull request was not opened; fix the cause reported above, then "
                            f"finish by pushing the branch (`git push -u origin {branch}`) and opening the pull "
                            f"request yourself: `gh pr create --repo {slug} --base {base} --head {branch}`.")
            raise RuntimeError(head + recovery) from exc
    path = f"/repos/{slug}/pulls"
    payload = _json.dumps({"title": title, "head": branch, "base": base, "body": body}).encode("utf-8")
    req = github_client.request(path, tok, user_agent="engine-module-manager", method="POST", data=payload)
    # THE POST. Reached only after the branch is committed and pushed above — so here the recovery IS to open
    # the pull request by hand from the pushed branch. State it once, concretely, and never interpolate the
    # token or headers (only exc.code + GitHub's response reason + the resolved repo/base/head).
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = _github_error_detail(exc)
        reason = f"GitHub returned HTTP {exc.code}" + (f" — {detail}" if detail else "")
        raise RuntimeError(
            f"the branch '{branch}' was pushed but opening the pull request failed ({reason}). Open it "
            f"yourself: `gh pr create --repo {slug} --base {base} --head {branch}`."
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"the branch '{branch}' was pushed but GitHub could not be reached ({exc.reason}), so the pull "
            f"request was not opened. Open it yourself once you are back online: "
            f"`gh pr create --repo {slug} --base {base} --head {branch}`."
        ) from exc


def _refresh_codeowners(handle) -> str:
    """Re-render the CODEOWNERS ownership wall for the POST-overlay engine path set, so a release that
    adds/removes engine files keeps the wall complete and every engine file still routes to the operator
    for review — the design's upgrade re-render (the engine.json
    `handle` field). Operator-added rules are preserved (fence-scoped). Single-sources the path set +
    render with first-run via module_coherence.codeowners_path_set + wiring.apply_codeowners, so the two
    render sites can't drift. Returns 'written' | 'already' | 'degraded'. DEGRADES (no change) when no
    operator handle is on record (the construction repo / a pre-handle manifest) or the render refuses —
    never crashes."""
    if not handle:
        return "degraded"
    co_path = os.path.join(validate.ROOT, ".github", "CODEOWNERS")
    try:
        return wiring.apply_codeowners(co_path, module_coherence.codeowners_path_set(), handle)["status"]
    except wiring.WiringError:
        return "degraded"


def _read_release_floor(release_tree: str, root_rel: str) -> "list | None":
    """The floor SOURCE for the upgrade path (#323): the `floor` fence body extracted from the release's
    committed root file (CLAUDE.md / AGENTS.md — the promoted adopter floor). None when the release ships no
    usable floor — its root file is absent, carries no `floor` fence, or carries a malformed one (an old
    pre-promotion release, whose root file is the construction body with no fence, reads as None and is
    skipped). A fence body needs no whole-file trailing-newline trim — fence_read returns the body lines
    exactly as fenced. An empty or all-blank fence body reads as None too (no usable floor), so the upgrade
    path skips it exactly as the arrival path (_insert_floor) does — the two never diverge on a degenerate
    empty floor the engine would never emit."""
    src = os.path.join(release_tree, root_rel)
    if not os.path.isfile(src):
        return None
    try:
        body = wiring.fence_read(validate.read(src), _FLOOR_FENCE, style=wiring.MD_FENCE)
    except wiring.WiringError:
        return None   # a malformed release fence is no usable source → skipped, never a mid-upgrade crash
    return body if (body and any(ln.strip() for ln in body)) else None


def _merge_agents_floor(release_tree: str) -> str:
    """The AGENTS.md half of the floor keyed-merge — _merge_claude_floor's mechanics over the Codex root
    floor (local AGENTS.md, the `floor` fence in the release's AGENTS.md). Same return vocabulary."""
    return _merge_floor(release_tree, _ROOT_AGENTS_REL)


def _merge_claude_floor(release_tree: str) -> str:
    """Keyed-merge the engine's root-CLAUDE.md floor from the RELEASE's committed root CLAUDE.md into the local
    CLAUDE.md, replacing ONLY the engine `floor` fence and preserving any operator content outside it
    (keyed, reversible entries; the #234/#272 coexistence obligation). The floor SOURCE is the `floor` fence
    body extracted from the release's root CLAUDE.md — the promoted adopter floor (#323). CLAUDE.md is kept
    OUT of FOUNDATION_CODE and keyed-merged (never wholesale-overlaid), so the release's own root file is only
    ever read for its fenced floor block, never copied whole over an adopter's file.

    Returns: 'merged' (the engine block was replaced); 'created' (the floor file was ABSENT and is created
    from the release floor source — the AGENTS.md-never-created case, #599 class 2); 'skipped' (the release
    ships no floor source — its root file is absent or carries no/ malformed `floor` fence, e.g. a pre-promotion
    release); 'skipped-no-section' (the local CLAUDE.md EXISTS but carries no engine `floor` fence — leave it
    untouched, NEVER append a duplicate floor: the pre-keyed-merge raw-floor case); 'degraded' (a malformed
    LOCAL fence — leave it untouched, never a mid-upgrade crash). Structural sibling of `_refresh_codeowners`,
    but with no handle dependency."""
    return _merge_floor(release_tree, _ROOT_CLAUDE_REL)


def _merge_floor(release_tree: str, root_rel: str) -> str:
    """The shared keyed-merge mechanics for one root floor file (CLAUDE.md or AGENTS.md) — see
    _merge_claude_floor's contract. The floor SOURCE is the `floor` fence body extracted from the RELEASE's
    committed root file (the promoted adopter floor), NOT a whole-file `.deployed.md` (retired) and NEVER the
    local target. A release root file that is absent, carries no `floor` fence, or carries a malformed one is
    'skipped' — no source, never a strand."""
    floor_lines = _read_release_floor(release_tree, root_rel)
    if floor_lines is None:
        return "skipped"
    local_path = os.path.join(validate.ROOT, root_rel)
    local_exists = os.path.isfile(local_path)
    local = validate.read(local_path) if local_exists else ""
    try:
        if not local_exists:
            # CREATE-IF-ABSENT (#599 class 2): the foundation floor file was never created on this deployed
            # repo — a floor a LATER version introduced (the AGENTS.md case, which the keyed-merge below would
            # otherwise skip forever). Create it from the DEPLOYED floor source, exactly as first-run
            # provisioning would have. This branch fires ONLY when the file is truly absent; an EXISTING
            # fence-less file still takes the skip below (never append a floor into an operator's own file).
            created = wiring.fence_apply("", _FLOOR_FENCE, floor_lines, style=wiring.MD_FENCE)
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            with open(local_path, "w", encoding="utf-8") as fh:
                fh.write(created)
            return "created"
        if not wiring.fence_present(local, _FLOOR_FENCE, style=wiring.MD_FENCE):
            return "skipped-no-section"
        merged = wiring.fence_apply(local, _FLOOR_FENCE, floor_lines, style=wiring.MD_FENCE)
    except wiring.WiringError:
        return "degraded"
    if merged != local:
        with open(local_path, "w", encoding="utf-8") as fh:
            fh.write(merged)
    return "merged"


# ---- the version-sensitive upgrade tail: run as freshly-overlaid code (issue #594) ----
#
# THE BUG (#594): `upgrade()` overlays the new release's `.engine/tools/*.py` (core's `provides` glob covers
# wiring.py / module_coherence.py) onto disk, but the running process keeps the `wiring`/`module_coherence`
# it imported at startup. So the wire APPLIER and the coherence VERIFIER ran the PRE-upgrade library, and any
# wire seam a release newly introduced (v0.3.0's codex-mcp/codex-hook) could never be applied by its own
# upgrade. THE FIX: split the upgrade at the overlay boundary and run the version-sensitive tail — apply the
# new wiring, re-render seams, migrate, bump, re-check coherence, open the PR — in a FRESH child interpreter
# of the just-overlaid `module_manager.py`, so the NEW libraries run. The in-process path is kept for the
# fully-injected test/demo callers only (a callable can't cross a process boundary); every real path runs
# the child. This fixes the whole class, not just the codex seams.

_UPGRADE_TAIL_MARKER = "engine-upgrade-tail/v1"   # the internal-invocation marker carried in the state


def _upgrade_state_dump(obj: dict, path: str) -> None:
    """Single-homed (de)serializer for the parent<->child upgrade-tail hand-off, so the two ends can't
    drift (the module's single-home discipline — cf. _overlay_copy_map)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _upgrade_state_load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _merge_tail(result: dict, tail: dict) -> None:
    """Merge the tail's phase-2 fields over the phase-1 `result`, EXTENDING `notes` rather than replacing —
    so parent-owned fields (`copied`, `from`, `to`, `synced`) and any phase-1 note survive (arch review)."""
    for key, value in tail.items():
        if key == "notes":
            result["notes"].extend(value or [])
        else:
            result[key] = value


def _glob_namespace_prefixes(old_by_id: dict) -> tuple:
    """Directory prefixes of the OLD manifests' GLOB `provides` patterns — the namespaces where a file is
    engine-owned by WILDCARD rather than by an explicit literal. A removed file under one of these that the
    release no longer ships is the operator-file-SUSPECT bucket (risk-S1): agents are literal `provides`, so a
    renamed engine agent buckets as engine-dropped, while an operator's own file under `.engine/tools/` is
    surfaced for the operator to catch at the merge. Returns a tuple of `dir/` prefixes."""
    prefixes = set()
    for m in old_by_id.values():
        for _group, patterns in ((m or {}).get("provides") or {}).items():
            for pat in patterns:
                if any(ch in pat for ch in "*?["):
                    head = re.split(r"[*?\[]", pat, maxsplit=1)[0]
                    d = head.rsplit("/", 1)[0] if "/" in head else ""
                    if d:
                        prefixes.add(d.rstrip("/") + "/")
    return tuple(sorted(prefixes))


def _reconcile_surface(release_tree: str, candidates: dict, old_owned: list, old_by_id: dict) -> tuple:
    """The #599 reconcile: drive the deployed FILE surface to `provision(release)`. ADD — deliver every
    `engine_synced_map` member the tree lacks or that differs (fixtures + any overlay-missed file), projected
    so the first-run-retired set is never delivered. DELETE — reusing `remove_engine`'s compute-the-whole-set-
    before-any-deletion discipline: candidates are the OLD engine-owned surface (`old_owned`, threaded from
    the parent pre-overlay — the rename/drop orphans) UNIONED with the release's RETIRE set (the first-run
    files the parent's copy-only overlay resurrected onto this deployed tree), minus the KEEP set and the
    carve-outs. Returns (fixtures_delivered:list, removed:dict{engine, suspect, left_in_place})."""
    # Compute the release's synced map + retire set ONCE — the deliver leg and the KEEP set both read the map;
    # a bad/dangerous retire manifest raises up to the tail (clean refusal).
    r_files, r_dirs = retire_set(release_tree)
    synced = engine_synced_map(release_tree, candidates, project_retire=True)
    # ADD (provision-projected).
    delivered = _copy_synced(synced)
    fixtures_delivered = sorted(rel for rel in delivered
                                if any(rel == ns or rel.startswith(ns + "/")
                                       for ns in module_coherence.FIXTURE_PATHS))
    # DELETE — compute the WHOLE candidate set first (the live globs need the files still on disk).
    keep = set(synced.keys()) | set(_FOUNDATION_KEYED)
    exact_cv, prefix_cv = _reconcile_carveouts()

    def _spared(rel: str) -> bool:
        return rel in keep or rel in exact_cv or rel.startswith(prefix_cv)

    to_delete = sorted(rel for rel in (set(old_owned) | set(r_files))
                       if not _spared(rel) and _within_root(rel)
                       and os.path.isfile(os.path.join(validate.ROOT, rel)))
    glob_prefixes = _glob_namespace_prefixes(old_by_id)
    tracked = module_coherence._tracked_paths()   # git-tracked relpaths, or None when git is unavailable
    removed = {"engine": [], "suspect": [], "left_in_place": []}
    for rel in to_delete:
        # A known first-run (retire-set) file is engine; otherwise a file under a GLOB provides namespace could
        # be one the operator added — surface it — while a literal-named file is engine.
        suspect = rel not in r_files and bool(glob_prefixes) and rel.startswith(glob_prefixes)
        if suspect and tracked is not None and rel not in tracked:
            # An UNTRACKED (git-ignored) file under an engine folder is almost certainly the operator's own,
            # and the undo cannot restore it (git only restores tracked files). LEAVE it, surface it — so every
            # file the reconcile actually removes stays recoverable (security review).
            removed["left_in_place"].append(
                f"{rel} — left in place: it looks like a file you added (the engine does not track it), so I "
                f"did not remove it. Delete it yourself if you don't need it.")
            continue
        try:
            os.remove(os.path.join(validate.ROOT, rel))
            removed["suspect" if suspect else "engine"].append(rel)
        except OSError as exc:
            removed["left_in_place"].append(f"{rel} (could not remove: {_plain_oserror(exc)})")
    # Resurrected first-run DIRECTORIES (the setup skills the overlay's provides glob re-copied). Guard them
    # like the file leg PLUS a whole-tree sanity check: never rmtree the repo root (retire_set already refuses
    # that), a carve-out, or a directory that CONTAINS a kept/carve-out path (rmtree would take the nested
    # protected file with it) — the security-review fix for the previously-unguarded directory leg.
    for d in sorted(r_dirs):
        dp = os.path.join(validate.ROOT, *d.split("/"))
        dnorm = d.rstrip("/") + "/"
        if os.path.abspath(dp) == os.path.abspath(validate.ROOT):
            continue
        if dnorm.startswith(prefix_cv) or any(c.startswith(dnorm) for c in prefix_cv):
            continue   # under, or an ancestor of, a carve-out namespace (operator memory/data/records/fixtures)
        if any(k == d or k.startswith(dnorm) for k in keep) or any(e == d or e.startswith(dnorm) for e in exact_cv):
            continue   # would delete a kept or foundation path nested under it
        if not os.path.isdir(dp):
            continue
        try:
            shutil.rmtree(dp)
            removed["engine"].append(dnorm + "(setup folder)")
        except OSError as exc:
            removed["left_in_place"].append(f"{dnorm} (could not remove: {_plain_oserror(exc)})")
    return fixtures_delivered, removed


# The offline-reproducible STRUCTURAL subset of CI the pre-open gate runs against the reconciled tree. NOT
# full CI: the full `engine-ci` required check is a TWO-step job (the validator AND the self-tests), and its
# hard checks read a PR/event/network context that does not exist pre-open on the operator's machine
# (arch-S4, feasibility-B1/S1). This subset reads the reconciled TREE live and is exactly what #599 trips —
# and none of it reaches the network or needs the repo token. The full-CI proof lives in the cut-time
# deployment gate, which practice-upgrades real past releases to the candidate before a release is cut (#664),
# never on an operator's upgrade.
_STRUCTURAL_GATE_CHECK_IDS = frozenset({
    "engine/check/catalog-coverage",        # a delivered/removed file leaving the surface catalog incomplete
    "engine/check/census-completeness",     # the census of catalogued surfaces vs the tree
    "engine/check/self-map-drift",          # the regenerated self-map matching the reconciled module graph
    "engine/check/knowledge-coverage",      # the regenerated knowledge graph matching the reconciled surfaces
    "engine/check/codex-provider-parity",   # an orphaned .claude/agents/* with no .codex twin (the #599 class)
    "engine/check/codex-agent-coherence",   # the Codex agent renders matching their .claude sources
})
# NOT in the gate: `hard-check-bite` — it is a release-cut META-check that every hard check bites its
# negative fixture, a property of the CHECK CORPUS (verified where releases are cut), not of the reconciled
# deployed tree, and some checks only bite with construction/vault state a deployed repo need not have. The
# fixture DELIVERY it once caught missing (#599 class 3) is now guaranteed by the reconcile's deliver leg and
# asserted by the regression; the release's own CI still runs hard-check-bite on the opened pull request.


# The note the upgrade tail appends when it runs in PRACTICE mode (a local release injected, no pull request
# opened). Exposed as a module constant because the release-cut deployment gate matches on it to confirm a
# practice upgrade took the real child path (`release_gate._upgrade_from`) rather than silently fetching a
# published release — a single shared source, so a reword here can never quietly break that check.
PRACTICE_RUN_NOTE = "(practice run — the pull request was not opened)"


def _reconcile_gate(body: str) -> list:
    """The structural pre-open gate (see `_STRUCTURAL_GATE_CHECK_IDS`): `check_coherence()` in-process plus the
    structural CI subset, run against the reconciled tree before opening the update PR. Returns the findings;
    the tail refuses cleanly on any `hard` one. Scoped by a rule filter so `suites.json` is untouched (no
    guardrail change) and no PR/event/network check runs."""
    findings = list(module_coherence.check_coherence())
    findings += validate.collect("CI", {"pr_body": body, "pr_author": None, "pr_labels": []},
                                 with_source=True,
                                 rule_filter=lambda r: r.get("id") in _STRUCTURAL_GATE_CHECK_IDS)
    return findings


def _coherence_only_gate(body: str) -> list:
    """The fixture-safe gate for the IN-PROCESS (test/demo full-injection) path only: `check_coherence()`
    alone. The real structural subset (`_reconcile_gate`'s custom/script checks) is a subprocess that resolves
    `ROOT/script` and so cannot run against a throwaway fixture tree (B1). The in-process path is NEVER a real
    deployed upgrade (`in_process` ⇒ an injected release tree + injected callables — a real upgrade fetches a
    release and spawns a child), so the full gate on the child path is the one that matters, and it is proven
    against a real reconciled tree by `demo_599` and by the cut-time deployment gate's practice upgrades from
    real past releases (#664). `body` is accepted for signature parity with `_reconcile_gate`."""
    return list(module_coherence.check_coherence())


def _reconcile_refuse_reason() -> str:
    """The plain-language refusal when the structural gate finds a problem in the rebuilt engine — names the
    class of problem in words, never a check id (product-S3), and points at the re-run / undo recourse."""
    return ("The update was applied to your working copy, but a consistency check on the rebuilt engine found "
            "a problem, so it was NOT opened for review and nothing was merged. Run the update again to retry, "
            "or ask me to undo the update's changes.")


def _stage_worktree() -> None:
    """Best-effort `git add -A` at ROOT so the pre-open structural gate sees the to-be-committed set (the
    reconcile's adds AND deletes), not a transient dirty tree — some structural checks read git's tracked set
    (risk-N2). Best-effort: a non-git tree (the injected test fixture) or any git error is swallowed; the gate
    still reads the filesystem, and the real PR-open stages again."""
    try:
        import subprocess   # local: only the real tail stages
        subprocess.run(["git", "-C", validate.ROOT, "add", "-A"], capture_output=True, timeout=60, check=False)
    except Exception:  # noqa: BLE001 — best-effort; never crash the tail
        pass


def _regen_indexes() -> None:
    """Regenerate the deployed-state-dependent index files — the self-map and the knowledge graph — from the
    reconciled tree, so they describe the DEPLOYED shape (post first-run projection), NOT the construction
    shape the release ships. Both are in `core`'s `provides`, so the overlay delivers the release's
    construction versions; but they fingerprint the surface the reconcile just changed (the retire set is
    absent on a deployed repo), so the shipped copies would drift (self-map-drift / knowledge-coverage). These
    are exactly the §B 'render locally iff a function of deployed state' artifacts. Best-effort: a regen
    failure surfaces as a drift finding at the gate (a clean refusal), never a crash mid-upgrade."""
    import self_map            # lazy: only the reconcile tail needs the generators
    import knowledge_gen
    # Pass an EXPLICIT target under the CURRENT validate.ENGINE_DIR: the generators' own default-path constants
    # are bound at import to the real repo, so a bare generate() would write there even under a redirected tree
    # (a test/demo fixture). validate.ENGINE_DIR IS redirected, so building the path from it writes to the tree
    # actually being reconciled — the deployed repo on the real child path, the fixture under a redirect.
    for gen, target in ((self_map.generate, os.path.join(validate.ENGINE_DIR, "self-map.md")),
                        (knowledge_gen.generate, os.path.join(validate.ENGINE_DIR, "knowledge", "graph.json"))):
        if not os.path.isfile(target):
            continue   # the tree does not carry this index (a minimal fixture) — never fabricate one
        try:
            gen(path=target)
        except Exception:  # noqa: BLE001 — a regen failure becomes a drift finding at the gate, not a traceback
            pass


def _upgrade_tail(*, release_tree, target_ref, from_versions, target_versions, old_by_id, old_owned,
                  candidates, handle, selected, seam, practice, opener, gate=None) -> dict:
    """The version-sensitive tail of an upgrade — the work that MUST run as the freshly-overlaid engine code
    (the #594 fix): apply the new version's wiring with the FRESH appliers, re-render the release-evolvable
    seams (ownership wall, CLAUDE/AGENTS floor, foundation ignores), RECONCILE the file surface to
    `provision(release)` (deliver what the release added, remove what it dropped/renamed — #599), run
    migrations, bump the engine manifest AFTER migrations succeed (so an abort before them leaves nothing to
    silently skip on a re-run), gate the rebuilt tree on the structural check subset, and open the review pull
    request. Returns the tail portion of the result dict; the caller merges it over the phase-1 result. On the
    real path this runs inside the child interpreter (`_run_upgrade_tail`); the fully-injected test/demo path
    calls it directly. `practice` (or a None opener) skips the real git/PR boundary. `gate` overrides the
    structural gate for the injected test path (the real gate's custom/script checks cannot resolve against a
    throwaway fixture tree — B1); it defaults to `_reconcile_gate`."""
    tail = {"wiring": [], "codeowners": None, "claude_floor": None, "agents_floor": None,
            "foundation_ignores": None, "fixtures_delivered": [],
            "orphans_removed": {"engine": [], "suspect": [], "left_in_place": []},
            "migrations": {"ran": [], "refused": []}, "retired_capabilities": [],
            "findings": [], "pr": None, "notes": [], "applied": False, "reason": None}
    # (a0) RETIRED-CAPABILITY ANNOUNCEMENTS — derived from the FULL present-manifest set (`candidates`), NEVER
    # from `selected`: a version that retires a capability but ships no migration must still announce it, so this
    # is independent of migration selection (design-review). Announcement-only, so it is computed once up front
    # and simply rides the result — it runs nothing and can never refuse.
    tail["retired_capabilities"] = select_retired_capabilities(
        from_versions, target_versions, list(candidates.values()))
    # (a) WIRING DELTAS — reverse a wire the new version drops, (re)apply the wires it declares now, with the
    # freshly-overlaid appliers (this is the seam #594 fixed: a new wire type now actually applies).
    tail["wiring"] = _apply_wiring_deltas(old_by_id, candidates)
    # (b) RE-RENDER the release-evolvable seams. The floor merge now CREATES a never-created foundation floor
    # (the AGENTS.md case, #599 class 2) rather than skipping it forever.
    tail["codeowners"] = _refresh_codeowners(handle)
    tail["claude_floor"] = _merge_claude_floor(release_tree)
    tail["agents_floor"] = _merge_agents_floor(release_tree)
    tail["foundation_ignores"] = wiring.apply_foundation_ignores(wiring.GITIGNORE_PATH)
    tail["applied"] = True   # the working copy is now mutated (overlay + seams); any refusal below is half-state
    # (b2) RECONCILE the file surface to provision(release) — the #599 authority the copy-only overlay is not.
    # A refusal (a bad retire manifest, a containment escape) surfaces cleanly: staged, un-merged, nothing opened.
    try:
        tail["fixtures_delivered"], tail["orphans_removed"] = _reconcile_surface(
            release_tree, candidates, old_owned, old_by_id)
    except _UpgradeRefused as ur:
        tail["reason"] = ur.reason
        return tail
    # (c) MIGRATIONS (selected + dependency-ordered; the no-backup guard already pre-flighted in phase 1).
    tail["migrations"] = run_migrations(selected, from_versions, target_ref, backup=seam)
    if tail["migrations"].get("refused"):
        tail["reason"] = ("The update was applied to the working copy but a stored-data update could not be "
                          "completed (its backup did not succeed), so it was NOT opened for review and "
                          "nothing was merged. Ask me to set up or check your backup, then update again.")
        return tail
    if any(item.get("kind") == "data" for item in selected):
        saved_note = ("Before changing your saved memory, I automatically saved a copy of it from right "
                      "before this update — there's nothing for you to do now. If this update is ever "
                      "undone, I can bring that copy back.")
        if tail["migrations"].get("backup_unprotected"):
            saved_note += (" One heads-up: I couldn't confirm that saved copy is locked, so it could be "
                           "deleted by hand — keep it in place and this undo stays available.")
        tail["notes"].append(saved_note)
    # (d) BUMP the engine manifest — AFTER migrations succeed, BEFORE the gate. A child-launch / import /
    # migration failure therefore leaves engine.json UNbumped, so a re-run re-selects the migrations rather
    # than seeing from==to and silently skipping them (risk-review). The gate sees a bumped manifest that
    # matches the overlaid modules.
    _bump_engine_manifest(target_versions, target_ref)
    # Regenerate the deployed-state-dependent indexes (self-map + knowledge graph) from the reconciled tree,
    # so they describe the DEPLOYED shape rather than the construction shape the release shipped (#599).
    _regen_indexes()
    # Author the review-PR body FIRST — it carries the reconcile facts (fixtures, removals) into the pull
    # request, and rendering it early catches a template-read failure before staging (the structural gate does
    # NOT check body completeness — the release's own CI does, on the opened PR). Guarded: render reads the PR
    # template (I/O) and can raise, so a failure degrades to a clean refusal (staged, not opened), never a
    # traceback (the surfaced-never-a-crash rule).
    try:
        body = render_upgrade_pr_body(from_versions, target_versions, tail)
    except Exception as exc:   # noqa: BLE001 — staged but not prepared; surfaced, never a traceback
        tail["notes"].append(f"(the update is staged but its review pull request could not be prepared: {exc})")
        tail["reason"] = ("The update was applied to the working copy but its review pull request could not be "
                          "prepared, so nothing was opened or merged. Run the update again, or ask me to undo "
                          "the update's changes.")
        return tail
    # (e) STRUCTURAL GATE — refuse cleanly on any hard finding; never open a broken PR. Stage first so the
    # tree-vs-index checks see the to-be-committed set (risk-N2).
    _stage_worktree()
    tail["findings"] = (gate or _reconcile_gate)(body)
    if any(f.get("severity") == "hard" for f in tail["findings"]):
        tail["reason"] = _reconcile_refuse_reason()
        return tail
    # (f) LAND as a reviewed pull request (skipped on a practice run — no git/PR boundary).
    if practice or opener is None:
        tail["notes"].append(PRACTICE_RUN_NOTE)
        return tail
    title = f"Maintenance: update the engine to {target_ref}"
    branch = "engine-update-" + re.sub(r"[^a-zA-Z0-9._-]+", "-", target_ref)
    try:
        tail["pr"] = opener(branch=branch, title=title, body=body)
    except Exception as exc:   # noqa: BLE001 — staged but not opened; surfaced, never a traceback
        tail["notes"].append(f"(the update is staged but the pull request could not be opened: {exc})")
    return tail


def _run_upgrade_tail(state: dict) -> None:
    """Child entrypoint for `__upgrade_tail__`: run the upgrade tail as the FRESHLY-OVERLAID code and write
    the result to the parent's `result_path`. Fails closed on a state without the internal marker or with an
    implausible release location — the mutating tail must not be drivable by a stray operator command or an
    injected instruction (the env marker in `main()` is the first gate; this is the second)."""
    if state.get("marker") != _UPGRADE_TAIL_MARKER:
        raise _UpgradeRefused("the internal upgrade step was invoked without a valid marker.")
    release_tree = state["release_tree"]
    if not (os.path.isabs(release_tree) and os.path.isdir(release_tree)):
        raise _UpgradeRefused("the internal upgrade step got an unexpected release location.")
    present_ids = state["present_ids"]
    from_versions = state["from_versions"]
    target_versions = state["target_versions"]
    candidates, release_manifests = {}, []
    for mid in present_ids:
        man = validate.load_json(os.path.join(release_tree, ".engine", "modules", mid, "manifest.json"))
        candidates[mid] = man
        release_manifests.append(man)
    selected = select_migrations(from_versions, target_versions, release_manifests)
    practice = bool(state.get("practice"))
    seam = None if practice else _resolve_backup_seam(None)   # real seam re-resolved child-side (not crossable)
    opener = None if practice else _open_upgrade_pr
    tail = _upgrade_tail(
        release_tree=release_tree, target_ref=state["target_ref"], from_versions=from_versions,
        target_versions=target_versions, old_by_id=state["old_by_id"],
        old_owned=state.get("old_owned") or [], candidates=candidates,
        handle=state.get("handle"), selected=selected, seam=seam, practice=practice, opener=opener)
    _upgrade_state_dump(tail, state["result_path"])


def _spawn_upgrade_tail(state: dict) -> dict:
    """Parent side of the #594 fix: run the version-sensitive tail in a FRESH child interpreter of the
    just-overlaid `module_manager.py`, so the new version's wiring/coherence code actually runs. State
    crosses as a temp JSON file (callables cannot); the child writes its result to a sibling file we read
    back and merge. A child that dies or writes nothing maps to a clean 'staged but not completed' result,
    never a traceback — the half-state law (an abort leaves an un-merged, re-runnable tree). The child env
    is scoped deliberately (cf. validate.py's token discipline): start from the environment minus the
    GitHub token, mark it as an internal child, and add the token back ONLY on the real (non-practice) path,
    which is the only one that opens the pull request."""
    import subprocess   # local: only the real spawn needs it
    work = tempfile.mkdtemp(prefix="engine-upgrade-tail-")
    try:
        state_path = os.path.join(work, "state.json")
        result_path = os.path.join(work, "result.json")
        _upgrade_state_dump({**state, "result_path": result_path}, state_path)
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        env["ENGINE_UPGRADE_CHILD"] = "1"
        if not state.get("practice") and os.environ.get("GITHUB_TOKEN"):
            env["GITHUB_TOKEN"] = os.environ["GITHUB_TOKEN"]
        proc = subprocess.run(
            [sys.executable, os.path.join(validate.ROOT, ".engine", "tools", "module_manager.py"),
             "__upgrade_tail__", state_path],
            cwd=validate.ROOT, env=env, capture_output=True, text=True, check=False)
        if proc.returncode == 0 and os.path.isfile(result_path):
            try:
                return _upgrade_state_load(result_path)
            except Exception:   # noqa: BLE001 — an unreadable result reads as an incomplete run
                pass
        detail = (proc.stderr or "").strip().splitlines()
        tail_note = f" ({detail[-1]})" if detail else ""
        return {"applied": True, "notes": [f"(the update was staged but could not be completed{tail_note})"],
                "reason": ("The update was applied to the working copy but could not be finished, so it was "
                           "NOT opened for review and nothing was merged. Run the update again with "
                           "--confirm to finish it, or ask me to undo the update's changes.")}
    finally:
        shutil.rmtree(work, ignore_errors=True)


_WIRE_KIND_LABELS = {
    "hook": "an automatic engine action",
    "codex-hook": "an automatic engine action (Codex)",
    "mcp": "a connected engine tool",
    "codex-mcp": "a connected engine tool (Codex)",
    # NOT "a data folder": a gitignore wire only controls whether an engine housekeeping folder is tracked by
    # version control — turning it off deletes NO data, and "data folder" would read to a non-engineer as
    # losing their data on the consent-critical data-safety axis (usability review).
    "gitignore": "an internal engine housekeeping rule",
    "permission": "an engine permission setting",
    "ontology-entry": "an engine knowledge entry",
}


def _describe_wire(w: dict) -> str:
    """One plain-language line for a single settings change the operator can read — WHAT kind of setting
    moves, never the internal seam vocabulary (no 'wire'/'seam'/'matcher'). A light identifying hint is
    added only where it reads plainly: a connected tool's name, or the housekeeping rule's target. Robust to
    a malformed (non-dict) wire from a tampered release — the preview must never crash the operator's check."""
    if not isinstance(w, dict):
        return "an engine setting"
    kind = w.get("type")
    label = _WIRE_KIND_LABELS.get(kind, "an engine setting")
    if kind in ("mcp", "codex-mcp") and w.get("name"):
        return f"{label} ({w['name']})"
    if kind == "gitignore":
        first = next((ln for ln in (w.get("lines") or []) if ln), None)
        if first:
            return f"{label} ({first})"
    return label


def _below_floor_refusal(deployed_release: str | None, release_tree: str) -> str | None:
    """The clean-upgrade floor preflight (#599 Slice 4). Returns a plain refusal reason when the DEPLOYED engine
    is OLDER than the target release's recorded `min_upgradeable_from`, else None (proceed). Below the floor the
    deployed engine's own already-shipped upgrade code predates the reconcile, so an automatic update cannot fully
    tidy files renamed/removed since then — it would stall without opening a pull request. So refuse cleanly here,
    pre-overlay, and route to the undo + staying on the current version. Fails OPEN (proceed) on anything that must
    not block a legitimate update: an absent/unreadable target engine.json, a target that declares no floor, or a
    deployed version that is absent, unparseable, or the 0.0.0-dev construction sentinel — a bad string never
    coerces to 'below floor' (validate._ver_tuple would silently map it low). Single-homed: called from both
    upgrade() and plan_upgrade() so the compare-and-refuse and its operator copy cannot drift."""
    mf = os.path.join(release_tree, ".engine", "engine.json")
    if not os.path.isfile(mf):
        return None
    try:
        floor = (validate.load_json(mf) or {}).get("min_upgradeable_from")
    except Exception:   # noqa: BLE001 — an unreadable target manifest never blocks; other gates handle it
        return None
    if not isinstance(floor, str) or not floor:   # absent, or a JSON-valid-but-mistyped floor -> proceed
        return None
    dep = str(deployed_release or "").strip()     # coerce: a non-string deployed version must never crash here
    m = re.match(r"^(\d+\.\d+\.\d+)", dep)
    if not m or m.group(1) == "0.0.0":            # absent, unparseable, or the dev/construction build -> proceed
        return None
    if validate._ver_tuple(dep) >= validate._ver_tuple(floor):
        return None
    return (f"This engine (release {dep}) is older than the oldest release that can update cleanly to this one "
            f"({floor}). An automatic update from a version this old can't fully tidy up the files that were "
            f"renamed or removed since then, so it would stop without opening a pull request. The engine is "
            f"unchanged — stay on {dep} for now; an automatic clean update from a release this old isn't "
            f"available. (If a previous update stopped half-applied, ask me to undo it.)")


def plan_upgrade(ref: str | None = None, release_tree: str | None = None,
                 available: str | None = None, target_ref: str | None = None) -> dict:
    """READ-ONLY upgrade impact preview: what an update WOULD change — the engine files it replaces or adds,
    the settings it turns on/off/updates, and the stored-data or config changes it would make — computed
    WITHOUT applying anything (no overlay, no wiring, no migration, no manifest bump). It mirrors the reads
    `upgrade()`'s parent phase does and none of its writes.

    Composes the SAME pure blocks the apply uses so the preview cannot drift from what the apply does:
    `_overlay_copy_map` (the overlay's own file membership, guarded by the SAME `_within_root` containment
    wall — a tampered release can never make the preview enumerate host paths), `_wiring_delta` (the shared
    removal rule, declaration-only so a release's new seam vocabulary is never executed — #594), and
    `select_migrations` (the same selector the apply pre-flights with).

    Fixture-testable offline: inject `release_tree` (a local extracted release) and the network is never
    touched — pass `target_ref`/`available` for the version line. On the real path it resolves the latest
    release ref (unless a concrete one is named or passed), returns UP-TO-DATE before any download, and only
    then fetches the tree read-only into a temp dir it always removes. Degrades plainly (never raises) on a
    missing release or an unreachable home. Returns a flat dict the CLI renders in plain language."""
    out = {"refused": False, "reason": None, "status": None, "current": None, "available": None,
           "target_ref": None, "named_ref": ref if (ref and ref != "latest") else None,
           "from_versions": {}, "target_versions": {},
           "files": {"replaced": [], "added": []},
           "wires": {"added": [], "removed": [], "updated": []},
           "migrations": [], "retired_capabilities": [], "backed_up": None}
    tmp = None
    try:
        engine = module_coherence.load_engine_manifest() or {"packages": {}}
        from_versions = dict(engine.get("packages") or {})
        present_ids = sorted(from_versions)
        out["from_versions"] = from_versions
        out["current"] = engine.get("engine_release")
        if not present_ids:
            return {**out, "refused": True, "reason": "There are no installed modules to update."}
        injected = release_tree is not None
        named = out["named_ref"] is not None
        if injected:
            target_ref = target_ref or ref or "latest"
        else:
            home = _home_repository()
            if not home:
                out["status"] = "no-home"
                out["reason"] = ("This engine has no update home recorded, so I can't check for updates. Tell "
                                 "me the repository your engine updates from and I'll record it, then check "
                                 "again.")
                return out
            try:
                target_ref = target_ref or _resolve_release_ref(ref, repo=home)   # concrete ref -> no network
            except Exception as exc:   # noqa: BLE001 — offline / no published release -> degrade, never crash
                return _preview_degrade(out, home, exc, target=ref or "latest")
        out["target_ref"] = target_ref
        out["available"] = available or target_ref
        # UP-TO-DATE before any download: an unnamed check whose latest is not newer needs no fetch.
        if not named and validate._ver_tuple(out["available"]) <= validate._ver_tuple(out["current"] or "0"):
            out["status"] = "up-to-date"
            return out
        if not injected:
            tmp = tempfile.mkdtemp(prefix="engine-preview-")
            try:
                release_tree = _fetch_release_tree(target_ref, tmp, repo=home)
            except Exception as exc:   # noqa: BLE001
                return _preview_degrade(out, home, exc, target=target_ref)
        # FLOOR PREFLIGHT (#599 Slice 4): if this engine is below the target's clean-upgrade floor, say so in the
        # preview too — an update from a version this old would refuse, so the operator learns it before --confirm.
        below = _below_floor_refusal(out["current"], release_tree)
        if below:
            return {**out, "refused": True, "status": "below-floor", "reason": below}
        # Read the release's manifests + capture the installed ones — the SAME reads upgrade() does, no writes.
        candidates = {}
        for mid in present_ids:
            man_src = os.path.join(release_tree, ".engine", "modules", mid, "manifest.json")
            if not os.path.isfile(man_src):
                return {**out, "refused": True, "status": "missing-module",
                        "reason": f"The update at {target_ref} does not contain the installed module '{mid}', "
                                  f"so it can't be previewed and nothing was changed."}
            candidates[mid] = validate.load_json(man_src)
            out["target_versions"][mid] = candidates[mid].get("version")
        old_by_id = {}
        for mid in present_ids:
            cur = os.path.join(_modules_dir(mid), "manifest.json")
            old_by_id[mid] = validate.load_json(cur) if os.path.isfile(cur) else {}
        # FILES — the overlay's OWN membership function (drift-proof vs the apply), guarded by the SAME
        # containment wall the apply uses (module_manager `_overlay_engine_code`): a tampered release must
        # never make the read-only preview enumerate or probe paths outside the engine.
        to_copy = _overlay_copy_map(release_tree, candidates)
        escapes = sorted(rel for rel in to_copy if not _within_root(rel))
        if escapes:
            shown = ", ".join(escapes[:3]) + ("…" if len(escapes) > 3 else "")
            return {**out, "refused": True, "status": "unsafe-release",
                    "reason": f"Stopped the update check: the update at {target_ref} described files outside "
                              f"the engine ({shown}), so nothing further was read. This can mean the release "
                              f"is not one to trust — check your engine's update home."}
        replaced, added_files = [], []
        for rel in sorted(to_copy):
            (replaced if os.path.exists(os.path.join(validate.ROOT, rel)) else added_files).append(rel)
        out["files"] = {"replaced": replaced, "added": added_files}
        # SETTINGS (wiring) — the shared identity delta, so the preview reports the apply's own reversals.
        out["wires"] = _wiring_delta(old_by_id, candidates)
        # STORED-DATA / CONFIG changes — the same pure selector the apply pre-flights with.
        selected = select_migrations(from_versions, out["target_versions"], list(candidates.values()))
        out["migrations"] = [{"module_id": s.get("module_id"), "version": s.get("version"),
                              "description": s.get("description"), "kind": s.get("kind")} for s in selected]
        # Capability retirements — the same range selector, from the SAME present-manifest set, independent of
        # whether any migration was selected (a retirement can ship with no migration). Preview mirrors apply.
        out["retired_capabilities"] = select_retired_capabilities(
            from_versions, out["target_versions"], list(candidates.values()))
        if any(s.get("kind") == "data" for s in selected):
            out["backed_up"] = _resolve_backup_seam(None) is not None   # engine-wide readiness probe; no write
        out["status"] = "update-available"
        return out
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


def _preview_degrade(out: dict, home: str, exc: BaseException, target: str) -> dict:
    """Map a release-resolve/fetch failure in the read-only preview to a plain degrade dict (never raises):
    a missing/renamed home names it and asks the operator to check; a transport failure degrades to 'the
    engine is unchanged and still working'. Mirrors upgrade()'s three-state resolution, preview-worded."""
    if _release_is_missing(exc):
        return {**out, "status": "missing-release",
                "reason": (f"Couldn't find a release to update to at your engine's update home, {home} "
                           f"(looked for '{target}'). That home may have no published releases yet, or it may "
                           f"have been renamed or removed. The engine is unchanged.")}
    return {**out, "status": "unreachable",
            "reason": (f"Couldn't reach your engine's update home ({home}) to check for updates — the network "
                       f"may be down right now. The engine is unchanged and still working.")}


def upgrade_preview(ref: str | None = None) -> dict:
    """Read-only pre-flight for the `upgrade` command's preview-by-default surface (the #594 footgun close):
    mutate NOTHING. First the coherence pre-check — a half-applied earlier update leaves engine.json bumped
    but the tree inconsistent, so a version-only check would wrongly read 'up to date'; then the full impact
    preview via `plan_upgrade` (current vs available version, and — when an update is available or a version
    is named — the files, settings, and stored-data changes it would make). Degrades plainly when no home is
    recorded or the home is unreachable, and never raises (a preview must not crash the operator's check)."""
    current = (module_coherence.load_engine_manifest() or {}).get("engine_release")
    named = ref if (ref and ref != "latest") else None
    hard = [f for f in module_coherence.check_coherence() if f.get("severity") == "hard"]
    if hard:
        # A hard coherence finding means the tree is inconsistent — most often a stalled update, but it can
        # also be an interrupted add/remove. State the SYMPTOM, don't assert "a previous update" as the cause.
        return {"status": "inconsistent", "coherent": False, "hard_findings": len(hard),
                "current": current, "named_ref": named, "reason":
                ("Your engine has a consistency problem — something a recent change (an update, or adding or "
                 "removing a module) left unfinished. You can run `upgrade --confirm` to try to finish an "
                 "update, or undo it with `rollback` (type `/engine-upgrade` and choose to undo) — undoing "
                 "saves a recovery point of your current state first.")}
    try:
        plan = plan_upgrade(ref)
    except Exception as exc:   # noqa: BLE001 — a preview must never crash the operator's update check
        return {"status": "unreachable", "current": current, "named_ref": named,
                "reason": f"Couldn't complete the update check — the engine is unchanged and still working. ({exc})"}
    plan["coherent"], plan["hard_findings"] = True, 0
    return plan


def _display_ver(v):
    """Show versions without the release-tag `v` prefix so the current (bare) and available (v-tagged)
    forms read as the same scheme to the operator."""
    return v[1:] if isinstance(v, str) and v.startswith("v") else (v or "unknown")


def _render_upgrade_preview(p: dict) -> None:
    """Plain-language render of the update preview (bare `upgrade`, no --confirm) — what an update WOULD
    change, in the operator's own terms. Changes nothing."""
    if p.get("reason"):     # inconsistent / no-home / unreachable / missing / unsafe all carry a reason
        print(p["reason"])
        return
    current = _display_ver(p.get("current"))
    if p.get("status") == "up-to-date":
        print(f"Your engine is up to date (version {current}). Nothing to update.")
        return
    named = p.get("named_ref")
    target = _display_ver(p.get("target_ref") or p.get("available"))
    if named:
        print(f"You're on version {current}. Here's what updating to {target} would change:")
    else:
        print(f"An update is available: you're on {current}, and {target} is published. Here's what "
              f"updating would change:")
    files = p.get("files") or {}
    nrep, nadd = len(files.get("replaced") or []), len(files.get("added") or [])
    if nrep or nadd:
        parts = ([f"{nrep} engine file{'s' if nrep != 1 else ''} updated"] if nrep else [])
        parts += ([f"{nadd} new file{'s' if nadd != 1 else ''}"] if nadd else [])
        print(f"  Files: {', '.join(parts)} — your settings and saved data are kept.")
        # These aren't in the file overlay above (they are kept-merged / re-rendered, not overwritten), so an
        # apply also touches them — name them here so they aren't a surprise in the pull request's diff.
        print("  It also refreshes the engine's own block in your CLAUDE.md and the engine-file review list — "
              "your own content is kept.")
    w = p.get("wires") or {}
    for verb, items in (("Turns on", w.get("added")), ("Updates", w.get("updated")),
                        ("Turns off", w.get("removed"))):
        for _mid, wire in (items or []):
            print(f"  {verb}: {_describe_wire(wire)}")
    migs = p.get("migrations") or []
    for m in migs:
        what = ("stored data" if m.get("kind") == "data"
                else "a setting" if m.get("kind") == "config" else "an engine record")
        print(f"  Changes {what}: {m.get('description') or m.get('module_id')}")
    retired = p.get("retired_capabilities") or []
    for r in retired:
        print(f"  Removes a capability: {_retired_capability_text(r.get('description'))}")
    if any(m.get("kind") == "data" for m in migs):
        if p.get("backed_up") is True:
            print("  Your stored data is backed up before any data change.")
        elif p.get("backed_up") is False:
            print("  Note: a stored-data change needs a backup set up first — ask me to set one up before "
                  "applying, or the update refuses that step and changes nothing.")
    if not (nrep or nadd or any(w.get(k) for k in ("added", "updated", "removed")) or migs or retired):
        print("  No file or settings changes — a version bump only.")
    tail = f" (or run `upgrade --confirm{(' ' + named) if named else ''}`)"
    print(f"\nThis only checked your engine — nothing changed. To apply, type `/engine-upgrade` and confirm"
          f"{tail}; it arrives as a pull request you review.")


_UPGRADE_USAGE = ("usage: module_manager.py upgrade [ref] [--confirm] [--json]\n"
                  "  Without --confirm it PREVIEWS only — checks for an update and changes nothing.\n"
                  "  With --confirm it applies the update and opens it as a reviewed pull request.\n"
                  "  [ref] optionally names a version; the default is the latest published release.")


def upgrade(ref: str | None = None, release_tree: str | None = None, opener=None, backup=None) -> dict:
    """Upgrade the whole engine vX -> vY. Steps: fetch the tagged
    release, overlay engine code and re-render the CODEOWNERS ownership wall for the new release's engine
    files (operator config + gitignored data preserved), re-sync the tool-runtime, run migrations in
    dependency order, run coherence, and land the change as a reviewed pull request.

    Phase 1 (parent, THIS interpreter — version-agnostic work): fetch, capture the pre-overlay manifests,
    pre-flight the backup guard, overlay the engine code, re-sync the tool-runtime. Phase 2 (the
    version-sensitive TAIL — wiring, seams, migrations, manifest bump, coherence, PR) MUST run as the
    freshly-overlaid code (issue #594), so on every real path it runs in a fresh child interpreter
    (`_spawn_upgrade_tail`); only a fully-injected test/demo caller runs it in-process (`_upgrade_tail`).

    Injectable boundaries (so tests + the demo run the REAL overlay/wiring/coherence and never touch the
    network, rebuild a venv, or open a real PR): `release_tree` injects a local extracted release AND marks
    a practice run (the real `uv sync` is skipped); `opener` injects the git+PR boundary; `backup` injects
    the migration backup seam (None = memory if installed, else none -> data migrations refuse). The tail
    runs in-process ONLY when a release AND a callable are injected together (`in_process`); a real caller
    that happens to pass only `backup=` still runs the child (so it can never run the stale in-process tail).
    Returns a structured result the CLI renders in plain language. Refuses cleanly (nothing applied) on an
    unreachable release, a containment escape, or a data migration with no backup seam (the pre-flight).
    Degrades to the current version on an unreachable release. The change lands ONLY as a reviewed pull
    request, so an abort at any step leaves it UN-MERGED — no half-state is ever the operating baseline; the
    manifest bump runs AFTER migrations (in the tail) so an early abort leaves nothing half-recorded, and a
    re-run with --confirm completes it. The engine does not attempt in-place rollback."""
    injected_release = release_tree is not None                       # captured before the fetch reassigns it
    in_process = injected_release and (opener is not None or backup is not None)   # test/demo full-injection
    practice = injected_release and not in_process                    # local release, no callables ⇒ child, no resync/PR
    result = {"refused": False, "applied": False, "reason": None, "from": None, "to": None,
              "copied": [], "wiring": [], "synced": None, "migrations": {"ran": [], "refused": []},
              "retired_capabilities": [], "findings": [], "pr": None, "notes": [], "codeowners": None,
              "claude_floor": None, "agents_floor": None}
    tmp = None
    try:
        engine = module_coherence.load_engine_manifest() or {"packages": {}}
        from_versions = dict(engine.get("packages") or {})
        present_ids = sorted(from_versions)
        result["from"] = from_versions
        target_ref = ref or "latest"
        # (1) FETCH the tagged release (reuse the release-fetch boundary + its plain-failure handler; degrade to the current version).
        # On the real path, resolve None/"latest" to a CONCRETE tag FIRST, so the engine fetches, runs, and
        # records a pinned ref — never a moving one (R7). The injected path passes a concrete ref already.
        if release_tree is None:
            if not present_ids:
                return {**result, "refused": True, "reason": "There are no installed modules to update."}
            # Resolve the engine's HOME from the manifest and fetch the release FROM THE HOME, never from
            # this repo's own origin (#367). Absent home -> refuse with a remedy (three-state).
            home = _home_repository()
            if not home:
                return {**result, "refused": True,
                        "reason": "This engine has no update home recorded, so it can't check for updates. "
                                  "Tell me the repository your engine updates from (for example your-org/your-engine) and I'll "
                                  "record it, then you can update again. The engine is unchanged."}
            tmp = tempfile.mkdtemp(prefix="engine-upgrade-")
            try:
                target_ref = _resolve_release_ref(ref, repo=home)   # None/"latest" -> concrete latest tag
                release_tree = _fetch_release_tree(target_ref, tmp, repo=home)
            except Exception as exc:
                if _release_is_missing(exc):   # recorded home, but no such release/repo -> refuse, NAME it
                    return {**result, "refused": True,
                            "reason": f"Couldn't find a release to update to at your engine's update home, "
                                      f"{home} (looked for '{ref or 'latest'}'). That home may have no "
                                      f"published releases yet, or it may have been renamed or removed. The "
                                      f"engine is unchanged. If the home is wrong, update the recorded home "
                                      f"and try again."}
                return {**result, "refused": True,   # transport/offline -> DEGRADE to the current version
                        "reason": f"Couldn't reach your engine's update home, {home}, to check for updates — "
                                  f"the network may be down, or the home may not be reachable right now. The "
                                  f"engine is unchanged and still working. ({exc})"}
        # read target versions + capture the CURRENTLY-installed manifests (for wiring deltas) BEFORE the
        # overlay overwrites them
        target_versions, old_by_id = {}, {}
        for mid in present_ids:
            man_src = os.path.join(release_tree, ".engine", "modules", mid, "manifest.json")
            if not os.path.isfile(man_src):
                return {**result, "refused": True,
                        "reason": f"The engine release does not contain the installed module '{mid}', so "
                                  f"the update was stopped and nothing was changed."}
            target_versions[mid] = validate.load_json(man_src).get("version")
            cur = os.path.join(_modules_dir(mid), "manifest.json")
            old_by_id[mid] = validate.load_json(cur) if os.path.isfile(cur) else {}
        result["to"] = target_versions
        # FLOOR PREFLIGHT (#599 Slice 4): refuse cleanly BEFORE any overlay if this engine is older than the
        # target's clean-upgrade floor — a version this old can't reconcile cleanly and would stall without a PR.
        below = _below_floor_refusal(engine.get("engine_release"), release_tree)
        if below:
            return {**result, "refused": True, "reason": below}
        # Capture the OLD engine-owned surface NOW — pre-overlay, with THIS (source) version's code: the old
        # `provides` globbed against the pristine deployed tree, UNIONED with the old FOUNDATION_INFRA (a code
        # constant only the pre-overlay process holds). The reconcile delete leg (in the tail) needs it to
        # remove a file the release renamed or dropped — INCLUDING a dropped FOUNDATION_INFRA artifact (a
        # workflow, an issue template) the post-overlay tail could no longer name from the new constant alone
        # (arch-S1). Threaded into the tail state next to `old_by_id`.
        # KNOWN BOUND (tech-integrity review): this reads the ON-DISK manifests, which a PRIOR aborted run may
        # have already overlaid. So if an update is interrupted AFTER the overlay but BEFORE the delete leg
        # removed a rename orphan, a plain re-run recomputes `old_owned` from the overlaid manifests and no
        # longer sees that orphan — the gate then keeps refusing. It fails SAFE (nothing merges); the recourse
        # is the undo, then update again. A self-healing recovery (persisting the pre-overlay set) is not attempted.
        old_owned = sorted(set(module_coherence.engine_owned_paths(module_coherence.discover_manifests())))
        # PRE-FLIGHT the data-migration backup guard BEFORE any overlay (the half-state law): refuse the
        # WHOLE upgrade if a data migration in range has no backup seam — nothing is applied.
        selected = select_migrations(
            from_versions, target_versions,
            [validate.load_json(os.path.join(release_tree, ".engine", "modules", mid, "manifest.json"))
             for mid in present_ids])
        seam = _resolve_backup_seam(backup)
        data_no_seam = sorted({s["module_id"] for s in selected
                               if s.get("kind") == "data" and seam is None})
        if data_no_seam:
            return {**result, "refused": True,
                    "reason": f"This update needs to change stored data for {', '.join(data_no_seam)}, but "
                              f"no data backup is set up yet — and the engine never changes stored data it "
                              f"can't first back up. The engine is unchanged. Ask me to set up a backup, then "
                              f"update again."}
        # (2) OVERLAY engine code (driven off the present set; containment fail-closed). This lands the new
        # release's `.engine/tools/*.py` on disk — but THIS process still holds the pre-upgrade libraries,
        # which is exactly why the version-sensitive tail below runs as a fresh child of the overlaid code.
        try:
            result["copied"], candidates = _overlay_engine_code(release_tree, present_ids)
        except _UpgradeRefused as ur:
            return {**result, "refused": True, "reason": ur.reason}
        # (3) RE-SYNC the tool-runtime BEFORE the tail's child boots (real path only; the injected/practice
        # run has no real venv and skips it). The child imports the just-overlaid tool code, so the runtime
        # must be rebuilt FIRST — provisioning's "rebuild the runtime, then run new code in it". A FAILED
        # re-sync aborts HERE, before any child and before the manifest bump, so the working copy is staged
        # but un-merged and a re-run is clean.
        if injected_release:
            result["synced"] = None
            result["notes"].append("(skipped re-building the tool-runtime — this is a practice run)")
        else:
            result["synced"] = _resync_tool_runtime()
            if not result["synced"]:
                result["applied"] = True
                result["reason"] = ("The update was applied to the working copy but the engine's tools "
                                    "could not be rebuilt from the new version, so it was NOT opened for "
                                    "review and no saved data was changed. Fix the problem and update "
                                    "again, or ask me to undo the update's changes.")
                return result
        # (4) THE VERSION-SENSITIVE TAIL — wiring, seams, migrations, manifest bump, coherence, PR. It MUST
        # run as the freshly-overlaid engine code (else a release's new wire seams silently no-op — #594),
        # so every real path runs it in a child interpreter of the overlaid module_manager; only a
        # fully-injected test/demo caller runs it in-process (a callable cannot cross a process boundary).
        if in_process:
            # The in-process tail is the full-injection test/demo seam only (a real upgrade spawns a child).
            # Its structural gate must be fixture-safe — the real subset's custom/script checks cannot resolve
            # against a throwaway fixture tree (B1); the child path runs the full gate. See _coherence_only_gate.
            tail = _upgrade_tail(
                release_tree=release_tree, target_ref=target_ref, from_versions=from_versions,
                target_versions=target_versions, old_by_id=old_by_id, old_owned=old_owned,
                candidates=candidates, handle=engine.get("handle"), selected=selected, seam=seam,
                practice=practice, opener=opener, gate=_coherence_only_gate)
        else:
            tail = _spawn_upgrade_tail({
                "release_tree": release_tree, "target_ref": target_ref, "from_versions": from_versions,
                "target_versions": target_versions, "present_ids": present_ids, "old_by_id": old_by_id,
                "old_owned": old_owned, "handle": engine.get("handle"), "practice": practice,
                "marker": _UPGRADE_TAIL_MARKER})
        _merge_tail(result, tail)
        return result
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


# ---- clean whole-engine removal ----

def _remove_engine_pr_body(result: dict) -> str:
    """A plain-language body for the whole-engine removal pull request (operator-facing). States what the
    removal does, the safety-rule outcome, and that it is reviewed + reversible."""
    lines = ["This pull request removes the engine from this repository, leaving an operable, "
             "engine-free product.", "", "What this does:",
             "- Deletes the engine's own files (its tools, checks, schemas, and configuration).",
             "- Removes the engine's entries from shared setup files. Anything there that might also be "
             "yours is left in place for you to review and remove if you don't need it."]
    db = result.get("de_bootstrap") or {}
    if db.get("status") == "kept":
        lines.append("- Keeps the safety rule on your main branch, with the engine's own checks removed "
                     "from it.")
    elif db.get("status") == "dropped":
        lines.append("- Removes the safety rule on your main branch entirely (you chose to remove it).")
    elif db.get("status") == "unaugmented":
        lines.append("- Takes the engine's checks — and any force-push/deletion/pull-request protection the "
                     "engine had added — back out of your own branch-protection rule, leaving the rest of "
                     "that rule exactly as it was. (The rule is yours, so it is not removed.)")
    lines += ["", "Reviewed and reversible: reverting this pull request restores the engine's files. The "
              "main-branch safety rule is turned back on by running the engine setup again.", "",
              "Merging this is your review and consent."]
    return "\n".join(lines)


def remove_engine(opener=None, transport=None, choice: str | None = None, announce=None,
                  repo=None, token=None) -> dict:
    """Remove the WHOLE engine cleanly — the
    'separate step' that per-module remove() points a required module toward, leaving an operable,
    engine-free product. The order is what safety demands:
      (1) DE-BOOTSTRAP FIRST (operator-privileged): remove the engine's required checks from its own
          safety rule (keep the floor remainder, or drop the rule per the operator's `choice`). This runs
          BEFORE the deletion pull request, because that PR deletes the engine workflows and a required
          check whose workflow is gone would 'wait forever' and deadlock the PR.
      (2) REVERSE ALL WIRES across every installed module (the engine's entries in shared files), leaving
          honest residue for anything it can't key to the engine alone (a permission the operator may
          also hold — the reversal firewall).
      (3) DELETE every engine file — UNLIKE per-module remove() (which deletes only under .engine/), the
          whole-engine removal also deletes the engine-owned files OUTSIDE .engine/: the foundation
          infrastructure artifacts (the .github/ control-plane files) and the root CLAUDE.md. CODEOWNERS
          loses only the engine block (the operator's own rules are kept; the file is removed iff nothing
          else remains). The .engine/ tree goes wholesale.
      (4) LAND the deletions as a reviewed pull request via the injectable opener (reuse _open_upgrade_pr).

    Reviewed + reversible: reverting the pull request restores the files; the safety rule is re-created by
    re-running the engine setup (de_bootstrap and bootstrap.apply are the reversal pair, both idempotent).
    FIXTURE-DEMOED — never run on the construction repo (it would delete the engine being built). The four
    boundaries (the de-bootstrap GitHub API, the git/PR open, the real working tree, the operator's real
    keep/drop choice) are injected/faked so tests + the demo run the REAL reversal / delete-set / de-
    bootstrap-decision logic; 'works on the fixture ⇒ works for a real adopter' is the inductive gap."""
    injected = opener is not None or transport is not None
    say = announce if announce is not None else (lambda text: print(text))
    result = {"de_bootstrap": None, "reversed": [], "left_in_place": [], "deleted": [],
              "pr": None, "reversal_note": None, "refused": False, "reason": None, "notes": []}
    manifests = module_coherence.discover_manifests()

    # (1) DE-BOOTSTRAP FIRST — drop the engine required checks so the deletion PR can't deadlock.
    import boot  # lazy: the shared GitHub-context helpers (matches _fetch_release_tree / _open_upgrade_pr)
    slug = repo or boot.repo_slug()
    tok = token if token is not None else boot.gh_token()
    cp = bootstrap.ControlPlane(slug or "", tok or "", transport=transport)
    # The control-plane marker the arrival recorded — whether the engine created its OWN ruleset or AUGMENTED
    # a pre-existing PRODUCT one, and the exact pieces it added — so de-bootstrap reverses precisely that and
    # nothing of the operator's. Absent on an older install or when none was recorded; de_bootstrap then falls
    # back to a bounded, name-only strip that still never deletes a product rule.
    marker = (module_coherence.load_engine_manifest() or {}).get("control_plane")
    try:
        result["de_bootstrap"] = cp.de_bootstrap(choice=choice, marker=marker, announce=say)
    except bootstrap.BootstrapError as exc:
        return {**result, "refused": True,
                "reason": f"Couldn't reach GitHub to remove the engine's branch protection ({exc}); "
                          f"nothing was changed. Try again when you're back online."}

    # (2) REVERSE ALL WIRES across every module + disclose honest permission residue.
    for _path, m in manifests:
        for f in wiring.reverse_all(m.get("wires") or []):
            result["reversed"].append(validate.fmt(f))
        result["left_in_place"].extend(_permission_residue(m))

    # (3) DELETE the engine file set. Compute it BEFORE any deletion (the live globs need the files).
    co_rel = ".github/CODEOWNERS"
    foundation = module_coherence.foundation_infra_paths()
    provides = set(module_coherence.provides_claims(manifests).keys())
    # engine-owned files OUTSIDE .engine/: provides-claimed (e.g. .claude/*/.gitkeep) + the non-.engine
    # foundation members (the .github/ artifacts), minus the three SHARED files handled specially below —
    # CODEOWNERS, root CLAUDE.md, and root .gitignore — which carry the engine as a keyed fenced block, so
    # they are block-reversed (operator content kept) rather than deleted wholesale.
    outside = sorted({r for r in (provides | set(foundation))
                      if not r.startswith(".engine/") and r not in (co_rel, _ROOT_CLAUDE_REL,
                                                                    _ROOT_AGENTS_REL, _GITIGNORE_REL)})
    deleted = []
    for rel in outside:
        p = os.path.join(validate.ROOT, rel)
        if os.path.isfile(p):
            try:
                os.remove(p)
                deleted.append(rel)
            except OSError as exc:
                result["left_in_place"].append(f"Could not delete {rel} ({exc}); remove it by hand.")
    # CODEOWNERS: remove ONLY the engine block; delete the file iff nothing but whitespace remains, else
    # keep the operator's own rules (the engine never clobbers operator content in a shared file).
    co_path = os.path.join(validate.ROOT, co_rel)
    if os.path.isfile(co_path):
        text = validate.read(co_path)
        remainder = wiring.fence_reverse(text, wiring.CODEOWNERS_FENCE)
        if remainder.strip() == "":
            os.remove(co_path)
            deleted.append(co_rel)
        elif remainder != text:
            with open(co_path, "w", encoding="utf-8") as fh:
                fh.write(remainder)
            deleted.append(f"{co_rel} (engine block removed; your own rules kept)")
    # Root CLAUDE.md: the SAME block-reversal as CODEOWNERS — remove only the engine `floor` fence and
    # delete the file iff nothing but whitespace remains (an all-engine greenfield CLAUDE.md), else keep the
    # operator's own content (a brownfield CLAUDE.md). Wrapped so a malformed local fence degrades (leaves
    # the file untouched) rather than crashing the uninstall in front of a non-engineer.
    claude_path = os.path.join(validate.ROOT, _ROOT_CLAUDE_REL)
    if os.path.isfile(claude_path):
        text = validate.read(claude_path)
        try:
            remainder = wiring.fence_reverse(text, _FLOOR_FENCE, style=wiring.MD_FENCE)
        except wiring.WiringError as exc:
            remainder = text
            result["left_in_place"].append(
                f"Left {_ROOT_CLAUDE_REL} as it is — its engine section looked damaged ({exc}).")
        if remainder.strip() == "":
            os.remove(claude_path)
            deleted.append(_ROOT_CLAUDE_REL)
        elif remainder != text:
            with open(claude_path, "w", encoding="utf-8") as fh:
                fh.write(remainder)
            deleted.append(f"{_ROOT_CLAUDE_REL} (engine block removed; your own content kept)")
    # Root AGENTS.md: the SAME block-reversal as CLAUDE.md — the Codex floor pair shares the keyed model.
    agents_path = os.path.join(validate.ROOT, _ROOT_AGENTS_REL)
    if os.path.isfile(agents_path):
        text = validate.read(agents_path)
        try:
            remainder = wiring.fence_reverse(text, _FLOOR_FENCE, style=wiring.MD_FENCE)
        except wiring.WiringError as exc:
            remainder = text
            result["left_in_place"].append(
                f"Left {_ROOT_AGENTS_REL} as it is — its engine section looked damaged ({exc}).")
        if remainder.strip() == "":
            os.remove(agents_path)
            deleted.append(_ROOT_AGENTS_REL)
        elif remainder != text:
            with open(agents_path, "w", encoding="utf-8") as fh:
                fh.write(remainder)
            deleted.append(f"{_ROOT_AGENTS_REL} (engine block removed; your own content kept)")
    # Root .gitignore: the SAME block-reversal — remove only the engine `foundation-ignores` fence and keep
    # the operator's own ignore lines (delete the file only if nothing but whitespace remains, which never
    # happens in practice — the generic dev-ignores survive). The module `gitignore` fences were already
    # reversed per-module in step (1) of each removal, so this leaves an engine-free .gitignore. Wrapped so a
    # malformed hand-edited fence degrades (file untouched) rather than crashing the uninstall — .gitignore
    # is the file operators edit most, so this fail-safe matters more here than for CODEOWNERS.
    gi_path = os.path.join(validate.ROOT, _GITIGNORE_REL)
    if os.path.isfile(gi_path):
        text = validate.read(gi_path)
        try:
            remainder = wiring.fence_reverse(text, wiring.FOUNDATION_IGNORES_FENCE)
        except wiring.WiringError as exc:
            remainder = text
            result["left_in_place"].append(
                f"Left {_GITIGNORE_REL} as it is — its engine section looked damaged ({exc}).")
        if remainder.strip() == "":
            os.remove(gi_path)
            deleted.append(_GITIGNORE_REL)
        elif remainder != text:
            with open(gi_path, "w", encoding="utf-8") as fh:
                fh.write(remainder)
            deleted.append(f"{_GITIGNORE_REL} (engine block removed; your own lines kept)")
    # the whole .engine/ tree (tools, checks, schemas, manifests, generated maps — everything). The
    # running tool keeps executing from memory, so the source being gone on disk before the opener stages
    # it (git add -A) is safe; any process needing .engine again would be a fresh process.
    if os.path.isdir(validate.ENGINE_DIR):
        shutil.rmtree(validate.ENGINE_DIR)
        deleted.append(".engine/")
    result["deleted"] = sorted(deleted)

    # (4) LAND the deletions as a reviewed pull request (reuse the upgrade opener; the opener's `git add
    #     -A` stages the deletions + the wire reversals). INJECTED in tests + the demo; the real path runs
    #     only on a deployed repo, never the construction repo. The opener should run on an otherwise-clean
    #     tree so the removal PR carries only the removal.
    body = _remove_engine_pr_body(result)
    open_fn = opener or (None if injected else _open_upgrade_pr)
    if open_fn is None:
        result["notes"].append("(practice run — the removal pull request was not opened)")
    else:
        try:
            result["pr"] = open_fn(branch="engine-remove", title="Removal: remove the engine", body=body)
        except Exception as exc:  # noqa: BLE001 — staged but not opened; surfaced, never a traceback
            result["notes"].append(f"(removal is staged but the pull request could not be opened: {exc})")

    # The sharpened reversal disclosure (names the unprotected window + the drop case explicitly).
    db = result["de_bootstrap"] or {}
    if db.get("status") == "dropped":
        protection_state = ("off — you removed the safety rule, so re-running the engine setup re-creates "
                            "it from scratch")
    elif db.get("status") == "kept":
        protection_state = ("still in place but without the engine's checks; re-running the engine setup "
                            "restores them")
    else:
        protection_state = "unchanged"
    result["reversal_note"] = (
        "To undo this removal: revert the pull request to bring the engine's files back. Until you then "
        f"run the engine setup again, your main branch's safety rule is {protection_state}.")
    return result


# ---- CLI rendering ----------------------------------------------------------------------------

def _render_remove(result: dict) -> None:
    mid = result.get("module_id")
    if result.get("refused"):
        print(f"Did not remove '{mid}': {result['reason']}")
        return
    print(f"Removed the module '{mid}'.")
    for line in result.get("reversed", []):
        print("  - " + line)
    for rel in result.get("deleted", []):
        print(f"  - deleted {rel}")
    if result.get("groups_after") is not None:
        print(f"  - tool-runtime dependency groups are now: {result['groups_after'] or '(none)'}")
    if result.get("left_in_place"):
        print("\nLeft in place (on purpose):")
        for line in result["left_in_place"]:
            print("  - " + line)
    hard = [f for f in result.get("findings", []) if f.get("severity") == "hard"]
    if hard:
        print(f"\nAfter removing '{mid}', a problem remains:")
        for f in hard:
            print("  - " + validate.fmt(f))
    else:
        print("\nThe remaining modules are consistent.")
    # The structural-not-fitness warrant, single-homed in module_coherence and matching the standalone
    # CLI's _print_report (printed on EVERY non-refused report) — so "consistent" is never misread as
    # "the module works" on the higher-traffic lifecycle renders (#400 F5).
    print(module_coherence.COHERENCE_WARRANT)


def _render_add(result: dict) -> None:
    mid = result.get("module_id")
    if result.get("refused"):
        print(f"Did not add '{mid}': {result['reason']}")
        return
    print(f"Added the module '{mid}' (version {result.get('version')}).")
    for rel in result.get("copied", []):
        print(f"  - added {rel}")
    for line in result.get("applied_wires", []):
        print("  - " + line)
    if result.get("groups_after") is not None:
        print(f"  - tool-runtime dependency groups are now: {result['groups_after'] or '(none)'}")
    for line in result.get("notes", []):
        print("  - " + line)
    hard = [f for f in result.get("findings", []) if f.get("severity") == "hard"]
    if hard:
        print(f"\nAfter adding '{mid}', a problem remains:")
        for f in hard:
            print("  - " + validate.fmt(f))
    else:
        print("\nThe installed modules are consistent.")
    print(module_coherence.COHERENCE_WARRANT)  # structural-not-fitness warrant (#400 F5) — see _render_remove


def _render_upgrade(result: dict) -> None:
    if result.get("refused"):
        print(f"Did not update the engine: {result['reason']}")
        return
    frm, to = result.get("from") or {}, result.get("to") or {}
    moved = [f"{mid} {frm.get(mid, '—')} -> {to.get(mid)}" for mid in sorted(to)]
    print("Updated the engine" + (f": {'; '.join(moved)}." if moved else "."))
    copied = result.get("copied", [])
    for rel in copied[:8]:
        print(f"  - replaced {rel}")
    if len(copied) > 8:
        print(f"  - … and {len(copied) - 8} more engine file(s)")
    co = result.get("codeowners")
    if co == "written":
        print("  - refreshed the list of engine files that route to you for review "
              "(this version's new files are covered; your own rules untouched)")
    elif co == "degraded":
        print("  - could not refresh the engine-file review list (no account handle on record); "
              "left it unchanged")
    cf = result.get("claude_floor")
    if cf == "merged":
        print("  - updated your project's working guide (the engine's marked block in CLAUDE.md; "
              "your own content kept)")
    elif cf == "degraded":
        print("  - could not update your project's working guide — the engine's marked block in CLAUDE.md "
              "looked damaged; left the file unchanged (check the marker lines and update again)")
    elif cf == "skipped-no-section":
        print("  - did not update your project's working guide — no engine marked block found in CLAUDE.md; "
              "left the file unchanged")
    for r in result.get("migrations", {}).get("ran", []):
        print(f"  - ran update: {r}")
    for r in result.get("migrations", {}).get("refused", []):
        print(f"  - {r}")
    for r in result.get("retired_capabilities", []):
        print(f"  - removed a capability: {_retired_capability_text(r.get('description'))}")
    for line in result.get("notes", []):
        print("  - " + line)
    pr = result.get("pr")
    if pr:
        num = pr.get("number") if isinstance(pr, dict) else None
        print(f"\nOpened a pull request{f' #{num}' if num else ''} for review — merging it is your consent; "
              f"reverting it undoes the update.")
    hard = [f for f in result.get("findings", []) if f.get("severity") == "hard"]
    if hard:
        print(f"\n{result.get('reason') or 'A problem remains:'}")
        for f in hard:
            print("  - " + validate.fmt(f))
    elif result.get("reason"):
        # Applied but NOT completed — a failed re-sync, a refused stored-data update, or a tail that could
        # not finish. Surface the recovery instruction and NEVER claim "consistent": coherence never passed
        # on this path, so "staged and consistent" would be a false all-clear (the higher-traffic gap the
        # child-failure / migration-refuse / resync-fail branches all land in).
        print(f"\n{result['reason']}")
    elif not pr:
        print("\nThe update is staged and consistent.")
    # Reached on every non-refused path — the staged-consistent line, the hard-findings line, AND the
    # PR-opened path (the dominant upgrade case, which prints neither branch above) — so the warrant is
    # never skipped on an upgrade that opens a review PR (#400 F5).
    print(module_coherence.COHERENCE_WARRANT)


def _render_remove_engine(result: dict) -> None:
    if result.get("refused"):
        print(f"Did not remove the engine: {result['reason']}")
        return
    db = result.get("de_bootstrap") or {}
    state = {"kept": "kept your main-branch safety rule (the engine's checks removed from it)",
             "dropped": "removed your main-branch safety rule entirely",
             "no-rule": "found no engine safety rule to remove"}.get(db.get("status"), "")
    print("Removed the engine." + (f" Safety rule: {state}." if state else ""))
    for rel in result.get("deleted", []):
        print(f"  - deleted {rel}")
    for line in result.get("reversed", []):
        print("  - " + line)
    for line in result.get("left_in_place", []):
        print("  - left in place: " + line)
    for line in result.get("notes", []):
        print("  - " + line)
    pr = result.get("pr")
    if pr:
        num = pr.get("number") if isinstance(pr, dict) else None
        print(f"\nOpened a pull request{f' #{num}' if num else ''} with the deletions — merging it is your "
              f"consent; reverting it brings the engine's files back.")
    if result.get("reversal_note"):
        print(f"\n{result['reversal_note']}")


def _status() -> int:
    manifests = module_coherence.discover_manifests()
    print(f"Installed modules ({len(manifests)}):")
    for _p, m in manifests:
        mid = m.get("id")
        deps = sorted((m.get("depends") or {}).keys())
        dependents = sorted(o.get("id") for _q, o in manifests
                            if o.get("id") != mid and mid in (o.get("depends") or {}))
        line = f"  - {mid} ({m.get('status')})"
        if deps:
            line += f"; needs: {', '.join(deps)}"
        if dependents:
            line += f"; needed by: {', '.join(dependents)}"
        print(line)
    try:
        derived = derive_uv_groups(manifests=manifests)
        committed = committed_default_groups()
        synced = derived == committed
        print(f"\nTool-runtime dependency groups: {derived or '(none)'} "
              f"({'in sync' if synced else f'OUT OF SYNC — committed: {committed}'}).")
    except Exception as exc:
        print(f"\nTool-runtime dependency groups: could not read the tool-runtime configuration ({exc}).")
    return 0


# ---- demo (mutation-free, real logic, fixture boundary) ---------------------------------------

@contextlib.contextmanager
def _redirect_root(root: str):
    """Point every ROOT-derived path at a throwaway fixture tree, restore on exit. The wiring-library
    path constants are bound at import, so they are redirected explicitly (the same discipline the
    coherence tests use)."""
    saved = (validate.ROOT, validate.ENGINE_DIR, wiring.SETTINGS_PATH, wiring.MCP_PATH,
             wiring.GITIGNORE_PATH, wiring.CATALOG_PATH,
             wiring.CODEX_HOOKS_PATH, wiring.CODEX_CONFIG_PATH)
    validate.ROOT = root
    validate.ENGINE_DIR = os.path.join(root, ".engine")
    wiring.SETTINGS_PATH = os.path.join(root, ".claude", "settings.json")
    wiring.MCP_PATH = os.path.join(root, ".mcp.json")
    wiring.GITIGNORE_PATH = os.path.join(root, ".gitignore")
    wiring.CATALOG_PATH = os.path.join(root, ".engine", "schemas", "surface-catalog.json")
    wiring.CODEX_HOOKS_PATH = os.path.join(root, ".codex", "hooks.json")
    wiring.CODEX_CONFIG_PATH = os.path.join(root, ".codex", "config.toml")
    try:
        yield
    finally:
        (validate.ROOT, validate.ENGINE_DIR, wiring.SETTINGS_PATH, wiring.MCP_PATH,
         wiring.GITIGNORE_PATH, wiring.CATALOG_PATH,
         wiring.CODEX_HOOKS_PATH, wiring.CODEX_CONFIG_PATH) = saved


def _build_fixture(root: str) -> None:
    """A minimal COHERENT fixture engine: a required `base` module + an optional `optx` module
    (one provided file, one gitignore wire, one declared dependency group). Every .engine/ file is
    claimed or named-infra, so coherence is clean before remove."""
    eng = os.path.join(root, ".engine")
    os.makedirs(os.path.join(eng, "modules", "base"))
    os.makedirs(os.path.join(eng, "modules", "optx"))
    os.makedirs(os.path.join(eng, "tools"))
    os.makedirs(os.path.join(root, ".claude"))
    _write_json(os.path.join(eng, "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.0.0", "status": "required",
                 "provides": {"tool": [".engine/tools/base_tool.py"]}, "depends": {}})
    _write_json(os.path.join(eng, "modules", "optx", "manifest.json"),
                {"id": "optx", "version": "0.0.0", "status": "optional",
                 "provides": {"tool": [".engine/tools/optx_tool.py"]},
                 "wires": [{"type": "gitignore", "key": "optx-cache",
                            "lines": [".engine/optx/.cache/"]},
                           {"type": "permission", "value": "Bash(optx-tool:*)"}],
                 "depends": {}})
    _write_json(os.path.join(eng, "engine.json"),
                {"engine_release": "0.0.0", "packages": {"base": "0.0.0", "optx": "0.0.0"},
                 "identity": "solo"})
    with open(os.path.join(eng, "tools", "base_tool.py"), "w") as fh:
        fh.write("# base\n")
    with open(os.path.join(eng, "tools", "optx_tool.py"), "w") as fh:
        fh.write("# optx\n")
    with open(os.path.join(eng, "uv.lock"), "w") as fh:
        fh.write("")
    with open(os.path.join(eng, "pyproject.toml"), "w") as fh:
        fh.write('[project]\nname = "x"\nversion = "0"\n\n[dependency-groups]\n'
                 'base = ["pkg-a"]\noptx = ["pkg-b"]\n\n[tool.uv]\ndefault-groups = ["base", "optx"]\n')
    for name in (".mcp.json", os.path.join(".claude", "settings.json")):
        with open(os.path.join(root, name), "w") as fh:
            fh.write("{}\n")
    with open(os.path.join(root, ".gitignore"), "w") as fh:
        fh.write("# a foundation plain line\n.engine/.venv/\n")
    # apply optx's declared wires so the forward leg sees them applied (the real appliers)
    wiring.apply_all([{"type": "gitignore", "key": "optx-cache", "lines": [".engine/optx/.cache/"]},
                      {"type": "permission", "value": "Bash(optx-tool:*)"}])


def run_demo() -> bool:
    """The fail-then-pass behavioral demonstration, returning True iff every step behaved. Real
    plan_remove / remove / derive logic runs; only the tree it touches is a throwaway. Part A shows the
    two refusals on the REAL repo (read-only); Part B removes an optional module end-to-end on a
    fixture; Part C shows the idempotent re-run."""
    ok = True
    print("Part A — refusals on your real repository (nothing is changed):")
    core = plan_remove("core")
    print("  remove core            -> " + ("REFUSED: " + core["reason"] if core["refused"] else "NOT refused?!"))
    ok = ok and core["refused"] and "validators-core" in core["reason"]   # reverse-dependency refusal
    vc = plan_remove("validators-core")
    print("  remove validators-core -> " + ("REFUSED: " + vc["reason"] if vc["refused"] else "NOT refused?!"))
    ok = ok and vc["refused"] and "audit-library" in vc["reason"]        # reverse-dependency refusal (audit-library needs it)
    leaf = plan_remove("routine-mode")
    print("  remove routine-mode    -> " + ("REFUSED: " + leaf["reason"] if leaf["refused"] else "NOT refused?!"))
    ok = ok and leaf["refused"] and "required" in leaf["reason"]          # required-foundation refusal (a required leaf)

    print("\nPart B — removing an optional module end-to-end on a throwaway fixture:")
    with tempfile.TemporaryDirectory() as d:
        with _redirect_root(d):
            _build_fixture(d)
            before = [f for f in module_coherence.check_coherence() if f["severity"] == "hard"]
            print("  fixture coherent before removal: " + ("yes" if not before else f"NO: {before}"))
            ok = ok and not before
            res = remove("optx")
            for line in [f"removed '{res['module_id']}'"] + res["reversed"] + \
                    [f"deleted {x}" for x in res["deleted"]] + [f"groups now {res['groups_after']}"]:
                print("    - " + line)
            engine = module_coherence.load_engine_manifest()
            checks = {
                "optx file deleted": not os.path.exists(os.path.join(d, ".engine/tools/optx_tool.py")),
                "optx module folder gone": not os.path.isdir(os.path.join(d, ".engine/modules/optx")),
                "engine.json drops optx": "optx" not in (engine or {}).get("packages", {}),
                "base survives": "base" in (engine or {}).get("packages", {}),
                "groups re-derived to [base]": res["groups_after"] == ["base"],
                "default-groups rewritten": committed_default_groups() == ["base"],
                "coherent after removal": not [f for f in res["findings"] if f["severity"] == "hard"],
            }
            for label, good in checks.items():
                print(f"    [{'ok' if good else 'FAIL'}] {label}")
                ok = ok and good

            print("\nPart C — removing it again is a clean refusal (safe to re-run):")
            again = remove("optx")
            print("    -> " + (again["reason"] if again.get("refused") else "NOT refused?!"))
            ok = ok and again.get("refused")
    print("\n" + ("DEMO PASSED: refusals hold, a real removal reversed cleanly, and a re-run is safe."
                  if ok else "DEMO DID NOT BEHAVE AS EXPECTED — see above."))
    return ok


# ---- add demo (mutation-free, real logic, faked fetch boundary) -------------------------------

def _build_add_fixture(root: str) -> None:
    """A minimal COHERENT live fixture engine for the add demo: just a required `base` module present, the
    tool-runtime pyproject declaring BOTH base's and feat's dependency-groups (so feat's group becomes
    selectable the moment feat is added — the shipped engine declares every module's group, deselected or
    not), default-groups selecting only base."""
    eng = os.path.join(root, ".engine")
    os.makedirs(os.path.join(eng, "modules", "base"))
    os.makedirs(os.path.join(eng, "tools"))
    os.makedirs(os.path.join(root, ".claude"))
    _write_json(os.path.join(eng, "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.0.0", "status": "required",
                 "provides": {"tool": [".engine/tools/base_tool.py"]}, "depends": {}})
    _write_json(os.path.join(eng, "engine.json"),
                {"engine_release": "0.0.0", "packages": {"base": "0.0.0"}, "identity": "solo",
                 "home_repository": "acme/engine-home"})   # the update home the fetch resolves + upgrade preserves
    with open(os.path.join(eng, "tools", "base_tool.py"), "w") as fh:
        fh.write("# base\n")
    with open(os.path.join(eng, "uv.lock"), "w") as fh:
        fh.write("")
    with open(os.path.join(eng, "pyproject.toml"), "w") as fh:
        fh.write('[project]\nname = "x"\nversion = "0"\n\n[dependency-groups]\n'
                 'base = ["pkg-a"]\nfeat = ["pkg-c"]\n\n[tool.uv]\ndefault-groups = ["base"]\n')
    for name in (".mcp.json", os.path.join(".claude", "settings.json")):
        with open(os.path.join(root, name), "w") as fh:
            fh.write("{}\n")
    with open(os.path.join(root, ".gitignore"), "w") as fh:
        fh.write("# a foundation plain line\n.engine/.venv/\n")


def _build_release_tree(root: str) -> str:
    """A throwaway extracted release tree (what _fetch_release_tree would return) holding two addable
    modules: `feat` (optional, depends the present `base`, brings one tool + a gitignore wire) and `needy`
    (optional, depends an ABSENT `ghost`). Returns the tree root (the directory that contains `.engine/`)."""
    eng = os.path.join(root, ".engine")
    os.makedirs(os.path.join(eng, "modules", "feat"))
    os.makedirs(os.path.join(eng, "modules", "needy"))
    os.makedirs(os.path.join(eng, "tools"))
    _write_json(os.path.join(eng, "modules", "feat", "manifest.json"),
                {"id": "feat", "version": "0.1.0", "status": "optional",
                 "provides": {"tool": [".engine/tools/feat_tool.py"]},
                 "wires": [{"type": "gitignore", "key": "feat-cache",
                            "lines": [".engine/feat/.cache/"]}],
                 "depends": {"base": ""}})
    _write_json(os.path.join(eng, "modules", "needy", "manifest.json"),
                {"id": "needy", "version": "0.1.0", "status": "optional",
                 "provides": {"tool": [".engine/tools/needy_tool.py"]}, "depends": {"ghost": ""}})
    with open(os.path.join(eng, "tools", "feat_tool.py"), "w") as fh:
        fh.write("# feat\n")
    with open(os.path.join(eng, "tools", "needy_tool.py"), "w") as fh:
        fh.write("# needy\n")
    return root


def add_demo() -> bool:
    """Fail-then-pass demonstration of `add`, returning True iff every step behaved. Real plan_add / add /
    derive / coherence logic runs against a throwaway fixture; only the release FETCH is faked (an injected
    local release tree — exactly the boundary _fetch_release_tree owns). Honest limit: a real release fetch
    is never exercised in the construction repo (no releases exist), so "works on the fixture ⇒ works for a
    real adopter" is the inductive step the fixture cannot discharge."""
    ok = True
    print("Part D — adding an optional module end-to-end on a throwaway fixture (the release fetch is "
          "faked; the copy / wire / coherence logic is real):")
    with tempfile.TemporaryDirectory() as d:
        live = os.path.join(d, "live")
        os.makedirs(live)
        release = _build_release_tree(os.path.join(d, "release"))
        with _redirect_root(live):
            _build_add_fixture(live)
            before = [f for f in module_coherence.check_coherence() if f["severity"] == "hard"]
            print("  fixture coherent before add: " + ("yes" if not before else f"NO: {before}"))
            ok = ok and not before
            res = add("feat", release_tree=release)
            for line in [f"added '{res.get('module_id')}' v{res.get('version')}"] + \
                    [f"copied {x}" for x in res.get("copied", [])] + \
                    res.get("applied_wires", []) + [f"groups now {res.get('groups_after')}"]:
                print("    - " + line)
            engine = module_coherence.load_engine_manifest()
            checks = {
                "feat tool copied in": os.path.exists(os.path.join(live, ".engine/tools/feat_tool.py")),
                "feat manifest copied in": os.path.isfile(
                    os.path.join(live, ".engine/modules/feat/manifest.json")),
                "engine.json records feat 0.1.0": (engine or {}).get("packages", {}).get("feat") == "0.1.0",
                "base survives": "base" in (engine or {}).get("packages", {}),
                "groups re-derived to [base, feat]": res.get("groups_after") == ["base", "feat"],
                "default-groups rewritten": committed_default_groups() == ["base", "feat"],
                "feat wire applied (gitignore fence present)":
                    "feat-cache" in validate.read(os.path.join(live, ".gitignore")),
                "coherent after add": not [f for f in res.get("findings", []) if f["severity"] == "hard"],
            }
            for label, good in checks.items():
                print(f"    [{'ok' if good else 'FAIL'}] {label}")
                ok = ok and good

            print("\nPart E — adding a module whose dependency is missing is refused (nothing changed):")
            needy = add("needy", release_tree=release)
            print("    -> " + (needy["reason"] if needy.get("refused") else "NOT refused?!"))
            unchanged = (not os.path.exists(os.path.join(live, ".engine/tools/needy_tool.py"))
                         and "needy" not in (module_coherence.load_engine_manifest() or {}).get("packages", {}))
            print(f"    [{'ok' if unchanged else 'FAIL'}] the refused add changed nothing")
            ok = ok and needy.get("refused") and "ghost" in (needy.get("reason") or "") and unchanged

            print("\nPart F — adding a module that is already installed is refused (safe to re-run):")
            again = add("feat", release_tree=release)
            print("    -> " + (again["reason"] if again.get("refused") else "NOT refused?!"))
            ok = ok and again.get("refused")
    print("\n" + ("ADD DEMO PASSED: a module was fetched-and-installed cleanly on the fixture, and the "
                  "missing-dependency and already-installed cases were refused."
                  if ok else "ADD DEMO DID NOT BEHAVE AS EXPECTED — see above."))
    return ok


# ---- upgrade demo (mutation-free, real logic, ALL FOUR boundaries faked) ----------------------

def _build_upgrade_fixture(root: str) -> None:
    """A minimal COHERENT live fixture engine at version 0.0.0: a required `base` module (one tool, no
    migrations yet), the engine manifest recording base 0.0.0 + a `solo` identity (operator config the
    upgrade must preserve), and the foundation code files an overlay replaces."""
    eng = os.path.join(root, ".engine")
    os.makedirs(os.path.join(eng, "modules", "base"))
    os.makedirs(os.path.join(eng, "tools"))
    os.makedirs(os.path.join(root, ".claude"))
    _write_json(os.path.join(eng, "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.0.0", "status": "required",
                 "provides": {"tool": [".engine/tools/base_tool.py"]}, "depends": {}, "migrations": {},
                 "wires": [{"type": "gitignore", "key": "oldcache",
                            "lines": [".engine/base/.oldcache/"]}]})
    _write_json(os.path.join(eng, "engine.json"),
                {"engine_release": "0.0.0", "packages": {"base": "0.0.0"}, "identity": "solo",
                 "home_repository": "acme/engine-home"})   # the update home the fetch resolves + upgrade preserves
    with open(os.path.join(eng, "tools", "base_tool.py"), "w") as fh:
        fh.write("# base v0\n")
    with open(os.path.join(eng, "uv.lock"), "w") as fh:
        fh.write("# lock v0\n")
    with open(os.path.join(eng, "pyproject.toml"), "w") as fh:
        fh.write('[project]\nname = "x"\nversion = "0"\n\n[dependency-groups]\nbase = ["pkg-a"]\n\n'
                 '[tool.uv]\ndefault-groups = ["base"]\n')
    for name in (".mcp.json", os.path.join(".claude", "settings.json")):
        with open(os.path.join(root, name), "w") as fh:
            fh.write("{}\n")
    with open(os.path.join(root, ".gitignore"), "w") as fh:
        fh.write("# foundation\n.engine/.venv/\n")
    # apply vX's declared wire so the upgrade has an OLD wire to REVERSE (the delta's reverse leg)
    wiring.apply_all([{"type": "gitignore", "key": "oldcache", "lines": [".engine/base/.oldcache/"]}])


def _build_upgrade_release(root: str) -> str:
    """A throwaway extracted release tree (what _fetch_release_tree would return) at version 0.2.0: `base`
    bumped, its tool updated, and TWO migrations declared — a `config` transform (0.1.0, runs directly) and
    a `data` transform (0.2.0, backup-first). The migration `.py` files are in `base`'s `provides` (so the
    overlay copies them and the ownership leg claims them); each migrate(context) leaves an observable
    marker under .engine/state/ (claimed by base's state glob). Returns the tree root."""
    eng = os.path.join(root, ".engine")
    os.makedirs(os.path.join(eng, "modules", "base", "migrations"))
    os.makedirs(os.path.join(eng, "tools"))
    _write_json(os.path.join(eng, "modules", "base", "manifest.json"),
                {"id": "base", "version": "0.2.0", "status": "required",
                 "provides": {"tool": [".engine/tools/base_tool.py"],
                              "migration": [".engine/modules/base/migrations/*.py"],
                              "state": [".engine/state/*.json"]},
                 "depends": {},
                 "wires": [{"type": "gitignore", "key": "newcache",
                            "lines": [".engine/base/.newcache/"]}],
                 "migrations": {
                     "0.1.0": {"description": "Tidy a committed settings file for the new layout.",
                               "run": "migrations/config_010.py", "kind": "config"},
                     "0.2.0": {"description": "Reshape the stored data for the new format.",
                               "run": "migrations/data_020.py", "kind": "data"}},
                 "retired_capabilities": {
                     "0.2.0": {"description": "The base tool no longer offers its one-shot cache reset; clear "
                                              "the cache with the standard cleanup instead."}}})
    with open(os.path.join(eng, "tools", "base_tool.py"), "w") as fh:
        fh.write("# base v2 (updated)\n")
    # the migration code runs IN the tool-runtime; it imports validate (module_manager already put the
    # tools dir on sys.path) to find the redirected ROOT — exactly how a real migration locates its store.
    cfg = ("import os, json, validate\n"
           "def migrate(context):\n"
           "    assert context['kind'] == 'config'\n"
           "    p = os.path.join(validate.ROOT, '.engine', 'state', 'config_marker.json')\n"
           "    os.makedirs(os.path.dirname(p), exist_ok=True)\n"
           "    with open(p, 'w') as fh:\n"
           "        json.dump({'ran': 'config', 'to': context['to_version']}, fh)\n")
    data = ("import os, json, validate\n"
            "def migrate(context):\n"
            "    assert context['kind'] == 'data'\n"
            "    handle = context['backup']('recall-ledger', context['engine_version'])\n"
            "    assert handle, 'backup-first: a data migration must snapshot before mutating'\n"
            "    p = os.path.join(validate.ROOT, '.engine', 'state', 'data_marker.json')\n"
            "    os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            "    with open(p, 'w') as fh:\n"
            "        json.dump({'ran': 'data', 'stamp': context['engine_version']}, fh)\n")
    with open(os.path.join(eng, "modules", "base", "migrations", "config_010.py"), "w") as fh:
        fh.write(cfg)
    with open(os.path.join(eng, "modules", "base", "migrations", "data_020.py"), "w") as fh:
        fh.write(data)
    with open(os.path.join(eng, "uv.lock"), "w") as fh:           # foundation code the overlay replaces
        fh.write("# lock v2\n")
    with open(os.path.join(eng, "pyproject.toml"), "w") as fh:
        fh.write('[project]\nname = "x"\nversion = "0"\n\n[dependency-groups]\nbase = ["pkg-a"]\n\n'
                 '[tool.uv]\ndefault-groups = ["base"]\n')
    # The PR template is foundation the overlay delivers (FOUNDATION_CODE), and the upgrade tail's PR-body
    # author reads its consent-preamble blockquote from the LIVE tree (post-overlay). A real release ships it,
    # so the fixture release must too — else the body author finds no template and the update can't open. This
    # blockquote just needs to be a valid `>` block for template_preamble() to extract; the real completeness
    # gate (and its anchor phrases) is verified separately, against the real template, in the unit tests.
    os.makedirs(os.path.join(root, ".github"), exist_ok=True)
    with open(os.path.join(root, ".github", "pull_request_template.md"), "w", encoding="utf-8") as fh:
        fh.write("> A green mechanical check below shows this change conforms to the engine's rules; it does "
                 "not judge whether the change is correct. Your merge is the binding gate. A safety check "
                 "that could not run leaves its area unverified.\n\n## Purpose\n\n<why this change exists>\n")
    # Since #323 the release's root CLAUDE.md/AGENTS.md ARE the fenced adopter floor — the source the keyed-merge
    # reads (its `floor` fence body). The floor body carries a v2 marker so the merge is observable; the AGENTS
    # floor is likewise fenced so a repo with no AGENTS.md yet has it CREATED on upgrade (#599 class 2).
    for rel, body in (
            ("CLAUDE.md", "# Your project runs on an Engine (v2)\n\nProject status block, refreshed in v2.\n"),
            ("AGENTS.md", "# Your project runs on an Engine — Codex floor (v2)\n\nCodex status block, refreshed in v2.\n")):
        lines = body.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
        with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
            # A DECOY line OUTSIDE the floor fence: the keyed-merge reads only the fence BODY (via fence_read),
            # so this release-only content must NEVER travel into an adopter's guide. Asserting its absence is
            # what gives the merge test real bite (a fence_read that grabbed the whole file would leak it).
            fh.write(wiring.fence_apply("", _FLOOR_FENCE, lines, style=wiring.MD_FENCE)
                     + "\n<!-- release-only content outside the floor fence; must never travel -->\n")
    # The committed FIXTURE namespace the copy-only overlay missed (#599 class 3) — delivered by the reconcile.
    os.makedirs(os.path.join(eng, "_fixtures", "probe"), exist_ok=True)
    with open(os.path.join(eng, "_fixtures", "probe", "bad_input.md"), "w", encoding="utf-8") as fh:
        fh.write("a negative-fixture input a hard check bites on\n")
    # The release's OWN retire manifest — the provision() projection input the reconcile reads (self-describing,
    # core-owned, travels). The entries are the first-run-only surface a deployed repo must NOT carry; a repo
    # that never had them stays clean, and any the copy-only overlay resurrects the reconcile removes.
    os.makedirs(os.path.join(eng, "provisioning"), exist_ok=True)
    # Synthetic first-run names (NOT the engine's real retired files — a literal `.engine/tools/instantiator.py`
    # here would read as this traveling module referencing retired code, tripping first-run-reference-closure).
    _write_json(os.path.join(eng, "provisioning", "first-run-assets.json"),
                {"description": "first-run-only assets retired from a deployed repo (fixture)",
                 "files": [".engine/tools/first_run_setup.py"],
                 "directories": [".engine/first-run-scratch"]})
    return root


def upgrade_demo() -> bool:
    """Fail-then-pass demonstration of `upgrade`, returning True iff every step behaved. Real overlay /
    migration runner / coherence logic runs against a throwaway fixture; ALL FOUR side-effect boundaries
    are faked — the release fetch (injected release tree), the tool-runtime rebuild (skipped on a practice
    run), the git/PR open (injected fake opener), and the data backup (injected fake seam). Honest limit:
    none of those four ever runs against a live release in this template repo (which cuts no releases of
    itself), so "works on the fixture ⇒ works for a real adopter" is the inductive step the fixture cannot
    discharge."""
    ok = True
    print("Part G — updating the whole engine on a throwaway fixture. FAKED: the release fetch, the "
          "tool-runtime rebuild, the pull-request open, and the data backup. REAL: the overlay, the "
          "migration runner, and the consistency check. (None of those four ever runs for real here.)")
    pulls = []
    def fake_opener(branch, title, body):
        pulls.append({"branch": branch, "title": title, "body": body})
        return {"number": 0, "title": title}
    snapshots = []
    def fake_backup(store, engine_version, **kw):                # **kw absorbs the migration_id run_migrations binds in
        snapshots.append((store, engine_version))
        return {"store": store, "engine_version": engine_version}

    with tempfile.TemporaryDirectory() as d:
        live = os.path.join(d, "live")
        os.makedirs(live)
        release = _build_upgrade_release(os.path.join(d, "release"))
        with _redirect_root(live):
            _build_upgrade_fixture(live)
            before = [f for f in module_coherence.check_coherence() if f["severity"] == "hard"]
            print("  fixture consistent before update: " + ("yes" if not before else f"NO: {before}"))
            ok = ok and not before

            print("\nPart H — an unreachable release leaves the engine on its current version (it degrades):")
            saved_fetch = globals().get("_fetch_release_tree")
            globals()["_fetch_release_tree"] = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("no such release"))
            try:
                degraded = upgrade(ref="v9.9.9")
            finally:
                globals()["_fetch_release_tree"] = saved_fetch
            still0 = (module_coherence.load_engine_manifest() or {}).get("packages", {}).get("base")
            print("    -> " + (degraded["reason"] if degraded.get("refused") else "NOT refused?!"))
            ok = ok and degraded.get("refused") and still0 == "0.0.0"

            print("\nPart I — an update that changes stored data is REFUSED with no backup set up "
                  "(nothing changes):")
            no_seam = upgrade(ref="v0.2.0", release_tree=release, opener=fake_opener, backup=None)
            still1 = (module_coherence.load_engine_manifest() or {}).get("packages", {}).get("base")
            print("    -> " + (no_seam["reason"] if no_seam.get("refused") else "NOT refused?!"))
            ok = ok and no_seam.get("refused") and still1 == "0.0.0" and not pulls and not snapshots

            print("\nPart J — the same update with a backup available runs end-to-end:")
            res = upgrade(ref="v0.2.0", release_tree=release, opener=fake_opener, backup=fake_backup)
            for line in [f"base {res['from'].get('base')} -> {res['to'].get('base')}"] + \
                    [f"replaced {x}" for x in res.get("copied", [])] + \
                    [f"ran {r}" for r in res.get("migrations", {}).get("ran", [])]:
                print("    - " + line)
            engine = module_coherence.load_engine_manifest()
            cfg_marker = os.path.join(live, ".engine", "state", "config_marker.json")
            data_marker = os.path.join(live, ".engine", "state", "data_marker.json")
            stamp = None
            if os.path.isfile(data_marker):
                with open(data_marker) as fh:
                    stamp = json.load(fh).get("stamp")
            checks = {
                "engine.json records base 0.2.0": (engine or {}).get("packages", {}).get("base") == "0.2.0",
                "operator identity preserved": (engine or {}).get("identity") == "solo",
                "base tool replaced with v2":
                    "v2" in validate.read(os.path.join(live, ".engine/tools/base_tool.py")),
                "config migration ran": os.path.isfile(cfg_marker),
                "data migration ran (after a backup)": os.path.isfile(data_marker),
                "backup taken before the data migration": snapshots == [("recall-ledger", "v0.2.0")],
                "data snapshot stamped with the engine version": stamp == "v0.2.0",
                "old wire reversed (oldcache gone from .gitignore)":
                    "oldcache" not in validate.read(os.path.join(live, ".gitignore")),
                "new wire applied (newcache present in .gitignore)":
                    "newcache" in validate.read(os.path.join(live, ".gitignore")),
                "consistent after the update":
                    not [f for f in res.get("findings", []) if f["severity"] == "hard"],
                "opened a pull request for review": bool(res.get("pr")) and len(pulls) == 1,
                "retired-capability notice selected on the version jump":
                    [r.get("version") for r in res.get("retired_capabilities", [])] == ["0.2.0"],
                "retired-capability notice carried into the review PR body":
                    bool(pulls) and "no longer offers its one-shot cache reset" in (pulls[-1].get("body") or ""),
            }
            for label, good in checks.items():
                print(f"    [{'ok' if good else 'FAIL'}] {label}")
                ok = ok and good

            print("\nPart J2 — the update is fetched from the engine's recorded HOME, never this repo's own "
                  "origin (#367); and with NO home recorded it refuses with a remedy rather than guess a home:")
            seen = {}
            saved_fetch2 = globals().get("_fetch_release_tree")
            globals()["_fetch_release_tree"] = lambda ref, dest, repo=None, token=None: (
                seen.__setitem__("repo", repo) or (_ for _ in ()).throw(RuntimeError("stop after capture")))
            try:
                upgrade(ref="v0.2.0")            # real fetch path -> captures the SOURCE repo, then stops
            finally:
                globals()["_fetch_release_tree"] = saved_fetch2
            from_home = seen.get("repo") == "acme/engine-home"
            print(f"    [{'ok' if from_home else 'FAIL'}] fetched from the recorded home 'acme/engine-home' "
                  f"(saw {seen.get('repo')!r}), not this repo's own origin")
            ok = ok and from_home
            eng2 = module_coherence.load_engine_manifest()
            eng2.pop("home_repository", None)    # a repo generated BEFORE the home coordinate shipped
            _write_json(os.path.join(live, ".engine", "engine.json"), eng2)
            no_home = upgrade(ref="v0.2.0")
            asks_to_record = (no_home.get("refused")
                              and "no update home recorded" in (no_home.get("reason") or ""))
            print("    -> " + (no_home["reason"] if no_home.get("refused") else "NOT refused?!"))
            print(f"    [{'ok' if asks_to_record else 'FAIL'}] absent home refuses with a plain remedy, "
                  f"never falling back to origin")
            ok = ok and asks_to_record

    print("\nPart K — the update RE-RENDERS the code-ownership wall for the new version's engine files, so "
          "a file the new release adds still routes to the operator for review — and the operator's OWN "
          "rules are kept (the design's upgrade re-render):")
    with tempfile.TemporaryDirectory() as d:
        live = os.path.join(d, "live")
        os.makedirs(live)
        release = _build_upgrade_release(os.path.join(d, "release"))   # v0.2.0 ADDS migration .py files
        with _redirect_root(live):
            _build_upgrade_fixture(live)
            eng = module_coherence.load_engine_manifest()
            eng["handle"] = "@operator"        # the preserved-identity owner first-run records
            _write_json(os.path.join(live, ".engine", "engine.json"), eng)
            co_path = os.path.join(live, ".github", "CODEOWNERS")
            os.makedirs(os.path.dirname(co_path), exist_ok=True)
            with open(co_path, "w", encoding="utf-8") as fh:        # an operator rule + the OLD wall
                fh.write(wiring.render_codeowners("# my rules\n/src/ @team\n",
                                                  module_coherence.codeowners_path_set(), "@operator"))
            new_file = "/.engine/modules/base/migrations/config_010.py @operator"
            covered_before = new_file in validate.read(co_path)
            res = upgrade(ref="v0.2.0", release_tree=release,
                          opener=lambda **k: {"number": 0}, backup=lambda *a, **k: {"ok": 1})
            co_after = validate.read(co_path)
            co_checks = {
                "the wall did NOT cover the new file before the update": not covered_before,
                "the update re-rendered the wall": res.get("codeowners") == "written",
                "the new version's engine file now routes for review": new_file in co_after,
                "the operator's own rule survived untouched": "/src/ @team" in co_after,
            }
            for label, good in co_checks.items():
                print(f"    [{'ok' if good else 'FAIL'}] {label}")
                ok = ok and good

    print("\nPart L — the update KEYED-MERGES the root CLAUDE.md floor: it replaces ONLY the engine's marked "
          "block and keeps the operator's own content byte-for-byte, and content the release ships OUTSIDE "
          "its fence never overlays the floor (the #234/#272 coexistence obligation + the latent-bug fix):")
    with tempfile.TemporaryDirectory() as d:
        live = os.path.join(d, "live")
        os.makedirs(live)
        release = _build_upgrade_release(os.path.join(d, "release"))   # ships a v2 fenced floor + a decoy outside the fence
        with _redirect_root(live):
            _build_upgrade_fixture(live)
            claude_path = os.path.join(live, "CLAUDE.md")
            top = "# My product\n\nHow we work here.\n\n"
            bottom = "\n## Contributing\n\nOpen a PR.\n"
            old_floor = wiring.fence_apply(
                "", _FLOOR_FENCE, ["# Old engine floor (v1)", "", "Project status block."],
                style=wiring.MD_FENCE)
            with open(claude_path, "w", encoding="utf-8") as fh:   # operator prose AROUND the engine block
                fh.write(top + old_floor + bottom)
            res = upgrade(ref="v0.2.0", release_tree=release,
                          opener=lambda **k: {"number": 0}, backup=lambda *a: {"ok": 1})
            after = validate.read(claude_path)
            cl_checks = {
                "the floor was keyed-merged": res.get("claude_floor") == "merged",
                "the operator's content above the block survived": top in after,
                "the operator's content below the block survived": bottom in after,
                "the new engine floor replaced the block": "Project status block, refreshed in v2." in after,
                "the old engine floor is gone": "Old engine floor (v1)" not in after,
                "only the fence BODY merged, not the release file's other content": "must never travel" not in after,
            }
            for label, good in cl_checks.items():
                print(f"    [{'ok' if good else 'FAIL'}] {label}")
                ok = ok and good

    print("\n" + ("UPGRADE DEMO PASSED: an unreachable release degraded, a data update with no backup was "
                  "refused, a backed-up update overlaid + migrated + opened a pull request cleanly, the "
                  "update re-rendered the code-ownership wall for the new files while keeping operator rules, "
                  "and it keyed-merged the CLAUDE.md floor while keeping the operator's own content."
                  if ok else "UPGRADE DEMO DID NOT BEHAVE AS EXPECTED — see above."))
    return ok


# ---- removal demo (CODEOWNERS render + clean whole-engine removal; ALL boundaries faked) -------

def _build_remove_fixture(root: str) -> None:
    """A coherent live fixture engine with engine-owned files BOTH under .engine/ and in .github/, plus a
    CODEOWNERS carrying an engine block after an operator rule — so the removal demo exercises every leg."""
    _build_fixture(root)                                  # base + optx (+ optx's shared-file edits applied)
    os.makedirs(os.path.join(root, ".github", "workflows"))
    with open(os.path.join(root, ".github", "workflows", "engine-ci.yml"), "w") as fh:
        fh.write("name: engine-ci\n")
    with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        # An all-engine greenfield CLAUDE.md: the floor wrapped in the engine fence (what 6a's first-run seed
        # writes), so the removal demo exercises the real block-reversal (→ whitespace-only → file deleted).
        fh.write(wiring.fence_apply("", _FLOOR_FENCE, ["# engine floor"], style=wiring.MD_FENCE))
    co = wiring.render_codeowners("# product rules\n/src/ @team\n",
                                  [".engine/engine.json", ".github/workflows/engine-ci.yml"], "@operator")
    with open(os.path.join(root, ".github", "CODEOWNERS"), "w") as fh:
        fh.write(co)


def remove_engine_demo() -> bool:
    """Fail-then-pass demonstration of the CODEOWNERS renderer and clean whole-engine removal, returning
    True iff every leg behaved. The REAL render / shared-file reversal / delete-set / safety-rule-decision
    logic runs; FOUR boundaries are faked because none can run in the construction repo: (1) the GitHub
    branch-protection API, (2) the git/pull-request open, (3) a real deployed working tree, (4) the
    operator's real keep/remove choice. 'Works on the fixture ⇒ works for a real adopter' is the inductive
    gap the fixture cannot discharge."""
    ok = True
    prs = []

    def fake_opener(branch, title, body):
        prs.append((branch, title))
        return {"number": 0, "html_url": "(fixture)"}

    def fake_transport(method, path, body=None):
        if method == "GET" and path.endswith("/rulesets"):
            return (200, [{"id": 1, "name": bootstrap.ENGINE_RULESET_NAME}], {})
        return (200 if method == "PUT" else 204 if method == "DELETE" else 200, None, {})

    print("=" * 70)
    print("REMOVAL DEMO — CODEOWNERS ownership block + clean whole-engine removal, on a FIXTURE engine.\n"
          "The branch-protection setting, the pull-request open, and the operator's keep/remove choice are\n"
          "all faked; the real render / reversal / delete logic runs. None of this runs on the real engine.")

    print("\nPart K — the CODEOWNERS ownership block renders one file-precise line per engine file:")
    green = wiring.render_codeowners("", [".engine/engine.json", "CLAUDE.md"], "@operator")
    brown = wiring.render_codeowners("# product rules\n/src/ @team\n", [".engine/engine.json"], "@operator")
    k_ok = ("/.engine/engine.json @operator" in green and brown.startswith("# product rules")
            and brown.index("engine.json") > brown.index("/src/ @team"))
    print("    [{}] greenfield seeds a block; brownfield appends AFTER the product's rules (last wins)"
          .format("ok" if k_ok else "FAIL"))
    ok = ok and k_ok

    print("\nPart L — clean removal, KEEPING the main-branch safety rule (the engine's checks removed):")
    with tempfile.TemporaryDirectory() as d:
        with _redirect_root(d):
            _build_remove_fixture(d)
            r = remove_engine(opener=fake_opener, transport=fake_transport, choice="keep",
                              announce=lambda m: None)
            co_text = validate.read(os.path.join(d, ".github", "CODEOWNERS"))
            checks = {
                "the main-branch safety rule was kept, the engine's checks removed":
                    (r["de_bootstrap"] or {}).get("status") == "kept",
                "the module's shared-file edits were undone": bool(r["reversed"]),
                "a permission the operator also holds was left in place and disclosed":
                    bool(r["left_in_place"]),
                "the whole .engine/ tree was deleted": not os.path.isdir(os.path.join(d, ".engine")),
                "the engine's .github/ file was deleted (per-module remove never touches .github/)":
                    not os.path.isfile(os.path.join(d, ".github", "workflows", "engine-ci.yml")),
                "the all-engine CLAUDE.md (only the engine block) was removed":
                    not os.path.isfile(os.path.join(d, "CLAUDE.md")),
                "CODEOWNERS kept the product rule and dropped the engine block":
                    "/src/ @team" in co_text and "engine.json" not in co_text,
                "the deletions were opened as a (fixture) pull request for review": r["pr"] is not None,
            }
        for label, good in checks.items():
            print(f"    [{'ok' if good else 'FAIL'}] {label}")
            ok = ok and good
    print("    reversal note -> " + (r.get("reversal_note") or ""))

    print("\nPart M — clean removal, REMOVING the safety rule entirely (the operator's other choice):")
    deletes = []

    def drop_transport(method, path, body=None):
        if method == "DELETE":
            deletes.append(path)
        return fake_transport(method, path, body)
    with tempfile.TemporaryDirectory() as d:
        with _redirect_root(d):
            _build_remove_fixture(d)
            r2 = remove_engine(opener=fake_opener, transport=drop_transport, choice="drop",
                               announce=lambda m: None)
        m_ok = (r2["de_bootstrap"] or {}).get("status") == "dropped" and bool(deletes)
        print(f"    [{'ok' if m_ok else 'FAIL'}] the safety rule was removed entirely (a delete was issued)")
        ok = ok and m_ok

    print("\nPart N — clean removal on a BROWNFIELD repo whose OWN rule the engine augmented:")
    # The arrival added the engine's two checks (and a non_fast_forward rule) INTO the product's own ruleset
    # and recorded that in engine.json. Removal must reverse EXACTLY that and leave the product's rule —
    # never delete it, and never offer a keep/drop choice (the rule is the operator's, not the engine's).
    product_detail = {
        "id": 9, "name": "team protections", "target": "branch", "enforcement": "active",
        "node_id": "RRS_x", "_links": {"self": {"href": "x"}}, "created_at": "2026-01-01T00:00:00Z",
        "source": "owner/repo", "source_type": "Repository", "current_user_can_bypass": "always",
        "bypass_actors": [{"actor_id": 5, "actor_type": "Team", "bypass_mode": "always"}],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "pull_request", "parameters": {"required_approving_review_count": 1},
             "ruleset_source_type": "Repository", "ruleset_id": 9},
            {"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "product-ci"}, {"context": "engine-ci"},
                                           {"context": "engine-guard"}],
                "strict_required_status_checks_policy": False}, "ruleset_id": 9},
            {"type": "non_fast_forward", "ruleset_id": 9},   # engine-added (recorded in the marker)
        ],
    }
    n_puts, n_deletes = [], []

    def aug_transport(method, path, body=None):
        if method == "GET" and path.endswith("/rulesets"):
            return (200, [{"id": 9, "name": "team protections"}], {})   # NO engine-named ruleset present
        if method == "GET" and path.endswith("/rulesets/9"):
            return (200, product_detail, {})
        if method == "PUT" and path.endswith("/rulesets/9"):
            n_puts.append(body)
            return (200, {"id": 9}, {})
        if method == "DELETE":
            n_deletes.append(path)
            return (204, None, {})
        return (200, None, {})

    with tempfile.TemporaryDirectory() as d:
        with _redirect_root(d):
            _build_remove_fixture(d)
            # Record the augment marker the arrival would have written.
            eng_path = os.path.join(d, ".engine", "engine.json")
            eng = json.loads(validate.read(eng_path))
            eng["control_plane"] = {"ruleset_mode": "augmented", "augmented_ruleset_id": 9,
                                    "added": {"checks": ["engine-ci", "engine-guard"],
                                              "rules": ["non_fast_forward"]}}
            _write_json(eng_path, eng)
            rN = remove_engine(opener=fake_opener, transport=aug_transport, choice="keep",
                               announce=lambda m: None)
        put_body = n_puts[-1] if n_puts else {}
        put_rules = put_body.get("rules", [])
        put_types = {r.get("type") for r in put_rules}
        put_checks = bootstrap._bound_checks(put_rules)
        n_checks = {
            "the engine's checks were taken out of the product's rule (status 'unaugmented')":
                (rN["de_bootstrap"] or {}).get("status") == "unaugmented",
            "the product's rule was NOT deleted (it is the operator's)": not n_deletes,
            "exactly one update was written back to the product's rule": len(n_puts) == 1,
            "the engine checks are gone, the product's own check remains":
                put_checks == {"product-ci"},
            "the engine-added force-push rule was removed": "non_fast_forward" not in put_types,
            "the product's own pull-request rule was left untouched":
                {"type": "pull_request", "parameters": {"required_approving_review_count": 1}} in put_rules,
            "the operator's bypass list was preserved verbatim":
                put_body.get("bypass_actors") == product_detail["bypass_actors"],
        }
        for label, good in n_checks.items():
            print(f"    [{'ok' if good else 'FAIL'}] {label}")
            ok = ok and good

    print("\n" + ("REMOVAL DEMO PASSED: the ownership block rendered file-precisely, and the engine removed\n"
                  "itself cleanly on the fixture — it took its checks off the safety rule first, undid its\n"
                  "shared-file edits, deleted its files, and opened a reviewed pull request — for the engine's\n"
                  "own keep AND remove choices, AND for a brownfield repo whose own rule it had only augmented\n"
                  "(reversing exactly what it added, never deleting the operator's rule). The four real\n"
                  "boundaries named above are the inductive gap a fixture cannot discharge."
                  if ok else "REMOVAL DEMO DID NOT BEHAVE AS EXPECTED — see above."))
    return ok


# ---- rollback: undo a staged/stalled update, or restore memory after a reverted one ----------
#
# `rollback` is the deliberate counterpart to `upgrade`, surfaced through the one `/engine-upgrade` command.
# It is the ONE operator action that changes the working copy WITHOUT a reviewable pull request — a stalled
# update was never committed, so there is nothing to open a PR against. The honest safety floor (accepted by
# the maintainer, and stated in the pull request body): it acts ONLY on a real staged/ahead state; it saves a
# recovery point (a local "safe point" branch capturing everything) BEFORE touching anything, so nothing is
# unrecoverable; the memory restore keeps its resurrection guard, so an older copy never overwrites newer
# memory; and the operator-typed skill (disable-model-invocation) plus the conduct routing are the
# pre-execution gates. No false "never" — the same honesty as slice 2's routing posture.

_ROLLBACK_USAGE = ("usage: module_manager.py rollback [--confirm] [--json]\n"
                   "  Without --confirm it only CHECKS what undoing would do and changes nothing.\n"
                   "  With --confirm it undoes a staged/stalled update (saving a recovery point first),\n"
                   "  or puts your saved memory back to the copy from before an update that was taken out.")


def _git(root: str, *args: str, timeout: int = 30):
    """Run a read-only-or-additive git command in `root`; stdout on success, None on any failure (missing
    binary, non-zero exit, timeout). The rollback path never forces (`-f`) and never touches gitignored data."""
    import subprocess   # local: only the rollback git plumbing needs it
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                           timeout=timeout, check=False)
        return r.stdout if r.returncode == 0 else None
    except Exception:   # noqa: BLE001 — a git failure is reported by the caller, never a traceback
        return None


def _git_status_paths(root: str) -> set:
    """Repo-relative paths git reports as changed — staged OR unstaged, tracked OR untracked (`??`) — from
    `git status --porcelain`. Empty when git is unavailable (callers degrade safely: the guard then finds no
    'foreign' work and the staged-update signal reads clean)."""
    paths: set = set()
    for line in (_git(root, "status", "--porcelain") or "").splitlines():
        if len(line) < 4:
            continue
        p = line[3:]
        if " -> " in p:                       # a rename 'XY old -> new' — the new path is what's on disk now
            p = p.split(" -> ", 1)[1]
        paths.add(p.strip().strip('"'))
    return paths


def _git_deleted_paths(root: str) -> set:
    """Repo-relative paths git reports as a DELETION of a tracked file (status 'D' in either column of
    `git status --porcelain`). A staged or unstaged deletion of a tracked file is losslessly reversible — the
    discard's `git checkout <branch>` restores it from HEAD, and the recovery point commits it first — so it is
    NEVER operator work at risk. The reconcile's delete leg leaves a renamed-away OLD path here as a staged
    deletion (a rename that also rewrote the file shows as `D`+`A`, not `R`), and it must not be mistaken for
    the operator's own uncommitted work and refuse the undo (#599). Empty when git is unavailable."""
    paths: set = set()
    for line in (_git(root, "status", "--porcelain") or "").splitlines():
        if len(line) < 4:
            continue
        if "D" in line[:2]:                    # 'D' in either status column — a tracked-file deletion
            p = line[3:]
            if " -> " in p:                    # defensive: a rename never carries 'D', but keep the new side
                p = p.split(" -> ", 1)[1]
            paths.add(p.strip().strip('"'))
    return paths


def _upgrade_footprint() -> set:
    """Every repo-relative path an upgrade's tail can WRITE — single-sourced so the discard's foreign-work
    guard cannot drift from what an update actually touches. Seeded from the RECONCILE DELIVER SET
    (`engine_synced_paths`, project_retire=False): the overlay membership (a module's `provides` files +
    module manifests + FOUNDATION_CODE) PLUS the `.engine/_fixtures/**` namespace the reconcile delivers PLUS
    the five keyed/rendered foundation files (engine.json, CODEOWNERS, root CLAUDE.md/AGENTS.md, .gitignore) —
    then the wiring-seam target files. Sourcing from the deliver set is what keeps a reconcile-delivered fixture
    from reading as the operator's own work at discard time (#599: the pre-Slice-2a footprint knew only the
    overlay copy-map, never the fixtures the reconcile now delivers). `project_retire=False` skips the
    first-run-assets read so this never raises on the rollback path."""
    manifests_by_id = {m.get("id"): m for _rel, m in module_coherence.discover_manifests()}
    paths = set(engine_synced_paths(validate.ROOT, manifests_by_id, project_retire=False))
    paths.update(module_coherence.WIRING_TARGETS.values())
    return paths


def _staged_upgrade_dirty() -> bool:
    """Is an update STAGED but not committed — the precise, coherence-independent signal of a stalled/
    half-applied update in the working tree? True iff any OVERLAY-CODE path (a module's `provides` file, a
    module manifest, or a FOUNDATION_CODE file — files an operator never hand-edits) differs from HEAD. A
    successfully-applied update is committed to its own branch (clean here); an operator editing their own
    `settings.json` does NOT trip this (settings are not overlay-code). Coherence-independent by design: a
    stall that leaves the wiring applied but the tree half-built passes `check_coherence` yet is still dirty
    here."""
    dirty = _git_status_paths(validate.ROOT)
    return bool(dirty and (dirty & set(overlay_replace_paths())))


def _diagnose_undo() -> dict:
    """Read-only: which undo state is the engine in? Precedence — a staged/stalled update first (dirty
    overlay-code), then memory-ahead-of-code (a reverted/merged update whose stored-data change outlived the
    code), then nothing-local. Never mutates and never promotes a durable Issue (github=None)."""
    current = (module_coherence.load_engine_manifest() or {}).get("engine_release")
    if _staged_upgrade_dirty():
        return {"state": "staged", "current": current}
    offer = None
    try:
        from memory import restore_vault as _rv   # lazy: restore_vault -> backup_vault -> boot is a back-edge
        offer = _rv.detect_migration_revert(github=None)   # github=None: an undo never promotes a durable Issue
    except Exception:   # noqa: BLE001 — detection fault degrades to no-offer
        offer = None
    if offer:
        return {"state": "memory-ahead", "current": current, "tag": offer.get("tag")}
    return {"state": "none", "current": current}


def _put_back_pre_update_memory(transport, base: dict) -> dict:
    """Put the saved memory back to the copy from before an update, reusing the exact restore the engine
    offers at startup. Fires only when the store is genuinely ahead of the code (`detect_migration_revert`),
    so it never runs on a clean match. Keeps the resurrection guard (never `override`), so an older copy can't
    overwrite newer memory. Degrades plainly when the backup can't be reached (the local stamp persists, so
    boot re-offers it)."""
    result = dict(base)
    try:
        from memory import restore_vault as _rv
        import memory as _memory
    except Exception:   # noqa: BLE001 — memory tools absent -> nothing to restore
        result["restored"] = False
        return result
    offer = None
    try:
        offer = _rv.detect_migration_revert(github=None)
    except Exception:   # noqa: BLE001
        offer = None
    if not offer or not offer.get("tag"):
        result["restored"] = False
        result["memory_note"] = "no saved-memory change to put back"
        return result
    try:
        res = _memory.restore_pre_migration(tag=offer["tag"], consent="y", transport=transport)
    except Exception as exc:   # noqa: BLE001 — vault unreachable / fetch failure
        result["restored"] = False
        result["memory_note"] = (f"couldn't reach your backup to put the copy back ({exc}); your memory is "
                                 f"unchanged — try again when you're online")
        return result
    result["restored"] = bool(res.get("ok"))
    result["memory_note"] = res.get("message")
    return result


def _discard_staged_update(resync, transport) -> dict:
    """Discard a staged/stalled update: guard the operator's own work, save a recovery point, return the
    working tree to its pre-update state, rebuild the runtime, and put back any memory the update changed."""
    import checkout_health   # lazy: the shared lossless-rescue primitive + the safe git readers
    root = validate.ROOT
    result: dict = {"state": "staged", "undone": False}
    # (a) GUARD — refuse if the operator has their OWN uncommitted work (anything the update didn't write).
    # Tracked-file deletions are excluded: a staged/unstaged delete is losslessly restored by the branch
    # switch below (and captured on the recovery point first), so a reconcile's renamed-away old path — or even
    # the operator's own deletion — is never work at risk, and must not false-refuse the undo (#599).
    foreign = sorted(_git_status_paths(root) - _upgrade_footprint() - _git_deleted_paths(root))
    if foreign:
        result["refused"] = True
        result["your_changes"] = foreign[:20]
        shown = ", ".join(foreign[:8]) + (" …" if len(foreign) > 8 else "")
        result["reason"] = (f"You have unsaved work of your own in files this update didn't touch, so I "
                            f"stopped rather than risk it: {shown}. Save that work somewhere safe first (or "
                            f"ask me to help you set it aside), then ask me to undo the update again. Nothing "
                            f"has changed.")
        return result
    # (b) which branch to return to — a stall was never committed, so its pre-update state IS this branch's HEAD.
    branch = (_git(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    if not branch or branch == "HEAD":
        result["refused"] = True
        result["reason"] = ("I couldn't tell which branch you're on (or you're not on one), so I stopped "
                            "rather than risk your work. Nothing has changed.")
        return result
    # (c) RECOVERY POINT — save everything (a lossless "safe point" branch) BEFORE anything is undone.
    rescue = checkout_health.save_recovery_point(
        root, message="engine: saved your work before undoing the staged update")
    if not rescue:
        result["refused"] = True
        result["reason"] = ("I couldn't save a recovery point first, so I stopped — nothing was undone and "
                            "your staged update is untouched.")
        return result
    result["recovery_point"] = rescue
    # (d) DISCARD — return to the pre-update branch; the switch reverts the tree to its last-saved state and
    #     drops the update's added files (they live only on the recovery point now). save_recovery_point left
    #     us on the rescue branch with a clean tree, so this switch cannot lose anything.
    if _git(root, "checkout", branch) is None:
        result["partial"] = True
        result["reason"] = (f"I saved your work to a recovery point ('{rescue}') but couldn't finish putting "
                            f"your engine back automatically. Nothing was lost — ask me and I'll take it "
                            f"from here.")
        return result
    result["undone"] = True
    result["branch"] = branch
    # (e) rebuild the tool-runtime for the restored (older) code; surface a failure, never hide it.
    if resync is not None and resync() is False:
        result["resync_failed"] = True
    # (f) put back any memory the update changed — engine.json is back at the old version now, so a stored-data
    #     change is 'ahead' and detect_migration_revert finds it.
    for k, v in _put_back_pre_update_memory(transport, {}).items():
        if k in ("restored", "memory_note"):
            result[k] = v
    return result


def rollback(*, confirm: bool = False, resync=_UNSET, transport=None) -> dict:
    """Undo an engine update — the deliberate counterpart to `upgrade`, surfaced through `/engine-upgrade`.
    Read-only unless `confirm`. Three states: a STAGED/stalled update (discard it, saving a recovery point
    first); memory AHEAD of the code after a reverted/merged update (put the saved copy back); or nothing to
    undo locally (a merged update is undone by reverting its pull request — guided, never a local reset of
    protected `main`). `resync` seams the tool-runtime rebuild (tests inject a no-op); `transport` is the
    memory backup transport (the real vault in production, a fake in tests)."""
    resync = _resync_tool_runtime if resync is _UNSET else resync
    diag = _diagnose_undo()
    if not confirm:
        return diag
    if diag["state"] == "staged":
        return _discard_staged_update(resync, transport)
    if diag["state"] == "memory-ahead":
        return _put_back_pre_update_memory(transport, {"state": "memory-ahead"})
    return dict(diag)   # nothing to undo locally


def _render_rollback(r: dict, applied: bool) -> None:
    """Plain-language render of a rollback preview (applied=False) or result (applied=True)."""
    state = r.get("state")
    if not applied:
        if state == "staged":
            # Disclose EVERYTHING the undo touches, up front, so the operator consents from a full picture:
            # the engine's own files, the shared setup files they may have edited, and any saved memory.
            print("An update is staged but not finished — I can undo it. First I save a recovery point of "
                  "everything exactly as it is now, then put your engine back to before the update: its own "
                  "files, the shared setup files it changes (like your CLAUDE.md and your settings), and any "
                  "saved memory the update changed. Nothing is lost — if you'd edited those setup files "
                  "yourself, your version is kept on the recovery point. I'll stop and ask first if you have "
                  "unsaved work of your own in other files.\n"
                  "To go ahead, type `/engine-upgrade` and choose to undo (or run `rollback --confirm`). To "
                  "finish the update instead, run `upgrade --confirm`.")
        elif state == "memory-ahead":
            print("Your saved memory was changed by an update that's no longer in place, so your memory and "
                  "your engine don't match right now. I can put your memory back to the copy saved before "
                  "that update.\nTo go ahead, type `/engine-upgrade` and choose to undo (or run "
                  "`rollback --confirm`).")
        else:
            print("There's nothing to undo — your engine and saved memory match. To undo an update you "
                  "already merged, revert its pull request; ask me and I'll prepare that for you.")
        return
    if r.get("refused") or r.get("partial"):
        print(r["reason"])
        return
    if state == "staged" and r.get("undone"):
        line = ("Done — I undid the staged update and put your engine back to before it: its own files, the "
                "shared setup files, and any saved memory the update changed. I saved everything as it was to "
                f"a recovery point ('{r.get('recovery_point')}') — if you'd made your own edits to the setup "
                "files, your version is there.")
        if r.get("resync_failed"):
            line += (" One heads-up: I couldn't rebuild the engine's tool-runtime automatically — ask me and "
                     "I'll finish that.")
        if r.get("restored") is False and r.get("memory_note") and "no saved-memory" not in r["memory_note"]:
            line += f" A note on your saved memory: {r['memory_note']}."
        print(line)
        return
    if state == "memory-ahead":
        if r.get("restored") is True:
            print("Done — I put your saved memory back to the copy from before the update. Your memory and "
                  "engine match again.")
        else:
            print(r.get("memory_note") or "Your memory is unchanged.")
        return
    print("There was nothing to undo — nothing changed.")


def main(argv: list) -> int:
    if not argv:
        print("usage: module_manager.py {status | sync-groups | add <id> [--json] | "
              "plan-remove <id> | remove <id> [--json] | upgrade [ref] [--confirm] [--json] | "
              "rollback [--confirm] [--json] | "
              "remove-engine [--confirm] [--keep-protection|--remove-protection] [--json] | demo}",
              file=sys.stderr)
        return 2
    cmd = argv[0]
    try:
        if cmd == "__upgrade_tail__":
            # INTERNAL: the child half of `upgrade` (issue #594). Gated so a stray operator command or an
            # injected instruction can't drive the mutating tail — it runs ONLY when spawned by a real
            # upgrade (the private env marker) with a valid state (the in-state marker, checked downstream).
            if os.environ.get("ENGINE_UPGRADE_CHILD") != "1" or len(argv) < 2:
                print("CONFIG ERROR: __upgrade_tail__ is an internal step of `upgrade`, not a command.",
                      file=sys.stderr)
                return 2
            try:
                _run_upgrade_tail(_upgrade_state_load(argv[1]))
                return 0
            except Exception as exc:   # noqa: BLE001 — surfaced to the parent via exit code + stderr
                print(f"upgrade tail failed: {exc}", file=sys.stderr)
                return 2
        if cmd == "status":
            return _status()
        if cmd == "sync-groups":
            res = sync_groups()
            tail = f"{res['groups'] or '(none)'}."
            print((f"Updated the tool-runtime dependency groups to match the installed modules: {tail}")
                  if res["changed"] else
                  (f"The tool-runtime dependency groups already match the installed modules: {tail}"))
            return 0
        if cmd == "demo":
            ok_remove = run_demo()
            print("\n" + ("-" * 70) + "\n")
            ok_add = add_demo()
            print("\n" + ("-" * 70) + "\n")
            ok_upgrade = upgrade_demo()
            print("\n" + ("-" * 70) + "\n")
            ok_remove_engine = remove_engine_demo()
            return 0 if (ok_remove and ok_add and ok_upgrade and ok_remove_engine) else 1
        if cmd == "plan-remove":
            if len(argv) < 2:
                print("CONFIG ERROR: plan-remove needs a module id.", file=sys.stderr)
                return 2
            plan = plan_remove(argv[1])
            if plan["refused"]:
                print(f"Removing '{argv[1]}' would be refused: {plan['reason']}")
                return 1
            print(f"'{argv[1]}' can be removed. It would undo {len(plan['wires'])} setting "
                  f"change(s), delete its files, and re-check that what remains is consistent.")
            return 0
        if cmd == "remove":
            if len(argv) < 2:
                print("CONFIG ERROR: remove needs a module id.", file=sys.stderr)
                return 2
            result = remove(argv[1])
            if "--json" in argv:
                print(json.dumps(result, indent=2))
            else:
                _render_remove(result)
            if result.get("refused"):
                return 1
            return 1 if any(f.get("severity") == "hard" for f in result.get("findings", [])) else 0
        if cmd == "add":
            if len(argv) < 2:
                print("CONFIG ERROR: add needs a module id.", file=sys.stderr)
                return 2
            result = add(argv[1])
            if "--json" in argv:
                print(json.dumps(result, indent=2))
            else:
                _render_add(result)
            if result.get("refused"):
                return 1
            return 1 if any(f.get("severity") == "hard" for f in result.get("findings", [])) else 0
        if cmd == "upgrade":
            # PREVIEW BY DEFAULT (the #594 footgun close): bare `upgrade` — and `upgrade --help`, and any
            # stray flag — must NEVER apply a real update. Applying takes a deliberate `--confirm`, mirroring
            # `remove-engine`'s gate.
            if "--help" in argv or "-h" in argv:
                print(_UPGRADE_USAGE)
                return 0
            unknown = [a for a in argv[1:] if a.startswith("-") and a not in ("--confirm", "--json")]
            if unknown:
                print(f"CONFIG ERROR: unknown option(s) for upgrade: {' '.join(unknown)}\n{_UPGRADE_USAGE}",
                      file=sys.stderr)
                return 2
            ref = next((a for a in argv[1:] if not a.startswith("-")), None)
            if "--confirm" not in argv:
                try:
                    preview = upgrade_preview(ref)   # READ-ONLY — changes nothing
                    if "--json" in argv:
                        print(json.dumps(preview, indent=2))
                    else:
                        _render_upgrade_preview(preview)
                except Exception as exc:   # noqa: BLE001 — the check must never crash on a malformed release
                    print(f"Couldn't complete the update check — the engine is unchanged and still working. "
                          f"({exc})")
                return 0
            result = upgrade(ref)
            if "--json" in argv:
                print(json.dumps(result, indent=2))
            else:
                _render_upgrade(result)
            # 0 only when the update actually landed a pull request; a refusal, a paused coherence
            # finding, a failed re-sync, or a PR that could not be opened all leave it un-landed -> 1.
            if result.get("refused"):
                return 1
            return 0 if result.get("pr") else 1
        if cmd == "rollback":
            # UNDO by default is a CHECK: bare `rollback` (and `--help`, and any stray flag) changes nothing;
            # undoing takes a deliberate `--confirm`, mirroring `upgrade`/`remove-engine`.
            if "--help" in argv or "-h" in argv:
                print(_ROLLBACK_USAGE)
                return 0
            unknown = [a for a in argv[1:] if a.startswith("-") and a not in ("--confirm", "--json")]
            if unknown:
                print(f"CONFIG ERROR: unknown option(s) for rollback: {' '.join(unknown)}\n{_ROLLBACK_USAGE}",
                      file=sys.stderr)
                return 2
            confirm = "--confirm" in argv
            try:
                result = rollback(confirm=confirm)
            except Exception as exc:   # noqa: BLE001 — the check/undo must never crash into a traceback
                print(f"Couldn't complete that — your engine is unchanged. ({exc})")
                return 1
            if "--json" in argv:
                print(json.dumps(result, indent=2))
            else:
                _render_rollback(result, applied=confirm)
            if result.get("refused") or result.get("partial"):
                return 1
            return 0
        if cmd == "remove-engine":
            # Destructive + operator-privileged: without --confirm this only PREVIEWS (changes nothing).
            if "--confirm" not in argv:
                print("Removing the WHOLE engine is a deliberate step. It takes the engine's checks off "
                      "your main branch's safety rule, removes the engine's entries from your shared setup "
                      "files, deletes all the engine's files, and opens a pull request with the deletions "
                      "for your review. Nothing has changed.\n\nTo proceed, re-run with --confirm and ONE "
                      "of:\n  --keep-protection    keep your main-branch safety rule (engine's checks "
                      "removed)\n  --remove-protection  remove your main-branch safety rule entirely")
                return 1
            keep_f, drop_f = "--keep-protection" in argv, "--remove-protection" in argv
            if keep_f == drop_f:   # neither, or BOTH (ambiguous) — never silently pick the destructive one
                print("CONFIG ERROR: remove-engine --confirm needs EXACTLY ONE of --keep-protection or "
                      "--remove-protection (your choice for the main-branch safety rule).", file=sys.stderr)
                return 2
            choice = "drop" if drop_f else "keep"
            result = remove_engine(choice=choice)
            if "--json" in argv:
                print(json.dumps(result, indent=2))
            else:
                _render_remove_engine(result)
            if result.get("refused"):
                return 1
            return 0 if result.get("pr") else 1
        print(f"unknown command {cmd!r}", file=sys.stderr)
        return 2
    except Exception as exc:  # a malformed manifest / engine.json halts loudly, never a traceback
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
