"""forget.py — the engine's active forgetting: Layer-1 logical retirement over the memory ledger.

Active forgetting is **two-layered**. This module is **Layer 1** — *reversible, mechanical,
memory-autonomous* tidying that needs no human gate because **nothing is lost**: a forgotten record is
excluded from recall but stays **resident and fully recoverable in the one canonical ledger**. (Layer 2 —
irreversible physical erasure, gated on the operator's merge of a single-purpose erasure PR — lives in
`compact.py` with `erase.py` / `erasure_observer.py`; this module deliberately has **no** erasure /
ledger-delete code path, a build-conformance invariant a test pins.)

The **logical retirement of crash-duplicate summaries** — legacy, and still load-bearing. A summarising pass
that crashed after its episodic summaries were appended but before its `consolidated` marker landed left two
passes in the ledger for one session ("a duplicate, never a loss"). `live_records` retires the orphaned pass
from recall: an episodic whose `batch` carries no closing marker is dropped, while the marked (completed) pass
is kept. Nothing writes either shape now, but every store that has been running holds them, and the retirement
is **derived from the ledger** (the batch↔marker linkage), never stored only in the throwaway index, so it
survives a rebuild.

Leaf discipline: this module RETURNS records / a report and renders no operator-facing prose
(boot/audits own that). Both recall paths — the FTS5 `rebuild` and the plain `_scan` — consume `live_records`,
so the fast and slow lookups retire identically (the parity law, index.py). `index.extend` applies the same
membership predicates when capture adds a turn to an already-built index — the injected-capture exclusion, and
the operator's withholds as the index itself recorded them — so the incremental path cannot admit what a full
rebuild would drop. That second half is what keeps "forget this conversation", said while the conversation is
still going, from being undone by its own next turn. stdlib-only.

**Nothing here decides membership by age, and nothing scores a record.** `live_records` once dropped whatever a
frecency score put in an "archived" tier, retiring a never-recalled record somewhere between 26 and 33 days old.
That is gone for every kind, and so is the score itself: the canonical record is now the conversation
(eADR-0038), and a captured turn could never have earned its way out of an age-out, because nothing reinforces
what nothing could recall. Membership no longer depends on the clock at all.

**Reinforcement markers are a legacy shape, not a live mechanism.** Recall used to append a `reinforcement`
marker naming each record it returned, and that record's own module ranked by it. Nothing writes one any more.
`live_records` still drops them, because a store that has been running holds thousands and none of them is a
recall result — the same reason it drops every other bookkeeping marker.

The **logical retirement of gist-rolled-up episodes** is legacy in exactly the same way. An AI-judged pass once
folded old EPISODIC summaries of one session into a compact GIST and superseded the raws — a per-raw
`superseded` marker naming the raw by its stable id and the gist by `superseded_by`, under a roll-up `batch`
closed by a `rolled-up` marker. Nothing writes those either. `live_records` still retires a raw whose
supersession's batch is CLOSED (its gist is intact), keyed on the ROLL-UP closed set (`_closed_rollup_batches`),
so a crash before the closing marker leaves every supersession inert and no raw is hidden without its gist; an
orphaned GIST of a crashed roll-up is itself retired (`_is_gist_orphan`). Those two predicates stay because the
records they govern are in every store that has been running — dropping them would surface a summary alongside
the source it replaced. On a store that never had a roll-up they cost nothing and match nothing. The retired raw
stays resident and fully recoverable; physical erasure is Layer-2.

The stable, content-free record id is minted in the record factories (records/capture), not here; this module
hosts its operator demo (the `identity` verb). Ledger compaction — the rebuild-and-swap that physically removes
what a merged erasure pull request authorised — lives in `compact.py` (it needs the atomic file-replace
primitive the Layer-1 erasure-free source-scan bans HERE); Layer-2 audit-gated erasure is that separate, gated
path. Cost: `_derive_membership` makes ONE raw pass collecting every exclusion set, and `live_records` streams a
second applying them.
"""

from __future__ import annotations

import math
import os
import sys
import time

# Make the package parent (.engine/tools) importable so `from memory import ledger` resolves even when this
# file is run directly as the demo script. Imported as `memory.forget`, the parent is already on sys.path, so
# this is a guarded no-op. Module-level imports stay limited to the cycle-free set `ledger` + `records` (neither
# imports anything that reaches back to `forget`): `index` imports THIS module for the fold, so importing
# `index`/`capture` here would cycle — the demos import them lazily. Nothing here scores a record at all
# any more: the scorer is gone with the archived-tier age-out it fed.
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import ledger, records  # noqa: E402


def _closed_batches(src: str) -> set:
    """The set of `batch` ids that a *completed* pass closed — i.e. carried by a `consolidated` marker."""
    closed = set()
    for record in ledger.iter_records(path=src):
        if isinstance(record, dict) and record.get("kind") == records.MARKER_KIND:
            batch = record.get(records.BATCH_KEY)
            if isinstance(batch, str) and batch:
                closed.add(batch)
    return closed


def _closed_rollup_batches(src: str) -> set:
    """The set of roll-up `batch` ids a *completed* roll-up closed — carried by a `rolled-up` marker. DISTINCT from `_closed_batches` (which reads `consolidated` markers): the two closure namespaces
    never mix — a consolidation and a gist roll-up can never cross-close — and uuid batch ids are globally
    unique anyway, so the disjointness is belt-and-suspenders. One pass over the RAW ledger (like
    `_closed_batches`): a roll-up's supersessions take effect ONLY once their batch is in this set."""
    closed = set()
    for record in ledger.iter_records(path=src):
        if isinstance(record, dict) and record.get("kind") == records.ROLLUP_KIND:
            batch = record.get(records.BATCH_KEY)
            if isinstance(batch, str) and batch:
                closed.add(batch)
    return closed


def _superseded_by_map(src: str, closed_rollup: set) -> dict:
    """Map each raw episode's id -> the gist id that superseded it, from `superseded` markers whose roll-up
    `batch` is CLOSED. One pass over the **RAW** ledger — `ledger.iter_records`, NOT
    `live_records` — exactly as `_closed_batches` reads markers raw: a `superseded` marker is
    itself dropped from recall (`_is_bookkeeping`), so deriving the supersession off the filtered stream would
    find no markers at all and silently un-hide every folded raw. A marker in an un-closed (crashed-pass)
    batch is INERT and never enters the map — the load-bearing crash-safety:
    a raw is hidden only once its gist's pass completed. Skips malformed entries (a fallen line never costs the
    records after it)."""
    out: dict = {}
    for record in ledger.iter_records(path=src):
        if not isinstance(record, dict) or record.get("kind") != records.SUPERSEDED_KIND:
            continue
        batch = record.get(records.BATCH_KEY)
        if not isinstance(batch, str) or batch not in closed_rollup:
            continue
        raw_id = record.get(records.TARGET_KEY)
        gist_id = record.get(records.SUPERSEDED_BY_KEY)
        if isinstance(raw_id, str) and raw_id and isinstance(gist_id, str) and gist_id:
            out[raw_id] = gist_id
    return out


