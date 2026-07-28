#!/usr/bin/env python3
"""Self-tests for the attention ranking function: the pure deterministic core (attention_rank),
its versioned output contract (attention-result.v1.json), and the policy it reads (.engine/policies/attention.md).

Run: uv run --directory .engine --frozen -- python -m unittest discover -s tools -p 'test_*.py' -b

These lock the FORM, not the calibration (the values are uncalibrated starting values, so
ranking *quality* is deliberately NOT asserted here):
  - partition ASSIGNMENT — a candidate lands in the category its source labels, and the ONE membership call
    attention owns (open debt is blocking iff severity reaches the policy bar) fires correctly; an unknown
    label is refused (the source must speak the locked vocabulary);
  - the HEADLINE structural guarantee — a low-weight blocking-debt item leads a high-weight feature WITH
    precedence, the feature floats up when precedence is removed, and the debt returns when it is restored:
    precedence is structural, never weight-driven;
  - DETERMINISM — same inputs yield byte-identical output; input order does not matter; ties break on id; the
    absolute (non-range-normalized) scoring yields no NaN even for a single-member or all-equal partition;
  - the budget FLEX — a clean session widens orientation, a high-debt session compresses it, fractions still
    sum to one (attention owns the flex);
  - DEGRADE — the result records absent substrates and never narrates; every category is present even empty;
  - the result CONFORMS to attention-result.v1 and the schema has teeth (this is the ONLY well-formedness lock
    on that schema — no live rule targets .engine/schemas/*.json);
  - the committed policy carries EXACTLY the value keys the tool reads (a drift guard), with precedence/trim a
    permutation of 1..5 and the budget fractions summing to one, and conforms to policy.v1.
"""
from __future__ import annotations
import copy
import json
import math
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402
import attention_rank    # noqa: E402
import attention         # noqa: E402
import telemetry         # noqa: E402  (the severity CLASSES the live register grades findings by)
from attention_rank import (rank, assign_partition, intra_weight, session_condition, apply_flex,  # noqa: E402
                            budget_split, CATEGORIES, SUBSTRATES, EXPECTED_VALUE_KEYS,
                            PRECEDENCE_KEYS, TRIM_KEYS)

RESULT_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "attention-result.v1.json"))
POLICY_SCHEMA = validate.load_json(os.path.join(validate.SCHEMAS_DIR, "policy.v1.json"))
POLICY_PATH = os.path.join(validate.ENGINE_DIR, "policies", "attention.md")
AS_OF = "2026-06-04T00:00:00Z"

# A self-contained fixture policy so the FORM tests do not depend on the real (tunable) numbers: precedence
# in canonical order, trim its reverse, budgets summing to one, a high debt-blocking bar of 2.
FIXTURE_POLICY = {
    **{f"budget_{c}": v for c, v in zip(CATEGORIES, [0.30, 0.25, 0.15, 0.15, 0.15])},
    **{f"precedence_{c}": i + 1 for i, c in enumerate(CATEGORIES)},
    **{f"trim_{c}": i + 1 for i, c in enumerate(reversed(CATEGORIES))},
    "weight_recency": 0.5, "weight_severity": 1.0, "weight_proximity": 0.5,
    "flex_high_debt_count": 3, "flex_orientation_delta": 0.10,
    "debt_blocking_threshold": 2,
}

# The headline pair: a blocking-debt item that is OLD and just-at-the-bar (low weight) and an in-flight
# feature that is CURRENT and close (high weight).
DEBT = {"id": "debt:overdue", "category": "blocking_debt", "severity": 2,
        "recency": "2026-04-01T00:00:00Z", "source": "telemetry"}
FEATURE = {"id": "feat:shiny", "category": "in_flight", "proximity": 1.0,
           "recency": "2026-06-04T00:00:00Z", "source": "git"}


def _errors(schema, instance):
    return list(validate.Draft202012Validator(schema).iter_errors(instance))


def _flatten(result):
    """Member ids across the whole partition in result order (partition array order, then member order)."""
    return [m["id"] for entry in result["partition"] for m in entry["members"]]


class TestAssignment(unittest.TestCase):
    def test_each_candidate_lands_in_its_category(self):
        cases = [
            ({"id": "d", "category": "blocking_debt", "severity": 3}, "blocking_debt"),
            ({"id": "b", "category": "in_flight"}, "in_flight"),
            ({"id": "p", "category": "recent_decisions"}, "recent_decisions"),
            ({"id": "n", "category": "structural_neighbors"}, "structural_neighbors"),
            ({"id": "o", "category": "orientation"}, "orientation"),
        ]
        for cand, expected in cases:
            self.assertEqual(assign_partition(cand, FIXTURE_POLICY), expected)

    def test_open_debt_below_threshold_is_not_blocking(self):
        # severity 1 is below the bar of 2 -> a deferral, not surfaced as blocking (returns None)
        self.assertIsNone(assign_partition({"id": "d", "category": "blocking_debt", "severity": 1}, FIXTURE_POLICY))
        # missing severity is likewise not provably blocking
        self.assertIsNone(assign_partition({"id": "d", "category": "blocking_debt"}, FIXTURE_POLICY))

    def test_open_debt_at_or_above_threshold_is_blocking(self):
        self.assertEqual(assign_partition({"id": "d", "category": "blocking_debt", "severity": 2}, FIXTURE_POLICY),
                         "blocking_debt")
        self.assertEqual(assign_partition({"id": "d", "category": "blocking_debt", "severity": 9}, FIXTURE_POLICY),
                         "blocking_debt")

    def test_unknown_category_label_raises(self):
        with self.assertRaises(ValueError):
            assign_partition({"id": "x", "category": "backlog"}, FIXTURE_POLICY)

    def test_non_finite_severity_is_not_blocking(self):
        # a malformed (NaN/Infinity) severity is not a PROVABLE severity -> not surfaced as blocking
        for bad in (float("nan"), float("inf")):
            self.assertIsNone(assign_partition({"id": "d", "category": "blocking_debt", "severity": bad},
                                               FIXTURE_POLICY))


class TestPrecedenceIsStructural(unittest.TestCase):
    """The slice's whole point: precedence is structural, not a weight anything can out-tune."""

    def test_blocking_debt_outranks_higher_weighted_feature(self):
        self.assertGreater(intra_weight(FEATURE, FIXTURE_POLICY, AS_OF),
                           intra_weight(DEBT, FIXTURE_POLICY, AS_OF),
                           "the fixture must give the feature the HIGHER weight for the test to be meaningful")
        flat = _flatten(rank([FEATURE, DEBT], FIXTURE_POLICY, AS_OF, set()))
        self.assertLess(flat.index("debt:overdue"), flat.index("feat:shiny"),
                        "with precedence, blocking debt leads despite its lower weight")

    def test_remove_precedence_lets_the_feature_float_up(self):
        result = rank([FEATURE, DEBT], FIXTURE_POLICY, AS_OF, set(), apply_precedence=False)
        flat = _flatten(result)
        self.assertLess(flat.index("feat:shiny"), flat.index("debt:overdue"),
                        "with precedence removed, the higher-weighted feature floats above the debt")
        self.assertTrue(all(e["precedence_rank"] == 1 for e in result["partition"]),
                        "precedence-removed is the visible diagnostic state: every category at rank 1")

    def test_restore_precedence_returns_debt_to_the_top(self):
        flat = _flatten(rank([FEATURE, DEBT], FIXTURE_POLICY, AS_OF, set(), apply_precedence=True))
        self.assertLess(flat.index("debt:overdue"), flat.index("feat:shiny"))


