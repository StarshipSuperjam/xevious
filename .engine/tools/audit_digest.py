#!/usr/bin/env python3
"""The audit digest (audit-library) — the engine's committed, plain-language self-attestation
of its own operational health, and the two rules that protect it.

The engine's periodic self-review (the audit) produces a short, human-readable file —
`.engine/audits/audit-digest.md` — that says, in plain words, what it looked at, what it found, and
what it recommends. It is committed so a non-engineer can open it in the repo and read it (the self-map
precedent), and it carries its run-date. This module ships the deterministic machinery around that file;
the scheduled run that actually WRITES one lands later (the audit persona writes the prose; this tool
seals it). Until then no digest exists, and the two rules below handle that honestly.

Unlike the self-map or the knowledge graph, the digest is NOT source-deterministic — it is run-dated,
judgment-bearing prose, so it cannot be regenerated-and-byte-compared. So "fingerprint-gated so it cannot
silently drift" (the audits design) is realized as a SELF-SEAL: the file carries a check-value computed
over its own run-date + body, and the gate recomputes that value and compares. A hand-edit that changes
the file after the audit wrote it — without re-running the audit to re-seal — no longer matches, and the
gate goes red. This catches a *silent* hand-edit, exactly as the self-map gate catches a silent edit of a
generated file; a deliberate re-seal (re-running the audit) passes, which is the intended way to change it.

SEAL INVARIANT (load-bearing): the seal hashes the PARSED `generated` scalar (read through
validate.frontmatter, which normalizes a YAML date to a stable ISO string) plus the RAW body bytes (the
text after the closing `---` fence, read with newline='' so a CRLF/LF change is caught, not masked). It
never hashes the frontmatter's serialized text, so re-quoting or re-ordering the header cannot affect
validity — only a change to the run-date or the body can. The `fingerprint` field is never part of its own
input.

Library + CLI (mirrors self_map.py — plain language first):

  uv run --directory .engine -- python tools/audit_digest.py seal <file> [YYYY-MM-DD] [--body-file P]  # stamp + seal
  uv run --directory .engine -- python tools/audit_digest.py check [<file>]            # is the seal intact?
  uv run --directory .engine -- python tools/audit_digest.py staleness [<file>]        # how fresh is it?
  uv run --directory .engine -- python tools/audit_digest.py body [<file>]             # the review prose, frontmatter stripped
  uv run --directory .engine -- python tools/audit_digest.py prior [--limit N]         # the engine's own recent digests, as over-time corroboration
  uv run --directory .engine -- python tools/audit_digest.py memory                     # the project's own backed-up saved beliefs (concern #1), or an honest disclosure

The two CI/audit-prep rules are thin custom/script entries over check()/staleness():
audit_digest_fingerprint_check.py (the CI seal gate) and audit_digest_staleness_check.py (the report-only
freshness signal). finding.v1 + the frontmatter reader are reused from validate.py via the sibling-import
precedent.
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402
import moment  # noqa: E402  (the time seam — today_utc: the UTC calendar day the digest must date by)
import github_client  # noqa: E402  (the shared authenticated GitHub API client; request-build + decode)
import repo_identity  # noqa: E402  (resolve_default_branch — the shared default-branch resolver)


# The committed digest's home: a file under .engine/audits/ (already a registered infra dir, beside the
# concern-list), audits-system-owned data, not a catalogued surface. It does NOT exist until the scheduled
# run first writes one — both rules below treat its absence as a first-class, honest state.
AUDIT_DIGEST_PATH = os.path.join(validate.ENGINE_DIR, "audits", "audit-digest.md")

# How long the engine may go without a self-review before the freshness signal warns. A plain bound, not a
# tuned parameter; re-tunable by editing this one line. Pinned by a unit test so it cannot drift silently.
STALENESS_DAYS = 30

# The action the freshness signal names when the self-review has stopped — the design's pinned re-arm verb.
REARM_HINT = "enable it with `gh workflow enable …` or push a commit to re-arm it"


# ---- small shared helpers --------------------------------------------------------------------

def _display(path: str) -> str:
    """A path for human messages: repo-relative inside the repo, else absolute — never a `../..` chain
    (matters for the demo's throwaway copy outside the repo). Mirrors self_map._display."""
    rel = os.path.relpath(path, validate.ROOT)
    return rel.replace(os.sep, "/") if not rel.startswith("..") else os.path.abspath(path)


def _loc_opt(path: str):
    """A finding.v1 location (repo-relative) — or None when the path is outside the repo. Mirrors
    self_map._loc_opt / wiring._loc_opt."""
    rel = os.path.relpath(path, validate.ROOT)
    return None if rel.startswith("..") else {"file": rel.replace(os.sep, "/"), "line": None}


def _iso(value) -> str:
    """The run-date as a stable ISO `YYYY-MM-DD` string, whether it arrives as a date (the seal verb's
    default) or an already-parsed string (frontmatter, normalized by validate._json_model). Both sides of
    the seal read the date this way, so the seal is independent of how the header was serialized."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()[:10]
    return str(value)[:10]


# ---- the seal (pure; fixture-testable) -------------------------------------------------------

def compute_seal(generated: str, body: str) -> str:
    """The self-seal: sha256 over the run-date + the raw body. Never includes the frontmatter's
    serialized text or the fingerprint field — only a change to the date or the body moves it."""
    digest = hashlib.sha256((generated + "\n" + body).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def split(path: str):
    """Return (frontmatter_dict, body_text) for a digest file. The dict is read through the engine's own
    frontmatter reader (validate.frontmatter — raises loudly on malformed YAML, {} on no front-matter).
    The body is the exact bytes after the closing `---` fence, read with newline='' so a CRLF/LF change
    is not masked. A file with no front-matter has the whole text as its body."""
    with open(path, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    fm = validate.frontmatter(path)
    parts = raw.split("---", 2)  # maxsplit=2: a `---` rule inside the body stays in the body
    body = parts[2] if (raw.startswith("---") and len(parts) == 3) else raw
    return fm, body


# ---- the two gates as pure functions (no IO beyond the read) ---------------------------------

def check(path: str | None = None) -> dict:
    """The fingerprint gate. `note` when no digest exists yet (nothing to verify) or the seal is intact;
    `hard` when the file is present but malformed, or its contents no longer match the recorded seal (a
    silent hand-edit). `path` defaults to the live digest, resolved at call time so a test may redirect."""
    path = AUDIT_DIGEST_PATH if path is None else path
    name, where = _display(path), _loc_opt(path)
    if not os.path.isfile(path):
        return validate.finding("note", f"No self-review file exists yet ({name}); nothing to verify.", where)
    try:
        fm, body = split(path)
    except Exception:
        return validate.finding(
            "hard",
            f"The engine's self-review file ({name}) is present but unreadable (its header is malformed). "
            f"Re-run the audit so it rewrites the file.",
            where)
    generated, fingerprint = fm.get("generated"), fm.get("fingerprint")
    if not generated or not fingerprint:
        return validate.finding(
            "hard",
            f"The engine's self-review file ({name}) is missing its run-date or its check-value, so it "
            f"cannot be verified. Re-run the audit so it rewrites the file.",
            where)
    if compute_seal(_iso(generated), body) != fingerprint:
        return validate.finding(
            "hard",
            f"The engine's self-review file ({name}) has been changed since the audit wrote it — its "
            f"contents no longer match the value the audit recorded. Re-run the audit to refresh the "
            f"file, or revert the hand-edit.",
            where)
    return validate.finding(
        "note", f"The engine's self-review file ({name}) matches the record the audit wrote.", where)


def staleness(path: str | None = None, now: datetime.date | None = None) -> dict:
    """The freshness signal. `note` when the digest is current; a `soft` (advisory, never blocking)
    finding when the engine has not self-reviewed in more than STALENESS_DAYS days, OR when no self-review
    has run yet — so a self-review that quietly stopped (an expired token, a disabled schedule, a setup
    never finished) is surfaced on the operator's return rather than missed. `now` defaults to the UTC
    calendar day inside the function — the day the digest's run-date is sealed in, so the two agree (the
    thin script calls it arg-less; tests inject a fixed date)."""
    path = AUDIT_DIGEST_PATH if path is None else path
    now = moment.today_utc() if now is None else now
    name, where = _display(path), _loc_opt(path)
    if not os.path.isfile(path):
        return validate.finding(
            "soft",
            f"The engine's self-review hasn't run yet — set up the scheduled self-review so the engine "
            f"can start checking its own health, or ask me to set it up for you. Once it runs, this notice "
            f"clears.",
            where)
    try:
        fm, _body = split(path)
        run_date = datetime.date.fromisoformat(_iso(fm.get("generated")))
    except Exception:
        return validate.finding(
            "soft",
            f"The engine's self-review file ({name}) is present but its run-date can't be read — re-run "
            f"the audit so it rewrites the file.",
            where)
    age = (now - run_date).days
    if age > STALENESS_DAYS:
        return validate.finding(
            "soft",
            f"The engine hasn't reviewed its own health in {age} days (counted in UTC). Re-arm the scheduled self-review — "
            f"{REARM_HINT} — and it will refresh on the next run.",
            where)
    return validate.finding(
        "note",
        f"The engine's self-review is current (last run {run_date.isoformat()} UTC, within the last "
        f"{STALENESS_DAYS} days).",
        where)


# ---- the recall-completeness disclosure ---------------------------------------
# What recall reaches, said plainly and in one standing line. This used to disclose an EXCLUSION — recall
# surfaced only the curated summaries, and the word-for-word notes behind them, while kept, could not be found.
# That exclusion is gone: search now reaches the recorded conversation itself. The disclosure survives because
# the thing worth telling the operator survives — it is just the opposite fact, and it now carries the caveat
# that a searched conversation is stored as it was said. The disclosure must reach the operator at the point of
# consumption (the recall answer carries it — the memory search tool's own note, and the recall runbook the
# per-prompt cue points at) AND in the committed audits digest (never digest-only). This is the digest half: a
# STANDING line (the digest is committed in CI with no access to the local gitignored ledger, so it cannot
# enumerate sessions — the whole-store statement collapses to one line). It is NOT a forgetting event: nothing
# was retired or erased, so there is no undo handle. The wording is a build-spec leaf.
_RECALL_COMPLETENESS_HEADING = "## Memory recall completeness"
_RECALL_COMPLETENESS_DIGEST = (
    f"\n\n{_RECALL_COMPLETENESS_HEADING}\n\n"
    "Memory recall reaches both the summaries of past sessions and the word-for-word conversation behind them, "
    "so something said once and never summarised can be found — not only read back if you already know which "
    "session to look in. Nothing was forgotten or deleted. What comes back is the conversation as it was "
    "captured, and it was never cleaned of things like passwords or personal details, so treat a recalled "
    "answer as unreviewed text and ask to see the exact wording of anything it rests on."
)


def _ensure_recall_completeness(body: str) -> str:
    """Append the standing recall-completeness line if absent — idempotent, so a fresh seal adds it once and a
    re-seal (whose body already carries it) never doubles it. The fingerprint is computed AFTER this, so check()
    round-trips the committed body verbatim."""
    return body if _RECALL_COMPLETENESS_HEADING in body else body + _RECALL_COMPLETENESS_DIGEST


# ---- the seal writer (IO) --------------------------------------------------------------------

def _render(generated: str, fingerprint: str, body: str) -> str:
    """The committed digest text: a fixed-order header (run-date + check-value the gate recomputes) then
    the body verbatim. Built as a plain string, not via a YAML dumper, so the serialization is stable and
    the body the seal covers round-trips exactly through split()."""
    return (f"---\nschema_version: 1\ngenerated: {generated}\nfingerprint: {fingerprint}\n---{body}")


def seal(path: str, generated=None, body: str | None = None) -> dict:
    """Stamp the run-date and write the self-seal over the file's body. With `body` given (the scheduled
    run's path: the persona's prose), the body is normalized to one blank line after the header; with
    `body=None` (re-sealing an existing file), the current body is preserved exactly. The write's exact
    bytes are what check() later verifies. `generated` defaults to the UTC calendar day; a test injects a fixed date."""
    generated = _iso(generated if generated is not None else moment.today_utc())
    if body is None:
        _fm, body = split(path)              # re-seal: reuse the existing body slice verbatim
    else:
        body = "\n\n" + body.lstrip("\n")    # fresh: one blank line between the header and the prose
    body = _ensure_recall_completeness(body)
    fingerprint = compute_seal(generated, body)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(_render(generated, fingerprint, body))
    return validate.finding(
        "note", f"Sealed the self-review file ({_display(path)}), dated {generated}.", _loc_opt(path))


# ---- the audit-over-audit corroboration read: the digest's own recent history --------
#
# The scheduled self-review reads its own most recent committed digests as over-time CORROBORATION —
# never as a decision (a keep/retire call still rests on a fresh check run THIS cycle; the persona's
# guardrails fix that). This is a same-repo read on the workflow's own token, so it needs no new auth.
# We read the last N committed versions of the digest directly via the GitHub API on the normal shallow
# checkout — deliberately NOT an `actions/checkout fetch-depth: 0` deep clone, whose cost grows unbounded
# with repo age just to read one file's history. The transport is injectable, so tests fake ONLY the
# network and run the real logic. When the history is unreadable (a fresh repo with none yet, a transient
# GitHub blip), the read degrades to a plain marker so the self-review says — plainly — that it has no
# earlier reviews to compare against, and never fabricates a trend.

USER_AGENT = "engine-audit-digest"

# The digest's repo-relative path — what the commits/contents API key on (AUDIT_DIGEST_PATH is absolute).
DIGEST_REPO_PATH = os.path.relpath(AUDIT_DIGEST_PATH, validate.ROOT).replace(os.sep, "/")

# The corroboration window: how many of the most recent committed digests the self-review is fed
# (oldest→newest). A plain bound — a build-spec leaf recorded with the maintainer — wide enough to catch
# an intermittent finding that comes and goes across cycles, not only one that persists every run. Each
# digest is the engine's own small output (~a few KB), so the whole window stays small. Pinned by a unit
# test so it cannot drift silently. (GitHub caps a single commits page at 100; a window past that would
# need pagination, which a "recent" read never wants, so the per-page request is clamped to 100.)
PRIOR_DIGESTS_DEFAULT_LIMIT = 20

# A generous per-digest safety cap (characters of body fed). Real digests are ~5KB; this only guards a
# pathological case and sits far above any real digest, so it never truncates a finding out of the window.
PRIOR_DIGEST_MAX_CHARS = 32 * 1024

# The persona-facing marker when there is nothing to corroborate against — no history yet, or a read
# failure. Plain and instructive, never backstage vocabulary: it tells the self-review to review only
# what it can check now and to disclose the gap, so a degraded read can never read as a clean trend.
_PRIOR_NONE_MARKER = (
    "PRIOR SELF-REVIEWS: none are available to compare against this run. Review only what you can check "
    "now, and say plainly in your digest that you have no earlier reviews to compare against — do not "
    "invent a trend or a change over time.")


class DegradedReadError(Exception):
    """Raised when the digest's own history cannot be read from GitHub. It is NEVER swallowed as 'no
    history' — an unreadable history degrades to an honest 'nothing to compare against' marker, never a
    silent empty that would let the self-review present a clean trend it never actually saw."""


def _split_text(raw: str):
    """(frontmatter_text, body) for IN-MEMORY digest content — the string analogue of split(), which
    reads a file path. Same `---`-fence rule: maxsplit=2 keeps an in-body `---` rule inside the body."""
    parts = raw.split("---", 2)
    if raw.startswith("---") and len(parts) == 3:
        return parts[1], parts[2]
    return "", raw


def _generated_of(fm_text: str):
    """The `generated:` run-date from a digest's frontmatter text, or None. A light line scan — the prior
    digests arrive from the API as strings (not files on disk), so validate.frontmatter (which reads a
    path) does not apply, and only the date label is needed here; the seal itself is verified at commit
    time, never on this read."""
    for line in fm_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("generated:"):
            return stripped[len("generated:"):].strip() or None
    return None


class _DigestHistory:
    """The committed digest's own recent-history boundary (same-repo, own-repo token). Mirrors telemetry's
    injectable-transport seam: `transport(method, path, body) -> (status, json|None)` is injectable so
    tests and the demo fake ONLY the network and run the real logic. NOT telemetry.GitHubIssues, which is
    issue-shaped — this reads the commits + contents APIs for one file's history."""

    def __init__(self, repo: str, token: str, *, path: str = DIGEST_REPO_PATH,
                 base: "str | None" = None, transport=None):
        self.repo = repo
        self.token = token
        self.path = path
        # The branch prior digests are read from — the same base the digest pull requests target. Resolved
        # here (NOT a module-level default arg pinned at import) from GITHUB_DEFAULT_BRANCH (which audit-prep
        # sets) -> recorded manifest -> origin/HEAD -> "main", so a `master` repo reads its own history.
        self.base = base or repo_identity.resolve_default_branch(env_var="GITHUB_DEFAULT_BRANCH")
        self._transport = transport or self._http

    def _http(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = github_client.request(path, self.token, user_agent=USER_AGENT, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:           # 4xx/5xx — surface the status, never swallow
            return exc.code, None
        except urllib.error.URLError as exc:             # network unreachable — a read failure
            raise DegradedReadError(f"GitHub is unreachable: {exc}") from exc

    def recent(self, limit: int) -> list:
        """The last `limit` committed digests, OLDEST→NEWEST, as (generated_date_or_None, body). The
        commits API returns newest-first, so we reverse to feed oldest→newest. Returns [] when the file
        has no history yet (a fresh repo, or the path was never committed); RAISES DegradedReadError on
        any read failure — an unreadable history must never read as 'no history'."""
        per_page = min(100, max(1, int(limit)))   # GitHub caps a commits page at 100; a recent read never paginates
        status, commits = self._transport(
            "GET",
            f"/repos/{self.repo}/commits?path={self.path}"
            f"&sha={urllib.parse.quote(self.base, safe='')}&per_page={per_page}",
            None)
        if status in (404, 409):   # 404 path/branch never committed, 409 empty repo — no history yet
            return []
        if status >= 400 or commits is None:
            raise DegradedReadError(f"GitHub returned {status} listing the digest history")
        out = []
        for commit in reversed(commits):     # newest-first → feed oldest→newest
            sha = commit.get("sha")
            if not sha:
                continue
            cstatus, payload = self._transport(
                "GET", f"/repos/{self.repo}/contents/{self.path}?ref={sha}", None)
            if cstatus >= 400 or payload is None or "content" not in payload:
                raise DegradedReadError(f"GitHub returned {cstatus} reading a digest at {sha[:8]}")
            raw = github_client.decode_content(payload, codec="utf-8")
            fm_text, body = _split_text(raw)
            out.append((_generated_of(fm_text), body))
        return out


def render_prior_digests(repo: str, token: str, *, limit: int = PRIOR_DIGESTS_DEFAULT_LIMIT,
                         transport=None) -> str:
    """Plain text the audit-prep workflow feeds into the read-only self-review persona's prompt so it can
    read its own recent digests as over-time CORROBORATION (never a decision). The persona never reaches
    GitHub itself — this owns the read and the workflow injects the result. Returns a header plus each
    prior digest oldest→newest (with its run-date); or — when there is NO history yet OR on ANY read
    failure — a plain 'nothing to compare against' marker (NEVER a silent empty, NEVER a fabricated
    trend), so the self-review honestly degrades to a review of only what it can see now. `transport` is
    injectable for tests; bounded by `limit`."""
    try:
        history = _DigestHistory(repo, token, transport=transport).recent(limit)
    except Exception as exc:  # noqa: BLE001 — any read failure degrades to honest, never a silent empty
        return _PRIOR_NONE_MARKER + f"  (the earlier reviews could not be read this run — {exc})"
    if not history:
        return _PRIOR_NONE_MARKER
    parts = [
        f"PRIOR SELF-REVIEWS — the engine's own {len(history)} most recent self-reviews, oldest first. "
        "Read these ONLY as corroboration: if a finding keeps showing up across them, that is evidence it "
        "is genuinely there rather than a one-time blip. They never decide anything — a keep-it-or-"
        "retire-it call still rests on the fresh check you run THIS cycle, and a quiet stretch is never "
        "read as 'gone'. Where this run's own fresh read agrees with what these earlier reviews showed, "
        "present that as a separate point from the fresh read itself, not as the reason for the call.", ""]
    for date, body in history:
        body = body.strip()
        if len(body) > PRIOR_DIGEST_MAX_CHARS:
            body = body[:PRIOR_DIGEST_MAX_CHARS] + "\n…(earlier review truncated)"
        # The run-date is read from the prior digest's own frontmatter; defang it too so an injected value
        # can't smuggle dash rails into this separator line (a normal ISO date has no 3-dash run, so this is
        # a no-op for it). The separator's OWN rails are engine-emitted, never untrusted.
        parts.append(f"----- prior self-review (run {validate.defang_prompt_fence_markers(date or 'date unknown')}) -----")
        if body:
            # A prior digest's body is fed BETWEEN the workflow's fence markers (----- BEGIN/END PRIOR
            # SELF-REVIEWS -----) and between the per-digest separators above. A self-review's prose can
            # describe this very machinery, so defang any line in it that mimics a fence marker — it cannot
            # then forge a separator or prematurely close the section in the persona's prompt.
            parts.append(validate.defang_prompt_fence_markers(body))
    return "\n".join(parts)


# ---- the saved-memory coverage read: the project's own backed-up beliefs, for concern #1 ----
#
# Concern #1 (stale saved-memory beliefs) needs the project's experiential memory, which is gitignored and so
# absent from any committed-files-only run. The self-review reaches it by reading the operator's off-repo memory
# BACKUP — a pure read of what memory has committed there. Memory owns the fetch + the durable-belief selection
# (restore_vault.read_saved_memory); this renders the result for the read-only persona and, when the backup
# can't be read this run, DISCLOSES the gap honestly — phrased about what THIS REVIEW could reach, never an
# absolute "the project has no memory" — so the headline check is never silently skipped nor falsely cleared.
# (On the public template the committed pointer is the unconfigured placeholder, so this always degrades to the
# not-configured disclosure with no network; the live read fires only in a repo configured with vault access.)

# A total-character bound on the rendered beliefs. It was written as a guard on a pathological case, on the
# reasoning that "a mature store holds ~hundreds" of short notes and so would sit far below it. Measured against
# a real store, that reasoning does not hold: 752 live durable beliefs carrying 674 KB of text, more than ten
# times this bound. It was already exceeded before the archived-tier age-out was removed (only one belief in
# that store scored archived), so the age-out was never what kept it under — but nothing reclaims a belief now,
# so the gap only widens.
#
# Two consequences the next reader should not have to rediscover. The truncation is newest-first, so what it
# drops is the OLDEST beliefs — which is the opposite of what a review of STALE saved memory wants to see. And
# raising the number is not obviously the fix: the audit persona has a context budget of its own. Naming the
# right bound belongs with the curation-removal work, which is where the store's growth is addressed.
SAVED_MEMORY_MAX_CHARS = 64 * 1024

# Persona-facing disclosure markers — instructions, in plain words, for when the saved memory could not be read.
# Each says what THIS REVIEW could observe (never "you have no memory"), tells the persona to treat its saved
# memory as not reviewed and say so plainly, and NEVER to claim memory is empty. The not-configured case names the way to
# turn it on (set up a memory backup); the whole memory-backup UX is conversational, so "ask me to set one up" is
# the real, findable action (mirrors restore_vault's own _MSG_NOT_CONFIGURED register).
_SAVED_MEMORY_NOT_CONFIGURED = (
    "YOUR SAVED MEMORY: I couldn't review your saved decisions this cycle — no memory backup is set up for this "
    "review to read. Treat your saved memory as not reviewed and say so plainly, and that the way to turn it on is to "
    "set up a memory backup — the operator can ask the engine to set one up in a chat session. NEVER claim the "
    "project has no saved memory; you just could not see it this run.")
_SAVED_MEMORY_ACCESS_NOT_GRANTED = (
    "YOUR SAVED MEMORY: a memory backup is set up, but this review wasn't given access to it this cycle — its "
    "read token is missing, expired, or scoped to the wrong place (a standing setup gap, not a passing glitch). "
    "Treat your saved memory as not reviewed and say so plainly: the fix is to re-issue the backup's read-only token and "
    "re-set the MEMORY_VAULT_TOKEN secret — this is NOT `claude setup-token`, which is the separate token that "
    "runs this review, so re-running that won't help. NEVER claim memory is empty.")
_SAVED_MEMORY_UNREACHABLE = (
    "YOUR SAVED MEMORY: a memory backup is set up and this review can reach for it, but the connection failed this "
    "cycle. Treat your saved memory as not reviewed and say so plainly — note it is likely transient and may clear on the "
    "next run, so no setup change is needed yet. NEVER claim memory is empty.")
_SAVED_MEMORY_UNREADABLE = (
    "YOUR SAVED MEMORY: a memory backup is set up, but I couldn't read a usable copy of your saved memory from it "
    "this cycle (it may clear on a later run). Treat your saved memory as not reviewed and say so plainly; NEVER claim "
    "memory is empty.")
_SAVED_MEMORY_NONE_YET = (
    "YOUR SAVED MEMORY: your memory backup is set up and I read it, but it holds no saved decisions or notes yet "
    "to review (as last backed up {as_of}). Your saved memory has nothing to check this cycle — say so plainly; this is "
    "NOT the same as the backup being missing or unreadable.")
_SAVED_MEMORY_PUBLIC_AGGREGATE_HEADER = (
    "YOUR SAVED MEMORY — the saved decisions and notes the engine has kept for you, as last backed up {as_of}. "
    "Review them HERE, IN THIS RUN: do any now contradict each other, has anything you can see "
    "refuted one, or is a note the project still leans on actually obsolete? You are reading these from the backup (you can't "
    "reach them yourself); treat them as what the engine had saved as of that backup. IMPORTANT — this project is "
    "public, or its visibility could not be confirmed private, and your summary is committed where anyone could "
    "read it. So in that committed summary report ONLY HOW MANY of these {n} look stale (for example: \"N of your "
    "{n} saved notes look stale\"). NEVER name, quote, or paraphrase any specific note, decision, or its content "
    "— the stale count and your concern about it are all that may leave this run. Then tell the operator, plainly, "
    "the two safe ways to see WHICH notes are stale: (1) ask the engine to review your saved memory in an ordinary "
    "chat session — there it reads your own copy on your computer and nothing is committed or published, so it can "
    "name the specific notes safely; (2) keep this project private, or give it its own private memory vault (just "
    "ask the engine to set one up) — then this review can name them here. Do NOT ask for, guess at, or reconstruct "
    "anything beyond what is given below, and NEVER claim memory is empty. {n} note(s) follow, most-recently-used "
    "first.")
_SAVED_MEMORY_HEADER = (
    "YOUR SAVED MEMORY — the saved decisions and notes the engine has kept for you, as last backed up {as_of}. "
    "Review them: do any now contradict each other, has anything you can see refuted one, or is a "
    "heavily-used note actually obsolete? You are reading these from the backup (you can't reach them yourself); "
    "treat them as what the engine had saved as of that backup. {n} note(s) follow, most-recently-recorded first.")

# fetch error code -> the disclosure marker (the corrected two-part split, #224). not-configured = no backup
# for this run; no-token = set up but THIS RUN wasn't granted access (a standing credential gap → re-arm the vault
# token); unreachable = set up + access exists but the connection failed (transient); the rest = reachable but no
# usable copy could be read. no-token is named DISTINCTLY from unreachable because `fetch_snapshot` returns no-token
# only when the token is absent (a deterministic operator fix), never on a transient blip (which is `unreachable`).
_SAVED_MEMORY_ERROR_MARKERS = {
    "not-configured": _SAVED_MEMORY_NOT_CONFIGURED,
    "no-token": _SAVED_MEMORY_ACCESS_NOT_GRANTED,
    "unreachable": _SAVED_MEMORY_UNREACHABLE,
    "no-backup-data": _SAVED_MEMORY_UNREADABLE,
    "namespace-missing": _SAVED_MEMORY_UNREADABLE,
    "corrupt": _SAVED_MEMORY_UNREADABLE,
}

# Memory's record `role`/`kind` (backstage vocabulary) -> plain operator words. The render NEVER prints a raw
# role/kind/"episodic"/"gist" label — only these plain phrases reach the persona's prompt and the digest.
_ROLE_PLAIN = {
    "decision": "a decision you made",
    "rationale/pushback": "a reason or a pushback",
    "lesson": "a lesson",
    "dead-end": "a dead end you hit",
    "preference": "a preference of yours",
    "intent": "something you meant to do",
    "observation": "an observation",
}


def _belief_plain_role(kind, role) -> str:
    if kind == "gist":
        return "a summary of older notes"
    return _ROLE_PLAIN.get(role or "", "a note")


def _project_repo_is_private() -> bool:
    """Selects the committed-digest OUTPUT MODE for the saved-memory review (#224). Read from the
    workflow env `MEMORY_AUDIT_REPO_VISIBILITY`, set by audit-prep's own-repo-token detection step (the vault
    token is scoped to the vault and cannot read the project repo; the `schedule` trigger's event payload omits
    visibility — so the value is detected on a dedicated step, never inferred here).

    On a confirmed-private repo the digest may NAME specific saved notes; on a public-or-unconfirmed repo it runs
    in aggregate-only mode — the persona reviews the same notes in-run, but its committed summary may report ONLY
    the stale COUNT plus the two safe levers, never a specific (the belief text reaches the persona's
    ephemeral prompt in BOTH modes via `render_saved_memory`; this gate governs only what may be COMMITTED, by
    selecting which instruction header leads that feed). DEFAULT-SAFE and load-bearing: ONLY an explicit `private`
    opens the naming mode; anything else — `public`, `internal`, empty, unset, or a non-Actions/local run where it
    can't be confirmed — is treated as not-private, so the digest stays aggregate-only rather than risk committing
    a specific where it could be public. Keep this an exact `== "private"` test; never relax it to an `in (...)`
    allow-list, which would silently widen the naming mode."""
    return os.environ.get("MEMORY_AUDIT_REPO_VISIBILITY", "").strip().lower() == "private"


def _epoch_date(ts):
    """An epoch int as a stable `YYYY-MM-DD` UTC string, or None — for the plain when-recorded / last-used hint."""
    if not isinstance(ts, int) or isinstance(ts, bool):
        return None
    try:
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _saved_memory_as_of(iso) -> str:
    """The backup date as a plain `on YYYY-MM-DD` phrase, or a plain unknown-date phrase. The date is VALIDATED
    as a real ISO date (not just defanged): a forged manifest timestamp that is not a clean date — including a
    letterless dash-rail run, which the shape-based defang deliberately leaves alone — degrades to the
    unknown-date phrase, so no untrusted fragment can ride this header line into the persona's prompt."""
    head = iso[:10] if isinstance(iso, str) and len(iso) >= 10 else ""
    try:
        return f"on {datetime.date.fromisoformat(head).isoformat()}"
    except ValueError:
        return "at an unknown date"


def _render_belief_line(b: dict) -> str:
    role = _belief_plain_role(b.get("kind"), b.get("role"))
    text = " ".join((b.get("text") or "").split())   # collapse whitespace so a multi-line note renders as one line
    recorded, used = _epoch_date(b.get("recorded_ts")), _epoch_date(b.get("last_access_ts"))
    when = []
    if recorded:
        when.append(f"recorded {recorded}")
    if used and used != recorded:
        when.append(f"last used {used}")
    suffix = f"  ({'; '.join(when)})" if when else ""
    return f"- {role}: {text}{suffix}"


def render_saved_memory(transport=None) -> str:
    """Plain text the audit-prep workflow feeds into the read-only self-review persona's prompt so it can work
    concern #1 (stale saved-memory beliefs). Memory owns the read + the durable-belief selection
    (restore_vault.read_saved_memory); this renders the live saved beliefs for the persona, or — when the backup
    is absent/unreachable/unreadable — returns a plain disclosure marker that says so honestly (about what this
    review could reach) and NEVER claims memory is empty. `transport` is injectable for tests. Never raises."""
    try:
        from memory import restore_vault   # lazy: keep restore_vault->backup_vault->boot off the seal-gate import
        snap = restore_vault.read_saved_memory(transport=transport)
    except Exception as exc:  # noqa: BLE001 — any read failure degrades to an honest disclosure, never a raise
        return _SAVED_MEMORY_UNREADABLE + f"  (the saved memory could not be read this run — {exc})"
    if not snap.get("ok"):
        return _SAVED_MEMORY_ERROR_MARKERS.get(snap.get("error") or "", _SAVED_MEMORY_UNREADABLE)
    beliefs = snap.get("beliefs") or []
    as_of = _saved_memory_as_of(snap.get("as_of"))
    if not beliefs:
        return _SAVED_MEMORY_NONE_YET.format(as_of=as_of)
    # Visibility gate (#224): the SAME belief lines feed the persona in both modes — it needs to SEE
    # the notes to judge which look stale (a semantic call, not a stored field). The gate selects only the
    # instruction header, i.e. what the persona may COMMIT: on a confirmed-private repo it may name specifics; on a
    # public/unconfirmed repo it reports only the stale count + the two safe levers (never a specific). Default-safe.
    header = _SAVED_MEMORY_HEADER if _project_repo_is_private() else _SAVED_MEMORY_PUBLIC_AGGREGATE_HEADER
    parts = [header.format(as_of=as_of, n=len(beliefs)), ""]
    total = 0
    for b in beliefs:
        line = _render_belief_line(b)
        total += len(line) + 1
        if total > SAVED_MEMORY_MAX_CHARS:
            parts.append("…(further saved notes omitted to keep this readable)")
            break
        parts.append(line)
    # Every belief is the operator's own saved text fed BETWEEN the workflow's ----- BEGIN/END YOUR SAVED MEMORY
    # ----- markers; defang any line that mimics a fence marker so a saved note can never forge or close it.
    return validate.defang_prompt_fence_markers("\n".join(parts))


# ---- CLI ------------------------------------------------------------------------------------

def _take_body_file(argv: list):
    """Pull an optional `--body-file PATH` pair out of argv (wherever it sits) and return
    (argv_without_it, body_text|None). The scheduled run passes the captured persona prose this way; the
    pair is removed BEFORE the positional file/date are read, so `--body-file` can never be mis-parsed as
    the file path (argv[1]) or the run-date (argv[2])."""
    out, body, i = [], None, 0
    while i < len(argv):
        if argv[i] == "--body-file":
            if i + 1 >= len(argv):
                raise ValueError("--body-file needs a file path")
            with open(argv[i + 1], encoding="utf-8", newline="") as fh:
                body = fh.read()
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out, body


def _prior_cli(argv: list) -> int:
    """The audit-prep workflow's audit-over-audit verb: print the engine's own recent committed digests
    (oldest→newest) so the read-only self-review can read them as over-time corroboration — the persona
    never reaches GitHub itself. Reads GITHUB_REPOSITORY + GITHUB_TOKEN from the environment (the GitHub
    token, never the Claude OAuth token, which only auths the persona run). Optional `--limit N` overrides
    the window (default PRIOR_DIGESTS_DEFAULT_LIMIT). Exits 0 whenever the env is present — even with no
    history or on a read failure, which it reports in-band so the self-review degrades honestly rather
    than failing; a transient GitHub blip must never fail the self-review."""
    limit = PRIOR_DIGESTS_DEFAULT_LIMIT
    i = 0
    while i < len(argv):
        if argv[i] == "--limit":
            if i + 1 >= len(argv):
                print("usage: audit_digest.py prior [--limit N]   (--limit needs a number)", file=sys.stderr)
                return 2
            try:
                limit = max(1, int(argv[i + 1]))
            except ValueError:
                print("usage: audit_digest.py prior [--limit N]   (--limit needs a number)", file=sys.stderr)
                return 2
            i += 2
            continue
        i += 1
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("usage: audit_digest.py prior [--limit N]   (needs GITHUB_REPOSITORY and GITHUB_TOKEN in "
              "the environment; it uses the GitHub token, never the Claude token)", file=sys.stderr)
        return 2
    print(render_prior_digests(repo, token, limit=limit))
    return 0


def _saved_memory_cli(argv: list) -> int:
    """The audit-prep workflow's saved-memory verb: print the project's own backed-up saved beliefs for the
    read-only self-review (concern #1), or — when the backup can't be read — a plain disclosure marker. Unlike
    `prior`, it takes NO env guard: the default not-configured path has no token and MUST still print a
    disclosure and exit 0 (the repo + token are resolved one layer down, from memory's committed pointer +
    boot.gh_token(), inside read_saved_memory — never from GITHUB_REPOSITORY here). A read failure is disclosed
    in-band, never a non-zero exit; a transient gap must never fail the self-review."""
    print(render_saved_memory())
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "check"
    try:
        if cmd == "seal":
            rest, body = _take_body_file(argv)   # strip --body-file PATH before any positional read
            if len(rest) < 2:
                print("usage: audit_digest.py seal <file> [YYYY-MM-DD] [--body-file PATH]", file=sys.stderr)
                return 2
            if body is not None and not body.strip():
                # The scheduled run captured nothing (the self-review produced no prose) — refuse rather than
                # commit an empty digest. The bare re-seal path (body is None) is unaffected.
                print("ERROR: the captured self-review is empty — nothing to seal.", file=sys.stderr)
                return 2
            gen = rest[2] if len(rest) > 2 else None
            print(validate.fmt(seal(rest[1], generated=gen, body=body)))
            return 0
        if cmd == "check":
            print(validate.fmt(check(argv[1] if len(argv) > 1 else None)))
            return 0
        if cmd == "staleness":
            print(validate.fmt(staleness(argv[1] if len(argv) > 1 else None)))
            return 0
        if cmd == "body":
            # Print the digest's prose with its YAML frontmatter stripped — what the scheduled run uses as
            # the digest pull request's body, so the operator reads the actual review rather than
            # boilerplate. The file must already exist (the seal step runs first); a missing file is a loud
            # error, never an empty pull-request body.
            path = argv[1] if len(argv) > 1 else AUDIT_DIGEST_PATH
            if not os.path.isfile(path):
                print(f"ERROR: no self-review file at {_display(path)} to read a body from.", file=sys.stderr)
                return 2
            _fm, body = split(path)
            print(body.strip("\n"))
            return 0
        if cmd == "prior":
            # The audit-over-audit corroboration feed: the engine's own recent committed digests, read
            # over the GitHub API (same-repo, own-repo token) and printed oldest→newest for the read-only
            # self-review. Degrades in-band (never raises) when there is no history or the read fails.
            return _prior_cli(argv[1:])
        if cmd == "memory":
            # The saved-memory coverage feed (concern #1): the project's own backed-up saved beliefs read
            # over memory's pure backup-read, printed for the read-only self-review. Degrades in-band to a
            # plain disclosure (never raises, never non-zero) when the backup is absent/unreachable/unreadable.
            return _saved_memory_cli(argv[1:])
    except Exception as exc:  # a tool error is loud, never a silent pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"unknown command '{cmd}' (expected: seal, check, staleness, body, prior, memory)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