def _is_retired(record, closed: set) -> bool:
    """True iff `record` is an episodic summary orphaned by a crashed pass — its `batch` is set but no marker
    closed it. Everything else stays live: turn-deltas, markers, batch-less records (any record from before batched passes), and
    the episodics of a completed (marked) pass. A batch-less episodic is ALWAYS live — there is no crash
    duplicate to resolve there, and nothing folds what it cannot key. The batch↔marker match is intentionally
    GLOBAL (not per-session): a `batch` is a uuid, so a cross-session collision is a 2**-128 non-event, and the
    failure direction is fail-safe anyway — a stray match could only ever KEEP a duplicate, never lose a record."""
    if not isinstance(record, dict) or record.get("kind") != records.EPISODIC_KIND:
        return False
    batch = record.get(records.BATCH_KEY)
    if not isinstance(batch, str) or not batch:
        return False
    return batch not in closed


def _is_gist_orphan(record, closed_rollup: set) -> bool:
    """True iff `record` is a GIST orphaned by a crashed roll-up pass — its `batch` is set but no closing
    `rolled-up` marker landed. The mirror of `_is_retired` for a gist, but keyed on the ROLL-UP
    closed set (`closed_rollup`), NEVER the consolidation `closed` set: a gist's batch is closed by a `rolled-up`
    marker, which `_closed_batches` never sees, so keying it on the consolidation set would wrongly retire EVERY
    completed gist. A batchless or closed gist stays live (and recall-scored like an episodic). So a crashed
    roll-up shows neither the orphan gist nor hides its raws — exactly one intact state."""
    if not isinstance(record, dict) or record.get("kind") != records.GIST_KIND:
        return False
    batch = record.get(records.BATCH_KEY)
    if not isinstance(batch, str) or not batch:
        return False
    return batch not in closed_rollup


def _is_superseded(record, superseded_ids: set) -> bool:
    """True iff recall should drop `record` because a COMPLETED roll-up consolidated it into a gist — either a closed-batch `superseded` marker names it (`superseded_ids`, derived from the RAW ledger
    by `_superseded_by_map`) OR it carries the folded `superseded_by` field a compaction stamped (minted only
    across a closed gate, so its mere presence proves the gist pass completed — trusted unconditionally). The raw
    stays resident + fully recoverable in the ledger (logical retirement, reversible — physical erasure is
    Layer-2); recall just doesn't surface it, so its gist is the one copy recall returns. ORTHOGONAL to the
    frecency score — a superseded raw is dropped even if it would score hot (the two exclusions OR together)."""
    if not isinstance(record, dict):
        return False
    if record.get(records.SUPERSEDED_BY_KEY):
        return True
    rid = record.get(records.RECORD_ID_KEY)
    return isinstance(rid, str) and bool(rid) and rid in superseded_ids


def _is_bookkeeping(record) -> bool:
    """True iff `record` is machinery rather than something recall should ever return. Six marker kinds carry no
    recall text and exist only to be read BY the readers: `reinforcement` (derivation fuel for the scorer), the
    two gist roll-up markers (`superseded` + `rolled-up`), `operator-adjudicated-erasure` (it authorises
    erasing its target; it is not itself a memory), and the `withheld`/`restored` pair (the operator's own
    reversible control over what recall may surface — instructions about a record, never a record). `consolidated`
    markers stay live — they are structural and the crash-duplicate retirement reads them in place. Everything
    else is content.

    NO RECORD IS DROPPED HERE FOR BEING OLD. This predicate used to end by scoring the record and dropping
    whatever landed in the archived tier, which retired a never-reinforced record at 26.7 days (`dead-end`) to
    32.9 (`decision`). The age-out is gone for every kind — see the module docstring for why the conversation
    could not earn its way out of it and why exempting only the conversation would have been the worse half.
    nothing scores a record any more — the ranking is lexical relevance alone; it decides nothing about
    what recall can reach."""
    if not isinstance(record, dict):
        return False
    return record.get("kind") in (records.REINFORCEMENT_KIND, records.SUPERSEDED_KIND,
                                  records.ROLLUP_KIND, records.ERASURE_KIND,
                                  records.WITHHOLD_KIND, records.RESTORE_KIND)


def withheld_targets(src: str) -> tuple:
    """`({record_id, ...}, {session_id, ...})` — what the operator currently has withheld from recall.

    LAST MARKER IN LEDGER ORDER WINS, which is why this is a positional pass and not a timestamp comparison.
    Capture stamps whole seconds, so withholding something and restoring it moments later can share one `ts`;
    sorting by time would leave that pair tied and resolve it arbitrarily — the operator's most recent
    instruction is exactly the one that must not be decided by a coin toss. The ledger is append-only, so
    reading it front to back and letting each marker overwrite the last is both the simplest rule and the
    truthful one. `_closed_batches` derives closure from position for the same reason.

    A marker names EITHER a record or a session, never both. The record key is checked first, so a marker
    carrying both well-formed keys is read as naming the record. Be precise about the residual case rather than
    overclaiming: a marker whose record key is malformed AND whose session key is valid does fall through to
    the session leg. No write path can produce one — `_write_control` refuses both-or-neither, and a test pins
    that — so this is the shape of a hand-edited or corrupted line, not of anything the engine mints.
    Failure direction throughout is SURFACE, not hide — a marker missing or mistyping its target is skipped, so
    a corrupt line can never take conversation out of recall on its own."""
    withheld_ids, withheld_sessions = set(), set()
    for record in ledger.iter_records(path=src):
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        if kind not in (records.WITHHOLD_KIND, records.RESTORE_KIND):
            continue
        hiding = kind == records.WITHHOLD_KIND
        rid = record.get(records.TARGET_KEY)
        sid = record.get(records.TARGET_SESSION_KEY)
        if isinstance(rid, str) and rid:
            withheld_ids.add(rid) if hiding else withheld_ids.discard(rid)
        elif isinstance(sid, str) and sid:
            withheld_sessions.add(sid) if hiding else withheld_sessions.discard(sid)
    return withheld_ids, withheld_sessions


def withheld_report(path: "str | None" = None) -> dict:
    """`{"notes": [...], "sessions": [...]}` — what is currently withheld, named so it can be restored.

    WHY THIS EXISTS. Every surface promises the operator that a withhold is reversible, and `restore` needs the
    exact identifier the withhold named. Nothing else could supply one: the readout reports counts, search
    excludes withheld records by construction, and the pin list shows only live pins. So the promise held only
    while the session that performed the withhold still had the identifier in its context — after that, "put it
    back" was unactionable short of hand-reading the store. A control that is reversible in principle and
    one-way in practice is not the control the operator was told they had.

    IDENTIFIERS AND WHEN, NEVER THE WORDING. That is the same line `set_aside` draws and for the same reason:
    reading withheld text back is exactly what the operator asked not to happen. A note carries its kind and
    the date it was withheld, which is enough to say which one you mean without saying what it said."""
    src = ledger.ledger_path() if path is None else path
    withheld_ids, withheld_sessions = withheld_targets(src)
    when: dict = {}
    for record in ledger.iter_records(path=src):
        if not isinstance(record, dict) or record.get("kind") != records.WITHHOLD_KIND:
            continue
        target = record.get(records.TARGET_KEY) or record.get(records.TARGET_SESSION_KEY)
        if isinstance(target, str) and target:
            when[target] = record.get("ts")
    kinds: dict = {}
    for record in ledger.iter_records(path=src):
        rid = record.get(records.RECORD_ID_KEY) if isinstance(record, dict) else None
        if isinstance(rid, str) and rid in withheld_ids:
            kinds[rid] = record.get("kind") or "note"
    return {
        "notes": sorted(({"id": rid, "kind": kinds.get(rid, "note"), "withheld_at": when.get(rid)}
                         for rid in withheld_ids), key=lambda r: r["id"]),
        "sessions": sorted(({"session_id": sid, "withheld_at": when.get(sid)}
                            for sid in withheld_sessions), key=lambda r: r["session_id"]),
    }


