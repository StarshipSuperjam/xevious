#!/usr/bin/env python3
"""The repository-behavior settings leg (issue #541). Turns on the repository settings a new engine repo
should carry that GitHub leaves off by default: delete-branch-on-merge (the engine's own flows create
branches constantly; without it every merge strands one and the session-start card nags about merged work),
the "update branch" button on pull requests (exactly the affordance a non-engineer needs when a sibling
change merges first), and Dependabot's alerts + automatic security-fix pull requests (the engine ships the
Dependabot configuration and a walkthrough for merging its PRs — these are the switches that make either
real). Free on all plans and visibilities.

Same discipline as the security floor beside it: one operator-privileged call per setting, branch on the
HTTP status, verify-after-write and NEVER report a setting on when the write did not confirm, disclose the
outcome in plain language, and never touch the branch ruleset / required checks. Augment-never-override:
each setting is read first and one already on is left exactly as it is (reported as already yours) — and
because GitHub's default for all four is OFF, an off state is indistinguishable from untouched, which the
originating issue's own rule reads as fair to enable; the disclosure names every change so it is one click
to reverse. An organization policy that reserves a Dependabot switch is disclosed as such, never forced.

Reuses the security floor's outcome vocabulary (Toggle + states) and the ruleset bootstrap's
operator-privileged `transport(method, path, body) -> (status, json, headers)` seam — injectable, so tests
and the demo replace ONLY the network and run the real status-branch logic."""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap       # noqa: E402  (transport reuse + BootstrapError; never its ruleset apply)
import security_floor  # noqa: E402  (Toggle, the outcome states, _join — one vocabulary, no drift)

Toggle = security_floor.Toggle
ON = security_floor.ON
ALREADY = security_floor.ALREADY
UNSUPPORTED = security_floor.UNSUPPORTED
UNVERIFIED = security_floor.UNVERIFIED
FAILED = security_floor.FAILED

# The one unlock category this leg adds: a Dependabot switch an organization's policy reserves.
ORG_CONTROLLED = "org-controlled"
# The turn-OFF outcomes (#541 item 4): a surface a new engine repo doesn't use, disabled on a fresh repo.
OFF = "off"                # was on, just turned off, read-back confirms it off
OFF_ALREADY = "off-already"  # already off — left untouched

# Every honest, non-failure outcome the repo-behavior step may report — the step is "applied" when all of
# its toggles land here (mirrors security_floor._GOOD, extended with the turn-off outcomes).
_GOOD_STATES = (ON, ALREADY, UNSUPPORTED, OFF, OFF_ALREADY)


def all_good(toggles: list) -> bool:
    """True iff every toggle reached an honest, non-failure outcome — the step's `applied` verdict."""
    return all(t.state in _GOOD_STATES for t in toggles)


