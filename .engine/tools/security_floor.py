#!/usr/bin/env python3
"""The native-scanning security floor (issue #124). Enables GitHub's NATIVE secret scanning + push protection, CodeQL
default code scanning, and private vulnerability reporting WHERE THE REPOSITORY'S TIER SUPPORTS THEM — each by
a single operator-privileged `gh` call, branching on the call's HTTP status, never fire-and-forget. It never
reports a feature on when the enabling call did not succeed (verify-after, mirroring bootstrap.ControlPlane),
never binds any of it as a required merge check (alerts are advisory), and never changes the repository's
visibility to unlock a feature (the choice stays the operator's). Where a feature is unavailable it is
DISCLOSED in plain language — what is off and the unlock stated so a non-engineer can evaluate it — never
silently dropped.

Reuses the operator-privileged transport the ruleset bootstrap already holds (no new capability): the
`transport(method, path, body) -> (status, json, headers)` seam is injectable, so tests/the demo replace ONLY
the network and run the real status-branch logic. The control plane locks these invariants; this is the
provisioning mechanism."""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap  # noqa: E402  (reuse ControlPlane._http transport + BootstrapError; never its ruleset apply)

# Per-toggle outcome states (the leaf returns DATA; render() turns them into operator prose):
ON = "on"                  # the enabling call succeeded AND the read-back confirms it is in force
ALREADY = "already"        # confirmed already in force (an idempotent re-run) — operator-identical to ON
PENDING = "pending"        # accepted but still configuring (CodeQL default setup is async) — confirm in settings
UNSUPPORTED = "unsupported"  # the tier does not offer it (skip + disclose the evaluable unlock)
UNVERIFIED = "unverified"  # the call succeeded but the read-back could not confirm — NEVER reported as on
FAILED = "failed"          # an error the retry did not clear

# Unlock categories — how an unsupported feature would become available (NEVER a bare product-tier name):
PUBLIC_OR_PAID = "public-or-paid"   # free on public; on private needs a paid add-on (secret + code scanning)
PUBLIC_ONLY = "public-only"         # a public-repository-only feature (private vulnerability reporting)

_GOOD = (ON, ALREADY, PENDING, UNSUPPORTED)   # honest, non-failure outcomes (the floor did its job)


class Toggle:
    """One native feature's outcome. Carries only the decided STATE + the unlock category — never the GitHub
    response body or an HTTP status, so render() cannot leak an API message or a status code to the operator."""

    def __init__(self, key: str, state: str, unlock: str | None = None):
        self.key = key            # "secret-scanning" | "code-scanning" | "pvr"
        self.state = state
        self.unlock = unlock

    def is_good(self) -> bool:
        return self.state in _GOOD


class SecurityFloor:
    """Enable the native security features where the tier supports them, branching on each call's status."""

    def __init__(self, repo: str, token: str, transport=None):
        self.repo = repo
        self.token = token
        # Reuse the EXACT operator-privileged HTTP the ruleset bootstrap holds (Bearer + urllib + the
        # (status, json, headers) contract); injectable so tests/the demo run the real branch logic offline.
        self._transport = transport or bootstrap.ControlPlane(repo, token)._http

    def _call(self, method: str, path: str, body=None):
        """Make one call, returning (status, json) or (None, None) on a transport (network) failure."""
        try:
            status, data, _headers = self._transport(method, path, body)
        except bootstrap.BootstrapError:
            return None, None
        return status, data

    # -- secret scanning + push protection (one security_and_analysis surface; free on public, paid on private) --

    def enable_secret_scanning(self) -> Toggle:
        payload = {"security_and_analysis": {
            "secret_scanning": {"status": "enabled"},
            "secret_scanning_push_protection": {"status": "enabled"}}}
        status, _ = self._call("PATCH", f"/repos/{self.repo}", payload)
        if status is None:
            return Toggle("secret-scanning", UNVERIFIED)
        if status == 403:                                  # GitHub Advanced Security not available on a free private repo
            return Toggle("secret-scanning", UNSUPPORTED, PUBLIC_OR_PAID)
        if status >= 400:
            return Toggle("secret-scanning", FAILED)
        return Toggle("secret-scanning", self._confirm_secret_scanning())

    def _confirm_secret_scanning(self) -> str:
        """Read the repo back and confirm secret scanning is enabled — never assume the write took."""
        status, data = self._call("GET", f"/repos/{self.repo}", None)
        if status is None or status >= 400 or not isinstance(data, dict):
            return UNVERIFIED
        sa = data.get("security_and_analysis") or {}
        return ON if (sa.get("secret_scanning") or {}).get("status") == "enabled" else FAILED

    # -- code scanning (CodeQL default setup; native-only, no traveling fallback; async 202) ------------------

    def enable_code_scanning(self) -> Toggle:
        path = f"/repos/{self.repo}/code-scanning/default-setup"
        payload = {"state": "configured"}
        status, _ = self._call("PATCH", path, payload)
        if status is None:
            return Toggle("code-scanning", UNVERIFIED)
        # For CODE SCANNING a 422 is TRANSIENT (a setup run in progress / not-yet-in-the-required-state) →
        # retry once. This is deliberately NOT the PVR 422, which means public-only-unsupported (enable_pvr):
        # the status is keyed per toggle so neither is misread as the other.
        if status in (409, 422):
            status, _ = self._call("PATCH", path, payload)
            if status is None:
                return Toggle("code-scanning", UNVERIFIED)
        if status == 403:                                  # Advanced Security not available on a free private repo
            return Toggle("code-scanning", UNSUPPORTED, PUBLIC_OR_PAID)
        if status not in (200, 201, 202):
            return Toggle("code-scanning", FAILED)
        # 202 = accepted, configured asynchronously: confirming takes time, so report PENDING unless the
        # read-back ALREADY shows it configured. 202-not-yet-configured is the common case, NOT an error.
        return Toggle("code-scanning", self._confirm_code_scanning())

    def _confirm_code_scanning(self) -> str:
        status, data = self._call("GET", f"/repos/{self.repo}/code-scanning/default-setup", None)
        if status is None or status >= 400 or not isinstance(data, dict):
            return PENDING                                  # accepted but unreadable yet — confirm in settings
        return ON if data.get("state") == "configured" else PENDING

    # -- private vulnerability reporting (public-repository-only) ---------------------------------------------

    def enable_pvr(self) -> Toggle:
        path = f"/repos/{self.repo}/private-vulnerability-reporting"
        status, _ = self._call("PUT", path, None)
        if status is None:
            return Toggle("pvr", UNVERIFIED)
        if status == 422:                                  # structurally absent on a private repo (public-only)
            return Toggle("pvr", UNSUPPORTED, PUBLIC_ONLY)
        if status >= 400:
            return Toggle("pvr", FAILED)
        return Toggle("pvr", self._confirm_pvr())

    def _confirm_pvr(self) -> str:
        status, data = self._call("GET", f"/repos/{self.repo}/private-vulnerability-reporting", None)
        if status is None or status >= 400 or not isinstance(data, dict):
            return UNVERIFIED
        return ON if bool(data.get("enabled")) else FAILED

    def apply(self, announce=None) -> list:
        """Enable all three surfaces, branching on each status, and disclose the outcome in plain language.
        Returns the list of Toggles (data). NEVER touches the branch ruleset / required checks — alerts are
        advisory — and NEVER changes repository visibility."""
        say = announce if announce is not None else (lambda text: print(text))
        toggles = [self.enable_secret_scanning(), self.enable_code_scanning(), self.enable_pvr()]
        say(render(toggles))
        return toggles