def is_withheld(record, withheld_ids: set, withheld_sessions: set) -> bool:
    """True iff the operator has withheld this record — by its own id, or by withholding its whole session.

    THE ONE DEFINITION, deliberately shared. Recall reaches conversation three ways: the ranked search paths
    (through `live_records`), the transcript-window reader, and the session cards the cold-start briefing is
    built from. The last two read the RAW ledger by design, because a window must show a conversation as it was
    captured. So a predicate applied only inside `live_records` would take a withheld session out of search
    while the window reader still quoted it back verbatim and the briefing still greeted the operator with its
    opening line every morning. All three readers ask this same question instead."""
    if not (withheld_ids or withheld_sessions) or not isinstance(record, dict):
        return False
    rid = record.get(records.RECORD_ID_KEY)
    if isinstance(rid, str) and rid in withheld_ids:
        return True
    # BOTH session keys, because a pin does not carry `session_id` — it records where it was asked for under
    # `source_session`. Matching only the first meant a pin made during a conversation survived that
    # conversation being withheld: the operator watched it vanish from search, from the reader and from the
    # briefing, while the one fragment of it still read into every future session was the one place they would
    # never think to look. A pin is up to a thousand characters of whatever they asked to be remembered.
    for key in ("session_id", records.PIN_SOURCE_SESSION_KEY):
        sid = record.get(key)
        if isinstance(sid, str) and sid in withheld_sessions:
            return True
    return False


class ControlNotRecorded(RuntimeError):
    """A withhold or restore could not be written. Carries the plain-language reason.

    This RAISES rather than degrading quietly, because a missed withhold is the operator's instruction not
    happening. Reporting "done" over a write that did not land is the failure eADR-0034 exists to forbid, and
    the operator would have no way to tell."""


def _target_state(src: str, rid, sid) -> tuple:
    """`(exists, already_withheld)` for one named target, in a single pass over the ledger.

    WHY EXISTENCE IS CHECKED AT ALL. Appending a marker always succeeds — it names a target and says nothing
    about whether that target is real — so an id that matches nothing produced a confident "that note is out
    of recall now" over a note still fully searchable. The id comes from a search result by way of a model, so
    a stale, mistyped or invented one is an ordinary occurrence, and this is the one class of report where
    being wrong is silent: the operator has no way to notice that the thing they asked to be private is not.

    The asymmetry with `restore` is deliberate and runs the other way. An unverified restore under-promises —
    the worst case is that something already reachable stays reachable — so it is reported rather than
    refused. An unverified withhold over-promises privacy, which is why it refuses."""
    exists = False
    withheld_ids, withheld_sessions = set(), set()
    for record in ledger.iter_records(path=src):
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        if kind in (records.WITHHOLD_KIND, records.RESTORE_KIND):
            hiding = kind == records.WITHHOLD_KIND
            t_rid, t_sid = record.get(records.TARGET_KEY), record.get(records.TARGET_SESSION_KEY)
            if isinstance(t_rid, str) and t_rid:
                withheld_ids.add(t_rid) if hiding else withheld_ids.discard(t_rid)
            elif isinstance(t_sid, str) and t_sid:
                withheld_sessions.add(t_sid) if hiding else withheld_sessions.discard(t_sid)
            continue
        if rid is not None and record.get(records.RECORD_ID_KEY) == rid:
            exists = True
        elif sid is not None and record.get("session_id") == sid:
            exists = True
    return exists, (rid in withheld_ids if rid is not None else sid in withheld_sessions)


def _write_control(kind: str, *, record_id=None, session_id=None,
                   path: "str | None" = None, now: "int | None" = None) -> dict:
    """Append one withhold/restore marker and return it. Raises ControlNotRecorded rather than failing quietly.

    Exactly ONE target, checked here rather than at each caller: a marker naming both would be ambiguous to
    every reader, and one naming neither would sit in the ledger doing nothing.

    THE INDEX-EPOCH BUMP IS THE LOAD-BEARING HALF. `ledger.append` alone would leave the fast index stamped
    current while its rows no longer match what recall may surface — so `search` would keep returning, as an
    authoritative answer, the very record the operator just withheld, while the plain scan correctly dropped it.
    That is the fast/slow divergence in its worse direction, and it would resurface withheld content rather
    than merely miss new content. `index.extend` cannot help: it accepts captured turns only, and no incremental
    update can express a REMOVAL anyway. Bumping the index epoch is what makes the index honestly stale, so the
    next ranked read heals it (`index._heal_if_stale`) and every read before that heals falls to the scan, which
    reads through `live_records` and is already correct. Held under the single-writer lock, and bumped BEFORE
    the append for the same reason compaction does it: every crash window then leaves the index stamped stale,
    which is always the safe way to be wrong.

    THE EPOCH, NOT THE CONTENT GENERATION, and that distinction is not cosmetic. `generation` means content was
    rewritten or removed, and `restore_vault` reads it that way: a local generation ahead of a backup's makes it
    refuse the restore and raise a trust-critical finding saying deliberate removals would be undone. Since
    backups are throttled to about a day, using `generation` here would have told any operator who withheld or
    pinned something in the last day that their backup could not be restored, for a removal that never
    happened — on the day they needed it. Withholding removes nothing, so it moves the counter that means only
    "the index may no longer hold the right set" (`ledger.index_epoch`)."""
    from memory import capture  # lazy: keep capture off the module-load path (cycle discipline)
    rid = record_id if isinstance(record_id, str) and record_id else None
    sid = session_id if isinstance(session_id, str) and session_id else None
    if (rid is None) == (sid is None):
        raise ControlNotRecorded("name exactly one thing to act on — a single note, or a whole session.")
    target = path if path is not None else ledger.ledger_path()
    exists, already = _target_state(target, rid, sid)
    if not exists:
        noun = "note" if rid is not None else "conversation"
        raise ControlNotRecorded(
            f"there is no {noun} in memory with that identifier, so nothing was changed. Check it against a "
            "search result — the identifier has to be one memory actually holds."
        )
    if kind == records.WITHHOLD_KIND and already:
        noun = "note" if rid is not None else "conversation"
        raise ControlNotRecorded(f"that {noun} is already out of recall — nothing needed changing.")
    data_dir = os.path.dirname(target) or "."
    os.makedirs(data_dir, exist_ok=True)
    lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
    if lock_fd is None:
        # A `None` here does NOT prove contention — the same value comes back when the store cannot be opened
        # at all, which is what a permissions problem, a full disk or an unmounted volume looks like. Saying
        # "try again in a moment" over one of those sends the operator into a retry that can never succeed, so
        # the message names both possibilities and points at the one they can act on.
        writable = os.access(data_dir, os.W_OK)
        raise ControlNotRecorded(
            "another memory write is in progress, so nothing was changed. Try again in a moment."
            if writable else
            f"memory could not be written to ({data_dir} is not writable), so nothing was changed. This will "
            "not clear on its own — check the folder's permissions and that its disk is mounted and has room."
        )
    try:
        marker = {
            "v": capture.RECORD_VERSION,
            "kind": kind,
            records.RECORD_ID_KEY: records.new_record_id(),
            "ts": int(time.time()) if now is None else now,
            "tags": [records.WITHHOLD_TAG],
        }
        if rid is not None:
            marker[records.TARGET_KEY] = rid
        else:
            marker[records.TARGET_SESSION_KEY] = sid
        ledger.bump_index_epoch(for_path=target)
        ledger.append(marker, path=path)
        return marker
    except ControlNotRecorded:
        raise
    except Exception as exc:
        raise ControlNotRecorded(f"the change could not be saved ({exc}).") from exc
    finally:
        capture._release_lock(lock_fd)


