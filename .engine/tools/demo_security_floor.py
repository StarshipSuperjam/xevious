#!/usr/bin/env python3
"""Operator-runnable demo: the engine turns on GitHub's native security features WHERE YOUR PROJECT'S PLAN
SUPPORTS THEM, branches on what GitHub actually answers, never claims a feature is on when it isn't, and
tells you — in plain words — what's on and (where something's off) how you'd turn it on.

Run: uv run --directory .engine -- python tools/demo_security_floor.py   (no network, no token needed)

This exercises the REAL logic (`security_floor.SecurityFloor`) against FAKE GitHub answers representing four
real situations. For each one you see, in plain words, the situation we fed in and the exact message the
engine would show you — so you can check that the SAME code says "on" in one case and "off, here's the
unlock" in another. Nothing here touches a real project."""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import security_floor as sf  # noqa: E402
import protection_guard      # noqa: E402

REPO = "you/your-project"


def _fake(*, secrets, secrets_enabled, code_patch, code_state, pvr_put, pvr_enabled):
    """A fake GitHub answering each security endpoint with the given status/shape (the (status, body) the
    enable call returns, plus what the read-back shows). Headers are irrelevant here."""
    def t(method, path, body=None):
        if path.endswith("/code-scanning/default-setup"):
            if method == "PATCH":
                return code_patch[0], code_patch[1], {}
            return 200, {"state": code_state}, {}
        if path.endswith("/private-vulnerability-reporting"):
            if method == "PUT":
                return pvr_put[0], pvr_put[1], {}
            return 200, {"enabled": pvr_enabled}, {}
        if method == "PATCH" and isinstance(body, dict) and "security_and_analysis" in body:
            return secrets[0], secrets[1], {}
        if method == "GET" and path.startswith("/repos/"):
            sa = {"secret_scanning": {"status": "enabled" if secrets_enabled else "disabled"}}
            return 200, {"security_and_analysis": sa}, {}
        return 404, None, {}
    return t


def _run(label: str, situation: str, transport, expect: dict) -> bool:
    captured = []
    toggles = sf.SecurityFloor(REPO, "tok", transport=transport).apply(announce=captured.append)
    states = {t.key: t.state for t in toggles}
    print(f"\n[{label}]  We pretended GitHub said: {situation}")
    print("-" * 78)
    for line in "\n".join(captured).splitlines():
        print("   " + line)
    ok = states == expect
    print(f"   → outcome: {states}  ({'as expected' if ok else 'UNEXPECTED'})")
    return ok


def main() -> int:
    print("=" * 78)
    print("Native security features: turned on where supported, honestly disclosed where not")
    print("=" * 78)
    results = []

    # [1] A public project where the operator can administer it — everything turns ON.
    results.append(_run(
        "1", "this is a public project, everything is available",
        _fake(secrets=(200, {}), secrets_enabled=True, code_patch=(202, {"run_id": 1}),
              code_state="configured", pvr_put=(204, None), pvr_enabled=True),
        {"secret-scanning": sf.ON, "code-scanning": sf.ON, "pvr": sf.ON}))

    # [2] A free PRIVATE project — secret + code scanning aren't offered; private reporting can't exist.
    results.append(_run(
        "2", "this is a private project on the free plan",
        _fake(secrets=(403, {"message": "Advanced Security is not enabled"}), secrets_enabled=False,
              code_patch=(403, {"message": "Advanced Security is not enabled"}), code_state="not-configured",
              pvr_put=(422, {"message": "public repositories only"}), pvr_enabled=False),
        {"secret-scanning": sf.UNSUPPORTED, "code-scanning": sf.UNSUPPORTED, "pvr": sf.UNSUPPORTED}))

    # [3] Code scanning is accepted but set up in the background (async) — reported as REQUESTED, not on.
    results.append(_run(
        "3", "code scanning was accepted but is still configuring in the background",
        _fake(secrets=(200, {}), secrets_enabled=True, code_patch=(202, {"run_id": 1}),
              code_state="not-configured", pvr_put=(204, None), pvr_enabled=True),
        {"secret-scanning": sf.ON, "code-scanning": sf.PENDING, "pvr": sf.ON}))

    # [4] The enable call returned success but the read-back can't confirm — NEVER reported as on.
    results.append(_run(
        "4", "the enable call returned OK, but reading it back failed",
        _fake(secrets=(200, {}), secrets_enabled=False, code_patch=(200, {}), code_state="not-configured",
              pvr_put=(204, None), pvr_enabled=False),
        {"secret-scanning": sf.FAILED, "code-scanning": sf.PENDING, "pvr": sf.FAILED}))

    # The advisory guarantee: none of these toggles is ever wired as a required merge check.
    advisory = "secret-scanning" not in protection_guard.REQUIRED_CHECKS \
        and "code-scanning" not in protection_guard.REQUIRED_CHECKS
    print("\n[5]  Advisory check — these features never block your merge.")
    print("-" * 78)
    print(f"   the checks that CAN block a merge: {protection_guard.REQUIRED_CHECKS}")
    print(f"   none of the native security features is in that list? {advisory}")

    ok = all(results) and advisory
    print("\n" + "=" * 78)
    print("In plain words: the engine turns these on where your project's plan allows it, tells you what's")
    print("on (including that outsiders can now privately report security problems), and where something")
    print("isn't available it says so and how you'd unlock it — never quietly, never claiming a feature is")
    print("on when it isn't, and never blocking your work.")
    print("DEMO OK" if ok else "DEMO FAILED -- unexpected outcome")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