class TestDeterminism(unittest.TestCase):
    def test_same_inputs_same_output(self):
        a = rank([DEBT, FEATURE], FIXTURE_POLICY, AS_OF, {"state"}, budget_total=20)
        b = rank([DEBT, FEATURE], FIXTURE_POLICY, AS_OF, {"state"}, budget_total=20)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_input_order_does_not_matter(self):
        a = rank([DEBT, FEATURE], FIXTURE_POLICY, AS_OF, set())
        b = rank([FEATURE, DEBT], FIXTURE_POLICY, AS_OF, set())
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_ties_break_on_id(self):
        # two identical-weight in-flight items (no signals) -> ordered by id ascending
        x = {"id": "zzz", "category": "in_flight"}
        y = {"id": "aaa", "category": "in_flight"}
        members = rank([x, y], FIXTURE_POLICY, AS_OF, set())["partition"]
        in_flight = next(e for e in members if e["category"] == "in_flight")
        self.assertEqual([m["id"] for m in in_flight["members"]], ["aaa", "zzz"])

    def test_single_member_and_all_equal_partitions_yield_no_nan(self):
        # the absolute (non-range-normalized) scoring cannot divide by a zero range; assert a finite,
        # JSON-strict result (json with allow_nan=False raises on a NaN/Infinity) for these edge fixtures.
        same_ts = [{"id": f"n{i}", "category": "structural_neighbors", "recency": AS_OF, "proximity": 0.5}
                   for i in range(3)]
        lone = [{"id": "solo", "category": "orientation"}]
        for fixture in (same_ts, lone, []):
            result = rank(fixture, FIXTURE_POLICY, AS_OF, set())
            json.dumps(result, allow_nan=False)  # raises if any NaN/Infinity leaked into the result

    def test_as_of_is_echoed_never_a_fresh_clock_read(self):
        result = rank([FEATURE], FIXTURE_POLICY, AS_OF, set())
        self.assertEqual(result["as_of"], AS_OF)

    def test_non_finite_signal_is_coerced_not_leaked(self):
        # a malformed NaN/Infinity signal must never poison the math or leak a non-JSON NaN into the result;
        # it is coerced to the weakest position (the degrade posture), and the result is strict-valid JSON.
        for bad in (float("nan"), float("inf"), float("-inf")):
            result = rank([{"id": "n", "category": "structural_neighbors", "proximity": bad, "recency": None}],
                          FIXTURE_POLICY, AS_OF, set())
            json.dumps(result, allow_nan=False)  # raises if any NaN/Infinity leaked
            members = [m for e in result["partition"] for m in e["members"]]
            self.assertTrue(all(math.isfinite(m["signals"]["proximity"]) for m in members if "signals" in m))


class TestFlex(unittest.TestCase):
    def _orientation_fraction(self, result):
        return next(e["budget_fraction"] for e in result["partition"] if e["category"] == "orientation")

    def test_clean_session_widens_orientation(self):
        result = rank([FEATURE], FIXTURE_POLICY, AS_OF, set())  # no blocking debt -> clean
        self.assertEqual(result["session_condition"], "clean")
        self.assertGreater(self._orientation_fraction(result), FIXTURE_POLICY["budget_orientation"])

    def test_high_debt_session_compresses_orientation(self):
        debts = [{"id": f"d{i}", "category": "blocking_debt", "severity": 3} for i in range(3)]  # >= flex count
        result = rank(debts, FIXTURE_POLICY, AS_OF, set())
        self.assertEqual(result["session_condition"], "high_debt")
        self.assertLess(self._orientation_fraction(result), FIXTURE_POLICY["budget_orientation"])

    def test_fractions_still_sum_to_one(self):
        for fixture in ([FEATURE], [{"id": f"d{i}", "category": "blocking_debt", "severity": 3} for i in range(4)]):
            result = rank(fixture, FIXTURE_POLICY, AS_OF, set())
            self.assertAlmostEqual(sum(e["budget_fraction"] for e in result["partition"]), 1.0, places=5)

    def test_apply_flex_helper_renormalizes(self):
        base = {c: FIXTURE_POLICY[f"budget_{c}"] for c in CATEGORIES}
        for condition in ("clean", "high_debt"):
            flexed = apply_flex(base, condition, FIXTURE_POLICY)
            self.assertAlmostEqual(sum(flexed.values()), 1.0, places=9)


class TestBudgetSplit(unittest.TestCase):
    """The trim order made load-bearing (#246): budget_split apportions the integer budget by the policy's
    shares, and the trim order decides WHICH kinds are shed when the budget cannot seat them all. It is inert
    while the budget fits everything and load-bearing only under overflow; blocking_debt is shed last at the
    shipped (reverse-of-precedence) trim order. FIXTURE_POLICY's shares and trim order match the real defaults."""

    def _applied(self, condition):
        base = {c: FIXTURE_POLICY[f"budget_{c}"] for c in CATEGORIES}
        return apply_flex(base, condition, FIXTURE_POLICY)

    def _trim(self, ranks=None):
        return ranks or {c: FIXTURE_POLICY[f"trim_{c}"] for c in CATEGORIES}

    def test_generous_budget_seats_every_kind_so_trim_is_inert(self):
        # at the cold-start budget the proportional split fits all five in BOTH session conditions, so the
        # trim order never fires — exactly its (dormant) state in a normal session.
        for condition in ("clean", "high_debt"):
            sizes = budget_split(self._applied(condition), 20, self._trim())
            self.assertTrue(all(sizes[c] >= 1 for c in CATEGORIES),
                            f"{condition}: a kind was shed at the cold-start budget: {sizes}")
            self.assertEqual(sum(sizes.values()), 20)

    def test_tight_budget_sheds_least_important_first_keeping_blocking_debt(self):
        # trim rank 1 (orientation) sheds first, then rank 2 (structural_neighbors); blocking_debt (rank 5) is
        # kept. A clean session at 3 slots seats blocking_debt / in_flight / recent_decisions.
        sizes = budget_split(self._applied("clean"), 3, self._trim())
        self.assertEqual(sizes["orientation"], 0)            # rank 1 — shed first
        self.assertEqual(sizes["structural_neighbors"], 0)   # rank 2 — shed next
        self.assertGreaterEqual(sizes["blocking_debt"], 1)   # rank 5 — kept
        self.assertEqual(sum(sizes.values()), 3)

    def test_retuned_trim_changes_what_is_shed(self):
        # THE #246 proof: with the shipped order a tight budget sheds orientation and keeps blocking_debt;
        # REVERSING the trim order sheds blocking_debt instead. Tuning trim_* demonstrably changes the result.
        shipped = budget_split(self._applied("clean"), 3, self._trim())
        reversed_ranks = {c: 6 - FIXTURE_POLICY[f"trim_{c}"] for c in CATEGORIES}
        flipped = budget_split(self._applied("clean"), 3, self._trim(reversed_ranks))
        self.assertEqual(shipped["orientation"], 0)
        self.assertGreaterEqual(shipped["blocking_debt"], 1)
        self.assertEqual(flipped["blocking_debt"], 0)        # now blocking_debt is least important -> shed
        self.assertNotEqual(shipped, flipped)                # the dial governs the outcome

    def test_sizes_sum_exactly_to_total_across_budgets(self):
        for condition in ("clean", "high_debt"):
            for total in range(0, 41):
                sizes = budget_split(self._applied(condition), total, self._trim())
                self.assertEqual(sum(sizes.values()), total, f"{condition} total={total}: {sizes}")

    def test_at_least_one_kind_always_survives(self):
        # a budget of 1 seats exactly one kind — blocking_debt (shed last) under the shipped order.
        sizes = budget_split(self._applied("clean"), 1, self._trim())
        self.assertEqual(sum(1 for c in CATEGORIES if sizes[c] >= 1), 1)
        self.assertEqual(sizes["blocking_debt"], 1)

    def test_budget_size_zero_reaches_the_result_through_rank(self):
        # the public API path: a tight budget_total drives budget_size 0 for the shed kind in the result.
        result = rank([FEATURE], FIXTURE_POLICY, AS_OF, {"state"}, budget_total=3)
        sizes = {e["category"]: e["budget_size"] for e in result["partition"]}
        self.assertEqual(sizes["orientation"], 0)            # shed under the tight budget
        self.assertGreaterEqual(sizes["blocking_debt"], 1)   # kept


class TestDegrade(unittest.TestCase):
    def test_absent_substrates_are_recorded_sorted(self):
        result = rank([FEATURE], FIXTURE_POLICY, AS_OF, {"state"})
        self.assertEqual(result["degraded_inputs"], ["git", "knowledge", "telemetry"])

    def test_telemetry_and_git_are_degraded_when_only_state_knowledge_present(self):
        result = rank([], FIXTURE_POLICY, AS_OF, {"state", "knowledge"})
        self.assertIn("telemetry", result["degraded_inputs"])
        self.assertIn("git", result["degraded_inputs"])

    def test_every_category_is_present_even_when_empty(self):
        result = rank([], FIXTURE_POLICY, AS_OF, set())
        self.assertEqual([e["category"] for e in result["partition"]], list(CATEGORIES))
        self.assertTrue(all(e["members"] == [] for e in result["partition"]))

    def test_result_carries_no_narration_field(self):
        result = rank([FEATURE], FIXTURE_POLICY, AS_OF, set())
        for forbidden in ("message", "warning", "narration", "note"):
            self.assertNotIn(forbidden, result)