class RepoBehavior:
    """Enable the four repository-behavior settings, branching on each call's status."""

    def __init__(self, repo: str, token: str, transport=None):
        self.repo = repo
        self.token = token
        self._transport = transport or bootstrap.ControlPlane(repo, token)._http

    def _call(self, method: str, path: str, body=None):
        """One call, returning (status, json) or (None, None) on a transport (network) failure."""
        try:
            status, data, _headers = self._transport(method, path, body)
        except bootstrap.BootstrapError:
            return None, None
        return status, data

    # -- delete-branch-on-merge + the update-branch button (two fields, one repo PATCH) ---------------------

    def enable_merge_hygiene(self) -> list:
        """Both repo-settings booleans in one read → one write → one confirming read. A field already on is
        ALREADY (left untouched — augment, never override); a written field is reported on only when the
        read-back confirms it."""
        fields = {"delete-branch-on-merge": "delete_branch_on_merge",
                  "update-branch": "allow_update_branch"}
        status, data = self._call("GET", f"/repos/{self.repo}", None)
        if status is None or status >= 400 or not isinstance(data, dict):
            return [Toggle(key, UNVERIFIED) for key in fields]
        need = {api for key, api in fields.items() if data.get(api) is not True}
        if not need:
            return [Toggle(key, ALREADY) for key in fields]
        status, _ = self._call("PATCH", f"/repos/{self.repo}", {api: True for api in need})
        if status is None:
            return [Toggle(key, ALREADY if fields[key] not in need else UNVERIFIED) for key in fields]
        if status >= 400:
            return [Toggle(key, ALREADY if fields[key] not in need else FAILED) for key in fields]
        status, data = self._call("GET", f"/repos/{self.repo}", None)
        confirmed = data if (status is not None and status < 400 and isinstance(data, dict)) else {}
        out = []
        for key, api in fields.items():
            if api not in need:
                out.append(Toggle(key, ALREADY))
            else:
                out.append(Toggle(key, ON if confirmed.get(api) is True else UNVERIFIED))
        return out

    # -- Dependabot alerts (PUT /vulnerability-alerts; GET answers 204 on / 404 off) ------------------------

    def enable_dependabot_alerts(self) -> Toggle:
        path = f"/repos/{self.repo}/vulnerability-alerts"
        status, _ = self._call("GET", path, None)
        if status == 204:
            return Toggle("dependabot-alerts", ALREADY)
        status, _ = self._call("PUT", path, None)
        if status is None:
            return Toggle("dependabot-alerts", UNVERIFIED)
        if status == 403:                       # an organization policy reserves this switch
            return Toggle("dependabot-alerts", UNSUPPORTED, ORG_CONTROLLED)
        if status >= 400:
            return Toggle("dependabot-alerts", FAILED)
        status, _ = self._call("GET", path, None)
        return Toggle("dependabot-alerts", ON if status == 204 else UNVERIFIED)

    # -- Dependabot security-fix pull requests (PUT /automated-security-fixes; GET returns {"enabled": …}) --

    def enable_dependabot_fixes(self) -> Toggle:
        path = f"/repos/{self.repo}/automated-security-fixes"
        # Read first, like every other setting in this leg: an already-on switch is left untouched and
        # reported as already yours (the review caught the first draft writing unconditionally and then
        # disclosing a no-op as a change). GitHub 404s this GET when Dependabot alerts are off — in which
        # case fixes cannot be on — so anything but a confirmed `enabled` proceeds to the write.
        status, data = self._call("GET", path, None)
        if status == 200 and isinstance(data, dict) and bool(data.get("enabled")):
            return Toggle("dependabot-fixes", ALREADY)
        status, _ = self._call("PUT", path, None)
        if status is None:
            return Toggle("dependabot-fixes", UNVERIFIED)
        if status == 403:
            return Toggle("dependabot-fixes", UNSUPPORTED, ORG_CONTROLLED)
        if status >= 400:
            return Toggle("dependabot-fixes", FAILED)
        status, data = self._call("GET", path, None)
        if status is None or status >= 400 or not isinstance(data, dict):
            return Toggle("dependabot-fixes", UNVERIFIED)
        return Toggle("dependabot-fixes", ON if bool(data.get("enabled")) else FAILED)

    # -- turn OFF the surfaces a fresh engine repo doesn't use (#541 item 4): wiki, and unused project boards --

    def disable_unused_surfaces(self, disable_wiki: bool, disable_projects: bool) -> list:
        """Turn OFF the requested surfaces — the same read-first / verify-after / augment-never-override
        discipline as the enable legs, in reverse. Only a surface currently ON is written; one already off is
        left untouched (`OFF_ALREADY`). One read → one PATCH of only the on-and-slated fields → one confirming
        read; a field reported OFF only when the read-back confirms it. The CALLER decides WHICH surfaces to
        disable (wiki always on a fresh repo; project boards only when the github-projects-sync module is not
        installed) and passes False for both on a brownfield arrival — so this never overrides an existing
        project's deliberate choice. Returns [] when nothing is slated."""
        fields = {}
        if disable_wiki:
            fields["wiki"] = "has_wiki"
        if disable_projects:
            fields["projects"] = "has_projects"
        if not fields:
            return []
        status, data = self._call("GET", f"/repos/{self.repo}", None)
        if status is None or status >= 400 or not isinstance(data, dict):
            return [Toggle(key, UNVERIFIED) for key in fields]
        need = {api for key, api in fields.items() if data.get(api) is True}   # currently ON -> turn off
        if not need:
            return [Toggle(key, OFF_ALREADY) for key in fields]
        status, _ = self._call("PATCH", f"/repos/{self.repo}", {api: False for api in need})
        if status is None:
            return [Toggle(key, OFF_ALREADY if fields[key] not in need else UNVERIFIED) for key in fields]
        if status >= 400:
            return [Toggle(key, OFF_ALREADY if fields[key] not in need else FAILED) for key in fields]
        status, data = self._call("GET", f"/repos/{self.repo}", None)
        confirmed = data if (status is not None and status < 400 and isinstance(data, dict)) else {}
        out = []
        for key, api in fields.items():
            if api not in need:
                out.append(Toggle(key, OFF_ALREADY))
            else:
                out.append(Toggle(key, OFF if confirmed.get(api) is False else UNVERIFIED))
        return out

    def apply(self, announce=None, *, disable_wiki: bool = False, disable_projects: bool = False) -> list:
        """Enable the four working-comfort settings, and — on a fresh repo — turn OFF the surfaces it doesn't
        use (#541 item 4). Branches on each status, discloses the outcome in plain language. Returns the list
        of Toggles (data). NEVER touches the branch ruleset / required checks, and never changes visibility."""
        say = announce if announce is not None else (lambda text: print(text))
        toggles = self.enable_merge_hygiene() + [self.enable_dependabot_alerts(),
                                                 self.enable_dependabot_fixes()]
        toggles += self.disable_unused_surfaces(disable_wiki, disable_projects)
        say(render(toggles))
        return toggles


