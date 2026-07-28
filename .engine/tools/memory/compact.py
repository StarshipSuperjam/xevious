"""compact.py — ledger compaction: the self-directed crash-safe rebuild-and-swap.

Active forgetting bounds the ledger's growth WITHOUT a second store and without seek-and-edit,
by a **whole-ledger rebuild-and-swap** — memory invoking its own restore primitive (*backup = copy; restore =
replace the ledger and rebuild the index*, ledger.replace_ledger). This module is **Layer 1**: *reversible,
mechanical, memory-autonomous* tidying that needs no human gate because **nothing recall-bearing is lost**.

What one pass does, under the single-writer `.capture.lock`, as ONE critical section:

  1. Read the RAW ledger (every record — including archived/retired ones; compaction must not drop recall).
  2. **Fold** each record's reinforcement markers into carried current-state fields (records.py:
     frecency_snapshot / snapshot_ts / last_access_ts / tier) via `score.mint_snapshot`, AND fold each
     CLOSED-batch gist supersession into the raw's carried `superseded_by` field, then **drop
     those folded markers**. The content-free `id` is carried verbatim. A record with no markers is rewritten byte-identical.
  3. Write the folded, marker-pruned ledger to a temp IN THE LEDGER'S OWN DIRECTORY, fsynced.
  4. **Bump the generation BEFORE the swap**, then `ledger.replace_ledger` (fsync temp → atomic rename → fsync
     dir), then rebuild the index stamped with the new generation.

**The Layer-1 fold never erases recall content** — only the (non-recall) `reinforcement` markers and the
(non-recall) CLOSED-batch `superseded` markers are pruned; every turn-delta, episodic (including a crash-duplicate
orphan, which stays logically retired, AND a gist-superseded raw, which stays superseded via its carried
`superseded_by`), gist, and `consolidated` marker survives the fold (a build-conformance invariant: no Layer-1
routine reaches erasure). Physical erasure of recall content is the SEPARATE, gated Layer-2 path below, never the
fold. A supersession is folded ONLY when its roll-up batch is closed: an un-closed (crashed-pass) `superseded` marker is
inert, so it is passed through verbatim, NEVER folded — else a crashed roll-up's hiding would be baked permanently
into the rewrite. Because frecency is
a **recurrence on the carried snapshot** (score.frecency), a compacted record scores IDENTICALLY to before — so
demotion survives the fold. The crash-safe-swap law: a crash at any point leaves exactly one intact
ledger (old or new); a stale index is always fully rebuilt — and the generation gate (index.py) routes a
crash-staled index to the always-correct scan until it is rebuilt, so an erased record can never
resurface from a stale index. Recovery binds to the fixed canonical ledger name: a temp left by a crash is a
complete same-schema file but is NEVER the canonical name, so it is ignored-and-reaped, never promoted.

**Layer-2 — gated physical erasure.** This same pass ALSO physically removes a record iff a VALID
`operator-adjudicated-erasure` marker targets its stable id (`_is_erased` / `_erasure_targets`) — the single
irreversible act in the memory system, reachable ONLY through that marker, which the operator authorises by merging
a single-purpose erasure pull request. The marker is RETAINED (the idempotency tombstone, so a re-compaction whose
target is already gone is a clean no-op), and a marker missing its merge SHA is inert — a READ-side fail-safe floor,
NOT consent verification (the merged-not-closed / immutable-merge-tree binding is the cross-session observer's job).
The AUTONOMOUS fold can never reach erasure: no routine mints a marker, and the SOLE minter
(`enact_erasure`) is reached only from the cross-session observer plus the test + demo, never from an
autonomous routine — so the removal set is non-empty only after that observer has minted a marker from a merged
erasure PR, and absent such a marker this pass is the Layer-1 no-op-shaped tidy. (Resurrection of an erased record via a restore /
migration-revert is SURFACED through boot's open-findings via the generation stamp this pass bumps — the consumer is
the restore round-trip's resurrection guard (`restore_vault.surface_resurrection`), which declines an
older-generation restore and surfaces it rather than silently resurrecting erased records.)

**The live AUTO-trigger.** `maybe_compact` rides memory's `PreCompact` hook — the "tolerable moment, never the
hot path" the design names for the expensive step — and fires `compact` for either of TWO reasons. The ordinary
one is housekeeping: `reclaimable_waste` (foldable markers — reinforcement + CLOSED-batch supersessions) reaching
`_COMPACT_WASTE_THRESHOLD`. On a ledger below that line nothing is rewritten. The other is not housekeeping at
all: a PENDING ERASURE (`pending_erasures`) fires the pass regardless of waste, because `compact` is the sole
executor of physical removal, so gating it behind unrelated bookkeeping would leave an erasure the operator had
already merged waiting on a counter that has nothing to do with it. Either way the fold itself is the Layer-1
no-op-shaped tidy (recall content byte-preserved, only non-recall markers folded) plus whatever Layer-2 removal a
valid merge-gated marker authorises. The reinforcement markers it folds come from recall; the closed-batch
supersessions from the live roll-up sweep. `should_compact` / `reclaimable_waste` / `pending_erasures` are pure
lock-free reads (the gate never writes).

Leaf discipline: RETURNS a small report and renders no operator-facing prose (the demo is the
one operator surface). stdlib + the cycle-free `memory` set (ledger / records / score / forget); `capture` (the
lock) and `index` (the rebuild) are lazy-imported inside `compact` to keep them off the module-load path.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import hooks  # noqa: E402 — the PreCompact entry point
from memory import forget, ledger, legacy_shapes as _legacy, records  # noqa: E402

_TEMP_PREFIX = ".compact-"          # the in-dir swap temp; NEVER the canonical name, so it is reapable
_TEMP_SUFFIX = ".ndjson"


_COMPACT_WASTE_THRESHOLD = 64   # build-spec leaf: the auto-trigger compacts only once this many foldable markers
#                                 have piled up. Failure direction is "nothing lost" — a too-high value just
#                                 defers a tidy.
#
#                                 Left at 64, now with the cadence MEASURED rather than assumed. Making the
#                                 conversation searchable raised how often this fires, because recall reinforces
#                                 every record it returns and the recall workflow runs several searches per
#                                 question: one triggered recall at the capped default (6 searches x 10 records)
#                                 leaves the waste at 60, so the trigger comes round about every other one.
#                                 Measured against a copy of a real 28.9 MB / 26.8 K-record store, one firing
#                                 costs 1.8 s to fold and swap plus 1.3 s to rebuild the index. That runs from
#                                 `consolidate`'s session-close sweep, not from anything the operator is waiting
#                                 on, and raising the threshold would only make each firing bigger for the same
#                                 total work — so the number stays where it is.


class _InjectedCrash(Exception):
    """A TEST/DEMO-only fault, raised at a chosen swap point to model a power-cut. Production `compact` never
    passes `_crash_after`, so this never fires in real use (a test pins the default off)."""


def _is_foldable(record, closed_rollup) -> bool:
    """True iff `record` is a NON-recall marker that `compact` folds-and-prunes: a `reinforcement` marker, or a
    `superseded` marker whose roll-up batch is CLOSED. An UN-closed (crashed-pass) supersession is inert — passed
    through verbatim, NEVER folded (so a crashed roll-up's hiding is never baked in). This is the SINGLE prune
    predicate that `compact`'s write loop, its `pruned` count, AND the `should_compact` gate all share, so the
    gate can never say "compact" for waste a compaction would not actually reclaim (the fire-when-nothing-folds
    trap)."""
    if not isinstance(record, dict):
        return False
    kind = record.get("kind")
    if kind == records.REINFORCEMENT_KIND:
        return True
    return kind == records.SUPERSEDED_KIND and record.get(records.BATCH_KEY) in closed_rollup


# --- Layer-2: gated physical erasure ------------------------------------------------------
# The single irreversible act. A record is physically removed by `compact` iff a VALID
# `operator-adjudicated-erasure` marker targets its stable id. The validity floor is enforced on the READ side
# here (not only at the writer), so a marker that reaches the ledger by any path other than `enact_erasure` —
# hand-edited, or minted by a future bypass — still cannot erase unless it carries its consent provenance.

def _is_erasure_marker(record) -> bool:
    """True iff `record` is a VALID operator-adjudicated-erasure marker: the right kind, a non-empty target id,
    AND a non-empty merge SHA. The SHA-presence is a READ-side STRUCTURAL fail-safe floor — a marker minted
    without its consent provenance is inert and erases nothing. It is NOT consent verification (the
    merged-not-closed / immutable-merge-tree binding is the cross-session observer's job)."""
    if not isinstance(record, dict) or record.get("kind") != records.ERASURE_KIND:
        return False
    target = record.get(records.TARGET_KEY)
    merge_sha = record.get(records.MERGE_SHA_KEY)
    return isinstance(target, str) and bool(target) and isinstance(merge_sha, str) and bool(merge_sha)


def _erasure_targets(raw_records) -> set:
    """The set of stable record ids that VALID erasure markers target — the Layer-2 removal set, derived from the
    already-materialized raw ledger (no extra disk pass). Empty whenever no valid marker is present, so the
    autonomous fold is a pure no-op on a ledger holding no valid marker — the cross-session observer writes those
    markers from merged erasure PRs."""
    return {r.get(records.TARGET_KEY) for r in raw_records if _is_erasure_marker(r)}


def _is_erased(record, erasure_targets) -> bool:
    """True iff `record` is recall content whose stable id a valid erasure marker targets — physical removal at
    compaction, the single irreversible act. Eligible ONLY for a NON-erasure-marker record (`kind != ERASURE_KIND`),
    so the erasure marker is NEVER pruned by its own (or a colliding) id — it is RETAINED as the idempotency
    tombstone, making a re-compaction whose target is already gone a clean no-op."""
    if not isinstance(record, dict) or record.get("kind") == records.ERASURE_KIND:
        return False
    rid = record.get(records.RECORD_ID_KEY)
    return isinstance(rid, str) and bool(rid) and rid in erasure_targets


def _reap_temps(data_dir: str) -> int:
    """Remove any leftover compaction temp (`.compact-*.ndjson`) — a crash between write and rename leaves a
    complete same-schema file that is NEVER the canonical ledger name, so it is ignored-and-reaped, never
    promoted (recovery binds to the fixed canonical name). Returns how many were reaped."""
    reaped = 0
    try:
        names = os.listdir(data_dir)
    except OSError:
        return 0
    for name in names:
        if name.startswith(_TEMP_PREFIX) and name.endswith(_TEMP_SUFFIX):
            try:
                os.remove(os.path.join(data_dir, name))
                reaped += 1
            except OSError:
                pass
    return reaped


def _fold_supersession(record, superseded_by_map: dict):
    """`record` with a CLOSED-batch gist supersession folded into its carried `superseded_by` field,
    or unchanged when nothing supersedes it. `superseded_by_map` (forget._superseded_by_map) maps a raw
    episode's id -> its gist id, built ONLY from closed-batch markers — so a raw enters it (and gets the carried
    field) only when its gist's roll-up completed. After the marker is pruned, `forget.live_records` still
    retires the raw via this field. The content-free id and every other field are preserved.

    THIS FOLD IS WHY THE PRUNE IS SAFE. Pruning a closed-batch `superseded` marker without writing this field
    would un-hide the raw episode the marker retired, surfacing a summary beside the source it replaced. The
    sibling fold that carried a frecency snapshot had no such duty and is gone with the scoring it fed:
    nothing reads a per-record score any more, so those markers are pruned outright."""
    if not isinstance(record, dict):
        return record
    rid = record.get(records.RECORD_ID_KEY)
    gist_id = superseded_by_map.get(rid) if isinstance(rid, str) and rid else None
    if not gist_id:
        return record
    folded = dict(record)
    folded[records.SUPERSEDED_BY_KEY] = gist_id
    return folded


def _write_compacted_temp(data_dir: str, raw_records,
                          closed_rollup: set, superseded_by_map: dict, erasure_targets: set,
                          torn_raw: "bytes | None" = None) -> str:
    """Write the folded, marker-pruned ledger to a fresh temp in `data_dir`, fsynced. Drops ONLY `reinforcement`
    markers AND CLOSED-batch `superseded` markers (both folded away into carried fields); every recall-content
    record (turn-delta, episodic, gist) + every `consolidated` marker + every UN-closed (crashed-pass)
    `superseded` marker survives (the Layer-1 fold never erases recall content, and an inert supersession is never
    folded — passed through verbatim so a crashed roll-up's hiding is never baked in). The ONE exception is
    Layer-2: a recall record whose stable id is in `erasure_targets` (a VALID merge-gated marker names
    it) is physically dropped — the single irreversible act; the marker itself is retained.

    A torn trailing fragment (`torn_raw`, from `ledger.read`) is PRESERVED: re-emitted verbatim as the final,
    un-terminated bytes — exactly as a normal read leaves it — so the whole-ledger swap never erases a crash-torn
    tail that a later append could heal (the ledger-integrity law: compaction is bound by the read law, not a
    privileged path around it). Returns the temp path."""
    tmp = os.path.join(data_dir, _TEMP_PREFIX + uuid.uuid4().hex + _TEMP_SUFFIX)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        for record in raw_records:
            if _is_foldable(record, closed_rollup):
                continue  # reinforcement / closed-batch supersession: folded into carried fields, then pruned
            if _is_erased(record, erasure_targets):
                continue  # Layer-2: a valid merge-gated erasure marker targets this record -> physically removed
            folded = _fold_supersession(record, superseded_by_map)
            line = json.dumps(folded, ensure_ascii=False, separators=(",", ":")) + "\n"
            os.write(fd, line.encode("utf-8"))
        if torn_raw:
            # Preserve the torn trailing fragment verbatim, un-terminated (no record terminator) — so the new
            # ledger ends in the same healable tail the old one did, and the next append heals it as always.
            # Looped like ledger.append (unlike the folded-record writes above): these are the exact recoverable
            # bytes this fix exists to preserve, so a partial write must never truncate the healable fragment.
            view = memoryview(torn_raw)
            while view:
                view = view[os.write(fd, view):]
        ledger._durable_fsync(fd)
    finally:
        os.close(fd)
    return tmp


def compact(path: "str | None" = None, *, now: "int | None" = None, _crash_after: "str | None" = None) -> dict:
    """Run one compaction pass over the ledger (the default store, or `path`). Returns a small report dict:
    `{status, folded, pruned, generation}` (or `{status: "busy", ...}` on lock contention — the pass retries
    later, never writes lock-free). Held under the single-writer `.capture.lock` across the whole
    read→fold→write→bump→swap→rebuild critical section, so a live append or a second compaction waits (or
    no-ops and retries) rather than interleaving — no committed append is lost and two compactions cannot race.

    `_crash_after` is a TEST/DEMO-only power-cut injector — `"write"` (after the temp is written, before the
    swap) or `"swap"` (after the swap, before the index rebuild). Production callers never pass it. A real
    power-cut at either point (the OS releases the lock on process death, which the `finally` mirrors here)
    leaves exactly one intact ledger — old for `"write"` (the tidy never took), new for `"swap"` — and a temp /
    stale index that the next pass reaps / the generation gate routes to the scan until rebuilt."""
    target = path if path is not None else ledger.ledger_path()
    data_dir = os.path.dirname(target) or "."
    index_dst = os.path.join(data_dir, _index_filename())
    from memory import capture  # lazy: keep capture off the module-load path (cycle discipline)
    os.makedirs(data_dir, exist_ok=True)
    lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
    if lock_fd is None:
        return {"status": "busy",
                "reason": "another memory write held the single-writer lock; compaction retries later",
                "folded": 0, "pruned": 0}
    try:
        # Ordering law: compaction must NOT run within a migration window. Checked HERE, under
        # the lock, so it sees a marker a migration raised (a file that outlives the migration's own brief lock
        # holds). A LIVE marker => refuse (retry later). An ORPHANED marker (the migrating process died, or it is
        # far past any real migration's span) is self-healed under this lock and compaction proceeds — compaction
        # is exactly what the marker blocks, so it is the natural recovery point.
        if capture.migration_in_flight(data_dir):
            return {"status": "busy",
                    "reason": "a memory migration is in progress; compaction waits until it finishes",
                    "folded": 0, "pruned": 0}
        capture.clear_orphaned_migration_locked(data_dir)
        _reap_temps(data_dir)                          # recovery: clear any prior-crash leftover, under the lock
        t0 = int(time.time()) if now is None else now
        # Read via the read-law reader (not the silent-skip iterator): it counts a malformed line and captures the
        # exact bytes of a torn trailing fragment, so the whole-ledger swap can preserve/report them instead of
        # erasing recoverable recall with erased:0. `records` is byte-identical to the old iter_records list here.
        health = ledger.read(path=target)
        raw = health.records
        closed_rollup = forget._closed_rollup_batches(target)          # roll-up batches a marker closed
        superseded_by_map = forget._superseded_by_map(target, closed_rollup)   # raw id -> gist id (closed batches only)
        erasure_targets = _erasure_targets(raw)        # ids a VALID merge-gated marker authorises removing
        pruned = sum(1 for r in raw if _is_foldable(r, closed_rollup))
        erased = sum(1 for r in raw if _is_erased(r, erasure_targets))   # recall content physically removed (Layer-2)
        folded = sum(1 for r in raw if isinstance(r, dict)
                     and r.get(records.RECORD_ID_KEY) in superseded_by_map)
        tmp = _write_compacted_temp(data_dir, raw, closed_rollup, superseded_by_map,
                                    erasure_targets, torn_raw=health.torn_raw)
        if _crash_after == "write":
            raise _InjectedCrash("write")              # power-cut: temp left, OLD ledger intact, gen unbumped
        ledger.bump_generation(for_path=target)        # bump BEFORE the swap (the crash-safe ordering)
        ledger.replace_ledger(tmp, path=target)        # fsync temp → atomic rename → fsync dir
        if _crash_after == "swap":
            raise _InjectedCrash("swap")               # power-cut: NEW ledger in place, gen bumped, index stale
        from memory import index                       # lazy: index imports forget; import at use, not load
        index.rebuild(ledger_file=target, index_file=index_dst)
        # `malformed`/`torn_preserved` make the pass honest about the read law: a malformed line is skipped-and-
        # counted (never a silent erased:0), a torn tail is preserved for a later append to heal. boot's memory-
        # health readout surfaces a rotting ledger from its OWN read; compaction stays a pure local pass (no
        # network here), so the count is reported, not promoted from under the lock.
        return {"status": "ok", "folded": folded, "pruned": pruned, "erased": erased,
                "malformed": health.malformed, "torn_preserved": bool(health.torn_raw),
                "generation": ledger.generation(for_path=target)}
    finally:
        capture._release_lock(lock_fd)                 # the OS frees the flock on a real power-cut; mirror it


def _index_filename() -> str:
    """The derived-index filename, read from index lazily (avoids importing index at module load)."""
    from memory import index
    return index.INDEX_FILENAME


# --- The live auto-trigger gate -----------------------------------------------------------

def reclaimable_waste(path: "str | None" = None) -> int:
    """How many foldable markers (reinforcement + CLOSED-batch supersessions) a compaction pass would prune RIGHT
    NOW — the reclaimable-waste signal the auto-trigger gates on, counted by the SAME `_is_foldable` predicate
    `compact` prunes by (so the gate can never fire when nothing folds). A LOCK-FREE read: two cheap O(ledger)
    passes (one to derive the closed roll-up batches, one to count) and it NEVER writes. The count is inherently
    a snapshot — a concurrent append can only shift it slightly, and `compact` re-derives everything under the
    single-writer lock — so the gate is advisory while the actual fold is always exact."""
    target = path if path is not None else ledger.ledger_path()
    closed_rollup = forget._closed_rollup_batches(target)
    return sum(1 for r in ledger.iter_records(path=target) if _is_foldable(r, closed_rollup))


def _gate_counts(path: "str | None" = None) -> tuple:
    """Both gate signals from ONE traversal: `(reclaimable_waste, pending_erasures)`. `maybe_compact` needs both
    on every PreCompact, and each answer is a full O(ledger) read — computing them separately read a 29 MB file
    twice for numbers one pass already has in hand, on a hook whose whole justification is that it is cheap.
    LOCK-FREE, never writes. The closed-rollup derivation stays its own pass: the fold predicate needs the whole
    set before it can classify any record."""
    target = path if path is not None else ledger.ledger_path()
    closed_rollup = forget._closed_rollup_batches(target)
    waste = 0
    wanted: set = set()
    present: set = set()
    for r in ledger.iter_records(path=target):
        if _is_foldable(r, closed_rollup):
            waste += 1
        if not isinstance(r, dict):
            continue
        if _is_erasure_marker(r):
            tid = r.get(records.TARGET_KEY)
            if isinstance(tid, str) and tid:
                wanted.add(tid)
            continue                       # a marker is never removable, so it is never "present" to erase
        rid = r.get(records.RECORD_ID_KEY)
        if isinstance(rid, str) and rid:
            present.add(rid)
    return waste, len(wanted & present)


def pending_erasures(path: "str | None" = None) -> int:
    """How many merge-authorised erasures are still WAITING to be carried out — valid `operator-adjudicated-erasure`
    markers whose target record is still resident in the ledger. A LOCK-FREE read (one O(ledger) pass), never writes.

    Counting "target still present" and not "marker present" is what keeps this from firing forever: `compact`
    removes the target but RETAINS the marker as an idempotency tombstone, so an already-enacted erasure leaves a
    marker whose target is gone, and this correctly reports nothing pending.

    "Present" mirrors what `compact` can actually REMOVE, which is why erasure markers are excluded from it:
    `_is_erased` refuses to remove a marker (that is what makes the tombstone durable), so a marker naming
    another marker's id would otherwise read as pending forever and rewrite the whole ledger on every pass. The
    counter's notion of resident must be the executor's notion of removable, or the gate cannot terminate."""
    target = path if path is not None else ledger.ledger_path()
    wanted: set = set()
    present: set = set()
    for r in ledger.iter_records(path=target):
        if not isinstance(r, dict):
            continue
        if _is_erasure_marker(r):
            tid = r.get(records.TARGET_KEY)
            if isinstance(tid, str) and tid:
                wanted.add(tid)
            continue                       # a marker is never removable, so it is never "present" to erase
        rid = r.get(records.RECORD_ID_KEY)
        if isinstance(rid, str) and rid:
            present.add(rid)
    return len(wanted & present)


def should_compact(path: "str | None" = None) -> bool:
    """True once the reclaimable waste reaches `_COMPACT_WASTE_THRESHOLD` — the gate that keeps the auto-trigger
    off a clean / low-waste ledger (else every `PreCompact` would rewrite a byte-identical ledger and rebuild the
    index for no gain). A pure lock-free read."""
    return reclaimable_waste(path) >= _COMPACT_WASTE_THRESHOLD


def maybe_compact(path: "str | None" = None) -> dict:
    """The auto-trigger memory's `PreCompact` hook rides: compact ONLY when enough waste has piled up, else a
    clean no-op. FAIL-OPEN by construction — it NEVER raises (any fault degrades to a skipped report so the host
    action, the context squash, always proceeds).

    It DOES carry out erasure: `compact` is the sole executor of physical removal (`_erasure_targets` /
    `_is_erased`), so every fire enacts whatever erasures already have a valid merge-gated marker. Consent is
    untouched by that — a marker exists only because the operator merged an `engine-erasure` pull request, and
    nothing here can mint one — but the TIMING is not this function's to choose on the operator's behalf.

    So a pending erasure fires the pass on its OWN account, ahead of the waste gate. Otherwise the answer to
    "when does the deletion actually happen?" would be "once roughly `_COMPACT_WASTE_THRESHOLD` units of
    unrelated bookkeeping happen to pile up" — an unbounded wait after the operator merged, with nothing
    surfacing that it had not happened yet. Consent honoured but not executed is its own defect, and it is the
    same reason `reap_orphaned_migration` below runs ahead of the gate.
    Returns the `compact` report on a fire, or `{"status": "skipped", ...}` when the gate holds it off or a fault
    is swallowed."""
    try:
        # Self-heal a crashed migration's orphaned marker on EVERY pass — before the waste gate — so the recovery
        # (and the boot heads-up that rode it) is NOT stranded on a low-waste ledger that never folds. `compact`
        # also clears it when it fires, but that only reaches here once waste crosses the threshold; this makes
        # "clears on its own the next time memory is tidied" true regardless of how dirty the ledger is.
        from memory import capture
        target = path if path is not None else ledger.ledger_path()
        capture.reap_orphaned_migration(os.path.dirname(target) or ".")
        waste, pending = _gate_counts(path)     # both signals, one read of the ledger
        if pending:
            # Say WHY it fired. Physical erasure is the one act here that cannot be undone, and without this
            # the report is indistinguishable from routine housekeeping — a caller (or a later reader of a
            # log) could not tell a tidy from a deletion having just been carried out.
            report = compact(path)
            if isinstance(report, dict):
                report["fired_for"] = "a merged erasure was waiting"
            return report
        if waste < _COMPACT_WASTE_THRESHOLD:
            return {"status": "skipped", "reason": "below the compaction threshold", "waste": waste}
        report = compact(path)
        if isinstance(report, dict):
            report["fired_for"] = "reclaimable bookkeeping had built up"
        return report
    except Exception as exc:   # fail-open: a maintenance fault must never strand the squash the hook rides
        return {"status": "skipped", "reason": f"compaction faulted, skipped: {exc}", "waste": -1}


# --- Layer-2 enactment: minting the merge-gated erasure marker ----------------------------
# `enact_erasure` is the SOLE minter of an `operator-adjudicated-erasure` marker. Its only live caller is the
# cross-session observer (`erasure_observer.enact_from_merged_prs`); there is DELIBERATELY no `erase`
# CLI verb on the real ledger, because that observer is the only design-sanctioned producer, and it mints a
# marker ONLY after reading a MERGED single-purpose erasure PR. So a marker appears only when the operator has
# merged such a PR; until then the removal set stays empty and `compact` stays the Layer-1 no-op-shaped tidy.

def enact_erasure(target_id: str, merge_sha: str, *, path: "str | None" = None, now: "int | None" = None):
    """Append an `operator-adjudicated-erasure` marker authorising the NEXT compaction to physically remove the
    record named by `target_id` — the single irreversible act, recorded with the `merge_sha` that authorised it.
    Returns the appended marker dict, or None on a no-op. APPENDS ONLY; never deletes or rewrites — the physical
    removal is `compact`'s, gated on this marker. A blank target OR a blank merge SHA is a no-op (the
    consent-provenance floor: no merge identity, no marker). Held under the shared single-writer `.capture.lock`
    on contention it is a clean no-op (the observer re-mints next session — the
    marker is idempotent), NEVER writing lock-free."""
    if not isinstance(target_id, str) or not target_id:
        return None
    if not isinstance(merge_sha, str) or not merge_sha:
        return None   # the consent-provenance floor: an erasure marker without its merge identity is never minted
    from memory import capture  # lazy: keep capture off the module-load path (cycle discipline)
    target = path if path is not None else ledger.ledger_path()
    data_dir = os.path.dirname(target) or "."
    os.makedirs(data_dir, exist_ok=True)
    lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
    if lock_fd is None:
        return None  # a compaction or live capture holds the single-writer lock — skip; never write lock-free
    try:
        marker = {
            "v": capture.RECORD_VERSION,
            "kind": records.ERASURE_KIND,
            records.RECORD_ID_KEY: records.new_record_id(),
            records.TARGET_KEY: target_id,
            records.MERGE_SHA_KEY: merge_sha,
            "ts": int(time.time()) if now is None else now,
            "tags": [records.ERASURE_TAG],
        }
        if marker[records.RECORD_ID_KEY] == target_id:
            return None   # a marker can never name itself (2^-128 paranoia guard on the uuid mint; cheap to pin)
        ledger.append(marker, path=path)
        return marker
    finally:
        capture._release_lock(lock_fd)


# --- Operator demonstration -------------------------------------------------------------------------------
# A walkthrough on a THROWAWAY practice cabinet (a temp folder), never real data. It runs the REAL fold + swap +
# rebuild + recall and reads the cabinet back, so every claim is recognizable words/counts on screen — and
# prints ONLY plain language (never "compaction"/"generation"/"frecency"/"snapshot"/"tier"/"index"). It proves
# the load-bearing promise: a power-cut mid-tidy never loses or corrupts your memory. PART 2 exercises BOTH
# crash points (just before, and just after, the tidied copy is put in place). Vary the notes/ages near the top
# and re-run:
#     uv run --directory .engine --frozen -- python tools/memory/compact.py demo
# (The `demo-trigger` walkthrough is a SEPARATE demo of WHEN the tidy fires on its own; this one is the tidy itself.)
_DEMO_SESSION = "session-harbor"
_DEMO_KEEP_TEXT = "Decided the harbor lights switch to solar next spring. DO-NOT-LOSE-THIS."
_DEMO_KEEP_WORD = "harbor"
_DEMO_KEEP2_TEXT = "Decided the ferry timetable moves to a 20-minute cadence in summer."
_DEMO_USED_TIMES = 4              # how many "used it" notes pile up behind the kept note
_DEMO_SET_ASIDE_TEXT = "Lesson: the old gantry crane jammed below freezing — never run it under 0C."
_DEMO_SET_ASIDE_WORD = "gantry"
_DEMO_SET_ASIDE_SUMMARY = "Summary: cold-weather rules for the yard machinery."
_DEMO_SET_ASIDE_AGE_DAYS = 40    # old enough to be worth folding into a summary; age alone never hides a note
_DEMO_DUP_TEXT = "Decided the festival route skips the drawbridge this year."
_DEMO_DUP_WORD = "festival"
_DEMO_DAY = 86400


def _ledger_path() -> str:
    return ledger.ledger_path()


def _all_records() -> list:
    return [r for r in ledger.iter_records(path=_ledger_path()) if isinstance(r, dict)]


def _content_records() -> list:
    """Every recall-bearing record in the cabinet (notes + summaries), regardless of recall visibility — the
    thing a rewrite must never lose."""
    keep = (records.EPISODIC_KIND, records.AMBIENT_CAPTURE_KIND)
    return [r for r in _all_records() if r.get("kind") in keep]


def _content_ids() -> set:
    return {r.get(records.RECORD_ID_KEY) for r in _content_records()}


def _bookkeeping_count() -> int:
    """The private 'used it' notes (reinforcement markers) physically in the cabinet."""
    return sum(1 for r in _all_records() if r.get("kind") == records.REINFORCEMENT_KIND)


def _recall_count(word: str) -> int:
    from memory import index
    return len(index.query(word).records)


def _in_cabinet(record_id: str) -> int:
    return sum(1 for r in _all_records() if r.get(records.RECORD_ID_KEY) == record_id)


def _scratch_copies(data_dir: str) -> int:
    try:
        return sum(1 for n in os.listdir(data_dir) if n.startswith(_TEMP_PREFIX) and n.endswith(_TEMP_SUFFIX))
    except OSError:
        return 0


def _cabinet_whole() -> "tuple[bool, int]":
    """(is the one canonical cabinet present and complete — no torn final entry, AND, int — how many notes it
    holds)."""
    report = ledger.read(path=_ledger_path())
    whole = os.path.exists(_ledger_path()) and not report.torn_trailing
    return whole, len(report.records)


def _make_episodic(text: str, age_days: int, role: str = "decision", batchless: bool = True) -> dict:
    """A real episodic through the live factory, back-dated by `age_days`. `batchless` makes it always-live (not
    a crashed-pass orphan); pass batchless=False to leave its batch unclosed (a retired duplicate)."""
    rec = _legacy.episodic(_DEMO_SESSION, role, text, "demo-batch")
    if batchless:
        rec.pop(records.BATCH_KEY, None)
    rec["ts"] = int(time.time()) - age_days * _DEMO_DAY
    return rec


def _rebuild() -> None:
    from memory import index
    index.rebuild()


def _snippet(text, width: int = 66) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _demo() -> int:
    import tempfile

    print("=" * 88)
    print("MEMORY — tidying away the private bookkeeping, safely, even if the power cuts out mid-tidy (practice)")
    print("=" * 88)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet
        try:
            ok = _demo_body(tmp)
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 88)
    print("What this just proved: the engine can tidy away the private 'used it' bookkeeping that piles up — and")
    print("a power-cut in the MIDDLE of that tidy never loses or corrupts a single note: you always end with one")
    print("whole filing cabinet (the old one if the tidy hadn't finished, the new one if it had), never a")
    print("half-written mess. Your real notes — even ones set aside from search, even a recovered duplicate —")
    print("all survive the rewrite. The engine now runs this tidy on its OWN — automatically, once enough private")
    print("bookkeeping has piled up (the `demo-trigger` walkthrough shows exactly when it fires and when it holds")
    print("off). NOTHING is ever erased here: tidying only removes the private bookkeeping; permanently erasing a")
    print("real note is a SEPARATE step you approve later by merging a pull request (and applying the")
    print("`guardrail-ack` safety label) — never this. That was a PRACTICE cabinet, thrown away when the demo")
    print("ended; like all memory, private, local, and deletable. Vary it: edit the notes/ages near the top and re-run.")
    return 0 if ok else 1


def _demo_body(data_dir: str) -> bool:
    # --- PART 1 ------------------------------------------------------------------------------------------
    print("\nPART 1 — the private bookkeeping piles up, the tidy clears it, your note is untouched")
    print("-" * 88)
    keep = _make_episodic(_DEMO_KEEP_TEXT, age_days=0)
    keep_id = keep[records.RECORD_ID_KEY]
    ledger.append(keep)
    for _ in range(_DEMO_USED_TIMES):
        ledger.append(_legacy.reinforcement(keep_id))
    _rebuild()
    book_before = _bookkeeping_count()
    found_before = _recall_count(_DEMO_KEEP_WORD)
    print(f'  a note from an older engine, with its private bookkeeping behind it: "{_snippet(_DEMO_KEEP_TEXT)}"')
    print(f"  spent 'used it' entries piled up behind it: {book_before}")
    print(f'  search "{_DEMO_KEEP_WORD}" -> found {found_before}')
    report = compact()
    book_after = _bookkeeping_count()
    found_after = _recall_count(_DEMO_KEEP_WORD)
    print(f"  ...tidied. status: {report['status']}")
    print(f"  spent 'used it' entries now: {book_after}   (reclaimed — no longer cluttering the cabinet)")
    print(f'  search "{_DEMO_KEEP_WORD}" -> found {found_after}')
    # The note must come out BYTE-IDENTICAL: the tidy reclaims spent bookkeeping and carries nothing onto the
    # record itself, so this compares the whole thing rather than a summary of it.
    kept_after = next((r for r in ledger.iter_records() if r.get(records.RECORD_ID_KEY) == keep_id), None)
    part1 = (book_before >= _DEMO_USED_TIMES and book_after == 0 and found_after == 1
             and kept_after == keep)
    print(f"  => {'reclaimed the bookkeeping; the note itself is untouched, byte for byte.' if part1 else '!!! the note changed or the bookkeeping was not reclaimed'}")

    # --- PART 2 ------------------------------------------------------------------------------------------
    print("\nPART 2 — a power-cut in the MIDDLE of the tidy never loses or corrupts your memory")
    print("-" * 88)
    ledger.append(_make_episodic(_DEMO_KEEP2_TEXT, age_days=0))      # a second real note, so 'N of N' is plural
    for _ in range(_DEMO_USED_TIMES):                  # pile the bookkeeping back up so there is something to tidy
        ledger.append(_legacy.reinforcement(keep_id))
    _rebuild()
    ids_before = _content_ids()
    print(f"  before the power-cut: {len(ids_before)} real notes in the cabinet, bookkeeping piled up again")

    print("\n  (a) the power cuts out JUST BEFORE the tidied copy is put in place")
    try:
        compact(_crash_after="write")
    except _InjectedCrash:
        pass
    whole_a, count_a = _cabinet_whole()
    present_a = _content_ids() == ids_before
    print(f"    the one filing cabinet is here and complete (no half-written entry): {'yes' if whole_a else 'NO'}")
    print(f"    entries on file: {count_a}  (the kept note + its bookkeeping)")
    print(f"    leftover half-finished scratch copies (ignored; cleaned up next tidy): {_scratch_copies(data_dir)}")
    print(f"    the DO-NOT-LOSE-THIS note still reads back: {'yes' if _in_cabinet(keep_id) >= 1 else 'NO'}")
    print(f"    every real note still present: {len(_content_ids())} of {len(ids_before)}")
    print(f"    the bookkeeping is still here (the tidy did NOT take): {_bookkeeping_count()}")
    part2a = whole_a and present_a and _bookkeeping_count() >= _DEMO_USED_TIMES
    print(f"    => {'the cabinet is UNCHANGED — one whole cabinet, nothing lost.' if part2a else '!!! the before-swap crash lost or changed something'}")

    print("\n  (b) the power comes back, it tries again, and cuts out JUST AFTER the tidied copy is put in place")
    try:
        compact(_crash_after="swap")
    except _InjectedCrash:
        pass
    whole_b, count_b = _cabinet_whole()
    present_b = _content_ids() == ids_before
    found_b = _recall_count(_DEMO_KEEP_WORD)           # answered by the slow backup (the quick-search isn't rebuilt yet)
    print(f"    the one filing cabinet is here and complete (no half-written entry): {'yes' if whole_b else 'NO'}")
    print(f"    entries on file: {count_b}  (the kept note; bookkeeping folded away)")
    print(f"    leftover half-finished scratch copies: {_scratch_copies(data_dir)}")
    print(f"    the DO-NOT-LOSE-THIS note still reads back: {'yes' if _in_cabinet(keep_id) >= 1 else 'NO'}")
    print(f"    every real note still present: {len(_content_ids())} of {len(ids_before)}")
    print(f"    the bookkeeping is now folded away (the tidy DID take): {_bookkeeping_count()}")
    print(f'    search still answers "{_DEMO_KEEP_WORD}" (via the slow backup until the fast search is refreshed): {found_b}')
    part2b = whole_b and present_b and _bookkeeping_count() == 0 and found_b == 1
    print(f"    => {'the tidied cabinet is in place and complete — one whole cabinet, nothing lost.' if part2b else '!!! the after-swap crash lost or changed something'}")

    # --- PART 3 ------------------------------------------------------------------------------------------
    print("\nPART 3 — nothing is erased: a set-aside note and a recovered duplicate both survive the rewrite")
    print("-" * 88)
    # A note is set aside from search when a SUMMARY is written over it — never merely by going unused, which
    # is why this plants a real roll-up rather than an old note. That is also the harder case for the rewrite:
    # compaction folds the supersession onto the note and prunes the marker that recorded it, so "still hidden
    # afterwards" is a genuine property to check rather than a restatement of the note's age.
    aside = _make_episodic(_DEMO_SET_ASIDE_TEXT, age_days=_DEMO_SET_ASIDE_AGE_DAYS, role="lesson")
    aside_id = aside[records.RECORD_ID_KEY]
    ledger.append(aside)
    _legacy.store_gist(_DEMO_SESSION, _DEMO_SET_ASIDE_SUMMARY, [aside_id])
    dup = _make_episodic(_DEMO_DUP_TEXT, age_days=0, batchless=False)   # a crashed-pass orphan (batch never closed)
    dup_id = dup[records.RECORD_ID_KEY]
    ledger.append(dup)
    _rebuild()
    aside_hidden_before = _recall_count(_DEMO_SET_ASIDE_WORD) == 0
    dup_retired_before = dup_id not in {r.get(records.RECORD_ID_KEY) for r in forget.live_records()}
    compact()
    aside_in = _in_cabinet(aside_id)
    dup_in = _in_cabinet(dup_id)
    aside_hidden_after = _recall_count(_DEMO_SET_ASIDE_WORD) == 0
    dup_retired_after = dup_id not in {r.get(records.RECORD_ID_KEY) for r in forget.live_records()}
    print(f"  a note a summary was written over: \"{_snippet(_DEMO_SET_ASIDE_TEXT)}\"")
    print(f"    still in the cabinet after the rewrite: {aside_in}    still hidden from search: {'yes' if aside_hidden_after else 'NO'}")
    print(f"  a recovered duplicate (a save that didn't finish): \"{_snippet(_DEMO_DUP_TEXT)}\"")
    print(f"    still in the cabinet after the rewrite: {dup_in}    still kept out of recall: {'yes' if dup_retired_after else 'NO'}")
    part3 = (aside_in == 1 and dup_in == 1 and aside_hidden_before and aside_hidden_after
             and dup_retired_before and dup_retired_after)
    print(f"  => {'every real note survived the rewrite; only the private bookkeeping was removed.' if part3 else '!!! a real note was lost or its state changed'}")

    return part1 and part2a and part2b and part3


# --- demo-trigger: WHEN the tidy fires on its own (the gate + fail-open) ----------------------------------
# A SEPARATE walkthrough from `demo` (which proves the tidy itself is crash-safe). This one proves the GATE: the
# tidy fires when enough private bookkeeping has piled up (and, separately, whenever an erasure the operator has
# already merged is waiting), leaves an otherwise clean-enough cabinet untouched, and — if
# the tidy ever faults — the engine carries on and loses nothing. Dial the two pile numbers below (or the
# threshold near the top) and watch the "skipped"/"fired" flip:
#     uv run --directory .engine --frozen -- python tools/memory/compact.py demo-trigger
_DEMO_TRIGGER_BELOW = max(1, _COMPACT_WASTE_THRESHOLD - 8)   # a pile JUST UNDER the line -> the tidy is SKIPPED
_DEMO_TRIGGER_ABOVE = _COMPACT_WASTE_THRESHOLD + 8           # a pile OVER the line -> the tidy FIRES
_DEMO_TRIGGER_TEXT = "Decided the lighthouse keeper rota rotates every fortnight. DO-NOT-LOSE-THIS."
_DEMO_TRIGGER_WORD = "lighthouse"


def _demo_trigger() -> int:
    import tempfile

    print("=" * 88)
    print("MEMORY — the tidy runs ON ITS OWN, but only when there's enough to clear, and never loses a note (practice)")
    print("=" * 88)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet
        try:
            ok = _demo_trigger_body()
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 88)
    print("What this just proved: the engine now tidies its private 'used it' bookkeeping ON ITS OWN — but ONLY once")
    print("enough has piled up; with a clean-enough cabinet it does nothing and leaves your filing exactly as it was.")
    print("When it does fire, every real note is still here and still findable. If the tidy ever hits a snag, the")
    print("engine just carries on — it never holds anything up. This tidy is PURELY MECHANICAL filing: no AI reads,")
    print("judges, or rewrites anything here, and none of your actual notes change — it only folds away the private")
    print("bookkeeping. (The AI-written summaries are a SEPARATE background job, the roll-up sweep; it never runs at")
    print("this moment.) This runs automatically on your real sessions — at the moment your conversation's context")
    print("is compacted — in the background, with no further approval each time. Nothing is erased by the tidying")
    print("itself; permanently erasing")
    print("a real note is a separate step you approve by merging a pull request (and applying the `guardrail-ack`")
    print("label). Practice cabinet, thrown away. Vary it: change the two pile numbers near the top (or the")
    print("threshold) and watch the tidy flip between skipped and fired.")
    return 0 if ok else 1


