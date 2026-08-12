#!/usr/bin/env python3
"""projects_sync.py — the github-projects-sync sync tool (the engine-field projection).

It reads the repo-authoritative work signal (via boot's own already-resolved, operator-clean signals)
and writes the engine-owned custom fields onto an external GitHub Projects v2 board through the
operator's local `gh`/GraphQL — strictly READ-THE-REPO / WRITE-THE-BOARD. The board is a one-way,
replaceable projection: the engine writes ONLY its own custom fields and adds ONLY items already
carrying the engine label (idempotently, applying no label itself), and NEVER touches Status, column,
card position, or any existing item's placement (those are native automation's and the operator's).
Every failure no-ops and discloses — it never errors and never blocks a session.

Design seams kept faithful to the locked module spec:
  - The projection reuses ``boot.gather_signals`` — the same operator-facing, leak-guard-clean signals
    the boot dashboard and /engine-status render — so the board stays CONSISTENT with what the operator
    already reads, and no substrate read is duplicated (attention's raw partition carries only opaque
    ids, which must never reach the board face — the leak guard). The five board-face field names are plain
    language; maintainer vocabulary never appears.
  - GitHub Projects v2 is GraphQL-only. ``BoardGraphQL`` carries an INJECTABLE transport
    (``transport(method, path, body) -> (status, json|None)``, the telemetry/audit-digest idiom), so
    tests and the demo fake ONLY the network and run the real resolve/add/write/verify logic. The REAL
    board write never runs in the construction repo (no live board) — the named inductive gap.
  - The trigger is a non-blocking, best-effort, fail-open SessionStart hook (matchers startup/resume),
    debounced by a gitignored sidecar timestamp so reopening a session does not hammer GitHub. It runs
    in every stance (the projection is read-only, safe under Explore's no-write stance). A
    never-configured board is a SILENT no-op (anti-nag); a configured-but-broken board (scope lapsed /
    board deleted / GitHub down) discloses the one plain-language fix.

CLI:
  python tools/projects_sync/projects_sync.py session-start   # the wired hook: debounced, fail-open
  python tools/projects_sync/projects_sync.py sync            # force a sync now (ignores the debounce)
  python tools/projects_sync/projects_sync.py plan            # dry-run: print the projection, write nothing
  python tools/projects_sync/projects_sync.py resolve [id]    # resolve the board's field ids -> config
  python tools/projects_sync/projects_sync.py check           # plain-language health of the projection
  python tools/projects_sync/projects_sync.py demo            # mutation-free fail-then-pass self-check
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Make the sibling `.engine/tools/` modules importable whether imported as `projects_sync.projects_sync`
# or run as the wired hook script — the same parent insert the memory subdir tools use.
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import hooks  # noqa: E402 — .engine/tools/hooks.py: the SessionStart fail-open harness
import boot  # noqa: E402 — the already-resolved, leak-guard-clean signal assembler + gh token/slug
import validate  # noqa: E402 — ROOT (test-redirectable) for the gitignored config path
import telemetry  # noqa: E402 — ENGINE_DOMAIN_LABEL + the open-engine-item lister
import github_client  # noqa: E402  (the shared authenticated GitHub API client; request-build)

# ---- constants ---------------------------------------------------------------------------------

GRAPHQL_PATH = "/graphql"
USER_AGENT = "engine-github-projects-sync"

# The five engine-owned custom fields, BY THE PLAIN-LANGUAGE NAME the operator sees on the board face
# (the leak guard — never maintainer vocabulary). Resolved to opaque ids at runtime. All are text fields
# in v1, so a value write never needs single-select option resolution.
FIELD_BUILDING = "What's being built"
FIELD_NEXT = "What's next"
FIELD_REVIEW = "Needs your review"
FIELD_ISSUES = "Known issues"
FIELD_SYNCED = "Last synced"
ENGINE_FIELD_NAMES = (FIELD_BUILDING, FIELD_NEXT, FIELD_REVIEW, FIELD_ISSUES, FIELD_SYNCED)

# Debounce: skip a re-sync within this window of the last one (a recorded build-spec leaf — operator
# chose session-start/resume triggering, so coalesce rapid reopens). Fail-open: an absent or unreadable
# stamp means "sync".
DEBOUNCE_SECONDS = 15 * 60

# Sync outcomes (the handler decides disclosure from these).
NOT_CONFIGURED = "not-configured"   # no board set up yet -> disclose the plain-language next step (setup)
SKIPPED = "skipped"                 # within the debounce window -> silent
SYNCED = "synced"                   # wrote the engine fields -> silent (the board is the surface)
DEGRADED = "degraded"              # configured but the board could not be reached -> disclose the fix


class DegradedReadError(Exception):
    """A board read/write could not complete (network, auth, deleted board, GraphQL error). Caught at the
    sync boundary and turned into a plain-language DEGRADED disclosure — never swallowed as success."""


# ---- the gitignored per-instance config (board coordinates) ------------------------------------

def _config_dir() -> str:
    """The gitignored per-instance config directory, computed from validate.ROOT at CALL time so a test
    can redirect ROOT. Keyed out of version control by the module's `gitignore` wire and pruned from the
    ownership walk (module_coherence.PRUNE_PATHS) — it is operator-local state, never a committed file."""
    return os.path.join(validate.ROOT, ".engine", "projects-sync")


def _config_path() -> str:
    return os.path.join(_config_dir(), "board-config.json")


def _sidecar_path() -> str:
    return os.path.join(_config_dir(), ".last-sync")


def load_config() -> dict | None:
    """The board-coordinate config, or None when no board is set up yet / the file is unreadable or not a
    schema-version-1 object. None is the never-configured state — a SILENT no-op, never an error."""
    path = _config_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:  # noqa: BLE001 — an unreadable config degrades to "not configured", never crashes
        return None
    if not isinstance(cfg, dict) or cfg.get("schema_version") != 1 or not isinstance(cfg.get("project"), dict):
        return None
    return cfg


def save_config(cfg: dict) -> None:
    """Write the board-coordinate config (2-space indent + trailing newline), creating the gitignored
    directory. Only the setup `resolve` path writes this."""
    os.makedirs(_config_dir(), exist_ok=True)
    with open(_config_path(), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")


def _recently_synced(now_ts: float, interval: int = DEBOUNCE_SECONDS) -> bool:
    """True iff the last sync stamp is within `interval` of `now_ts`. Fail-open: an absent, unreadable, or
    future-dated stamp returns False (sync), so a corrupt stamp never wedges the projection off."""
    try:
        with open(_sidecar_path(), encoding="utf-8") as fh:
            last = float(fh.read().strip())
    except Exception:  # noqa: BLE001 — no/garbled stamp -> not recent -> sync
        return False
    return 0 <= (now_ts - last) < interval


def _stamp_sync(now_ts: float) -> None:
    try:
        os.makedirs(_config_dir(), exist_ok=True)
        with open(_sidecar_path(), "w", encoding="utf-8") as fh:
            fh.write(f"{now_ts:.0f}\n")
    except Exception:  # noqa: BLE001 — a stamp-write failure only costs an extra sync next time
        pass


# ---- the GraphQL boundary (injectable transport; Projects v2 is GraphQL-only) ------------------

class BoardGraphQL:
    """The Projects v2 GraphQL boundary. `transport(method, path, body) -> (status, json|None)` is
    injectable (the telemetry/audit-digest idiom) so tests/the demo fake ONLY the network. NOT
    telemetry.GitHubIssues, which is REST/issue-shaped — this posts GraphQL to one endpoint. A 200 body
    may carry `errors[]`; that is detected here as a degrade, never read as success."""

    def __init__(self, token: str, *, transport=None):
        self.token = token
        self._transport = transport or self._http

    def _http(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = github_client.request(path, self.token, user_agent=USER_AGENT, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:          # 4xx/5xx — surface the status, never swallow
            return exc.code, None
        except urllib.error.URLError as exc:            # network unreachable — a read failure
            raise DegradedReadError(f"GitHub is unreachable: {exc}") from exc

    def run(self, query: str, variables: dict) -> dict:
        """Execute one GraphQL operation, returning its `data`. RAISES DegradedReadError on any transport
        failure OR a 200 body carrying `errors[]` — a board the engine cannot reach must never read as a
        clean result."""
        status, body = self._transport("POST", GRAPHQL_PATH, {"query": query, "variables": variables or {}})
        if status >= 400 or body is None:
            raise DegradedReadError(f"GitHub returned {status} for a board operation")
        if body.get("errors"):
            msgs = "; ".join((e or {}).get("message", "?") for e in body["errors"])
            raise DegradedReadError(f"GitHub reported a board error: {msgs}")
        return body.get("data") or {}


_RESOLVE_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on ProjectV2 {
      id
      number
      url
      title
      fields(first: 50) {
        nodes {
          ... on ProjectV2FieldCommon { id name dataType }
          ... on ProjectV2SingleSelectField { id name options { id name } }
        }
      }
      workflows(first: 20) { nodes { id name enabled } }
    }
  }
}
"""

