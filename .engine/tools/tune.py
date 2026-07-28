#!/usr/bin/env python3
"""`/engine-tune` tool — adjust a tunable engine setting through a reviewed change.

Backs the `/engine-tune` operator command: an
engine-mediated way for a non-engineer to change one of the engine's tuning numbers, **never a hand-edit**.
The flow is: show the current effective value → the operator picks a new number → validate it → save it to
the committed operator-override file → open it as a pull request the operator approves → confirm. The saved
value supersedes the shipped default per-key at read time, and is preserved across an engine update (the
operator-override file is operator config, claimed by no module).

Design fidelity (for a maintainer reading the source, not the operator):
- The MERGE is `validate.effective_policy_values` (the core merge); the FILE read/write is here + the floor
  reader `operator_overrides`. ELIGIBILITY is the consumer's own structural set, imported (never restated):
  attention's partition precedence + trim order are structural LAWS an override may never retune
  (`attention_rank.PRECEDENCE_KEYS ∪ TRIM_KEYS`); the threshold policies have no structural keys. No
  enforcement/guardrail value is a tunable policy value, so the override never reaches the weakening-guard surface.
- The write LANDS as a reviewed pull request (the engine's standard transport to protected `main`): the engine writes + commits the override on a branch and opens a PR; the
  operator's MERGE is the "confirm" ("write + commit → confirm", reconciled with the protected-branch
  invariant). The PR-opener is INJECTED (`set_value(opener=…)`) and faked in tests/the demo — the real
  open NEVER runs in the construction repo (a named inductive gap, like the module-manager upgrade opener).
- All operator-facing strings address the operator plainly — the right word, explained where it carries
  weight, with the engine's internal machinery kept out of view (a judgment in the writing and the review,
  never a word-list); the non-tunable-key refusal is a pinned sentence.
"""
from __future__ import annotations
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate           # noqa: E402  (the core merge + frontmatter reader + ROOT)
import operator_overrides  # noqa: E402  (the floor reader/writer home for the override file)
import attention_rank      # noqa: E402  (the structural-key CONSTANTS only — never a runtime read)

OVERRIDES_PATH = operator_overrides.OVERRIDES_PATH

# Each policy's structural keys (values that encode a LAW an override may never retune), imported from the
# owning consumer so this tool never restates them. attention owns the partition precedence + trim order;
# the threshold policies own no structural keys.
_STRUCTURAL = {
    "attention": frozenset(attention_rank.PRECEDENCE_KEYS) | frozenset(attention_rank.TRIM_KEYS),
}

# Pinned operator copy (plain, judged in the review — no word-list). The refusal names the setting's
# structural, fixed nature and steers to what CAN change (plan-gate operator finding).
_REFUSE_STRUCTURAL = ("That setting is structural — it encodes part of the engine's safety order, so it's "
                      "fixed on purpose and can't be changed here. The settings you can adjust are the ones "
                      "this command lists.")
_REASSURANCE = ("This won't change anything on its own — it prepares your change as a request you approve "
                "before it takes effect.")
_CONFIRM = ("I've prepared your change as a pull request — open it and merge it to make it take effect. "
            "Nothing changes until you do.")


def _merge_message() -> str:
    return "An engine setting is a tuning number you can adjust; the engine's safety order is fixed."


def structural_keys(policy_id: str) -> set:
    """The keys of `policy_id` an override may never set (empty for a policy with no structural law)."""
    return set(_STRUCTURAL.get(policy_id, frozenset()))


def _policy_path(policy_id: str) -> str:
    return os.path.join(validate.ENGINE_DIR, "policies", f"{policy_id}.md")


def default_values(policy_id: str) -> dict:
    """The shipped default tuning values of `policy_id` (its frontmatter `values`), or `{}` if the policy
    has no such file / no values block."""
    path = _policy_path(policy_id)
    if not os.path.isfile(path):
        return {}
    return validate.frontmatter(path).get("values", {}) or {}


def eligible_keys(policy_id: str) -> list:
    """The settings of `policy_id` the operator may adjust: the default keys minus the structural ones,
    sorted for a stable listing."""
    structural = structural_keys(policy_id)
    return sorted(k for k in default_values(policy_id) if k not in structural)


