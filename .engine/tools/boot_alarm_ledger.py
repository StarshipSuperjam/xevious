#!/usr/bin/env python3
"""boot_alarm_ledger — the standing-alarm presentation ledger.

Boot's ONE local write: boot stays read-only of *canonical* state, and its one local write is this
gitignored, non-canonical presentation ledger. It records, per standing governance alarm, the
structured CONDITION VALUE last relayed IN FULL, so a SessionStart that finds the same condition
unchanged can collapse it to a terse reminder instead of re-relaying the full paragraph every resume
(the #313 habituation). The decision is DETERMINISTIC and lives in this hook-side code, never the model —
boot relays whatever variant the decision hands it.

Laws (all load-bearing):
  - FAIL-TOWARD-FULL. A missing / unreadable / malformed ledger, lock contention, or a write failure
    renders every alarm IN FULL (repetition is the tolerable failure; suppression is not). Nothing here
    raises to the caller, and a failed write never blocks the turn.
  - FINGERPRINT THE STRUCTURED CONDITION, never the rendered prose (the prose is reworded on relay, so
    hashing it would never collapse). The compared value is the structured signal the substrate already
    detected — the gate `[state, reason]`, the open-findings count. Stored as the comparable VALUE (not an
    opaque hash) so the renderer can tell unchanged from worsened.
  - SHOWN-IN-FULL IS STAMPED ONLY ON A TRUE FULL RELAY. A collapsed (terse) session keeps the prior entry
    untouched; a vanished alarm is DROPPED, so a recurrence relays full again (the operator-facing "a
    problem vanishing means the engine verified it fixed, not that it stopped checking" promise). A terse
    render can never become a future collapse baseline (no suppression-by-drift).
  - ISOLATED FROM MEMORY'S CONSOLIDATION SWEEP — no shared code path: the git-common-root resolver is
    COPIED here (the checkout_health / memory-ledger idiom), never imported from the memory package, and
    the ledger lives in a distinct gitignored directory.
  - STABLE PER-INSTANCE PATH under the shared clone root's `.engine/boot/.cache/`, so the ledger spans
    separate sessions on the one operator's machine and is never trapped in an ephemeral worktree.
  - TWO WRITERS, ONE LOCK (#471). The SessionStart hook's decide() writes the collapse baselines; a
    SECOND, model-invoked writer (retire(), the operator's "I meant to keep this") writes the RETIRED
    namespace. Both take the same `<ledger>.lock` for a read-modify-write, and decide() CARRIES the RETIRED
    namespace FORWARD untouched, so neither writer erases the other's state. Retire-eligibility is a code
    constant (RETIRE_ELIGIBLE_CLASSES) checked mechanically at honor time keyed on the LIVE finding class —
    never a label read from the ledger — so a retired marker can never silence a governance alarm.

CLI (operator-runnable): python tools/boot_alarm_ledger.py path     # print the resolved ledger path
                         python tools/boot_alarm_ledger.py retire   # retire the live leftover-license offer
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

try:
    import fcntl
    _HAVE_FCNTL = True
except Exception:  # pragma: no cover — non-POSIX target; degrade to no cross-process lock
    _HAVE_FCNTL = False

# A test/override hook for the cache directory (the ENGINE_MEMORY_DIR idiom in memory/ledger.py). Lets a
# test point the ledger at a tmp dir without a git layout.
ENV_DIR = "ENGINE_BOOT_CACHE_DIR"
# The `.cache` segment is DELIBERATE: module_coherence prunes `.cache` directories at any depth, so the
# ledger never trips the orphan-wire walk. `.engine/boot/` is boot's topology-sanctioned artifact home.
CACHE_SUBDIR = os.path.join(".engine", "boot", ".cache")
LEDGER_FILENAME = "standing-alarms.json"

# The RETIRED namespace — a reserved top-level key holding {fingerprint: true} for findings the operator has
# deliberately kept ("I meant to keep this", #471). It lives in the SAME ledger file as the collapse
# baselines but in its own key, and decide() CARRIES IT FORWARD untouched on every rewrite (a collapse-key
# rebuild must never erase a retire marker). No alarm key collides with this reserved name.
_RETIRED_NS = "__retired__"

# The CLOSED set of retire-eligible finding classes — a build-time constant, the retire-eligibility gate. A retired marker is
# honored ONLY for a class in this set; the class is the LIVE one the caller passes (derived from the producing
# detector), never a label read from the ledger. A governance/strand/unprovisioned alarm is NOT here, so it can
# be declined (collapse to terse) but NEVER retired — a mis-written or injection-planted marker cannot silence
# it. A drift test pins this set to exactly {"foreign_license"} (test_boot_alarm_ledger) so no future alarm
# becomes silenceable without a deliberate edit here.
RETIRE_ELIGIBLE_CLASSES = frozenset({"foreign_license", "greenfield_intake"})


def _run(cmd: list) -> str | None:
    """Run a local command, return stripped stdout or None on any failure. Never raises."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a missing binary / OS error degrades to "unavailable"
        return None