def _demo_trigger_body() -> bool:
    from unittest import mock

    # --- PART 1 ------------------------------------------------------------------------------------------
    print("\nPART 1 — a clean-enough cabinet: the tidy is SKIPPED and your filing is left untouched")
    print("-" * 88)
    keep = _make_episodic(_DEMO_TRIGGER_TEXT, age_days=0)
    keep_id = keep[records.RECORD_ID_KEY]
    ledger.append(keep)
    for _ in range(_DEMO_TRIGGER_BELOW):
        ledger.append(_legacy.reinforcement(keep_id))
    _rebuild()
    waste_below = reclaimable_waste()
    version_before = ledger.generation()
    ids_before = _content_ids()
    report1 = maybe_compact()                          # the REAL auto-trigger
    untouched = (ledger.generation() == version_before and _content_ids() == ids_before)
    print(f"  private bookkeeping piled up: {waste_below}   (that alone fires the tidy at {_COMPACT_WASTE_THRESHOLD};")
    print("   an erasure you already approved fires it whatever the pile is)")
    print(f"  the engine's own decision: {report1['status']}   (it chose NOT to tidy)")
    print(f"  the filing cabinet is left exactly as it was (nothing rewritten): {'yes' if untouched else 'NO'}")
    part1 = report1["status"] == "skipped" and untouched
    print(f"  => {'below the line, so the engine left everything alone — no needless rewrite.' if part1 else '!!! the tidy ran (or changed the cabinet) when it should have skipped'}")

    # --- PART 2 ------------------------------------------------------------------------------------------
    print("\nPART 2 — enough has piled up: the tidy FIRES on its own, and every real note survives")
    print("-" * 88)
    ledger.append(_make_episodic("Decided the south jetty repaint waits for calmer weather.", age_days=0))
    for _ in range(_DEMO_TRIGGER_ABOVE):               # pile MORE on top of Part 1's, well over the line
        ledger.append(_legacy.reinforcement(keep_id))
    _rebuild()
    waste_above = reclaimable_waste()
    content_before = _content_ids()
    fires = should_compact()
    report2 = maybe_compact()                          # the REAL auto-trigger
    kept_found = _recall_count(_DEMO_TRIGGER_WORD)
    content_after = _content_ids()
    waste_now = reclaimable_waste()
    print(f"  private bookkeeping piled up: {waste_above}   (now well over {_COMPACT_WASTE_THRESHOLD})")
    print(f"  the engine's own decision: {report2['status']}   (it chose to tidy)")
    print(f"  every real note still present after the tidy: {len(content_after)} of {len(content_before)}")
    print(f'  the DO-NOT-LOSE-THIS note is still findable: search "{_DEMO_TRIGGER_WORD}" -> found {kept_found}')
    print(f"  private bookkeeping after the tidy: {waste_now}   (folded away)")
    part2 = (fires and report2["status"] == "ok" and content_after == content_before
             and len(content_after) == 2 and kept_found == 1 and waste_now == 0)
    print(f"  => {'over the line, so the engine tidied on its own — every real note survived, bookkeeping cleared.' if part2 else '!!! the tidy failed to fire, lost a note, or left the bookkeeping'}")

    # --- PART 3 ------------------------------------------------------------------------------------------
    print("\nPART 3 — if the tidy ever hits a snag, the engine carries on and loses nothing")
    print("-" * 88)
    for _ in range(_DEMO_TRIGGER_ABOVE):               # pile the bookkeeping back up so the tidy WOULD fire
        ledger.append(_legacy.reinforcement(keep_id))
    _rebuild()
    ids_pre_fault = _content_ids()
    with mock.patch.object(sys.modules[__name__], "compact",
                           side_effect=RuntimeError("disk hiccup (simulated)")):
        report3 = maybe_compact()                      # the REAL auto-trigger, with the underlying tidy faulting
    whole, _count = _cabinet_whole()
    survived = _content_ids() == ids_pre_fault
    print("  the underlying tidy was forced to fail.")
    print(f"  the engine carried on (no error raised to the session): {'yes' if report3['status'] == 'skipped' else 'NO'}")
    print(f"  the filing cabinet is whole and every real note survived: {'yes' if (whole and survived) else 'NO'}")
    part3 = report3["status"] == "skipped" and whole and survived
    print(f"  => {'a snag in the tidy never stalls you and never loses a note — the engine just moves on.' if part3 else '!!! a fault stranded the session or lost a note'}")

    return part1 and part2 and part3