def _row(number, severity=None, source_id=None):
    """One row of the live debt register as boot's open_findings projects it (#394)."""
    return {"number": number, "source_id": source_id, "severity": severity}


class TestLiveDebtRegister(unittest.TestCase):
    """assemble_candidates ranks the LIVE telemetry debt register threaded in by boot (`live_findings`): a LIST
    of per-issue rows marks telemetry AVAILABLE and grades EACH open finding into its own blocking-debt
    candidate; None (the offline CLI / a failed read) leaves telemetry degraded and the committed state count
    stands in.

    Per-issue grading is what makes the policy's dials real (#394) — against the previous single aggregate,
    whose severity was pinned EQUAL to the bar, `debt_blocking_threshold` compared itself to itself and
    `flex_high_debt_count` could never see more than one blocking item. These pin that the dials now GOVERN;
    they do NOT assert calibration."""

    _STATE = {"standing_situation": {"phase": "x"},
              "integration_debt": {"open_count": 2, "as_of": "2026-01-01T00:00:00Z"}}

    def _assemble(self, *, live_findings=None, policy=None, shipped=None):
        # find() is mocked so the empty-focus reachability probe (assemble_candidates now reads the map even with
        # no focus) stays hermetic — these cases assert on telemetry, not on the real graph. An empty list means
        # the probe succeeds (a reachable, if empty, map), so knowledge is available and never the variable here.
        with mock.patch.object(attention.validate, "load_json", return_value=dict(self._STATE)), \
                mock.patch.object(attention.work_record, "read_in_flight", return_value=[]), \
                mock.patch.object(attention.knowledge_query, "find", return_value=[]):
            return attention.assemble_candidates(policy or FIXTURE_POLICY, live_findings=live_findings,
                                                 shipped=shipped)

    def _debt(self, cands):
        return [c for c in cands if c["category"] == "blocking_debt"]

    def _surfaced(self, cands, policy=None):
        """The blocking-debt candidates that actually CLEAR the bar — assign_partition drops the rest as
        deferrals (the spec's "open Issues as deferrals/backlog")."""
        pol = policy or FIXTURE_POLICY
        return [c for c in self._debt(cands) if assign_partition(c, pol) == "blocking_debt"]

    def test_live_read_marks_telemetry_available_and_grades_each_finding_separately(self):
        cands, available, _ = self._assemble(live_findings=[_row(7), _row(8), _row(9)])
        self.assertIn("telemetry", available)             # the register WAS read -> no degraded caveat
        debt = self._debt(cands)
        self.assertEqual(len(debt), 3)                    # ONE candidate per open finding (not one aggregate)
        self.assertEqual({c["id"] for c in debt}, {"finding:7", "finding:8", "finding:9"})
        self.assertEqual({c["source"] for c in debt}, {"telemetry"})  # the live register, not the shadow
        # as_of stays the committed cursor's marker — the live read carries no timestamp of its own.
        self.assertEqual({c["recency"] for c in debt}, {"2026-01-01T00:00:00Z"})

    def test_a_benign_finding_is_a_deferral_and_a_trust_critical_one_blocks(self):
        # THE dial made real: severity now DISCRIMINATES instead of every finding riding the bar.
        cands, _, _ = self._assemble(live_findings=[_row(1, telemetry.PERSISTENT_BENIGN),
                                                    _row(2, telemetry.TRUST_CRITICAL)])
        self.assertEqual([c["id"] for c in self._surfaced(cands)], ["finding:2"])  # only the critical blocks
        benign = next(c for c in self._debt(cands) if c["id"] == "finding:1")
        # A benign finding falls BELOW the bar -> assign_partition returns None: it is a deferral/backlog,
        # considered and ordered but not surfaced in the budgeted five.
        self.assertIsNone(assign_partition(benign, FIXTURE_POLICY))

    def test_handed_shipped_rows_are_used_instead_of_reading_the_floor_again(self):
        # Boot needs these same rows again to restore the titles rank() strips. Reading the floor a second
        # time is another `git log` spawn AND a seam: a merge landing between the two reads leaves the digest
        # naming a number whose title it never saw.
        rows = [{"id": "shipped:9", "recency": "2026-07-01T00:00:00Z", "title": "a change"}]
        with mock.patch.object(attention.work_record, "read_recent_decisions") as read:
            cands, _, _ = self._assemble(shipped=rows)
        read.assert_not_called()
        self.assertIn("shipped:9", [c["id"] for c in cands])

    def test_the_floor_is_still_read_when_no_rows_are_handed_over(self):
        # The CLI has no boot to read them for it.
        with mock.patch.object(attention.work_record, "read_recent_decisions", return_value=[]) as read:
            self._assemble()
        read.assert_called_once()

    def test_a_finding_with_no_number_is_dropped_rather_than_rendered_as_none(self):
        # It could not be referenced or acted on, and "#None is open and blocking" is worse than silence.
        cands, _, _ = self._assemble(live_findings=[_row(None, telemetry.TRUST_CRITICAL), _row(2)])
        self.assertEqual([c["id"] for c in self._debt(cands)], ["finding:2"])

    def test_an_ungraded_finding_carries_its_severity_through_as_absent(self):
        # Telemetry owns the class and has not graded this Issue. Attention must pass that through as ABSENT
        # rather than substitute a number: assign_partition says in as many words that attention does not
        # invent what its source did not set. A stand-in equal to the bar is what made the bar compare to
        # itself — the defect this whole slice exists to remove.
        cands, _, _ = self._assemble(live_findings=[_row(4)])
        self.assertIsNone(next(c for c in cands if c["id"] == "finding:4")["severity"])

    def test_an_ungraded_finding_is_a_deferral_at_every_setting_of_the_bar(self):
        # Absent severity is assign_partition's own case: a deferral. It stays one however the dial is tuned,
        # because there is no severity to compare — the operator still sees it in the card's open-problems
        # count ("rather than just being mentioned", the policy's words), it just does not gate the start of
        # work. If tuning the bar ever moved an ungraded finding, attention would be ranking a severity it
        # invented.
        for bar in (0, 1, 2, 99):
            policy = {**FIXTURE_POLICY, "debt_blocking_threshold": bar}
            cands, _, _ = self._assemble(live_findings=[_row(4)], policy=policy)
            self.assertEqual(self._surfaced(cands, policy), [], f"an ungraded finding blocked at bar {bar}")

    def test_lowering_the_bar_surfaces_an_otherwise_deferred_benign_finding(self):
        # Proof the dial GOVERNS: the SAME register, a lower bar -> what was a deferral now blocks. Against the
        # old single aggregate (pinned equal to the bar) no setting of this dial could change any outcome.
        lax = {**FIXTURE_POLICY, "debt_blocking_threshold": 1}
        rows = [_row(1, telemetry.PERSISTENT_BENIGN)]
        self.assertEqual(self._surfaced(self._assemble(live_findings=rows)[0]), [])            # bar 2 -> deferral
        cands, _, _ = self._assemble(live_findings=rows, policy=lax)
        self.assertEqual([c["id"] for c in self._surfaced(cands, lax)], ["finding:1"])         # bar 1 -> blocks

    def test_a_trust_critical_finding_can_never_be_tuned_out_of_blocking(self):
        # The clamp (telemetry.severity_rank): trust-critical means "a safety gate could not run; promotes
        # immediately", so however high an operator tunes the bar it still blocks — the dial can defer ordinary
        # debt, never this class. Without the clamp a bar above the fixed rank would silently drop it.
        strict = {**FIXTURE_POLICY, "debt_blocking_threshold": 10_000}
        cands, _, _ = self._assemble(live_findings=[_row(2, telemetry.TRUST_CRITICAL)], policy=strict)
        self.assertEqual([c["id"] for c in self._surfaced(cands, strict)], ["finding:2"])

    def test_flex_high_debt_count_now_counts_real_blocking_findings(self):
        # The other dead dial: one aggregate capped the blocking count at 1, so a busy session could never be
        # detected whatever the policy said. Per-issue candidates make the count real. The rows are GRADED —
        # the count is of findings that actually block, so only a graded severity can move it.
        few, _, _ = self._assemble(live_findings=[_row(i, telemetry.TRUST_CRITICAL) for i in (1, 2)])
        many, _, _ = self._assemble(live_findings=[_row(i, telemetry.TRUST_CRITICAL) for i in range(1, 6)])
        self.assertEqual(session_condition(few, FIXTURE_POLICY), "clean")       # 2 < flex_high_debt_count (3)
        self.assertEqual(session_condition(many, FIXTURE_POLICY), "high_debt")  # 5 >= 3 -> orientation compresses

    def test_a_register_of_ungraded_findings_never_reads_as_a_busy_session(self):
        # The honest consequence of the absent-severity rule, pinned deliberately: a pile of findings nobody
        # has graded is a backlog, not a busy session. Before, every finding rode the bar, so a register of
        # ungraded findings pinned the session to "busy" forever and orientation was permanently squeezed —
        # the same defect as the bar comparing itself to itself, wearing the other dial's clothes.
        cands, _, _ = self._assemble(live_findings=[_row(i) for i in range(1, 16)])
        self.assertEqual(session_condition(cands, FIXTURE_POLICY), "clean")

    def test_a_benign_backlog_never_trips_the_busy_session_flex(self):
        # Deferrals are not blocking work: a deep BENIGN backlog must not read as a busy session.
        cands, _, _ = self._assemble(live_findings=[_row(i, telemetry.PERSISTENT_BENIGN) for i in range(1, 9)])
        self.assertEqual(session_condition(cands, FIXTURE_POLICY), "clean")

    def test_live_read_of_zero_supersedes_the_committed_count_and_emits_no_debt(self):
        # The register was read and is EMPTY ([]), even though the committed shadow says 2 -> no blocking debt.
        cands, available, _ = self._assemble(live_findings=[])
        self.assertIn("telemetry", available)             # [] still means the read succeeded
        self.assertEqual(self._debt(cands), [])           # the live empty read supersedes the stale committed 2

    def test_no_live_read_leaves_telemetry_degraded_and_uses_the_committed_count(self):
        # The offline CLI / a failed read: live_findings is None -> telemetry degraded, committed count stands in.
        cands, available, _ = self._assemble(live_findings=None)
        self.assertNotIn("telemetry", available)          # telemetry stays degraded -> boot raises the notice
        debt = self._debt(cands)
        self.assertEqual(len(debt), 1)                    # the committed shadow stands in (open_count=2 > 0)
        self.assertEqual(debt[0]["source"], "state")      # sourced from the committed cursor, not the register

    def test_live_assembly_is_deterministic(self):
        # The same inputs (a fixed committed as_of + the same rows) yield byte-identical candidates: the as_of is
        # the committed cursor's, never the clock, so no derive-twice flake.
        rows = [_row(3, telemetry.TRUST_CRITICAL), _row(4, telemetry.PERSISTENT_BENIGN), _row(5)]
        a, _, _ = self._assemble(live_findings=rows)
        b, _, _ = self._assemble(live_findings=rows)
        self.assertEqual(a, b)