def _git_common_root(cwd: str | None = None) -> str | None:
    """The shared clone root (parent of the common `.git` dir) so every worktree shares ONE ledger; None
    for a bare repo / unusual layout / git unavailable. COPIED (not imported) from the checkout_health /
    memory-ledger idiom so boot's ledger shares no code path with memory's consolidation sweep."""
    base = cwd or os.getcwd()
    out = _run(["git", "-C", base, "rev-parse", "--git-common-dir"])
    if not out:
        return None
    common = out if os.path.isabs(out) else os.path.join(base, out)
    common = os.path.normpath(os.path.abspath(common))
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return None


def ledger_dir(cwd: str | None = None) -> str:
    """The directory holding the ledger: the ENV_DIR override, else the shared clone root's
    `.engine/boot/.cache/`, else a CWD-relative fallback (so it still resolves where git is unavailable)."""
    env = os.environ.get(ENV_DIR)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    root = _git_common_root(cwd)
    base = root if root is not None else (cwd or os.getcwd())
    return os.path.join(base, CACHE_SUBDIR)


def ledger_path(cwd: str | None = None, path: str | None = None) -> str:
    """The full ledger path. An explicit `path` wins (tests); else `<ledger_dir>/standing-alarms.json`."""
    return path if path else os.path.join(ledger_dir(cwd), LEDGER_FILENAME)


def _read(path: str) -> dict | None:
    """The ledger as {key: {"value": <json>, "shown_in_full": bool}}, or None on
    absent/unreadable/malformed (-> fail-toward-full at the caller)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — absent / unreadable / malformed -> None (full relay)
        return None
    return data if isinstance(data, dict) else None


def _write(path: str, ledger: dict) -> bool:
    """Atomically write the ledger — temp file in the SAME directory, then os.replace. Returns
    True/False; never raises (a failed write degrades to 'no ledger next time' -> full relay)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 — degrade to no ledger, never a crash
        return False


