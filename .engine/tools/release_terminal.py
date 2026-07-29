#!/usr/bin/env python3
"""Release publisher — the terminal cut.

The complement to release_cut.py: where `release_cut` decides + records the next version into the
manifests (the produce side that lands on `main` through the maintainer's merged release PR), THIS
tool runs AFTER that merge and publishes the version — a git tag and a GitHub Release **at the exact
reviewed merge commit** — then tells the maintainer, in plain language on the PR he just merged, that
it happened (or that it did not finish, and how to finish it). It is driven by
`.github/workflows/release-publish.yml` on a `release/*` PR merge.

Why the two halves are separate tools: they run at different points in the merge lifecycle and under
different trust — `release_cut` runs pre-merge under the release PAT; this runs post-merge under the
default `GITHUB_TOKEN` on a commit already trusted (it landed on protected `main`). They share no
state, so folding publish into `release_cut` would couple two things that must stay separable.

The invariants this enforces:
  * The tag lands on the EXACT reviewed merge commit. `POST /releases` with `target_commitish` is
    documented "unused if the git tag already exists," so a dangling tag at a wrong commit would be
    silently honored — defeating the guarantee. Instead the tag is created via the Git Data API
    (`POST /git/refs`, pinned to the merge SHA — the `memory/backup_vault._create_tag` primitive), and
    an existing tag's REAL commit is resolved (`/git/ref/tags/…`, annotated tags dereferenced) and
    compared: a tag on a different commit is REFUSED, never overwritten.
  * Atomic-or-loudly-incomplete. Tag-create and Release-create are two steps; a re-run CONVERGES —
    keyed on the tag's real resolved SHA it completes a half-done publish (tag made, Release missing)
    rather than double-failing, and a fully-done publish is a clean no-op. Every non-success outcome —
    a refusal, an unfinished publish, or a transient read/transport failure — exits non-zero with a
    plain-language recovery (no traceback) AND (when a PR number is present) posts that recovery to the
    merged PR, because the Actions run log is not where a non-engineer looks after merging.
  * The version actually moved. Before publishing, the version is checked strictly-greater than the
    repo's current `/releases/latest` (first cut — no latest — is allowed). This makes "it moved" a
    real gate, not an accident of the idempotency probe, and refuses an arbitrary/typo version or a
    stale out-of-order cut that would otherwise become the release every instance auto-pulls.

The write reuses the guardrail-safe seam: requests are built through the shared `github_client`
(carrying the off-host guard) and executed by this tool's own `urlopen` with a `(status, json)` return
— the `issue_conformance_ci` / `telemetry` idiom — so `github_client` (guardrail-class) is untouched.
The version grammar / ordering / gate-path prose are reused from `release_cut`, one home, no drift.

CLI (driven by the workflow; the network boundary is the injectable `transport` seam a test replaces):
  python tools/release_terminal.py publish --commit <merge_sha> --pr <number>
    env: GITHUB_REPOSITORY (owner/repo to publish into), GITHUB_TOKEN (contents+PR write)
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request

import github_client
import module_coherence
import release_cut

USER_AGENT = "engine-release-terminal"
SENTINEL = release_cut.SENTINEL


class PublishError(Exception):
    """A GitHub call the publish depends on failed in a way that is NOT a clean 404 — an unreachable host
    or an unexpected status. NEVER swallowed as success: it surfaces as a loud non-zero exit + a plain
    recovery on the PR, so a failed-or-unverifiable publish is visible, never a silent split-brain."""


# --------------------------------------------------------------------------- the GitHub boundary
class TerminalCutClient:
    """The publish client over one repo + token. Mirrors issue_conformance_ci's injectable-transport seam:
    `transport(method, path, body) -> (status, json|None)` is injectable, so the demo and tests fake ONLY
    the network and run the REAL publish logic offline. Requests are built through the shared
    `github_client` (the off-host guard); an unreachable host raises PublishError (a publish that cannot be
    verified is loud, never assumed done)."""

    def __init__(self, repo: str, token: str, *, transport=None):
        self.repo = repo
        self.token = token
        self._transport = transport or self._http

    def _http(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = github_client.request(path, self.token, user_agent=USER_AGENT, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:            # 4xx/5xx — surface the status, never swallow
            return exc.code, None
        except urllib.error.URLError as exc:              # unreachable host — a publish we cannot verify
            raise PublishError(f"GitHub is unreachable: {exc}") from exc

    def latest_release_tag(self) -> str | None:
        """The repo's latest published Release tag, or None when there is NO published release yet (a clean
        404 — the first-cut case). A non-404 failure raises (an unverifiable 'is this newer?' must be loud)."""
        status, data = self._transport("GET", f"/repos/{self.repo}/releases/latest", None)
        if status == 404:
            return None
        if not isinstance(status, int) or status >= 400:
            raise PublishError(f"could not read the latest release (GitHub returned {status})")
        return (data or {}).get("tag_name")

    def tag_commit_sha(self, tag: str) -> str | None:
        """The commit SHA a tag ref resolves to, or None when the tag does not exist (a clean 404). An
        annotated tag (`object.type == 'tag'`) is dereferenced to its target commit, so the returned SHA is
        always a commit — the value compared against the merge commit."""
        status, data = self._transport("GET", f"/repos/{self.repo}/git/ref/tags/{tag}", None)
        if status == 404:
            return None
        if not isinstance(status, int) or status >= 400 or not data:
            raise PublishError(f"could not read the tag {tag} (GitHub returned {status})")
        obj = data.get("object") or {}
        sha, kind = obj.get("sha"), obj.get("type")
        if kind == "tag":                                 # annotated tag -> deref to the commit it points at
            s2, d2 = self._transport("GET", f"/repos/{self.repo}/git/tags/{sha}", None)
            if not isinstance(s2, int) or s2 >= 400 or not d2:
                raise PublishError(f"could not dereference the annotated tag {tag} (GitHub returned {s2})")
            sha = (d2.get("object") or {}).get("sha")
        return sha

    def create_tag_ref(self, tag: str, commit_sha: str) -> "int | None":
        """Create refs/tags/<tag> -> commit_sha via the Git Data API (pins the EXACT commit; the
        memory/backup_vault._create_tag primitive). Returns the HTTP status: 200/201 created; 409/422 a name
        collision (a ref already exists — the caller re-resolves and compares, never overwrites)."""
        status, _ = self._transport("POST", f"/repos/{self.repo}/git/refs",
                                     {"ref": f"refs/tags/{tag}", "sha": commit_sha})
        return status

    def release_exists(self, tag: str) -> bool:
        """Whether a published Release object exists for `tag` (the artifact the updater's `/releases/latest`
        reads — a bare tag is invisible to it, so this, not tag-existence, is the 'already published' test)."""
        status, _ = self._transport("GET", f"/repos/{self.repo}/releases/tags/{tag}", None)
        if status == 404:
            return False
        if not isinstance(status, int) or status >= 400:
            raise PublishError(f"could not read the release for {tag} (GitHub returned {status})")
        return True

    def create_release(self, tag: str, name: str, body: str) -> "int | None":
        """Create the published Release for an ALREADY-EXISTING tag (so `target_commitish` is moot — the tag
        is already pinned to the reviewed commit). Returns the HTTP status."""
        status, _ = self._transport("POST", f"/repos/{self.repo}/releases",
                                     {"tag_name": tag, "name": name, "body": body})
        return status

    def post_pr_comment(self, number: int, body: str) -> "int | None":
        """Post a plain-language comment to the merged release PR (a PR is an Issue for the comments API).
        Returns the HTTP status; the caller treats a failure as a legibility gap to note, not a publish
        failure (the published Release is the durable success artifact)."""
        status, _ = self._transport("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})
        return status


# --------------------------------------------------------------------------- release notes + PR comment prose
def _proposal_for_publish(baseline=None, baseline_tree=None, target=None, product=False, slug=None):
    """The release proposal (change inventory + interface impacts + floor level + the merged-PR work list) for
    the published Release notes, recomputed from the merged tree against the prior release (the same
    `release_cut.classify` the cut ran). BEST-EFFORT by design: any anomaly — an unreachable home, a malformed
    tarball, or a first-cut baseline where a prior release MUST exist at publish — yields None so the notes
    degrade to the minimal version + readiness line and NEVER block or crash the publish. This matters because
    the git tag is already created by the time the notes render (publish() step 4); a notes failure must not
    strand a tag with no Release and no PR comment.

    `target` is the merged commit being published (the generate-notes head for the pull-request list).
    `baseline`/`baseline_tree` are injectable so tests run offline: the recompute reaches GitHub through
    `module_manager` (NOT this tool's `transport` seam), so an injected local tree is the only offline path,
    and the pull-request list is skipped unless a `target` is passed. In PRODUCT-mode (#516) the baseline is
    the DEPLOYED repo's own last release (`slug`) and there is no capability tree to diff — the proposal is the
    product proposal (merged PRs beside the version), so the published notes describe the PRODUCT release."""
    try:
        if product:
            b = baseline if baseline is not None else release_cut._product_baseline(slug)
            current = release_cut.read_product_version()
            if not isinstance(current, str):        # absent / malformed at publish -> a safe display default
                current = "0.0.0"
            merged = release_cut.merged_pr_titles(b.ref, target, repo=slug) if target else []
            return release_cut._product_proposal(b, current, merged)
        b = baseline if baseline is not None else release_cut.resolve_baseline()
        if b.first_cut:
            # The GENUINE first release: the home has no prior release yet (the new tag is created after these
            # notes render), so first-cut is the CORRECT state, not an anomaly. There is nothing to diff, but
            # classify still authors the first-release framing line — render that (offline, no tree), so the
            # first published Release is not barer than the pull request that was merged.
            proposal = release_cut.classify(b, None)
        else:
            tree, cleanup = release_cut._baseline_tree_for(b, baseline_tree)
            try:
                proposal = release_cut.classify(b, tree)
            finally:
                if cleanup:
                    shutil.rmtree(cleanup, ignore_errors=True)
        # the body of work: the pull requests merged since the last release (best-effort; [] on the first cut,
        # when no `target` is passed, or on any failure — the section is then simply omitted).
        proposal["merged_prs"] = release_cut.merged_pr_titles(b.ref, target) if target else []
        return proposal
    except Exception:                     # noqa: BLE001 — any failure degrades to minimal notes, never blocks
        return None


def _release_notes(tag: str, proposal=None) -> str:
    """The published Release's notes. A DERIVED VIEW of the same signals the release PR body renders — one
    source (the proposal recomputed from the MERGED tree), never a second history store (eADR-0014). Because
    it is recomputed from the merged tree it describes what actually shipped and can legitimately differ from
    the cut-time PR body if `main` moved between the cut and the merge. `proposal` None (the best-effort
    fallback when the recompute could not run) degrades to the minimal version + readiness line. This renderer
    is PURE (no network) — the network recompute stays at the CLI edge, off the publish logic tests run
    offline. The structure (breaking callout, 'What changed', 'Interface changes to read' with descriptions)
    lives in `release_cut.render_release_notes` — one home for release-notes prose, no drift."""
    return release_cut.render_release_notes(tag, proposal)


def _rerun_hint(run_url: "str | None") -> str:
    """How to finish an unfinished publish, in terms a non-engineer can act on: a direct link to re-run when
    the workflow passed its run URL, else where to find the control (the Actions tab is not where he starts)."""
    if run_url:
        return f" To finish it, re-run this workflow here: {run_url} — re-running is safe."
    return (" To finish it, open this repository's **Actions** tab, find the most recent "
            "\"Publish a merged release\" run, and re-run it — re-running is safe.")


def _comment_body(result: dict, run_url: "str | None" = None) -> str:
    """The plain-language comment posted back to the merged release PR — the legible surface a non-engineer
    actually sees after merging (the Actions run log is not). One home for the release/failure prose. Every
    non-success outcome — including a transient read/transport failure (`errored`) — lands here with a
    recovery, so the legibility promise holds on the PR for every path, not only the decided refusals. In a
    deployed repo (`product`) it speaks of the PRODUCT release, not the engine (#516)."""
    tag = result.get("tag") or "the new version"
    product = bool(result.get("product"))
    if result.get("published"):
        if result.get("reason") == "already-published":
            noun = "Release" if product else "Engine version"
            return (f"**{noun} {tag} was already published**, so nothing changed — there is nothing "
                    f"for you to do.")
        if product:
            return (f"**Release {tag} is now published.** Your product's release {tag} is live. "
                    f"You do not need to do anything else.")
        return (f"**Engine version {tag} is now released.** Your instances can upgrade to it. "
                f"You do not need to do anything else.")
    # the "did not finish" family — a failed tag/Release step, or a transient failure while checking state.
    # The version merged but no release landed; the maintainer is told, on the PR, how to finish it safely.
    if result.get("reason") in ("tag-create-failed", "release-create-failed", "errored"):
        return (f"**The version was merged, but publishing {tag} did not finish.** {result.get('message', '')}"
                f"{_rerun_hint(run_url)} Nothing else you did is lost.")
    if result.get("reason") == "nothing-to-publish":
        return (f"**No release was published.** {result.get('message', '')} This is expected if this branch "
                f"did not set a new {'product' if product else 'engine'} version.")
    # a loud refusal (not newer than the latest release, or a tag already on a different commit)
    return (f"**This merge did not publish a release.** {result.get('message', '')} "
            f"{result.get('recovery', '')}").strip()


# --------------------------------------------------------------------------- the publish decision
def publish(client: TerminalCutClient, engine_release: str, commit_sha: str, proposal=None) -> dict:
    """Publish `engine_release` at `commit_sha` (idempotent, exact-commit, raise-only). Returns a result
    dict carrying `published`, a `reason`, the `tag`, and plain-language `message`/`recovery`. `proposal` is
    the pre-computed release proposal for the Release notes (the network recompute lives at the CLI edge, so
    this decision stays offline-testable); None publishes the minimal notes. Writes only
    a git tag + a Release; refuses loudly (never silently) on any doubt."""
    # 1. version grammar first — a malformed string (which may itself contain a '-') is 'invalid', distinct
    #    from a well-formed pre-release. In the real flow release_cut.apply already enforced this; the check
    #    here means a hand-edited/garbage engine.json is named plainly rather than mis-called a pre-release.
    if not release_cut._valid_version(engine_release):
        return {"published": False, "reason": "invalid-version", "tag": None,
                "message": f"the recorded engine version '{engine_release}' is not a valid version number.",
                "recovery": "the version in .engine/engine.json must be a dotted-number version like 0.1.0."}

    # 2. sentinel / pre-release -> a loud no-op (a stray release/* merge that never ran the cut writer, or a
    #    well-formed pre-release that is not a real release). Not a failure — there is nothing to publish.
    if engine_release == SENTINEL or release_cut._is_prerelease(engine_release):
        return {"published": False, "reason": "nothing-to-publish", "tag": None,
                "message": f"the recorded engine version is '{engine_release}', which is not a released "
                           f"version, so nothing was published."}

    tag = f"v{engine_release}"

    # 3. the version must have actually MOVED — at least as high as the current latest release (first cut, no
    #    latest, is allowed). A STRICTLY OLDER version is refused (an arbitrary/typo version or a stale
    #    out-of-order cut that would otherwise become the release every instance auto-pulls). A version EQUAL
    #    to the latest is allowed through to the idempotency checks below: it is either a clean re-run of this
    #    same publish (already-published) or the same version re-cut on a different commit (a tag conflict) —
    #    both handled there, so this guard must not pre-empt the idempotent no-op with a false "not newer".
    latest = client.latest_release_tag()
    if latest is not None:
        lb = _bare(latest)
        newer = release_cut._strictly_greater(engine_release, lb)
        same = release_cut._release_tuple(engine_release) == release_cut._release_tuple(lb)
        if not (newer or same):
            return {"published": False, "reason": "not-newer", "tag": tag,
                    "message": f"version {tag} is older than the current latest release {latest}, so it was "
                               f"not published (a release can only ever go up).",
                    "recovery": "if this should be a new release, run the release again with a higher version number."}

    # 4. the tag must land on the EXACT reviewed merge commit (Git Data API, not target_commitish).
    existing_sha = client.tag_commit_sha(tag)
    if existing_sha is None:
        status = client.create_tag_ref(tag, commit_sha)
        if status in (200, 201):
            existing_sha = commit_sha                     # created at the exact commit
        elif status in (409, 422):                        # lost a race — a ref appeared; re-resolve + compare
            existing_sha = client.tag_commit_sha(tag)
            if existing_sha is None:
                return {"published": False, "reason": "tag-create-failed", "tag": tag,
                        "message": f"the tag {tag} could not be created (GitHub returned {status}).",
                        "recovery": "re-run this workflow run to finish publishing."}
        else:
            return {"published": False, "reason": "tag-create-failed", "tag": tag,
                    "message": f"the tag {tag} could not be created (GitHub returned {status}).",
                    "recovery": "re-run this workflow run to finish publishing."}
    if existing_sha != commit_sha:                        # a tag for this version exists on a DIFFERENT commit
        return {"published": False, "reason": "tag-conflict", "tag": tag,
                "message": f"a tag {tag} already exists on a different commit ({existing_sha[:12]}) than this "
                           f"merge ({commit_sha[:12]}), so nothing was published — the existing tag was left "
                           f"untouched.",
                "recovery": "resolve which commit should carry this version before publishing it."}

    # 5. ensure the published Release (the tag now exists at the correct commit). Idempotent: an existing
    #    Release is a clean no-op — a re-run after a half-done publish completes it here.
    if client.release_exists(tag):
        return {"published": True, "reason": "already-published", "tag": tag, "commit": commit_sha,
                "message": f"{tag} is already released."}
    status = client.create_release(tag, tag, _release_notes(tag, proposal=proposal))
    if status not in (200, 201):
        return {"published": False, "reason": "release-create-failed", "tag": tag,
                "message": f"the tag {tag} was created at this commit, but the release could not be published "
                           f"(GitHub returned {status}).",
                "recovery": "re-run this workflow run to finish publishing the release."}
    return {"published": True, "reason": "published", "tag": tag, "commit": commit_sha,
            "message": f"{tag} is now released."}


def _bare(tag: str) -> str:
    """A tag string with a single leading 'v' stripped for version comparison (the tags are v-prefixed; the
    ordering in release_cut compares bare version numbers)."""
    return tag[1:] if tag.startswith("v") else tag


def run(client: TerminalCutClient, engine_release: str, commit_sha: str, pr_number: "int | None",
        run_url: "str | None" = None, proposal=None, product: bool = False) -> dict:
    """Publish, then announce the outcome on the merged PR (both success AND failure — the legibility
    surface). A comment failure is NOTED, never allowed to flip the publish verdict: the published Release
    is the durable success artifact, and a missing comment is a legibility gap, not a publish failure.
    `proposal` is the pre-computed Release-notes proposal, threaded to `publish` (None => minimal notes).
    `product` marks a deployed repo's product release (#516) so the PR comment speaks of the product."""
    try:
        result = publish(client, engine_release, commit_sha, proposal=proposal)
    except PublishError as exc:
        # a read/transport failure mid-publish (an unreachable host or an unexpected status while checking
        # the release/tag state) — the most likely real-world failure, a transient GitHub blip. Convert it to
        # a loud result so the recovery STILL reaches the merged PR (the legibility promise holds for this
        # path too, not only the decided refusals — the raise otherwise reaches only the Actions log).
        result = {"published": False, "reason": "errored", "tag": None,
                  "message": "publishing did not finish — GitHub could not be reached or returned an "
                             "unexpected error while checking the release.",
                  "recovery": "re-running is safe and nothing you did is lost.",
                  "error_detail": str(exc)}
    result["product"] = product   # the comment renderer speaks of the product vs the engine (#516)
    if pr_number:
        try:
            status = client.post_pr_comment(pr_number, _comment_body(result, run_url))
            result["announced"] = isinstance(status, int) and status < 400
            if not result["announced"]:
                result["announce_error"] = f"GitHub returned {status} posting the comment"
        except PublishError as exc:
            result["announced"] = False
            result["announce_error"] = str(exc)
    return result


# --------------------------------------------------------------------------- CLI
def _exit_code(result: dict) -> int:
    """0 for a published release or a deliberate no-op; 1 for any refusal or unfinished publish (a loud stop
    the maintainer must see — a red run plus the PR comment)."""
    if result.get("published") or result.get("reason") == "nothing-to-publish":
        return 0
    return 1


def _cmd_publish(args) -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not repo:
        print("CONFIG ERROR: GITHUB_REPOSITORY is not set; cannot tell which repository to publish into.",
              file=sys.stderr)
        return 2
    if not token:
        print("CONFIG ERROR: GITHUB_TOKEN is not set; cannot authenticate to publish the release.",
              file=sys.stderr)
        return 2
    engine = module_coherence.load_engine_manifest()
    if engine is None:
        print("CONFIG ERROR: the engine manifest (.engine/engine.json) is missing; nothing to publish.",
              file=sys.stderr)
        return 2
    # Which release is being published — the engine's version (construction repo) or the deployed repo's own
    # PRODUCT version (#516). PRODUCT dominates: a present product-version.json (or a downstream deployment)
    # publishes the product. A present-but-malformed product file refuses loudly, never an engine publish.
    mode, ctx = release_cut.release_mode()
    if mode == "refuse":
        print(f"CONFIG ERROR: your product's version file ({release_cut.PRODUCT_VERSION_REL}) could not be read "
              f'— it must be a small JSON file with a version, like {{"version": "0.1.0"}}. Fix it, then re-run. '
              f"Nothing was published.", file=sys.stderr)
        return 2
    product = (mode == "product")
    # In product-mode the version comes from product-version.json at the merged commit; an absent/None value
    # degrades to the dev sentinel, which publish() turns into a clean "nothing to publish" no-op (a stray
    # release/* merge that carried no product version).
    engine_release = (ctx["current"] or SENTINEL) if product else engine.get("engine_release", SENTINEL)
    pr_number = int(args.pr) if args.pr else None
    run_url = os.environ.get("RUN_URL", "").strip() or None   # the workflow's own run URL, for a re-run link

    # The Release-notes proposal is recomputed HERE, at the CLI edge — best-effort, so a network hiccup
    # yields the minimal notes and never blocks the publish (which has already created the tag by then). The
    # merged commit is the head for the merged-PR list.
    proposal = _proposal_for_publish(target=args.commit, product=product, slug=ctx["slug"])

    client = TerminalCutClient(repo, token)
    result = run(client, engine_release, args.commit, pr_number, run_url=run_url, proposal=proposal,
                 product=product)

    # plain-language outcome to the run log (the PR comment carries the same words to the maintainer)
    print(result.get("message", ""))
    if result.get("recovery"):
        print(f"To fix: {result['recovery']}")
    if result.get("announce_error"):
        print(f"(could not post the outcome to the pull request: {result['announce_error']})", file=sys.stderr)
    return _exit_code(result)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="release_terminal.py",
                                 description="Publish the tag + GitHub Release for a merged release.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pub = sub.add_parser("publish", help="publish the tag + Release at the merged commit (idempotent)")
    pub.add_argument("--commit", required=True, help="the reviewed merge commit SHA to tag")
    pub.add_argument("--pr", help="the merged pull request number to comment the outcome onto")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "publish":
            return _cmd_publish(args)
    except Exception as exc:  # plain-language failure, never a traceback
        print(f"\nRELEASE PUBLISH ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
