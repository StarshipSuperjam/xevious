"""The write boundary for the engine's OWN files — one predicate, homed once (StarshipSuperjam/engine-template#923).

The invariant: the engine never writes one of its own generated files THROUGH a symlink, or to a
destination that resolves outside the tree it belongs to. A planted shortcut at an engine-owned path
(`.engine/engine.json` above all — it is tracked, so it can arrive in a clone, a pull request, or a
release) would otherwise be silently followed on write, placing the engine's file OUTSIDE the repository.
StarshipSuperjam/engine-template#862 built this guard for the arrival path's writers; StarshipSuperjam/engine-template#923 homes the predicate here so the module
lifecycle's writers (upgrade, add, remove) inherit the same rule instead of re-remembering it per site —
the StarshipSuperjam/engine-template#862 review found new unguarded writers four rounds running, which is what per-site guarding buys.

What is unified here is the PREDICATE (and the one guarded JSON writer below); the enforcement idiom
stays per-site on purpose, because the right failure mode differs by surface: the arrival flow and the
standalone CLIs catch `EngineWriteRefused` into a clean disclosed stop, the upgrade tail converts it to
its staged-refusal result, the release cut pre-flights its stage/swap destinations, and best-effort
writers skip — disclosed where their surface already discloses (a cleanup's residue line, the drift
gate behind an index regen), silently where the surface's own documented posture is
every-failure-is-silent (bootstrap's finalize marker). A
caller that writes IN PLACE to an engine-owned slot outside the overlay's realpath wall should route
through `write_json` (or check `write_through_symlink_reason` first when it writes prose or must check
before a read).

Choosing `base` (the StarshipSuperjam/engine-template#923 review's central lesson): base and target must come from the SAME source.
- A fixed engine-owned slot (`.engine/engine.json`, `.engine/pyproject.toml`, the committed audit
  digest) is guarded against the repository root — the full wall: symlinked leaf, symlinked ancestor,
  or any escape refuses.
- A caller-supplied path (an injected fixture tree, a test's temp copy, a demo's throwaway) is guarded
  against its OWN parent directory, which reduces the rule to "the leaf must not be a symlink" — never
  against an ambient root the caller did not choose, which would refuse legitimate out-of-tree fixtures.

Deliberately NOT guarded (judged per surface, StarshipSuperjam/engine-template#923):
- Generic JSON writers (`module_manager._write_json`, `instantiator._write_json`): part of their real
  job is writing release-tree and fixture files DELIBERATELY outside the repository root. The invariant
  belongs to destinations, not to writers — the guarded slots above route around them.
- The engine RUNTIME cache writers (the boot slice cache, the standing-alarm ledger, memory capture's
  cursor/status/failure sidecars): their homes (`.engine/knowledge/.cache/`, `.engine/boot/.cache/`,
  `.engine/telemetry/.cache/`, `.engine/memory/`) all sit behind engine-managed gitignore blocks, so
  neither a leaf symlink nor a symlinked ancestor can arrive via a clone, a pull request, or a release
  — only local write access can plant one, and that access could write any target directly. (Note the
  weaker claim deliberately NOT made: `os.replace` protects only against a symlinked LEAF — it still
  traverses a symlinked ancestor directory, and the status sidecar uses a plain open — so the
  no-arrival-vector argument, not the replace mechanics, is what carries these.)
- The memory ledger (`.engine/memory/ledger.ndjson`) — including the close-turn transcript captures,
  which write by APPENDING to this same ledger: the gitignore argument above, plus it deliberately
  lives OUTSIDE the repository for worktrees (the shared clone root, or wherever ENGINE_MEMORY_DIR
  points), so containment is meaningless there — and a refusal inside the append would be silent and
  sticky (the capture cursor never advances). Guarding it would cost more than the hazard it closes.
- Operator-owned root/shared files (README.md, SECURITY.md, LICENSE, product-version.json,
  `.claude/settings.json` and the fence files via wiring): a LIVE shortcut there is the operator's own
  arrangement (a dotfiles-linked settings file) and writes through it are honored. What IS refused is
  the DANGLING shortcut — it reads as "absent" to every exists() check, so a seed-if-absent or
  create-through would drop a brand-new file outside the tree: the seeders skip it
  (`instantiator._seed_security` / `_seed_product_version`), and wiring's write primitives raise a
  WiringError finding (`wiring._dangling_shortcut_reason` — its own operator-file rule, deliberately
  narrower than this module's). README's content-marker gate and LICENSE's remove-only path need no
  guard (verified: a dangling link reads as unreadable content / os.remove unlinks the link itself).
- `.engine/state/*.json`: the only in-place writer is the arrival's state reseed
  (`instantiator._seed_state`), guarded there since StarshipSuperjam/engine-template#862 (it must check BEFORE its register read, so a
  dangling symlink is refused rather than read-swallowed). Upgrade migrations that touch state run
  behind the data-backup pre-flight, not through this module.

Check-then-write is accepted here on StarshipSuperjam/engine-template#862's own precedent: the race window is microseconds, and an
attacker who can flip the destination inside it already has local write access — with which they could
write the target directly. The guard closes the ARRIVAL vector, not local compromise.

This module imports only the standard library, so any tool — including module-provided packages — can
import it without a cycle.
"""

from __future__ import annotations
import json
import os


class EngineWriteRefused(Exception):
    """The engine refused to write one of its OWN generated files because the destination is a symlink
    or resolves outside the tree it belongs to — following it on write could place the file OUT of the
    repository. The fail-closed backstop behind the early warnings (StarshipSuperjam/engine-template#862's resume recognizer, StarshipSuperjam/engine-template#923's
    upgrade pre-flight): the warnings tell the operator early, this guarantees the write never follows
    the link."""


def write_through_symlink_reason(path: str, base: str) -> str | None:
    """A plain reason if writing `path` would follow a symlink or escape `base`, else None. Fail closed:
    refuse when the final component is a symlink, OR the fully resolved path (parent directories
    included — so a symlinked ancestor is caught too) lands outside `base`. Pick `base` per the module
    docstring: the repository root for a fixed engine-owned slot, the target's own parent for a
    caller-supplied path (which reduces this to the leaf-symlink rule)."""
    root = os.path.realpath(base)
    resolved = os.path.realpath(path)
    if os.path.islink(path) or not (resolved == root or resolved.startswith(root + os.sep)):
        rel = os.path.relpath(path, base)
        return (f"{rel!r} is a shortcut (a symlink), or sits under one, that points outside your project — writing "
                f"through it could put the engine's own file outside your project. Delete or replace the shortcut "
                f"at {rel!r}, then run again.")
    return None


def write_json(path: str, data, base: str) -> None:
    """Write `data` as 2-space-indented JSON with a trailing newline (the manifest's on-disk shape, so a
    written file diffs minimally) — REFUSING first when the destination is a symlink or escapes `base`.
    The check runs BEFORE `os.makedirs`: a refused write must not create directories through a symlinked
    ancestor either — keep that order if this is ever edited. Raises `EngineWriteRefused`; the caller
    owns the failure mode (catch into a refusal result, a disclosed skip, or a clean CLI stop)."""
    reason = write_through_symlink_reason(path, base)
    if reason:
        raise EngineWriteRefused(reason)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
