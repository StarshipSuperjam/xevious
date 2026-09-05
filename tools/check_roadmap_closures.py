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
MANIFEST_REL = "docs/roadmap/manifest.json"
MANIFEST = ROOT / MANIFEST_REL
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


def file_at_revision(revision: str, path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{revision}:{path}"], cwd=ROOT, text=True, capture_output=True, check=False
    )
    return result.stdout if result.returncode == 0 else ""


def manifest_at_revision(revision: str) -> dict[str, Any]:
    """The roadmap manifest as committed at a git revision, or fail closed.

    ``file_at_revision`` returns "" on any git error (an unfetched sha, a deleted
    file). For the manifest that "" is never a legitimate answer, so we raise
    ``ClosureError`` rather than treat an unreadable manifest as an empty one — the
    check refuses the closure instead of certifying it on missing evidence.
    """
    text = file_at_revision(revision, MANIFEST_REL)
    if not text.strip():
        raise ClosureError(
            f"could not read {MANIFEST_REL} at {revision[:12]}; refusing closures fail-closed "
            "(the delivering commit may not be fetched, e.g. a squash/rebase-merged head)"
        )
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClosureError(f"{MANIFEST_REL} at {revision[:12]} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ClosureError(f"{MANIFEST_REL} at {revision[:12]} must be a JSON object")
    return value


def added_test_lines(base: str, head: str) -> str:
    result = subprocess.run(
        ["git", "diff", "--unified=0", f"{base}...{head}", "--", "tests"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ClosureError(result.stderr.strip())
    return "\n".join(
        line[1:] for line in result.stdout.splitlines() if line.startswith("+") and not line.startswith("+++")
    )


def paginated(endpoint: str) -> list[dict[str, Any]]:
    value = gh("api", "--paginate", "--slurp", endpoint)
    pages = value if not value or isinstance(value[0], list) else [value]
    return [item for page in pages for item in page]


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
    comments = json.loads(injected) if injected else paginated(f"repos/{repo}/issues/{pr['number']}/comments?per_page=100")
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
    files = changed_files(pr["base"]["sha"], pr["head"]["sha"]) if closures else []
    added_tests = added_test_lines(pr["base"]["sha"], pr["head"]["sha"]) if closures else ""
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
        for dependency_slice in manifest.get("slice_dependencies", {}).get(str(leaf.get("slice")), []):
            for dependency in manifest["leaves"]:
                if str(dependency["slice"]) != dependency_slice or dependency["status"] == "history":
                    continue
                dependency_number = numbers_by_key[dependency["key"]]
                if dependency_number not in closures and issue_state(repo, dependency_number) != "closed":
                    failures.append(
                        f"#{number} ({leaf['key']}) depends on open slice-{dependency_slice} "
                        f"leaf #{dependency_number} ({dependency['key']})"
                    )
        if leaf["proof"] in {"playable", "operator"} and not playtest_recorded(repo, pr):
            failures.append(
                f"#{number} ({leaf['key']}) requires `playtest-approved` plus an owner comment "
                f"recording the exact tested head commit"
            )
        if leaf.get("records"):
            mechanics_files = [path for path in files if path.startswith("docs/mechanics/") and path.endswith(".md")]
            evidence = "\n".join(file_at_revision(pr["head"]["sha"], path) for path in mechanics_files)
            for record in leaf["records"]:
                if not re.search(rf"(?<![A-Z0-9-]){re.escape(record)}(?![A-Z0-9-])", evidence):
                    failures.append(f"#{number} ({leaf['key']}) requires changed mechanics evidence for {record}")
        for criterion in leaf.get("criteria", []):
            obligation = criterion.split(".", 1)[0]
            success = re.search(rf"roadmap-evidence:\s*{re.escape(obligation)}\s+success\b", added_tests, re.IGNORECASE)
            failure = re.search(rf"roadmap-evidence:\s*{re.escape(obligation)}\s+failure\b", added_tests, re.IGNORECASE)
            if not success or not failure:
                failures.append(
                    f"#{number} ({leaf['key']}) requires newly added success and failure evidence markers for {obligation}"
                )
    return failures


def _manifest_leaf(manifest: dict[str, Any], key: str) -> dict[str, Any] | None:
    for leaf in manifest.get("leaves", []):
        if isinstance(leaf, dict) and leaf.get("key") == key:
            return leaf
    return None


def delivery_recording_failures(repo: str, pr: dict[str, Any], leaves_by_number: dict[int, dict[str, Any]]) -> list[str]:
    """Every roadmap leaf this PR closes must record its own delivery in the manifest.

    A slice PR that closes a leaf issue must, in the SAME PR, flip that leaf from
    ``planned`` at the PR base to ``history`` with ``delivered_by == pr.number`` at the
    PR head — the edit ``roadmap.py deliver --pr N`` makes. That is what keeps the
    manifest from silently lagging main: GitHub closes the issue at merge, and the same
    merge lands the manifest that records it, so the two agree by construction.

    A leaf already ``history`` at the base was recorded by an earlier PR; a later PR
    that merely lists it in its closing references is not blocked. A leaf absent at the
    base is reported by name, never a ``KeyError``. The two ``git show`` reads run only
    when the PR actually closes a leaf, matching ``validate_pr``'s own gating.
    """
    closures = computed_closures(repo, pr)
    leaf_numbers = sorted(number for number in closures if number in leaves_by_number)
    if not leaf_numbers:
        return []
    base = manifest_at_revision(pr["base"]["sha"])
    head = manifest_at_revision(pr["head"]["sha"])
    number = pr["number"]
    fix = f"run `python3 tools/roadmap.py deliver --pr {number}` and commit the manifest edit into this PR"
    failures: list[str] = []
    for closed in leaf_numbers:
        key = leaves_by_number[closed]["key"]
        base_leaf = _manifest_leaf(base, key)
        if base_leaf is not None and base_leaf.get("status") == "history":
            continue  # already recorded on main by an earlier PR; this PR only references it
        if base_leaf is None:
            failures.append(f"#{closed} ({key}) is closed by this PR but is not a leaf in the manifest at its base; {fix}")
            continue
        if base_leaf.get("status") != "planned":
            failures.append(
                f"#{closed} ({key}) is {base_leaf.get('status')} at this PR's base, not planned — "
                "settle its status before a PR may record it delivered"
            )
            continue
        head_leaf = _manifest_leaf(head, key)
        if not head_leaf or head_leaf.get("status") != "history" or head_leaf.get("delivered_by") != number:
            failures.append(
                f"#{closed} ({key}) is closed by this PR but the manifest at its head does not record it "
                f'delivered (needs status "history", delivered_by {number}); {fix}'
            )
    return failures


def source_pr_for_closed_issue(repo: str, number: int) -> int | None:
    timeline = paginated(f"repos/{repo}/issues/{number}/timeline?per_page=100")
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
    pr = load_pr(manifest["repository"], pr_number)
    if not pr.get("merged_at"):
        return [f"roadmap leaf #{number} closed without a merged delivering pull request"]
    if number not in computed_closures(manifest["repository"], pr):
        return [f"roadmap leaf #{number} is not in PR #{pr_number}'s computed closing list"]
    return validate_pr(pr, manifest, migration) + delivery_recording_failures(
        manifest["repository"], pr, leaves_by_number
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["pr", "issue-event"])
    parser.add_argument("--event", type=Path, default=Path(os.environ.get("GITHUB_EVENT_PATH", "")))
    args = parser.parse_args(argv)
    manifest = read_json(MANIFEST)
    migration = read_json(MIGRATION)
    event = read_json(args.event)
    if args.mode == "pr":
        payload_pr = event.get("pull_request")
        if not payload_pr and event.get("issue", {}).get("pull_request"):
            payload_pr = load_pr(manifest["repository"], int(event["issue"]["number"]))
        if not payload_pr:
            raise ClosureError("event does not identify a pull request")
        # Always load current labels/body/head instead of trusting a stale rerun payload.
        current_pr = load_pr(manifest["repository"], int(payload_pr["number"]))
        _, leaves_by_number, _ = indexes(manifest, migration)
        failures = validate_pr(current_pr, manifest, migration) + delivery_recording_failures(
            manifest["repository"], current_pr, leaves_by_number
        )
    else:
        failures = validate_issue_event(event, manifest, migration)
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
