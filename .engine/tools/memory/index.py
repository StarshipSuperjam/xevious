"""index.py — the engine's derived memory lookup: the throwaway SQLite/FTS5 accelerator + the plain-scan floor.

The ledger (`ledger.py`) is the ONE source of truth. This module builds a FAST lookup over it — a
SQLite FTS5 full-text index — and, beneath that, a SLOW backup lookup: a plain scan straight through the
ledger, for when a machine's SQLite was built without the FTS5 module. The promise is the locked law: recall
always answers — *availability holds, latency does not*. When FTS5 is absent the answer still comes back,
just slower.

This index is DERIVED and THROWAWAY. It is rebuilt from the ledger and is never the only copy; deleting it
loses nothing (`rebuild()` reconstructs it), and backup is still "copy the ledger", never this file.

Leaf discipline: this module DETECTS the FTS5-absent / slow-path condition and RETURNS it to the caller; it
never renders operator-facing prose (boot does that). It writes no telemetry and logs no findings.

This module builds the index machinery + the two retrieval paths, record-shape-agnostic and UNRANKED (`query`).
Ranked, filtered recall is `search` (BM25 best-first, equally-relevant matches newest first; tag and session
filters) — the RANKING ENGINE beneath the `search.json` contract, not the conforming implementation of it. The
engine-memory MCP server (`mcp_server.py`) is what conforms: it supplies the default bound the contract requires
on an omitted `limit` (this library leaves it unbounded, which only a caller passing its own ceiling can rely on)
and it is the declared fallback handle. `query` stays UNRANKED for the
rebuild/scan callers. Nothing queries this index per prompt: the boot-owned `scent.py` UserPromptSubmit hook
pushes a constant cue asking the model to run the recall workflow, and every query here is one the model then
makes deliberately.

Both retrieval paths split text into words with ONE tokenizer (`_tokenize`, modeled on SQLite's FTS5
`unicode61`): the fast lookup is BUILT from the tokens it produces (the FTS table is contentless — it keeps the
inverted index, not a second copy of the text), and the slow scan matches the same way. That one
shared word-splitter — not FTS5's own, which folds some scripts differently — is what makes the slow backup
return the same set of records the fast lookup does, not a degraded different answer.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field

# Make the package parent (.engine/tools) importable so `from memory import ledger` resolves even when this
# file is run directly as a script (the demo). When imported as `memory.index`, the parent is already on
# sys.path, so this is a guarded no-op. (Not FS/DB work — close.py never imports this module, only the
# side-effect-free package `__init__`, so the leaf-safety invariant is untouched.)
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import forget, ledger, records  # noqa: E402

INDEX_FILENAME = "index.sqlite3"
# The index's own shape version, stamped into `meta` and checked on every read. Bump it whenever what the
# index is allowed to CONTAIN changes (membership), or how it is built changes (projection, tokenizer, table
# shape) — a generation bump cannot signal any of those, because generation tracks the ledger, not the rules.
#   1 — curated records only; content-bearing FTS table.
#   2 — captured conversation admitted (harness-injected pseudo-turns excluded); contentless FTS table.
#   3 — fused harness blocks removed from the searchable projection.
#   4 — the archived-tier age-out removed, so records an older index left out are now members.
#   5 — the operator's withholds are members of the index's own state: it stamps what was withheld when it
#       was built, so the incremental update can honour a withhold it did not itself see.
INDEX_SCHEMA_VERSION = 5
_FTS_PROBE_TABLE = "engine_fts5_probe"
# Top-level record fields kept OUT of the searchable text. `tags` honors the locked typing law (tags are a
# secondary filter, never in the FTS body, so tag drift never poisons term statistics). The capture-record
# ENVELOPE metadata is excluded for the same reason: `session_id` (a per-session UUID — its hex fragments are
# real words: dead/beef/cafe/face…), `kind` ("turn-delta"), and `speaker` ("user"/"assistant") are provenance,
# not content, and indexing them makes `query("user")`/`query("delta")` match every record. Only the human
# `text` (and any other non-metadata string leaf) is searchable. A `role` an older engine stamped on a summary is a label, not content, so it joins this set: searching a label like "decision" must
# never drag in every record that carries it (the same pollution the capture-record provenance fields would
# cause). Episodic provenance (`consolidated_ts`, `source_seqs`) is non-string and stays out by type. The
# per-pass `batch` id the forgetting step adds is a uuid — its hex fragments are real words, exactly the
# `session_id` problem — so it joins this set too. The per-record `id` is also a uuid hex (its only
# purpose is to NAME a record, never to be searched), so it joins for the same reason. The reinforcement
# marker's `target` is a uuid hex too — it points at the reinforced record's `id` — so it joins as
# well (the marker is dropped from recall by `forget.live_records` before indexing, but this keeps it out of
# the body even if it were reached). The carried `tier` (a compaction carry) is a STRING
# ("hot"/"cold"/"archived"), so it MUST join too, else those words would match every compacted record; its
# sibling carried fields (frecency_snapshot/snapshot_ts/last_access_ts) are numeric and stay out of the body by
# type already (the projection indexes only string leaves). The gist roll-up adds two more uuid-hex
# fields: a raw episode's `superseded_by` (the gist id a compaction folded onto it) and a gist's `source_ids`
# (the list of raw ids it consolidates) — both are uuid hex, exactly the `id`/`batch` problem, so both join too.
_TAGS_KEY = "tags"
_NON_BODY_KEYS = frozenset(
    {"tags", "session_id", "kind", "speaker", "role",
     records.BATCH_KEY, records.RECORD_ID_KEY, records.TARGET_KEY, records.TIER_KEY,
     records.SUPERSEDED_BY_KEY, records.SOURCE_IDS_KEY, records.SCORE_KEY, records.MERGE_SHA_KEY,
     # A withhold marker's session target and a pin's source session are both uuid hex — the same
     # fragments-are-real-words problem every id field has. A pin's route is worse still: "assistant" and
     # "cli" are ordinary words, so indexing that field would make every pin match a search for either.
     records.TARGET_SESSION_KEY, records.PIN_SOURCE_SESSION_KEY, records.PIN_VIA_KEY}
)


@dataclass
class RebuildReport:
    """The outcome of rebuilding the fast lookup. `fts5` is False when this machine has no FTS5, in which case
    no index is built (recall uses the slow scan). `with_text` counts how many indexed records had any
    searchable text — a record with no string content is indexed but unsearchable, surfaced so the fast and
    slow paths cannot silently diverge. Leaf law: returned, never logged."""

    indexed: int = 0
    with_text: int = 0
    fts5: bool = True
    path: str = ""


@dataclass
class QueryResult:
    """The records matching a query. `query` returns them in ledger order (UNRANKED); `search` returns
    them ranked best-first, each a shallow copy carrying `records.SCORE_KEY` (the lexical relevance). `degraded` is
    True when the answer came from the slow backup scan (FTS5 absent, the fast lookup not yet built, or scan forced)."""

    records: list = field(default_factory=list)
    degraded: bool = False


def _tokenize(text: str) -> list:
    """Tokenize text the way SQLite's FTS5 `unicode61` tokenizer does, so the slow backup lookup finds the SAME
    records the fast lookup does.

    This is the SINGLE folding authority for both lookup paths: the fast lookup stores the tokens this produces
    (and FTS5 is configured to add no diacritic folding of its own — see `_build_schema`), and the slow scan
    tokenizes the same way. So the two paths agree across scripts whose folding FTS5 handles differently from
    Python (Cyrillic, Greek, accented Latin) — not just Latin text.

    The rule, matching FTS5 `unicode61`: split on every codepoint that is not a letter or number (so
    `snake_case_config` is three tokens), fold case with `str.lower()` (NOT `casefold()` — casefold turns ß
    into ss, which unicode61 does not), and strip diacritics via NFD (canonical, NOT NFKD compatibility, so
    `Ⅳ` stays `ⅳ` rather than becoming `iv`) + dropping combining marks (so `café` matches `cafe`).

    Residual: FTS5 still applies its OWN case-fold to the stored tokens, which differs from `str.lower()` in a
    few exotic corners (e.g. Greek final sigma `ς` vs `σ`). Such a record is still recalled by the scan, so the
    locked law's guarantee — availability — holds; only the fast path's match set differs there, never a wrong
    result.
    """
    folded = unicodedata.normalize("NFD", text.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    tokens = []
    current = []
    for ch in folded:
        if unicodedata.category(ch)[0] in ("L", "N"):
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _record_text(record) -> str:
    """The searchable text for one record — the projection BOTH lookup paths use (so they agree).

    Gathers the record's string leaf values and joins them, EXCLUDING the top-level envelope-metadata keys
    in `_NON_BODY_KEYS` (the locked tags-not-in-the-FTS-body law, plus the capture-record provenance fields
    that are not content), and replacing any fused harness block with a marker (`records.mark_harness_spans`)
    so engine-inserted text is never searchable as the operator's words. Otherwise shape-agnostic; the
    reflection step finalizes the projection against the full record shape.
    """
    parts: list = []

    def walk(value) -> None:
        if isinstance(value, str):
            # A fused harness block is not content: it arrives inside a real turn, marked as spoken by the
            # operator, and indexing it would make engine-inserted text keyword-findable under their name.
            # Removed from the SEARCHABLE projection only — the stored record keeps every byte.
            parts.append(records.mark_harness_spans(value))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    if isinstance(record, dict):
        for key, value in record.items():
            if key in _NON_BODY_KEYS:
                continue
            walk(value)
    else:
        walk(record)
    return " ".join(parts)


def fts5_available(conn: "sqlite3.Connection | None" = None) -> bool:
    """True if this machine's SQLite has the FTS5 full-text module compiled in.

    The locked law: when FTS5 is absent the fast lookup is unavailable and recall falls back to the slow scan
    — availability holds, latency does not. This DETECTS and RETURNS that condition; boot renders the
    operator-facing disclosure. Absence is decided ONLY here, by probing — never by catching a query-time
    error, because a malformed MATCH raises the same `sqlite3.OperationalError` and must not be mislabeled
    "FTS5 absent".
    """
    own = conn is None
    if own:
        conn = sqlite3.connect(":memory:")
    try:
        conn.execute(f"CREATE VIRTUAL TABLE temp.{_FTS_PROBE_TABLE} USING fts5(x)")
        conn.execute(f"DROP TABLE temp.{_FTS_PROBE_TABLE}")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        if own:
            conn.close()


def index_path(cwd: "str | None" = None) -> str:
    """The derived-index file: a sibling of the ledger, in the gitignored `.engine/memory/` data dir."""
    return os.path.join(ledger.ledger_dir(cwd), INDEX_FILENAME)


def _build_schema(conn: sqlite3.Connection) -> None:
    # `entries` holds the full record per ledger ordinal (so a hit hydrates the exact record — the provenance
    # `search` ranks over). `entries_fts` is a standalone FTS5 index keyed by the same ordinal, fed the
    # PRE-FOLDED token stream from `_tokenize`. `remove_diacritics 0` tells FTS5 to do no diacritic folding of
    # its own — `_tokenize` already did it — so the indexed tokens are exactly what the scan path matches
    # against and the two paths agree across scripts. No porter stemming — `search` ranks the un-stemmed
    # tokens (bm25 over this body); stemming stays a future ranking concern.
    conn.execute("CREATE TABLE entries (ord INTEGER PRIMARY KEY, record_json TEXT NOT NULL)")
    # CONTENTLESS (`content=''`): FTS5 keeps the inverted index but does NOT keep a second copy of the body.
    # Nothing reads the body back — `_ranked` and `query` both MATCH and then hydrate the record from `entries`
    # — so the copy was pure duplication. It stopped being negligible when the conversation became recall
    # content: measured through this very `rebuild` over the maintainer's real store, the index went from 894
    # records / 2.3 MB to 21,507 / 20.5 MB, where the same pipeline keeping a second copy of the text costs
    # ~31 MB — so contentless saves roughly a third, at the same rebuild time (1.8 s) and with bm25 unaffected
    # (FTS5 still keeps the per-document lengths bm25 normalises by). An earlier figure in this comment claimed
    # 72.2 -> 45.5 MB; that was measured with the injected-pseudo-turn exclusion switched off and overstated
    # both the size and the saving. This is a schema change, so it belongs with the change that made the index
    # an order of magnitude larger rather
    # than after it. The index is derived and throwaway, so an older index built the other way is simply
    # replaced by the next rebuild.
    conn.execute("CREATE VIRTUAL TABLE entries_fts USING fts5(body, content='', "
                 "tokenize='unicode61 remove_diacritics 0')")
    # `meta` carries the ledger GENERATION this index was built against. `query` trusts the fast
    # lookup only when this matches the ledger's current generation — so a compaction that swapped the ledger
    # out from under a stale index is detected and the query falls back to the always-correct scan, never a
    # stale fast answer (a full index rebuild gated on a monotonic ledger-generation stamp).
    # `meta` carries BOTH the ledger generation this index was built against AND the index's own SCHEMA
    # VERSION. The generation leg alone is not enough, and the gap is not theoretical: generation moves only on
    # compaction, so a change to what the index is allowed to CONTAIN leaves an existing index stamped current
    # while holding the wrong set. That is exactly what admitting captured conversation did — an index built
    # before it holds no conversation at all, matches on generation, and `_ranked` answers from it reporting
    # `degraded=False` while the plain scan finds the turns. Silent, and it heals only when some unrelated event
    # happens to force a rebuild. The version leg forces an old-SHAPE index to be treated as stale, exactly as
    # `knowledge_index.INDEX_SCHEMA_VERSION` does for the knowledge graph. Bump it whenever membership, the
    # projection, the tokenizer or the table shape changes. Removing the archived-tier age-out is the second
    # membership change to need it, and the failure was reproduced rather than assumed: an index built by the
    # previous version over a store holding one 60-day-old note answered a query for that note with nothing and
    # `degraded=False`, while the plain scan answered with the note. A change to what recall MAY reach is
    # exactly a change to what the index is allowed to contain, even when no line of the build code moved.
    # `withheld` carries the operator's withholds AS OF THIS BUILD, as JSON. It is state the index needs about
    # itself, not a second copy of the ledger's truth: `extend` inserts a freshly captured turn without ever
    # reading the ledger, so without this it re-admits turns from a conversation the operator withheld — and
    # it does so on the fast path only, which is the divergence direction that RESURFACES withheld content.
    # Stamping is sound precisely because `extend` refuses to touch an index whose generation no longer
    # matches, and a withhold bumps the generation: so whenever `extend` runs at all, this stamp is current.
    conn.execute("CREATE TABLE meta (rowid INTEGER PRIMARY KEY, generation INTEGER NOT NULL, "
                 "schema_version INTEGER NOT NULL DEFAULT 0, withheld TEXT NOT NULL DEFAULT '[]', "
                 "index_epoch INTEGER NOT NULL DEFAULT 0)")


def _index_is_current(conn: sqlite3.Connection, src: str) -> bool:
    """True iff the fast index may be trusted for `src`: its stamped ledger generation matches the ledger's
    CURRENT generation AND its stamped schema version matches this module's. Both legs are load-bearing and
    they catch different failures — the generation leg catches a compaction that swapped the ledger out from
    under the index; the schema leg catches an index built when the rules about what belongs in it were
    different, which no generation bump would ever signal. A missing or unreadable stamp reads as stale, so an
    index built before this stamp existed falls back to the always-correct scan rather than answering
    confidently from the wrong set."""
    if _index_epoch_of(conn) != ledger.index_epoch(for_path=src):
        return False
    return (_index_schema_version(conn) == INDEX_SCHEMA_VERSION
            and _index_generation(conn) == ledger.generation(for_path=src))


def _index_epoch_of(conn: sqlite3.Connection) -> int:
    """The index epoch this index was built against. Guarded like `_index_schema_version`: an index built by an
    older engine has no such column, and reading -1 there makes it honestly stale rather than raising."""
    try:
        row = conn.execute("SELECT index_epoch FROM meta WHERE rowid = 1").fetchone()
    except sqlite3.Error:
        return -1
    val = row[0] if row else None
    return val if isinstance(val, int) and not isinstance(val, bool) else -1


def _stamped_withholds(conn: sqlite3.Connection) -> tuple:
    """`({record_id}, {session_id})` the index was built under, from its own `meta` row.

    Guarded exactly the way `_index_schema_version` is: an older index has no such column, and a read that
    raised here would land inside the incremental update, whose whole contract is that it never gates a turn's
    close. Failure returns EMPTY sets, which is the direction that admits a record rather than dropping one —
    safe because a withhold bumps the generation, so an index that has not seen it is already refused by
    `_index_is_current` before this is consulted."""
    try:
        row = conn.execute("SELECT withheld FROM meta WHERE rowid = 1").fetchone()
        data = json.loads(row[0]) if row and row[0] else {}
        return set(data.get("ids") or ()), set(data.get("sessions") or ())
    except (sqlite3.Error, ValueError, TypeError, IndexError):
        return set(), set()


def _index_schema_version(conn: sqlite3.Connection) -> int:
    """The index's own schema version, or -1 when absent/unreadable — including the pre-stamp shape, whose
    `meta` table has no such column, so the SELECT raises and this returns the never-matching -1."""
    try:
        row = conn.execute("SELECT schema_version FROM meta WHERE rowid = 1").fetchone()
    except sqlite3.Error:
        return -1
    if not row:
        return -1
    val = row[0]
    return val if isinstance(val, int) and not isinstance(val, bool) else -1


def _index_generation(conn: sqlite3.Connection) -> int:
    """The ledger generation the fast index was built against (its `meta` row), or -1 if absent/unreadable — a
    value that never equals a real ledger generation (>= 0), so an unstamped/old index is treated as stale and
    the query falls back to the slow scan."""
    try:
        row = conn.execute("SELECT generation FROM meta WHERE rowid = 1").fetchone()
    except sqlite3.Error:
        return -1
    if not row:
        return -1
    val = row[0]
    return val if isinstance(val, int) and not isinstance(val, bool) and val >= 0 else -1


def rebuild(*, ledger_file: "str | None" = None, index_file: "str | None" = None) -> RebuildReport:
    """Rebuild the fast lookup from the ledger (the one source of truth).

    Throwaway-safe: builds a fresh index in a uniquely-named temp file IN THE TARGET DIRECTORY, closes it, then
    atomically `os.replace`s it into place — so a crash mid-rebuild leaves the previous index intact and a
    reader never sees a half-built one. Streams the ledger via `forget.live_records` (logically-retired
    duplicates excluded from recall; malformed/torn lines dropped), so one bad line never costs the rest and a
    crash-duplicated summary is indexed once. If this machine has no FTS5, there is no fast lookup to build and
    this returns a no-op report (recall uses the slow scan). Stamps the ledger generation it built against
    so `query` can detect a compaction-staled index and fall back to the scan.
    """
    src = ledger.ledger_path() if ledger_file is None else ledger_file
    dst = index_path() if index_file is None else index_file
    if not fts5_available():
        return RebuildReport(fts5=False, path=dst)
    parent = os.path.dirname(dst) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".index-build-", suffix=".sqlite3")
    os.close(fd)
    report = RebuildReport(path=dst)
    try:
        # Default rollback journal (NOT WAL): a clean close leaves no -wal/-shm sidecars, so the whole index is
        # the single file we atomically replace into place.
        conn = sqlite3.connect(tmp)
        try:
            _build_schema(conn)
            # READ THE LINEAGE BEFORE STREAMING, never after. This build takes over a second on a real store
            # and holds no lock, so a withhold landing mid-stream would otherwise be stamped as though the
            # index had seen it — leaving a withheld record in the fast answer, reported `degraded=False`,
            # with nothing to invalidate it until the next bump. Reading first makes that race stamp the index
            # honestly STALE instead, which costs one wasted rebuild and never a wrong answer.
            built_generation = ledger.generation(for_path=src)
            built_epoch = ledger.index_epoch(for_path=src)
            w_ids, w_sessions = forget.withheld_targets(src)
            ordinal = 0
            # `live_records` excludes logically-retired duplicates (a crashed pass's orphans) — the SAME shared
            # filter the slow `_scan` uses, so the fast and slow lookups retire identically (parity).
            for record in forget.live_records(path=src):
                tokens = _tokenize(_record_text(record))
                conn.execute(
                    "INSERT INTO entries (ord, record_json) VALUES (?, ?)",
                    (ordinal, json.dumps(record, ensure_ascii=False, separators=(",", ":"))),
                )
                # Store the PRE-FOLDED token stream (space-joined), not the raw text, so the fast lookup
                # indexes exactly the tokens the scan matches against — see `_tokenize` / `_build_schema`.
                conn.execute("INSERT INTO entries_fts (rowid, body) VALUES (?, ?)", (ordinal, " ".join(tokens)))
                report.indexed += 1
                if tokens:
                    report.with_text += 1
                ordinal += 1
            # Stamp the ledger generation this index was built against — `query`'s fast path is
            # trusted only while it matches `ledger.generation`. Resolved from the SAME ledger file being read
            # (its sidecar sibling), never the default dir, so an explicit `ledger_file=` build stamps its own
            # store's generation.
            conn.execute(
                "INSERT INTO meta (rowid, generation, schema_version, withheld, index_epoch) "
                "VALUES (1, ?, ?, ?, ?)",
                (built_generation, INDEX_SCHEMA_VERSION,
                 json.dumps({"ids": sorted(w_ids), "sessions": sorted(w_sessions)}, separators=(",", ":")),
                 built_epoch),
            )
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp, dst)
    except BaseException:
        # Leave any prior index untouched; discard the half-built temp.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return report


def extend(new_records: list, *, ledger_file: "str | None" = None, index_file: "str | None" = None) -> int:
    """Add just-appended ledger records to the EXISTING fast index, returning how many rows were inserted.

    Why this exists. Nothing else refreshes the index between full rebuilds, and `ledger.append` does not move
    the generation stamp (only compaction does). Before the conversation became recall content that was
    harmless: every record `live_records` yielded was written by a path that rebuilds afterwards, and captured
    turns — the only records appended without one — were excluded by kind anyway. Now they are content, so
    without this a captured turn sits in the ledger while the index does not hold it AND the generation still
    MATCHES — so `query`/`search` would trust the fast path, return the stale set, and report `degraded=False`
    while the plain scan found the turn. That is a silent fast/slow divergence, exactly what the parity law
    forbids, and it would be invisible to any test built on the usual throwaway cabinet, because those rebuild
    before querying.

    Appends at the next free ordinal. Ledger order is append-only, so a row added here keeps `ord` monotone in
    ledger position — which is all the ranking tiebreak asks of it.

    NARROW CONTRACT, deliberately: this accepts CAPTURED TURNS ONLY and rejects any other kind outright. A full
    `rebuild` streams `forget.live_records`, which ORs together four exclusions (injected capture, crash-orphan
    retirement, gist-orphan + supersession, and the bookkeeping markers); this applies the one that can apply to a turn just
    written. Passing anything else would let the fast path hold a record `rebuild` and the plain scan both drop
    — a fast/slow divergence in the direction that RESURFACES set-aside content, which is the worse direction.
    Rejecting is cheap and keeps the invariant true rather than merely usually-true.

    BEST-EFFORT BY CONTRACT: every failure path returns 0 and leaves the index untouched, because the caller is
    end-of-turn capture and ambient capture must never gate a turn's close. Declines silently when this machine
    has no FTS5, when no index exists yet, or when the index's stamped epoch/generation no longer matches the
    ledger's: in that last case the index is already stale and every reader already knows it, so extending it
    would be writing into a file none of them trusts.

    A FAULT MARKS THE INDEX STALE RATHER THAN LEAVING A HOLE. This used to say a skipped extend was
    self-healing, because the next full rebuild — consolidation, roll-up, compaction or restore — would
    reconstruct from the ledger. Three of those four are gone with the curation lifecycle, and the fourth is
    operator-initiated, so nothing routinely rebuilds any more. That turned a transient fault here (a locked
    database, a disk hiccup) into a PERMANENT hole: the turn would sit in the ledger while the index stayed
    stamped CURRENT, so the fast path would answer authoritatively without it and report `degraded=False`,
    while the plain scan found it. So a fault now bumps the index epoch, which makes `_index_is_current` false
    and the next search rebuild. Costs one rebuild; the alternative is a silently missing turn forever."""
    src = ledger.ledger_path() if ledger_file is None else ledger_file
    dst = index_path() if index_file is None else index_file
    if not new_records or not fts5_available() or not os.path.exists(dst):
        return 0
    inserted = 0
    try:
        conn = sqlite3.connect(dst)
        try:
            if not _index_is_current(conn, src):
                return 0
            # The operator's withholds, as the index itself recorded them. A withheld SESSION is the case that
            # matters: withholding a conversation the operator is still in is the most natural way to use the
            # control, and every turn captured after it would otherwise be inserted straight back here — found
            # by the fast path, absent from the scan, and reported as an authoritative answer.
            w_ids, w_sessions = _stamped_withholds(conn)
            row = conn.execute("SELECT MAX(ord) FROM entries").fetchone()
            ordinal = (row[0] + 1) if row and isinstance(row[0], int) else 0
            for record in new_records:
                if not isinstance(record, dict) or record.get("kind") != records.AMBIENT_CAPTURE_KIND:
                    continue          # the narrow contract: captured turns only (see the docstring)
                if forget._is_excluded_capture(record):
                    continue
                if forget.is_withheld(record, w_ids, w_sessions):
                    continue          # the operator took this conversation out of recall; a new turn in it is
                    # not a new exception to that
                tokens = _tokenize(_record_text(record))
                conn.execute(
                    "INSERT INTO entries (ord, record_json) VALUES (?, ?)",
                    (ordinal, json.dumps(record, ensure_ascii=False, separators=(",", ":"))),
                )
                conn.execute("INSERT INTO entries_fts (rowid, body) VALUES (?, ?)", (ordinal, " ".join(tokens)))
                ordinal += 1
                inserted += 1
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — the index is derived and rebuildable; capture must never fail for it
        # The turn is in the ledger and may not be in the index. Say so, so the next search repairs it: this
        # runs inside the capture lock the caller already holds, which is what the epoch bump requires.
        try:
            ledger.bump_index_epoch(for_path=src)
        except Exception:  # noqa: BLE001 — even the honesty is best-effort; capture still must not fail
            pass
        return 0
    return inserted


def _scan(query_tokens: list, src: str, limit: "int | None") -> list:
    """The slow backup lookup: read the ledger straight through, tokenize each record the same way, and keep
    the records whose text contains EVERY query token. Always available (no FTS5 needed). This is the single
    fallback path that both a genuine FTS5-absent machine and `force_scan=True` flow through, so exercising one
    is real evidence for the other. Reads through `live_records`, the same retirement filter the fast `rebuild`
    bakes in, so the slow backup returns the deduped set the fast lookup does (parity)."""
    want = set(query_tokens)
    out = []
    for record in forget.live_records(path=src):
        have = set(_tokenize(_record_text(record)))
        if want <= have:
            out.append(record)
            if limit is not None and len(out) >= limit:
                break
    return out


def query(
    text: str,
    *,
    limit: "int | None" = None,
    force_scan: bool = False,
    ledger_file: "str | None" = None,
    index_file: "str | None" = None,
) -> QueryResult:
    """Recall the records matching `text` — every query word must appear (implicit AND). Uses the fast lookup
    when this machine has FTS5 and the index exists; otherwise the slow backup scan over the ledger. Both paths
    apply the SAME tokenizer, so they return the same set of records. UNRANKED (ledger order) on purpose — this
    is the membership primitive the rebuild/scan callers need; ranked recall is `search`.
    """
    src = ledger.ledger_path() if ledger_file is None else ledger_file
    dst = index_path() if index_file is None else index_file
    tokens = _query_terms(text)
    if not tokens:
        return QueryResult(records=[], degraded=False)
    if (not force_scan) and fts5_available() and os.path.exists(dst):
        # Fast path. Per-token double-quoting neutralizes any FTS5 MATCH syntax — the tokens are letters/numbers
        # only (the tokenizer already stripped operators), so this is belt-and-suspenders.
        match = " ".join('"' + token + '"' for token in tokens)
        sql = (
            "SELECT e.record_json FROM entries_fts "
            "JOIN entries e ON e.ord = entries_fts.rowid "
            "WHERE entries_fts MATCH ? ORDER BY e.ord"
        )
        params: list = [match]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        try:
            conn = sqlite3.connect(dst)
            try:
                # Trust the fast lookup only while its stamped generation matches the ledger's current one
                # A mismatch means a compaction swapped the ledger out from under this index — treat
                # it like a missing index and fall through to the always-correct scan over the CURRENT ledger,
                # never a stale fast answer. The stamp is read from the same `conn`; the ledger generation from
                # the queried ledger file's own sidecar.
                if _index_is_current(conn, src):
                    rows = conn.execute(sql, params).fetchall()
                    records = [json.loads(row[0]) for row in rows]
                    return QueryResult(records=records, degraded=False)
            finally:
                conn.close()
        except sqlite3.Error:
            # The fast lookup file is present but unreadable/corrupt (a truncated copy, a disk error, a stale
            # pre-atomic file). Availability law: fall through to the slow backup rather than take recall down.
            # A malformed MATCH cannot land here (the tokens are always valid), so this only ever catches a
            # broken index, never a bad query.
            pass
    return QueryResult(records=_scan(tokens, src, limit), degraded=True)


# --- Ranked recall: the `search` interface ------------------------------------------------------
# `query` (above) answers UNRANKED — it is the rebuild/scan workhorse and must stay order-stable for its callers
# and tests. `search` is the ranked, filtered recall the `search.json` contract names: best-first by lexical
# relevance, reinforced by usage, with optional role/tag filters. It is SIDE-EFFECT-FREE — it never reinforces and
# never writes the ledger; the live reinforcement-on-recall caller is the engine-memory MCP server (mcp_server.py),
# at the recall boundary, never here (rebuild/_scan/the demos all call read-only).

# RANKING: lexical relevance alone, ties broken NEWEST FIRST.
#
# There used to be a second term — how often a record had been recalled — and a rounding step that grouped
# near-equal matches into one relevance bucket so that usage could reorder inside it. Both are gone with
# reinforcement-on-read (eADR-0038 ends per-record scoring). Removing the usage term alone would have left the
# rounding as pure information loss AND left the final tiebreak deciding on its own: ledger position ascending,
# which is OLDEST first. On a store that is overwhelmingly conversation, and on exactly the broad query where
# bm25's separating term collapses and every match ties, that would have answered every such question with the
# oldest thing in the store. So the rounding goes with it and the tiebreak flips: equal relevance is broken by
# ledger position DESCENDING — the most recent of equally-good matches, which is the one more likely meant.


def _query_terms(text: str) -> list:
    """The query's terms: the shared tokenization, with repeats collapsed and first-seen order kept.

    Deduping belongs HERE and never in `_tokenize`, which also splits record bodies — a body's repeats are
    exactly what term frequency counts. A query's are not: "cache cache" asks the same question as "cache".
    It is also what keeps the two retrieval paths honest. The fast path hands its terms to fts5 as a MATCH
    expression, and fts5 sums a repeated term's bm25 contribution once per occurrence, so an undeduped repeat
    scored a record at 7.815 where the plain scan — which counts each distinct term once — scored the same
    record 3.907, and a wide enough gap moved records across relevance buckets and reordered the answer.
    """
    return list(dict.fromkeys(_tokenize(text)))


def _passes_filters(record, tags, session=None) -> bool:
    """The structured POST-FETCH filters (tags/session are non-body — never FTS MATCH terms). `tags`: any-match
    — the record shares at least one of the requested tags. `session`: the record belongs to that one
    conversation. Both apply identically on the fast and slow paths, so the degraded path returns the same
    FILTERED set.

    THERE IS NO ROLE FILTER. There used to be, over a closed vocabulary the summary writer stamped onto what it
    produced. Nothing writes a `role` any more, so in any repository the engine deploys into, that filter could
    only ever have matched nothing — an input a caller could pass, be answered "no results" for, and reasonably
    read as "memory does not hold it". Removing it is the honest form.

    WHY SESSION IS A FILTER AND NOT A SECOND SEARCH. Reaching a remembered thing has two moves: find which
    conversation it was in, then find the moment inside it. The first was already answered; the second had no
    answer at all, so a hit that carried no position sent the reader to the start of the session to page
    forward — against a median session of well over a hundred records here, with the largest past what one
    window can hold. Scoping the SAME ranked search to one conversation makes the second move the same shape as
    the first, and being a filter rather than a new operation is what keeps one ranking and one seam."""
    if tags is not None:
        have = record.get("tags") if isinstance(record, dict) else None
        if not isinstance(have, (list, tuple)) or not (set(have) & tags):
            return False
    if session is not None:
        if not isinstance(record, dict) or record.get("session_id") != session:
            return False
    return True


def _rank_slice_score(candidates: list, limit: "int | None") -> list:
    """Order the candidates best-first, slice to `limit`, and attach `records.SCORE_KEY` (the lexical relevance) to
    a SHALLOW COPY of each kept record (never mutate the live record — the score must not leak back into the
    ledger/index). Each candidate is `(rel, ord, record)` with `rel` the positive lexical relevance
    (higher = better). Sort key: relevance DESC, then ledger `ord` DESC — newest of equally-good matches first,
    a total and deterministic order."""
    candidates.sort(key=lambda c: (-c[0], -c[1]))
    if limit is not None:
        candidates = candidates[:limit]
    out = []
    for rel, _ord, record in candidates:
        scored = dict(record) if isinstance(record, dict) else record
        if isinstance(scored, dict):
            scored[records.SCORE_KEY] = rel
        out.append(scored)
    return out


# SQLite fts5's own bm25 constants, so the plain-Python scan scores identically to the FTS5 index rather than
# approximately. `k1` damps repetition, `b` is how hard a long document is penalised for its length. The epsilon
# floor is fts5's too, and it is not a rounding detail: the textbook inverse-document-frequency goes NEGATIVE for
# a term carried by more than half the corpus, which would rank a document DOWN for containing the word it was
# searched for; fts5 clamps it to a positive sliver instead, so every match of a very common word scores the same
# near-zero and the usage tiebreak decides between them. Verified against fts5's own `bm25()` over a generated
# corpus: the largest disagreement across 685 scores was 8.5e-22.
_BM25_K1 = 1.2
_BM25_B = 0.75
_BM25_MIN_IDF = 1e-6


def _bm25_idf(n_docs: int, doc_freq: int) -> float:
    """The inverse document frequency of one query term, exactly as fts5 computes it. `doc_freq` can never
    exceed `n_docs` (it is counted in the same pass), so the logarithm's argument is never non-positive."""
    idf = math.log((n_docs - doc_freq + 0.5) / (doc_freq + 0.5))
    return idf if idf > 0.0 else _BM25_MIN_IDF