class TestResultSchema(unittest.TestCase):
    def test_result_schema_is_well_formed(self):
        # the ONLY well-formedness lock on attention-result.v1 — no live rule targets .engine/schemas/*.json
        validate.Draft202012Validator.check_schema(RESULT_SCHEMA)

    def test_rank_output_conforms(self):
        fixtures = [
            rank([DEBT, FEATURE], FIXTURE_POLICY, AS_OF, {"state"}, budget_total=20),   # headline + sizing
            rank([], FIXTURE_POLICY, AS_OF, set()),                                     # all-empty
            rank([{"id": f"d{i}", "category": "blocking_debt", "severity": 3} for i in range(3)],
                 FIXTURE_POLICY, AS_OF, {"state"}),                                     # flexed high-debt
        ]
        for result in fixtures:
            self.assertEqual(_errors(RESULT_SCHEMA, result), [])

    def test_signals_slot_is_carried_when_present(self):
        result = rank([DEBT, FEATURE], FIXTURE_POLICY, AS_OF, set())
        members = [m for e in result["partition"] for m in e["members"]]
        self.assertTrue(any("signals" in m for m in members), "the optional per-member signals slot is populated")
        self.assertEqual(_errors(RESULT_SCHEMA, result), [])

    def test_schema_has_teeth(self):
        good = rank([FEATURE], FIXTURE_POLICY, AS_OF, set())
        # drop a required top-level field
        bad = copy.deepcopy(good); del bad["degraded_inputs"]
        self.assertNotEqual(_errors(RESULT_SCHEMA, bad), [])
        # a partition that is not exactly five categories
        bad = copy.deepcopy(good); bad["partition"] = bad["partition"][:4]
        self.assertNotEqual(_errors(RESULT_SCHEMA, bad), [])
        # an out-of-vocabulary category
        bad = copy.deepcopy(good); bad["partition"][0]["category"] = "backlog"
        self.assertNotEqual(_errors(RESULT_SCHEMA, bad), [])
        # a malformed as_of
        bad = copy.deepcopy(good); bad["as_of"] = "June 4"
        self.assertNotEqual(_errors(RESULT_SCHEMA, bad), [])
        # a degraded input outside the closed substrate vocabulary
        bad = copy.deepcopy(good); bad["degraded_inputs"] = ["memory"]
        self.assertNotEqual(_errors(RESULT_SCHEMA, bad), [])


class TestPolicyValues(unittest.TestCase):
    """Drift guard: the committed policy carries EXACTLY the value keys the tool reads, structurally well-formed."""

    def setUp(self):
        self.values = validate.frontmatter(POLICY_PATH).get("values", {})

    def test_policy_carries_exactly_the_expected_value_keys(self):
        self.assertEqual(set(self.values.keys()), set(EXPECTED_VALUE_KEYS))

    def test_precedence_ranks_are_a_permutation_of_one_to_five(self):
        self.assertEqual(sorted(self.values[k] for k in attention_rank.PRECEDENCE_KEYS), [1, 2, 3, 4, 5])

    def test_trim_ranks_are_a_permutation_of_one_to_five(self):
        self.assertEqual(sorted(self.values[k] for k in attention_rank.TRIM_KEYS), [1, 2, 3, 4, 5])

    def test_budget_fractions_sum_to_one(self):
        self.assertAlmostEqual(sum(self.values[k] for k in attention_rank.BUDGET_KEYS), 1.0, places=6)

    def test_policy_frontmatter_conforms_to_policy_v1(self):
        self.assertEqual(_errors(POLICY_SCHEMA, validate.frontmatter(POLICY_PATH)), [])