# --- demo-erase: the ONE irreversible act — permanently erasing a note you authorised --------------------
# A SEPARATE walkthrough proving Layer-2: a note you AUTHORISED is permanently erased at the next tidy; a note you
# did NOT authorise is untouched; running the tidy again changes nothing; and the slip that authorises it names the
# note by a private tag, never its words. Vary which note is authorised (by its readable word) at the top and re-run:
#     uv run --directory .engine --frozen -- python tools/memory/compact.py demo-erase
_DEMO_ERASE_KEEP_TEXT = "Decided the harbor festival keeps its Saturday fireworks. KEEP-THIS-NOTE."
_DEMO_ERASE_KEEP_WORD = "fireworks"
_DEMO_ERASE_GONE_TEXT = "Withdrawn idea: move the depot onto the floodplain. ERASE-THIS-NOTE."
_DEMO_ERASE_GONE_WORD = "floodplain"
_DEMO_ERASE_AUTHORISED = "gone"   # which note you authorised erasing: "gone" (default) or "keep" — VARY and re-run


def _slip_count() -> int:
    """How many permanent-erase slips (erasure markers) are physically on file."""
    return sum(1 for r in _all_records() if r.get("kind") == records.ERASURE_KIND)


def _slip_mentions_word(word: str) -> bool:
    """True iff any erase-slip's stored form contains the note's distinctive word — it must NOT (a slip names the
    note only by a private content-free tag, never its words; the memory-privacy promise)."""
    w = (word or "").lower()
    return any(w in json.dumps(r, ensure_ascii=False).lower()
               for r in _all_records() if r.get("kind") == records.ERASURE_KIND)


