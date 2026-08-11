"""records.py — the shared record vocabulary for the memory ledger.

The `kind` strings and provenance keys that more than one memory tool must agree on, in ONE place so they
never drift and no import cycle can form. `consolidate` writes the episodic + marker records; `index` keeps
provenance keys out of the search body; `forget` derives logical retirement from the marker↔batch linkage and
appends the `reinforcement` access marker + scores demotion from it; `compact` folds those
markers into the carried current-state fields below and `score` reads them back; `rollup` writes the
gist + supersession markers and `forget` derives the raws' retirement from them; `index.search` ranks
recall best-first and attaches the per-result `SCORE_KEY`. Because all of them need these names and `consolidate`
already imports `index`, defining them here — a leaf that imports nothing from the `memory` package — lets `index`,
`forget`, `score`, `compact`, and `rollup` import them without
`consolidate`→`index`→`forget`→`consolidate` becoming a cycle.

stdlib-only; imports nothing from `memory`.
"""

import re
import uuid

# Record kinds (the `kind` field). These are the shared kinds the reflection and forgetting layers
# both reference.
AMBIENT_CAPTURE_KIND = "turn-delta"  # the role-less, Stop-appended verbatim capture record. Promoted here (it was
                                     # capture's own) so `forget`'s recall-membership filter can name it WITHOUT
                                     # importing `capture` at module load (cycle discipline); `capture.RECORD_KIND`
                                     # now aliases this so the string never drifts.
EPISODIC_KIND = "episodic"          # an AI-written episodic summary record
MARKER_KIND = "consolidated"        # the in-ledger "this session has been tidied" marker (survives backup)

# Recall membership. Recall surfaces the recorded conversation AND the curated layer over it — the transcript
# is the canonical record and the summaries above it are the disposable layer (eADR-0038). The whole
# `turn-delta` kind was once excluded, because verbatim turns vastly outnumber paraphrased summaries and matched
# more exactly, crowding them out of every recall; the answer now is that the summaries are the layer being
# retired, not the conversation. `forget._is_excluded_capture` holds what remains of the exclusion: harness-
# injected pseudo-turns only, keyed on `is_injected_record` below. It is re-derived on every recall read / index
# rebuild (no per-record marker, no carried bit — it survives compaction for free), and it is a targeted
# exclusion rather than an allowlist: a record carrying a `role` + `text` but no explicit kind is an
# episodic-shaped recall record and stays surfaced.

# Tags.
DEFAULT_EPISODIC_TAG = "episodic"
MARKER_TAG = "consolidated"

# Harness-injected pseudo-turns (issue StarshipSuperjam/engine-template#274, folding in StarshipSuperjam/engine-template#333). Claude Code injects non-conversational blocks as
# `user`-role transcript turns — a background-agent completion notice (`<task-notification>`) and the `/compact`
# continuation summary (`This session is being continued from a previous conversation…`). They reach the ledger
# as ambient `turn-delta` records, and this tag is now the WHOLE of what keeps them out of recall — the rest of
# the conversation is recall content, so nothing else is holding them back (see the membership block above).
# The consolidation sweep also reads the raw ledger, so without this filter the in-context AI would consolidate
# them as if the operator had said them. The fix is NOT a pre-ledger drop — StarshipSuperjam/engine-template#333 chose to keep them RESIDENT
# + recoverable (the durability
# law: an abandoned session loses the reflection, not the content). Instead capture TAGS them (`INJECTED_TAG`, on
# every chunk of an injected message, recognised before chunking so a multi-chunk continuation summary is fully
# tagged) and `consolidate` SKIPS a tagged/injected record as fuel. The prefix set is deliberately the two
# DISTINCTIVE, ground-truthed standalone sentinels: each is the WHOLE injected message (never fused with a real
# prompt, confirmed against the live ledger), so a start-anchored match cannot eat conversation. `<system-reminder>`
# is deliberately EXCLUDED from this set — it fuses with a human prompt in the same turn, so dropping the whole
# record would lose real content. It is handled instead by `mark_harness_spans` below, which removes just the
# block wherever the text is indexed or shown.
INJECTED_TAG = "injected"               # the tag capture stamps on every chunk of a harness-injected pseudo-turn
_INJECTED_PSEUDO_TURN_PREFIXES = (
    "<task-notification>",                                              # background-agent completion notice
    "This session is being continued from a previous conversation",    # the /compact continuation summary
)