def effective(policy_id: str, override_slice: dict | None = None) -> dict:
    """The effective values for `policy_id`: the shipped default with the operator override merged per-key
    (the core merge). With no override, the default is returned unchanged. A structural or stale override key
    is refused/ignored inside the merge (its finding is the stale-key check's concern, not shown here)."""
    default = default_values(policy_id)
    if not override_slice:
        return default
    eff, _findings = validate.effective_policy_values(
        default, override_slice, structural_keys=structural_keys(policy_id), tier="soft",
        message=_merge_message())
    return eff


def validate_value(policy_id: str, key: str, value) -> tuple[bool, str]:
    """Check a proposed change BEFORE saving, returning (ok, plain-message). Refuses a fixed (structural)
    setting with the pinned sentence, an unknown setting, and a non-number value — each in plain words."""
    default = default_values(policy_id)
    if key in structural_keys(policy_id):
        return False, _REFUSE_STRUCTURAL
    if key not in default:
        return False, f"I don't have a setting called “{key}” to change."
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, f"That needs to be a number — “{value}” isn't one."
    if not math.isfinite(value):
        # "infinity" and "not a number" survive float() and json.dumps (as the non-standard `Infinity`/`NaN`
        # literals), so without this they would save cleanly and then quietly break the setting they tune —
        # an endless bar defers even the things that must never be deferred, and "not a number" compares
        # false against everything, so it blocks what it should let past. Refused at the gate, for every
        # setting: a dial the engine cannot act on is not a value it should store.
        return False, f"That needs to be an ordinary number — “{value}” isn't one I can measure against."
    return True, ""


def write_override(policy_id: str, key: str, value, *, path: str = OVERRIDES_PATH) -> dict:
    """Save the change into the committed operator-override file, preserving every other saved setting, and
    return the new override map. Creates the file on the first tune. Caller validates first."""
    data = operator_overrides.load(path)
    data.setdefault(policy_id, {})[key] = value
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return data