def _demo_erase() -> int:
    import tempfile

    print("=" * 88)
    print("MEMORY — permanently erasing a note you authorised: the ONE thing that cannot be undone (practice)")
    print("=" * 88)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet
        try:
            ok = _demo_erase_body()
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 88)
    print("What this just proved: the engine can PERMANENTLY erase a note — but ONLY the one you authorised, and the")
    print("note you did not authorise is left exactly as it was. The slip that authorises an erase names the note by a")
    print("private tag, never its words. And running the tidy again changes nothing — an erase happens once and only")
    print("once. This is the ONE thing the engine can do to your memory that cannot be undone, and it happens ONLY")
    print("because you merged a single-purpose pull request asking for exactly that note. From time to time the")
    print("engine opens such a pull request itself, proposing notes it judges you no longer need — and once you")
    print("merge one, the engine does write the slip and carry the erase out. What it can never do is merge:")
    print("your merge is the whole of the authorisation, and nothing in the engine can supply it for you.")
    print("That was a PRACTICE cabinet, thrown away when the demo ended. Vary it: at the top, switch which note is")
    print("authorised (by its word) and re-run — the erase follows your choice; the note you did not authorise survives.")
    return 0 if ok else 1


def _demo_erase_body() -> bool:
    # plant the two notes; pick which is authorised-for-erasing by its readable word (varyable at the top)
    keep = _make_episodic(_DEMO_ERASE_KEEP_TEXT, age_days=0)
    gone = _make_episodic(_DEMO_ERASE_GONE_TEXT, age_days=0)
    ledger.append(keep)
    ledger.append(gone)
    _rebuild()
    if _DEMO_ERASE_AUTHORISED == "keep":
        target, target_word, other_text, other_word = keep, _DEMO_ERASE_KEEP_WORD, _DEMO_ERASE_GONE_TEXT, _DEMO_ERASE_GONE_WORD
    else:
        target, target_word, other_text, other_word = gone, _DEMO_ERASE_GONE_WORD, _DEMO_ERASE_KEEP_TEXT, _DEMO_ERASE_KEEP_WORD
    target_id = target[records.RECORD_ID_KEY]

    # --- PART 1 ------------------------------------------------------------------------------------------
    print("\nPART 1 — two real notes are on file, both findable")
    print("-" * 88)
    found_target = _recall_count(target_word)
    found_other = _recall_count(other_word)
    print(f'  the note you will authorise erasing: "{_snippet(target.get("text") or _DEMO_ERASE_GONE_TEXT)}"')
    print(f'    search "{target_word}" -> found {found_target}')
    print(f'  the other note (you will NOT authorise it): "{_snippet(other_text)}"')
    print(f'    search "{other_word}" -> found {found_other}')
    part1 = found_target == 1 and found_other == 1
    print(f"  => {'both notes are on file and findable.' if part1 else '!!! a note is missing at the start'}")

    # --- PART 2 ------------------------------------------------------------------------------------------
    print("\nPART 2 — you authorise erasing ONE note; the slip names it by a private tag, never its words")
    print("-" * 88)
    slip = enact_erasure(target_id, merge_sha="demo-merge-authorisation")   # in real use: created ONLY when you merge a PR
    slips = _slip_count()
    leak = _slip_mentions_word(target_word)
    print("  an erase-slip authorising this one note is now on file")
    print("    (in real use, this slip is created ONLY when you merge a single-purpose pull request authorising it)")
    print(f"  permanent-erase slips on file: {slips}")
    print(f'  does the slip contain the note\'s words? search the slip for "{target_word}" -> {"YES (leak!)" if leak else "no — named by a private tag only"}')
    part2 = slip is not None and slips == 1 and not leak
    print(f"  => {'one slip on file, carrying none of the note words.' if part2 else '!!! the slip is missing or it leaked the note words'}")

    # --- PART 3 ------------------------------------------------------------------------------------------
    print("\nPART 3 — the tidy enacts it: the authorised note is permanently gone; the other is untouched")
    print("-" * 88)
    report = compact()
    target_gone = _in_cabinet(target_id) == 0
    found_target_after = _recall_count(target_word)
    found_other_after = _recall_count(other_word)
    print(f'  the authorised note: search "{target_word}" -> found {found_target_after}   (physically gone: {"yes" if target_gone else "NO"})')
    print(f'  the other note: search "{other_word}" -> found {found_other_after}   (untouched)')
    print(f"  erase-slips still on file: {_slip_count()}  (kept as proof that this was authorised)")
    part3 = (target_gone and found_target_after == 0 and found_other_after == 1
             and report.get("erased") == 1 and _slip_count() == 1)
    print(f"  => {'the note you authorised is permanently gone; the other is exactly as it was.' if part3 else '!!! the wrong note changed, or nothing was erased'}")

    # --- PART 4 ------------------------------------------------------------------------------------------
    print("\nPART 4 — running the tidy again changes nothing: once, and only once")
    print("-" * 88)
    report2 = compact()
    other_still_here = _recall_count(other_word)
    target_still_gone = _in_cabinet(target_id) == 0
    print(f"  the other note is still here: found {other_still_here}")          # gate: the KEPT note's count must hold
    print(f'  the erased note is still gone: {"yes" if target_still_gone else "NO"}')
    print(f"  this tidy erased (again): {report2.get('erased')}   (0 — the erase already happened once)")
    part4 = other_still_here == 1 and target_still_gone and report2.get("erased") == 0
    print(f"  => {'the other note is untouched and the erase did not repeat — once and only once.' if part4 else '!!! the kept note changed or the erase repeated'}")

    return part1 and part2 and part3 and part4