def withhold(*, record_id=None, session_id=None, path: "str | None" = None,
             now: "int | None" = None) -> dict:
    """Take one note, or one whole session's conversation, out of everything recall surfaces. Reversible.

    NOTHING IS DELETED and nothing becomes unrecoverable: the records stay in the ledger byte for byte, and
    `restore` brings them back. This is Layer-1 — the operator's own reversible control. Physical erasure is a
    different act entirely, reachable only by merging a single-purpose erasure pull request, and the two are
    kept apart in vocabulary as well as in mechanism (`records.WITHHOLD_KIND`)."""
    return _write_control(records.WITHHOLD_KIND, record_id=record_id, session_id=session_id,
                          path=path, now=now)


def restore(*, record_id=None, session_id=None, path: "str | None" = None,
            now: "int | None" = None) -> dict:
    """Undo a withhold, by the same target the withhold named. Appends; it never edits the earlier marker.

    Restoring something that was never withheld is harmless rather than an error — the marker simply names a
    target no withhold covers, and `withheld_targets` discards what is not there. That keeps "put it back"
    safe to say twice, which is how an operator actually talks to it. The target must still be something
    memory holds: an identifier matching nothing is a mistake worth telling them about rather than a silent
    no-op dressed as success (`_target_state`)."""
    return _write_control(records.RESTORE_KIND, record_id=record_id, session_id=session_id,
                          path=path, now=now)


def _injected_message_keys(src: str) -> set:
    """The `(session_id, seq)` of every captured MESSAGE that is a harness-injected pseudo-turn.

    Why a message-level pass is needed. Capture splits a message over the chunk size into several records that
    share one `seq`, and `records.is_injected_record` recognises a pseudo-turn two ways: the durable tag, which
    capture stamps on EVERY chunk, and — back-compat for records captured before tagging existed — a
    START-ANCHORED text match, which by construction can only ever match the FIRST chunk. So for any untagged
    legacy `/compact` continuation summary, chunks two onward match neither arm.

    That was inert while the whole kind was excluded from recall. It is not inert now: those tail chunks would
    be surfaced as ordinary conversation, and a continuation summary contains a section headed "All user
    messages" — so recall could hand back a machine's paraphrase of what was asked for, attributed to the
    operator. Measured on the maintainer's own store when this was found: 442 such chunks across 16 sessions.

    One cheap sequential pass, keyed on the pair every chunk of one message shares, so the tail travels with the
    head. A record missing either field cannot be grouped and is judged on its own."""
    keys = set()
    for record in ledger.iter_records(path=src):
        if not isinstance(record, dict) or record.get("kind") != records.AMBIENT_CAPTURE_KIND:
            continue
        if not records.is_injected_record(record):
            continue
        sid, seq = record.get("session_id"), record.get("seq")
        if isinstance(sid, str) and sid and isinstance(seq, int) and not isinstance(seq, bool):
            keys.add((sid, seq))
    return keys


def _is_excluded_capture(record, injected_keys: "set | None" = None) -> bool:
    """True iff `record` is captured conversation recall must NOT surface — a harness-injected pseudo-turn (a
    `/compact` continuation summary, a `task-notification` block). The single recall-membership discriminator
    for the capture layer; recall drops it on every path via `live_records`.

    GENUINE turns are recall content (eADR-0038: the exact conversation is the canonical record, and the curated
    summaries over it are the disposable layer). This predicate is deliberately NOT a kind test: the earlier
    verdict excluded the whole `turn-delta` kind because verbatim raw crowded paraphrased summaries out of every
    recall, and the answer then was to hide the canonical record. The transcript-first contract reverses that —
    what stays out is only text the operator never said. `records.is_injected_record` is the shared definition
    (the consolidation sweep skips the same records as fuel, and the transcript-window reader leaves them out of
    a window), so machine scaffolding can never be presented as the operator's own words on any path.

    Asks a MEMBERSHIP question, not a kind question. A caller that needs "is this a captured turn?" tests
    `record.get("kind") == records.AMBIENT_CAPTURE_KIND` itself — borrowing this predicate for that would break
    silently the next time membership changes.

    `injected_keys` (from `_injected_message_keys`) carries the MESSAGE-level verdict, so a later chunk of an
    untagged legacy pseudo-turn is excluded along with the first — see that function for why the per-record
    test alone is not enough. Omitting it judges each record alone, which is correct only where every chunk is
    known to be tagged (the freshly-captured turn `index.extend` receives)."""
    if not (isinstance(record, dict) and record.get("kind") == records.AMBIENT_CAPTURE_KIND):
        return False
    if records.is_injected_record(record):
        return True
    if injected_keys:
        sid, seq = record.get("session_id"), record.get("seq")
        if isinstance(sid, str) and isinstance(seq, int) and not isinstance(seq, bool):
            return (sid, seq) in injected_keys
    return False