class TestReferenceTime(unittest.TestCase):
    def test_more_recent_ranks_first_within_a_partition(self):
        recent = {"id": "recent", "category": "in_flight", "recency": "2026-06-04T00:00:00Z"}
        old = {"id": "old", "category": "in_flight", "recency": "2026-01-01T00:00:00Z"}
        result = rank([old, recent], FIXTURE_POLICY, AS_OF, set())
        in_flight = next(e for e in result["partition"] if e["category"] == "in_flight")
        self.assertEqual([m["id"] for m in in_flight["members"]], ["recent", "old"])

    def test_as_of_is_an_explicit_reproducible_input(self):
        a = rank([FEATURE], FIXTURE_POLICY, "2026-06-04T00:00:00Z", set())
        b = rank([FEATURE], FIXTURE_POLICY, "2026-06-04T00:00:00Z", set())
        self.assertEqual(a, b)
        self.assertEqual(a["as_of"], "2026-06-04T00:00:00Z")

    # ---- the reference moment must LEAD the work it orders (#394) --------------------------------------
    # The committed cursor lags by design (its own refresh cadence), so a session routinely ranks work that
    # landed after it. These drive the REAL rank() with a lagging moment — the configuration every render
    # test missed by hand-assigning ranks, and the live one.

    def _merges(self):
        return [{"id": f"shipped:{n}", "category": "recent_decisions", "recency": ts} for n, ts in
                (("489", "2026-07-13T11:59:21Z"), ("491", "2026-07-13T12:25:06Z"),
                 ("492", "2026-07-13T12:36:05Z"))]

    def _order(self, as_of):
        result = rank(self._merges(), FIXTURE_POLICY, as_of, {"git"}, budget_total=20)
        entry = next(e for e in result["partition"] if e["category"] == "recent_decisions")
        return [m["id"] for m in entry["members"]]

    def test_a_lagging_moment_would_flatten_newer_work_into_a_tie(self):
        # Why the resolution below exists: age is floored at zero, so everything newer than the reference
        # scores identically and the order falls to the id tiebreak — which renders the digest OLDEST-first.
        weights = {c["id"]: attention_rank.intra_weight(c, FIXTURE_POLICY, "2026-07-12T09:21:38Z")
                   for c in self._merges()}
        self.assertEqual(len(set(weights.values())), 1, "a lagging moment ties every newer merge")
        self.assertEqual(self._order("2026-07-12T09:21:38Z"), ["shipped:489", "shipped:491", "shipped:492"])

    def test_the_resolved_moment_leads_the_pack_so_the_newest_merge_ranks_first(self):
        as_of = attention._reference_moment(self._merges(), "2026-07-12T09:21:38Z")  # the lagging cursor
        self.assertEqual(as_of, "2026-07-13T12:36:05Z", "the newest recorded moment, not the stale cursor")
        self.assertEqual(self._order(as_of), ["shipped:492", "shipped:491", "shipped:489"])

    def test_the_resolved_moment_stays_a_recorded_one_never_the_clock(self):
        # It may only ever be a moment some substrate actually recorded — that is what keeps a run
        # reproducible from its inputs and `as_of_is_wallclock` honest.
        cands = self._merges()
        self.assertIn(attention._reference_moment(cands, "2026-07-12T09:21:38Z"),
                      {c["recency"] for c in cands} | {"2026-07-12T09:21:38Z"})
        self.assertEqual(attention._reference_moment([], "2026-07-12T09:21:38Z"), "2026-07-12T09:21:38Z")
        self.assertIsNone(attention._reference_moment([], None))  # nothing recorded -> the caller's fallback

    def test_the_moment_is_compared_by_absolute_time_not_by_text(self):
        # The recorded sources normalize differently; a lexical max would pick the wrong one here.
        self.assertEqual(attention._reference_moment([{"recency": "2026-07-13T12:36:05-07:00"}],
                                                     "2026-07-13T13:00:00Z"), "2026-07-13T12:36:05-07:00")

    def test_a_malformed_moment_costs_its_own_ordering_never_the_reference(self):
        self.assertEqual(attention._reference_moment([{"recency": "not-a-date"}], "2026-07-12T09:21:38Z"),
                         "2026-07-12T09:21:38Z")

    def test_the_live_path_resolves_the_leading_moment_and_ships_newest_first(self):
        # The wiring, not just the helper: rank_live must RESOLVE the reference moment, or the fix is
        # orphaned and the live digest stays inverted.
        with mock.patch.object(attention, "assemble_candidates",
                               return_value=(self._merges(), {"git"}, "2026-07-12T09:21:38Z")):
            result = attention.rank_live(budget_total=20)
        self.assertEqual(result["as_of"], "2026-07-13T12:36:05Z")
        self.assertFalse(result["as_of_is_wallclock"], "a recorded moment is not a clock read")
        entry = next(e for e in result["partition"] if e["category"] == "recent_decisions")
        self.assertEqual([m["id"] for m in entry["members"]],
                         ["shipped:492", "shipped:491", "shipped:489"])

    def test_the_live_path_still_falls_back_to_the_clock_when_nothing_recorded_a_moment(self):
        with mock.patch.object(attention, "assemble_candidates", return_value=([], {"git"}, None)):
            result = attention.rank_live(budget_total=20)
        self.assertTrue(result["as_of_is_wallclock"])


ATTENTION_STRUCTURAL = set(PRECEDENCE_KEYS) | set(TRIM_KEYS)


class TestTheMergeRefusesAValueTheEngineCannotMeasure(unittest.TestCase):
    """The read-time merge is the ONLY gate every reader passes through, so it is where a dial the engine
    cannot measure against has to be refused (#394, deliverable gate).

    The tuning command checks what it writes, but the override is a committed file an operator can hand-edit
    and JSON round-trips the non-standard `Infinity`/`NaN` literals — so the write-side check alone leaves
    the door open, and what comes through it is not cosmetic."""

    def _bar(self, override):
        return attention.load_policy_values(override=override)["debt_blocking_threshold"]

    def test_an_endless_bar_can_no_longer_drop_a_safety_check_that_could_not_run(self):
        # THE one that matters. Every severity is below an endless bar, so a trust-critical finding — "a
        # safety gate could not run" — would silently leave blocking debt while still being counted, and the
        # operator would see the number but never the item. The clamp cannot save it; refusing the bar does.
        shipped = self._bar(None)
        self.assertEqual(self._bar({"debt_blocking_threshold": float("inf")}), shipped)
        policy = attention.load_policy_values(override={"debt_blocking_threshold": float("inf")})
        sev = telemetry.severity_rank(telemetry.TRUST_CRITICAL, policy["debt_blocking_threshold"])
        self.assertEqual(assign_partition({"id": "x", "category": "blocking_debt", "severity": sev}, policy),
                         "blocking_debt")

    def test_not_a_number_can_no_longer_invert_the_bar(self):
        # NaN compares false against everything, so it blocks what should pass.
        self.assertEqual(self._bar({"debt_blocking_threshold": float("nan")}), self._bar(None))

    def test_a_non_numeric_dial_costs_its_own_value_not_the_whole_ranking(self):
        # It used to reach float() deep in the grading and raise, which boot catches by dropping the ENTIRE
        # ranked pack for the session — one bad character in operator config, no priorities at all.
        self.assertEqual(self._bar({"debt_blocking_threshold": "high"}), self._bar(None))
        result = attention.rank_live(override={"debt_blocking_threshold": "high"}, budget_total=20)
        self.assertTrue(result["partition"], "a non-numeric dial destroyed the ranked pack")

    def test_a_bool_is_not_a_threshold(self):
        self.assertEqual(self._bar({"debt_blocking_threshold": True}), self._bar(None))

    def test_the_refusal_is_surfaced_rather_than_swallowed(self):
        _effective, findings = validate.effective_policy_values(
            {"debt_blocking_threshold": 2}, {"debt_blocking_threshold": float("inf")},
            structural_keys=set(), tier="soft", message="m")
        self.assertEqual(len(findings), 1)
        self.assertIn("not an ordinary number", findings[0]["message"])

    def test_an_ordinary_number_still_merges(self):
        self.assertEqual(self._bar({"debt_blocking_threshold": 3}), 3)
        self.assertEqual(self._bar({"debt_blocking_threshold": 0}), 0)


