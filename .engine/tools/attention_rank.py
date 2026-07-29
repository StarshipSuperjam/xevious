#!/usr/bin/env python3
"""The attention ranking function (the deterministic core).

Attention is a policy plus a function, not a store: the policy
(.engine/policies/attention.md) carries the tuning values; THIS module is the function. It realizes the
locked form — an **ordered partition with weighted intra-partition ranking** over the five fixed
budget categories — under **hard cross-category precedence**: a lower-importance category can never overtake
a higher one whatever the per-candidate weights, because precedence is the primary sort key (structural),
never a weight. The result is an attention-result.v1 object (.engine/schemas/attention-result.v1.json).

This file is the PURE core: no file IO, no clock, no substrate reads — it ranks a candidate-set it is
HANDED and is fully fixture-testable. The substrate-facing assembly (state/knowledge today; telemetry/git
later) and the CLI live in attention.py. The split mirrors the spec's own seam: membership
is telemetry's promotion call, *blocking* is attention's debt-blocking rule (the one membership decision
this core owns), and "the partition orders what those determinations hand it."

Determinism: the same (candidates, policy_values, as_of, available_inputs) yield a byte-identical
result. `as_of` is the ONLY time source — the core never reads the wall clock; recency is a weight over that
recorded moment. The intra-partition score is ABSOLUTE per candidate (no cross-candidate range normalization),
so there is no zero-range division and thus no NaN — a single-member or all-equal partition ranks cleanly.
Ties break on the candidate id (a total order). Degrades over partial inputs: it ranks whatever is present
and records the absent substrates in `degraded_inputs`; it never narrates that (boot surfaces it loudly).

Quality is NOT proven here. The form is fixed and tested (partition assignment + structural precedence);
the concrete values in the policy are uncalibrated starting values, so "surfaces the right things first"
stays unproven until calibrated.
"""
from __future__ import annotations
import math

import moment  # the trailing-Z time seam; pure stdlib leaf, reads no clock/IO/substrate (epoch is a pure parse)

RESULT_SCHEMA_VERSION = 1

# The five budget categories in their LOCKED canonical order. This tuple is
# the category vocabulary; the policy supplies the precedence RANKS, budget fractions, and trim ranks over it.
CATEGORIES = ("blocking_debt", "in_flight", "recent_decisions", "structural_neighbors", "orientation")

# The substrate inputs attention ranks over. The git/GitHub work-record reader (work_record) feeds in_flight
# candidates, so `git` is available when the work record can be consulted and only degraded when git cannot be
# read. `telemetry` is the live debt register (open engine-labelled Issues): boot reads it once and threads the
# count into the assembler, so it is available on a successful read and in degraded_inputs ONLY when that read
# failed (an outage/expired auth) or the offline CLI ran with no reader — the committed state count stands in then.
SUBSTRATES = ("state", "knowledge", "telemetry", "git")

# The policy value keys this core reads (the drift-guard set; the VALUES themselves live only in the policy).
BUDGET_KEYS = tuple(f"budget_{c}" for c in CATEGORIES)
PRECEDENCE_KEYS = tuple(f"precedence_{c}" for c in CATEGORIES)
TRIM_KEYS = tuple(f"trim_{c}" for c in CATEGORIES)
WEIGHT_KEYS = ("weight_recency", "weight_severity", "weight_proximity")
FLEX_KEYS = ("flex_high_debt_count", "flex_orientation_delta")
THRESHOLD_KEYS = ("debt_blocking_threshold",)
EXPECTED_VALUE_KEYS = frozenset(
    BUDGET_KEYS + PRECEDENCE_KEYS + TRIM_KEYS + WEIGHT_KEYS + FLEX_KEYS + THRESHOLD_KEYS)


# ---- pure helpers (each fixture-testable) ---------------------------------------------------

def _epoch(ts: str) -> float:
    """Parse a trailing-Z UTC moment to absolute epoch seconds (deterministic; UTC-anchored). A malformed or
    absent value degrades to 0.0 (the oldest position) via `_finite`, never a crash or a non-total sort key —
    the recall-crash class this ranking must not reintroduce."""
    return _finite(moment.epoch(ts))


def _finite(x):
    """A finite number as-is (an int stays an int), or 0.0 for None / non-numeric / NaN / Infinity. A
    malformed numeric signal must never poison the ranking math or leak a non-JSON NaN into the result — a
    degraded signal contributes the weakest position rather than crashing or producing an invalid output."""
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else 0.0


def assign_partition(candidate: dict, policy_values: dict):
    """Which of the five categories a candidate belongs to — or None when it is open debt that does NOT
    gate work (it can wait; not surfaced in the budgeted five). Membership comes from the candidate's
    own `category` label (its source — telemetry/state/knowledge — set it); attention does NOT invent it.
    The ONE membership call attention owns: an open-debt candidate (category 'blocking_debt')
    is BLOCKING iff its severity reaches the policy's debt-blocking threshold; below the bar it can wait
    (returns None — excluded from the surfaced partition, not promoted to any other category). Raises on an
    unknown category label (the source must speak the locked vocabulary)."""
    cat = candidate.get("category")
    if cat not in CATEGORIES:
        raise ValueError(f"unknown candidate category {cat!r}; valid: {list(CATEGORIES)}")
    if cat == "blocking_debt":
        severity = candidate.get("severity")
        finite = isinstance(severity, (int, float)) and not isinstance(severity, bool) and math.isfinite(severity)
        if not finite or severity < policy_values["debt_blocking_threshold"]:
            return None  # no provable severity (absent / malformed / below the bar) -> a deferral, not blocking
    return cat