def _derive_membership(src: str) -> tuple:
    """Every exclusion `live_records` needs, from ONE traversal of the ledger.

    WHY THIS EXISTS. The five derivations below are each a full sequential read: the consolidation closed set,
    the roll-up closed set, the supersession map, the injected-message keys, and the operator's withholds. Run
    separately they re-read and re-parse every line five times, and `live_records` sits on the recall hot path
    AND inside every index rebuild. Measured on a 30 MB / 28,000-record store, folding them took `live_records`
    from 0.434 s to 0.098 s — the same answer, a third of a second cheaper, every time it is called.

    The one ordering constraint is that supersession is only in force once its roll-up batch is CLOSED, and the
    closing markers can appear anywhere in the file. That is why the markers are COLLECTED here and resolved
    after the pass rather than during it: a single read cannot know, at the moment it meets a supersession,
    whether the batch that closes it comes later. Resolving afterwards is what keeps the crash-safety exact —
    a marker whose pass never finished stays inert, so no raw is ever hidden without its gist.

    Each individual helper is kept and still used by the other readers that need one derivation alone; this is
    the composite for the one caller that needs all five."""
    closed: set = set()
    closed_rollup: set = set()
    supersessions: list = []
    injected_keys: set = set()
    withheld_ids: set = set()
    withheld_sessions: set = set()
    for record in ledger.iter_records(path=src):
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        if kind == records.MARKER_KIND:
            batch = record.get(records.BATCH_KEY)
            if isinstance(batch, str) and batch:
                closed.add(batch)
        elif kind == records.ROLLUP_KIND:
            batch = record.get(records.BATCH_KEY)
            if isinstance(batch, str) and batch:
                closed_rollup.add(batch)
        elif kind == records.SUPERSEDED_KIND:
            supersessions.append((record.get(records.BATCH_KEY), record.get(records.TARGET_KEY),
                                  record.get(records.SUPERSEDED_BY_KEY)))
        elif kind in (records.WITHHOLD_KIND, records.RESTORE_KIND):
            hiding = kind == records.WITHHOLD_KIND
            rid, sid = record.get(records.TARGET_KEY), record.get(records.TARGET_SESSION_KEY)
            if isinstance(rid, str) and rid:
                withheld_ids.add(rid) if hiding else withheld_ids.discard(rid)
            elif isinstance(sid, str) and sid:
                withheld_sessions.add(sid) if hiding else withheld_sessions.discard(sid)
        elif kind == records.AMBIENT_CAPTURE_KIND and records.is_injected_record(record):
            sid, seq = record.get("session_id"), record.get("seq")
            if isinstance(sid, str) and sid and isinstance(seq, int) and not isinstance(seq, bool):
                injected_keys.add((sid, seq))
    superseded = {raw for batch, raw, gist in supersessions
                  if isinstance(batch, str) and batch in closed_rollup
                  and isinstance(raw, str) and raw and isinstance(gist, str) and gist}
    return closed, closed_rollup, superseded, injected_keys, withheld_ids, withheld_sessions


def live_records(path: "str | None" = None):
    """Yield the ledger records recall should surface.

    Recall surfaces the CONVERSATION and the curated layer over it. A genuine `turn-delta` — the `Stop`-appended
    verbatim of what was actually said — is recall content, because the transcript is the canonical record and
    the summaries above it are the disposable layer (eADR-0038). Only a harness-injected pseudo-turn is dropped,
    by `_is_excluded_capture`, on the ONE shared read path the fast (FTS5) and slow (scan) lookups both consume,
    so membership holds identically on every path. It is re-derived on every read / index rebuild: no per-record
    marker, no carried bit, so membership survives compaction for free and edits no ledger line in place.

    The exclusion is targeted, not a curated-kind allowlist: a record carrying a `role` + `text` but no explicit
    kind is an episodic-shaped recall record and stays surfaced. A future kind that is fuel rather than content
    must be added to `_is_excluded_capture` to stay out of recall.

    The remaining exclusions trim a record that is machinery, superseded, or withheld: (a) an episodic a crashed
    consolidation pass orphaned (logical retirement); (b) a bookkeeping marker, which is never a recall result
    (`_is_bookkeeping`); (c) a raw episode a COMPLETED gist roll-up superseded, a crashed roll-up's orphaned
    gist, and the roll-up markers; (d) anything the operator has WITHHELD, by its own id or by withholding its
    session (`is_withheld`) — the one exclusion here that a person chose rather than the machinery deriving. A
    dropped record stays in the ledger, fully recoverable; this generator just doesn't surface it. NOTHING IS
    DROPPED FOR BEING OLD — the archived-tier age-out that used to sit here is gone for every kind (module
    docstring), and with it the `now` this took: membership no longer depends on the clock at all, so the same
    ledger yields the same records whenever it is read.

    Withholding a SESSION reaches every record carrying that session id, so the curated summaries written over
    a withheld conversation go with it. A cross-session gist is the deliberate exception: its `session_id` is a
    `tag:` cluster sentinel rather than any one session (`records.is_cross_session_sentinel`), so withholding
    one contributing session does not silently retract a summary drawn from several.

    ONE derivation pass over the RAW ledger (never the filtered stream) collects every exclusion — the
    consolidation and roll-up closed sets, the supersession map, the injected-message keys and the withhold
    set (`_derive_membership`, which carries why they are folded rather than read five times) — then a second
    pass streams, dropping a record if ANY exclusion fires (they OR together — any one reason hides it).
    Mutates nothing — never writes, never deletes."""
    src = ledger.ledger_path() if path is None else path
    closed, closed_rollup, superseded, injected_keys, withheld_ids, withheld_sessions = _derive_membership(src)
    for record in ledger.iter_records(path=src):
        if (not _is_excluded_capture(record, injected_keys)
                and not _is_retired(record, closed)
                and not _is_gist_orphan(record, closed_rollup)
                and not _is_superseded(record, superseded)
                and not is_withheld(record, withheld_ids, withheld_sessions)
                and not _is_bookkeeping(record)):
            yield record


def duplicates(path: "str | None" = None) -> dict:
    """The logically-retired passes — what `live_records` drops from recall but the ledger still holds, grouped
    by session id. A READ-ONLY report (mutates nothing); the records are returned as-is so a caller can render
    a snippet. The retired copies remain fully recoverable in the ledger."""
    src = ledger.ledger_path() if path is None else path
    closed = _closed_batches(src)
    out: dict = {}
    for record in ledger.iter_records(path=src):
        if _is_retired(record, closed):
            sid = record.get("session_id") or "(unknown session)"
            out.setdefault(sid, []).append(record)
    return out


# --- the set-aside report: what recall no longer surfaces but the operator has a handle on --------------------

# The one class recall drops that the operator can act on, kept as data so the readout's wording can never
# promise more than the mechanism delivers:
SET_ASIDE_SUMMARISED = "summarised"  # a completed roll-up folded it into a summary; there is NO way to un-fold it,
#                                      only to read its original wording back (recall's stand-in is the summary)
_SET_ASIDE_LIMIT = 20                 # matches index's recent-decisions cap: a bounded newest-first sample, never the
#                                      whole population (the report also carries the full count + id set)