_ADD_ITEM_MUTATION = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item { id }
  }
}
"""

# Writes ONLY a text value into an engine-owned field on an engine-owned item — never Status, column, or
# card position. The field id is always one the engine resolved into config.fields, so the field-ownership
# invariant is structural: this mutation cannot target a field the engine does not own.
_SET_TEXT_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $text: String!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: {text: $text}}
  ) {
    projectV2Item { id }
  }
}
"""


# Lists the OPEN engine-labeled issues AND pull requests by their GraphQL node id (the contentId the
# defensive add takes). With auto-add off — the default — this is what populates the board with engine
# work, so it must resolve real ids, not silently come back empty.
_ENGINE_ITEMS_QUERY = """
query($owner: String!, $name: String!, $label: String!) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, states: [OPEN], labels: [$label]) { nodes { id } }
    pullRequests(first: 100, states: [OPEN], labels: [$label]) { nodes { id } }
  }
}
"""


def resolve_board(gql: BoardGraphQL, project_id: str) -> dict:
    """Resolve a board's current field ids + auto-add state by its node id. Returns
    {id, number, url, fields:{name->id}, options:{name->{opt->id}}, auto_add_enabled:bool|None}. RAISES
    DegradedReadError when the board cannot be read (deleted, scope lapsed, unreachable)."""
    data = gql.run(_RESOLVE_QUERY, {"id": project_id})
    node = data.get("node") or {}
    if not node.get("id"):
        raise DegradedReadError("the board could not be found (it may have been deleted)")
    fields: dict = {}
    options: dict = {}
    for f in ((node.get("fields") or {}).get("nodes") or []):
        name, fid = f.get("name"), f.get("id")
        if not name or not fid:
            continue
        fields[name] = fid
        if f.get("options"):
            options[name] = {o["name"]: o["id"] for o in f["options"] if o.get("name") and o.get("id")}
    auto = None
    for w in ((node.get("workflows") or {}).get("nodes") or []):
        if "auto-add" in (w.get("name") or "").lower():
            auto = bool(w.get("enabled"))
    return {"id": node.get("id"), "number": node.get("number"), "url": node.get("url"),
            "fields": fields, "options": options, "auto_add_enabled": auto}