# How many matched rows the fast path fetches bodies for per round trip while walking the bm25 order. Sized from
# the caller's `limit` so a small page fetches little, with a floor that keeps a heavily-filtered query (which
# must keep reading past everything the filter rejects) from paying a round trip per record.
_HYDRATE_CHUNK = 200
_HYDRATE_MIN = 32


def _fast_candidates(conn, match, *, tags, session, limit):
    """The fast path's candidate list — ranked WITHOUT holding the whole matched set in memory.

    The old shape fetched every matching row up front and parsed each one into a record dict before ranking. On
    a curated-only store that was a rounding error. Once the conversation became recall content it was not: a
    single common word matches by the tens of thousands (20,092 records for "the" against the maintainer's
    store), so answering a ten-record query parsed and RETAINED the whole store — a measured 134 MB resident
    spike inside the long-lived recall server, growing linearly with the store.

    Two bounds, because they cover different queries and neither covers both:

    * **Nothing is retained but the sort key.** Each record is parsed, tested against the filters and released;
      only `(relevance, ord)` survives the walk, and the record bodies of the survivors are re-read at the end.
      Retained cost falls from a parsed dict per match to a flat tuple per match. This is the bound that holds
      for a COMMON word, where bm25's IDF term collapses to ~0 and tens of thousands of rows tie.
    * **The walk stops early once no unread row could reach the top `limit`.** `_rank_slice_score` sorts by
      relevance, then ledger position descending, and the SQL already returns rows in bm25 order. Once `limit`
      candidates have been kept, the relevance of the last of them is the BOUNDARY: a row with the SAME
      relevance can still overtake on the newer-first tiebreak, so it is read too, but a strictly worse row
      sorts behind all of them and can never reach the top `limit`. With no `limit` the caller asked for
      everything, so nothing is skipped.

    Either way the returned records are exactly the ones ranking the full matched set would have returned."""
    cur = conn.execute(
        "SELECT rowid, bm25(entries_fts) AS relevance FROM entries_fts "
        "WHERE entries_fts MATCH ? ORDER BY relevance", [match])
    span = _HYDRATE_CHUNK if limit is None else min(_HYDRATE_CHUNK, max(2 * limit, _HYDRATE_MIN))
    keys: list = []                      # (relevance, ord) — deliberately NOT the record
    boundary = None                      # the raw bm25 of the limit-th kept row; more-positive is worse
    done = False
    while not done:
        chunk = cur.fetchmany(span)
        if not chunk:
            break
        if boundary is not None and float(chunk[0][1]) > boundary:
            break                        # the whole chunk is past the boundary — do not read any of it
        bodies = dict(conn.execute(
            "SELECT ord, record_json FROM entries WHERE ord IN (%s)" % ",".join("?" * len(chunk)),
            [row[0] for row in chunk]).fetchall())
        for ordinal, relevance in chunk:
            raw = float(relevance)
            if boundary is not None and raw > boundary:
                done = True
                break
            record_json = bodies.get(ordinal)
            if record_json is None:
                continue                 # an fts row with no `entries` row — what the old JOIN dropped silently
            record = json.loads(record_json)
            if not _passes_filters(record, tags, session):
                continue
            # bm25 is more-negative for a better match; flip to a positive relevance (higher = better).
            keys.append((-raw, ordinal))
            if limit is not None and boundary is None and len(keys) >= limit:
                boundary = raw
        bodies = None                    # release the chunk before reading the next one
    return _hydrate_winners(conn, keys, limit)


