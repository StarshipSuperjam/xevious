#!/usr/bin/env python3
"""Shared reader for the optional-module catalog — the single home that reads
`.engine/provisioning/module-catalog.json`.

Provisioning owns the catalog (it ships empty and grows as optional modules are built); two readers RELAY
it and must never drift in how they parse it, so both go through this one function:
- the first-run setup walkthrough (`instantiator.py`) — groups the entries by discipline and presents them
  as opt-out-able choices;
- the `/engine-help` command index (`engine_help.py`) — lists an uninstalled module's command under
  "available if you install it".

This is the shared skill/command-discovery helper the `/engine-help` work recorded as owed once a
second reader appeared — that second reader is the instantiator, so it lands here. The reader DEGRADES and
never raises (degrade-to-git-native): a missing, unreadable, malformed, or wrong-shaped catalog
narrows the relay to nothing rather than breaking either caller. It only relays — it decides nothing about
what is installed, and validates nothing (the shape is governed by `provisioning-catalog.v1.json`, enforced
by the `engine/check/provisioning-catalog` schema check, not this read path). Every well-formed entry is
relayed, including a command-less optional module (one with no `verb`): the setup walkthrough offers it by
its description, while `/engine-help` — which lists only typeable commands — filters it out at that reader.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

CATALOG_PATH = os.path.join(validate.ENGINE_DIR, "provisioning", "module-catalog.json")

# The fields a relayed entry carries. `verb` + `description` feed /engine-help; `category` + `status` feed
# the setup walkthrough's grouping. A command-less module simply carries an empty `verb`: the walkthrough
# still offers it (by its description), and /engine-help filters it out where a typeable command is required.
_FIELDS = ("id", "verb", "description", "category", "status")


def _normalize(entry: dict) -> dict:
    """One catalog record coerced to the relayed shape. Missing fields (including `verb`, which a command-less
    module legitimately omits) become empty strings; nothing raises. The reader relays every well-formed entry
    and decides nothing — the shape is enforced by the `engine/check/provisioning-catalog` schema check."""
    return {field: str(entry.get(field) or "") for field in _FIELDS}


def entries(path: str | None = None) -> list:
    """The optional-module catalog as a list of normalized records (each a dict with `id`, `verb`,
    `description`, `category`, `status`), sorted by `verb` then `id` (the secondary key keeps command-less
    entries, which all share an empty `verb`, in a deterministic order). Returns `[]` when there is no catalog
    or it cannot be read as the expected top-level array — a missing or damaged catalog narrows the relay,
    never raises. `path` is injectable for tests/demo; the committed catalog is read by default."""
    target = path or CATALOG_PATH
    if not os.path.isfile(target):
        return []
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = [_normalize(entry) for entry in data if isinstance(entry, dict)]
    return sorted(out, key=lambda e: (e["verb"], e["id"]))