def add_item(gql: BoardGraphQL, project_id: str, content_id: str) -> str | None:
    """Idempotently add one engine-labeled item to the board (addProjectV2ItemById returns the existing
    item when present), returning its project-item id. The caller passes ONLY already-engine-labeled
    content ids; this applies no label itself."""
    data = gql.run(_ADD_ITEM_MUTATION, {"projectId": project_id, "contentId": content_id})
    return (((data.get("addProjectV2ItemById") or {}).get("item")) or {}).get("id")


def set_text_field(gql: BoardGraphQL, project_id: str, item_id: str, field_id: str, text: str) -> None:
    """Write one engine-owned text field on one engine-owned item. Never Status/column/position."""
    gql.run(_SET_TEXT_MUTATION, {"projectId": project_id, "itemId": item_id, "fieldId": field_id,
                                 "text": text})


# ---- the projection compute (pure given the signals + the clock string) ------------------------

def _state_phase(state) -> str | None:
    if not isinstance(state, dict):
        return None
    for key in ("phase", "where_we_are", "headline", "summary"):
        val = state.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def compute_projection(signals: dict, synced_stamp: str) -> dict:
    """Map boot's already-resolved, leak-guard-clean signals to the five engine-owned board fields. PURE
    given the signals dict + the time string, so the whole projection is fixture-testable with no network.
    A missing signal renders as an em dash rather than a guess or a maintainer-vocabulary leak."""
    standing = signals.get("live_standing") or {}
    building = (standing.get("phase") if isinstance(standing, dict) else None) \
        or _state_phase(signals.get("state")) or "—"
    att_lines = signals.get("att_lines") or []
    nxt = att_lines[0] if att_lines else "—"
    review = signals.get("finding_count")
    issues = signals.get("debt_count")
    return {
        FIELD_BUILDING: building,
        FIELD_NEXT: nxt,
        FIELD_REVIEW: str(review) if review is not None else "—",
        FIELD_ISSUES: str(issues) if issues is not None else "—",
        FIELD_SYNCED: synced_stamp,
    }