def _hydrate_winners(conn, keys, limit):
    """Turn the surviving sort keys back into the `(rel, ord, record)` candidates `_rank_slice_score`
    expects, reading ONLY the records that can still make the answer. Sorts and slices by the same key that
    function does — doing it twice costs nothing and is what lets the walk above keep no record bodies at all."""
    keys.sort(key=lambda c: (-c[0], -c[1]))
    if limit is not None:
        keys = keys[:limit]
    if not keys:
        return []
    # CHUNKED, and that is not tidiness. An unlimited query keeps every match, and one placeholder per match
    # runs into SQLite's cap on parameters per statement (32,766 on the bundled build). Over that cap the
    # driver raises `OperationalError`, which is a `sqlite3.Error` — so `_ranked`'s broken-index guard would
    # have swallowed it and dropped a perfectly healthy index through to the full plain-Python scan, seven
    # times slower and reported only as `degraded`. Reproduced at 33,000 matches before this loop existed.
    bodies = {}
    for start in range(0, len(keys), _HYDRATE_CHUNK):
        ords = [k[1] for k in keys[start:start + _HYDRATE_CHUNK]]
        bodies.update(conn.execute(
            "SELECT ord, record_json FROM entries WHERE ord IN (%s)" % ",".join("?" * len(ords)),
            ords).fetchall())
    out = []
    for rel, ordinal in keys:
        record_json = bodies.get(ordinal)
        if record_json is not None:
            out.append((rel, ordinal, json.loads(record_json)))
    return out


