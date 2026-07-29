#!/usr/bin/env python3
"""Stale-saved-setting check — surfaces, in plain operator language, a saved tuning value
that will not apply.

The `/engine-tune` command saves per-deployment tuning values that supersede the shipped policy defaults.
After an engine update a saved value can stop applying: the policy no longer carries that setting
(renamed or removed), the setting is a fixed one that can't be changed, or — only if the file was hand-edited
past the command — the value is not a number. In each case the engine uses its own default instead. This
rule runs at the merge gate and surfaces each such saved setting so it is caught rather than quietly
forgotten (the freshly-stale catch at validation; a lingering one is the audit's job).

It surfaces in PLAIN operator language: the live MERGE that actually applies the override is
`validate.effective_policy_values`, consumed by attention's `load_policy_values` (never re-implemented) — but
that merge's per-key message is maintainer-register ("Override key … structural-law value …"), which must not
reach the operator at the merge gate (the validation plain-language law — keep the engine's internal machinery
out of operator view, the same judgment the tune tool's own copy is written to). So this rule classifies each
saved setting against the SAME inputs
the merge is given — the policy's shipped defaults and the consumer's structural-key set — and writes its own
plain sentence. It is a `custom/script` rule: it prints the finding.v1 array on stdout and returns 0 (the
`skill_coherence_check` pattern). With no saved-settings file (the normal state until the operator first
tunes) it surfaces nothing. The per-policy structural-key map is replicated from `tune.py` to keep this
validator independent of the verb tool — a shared tuning-helper floor could absorb it when a third consumer needs it
(the `_typed_name` precedent).
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate            # noqa: E402  (the finding constructor + frontmatter reader)
import operator_overrides  # noqa: E402  (the committed saved-settings reader)
import attention_rank      # noqa: E402  (the structural-key CONSTANTS only)

# Per-policy structural keys: a value encoding a LAW an override may never retune. attention owns its
# partition precedence + trim order; the threshold policies own none. (Replicated from tune.py; see header.)
_STRUCTURAL = {"attention": frozenset(attention_rank.PRECEDENCE_KEYS) | frozenset(attention_rank.TRIM_KEYS)}

_FILE = ".engine/operator-overrides.json"
_STALE = ("A saved setting no longer exists: “{key}” isn't one of the engine's settings anymore, "
          "so the engine is using its own value for it instead. Type /engine-tune to set a setting the "
          f"engine still has, or remove this one from your saved settings ({_FILE}).")
# The same situation when the engine KNOWS why the setting went away (operator_overrides.RETIRED). It names
# the reason instead of leaving the operator to guess at a change they did not make, and points at the
# one-step clear rather than at hand-editing a committed file. An engine update normally removes the entry
# before this can fire, so reaching this line means the value outlived its retirement some other way.
_RETIRED = ("A setting you saved has been retired: “{key}” — {reason}. The engine is using its own value for "
            "it instead, so nothing is behaving unexpectedly. An engine update normally clears this out for "
            f"you; to clear it now, type /engine-tune forget {{key}}, or remove it from {_FILE}.")
_FIXED = ("A saved setting can't be changed: “{key}” is structural — it encodes part of the engine's safety "
          "order, so the engine is ignoring the saved value. Type /engine-tune to change a setting you can, "
          f"or remove this one from your saved settings ({_FILE}).")
_NOTNUM = ("A saved setting isn't a number: “{key}” is set to something that isn't a number, so the "
           f"engine is using its own value for it instead. Type /engine-tune to set it again, or fix it in "
           f"your saved settings ({_FILE}).")


def _default_values(policy_id: str) -> dict:
    """The shipped default tuning values of `policy_id`, or `{}` when the policy / its values are absent."""
    path = os.path.join(validate.ENGINE_DIR, "policies", f"{policy_id}.md")
    return (validate.frontmatter(path).get("values", {}) or {}) if os.path.isfile(path) else {}


def findings(tier: str, override: dict | None = None) -> list:
    """The finding.v1 list for the committed (or supplied) saved settings: one plain-language finding per
    saved value that will not apply — a fixed (structural) setting, a setting the policy no longer carries,
    or a non-number value. Empty when there is no saved-settings file or every saved value applies cleanly.
    `override` is injectable for tests/demo; by default the committed file is read."""
    data = operator_overrides.load() if override is None else override
    out = []
    for policy_id, slice_ in sorted(data.items()):
        default = _default_values(policy_id)
        structural = set(_STRUCTURAL.get(policy_id, frozenset()))
        for key in sorted(slice_):
            value = slice_[key]
            if key in structural:
                out.append(validate.finding(tier, _FIXED.format(key=key)))
            elif key not in default:
                # A RECORDED retirement gets the reason and the one-step clear; anything else the engine has
                # no record of retiring keeps the generic line, rather than a reason invented to fill a gap.
                reason = operator_overrides.retirement_reason(policy_id, key)
                out.append(validate.finding(
                    tier,
                    _RETIRED.format(key=key, reason=reason) if reason else _STALE.format(key=key)))
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                out.append(validate.finding(tier, _NOTNUM.format(key=key)))
            # else: a current, eligible, numeric setting — it applies, so nothing to surface.
    return out


def emit(fs: list) -> int:
    """Write the finding.v1 array to stdout (the custom/script machine channel) and return 0 — a successful
    evaluation, whatever it found. Human-readable prose lives inside each finding's `message`."""
    print(json.dumps(fs))
    return 0


def _demo() -> int:
    """Show the check over a planted saved-settings example that has gone out of date — nothing on disk is
    touched. It plants one stale setting (the engine no longer has it) and one fixed setting, and prints what
    the operator would see at the merge gate."""
    planted = {
        "triage-threshold": {"persistence": 5, "a_setting_that_was_removed": 9},
        "attention": {"precedence_blocking_debt": 1},
    }
    fs = findings("hard", override=planted)
    print("Saved settings that no longer apply (the engine uses its own value for these):\n")
    if not fs:
        print("  (none)")
    for f in fs:
        print(f"  - {f.get('message')}")
    print("\nThe valid saved value (the triage patience setting = 5) applies as normal and is not listed.")
    # Self-check: the two planted bad settings (the removed one + the fixed one) surface; the valid numeric
    # setting does not — so exactly two findings.
    ok = len(fs) == 2
    if not ok:
        print(f"\nDEMO UNEXPECTED: expected exactly the two stale/fixed settings to surface, got {len(fs)}.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    # ENGINE_OVERRIDE_PATH (unset in production) lets the negative-fixture meta-check feed a seeded
    # saved-settings file so the stale-override gate is witnessed biting a real bad input (#286).
    override_path = validate.env_override_path("ENGINE_OVERRIDE_PATH")
    override = validate.load_json(override_path) if override_path else None
    return emit(findings(tier, override))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
