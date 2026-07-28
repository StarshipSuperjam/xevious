#!/usr/bin/env python3
"""Release-cut classifier + writer — the produce side of the engine's version line
(the complement to the consume side the module manager owns).

The engine and every module carry a version (`.engine/engine.json` `engine_release` + the
`packages` map; each `.engine/modules/<id>/manifest.json` `version`). The module manager
*consumes* a published release (fetch + overlay + migrate); nothing yet *produces* one — this tool
is that missing half: it decides the next version from what changed since the last release, and it
records the chosen versions into the manifests. It does NOT tag, open a PR, or publish a Release —
that GitHub-facing plumbing is later slices; this is the version-decision core they drive.

Two subcommands, split so consent attaches to a proposal the writer cannot silently drift from:

  propose  — read-only. Resolve the last release baseline from the engine's HOME repo (the #369
             `home_repository` coordinate — the same source the updater fetches from, so producer
             and consumer agree on what "a release" is), diff since it, and author:
               * the mechanical bump FLOOR: a module ADDED => engine >= minor;
                 a module REMOVED => engine >= major; a new `migrations` entry in a package => that
                 package >= minor; the engine version = the MAX implied bump;
               * a plain-language CHANGE INVENTORY (what changed since the last release), so the
                 maintainer can catch a wrong floor or a missing signal;
               * where a contract/seam/interface/wiring surface changed, an AI-authored plain-language
                 IMPACT statement, with the break/no-break behavioral demonstration marked present
                 (a correlate exists) or "no correlate — release consciously sub-bar, named" (the
                 legible gate path; no acceptance-benchmark instrument is available,
                 and its absence is stated, never faked).
             It writes nothing.

  apply    — the writer. Records the chosen engine + per-package versions into the manifests, with:
               * RAISE-ONLY enforcement: the engine version and every CHANGED
                 capability are compared against the current on-disk version, and a target that is a
                 detectable LOWERING is REFUSED loudly (the dev sentinel `0.0.0-dev` sorts below any
                 real release). An unchanged capability keeps its recorded version — it is a no-op keep,
                 not a lowering, so it is neither rewritten nor refused; a capability a change requires
                 to bump (its package_floor) is auto-raised to that floor. Nothing is ever silently
                 lowered, and a required bump is never silently skipped (below-confirmed-floor is
                 checked over the full floor set);
               * an ATOMIC staged write: every touched file is written to a temp sibling and
                 schema-re-validated (plus a packages<->manifest equality check) BEFORE any swap, then
                 all swapped together; a validation failure changes nothing, and a write error mid-swap
                 rolls back the files already written and reports loudly (no split-brain — the
                 "atomic-or-loudly-incomplete" invariant; the reviewed-PR merge is the real
                 all-or-nothing unit, this bounds the on-disk window);
               * shape preservation: manifests are loaded, mutated in place, and rewritten with the
                 house 2-space+newline writer, so only version VALUES change — the `home_repository`
                 line stays byte-identical and the tightened weakening_guard is not
                 tripped by a version-only cut.

Read-only discovery + the release-ref/fetch/manifest-write helpers are reused from module_coherence
and module_manager (one present-set reader, one release-ref resolver — no drift).

A third subcommand renders the maintainer's evidence:

  pr-body  — read-only. Render the release pull request's body from a `propose` JSON + an `apply` result
             JSON: the change inventory, the versions actually recorded, a legible gate-path line
             (passed / consciously-sub-bar / errored — the three read as distinct), and the confirm/raise/
             reject guidance that makes the PR review the consent act. Authored HERE, never in workflow
             bash, so the gate-path legibility has one home.

CLI:
  python tools/release_cut.py propose [--json] [--baseline-tree DIR]
  python tools/release_cut.py apply --engine VER [--all VER] [--package id=ver ...] \
                                    [--proposal FILE] [--dry-run] [--json]
  python tools/release_cut.py pr-body --proposal FILE --applied FILE [--gate-state STATE]
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import sys
import tempfile

import jsonschema

import validate
import module_coherence
import module_manager

SENTINEL = "0.0.0-dev"
ENGINE_SCHEMA = os.path.join(validate.SCHEMAS_DIR, "engine.v1.json")
MODULE_SCHEMA = os.path.join(validate.SCHEMAS_DIR, "module.v1.json")

# The change-inventory line classify() adds when NOTHING structural fired — a caveat, not a per-item signal,
# so the renderers exclude it when listing the structural signals beside the merged-PR list (one home for the
# string, referenced in both places).
_NO_STRUCTURAL_SIGNAL_NOTE = ("No module added or removed and no new migration since the last release — "
                              "so at most a patch. A behaviour change with no structural signal would not "
                              "show here; cross-check against what you actually shipped.")


# --------------------------------------------------------------------------- version ordering
# Strict MAJOR.MINOR.PATCH with an optional pre-release suffix — the SAME grammar the module.v1 schema
# now enforces on the manifest `version` field (#402 U07a), so the writer here and the schema gate at CI
# cannot bless different shapes. Kept in sync deliberately: the schema is the harder gate, and this writer
# check catches a nonsense version before it ever reaches a release manifest.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def _valid_version(v: str) -> bool:
    """A MAJOR.MINOR.PATCH version, optionally with a pre-release suffix (1.2.0, 1.0.0-rc1, 0.0.0-dev).
    The manifest schema requires this exact shape (module.v1 version pattern), and it is enforced HERE at
    the writer too so a nonsense version (a typo, a shell fragment, a 1- or 2-component number) never
    reaches a release manifest and never fools the digit-only ordering."""
    return bool(_VERSION_RE.match(v or ""))


def _is_prerelease(v: str) -> bool:
    """A version carrying a pre-release suffix (a '-', e.g. the `0.0.0-dev` sentinel or `1.0.0-rc1`)."""
    return "-" in (v or "")


def _release_tuple(v: str) -> tuple:
    """The numeric release identity of a version, the pre-release suffix REMOVED before tupling —
    otherwise `validate._ver_tuple` folds `-rc1`'s digits into the tuple and a pre-release sorts
    ABOVE its own release (1.0.0-rc1 -> (1,0,0,1) > (1,0,0))."""
    return validate._ver_tuple((v or "").split("-", 1)[0])


def _strictly_greater(new: str, cur: str) -> bool:
    """True iff `new` is a strictly higher RELEASE than `cur`. Compared on the release numbers with the
    pre-release stripped; on equal numbers a real release outranks a pre-release of the same numbers
    (so `0.1.0` > `0.0.0-dev` and `1.0.0` > `1.0.0-rc1`), and a pre-release is never taken as greater
    than another version of the same numbers (conservative — a pre-release progression like rc1 -> rc2
    is refused rather than risk a silent mis-order; raise-only never lowers)."""
    nt, ct = _release_tuple(new), _release_tuple(cur)
    if nt != ct:
        return nt > ct
    return _is_prerelease(cur) and not _is_prerelease(new)


# --------------------------------------------------------------------------- product-release mode (#516)
# Once the engine is DEPLOYED, this same machinery cuts the deployed repo's OWN product release instead of the
# engine's version: the version is read from (and written to) a product-owned `product-version.json` at the
# repository ROOT (product territory, eADR-0007 — so it survives an engine uninstall), the baseline is the
# deployed repo's own last release, and the tag + GitHub Release publish into the deployed repo itself
# (release_terminal already targets GITHUB_REPOSITORY). The CONSTRUCTION repo (where the engine IS the product)
# keeps cutting the engine version, unchanged. A deployment inherits a working release system instead of
# building versioning plumbing from scratch. The workflow shell is untouched: product-mode speaks the SAME
# propose/apply JSON shape (a `mode`, an `engine_floor_version` carrying the patch-bump default, an `engine`
# key carrying the recorded version) with product semantics underneath, plus a `product` marker the renderers
# and the publisher read to speak of the PRODUCT rather than the engine.
PRODUCT_VERSION_REL = "product-version.json"
_PRODUCT_MALFORMED = object()   # the file exists but is not a readable {"version": "<semver>"} -> refuse loudly


def _product_version_path(root: str | None = None) -> str:
    return os.path.join(root if root is not None else validate.ROOT, PRODUCT_VERSION_REL)


def read_product_version(root: str | None = None):
    """The current product version string, or None (no file — an un-seeded deployment / a first cut), or the
    `_PRODUCT_MALFORMED` sentinel (the file is present but is not a readable `{"version": "<semver>"}`).
    Malformed is NEVER silently treated as absent: the mode resolver turns it into a loud refuse, so a corrupt
    product file can never fall through to an ENGINE cut in a deployed repo."""
    path = _product_version_path(root)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — present-but-unreadable is a loud refuse, never "absent"
        return _PRODUCT_MALFORMED
    v = data.get("version") if isinstance(data, dict) else None
    return v if isinstance(v, str) and _valid_version(v) else _PRODUCT_MALFORMED


def release_mode(own_slug: str | None = None, root: str | None = None) -> tuple:
    """Which release this repo cuts: `("engine" | "product" | "refuse", ctx)` where `ctx` carries the current
    product version (`current`, None on a first product cut) and the repo `slug`. PRODUCT dominates: a repo is
    in product-mode when it carries a `product-version.json` OR it is a downstream deployment (recorded update
    home != own origin) — so product-mode ARMS on a deployed repo's very first upgrade, and the first product
    cut CREATES the file. A present-but-MALFORMED product file is a loud REFUSE, never an engine cut. Only the
    construction repo — not a downstream copy, no product file — cuts the ENGINE version. `own_slug`/`root` are
    injectable so a fixture forces either mode offline.

    File-presence DOMINATES deliberately. `module_coherence.is_downstream_copy` fails soft to False on an
    unreadable origin; keying product-mode on the downstream check alone could route a deployed repo that DOES
    carry the product file into an engine cut whenever its origin momentarily can't be read. Because a present
    product file forces product-mode on its own, that regression cannot happen — the deployed repo's committed
    declaration wins over live origin resolution."""
    pv = read_product_version(root)
    if pv is _PRODUCT_MALFORMED:
        return "refuse", {"current": None, "slug": own_slug}
    if own_slug is None:
        import boot   # local: only mode resolution needs the origin slug (mirrors _generate_notes_body)
        own_slug = boot.repo_slug()
    if pv is not None or module_coherence.is_downstream_copy(own_slug):
        return "product", {"current": pv, "slug": own_slug}
    return "engine", {"current": None, "slug": own_slug}


# --------------------------------------------------------------------------- baseline resolution
class Baseline:
    """The last-release baseline for the diff. `ref` is None in FIRST-CUT mode (the home has no
    published release yet — the current reality, and the state the v1/beta cut is made from)."""
    def __init__(self, ref, first_cut: bool, note: str):
        self.ref = ref
        self.first_cut = first_cut
        self.note = note


def _product_baseline(slug: str | None) -> Baseline:
    """The product baseline for a deployed repo's OWN release stream. When the repo slug could NOT be resolved
    (`boot.repo_slug()` returned None — no GITHUB_REPOSITORY and no readable git origin), a product cut must
    NOT fall through to `resolve_baseline`'s engine-`home_repository` default (that would diff the product
    against the ENGINE's releases): with no slug there is no release stream to look up, so it is a first cut —
    the version is chosen, not derived. On the sanctioned CI path the slug is always set, so this is defensive."""
    if not slug:
        return Baseline(None, True, "the repository could not be identified, so there is no prior release to "
                                    "diff against — treating this as the first product release.")
    return resolve_baseline(slug=slug)


def resolve_baseline(slug: str | None = None) -> Baseline:
    """The last released tag to diff against, or a first-cut baseline when there is no release yet. `slug`
    defaults to the engine's HOME repo (#369 `home_repository` — the engine's own release stream); in
    PRODUCT-mode (#516) the caller passes the DEPLOYED repo's own slug, so a product cut resolves the product's
    own last release, never the engine's home. A TRANSPORT failure (offline/DNS) is not a first cut — it is
    unknowable, and we say so rather than guess an empty baseline."""
    home = slug if slug is not None else module_manager._home_repository()
    if not home:
        return Baseline(None, True, "no home repository is recorded, so there is no prior release to "
                                    "diff against — treating this as the first cut.")
    try:
        ref = module_manager._resolve_release_ref(None, repo=home)
        return Baseline(ref, False, f"diffing since the last release {ref} of {home}.")
    except Exception as exc:  # _resolve_release_ref raises RuntimeError subclasses (Exception), never BaseException
        if module_manager._release_is_missing(exc):
            return Baseline(None, True, f"{home} has no published release yet — this is the first cut.")
        raise


def _baseline_tree_for(baseline: Baseline, injected: str | None) -> tuple:
    """The baseline release tree to diff against, and a temp dir to clean up (or None). An INJECTED local
    tree always wins (tests and an explicit `--baseline-tree` pass one, so `propose` never reaches the
    network in a test). Otherwise, in diff mode, the tree is fetched from the home's release tarball at the
    resolved ref via the module_manager network boundary — a TESTED Python caller (like the other release
    helpers), never a private symbol reached from workflow bash. First-cut mode diffs nothing, so no tree."""
    if injected:
        return injected, None
    if baseline.first_cut:
        return None, None
    home = module_manager._home_repository()
    tmp = tempfile.mkdtemp(prefix="release-baseline-")
    try:
        tree = module_manager._fetch_release_tree(baseline.ref, tmp, repo=home)
    except BaseException:
        # the fetch can raise (transport failure, non-200, a malformed tarball) BEFORE the temp dir is
        # returned to the caller's finally — clean it up here so a failed fetch never strands a temp dir
        # (the caller only removes what it receives back).
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tree, tmp


# --------------------------------------------------------------------------- merged-PR summary (the work log)
# The structural floor signals (a capability added/removed, a new migration, a changed contract) justify the
# VERSION, but they are a narrow slice of a release — a busy release can merge dozens of pull requests that
# touch none of them. So the notes ALSO carry the plain list of pull requests merged since the last release —
# the actual body of work — from GitHub's own generator, which lists them independently of the merge strategy
# (merge / squash / rebase), so it holds in a generated repo too. This is a derived view of the pull requests
# themselves (the one history store, eADR-0014), never a second store.
_PR_LINE_RE = re.compile(r"^\* (.+) by @\S+ in \S+/pull/(\d+)\s*$")
# The engine's OWN release pull request (title "Release X.Y.Z", authored by release.yml). At publish the notes
# are generated over previous_tag..merge_sha, which spans the release PR's own merge — so without this it would
# list itself and the count would be one high. Past release PRs sit before previous_tag, out of range.
_RELEASE_PR_RE = re.compile(r"^Release \d+\.\d+\.\d+")
# A closing keyword directly bound to an issue reference (GitHub's own auto-close grammar: close/closes/closed,
# fix/fixes/fixed, resolve/resolves/resolved, then optional colon/whitespace, then `#N` or a cross-repo
# `owner/repo#N`). A merged PR's author often writes "(Closes #N)" into the PR title; rendered VERBATIM into the
# RELEASE pull-request body it makes GitHub attribute that close to the release — so on merge the release would
# (re-)close it. We strip the KEYWORD and keep the reference (readable, inert). A keyword NOT directly adjacent
# to the reference is not a GitHub close (e.g. "fail closed (#390)" — the `(` breaks the bond; "Fixed several
# bugs, see #5") and is left untouched (confirmed empirically against GitHub). The bare-URL and `GH-N` forms are
# out of scope: GitHub's documented auto-close grammar does not include them.
_CLOSING_KEYWORD_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]+((?:[\w.-]+/[\w.-]+)?#\d+)")


def _defuse_closing_keywords(text: str) -> str:
    """Neutralise any `Closes #N` / `Fixes #N` / `Resolves #N` in a merged-PR title so it cannot auto-close the
    issue from the release pull-request body — the keyword is dropped, the `#N` reference is kept."""
    return _CLOSING_KEYWORD_RE.sub(r"\1", text)


def _parse_pr_lines(body: str) -> list:
    """GitHub's generated 'What's Changed' body -> plain 'Title (#N)' lines, dropping the author + URL noise
    the engine's plain-language notes don't carry, the engine's own release pull request (a release must not
    list itself), and any closing keyword the title carries (so the release does not re-close those issues)."""
    out = []
    for line in (body or "").splitlines():
        m = _PR_LINE_RE.match(line.strip())
        if m and not _RELEASE_PR_RE.match(m.group(1).strip()):
            out.append(f"{_defuse_closing_keywords(m.group(1).strip())} (#{m.group(2)})")
    return out


# The release-notes change kinds. A title that leads with one as a `Kind:` prefix (`Fix: quote the hook path`)
# groups the merged-PR list so the notes read as sorted work, not one flat pile. This list is BOTH the
# recognised set AND the display order; a title with no recognised prefix falls to "Other changes", rendered
# last. It is the one place a deployed repo edits to change its kind vocabulary — so each kind is regex-escaped
# before matching, since `render_*` are not best-effort wrapped and an edited kind carrying a metacharacter
# must not break the render. Grouping is a DISPLAY view: it never touches `_parse_pr_lines`' flat list, which
# both render sites share.
_RELEASE_NOTE_KINDS = ["Feature", "Improvement", "Fix", "Security", "Removal", "Maintenance"]
_OTHER_KIND = "Other changes"
# The security marker a dependency bot writes into a title AFTER any configured prefix (dependabot-core's
# pr_name_prefixer.rb: `prefix = commit_prefix.to_s; prefix += security_prefix if security_fix?`, where
# security_prefix is "[Security] "). So a CVE fix in a repo that prefixes its bumps arrives as
# "Maintenance: [Security] bump …". A security fix must NEVER read as the upkeep that prefix claims, so the
# marker WINS over the declared kind — on any title that carries it, whoever wrote it.
_SECURITY_MARKER_RE = re.compile(r"^\[security\][ \t]*", re.I)


def _compile_kind_prefix(kinds: list) -> "re.Pattern":
    """The case-insensitive `^Kind:` matcher, with each kind regex-escaped so an edited kind vocabulary
    carrying a metacharacter (a deployer's `C++`, `.NET`) matches literally and cannot make the render throw."""
    return re.compile(r"^(" + "|".join(re.escape(k) for k in kinds) + r"):[ \t]*", re.I)


_KIND_PREFIX_RE = _compile_kind_prefix(_RELEASE_NOTE_KINDS)
_KIND_BY_LOWER = {k.lower(): k for k in _RELEASE_NOTE_KINDS}


def _group_prs_by_kind(lines: list) -> list:
    """Group the plain 'Title (#N)' merged-PR lines by the change kind their title declares as a leading
    'Kind:' prefix — stripping that prefix from the displayed line (the group heading now carries it). A line
    with no recognised prefix collects under 'Other changes'. Returns (kind, [line, …]) pairs in
    `_RELEASE_NOTE_KINDS` order with 'Other changes' always last, skipping any empty group; `[]` in, `[]` out."""
    buckets = {k: [] for k in _RELEASE_NOTE_KINDS}
    other = []
    for ln in lines:
        m = _KIND_PREFIX_RE.match(ln)
        # `re.I` case-folds WIDER than str.lower() (Turkish `İmprovement`, dotless `ımprovement`, long-s
        # `ſecurity`), so a match can carry a spelling this map has no key for. The lookup is therefore TOTAL:
        # an unmappable match falls through to "Other changes" rather than raising. render_* is NOT
        # best-effort wrapped, so a KeyError here would block a release cut over nothing but a title's spelling.
        kind = _KIND_BY_LOWER.get(m.group(1).lower()) if m else None
        rest = ln[m.end():] if kind else ln
        sm = _SECURITY_MARKER_RE.match(rest)
        if sm:
            kind, rest = "Security", rest[sm.end():]
        if kind:
            buckets[kind].append(rest)
        else:
            other.append(rest)
    grouped = [(k, buckets[k]) for k in _RELEASE_NOTE_KINDS if buckets[k]]
    if other:
        grouped.append((_OTHER_KIND, other))
    return grouped


def _render_pr_groups(merged: list, heading) -> list:
    """The merged-PR list as display lines: one `heading(kind)` block per change kind with its bullets under
    it. The two render sites share this — only the heading form differs (a `###` subheading in the published
    Release body; a bold label inside the pull request's one `## Scope` section, whose plain-text peers a
    heading would out-rank). When NOTHING carries a kind, the lone 'Other changes' heading is OMITTED: a
    heading that says "other" is only meaningful against something else, and standing alone it would label a
    reader's whole release as leftovers. So an unadopted convention degrades to EXACTLY the old flat list —
    never worse — which is the state every generated repo starts in."""
    groups = _group_prs_by_kind(merged)
    if len(groups) == 1 and groups[0][0] == _OTHER_KIND:
        return [f"- {p}" for p in groups[0][1]]
    out = []
    for i, (kind, items) in enumerate(groups):
        if i:
            out.append("")
        out += [heading(kind), ""]
        out += [f"- {p}" for p in items]
    return out


def _generate_notes_body(slug: str, previous_tag: str, target: str, token: str | None) -> str:
    """POST /repos/{slug}/releases/generate-notes -> the generated markdown body. Despite the POST verb this
    creates nothing — it is GitHub's read-only release-notes generator. `tag_name` is a placeholder label; the
    listed pull requests depend only on the previous_tag..target range. This builds its own request rather than
    routing through `github_client` DELIBERATELY: the host is a hardcoded literal, it is a single POST with no
    pagination / Link-following, so the off-host guard `github_client` carries has nothing to protect here; a
    future edit that adds pagination should reconsider that."""
    import urllib.request, json as _json, boot   # local: only the real fetch needs these (mirrors resolve_baseline)
    tok = token if token is not None else boot.gh_token()
    payload = _json.dumps({"tag_name": "unreleased", "previous_tag_name": previous_tag,
                           "target_commitish": target}).encode("utf-8")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "engine-release-cut", "Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    url = f"https://api.github.com/repos/{slug}/releases/generate-notes"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return (_json.loads(resp.read()) or {}).get("body") or ""


def merged_pr_titles(previous_tag: str | None, target: str, repo: str | None = None,
                     token: str | None = None, *, _fetch=None) -> list:
    """The pull requests merged since the last release, as plain 'Title (#N)' lines — the release's body of
    work, beside the structural floor signals. BEST-EFFORT: any failure (offline, no token, no previous tag,
    an unexpected response) returns [] so the notes simply omit the section — never a crash, never a blocked
    release. `previous_tag` is the last release tag; `target` is the commit-ish being released (the branch tip
    at cut time, the merge commit at publish). `repo` defaults to the engine's home (where the release tags
    and the pull requests live). `_fetch` is injectable so tests run offline."""
    try:
        slug = repo if repo is not None else module_manager._home_repository()
        if not slug or not previous_tag or not target:
            return []
        return _parse_pr_lines((_fetch or _generate_notes_body)(slug, previous_tag, target, token))
    except Exception:  # noqa: BLE001 — best-effort; on any failure the section is omitted, never blocking
        return []


# --------------------------------------------------------------------------- present / baseline sets
def _present_modules() -> dict:
    """id -> manifest for every present module (the live tree)."""
    out = {}
    for _rel, man in module_coherence.discover_manifests():
        mid = man.get("id")
        if mid:
            out[mid] = man
    return out


def _modules_in_tree(tree_root: str) -> dict:
    """id -> manifest for every module manifest under a fetched/injected release TREE root (the
    baseline side of the diff — `discover_manifests` only reads the live tree, so the baseline set
    is read from the release tree here)."""
    import glob as _glob
    out = {}
    for path in sorted(_glob.glob(os.path.join(tree_root, ".engine", "modules", "*", "manifest.json"))):
        man = validate.load_json(path)
        mid = man.get("id")
        if mid:
            out[mid] = man
    return out


# --------------------------------------------------------------------------- floor classification
def _bump_at_least(current: str, level: str) -> str:
    """The version `current` bumped to at least the given `level` (major|minor|patch). Used to express the
    mechanical FLOOR as a concrete next version for the change inventory (the engine floor uses major/minor; a
    product cut's derive-default uses patch); the maintainer may raise it."""
    parts = list(validate._ver_tuple(current))
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[0], parts[1], parts[2]
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _max_level(a: str, b: str) -> str:
    order = {"none": 0, "patch": 1, "minor": 2, "major": 3}
    return a if order[a] >= order[b] else b


def _migration_accumulation_violations(was: dict, present: dict) -> list:
    """Every migration a RETAINED module shipped in the previous release but the candidate no longer declares —
    a dropped migration version-key. Upgrades replay migrations by version RANGE (`module_manager.select_migrations`
    runs each key where from < ver <= target), so a key silently removed from a manifest is SKIPPED on a
    multi-version jump, never run — the #599 silent-skip class, at the migration layer. Keys are compared on
    NORMALIZED version tuples (`validate._ver_tuple`), so a re-key ('0.4' -> '0.4.0', equal as versions) is not a
    false drop. A whole REMOVED module is NOT checked here — it is inventoried as a removed capability, and its
    still-unrun migrations for a lagging upgrader are a KNOWN BOUND handled with the min-upgradeable-from floor.
    The sanctioned way to retire a transform is to KEEP its key with a no-op `run`, never to delete the key.

    Coverage assumes a POPULATED baseline: a missing baseline tree fails closed upstream (classify raises), but a
    baseline that resolves yet carries no module manifests compares against an empty set and finds no drop — loud
    in practice (every present module then reads as newly Added and forces a major floor), so the residual gap is
    low, but the hard fail-closed guarantee is only at the no-tree level."""
    def _norm(v):
        # Compare on a length-normalized version tuple so a re-key ('0.4' -> '0.4.0', equal as versions but a
        # 2- vs 3-tuple) is not read as a drop. Keys are conventionally MAJOR.MINOR.PATCH but NOT schema-enforced
        # (the migrations schema constrains only the value, not the key), so this normalization is load-bearing.
        t = validate._ver_tuple(v)
        return t + (0,) * (3 - len(t)) if len(t) < 3 else t
    out = []
    for mid, man in present.items():
        old = was.get(mid)
        if not old:
            continue
        new_keys = {_norm(k) for k in (man.get("migrations") or {})}
        for ver in sorted((old.get("migrations") or {}), key=validate._ver_tuple):
            if _norm(ver) not in new_keys:
                out.append(f"the '{mid}' capability dropped the upgrade step for version {ver} that the last "
                           f"release shipped; an engine updating across this version would skip it")
    return out


def classify(baseline: Baseline, baseline_tree: str | None) -> dict:
    """The proposal: the floor per package + engine, the change inventory, and the impact statements.
    In first-cut mode there is no baseline to diff, so no delta/floor is derived — the initial version
    is the maintainer's explicit choice."""
    present = _present_modules()
    engine = module_coherence.load_engine_manifest() or {}
    inventory: list[str] = []
    impacts: list[dict] = []
    package_floor: dict[str, str] = {}
    engine_level = "none"

    if baseline.first_cut:
        inventory.append(
            f"First release: establishes the baseline version for the engine and all "
            f"{len(present)} installed packages. No prior release exists to diff against, so the "
            f"initial version is chosen, not derived.")
        return {
            "mode": "first-cut",
            "baseline": None,
            "baseline_note": baseline.note,
            "current_engine": engine.get("engine_release"),
            "engine_floor_level": "none",
            "engine_floor_version": None,   # first cut: no prior release, so no mechanical floor to meet
            "package_floor": {},
            "change_inventory": inventory,
            "impacts": impacts,
        }

    # diff mode — compare the present set against the baseline release tree
    if not baseline_tree:
        raise RuntimeError(
            "a prior release exists but no baseline tree was provided to diff against; the release "
            "workflow fetches it (module_manager._fetch_release_tree), and tests inject a local tree.")
    was = _modules_in_tree(baseline_tree)
    added = sorted(set(present) - set(was))
    removed = sorted(set(was) - set(present))

    for mid in added:
        inventory.append(f"Added the '{mid}' capability.")
        engine_level = _max_level(engine_level, "minor")
    for mid in removed:
        inventory.append(f"Removed the '{mid}' capability.")
        engine_level = _max_level(engine_level, "major")

    for mid, man in present.items():
        old = was.get(mid)
        if not old:
            continue
        new_migs = set((man.get("migrations") or {}).keys())
        old_migs = set((old.get("migrations") or {}).keys())
        if new_migs - old_migs:
            keys = ", ".join(sorted(new_migs - old_migs))
            inventory.append(f"'{mid}' gained a data/config migration ({keys}).")
            package_floor[mid] = _bump_at_least(man.get("version", "0.0.0"), "minor")

    # contract / seam / interface / wiring changes carry an AI-authored impact statement
    impacts = _impact_statements(baseline_tree)
    if impacts:
        for im in impacts:
            engine_level = _max_level(engine_level, im["floor_level"])

    if not inventory and not impacts:
        inventory.append(_NO_STRUCTURAL_SIGNAL_NOTE)

    # The concrete mechanical floor version: the minimum next engine version a minor/major signal forces
    # (None when nothing structural fired — a patch is discretionary, so raise-only alone bounds it). This is
    # what `apply` enforces the chosen version against and what the PR body shows the maintainer to check.
    current_engine = engine.get("engine_release", SENTINEL)
    engine_floor_version = (_bump_at_least(current_engine, engine_level)
                            if engine_level in ("minor", "major") else None)

    return {
        "mode": "diff",
        "baseline": baseline.ref,
        "baseline_note": baseline.note,
        "current_engine": current_engine,
        "engine_floor_level": engine_level,
        "engine_floor_version": engine_floor_version,
        "package_floor": package_floor,
        "change_inventory": inventory,
        "impacts": impacts,
        # A dropped migration key on a retained module — the cut is refused on this, before apply writes (see
        # _cmd_propose). Empty on a clean diff; a stable field of the diff proposal so the refusal is legible.
        "migration_violations": _migration_accumulation_violations(was, present),
    }


# --------------------------------------------------------------------------- product proposal (#516)
def _product_proposal(baseline: Baseline, current_version: str, merged_prs: list) -> dict:
    """The release proposal for a PRODUCT cut — the SAME mode-neutral shape the workflow shell and the
    renderers consume, with product semantics. A product has no engine packages to diff, so there is no
    capability floor: the mechanical `engine_floor_version` is simply a PATCH bump of the current product
    version (the derive-the-version default when the operator leaves the version blank; raise-only still lets
    them name any higher one), and None on a first cut (where the version is chosen, not derived). The `product`
    marker rides in the proposal so the renderers and the publisher speak of the PRODUCT."""
    first_cut = baseline.first_cut
    note = ("this deployment has no published release yet — this is the first release of your product."
            if first_cut else f"releasing your product; the last release was {baseline.ref}.")
    inventory = (["First release: establishes the starting version of your product. No prior release exists, "
                  "so the version is chosen, not derived."] if first_cut else [])
    return {
        "mode": "first-cut" if first_cut else "diff",
        "product": True,
        "baseline": baseline.ref,
        "baseline_note": note,
        "current_engine": current_version,           # the current PRODUCT version (the renderers' generic key)
        "engine_floor_level": "none",                # a product has no structural capability floor
        "engine_floor_version": None if first_cut else _bump_at_least(current_version, "patch"),
        "package_floor": {},
        "change_inventory": inventory,
        "impacts": [],
        "merged_prs": merged_prs,
    }


# --------------------------------------------------------------------------- impact statements
_CONTRACT_GLOBS = (
    os.path.join(".engine", "contracts"),        # eADR contracts
    os.path.join(".engine", "interfaces"),        # interface surfaces
)


def _impact_statements(baseline_tree: str) -> list[dict]:
    """For each changed/added/removed contract or interface surface between the baseline tree and the
    live tree, an AI-authored plain-language impact statement (what changed · a note that consumers
    depend on it · why that reads breaking-or-additive), plus the behavioral-correlate marking. The
    break/no-break demonstration runs "where a behavioral correlate exists"; with
    no acceptance-benchmark instrument available, the marking is honest, not faked."""
    out: list[dict] = []
    for sub in _CONTRACT_GLOBS:
        live_dir = os.path.join(validate.ROOT, sub)
        base_dir = os.path.join(baseline_tree, sub)
        live = _dir_bytes(live_dir)
        base = _dir_bytes(base_dir)
        for name in sorted(set(live) | set(base)):
            lb, bb = live.get(name), base.get(name)
            if lb == bb:
                continue
            if bb is None:
                what, level = f"a new contract surface '{name}' was added", "minor"
                why = "new surfaces are additive — nothing existing depended on it yet."
            elif lb is None:
                what, level = f"the contract surface '{name}' was removed", "major"
                why = "removing a surface other parts may depend on is a breaking change."
            else:
                what, level = f"the contract surface '{name}' changed", "minor"
                why = ("a changed contract can be additive or breaking depending on which consumers "
                       "depend on it — read the change against them before confirming.")
            out.append({
                "surface": os.path.join(sub, name),
                "what": what,
                "why": why,
                "floor_level": level,
                "behavioral_demo": "none — no behavioral correlate is available for this signal, so this rests "
                                   "on the impact statement and your "
                                   "confirmation; the release is consciously sub-bar on this signal, named here.",
            })
    return out


def _dir_bytes(d: str) -> dict:
    """relative-path -> raw bytes for every file ANYWHERE under `d`, recursively (empty when the dir is
    absent). Recursive so a contract/interface surface in a subdirectory (e.g. `contracts/instance/…`) is
    diffed too — a non-recursive read silently skipped an entire subtree, so a nested surface added, changed,
    or removed produced no impact statement and no floor signal."""
    out = {}
    if not os.path.isdir(d):
        return out
    for root, _dirs, files in os.walk(d):
        for name in files:
            p = os.path.join(root, name)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, d)] = fh.read()
    return out