def _heal_if_stale(src: str, dst: str) -> bool:
    """Rebuild the index when it exists but no longer matches the code that reads it. Returns whether it did.

    WHY THIS IS HERE. A stale index is not a crash — `_ranked` simply declines the fast path and answers from
    the full-ledger scan, correctly but far more slowly, and it will keep doing so for as long as the index
    stays stale. Nothing else brings it back on its own: the scheduled rebuilds all belong to the curation
    lifecycle, so a store can sit on the slow path indefinitely with every answer still correct and nothing
    saying why recall got slow. Measured on a real 29.7 MB store the difference was 1.15 s against 95 ms per
    query, and the repair takes about a second.

    Healing here rather than at a hook keeps it tied to the thing that actually notices: whoever reads is
    whoever repairs, so this survives any change of what runs at session start. It costs one cheap staleness
    read on a hot path and does real work only when the index is genuinely behind.

    FAIL-SOFT AND SILENT ON FAILURE, deliberately: a rebuild that cannot run leaves the slow path in place,
    which is the correct answer either way. Recall must not fail because a repair did.
    """
    if not fts5_available() or not os.path.exists(dst):
        return False                      # no index to heal, or no fast path to heal it for
    try:
        conn = sqlite3.connect(dst)
        try:
            if _index_is_current(conn, src):
                return False
        finally:
            conn.close()
        rebuild(ledger_file=src, index_file=dst)
        return True
    except Exception:
        return False


