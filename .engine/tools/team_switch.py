#!/usr/bin/env python3
"""The permanent operator-privileged solo->team migration (engine-template #408).

Team mode is the "cannot weaken at all" structural close: a distinct, NON-ADMIN GitHub identity (a machine-user
account the operator creates) authors the engine's commits and pull requests, so the operator becomes the
enforced code-owner reviewer — a change cannot merge without their second sign-off — and that identity, holding
no admin, cannot itself weaken the branch-protection ruleset.

The engine CANNOT create a GitHub identity for the operator, so this operation is GUIDE + VERIFY + RECORD +
WIRE + APPLY: the operator (guided by the runbook `.engine/operations/engine-team-switch.md`) creates a second
account, adds it as a non-admin (write) collaborator, and generates a repo-scoped PAT; this tool then verifies
the identity genuinely bites (non-admin + CODEOWNERS names the operator), records the public login in the
manifest, wires the git commit author, and DELEGATES the team-ruleset application to the control-plane bootstrap
(`bootstrap.ControlPlane(tier=TEAM).apply()` — never re-mirrored here). Idempotent (decided by evaluating the
current state, not a ran-flag), verify-after-write, degrade-never-fake.

TWO credentials, by design: applying the ruleset needs repo ADMIN, so `apply`/`reverse` run under the operator's
OWN credential; ongoing build sessions then authenticate as the machine-user PAT to author commits/PRs. The tool
never stores the PAT — it lives in the operator's `gh` credential store.

Reversible: `reverse` (team->solo) drops the identity record and re-applies the solo floor. That is a
WEAKENING (it removes the required approval), so the manifest change it writes routes through the `guardrail-ack`
when committed — the weakening guard's identity-downgrade detector catches it.

  status  — read-only: which tier you're in, and (for a switch in progress) the single next step.
  apply   — perform or resume the switch to team (idempotent). --login <machine-user> [--email <addr>]
  reverse — switch back to solo (idempotent).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap  # noqa: E402  (the ruleset-applying control plane — DELEGATE the apply, never re-mirror it)
import boot  # noqa: E402  (repo_slug + gh_token — the shared GitHub-context helpers)
import protection_guard  # noqa: E402  (SOLO/TEAM + resolve_tier — the single tier home)

SOLO, TEAM = protection_guard.SOLO, protection_guard.TEAM
_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .engine
_MANIFEST = os.path.join(_ENGINE_DIR, "engine.json")
_CODEOWNERS = os.path.join(os.path.dirname(_ENGINE_DIR), ".github", "CODEOWNERS")


class TeamSwitchError(Exception):
    """A GitHub read/transport failure during the switch — surfaced and degraded, never swallowed."""


class TeamSwitch:
    """The solo<->team migration boundary. `transport(method, path, body) -> (status, json)` is injectable so
    tests/demo replace ONLY the network; `cp_factory(tier) -> ControlPlane` is injectable so the delegated
    ruleset apply is exercised without a live ruleset; `run_git` and the manifest path are injectable too."""

    def __init__(self, repo, token, *, transport=None, cp_factory=None, run_git=None, manifest_path=None):
        self.repo = repo
        self.token = token
        self._transport = transport or self._http
        self._cp_factory = cp_factory or (lambda tier: bootstrap.ControlPlane(repo, token, tier=tier))
        self._run_git = run_git or self._git
        self._manifest_path = manifest_path or _MANIFEST

    # -- boundaries (injectable) ------------------------------------------------------------------

    def _http(self, method, path, body=None):
        cp = bootstrap.ControlPlane(self.repo, self.token)   # reuse the authenticated transport
        status, data, _headers = cp._http(method, path, body)
        return status, data

    def _git(self, args):
        return subprocess.run(["git", *args], cwd=os.path.dirname(_ENGINE_DIR),
                              capture_output=True, text=True, timeout=15)

    def _manifest(self) -> dict:
        with open(self._manifest_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _current_tier(self) -> str:
        # Resolve the tier from THIS instance's manifest (injectable), not the process-global one — so tests and
        # the demo see the temp manifest they wrote, and production reads .engine/engine.json.
        return protection_guard.resolve_tier(os.path.dirname(self._manifest_path))

    def _write_manifest(self, data: dict) -> None:
        with open(self._manifest_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")

    # -- verification (verify-the-tier-genuinely-bites) ----------------------------------------

    def collaborator_permission(self, login: str) -> str | None:
        """The machine-user's permission on the repo: 'admin' | 'write' | 'read' | 'none', or None if it is not
        a collaborator (a 404) or the read failed. The identity MUST be a non-admin collaborator: admin would let
        it weaken the ruleset (defeating 'cannot weaken at all'); non-collaborator can't author PRs at all."""
        status, data = self._transport(
            "GET", f"/repos/{self.repo}/collaborators/{login}/permission", None)
        if status == 404:
            return None
        if status >= 400 or not isinstance(data, dict):
            raise TeamSwitchError(f"could not read '{login}' collaborator permission (HTTP {status})")
        return data.get("permission")

    def codeowners_names_operator(self) -> bool:
        """True iff the committed CODEOWNERS names the operator's own handle — the code-owner review requirement
        only bites when the operator owns the changed paths. Reads the operator handle from the manifest."""
        handle = (self._manifest().get("handle") or "").lstrip("@").lower()
        if not handle:
            return False
        try:
            with open(_CODEOWNERS, encoding="utf-8") as fh:
                text = fh.read().lower()
        except OSError:
            return False
        return f"@{handle}" in text

    def unmet_prerequisites(self, login: str) -> list:
        """The operator-facing steps still needed before the switch can complete — empty means ready. Each is a
        plain-language single next action, so the operator is never left guessing which half-done state they are in."""
        gaps = []
        perm = self.collaborator_permission(login)
        if perm is None:
            gaps.append(f"Add the account '{login}' to this repository as a collaborator with write (not admin) "
                        "access — it needs to push branches and open pull requests, but must NOT be an admin "
                        "(an admin could change your safety rules, which team mode exists to prevent).")
        elif perm == "admin":
            gaps.append(f"The account '{login}' is a repository admin. Team mode needs it to be a NON-admin "
                        "(write-only) collaborator — an admin could weaken your safety rules, which is exactly "
                        "what team mode is meant to make impossible. Lower it to write access, then re-run.")
        if not self.codeowners_names_operator():
            gaps.append("Your CODEOWNERS file doesn't name your own account as an owner, so 'a code-owner must "
                        "approve' would have no one to require. Add yourself as an owner of the paths you want to "
                        "review (at least the engine's), then re-run.")
        return gaps

    # -- status -----------------------------------------------------------------------------------

    def status(self, login: str | None = None) -> dict:
        """A read-only report of where the switch stands: the current tier, and — when a login is supplied and
        the repo is still solo — the single next step. Never mutates."""
        m = self._manifest()
        tier = self._current_tier()
        recorded = m.get("engine_identity") or None
        if tier == TEAM:
            return {"tier": TEAM, "identity": recorded,
                    "message": f"You are in team mode. The engine authors changes as "
                    f"'{(recorded or {}).get('login', 'a separate account')}', and your approval is required "
                    "before anything merges. To go back to on-your-own mode, run this with `reverse`."}
        if not login:
            return {"tier": SOLO, "identity": None,
                    "message": "You are in on-your-own (solo) mode. To switch to team mode — where a separate "
                    "account makes changes and your approval is required before they merge — run this with "
                    "`apply --login <the second account's name>`."}
        gaps = self.unmet_prerequisites(login)
        if gaps:
            return {"tier": SOLO, "identity": None, "next": gaps[0], "remaining": gaps,
                    "message": "Not ready to switch yet. Next step:\n  - " + gaps[0]}
        return {"tier": SOLO, "identity": None, "next": None, "remaining": [],
                "message": f"Ready to switch: '{login}' is set up correctly. Run `apply --login {login}` to turn "
                "on team mode."}

    # -- apply / reverse --------------------------------------------------------------------------

    def apply(self, login: str, email: str | None = None, branch: str | None = None) -> dict:
        """Perform or resume the switch to team (idempotent). Refuses to proceed while any prerequisite is unmet
        (returns the single next step — never a half-done switch). On a ready repo: record the identity, wire the
        git author, DELEGATE the team-ruleset application to the control plane, verify, and flip the tier."""
        branch = branch or boot.PROTECTED_BRANCH
        if self._current_tier() == TEAM:
            return {"status": "already", "message": "Already in team mode — nothing to do."}
        gaps = self.unmet_prerequisites(login)
        if gaps:
            return {"status": "blocked", "remaining": gaps,
                    "message": "Can't switch yet — do this first:\n  - " + "\n  - ".join(gaps)}
        email = email or f"{login}@users.noreply.github.com"
        # 1. Apply the team ruleset FIRST (needs your admin credential) — degrade-never-fake before we record.
        cp = self._cp_factory(TEAM)
        try:
            result = cp.apply(branch=branch)
        except bootstrap.BootstrapError as e:
            return {"status": "degraded", "message": f"Couldn't apply the team safety rules ({e}). Nothing was "
                    "changed — try again when you're back online, as an account that administers this repository."}
        if not result.is_protected():
            return {"status": "degraded", "message": bootstrap.render(result)}
        # 2. Wire the git commit author to the machine-user (so the engine's commits are attributed to it).
        self._run_git(["config", "user.name", login])
        self._run_git(["config", "user.email", email])
        # 3. Record the identity + flip the tier in the manifest (a strengthening — the committed change merges
        #    cleanly; the weakening guard only gates the reverse direction).
        m = self._manifest()
        m["identity"] = TEAM
        m["engine_identity"] = {"login": login, "email": email}
        self._write_manifest(m)
        return {"status": "applied", "login": login,
                "message": f"Team mode is on. From now the engine commits as '{login}' and opens pull requests "
                "under it, and your approval is required before anything merges — your next change will show up "
                "for you to review. Commit the updated engine.json to record the switch. Run your build sessions "
                f"signed in as '{login}' (the account whose token you created); you stay signed in as yourself to "
                "review and merge."}

    def reverse(self, branch: str | None = None) -> dict:
        """Switch back to solo (idempotent). Re-applies the solo floor and drops the identity record. This is a
        WEAKENING (it removes the required approval), so the manifest change it writes routes through the
        guardrail-ack when committed — the weakening guard's identity-downgrade detector catches it."""
        branch = branch or boot.PROTECTED_BRANCH
        if self._current_tier() == SOLO:
            return {"status": "already", "message": "Already in on-your-own (solo) mode — nothing to do."}
        cp = self._cp_factory(SOLO)
        try:
            result = cp.apply(branch=branch)
        except bootstrap.BootstrapError as e:
            return {"status": "degraded", "message": f"Couldn't re-apply the solo safety rules ({e}). Nothing was "
                    "changed — try again when you're back online."}
        m = self._manifest()
        m["identity"] = SOLO
        m.pop("engine_identity", None)
        self._write_manifest(m)
        return {"status": "reversed",
                "message": "Back in on-your-own (solo) mode: the engine commits as you again, and the required "
                "second approval is off. Commit the updated engine.json — because this removes a protection, that "
                "pull request will ask you to apply the `guardrail-ack` label to confirm you mean it."}