# --------------------------------------------------------------------------- apply (the writer)
def _target_versions(engine_ver: str, all_ver: str | None, packages: dict, present: dict) -> dict:
    """The concrete version each package is written to: `--all` sets every present package, an explicit
    `--package id=ver` overrides, and any package left unspecified keeps its current version."""
    out = {}
    for mid, man in present.items():
        if mid in packages:
            out[mid] = packages[mid]
        elif all_ver is not None:
            out[mid] = all_ver
        else:
            out[mid] = man.get("version", SENTINEL)
    return out


def _raise_only_violations(engine_ver: str, targets: dict, engine_cur: str, present: dict) -> list[str]:
    """Every target that is NOT strictly greater than its current on-disk version — the raise-only
    guard. The guard itself is strict; the caller passes only the capabilities
    actually being WRITTEN (a no-op keep at the current version is excluded upstream, so this flags a
    genuine lowering, never an unchanged capability). A returned non-empty list means the write must be
    refused."""
    bad = []
    if not _strictly_greater(engine_ver, engine_cur):
        bad.append(f"engine version {engine_ver} is not higher than the current {engine_cur}")
    for mid, ver in targets.items():
        cur = present[mid].get("version", SENTINEL)
        if not _strictly_greater(ver, cur):
            bad.append(f"package '{mid}' version {ver} is not higher than the current {cur}")
    return bad