def is_injected_pseudo_turn_text(text) -> bool:
    """True iff `text` BEGINS with a known harness-injected pseudo-turn marker. Start-anchored (the whole injected
    message IS the block), so a genuine turn that merely mentions a marker mid-sentence is never matched. Used at
    CAPTURE, on the whole message before chunking, so every chunk of an injected message is tagged uniformly."""
    return isinstance(text, str) and text.strip().startswith(_INJECTED_PSEUDO_TURN_PREFIXES)


# A harness-inserted block that arrives FUSED into the same turn as a real prompt. Unlike the standalone
# pseudo-turns above, it cannot be excluded record-wise without losing the operator's own words alongside it —
# which is why it was deliberately left out of the injected set. That was harmless while captured conversation
# was unsearchable. It is not harmless now: such a turn is keyword-findable and carries `speaker: "user"`, so a
# reader is told the operator said something the engine inserted. Measured on the maintainer's store when this
# was found, 2 user turns carry a complete block — and in one, 420 of 456 characters were the block.
#
# The fix is presentational, never destructive: the stored bytes are untouched (the ledger is the canonical
# record and this is not erasure), and the span is replaced by a visible marker wherever the text is INDEXED or
# SHOWN. So the harness half is not searchable and is never quoted as speech, while the operator's own words in
# the same turn survive intact.
_HARNESS_SPAN = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
HARNESS_SPAN_MARKER = "[engine-inserted note removed]"


def mark_harness_spans(text):
    """`text` with every fused harness block replaced by `HARNESS_SPAN_MARKER`. Returns the input unchanged for
    a non-string or when no block is present. Idempotent — the marker matches no pattern — and it never raises:
    a reader that fails here would be worse than one that under-marks.

    HONEST BOUND: it matches a COMPLETE block within ONE record. An opening tag with no close in the same
    record is left alone, which is right for the common case (an assistant quoting the tag while discussing it —
    real content) and is a residual for the rare one (a block split across the chunk boundary, whose halves land
    in different records). Verified on the maintainer's store after this landed: no complete block is searchable
    any more, and every remaining occurrence is an unclosed tag."""
    if not isinstance(text, str) or "<system-reminder>" not in text:
        return text
    try:
        return _HARNESS_SPAN.sub(HARNESS_SPAN_MARKER, text)
    except Exception:  # noqa: BLE001 — presentation must never break a read
        return text


def is_injected_record(record) -> bool:
    """True iff `record` is a harness-injected pseudo-turn the consolidation sweep should skip as fuel: tagged
    `INJECTED_TAG` at capture (the durable path — covers every chunk), OR — back-compat for records captured
    before tagging existed — its text begins with an injected marker. The record stays physically resident and
    recoverable in the ledger; what this withholds is only ever surfacing, never storage. THREE readers share
    this predicate, and it carries more weight than it used to: the consolidation sweep skips such a record as
    fuel, the transcript-window reader leaves it out of a window, and recall itself now excludes it — since the
    rest of the conversation became recall content, this predicate is the whole of what recall withholds, so a
    gap here would surface machine scaffolding as something the operator said."""
    if not isinstance(record, dict):
        return False
    tags = record.get("tags")
    if isinstance(tags, list) and INJECTED_TAG in tags:
        return True
    return is_injected_pseudo_turn_text(record.get("text"))

# Provenance keys — envelope fields that are NOT human content, so the derived index keeps them OUT of the
# search body (index._NON_BODY_KEYS).
BATCH_KEY = "batch"                 # one id per consolidation pass, stamped on every episodic of that pass AND
                                    # on the pass's marker. It lets `forget` derive, purely from the ledger,
                                    # which episodics a *completed* pass closed — and which are orphans from a
                                    # crashed pass (their batch carries no marker), to logically retire.

THROUGH_SEQ_KEY = "through_seq"     # on the `consolidated` marker (StarshipSuperjam/engine-template#446): the per-session HIGH-WATER-MARK — the
                                    # `seq` of the last genuine turn the pass EXAMINED (reusing capture's own
                                    # per-message seq, never a parallel counter). It turns the marker from a
                                    # binary done-flag into "swept through here", so a session tidied mid-run is
                                    # re-swept for only its later half. An INT (so, like seq/ts, it stays out of
                                    # the string-leaf search body by type — no _NON_BODY_KEYS entry needed).
                                    # OPTIONAL on read: a LEGACY marker written before StarshipSuperjam/engine-template#446 lacks it and is
                                    # projected into seq-space from its `ts`; always present on a marker written
                                    # now. Effective per-session watermark = the MAX across the session's markers.