# ---- CLI ----------------------------------------------------------------------------------------

def _flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def main(argv) -> int:
    cmd = argv[0] if argv else "status"
    repo, token = boot.repo_slug(), boot.gh_token()
    if cmd == "demo":
        return _demo()
    if not repo or not token:
        print("Can't do this from here — no repository access is available. Run this where you're logged in to "
              "GitHub (`gh auth login`).")
        return 1
    ts = TeamSwitch(repo, token)
    try:
        if cmd == "status":
            print(ts.status(_flag(argv, "--login")).get("message", ""))
            return 0
        if cmd == "apply":
            login = _flag(argv, "--login")
            if not login:
                print("Which account should make the engine's changes? Pass `apply --login <account>`.")
                return 1
            res = ts.apply(login, _flag(argv, "--email"))
            print(res.get("message", ""))
            return 0 if res.get("status") in ("applied", "already") else 1
        if cmd == "reverse":
            res = ts.reverse()
            print(res.get("message", ""))
            return 0 if res.get("status") in ("reversed", "already") else 1
    except TeamSwitchError as e:
        # A transient GitHub read (rate-limit / outage) while checking the account — degrade in plain language,
        # never a raw traceback for a non-engineer. Nothing was changed; the fail-closed path leaves solo intact.
        print(f"Couldn't reach GitHub to check your setup ({e}). Nothing changed — try again when you're back "
              "online.")
        return 1
    print(__doc__)
    return 0


