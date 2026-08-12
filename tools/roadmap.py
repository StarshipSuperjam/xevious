#!/usr/bin/env python3
"""Validate and reconcile the dependency-aware Xevious GitHub roadmap.

The committed manifest is authoritative. GitHub Issues and Project #4 are a
projection. Writes are idempotent by the stable ``roadmap-key`` marker and are
journaled after every successful external mutation so an interrupted run rolls
forward instead of creating duplicates.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "roadmap" / "manifest.json"
MIGRATION_PATH = ROOT / "docs" / "roadmap" / "migration.json"
CATALOG_PATH = ROOT / "docs" / "MECHANICS_CATALOG.md"
KEY_MARKER = "<!-- roadmap-key: {key} -->"
PROPOSED_MARKER = "<!-- roadmap-migration: PR #36 -->"


class RoadmapError(RuntimeError):
    """The desired roadmap or its live projection is unsafe to apply."""


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoadmapError(f"cannot read {path.relative_to(ROOT)}: {exc}") from exc
    if not isinstance(value, dict):
        raise RoadmapError(f"{path.relative_to(ROOT)} must contain a JSON object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def gh(*args: str, input_text: str | None = None) -> Any:
    command = ["gh", *args]
    result = subprocess.run(
        command,
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RoadmapError(f"{' '.join(command)} failed: {detail}")
    output = result.stdout.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def catalog_ids() -> set[str]:
    text = CATALOG_PATH.read_text(encoding="utf-8")
    return set(re.findall(r"^\| ([A-Z]+-[0-9]+) \|", text, flags=re.MULTILINE))


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if manifest.get("version") != 1:
        failures.append("manifest version must be 1")
    parents = manifest.get("parents")
    leaves = manifest.get("leaves")
    if not isinstance(parents, list) or not isinstance(leaves, list):
        return failures + ["parents and leaves must be lists"]

    parent_keys = [item.get("key") for item in parents if isinstance(item, dict)]
    leaf_keys = [item.get("key") for item in leaves if isinstance(item, dict)]
    if len(parent_keys) != len(set(parent_keys)):
        failures.append("parent keys must be unique")
    if len(leaf_keys) != len(set(leaf_keys)):
        failures.append("leaf keys must be unique")
    parent_map = {item.get("key"): item for item in parents if isinstance(item, dict)}
    leaf_map = {item.get("key"): item for item in leaves if isinstance(item, dict)}

    criteria: dict[str, str] = {}
    for leaf in leaves:
        if not isinstance(leaf, dict):
            failures.append("every leaf must be an object")
            continue
        key = leaf.get("key", "<missing>")
        parent = parent_map.get(leaf.get("parent"))
        if parent is None:
            failures.append(f"{key}: unknown parent {leaf.get('parent')!r}")
        if not leaf.get("milestone"):
            failures.append(f"{key}: every leaf needs a milestone")
        if not leaf.get("slice"):
            failures.append(f"{key}: every leaf needs a delivery slice")
        if leaf.get("proof") not in {"playable", "operator", "historical"}:
            failures.append(f"{key}: invalid proof level")
        if leaf.get("status") == "history" and not leaf.get("delivered_by"):
            failures.append(f"{key}: history needs a delivering PR")
        if parent and parent.get("spec_status") in {"draft", "provisional"}:
            if leaf.get("status") != "provisional":
                failures.append(f"{key}: work under an unsettled parent must be provisional")
        for blocker in leaf.get("blocked_by", []):
            if blocker not in leaf_map:
                failures.append(f"{key}: unknown blocker {blocker}")
            if blocker == key:
                failures.append(f"{key}: cannot block itself")
        leaf_criteria = leaf.get("criteria")
        if not isinstance(leaf_criteria, list) or not leaf_criteria:
            failures.append(f"{key}: needs at least one atomic criterion")
            continue
        for criterion in leaf_criteria:
            if criterion in criteria:
                failures.append(
                    f"criterion {criterion} is assigned to both {criteria[criterion]} and {key}"
                )
            criteria[criterion] = key

    # A cycle would make closure impossible even if every individual blocker exists.
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            failures.append(f"blocker cycle includes {key}")
            return
        if key in visited:
            return
        visiting.add(key)
        for blocker in leaf_map.get(key, {}).get("blocked_by", []):
            visit(blocker)
        visiting.remove(key)
        visited.add(key)

    for key in leaf_map:
        visit(key)

    excluded = tuple(manifest.get("excluded_catalog_prefixes", []))
    expected = {item for item in catalog_ids() if not item.startswith(excluded)}
    covered = {criterion.split(".", 1)[0] for criterion in criteria}
    missing = sorted(expected - covered)
    if missing:
        failures.append("catalog mechanics without an atomic roadmap criterion: " + ", ".join(missing))
    slices = {str(leaf.get("slice")) for leaf in leaves}
    expected_slices = {str(number) for number in range(1, 22)} | {"2a"}
    if slices != expected_slices:
        failures.append(
            "delivery slices differ from 1–21 plus 2a: missing "
            + ", ".join(sorted(expected_slices - slices))
            + "; extra "
            + ", ".join(sorted(slices - expected_slices))
        )
    return failures


def marker(key: str) -> str:
    return KEY_MARKER.format(key=key)


def parent_body(parent: dict[str, Any]) -> str:
    spec = parent.get("spec")
    spec_line = f"Builds to `{spec}`." if spec else "Builds to the outcome and engineering plan."
    return "\n".join(
        [
            marker(parent["key"]),
            PROPOSED_MARKER,
            "",
            spec_line,
            "",
            "This is a capability tracker. It is complete only when every native sub-issue is closed.",
            "Capability parents intentionally have no milestone; milestones measure delivery leaves only.",
            "",
            f"Specification status: **{parent['spec_status']}**.",
            "The committed desired state is `docs/roadmap/manifest.json`; the Project board is a derived view.",
        ]
    )


def leaf_body(leaf: dict[str, Any], parent: dict[str, Any]) -> str:
    blockers = leaf.get("blocked_by", [])
    blocker_text = ", ".join(f"`{item}`" for item in blockers) if blockers else "None"
    records = leaf.get("records", [])
    record_text = ", ".join(f"`{item}`" for item in records) if records else "None declared"
    provisional = parent.get("spec_status") in {"draft", "provisional"}
    executable = not provisional and leaf.get("status") != "history"
    lines = [
        marker(leaf["key"]),
        PROPOSED_MARKER,
        "",
        f"Parent capability: `{leaf['parent']}`.",
        f"Delivery slice: **{leaf['slice']}**. Milestone: **{leaf['milestone']}**.",
        f"Roadmap state: **{leaf['status']}**. Executable now: **{'yes' if executable else 'no'}**.",
        "",
        "## Atomic obligations",
        "",
        *[f"- `{criterion}`" for criterion in leaf["criteria"]],
        "",
        "## Blockers",
        "",
        f"- {blocker_text}",
    ]
    if provisional:
        lines += [
            f"- `{parent.get('spec') or parent['key']}` must be settled before this leaf may close.",
        ]
    lines += [
        "",
        "## Completion evidence",
        "",
        f"- Proof level: **{leaf['proof']}**.",
        f"- Mechanics records/catalog rows: {record_text}.",
        "- Automated evidence must exercise the implemented path and its failure case.",
        "- A gameplay-affecting PR stays draft until the operator approves the playable `.sb3` at its tested commit.",
        "",
        "## Closure rule",
        "",
        "This leaf closes only when all obligations above are delivered, every blocker is closed (or closes in the same PR), the required evidence is present, and the repository closure check passes.",
    ]
    if leaf.get("status") == "history":
        lines += [
            "",
            "## Imported history",
            "",
            f"Delivered by PR #{leaf['delivered_by']}. This records original delivery evidence; it does not retroactively certify the work under today's closure controls.",
        ]
    return "\n".join(lines)


def api_issue(repo: str, number: int) -> dict[str, Any]:
    value = gh("api", f"repos/{repo}/issues/{number}")
    if not isinstance(value, dict):
        raise RoadmapError(f"issue #{number} returned an unexpected response")
    return value


def all_issues(repo: str) -> list[dict[str, Any]]:
    value = gh("api", "--paginate", "--slurp", f"repos/{repo}/issues?state=all&per_page=100")
    if not isinstance(value, list):
        raise RoadmapError("issue inventory returned an unexpected response")
    pages = value if not value or isinstance(value[0], list) else [value]
    return [item for page in pages for item in page if "pull_request" not in item]


def live_key_index(repo: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    pattern = re.compile(r"<!-- roadmap-key: ([a-z0-9.-]+) -->")
    for issue in all_issues(repo):
        match = pattern.search(issue.get("body") or "")
        if not match:
            continue
        key = match.group(1)
        if key in found:
            raise RoadmapError(f"live roadmap key {key} appears on more than one issue")
        found[key] = issue
    return found


def migration_template(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "phase": "planned",
        "claim_pr": 36,
        "repository": manifest["repository"],
        "project": manifest["project"],
        "parents": {},
        "leaves": {},
        "project_fields": {},
        "snapshot": {},
    }


def live_plan(manifest: dict[str, Any]) -> dict[str, Any]:
    repo = manifest["repository"]
    keyed = live_key_index(repo)
    explicit_parent_numbers = {int(parent["issue"]) for parent in manifest["parents"] if parent.get("issue")}
    explicit_leaf_numbers = {int(leaf["issue"]) for leaf in manifest["leaves"] if leaf.get("issue")}
    if explicit_parent_numbers & explicit_leaf_numbers:
        raise RoadmapError("one existing issue is assigned as both parent and leaf")
    for item in [*manifest["parents"], *manifest["leaves"]]:
        number = item.get("issue")
        if not number:
            continue
        issue = api_issue(repo, int(number))
        body = issue.get("body") or ""
        found = re.search(r"<!-- roadmap-key: ([a-z0-9.-]+) -->", body)
        if found and found.group(1) != item["key"]:
            raise RoadmapError(f"existing issue #{number} carries conflicting roadmap key {found.group(1)}")
    fields = gh("project", "field-list", "4", "--owner", "@me", "--format", "json")
    field_names = {field["name"] for field in fields["fields"]}
    return {
        "parents": {
            "create": sum(not item.get("issue") and item["key"] not in keyed for item in manifest["parents"]),
            "reuse_or_update": sum(bool(item.get("issue")) or item["key"] in keyed for item in manifest["parents"]),
        },
        "leaves": {
            "create": sum(not item.get("issue") and item["key"] not in keyed for item in manifest["leaves"]),
            "reuse_or_update": sum(bool(item.get("issue")) or item["key"] in keyed for item in manifest["leaves"]),
            "close_as_imported_history": sum(item["status"] == "history" for item in manifest["leaves"]),
        },
        "project_fields_to_create": sorted({"Roadmap role", "Delivery slice", "Proof level"} - field_names),
        "protected_pr": 34,
        "protected_pr_writes": 0,
    }


def snapshot(manifest: dict[str, Any], journal: dict[str, Any]) -> None:
    repo = manifest["repository"]
    project = gh("project", "field-list", "4", "--owner", "@me", "--format", "json")
    items = gh("project", "item-list", "4", "--owner", "@me", "--limit", "500", "--format", "json")
    pr34 = gh(
        "pr", "view", "34", "--repo", repo, "--json",
        "number,title,state,isDraft,body,headRefName,headRefOid,baseRefName,labels,milestone,projectItems,url",
    )
    views = gh(
        "api", "graphql", "-f",
        "query=query { viewer { projectV2(number:4) { id title views(first:20) { nodes { id name layout filter } } } } }",
    )
    milestones = gh("api", f"repos/{repo}/milestones?state=all&per_page=100")
    journal["snapshot"] = {
        "project_fields": project,
        "project_items": items,
        "project_views": views,
        "milestones": milestones,
        "pr34": pr34,
    }
    journal["phase"] = "snapshotted"
    write_json(MIGRATION_PATH, journal)


def milestone_numbers(journal: dict[str, Any]) -> dict[str, int]:
    rows = journal.get("snapshot", {}).get("milestones", [])
    return {row["title"]: row["number"] for row in rows}


def issue_labels(role: str, leaf: dict[str, Any] | None = None) -> list[str]:
    labels = [f"roadmap:{role}"]
    if leaf:
        labels.append(f"slice:{leaf['slice']}")
        labels.append(f"proof:{leaf['proof']}")
        if leaf["status"] == "provisional":
            labels.append("spec:draft")
        if leaf["status"] == "history":
            labels.append("roadmap:history")
    return labels


LABELS = {
    "roadmap:parent": ("Capability tracker; milestones belong to its delivery leaves", "5319E7"),
    "roadmap:leaf": ("Independently closable roadmap component", "0E8A16"),
    "roadmap:history": ("Imported delivery history; not retroactively certified", "6E7781"),
    "spec:draft": ("Blocked until its product specification is settled", "D4C5F9"),
    "proof:playable": ("Requires operator approval of the playable Scratch build", "FBCA04"),
    "proof:operator": ("Requires an operator-run acceptance or audit", "B60205"),
    "proof:historical": ("Uses original delivery evidence only", "6E7781"),
    "playtest-approved": ("Operator approved the playable build at the recorded commit", "0E8A16"),
}
for value in ["1", "2", "2a", *[str(number) for number in range(3, 22)]]:
    LABELS[f"slice:{value}"] = (f"Delivery slice {value}; derived from the committed roadmap", "1D76DB")


def ensure_label(repo: str, name: str, description: str, color: str) -> None:
    encoded = name.replace("/", "%2F")
    try:
        gh("api", f"repos/{repo}/labels/{encoded}")
    except RoadmapError:
        gh("api", "--method", "POST", f"repos/{repo}/labels", "-f", f"name={name}", "-f", f"description={description}", "-f", f"color={color}")


def patch_issue(repo: str, number: int, *, title: str, body: str, labels: list[str], milestone: int | None, state: str = "open") -> dict[str, Any]:
    args = ["api", "--method", "PATCH", f"repos/{repo}/issues/{number}", "-f", f"title={title}", "-f", f"body={body}", "-f", f"state={state}"]
    for label in labels:
        args += ["-f", f"labels[]={label}"]
    if milestone is None:
        args += ["-F", "milestone=null"]
    else:
        args += ["-F", f"milestone={milestone}"]
    value = gh(*args)
    if not isinstance(value, dict):
        raise RoadmapError(f"issue #{number} update returned an unexpected response")
    return value


def create_issue(repo: str, *, title: str, body: str, labels: list[str], milestone: int | None) -> dict[str, Any]:
    args = ["api", "--method", "POST", f"repos/{repo}/issues", "-f", f"title={title}", "-f", f"body={body}"]
    for label in labels:
        args += ["-f", f"labels[]={label}"]
    if milestone is not None:
        args += ["-F", f"milestone={milestone}"]
    value = gh(*args)
    if not isinstance(value, dict):
        raise RoadmapError("issue create returned an unexpected response")
    return value


def add_sub_issue(repo: str, parent_number: int, child_id: int) -> None:
    current = gh("api", f"repos/{repo}/issues/{parent_number}/sub_issues")
    if any(item.get("id") == child_id for item in (current or [])):
        return
    gh("api", "--method", "POST", f"repos/{repo}/issues/{parent_number}/sub_issues", "-F", f"sub_issue_id={child_id}")


def ensure_project_fields(journal: dict[str, Any]) -> None:
    current = gh("project", "field-list", "4", "--owner", "@me", "--format", "json")
    by_name = {field["name"]: field for field in current["fields"]}
    desired = {
        "Roadmap role": ("SINGLE_SELECT", "Parent,Leaf,Imported history"),
        "Delivery slice": ("TEXT", None),
        "Proof level": ("SINGLE_SELECT", "Playable,Operator,Historical"),
    }
    for name, (kind, options) in desired.items():
        if name not in by_name:
            args = ["project", "field-create", "4", "--owner", "@me", "--name", name, "--data-type", kind, "--format", "json"]
            if options:
                args += ["--single-select-options", options]
            gh(*args)
    refreshed = gh("project", "field-list", "4", "--owner", "@me", "--format", "json")
    journal["project_fields"] = {field["name"]: field for field in refreshed["fields"]}
    write_json(MIGRATION_PATH, journal)


def project_item(url: str) -> None:
    gh("project", "item-add", "4", "--owner", "@me", "--url", url, "--format", "json")


def project_edit(url: str, field: str, value: str) -> None:
    gh("project", "item-edit", "4", "--owner", "@me", "--url", url, "--field", field, "--value", value)


def apply(manifest: dict[str, Any], journal: dict[str, Any]) -> None:
    failures = validate_manifest(manifest)
    if failures:
        raise RoadmapError("manifest invalid:\n- " + "\n- ".join(failures))
    if journal.get("phase") not in {"snapshotted", "applying", "applied"}:
        raise RoadmapError("run snapshot before apply")
    repo = manifest["repository"]
    if api_issue(repo, 34).get("pull_request") is None:
        raise RoadmapError("PR #34 identity check failed")
    journal["phase"] = "applying"
    write_json(MIGRATION_PATH, journal)

    for name, (description, color) in LABELS.items():
        ensure_label(repo, name, description, color)

    live = live_key_index(repo)
    milestones = milestone_numbers(journal)
    parents = {parent["key"]: parent for parent in manifest["parents"]}

    for parent in manifest["parents"]:
        existing = parent.get("issue") or journal["parents"].get(parent["key"], {}).get("number")
        issue = live.get(parent["key"])
        if issue:
            existing = issue["number"]
        if existing:
            issue = patch_issue(repo, int(existing), title=parent["title"], body=parent_body(parent), labels=issue_labels("parent"), milestone=None, state="open")
        else:
            issue = create_issue(repo, title=parent["title"], body=parent_body(parent), labels=issue_labels("parent"), milestone=None)
        journal["parents"][parent["key"]] = {"number": issue["number"], "id": issue["id"], "node_id": issue["node_id"], "url": issue["html_url"]}
        write_json(MIGRATION_PATH, journal)

    for leaf in manifest["leaves"]:
        existing = leaf.get("issue") or journal["leaves"].get(leaf["key"], {}).get("number")
        issue = live.get(leaf["key"])
        if issue:
            existing = issue["number"]
        milestone = milestones.get(leaf["milestone"])
        if milestone is None:
            raise RoadmapError(f"missing milestone {leaf['milestone']}")
        body = leaf_body(leaf, parents[leaf["parent"]])
        labels = issue_labels("leaf", leaf)
        if existing:
            issue = patch_issue(repo, int(existing), title=leaf["title"], body=body, labels=labels, milestone=milestone, state="open")
        else:
            issue = create_issue(repo, title=leaf["title"], body=body, labels=labels, milestone=milestone)
        journal["leaves"][leaf["key"]] = {"number": issue["number"], "id": issue["id"], "node_id": issue["node_id"], "url": issue["html_url"]}
        write_json(MIGRATION_PATH, journal)

    for leaf in manifest["leaves"]:
        parent_number = journal["parents"][leaf["parent"]]["number"]
        child_id = journal["leaves"][leaf["key"]]["id"]
        add_sub_issue(repo, parent_number, child_id)

    ensure_project_fields(journal)
    for parent in manifest["parents"]:
        url = journal["parents"][parent["key"]]["url"]
        project_item(url)
        project_edit(url, "Roadmap role", "Parent")
        project_edit(url, "Work type", "Feature")
        project_edit(url, "Status", "Backlog")
    for leaf in manifest["leaves"]:
        url = journal["leaves"][leaf["key"]]["url"]
        project_item(url)
        role = "Imported history" if leaf["status"] == "history" else "Leaf"
        proof = leaf["proof"].title()
        project_edit(url, "Roadmap role", role)
        project_edit(url, "Delivery slice", str(leaf["slice"]))
        project_edit(url, "Proof level", proof)
        project_edit(url, "Work type", "Feature")
        project_edit(url, "Status", "Done" if leaf["status"] == "history" else "Backlog")

    # Historical leaves close last, after their parents, milestone, evidence and Project records exist.
    for leaf in manifest["leaves"]:
        if leaf["status"] != "history":
            continue
        number = journal["leaves"][leaf["key"]]["number"]
        patch_issue(repo, number, title=leaf["title"], body=leaf_body(leaf, parents[leaf["parent"]]), labels=issue_labels("leaf", leaf), milestone=milestones[leaf["milestone"]], state="closed")

    journal["phase"] = "applied"
    write_json(MIGRATION_PATH, journal)


def compare_pr34(journal: dict[str, Any]) -> list[str]:
    before = journal.get("snapshot", {}).get("pr34")
    if not before:
        return ["PR #34 snapshot is absent"]
    now = gh(
        "pr", "view", "34", "--repo", journal["repository"], "--json",
        "number,title,state,isDraft,body,headRefName,headRefOid,baseRefName,labels,milestone,projectItems,url",
    )
    return [] if now == before else ["PR #34 changed during the roadmap migration"]


def reconcile(manifest: dict[str, Any], journal: dict[str, Any]) -> list[str]:
    failures = validate_manifest(manifest)
    if journal.get("phase") not in {"applied", "reconciled"}:
        failures.append("migration is not applied")
        return failures
    repo = manifest["repository"]
    parents = {parent["key"]: parent for parent in manifest["parents"]}
    live = live_key_index(repo)
    expected_keys = set(parents) | {leaf["key"] for leaf in manifest["leaves"]}
    if set(live) != expected_keys:
        failures.append(f"live roadmap keys differ: missing {sorted(expected_keys-set(live))}; extra {sorted(set(live)-expected_keys)}")
    milestones = milestone_numbers(journal)
    for parent in manifest["parents"]:
        issue = live.get(parent["key"])
        if issue and issue.get("milestone") is not None:
            failures.append(f"{parent['key']}: parent must not have a milestone")
    for leaf in manifest["leaves"]:
        issue = live.get(leaf["key"])
        if not issue:
            continue
        actual = issue.get("milestone", {}).get("number") if issue.get("milestone") else None
        if actual != milestones.get(leaf["milestone"]):
            failures.append(f"{leaf['key']}: wrong milestone")
        should_close = leaf["status"] == "history"
        if (issue.get("state") == "closed") != should_close:
            failures.append(f"{leaf['key']}: wrong open/closed state")
        parent_number = journal["parents"][leaf["parent"]]["number"]
        children = gh("api", f"repos/{repo}/issues/{parent_number}/sub_issues") or []
        if sum(child.get("number") == issue["number"] for child in children) != 1:
            failures.append(f"{leaf['key']}: missing or duplicate native parent link")
    failures += compare_pr34(journal)
    if not failures:
        journal["phase"] = "reconciled"
        write_json(MIGRATION_PATH, journal)
    return failures


def render_handoff(manifest: dict[str, Any], journal: dict[str, Any]) -> str:
    number = journal.get("leaves", {}).get("difficulty.models-live-state", {}).get("number")
    if not number:
        raise RoadmapError("slice-7 issue has not been created")
    return "\n".join(
        [
            "PR #34 remains the active slice-7 build. Before submission:",
            f"1. Replace the stale `Part of #17` with `Closes #{number}` and `Part of #18`.",
            "2. Do not close any slice-8 integration leaf; enemy-dependent play criteria remain open.",
            "3. Reconcile onto current main so the roadmap closure check runs.",
            "4. After the operator tests the exact head commit, record the playtest marker and apply `playtest-approved`.",
            f"5. Verify GitHub's computed closing-issue list contains only #{number}.",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["validate", "snapshot", "plan", "apply", "reconcile", "handoff"])
    parser.add_argument("--live", action="store_true", help="include a read-only live mutation diff")
    args = parser.parse_args(argv)
    manifest = read_json(MANIFEST_PATH)
    journal = read_json(MIGRATION_PATH) if MIGRATION_PATH.exists() else migration_template(manifest)
    if args.command == "validate":
        failures = validate_manifest(manifest)
        if failures:
            print("\n".join(f"- {failure}" for failure in failures), file=sys.stderr)
            return 1
        print(f"roadmap manifest valid: {len(manifest['parents'])} parents, {len(manifest['leaves'])} leaves")
        return 0
    if args.command == "snapshot":
        snapshot(manifest, journal)
        print("live roadmap, Project #4, milestones, and PR #34 snapshotted")
        return 0
    if args.command == "plan":
        failures = validate_manifest(manifest)
        if failures:
            print("\n".join(f"- {failure}" for failure in failures), file=sys.stderr)
            return 1
        value = {"parents": len(manifest["parents"]), "leaves": len(manifest["leaves"]), "history": sum(leaf["status"] == "history" for leaf in manifest["leaves"]), "provisional": sum(leaf["status"] == "provisional" for leaf in manifest["leaves"])}
        if args.live:
            value["live_diff"] = live_plan(manifest)
        print(json.dumps(value, indent=2))
        return 0
    if args.command == "apply":
        apply(manifest, journal)
        print("roadmap desired state applied; run reconcile")
        return 0
    if args.command == "reconcile":
        failures = reconcile(manifest, journal)
        if failures:
            print("\n".join(f"- {failure}" for failure in failures), file=sys.stderr)
            return 1
        print("roadmap live state matches the committed manifest; PR #34 is unchanged")
        return 0
    if args.command == "handoff":
        print(render_handoff(manifest, journal))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RoadmapError as exc:
        print(f"roadmap error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