def _ranked(tokens, src, dst, *, tags, session, limit, force_scan):
    """The shared ranked retrieval. Fast path: bm25 read from the FTS5 index (when present +
    generation-current); slow path: a full scan over the ledger computing THE SAME bm25 in plain Python. So the
    two paths agree on the matched set AND on its order — the availability law now holds for the answer, not
    just for the fact that one comes back. NEITHER truncates in ledger order. The fast path stops walking its
    (already relevance-ordered) matches once no unread row could reach the top `limit` — a bound on work, never
    on the answer; see `_fast_candidates`. Returns a QueryResult."""
    if not force_scan:
        _heal_if_stale(src, dst)
    # Fast path — trust the FTS5 index only while its stamped generation matches the ledger's current one.
    if (not force_scan) and fts5_available() and os.path.exists(dst):
        match = " ".join('"' + token + '"' for token in tokens)
        try:
            conn = sqlite3.connect(dst)
            try:
                if _index_is_current(conn, src):
                    candidates = _fast_candidates(conn, match, tags=tags, session=session, limit=limit)
                    return QueryResult(records=_rank_slice_score(candidates, limit), degraded=False)
            finally:
                conn.close()
        except sqlite3.Error:
            # Broken/corrupt index: fall through to the always-correct scan (availability law). A malformed MATCH
            # cannot land here (the tokens are always valid), so this only catches a broken index, never a bad query.
            pass
    # Slow path — rank the FULL matched set (no early limit break, so the SET matches the fast path), scoring it
    # with the SAME bm25 the fast path reads out of FTS5. One streaming pass collects both the corpus statistics
    # bm25 needs (document count, average length, per-term document frequency) and the matched records; the
    # scores follow once the pass is complete, because a document's rank depends on the whole corpus.
    want = list(tokens)                  # already unique — `_query_terms` is the one place that collapses repeats
    wanted = set(want)
    n_docs = total_len = 0
    doc_freq = dict.fromkeys(want, 0)
    matched = []
    for ordinal, record in enumerate(forget.live_records(path=src)):
        body_tokens = _tokenize(_record_text(record))
        n_docs += 1
        total_len += len(body_tokens)
        present = set(body_tokens)
        for term in wanted & present:
            doc_freq[term] += 1
        if not (wanted <= present) or not _passes_filters(record, tags, session):
            continue
        # Per-term counts as a flat TUPLE in `want` order, not a dict keyed by the term strings. A matched
        # record has to be held until the pass ends (its score depends on statistics only the end of the pass
        # knows), so what is held per match is the whole cost here — and a dict per match, on a query whose
        # word is common enough to match most of the store, is a measurable share of it.
        counts = dict.fromkeys(want, 0)
        for token in body_tokens:
            if token in wanted:
                counts[token] += 1
        matched.append((ordinal, len(body_tokens), tuple(counts[term] for term in want), record))
    candidates = []
    if matched:
        avgdl = (total_len / n_docs) if n_docs and total_len else 1.0
        idfs = [_bm25_idf(n_docs, doc_freq[term]) for term in want]
        for ordinal, doc_len, tfs, record in matched:
            norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * doc_len / avgdl)
            rel = sum(idf * (tf * (_BM25_K1 + 1)) / (tf + norm) for idf, tf in zip(idfs, tfs))
            candidates.append((rel, ordinal, record))
    return QueryResult(records=_rank_slice_score(candidates, limit), degraded=True)


