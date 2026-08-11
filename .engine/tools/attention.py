#!/usr/bin/env python3
"""The attention tool: substrate adapters + CLI over the pure ranking core (attention_rank.py).

The core ranks a candidate-set it is handed. THIS module assembles that set from the substrates that exist
today and degrades over the ones that do not, then exposes the operator's no-Claude-Desktop CLI. The reads:
  - state  (.engine/state/state.json via validate.load_json): the standing-situation pointers become an
    `orientation` candidate; the committed integration-debt count is the OFFLINE STAND-IN for the live
    telemetry register, surfaced as a single `blocking_debt` candidate only when
    the live register could not be read this session (state's count is a derived convenience, never authoritative).
  - knowledge (knowledge_query.neighbors): each neighbour of the focus becomes a `structural_neighbors`
    candidate. The focus is the "work in hand" — given explicitly (the CLI `--focus`) or DERIVED from the
    in-flight work record (`derive_focus`, the orientation default boot uses). A focus may be a single entity
    id or a SET; each member is walked at depth 1 — structural adjacency is neighbour-membership,
    so every neighbour enters with a flat proximity signal; the pinned ranking form
    defines no hop-distance score, so this is faithful to the spec, not a stand-in for one.
  - git/GitHub (the in-flight work-record reader, work_record): open PRs + the working branch become
    `in_flight` candidates, and the files that work touches drive the knowledge focus above (StarshipSuperjam/engine-template#37).
  - telemetry (the live debt register — open engine-labelled Issues): the canonical debt source attention
    ranks. Boot performs the single live read (open_findings) and threads its
    PER-ISSUE rows in as `live_findings`, so EACH open finding is graded on its own severity into its own
    `blocking_debt` candidate — the sub-threshold ones falling out as debt that does not gate the start of
    work — while the card header reads that same read's count (they cannot
    disagree: it is `len()` of these rows) and the SessionStart path makes no second GitHub call; the committed
    state count above is the stand-in when that read fails. telemetry is in `degraded_inputs` ONLY when the live
    read failed (an outage/expired auth) or the offline CLI ran with no reader — never as standing scaffolding.

The adapter NARRATES nothing in the result (boot surfaces degradation loudly, at its slice); the CLI prints
a degrade note to stderr only, for the operator's own demo. `as_of` is the recorded reference time: the live
`rank` takes it from the cursor's integration-debt as-of, falling back to the wall clock — and ONLY the
adapter ever reads the clock, marking such a run `as_of_is_wallclock` so a consumer never mistakes it for a
refreshed-debt timestamp. The pure core never reads the clock.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import moment            # noqa: E402  (the trailing-Z time seam; pure stdlib leaf, imports nothing back)
import attention_rank    # noqa: E402
from attention_rank import rank, SUBSTRATES, CATEGORIES, PRECEDENCE_KEYS, TRIM_KEYS  # noqa: E402

try:
    import knowledge_query   # noqa: E402
except Exception:            # pragma: no cover - knowledge import should not fail, but assembly degrades if it does
    knowledge_query = None

try:
    import work_record       # noqa: E402  (the in-flight git/GitHub work-record reader, a pure leaf -> no cycle)
except Exception:            # pragma: no cover - a stdlib-only leaf should import, but assembly degrades if not
    work_record = None

POLICY_PATH = os.path.join(validate.ENGINE_DIR, "policies", "attention.md")
STATE_PATH = os.path.join(validate.ENGINE_DIR, "state", "state.json")

# How many distinct entities the work-in-hand focus may span — the build-spec-leaf cap decided
# with the maintainer (a SET of all touched components, capped). It bounds the FOCUS, not the neighbour count.
FOCUS_CAP = 5

# How many neighbours of each (focus member, relationship) the orientation SUMMARY samples for display. The
# rest are NOT dropped silently — `neighborhood_of` keeps the FULL count per relationship so the render can
# disclose the truncation honestly ("core provides 147, showing 4"), never an arbitrary capped few passed off
# as the whole (honest-truncation; the shown few are a sample, not relevance-ranked).
NEIGHBORHOOD_SAMPLE_CAP = 4


# ---- policy + substrate reads ---------------------------------------------------------------

def load_policy_values(policy_path: str = POLICY_PATH, override: dict | None = None) -> dict:
    """The attention policy's machine-read tuning values (the flat snake_case->number map), read straight
    from its YAML frontmatter by the validation foundation's reader — never parsed out of the prose body.

    When an operator policy-override is supplied, this returns the EFFECTIVE values: the shipped default with
    the override merged per-key at read time, via the core merge `validate.effective_policy_values`.
    ATTENTION owns which of its keys are structural — the partition precedence + trim order, never
    override-eligible, so "blocking-debt-first holds by construction" — and CORE owns the merge. The override
    is operator config supplied as DATA; this module never reads an override FILE: its path/format is
    owned by the policy-tuning authoring layer, which loads the file and passes it here, and wires the
    live stale-key rule that consumes the merge's findings. With no override (the live path today), the
    shipped default is returned unchanged — the new `override` is a trailing keyword arg, so the existing
    positional callers are bit-for-bit unaffected."""
    default = validate.frontmatter(policy_path).get("values", {})
    if override is None:
        return default
    effective, _findings = validate.effective_policy_values(
        default, override, structural_keys=set(PRECEDENCE_KEYS) | set(TRIM_KEYS), tier="soft",
        message="An operator policy-override tunes values only, never the structural ordering.")
    return effective


def derive_focus(*, run=None, gh=None, cap: int = FOCUS_CAP, with_total: bool = False, source=None):
    """The knowledge focus for the orientation-time focused read: the distinct graph entities that OWN the
    files the in-flight work touches (StarshipSuperjam/engine-template#37 — which entities neighbor the work in hand).

    Maps each changed path (`work_record.changed_paths`) to its entity via an EXACT source-path lookup over a
    one-shot `knowledge_query.find()` index — never a SQLite GLOB match, so a path with shell metacharacters
    (`*?[]`) can't silently mis-resolve. Skips a path that owns no entity (a non-surface file: the root
    README, CLAUDE.md, the derived graph.json, ...) and EXCLUDES a changed file's own `test_`/`demo_` entity
    (the focus is the thing under test, not its test). Distinct, stable order, capped at `cap`.

    `with_total` (opt-in; default off, so existing list-returning callers are bit-for-bit unaffected): when
    True, returns `(focus, total)` where `total` is the count of ALL distinct mapped entities BEFORE the cap —
    so the render can DISCLOSE focus truncation honestly ("touching 5 of 7 you've changed", StarshipSuperjam/engine-template#165) rather than
    pass the capped few off as the whole. To know the true total the scan no longer stops early at the cap; the
    changed-path set is already bounded (work_record caps it), so the full scan stays cheap.

    Fail-open: returns the empty result on no in-flight work, or any read failure (work_record/knowledge absent,
    or `find` raising KnowledgeUnavailable) — boot still ranks the rest. `gh` is accepted for the deferred
    PR-files layer but unused today; the floor is local git. `run` defaults LAZILY to work_record's git runner
    (never as a default-arg expression, which would crash at import if the guarded work_record import degraded).

    `source` (opt-in; default off -> the `knowledge_query` module, so the CLI/tests are bit-for-bit unchanged):
    boot passes its rung-1 boot-slice read-shim (StarshipSuperjam/engine-template#37) here, which exposes the same `find()` — so orientation
    reads the cached path->entity map instead of the SQLite index. A slice present means knowledge is available
    even if the `knowledge_query` import itself degraded (`src` is then the slice)."""
    empty = ([], 0) if with_total else []
    src = source or knowledge_query
    if work_record is None or src is None:
        return empty
    runner = run or work_record._run_git
    try:
        paths = work_record.changed_paths(run=runner)
        if not paths:
            return empty
        # One find() -> an EXACT path->id index (no SQLite GLOB, so a metacharacter path can't mis-resolve).
        # The catalog guarantees one entity per source_path (a file owns exactly one surface entity), so the
        # dict build has no real clobber; if that invariant ever broke, find()'s id-order makes it deterministic.
        by_path = {e["source_path"]: e["id"] for e in src.find()
                   if e.get("source_path") and e.get("id")}
        mapped: list = []
        for p in paths:
            eid = by_path.get(p)
            if eid is None:
                continue                                       # a non-surface file owns no entity -> skip
            if eid.split(":", 1)[-1].startswith(("test_", "demo_")):
                continue                                       # focus the thing under test, not its test/demo
            if eid not in mapped:                              # collect ALL distinct (no early break) so `total`
                mapped.append(eid)                             # is the true count behind the cap
        focus = mapped[:cap]
        return (focus, len(mapped)) if with_total else focus
    except Exception:
        return empty  # the work-in-hand focus could not be derived -> degrade (no focused read this session)


def assemble_candidates(policy_values: dict, *, state_path: str = STATE_PATH,
                        focus: "str | list[str] | None" = None,
                        edge_filter=None, depth: int = 1, gh=None, source=None,
                        live_findings: list | None = None, memory_recall: list | None = None,
                        shipped: list | None = None):
    """Assemble the candidate-set from the substrates present today, reporting which were available and the
    cursor's as-of marker. Returns (candidates, available_inputs:set, cursor_as_of:str|None). Narrates nothing.

    `gh` is the GitHub reader for the in-flight work-record read (None -> the local-git floor only; boot
    passes a real reader, the CLI passes None). Like state/knowledge, the work record is read HERE — the only
    boot-loaded input is the operator override (config, not a substrate).

    `live_findings` is the live telemetry debt register's PER-ISSUE rows (open engine-labelled Issues, each
    `{number, source_id, severity}`) — the canonical debt source attention ranks. Boot performs the one live read
    (open_findings) and threads the rows here, so the ranking grades each finding on its own severity while the
    card header reads that same read's count (`len(rows)`) — one read, so they cannot disagree — and the
    SessionStart path makes no second GitHub call. A list (empty included) means the register WAS read: telemetry
    is marked available and each open finding is its own graded blocking-debt candidate. None means it was not
    read this session (the offline CLI, or a failed read): telemetry stays in degraded_inputs and the committed
    state count stands in (its derived-convenience role).

    `source` (opt-in; default off -> the `knowledge_query` module, so the CLI's `rank` is bit-for-bit
    unchanged): boot passes its rung-1 boot-slice read-shim (StarshipSuperjam/engine-template#37) so the structural-neighbours walk reads the
    cache, not the SQLite index — making boot's "zero index consults at orientation" real (this discarded
    partition would otherwise still walk the index). The shim exposes the same `neighbors()` + edge vocabulary."""
    candidates: list = []
    available: set = set()
    cursor_as_of = None

    try:
        state = validate.load_json(state_path)
        available.add("state")
        situation = state.get("standing_situation") or {}
        if situation.get("milestone") or situation.get("phase"):
            candidates.append({"id": "state:standing-situation", "category": "orientation",
                               "recency": None, "source": "state"})
        debt = state.get("integration_debt") or {}
        cursor_as_of = debt.get("as_of")
        if live_findings is None and (debt.get("open_count") or 0) > 0:
            # OFFLINE/DEGRADED path only (the live register was not read this session): state's committed
            # count is the stand-in, surfaced AS blocking by setting severity to the bar. telemetry stays in
            # degraded_inputs (it is NOT added to `available` below), so boot raises the loud "couldn't reach"
            # notice; the live path just below supersedes this whenever the register WAS read.
            #
            # This LOOKS like the pinned-at-the-bar shape the live path below exists to remove, and it is
            # deliberately not the same thing — worth spelling out, because the two are one line apart.
            # There, the engine HAS read a finding and telemetry has not graded it: severity is a fact that
            # is absent, so inventing one is inventing. HERE the engine could not look at all — this
            # candidate is not a finding, it is "there was debt when we last saw, and I cannot check". The
            # bar-valued severity is not a claim about how bad anything is; it is how this file says ALWAYS
            # SURFACE, and it must stay true however the bar is tuned (degrade loud). So the
            # blind session is louder than the sighted one on purpose: that is the right way round.
            candidates.append({"id": "state:integration-debt", "category": "blocking_debt",
                               "severity": policy_values.get("debt_blocking_threshold", 0),
                               "recency": cursor_as_of, "source": "state"})
    except Exception:
        pass  # state absent or malformed -> degrade over it (it stays out of available_inputs)

    # The telemetry debt register (the live view over open engine-labelled Issues) is the canonical debt source
    # attention ranks (it reads; it never owns); state's committed count above is only its
    # offline stand-in. `live_findings` is that register's PER-ISSUE rows, read ONCE by boot (open_findings) and
    # threaded in — so the ranking and the card header agree by construction (the header's count is len() of
    # these very rows) and the hot path makes no second GitHub call. A list (empty included) means the read
    # SUCCEEDED: telemetry is available (no degraded notice) and EACH open finding becomes its OWN blocking-debt
    # candidate, graded on its own severity.
    #
    # Per-issue grading is what makes the policy's dials real (both were structurally inert against one pinned
    # aggregate):
    #   - `debt_blocking_threshold` can now DISCRIMINATE. The aggregate's severity was pinned EQUAL to the
    #     threshold, so the bar was forever comparing itself to itself. Now a benign finding grades below the bar
    #     -> assign_partition returns None -> it is debt that can WAIT rather than gate the start of work,
    #     still counted in the card's open-problems line, just not surfaced among the five.
    #   - `flex_high_debt_count` can now COUNT. One aggregate capped the blocking count at 1 forever, so a
    #     busy-session flex could never fire whatever the dial said.
    # Telemetry GRADES the class (`severity_rank` — it owns the class); whether a grade BLOCKS stays
    # attention's own debt-blocking rule (attention does not attribute blocking-membership to telemetry), applied
    # downstream in assign_partition against this policy's threshold. `as_of` stays the committed cursor's marker
    # (the only deterministic time source; the live read carries no timestamp). None -> not read -> telemetry
    # stays degraded and the committed stand-in above carried the count.
    #
    # An UNGRADED finding (severity_rank -> None) carries its severity through as ABSENT, never as a stand-in
    # number. Telemetry owns the class and simply has not graded that Issue, and assign_partition is explicit
    # that attention does not invent what its source did not set — absent severity is its rule's own case, and
    # it makes such a finding a deferral: still mentioned in the card's open-problems count, just not gating
    # the start of work ("rather than just being mentioned", the policy's own words for the bar).
    if live_findings is not None:
        available.add("telemetry")
        import telemetry as _telemetry  # lazy: keeps telemetry + its GitHub client off attention's import path
        threshold = policy_values.get("debt_blocking_threshold", 0)
        for row in live_findings:
            number = row.get("number")
            if number is None:
                continue    # a finding with no number cannot be referenced or acted on; never render "#None"
            candidates.append({"id": f"finding:{number}", "category": "blocking_debt",
                               "severity": _telemetry.severity_rank(row.get("severity"), threshold),
                               "recency": cursor_as_of, "source": "telemetry"})

    # The MEMORY half of the recent-decisions partition. Its source is BOTH recently merged pull requests
    # and the memory recall boot assembles into the pack; boot pulls that recall at cold
    # start (it pulls knowledge structure and memory recall when their servers are up) and RELAYS
    # it here. Attention never queries memory itself — the design rejects adding memory to attention's
    # direct-reads list (attention ranks the boot-assembled pack, it does not directly query memory), so this
    # arrives as DATA, exactly like the debt register's rows.
    #
    # Memory is NOT one of attention's four substrates, so it never enters `degraded_inputs`: an unreadable
    # store is boot's own memory-offline notice to raise (it owns that relay), and boot simply hands over
    # nothing here. The recency each row carries is already a trailing-Z moment the relay normalised, so the
    # ranking math never sees a raw epoch.
    for r in (memory_recall or []):
        candidates.append({"id": f"memory:{r.get('id')}", "category": "recent_decisions",
                           "recency": r.get("recency"), "source": "memory"})

    src = source or knowledge_query
    # Knowledge is AVAILABLE iff the map could be READ — NOT iff there was a focus to walk it from. The old
    # `if focus and ...` guard conflated the two: a clean session (no work in hand -> empty focus) skipped the
    # block entirely, so knowledge never entered `available` and boot raised the false "couldn't reach your
    # project map" notice every clean session. Split the two concerns: walk only when there is a focus, but
    # mark knowledge available whenever the map is reachable.
    if src is not None:
        try:
            if focus:
                # The cold-start adjacency walk is PINNED to the walk edges (WALK_EDGE_KINDS — the containment
                # edges plus the code-dependency and wiring edges; the attention policy's `## Scope` owns which
                # kinds ride it): supersedes stays pull-only, and orientation stays flat via the per-relationship
                # sample cap (not a small vocabulary). Pass the walk set explicitly rather than leaning on the
                # neighbors() default, so the pin lives at attention's own call site. Read it from `src` (the
                # boot slice carries the same WALK_EDGE_KINDS) so the branch never depends on the knowledge_query
                # module when boot passes its own rung-1 source.
                walk_edges = edge_filter if edge_filter is not None else list(src.WALK_EDGE_KINDS)
                # The walk is BIDIRECTIONAL (forward + reverse, `direction="both"`) over that same pinned edge
                # set. Forward-only starves a leaf: a non-check, ungoverned surface has no outgoing
                # structural edge but `provided_by` -> its module, so it collapses to just its module. Reverse
                # (`direction:in`) surfaces the connective tissue that already exists in the graph — a policy's
                # governed surfaces, a module's dependents/surfaces, any surface's targeting checks. Reverse is a
                # query-time direction over the SAME forward edges, NOT a new edge type, so it is
                # budget-neutral: reverse candidates compete for the same fixed structural_neighbors slice, never
                # grow it. A genuinely bare leaf (ungoverned AND untargeted, e.g. a tool) still resolves to only
                # its module; a dense neighbourhood is returned unranked, not relevance-ordered.
                # The focus is the work in hand — a single entity id or a SET (the changed work usually spans
                # several entities, StarshipSuperjam/engine-template#37). Walk each member, then DEDUPE neighbours and EXCLUDE any neighbour that
                # is itself a focus member (co-changed entities are not each other's "structural neighbours").
                # FOCUS_CAP bounds the focus set; the structural_neighbors PARTITION is bounded downstream by the
                # policy budget/trim via rank() — this cap does not (and need not) bound the candidate count.
                focus_ids = [focus] if isinstance(focus, str) else list(focus)
                focus_set = set(focus_ids)
                seen: set = set()
                for fid in focus_ids:
                    for n in src.neighbors(fid, edge_filter=walk_edges, depth=depth, direction="both"):
                        nid = n["id"]
                        if nid in focus_set or nid in seen:
                            continue
                        seen.add(nid)
                        candidates.append({"id": nid, "category": "structural_neighbors",
                                           "proximity": 1.0, "recency": None, "source": "knowledge"})
            else:
                # No focus to walk from (a clean session, no work in hand). The map is still REACHABLE, so
                # mark it available rather than degrade — `find()` is a reachability probe. It is a no-op on a
                # present boot slice (whose existence already proves the map was read), but the REAL read on
                # the `knowledge_query` fallback (when boot's slice read failed and `source` was None), which
                # RAISES KnowledgeUnavailable if the map is genuinely unreachable (rung 4) — so a true failure
                # still falls to the `except` below and degrades. A live-rebuilt map reads fine here (it WAS
                # reached); boot surfaces that separately, never as "couldn't reach" (see boot_slice from_live).
                src.find()
            available.add("knowledge")  # reached (walked, or probed reachable) -> NOT degraded
        except Exception:
            pass  # map genuinely unreachable (rung 4) -> degrade -> the real "couldn't reach" notice fires

    if work_record is not None:
        try:
            # The native git/GitHub work record, in the halves this module owns.
            # Every reader emits an already trailing-Z-normalised recency (or None), so the ranking math never
            # sees a bad ts. `git` is marked available only once BOTH reads succeed.
            #
            # In-flight (open PRs + the working branch). RAISES only when git cannot be consulted at all; an
            # empty list means git IS available with no in-flight work.
            for r in work_record.read_in_flight(gh=gh):
                candidates.append({"id": r["id"], "category": "in_flight",
                                   "recency": r.get("recency"), "source": "git"})
            # Recent decisions: recently merged pull requests — the structured PR body is the decision record,
            # there is no changelog. The LOCAL-GIT floor, so a cold session always reads what
            # shipped. This retires boot's separate RECENTLY_SHIPPED_COUNT digest into the ranked partition:
            # how many surface is now the policy's budget_recent_decisions slice, not a buried constant.
            # `shipped` lets the CALLER hand over rows it has already read (boot needs the same rows again to
            # restore the titles rank() strips) — one `git log` per session instead of two, and, like the
            # memory-recall half, the digest can then never name a merge the ranking did not rank. Left None
            # (the CLI) this reads the floor itself.
            for r in (work_record.read_recent_decisions() if shipped is None else shipped):
                candidates.append({"id": r["id"], "category": "recent_decisions",
                                   "recency": r.get("recency"), "source": "git"})
            # The project's plan — open Issues and Milestones — is NOT candidate work. These categories
            # budget a cold session's context, and the roadmap is not context; it
            # belongs to product-design's build-plan, and attention neither reads nor re-ranks it. Ranking it
            # would also spend the budget it is not entitled to: orientation flexes down to one slot on a
            # busy session, and a milestone there would evict `state:standing-situation` — the where-we-are
            # pointer that IS context.
            available.add("git")
        except Exception:
            pass  # the work record could not be fully consulted -> degrade over git (boot says so loudly)

    return candidates, available, cursor_as_of


def neighborhood_of(focus: "str | list[str] | None", *, depth: int = 1, source=None):
    """The work-in-hand's structural neighbourhood as a per-(member, relationship) SUMMARY — the render
    channel for the orientation block (StarshipSuperjam/engine-template#37). For each focus member it runs the SAME bidirectional,
    edge-pinned walk assemble_candidates runs (`direction="both"`, WALK_EDGE_KINDS, depth 1), then GROUPS the
    neighbours by the relationship that reaches each one — its (predicate, direction) — carrying the FULL
    count plus a bounded sample (NEIGHBORHOOD_SAMPLE_CAP) per group.

    This exists because the ranked partition cannot carry it: rank() reduces every member to {id, rank}
    (the per-neighbour detail is stripped) and structural_neighbors is a budgeted slice, so a hub focus would
    render an arbitrary capped few with no count. The per-relationship count preserved here lets the render
    DISCLOSE truncation honestly ("core provides 147, showing 4") instead of passing a sample off as salient.

    Returns {"focus": [ids], "groups": [{"source", "predicate", "direction", "total", "sample": [ids]}]} with
    groups in a deterministic order (focus order, then the pinned edge order, forward before reverse), or None
    when there is no work in hand / knowledge is unavailable (fail-open, like derive_focus — boot then renders
    no block). Returns IDs, not slugs: the render owns the plain-language slugging + relationship phrasing.

    `source` (opt-in; default off -> the `knowledge_query` module): boot passes its rung-1 boot-slice read-shim
    (StarshipSuperjam/engine-template#37), whose `neighbors()` returns the same shape in the same `(id,predicate,direction)` order — so the
    grouping/sampling here, and thus the rendered block, is byte-identical whether read from the cache or the
    live walk (the parity test pins this)."""
    src = source or knowledge_query
    if not focus or src is None:
        return None
    focus_ids = [focus] if isinstance(focus, str) else list(focus)
    focus_set = set(focus_ids)
    try:
        walk_edges = list(src.WALK_EDGE_KINDS)
        edge_order = {e: i for i, e in enumerate(walk_edges)}
        groups: list = []
        for idx, fid in enumerate(focus_ids):
            # Group this member's bidirectional neighbours by the relationship that reaches each — (predicate,
            # direction) — excluding co-changed focus members (they are not each other's structural neighbours).
            by_rel: dict = {}
            for n in src.neighbors(fid, edge_filter=walk_edges, depth=depth, direction="both"):
                nid = n["id"]
                if nid in focus_set:
                    continue
                bucket = by_rel.setdefault((n["predicate"], n["direction"]), [])
                if nid not in bucket:        # dedupe within a relationship (the walk's UNION already dedupes;
                    bucket.append(nid)       # this guards a degenerate multi-edge graph too)
            for (predicate, direction), nids in by_rel.items():
                groups.append({"source": fid, "predicate": predicate, "direction": direction,
                               "_order": (idx, edge_order.get(predicate, len(walk_edges)),
                                          0 if direction == "out" else 1),
                               "total": len(nids), "sample": nids[:NEIGHBORHOOD_SAMPLE_CAP]})
        groups.sort(key=lambda g: g.pop("_order"))
        return {"focus": focus_ids, "groups": groups}
    except Exception:
        return None  # knowledge unavailable -> no focused neighbourhood this session (boot renders no block)


# ---- live ranking (the single assembler shared by the CLI and boot) -------------------------

def _now_z() -> str:
    """The wall-clock reference moment, trailing-Z UTC. The ONLY clock read in attention, and only on the
    live path when the cursor carries no as-of; the result is marked as_of_is_wallclock."""
    return moment.utc_now()


def _reference_moment(candidates: list, cursor_as_of: str | None) -> str | None:
    """The newest moment the assembled pack actually knows about — the cursor's as-of, or any candidate's own
    recorded moment, whichever is latest. None only when nothing recorded a moment at all.

    WHY the cursor alone will not do: the committed cursor LAGS (it is refreshed on its own cadence, so a
    session routinely ranks work that landed after it). `intra_weight` measures age as `as_of - recency`
    FLOORED AT ZERO, so every candidate newer than the reference moment scores an identical 1.0 — they tie,
    the tie falls through to the id tiebreak, and "recently shipped" silently renders OLDEST-first. A
    reference moment that precedes the data it ranks cannot order that data: you cannot measure how old a
    thing is from a moment before it existed.

    Still clock-free and deterministic: every moment here is a RECORDED one (a merge date, a recall
    timestamp, the cursor), never the wall clock — so a run stays reproducible from its inputs and
    `as_of_is_wallclock` stays honest. Compared by absolute epoch, not lexically, because the recorded
    sources normalize differently; an unparseable moment is skipped rather than allowed to poison the
    reference."""
    best, best_epoch = None, None
    for moment in [cursor_as_of] + [c.get("recency") for c in candidates]:
        if not moment:
            continue
        try:
            seconds = attention_rank._epoch(moment)
        except (ValueError, TypeError, AttributeError):
            # AttributeError too: _epoch calls .replace on the value, so a non-string (a number, a list — a
            # corrupt state cursor can hold either) fails there, not on the parse.
            continue  # a malformed moment costs its own ordering, never the whole reference
        if best_epoch is None or seconds > best_epoch:
            best, best_epoch = moment, seconds
    return best


def rank_live(*, policy_path: str = POLICY_PATH, override: dict | None = None,
              focus: "str | list[str] | None" = None,
              depth: int = 1, budget_total: int | None = None, as_of: str | None = None,
              apply_precedence: bool = True, gh=None, source=None, live_findings: list | None = None,
              memory_recall: list | None = None, shipped: list | None = None) -> dict:
    """The live ranking path over the substrates present today, returning the attention-result.v1 dict
    (whose own `degraded_inputs` records the absent substrates). This is the ONE assembler the CLI (`rank`)
    and boot's SessionStart pack both call, so boot CONSUMES the partition it is handed — in the locked
    precedence order — and never re-ranks (relay-not-detect; the result contract is attention's,
    not boot's to re-derive). `as_of` defaults to the newest moment the pack recorded (`_reference_moment`:
    the cursor's integration-debt as-of, or a later candidate's own moment — a lagging cursor cannot order
    work that landed after it), falling back to the wall clock only when nothing recorded a moment at all
    (the run then marked `as_of_is_wallclock`) — the only clock read; the pure core stays clock-free. `budget_total` (boot owns it) sizes the per-category split when supplied. `override` is the
    attention slice of the operator policy-override that the LOADING layer (boot) reads and passes as DATA;
    it is merged per-key into the effective values via the core merge (`load_policy_values`), keeping the
    static-input determinism — attention never reads the override FILE itself. `gh` is the GitHub reader for
    the in-flight work-record read (boot builds + passes it; the CLI leaves it None -> the local-git floor).
    `source` (opt-in, default off -> `knowledge_query`) is boot's rung-1 boot-slice read-shim, threaded to
    assemble_candidates so the structural-neighbours walk reads the cache, not the SQLite index (StarshipSuperjam/engine-template#37).
    `live_findings` is the live telemetry register's per-issue rows boot already read (open_findings): a list (0
    included) marks the register read -> telemetry is available and each open finding becomes its own graded
    blocking-debt candidate; None (the offline CLI, or a failed live read) -> telemetry degrades and the
    committed state count stands in."""
    policy_values = load_policy_values(policy_path, override)
    candidates, available, cursor_as_of = assemble_candidates(policy_values, focus=focus, depth=depth, gh=gh,
                                                              source=source, live_findings=live_findings,
                                                              memory_recall=memory_recall, shipped=shipped)
    resolved_as_of = as_of or _reference_moment(candidates, cursor_as_of)
    as_of_is_wallclock = False
    if resolved_as_of is None:
        resolved_as_of, as_of_is_wallclock = _now_z(), True
    return rank(candidates, policy_values, resolved_as_of, available,
                budget_total=budget_total, apply_precedence=apply_precedence,
                as_of_is_wallclock=as_of_is_wallclock)


# ---- CLI ------------------------------------------------------------------------------------


def _emit(result: dict) -> None:
    # allow_nan=False: the core coerces non-finite signals away, so a NaN can never reach here on a valid
    # run — but if one ever did it must fail LOUD rather than emit an invalid bare-NaN token (halt-on-malformed).
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _note_degrade(degraded: list) -> None:
    if degraded:
        print(f"(attention ranked over the available substrates; absent and degraded over: "
              f"{', '.join(degraded)} — boot surfaces this loudly at its slice)", file=sys.stderr)


def _cmd_rank(rest: list) -> int:
    """Live rank over the substrates present today (the operator CLI over the shared `rank_live`). With no
    explicit `--focus`, the focus is AUTO-DERIVED from the in-flight work record (the orientation default, so
    the CLI shows the same focused read boot does); `--no-focus` opts out (a bare, focus-free rank)."""
    if "--no-focus" in rest:
        focus = None
    else:
        explicit = _flag(rest, "--focus", None)
        focus = explicit if explicit is not None else (derive_focus() or None)
    budget = _flag(rest, "--budget", None)
    result = rank_live(
        policy_path=_flag(rest, "--policy", POLICY_PATH),
        focus=focus,
        depth=int(_flag(rest, "--depth", "1")),
        budget_total=int(budget) if budget is not None else None,
        as_of=_flag(rest, "--as-of", None),
        apply_precedence="--no-precedence" not in rest)
    _note_degrade(result["degraded_inputs"])
    _emit(result)
    return 0


def _cmd_rank_fixture(rest: list) -> int:
    """Rank a CONSTRUCTED candidate-set from a fixture file — the demo/test path, no live substrates. The
    fixture is {"candidates": [...], "available_inputs": [...]}. --as-of is REQUIRED so the core stays clock-free.
    """
    if not rest:
        print("usage: attention.py rank-fixture FIXTURE.json --as-of <UTC-Z> [--policy P] [--budget N] "
              "[--no-precedence]", file=sys.stderr)
        return 2
    fixture = validate.load_json(rest[0])
    as_of = _flag(rest, "--as-of", None)
    if not as_of:
        print("rank-fixture requires --as-of <UTC-Z> (the core never reads the clock)", file=sys.stderr)
        return 2
    policy_values = load_policy_values(_flag(rest, "--policy", POLICY_PATH))
    budget = _flag(rest, "--budget", None)
    result = rank(fixture.get("candidates", []), policy_values, as_of,
                  set(fixture.get("available_inputs", [])),
                  budget_total=int(budget) if budget is not None else None,
                  apply_precedence="--no-precedence" not in rest)
    _emit(result)
    return 0


# A built-in constructed fixture for the scripted demo: one blocking-debt item with a deliberately LOW
# weight (old, severity just at the bar) and one in-flight feature with a deliberately HIGH weight (current,
# close). With precedence the debt leads structurally; without it the feature out-weighs and floats up.
DEMO_AS_OF = "2026-06-04T00:00:00Z"
DEMO_CANDIDATES = [
    {"id": "debt:payment-overdue", "category": "blocking_debt", "severity": 2,
     "recency": "2026-04-01T00:00:00Z", "source": "telemetry"},
    {"id": "feature:shiny-rewrite", "category": "in_flight", "proximity": 1.0,
     "recency": "2026-06-04T00:00:00Z", "source": "git"},
]


def _lead_id(result: dict) -> str:
    """The id of the single highest-ranked candidate across the whole partition (flattened in array order)."""
    for entry in result["partition"]:
        if entry["members"]:
            return entry["members"][0]["id"]
    return "(none)"


def _dropped(result: dict, trim_ranks: dict | None = None) -> list:
    """The categories the trim order shed under this run's budget — those allotted zero slots (budget_size 0).
    Empty when the budget seated every kind (trim inert). With `trim_ranks`, returned in SHED order (least
    important first), so the list reads in the order they were dropped; otherwise in partition order."""
    dropped = [e["category"] for e in result["partition"] if e.get("budget_size") == 0]
    return sorted(dropped, key=lambda c: trim_ranks[c]) if trim_ranks is not None else dropped


def _cmd_demo(rest: list) -> int:
    """Scripted fail->pass the operator can read without JSON, and VARY with one flag. Ranks the built-in
    fixture WITH precedence, with precedence REMOVED, then RESTORED, printing the lead each time. The flag
    `--feature-proximity N` raises the feature's ranking weight, so the operator can confirm by hand that
    blocking debt STILL leads with precedence however high the feature's weight goes (the guarantee is
    structural). No JSON authoring needed — one flag does it."""
    policy_values = load_policy_values(_flag(rest, "--policy", POLICY_PATH))
    feature_proximity = float(_flag(rest, "--feature-proximity", "1.0"))
    candidates = [dict(DEMO_CANDIDATES[0]), {**DEMO_CANDIDATES[1], "proximity": feature_proximity}]
    avail = {"state", "knowledge"}  # the demo fixture stands in for telemetry/git content; mark them degraded
    with_p = rank(candidates, policy_values, DEMO_AS_OF, avail, apply_precedence=True)
    without_p = rank(candidates, policy_values, DEMO_AS_OF, avail, apply_precedence=False)
    restored = rank(candidates, policy_values, DEMO_AS_OF, avail, apply_precedence=True)
    print("Attention ranking — structural-precedence demonstration")
    print("  Candidates: a blocking-debt item ('debt:payment-overdue', old, lower weight) and an in-flight")
    print(f"              feature ('feature:shiny-rewrite', current and close; proximity={feature_proximity}).")
    print(f"  WITH precedence (the engine's normal behaviour): leads with -> {_lead_id(with_p)}")
    print("      The blocking debt leads even though the feature has the higher ranking weight, because the")
    print("      order of importance is structural, not a weight anything can out-tune.")
    print(f"  PRECEDENCE REMOVED (diagnostic): leads with -> {_lead_id(without_p)}")
    print("      With the structural order taken away, the higher-weighted feature floats to the top —")
    print("      proving the lead above was held by structure, not by the weights.")
    print(f"  PRECEDENCE RESTORED: leads with -> {_lead_id(restored)}")
    print("  Try it yourself (no file editing needed): re-run with the feature's weight cranked far higher —")
    print("      uv run --directory .engine -- python tools/attention.py demo --feature-proximity 1000")
    print("  WITH precedence the blocking debt STILL leads. The guarantee is structural, not out-tunable.")

    # --- trim (the overflow rule): what is shed first when the budget can't seat every kind ---
    # The budget splits a fixed number of item-slots across the five kinds by the policy's shares. When the
    # budget is too small to give every kind a slot, the TRIM ORDER decides what is shed first. Here we hold
    # the candidates fixed and vary only the budget, then flip the trim order — proving trim_* is live.
    shipped_trim = {c: policy_values[f"trim_{c}"] for c in CATEGORIES}
    reversed_trim = {c: 6 - policy_values[f"trim_{c}"] for c in CATEGORIES}  # invert the 1..5 permutation
    reversed_policy = {**policy_values, **{f"trim_{c}": reversed_trim[c] for c in CATEGORIES}}
    generous = rank(candidates, policy_values, DEMO_AS_OF, avail, budget_total=20)
    tight = rank(candidates, policy_values, DEMO_AS_OF, avail, budget_total=3)
    tight_reversed = rank(candidates, reversed_policy, DEMO_AS_OF, avail, budget_total=3)
    print("Attention budget — trim (overflow) demonstration")
    print("  The budget is a chosen count of item-slots to surface (not a measurement of context), split")
    print("  across the five kinds by the policy's shares. When it can't seat them all, trim sheds first.")
    print(f"  Generous budget (20 slots — the cold-start default): shed nothing -> {_dropped(generous) or 'none'}")
    print("      Every kind seats, so trim is INERT here — exactly its state in a normal session.")
    print(f"  Tight budget (3 slots), shipped trim order: shed (in order) -> {_dropped(tight, shipped_trim)}")
    print("      The least-important kinds go first (general orientation, then structural neighbours);")
    print("      blocking debt is kept — last in the shipped trim order.")
    print(f"  Tight budget (3 slots), trim order REVERSED: shed (in order) -> {_dropped(tight_reversed, reversed_trim)}")
    print("      Now blocking debt is shed instead — proving the dial is live: changing trim_* changes")
    print("      what is dropped. (In normal use blocking debt is never trimmed — it is shed last.)")
    # the self-check asserts BOTH structural invariants: (1) precedence — debt leads WITH precedence and again
    # once restored; (2) trim — at a tight budget the shipped order sheds orientation and keeps blocking debt,
    # and reversing the trim order sheds blocking debt instead (so trim_* demonstrably governs the outcome).
    ok_precedence = _lead_id(with_p) == "debt:payment-overdue" and _lead_id(restored) == "debt:payment-overdue"
    ok_trim = (not _dropped(generous)
               and "orientation" in _dropped(tight) and "blocking_debt" not in _dropped(tight)
               and "blocking_debt" in _dropped(tight_reversed))
    if not (ok_precedence and ok_trim):
        print("DEMO UNEXPECTED: a structural guarantee did not hold on the built-in fixture "
              f"(precedence_ok={ok_precedence}, trim_ok={ok_trim}).", file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if not argv:
        print("usage: attention.py {rank [--focus ID] [--no-focus] [--as-of T] [--budget N] [--no-precedence] "
              "[--policy P] | rank-fixture FIXTURE.json --as-of T [...] | demo}", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "rank":
        return _cmd_rank(rest)
    if cmd == "rank-fixture":
        return _cmd_rank_fixture(rest)
    if cmd == "demo":
        return _cmd_demo(rest)
    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


def _flag(argv: list, flag: str, default):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else default


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
