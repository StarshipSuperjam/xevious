"""Retire the per-prompt hint's strength setting from a deployment's saved settings.

WHY THIS EXISTS. The per-prompt reminder used to run a keyword lookup and speak only when a stored memory
matched the words strongly enough; `scent_strong_match_threshold` was the bar it had to clear. That reminder
now appears on every message and reads nothing from the attention policy, so the setting governs nothing and
was removed from the policy's own settings block. Any deployment whose operator had tuned it would otherwise
carry a value naming a setting that no longer exists — which the stale-saved-setting check reports as a HARD,
merge-blocking finding on their very next pull request, for a change they did not make. This transform removes
that one entry at upgrade instead, so the block never happens; the operator meets it as a plain line in the
upgrade's own pull request ("Changes a setting: …"), and reverting the upgrade restores the file.

WHAT IT WILL NOT DO. It removes exactly one key from exactly one policy slice, and only when that key is
present. Every other saved setting, including any the operator has tuned since, is written back untouched. It
never creates the file, never empties it, and on anything it cannot parse it changes nothing at all — an
unreadable saved-settings file is the operator's to fix, and a transform that "helpfully" rewrote it would
destroy tuning it could not read. Under-reaching leaves a stale entry the check still catches and explains;
over-reaching silently discards the operator's choices, so the bias is deliberate.
"""
from __future__ import annotations

import json
import os

import validate  # the tool-runtime puts the tools dir on sys.path; ROOT is the deployment being upgraded

_POLICY = "attention"
_KEY = "scent_strong_match_threshold"


def migrate(context) -> dict:
    """Drop `attention.scent_strong_match_threshold` from the committed saved settings, if it is there.

    Returns a small report (`{"changed": bool, "reason": str}`) for the caller's log. Never raises: a
    migration that blew up would fail an upgrade over a setting that is already being ignored.
    """
    assert context["kind"] == "config"
    path = os.path.join(validate.ROOT, ".engine", "operator-overrides.json")
    if not os.path.isfile(path):
        return {"changed": False, "reason": "no saved settings file — nothing to tidy"}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — unreadable/malformed: leave it exactly as found
        return {"changed": False, "reason": "the saved settings file could not be read — left untouched"}
    slice_ = data.get(_POLICY) if isinstance(data, dict) else None
    if not isinstance(slice_, dict) or _KEY not in slice_:
        return {"changed": False, "reason": "this setting was not tuned here"}
    del slice_[_KEY]
    if not slice_:
        del data[_POLICY]          # an emptied slice would linger as a meaningless {} in a committed file
    try:
        # Write a sibling and rename over the original, so the claim above ("never empties it") is true even
        # when the write dies partway: opening the real path for writing truncates it FIRST, which would leave
        # the operator's settings empty on a full disk or an interrupt while reporting them untouched.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — a failed write leaves the original in place; the check still explains it
        return {"changed": False, "reason": "the saved settings file could not be written — left untouched"}
    return {"changed": True, "reason": "removed the retired per-prompt hint strength setting"}