def intra_weight(candidate: dict, policy_values: dict, as_of: str) -> float:
    """The within-partition ranking score for one candidate — higher sorts earlier. A weighted sum of three
    ABSOLUTE per-candidate signals, each mapped into a comparable 0..1 band by a parameter-free bounded
    transform (no cross-candidate normalization, so no zero-range NaN): recency over the recorded `as_of`
    (1/(1+age_in_days) — the same moment as as_of scores ~1.0, older scores toward 0), debt severity
    (severity/(1+severity) — so a more severe item scores higher but can never dwarf the other signals, which
    keeps cross-category comparison meaningful when precedence is removed), and structural proximity. A
    missing signal contributes zero (the weakest position). `as_of` is the only time source — never the clock."""
    recency_score = 0.0
    recency = candidate.get("recency")
    if recency:
        age_days = max(0.0, _epoch(as_of) - _epoch(recency)) / 86400.0
        recency_score = 1.0 / (1.0 + age_days)
    severity = _finite(candidate.get("severity"))
    severity_score = severity / (1.0 + severity) if severity > 0 else 0.0
    proximity_score = _finite(candidate.get("proximity"))
    return (policy_values["weight_recency"] * recency_score
            + policy_values["weight_severity"] * severity_score
            + policy_values["weight_proximity"] * proximity_score)


def precedence_rank(category: str, policy_values: dict, apply_precedence: bool = True) -> int:
    """The category's hard cross-category precedence rank (1 = surfaced first). When precedence is REMOVED
    (the diagnostic state the demo flips to), every category reports rank 1 — the sort then falls through to
    the intra-partition weight, so a higher-weighted candidate floats above blocking debt, proving the
    ordering was structural and not weight-driven."""
    return policy_values[f"precedence_{category}"] if apply_precedence else 1


def session_condition(candidates: dict, policy_values: dict) -> str:
    """The session-load reading that drives the budget flex: 'high_debt' once the number of BLOCKING-debt
    candidates reaches the policy's flex threshold, else 'clean'. Deterministic over the candidate set."""
    blocking = sum(1 for c in candidates if assign_partition(c, policy_values) == "blocking_debt")
    return "high_debt" if blocking >= policy_values["flex_high_debt_count"] else "clean"


def apply_flex(base_fractions: dict, condition: str, policy_values: dict) -> dict:
    """Flex the base budget split by session condition (this is ATTENTION's, not
    boot's): a clean session gives more room to orientation (taken from blocking debt); a high-debt session
    compresses orientation (given to blocking debt). The shift is the policy's `flex_orientation_delta`,
    clamped at zero and renormalized so the five fractions still sum to one."""
    delta = policy_values["flex_orientation_delta"]
    f = dict(base_fractions)
    if condition == "clean":
        moved = min(delta, f["blocking_debt"])
        f["orientation"] += moved
        f["blocking_debt"] -= moved
    elif condition == "high_debt":
        moved = min(delta, f["orientation"])
        f["orientation"] -= moved
        f["blocking_debt"] += moved
    total = sum(f.values())
    return {c: f[c] / total for c in CATEGORIES} if total else {c: f[c] for c in CATEGORIES}


def _largest_remainder(fractions: dict, budget_total: int, members: tuple) -> dict:
    """Apportion budget_total across `members` by their (already-renormalized) fractions, summing EXACTLY
    to the total: floor each share, then hand the flooring leftover to the largest fractional remainders,
    ties broken by the locked CATEGORIES order — deterministic, no rounding drift. The proportional core
    budget_split runs over its surviving categories."""
    raw = {c: fractions[c] * budget_total for c in members}
    floors = {c: int(raw[c]) for c in members}
    leftover = budget_total - sum(floors.values())
    order = sorted(members, key=lambda c: (-(raw[c] - floors[c]), CATEGORIES.index(c)))
    for c in order[:max(0, leftover)]:
        floors[c] += 1
    return floors


