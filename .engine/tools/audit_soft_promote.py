#!/usr/bin/env python3
"""Promote a standing length-budget soft finding to a tracked engine Issue (issue #273 half 2).

The soft-findings feed lets the weekly self-review SEE the firing soft validator findings; this closes the loop by giving
a standing one a durable home — a deduped, lane-aware engine-labelled Issue — so it reaches boot and the
issue tracker even when no one reads the digest closely. It runs a LIVE-DERIVED telemetry pass:
open/update an Issue for each over-budget surface AND auto-resolve the Issue of a surface seen back under
its budget — telemetry's standard source-scoped resolution, confined to `soft-budget:` sids so it can never
close another source's Issue. This SUPERSEDES the original open/update-only stance: once telemetry gained a
safe source-scoped auto-resolve, a trimmed file self-closing its own stale Issue is the honest
behaviour and the same as every other engine health signal (CI, ambient, episodic). Auto-resolve clears the
FLAG — it never repairs the file; trimming or raising the budget is still work someone does.

SCOPE — length-budget nudges ONLY. The report-only `audit-prep` suite also carries other soft findings
(e.g. the audit-digest staleness warning) whose remedy is different and which already have an escalation
path; promoting them under a budget framing would misdescribe them. They are excluded by provenance: only
findings emitted by a `shape`-kind rule (the length-budget nudge) are promoted.

LANE — a budget overage on a TEMPLATE-OWNED file (machinery) cannot be durably fixed in a DOWNSTREAM copy: the
next engine update replaces the engine's own files wholesale, so a local trim is overwritten. The Issue says so
plainly and points the durable fix UPSTREAM to the engine-template project — and the engine never files that
upstream report itself ("never phone home"); logging it there stays the operator's call. The EXCEPTION is the
engine's OWN home repo (the target repo's slug equals the recorded home_repository): there the machinery IS the
project, so the same file is fixable right here (trim it, or raise its recorded budget), and pointing "upstream"
would point the repo at itself — so the home lane says fix-here instead. Home-ness is the strict-positive
target-slug-equals-home test (_is_home_repo compares the target repo's own slug to the recorded home, NOT the
git origin, and fails toward the deployed lane so a copy is never mis-told the fix sticks). A
budget overage on a file THIS PROJECT owns (local state, not machinery) is fixable here in either repo.
Ownership is the authoritative machinery test (module_coherence.provides_claims, the live-filesystem manifest
claims). Today every budget-governed surface is module-owned machinery, so the local lane is built and
fixture-tested; the deployed machinery lane fires live when a downstream repo's over-budget doc trips it, and
the home lane fires here.

SAFETY / HONESTY:
- source_id = "soft-budget:<file>" — keyed on the FILE, never the message. The live line-count in the
  message is per-occurrence material; keying on it would fork a new Issue every time the count changed. The
  path of a catalogued surface is a plain repo-relative path (it cannot contain the HTML-comment delimiters
  the tracking marker uses), so it is a stable, collision-free signal id — one Issue per over-budget surface.
- The finding message and file path are author-influenced text that lands in the Issue body, so both are
  defanged with the same neutraliser the soft-findings feed uses before they are embedded.
- A finding with no file location is skipped (it cannot be source-keyed); a length-budget finding always
  carries one.
- Fail-open: any error prints a visible status line and exits 0 — a transient GitHub blip must never fail
  the self-review, and the finding simply re-fires next run (and the soft-findings feed still surfaces it).

CLI (the audit-prep workflow's promote step; needs GITHUB_REPOSITORY + GITHUB_TOKEN with issues:write —
the GitHub token, never the Claude token):
  uv run --directory .engine -- python tools/audit_soft_promote.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate          # noqa: E402  (the collect seam + the prompt-fence defang)
import moment            # noqa: E402  (the trailing-Z time seam; pure stdlib leaf)
import telemetry         # noqa: E402  (the GitHub boundary + the live triage pass + the benign severity class)
import issue_author      # noqa: E402  (the shared engine-Issue body contract)
import module_coherence  # noqa: E402  (provides_claims — the authoritative machinery test)
import knowledge_query   # noqa: E402  (resolve the over-budget surface to its knowledge entity)

# The report-only suite the length-budget nudges join. Read here, never the CI gate.
FEED_SUITE = "audit-prep"
# The dedup namespace for these tracked Issues. Disjoint from telemetry's own health sources
# (`rule:` / `check/`) and the other producers (`close/disposition/`, `migration/version-stamp/`), so this
# producer's Issues never collide with theirs.
SOURCE_PREFIX = "soft-budget:"

# This producer's OWN reconcile-accrual cache, separate from the CI / ambient / episodic stream caches so no
# other local `run` can clobber it. Under the live-derived pass (below) the cache is not RELIED upon — the
# durable Issue set is the truth — but `run` still takes and writes one; on the ephemeral audit runner that
# write is harmless waste (the same reason the CI signal is live-derived, telemetry.py's CI-outcome signal).
DEFAULT_SOFT_BUDGET_STREAMS_PATH = os.path.join(
    validate.ROOT, ".engine", "telemetry", ".cache", "soft-budget-streams.json")


def _neutralize(text: str) -> str:
    """Render author-influenced text inertly in a GitHub issue body. The finding message and the file
    path embed an author-chosen filename, and a budget issue's body is rendered markdown — so neutralise
    both the prompt-fence rails (the soft-findings feed's defang, for when this body is later re-read into a
    persona prompt) AND the markdown/HTML a crafted filename could smuggle: HTML-escape the angle
    brackets and ampersand (no tag, no comment, no forged `<!-- engine-signal -->` tracking marker) and
    backslash-escape the markdown image/link/code characters (no beacon image, link, or code-span
    breakout). A plain repo path passes through untouched."""
    text = validate.defang_prompt_fence_markers(text or "")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\\", "\\\\")
    for ch in ("`", "[", "]", "!"):
        text = text.replace(ch, "\\" + ch)
    return text


def _is_home_repo(repo: str | None) -> bool:
    """True ONLY when the target repo is CONFIRMED to be the engine's own home — its slug equals the recorded
    `home_repository`. Fails toward NOT-home (the deployed/upstream lane): a repo whose home cannot be read, or
    that differs, is treated as a downstream copy. This is the strict-positive target-slug-equals-home test —
    it compares the caller's `repo` STRING (in practice GITHUB_REPOSITORY, the slug the Issues are filed into),
    NOT `repo_identity`'s on-disk git `origin`, and it deliberately fails toward the deployed lane rather than
    inheriting `is_home_repo`'s fail-TOWARD-home: the wrong direction here would tell a real deployed repo whose
    manifest could not be read "fix it here, it sticks", which is false."""
    try:
        return module_coherence.slug_eq(repo, module_coherence.home_repository())
    except Exception:  # noqa: BLE001 — a malformed manifest must never flip us onto the home lane
        return False


def _entity_reference(rel: str, repo: str | None) -> list | None:
    """A single labelled link to the knowledge entity for "what is broken" (a debt Issue
    references knowledge entity-ids so telemetry owns the debt while knowledge stays surface-derived). The
    entity IS the over-budget surface; the link is a GitHub blob permalink to it. `blob/HEAD/` resolves to
    the default branch with NO branch lookup, so it needs no workflow env / extra ack. Returns None (no
    reference — the Issue still opens; a reference is enrichment, never a gate) when there is no repo context,
    the path resolves to no catalogued knowledge entity, or the resolve degrades. The label is neutralised
    (author-influenced path); a catalogued surface path carries no url-breaking characters (budget_records
    already drops an anomalous one), so the url is a plain, safe segment."""
    if not repo:
        return None
    try:
        entities = knowledge_query.find(path_glob=rel)
    except Exception:  # noqa: BLE001 — a knowledge-read failure must never break promotion
        return None
    if not entities:
        return None
    url = f"https://github.com/{repo}/blob/HEAD/{rel}"
    return [(_neutralize(f"The file that is over its length limit — {rel}"), url)]


# The closing bullet every lane ends on — the self-review + auto-close reassurance. Defined once so a future
# wording change lands in one place, not three (technical-integrity review of #658).
_SELF_REVIEW_TRAILER = (
    "- This is also noted in your weekly self-review. The engine will **close this tracking issue "
    "on its own** once the file is back under its limit — that only clears the flag, it does not "
    "change the file for you."
)


def _render(rel: str, message: str, machinery: bool, *, repo: str | None = None,
            home: bool = False) -> tuple:
    """The lane-aware (title, body_core) for one over-budget surface. `rel` is the raw repo path (the
    title is plain text, not rendered markdown) and `message` is the raw finding message; both the path
    and the message are neutralised before they enter the rendered body. body_core is prose only —
    telemetry appends its tracking trailers + signal marker. When the surface resolves to a knowledge
    entity, a blob-permalink reference to it is folded in.

    Three lanes: an engine machinery file in a DOWNSTREAM copy points the durable fix UPSTREAM (a local
    trim is overwritten by the next update); the SAME machinery file in the engine's OWN home repo
    (`home=True`) is fixable right here, because here the engine's source IS the project; a file this
    project owns (not machinery) is always fixable here."""
    where = _neutralize(rel)
    message = _neutralize(message)
    title = f"Engine length budget: {rel} is over its limit"
    if machinery and not home:
        what_this_is = (
            f"The engine noticed one of its OWN files has grown past the length it is meant to stay "
            f"within. {message}\n\n"
            f"- **What it is:** engine machinery — a file the engine itself ships and maintains, not a "
            f"file in your project. (A shorter file is easier for a fresh AI session to read in full, "
            f"which is why the limit exists.)\n"
            f"- **Where:** `{where}`."
        )
        whats_next = (
            "Trimming it here will NOT last: the next engine update replaces the engine's own files "
            "wholesale, so a local edit to this one is overwritten on the next upgrade.\n\n"
            "- **To fix it durably,** raise it in the engine-template project this engine was created "
            "from — the project you (or whoever set up this engine) used GitHub's \"Use this template\" "
            "on. If you are not sure where that is, whoever set the engine up will know.\n"
            "- **Or leave it** — it is only a nudge and never blocks anything.\n"
            "- The engine has not sent anything to that upstream project and will not; logging it there "
            "is yours to decide.\n"
            + _SELF_REVIEW_TRAILER
        )
    elif machinery and home:
        what_this_is = (
            f"The engine noticed one of its OWN files has grown past the length it is meant to stay "
            f"within. {message}\n\n"
            f"- **What it is:** engine machinery — and this repository IS the engine's own source "
            f"project, so a fix here is durable (it ships to every engine made from this one). (A shorter "
            f"file is easier for a fresh AI session to read in full, which is why the limit exists.)\n"
            f"- **Where:** `{where}`."
        )
        whats_next = (
            "Because this is the engine's own source, a fix here lasts — there is no upstream to send it "
            "to. You can:\n\n"
            "- **Trim it** in an ordinary change, or\n"
            "- **Raise its recorded budget** with a reason in the length-budget rule (an edit to a "
            "protected settings file, which you sign off on deliberately), or\n"
            "- **Leave it** — it is only a nudge and never blocks anything.\n"
            + _SELF_REVIEW_TRAILER
        )
    else:
        what_this_is = (
            f"The engine noticed one of your project's files has grown past the length it is meant to stay "
            f"within. {message}\n\n"
            f"- **What it is:** a file your project owns — fixable right here.\n"
            f"- **Where:** `{where}`."
        )
        whats_next = (
            "You can:\n\n"
            "- **Trim it** in an ordinary change, or\n"
            "- **Raise its budget** with a recorded reason, or\n"
            "- **Leave it** — it is only a nudge and never blocks anything.\n"
            + _SELF_REVIEW_TRAILER
        )
    body_core = issue_author.render_engine_issue_body(
        what_this_is=what_this_is, whats_next=whats_next, references=_entity_reference(rel, repo))
    return title, body_core


def budget_records(now: str, *, claims: dict | None = None, repo: str | None = None) -> list:
    """One finding-record per firing length-budget nudge — soft, kind `shape`, with a file location —
    each carrying a lane-aware `title` + `body_core` ready for the live triage pass. Lane is the
    authoritative machinery test: a file a present module manifest claims is overlaid on every upgrade.
    `claims` (the {relpath: [owner,...]} ownership map) is injectable so the demo/tests can exercise both
    lanes on a real over-budget finding without mutating shipped files; by default it is computed live.
    `repo` (owner/name) is threaded to the body render for the entity permalink; None omits it."""
    findings = validate.collect(FEED_SUITE, {}, with_source=True)
    if claims is None:
        claims = module_coherence.provides_claims(module_coherence.discover_manifests())
    home = _is_home_repo(repo)
    records = []
    for f in findings:
        if f.get("severity") == "hard":
            continue
        if f.get("source_kind") != "shape":          # only the length-budget nudge (not e.g. staleness)
            continue
        rel = (f.get("location") or {}).get("file")
        if not rel:                                  # cannot source-key a location-less finding
            continue
        if any(c in rel for c in "<>\n\r"):
            # An anomalous path that could break the tracking marker the source_id is embedded in (a real
            # catalogued surface path never contains these). Skip it rather than corrupt dedup; defence in
            # depth alongside the body neutralisation below.
            continue
        machinery = bool(claims.get(rel))            # claimed by a manifest's provides => machinery
        title, body_core = _render(rel, f.get("message", ""), machinery, repo=repo, home=home)
        records.append({
            "source_id": f"{SOURCE_PREFIX}{rel}",
            "severity": telemetry.PERSISTENT_BENIGN,
            "message": _neutralize(f.get("message", "")),
            "location": {"file": rel},
            "title": title,
            "body_core": body_core,
        })
    return records


def budget_surfaces() -> set:
    """Every file the length-budget (`shape`) rules EVALUATE this pass — the full budget-governed surface set,
    over AND under budget — as `soft-budget:<rel>` source-ids. This is the `authoritative` set for the live
    triage pass (below): it is exactly `derive_ci_records`'s move — a now-passing check is authoritative so its
    Issue can auto-resolve — applied here, so a file that DROPPED back under its budget (still evaluated, no
    longer firing → absent from the records) auto-resolves its tracked Issue. Confined to `soft-budget:` sids
    BY CONSTRUCTION, so a soft-budget pass can never close a `ci/`/`ambient/`/`episodic/`/out-of-band Issue
    (the source-scoping rule — never AUTHORITATIVE_ALL). An anomalous path that could break the tracking marker
    is dropped (marker-safety, mirroring budget_records)."""
    surfaces = set()
    for rule in validate.load_rules():
        if rule.get("kind") != "shape":          # the length-budget nudge lives on the shape rules
            continue
        for path in validate.target_files(rule):
            rel = os.path.relpath(path, validate.ROOT)
            if any(c in rel for c in "<>\n\r"):
                continue
            surfaces.add(f"{SOURCE_PREFIX}{rel}")
    return surfaces


def promote(repo: str, token: str, now: str, *, transport=None, claims: dict | None = None,
            cache_path: str = DEFAULT_SOFT_BUDGET_STREAMS_PATH):
    """Run ONE live-derived triage pass over the firing budget nudges: open/update a deduped, lane-aware
    tracked Issue for each over-budget surface, and AUTO-RESOLVE the Issue of any surface that dropped back
    under its budget. Returns the telemetry `Report` (opened/updated/closed/degraded). `transport` is
    injectable so the demo/tests fake only the network; `claims` is passed through to budget_records for lane
    control; `cache_path` overrides the accrual cache so the demo/tests isolate it (a shared default cache with
    a fresh fake GitHub — whose issue numbers reset per run — would misattribute across runs). Under live=True
    the cache is not RELIED upon, but it is still read/written, so isolating it keeps the demo/tests deterministic.

    LIVE-DERIVED (live=True), like the CI signal: a length-budget overrun is a re-derivable STANDING condition
    (not a flicker), so it promotes on first observation and its Issue auto-resolves the first pass it is seen
    back under budget — telemetry's standard resolution (auto-resolve
    clears the flag, it does NOT repair the file). `authoritative` is the FULL budget-checked surface set
    (`budget_surfaces()`), confined to `soft-budget:` sids, so the pass closes a cleared budget Issue but never
    another source's. This supersedes the earlier open-or-update-only stance: telemetry's source-scoped
    auto-resolve now makes a safe close possible, so a trimmed file no longer leaves a stale Issue open
    forever."""
    records = budget_records(now, claims=claims, repo=repo)
    github = telemetry.GitHubIssues(repo, token, transport=transport)
    return telemetry.run(github, records, telemetry.Cache(cache_path),
                         telemetry.load_thresholds(), now,
                         authoritative=budget_surfaces(), live=True)


def main(argv: list) -> int:
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("usage: audit_soft_promote.py   (needs GITHUB_REPOSITORY and GITHUB_TOKEN with issues:write "
              "in the environment; it uses the GitHub token, never the Claude token)", file=sys.stderr)
        return 2
    try:
        report = promote(repo, token, moment.utc_now())
    except Exception as exc:  # noqa: BLE001 — fail-open: a transient blip must never fail the self-review
        print(f"Could not track standing soft findings this run ({exc}); the self-review continues and the "
              f"finding will re-fire next run. Nothing was lost.")
        return 0
    if report.degraded:
        print("Could not reach GitHub to track standing length-budget findings this run; they will "
              "re-fire next run. Nothing was lost.")
    elif report.opened or report.updated or report.closed:
        print(f"Length-budget triage: opened {report.opened}, updated {report.updated}, "
              f"closed {report.closed} engine issue(s).")
    else:
        print("No standing length-budget findings are firing — nothing to track this run.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