def set_aside(path: "str | None" = None, *, limit: int = _SET_ASIDE_LIMIT) -> dict:
    """What `live_records` drops from recall for a reason the operator has a handle on — a READ-ONLY report the
    boot readout relays. Mutates NOTHING: every record named here is still resident and fully recoverable in the
    one ledger; recall just doesn't surface it. Returns
        {"rows": [...bounded newest-first...],
         "totals": {"summarised": int, "withheld_notes": int, "withheld_sessions": int},
         "identity": [every set-aside id, sorted — the FULL population, independent of `limit`]}
    so a render can tell "there is none of this" apart from "there is, and it did not all fit", and a caller
    watching for change compares the full id set, not the bounded sample.

    TWO classes, and NOT the union of every `live_records` exclusion.

    SUMMARISED — a raw episode a COMPLETED roll-up folded into a gist. There is no un-fold, so the honest handle
    is `recorded_text`, which reads its original wording; the row carries `reversible=False` so the readout
    never offers to bring one back. This is the class the `rows` carry.

    WITHHELD — what the operator themselves took out of recall, which `restore` puts straight back. It is
    reported as a COUNT ONLY, deliberately: a row would carry the record's own text, and printing withheld
    wording back into the briefing at every session start is precisely what the operator asked not to happen.
    A count tells them the state exists and is reversible without re-surfacing the thing itself. Sessions and
    single notes are counted apart because they are what the operator named, and "two conversations" reads very
    differently from "two notes".

    Nothing about this report is time-dependent: the archived-tier ratchet that once aged records out of recall
    is gone (module docstring), so no row here appears or disappears with the clock.

    A crash-orphaned record (a consolidation or roll-up that did not finish) is DELIBERATELY excluded: it is a
    duplicate the good copy already replaces, not something the operator lost, so an "undo" would only re-admit a
    duplicate into search. `duplicates()` reports that class for the maintainer digest instead.

    Row: {id, reason, text, role, ts, since, reversible, stands_in}. `since` is when the summary folded the raw
    in (the supersession marker's ts), or None once a compaction has pruned that marker — no fold event survives
    the fold, and this reader never invents one. Excludes ambient turn-deltas and every bookkeeping marker
    (nothing without role+text), and any record with no stable id or no usable text. Ordering mirrors
    index.recent_decisions: a TOTAL sort key, so a record with a damaged ts sorts last instead of raising.
    Degrades to an empty report on ANY read fault — an unreadable store costs the readout, never the pack, and
    boot surfaces an unreadable store through its own memory-offline notice, never from here."""
    src = ledger.ledger_path() if path is None else path
    try:
        closed = _closed_batches(src)
        closed_rollup = _closed_rollup_batches(src)
        # Classify from the SAME view `live_records` excludes by, so the readout can never diverge from what recall
        # actually hides. Supersession is recognised BOTH ways `_is_superseded` recognises it: a live `superseded`
        # marker (pre-compaction) OR the folded `superseded_by` field compaction carries onto the raw and then
        # prunes the marker. `superseded_at` records the marker's gist id + fold moment where a marker still
        # exists (for `since`); post-compaction the raw's own carried field supplies the stand-in and `since` is
        # simply unknown (no event survives the fold).
        superseded_ids = set(_superseded_by_map(src, closed_rollup))
        superseded_at: dict = {}
        for record in ledger.iter_records(path=src):
            if not isinstance(record, dict) or record.get("kind") != records.SUPERSEDED_KIND:
                continue
            batch = record.get(records.BATCH_KEY)
            if not isinstance(batch, str) or batch not in closed_rollup:
                continue
            raw_id = record.get(records.TARGET_KEY)
            gist_id = record.get(records.SUPERSEDED_BY_KEY)
            ts = record.get("ts")
            if isinstance(raw_id, str) and raw_id and isinstance(gist_id, str) and gist_id:
                superseded_at[raw_id] = (gist_id, ts if isinstance(ts, int) and not isinstance(ts, bool) else None)

        rows: list = []
        summarised = 0
        # The withheld set is needed BEFORE the rows are built, not only for the counts: a summary the
        # operator withheld — by its own id, or by withholding the conversation it was written over — must
        # not appear as a row, because a row carries the record's own `text` and the readout prints that into
        # the briefing at every session start. Reporting a withhold and quoting the withheld wording in the
        # same block is the exact outcome the count-only design exists to prevent.
        withheld_ids, withheld_sessions = withheld_targets(src)
        for record in ledger.iter_records(path=src):
            if not isinstance(record, dict):
                continue
            if record.get("kind") not in (records.EPISODIC_KIND, records.GIST_KIND):
                continue                                   # only recall content — never a marker or a turn-delta
            if is_withheld(record, withheld_ids, withheld_sessions):
                continue
            rid = record.get(records.RECORD_ID_KEY)
            text = record.get("text")
            if not (isinstance(rid, str) and rid) or not (isinstance(text, str) and text.strip()):
                continue
            if _is_retired(record, closed) or _is_gist_orphan(record, closed_rollup):
                continue                                   # a crash-orphan duplicate is not a loss — never shown
            if not _is_superseded(record, superseded_ids):  # marker OR the carried field: survives compaction
                continue                                   # still surfaced by recall — not set aside
            folded = superseded_at.get(rid)
            gist_id = folded[0] if folded else record.get(records.SUPERSEDED_BY_KEY)
            since = folded[1] if folded else None           # no fold event survives compaction -> unknown
            summarised += 1
            rows.append({"id": rid, "reason": SET_ASIDE_SUMMARISED, "text": text, "role": record.get("role"),
                         "ts": record.get("ts"), "since": since, "reversible": False, "stands_in": gist_id})

        def _order(row):
            # A TOTAL key (index.recent_decisions' guard): a non-numeric moment sorts into the unusable bucket
            # carrying a fixed 0, so mixed rows only ever compare like with like instead of raising mid-sort.
            m = row["since"] if row["since"] is not None else row["ts"]
            usable = isinstance(m, (int, float)) and not isinstance(m, bool) and math.isfinite(m)
            return usable, (m if usable else 0), row["id"]

        rows.sort(key=_order, reverse=True)
        identity = sorted(r["id"] for r in rows)
        return {"rows": rows[:limit],
                "totals": {"summarised": summarised, "withheld_notes": len(withheld_ids),
                           "withheld_sessions": len(withheld_sessions)},
                "identity": identity}
    except Exception:  # noqa: BLE001 — an unreadable/degraded store costs the readout, never the session
        return {"rows": [], "totals": {"summarised": 0, "withheld_notes": 0, "withheld_sessions": 0},
                "identity": []}


def recorded_text(record_id: str, *, path: "str | None" = None) -> "dict | None":
    """The full recorded wording of ONE record by its stable id, read straight from the ledger — the "show me
    the exact wording" handle for a record recall no longer surfaces (a summarised raw). Reads
    the RAW ledger on purpose, bypassing the recall filter: keeping every set-aside record recoverable word-for-
    word is the guarantee that makes this forgetting reversible in the first place. SIDE-EFFECT-FREE — records
    no access, so merely looking at a set-aside note never silently re-ranks what recall surfaces. Returns the
    record dict, or None on an unknown id or any read fault. Never raises."""
    if not isinstance(record_id, str) or not record_id:
        return None
    try:
        for record in ledger.iter_records(path=ledger.ledger_path() if path is None else path):
            if isinstance(record, dict) and record.get(records.RECORD_ID_KEY) == record_id:
                return record
    except Exception:  # noqa: BLE001 — an unreadable store yields no text, never a raised error
        return None
    return None


def _snippet(text, width: int = 70) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_duplicates(path: "str | None" = None) -> int:
    """The `duplicates` CLI verb: an operator-legible list of what is logically retired from recall — by
    session and a plain-language snippet, never a record-id or ledger offset. Each is still in the ledger."""
    groups = duplicates(path)
    if not groups:
        print("No hidden duplicates — recall surfaces every consolidated session once.")
        return 0
    total = sum(len(v) for v in groups.values())
    print(f"{total} duplicate summary record(s) across {len(groups)} session(s) are hidden from recall")
    print("(left behind by a consolidation pass that didn't finish saving; each is STILL SAVED and fully")
    print("recoverable — nothing was erased):\n")
    for sid, recs in groups.items():
        print(f"  session {sid}:")
        for rec in recs:
            print(f"    - hidden from recall: {_snippet(rec.get('text'))}")
    return 0