# The stable, content-free record id. Minted at capture in each record factory — one per record, on
# every kind (turn-delta, episodic, marker). It is a durable NAME for a record: a uuid hex, so it reveals nothing
# about the gitignored content (content-free) and survives the index rebuild and the future compaction rewrite (it
# rides in the record JSON, not an ephemeral index offset). The derived index keeps it OUT of the search body
# (index._NON_BODY_KEYS): a uuid's hex fragments are real words, exactly the `session_id`/`batch` problem.
RECORD_ID_KEY = "id"

# The reinforcement (access) marker. An append-only ledger record minted each time a record is RECALLED: it
# names, by the reinforced record's stable id, that the record was used. `score` folds these into a
# frecency × role-weight × recency value on a four-step scale (hot → warm → cold → archived). That value RANKS
# recall results and picks roll-up candidates. No step of the scale is itself a membership rule — `live_records`
# reads no tier — but it is not inert either: a completed roll-up supersedes the episodes it folded out of
# recall, so the cold end of this scale reaches membership through roll-up (see `score.tier`).
# A reinforcement marker is pure derivation fuel — non-content provenance — so it carries no `text`/
# `session_id`; `index` keeps its `target` (a uuid hex, the `id`/`batch` problem) OUT of the search body
# (index._NON_BODY_KEYS), and `forget.live_records` drops the marker itself from recall. The live caller that
# appends it on recall is the search server (`mcp_server`); this module ships the kind, and `forget` the appender.
REINFORCEMENT_KIND = "reinforcement"   # the `kind` field of an access marker
TARGET_KEY = "target"                  # the reinforced record's RECORD_ID_KEY value (whom the access points at)
REINFORCEMENT_TAG = "reinforcement"    # the marker's tag (kept out of the search body like every tag)

# The carried current-state fields ledger compaction folds onto a recall record before it prunes that
# record's reinforcement markers. They make a compacted record's demotion score durable WITHOUT keeping the
# folded-away markers: `score` reproduces the pre-compaction score from `FRECENCY_SNAPSHOT_KEY` (the frecency
# value at compaction time) decayed forward from `SNAPSHOT_TS_KEY`, with `LAST_ACCESS_TS_KEY` flooring recency.
# This is legal precisely because frecency is a RECURRENCE on the carried snapshot (score.frecency). `TIER_KEY`
# is NO LONGER WRITTEN: compaction used to stamp the snapshot-time tier as a legibility field, back when a tier
# decided whether a record still surfaced. None does now, so the name survives only because a record an OLDER
# engine compacted still carries one — and `index` must keep it (a string: "hot"/"cold"/"archived") OUT of the
# search body (index._NON_BODY_KEYS), else those words would match every such record. The numeric snapshot
# fields are excluded from the body by type already.
FRECENCY_SNAPSHOT_KEY = "frecency_snapshot"   # float: score.frecency value at compaction time t0
SNAPSHOT_TS_KEY = "snapshot_ts"               # int: t0, the compaction time the snapshot was stamped at
LAST_ACCESS_TS_KEY = "last_access_ts"         # int: max(birth, *accesses) at t0, the recency floor
TIER_KEY = "tier"                             # str: written by older engines only; kept out of the search body

# The gist roll-up vocabulary. Active forgetting's first move is a SECOND-order
# consolidation: an AI-judged maintenance pass rolls up OLD, low-frecency EPISODIC summaries of one session into a
# compact GIST and LOGICALLY RETIRES the raw episodes (excluded from recall, still resident + fully recoverable —
# Layer-1 never erases; physical erasure is Layer-2, audit-gated). `rollup` writes, in strict order under the
# single-writer lock, the gist → a per-raw `superseded` marker → the closing `rolled-up` marker; `forget` derives
# the raws' retirement from a CLOSED-batch supersession; `compact` (extended) folds a closed-batch
# supersession into the carried `SUPERSEDED_BY_KEY` field below and prunes the marker. The gist↔raw link is thus
# carried in the ledger (the marker, then the folded field) and survives the rewrite.
GIST_KIND = "gist"                  # an AI-written gist consolidating several old episodes of one session
GIST_TAG = "gist"                   # surfaces alongside DEFAULT_EPISODIC_TAG so a gist rides episodic recall
ROLLUP_KIND = "rolled-up"           # the closing marker of a roll-up pass — the ONLY kind that CLOSES its batch,
                                    # DISTINCT from MARKER_KIND so a roll-up never spuriously marks a session
                                    # consolidated (the two closure namespaces never mix — forget._closed_batches
                                    # reads MARKER_KIND, forget._closed_rollup_batches reads ROLLUP_KIND)