def _synced_stamp(now: datetime | None = None) -> str:
    """The board's 'Last synced' value: a full UTC date-time with an explicit zone, so an operator in any
    timezone reads it unambiguously (a bare HH:MM would be read as their own local time)."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")


def _engine_item_content_ids(gql: BoardGraphQL, repo: str | None, label: str) -> list:
    """The GraphQL node ids of the OPEN engine-labeled issues AND pull requests the engine may add to the
    board (the contentIds addProjectV2ItemById takes). RAISES DegradedReadError when the repo cannot be
    read — the caller turns that into a DEGRADED disclosure rather than a false 'synced'. An empty list
    means there is genuinely no open engine-labeled work right now."""
    if not repo or "/" not in repo:
        return []
    owner, name = repo.split("/", 1)
    data = gql.run(_ENGINE_ITEMS_QUERY, {"owner": owner, "name": name, "label": label})
    repository = data.get("repository") or {}
    ids = []
    for key in ("issues", "pullRequests"):
        for node in ((repository.get(key) or {}).get("nodes") or []):
            if node.get("id"):
                ids.append(node["id"])
    return ids


# ---- sync (the heart) --------------------------------------------------------------------------

def sync(*, force: bool = False, session_id: str | None = None, config: dict | None = None,
         signals: dict | None = None, gql: BoardGraphQL | None = None, items: list | None = None,
         now: datetime | None = None) -> dict:
    """Project the engine signal onto the configured board. Returns a structured result the CLI/handler
    render: {"status": one of NOT_CONFIGURED/SKIPPED/SYNCED/DEGRADED, "message": plain-language, ...}.
    NEVER raises and NEVER blocks. Every injectable argument lets tests/the demo run the real logic with
    the network and clock faked.

    Order: a never-configured board returns NOT_CONFIGURED with the plain-language setup next step (the
    handler surfaces it); the debounce skips a recent sync; otherwise gather the projection, resolve the
    board credential, idempotently add the engine-labeled items, and write the engine fields on them. A
    board the engine cannot reach (deleted, scope lapsed, GitHub down) returns DEGRADED with the one
    plain-language fix — never an error."""
    cfg = config if config is not None else load_config()
    if cfg is None:
        return {"status": NOT_CONFIGURED,
                "message": "No progress board is set up for this project yet. Run /engine-board-setup to "
                           "create one — until then the engine just works from your issues and pull "
                           "requests as usual."}
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    if not force and _recently_synced(now_ts):
        return {"status": SKIPPED, "message": "The board was synced recently; skipping this one."}

    project = cfg.get("project") or {}
    project_id = project.get("id")
    fields = cfg.get("fields") or {}
    label = cfg.get("label") or telemetry.ENGINE_DOMAIN_LABEL
    if not project_id:
        return {"status": DEGRADED,
                "message": "Your progress board's settings look incomplete. Run /engine-board-setup again "
                           "to reconnect it. Nothing else is affected — your issues and pull requests are "
                           "unchanged."}

    if signals is None:
        signals = boot.gather_signals(session_id)
    projection = compute_projection(signals, _synced_stamp(now))

    if gql is None:
        token = boot.gh_token()
        if not token:
            return {"status": DEGRADED,
                    "message": "The engine couldn't reach GitHub to update your progress board (it isn't "
                               "signed in here). Run `gh auth login` and it will refresh the board next "
                               "session. Your issues and pull requests are unchanged."}
        gql = BoardGraphQL(token)

    try:
        if items is None:
            items = _engine_item_content_ids(gql, boot.repo_slug(), label)
        written = 0
        for content_id in items:
            item_id = add_item(gql, project_id, content_id)
            if not item_id:
                continue
            for name, value in projection.items():
                field_id = fields.get(name)
                if field_id:
                    set_text_field(gql, project_id, item_id, field_id, value)
                    written += 1
    except DegradedReadError as exc:
        return {"status": DEGRADED,
                "message": "The engine couldn't reach your progress board to refresh it "
                           f"({exc}). It will try again next session; your issues and pull requests are "
                           "unchanged. If the board was deleted, run /engine-board-setup to make a new one."}

    _stamp_sync(now_ts)
    msg = (f"Refreshed the board for {len(items)} item(s) ({written} field update(s))." if items
           else "No open engine-labelled work to show on the board right now.")
    return {"status": SYNCED, "message": msg, "projection": projection, "items": len(items)}


# ---- the wired SessionStart hook (non-blocking, fail-open, mode-agnostic) -----------------------

def _session_start_handler(payload) -> dict:
    """The SessionStart sweep: a debounced, best-effort sync. DISCLOSES the one plain-language next step
    whenever the board no-ops for a reason the operator can act on — a never-configured board
    (NOT_CONFIGURED: "run /engine-board-setup") or a board that WAS set up but cannot be reached (DEGRADED).
    SILENT on a debounce skip or a clean sync (the board itself is the surface). Read-only, so it runs in
    every stance."""
    session_id = (payload or {}).get("session_id") if isinstance(payload, dict) else None
    result = sync(session_id=session_id)
    if result.get("status") in (DEGRADED, NOT_CONFIGURED):
        return hooks.inject(result.get("message", ""))
    return hooks.proceed()


# ---- CLI ---------------------------------------------------------------------------------------

def _cmd_plan() -> int:
    """Dry-run: print the five field values the projection would write, from live signals. Writes nothing."""
    signals = boot.gather_signals(None)
    projection = compute_projection(signals, _synced_stamp())
    print("The board would show (writes nothing):")
    for name, value in projection.items():
        print(f"  {name}: {value}")
    return 0


def _cmd_sync() -> int:
    result = sync(force=True)
    print(result.get("message", result.get("status", "")))
    return 0


def _cmd_check() -> int:
    """Plain-language health of the projection — never errors."""
    cfg = load_config()
    if cfg is None:
        print("No progress board is set up yet. Run /engine-board-setup to create one.")
        return 0
    project = cfg.get("project") or {}
    token = boot.gh_token()
    if not token:
        print("A board is set up, but the engine isn't signed in to GitHub here, so it can't refresh it. "
              "Run `gh auth login`.")
        return 0
    try:
        board = resolve_board(BoardGraphQL(token), project.get("id"))
    except DegradedReadError as exc:
        print(f"A board is set up, but the engine couldn't reach it ({exc}). If it was deleted, run "
              "/engine-board-setup to make a new one.")
        return 0
    auto = board.get("auto_add_enabled")
    auto_line = ("auto-add is on" if auto else
                 "auto-add is off — the board still shows the engine's own work; turn it on in the "
                 "Projects UI to also pull in your other issues and pull requests")
    print(f"Board {project.get('url') or project.get('number')} is connected ({auto_line}).")
    return 0


def _cmd_resolve(rest: list) -> int:
    """Resolve the board's current field ids + auto-add state into the config. Takes the board's node id
    (printed by `gh project create`); falls back to the id already in the config (a re-resolve)."""
    cfg = load_config() or {"schema_version": 1, "project": {}}
    project_id = rest[0] if rest else (cfg.get("project") or {}).get("id")
    if not project_id:
        print("usage: resolve <project-node-id>  (the id `gh project create --format json` prints)",
              file=sys.stderr)
        return 2
    token = boot.gh_token()
    if not token:
        print("The engine isn't signed in to GitHub here. Run `gh auth login`, then try again.",
              file=sys.stderr)
        return 1
    try:
        board = resolve_board(BoardGraphQL(token), project_id)
    except DegradedReadError as exc:
        print(f"Couldn't read the board ({exc}). Check it exists and you've granted the `project` scope.",
              file=sys.stderr)
        return 1
    cfg["schema_version"] = 1
    cfg["project"] = {k: board.get(k) for k in ("id", "number", "url") if board.get(k) is not None}
    cfg["fields"] = board.get("fields") or {}
    if board.get("options"):
        cfg["options"] = board["options"]
    if board.get("auto_add_enabled") is not None:
        cfg["auto_add_enabled"] = board["auto_add_enabled"]
    save_config(cfg)
    found = [n for n in ENGINE_FIELD_NAMES if n in cfg["fields"]]
    print(f"Connected board {cfg['project'].get('url') or project_id}. Engine fields found: "
          f"{', '.join(found) if found else 'none yet — create them on the board, then re-run resolve'}.")
    return 0


# ---- demo: a mutation-free, falsifiable self-check (the in-tool-demo-failure-path floor) --------

class _RecordingGQL:
    """A fake BoardGraphQL transport that records every operation and serves canned data — fakes ONLY the
    network so the real resolve/add/set logic runs. `error_mode` makes a query return a 200 + errors[]
    body so the degrade path is exercised."""

    def __init__(self, *, error_mode: bool = False):
        self.calls: list = []
        self.error_mode = error_mode

    def transport(self, method, path, body):
        self.calls.append((path, body))
        if path != GRAPHQL_PATH:
            return 404, None
        if self.error_mode:
            return 200, {"errors": [{"message": "the board was not found"}]}
        query = (body or {}).get("query", "")
        variables = (body or {}).get("variables", {})
        if "repository(" in query:
            return 200, {"data": {"repository": {
                "issues": {"nodes": [{"id": "ISSUE_1"}, {"id": "ISSUE_2"}]},
                "pullRequests": {"nodes": []}}}}
        if "addProjectV2ItemById" in query:
            # idempotent: the same content id always maps to the same stable item id
            return 200, {"data": {"addProjectV2ItemById": {"item": {"id": "ITEM_" + variables["contentId"]}}}}
        if "updateProjectV2ItemFieldValue" in query:
            return 200, {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["itemId"]}}}}
        return 200, {"data": {}}


def _demo() -> int:
    """Prove the engine writes ONLY its own fields on its own labeled items, idempotently, and degrades on
    a board error — RETURNS NON-ZERO if any invariant is violated (the falsification can fail). Mutation
    free: it injects a fake transport + fake signals + an in-memory config and runs the real `sync` under a
    throwaway ROOT, so the real working tree is never touched (the sync's debounce stamp lands in the temp
    dir, which is removed)."""
    import shutil
    import tempfile
    old_root = validate.ROOT
    validate.ROOT = tempfile.mkdtemp(prefix="engine-projects-demo-")
    try:
        rc = _demo_body()
    finally:
        shutil.rmtree(validate.ROOT, ignore_errors=True)
        validate.ROOT = old_root
    if rc != 0:
        return 1   # the falsification bit — a broken invariant fails the demo (a reachable non-zero exit)
    return 0


def _demo_body() -> int:
    fields = {FIELD_BUILDING: "F_BUILD", FIELD_NEXT: "F_NEXT", FIELD_REVIEW: "F_REVIEW",
              FIELD_ISSUES: "F_ISSUES", FIELD_SYNCED: "F_SYNCED"}
    cfg = {"schema_version": 1, "project": {"id": "PVT_demo"}, "fields": fields, "label": "engine"}
    signals = {"live_standing": {"phase": "building the demo"}, "att_lines": ["carry the open PR forward"],
               "finding_count": 4, "debt_count": 2, "state": None}
    gql = _RecordingGQL()
    result = sync(force=True, config=cfg, signals=signals, gql=BoardGraphQL("tok", transport=gql.transport),
                  items=["ISSUE_1", "ISSUE_2"], now=datetime(2026, 1, 1, 14, 32, tzinfo=timezone.utc))

    failures = []
    if result.get("status") != SYNCED:
        failures.append(f"expected a clean sync, got {result.get('status')}")
    # Field-ownership invariant: every field write targets an ENGINE field id, never Status/position.
    own_field_ids = set(fields.values())
    own_item_ids = {"ITEM_ISSUE_1", "ITEM_ISSUE_2"}
    for path, body in gql.calls:
        query = (body or {}).get("query", "")
        variables = (body or {}).get("variables", {})
        if "updateProjectV2ItemFieldValue" in query:
            if variables.get("fieldId") not in own_field_ids:
                failures.append(f"wrote a field the engine does not own: {variables.get('fieldId')}")
            if variables.get("itemId") not in own_item_ids:
                failures.append(f"wrote onto an item the engine did not add: {variables.get('itemId')}")
        if "addProjectV2ItemById" in query and variables.get("contentId") not in {"ISSUE_1", "ISSUE_2"}:
            failures.append(f"added a non-engine-labeled item: {variables.get('contentId')}")
        if "updateProjectV2ItemFieldValue" in query and "status" in query.lower():
            failures.append("a write touched Status — forbidden")
    # The projection rendered the leak-guard-clean values.
    if result.get("projection", {}).get(FIELD_SYNCED) != "2026-01-01 14:32 UTC":
        failures.append("the last-synced stamp did not render")
    # Idempotent add: two items -> exactly two add calls, ten field writes (5 fields x 2 items).
    adds = sum(1 for _p, b in gql.calls if "addProjectV2ItemById" in (b or {}).get("query", ""))
    sets = sum(1 for _p, b in gql.calls if "updateProjectV2ItemFieldValue" in (b or {}).get("query", ""))
    if adds != 2 or sets != 10:
        failures.append(f"expected 2 adds + 10 field writes, got {adds} + {sets}")
    # Degrade path: a board error must yield DEGRADED, never a crash or a false success.
    err = sync(force=True, config=cfg, signals=signals,
               gql=BoardGraphQL("tok", transport=_RecordingGQL(error_mode=True).transport),
               items=["ISSUE_1"], now=datetime(2026, 1, 1, 14, 33, tzinfo=timezone.utc))
    if err.get("status") != DEGRADED:
        failures.append(f"a board error should degrade, got {err.get('status')}")
    # Item discovery: the resolver collects engine node ids from the live query (no longer bypassed) — the
    # default-config board-population path. An empty result here would silently project nothing.
    disc = _engine_item_content_ids(BoardGraphQL("tok", transport=_RecordingGQL().transport),
                                    "acme/widgets", "engine")
    if disc != ["ISSUE_1", "ISSUE_2"]:
        failures.append(f"item discovery did not resolve engine node ids: {disc}")
    # A never-configured board no-ops, but now DISCLOSES the plain-language setup next step (it used to be
    # silent). Under the throwaway ROOT there is no board config, so drive the REAL SessionStart handler and
    # prove it surfaces the setup step rather than staying silent.
    nc = sync(force=True, config=None, signals=signals, gql=gql, items=[])
    if nc.get("status") != NOT_CONFIGURED:
        failures.append(f"a never-configured board should be NOT_CONFIGURED, got {nc.get('status')}")
    decided = _session_start_handler({"session_id": "demo"})
    if decided.get("action") != "inject" or "engine-board-setup" not in decided.get("context", ""):
        failures.append(f"a never-configured board should surface the setup step, got {decided}")

    if failures:
        print("DEMO FAILED — the projection broke an invariant:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED — the engine wrote only its own fields on its own labeled items, idempotently, "
          "degraded cleanly on a board error, and a never-configured board surfaced its setup next step.")
    return 0


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "session-start":
        return hooks.run_hook("SessionStart", _session_start_handler)
    if cmd == "sync":
        return _cmd_sync()
    if cmd == "plan":
        return _cmd_plan()
    if cmd == "resolve":
        return _cmd_resolve(argv[1:])
    if cmd == "check":
        return _cmd_check()
    if cmd == "demo":
        return _demo()
    print(f"usage: projects_sync.py {{session-start|sync|plan|resolve|check|demo}}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