# --- Operator demonstration -------------------------------------------------------------------------------
# A walkthrough on a THROWAWAY practice cabinet (a temp folder), never real data. It runs the REAL consolidate
# + rebuild + recall code and reads the cabinet back, so every claim is recognizable words on screen. Run it
# and vary the two summaries near the top:
#     uv run --directory .engine --frozen -- python tools/memory/forget.py demo

# Two summaries of ONE session: the first is the pass that CRASHED before its marker (so it is an orphan); the
# second is the retry that completed. Both mention "sourdough", so a search for it would find both copies were
# they both surfaced — which is exactly what logical retirement prevents. Vary the wording and re-run.
_DEMO_SESSION = "session-sourdough"
_DEMO_CRASHED_TEXT = "Decided the sourdough starter gets fed every morning at eight — DO-NOT-LOSE-THIS."
_DEMO_RETRY_TEXT = "Decided the sourdough starter is fed daily at 8am."
_DEMO_WORD = "sourdough"


def _demo() -> int:
    import tempfile

    print("=" * 80)
    print("MEMORY — tidying a crash-duplicated summary out of recall, without losing it (a practice run)")
    print("=" * 80)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet
        try:
            ok = _demo_body()
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 80)
    print("Reminder: that was a PRACTICE cabinet, thrown away when this demo ended. The duplicate existed only")
    print("because a save CRASHED mid-write (rare) — this is crash-recovery of an accidental double, never the")
    print("deletion of anything you meant to keep. The extra copy is LOGICALLY RETIRED: dropped from search,")
    print("but STILL IN THE CABINET and fully recoverable — nothing is erased here. (Permanently erasing a")
    print("record is a separate, audit-reviewed step you approve by merging a pull request — never this.) Like")
    print("all memory, this is private, local, and deletable. Vary it: edit the two summaries near the top of")
    print("this file and re-run.")
    return 0 if ok else 1


def _ledger_episodics(session_id: str) -> list:
    """The episodic records for one session AS THEY SIT IN THE LEDGER (unfiltered) — the recoverability proof:
    everything is still here even after a copy is retired from recall."""
    return [
        r for r in ledger.iter_records(path=ledger.ledger_path())
        if isinstance(r, dict) and r.get("kind") == records.EPISODIC_KIND and r.get("session_id") == session_id
    ]


def _recall_episodics(word: str, session_id: str) -> list:
    """What RECALL surfaces for `word` (the fast index, rebuilt through `live_records`) — filtered to this
    session's summaries."""
    from memory import index  # lazy: index imports THIS module, so import it here, not at module load
    return [
        r for r in index.query(word).records
        if isinstance(r, dict) and r.get("kind") == records.EPISODIC_KIND and r.get("session_id") == session_id
    ]


def _demo_body() -> bool:
    from memory import legacy_shapes as legacy  # lazy: the legacy episodic-shape factories (consolidate's heir)

    print("\nPART 1 — a crash leaves the SAME session's summary in the cabinet twice")
    print("-" * 80)
    # The pass that CRASHED: its episodic was appended, but the crash hit before its `consolidated` marker —
    # so this batch is never closed. (A fixed id stands in for the real per-pass uuid.)
    crashed = legacy.episodic(_DEMO_SESSION, "decision", _DEMO_CRASHED_TEXT, "the-pass-that-crashed")
    ledger.append(crashed)
    # The RETRY that completed: store_episodic writes its episodic + a marker (a NEW batch) and rebuilds recall.
    legacy.store_episodic(_DEMO_SESSION, [{"role": "decision", "text": _DEMO_RETRY_TEXT}])
    in_ledger = _ledger_episodics(_DEMO_SESSION)
    print(f"  The cabinet now holds {len(in_ledger)} summaries for '{_DEMO_SESSION}':")
    for r in in_ledger:
        print(f"    - {_snippet(r.get('text'))}")
    print("  (One is from the pass that crashed before it finished; one is the completed retry.)")

    print(f"\nPART 2 — recall surfaces it ONCE (search for \"{_DEMO_WORD}\")")
    print("-" * 80)
    recalled = _recall_episodics(_DEMO_WORD, _DEMO_SESSION)
    print(f"  in the cabinet: {len(in_ledger)}    surfaced by recall: {len(recalled)}")
    for r in recalled:
        print(f"    recall returns: {_snippet(r.get('text'))}")
    deduped = len(recalled) == 1 and len(in_ledger) == 2
    print(f"  => {'recall shows 1 (the completed pass), though the cabinet holds 2.' if deduped else '!!! recall did not dedupe'}")

    print("\nPART 3 — nothing was erased: the retired copy is STILL in the cabinet, and recoverable")
    print("-" * 80)
    groups = duplicates()
    retired = [r for recs in groups.values() for r in recs]
    still_there = _ledger_episodics(_DEMO_SESSION)
    print(f"  logically retired from recall: {len(retired)}")
    for r in retired:
        print(f"    - retired: {_snippet(r.get('text'))}")
    print(f"  summaries still physically in the cabinet: {len(still_there)} (unchanged — nothing was deleted)")
    recoverable = len(retired) == 1 and len(still_there) == 2
    print(f"  => {'the duplicate is hidden from recall but still in the cabinet (recoverable).' if recoverable else '!!! something was lost'}")

    print("\nPART 4 — reversible by construction: rebuild from the cabinet alone, recall stays correct")
    print("-" * 80)
    from memory import index  # lazy
    index.rebuild()                          # rebuilt from the one real copy — the retirement is re-derived
    again = _recall_episodics(_DEMO_WORD, _DEMO_SESSION)
    stable = len(again) == 1 and len(_ledger_episodics(_DEMO_SESSION)) == 2
    print(f"  after a fresh rebuild — surfaced by recall: {len(again)}; in the cabinet: {len(_ledger_episodics(_DEMO_SESSION))}")
    print("  The retirement is a RULE derived from the cabinet, not a deletion: rebuilding re-applies it, and")
    print("  the retired copy is one rule-change away from resurfacing — it never left.")
    print(f"  => {'stable across a rebuild; nothing destroyed.' if stable else '!!! rebuild changed the answer'}")

    return deduped and recoverable and stable


# --- Operator demonstration: the stable, content-free record id --------------------------------
# A second THROWAWAY-cabinet walkthrough, for the per-record name-tag the id adds. It runs the REAL factories +
# store + rebuild + recall and reads the cabinet back, so every claim is recognizable words on screen. Vary the
# notes near the top and re-run:
#     uv run --directory .engine --frozen -- python tools/memory/forget.py identity
_ID_DEMO_SESSION = "session-blueprint"
_ID_DEMO_TURN_TEXT = "Let's lock the launch to the blue plan."
_ID_DEMO_EPISODIC_TEXT = "Decided: the launch ships on the blue plan."
_ID_DEMO_TWIN_TEXT = "Decided: the launch ships on the blue plan."   # identical wording, stored twice
_ID_DEMO_WORD = "launch"


def _short(tag) -> str:
    """A readable short form of a 32-char name-tag, e.g. '3f9a…c7d1' — enough to compare two by eye."""
    tag = str(tag or "")
    return f"{tag[:4]}…{tag[-4:]}" if len(tag) >= 8 else (tag or "(none)")