# ---- operator-facing disclosure (plain language; bidirectional; NEVER an HTTP status or a bare tier name) ----

_HUMAN_ON = {
    "secret-scanning": ("Secret scanning is on — GitHub will warn you if a password or key is about to be "
                        "committed by accident."),
    "code-scanning": ("Code scanning is on — GitHub checks your code for common security mistakes and flags "
                      "what it finds. It only warns; it never blocks your work."),
    "pvr": ("Private vulnerability reporting is on — people outside your project can now send you a security "
            "problem privately, and it arrives in your repository's Security tab on GitHub. So the first "
            "private report is something you're expecting, not a surprise."),
}
_HUMAN_PENDING = {
    "code-scanning": ("Code scanning has been requested — GitHub sets this up in the background. Check your "
                      "repository's Security settings in a few minutes to confirm it is on. I won't report it "
                      "as on until it is."),
}
_HUMAN_UNSUPPORTED = {
    PUBLIC_OR_PAID: ("isn't available on a private project at the free level, so it is off for now. Two ways "
                     "to turn it on: making the project public is free, or you can add a paid GitHub security "
                     "add-on that costs a set amount per person. I won't change anything — including whether "
                     "your project is public or private — without you."),
    PUBLIC_ONLY: ("is a public-project feature, so it isn't available on a private project. To turn it on you "
                  "would make the project public, which is free. In the meantime the SECURITY.md file in your "
                  "project is how people reach you about a security problem."),
}
_HUMAN_NAME = {
    "secret-scanning": "Secret scanning",
    "code-scanning": "Code scanning",
    "pvr": "Private vulnerability reporting",
}


def render(toggles: list) -> str:
    """Compose the bidirectional disclosure: lead with what is now protecting them (naming the private-
    reporting consequence), then what is off and the evaluable unlock, then anything that couldn't be
    confirmed. Built ONLY from the decided states — never from a GitHub response body or status code."""
    on_lines, pending_lines, off_lines, unconfirmed = [], [], [], []
    for t in toggles:
        if t.state in (ON, ALREADY):
            on_lines.append(_HUMAN_ON[t.key])
        elif t.state == PENDING:
            pending_lines.append(_HUMAN_PENDING.get(t.key, f"{_HUMAN_NAME[t.key]} has been requested."))
        elif t.state == UNSUPPORTED:
            off_lines.append(f"{_HUMAN_NAME[t.key]} {_HUMAN_UNSUPPORTED[t.unlock]}")
        else:  # UNVERIFIED / FAILED
            unconfirmed.append(_HUMAN_NAME[t.key].lower())

    parts = []
    if on_lines:
        parts.append("Here's what's now protecting your project:\n- " + "\n- ".join(on_lines))
    if pending_lines:
        parts.append("\n".join(pending_lines))
    if off_lines:
        parts.append("And here's what isn't on, and how you'd turn it on if you want it:\n- "
                     + "\n- ".join(off_lines))
    if unconfirmed:
        parts.append("I couldn't confirm " + _join(unconfirmed) + " turned on, so I'm not reporting "
                     + ("them" if len(unconfirmed) > 1 else "it") + " as on. Please check your repository's "
                     "Security settings, or ask me to run setup again.")
    if not parts:
        return "I didn't change any of your project's built-in security settings."
    return "\n\n".join(parts)


def _join(items: list) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return items[0] + " and " + items[1]
    return ", ".join(items[:-1]) + ", and " + items[-1]