class TestEffectiveValues(unittest.TestCase):
    """Issue #42 — the operator policy-override read-time merge. Exercises the REAL core merge
    (validate.effective_policy_values) and the REAL consumer (attention.load_policy_values). The merge is
    static-data only, so determinism is preserved and the structural ordering cannot be out-tuned. No live
    rule exists yet (the stale-key rule on the committed override file is wired later); these fixtures
    plus the operator demo are the merge's only proof. The eligibility partition: every attention value is
    override-eligible EXCEPT the structural precedence/trim keys (Shane's flex-is-tunable leaf — flex merges)."""

    def _merge(self, override, *, structural=ATTENTION_STRUCTURAL):
        return validate.effective_policy_values(FIXTURE_POLICY, override, structural_keys=structural,
                                                tier="soft", message="")

    def test_sparse_override_merges_per_key(self):
        effective, findings = self._merge({"budget_orientation": 0.40, "weight_recency": 0.9})
        self.assertEqual(findings, [])
        self.assertEqual(effective["budget_orientation"], 0.40)
        self.assertEqual(effective["weight_recency"], 0.9)
        for k, v in FIXTURE_POLICY.items():           # every unnamed key keeps the shipped default
            if k not in ("budget_orientation", "weight_recency"):
                self.assertEqual(effective[k], v)

    def test_partial_override_leaves_unset_keys_at_default(self):
        effective, findings = self._merge({"weight_severity": 2.0})
        self.assertEqual(findings, [])
        self.assertEqual(effective["weight_severity"], 2.0)
        self.assertEqual({k: effective[k] for k in FIXTURE_POLICY if k != "weight_severity"},
                         {k: v for k, v in FIXTURE_POLICY.items() if k != "weight_severity"})

    def test_empty_override_returns_default_unchanged(self):
        effective, findings = self._merge({})
        self.assertEqual(effective, FIXTURE_POLICY)
        self.assertEqual(findings, [])

    def test_stale_key_falls_back_and_is_surfaced(self):
        effective, findings = self._merge({"budget_legacy_thing": 0.1})
        self.assertNotIn("budget_legacy_thing", effective)   # falls back: never enters the effective map
        self.assertEqual(effective, FIXTURE_POLICY)          # everything else is the default
        self.assertEqual(len(findings), 1)
        self.assertIn("budget_legacy_thing", findings[0]["message"])

    def test_ineligible_precedence_key_refused_at_the_merge_layer(self):
        # the law guard asserted at the MERGE layer (not only via the downstream rank): a structural key is
        # refused and the shipped default value stands in the effective map.
        effective, findings = self._merge({"precedence_blocking_debt": 5})
        self.assertEqual(effective["precedence_blocking_debt"], FIXTURE_POLICY["precedence_blocking_debt"])
        self.assertEqual(len(findings), 1)
        self.assertIn("precedence_blocking_debt", findings[0]["message"])

    def test_ineligible_trim_key_refused(self):
        effective, findings = self._merge({"trim_orientation": 9})
        self.assertEqual(effective["trim_orientation"], FIXTURE_POLICY["trim_orientation"])
        self.assertEqual(len(findings), 1)

    def test_flex_keys_are_eligible_and_merge(self):
        # Shane's leaf: the two flex dials ARE tunable (only precedence/trim are structural).
        effective, findings = self._merge({"flex_orientation_delta": 0.25, "flex_high_debt_count": 7})
        self.assertEqual(findings, [])
        self.assertEqual(effective["flex_orientation_delta"], 0.25)
        self.assertEqual(effective["flex_high_debt_count"], 7)

    def test_eligibility_partition_over_every_key(self):
        # comprehensive: every non-structural key merges with no finding; every structural key is refused.
        for k in FIXTURE_POLICY:
            effective, findings = self._merge({k: 99})
            if k in ATTENTION_STRUCTURAL:
                self.assertEqual(effective[k], FIXTURE_POLICY[k], f"{k} must be refused (structural)")
                self.assertEqual(len(findings), 1, f"{k} must surface a finding")
            else:
                self.assertEqual(effective[k], 99, f"{k} must merge (eligible)")
                self.assertEqual(findings, [], f"{k} must merge cleanly")

    def test_determinism_same_inputs_byte_identical(self):
        override = {"budget_orientation": 0.4, "trim_orientation": 9, "stale": 1}
        self.assertEqual(json.dumps(self._merge(override), sort_keys=True),
                         json.dumps(self._merge(override), sort_keys=True))
        # order-independent: a differently-ordered override yields the identical effective map AND the
        # identical finding order — the merge iterates `sorted(override)`, so dict insertion order never leaks.
        eff_a, find_a = self._merge(override)
        eff_b, find_b = self._merge({"stale": 1, "trim_orientation": 9, "budget_orientation": 0.4})
        self.assertEqual(eff_a, eff_b)
        self.assertEqual([f["message"] for f in find_a], [f["message"] for f in find_b])

    def test_findings_are_finding_v1_shape(self):
        _effective, findings = self._merge({"precedence_blocking_debt": 5, "stale_key": 1})
        self.assertEqual(len(findings), 2)
        for f in findings:
            self.assertEqual(set(f.keys()), {"severity", "message", "location"})
            self.assertEqual(f["severity"], "soft")

    def test_precedence_override_cannot_reorder_the_partition(self):
        # end-to-end law guard: an override that tries to demote blocking debt below in-flight is refused, so
        # rank() over the same fixture STILL leads with blocking debt — structural, not out-tunable.
        effective, findings = self._merge({"precedence_blocking_debt": 5, "precedence_in_flight": 1})
        self.assertEqual(len(findings), 2)   # both structural keys refused
        flat = _flatten(rank([FEATURE, DEBT], effective, AS_OF, set()))
        self.assertLess(flat.index("debt:overdue"), flat.index("feat:shiny"))

    def test_load_policy_values_no_override_matches_shipped_default(self):
        # the regression lock: the live path (no override) returns the committed default bit-for-bit.
        shipped = validate.frontmatter(POLICY_PATH).get("values", {})
        self.assertEqual(attention.load_policy_values(), shipped)
        self.assertEqual(attention.load_policy_values(POLICY_PATH, None), shipped)

    def test_load_policy_values_with_override_returns_effective(self):
        shipped = validate.frontmatter(POLICY_PATH).get("values", {})
        effective = attention.load_policy_values(POLICY_PATH, {"budget_orientation": 0.40})
        self.assertEqual(effective["budget_orientation"], 0.40)
        for k, v in shipped.items():
            if k != "budget_orientation":
                self.assertEqual(effective[k], v)
        # a structural override through the consumer is still refused (the default stands)
        effective2 = attention.load_policy_values(POLICY_PATH, {"trim_blocking_debt": 1})
        self.assertEqual(effective2["trim_blocking_debt"], shipped["trim_blocking_debt"])

    def test_rank_live_threads_override_to_the_merge(self):
        # The live seam: boot reads the operator override and hands attention's slice to
        # rank_live, which must forward it to the per-key merge — so a tuned value reaches the live ranking.
        captured = {}
        original = attention.load_policy_values

        def spy(policy_path=POLICY_PATH, override=None):
            captured["override"] = override
            return original(policy_path, override)

        attention.load_policy_values = spy
        try:
            attention.rank_live(override={"budget_orientation": 0.40})
        finally:
            attention.load_policy_values = original
        self.assertEqual(captured["override"], {"budget_orientation": 0.40},
                         "rank_live forwards the operator override to the per-key merge")


class TestDeriveFocus(unittest.TestCase):
    """derive_focus maps the in-flight changed files to their owning graph entities — the work-in-hand focus
    the orientation-time focused read keys on (#37). The git runner and the graph are injected/mocked."""

    def _patch(self, paths, entities):
        return (mock.patch.object(attention.work_record, "changed_paths", return_value=paths),
                mock.patch.object(attention.knowledge_query, "find", return_value=entities))

    def test_maps_changed_files_to_owning_entities_exactly(self):
        entities = [{"source_path": ".engine/tools/attention.py", "id": "tool:attention"},
                    {"source_path": ".engine/tools/boot.py", "id": "tool:boot"}]
        p1, p2 = self._patch([".engine/tools/attention.py", ".engine/tools/boot.py"], entities)
        with p1, p2:
            self.assertEqual(attention.derive_focus(run=lambda a: None), ["tool:attention", "tool:boot"])

    def test_skips_paths_that_own_no_entity(self):
        # a non-surface file (root README) owns no entity -> silently skipped, never guessed
        entities = [{"source_path": ".engine/tools/attention.py", "id": "tool:attention"}]
        p1, p2 = self._patch(["README.md", ".engine/tools/attention.py"], entities)
        with p1, p2:
            self.assertEqual(attention.derive_focus(run=lambda a: None), ["tool:attention"])

    def test_excludes_test_and_demo_entities(self):
        entities = [{"source_path": ".engine/tools/attention.py", "id": "tool:attention"},
                    {"source_path": ".engine/tools/test_attention.py", "id": "tool:test_attention"},
                    {"source_path": ".engine/tools/demo_x.py", "id": "tool:demo_x"}]
        paths = [e["source_path"] for e in entities]
        p1, p2 = self._patch(paths, entities)
        with p1, p2:
            self.assertEqual(attention.derive_focus(run=lambda a: None), ["tool:attention"])

    def test_distinct_and_capped_in_stable_order(self):
        entities = [{"source_path": f".engine/tools/t{i}.py", "id": f"tool:t{i}"} for i in range(10)]
        p1, p2 = self._patch([e["source_path"] for e in entities], entities)
        with p1, p2:
            self.assertEqual(attention.derive_focus(run=lambda a: None, cap=3),
                             ["tool:t0", "tool:t1", "tool:t2"])

    def test_no_changed_paths_is_empty(self):
        p1, p2 = self._patch([], [{"source_path": "x", "id": "tool:x"}])
        with p1, p2:
            self.assertEqual(attention.derive_focus(run=lambda a: None), [])

    def test_find_failure_degrades_to_empty(self):
        with mock.patch.object(attention.work_record, "changed_paths",
                               return_value=[".engine/tools/attention.py"]), \
             mock.patch.object(attention.knowledge_query, "find",
                               side_effect=Exception("knowledge unavailable")):
            self.assertEqual(attention.derive_focus(run=lambda a: None), [])

    def test_lazy_run_default_never_crashes_when_work_record_absent(self):
        # the run default is LAZY (run=None), so calling derive_focus() with the guarded work_record import
        # degraded to None returns [] cleanly — never an AttributeError at call (or import) time.
        with mock.patch.object(attention, "work_record", None):
            self.assertEqual(attention.derive_focus(), [])

    def test_with_total_reports_count_behind_the_cap_and_keeps_list_default(self):
        # opt-in `with_total` returns (capped_focus, true_total) so the render can disclose focus truncation
        # (#165); the default (no with_total) is unchanged — a bare list — so existing callers are unaffected.
        entities = [{"source_path": f".engine/tools/t{i}.py", "id": f"tool:t{i}"} for i in range(7)]
        capped = [f"tool:t{i}" for i in range(5)]
        p1, p2 = self._patch([e["source_path"] for e in entities], entities)
        with p1, p2:
            self.assertEqual(attention.derive_focus(run=lambda a: None, cap=5), capped)   # default: bare list
            focus, total = attention.derive_focus(run=lambda a: None, cap=5, with_total=True)
        self.assertEqual(focus, capped)                                  # the capped focus is identical
        self.assertEqual(total, 7)                                       # AND the TRUE count behind the cap

    def test_with_total_respects_the_pair_shape_on_the_degrade_paths(self):
        # fail-open must respect the return shape: with_total -> ([], 0), never a bare [] that crashes unpacking.
        p1, p2 = self._patch([], [{"source_path": "x", "id": "tool:x"}])      # no changed paths -> empty
        with p1, p2:
            self.assertEqual(attention.derive_focus(run=lambda a: None, with_total=True), ([], 0))
        with mock.patch.object(attention, "work_record", None):              # substrate absent -> empty
            self.assertEqual(attention.derive_focus(with_total=True), ([], 0))