def _acquire(lock_path: str):
    """A single non-blocking exclusive lock on the read-decide-write, or None on contention (the tightest
    bound — no sleep stalls a SessionStart hook; contention degrades to fail-toward-full)."""
    fd = None
    try:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    except OSError:
        return None
    if not _HAVE_FCNTL:  # pragma: no cover — no cross-process lock available; proceed best-effort
        return fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _release(fd) -> None:
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def decide(alarms: list, *, cwd: str | None = None, path: str | None = None) -> dict:
    """Read the ledger, decide collapse-vs-full per collapse-eligible alarm, write the new ledger, and
    return the per-key outcome the renderer (boot) turns into wording.

    `alarms`: an ordered list of {"key": str, "value": <json-able>} for the COLLAPSE-ELIGIBLE alarms live
    THIS session (boot passes only those; the degrade-loud tells never reach the ledger). Returns
        {"ok": bool, "results": {key: {"outcome": "collapse"|"full", "prior": <value|None>}}}
    `ok` is False on any ledger-read failure / contention -> every alarm "full" with prior None
    (fail-toward-full; the renderer then uses neutral full wording, never a misleading "still"/"worse").

    Ledger semantics: an alarm whose stored value equals the live value AND was last shown in full
    collapses (and its entry is kept verbatim). A new / changed alarm renders full and (re)stamps
    shown_in_full at the live value. Alarms not live this session are dropped (vanished -> verified-fixed,
    so a recurrence relays full). A write failure leaves the decision intact (ok unchanged) and never
    blocks the turn."""
    # Build the key/value view DEFENSIVELY: a malformed alarm entry must degrade to fail-toward-full
    # (the renderer defaults a missing key to full), never raise into boot's SessionStart hook.
    try:
        keys = [a["key"] for a in alarms]
        current = {a["key"]: a["value"] for a in alarms}
    except Exception:  # noqa: BLE001 — a malformed alarm -> empty results -> renderer renders all full
        return {"ok": False, "results": {}}
    failsafe = {"ok": False, "results": {k: {"outcome": "full", "prior": None} for k in keys}}
    # An empty alarm set is NOT a no-op: it still runs so any prior entry is DROPPED (every alarm vanished
    # -> verified-fixed). Skipping the write here would let a stale entry survive and wrongly collapse a
    # recurrence. The write is boot's one sanctioned local write; an empty ledger is the correct result.
    target = ledger_path(cwd, path)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
    except Exception:  # noqa: BLE001 — can't even make the dir -> fail-toward-full
        return failsafe
    fd = _acquire(target + ".lock")
    if fd is None:
        return failsafe  # contention -> fail-toward-full (a collapse lost is safe; a hidden alarm is not)
    try:
        old = _read(target)
        ok = old is not None  # a missing/unreadable/malformed ledger -> neutral full this run, then seed
        if old is None:
            old = {}
        results: dict = {}
        new_ledger: dict = {}
        for k in keys:
            val = current[k]
            entry = old.get(k) if isinstance(old.get(k), dict) else None
            if ok and entry is not None and entry.get("shown_in_full") and entry.get("value") == val:
                results[k] = {"outcome": "collapse", "prior": val}
                new_ledger[k] = {"value": val, "shown_in_full": True}   # keep the prior full baseline
            else:
                prior = entry.get("value") if (ok and entry is not None) else None
                results[k] = {"outcome": "full", "prior": prior}
                new_ledger[k] = {"value": val, "shown_in_full": True}   # stamp THIS true full relay
        # Keys present last session but not live now are simply absent from new_ledger -> dropped. But the RETIRED
        # namespace is NOT a collapse key and has a lifecycle of its own — carry it forward untouched so a
        # collapse-key rebuild never erases an operator's "I meant to keep this" (#471). Preserved only when
        # the ledger read succeeded (ok); a fresh/unreadable ledger seeds an empty namespace, never a false retire.
        retired_ns = old.get(_RETIRED_NS)
        if ok and isinstance(retired_ns, dict) and retired_ns:
            new_ledger[_RETIRED_NS] = retired_ns
        _write(target, new_ledger)
        return {"ok": ok, "results": results}
    except Exception:  # noqa: BLE001 — any unexpected failure -> fail-toward-full
        return failsafe
    finally:
        _release(fd)