def _schema_ok(instance, schema_path: str) -> list[str]:
    schema = validate.load_json(schema_path)
    v = jsonschema.Draft202012Validator(schema)
    return [e.message for e in v.iter_errors(instance)]


def apply(engine_ver: str, all_ver: str | None, packages: dict, proposal: dict | None,
          dry_run: bool, min_upgradeable_from: str | None = None) -> dict:
    """Record the chosen versions atomically. Returns a result dict (applied/refused + the proposed-vs-
    applied record for traceability). Writes nothing on a raise-only violation or a validation failure.
    `min_upgradeable_from` (optional) records the clean-upgrade floor into engine.json; a malformed value is
    refused fail-loud at the door (below), never persisted. When None, any prior floor is carried forward
    unchanged (engine.json is copied byte-preserved)."""
    present = _present_modules()
    engine = module_coherence.load_engine_manifest()
    if engine is None:
        raise RuntimeError("the engine manifest (.engine/engine.json) is missing; cannot cut a release.")
    engine_cur = engine.get("engine_release", SENTINEL)
    targets = _target_versions(engine_ver, all_ver, packages, present)

    # Auto-raise each floored capability to its mechanical floor. When a proposal is
    # supplied, a capability a change REQUIRES to bump (a new migration => its package_floor entry) is
    # written to that floor unless the caller set it explicitly with `--package`. This is the per-capability
    # analogue of the engine version auto-deriving to its floor: the release workflow passes no `--package`,
    # so without this a migration-bearing cut would keep the capability at its current version and then refuse
    # on below-confirmed-floor with no way to bump it.
    if proposal:
        for mid, floor in (proposal.get("package_floor") or {}).items():
            if mid in present and mid not in packages and _strictly_greater(floor, targets[mid]):
                targets[mid] = floor

    # version grammar: refuse a non-version string at the door (a typo must not reach a manifest)
    bad_fmt = []
    if not _valid_version(engine_ver):
        bad_fmt.append(f"engine version '{engine_ver}' is not a valid version (expected like 1.2.0 or 1.0.0-rc1)")
    for mid, ver in targets.items():
        if not _valid_version(ver):
            bad_fmt.append(f"package '{mid}' version '{ver}' is not a valid version (expected like 1.2.0)")
    if min_upgradeable_from is not None and not _valid_version(min_upgradeable_from):
        bad_fmt.append(f"minimum-upgradeable-from '{min_upgradeable_from}' is not a valid version "
                       f"(expected like 0.3.2) — a malformed floor would silently disable the upgrade guard")
    if bad_fmt:
        return {"applied": False, "reason": "invalid-version", "violations": bad_fmt,
                "recovery": "use dotted-number versions, optionally with a -prerelease suffix (1.2.0, 1.0.0-rc1)."}

    # The capabilities this cut actually WRITES: those whose version changes. A capability left at its current
    # version is a no-op keep — an unchanged capability keeps its recorded version (the locked module-system
    # law: per-package versions are independent recorded state, bumped only on that capability's own signal),
    # not a lowering — so it is neither rewritten nor raise-only-checked. This is what lets an engine-only cut
    # (the engine version moves; no capability changed) apply, instead of refusing because unchanged
    # capabilities are not strictly greater than themselves.
    changed = {mid: ver for mid, ver in targets.items() if ver != present[mid].get("version", SENTINEL)}

    # raise-only over the engine + the CHANGED set: a target that is a detectable
    # lowering is refused loudly (the guard is strict and unchanged — only the set it sees is narrowed to the
    # capabilities being written); the engine version must strictly increase. Nothing is ever silently lowered.
    violations = _raise_only_violations(engine_ver, changed, engine_cur, present)
    if violations:
        return {"applied": False, "reason": "raise-only", "violations": violations,
                "recovery": "choose versions strictly higher than the current ones, then re-run."}

    # not-below-the-confirmed-floor: when a proposal is supplied, a target must MEET OR RAISE its
    # confirmed floor — compared against the floor value, not the current version (raise-only already
    # covered current). A target strictly below the floor is refused.
    floor_notes = []
    if proposal:
        # the ENGINE floor: a minor/major bump forced by what changed since the last release (a module added
        # or removed, an interface changed) must be MET, not just be higher than the current version. Without
        # this, a removed-module major floor could be undercut by a patch bump — the "catch a wrong floor"
        # backstop. None when nothing structural fired (a patch is discretionary; raise-only bounds it).
        engine_floor = proposal.get("engine_floor_version")
        if engine_floor and _strictly_greater(engine_floor, engine_ver):
            floor_notes.append(f"engine version {engine_ver} is below the mechanical floor {engine_floor} "
                               f"that what changed since the last release requires")
        pf = proposal.get("package_floor", {})
        for mid, floor in pf.items():
            if mid in targets and _strictly_greater(floor, targets[mid]):
                floor_notes.append(f"'{mid}' version {targets[mid]} is below its confirmed floor {floor}")
        if floor_notes:
            return {"applied": False, "reason": "below-confirmed-floor", "violations": floor_notes,
                    "recovery": "raise the engine and any flagged packages to at least their mechanical floor."}

    # stage every touched file, validate ALL before any swap, then swap together (rollback on failure)
    staged: list[tuple[str, str]] = []  # (target_path, temp_path)
    errors: list[str] = []
    try:
        # engine.json — mutate in place so home_repository/identity/order are byte-preserved
        new_engine = dict(engine)
        new_engine["engine_release"] = engine_ver
        pkgs = dict(new_engine.get("packages", {}))
        for mid, ver in changed.items():
            if mid in pkgs:
                pkgs[mid] = ver
        new_engine["packages"] = pkgs
        if min_upgradeable_from is not None:               # record/refresh the clean-upgrade floor when given;
            new_engine["min_upgradeable_from"] = min_upgradeable_from   # else the dict copy carries any prior one
        errors += [f"engine.json: {m}" for m in _schema_ok(new_engine, ENGINE_SCHEMA)]

        # each CHANGED module manifest — mutate version only; unchanged capabilities are left untouched
        module_new: dict[str, dict] = {}
        for _rel, man in module_coherence.discover_manifests():
            mid = man.get("id")
            if mid in changed:
                nm = dict(man)
                nm["version"] = changed[mid]
                module_new[_rel] = nm
                errors += [f"{_rel}: {m}" for m in _schema_ok(nm, MODULE_SCHEMA)]

        # split-brain guard: engine.json packages[mid] must equal each module manifest version
        for _rel, nm in module_new.items():
            mid = nm.get("id")
            if new_engine["packages"].get(mid) != nm.get("version"):
                errors.append(f"split-brain: engine.json packages['{mid}']="
                              f"{new_engine['packages'].get(mid)} != {_rel} version={nm.get('version')}")

        if errors:
            return {"applied": False, "reason": "validation", "violations": errors,
                    "recovery": "the computed manifests did not validate; nothing was written."}

        if dry_run:
            return {"applied": False, "reason": "dry-run", "targets": changed, "engine": engine_ver,
                    "from_engine": engine_cur}

        # write temps
        def _stage(path, data):
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            staged.append((path, tmp))

        _stage(module_manager._engine_manifest_path(), new_engine)
        for _rel, nm in module_new.items():
            _stage(os.path.join(validate.ROOT, _rel), nm)

        # swap together; a write error mid-swap rolls back the files already swapped, so the tree is
        # never left half-written (best-effort atomic — the reviewed-PR merge is the real all-or-
        # nothing unit, and the release-integrity check catches any residual split-brain at merge).
        def _read_bytes(p):
            with open(p, "rb") as fh:
                return fh.read()

        originals = {path: _read_bytes(path) for path, _tmp in staged}
        swapped = []
        try:
            for path, tmp in staged:
                os.replace(tmp, path)
                swapped.append(path)
            staged = []
        except OSError as exc:
            for path in swapped:
                with open(path, "wb") as fh:
                    fh.write(originals[path])
            raise RuntimeError(f"a write error interrupted the cut ({exc}); the files already written were "
                               f"restored, so no versions changed and nothing was left half-written.")
    finally:
        for _path, tmp in staged:  # any un-swapped temp on an error path
            try:
                os.unlink(tmp)
            except OSError:
                pass

    return {"applied": True, "engine": engine_ver, "from_engine": engine_cur, "targets": changed,
            "proposed_floor": (proposal or {}).get("package_floor", {})}