def budget_split(fractions: dict, budget_total: int, trim_ranks: dict) -> dict:
    """Apportion an integer total budget across the five categories by their fractions, summing EXACTLY to
    the total — with the TRIM ORDER deciding what is shed when the budget cannot seat them all.

    The proportional largest-remainder split (_largest_remainder) seats every category whenever the budget
    can fit one slot for each — which, at a generous total, it does, so budget_split returns the plain
    proportional split UNCHANGED. `trim_ranks` (a permutation of 1..5; rank 1 sheds FIRST — the policy's
    reverse-of-precedence default, attention.md) is the OVERFLOW rule: it changes only WHICH categories
    take the zeros when 'space runs short'. Rather than letting whichever category lost the largest-
    remainder race fall to zero, the least-important category (trim rank 1) is shed first, its share handed
    to the survivors and the split re-apportioned over them — repeating until every survivor seats (>=1) or
    one category remains. So trim is INERT while the budget fits everything and load-bearing only under
    genuine overflow; it is the realization of "what is dropped first when space runs short".

    blocking_debt carries the highest default trim rank (shed LAST), so at the shipped trim order it is
    never dropped — but that is a property of the shipped VALUES, not a structural law: a different trim
    order sheds it sooner (the demo flips the order to prove the dial is live). Deterministic: trim_ranks
    is a total order and the largest-remainder tiebreak is the locked CATEGORIES order, so the same
    (fractions, budget_total, trim_ranks) yield a byte-identical result."""
    survivors = list(CATEGORIES)
    shed_order = sorted(CATEGORIES, key=lambda c: trim_ranks[c])  # trim rank 1 (least important) sheds first
    shed_i = 0
    sizes: dict = {c: 0 for c in CATEGORIES}
    while True:
        share_total = sum(fractions[c] for c in survivors) or 1.0
        renorm = {c: fractions[c] / share_total for c in survivors}
        sizes = _largest_remainder(renorm, budget_total, tuple(survivors))
        if len(survivors) == 1 or all(sizes[c] >= 1 for c in survivors):
            break
        while shed_i < len(shed_order) and shed_order[shed_i] not in survivors:
            shed_i += 1
        if shed_i >= len(shed_order):
            break
        survivors.remove(shed_order[shed_i])
        shed_i += 1
    return {c: sizes.get(c, 0) for c in CATEGORIES}


def _signals(candidate: dict) -> dict:
    """The optional per-member signals block (only the signals the candidate actually carries)."""
    sig = {}
    if candidate.get("severity") is not None:
        sig["severity"] = _finite(candidate["severity"])
    if candidate.get("proximity") is not None:
        sig["proximity"] = _finite(candidate["proximity"])
    if "recency" in candidate:
        sig["recency"] = candidate.get("recency")
    return sig


def rank(candidates: list, policy_values: dict, as_of: str, available_inputs,
         *, budget_total: int | None = None, apply_precedence: bool = True,
         as_of_is_wallclock: bool = False) -> dict:
    """Rank a candidate-set into the ordered partition. PURE and deterministic: the same arguments always
    yield a byte-identical attention-result.v1 dict. `available_inputs` is the set of substrate names that
    fed candidate assembly; every absent substrate is recorded in `degraded_inputs` (the core is TOLD what
    was available — it never fetches — so degradation is reproducible). `apply_precedence=False` is the
    demo's diagnostic flip (precedence removed → ordering falls to weight). `budget_total` sizes each
    category's integer slot when supplied; omit it and only the fractions are reported (boot owns the total).
    """
    surfaced = [c for c in candidates if assign_partition(c, policy_values) is not None]
    by_category = {cat: [] for cat in CATEGORIES}
    for cand in surfaced:
        by_category[assign_partition(cand, policy_values)].append(cand)
    # order within each category: best-first by weight, ties broken by id (a total order)
    ordered = {cat: sorted(members, key=lambda c: (-intra_weight(c, policy_values, as_of), c["id"]))
               for cat, members in by_category.items()}

    condition = session_condition(candidates, policy_values)
    base = {c: policy_values[f"budget_{c}"] for c in CATEGORIES}
    applied = apply_flex(base, condition, policy_values)
    trim_ranks = {c: policy_values[f"trim_{c}"] for c in CATEGORIES}
    sizes = budget_split(applied, budget_total, trim_ranks) if budget_total is not None else None

    ranks = {c: precedence_rank(c, policy_values, apply_precedence) for c in CATEGORIES}
    best = {c: (-intra_weight(ordered[c][0], policy_values, as_of) if ordered[c] else float("inf"))
            for c in CATEGORIES}
    # the partition array order IS the cross-category precedence; with precedence removed (all ranks equal)
    # it falls through to the best-member weight, so the highest-weighted category floats to the front.
    cats_in_order = sorted(CATEGORIES, key=lambda c: (ranks[c], best[c], CATEGORIES.index(c)))

    partition = []
    for cat in cats_in_order:
        members = []
        for i, cand in enumerate(ordered[cat]):
            member = {"id": cand["id"], "rank": i + 1}
            sig = _signals(cand)
            if sig:
                member["signals"] = sig
            members.append(member)
        entry = {"category": cat, "precedence_rank": ranks[cat],
                 "budget_fraction": round(applied[cat], 6), "members": members}
        if sizes is not None:
            entry["budget_size"] = sizes[cat]
        partition.append(entry)

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "as_of": as_of,
        "as_of_is_wallclock": as_of_is_wallclock,
        "session_condition": condition,
        "partition": partition,
        "degraded_inputs": sorted(set(SUBSTRATES) - set(available_inputs)),
    }