SUPERSEDED_KIND = "superseded"      # a per-raw marker: this raw episode's content now lives in a gist. It points at
                                    # the raw by TARGET_KEY (reused — already non-body) and names the gist by
                                    # SUPERSEDED_BY_KEY, and carries the pass's BATCH_KEY. INERT until its batch is
                                    # closed (a `rolled-up` marker landed): only then does it hide its raw, so a
                                    # crash before the closing marker never hides a raw whose gist's pass didn't finish.
SOURCE_IDS_KEY = "source_ids"       # on the gist: the RECORD_ID_KEY values of the raw episodes it consolidates — the
                                    # forward half of the gist↔raw link (a list of uuid hex; kept OUT of the search
                                    # body, index._NON_BODY_KEYS, like every uuid-hex field)
# The carried current-state field compaction folds a CLOSED-batch supersession into, before it prunes the marker:
# the raw episode carries the gist id it was superseded by, so `forget.live_records` still retires it after the
# marker is gone. Minted ONLY across a closed gate, so its mere presence proves the gist pass completed — trusted
# unconditionally. A uuid hex, so `index` keeps it OUT of the search body (index._NON_BODY_KEYS).
SUPERSEDED_BY_KEY = "superseded_by"

# The pin vocabulary — durable operator intent that has no better canonical home (eADR-0038). A pin is
# CONTENT, not a marker: it carries `text` and rides ordinary recall, and it is a record-type inside this one
# substrate rather than a second store.
#
# PROVENANCE IS RECORDED BECAUSE IT CANNOT BE VERIFIED. A pin is minted when the operator says "remember
# this", but what lands in the ledger is whatever the calling session passed — and a session's context can
# contain a page it recalled, a file it read, or tool output, any of which may be shaped like an instruction.
# Nothing downstream can tell those apart from something the operator typed. So a pin records the ROUTE it
# arrived by and nothing stronger, and every reader presents it as "saved when you asked me to remember
# this" rather than as a verified quotation. PIN_VIA_ASSISTANT is a model calling the write tool;
# PIN_VIA_CLI is the command line. Neither proves a person: an AI session runs commands here too (the actor
# model), so the field distinguishes paths, never authorities.
#
# WHY A PIN IS SCRUBBED ON THE WAY IN. Capture scrubs secret-shaped text as it stores conversation, but a pin
# does not come through capture — it is written directly by a verb. A credential pasted into a session and
# then pinned would otherwise be stored unscrubbed AND read back into the briefing of every future session,
# which is a worse exposure than the one capture's scrub exists to prevent. `pins.add` runs the same scrubber.
PIN_KIND = "pin"                    # the record kind: durable operator intent, surfaced by ordinary recall
PIN_VIA_KEY = "pinned_via"          # how it arrived — a route, never a claim about who authored it
PIN_VIA_ASSISTANT = "assistant"     # a model called the write tool, transcribing what the operator asked
PIN_VIA_CLI = "cli"                 # typed at the command line
PIN_SOURCE_SESSION_KEY = "source_session"   # the session it was asked for in (a session id; kept non-body)
PIN_TAG = "pin"                     # the pin's tag (kept out of the search body like every tag)

# The withhold vocabulary — the operator's own REVERSIBLE control over what recall may surface.
#
# WHY "WITHHELD" AND NOT "HIDDEN". "Hidden from recall" is already spoken for: it is the phrase the
# erasure consent copy uses for a note queued for PHYSICAL, irreversible removal (`erase`), and
# "set aside" is already the roll-up's summarised-and-unfoldable class (`forget.set_aside`). A third control
# wearing either word would blur the one distinction that must never blur — what comes back and what does
# not. Withholding keeps the record exactly where it is, byte for byte, and stops recall returning it.
#
# TWO TARGET KEYS, NOT ONE FIELD WITH TWO MEANINGS. A record id and a session id are both uuid hex, so a
# single `target` carrying either would be indistinguishable to every reader. A marker names ONE of them:
# TARGET_KEY for a single record, TARGET_SESSION_KEY for a whole session's conversation.
#
# ORDER IS LEDGER POSITION, NEVER `ts`. Capture stamps whole-second timestamps, so a withhold and the
# restore that reverses it can share one `ts`; ordering by time would leave the pair tied and the outcome
# arbitrary. The ledger is append-only, so its own order is the authority — the LAST marker naming a target
# decides, exactly as `_closed_batches` derives closure from position rather than time.
#
# Both markers are pure non-content provenance: no `text`, no `session_id` of their own, so `index` keeps
# their uuid-hex target fields OUT of the search body (index._NON_BODY_KEYS) and `forget._is_bookkeeping`
# drops the markers themselves from recall. Layer-1 to the letter: nothing is deleted, and `recall`'s window
# reader honours them too, so a withheld session is not merely unsearchable but unquoted.
WITHHOLD_KIND = "withheld"           # the marker that takes a record, or a session, out of recall
RESTORE_KIND = "restored"            # the marker that reverses a withhold — the same two target keys
TARGET_SESSION_KEY = "target_session"  # a whole session's conversation (a session id, never a record id)
WITHHOLD_TAG = "withheld"            # the markers' tag (kept out of the search body like every tag)

