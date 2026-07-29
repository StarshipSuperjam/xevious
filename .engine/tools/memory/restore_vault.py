"""restore_vault.py — memory's backup vault, the RESTORE path.

The EXPORT half (`backup_vault.py`) copies the gitignored ledger + a 4-key snapshot manifest to a PRIVATE GitHub
repo — by default the shared `engine-memory-vault`, this project in its own minted-id folder; restore binds
on that same folder id. That made memory durable off-machine — but nothing brought it BACK. This module is
the RESTORE half, which fully closes the memory-loss / portability risk for a non-engineer. The restore contract: replace the
ledger and rebuild the derived index (routed through `migrations` if the record shape changed), guarded by the
ledger-generation stamp so an older
backup landing over newer state is SURFACED, never silently resurrected. The module serves TWO restore modes through
ONE mechanism: `restore_now` reads the rolling backup head (fresh-machine + operator recovery), and
`restore_pre_migration` reads a retained pre-migration snapshot TAG (the migration-revert undo, after a reverted
upgrade leaves the store ahead of the code) — both share the fetch -> format/resurrection guard -> consent -> crash-safe
swap pipeline, differing only in which ref the fetch resolves. Two operator floors live here:
  Floor 3 — the auto-restore-offer: a fresh instance whose local memory is empty but whose committed pointer is
    configured surfaces a plain-language offer at session start (boot relays `detect_restore_offer`, the strand /
    pr_conflict "boot offers, the assistant executes on consent" model). New-laptop recovery never depends on CLI or
    path knowledge — bounded honestly: it needs the project repo present (the committed pointer is what it reads).
  Floor 4 — degrade-and-disclose: every failure names a consequence + ONE recovery action, never a git/HTTP error.

Posture (the backup_vault precedent): pure GitHub API over the same 10s-bounded transport; the restore ACT is a
FOREGROUND, consent-gated command (it OVERWRITES local memory, so it must never run unattended). The detector is
LOCAL-ONLY (no network) so it adds nothing to session-start cost. The swap is crash-safe and serialized behind the
single-writer lock; the canonical ledger is untouched until the atomic rename.

CLI: restore | status | test-read | demo [--live]. Run the demo (fully offline):
    uv run --directory .engine --frozen -- python tools/memory/restore_vault.py demo
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import backup_vault as bv  # noqa: E402 — the shared vault surface (pointer/transport/manifest/helpers)
from memory import ledger              # noqa: E402 — the canonical store + its restore primitive + generation stamp

_UNSET = object()

# A restore temp written in the SAME dir as the ledger (replace_ledger requires a sibling for an atomic rename).
_RESTORE_TMP = "ledger.ndjson.restore-tmp"


# ============================================================================================================
# Fetch — the GET/read side the export tool lacks (Git Data: ref -> commit -> tree -> blob).
# ============================================================================================================

def _fetch_blob(gh, owner: str, repo: str, sha) -> "bytes | None":
    """GET one blob and decode it, VERIFYING the bytes against the tree's object id (git's content-addressing) and
    the API's `size` — so a truncated/corrupt download is rejected before it can ever overwrite memory. None on any
    doubt."""
    if not (isinstance(sha, str) and sha):
        return None
    obj = bv._get(gh, f"/repos/{owner}/{repo}/git/blobs/{sha}")
    if not isinstance(obj, dict) or obj.get("encoding") != "base64" or not isinstance(obj.get("content"), str):
        return None
    try:
        raw = base64.b64decode(obj["content"])          # b64decode tolerates the API's 60-char line wraps
    except Exception:  # noqa: BLE001 — undecodable -> reject (never a partial write)
        return None
    if bv._git_blob_sha1(raw) != sha:                    # Merkle integrity: the bytes ARE what the tree points at
        return None
    size = obj.get("size")
    if isinstance(size, int) and not isinstance(size, bool) and size != len(raw):
        return None
    return raw


def fetch_snapshot(*, transport=None, ref=None) -> dict:
    """Fetch the backed-up ledger bytes + manifest from the configured vault. Pure GitHub API over the bounded
    transport; cheap-probe-first (a repo GET bounds a dead host). Never raises. Returns {ok, error, ledger_bytes,
    manifest, ...}; error in {not-configured, no-token, unreachable, no-backup-data, snapshot-missing,
    namespace-missing, corrupt}.

    `ref` selects which git ref to read: the default `None` reads the ROLLING backup head (`("heads", branch)`),
    and `("tags", <tag_name>)` reads a retained pre-migration SNAPSHOT tag (the migration-revert restore path). The commit -> tree -> blob -> manifest read is IDENTICAL for both — only the resolved ref differs —
    so the one fetch mechanism serves both restore modes (no forked copy)."""
    pointer = bv.read_pointer()
    if pointer is None:
        return {"ok": False, "error": "not-configured"}
    gh = bv._gh(transport)
    if gh is None:
        return {"ok": False, "error": "no-token"}
    owner, repo, branch, namespace = pointer["owner"], pointer["repo"], pointer["branch"], pointer["namespace"]
    ref_kind, ref_name = ref if ref is not None else ("heads", branch)
    try:
        if bv._get(gh, f"/repos/{owner}/{repo}") is None:               # cheap probe: reachability
            return {"ok": False, "error": "unreachable"}
        ref_obj = bv._get(gh, f"/repos/{owner}/{repo}/git/ref/{ref_kind}/{ref_name}")
        base_sha = (ref_obj or {}).get("object", {}).get("sha")
        if not (isinstance(base_sha, str) and base_sha):
            # A HEADS miss is "nothing backed up yet" (no-backup-data). A TAGS miss is a CITED snapshot that is
            # gone — a hand-deletion in the operator's own vault — a DISTINCT plain-language finding the operator
            # must see, never collapsed into the rolling "run a backup first" message.
            return {"ok": False, "error": "snapshot-missing" if ref_kind == "tags" else "no-backup-data"}
        commit = bv._get(gh, f"/repos/{owner}/{repo}/git/commits/{base_sha}")
        tree_sha = (commit or {}).get("tree", {}).get("sha")
        if not (isinstance(tree_sha, str) and tree_sha):
            return {"ok": False, "error": "corrupt"}
        tree = bv._get(gh, f"/repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1")
        if not isinstance(tree, dict) or tree.get("truncated") is True:  # a truncated tree could hide the ledger entry
            return {"ok": False, "error": "corrupt"}
        entries = {e.get("path"): e for e in tree.get("tree", []) if isinstance(e, dict)}
        led_entry = entries.get(f"{namespace}/ledger.ndjson")
        man_entry = entries.get(f"{namespace}/manifest.json")
        if not isinstance(led_entry, dict) or not isinstance(man_entry, dict):
            # A now-MISSING namespace (the vault is populated — it holds OTHER projects' folders — but mine is gone,
            # i.e. my folder was removed by hand) is a DISTINCT finding the operator must see (floor 2), never the
            # silent "no backup yet" no-restore. A truly fresh vault (no other folders) stays `no-backup-data`.
            mine = f"{namespace}/"
            others = any(e.get("type") == "blob" and "/" in (e.get("path") or "")
                         and not (e.get("path") or "").startswith(mine)
                         for e in entries.values() if isinstance(e, dict))
            return {"ok": False, "error": "namespace-missing" if others else "no-backup-data"}
        ledger_bytes = _fetch_blob(gh, owner, repo, led_entry.get("sha"))
        manifest_raw = _fetch_blob(gh, owner, repo, man_entry.get("sha"))
        if ledger_bytes is None or manifest_raw is None:
            return {"ok": False, "error": "corrupt"}
        try:
            manifest = json.loads(manifest_raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "corrupt"}
        if not isinstance(manifest, dict):
            return {"ok": False, "error": "corrupt"}
        return {"ok": True, "error": None, "ledger_bytes": ledger_bytes, "manifest": manifest,
                "owner": owner, "repo": repo, "namespace": namespace}
    except Exception:  # noqa: BLE001 — any transport fault degrades to a clean failure, never a raise
        return {"ok": False, "error": "unreachable"}


# ============================================================================================================
# The engine-run test read — the positive correlate that ends provisioning's saved-memory turn-on (#224).
# ============================================================================================================
# A plain-language fault->fix map (Floor 4): one message per fetch_snapshot code, each naming the exact fault AND
# the one fix, NEVER a git/HTTP error. POINTER_REL is named so a missing-pointer fault points at the exact file.
_TEST_READ_MESSAGES = {
    "ok": "Success — the scheduled review can reach your saved memory from its backup.",
    "not-configured": ("The backup location isn't recorded in this project yet. Set up the backup first, then "
                       f"commit {bv.POINTER_REL} so a scheduled run can find it."),
    "no-token": ("No access token reached this test. Paste your read-only backup token and try again — and for the "
                 "scheduled review, set it as the MEMORY_VAULT_TOKEN secret."),
    "unreachable": ("I reached for your backup but couldn't open it. The token is most likely scoped to the wrong "
                    "repository or has the wrong permission — re-issue it as a read-only (contents read) token on "
                    "the backup repository only."),
    "no-backup-data": ("I opened your backup repository, but nothing has been backed up to it yet. Run a backup "
                       "first, then try this again."),
    "namespace-missing": ("I opened your backup, but this project's saved memory is no longer in it (its folder is "
                          "gone). Check the backup repository, then try again."),
    "corrupt": ("I opened your backup, but couldn't read a usable copy of your saved memory from it. Try again "
                "later; if it persists, the backup may need re-pushing."),
}


def test_read(*, transport=None) -> dict:
    """The engine-run test read that ENDS provisioning's saved-memory turn-on — a one-shot read of the vault with
    the credential just set, exercising BOTH the committed pointer (read by `fetch_snapshot` via `bv.read_pointer`)
    AND the access token (via `bv._gh` -> `boot.gh_token`), so a missing-pointer fault is caught HERE, not on the
    next scheduled run. A pure consumer read that changes nothing and NEVER raises. Returns {ok, error,
    message}: `message` is plain operator language naming the exact fault and the one fix (Floor 4), never a
    git/HTTP error. It proves the token's SCOPE / REPO / PERMISSION (the dominant failure modes); the one residual
    it cannot prove — that the secret is set under the right NAME in CI — only the first scheduled run exercises,
    which the turn-on copy discloses honestly."""
    snap = fetch_snapshot(transport=transport)
    code = "ok" if snap.get("ok") else snap.get("error")
    message = _TEST_READ_MESSAGES.get(code or "", _TEST_READ_MESSAGES["corrupt"])
    return {"ok": bool(snap.get("ok")), "error": snap.get("error"), "message": message}


# ============================================================================================================
# The audit's pure read-projection — the durable saved beliefs, for the self-review's concern #1.
# ============================================================================================================
# The scheduled self-audit reviews "stale saved-memory beliefs" but the ledger is gitignored, absent from any
# committed-files-only run. It reaches the memory by reading THIS backup — a pure read that changes nothing.
# Memory OWNS this projection (the audit is a downstream consumer that may not widen the mechanism): the
# durable-belief SELECTION is single-sourced here against memory's own record vocabulary (`records`) and its own
# recall authority (`forget.live_records` — the ONE place that retires orphaned / superseded records, and the
# bookkeeping markers), so a future memory change to what counts as "live" updates one place and the audit's
# view follows. The audit
# side (audit_digest) owns only the audit-context rendering + the disclosure wording, never these semantics.

def read_saved_memory(*, transport=None) -> dict:
    """Fetch the backed-up memory and project it to the durable SAVED BELIEFS the self-audit reviews — a pure
    read that changes nothing. Returns {ok, error, beliefs, as_of}: on success `beliefs` is a list of plain
    projections (newest-first) `{text, kind, role, recorded_ts, last_access_ts}` covering ONLY the live durable
    beliefs (episodic summaries + gists; markers, orphans and superseded raws are dropped by memory's own
    `forget.live_records`), and `as_of` is the backup timestamp (ISO, or None). Nothing is dropped for AGE — the
    projection is the whole live durable set, however old, which is what the audit's staleness review wants. On any failure
    `beliefs`/`as_of` are None and `error` is `fetch_snapshot`'s code ({not-configured, no-token, unreachable,
    no-backup-data, namespace-missing, corrupt}) so the caller discloses the gap honestly. Never raises."""
    snap = fetch_snapshot(transport=transport)
    if not snap.get("ok"):
        return {"ok": False, "error": snap.get("error"), "beliefs": None, "as_of": None}
    as_of = (snap.get("manifest") or {}).get("timestamp")
    try:
        beliefs = _project_beliefs(snap["ledger_bytes"])
    except Exception:  # noqa: BLE001 — a decode/projection fault degrades to a clean "couldn't read", never a raise
        return {"ok": False, "error": "corrupt", "beliefs": None, "as_of": None}
    return {"ok": True, "error": None, "beliefs": beliefs, "as_of": as_of if isinstance(as_of, str) else None}


def _project_beliefs(ledger_bytes: bytes) -> list:
    """Decode the backed-up ledger bytes (a temp sibling, never the live ledger) and return the durable saved
    content newest-first, for the scheduled self-review to judge for staleness.

    WHAT COUNTS AS DURABLE CHANGED, and leaving it alone would have made the review lie. It kept only the two
    AI-written summary kinds, which was right while a background pass wrote them. Nothing writes either any
    more, and a repository the engine deploys into starts empty — so this would have returned nothing forever,
    and the digest would have told an operator whose store was full of their own conversation that it "holds no
    saved decisions or notes yet to review". So it keeps what a store actually holds now: the operator's pins
    first — the one kind that IS a durable standing statement and the one most worth re-reading for staleness —
    then the older summaries, which real stores still carry.

    NOT the raw conversation, deliberately. A review asks "is this belief still true?", and a turn in a
    conversation is not a belief; it is a record of a moment, true forever by construction. Including it would
    also push tens of thousands of records at a digest that truncates oldest-first, burying the few records the
    review is actually for.

    `forget.live_records` is memory's own recall authority — it drops the provenance markers, the crashed-pass
    orphans and the gist-superseded raws — so we reimplement none of that. It takes no clock: membership
    stopped depending on one when the archived-tier age-out was removed."""
    import tempfile
    from memory import forget                    # lazy: forget pulls score/index — keep it off the module-load path
    from memory import records as rec
    fd, tmp = tempfile.mkstemp(suffix=".ndjson")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(ledger_bytes)
        live = list(forget.live_records(path=tmp))
    finally:
        _quiet_remove(tmp)
    keep = (rec.PIN_KIND, rec.EPISODIC_KIND, rec.GIST_KIND)
    durable = []
    for r in live:
        if r.get("kind") not in keep:
            continue
        text = r.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        recorded = r.get("ts") if isinstance(r.get("ts"), int) else None
        durable.append({"text": text, "kind": r.get("kind"), "role": r.get("role"),
                        "recorded_ts": recorded, "last_access_ts": None})
    # Newest first. It used to sort by last-use, falling back to when the record was written; nothing tracks
    # use any more, so the fallback is now the whole rule and the field is carried as None rather than read.
    durable.sort(key=lambda b: b["recorded_ts"] or 0, reverse=True)
    return durable


# ============================================================================================================
# Local state reads (for the resurrection guard + the consent count + the offer).
# ============================================================================================================

def _local_structurally_empty() -> bool:
    """True iff the local ledger file is missing or zero bytes — the fresh-machine case. Deliberately NOT '0
    parseable records': a corrupt non-empty ledger must never read as empty (else the offer would invite overwriting
    it)."""
    try:
        return os.path.getsize(ledger.ledger_path()) == 0
    except OSError:
        return True


def _local_record_count() -> int:
    try:
        return len(ledger.read().records)
    except Exception:  # noqa: BLE001
        return 0


def _generation_known() -> bool:
    """Whether the local generation is KNOWN — the meta sidecar exists and holds a valid int. A non-empty local
    ledger whose sidecar is missing/unreadable reads as generation 0, which could MASK a real higher generation
    (and so a real resurrection); the restore guard treats that 'unknown' as a possible resurrection."""
    try:
        with open(ledger.meta_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return False
    val = data.get("generation") if isinstance(data, dict) else None
    return isinstance(val, int) and not isinstance(val, bool) and val >= 0


def _count_lines(raw: bytes) -> int:
    return raw.count(b"\n") if raw else 0


# ============================================================================================================
# Resurrection-surfacing — a restore that would land an OLDER generation is surfaced, never silently applied.
# (Mirrors module_manager.surface_stamp_mismatch's body for generations; routes through boot's open-findings.)
# ============================================================================================================

def surface_resurrection(local_gen: int, backup_gen, *, now: "int | None" = None, github=_UNSET) -> "int | None":
    """Surface a declined resurrecting restore as ONE tracked engine finding via telemetry.promote_finding (which
    boot renders through its read-only open-findings path — no boot change). Content-free: names only the condition
    + the one recovery action, never ledger content / repo / git error. Returns the Issue number, or None offline
    (the in-session disclose + the decline still stand). `github` is injectable for tests/demo (None => offline)."""
    f = _resurrection_finding()
    import close       # noqa: E402 — lazy: this rare path keeps the common import graph lean
    import telemetry   # noqa: E402
    gh = (close._github() if github is _UNSET else github)
    if gh is None:                                       # offline -> surfaced-in-session-not-tracked; consent is the wall
        return None
    now_iso = bv._iso_utc(int(time.time()) if now is None else now)
    digest = hashlib.sha1(b"memory/restore-resurrection").hexdigest()[:12]
    record = {"source_id": f"memory/restore-resurrection/{digest}", "severity": telemetry.TRUST_CRITICAL,
              "message": f.get("message"), "location": f.get("location"),
              "first_seen": now_iso, "last_seen": now_iso}
    try:
        return telemetry.promote_finding(gh, record, now_iso)
    except Exception:  # noqa: BLE001 — surfacing must never raise into the restore path
        return None


def _resurrection_finding() -> dict:
    import validate  # noqa: E402 — lazy: the finding-record shape
    return validate.finding(
        "hard",
        "The engine declined a memory restore: the backup is older than the memory on this computer, so restoring "
        "it would bring back notes that were deliberately removed since the backup was taken. It was surfaced here "
        "rather than applied. If you intend to recover the older copy, run the restore again and confirm explicitly.",
        location="memory backup/restore")


# ============================================================================================================
# Floor-4 wording — plain language, non-engineer; consequence + ONE recovery action, never a git/HTTP error.
# ============================================================================================================

_MSG_NOT_CONFIGURED = ("No backup is set up yet for this project, so there's nothing to restore. Ask me to set one "
                       "up to keep an off-site copy of this project's AI memory.")
_MSG_UNREACHABLE = ("I couldn't reach your backup just now, so I didn't restore anything. Your memory on this "
                    "computer is unchanged. Check your internet connection and ask me to try the restore again.")
_MSG_NO_BACKUP_DATA = ("The backup is set up, but I couldn't find a saved memory in it to restore yet (it may not "
                       "have finished its first backup). Your memory on this computer is unchanged.")
_MSG_NAMESPACE_MISSING = ("Your project's saved-memory folder is no longer in the backup — it looks like it was "
                          "removed from the backup by hand. Nothing on this computer changed. If your memory is still "
                          "here, ask me to set up the backup again and I'll rebuild it from this computer. If this "
                          "computer is empty too and the memory isn't saved on another machine, that backed-up copy "
                          "is gone for good.")
# The migration-revert distinct miss: the CITED pre-update snapshot is gone. The message names
# the CONSEQUENCE + one honest recovery action, not the cause — so this does NOT assert "removed by hand" (it could
# also be the engine's own retention prune, the open reversibility-unit question, #303). The recovery action is
# DELIBERATELY NOT _MSG_NAMESPACE_MISSING's "set up the backup again" — that would re-push the RESHAPED store and
# destroy the right copy's addressability. The honest action is re-run the update / ask for help, never a silent
# no-restore.
_MSG_SNAPSHOT_MISSING = ("The saved copy of your memory from before the last update isn't in your backup anymore. "
                         "Nothing on this computer changed. The one-step undo isn't available, so I can re-run the "
                         "update to get things working again, or you can ask me for help.")
_MSG_CORRUPT = ("I couldn't read a complete copy of your memory from the backup, so I did NOT change anything on "
                "this computer — better to keep what you have than risk a half copy. Try the restore again in a "
                "little while.")
_MSG_VERSION_MISMATCH = ("This backup was made by a different version of the engine, and this version has no safe "
                         "way to bring its saved notes forward, so I left your memory on this computer unchanged. "
                         "If you need this older backup, ask me for help.")
_MSG_BAD_MANIFEST = ("I couldn't make sense of this backup's details, so I left your memory on this computer "
                     "unchanged rather than risk restoring something wrong.")
_MSG_RESURRECTION = ("Your memory on this computer is MORE RECENT than this backup. Restoring it would undo edits "
                     "and removals you've made since the backup was taken — so I did NOT restore it. If you truly "
                     "want the older copy, tell me explicitly and I'll restore it.")
_MSG_BUSY = ("Memory is busy right now, so I didn't restore anything — nothing was changed. Ask me to try the "
             "restore again in a moment.")
_MSG_APPLY_FAILED = ("Something went wrong part-way through restoring, so I stopped. Your memory on this computer is "
                     "unchanged. Ask me to try the restore again.")
_MSG_DECLINED = "No restore was done. Your memory on this computer is unchanged."


def _floor4_fetch(error: "str | None") -> str:
    # `snapshot-missing` arises only on the tags (migration-revert) fetch path; mapping it here means the shared
    # restore core surfaces the distinct snapshot-missing message rather than the generic unreachable default.
    return {"not-configured": _MSG_NOT_CONFIGURED, "no-token": _MSG_UNREACHABLE, "unreachable": _MSG_UNREACHABLE,
            "no-backup-data": _MSG_NO_BACKUP_DATA, "snapshot-missing": _MSG_SNAPSHOT_MISSING,
            "namespace-missing": _MSG_NAMESPACE_MISSING, "corrupt": _MSG_CORRUPT}.get(error or "", _MSG_UNREACHABLE)


def _restore_consent_prompt(local_count: int, backup_count: int) -> str:
    """Floor 1 ethos — the single most dangerous string: restore OVERWRITES local memory. Name the loss flatly."""
    return (f"This will replace the notes saved on this computer ({local_count}) with the backup copy "
            f"({backup_count}). The current notes will be gone. Continue? [y/N]: ")


def _restored_msg(count: int) -> str:
    return (f"Restored your project's AI memory from the backup — {count} note(s) are back on this computer, ready "
            "to use.")


# ============================================================================================================
# The restore act (foreground, consent-gated, crash-safe + serialized).
# ============================================================================================================

def _ask_restore_consent(local_count: int, backup_count: int) -> str:
    try:
        return input(_restore_consent_prompt(local_count, backup_count))
    except EOFError:
        return "n"


def restore_now(*, transport=None, consent: "str | None" = None, override: bool = False,
                now: "int | None" = None, github=_UNSET) -> dict:
    """Restore the local ledger + index from the ROLLING backup head. Fetch -> format guard -> resurrection guard ->
    consent -> apply (under the writer lock). OVERWRITES local memory, so it is foreground + consent-gated. Fail-SAFE:
    the canonical ledger is untouched until the atomic rename, and every failure is a plain Floor-4 message, never a
    raise. `consent` ('y'/'n') bypasses the prompt for tests/demo; `override` proceeds past the resurrection guard;
    `github` is forwarded to surfacing (None => offline). Result: {ok, error, restored, message}."""
    fetch = fetch_snapshot(transport=transport)
    return _restore_from_fetch(fetch, consent=consent, override=override, now=now, github=github)


def restore_pre_migration(*, tag: str, transport=None, consent: "str | None" = None, override: bool = False,
                          now: "int | None" = None, github=_UNSET) -> dict:
    """Restore the local ledger from a retained PRE-MIGRATION snapshot TAG — the migration-revert recovery:
    after an engine upgrade pull request is reverted, engine code goes back but a `data` migration that already
    reshaped the gitignored store is not, so the store is ahead of the code; this restores the true pre-migration
    memory the named snapshot tag holds. SAME fail-safe, consent-gated, crash-safe pipeline as `restore_now` — only
    the ref read differs (`("tags", tag)`), so the one restore mechanism serves both modes. The snapshot manifest
    carries the PRE-migration ledger-generation, so the resurrection guard still fires (only) if an erasure-compaction
    ran in the revert window — no special generation handling. A cited tag that is GONE (operator
    hand-deletion) degrades to the distinct `_MSG_SNAPSHOT_MISSING`, never a silent no-restore.

    Memory owns this mechanism + restore contract; the caller (the detector / boot offer) supplies the
    tag — the operator never reads or types a `refs/…` string. Result: {ok, error, restored,
    message}."""
    if not (isinstance(tag, str) and tag.strip()):
        return {"ok": False, "error": "snapshot-missing", "restored": False, "message": _MSG_SNAPSHOT_MISSING}
    fetch = fetch_snapshot(transport=transport, ref=("tags", tag))
    result = _restore_from_fetch(fetch, consent=consent, override=override, now=now, github=github)
    if result.get("ok"):
        # The store now holds the pre-update copy, so it is no longer ahead of the code — clear the reversibility
        # stamp so the code-older-than-data offer self-clears. Scoped to the migration-revert mode ONLY: a rolling
        # `restore_now` must not clear a migration stamp (it restores the live head, not a pre-update state).
        bv.clear_migration_stamp()
    return result


def _restore_from_fetch(fetch: dict, *, consent: "str | None" = None, override: bool = False,
                        now: "int | None" = None, github=_UNSET) -> dict:
    """The shared post-fetch restore pipeline BOTH restore modes run: format guard -> resurrection guard -> consent
    -> apply (under the writer lock). Takes an already-fetched `{ok, manifest, ledger_bytes, ...}` (from a rolling
    head OR a snapshot tag — the only difference is upstream, in which ref the fetch read). A failed fetch degrades
    to its plain Floor-4 message via `_floor4_fetch` (which maps the tag path's distinct `snapshot-missing`). The
    canonical ledger is untouched until the atomic rename; every failure is a plain message, never a raise."""
    if not fetch.get("ok"):
        return {"ok": False, "error": fetch.get("error"), "restored": False, "message": _floor4_fetch(fetch.get("error"))}
    when = int(time.time()) if now is None else int(now)
    manifest, ledger_bytes = fetch["manifest"], fetch["ledger_bytes"]

    backup_version = manifest.get("ledger-version")
    # Proceed only on a genuine exact match with the current record shape (an int, never a bool coerced to 1). Any
    # other value — an older or newer shape, or a malformed one — routes through the migrations home, which declines
    # by default, so a malformed version can never be mistaken for the current one. When a real record-shape change
    # ships, the migrations home carries an old backup forward here, IN MEMORY, before anything on this computer is
    # touched; if it cannot be carried forward, the restore declines and leaves local memory alone — never a half copy.
    if not (isinstance(backup_version, int) and not isinstance(backup_version, bool)
            and backup_version == ledger.LEDGER_FORMAT_VERSION):
        from memory import ledger_migrations
        chain = ledger_migrations.resolve_ledger_migration(backup_version, ledger.LEDGER_FORMAT_VERSION)
        if chain is None:
            return {"ok": False, "error": "version-mismatch", "restored": False, "message": _MSG_VERSION_MISMATCH}
        try:
            ledger_bytes = ledger_migrations.apply_ledger_migrations(ledger_bytes, chain)
        except Exception as exc:  # noqa: BLE001 — a registered step that can't complete is a bug in that step, not a
            # "no path" case; record it distinctly and note it so the two are tellable apart the day a real step
            # exists, then decline safely — the memory on this computer is never touched.
            print(f"restore: a saved-memory format step failed and was not applied ({exc}); leaving memory unchanged",
                  file=sys.stderr)
            return {"ok": False, "error": "migration-failed", "restored": False, "message": _MSG_VERSION_MISMATCH}
    backup_gen = manifest.get("ledger-generation")
    if not (isinstance(backup_gen, int) and not isinstance(backup_gen, bool) and backup_gen >= 0):
        return {"ok": False, "error": "bad-manifest", "restored": False, "message": _MSG_BAD_MANIFEST}

    # Resurrection guard — only relevant when the local ledger is NOT structurally empty (the fresh-machine case
    # has nothing to lose). A non-empty local ledger is protected when the backup is provably older OR its
    # generation is UNKNOWN (sidecar missing -> local_gen reads 0, which could mask a real higher generation).
    if not _local_structurally_empty():
        local_gen = ledger.generation()
        if (not _generation_known()) or (backup_gen < local_gen):
            if not override:
                surface_resurrection(local_gen, backup_gen, now=when, github=github)
                return {"ok": False, "error": "resurrection", "restored": False, "message": _MSG_RESURRECTION}

    local_count = _local_record_count()
    backup_count = _count_lines(ledger_bytes)
    answer = consent if consent is not None else _ask_restore_consent(local_count, backup_count)
    if str(answer).strip().lower() not in ("y", "yes"):
        return {"ok": False, "declined": True, "restored": False, "message": _MSG_DECLINED}

    return _apply_restore(ledger_bytes, backup_gen, backup_count)


def _apply_restore(ledger_bytes: bytes, backup_gen: int, backup_count: int) -> dict:
    """The crash-safe, serialized swap. Under the single-writer lock: write a validated sibling temp -> remove the
    existing index (so once it is gone, any concurrent query scans the live ledger and cannot trust a stale index
    over the swapped one, for any generation relationship) -> atomic replace -> stamp the backup's generation ->
    rebuild the index."""
    from memory import capture, index   # noqa: E402 — lazy: keep capture/index off the module-load path
    data_dir = ledger.ledger_dir()
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError:
        pass
    lock_fd = capture._acquire_lock(os.path.join(data_dir, capture.LOCK_FILENAME))
    if lock_fd is None:                                  # a live capture / compaction holds it — restore can't retry
        return {"ok": False, "error": "busy", "restored": False, "message": _MSG_BUSY}
    tmp = os.path.join(data_dir, _RESTORE_TMP)
    try:
        with open(tmp, "wb") as fh:
            fh.write(ledger_bytes)
        chk = ledger.read(path=tmp)                      # completeness: a complete, parseable ledger only
        if chk.torn_trailing or chk.malformed or (ledger_bytes and not chk.records):
            _quiet_remove(tmp)
            return {"ok": False, "error": "corrupt", "restored": False, "message": _MSG_CORRUPT}
        _quiet_remove(index.index_path())                # drop the stale index for the swap window
        ledger.replace_ledger(tmp, path=ledger.ledger_path())   # fsync temp -> atomic rename -> fsync dir
        ledger.set_generation(backup_gen)                # the restored content carries the backup's TRUE generation
        index.rebuild()                                  # rebuild from the restored ledger, re-stamp the generation
        return {"ok": True, "error": None, "restored": True, "message": _restored_msg(backup_count)}
    except Exception:  # noqa: BLE001 — any fault leaves the canonical ledger as it was before the rename
        _quiet_remove(tmp)
        return {"ok": False, "error": "apply-failed", "restored": False, "message": _MSG_APPLY_FAILED}
    finally:
        capture._release_lock(lock_fd)


def _quiet_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


# ============================================================================================================
# Floor 3 — the local-only auto-restore-offer detector (boot relays it; boot owns the wording).
# ============================================================================================================

def detect_restore_offer() -> "dict | None":
    """LOCAL-ONLY (no network): an offer signal iff a backup is configured (the committed pointer) AND the local
    memory is structurally empty — the new-laptop recovery case. None otherwise (a configured-but-populated machine,
    or no backup). The fetch happens only when the operator accepts; boot renders the plain-language offer."""
    try:
        if not bv._setup_done() or not _local_structurally_empty():
            return None
        return {"configured": True}
    except Exception:  # noqa: BLE001 — a detector fault degrades to no-offer, never breaks the boot pack
        return None


# ============================================================================================================
# The code-older-than-data detector (#303): is the local store AHEAD of the engine code?
# OFFLINE detection (the migration stamp records what no other local file does — the migrated version);
# boot relays the one-action restore offer, and when online the durable tracked Issue is promoted too.
# ============================================================================================================

_MIGRATION_REVERT_HANDLE = "ask me to restore the copy saved before the last update"


def detect_migration_revert(*, github=_UNSET, now: "int | None" = None) -> "dict | None":
    """OFFLINE, READ-ONLY: after a reverted/half-applied engine update, is the gitignored memory store
    AHEAD of the running engine code? The version that migrated the store is recorded nowhere else locally
    (engine.json reverts WITH the code), so this reads memory's own migration stamp (`bv.read_migration_stamp`,
    written at the upgrade's batch floor) and compares its `migrated_by_version` against the running engine version
    (`bv._engine_version`). On `running < migrated_by_version` it returns an offer dict
    `{store_label, stamped, running, tag}` — the `tag` is OPAQUE executor payload (boot renders the plain handle, never
    the tag); else None. ALSO promotes the ONE durable tracked Issue via `module_manager.surface_stamp_mismatch` when
    `github` resolves (boot.open_findings renders it); offline (github=None) the in-session offer still stands. Any
    fault → None (no false 'behind'); a non-version-shaped running version → None (it would compare as (0,))."""
    try:
        stamp = bv.read_migration_stamp()
        if stamp is None:
            return None
        stamped, running = stamp.get("migrated_by_version"), bv._engine_version()
        if not bv._is_version_shaped(running):                # unreadable engine.json → "unknown" → (0,); never false-fire
            return None
        import validate  # noqa: E402 — lazy: the shared version comparator (same one stamp_mismatch_finding uses)
        if validate._ver_tuple(running) >= validate._ver_tuple(stamped):
            return None                                       # code is at/ahead of the data — no mismatch
        offer = {"store_label": stamp.get("store_label"), "stamped": stamped, "running": running,
                 "tag": stamp.get("snapshot_tag")}
        # Durable tracked Issue (the live caller that retires the orphaned primitive). Lazy import: a top-level
        # restore_vault -> module_manager -> bootstrap -> boot edge would close an import cycle (restore_vault ->
        # backup_vault -> boot is already a lazy back-edge). Best-effort + quiet: surfacing never breaks detection.
        try:
            import module_manager  # noqa: E402 — function-local to avoid the import cycle named above
            now_iso = bv._iso_utc(int(time.time()) if now is None else now)
            module_manager.surface_stamp_mismatch(stamp.get("store_label") or "your saved memory", stamped, running,
                                                  _MIGRATION_REVERT_HANDLE, now_iso, github=github)
        except Exception:  # noqa: BLE001 — the durable-Issue path is additive; its failure never suppresses the offer
            pass
        return offer
    except Exception:  # noqa: BLE001 — a detector fault degrades to no-offer, never breaks the boot pack
        return None


# ============================================================================================================
# CLI verbs.
# ============================================================================================================

def status(*, now: "int | None" = None) -> int:
    """Read-only, plain voice (never 'generation'/'ledger'/'index'): is a backup set up, and is local memory empty
    (restore-eligible) or populated."""
    pointer = bv.read_pointer()
    if pointer is None:
        print(_MSG_NOT_CONFIGURED)
        return 0
    where = f"{pointer['owner']}/{pointer['repo']}"
    print(f"A backup is set up for this project (your private repository \"{where}\").")
    if _local_structurally_empty():
        print("Your memory on this computer is empty — say \"restore my memory\" and I'll try to bring it back from "
              "the backup.")
    else:
        print(f"Your memory on this computer has {_local_record_count()} note(s); restoring would replace them with "
              "the backup copy.")
    return 0


def _restore_cli() -> int:
    print(restore_now()["message"])
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "restore":
        return _restore_cli()
    if cmd == "status":
        return status()
    if cmd == "test-read":
        print(test_read()["message"])
        return 0                                          # a diagnostic never fails red — it REPORTS the fault
    if cmd == "demo":
        return _demo_live() if "--live" in argv[1:] else _demo()
    print(f"usage: restore_vault.py [restore|status|test-read|demo [--live]]\nunknown command {cmd!r}",
          file=sys.stderr)
    return 2


# ============================================================================================================
# Operator demonstration — REAL restore logic against the in-memory _FakeVault; only the network is stubbed.
# ============================================================================================================
# A fully offline round-trip on a throwaway memory cabinet + repo root (the real ledger and pointer are never
# touched): back up some memory, WIPE the local copy, RESTORE it, and prove it came back identical and searchable;
# then prove the resurrection guard refuses an older backup, the consent text on a populated machine, and the
# Floor-4 degrade on an unreachable backup. Vary it: change the notes, the generations, force the fetch to fail.

def _demo() -> int:
    import tempfile
    print("=" * 96)
    print("MEMORY — the engine restores your AI memory from its private backup (practice run)")
    print("=" * 96)
    with tempfile.TemporaryDirectory() as cabinet, tempfile.TemporaryDirectory() as root:
        import validate
        old_root = validate.ROOT
        os.environ["ENGINE_MEMORY_DIR"] = cabinet
        validate.ROOT = root
        os.makedirs(os.path.join(root, ".engine"), exist_ok=True)
        with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"engine_release": "0.0.0-dev"}, fh)
        try:
            ok = _demo_body()
        finally:
            validate.ROOT = old_root
            os.environ.pop("ENGINE_MEMORY_DIR", None)
    print("\n" + "-" * 96)
    print("What this just proved: after a backup, the engine can WIPE the local memory and bring it back identical")
    print("and searchable — so a dead disk or a new laptop no longer loses 'how did I get here'. It will NOT silently")
    print("overwrite newer memory with an older backup (it surfaces that and refuses), it tells you plainly what will")
    print("be replaced before it does anything, and a failed fetch changes nothing. It also proves the mechanism that")
    print("undoes a bad engine update — bringing back the exact memory saved before it — which the engine will offer")
    print("on its own when it later detects your memory is ahead of your code. That was a PRACTICE run, thrown")
    print("away. To prove it end-to-end on your REAL GitHub — a throwaway private repo created, backed up to, restored")
    print("from, and deleted — run this command with --live.")
    return 0 if ok else 1


def _demo_body() -> bool:
    from memory import index

    def query_hits(text: str) -> int:
        return len(index.query(text).records)

    # --- PART 1 — back up, WIPE, then RESTORE: the memory comes back identical and searchable -----------------
    print("\nPART 1 — back up your memory, lose it, and restore it (the round trip)")
    print("-" * 96)
    bv._demo_plant("Decided the launch banner ships in the spring release.")
    bv._demo_plant("Lesson: never deploy on a Friday — RUMBLEDETHUMPS.")
    fake = bv._FakeVault()
    bv.setup(transport=fake.transport, consent="y")                 # creates the vault + pushes this memory
    original = _read_bytes(ledger.ledger_path())
    before_hits = query_hits("rumbledethumps")
    os.remove(ledger.ledger_path())                                 # simulate disk loss
    _quiet_remove(ledger.meta_path())
    lost = _local_structurally_empty()
    restored = restore_now(transport=fake.transport, consent="y", github=None)
    came_back = _read_bytes(ledger.ledger_path())
    after_hits = query_hits("rumbledethumps")
    print(f"  backed up 2 notes, then wiped the local copy (empty now: {lost})")
    print(f"  restore says: {restored['message']}")
    print(f"  the restored memory is byte-identical to the original: {came_back == original}")
    print(f"  and it is searchable again (a known note found before={before_hits}, after={after_hits})")
    part1 = (restored.get("ok") is True and restored.get("restored") is True and came_back == original
             and lost is True and before_hits == 1 and after_hits == 1)
    print(f"  => {'memory survived a total local loss.' if part1 else '!!! the round trip failed'}")

    # --- PART 2 — the resurrection guard: an OLDER backup is surfaced, not silently applied -------------------
    print("\nPART 2 — it refuses to silently bring back notes you've removed since (resurrection guard)")
    print("-" * 96)
    ledger.set_generation(9)                                        # pretend this machine has compacted/erased since
    guarded = restore_now(transport=fake.transport, consent="y", github=None)   # backup generation is 0 < 9
    still_there = _read_bytes(ledger.ledger_path()) == came_back
    print(f"  the engine's plain-language message: \"{guarded['message']}\"")
    part2 = guarded.get("ok") is False and guarded.get("error") == "resurrection" and still_there
    print(f"  => {'an older backup is surfaced and refused — your memory is untouched.' if part2 else '!!! the guard failed'}")

    # --- PART 3 — the same restore, but the operator EXPLICITLY overrides ------------------------------------
    print("\nPART 3 — if you truly want the older copy, an explicit override restores it")
    print("-" * 96)
    forced = restore_now(transport=fake.transport, consent="y", override=True, github=None)
    part3 = forced.get("ok") is True and forced.get("restored") is True
    print(f"  with an explicit override: {forced['message']}")
    print(f"  => {'the operator stays in control.' if part3 else '!!! the override failed'}")

    # --- PART 4 — the auto-restore offer fires only on an EMPTY machine with a backup -------------------------
    print("\nPART 4 — on a fresh machine the engine OFFERS to restore (Floor 3), and stays quiet otherwise")
    print("-" * 96)
    populated_offer = detect_restore_offer()                       # memory is present now -> no offer
    os.remove(ledger.ledger_path()); _quiet_remove(ledger.meta_path())
    empty_offer = detect_restore_offer()                           # empty + configured pointer -> offer
    print(f"  with memory present: {'offers' if populated_offer else 'stays quiet'}")
    print(f"  with memory empty + a backup configured: {'offers to restore' if empty_offer else 'stays quiet'}")
    part4 = populated_offer is None and bool(empty_offer)
    print(f"  => {'the offer appears exactly when recovery is wanted.' if part4 else '!!! the offer mis-fired'}")

    # --- PART 5 — the consent text shown before any overwrite (a populated machine) ---------------------------
    print("\nPART 5 — the exact words you see before anything is replaced (a populated machine)")
    print("-" * 96)
    for line in _restore_consent_prompt(42, 40).rstrip().splitlines():
        print(f"    | {line}")
    part5 = "will be gone" in _restore_consent_prompt(42, 40)
    print(f"  => {'the overwrite is named plainly, before it happens.' if part5 else '!!! the consent text is unclear'}")

    # --- PART 6 — degrade-and-disclose: an unreachable backup changes nothing (Floor 4) ----------------------
    print("\nPART 6 — if the backup can't be reached, nothing on this computer changes (Floor 4)")
    print("-" * 96)
    bv._demo_plant("A note that must survive a failed restore.")
    guard_bytes = _read_bytes(ledger.ledger_path())
    def _dead_transport(method, path, body=None):
        return None, None
    failed = restore_now(transport=_dead_transport, consent="y", github=None)
    unchanged = _read_bytes(ledger.ledger_path()) == guard_bytes
    print(f"  the engine's plain-language message: \"{failed['message']}\"")
    part6 = failed.get("ok") is False and unchanged and "http" not in failed["message"].lower()
    print(f"  => {'a failure names a consequence and one action, and changes nothing.' if part6 else '!!! a failed fetch was mishandled'}")

    # --- PART 7 — migration-revert: restore the PRE-update memory from its retained snapshot tag -------
    print("\nPART 7 — undo a bad engine update: restore the copy saved before the last update (migration-revert)")
    print("-" * 96)
    # A clean pre-update state, snapshotted as a RETAINED TAG before a data migration reshapes the live store.
    os.remove(ledger.ledger_path()); _quiet_remove(ledger.meta_path())
    bv._demo_plant("Pre-update note: the quarterly plan is locked — PLUMBUS.")
    ledger.set_generation(3)                                        # the pre-migration generation the snapshot carries
    pre_update = _read_bytes(ledger.ledger_path())
    snap = bv.snapshot_for_migration("recall-ledger", "9.9.9", migration_id="demo-mod@1.0.0", transport=fake.transport)
    tag = (snap or {}).get("tag")
    # The migration reshapes the live store, and a routine rolling backup runs over it — but the retained tag is a
    # DISTINCT ref the rolling backup never touches, so the true pre-update copy stays addressable.
    bv._demo_plant("Post-update row the new schema added — GRUMBO.")
    bv.push_now(transport=fake.transport)                          # the routine rolling backup over the reshaped store
    reshaped_hit = query_hits("grumbo")
    reverted = restore_pre_migration(tag=tag, transport=fake.transport, consent="y", github=None)
    back = _read_bytes(ledger.ledger_path())
    print(f"  saved a pre-update snapshot, then the update reshaped memory (its new note present: {bool(reshaped_hit)})")
    print(f"  restore says: {reverted['message']}")
    print(f"  the pre-update memory is back, byte-identical: {back == pre_update}; the update's note is gone: "
          f"{query_hits('grumbo') == 0}")
    part7a = (bool(tag) and reverted.get("ok") is True and reverted.get("restored") is True and back == pre_update
              and reshaped_hit == 1 and query_hits("grumbo") == 0 and query_hits("plumbus") == 1)

    # A cited snapshot that is GONE (hand-deleted in the operator's own vault) -> the DISTINCT snapshot-missing message, never
    # a silent no-restore, and distinct from the rolling "nothing backed up yet" / "couldn't reach it" messages.
    guard_bytes = _read_bytes(ledger.ledger_path())
    missing = restore_pre_migration(tag="engine-snapshot/recall/does-not-exist", transport=fake.transport,
                                    consent="y", github=None)
    part7b = (missing.get("error") == "snapshot-missing" and missing["message"] == _MSG_SNAPSHOT_MISSING
              and missing["message"] not in (_MSG_NO_BACKUP_DATA, _MSG_UNREACHABLE)
              and _read_bytes(ledger.ledger_path()) == guard_bytes)
    print(f"  a hand-deleted snapshot is disclosed plainly (not silently skipped): {part7b}")

    # An erasure-compaction since the snapshot -> restoring the older-generation snapshot is SURFACED, not silently
    # applied (the retained tag participates in the generation-resurrection check).
    ledger.set_generation(12)                                      # an erasure bumped the local generation since
    resurrect = restore_pre_migration(tag=tag, transport=fake.transport, consent="y", github=None)
    part7c = resurrect.get("error") == "resurrection" and _read_bytes(ledger.ledger_path()) == guard_bytes
    print(f"  if an erasure ran since the snapshot, the revert is surfaced, not silently applied: {part7c}")
    part7 = part7a and part7b and part7c
    print(f"  => {'a reverted update can be undone to the true pre-update memory.' if part7 else '!!! the migration-revert restore failed'}")

    # --- PART 8 — the engine DETECTS the store is ahead of the code and OFFERS the whole-update undo (#303) -
    print("\nPART 8 — the engine notices your memory is ahead of your code after a reverted update, and offers the undo")
    print("-" * 96)
    import boot       # noqa: E402 — lazy: only the demo renders the boot offer (boot -> restore_vault is a lazy back-edge)
    import validate   # noqa: E402 — the throwaway repo root the demo wrote engine.json under
    engine_json = os.path.join(validate.ROOT, ".engine", "engine.json")

    def _set_running(v):
        with open(engine_json, "w", encoding="utf-8") as fh:
            json.dump({"engine_release": v}, fh)

    # A clean pre-update state; the FIRST migration of the upgrade is the reversibility floor (stamped), then two more
    # migrations of the SAME multi-step update reshape the store (#303 — "undo the update" must reach before ALL of them).
    os.remove(ledger.ledger_path()); _quiet_remove(ledger.meta_path())
    bv._demo_plant("Pre-update note: the roadmap is locked — SNORGLE.")
    ledger.set_generation(4)
    pre_upgrade = _read_bytes(ledger.ledger_path())
    _set_running("2.0.0")                                          # the engine version that RUNS the update
    floor = bv.snapshot_for_migration("recall-ledger", "2.0.0", migration_id="demo@1.0.0",
                                      reversibility_floor=True, transport=fake.transport)
    floor_tag = (floor or {}).get("tag")
    bv._demo_plant("Migration A reshaped the store — FROOBLE.")
    bv.snapshot_for_migration("recall-ledger", "2.0.0", migration_id="demo@1.1.0", transport=fake.transport)
    bv._demo_plant("Migration B reshaped the store — BAZZLE.")
    bv.snapshot_for_migration("recall-ledger", "2.0.0", migration_id="demo@1.2.0", transport=fake.transport)
    _set_running("1.0.0")                                         # the update is UNDONE: code drops back, the store does not
    offer = detect_migration_revert(github=None)
    cites_floor = bool(offer) and offer.get("tag") == floor_tag   # cites the WHOLE-update floor, not the last step
    marker = boot.present_marker_line({"gate": "on", "refused": False, "finding_count": 0, "strand": None,
                                       "pr_conflict": None, "migration_revert": offer, "restore_offer": None})
    leak_free = bool(floor_tag) and floor_tag not in marker and "engine-snapshot/" not in marker
    print(f"  the engine detects it and offers the undo by plain handle, never the raw backup name: {cites_floor and leak_free}")
    undo = restore_pre_migration(tag=offer["tag"], transport=fake.transport, consent="y", github=None) if offer else {}
    back = _read_bytes(ledger.ledger_path())
    cleared = bv.read_migration_stamp() is None and detect_migration_revert(github=None) is None
    print(f"  restoring brings back the memory from before the WHOLE update, byte-identical: {back == pre_upgrade}")
    print(f"  and the offer then self-clears (the store matches the code again): {cleared}")
    part8 = (cites_floor and leak_free and undo.get("ok") is True and back == pre_upgrade and cleared)
    print(f"  => {'the engine detects code-older-than-data and offers a clean whole-update undo.' if part8 else '!!! the detector/offer failed'}")

    ok = part1 and part2 and part3 and part4 and part5 and part6 and part7 and part8
    if not ok:
        print("\nDEMO UNEXPECTED: a restore guarantee did not hold (the round trip, the resurrection guard, the "
              "override, the offer, the consent text, degrade-and-disclose, the migration-revert restore, or the "
              "code-older-than-data detection + whole-update undo).", file=sys.stderr)
    return bool(ok)


def _demo_live() -> int:
    """The LIVE end-to-end test the operator runs: create a throwaway PRIVATE repo, back a tiny FAKE memory up into
    it, RESTORE from it into a throwaway cabinet and prove it round-trips, then DELETE the repo. The real ledger and
    pointer are never touched; the DELETE is name-guarded (backup_vault._safe_demo_delete) to the disposable repo."""
    import tempfile
    print("=" * 96)
    print("LIVE TEST — creates a REAL, throwaway PRIVATE repo on your GitHub, backs a tiny fake memory up into it,")
    print("            restores it back and checks it matches, then DELETES the repo. Real memory is never touched.")
    print("=" * 96)
    project = bv._project_slug()
    if not project or "/" not in project:
        print(f"\n  {bv._MSG_NO_PROJECT}")
        return 0
    gh = bv._gh()
    if gh is None:
        print(f"\n  {bv._MSG_NO_TOKEN}")
        return 0
    import secrets
    project_name = project.split("/")[-1]
    demo_name = f"{project_name}{bv._DEMO_MARKER}{secrets.token_hex(4)}"
    status_code, repo_obj = bv._send(gh, "POST", "/user/repos",
                                     {"name": demo_name, "private": True, "auto_init": True,
                                      "description": "Throwaway engine memory-restore live test — safe to delete."})
    if status_code == 403:
        print(f"\n  {bv._MSG_NO_SCOPE}")
        return 0
    if status_code not in (200, 201) or not isinstance(repo_obj, dict):
        print("\n  I couldn't create the throwaway test repository just now. Nothing was created; try again later.")
        return 0
    owner = (repo_obj.get("owner") or {}).get("login")
    repo = repo_obj.get("name")
    branch = repo_obj.get("default_branch") or "main"
    print(f"\n  Created a throwaway private repo: https://github.com/{owner}/{repo}")
    with tempfile.TemporaryDirectory() as cabinet, tempfile.TemporaryDirectory() as root:
        import validate
        old_root = validate.ROOT
        os.environ["ENGINE_MEMORY_DIR"] = cabinet
        validate.ROOT = root
        os.makedirs(os.path.join(root, ".engine"), exist_ok=True)
        with open(os.path.join(root, ".engine", "engine.json"), "w", encoding="utf-8") as fh:
            json.dump({"engine_release": "0.0.0-dev"}, fh)
        try:
            bv._demo_plant("A throwaway note for the live restore test.")
            original = _read_bytes(ledger.ledger_path())
            files = {"livetest/ledger.ndjson": original,
                     "livetest/manifest.json": (json.dumps(bv.build_manifest(ledger_path=ledger.ledger_path())) + "\n").encode()}
            pushed = bv._push_files(gh, owner, repo, branch, files)
            bv.write_pointer(owner, repo, branch, "livetest")
            os.remove(ledger.ledger_path())
            restored = restore_now(consent="y", github=None)
            came_back = _read_bytes(ledger.ledger_path()) if os.path.exists(ledger.ledger_path()) else b""
            print(f"  Backed up a tiny fake memory: {'yes' if pushed else 'no'}")
            print(f"  Restored it from the real repo and it matched: {came_back == original and restored.get('ok')}")
        finally:
            validate.ROOT = old_root
            os.environ.pop("ENGINE_MEMORY_DIR", None)
    if bv._safe_demo_delete(repo, project_name):
        del_status, _ = bv._send(gh, "DELETE", f"/repos/{owner}/{repo}")
        if del_status in (200, 204):
            print("  Deleted the throwaway repo. Nothing is left behind.")
        else:
            print(f"  I couldn't auto-delete it (that repo is PRIVATE and harmless). Remove it yourself with:\n"
                  f"      gh repo delete {owner}/{repo} --yes")
    else:
        print(f"  Safety: the repo name didn't look disposable, so I did NOT delete it:\n"
              f"      gh repo delete {owner}/{repo} --yes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