def drop_override(policy_id: str, key: str, *, path: str = OVERRIDES_PATH) -> bool:
    """Remove one saved setting, preserving every other. True when something was removed, False when there was
    nothing to remove. An emptied policy slice is dropped too, so a committed file is never left holding a
    meaningless `{}`; a file left with no settings at all is removed rather than committed empty.

    Reads the file RAW rather than through `operator_overrides.load`. That reader deliberately drops any policy
    slice that is not a plain object — the right call for a *reader*, which must degrade to the shipped defaults
    rather than strand a boot. But writing back what it returned would silently erase the dropped slices, and
    this path is reached precisely when someone has been hand-editing the file (the stale-setting finding used
    to tell them to). A clearing verb that quietly discards the settings it could not parse is worse than the
    stale entry it was invoking to remove.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 — an unparseable file is the operator's to fix; never rewrite it blind
        return False
    if not isinstance(data, dict) or not isinstance(data.get(policy_id), dict) or key not in data[policy_id]:
        return False
    del data[policy_id][key]
    if not data[policy_id]:
        del data[policy_id]
    if not data:
        try:
            os.remove(path)
        except OSError:
            pass
        return True
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)   # atomic: a failed write never leaves the operator's settings truncated
    return True


def _pr_title(policy_id: str, key: str) -> str:
    # `Maintenance:` — the release-notes change-kind prefix (release_cut._RELEASE_NOTE_KINDS): a setting change
    # is upkeep. Only the LEADING prefix is matched, so the `{policy_id}: {key}` colon inside is untouched.
    return f"Maintenance: save an engine setting change ({policy_id}: {key})"


def _pr_body(policy_id: str, key: str, value) -> str:
    """The plain-language pull-request body the operator reviews and merges. Names the change for the record
    and explains, in plain words, that merging is what makes it take effect and that it survives updates."""
    return (
        f"You used `/engine-tune` to change an engine setting. This pull request saves your choice.\n\n"
        f"- Setting: `{key}` (in {policy_id})\n"
        f"- New value: `{value}`\n\n"
        "Merging this is what makes the change take effect — nothing changes until you do. Your choice is "
        "saved in a place engine updates do not touch, so an update will not undo it.\n")


def _open_tune_pr(branch: str, title: str, body: str, paths: list, repo=None, token=None) -> dict:
    """THE GIT+PR BOUNDARY: stage the saved override on a new branch, commit, push, and open a pull request
    so the change is reviewed + reversible like any change (mirrors the module-manager upgrade opener — git via subprocess, the PR via POST /pulls, slug/token via boot).
    INJECTED for tests + the demo (`set_value(opener=…)`), so this real path NEVER runs in the construction
    repo (a named inductive gap — no real deployment to tune, no PR to open here)."""
    import subprocess
    import urllib.request
    import json as _json
    import boot  # local: only the real open needs boot's slug/token/base
    slug = repo or boot.repo_slug()
    tok = token if token is not None else boot.gh_token()
    if not slug or not tok:
        raise RuntimeError("could not determine the engine repository / credentials to open the pull request.")
    base = getattr(boot, "PROTECTED_BRANCH", "main")
    subprocess.run(["git", "checkout", "-b", branch], cwd=validate.ROOT, check=True, capture_output=True)
    subprocess.run(["git", "add", *paths], cwd=validate.ROOT, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", title], cwd=validate.ROOT, check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", branch], cwd=validate.ROOT, check=True, capture_output=True)
    url = f"https://api.github.com/repos/{slug}/pulls"
    payload = _json.dumps({"title": title, "head": branch, "base": base, "body": body}).encode("utf-8")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "engine-tune", "Authorization": f"Bearer {tok}",
               "Content-Type": "application/json"}
    with urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers),
                                timeout=60) as resp:
        return _json.loads(resp.read())


def set_value(policy_id: str, key: str, value, *, override_path: str = OVERRIDES_PATH,
              opener=_open_tune_pr, open_pr: bool = True) -> dict:
    """Validate a change, save it to the override file, and (by default) open it as a reviewed pull request.
    Returns a result dict {ok, message, pr}. The opener is injectable (faked in tests/demo); with
    open_pr=False or opener=None the change is saved without opening a PR (a practice run). The save happens
    only after validation passes — an invalid change never touches the file."""
    ok, msg = validate_value(policy_id, key, value)
    if not ok:
        return {"ok": False, "message": msg, "pr": None}
    write_override(policy_id, key, value, path=override_path)
    if not open_pr or opener is None:
        return {"ok": True, "message": "Saved (no pull request opened — practice run).", "pr": None}
    relpath = os.path.relpath(override_path, validate.ROOT)
    branch = "engine-tune-" + re.sub(r"[^a-zA-Z0-9._-]+", "-", f"{policy_id}-{key}")
    title = _pr_title(policy_id, key)
    body = _pr_body(policy_id, key, value)
    try:
        pr = opener(branch=branch, title=title, body=body, paths=[relpath])
    except Exception as exc:
        return {"ok": True, "message": f"Saved, but the pull request could not be opened: {exc}", "pr": None}
    return {"ok": True, "message": _CONFIRM, "pr": pr}


def forget_value(policy_id: str, key: str, *, override_path: str = OVERRIDES_PATH,
                 opener=_open_tune_pr, open_pr: bool = True) -> dict:
    """Clear a saved setting the engine no longer has, and open the removal as a reviewed pull request.

    The recourse behind the stale-saved-setting finding. That check blocks a merge when a saved value names a
    setting that no longer exists, and its advice used to end at "remove this one from your saved settings" —
    a committed JSON file, which is not a remedy for the non-engineer this engine is written for. This is the
    same save-and-propose path `set_value` uses, so clearing a setting is reviewed and reversible exactly like
    changing one; nothing is edited behind the operator's back.

    REFUSES to remove a setting that still exists, and says so: a live setting is changed with `set`, and a
    verb that could silently delete a working choice is a different and more dangerous thing than this one.
    """
    if not os.path.isfile(_policy_path(policy_id)):
        # FAIL CLOSED on doubt. `default_values` returns {} for a policy it cannot read, which would otherwise
        # make every key under an unrecognised group look retired and clear it — and with `--no-pr` that write
        # happens with no review behind it.
        return {"ok": False, "pr": None,
                "message": (f"I don't have a group of settings called “{policy_id}”, so I can't tell whether "
                            f"“{key}” was retired or mistyped. Nothing was changed.")}
    if key in default_values(policy_id) and key not in structural_keys(policy_id):
        return {"ok": False, "pr": None,
                "message": (f"“{key}” is still one of the engine's settings, so there is nothing to clear. To "
                            f"change it, use `set {policy_id} {key} <number>`.")}
    # A STRUCTURAL key is deliberately NOT refused. It still exists, but it is fixed — `set` refuses it too, so
    # refusing here as well would leave a saved value that can never apply, keeps failing the saved-settings
    # check, and has no way out but hand-editing the file: the exact dead end this verb exists to close.
    reason = operator_overrides.retirement_reason(policy_id, key)
    if not drop_override(policy_id, key, path=override_path):
        return {"ok": False, "pr": None,
                "message": f"“{key}” is not in your saved settings, so there is nothing to clear."}
    if not open_pr or opener is None:
        return {"ok": True, "message": "Cleared (no pull request opened — practice run).", "pr": None}
    relpath = os.path.relpath(override_path, validate.ROOT)
    branch = "engine-tune-clear-" + re.sub(r"[^a-zA-Z0-9._-]+", "-", f"{policy_id}-{key}")
    title = f"Maintenance: clear a retired engine setting ({policy_id}: {key})"
    body = (
        f"You used `/engine-tune` to clear a saved setting the engine no longer has. This pull request removes "
        f"it.\n\n- Setting: `{key}` (in {policy_id})\n"
        + (f"- Why it was retired: {reason}\n" if reason else "")
        + "\nThe value was already being ignored, so nothing about how the engine behaves changes when you "
        "merge this — it only stops the saved-settings check flagging it on your future changes.\n")
    try:
        pr = opener(branch=branch, title=title, body=body, paths=[relpath])
    except Exception as exc:  # noqa: BLE001 — the clear already happened; report the opener's failure plainly
        return {"ok": True, "message": f"Cleared, but the pull request could not be opened: {exc}", "pr": None}
    return {"ok": True, "message": _CONFIRM, "pr": pr}


# ---- CLI ------------------------------------------------------------------------------------

def _flag(rest: list, name: str, default=None):
    return rest[rest.index(name) + 1] if name in rest and rest.index(name) + 1 < len(rest) else default


def _num(s):
    """Coerce a CLI string to a number (int when integral, else float); return the raw string when it is not
    a number so validate_value rejects it with the plain message."""
    if isinstance(s, (int, float)):
        return s
    try:
        return int(s)
    except (TypeError, ValueError):
        pass
    try:
        return float(s)
    except (TypeError, ValueError):
        return s


def _show_lines(policy_id: str, override_slice: dict | None = None) -> list:
    eff = effective(policy_id, override_slice)
    keys = eligible_keys(policy_id)
    if not keys:
        return [f"There are no settings you can adjust in {policy_id}."]
    lines = [f"Settings you can adjust in {policy_id}:"]
    lines.extend(f"  {k}: {eff.get(k)}" for k in keys)
    lines.append("To change one, run /engine-tune and pick a setting and a new number.")
    return lines


def _cmd_show(rest: list) -> int:
    if not rest:
        print("Tell me which group of settings to show, e.g. `show triage-threshold`.")
        return 2
    policy_id = rest[0]
    print("\n".join(_show_lines(policy_id, operator_overrides.slice_for(policy_id))))
    return 0


def _cmd_set(rest: list) -> int:
    if len(rest) < 3:
        print("To change a setting: `set <group> <setting> <number>`.")
        return 2
    policy_id, key, raw = rest[0], rest[1], rest[2]
    override_path = _flag(rest, "--override", OVERRIDES_PATH)
    open_pr = "--no-pr" not in rest
    result = set_value(policy_id, key, _num(raw), override_path=override_path, open_pr=open_pr)
    print(result["message"])
    return 0 if result["ok"] else 1


def _cmd_forget(rest: list) -> int:
    if len(rest) < 2:
        print("To clear a setting the engine no longer has: `forget <group> <setting>`.")
        return 2
    policy_id, key = rest[0], rest[1]
    override_path = _flag(rest, "--override", OVERRIDES_PATH)
    open_pr = "--no-pr" not in rest
    result = forget_value(policy_id, key, override_path=override_path, open_pr=open_pr)
    print(result["message"])
    return 0 if result["ok"] else 1


def main(argv: list) -> int:
    if not argv:
        print("Usage: tune.py show <group> | set <group> <setting> <number> | "
              "forget <group> <setting> | demo")
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "demo":
        return _demo()
    if cmd == "show":
        return _cmd_show(rest)
    if cmd == "set":
        return _cmd_set(rest)
    if cmd == "forget":
        return _cmd_forget(rest)
    print(f"Unknown command: {cmd}. Try: show, set, forget, demo.")
    return 2


def _demo() -> int:
    """An operator-runnable demonstration of `/engine-tune` that fakes ONLY the boundary (the pull-request
    opener) and runs the REAL save + the REAL merge + the REAL live-attention read. Everything happens in a
    throwaway temporary override file (your real settings are NOT touched, and NO pull request is opened)."""
    import tempfile
    import attention  # local: the demo shows the live consumer reading the saved value

    captured = {}

    def fake_opener(branch, title, body, paths):
        captured.update(branch=branch, title=title)
        return {"number": 0, "html_url": "https://github.com/example/example/pull/0 (example only)"}

    with tempfile.TemporaryDirectory() as tmp:
        override = os.path.join(tmp, "operator-overrides.json")

        print("1) The settings you can adjust today, with their current values:\n")
        print("\n".join("   " + ln for ln in _show_lines("triage-threshold")))

        print("\n2) Changing one — /engine-tune saves it and prepares it as a request you approve:\n")
        print("   " + _REASSURANCE)
        res = set_value("triage-threshold", "persistence", 5, override_path=override, opener=fake_opener)
        print("   " + res["message"])
        print(f"   (example pull request: {res['pr']['html_url']})")

        print("\n3) The same settings after the change — the new value is now in effect:\n")
        print("\n".join("   " + ln for ln in _show_lines("triage-threshold",
                                                          operator_overrides.slice_for("triage-threshold", override))))

        print("\n4) The engine actually reads your saved value. Adjust one of attention's budgets and watch\n"
              "   the value the engine reads change (the live read, not a copy):\n")
        before = attention.load_policy_values()
        set_value("attention", "budget_orientation", 0.40, override_path=override, opener=fake_opener)
        after_slice = operator_overrides.slice_for("attention", override)
        after = attention.load_policy_values(override=after_slice)
        print(f"   budget_orientation the engine reads — before: {before.get('budget_orientation')}, "
              f"after your change: {after.get('budget_orientation')}")

        print("\n5) Things /engine-tune will not do — it refuses safely, in plain words:\n")
        for pid, key, val, why in (
                ("triage-threshold", "persistence", "lots", "not a number"),
                ("attention", "precedence_blocking_debt", 9, "a fixed safety setting"),
                ("triage-threshold", "made_up_setting", 5, "a setting that does not exist")):
            r = set_value(pid, key, _num(val) if val == "lots" else val, override_path=override,
                          opener=fake_opener)
            print(f"   [{why}] {r['message']}")

    print("\nHonest limits: in the real engine the change opens a pull request you merge — that step is\n"
          "faked here (no pull request is opened, your files are untouched). On this build repo the\n"
          "attention ranking is mostly empty, so step 4 shows the value the engine READS changing, which is\n"
          "the part your tuning controls. Some settings (the background-monitoring ones) only take effect\n"
          "once that monitoring is switched on in a later part of the engine.")
    # Self-check: the saved value is the value the engine actually reads (step 4 — the load-bearing claim).
    ok = after.get("budget_orientation") == 0.40
    if not ok:
        print("\nDEMO UNEXPECTED: the saved tuning value did not become the value the engine reads "
              f"(expected budget_orientation 0.40, got {after.get('budget_orientation')}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