class TestFocusSetWalk(unittest.TestCase):
    """assemble_candidates walks a focus SET BIDIRECTIONALLY: it asks knowledge for each focus
    member's neighbours in BOTH directions, dedupes across members and across edges, and excludes focus
    members themselves (co-changed entities are not each other's structural neighbors). Graph + work record
    are mocked here; the real `direction="both"` SQL is covered in test_knowledge_query."""

    def test_set_focus_dedupes_neighbors_and_excludes_focus_members(self):
        # The mock signature MUST accept `direction` — assemble_candidates now passes direction="both".
        def fake_neighbors(fid, edge_filter=None, depth=1, direction="out"):
            return {"tool:a": [{"id": "tool:b"}, {"id": "mod:x"}],   # tool:b is itself a focus member
                    "tool:b": [{"id": "tool:a"}, {"id": "mod:x"}]}.get(fid, [])
        with mock.patch.object(attention.work_record, "read_in_flight", return_value=[]), \
                mock.patch.object(attention.knowledge_query, "neighbors",
                                  side_effect=fake_neighbors) as walk:
            cands, available, _ = attention.assemble_candidates(
                FIXTURE_POLICY, state_path="/nonexistent", focus=["tool:a", "tool:b"], gh=None)
        sn = [c["id"] for c in cands if c["category"] == "structural_neighbors"]
        self.assertEqual(sn, ["mod:x"])             # mod:x once; tool:a / tool:b excluded (focus members)
        self.assertIn("knowledge", available)
        for call in walk.call_args_list:            # the bidirectional pin lives at attention's call site
            self.assertEqual(call.kwargs.get("direction"), "both")

    def test_walk_dedupes_a_neighbour_reached_by_two_edges(self):
        # `direction="both"` can return the SAME id twice from one focus member (e.g. reached via reverse
        # provided_by AND reverse targets); the `seen` dedup collapses it to a single candidate.
        def fake_neighbors(fid, edge_filter=None, depth=1, direction="out"):
            return [{"id": "check:x", "predicate": "provided_by", "direction": "in"},
                    {"id": "check:x", "predicate": "targets", "direction": "in"}]
        with mock.patch.object(attention.work_record, "read_in_flight", return_value=[]), \
                mock.patch.object(attention.knowledge_query, "neighbors", side_effect=fake_neighbors):
            cands, available, _ = attention.assemble_candidates(
                FIXTURE_POLICY, state_path="/nonexistent", focus="module:y", gh=None)
        sn = [c["id"] for c in cands if c["category"] == "structural_neighbors"]
        self.assertEqual(sn, ["check:x"])           # one candidate despite two edge-rows
        self.assertIn("knowledge", available)

    def test_single_string_focus_still_walks(self):
        with mock.patch.object(attention.work_record, "read_in_flight", return_value=[]), \
                mock.patch.object(attention.knowledge_query, "neighbors",
                                  return_value=[{"id": "mod:x"}]) as walk:
            cands, available, _ = attention.assemble_candidates(
                FIXTURE_POLICY, state_path="/nonexistent", focus="tool:a", gh=None)
        sn = [c["id"] for c in cands if c["category"] == "structural_neighbors"]
        self.assertEqual(sn, ["mod:x"])
        self.assertIn("knowledge", available)
        self.assertEqual(walk.call_args.kwargs.get("direction"), "both")


class TestNeighborhoodOf(unittest.TestCase):
    """neighborhood_of returns the per-(member, relationship) SUMMARY the orientation render needs: each focus
    member's BIDIRECTIONAL neighbours grouped by (predicate, direction), with the FULL count + a bounded
    sample — so the render can disclose truncation honestly rather than show an arbitrary capped few.
    The graph is mocked here; the real `direction="both"` SQL is covered in test_knowledge_query."""

    def test_groups_by_relationship_and_walks_both_directions(self):
        def fake(fid, edge_filter=None, depth=1, direction="out"):
            return [{"id": "module:core", "predicate": "provided_by", "direction": "out"},
                    {"id": "check:policy-frontmatter", "predicate": "targets", "direction": "in"},
                    {"id": "check:policy-shape", "predicate": "targets", "direction": "in"}]
        with mock.patch.object(attention.knowledge_query, "neighbors", side_effect=fake) as walk:
            nb = attention.neighborhood_of(["policy:attention"])
        self.assertEqual(nb["focus"], ["policy:attention"])
        by_rel = {(g["predicate"], g["direction"]): g for g in nb["groups"]}
        self.assertEqual(by_rel[("provided_by", "out")]["total"], 1)
        self.assertEqual(by_rel[("provided_by", "out")]["sample"], ["module:core"])
        self.assertEqual(by_rel[("targets", "in")]["total"], 2)
        self.assertEqual(by_rel[("targets", "in")]["sample"],
                         ["check:policy-frontmatter", "check:policy-shape"])
        for call in walk.call_args_list:                    # the bidirectional pin lives at the call site
            self.assertEqual(call.kwargs.get("direction"), "both")

    def test_excludes_focus_members_from_their_own_neighbourhood(self):
        def fake(fid, edge_filter=None, depth=1, direction="out"):
            return {"tool:a": [{"id": "tool:b", "predicate": "depends_on", "direction": "out"},
                               {"id": "module:core", "predicate": "provided_by", "direction": "out"}],
                    "tool:b": []}.get(fid, [])
        with mock.patch.object(attention.knowledge_query, "neighbors", side_effect=fake):
            nb = attention.neighborhood_of(["tool:a", "tool:b"])
        ids = [n for g in nb["groups"] for n in g["sample"]]
        self.assertNotIn("tool:b", ids)         # a co-changed focus member is not its own structural neighbour
        self.assertIn("module:core", ids)

    def test_full_count_preserved_when_sample_is_capped(self):
        many = [{"id": f"tool:t{i:02d}", "predicate": "provided_by", "direction": "in"} for i in range(30)]
        with mock.patch.object(attention.knowledge_query, "neighbors", return_value=many):
            nb = attention.neighborhood_of(["module:core"])
        g = nb["groups"][0]
        self.assertEqual(g["total"], 30)                                 # the TRUE count is kept for the render
        self.assertEqual(len(g["sample"]), attention.NEIGHBORHOOD_SAMPLE_CAP)   # only a bounded sample is shown
        self.assertEqual(g["sample"], [f"tool:t{i:02d}" for i in range(attention.NEIGHBORHOOD_SAMPLE_CAP)])

    def test_groups_in_deterministic_order_forward_edges_first(self):
        # neighbours arrive reverse-first; groups must still sort by the pinned edge order, forward before reverse.
        def fake(fid, edge_filter=None, depth=1, direction="out"):
            return [{"id": "x:1", "predicate": "targets", "direction": "in"},
                    {"id": "x:2", "predicate": "provided_by", "direction": "out"}]
        with mock.patch.object(attention.knowledge_query, "neighbors", side_effect=fake):
            nb = attention.neighborhood_of(["m:a"])
        self.assertEqual([(g["predicate"], g["direction"]) for g in nb["groups"]],
                         [("provided_by", "out"), ("targets", "in")])
        self.assertNotIn("_order", nb["groups"][0])          # the internal sort key is popped, never leaked

    def test_fail_open_returns_none(self):
        self.assertIsNone(attention.neighborhood_of(None))
        self.assertIsNone(attention.neighborhood_of([]))
        with mock.patch.object(attention.knowledge_query, "neighbors",
                               side_effect=Exception("knowledge unavailable")):
            self.assertIsNone(attention.neighborhood_of(["tool:a"]))