def search(
    query_text: str,
    *,
    tags: "list | None" = None,
    session: "str | None" = None,
    limit: "int | None" = None,
    force_scan: bool = False,
    ledger_file: "str | None" = None,
    index_file: "str | None" = None,
) -> QueryResult:
    """Ranked, filtered recall — the `search` interface (search.json). Every query word must appear (implicit AND),
    and the matches come back BEST-FIRST by lexical relevance — the SAME bm25 on both paths, read from FTS5 on
    the fast one and recomputed in plain Python on the slow backup — with equally-relevant matches ordered
    newest first. Optional `tags` (any-match) narrows. Each result is a shallow copy carrying
    `records.SCORE_KEY` (the lexical relevance). `degraded` is True when answered by the slow backup scan.

    `session` narrows to ONE conversation — the second move of a recall, once the first has named which
    conversation to look in (`_passes_filters` carries why this is a filter rather than a second operation).
    A blank string is treated as no filter rather than as a session nothing can match, so a caller passing an
    empty value through gets the whole store instead of a silent nothing.

    SIDE-EFFECT-FREE, and now free of the ledger entirely on the fast path: it reads no records the index does
    not already hold, so what a search costs tracks how much it matched rather than how much is stored."""
    src = ledger.ledger_path() if ledger_file is None else ledger_file
    dst = index_path() if index_file is None else index_file
    tags_set = set(tags) if tags is not None else None
    session_key = session if isinstance(session, str) and session else None
    tokens = _query_terms(query_text)
    if not tokens:
        return QueryResult(records=[], degraded=False)
    return _ranked(tokens, src, dst, tags=tags_set, session=session_key,
                   limit=limit, force_scan=force_scan)


