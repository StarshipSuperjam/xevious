#!/usr/bin/env python3
"""ledger_migrations.py — the home for carrying a saved-memory backup forward when its record shape changed.

The saved-memory store is an append-only ledger of records. Across engine versions the *shape* of those
records can change; when it does, a backup taken at the old shape has to be carried up to the current shape
before it can be restored. This module is where those record-shape transforms live, and it is what a restore
routes through.

There are NONE today: only one record shape has ever shipped, so no transform exists and the registry below is
empty. That is deliberate — this is the place a future shape change adds its step, not something to fill
speculatively. Until then, a restore of a backup whose shape does not match the current one has no path here,
so the restore declines honestly and leaves the memory on this computer untouched.

Two rules keep this safe:
  - The registry is a private, hardcoded-empty table. Nothing outside a test ever adds to it, and it is never
    read from disk, the environment, or a backup's own contents — so a corrupt or hostile backup can never
    introduce a transform of its own.
  - Resolving a path refuses by default: anything it cannot make sense of, or any gap it cannot bridge, comes
    back as "no path", so a restore declines rather than guess. It never raises.
"""
from __future__ import annotations

from collections import deque

# (from_version, to_version) -> a callable that rewrites the raw ledger bytes from one record shape to the next.
# EMPTY in this version: no record-shape change has shipped. A future shape change registers its single step
# here (e.g. carrying a version-1 backup up to a version-2 shape); a restore then finds and applies it. Private
# on purpose — the only way a step is ever added is by editing this table in the engine's own source, never from
# any backup, file, or environment a restore reads.
_REGISTRY: dict = {}


def resolve_ledger_migration(from_version, to_version):
    """Return the ordered list of record-shape transforms that carry a backup made at `from_version` up to
    `to_version`, or None when there is no way to bridge them.

    Only ever asked about DIFFERING versions — the restore caller handles an exact match itself and never calls
    here for it. Refuses by default: a version value it cannot make sense of, or a gap it cannot close, returns
    None so the restore declines rather than guess. Never raises. With the registry empty (the current state),
    every call returns None."""
    # A version must be a plain whole number to be bridged. A missing, malformed, or boolean value can't be, so
    # it declines here rather than risk being coerced into a match (True == 1 would otherwise sneak through).
    if not (isinstance(from_version, int) and not isinstance(from_version, bool)):
        return None
    # Walk the registered single steps forward from `from_version`, shortest chain first. An empty registry, a
    # value with no outgoing step, or a gap that can't be closed all fall through to None. Registry keys are
    # whole-number version pairs, so the comparisons here are int-to-int. The whole walk is guarded: a
    # malformed registry (a future authoring slip) declines by default rather than letting a restore crash.
    try:
        seen = {from_version}
        queue = deque([(from_version, [])])
        while queue:
            current, chain = queue.popleft()
            for (src, dst), transform in _REGISTRY.items():
                if src == current and dst not in seen:
                    next_chain = chain + [transform]
                    if dst == to_version:
                        return next_chain
                    seen.add(dst)
                    queue.append((dst, next_chain))
    except Exception:  # noqa: BLE001 — never raise; an unusable registry is treated as "no path" (decline)
        return None
    return None


def apply_ledger_migrations(ledger_bytes: bytes, chain) -> bytes:
    """Apply the ordered transforms to the raw ledger bytes and return the carried-forward bytes. All-or-nothing:
    a transform that fails, or returns something other than bytes, raises — and the restore caller treats a
    raised transform as an unbridgeable backup, writing nothing to the memory on this computer. So a restore
    never lands a half-carried copy."""
    out = ledger_bytes
    for transform in chain:
        out = transform(out)
        if not isinstance(out, (bytes, bytearray)):
            raise TypeError("a ledger migration must return bytes")
    return bytes(out)
