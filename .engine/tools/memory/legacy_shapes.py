"""legacy_shapes.py — mint the record shapes an older engine wrote, so the readers that still honour them
can be tested.

WHY THIS EXISTS AT ALL. The engine once wrote AI summaries of each session (`episodic`, closed by a
`consolidated` marker), folded old summaries into a `gist` (closed by a `rolled-up` marker, each replaced raw
named by a `superseded` marker), and appended a `reinforcement` marker every time recall returned a record.
None of that is written any more — the whole curation lifecycle was deleted when eADR-0038 made the exact
transcript the canonical record.

But the records themselves are still there. Every store that has been running holds thousands of them, and
several live predicates exist ONLY to read them correctly: `forget._is_retired` hides the orphan summaries of
a pass that crashed before its marker landed, `forget._is_gist_orphan` hides a gist whose roll-up crashed,
`forget._is_superseded` hides a raw episode whose gist replaced it, and `compact._is_foldable` reclaims the
spent markers. Delete those predicates and a real store surfaces a summary beside the source it replaced, or
un-hides work an operator watched disappear years ago.

So the shapes need a mint, and it belongs HERE rather than in the modules that read them: production never
writes one, and a factory in `records.py` would be dormant code the moment it shipped. These are fixtures for
the tests that keep the legacy readers honest — the only callers, by design.

FAITHFUL, not approximate. Each function reproduces the envelope the deleted writer actually produced, field
for field, because a fixture that drifts from the real shape tests a predicate against a record no store
contains. The originals are recoverable from the merge that removed them; what is reproduced here is their
output, not their logic.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import records  # noqa: E402

DEFAULT_EPISODIC_TAG = "episodic"
MARKER_TAG = "consolidated"
# The closed set of labels the summary writer stamped onto what it produced. Nothing writes one now, and the
# search interface no longer offers a filter over them — but they are on thousands of stored records, and a
# test that exercises the readers has to be able to mint every one of them.
ROLE_VOCABULARY = (
    "decision", "rationale/pushback", "lesson", "dead-end", "preference", "intent", "observation",
)


def episodic(session_id: str, role: str, text: str, batch: str, *, tags=(), now: "int | None" = None) -> dict:
    """One AI-written summary of part of a session, keyed to the pass (`batch`) that wrote it."""
    stamp = int(time.time()) if now is None else now
    out_tags = [DEFAULT_EPISODIC_TAG]
    for t in tags:
        if isinstance(t, str) and t.strip() and t.strip() not in out_tags:
            out_tags.append(t.strip())
    return {"v": 1, "kind": records.EPISODIC_KIND, records.RECORD_ID_KEY: records.new_record_id(),
            "session_id": session_id, "ts": stamp, "role": role, "text": text.strip(), "tags": out_tags,
            "consolidated_ts": stamp, records.BATCH_KEY: batch}


def marker(session_id: str, batch: str, *, through_seq: "int | None" = None,
           now: "int | None" = None) -> dict:
    """The marker that CLOSES a summarising pass. Its absence is what makes that pass's summaries orphans."""
    out = {"v": 1, "kind": records.MARKER_KIND, records.RECORD_ID_KEY: records.new_record_id(),
           "session_id": session_id, "ts": int(time.time()) if now is None else now,
           "tags": [MARKER_TAG], records.BATCH_KEY: batch}
    if isinstance(through_seq, int) and not isinstance(through_seq, bool):
        out[records.THROUGH_SEQ_KEY] = through_seq
    return out


def gist(session_key: str, text: str, source_ids, batch: str, *, role: str = "lesson",
         now: "int | None" = None) -> dict:
    """The compact record several summaries were folded into. `session_key` may be a `tag:` cluster sentinel
    when the fold crossed sessions — which is why the transcript window still resolves one."""
    return {"v": 1, "kind": records.GIST_KIND, records.RECORD_ID_KEY: records.new_record_id(),
            "session_id": session_key, "ts": int(time.time()) if now is None else now,
            "role": role, "text": text.strip(), "tags": [records.GIST_TAG],
            records.SOURCE_IDS_KEY: list(source_ids), records.BATCH_KEY: batch}