# --- Operator demonstration -------------------------------------------------------------------------------
# An operator-runnable walkthrough on a throwaway PRACTICE filing cabinet (a temp folder), never the real
# store. It exercises the REAL rebuild/query above. Run it and vary the questions/memories near the top:
#     uv run --directory .engine --frozen -- python tools/memory/index.py demo
# Plain words only — three nouns throughout: "the filing cabinet" (the one real copy), "the fast lookup",
# and "the slow backup lookup".

# Vary these and re-run — every memory that mentions your question's words should still come back both ways.
_DEMO_MEMORIES = [
    {"body": "We chose the snake_case_config naming for all the project settings."},
    {"body": "The cafe meeting on Tuesday decided to ship the export feature on Friday."},
    {"body": "We rejected the cron approach because it could not see the user's calendar."},
    {"body": "Preference: keep the onboarding copy short and friendly, no jargon."},
]
_DEMO_QUESTIONS = ["config", "cafe", "calendar"]


def _demo_same_both_ways(cabinet: str, index_file: str) -> bool:
    print("=" * 80)
    print("PART 1 — does the slow backup lookup find the SAME memories as the fast lookup?")
    print("=" * 80)
    for memory in _DEMO_MEMORIES:
        ledger.append(memory, path=cabinet)
    rebuild(ledger_file=cabinet, index_file=index_file)
    all_same = True
    for question in _DEMO_QUESTIONS:
        fast = query(question, ledger_file=cabinet, index_file=index_file)
        slow = query(question, force_scan=True, ledger_file=cabinet, index_file=index_file)
        fast_bodies = sorted(r["body"] for r in fast.records)
        slow_bodies = sorted(r["body"] for r in slow.records)
        same = fast_bodies == slow_bodies and len(fast_bodies) >= 1
        all_same = all_same and same
        print(f'\n  question: "{question}"')
        for body in fast_bodies:
            print(f"    found: {body}")
        print(f"    fast lookup and slow backup agree: {'yes' if same else 'NO'}")
    print('\n  Note: "config" only matches "snake_case_config" because the backup splits words the same careful')
    print("  way the fast lookup does — a naive backup would miss it. That is the faithfulness this proves.")
    print(f"  => {'Both ways found the same memories for every question.' if all_same else '!!! a mismatch'}")
    return all_same