# ---- operator-facing disclosure (plain language; never an HTTP status or an API field name) --------------

_HUMAN_ON = {
    "delete-branch-on-merge": ("Merged branches now tidy themselves away — when a change is approved in, its "
                               "work branch is deleted automatically, so finished work stops piling up."),
    "update-branch": ("Pull requests now offer an update button — when another change lands first, one click "
                      "brings a waiting change up to date instead of leaving it stuck."),
    "dependabot-alerts": ("Dependency alerts are on — GitHub tells you when something your project depends "
                          "on has a known security problem."),
    "dependabot-fixes": ("Automatic security fixes are on — when a dependency has a known fix, GitHub opens "
                         "a small pull request with the update, and you approve it like any other change."),
}
_HUMAN_ALREADY = {
    "delete-branch-on-merge": "automatic tidy-up of merged branches",
    "update-branch": "the pull-request update button",
    "dependabot-alerts": "dependency alerts",
    "dependabot-fixes": "automatic security-fix pull requests",
}
_HUMAN_NAME = {
    "delete-branch-on-merge": "Automatic tidy-up of merged branches",
    "update-branch": "The pull-request update button",
    "dependabot-alerts": "Dependency alerts",
    "dependabot-fixes": "Automatic security-fix pull requests",
    "wiki": "The project wiki",
    "projects": "Project boards",
}
_HUMAN_OFF = {
    "wiki": ("The project wiki is now off — a new engine project keeps its documentation in the repository "
             "itself, so the separate wiki isn't needed."),
    "projects": ("Project boards are now off — nothing in this project uses them. (If you add the project-board "
                 "sync later, turn them back on then.)"),
}
_HUMAN_OFF_ALREADY = {
    "wiki": "the project wiki",
    "projects": "project boards",
}


def render(toggles: list) -> str:
    """The bidirectional disclosure: what was just turned on, what was already yours (left untouched), what
    an organization policy reserves, and what couldn't be confirmed. Built ONLY from the decided states —
    never from a GitHub response body or status code."""
    on_lines, already, org_held, unconfirmed, off_lines, off_already = [], [], [], [], [], []
    for t in toggles:
        if t.state == ON:
            on_lines.append(_HUMAN_ON[t.key])
        elif t.state == ALREADY:
            already.append(_HUMAN_ALREADY[t.key])
        elif t.state == UNSUPPORTED:
            org_held.append(_HUMAN_NAME[t.key].lower())
        elif t.state == OFF:
            off_lines.append(_HUMAN_OFF[t.key])
        elif t.state == OFF_ALREADY:
            off_already.append(_HUMAN_OFF_ALREADY[t.key])
        else:  # UNVERIFIED / FAILED
            unconfirmed.append(_HUMAN_NAME[t.key].lower())

    join = security_floor._join
    parts = []
    if on_lines:
        lead = ("One working-comfort setting is now on for your project:" if len(on_lines) == 1
                else "A few working-comfort settings are now on for your project:")
        parts.append(lead + "\n- " + "\n- ".join(on_lines)
                     + "\n\nEach of these is an ordinary repository setting — you can flip any of them "
                       "back at any time on your project's Settings page on GitHub.")
    if off_lines:
        lead = ("A GitHub feature your new project doesn't use is now off:" if len(off_lines) == 1
                else "A couple of GitHub features your new project doesn't use are now off:")
        parts.append(lead + "\n- " + "\n- ".join(off_lines)
                     + "\n\nEach is an ordinary repository setting you can turn back on at any time on your "
                       "project's Settings page on GitHub.")
    if already:
        parts.append("Already set up on your project, left exactly as it was: " + join(already) + ".")
    if off_already:
        parts.append("Already off, left as it was: " + join(off_already) + ".")
    if org_held:
        parts.append("I couldn't turn on " + join(org_held) + " — this usually means your organization's "
                     "own settings reserve "
                     + ("them" if len(org_held) > 1 else "it") + ", so turning "
                     + ("them" if len(org_held) > 1 else "it") + " on is a call for whoever manages the "
                     "organization. If this project isn't in an organization, check the repository's "
                     "security settings on GitHub.")
    if unconfirmed:
        parts.append("I couldn't confirm " + join(unconfirmed) + " turned on, so I'm not reporting "
                     + ("them" if len(unconfirmed) > 1 else "it") + " as on. Please check your repository's "
                     "settings on GitHub, or ask me to run setup again.")
    if not parts:
        return "I didn't change any of your project's repository settings."
    return "\n\n".join(parts)