# The operator-adjudicated-erasure marker (Layer-2 physical erasure). Its OWN evidence class (NOT a
# stretch of `operator-directed`): the one marker that authorises COMPACTION to physically REMOVE a recall record
# from the ledger — the single irreversible act in the memory system, reachable ONLY because the operator merged a
# single-purpose erasure pull request (the consent gate). It names the target by its stable, content-free
# RECORD_ID_KEY (reusing TARGET_KEY — already non-body) and carries MERGE_SHA_KEY, the merge identity that
# authorised it. Pure non-content provenance: no `text`/`session_id`; `index` keeps MERGE_SHA_KEY (and TARGET_KEY)
# OUT of the search body, and `forget.live_records` drops the marker from recall (forget._is_bookkeeping). `compact`
# removes the TARGET but RETAINS the marker itself (the idempotency tombstone, so a re-compaction is a clean no-op).
# `compact.enact_erasure` is the SOLE minter, and its one live caller is the cross-session observer
# (`erasure_observer.enact_from_merged_prs`), which mints a marker only from a MERGED single-purpose erasure pull
# request; a test and the throwaway-cabinet demo mint one directly. No path mints one from an AI's say-so alone.
# The MERGE_SHA presence is a STRUCTURAL fail-safe floor, NOT consent verification — the real merged-not-closed /
# immutable-merge-tree binding is the observer's job; `compact`'s read-side validity check ignores a
# SHA-less marker so a hand-written or bypassed one can never erase.
ERASURE_KIND = "operator-adjudicated-erasure"   # the `kind` of the merge-gated physical-removal marker
MERGE_SHA_KEY = "merge_sha"                      # the merge commit SHA that authorised the erasure (provenance only)
ERASURE_TAG = "operator-adjudicated-erasure"     # the marker's tag (kept out of the search body like every tag)

# The per-result ranking field (the `search` interface). NOT a stored ledger field: `index.search`
# attaches it to a SHALLOW COPY of each returned record, carrying the record's lexical relevance so a caller
# can see the ordering basis. The usage signal (frecency) is the
# internal tiebreak, NOT this exposed number. Because `search` could re-project a scored copy, `index` keeps this
# key OUT of the search body too (index._NON_BODY_KEYS) — belt-and-suspenders, since scored copies are never indexed.
SCORE_KEY = "score"

# Cross-session roll-up cluster sentinel (StarshipSuperjam/engine-template#235). Roll-up's coarse "related" pre-filter was group-by-session; the
# richer signal relates COLD episodes ACROSS sessions by shared topic tag (`tag:<tag>`). Such a gist has no single
# originating session, so it carries the CLUSTER KEY as its `session_id` — a non-empty string, so every store
# invariant that assumes a session_id still holds. The gist's real-session provenance is NOT lost: it lives in
# SOURCE_IDS_KEY, and `recall.resolve_sessions` reads it back to reach the real conversations. A real work session
# id is a uuid hex, so it can never collide with a `<prefix>:` sentinel.
TAG_SESSION_PREFIX = "tag:"      # a gist rolling up a cross-session shared-topic-tag cluster
_CROSS_SESSION_SENTINEL_PREFIXES = (TAG_SESSION_PREFIX,)


def is_cross_session_sentinel(session_id) -> bool:
    """True iff `session_id` is a roll-up CLUSTER key (a gist that folds notes from MORE than one real session),
    not a real work session. Its contributing real sessions are recoverable from the gist's SOURCE_IDS_KEY."""
    return isinstance(session_id, str) and session_id.startswith(_CROSS_SESSION_SENTINEL_PREFIXES)


def new_record_id() -> str:
    """Mint a fresh content-free record id (a uuid4 hex). Distinct per call; reveals nothing about content."""
    return uuid.uuid4().hex
