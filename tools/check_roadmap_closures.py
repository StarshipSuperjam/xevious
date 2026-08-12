#!/usr/bin/env python3
"""Refuse roadmap closures that are provisional, blocked, or unproved."""

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
MANIFEST = ROOT / "docs" / "roadmap" / "manifest.json"
MIGRATION = ROOT / "docs" / "roadmap" / "migration.json"
PLAYTEST_LABEL = "playtest-approved"
PLAYTEST_MARKER = re.compile(r"<!-- xevious-playtest:v1 commit=([0-9a-f]{40}) -->")
CLOSE_LINE = re.compile(r"(?im)^\s*(?:close[sd]?|fixe[sd]?|resolve[sd]?)\s+#(\d+)\s*$")


class ClosureError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClosureError(f"cannot read {path.relative_to(ROOT)}: {exc}") from exc
    if not isinstance(value, dict):
        raise ClosureError(f"{path.relative_to(ROOT)} must be a JSON object")
    return value


def gh(*args: str) -> Any:
    result = subprocess.run(["gh", *args], cwd=ROOT, text=True, capture_output=True, check=False)
    if result.returncode:
        raise ClosureError(result.stderr.strip() or result.stdout.strip())
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def indexes(manifest: dict[str, Any], migration: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[str, int]]:
    parents_by_number: dict[int, dict[str, Any]] = {}
    leaves_by_number: dict[int, dict[str, Any]] = {}
    numbers_by_key: dict[str, int] = {}
    parent_defs = {parent["key"]: parent for parent in manifest["parents"]}
    for key, row in migration.get("parents", {}).items():
        number = int(row["number"])
        parents_by_number[number] = parent_defs[key]
        numbers_by_key[key] = number
    leaf_defs = {leaf["key"]: leaf for leaf in manifest["leaves"]}
    for key, row in migration.get("leaves", {}).items():
        number = int(row["number"])
        leaves_by_number[number] = leaf_defs[key]
        numbers_by_key[key] = number
    return parents_by_number, leaves_by_number, numbers_by_key


def changed_files(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ClosureError(result.stderr.strip())
    return [line for line in result.stdout.splitlines() if line]


def issue_state(repo: str, number: int) -> str:
    injected = os.environ.get("ROADMAP_ISSUE_STATES_JSON")
    if injected:
        return json.loads(injected).get(str(number), "open")
    issue = gh("api", f"repos/{repo}/issues/{number}")
    return issue["state"]


def computed_closures(repo: str, pr: dict[str, Any]) -> set[int]:
    injected = os.environ.get("ROADMAP_CLOSURES_JSON")
    if injected:
        return {int(item) for item in json.loads(injected)}
    if os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        value = gh("pr", "view", str(pr["number"]), "--repo", repo, "--json", "closingIssuesReferences")
        return {int(item["number"]) for item in value.get("closingIssuesReferences", [])}
    return {int(number) for number in CLOSE_LINE.findall(pr.get("body") or "")}


def playtest_recorded(repo: str, pr: dict[str, Any]) -> bool:
    labels = {item["name"] if isinstance(item, dict) else item for item in pr.get("labels", [])}
    if PLAYTEST_LABEL not in labels:
        return False
    injected = os.environ.get("ROADMAP_COMMENTS_JSON")
    comments = json.loads(injected) if injected else gh("api", f"repos/{repo}/issues/{pr['number']}/comments?per_page=100")
    owner = repo.split("/", 1)[0].lower()
    head = pr["head"]["sha"]
    for comment in comments:
        author = (comment.get("user") or {}).get("login", "").lower()
        match = PLAYTEST_MARKER.search(comment.get("body") or "")
        if author == owner and match and match.group(1) == head:
            return True
    return False


def validate_pr(pr: dict[str, Any], manifest: dict[str, Any], migration: dict[str, Any]) -> list[str]:
    repo = manifest["repository"]
    parents_by_number, leaves_by_number, numbers_by_key = indexes(manifest, migration)
    closures = computed_closures(repo, pr)
    failures: list[str] = []
    for number in sorted(closures):
        if number in parents_by_number:
            failures.append(f"PR closes capability parent #{number}; close its independently complete leaves instead")
        leaf = leaves_by_number.get(number)
        if not leaf:
            continue
        if leaf["status"] == "provisional":
            failures.append(f"#{number} ({leaf['key']}) is provisional until its specification is settled")
            continue
        for blocker in leaf.get("blocked_by", []):
            blocker_number = numbers_by_key[blocker]
            if blocker_number not in closures and issue_state(repo, blocker_number) != "closed":
                failures.append(f"#{number} ({leaf['key']}) is blocked by open #{blocker_number} ({blocker})")
        if leaf["proof"] in {"playable", "operator"} and not playtest_recorded(repo, pr):
            failures.append(
                f"#{number} ({leaf['key']}) requires `playtest-approved` plus an owner comment "
                f"recording the exact tested head commit"
            )
        if leaf.get("records"):
            files = changed_files(pr["base"]["sha"], pr["head"]["sha"])
            if not any(path.startswith("docs/mechanics/") for path in files):
                failures.append(f"#{number} ({leaf['key']}) requires updated mechanics evidence")
    return failures


def source_pr_for_closed_issue(repo: str, number: int) -> int | None:
    timeline = gh(
        "api",
        "-H", "Accept: application/vnd.github+json",
        f"repos/{repo}/issues/{number}/timeline?per_page=100",
    )
    candidates: list[int] = []
    for event in timeline:
        source = event.get("source", {}).get("issue", {})
        if source.get("pull_request") and source.get("number"):
            candidates.append(int(source["number"]))
    return candidates[-1] if candidates else None


def load_pr(repo: str, number: int) -> dict[str, Any]:
    value = gh("api", f"repos/{repo}/pulls/{number}")
    issue = gh("api", f"repos/{repo}/issues/{number}")
    value["labels"] = issue.get("labels", [])
    return value


def validate_issue_event(event: dict[str, Any], manifest: dict[str, Any], migration: dict[str, Any]) -> list[str]:
    issue = event["issue"]
    number = int(issue["number"])
    parents_by_number, leaves_by_number, _ = indexes(manifest, migration)
    if number in parents_by_number:
        return [f"capability parent #{number} cannot close directly"]
    leaf = leaves_by_number.get(number)
    if not leaf or leaf["status"] == "history":
        return []
    pr_number = source_pr_for_closed_issue(manifest["repository"], number)
    if pr_number is None:
        return [f"roadmap leaf #{number} closed without a delivering pull request"]
    return validate_pr(load_pr(manifest["repository"], pr_number), manifest, migration)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["pr", "issue-event"])
    parser.add_argument("--event", type=Path, default=Path(os.environ.get("GITHUB_EVENT_PATH", "")))
    args = parser.parse_args(argv)
    manifest = read_json(MANIFEST)
    migration = read_json(MIGRATION)
    if migration.get("phase") not in {"applied", "reconciled"}:
        print("roadmap migration is not active; closure projection not enforced yet")
        return 0
    event = read_json(args.event)
    failures = validate_pr(event["pull_request"], manifest, migration) if args.mode == "pr" else validate_issue_event(event, manifest, migration)
    if failures:
        print("roadmap closure refused:\n- " + "\n- ".join(failures), file=sys.stderr)
        return 1
    print("roadmap closures satisfy the committed evidence contract")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ClosureError as exc:
        print(f"roadmap closure check failed closed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