def _pre_compact_handler(payload) -> dict:
    """The PreCompact hook: deterministic ledger housekeeping at a tolerable moment, never on the hot path.

    It rode the consolidation sweep's module until that module was deleted with the curation lifecycle, and it
    is HERE now because it was that sweep's only surviving job — and a load-bearing one. `maybe_compact` is the
    single production trigger for physical erasure: the cross-session observer mints an
    `operator-adjudicated-erasure` marker when the operator merges an erasure pull request, and this is what
    then removes the bytes. Left unwired, an approved erasure would be recorded and never carried out.

    Fires regardless of accumulated waste when an erasure is pending, so an approved deletion never waits on
    unrelated housekeeping. `maybe_compact` is fail-open and never raises; this handler ALWAYS proceeds —
    PreCompact must never block the squash."""
    maybe_compact()               # a pending merged erasure, or the waste gate; report dropped (a leaf renders
                                  # no prose); fail-open
    return hooks.proceed()


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "pre-compact":
        return hooks.run_hook("PreCompact", _pre_compact_handler)
    if cmd == "run":                                   # the manual lever: a REAL gated compaction on the real ledger
        report = maybe_compact()
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report.get("status") in ("ok", "skipped", "busy") else 1   # busy = a transient, not an error
    if cmd == "demo":
        return _demo()
    if cmd == "demo-trigger":
        return _demo_trigger()
    if cmd == "demo-erase":
        return _demo_erase()
    print(f"usage: compact.py [pre-compact|run|demo|demo-trigger|demo-erase]\nunknown command {cmd!r}",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