# --------------------------------------------------------------------------- apply (product writer, #516)
def apply_product(version: str, dry_run: bool, root: str | None = None) -> dict:
    """Record the product version into `product-version.json` — the product analogue of `apply`. A product has
    no engine packages, so there is no per-package/floor/split-brain machinery: one root file, one version.
    Validate the version, enforce RAISE-ONLY against the current product version, then write ATOMICALLY (temp
    sibling + os.replace, temp cleaned up on ANY error), mirroring `apply`'s staged swap for the single file. An
    ABSENT file is a first cut from the construction sentinel (the summary reads 'no earlier version'); the
    common seeded first cut reads its `0.0.0` starting version ('0.0.0 → …'); a present-but-MALFORMED file
    refuses loudly. Returns the same result shape `apply` does (`engine` = the recorded version, `targets` =
    {} for a product) plus a `product` marker, so the workflow shell and the renderers are unchanged."""
    path = _product_version_path(root)
    current = read_product_version(root)
    if current is _PRODUCT_MALFORMED:
        return {"applied": False, "reason": "malformed-product-file",
                "violations": [f"{PRODUCT_VERSION_REL} is present but is not a readable "
                               f"{{\"version\": \"<semver>\"}} object"],
                "recovery": f"fix {PRODUCT_VERSION_REL} to be a JSON object with a version like 0.1.0, then re-run."}
    from_v = current if current is not None else SENTINEL
    if not _valid_version(version):
        return {"applied": False, "reason": "invalid-version",
                "violations": [f"product version '{version}' is not a valid version (expected like 1.2.0)"],
                "recovery": "use dotted-number versions, optionally with a -prerelease suffix (1.2.0, 1.0.0-rc1)."}
    if not _strictly_greater(version, from_v):
        return {"applied": False, "reason": "raise-only",
                "violations": [f"product version {version} is not higher than the current {from_v}"],
                "recovery": "choose a version strictly higher than the current one, then re-run."}
    if dry_run:
        return {"applied": False, "reason": "dry-run", "engine": version, "from_engine": from_v,
                "targets": {}, "product": True}
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": version}, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)   # atomic swap; tmp no longer exists after this
    except OSError as exc:
        raise RuntimeError(f"a write error interrupted the cut ({exc}); {PRODUCT_VERSION_REL} was not changed.")
    finally:
        if os.path.exists(tmp):   # any un-swapped temp on an error path (mirrors apply()'s finally)
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return {"applied": True, "engine": version, "from_engine": from_v, "targets": {}, "product": True}