class _FakeSource:
    """A stand-in for boot_slice's read-shim: exposes the same find()/neighbors() + edge vocabulary the
    orientation reads call, so the `source=` seam is provable WITHOUT the real graph. (The order-faithful
    real-graph parity and the byte-identical render live in test_boot_slice / test_boot.)"""
    # Mirror the REAL walk/edge vocabulary so this stand-in can never drift from production (the shim it fakes
    # carries the same constants). Reading them from the module under test keeps the fake faithful as the
    # vocabulary grows.
    WALK_EDGE_KINDS = attention.knowledge_query.WALK_EDGE_KINDS
    EDGE_KINDS = attention.knowledge_query.EDGE_KINDS

    def __init__(self, by_path=None, adjacency=None):
        self._by_path = by_path or {}
        self._adjacency = adjacency or {}

    def find(self):
        return [{"source_path": p, "id": i} for p, i in self._by_path.items()]

    def neighbors(self, entity_id, edge_filter=None, depth=1, direction="out"):
        return list(self._adjacency.get(entity_id, []))


class TestSliceSourceInjection(unittest.TestCase):
    """The opt-in `source=` (boot's rung-1 boot-slice read-shim, #37) makes the orientation reads consult the
    injected source instead of the knowledge_query module — one code path, default off so the CLI is unchanged."""

    def test_derive_focus_reads_the_injected_source_not_knowledge_query(self):
        src = _FakeSource(by_path={".engine/tools/attention.py": "tool:attention"})
        with mock.patch.object(attention.work_record, "changed_paths",
                               return_value=[".engine/tools/attention.py"]), \
             mock.patch.object(attention.knowledge_query, "find",
                               side_effect=AssertionError("knowledge_query.find must NOT be consulted")):
            self.assertEqual(attention.derive_focus(run=lambda a: None, source=src), ["tool:attention"])

    def test_neighborhood_of_reads_the_injected_source(self):
        src = _FakeSource(adjacency={"policy:attention": [
            {"id": "module:core", "predicate": "provided_by", "direction": "out"},
            {"id": "check:policy-shape", "predicate": "targets", "direction": "in"}]})
        with mock.patch.object(attention.knowledge_query, "neighbors",
                               side_effect=AssertionError("knowledge_query.neighbors must NOT be consulted")):
            nb = attention.neighborhood_of(["policy:attention"], source=src)
        by_rel = {(g["predicate"], g["direction"]): g for g in nb["groups"]}
        self.assertEqual(by_rel[("provided_by", "out")]["sample"], ["module:core"])
        self.assertEqual(by_rel[("targets", "in")]["sample"], ["check:policy-shape"])

    def test_assemble_candidates_reads_the_injected_source_and_records_knowledge_available(self):
        src = _FakeSource(adjacency={"tool:a": [{"id": "module:core", "predicate": "provided_by",
                                                 "direction": "out"}]})
        with mock.patch.object(attention.work_record, "read_in_flight", return_value=[]), \
             mock.patch.object(attention.knowledge_query, "neighbors",
                               side_effect=AssertionError("knowledge_query.neighbors must NOT be consulted")):
            cands, available, _ = attention.assemble_candidates(
                FIXTURE_POLICY, state_path="/nonexistent", focus="tool:a", gh=None, source=src)
        sn = [c["id"] for c in cands if c["category"] == "structural_neighbors"]
        self.assertEqual(sn, ["module:core"])
        self.assertIn("knowledge", available)        # a slice read counts as knowledge available

    def test_source_works_even_when_the_knowledge_query_import_degraded(self):
        # NIT-1 robustness: slice present => `src` is truthy even if knowledge_query is None, so orientation
        # still reads knowledge (and the WALK_EDGE_KINDS reference comes off `src`, never the absent module).
        src = _FakeSource(by_path={".engine/tools/boot.py": "tool:boot"},
                          adjacency={"policy:attention": [{"id": "module:core", "predicate": "provided_by",
                                                           "direction": "out"}]})
        with mock.patch.object(attention, "knowledge_query", None), \
             mock.patch.object(attention.work_record, "changed_paths",
                               return_value=[".engine/tools/boot.py"]):
            self.assertEqual(attention.derive_focus(run=lambda a: None, source=src), ["tool:boot"])
            nb = attention.neighborhood_of(["policy:attention"], source=src)
        self.assertEqual(nb["groups"][0]["sample"], ["module:core"])


class TestEmptyFocusKnowledgeAvailability(unittest.TestCase):
    """The clean-worktree false-alarm fix: knowledge AVAILABILITY tracks whether the map could be READ, not
    whether there was a focus to walk it from. A clean session (no work in hand -> empty focus) must NOT degrade
    knowledge — the map is reachable, so boot does not raise the false "couldn't reach your project map" notice;
    a GENUINELY unreachable map still degrades, so the real notice is preserved."""

    def test_empty_focus_with_a_reachable_source_is_available_and_walks_nothing(self):
        # The fix's positive case (the real boot path): boot passes its rung-1 slice as `source`. With no focus,
        # the walk must NOT run (no structural_neighbors candidate), yet knowledge IS available (the map was
        # reached) — the inverse of the bug, which left knowledge degraded on every clean session.
        src = _FakeSource(by_path={".engine/tools/attention.py": "tool:attention"})
        with mock.patch.object(attention.work_record, "read_in_flight", return_value=[]), \
             mock.patch.object(attention.knowledge_query, "neighbors",
                               side_effect=AssertionError("no focus => the walk must NOT run")):
            for empty in (None, []):
                cands, available, _ = attention.assemble_candidates(
                    FIXTURE_POLICY, state_path="/nonexistent", focus=empty, gh=None, source=src)
                self.assertIn("knowledge", available)                                  # reachable -> available
                self.assertEqual([c for c in cands if c["category"] == "structural_neighbors"], [])  # no walk

    def test_empty_focus_stays_degraded_when_the_map_is_genuinely_unreachable(self):
        # The fix's safety case: boot's slice read failed (source=None) so the empty-focus probe falls to
        # knowledge_query.find(); when THAT raises (rung 4 — committed graph absent AND the live walk also
        # fails), knowledge stays degraded so boot's real "couldn't reach your project map" notice still fires.
        with mock.patch.object(attention.work_record, "read_in_flight", return_value=[]), \
             mock.patch.object(attention.knowledge_query, "find",
                               side_effect=Exception("knowledge unavailable")):
            cands, available, _ = attention.assemble_candidates(
                FIXTURE_POLICY, state_path="/nonexistent", focus=None, gh=None, source=None)
        self.assertNotIn("knowledge", available)                                       # unreachable -> degraded
        self.assertEqual([c for c in cands if c["category"] == "structural_neighbors"], [])


if __name__ == "__main__":
    unittest.main()