def rollup_marker(session_key: str, batch: str, *, now: "int | None" = None) -> dict:
    """The marker that CLOSES a roll-up pass. Until it lands, every supersession in the batch is inert — the
    crash-safety that stops a raw episode being hidden without the gist that replaced it."""
    return {"v": 1, "kind": records.ROLLUP_KIND, records.RECORD_ID_KEY: records.new_record_id(),
            "session_id": session_key, "ts": int(time.time()) if now is None else now,
            "tags": [records.GIST_TAG], records.BATCH_KEY: batch}


def superseded(raw_id: str, gist_id: str, batch: str, *, now: "int | None" = None) -> dict:
    """One raw episode's supersession: it names the raw it retires and the gist that replaced it."""
    return {"v": 1, "kind": records.SUPERSEDED_KIND, records.RECORD_ID_KEY: records.new_record_id(),
            "ts": int(time.time()) if now is None else now, records.TARGET_KEY: raw_id,
            records.SUPERSEDED_BY_KEY: gist_id, records.BATCH_KEY: batch}


def reinforcement(target_id: str, *, now: "int | None" = None) -> dict:
    """One access marker — what recall appended for every record it returned, back when reading wrote."""
    return {"v": 1, "kind": records.REINFORCEMENT_KIND, records.RECORD_ID_KEY: records.new_record_id(),
            records.TARGET_KEY: target_id, "ts": int(time.time()) if now is None else now,
            "tags": [records.REINFORCEMENT_TAG]}


# --- appending helpers -------------------------------------------------------------------------------------
# The deleted writers did more than mint: they appended a whole pass in a strict order whose crash points are
# exactly what the surviving predicates key on. A fixture that appends in the wrong order tests nothing, so
# these reproduce the order rather than leaving each caller to remember it.
#
# THEY REFUSE TO WRITE TO THE DEFAULT STORE. `ledger.append(path=None)` resolves to the operator's real ledger,
# and these mint shapes no live path produces — so a caller that forgot to point them at a throwaway cabinet
# would quietly plant retired records in real memory. An explicit path is required, and the demos that use
# these run under a temporary ENGINE_MEMORY_DIR, which resolves before this check.


def _refuse_default_store(path) -> None:
    """Refuse an append with no explicit destination unless the environment already redirects the store."""
    if path is None and not os.environ.get("ENGINE_MEMORY_DIR"):
        raise RuntimeError(
            "legacy_shapes writes record shapes nothing produces any more; it refuses to append to the real "
            "store. Pass an explicit `path=`, or point ENGINE_MEMORY_DIR at a throwaway cabinet.")

def store_episodic(session_id: str, entries, *, batch: "str | None" = None, path=None,
                   close: bool = True) -> str:  # noqa: D401
    """Append a summarising pass: its summaries, then the marker that closes it. Returns the batch id.
    `close=False` leaves the pass OPEN — the crashed shape whose summaries recall must retire as orphans."""
    import uuid
    from memory import ledger
    _refuse_default_store(path)
    batch = batch or uuid.uuid4().hex
    for e in entries:
        ledger.append(episodic(session_id, e["role"], e["text"], batch, tags=e.get("tags") or ()), path=path)
    if close:
        ledger.append(marker(session_id, batch), path=path)
    return batch


def store_gist(session_key: str, text: str, source_ids, *, batch: "str | None" = None, path=None,
               close: bool = True, role: str = "lesson") -> dict:
    """Append a roll-up pass in its real order: the gist, then one supersession per replaced raw, then the
    marker that closes the batch LAST. Returns the gist record. `close=False` leaves the batch un-closed —
    the crash shape that must leave every supersession INERT, so no raw is hidden without its gist."""
    import uuid
    from memory import ledger
    _refuse_default_store(path)
    batch = batch or uuid.uuid4().hex
    g = gist(session_key, text, source_ids, batch, role=role)
    ledger.append(g, path=path)
    for rid in source_ids:
        ledger.append(superseded(rid, g[records.RECORD_ID_KEY], batch), path=path)
    if close:
        ledger.append(rollup_marker(session_key, batch), path=path)
    return g