# --------------------------------------------------------------------------- rendering
def _render_proposal(p: dict) -> str:
    lines = ["Release proposal", "================", "", p["baseline_note"], ""]
    lines.append("What changed since the last release:")
    for c in p["change_inventory"]:
        lines.append(f"  - {c}")
    if p["impacts"]:
        lines.append("")
        lines.append("Contract / interface changes (read before confirming):")
        for im in p["impacts"]:
            lines.append(f"  - {im['what']}: {im['why']}")
            lines.append(f"    behavioral check: {im['behavioral_demo']}")
    lines.append("")
    if p["mode"] == "first-cut":
        lines.append("This is the first cut — choose the initial version explicitly, e.g.:")
        lines.append("  release_cut.py apply --engine <ver> --all <ver>")
    else:
        floor = p["engine_floor_level"]
        if floor == "none":
            lines.append(f"No structural change forces a bump — a patch at most (current "
                         f"{p['current_engine']}). You may still raise it if you shipped a behaviour "
                         f"change with no structural signal; you can never lower it.")
        else:
            lines.append(f"Mechanical engine floor: at least a {floor} bump "
                         f"(current {p['current_engine']}). You may raise it, never lower it.")
        if p["package_floor"]:
            lines.append("Per-package floors:")
            for mid, ver in p["package_floor"].items():
                lines.append(f"  - {mid}: at least {ver}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- change summary (one renderer)
def change_summary(proposal: dict) -> list:
    """The plain-language "what changed since the last release" list that JUSTIFIES the version — the
    single derived view rendered into BOTH the release pull-request body and the published GitHub Release
    notes. One renderer over one proposal, never a second history store (eADR-0014): history routes to the
    pull-request body and, as a derived view of the same signals, the Release notes.

    It merges the structural change inventory (a capability added or removed, a new migration) with a
    one-line surface note for each CHANGED contract/interface — because a contract-only release carries no
    structural inventory line, so without the impact surfaces the "what changed" list would read empty even
    though a changed contract is exactly what forced the bump. The detail on each impact (why it may be
    breaking) is rendered separately in the pull-request Risk section; this is the summary line."""
    lines = list(proposal.get("change_inventory") or [])
    for im in proposal.get("impacts") or []:
        what = im.get("what")
        if what:                       # e.g. "the contract surface 'X' changed" -> "The contract surface 'X' changed."
            lines.append(_cap(what) + ".")
    return lines


def _cap(text: str) -> str:
    """Capitalize the first letter of a plain-language fragment (the impact `what` strings are lower-case)."""
    text = (text or "").strip()
    return (text[0].upper() + text[1:]) if text else text


def _structural_signals(proposal: dict) -> list:
    """The capability + data signals from the change inventory — 'Added the X capability', 'Removed the X
    capability', and (consent-critical for an upgrader) ''X' gained a data/config migration' — with the
    no-signal caveat and the first-release framing excluded. These answer 'what does upgrading DO to me?', a
    question the flat merged-PR list does not, so they are surfaced BESIDE the pull-request list, not replaced
    by it. A new migration in particular has no other callout (a removed capability rides the breaking
    warning, a changed contract rides the interface section)."""
    return [c for c in (proposal.get("change_inventory") or [])
            if c != _NO_STRUCTURAL_SIGNAL_NOTE and not c.startswith("First release:")]


def render_release_notes(tag: str, proposal: dict | None = None, gate_state: str = "sub-bar") -> str:
    """The published GitHub Release body — a human-readable, self-contained account of the release: the
    version, the readiness line, a breaking-change callout when the release is breaking, a "What changed"
    section (the pull requests merged since the last release, or the structural signals when that list is
    unavailable), and an "Interface changes to read" section carrying each changed contract/interface WITH
    its plain-language description. It is a derived VIEW of the same signals the
    release pull-request body renders (one source — the proposal recomputed at publish — never a second
    history store, eADR-0014); it does not restate the version-by-version manifest table (that is the pull
    request's job), it tells a reader of the published release what changed and why it matters. A None/empty
    proposal (the best-effort fallback when the publish-time recompute could not run) degrades to the version
    + readiness line alone. Maintainer register: 'engine version vX.Y.Z', no internal vocabulary."""
    product = bool((proposal or {}).get("product"))
    out = [f"Release {tag}." if product else f"Engine version {tag}.", "", _gate_path_line(gate_state, product)]
    proposal = proposal or {}
    if proposal.get("engine_floor_level") == "major":
        out += ["", "⚠️ **This release makes a breaking change.** Something an earlier version provided was "
                    "removed, or changed in a way that is not backward-compatible — so anything that relied on "
                    "it will need attention. See the changes below."]
    # "What changed" leads with the pull requests merged since the last release — the actual body of work —
    # when the list is available; otherwise it falls back to the structural signals (a first release, or a
    # best-effort failure to reach the pull-request list). Either way, the capability + data signals are
    # surfaced BESIDE the list (a flat PR title does not answer 'does this migrate my data?'), and the
    # interface-change detail follows.
    merged = proposal.get("merged_prs") or []
    inventory = proposal.get("change_inventory") or []
    if merged:
        n = len(merged)
        out += ["", f"## What changed since the last release ({n} pull request{'' if n == 1 else 's'})", ""]
        out += _render_pr_groups(merged, lambda k: f"### {k}")
        signals = _structural_signals(proposal)
        if signals:
            out += ["", "## Capability and data changes", ""]
            out += [f"- {c}" for c in signals]
    elif inventory:
        # "since the last release" would contradict a first release (there is no last release); title it plainly.
        heading = "What this release establishes" if proposal.get("mode") == "first-cut" \
            else "What changed since the last release"
        out += ["", f"## {heading}", ""]
        out += [f"- {c}" for c in inventory]
    impacts = proposal.get("impacts") or []
    if impacts:
        out += ["", "## Interface changes to read", ""]
        for im in impacts:
            what = _cap(im.get("what")) or "A contract surface changed"
            why = _cap(im.get("why"))          # its own sentence after the bold heading — capitalized, not a run-on
            out.append(f"- **{what}.**" + (f" {why}" if why else ""))
    return "\n".join(out)


# --------------------------------------------------------------------------- release-PR body (legibility)
def _gate_path_line(state: str, product: bool = False) -> str:
    """The legible gate-path line: the three release-readiness states must read as VISIBLY DISTINCT, never
    alike. Only `sub-bar` is reachable — no acceptance-benchmark instrument measures a release — but
    `passed`/`errored` are rendered here structurally so a benchmark reads legibly
    rather than as a retrofit (the standing legibility invariant, not a one-of-three accident). `product` swaps
    the subject to 'this release' for a deployed repo's product cut (the sub-bar text is already neutral)."""
    subject = "this release" if product else "the engine"
    if state == "passed":
        return (f"**Release readiness — passed.** {_cap(subject)} was exercised against its readiness check and "
                "met the bar for this release.")
    if state == "errored":
        return ("**Release readiness — could not be checked (it errored).** The readiness check did not run to "
                "completion, so readiness is unproven — treat this release as unverified until it runs clean.")
    return ("**Release readiness — no automated check ran (this is on purpose).** There is no automated "
            "readiness check built yet, so this release was not measured against one. It rests on the summary "
            "below and your own read — not a machine check. This is a deliberate, recorded choice, not a "
            "passed check.")


def _version_lines(applied: dict) -> list:
    """Plain-language 'what versions this sets' — collapsed to one line when every capability moves to the
    engine's own new version (the uniform first-cut case), else itemised so a per-capability difference shows."""
    engine = applied.get("engine")
    from_engine = applied.get("from_engine")
    targets = applied.get("targets") or {}
    # the first cut moves from the construction sentinel `0.0.0-dev`, which is internal and means nothing to the
    # maintainer — say "no earlier version" instead of surfacing it.
    from_shown = "no earlier version" if from_engine == SENTINEL else from_engine
    # PRODUCT cut: one product version, no per-capability lines (a product has no engine packages; targets={}).
    label = "Product" if applied.get("product") else "Engine"
    lines = [f"- {label}: {from_shown} → {engine}"]
    if targets and all(v == engine for v in targets.values()):
        lines.append(f"- Every capability ({len(targets)}): → {engine}")
    else:
        for mid in sorted(targets):
            lines.append(f"- {mid}: → {targets[mid]}")
    return lines


def pr_section(header: str, summary: str, body_lines: list, impact: str) -> list:
    """One pull-request-body section in the repo template's shape — a **bold one-line summary**, its bullets,
    then the italic `*Impact:*` line — so the release body matches the form every engine pull request's body
    uses, not merely the required headers (a header-only body clears the completeness gate but is not a
    template-conforming body). PUBLIC: also consumed by module_manager's upgrade-PR-body author, so the
    engine's update pull request reads in the same template shape — the name is public because the dependency
    crosses a module boundary (an underscore would hide that a second module relies on it)."""
    return [f"## {header}", "", f"**{summary}**", "", *body_lines, "", f"*Impact: {impact}*", ""]


def template_preamble() -> str:
    """The consent-preamble blockquote lifted VERBATIM from the repo pull-request template, so the release
    body carries the same standing note on how to read the checks that every other pull request carries —
    one source, no second copy to drift, and always the preamble the pull-request-completeness gate requires.
    It is the leading `>` blockquote that sits above the first `## ` heading in the template. PUBLIC for the
    same cross-module reason as pr_section — the upgrade-PR-body author reuses it rather than keeping a second
    preamble copy that could drift from the template's anchor phrases."""
    path = os.path.join(validate.ROOT, ".github", "pull_request_template.md")
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    block: list = []
    for line in lines:
        if line.startswith("## "):
            break
        if line.startswith(">"):
            block.append(line)
        elif block:                    # the blockquote ended before the first heading
            break
    if not block:
        raise RuntimeError("the pull-request template carries no consent-preamble blockquote to lift into "
                           "the release body (.github/pull_request_template.md).")
    return "\n".join(block)


def render_pr_body(proposal: dict, applied: dict, gate_state: str = "sub-bar") -> str:
    """The release pull request's body — the maintainer's whole evidence bundle, authored HERE (never
    composed in workflow bash) so the gate-path legibility has one home. It takes both the `propose` JSON
    (the change inventory + interface impacts) and the `apply` result JSON (the versions actually recorded),
    and closes with the confirm/raise/reject guidance that makes the PR review the consent act: the merge
    is the go-ahead, and a wrong or missing signal is caught by closing and re-running with the right version.
    Maintainer-facing register: one engine version moving vX→vY — no 'release-cut'/'bump'/'version
    production' vocabulary. Every section follows the repo pull-request template's form (bold summary →
    bullets → `*Impact:*`), not just its headers — a real template-conforming body, whose section names also
    clear the pull-request-completeness gate."""
    engine = applied.get("engine")
    from_engine = applied.get("from_engine")
    # this body IS the maintainer's consent surface, so it must never author a "None → None" release: a refused
    # or malformed apply result carries no versions and cannot be rendered as a release.
    if not engine:
        raise RuntimeError("cannot render a release summary: the apply result recorded no engine version "
                           "(the release was refused or the result is malformed).")
    # the construction sentinel `0.0.0-dev` is internal — never surface it to the maintainer (see _version_lines)
    from_shown = "no earlier version" if from_engine == SENTINEL else from_engine
    # PRODUCT cut (#516): a deployed repo cutting its OWN product release — speak of the product, not the engine.
    product = bool(applied.get("product") or proposal.get("product"))
    thing = "product" if product else "engine"

    # The consent preamble every pull request carries at the top — lifted from the template so the release
    # body reads the same and satisfies the pull-request-completeness gate's preamble anchors (#589). Emitted in
    # BOTH modes, so a product release PR clears the same gate an engine one does.
    out = [f"# A new {'release of your product' if product else 'engine version'}: "
           f"{from_shown} → {engine}", "", template_preamble(), ""]

    out += pr_section(
        "Purpose",
        f"This records a new version of your {thing} — {from_shown} → {engine} — for you to review and publish.",
        [f"- Merging this is your go-ahead to release {engine}; closing it releases nothing and changes none of "
         "your own settings or content.",
         "- A release only ever moves the version up, never down."],
        (f"merging publishes {engine} as a release of your product; nothing is published until then." if product
         else f"merging publishes {engine} for your instances to upgrade to; nothing is published until then."))

    # Scope — the versions recorded + the change inventory that set them (the itemised version lines and the
    # least-version floor line stay verbatim; they are what a reviewer checks the release against).
    scope = ["The versions this release sets:", *_version_lines(applied)]
    floor_v = proposal.get("engine_floor_version")
    if floor_v:
        scope.append(f"- The least this release could be is **{floor_v}** — that is what the changes below "
                     f"require; a higher version is fine, a lower one is not.")
    # "What changed" leads with the pull requests merged since the last release (the actual work) when the
    # list is available; otherwise the structural floor-signal summary. The capability + data signals are
    # surfaced beside the list (the migration signal has no other home); the interface detail is under Risk.
    merged = proposal.get("merged_prs") or []
    if merged:
        n = len(merged)
        header = f"What changed since the last release ({n} pull request{'' if n == 1 else 's'}):"
        # Same kind-grouping as the published Release notes, but rendered as BOLD LABELS, not `###` headings:
        # this block sits inside the one `## Scope` section, whose peers ("Capability and data changes:") are
        # plain-text labels — a heading here would out-rank them and invert the outline — and bold labels render
        # cleanly inside the <details> block below where headings need careful blank-line handling.
        pr_lines = _render_pr_groups(merged, lambda k: f"**{k}**")
        # a long list is wrapped in a foldable <details> so the reader CAN collapse it (it otherwise pushes the
        # Review guidance far down the consent surface) — but rendered OPEN by default, so the work is visible on
        # load, not hidden behind a click.
        if n > 15:
            scope += ["", header, "", "<details open><summary>Merged pull requests</summary>", "", *pr_lines,
                      "", "</details>"]
        else:
            scope += ["", header, "", *pr_lines]
        signals = _structural_signals(proposal)
        if signals:
            scope += ["", "Capability and data changes:"]
            scope += [f"- {c}" for c in signals]
    else:
        heading = "What this release establishes" if proposal.get("mode") == "first-cut" \
            else "What changed since the last release"
        scope += ["", f"{heading}:"]
        scope += [f"- {c}" for c in change_summary(proposal)]
    out += pr_section(
        "Scope",
        ("The product version this records, and the changes that set it." if product
         else "The engine and capability versions this records, and the changes that set them."),
        scope,
        ("this is the exact version written into product-version.json." if product
         else "these are the exact versions written into the manifests and the maps that mirror them."))

    out += pr_section(
        "Out of scope",
        "What merging does not do.",
        [f"- It does not change how your {thing} behaves beyond the version stamp.",
         "- It does not migrate any of your data.",
         "- It does not touch your own settings or content."],
        ("the only thing this pull request changes is the recorded product version." if product
         else "the only thing this pull request changes is the recorded version and the generated maps that "
              "mirror it."))

    # Risk — the gate-path line is the (already bold-led) section summary; the breaking-change warning and
    # the interface-impact list are its bullets, so a reviewer scanning "Risk" sees the weight here, not only
    # as a neutral line up in Scope.
    risk = []
    if proposal.get("engine_floor_level") == "major":
        risk.append("- **This release makes a breaking change.** Something an earlier version provided was "
                    "removed, or changed in a way that is not backward-compatible — so anything that relied on "
                    "it will need attention. What changed is listed under Scope above.")
    impacts = proposal.get("impacts") or []
    if impacts:
        if risk:             # a breaking-change bullet precedes this intro — a blank line keeps the intro from
            risk.append("")  # being absorbed into that bullet as a lazy markdown continuation (the two would
                             # otherwise fuse, hiding the interface-changes signpost on the highest-stakes release).
        risk.append("Interface changes to read before you merge:")
        # Same polished rendering as the published Release notes — a bold heading, then the description as its
        # own sentence — so the consent surface the maintainer reads FIRST is no rougher than the Release body.
        risk += [f"- **{_cap(im.get('what')) or 'A contract surface changed'}.**"
                 + (f" {_cap(im.get('why'))}" if im.get("why") else "") for im in impacts]
    elif product:
        risk.append("- The summary can only show what it detects mechanically — the list of merged pull "
                    "requests above. Your own knowledge of what you shipped is the backstop (see Review).")
    else:
        risk.append("- No changes to interface contract files were detected — this does not cover a removed "
                    "capability or a data migration, which would be listed under Scope. The summary can only "
                    "show changes it detects mechanically, so your own knowledge of what you shipped is the "
                    "backstop (see Review).")
    out += ["## Risk", "", _gate_path_line(gate_state), "", *risk, "",
            "*Impact: a wrong version, or a change the summary could not detect mechanically, is caught by "
            "closing and re-running with the right version — nothing publishes until you merge.*", ""]

    out += pr_section(
        "Validation",
        "The engine's own tooling produced this and `engine-ci` checks it — the mechanical floor.",
        [("- A green check shows the recorded version is well-formed and this summary is complete." if product else
          "- A green check shows the versions agree across all the files that record them, the generated maps "
          "are in sync, and this summary is complete."),
         f"- It does **not** judge whether {engine} is the right version to release — that judgment is yours."],
        f"green means the release conforms to the engine's rules, not that {engine} is the right call.")

    out += pr_section(
        "Review",
        "How to act on this — go ahead, raise the version, or stop.",
        [f"- **Go ahead** — if the summary above matches what you built, merge this; that merge is your consent "
         f"to release {engine}.",
         "- **Want a higher version** — close this and run the release again with a higher version number (a "
         "release can only ever go up, never down).",
         "- **Something's missing** — if you know you changed something that is not listed above" +
         ("" if product else " (for example you removed a capability but do not see it here)") +
         ", close this and run the release again with the "
         "version you know it should be; the summary shows only what it can detect mechanically, so your own "
         "knowledge of what you shipped is the backstop."],
        f"your merge is the binding consent to publish {engine} — the engine never merges this for you.")

    out += pr_section(
        "Files of interest",
        ("Where to look — the recorded product version." if product
         else "Where to look — the recorded versions and the maps that mirror them."),
        (["- `product-version.json` — the recorded version of your product."] if product else
         ["- `.engine/engine.json` and each installed capability's `.engine/modules/<id>/manifest.json` — the "
          "recorded versions.",
          "- `.engine/knowledge/graph.json` and `.engine/self-map.md` — the generated maps, refreshed to match."]),
        "these are the only files this pull request changes.")

    out += pr_section(
        "AI involvement",
        "The engine's release workflow prepared this; the version choice and the decision to publish are yours.",
        [("- It computed the version, recorded it into product-version.json, and opened this for your review."
          if product else
          "- It computed the version, recorded it into the manifests, regenerated the derived maps, and opened "
          "this for your review."),
         "- The version follows the engine's release process; nothing is published until you merge."],
        f"the mechanical steps are the engine's; the decision to publish {engine} is yours.")

    out += ["_Closing this pull request leaves behind the `release/…` branch it was opened from. That branch is "
            "not a release — nothing is released until you merge — and it is safe to delete._"]
    return "\n".join(out)


# --------------------------------------------------------------------------- CLI
def _current_sha() -> "str | None":
    """The commit being released — the workflow's `GITHUB_SHA`, else the local `git rev-parse HEAD`. Used as
    the generate-notes target; a sha not on GitHub simply yields no pull-request list (best-effort)."""
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha.strip()
    try:
        import subprocess
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001 — no sha available -> no pull-request list, never a failure
        return None


def _cmd_propose(args) -> int:
    mode, ctx = release_mode()
    if mode == "refuse":
        print(f"RELEASE-CUT ERROR: your product's version file ({PRODUCT_VERSION_REL}) could not be read — it "
              f'must be a small JSON file with a version, like {{"version": "0.1.0"}}. Fix it, then run the '
              f"release again. Nothing was changed.", file=sys.stderr)
        return 2
    if mode == "product":
        # PRODUCT cut (#516): baseline is the DEPLOYED repo's own last release; no capability tree to diff.
        # A None slug (unresolved origin) forces a first cut — never the engine-home fallback (see _product_baseline).
        baseline = _product_baseline(ctx["slug"])
        merged = ([] if args.baseline_tree
                  else merged_pr_titles(baseline.ref, _current_sha(), repo=ctx["slug"]))
        proposal = _product_proposal(baseline, ctx["current"] or "0.0.0", merged)
        print(json.dumps(proposal, indent=2) if args.json else _render_proposal(proposal))
        return 0
    baseline = resolve_baseline()
    tree, cleanup = _baseline_tree_for(baseline, args.baseline_tree)
    try:
        proposal = classify(baseline, tree)
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    # The pull requests merged since the last release — the body of work beside the floor signals. Skipped when
    # a baseline tree is injected (the tests' / `--baseline-tree` offline path), best-effort otherwise.
    proposal["merged_prs"] = ([] if args.baseline_tree
                              else merged_pr_titles(baseline.ref, _current_sha()))
    print(json.dumps(proposal, indent=2) if args.json else _render_proposal(proposal))
    # A dropped migration key would be silently skipped on a multi-version upgrade (the #599 class at the
    # migration layer) — REFUSE the cut here, before `apply` writes anything. `propose` runs under
    # `set -euo pipefail` in release.yml, so this non-zero exit fails the release job at this step; apply and
    # pr-body never run, so there is no PR body to carry the fact — the refusal message is the whole surface.
    if proposal.get("migration_violations"):
        _print_refusal({"reason": "an upgrade step was dropped", "violations": proposal["migration_violations"],
                        "recovery": "nothing was written and no release was opened. Restore each dropped upgrade "
                                    "step to the capability's settings file; to retire a step, keep its version "
                                    "key and make its action do nothing — never delete the key, or engines that "
                                    "have not yet run it will skip it forever."})
        return 2
    return 0


def _cmd_pr_body(args) -> int:
    proposal = validate.load_json(args.proposal)
    applied = validate.load_json(args.applied)
    print(render_pr_body(proposal, applied, args.gate_state))
    return 0


def _print_refusal(result: dict) -> None:
    """The plain-language reason a cut was refused, to stderr — the one legible account shared by the
    `--json` and human paths, so a refusal always says WHY (never a bare non-zero exit)."""
    print(f"Refused ({result.get('reason')}):", file=sys.stderr)
    for v in result.get("violations", []):
        print(f"  - {v}", file=sys.stderr)
    if result.get("recovery"):
        print(f"To fix: {result['recovery']}", file=sys.stderr)


def _cmd_apply(args) -> int:
    mode, _ctx = release_mode()
    if mode == "refuse":
        print(f"CONFIG ERROR: your product's version file ({PRODUCT_VERSION_REL}) could not be read — it must "
              f'be a small JSON file with a version, like {{"version": "0.1.0"}}. Fix it, then run the release '
              f"again. Nothing was changed.", file=sys.stderr)
        return 2
    if mode == "product":
        # PRODUCT cut (#516): write the one root product-version.json; --all/--package/--proposal (engine
        # package machinery) do not apply to a product and are ignored.
        result = apply_product(args.engine, args.dry_run)
    else:
        packages = {}
        for spec in args.package or []:
            if "=" not in spec:
                print(f"CONFIG ERROR: --package expects id=version, got '{spec}'.", file=sys.stderr)
                return 2
            mid, ver = spec.split("=", 1)
            packages[mid.strip()] = ver.strip()
        proposal = None
        if args.proposal:
            if not os.path.isfile(args.proposal):
                print(f"CONFIG ERROR: the proposal file '{args.proposal}' does not exist. Pass the path to a "
                      f"proposal written by `propose --json`.", file=sys.stderr)
                return 2
            proposal = validate.load_json(args.proposal)
        result = apply(args.engine, getattr(args, "all"), packages, proposal, args.dry_run,
                       min_upgradeable_from=getattr(args, "min_upgradeable_from", None))
    ok = bool(result.get("applied")) or result.get("reason") == "dry-run"
    if args.json:
        print(json.dumps(result, indent=2))
        # The machine-readable refusal goes to stdout (the caller captures it, e.g. into applied.json). Print
        # the plain-language reason to STDERR too, so a refusal is never a bare non-zero exit: the release
        # workflow redirects stdout into a file, so without this the maintainer would see only "exit code 1".
        if not ok:
            _print_refusal(result)
        return 0 if ok else 1
    if result.get("applied"):
        print(f"Applied: engine {result['from_engine']} -> {result['engine']}; "
              f"{len(result['targets'])} package version(s) recorded.")
        return 0
    if result.get("reason") == "dry-run":
        print(f"Dry run: engine {result['from_engine']} -> {result['engine']} across "
              f"{len(result['targets'])} package(s); nothing written.")
        return 0
    _print_refusal(result)
    return 1


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="release_cut.py", description="Decide and record the next engine version.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("propose", help="read-only: the proposed bump floor + change inventory")
    pp.add_argument("--json", action="store_true")
    pp.add_argument("--baseline-tree", help="a local release tree to diff against (tests/workflow inject this)")
    pa = sub.add_parser("apply", help="record the chosen versions into the manifests (atomic, raise-only)")
    pa.add_argument("--engine", required=True, help="the new engine version")
    pa.add_argument("--all", help="set every present package to this version (the first-cut / uniform case)")
    pa.add_argument("--package", action="append", help="id=version override for one package (repeatable)")
    pa.add_argument("--proposal", help="a proposal JSON from `propose` to enforce the confirmed floor against")
    pa.add_argument("--min-upgradeable-from", dest="min_upgradeable_from",
                    help="record the oldest engine release with a clean one-run upgrade path to this release "
                         "(for example 0.3.2) into engine.json; omit to carry any prior floor forward unchanged")
    pa.add_argument("--dry-run", action="store_true", help="compute + validate but write nothing")
    pa.add_argument("--json", action="store_true")
    pb = sub.add_parser("pr-body", help="render the release pull-request body from a proposal + apply-result")
    pb.add_argument("--proposal", required=True, help="the proposal JSON written by `propose --json`")
    pb.add_argument("--applied", required=True, help="the result JSON written by `apply --json`")
    pb.add_argument("--gate-state", default="sub-bar", choices=["passed", "sub-bar", "errored"],
                    help="the acceptance-benchmark outcome to render (only 'sub-bar' is reachable while no "
                         "benchmark measures a release)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "propose":
            return _cmd_propose(args)
        if args.cmd == "pr-body":
            return _cmd_pr_body(args)
        return _cmd_apply(args)
    except Exception as exc:  # plain-language failure, never a traceback
        print(f"\nRELEASE-CUT ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