def _demo_still_answered_when_fast_off(cabinet: str, index_file: str) -> bool:
    print("\n" + "=" * 80)
    print("PART 2 — turn the fast lookup OFF (as if this computer lacked the fast-search feature)")
    print("=" * 80)
    result = query("config", force_scan=True, ledger_file=cabinet, index_file=index_file)
    answered = len(result.records) >= 1
    print(f'\n  question: "config", with the fast lookup turned off')
    for record in result.records:
        print(f"    still found: {record['body']}")
    print("\n  Nothing is broken — the question is still answered. On a large memory this backup is slower than")
    print("  the fast lookup; you will not see that here because this practice cabinet is tiny. In real use, when")
    print("  the fast recall is unavailable the engine tells you — the session-start briefing says so — so a")
    print("  slower answer is never a mystery. A missing fast-search feature is a non-event, never a failure.")
    print(f"  => {'Answered without the fast lookup.' if answered else '!!! not answered'}")
    return answered


def _demo_throwaway_nothing_lost(cabinet: str, index_file: str) -> bool:
    print("\n" + "=" * 80)
    print("PART 3 — DELETE the entire fast lookup. Is anything lost?")
    print("=" * 80)
    ledger.append({"body": "DO NOT LOSE THIS — the decision we must never forget."}, path=cabinet)
    rebuild(ledger_file=cabinet, index_file=index_file)
    before = query("forget", ledger_file=cabinet, index_file=index_file)
    os.remove(index_file)  # blow away the whole fast lookup
    after_gone = query("forget", ledger_file=cabinet, index_file=index_file)
    rebuild(ledger_file=cabinet, index_file=index_file)  # rebuilt from the one real copy
    after_rebuilt = query("forget", ledger_file=cabinet, index_file=index_file)

    def bodies(result):
        return [r["body"] for r in result.records]

    survived = bodies(before) == bodies(after_gone) == bodies(after_rebuilt) and len(bodies(before)) == 1
    print(f"\n  before deleting the fast lookup: {bodies(before)}")
    print(f"  after deleting the fast lookup:  {bodies(after_gone)}   (answered by the slow backup)")
    print(f"  after rebuilding the fast lookup: {bodies(after_rebuilt)}")
    print("\n  The fast lookup is disposable: deleting it lost nothing, and the engine rebuilt it from the")
    print("  filing cabinet — the one real copy. Backing up memory only ever means copying the cabinet.")
    print(f"  => {'Nothing was lost.' if survived else '!!! something was lost'}")
    return survived


def _demo_one_bad_entry(cabinet: str, index_file: str) -> bool:
    print("\n" + "=" * 80)
    print("PART 4 — one corrupted entry in the cabinet. Do the memories around it survive?")
    print("=" * 80)
    ledger.append({"body": "the lesson we keep about retries"}, path=cabinet)
    with open(cabinet, "a", encoding="utf-8") as fh:  # a corrupted, unreadable line
        fh.write("@@@ corrupted junk that is not a real memory @@@\n")
    ledger.append({"body": "the lesson we keep about timeouts"}, path=cabinet)
    rebuild(ledger_file=cabinet, index_file=index_file)
    fast = [r["body"] for r in query("lesson", ledger_file=cabinet, index_file=index_file).records]
    slow = [r["body"] for r in query("lesson", force_scan=True, ledger_file=cabinet, index_file=index_file).records]
    ok = "the lesson we keep about retries" in fast and "the lesson we keep about timeouts" in fast and fast == slow
    print(f"\n  memories found around the corrupted entry: {sorted(fast)}")
    print("  The corrupted entry was skipped by both lookups; the good memories on either side came back.")
    print(f"  => {'Both good memories survived.' if ok else '!!! a good memory was lost'}")
    return ok


def _demo_conversation_is_findable(cabinet: str, index_file: str) -> bool:
    print("\n" + "=" * 80)
    print("PART 5 — something said once in conversation and never summarised. Can it be FOUND?")
    print("=" * 80)
    said = {"kind": records.AMBIENT_CAPTURE_KIND, records.RECORD_ID_KEY: "turn-1", "session_id": "s-demo",
            "seq": 7, "speaker": "user", "tags": ["transcript", "stop"], "ts": int(time.time()),
            "text": "the quokka connector keeps dropping friday deploys"}
    scaffolding = {"kind": records.AMBIENT_CAPTURE_KIND, records.RECORD_ID_KEY: "turn-2", "session_id": "s-demo",
                   "seq": 8, "speaker": "user", "tags": ["transcript", "stop", records.INJECTED_TAG],
                   "ts": int(time.time()),
                   "text": "<task-notification> the quokka background job finished </task-notification>"}
    ledger.append(said, path=cabinet)
    ledger.append(scaffolding, path=cabinet)
    rebuild(ledger_file=cabinet, index_file=index_file)
    fast = [r.get("text") for r in query("quokka", ledger_file=cabinet, index_file=index_file).records]
    slow = [r.get("text") for r in query("quokka", force_scan=True,
                                         ledger_file=cabinet, index_file=index_file).records]
    found = said["text"] in fast
    scaffolding_kept_out = all("task-notification" not in (t or "") for t in fast)
    agree = fast == slow
    print("\n  you asked about: \"quokka\"")
    for text in fast:
        print("    found:", text)
    print("\n  The sentence was said once and no summary of it was ever written — the only record of it is the")
    print("  conversation itself, and it came back anyway. That is the whole point of this change.")
    print("  What did NOT come back is the machine's own notification, which also contained the word: text the")
    print("  engine inserted is never handed back as something you said.")
    ok = found and scaffolding_kept_out and agree
    print(f"  => {'Found it, and kept the scaffolding out.' if ok else '!!! ' + ('the conversation was not findable' if not found else 'machine scaffolding leaked into recall' if not scaffolding_kept_out else 'the two lookups disagreed')}")
    return ok


def _demo() -> int:
    import shutil

    if not fts5_available():
        print("This computer's search feature is unavailable, so this demo would only show the slow backup.")
        print("That is itself fine (recall still works), but the side-by-side comparison needs the fast lookup.")
        return 0
    tmp = tempfile.mkdtemp(prefix="engine-memory-demo-")
    try:
        cabinet = os.path.join(tmp, "ledger.ndjson")
        index_file = os.path.join(tmp, "index.sqlite3")
        results = [
            _demo_same_both_ways(cabinet, index_file),
            _demo_still_answered_when_fast_off(cabinet, index_file),
            _demo_throwaway_nothing_lost(cabinet, index_file),
            _demo_one_bad_entry(cabinet, index_file),
            _demo_conversation_is_findable(cabinet, index_file),
        ]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "-" * 80)
    print("Reminder: what you just saw is the engine looking things up in a PRACTICE cabinet we filled for this")
    print("demo, then threw away — your own saved memory was never opened. What this proves is narrow and")
    print("durable: once things ARE filed, you can always look them up — fast when the search feature is")
    print("present, slower but never broken when it is not, and nothing is lost if the fast lookup is thrown")
    print("away. Whether your own cabinet has anything in it depends on how long the engine has been running")
    print("here; ask me and I'll tell you what is actually stored.")
    print("\nVary it yourself: edit the questions and memories near the top of this file and run it again.")
    return 0 if all(results) else 1


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    print("usage: index.py demo")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
