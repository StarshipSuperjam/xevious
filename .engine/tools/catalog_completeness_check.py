#!/usr/bin/env python3
"""engine/check/catalog-completeness — every engine MODULE whose manifest `status` is `default-on` or
`optional` must have a matching entry in the optional-module catalog (.engine/provisioning/module-catalog.json).

Why it is load-bearing (StarshipSuperjam/engine-template#759). An engine UPDATE decides whether to auto-install a `default-on` module the
deployment lacks by asking whether that module was ever KNOWN here — and once a module is declined at first-run
its files and manifest are DELETED, so the catalog is the only durable record it ever existed. If a `default-on`
module shipped WITHOUT a catalog entry and an operator declined it, a later update would misread it as brand-new
and RESURRECT it against the operator's choice. This check keeps the catalog complete so that discriminator can
never be fooled. `optional` modules are covered too — the same offer surfaces (first-run, `/engine-help`, and the
update's offer) read the catalog, so an uncatalogued optional module is silently unofferable. `required` modules
are exempt: they are never offered or declined, so they need no catalog entry.

It reads the catalog and the module manifests as PLAIN DATA (never importing the update code it protects), and
scans the modules present in the tree — in the engine's own home repo that is the full module set (where the
guarantee is realized), and in a deployed repo it is that deployment's present set (still honest, never a false
positive on an already-declined module, whose manifest is gone). Emits finding.v1 JSON to stdout per the
custom/script contract: one finding per uncatalogued default-on/optional module at the rule tier
(ENGINE_RULE_TIER); an empty array passes; an unreadable catalog is a single soft could-not-evaluate note.
ENGINE_CATALOG_ROOT (unset in production) lets the negative-fixture meta-check point the scan at a seeded tree.
"""
from __future__ import annotations
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (finding.v1, ROOT, load_json, env_override_path)

# The manifest statuses that MUST be catalogued (the offerable set). `required`/`retired`/`experimental` are
# not: required is never offered, retired is gone, and experimental is surfaced only where explicitly opted in.
_CATALOGUED_STATUSES = frozenset({"default-on", "optional"})
_TOKEN = "not listed in the optional-module catalog"   # the distinctive bite token (message_contains)


def check(root: "str | None" = None) -> list:
    """Return the finding.v1 list for `root` (defaults to the live ROOT). Pure over the tree it is handed."""
    base = root or validate.ROOT
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    cat_path = os.path.join(base, ".engine", "provisioning", "module-catalog.json")
    if os.path.isfile(cat_path):
        try:
            with open(cat_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, list):
                raise ValueError("the catalog is not a JSON array")
            catalogued = {str(e.get("id")) for e in data if isinstance(e, dict) and e.get("id")}
        except Exception as exc:   # noqa: BLE001 — an unreadable catalog is a could-not-evaluate soft note
            return [validate.finding("soft", f"Could not read the optional-module catalog to check "
                                     f"completeness ({exc}); this check did not run.")]
    else:
        catalogued = set()   # absent catalog: any default-on/optional module present is genuinely uncatalogued
    out = []
    for man_path in sorted(glob.glob(os.path.join(base, ".engine", "modules", "*", "manifest.json"))):
        try:
            m = validate.load_json(man_path)
        except Exception:   # noqa: BLE001 — a malformed manifest is another check's job, not this one's
            continue
        mid, status = m.get("id"), m.get("status")
        if status in _CATALOGUED_STATUSES and mid and mid not in catalogued:
            out.append(validate.finding(
                tier,
                f"Module '{mid}' is '{status}' but is {_TOKEN} (.engine/provisioning/module-catalog.json). "
                f"Every default-on or optional module must be catalogued: an engine update tells a net-new "
                f"default-on module from one an operator previously declined by whether it appears in the "
                f"catalog (a declined module leaves no other trace on disk), so an uncatalogued default-on "
                f"module could be silently turned back on by a later update. Add an entry for '{mid}' — an id, a "
                f"one-line description, and a discipline category (plus a command/verb only if it has one), "
                f"matching the other entries — to clear this.",
                os.path.relpath(man_path, base)))
    return out


def main() -> int:
    print(json.dumps(check(validate.env_override_path("ENGINE_CATALOG_ROOT"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