def _all_records() -> list:
    return [r for r in ledger.iter_records(path=ledger.ledger_path()) if isinstance(r, dict)]


def _demo_identity() -> int:
    import tempfile

    print("=" * 80)
    print("MEMORY — every note gets a permanent, private name-tag that survives tidying (a practice run)")
    print("=" * 80)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet
        try:
            ok = _demo_identity_body()
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 80)
    print("Reminder: that was a PRACTICE cabinet, thrown away when this demo ended — private, on your machine,")
    print("and deletable. NOTHING was removed here; this step only ADDS the name-tag. The tag is permanent and")
    print("private: its only job is to let a LATER piece of work ask for exactly one note to be removed without")
    print("that request ever showing the note's words. Vary it: edit the notes near the top of this file and")
    print("re-run — the tags change, the stability still holds.")
    return 0 if ok else 1


def _demo_identity_body() -> bool:
    from memory import capture, index, legacy_shapes as legacy  # lazy: legacy → index → forget would cycle at load

    print("\nPART 1 — every note gets a name-tag")
    print("-" * 80)
    # A real captured turn-delta and a real stored episodic, both through the live factories the id rides in.
    ledger.append(capture._make_record(_ID_DEMO_SESSION, 0, "user", _ID_DEMO_TURN_TEXT))
    legacy.store_episodic(_ID_DEMO_SESSION, [{"role": "decision", "text": _ID_DEMO_EPISODIC_TEXT}])
    stored = [r for r in _all_records() if r.get("session_id") == _ID_DEMO_SESSION]
    for r in stored:
        if r.get("text"):
            label = _snippet(r.get("text"))
        else:
            label = {records.MARKER_KIND: "(a tidy-up marker)",
                     records.EPISODIC_KIND: "(a summary note)"}.get(r.get("kind"), "(a conversation note)")
        print(f"  note: {label:<52}  tag: {_short(r.get(records.RECORD_ID_KEY))}")
    tagged = [r for r in stored
              if isinstance(r.get(records.RECORD_ID_KEY), str) and len(r[records.RECORD_ID_KEY]) == 32]
    every_tagged = len(stored) >= 2 and len(tagged) == len(stored)
    print(f"  => {'every note carries its own private name-tag.' if every_tagged else '!!! a note is missing its tag'}")

    print("\nPART 2 — the name-tag reveals nothing about the note")
    print("-" * 80)
    twin_a = capture._make_record(_ID_DEMO_SESSION, 1, "user", _ID_DEMO_TWIN_TEXT)
    twin_b = capture._make_record(_ID_DEMO_SESSION, 2, "user", _ID_DEMO_TWIN_TEXT)
    ledger.append(twin_a)
    ledger.append(twin_b)
    print(f'  two notes with the SAME wording: "{_snippet(_ID_DEMO_TWIN_TEXT)}"')
    print(f"    note 1 tag: {_short(twin_a[records.RECORD_ID_KEY])}")
    print(f"    note 2 tag: {_short(twin_b[records.RECORD_ID_KEY])}")
    different = twin_a[records.RECORD_ID_KEY] != twin_b[records.RECORD_ID_KEY]
    print(f"  => {'identical words, DIFFERENT tags (the tag is random, not made from the words).' if different else '!!! identical text produced the same tag'}")
    index.rebuild()
    found_by_word = index.query(_ID_DEMO_WORD).records
    found_by_tag = index.query(twin_a[records.RECORD_ID_KEY]).records
    tag_result = "[no matches]" if not found_by_tag else f"{len(found_by_tag)} match(es)"
    if found_by_word:
        print(f'  search for the word "{_ID_DEMO_WORD}": {len(found_by_word)} match(es) — the notes ARE findable by their words')
    else:
        print(f'  search for the word "{_ID_DEMO_WORD}": 0 matches — that word is not in the notes above')
        print('    (edit _ID_DEMO_WORD near the top of this file to a word you can see, then re-run)')
    print(f'  search for a tag "{_short(twin_a[records.RECORD_ID_KEY])}": {tag_result} — you cannot find a note by its tag')
    # The ONLY failure here is the tag actually surfacing in search. A search-word that matches no note is the
    # operator's own input, not a leak — guide them to a present word, never cry "leaked".
    tag_private = not found_by_tag
    if not tag_private:
        print("  => !!! the tag leaked into search")
    elif found_by_word:
        print("  => the tag is private: words find the note, the tag never does.")
    else:
        print("  => the tag is private (no match by tag); pick a word from the notes to see the other half.")

    print("\nPART 3 — the name-tag stays the same when the engine tidies")
    print("-" * 80)
    # Track ONE note's tag through a rebuild, a re-file (the move the future tidy-up makes), and a 2nd rebuild.
    tag0 = twin_a[records.RECORD_ID_KEY]
    index.rebuild()                                                    # (a) the index READS the tag, never re-mints
    fetched = [r for r in index.query(_ID_DEMO_WORD).records if r.get(records.RECORD_ID_KEY) == tag0]
    tag1 = fetched[0][records.RECORD_ID_KEY] if fetched else None
    ledger.append(twin_a)                                             # (b) re-file the SAME note (compaction's move)
    refiled = [r for r in _all_records() if r.get(records.RECORD_ID_KEY) == tag0]
    tag2 = refiled[-1][records.RECORD_ID_KEY] if refiled else None
    index.rebuild()                                                   # (c) rebuild once more
    again = [r for r in index.query(_ID_DEMO_WORD).records if r.get(records.RECORD_ID_KEY) == tag0]
    tag3 = again[0][records.RECORD_ID_KEY] if again else None
    print(f"  the note's tag at creation:        {_short(tag0)}")
    print(f"    after rebuilding recall:           {_short(tag1)}")
    print(f"    after re-filing the note:          {_short(tag2)}")
    print(f"    after rebuilding recall once more: {_short(tag3)}")
    stable = bool(tag0) and tag1 == tag0 and tag2 == tag0 and tag3 == tag0
    print(f"  => STABLE: {'yes' if stable else 'NO — !!! tag changed'}")

    return every_tagged and different and tag_private and stable


def _print_set_aside(path: "str | None" = None) -> int:
    """The `set-aside` CLI verb: an operator-legible list of what recall has set aside and how to act on each —
    the words the AI matches against when the operator says "show me the one about X". Never a record id."""
    report = set_aside(path)
    rows, total = report["rows"], report["totals"]["summarised"]
    if not rows:
        print("Nothing set aside — recall is surfacing every saved note.")
        return 0
    shown = len(rows)
    noun = "note" if total == 1 else "notes"
    print(f"{total} {noun} set aside from recall (nothing deleted — all still saved)"
          + (f"; the {shown} most recent:" if shown < total else ":"))
    for row in rows:
        print(f"  - folded into a shorter summary: {_snippet(row['text'])}")
        print(f"      -> the summary stands in now; ask to see this one's exact wording  [{row['id']}]")
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "duplicates":
        return _print_duplicates()
    if cmd == "set-aside":
        return _print_set_aside()
    if cmd == "demo":
        return _demo()
    if cmd == "identity":
        return _demo_identity()
    print(f"usage: forget.py [duplicates|set-aside|demo|identity]\nunknown command {cmd!r}",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
