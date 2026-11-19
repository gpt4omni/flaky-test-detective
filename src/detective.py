#!/usr/bin/env python3
"""Flaky Test Detective — MVP core engine.

Parses JUnit XML + optional shard metadata, detects same-SHA fail->pass flakes
(both per-test and via the GitHub Actions Workflow Runs API), maintains a
rolling JSON state file, classifies isolation vs. timing/race patterns, and
renders a Markdown summary / sticky PR comment.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STICKY_MARKER = "<!-- flaky-test-detective -->"
CLEAN_RUN_CLOSE_THRESHOLD = 50


@dataclass
class TestObservation:
    test_id: str
    outcome: str  # "passed" | "failed" | "skipped"
    duration_sec: float = 0.0
    message: str = ""
    commit_sha: str = ""
    run_id: str = ""
    run_attempt: int = 1
    shard: str = "default"
    runner_id: str = ""
    seed: str = ""
    test_order: int = 0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def _load_sidecar_metadata(xml_path: Path) -> dict[str, Any]:
    """Load optional `<stem>.meta.json` next to a JUnit XML file."""
    meta_path = xml_path.with_suffix(".meta.json")
    if meta_path.is_file():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def parse_junit_file(
    xml_path: Path,
    *,
    commit_sha: str = "",
    run_id: str = "",
    run_attempt: int = 1,
    default_shard: str = "default",
    default_runner_id: str = "",
    default_seed: str = "",
) -> list[TestObservation]:
    """Parse a JUnit XML report into TestObservation records."""
    meta = _load_sidecar_metadata(xml_path)
    sha = str(meta.get("commit_sha") or commit_sha)
    rid = str(meta.get("run_id") or run_id)
    attempt = int(meta.get("run_attempt") or run_attempt)
    shard = str(meta.get("shard") or default_shard)
    runner_id = str(meta.get("runner_id") or default_runner_id)
    seed = str(meta.get("seed") or default_seed)

    tree = ET.parse(xml_path)
    root = tree.getroot()

    testcases: list[ET.Element] = []
    if root.tag == "testcase":
        testcases = [root]
    else:
        testcases = list(root.iter("testcase"))

    observations: list[TestObservation] = []
    for idx, tc in enumerate(testcases, start=1):
        classname = (tc.attrib.get("classname") or "").strip()
        name = (tc.attrib.get("name") or "unknown").strip()
        test_id = f"{classname}::{name}" if classname else name
        try:
            duration = float(tc.attrib.get("time") or 0.0)
        except ValueError:
            duration = 0.0

        failure = tc.find("failure")
        error = tc.find("error")
        skipped = tc.find("skipped")

        if failure is not None or error is not None:
            node = failure if failure is not None else error
            outcome = "failed"
            msg = (node.attrib.get("message") or (node.text or "")).strip()
            msg = msg.splitlines()[0][:200] if msg else "Test failed"
        elif skipped is not None:
            outcome = "skipped"
            msg = ""
        else:
            outcome = "passed"
            msg = ""

        observations.append(
            TestObservation(
                test_id=test_id,
                outcome=outcome,
                duration_sec=duration,
                message=msg,
                commit_sha=sha,
                run_id=rid,
                run_attempt=attempt,
                shard=shard,
                runner_id=runner_id,
                seed=seed,
                test_order=idx,
            )
        )
    return observations


def load_state(state_path: Path) -> dict[str, Any]:
    if state_path.is_file():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("tests"), dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"version": 1, "tests": {}, "job_flakes": []}


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def classify_flake(history: list[dict[str, Any]]) -> str:
    """Classify a flaky test as isolation/shared-state vs. timing/race."""
    failed_entries = [h for h in history if h.get("outcome") == "failed"]
    passed_entries = [h for h in history if h.get("outcome") == "passed"]
    if not failed_entries or not passed_entries:
        return "none"

    failed_shards = {h.get("shard", "") for h in failed_entries if h.get("shard")}
    passed_shards = {h.get("shard", "") for h in passed_entries if h.get("shard")}
    failed_seeds = {h.get("seed", "") for h in failed_entries if h.get("seed")}
    passed_seeds = {h.get("seed", "") for h in passed_entries if h.get("seed")}

    # Fails exclusively on a subset of shards or seeds while passing on others
    if failed_shards and passed_shards and failed_shards.isdisjoint(passed_shards):
        return "isolation/shared-state (shard-specific)"
    if failed_seeds and passed_seeds and failed_seeds.isdisjoint(passed_seeds):
        return "isolation/shared-state (seed-specific)"

    # Fails and passes on the same shard/seed/SHA -> timing or concurrency race
    return "timing/race"


def update_state(
    state: dict[str, Any],
    observations: list[TestObservation],
    *,
    window_size: int = 200,
) -> dict[str, Any]:
    """Merge new observations into rolling state and recompute flake metrics."""
    tests: dict[str, Any] = state.setdefault("tests", {})

    for obs in observations:
        if obs.outcome == "skipped":
            continue
        record = tests.setdefault(
            obs.test_id,
            {
                "first_seen_sha": obs.commit_sha,
                "history": [],
                "flake_events": 0,
                "consecutive_clean_runs": 0,
                "is_flaky": False,
                "flake_rate": 0.0,
                "failure_rate": 0.0,
                "classification": "none",
                "last_failure_message": "",
            },
        )

        entry = {
            "outcome": obs.outcome,
            "commit_sha": obs.commit_sha,
            "run_id": obs.run_id,
            "run_attempt": obs.run_attempt,
            "shard": obs.shard,
            "runner_id": obs.runner_id,
            "seed": obs.seed,
            "test_order": obs.test_order,
            "duration_sec": obs.duration_sec,
            "timestamp": obs.timestamp,
        }
        history: list[dict[str, Any]] = record["history"]
        history.append(entry)
        if len(history) > window_size:
            history[:] = history[-window_size:]

        if obs.outcome == "failed" and obs.message:
            record["last_failure_message"] = obs.message

    # Recompute metrics per test
    for test_id, record in tests.items():
        history = record["history"]
        total = len(history)
        if total == 0:
            continue

        # Group outcomes by commit SHA to detect same-SHA fail <-> pass transitions
        by_sha: dict[str, set[str]] = {}
        for h in history:
            sha = h.get("commit_sha") or "unknown"
            by_sha.setdefault(sha, set()).add(h["outcome"])

        flaky_shas = [
            sha for sha, outcomes in by_sha.items() if {"passed", "failed"} <= outcomes
        ]
        failures = sum(1 for h in history if h["outcome"] == "failed")

        # Count trailing clean passes
        clean = 0
        for h in reversed(history):
            if h["outcome"] == "passed":
                clean += 1
            else:
                break

        record["flake_events"] = len(flaky_shas)
        record["consecutive_clean_runs"] = clean
        record["failure_rate"] = round(failures / total, 4)

        # A test is considered flaky if it both passed and failed on the same SHA
        # and has not yet cleared the 50-consecutive-clean-runs bar.
        is_flaky = len(flaky_shas) > 0 and clean < CLEAN_RUN_CLOSE_THRESHOLD
        record["is_flaky"] = is_flaky
        record["flake_rate"] = round(failures / total, 4) if is_flaky else 0.0
        record["classification"] = classify_flake(history) if is_flaky else "none"

    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return state


def detect_same_sha_job_flakes(
    repo: str, commit_sha: str, token: str
) -> list[dict[str, Any]]:
    """Query GitHub Actions API for workflow runs that failed and passed on the same SHA."""
    if not repo or not commit_sha or not token:
        return []

    url = f"https://api.github.com/repos/{repo}/actions/runs?head_sha={commit_sha}&per_page=30"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "flaky-test-detective",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []

    by_workflow: dict[str, set[str]] = {}
    for run in payload.get("workflow_runs", []):
        name = run.get("name") or str(run.get("workflow_id"))
        conclusion = run.get("conclusion")
        if conclusion in {"success", "failure"}:
            by_workflow.setdefault(name, set()).add(conclusion)

    flakes = []
    for name, conclusions in sorted(by_workflow.items()):
        if {"success", "failure"} <= conclusions:
            flakes.append(
                {
                    "workflow": name,
                    "commit_sha": commit_sha,
                    "signal": "same-sha-rerun-pass",
                }
            )
    return flakes


def render_markdown_report(
    state: dict[str, Any],
    current_failures: list[TestObservation],
    job_flakes: list[dict[str, Any]],
) -> str:
    """Render a concise Markdown summary suitable for PR comments and Job Summaries."""
    tests: dict[str, Any] = state.get("tests", {})
    flaky_tests = [
        (tid, rec) for tid, rec in tests.items() if rec.get("is_flaky")
    ]
    flaky_tests.sort(
        key=lambda item: (item[1].get("flake_rate", 0.0), item[1].get("flake_events", 0)),
        reverse=True,
    )

    matched_failures = [
        obs for obs in current_failures if tests.get(obs.test_id, {}).get("is_flaky")
    ]

    lines = [
        STICKY_MARKER,
        "## Flaky Test Detective",
        "",
    ]

    if matched_failures:
        lines.append(
            f"> **Known flake detected:** {len(matched_failures)} failing test(s) match a known flaky signature. Safe to re-run — likely not caused by this change."
        )
        lines.append("")
        for obs in matched_failures:
            rec = tests[obs.test_id]
            pct = rec["flake_rate"] * 100
            lines.append(
                f"- `{obs.test_id}` — **{pct:.1f}% flake rate** across {len(rec['history'])} runs (`{rec['classification']}`)"
            )
        lines.append("")
    elif flaky_tests:
        lines.append(
            f"Tracking **{len(flaky_tests)}** known flaky test(s) across {len(tests)} total monitored tests."
        )
        lines.append("")
    else:
        lines.append(
            f"All clear — no flaky tests detected across **{len(tests)}** monitored test(s)."
        )
        lines.append("")

    if job_flakes:
        lines.append("### Same-SHA Workflow Re-run Signals")
        for jf in job_flakes:
            lines.append(
                f"- Workflow **{jf['workflow']}** failed and passed on `{jf['commit_sha'][:7]}`"
            )
        lines.append("")

    if flaky_tests:
        lines.extend(
            [
                "### Flake Leaderboard",
                "",
                "| Test | Flake Rate | Same-SHA Flakes | Classification | Clean Streak |",
                "| --- | ---: | ---: | --- | ---: |",
            ]
        )
        for tid, rec in flaky_tests[:15]:
            pct = rec["flake_rate"] * 100
            streak = f"{rec['consecutive_clean_runs']}/{CLEAN_RUN_CLOSE_THRESHOLD}"
            lines.append(
                f"| `{tid}` | {pct:.1f}% | {rec['flake_events']} | {rec['classification']} | {streak} |"
            )
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def upsert_pr_comment(
    repo: str, pr_number: int, body: str, token: str
) -> bool:
    """Create or update the sticky Flaky Test Detective comment on a PR."""
    if not repo or not pr_number or not token:
        return False

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "flaky-test-detective",
    }
    list_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments?per_page=100"
    existing_id: int | None = None
    try:
        req = urllib.request.Request(list_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            comments = json.loads(resp.read().decode("utf-8"))
        for c in comments:
            if STICKY_MARKER in (c.get("body") or ""):
                existing_id = int(c["id"])
                break
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return False

    payload = json.dumps({"body": body}).encode("utf-8")
    if existing_id is not None:
        url = f"https://api.github.com/repos/{repo}/issues/comments/{existing_id}"
        method = "PATCH"
    else:
        url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
        method = "POST"

    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10):
            return True
    except (urllib.error.URLError, TimeoutError):
        return False


def _detect_pr_number() -> int | None:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).is_file():
        return None
    try:
        data = json.loads(Path(event_path).read_text(encoding="utf-8"))
        pr = data.get("pull_request")
        if isinstance(pr, dict) and pr.get("number"):
            return int(pr["number"])
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flaky Test Detective")
    parser.add_argument(
        "--junit-glob",
        default="**/junit*.xml",
        help="Glob pattern for JUnit XML files (default: **/junit*.xml)",
    )
    parser.add_argument(
        "--state-file",
        default=".flaky-detective/state.json",
        help="Path to rolling JSON state file",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=200,
        help="Rolling history window per test (default: 200)",
    )
    parser.add_argument(
        "--commit-sha",
        default=os.environ.get("GITHUB_SHA", "local"),
        help="Commit SHA associated with this run",
    )
    parser.add_argument(
        "--run-id",
        default=os.environ.get("GITHUB_RUN_ID", "local"),
        help="Workflow run ID",
    )
    parser.add_argument(
        "--run-attempt",
        type=int,
        default=int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
        help="Workflow run attempt number",
    )
    parser.add_argument(
        "--shard",
        default=os.environ.get("FLAKY_DETECTIVE_SHARD", "default"),
        help="Default shard label when no sidecar metadata is present",
    )
    parser.add_argument(
        "--seed",
        default=os.environ.get("FLAKY_DETECTIVE_SEED", ""),
        help="Default test randomizer seed",
    )
    parser.add_argument(
        "--post-pr-comment",
        action="store_true",
        help="Post or update a sticky PR comment when running on a pull request",
    )
    parser.add_argument(
        "--write-summary",
        action="store_true",
        help="Append Markdown report to GITHUB_STEP_SUMMARY if set",
    )
    parser.add_argument(
        "--report-file",
        default="",
        help="Optional path to write the rendered Markdown report",
    )
    args = parser.parse_args(argv)

    matched_files = sorted(
        Path(p) for p in glob.glob(args.junit_glob, recursive=True) if Path(p).is_file()
    )

    observations: list[TestObservation] = []
    for xml_path in matched_files:
        observations.extend(
            parse_junit_file(
                xml_path,
                commit_sha=args.commit_sha,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                default_shard=args.shard,
                default_runner_id=os.environ.get("RUNNER_NAME", ""),
                default_seed=args.seed,
            )
        )

    state_path = Path(args.state_file)
    state = load_state(state_path)
    update_state(state, observations, window_size=args.window_size)

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    job_flakes = detect_same_sha_job_flakes(repo, args.commit_sha, token)
    if job_flakes:
        state["job_flakes"] = job_flakes

    save_state(state_path, state)

    current_failures = [o for o in observations if o.outcome == "failed"]
    report = render_markdown_report(state, current_failures, job_flakes)

    print(report)

    if args.report_file:
        rp = Path(args.report_file)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(report, encoding="utf-8")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if args.write_summary and summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")

    if args.post_pr_comment:
        pr_number = _detect_pr_number()
        if pr_number and (
            current_failures
            or any(r.get("is_flaky") for r in state["tests"].values())
            or job_flakes
        ):
            upsert_pr_comment(repo, pr_number, report, token)

    # Emit GitHub Actions outputs when running inside a workflow
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        flaky_count = sum(1 for r in state["tests"].values() if r.get("is_flaky"))
        known_matches = sum(
            1
            for o in current_failures
            if state["tests"].get(o.test_id, {}).get("is_flaky")
        )
        with open(gh_output, "a", encoding="utf-8") as fh:
            fh.write(f"flaky-count={flaky_count}\n")
            fh.write(f"matched-known-flakes={known_matches}\n")
            fh.write(f"parsed-files={len(matched_files)}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