def _demo() -> int:
    """Self-check on a temp manifest with a fake GitHub + control plane: the switch is blocked until the identity
    is a non-admin collaborator AND CODEOWNERS names the operator, then applies, records the identity, and
    reverses — never touching a live ruleset or the real manifest."""
    import tempfile
    from bootstrap import Result

    class _FakeCP:
        def apply(self, branch=None):
            return Result("applied", branch or "main", [], None, True)

    ok = True
    with tempfile.TemporaryDirectory() as d:
        mpath = os.path.join(d, "engine.json")
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump({"engine_release": "0.0.0", "packages": {}, "identity": "solo", "handle": "owner"}, fh)
        perms = {"perm": None}
        codeowners = {"has": False}

        def fake_transport(method, path, body=None):
            return (404 if perms["perm"] is None else 200), (
                None if perms["perm"] is None else {"permission": perms["perm"]})

        ts = TeamSwitch("o/r", "tok", transport=fake_transport, cp_factory=lambda tier: _FakeCP(),
                        run_git=lambda args: None, manifest_path=mpath)
        ts.codeowners_names_operator = lambda: codeowners["has"]

        blocked = ts.apply("bot", branch="main")
        ok &= blocked["status"] == "blocked" and "collaborator" in blocked["message"].lower()
        perms["perm"] = "admin"
        admin_blocked = ts.apply("bot", branch="main")
        ok &= admin_blocked["status"] == "blocked" and "admin" in admin_blocked["message"].lower()
        perms["perm"] = "write"
        no_co = ts.apply("bot", branch="main")
        ok &= no_co["status"] == "blocked" and "codeowners" in no_co["message"].lower()
        def _read():
            with open(mpath, encoding="utf-8") as fh:
                return json.load(fh)

        codeowners["has"] = True
        applied = ts.apply("bot", "bot@example.invalid", branch="main")
        ok &= applied["status"] == "applied"
        ok &= _read()["identity"] == "team"
        ok &= _read()["engine_identity"]["login"] == "bot"
        again = ts.apply("bot", branch="main")
        ok &= again["status"] == "already"
        reversed_ = ts.reverse(branch="main")
        ok &= reversed_["status"] == "reversed"
        ok &= _read()["identity"] == "solo"
        ok &= "engine_identity" not in _read()

    print("team-switch self-check:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