def is_retired(fingerprint: str, cls: str, *, cwd: str | None = None, path: str | None = None) -> bool:
    """True iff `cls` is a retire-eligible finding class AND a retired marker for `fingerprint` is recorded. The
    eligibility gate is a CODE CONSTANT (RETIRE_ELIGIBLE_CLASSES) keyed on the LIVE class the caller passes —
    derived from the producing detector, NEVER a label read from the ledger — so a retired marker planted on a
    governance alarm's fingerprint is ignored and that alarm still renders. Read-only, no lock.
    Fail-toward-SHOWING: a non-eligible class, an absent/unreadable/malformed ledger, or a missing marker all
    return False (the finding surfaces)."""
    if cls not in RETIRE_ELIGIBLE_CLASSES:
        return False
    old = _read(ledger_path(cwd, path))
    if not isinstance(old, dict):
        return False
    ns = old.get(_RETIRED_NS)
    return isinstance(ns, dict) and bool(ns.get(fingerprint))


def retire(fingerprint: str, cls: str, *, cwd: str | None = None, path: str | None = None) -> dict:
    """Record a retired marker for `fingerprint` — the operator's deliberate 'I meant to keep this'. Returns
    {"ok": bool, "reason": <str>}. REFUSES a class not in RETIRE_ELIGIBLE_CLASSES (write-time defense-in-depth;
    is_retired's honor gate is the real guarantee). Read-modify-write UNDER THE LOCK, preserving EVERY
    existing entry (collapse baselines and any prior markers) — this is the model-invoked SECOND writer the
    ledger's concurrency discipline must cover, alongside the SessionStart hook's decide(). Never raises; lock
    contention returns an honest {"ok": False}, never a silent no-op."""
    if cls not in RETIRE_ELIGIBLE_CLASSES:
        return {"ok": False, "reason": "not-retire-eligible"}
    target = ledger_path(cwd, path)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
    except Exception:  # noqa: BLE001 — can't even make the dir
        return {"ok": False, "reason": "no-dir"}
    fd = _acquire(target + ".lock")
    if fd is None:
        return {"ok": False, "reason": "contended"}
    try:
        old = _read(target)
        if not isinstance(old, dict):
            old = {}
        ns = old.get(_RETIRED_NS)
        if not isinstance(ns, dict):
            ns = {}
        ns[fingerprint] = True
        old[_RETIRED_NS] = ns
        return {"ok": True} if _write(target, old) else {"ok": False, "reason": "write-failed"}
    finally:
        _release(fd)


def main(argv: list) -> int:
    if argv and argv[0] == "path":
        print(ledger_path())
        return 0
    if argv and argv[0] == "retire":
        # The operator said "I meant to keep this." DERIVE the current leftover-license fingerprint from the live
        # detector (single source of truth), so the honored marker always matches what the detector emits — never
        # a caller-supplied value that could silently mismatch. No leftover license present -> nothing to retire.
        import license_health
        d = license_health.detect_foreign_license()
        if d is None:
            print("No leftover template LICENSE to retire (nothing is offering).", file=sys.stderr)
            return 1
        r = retire(d["fingerprint"], "foreign_license")
        if r.get("ok"):
            print("Retired: the leftover-license offer won't surface again on this checkout.")
            return 0
        print(f"Could not retire ({r.get('reason')}) — it will surface again; try once more.", file=sys.stderr)
        return 1
    if argv and argv[0] == "retire-greenfield":
        # The operator said "I'd rather work without a written description." DERIVE the current greenfield
        # fingerprint from the live detector (single source of truth), so the honored marker always matches what
        # the detector emits — never a caller-supplied value that could silently mismatch and keep the offer
        # firing. No greenfield state -> nothing to retire.
        import greenfield_intake
        d = greenfield_intake.detect_greenfield()
        if d is None:
            print("No greenfield-intake offer to retire (nothing is offering).", file=sys.stderr)
            return 1
        r = retire(d["fingerprint"], "greenfield_intake")
        if r.get("ok"):
            print("Retired: the describe-your-project offer won't surface again on this checkout.")
            return 0
        print(f"Could not retire ({r.get('reason')}) — it will surface again; try once more.", file=sys.stderr)
        return 1
    print("usage: boot_alarm_ledger.py [path|retire|retire-greenfield]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
